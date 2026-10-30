# Wisp Ask LoRA on a Brev GPU (Gemma 4 E4B)

This kit trains a LoRA adapter for Wisp's Ask model on one rented NVIDIA GPU. It then merges the adapter into the bf16 base, and brings the result back to the Mac to be quantized to oQ4e by oMLX.

**Not run yet.** No GPU step has executed. Only the Mac-side steps have been tested so far: the render, the trainer's tokenizer preflight, the archive check and the shell syntax.

| file | what |
| --- | --- |
| `render.py` | Mac: renders `../train.jsonl` and `../valid.jsonl` into prompt/completion rows with Wisp's own Gemma template, then writes `ask-lora-data.zip` |
| `ask-lora-data.zip`, `upload-zip.json` | the rendered data (2 MB) and its sha256. The zip includes `train.py`. |
| `vm-setup.sh` | Brev: installs the stack, downloads the base, and writes `~/wisp/run.sh` |
| `train.py` | Brev: `preflight`, `smoke`, `train`, `sample` and `merge` (packed into the zip) |

## The recipe, and where it comes from

| choice | value | source |
| --- | --- | --- |
| base | `google/gemma-4-E4B-it` @ `ee0ef602`, bf16, not gated | Wisp's oQ4e build has the identical chat template, tokenizer and generation config (sha256-checked). It differs only by the MTP head oMLX added. |
| stack | `transformers>=5.10.1`, `peft>=0.19`, `torch>=2.7` | [Google's Gemma 4 QLoRA guide](https://ai.google.dev/gemma/docs/core/huggingface_text_finetune_qlora) pins these. Transformers 5.5.2 fixed [use_cache=False corrupting E4B's KV-shared layers](https://github.com/huggingface/transformers/issues/45242), and PEFT 0.19 needs torch 2.7 ([Oxen write-up](https://ghost.oxen.ai/writing-a-fine-tuning-and-deployment-pipeline-isnt-as-easy-as-it-looks-gemma-4-version/)). |
| LoRA | r=16, alpha=16, dropout 0.05, on the language model's q/k/v/o and gate/up/down (294 modules) | r, alpha and dropout are from Google's guide. Attention plus MLP is [Unsloth's Gemma 4 guide](https://unsloth.ai/docs/models/gemma-4/train) default. Towers are excluded, as in every source. |
| towers | vision and audio frozen, and the target regex is limited to `language_model` | The towers' `Gemma4ClippableLinear` layers break PEFT when a target matches them (Oxen) |
| model class | `AutoModelForMultimodalLM` for training and merging | Google's guide merges with this class. The same class for training keeps adapter keys aligned. Wisp needs the towers for image input and voice. |
| optimizer | AdamW (fused), lr 2e-4, cosine schedule, 3% warmup, grad clip 0.3, batch 1 × 8 accumulation, 2 epochs, best checkpoint by dev loss | lr 2e-4 is from Google and Unsloth; the community E4B adapters use 1e-4 to 2e-4 and 2 to 3 epochs |
| loss | completion only, with logits computed for the completion positions alone | Google's prompt-completion format. Gemma's 262k vocabulary makes full logits about 10 GiB at 9.5k tokens. |
| thinking | the prompt is rendered with each request's `enable_thinking: true`, as Wisp sends it | Wisp production. See the next section. |
| sanity checks | cache parity, then overfit 2 rows before training | the KV-sharing bug report and Oxen's "overfit a tiny set first" |
| optional QLoRA | `QLORA=1` loads the base in NF4 for a 24 GB GPU; the merge stays bf16 | Google's guide |

### Thinking: why some targets start with a short thought

Wisp sends every Ask step with `enable_thinking: true`. After a tool result, Gemma's template ends the prompt with `<|channel>thought\n`, so the model's reply there always opens inside a thought.

A target with no thought would therefore teach the model to put its answer inside the thought channel. So `render.py` gives those steps (457 of 833 train rows) a one-sentence thought before the answer or call, for example: "The get_upcoming result has what's needed. Answer only from it, lead with the answer, keep it short."

First steps, straight after the user, have no forced thought and stay answer-only.

The thoughts are deliberately short, because decode dominates Wisp's latency. Unsloth suggests keeping at least 75% reasoning-style rows to preserve reasoning. This set is at 55%, with very short thoughts, so check reasoning-heavy Ask questions in the eval.

## GPU and cost

Choose a 48 GB card: L40S, A6000, or A100/H100 for more speed. Unsloth puts E4B LoRA at about 17 GB. This set adds 9.5k-token rows, and `smoke` measures the real peak.

A 24 GB card (L4, A10) needs `QLORA=1`. Each epoch is 5.2M tokens over 833 rows, so 2 epochs is about 10.4M tokens. Expect well under an hour on an H100 and about 1 to 2 hours on an L40S or A6000. These are estimates; `smoke` prints the measured time for the full run.

The rendered data is synthetic and already public in this repository, so no user data leaves the Mac.

## Steps

**On the Mac** (repo `.venv` Python, Gemma tokenizer only, no model load):

```bash
cd training/ask-lora-20261029
PY=<main checkout>/.venv/bin/python
$PY builder/build.py && $PY builder/validate.py          # only if the dataset changed
$PY brev/render.py --model ~/Desktop/OMLX_Model_Files/Jundot/gemma-4-E4B-it-oQ4e-mtp
```

**On the Brev instance:**

1. Start an instance with one 48 GB GPU, Ubuntu with a CUDA driver, and at least 60 GB of disk.
2. Copy over `vm-setup.sh` and `ask-lora-data.zip`, for example with `brev shell` / `scp`, or with Jupyter's upload.

| # | command | does | expect |
| --- | --- | --- | --- |
| 0 | `bash vm-setup.sh` | uv venv, the stack, the 16 GB base | `~/wisp/SETUP_DONE` |
| 1 | `~/wisp/run.sh check` | GPU, versions, multimodal class, base, zip sha | sha equals `upload-zip.json` |
| 2 | move the zip into `~/wisp/uploads`, then `~/wisp/run.sh unpack <sha256 from upload-zip.json>` | verifies the zip sha and every file's hash | `unpacked 4 verified files` |
| 3 | `~/wisp/run.sh preflight` | template sha, lengths, stop tokens, decode round-trip | `PREFLIGHT OK` |
| 4 | `~/wisp/run.sh smoke` | cache parity; 8 longest rows forward and backward; overfit 2 rows | `MEMORY OK`, peak GiB, minutes per epoch, overfit loss falls |
| 5 | `~/wisp/run.sh train` | background run, 2 epochs, dev eval about 4 times per epoch, step-0 baseline | `training started` |
| 6 | `~/wisp/run.sh status` (repeat) | progress, evals, errors | dev loss below the step-0 baseline |
| 7 | `~/wisp/run.sh sample` | greedy decode of 8 dev prompts next to the target | answers and calls look right |
| 8 | `~/wisp/run.sh merge` | best adapter folded into the bf16 multimodal model | `names identical to the base` |
| 9 | `~/wisp/run.sh package` | adapter zip (checks for empty tensors) plus merged checksums | sha256 lines |

**Unattended:** after steps 1 and 2, `~/wisp/run.sh auto <hours>` runs steps 3 to 9. It retries smoke at a shorter length cap if memory is tight, and stops for a pipeline fault. It fits as many epochs (at most 2) as the deadline allows. Check it with `~/wisp/run.sh autostatus`. When it's done, stop the instance so it stops billing.

**Back on the Mac:**

1. Download `~/wisp/ask-lora-adapter.zip` and the merged folder `~/wisp/gemma-4-E4B-it-Wisp-Ask-merged` (about 16 GB), and check it against `SHA256SUMS`.
2. Download the MTP donor `google/gemma-4-E4B-it-assistant`.
3. In oMLX's admin panel, under oQ:
   - choose the merged folder;
   - set level 4, turn on enhanced, and set dtype to bfloat16;
   - set **Combine MTP head** to the assistant folder.
   - The output is `gemma-4-E4B-it-Wisp-Ask-oQ4e-mtp`, the same recipe as Wisp's current model.
4. Evaluate before adopting:
   - re-run the Ask eval against an isolated backend on port 8775 (never the live 8765);
   - run `scripts/run_toolbench.py` with Wisp quit;
   - compare against the current oQ4e on the same questions, including decode speed, since the MTP head was trained on the original base.
   - Adoption needs your go-ahead.

## Not verified

- No GPU step has run.
  - Model load, LoRA targeting, `logits_to_keep` on this class, the forward signature's modality masks, merge and save first run in `smoke` and `merge`.
  - The trainer exits rather than training on a wrong setup: LoRA must match exactly 42 language q_proj, cache parity must hold, the overfit must work, and merged tensor names must equal the base's.
- **oQ on a fine-tuned checkpoint.** oMLX's quantizer and its MTP combine are used exactly as for the stock model, but haven't been run on a merged one.
  - A LoRA shifts the hidden states the MTP draft head predicts from. Draft acceptance, and with it decode speed, may drop.
- **Time and memory** are estimates until `smoke` runs.
