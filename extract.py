#!/usr/bin/env python3
"""
Extract USC statutory citation data from CourtListener search API.

No authentication required — uses public search endpoint.
Implements USCT-002 (explicit source acquisition outcomes and prior-snapshot fallback)
and USCT-004 (unreviewed search candidate provenance).

Usage:
    python extract.py --sample 10     # Test with 10 sections
    python extract.py --title 18      # Process all Title 18 sections
    python extract.py --full          # Process all sections (slow, ~8 hours)
    python extract.py --output ./annotations
"""

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from annotation_writer import write_annotations as _write_annotations


COURTLISTENER_SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"
REQUEST_DELAY_S = 0.5  # Rate limit: 2 requests/second max
RATE_LIMIT_WAIT_S = 10.0
MAX_RATE_LIMIT_RETRIES = 3
PAGE_SIZE = 20
MAX_PAGES = 5
USER_AGENT = "civic-source-us-code-tracker/1.0 (https://github.com/civic-source)"

MAX_CASE_NAME_LENGTH = 500
MAX_CITATION_LENGTH = 200
MAX_EXCERPT_LENGTH = 500
MAX_QUERY_LENGTH = 200
MAX_CASES_PER_SECTION = 20


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 Z format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_search_query(title: int, section: str) -> str:
    """Build the CourtListener statutory search query for a USC section."""
    return f'"{title} U.S.C. § {section}"'


def _fetch_json_url(
    url: str,
    max_retries: int = MAX_RATE_LIMIT_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch a single CourtListener URL with bounded 429 retry and structured outcome."""
    attempt = 0
    while True:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            status_code = getattr(resp, "status", 200) or 200
            raw_bytes = resp.read()
            payload = json.loads(raw_bytes)
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                return {
                    "ok": False,
                    "httpStatus": status_code,
                    "failureKind": "malformed_payload",
                    "errorMessage": "CourtListener response missing results array",
                }
            return {
                "ok": True,
                "httpStatus": status_code,
                "payload": payload,
            }
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                attempt += 1
                print(f"  Rate limited (attempt {attempt}/{max_retries}), waiting {RATE_LIMIT_WAIT_S}s...")
                sleep_fn(RATE_LIMIT_WAIT_S)
                continue
            failure_kind = "rate_limited" if e.code == 429 else "http_error"
            return {
                "ok": False,
                "httpStatus": e.code,
                "failureKind": failure_kind,
                "errorMessage": f"HTTP {e.code}",
            }
        except json.JSONDecodeError as e:
            return {
                "ok": False,
                "httpStatus": 200,
                "failureKind": "malformed_payload",
                "errorMessage": f"Malformed JSON: {e}",
            }
        except Exception as e:
            msg = str(e)
            failure_kind = "timeout" if "timed out" in msg.lower() or "timeout" in msg.lower() else "network_error"
            return {
                "ok": False,
                "httpStatus": None,
                "failureKind": failure_kind,
                "errorMessage": msg,
            }


def search_courtlistener(
    title: int,
    section: str,
    max_results: int = 10,
    max_retries: int = MAX_RATE_LIMIT_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """
    Search CourtListener for cases citing a USC section.
    Returns structured USCT-002 acquisition outcome metadata alongside `count` and `results`.
    """
    query = build_search_query(title, section)
    page_size = min(max(max_results, 1), PAGE_SIZE)
    target_pages = min(max(1, math.ceil(max_results / page_size)), MAX_PAGES)
    params = urllib.parse.urlencode({
        "q": query,
        "type": "o",
        "format": "json",
        "page_size": page_size,
    })
    next_url: str | None = f"{COURTLISTENER_SEARCH_URL}?{params}"

    collected: list[dict[str, Any]] = []
    total_count = 0
    completed_pages = 0
    expected_pages = 1
    last_http_status: int | None = 200

    while next_url and completed_pages < target_pages and len(collected) < max_results:
        page_res = _fetch_json_url(next_url, max_retries=max_retries, sleep_fn=sleep_fn)
        last_http_status = page_res.get("httpStatus")
        if not page_res.get("ok"):
            if completed_pages > 0:
                return {
                    "status": "partial",
                    "httpStatus": last_http_status,
                    "failureKind": "pagination_incomplete",
                    "errorMessage": page_res.get("errorMessage"),
                    "completedPages": completed_pages,
                    "expectedPages": max(expected_pages, completed_pages + 1),
                    "count": total_count,
                    "results": collected[:max_results],
                }
            print(
                f"  Failure ({page_res.get('failureKind')}) for {title} U.S.C. § {section}: {page_res.get('errorMessage')}",
                file=sys.stderr,
            )
            return {
                "status": "failed",
                "httpStatus": last_http_status,
                "failureKind": page_res.get("failureKind"),
                "errorMessage": page_res.get("errorMessage"),
                "completedPages": 0,
                "expectedPages": 1,
                "count": 0,
                "results": [],
            }

        payload = page_res["payload"]
        completed_pages += 1
        page_results = payload.get("results", [])
        if completed_pages == 1:
            raw_count = payload.get("count")
            total_count = int(raw_count) if isinstance(raw_count, int) else len(page_results)
            if total_count > 0:
                expected_pages = min(target_pages, max(1, math.ceil(min(total_count, max_results) / page_size)))

        for item in page_results:
            if isinstance(item, dict):
                collected.append(item)

        raw_next = payload.get("next")
        if isinstance(raw_next, str) and raw_next.strip():
            next_url = raw_next.strip()
        else:
            next_url = None
            break

    if completed_pages < expected_pages:
        return {
            "status": "partial",
            "httpStatus": last_http_status,
            "failureKind": "pagination_incomplete",
            "errorMessage": f"Completed {completed_pages} of {expected_pages} expected pages",
            "completedPages": completed_pages,
            "expectedPages": expected_pages,
            "count": total_count,
            "results": collected[:max_results],
        }

    status = "success-empty" if len(collected) == 0 else "success-nonempty"
    return {
        "status": status,
        "httpStatus": last_http_status or 200,
        "completedPages": completed_pages,
        "expectedPages": expected_pages,
        "count": total_count,
        "results": collected[:max_results],
    }


def classify_court(court_name: str, court_id: str | None = None) -> str:
    """
    Classify federal court identifier/name into SCOTUS/Appellate/District.
    Restricts SCOTUS strictly to the Supreme Court of the United States so state
    supreme courts are never misclassified as SCOTUS.
    """
    cid = (court_id or "").strip().lower()
    if cid == "scotus":
        return "SCOTUS"
    if cid in ("cadc", "cafc") or (cid.startswith("ca") and cid[2:].isdigit()):
        return "Appellate"

    if not court_name:
        return "District"
    lower = court_name.strip().lower()
    if (
        lower == "scotus"
        or "supreme court of the united states" in lower
        or "u.s. supreme court" in lower
        or "united states supreme court" in lower
    ):
        return "SCOTUS"
    if "circuit" in lower or "court of appeals" in lower or "appellate" in lower:
        return "Appellate"
    return "District"


def build_case_annotation(result: dict[str, Any], retrieval_query: str, retrieved_at: str) -> dict[str, Any]:
    """Build a USCT-004 / USCT-015 compliant candidate case annotation from a search hit."""
    citations = result.get("citation")
    first_citation = ""
    if isinstance(citations, list) and citations and citations[0]:
        first_citation = str(citations[0])
    elif isinstance(citations, str):
        first_citation = citations

    snippet = (result.get("snippet") or "")[:MAX_EXCERPT_LENGTH]
    absolute_url = result.get("absolute_url") or ""
    if absolute_url.startswith(("http://", "https://")):
        source_url = absolute_url
    elif absolute_url:
        source_url = f"https://www.courtlistener.com{absolute_url}"
    else:
        source_url = ""

    return {
        "caseName": (result.get("caseName") or "Unknown")[:MAX_CASE_NAME_LENGTH],
        "citation": first_citation[:MAX_CITATION_LENGTH],
        "court": classify_court(result.get("court") or "", result.get("court_id")),
        "date": result.get("dateFiled") or "",
        # Legacy field retained for sidecar compatibility (USCT-004: search snippet, not holding)
        "holdingSummary": snippet,
        "retrievalExcerpt": snippet,
        "retrievalQuery": retrieval_query[:MAX_QUERY_LENGTH],
        "retrievedAt": retrieved_at,
        "extractionState": "candidate",
        "editorialReview": "unreviewed",
        "versionAlignmentState": "unknown",
        "sourceUrl": source_url,
        "impact": "unverified_mention",
    }


def _normalize_search_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy `{count, results}` test doubles or structured outcomes into a canonical outcome dict."""
    if "status" in raw:
        return raw
    results = raw.get("results", [])
    count = raw.get("count", len(results))
    status = "success-empty" if len(results) == 0 else "success-nonempty"
    return {
        "status": status,
        "httpStatus": 200,
        "completedPages": 1,
        "expectedPages": 1,
        "count": count,
        "results": results,
    }


def process_sections(
    sections: list[tuple[int, str]],
    max_cases: int = 10,
    search_fn: Callable[[int, str, int], dict[str, Any]] = search_courtlistener,
    delay_s: float = REQUEST_DELAY_S,
) -> dict[str, dict[str, Any]]:
    """Process a list of (title, section) tuples and return annotation data with USCT-002 outcome metadata."""
    results: dict[str, dict[str, Any]] = {}
    total = len(sections)

    for i, (title, section) in enumerate(sections):
        key = f"title-{title}/section-{section}"
        retrieval_query = build_search_query(title, section)
        retrieved_at = utc_now_iso()
        outcome = _normalize_search_response(search_fn(title, section, max_cases))
        status = outcome.get("status", "failed")
        count = int(outcome.get("count", 0) or 0)
        raw_results = outcome.get("results", [])

        if status == "success-empty":
            if (i + 1) % 50 == 0:
                print(f"  [{i+1}/{total}] No cases for {title} U.S.C. § {section}")
            if delay_s > 0:
                time.sleep(delay_s)
            continue

        cases = [
            build_case_annotation(r, retrieval_query, retrieved_at)
            for r in raw_results
        ]

        results[key] = {
            "status": status,
            "observedAt": retrieved_at,
            "httpStatus": outcome.get("httpStatus"),
            "failureKind": outcome.get("failureKind"),
            "errorMessage": outcome.get("errorMessage"),
            "completedPages": outcome.get("completedPages", 1),
            "expectedPages": outcome.get("expectedPages", 1),
            "totalCases": count,
            "cases": cases,
        }

        print(f"  [{i+1}/{total}] {title} U.S.C. § {section}: {count} cases ({status})")
        if delay_s > 0:
            time.sleep(delay_s)

    return results


def get_sections_from_repo(repo_path: str, title_filter: int | None = None) -> list[tuple[int, str]]:
    """Read section list from the us-code content-data repo."""
    statutes_dir = Path(repo_path) / "statutes"
    sections: list[tuple[int, str]] = []

    if not statutes_dir.exists():
        print(f"Statutes dir not found: {statutes_dir}", file=sys.stderr)
        return sections

    for title_dir in sorted(statutes_dir.iterdir()):
        if not title_dir.is_dir() or not title_dir.name.startswith("title-"):
            continue
        title_num = int(title_dir.name.replace("title-", ""))
        if title_filter and title_num != title_filter:
            continue

        for chapter_dir in sorted(title_dir.iterdir()):
            if not chapter_dir.is_dir():
                continue
            for section_file in sorted(chapter_dir.iterdir()):
                if section_file.suffix != ".md":
                    continue
                section_num = section_file.stem.replace("section-", "")
                sections.append((title_num, section_num))

    return sections


def write_annotations(results: dict[str, dict[str, Any]], output_dir: Path) -> int:
    """Write automatic annotations through the protected output boundary."""
    return _write_annotations(results, output_dir, utc_now_iso(), MAX_CASES_PER_SECTION)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract USC citation data from CourtListener")
    parser.add_argument("--sample", type=int, help="Process N sample sections")
    parser.add_argument("--title", type=int, help="Process only this title number")
    parser.add_argument("--repo", type=str, help="Path to us-code content-data repo")
    parser.add_argument("--full", action="store_true", help="Process all sections")
    parser.add_argument("--output", type=str, default="./annotations", help="Output directory")
    parser.add_argument("--max-cases", type=int, default=10, help="Max cases per section")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing YAML files")
    args = parser.parse_args()

    if args.repo:
        sections = get_sections_from_repo(args.repo, args.title)
    else:
        sections = [
            (18, "111"), (18, "1001"), (18, "924"), (18, "1341"),
            (42, "1983"), (42, "1985"), (42, "2000e"),
            (26, "7201"), (26, "61"),
            (28, "1332"),
        ]

    if args.sample:
        sections = sections[:args.sample]

    if not sections:
        print("No sections to process. Use --repo or --sample.", file=sys.stderr)
        sys.exit(1)

    print(f"Processing {len(sections)} sections...")
    results = process_sections(sections, args.max_cases)

    if results:
        output_dir = Path(args.output)
        if args.dry_run:
            print(f"\n[dry-run] Would write {len(results)} annotation files to {output_dir}/")
        else:
            written = write_annotations(results, output_dir)
            print(f"\nWrote {written} annotation files to {output_dir}/")

        print("\nTop 10 most-cited sections:")
        sorted_results = sorted(results.items(), key=lambda x: x[1].get("totalCases", 0), reverse=True)
        for key, data in sorted_results[:10]:
            print(f"  {key}: {data.get('totalCases', 0):,} cases ({data.get('status', 'unknown')})")
    else:
        print("No results found.")


if __name__ == "__main__":
    main()
