from __future__ import annotations

from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def northwind_dev_accdt() -> Path:
    candidates = sorted(
        (
            PROJECT_ROOT
            / "samples"
            / "open_access_systems"
            / "northwind2_dev_edition"
            / "original"
        ).glob("*.accdt")
    )
    if not candidates:
        pytest.skip("Northwind Developer Edition sample is not present")
    assert len(candidates) == 1
    return candidates[0]


@pytest.fixture(scope="session")
def northwind_starter_accdt() -> Path:
    candidates = sorted(
        (
            PROJECT_ROOT
            / "samples"
            / "open_access_systems"
            / "northwind2_starter_edition"
            / "original"
        ).glob("*.accdt")
    )
    if not candidates:
        pytest.skip("Northwind Starter Edition sample is not present")
    assert len(candidates) == 1
    return candidates[0]
