"""Translate Access macros and data macros into an explicit statement tree.

Access stores a macro twice.  The legacy ``Begin``/``Action``/``Argument``
list is a flat sequence with a per-row ``Condition``; the modern AXL XML
(``_AXL:`` comment chunks in a macro, ``.axl`` files for data macros) is a
properly nested program with conditional blocks, loops and record scopes.
The XML is used whenever it is present, because a flat list cannot express
the ``Else`` branch a nested block has.

Actions are classified, not merely listed.  A data action such as ``SetField``
becomes a statement in the target database; a UI action such as ``OpenForm``
cannot, and saying so is the point of the classification.
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree

from ._expr import analyze_expression
from ._textformat import Block

__all__ = [
    "translate_macro",
    "translate_data_macro",
    "MACRO_ACTION_SUPPORT",
]

_AXL_NS = "{http://schemas.microsoft.com/office/accessservices/2009/11/application}"

#: How each macro action can be carried into a target system.
#:
#: ``data``      - expressible as data manipulation in the target database.
#: ``control``   - control flow this converter models directly.
#: ``ui``        - drives the Access user interface; needs an application layer.
#: ``system``    - touches the host, the file system or the Access application.
MACRO_ACTION_SUPPORT: dict[str, str] = {}


def _register(kind: str, names: str) -> None:
    for name in names.split():
        MACRO_ACTION_SUPPORT[name.casefold()] = kind


_register("data", """
    SetField SetLocalVar LookUpRecord ForEachRecord CreateRecord EditRecord
    DeleteRecord RunDataMacro RaiseError ClearMacroError SetReturnVar
    CreateSharePointRecord
""")
_register("control", """
    StopMacro StopAllMacros OnError ExitForEachRecord Comment Group
    SubMacro If Else ElseIf
""")
_register("ui", """
    OpenForm OpenReport OpenQuery OpenTable OpenView OpenStoredProcedure
    CloseWindow Close GoToRecord GoToControl GoToPage FindRecord FindNext
    ApplyFilter RemoveFilter Requery RepaintObject RefreshRecord
    SetValue SetProperty SetFilter SetOrderBy SetDisplayedCategories
    SelectObject MaximizeWindow MinimizeWindow RestoreWindow MoveAndSizeWindow
    MessageBox Beep NavigateTo BrowseTo LockNavigationPane
    ShowAllRecords SearchForRecord PrintObject PrintPreview
    AddMenu SetMenuItem ShowToolbar RunMenuCommand SingleStep
    RedisplayRecords RefreshAllData UndoRecord SaveRecord
    NewObjectForm NewObjectReport
""")
_register("system", """
    RunCode RunMacro RunApplication RunSQL RunSavedImportExport
    QuitAccess Quit CopyObject DeleteObject RenameObject SaveObject
    OutputTo SendObject TransferDatabase TransferSpreadsheet TransferText
    ImportExportData ImportExportSpreadsheet ImportExportText
    SetWarnings SetLocalVarEcho Echo Hourglass DisplayHourglassPointer
    CancelEvent StopAllMacrosOnError
""")

_UI_NOTES = {
    "ui": "drives the Access user interface; the target system needs an "
          "application layer to reproduce it",
    "system": "runs code, moves data between files or changes the Access "
              "application itself; it has no place inside a database schema",
}


def _classify(action: str) -> tuple[str, str]:
    kind = MACRO_ACTION_SUPPORT.get(action.casefold())
    if kind is None:
        return "unknown", "not a macro action this converter recognises"
    return kind, _UI_NOTES.get(kind, "")


def _condition(text: str, known: set[str]) -> dict[str, Any] | None:
    if not text:
        return None
    analysis = analyze_expression(text, known)
    return {
        "source": text,
        "sql": analysis.sql,
        "translated": analysis.translatable,
        "unsupported": analysis.unsupported,
    }


def _argument_value(text: str, known: set[str]) -> dict[str, Any]:
    analysis = analyze_expression(text, known)
    return {
        "source": text,
        "sql": analysis.sql if analysis.parsed else None,
        "translated": analysis.translatable,
        "unsupported": analysis.unsupported if analysis.parsed else [],
    }


# --------------------------------------------------------------------------
# AXL (XML) macros
# --------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _axl_statements(
    parent: ElementTree.Element,
    known: set[str],
    problems: list[dict[str, str]],
) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = []
    for element in parent:
        tag = _local(element.tag)
        if tag == "Comment":
            statements.append({"type": "comment", "text": (element.text or "").strip()})
            continue
        if tag == "Action":
            name = element.get("Name") or ""
            kind, note = _classify(name)
            arguments: dict[str, Any] = {}
            for argument in element:
                if _local(argument.tag) != "Argument":
                    continue
                arguments[argument.get("Name") or "?"] = _argument_value(
                    (argument.text or "").strip(), known
                )
            entry = {
                "type": "action",
                "action": name,
                "category": kind,
                "arguments": arguments,
                "translated": kind in {"data", "control"},
            }
            if kind not in {"data", "control"}:
                problem = {
                    "reason_code": f"MACRO_ACTION_{kind.upper()}_NOT_TRANSLATABLE",
                    "detail": name,
                    "note": note,
                }
                entry["unsupported"] = [problem]
                if problem not in problems:
                    problems.append(problem)
            statements.append(entry)
            continue
        if tag == "ConditionalBlock":
            branches: list[dict[str, Any]] = []
            for branch in element:
                branch_tag = _local(branch.tag)
                condition_element = branch.find(f"{_AXL_NS}Condition")
                if condition_element is None:
                    condition_element = branch.find("Condition")
                condition_text = (
                    (condition_element.text or "").strip()
                    if condition_element is not None
                    else ""
                )
                body = branch.find(f"{_AXL_NS}Statements")
                if body is None:
                    body = branch.find("Statements")
                branches.append(
                    {
                        "branch": branch_tag.lower(),
                        "condition": _condition(condition_text, known),
                        "statements": (
                            _axl_statements(body, known, problems)
                            if body is not None
                            else []
                        ),
                    }
                )
            statements.append({"type": "conditional", "branches": branches})
            continue
        if tag in {
            "ForEachRecord", "LookUpRecord", "CreateRecord", "EditRecord",
            "DeleteRecord", "Group", "SubMacro", "UserInterfaceMacro",
            "DataMacro", "Statements",
        }:
            body = element.find(f"{_AXL_NS}Statements") or element.find("Statements")
            data = element.find(f"{_AXL_NS}Data") or element.find("Data")
            kind, note = _classify(tag)
            entry = {
                "type": "block",
                "block": tag,
                "name": element.get("Name"),
                "category": kind if kind != "unknown" else "control",
                "scope": {
                    child.get("Name") or _local(child.tag): (child.text or "").strip()
                    for child in (data if data is not None else [])
                }
                if data is not None
                else {},
                "attributes": dict(element.attrib),
                "statements": (
                    _axl_statements(body, known, problems)
                    if body is not None
                    else _axl_statements(element, known, problems)
                ),
            }
            statements.append(entry)
            continue
        statements.append(
            {
                "type": "unknown_element",
                "element": tag,
                "attributes": dict(element.attrib),
            }
        )
        problem = {
            "reason_code": "MACRO_ELEMENT_NOT_RECOGNISED",
            "detail": tag,
            "note": "an AXL element this converter does not model",
        }
        if problem not in problems:
            problems.append(problem)
    return statements


def _parse_axl(xml_text: str) -> ElementTree.Element:
    lowered = xml_text.casefold()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ValueError("XML document type and entity declarations are refused")
    return ElementTree.fromstring(xml_text)


def translate_data_macro(
    xml_text: str, table_name: str, known_functions: set[str] | None = None
) -> dict[str, Any]:
    """Translate one table's ``.axl`` data macros into trigger-shaped IR."""
    known = known_functions or set()
    problems: list[dict[str, str]] = []
    root = _parse_axl(xml_text)
    handlers: list[dict[str, Any]] = []
    for element in root.iter():
        if _local(element.tag) != "DataMacro":
            continue
        body = element.find(f"{_AXL_NS}Statements") or element.find("Statements")
        event = element.get("Event") or element.get("Name") or "Unnamed"
        handlers.append(
            {
                "event": event,
                "name": element.get("Name"),
                "trigger_timing": "before" if event.lower().startswith("before") else "after",
                "trigger_operation": _trigger_operation(event),
                "statements": (
                    _axl_statements(body, known, problems) if body is not None else []
                ),
            }
        )
    return {
        "kind": "data_macro",
        "name": table_name,
        "table": table_name,
        "handlers": handlers,
        "unsupported": problems,
        "translated": bool(handlers) and not problems,
    }


def _trigger_operation(event: str) -> str:
    lowered = event.casefold()
    if "delete" in lowered:
        return "delete"
    if "insert" in lowered:
        return "insert"
    if "change" in lowered or "update" in lowered:
        return "insert_or_update"
    return "unknown"


# --------------------------------------------------------------------------
# Legacy macro text
# --------------------------------------------------------------------------


def _reassemble_axl(root: Block) -> str | None:
    chunks: list[str] = []
    for block in root.walk():
        for item in block.properties:
            if item.key == "Comment" and item.value.startswith("_AXL:"):
                chunks.append(item.value[len("_AXL:") :])
    if not chunks:
        return None
    return "".join(chunks)


def translate_macro(
    root: Block, name: str, known_functions: set[str] | None = None
) -> dict[str, Any]:
    """Translate a stand-alone macro object."""
    known = known_functions or set()
    problems: list[dict[str, str]] = []

    axl = _reassemble_axl(root)
    if axl:
        try:
            element = _parse_axl(axl)
        except (ElementTree.ParseError, ValueError) as error:
            problems.append(
                {
                    "reason_code": "MACRO_AXL_NOT_PARSED",
                    "detail": str(error),
                    "note": "the embedded AXL representation could not be "
                    "reassembled; the legacy action list is used instead",
                }
            )
        else:
            body = element.find(f"{_AXL_NS}Statements") or element.find("Statements")
            statements = (
                _axl_statements(body, known, problems) if body is not None else []
            )
            return {
                "kind": "macro",
                "name": name,
                "representation": "axl",
                "statements": statements,
                "actions": _action_census(statements),
                "unsupported": problems,
                "translated": bool(statements) and not problems,
            }

    statements: list[dict[str, Any]] = []
    for block in root.children:
        if block.type_name is not None:
            continue
        properties = {item.key: item.value for item in block.properties}
        if "Action" not in properties:
            if properties.get("Comment"):
                statements.append(
                    {"type": "comment", "text": properties["Comment"]}
                )
            continue
        action = properties["Action"]
        kind, note = _classify(action)
        arguments = [
            _argument_value(item.value, known)
            for item in block.properties
            if item.key == "Argument"
        ]
        entry: dict[str, Any] = {
            "type": "action",
            "action": action,
            "category": kind,
            "positional_arguments": arguments,
            "condition": _condition(properties.get("Condition", ""), known),
            "translated": kind in {"data", "control"},
        }
        if kind not in {"data", "control"}:
            problem = {
                "reason_code": f"MACRO_ACTION_{kind.upper()}_NOT_TRANSLATABLE",
                "detail": action,
                "note": note,
            }
            entry["unsupported"] = [problem]
            if problem not in problems:
                problems.append(problem)
        statements.append(entry)

    problems.append(
        {
            "reason_code": "MACRO_STORED_AS_FLAT_ACTION_LIST",
            "detail": name,
            "note": "only the legacy row-per-action representation was "
            "available, so branch structure beyond a per-row condition "
            "cannot be recovered",
        }
    )
    return {
        "kind": "macro",
        "name": name,
        "representation": "legacy_action_list",
        "statements": statements,
        "actions": _action_census(statements),
        "unsupported": problems,
        "translated": False,
    }


def _action_census(statements: list[dict[str, Any]]) -> dict[str, list[str]]:
    census: dict[str, list[str]] = {}

    def walk(items: list[dict[str, Any]]) -> None:
        for item in items:
            if item.get("type") == "action":
                bucket = census.setdefault(item["category"], [])
                if item["action"] not in bucket:
                    bucket.append(item["action"])
            for key in ("statements",):
                if isinstance(item.get(key), list):
                    walk(item[key])
            for branch in item.get("branches", []) or []:
                walk(branch.get("statements", []))

    walk(statements)
    return {key: sorted(value) for key, value in sorted(census.items())}
