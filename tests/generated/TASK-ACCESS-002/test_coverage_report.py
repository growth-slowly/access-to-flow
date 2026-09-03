"""Coverage arithmetic, hierarchy, and truthfulness tests for TASK-ACCESS-002."""

from __future__ import annotations

import json
from pathlib import Path

from converter.ir import build_coverage_report, load_inventory_corpus

from conftest import KINDS, STAGES, STATUSES, all_count_only, all_named_objects


def assert_metric(metric: dict[str, object]) -> None:
    counts = metric["counts"]
    assert set(counts) == set(STATUSES)
    assert all(isinstance(value, int) and value >= 0 for value in counts.values())
    assert metric["eligible"] == sum(
        counts[name]
        for name in ("complete", "partial", "unsupported", "failed", "not_started")
    )
    expected = (
        round(counts["complete"] / metric["eligible"] * 100, 2)
        if metric["eligible"]
        else 0.0
    )
    assert metric["completion_percentage"] == expected
    assert round(metric["completion_percentage"], 2) == metric["completion_percentage"]
    assert isinstance(metric["reason_codes"], dict)
    assert sum(metric["reason_codes"].values()) == sum(
        counts[name] for name in ("partial", "unsupported", "failed", "not_started")
    )


def assert_scope(scope: dict[str, object]) -> None:
    assert set(scope["stages"]) == set(STAGES)
    for metric in scope["stages"].values():
        assert_metric(metric)


def find_unqualified_overall_percentages(value: object) -> list[str]:
    problems: list[str] = []

    def visit(node: object, location: str) -> None:
        if isinstance(node, dict):
            lowered = {str(key).lower() for key in node}
            for key, child in node.items():
                key_text = str(key).lower()
                if "overall" in key_text and "percent" in key_text:
                    if "denominator" not in lowered or "weighting" not in lowered:
                        problems.append(f"{location}.{key}")
                visit(child, f"{location}.{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{location}[{index}]")

    visit(value, "report")
    return problems


def test_report_has_every_required_scope_and_valid_arithmetic(
    binary_inventory_root: Path,
) -> None:
    corpus = load_inventory_corpus(binary_inventory_root)
    report = build_coverage_report(corpus)

    assert report == build_coverage_report(corpus)
    assert json.loads(json.dumps(report, ensure_ascii=False)) == report
    assert_scope(report["corpus"])
    assert len(report["samples"]) == 1
    assert_scope(report["samples"][0])
    assert len(report["samples"][0]["artifacts"]) == 2
    for artifact_scope in report["samples"][0]["artifacts"]:
        assert_scope(artifact_scope)
    assert {scope["kind"] for scope in report["object_kinds"]} == set(KINDS)
    for kind_scope in report["object_kinds"]:
        assert_scope(kind_scope)
    assert not find_unqualified_overall_percentages(report)


def test_partial_unsupported_failed_and_not_started_are_not_conflated_or_fractional(
    binary_inventory_root: Path,
) -> None:
    corpus = load_inventory_corpus(binary_inventory_root)
    units = [*all_named_objects(corpus), *all_count_only(corpus)]
    assert len(units) == 5
    assigned = ("complete", "partial", "unsupported", "failed", "not_started")
    for unit, status in zip(units, assigned):
        stage = {"status": status}
        if status != "complete":
            stage["reason_code"] = "TEST_" + status.upper()
        unit["stages"]["translation"] = stage

    report = build_coverage_report(corpus)
    metric = report["corpus"]["stages"]["translation"]
    assert metric["counts"] == {
        "complete": 1,
        "partial": 1,
        "unsupported": 1,
        "failed": 1,
        "not_started": 1,
        "not_applicable": 0,
    }
    assert metric["eligible"] == 5
    assert metric["completion_percentage"] == 20.0
    assert metric["reason_codes"] == {
        "TEST_FAILED": 1,
        "TEST_NOT_STARTED": 1,
        "TEST_PARTIAL": 1,
        "TEST_UNSUPPORTED": 1,
    }


def test_not_applicable_is_the_only_status_excluded_from_denominator(
    binary_inventory_root: Path,
) -> None:
    corpus = load_inventory_corpus(binary_inventory_root)
    units = [*all_named_objects(corpus), *all_count_only(corpus)]
    for unit in units:
        unit["stages"]["translation"] = {
            "status": "not_started",
            "reason_code": "TEST_NOT_STARTED",
        }
    units[0]["stages"]["translation"] = {"status": "not_applicable"}
    units[1]["stages"]["translation"] = {"status": "complete"}

    metric = build_coverage_report(corpus)["corpus"]["stages"]["translation"]
    assert metric["counts"]["not_applicable"] == 1
    assert metric["eligible"] == 4
    assert metric["counts"]["complete"] == 1
    assert metric["completion_percentage"] == 25.0


def test_initial_corpus_reports_zero_translation_completion_and_no_failures(
    sample_root: Path,
) -> None:
    corpus = load_inventory_corpus(sample_root)
    report = build_coverage_report(corpus)

    scopes = [report["corpus"], *report["samples"], *report["object_kinds"]]
    scopes.extend(
        artifact
        for sample in report["samples"]
        for artifact in sample["artifacts"]
    )
    assert len(report["samples"]) == 15
    assert sum(len(sample["artifacts"]) for sample in report["samples"]) == 21
    assert {scope["kind"] for scope in report["object_kinds"]} == set(KINDS)

    for scope in scopes:
        assert_scope(scope)
        translation = scope["stages"]["translation"]
        assert translation["completion_percentage"] == 0.0
        assert translation["counts"]["failed"] == 0
    corpus_translation = report["corpus"]["stages"]["translation"]
    assert corpus_translation["eligible"] == 1561
    assert corpus_translation["counts"]["not_applicable"] == 0
