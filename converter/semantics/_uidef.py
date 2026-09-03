"""Translate Access form and report definitions into a screen model.

A form is where an Access application actually *is*: the record source binds
it to data, the controls bind to columns, and the event properties are the
entry points into VBA and macros.  Recovering that wiring is what makes a
call graph and a flow chart possible.

The definition text is parsed structurally; nothing is rendered, executed or
opened.  Layout geometry is preserved because the flow-chart UI draws a screen
sketch from it, but geometry is never counted as translated behaviour.
"""

from __future__ import annotations

import re
from typing import Any

from ._expr import analyze_expression
from ._textformat import Block

__all__ = ["translate_form", "SECTION_TYPES", "EVENT_PROPERTIES"]

#: Real form/report sections.  ``Page`` (a tab page) and ``BreakLevel`` (a
#: report grouping level) look similar in the text format but are not
#: sections, and a style-default block carries the same type name with no
#: ``Name`` property at all - that is what separates them.
SECTION_TYPES = {
    "FormHeader", "FormFooter", "PageHeader", "PageFooter", "Section",
    "ReportHeader", "ReportFooter", "GroupHeader", "GroupFooter",
    "BreakHeader", "BreakFooter",
}

_EVENT_PATTERN = re.compile(r"^(On[A-Z]\w*|Before[A-Z]\w*|After[A-Z]\w*)$")

#: Event property -> the VBA procedure suffix Access generates for it.
EVENT_PROPERTIES = {
    "OnCurrent": "Current", "OnLoad": "Load", "OnUnload": "Unload",
    "OnOpen": "Open", "OnClose": "Close", "OnClick": "Click",
    "OnDblClick": "DblClick", "OnChange": "Change", "OnEnter": "Enter",
    "OnExit": "Exit", "OnGotFocus": "GotFocus", "OnLostFocus": "LostFocus",
    "OnTimer": "Timer", "OnError": "Error", "OnDirty": "Dirty",
    "OnUndo": "Undo", "OnResize": "Resize", "OnActivate": "Activate",
    "OnDeactivate": "Deactivate", "OnDelete": "Delete", "OnFilter": "Filter",
    "OnApplyFilter": "ApplyFilter", "OnNoData": "NoData", "OnPage": "Page",
    "OnFormat": "Format", "OnPrint": "Print", "OnRetreat": "Retreat",
    "OnNotInList": "NotInList", "OnKeyDown": "KeyDown", "OnKeyUp": "KeyUp",
    "OnKeyPress": "KeyPress", "OnMouseDown": "MouseDown",
    "OnMouseUp": "MouseUp", "OnMouseMove": "MouseMove",
    "OnMouseWheel": "MouseWheel", "BeforeUpdate": "BeforeUpdate",
    "AfterUpdate": "AfterUpdate", "BeforeInsert": "BeforeInsert",
    "AfterInsert": "AfterInsert", "BeforeDelConfirm": "BeforeDelConfirm",
    "AfterDelConfirm": "AfterDelConfirm", "AfterFinalRender": "AfterFinalRender",
}

#: Control properties that carry behaviour rather than appearance.
_BEHAVIOUR_PROPERTIES = (
    "ControlSource", "RowSource", "RowSourceType", "BoundColumn",
    "DefaultValue", "ValidationRule", "ValidationText", "InputMask",
    "SourceObject", "LinkChildFields", "LinkMasterFields", "Filter",
    "FilterOn", "OrderBy", "OrderByOn", "Enabled", "Locked", "Visible",
    "TabStop", "TabIndex", "StatusBarText", "ControlTipText", "Caption",
    "HyperlinkAddress", "ShortcutMenuBar", "DisplayWhen",
)

_GEOMETRY = ("Left", "Top", "Width", "Height")

#: Form/report properties whose behaviour a target application must reproduce.
_OBJECT_BEHAVIOUR = (
    "RecordSource", "RecordsetType", "Filter", "FilterOn", "OrderBy",
    "OrderByOn", "AllowEdits", "AllowAdditions", "AllowDeletions",
    "AllowFilters", "DataEntry", "Caption", "DefaultView", "ViewsAllowed",
    "Cycle", "Modal", "PopUp", "MenuBar", "ShortcutMenuBar", "HasModule",
    "UniqueTable", "ServerFilter", "FilterOnLoad", "OrderByOnLoad",
)


def _event_binding(
    key: str, value: str, owner: str, known: set[str]
) -> dict[str, Any]:
    suffix = EVENT_PROPERTIES.get(key)
    if suffix is None:
        suffix = re.sub(r"^(On|Before|After)", "", key)
        if key.startswith(("Before", "After")):
            suffix = key
    procedure = f"{owner}_{suffix}"
    if value == "[Event Procedure]":
        return {
            "event": key,
            "handler_kind": "vba_event_procedure",
            "handler": procedure,
            "translated": True,
        }
    if value.startswith("="):
        analysis = analyze_expression(value[1:], known)
        return {
            "event": key,
            "handler_kind": "expression",
            "handler": value,
            "sql": analysis.sql,
            "translated": analysis.translatable,
            "unsupported": analysis.unsupported,
        }
    if value == "[Embedded Macro]":
        return {
            "event": key,
            "handler_kind": "embedded_macro",
            "handler": None,
            "translated": False,
            "unsupported": [
                {
                    "reason_code": "EMBEDDED_MACRO_BODY_NOT_IN_TEXT_DEFINITION",
                    "detail": f"{owner}.{key}",
                    "note": "the macro body is stored in the object's binary "
                    "properties, not in the text definition",
                }
            ],
        }
    return {
        "event": key,
        "handler_kind": "macro_object",
        "handler": value,
        "translated": True,
    }


def _properties(block: Block) -> dict[str, str]:
    return {item.key: item.value for item in block.properties if not item.is_binary}


def _translate_control(
    block: Block,
    owner_prefix: str,
    unsupported: list[dict[str, str]],
    known: set[str],
) -> dict[str, Any]:
    props = _properties(block)
    name = props.get("Name") or ""
    control: dict[str, Any] = {
        "name": name,
        "control_type": block.type_name,
        "geometry": {
            key.lower(): int(props[key])
            for key in _GEOMETRY
            if props.get(key, "").lstrip("-").isdigit()
        },
        "behaviour": {
            key: props[key] for key in _BEHAVIOUR_PROPERTIES if key in props
        },
        "events": [],
        "children": [],
        "bindings": [],
        "unsupported": [],
    }

    source = props.get("ControlSource")
    if source:
        if source.startswith("="):
            analysis = analyze_expression(source[1:], known)
            control["bindings"].append(
                {
                    "kind": "calculated",
                    "source": source,
                    "sql": analysis.sql,
                    "translated": analysis.translatable,
                }
            )
            for item in analysis.unsupported:
                if item not in control["unsupported"]:
                    control["unsupported"].append(item)
        else:
            control["bindings"].append(
                {"kind": "column", "source": source, "translated": True}
            )

    row_source = props.get("RowSource")
    if row_source:
        row_type = props.get("RowSourceType", "")
        control["bindings"].append(
            {
                "kind": "row_source",
                "row_source_type": row_type,
                "source": row_source,
                "translated": row_type.casefold() in {"table/query", ""},
            }
        )
        if row_type.casefold() not in {"table/query", ""}:
            control["unsupported"].append(
                {
                    "reason_code": "ROW_SOURCE_TYPE_NOT_A_QUERY",
                    "detail": f"{name}: {row_type}",
                    "note": "the list is filled by a value list or a VBA "
                    "callback function rather than by a query",
                }
            )

    if props.get("SourceObject"):
        control["bindings"].append(
            {
                "kind": "subform",
                "source": props["SourceObject"],
                "link_child": props.get("LinkChildFields"),
                "link_master": props.get("LinkMasterFields"),
                "translated": True,
            }
        )

    for key, value in props.items():
        if _EVENT_PATTERN.match(key) and value:
            binding = _event_binding(key, value, name or owner_prefix, known)
            control["events"].append(binding)
            for item in binding.get("unsupported", []) or []:
                if item not in control["unsupported"]:
                    control["unsupported"].append(item)

    if block.type_name in {"CustomControl", "UnboundObjectFrame", "WebBrowser"}:
        control["unsupported"].append(
            {
                "reason_code": "CONTROL_TYPE_HAS_NO_PORTABLE_EQUIVALENT",
                "detail": f"{name}: {block.type_name}",
                "note": "an ActiveX/OLE/browser control depends on a component "
                "outside the database",
            }
        )

    for child in block.children:
        if child.type_name is None:
            for grandchild in child.children:
                control["children"].append(
                    _translate_control(
                        grandchild, owner_prefix, unsupported, known
                    )
                )
        else:
            control["children"].append(
                _translate_control(child, owner_prefix, unsupported, known)
            )

    for item in control["unsupported"]:
        if item not in unsupported:
            unsupported.append(item)
    return control


def _iterate_controls(controls: list[dict[str, Any]]):
    for control in controls:
        yield control
        yield from _iterate_controls(control["children"])


def translate_form(
    root: Block,
    name: str,
    kind: str,
    known_functions: set[str] | None = None,
) -> dict[str, Any]:
    """Translate a parsed form or report definition."""
    known = known_functions or set()
    unsupported: list[dict[str, str]] = []
    holders = root.blocks("Form") + root.blocks("Report")
    if not holders:
        return {
            "kind": kind,
            "name": name,
            "translated": False,
            "sections": [],
            "controls": [],
            "events": [],
            "unsupported": [
                {
                    "reason_code": "OBJECT_BLOCK_NOT_FOUND",
                    "detail": name,
                    "note": "the definition has no Form or Report block",
                }
            ],
        }
    holder = holders[0]
    props = _properties(holder)
    owner = "Report" if holder.type_name == "Report" else "Form"

    object_events: list[dict[str, Any]] = []
    for key, value in props.items():
        if _EVENT_PATTERN.match(key) and value:
            binding = _event_binding(key, value, owner, known)
            object_events.append(binding)
            for item in binding.get("unsupported", []) or []:
                if item not in unsupported:
                    unsupported.append(item)

    sections: list[dict[str, Any]] = []
    style_defaults: list[str] = []
    group_levels: list[dict[str, Any]] = []
    for wrapper in holder.children:
        candidates = wrapper.children if wrapper.type_name is None else [wrapper]
        for child in candidates:
            if child.type_name == "BreakLevel":
                level_props = _properties(child)
                group_levels.append(
                    {
                        "control_source": level_props.get("ControlSource"),
                        "group_on": level_props.get("GroupOn"),
                        "group_interval": level_props.get("GroupInterval"),
                        "sort_order": level_props.get("SortOrder"),
                        "keep_together": level_props.get("KeepTogether"),
                    }
                )
                continue
            if child.type_name in SECTION_TYPES and _properties(child).get("Name"):
                section_props = _properties(child)
                controls: list[dict[str, Any]] = []
                for inner in child.children:
                    targets = (
                        inner.children if inner.type_name is None else [inner]
                    )
                    for target in targets:
                        controls.append(
                            _translate_control(target, owner, unsupported, known)
                        )
                section: dict[str, Any] = {
                    "name": section_props.get("Name") or child.type_name,
                    "section_type": child.type_name,
                    "height": section_props.get("Height"),
                    "controls": controls,
                    "events": [],
                }
                for key, value in section_props.items():
                    if _EVENT_PATTERN.match(key) and value:
                        section["events"].append(
                            _event_binding(
                                key,
                                value,
                                section["name"] or child.type_name,
                                known,
                            )
                        )
                sections.append(section)
            elif child.type_name is not None and not _properties(child).get("Name"):
                style_defaults.append(child.type_name)
            elif child.type_name is not None:
                for target in (
                    child.children[0].children
                    if child.children and child.children[0].type_name is None
                    else [child]
                ):
                    sections.append(
                        {
                            "name": _properties(child).get("Name") or child.type_name,
                            "section_type": child.type_name,
                            "height": _properties(child).get("Height"),
                            "controls": [
                                _translate_control(
                                    target, owner, unsupported, known
                                )
                            ],
                            "events": [],
                        }
                    )

    all_controls = [
        control for section in sections for control in _iterate_controls(section["controls"])
    ]

    record_source = props.get("RecordSource")
    dependencies: list[str] = []
    if record_source:
        if re.match(r"^\s*SELECT\b", record_source, re.IGNORECASE):
            dependencies.extend(
                sorted(
                    {
                        m.group(1).strip("[]")
                        for m in re.finditer(
                            r"\b(?:FROM|JOIN)\s+(\[[^\]]+\]|[A-Za-z_]\w*)",
                            record_source,
                            re.IGNORECASE,
                        )
                    }
                )
            )
        else:
            dependencies.append(record_source)
    for control in all_controls:
        for binding in control["bindings"]:
            if binding["kind"] in {"row_source", "subform"} and binding.get("source"):
                value = binding["source"]
                if not re.match(r"^\s*SELECT\b", value, re.IGNORECASE):
                    if value not in dependencies:
                        dependencies.append(value)

    events = list(object_events)
    for section in sections:
        events.extend(section["events"])
    for control in all_controls:
        events.extend(control["events"])

    handlers = sorted({e["handler"] for e in events if e.get("handler_kind") == "vba_event_procedure"})
    macro_handlers = sorted(
        {e["handler"] for e in events if e.get("handler_kind") == "macro_object" and e.get("handler")}
    )

    behaviour = {key: props[key] for key in _OBJECT_BEHAVIOUR if key in props}
    return {
        "kind": kind,
        "name": name,
        "record_source": record_source,
        "behaviour": behaviour,
        "sections": sections,
        "control_count": len(all_controls),
        "controls": all_controls,
        "events": events,
        "vba_handlers": handlers,
        "macro_handlers": macro_handlers,
        "dependencies": dependencies,
        "group_levels": group_levels,
        "style_defaults": sorted(set(style_defaults)),
        "unsupported": unsupported,
        "translated": bool(sections),
    }
