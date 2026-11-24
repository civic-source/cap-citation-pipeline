# ADR 0002: One historical joined shard and multipart recovery

Status: accepted for the scoped experiment. Nexus job
`job-consensus_vote-bce2df56712df786`, vote
`vote-1795548461232-guj0bg0`, approved the pinned reader in CI option with three
approving roles. The independent contrarian checker errored; two operational
model families, one fallback and unmeasured serving independence were disclosed.
This approves implementation and bounded qualification, not completed data
acceptance or a full corpus transfer.

## Context

The historical JSONL sample qualifies a bounded recovery path, but contains only
1,000 cases. Current native CourtListener tables still have an unresolved
opinion-content publication relationship. A complete original COLD Parquet shard
contains joined case, court and opinion information without that separate join.
It remains historical data and cannot establish current or global coverage.

The selected publisher revision is
`5d8d0d8457ef63b6463af9737da21d3badd924ad` in
[harvard-lil/cold-cases](https://huggingface.co/datasets/harvard-lil/cold-cases/tree/5d8d0d8457ef63b6463af9737da21d3badd924ad).
The selected file is
`part-00030-bd7b5d85-e1bd-440b-8817-95ec234884f3-c000.gz.parquet`,
959,035,282 bytes, with publisher LFS SHA-256
`f69470bb88733f2ecfafe0c7a3f4b14eb5bc828155c339310d04c9765bdb5070`.
Its footer describes 250,162 rows in eight row groups, 3,080,793,796 total
uncompressed column bytes and a largest row group of 421,187,627 bytes.
The maximum individual decoded cell is unknown. Footer statistics do not justify
skipping groups for the federal or filing-date cohort.

## Decision

Qualify exactly this complete file. Retain it as one verified content-addressed
object and record its acquisition before transforming it. Failed processing
reuses verified bytes without another transfer. Existing small-sample source and
archive limits remain unchanged.

Use a separately installed, pinned `pyarrow==23.0.1` reader for joined commands
and real synthetic Parquet tests in CI. Legacy commands do not require the
reader. Decode in one owned subprocess with at most 4 GiB address space and
explicit CPU, wall-time, file and output limits. Small batches alone cannot bound
native decompression allocations. Resource exhaustion fails before selection.

Retain deterministic candidate evidence partitions, exact decoded text hashes,
Unicode spans and original row-group, row and opinion locators. Normalize native
nullable dates and identifiers explicitly; unknown data becomes unresolved
observations rather than invented identities or negative evidence. Preserve
literal-only extraction and federal/date selection semantics. All matches remain
candidate, unreviewed and unknown in statutory version alignment.

Store output in at most 1 GiB of deterministic JSONL partitions of at most 32 MiB.
Use a disk-backed duplicate/contradiction index limited to 512 MiB. Archive raw
object bytes in ordered parts of at most 128 MiB and a canonical index binding
complete manifests, reference closure, extents, lengths and digests. Limit the
closure to 4 GiB, 4,096 objects and 64 parts; every asset is below 2 GiB. Verify
all remote bytes before publishing. Restore verifies full objects and replays
before selecting a snapshot; it never executes code from archived data. Ordinary
failures with verified rollback preserve prior selections. Failed rollback
durability or foreign pointer replacement reports `selection outcome unknown`
and requires reconciliation rather than claiming preservation.

Require at least 10 GiB of additional persistent headroom within the 16 GiB cache
quota while preserving a 4 GiB free-space reserve for this workflow. Account for
old and new generations, indexing, output, archive readback and isolated restore.
Stage under the persistent cache, not `/tmp`; never prune automatically. Measure
actual peak memory and disk before accepting the live source.

## Consequences

The reader, source acquisition and archive format have separate contracts from
the pilot. The pilot recipe files remain byte-identical. Offline replay records
and checks exact producer, reader, schema and configuration identities.

A filing-date filter selects reported historical dates; it is not an incremental
publisher feed. Cached inputs support deterministic rebuild and changed-cohort
processing. Current native joins, authenticated incremental updates and a full
historical corpus require separately tracked qualification. No sidecars or web
publication change through this workflow.
