"""Frozen vocabulary for the ``access-ir/1`` intermediate representation.

Everything in this module is a *claim vocabulary*: the words here are the only
words the converter is allowed to use when it describes how much of an Access
system it has actually understood.  Adding a value here is a truthfulness
decision, not a formatting one.

Nothing in this module starts Access/COM/PowerShell, executes SQL, or runs VBA
or macros. Inventory import reads ``object_inventory.json`` metadata; the
separate ACCDT adapter may feed inert, validated package definitions into the
same claim vocabulary.
"""

from __future__ import annotations

__all__ = [
    "SCHEMA",
    "KINDS",
    "STAGES",
    "STATUSES",
    "ELIGIBLE_STATUSES",
    "INCOMPLETE_STATUSES",
    "FORMAT_FAMILIES",
    "REASON_COUNT_ONLY",
    "REASON_EXTRACTION_NOT_ATTEMPTED",
    "REASON_TRANSLATION_NOT_ATTEMPTED",
    "KNOWN_UNMODELED_SCOPE",
    "UNIT_WEIGHTING",
    "classify_format_family",
    "bucket_for_count_key",
    "bucket_for_object_kind",
    "bucket_identity",
    "canonical_bucket_order",
    "empty_flags",
    "flags_dict",
]

SCHEMA = "access-ir/1"

#: Canonical Access object kinds, in canonical report order.
KINDS = ("table", "query", "form", "report", "macro", "module")

#: Pipeline stages.  ``discovery`` means "a catalog/inventory record exists",
#: which is a far weaker claim than ``extraction`` (content read out of the
#: artifact) or ``translation`` (semantics carried into a target system).
STAGES = ("discovery", "extraction", "translation")

STATUSES = (
    "complete",
    "partial",
    "unsupported",
    "failed",
    "not_started",
    "not_applicable",
)

#: Everything except ``not_applicable`` counts towards the denominator.
ELIGIBLE_STATUSES = (
    "complete",
    "partial",
    "unsupported",
    "failed",
    "not_started",
)

#: Statuses that must carry a stable, machine-readable reason code.
INCOMPLETE_STATUSES = ("partial", "unsupported", "failed", "not_started")

FORMAT_FAMILIES = ("jet3", "jet4", "ace", "accdt")

REASON_COUNT_ONLY = "COUNT_ONLY_NO_OBJECT_IDENTITY"
REASON_EXTRACTION_NOT_ATTEMPTED = "CONTENT_EXTRACTION_NOT_ATTEMPTED"
REASON_TRANSLATION_NOT_ATTEMPTED = "SEMANTIC_TRANSLATION_NOT_ATTEMPTED"
REASON_SOURCE_PRESERVED = (
    "SOURCE_DEFINITION_PRESERVED_SEMANTICS_NOT_NORMALIZED"
)
REASON_SOURCE_UNAVAILABLE = "SOURCE_DEFINITION_UNAVAILABLE"
REASON_SOURCE_DECODE_FAILED = "SOURCE_DEFINITION_DECODE_FAILED"
REASON_SOURCE_XML_INVALID = "SOURCE_DEFINITION_XML_INVALID"
REASON_SOURCE_XML_UNSAFE = "SOURCE_DEFINITION_XML_UNSAFE"

#: Human-readable gloss for the reason codes this module can emit.  The
#: count-only gloss deliberately says "not captured by this inventory": the
#: objects do have identities inside Access, the source inventory simply did
#: not record them.
REASON_CODE_MEANINGS = {
    REASON_COUNT_ONLY: (
        "declared by an aggregate count only; the source inventory did not "
        "capture per-object identity, so no name may be invented"
    ),
    REASON_EXTRACTION_NOT_ATTEMPTED: (
        "no content was read out of the artifact; only catalog/inventory "
        "metadata was available"
    ),
    REASON_TRANSLATION_NOT_ATTEMPTED: (
        "no semantic translation was attempted; a catalog record is not a "
        "translation"
    ),
    REASON_SOURCE_PRESERVED: (
        "the inert source definition was decoded and preserved, but its Access "
        "semantics have not yet been normalized into a target-neutral model"
    ),
    REASON_SOURCE_UNAVAILABLE: (
        "semantic translation could not start because the source definition "
        "was not extracted successfully"
    ),
    REASON_SOURCE_DECODE_FAILED: (
        "the package definition could not be decoded strictly as UTF-8 or "
        "BOM-marked UTF-16"
    ),
    REASON_SOURCE_XML_INVALID: (
        "the package definition was decoded but was not well-formed XML"
    ),
    REASON_SOURCE_XML_UNSAFE: (
        "the XML definition used a document type or entity declaration that "
        "the offline importer refuses to process"
    ),
}

#: Work that is *not in the denominator at all*.  Publishing this list next to
#: the coverage numbers is the difference between "0.00% of 1,561 catalogued
#: objects" and the false impression of "0% of the conversion work".
KNOWN_UNMODELED_SCOPE = (
    "form and report code-behind VBA modules (HasModule) are not catalogued as "
    "objects and are therefore absent from every denominator",
    "macro and data-macro action bodies are counted per object, never per action",
    "table relationships, indexes, validation rules and default values",
    "control-level event properties and control layout",
    "ribbon XML held in USysRibbons and other USys/MSys system objects",
    "VBA project references, startup properties and navigation-pane metadata",
    "query type (select/action/crosstab/pass-through/DDL/union) is not captured "
    "by the source inventories, so no query subtype is asserted",
    "linked and ODBC tables have no local definition or data; the back ends "
    "they point at may be counted separately as their own artifacts",
)

UNIT_WEIGHTING = (
    "one named inventory record counts as one unit; a count-only aggregate "
    "counts as its full declared count, so objects without captured identity "
    "stay visible in every denominator"
)


def empty_flags() -> "dict[str, bool]":
    """Return the canonical, always-present flag set (all false)."""
    return {"linked": False, "odbc_linked": False, "hidden": False}


def flags_dict(*set_flags: str) -> "dict[str, bool]":
    flags = empty_flags()
    for name in set_flags:
        flags[name] = True
    return flags


# --------------------------------------------------------------------------
# Format families
# --------------------------------------------------------------------------

# The first match wins.  ACE 12/14/16 are one on-disk generation (.accdb) and
# collapse to ``ace``; Jet 3.x and Jet 4.0 genuinely differ (ANSI code-page vs
# UCS-2 object names) and stay separate.  Note that "access" does not contain
# the substring "ace", so an ``ace`` match is never triggered by the word
# "Access" alone.
_FORMAT_RULES = (
    ("accdt", "accdt"),
    ("jet3", "jet3"),
    ("jet 3", "jet3"),
    ("jet4", "jet4"),
    ("jet 4", "jet4"),
    ("ace", "ace"),
)


def classify_format_family(description: str) -> "str | None":
    """Map an original format description to a family, or ``None`` if unknown.

    The original description is never discarded by the caller; this is a pure
    classification helper.
    """
    lowered = description.lower()
    for needle, family in _FORMAT_RULES:
        if needle in lowered:
            return family
    return None


# --------------------------------------------------------------------------
# Object buckets
# --------------------------------------------------------------------------
#
# A "bucket" is the (kind, subtype, flags, derived_from_kind) tuple that both a
# declared count key and a named object record can map onto.  Declared totals
# and named records are reconciled bucket by bucket, which is what makes the
# count-only residual meaningful.

_BUCKETS = {
    # kind, subtype, flags, derived_from_kind
    "table": ("table", None, (), None),
    # Linked/ODBC tables keep kind "table" (they are TableDefs) but carry the
    # link flag, because they hold no local definition or data.
    "table_linked": ("table", None, ("linked",), None),
    "table_odbc": ("table", None, ("odbc_linked",), None),
    "query": ("query", None, (), None),
    # Access materialises ~sq_ queries from a form/report RecordSource or a
    # control RowSource.  They are real saved QueryDefs, but they are derived
    # from a UI object; recording that prevents later double counting when
    # form/report translation lands.
    "query_hidden": ("query", "hidden_query", ("hidden",), "form_or_report"),
    "form": ("form", None, (), None),
    "report": ("report", None, (), None),
    "macro": ("macro", None, (), None),
    # Project convention, not Access semantics: an .axl data macro is a
    # table-scoped event-handler bundle, not a Navigation Pane macro object,
    # and one .axl file may hold several handlers.
    "macro_data": ("macro", "data_macro", (), "table"),
    "module": ("module", None, (), None),
}

#: Deterministic emission order for count-only aggregates.
_BUCKET_ORDER = (
    "table",
    "table_linked",
    "table_odbc",
    "query",
    "query_hidden",
    "form",
    "report",
    "macro",
    "macro_data",
    "module",
)

_COUNT_KEY_EXACT = {
    "table": "table",
    "table(linked)": "table_linked",
    "linkedtable": "table_linked",
    "table(odbc-linked)": "table_odbc",
    "table(odbclinked)": "table_odbc",
    "query": "query",
    "form": "form",
    "report": "report",
    "macro": "macro",
    "module": "module",
}


def canonical_bucket_order() -> "tuple[str, ...]":
    return _BUCKET_ORDER


def bucket_for_count_key(key: str) -> "str | None":
    """Return the internal bucket id for a declared-count key, else ``None``."""
    normalized = "".join(key.lower().split())
    bucket = _COUNT_KEY_EXACT.get(normalized)
    if bucket is not None:
        return bucket
    if normalized.startswith("hiddenquery"):
        return "query_hidden"
    if normalized.startswith("datamacro"):
        return "macro_data"
    return None


def bucket_for_object_kind(kind: str) -> "str | None":
    """Return the internal bucket id for a named object's kind, else ``None``."""
    return bucket_for_count_key(kind)


def bucket_identity(bucket: str) -> "tuple[str, str | None, tuple[str, ...], str | None]":
    """Return ``(kind, subtype, flag_names, derived_from_kind)``."""
    return _BUCKETS[bucket]
