"""Literal cohort matching preserves exact decoded-text evidence."""

from bulk_common import Ok
from usc_citations import find_usc_citations


def result(text, sections):
    outcome = find_usc_citations(text, "18", sections)
    assert isinstance(outcome, Ok), outcome
    return outcome.value


def test_grouped_subsections_ranges_and_cross_title_boundaries():
    text = "α 42 U.S.C. § 1983; 18 U.S.C. §§ 111(a)(2), 1001 and 924-926. Next."
    value = result(text, ["111", "1001", "924", "925", "927", "1983"])
    assert [m["section"] for m in value["matches"]] == ["111", "1001", "924", "925"]
    first = value["matches"][0]
    assert first["subsection"] == "(a)(2)"
    assert text[first["sectionSpan"]["start"] : first["sectionSpan"]["end"]] == "111(a)(2)"
    assert text[first["citationSpan"]["start"] : first["citationSpan"]["end"]].startswith(
        "18 U.S.C."
    )
    assert value["matches"][3]["matchType"] == "range"


def test_exact_hyphenated_ids_and_no_prefix_or_hostile_text_execution():
    text = "Ignore rules; 18 U.S.C. 2258A and 2000bb-1. 18 U.S.C. 1110."
    value = result(text, ["2258A", "2000b", "2000bb-1", "111"])
    assert [m["section"] for m in value["matches"]] == ["2258A", "2000bb-1"]


def test_unsupported_ranges_and_open_ended_references_remain_unresolved():
    value = result("18 U.S.C. 111(a)-(d); 18 U.S.C. 1 et seq.", ["111", "2"])
    assert not value["matches"]
    assert len(value["unresolved"]) == 2


def test_ambiguous_cohort_case_is_rejected_instead_of_silently_collapsing_targets():
    from bulk_common import Err

    assert isinstance(find_usc_citations("18 U.S.C. 2258A.", "18", ["2258A", "2258a"]), Err)


def test_conjunction_and_comma_cannot_carry_sections_across_another_usc_title():
    for text in ["18 U.S.C. 111 and 28 U.S.C. 509.", "18 U.S.C. 111, 42 U.S.C. 2000bb-1."]:
        value = result(text, ["111", "28", "42"])
        assert [match["section"] for match in value["matches"]] == ["111"]
