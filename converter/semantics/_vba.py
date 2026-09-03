"""Structural analysis of VBA source into procedures and control-flow graphs.

This is not a VBA interpreter and never becomes one: no code is executed and
no expression is evaluated.  The module recovers the *shape* of each
procedure - its branches, loops, error handlers, exits and calls - because
that shape is what a migration has to reproduce and what a human needs to see
drawn as a flow chart.

The recovered statement tree is deliberately conservative.  A line that does
not match a structure keyword becomes a plain statement rather than being
guessed at, and anything that reaches outside the database (``DoCmd``, file
I/O, ``Shell``, automation) is recorded as an explicit external effect.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

__all__ = [
    "parse_vba_module",
    "build_flow_graph",
    "EXTERNAL_EFFECTS",
]


# --------------------------------------------------------------------------
# Lexical preparation
# --------------------------------------------------------------------------

_LINE_NUMBER = re.compile(r"^\s*(\d+)\s+(?=\S)")
_LABEL = re.compile(r"^([A-Za-z_]\w*)\s*:\s*$")


def _strip_comment(line: str) -> tuple[str, str | None]:
    """Remove a trailing VBA comment, respecting string literals."""
    in_string = False
    index = 0
    while index < len(line):
        char = line[index]
        if char == '"':
            in_string = not in_string
        elif not in_string:
            if char == "'":
                return line[:index], line[index + 1 :].strip()
            if line[index:].lower().startswith("rem ") and (
                index == 0 or line[index - 1] in " \t:"
            ):
                return line[:index], line[index + 4 :].strip()
        index += 1
    return line, None


def _join_continuations(text: str) -> list[tuple[int, str, str | None]]:
    """Return ``(line_number, code, comment)`` with ``_`` continuations joined."""
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result: list[tuple[int, str, str | None]] = []
    buffer = ""
    comments: list[str] = []
    start = 1
    for number, raw in enumerate(raw_lines, start=1):
        code, comment = _strip_comment(raw)
        if comment:
            comments.append(comment)
        stripped = code.rstrip()
        if not buffer:
            start = number
        if stripped.endswith(" _") or stripped == "_":
            buffer += stripped[:-1] + " "
            continue
        buffer += stripped
        joined = buffer.strip()
        buffer = ""
        result.append((start, joined, " ".join(comments) if comments else None))
        comments = []
    if buffer.strip() or comments:
        result.append((start, buffer.strip(), " ".join(comments) if comments else None))
    return result


def _split_colon_statements(code: str) -> list[str]:
    """Split ``a = 1 : b = 2`` while leaving labels and strings alone."""
    if ":" not in code:
        return [code]
    if _LABEL.match(code.strip()):
        return [code]
    parts: list[str] = []
    current = ""
    in_string = False
    index = 0
    while index < len(code):
        char = code[index]
        if char == '"':
            in_string = not in_string
            current += char
        elif char == ":" and not in_string:
            following = code[index + 1 : index + 2]
            if following == "=":  # not VBA, but never split a := token
                current += char
            else:
                parts.append(current)
                current = ""
        else:
            current += char
        index += 1
    parts.append(current)
    return [part for part in (p.strip() for p in parts) if part]


# --------------------------------------------------------------------------
# External effects
# --------------------------------------------------------------------------

#: Constructs that reach outside the database.  Each entry is a reason code
#: plus the note explaining why the target system cannot simply inherit it.
EXTERNAL_EFFECTS: dict[str, tuple[str, str]] = {
    "docmd": (
        "VBA_DRIVES_ACCESS_UI",
        "DoCmd drives the Access user interface and object model; the target "
        "system needs an application layer to reproduce it",
    ),
    "msgbox": (
        "VBA_INTERACTIVE_DIALOG",
        "an interactive dialog cannot run inside a database engine",
    ),
    "inputbox": (
        "VBA_INTERACTIVE_DIALOG",
        "an interactive dialog cannot run inside a database engine",
    ),
    "shell": (
        "VBA_STARTS_EXTERNAL_PROCESS",
        "starts a process on the host machine",
    ),
    "createobject": (
        "VBA_AUTOMATION_OBJECT",
        "creates a COM automation object outside the database",
    ),
    "getobject": (
        "VBA_AUTOMATION_OBJECT",
        "binds to a COM automation object outside the database",
    ),
    "sendkeys": ("VBA_SENDS_KEYSTROKES", "synthesises keyboard input"),
    "kill": ("VBA_FILE_SYSTEM_ACCESS", "deletes a file on the host machine"),
    "open": ("VBA_FILE_SYSTEM_ACCESS", "opens a host file for I/O"),
    "filecopy": ("VBA_FILE_SYSTEM_ACCESS", "copies a host file"),
    "mkdir": ("VBA_FILE_SYSTEM_ACCESS", "creates a host directory"),
    "environ": ("VBA_HOST_ENVIRONMENT", "reads a host environment variable"),
    "application": (
        "VBA_ACCESS_APPLICATION_OBJECT",
        "reads or changes the Access application object",
    ),
    "screen": (
        "VBA_ACCESS_APPLICATION_OBJECT",
        "reads the Access screen object",
    ),
    "forms": (
        "VBA_ACCESS_FORM_REFERENCE",
        "reads or writes a loaded Access form",
    ),
    "reports": (
        "VBA_ACCESS_REPORT_REFERENCE",
        "reads or writes a loaded Access report",
    ),
}

_DATA_ACCESS = re.compile(
    r"\b(CurrentDb|DBEngine|CodeDb)\b|\b(?:OpenRecordset|Execute|RunSQL)\s*\(?",
    re.IGNORECASE,
)
_SQL_LITERAL = re.compile(
    r'"\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b', re.IGNORECASE
)
_DOCMD = re.compile(r"\bDoCmd\s*\.\s*(\w+)", re.IGNORECASE)
_CALL_NAME = re.compile(r"(?<![.\w])([A-Za-z_]\w*)\s*\(")
_CALL_STATEMENT = re.compile(r"^Call\s+([A-Za-z_][\w.]*)", re.IGNORECASE)


def _string_literals(code: str) -> list[str]:
    return re.findall(r'"((?:[^"]|"")*)"', code)


# --------------------------------------------------------------------------
# Procedure extraction
# --------------------------------------------------------------------------

_PROC_START = re.compile(
    r"^(?P<modifiers>(?:(?:Public|Private|Friend|Static)\s+)*)"
    r"(?P<kind>Sub|Function|Property\s+(?:Get|Let|Set))\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*"
    r"(?P<signature>\(.*\))?"
    r"(?:\s+As\s+(?P<returns>[\w.()]+))?\s*$",
    re.IGNORECASE,
)
_PROC_END = re.compile(r"^End\s+(Sub|Function|Property)\s*$", re.IGNORECASE)
_DECLARE = re.compile(r"^(?:Public|Private)?\s*Declare\b", re.IGNORECASE)


def _split_parameters(signature: str | None) -> list[dict[str, Any]]:
    if not signature:
        return []
    body = signature.strip()[1:-1].strip()
    if not body:
        return []
    parameters: list[dict[str, Any]] = []
    depth = 0
    current = ""
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parameters.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        parameters.append(current.strip())
    result = []
    for raw in parameters:
        optional = bool(re.match(r"^Optional\b", raw, re.IGNORECASE))
        by_ref = bool(re.search(r"\bByRef\b", raw, re.IGNORECASE))
        by_val = bool(re.search(r"\bByVal\b", raw, re.IGNORECASE))
        paramarray = bool(re.search(r"\bParamArray\b", raw, re.IGNORECASE))
        cleaned = re.sub(
            r"^(Optional|ByRef|ByVal|ParamArray)\s+", "", raw, flags=re.IGNORECASE
        )
        cleaned = re.sub(
            r"^(Optional|ByRef|ByVal|ParamArray)\s+", "", cleaned, flags=re.IGNORECASE
        )
        name_match = re.match(r"^([A-Za-z_]\w*)", cleaned)
        type_match = re.search(r"\bAs\s+([\w.]+)", cleaned, re.IGNORECASE)
        default_match = re.search(r"=\s*(.+)$", cleaned)
        result.append(
            {
                "name": name_match.group(1) if name_match else cleaned,
                "type": type_match.group(1) if type_match else None,
                "optional": optional,
                "by_ref": by_ref or not by_val,
                "param_array": paramarray,
                "default": default_match.group(1).strip() if default_match else None,
            }
        )
    return result


# --------------------------------------------------------------------------
# Statement tree
# --------------------------------------------------------------------------

_SINGLE_IF = re.compile(r"^If\s+(?P<cond>.+?)\s+Then\s+(?P<body>\S.*)$", re.IGNORECASE)
_BLOCK_IF = re.compile(r"^If\s+(?P<cond>.+?)\s+Then$", re.IGNORECASE)
_ELSEIF = re.compile(r"^ElseIf\s+(?P<cond>.+?)\s+Then$", re.IGNORECASE)
_FOR_EACH = re.compile(r"^For\s+Each\s+(?P<var>[\w.]+)\s+In\s+(?P<coll>.+)$", re.IGNORECASE)
_FOR = re.compile(r"^For\s+(?P<var>[\w.]+)\s*=\s*(?P<range>.+)$", re.IGNORECASE)
_DO = re.compile(r"^Do(?:\s+(?P<test>While|Until)\s+(?P<cond>.+))?$", re.IGNORECASE)
_LOOP = re.compile(r"^Loop(?:\s+(?P<test>While|Until)\s+(?P<cond>.+))?$", re.IGNORECASE)
_WHILE = re.compile(r"^While\s+(?P<cond>.+)$", re.IGNORECASE)
_SELECT = re.compile(r"^Select\s+Case\s+(?P<subject>.+)$", re.IGNORECASE)
_CASE = re.compile(r"^Case\s+(?P<match>.+)$", re.IGNORECASE)
_WITH = re.compile(r"^With\s+(?P<target>.+)$", re.IGNORECASE)
_ON_ERROR = re.compile(
    r"^On\s+Error\s+(?P<mode>GoTo\s+\w+|Resume\s+Next|GoTo\s+0)$", re.IGNORECASE
)
_GOTO = re.compile(r"^GoTo\s+(?P<target>\w+)$", re.IGNORECASE)
_RESUME = re.compile(r"^Resume(?:\s+(?P<target>Next|\w+))?$", re.IGNORECASE)
_EXIT = re.compile(r"^Exit\s+(?P<what>Sub|Function|Property|For|Do)$", re.IGNORECASE)
_DECLARATION = re.compile(
    r"^(Dim|Const|Static|ReDim|Public|Private|Set\s+|Let\s+)\b", re.IGNORECASE
)


class _StatementParser:
    def __init__(self, lines: list[tuple[int, str, str | None]]):
        self.lines = lines
        self.index = 0

    def peek(self) -> tuple[int, str, str | None] | None:
        return self.lines[self.index] if self.index < len(self.lines) else None

    def parse_block(self, terminators: tuple[str, ...]) -> list[dict[str, Any]]:
        statements: list[dict[str, Any]] = []
        while True:
            item = self.peek()
            if item is None:
                return statements
            _, code, _ = item
            lowered = code.casefold()
            if any(
                lowered == term or lowered.startswith(term + " ")
                for term in terminators
            ):
                return statements
            self.index += 1
            statements.append(self.parse_statement(item))

    def parse_statement(self, item: tuple[int, str, str | None]) -> dict[str, Any]:
        line, code, comment = item
        base = {"line": line, "source": code, "comment": comment}

        label = _LABEL.match(code)
        if label is not None:
            return {**base, "type": "label", "name": label.group(1)}

        match = _SINGLE_IF.match(code)
        if match is not None:
            body_lines = [
                (line, part, None)
                for part in _split_colon_statements(match.group("body"))
            ]
            inner = _StatementParser(body_lines)
            return {
                **base,
                "type": "if",
                "branches": [
                    {
                        "kind": "if",
                        "condition": match.group("cond").strip(),
                        "statements": inner.parse_block(()),
                    }
                ],
                "single_line": True,
            }

        match = _BLOCK_IF.match(code)
        if match is not None:
            branches = [
                {
                    "kind": "if",
                    "condition": match.group("cond").strip(),
                    "statements": self.parse_block(("elseif", "else", "end if")),
                }
            ]
            while True:
                nxt = self.peek()
                if nxt is None:
                    break
                lowered = nxt[1].casefold()
                if lowered.startswith("elseif"):
                    self.index += 1
                    elseif = _ELSEIF.match(nxt[1])
                    branches.append(
                        {
                            "kind": "elseif",
                            "condition": (
                                elseif.group("cond").strip() if elseif else nxt[1]
                            ),
                            "statements": self.parse_block(
                                ("elseif", "else", "end if")
                            ),
                        }
                    )
                    continue
                if lowered == "else":
                    self.index += 1
                    branches.append(
                        {
                            "kind": "else",
                            "condition": None,
                            "statements": self.parse_block(("end if",)),
                        }
                    )
                    continue
                break
            self._consume("end if")
            return {**base, "type": "if", "branches": branches, "single_line": False}

        match = _FOR_EACH.match(code)
        if match is not None:
            body = self.parse_block(("next",))
            self._consume("next")
            return {
                **base,
                "type": "for_each",
                "variable": match.group("var"),
                "collection": match.group("coll").strip(),
                "statements": body,
            }

        match = _FOR.match(code)
        if match is not None and not code.casefold().startswith("for each"):
            body = self.parse_block(("next",))
            self._consume("next")
            return {
                **base,
                "type": "for",
                "variable": match.group("var"),
                "range": match.group("range").strip(),
                "statements": body,
            }

        match = _DO.match(code)
        if match is not None:
            body = self.parse_block(("loop",))
            closing = self.peek()
            test_at_end = None
            if closing is not None and closing[1].casefold().startswith("loop"):
                self.index += 1
                loop = _LOOP.match(closing[1])
                if loop is not None and loop.group("cond"):
                    test_at_end = {
                        "test": (loop.group("test") or "").lower(),
                        "condition": loop.group("cond").strip(),
                    }
            return {
                **base,
                "type": "do_loop",
                "test_at_start": (
                    {
                        "test": (match.group("test") or "").lower(),
                        "condition": match.group("cond").strip(),
                    }
                    if match.group("cond")
                    else None
                ),
                "test_at_end": test_at_end,
                "statements": body,
            }

        match = _WHILE.match(code)
        if match is not None:
            body = self.parse_block(("wend",))
            self._consume("wend")
            return {
                **base,
                "type": "while",
                "condition": match.group("cond").strip(),
                "statements": body,
            }

        match = _SELECT.match(code)
        if match is not None:
            cases: list[dict[str, Any]] = []
            while True:
                nxt = self.peek()
                if nxt is None:
                    break
                lowered = nxt[1].casefold()
                if lowered.startswith("end select"):
                    break
                if lowered.startswith("case"):
                    self.index += 1
                    case = _CASE.match(nxt[1])
                    label_text = case.group("match").strip() if case else nxt[1]
                    cases.append(
                        {
                            "match": None if label_text.casefold() == "else" else label_text,
                            "is_else": label_text.casefold() == "else",
                            "statements": self.parse_block(("case", "end select")),
                        }
                    )
                    continue
                self.index += 1
            self._consume("end select")
            return {
                **base,
                "type": "select_case",
                "subject": match.group("subject").strip(),
                "cases": cases,
            }

        match = _WITH.match(code)
        if match is not None:
            body = self.parse_block(("end with",))
            self._consume("end with")
            return {
                **base,
                "type": "with",
                "target": match.group("target").strip(),
                "statements": body,
            }

        match = _ON_ERROR.match(code)
        if match is not None:
            mode = re.sub(r"\s+", " ", match.group("mode")).strip()
            target = None
            goto = re.match(r"GoTo\s+(\w+)", mode, re.IGNORECASE)
            if goto is not None and goto.group(1) != "0":
                target = goto.group(1)
            return {
                **base,
                "type": "on_error",
                "mode": (
                    "resume_next"
                    if mode.casefold().startswith("resume")
                    else ("disable" if target is None else "goto")
                ),
                "target": target,
            }

        match = _GOTO.match(code)
        if match is not None:
            return {**base, "type": "goto", "target": match.group("target")}

        match = _RESUME.match(code)
        if match is not None:
            return {**base, "type": "resume", "target": match.group("target")}

        match = _EXIT.match(code)
        if match is not None:
            return {**base, "type": "exit", "what": match.group("what").lower()}

        if _DECLARATION.match(code) and not re.match(
            r"^(Set|Let)\s", code, re.IGNORECASE
        ):
            return {**base, "type": "declaration"}

        return {**base, "type": "statement"}

    def _consume(self, keyword: str) -> None:
        item = self.peek()
        if item is not None and item[1].casefold().startswith(keyword):
            self.index += 1


# --------------------------------------------------------------------------
# Effect extraction
# --------------------------------------------------------------------------


def _walk_statements(statements: Iterable[dict[str, Any]]):
    for statement in statements:
        yield statement
        for key in ("statements",):
            if isinstance(statement.get(key), list):
                yield from _walk_statements(statement[key])
        for branch in statement.get("branches", []) or []:
            yield from _walk_statements(branch.get("statements", []))
        for case in statement.get("cases", []) or []:
            yield from _walk_statements(case.get("statements", []))


def _analyze_effects(
    statements: list[dict[str, Any]], declared: set[str]
) -> dict[str, Any]:
    calls: list[str] = []
    docmd: list[str] = []
    sql: list[str] = []
    effects: list[dict[str, str]] = []
    data_access = False
    for statement in _walk_statements(statements):
        code = statement.get("source") or ""
        if statement["type"] in {"label", "declaration"}:
            continue
        for match in _DOCMD.finditer(code):
            action = match.group(1)
            if action not in docmd:
                docmd.append(action)
        call = _CALL_STATEMENT.match(code)
        if call is not None and call.group(1) not in calls:
            calls.append(call.group(1))
        for match in _CALL_NAME.finditer(code):
            name = match.group(1)
            if name.casefold() in _NOT_A_CALL:
                continue
            if name not in calls:
                calls.append(name)
        bare = re.match(r"^([A-Za-z_]\w*)(\s+[^=]*)?$", code)
        if bare is not None and bare.group(1) in declared and bare.group(1) not in calls:
            calls.append(bare.group(1))
        for literal in _string_literals(code):
            if _SQL_LITERAL.match('"' + literal + '"'):
                if literal not in sql:
                    sql.append(literal)
        if _DATA_ACCESS.search(code):
            data_access = True
        lowered = code.casefold()
        for token, (reason, note) in EXTERNAL_EFFECTS.items():
            if re.search(rf"(?<![\w.]){re.escape(token)}\b", lowered):
                entry = {"reason_code": reason, "detail": token, "note": note}
                if entry not in effects:
                    effects.append(entry)
    return {
        "calls": calls,
        "docmd_actions": docmd,
        "sql_literals": sql,
        "external_effects": effects,
        "uses_data_access": data_access,
    }


_NOT_A_CALL = {
    "if", "then", "else", "elseif", "for", "next", "do", "loop", "while",
    "wend", "select", "case", "with", "end", "sub", "function", "property",
    "dim", "set", "let", "const", "static", "redim", "exit", "goto", "resume",
    "on", "error", "and", "or", "not", "xor", "mod", "is", "like", "to",
    "step", "each", "in", "as", "byval", "byref", "optional", "call", "new",
    "true", "false", "nothing", "null", "empty", "me", "typeof", "print",
    "debug", "stop", "return", "gosub", "type", "enum", "declare", "lib",
    "preserve", "erase", "option", "explicit", "compare", "database",
}


# --------------------------------------------------------------------------
# Module parsing
# --------------------------------------------------------------------------


def parse_vba_module(source: str, module_name: str, module_kind: str = "module") -> dict[str, Any]:
    """Split VBA source into a header and a list of analysed procedures."""
    lines = _join_continuations(source)
    prepared: list[tuple[int, str, str | None]] = []
    for number, code, comment in lines:
        stripped = _LINE_NUMBER.sub("", code).strip()
        if not stripped:
            if comment:
                prepared.append((number, "", comment))
            continue
        for piece in _split_colon_statements(stripped):
            prepared.append((number, piece, comment))
            comment = None

    header: list[str] = []
    procedures: list[dict[str, Any]] = []
    declared_names: set[str] = set()
    index = 0
    total = len(prepared)
    while index < total:
        number, code, comment = prepared[index]
        match = _PROC_START.match(code) if code else None
        if match is None:
            if code:
                header.append(code)
            index += 1
            continue
        declared_names.add(match.group("name"))
        body: list[tuple[int, str, str | None]] = []
        index += 1
        while index < total:
            inner = prepared[index]
            if inner[1] and _PROC_END.match(inner[1]):
                index += 1
                break
            body.append(inner)
            index += 1
        statements = _StatementParser(
            [item for item in body if item[1]]
        ).parse_block(())
        kind = re.sub(r"\s+", " ", match.group("kind")).lower()
        procedures.append(
            {
                "name": match.group("name"),
                "kind": kind,
                "scope": (
                    "private"
                    if re.search(r"\bPrivate\b", match.group("modifiers") or "", re.I)
                    else "public"
                ),
                "static": bool(
                    re.search(r"\bStatic\b", match.group("modifiers") or "", re.I)
                ),
                "returns": match.group("returns"),
                "parameters": _split_parameters(match.group("signature")),
                "line": number,
                "statements": statements,
            }
        )

    for procedure in procedures:
        procedure["effects"] = _analyze_effects(
            procedure["statements"], declared_names
        )
        procedure["flow"] = build_flow_graph(procedure)
        procedure["metrics"] = _metrics(procedure)

    module_effects: list[dict[str, str]] = []
    for procedure in procedures:
        for effect in procedure["effects"]["external_effects"]:
            if effect not in module_effects:
                module_effects.append(effect)

    header_problems: list[dict[str, str]] = []
    for line in header:
        if _DECLARE.match(line):
            header_problems.append(
                {
                    "reason_code": "VBA_DECLARES_EXTERNAL_LIBRARY",
                    "detail": line[:160],
                    "note": "the module calls into a Windows DLL; that call has "
                    "no equivalent inside a database",
                }
            )
    for effect in module_effects:
        if effect not in header_problems:
            header_problems.append(effect)
    return {
        "kind": module_kind,
        "name": module_name,
        "option_lines": [line for line in header if line.lower().startswith("option ")],
        "module_declarations": [
            line for line in header if not line.lower().startswith("option ")
        ],
        "procedures": procedures,
        "external_effects": module_effects,
        "unsupported": header_problems,
        "translated": bool(procedures),
    }


def _metrics(procedure: dict[str, Any]) -> dict[str, int]:
    counts = {
        "statements": 0,
        "branches": 0,
        "loops": 0,
        "exits": 0,
        "gotos": 0,
        "labels": 0,
    }
    for statement in _walk_statements(procedure["statements"]):
        counts["statements"] += 1
        if statement["type"] == "if":
            counts["branches"] += len(statement.get("branches", []))
        elif statement["type"] == "select_case":
            counts["branches"] += len(statement.get("cases", []))
        elif statement["type"] in {"for", "for_each", "do_loop", "while"}:
            counts["loops"] += 1
        elif statement["type"] == "exit":
            counts["exits"] += 1
        elif statement["type"] == "goto":
            counts["gotos"] += 1
        elif statement["type"] == "label":
            counts["labels"] += 1
    counts["cyclomatic_complexity"] = 1 + counts["branches"] + counts["loops"]
    return counts


# --------------------------------------------------------------------------
# Control flow graph
# --------------------------------------------------------------------------


class _GraphBuilder:
    """Turns a structured statement tree into a drawable flow graph."""

    def __init__(self, procedure: dict[str, Any]):
        self.nodes: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self.counter = 0
        self.procedure = procedure
        self.label_nodes: dict[str, str] = {}
        self.pending_goto: list[tuple[str, str]] = []
        self.exit_nodes: list[str] = []
        self.loop_stack: list[tuple[str, str]] = []
        self.error_jumps: list[tuple[str, str]] = []

    def new_node(self, kind: str, label: str, **extra: Any) -> str:
        """Create a node.

        ``label`` carries source code, which is language neutral.  Wording that
        belongs to a human language is passed as ``text_key`` instead and is
        translated by the viewer, so one intermediate representation serves
        every locale.
        """
        self.counter += 1
        identifier = f"n{self.counter}"
        node = {"id": identifier, "kind": kind, "label": label}
        node.update(extra)
        self.nodes.append(node)
        return identifier

    def link(
        self,
        source: str | None,
        target: str,
        label_key: str = "",
        **extra: Any,
    ) -> None:
        """Join two nodes.  ``label_key`` names a phrase, never a phrase."""
        if source is None:
            return
        edge: dict[str, Any] = {"from": source, "to": target}
        if label_key:
            edge["label_key"] = label_key
        edge.update(extra)
        if edge not in self.edges:
            self.edges.append(edge)

    def build(self) -> dict[str, Any]:
        signature = _signature_text(self.procedure)
        start = self.new_node("start", signature)
        end = self.new_node("end", "", text_key="end")
        tail = self.emit(self.procedure["statements"], start)
        if tail is not None:
            self.link(tail, end)
        for source, target in self.pending_goto:
            node = self.label_nodes.get(target)
            self.link(source, node if node else end, "goto")
        for source, target in self.error_jumps:
            node = self.label_nodes.get(target)
            if node is not None:
                edge = {
                    "from": source,
                    "to": node,
                    "label_key": "on_error",
                    "kind": "error",
                }
                if edge not in self.edges:
                    self.edges.append(edge)
        for node_id in self.exit_nodes:
            self.link(node_id, end)
        return {"nodes": self.nodes, "edges": self.edges, "start": start, "end": end}

    def emit(self, statements: list[dict[str, Any]], previous: str | None) -> str | None:
        current = previous
        for statement in statements:
            current = self.emit_statement(statement, current)
        return current

    def emit_statement(
        self, statement: dict[str, Any], previous: str | None
    ) -> str | None:
        kind = statement["type"]
        source = statement.get("source", "")

        if kind == "label":
            node = self.new_node("label", statement["name"], line=statement["line"])
            self.label_nodes[statement["name"]] = node
            self.link(previous, node)
            return node

        if kind == "if":
            joins: list[str] = []
            fallthrough: str | None = previous
            has_else = False
            for branch in statement["branches"]:
                if branch["kind"] in {"if", "elseif"}:
                    decision = self.new_node(
                        "decision",
                        branch["condition"],
                        line=statement["line"],
                    )
                    self.link(fallthrough, decision)
                    entry, tail = self.emit_chain(branch["statements"])
                    if entry is not None:
                        self.link(decision, entry, "yes")
                        if tail is not None:
                            joins.append(tail)
                    else:
                        joins.append(decision)
                    fallthrough = decision
                else:
                    has_else = True
                    entry, tail = self.emit_chain(branch["statements"])
                    if entry is not None:
                        self.link(fallthrough, entry, "no")
                        if tail is not None:
                            joins.append(tail)
                        fallthrough = None
            merge = self.new_node("merge", "")
            for node in joins:
                self.link(node, merge)
            if fallthrough is not None:
                self.link(fallthrough, merge, "" if has_else else "no")
            return merge

        if kind == "select_case":
            decision = self.new_node(
                "decision", f"Select Case {statement['subject']}", line=statement["line"]
            )
            self.link(previous, decision)
            merge = self.new_node("merge", "")
            for case in statement["cases"]:
                # ``Case Else`` and ``Case 3`` are VBA source, not prose.
                label = "Case Else" if case["is_else"] else f"Case {case['match']}"
                entry, tail = self.emit_chain(case["statements"])
                if entry is None:
                    self.link(decision, merge, "", label=label)
                    continue
                self.link(decision, entry, "", label=label)
                if tail is not None:
                    self.link(tail, merge)
            if not statement["cases"] or not any(c["is_else"] for c in statement["cases"]):
                self.link(decision, merge, "no_match")
            return merge

        if kind in {"for", "for_each", "while", "do_loop"}:
            label = _loop_label(statement)
            head = self.new_node("loop", label, line=statement["line"])
            self.link(previous, head)
            exit_node = self.new_node("merge", "")
            self.loop_stack.append((head, exit_node))
            entry, tail = self.emit_chain(statement["statements"])
            if entry is not None:
                self.link(head, entry, "loop_body")
                if tail is not None:
                    self.link(tail, head, "loop_next")
            self.loop_stack.pop()
            self.link(head, exit_node, "loop_exit")
            return exit_node

        if kind == "with":
            node = self.new_node(
                "process", f"With {statement['target']}", line=statement["line"]
            )
            self.link(previous, node)
            return self.emit(statement["statements"], node)

        if kind == "on_error":
            text_key = {
                "goto": "on_error_goto",
                "resume_next": "on_error_resume_next",
                "disable": "on_error_disable",
            }[statement["mode"]]
            node = self.new_node(
                "error",
                "",
                line=statement["line"],
                target=statement.get("target"),
                text_key=text_key,
                text_args={"target": statement.get("target") or ""},
            )
            self.link(previous, node)
            if statement["mode"] == "goto" and statement.get("target"):
                self.error_jumps.append((node, statement["target"]))
            return node

        if kind == "goto":
            node = self.new_node(
                "goto", f"GoTo {statement['target']}", line=statement["line"]
            )
            self.link(previous, node)
            self.pending_goto.append((node, statement["target"]))
            return None

        if kind == "resume":
            target = statement.get("target")
            node = self.new_node(
                "goto",
                "Resume" + (f" {target}" if target else ""),
                line=statement["line"],
            )
            self.link(previous, node)
            if target and target.lower() != "next":
                self.pending_goto.append((node, target))
            return None

        if kind == "exit":
            what = statement["what"]
            node = self.new_node(
                "exit", f"Exit {what.capitalize()}", line=statement["line"]
            )
            self.link(previous, node)
            if what in {"for", "do"} and self.loop_stack:
                self.link(node, self.loop_stack[-1][1], "break")
            else:
                self.exit_nodes.append(node)
            return None

        node_kind = "process"
        if _DOCMD.search(source):
            node_kind = "ui"
        elif re.search(r"\bMsgBox\b|\bInputBox\b", source, re.IGNORECASE):
            node_kind = "io"
        elif _SQL_LITERAL.search(source) or _DATA_ACCESS.search(source):
            node_kind = "data"
        elif kind == "declaration":
            node_kind = "declaration"
        node = self.new_node(node_kind, _shorten(source), line=statement["line"])
        self.link(previous, node)
        return node

    def emit_chain(
        self, statements: list[dict[str, Any]]
    ) -> tuple[str | None, str | None]:
        """Emit a detached chain once and return its entry and tail nodes."""
        before = len(self.nodes)
        tail = self.emit(statements, None)
        if len(self.nodes) == before:
            return None, None
        return self.nodes[before]["id"], tail


def _signature_text(procedure: dict[str, Any]) -> str:
    parameters = ", ".join(
        (parameter["name"] + (f" As {parameter['type']}" if parameter["type"] else ""))
        for parameter in procedure["parameters"]
    )
    returns = f" As {procedure['returns']}" if procedure.get("returns") else ""
    return f"{procedure['kind'].title()} {procedure['name']}({parameters}){returns}"


def _loop_label(statement: dict[str, Any]) -> str:
    kind = statement["type"]
    if kind == "for":
        return f"For {statement['variable']} = {statement['range']}"
    if kind == "for_each":
        return f"For Each {statement['variable']} In {statement['collection']}"
    if kind == "while":
        return f"While {statement['condition']}"
    start = statement.get("test_at_start")
    end = statement.get("test_at_end")
    if start:
        return f"Do {start['test'].title()} {start['condition']}"
    if end:
        return f"Do ... Loop {end['test'].title()} {end['condition']}"
    return "Do ... Loop"


def _shorten(text: str, limit: int = 90) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def build_flow_graph(procedure: dict[str, Any]) -> dict[str, Any]:
    """Build the control-flow graph for one analysed procedure."""
    return _GraphBuilder(procedure).build()
