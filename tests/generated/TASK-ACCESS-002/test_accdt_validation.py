"""Strict validation tests for the top-level ACCDT inventory layout."""

from __future__ import annotations

from pathlib import Path

import pytest

from converter.ir import AccessIRValidationError, load_inventory_corpus

from conftest import write_json


def write_accdt_inventory(
    root: Path,
    *,
    counts: dict[str, object],
    tables: object = (),
) -> None:
    sample = root / "accdt-case"
    write_json(
        sample / "object_inventory.json",
        {
            "sample": "accdt-case",
            "counts": counts,
            "tables": list(tables) if isinstance(tables, tuple) else tables,
            "queries": [],
            "forms": [],
            "reports": [],
            "macros": [],
            "data_macros": [],
            "modules": [],
        },
    )
    artifact = sample / "original" / "Case.accdt"
    artifact.parent.mkdir(parents=True)
    artifact.touch()


@pytest.mark.parametrize("bad_count", [-1, 1.25, "1", True, None])
def test_accdt_counts_must_be_nonnegative_integers(
    tmp_path: Path, bad_count: object
) -> None:
    write_accdt_inventory(tmp_path, counts={"Table": bad_count}, tables=())

    with pytest.raises(AccessIRValidationError, match="(?i)count|integer|negative"):
        load_inventory_corpus(tmp_path)


@pytest.mark.parametrize(
    ("counts", "tables"),
    [
        ({"Table": 2}, ("OnlyOne",)),
        ({"Table": 0}, ("Unexpected",)),
    ],
)
def test_accdt_declared_counts_must_match_named_lists(
    tmp_path: Path, counts: dict[str, object], tables: tuple[str, ...]
) -> None:
    write_accdt_inventory(tmp_path, counts=counts, tables=tables)

    with pytest.raises(AccessIRValidationError, match="(?i)count|declared|inconsistent"):
        load_inventory_corpus(tmp_path)


@pytest.mark.parametrize("bad_tables", [None, "TableName", {"TableName": 1}, [""]])
def test_accdt_object_lists_and_names_are_validated(
    tmp_path: Path, bad_tables: object
) -> None:
    declared = 1 if bad_tables == [""] else 0
    write_accdt_inventory(tmp_path, counts={"Table": declared}, tables=bad_tables)

    with pytest.raises(AccessIRValidationError, match="(?i)table|list|name|identity|shape"):
        load_inventory_corpus(tmp_path)


def test_unknown_declared_count_kind_is_not_silently_discarded(tmp_path: Path) -> None:
    write_json(
        tmp_path / "unknown-count" / "object_inventory.json",
        {
            "sample": "unknown-count",
            "databases": [
                {
                    "file": "case.accdb",
                    "format": "ACE 12",
                    "counts": {"Widget": 3},
                    "objects": [],
                }
            ],
        },
    )

    with pytest.raises(AccessIRValidationError, match="(?i)count|kind|widget|inventory"):
        load_inventory_corpus(tmp_path)
