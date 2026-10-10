# Arguments shard: fresh synthetic routing

This shard owns only this directory. It was authored against the new V2 specification and frozen router system/schema at clean base `20e256d91bcb0bc34f5eb3a6c506bb9edc90f6fe`, branch `codex/ling-dataset-v2`. No historical datasets, generators, evaluation prompts/results, private data, comparison harnesses, models, native sources, network services, or GPU operations were used. No Git actions were performed by this author.

## Contents and counts

| Split | Time | Filters | Simple | Total | Authored scene families | Used authored surfaces |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| train | 900 | 650 | 200 | 1750 | 47 | 141 |
| dev | 90 | 65 | 20 | 175 | 26 | 78 |

All rows are routing examples. Each has the exact frozen system text, one user message, and one final JSON assistant target with `training:true`. There is no prior assistant message. Train/dev scene families are declared separately before expansion: 30/9 time families, 13/13 filter families, 4/4 simple families. Each family has three independently authored wording surfaces, all used. Scenes differ in purpose and source/composition; slot and wording variants stay within their family's split. Broad competencies intentionally overlap across splits. The 1925 rows are synthetic expansions of 73 authored scene families and 219 authored surfaces, **not** 1925 independent human examples. `scenario_id` names a within-family slot-pair bucket, not an additional authored family. Neutral workstation context is an irrelevant within-family slot variation, not a source constraint.

The deterministic indexing seed identifier is `610220261007`; no random sampling is performed. Query, account, date, conversation, duration and count slots are synthetic. Addresses use `.invalid`. Literal query slots cover Unicode, punctuation, leading-plus addresses, and quoted action-like strings. Account restrictions are never inferred from queried addresses. Targets omit all absent optional fields. Email unread true/false/absence (false explicitly includes both read and unread mail), account/count/duration presence, exact time representations, reminder scope, and unsupported restrictions remain separate dimensions. Reminder arbitrary periods and message unread/read status remain literal unsupported constraints. Source switching/follow-ups and outbound-effect boundaries belong to the sibling context shard.

Every completed `pair_id` has exactly two rows and changes one semantic dimension: time, unsupported constraint, query punctuation, account, count, unread, scope, duration, or conversation punctuation. Dev expansion counts leave some unpaired final rows per family; these are intentionally recorded as singleton pair buckets. Public simple examples have no contrast-pair ID. All current prompts are unique within each split, with zero exact user-prompt overlap across train/dev.

## Reproduction and verification

From the V2 root:

```sh
python3 -B shards/arguments/generate.py
python3 -B shards/arguments/generate.py --output-dir /private/tmp/ling-v2-arguments-repro
```

The generator locates the frozen contract relative to its source file and writes four deterministic JSONL files to the chosen directory. It uses only the standard library. Every generated row has matching provenance with semantic specification, literal evidence spans, family/surface/slot identifiers, and contrastive metadata. The final target is independently reconstructable from `semantic_spec`.

Author checks: exact split/group/family/surface counts; JSONL parse and required row/message shape; frozen system equality; generic frozen-schema validation plus source-specific allowed fields; canonical reconstruction from semantic provenance; literal user evidence and target field-value equality; unique IDs and user prompts; disjoint split families; single-dimension complete pairs; and byte-identical reproduction of all four output files. Preparation remains CPU-only and inert. Target length is checked in characters only: **tokenizer target-limit preflight and actual trainer loss-masking/compatibility checks remain pending** under the parent workflow. No quality-gain or trainer-compatibility claim is made.

During development, inert checks exposed a substring match between `account` and `count`, duplicate expansions, and multiple changing fields in some pairs; those were repaired before final verification. No external capability or execution was attempted. Label limits: these examples exercise the frozen interpretation contract and cannot establish native source execution behavior or training benefit. The set is templated and compact by design.
