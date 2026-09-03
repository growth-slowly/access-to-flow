"""Translate Access query definitions into a target-neutral relational model.

Two shapes exist in the ``SaveAsText`` query format:

*Design-grid queries* carry ``Operation``/``Option`` plus ``InputTables``,
``OutputColumns``, ``Joins``, ``Groups``, ``OrderBy`` and a ``Where``
expression.  These are reconstructed field by field and rendered as SQL.

*SQL-text queries* (union, pass-through, data-definition and anything the
design grid cannot hold) carry ``dbMemo "SQL"``.  Access already stored the
statement as text, so it is preserved and classified, never re-derived.

No query is ever executed and no database engine is contacted.
"""

from __future__ import annotations

import re
from typing import Any

from ._expr import analyze_expression
from ._textformat import Block

__all__ = ["translate_query", "QUERY_OPERATION_CODES"]


#: ``Operation`` codes seen in the ``SaveAsText`` query format.
#:
#: ``verified`` means the code was observed in this project's sample corpus and
#: cross-checked against the query's own structure.  Any other code is carried
#: through unmapped rather than guessed at, because mislabelling an append
#: query as a select query would silently lose a write.
QUERY_OPERATION_CODES: dict[int, dict[str, str]] = {
    1: {"query_type": "select", "confidence": "verified"},
}

_JOIN_FLAGS = {
    "1": "INNER JOIN",
    "2": "LEFT OUTER JOIN",
    "3": "RIGHT OUTER JOIN",
}

_OPTION_DISTINCT = 2
_OPTION_TOP = 16

_SQL_LEADING = re.compile(r"^\s*(?:\(\s*)?([A-Za-z_]+)")


def _identifier(name: str) -> str:
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return name
    return '"' + name.replace('"', '""') + '"'


#: Keys that Access writes *after* the value they qualify.  Everything else
#: in a paired block is written before it.  The two orders really do differ
#: between blocks: an OutputColumns ``Alias`` precedes its ``Expression``,
#: while an InputTables ``Alias`` follows its ``Name``.
_TRAILING_KEYS = frozenset({"Flag", "GroupLevel"})


def _paired(
    block: Block | None,
    primary: str,
    trailing: frozenset[str] = _TRAILING_KEYS,
) -> list[dict[str, str]]:
    """Read a block whose properties qualify a repeated primary property."""
    if block is None:
        return []
    rows: list[dict[str, str]] = []
    pending: dict[str, str] = {}
    for item in block.properties:
        key = item.key
        if key == primary:
            entry = {"value": item.value}
            entry.update(pending)
            pending = {}
            rows.append(entry)
        elif rows and key in trailing:
            rows[-1][key.lower()] = item.value
        else:
            pending[key.lower()] = item.value
    return rows


def _analyze(expression: str, known: set[str]) -> dict[str, Any]:
    return analyze_expression(expression, known).to_dict()


def _collect(target: list[dict[str, str]], analysis: dict[str, Any]) -> None:
    for item in analysis.get("unsupported", []):
        if item not in target:
            target.append(item)


def _build_from_clause(
    tables: list[dict[str, str]], joins: list[dict[str, Any]]
) -> tuple[str, list[dict[str, str]]]:
    """Assemble a FROM clause from Access's pairwise join list."""
    problems: list[dict[str, str]] = []
    if not tables:
        return "", problems
    # A table used twice carries an alias, and every join in the definition
    # refers to that alias rather than to the base table name.  The alias is
    # therefore the key, and the base name is only the thing being aliased.
    names = [row.get("alias") or row["name"] for row in tables]
    rendered = {
        (row.get("alias") or row["name"]): (
            _identifier(row["name"])
            + (f" AS {_identifier(row['alias'])}" if row.get("alias") else "")
        )
        for row in tables
    }
    remaining = list(joins)
    used: set[str] = set()
    clause_parts: list[str] = []
    while names:
        anchor = None
        for candidate in names:
            anchor = candidate
            break
        names.remove(anchor)
        used.add(anchor)
        clause = rendered.get(anchor, _identifier(anchor))
        progressed = True
        while progressed:
            progressed = False
            for join in list(remaining):
                left, right = join["left_table"], join["right_table"]
                if left in used and right not in used:
                    new, keep = right, left
                elif right in used and left not in used:
                    new, keep = left, right
                else:
                    continue
                keyword = _JOIN_FLAGS.get(join["flag"], "INNER JOIN")
                if keyword != "INNER JOIN" and new == join["left_table"]:
                    # The Access join direction is stated left-to-right; adding
                    # the left table second flips an outer join's meaning.
                    keyword = (
                        "RIGHT OUTER JOIN"
                        if keyword == "LEFT OUTER JOIN"
                        else "LEFT OUTER JOIN"
                    )
                target = rendered.get(new, _identifier(new))
                clause = f"{clause} {keyword} {target} ON {join['on_sql']}"
                used.add(new)
                if new in names:
                    names.remove(new)
                remaining.remove(join)
                progressed = True
        clause_parts.append(clause)
    if remaining:
        problems.append(
            {
                "reason_code": "JOIN_GRAPH_NOT_RESOLVED",
                "detail": ", ".join(
                    f"{j['left_table']}<->{j['right_table']}" for j in remaining
                ),
                "note": "a join references a table that is not in InputTables",
            }
        )
    if len(clause_parts) > 1:
        problems.append(
            {
                "reason_code": "IMPLICIT_CROSS_JOIN",
                "detail": " x ".join(clause_parts),
                "note": "the query's tables are not fully connected by joins, "
                "so the result is a Cartesian product",
            }
        )
    return ", ".join(clause_parts), problems


def _classify_sql_text(sql: str) -> str:
    match = _SQL_LEADING.match(sql or "")
    leading = (match.group(1) if match else "").casefold()
    if "union" in (sql or "").casefold() and leading == "select":
        return "union"
    return {
        "select": "select",
        "insert": "insert",
        "update": "update",
        "delete": "delete",
        "transform": "crosstab",
        "create": "data_definition",
        "alter": "data_definition",
        "drop": "data_definition",
    }.get(leading, "unknown")


_SQL_SOURCES = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def translate_query(root: Block, name: str, known_functions: set[str]) -> dict[str, Any]:
    """Translate one parsed query definition."""
    unsupported: list[dict[str, str]] = []
    properties = {
        item.key: item.value
        for item in root.properties
        if item.db_type is not None and not item.is_binary
    }

    sql_text = properties.get("SQL")
    if sql_text is not None:
        query_type = _classify_sql_text(sql_text)
        sources = sorted(
            {
                m.group(1).strip("[]")
                for m in _SQL_SOURCES.finditer(sql_text)
            }
        )
        connect = properties.get("Connect") or ""
        if connect:
            query_type = "pass_through"
            unsupported.append(
                {
                    "reason_code": "PASS_THROUGH_QUERY_TARGETS_EXTERNAL_SERVER",
                    "detail": connect,
                    "note": "the statement is sent verbatim to another server; "
                    "its dialect is that server's, not Access's",
                }
            )
        if query_type in {"union", "crosstab", "unknown", "data_definition"}:
            unsupported.append(
                {
                    "reason_code": "STORED_SQL_NOT_RE_PARSED",
                    "detail": query_type,
                    "note": "Access stored this query as SQL text; it is "
                    "preserved exactly and not re-derived into the "
                    "relational model",
                }
            )
        return {
            "kind": "query",
            "name": name,
            "representation": "stored_sql_text",
            "query_type": query_type,
            "sql": sql_text.strip(),
            "returns_records": properties.get("ReturnsRecords") == "-1",
            "dependencies": sources,
            "from": [],
            "joins": [],
            "columns": [],
            "where": None,
            "group_by": [],
            "having": None,
            "order_by": [],
            "distinct": False,
            "top": None,
            "parameters": [],
            "unsupported": unsupported,
            "translated": not unsupported,
        }

    operation_raw = root.get("Operation")
    operation = int(operation_raw) if (operation_raw or "").isdigit() else None
    mapping = QUERY_OPERATION_CODES.get(operation or -1)
    if mapping is None:
        query_type = "unknown"
        unsupported.append(
            {
                "reason_code": "QUERY_OPERATION_CODE_NOT_MAPPED",
                "detail": str(operation_raw),
                "note": "this converter only claims the Operation codes it has "
                "verified against real definitions; an unverified code is "
                "never guessed at because mislabelling an action query "
                "would silently drop a write",
            }
        )
    else:
        query_type = mapping["query_type"]

    option_raw = root.get("Option") or "0"
    option = int(option_raw) if option_raw.lstrip("-").isdigit() else 0
    distinct = bool(option & _OPTION_DISTINCT)
    top_rows = root.get("RowCount") if option & _OPTION_TOP else None
    leftover = option & ~(_OPTION_DISTINCT | _OPTION_TOP)
    if leftover:
        unsupported.append(
            {
                "reason_code": "QUERY_OPTION_BITS_NOT_MAPPED",
                "detail": str(leftover),
                "note": "unrecognised bits in the query's Option flags",
            }
        )

    def one(kind: str) -> Block | None:
        found = root.blocks(kind)
        return found[0] if found else None

    tables: list[dict[str, str]] = []
    for row in _paired(
        one("InputTables"), "Name", _TRAILING_KEYS | {"Alias"}
    ):
        entry = {"name": row["value"]}
        if row.get("alias"):
            entry["alias"] = row["alias"]
        tables.append(entry)

    columns: list[dict[str, Any]] = []
    for row in _paired(one("OutputColumns"), "Expression"):
        analysis = _analyze(row["value"], known_functions)
        _collect(unsupported, analysis)
        columns.append(
            {
                "expression": row["value"],
                "alias": row.get("alias"),
                "sql": analysis["sql"],
                "translated": analysis["translatable"],
                "functions": analysis["functions"],
                "identifiers": analysis["identifiers"],
            }
        )

    joins: list[dict[str, Any]] = []
    join_block = one("Joins")
    if join_block is not None:
        current: dict[str, Any] = {}
        for item in join_block.properties:
            if item.key == "LeftTable":
                current = {"left_table": item.value}
            elif item.key == "RightTable":
                current["right_table"] = item.value
            elif item.key == "Expression":
                analysis = _analyze(item.value, known_functions)
                _collect(unsupported, analysis)
                current["on"] = item.value
                current["on_sql"] = analysis["sql"] or item.value
            elif item.key == "Flag":
                current["flag"] = item.value
                current["join_type"] = _JOIN_FLAGS.get(item.value, "INNER JOIN")
                if item.value not in _JOIN_FLAGS:
                    unsupported.append(
                        {
                            "reason_code": "JOIN_FLAG_NOT_MAPPED",
                            "detail": item.value,
                            "note": "unrecognised Access join flag",
                        }
                    )
                joins.append(current)
                current = {}

    where_source = root.get("Where")
    where_sql = None
    if where_source:
        analysis = _analyze(where_source, known_functions)
        _collect(unsupported, analysis)
        where_sql = analysis["sql"]

    having_source = root.get("Having")
    having_sql = None
    if having_source:
        analysis = _analyze(having_source, known_functions)
        _collect(unsupported, analysis)
        having_sql = analysis["sql"]

    group_by: list[dict[str, Any]] = []
    for row in _paired(one("Groups"), "Expression"):
        analysis = _analyze(row["value"], known_functions)
        _collect(unsupported, analysis)
        group_by.append(
            {
                "expression": row["value"],
                "sql": analysis["sql"],
                "level": row.get("grouplevel"),
            }
        )

    order_by: list[dict[str, Any]] = []
    for row in _paired(one("OrderBy"), "Expression"):
        analysis = _analyze(row["value"], known_functions)
        _collect(unsupported, analysis)
        order_by.append(
            {
                "expression": row["value"],
                "sql": analysis["sql"],
                "direction": "DESC" if row.get("flag") == "1" else "ASC",
            }
        )

    from_clause, join_problems = _build_from_clause(tables, joins)
    unsupported.extend(join_problems)

    parameters: list[str] = []
    for bucket in (columns, group_by, order_by):
        for entry in bucket:
            for token in re.findall(r":[A-Za-z0-9_]+", entry.get("sql") or ""):
                if token not in parameters:
                    parameters.append(token)
    for text in (where_sql, having_sql):
        for token in re.findall(r":[A-Za-z0-9_]+", text or ""):
            if token not in parameters:
                parameters.append(token)

    sql = None
    if query_type == "select" and from_clause:
        select_items = []
        for column in columns:
            rendered = column["sql"] or column["expression"]
            if column.get("alias"):
                rendered = f"{rendered} AS {_identifier(column['alias'])}"
            select_items.append(rendered)
        if not select_items:
            select_items = ["*"]
        head = "SELECT"
        if distinct:
            head += " DISTINCT"
        if top_rows:
            head += f" TOP {top_rows}"
        lines = [f"{head} {', '.join(select_items)}", f"FROM {from_clause}"]
        if where_sql:
            lines.append(f"WHERE {where_sql}")
        if group_by:
            lines.append(
                "GROUP BY " + ", ".join(g["sql"] or g["expression"] for g in group_by)
            )
        if having_sql:
            lines.append(f"HAVING {having_sql}")
        if order_by:
            lines.append(
                "ORDER BY "
                + ", ".join(
                    f"{o['sql'] or o['expression']} {o['direction']}" for o in order_by
                )
            )
        sql = "\n".join(lines) + ";"
        if top_rows:
            unsupported.append(
                {
                    "reason_code": "TOP_CLAUSE_IS_DIALECT_SPECIFIC",
                    "detail": f"TOP {top_rows}",
                    "note": "row limiting is spelled differently per target "
                    "(FETCH FIRST, LIMIT, OBS=); the count is preserved",
                }
            )
    elif query_type != "select":
        unsupported.append(
            {
                "reason_code": "NON_SELECT_QUERY_SQL_NOT_GENERATED",
                "detail": query_type,
                "note": "the design-grid model was recovered but no statement "
                "is emitted for an operation this converter has not verified",
            }
        )

    blocking = [
        item
        for item in unsupported
        if item["reason_code"]
        not in {
            "LIKE_WILDCARD_DIALECT",
            "UI_REFERENCE_BECOMES_PARAMETER",
            "TOP_CLAUSE_IS_DIALECT_SPECIFIC",
            "INT_FIX_ROUNDING",
            "NZ_DEFAULT_DEPENDS_ON_TYPE",
            "INTEGER_DIVISION_SEMANTICS",
        }
    ]
    return {
        "kind": "query",
        "name": name,
        "representation": "design_grid",
        "query_type": query_type,
        "operation_code": operation,
        "sql": sql,
        "distinct": distinct,
        "top": top_rows,
        "from": tables,
        "joins": joins,
        "columns": columns,
        "where": {"source": where_source, "sql": where_sql} if where_source else None,
        "group_by": group_by,
        "having": {"source": having_source, "sql": having_sql} if having_source else None,
        "order_by": order_by,
        "dependencies": [row["name"] for row in tables],
        "parameters": parameters,
        "returns_records": properties.get("ReturnsRecords") == "-1",
        "unsupported": unsupported,
        "translated": sql is not None and not blocking,
    }
