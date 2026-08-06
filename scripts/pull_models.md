# Adding models to oMLX

oMLX loads models from the dirs in `~/.omlx/settings.json` → `model.model_dirs`
(currently `~/Desktop/OMLX_Model_Files`). Add a model by downloading its MLX build
into that directory (or via the oMLX menu-bar app's model manager).

## Already installed
- `gemma-4-e4b-it-4bit` — small/fast (router + trivial)
- `DeepSeek-R1-Distill-Qwen-14B-4bit` — reasoning
- `gpt-oss-20b-MXFP4-Q8` — general + coding (current `coding` role)
- `gemma-4-26b-a4b-it-4bit` — MoE, fast (current `reasoning_max`)
- `gemma-4-31b-it-4bit` — large dense (current `coding_max`)

## Recommended to add — dedicated coding specialist (quality-first goal)
No coding-specialized model is installed yet. To get the dedicated coding tier from
the plan, pull a Qwen2.5-Coder MLX build into the model dir, e.g. via huggingface-cli:

```bash
# primary coding tier (~9 GB, coexists)
huggingface-cli download mlx-community/Qwen2.5-Coder-14B-Instruct-4bit \
  --local-dir ~/Desktop/OMLX_Model_Files/mlx-community/Qwen2.5-Coder-14B-Instruct-4bit

# opt-in max coding (~14 GB at 3-bit/DWQ, load exclusively) — pick a 3-bit/DWQ build
# to leave headroom under the 18 GB wired cap; 4-bit (~18 GB) nearly fills it.
```

After downloading, restart oMLX (or its server) and point the `coding` /
`coding_max` roles in `service/config/models.yaml` at the new ids.
