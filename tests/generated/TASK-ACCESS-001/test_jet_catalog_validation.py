"""Public-interface and synthetic byte-level tests for the Jet3 reader."""

import pytest

from converter.access import JetCatalogError, read_catalog
from converter.access.jet_catalog import (
    JetCatalogError as ModuleJetCatalogError,
    read_catalog as module_read_catalog,
)


PAGE_SIZE = 2048


def _jet_header(version, size):
    """Return a minimal image with Jet's signature and version fields."""
    image = bytearray(size)
    image[0:4] = b"\x00\x01\x00\x00"
    image[4:20] = b"Standard Jet DB\x00"
    image[0x14] = version
    return bytes(image)


def _put_int(buffer, offset, value, width, *, signed=False):
    buffer[offset : offset + width] = value.to_bytes(width, "little", signed=signed)


def _catalog_tdef(row_count):
    """Build the Jet3 TDEF subset describing Id, ParentId, Name, and Type."""
    page = bytearray(PAGE_SIZE)
    page[0] = 0x02
    _put_int(page, 12, row_count, 4, signed=True)
    page[20] = 1
    _put_int(page, 25, 4, 2)
    _put_int(page, 27, 0, 4, signed=True)
    _put_int(page, 31, 0, 4, signed=True)

    descriptors = (
        (4, 0, None, 0),
        (4, 1, None, 4),
        (10, 2, 0, None),
        (3, 3, None, 8),
    )
    position = 43
    for field_type, column_number, variable_index, fixed_offset in descriptors:
        descriptor = bytearray(18)
        descriptor[0] = field_type
        descriptor[1] = column_number
        if variable_index is not None:
            _put_int(descriptor, 3, variable_index, 2)
        else:
            descriptor[13] = 1
            _put_int(descriptor, 14, fixed_offset, 2)
            _put_int(descriptor, 16, 4 if field_type == 4 else 2, 2)
        page[position : position + 18] = descriptor
        position += 18

    for name in (b"Id", b"ParentId", b"Name", b"Type"):
        page[position] = len(name)
        position += 1
        page[position : position + len(name)] = name
        position += len(name)
    return page


def _catalog_data_page(name_bytes, type_code, object_id):
    """Build one bounds-valid Jet3 data page containing one catalog row."""
    page = bytearray(PAGE_SIZE)
    page[0] = 0x01
    _put_int(page, 4, 2, 4)
    _put_int(page, 8, 1, 2)

    row_start = 1900
    _put_int(page, 10, row_start, 2)
    page[row_start] = 4
    _put_int(page, row_start + 1, object_id, 4, signed=True)
    _put_int(page, row_start + 5, 0, 4, signed=True)
    _put_int(page, row_start + 9, type_code, 2, signed=True)
    variable_start = 11
    variable_end = variable_start + len(name_bytes)
    page[row_start + variable_start : row_start + variable_end] = name_bytes

    # Jet3 variable offsets are stored in reverse order at the row's end.
    page[-4] = variable_end
    page[-3] = variable_start
    page[-2] = 1
    page[-1] = 0x0F
    return page


def _synthetic_catalog(objects):
    """Build a catalog where names are strings or already encoded byte strings."""
    image = bytearray(_jet_header(version=0, size=PAGE_SIZE * (3 + len(objects))))
    image[2 * PAGE_SIZE : 3 * PAGE_SIZE] = _catalog_tdef(len(objects))
    for index, (name, type_code) in enumerate(objects, start=3):
        encoded_name = name if isinstance(name, bytes) else name.encode("cp1252")
        image[index * PAGE_SIZE : (index + 1) * PAGE_SIZE] = _catalog_data_page(
            encoded_name, type_code, index
        )
    return bytes(image)


def test_public_symbols_are_reexported_without_wrapping():
    assert JetCatalogError is ModuleJetCatalogError
    assert read_catalog is module_read_catalog
    assert issubclass(JetCatalogError, Exception)


@pytest.mark.parametrize("bad_path", [None, 7, 3.5, b"database.mdb", object()])
def test_rejects_non_string_non_pathlike_inputs(bad_path):
    with pytest.raises(TypeError):
        read_catalog(bad_path)


@pytest.mark.parametrize("as_string", [False, True])
def test_missing_path_raises_file_not_found(tmp_path, as_string):
    missing = tmp_path / "does-not-exist.mdb"
    with pytest.raises(FileNotFoundError):
        read_catalog(str(missing) if as_string else missing)


@pytest.mark.parametrize("length", [0, 1, 20, 2047])
def test_truncated_byte_images_raise_catalog_error(tmp_path, length):
    database = tmp_path / f"truncated-{length}.mdb"
    database.write_bytes(b"\x00" * length)
    with pytest.raises(JetCatalogError):
        read_catalog(database)


def test_full_page_with_invalid_header_is_rejected(tmp_path):
    database = tmp_path / "not-jet.mdb"
    database.write_bytes(b"not a database".ljust(PAGE_SIZE, b"\x00"))
    with pytest.raises(JetCatalogError):
        read_catalog(str(database))


@pytest.mark.parametrize("version", [1, 2, 5, 255])
def test_non_jet3_versions_fail_clearly(tmp_path, version):
    database = tmp_path / f"unsupported-{version}.mdb"
    database.write_bytes(_jet_header(version=version, size=4096))
    with pytest.raises(JetCatalogError, match=r"(?i)(unsupported|version|jet|ace)"):
        read_catalog(database)


def test_recognizable_but_malformed_jet3_never_returns_results_or_sidecars(tmp_path):
    database = tmp_path / "malformed-jet3.mdb"
    original = _jet_header(version=0, size=PAGE_SIZE)
    database.write_bytes(original)
    with pytest.raises(JetCatalogError):
        read_catalog(database)
    assert database.read_bytes() == original
    assert sorted(item.name for item in tmp_path.iterdir()) == [database.name]


def test_out_of_range_tdef_continuation_page_is_rejected(tmp_path):
    database = tmp_path / "bad-page-reference.mdb"
    image = bytearray(_jet_header(version=0, size=PAGE_SIZE * 3))
    tdef = _catalog_tdef(0)
    _put_int(tdef, 4, 99, 4)
    image[2 * PAGE_SIZE : 3 * PAGE_SIZE] = tdef
    database.write_bytes(image)
    with pytest.raises(JetCatalogError):
        read_catalog(database)


def test_malformed_later_catalog_page_does_not_yield_partial_results(tmp_path):
    database = tmp_path / "partial-catalog.mdb"
    image = bytearray(_synthetic_catalog([("Customers", 1)]))
    malformed = bytearray(PAGE_SIZE)
    malformed[0] = 0x01
    _put_int(malformed, 4, 2, 4)
    _put_int(malformed, 8, 1, 2)
    _put_int(malformed, 10, PAGE_SIZE + 100, 2)
    image.extend(malformed)
    database.write_bytes(image)

    with pytest.raises(JetCatalogError):
        read_catalog(database)


def test_synthetic_catalog_decodes_cp1252_and_preserves_spaces(tmp_path):
    database = tmp_path / "synthetic-valid-jet3.mdb"
    original = _synthetic_catalog(
        [("Umsätze Report", -32764), ("Sales Query", 5), ("Future Thing", 1234)]
    )
    database.write_bytes(original)

    result = read_catalog(database)

    assert result["format"] == "jet3"
    assert result["page_size"] == PAGE_SIZE
    assert [
        {key: item[key] for key in ("name", "type_code", "kind")}
        for item in result["objects"]
    ] == [
        {"name": "Umsätze Report", "type_code": -32764, "kind": "report"},
        {"name": "Sales Query", "type_code": 5, "kind": "query"},
        {"name": "Future Thing", "type_code": 1234, "kind": "unknown(1234)"},
    ]
    assert database.read_bytes() == original
    assert sorted(item.name for item in tmp_path.iterdir()) == [database.name]


def test_all_required_object_type_codes_are_classified(tmp_path):
    database = tmp_path / "object-kinds.mdb"
    expected = [
        ("A table", 1, "table"),
        ("A query", 5, "query"),
        ("A form", -32768, "form"),
        ("A report", -32764, "report"),
        ("A macro", -32766, "macro"),
        ("A module", -32761, "module"),
    ]
    database.write_bytes(_synthetic_catalog([(name, code) for name, code, _ in expected]))

    result = read_catalog(database)

    assert [
        (item["name"], item["type_code"], item["kind"])
        for item in result["objects"]
    ] == expected


@pytest.mark.parametrize("raw_name", [b"Bad\x00Name", b"Bad\x01Name", b"Bad\x7fName"])
def test_control_character_in_catalog_name_is_structural_error(tmp_path, raw_name):
    database = tmp_path / "control-name.mdb"
    database.write_bytes(_synthetic_catalog([(raw_name, 5)]))
    with pytest.raises(JetCatalogError):
        read_catalog(database)


def test_pathlike_protocol_is_accepted_without_requiring_pathlib_type(tmp_path):
    database = tmp_path / "path-protocol.mdb"
    database.write_bytes(_jet_header(version=0, size=PAGE_SIZE))

    class PathProtocolOnly:
        def __fspath__(self):
            return str(database)

    with pytest.raises(JetCatalogError):
        read_catalog(PathProtocolOnly())
