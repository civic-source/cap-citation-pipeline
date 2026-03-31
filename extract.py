#!/usr/bin/env python3
"""
Extract USC statutory citations from CAP (Caselaw Access Project) opinions.

Usage:
    python extract.py --sample 100    # Test with 100 opinions
    python extract.py --full          # Process all federal opinions
    python extract.py --output ./annotations  # Custom output dir
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml
from eyecite import get_citations
from eyecite.models import FullLawCitation


def extract_usc_citations(text: str) -> list[dict]:
    """Extract USC citations from opinion text using eyecite."""
    citations = get_citations(text)
    usc_cites = []
    for cite in citations:
        if isinstance(cite, FullLawCitation):
            groups = cite.groups
            reporter = groups.get("reporter", "")
            if "U.S.C." in reporter:
                usc_cites.append({
                    "title": groups.get("title", ""),
                    "section": groups.get("section", ""),
                    "pin_cite": cite.metadata.pin_cite,
                    "raw": str(cite),
                })
    return usc_cites


def process_sample(n: int = 100) -> dict[str, list[dict]]:
    """Process a sample of CAP opinions and return citation mappings."""
    try:
        from datasets import load_dataset
    except ImportError:
        print("Install datasets: pip install datasets", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {n} sample opinions from CAP...")
    ds = load_dataset(
        "free-law/Caselaw_Access_Project",
        split=f"train[:{n}]",
        streaming=True,
    )

    # Map: "title-N/section-M" -> [case info]
    section_cases: dict[str, list[dict]] = defaultdict(list)
    total_cites = 0
    processed = 0

    for row in ds:
        processed += 1
        text = row.get("text", "") or ""
        if not text:
            continue

        cites = extract_usc_citations(text)
        if not cites:
            continue

        case_name = row.get("name_abbreviation", "") or row.get("name", "Unknown")
        court = row.get("court", {})
        court_name = court.get("name", "Unknown") if isinstance(court, dict) else str(court)
        date = row.get("decision_date", "")

        for cite in cites:
            key = f"title-{cite['title']}/section-{cite['section']}"
            section_cases[key].append({
                "caseName": case_name,
                "citation": cite["raw"],
                "court": classify_court(court_name),
                "date": date or "",
                "impact": "interpretation",
            })
            total_cites += 1

        if processed % 10 == 0:
            print(f"  Processed {processed}/{n} opinions, {total_cites} USC citations found")

    print(f"\nDone: {processed} opinions, {total_cites} USC citations, {len(section_cases)} unique sections")
    return dict(section_cases)


def classify_court(name: str) -> str:
    """Classify court name into SCOTUS/Appellate/District."""
    name_lower = name.lower()
    if "supreme" in name_lower:
        return "SCOTUS"
    if "circuit" in name_lower or "appeal" in name_lower:
        return "Appellate"
    return "District"


def write_annotations(section_cases: dict[str, list[dict]], output_dir: Path) -> int:
    """Write YAML annotation files for each section."""
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    written = 0

    for key, cases in section_cases.items():
        parts = key.split("/")
        if len(parts) != 2:
            continue

        title_dir = output_dir / parts[0]
        title_dir.mkdir(parents=True, exist_ok=True)

        section_file = title_dir / f"{parts[1]}.yaml"

        # Extract title and section numbers for targetSection
        title_num = parts[0].replace("title-", "")
        section_num = parts[1].replace("section-", "")

        annotation = {
            "targetSection": f"{title_num} U.S.C. § {section_num}",
            "lastSyncedET": now,
            "cases": cases[:50],  # Cap at 50 cases per section
        }

        with open(section_file, "w") as f:
            yaml.dump(annotation, f, default_flow_style=False, allow_unicode=True)
        written += 1

    return written


def main():
    parser = argparse.ArgumentParser(description="Extract USC citations from CAP opinions")
    parser.add_argument("--sample", type=int, default=100, help="Number of sample opinions (default: 100)")
    parser.add_argument("--full", action="store_true", help="Process all federal opinions")
    parser.add_argument("--output", type=str, default="./annotations", help="Output directory")
    args = parser.parse_args()

    n = args.sample if not args.full else 10000  # Start with 10K for --full, scale up later

    section_cases = process_sample(n)

    if section_cases:
        output_dir = Path(args.output)
        written = write_annotations(section_cases, output_dir)
        print(f"\nWrote {written} annotation files to {output_dir}/")

        # Print top cited sections
        print("\nTop 10 most-cited sections:")
        sorted_sections = sorted(section_cases.items(), key=lambda x: len(x[1]), reverse=True)
        for key, cases in sorted_sections[:10]:
            print(f"  {key}: {len(cases)} citations")
    else:
        print("No USC citations found in sample.")


if __name__ == "__main__":
    main()
