# Retained precedent-source qualification

## Problem

Per-section CourtListener API queries cannot cheaply reproduce a historical
result after source changes or local data loss. Current native bulk data is
large, independently exported and has an unresolved text-table publication gap.
A small historical joined sample can qualify evidence and recovery before a
full-corpus commitment.

## Requirements

- Preserve an exact observed native object inventory and its raw metadata.
  Separate object dates, publisher modification times and local observations.
- Do not equate native schema/exporter source with published tables. Explicitly
  report absent opinion-contents artifacts and unavailable joins/text coverage.
- Plan transfer and coexisting source/staging/derived/archive/restore disk budgets
  against actual filesystems, with configurable quota and free-space reserve.
- Capture only the explicitly selected pinned historical sample within agreed
  byte limits. Verify the publisher Git blob and actual SHA-256; retain raw bytes.
- Parse actual source fields, retaining complete selected-sample and target-cohort
  counts separately from global corpus coverage. Federal filtering requires
  explicit source court types; unknown courts remain unresolved/out of scope.
- Produce candidate-only USC cohort evidence with exact source/opinion identity,
  text representation, character spans, raw citation and subsection text.
- Reproduce canonical products offline from retained inputs and configuration.
  Missing/tampered inputs fail without silently fetching latest replacements.
- Treat full snapshots as snapshots. A UTC since filter selects locally retained
  case dates/refresh work; it does not imply unavailable historical deltas.
- Export and restore complete verified bundles; retain exact producer code and
  independently verify GitHub binary readback before acknowledging archival.
- Preserve reviewed sidecars and existing publication. Bulk evidence remains a
  separate product; no automatic editorial acceptance or publication.

## Acceptance criteria

Tests cover malformed/oversized responses, identity mismatch, incomplete source
coverage, exact text offsets, grouped/subsection/hyphenated references, quota and
reserve refusal, corrupt restore, immutable targets and failed acceptance.
A bounded live sample produces a recorded inventory and deterministic candidate
product; a separate cache restored from GitHub reproduces it with networking
blocked. Required checks and independent real-diff review precede protected merge.

## Scope

Standard-library Python CLI/modules/tests and operator documentation. Existing
writer fixes remain a separate slice. Full native-table transfer, general legal
citation parsing, new mandatory dependencies, UI publication and Merlin models
are outside this pilot. Any larger source adapter requires separate sizing and
source-contract qualification.
