# ADR 0001: Qualified precedent inputs and bounded historical evidence

Status: accepted. Scoped inventory/producer-integrity and pinned historical sample choices were approved through separate recorded Nexus consensus votes. See docs/verification/2026-11-24-bulk-consensus.json.

## Context

Native CourtListener exports are quarterly complete, independently generated
PostgreSQL CSV snapshots. The observed 2026-09-30 opinions object is over 55 GB
compressed and necessary metadata joins add over 7 GB. The published legacy
opinion table omits a newer content relationship present in schema/exporter
source, while no opinion-contents object was observed. Unauthenticated metadata
API access requires credentials, and API quotas include concurrent minute/hour/
day limits. Independent compressed prefixes cannot establish court joins.

The official Harvard/Free Law Project COLD sample supplies historical joined
case/court/opinion records. Its exact file revision is from 2023-10-19 and is
separate from the 2024 Parquet publication. At 12,907,287 bytes it allows a
standard-library pilot without a new Parquet runtime dependency.

## Proposed decision

Keep native inventory qualification separate from historical sample capture.
Retain original metadata and selected source bytes in a content-addressed cache;
select accepted manifests only after all references and derived products verify.
Record source kind, explicit publisher revision, exact byte identity, acquisition
observations and producer/configuration identity. Do not manufacture OLRC release
points or reuse eCFR-specific title contracts for case data.

A historical sample product declares its source/cohort scope and incomplete
global coverage. Use actual court_type values and preserve original court labels;
do not use the legacy display classifier as a federal filter. Evidence locators
name the JSONL record, nested opinion, selected text representation and decoded
Unicode character span. Original bytes and source text remain authoritative.
Unsupported syntax remains unresolved, and all generated matches are unreviewed
candidates rather than holdings or claims about current law.

Use standard-library bounded streaming, hashing, JSONL, filesystem primitives and
complete deterministic archives. Archive raw inputs, canonical products and
manifest/configuration, retain exact producer code by a content-addressed GitHub
release tag, never clobber existing assets and verify actual binary readback.
Capacity planning includes simultaneous retained files and staging/readback;
archive/restore acceptance is distinct from publication and editorial review.

Incremental work compares retained snapshot identities and applies explicit reported
filing-date filters. It cannot reconstruct missing historical inputs or infer
unsearched negative results. Missing/corrupt replay inputs fail explicitly.

## Consequences

The small pilot qualifies the complete operator/evidence/recovery path without
claiming current native-corpus coverage. Native full backfill, larger joined
sources, authenticated incremental cursors and publication remain tracked work.
No GPU, new serving service or dependency/CI policy change is required here.
