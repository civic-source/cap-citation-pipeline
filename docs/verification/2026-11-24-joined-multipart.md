# Joined-shard code and recovery verification

Base: `7f30de17fe52b2eb36d68cf871128915447279f5`.

## Commands and observed results

```text
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q -p no:cacheprovider
280 passed in 16.06s

ruff check --select F,C901 joined*.py multipart*.py bulk_atomic.py bulk_store.py bulk_github.py tests/test_joined*.py tests/test_multipart*.py tests/test_bulk_store_atomic.py
All checks passed!

git diff --check
exit 0; no output
```

A Python AST check found no production file above 400 lines, no function above
50 nonblank logic lines, and no function with more than five parameters except
the required standard-library redirect handler override.

Real synthetic Parquet capture/export/import/rebuild tests run the actual bounded
reader in separate processes. They reproduce the selected snapshot and preserve
the unrelated pilot pointer. A hash-valid but semantically incorrect product
fails worker replay before selecting a restored snapshot. The normal test suite
installs the explicitly approved pinned reader rather than skipping these paths.

The multipart public tests cover deterministic small-part tiling, empty objects,
bad offsets/lengths/closure, missing/corrupt/extra/linked files, immutable output
paths, quota/free-reserve refusal, failed replay and selection. Additional
failing-first regressions reproduced and corrected:

- An exclusive staging collision deleting a pre-existing file.
- Excessive part counts being rejected only after source extent writes.
- Free space being checked only before export rather than during writes.
- Public index files inheriting an overly permissive process umask.
- A concurrently growing part exceeding the hashing read budget.
- Staged output corruption escaping local publication readback.

All cases now pass. Optional native inventory context is replayed and retained
by immutable manifest ID without changing the current inventory pointer.

The separate storage fault tests preserve prior pointer bytes or absence after
a recoverable post-rename sync failure. A foreign replacement or unverifiable
rollback reports explicit selection-outcome uncertainty. It never silently
claims preserved selection or verified durability.

## Scope and remaining acceptance

The authentic Nexus decision is recorded in
`2026-11-24-joined-consensus.json`; phase budgets are in
`2026-11-24-joined-budget.json`. Source identity/footer qualification and reader
verification are adjacent. Independent exact unified-diff review and required
PR CI are recorded on the PR with final file hashes.

Code-tested locally: yes. Full historical source acquisition, independent
positive-evidence acceptance, GitHub binary readback and isolated network-blocked
replay: not yet performed at this checkpoint. Their exact producer/archive/data
identities will be recorded separately before closing live acceptance.

The Python CLI has no static application build. No editorial promotion,
current-native-corpus acceptance, web publication or model deployment is implied.
