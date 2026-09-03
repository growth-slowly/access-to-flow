"""Additional ordering, rounding, format, and truthfulness regressions."""

from __future__ import annotations

import builtins
import io
from pathlib import Path

import pytest

from converter.ir import AccessIRValidationError, build_coverage_report, load_inventory_corpus

from conftest import all_count_only, all_named_objects, artifact_objects, write_json


def _write_empty_accdt_sample(
    root: Path,
    *candidate_paths: str,
) -> Path:
    sample = root / "accdt-sample"
    write_json(
        sample / "object_inventory.json",
        {
            "sample": "accdt-sample",
            "counts": {},
            "tables": [],
            "queries": [],
            "forms": [],
            "reports": [],
            "macros": [],
            "data_macros": [],
            "modules": [],
        },
    )
    for relative in candidate_paths:
        candidate = sample / Path(relative)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.touch()
    return sample


def test_accdt_identity_rejects_a_sample_without_a_candidate(tmp_path: Path) -> None:
    _write_empty_accdt_sample(tmp_path)

    with pytest.raises(AccessIRValidationError, match="(?i)missing|ambiguous|accdt"):
        load_inventory_corpus(tmp_path)


def test_accdt_identity_rejects_multiple_candidates_without_original(
    tmp_path: Path,
) -> None:
    _write_empty_accdt_sample(
        tmp_path,
        "archive/a.accdt",
        "backup/b.accdt",
    )

    with pytest.raises(AccessIRValidationError, match="(?i)ambiguous|accdt"):
        load_inventory_corpus(tmp_path)


def test_accdt_identity_rejects_multiple_candidates_under_original(
    tmp_path: Path,
) -> None:
    _write_empty_accdt_sample(
        tmp_path,
        "original/a.accdt",
        "original/nested/b.accdt",
    )

    with pytest.raises(AccessIRValidationError, match="(?i)ambiguous|accdt"):
        load_inventory_corpus(tmp_path)


def test_accdt_identity_does_not_collapse_distinct_paths_by_basename(
    tmp_path: Path,
) -> None:
    _write_empty_accdt_sample(
        tmp_path,
        "archive/template.accdt",
        "backup/template.accdt",
    )

    with pytest.raises(AccessIRValidationError) as error:
        load_inventory_corpus(tmp_path)

    message = str(error.value)
    assert "archive/template.accdt" in message
    assert "backup/template.accdt" in message


def test_accdt_source_identity_preserves_selected_sample_relative_path(
    tmp_path: Path,
) -> None:
    _write_empty_accdt_sample(
        tmp_path,
        "archive/template.accdt",
        "original/Nested Dir/Template Case.accdt",
    )

    corpus = load_inventory_corpus(tmp_path)
    artifact = corpus["samples"][0]["artifacts"][0]
    assert artifact["artifact_name"] == "Template Case.accdt"
    identity = artifact["source_identity"]
    assert "original/Nested Dir/Template Case.accdt" in identity
    assert "original/nested dir/template case.accdt" not in identity


def test_samples_are_sorted_and_source_identities_are_stable_and_distinct(
    binary_inventory_root: Path,
) -> None:
    write_json(
        binary_inventory_root / "a" / "object_inventory.json",
        {
            "sample": "a-sample",
            "databases": [
                {
                    "file": "C.accdb",
                    "format": "ACE 12 (.accdb)",
                    "counts": {"Table": 0},
                    "objects": [],
                }
            ],
        },
    )

    first = load_inventory_corpus(binary_inventory_root)
    second = load_inventory_corpus(binary_inventory_root)
    assert [sample["sample_id"] for sample in first["samples"]] == [
        "a-sample",
        "z_نمونه_Żółć_Пример_Straße",
    ]

    first_identities = [artifact["source_identity"] for artifact in artifact_objects(first)]
    second_identities = [artifact["source_identity"] for artifact in artifact_objects(second)]
    assert first_identities == second_identities
    assert len(first_identities) == len(set(first_identities))


def test_completion_percentage_rounds_to_two_decimal_places_without_losing_counts(
    binary_inventory_root: Path,
) -> None:
    corpus = load_inventory_corpus(binary_inventory_root)
    units = [*all_named_objects(corpus), *all_count_only(corpus)]
    assert len(units) == 5

    replacements = [
        {"status": "complete"},
        {"status": "partial", "reason_code": "TEST_PARTIAL"},
        {"status": "not_started", "reason_code": "TEST_NOT_STARTED"},
        {"status": "not_applicable"},
        {"status": "not_applicable"},
    ]
    for unit, stage in zip(units, replacements):
        unit["stages"]["translation"] = stage

    metric = build_coverage_report(corpus)["corpus"]["stages"]["translation"]
    assert metric["counts"] == {
        "complete": 1,
        "partial": 1,
        "unsupported": 0,
        "failed": 0,
        "not_started": 1,
        "not_applicable": 2,
    }
    assert metric["eligible"] == 3
    assert metric["completion_percentage"] == 33.33


def test_unknown_format_cannot_escape_the_canonical_format_vocabulary(tmp_path: Path) -> None:
    write_json(
        tmp_path / "unknown-format" / "object_inventory.json",
        {
            "sample": "unknown-format",
            "databases": [
                {
                    "file": "mystery.data",
                    "format": "Mystery Database 1.0",
                    "counts": {"Table": 0},
                    "objects": [],
                }
            ],
        },
    )

    with pytest.raises(AccessIRValidationError, match="(?i)format|family|inventory"):
        load_inventory_corpus(tmp_path)


def test_inventory_only_corpus_reports_no_content_extraction_or_translation_success(
    sample_root: Path,
) -> None:
    report = build_coverage_report(load_inventory_corpus(sample_root))
    scopes = [report["corpus"], *report["samples"], *report["object_kinds"]]
    scopes.extend(
        artifact
        for sample in report["samples"]
        for artifact in sample["artifacts"]
    )

    for scope in scopes:
        discovery = scope["stages"]["discovery"]
        assert discovery["counts"]["complete"] == discovery["eligible"]
        assert discovery["counts"]["failed"] == 0
        for stage_name in ("extraction", "translation"):
            metric = scope["stages"][stage_name]
            assert metric["completion_percentage"] == 0.0
            assert metric["counts"]["complete"] == 0
            assert metric["counts"]["failed"] == 0


def test_loading_corpus_never_opens_access_binaries(
    sample_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inventory translation may read JSON/catalog metadata, never MDB/ACCDB/ACCDT."""
    forbidden_suffixes = (".mdb", ".accdb", ".accda", ".accdt")
    builtin_open = builtins.open
    io_open = io.open

    def reject_access_with(delegate):
        def guarded(file, *args, **kwargs):
            if str(file).casefold().endswith(forbidden_suffixes):
                pytest.fail(f"loader attempted to open Access artifact: {file}")
            return delegate(file, *args, **kwargs)

        return guarded

    monkeypatch.setattr(builtins, "open", reject_access_with(builtin_open))
    monkeypatch.setattr(io, "open", reject_access_with(io_open))

    corpus = load_inventory_corpus(sample_root)
    assert len(corpus["samples"]) == 15
