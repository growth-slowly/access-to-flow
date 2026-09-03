"""Strict offline translation of inventory JSON into canonical Access IR."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ._errors import AccessIRValidationError
from ._vocab import (KINDS, REASON_COUNT_ONLY, REASON_EXTRACTION_NOT_ATTEMPTED,
    REASON_TRANSLATION_NOT_ATTEMPTED, SCHEMA, bucket_for_count_key,
    bucket_for_object_kind, bucket_identity, canonical_bucket_order,
    classify_format_family, flags_dict)

__all__ = ["load_inventory_corpus"]
_LISTS = (("tables", "table"), ("queries", "query"), ("forms", "form"),
          ("reports", "report"), ("macros", "macro"),
          ("data_macros", "macro_data"), ("modules", "module"))
_ACCDT_DESCRIPTION = ("ACE Access deployable template package (.accdt, OOXML/ZIP); "
                      "object inventory extracted from template/database/objects/*")


def _root(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview, bool, int, float)):
        raise TypeError("load_inventory_corpus() expects str or os.PathLike[str]")
    if hasattr(value, "__fspath__"):
        result = os.fspath(value)
        if isinstance(result, str):
            return result
    raise TypeError("load_inventory_corpus() expects str or os.PathLike[str]")


def _text(value: Any, field: str, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise AccessIRValidationError(
            f"missing required identity field {field!r} in {where}: expected a non-empty string")
    return value


def _count(value: Any, key: str, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AccessIRValidationError(
            f"invalid declared count for {key!r} in {where}: expected a non-negative integer")
    return value


def _stages(count_only: bool) -> dict[str, Any]:
    if count_only:
        extraction = translation = {
            "status": "unsupported", "reason_code": REASON_COUNT_ONLY}
    else:
        extraction = {"status": "not_started",
                      "reason_code": REASON_EXTRACTION_NOT_ATTEMPTED}
        translation = {"status": "not_started",
                       "reason_code": REASON_TRANSLATION_NOT_ATTEMPTED}
    return {"discovery": {"status": "complete"},
            "extraction": dict(extraction), "translation": dict(translation)}


def _object(name: str, bucket: str, index: int, provenance: str) -> dict[str, Any]:
    kind, subtype, flag_names, derived = bucket_identity(bucket)
    result = {"name": name, "kind": kind, "subtype": subtype,
              "flags": flags_dict(*flag_names), "source_index": index,
              "name_provenance": provenance, "stages": _stages(False)}
    if derived is not None:
        result["derived_from_kind"] = derived
    return result


def _aggregate(bucket: str, count: int) -> dict[str, Any]:
    kind, subtype, flag_names, derived = bucket_identity(bucket)
    result = {"kind": kind, "subtype": subtype, "flags": flags_dict(*flag_names),
              "count": count, "reason_code": REASON_COUNT_ONLY,
              "stages": _stages(True)}
    if derived is not None:
        result["derived_from_kind"] = derived
    return result


def _counts(raw: Any, where: str) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise AccessIRValidationError(f"malformed declared counts in {where}: counts must be an object")
    result: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise AccessIRValidationError(f"malformed count key {key!r} in {where}")
        bucket = bucket_for_count_key(key)
        if bucket is None:
            raise AccessIRValidationError(f"unknown object kind/count key {key!r} in {where}")
        result[bucket] = result.get(bucket, 0) + _count(value, key, where)
    return result


def _declared(counts: dict[str, int]) -> dict[str, int]:
    result = {kind: 0 for kind in KINDS}
    for bucket, value in counts.items():
        result[bucket_identity(bucket)[0]] += value
    return result


def _residuals(counts: dict[str, int], named: dict[str, int], where: str) -> list[dict[str, Any]]:
    result = []
    for bucket in canonical_bucket_order():
        residual = counts.get(bucket, 0) - named.get(bucket, 0)
        if residual < 0:
            raise AccessIRValidationError(
                f"inconsistent declared totals in {where}: named records exceed declared count")
        if residual:
            result.append(_aggregate(bucket, residual))
    return result


def _require_exact_counts(counts: dict[str, int], named: dict[str, int], where: str) -> None:
    """Require exhaustive inventory lists to equal declarations bucket by bucket."""
    discrepancies = {
        bucket: {"declared": counts.get(bucket, 0), "named": named.get(bucket, 0)}
        for bucket in canonical_bucket_order()
        if counts.get(bucket, 0) != named.get(bucket, 0)
    }
    if discrepancies:
        raise AccessIRValidationError(
            f"inconsistent declared totals in {where}: ACCDT object lists are exhaustive; "
            f"bucket discrepancies {discrepancies!r}")


def _binary(entry: Any, sample: str, source: str, where: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise AccessIRValidationError(f"malformed databases entry in {where}")
    name = _text(entry.get("file"), "file", where)
    place = f"{where} artifact {name!r}"
    description = entry.get("format")
    if not isinstance(description, str) or not description:
        raise AccessIRValidationError(f"missing or empty artifact format in {place}")
    family = classify_format_family(description)
    if family is None:
        raise AccessIRValidationError(f"unknown Access format description {description!r} in {place}")
    counts = _counts(entry.get("counts"), place)
    raw_objects = entry.get("objects")
    if not isinstance(raw_objects, list):
        raise AccessIRValidationError(f"malformed objects list in {place}")
    objects, named = [], {}
    for index, raw in enumerate(raw_objects):
        if not isinstance(raw, dict):
            raise AccessIRValidationError(f"malformed object #{index} in {place}")
        object_name = _text(raw.get("name"), "name", f"{place} object #{index}")
        raw_kind = raw.get("kind")
        if not isinstance(raw_kind, str) or not raw_kind:
            raise AccessIRValidationError(f"missing object kind in {place} object #{index}")
        bucket = bucket_for_object_kind(raw_kind)
        if bucket is None:
            raise AccessIRValidationError(f"unknown object kind {raw_kind!r} in {place}")
        named[bucket] = named.get(bucket, 0) + 1
        objects.append(_object(object_name, bucket, index, "jet_catalog_msysobjects"))
    return {"sample_id": sample, "artifact_name": name,
            "source_identity": f"{source}#{name}", "format_family": family,
            "format_description": description,
            "format_description_provenance": "source_inventory",
            "declared_counts": _declared(counts), "objects": objects,
            "count_only": _residuals(counts, named, place)}


def _accdt_identity(directory: Path, where: str) -> tuple[str, str]:
    """Return the display name and authoritative sample-relative ACCDT path.

    The corpus convention is that a template below the immediate ``original``
    directory is authoritative over archival copies.  Without exactly one such
    candidate, exactly one ACCDT must exist in the sample.  Candidate paths are
    retained (not collapsed by basename), and artifacts are never opened.
    """
    candidates = sorted(
        (p for p in directory.rglob("*")
         if p.suffix.casefold() == ".accdt" and p.is_file()),
        key=lambda p: p.relative_to(directory).as_posix())
    relative = [(p, p.relative_to(directory)) for p in candidates]
    originals = [(p, rel) for p, rel in relative
                 if rel.parts and rel.parts[0].casefold() == "original"]
    if len(originals) == 1:
        selected, selected_relative = originals[0]
    elif not originals and len(relative) == 1:
        selected, selected_relative = relative[0]
    else:
        relative_candidates = [rel.as_posix() for _, rel in relative]
        raise AccessIRValidationError(
            "missing or ambiguous ACCDT artifact identity in "
            f"{where}: found {relative_candidates!r}")
    return selected.name, selected_relative.as_posix()


def _accdt(data: dict[str, Any], sample: str, directory: Path,
           source: str, where: str) -> dict[str, Any]:
    name, artifact_path = _accdt_identity(directory, where)
    place = f"{where} artifact {name!r}"
    counts = _counts(data.get("counts"), place)
    objects, named, index = [], {}, 0
    for key, bucket in _LISTS:
        values = data.get(key)
        if not isinstance(values, list):
            raise AccessIRValidationError(f"malformed inventory {place}: {key!r} must be a list")
        for position, value in enumerate(values):
            object_name = _text(value, "name", f"{place} {key}[{position}]")
            named[bucket] = named.get(bucket, 0) + 1
            objects.append(_object(object_name, bucket, index, "accdt_package_path"))
            index += 1
    # Unlike binary catalog inventories, ACCDT's top-level lists are defined
    # as exhaustive. A mismatch is malformed input, never an identity-less
    # residual that the converter may silently turn into count-only work.
    _require_exact_counts(counts, named, place)
    return {"sample_id": sample, "artifact_name": name,
            "source_identity": f"{source}#{artifact_path}", "format_family": "accdt",
            "format_description": _ACCDT_DESCRIPTION,
            "format_description_provenance": "derived_from_inventory_layout",
            "artifact_name_provenance": "sample_directory_template_file",
            "declared_counts": _declared(counts), "objects": objects,
            "count_only": []}


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AccessIRValidationError(f"malformed inventory JSON in {path.name}: {exc}") from exc


def _sample(path: Path, root: Path) -> dict[str, Any]:
    source = path.relative_to(root).as_posix()
    where = f"inventory {source!r}"
    data = _read(path)
    if not isinstance(data, dict):
        raise AccessIRValidationError(f"unknown inventory shape in {where}")
    sample = _text(data.get("sample"), "sample", where)
    databases = data.get("databases")
    if isinstance(databases, list):
        artifacts = [_binary(entry, sample, source, where) for entry in databases]
    elif "counts" in data and all(key in data for key, _ in _LISTS):
        artifacts = [_accdt(data, sample, path.parent, source, where)]
    else:
        raise AccessIRValidationError(f"unknown inventory shape/layout in {where}")
    names = [item["artifact_name"] for item in artifacts]
    if len(names) != len(set(names)):
        raise AccessIRValidationError(f"duplicate artifact identity in {where}")
    artifacts.sort(key=lambda item: item["artifact_name"])
    return {"sample_id": sample, "source_identity": source, "artifacts": artifacts}


def load_inventory_corpus(root: "str | os.PathLike[str]") -> dict[str, object]:
    """Load all immediate ``*/object_inventory.json`` files, atomically."""
    directory = Path(_root(root))
    if not directory.exists():
        raise FileNotFoundError(f"inventory corpus root does not exist: {directory}")
    if not directory.is_dir():
        raise AccessIRValidationError(f"inventory corpus root is not a directory: {directory}")
    paths = sorted((p for p in directory.glob("*/object_inventory.json") if p.is_file()),
                   key=lambda p: p.relative_to(directory).as_posix())
    if not paths:
        raise AccessIRValidationError(f"empty corpus: no object_inventory.json files under {directory}")
    samples, seen = [], {}
    for path in paths:
        sample = _sample(path, directory)
        sample_id = sample["sample_id"]
        if sample_id in seen:
            raise AccessIRValidationError(f"duplicate sample identity {sample_id!r}")
        seen[sample_id] = sample["source_identity"]
        samples.append(sample)
    samples.sort(key=lambda item: item["sample_id"])
    return {"schema": SCHEMA, "samples": samples}
