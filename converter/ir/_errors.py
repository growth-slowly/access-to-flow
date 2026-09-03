"""Error types for the canonical Access intermediate representation."""

from __future__ import annotations

__all__ = ["AccessIRValidationError"]


class AccessIRValidationError(Exception):
    """Raised when inventory input cannot be turned into valid Access IR.

    Deliberately *not* an :class:`OSError` subclass, so that a genuinely
    missing corpus root keeps surfacing as :class:`FileNotFoundError` instead
    of being swallowed by domain validation handling.

    No partially built corpus is ever returned alongside this error: the first
    invalid inventory aborts the whole load.
    """
