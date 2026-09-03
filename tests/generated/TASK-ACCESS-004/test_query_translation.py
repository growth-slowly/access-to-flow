"""Design-grid queries become SQL; stored SQL is preserved, never re-derived."""

from __future__ import annotations

import unittest

from converter.semantics._querydef import translate_query
from converter.semantics._textformat import parse_access_text


def build(text: str, known: set[str] | None = None) -> dict:
    return translate_query(parse_access_text(text), "q", known or set())


SIMPLE = """Operation =1
Option =0
Begin InputTables
    Name ="Companies"
End
Begin OutputColumns
    Expression ="Companies.CompanyID"
    Alias ="Label"
    Expression ="Companies.Name"
End
Begin OrderBy
    Expression ="Companies.Name"
    Flag =0
End
"""

SELF_JOIN = """Operation =1
Option =0
Begin InputTables
    Name ="Orders"
    Name ="Employees"
    Name ="Employees"
    Alias ="Approver"
End
Begin OutputColumns
    Expression ="Orders.OrderID"
End
Begin Joins
    LeftTable ="Orders"
    RightTable ="Employees"
    Expression ="Orders.EmployeeID = Employees.ID"
    Flag =1
    LeftTable ="Orders"
    RightTable ="Approver"
    Expression ="Orders.ApproverID = Approver.ID"
    Flag =2
End
"""

TOP_DISTINCT = """Operation =1
Option =18
RowCount ="20"
Begin InputTables
    Name ="Orders"
End
Begin OutputColumns
    Expression ="Orders.*"
End
"""

DISCONNECTED = """Operation =1
Option =0
Begin InputTables
    Name ="A"
    Name ="B"
End
Begin OutputColumns
    Expression ="A.X"
End
"""


class SelectTests(unittest.TestCase):
    def test_columns_aliases_and_order_by(self) -> None:
        result = build(SIMPLE)
        self.assertEqual(result["query_type"], "select")
        self.assertIn("SELECT Companies.CompanyID, Companies.Name AS Label", result["sql"])
        self.assertIn("ORDER BY Companies.Name ASC", result["sql"])
        self.assertTrue(result["translated"])

    def test_alias_belongs_to_the_expression_that_follows_it(self) -> None:
        result = build(SIMPLE)
        self.assertEqual(result["columns"][0]["alias"], None)
        self.assertEqual(result["columns"][1]["alias"], "Label")

    def test_a_table_used_twice_keeps_its_alias_in_the_from_clause(self) -> None:
        result = build(SELF_JOIN)
        self.assertIn("Employees AS Approver", result["sql"])
        self.assertIn("LEFT OUTER JOIN", result["sql"])
        # The join graph is fully connected, so no cross join is reported.
        self.assertNotIn(
            "IMPLICIT_CROSS_JOIN",
            {u["reason_code"] for u in result["unsupported"]},
        )

    def test_distinct_and_top_are_read_from_the_option_bits(self) -> None:
        result = build(TOP_DISTINCT)
        self.assertTrue(result["distinct"])
        self.assertEqual(result["top"], "20")
        self.assertIn("SELECT DISTINCT TOP 20", result["sql"])
        self.assertIn(
            "TOP_CLAUSE_IS_DIALECT_SPECIFIC",
            {u["reason_code"] for u in result["unsupported"]},
        )

    def test_unjoined_tables_are_reported_as_a_cartesian_product(self) -> None:
        result = build(DISCONNECTED)
        self.assertIn(
            "IMPLICIT_CROSS_JOIN",
            {u["reason_code"] for u in result["unsupported"]},
        )
        self.assertFalse(result["translated"])


class StoredSqlTests(unittest.TestCase):
    def test_union_query_is_preserved_and_flagged_as_not_re_parsed(self) -> None:
        result = build(
            'dbMemo "SQL" ="SELECT a FROM T UNION ALL SELECT b FROM U;"\n'
            'dbMemo "Connect" =""\n'
        )
        self.assertEqual(result["query_type"], "union")
        self.assertEqual(result["representation"], "stored_sql_text")
        self.assertIn("T", result["dependencies"])
        self.assertIn(
            "STORED_SQL_NOT_RE_PARSED",
            {u["reason_code"] for u in result["unsupported"]},
        )

    def test_pass_through_query_names_its_external_server(self) -> None:
        result = build(
            'dbMemo "SQL" ="SELECT 1"\n'
            'dbMemo "Connect" ="ODBC;DSN=remote"\n'
        )
        self.assertEqual(result["query_type"], "pass_through")
        self.assertIn(
            "PASS_THROUGH_QUERY_TARGETS_EXTERNAL_SERVER",
            {u["reason_code"] for u in result["unsupported"]},
        )


class UnverifiedOperationTests(unittest.TestCase):
    def test_unmapped_operation_code_is_refused_rather_than_guessed(self) -> None:
        result = build(SIMPLE.replace("Operation =1", "Operation =7"))
        self.assertEqual(result["query_type"], "unknown")
        self.assertIsNone(result["sql"])
        self.assertIn(
            "QUERY_OPERATION_CODE_NOT_MAPPED",
            {u["reason_code"] for u in result["unsupported"]},
        )


if __name__ == "__main__":
    unittest.main()
