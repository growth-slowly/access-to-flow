"""Shared black-box helpers for TASK-ACCESS-002 tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


KINDS = ("table", "query", "form", "report", "macro", "module")
STAGES = ("discovery", "extraction", "translation")
STATUSES = (
    "complete",
    "partial",
    "unsupported",
    "failed",
    "not_started",
    "not_applicable",
)
INCOMPLETE = {"partial", "unsupported", "failed", "not_started"}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def binary_inventory_root(tmp_path: Path) -> Path:
    """A small Jet/ACE-layout corpus, including flags and all status units."""
    inventory = {
        "sample": "z_نمونه_Żółć_Пример_Straße",
        "generated_by": "test metadata only",
        "databases": [
            {
                "file": "B.accdb",
                "size_bytes": 1,
                "format": "ACE 14 (Access 2010 / .accdb)",
                "counts": {
                    "Table": 1,
                    "Table(linked)": 1,
                    "Table(ODBC-linked)": 1,
                    "Query": 1,
                    "Form": 0,
                    "Report": 0,
                    "Macro": 0,
                    "Module": 0,
                    "HiddenQuery(~sq_ form/report recordsource)": 1,
                },
                "objects": [
                    {"name": "عنوان_Żółć_Пример_Straße", "kind": "Table"},
                    {"name": "Linked", "kind": "Table(linked)", "linked_db": "x"},
                    {"name": "Remote", "kind": "Table(ODBC-linked)"},
                    {"name": "Visible", "kind": "Query"},
                ],
            },
            {
                "file": "A.mdb",
                "size_bytes": 1,
                "format": "Jet4 (Access 2000-2003 / .mdb)",
                "counts": {
                    "Table": 0,
                    "Table(linked)": 0,
                    "Table(ODBC-linked)": 0,
                    "Query": 0,
                    "Form": 0,
                    "Report": 0,
                    "Macro": 0,
                    "Module": 0,
                    "HiddenQuery(~sq_ form/report recordsource)": 0,
                },
                "objects": [],
            },
        ],
    }
    write_json(tmp_path / "z" / "object_inventory.json", inventory)
    return tmp_path


@pytest.fixture(scope="session")
def sample_root() -> Path:
    root = Path(__file__).resolve().parents[3] / "samples" / "open_access_systems"
    if not root.is_dir():
        pytest.skip(
            "requires the ignored local samples/open_access_systems corpus "
            "(15 cataloged object_inventory.json files)"
        )
    return root


def artifact_objects(corpus: dict[str, object]):
    for sample in corpus["samples"]:
        for artifact in sample["artifacts"]:
            yield artifact


def all_named_objects(corpus: dict[str, object]):
    for artifact in artifact_objects(corpus):
        yield from artifact["objects"]


def all_count_only(corpus: dict[str, object]):
    for artifact in artifact_objects(corpus):
        yield from artifact["count_only"]


def snapshot_inventory_sources(root: Path) -> dict[str, tuple[int, int, str | None]]:
    """Snapshot metadata bytes and sample-directory mtimes without Access binaries."""
    result: dict[str, tuple[int, int, str | None]] = {}
    metadata_files = sorted(root.glob("*/object_inventory.json"))
    catalog = root / "catalog.csv"
    if catalog.is_file():
        metadata_files.append(catalog)
    for metadata in sorted(metadata_files):
        stat = metadata.stat()
        digest = hashlib.sha256(metadata.read_bytes()).hexdigest()
        result[metadata.relative_to(root).as_posix()] = (
            stat.st_size,
            stat.st_mtime_ns,
            digest,
        )
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        stat = directory.stat()
        result[directory.relative_to(root).as_posix() + "/"] = (
            stat.st_size,
            stat.st_mtime_ns,
            None,
        )
    return result
