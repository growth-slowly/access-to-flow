"""Macros and data macros: nesting, action classification and honesty."""

from __future__ import annotations

import unittest

from converter.semantics._macrodef import translate_data_macro, translate_macro
from converter.semantics._textformat import parse_access_text

AXL_NS = 'xmlns="http://schemas.microsoft.com/office/accessservices/2009/11/application"'

DATA_MACRO = f"""<?xml version="1.0" encoding="UTF-8"?>
<DataMacros {AXL_NS}><DataMacro Event="BeforeChange"><Statements>
<Comment>audit</Comment>
<ConditionalBlock>
<If><Condition>[IsInsert]</Condition><Statements>
<Action Name="SetField"><Argument Name="Field">AddedOn</Argument>
<Argument Name="Value">Now()</Argument></Action>
</Statements></If>
<Else><Statements>
<Action Name="SetField"><Argument Name="Field">ModifiedOn</Argument>
<Argument Name="Value">Now()</Argument></Action>
</Statements></Else>
</ConditionalBlock>
</Statements></DataMacro></DataMacros>
"""

LEGACY_MACRO = """Version =196611
Begin
    Condition ="[X]=1"
    Action ="OpenForm"
    Argument ="frmA"
End
Begin
    Action ="RunCode"
    Argument ="Startup()"
End
"""


class DataMacroTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = translate_data_macro(DATA_MACRO, "Orders")

    def test_event_becomes_a_trigger_shape(self) -> None:
        handler = self.model["handlers"][0]
        self.assertEqual(handler["event"], "BeforeChange")
        self.assertEqual(handler["trigger_timing"], "before")
        self.assertEqual(handler["trigger_operation"], "insert_or_update")

    def test_conditional_block_keeps_both_branches(self) -> None:
        statements = self.model["handlers"][0]["statements"]
        conditional = [s for s in statements if s["type"] == "conditional"][0]
        self.assertEqual(
            [b["branch"] for b in conditional["branches"]], ["if", "else"]
        )

    def test_setfield_is_classified_as_data_and_translatable(self) -> None:
        conditional = [
            s for s in self.model["handlers"][0]["statements"] if s["type"] == "conditional"
        ][0]
        action = conditional["branches"][0]["statements"][0]
        self.assertEqual(action["category"], "data")
        self.assertTrue(action["translated"])
        self.assertTrue(self.model["translated"])


class LegacyMacroTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = translate_macro(parse_access_text(LEGACY_MACRO), "mcrA")

    def test_flat_action_list_is_reported_as_a_structural_limit(self) -> None:
        self.assertEqual(self.model["representation"], "legacy_action_list")
        self.assertIn(
            "MACRO_STORED_AS_FLAT_ACTION_LIST",
            {u["reason_code"] for u in self.model["unsupported"]},
        )
        self.assertFalse(self.model["translated"])

    def test_ui_and_system_actions_are_separated(self) -> None:
        census = self.model["actions"]
        self.assertEqual(census["ui"], ["OpenForm"])
        self.assertEqual(census["system"], ["RunCode"])

    def test_per_row_condition_is_kept(self) -> None:
        first = [s for s in self.model["statements"] if s["type"] == "action"][0]
        self.assertEqual(first["condition"]["source"], "[X]=1")


class AxlPreferenceTests(unittest.TestCase):
    def test_embedded_axl_is_preferred_over_the_flat_list(self) -> None:
        axl = (
            '<?xml version="1.0"?><UserInterfaceMacro ' + AXL_NS +
            "><Statements><Action Name=\"Beep\"/></Statements></UserInterfaceMacro>"
        )
        text = LEGACY_MACRO + "Begin\n    Comment =\"_AXL:" + axl.replace('"', '\\"') + "\"\nEnd\n"
        model = translate_macro(parse_access_text(text), "mcrA")
        self.assertEqual(model["representation"], "axl")


if __name__ == "__main__":
    unittest.main()
