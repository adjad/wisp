# Wisp synthetic Ling corpus (2026-10-06)

This is a deterministic, specification-authored synthetic dataset for Wisp read-intent routing and grounded source overviews. No private user logs, original evaluations, old heldout prompts, model-generated inference, or external source data were used. All names, invalid example email domains, dates, source items, conversations and facts are synthetic. The author used programmatic expansion of newly authored linguistic templates and structured facts. This is not independently human-authored conversation data and does not demonstrate measured model improvements.

## Contents and intended use

| Split | Routing | Grounded overviews | Total |
| --- | ---: | ---: | ---: |
| train | 1,400 | 600 | 2,000 |
| dev | 140 | 60 | 200 |
| final | 210 | 90 | 300 |

There are zero unrelated general-assistant examples. Core natural/informal day/week/month agenda routing has 340 training rows after review-driven rebalancing. Routing uses the exact frozen schema and system contract from `training/ling-brev-20261006`, pinned at base `c1ef1be4c95f4806688484f8785e36c4b06138c0`. Grounded overviews have their own explicit fixture-only system instruction. The two tasks have different outputs: JSON routing labels and concise prose overviews. Preserve each example's system message; do not treat overview targets as router JSON.

`train.jsonl` and `dev.jsonl` contain only `id` and chat messages. The final assistant message has `training: true`; every earlier assistant message has `training: false`. User/system messages do not have a training field. Apply loss only to the last assistant response in the training stack; verify the selected trainer honors the flags, or translate them into the trainer's assistant-token masking mechanism. No trainer compatibility or production training configuration is claimed here. Use `train.jsonl` for training, `dev.jsonl` for development; keep `final.jsonl` sealed until a predeclared evaluation after recipe selection. No token totals have been measured.

Provenance JSONL files are separate CPU QA oracles and fixtures, not model inputs or training targets. They include category, family, split, synthetic slots, canonical routing semantics or overview rubrics. The source fixtures required for an overview are already present in that example's user message. Do not append oracle sidecars to a training conversation.

## Families, variation and leakage limits

Before any slot expansion, linguistic surfaces are assigned to one split. There are 40 routing behavioral categories, with 173 train, 44 dev and 83 final surface families. There are 20 overview categories, with 40 train, 20 dev and 20 final linguistic/composition families. Broad behavioral categories are intentionally reused: the model should generalize the same contract across different wording. Overview composition profiles use two primary items in train and three in dev; final has distinct composition profiles kept in the sealed author assets. Multi-source profiles have separate split-specific mixtures. These profiles are simple deterministic challenge variations, not a claim that final measures independent real-world distribution or difficulty.

Routing families cover informal and misspelled day/week/month agendas; named times, exact dates, months and inclusive ranges; literal queries including leading `+`, punctuation and Unicode; account/unread/limits; unfiltered and daily source reads, explicit unread=false, rolling calendar windows, named-person email searches and individual message conversations; exclusions and complete multi-source requests; same-source filter retention and source-switch reset; inline drafting versus sending/creating/deleting/saving; unsupported source access, location/status/negative-entity filters and reminder-time or message-unread constraints. Ordinary conversational requests have kind=none routing labels rather than generic assistant prose targets. Pure prohibitions do not grant access. Unsupported effect-containing requests never silently become reads.

Overview families cover calendar, reminders, notes, email, messages, combined source coverage, explicitly empty scoped results, denied access, timeouts, missing returns, partial folders, truncation counts, speaker attribution, unconfirmed proposals, overlapping calendar commitments, explicit time zones, grouped multi-day weekly agendas, supplied draft-versus-sent record state, draft notes and absence of stated deadlines. Reference summaries preserve supplied facts and caveats and never claim an outbound action.

Natural/informal daily and weekly/monthly agendas have additional split-owned surfaces and more weight in training. Availability uses current/future periods; inbox and message catch-up uses present/past periods. Expansion varies date/time windows, domains, literal search terms, limits, account, reminder scope, speaker facts, composition and result status. Templates are reused within a split, and synthetic slot vocabulary is finite. Low-information constant prohibitions/typos are capped at one example per surface. Family and category counts are recorded honestly in `manifest.json`; `families.json` records all surfaces in the author checkout. This is a small templated corpus, not 2,500 independently authored situations. Lexical normalized screens remove superficial cross-split duplicates after entity/date/number substitution and compare every category within each task at a 0.88 SequenceMatcher threshold. They are not a semantic-embedding audit and cannot prove zero conceptual leakage. Shared schema/system instructions and broad category concepts are expected.

General week/month agenda wording applies the calendar time window and retains the reminder source, while recording the literal period in `unsupported_constraints` because reminder reads cannot implement that period. It does not invent reminder dates or broaden the requested period silently. An explicit arbitrary reminder-time request also records that literal unsupported constraint. Named day agendas can map reminders to an explicitly requested today/tomorrow scope. Effect-plus-read compound requests are conservatively unsupported with no source permission. These choices follow the frozen capability contract; ambiguous production requests still need runtime policy and testing.

## Reproduction and checks

In the author checkout, run the generator with Python 3, then the validator in the existing CPU environment containing `jsonschema`:

```
python3 generate.py
python3 validate.py
python3 validate.py --public
```

`generate.py` uses only the Python standard library and the two frozen contract files. Seed: `610120261006`; generator version: `1.1.0`. It emits canonical sorted JSONL, stable shuffled order and a ZIP with fixed timestamps and member modes. Rerunning should reproduce payload and package hashes exactly. The validator independently reconstructs routing intent from category/slot semantics, checks schema plus capability-specific fields, valid dates and exclusive time forms, source/exclusion completeness, literal evidence and history reset/retention. Negative controls corrupt sources, dates, filters, exclusion behavior, context, authorization, grounding, speaker associations and output claims and must be rejected.

Overview reference QA checks supplied fixture facts, source and speaker associations, dates, deadlines, numeric grounding, unavailable/partial/truncated honesty and forbidden effect claims. This checks the generated references with literal fixture facts and associations; downstream evaluation should accept faithful paraphrases and score factual entailment, coverage/status honesty, attribution and readable concision. Use the sidecar rubric rather than an exact-text-only accuracy measure. These checks do not replace independent human product review or downstream model evaluation.

`validate.py --public` reads only train/dev payloads and sidecars plus metadata, contract and ZIP. It never reads final payload, final sidecar or family definitions. The full validator is for author-only generation/semantic validation of final data. The parent may inspect final counts and hashes only. Neither author nor parent has run inference or tuned a model against final examples; bounded semantic and quality repairs were driven by train/dev review.

## Portable upload and exclusions

Upload `ling-train-dev-20261006.zip`; it contains train/dev chat JSONL, train/dev QA sidecars, the schema, router system contract, this data card and public upload metadata. Only the chat JSONL files are training/development inputs. The ZIP omits final payload/sidecar, generator, validator, family/template definitions and all other generation assets, caches, secrets and original heldout data. Public final metadata records counts and hashes only. `upload-zip.json` records the exact allowlisted members, bytes and SHA-256. The package is data preparation only and does not execute training, cloud operations, model imports or outbound actions.

Risks: synthetic style and repeated split-local templates can overfit; reference prose uses regular rendering conventions; coverage is bounded to the pinned schema rather than all possible Wisp behavior. Some facts use a fictional `credits` currency. Domain/context ambiguity and unsupported constraints should be measured with fresh independent evals. Model quality, tokenizer length, trainer compatibility, final-test difficulty, safety after weight updates, training duration, cost and export/integration behavior remain unmeasured. The four-update smoke and main-corpus training are separate work; this corpus itself makes no claim about their result.
