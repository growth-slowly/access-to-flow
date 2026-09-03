"""Read-only, pure-Python reader for the Jet3 (Access 97) system catalog.

This module parses just enough of the Jet 3.x on-disk format to return the
names and type codes stored in ``MSysObjects``.  It is deliberately offline and
dependency free: the only imports are from the Python standard library, the
database file is opened once in binary read mode, and nothing is ever written
back (no lock files, no sidecar files, no timestamp updates).

Jet 3.x is a reverse engineered format; the structure offsets documented below
were derived from the public format description and cross-checked against the
byte level fixtures used by this project's tests.

Jet3 differs from Jet4/ACE in ways that are easy to confuse and that cause
silently garbled output when mixed up.  The differences that matter here:

===================  ==========================  =========================
Aspect               Jet3 (Access 95/97)         Jet4 / ACE
===================  ==========================  =========================
Page size            2048                        4096
Version byte @0x14   0x00                        0x01 (Jet4), >=0x02 (ACE)
Text storage         8-bit database code page    UTF-16LE (+ compression)
Column name prefix   1 byte                      2 bytes
Column descriptor    18 bytes                    25 bytes
Row column count     1 byte                      2 bytes
Row var-col offsets  1 byte (+ jump table)       2 bytes
===================  ==========================  =========================

Only Jet3 is implemented; every other version fails loudly instead of being
misparsed.
"""

from __future__ import annotations

import os
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["JetCatalogError", "read_catalog"]


# --------------------------------------------------------------------------
# Format constants
# --------------------------------------------------------------------------

_PAGE_SIZE = 2048  # Jet3 pages are always 2048 bytes; never infer this.

_SIGNATURE_OFFSET = 0x04
_SIGNATURE_LENGTH = 16
_JET_SIGNATURE = b"Standard Jet DB\x00"
_ACE_SIGNATURE = b"Standard ACE DB\x00"

_VERSION_OFFSET = 0x14
_JET3_VERSION = 0x00

# Database code page, uint16 LE.  Reverse engineered location; an unrecognised
# value is treated as metadata noise and falls back to cp1252 rather than being
# reported as a structural failure.
_CODE_PAGE_OFFSET = 0x3C

_CATALOG_TDEF_PAGE = 2  # MSysObjects table definition lives on page 2.

_PAGE_TYPE_DATA = 0x01
_PAGE_TYPE_TDEF = 0x02

# Row offset entries in a data page carry flags in their high bits.
_ROW_DELETED_FLAG = 0x8000
_ROW_POINTER_FLAG = 0x4000
_ROW_OFFSET_MASK = 0x1FFF

# TDEF (table definition) field offsets, Jet3.
_TDEF_NEXT_PAGE = 4
_TDEF_NUM_COLS = 25
_TDEF_NUM_REAL_INDEXES = 31
_TDEF_BODY_START = 43
_TDEF_REAL_INDEX_SIZE = 8
_TDEF_COLUMN_SIZE = 18

_MAX_TDEF_PAGES = 256  # Bounds the definition chain (anti-cycle / anti-DoS).
_MAX_COLUMNS = 4096  # Jet's own hard limit is far lower; this is a sanity cap.

_COLUMN_FLAG_FIXED = 0x01

_DEFAULT_ENCODING = "cp1252"
_CODE_PAGE_CODECS = {
    0: "cp1252",  # unspecified -> ANSI Latin-1 default
    437: "cp437",
    850: "cp850",
    852: "cp852",
    866: "cp866",
    874: "cp874",
    932: "cp932",
    936: "cp936",
    949: "cp949",
    950: "cp950",
    1250: "cp1250",
    1251: "cp1251",
    1252: "cp1252",
    1253: "cp1253",
    1254: "cp1254",
    1255: "cp1255",
    1256: "cp1256",
    1257: "cp1257",
    1258: "cp1258",
}

# MSysObjects.Type is a *signed* 16-bit integer; forms/reports/macros/modules
# use negative codes.  Note that types 4 and 6 are linked tables: consumers
# filtering on ``kind == "table"`` will not see them.
_OBJECT_KINDS = {
    1: "table",
    2: "database",
    3: "container",
    4: "linked_odbc_table",
    5: "query",
    6: "linked_table",
    8: "relationship",
    -32768: "form",
    -32766: "macro",
    -32764: "report",
    -32761: "module",
    -32758: "users",
    -32757: "database_document",
    -32756: "data_access_page",
}


class JetCatalogError(Exception):
    """Raised when a file is not a readable Jet3 catalog.

    Deliberately not an :class:`OSError` subclass so that a missing input file
    keeps surfacing as :class:`FileNotFoundError`.
    """


# --------------------------------------------------------------------------
# Low level helpers (every read is bounds checked)
# --------------------------------------------------------------------------


def _slice(buffer: bytes, offset: int, length: int) -> bytes:
    """Return ``buffer[offset:offset + length]`` or raise on any overrun."""
    if offset < 0 or length < 0 or offset + length > len(buffer):
        raise JetCatalogError(
            "malformed Jet3 structure: read of %d bytes at offset %d is out of "
            "bounds for a %d byte buffer" % (length, offset, len(buffer))
        )
    return bytes(buffer[offset : offset + length])


def _byte_at(buffer: bytes, offset: int) -> int:
    """Return one unsigned byte, rejecting negative/out-of-range offsets.

    Guards against Python's negative-index wraparound, which would otherwise
    turn a malformed structure into silently wrong data.
    """
    if offset < 0 or offset >= len(buffer):
        raise JetCatalogError(
            "malformed Jet3 structure: byte offset %d is out of bounds" % offset
        )
    return buffer[offset]


def _uint16(buffer: bytes, offset: int) -> int:
    return int.from_bytes(_slice(buffer, offset, 2), "little", signed=False)


def _int16(buffer: bytes, offset: int) -> int:
    return int.from_bytes(_slice(buffer, offset, 2), "little", signed=True)


def _uint32(buffer: bytes, offset: int) -> int:
    return int.from_bytes(_slice(buffer, offset, 4), "little", signed=False)


def _normalize_path(path: Any) -> str:
    """Accept ``str`` and ``os.PathLike[str]`` only; anything else is a TypeError."""
    if isinstance(path, str):
        return path
    if isinstance(path, (bytes, bytearray, memoryview)):
        raise TypeError(
            "read_catalog() expects str or os.PathLike[str], got bytes-like path"
        )
    if hasattr(path, "__fspath__"):
        resolved = os.fspath(path)
        if not isinstance(resolved, str):
            raise TypeError(
                "read_catalog() expects os.PathLike[str]; __fspath__ returned "
                f"{type(resolved).__name__}"
            )
        return resolved
    raise TypeError(
        "read_catalog() expects str or os.PathLike[str], got "
        f"{type(path).__name__}"
    )


def _classify(type_code: int) -> str:
    """Map a MSysObjects type code to a lowercase kind, with a stable fallback."""
    return _OBJECT_KINDS.get(type_code, "unknown(%d)" % type_code)


def _decode_name(raw: bytes, encoding: str) -> str:
    """Decode Jet3 catalog text (8-bit, database code page, uncompressed).

    Strict error handling is used on purpose: a decoding failure is evidence of
    a misparse and must not be papered over with U+FFFD replacement characters.
    """
    try:
        text = raw.decode(encoding, errors="strict")
    except (UnicodeDecodeError, LookupError) as exc:
        raise JetCatalogError(
            "undecodable catalog name using code page codec %r: %s" % (encoding, exc)
        ) from exc
    return _validate_name(text)


def _validate_name(text: str) -> str:
    """Reject NUL/control/replacement characters; keep spaces and accents."""
    if "\ufffd" in text:
        raise JetCatalogError(
            "invalid catalog object name: contains a Unicode replacement character"
        )
    for character in text:
        if character == "\x00" or unicodedata.category(character) == "Cc":
            raise JetCatalogError(
                "invalid catalog object name: contains control character %r"
                % (character,)
            )
    return text


# --------------------------------------------------------------------------
# Table definition parsing
# --------------------------------------------------------------------------


class _Column:
    """One Jet3 column descriptor from a table definition."""

    __slots__ = ("name", "column_number", "is_fixed", "fixed_offset", "var_index", "size")

    def __init__(
        self,
        column_number: int,
        is_fixed: bool,
        fixed_offset: int,
        var_index: int,
        size: int,
    ) -> None:
        self.name = ""
        self.column_number = column_number
        self.is_fixed = is_fixed
        self.fixed_offset = fixed_offset
        self.var_index = var_index
        self.size = size


def _read_page(handle, page_number: int, page_count: int) -> bytes:
    """Read one whole 2048-byte page, validating the page reference first."""
    if not isinstance(page_number, int) or page_number < 0 or page_number >= page_count:
        raise JetCatalogError(
            "invalid page reference %r: file contains %d pages"
            % (page_number, page_count)
        )
    handle.seek(page_number * _PAGE_SIZE)
    data = handle.read(_PAGE_SIZE)
    if len(data) != _PAGE_SIZE:
        raise JetCatalogError(
            "truncated database: page %d could not be read in full" % page_number
        )
    return data


def _read_tdef_buffer(handle, page_count: int) -> bytes:
    """Concatenate the MSysObjects table definition chain starting at page 2.

    Continuation pages contribute their bytes from offset 8 onwards.  The walk
    is bounded and cycle safe.
    """
    first = _read_page(handle, _CATALOG_TDEF_PAGE, page_count)
    if first[0] != _PAGE_TYPE_TDEF:
        raise JetCatalogError(
            "invalid MSysObjects table definition page: page %d has page type "
            "0x%02x instead of 0x02; the database may be password-protected, "
            "encoded, or not a Jet3 database" % (_CATALOG_TDEF_PAGE, first[0])
        )

    buffer = bytearray(first)
    visited = {_CATALOG_TDEF_PAGE}
    next_page = _uint32(first, _TDEF_NEXT_PAGE)
    while next_page:
        if len(visited) >= _MAX_TDEF_PAGES:
            raise JetCatalogError(
                "malformed MSysObjects definition: table definition chain "
                "exceeds %d pages" % _MAX_TDEF_PAGES
            )
        if next_page in visited:
            raise JetCatalogError(
                "invalid page reference: table definition chain revisits page %d"
                % next_page
            )
        page = _read_page(handle, next_page, page_count)
        if page[0] != _PAGE_TYPE_TDEF:
            raise JetCatalogError(
                "invalid page reference: continuation page %d is not a table "
                "definition page" % next_page
            )
        visited.add(next_page)
        buffer.extend(page[8:])
        next_page = _uint32(page, _TDEF_NEXT_PAGE)
    return bytes(buffer)


def _parse_columns(tdef: bytes) -> List[_Column]:
    """Parse the Jet3 column descriptor array and the 1-byte-prefixed names."""
    num_cols = _uint16(tdef, _TDEF_NUM_COLS)
    num_real_indexes = _uint32(tdef, _TDEF_NUM_REAL_INDEXES)
    if num_cols == 0 or num_cols > _MAX_COLUMNS:
        raise JetCatalogError(
            "malformed MSysObjects definition: implausible column count %d" % num_cols
        )
    if num_real_indexes > _MAX_COLUMNS:
        raise JetCatalogError(
            "malformed MSysObjects definition: implausible index count %d"
            % num_real_indexes
        )

    position = _TDEF_BODY_START + num_real_indexes * _TDEF_REAL_INDEX_SIZE
    columns: List[_Column] = []
    for _ in range(num_cols):
        descriptor = _slice(tdef, position, _TDEF_COLUMN_SIZE)
        position += _TDEF_COLUMN_SIZE
        columns.append(
            _Column(
                column_number=descriptor[1],
                is_fixed=bool(descriptor[13] & _COLUMN_FLAG_FIXED),
                fixed_offset=_uint16(descriptor, 14),
                var_index=_uint16(descriptor, 3),
                size=_uint16(descriptor, 16),
            )
        )

    # Column names follow the descriptors, each as <1-byte length><bytes>.
    # (Jet4 uses a 2-byte length here; applying that rule to Jet3 desynchronises
    # the whole name list.)
    for column in columns:
        length = _byte_at(tdef, position)
        position += 1
        column.name = _slice(tdef, position, length).decode("cp1252", errors="replace")
        position += length
    return columns


def _find_catalog_columns(columns: List[_Column]) -> Tuple[_Column, _Column]:
    """Locate the variable-length ``Name`` and the fixed 2-byte ``Type`` column."""
    name_column: Optional[_Column] = None
    type_column: Optional[_Column] = None
    for column in columns:
        lowered = column.name.lower()
        if lowered == "name" and name_column is None:
            name_column = column
        elif lowered == "type" and type_column is None:
            type_column = column
    if name_column is None or type_column is None:
        raise JetCatalogError(
            "MSysObjects definition is missing the Name and/or Type column"
        )
    if name_column.is_fixed:
        raise JetCatalogError(
            "MSysObjects definition is malformed: Name is not a variable-length column"
        )
    if not type_column.is_fixed or type_column.size != 2:
        raise JetCatalogError(
            "MSysObjects definition is malformed: Type is not a fixed 2-byte column"
        )
    return name_column, type_column


# --------------------------------------------------------------------------
# Row parsing
# --------------------------------------------------------------------------


def _variable_offsets(
    page: bytes, row_start: int, row_end: int, null_size: int
) -> List[int]:
    """Return the row's variable-column offsets, relative to ``row_start``.

    Jet3 row trailer, laid out from the *end* of the row downwards::

        [ null bitmap (null_size bytes) ]
        [ variable column count       ]  1 byte
        [ jump table                  ]  num_jumps bytes
        [ offset[0] .. offset[n-1]    ]  1 byte each, descending addresses
        [ end-of-data offset          ]  1 byte

    Because the offsets are a single byte, a row longer than 256 bytes cannot
    address its own tail.  Jet3 therefore stores a jump table whose entries name
    the variable-column index at which each 256-byte boundary is crossed; the
    reader adds ``256 * jumps_used`` to subsequent offsets.  The returned list
    holds ``count + 1`` entries (the last one being end-of-data).
    """
    last = row_end - 1  # inclusive index of the row's final byte
    count_pointer = last - null_size
    count = _byte_at(page, count_pointer)

    row_length = row_end - row_start
    num_jumps = (row_length - 1) // 256
    offset_pointer = last - null_size - num_jumps - 1

    # A trailing dummy jump entry still occupies a byte (so ``offset_pointer``
    # stays correct) but must never be consumed by the jump walk below.
    if offset_pointer - row_start - count < 0:
        raise JetCatalogError("malformed catalog row: variable offset table overruns row")
    if (offset_pointer - row_start - count) // 256 < num_jumps:
        num_jumps -= 1

    if offset_pointer - count <= row_start:
        raise JetCatalogError(
            "malformed catalog row: variable offset table does not fit in the row"
        )

    offsets: List[int] = []
    jumps_used = 0
    for index in range(count + 1):
        while jumps_used < num_jumps and index == _byte_at(
            page, last - null_size - jumps_used - 1
        ):
            jumps_used += 1
        offsets.append(_byte_at(page, offset_pointer - index) + jumps_used * 256)
    return offsets


def _is_null(page: bytes, row_end: int, null_size: int, column_number: int) -> bool:
    """A set bit in the trailing null bitmap means the column is NOT null."""
    byte_index = column_number // 8
    if byte_index >= null_size:
        return True
    value = _byte_at(page, row_end - null_size + byte_index)
    return not (value >> (column_number % 8)) & 0x01


def _parse_row(
    page: bytes,
    row_start: int,
    row_end: int,
    name_column: _Column,
    type_column: _Column,
) -> Optional[Tuple[bytes, int]]:
    """Return ``(raw_name_bytes, type_code)`` for one Jet3 catalog row.

    Returns ``None`` for rows that simply do not carry a usable catalog entry
    (column absent from this row, NULL name/type, or an empty name).  Genuine
    structural damage raises :class:`JetCatalogError`.
    """
    if row_end - row_start < 3:
        raise JetCatalogError("malformed catalog row: row is too small to be valid")

    # Jet3 rows start with a single byte holding the column count (Jet4: two).
    num_cols = _byte_at(page, row_start)
    if num_cols == 0:
        return None
    null_size = (num_cols + 7) // 8
    if row_start + 1 + null_size + 2 > row_end:
        raise JetCatalogError("malformed catalog row: trailer does not fit in the row")

    if type_column.column_number >= num_cols or name_column.column_number >= num_cols:
        return None
    if _is_null(page, row_end, null_size, type_column.column_number):
        return None
    if _is_null(page, row_end, null_size, name_column.column_number):
        return None

    # Fixed-length column data begins immediately after the column count byte.
    type_offset = row_start + 1 + type_column.fixed_offset
    if type_offset < row_start + 1 or type_offset + 2 > row_end - null_size:
        raise JetCatalogError(
            "malformed catalog row: fixed column data lies outside the row"
        )
    type_code = _int16(page, type_offset)

    offsets = _variable_offsets(page, row_start, row_end, null_size)
    var_index = name_column.var_index
    if var_index + 1 >= len(offsets):
        return None
    start = offsets[var_index]
    end = offsets[var_index + 1]
    if start > end or row_start + end > row_end:
        raise JetCatalogError(
            "malformed catalog row: variable column bounds are inconsistent"
        )
    raw_name = _slice(page, row_start + start, end - start)
    if not raw_name:
        return None
    return raw_name, type_code


def _parse_data_page(
    page: bytes,
    page_number: int,
    name_column: _Column,
    type_column: _Column,
    encoding: str,
) -> List[Dict[str, object]]:
    """Parse every live catalog row on one data page, in on-disk row order."""
    num_rows = _uint16(page, 8)
    if 10 + num_rows * 2 > _PAGE_SIZE:
        raise JetCatalogError(
            "malformed catalog page %d: row count %d does not fit in the page"
            % (page_number, num_rows)
        )

    results: List[Dict[str, object]] = []
    previous_start = _PAGE_SIZE
    for index in range(num_rows):
        entry = _uint16(page, 10 + index * 2)
        row_start = entry & _ROW_OFFSET_MASK
        row_end = previous_start
        # Deleted and pointer rows still delimit their neighbours, so the row
        # boundary must be recorded before the row is skipped.
        previous_start = row_start
        if entry & (_ROW_DELETED_FLAG | _ROW_POINTER_FLAG):
            continue
        if row_start < 10 or row_start >= row_end or row_end > _PAGE_SIZE:
            raise JetCatalogError(
                "malformed catalog page %d: row %d has an out-of-range offset %d"
                % (page_number, index, row_start)
            )
        parsed = _parse_row(page, row_start, row_end, name_column, type_column)
        if parsed is None:
            continue
        raw_name, type_code = parsed
        name = _decode_name(raw_name, encoding)
        results.append(
            {"name": name, "type_code": int(type_code), "kind": _classify(type_code)}
        )
    return results


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def read_catalog(path: "str | os.PathLike[str]") -> Dict[str, object]:
    """Read the ``MSysObjects`` catalog of a Jet3 (Access 97) database.

    The database is opened read-only; nothing is written, and no lock or
    sidecar file is created.

    Args:
        path: Path to an ``.mdb`` file, as ``str`` or ``os.PathLike[str]``.

    Returns:
        A dict with the keys ``format`` (always ``"jet3"``), ``page_size``
        (always ``2048``), ``code_page`` (the raw header value), ``encoding``
        (the codec actually used) and ``objects`` -- a list of
        ``{"name", "type_code", "kind"}`` dicts in deterministic on-disk order
        (ascending page number, then ascending row index).

        The result is a superset of Access's Navigation Pane: system tables,
        containers and relationship rows are included, because this reader
        works on raw pages rather than through DAO's visibility rules.

    Raises:
        TypeError: ``path`` is neither ``str`` nor ``os.PathLike[str]``.
        FileNotFoundError: ``path`` does not exist.
        JetCatalogError: the file is truncated, is not a Jet database, is not
            Jet3, contains an invalid page reference, or has a malformed
            catalog structure.  No partial result is ever returned.
    """
    resolved = _normalize_path(path)

    size = os.path.getsize(resolved)  # FileNotFoundError propagates unchanged.
    if size < _PAGE_SIZE:
        raise JetCatalogError(
            "truncated database: file is smaller than one %d-byte Jet3 page"
            % _PAGE_SIZE
        )
    if size % _PAGE_SIZE:
        raise JetCatalogError(
            "truncated database: file length %d is not a multiple of the "
            "%d-byte Jet3 page size" % (size, _PAGE_SIZE)
        )
    page_count = size // _PAGE_SIZE

    objects: List[Dict[str, object]] = []
    with open(resolved, "rb") as handle:
        header = _read_page(handle, 0, page_count)

        signature = _slice(header, _SIGNATURE_OFFSET, _SIGNATURE_LENGTH)
        if signature not in (_JET_SIGNATURE, _ACE_SIGNATURE):
            raise JetCatalogError(
                "not a Jet database: missing the 'Standard Jet DB' signature"
            )
        version = header[_VERSION_OFFSET]
        if signature == _ACE_SIGNATURE or version != _JET3_VERSION:
            raise JetCatalogError(
                "unsupported database version 0x%02x; only Jet3 (Access 97) "
                "databases are supported by this reader" % version
            )

        code_page = _uint16(header, _CODE_PAGE_OFFSET)
        encoding = _CODE_PAGE_CODECS.get(code_page, _DEFAULT_ENCODING)

        tdef = _read_tdef_buffer(handle, page_count)
        columns = _parse_columns(tdef)
        name_column, type_column = _find_catalog_columns(columns)

        # Linear ascending scan: data pages belonging to MSysObjects name page 2
        # as their owning table definition.  Jet3 Text is capped at 255 bytes and
        # always stored inline, so long-value (LVAL) pages are irrelevant here.
        for page_number in range(1, page_count):
            page = _read_page(handle, page_number, page_count)
            if page[0] != _PAGE_TYPE_DATA:
                continue
            if _uint32(page, 4) != _CATALOG_TDEF_PAGE:
                continue
            objects.extend(
                _parse_data_page(page, page_number, name_column, type_column, encoding)
            )

    # Built fully before returning: a failure on any page aborts the whole read.
    return {
        "format": "jet3",
        "page_size": _PAGE_SIZE,
        "code_page": code_page,
        "encoding": encoding,
        "objects": objects,
    }
