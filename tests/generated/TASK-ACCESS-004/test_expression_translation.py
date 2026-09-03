"""Access expression analysis and its honesty about what it cannot carry."""

from __future__ import annotations

import unittest

from converter.semantics._expr import analyze_expression


def codes(source: str, known: set[str] | None = None) -> set[str]:
    return {item["reason_code"] for item in analyze_expression(source, known).unsupported}


class TranslationTests(unittest.TestCase):
    def test_string_concatenation_becomes_sql_concatenation(self) -> None:
        result = analyze_expression('[A].[B] & " " & [A].[C]')
        self.assertEqual(result.sql, "A.B || ' ' || A.C")
        self.assertTrue(result.translatable)

    def test_iif_becomes_a_case_expression(self) -> None:
        result = analyze_expression("IIf([X]>0,1,0)")
        self.assertEqual(result.sql, "CASE WHEN X > 0 THEN 1 ELSE 0 END")

    def test_isnull_becomes_an_is_null_predicate(self) -> None:
        self.assertEqual(analyze_expression("IsNull([X])").sql, "(X IS NULL)")

    def test_nz_with_a_default_becomes_coalesce(self) -> None:
        self.assertEqual(analyze_expression("Nz([X],0)").sql, "COALESCE(X, 0)")

    def test_zero_argument_date_functions_lose_their_parentheses(self) -> None:
        self.assertEqual(analyze_expression("Now()").sql, "CURRENT_TIMESTAMP")
        self.assertEqual(analyze_expression("Date()").sql, "CURRENT_DATE")

    def test_year_becomes_extract(self) -> None:
        self.assertEqual(
            analyze_expression("Year([D])").sql, "EXTRACT(YEAR FROM D)"
        )

    def test_between_and_in_are_recognised_as_predicates(self) -> None:
        self.assertEqual(
            analyze_expression("[A] Between 1 And 5").sql, "A BETWEEN 1 AND 5"
        )
        self.assertEqual(analyze_expression("[A] In (1,2)").sql, "A IN (1, 2)")

    def test_whole_row_selector_is_not_quoted_as_a_column(self) -> None:
        self.assertEqual(analyze_expression("Orders.*").sql, "Orders.*")

    def test_like_pattern_wildcards_are_rewritten_for_sql(self) -> None:
        result = analyze_expression('[N] Like "A*b?"')
        self.assertEqual(result.sql, "N LIKE 'A%b_'")
        self.assertIn("LIKE_WILDCARD_DIALECT", codes('[N] Like "A*b?"'))

    def test_identifier_needing_quotes_is_quoted(self) -> None:
        self.assertEqual(analyze_expression("[my field]").sql, '"my field"')


class UntranslatableTests(unittest.TestCase):
    def test_format_is_reported_as_an_access_runtime_function(self) -> None:
        self.assertIn("ACCESS_RUNTIME_FUNCTION", codes('Format([D],"yyyy")'))

    def test_domain_aggregate_is_reported(self) -> None:
        self.assertIn("ACCESS_RUNTIME_FUNCTION", codes('DLookUp("a","b","c")'))

    def test_project_defined_function_is_named_as_such(self) -> None:
        self.assertIn(
            "USER_DEFINED_VBA_FUNCTION", codes("GetString(1)", {"GetString"})
        )

    def test_unknown_function_is_distinguished_from_a_project_function(self) -> None:
        self.assertIn("UNKNOWN_FUNCTION", codes("NoSuchThing(1)", set()))

    def test_application_object_reference_is_not_treated_as_a_column(self) -> None:
        result = analyze_expression("[CurrentProject].[IsTrusted]")
        self.assertFalse(result.translatable)
        self.assertIn("ACCESS_OBJECT_MODEL_REFERENCE", codes("[CurrentProject].[IsTrusted]"))

    def test_form_reference_becomes_a_bind_parameter(self) -> None:
        result = analyze_expression("[Forms]![frmA]![ctlB]")
        self.assertEqual(result.sql, ":Forms_frmA_ctlB")
        self.assertEqual(result.parameters, [":Forms_frmA_ctlB"])
        # A bind parameter is an advisory, not a blocker: the query still runs
        # once the application supplies the value.
        self.assertTrue(result.translatable)

    def test_unparseable_expression_reports_a_parse_failure(self) -> None:
        result = analyze_expression("((")
        self.assertFalse(result.parsed)
        self.assertIn("EXPRESSION_PARSE_FAILED", codes("(("))

    def test_empty_expression_is_translatable_and_produces_nothing(self) -> None:
        result = analyze_expression("   ")
        self.assertTrue(result.translatable)
        self.assertIsNone(result.sql)


if __name__ == "__main__":
    unittest.main()
