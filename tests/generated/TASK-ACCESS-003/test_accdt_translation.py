from __future__ import annotations

import hashlib
import json
from pathlib import Path

from converter.access import translate_access_file


EXPECTED_COUNTS = {
    "table": 28,
    "query": 65,
    "form": 40,
    "report": 15,
    "macro": 21,
    "module": 18,
}


def _artifact(result: dict[str, object]) -> dict[str, object]:
    return result["ir"]["samples"][0]["artifacts"][0]


def test_real_northwind_accdt_becomes_truthful_intermediate_representation(
    northwind_dev_accdt: Path,
) -> None:
    result = translate_access_file(northwind_dev_accdt)
    artifact = _artifact(result)

    assert result["schema"] == "access-conversion-result/1"
    assert result["status"] == "partial"
    assert result["ir"]["schema"] == "access-ir/1"
    assert artifact["format_family"] == "accdt"
    assert artifact["declared_counts"] == EXPECTED_COUNTS
    assert len(artifact["objects"]) == 187
    assert artifact["count_only"] == []

    extraction = result["coverage"]["corpus"]["stages"]["extraction"]
    translation = result["coverage"]["corpus"]["stages"]["translation"]
    assert extraction["counts"]["complete"] == 187
    assert extraction["completion_percentage"] == 100.0
    # Semantic translation now runs on top of extraction.  These numbers are
    # the current, measured truth for this artifact and are asserted exactly:
    # a silent drift in either direction is a regression worth failing on.
    assert translation["counts"]["complete"] == 122
    assert translation["counts"]["partial"] == 65
    assert translation["counts"]["failed"] == 0
    assert translation["completion_percentage"] == 65.24


def test_representative_source_definitions_are_preserved(
    northwind_dev_accdt: Path,
) -> None:
    objects = _artifact(translate_access_file(northwind_dev_accdt))["objects"]
    by_identity = {
        (item["kind"], item["name"], item.get("subtype")): item
        for item in objects
    }

    table = by_identity[("table", "Companies", None)]
    assert "<xsd:schema" in table["content"]["source_text"]
    assert table["content"]["representation"] == "xml_schema"

    query = by_identity[("query", "qryCompanies", None)]
    assert 'Name ="Companies"' in query["content"]["source_text"]
    assert query["content"]["representation"] == "access_text_definition"

    form = by_identity[("form", "frmAbout", None)]
    assert "Begin Form" in form["content"]["source_text"]

    data_macro = by_identity[("macro", "Companies", "data_macro")]
    assert "<DataMacros" in data_macro["content"]["source_text"]
    assert data_macro["content"]["representation"] == "access_data_macro_xml"

    module = by_identity[("module", "modDAO", None)]
    assert "GetRandomPkValue" in module["content"]["source_text"]
    assert module["content"]["representation"] == "vba_source"


def test_result_lists_unprocessed_features_instead_of_hiding_them(
    northwind_dev_accdt: Path,
) -> None:
    result = translate_access_file(northwind_dev_accdt)
    features = {item["feature"]: item for item in result["unprocessed_features"]}

    assert features["table_data"]["status"] == "not_started"
    # Relationships are translated now, so they are no longer listed here.
    # Their absence from this list is exactly what "we did the work" looks
    # like, and the positive assertion lives with the result itself.
    assert "relationships" not in features
    assert result["semantics"]["relationships"]
    assert features["object_properties"]["status"] == "not_started"
    assert features["navigation_pane"]["status"] == "not_started"
    assert features["target_generation"]["status"] == "not_started"
    assert all(item["reason_code"] for item in features.values())
    assert result["package_summary"]["unprocessed_entries"] > 0


def test_translation_is_deterministic_and_json_serializable(
    northwind_starter_accdt: Path,
) -> None:
    first = translate_access_file(northwind_starter_accdt)
    second = translate_access_file(northwind_starter_accdt)
    assert first == second

    artifact = _artifact(first)
    assert artifact["declared_counts"] == {
        "table": 9,
        "query": 30,
        "form": 30,
        "report": 7,
        "macro": 4,
        "module": 8,
    }
    assert first["completion_summary"] == {
        "object_definitions": 88,
        "raw_extraction_complete": 88,
        "raw_extraction_failed": 0,
        "raw_extraction_completion_percentage": 100.0,
        "semantic_translation_complete": 46,
        "semantic_translation_partial": 42,
        "semantic_translation_completion_percentage": 52.27,
        "semantic_objects_complete": 46,
        "semantic_objects_partial": 42,
        "semantic_objects_failed": 0,
        "relationships_translated": 5,
    }

    encoded = json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert json.loads(encoded) == first
    assert str(northwind_starter_accdt.resolve()) not in encoded


def test_real_input_is_never_modified(
    northwind_dev_accdt: Path,
) -> None:
    before_bytes = northwind_dev_accdt.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    before_stat = northwind_dev_accdt.stat()
    before_listing = sorted(path.name for path in northwind_dev_accdt.parent.iterdir())

    translate_access_file(northwind_dev_accdt)

    after_stat = northwind_dev_accdt.stat()
    assert hashlib.sha256(northwind_dev_accdt.read_bytes()).hexdigest() == before_hash
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert sorted(path.name for path in northwind_dev_accdt.parent.iterdir()) == before_listing
