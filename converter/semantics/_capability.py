"""Classification of what a migration can and cannot carry across.

A single yes/no verdict per object is not useful to a migration team.  A form
whose layout and data binding translate perfectly but whose button opens
another form with ``DoCmd`` is not "untranslatable"; it is translatable as
data and untranslatable as application behaviour, and those two halves go to
different people.

Every reason code this converter can emit is therefore assigned to exactly one
aspect.  An unassigned code is reported as ``unclassified`` rather than being
quietly folded into a bucket, so a new reason code can never inflate a score.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ASPECTS",
    "ASPECT_LABELS",
    "classify_reason_code",
    "aspect_report",
]

ASPECTS = ("structure", "data_logic", "application_logic")

#: English is the intermediate representation's own language, so that a
#: consumer of the JSON needs no translation table to read it.  Every viewer
#: localises these for its audience; the data never carries prose in one
#: particular human language only.
ASPECT_LABELS = {
    "structure": {
        "label": "structure",
        "description": "whether the definition itself was recovered and modelled",
    },
    "data_logic": {
        "label": "data logic",
        "description": "whether it can be expressed as data processing in the target",
    },
    "application_logic": {
        "label": "application logic",
        "description": "screen behaviour, VBA and macros, which need an application layer",
    },
}

_STRUCTURE = {
    "SEMANTIC_PARSE_FAILED",
    "OBJECT_BLOCK_NOT_FOUND",
    "TABLE_ELEMENT_NOT_FOUND",
    "OBJECT_KIND_NOT_TRANSLATED",
    "SOURCE_TEXT_NOT_RETAINED",
    "SOURCE_DEFINITION_UNAVAILABLE",
    "MACRO_STORED_AS_FLAT_ACTION_LIST",
    "MACRO_AXL_NOT_PARSED",
    "MACRO_ELEMENT_NOT_RECOGNISED",
    "STORED_SQL_NOT_RE_PARSED",
    "QUERY_OPERATION_CODE_NOT_MAPPED",
    "QUERY_OPTION_BITS_NOT_MAPPED",
    "JOIN_FLAG_NOT_MAPPED",
    "JOIN_GRAPH_NOT_RESOLVED",
    "RELATIONSHIP_XML_NOT_PARSED",
    "SEMANTIC_MODEL_INCOMPLETE",
}

_DATA_LOGIC = {
    "JET_FIELD_TYPE_NOT_MAPPED",
    "FIELD_TYPE_HAS_NO_PORTABLE_EQUIVALENT",
    "EXPRESSION_PARSE_FAILED",
    "EXPRESSION_NODE_NOT_TRANSLATED",
    "ACCESS_RUNTIME_FUNCTION",
    "USER_DEFINED_VBA_FUNCTION",
    "UNKNOWN_FUNCTION",
    "FUNCTION_REWRITE_INCOMPLETE",
    "LOGICAL_OPERATOR_NOT_IN_SQL",
    "IMPLICIT_CROSS_JOIN",
    "NON_SELECT_QUERY_SQL_NOT_GENERATED",
    "PASS_THROUGH_QUERY_TARGETS_EXTERNAL_SERVER",
}

_APPLICATION_LOGIC = {
    "ACCESS_OBJECT_MODEL_REFERENCE",
    "OBJECT_METHOD_CALL",
    "EMBEDDED_MACRO_BODY_NOT_IN_TEXT_DEFINITION",
    "CONTROL_TYPE_HAS_NO_PORTABLE_EQUIVALENT",
    "ROW_SOURCE_TYPE_NOT_A_QUERY",
    "MACRO_ACTION_UI_NOT_TRANSLATABLE",
    "MACRO_ACTION_SYSTEM_NOT_TRANSLATABLE",
    "MACRO_ACTION_UNKNOWN_NOT_TRANSLATABLE",
    "VBA_DRIVES_ACCESS_UI",
    "VBA_INTERACTIVE_DIALOG",
    "VBA_STARTS_EXTERNAL_PROCESS",
    "VBA_AUTOMATION_OBJECT",
    "VBA_SENDS_KEYSTROKES",
    "VBA_FILE_SYSTEM_ACCESS",
    "VBA_HOST_ENVIRONMENT",
    "VBA_ACCESS_APPLICATION_OBJECT",
    "VBA_ACCESS_FORM_REFERENCE",
    "VBA_ACCESS_REPORT_REFERENCE",
    "VBA_DECLARES_EXTERNAL_LIBRARY",
}

_ADVISORY = {
    "LIKE_WILDCARD_DIALECT",
    "UI_REFERENCE_BECOMES_PARAMETER",
    "TOP_CLAUSE_IS_DIALECT_SPECIFIC",
    "INT_FIX_ROUNDING",
    "NZ_DEFAULT_DEPENDS_ON_TYPE",
    "INTEGER_DIVISION_SEMANTICS",
}


def classify_reason_code(code: str) -> str:
    """Return the aspect a reason code belongs to."""
    if code in _ADVISORY:
        return "advisory"
    if code in _STRUCTURE:
        return "structure"
    if code in _DATA_LOGIC:
        return "data_logic"
    if code in _APPLICATION_LOGIC:
        return "application_logic"
    return "unclassified"


#: Aspects an object kind is even expected to have.  A table has no
#: application logic of its own, so scoring it against that aspect would
#: manufacture a denominator that does not exist.
_APPLICABLE: dict[str, tuple[str, ...]] = {
    "table": ("structure", "data_logic"),
    "query": ("structure", "data_logic"),
    "form": ("structure", "data_logic", "application_logic"),
    "report": ("structure", "data_logic", "application_logic"),
    "macro": ("structure", "application_logic"),
    "module": ("structure", "application_logic"),
}


def applicable_aspects(kind: str, subtype: str | None = None) -> tuple[str, ...]:
    if kind == "macro" and subtype == "data_macro":
        return ("structure", "data_logic", "application_logic")
    return _APPLICABLE.get(kind, ASPECTS)


def aspect_report(
    kind: str,
    subtype: str | None,
    structure_recovered: bool,
    problems: list[dict[str, Any]],
) -> dict[str, Any]:
    """Split one object's problems into per-aspect verdicts."""
    applicable = applicable_aspects(kind, subtype)
    grouped: dict[str, list[dict[str, Any]]] = {aspect: [] for aspect in ASPECTS}
    advisories: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []
    for problem in problems:
        aspect = classify_reason_code(problem.get("reason_code", ""))
        if aspect == "advisory":
            advisories.append(problem)
        elif aspect == "unclassified":
            unclassified.append(problem)
        else:
            grouped[aspect].append(problem)

    report: dict[str, Any] = {}
    for aspect in ASPECTS:
        if aspect not in applicable:
            report[aspect] = {
                "status": "not_applicable",
                "blockers": [],
                "blocker_count": 0,
            }
            continue
        blockers = grouped[aspect]
        if aspect == "structure" and not structure_recovered:
            status = "failed" if any(
                b["reason_code"] == "SEMANTIC_PARSE_FAILED" for b in blockers
            ) else "partial"
        elif blockers:
            status = "partial"
        else:
            status = "complete"
        report[aspect] = {
            "status": status,
            "blockers": blockers,
            "blocker_count": len(blockers),
        }
    report["advisories"] = advisories
    report["unclassified"] = unclassified
    return report
