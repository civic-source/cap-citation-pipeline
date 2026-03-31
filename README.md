# CAP Citation Pipeline

Extract USC (United States Code) statutory citations from [Caselaw Access Project](https://case.law/) opinions using [eyecite](https://github.com/freelawproject/eyecite).

## What it does

1. Downloads case opinions from CAP (via HuggingFace Parquet dataset)
2. Extracts USC citations using eyecite (`get_citations()`)
3. Maps citations to US Code sections (title + section)
4. Generates YAML annotation sidecars for [us-code-tracker](https://github.com/civic-source/us-code-tracker)

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python extract.py --sample 100    # Test with 100 opinions
python extract.py --full          # Process all federal opinions
```

## Output format

```yaml
# annotations/title-18/section-111.yaml
targetSection: "18 U.S.C. § 111"
lastSyncedET: "2026-03-30T22:00:00-04:00"
cases:
  - caseName: "United States v. Smith"
    citation: "500 U.S. 123 (2020)"
    court: "SCOTUS"
    date: "2020-06-15"
    holdingSummary: "..."
    sourceUrl: "https://case.law/..."
    impact: "interpretation"
```

## Data sources

- **CAP**: [free-law/Caselaw_Access_Project](https://huggingface.co/datasets/free-law/Caselaw_Access_Project) (6.7M cases, Parquet)
- **eyecite**: v2.7.6 — validated for USC citations (100% precision, ~70% recall)

## Validation results

| Format | Detected |
|--------|----------|
| `18 U.S.C. § 111` | Yes |
| `42 U.S.C. §§ 1983` | Yes (handles §§) |
| `28 U.S.C. § 1332(a)` | Yes (pin cite) |
| `15 U.S.C. § 78j(b)` | No (alphanumeric) |

## License

Apache 2.0 (code) / CC0 (data)
