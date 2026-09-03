from __future__ import annotations

import json
import subprocess
import sys
import warnings
import zipfile
from pathlib import Path

import pytest

from converter.access import translate_access_file
from converter.access import translation


def _minimal_package(path: Path, object_path: str, payload: str) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr(
            "template/template.xml",
            "<?xml version='1.0'?><Template />",
        )
        package.writestr(object_path, payload)


@pytest.mark.parametrize("bad", [None, 1, True, b"input.accdt", object()])
def test_invalid_source_types_raise_type_error(bad: object) -> None:
    with pytest.raises(TypeError):
        translate_access_file(bad)  # type: ignore[arg-type]


def test_missing_source_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        translate_access_file(tmp_path / "missing.accdt")


@pytest.mark.parametrize("suffix", [".mdb", ".accdb"])
def test_unreadable_binary_is_refused_without_inventing_objects(
    tmp_path: Path,
    suffix: str,
) -> None:
    """A binary file that is not a readable database yields no objects.

    Binary inputs are now opened - the catalog reader needs the bytes - but a
    file the reader cannot make sense of must produce an ``unsupported``
    result rather than a partial guess.
    """
    source = tmp_path / f"input{suffix}"
    source.write_bytes(b"\x00" * 8192)

    result = translate_access_file(source)
    assert result["status"] == "unsupported"
    assert result["ir"] is None
    assert result["diagnostics"][0]["reason_code"] == "BINARY_CATALOG_NOT_READABLE"


def test_invalid_zip_returns_failed_diagnostic(tmp_path: Path) -> None:
    source = tmp_path / "invalid.accdt"
    source.write_bytes(b"not a ZIP package")

    result = translate_access_file(source)
    assert result["status"] == "failed"
    assert result["ir"] is None
    assert result["diagnostics"][0]["reason_code"] == "INVALID_ACCDT_PACKAGE"


def test_unsafe_package_path_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.accdt"
    with zipfile.ZipFile(source, "w") as package:
        package.writestr("../escape.txt", "unsafe")

    result = translate_access_file(source)
    assert result["status"] == "failed"
    assert result["diagnostics"][0]["reason_code"] == "UNSAFE_PACKAGE_PATH"
    assert not (tmp_path / "escape.txt").exists()


def test_duplicate_package_entries_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "duplicate.accdt"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(source, "w") as package:
            package.writestr("template/template.xml", "<Template />")
            package.writestr("template/template.xml", "<Template />")

    result = translate_access_file(source)
    assert result["status"] == "failed"
    assert result["diagnostics"][0]["reason_code"] == "DUPLICATE_PACKAGE_ENTRY"


def test_encrypted_package_entry_is_rejected_before_reading(tmp_path: Path) -> None:
    source = tmp_path / "encrypted.accdt"
    _minimal_package(
        source,
        "template/database/objects/moduleSafe.txt",
        "Option Explicit\n",
    )
    payload = bytearray(source.read_bytes())
    for signature, flag_offset in [(b"PK\x03\x04", 6), (b"PK\x01\x02", 8)]:
        start = 0
        while True:
            position = payload.find(signature, start)
            if position < 0:
                break
            flags = int.from_bytes(
                payload[position + flag_offset:position + flag_offset + 2],
                "little",
            )
            payload[position + flag_offset:position + flag_offset + 2] = (
                flags | 1
            ).to_bytes(2, "little")
            start = position + len(signature)
    source.write_bytes(payload)

    result = translate_access_file(source)
    assert result["status"] == "failed"
    assert result["diagnostics"][0]["reason_code"] == "ENCRYPTED_PACKAGE_ENTRY"


def test_package_entry_size_limit_is_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "oversized.accdt"
    _minimal_package(
        source,
        "template/database/objects/moduleLarge.txt",
        "Option Explicit\n",
    )
    monkeypatch.setattr(translation, "_MAX_ENTRY_BYTES", 8)

    result = translate_access_file(source)
    assert result["status"] == "failed"
    assert result["diagnostics"][0]["reason_code"] == "PACKAGE_LIMIT_EXCEEDED"


def test_unsafe_xml_declarations_are_not_parsed(tmp_path: Path) -> None:
    source = tmp_path / "unsafe-xml.accdt"
    _minimal_package(
        source,
        "template/database/objects/tableUnsafe.xsd",
        "<!DOCTYPE schema [<!ENTITY x 'unsafe'>]><schema>&x;</schema>",
    )

    result = translate_access_file(source)
    unit = result["ir"]["samples"][0]["artifacts"][0]["objects"][0]
    assert unit["stages"]["extraction"]["reason_code"] == "SOURCE_DEFINITION_XML_UNSAFE"


def test_malformed_object_xml_is_retained_as_an_extraction_failure(
    tmp_path: Path,
) -> None:
    source = tmp_path / "malformed.accdt"
    _minimal_package(
        source,
        "template/database/objects/tableBroken.xsd",
        "<xsd:schema>",
    )

    result = translate_access_file(source)
    artifact = result["ir"]["samples"][0]["artifacts"][0]
    unit = artifact["objects"][0]
    assert result["status"] == "partial"
    assert unit["name"] == "Broken"
    assert unit["stages"]["extraction"] == {
        "status": "failed",
        "reason_code": "SOURCE_DEFINITION_XML_INVALID",
    }
    assert result["coverage"]["corpus"]["stages"]["extraction"]["counts"]["failed"] == 1


def test_source_text_can_be_omitted_without_overclaiming_translation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "compact.accdt"
    _minimal_package(
        source,
        "template/database/objects/moduleCompact.txt",
        "Option Explicit\n",
    )

    result = translate_access_file(source, include_source_text=False)
    unit = result["ir"]["samples"][0]["artifacts"][0]["objects"][0]
    assert "source_text" not in unit["content"]
    assert unit["content"]["source_sha256"]
    assert unit["stages"]["extraction"]["status"] == "complete"
    assert unit["stages"]["translation"]["status"] == "not_started"


def test_cli_writes_utf8_json_atomically(tmp_path: Path) -> None:
    source = tmp_path / "small.accdt"
    output = tmp_path / "result.json"
    _minimal_package(
        source,
        "template/database/objects/moduleMódulo.txt",
        "Option Explicit\nPublic Function Café()\nEnd Function\n",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "converter",
            "translate",
            str(source),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "partial"
    assert result["ir"]["samples"][0]["artifacts"][0]["objects"][0]["name"] == "Módulo"
    assert not list(tmp_path.glob("*.tmp"))
