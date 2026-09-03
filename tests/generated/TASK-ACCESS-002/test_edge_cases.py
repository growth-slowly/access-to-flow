"""Focused boundary/regression tests for canonical Access IR behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from converter.ir import AccessIRValidationError, build_coverage_report, load_inventory_corpus

from conftest import write_json


def _binary_inventory(*, format_description: str, suffix: str = "accdb") -> dict[str, object]:
    return {
        "sample": "format-case",
        "databases": [
            {
                "file": f"case.{suffix}",
                "format": format_description,
                "counts": {"Table": 0},
                "objects": [],
            }
        ],
    }


@pytest.mark.parametrize(
    ("description", "suffix", "family"),
    [
        ("Jet3 (Access 97 / .mdb)", "mdb", "jet3"),
        ("Jet4 (Access 2000-2003 / .mdb)", "mdb", "jet4"),
        ("ACE 12 (Access 2007 / .accdb)", "accdb", "ace"),
    ],
)
def test_binary_format_families_are_normalized_without_losing_description(
    tmp_path: Path, description: str, suffix: str, family: str
) -> None:
    write_json(
        tmp_path / "format-case" / "object_inventory.json",
        _binary_inventory(format_description=description, suffix=suffix),
    )

    artifact = load_inventory_corpus(tmp_path)["samples"][0]["artifacts"][0]
    assert artifact["format_family"] == family
    assert artifact["format_description"] == description


def test_count_only_aggregate_is_weighted_as_every_declared_object_in_coverage(
    tmp_path: Path,
) -> None:
    inventory = {
        "sample": "aggregate-weight",
        "databases": [
            {
                "file": "aggregate.accdb",
                "format": "ACE 12",
                "counts": {"Table": 5},
                "objects": [{"name": "OnlyKnownName", "kind": "Table"}],
            }
        ],
    }
    write_json(tmp_path / "aggregate-weight" / "object_inventory.json", inventory)
    corpus = load_inventory_corpus(tmp_path)
    artifact = corpus["samples"][0]["artifacts"][0]

    assert len(artifact["objects"]) == 1
    assert len(artifact["count_only"]) == 1
    aggregate = artifact["count_only"][0]
    assert aggregate["count"] == 4
    assert aggregate["reason_code"] == "COUNT_ONLY_NO_OBJECT_IDENTITY"

    artifact["objects"][0]["stages"]["translation"] = {"status": "complete"}
    aggregate["stages"]["translation"] = {
        "status": "failed",
        "reason_code": "TEST_AGGREGATE_FAILURE",
    }
    metric = build_coverage_report(corpus)["corpus"]["stages"]["translation"]

    assert metric["eligible"] == 5
    assert metric["counts"]["complete"] == 1
    assert metric["counts"]["failed"] == 4
    assert metric["completion_percentage"] == 20.0
    assert metric["reason_codes"]["TEST_AGGREGATE_FAILURE"] == 4


@pytest.mark.parametrize(
    "artifact_update",
    [
        {"format": None},
        {"format": ""},
    ],
)
def test_missing_or_empty_artifact_format_is_rejected(
    tmp_path: Path, artifact_update: dict[str, object]
) -> None:
    inventory = _binary_inventory(format_description="ACE 12")
    inventory["databases"][0].update(artifact_update)  # type: ignore[index,union-attr]
    write_json(tmp_path / "bad-format" / "object_inventory.json", inventory)

    with pytest.raises(AccessIRValidationError, match="(?i)format|identity"):
        load_inventory_corpus(tmp_path)


@pytest.mark.parametrize("bad_kind", [None, "", "Widget", 7])
def test_missing_or_unknown_named_object_kind_is_rejected(
    tmp_path: Path, bad_kind: object
) -> None:
    inventory = {
        "sample": "bad-kind",
        "databases": [
            {
                "file": "bad.accdb",
                "format": "ACE 12",
                "counts": {"Table": 1},
                "objects": [{"name": "ObjectWithBadKind", "kind": bad_kind}],
            }
        ],
    }
    write_json(tmp_path / "bad-kind" / "object_inventory.json", inventory)

    with pytest.raises(AccessIRValidationError, match="(?i)kind|object|inventory"):
        load_inventory_corpus(tmp_path)
