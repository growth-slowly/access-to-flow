"""Canonical Access intermediate representation (``access-ir/1``).

This package turns the project's ``object_inventory.json`` metadata or safely
decoded definitions from an offline file adapter into a deterministic,
versioned intermediate representation and reports truthfully on how much of it
has actually been converted.

It is fully offline and uses only the Python standard library. No Access, COM,
PowerShell or other subprocess is started, no SQL is executed, and no VBA or
macro is run. Nothing under ``samples/`` is ever written.

Discovering a catalog entry is *not* a translation. Likewise, preserving raw
ACCDT definition text proves extraction but not semantic conversion. The
coverage report keeps those claims separate with explicit reason codes.

Public API:

* :class:`AccessIRValidationError`
* :func:`load_inventory_corpus`
* :func:`build_coverage_report`
"""

from __future__ import annotations

from ._coverage import build_coverage_report
from ._errors import AccessIRValidationError
from ._inventory import load_inventory_corpus
from ._vocab import (
    FORMAT_FAMILIES,
    KINDS,
    SCHEMA,
    STAGES,
    STATUSES,
)

__all__ = [
    "AccessIRValidationError",
    "load_inventory_corpus",
    "build_coverage_report",
    "SCHEMA",
    "KINDS",
    "STAGES",
    "STATUSES",
    "FORMAT_FAMILIES",
]
