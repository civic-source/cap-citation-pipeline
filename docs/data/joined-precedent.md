# One historical joined COLD shard

This workflow processes exactly the original Parquet shard qualified in ADR 0002.
It is historical, joined source evidence. It does not establish current native
CourtListener coverage or global USC citation coverage. Generated findings remain
candidate and unreviewed; no annotation sidecars or publication are updated.

Install the optional reader into the same Python environment as the pipeline:

```bash
python3 -m pip install -r requirements-joined.txt
```

Legacy `bulk.py` commands and archived sample recipes remain independent. Joined
commands use separate `joined-source` and `joined-snapshot` pointers, so the pilot
can coexist in the same cache without changing either selected generation.

## Plan, capture and rebuild

Use persistent storage with at least 10 GiB additional capacity inside the cache
quota after preserving the configured free reserve. The default joined cache is
16 GiB with a 4 GiB reserve. The plan reports measured current capacity and a `ready` preflight result; a plan
alone does not authorize another source or demonstrate live acceptance.

```bash
python3 joined.py --action plan --cache /persistent/path/precedent
python3 joined.py --action capture --cache /persistent/path/precedent \
  --usc-title 18 --data-repo /path/to/us-code
python3 joined.py --action status --cache /persistent/path/precedent
python3 joined.py --action rebuild --cache /persistent/path/precedent
```

Alternatively declare `--sections 111,841` explicitly. After a successful capture,
omitting both cohort options reuses the verified selected joined cohort, including
its filing-date filter; an explicit `--since` replaces that filter. `--since YYYY-MM-DD` selects
inclusive reported filing dates; it is a local cohort filter, not an upstream
incremental feed. Approximate source dates remain flagged. Unknown dates remain
unresolved and cannot count as negative evidence. Rebuilding retained bytes never
fetches latest replacements. Changing a cohort reuses the pinned cached source
and creates a new derived generation when its result or recipe differs.

Capture verifies the complete publisher SHA-256 and length, retains an acquisition
checkpoint, then runs one bounded decoder. If processing fails, retry reuses the
verified source; corrupt checkpoints or objects fail explicitly. Capture selects
only complete verified partitions and counts. Its resource observation is separate
from canonical evidence. A complete capture is not an offhost recovery drill.

The decoder has at most 4 GiB address space, explicit CPU and wall limits, small
batches, a 4 MiB opinion-text limit and a 512 MiB transient SQLite index. Total
candidate JSONL output is limited to 1 GiB in partitions of at most 32 MiB.
Malformed or oversized input fails without selecting partial evidence. Native
reader allocation can fail before a Python field check; process limits enforce
that boundary. No full-table conversion, hidden remote reader fetch or GPU is used.

Every evidence locator uses zero-based row-group, row-within-group, absolute row
and nested opinion indexes. Citation and section offsets index decoded Unicode
codepoints, not UTF-8 bytes. Text hashes use original decoded UTF-8 text. A section
span includes its literal subsection when present. Coverage distinguishes all
scanned records/opinions, selected records/texts, exclusions and unresolved
observations; `global_corpus_complete` is always false. Unknown-metadata counters
count observed unresolved records and selected opinions, not a census of every
optional field outside the chosen court/date scope.

## Archive and restore

Commit the exact producer before offhost archival. Archive tags retain its code;
reader/runtime identity remains explicit and replay refuses a different recipe.

```bash
python3 joined.py --action export --cache /persistent/path/precedent \
  --output /persistent/path/new-bundle
python3 joined.py --action import --cache /persistent/path/restored \
  --input /persistent/path/new-bundle/joined-index-SHA256.json
python3 joined.py --action archive --cache /persistent/path/precedent \
  --repository civic-source/cap-citation-pipeline
python3 joined.py --action restore --cache /persistent/path/restored \
  --repository civic-source/cap-citation-pipeline --tag RETAINED-DATA-TAG
python3 joined.py --action rebuild --cache /persistent/path/restored
```

Choose a new export directory and use the actual index filename/tag returned by
the commands. Parts are raw byte transfers of at most 128 MiB; the canonical index
binds every part and complete object. No asset is replaced. Remote archival reads
back all assets before publication; restore checks each full object and replays
before selecting the joined snapshot. Optional inventory context is retained by
manifest ID without changing the separately selected native inventory pointer.
Ordinary failures with verified rollback preserve prior selections. If rollback
durability fails or another writer replaces the pointer, an explicit
`selection outcome unknown` error requires operator reconciliation; inspect and
verify the selected manifest before retrying.

If a newer checkout cannot reproduce the recorded recipe, use the trusted archive
producer tag in a separate checkout and the recorded Python/PyArrow runtime.
Archived data never supplies executable code to the importer. A fresh-cache
restore plus network-blocked replay is a separate required data acceptance gate.

Code tests, source/evidence acceptance, builds and deployment verification are
reported separately. This workflow makes no deployment or editorial claim.
