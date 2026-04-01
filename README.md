# CAP Citation Pipeline

Extract case law citation data for U.S. Code sections from [CourtListener](https://www.courtlistener.com/) — no authentication required.

## What it does

1. Queries CourtListener's public search API for each USC section
2. Retrieves case metadata (name, court, date, URL)
3. Generates YAML annotation sidecars for [us-code-tracker](https://github.com/civic-source/us-code-tracker)
4. Annotations power the "Case Law" section on statute pages

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Test with 10 sample sections
python extract.py --sample 10

# Process all sections in a specific title
python extract.py --repo /path/to/us-code --title 18

# Process all sections (slow, ~8 hours for 60K sections)
python extract.py --repo /path/to/us-code --full
```

## Output format

```yaml
# annotations/title-18/section-111.yaml
targetSection: "18 U.S.C. § 111"
lastSyncedET: "2026-03-31T03:28:58+00:00"
totalCases: 1298
cases:
  - caseName: "United States v. Mobley"
    citation: "344 F. Supp. 3d 1089"
    court: "District"
    date: "2018-10-01"
    sourceUrl: "https://www.courtlistener.com/opinion/7332965/..."
    impact: "interpretation"
```

## Data source

**CourtListener** — public search API, no authentication required. Rate-limited at 0.5s between requests.

Top cited sections from our analysis:

| Section | Cases |
|---------|-------|
| 42 U.S.C. § 1983 | 88,031 |
| 42 U.S.C. § 2000e | 32,717 |
| 28 U.S.C. § 1332 | 26,106 |
| 18 U.S.C. § 922 | 15,800 |

## License

Apache 2.0 (code) / CC0 (data)
