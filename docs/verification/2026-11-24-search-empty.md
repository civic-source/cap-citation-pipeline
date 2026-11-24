# Searched-empty outcome verification — 2026-11-24

Scope: civic-source/cap-citation-pipeline#12, based on main
`1e9ab73cb8df5d45e3e475bc61bec10066ae7e13`.

The public `process_sections` mapping retains successful-empty observations.
Nonempty and failed entries retain their existing fields and key identities.
Only local writer/CLI/tests call this function. Sidecar mutation explicitly skips
successful-empty results so neither fresh empty files nor destructive clearing of
prior candidates/reviewed content occurs. Query coverage is an optional separate
receipt, never an annotation-schema field.

## Failing-first evidence

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_search_coverage.py
```

Initial result: **5 failed, 1 passed**. The mixed nonempty/empty/failed cohort
lost its empty entry; direct empty writes cleared a candidate; receipt support
was absent; and an all-empty CLI mapping produced an empty sidecar instead of
the existing no-results behavior. A later public CLI regression also failed
before preflight was added for an existing receipt destination, preventing wasted
search requests before an inevitable exclusive-output failure.

## Final local verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
ruff check --select F,C901 extract.py annotation_writer.py search_coverage.py tests/test_search_coverage.py
git diff --check
```

Observed: **174 passed in 10.44s**; Ruff and whitespace checks passed. No external
API requests, dependency installation, CI changes or content-data writes occurred.
The existing annotation review-guard tests and all bulk-source tests remain green.

The bulk spec and ADR also replace UTC-filter wording with the source-accurate
reported filing-date terminology. That wording has no data/recipe effect.

`extract.py` remains a justified legacy size exception: 430 lines versus 425 at
baseline, with the new receipt logic in a separate 92-line module. Unrelated
network/search behavior was not refactored. New functions remain below the
project complexity limit.

Independent exact-model real-diff review and GitHub CI/merge receipts are tracked
on the PR; local tests alone do not claim merged or deployed status.
