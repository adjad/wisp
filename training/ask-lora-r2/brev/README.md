# Wisp Ask LoRA on a Brev GPU (round 2: Gemma 4 E4B and Nemotron 3.5 Bolt E4B)

This kit trains **two separate LoRA adapters on one shared dataset** on one rented NVIDIA GPU:

| tag | base | local build Wisp can run today |
| --- | --- | --- |
| `gemma` | `google/gemma-4-E4B-it` @ `ee0ef602`, bf16, not gated | `Jundot/gemma-4-E4B-it-oQ4e-mtp` |
| `bolt` | `nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-BF16` (an RL post-train of Gemma 4 E4B: same architecture, same tokenizer), **revision: you must pin it** | `UraionLabs/NVIDIA-Nemotron-3.5-Bolt-E4B-MLX-oQ4e-mtp` |

Each adapter is merged into its own bf16 base and brought back to the Mac, where oMLX quantizes it. The two runs never share outputs: everything for a model lives under `~/wisp/<tag>/`.

**Not run yet.** No GPU step has executed. Only Mac-side steps have been tested so far: the render, the trainer's tokenizer preflight, the archive check and the shell syntax. The Bolt path has not run even on the Mac beyond the static checks.

| file | what |
| --- | --- |
| `render.py` | Mac: renders `../train.jsonl` and `../valid.jsonl` into prompt/completion rows with Gemma's template, checks them against Bolt's template, then writes `ask-lora-data.zip` |
| `ask-lora-data.zip`, `upload-zip.json` | the rendered data and its sha256. The zip also carries `train.py`, `render.py` and `parity.jsonl` (raw sample rows for the on-GPU template check). `upload-zip.json` is stale until `render.py` is re-run on the rebuilt dataset. |
| `vm-setup.sh` | Brev: installs the stack, downloads the base(s), and writes `~/wisp/run.sh` |
| `train.py` | Brev: `preflight`, `smoke`, `train`, `sample` and `merge`, with `--tag gemma|bolt` (packed into the zip) |

## One dataset, two templates

The dataset is rendered **once**, with Gemma's chat template. Bolt's template differs (its `conversational_mode` preamble, which Wisp never sends, and it keeps assistant `content` before tool calls where Gemma's drops it). The markup, role tokens, tool-call syntax and tokenizer are the same. For this data the rendered rows are byte-identical under both, because tool-call steps carry empty content. This was measured on all 920 rows (audit, tokenizer only). **Rule for the dataset builder: keep tool-call steps' `content` empty**, or the two models would see different training rows.

That is checked rather than assumed, in two places:

1. **On the Mac**, `render.py --parity-model <Bolt local build>` re-renders every row with Bolt's own template and aborts on any difference. It records the result in the zip's `stats.json`. Without `--parity-model` it prints `NOT CHECKED`; do not train Bolt then.
2. **On the GPU box**, `train.py preflight` re-renders a spread sample (60 train, 20 dev rows, from `parity.jsonl`) with the template of the **downloaded** repo and requires byte-identical rows. This is the binding gate, because the Hugging Face bf16 repo's template is not guaranteed to equal the quant's.

Template sha256 is also checked per model (pinned in `render.py` as `TEMPLATE_SHAS`):

| model | pinned sha256 (of the local oQ4e build's `chat_template.jinja`) | if the downloaded bf16 repo differs |
| --- | --- | --- |
| gemma | `0a2c8073c878ab1d...` (same as the HF repo, checked) | preflight **fails** |
| bolt | `35b7d0641b6fb4ee...` (inline copy in `tokenizer_config.json` is identical) | preflight **warns** and prints the new sha; parity decides. Record that sha in your notes. |

Preflight also checks `config.json`: `Gemma4ForConditionalGeneration`, 42 language layers (the LoRA regex, stop ids and cache check assume both).

## The recipe, and where it comes from

| choice | value | source |
| --- | --- | --- |
| base | see the table above, bf16 | Gemma: Wisp's oQ4e build has the identical chat template, tokenizer and generation config (sha256-checked); it differs only by the MTP head oMLX added. Bolt: UraionLabs' README names `nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-BF16` as `base_model`; tokenizer.json and generation_config.json are byte-identical to Gemma's, config differs only in `max_position_embeddings` (65536 vs 131072). |
| stack | `transformers>=5.10.1`, `peft>=0.19`, `torch>=2.7` | [Google's Gemma 4 QLoRA guide](https://ai.google.dev/gemma/docs/core/huggingface_text_finetune_qlora) pins these. Transformers 5.5.2 fixed [use_cache=False corrupting E4B's KV-shared layers](https://github.com/huggingface/transformers/issues/45242), and PEFT 0.19 needs torch 2.7 ([Oxen write-up](https://ghost.oxen.ai/writing-a-fine-tuning-and-deployment-pipeline-isnt-as-easy-as-it-looks-gemma-4-version/)). |
| LoRA | r=16, alpha=16, dropout 0.05, on the language model's q/k/v/o and gate/up/down (294 modules) | r, alpha and dropout are from Google's guide. Attention plus MLP is [Unsloth's Gemma 4 guide](https://unsloth.ai/docs/models/gemma-4/train) default. Towers are excluded, as in every source. |
| towers | vision and audio frozen, and the target regex is limited to `language_model` | The towers' `Gemma4ClippableLinear` layers break PEFT when a target matches them (Oxen) |
| model class | `AutoModelForMultimodalLM` for training and merging | Google's guide merges with this class. The same class for training keeps adapter keys aligned. Wisp needs the towers for image input and voice. Bolt is the same class by architecture, but loading it is unverified until `smoke` runs. |
| optimizer | AdamW (fused), lr 2e-4, cosine schedule, 3% warmup, grad clip 0.3, batch 1 x 8 accumulation, 2 epochs, best checkpoint by dev loss | lr 2e-4 is from Google and Unsloth; the community E4B adapters use 1e-4 to 2e-4 and 2 to 3 epochs. Bolt is an RL post-train, so it may want a lower lr; the dev-loss curve from `status` is the check. |
| loss | completion only, with logits computed for the completion positions alone | Google's prompt-completion format. The 262k vocabulary makes full logits about 10 GiB at 9.5k tokens. |
| thinking | the prompt is rendered with each request's `enable_thinking: true`, as Wisp sends it | Wisp production. See the next section. |
| sanity checks | cache parity, then overfit 2 rows before training | the KV-sharing bug report and Oxen's "overfit a tiny set first" |
| optional QLoRA | `QLORA=1` loads the base in NF4 for a 24 GB GPU; the merge stays bf16 | Google's guide |

### Thinking: situational (round 2)

Wisp sends every Ask step with `enable_thinking: true`, and that does not change, because toggling it would break oMLX's prefix cache. Each example's meta carries `think` and `thought`, chosen by `builder/thinking.py`:
- **THINK:** the target opens with the thought, 1–3 sentences of at most 60 words. On a first step the model opens the channel itself; after a tool result the template has already opened it.
- **NO THINK on a first step:** answer or call only.
- **NO THINK after a tool result:** the template forces `<|channel>thought\n`, so the completion closes it empty, `\n<channel|>` (token ids 107, 101), then answers.

About 37% of steps think. Chat, knowledge, simple reads and narration over one clean result never think.

## GPU, time and disk (estimates, not measurements)

Choose a 48 GB card: L40S, A6000, or A100/H100 for more speed. Unsloth puts E4B LoRA at about 17 GB. The long rows (about 9.5k tokens) add to that, and `smoke` measures the real peak per model. A 24 GB card (L4, A10) needs `QLORA=1`.

| | estimate | basis |
| --- | --- | --- |
| time per model | about 1 to 2 h on an L40S or A6000, under 1 h on an H100 | token count: 2 epochs of about 10 M tokens (from the round-1 dataset; re-check `stats.json` for the rebuilt one) |
| both models | about 2 to 4 h on an L40S, about 2 h or less on an H100, plus downloads | two sequential runs |
| disk, one model | about 60 GB | base 16 + merged 16 + checkpoints + venv + data |
| disk, both | about 100 to 150 GB (both bases, both merged) | with less than 80 GB free after the first model, run-both deletes the finished base (`TIGHT_GB`) |

Replace these with `smoke`'s measured minutes per epoch and `df` as soon as they exist. The rendered data is synthetic and already public in this repository, so no user data leaves the Mac.

## Steps

**On the Mac** (repo `.venv` Python, tokenizers only, no model load):

```bash
cd training/ask-lora-r2
PY=<main checkout>/.venv/bin/python
$PY brev/render.py --model ~/Desktop/OMLX_Model_Files/Jundot/gemma-4-E4B-it-oQ4e-mtp
```

It writes the zip and records its sha in `upload-zip.json`. Round 2 trains Gemma only. Add `--parity-model <dir>` only if Bolt returns.

**Only if Bolt returns (it is dropped, because it is text-only), settle the Bolt base first.** Open `nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-BF16` on Hugging Face, pick the commit to train from (its commit history), copy the full 40-hex sha, and check whether the repo is gated or needs a licence click-through. UraionLabs lists OpenMDW-1.1; read the terms, including what they say about a fine-tuned derivative. Setup refuses to run for Bolt without `BOLT_REV`.

**On the Brev instance:**

1. Start an instance with one 48 GB GPU, Ubuntu with a CUDA driver, and disk per the table above.
2. Copy over `vm-setup.sh` and `ask-lora-data.zip` (`brev shell` / `scp`, or Jupyter's upload).
3. Run `bash vm-setup.sh`. It defaults to Gemma; use `MODELS=bolt|both BOLT_REV=<sha>` only if Bolt returns. For a gated repo, `export HF_TOKEN=...` first.

Every later command names the model: `~/wisp/run.sh <gemma|bolt> <step>`.

| # | command | does | expect |
| --- | --- | --- | --- |
| 0 | `MODELS=... BOLT_REV=... bash vm-setup.sh` | uv venv, the stack, the 16 GB base(s) | `~/wisp/SETUP_DONE` |
| 1 | `~/wisp/run.sh gemma check` | GPU, versions, multimodal class, base, zip sha | sha equals `upload-zip.json` |
| 2 | move the zip into `~/wisp/uploads`, then `~/wisp/run.sh gemma unpack <sha256 from upload-zip.json>` | verifies the zip sha and every file's hash | `unpacked 6 verified files` |
| 3 | `~/wisp/run.sh gemma preflight` | config check, template sha, render parity against this base's template, lengths, stop tokens | `render parity ...: N/N rows byte-identical`, `PREFLIGHT OK` |
| 4 | `~/wisp/run.sh gemma smoke` | cache parity; 8 longest rows forward and backward; overfit 2 rows | `MEMORY OK`, peak GiB, minutes per epoch, overfit loss falls |
| 5 | `~/wisp/run.sh gemma train` | background run, 2 epochs, dev eval about 4 times per epoch, step-0 baseline | `training started` |
| 6 | `~/wisp/run.sh gemma status` (repeat) | progress, evals, errors | dev loss below the step-0 baseline |
| 7 | `~/wisp/run.sh gemma sample` | greedy decode of 8 dev prompts next to the target | answers and calls look right |
| 8 | `~/wisp/run.sh gemma merge` | best adapter folded into the bf16 multimodal model | `names identical to the base` |
| 9 | `~/wisp/run.sh gemma package` | adapter zip (checks for empty tensors), `run-info.json`, merged checksums | sha256 lines |

Repeat 1 to 9 with `bolt` (step 1 onward; the zip is already in `uploads`).

**Unattended, one model:** after steps 1 and 2, `~/wisp/run.sh <tag> auto <hours>` runs steps 3 to 9. It retries smoke at a shorter length cap if memory is tight, stops for a pipeline fault, and fits as many epochs (at most 2) as the deadline allows. Check it with `~/wisp/run.sh <tag> autostatus`.

**Unattended, both models:** `~/wisp/run.sh both <hours-per-model> <sha256 from upload-zip.json>` trains gemma, then bolt, back to back. For each it downloads the base if needed, unpacks, and runs the same unattended pipeline with its own deadline. When free disk is under `TIGHT_GB` (default 80) after Gemma is packaged, it deletes Gemma's base download (`freebase`; refuses unless the adapter zip and merged checksums exist) before fetching Bolt's. Merged folders are never deleted. It stops at the first failure and does not start the second model, because GPU time is billed. Check it with `~/wisp/run.sh bothstatus`. When it's done, stop the instance so it stops billing.

**Back on the Mac, per model:** download `~/wisp/<tag>/ask-lora-adapter.zip`, `~/wisp/<tag>/run-info.json` and the merged folder (about 16 GB, `gemma-4-E4B-it-Wisp-Ask-merged` or `NVIDIA-Nemotron-3.5-Bolt-E4B-Wisp-Ask-merged`), and check it against `SHA256SUMS`. Then follow the next section.

## Re-quantisation, MTP and the gate (per model)

The point of the Mac-side work is a **like-for-like** comparison: does the LoRA help, with everything else held fixed? The shipped quants are not that baseline, because their build recipes differ from anything stock oMLX reproduces. So for each model, **quantize both the untuned bf16 and the merged LoRA with the same recipe** and compare those two. The shipped quant is only a sanity reference.

### Quantisation

| | gemma | bolt |
| --- | --- | --- |
| shipped recipe (Jundot / UraionLabs) | oMLX 0.5.4.dev1, oQ level 4 enhanced, mixed 5/6-bit (84 layers at 5-bit, 18 at 6-bit) | **patched oMLX 0.7.2** with an imatrix lookup fix; attention q/k/v/o and per-layer gates at 8-bit (252 layers at 8-bit) |
| what to run | the installed oMLX: oQ, level 4, enhanced, dtype bfloat16, on (a) the untuned `gemma-4-E4B-it` bf16 and (b) the merged folder | the same, **but it must reproduce the Uraion recipe**: the imatrix fix and the 8-bit attention. Stock oMLX may not. Without the fix Uraion measured KLD 0.189 against 0.033 with it |
| if the recipe cannot be reproduced | quantize both bf16s with whatever the Mac has and say so in the notes; the gemma comparison is still like-for-like | same, but the untuned-vs-tuned comparison is **confounded by the quant** if the two builds use different recipes. Never compare the tuned stock-oMLX quant against the shipped Uraion quant |

For Bolt, the untuned bf16 you quantize must be the **same pinned revision** (`BOLT_REV` in `run-info.json`) that was trained. Quantize a folder with the same file layout both times and record the oMLX version/patch for each build.

### MTP head

Both shipped `-mtp` folders carry the same `model-mtp.safetensors` (159 MB, hard-linked: one file, Google's Gemma head; for Bolt it is already a graft onto a different backbone). After quantizing, in oMLX's admin panel set **Combine MTP head** to that **same donor file for both builds of a model** (untuned and merged), using the folder it came from (`google/gemma-4-E4B-it-assistant` is the upstream donor). Speculative decoding is lossless, so only acceptance rate and speed move.

A LoRA on attention and MLP shifts the hidden states the head predicts from. Measure **MTP acceptance rate and decode tokens/s** for the untuned build and the merged build, same prompts, same machine, same oMLX settings. If acceptance drops materially, retraining the head is a separate job and out of this kit's scope.

### Post-train checklist (per model; do not adopt anything without the user's go-ahead)

- [ ] **Artifacts:** adapter zip and merged folder match `SHA256SUMS`; `run-info.json` shows the pinned `base_rev` and the template sha that preflight printed.
- [ ] **Quant parity:** untuned bf16 and merged bf16 quantized with the identical recipe, versions recorded; for Bolt the Uraion recipe reproduced (imatrix fix, 8-bit attention) or the confound written down.
- [ ] **MTP:** same `model-mtp.safetensors` combined into both builds; acceptance rate and decode tok/s measured before (untuned) and after (tuned).
- [ ] **Toolbench gate**, with Wisp quit and nothing else loading models, one model at a time:
  `scripts/run_toolbench.py --models <model id> --profile-as gemma-4-E4B-it-oQ4e-mtp --exclusive --trials 3`
  once for the untuned quant and once for the tuned quant of each model, so the profile (prompts, thinking flags, sampling) is the packaged agent's for both. Compare against the untuned quant of the same model, then across models.
- [ ] **Chat-quality check:** reasoning-heavy and plain-chat Ask questions, tone, and the short-thought behaviour. Confirm the answers do not open inside the thought channel and are not terser or stiffer than the untuned build. Also confirm simple questions skip thinking (an empty thought) and that write, error and multi-source steps still think.
- [ ] **Isolation:** evaluation backends on port 8775, never the live 8765.

## Not verified

- No GPU step has run.
  - Model load, LoRA targeting, `logits_to_keep` on this class, the forward signature's modality masks, merge and save first run in `smoke` and `merge`. For Bolt, whether `AutoModelForMultimodalLM` loads the HF bf16 repo and whether its tensor names equal what the trainer expects.
  - The trainer exits rather than training on a wrong setup: LoRA must match exactly 42 language q_proj, cache parity must hold, the overfit must work, and merged tensor names must equal the base's.
- **Bolt revision, gating and licence terms** are unresolved: this kit does not know the pinned commit and has not seen the repo page.
- **Bolt's HF bf16 template** has not been compared with the quant's. Preflight's parity check is what tells.
- **oQ on a fine-tuned checkpoint.** oMLX's quantizer and its MTP combine are used exactly as for the stock models, but haven't been run on a merged one.
  - Draft acceptance, and with it decode speed, may drop (see MTP above).
- **Time and disk** are estimates until `smoke` and a first run measure them.
