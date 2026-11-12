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
