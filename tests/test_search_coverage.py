"""Searched-empty outcomes remain distinct from unattempted scopes."""
import json
import sys

import pytest
import yaml

import extract

OBSERVED = "2026-11-24T20:00:00Z"


def outcome(status, cases=None):
    return {"status": status, "httpStatus": 503 if status == "failed" else 200,
            "observedAt": OBSERVED, "completedPages": 0 if status == "failed" else 1,
            "expectedPages": 1, "totalCases": len(cases or []), "cases": cases or []}


def test_mixed_cohort_retains_nonempty_empty_and_failed_acquisitions_without_empty_sidecars(tmp_path):
    cohort = [(18, "111"), (18, "112"), (18, "113")]
    calls = []
    def search(title, section, limit):
        calls.append((title, section))
        status = {"111": "success-nonempty", "112": "success-empty", "113": "failed"}[section]
        return {"status": status, "httpStatus": 503 if status == "failed" else 200,
                "completedPages": 0 if status == "failed" else 1, "expectedPages": 1,
                "count": 1 if section == "111" else 0,
                "results": [{"caseName": "Candidate", "snippet": "18 U.S.C. § 111"}] if section == "111" else []}
    results = extract.process_sections(cohort, search_fn=search, delay_s=0)
    assert list(results) == ["title-18/section-111", "title-18/section-112", "title-18/section-113"]
    assert calls == cohort
    assert results["title-18/section-112"]["status"] == "success-empty"
    assert results["title-18/section-112"]["cases"] == []
    assert extract.write_annotations(results, tmp_path) == 1
    assert [path.name for path in tmp_path.rglob("*.yaml")] == ["section-111.yaml"]


@pytest.mark.parametrize("reviewed", [False, True])
def test_searched_empty_preserves_existing_candidate_and_reviewed_bytes(tmp_path, reviewed):
    case = extract.build_case_annotation({"caseName": "Retained"}, "query", OBSERVED)
    if reviewed:
        case.update(extractionState="verified", editorialReview="accepted", reviewedProposition="A reviewed proposition")
    existing = {"targetSection": "18 U.S.C. § 111", "lastSyncedET": OBSERVED,
                "totalCases": 1, "cases": [case]}
    path = tmp_path / "title-18/section-111.yaml"
    path.parent.mkdir()
    path.write_text("# Keep prior formatting.\n" + yaml.safe_dump(existing), encoding="utf-8")
    before = path.read_bytes()
    assert extract.write_annotations({"title-18/section-111": outcome("success-empty")}, tmp_path) == 0
    assert path.read_bytes() == before


def test_receipt_distinguishes_completed_empty_failed_and_unattempted_queries():
    from search_coverage import build_search_coverage
    cohort = [(18, "111"), (18, "112"), (18, "113")]
    results = {"title-18/section-111": outcome("success-empty"), "title-18/section-112": outcome("failed")}
    receipt = build_search_coverage(cohort, results)
    assert receipt["counts"] == {"expectedCount": 3, "attemptedCount": 2, "completedCount": 1,
                                 "emptyCount": 1, "nonemptyCount": 0, "partialCount": 0,
                                 "failedCount": 1, "notCollectedCount": 1}
    assert receipt["coverageStatus"] == "partial"
    assert receipt["globalCorpusComplete"] is False
    assert [item["status"] for item in receipt["outcomes"]] == ["success-empty", "failed", "not_collected"]
    assert receipt["outcomes"][0]["scope"] == "18 U.S.C. § 111"
    assert receipt["outcomes"][0]["query"] == extract.build_search_query(18, "111")
    assert "observedAt" not in receipt["outcomes"][2]


def test_cli_saves_failed_and_empty_coverage_without_publishing_empty_annotations(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "process_sections", lambda sections, limit: {
        "title-18/section-111": outcome("success-empty"), "title-18/section-1001": outcome("failed")})
    output, receipt = tmp_path / "annotations", tmp_path / "coverage.json"
    monkeypatch.setattr(sys, "argv", ["extract.py", "--sample", "2", "--output", str(output), "--coverage-out", str(receipt)])
    extract.main()
    assert not list(output.rglob("*.yaml"))
    saved = json.loads(receipt.read_text())
    assert saved["counts"]["emptyCount"] == saved["counts"]["failedCount"] == 1
    assert saved["coverageStatus"] == "partial"


def test_all_empty_cli_keeps_no_results_behavior(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(extract, "process_sections", lambda sections, limit: {"title-18/section-111": outcome("success-empty")})
    monkeypatch.setattr(sys, "argv", ["extract.py", "--sample", "1", "--output", str(tmp_path)])
    extract.main()
    assert "No results found." in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_complete_empty_queries_are_not_a_global_absence_claim():
    from search_coverage import build_search_coverage
    receipt = build_search_coverage([(18, "111")], {"title-18/section-111": outcome("success-empty")})
    assert receipt["coverageStatus"] == "complete"
    assert receipt["counts"]["completedCount"] == receipt["counts"]["emptyCount"] == 1
    assert receipt["scopeKind"] == "literal-section-search-cohort"
    assert receipt["globalCorpusComplete"] is False


def test_duplicate_cohort_is_refused_before_any_search():
    calls = []
    with pytest.raises(ValueError, match="Duplicate"):
        extract.process_sections([(18, "111"), (18, "111")], search_fn=lambda *args: calls.append(args), delay_s=0)
    assert calls == []


def test_receipt_refuses_extra_scopes_and_contradictory_empty_status():
    from search_coverage import build_search_coverage
    with pytest.raises(ValueError, match="declared"):
        build_search_coverage([(18, "111")], {"title-18/section-112": outcome("success-empty")})
    with pytest.raises(ValueError, match="contradicts"):
        build_search_coverage([(18, "111")], {"title-18/section-111": {**outcome("success-empty"), "totalCases": 2}})


def test_receipt_never_overwrites_an_existing_path(tmp_path):
    from search_coverage import write_search_coverage
    destination = tmp_path / "coverage.json"
    destination.write_text("previous immutable receipt")
    with pytest.raises(FileExistsError):
        write_search_coverage({"coverageStatus": "complete"}, destination)
    assert destination.read_text() == "previous immutable receipt"
    assert list(tmp_path.iterdir()) == [destination]


def test_cli_refuses_existing_receipt_before_searching(tmp_path, monkeypatch):
    destination = tmp_path / "coverage.json"
    destination.write_text("prior")
    calls = []
    monkeypatch.setattr(extract, "process_sections", lambda *args: calls.append(args))
    monkeypatch.setattr(sys, "argv", ["extract.py", "--sample", "1", "--coverage-out", str(destination)])
    with pytest.raises(FileExistsError):
        extract.main()
    assert calls == []
    assert destination.read_text() == "prior"
