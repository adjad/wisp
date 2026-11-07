# r5 dataset handoff, 2026-11-07

**The dataset isn't complete, and nothing trained.** Base hasn't written its answers yet, and that needs a GPU no one has authorized here. Everything that needs no GPU is done and checked. No model was loaded, no node was started, and oMLX on port 8000 wasn't used.

Audited commit: `786122746d1c1abd9719559344a41b7d03e915e0`. Output folder (git-ignored): `training/ask-lora-r5/data/r5-prep/`. Hashes: `SHA256SUMS` in that folder. `prep_r5.sh` rebuilds all of it except the served-prompt check.

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
| Finals with no thought after a tool result | 825 | waiting for base |

Token shares below describe this state, so they aren't the final distribution. A row holding "Base writes this answer." counts as about 6 tokens, which is why `chat_contrast_r5` and `memory_contrast_r5` show very high per-token weight. Recount with `report_r5.py` once base's answers exist.

## The exact compute dependency

Generation needs a GPU, and none is authorized for this step: the MI355X node isn't started, and the Mac's oMLX can't be used without contending with Wisp. Once the user starts the node, follow `README.md`, using `r5-kit.tgz` from the output folder:

1. Base's answers: 3,081 finals. Speed on this node is still unmeasured, so the first command measures it.
2. Base's thoughts at tool steps (`TRACES=1`): 3,994 steps, about 1-2 h more (unmeasured), about $24-48. This needs the user's OK for the cost, and I recommend it (below).
3. Back on the Mac: `finish_r5.py`, then `validate.py` must exit 0, then `report_r5.py`.

Training stays a separate decision and isn't part of this.

## Decisions for the user

**1. The overlap rule removes most of the debug-log family.** Applied as instructed, with no exemption: 572 of 762 `debug_log_r5` rows leave. The user earlier called that family r5's biggest goal, and the private test sets come from the same logs. 190 rows stay, and some question types lose all or nearly all their rows. Of the 572:

| Overlaps with | Rows |
| --- | --- |
| Private test sets only | 325 |
| Private and public | 189 |
| Public only | 35 |
| Audit evidence, private and public | 23 |

`seal_r5.py --keep-own-wording` restores the 325 private-only rows, listed apart in `kept_known_overlaps`. Those test questions would then no longer count as unseen, so results on them must be reported separately. It's cheapest to decide before node time, since generation is paid per row. I built with the strict rule.

**2. `TRACES=1`.** See "Thoughts at tool steps" for why I recommend it.

**3. Runtime mode, recorded.** Rows are pinned to the installed oMLX text path with earlier in-turn reasoning dropped: both Wisp (`service/agent/loop.py`) and the engine (`omlx/adapter/gemma4.py`) drop it, so histories carry no earlier thoughts. All 8,492 prompts match oMLX byte for byte. The running thoughts test doesn't change this today. If the user later changes the runtime to send thoughts back, the histories must be regenerated before generating answers.

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
- Scripted finals and scripted tool-step thoughts are templated, and some read as instructions rather than reasoning. They're placeholders for base's text. Any that remain after generation are counted in `stats.json`, and a high count should stop the build for review.

## 40-record review

`report/review40.md`, with the records in `report/sample40.md`. Only 5 of the 40 are base-written finals, since base hasn't written the rest, so **this review checks prompts, tool steps and scripted fallbacks, not r5's real answers, and must be repeated on the finished build.** No wrong date, weekday, amount, time or list remains in the sample. The reviews found real problems in scripted targets (invented draft purposes, a resend with no read, a repeated send counted as a channel switch), and those are fixed, with tests where the logic changed. I rejected four findings that the rows don't support, and the file says why.
