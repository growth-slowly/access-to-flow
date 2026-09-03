"""Read-only regression coverage using the ignored, license-unclear nwind.mdb."""

import hashlib
import unicodedata

import pytest

from converter.access import read_catalog


SAMPLE_RELATIVE_PATH = (
    "samples/open_access_systems/mdbtools_testdata/original/nwind.mdb"
)
EXPECTED_SHA256 = "4682dfc91be526e6508948cc53adf5c63d70fdf7f2cc1f1403ee76b66ac914b2"


def _verified_nwind(request):
    """Locate the local-only sample and enforce the task's fixture identity."""
    sample = request.config.rootpath / SAMPLE_RELATIVE_PATH
    if not sample.is_file():
        pytest.skip(
            "local mdbtools_testdata nwind.mdb is absent; this ignored, "
            "license-unclear sample is required for the Jet3 integration test"
        )
    digest = hashlib.sha256(sample.read_bytes()).hexdigest()
    if digest != EXPECTED_SHA256:
        pytest.skip(
            "local nwind.mdb does not have the required SHA-256; refusing to "
            "use an unverified sample fixture"
        )
    return sample


def test_nwind_catalog_decoding_schema_classification_and_read_only_behavior(request):
    sample = _verified_nwind(request)
    original_bytes = sample.read_bytes()
    original_stat = sample.stat()
    original_directory = sorted(entry.name for entry in sample.parent.iterdir())

    first = read_catalog(str(sample))
    second = read_catalog(sample)

    assert first == second, "catalog order/results must be deterministic"
    assert first["format"] == "jet3"
    assert first["page_size"] == 2048
    assert isinstance(first["objects"], list)
    assert first["objects"]

    for catalog_object in first["objects"]:
        assert {"name", "type_code", "kind"} <= catalog_object.keys()
        assert isinstance(catalog_object["name"], str)
        assert catalog_object["name"]
        assert type(catalog_object["type_code"]) is int
        assert isinstance(catalog_object["kind"], str)
        assert catalog_object["kind"]

    names = {item["name"] for item in first["objects"]}
    assert {
        "Customers",
        "Orders",
        "Products",
        "Employees",
        "Order Details",
        "Umsätze",
    } <= names

    for name in names:
        assert "\ufffd" not in name
        assert "\x00" not in name
        assert not any(
            unicodedata.category(character) == "Cc" for character in name
        ), f"catalog name contains a control character: {name!r}"

    expected_type_kinds = {
        1: "table",
        5: "query",
        -32768: "form",
        -32764: "report",
        -32766: "macro",
        -32761: "module",
    }
    observed_pairs = {
        (item["type_code"], item["kind"]) for item in first["objects"]
    }
    for pair in expected_type_kinds.items():
        assert pair in observed_pairs

    # Reading must neither alter the database nor leave lock/sidecar artifacts.
    final_stat = sample.stat()
    assert sample.read_bytes() == original_bytes
    assert final_stat.st_size == original_stat.st_size
    assert final_stat.st_mtime_ns == original_stat.st_mtime_ns
    assert sorted(entry.name for entry in sample.parent.iterdir()) == original_directory
