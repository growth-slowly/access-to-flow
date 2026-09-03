"""VBA structure, control-flow graphs and external effects."""

from __future__ import annotations

import unittest

from converter.semantics._vba import parse_vba_module

SOURCE = '''Option Compare Database
Option Explicit

Private Const LIMIT As Long = 10

Public Function Total(ByVal items As Long, Optional ByVal rate As Double = 1.5) As Double
10        On Error GoTo Err_Handler
          Dim i As Long
20        For i = 1 To items
30            If i Mod 2 = 0 Then
40                Total = Total + i * rate
50            ElseIf i > LIMIT Then
60                Exit For
70            Else
80                Total = Total - i
90            End If
100       Next i

110       Select Case items
              Case 0
120               MsgBox "none"
              Case Else
130               DoCmd.OpenForm "frmResult"
140       End Select

Exit_Handler:
150       Exit Function
Err_Handler:
160       Resume Exit_Handler
End Function

Private Sub Helper()
10        Dim db As Object
20        Set db = CurrentDb
30        db.Execute "UPDATE Orders SET Paid = True WHERE OrderID = 1"
40        Total 3
End Sub
'''


class ModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = parse_vba_module(SOURCE, "modTest")

    def test_procedures_are_found_with_their_signatures(self) -> None:
        names = [p["name"] for p in self.module["procedures"]]
        self.assertEqual(names, ["Total", "Helper"])
        total = self.module["procedures"][0]
        self.assertEqual(total["kind"], "function")
        self.assertEqual(total["returns"], "Double")
        self.assertEqual([p["name"] for p in total["parameters"]], ["items", "rate"])
        self.assertTrue(total["parameters"][1]["optional"])
        self.assertEqual(total["parameters"][1]["default"], "1.5")

    def test_module_level_declarations_stay_out_of_procedures(self) -> None:
        self.assertTrue(
            any("LIMIT" in line for line in self.module["module_declarations"])
        )

    def test_line_numbers_are_not_mistaken_for_statements(self) -> None:
        total = self.module["procedures"][0]
        sources = [s["source"] for s in total["statements"]]
        self.assertNotIn("10", sources)
        self.assertEqual(sources[0], "On Error GoTo Err_Handler")

    def test_structure_is_nested_not_flattened(self) -> None:
        total = self.module["procedures"][0]
        loop = [s for s in total["statements"] if s["type"] == "for"][0]
        branch = [s for s in loop["statements"] if s["type"] == "if"][0]
        self.assertEqual(
            [b["kind"] for b in branch["branches"]], ["if", "elseif", "else"]
        )

    def test_select_case_keeps_its_cases(self) -> None:
        total = self.module["procedures"][0]
        select = [s for s in total["statements"] if s["type"] == "select_case"][0]
        self.assertEqual(len(select["cases"]), 2)
        self.assertTrue(select["cases"][1]["is_else"])

    def test_metrics_count_branches_and_loops(self) -> None:
        metrics = self.module["procedures"][0]["metrics"]
        self.assertEqual(metrics["loops"], 1)
        self.assertGreaterEqual(metrics["cyclomatic_complexity"], 4)


class EffectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = parse_vba_module(SOURCE, "modTest")

    def test_docmd_actions_are_named(self) -> None:
        total = self.module["procedures"][0]
        self.assertEqual(total["effects"]["docmd_actions"], ["OpenForm"])

    def test_sql_literals_are_extracted(self) -> None:
        helper = self.module["procedures"][1]
        self.assertTrue(
            any("UPDATE Orders" in s for s in helper["effects"]["sql_literals"])
        )
        self.assertTrue(helper["effects"]["uses_data_access"])

    def test_calls_between_procedures_are_recorded(self) -> None:
        helper = self.module["procedures"][1]
        self.assertIn("Total", helper["effects"]["calls"])

    def test_external_effects_make_the_module_only_partly_portable(self) -> None:
        codes = {e["reason_code"] for e in self.module["external_effects"]}
        self.assertIn("VBA_DRIVES_ACCESS_UI", codes)
        self.assertIn("VBA_INTERACTIVE_DIALOG", codes)
        self.assertTrue(self.module["unsupported"])


class FlowGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow = parse_vba_module(SOURCE, "modTest")["procedures"][0]["flow"]

    def test_graph_has_one_start_and_one_end(self) -> None:
        kinds = [n["kind"] for n in self.flow["nodes"]]
        self.assertEqual(kinds.count("start"), 1)
        self.assertEqual(kinds.count("end"), 1)

    def test_branch_bodies_are_emitted_exactly_once(self) -> None:
        labels = [n["label"] for n in self.flow["nodes"] if n["kind"] == "process"]
        self.assertEqual(labels.count("Total = Total - i"), 1)

    def test_decisions_carry_yes_and_no_edges(self) -> None:
        decisions = [n for n in self.flow["nodes"] if n["kind"] == "decision"]
        self.assertTrue(decisions)
        labelled = {e.get("label_key") for e in self.flow["edges"]}
        self.assertIn("yes", labelled)
        self.assertIn("no", labelled)

    def test_graph_carries_no_prose_in_any_single_language(self) -> None:
        """Wording is named, not written, so one IR serves every locale."""
        for edge in self.flow["edges"]:
            # The only literal an edge may carry is VBA source - a Case label.
            # Everything a human reads as a sentence is a key instead.
            if "label" in edge:
                self.assertTrue(
                    edge["label"].startswith("Case "),
                    f"edge carries prose: {edge}",
                )
        for node in self.flow["nodes"]:
            if node.get("text_key"):
                self.assertEqual(node["label"], "")

    def test_error_handler_is_reachable_from_the_on_error_statement(self) -> None:
        error_edges = [e for e in self.flow["edges"] if e.get("kind") == "error"]
        self.assertTrue(error_edges)
        targets = {e["to"] for e in error_edges}
        labels = {
            n["id"]: n["label"] for n in self.flow["nodes"] if n["kind"] == "label"
        }
        self.assertTrue(any(labels.get(t) == "Err_Handler" for t in targets))

    def test_every_edge_points_at_a_node_that_exists(self) -> None:
        ids = {n["id"] for n in self.flow["nodes"]}
        for edge in self.flow["edges"]:
            self.assertIn(edge["from"], ids)
            self.assertIn(edge["to"], ids)


if __name__ == "__main__":
    unittest.main()
