"""Translate the Access table schema (``*.xsd``) into a portable table model.

Access exports each table as an XML Schema annotated with ``od:`` metadata:
the Jet field type, the auto-number flag, the nullability, every index, and a
long tail of field properties.  Some of those properties are pure display
(``ColumnWidth``, ``TextAlign``); others carry real semantics that a target
database must reproduce (``DefaultValue``, ``ValidationRule``, lookup row
sources).  They are separated here rather than lumped together.

The XML is parsed with the standard library only, with document type and
entity declarations refused before parsing starts.
"""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree

from ._expr import analyze_expression

__all__ = ["translate_table", "translate_relationships", "JET_TYPE_MAP"]

_XSD = "{http://www.w3.org/2001/XMLSchema}"
_OD = "{urn:schemas-microsoft-com:officedata}"

#: Jet/ACE field type to a target-neutral SQL type.  ``None`` means the type
#: has no portable equivalent and must be reported instead of guessed.
JET_TYPE_MAP: dict[str, str | None] = {
    "autonumber": "INTEGER",
    "byte": "SMALLINT",
    "integer": "SMALLINT",
    "longinteger": "INTEGER",
    "single": "REAL",
    "double": "DOUBLE PRECISION",
    "currency": "DECIMAL(19,4)",
    "decimal": "DECIMAL",
    "datetime": "TIMESTAMP",
    "dateTime": "TIMESTAMP",
    "text": "VARCHAR",
    "memo": "CLOB",
    "yesno": "BOOLEAN",
    "guid": "CHAR(36)",
    "replicationid": "CHAR(36)",
    "binary": "BINARY",
    "oleobject": None,
    "attachment": None,
    "hyperlink": "VARCHAR",
    "calculated": None,
    "multivalued": None,
    # ACE writes attachment and multi-valued lookup fields as "complex": the
    # values live in a hidden child table, not in the column itself.
    "complex": None,
}

#: Field properties that only affect how Access draws a datasheet.
_DISPLAY_ONLY = {
    "ColumnWidth", "ColumnOrder", "ColumnHidden", "TextAlign", "GUID",
    "AggregateType", "ResultType", "CurrencyLCID", "DecimalPlaces",
    "IMEMode", "IMESentenceMode", "ShowDatePicker", "DisplayControl",
    "ColumnWidths", "ColumnCount", "ColumnHeads", "ListRows", "ListWidth",
    "LimitToList", "AllowValueListEdits", "ShowOnlyRowSourceValues",
    "UnicodeCompression", "AppendOnly", "TextFormat",
}

#: Field properties that carry behaviour a target database has to reproduce.
_SEMANTIC_PROPERTIES = {
    "DefaultValue", "ValidationRule", "ValidationText", "Required",
    "AllowZeroLength", "Description", "Format", "InputMask", "Caption",
    "RowSourceType", "RowSource", "BoundColumn", "Expression",
}


_PARTIAL_PREDICATE = re.compile(
    r"^\s*(<>|<=|>=|=|<|>|Between\b|Like\b|In\b|Is\b|Not\b)", re.IGNORECASE
)


def _complete_predicate(rule: str, column: str) -> str:
    """Give an Access field validation rule its implicit left-hand side.

    Access lets a field rule be written as a bare comparison (``>0``); the
    field itself is the implied subject.  Without restoring it the rule is not
    an expression at all and would be reported as a parse failure.
    """
    if _PARTIAL_PREDICATE.match(rule):
        return f"[{column}] {rule.strip()}"
    return rule


def _refuse_unsafe(text: str) -> None:
    lowered = text.casefold()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ValueError("XML document type and entity declarations are refused")


def _field_properties(element: ElementTree.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for appinfo in element.iter(f"{_XSD}appinfo"):
        for prop in appinfo.findall(f"{_OD}fieldProperty"):
            name = prop.get("name")
            if name:
                result[name] = prop.get("value", "")
    return result


def _max_length(element: ElementTree.Element) -> int | None:
    for restriction in element.iter(f"{_XSD}restriction"):
        for limit in restriction.findall(f"{_XSD}maxLength"):
            value = limit.get("value")
            if value and value.isdigit():
                return int(value)
    return None


def translate_table(xsd_text: str, name: str) -> dict[str, Any]:
    """Translate one table schema into a portable table definition."""
    _refuse_unsafe(xsd_text)
    root = ElementTree.fromstring(xsd_text)
    unsupported: list[dict[str, str]] = []

    table_element = None
    for element in root.findall(f"{_XSD}element"):
        if element.get("name") == name:
            table_element = element
            break
    if table_element is None:
        candidates = [
            element
            for element in root.findall(f"{_XSD}element")
            if element.get("name") != "dataroot"
        ]
        table_element = candidates[0] if candidates else None
    if table_element is None:
        return {
            "kind": "table",
            "name": name,
            "columns": [],
            "primary_key": [],
            "indexes": [],
            "translated": False,
            "unsupported": [
                {
                    "reason_code": "TABLE_ELEMENT_NOT_FOUND",
                    "detail": name,
                    "note": "the schema holds no element for this table",
                }
            ],
        }

    indexes: list[dict[str, Any]] = []
    primary_key: list[str] = []
    for appinfo in table_element.iter(f"{_XSD}appinfo"):
        for index in appinfo.findall(f"{_OD}index"):
            columns = [
                part for part in (index.get("index-key") or "").split() if part
            ]
            entry = {
                "name": index.get("index-name"),
                "columns": columns,
                "unique": index.get("unique") == "yes",
                "primary": index.get("primary") == "yes",
                "order": index.get("order", "asc"),
            }
            indexes.append(entry)
            if entry["primary"] and not primary_key:
                primary_key = columns
        break

    columns: list[dict[str, Any]] = []
    sequence = table_element.find(f"{_XSD}complexType/{_XSD}sequence")
    if sequence is not None:
        for field in sequence.findall(f"{_XSD}element"):
            column_name = field.get("name")
            if not column_name:
                continue
            jet_type = field.get(f"{_OD}jetType") or ""
            sql_type = JET_TYPE_MAP.get(jet_type, "MISSING")
            length = _max_length(field)
            if sql_type == "VARCHAR" and length:
                rendered_type = f"VARCHAR({length})"
            elif sql_type == "MISSING":
                rendered_type = None
                unsupported.append(
                    {
                        "reason_code": "JET_FIELD_TYPE_NOT_MAPPED",
                        "detail": f"{column_name}: {jet_type}",
                        "note": "unknown Jet/ACE field type",
                    }
                )
            elif sql_type is None:
                rendered_type = None
                unsupported.append(
                    {
                        "reason_code": "FIELD_TYPE_HAS_NO_PORTABLE_EQUIVALENT",
                        "detail": f"{column_name}: {jet_type}",
                        "note": "Access stores this field in a form no plain "
                        "SQL column can hold (OLE object, attachment, "
                        "multi-valued or calculated field)",
                    }
                )
            else:
                rendered_type = sql_type

            properties = _field_properties(field)
            semantics: dict[str, Any] = {}
            for key, value in properties.items():
                if key in _DISPLAY_ONLY:
                    continue
                if key in _SEMANTIC_PROPERTIES:
                    semantics[key] = value
                else:
                    semantics.setdefault("_other", {})[key] = value

            default = semantics.get("DefaultValue")
            default_sql = None
            if default:
                analysis = analyze_expression(default)
                default_sql = analysis.sql
                for item in analysis.unsupported:
                    if item not in unsupported:
                        unsupported.append(item)
            validation = semantics.get("ValidationRule")
            validation_sql = None
            if validation:
                analysis = analyze_expression(
                    _complete_predicate(validation, column_name)
                )
                validation_sql = analysis.sql
                for item in analysis.unsupported:
                    if item not in unsupported:
                        unsupported.append(item)

            lookup = None
            if semantics.get("RowSourceType") or semantics.get("RowSource"):
                lookup = {
                    "row_source_type": semantics.get("RowSourceType"),
                    "row_source": semantics.get("RowSource"),
                    "bound_column": semantics.get("BoundColumn"),
                }

            columns.append(
                {
                    "name": column_name,
                    "jet_type": jet_type,
                    "sql_type": rendered_type,
                    "max_length": length,
                    "nullable": field.get(f"{_OD}nonNullable") != "yes",
                    "autonumber": field.get(f"{_OD}autoUnique") == "yes",
                    "required": semantics.get("Required") == "1",
                    "allow_zero_length": semantics.get("AllowZeroLength") == "1",
                    "description": semantics.get("Description"),
                    "caption": semantics.get("Caption"),
                    "format": semantics.get("Format"),
                    "input_mask": semantics.get("InputMask"),
                    "default": {"source": default, "sql": default_sql} if default else None,
                    "validation": (
                        {
                            "source": validation,
                            "sql": validation_sql,
                            "message": semantics.get("ValidationText"),
                        }
                        if validation
                        else None
                    ),
                    "lookup": lookup,
                    "translated": rendered_type is not None,
                }
            )

    ddl = _render_create_table(name, columns, primary_key)
    return {
        "kind": "table",
        "name": name,
        "columns": columns,
        "primary_key": primary_key,
        "indexes": indexes,
        "ddl": ddl,
        "unsupported": unsupported,
        "translated": bool(columns) and all(c["translated"] for c in columns),
    }


def _quote(name: str) -> str:
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _render_create_table(
    name: str, columns: list[dict[str, Any]], primary_key: list[str]
) -> str | None:
    if not columns:
        return None
    lines = []
    for column in columns:
        sql_type = column["sql_type"] or "/* type not translated */"
        piece = f"  {_quote(column['name'])} {sql_type}"
        if not column["nullable"]:
            piece += " NOT NULL"
        if column["default"] and column["default"]["sql"]:
            piece += f" DEFAULT {column['default']['sql']}"
        lines.append(piece)
    if primary_key:
        lines.append(
            "  PRIMARY KEY (" + ", ".join(_quote(c) for c in primary_key) + ")"
        )
    for column in columns:
        if column["validation"] and column["validation"]["sql"]:
            lines.append(f"  CHECK ({column['validation']['sql']})")
    return f"CREATE TABLE {_quote(name)} (\n" + ",\n".join(lines) + "\n);"


def translate_relationships(xml_text: str) -> list[dict[str, Any]]:
    """Translate ``relationships.xml`` into foreign-key constraints."""
    _refuse_unsafe(xml_text)
    root = ElementTree.fromstring(xml_text)
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in root:
        def value(tag: str) -> str:
            element = row.find(tag)
            return (element.text or "").strip() if element is not None else ""

        name = value("szRelationship")
        child = value("szObject")
        parent = value("szReferencedObject")
        if not (child and parent):
            continue
        key = (name, child, parent)
        entry = grouped.setdefault(
            key,
            {
                "name": name,
                "child_table": child,
                "parent_table": parent,
                "child_columns": [],
                "parent_columns": [],
                "flags": value("grbit"),
            },
        )
        entry["child_columns"].append(value("szColumn"))
        entry["parent_columns"].append(value("szReferencedColumn"))
    result = []
    for entry in grouped.values():
        flags = int(entry["flags"]) if entry["flags"].lstrip("-").isdigit() else 0
        entry["enforced"] = not bool(flags & 0x2)
        entry["cascade_update"] = bool(flags & 0x100)
        entry["cascade_delete"] = bool(flags & 0x1000)
        entry["ddl"] = (
            f"ALTER TABLE {_quote(entry['child_table'])} ADD CONSTRAINT "
            f"{_quote(entry['name'] or 'fk')} FOREIGN KEY ("
            + ", ".join(_quote(c) for c in entry["child_columns"])
            + f") REFERENCES {_quote(entry['parent_table'])} ("
            + ", ".join(_quote(c) for c in entry["parent_columns"])
            + ")"
            + (" ON UPDATE CASCADE" if entry["cascade_update"] else "")
            + (" ON DELETE CASCADE" if entry["cascade_delete"] else "")
            + ";"
        )
        result.append(entry)
    result.sort(key=lambda item: (item["child_table"], item["name"]))
    return result
