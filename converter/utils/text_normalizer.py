"""Deterministic text normalization helpers.

This module is pure standard library and works completely offline.
"""

import re

# Runs of Unicode whitespace and/or ASCII hyphen-minus become one underscore.
_SEPARATOR_RUN = re.compile(r"[\s-]+")
# Everything outside the ASCII identifier alphabet is removed. This must run
# BEFORE lowercasing so that Unicode lowercasing cannot manufacture ASCII
# letters (e.g. U+0130 'İ' -> 'i', U+212A KELVIN SIGN -> 'k').
_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_]")
# Collapse repeated underscores to a single underscore.
_UNDERSCORE_RUN = re.compile(r"_+")


def normalize_identifier(text: str) -> str:
    """Normalize *text* into a lowercase ASCII underscore identifier.

    Pipeline (order is significant):

    1. Reject non-``str`` input with ``TypeError`` (``str`` subclasses pass).
    2. Strip leading/trailing whitespace.
    3. Replace each run of whitespace or hyphens with one underscore.
    4. Remove every character other than ASCII letters, digits, underscore.
    5. Lowercase (the string is pure ASCII at this point).
    6. Collapse repeated underscores.
    7. Trim leading/trailing underscores.

    Returns the empty string when no valid character remains.
    """
    if not isinstance(text, str):
        raise TypeError(
            f"normalize_identifier expects str, got {type(text).__name__}"
        )
    result = text.strip()
    result = _SEPARATOR_RUN.sub("_", result)
    result = _INVALID_CHARS.sub("", result)
    result = result.lower()
    result = _UNDERSCORE_RUN.sub("_", result)
    return result.strip("_")
