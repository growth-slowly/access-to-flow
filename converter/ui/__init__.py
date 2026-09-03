"""Render the translated IR as one self-contained, offline HTML viewer.

The output is a single file with no external references at all: no CDN, no
web font, no image request, no analytics.  That is a hard requirement, not a
preference - the databases this tool reads are usually confidential, and the
report must be safe to open on a machine with no network and safe to hand to
someone who will not inspect what it loads.

The payload embedded in the page is the intermediate representation itself,
so what the viewer draws and what the converter produced cannot drift apart.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from ..flow import build_flow_model
from ..semantics._capability import classify_reason_code

__all__ = ["render_html", "build_payload"]

_ASSETS = Path(__file__).parent / "assets"


def _objects_of(result: dict[str, Any]) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for sample in (result.get("ir") or {}).get("samples", []):
        for artifact in sample.get("artifacts", []):
            objects.extend(artifact.get("objects", []))
    return objects


def _reason_codes_in(result: dict[str, Any]) -> dict[str, str]:
    codes: dict[str, str] = {}
    semantics = result.get("semantics") or {}
    for row in semantics.get("reason_codes", []):
        codes[row["reason_code"]] = row["aspect"]
    for item in _objects_of(result):
        model = item.get("semantics") or {}
        for aspect in ("structure", "data_logic", "application_logic"):
            for problem in (model.get("aspects") or {}).get(aspect, {}).get("blockers", []):
                codes.setdefault(problem["reason_code"], aspect)
        for problem in (model.get("aspects") or {}).get("advisories", []):
            codes.setdefault(problem["reason_code"], classify_reason_code(problem["reason_code"]))
    return codes


def build_payload(
    result: dict[str, Any], *, include_source: bool = True
) -> dict[str, Any]:
    """Assemble everything the viewer needs, and nothing it does not."""
    flow = build_flow_model(result)
    objects = _objects_of(result)
    semantics = result.get("semantics") or {}

    details: dict[str, Any] = {}
    for item in objects:
        model = item.get("semantics")
        if model is None:
            continue
        identifier = f"{item.get('kind')}::{item.get('name')}"
        payload = {
            key: value
            for key, value in model.items()
            if key not in {"aspects", "schema"}
        }
        # The per-procedure flow graphs are already published once under
        # ``flow.diagrams``; carrying them twice would double the file size.
        if "procedures" in payload:
            payload["procedures"] = [
                {k: v for k, v in procedure.items() if k not in {"flow", "statements"}}
                for procedure in payload["procedures"]
            ]
        if "code_behind" in payload:
            payload["code_behind"] = {
                key: value
                for key, value in payload["code_behind"].items()
                if key != "procedures"
            }
        if include_source:
            payload["source_text"] = (item.get("content") or {}).get("source_text")
        details[identifier] = payload

    procedures = [
        {key: value for key, value in procedure.items() if key != "flow"}
        for procedure in flow["procedures"]
    ]

    source = result.get("source") or {}
    return {
        "meta": {
            "file_name": source.get("file_name"),
            "format_description": (
                (result.get("ir") or {})
                .get("samples", [{}])[0]
                .get("artifacts", [{}])[0]
                .get("format_description", "")
                if result.get("ir")
                else ""
            ),
            "size_bytes": source.get("size_bytes"),
            "sha256": source.get("sha256"),
            "status": result.get("status"),
            "object_count": len(objects),
            "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "includes_source_text": include_source,
        },
        "semantics": {
            "totals": semantics.get("totals", {}),
            "aspect_totals": semantics.get("aspect_totals", {}),
            "features": semantics.get("features", []),
            "reason_codes": semantics.get("reason_codes", []),
            "relationships": semantics.get("relationships", []),
        },
        "flow": {
            "objects": flow["objects"],
            "procedures": procedures,
            "graph": flow["graph"],
            "diagrams": flow["diagrams"],
            "entry_points": flow["entry_points"],
        },
        "stages": ((result.get("coverage") or {}).get("corpus") or {}).get("stages", {}),
        "unprocessed": result.get("unprocessed_features", []),
        "details": details,
        "aspect_of": _reason_codes_in(result),
    }


def render_html(result: dict[str, Any], *, include_source: bool = True) -> str:
    """Return the complete HTML document for one translation result."""
    template = (_ASSETS / "index.html").read_text(encoding="utf-8")
    css = (_ASSETS / "app.css").read_text(encoding="utf-8")
    script = (_ASSETS / "app.js").read_text(encoding="utf-8")
    messages = (_ASSETS / "i18n.js").read_text(encoding="utf-8")
    payload = build_payload(result, include_source=include_source)
    title = (result.get("source") or {}).get("file_name") or "Access 変換結果"

    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # The payload lives in a <script type="application/json"> block, so the
    # only sequence that could break out of it is a literal closing tag.
    data = data.replace("</", "<\\/")

    return (
        template.replace("__CSS__", css)
        .replace("__I18N__", messages)
        .replace("__JS__", script)
        .replace("__TITLE__", _escape(title))
        .replace("__DATA__", data)
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
