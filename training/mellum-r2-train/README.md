# Mellum R2 LoRA on one A6000 48GB

| file | what |
|---|---|
| `vm-setup.sh` | paste into the VM's setup-script field; installs the stack, downloads the base, writes `~/wisp/run.sh` |
| `mellum-r2-a6000.ipynb` | upload to Jupyter and run top to bottom; every cell is one `run.sh` step |
| `train.py` | `preflight` / `smoke` / `train` / `merge`; packed into the data zip as `train/train.py` |

Base: `JetBrains/Mellum2.1-12B-A2.5B-Thinking` @ `92ddae9fc7665e9f801d141d2e5a6b2caf2460c4`, the revision the
local oQ5e build was quantized from. Data: `training/mellum-dataset-r2-20261015/` → `mellum-r2-data.zip`.

## R2 vs R1

This kit is the R1 kit (`training/mellum-r1-train/`) with every path and name moved to R2: the data zip
`mellum-r2-data.zip`, the work dirs `~/wisp/mellum-r2*`, the adapter `mellum-r2-adapter.zip` and the merged model
`Mellum2.1-12B-A2.5B-Thinking-Wisp-R2-merged`. Training hyperparameters remain unchanged.
The current-main reconciliation adds source acceptance, verified staged unpack,
and fail-closed local template/preflight binding; it does not run GPU training.

| | R1 | R2 |
|---|---|---|
| train / dev rows | 2252 / 272 | 3270 / 392 |
| tokens per train epoch | 10.1M | 13.6M (+34%) |
| longest row | 11,127 | 12,129 |

Hyperparameters are unchanged: LoRA attention-only r=16, lr 1e-4, 16 rows per optimizer step, 2 epochs, eval every
50 steps (~205 steps per epoch, so ~4 evals per epoch). R1's problems were new failure modes in the data (unoffered
tools, acting on file contents, tools on chat answers), not under- or over-fitting: its dev loss fell steadily from
0.32 to 0.127. R2 adds rows for those modes rather than changing the recipe, so a gate result stays comparable with R1.
Expect each epoch to take about a third longer than R1's. `auto <hours>` sizes the epochs from the smoke speed, so
give it the same deadline or a longer one.

## All commands, in order

On the Mac, accept and package the committed historical corpus without a model
or tokenizer (use the repository Python environment with PyYAML):

```bash
python training/mellum-dataset-r2-20261015/build.py validate-rendered
python -m pytest -q tests/test_mellum_r2_source_contracts.py
python training/mellum-dataset-r2-20261015/build.py package
```

`package` reruns acceptance and refuses stale/missing evidence, then writes
`mellum-r2-data.zip`, with its sha256 recorded in `upload-zip.json`. Preserve that
record outside the archive as the trusted source of the digest passed to unpack.
These are source/data/package checks. They do not replace tokenizer preflight,
the GPU smoke step, training, or the post-training quality gate.

On the VM:

| # | notebook cell | does | expect |
|---|---|---|---|
| 0 | (setup field) `vm-setup.sh` | uv venv, torch + transformers ≥5.8.1 + peft, optional flash-attn, 24GB base download | `~/wisp/SETUP_DONE` |
| 1 | `!~/wisp/run.sh check` | GPU, versions, `MellumForCausalLM`, flash-attn, base shards, data zip sha | all yes; sha = `upload-zip.json` |
| 2 | upload `mellum-r2-data.zip` → `wisp/uploads`, then `!~/wisp/run.sh unpack <trusted-sha256>` | verify the external ZIP digest, fixed inventory, safe paths/types/sizes and every member hash, then stage and replace data | `manifest: … ALL OK` |
| 3 | `!~/wisp/run.sh preflight` | local tokenizer only: required template sha, lengths, `<|im_end|>` endings, decode round-trip; bind marker to data/trainer/template hashes | `PREFLIGHT OK` |
| 4 | `!~/wisp/run.sh smoke` | 8 longest rows fwd+bwd, 1 optimizer step | `MEMORY OK`, peak GiB, full-run estimate |
| 5 | `!~/wisp/run.sh train` | background run, 2 epochs, eval + checkpoint every 50 steps | `training started` |
| 6 | `!~/wisp/run.sh status` (repeat) | progress, last evals, errors | loss falling, no OOM |
| 7 | `!~/wisp/run.sh merge` | best adapter → bf16 merged model, checked against adapter on a dev row | `merged model saved` |
| 8 | `!~/wisp/run.sh package` | adapter zip + merged shard checksums | sha256 lines |

**Overnight, unattended:** after `check` and `unpack`, run `!~/wisp/run.sh auto <hours until you need it>` instead of
cells 3–8. It runs preflight, smoke (falling back to SDPA and lower length caps if memory is short), training with as
many epochs (≤2) as fit before the deadline, merge and package. Training stops cleanly 30 minutes before the
deadline, and a crash is resumed once. In the morning, `!~/wisp/run.sh autostatus` shows `ALL DONE` or the `STOP:` reason.

If anything fails:
- **Resume after a stop:** `!~/wisp/run.sh train ~/wisp/mellum-r2-lora/step-NNNNN`
- **Lower the length cap** after OOM or `MEMORY TIGHT`: `!~/wisp/run.sh smoke 10240`, then `!~/wisp/run.sh train "" 10240`.
  An OOM during training saves `oom-recovery/` and prints the resume command.
- **Missing base download:** `!~/wisp/run.sh base`

Back on the Mac: download the merged model and quantize it to oQ5e with oMLX as
`Mellum2.1-12B-A2.5B-Thinking-Wisp-R2-oQ5e`. Then quit Wisp and run:

```bash
.venv/bin/python scripts/run_toolbench.py --models Mellum2.1-12B-A2.5B-Thinking-Wisp-R2-oQ5e --split gate --trials 3 --thinking narration-off --exclusive
```

The plan's thresholds decide adoption, and adoption still needs your go-ahead.

## GPU memory

Mellum has 12.15B parameters (22.6 GiB in bf16), about 50% more than Ling-3.0-tiny (~16 GiB). The extra ~7 GiB of
weights is fixed, so the room has to come from elsewhere. The Ling run's largest transient was the logits:
16k tokens × 157k vocab, about 10 GiB in fp32, plus the same again for their gradient, with the cut-cross-entropy
plugin uncertain for that custom model.

| lever | saves (at 12k tokens) | how |
|---|---|---|
| **logits for the completion only** | ~9 GiB (2.2 GiB bf16 + 4.5 GiB fp32 + grad) → <0.5 GiB | `logits_to_keep = len(completion)+1`; the loss is completion-only anyway, so this is exact, not an approximation |
| `--max-len 12288` instead of 16384 | ~25% of activations | the real longest R2 row is 12,129 tokens (R1: 11,127), so 12288 keeps every row; preflight prints it |
| LoRA attention only, r=16 | optimizer state ~0.1 GiB | 8.3M trainable params; experts and router frozen |
| gradient checkpointing (non-reentrant), micro-batch 1 | stores 28 layer inputs (~1.5 GiB) instead of all activations | |
| `expandable_segments:True` | fragmentation, often the "near the limit" cause | set before torch loads |
| flash-attn 2 (if a wheel exists) | no materialised sliding-window attention | SDPA is the fallback; eager is never used |

Estimated peak: 22.6 weights + ~1.5 checkpoints + ~1–2 recompute of one MoE layer + <0.5 logits + ~2 CUDA
context and slack, so about **28–32 GiB of 48**. This is an estimate. The smoke step measures it on the 8 longest
rows, which bounds the full run because every other row is shorter.

**Why not QLoRA as the fallback?** 11.1B of the 12.15B parameters (91%) are expert weights. They are fused 3-D tensors
in the MLX build (`mlp.switch_mlp.*`) and expected to be fused in Transformers 5.x as well; `check` prints the
real layer-0 weight names to confirm. bitsandbytes 4-bit only replaces `nn.Linear` layers, so QLoRA would quantize the attention, embedding
and head (~1B params) and leave the experts in bf16, saving roughly 1.5 GiB. Lowering `--max-len` is the real lever.
At 10,240 the trainer skips the 32 longest train rows (of 3270) and 4 dev rows, which preflight lists.

## Verification limits

- The historical 2026-10-16 Mac tokenizer preflight passed; it has **not** been
  rerun for this reconciliation. Current source tests mock the tokenizer and
  exercise only its local-only and required-template controls. Missing or changed
  templates fail before tokenizer loading, and changed data/trainer/template
  bytes invalidate the VM's preflight marker.
- `train.py` has only been exercised offline; the GPU
  path (model load, LoRA targets, `logits_to_keep`, backward) first runs in the smoke cell. The trainer fails fast
  if LoRA does not find exactly 112 attention projections, and it warns if the model ignores `logits_to_keep`.
- The flash-attn wheel availability and the HF MoE expert implementation's speed are unknown until `check` and
  `smoke` run. Plan from the smoke estimate, not from FLOPs.
