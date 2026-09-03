"""Boundary and invalid-input tests for the strict default corpus loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from converter.ir import AccessIRValidationError, load_inventory_corpus

from conftest import write_json


@pytest.mark.parametrize("bad_path", [None, 7, 3.5, b"samples", True, object()])
def test_rejects_non_text_non_pathlike_roots(bad_path: object) -> None:
    with pytest.raises(TypeError):
        load_inventory_corpus(bad_path)  # type: ignore[arg-type]


def test_missing_root_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_inventory_corpus(tmp_path / "does-not-exist")


def test_pathlike_root_is_accepted(binary_inventory_root: Path) -> None:
    result = load_inventory_corpus(binary_inventory_root)
    assert result["samples"]


def test_malformed_json_is_wrapped_as_domain_validation_error(tmp_path: Path) -> None:
    path = tmp_path / "bad" / "object_inventory.json"
    path.parent.mkdir()
    path.write_text('{"sample": ', encoding="utf-8")
    with pytest.raises(AccessIRValidationError, match="(?i)json|inventory|malformed"):
        load_inventory_corpus(tmp_path)


def test_unknown_inventory_shape_is_rejected(tmp_path: Path) -> None:
    write_json(
        tmp_path / "unknown" / "object_inventory.json",
        {"sample": "unknown", "counts": {}, "widgets": []},
    )
    with pytest.raises(AccessIRValidationError, match="(?i)shape|layout|inventory"):
        load_inventory_corpus(tmp_path)


@pytest.mark.parametrize("bad_count", [-1, -999, 1.5, "1", True, None])
def test_invalid_and_negative_declared_counts_are_rejected(
    tmp_path: Path, bad_count: object
) -> None:
    inventory = {
        "sample": "bad-count",
        "databases": [
            {
                "file": "bad.accdb",
                "format": "ACE 12 (.accdb)",
                "counts": {"Table": bad_count},
                "objects": [],
            }
        ],
    }
    write_json(tmp_path / "bad" / "object_inventory.json", inventory)
    with pytest.raises(AccessIRValidationError, match="(?i)count|integer|negative"):
        load_inventory_corpus(tmp_path)


@pytest.mark.parametrize("missing_field", ["counts", "objects"])
def test_binary_inventory_requires_counts_and_objects(
    tmp_path: Path, missing_field: str
) -> None:
    artifact = {
        "file": "empty.accdb",
        "format": "ACE 12 (.accdb)",
        "counts": {},
        "objects": [],
    }
    del artifact[missing_field]
    write_json(
        tmp_path / "missing-field" / "object_inventory.json",
        {"sample": "missing-field", "databases": [artifact]},
    )

    with pytest.raises(AccessIRValidationError, match=missing_field):
        load_inventory_corpus(tmp_path)


def test_binary_inventory_accepts_explicit_consistent_empty_collections(
    tmp_path: Path,
) -> None:
    write_json(
        tmp_path / "empty" / "object_inventory.json",
        {
            "sample": "empty",
            "databases": [
                {
                    "file": "empty.accdb",
                    "format": "ACE 12 (.accdb)",
                    "counts": {},
                    "objects": [],
                }
            ],
        },
    )

    corpus = load_inventory_corpus(tmp_path)
    artifact = corpus["samples"][0]["artifacts"][0]
    assert set(artifact["declared_counts"].values()) == {0}
    assert artifact["objects"] == []
    assert artifact["count_only"] == []


@pytest.mark.parametrize(
    "inventory",
    [
        {
            "databases": [
                {
                    "file": "x.accdb",
                    "format": "ACE 12",
                    "counts": {"Table": 0},
                    "objects": [],
                }
            ]
        },
        {
            "sample": "missing-artifact",
            "databases": [
                {
                    "format": "ACE 12",
                    "counts": {"Table": 0},
                    "objects": [],
                }
            ],
        },
        {
            "sample": "missing-object-name",
            "databases": [
                {
                    "file": "x.accdb",
                    "format": "ACE 12",
                    "counts": {"Table": 1},
                    "objects": [{"kind": "Table"}],
                }
            ],
        },
        {
            "sample": "empty-object-name",
            "databases": [
                {
                    "file": "x.accdb",
                    "format": "ACE 12",
                    "counts": {"Table": 1},
                    "objects": [{"name": "", "kind": "Table"}],
                }
            ],
        },
    ],
)
def test_missing_required_identity_is_rejected(
    tmp_path: Path, inventory: dict[str, object]
) -> None:
    write_json(tmp_path / "bad" / "object_inventory.json", inventory)
    with pytest.raises(AccessIRValidationError, match="(?i)identity|sample|file|name"):
        load_inventory_corpus(tmp_path)


def test_named_records_cannot_exceed_declared_totals(tmp_path: Path) -> None:
    inventory = {
        "sample": "inconsistent",
        "databases": [
            {
                "file": "x.mdb",
                "format": "Jet3 (Access 97)",
                "counts": {"Table": 0},
                "objects": [{"name": "Unexpected", "kind": "Table"}],
            }
        ],
    }
    write_json(tmp_path / "bad" / "object_inventory.json", inventory)
    with pytest.raises(AccessIRValidationError, match="(?i)inconsistent|declared|total|count"):
        load_inventory_corpus(tmp_path)


def test_one_bad_inventory_aborts_the_entire_default_load(tmp_path: Path) -> None:
    good = {
        "sample": "good",
        "databases": [
            {
                "file": "good.accdb",
                "format": "ACE 12",
                "counts": {"Table": 1},
                "objects": [{"name": "T", "kind": "Table"}],
            }
        ],
    }
    write_json(tmp_path / "a-good" / "object_inventory.json", good)
    bad_path = tmp_path / "z-bad" / "object_inventory.json"
    bad_path.parent.mkdir()
    bad_path.write_text("not json", encoding="utf-8")

    with pytest.raises(AccessIRValidationError):
        load_inventory_corpus(tmp_path)


def test_empty_existing_root_is_an_unknown_corpus_not_a_silent_success(tmp_path: Path) -> None:
    with pytest.raises(AccessIRValidationError, match="(?i)inventory|empty|corpus"):
        load_inventory_corpus(tmp_path)
