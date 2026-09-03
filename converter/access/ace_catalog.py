"""Read-only reader for the Jet4 / ACE (``.mdb`` 2000+, ``.accdb``) catalog.

Jet 4.0 and every ACE generation share one on-disk page layout; only the
version byte at ``0x14`` differs.  That is why a single reader covers Access
2000 through Access 2019 files while Jet 3.x needs its own module: Jet3 stores
names in the database code page with one-byte length prefixes and a jump table
in the row trailer, none of which exists here.

Differences from :mod:`converter.access.jet_catalog` that actually matter::

    Aspect                  Jet3                 Jet4 / ACE
    ----------------------  -------------------  ---------------------------
    Page size               2048                 4096
    Table definition body   offset 43            offset 63
    Index entry / column    8 / 18 bytes         12 / 25 bytes
    Column name prefix      1 byte               2 bytes, UTF-16LE text
    Data page row count     offset 8             offset 12
    Row column count        1 byte               2 bytes
    Variable offsets        1 byte + jump table  2 bytes, no jump table

Only ``MSysObjects`` is read: this reader recovers *which objects exist*, not
their definitions.  Saying so plainly matters, because a name list is a
discovery result and must never be reported as a translation.

The file is opened once, read-only.  Nothing is written, no lock file is
created, and no Access, COM or ODBC component is involved.
"""

from __future__ import annotations

import os
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["AceCatalogError", "read_catalog", "FORMAT_VERSIONS"]

_PAGE_SIZE = 4096

_SIGNATURE_OFFSET = 0x04
_SIGNATURE_LENGTH = 16
_JET_SIGNATURE = b"Standard Jet DB\x00"
_ACE_SIGNATURE = b"Standard ACE DB\x00"
_VERSION_OFFSET = 0x14

#: Version byte -> (format family, human description).  An unlisted version is
#: refused rather than parsed on the assumption that it resembles a known one.
FORMAT_VERSIONS: Dict[int, Tuple[str, str]] = {
    0x01: ("jet4", "Jet 4.0 (Access 2000/2002/2003 .mdb)"),
    0x02: ("ace", "ACE 12 (Access 2007 .accdb)"),
    0x03: ("ace", "ACE 14 (Access 2010/2013 .accdb)"),
    0x04: ("ace", "ACE 16 (Access 2016+ .accdb)"),
    0x05: ("ace", "ACE 17 (Access 2019+ .accdb)"),
}

_CATALOG_TDEF_PAGE = 2
_PAGE_TYPE_DATA = 0x01
_PAGE_TYPE_TDEF = 0x02

_ROW_DELETED_FLAG = 0x8000
_ROW_POINTER_FLAG = 0x4000
_ROW_OFFSET_MASK = 0x1FFF

_TDEF_NEXT_PAGE = 4
_TDEF_NUM_REAL_INDEXES = 51
_TDEF_NUM_COLS = 45
_TDEF_BODY_START = 63
_TDEF_REAL_INDEX_SIZE = 12
_TDEF_COLUMN_SIZE = 25

_DATA_NUM_ROWS = 12
_DATA_ROW_TABLE = 14

_COLUMN_FLAG_FIXED = 0x01

_MAX_TDEF_PAGES = 512
_MAX_COLUMNS = 4096

#: MSysObjects.Type, a signed 16-bit value.  Types 4 and 6 are linked tables.
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


class AceCatalogError(Exception):
    """Raised when a file is not a readable Jet4/ACE catalog."""


def _slice(buffer: bytes, offset: int, length: int) -> bytes:
    if offset < 0 or length < 0 or offset + length > len(buffer):
        raise AceCatalogError(
            "malformed structure: read of %d bytes at offset %d is out of bounds "
            "for a %d byte buffer" % (length, offset, len(buffer))
        )
    return bytes(buffer[offset : offset + length])


def _uint16(buffer: bytes, offset: int) -> int:
    return int.from_bytes(_slice(buffer, offset, 2), "little", signed=False)


def _int16(buffer: bytes, offset: int) -> int:
    return int.from_bytes(_slice(buffer, offset, 2), "little", signed=True)


def _uint32(buffer: bytes, offset: int) -> int:
    return int.from_bytes(_slice(buffer, offset, 4), "little", signed=False)


def _normalize_path(path: Any) -> str:
    if isinstance(path, str):
        return path
    if isinstance(path, (bytes, bytearray, memoryview)):
        raise TypeError("read_catalog() expects str or os.PathLike[str]")
    if hasattr(path, "__fspath__"):
        resolved = os.fspath(path)
        if isinstance(resolved, str):
            return resolved
    raise TypeError("read_catalog() expects str or os.PathLike[str]")


def _decode_text(raw: bytes) -> str:
    """Decode Jet4/ACE text, including the compressed-UTF-16 representation.

    Access may store a string as UTF-16LE, or "compressed" - a ``0xFF 0xFE``
    marker followed by one byte per character.  A further marker toggles back
    to two bytes per character, so a mixed string round-trips correctly.
    """
    if raw[:2] != b"\xff\xfe":
        if len(raw) % 2:
            raw = raw[:-1]
        return raw.decode("utf-16-le", errors="strict")
    out: List[str] = []
    compressed = True
    index = 2
    while index < len(raw):
        if raw[index : index + 2] == b"\xff\xfe":
            compressed = not compressed
            index += 2
            continue
        if compressed:
            out.append(chr(raw[index]))
            index += 1
        else:
            chunk = raw[index : index + 2]
            if len(chunk) < 2:
                break
            out.append(chunk.decode("utf-16-le", errors="strict"))
            index += 2
    return "".join(out)


def _validate_name(text: str) -> str:
    if "�" in text:
        raise AceCatalogError(
            "invalid catalog object name: contains a replacement character"
        )
    for character in text:
        if character == "\x00" or unicodedata.category(character) == "Cc":
            raise AceCatalogError(
                "invalid catalog object name: contains control character %r"
                % (character,)
            )
    return text


class _Column:
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
    if not isinstance(page_number, int) or page_number < 0 or page_number >= page_count:
        raise AceCatalogError(
            "invalid page reference %r: file contains %d pages"
            % (page_number, page_count)
        )
    handle.seek(page_number * _PAGE_SIZE)
    data = handle.read(_PAGE_SIZE)
    if len(data) != _PAGE_SIZE:
        raise AceCatalogError(
            "truncated database: page %d could not be read in full" % page_number
        )
    return data


def _read_tdef_buffer(handle, page_count: int) -> bytes:
    first = _read_page(handle, _CATALOG_TDEF_PAGE, page_count)
    if first[0] != _PAGE_TYPE_TDEF:
        raise AceCatalogError(
            "invalid MSysObjects table definition page: page %d has page type "
            "0x%02x instead of 0x02; the database may be password protected or "
            "encrypted" % (_CATALOG_TDEF_PAGE, first[0])
        )
    buffer = bytearray(first)
    visited = {_CATALOG_TDEF_PAGE}
    next_page = _uint32(first, _TDEF_NEXT_PAGE)
    while next_page:
        if len(visited) >= _MAX_TDEF_PAGES:
            raise AceCatalogError(
                "malformed MSysObjects definition: chain exceeds %d pages"
                % _MAX_TDEF_PAGES
            )
        if next_page in visited:
            raise AceCatalogError(
                "invalid page reference: definition chain revisits page %d" % next_page
            )
        page = _read_page(handle, next_page, page_count)
        if page[0] != _PAGE_TYPE_TDEF:
            raise AceCatalogError(
                "invalid page reference: continuation page %d is not a table "
                "definition page" % next_page
            )
        visited.add(next_page)
        buffer.extend(page[8:])
        next_page = _uint32(page, _TDEF_NEXT_PAGE)
    return bytes(buffer)


def _parse_columns(tdef: bytes) -> List[_Column]:
    num_cols = _uint16(tdef, _TDEF_NUM_COLS)
    num_real_indexes = _uint32(tdef, _TDEF_NUM_REAL_INDEXES)
    if num_cols == 0 or num_cols > _MAX_COLUMNS:
        raise AceCatalogError(
            "malformed MSysObjects definition: implausible column count %d" % num_cols
        )
    if num_real_indexes > _MAX_COLUMNS:
        raise AceCatalogError(
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
                column_number=_uint16(descriptor, 5),
                is_fixed=bool(descriptor[15] & _COLUMN_FLAG_FIXED),
                fixed_offset=_uint16(descriptor, 21),
                var_index=_uint16(descriptor, 7),
                size=_uint16(descriptor, 23),
            )
        )
    # Jet4 column names: a two-byte byte-length prefix, then UTF-16LE.
    for column in columns:
        length = _uint16(tdef, position)
        position += 2
        column.name = _decode_text(_slice(tdef, position, length))
        position += length
    return columns


def _find_catalog_columns(columns: List[_Column]) -> Tuple[_Column, _Column]:
    name_column: Optional[_Column] = None
    type_column: Optional[_Column] = None
    for column in columns:
        lowered = column.name.lower()
        if lowered == "name" and name_column is None:
            name_column = column
        elif lowered == "type" and type_column is None:
            type_column = column
    if name_column is None or type_column is None:
        raise AceCatalogError(
            "MSysObjects definition is missing the Name and/or Type column"
        )
    if name_column.is_fixed:
        raise AceCatalogError(
            "MSysObjects definition is malformed: Name is not variable-length"
        )
    if not type_column.is_fixed or type_column.size != 2:
        raise AceCatalogError(
            "MSysObjects definition is malformed: Type is not a fixed 2-byte column"
        )
    return name_column, type_column


def _variable_offsets(page: bytes, row_start: int, row_end: int, null_size: int) -> List[int]:
    """Return the row's variable-column offsets, relative to ``row_start``.

    Jet4 row trailer, from the end of the row downwards::

        [ null bitmap            ]  null_size bytes
        [ variable column count  ]  2 bytes
        [ offset[0] .. offset[n] ]  2 bytes each, descending addresses

    There is no jump table: the offsets are 16 bit and can address the whole
    row directly.  The last entry is the end-of-data offset.
    """
    count_pointer = row_end - null_size - 2
    if count_pointer <= row_start:
        raise AceCatalogError("malformed catalog row: trailer does not fit in the row")
    count = _uint16(page, count_pointer)
    if count > _MAX_COLUMNS:
        raise AceCatalogError(
            "malformed catalog row: implausible variable column count %d" % count
        )
    offset_pointer = count_pointer - 2
    if offset_pointer - count * 2 < row_start:
        raise AceCatalogError(
            "malformed catalog row: variable offset table overruns the row"
        )
    return [_uint16(page, offset_pointer - index * 2) for index in range(count + 1)]


def _is_null(page: bytes, row_end: int, null_size: int, column_number: int) -> bool:
    byte_index = column_number // 8
    if byte_index >= null_size:
        return True
    value = page[row_end - null_size + byte_index]
    return not (value >> (column_number % 8)) & 0x01


def _parse_row(
    page: bytes,
    row_start: int,
    row_end: int,
    name_column: _Column,
    type_column: _Column,
) -> Optional[Tuple[bytes, int]]:
    if row_end - row_start < 6:
        raise AceCatalogError("malformed catalog row: row is too small to be valid")
    num_cols = _uint16(page, row_start)
    if num_cols == 0 or num_cols > _MAX_COLUMNS:
        return None
    null_size = (num_cols + 7) // 8
    if row_start + 2 + null_size + 2 > row_end:
        raise AceCatalogError("malformed catalog row: trailer does not fit in the row")
    if type_column.column_number >= num_cols or name_column.column_number >= num_cols:
        return None
    if _is_null(page, row_end, null_size, type_column.column_number):
        return None
    if _is_null(page, row_end, null_size, name_column.column_number):
        return None

    type_offset = row_start + 2 + type_column.fixed_offset
    if type_offset < row_start + 2 or type_offset + 2 > row_end - null_size:
        raise AceCatalogError(
            "malformed catalog row: fixed column data lies outside the row"
        )
    type_code = _int16(page, type_offset)

    offsets = _variable_offsets(page, row_start, row_end, null_size)
    var_index = name_column.var_index
    if var_index + 1 >= len(offsets):
        return None
    start, end = offsets[var_index], offsets[var_index + 1]
    if start > end or row_start + end > row_end:
        raise AceCatalogError(
            "malformed catalog row: variable column bounds are inconsistent"
        )
    raw_name = _slice(page, row_start + start, end - start)
    if not raw_name:
        return None
    return raw_name, type_code


_MAX_OVERFLOW_HOPS = 8


def _row_bounds(page: bytes, row_number: int) -> Tuple[int, int, int]:
    """Return ``(entry_flags, row_start, row_end)`` for one row slot."""
    entry = _uint16(page, _DATA_ROW_TABLE + row_number * 2)
    row_start = entry & _ROW_OFFSET_MASK
    if row_number == 0:
        row_end = _PAGE_SIZE
    else:
        row_end = _uint16(page, _DATA_ROW_TABLE + (row_number - 1) * 2) & _ROW_OFFSET_MASK
    return entry, row_start, row_end


def _follow_overflow(
    page: bytes, row_start: int, row_end: int, read_page
) -> Optional[Tuple[bytes, int, int]]:
    """Resolve an overflow row slot to the page and bounds holding its data.

    ACE stores a row that no longer fits on its home page as a four byte
    pointer: one byte row number, three byte page number.  Skipping those
    slots - which is harmless in Jet3, where they are rare - silently loses a
    large share of the catalog in ACE files, so they are followed here.
    """
    for _ in range(_MAX_OVERFLOW_HOPS):
        if row_end - row_start < 4:
            return None
        row_number = page[row_start]
        page_number = int.from_bytes(
            _slice(page, row_start + 1, 3), "little", signed=False
        )
        target = read_page(page_number)
        if target[0] != _PAGE_TYPE_DATA:
            return None
        row_count = _uint16(target, _DATA_NUM_ROWS)
        if row_number >= row_count:
            return None
        entry, row_start, row_end = _row_bounds(target, row_number)
        page = target
        # The slot on the target page carries the deleted flag because the row
        # is not a live row *there*; it exists only as the overflow payload of
        # the pointer that led here.  Rejecting it would discard real rows.
        if not entry & _ROW_POINTER_FLAG:
            if row_start >= row_end or row_end > _PAGE_SIZE:
                return None
            return page, row_start, row_end
    return None


def _parse_data_page(
    page: bytes,
    page_number: int,
    name_column: _Column,
    type_column: _Column,
    read_page,
) -> List[Dict[str, object]]:
    num_rows = _uint16(page, _DATA_NUM_ROWS)
    if _DATA_ROW_TABLE + num_rows * 2 > _PAGE_SIZE:
        raise AceCatalogError(
            "malformed catalog page %d: row count %d does not fit in the page"
            % (page_number, num_rows)
        )
    results: List[Dict[str, object]] = []
    previous_start = _PAGE_SIZE
    for index in range(num_rows):
        entry = _uint16(page, _DATA_ROW_TABLE + index * 2)
        row_start = entry & _ROW_OFFSET_MASK
        row_end = previous_start
        previous_start = row_start
        if entry & _ROW_DELETED_FLAG:
            continue
        if row_start < _DATA_ROW_TABLE or row_start >= row_end or row_end > _PAGE_SIZE:
            raise AceCatalogError(
                "malformed catalog page %d: row %d has an out-of-range offset %d"
                % (page_number, index, row_start)
            )
        row_page = page
        if entry & _ROW_POINTER_FLAG:
            resolved = _follow_overflow(page, row_start, row_end, read_page)
            if resolved is None:
                continue
            row_page, row_start, row_end = resolved
        parsed = _parse_row(row_page, row_start, row_end, name_column, type_column)
        if parsed is None:
            continue
        raw_name, type_code = parsed
        name = _validate_name(_decode_text(raw_name))
        results.append(
            {
                "name": name,
                "type_code": int(type_code),
                "kind": _OBJECT_KINDS.get(type_code, "unknown(%d)" % type_code),
            }
        )
    return results


def read_catalog(path: "str | os.PathLike[str]") -> Dict[str, object]:
    """Read the ``MSysObjects`` catalog of a Jet4 or ACE database.

    Returns a dict with ``format`` (``"jet4"`` or ``"ace"``),
    ``format_description``, ``version_byte``, ``page_size`` and ``objects``:
    a list of ``{"name", "type_code", "kind"}`` in deterministic on-disk order.

    The result is a superset of Access's Navigation Pane, because the reader
    works on raw pages rather than through DAO's visibility rules.  It contains
    no object definitions at all; recovering those from a binary Access file is
    a separate, unimplemented capability.
    """
    resolved = _normalize_path(path)
    size = os.path.getsize(resolved)
    if size < _PAGE_SIZE:
        raise AceCatalogError(
            "truncated database: file is smaller than one %d-byte page" % _PAGE_SIZE
        )
    if size % _PAGE_SIZE:
        raise AceCatalogError(
            "truncated database: file length %d is not a multiple of the %d-byte "
            "page size" % (size, _PAGE_SIZE)
        )
    page_count = size // _PAGE_SIZE

    objects: List[Dict[str, object]] = []
    with open(resolved, "rb") as handle:
        header = _read_page(handle, 0, page_count)
        signature = _slice(header, _SIGNATURE_OFFSET, _SIGNATURE_LENGTH)
        if signature not in (_JET_SIGNATURE, _ACE_SIGNATURE):
            raise AceCatalogError(
                "not a Jet/ACE database: the file signature is missing"
            )
        version = header[_VERSION_OFFSET]
        if version not in FORMAT_VERSIONS:
            raise AceCatalogError(
                "unsupported database version 0x%02x; this reader handles Jet4 "
                "and ACE only" % version
            )
        family, description = FORMAT_VERSIONS[version]

        tdef = _read_tdef_buffer(handle, page_count)
        columns = _parse_columns(tdef)
        name_column, type_column = _find_catalog_columns(columns)

        for page_number in range(1, page_count):
            page = _read_page(handle, page_number, page_count)
            if page[0] != _PAGE_TYPE_DATA:
                continue
            if _uint32(page, 4) != _CATALOG_TDEF_PAGE:
                continue
            objects.extend(
                _parse_data_page(
                    page,
                    page_number,
                    name_column,
                    type_column,
                    lambda number: _read_page(handle, number, page_count),
                )
            )

    return {
        "format": family,
        "format_description": description,
        "version_byte": version,
        "page_size": _PAGE_SIZE,
        "objects": objects,
    }
