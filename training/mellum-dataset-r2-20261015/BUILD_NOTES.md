# R2 build notes

## Archived (2026-10-26)

Mellum is no longer used by Wisp, so this dataset is archived. Its source-contract
check (`tests/test_mellum_r2_source_contracts.py`) was removed from Simulation QA
and CI, and router or tool changes no longer rebind `validation.json` or
`upload-zip.json`. The hashes there describe the sources as of the last rebind,
not current main. Before training on this data again, restore the check from git
history and run `build.py validate-rendered` and `package` against the then-current tree.

## Current-main source acceptance (2026-10-22)

The 2026-10-15/16 corpus below is preserved byte for byte: 3,270 train rows and
392 dev rows, 60 families and 1,959 items. Its menus remain the historical router
snapshot. Current-main reconciliation checks source compatibility; it does not
regenerate examples or claim that today's router emits identical prompts.

From a clean checkout, run the repository Python environment with PyYAML:

```bash
python training/mellum-dataset-r2-20261015/build.py validate-rendered
python -m pytest -q tests/test_mellum_r2_source_contracts.py
python training/mellum-dataset-r2-20261015/build.py package
```

`validate-rendered` streams the committed files, requires their frozen SHA256s,
checks row/family/category/source counts, world and item split boundaries,
completion and thinking frames, offered/forbidden tool calls and argument keys,
R2 first-step menus, Messages/card-balance channel choices, and current held-out
gate overlap. Current registered tool names and target argument keysets are read
from Python AST without importing the service. These key checks do not establish
argument values/types or runtime tool availability. All current router/tool source, held-out fixtures, corpus,
validator and trainer-kit inputs are hash-bound in `validation.json`.

The existing privacy regex finds no matches; this scan alone cannot prove that
every possible kind of personal data is absent. Synthetic provenance and
semantic quality still need independent review. The highest current held-out
4-gram overlap is 0.428571 (threshold 0.5). Token counts remain historical
metadata; no tokenizer, model, GPU training, provider, VM setup, or live tools ran
for this reconciliation. Runtime router parity and model quality remain unrun.

`package` reruns committed acceptance and refuses stale/failing evidence. Its
ZIP has six payloads plus `MANIFEST.json`; `upload-zip.json` binds the archive
outside the ZIP. VM unpack now requires that trusted outer digest, validates
the exact inventory, relative names, regular-file types, size bounds and every
member digest before staging, and replaces existing data only after success.
Tokenizer preflight requires the pinned local template and uses local files
only. Source-contract tests use synthetic temporary archives and a mocked
tokenizer; they are not a real tokenizer/GPU preflight.

Preflight captures data/trainer/template hashes before validation, requires the
same hashes after successful validation, then atomically publishes the captured
binding. Failure removes the marker; later smoke/train checks reject changed
inputs. The unattended loop checks that binding immediately before every smoke
and training attempt, including retries. Mutation-during-preflight and
between-stage controls use a copied runner and stub trainer.
This assures stable before/after inputs on a trusted local filesystem, not an
immutable snapshot or protection against hostile change-and-restore behavior.

Generation commands (`pool`, `gold`, `distill`, `render`, `stats`, `validate`)
need the original ignored generation artifacts and/or model resources. They are
not clean-checkout acceptance commands. Missing inputs fail before overwriting
committed data or evidence. A newly generated corpus needs its own reviewed
snapshot and frozen hashes; it cannot silently replace this R2 delivery.

## Historical generation evidence

Built 2026-10-15/16 on `main` at 40ffe997 (router PRs #239 + #241), with every step run in order:
`pool` → `gold --engine` → `distill --k 2` → `render` → `validate --template-checks 100000` → `stats` → `package`.

- **Engine:** oMLX with base **Mellum2.1-12B-A2.5B-Thinking-oQ5e** loaded, Wisp quit. Production tool retrieval
  is `lexical` (packaged default, no user override), so `gold --engine` and the offline `gold` (stub embedder)
  gave identical yields.
- **Distill:** Mellum passed 591/676 items at production sampling (try 0 greedy tool steps, retry at 0.6),
  within the reasoning caps (1500 chars/step, 2500/turn). Of the 85 unpassed, 72 fell back to gold and 4 fallbacks
  were dropped by the router. The remaining 9 belong to families with no gold plan (cal_free, rem_list, contact_mom).
- **Router #241 check:** the distill ran on the #239 router. Routing all 2110 pool items under #239 and #241 found
  11 menus that differ, all gold `todo.txt` items, and only in tool order (`find_files` / `list_dir` swapped). No
  distill item changed. Gold re-run on #241: no family's yield changed. `family_route_audit.py` agrees with
  ROUTER_FINDINGS.md.
- **Host incident:** the first distill pilot hit an oMLX that hung right after loading Mellum, while `syspolicyd`
  was down and fresh binaries parked in `_dyld_start`. It recovered without a reboot; that pilot's log was
  discarded.
- **Package:** `mellum-r2-data.zip` (gitignored; sha256 in `upload-zip.json`).

## Training
Use `training/mellum-r2-train/` (the R1 kit with R2 names; `package` packs its `train.py`). Its tokenizer-only
`preflight` passes on this data on the Mac: template sha matches, 0 rows over `--max-len 12288`, 0 decode
mismatches. Upload `mellum-r2-data.zip` and compare its sha with `upload-zip.json`.
