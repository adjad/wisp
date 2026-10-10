# Context and exclusions shard

Fresh synthetic routing examples for the user-authorized Wisp Ling dataset V2. Sole author scope is this directory. Base is `20e256d91bcb0bc34f5eb3a6c506bb9edc90f6fe`, task branch is `codex/ling-dataset-v2`, and the frozen routing contract is `c313a459f6ab259e55981ffcb1bbe3df11fe9307`. This author performed no Git operations.

## Contents and generation

`generate.py` is standalone standard-library Python. It reads only the new parent `router-system.txt` and `intent.schema.v1.json`, locating them from its source path rather than the output path. Its deterministic shard seed is `610220261208` (global seed plus 202). No model, tokenizer, product code, native data, network, downloads, GPU, or cloud execution is used.

From the dataset root:

```sh
python3 shards/context/generate.py --output-dir /private/tmp/wisp-context-reproduction
```

Without `--output-dir`, it writes the four JSONL artifacts beside the script. Sorted dictionary serialization, explicit source order, UTF-8, and trailing newlines are stable. No earlier dataset, old generator/family asset, evaluation raw/failure prompt, comparison corpus, or private/user log was read. Only the permitted aggregate source-switch failure class informed the emphasis.

| Split | Follow-up | Multi-source | Boundary | Total | Scene families |
| --- | ---: | ---: | ---: | ---: | ---: |
| Train | 800 | 450 | 400 | 1650 | 54 |
| Dev | 80 | 45 | 40 | 165 | 25 |

The family partition is declared before expansion: train has 20 follow-up, 18 multi-source, and 16 boundary scenes; independently authored dev has 8, 9, and 8 respectively. There are 20 opening phrase surfaces in train and 5 in dev, plus five follow-up assistant-context phrasings. Opening prefixes and slot substitutions are variation within a family, not additional independent families. Every family has two contrast clauses. These are a bounded set of authored dialogue compositions, not 1815 independently written natural dialogues.

Train has 760 pairs with different semantic targets, 48 pairs that add literal-search or inline no-effect clarification while preserving the target, and 34 singletons. Dev has 72 different-target pairs, 2 clarification pairs, and 17 singletons. Pair members share the scene slots and opening surface. There are exactly 1650 unique train and 165 unique dev user dialogues.

## Coverage and provenance

Follow-ups include 480 train and 40 dev source switches that discard every old-source filter, followed only by filters explicitly authorized for the new source. Other follow-ups preserve unaffected fields while replacing or removing unread, account, count, conversation, query, scope, exact day, month, and inclusive range filters. `unread:false` means disabling the unread-only restriction and including both read and unread mail, per the parent's verified frozen email-tool semantics. Explicit filter removal omits the field. Message read/unread status remains unsupported.

Multi-source scenes bind filters to their named source, preserve explicit source exclusions, include day/week/month personal agendas, and distinguish calendar-only requests from explicit reminder prohibitions. Weekly/monthly agendas retain the calendar period, include a reminder source without an invented period, and record the literal unsupported reminder period.

Boundary scenes cover public questions and pure prohibitions, inline drafting, positive versus negated send/delete/save/create requests, mixed effects and reads with no source access, literal quoted action/rule words, and unsupported entity, location, and status constraints. There are 875 train and 90 dev rows with prior assistant context; every earlier assistant has `training:false`, and source-handler names confer no authority. Only the final assistant is marked `training:true`.

Each provenance row contains its scene/family/template identifiers, synthetic slots, canonical `semantic_spec`, literal user evidence, and a declared contrast axis. Follow-ups also contain `history_spec.initial` and replayable `history_spec.edits` using `switch`, `remove`, and `replace`, plus initial evidence and explicit reset/retention metadata. Removed-field evidence uses `removed:sources[0].<field>`; output evidence uses canonical `sources[N].<field>`, `excluded_sources[N]`, and `unsupported_constraints[N]` paths.

## Final verification

All final checks passed, with no unresolved failed checks:

- Generator assertions: exact group totals, allowed capability fields, valid time form, non-null filters, literal evidence, row shape, exact system text, final-only supervision markers, compact messages, and unique user dialogues.
- Parent `qa.py` `row_checks` run on all 1815 shard rows: schema, capabilities, actual dates/ranges, literal query/account/conversation constraints, literal unsupported constraints, evidence-to-target equality, and training markers passed.
- Independent replay of all 800 train and 80 dev histories exactly reproduced `semantic_spec`; initial-state evidence and evidence coverage for every final filter passed.
- Zero normalized cross-split prompt matches under the parent's normalization checker; no cross-split family sharing.
- `--output-dir` regeneration in a fresh temporary directory produced byte-identical train/dev and both provenance files.

Maximum final JSON length is 348 characters for train and 342 for dev. Maximum serialized message-list length is 3364/3403 characters, within the requested compact budget. These are character measurements only: the pinned tokenizer-length preflight and actual trainer masking/compatibility checks remain pending with the parent. Synthetic oracle/evidence agreement is not proof of universal natural-language entailment, quality improvement, or training compatibility. Independent immutable-snapshot review remains a parent integration step.

Final artifact SHA-256:

| Artifact | SHA-256 |
| --- | --- |
| `train.jsonl` | `dcef2825221bcef934a58e29699a943c9b47bfb14624bd0fd8312f405ba2a7a3` |
| `train.provenance.jsonl` | `473a33fa09c583958d22d4d6dcd5e10be557a142dec402e594c76423a5b2d948` |
| `dev.jsonl` | `f210fb53e5e9419a9c7f3a85473a9815adc75772e8bb10fed1f9adcc23cd0655` |
| `dev.provenance.jsonl` | `a451e3f91e5c2481f05e6dcd52c11f633291e9cb9aefca8bdc6097f051fe67d6` |

## Handoff

Changed only `generate.py`, `train.jsonl`, `dev.jsonl`, `train.provenance.jsonl`, `dev.provenance.jsonl`, and this `README.md` in the owned context shard. No dependency on another author's files; integration requires the parent's unchanged frozen system/schema and canonical history replay support. No commit was created by this author. Parent owns commit/push/combined validation and review. Author is quiescent at handoff and will perform no further writes unless explicitly released for a recorded repair.
