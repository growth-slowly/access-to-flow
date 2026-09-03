"""Shared helpers for the semantic-translation test suite.

These tests are written with :mod:`unittest` rather than pytest fixtures so
that they run under either runner, including on a machine with no third-party
packages installed at all - which is the same constraint the product itself
has to satisfy.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SAMPLES = PROJECT_ROOT / "samples" / "open_access_systems"


def sample_file(sample: str, pattern: str) -> Path | None:
    candidates = sorted((SAMPLES / sample / "original").glob(pattern))
    return candidates[0] if candidates else None


NORTHWIND_DEV = sample_file("northwind2_dev_edition", "*.accdt")
NORTHWIND_STARTER = sample_file("northwind2_starter_edition", "*.accdt")
SPORTS_ACCDB = sample_file("sports_admin", "Sports.accdb")


@functools.lru_cache(maxsize=4)
def translated(path: Path) -> dict[str, Any]:
    from converter.access import translate_access_file

    return translate_access_file(path)


def objects_of(result: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for sample in (result.get("ir") or {}).get("samples", []):
        for artifact in sample.get("artifacts", []):
            found.extend(artifact.get("objects", []))
    return found


def find_object(result: dict[str, Any], kind: str, name: str) -> dict[str, Any] | None:
    for item in objects_of(result):
        if item["kind"] == kind and item["name"] == name:
            return item
    return None
