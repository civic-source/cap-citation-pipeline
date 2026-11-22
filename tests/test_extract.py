"""Unit tests for USCT-004 / USCT-015 candidate provenance in extract.py."""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract  # noqa: E402

ISO_UTC_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_classify_court() -> None:
    assert extract.classify_court("Supreme Court of the United States") == "SCOTUS"
    assert extract.classify_court("United States Court of Appeals for the Ninth Circuit") == "Appellate"
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
    # Backward-compatible holdingSummary paired with explicit USCT-004 retrievalExcerpt
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


def test_process_sections_and_write_annotations_yaml_roundtrip(tmp_path: Path) -> None:
    def fake_search(title: int, section: str, max_results: int) -> dict:
        if title == 18 and section == "111":
            return {
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
        return {"count": 0, "results": []}

    results = extract.process_sections(
        [(18, "111"), (18, "99999")],
        max_cases=5,
        search_fn=fake_search,
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
    assert len(parsed["cases"]) == 1

    c = parsed["cases"][0]
    assert c["extractionState"] == "candidate"
    assert c["editorialReview"] == "unreviewed"
    assert c["versionAlignmentState"] == "unknown"
    assert c["impact"] == "unverified_mention"
    assert c["retrievalExcerpt"] == "Mentions 18 U.S.C. § 111 in passing."
    assert c["holdingSummary"] == "Mentions 18 U.S.C. § 111 in passing."
    assert c["retrievalQuery"] == '"18 U.S.C. § 111"'
    assert ISO_UTC_Z_RE.match(c["retrievedAt"])


def test_dry_run_skips_writing_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        extract,
        "process_sections",
        lambda sections, max_cases: {
            "title-18/section-111": {
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
