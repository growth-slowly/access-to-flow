"""Validation and deterministic coverage reporting for ``access-ir/1``."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

from ._errors import AccessIRValidationError
from ._vocab import (
    ELIGIBLE_STATUSES,
    FORMAT_FAMILIES,
    INCOMPLETE_STATUSES,
    KINDS,
    KNOWN_UNMODELED_SCOPE,
    REASON_CODE_MEANINGS,
    REASON_COUNT_ONLY,
    SCHEMA,
    STAGES,
    STATUSES,
    UNIT_WEIGHTING,
)

__all__ = ["build_coverage_report"]


class _Accumulator:
    __slots__ = ("counts", "reasons")

    def __init__(self) -> None:
        self.counts = {stage: {status: 0 for status in STATUSES} for stage in STAGES}
        self.reasons: Dict[str, Dict[str, int]] = {stage: {} for stage in STAGES}

    def add(self, stage: str, status: str, reason: "str | None", weight: int) -> None:
        self.counts[stage][status] += weight
        if status in INCOMPLETE_STATUSES:
            assert reason is not None
            self.reasons[stage][reason] = self.reasons[stage].get(reason, 0) + weight

    def stages(self) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for stage in STAGES:
            counts = dict(self.counts[stage])
            eligible = sum(counts[status] for status in ELIGIBLE_STATUSES)
            result[stage] = {
                "counts": counts,
                "eligible": eligible,
                "completion_percentage": (
                    round(counts["complete"] / eligible * 100, 2) if eligible else 0.0
                ),
                "reason_codes": {
                    reason: self.reasons[stage][reason]
                    for reason in sorted(self.reasons[stage])
                },
            }
        return result


def _text(value: Any, field: str, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise AccessIRValidationError(
            f"malformed IR {where}: {field!r} must be a non-empty string"
        )
    return value


def _read_stage(unit: Dict[str, Any], stage: str, where: str) -> Tuple[str, "str | None"]:
    stages = unit.get("stages")
    if not isinstance(stages, dict) or set(stages) != set(STAGES):
        raise AccessIRValidationError(
            f"malformed IR unit in {where}: stages must be exactly {STAGES!r}"
        )
    entry = stages.get(stage)
    if not isinstance(entry, dict):
        raise AccessIRValidationError(
            f"malformed IR unit in {where}: stage {stage!r} must be an object"
        )
    status = entry.get("status")
    if status not in STATUSES:
        raise AccessIRValidationError(
            f"unknown translation status {status!r} for stage {stage!r} in {where}"
        )
    reason = entry.get("reason_code")
    if status in INCOMPLETE_STATUSES:
        if not isinstance(reason, str) or not reason:
            raise AccessIRValidationError(
                f"status {status!r} for stage {stage!r} in {where} must carry a "
                "non-empty, machine-readable reason code"
            )
        return status, reason
    return status, None


def _validate_flags(unit: Dict[str, Any], where: str) -> None:
    flags = unit.get("flags")
    expected = {"linked", "odbc_linked", "hidden"}
    if not isinstance(flags, dict) or set(flags) != expected or any(
        not isinstance(flags[key], bool) for key in expected
    ):
        raise AccessIRValidationError(
            f"malformed IR unit in {where}: flags must contain exactly boolean "
            "linked, odbc_linked and hidden values"
        )


def _units(
    artifact: Dict[str, Any], where: str
) -> Iterable[Tuple[Dict[str, Any], str, int]]:
    objects = artifact.get("objects")
    aggregates = artifact.get("count_only")
    if not isinstance(objects, list) or not isinstance(aggregates, list):
        raise AccessIRValidationError(
            f"malformed IR artifact in {where}: 'objects' and 'count_only' must be lists"
        )
    for index, unit in enumerate(objects):
        unit_where = f"{where} named object #{index}"
        if not isinstance(unit, dict):
            raise AccessIRValidationError(f"malformed IR unit in {unit_where}")
        _text(unit.get("name"), "name", unit_where)
        source_index = unit.get("source_index")
        if isinstance(source_index, bool) or not isinstance(source_index, int) or source_index < 0:
            raise AccessIRValidationError(
                f"malformed IR unit in {unit_where}: source_index must be a non-negative integer"
            )
        kind = unit.get("kind")
        if kind not in KINDS:
            raise AccessIRValidationError(f"unknown object kind {kind!r} in {unit_where}")
        _validate_flags(unit, unit_where)
        yield unit, kind, 1
    for index, unit in enumerate(aggregates):
        unit_where = f"{where} count-only aggregate #{index}"
        if not isinstance(unit, dict):
            raise AccessIRValidationError(f"malformed IR unit in {unit_where}")
        if "name" in unit:
            raise AccessIRValidationError(
                f"malformed IR unit in {unit_where}: count-only aggregates cannot have names"
            )
        kind = unit.get("kind")
        if kind not in KINDS:
            raise AccessIRValidationError(f"unknown object kind {kind!r} in {unit_where}")
        weight = unit.get("count")
        if isinstance(weight, bool) or not isinstance(weight, int) or weight <= 0:
            raise AccessIRValidationError(
                f"invalid count-only aggregate weight {weight!r} in {unit_where}; "
                "it must be a positive integer"
            )
        if unit.get("reason_code") != REASON_COUNT_ONLY:
            raise AccessIRValidationError(
                f"malformed IR unit in {unit_where}: reason_code must be {REASON_COUNT_ONLY!r}"
            )
        _validate_flags(unit, unit_where)
        yield unit, kind, weight


def _declared_counts(artifact: Dict[str, Any], where: str) -> Dict[str, int]:
    declared = artifact.get("declared_counts")
    if not isinstance(declared, dict) or set(declared) != set(KINDS):
        raise AccessIRValidationError(
            f"malformed IR artifact in {where}: declared_counts must contain exactly {KINDS!r}"
        )
    result: Dict[str, int] = {}
    for kind in KINDS:
        value = declared[kind]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AccessIRValidationError(
                f"invalid declared count {value!r} for kind {kind!r} in {where}"
            )
        result[kind] = value
    return result


def build_coverage_report(corpus: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a corpus and report weighted status coverage at every scope."""
    if not isinstance(corpus, dict):
        raise AccessIRValidationError(
            f"coverage input must be an access-ir corpus object, got {type(corpus).__name__}"
        )
    if corpus.get("schema") != SCHEMA:
        raise AccessIRValidationError(
            f"coverage input schema must be {SCHEMA!r}, got {corpus.get('schema')!r}"
        )
    samples = corpus.get("samples")
    if not isinstance(samples, list):
        raise AccessIRValidationError("coverage input must contain a 'samples' list")

    corpus_acc = _Accumulator()
    kind_acc = {kind: _Accumulator() for kind in KINDS}
    sample_scopes: List[Dict[str, Any]] = []
    seen_samples: set[str] = set()
    seen_sample_sources: set[str] = set()
    seen_artifact_sources: set[str] = set()

    ordered_samples: List[Tuple[str, Dict[str, Any]]] = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise AccessIRValidationError("malformed IR sample: expected an object")
        sample_id = _text(sample.get("sample_id"), "sample_id", "sample")
        source = _text(sample.get("source_identity"), "source_identity", f"sample {sample_id!r}")
        if sample_id in seen_samples or source in seen_sample_sources:
            raise AccessIRValidationError(f"duplicate sample identity in sample {sample_id!r}")
        seen_samples.add(sample_id)
        seen_sample_sources.add(source)
        ordered_samples.append((sample_id, sample))

    for sample_id, sample in sorted(ordered_samples, key=lambda item: item[0]):
        artifacts = sample.get("artifacts")
        if not isinstance(artifacts, list):
            raise AccessIRValidationError(
                f"malformed IR sample {sample_id!r}: missing an 'artifacts' list"
            )
        names: set[str] = set()
        ordered_artifacts: List[Tuple[str, Dict[str, Any]]] = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise AccessIRValidationError(f"malformed IR artifact in sample {sample_id!r}")
            name = _text(artifact.get("artifact_name"), "artifact_name", f"sample {sample_id!r}")
            if name in names:
                raise AccessIRValidationError(
                    f"duplicate artifact identity {name!r} in sample {sample_id!r}"
                )
            names.add(name)
            ordered_artifacts.append((name, artifact))

        sample_acc = _Accumulator()
        artifact_scopes: List[Dict[str, Any]] = []
        for artifact_name, artifact in sorted(ordered_artifacts, key=lambda item: item[0]):
            where = f"sample {sample_id!r} artifact {artifact_name!r}"
            if artifact.get("sample_id") != sample_id:
                raise AccessIRValidationError(f"artifact sample identity mismatch in {where}")
            source = _text(artifact.get("source_identity"), "source_identity", where)
            if source in seen_artifact_sources:
                raise AccessIRValidationError(f"duplicate artifact source identity {source!r}")
            seen_artifact_sources.add(source)
            family = artifact.get("format_family")
            if family not in FORMAT_FAMILIES:
                raise AccessIRValidationError(f"unknown format family {family!r} in {where}")
            _text(artifact.get("format_description"), "format_description", where)
            declared = _declared_counts(artifact, where)
            actual = {kind: 0 for kind in KINDS}
            artifact_acc = _Accumulator()
            for unit, kind, weight in _units(artifact, where):
                actual[kind] += weight
                for stage in STAGES:
                    status, reason = _read_stage(unit, stage, where)
                    artifact_acc.add(stage, status, reason, weight)
                    sample_acc.add(stage, status, reason, weight)
                    corpus_acc.add(stage, status, reason, weight)
                    kind_acc[kind].add(stage, status, reason, weight)
            if actual != declared:
                raise AccessIRValidationError(
                    f"inconsistent declared totals in {where}: declared {declared!r}, "
                    f"but named and count-only units total {actual!r}"
                )
            artifact_scopes.append({
                "sample_id": sample_id,
                "artifact_name": artifact_name,
                "format_family": family,
                "declared_counts": dict(declared),
                "stages": artifact_acc.stages(),
            })
        sample_scopes.append({
            "sample_id": sample_id,
            "stages": sample_acc.stages(),
            "artifacts": artifact_scopes,
        })

    return {
        "schema": SCHEMA,
        "unit_weighting": UNIT_WEIGHTING,
        "percentage_formula": "complete / eligible * 100, eligible excludes only not_applicable",
        "partial_credit": "none; a partial status earns no fractional completion credit",
        "known_unmodeled_scope": list(KNOWN_UNMODELED_SCOPE),
        "reason_code_meanings": {
            key: REASON_CODE_MEANINGS[key] for key in sorted(REASON_CODE_MEANINGS)
        },
        "corpus": {"stages": corpus_acc.stages()},
        "samples": sample_scopes,
        "object_kinds": [
            {"kind": kind, "stages": kind_acc[kind].stages()} for kind in KINDS
        ],
    }
