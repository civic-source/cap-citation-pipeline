# Explicit section-search coverage

`process_sections` returns one mapping entry for every attempted section, including
`success-empty`, `partial` and `failed` outcomes. An absent key means no attempt
is recorded; it does not mean the search returned no cases. The existing key and
per-section metadata shapes remain unchanged.

The annotation writer does not create an empty sidecar or clear a prior candidate
on `success-empty`. Prior reviewed content remains protected byte for byte.
Acquisition observations and annotation publication are separate operations.

To retain a separate acquisition receipt:

```bash
python3 extract.py --repo /path/to/us-code --title 18 --sample 10 \
  --coverage-out /persistent/path/title18-search-coverage.json
```

The optional receipt is published exclusively; an existing destination is refused
before searching. No receipt file is written unless the flag is supplied. A
requested receipt can accompany `--dry-run`, which still does not write YAML.

The `cap.courtlistener-search-coverage/v1` JSON contract records the declared
literal queries, each actual observation/status, returned versus collected case
counts, and expected/attempted/completed/empty/failed/unattempted counts. Missing
scopes receive `not_collected` without an invented observation time. Duplicate
cohorts, undeclared result scopes and contradictory empty status/counts are
rejected. `build_search_coverage(sections, results)` also supports a deliberately
partial result mapping for bounded retry planning.

`coverageStatus: complete` means the declared query attempts returned completed
outcomes within the existing client limits. It does not establish complete case
collection, every citation variant, a global absence of relevant cases, editorial
acceptance or deployment. The receipt explicitly retains
`globalCorpusComplete: false`. Empty-only CLI runs keep the existing no-results
message and do not publish annotation files. Failed acquisition remains failed,
including when no cases were returned.
