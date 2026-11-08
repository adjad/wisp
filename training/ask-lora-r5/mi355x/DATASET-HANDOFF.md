# r5 dataset handoff, 2026-11-07

**The dataset isn't complete, and nothing trained.** Base hasn't written its answers yet. On 2026-11-07 the user chose one RTX A6000 for that generation, and only that, and started the instance themselves. Everything that needs no GPU is done and checked. oMLX on port 8000 wasn't used.

Audited commit: `786122746d1c1abd9719559344a41b7d03e915e0`. Rows rebuilt on a clean commit, the one after this file's last change (see "Provenance"). Output folder (git-ignored): `training/ask-lora-r5/data/r5-prep/`. Hashes: `SHA256SUMS` in that folder. `prep_r5.sh` rebuilds all of it except the served-prompt check.

## What exists now

| | Rows | Note |
| --- | --- | --- |
| Rows in the sealed build | 8,494 | 45 families; 813 rows removed for overlap |
| Rows in `prep-INCOMPLETE` | 8,492 | train 7,736, valid 756; status `incomplete_no_base_answers`, which `pack_r4.py` refuses |
| Finals | 4,498 of 4,500 kept | 2 dropped |
| Finals base has written | 1,419 | reused from r4, each passing the new checks |
| Finals still to generate | 3,081 | 701 have no text at all; 2,380 hold scripted text base should replace |
| Tool steps (calls and loads) | 3,994 | call targets stay scripted; base's thought is added where its call matches |
| Tool steps with no thought after a tool result | 146 | need base's thought, or the row and its turn leave the build |
| Finals with no thought after a tool result | 1,011 | waiting for base: 825 hold scripted text, 186 hold the placeholder |
| All rows ending in an empty thought after a tool result | 1,157 | 1,011 + 146; equals `rendered_shapes.forced_empty`, and `finish_r5.py` stops if they ever differ |

Token shares below describe this state, so they aren't the final distribution. A row holding "Base writes this answer." counts as about 6 tokens, which is why `chat_contrast_r5` and `memory_contrast_r5` show very high per-token weight. Recount with `report_r5.py` once base's answers exist.

## The exact compute dependency

Generation needs a GPU; the Mac's oMLX can't be used without contending with Wisp. **Decided 2026-11-07:** the user chose an RTX A6000 (about $0.60/h) over the MI355X, for generation only, in their words "Yes, use the A6000". The quote they saw was roughly 15-25 h with batching, up to about 4 days without, roughly $10-60 for answers plus tool-step thoughts, so `TRACES=1` is inside that OK. That quote came from r4's picker speed on the A6000, not a measurement of `gen_finals.py`: if the smoke's measured speed projects past about $60 or 4 days, the full run doesn't start. Follow `README.md`'s A6000 section, using `r5-kit.tgz` from the output folder:

1. Base's answers: 3,081 finals. The smoke step measures the speed first.
2. Base's thoughts at tool steps (`TRACES=1`): 3,994 steps.
3. Back on the Mac: `finish_r5.py`, then `validate.py` must exit 0, then `report_r5.py`.

Training stays a separate decision and isn't part of this.

## Decisions: three made, one recorded

**1. The overlap rule: decided 2026-11-07, keep it strict.** The user chose "Keep it strict", relayed by the RC3 coordinator. `--keep-own-wording` isn't used, so all 813 removals stand and every test question still counts as unseen. What that costs, for the record: 572 of 762 `debug_log_r5` rows leave. The user earlier called that family r5's biggest goal, and the private test sets come from the same logs. 190 rows stay, and some question types lose all or nearly all their rows. Of the 572:

| Overlaps with | Rows |
| --- | --- |
| Private test sets only | 325 |
| Private and public | 189 |
| Public only | 35 |
| Audit evidence, private and public | 23 |

`seal_r5.py --keep-own-wording` would have restored the 325 private-only rows, and those test questions would then no longer count as unseen. The user declined it.

**2. `TRACES=1`: decided 2026-11-07, yes**, inside the A6000 quote above. See "Thoughts at tool steps" for why.

**3. Chat rows base answers with a web search: decided 2026-11-07, let them drop.** The user chose "Let them drop", relayed by the RC3 coordinator. Scope: `chat_direct_r4` and `knowledge_direct` finals (the row wants text) where base called `web_search`. When no sample passes and any sample called `web_search`, `choose_r5.py` puts the row in `drop.ids` and `web_search_drop_ids`, never back to its scripted text, and the call is still a rejection, not a pass. `finish_r5.py` drops it as `base_answered_with_web_search`, and the finished `stats.json` reports `web_search_drop` with each family's drops and the rows it has left. Rows where base called another tool (`wikipedia_summary`, `public_web`, `convert_units`, `calculate`) aren't in the decision's scope: they keep the old rule, staying scripted, and are counted per family in `scripted_kept_after_tool`. On the first 1,648 answers: `chat_direct_r4` 58 of 88 passed, 27 drop for web search, 3 stay scripted; `knowledge_direct` 57 of 86 passed, 14 drop, 15 stay scripted. The top-up round can still rescue a row, since only a row with no passing sample drops.

**4. Runtime mode, recorded.** Rows are pinned to the installed oMLX text path with earlier in-turn reasoning dropped: both Wisp (`service/agent/loop.py`) and the engine (`omlx/adapter/gemma4.py`) drop it, so histories carry no earlier thoughts. All 8,492 prompts match oMLX byte for byte. The running thoughts test doesn't change this today. If the user later changes the runtime to send thoughts back, the histories must be regenerated before generating answers.

## Provenance

Each `stats.json` now names the code its own step ran on (`ask-lora-r2/builder/code_provenance.py`), at the top as `code_head`, `code_dirty`, and, when training/ had uncommitted code, `code_dirty_sha256` and `code_dirty_files`. Earlier steps stay under `provenance.build`, `provenance.seal` and `provenance.finish`, and no step copies another's head. The previous build said `78612274` while it ran on the changes later committed as `cf86db0d`, and its `service_head` was blank, because the builder asked git about the pinned copy's folder, which git doesn't track.

The rebuild ran build, seal and finish on one clean commit, named in each step's `provenance` in `prep-INCOMPLETE/stats.json`, with `code_dirty` false. Its rows are byte-identical to the earlier build's: `all.jsonl`, the splits, `partial.jsonl`, provenance, drops, the overlap manifest, chosen finals and the token reports all hash the same. So the served-prompt check, the contract verification and the 40-record review still describe these rows. Any tracked change under training/, even a Markdown file, makes a step dirty: that rule is strict on purpose.

**Service code.** The rows' prompts and tools come from the pinned copy `data/pin-7e442f2d`. Its 460 files match `7e442f2de513115f99e23e2fb3b5054a1cfc96d8` blob for blob across `service/` and `scripts/`, so `service_head` is that commit; the pin's tree hash is `pin_tree_sha256`. A pin that differed would get no `service_head` and list each differing file. On the node, which has no git, `r5-kit.tgz` carries `CODE_PROVENANCE.json` naming the code it was packed from. `finish_r5.py` runs on the Mac and records its own head.

## Which rows the overlap rule affects

Every removal, by id and matched set, is in `sealed/overlap_manifest.json`. It holds ids only, never private wording. A second seal over the sealed rows removes none.

| Family | Turns | Rows |
| --- | --- | --- |
| `debug_log_r5` | 285 | 572 |
| `session_recall_r4` | 105 | 128 |
| `instructions_r4` | 48 | 68 |
| `day_chat_r4` | 9 | 18 |
| `followup_r3` | 5 | 8 |
| `tool_choice_r5` | 3 | 6 |
| `day_answers_r4` | 2 | 4 |
| `chat_voice_r4`, `lookup_answers_r4`, `no_write_for_reads`, `safe_restraint` | 5 | 8 |
| `device_r3` | 1 | 1 |
| **Total** | **463** | **813** |

**Reconciling the audit's numbers.** Its 719 train + 31 valid rows are `leak_ok`, which is set on every `debug_log_r5` row. Its 99 are dry-pack train rows that hit the public index. It didn't check private sets or history turns, and those account for most of the removals. The `session_recall_r4` and `instructions_r4` removals come from history turns; the ones checked were real matches (103 of 153 hits score 1.0), not false positives. The pre-existing leak check looks at the current question only, which is why these rows passed it. Known overlaps kept under the strict rule: none.

## What changed for each audit finding

1. **Source contracts persist.** `meta.gold` carries expected sources, per-source facts, empty and failed sources, conflicts and event dates through generation, selection, splicing and validation. `gpu/source_contract.py` is the one implementation, shared by chooser and validator.
2. **Bad answers are rejected and correct ones pass.** On real rows (`contract_verification.json`), 627 correct answers passed and 0 were rejected; 1,306 damaged answers were caught and 0 missed. The audit's probe answer ("Everything looks good. You are all set.") on `all_sources_r5-000-s1` is now rejected by both chooser and validator, where it passed both before (`report/audit_probes_rerun.json`). This run found and fixed two real bugs: a plural weekday in a result didn't support the day, and "Mt. Tabor" split its sentence and hid a wrong day. **Base's own wording on the source rows is unmeasured**, so `README.md` runs a smoke step on those families first. On the 1,435 real r4 answers, the checks flagged 16 (1.1%).
3. **Reasoning.** See below.
4. **Chat doesn't become memory.** New `memory_contrast_r5` holds each topic as ordinary chat, a remark while something related is saved, a how-to, a fact already in the chat, and real saved-memory questions with and without a hit. All wording is new.
5. **Sources and restraint.** `all_sources_r5` gained failed-read and conflicting-amount rows. New `write_contrast_r5` holds each topic as a plain write, a draft, "how should I word it", a read, a clarifying question and a passing remark. There's no row for a declined card, because the host writes "I didn't send it" itself. Rows follow the host's card rules and don't teach around them.
6. **Statistics and provenance.** `r5_stats.py` recounts from the rows on disk. Each row has an origin in `provenance.jsonl`, each drop a reason in `drop_manifest.jsonl`, each rejected sample its reasons in `finals/rejections.jsonl`, and each generation run its settings and hashes. A build that never sampled a final can't call itself complete.

## Thoughts at tool steps

Measured from r4's stored test run (466 trials per model):

| Step | Base: with a thought, median chars | r4 step 1150 |
| --- | --- | --- |
| First step, call | 362 of 362, 799.5 | 155 of 397, 0 |
| First step, text | 80 of 104, 1051.5 | 38 of 69, 72 |
| Later step, call | 84 of 84, 1129.5 | 119 of 123, 99 |
| Later step, text | 373 of 378, 667.5 | 384 of 389, 116 |

Base always thinks before a first tool call. Trace-free first-step rows teach the opposite, and that's the state of 2,289 rows in this build. So I recommend base's own thoughts at tool steps.

The target policy has no filler and no quota:

- **Finals:** base's own thought is required.
- **Tool steps:** base's thought where its call matches the script (`choose_traces.py`); otherwise the scripted thought if there is one; otherwise a first step stays trace-free, because Gemma's template renders nothing to mask.
- **After a tool result with no thought:** the row leaves, with its whole turn, since supervising `\n<channel|>` teaches closing the thought at once.

**Weighting.** `train.py` averages within a row, then equally across rows. A token in a 20-token call row counts far more than one in a long answer: calls are 46% of train rows and 22% of tokens right now. Per-family numbers are in `report/family_tokens.csv`, and overall numbers in `report/token_stats.json`. `--anchor-text-frac` defaults to 0, so the KL term doesn't anchor text rows. Whether to change either is a training decision and I changed neither.

**Crusoe's guide.** It used gemma-4-31B, not E4B, so it isn't an established recipe here. On rendered E4B targets its "nothing to mask" holds for first steps. It doesn't hold after a tool result, where the empty span is supervised. Its echo test is applied: base's thoughts share a median 0.39 of trigrams with their answers, and 1.9% are at 0.8 or above.

## Where I disagree with the audit

- It cites `ai.google.dev` thinking guidance, which the user said is deprecated. `gemma.ai/tuning` is the guide.
- It understated the provenance gap: `chat_contrast_r5`'s memory-talk check and `session_chat_r4`'s recall references also never reached the chooser, and the stall rule rejected an honest "I couldn't find it" on empty-source rows. All fixed.
- Its claims on row weighting, the supervised empty thought, unused KL, the unserialized references and the 45% checkpoint filter all held up in code. I didn't re-verify the gate's PASS path beyond reading it.

## Separate from this dataset, for their owners

Data can't fix these, and they block a trustworthy gate:

- `gate/score_r5.py`: an incomplete run can still print PASS (`n/a` bars don't fail).
- `a6000/pick_text.py:1095`: the picker's 384-token budget truncates long reasoners.
- `a6000/pick_text.py:596`: checkpoints before 45% of training are excluded.
- Reliable save-and-stop, and thought retention, need their own checks.
- The toolbench leak (earlier questions returning as HISTORICAL MEMORY) is fixed in adjad/wisp#499, not merged yet. r5's rows don't carry it: a read-only scan of every r5 file found 0 such rows, since the builder gives each scenario a fresh WISP_HOME. But the gate runs this branch's own `runner.py`, so merge main in after #499 lands (`tests/test_toolbench.py` may conflict at its end), and don't compare r2-r4 gate numbers with post-fix runs.
- Thought retention was tested for real on 2026-11-07: keeping thoughts between tool calls had no detectable effect on base (582 turns, 50.2% vs 48.5%, p=.62), so the runtime mode above stands.

## Validation run

| Check | Result |
| --- | --- |
| Builder tests (`test_scen_r5_*`, `test_validate_r5`) | 129 passed |
| Contract and generation tests | 62 passed |
| Finish, seal and stats tests | 17 passed |
| `test_pick_text.py` | 51 passed |
| Prompts byte-identical to oMLX | 8,492 of 8,492 |
| `validate.py` on `prep-INCOMPLETE` | exit 1, two problems, explained below |
| Second seal | none removed |

`validate.py` exits 1 on two `think:adjacent_duplicate` entries: `instructions_r4` scripted finals whose identical thoughts became neighbours after the seal. Base's answers replace both. If either stays scripted, the final validation stops the build, which is what it should do. Placeholder rows are warned about, not failed, here; `finish_r5.py` refuses them in a real build.

The heavy lock belongs to the thoughts test and I never touched it. These were single CPU processes with no model, as earlier r5 builds were, and only the served-prompt check ran outside the sandbox, because importing oMLX's code needs a Metal device.

## Known limits

- About 95 first-step rows read email alone for a question that names no source, such as a booking code (mostly `mail_r3` and `lookup_answers_r4`). Mail is the likely home for most of them, and they serve as single-source contrast, but they do pull against "use all my sources". I left them, and the source gain is measured on the held-out set.
- Out-of-memory on the A6000 (fixed for the top-up and traces passes): halving split a batch of 16 long prompts down to one, but the halves ran inside the `except` block, whose traceback kept the failed batch's KV cache, so a single 7.6k-token prompt still failed with 46.7 GiB held. 32 answers in the first pass (22 `gather_then_send_r4`, 8 `restraint_r4`, 2 `session_chat_r4`) were written as errors. Errors never count as done, so the top-up round regenerates them with the fix (halves now run after the failed batch is released). Any that still fail stay as no-pass rows and are counted.
- Scripted finals and scripted tool-step thoughts are templated, and some read as instructions rather than reasoning. They're placeholders for base's text. Any that remain after generation are counted in `stats.json`, and a high count should stop the build for review.

## 40-record review

`report/review40.md`, with the records in `report/sample40.md`. Only 5 of the 40 are base-written finals, since base hasn't written the rest, so **this review checks prompts, tool steps and scripted fallbacks, not r5's real answers, and must be repeated on the finished build.** No wrong date, weekday, amount, time or list remains in the sample. The reviews found real problems in scripted targets (invented draft purposes, a resend with no read, a repeated send counted as a channel switch), and those are fixed, with tests where the logic changed. I rejected four findings that the rows don't support, and the file says why.
