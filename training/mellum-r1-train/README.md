# Mellum R1 LoRA on one A6000 48GB

| file | what |
|---|---|
| `vm-setup.sh` | paste into the VM's setup-script field; installs the stack, downloads the base, writes `~/wisp/run.sh` |
| `mellum-r1-a6000.ipynb` | upload to Jupyter and run top to bottom; every cell is one `run.sh` step |
| `train.py` | `preflight` / `smoke` / `train` / `merge`; packed into the data zip as `train/train.py` |

Base: `JetBrains/Mellum2.1-12B-A2.5B-Thinking` @ `92ddae9fc7665e9f801d141d2e5a6b2caf2460c4`, the revision the
local oQ5e build was quantized from. Data: `training/mellum-dataset-r1-20261014/` → `mellum-r1-data.zip`.

## All commands, in order

On the Mac, once the dataset is final:

```bash
cd training/mellum-dataset-r1-20261014 && ../../.venv/bin/python build.py render && ../../.venv/bin/python build.py validate --template-checks 100000 && ../../.venv/bin/python build.py stats && ../../.venv/bin/python build.py package
```

`package` writes `mellum-r1-data.zip`, with its sha256 recorded in `upload-zip.json`.

On the VM:

| # | notebook cell | does | expect |
|---|---|---|---|
| 0 | (setup field) `vm-setup.sh` | uv venv, torch + transformers ≥5.8.1 + peft, optional flash-attn, 24GB base download | `~/wisp/SETUP_DONE` |
| 1 | `!~/wisp/run.sh check` | GPU, versions, `MellumForCausalLM`, flash-attn, base shards, data zip sha | all yes; sha = `upload-zip.json` |
| 2 | upload `mellum-r1-data.zip` → `wisp/uploads`, then `!~/wisp/run.sh unpack` | unzip + per-file manifest check | `manifest: … ALL OK` |
| 3 | `!~/wisp/run.sh preflight` | tokenizer-only: template sha, lengths, `<|im_end|>` endings, decode round-trip | `PREFLIGHT OK` |
| 4 | `!~/wisp/run.sh smoke` | 8 longest rows fwd+bwd, 1 optimizer step | `MEMORY OK`, peak GiB, full-run estimate |
| 5 | `!~/wisp/run.sh train` | background run, 2 epochs, eval + checkpoint every 50 steps | `training started` |
| 6 | `!~/wisp/run.sh status` (repeat) | progress, last evals, errors | loss falling, no OOM |
| 7 | `!~/wisp/run.sh merge` | best adapter → bf16 merged model, checked against adapter on a dev row | `merged model saved` |
| 8 | `!~/wisp/run.sh package` | adapter zip + merged shard checksums | sha256 lines |

If anything fails:
- **Resume after a stop:** `!~/wisp/run.sh train ~/wisp/mellum-r1-lora/step-NNNNN`
- **Lower the length cap** after OOM or `MEMORY TIGHT`: `!~/wisp/run.sh smoke 10240`, then `!~/wisp/run.sh train "" 10240`.
  An OOM during training saves `oom-recovery/` and prints the resume command.
- **Missing base download:** `!~/wisp/run.sh base`

Back on the Mac: download the merged model and quantize it to oQ5e with oMLX as
`Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-oQ5e`. Then quit Wisp and run:

```bash
.venv/bin/python scripts/run_toolbench.py --models Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-oQ5e --split gate --trials 3 --thinking narration-off --exclusive
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
| `--max-len 12288` instead of 16384 | ~25% of activations | the real longest row is ~11.1k tokens (preflight prints it) |
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
At 10,240 the trainer skips only the longest few rows, which preflight lists.

## Not verified yet

- `train.py` has only been exercised offline: preflight on the Mac passes with Mellum's tokenizer, but the GPU
  path (model load, LoRA targets, `logits_to_keep`, backward) first runs in the smoke cell. The trainer fails fast
  if LoRA does not find exactly 112 attention projections, and it warns if the model ignores `logits_to_keep`.
- The flash-attn wheel availability and the HF MoE expert implementation's speed are unknown until `check` and
  `smoke` run. Plan from the smoke estimate, not from FLOPs.
