# Ling router training feasibility — October 5, 2026

Read-only literature and static metadata review by a separate GPT-6.1 Sol agent, high reasoning. No training, model loading, allocation, inference, environment changes, or sealed-corpus access occurred. There is no measured accuracy improvement from training.

## Decision

The first experiment should be a small attention-only MLX QLoRA SFT **feasibility smoke**, after checkpoint compatibility and an exclusive resource window are established. Verified rejection sampling followed by SFT is a practical next experiment. GRPO remains a later conditional experiment because a compatible trainer and reliable reward are prerequisites. The exact structured scoring task makes verifiable reinforcement learning relevant, but that does not establish a runnable local recipe.

## Verified prerequisites and blockers

- Installed project runtime: Python 3.14, MLX 0.32.0, MLX-LM 0.31.3. Its model mapping lacks Ling3 `bailing_moe_v3`. Do not modify the production environment for training.
- Installed `Ling-3.0-tiny-oQ6e` uses `bailing_hybrid` / `BailingMoeV3ForCausalLM`: 24 layers, 18 KDA and 6 MLA, 128 experts, eight active. It has default affine 6-bit quantization with 8-bit overrides. Index metadata reports 6,611,004,425 tensor bytes (about 6.16 GiB); this is not training peak memory.
- Upstream MLX-LM commit `5cfec4cb39deba54210b3ff4d86f2337c7bc10b5` includes the architecture remap and a training-specific KDA path. Source support does not prove backward compatibility or adapter reload for this installed checkpoint.
- The installed index has quantized `model.layers.N.mlp.gate.gate_proj.{weight,scales,biases}`. Upstream `BailingGate` expects raw `gate.weight`; its sanitizer does not translate this namespace/quantization. Static inference: upgrading alone will not establish strict checkpoint compatibility. Any conversion requires its own bounded implementation, tests, and base-inference equivalence evidence.
- Custom MLA `embed_q` / `unembed_out` projections do not use the ordinary LoRA conversion path. Do not copy an Axolotl `kv_b_proj` adapter target into MLX.
- The official Axolotl tiny LoRA example reports 25.0 GiB reserved / 29.0 GiB device on H100 at sequence 4096, batch one. This does not demonstrate fit on this 24 GiB Mac or for its quantized model.
- Shared model measurements remain blocked on explicit resource coordination with the existing Wisp 1.2 performance owner. No model unload/reload or production setting changes are authorized by this implementation.

## Bounded first recipe, not a verified configuration

Use an isolated environment pinned to a reviewed runtime revision and a compatible immutable base. Start with rank 8, MLX scale 20.0, dropout zero, learning rate 1e-5, batch one, accumulation one, last four layers, gradient checkpointing, and about ten newly authored synthetic demonstrations. A 256-token initial limit is usable only if the complete production prompt and completion fit; otherwise increase under a measured memory bound, never truncate away the contract. Save a checkpoint after the first update. MLX scale is a direct adapter multiplier, not Axolotl lora_alpha.

Explicit ordinary-linear adapter keys to qualify: `attention.q_proj`, `attention.k_proj`, `attention.v_proj`, `attention.o_proj`, `attention.q_a_proj`, `attention.q_b_proj`, `attention.kv_a_proj_with_mqa`, `attention.dense`. Freeze experts, router, embeddings, output head, convolutions, and custom MLA projections. Automatic target selection can include expert switch-linear modules; use explicit targets and verify the actual trainable names.

Required smoke evidence: equivalent base inference, finite loss/gradients, a nonzero adapter update, frozen base weights, saved adapter/configuration, process termination, successful reload through the actual benchmark/production loader, and nonzero exact correctness on independently authored smoke holdout items. A successful trainer-only reload is insufficient. This gate establishes feasibility, not an accuracy gain.

After the smoke gate, compare original and adapted weights on the same frozen code candidate. Keep source/operation/effective arguments, exclusions/effects, abstention, latency and peak memory separate. Use fresh training and development families from the specification; never use either sealed evaluation corpus or failure-derived variants. Do not run the existing personal-session-harvesting `scripts/build_lora_dataset.py`.

For longer runs, checkpoint periodically, evaluate a separate development subset, stop after two non-improving checkpoints, and preserve the best checkpoint. No tracker or upload is needed. RFT can sample up to four completions serially per fresh training prompt and keep only exact verified successes; zero accepted examples is an abort signal. GRPO requires a separately qualified trainer/log-probability/backward path and memory budget; a PyTorch/PEFT example is not a drop-in MLX implementation.

## Primary references

- [Pinned Ling3 MLX implementation](https://github.com/ml-explore/mlx-lm/blob/5cfec4cb39deba54210b3ff4d86f2337c7bc10b5/mlx_lm/models/bailing_moe_v3.py)
- [Pinned architecture mapping](https://github.com/ml-explore/mlx-lm/blob/5cfec4cb39deba54210b3ff4d86f2337c7bc10b5/mlx_lm/utils.py)
- [MLX LoRA documentation](https://github.com/ml-explore/mlx-lm/blob/5cfec4cb39deba54210b3ff4d86f2337c7bc10b5/mlx_lm/LORA.md)
- [MLX adapter conversion](https://github.com/ml-explore/mlx-lm/blob/5cfec4cb39deba54210b3ff4d86f2337c7bc10b5/mlx_lm/tuner/utils.py)
- [Axolotl official Ling3 training documentation](https://docs.axolotl.ai/docs/models/ling3.html)
- [Official tiny LoRA recipe](https://github.com/axolotl-ai-cloud/axolotl/blob/main/examples/ling3/ling-3.0-tiny-lora.yaml)
- [TRL GRPO documentation](https://huggingface.co/docs/trl/main/en/grpo_trainer)

The evo finetuning skill's literature prerequisite was completed as a read-only ideator brief, with results returned in memory because no evo run or training write scope exists. Actual loader behavior, backward pass, memory/time, adapter reload, and all training uplift remain unverified.
