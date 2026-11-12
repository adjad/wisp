# r5.1 launch check (2026-11-12): verified, not trained

Nothing here trains. No training run, no smoke test, no optimizer step on the model. Training waits for the user's go-ahead.

Inputs:
- the finished split `data/r51-final` (7,452 train rows), packed offline into `ask-lora-data.zip`, sha256 `910bcdf4…250d`;
- the pinned r5 step-1800 adapter from `START.md`.

## 1. Launch path and the weights-only start

The launch runs through four layers, from shell to trainer:

| Layer | Path |
|---|---|
| Entry | `run_r5.sh train` (`ask-lora-r5/mi355x`) |
| | `run_sft.sh train` (`ask-lora-r4/a6000`) |
| | `~/wisp/run.sh gemma _train` (written by `ask-lora-r3/brev/vm-setup.sh`) |
| Trainer | `train.py train` (`ask-lora-r3/brev/train.py`, sha256 `96a4ef53…`, the same file r5 trained with) |

**Gap: there is no weights-only start.** `train.py` only reads an adapter through `--resume`. That path also restores
the old optimizer, the learning-rate schedule and the step counter (`opt.load_state_dict`, `sched.load_state_dict`,
`state["step"]`). So `--resume step-01800` would continue r5 at step 1800 of 2126, inside r5's schedule, which is
what `START.md` rules out. Without any adapter flag, `load_model` builds a fresh LoRA (B = 0), so the run would start
from base.

**Smallest change (proposed, not applied):** two patches in this folder.

`train-init-adapter.patch` adds `--init-adapter DIR --init-adapter-sha256 SHA` to `train.py`. After `load_model` builds
the fresh LoRA, and before the optimizer exists, it:
- checks the file's sha256;
- checks `adapter_config.json` against this run's LoRA: r 16, alpha 16, dropout 0.05, the same target regex, no
  DoRA or rsLoRA, no rank or alpha patterns;
- loads the weights with `set_peft_model_state_dict`;
- fails on any unexpected key, on any tensor-set mismatch, and on any tensor that isn't bit-identical after loading.

The optimizer, schedule and step counter are then built fresh, at step 0. With `--resume`, it only accepts a resume dir
inside `--out` (a pause of this run), and it skips the init there.

`run_sft-init-adapter.patch` makes two changes to `run_sft.sh`:
- `LR` from the environment now wins; it was forced to `2e-4`.
- `EPOCHS`, `INIT_ADAPTER` and `INIT_ADAPTER_SHA256` now reach `train.py`; `--epochs 2` was hardcoded.

Launch, once approved:

```
INIT_ADAPTER=~/r51/step-01800 INIT_ADAPTER_SHA256=b8750d1b… PAUSE_AT=50 bash run_r5.sh train
```

`PAUSE_AT` has to be given: `run_r5.sh`'s default is 400.

**Checks run** (on the A6000 `following-indigo-goose`: torch 2.11.0+cu128, transformers 5.22.1, peft 0.21.2, base
`google/gemma-4-E4B-it@ee0ef602`):
- **Hashes match `START.md`:** `adapter_model.safetensors` `b8750d1b…`, `adapter_config.json` `0d43b541…`.
  `trainer_state.pt` wasn't uploaded and isn't used.
- **Config:** r 16, alpha 16, dropout 0.05, target regex identical to `train.py`'s `TARGET_RE`.
- **Every weight loads:** 516 LoRA tensors, 34,881,536 params. The model's set equals the adapter's and every tensor
  is bit-identical after loading. 516 is 42 layers × 7 modules × 2 (A, B) = 588, minus k_proj and v_proj on E4B's 18
  KV-shared layers.
- **Step-zero parity** (`parity_step0.py`, result `parity_step0.json`):
  - Setup: 6 fixed rows (4 from step 1's batch, 2 new safety rows), eval mode, no grad, sdpa.
  - The launch path (`load_model` plus `--init-adapter`) gives logits **identical** (max |Δlogit| = 0, top-1 agreement
    100%, same CE) to `PeftModel.from_pretrained(base, step-01800)`.
  - Base (LoRA off) differs on every row (max |Δlogit| 19–48), so a no-op load can't pass.
  - The fresh LoRA before loading equals base exactly.
  - No optimizer was built.
- **Not run on the GPU:** the patched trainer's own `train` loop (that would mean training). The fresh-optimizer claim
  rests on the code order: the init happens before `AdamW(...)`, `state = {"step": 0}`, and the resume branch isn't taken.

## 2. Learning-rate schedule (as the launcher resolves it today)

- **Optimizer:** AdamW, weight decay 0, fused, gradient clip 0.3, global batch 8 (1 row × 8 accumulation).
- **Learning rate:** peak 2e-4. `run_sft.sh` exports `LR=2e-4` unconditionally, so an `LR=` from outside is overwritten.
  `--epochs 2` is hardcoded too.
- **Schedule:** `get_cosine_schedule_with_warmup`, warmup `round(0.03 × total)`. Total steps = 2 × ceil(rows per
  epoch / 8):
  - text 7,452;
  - vision drawn 1,091 (pool 536);
  - audio drawn 545 (pool 296);
  - = 9,088 rows per epoch, 1,136 steps per epoch, **2,272 steps, warmup 68**.

Learning rate actually used by each update. The scheduler steps after `opt.step()`, so update n uses lambda(n − 1),
and the value logged at step n is the next update's. Checked against r5's own log: logged 3.13e-6 at step 1 = 2e-4/64.

| Update | r5.1 as configured (2e-4, 2 ep) | 5e-5, 1 epoch (warmup 34) | 1e-4, 1 epoch |
|---|---|---|---|
| 1 | 0 | 0 | 0 |
| 10 | 2.65e-5 | 1.32e-5 | 2.65e-5 |
| 25 | 7.06e-5 | 3.53e-5 | 7.06e-5 |
| 50 | 1.44e-4 | 5.00e-5 | 1.00e-4 |

(The second and third columns are illustrations, not a recommendation.) Update 1 has a learning rate of 0: it updates
Adam's moments but moves no weight, so the first 50 updates change the weights 49 times.

**The pause doesn't change the schedule.** `PAUSE_AT=50` runs a full dev eval and saves `step-00050` with the adapter,
optimizer, scheduler and state (plus the `ckpt/step-00050` snapshot the gate uses), then exits. `run_r5.sh resume`
passes `--resume step-00050`, which restores all of it, so updates 51 onward follow the same cosine as an uninterrupted
run. Warmup ends at update 69, after the pause. The data order depends only on seed and epoch, so the batches are
unchanged too.

**Flag:** step 1800 was last trained at 1.21e-5 (r5's cosine near its end). The default schedule re-warms the same
adapter to 2e-4, 16.5× higher, for 2 more epochs. About 93% of r5.1's text rows are r5 rows the adapter has already seen
about 1.7 times. The step-50 gate would catch damage done in the first 50 updates. The peak and the length are the
user's call.

## 3. The first 50 steps (`first_batches.py`, list in `first50.json`)

`first_batches.py` reproduces the trainer's order offline, using `train.py`'s own `load_all` (tokenize, `--max-len 11264`
filter), `pool_sizes`, `epoch_order_pools` (seed 20261029, epoch 0) and batches of 8. It gives the same result with the
Mac's tokenizer and with the box's pinned stack (identical JSON). No row is over the length cap.

**400 rows in steps 1–50:**

| Source | Rows |
|---|---:|
| r5 replay | 298 |
| new safety rows | 27 |
| vision | 49 |
| audio | 26 |

The new family is 7.35% of the text rows, 6.0% of all rows. Its 27 rows come from 22 distinct contrast groups:

| Intent | Rows (groups) |
|---|---|
| refused | 5 (5) |
| clarify | 4 (4) |
| reply | 4 (4) |
| read_only | 4 (3) |
| authorized_write | 4 (4) |
| draft | 2 (1) |
| embedded_instruction | 2 (2) |
| direct_send | 2 (2) |
| leave_alone | 0 |

These 27 rows are 13 finals and 14 calls.

r5 rows by audit intent:

| Intent | Rows |
|---|---:|
| authorized_write | 21 |
| direct_send | 11 |
| draft | 7 |
| reply | 3 |
| clarify | 3 |
| schedule_send | 2 |
| steps inside write turns | 67 |
| turns with no write | 184 |

r5's restraint families in these steps:

| Family | Rows |
|---|---:|
| safe_restraint | 14 |
| instructions_r4 | 11 |
| restraint_r4 | 4 |
| safe_effects | 3 |
| ignore_embedded_instructions | 2 |

Which steps cover what ("new" counts only the safety rows):

| Behaviour | Rows (all / new) | Steps with any | Steps with a new row |
|---|---|---|---|
| Legitimate sends and replies | 22 / 6 | 19 steps (2, 3, 5, 7, 9, 12, 14, 17, 20, 25, 27, 33, 34, 36, 37, 44, 45, 47, 49) | 7, 20, 25, 27, 36, 47 |
| Drafts | 9 / 2 | 1, 3, 22, 25, 36, 37, 39, 47, 50 | 3, 50 |
| Read-only or leave-alone | 4 / 4 | 8, 18, 50 (read-only only) | 8, 18, 50 |
| Clarification | 7 / 4 | 2, 12, 22, 31, 40, 48 | 2, 12, 22, 40 |
| Refused actions | 5 / 5 | 10, 33, 35, 42, 43 | same |
| Embedded instructions | 2 / 2 | 4, 5 | same |

## Gaps and proposals

1. **No weights-only entry point.** Without the patch, the only adapter path is `--resume`, which restores the old
   optimizer, schedule and step. The patches above are needed before any launch.
2. **The launcher overrides LR and epochs.** `LR=2e-4` and `--epochs 2` are hardcoded in `run_sft.sh` (patched above).
   The peak learning rate and the length are an open decision. See the flag in section 2.
3. **Thin new-family exposure by step 50.** 27 safety rows out of 400, at a mean learning rate of 7.2e-5, with update 1
   at 0. **No leave-alone row lands in the first 50 steps**: there are 29 in train, about 1.3 expected. Drafts get only
   2 new rows from 1 group. So step 50 is mostly a "no harm yet" check, not a test of whether the new data works.
   Proposed, smallest first:
   - (a) Repeat the 548 new-family train rows 2× in the packed `train.jsonl`. The new family becomes about 11% of all
     rows: about 45 new rows by step 50 instead of 27, and about 2.4 leave-alone rows expected. These are expected
     values; the real order has to be re-enumerated on the repacked file. The split doesn't change and no new data is written.
   - (b) Also add the 29 leave-alone and 52 draft train rows once more.
   - (c) Move the gate's pause to step 100.

   The validator and overlap checks don't change with (a) or (b), because no row changes.
4. **The step-0 multimodal reference.** With `--init-adapter`, the step-0 dev eval (`state["ref"]`) measures step 1800,
   not base, so `mm_ok` holds checkpoints to within 5% of step 1800's vision and audio dev loss. r5 was held to base's
   (1.7925 and 1.1459). That's fine if step 1800 is the reference you want. Otherwise, pass the base reference
   explicitly.
5. **Smoke comes first.** `run_sft.sh train` refuses to start without a passing `OUT/smoke.json`. Smoke takes optimizer
   steps on a throwaway fresh LoRA (memory, attention parity, a 15-step overfit) and saves no adapter. It wasn't run,
   as instructed. It's the first command after a go-ahead.
6. **The dataset may still change.** If base's bf16 answers replace the oQ4e ones (see the backend comparison), the
   pack and `first50.json` must be rebuilt. The row order holds only for the exact `train.jsonl`.
