"""The Access ``SaveAsText`` parser."""

from __future__ import annotations

import unittest

from converter.semantics._textformat import (
    parse_access_text,
    split_code_behind,
    unescape_access_string,
)


class UnescapeTests(unittest.TestCase):
    def test_octal_escapes_become_control_characters(self) -> None:
        self.assertEqual(unescape_access_string(r"a\015\012b"), "a\r\nb")

    def test_escaped_quote_and_backslash(self) -> None:
        self.assertEqual(unescape_access_string(r"say \"hi\" \\ ok"), 'say "hi" \\ ok')

    def test_unknown_escape_is_preserved_rather_than_guessed(self) -> None:
        self.assertEqual(unescape_access_string(r"a\qb"), r"a\qb")


class BlockTests(unittest.TestCase):
    def test_named_blocks_and_scalar_properties(self) -> None:
        root = parse_access_text(
            "Operation =1\n"
            "Begin InputTables\n"
            '    Name ="Orders"\n'
            '    Name ="Employees"\n'
            "End\n"
        )
        self.assertEqual(root.get("Operation"), "1")
        self.assertEqual([b.type_name for b in root.children], ["InputTables"])
        self.assertEqual(
            root.children[0].get_all("Name"), ["Orders", "Employees"]
        )

    def test_quoted_value_continues_across_lines(self) -> None:
        root = parse_access_text(
            'Where ="(((Orders.OrderDate) Between #1/1/2020# And "\n'
            '    "#12/31/2020#))"\n'
        )
        self.assertEqual(
            root.get("Where"),
            "(((Orders.OrderDate) Between #1/1/2020# And #12/31/2020#))",
        )

    def test_typed_property_records_its_storage_type(self) -> None:
        root = parse_access_text('dbBoolean "ReturnsRecords" ="-1"\n')
        self.assertEqual(root.properties[0].db_type, "dbBoolean")
        self.assertEqual(root.properties[0].key, "ReturnsRecords")
        self.assertEqual(root.properties[0].value, "-1")

    def test_binary_value_block_is_captured_without_being_decoded(self) -> None:
        root = parse_access_text(
            'dbBinary "GUID" = Begin\n    0x2cf68b0e ,\n    0x2004c844\n End\n'
        )
        prop = root.properties[0]
        self.assertTrue(prop.is_binary)
        self.assertEqual(prop.binary_lines, ("0x2cf68b0e", "0x2004c844"))

    def test_nested_blocks_keep_their_hierarchy(self) -> None:
        root = parse_access_text(
            "Begin Form\n"
            "    Begin\n"
            "        Begin Label\n"
            '            Name ="lbl"\n'
            "        End\n"
            "    End\n"
            "End\n"
        )
        form = root.blocks("Form")[0]
        wrapper = form.children[0]
        self.assertIsNone(wrapper.type_name)
        self.assertEqual(wrapper.children[0].get("Name"), "lbl")


class CodeBehindTests(unittest.TestCase):
    def test_split_finds_the_module_header(self) -> None:
        body, code = split_code_behind(
            "Begin Form\r\nEnd\r\nOption Compare Database\r\nSub A()\r\nEnd Sub\r\n"
        )
        self.assertNotIn("Option Compare", body)
        self.assertTrue(code.startswith("Option Compare Database"))

    def test_definition_without_code_behind_returns_none(self) -> None:
        body, code = split_code_behind("Begin Form\r\nEnd\r\n")
        self.assertIsNone(code)
        self.assertIn("Begin Form", body)


if __name__ == "__main__":
    unittest.main()
