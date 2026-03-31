#!/usr/bin/env python3
"""
Extract USC statutory citation data from CourtListener search API.

No authentication required — uses public search endpoint.

Usage:
    python extract.py --sample 10     # Test with 10 sections
    python extract.py --title 18      # Process all Title 18 sections
    python extract.py --full          # Process all sections (slow, ~8 hours)
    python extract.py --output ./annotations
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml


COURTLISTENER_SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"
REQUEST_DELAY_S = 0.5  # Rate limit: 2 requests/second max
USER_AGENT = "civic-source-us-code-tracker/1.0 (https://github.com/civic-source)"


def search_courtlistener(title: int, section: str, max_results: int = 10) -> dict:
    """Search CourtListener for cases citing a USC section."""
    query = f'"{title} U.S.C. § {section}"'
    params = urllib.parse.urlencode({
        "q": query,
        "type": "o",
        "format": "json",
        "page_size": min(max_results, 20),
    })
    url = f"{COURTLISTENER_SEARCH_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print(f"  Rate limited, waiting 10s...")
            time.sleep(10)
            return search_courtlistener(title, section, max_results)
        print(f"  HTTP {e.code} for {title} U.S.C. § {section}", file=sys.stderr)
        return {"count": 0, "results": []}
    except Exception as e:
        print(f"  Error for {title} U.S.C. § {section}: {e}", file=sys.stderr)
        return {"count": 0, "results": []}


def classify_court(court_name: str) -> str:
    """Classify court name into SCOTUS/Appellate/District."""
    if not court_name:
        return "District"
    lower = court_name.lower()
    if "supreme" in lower:
        return "SCOTUS"
    if "circuit" in lower or "appeal" in lower:
        return "Appellate"
    return "District"


def process_sections(sections: list[tuple[int, str]], max_cases: int = 10) -> dict[str, dict]:
    """Process a list of (title, section) tuples and return annotation data."""
    results = {}
    total = len(sections)

    for i, (title, section) in enumerate(sections):
        key = f"title-{title}/section-{section}"
        data = search_courtlistener(title, section, max_cases)
        count = data.get("count", 0)

        if count == 0:
            if (i + 1) % 50 == 0:
                print(f"  [{i+1}/{total}] No cases for {title} U.S.C. § {section}")
            time.sleep(REQUEST_DELAY_S)
            continue

        cases = []
        for r in data.get("results", []):
            cases.append({
                "caseName": r.get("caseName", "Unknown"),
                "citation": r.get("citation", [None])[0] if r.get("citation") else "",
                "court": classify_court(r.get("court", "")),
                "date": r.get("dateFiled", ""),
                "holdingSummary": (r.get("snippet", "") or "")[:500],
                "sourceUrl": f"https://www.courtlistener.com{r.get('absolute_url', '')}",
                "impact": "interpretation",
            })

        results[key] = {
            "totalCases": count,
            "cases": cases,
        }

        print(f"  [{i+1}/{total}] {title} U.S.C. § {section}: {count} cases")
        time.sleep(REQUEST_DELAY_S)

    return results


def get_sections_from_repo(repo_path: str, title_filter: int | None = None) -> list[tuple[int, str]]:
    """Read section list from the us-code content-data repo."""
    statutes_dir = Path(repo_path) / "statutes"
    sections = []

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


def write_annotations(results: dict[str, dict], output_dir: Path) -> int:
    """Write YAML annotation files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    written = 0

    for key, data in results.items():
        parts = key.split("/")
        if len(parts) != 2:
            continue

        title_dir = output_dir / parts[0]
        title_dir.mkdir(parents=True, exist_ok=True)

        title_num = parts[0].replace("title-", "")
        section_num = parts[1].replace("section-", "")

        annotation = {
            "targetSection": f"{title_num} U.S.C. § {section_num}",
            "lastSyncedET": now,
            "totalCases": data["totalCases"],
            "cases": data["cases"][:20],  # Cap at 20 per section
        }

        section_file = title_dir / f"{parts[1]}.yaml"
        with open(section_file, "w") as f:
            yaml.dump(annotation, f, default_flow_style=False, allow_unicode=True)
        written += 1

    return written


def main():
    parser = argparse.ArgumentParser(description="Extract USC citation data from CourtListener")
    parser.add_argument("--sample", type=int, help="Process N sample sections")
    parser.add_argument("--title", type=int, help="Process only this title number")
    parser.add_argument("--repo", type=str, help="Path to us-code content-data repo")
    parser.add_argument("--full", action="store_true", help="Process all sections")
    parser.add_argument("--output", type=str, default="./annotations", help="Output directory")
    parser.add_argument("--max-cases", type=int, default=10, help="Max cases per section")
    args = parser.parse_args()

    # Get section list
    if args.repo:
        sections = get_sections_from_repo(args.repo, args.title)
    else:
        # Default sample sections for testing
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
        written = write_annotations(results, output_dir)
        print(f"\nWrote {written} annotation files to {output_dir}/")

        # Top cited sections
        print("\nTop 10 most-cited sections:")
        sorted_results = sorted(results.items(), key=lambda x: x[1]["totalCases"], reverse=True)
        for key, data in sorted_results[:10]:
            print(f"  {key}: {data['totalCases']:,} cases")
    else:
        print("No results found.")


if __name__ == "__main__":
    main()
