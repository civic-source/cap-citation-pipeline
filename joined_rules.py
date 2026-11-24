"""Source-native selection and exact candidate evidence rules for joined shards."""
from datetime import date
import hashlib
import re

from bulk_common import Err
from usc_citations import find_usc_citations

CANDIDATE = {"extraction_state": "candidate", "editorial_review": "unreviewed", "version_alignment_state": "unknown"}
STATES = frozenset("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia|Puerto Rico|Virgin Islands|Guam|Northern Mariana Islands|American Samoa".casefold().split("|"))


def configuration(value):
    if not isinstance(value, dict) or set(value) - {"usc_title", "sections", "court_types", "since"}:
        raise ValueError("Invalid joined configuration")
    title, sections = value.get("usc_title"), value.get("sections")
    if not isinstance(sections, list) or not 0 < len(sections) <= 10000:
        raise ValueError("Explicit nonempty bounded cohort required")
    if any(not isinstance(item, str) or len(item) > 256 for item in sections):
        raise ValueError("Invalid joined section identity")
    if len({item.casefold() for item in sections}) != len(sections):
        raise ValueError("Duplicate joined section identity")
    courts = value.get("court_types", ["F", "FD"])
    if not isinstance(courts, list) or not courts or any(item not in ("F", "FD") for item in courts) or len(set(courts)) != len(courts):
        raise ValueError("Explicit federal F/FD court codes required")
    since = value.get("since")
    if since is not None:
        if not isinstance(since, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
            raise ValueError("Since must be an ISO filing date")
        date.fromisoformat(since)
    result = find_usc_citations("", title, sections)
    if isinstance(result, Err):
        raise ValueError(result.error)
    return {"usc_title": title, "sections": list(sections), "court_types": list(courts), "since": since}, result.value["grammar"]


def court_selection(row, courts):
    code, jurisdiction, name = row.get("court_type"), row.get("court_jurisdiction"), row.get("court_full_name")
    if code not in ("F", "FD", "SA"):
        return False, "unknown_court_type"
    label = jurisdiction.casefold().strip() if isinstance(jurisdiction, str) else ""
    full = name.casefold() if isinstance(name, str) else ""
    if code == "SA":
        return False, "conflicting_court_metadata" if label == "usa, federal" or "united states" in full else None
    if not label or not full:
        return False, "missing_court_metadata"
    state_name = bool(re.search(r"\b(?:state|superior|municipal|county|common pleas)\b", full)) or ("supreme court" in full and "united states" not in full)
    geographic = code == "FD" and label.split(",", 1)[0].strip() in STATES
    if state_name or not (label == "usa, federal" or geographic):
        return False, "conflicting_court_metadata"
    return code in courts, None


def normalize(row):
    observed = row.get("date_filed")
    if observed is not None and type(observed) is not date:
        raise ValueError("Joined filing date must be native date32 or null")
    return {**row, "date_filed": observed.isoformat() if observed is not None else None}


def identity(value):
    return type(value) is int and 0 < value <= (1 << 53) - 1


def evidence(base, text, citation_span, section_span=None):
    start, end = citation_span["start"], citation_span["end"]
    if not 0 <= start <= end <= len(text):
        raise ValueError("Citation span exceeds source text")
    focus = section_span or citation_span
    if not start <= focus["start"] <= focus["end"] <= end:
        raise ValueError("Section span exceeds citation")
    left, right = max(0, focus["start"] - 160), min(len(text), focus["end"] + 160)
    right = min(right, left + 1024)
    return {**base, "citation_span": citation_span, "citation_text": text[start:end],
            "context": {"start": left, "end": right, "text": text[left:right]},
            **({"section_span": section_span} if section_span is not None else {})}


def text_base(row, opinion, locator, text):
    return {**locator, **CANDIDATE, "case_id": row.get("id"), "opinion_id": opinion.get("opinion_id"),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "case_name": row.get("case_name"), "citations": row.get("citations"),
            "date_filed": row.get("date_filed"), "date_filed_is_approximate": row.get("date_filed_is_approximate"),
            "court_type": row.get("court_type"), "court_jurisdiction": row.get("court_jurisdiction"),
            "court_full_name": row.get("court_full_name"), "court_short_name": row.get("court_short_name"),
            "opinion_type": opinion.get("type"), "ocr": opinion.get("ocr")}
