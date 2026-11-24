# Joined reader, acquisition and pipeline verification

Scope: one historical original COLD shard under ADR 0002. This transcript records
code tests; no full source transfer, historical data acceptance, deployment or
editorial acceptance is claimed here.

## Failing-first checks

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_joined_projection.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_joined_pipeline.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_joined_cli.py
```

Each first run failed collection because its implementation module did not exist.
Real Parquet fixtures then exercised native date32/nullable IDs and opinion lists,
exact Unicode spans, row-group locators, zero-match scope, duplicate identities,
contradictions, malformed footer/hash, deterministic partitions across batch sizes,
output/index budgets, actual worker wall timeout and address-space failure.

An added missing-case/reporter regression failed on absent reporter-unresolved
metadata before its implementation. A separate counter assertion failed with
zero unknown records for four explicitly unresolved records; it passes with
record-scoped counting. Original literal section spans include subsection text;
fixtures assert the exact source span and separate normalized section/subsection.

Pipeline tests prove failed projection retains verified raw acquisition for retry,
without another fetch or selected joined snapshot; successful processing replays
offline; wrong/tampered products and quota refusal preserve prior selection.
CLI tests verify exact source planning, explicit cohort and failure exit behavior,
separate namespace and a 4 GiB reserve. Store retention tests verify immutable
context manifests can be retained/read by ID without selecting any pointer.

## Observed checks

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
ruff check --select F,C901 joined*.py tests/test_joined*.py
git diff --check
```

Whole-suite integration observed 257 passed in 15.52 seconds at this checkpoint,
including then-present multipart archive/GitHub tests. Ruff and whitespace passed.
Other parallel tracks may add tests before the final combined verification.

All owned implementation functions remain at most 50 lines of logic and modules
at most 400 lines. Existing pilot recipe files `bulk_common.py`,
`cold_projection.py` and `usc_citations.py` remain byte-identical. The optional
reader is pinned to PyArrow 23.0.1; the scoped CI installation enables genuine
Parquet tests without skipping the decoder path. No local dependency installation
or bulk external request was required for these tests.

Capture uses one verified complete decode. Independent evidence checks and a
fresh-cache remote restore plus network-blocked replay remain separate live
acceptance gates. Worker resource observations are outside canonical evidence.

## Supplemental bounded observation and operator checks

Three added public tests failed before implementation: worker observations were
read without a byte bound on the identical-generation path, capture could not
reuse a verified stored cohort, and planning omitted explicit capacity readiness.
A further nonfinite `1e999` elapsed-time regression failed before finite-number
validation. The observation reader now opens a regular single-link file, checks
its size before reading at most 8,193 bytes, and validates both fields before
any selected-pointer publication, including identical-generation handling.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_joined_pipeline.py tests/test_joined_cli.py tests/test_joined_projection.py
ruff check --select F,C901 joined.py joined_pipeline.py tests/test_joined_pipeline.py tests/test_joined_cli.py
git diff --check
```

Observed: 21 passed in 5.60 seconds; Ruff and whitespace passed. This supplement
retains every original assertion and adds bounded-read, finite-number, prior
selection, stored-cohort and plan-readiness assertions.
