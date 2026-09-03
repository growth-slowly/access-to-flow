"""Table schemas and relationships."""

from __future__ import annotations

import unittest

from converter.semantics._tabledef import translate_relationships, translate_table

XSD = """<?xml version="1.0" encoding="UTF-8"?>
<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema"
            xmlns:od="urn:schemas-microsoft-com:officedata">
<xsd:element name="Orders">
<xsd:annotation><xsd:appinfo>
<od:index index-name="PrimaryKey" index-key="OrderID " primary="yes" unique="yes"
          clustered="no" order="asc"/>
</xsd:appinfo></xsd:annotation>
<xsd:complexType><xsd:sequence>
<xsd:element name="OrderID" minOccurs="1" od:jetType="autonumber"
             od:autoUnique="yes" od:nonNullable="yes" type="xsd:int"/>
<xsd:element name="Note" minOccurs="0" od:jetType="text" type="xsd:string">
  <xsd:annotation><xsd:appinfo>
    <od:fieldProperty name="ColumnWidth" type="3" value="1000"/>
    <od:fieldProperty name="DefaultValue" type="12" value="Now()"/>
  </xsd:appinfo></xsd:annotation>
  <xsd:simpleType><xsd:restriction base="xsd:string">
    <xsd:maxLength value="50"/>
  </xsd:restriction></xsd:simpleType>
</xsd:element>
<xsd:element name="Qty" minOccurs="1" od:jetType="longinteger"
             od:nonNullable="yes" type="xsd:int">
  <xsd:annotation><xsd:appinfo>
    <od:fieldProperty name="ValidationRule" type="12" value="&gt;0"/>
  </xsd:appinfo></xsd:annotation>
</xsd:element>
<xsd:element name="Files" minOccurs="0" od:jetType="complex" type="xsd:string"/>
</xsd:sequence></xsd:complexType>
</xsd:element>
</xsd:schema>
"""

RELATIONSHIPS = """<?xml version="1.0" encoding="UTF-8"?>
<dataroot xmlns:od="urn:schemas-microsoft-com:officedata">
<MSysRelationships>
  <grbit>4352</grbit>
  <szColumn>CompanyID</szColumn>
  <szObject>Contacts</szObject>
  <szReferencedColumn>ID</szReferencedColumn>
  <szReferencedObject>Companies</szReferencedObject>
  <szRelationship>FK_Contacts_Companies</szRelationship>
</MSysRelationships>
</dataroot>
"""


class TableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = translate_table(XSD, "Orders")

    def test_primary_key_comes_from_the_index_annotation(self) -> None:
        self.assertEqual(self.model["primary_key"], ["OrderID"])

    def test_text_length_reaches_the_generated_type(self) -> None:
        note = [c for c in self.model["columns"] if c["name"] == "Note"][0]
        self.assertEqual(note["sql_type"], "VARCHAR(50)")

    def test_default_value_expression_is_translated(self) -> None:
        note = [c for c in self.model["columns"] if c["name"] == "Note"][0]
        self.assertEqual(note["default"]["sql"], "CURRENT_TIMESTAMP")

    def test_bare_comparison_validation_rule_gets_its_implicit_subject(self) -> None:
        qty = [c for c in self.model["columns"] if c["name"] == "Qty"][0]
        self.assertEqual(qty["validation"]["sql"], "Qty > 0")

    def test_complex_field_has_no_portable_type_and_says_so(self) -> None:
        files = [c for c in self.model["columns"] if c["name"] == "Files"][0]
        self.assertIsNone(files["sql_type"])
        self.assertFalse(files["translated"])
        self.assertIn(
            "FIELD_TYPE_HAS_NO_PORTABLE_EQUIVALENT",
            {u["reason_code"] for u in self.model["unsupported"]},
        )

    def test_generated_ddl_carries_keys_defaults_and_checks(self) -> None:
        ddl = self.model["ddl"]
        self.assertIn("CREATE TABLE Orders", ddl)
        self.assertIn("PRIMARY KEY (OrderID)", ddl)
        self.assertIn("DEFAULT CURRENT_TIMESTAMP", ddl)
        self.assertIn("CHECK (Qty > 0)", ddl)

    def test_display_only_properties_do_not_become_semantics(self) -> None:
        note = [c for c in self.model["columns"] if c["name"] == "Note"][0]
        self.assertNotIn("ColumnWidth", str(note.get("format")))

    def test_unsafe_xml_is_refused_before_parsing(self) -> None:
        with self.assertRaises(ValueError):
            translate_table('<!DOCTYPE x [<!ENTITY a "b">]><x/>', "x")


class RelationshipTests(unittest.TestCase):
    def test_foreign_key_and_cascade_flags(self) -> None:
        rows = translate_relationships(RELATIONSHIPS)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["child_table"], "Contacts")
        self.assertEqual(row["parent_table"], "Companies")
        self.assertTrue(row["cascade_update"])
        self.assertTrue(row["cascade_delete"])
        self.assertIn("FOREIGN KEY (CompanyID)", row["ddl"])
        self.assertIn("ON DELETE CASCADE", row["ddl"])


if __name__ == "__main__":
    unittest.main()
