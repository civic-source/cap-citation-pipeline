# Retained precedent-source qualification

Run `bulk.py` with Python 3.12 or newer on a POSIX filesystem. The bulk path uses
only the standard library; existing search/writer commands still use their
existing dependencies. GitHub archive/restore requires authenticated `gh`.

The [observed source contract](courtlistener-source-contract.md) records native
table relationships, parser probes, sizing limits and update constraints.

This workflow qualifies two distinct sources:

- Native current CourtListener **inventory metadata**, including object sizes,
  dates, validators and publication gaps. It does not download native table bodies.
- The pinned official **historical 2023 joined COLD JSONL sample**: 12,907,287 bytes,
  file revision `5907c8453ffdfb5a6798d4fb8cb8faebdfc1025f`, publisher Git blob
  `d0a099b3b8ff136dfc084e8899908cac40622066`. This is neither the entire COLD
  dataset nor a current CourtListener corpus. The partial dataset viewer is not
  an authoritative count of the complete Parquet publication.

Source code licensing and source-data licensing are distinct: the exporter
repository uses MIT; the published COLD data card declares CC0-1.0. Retained
source identity and actual SHA-256 are recorded independently of those labels.

## Plan, capture and inspect

Choose persistent storage. Do not use a RAM-backed `/tmp` for corpus/cache data.
Default quota is 16 GiB per cache and minimum free-space reserve is 2 GiB.
Budgets include retained generations, source staging, derived outputs and archive/
readback/restore copies. Writes repeat capacity checks; no retained source is
silently pruned. Explicit overrides are `--max-cache-gib` and `--min-free-gib`.

```bash
export PRECEDENT_CACHE=/persistent/path/precedent-bulk
python3 bulk.py --action plan --cache "$PRECEDENT_CACHE"
python3 bulk.py --action inventory --cache "$PRECEDENT_CACHE"
python3 bulk.py --action capture --cache "$PRECEDENT_CACHE" \
  --usc-title 18 --data-repo /path/to/us-code
python3 bulk.py --action status --cache "$PRECEDENT_CACHE"
```

Use `--sections 111,924,1001` for an explicit cohort or `--limit 100` with a data
repository. The exact cohort is retained. A verified source acquisition is
accepted separately from its projection, so a failed transform can resume from
retained bytes. A case snapshot pointer advances only after its source and
candidate product have verified. Corruption fails closed without fetching latest
substitutes. A single writer lock serializes cache mutations.

The observed native 2026-09-30 tables require about 63 GB compressed for opinions
and joins. Published CSV uses PostgreSQL backslash escaping and quoted multiline
records; compressed ranges are not independent row filters. The observed schema
contains an opinion-content relationship that is absent from the published bulk
objects. The inventory reports this gap explicitly. The pilot's 32 MiB source
and 256 MiB archive bounds cannot be used to silently ingest this larger corpus.
Native joins/multipart backfill are tracked in civic-source/cap-citation-pipeline#17.

## Incremental processing and offline replay

```bash
python3 bulk.py --action capture --cache "$PRECEDENT_CACHE" --since 2020-01-01
python3 bulk.py --action inventory --offline --cache "$PRECEDENT_CACHE"
python3 bulk.py --action rebuild --cache "$PRECEDENT_CACHE"
```

A later capture reuses the stored cohort when none is supplied and verifies its
pinned source before reprocessing. `--since` is an inclusive **reported filing-date
filter inside the selected historical sample**. It is not a source modification
cursor, a quarterly delta, a legal effective-date query or a claim about newer
cases outside that sample. Approximate/unknown source dates remain explicit.
Native inventory refreshes compare current full-publication metadata; a future
larger adapter needs its own supported update-window contract.

Products retain case/opinion identifiers, court type and original labels, record
and JSON-Pointer field locations, text hashes, citation and subsection text,
Unicode codepoint spans and context offsets. Spans address decoded source text,
not raw JSON byte offsets or JavaScript UTF-16 indexes. F/FD filtering uses actual
source codes; unsupported/conflicting metadata remains unresolved. Counters can
overlap when a selected record has unresolved optional metadata.

All findings remain candidate/unreviewed with unknown version alignment. Literal
cohort matching is deliberately bounded; unsupported reference syntax remains
unresolved. No-match output cannot establish absence from the global corpus.
Existing reviewed sidecars and publication are not changed by bulk commands.

Offline rebuild compares exact canonical product bytes. Inventory replay also
rebuilds metadata from retained raw XML pages. Recipe changes require the recorded
producer or a new explicitly captured generation; timestamps are not replaced by
execution time during replay. Archives reject mixed inventory/case recipes that
the retained producer cannot reproduce.

## Export, archive and recover

Commit producer code before first offhost archival, then capture the generation
that records that commit. The archive tag retains the exact producer, including
both case and inventory parser contracts.

```bash
python3 bulk.py --action export --cache "$PRECEDENT_CACHE" --output /persistent/path/snapshot.tar.gz
python3 bulk.py --action import --cache /persistent/path/restored --input /persistent/path/snapshot.tar.gz
python3 bulk.py --action rebuild --cache /persistent/path/restored
python3 bulk.py --action archive --cache "$PRECEDENT_CACHE" --repository civic-source/cap-citation-pipeline
python3 bulk.py --action restore --cache /persistent/path/restored \
  --repository civic-source/cap-citation-pipeline --tag precedent-data-SNAPSHOT16-BUNDLE16
```

Export destinations are exclusive: choose a new path rather than overwrite an
existing archive. Bundles contain the selected case manifest, optional native
inventory context, complete referenced source bytes and canonical products.
Import rejects links, traversal, duplicate/extra members, oversized data, bad
hashes and recipe mismatches before selecting the case pointer. It does not
execute code from an archive.

GitHub uses content-addressed tags/assets, never replaces assets and independently
reads back exact bytes before publishing. Drafts are discovered by release ID if
tag lookup does not expose them. Data releases do not become the latest software
release. The receipt reports GitHub's server immutability setting separately from
the tool's write-once policy; repository policy is not changed.

If a newer checkout cannot reproduce an older recipe, fetch the trusted archive
producer tag into a separate checkout:

```bash
git fetch origin tag precedent-data-SNAPSHOT16-BUNDLE16
git worktree add --detach /persistent/path/precedent-producer precedent-data-SNAPSHOT16-BUNDLE16
cd /persistent/path/precedent-producer
python3 bulk.py --action restore --cache /persistent/path/restored \
  --repository civic-source/cap-citation-pipeline --tag precedent-data-SNAPSHOT16-BUNDLE16
python3 bulk.py --action rebuild --cache /persistent/path/restored
```

A restore/replay drill verifies retained inputs and extraction; it does not confer
editorial acceptance, deployment verification or a legal conclusion.
