# WispLing

On-device Ling-3.0-tiny (`model_type: bailing_hybrid`, `BailingMoeV3ForCausalLM`)
for the Wisp iPhone app, on MLX Swift (mlx-swift-lm 3.32.3, mlx-swift 0.32.3 /
MLX core 0.32.2).

## What was ported, and from where

oMLX does **not** run mlx-lm's own `mlx_lm/models/bailing_hybrid.py`. That file is
Ling 2.6 (GLA linear attention with ALiBi-style slopes), and mlx-lm also remaps
`(bailing_hybrid, BailingMoeV3ForCausalLM)` to `bailing_moe_v3`. At model load,
`omlx/utils/model_loading.py` calls
`omlx.patches.bailing_hybrid.apply_bailing_hybrid_patch()`. That function registers
`omlx/patches/bailing_hybrid/bailing_hybrid_model.py` as
`mlx_lm.models.bailing_hybrid` and removes the remapping. The registered file is
vendored from scaryrawr/mlx-lm `ling-3.0-flash` (d719464f). The patch exists for
three reasons:

- Ling 3.0's linear layers are **KDA** (Kimi delta attention), not GLA.
- Ling 3.0 is trained with a per-layer **SwiGLU clamp**. Without it, HumanEval drops
  17 pp on Ling-3.0-flash. The upstream module does not have the clamp.
- Ling's FP8 and MXFP4 checkpoints need a loader the upstream module lacks.

This package ports that vendored module (`Sources/WispLing/BailingHybrid.swift`):

| Piece | Port |
| --- | --- |
| Layer layout | `is_global = (i+1) % layer_group_size == 0 or i >= ⌊n/g⌋·g`. Global layers are MLA; the rest are KDA. |
| MLA | q LoRA (`q_a_proj` → RMSNorm → `q_b_proj`) and compressed KV (`kv_a_proj_with_mqa` → RMSNorm). Interleaved RoPE runs on the 64 rope dims. The nope part is absorbed through per-head `embed_q` / `unembed_out` (`MultiLinear`, quantized with `quantizedMM`). The rope part is added as a float additive SDPA mask (`pe_scores`, causal positions set to `finfo.min`). The single-token path uses the latent as K/V; the prompt path expands K/V. A head-wise sigmoid output gate (`g_proj`) runs before `dense`. The cache is a `KVCacheSimple` with keys = latent `[B,1,S,512]` and values = `k_pe` `[B,1,S,64]`. |
| KDA | `q/k/v_proj` → depthwise causal conv (k=4) + SiLU, with a conv-history cache. q and k are L2-normalised via `rms_norm` with eps folded in and `D^-0.5` folded into q. Decay is `exp(lower_bound · sigmoid(exp(A_log)·(f + dt_bias)))` (safe gate) and `β = sigmoid(b_proj)`. The gated-delta recurrence uses **per-channel** decay in a Metal kernel. The output is a gated RMSNorm (`o_norm` × sigmoid(`g_proj`)) → `o_proj`. The cache is a 4-slot `ArraysCache` (recurrent state, q/k/v conv history). |
| MoE | Sigmoid router with fp32 `expert_bias` and grouped `noaux_tc` selection (8 groups, top-2-sum group score, keep 4). Top-8 is renormalised, × `routed_scaling_factor`. `SwitchGLU` uses sorted gather above 64 assignments. There is one shared expert. Layer 0 is a dense SwiGLU MLP. |
| SwiGLU clamp | `min(silu(g), L) · clip(u, −L, L)` per layer from `expert_swiglu_limit_list` and `share_expert_swiglu_limit_list`. This checkpoint has `null` for both, so the clamp is unused here but supported. |
| Weights | Mixed per-layer quantization comes from `config.json["quantization"]`, using mlx-swift-lm `BaseConfiguration.perLayerQuantization` + `loadWeights`. Embeddings are 8-bit `QuantizedEmbedding`; `embed_q` / `unembed_out` become `LingQuantizedMultiLinear`. `sanitize` drops layers ≥ `num_hidden_layers`, stacks `experts.N.*`, renames `mlp.gate.*` → `mlp.gate.gate_proj.*` and splits `kv_b_proj` (only needed for unconverted checkpoints). |
| Registration | `LingRuntime.registerModelTypes()` → `LLMTypeRegistry.shared.registerModelType("bailing_hybrid")`, so `LLMModelFactory` / `ModelContainer` can also load it. `BailingHybridModel` conforms to `LLMModel` (`newCache` / `makeCache`, `sanitize`, `loraLayers`). |

### Deviations from Python

- **Single sequence only.** There are no padding masks, batch `lengths`, or pipeline parallelism. The legacy one-slot recurrent-cache migration is gone.
- **The FP8 / MXFP4 conversion in `sanitize` is not ported.** The Wisp checkpoints are MLX affine-quantized (oQ), so it is not needed.
- **KDA kernel.** This package has its own copy of Python mlx-lm's `gated_delta_step_vec` Metal source, with plain `simd_sum` reductions. It does not use mlx-swift-lm's `GatedDelta.swift`, which is scalar-gate only and adds Kahan compensation, so it would round differently. The recurrent state is stored in the kernel's `[Dv, Dk]` layout instead of Python's transposed `[Dk, Dv]`. The math is the same.
- **Sigmoid.** The model uses `lingSigmoid` instead of `MLX.sigmoid`, for bit-identical rounding with Python's precompiled kernel (see Results).
- **Chunked prefill.** `LingRuntime.stream` prefills in 512-token chunks. The Python reference (and `probe`) runs the prompt in one pass; the math is the same but rounding can differ slightly on long prompts.
- **`rms_norm(weight=None)`** is replaced by a ones weight in the input dtype. mlx-swift 0.32.3 has no optional weight; the result is bit-identical.
- **Chat template.** The checkpoint's `chat_template.jinja` is rendered with swift-transformers / swift-jinja and `enable_thinking=false`. If the Jinja engine throws, `LingChatFormat.render` takes over: it is a hand port of the same template for tool-free chats. `ling-parity` reports whether the two agree.

## App façade (`LingRuntime`)

```swift
await LingRuntime.registerModelTypes()          // optional; only needed for LLMModelFactory use
let ling = LingRuntime(modelDirectory: dir)
try await ling.load()                           // local files only, no network
for try await delta in await ling.stream(msgs, maxTokens: 512, temperature: 0.7) { … }
await ling.unload()                             // drops weights, Memory.clearCache()
```

- `requiredFiles(in:)` lists the files that must exist: `config.json`, `tokenizer.json`, `tokenizer_config.json`, `chat_template.jinja`, and `model.safetensors` (or the shards named by the index).
- `Memory.cacheLimit` is set to 256 MB, and the buffer cache is cleared after load, after each generation, and on unload.
- Generation stops at `<|role_end|>` (`eos_token_id`) or `<|endoftext|>`.
- `temperature <= 0` is greedy decoding. Otherwise sampling is temperature + top-k, where k is `top_k` from `generation_config.json` (20).
- `<think>…</think>` is removed from yielded text. This includes a span split across deltas, an unclosed `<think>`, and a stray `</think>`.
- Cancelling the consuming task (or dropping the stream) cancels generation before the next token.

## Parity

### How to run

```bash
cd ios/Packages/WispLing
MODEL=/Users/adijain/Desktop/OMLX_Model_Files/Ling-3.0-tiny-Wisp-V3-merged-oQ4e
WORK=$TMPDIR/wispling                       # DerivedData + SwiftPM checkouts

parity/build.sh macos "$WORK"               # xcodebuild ling-parity (needs the Metal Toolchain, see below)
TOOL="$WORK"/dd/Build/Products/Release/ling-parity
"$TOOL" --selftest                          # think-filter checks; no model needed

# One side at a time. Each refuses to start below 8 GB free+inactive.
parity/run_parity.sh reference     "$MODEL"           # installed oMLX (mlx 0.32.4)  -> parity/reference.jsonl
parity/run_parity.sh swift         "$MODEL" "$TOOL"   # -> parity/swift.jsonl (also stream(), long-prompt, cancel checks)
parity/run_parity.sh swift-teacher "$MODEL" "$TOOL"   # Swift fed the reference tokens -> parity/swift_teacher.jsonl
python3 parity/compare.py parity/reference.jsonl parity/swift.jsonl --teacher parity/swift_teacher.jsonl

# Same check against an MLX-core-matched reference (mlx 0.32.2, the core mlx-swift 0.32.3 vendors).
# The older oMLX engine kept in the Wisp backup bundle has the identical bailing_hybrid patch
# and the identical mlx-lm commit:
E=/Applications/.Wisp-backup-4545581455af768739d3dec7fabb1b06.app/Contents/Resources/engine
LING_REF_PY=$E/py/bin/python3 LING_REF_OMLX=$E LING_REF_SITE=$E/sp LING_REF_OUT=reference_mlx0322.jsonl \
    parity/run_parity.sh reference "$MODEL"
LING_REF_OUT=reference_mlx0322.jsonl LING_SWIFT_OUT=swift_teacher_mlx0322.jsonl \
    parity/run_parity.sh swift-teacher "$MODEL" "$TOOL"
python3 parity/compare.py parity/reference_mlx0322.jsonl parity/swift.jsonl --teacher parity/swift_teacher_mlx0322.jsonl
```

Both sides run the same procedure:

1. Render the template (thinking off, generation prompt added).
2. Run one forward pass over the whole prompt with a fresh cache.
3. Report the top 5 (id, logit) of the last position.
4. Decode 32 greedy tokens one at a time, with no EOS stop, so text after `<|role_end|>` is compared too.

Debug helpers:

- `ling-parity --trace DIR` with `parity/trace.py dump|diff` compares per-layer activations.
- `ling-parity --optest FILE` with `parity/trace.py optest FILE` compares individual MLX ops.

### Results (M5 Pro, 2026-10-21)

The prompts are `greeting` (27 prompt tokens), `system_math` (47) and `multi_turn` (60). The prompt token ids are identical on both sides, and the Jinja rendering equals the hand-ported fallback.

**Against mlx 0.32.2 (the same MLX core mlx-swift uses): bit-identical.**

| prompt | top-5 ids | max \|Δlogit\| (top-5) | 32 greedy ids | teacher-forced top-1 |
| --- | --- | --- | --- | --- |
| greeting | identical | 0.0000 | identical | 32/32 |
| system_math | identical | 0.0000 | identical | 32/32 |
| multi_turn | identical | 0.0000 | identical | 32/32 |

At every one of the 96 teacher-forced positions, the top-5 logits match exactly (max |Δ| 0.0000). `compare.py` returns PARITY: PASS.

**Against the installed oMLX (mlx 0.32.4): identical up to every end-of-turn token. It fails the strict 32-token criterion only after `<|role_end|>`.**

| prompt | top-5 ids | max \|Δlogit\| (top-5) | 32 greedy ids |
| --- | --- | --- | --- |
| greeting | identical | 0.125 | identical (32/32) |
| system_math | ids 678 and 16 swap (tied in Swift at 12.125) | 0.375 | identical through `391<\|role_end\|><\|endoftext\|>…`; diverges at position 7, after EOS (position 3) |
| multi_turn | identical | 0.25 | identical through the whole answer and its `<\|role_end\|>` (position 19); diverges at position 21, after EOS |

With teacher forcing, top-1 agrees at 93/96 positions. All three flips are after EOS, where the reference's top-1/top-2 margin was 1.0, 0.0625 and 0.125. Before EOS, logits differ by at most about 0.56; bf16's step size at logit ≈ 16–32 is 0.125.

**Why exact identity with 0.32.4 is impossible.** MLX core changed between 0.32.2 and 0.32.4:

- `quantized.h` changed. On identical inputs, 4-bit `quantized_matmul` gives 46% (qmm) and 66% (qmv) of bf16 outputs differing, by up to 1.6e-2.
- `Sigmoid` switched to `metal::precise::exp`.

mlx-swift 0.32.3 is the newest release and vendors core 0.32.2, so a Swift build cannot reproduce 0.32.4 rounding. Against 0.32.2 every one of these ops is bit-identical, as shown by `--optest`.

One extra fix was needed to reach bit-identity. mlx-swift always builds MLX in JIT mode, so its stock `sigmoid` kernel is compiled at run time and rounds about 1/3 of fp32 inputs 1 ulp differently from the ahead-of-time metallib Python ships. `lingSigmoid` writes the same formula as ops that are bit-identical in both builds. The model uses it for every non-compiled sigmoid: router scores, KDA decay, β, the output gates, and o_norm.

### Memory and speed (macOS, release build, `/usr/bin/time -l`)

| | Swift (`ling-parity`) | Python reference (oMLX) |
| --- | --- | --- |
| MLX active memory after load | 4.64 GB | — |
| Peak footprint, load + 3 × 32-token generation (teacher run) | 5.12 GB | 5.26–5.38 GB |
| Peak footprint, same plus a 1,234-token chunked-prefill stream and the cancel test | 5.56 GB | — |
| Max RSS (varies with how Metal memory is attributed) | 1.6–4.1 GB | 1.6–3.5 GB |
| MLX peak memory (`Memory.peakMemory`) | 5.24 GB | 4.80–4.84 GB |
| Load time (warm page cache) | 0.9–1.1 s | 1.0–2.5 s |
| Decode, warm (single sequence, greedy) | 110–128 tok/s | 115–131 tok/s |
| Prefill, warm (47–60 tokens, one pass) | 1,020–1,170 tok/s | — |
| Long prompt via `stream()` (1,234 tokens, 512-token chunks, + 24 tokens) | 0.3–1.4 s total | — |
| Cancellation | stream stops at the next token; actor free 3 ms after the consumer breaks | — |

The first generation in a fresh process is much slower: 11 tok/s prefill and 28 tok/s decode. That is MLX JIT kernel compilation. The OS Metal shader cache makes later processes fast, and later prompts in the same process run at full speed.

## Build status and open risks

- **The Metal Toolchain is not installed on this Mac.** `xcodebuild -showComponent MetalToolchain` reports `uninstalled`, so `xcodebuild` (and `swift build`) fail compiling MLX's `.metal` files, and no `default.metallib` was produced. To still validate:
  - Compile-only: Swift and C++ for the macOS tool and for `generic/platform=iOS` were built with a scratch-only patch of the mlx-swift checkout that skips the shaders. The iOS build of scheme `WispLing` (`CODE_SIGNING_ALLOWED=NO`, arm64-apple-ios17) reported `BUILD SUCCEEDED`. The package itself is unmodified.
  - Mac runs: the Swift parity runs loaded the AOT `mlx.metallib` from the mlx 0.32.2 Python wheel, placed next to the binary. That is the same MLX core version mlx-swift would compile.
  - Before the app can ship, install the toolchain with `xcodebuild -downloadComponent MetalToolchain`. This needs the owner's approval. Then rerun `parity/build.sh macos|ios`. An app built without `default.metallib` fails at the first GPU op.
- **iPhone memory is the main risk.** The checkpoint is 4.64 GB resident. Footprint was about 5.1 GB for a short chat and about 5.6 GB with a 1.2k-token prompt.
  - On 8 GB iPhones (15 Pro / 16 / 16 Pro), this needs the `com.apple.developer.kernel.increased-memory-limit` entitlement. Even with it, headroom is roughly 0.5–1 GB. Without it the app will be jetsam-killed.
  - 12 GB devices (iPhone 17 Pro / Air) are comfortable.
  - Recommended: check `os_proc_available_memory()` before `load()`, keep `prefillChunk` at 512 or lower, cap the context, and unload on memory warnings.
  - The KV cost per token is small: MLA latent 6 × 576 bf16 values (about 7 KB/token), and KDA state is a fixed 18 × 1 MB.
- **First-use latency.** JIT kernel compilation makes the first turn slow. Run a 1-token warm-up after `load()`.
- **Tokenizer.** The tokenizer is swift-transformers 1.3.5 and the template engine is swift-jinja 2.5.2. Prompt ids matched Python for the three prompts, but tool-calling templates (the `tools` branch) were not exercised. The hand-ported fallback covers tool-free chats only.
- **Sampling is temperature + top-k only.** `top_p` from `generation_config.json` is not applied.
