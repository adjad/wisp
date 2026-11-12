# r5.1 dataset (built 2026-11-11, not trained)

The training data for r5.1 is r5-final with the audit's drops removed, plus the new `safety_contrast_r51` family.
Nothing has been trained on it: no pilot, no smoke test. Training needs a separate instruction from the user, and the run
pauses at step 50 for the frozen gate (`../eval/CRITERIA.md`) and the user's review.

The rows themselves are in `../data/r51-final/` (ignored by git). This folder holds only counts, manifests, logs and hashes.
`sample_labels.jsonl` is not committed because it contains row text.

## Final counts

| | rows | train | valid |
|---|---:|---:|---:|
| r5-final | 7,673 | | |
| dropped by the audit | −67 rows, 32 turns: 25 turns for label problems, 7 for overlap with the step-50 eval | | |
| new `safety_contrast_r51` | +622 | 548 | 74 |
| **r5.1** | **8,228** | **7,452** | **776** |

Counts by family, intent, target kind and target tool, before and after, are in `COUNTS.md` and `stats.json`.

### The new family

There are 355 scenarios in 90 contrast groups. Each group sets acting and not acting on the same topic side by side.
- 671 rows were built.
- Neither seal removed any.
- 49 rows were dropped because no base sample passed.
- That leaves 622 rows.

| Intent | rows | of which finals |
|---|---:|---:|
| authorized_write | 140 | 54 |
| read_only | 96 | 47 |
| direct_send | 86 | 39 |
| clarify | 69 | 55 |
| reply | 64 | 20 |
| draft | 56 | 22 |
| refused | 43 | 17 |
| leave_alone | 36 | 36 |
| embedded_instruction | 32 | 16 |

The 622 rows target 314 tool calls, 306 finals and 2 loads.

| Call targets | count |
|---|---:|
| view_emails | 43 |
| send_message | 33 |
| lookup_contact | 29 |
| clear_reminders | 28 |
| get_upcoming | 25 |
| draft_email | 24 |
| send_email | 18 |
| view_messages | 15 |
| search_reminders | 14 |
| reply_to_email, update_event, cancel_event | 12 each |
| add_calendar_event | 10 |
| complete_reminder | 9 |
| add_reminder | 8 |
| get_weather, find_place | 7 each |
| search_notes | 6 |
| update_reminder | 2 |

- **No `forget` calls.** Bulk forgetting stays out of the new family. r5's existing forget rows are unchanged.
- **Split:** group-aware, made by `../build/merge_r51.py`. 79 groups are in train and 11 in valid, with no group, thread or folded prompt on both sides. That holds across every family, not only the new one.
- **Every send and draft has real content:** 75 calls, 0 content-free. Each `text`/`body` is at least 2 words and none is a bare "here is".

## Checks

| Check | Result |
|---|---|
| Validator, new family (`new-family.validate.log`) | all 622, train 549, valid 73 (finish's own split, replaced by the merge): problems none, exit 0 |
| Validator, merged (`validate.log`) | exit 0, no problems |
| Seal 1: r5's sets (public toolbench with history, chat eval, private debug-log set) (`overlap_manifest.r5-sets.json`) | 0 rows removed |
| Seal 2: the sealed step-50 eval (`overlap_manifest.r51-step50.json`) | 0 rows removed |
| Full-history overlap with the step-50 eval: trigram Jaccard ≥ 0.7 on every user turn, history included | new family: 622 rows, 0 hits; merged: 8,228 rows, 0 hits |
| Raw intent filter, before choosing (`new-family.raw_filter.log`) | 1,342 base samples: 44 cut and blanked, so choose rejects them. Cuts: draft claims sent 16, leave-alone claims a note 14, leave-alone takes the remark as a task 5, clarify asks nothing 5, refused claims an action 4 |
| Safety backstop on the chosen finals (`new-family.safety_check.log`) | 305 checked, 0 rejected |

Eval prompts, eval answers and failure transcripts were never used to build rows. The step-50 cases were written separately and were never shown to the builder.

## Second reader

A separate reader (a sonnet agent) went through 45 stratified rows of the earlier build.
- 40 OK, 0 unsafe, 0 ungrounded.
- 3 labels were flagged as wrong, but they are per-turn intent labels: a call step inside a turn that is later refused carries "refused". That is by design.
- 1 leave-alone final treated the user's remark as a task.
- 1 send had no real content.

Both real findings are fixed in this build:
- Scripted sends now carry content (9604bd46).
- The leave-alone rules plus the raw filter cut such samples before choosing, so the row falls back to another sample or is dropped.

A spot check of 8 random leave-alone finals in this build found offers of help, no claims, and no remark taken as a task.

## Hashes

- `SHA256SUMS.r51-final`: the files in `../data/r51-final/`. The combined sha256 that `merge_r51.py` prints is `193387e309e5b87aa7ff0c0a40dacc3861c3e8a7739b511aafe3d8c0cec5d6bb`.
- `SHA256SUMS.new-family`: `../data/pipe3/finished/`.
- `SHA256SUMS.base-samples`: base Gemma's raw samples (`../data/pipe3/raw/base.jsonl`, 1,342 lines).

The JSON and log copies in this folder have the home directory replaced with `~`, so their own hashes differ from the originals.

## How it was built

```
bash pipeline/pipeline_r51.sh safety_contrast_r51 data/pipe3                 # build, seal 1, seal 2, stop
D=data/pipe3 IDS=data/pipe3/todo.ids zsh pipeline/gen_chain.sh               # base samples, under the heavy lock
GEN=done bash pipeline/pipeline_r51.sh safety_contrast_r51 data/pipe3        # raw filter, choose, finish, validate
sh overlap/check_r51_eval_overlap.sh data/pipe3/finished ~/.moe/evals/r51-step50 OUT.json
python build/merge_r51.py --r5 ../ask-lora-r5/data/r5-final --decisions audit/decisions.jsonl \
    --labels audit/labels.jsonl --new data/pipe3/finished --out data/r51-final
```

Steps that read `~/.moe/evals` need the sandbox off.
- 617 of the 671 rows reused base samples from the earlier build (pipe2), matched by request hash.
- Only the 54 changed rows were sampled again (`todo.ids`).

## Known gaps

- Base finals come from oQ4e on the Mac, not from bf16 as in r5.
- `verify_contract.py` doesn't know the new family's gold, so seal-time contract verification is skipped with a WARN.
- The toolbench harness can't decline a card, so the step-50 gate can't test "proposed, then declined".
- Intent labels are per turn, so a call step inside a refused or clarify turn carries that turn's intent.
- Leave-alone has only 36 finals. Most of the restraint signal still comes from r5's `restraint_r4` and `safe_restraint` rows (309).

## v2 (2026-11-12): bf16 top-up, the set r5.1 trains on

The user asked for base's answers to be redone in bf16 "if needed". A test on the A6000 decided this.

**The test:** 160 final prompts and 60 call prompts, 2 samples each. The finals were all 49 rows that 4-bit had dropped
plus 111 random ones. bf16 ran through `gen_finals.py`, sdpa, batch 8, after the batched-vs-single self-check passed.
Both backends then went through the same raw filter, choose and trace steps (`pipeline/compare_backends.sh`; results in
`bf16-vs-oq4e.comparison.json`).

| | oQ4e | bf16 |
|---|---:|---:|
| 111 random final prompts passed | 110 | 110 |
| samples passing on those rows | 197 / 222 | 200 / 222 |
| the 49 earlier drops recovered | 0 | 24 |
| call prompts with a sample matching the scripted call | 13 / 60 | 15 / 60 |
| safety backstop rejections | 0 | 0 |

**Verdict: no full bf16 regeneration.** On unselected prompts the backends are equal: paired, 1 discordant each way.
bf16's +24 is on rows chosen *because* 4-bit failed both samples there, so it is mostly a selection effect. Fresh
samples from either backend would recover some of them. Before seeing the results I had set a rule: regenerate if
bf16 recovers at least 10 of the 49. That rule ignored this bias, and I didn't follow it.

**What changed instead:** the 98 bf16 samples for the 49 dropped rows were added as a top-up (k = 2, 3, marked
`topup`), the way r5's TOPUP round adds samples to rows with no pass. All the other rows keep their oQ4e answers. The
rows ran through the same pipeline into `data/pipe4`:
- the raw filter cut 14 of the 98;
- 24 rows were recovered: leave_alone 12, draft 5, clarify 3, reply 2, refused 2;
- 25 rows stay dropped;
- the new family now has 646 rows, and its leave-alone finals went from 36 to 47;
- the validator is clean and the safety backstop rejected 0.

Overlap with the step-50 eval: 0 hits in the new family (646 rows) and 0 in the merged set (8,252 rows). 8 recovered
finals were read: their calls and effects are grounded. One reply adds "tonight", which the user never said.

- **`data/r51-final-v2`:** 8,252 rows (7,473 train, 779 valid), combined sha256 `11067088…a28`
  (`SHA256SUMS.r51-final-v2`).
- **`data/r51-final-v2-up2`:** the training copy, with the new family's 569 train rows repeated once (user: "Yes,
  repeat 2x"), for 8,042 train rows. Valid is unchanged.
- **Packed:** `data/r51-pack-v2-up2/ask-lora-data.zip`, unpack sha256 `0c673799…6d` (`SHA256SUMS.pack-v2-up2`).
- **The first 50 steps of this pack:** `../launch/first50.v2-up2.json`. They hold 42 new rows from 35 groups (27 before).
