"""The Jet4/ACE catalog reader, checked against the recorded sample inventories.

The inventories under ``samples/*/object_inventory.json`` were produced by a
separate tool.  Reproducing their counts from a freshly written reader is the
strongest evidence available here that the page walk is correct, because the
two implementations agree only if both match the file.
"""

from __future__ import annotations

import collections
import json
import unittest
from pathlib import Path

from converter.access import ace_catalog
from converter.access.ace_catalog import AceCatalogError, read_catalog

from _support import SAMPLES  # noqa: F401  (path bootstrap)

_KIND_TO_INVENTORY = {
    "table": "Table",
    "query": "Query",
    "form": "Form",
    "report": "Report",
    "macro": "Macro",
    "module": "Module",
    "linked_table": "Table(linked)",
    "linked_odbc_table": "Table(ODBC-linked)",
}
_HIDDEN_KEY = "HiddenQuery(~sq_ form/report recordsource)"


def _inventories() -> list[tuple[Path, dict]]:
    found: list[tuple[Path, dict]] = []
    for inventory in sorted(SAMPLES.glob("*/object_inventory.json")):
        data = json.loads(inventory.read_text(encoding="utf-8"))
        for database in data.get("databases", []):
            path = inventory.parent / "original" / database["file"]
            if path.exists():
                found.append((path, database))
    return found


class CatalogAgreementTests(unittest.TestCase):
    def test_corpus_is_present(self) -> None:
        if not _inventories():
            self.skipTest("sample corpus is not present")

    def test_counts_match_the_recorded_inventories(self) -> None:
        if not _inventories():
            self.skipTest("sample corpus is not present")
        checked = 0
        for path, database in _inventories():
            try:
                catalog = read_catalog(path)
            except AceCatalogError:
                # Jet3 files are read by the other module; they are covered by
                # that module's own tests.
                continue
            counts: collections.Counter = collections.Counter()
            for entry in catalog["objects"]:
                name = str(entry["name"])
                if name.startswith("MSys") or name.startswith("f_"):
                    continue
                key = _KIND_TO_INVENTORY.get(str(entry["kind"]))
                if key is None:
                    continue
                if key == "Query" and name.startswith("~"):
                    key = _HIDDEN_KEY
                counts[key] += 1
            expected = database["counts"]
            for key in set(expected) | set(counts):
                self.assertEqual(
                    counts.get(key, 0),
                    expected.get(key, 0),
                    f"{path.name}: {key}",
                )
            checked += 1
        self.assertGreaterEqual(checked, 10, "too few ACE artifacts were checked")

    def test_object_names_survive_decoding(self) -> None:
        for path, _ in _inventories():
            try:
                catalog = read_catalog(path)
            except AceCatalogError:
                continue
            for entry in catalog["objects"]:
                self.assertTrue(str(entry["name"]))
                self.assertNotIn("\x00", str(entry["name"]))


class FormatGuardTests(unittest.TestCase):
    def test_jet3_is_refused_rather_than_misparsed(self) -> None:
        jet3 = SAMPLES / "mdbtools_testdata" / "original" / "nwind.mdb"
        if not jet3.exists():
            self.skipTest("Jet3 sample is not present")
        with self.assertRaises(AceCatalogError):
            read_catalog(jet3)

    def test_non_database_input_is_refused(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".accdb", delete=False) as stream:
            stream.write(b"\x00" * (ace_catalog._PAGE_SIZE * 2))
            name = stream.name
        try:
            with self.assertRaises(AceCatalogError):
                read_catalog(name)
        finally:
            Path(name).unlink(missing_ok=True)

    def test_bytes_path_is_a_type_error(self) -> None:
        with self.assertRaises(TypeError):
            read_catalog(b"x.accdb")


class TextDecodingTests(unittest.TestCase):
    def test_uncompressed_utf16(self) -> None:
        self.assertEqual(ace_catalog._decode_text("Orders".encode("utf-16-le")), "Orders")

    def test_compressed_text_uses_one_byte_per_character(self) -> None:
        self.assertEqual(ace_catalog._decode_text(b"\xff\xfeOrders"), "Orders")

    def test_marker_toggles_back_to_two_bytes(self) -> None:
        payload = b"\xff\xfeAB" + b"\xff\xfe" + "あ".encode("utf-16-le")
        self.assertEqual(ace_catalog._decode_text(payload), "ABあ")


if __name__ == "__main__":
    unittest.main()
