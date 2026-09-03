"""Offline Access-file translation with truthful partial-result reporting."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import IO, Any, NamedTuple
from xml.etree import ElementTree

from . import ace_catalog, jet_catalog
from ..ir import SCHEMA as IR_SCHEMA
from ..ir import build_coverage_report
from ..semantics import translate_objects
from ..ir._vocab import (
    REASON_SOURCE_DECODE_FAILED,
    REASON_SOURCE_PRESERVED,
    REASON_SOURCE_UNAVAILABLE,
    REASON_SOURCE_XML_INVALID,
    REASON_SOURCE_XML_UNSAFE,
    empty_flags,
)

__all__ = ["translate_access_file", "translate_access_bytes"]

RESULT_SCHEMA = "access-conversion-result/1"
_OBJECT_ROOT = "template/database/objects/"
_MANIFEST = "template/template.xml"
_RELATIONSHIPS = "template/database/relationships.xml"
_MAX_ENTRIES = 5_000
_MAX_ENTRY_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 250


class _ObjectPart(NamedTuple):
    kind: str
    subtype: str | None
    name: str
    representation: str
    validate_xml: bool


class _PackageError(Exception):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


_DIRECT_PARTS = (
    ("table", ".xsd", "table", None, "xml_schema", True),
    ("query", ".txt", "query", None, "access_text_definition", False),
    ("form", ".txt", "form", None, "access_text_definition", False),
    ("report", ".txt", "report", None, "access_text_definition", False),
    ("macro", ".txt", "macro", None, "access_macro_text", False),
    ("module", ".txt", "module", None, "vba_source", False),
)


def _path(value: Any) -> Path:
    if isinstance(value, str):
        return Path(value)
    if isinstance(value, (bytes, bytearray, memoryview, bool, int, float)):
        raise TypeError("translate_access_file() expects str or os.PathLike[str]")
    if hasattr(value, "__fspath__"):
        converted = os.fspath(value)
        if isinstance(converted, str):
            return Path(converted)
    raise TypeError("translate_access_file() expects str or os.PathLike[str]")


class _Source:
    """One Access input, identified independently of where its bytes live.

    A file on disk and an upload held in memory differ only in how their bytes
    are reached.  Keeping that difference inside one small object is what lets
    the web service convert an uploaded database without ever writing it to
    disk - there is no temporary file to leak, to forget to delete, or to be
    read by anything else on the host.
    """

    __slots__ = ("name", "size_bytes", "sha256", "_opener")

    def __init__(
        self,
        name: str,
        size_bytes: int,
        sha256: str,
        opener: "Any",
    ) -> None:
        self.name = name
        self.size_bytes = size_bytes
        self.sha256 = sha256
        self._opener = opener

    @property
    def stem(self) -> str:
        return PurePosixPath(self.name).stem

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.name).suffix

    def open(self) -> IO[bytes]:
        return self._opener()

    @classmethod
    def from_path(cls, path: Path) -> "_Source":
        return cls(
            path.name,
            path.stat().st_size,
            _sha256_file(path),
            lambda: path.open("rb"),
        )

    @classmethod
    def from_bytes(cls, data: bytes, filename: str) -> "_Source":
        digest = hashlib.sha256(data).hexdigest()
        # ``io.BytesIO`` over the same buffer: no copy per open, no disk.
        return cls(
            PurePosixPath(filename).name,
            len(data),
            digest,
            lambda: io.BytesIO(data),
        )


def _source_summary(source: _Source, family: str | None) -> dict[str, Any]:
    return {
        "file_name": source.name,
        "size_bytes": source.size_bytes,
        "format_family": family,
    }


def _diagnostic(reason_code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": "failed",
        "reason_code": reason_code,
        "message": message,
        **extra,
    }


def _terminal_result(
    source: _Source,
    *,
    status: str,
    reason_code: str,
    message: str,
    family: str | None,
) -> dict[str, Any]:
    diagnostic = _diagnostic(reason_code, message)
    if status == "unsupported":
        diagnostic["status"] = "unsupported"
    return {
        "schema": RESULT_SCHEMA,
        "status": status,
        "source": _source_summary(source, family),
        "ir": None,
        "coverage": None,
        "semantics": None,
        "completion_summary": None,
        "package_summary": None,
        "unprocessed_features": [],
        "diagnostics": [diagnostic],
    }


def _sha256_file(source: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _validate_entry_name(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or ".." in path.parts
        or (path.parts and ":" in path.parts[0])
    ):
        raise _PackageError(
            "UNSAFE_PACKAGE_PATH",
            f"unsafe ACCDT package entry path: {name!r}",
        )


def _validate_entries(infos: list[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
    if len(infos) > _MAX_ENTRIES:
        raise _PackageError(
            "PACKAGE_LIMIT_EXCEEDED",
            f"ACCDT package has {len(infos)} entries; limit is {_MAX_ENTRIES}",
        )
    files = [info for info in infos if not info.is_dir()]
    seen: set[str] = set()
    total = 0
    for info in files:
        _validate_entry_name(info.filename)
        identity = info.filename.casefold()
        if identity in seen:
            raise _PackageError(
                "DUPLICATE_PACKAGE_ENTRY",
                f"duplicate ACCDT package entry: {info.filename!r}",
            )
        seen.add(identity)
        if info.flag_bits & 0x1:
            raise _PackageError(
                "ENCRYPTED_PACKAGE_ENTRY",
                f"encrypted ACCDT package entry is not supported: {info.filename!r}",
            )
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type == stat.S_IFLNK:
            raise _PackageError(
                "UNSAFE_PACKAGE_ENTRY_TYPE",
                f"symbolic-link ACCDT package entry is not supported: {info.filename!r}",
            )
        if info.file_size > _MAX_ENTRY_BYTES:
            raise _PackageError(
                "PACKAGE_LIMIT_EXCEEDED",
                f"ACCDT package entry exceeds size limit: {info.filename!r}",
            )
        total += info.file_size
        if total > _MAX_TOTAL_BYTES:
            raise _PackageError(
                "PACKAGE_LIMIT_EXCEEDED",
                "ACCDT package exceeds the total uncompressed size limit",
            )
        compressed = max(info.compress_size, 1)
        if info.file_size > 1024 * 1024 and info.file_size / compressed > _MAX_COMPRESSION_RATIO:
            raise _PackageError(
                "PACKAGE_LIMIT_EXCEEDED",
                f"ACCDT package entry has an unsafe compression ratio: {info.filename!r}",
            )
    return sorted(files, key=lambda item: item.filename)


def _read_entry(package: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    payload = package.read(info)
    if len(payload) != info.file_size:
        raise _PackageError(
            "PACKAGE_ENTRY_SIZE_MISMATCH",
            f"ACCDT package entry size changed while reading: {info.filename!r}",
        )
    return payload


def _decode(payload: bytes) -> tuple[str, str]:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16"), "utf-16"
    if payload.startswith(b"\xef\xbb\xbf"):
        return payload.decode("utf-8-sig"), "utf-8"
    return payload.decode("utf-8"), "utf-8"


def _parse_safe_xml(text: str) -> None:
    lowered = text.casefold()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise _PackageError(
            REASON_SOURCE_XML_UNSAFE,
            "XML document type and entity declarations are not supported",
        )
    ElementTree.fromstring(text)


def _object_part(path: str) -> _ObjectPart | None:
    if not path.startswith(_OBJECT_ROOT):
        return None
    relative = path[len(_OBJECT_ROOT):]
    parts = relative.split("/")
    if len(parts) == 2 and parts[0].casefold() == "datamacros":
        filename = parts[1]
        lowered = filename.casefold()
        prefix = "datamacros"
        suffix = ".axl"
        if lowered.startswith(prefix) and lowered.endswith(suffix):
            name = filename[len(prefix):-len(suffix)]
            if name:
                return _ObjectPart(
                    "macro", "data_macro", name,
                    "access_data_macro_xml", True,
                )
        return None
    if len(parts) != 1:
        return None
    filename = parts[0]
    lowered = filename.casefold()
    for prefix, suffix, kind, subtype, representation, validate_xml in _DIRECT_PARTS:
        if lowered.startswith(prefix) and lowered.endswith(suffix):
            name = filename[len(prefix):-len(suffix)]
            if name:
                return _ObjectPart(
                    kind, subtype, name, representation, validate_xml,
                )
    return None


def _stages(extraction: str, extraction_reason: str | None) -> dict[str, Any]:
    extraction_stage: dict[str, Any] = {"status": extraction}
    if extraction_reason is not None:
        extraction_stage["reason_code"] = extraction_reason
    if extraction == "complete":
        translation = {
            "status": "partial",
            "reason_code": REASON_SOURCE_PRESERVED,
        }
    else:
        translation = {
            "status": "not_started",
            "reason_code": REASON_SOURCE_UNAVAILABLE,
        }
    return {
        "discovery": {"status": "complete"},
        "extraction": extraction_stage,
        "translation": translation,
    }


def _definition_object(
    package: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    part: _ObjectPart,
    source_index: int,
    include_source_text: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    payload = _read_entry(package, info)
    content: dict[str, Any] = {
        "source_part": info.filename,
        "representation": part.representation,
        "source_bytes": len(payload),
        "source_sha256": hashlib.sha256(payload).hexdigest(),
    }
    extraction = "complete"
    reason: str | None = None
    diagnostic = None
    try:
        text, encoding = _decode(payload)
        content["encoding"] = encoding
        if include_source_text:
            content["source_text"] = text
        if part.validate_xml:
            _parse_safe_xml(text)
    except UnicodeDecodeError:
        extraction = "failed"
        reason = REASON_SOURCE_DECODE_FAILED
        content["encoding"] = None
    except ElementTree.ParseError:
        extraction = "failed"
        reason = REASON_SOURCE_XML_INVALID
    except _PackageError as exc:
        extraction = "failed"
        reason = exc.reason_code

    if reason is not None:
        diagnostic = _diagnostic(
            reason,
            "source definition could not be safely validated",
            scope="object",
            source_part=info.filename,
        )

    result = {
        "name": part.name,
        "kind": part.kind,
        "subtype": part.subtype,
        "flags": empty_flags(),
        "source_index": source_index,
        "name_provenance": "accdt_package_part_prefix",
        "content": content,
        "stages": _stages(extraction, reason),
    }
    if part.subtype == "data_macro":
        result["derived_from_kind"] = "table"
    return result, diagnostic


def _feature_for_entry(path: str) -> str:
    lowered = path.casefold()
    if "/sampledata/" in lowered:
        return "table_data"
    if "/properties/" in lowered:
        return "object_properties"
    if lowered == "template/database/relationships.xml":
        return "relationships"
    if lowered == "template/database/navpane.xml":
        return "navigation_pane"
    if lowered == "template/database/databaseproperties.xml":
        return "database_properties"
    if lowered == "template/database/vbareferences.xml":
        return "vba_references"
    if "/resources/" in lowered:
        return "embedded_resources"
    if "/_rels/" in lowered or lowered.endswith(".rels"):
        return "package_relationships"
    return "package_metadata"


def _unprocessed_features(paths: list[str], object_count: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    for path in paths:
        grouped.setdefault(_feature_for_entry(path), []).append(path)
    reason_codes = {
        "table_data": "TABLE_DATA_TRANSLATION_NOT_IMPLEMENTED",
        "object_properties": "OBJECT_PROPERTIES_NORMALIZATION_NOT_IMPLEMENTED",
        "relationships": "RELATIONSHIP_TRANSLATION_NOT_IMPLEMENTED",
        "navigation_pane": "NAVIGATION_METADATA_TRANSLATION_NOT_IMPLEMENTED",
        "database_properties": "DATABASE_PROPERTIES_TRANSLATION_NOT_IMPLEMENTED",
        "vba_references": "VBA_REFERENCE_TRANSLATION_NOT_IMPLEMENTED",
        "embedded_resources": "EMBEDDED_RESOURCE_TRANSLATION_NOT_IMPLEMENTED",
        "package_relationships": "PACKAGE_RELATIONSHIP_NORMALIZATION_NOT_IMPLEMENTED",
        "package_metadata": "PACKAGE_METADATA_NORMALIZATION_NOT_IMPLEMENTED",
    }
    messages = {
        "table_data": "Table row data is present but has not been imported.",
        "object_properties": "Object property metadata has not been normalized.",
        "relationships": "Access relationships have not been translated.",
        "navigation_pane": "Navigation-pane metadata has not been translated.",
        "database_properties": "Database properties have not been translated.",
        "vba_references": "VBA library references have not been resolved.",
        "embedded_resources": "Embedded images and themes have not been translated.",
        "package_relationships": "OOXML relationship parts have not been normalized.",
        "package_metadata": "Package-level metadata is preserved only as an inventory.",
    }
    result = [
        {
            "feature": feature,
            "status": "not_started",
            "reason_code": reason_codes[feature],
            "message": messages[feature],
            "package_entry_count": len(grouped[feature]),
            "package_parts": sorted(grouped[feature]),
        }
        for feature in sorted(grouped)
    ]
    result.append(
        {
            "feature": "target_generation",
            "status": "not_started",
            "reason_code": "TARGET_GENERATOR_NOT_IMPLEMENTED",
            "message": "No target-language application has been generated.",
            "affected_object_count": object_count,
            "package_entry_count": 0,
            "package_parts": [],
        }
    )
    return result


def _translate_accdt(source: _Source, include_source_text: bool) -> dict[str, Any]:
    source_hash = source.sha256
    try:
        stream = source.open()
        with contextlib.closing(stream), zipfile.ZipFile(stream, "r") as package:
            infos = _validate_entries(package.infolist())
            by_name = {info.filename: info for info in infos}
            manifest = by_name.get(_MANIFEST)
            if manifest is None:
                raise _PackageError(
                    "INVALID_ACCDT_PACKAGE",
                    f"required ACCDT package part is missing: {_MANIFEST}",
                )
            manifest_text, _ = _decode(_read_entry(package, manifest))
            _parse_safe_xml(manifest_text)

            relationships_xml: str | None = None
            relationships_info = by_name.get(_RELATIONSHIPS)
            if relationships_info is not None:
                try:
                    relationships_xml, _ = _decode(
                        _read_entry(package, relationships_info)
                    )
                except UnicodeDecodeError:
                    relationships_xml = None

            objects: list[dict[str, Any]] = []
            diagnostics: list[dict[str, Any]] = []
            object_paths: set[str] = set()
            for info in infos:
                part = _object_part(info.filename)
                if part is None:
                    continue
                item, diagnostic = _definition_object(
                    package,
                    info,
                    part,
                    len(objects),
                    include_source_text,
                )
                objects.append(item)
                object_paths.add(info.filename)
                if diagnostic is not None:
                    diagnostics.append(diagnostic)
    except _PackageError as exc:
        return _terminal_result(
            source,
            status="failed",
            reason_code=exc.reason_code,
            message=str(exc),
            family="accdt",
        )
    except (zipfile.BadZipFile, ElementTree.ParseError, UnicodeDecodeError, RuntimeError) as exc:
        return _terminal_result(
            source,
            status="failed",
            reason_code="INVALID_ACCDT_PACKAGE",
            message=f"invalid ACCDT package structure: {type(exc).__name__}",
            family="accdt",
        )
    except OSError as exc:
        return _terminal_result(
            source,
            status="failed",
            reason_code="SOURCE_READ_FAILED",
            message=f"ACCDT package could not be read: {type(exc).__name__}",
            family="accdt",
        )

    if not objects:
        return _terminal_result(
            source,
            status="failed",
            reason_code="NO_ACCESS_OBJECT_DEFINITIONS",
            message="ACCDT package contains no recognized Access object definitions",
            family="accdt",
        )

    counts = {kind: 0 for kind in ("table", "query", "form", "report", "macro", "module")}
    for item in objects:
        counts[item["kind"]] += 1
    source_identity = f"sha256:{source_hash}#{source.name}"
    artifact = {
        "sample_id": source.stem,
        "artifact_name": source.name,
        "source_identity": source_identity,
        "format_family": "accdt",
        "format_description": "Access deployable template package (.accdt, OOXML/ZIP)",
        "format_description_provenance": "validated_package_layout",
        "artifact_name_provenance": "input_file_name",
        "declared_counts": counts,
        "objects": objects,
        "count_only": [],
    }
    ir = {
        "schema": IR_SCHEMA,
        "samples": [
            {
                "sample_id": source.stem,
                "source_identity": source_identity,
                "artifacts": [artifact],
            }
        ],
    }
    semantics = translate_objects(objects, relationships_xml=relationships_xml)
    coverage = build_coverage_report(ir)
    unprocessed_paths = [
        info.filename
        for info in infos
        if info.filename not in object_paths
        and info.filename not in {_MANIFEST, _RELATIONSHIPS}
    ]
    failed = coverage["corpus"]["stages"]["extraction"]["counts"]["failed"]
    extraction = coverage["corpus"]["stages"]["extraction"]
    translation = coverage["corpus"]["stages"]["translation"]
    source_summary = _source_summary(source, "accdt")
    source_summary.update({"sha256": source_hash, "source_identity": source_identity})
    return {
        "schema": RESULT_SCHEMA,
        "status": "partial",
        "source": source_summary,
        "ir": ir,
        "coverage": coverage,
        "semantics": semantics,
        "completion_summary": {
            "object_definitions": len(objects),
            "raw_extraction_complete": extraction["counts"]["complete"],
            "raw_extraction_failed": extraction["counts"]["failed"],
            "raw_extraction_completion_percentage": extraction[
                "completion_percentage"
            ],
            "semantic_translation_complete": translation["counts"]["complete"],
            "semantic_translation_partial": translation["counts"]["partial"],
            "semantic_translation_completion_percentage": translation[
                "completion_percentage"
            ],
            "semantic_objects_complete": semantics["totals"]["complete"],
            "semantic_objects_partial": semantics["totals"]["partial"],
            "semantic_objects_failed": semantics["totals"]["failed"],
            "relationships_translated": len(semantics["relationships"]),
        },
        "package_summary": {
            "file_entries": len(infos),
            "object_definition_entries": len(objects),
            "extracted_object_definitions": len(objects) - failed,
            "failed_object_definitions": failed,
            "unprocessed_entries": len(unprocessed_paths),
        },
        "unprocessed_features": _unprocessed_features(
            unprocessed_paths,
            len(objects),
        ),
        "diagnostics": diagnostics,
    }


#: Catalog kinds that are real Access objects, and how they map into the IR.
_CATALOG_KIND_MAP = {
    "table": ("table", None, ()),
    "linked_table": ("table", None, ("linked",)),
    "linked_odbc_table": ("table", None, ("odbc_linked",)),
    "query": ("query", None, ()),
    "form": ("form", None, ()),
    "report": ("report", None, ()),
    "macro": ("macro", None, ()),
    "module": ("module", None, ()),
}

#: Catalog rows that describe the database itself rather than a user object.
_CATALOG_INFRASTRUCTURE = {
    "database", "container", "relationship", "users", "database_document",
    "data_access_page",
}

_BINARY_EXTRACTION_REASON = "BINARY_OBJECT_DEFINITION_EXTRACTION_NOT_IMPLEMENTED"


def _is_system_object(name: str) -> bool:
    """System catalog rows Access itself hides from the Navigation Pane."""
    return name.startswith("MSys") or name.startswith("f_") or name.startswith("~TMPCLP")


def _binary_object(entry: dict[str, Any], index: int) -> dict[str, Any] | None:
    kind_info = _CATALOG_KIND_MAP.get(str(entry["kind"]))
    if kind_info is None:
        return None
    kind, subtype, flag_names = kind_info
    name = str(entry["name"])
    flags = empty_flags()
    for flag in flag_names:
        flags[flag] = True
    if kind == "query" and name.startswith("~"):
        subtype = "hidden_query"
        flags["hidden"] = True
    item: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "subtype": subtype,
        "flags": flags,
        "source_index": index,
        "name_provenance": "msysobjects_catalog_row",
        "content": {
            "source_part": "MSysObjects",
            "representation": "catalog_row",
            "type_code": entry["type_code"],
        },
        "stages": {
            "discovery": {"status": "complete"},
            "extraction": {
                "status": "not_started",
                "reason_code": _BINARY_EXTRACTION_REASON,
            },
            "translation": {
                "status": "not_started",
                "reason_code": "SOURCE_DEFINITION_UNAVAILABLE",
            },
        },
    }
    if subtype == "hidden_query":
        item["derived_from_kind"] = "form_or_report"
    return item


def _translate_binary(path: Path) -> dict[str, Any]:
    """Read a Jet3/Jet4/ACE binary catalog and report a discovery-level result.

    A name list is not a translation and is never presented as one: every
    object leaves this function with ``extraction`` and ``translation`` at
    ``not_started`` and a reason code that says why.
    """
    source = _Source.from_path(path)
    source_hash = source.sha256
    errors: list[str] = []
    catalog: dict[str, Any] | None = None
    for reader, family in (
        (ace_catalog.read_catalog, "jet4_or_ace"),
        (jet_catalog.read_catalog, "jet3"),
    ):
        try:
            catalog = reader(path)
            break
        except (ace_catalog.AceCatalogError, jet_catalog.JetCatalogError) as error:
            errors.append(f"{family}: {error}")
        except OSError as error:
            return _terminal_result(
                source,
                status="failed",
                reason_code="SOURCE_READ_FAILED",
                message=f"database could not be read: {type(error).__name__}",
                family=None,
            )
    if catalog is None:
        return _terminal_result(
            source,
            status="unsupported",
            reason_code="BINARY_CATALOG_NOT_READABLE",
            message="; ".join(errors)[:400],
            family=None,
        )

    entries = list(catalog["objects"])
    objects: list[dict[str, Any]] = []
    system_entries = 0
    infrastructure_entries = 0
    for entry in entries:
        name = str(entry["name"])
        if str(entry["kind"]) in _CATALOG_INFRASTRUCTURE:
            infrastructure_entries += 1
            continue
        if _is_system_object(name):
            system_entries += 1
            continue
        item = _binary_object(entry, len(objects))
        if item is not None:
            objects.append(item)

    counts = {kind: 0 for kind in ("table", "query", "form", "report", "macro", "module")}
    for item in objects:
        counts[item["kind"]] += 1

    family = str(catalog["format"])
    description = str(
        catalog.get("format_description")
        or "Jet 3.x (Access 95/97 .mdb)"
    )
    source_identity = f"sha256:{source_hash}#{source.name}"
    artifact = {
        "sample_id": source.stem,
        "artifact_name": source.name,
        "source_identity": source_identity,
        "format_family": family,
        "format_description": description,
        "format_description_provenance": "database_header_version_byte",
        "artifact_name_provenance": "input_file_name",
        "declared_counts": counts,
        "objects": objects,
        "count_only": [],
    }
    ir = {
        "schema": IR_SCHEMA,
        "samples": [
            {
                "sample_id": source.stem,
                "source_identity": source_identity,
                "artifacts": [artifact],
            }
        ],
    }
    coverage = build_coverage_report(ir)
    semantics = translate_objects(objects, relationships_xml=None)
    source_summary = _source_summary(source, family)
    source_summary.update({"sha256": source_hash, "source_identity": source_identity})
    return {
        "schema": RESULT_SCHEMA,
        "status": "partial",
        "source": source_summary,
        "ir": ir,
        "coverage": coverage,
        "semantics": semantics,
        "completion_summary": {
            "object_definitions": len(objects),
            "raw_extraction_complete": 0,
            "raw_extraction_failed": 0,
            "raw_extraction_completion_percentage": 0.0,
            "semantic_translation_complete": 0,
            "semantic_translation_partial": 0,
            "semantic_translation_completion_percentage": 0.0,
            "semantic_objects_complete": 0,
            "semantic_objects_partial": 0,
            "semantic_objects_failed": 0,
            "relationships_translated": 0,
        },
        "package_summary": {
            "catalog_rows": len(entries),
            "user_objects": len(objects),
            "system_catalog_rows": system_entries,
            "database_infrastructure_rows": infrastructure_entries,
        },
        "unprocessed_features": [
            {
                "feature": "binary_object_definitions",
                "status": "not_started",
                "reason_code": _BINARY_EXTRACTION_REASON,
                "message": (
                    "The catalog was read, so every object is known by name and "
                    "kind. Their definitions - table columns, query SQL, form "
                    "and report layout, macro actions and VBA source - live in "
                    "binary structures this converter does not yet decode, so "
                    "nothing here is a translation."
                ),
                "affected_object_count": len(objects),
                "package_entry_count": 0,
                "package_parts": [],
            },
            {
                "feature": "target_generation",
                "status": "not_started",
                "reason_code": "TARGET_GENERATOR_NOT_IMPLEMENTED",
                "message": "No target-language application has been generated.",
                "affected_object_count": len(objects),
                "package_entry_count": 0,
                "package_parts": [],
            },
        ],
        "diagnostics": [],
    }


def translate_access_file(
    source: "str | os.PathLike[str]",
    *,
    include_source_text: bool = True,
) -> dict[str, object]:
    """Translate one Access file without Access, COM, network, or execution.

    ACCDT packages are read in full: object definitions are extracted and then
    semantically translated.  Binary MDB/ACCDB files are read only as far as
    the ``MSysObjects`` catalog, which yields a complete object inventory and
    nothing more; that difference is reported per object rather than averaged
    away.
    """
    if not isinstance(include_source_text, bool):
        raise TypeError("include_source_text must be a bool")
    path = _path(source)
    if not path.exists():
        raise FileNotFoundError(f"Access input does not exist: {path.name}")
    if not path.is_file():
        raise IsADirectoryError(f"Access input is not a file: {path.name}")
    suffix = path.suffix.casefold()
    if suffix == ".accdt":
        return _translate_accdt(_Source.from_path(path), include_source_text)
    if suffix in {".mdb", ".accdb"}:
        return _translate_binary(path)
    return _terminal_result(
        _Source.from_path(path),
        status="unsupported",
        reason_code="UNSUPPORTED_INPUT_FORMAT",
        message=f"unsupported Access input extension: {suffix or '(none)'}",
        family=None,
    )


def translate_access_bytes(
    data: bytes,
    filename: str,
    *,
    include_source_text: bool = True,
) -> dict[str, object]:
    """Translate one Access package held in memory, touching no disk at all.

    This is the entry point the web service uses.  An uploaded database is
    converted from the bytes in the request and is never written anywhere: no
    temporary file, no cache, no spool.  When the process ends, or the request
    finishes and the buffer is released, nothing of the customer's database
    remains on the host.

    Only ACCDT packages are accepted here.  Binary MDB/ACCDB files are read by
    a page-level reader that seeks within a real file, so accepting them would
    mean writing the upload to disk - exactly the thing this function exists to
    avoid.  They are refused with a reason code rather than silently spooled.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("translate_access_bytes() expects bytes")
    if not isinstance(filename, str):
        raise TypeError("translate_access_bytes() expects a str filename")
    if not isinstance(include_source_text, bool):
        raise TypeError("include_source_text must be a bool")
    payload = bytes(data)
    source = _Source.from_bytes(payload, filename)
    suffix = source.suffix.casefold()
    if suffix == ".accdt":
        return _translate_accdt(source, include_source_text)
    if suffix in {".mdb", ".accdb"}:
        return _terminal_result(
            source,
            status="unsupported",
            reason_code="BINARY_INPUT_REQUIRES_A_FILE_PATH",
            message=(
                "binary Jet/ACE databases are read page by page from a real "
                "file; this in-memory entry point refuses them rather than "
                "writing the upload to disk"
            ),
            family="jet_or_ace_binary",
        )
    return _terminal_result(
        source,
        status="unsupported",
        reason_code="UNSUPPORTED_INPUT_FORMAT",
        message=f"unsupported Access input extension: {suffix or '(none)'}",
        family=None,
    )
