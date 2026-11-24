# Annotation refresh producer verification — 2026-11-24

Scope: civic-source/cap-citation-pipeline#9 and civic-source/cap-citation-pipeline#11.

The existing `extract.write_annotations(results, output_dir) -> int` interface delegates to one output module. Fresh and retained-prior metadata emit `source: courtlistener` and a scope equal to `targetSection`. Candidate provenance, stale/retained flags, pagination counts and acquisition outcomes keep their existing meanings.

A prior verified case or an editorial state other than unreviewed protects the entire sidecar for every refresh outcome. The writer emits a refusal diagnostic and counts no write; original bytes, including comments and formatting, remain untouched. Unreadable or duplicate-key YAML, mismatched scopes, invalid prior fields, linked/nonregular targets and concurrent writers fail explicitly. Inputs and outputs are capped at 4 MiB per sidecar. Candidate replacements use validated directory descriptors and an fsynced atomic publication; simulated publication failure leaves the original file intact and removes staging files.

No source collection, dependency changes, workflow changes, publication or deployment occurred during this verification. Empty search-result accounting remains separately tracked in civic-source/cap-citation-pipeline#12.

## Exact commands and results

From `/home/william/git/civic-source-diaspora/.wt-cap-refresh`:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_annotation_writer_guards.py
```

Before production changes: **28 failed, 2 passed**. Failures demonstrated omitted source/scope, reviewed-sidecar rewrites, ignored unreadable YAML and linked-path writes.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_annotation_writer_guards.py -k semantically
```

Additional prior-integrity regressions before their fixes: **3 failed, 37 deselected**. These covered missing required case fields, invalid timestamps and nonnumeric case counts.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
git diff --check
```

Final local suite: **46 passed in 0.29s**. Whitespace check exited zero. Existing six tests still pass; all collection requests remain mocked. Filesystem tests use disposable temporary directories.

Environment: Python 3.12.3, PyYAML 6.0.3, pytest 9.1.1. No dependencies were installed.

## File-size exception

`extract.py` decreases from 508 to 425 lines by moving the writer behind a compatible wrapper. The remaining size is pre-existing search and CLI behavior; refactoring it would expand this correctness patch beyond its scope. The new writer and guard-test files are below 400 lines, and new functions stay below 50 lines of logic.

Independent review, downstream TypeScript contract verification, CI and merge receipts belong to the PR lifecycle; they are not claimed by this local verification record.

## Downstream contract and independent review

Fresh and retained-prior fixtures were generated through the actual writer in this worktree, then parsed from `/home/william/git/us-code-tracker` using:

```bash
node --import tsx --input-type=module - <<'JS'
import {readFile} from 'node:fs/promises';
import {parseAnnotationYaml} from './scripts/lib/annotation-yaml-parser.ts';
for (const name of ['fresh','retained']) {
 const raw=await readFile('/home/william/git/civic-source-diaspora/.workbench/cap-writer-contract/'+name+'.yaml','utf8');
 const result=parseAnnotationYaml(raw);
 if(!result.ok)throw result.error;
 if(result.value.refreshStatus?.source!=='courtlistener'||result.value.refreshStatus?.scope!==result.value.targetSection)throw new Error('Producer identity mismatch');
 console.log(name+': TypeScript annotation contract passed');
}
JS
```

Observed: both `fresh` and `retained` passed the TypeScript contract. No downstream source files changed. Node/pnpm build commands are not applicable to this Python repository.

Independent exact-model `google/gemini-3.1-pro-preview` real unified-diff review returned `APPROVE`, `finish_reason=stop`; source hashes and the response are in the adjacent review record. The scoped inventory/producer-integrity consensus also approved all seven role votes; model-family attribution and fallbacks are recorded honestly (not seven independent models). The maintainer delegated autonomous decisions and approved protected PR merges after checks/review. CI and merge receipts remain PR lifecycle evidence.

## CI entry-point regression

CI run45456340653 exposed a flat-repository import difference: `pytest` collected the new guard tests before the legacy file inserted the repository root. `python -m pytest` had the root on sys.path, so it did not reproduce that integration defect.

Exact reproducer before the fix:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q -p no:cacheprovider
```

Observed: collection failed with ModuleNotFoundError for extract. A shared tests/conftest.py now supplies the same flat-CLI root under both entry points. The unchanged reproducer and the original python -m pytest command both pass all46tests; no assertions, dependencies or CI gates were weakened. Independent real-diff follow-up review approved this bootstrap file.
