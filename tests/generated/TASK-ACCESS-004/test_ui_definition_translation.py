"""Forms and reports: sections, controls, bindings and event wiring."""

from __future__ import annotations

import unittest

from converter.semantics._textformat import parse_access_text
from converter.semantics._uidef import translate_form

FORM = """Version =21
Begin Form
    RecordSource ="qryOrders"
    OnLoad ="[Event Procedure]"
    OnCurrent ="mcrRefresh"
    Begin
        Begin Label
            FontSize =11
        End
        Begin CommandButton
            FontSize =11
        End
        Begin FormHeader
            Height =720
            Name ="FormHeader"
            Begin
                Begin Label
                    Name ="lblTitle"
                    Caption ="Orders"
                    Left =100
                    Top =50
                    Width =2000
                    Height =300
                End
            End
        End
        Begin Section
            Height =3000
            Name ="Detail"
            Begin
                Begin ComboBox
                    Name ="cboCustomer"
                    ControlSource ="CustomerID"
                    RowSource ="qryCustomers"
                    AfterUpdate ="[Event Procedure]"
                End
                Begin TextBox
                    Name ="txtTotal"
                    ControlSource ="=Nz([Amount],0)*1.1"
                End
                Begin ListBox
                    Name ="lstKind"
                    RowSourceType ="Value List"
                    RowSource ="A;B"
                End
                Begin Subform
                    Name ="subDetails"
                    SourceObject ="Form.sfrmOrderLines"
                    LinkChildFields ="OrderID"
                    LinkMasterFields ="OrderID"
                End
                Begin CustomControl
                    Name ="axCalendar"
                End
            End
        End
    End
End
"""


class FormTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = translate_form(parse_access_text(FORM), "frmOrders", "form")

    def test_style_default_blocks_are_not_mistaken_for_sections(self) -> None:
        self.assertEqual(
            [s["section_type"] for s in self.model["sections"]],
            ["FormHeader", "Section"],
        )
        self.assertIn("Label", self.model["style_defaults"])

    def test_record_source_becomes_a_dependency(self) -> None:
        self.assertEqual(self.model["record_source"], "qryOrders")
        self.assertIn("qryOrders", self.model["dependencies"])
        self.assertIn("qryCustomers", self.model["dependencies"])

    def test_event_procedure_name_follows_the_access_naming_rule(self) -> None:
        handlers = {(e["event"], e["handler"]) for e in self.model["events"]}
        self.assertIn(("OnLoad", "Form_Load"), handlers)
        self.assertIn(("AfterUpdate", "cboCustomer_AfterUpdate"), handlers)

    def test_macro_handler_is_distinguished_from_a_vba_handler(self) -> None:
        current = [e for e in self.model["events"] if e["event"] == "OnCurrent"][0]
        self.assertEqual(current["handler_kind"], "macro_object")
        self.assertEqual(current["handler"], "mcrRefresh")

    def test_calculated_control_source_is_translated(self) -> None:
        total = [c for c in self.model["controls"] if c["name"] == "txtTotal"][0]
        binding = total["bindings"][0]
        self.assertEqual(binding["kind"], "calculated")
        self.assertEqual(binding["sql"], "COALESCE(Amount, 0) * 1.1")

    def test_value_list_row_source_is_reported_as_not_a_query(self) -> None:
        codes = {u["reason_code"] for u in self.model["unsupported"]}
        self.assertIn("ROW_SOURCE_TYPE_NOT_A_QUERY", codes)

    def test_activex_control_is_reported_as_unportable(self) -> None:
        codes = {u["reason_code"] for u in self.model["unsupported"]}
        self.assertIn("CONTROL_TYPE_HAS_NO_PORTABLE_EQUIVALENT", codes)

    def test_subform_source_drops_its_container_prefix(self) -> None:
        sub = [c for c in self.model["controls"] if c["name"] == "subDetails"][0]
        binding = [b for b in sub["bindings"] if b["kind"] == "subform"][0]
        self.assertEqual(binding["source"], "Form.sfrmOrderLines")
        self.assertEqual(binding["link_child"], "OrderID")

    def test_geometry_is_kept_for_the_screen_sketch(self) -> None:
        label = [c for c in self.model["controls"] if c["name"] == "lblTitle"][0]
        self.assertEqual(label["geometry"]["width"], 2000)


if __name__ == "__main__":
    unittest.main()
