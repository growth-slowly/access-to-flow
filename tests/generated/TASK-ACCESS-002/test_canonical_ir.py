"""Black-box canonical-IR and corpus acceptance tests for TASK-ACCESS-002."""

from __future__ import annotations

import json
from pathlib import Path

from converter.ir import load_inventory_corpus

from conftest import (
    INCOMPLETE,
    KINDS,
    STAGES,
    STATUSES,
    all_count_only,
    all_named_objects,
    artifact_objects,
    snapshot_inventory_sources,
)


def test_public_loader_is_deterministic_json_and_preserves_source_order(
    binary_inventory_root: Path,
) -> None:
    first = load_inventory_corpus(binary_inventory_root)
    second = load_inventory_corpus(binary_inventory_root)

    assert first == second
    assert first["schema"] == "access-ir/1"
    assert json.loads(json.dumps(first, ensure_ascii=False)) == first

    sample = first["samples"][0]
    assert sample["sample_id"] == "z_نمونه_Żółć_Пример_Straße"
    # Artifact ordering is canonical by artifact name, not filesystem or source list order.
    assert [item["artifact_name"] for item in sample["artifacts"]] == ["A.mdb", "B.accdb"]
    b_artifact = sample["artifacts"][1]
    # Within an artifact, source object order must be retained.
    assert [item["name"] for item in b_artifact["objects"]] == [
        "عنوان_Żółć_Пример_Straße",
        "Linked",
        "Remote",
        "Visible",
    ]


def test_artifact_identity_formats_kinds_flags_and_count_only_are_truthful(
    binary_inventory_root: Path,
) -> None:
    corpus = load_inventory_corpus(binary_inventory_root)
    sample = corpus["samples"][0]
    a_artifact, b_artifact = sample["artifacts"]

    assert a_artifact["format_family"] == "jet4"
    assert "Jet4" in a_artifact["format_description"]
    assert b_artifact["format_family"] == "ace"
    assert "ACE 14" in b_artifact["format_description"]

    for artifact in (a_artifact, b_artifact):
        assert artifact["sample_id"] == sample["sample_id"]
        assert artifact["source_identity"]
        assert set(artifact["declared_counts"]) == set(KINDS)

    by_name = {item["name"]: item for item in b_artifact["objects"]}
    assert by_name["Linked"]["kind"] == "table"
    assert by_name["Linked"]["flags"]["linked"] is True
    assert by_name["Remote"]["kind"] == "table"
    assert by_name["Remote"]["flags"]["odbc_linked"] is True
    assert by_name["Visible"]["kind"] == "query"

    assert sum(item["count"] for item in b_artifact["count_only"]) == 1
    aggregate = b_artifact["count_only"][0]
    assert aggregate["kind"] == "query"
    assert aggregate["subtype"] == "hidden_query"
    assert aggregate["reason_code"] == "COUNT_ONLY_NO_OBJECT_IDENTITY"
    assert "name" not in aggregate, "count-only records must not invent identities"


def test_every_work_unit_has_valid_truthful_stage_statuses(
    binary_inventory_root: Path,
) -> None:
    corpus = load_inventory_corpus(binary_inventory_root)
    units = [*all_named_objects(corpus), *all_count_only(corpus)]
    assert units

    for unit in units:
        assert set(unit["stages"]) == set(STAGES)
        for stage_name, stage in unit["stages"].items():
            assert stage["status"] in STATUSES
            if stage["status"] in INCOMPLETE:
                assert isinstance(stage.get("reason_code"), str) and stage["reason_code"]
            if stage["status"] in {"complete", "not_applicable"}:
                assert not stage.get("reason_code")
        assert unit["stages"]["discovery"]["status"] == "complete"
        assert unit["stages"]["extraction"]["status"] in {
            "not_started",
            "unsupported",
        }
        assert unit["stages"]["translation"]["status"] in {
            "not_started",
            "unsupported",
        }


def test_accdt_layout_and_data_macros_are_normalized(sample_root: Path) -> None:
    corpus = load_inventory_corpus(sample_root)
    sample = next(
        item for item in corpus["samples"] if item["sample_id"] == "northwind2_dev_edition"
    )
    assert len(sample["artifacts"]) == 1
    artifact = sample["artifacts"][0]
    assert artifact["artifact_name"] == "tf22238896_win32.accdt"
    assert artifact["source_identity"]
    assert artifact["format_family"] == "accdt"
    assert "ACE" in artifact["format_description"]
    assert ".accdt" in artifact["format_description"]

    data_macros = [
        item
        for item in artifact["objects"]
        if item["kind"] == "macro" and item.get("subtype") == "data_macro"
    ]
    assert len(data_macros) == 20
    assert artifact["declared_counts"]["macro"] == 21
    assert all(item["name"].endswith(".axl") for item in data_macros)


def test_full_local_corpus_acceptance_totals_unicode_and_nonmutation(sample_root: Path) -> None:
    before = snapshot_inventory_sources(sample_root)
    corpus = load_inventory_corpus(sample_root)
    after = snapshot_inventory_sources(sample_root)

    assert after == before, "loading metadata modified catalog/inventories or sample timestamps"
    assert corpus["schema"] == "access-ir/1"
    assert len(corpus["samples"]) == 15
    assert sum(1 for _ in artifact_objects(corpus)) == 21

    artifacts = list(artifact_objects(corpus))
    named = list(all_named_objects(corpus))
    count_only = list(all_count_only(corpus))
    declared_by_kind = {
        kind: sum(artifact["declared_counts"][kind] for artifact in artifacts)
        for kind in KINDS
    }
    discrepancy = {
        "samples": len(corpus["samples"]),
        "artifacts": len(artifacts),
        "declared": sum(declared_by_kind.values()),
        "named": len(named),
        "count_only": sum(item["count"] for item in count_only),
        "kinds": declared_by_kind,
    }
    assert discrepancy == {
        "samples": 15,
        "artifacts": 21,
        "declared": 1561,
        "named": 1249,
        "count_only": 312,
        "kinds": {
            "table": 350,
            "query": 634,
            "form": 309,
            "report": 117,
            "macro": 65,
            "module": 86,
        },
    }, f"catalog/object_inventory corpus totals changed: {discrepancy!r}"

    names = {item["name"] for item in named}
    expected_unicode = {
        "Borrowing استعلام",
        "Варіант_ВільніНомериНаДанийМомент",
        "Narzędzia",
    }
    assert expected_unicode <= names
    round_tripped = json.loads(json.dumps(corpus, ensure_ascii=False))
    assert {item["name"] for item in all_named_objects(round_tripped)} == names
