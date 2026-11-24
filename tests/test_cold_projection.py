"""Bounded historical-sample projections preserve exact source evidence."""
import json

import pytest

from bulk_common import Blob, Err, Ok
from cold_projection import project_sample


def row(case_id=1, opinion_id=10, text="18 U.S.C. § 111(a)(2).", **updates):
    return {"id": case_id, "case_name": "United States v. Example", "date_filed": "2023-01-01",
            "date_filed_is_approximate": False, "court_type": "F", "court_jurisdiction": "USA, Federal",
            "court_full_name": "Court of Appeals for the Ninth Circuit", "court_short_name": "Ninth Circuit",
            "citations": ["123 F.4th 1"], "opinions": [{"opinion_id": opinion_id, "opinion_text": text, "type": "020lead", "ocr": True}], **updates}


def project(rows, **configuration):
    raw = b"\n".join(json.dumps(value, ensure_ascii=False).encode() for value in rows) + b"\n"
    return project_sample(raw, Blob.of(raw), {"usc_title": "18", "sections": ["111", "1001"], **configuration})


def good(result):
    assert isinstance(result, Ok), result
    return result.value


def test_preserves_unicode_codepoint_spans_and_candidate_provenance():
    text = "⚖ α: 42 U.S.C. § 1983; 18 U.S.C. §§ 111(a)(2), 1001."
    product = good(project([row(text=text)]))
    assert len(product["matches"]) == 2
    match = product["matches"][0]
    assert match["case_id"] == 1 and match["opinion_id"] == 10
    assert match["record_line"] == 1 and match["opinion_index"] == 0
    assert match["field_path"] == "/opinions/0/opinion_text"
    assert match["subsection"] == "(a)(2)"
    assert text[match["section_span"]["start"]:match["section_span"]["end"]] == "111(a)(2)"
    assert text[match["citation_span"]["start"]:match["citation_span"]["end"]] == match["citation_text"]
    context = match["context"]
    assert text[context["start"]:context["end"]] == context["text"]
    assert match["text_sha256"] == Blob.of(text.encode()).sha256
    assert (match["extraction_state"], match["editorial_review"], match["version_alignment_state"]) == ("candidate", "unreviewed", "unknown")
    assert product["coverage"]["records_scanned"] == product["coverage"]["opinions_scanned"] == 1
    assert product["coverage"]["global_corpus_complete"] is False
    assert product["coverage"]["extraction_coverage"] == "supported-literal-only"


def test_federal_district_geographic_state_label_is_retained():
    product = good(project([row(court_type="FD", court_jurisdiction="Louisiana, LA", court_full_name="District Court, W.D. Louisiana")]))
    assert product["matches"][0]["court_jurisdiction"] == "Louisiana, LA"
    assert product["matches"][0]["court_type"] == "FD"


def test_excludes_state_and_unknown_or_conflicting_courts_without_inventing_federal_classification():
    rows = [row(court_type="SA", court_jurisdiction="Arizona, AZ", court_full_name="Arizona Court of Appeals"),
            row(2, 20, court_type="unknown"),
            row(3, 30, court_type="F", court_jurisdiction="Texas, TX", court_full_name="Supreme Court of Texas")]
    product = good(project(rows))
    assert product["matches"] == []
    assert product["coverage"]["records_scanned"] == product["coverage"]["opinions_scanned"] == 3
    assert product["coverage"]["records_excluded_court"] == 1
    assert product["coverage"]["records_unknown_metadata"] == 2
    assert {entry["reason"] for entry in product["unresolved"]} >= {"unknown_court_type", "conflicting_court_metadata"}


def test_since_is_inclusive_and_retains_approximate_reported_dates():
    rows = [row(date_filed_is_approximate=True), row(2, 20, date_filed="2022-12-31")]
    product = good(project(rows, since="2023-01-01"))
    assert len(product["matches"]) == 1
    assert product["matches"][0]["date_filed_is_approximate"] is True
    assert product["coverage"]["records_excluded_date"] == 1
    assert product["coverage"]["records_approximate_date"] == 1


def test_unknown_dates_are_explicit_and_never_silently_treated_as_before_since():
    selected = good(project([row(date_filed=None)]))
    assert len(selected["matches"]) == 1
    assert selected["matches"][0]["date_status"] == "unknown"
    excluded = good(project([row(date_filed=None)], since="2023-01-01"))
    assert excluded["matches"] == []
    assert excluded["coverage"]["selected_opinions_complete"] is False
    assert excluded["coverage"]["records_unknown_date"] == excluded["coverage"]["records_excluded_date"] == 1
    assert any(entry["reason"] == "unknown_source_date" for entry in excluded["unresolved"])


def test_repeated_identical_records_are_deduplicated_and_counts_retain_all_rows():
    product = good(project([row(), row()]))
    assert len(product["matches"]) == 1
    assert product["coverage"]["records_scanned"] == product["coverage"]["opinions_scanned"] == 2
    assert product["coverage"]["records_duplicate"] == product["coverage"]["opinions_duplicate"] == 1


@pytest.mark.parametrize("rows", [[row(case_id=True)], [row(opinion_id=-1)], [row(), row(text="different")], [row(), row(case_id=2)]])
def test_invalid_ids_and_contradictory_case_or_opinion_identity_fail_closed(rows):
    assert isinstance(project(rows), Err)


def test_source_identity_partial_final_records_and_duplicate_keys_are_rejected():
    raw = json.dumps(row()).encode()
    assert isinstance(project_sample(raw, Blob.of(b"different"), {"usc_title": "18", "sections": ["111"]}), Err)
    for body in [raw + b'\n{"id":', b'{"id":1,"id":2}\n']:
        assert isinstance(project_sample(body, Blob.of(body), {"usc_title": "18", "sections": ["111"]}), Err)


def test_unsupported_literals_are_retained_as_located_unresolved_evidence():
    text = "18 U.S.C. 1 et seq.; no complete literal range."
    product = good(project([row(text=text)], sections=["1"]))
    assert not product["matches"]
    entry = next(item for item in product["unresolved"] if item["kind"] == "citation")
    assert entry["case_id"] == 1 and entry["opinion_id"] == 10
    assert text[entry["citation_span"]["start"]:entry["citation_span"]["end"]] == entry["citation_text"]


def test_config_and_resource_caps_fail_instead_of_truncating_output():
    assert isinstance(project([row()], sections=["111", "111"]), Err)
    assert isinstance(project([row()], since="2023-02-30"), Err)
    assert isinstance(project([row()], court_types=["SA"]), Err)
    large = b" " * (32 * 1024**2 + 1)
    assert isinstance(project_sample(large, Blob.of(large), {"usc_title": "18", "sections": ["111"]}), Err)
    assert isinstance(project([row(text="x" * (4 * 1024**2 + 1))]), Err)


def test_decoded_unicode_line_separators_do_not_create_false_jsonl_records():
    product = good(project([row(text="Before\u2028a paragraph; 18 U.S.C. 111.")]))
    assert len(product["matches"]) == product["coverage"]["records_scanned"] == 1


def test_missing_opinion_text_is_explicitly_unresolved_with_raw_counts_preserved():
    value = row()
    value["opinions"][0]["opinion_text"] = None
    product = good(project([value]))
    assert product["coverage"]["opinions_scanned"] == product["coverage"]["opinions_unknown_metadata"] == 1
    assert product["coverage"]["selected_opinions_complete"] is False
    assert any(entry["reason"] == "missing_opinion_text" for entry in product["unresolved"])


def test_expanded_candidate_output_is_bounded_without_silently_truncating():
    value = row(text="18 U.S.C. 111. " * 7000, case_name="Example " * 1500)
    result = project([value])
    assert isinstance(result, Err)
    assert "output limit" in result.error


def test_invalid_reporter_metadata_remains_unknown_instead_of_fabricating_citations():
    product = good(project([row(citations="not an array")]))
    assert product["matches"][0]["citations"] is None
    assert product["coverage"]["records_unknown_metadata"] == 1
    assert any(entry["reason"] == "invalid_case_citations" for entry in product["unresolved"])


def test_unresolved_metadata_never_implies_verified_or_reviewed_findings():
    product = good(project([row(court_type="unknown")]))
    unresolved = product["unresolved"][0]
    assert unresolved["extraction_state"] == "candidate"
    assert unresolved["editorial_review"] == "unreviewed"
    assert unresolved["version_alignment_state"] == "unknown"


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("selected", [False, True])
def test_unavailable_opinion_collection_is_unresolved_and_only_affects_selected_scope(missing, selected):
    value = row(opinions=None)
    if missing:
        value.pop("opinions")
    if not selected:
        value.update(court_type="SA", court_jurisdiction="Florida, FL", court_full_name="Florida Court of Appeals")
    product = good(project([value]))
    assert not product["matches"]
    assert product["coverage"]["records_unknown_opinion_collection"] == 1
    assert product["coverage"]["opinions_scanned"] == 0
    assert product["coverage"]["selected_opinions_complete"] is (not selected)
    assert any(entry["reason"] == "unavailable_opinion_collection" and entry["field_path"] == "/opinions" for entry in product["unresolved"])


def test_unavailable_opinions_before_since_do_not_invalidate_selected_window():
    product = good(project([row(opinions=None, date_filed="2000-01-01")], since="2023-01-01"))
    assert product["coverage"]["records_excluded_date"] == 1
    assert product["coverage"]["selected_opinions_complete"] is True
    assert product["coverage"]["records_unknown_opinion_collection"] == 1


@pytest.mark.parametrize("opinions", [False, 42, "not a collection", {}])
def test_malformed_scalar_opinion_collections_still_fail_closed(opinions):
    assert isinstance(project([row(opinions=opinions)]), Err)
