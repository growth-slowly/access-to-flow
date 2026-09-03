"""Command-line interface for the fully offline Access converter."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .access import translate_access_file
from .ui import render_html


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="access-converter")
    commands = parser.add_subparsers(dest="command", required=True)
    translate = commands.add_parser(
        "translate",
        help="translate one Access file into offline intermediate JSON",
    )
    translate.add_argument("input")
    translate.add_argument("--output", required=True)
    translate.add_argument(
        "--omit-source-text",
        action="store_true",
        help="keep hashes and metadata but omit raw definition text",
    )
    translate.add_argument(
        "--html",
        help="also write a self-contained offline HTML viewer to this path",
    )
    translate.add_argument(
        "--ui-omit-source",
        action="store_true",
        help="leave the raw definition text out of the HTML viewer",
    )

    view = commands.add_parser(
        "ui",
        help="render a self-contained offline HTML viewer from an existing result",
    )
    view.add_argument("input", help="a JSON file written by the translate command")
    view.add_argument("--output", required=True)
    view.add_argument(
        "--omit-source",
        action="store_true",
        help="leave the raw definition text out of the HTML viewer",
    )
    return parser


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "ui":
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        output = Path(args.output)
        _write_text_atomic(
            output, render_html(payload, include_source=not args.omit_source)
        )
        print(f"Viewer: {output}")
        return 0
    source = Path(args.input)
    output = Path(args.output)
    if source.resolve() == output.resolve():
        raise SystemExit("output must not overwrite the Access input")
    result = translate_access_file(
        source,
        include_source_text=not args.omit_source_text,
    )
    _write_json_atomic(output, result)
    print(f"Status: {result['status']}")
    print(f"Output: {output}")
    if args.html and result.get("ir"):
        viewer = Path(args.html)
        _write_text_atomic(
            viewer,
            render_html(result, include_source=not args.ui_omit_source),
        )
        print(f"Viewer: {viewer}")
    semantics = result.get("semantics")
    if semantics:
        for aspect, row in semantics["aspect_totals"].items():
            print(
                f"  {row['label']}: {row['complete']}/{row['scored_objects']} "
                f"({row['completion_percentage']:.2f}%)"
            )
    if result["status"] in {"complete", "partial"}:
        return 0
    if result["status"] == "unsupported":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
