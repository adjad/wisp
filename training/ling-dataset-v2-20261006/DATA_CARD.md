# Wisp Ling synthetic dataset V2 — 6 October 2026

This dataset teaches Wisp's frozen read-intent JSON contract and concise summaries grounded only in supplied synthetic source results. It is data preparation, not evidence of improved model accuracy. It changes no Wisp production settings or tools and does not train, merge, export or deploy a model.

## Distribution

| Primary competency | Train | Development | Pilot subset |
|---|---:|---:|---:|
| Dates, periods and durations | 900 | 90 | 450 |
| Literal queries/accounts/unread/count filters | 650 | 65 | 325 |
| Simple reads and canonical output | 200 | 20 | 100 |
| Follow-up filter edits/removals/source resets | 800 | 80 | 400 |
| Multiple requested sources and exclusions | 450 | 45 | 225 |
| No-access/action/unsupported boundaries | 400 | 40 | 200 |
| Grounded source overviews | 600 | 60 | 300 |
| **Total** | **4,000** | **400** | **2,000** |

Routing is 85% of each split; grounded source overviews 15%. There are zero unrelated general-assistant/persona/reasoning examples. The pilot is an exact balanced subset of full training data, not another independent dataset; do not concatenate it with full training. Development data is never a training input.

## Sources and authorship

Three GPT-6.1 Sol/high authors wrote disjoint argument, context and grounded shards; a fourth Sol/high reviewer independently reviews frozen new artifacts. All names, .invalid addresses, dates, groups, calendar entries, message/email content and source-return status are synthetic. Each author wrote scenario/dialogue/composition specifications and linguistic surfaces, then used deterministic Python slot and contrastive expansion. This is not 4,000 independently collected human requests.

Authors receive the frozen public contract and aggregate development failure classes only. They do not read old training/dev/final rows, original evaluation prompts/per-case failure outputs, the new independent comparison corpus, private debug logs or user data. No model inference generates examples. No model/native/cloud/GPU tools execute in this preparation task. The unchanged old final test remains outside this dataset and upload; it has not been read. New data is not claimed semantically screened against an unseen final payload.

Scene/dialogue/composition families are assigned to a single split before paraphrases/slots. Contrastive pairs deliberately share a family inside a split, changing one requested distinction; related pairs never cross training/development. Broad competencies and domains are shared so the model can learn the same contract. Family/surface counts are reported by author READMEs and static validation metadata. Unique `expanded_template_ids` count generated identifiers, not independently authored templates; opening prefixes, dialogue contrast branches and full request surfaces are reported as separate units. Parameter expansions and synthetic style can still overfit. Fresh independent final and end-to-end Wisp evaluations are necessary after recipe selection.

## Contract and semantics

Frozen contract commit: c313a459f6ab259e55981ffcb1bbe3df11fe9307. Schema SHA256: 85186431c4d3e8f1c38ca7d2aa899714da1dd5b275de97998432a6de76705678. Router system SHA256: 800b235e8ab3ed7d76e74461cd20dbd0d58cb111e3d8f31b5adba27117b1b830. New repository base: 20e256d91bcb0bc34f5eb3a6c506bb9edc90f6fe.

Routing targets preserve exact dates/ranges/literal strings and requested optional filters. Absent optional fields are omitted, never null/defaulted. Same-source edits retain unmodified filters; source corrections reset old filters. Explicit unread:false disables unread-only restriction and includes both read and unread mail; it does not mean already-read-only. Already-read-only status constraints require an honest unsupported-constraint interpretation. User prohibitions never become read permission; drafts in chat are distinguished from requested send/create/delete/save effects.

The current reminder capability supports query and limited scopes, not arbitrary date ranges. Week/month agendas retain both requested calendar and reminder sources, recording the unsupported reminder period rather than silently omitting reminders or broadening the period. Some accurate interpretations must therefore clarify at compilation; exact JSON matching is not equivalent to executable tool success. Schema-valid combinations can also hit runtime capability limits. This corpus does not add missing tool capability or authorize actions.

Grounded overviews distinguish successful empty results from denied/missing/timeout/partial/truncated returns, preserve source and speaker attribution, separate proposals from confirmations and drafts from sent records, and avoid invented urgency/deadlines/actions. All source facts are explicitly included in the user message. Rubrics are QA sidecars, not model input.

## Input and loss masking

Training rows have only `id`/`messages`. Preserve their per-row system instruction: routing targets JSON, grounded targets prose. The final assistant turn alone has `training: true`; earlier assistant turns have `training: false`. These are dataset annotations, not proof the trainer honors them. The user-run pinned tokenizer preflight verifies training/generation prefix equality and complete targets within 2,048 tokens; actual prepared Axolotl label masking must then be audited independently. Both tokenizer measurements and actual trainer masking remain unqualified until those checks pass in the Brev environment. No token totals or training-time/quality claim is made here.

## Verification and limits

Static CPU checks cover exact distribution, IDs, complete JSON schema, calendar validity/exclusive time forms, source-specific capability fields, source/exclusion overlap, semantic-spec reconstruction, literal user evidence, independent follow-up edit replay, supplied fixture/rubric association, required phrases/status/source attribution with natural singular/plural labels for mixed sources, a bounded check for distinctive facts assigned to the wrong source within singly labelled clauses, final-only supervision annotations, exact and normalized cross-split prompt checks, and balanced pilot membership. Corruption controls test wrong schema/source/date/count/masking/grounding/false-action targets. Deterministic reproduction compares all generated payload hashes in fresh temporary output state. An independent advisory label/reference review inspects the new dataset. Its initial review required source-label clarity in 98 summaries and correction of 379 validator false positives; the original findings are preserved. Source-label presence and clause-anchor checks are necessary checks, not a universal proof of attribution or entailment. The repaired data and checker require a fresh frozen review.

These checks do not prove universal natural-language entailment, all real-world phrasing, zero semantic family leakage, model behavior, production safety, or Mac export compatibility. Normalized lexical duplicate screening is a heuristic, not an embedding or human semantic audit. Training improvements require measured comparisons; existing development results of 5.0% base / 31.4% adapter belong to the previous pilot, not this V2 dataset.

## Delivery

The portable upload includes train/dev/pilot messages, train/dev provenance, frozen schema/system, this card, README, CPU validator, user-run tokenizer preflight, and manifests. Only the selected train JSONL is a training input. ZIP allowlist/hash/CRC checks exclude final data, old evaluations, private logs, model weights, caches and secrets. Checkpoint or model integration remains separate, with the user's no-merge/no-activation hold intact.
