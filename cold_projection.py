"""Candidate literal-citation evidence from retained historical COLD sample bytes."""
from datetime import date
import hashlib
import io
import re

from bulk_common import Blob, Err, Ok, Result, canonical_bytes, load_json
from usc_citations import find_usc_citations

_MAX_BODY = 32 * 1024**2
_MAX_RECORD = 4 * 1024**2
_MAX_PRODUCT = 64 * 1024**2
_CANDIDATE = {"extraction_state": "candidate", "editorial_review": "unreviewed", "version_alignment_state": "unknown"}
_US_STATES = frozenset(("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia|Puerto Rico|Virgin Islands|Guam|Northern Mariana Islands|American Samoa").casefold().split("|"))


def _source_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _configuration(value):
    if not isinstance(value, dict) or set(value) - {"usc_title", "sections", "court_types", "since"}:
        raise ValueError("Invalid projection configuration fields")
    title, sections = value.get("usc_title"), value.get("sections")
    if not isinstance(title, str) or not isinstance(sections, list) or not sections or len(sections) > 10000:
        raise ValueError("USC title and nonempty cohort are required")
    if any(not isinstance(section, str) or len(section) > 256 for section in sections):
        raise ValueError("Invalid cohort section identity")
    if len({section.casefold() for section in sections}) != len(sections):
        raise ValueError("Duplicate cohort section identity")
    court_types = value.get("court_types", ["F", "FD"])
    if not isinstance(court_types, list) or not court_types or any(item not in ("F", "FD") for item in court_types) or len(set(court_types)) != len(court_types):
        raise ValueError("Only explicit federal F/FD court codes may be selected")
    since = value.get("since")
    if since is not None and _source_date(since) is None:
        raise ValueError("Since must be an ISO calendar date")
    validated = find_usc_citations("", title, sections)
    if isinstance(validated, Err):
        raise ValueError(validated.error)
    return {"usc_title": title, "sections": list(sections), "court_types": list(court_types), "since": since}, validated.value["grammar"]


def _identifier(value, kind):
    if type(value) is not int or not 0 < value <= (1 << 53) - 1:
        raise ValueError(f"Invalid {kind} identity")
    return value


def _reporter_citations(row):
    values = row.get("citations")
    return values if isinstance(values, list) and all(isinstance(value, str) for value in values) else None


def _court(row, selected):
    code, jurisdiction, name = row.get("court_type"), row.get("court_jurisdiction"), row.get("court_full_name")
    if code not in ("F", "FD", "SA"):
        return False, "unknown_court_type"
    label = jurisdiction.casefold().strip() if isinstance(jurisdiction, str) else ""
    full_name = name.casefold() if isinstance(name, str) else ""
    federal_label = label == "usa, federal"
    if code == "SA":
        return False, "conflicting_court_metadata" if federal_label or "united states" in full_name else None
    if not label or not full_name:
        return False, "missing_court_metadata"
    state_label = label.split(",", 1)[0].strip() in _US_STATES
    state_name = bool(re.search(r"\b(?:state|superior|municipal|county|common pleas)\b", full_name)) or ("supreme court" in full_name and "united states" not in full_name)
    if state_name or not (federal_label or (code == "FD" and state_label)):
        return False, "conflicting_court_metadata"
    return code in selected, None


def _span(value, text):
    if not isinstance(value, dict) or type(value.get("start")) is not int or type(value.get("end")) is not int:
        raise ValueError("Invalid citation evidence span")
    start, end = value["start"], value["end"]
    if not 0 <= start <= end <= len(text):
        raise ValueError("Citation evidence span exceeds source text")
    return {"start": start, "end": end}


class _Projection:
    def __init__(self, source, configuration, grammar):
        self.configuration = configuration
        counters = ("records_scanned", "opinions_scanned", "records_selected", "opinions_selected", "records_excluded_court", "records_excluded_date", "records_unknown_metadata", "opinions_unknown_metadata", "records_unknown_date", "records_approximate_date", "records_unknown_opinion_collection", "records_duplicate", "opinions_duplicate", "records_with_matches", "opinions_with_matches")
        self.coverage = {key: 0 for key in counters}
        self.coverage.update({"sample_enumeration_complete": True, "global_corpus_complete": False,
                              "selected_opinions_complete": True, "extraction_coverage": "supported-literal-only"})
        self.product = {"schema_version": "cap.cold-cohort/v1", "source_blob": source.to_dict(),
                        "source_kind": "historical-2023-sample", "span_unit": "unicode-codepoint", "text_encoding": "utf-8",
                        "configuration": configuration, "grammar": grammar, "coverage": self.coverage,
                        "matches": [], "unresolved": []}
        self.case_ids, self.opinion_ids, self.unknown_records = {}, {}, set()
        self.output_bytes = len(canonical_bytes(self.product)) + 4096

    def _emit(self, collection, value):
        self.output_bytes += len(canonical_bytes(value)) + 1
        if self.output_bytes > _MAX_PRODUCT:
            raise ValueError("Projection exceeds 64 MiB output limit")
        self.product[collection].append(value)

    def _unknown(self, row, line, reason, field, details=None):
        if line not in self.unknown_records:
            self.unknown_records.add(line)
            self.coverage["records_unknown_metadata"] += 1
        details = details or {}
        if details.get("affects_selected", True):
            self.coverage["selected_opinions_complete"] = False
        opinion_index = details.get("opinion_index")
        value = {**_CANDIDATE, "kind": "metadata", "case_id": row["id"], "record_line": line,
                 "field_path": field, "reason": reason,
                 "source_metadata": {key: row.get(key) for key in ("case_name", "court_type", "court_jurisdiction", "court_full_name")}}
        if opinion_index is not None:
            value.update({"opinion_index": opinion_index, "opinion_id": row["opinions"][opinion_index]["opinion_id"]})
        self._emit("unresolved", value)

    def _unique_opinions(self, row):
        opinions = row.get("opinions")
        if opinions is None:
            self.coverage["records_unknown_opinion_collection"] += 1
            return []
        if not isinstance(opinions, list) or len(opinions) > 1000:
            raise ValueError("Invalid or oversized opinion collection")
        self.coverage["opinions_scanned"] += len(opinions)
        selected = []
        for index, opinion in enumerate(opinions):
            if not isinstance(opinion, dict):
                raise ValueError("Opinion record must be a mapping")
            identifier = _identifier(opinion.get("opinion_id"), "opinion")
            identity = (row["id"], Blob.of(canonical_bytes(opinion)).sha256)
            if identifier in self.opinion_ids:
                if self.opinion_ids[identifier] != identity:
                    raise ValueError("Contradictory opinion identity")
                self.coverage["opinions_duplicate"] += 1
            else:
                self.opinion_ids[identifier] = identity
                selected.append((index, opinion))
        return selected

    def _date_selected(self, row, line):
        observed = _source_date(row.get("date_filed"))
        if row.get("date_filed_is_approximate") is True:
            self.coverage["records_approximate_date"] += 1
        if observed is None:
            self.coverage["records_unknown_date"] += 1
            self._emit("unresolved", {**_CANDIDATE, "kind": "metadata", "case_id": row["id"], "record_line": line,
                                      "field_path": "/date_filed", "reason": "unknown_source_date",
                                      "reported_value": row.get("date_filed")})
        since = self.configuration["since"]
        if since is not None and observed is None:
            self.coverage["selected_opinions_complete"] = False
        if since is not None and (observed is None or observed < _source_date(since)):
            self.coverage["records_excluded_date"] += 1
            return False
        return True

    def _base(self, row, opinion, line, index, text):
        observed = _source_date(row.get("date_filed"))
        return {"case_id": row["id"], "opinion_id": opinion["opinion_id"], "record_line": line,
                "opinion_index": index, "field_path": f"/opinions/{index}/opinion_text",
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "case_name": row["case_name"], "citations": _reporter_citations(row),
                "date_filed": observed.isoformat() if observed else None,
                "date_filed_raw": row.get("date_filed"), "date_status": "known" if observed else "unknown",
                "date_filed_is_approximate": row.get("date_filed_is_approximate") if type(row.get("date_filed_is_approximate")) is bool else None,
                "court_type": row["court_type"], "court_jurisdiction": row.get("court_jurisdiction"),
                "court_full_name": row.get("court_full_name"), "court_short_name": row.get("court_short_name"),
                "opinion_type": opinion.get("type") if isinstance(opinion.get("type"), str) else None,
                "ocr": opinion.get("ocr") if type(opinion.get("ocr")) is bool else None,
                **_CANDIDATE}

    def _evidence(self, base, text, citation_span, section_span=None):
        citation = _span(citation_span, text)
        focus = _span(section_span, text) if section_span is not None else citation
        if not citation["start"] <= focus["start"] <= focus["end"] <= citation["end"]:
            raise ValueError("Section span exceeds its citation")
        start, end = max(0, focus["start"] - 160), min(len(text), focus["end"] + 160)
        if end - start > 1024:
            end = min(len(text), start + 1024)
        return {**base, "citation_span": citation, "citation_text": text[citation["start"]:citation["end"]],
                "context": {"start": start, "end": end, "text": text[start:end]},
                **({"section_span": focus} if section_span is not None else {})}

    def _opinion(self, row, opinion, line, index):
        text = opinion.get("opinion_text")
        if not isinstance(text, str) or not text.strip():
            self.coverage["opinions_unknown_metadata"] += 1
            self._unknown(row, line, "missing_opinion_text", f"/opinions/{index}/opinion_text", {"opinion_index": index})
            return False
        if len(text.encode("utf-8")) > _MAX_RECORD:
            raise ValueError("Opinion text exceeds 4 MiB limit")
        self.coverage["opinions_selected"] += 1
        result = find_usc_citations(text, self.configuration["usc_title"], self.configuration["sections"])
        if isinstance(result, Err):
            raise ValueError(result.error)
        if result.value["grammar"] != self.product["grammar"]:
            raise ValueError("Mixed citation grammar identities")
        base = self._base(row, opinion, line, index, text)
        for match in result.value["matches"]:
            entry = self._evidence(base, text, match["citationSpan"], match["sectionSpan"])
            if entry["citation_text"] != match["citationText"]:
                raise ValueError("Citation text differs from located evidence")
            self._emit("matches", {**entry, "section": match["section"], "subsection": match["subsection"], "match_type": match["matchType"]})
        for unresolved in result.value["unresolved"]:
            entry = self._evidence(base, text, {"start": unresolved["start"], "end": unresolved["end"]})
            self._emit("unresolved", {**entry, "kind": "citation", "reason": unresolved["reason"]})
        if result.value["matches"]:
            self.coverage["opinions_with_matches"] += 1
        return bool(result.value["matches"])

    def _unavailable_collection(self, row, line, selected):
        if row.get("opinions") is not None:
            return
        since = self.configuration["since"]
        observed = _source_date(row.get("date_filed"))
        in_window = since is None or observed is None or observed >= _source_date(since)
        self._unknown(row, line, "unavailable_opinion_collection", "/opinions",
                      {"affects_selected": selected and in_window})

    def _selected_case(self, row, line):
        selected, reason = _court(row, self.configuration["court_types"])
        self._unavailable_collection(row, line, selected)
        if reason:
            self._unknown(row, line, reason, "/court_type")
            return False
        if not selected:
            self.coverage["records_excluded_court"] += 1
            return False
        if not isinstance(row.get("case_name"), str) or not row["case_name"].strip():
            self._unknown(row, line, "missing_case_name", "/case_name")
            return False
        if _reporter_citations(row) is None:
            self._unknown(row, line, "invalid_case_citations", "/citations")
        return self._date_selected(row, line)

    def record(self, row, line):
        if not isinstance(row, dict):
            raise ValueError("Sample record must be a mapping")
        identifier = _identifier(row.get("id"), "case")
        self.coverage["records_scanned"] += 1
        opinions = self._unique_opinions(row)
        identity = Blob.of(canonical_bytes(row)).sha256
        if identifier in self.case_ids:
            if self.case_ids[identifier] != identity:
                raise ValueError("Contradictory case identity")
            self.coverage["records_duplicate"] += 1
            return
        self.case_ids[identifier] = identity
        if not self._selected_case(row, line):
            return
        self.coverage["records_selected"] += 1
        if row.get("opinions") == []:
            self._unknown(row, line, "missing_opinions", "/opinions")
        matched = False
        for index, opinion in opinions:
            matched = self._opinion(row, opinion, line, index) or matched
        if matched:
            self.coverage["records_with_matches"] += 1


def project_sample(raw: bytes, source: Blob, configuration: dict) -> Result[dict]:
    try:
        if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_BODY:
            raise ValueError("Sample body must be nonempty and at most 32 MiB")
        if Blob.of(raw) != source:
            raise ValueError("Sample source hash or byte length mismatch")
        settings, grammar = _configuration(configuration)
        projection = _Projection(source, settings, grammar)
        for line, record in enumerate(io.BytesIO(raw), 1):
            if line > 10000 or len(record) > _MAX_RECORD:
                raise ValueError("Sample record count or 4 MiB record limit exceeded")
            try:
                projection.record(load_json(record.decode("utf-8")), line)
            except (ValueError, UnicodeError, TypeError, KeyError) as error:
                raise ValueError(f"Cannot project JSONL record {line}: {error}") from error
        if len(canonical_bytes(projection.product)) > _MAX_PRODUCT:
            raise ValueError("Projection exceeds 64 MiB output limit")
        return Ok(projection.product)
    except Exception as error:
        return Err(str(error))
