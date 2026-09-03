"""Run the converter over the whole sample corpus and report what it achieved.

This is the project's evidence generator.  It converts every Access artifact
under ``samples/open_access_systems`` and prints - and optionally writes as
JSON - what was discovered, what was extracted and what was semantically
translated, with the blocking reason codes behind every shortfall.

It never writes anything under ``samples/``.

Usage::

    python tools/verify_corpus.py [--json docs/translation_progress.json]
                                  [--html-dir build/ui]
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from converter.access import translate_access_file  # noqa: E402
from converter.semantics._capability import ASPECTS  # noqa: E402
from converter.ui import render_html  # noqa: E402

SAMPLES = ROOT / "samples" / "open_access_systems"
SUFFIXES = (".accdt", ".accdb", ".mdb")


def artifacts() -> list[Path]:
    found: list[Path] = []
    for sample in sorted(SAMPLES.iterdir()):
        original = sample / "original"
        if not original.is_dir():
            continue
        for path in sorted(original.iterdir()):
            if path.suffix.casefold() in SUFFIXES:
                found.append(path)
    return found


def summarise(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    for sample in (result.get("ir") or {}).get("samples", []):
        for artifact in sample.get("artifacts", []):
            objects.extend(artifact.get("objects", []))
    stages = ((result.get("coverage") or {}).get("corpus") or {}).get("stages", {})
    semantics = result.get("semantics") or {}
    row: dict[str, Any] = {
        "sample": path.parent.parent.name,
        "artifact": path.name,
        "format_family": (result.get("source") or {}).get("format_family"),
        "status": result["status"],
        "objects_discovered": len(objects),
        "definitions_extracted": (stages.get("extraction", {}).get("counts", {}) or {}).get(
            "complete", 0
        ),
        "semantically_complete": (
            stages.get("translation", {}).get("counts", {}) or {}
        ).get("complete", 0),
        "semantically_partial": (
            stages.get("translation", {}).get("counts", {}) or {}
        ).get("partial", 0),
        "relationships": len(semantics.get("relationships", [])),
        "aspects": {
            aspect: semantics.get("aspect_totals", {}).get(aspect, {})
            for aspect in ASPECTS
        },
        "blocking_reason_codes": {
            row["reason_code"]: row["objects"]
            for row in semantics.get("reason_codes", [])
        },
    }
    if result["status"] in {"failed", "unsupported"}:
        row["diagnostic"] = (result.get("diagnostics") or [{}])[0].get("reason_code")
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="write the machine-readable snapshot here")
    parser.add_argument("--html-dir", help="also write one offline viewer per artifact")
    parser.add_argument(
        "--ui-omit-source",
        action="store_true",
        help="leave raw definition text out of the generated viewers",
    )
    args = parser.parse_args(argv)

    rows: list[dict[str, Any]] = []
    totals = collections.Counter()
    reason_totals: collections.Counter = collections.Counter()
    for path in artifacts():
        result = translate_access_file(path)
        row = summarise(path, result)
        rows.append(row)
        totals["artifacts"] += 1
        totals["objects"] += row["objects_discovered"]
        totals["extracted"] += row["definitions_extracted"]
        totals["complete"] += row["semantically_complete"]
        totals["partial"] += row["semantically_partial"]
        reason_totals.update(row["blocking_reason_codes"])
        if args.html_dir and result.get("ir"):
            target = Path(args.html_dir) / (path.parent.parent.name + ".html")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                render_html(result, include_source=not args.ui_omit_source),
                encoding="utf-8",
            )

    width = max(len(f"{r['sample']}/{r['artifact']}") for r in rows)
    print(f"{'artifact'.ljust(width)}  fmt    disc  extr  sem   status")
    for row in rows:
        label = f"{row['sample']}/{row['artifact']}".ljust(width)
        print(
            f"{label}  {str(row['format_family'])[:6]:6} "
            f"{row['objects_discovered']:5} {row['definitions_extracted']:5} "
            f"{row['semantically_complete']:5} {row['status']}"
            + (f"  ({row.get('diagnostic')})" if row.get("diagnostic") else "")
        )
    print()
    print(
        f"artifacts={totals['artifacts']}  objects={totals['objects']}  "
        f"definitions extracted={totals['extracted']}  "
        f"semantically complete={totals['complete']}  partial={totals['partial']}"
    )
    print()
    print("top blocking reason codes:")
    for code, count in reason_totals.most_common(15):
        print(f"  {count:5}  {code}")

    snapshot = {
        "schema": "access-translation-progress/2",
        "corpus": {
            "artifacts": totals["artifacts"],
            "objects_discovered": totals["objects"],
            "definitions_extracted": totals["extracted"],
            "semantically_complete": totals["complete"],
            "semantically_partial": totals["partial"],
            "extraction_percentage": round(
                totals["extracted"] * 100 / totals["objects"], 2
            )
            if totals["objects"]
            else 0.0,
            "semantic_percentage": round(
                totals["complete"] * 100 / totals["objects"], 2
            )
            if totals["objects"]
            else 0.0,
        },
        "artifacts": rows,
        "blocking_reason_codes": dict(reason_totals.most_common()),
    }
    if args.json:
        Path(args.json).write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\nsnapshot written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
