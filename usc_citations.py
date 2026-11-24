"""Conservative literal U.S. Code cohort references with decoded-text spans."""

from bisect import bisect_left, bisect_right
import re
from bulk_common import Err, Ok, Result

GRAMMAR = "literal-us-code/1"
_HEADER = re.compile(r"(?<!\w)(\d+[A-Za-z]?)\s+U\.?\s*S\.?\s*C\.?(?:\s*§{1,2})?\s*", re.IGNORECASE)
_TOKEN = re.compile(r"\d+[A-Za-z]*(?:\s*[-–]\s*\d+[A-Za-z]*)*(?:\([A-Za-z0-9]+\))*")
_SEPARATOR = re.compile(r"(?:\s*,\s*(?:and\s+)?|\s+and\s+)", re.IGNORECASE)
_ID = re.compile(r"^[0-9]+[A-Za-z]*(?:-[0-9]+[A-Za-z]*)*$")


def _tokens(text, marker):
    tokens = []
    cursor = marker.end()
    reason = None
    for _ in range(128):
        token = _TOKEN.match(text, cursor)
        if (
            not token
            or token.end() - cursor > 256
            or (
                token.end() < len(text)
                and text[token.end()]
                in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789()–-/"
            )
        ):
            reason = "unsupported_section_syntax"
            break
        tokens.append(token)
        cursor = token.end()
        separator = _SEPARATOR.match(text, cursor)
        if not separator or separator.end() >= len(text) or not text[separator.end()].isdigit():
            break
        if _HEADER.match(text, separator.end()):
            break
        cursor = separator.end()
    else:
        reason = "section_list_limit"
    if re.match(r"\s+et\s+seq\b", text[cursor : cursor + 32], re.IGNORECASE):
        reason = "open_ended_reference"
    return tokens, cursor, reason


def _targets(token, cohort, numeric):
    compact = re.sub(r"\s+", "", token.group())
    ranged = re.fullmatch(r"(\d+)[-–](\d+)", compact)
    if ranged:
        first, last = map(int, ranged.groups())
        if first > last or last - first > 10000:
            return [], "unsupported_range"
        values, labels = numeric
        return [
            (labels[value], "", "range")
            for value in values[bisect_left(values, first) : bisect_right(values, last)]
        ], None
    single = re.match(r"\d+[A-Za-z]*(?:-\d+)*", compact)
    if not single:
        return [], "unsupported_section_syntax"
    base = single.group()
    suffix = compact[len(base) :]
    if suffix and not re.fullmatch(r"(?:\([A-Za-z0-9]+\))+", suffix):
        return [], "unsupported_section_syntax"
    label = cohort.get(base.casefold())
    return ([(label, suffix, "single")] if label else []), None


def _token_evidence(text, marker, token, lookup, end):
    targets, unsupported = _targets(token, *lookup)
    if unsupported:
        return [], [{"start": token.start(), "end": token.end(), "text": token.group(), "reason": unsupported}]
    return [
        {"section": section, "subsection": subsection, "matchType": kind,
         "sectionSpan": {"start": token.start(), "end": token.end()},
         "citationSpan": {"start": marker.start(), "end": end},
         "citationText": text[marker.start():end]}
        for section, subsection, kind in targets
    ], []


def find_usc_citations(text: str, usc_title: str, sections: list[str]) -> Result[dict[str, object]]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > 4 * 1024**2:
        return Err("Opinion text exceeds 4 MiB matcher limit")
    if (
        not re.fullmatch(r"[1-9][0-9]*[A-Za-z]?", usc_title)
        or len(sections) > 10000
        or len({section.casefold() for section in sections}) != len(sections)
        or any(not _ID.fullmatch(section) for section in sections)
    ):
        return Err("Invalid USC title/cohort")
    cohort = {section.casefold(): section for section in sections}
    labels = {int(section): section for section in sections if section.isdecimal()}
    numeric = (sorted(labels), labels)
    matches = []
    unresolved = []
    markers = 0
    for marker in _HEADER.finditer(text):
        if marker.group(1).casefold() != usc_title.casefold():
            continue
        markers += 1
        if markers > 10000:
            return Err("Opinion citation marker limit exceeded")
        tokens, end, reason = _tokens(text, marker)
        if reason:
            unresolved.append(
                {
                    "start": marker.start(),
                    "end": max(marker.end(), end),
                    "text": text[marker.start() : max(marker.end(), end)],
                    "reason": reason,
                }
            )
            if reason in {"open_ended_reference", "section_list_limit"}:
                continue
        for token in tokens:
            candidates, findings = _token_evidence(text, marker, token, (cohort, numeric), end)
            if len(matches) + len(candidates) > 50000:
                return Err("Opinion candidate match limit exceeded")
            matches.extend(candidates)
            unresolved.extend(findings)
    return Ok({"grammar": GRAMMAR, "matches": matches, "unresolved": unresolved})
