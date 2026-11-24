"""Streaming historical joined records into candidate literal-citation evidence."""
from bulk_common import Err
from joined_rules import CANDIDATE, court_selection, evidence, identity, normalize, text_base
from usc_citations import find_usc_citations


class Projection:
    def __init__(self, configuration, grammar, output):
        self.configuration, self.grammar, self.output = configuration, grammar, output
        counters = ("records_scanned", "opinions_scanned", "records_selected", "opinions_selected", "records_excluded_court", "records_excluded_date", "records_unknown_metadata", "opinions_unknown_metadata", "records_unknown_date", "records_approximate_date", "records_unknown_opinion_collection", "records_duplicate", "opinions_duplicate", "records_with_matches", "opinions_with_matches")
        self.coverage = dict.fromkeys(counters, 0)
        self.coverage.update({"shard_enumeration_complete": True, "selected_opinions_complete": True,
                              "global_corpus_complete": False, "extraction_coverage": "supported-literal-only"})

    def unknown(self, row, locator, reason, field, *, affects_selected=True):
        if not self.unknown_seen:
            self.coverage["records_unknown_metadata"] += 1
            self.unknown_seen = True
        if affects_selected:
            self.coverage["selected_opinions_complete"] = False
        opinion = row["opinions"][locator["opinion_index"]] if "opinion_index" in locator else None
        raw_identity = {"opinion_id": opinion.get("opinion_id")} if opinion is not None else {}
        self.output.emit(locator["row_group"], {**locator, **raw_identity, **CANDIDATE, "kind": "unresolved", "reason": reason,
                         "case_id": row.get("id"), "field_path": field,
                         "source_metadata": {key: row.get(key) for key in ("case_name", "date_filed", "court_type", "court_jurisdiction", "court_full_name")}})

    def opinions(self, row, locator):
        values = row.get("opinions")
        if values is None:
            self.coverage["records_unknown_opinion_collection"] += 1
            return None
        if not isinstance(values, list) or len(values) > 1000:
            raise ValueError("Invalid or oversized opinion collection")
        self.coverage["opinions_scanned"] += len(values)
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("Invalid opinion structure")
            text = value.get("opinion_text")
            if isinstance(text, str) and len(text.encode("utf-8")) > 4 * 1024**2:
                raise ValueError("Opinion text exceeds 4 MiB limit")
        return values

    def selected(self, row, locator, opinions):
        selected, reason = court_selection(row, self.configuration["court_types"])
        since, observed = self.configuration["since"], row.get("date_filed")
        in_window = since is None or observed is None or observed >= since
        if opinions is None:
            self.unknown(row, locator, "unavailable_opinion_collection", "/opinions", affects_selected=selected and in_window)
        if reason:
            self.unknown(row, locator, reason, "/court_type")
            return False
        if not selected:
            self.coverage["records_excluded_court"] += 1
            return False
        return self.selected_metadata(row, locator)

    def selected_metadata(self, row, locator):
        if not isinstance(row.get("case_name"), str) or not row["case_name"].strip():
            self.unknown(row, locator, "missing_case_name", "/case_name")
            return False
        citations = row.get("citations")
        if not isinstance(citations, list) or any(not isinstance(item, str) for item in citations):
            self.unknown(row, locator, "invalid_case_citations", "/citations")
        observed, since = row.get("date_filed"), self.configuration["since"]
        if row.get("date_filed_is_approximate") is True:
            self.coverage["records_approximate_date"] += 1
        if observed is None:
            self.coverage["records_unknown_date"] += 1
            self.unknown(row, locator, "unknown_source_date", "/date_filed", affects_selected=since is not None)
        if since is not None and (observed is None or observed < since):
            self.coverage["records_excluded_date"] += 1
            return False
        return True

    def opinion(self, row, opinion, locator):
        identifier, text = opinion.get("opinion_id"), opinion.get("opinion_text")
        if not identity(identifier):
            self.coverage["opinions_unknown_metadata"] += 1
            self.unknown(row, locator, "missing_opinion_id", locator["field_path"].rsplit("/", 1)[0] + "/opinion_id")
            return False
        if not self.output.unique("opinion", identifier, {"case_id": row["id"], "opinion": opinion}):
            self.coverage["opinions_duplicate"] += 1
            return False
        if not isinstance(text, str) or not text.strip():
            self.coverage["opinions_unknown_metadata"] += 1
            self.unknown(row, locator, "missing_opinion_text", locator["field_path"])
            return False
        if len(text.encode("utf-8")) > 4 * 1024**2:
            raise ValueError("Opinion text exceeds 4 MiB limit")
        self.coverage["opinions_selected"] += 1
        result = find_usc_citations(text, self.configuration["usc_title"], self.configuration["sections"])
        if isinstance(result, Err):
            raise ValueError(result.error)
        base = text_base(row, opinion, locator, text)
        self.emit_citations(base, text, result.value)
        if result.value["matches"]:
            self.coverage["opinions_with_matches"] += 1
        return bool(result.value["matches"])

    def emit_citations(self, base, text, result):
        for match in result["matches"]:
            item = evidence(base, text, match["citationSpan"], match["sectionSpan"])
            self.output.emit(base["row_group"], {**item, "kind": "match", "section": match["section"],
                             "subsection": match["subsection"], "match_type": match["matchType"]})
        for item in result["unresolved"]:
            located = evidence(base, text, {"start": item["start"], "end": item["end"]})
            self.output.emit(base["row_group"], {**located, "kind": "unresolved", "reason": item["reason"]})

    def record(self, native, locator):
        self.unknown_seen = False
        row = normalize(native)
        self.coverage["records_scanned"] += 1
        opinions = self.opinions(row, locator)
        if not identity(row.get("id")):
            self.unknown(row, locator, "missing_case_id", "/id")
            return
        if not self.output.unique("case", row["id"], row):
            self.coverage["records_duplicate"] += 1
            return
        if not self.selected(row, locator, opinions):
            return
        self.coverage["records_selected"] += 1
        if opinions == []:
            self.unknown(row, locator, "missing_opinions", "/opinions")
        matched = False
        for index, opinion in enumerate(opinions or []):
            nested = {**locator, "opinion_index": index, "field_path": f"/opinions/{index}/opinion_text"}
            matched = self.opinion(row, opinion, nested) or matched
        if matched:
            self.coverage["records_with_matches"] += 1
