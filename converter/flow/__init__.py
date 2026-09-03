"""Build a navigable flow model from the translated intermediate representation.

Two kinds of flow matter to someone reading an Access system for the first
time:

*System flow* - which screen reads which query, which query reads which table,
which button calls which procedure, and which procedure opens which screen.
That graph is what tells a migration team where the work actually is.

*Procedure flow* - the control-flow graph inside one VBA procedure, one macro
or one data-macro handler.

This module derives both from the IR alone.  It never reopens the Access file.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["build_flow_model", "FLOW_SCHEMA"]

FLOW_SCHEMA = "access-flow/1"

#: DoCmd actions whose first string argument names another Access object.
_DOCMD_TARGETS = {
    "openform": "form",
    "openreport": "report",
    "openquery": "query",
    "opentable": "table",
    "close": None,
    "runmacro": "macro",
    "browseto": "form",
}

_DOCMD_CALL = re.compile(
    r"DoCmd\s*\.\s*(\w+)\s*[( ]\s*(?:acForm|acReport|acQuery|acTable)?\s*,?\s*"
    r'"((?:[^"]|"")*)"',
    re.IGNORECASE,
)
_OPEN_ARGS = re.compile(r'"((?:[^"]|"")*)"')


def _walk(statements: list[dict[str, Any]]):
    for statement in statements or []:
        yield statement
        if isinstance(statement.get("statements"), list):
            yield from _walk(statement["statements"])
        for branch in statement.get("branches", []) or []:
            yield from _walk(branch.get("statements", []))
        for case in statement.get("cases", []) or []:
            yield from _walk(case.get("statements", []))


def _procedure_id(owner: str, name: str) -> str:
    return f"proc::{owner}::{name}"


def _object_id(kind: str, name: str) -> str:
    return f"{kind}::{name}"


def _aspect_status(model: dict[str, Any] | None, aspect: str) -> str:
    if not model:
        return "not_started"
    return (model.get("aspects") or {}).get(aspect, {}).get("status", "not_started")


def _collect_objects(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalogue: list[dict[str, Any]] = []
    for item in objects:
        model = item.get("semantics")
        kind = item.get("kind") or "unknown"
        entry = {
            "id": _object_id(kind, item.get("name") or ""),
            "name": item.get("name"),
            "kind": kind,
            "subtype": item.get("subtype"),
            "status": item["stages"]["translation"]["status"],
            "reason_code": item["stages"]["translation"].get("reason_code"),
            "aspects": {
                aspect: _aspect_status(model, aspect)
                for aspect in ("structure", "data_logic", "application_logic")
            },
            "blockers": [
                problem
                for aspect in ("structure", "data_logic", "application_logic")
                for problem in (model.get("aspects") or {})
                .get(aspect, {})
                .get("blockers", [])
            ]
            if model
            else [],
            "advisories": (model.get("aspects") or {}).get("advisories", [])
            if model
            else [],
        }
        if model:
            entry["summary"] = _summarise(kind, item.get("subtype"), model)
        catalogue.append(entry)
    catalogue.sort(key=lambda row: (row["kind"], (row["name"] or "").casefold()))
    return catalogue


def _summarise(kind: str, subtype: str | None, model: dict[str, Any]) -> dict[str, Any]:
    if kind == "table":
        return {
            "columns": len(model.get("columns", [])),
            "primary_key": model.get("primary_key", []),
            "indexes": len(model.get("indexes", [])),
        }
    if kind == "query":
        return {
            "query_type": model.get("query_type"),
            "tables": len(model.get("from", [])),
            "joins": len(model.get("joins", [])),
            "columns": len(model.get("columns", [])),
            "has_sql": bool(model.get("sql")),
        }
    if kind in {"form", "report"}:
        code = model.get("code_behind") or {}
        return {
            "record_source": model.get("record_source"),
            "controls": model.get("control_count", 0),
            "events": len(model.get("events", [])),
            "procedures": len(code.get("procedures", [])),
        }
    if kind == "macro" and subtype == "data_macro":
        return {
            "handlers": [h["event"] for h in model.get("handlers", [])],
        }
    if kind == "macro":
        return {"representation": model.get("representation"), "actions": model.get("actions", {})}
    if kind == "module":
        return {
            "procedures": len(model.get("procedures", [])),
            "public_procedures": len(
                [p for p in model.get("procedures", []) if p["scope"] == "public"]
            ),
        }
    return {}


def _docmd_targets(statements: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Return ``(action, target_kind, target_name)`` for object-opening calls."""
    found: list[tuple[str, str, str]] = []
    for statement in _walk(statements):
        source = statement.get("source") or ""
        for match in re.finditer(r"DoCmd\s*\.\s*(\w+)", source, re.IGNORECASE):
            action = match.group(1)
            kind = _DOCMD_TARGETS.get(action.casefold())
            if kind is None:
                continue
            tail = source[match.end() :]
            literal = _OPEN_ARGS.search(tail)
            if literal is None:
                continue
            found.append((action, kind, literal.group(1)))
    return found


def _procedures(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    procedures: list[dict[str, Any]] = []
    for item in objects:
        model = item.get("semantics") or {}
        kind = item.get("kind")
        if kind == "module":
            owner = item.get("name") or ""
            owner_kind = "module"
            module = model
        elif kind in {"form", "report"} and model.get("code_behind"):
            owner = item.get("name") or ""
            owner_kind = kind
            module = model["code_behind"]
        else:
            continue
        for procedure in module.get("procedures", []):
            procedures.append(
                {
                    "id": _procedure_id(owner, procedure["name"]),
                    "owner": owner,
                    "owner_kind": owner_kind,
                    "name": procedure["name"],
                    "kind": procedure["kind"],
                    "scope": procedure["scope"],
                    "parameters": procedure["parameters"],
                    "returns": procedure.get("returns"),
                    "line": procedure.get("line"),
                    "metrics": procedure.get("metrics", {}),
                    "effects": procedure.get("effects", {}),
                    "flow": procedure.get("flow", {"nodes": [], "edges": []}),
                    "opens": _docmd_targets(procedure.get("statements", [])),
                }
            )
    return procedures


def _macro_flow(model: dict[str, Any], title: dict[str, Any]) -> dict[str, Any]:
    """Render a macro or data-macro statement tree as a flow graph."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    counter = {"value": 0}

    def new_node(kind: str, label: str, **extra: Any) -> str:
        counter["value"] += 1
        identifier = f"m{counter['value']}"
        nodes.append({"id": identifier, "kind": kind, "label": label, **extra})
        return identifier

    def link(source: str | None, target: str, label_key: str = "") -> None:
        if source is None:
            return
        edge: dict[str, Any] = {"from": source, "to": target}
        if label_key:
            edge["label_key"] = label_key
        if edge not in edges:
            edges.append(edge)

    def emit(statements: list[dict[str, Any]], previous: str | None) -> str | None:
        current = previous
        for statement in statements:
            kind = statement.get("type")
            if kind == "comment":
                continue
            if kind == "action":
                label = statement["action"]
                arguments = statement.get("arguments") or {}
                if arguments:
                    label += "(" + ", ".join(
                        f"{key}={value['source']}" for key, value in arguments.items()
                    ) + ")"
                elif statement.get("positional_arguments"):
                    values = [
                        argument["source"]
                        for argument in statement["positional_arguments"]
                        if argument["source"]
                    ]
                    if values:
                        label += "(" + ", ".join(values) + ")"
                node_kind = {
                    "data": "data",
                    "control": "process",
                    "ui": "ui",
                    "system": "ui",
                }.get(statement.get("category", ""), "process")
                node = new_node(
                    node_kind,
                    _shorten(label),
                    category=statement.get("category"),
                    translated=statement.get("translated", False),
                )
                condition = statement.get("condition")
                if condition:
                    decision = new_node("decision", _shorten(condition["source"]))
                    link(current, decision)
                    link(decision, node, "yes")
                    merge = new_node("merge", "")
                    link(node, merge)
                    link(decision, merge, "no")
                    current = merge
                else:
                    link(current, node)
                    current = node
                continue
            if kind == "conditional":
                merge = new_node("merge", "")
                fallthrough = current
                for branch in statement.get("branches", []):
                    condition = branch.get("condition")
                    if branch["branch"] in {"if", "elseif"} and condition:
                        decision = new_node("decision", _shorten(condition["source"]))
                        link(fallthrough, decision)
                        before = len(nodes)
                        tail = emit(branch["statements"], None)
                        if len(nodes) > before:
                            link(decision, nodes[before]["id"], "yes")
                            link(tail, merge)
                        else:
                            link(decision, merge, "yes")
                        fallthrough = decision
                    else:
                        before = len(nodes)
                        tail = emit(branch["statements"], None)
                        if len(nodes) > before:
                            link(fallthrough, nodes[before]["id"], "no")
                            link(tail, merge)
                            fallthrough = None
                if fallthrough is not None:
                    link(fallthrough, merge, "no")
                current = merge
                continue
            if kind == "block":
                scope = statement.get("scope") or {}
                detail = ", ".join(f"{k}={v}" for k, v in scope.items() if v)
                node = new_node(
                    "loop" if statement["block"] == "ForEachRecord" else "data",
                    _shorten(statement["block"] + (f" [{detail}]" if detail else "")),
                )
                link(current, node)
                before = len(nodes)
                tail = emit(statement.get("statements", []), None)
                if len(nodes) > before:
                    link(node, nodes[before]["id"], "run")
                    exit_node = new_node("merge", "")
                    link(tail, exit_node)
                    current = exit_node
                else:
                    current = node
                continue
            node = new_node("process", _shorten(str(statement.get("element") or kind)))
            link(current, node)
            current = node
        return current

    start = new_node("start", title["label"], **title.get("extra", {}))
    end = new_node("end", "", text_key="end")
    tail = emit(model, start)
    link(tail, end)
    return {"nodes": nodes, "edges": edges, "start": start, "end": end}


def _shorten(text: str, limit: int = 90) -> str:
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def build_flow_model(result: dict[str, Any]) -> dict[str, Any]:
    """Build the complete flow model for one translated Access artifact."""
    ir = result.get("ir") or {}
    samples = ir.get("samples") or []
    objects: list[dict[str, Any]] = []
    for sample in samples:
        for artifact in sample.get("artifacts", []):
            objects.extend(artifact.get("objects", []))

    catalogue = _collect_objects(objects)
    procedures = _procedures(objects)
    by_name = {
        (entry["kind"], (entry["name"] or "").casefold()): entry["id"]
        for entry in catalogue
    }
    procedure_index: dict[str, list[dict[str, Any]]] = {}
    for procedure in procedures:
        procedure_index.setdefault(procedure["name"].casefold(), []).append(procedure)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()

    def add_edge(source: str, target: str, kind: str, label: str = "") -> None:
        key = (source, target, kind)
        if key in seen_edges or source == target:
            return
        seen_edges.add(key)
        edges.append({"from": source, "to": target, "kind": kind, "label": label})

    for entry in catalogue:
        nodes.append(
            {
                "id": entry["id"],
                "label": entry["name"],
                "kind": entry["kind"],
                "subtype": entry.get("subtype"),
                "status": entry["status"],
                "aspects": entry["aspects"],
            }
        )
    for procedure in procedures:
        nodes.append(
            {
                "id": procedure["id"],
                "label": f"{procedure['owner']}.{procedure['name']}",
                "kind": "procedure",
                "owner": procedure["owner"],
                "status": "complete"
                if not procedure["effects"].get("external_effects")
                else "partial",
                "aspects": {},
            }
        )

    def resolve(kind_hint: str | None, name: str) -> str | None:
        folded = (name or "").casefold()
        order = (
            [kind_hint] if kind_hint else ["query", "table", "form", "report", "macro"]
        )
        for kind in order + ["query", "table", "form", "report", "macro"]:
            identifier = by_name.get((kind, folded))
            if identifier:
                return identifier
        return None

    for item in objects:
        model = item.get("semantics")
        if not model:
            continue
        kind = item.get("kind")
        name = item.get("name") or ""
        source_id = _object_id(kind or "", name)
        if kind == "query":
            for dependency in model.get("dependencies", []):
                target = resolve(None, dependency)
                if target:
                    add_edge(source_id, target, "reads", "参照")
        elif kind in {"form", "report"}:
            for dependency in model.get("dependencies", []):
                target = resolve(None, dependency)
                if target:
                    add_edge(source_id, target, "reads", "データ源")
            for control in model.get("controls", []):
                for binding in control.get("bindings", []):
                    if binding["kind"] == "subform" and binding.get("source"):
                        # Access writes a subform source as "Form.sfrmX" or
                        # "Report.rptX"; the prefix is the container type, not
                        # part of the object's name.
                        raw = re.sub(
                            r"^(Form|Report)\.", "", binding["source"], flags=re.I
                        )
                        target = resolve("form", raw) or resolve("report", raw)
                        if target:
                            add_edge(source_id, target, "embeds", "サブフォーム")
            for event in model.get("events", []):
                if event.get("handler_kind") == "vba_event_procedure":
                    for procedure in procedure_index.get(
                        event["handler"].casefold(), []
                    ):
                        if procedure["owner"].casefold() == name.casefold():
                            add_edge(
                                source_id, procedure["id"], "handles", event["event"]
                            )
                elif event.get("handler_kind") == "macro_object" and event.get("handler"):
                    target = resolve("macro", event["handler"])
                    if target:
                        add_edge(source_id, target, "handles", event["event"])
        elif kind == "macro" and item.get("subtype") == "data_macro":
            target = resolve("table", name)
            if target:
                add_edge(source_id, target, "trigger", "データマクロ")
        elif kind == "macro":
            for statement in _walk(model.get("statements", [])):
                if statement.get("type") != "action":
                    continue
                arguments = statement.get("arguments") or {}
                for key in ("FormName", "ReportName", "QueryName", "TableName", "MacroName"):
                    if key in arguments:
                        hint = key.replace("Name", "").casefold()
                        target = resolve(hint, arguments[key]["source"])
                        if target:
                            add_edge(source_id, target, "opens", statement["action"])
                if statement["action"].casefold() == "runcode":
                    argument = arguments.get("FunctionName")
                    if argument:
                        called = re.sub(r"\(.*\)$", "", argument["source"]).strip()
                        for procedure in procedure_index.get(called.casefold(), []):
                            add_edge(source_id, procedure["id"], "calls", "RunCode")

    for procedure in procedures:
        owner_kind = procedure["owner_kind"]
        add_edge(
            _object_id(owner_kind, procedure["owner"]),
            procedure["id"],
            "contains",
            "",
        )
        for called in procedure["effects"].get("calls", []):
            for target in procedure_index.get(called.casefold(), []):
                if target["id"] != procedure["id"]:
                    add_edge(procedure["id"], target["id"], "calls", "")
        for _, kind_hint, target_name in procedure["opens"]:
            target = resolve(kind_hint, target_name)
            if target:
                add_edge(procedure["id"], target, "opens", "DoCmd")
        for literal in procedure["effects"].get("sql_literals", []):
            for match in re.finditer(
                r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(\[[^\]]+\]|\w+)", literal, re.IGNORECASE
            ):
                target = resolve(None, match.group(1).strip("[]"))
                if target:
                    add_edge(procedure["id"], target, "reads", "SQL")

    entry_points: list[dict[str, Any]] = []
    for entry in catalogue:
        if entry["kind"] == "macro" and (entry["name"] or "").casefold() == "autoexec":
            entry_points.append(
                {"id": entry["id"], "name": entry["name"], "why_key": "autoexec"}
            )
    incoming = {edge["to"] for edge in edges}
    for entry in catalogue:
        if entry["kind"] in {"form", "report"} and entry["id"] not in incoming:
            entry_points.append(
                {"id": entry["id"], "name": entry["name"], "why_key": "no_inbound"}
            )

    diagrams: dict[str, Any] = {}
    for procedure in procedures:
        diagrams[procedure["id"]] = procedure["flow"]
    for item in objects:
        model = item.get("semantics")
        if not model:
            continue
        name = item.get("name") or ""
        if item.get("kind") == "macro" and item.get("subtype") == "data_macro":
            for handler in model.get("handlers", []):
                diagrams[f"datamacro::{name}::{handler['event']}"] = _macro_flow(
                    handler["statements"],
                    {
                        "label": "",
                        "extra": {
                            "text_key": "data_macro_title",
                            "text_args": {
                                "table": name,
                                "event": handler["event"],
                            },
                        },
                    },
                )
        elif item.get("kind") == "macro":
            diagrams[f"macro::{name}"] = _macro_flow(
                model.get("statements", []),
                {
                    "label": "",
                    "extra": {
                        "text_key": "macro_title",
                        "text_args": {"name": name},
                    },
                },
            )

    return {
        "schema": FLOW_SCHEMA,
        "objects": catalogue,
        "procedures": procedures,
        "graph": {"nodes": nodes, "edges": edges},
        "diagrams": diagrams,
        "entry_points": entry_points,
    }
