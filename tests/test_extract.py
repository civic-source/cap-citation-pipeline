"""Unit tests for USCT-002 / USCT-004 / USCT-015 candidate provenance and outcome handling in extract.py."""

import io
import json
import re
import sys
import urllib.error
from email.message import Message
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract  # noqa: E402

ISO_UTC_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_classify_court_restricts_scotus_to_us_supreme_court() -> None:
    assert extract.classify_court("Supreme Court of the United States") == "SCOTUS"
    assert extract.classify_court("U.S. Supreme Court") == "SCOTUS"
    assert extract.classify_court("Supreme Court", court_id="scotus") == "SCOTUS"
    # State supreme courts must not be classified as SCOTUS
    assert extract.classify_court("California Supreme Court") == "District"
    assert extract.classify_court("Supreme Court of Texas") == "District"
    assert extract.classify_court("United States Court of Appeals for the Ninth Circuit") == "Appellate"
    assert extract.classify_court("Court", court_id="ca9") == "Appellate"
    assert extract.classify_court("U.S. District Court for the District of Columbia") == "District"
    assert extract.classify_court("") == "District"


def test_build_case_annotation_emits_usct_004_fields() -> None:
    raw = {
        "caseName": "United States v. Mobley",
        "citation": ["344 F. Supp. 3d 1089", "2018 WL 12345"],
        "court": "U.S. District Court",
        "dateFiled": "2018-10-01",
        "snippet": "Defendant was indicted under 18 U.S.C. § 111 for assaulting an officer.",
        "absolute_url": "/opinion/7332965/united-states-v-mobley/",
    }
    case = extract.build_case_annotation(
        raw,
        retrieval_query='"18 U.S.C. § 111"',
        retrieved_at="2026-11-22T05:00:00Z",
    )

    assert case["caseName"] == "United States v. Mobley"
    assert case["citation"] == "344 F. Supp. 3d 1089"
    assert case["court"] == "District"
    assert case["date"] == "2018-10-01"
    assert case["holdingSummary"] == raw["snippet"]
    assert case["retrievalExcerpt"] == raw["snippet"]
    assert case["retrievalQuery"] == '"18 U.S.C. § 111"'
    assert case["retrievedAt"] == "2026-11-22T05:00:00Z"
    assert case["extractionState"] == "candidate"
    assert case["editorialReview"] == "unreviewed"
    assert case["versionAlignmentState"] == "unknown"
    assert case["sourceUrl"] == "https://www.courtlistener.com/opinion/7332965/united-states-v-mobley/"
    assert case["impact"] == "unverified_mention"


def test_build_case_annotation_clamps_long_fields_and_handles_missing() -> None:
    raw = {
        "caseName": "A" * 600,
        "citation": None,
        "court": "",
        "dateFiled": None,
        "snippet": "S" * 700,
        "absolute_url": "",
    }
    case = extract.build_case_annotation(
        raw,
        retrieval_query="Q" * 300,
        retrieved_at="2026-11-22T05:00:00Z",
    )

    assert len(case["caseName"]) == 500
    assert case["citation"] == ""
    assert case["date"] == ""
    assert len(case["holdingSummary"]) == 500
    assert len(case["retrievalExcerpt"]) == 500
    assert len(case["retrievalQuery"]) == 200
    assert case["sourceUrl"] == ""
    assert case["impact"] == "unverified_mention"


def test_search_courtlistener_distinguishes_empty_from_http_failures_and_bounds_429(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload: dict, status: int = 200) -> None:
            self._bytes = json.dumps(payload).encode("utf-8")
            self.status = status

        def read(self) -> bytes:
            return self._bytes

    # 1. Verified 200 empty
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=15: FakeResponse({"count": 0, "results": []}),
    )
    empty_out = extract.search_courtlistener(18, "99999", max_results=5, sleep_fn=lambda _: None)
    assert empty_out["status"] == "success-empty"
    assert empty_out["results"] == []

    # 2. HTTP 500 error must return status == 'failed', not success-empty
    def raise_500(req, timeout=15):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", Message(), io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", raise_500)
    err_out = extract.search_courtlistener(18, "111", max_results=5, sleep_fn=lambda _: None)
    assert err_out["status"] == "failed"
    assert err_out["httpStatus"] == 500
    assert err_out["failureKind"] == "http_error"

    # 3. Persistent HTTP 429 must terminate after max_retries and return rate_limited
    attempts = {"count": 0}

    def raise_429(req, timeout=15):
        attempts["count"] += 1
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", Message(), io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", raise_429)
    rl_out = extract.search_courtlistener(18, "111", max_results=5, max_retries=2, sleep_fn=lambda _: None)
    assert rl_out["status"] == "failed"
    assert rl_out["failureKind"] == "rate_limited"
    assert attempts["count"] == 3  # 1 initial + 2 retries

    # 4. Multi-page pagination failure on page 2 returns status == 'partial'
    call_idx = {"n": 0}

    def page1_then_fail(req, timeout=15):
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            return FakeResponse({
                "count": 40,
                "next": "https://www.courtlistener.com/api/rest/v4/search/?page=2",
                "results": [{"caseName": "Page 1 Case", "snippet": "Page 1"}],
            })
        raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", Message(), io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", page1_then_fail)
    partial_out = extract.search_courtlistener(18, "111", max_results=30, sleep_fn=lambda _: None)
    assert partial_out["status"] == "partial"
    assert partial_out["failureKind"] == "pagination_incomplete"
    assert partial_out["completedPages"] == 1
    assert partial_out["expectedPages"] == 2
    assert len(partial_out["results"]) == 1


def test_process_sections_and_write_annotations_preserves_prior_sidecar_on_failure(tmp_path: Path) -> None:
    def fake_search_ok(title: int, section: str, max_results: int) -> dict:
        if title == 18 and section == "111":
            return {
                "status": "success-nonempty",
                "httpStatus": 200,
                "completedPages": 1,
                "expectedPages": 1,
                "count": 42,
                "results": [
                    {
                        "caseName": "United States v. Sample",
                        "citation": ["100 F.4th 1"],
                        "court": "United States Court of Appeals for the First Circuit",
                        "dateFiled": "2024-01-15",
                        "snippet": "Mentions 18 U.S.C. § 111 in passing.",
                        "absolute_url": "/opinion/999/united-states-v-sample/",
                    }
                ],
            }
        return {"status": "success-empty", "count": 0, "results": []}

    results = extract.process_sections(
        [(18, "111"), (18, "99999")],
        max_cases=5,
        search_fn=fake_search_ok,
        delay_s=0.0,
    )
    assert list(results.keys()) == ["title-18/section-111"]

    written = extract.write_annotations(results, tmp_path)
    assert written == 1

    yaml_path = tmp_path / "title-18" / "section-111.yaml"
    assert yaml_path.exists()

    parsed = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert parsed["targetSection"] == "18 U.S.C. § 111"
    assert ISO_UTC_Z_RE.match(parsed["lastSyncedET"])
    assert parsed["totalCases"] == 42
    assert parsed["refreshStatus"]["status"] == "success-nonempty"
    assert parsed["refreshStatus"]["isStale"] is False
    assert len(parsed["cases"]) == 1

    c = parsed["cases"][0]
    assert c["extractionState"] == "candidate"
    assert c["editorialReview"] == "unreviewed"
    assert c["versionAlignmentState"] == "unknown"
    assert c["impact"] == "unverified_mention"

    # Now simulate a failed refresh for 18 U.S.C. § 111 — prior cases must be preserved and marked stale!
    def fake_search_fail(title: int, section: str, max_results: int) -> dict:
        return {
            "status": "failed",
            "httpStatus": 503,
            "failureKind": "http_error",
            "errorMessage": "HTTP 503",
            "completedPages": 0,
            "expectedPages": 1,
            "count": 0,
            "results": [],
        }

    failed_results = extract.process_sections(
        [(18, "111")],
        max_cases=5,
        search_fn=fake_search_fail,
        delay_s=0.0,
    )
    extract.write_annotations(failed_results, tmp_path)
    after_fail = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert len(after_fail["cases"]) == 1
    assert after_fail["cases"][0]["caseName"] == "United States v. Sample"
    assert after_fail["refreshStatus"]["status"] == "failed"
    assert after_fail["refreshStatus"]["isStale"] is True
    assert after_fail["refreshStatus"]["retainedPriorSnapshot"] is True


def test_dry_run_skips_writing_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        extract,
        "process_sections",
        lambda sections, max_cases: {
            "title-18/section-111": {
                "status": "success-nonempty",
                "totalCases": 1,
                "cases": [
                    extract.build_case_annotation(
                        {"caseName": "Dry Run Case", "snippet": "Excerpt"},
                        '"18 U.S.C. § 111"',
                        "2026-11-22T05:00:00Z",
                    )
                ],
            }
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["extract.py", "--sample", "1", "--output", str(tmp_path), "--dry-run"],
    )

    extract.main()
    assert list(tmp_path.iterdir()) == []
