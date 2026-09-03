"""Offline analysis and translation of Access expressions.

Access expressions appear in query criteria, calculated columns, macro
conditions, control sources and validation rules.  They are *not* SQL: they mix
SQL predicates with VBA operators (``&``, ``\\``, ``Mod``), Access-only
functions (``Nz``, ``IIf``, ``DLookUp``), UI references (``[Forms]![f]![c]``)
and calls into the database's own VBA project.

This module parses such an expression into an AST and then answers the only
two questions the product must answer honestly:

* which parts can be carried into a target SQL system, and
* which parts cannot, and why.

Nothing here evaluates an expression.  No VBA runs, no query executes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ExpressionAnalysis",
    "analyze_expression",
    "FUNCTION_SUPPORT",
]


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

_KEYWORDS = {
    "and", "or", "not", "xor", "eqv", "imp", "mod", "like", "between",
    "in", "is", "null", "true", "false", "as",
}

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<bracket>\[[^\]]*\])
  | (?P<number>\d+\.\d+(?:[eE][+-]?\d+)?|\.\d+|\d+(?:[eE][+-]?\d+)?)
  | (?P<string>"(?:[^"]|"")*"|'(?:[^']|'')*')
  | (?P<date>\#[^#]*\#)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op><>|<=|>=|=|<|>|\+|-|\*|/|\\|\^|&|!|\.|,|\(|\)|;)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    position: int


class ExpressionError(Exception):
    """Raised when an expression cannot be parsed."""


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    length = len(text)
    while index < length:
        match = _TOKEN.match(text, index)
        if match is None:
            raise ExpressionError(
                f"unrecognised character at offset {index}: {text[index]!r}"
            )
        index = match.end()
        kind = match.lastgroup
        assert kind is not None
        if kind == "ws":
            continue
        value = match.group()
        if kind == "name" and value.casefold() in _KEYWORDS:
            kind = "keyword"
        tokens.append(Token(kind, value, match.start()))
    return tokens


# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------


@dataclass
class Node:
    kind: str
    #: Operator text, function name, literal text or identifier path.
    value: Any = None
    children: list["Node"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind}
        if self.value is not None:
            payload["value"] = self.value
        if self.children:
            payload["children"] = [c.to_dict() for c in self.children]
        return payload


# Binary operator precedence, lowest binds last.
_BINARY_PRECEDENCE = {
    "imp": 1,
    "eqv": 2,
    "xor": 3,
    "or": 4,
    "and": 5,
    "=": 7, "<>": 7, "<": 7, ">": 7, "<=": 7, ">=": 7,
    "like": 7, "between": 7, "in": 7, "is": 7,
    "&": 8,
    "+": 9, "-": 9,
    "*": 10, "/": 10, "\\": 10, "mod": 10,
    "^": 12,
}


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.index = 0

    def peek(self) -> Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def next(self) -> Token:
        token = self.peek()
        if token is None:
            raise ExpressionError("unexpected end of expression")
        self.index += 1
        return token

    def accept(self, text: str) -> bool:
        token = self.peek()
        if token is not None and token.text.casefold() == text.casefold():
            self.index += 1
            return True
        return False

    def expect(self, text: str) -> None:
        if not self.accept(text):
            token = self.peek()
            found = token.text if token else "<end>"
            raise ExpressionError(f"expected {text!r}, found {found!r}")

    # -- expression levels -------------------------------------------------

    def parse(self) -> Node:
        node = self.parse_binary(0)
        if self.peek() is not None:
            raise ExpressionError(
                f"unexpected trailing token {self.peek().text!r}"  # type: ignore[union-attr]
            )
        return node

    def parse_binary(self, minimum: int) -> Node:
        left = self.parse_unary()
        while True:
            token = self.peek()
            if token is None:
                return left
            operator = token.text.casefold()
            precedence = _BINARY_PRECEDENCE.get(operator)
            if precedence is None or precedence < minimum:
                return left
            self.index += 1
            if operator == "between":
                low = self.parse_binary(_BINARY_PRECEDENCE["&"])
                self.expect("and")
                high = self.parse_binary(_BINARY_PRECEDENCE["&"])
                left = Node("between", "between", [left, low, high])
                continue
            if operator == "in":
                self.expect("(")
                items: list[Node] = []
                if self.peek() is not None and self.peek().text != ")":  # type: ignore[union-attr]
                    items.append(self.parse_binary(0))
                    while self.accept(","):
                        items.append(self.parse_binary(0))
                self.expect(")")
                left = Node("in", "in", [left, *items])
                continue
            if operator == "is":
                negated = self.accept("not")
                self.expect("null")
                left = Node("is_null", "is not null" if negated else "is null", [left])
                continue
            right = self.parse_binary(precedence + 1)
            left = Node("binary", operator, [left, right])

    def parse_unary(self) -> Node:
        token = self.peek()
        if token is None:
            raise ExpressionError("unexpected end of expression")
        if token.text.casefold() == "not":
            self.index += 1
            return Node("unary", "not", [self.parse_unary()])
        if token.text in {"-", "+"}:
            self.index += 1
            return Node("unary", token.text, [self.parse_unary()])
        return self.parse_postfix()

    def parse_postfix(self) -> Node:
        node = self.parse_primary()
        while True:
            token = self.peek()
            if token is None:
                return node
            if token.text in {".", "!"}:
                self.index += 1
                member = self.next()
                name = _clean_identifier(member.text)
                node = Node(
                    "member",
                    "!" if token.text == "!" else ".",
                    [node, Node("name", name)],
                )
                continue
            if token.text == "(" and node.kind in {"name", "member"}:
                self.index += 1
                args: list[Node] = []
                if self.peek() is not None and self.peek().text != ")":  # type: ignore[union-attr]
                    args.append(self.parse_binary(0))
                    while self.accept(","):
                        args.append(self.parse_binary(0))
                self.expect(")")
                node = Node("call", _flatten_path(node), [node, *args])
                continue
            return node

    def parse_primary(self) -> Node:
        token = self.next()
        if token.text == "(":
            inner = self.parse_binary(0)
            self.expect(")")
            return Node("paren", None, [inner])
        if token.kind == "number":
            return Node("number", token.text)
        if token.kind == "string":
            quote = token.text[0]
            body = token.text[1:-1].replace(quote * 2, quote)
            return Node("string", body)
        if token.kind == "date":
            return Node("date", token.text[1:-1])
        if token.kind == "bracket":
            return Node("name", token.text[1:-1])
        if token.kind == "keyword":
            folded = token.text.casefold()
            if folded in {"true", "false"}:
                return Node("boolean", folded)
            if folded == "null":
                return Node("null", "null")
            raise ExpressionError(f"unexpected keyword {token.text!r}")
        if token.kind == "name":
            return Node("name", token.text)
        if token.text == "*":
            return Node("star", "*")
        raise ExpressionError(f"unexpected token {token.text!r}")


def _clean_identifier(text: str) -> str:
    if text.startswith("[") and text.endswith("]"):
        return text[1:-1]
    return text


def _flatten_path(node: Node) -> str:
    if node.kind == "name":
        return str(node.value)
    if node.kind == "member":
        return _flatten_path(node.children[0]) + "." + str(node.children[1].value)
    return "?"


# --------------------------------------------------------------------------
# Function support registry
# --------------------------------------------------------------------------
#
# ``sql`` entries translate directly.  ``rewrite`` entries need a structural
# rewrite that this module performs.  ``runtime`` entries have no portable SQL
# equivalent and must be reported, never silently dropped.

FUNCTION_SUPPORT: dict[str, dict[str, str]] = {}


def _register(names: str, support: str, target: str = "", note: str = "") -> None:
    for name in names.split():
        FUNCTION_SUPPORT[name.casefold()] = {
            "support": support,
            "target": target or name.upper(),
            "note": note,
        }


_register("abs", "sql", "ABS")
_register("sgn", "sql", "SIGN")
_register("sqr", "sql", "SQRT")
_register("exp", "sql", "EXP")
_register("log", "sql", "LN")
_register("int fix", "rewrite", "FLOOR", "Int/Fix differ for negatives; FLOOR/TRUNC is chosen per sign")
_register("round", "sql", "ROUND")
_register("len", "sql", "CHAR_LENGTH")
_register("ucase", "sql", "UPPER")
_register("lcase", "sql", "LOWER")
_register("trim", "sql", "TRIM")
_register("ltrim", "sql", "LTRIM")
_register("rtrim", "sql", "RTRIM")
_register("left", "sql", "LEFT")
_register("right", "sql", "RIGHT")
_register("mid", "rewrite", "SUBSTRING", "Access Mid() is 1-based; maps to SUBSTRING(x FROM p FOR n)")
_register("instr", "rewrite", "POSITION", "argument order differs from POSITION")
_register("replace", "sql", "REPLACE")
_register("string", "runtime", "", "repeats a character; no single portable SQL equivalent")
_register("space", "rewrite", "REPEAT", "")
_register("asc", "sql", "ASCII")
_register("chr", "sql", "CHR")
_register("nz", "rewrite", "COALESCE", "Nz(x, y) becomes COALESCE(x, y); Nz(x) becomes COALESCE(x, '')/0 by column type")
_register("iif", "rewrite", "CASE", "IIf(c, a, b) becomes CASE WHEN c THEN a ELSE b END")
_register("switch", "rewrite", "CASE", "Switch() becomes a searched CASE expression")
_register("choose", "rewrite", "CASE", "Choose() becomes a CASE over the index")
_register("isnull", "rewrite", "IS NULL", "IsNull(x) becomes x IS NULL")
_register("isnumeric isdate iserror isempty isobject isarray", "runtime", "", "VBA type test with no SQL equivalent")
_register("date", "sql", "CURRENT_DATE")
_register("now", "sql", "CURRENT_TIMESTAMP")
_register("time", "sql", "CURRENT_TIME")
_register("year", "rewrite", "EXTRACT", "Year(x) becomes EXTRACT(YEAR FROM x)")
_register("month", "rewrite", "EXTRACT", "Month(x) becomes EXTRACT(MONTH FROM x)")
_register("day", "rewrite", "EXTRACT", "Day(x) becomes EXTRACT(DAY FROM x)")
_register("hour minute second", "rewrite", "EXTRACT", "")
_register("weekday datepart datediff dateadd dateserial timeserial datevalue timevalue",
          "runtime", "", "Access date arithmetic uses Access interval codes; the target dialect must be chosen explicitly")
_register("format", "runtime", "", "Format() applies an Access/VBA display picture that has no portable SQL equivalent")
_register("cint clng cdbl csng ccur cstr cdate cbool cbyte cdec cvar",
          "rewrite", "CAST", "VBA conversion becomes CAST(); rounding rules differ")
_register("val", "rewrite", "CAST", "")
_register("str", "rewrite", "CAST", "")
_register("sum avg min max count first last stdev stdevp var varp",
          "sql", "", "SQL aggregate")
_register("dlookup dcount dsum davg dmin dmax dfirst dlast dstdev dvar dlookup",
          "runtime", "", "domain aggregate; becomes a correlated subquery only after its criteria string is resolved")
_register("eval", "runtime", "", "evaluates an arbitrary Access expression at run time")
_register("environ command shell createobject getobject",
          "runtime", "", "host/environment access; cannot exist inside the target database")
_register("currentuser currentproject currentdb codedb application doevents",
          "runtime", "", "Access application object model")
_register("msgbox inputbox", "runtime", "", "interactive dialog")
_register("partition", "runtime", "", "")
_register("rnd randomize", "runtime", "", "non-deterministic")


_UI_ROOTS = {"forms", "reports", "form", "report", "me", "screen"}

#: Roots that address the Access application object model rather than data.
#: A reference through one of these cannot become a column or a parameter; the
#: target system has no equivalent object to read.
_APP_ROOTS = {
    "currentproject", "currentdb", "codeproject", "codedb", "docmd",
    "application", "dbengine", "vba", "access", "sysvar", "tempvars",
}


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


@dataclass
class ExpressionAnalysis:
    """The result of analysing one Access expression."""

    source: str
    parsed: bool
    ast: dict[str, Any] | None
    sql: str | None
    translatable: bool
    identifiers: list[str]
    functions: list[str]
    unsupported: list[dict[str, str]]
    ui_references: list[str]
    parameters: list[str]
    parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "parsed": self.parsed,
            "sql": self.sql,
            "translatable": self.translatable,
            "identifiers": self.identifiers,
            "functions": self.functions,
            "unsupported": self.unsupported,
            "ui_references": self.ui_references,
            "parameters": self.parameters,
            "parse_error": self.parse_error,
        }


class _Renderer:
    """Renders an AST as target-neutral SQL, recording what it cannot carry."""

    def __init__(self, known_functions: set[str] | None = None):
        self.unsupported: list[dict[str, str]] = []
        self.identifiers: list[str] = []
        self.functions: list[str] = []
        self.ui_references: list[str] = []
        self.parameters: list[str] = []
        self.known_functions = {n.casefold() for n in (known_functions or set())}

    def note(self, code: str, detail: str, note: str = "") -> None:
        entry = {"reason_code": code, "detail": detail, "note": note}
        if entry not in self.unsupported:
            self.unsupported.append(entry)

    def render(self, node: Node) -> str:
        method = getattr(self, f"_r_{node.kind}", None)
        if method is None:
            self.note("EXPRESSION_NODE_NOT_TRANSLATED", node.kind)
            return "/* untranslated */"
        return method(node)

    # -- leaves ----------------------------------------------------------

    def _r_number(self, node: Node) -> str:
        return str(node.value)

    def _r_string(self, node: Node) -> str:
        escaped = str(node.value).replace("'", "''")
        return f"'{escaped}'"

    def _r_boolean(self, node: Node) -> str:
        return "TRUE" if node.value == "true" else "FALSE"

    def _r_null(self, node: Node) -> str:
        return "NULL"

    def _r_date(self, node: Node) -> str:
        return f"DATE '{node.value}'"

    def _r_star(self, node: Node) -> str:
        return "*"

    def _r_name(self, node: Node) -> str:
        name = str(node.value)
        if name not in self.identifiers:
            self.identifiers.append(name)
        return _quote_identifier(name)

    def _r_paren(self, node: Node) -> str:
        return f"({self.render(node.children[0])})"

    # -- composites ------------------------------------------------------

    def _r_member(self, node: Node) -> str:
        path = _flatten_path(node)
        root = path.split(".")[0].casefold()
        if root in _APP_ROOTS:
            self.note(
                "ACCESS_OBJECT_MODEL_REFERENCE",
                path,
                "reads Access application state rather than data; the target "
                "system has no equivalent object to read",
            )
            if path not in self.ui_references:
                self.ui_references.append(path)
            return f"/* {path} */ NULL"
        if root in _UI_ROOTS:
            if path not in self.ui_references:
                self.ui_references.append(path)
            parameter = ":" + re.sub(r"[^A-Za-z0-9_]", "_", path)
            if parameter not in self.parameters:
                self.parameters.append(parameter)
            self.note(
                "UI_REFERENCE_BECOMES_PARAMETER",
                path,
                "an Access form/report reference is not a column; it is emitted "
                "as a bind parameter the target application must supply",
            )
            return parameter
        recorded = path[:-2] if path.endswith(".*") else path
        if recorded not in self.identifiers:
            self.identifiers.append(recorded)
        return ".".join(_quote_identifier(p) for p in path.split("."))

    def _r_unary(self, node: Node) -> str:
        operator = str(node.value)
        inner = self.render(node.children[0])
        if operator == "not":
            return f"NOT {inner}"
        return f"{operator}{inner}"

    def _r_is_null(self, node: Node) -> str:
        return f"{self.render(node.children[0])} {str(node.value).upper()}"

    def _r_between(self, node: Node) -> str:
        target, low, high = (self.render(c) for c in node.children)
        return f"{target} BETWEEN {low} AND {high}"

    def _r_in(self, node: Node) -> str:
        target = self.render(node.children[0])
        items = ", ".join(self.render(c) for c in node.children[1:])
        return f"{target} IN ({items})"

    def _r_binary(self, node: Node) -> str:
        operator = str(node.value)
        left = self.render(node.children[0])
        right = self.render(node.children[1])
        if operator == "&":
            return f"{left} || {right}"
        if operator == "\\":
            self.note(
                "INTEGER_DIVISION_SEMANTICS",
                "\\",
                "VBA integer division truncates toward zero after rounding "
                "both operands; the target dialect must confirm this",
            )
            return f"TRUNC({left} / {right})"
        if operator == "mod":
            return f"MOD({left}, {right})"
        if operator == "^":
            return f"POWER({left}, {right})"
        if operator == "like":
            self.note(
                "LIKE_WILDCARD_DIALECT",
                "Like",
                "Access LIKE uses * and ? wildcards; the emitted SQL uses "
                "% and _ and the pattern is rewritten when it is a literal",
            )
            right = _rewrite_like_pattern(node.children[1], right)
            return f"{left} LIKE {right}"
        if operator in {"and", "or", "xor", "eqv", "imp"}:
            if operator in {"eqv", "imp"}:
                self.note(
                    "LOGICAL_OPERATOR_NOT_IN_SQL",
                    operator.upper(),
                    "VBA logical equivalence/implication has no SQL operator",
                )
                return f"/* {operator.upper()} */ ({left})"
            if operator == "xor":
                return f"(({left}) <> ({right}))"
            return f"{left} {operator.upper()} {right}"
        return f"{left} {operator} {right}"

    def _r_call(self, node: Node) -> str:
        name = str(node.value)
        simple = name.split(".")[-1]
        folded = simple.casefold()
        if simple not in self.functions:
            self.functions.append(simple)
        args = [self.render(child) for child in node.children[1:]]
        entry = FUNCTION_SUPPORT.get(folded)
        if entry is None and "." in name:
            root = name.split(".")[0].casefold()
            self.note(
                "OBJECT_METHOD_CALL",
                name,
                "a method call on an Access object"
                + (" (application object model)" if root in _APP_ROOTS or root in _UI_ROOTS else ""),
            )
            return f"{name}({', '.join(args)})"
        if entry is None:
            if folded in self.known_functions:
                self.note(
                    "USER_DEFINED_VBA_FUNCTION",
                    simple,
                    "defined by this database's own VBA project; it must be "
                    "ported before the expression can run in the target",
                )
            else:
                self.note(
                    "UNKNOWN_FUNCTION",
                    simple,
                    "not a recognised Access built-in and not found in this "
                    "database's VBA project",
                )
            return f"{simple}({', '.join(args)})"
        support = entry["support"]
        if support == "sql":
            target = entry["target"] or simple.upper()
            if not args and target in {
                "CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_TIME"
            }:
                return target
            return f"{target}({', '.join(args)})"
        if support == "rewrite":
            return self._rewrite(folded, simple, args, entry)
        self.note("ACCESS_RUNTIME_FUNCTION", simple, entry["note"])
        return f"{simple}({', '.join(args)})"

    def _rewrite(
        self, folded: str, simple: str, args: list[str], entry: dict[str, str]
    ) -> str:
        if folded == "iif" and len(args) == 3:
            return f"CASE WHEN {args[0]} THEN {args[1]} ELSE {args[2]} END"
        if folded == "iif" and len(args) == 2:
            return f"CASE WHEN {args[0]} THEN {args[1]} ELSE NULL END"
        if folded == "isnull" and len(args) == 1:
            return f"({args[0]} IS NULL)"
        if folded == "nz":
            if len(args) == 2:
                return f"COALESCE({args[0]}, {args[1]})"
            self.note(
                "NZ_DEFAULT_DEPENDS_ON_TYPE",
                "Nz",
                "single argument Nz() returns '' for text and 0 for numbers; "
                "the column type decides and must be confirmed",
            )
            return f"COALESCE({args[0]}, NULL)"
        if folded in {"year", "month", "day", "hour", "minute", "second"} and len(args) == 1:
            return f"EXTRACT({folded.upper()} FROM {args[0]})"
        if folded == "mid" and len(args) >= 2:
            if len(args) == 3:
                return f"SUBSTRING({args[0]} FROM {args[1]} FOR {args[2]})"
            return f"SUBSTRING({args[0]} FROM {args[1]})"
        if folded == "instr" and len(args) == 2:
            return f"POSITION({args[1]} IN {args[0]})"
        if folded == "space" and len(args) == 1:
            return f"REPEAT(' ', {args[0]})"
        if folded in {"int", "fix"} and len(args) == 1:
            self.note("INT_FIX_ROUNDING", simple, entry["note"])
            return ("FLOOR" if folded == "int" else "TRUNC") + f"({args[0]})"
        if folded == "switch" and len(args) >= 2 and len(args) % 2 == 0:
            pairs = " ".join(
                f"WHEN {args[i]} THEN {args[i + 1]}" for i in range(0, len(args), 2)
            )
            return f"CASE {pairs} END"
        if folded in {"cint", "clng", "cbyte"}:
            return f"CAST({args[0]} AS INTEGER)" if args else "NULL"
        if folded in {"cdbl", "csng", "val"}:
            return f"CAST({args[0]} AS DOUBLE PRECISION)" if args else "NULL"
        if folded == "ccur" or folded == "cdec":
            return f"CAST({args[0]} AS DECIMAL(19,4))" if args else "NULL"
        if folded in {"cstr", "str"}:
            return f"CAST({args[0]} AS VARCHAR)" if args else "NULL"
        if folded == "cdate":
            return f"CAST({args[0]} AS TIMESTAMP)" if args else "NULL"
        if folded == "cbool":
            return f"CAST({args[0]} AS BOOLEAN)" if args else "NULL"
        self.note(
            "FUNCTION_REWRITE_INCOMPLETE",
            simple,
            entry["note"] or "argument shape is not one this converter rewrites",
        )
        return f"{simple}({', '.join(args)})"


def _rewrite_like_pattern(node: Node, rendered: str) -> str:
    if node.kind != "string":
        return rendered
    pattern = str(node.value)
    converted = (
        pattern.replace("%", r"\%")
        .replace("_", r"\_")
        .replace("*", "%")
        .replace("?", "_")
    )
    return "'" + converted.replace("'", "''") + "'"


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_identifier(name: str) -> str:
    # ``Orders.*`` is a whole-row selector, not a column called "*".
    if name == "*" or _SAFE_IDENTIFIER.match(name):
        return name
    return '"' + name.replace('"', '""') + '"'


#: Reason codes that describe a difference the user must decide about but that
#: do not by themselves make an expression untranslatable.
_ADVISORY_CODES = {
    "LIKE_WILDCARD_DIALECT",
    "UI_REFERENCE_BECOMES_PARAMETER",
    "INT_FIX_ROUNDING",
    "NZ_DEFAULT_DEPENDS_ON_TYPE",
    "INTEGER_DIVISION_SEMANTICS",
}


def analyze_expression(
    source: str, known_functions: set[str] | None = None
) -> ExpressionAnalysis:
    """Parse and translate one Access expression."""
    text = (source or "").strip()
    if not text:
        return ExpressionAnalysis(
            source=source or "",
            parsed=True,
            ast=None,
            sql=None,
            translatable=True,
            identifiers=[],
            functions=[],
            unsupported=[],
            ui_references=[],
            parameters=[],
        )
    try:
        tokens = tokenize(text)
        ast = _Parser(tokens).parse()
    except ExpressionError as error:
        return ExpressionAnalysis(
            source=text,
            parsed=False,
            ast=None,
            sql=None,
            translatable=False,
            identifiers=[],
            functions=[],
            unsupported=[
                {
                    "reason_code": "EXPRESSION_PARSE_FAILED",
                    "detail": text[:200],
                    "note": str(error),
                }
            ],
            ui_references=[],
            parameters=[],
            parse_error=str(error),
        )
    renderer = _Renderer(known_functions)
    sql = renderer.render(ast)
    blocking = [
        item
        for item in renderer.unsupported
        if item["reason_code"] not in _ADVISORY_CODES
    ]
    return ExpressionAnalysis(
        source=text,
        parsed=True,
        ast=ast.to_dict(),
        sql=sql,
        translatable=not blocking,
        identifiers=renderer.identifiers,
        functions=renderer.functions,
        unsupported=renderer.unsupported,
        ui_references=renderer.ui_references,
        parameters=renderer.parameters,
    )
