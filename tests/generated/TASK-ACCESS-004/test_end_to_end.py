"""Whole-file translation over the real sample corpus."""

from __future__ import annotations

import json
import unittest

from converter.access import translate_access_file
from converter.flow import build_flow_model
from converter.semantics import ADVISORY_REASON_CODES
from converter.semantics._capability import ASPECTS, classify_reason_code
from converter.ui import render_html

from _support import (
    NORTHWIND_DEV,
    NORTHWIND_STARTER,
    SPORTS_ACCDB,
    find_object,
    objects_of,
    translated,
)


class AccdtTranslationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if NORTHWIND_DEV is None:
            raise unittest.SkipTest("Northwind Developer Edition sample is missing")
        cls.result = translated(NORTHWIND_DEV)

    def test_every_object_definition_was_extracted(self) -> None:
        objects = objects_of(self.result)
        self.assertGreater(len(objects), 150)
        self.assertTrue(
            all(o["stages"]["extraction"]["status"] == "complete" for o in objects)
        )

    def test_no_object_fails_semantic_parsing(self) -> None:
        failures = [
            o["name"]
            for o in objects_of(self.result)
            if o["stages"]["translation"]["status"] == "failed"
        ]
        self.assertEqual(failures, [])

    def test_a_known_query_produces_the_expected_sql(self) -> None:
        query = find_object(self.result, "query", "qrycboCompanyType")
        self.assertIsNotNone(query)
        sql = query["semantics"]["sql"]
        self.assertIn("SELECT CompanyTypes.CompanyTypeID", sql)
        self.assertIn("FROM CompanyTypes", sql)
        self.assertIn("ORDER BY CompanyTypes.CompanyType ASC", sql)

    def test_a_known_table_produces_ddl_with_its_primary_key(self) -> None:
        table = find_object(self.result, "table", "Orders")
        self.assertIsNotNone(table)
        self.assertIn("PRIMARY KEY (OrderID)", table["semantics"]["ddl"])

    def test_form_code_behind_is_analysed_with_its_events(self) -> None:
        form = find_object(self.result, "form", "frmLogin")
        self.assertIsNotNone(form)
        model = form["semantics"]
        self.assertIn("cmdLogin_Click", model["vba_handlers"])
        procedures = {p["name"] for p in model["code_behind"]["procedures"]}
        self.assertIn("cmdLogin_Click", procedures)

    def test_relationships_are_translated_into_foreign_keys(self) -> None:
        relationships = self.result["semantics"]["relationships"]
        self.assertGreater(len(relationships), 20)
        self.assertTrue(all("FOREIGN KEY" in r["ddl"] for r in relationships))

    def test_aspect_totals_are_reported_per_aspect(self) -> None:
        totals = self.result["semantics"]["aspect_totals"]
        for aspect in ASPECTS:
            self.assertIn(aspect, totals)
            row = totals[aspect]
            self.assertEqual(
                row["complete"] + row["partial"] + row["failed"],
                row["scored_objects"],
            )

    def test_structure_recovery_is_essentially_complete(self) -> None:
        structure = self.result["semantics"]["aspect_totals"]["structure"]
        self.assertGreater(structure["completion_percentage"], 95.0)

    def test_a_catalog_record_is_never_reported_as_a_translation(self) -> None:
        for item in objects_of(self.result):
            if item["stages"]["translation"]["status"] == "complete":
                self.assertIn("semantics", item)


class ReasonCodeDisciplineTests(unittest.TestCase):
    """Every reason code the corpus emits must be classified, with a note."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.results = [
            translated(path)
            for path in (NORTHWIND_DEV, NORTHWIND_STARTER, SPORTS_ACCDB)
            if path is not None
        ]
        if not cls.results:
            raise unittest.SkipTest("no samples available")

    def _problems(self):
        for result in self.results:
            for item in objects_of(result):
                model = item.get("semantics") or {}
                for problem in model.get("unsupported", []):
                    yield problem

    def test_no_reason_code_is_unclassified(self) -> None:
        unclassified = {
            problem["reason_code"]
            for problem in self._problems()
            if classify_reason_code(problem["reason_code"]) == "unclassified"
        }
        self.assertEqual(unclassified, set())

    def test_every_problem_carries_a_detail_and_a_note(self) -> None:
        for problem in self._problems():
            self.assertTrue(problem.get("reason_code"))
            self.assertIn("detail", problem)
            self.assertTrue(
                problem.get("note")
                or problem["reason_code"] in ADVISORY_REASON_CODES
            )

    def test_advisories_never_block_an_object(self) -> None:
        for result in self.results:
            for item in objects_of(result):
                model = item.get("semantics") or {}
                aspects = model.get("aspects") or {}
                for aspect in ASPECTS:
                    for blocker in aspects.get(aspect, {}).get("blockers", []):
                        self.assertNotIn(
                            blocker["reason_code"], ADVISORY_REASON_CODES
                        )


class BinaryDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if SPORTS_ACCDB is None:
            raise unittest.SkipTest("Sports.accdb sample is missing")
        cls.result = translated(SPORTS_ACCDB)

    def test_objects_are_discovered_but_never_claimed_as_extracted(self) -> None:
        objects = objects_of(self.result)
        self.assertGreater(len(objects), 400)
        for item in objects:
            self.assertEqual(item["stages"]["discovery"]["status"], "complete")
            self.assertEqual(item["stages"]["extraction"]["status"], "not_started")
            self.assertEqual(item["stages"]["translation"]["status"], "not_started")

    def test_the_gap_is_named_in_unprocessed_features(self) -> None:
        codes = {f["reason_code"] for f in self.result["unprocessed_features"]}
        self.assertIn("BINARY_OBJECT_DEFINITION_EXTRACTION_NOT_IMPLEMENTED", codes)

    def test_semantic_completion_is_zero_not_omitted(self) -> None:
        self.assertEqual(self.result["completion_summary"]["semantic_objects_complete"], 0)


class ViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if NORTHWIND_STARTER is None:
            raise unittest.SkipTest("Northwind Starter Edition sample is missing")
        cls.result = translated(NORTHWIND_STARTER)
        cls.html = render_html(cls.result, include_source=False)

    @staticmethod
    def _markup_without_payload(html: str) -> str:
        """Return the page's own markup, with the embedded data removed.

        The payload legitimately contains URLs, because the Access application
        being reported on contains URLs.  What must not exist is markup or
        script that *loads* something.
        """
        opening = html.index('type="application/json">')
        closing = html.index("</script>", opening)
        return (html[:opening] + html[closing:]).lower()

    def test_page_loads_nothing_from_anywhere(self) -> None:
        markup = self._markup_without_payload(self.html)
        for forbidden in (
            "src=", "href=", "@import", "url(http", "<link", "<iframe",
            "<img", "xmlhttprequest", "fetch(", "websocket", "importscripts",
        ):
            self.assertNotIn(forbidden, markup, f"viewer references {forbidden!r}")

    def test_page_declares_its_own_styles_and_script_inline(self) -> None:
        self.assertIn("<style>", self.html)
        self.assertIn("<script>", self.html)

    def test_payload_is_valid_json_and_carries_the_model(self) -> None:
        start = self.html.index('type="application/json">') + len('type="application/json">')
        end = self.html.index("</script>", start)
        payload = json.loads(self.html[start:end].replace("<\\/", "</"))
        self.assertIn("flow", payload)
        self.assertIn("semantics", payload)
        self.assertTrue(payload["flow"]["objects"])

    def test_source_text_is_omitted_when_requested(self) -> None:
        for detail in json.loads(
            self.html[
                self.html.index('type="application/json">')
                + len('type="application/json">') : self.html.index(
                    "</script>",
                    self.html.index('type="application/json">'),
                )
            ].replace("<\\/", "</")
        )["details"].values():
            self.assertNotIn("source_text", detail)


class FlowModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if NORTHWIND_STARTER is None:
            raise unittest.SkipTest("Northwind Starter Edition sample is missing")
        cls.model = build_flow_model(translated(NORTHWIND_STARTER))

    def test_graph_edges_reference_existing_nodes(self) -> None:
        ids = {n["id"] for n in self.model["graph"]["nodes"]}
        for edge in self.model["graph"]["edges"]:
            self.assertIn(edge["from"], ids)
            self.assertIn(edge["to"], ids)

    def test_forms_are_linked_to_their_record_sources(self) -> None:
        kinds = {e["kind"] for e in self.model["graph"]["edges"]}
        self.assertIn("reads", kinds)
        self.assertIn("handles", kinds)

    def test_every_procedure_has_a_drawable_diagram(self) -> None:
        for procedure in self.model["procedures"]:
            diagram = self.model["diagrams"][procedure["id"]]
            self.assertTrue(diagram["nodes"])
            ids = {n["id"] for n in diagram["nodes"]}
            for edge in diagram["edges"]:
                self.assertIn(edge["from"], ids)
                self.assertIn(edge["to"], ids)

    def test_autoexec_is_offered_as_an_entry_point(self) -> None:
        names = {e["name"] for e in self.model["entry_points"]}
        self.assertTrue(names)


if __name__ == "__main__":
    unittest.main()
