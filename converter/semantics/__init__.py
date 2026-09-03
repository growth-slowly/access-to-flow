"""Semantic translation of extracted Access definitions.

The extraction layer proves that a definition was read.  This layer answers
the harder question: *what does it mean, and how much of that meaning can be
carried into another database system?*

Every object gets one of four verdicts, and every verdict short of
``complete`` names the concrete construct that blocked it:

``complete``     the whole definition was carried into the model.
``partial``      the structure was recovered but some construct cannot move.
``unsupported``  the definition's storage form carries no recoverable model.
``failed``       parsing the definition raised a concrete error.

Nothing in this package opens Access, runs SQL, or executes VBA or macros.
"""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree

from ._capability import ASPECTS, ASPECT_LABELS, aspect_report, classify_reason_code
from ._expr import analyze_expression
from ._macrodef import translate_data_macro, translate_macro
from ._querydef import translate_query
from ._tabledef import translate_relationships, translate_table
from ._textformat import AccessTextError, parse_access_text, split_code_behind
from ._uidef import translate_form
from ._vba import parse_vba_module

__all__ = [
    "translate_objects",
    "ASPECTS",
    "ASPECT_LABELS",
    "ADVISORY_REASON_CODES",
    "SEMANTICS_SCHEMA",
]

SEMANTICS_SCHEMA = "access-semantics/1"

#: Differences the migration team must decide about, but which do not by
#: themselves stop a definition from being carried across.
ADVISORY_REASON_CODES = frozenset(
    {
        "LIKE_WILDCARD_DIALECT",
        "UI_REFERENCE_BECOMES_PARAMETER",
        "TOP_CLAUSE_IS_DIALECT_SPECIFIC",
        "INT_FIX_ROUNDING",
        "NZ_DEFAULT_DEPENDS_ON_TYPE",
        "INTEGER_DIVISION_SEMANTICS",
    }
)

_VBA_PROCEDURE = re.compile(
    r"^\s*(?:(?:Public|Private|Friend|Static)\s+)*"
    r"(?:Sub|Function|Property\s+(?:Get|Let|Set))\s+([A-Za-z_]\w*)",
    re.IGNORECASE | re.MULTILINE,
)


def _known_function_names(objects: list[dict[str, Any]]) -> set[str]:
    """Collect every procedure name this database's own VBA project defines."""
    names: set[str] = set()
    for item in objects:
        text = (item.get("content") or {}).get("source_text")
        if not text:
            continue
        if item.get("kind") == "module":
            names.update(match.group(1) for match in _VBA_PROCEDURE.finditer(text))
        elif item.get("kind") in {"form", "report"}:
            _, code = split_code_behind(text)
            if code:
                names.update(match.group(1) for match in _VBA_PROCEDURE.finditer(code))
    return names


def _verdict(
    translated: bool, problems: list[dict[str, str]]
) -> tuple[str, str | None]:
    blocking = [
        item
        for item in problems
        if item.get("reason_code") not in ADVISORY_REASON_CODES
    ]
    if translated and not blocking:
        return "complete", None
    if blocking:
        return "partial", blocking[0]["reason_code"]
    return "partial", "SEMANTIC_MODEL_INCOMPLETE"


def _object_semantics(
    item: dict[str, Any], known: set[str]
) -> tuple[dict[str, Any] | None, str, str | None, list[dict[str, str]]]:
    kind = item.get("kind")
    subtype = item.get("subtype")
    name = item.get("name") or ""
    content = item.get("content") or {}
    text = content.get("source_text")
    representation = content.get("representation")

    if not text:
        return (
            None,
            "not_started",
            "SOURCE_TEXT_NOT_RETAINED",
            [
                {
                    "reason_code": "SOURCE_TEXT_NOT_RETAINED",
                    "detail": name,
                    "note": "the run was asked to omit raw definition text, so "
                    "no semantics can be derived from it",
                }
            ],
        )

    try:
        if kind == "table":
            model = translate_table(text, name)
        elif kind == "query":
            model = translate_query(parse_access_text(text), name, known)
        elif kind in {"form", "report"}:
            body, code = split_code_behind(text)
            model = translate_form(parse_access_text(body), name, kind, known)
            if code:
                module = parse_vba_module(
                    code, f"{'Form' if kind == 'form' else 'Report'}_{name}",
                    f"{kind}_module",
                )
                model["code_behind"] = module
                for problem in module["unsupported"]:
                    if problem not in model["unsupported"]:
                        model["unsupported"].append(problem)
        elif kind == "macro" and subtype == "data_macro":
            model = translate_data_macro(text, name, known)
        elif kind == "macro":
            model = translate_macro(parse_access_text(text), name, known)
        elif kind == "module":
            model = parse_vba_module(text, name, "module")
        else:
            return (
                None,
                "unsupported",
                "OBJECT_KIND_NOT_TRANSLATED",
                [
                    {
                        "reason_code": "OBJECT_KIND_NOT_TRANSLATED",
                        "detail": str(kind),
                        "note": "no semantic translator exists for this kind",
                    }
                ],
            )
    except (AccessTextError, ElementTree.ParseError, ValueError) as error:
        return (
            None,
            "failed",
            "SEMANTIC_PARSE_FAILED",
            [
                {
                    "reason_code": "SEMANTIC_PARSE_FAILED",
                    "detail": f"{name}: {type(error).__name__}",
                    "note": str(error)[:300],
                }
            ],
        )

    problems = list(model.get("unsupported", []))
    status, reason = _verdict(bool(model.get("translated")), problems)
    model["schema"] = SEMANTICS_SCHEMA
    model["source_representation"] = representation
    return model, status, reason, problems


def _feature_rows(
    objects: list[dict[str, Any]], models: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Summarise, per Access feature, what could and could not be carried."""
    buckets: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(objects):
        model = models.get(index)
        kind = item.get("kind") or "unknown"
        subtype = item.get("subtype")
        feature = f"{kind}:{subtype}" if subtype else kind
        bucket = buckets.setdefault(
            feature,
            {
                "feature": feature,
                "kind": kind,
                "subtype": subtype,
                "total": 0,
                "complete": 0,
                "partial": 0,
                "unsupported": 0,
                "failed": 0,
                "not_started": 0,
                "aspects": {},
                "blocking_reason_codes": {},
            },
        )
        bucket["total"] += 1
        status = item["stages"]["translation"]["status"]
        bucket[status] = bucket.get(status, 0) + 1
        if model is None:
            continue
        for aspect, verdict in (model.get("aspects") or {}).items():
            if aspect not in ASPECTS:
                continue
            counters = bucket["aspects"].setdefault(
                aspect,
                {"complete": 0, "partial": 0, "failed": 0, "not_applicable": 0},
            )
            counters[verdict["status"]] = counters.get(verdict["status"], 0) + 1
        for problem in model.get("unsupported", []):
            code = problem.get("reason_code")
            if code in ADVISORY_REASON_CODES:
                continue
            entry = bucket["blocking_reason_codes"].setdefault(
                code, {"reason_code": code, "objects": 0, "note": problem.get("note", "")}
            )
            entry["objects"] += 1
    rows = []
    for bucket in buckets.values():
        bucket["blocking_reason_codes"] = sorted(
            bucket["blocking_reason_codes"].values(),
            key=lambda row: (-row["objects"], row["reason_code"]),
        )
        bucket["completion_percentage"] = (
            round(bucket["complete"] * 100 / bucket["total"], 2)
            if bucket["total"]
            else 0.0
        )
        rows.append(bucket)
    rows.sort(key=lambda row: row["feature"])
    return rows


def translate_objects(
    objects: list[dict[str, Any]],
    *,
    relationships_xml: str | None = None,
) -> dict[str, Any]:
    """Translate every extracted object and report on the result.

    ``objects`` is mutated in place: each object gains a ``semantics`` model
    and its ``stages.translation`` verdict is replaced with a real one.
    """
    known = _known_function_names(objects)
    models: dict[int, dict[str, Any]] = {}
    problems_by_object: list[dict[str, Any]] = []

    for index, item in enumerate(objects):
        if item["stages"]["extraction"]["status"] != "complete":
            item["stages"]["translation"] = {
                "status": "not_started",
                "reason_code": "SOURCE_DEFINITION_UNAVAILABLE",
            }
            continue
        model, status, reason, problems = _object_semantics(item, known)
        stage: dict[str, Any] = {"status": status}
        if reason is not None:
            stage["reason_code"] = reason
        item["stages"]["translation"] = stage
        if model is not None:
            model["aspects"] = aspect_report(
                item.get("kind") or "",
                item.get("subtype"),
                bool(model.get("translated")) or status == "partial",
                problems,
            )
            models[index] = model
            item["semantics"] = model
        if problems:
            problems_by_object.append(
                {
                    "object": item.get("name"),
                    "kind": item.get("kind"),
                    "subtype": item.get("subtype"),
                    "status": status,
                    "problems": [
                        problem
                        for problem in problems
                        if problem.get("reason_code") not in ADVISORY_REASON_CODES
                    ],
                    "advisories": [
                        problem
                        for problem in problems
                        if problem.get("reason_code") in ADVISORY_REASON_CODES
                    ],
                }
            )

    relationships: list[dict[str, Any]] = []
    relationship_problems: list[dict[str, str]] = []
    if relationships_xml:
        try:
            relationships = translate_relationships(relationships_xml)
        except (ElementTree.ParseError, ValueError) as error:
            relationship_problems.append(
                {
                    "reason_code": "RELATIONSHIP_XML_NOT_PARSED",
                    "detail": type(error).__name__,
                    "note": str(error)[:200],
                }
            )

    features = _feature_rows(objects, models)
    totals = {
        "objects": len(objects),
        "complete": sum(row["complete"] for row in features),
        "partial": sum(row["partial"] for row in features),
        "unsupported": sum(row["unsupported"] for row in features),
        "failed": sum(row["failed"] for row in features),
        "not_started": sum(row["not_started"] for row in features),
    }
    totals["completion_percentage"] = (
        round(totals["complete"] * 100 / totals["objects"], 2)
        if totals["objects"]
        else 0.0
    )
    aspect_totals: dict[str, dict[str, Any]] = {}
    for aspect in ASPECTS:
        counters = {"complete": 0, "partial": 0, "failed": 0, "not_applicable": 0}
        for row in features:
            for key, value in row["aspects"].get(aspect, {}).items():
                counters[key] = counters.get(key, 0) + value
        scored = counters["complete"] + counters["partial"] + counters["failed"]
        aspect_totals[aspect] = {
            **counters,
            "scored_objects": scored,
            "completion_percentage": (
                round(counters["complete"] * 100 / scored, 2) if scored else 0.0
            ),
            "label": ASPECT_LABELS[aspect]["label"],
            "description": ASPECT_LABELS[aspect]["description"],
        }
    reason_index: dict[str, dict[str, Any]] = {}
    for entry in problems_by_object:
        for problem in entry["problems"]:
            code = problem["reason_code"]
            row = reason_index.setdefault(
                code,
                {
                    "reason_code": code,
                    "aspect": classify_reason_code(code),
                    "note": problem.get("note", ""),
                    "objects": 0,
                    "examples": [],
                },
            )
            row["objects"] += 1
            if len(row["examples"]) < 8:
                detail = problem.get("detail", "")
                sample = f"{entry['object']}: {detail}" if detail else entry["object"]
                if sample not in row["examples"]:
                    row["examples"].append(sample)
    return {
        "schema": SEMANTICS_SCHEMA,
        "known_vba_procedures": sorted(known),
        "relationships": relationships,
        "relationship_problems": relationship_problems,
        "features": features,
        "totals": totals,
        "aspect_totals": aspect_totals,
        "reason_codes": sorted(
            reason_index.values(), key=lambda row: (-row["objects"], row["reason_code"])
        ),
        "blocked_objects": problems_by_object,
    }
