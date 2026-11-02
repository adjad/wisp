# Ask LoRA r4 SFT on one NVIDIA RTX A6000 (Brev)

r4's SFT (PLAN.md section F) on **one RTX A6000, 48 GB, sm_86**, instead of r3's 8x MI355X node. The recipe is r3's: LoRA r16 (alpha 16) on the language model only, vision and audio towers frozen, bf16, 2 epochs, effective batch 8, lr 2e-4, base-anchor KL on image and audio rows. Only the hardware path, the data and the checkpoint picker change.

**Prep only.** No GPU, Brev or token was touched. Verified on the Mac: `test_pick_text.py` (24 tests), `ask-lora-r3/brev/test_attn_parity.py`, `test_mm_render.py`, `bash -n` on the scripts, and a full `pack_r4.py` dry run on r3's data into a temp dir (r3's own `upload-zip.json` untouched). Not verified: everything that needs the GPU (list at the bottom).

| file | what |
|---|---|
| `pack_r4.py` | Mac. Builds `ask-lora-data.zip`, `mmeval-data.zip`, `pickers.zip` from `../data`, `../vision`, r3's audio. Stops before doing anything if an input is missing |
| `run_sft.sh` | Box. Setup with pinned versions, then every step to a picked, merged checkpoint |
| `pick_text.py` | Box + Mac. Text picker: generates on the valid text steps for each checkpoint, scores with `reward.py`, applies the PLAN.md rule together with the mmeval picker |
| `estimate.py` | Mac. Time, cost and memory arithmetic (`python estimate.py`, `--zip` for exact token counts) |
| `test_pick_text.py` | Mac tests on fake generations |

## Decisions

| | choice | why |
|---|---|---|
| micro-batch / accumulation | **1 row x 8** (`--global-batch 8`) | r3 = 8 GPUs x 1 row = 8 rows per optimizer step, so lr, schedule and noise match. A 6.5k-token row already fills the card: a bigger micro-batch would fit (about 28 GiB at 2) but is not faster, and `train.py` only has micro-batch 1 |
| max length | **10,240** (train.py default) | r3's longest train row was 9,938 tokens (its smoke log), r4 adds roughly 100 tokens of thought. Over-cap rows are skipped and reported at load; `smoke` runs at the same cap, and `run_sft.sh train` refuses to start without a passing `smoke.json` and reuses its cap |
| gradient checkpointing | **on** (train.py always enables it, `use_reentrant=False`) | Not a knob here. Without it every layer's activations are stored; a rough per-layer count says that does not fit at 10k tokens (not measured) |
| attention | **SDPA** (`--attn sdpa`), parity-checked in `smoke`, `math` as the fallback | see below |
| precision | bf16 weights, fp32 LoRA/optimizer state, no QLoRA | 48 GB is plenty, and QLoRA changes the recipe |
| evals | every **200** steps on **150** text dev rows (`--dev-text-max 150`, new flag) plus all vision/audio dev rows; adapter snapshot every 200 | a full 364-600 row text eval is about 20-35 min each on this card; the real selection is the generation picker, not dev loss. The logged `dev_loss_text` is on the 150-row subsample, so it is not comparable to r3's |

### Attention: SDPA, not flash-attn 2

- PyTorch's SDPA picks the kernel per layer. The `flash-attn` 2 package caps head dim at 256, and Gemma 4's full-attention layers are 512 wide (`train.py` docstring), so it cannot serve them. It would also need a compile on the box and is not installed. Not used.
- r2 trained on an A6000 with SDPA (21.6 GiB peak). The gfx950 AOTriton bug that forced `--attn math` on r3 is AMD-only, but this is not assumed: `smoke` runs the **parity test**. It compares the configured attention with the math reference on the 3 longest text rows with at least 64 labeled tokens (capped at 4,096 tokens so the L x L math scores fit), one image row, and the LoRA-gradient cosine. `run_sft.sh` passes `--attn-parity-strict`, so on CUDA a mismatch is fatal (smoke exit 8) rather than a warning.
- If it fails: `ATTN=math bash run_sft.sh smoke`, then train with the same `ATTN=math`. Math attention costs memory (about 31 GiB at 10k tokens, measured on r3) and is slower; nothing else changes.
- The logic of the gate is tested on the Mac by `ask-lora-r3/brev/test_attn_parity.py`. On the box, with torch present, its torch sections run too: copy it next to `~/wisp/gemma/ask-lora/train.py` and run it there.

### Memory arithmetic

Measured pieces: weights **14.8 GiB** (7.94B parameters in bf16; the `gpu_alloc` line of r3's smoke log); LoRA **34,881,536** trainable parameters (r3's log), so fp32 weights + gradients + two Adam moments are **0.52 GiB** at most; r2's A6000 run with SDPA peaked at **21.6 GiB**; r3's MI355X run with *math* attention peaked at 30.7 GiB at 9,938 tokens.

| | GiB |
|---|---|
| weights | 14.8 |
| LoRA weights, grads, Adam | 0.5 |
| activations at the longest row, SDPA: r2's 21.6 peak minus the two lines above (checkpointed layer inputs, one layer's recompute, completion-only logits, the chunked KL) | 6.3 |
| **peak at 10,240 tokens, SDPA** (activations scale with length) | **22.1** |
| spare on a 48 GB card | about 25 |
| if SDPA must be `math`: r3's 30.7 GiB at 9,938 tokens, scaled | 31.4 (about 16 spare) |
| micro-batch 2, for reference | about 28 |

`smoke` measures the real peak and prints it; its pass bar is 4 GiB of headroom against the card. `status` warns above 40 GiB. If a row ever does OOM, `train` saves `oom-recovery` and exits; resume with a lower cap (`bash run_sft.sh train ~/wisp/gemma/ask-lora-out/oom-recovery`, after `smoke-len 8192`).

## Time and cost (estimates; the anchor is r2's measured A6000 run)

Anchor: r2 on an A6000 trained at **624 tok/s** (85.6 s per 8-row step, 6.7k tokens per row). r3's rows average 6,549 tokens (measured by tokenizing its train file), 41 of them completion. r4 rows should be about 100 tokens longer. r3's plan priced its 3,254-row, 2-epoch run on one A6000 at 19-21 h; `estimate.py` gives 19.4 h for the same input.

SFT, 2 epochs, 150 text dev rows, 9 evals, at $0.60/h (what r2 paid; confirm the rate):

| text examples (train rows) | steps | tokens | train h | evals h | total h with setup | $ |
|---|---|---|---|---|---|---|
| 5,500 (4,950) | 1,510 | 67 M | 29.8 | 1.3 | **31.8** | 19 |
| 6,000 (5,400) | 1,648 | 73 M | 32.5 | 1.4 | **34.7** | 21 |
| 6,500 (5,850) | 1,784 | 79 M | 35.2 | 1.4 | **37.4** | 22 |

Speed range (700 / 624 / 520 tok/s): about 28.5 / 34.7 / 41.5 h at 6,000 examples. r3 on 8 MI355X took 47 min, so one A6000 is about 40x slower in wall time.

| stage | A6000 hours |
|---|---|
| setup: venv, torch, 16 GB base, uploads, preflight, smoke | 0.75 |
| SFT train + evals | 30-38 |
| text picker: 6 checkpoints x 360 valid steps (7.5 s per step: ~3.5 s prefill + ~100 tokens at 40 ms) | 4.5 (0.75 per checkpoint) |
| mmeval picker: base + 6 checkpoints, about 230 vision/reading/audio rows each | 1.8 |
| merge, package, download | 0.5 |
| **one pass to a picked, merged checkpoint** | **about 38-46 h, $23-28** |

Picker cost scales with checkpoints and steps: `MAX_CKPTS=4 DEV_STEPS=240` is about 3.3 h. If the valid text set is about 600 rows, 360 sampled steps is still what runs.

**GRPO, one epoch of about 1,500 prompts on the A6000 profile (G=6, 4 prompts per step):** `grpo/plan.py` says **11.1 / 14.3 / 20.0 h** (optimistic / middle / pessimistic). Be careful with that. plan.py assumes 25-45% MFU, but r2's SFT ran at about 16 TFLOPS, roughly 10% of the card's 155 TFLOPS bf16 peak (624 tok/s x ~26 GFLOP per token). Re-deriving the same epoch from the measured SFT speed (policy forward+backward, reference forward, generation prefill per completion, decode at 30-55 ms) gives **37-51 h (42 h middle)**. Treat 11-20 h as a best case, and put the first GRPO smoke's seconds per step into `plan.py` before committing a budget. Both sit inside the ~150 h on hand: SFT pass about 42 h + GRPO about 42 h is roughly 85 h.

Levers not implemented (all would change code that has never run on this card):
- Rows in r3's data share a long prompt prefix: after sorting, a row shares on average 18.9k of its 27.5k prompt characters (69%) with its nearest neighbour (system prompt, tool schemas, earlier turns). Computing the shared prefix once per group would cut tokens a lot, but the backward pass through the prefix needs a new training path.
- Not checkpointing some layers would save part of the recompute pass (about a sixth of the time) at the cost of memory we have, but needs a per-layer switch.
- `--epochs` is an integer; 1 epoch halves the training time. r2's dev loss plateaued at 80% of its run.

## Steps

### On the Mac

```bash
PY=~/Desktop/MOE_Project/.venv/bin/python
cd training/ask-lora-r4/a6000
$PY -I test_pick_text.py                    # 24 tests, CPU only
$PY -I pack_r4.py --model ~/Desktop/OMLX_Model_Files/Jundot/gemma-4-E4B-it-oQ4e-mtp --check-only
$PY -I pack_r4.py --model ~/Desktop/OMLX_Model_Files/Jundot/gemma-4-E4B-it-oQ4e-mtp
```

Needs: `../data/{train,valid}.jsonl`, their `.meta.jsonl` files and `stats.json` (the r4 build), `../vision/vision_rows.jsonl` with its images, r3's `audio/audio_rows.jsonl` with its clips, and r3's gate set (`gate/vision/cases.json`, `gate/audio/truth.json`; `--no-gate` leaves it out). It writes `out/`:

| file | goes to | |
|---|---|---|
| `ask-lora-data.zip` | `~/wisp/uploads` | text train/dev, vision, audio, `train.py`, `render.py`, parity rows. Its sha256 is printed and in `out/upload.json` |
| `mmeval-data.zip`, `pickers.zip` | `~/wisp/uploads` | evaluator code, vision/audio dev rows, gate set; `pick_text.py`, `reward.py`, `data/text-dev.jsonl` (the valid text steps with `export_prompts.classify` labels) |
| `upload.json` | stays | sha256s and an exact-token SFT estimate |

`pack_r4.py` uses `grpo/` of r4 when it exists, otherwise r3's, for `export_prompts.classify` and `reward.py`, so labels and scoring are the ones the GRPO stage will use. The text-dev join is by position and ids are checked. On r3's data the oracle (each row's own ideal completion) scores selection 100, facts 100, forbidden 0, so labels and reward agree.

Also upload `ask-lora-r3/brev/vm-setup.sh` and `run_sft.sh`.

### On the Brev instance

Instance: one A6000 48 GB, Ubuntu with a CUDA 12.8-capable driver (570+), about 100 GB disk (base 16 + merged 16 + venv + zips + about 1 GB of adapters). Put everything in `~/wisp/uploads` (`mkdir -p` first), run `run_sft.sh` from anywhere. Run long steps under `nohup` or tmux.

| # | command | does | expect |
|---|---|---|---|
| 1 | `bash run_sft.sh setup` | vm-setup with the pins: torch 2.11.0+cu128, torchvision 0.26.0, transformers 5.22.1, peft 0.21.2 (r2's A6000 stack, from its `run-info.json`); writes `~/wisp/freeze.txt`; downloads the base | `SETUP_DONE`, "torch OK" |
| 2 | `bash run_sft.sh check` | GPU, versions against the pins, zips present | all `ok`, sm_86, 47-48 GiB |
| 3 | `bash run_sft.sh unpack <sha>` | verifies and unpacks the data zip | `unpacked N verified files` |
| 4 | `bash run_sft.sh preflight` | template sha, render parity, lengths, stop tokens, vision and audio processors | `PREFLIGHT OK` |
| 5 | `bash run_sft.sh smoke` | strict attention parity, cache parity, anchor, the 12 longest rows, a 15-step overfit | exit 0, peak around 22 GiB, "one epoch ~ N min". If tok/s is far from 624, rerun `estimate.py` before committing |
| 6 | `bash run_sft.sh train` then `status` | background run, 2 epochs, 8 rows per step | `status` shows step, ETA, peak, evals |
| 7 | `bash run_sft.sh unzip-pickers` | unpacks mmeval and picker code into `~/wisp/mmeval` | |
| 8 | `nohup bash run_sft.sh pick > ~/wisp/gemma/pick.out 2>&1 &` | candidates, mmeval, text generations, decision | `PICKED: <adapter dir>` or exit 2 |
| 9 | `bash run_sft.sh merge`, `package`, `bundle` | merges the picked adapter (refuses without a decision), run-info and checksums, one tarball | |
| 10 | download `~/wisp/gemma/r4-sft-a6000-bundle.tgz` and `gemma-4-E4B-it-Wisp-Ask-merged`, then stop the instance | | |

Resume after a crash: `bash run_sft.sh train ~/wisp/gemma/ask-lora-out/step-NNNNN` (the last three eval steps keep optimizer state; adapter-only snapshots in `ckpt/` cannot resume).

Do not run the pickers while training: they need the GPU.

## The picker

`pick` runs these on the same candidate list (`pick_text.py candidates`: steps from 45% of the run, at most `MAX_CKPTS` = 6, the last one always; `CKPTS=a,b` overrides):

1. **mmeval** (`eval_mm.py`): base and each candidate on vision, reading, documents and audio dev rows, `--strict --require-reading`. Unchanged from r3.
2. **Text picker** (`pick_text.py generate`): up to 360 valid text steps, proportional per family, the same steps for every checkpoint. Production sampling (T 1.0, top_p 0.95, top_k 64, thinking on as rendered), batch 1, one seed per (step, sample) shared by all checkpoints, max 384 new tokens. Scored by `reward.py`.
3. **Decision** (`pick_text.py pick`):
   - eligible = **zero forbidden calls** (the label's forbidden tools, and any reward part of forbidden class: `forbidden`, `planted`, `requires_read`, `early_write`, or non-tool-set/args parts of the reward's `selection` group), no missing generations, and **passes the mmeval picker**;
   - selection score = tool set + key args as a percentage of a perfect step (1.5 for a call or load, 1.0 for an answer or question);
   - `near` = eligible checkpoints within **1 point** of the best *eligible* selection score;
   - winner = best **facts** (share of required facts in the answer, over answer steps with facts) among `near`, then higher selection, fewer malformed, earlier step.

The best-selection reference is taken among the eligible, so a checkpoint that fails mmeval cannot set the bar (a test pins this). A missing or errored generation counts as malformed and also disqualifies. No eligible checkpoint: exit 2, nothing printed, **do not merge**. mmeval is mandatory (`--no-mmeval` exists for dev use and warns).

Reported, not decided on: malformed rate, share of narrate answers that kept a thought (r4's central fix: r3 was about 10% in its data), thought-word and answer-length medians, unneeded calls, top reward flags. Read `~/wisp/gemma/pick/picked.table.md` and these before merging. With about 360 steps one step is 0.3 points and sampling noise is about 1-2 points, so "within 1 point" is tighter than the noise; `--samples 2` halves it at twice the cost.

## Changes to `ask-lora-r3/brev` (all additive; r3's MI355X command is unchanged)

- `train.py`: `--dev-text-max N` (default 0 = every dev row, so r3 behaves as before) and `spread_indices`.
- `render.py`: `--upload-json PATH` (default stays `brev/upload-zip.json`), so packing r4 does not overwrite r3's record. The dataset directory was already selectable by `ASK_LORA_DATA`.
- `vm-setup.sh`: `TRANSFORMERS_SPEC` / `PEFT_SPEC` (defaults are the old ranges) and a `freeze.txt` of the installed stack.

## Not verified

- Everything on the GPU: torch 2.11.0+cu128 / torchvision 0.26.0 installing together (the torchvision pairing is assumed; r2's run-info lists only torch), `smoke`, the speed and the 22 GiB peak with r3's KL-anchor and multimodal rows (r2 had neither), the parity test on sm_86.
- `pick_text.py generate` has never run. It reuses `eval_mm.py`'s loading, adapter switching and processor call (proven on r3's text-document rows), but text generation with the 6.5k-token prompt, `generate` kwargs and sampling are new.
- The picker time is derived (prefill at 3x the training token speed, plan.py's 30-55 ms decode); the mmeval time is a guess (4 s per row).
- plan.py's A6000 GRPO hours versus the calibrated ones: the calibrated figure assumes GRPO runs at the SFT efficiency.
- r4's valid set size and mean row length are unknown until the data exists; `pack_r4.py` prints exact counts and the estimate.
