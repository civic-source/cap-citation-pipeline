# Historical joined-shard capture and recovery

Status: accepted for the scoped experiment by the concrete Nexus decision
recorded in ADR 0002. Code and data acceptance remain separate gates.
This extends the bounded source workflow without widening the legacy limits.
Use separate `joined-source` and `joined-snapshot` Store kinds and a `joined.py`
CLI so pilot pointers, commands and recipe bytes remain unchanged.

## Scope

Capture exactly the pinned source in ADR 0002. Scan its complete eight row groups
and 250,162 rows, selecting explicit federal F/FD court codes, a declared USC
section cohort and an optional inclusive reported filing-date filter. Preserve
unknown metadata and unsupported syntax explicitly. Report shard enumeration,
selected text coverage and global corpus coverage separately; global completeness
is always false. No editorial acceptance, automatic sidecar update or publication.

## Source and execution

Acquisition verifies the publisher's exact byte length and SHA-256 while streaming
into exclusive persistent staging, then adopts a regular immutable CAS object.
Redirects are bounded public HTTPS hops; credentials and unexpected content
encoding are forbidden. A distinct acquisition checkpoint survives a failed
projection and cannot replace the pilot's acquisition pointer.

Decode only verified local bytes with pinned PyArrow 23.0.1 in one subprocess.
Enforce at most 4 GiB address space, CPU/wall time, file-size and output budgets;
limit footer metadata, rows, opinions and decoded opinion text. There is no
whole-table Python conversion or implicit reader networking. Native nullable
`date32`, integer IDs and nested opinion lists are normalized deliberately.
A malformed source or exhausted limit fails without selecting a partial result.

## Evidence and determinism

Each match or unresolved observation retains its original case/opinion ID when
available, zero-based row group and row within group, absolute source row,
opinion index, source field path and raw metadata needed to assess selection.
Text evidence retains UTF-8 SHA-256, Unicode-codepoint citation and section spans,
raw citation text, subsection and bounded context with its source offsets.
Unknown IDs are not replaced with invented IDs. Duplicate identities preserve
counts; conflicting non-null identities fail closed and never silently replace
evidence. Missing identities remain explicitly unresolved observations.

JSONL partitions are canonical UTF-8 records with a newline and deterministic
order. Boundaries follow row-group order and encoded-byte limits rather than
reader batch scheduling. Each partition is at most 32 MiB; total output is at
most 1 GiB. Cross-group identity checking uses a staging SQLite index of at most
512 MiB. No unbounded output list or cross-corpus Python identity set is allowed.

Replay retains exact producer commit, recipe, Python/PyArrow versions, source
schema fingerprint, configuration, complete source identity and ordered output
references. Missing or corrupt inputs fail offline without fetching replacements.

## Resource and recovery acceptance

A joined operation uses a 16 GiB cache quota and 4 GiB free-space reserve, requiring
10 GiB additional persistent headroom before live capture. Capacity includes
retained generations, source staging, partitions, SQLite, archive/readback and an
isolated restore. Recheck actual capacity during processing; never stage in
`/tmp` or automatically remove retained data.

Multipart archives use at most 64 raw parts of at most 128 MiB, a canonical index
of at most 4 MiB, at most 4,096 objects and at most 4 GiB referenced closure.
The index binds exact manifests, object closure and contiguous ordered object
extents. Reject missing, duplicate, reordered, corrupt or conflicting bytes.
Existing assets are write-once; resume requires verified exact draft assets.
Read back every part and index before publication. Reconstruct regular staged
objects, verify full hashes and replay before selecting snapshot/inventory
pointer. Optional inventory context is retained by manifest ID without advancing
its independently selected pointer. Ordinary failures with verified rollback
preserve prior selections. Failed rollback durability or a foreign pointer
replacement reports `selection outcome unknown` and requires reconciliation;
no preservation claim is made for that uncertain outcome.

## Verification

Failing-first tests cover genuine Parquet native/null types, exact evidence,
row-group boundaries, deterministic partitions, malformed footer/cells, worker
resource failure, disk budgets, source retry, immutable targets and prior-pointer
preservation. Tiny multipart fixtures exercise multiple parts without large test
files. Preserve all existing pilot tests and deterministic archive replay.

Code acceptance requires full tests, Ruff F/C901, whitespace checks, an independent
real unified-diff review and green required CI. Data acceptance additionally
requires exact publisher-hash capture, full-shard counts, independent positive
locator verification, measured resource use, remote readback, isolated restore
and network-blocked replay. Historical shard acceptance does not close the
current native/full-corpus qualification work.
