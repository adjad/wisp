# LFM2.5 chat dataset v1

This directory contains a deterministic conversion of `../ling-dataset-v3.1-20261013/` for LFM2.5-24B-A2B fine-tuning with MLX-LM. No model or tokenizer was loaded, and no training was run.

## Counts

| Split | New V3.1 step rows | V3 replay rows | V2 replay rows | Total |
|---|---:|---:|---:|---:|
| train | 1,205 | 621 | 600 | **2,426** |
| valid | 143 | 76 | 60 | **279** |
| Total | 1,348 | 697 | 660 | **2,705** |

The 410 new training conversations and 49 new validation conversations expand into one row per assistant step. V3 replay samples whole V3 conversations, so its row count is near, but not exactly, the 600/60 target. V2 replay rows are kept in their source splits.

## Row and message format

Each JSONL row has `messages` in MLX-LM's chat dataset format, plus `id` and `conversation_id` metadata for traceability. MLX-LM's chat dataset passes the messages to the tokenizer chat template. System, user, assistant, and tool messages are retained; tool results use `role: "tool"` and include the tool `name`.

The source trains only the final assistant message of each step row. The converter keeps that target as the final assistant turn and drops source-only `training` flags. Set MLX-LM's `mask_prompt=True` when training so earlier assistant messages in the row remain context; the default unmasked chat loss would train on those history turns too.

The source's three system messages are combined into one system message. The exact JSON Schema tool definitions offered at that assistant step are appended there, so the tool list travels in the system prompt. A top-level `tools` field is not used. Each tool call is encoded in assistant content as:

```text
<think>
I should use the listed tool definitions and conversation to form this call.
</think>
<|tool_call_start|>[lookup_contact(name='Sam')]<|tool_call_end|>
```

Multiple calls in one assistant turn are a Python list of calls in source order. Arguments are serialized as Python literals (`None`, `True`, `False`, strings, numbers, lists, and dictionaries). `convert.py` parses every emitted call with Python's AST and checks that ordered tool names and argument objects match the source. Tool-call IDs are converted to tool names on the paired `tool` messages.

Plain assistant answers and assistant tool-call turns each receive a short synthesized `<think>...</think>` block. The text only states how to use the conversation, listed tool definitions, or tool results; it adds no claims about the user or the world. The two fixed forms are “I should use the listed tool definitions and conversation to form this call.” and “I should answer using only the conversation context.”

## Template availability and format decision

The read-only search under `$HOME/.omlx` and `$HOME/.cache/huggingface` found no `tokenizer_config.json` or `chat_template.jinja` for `LFM2.5-24B-A2B`. The documented fallback is the model's ChatML-like message format, its always-on think block, Pythonic function-call list between `<|tool_call_start|>` / `<|tool_call_end|>`, and tool results in the `tool` role. The tool schemas are listed in the system prompt as JSON Schema.

Because the local authoritative template is absent, exact tokenizer-template rendering and row round-trip could not be tested. This conversion follows the documented format and validates the tool-call serialization and source pairing. The exact whitespace, thinking-block handling, and schema presentation expected by the MLX checkpoint remain unverified until its tokenizer template is available. References: [LFM2.5-24B-A2B model card](https://huggingface.co/LiquidAI/LFM2.5-24B-A2B) and [MLX-LM chat dataset loader](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/tuner/datasets.py).

## Rebuild and validate

Run from the repository root:

```sh
python3 training/lfm-dataset-v1-20261014/convert.py
```

The script copies the source generator inputs to a temporary directory, regenerates V3 replay rows there, runs the V3.1 generator, and writes `train.jsonl` and `valid.jsonl` here. The checked-in V2 replay files are copied as source inputs. It does not modify the source directory. The script is standard-library-only and fails if split counts change from 2,426 / 279.

For every user message it reuses `generate.max_heldout_overlap` and `generate.OVERLAP_LIMIT` from the source generator. The source exclusion limit is 0.4; any prompt at or above it stops conversion.

Focused converter tests:

```sh
HOME="$PWD/.codex-home" WISP_HOME="$PWD/.codex-home/.moe" \
  /Users/adijain/Desktop/MOE_Project/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_lfm_dataset_convert.py
```

## Validation results

Recorded after conversion and focused tests:

- Source rows: train **2,426**, valid **279** (2,705 total). Split composition is train 1,205 V3.1 + 621 V3 replay + 600 V2 replay; valid 143 + 76 + 60. IDs are unique within both splits.
- Tool calls: every serialized call parses back to the original ordered function name and JSON argument object.
- Held-out prompts: every converted user message is checked using the source exclusion algorithm at the 0.4 threshold; maximum Jaccard was **0.333** for train and **0.000** for valid.
- Reproducibility: a second full conversion produced byte-identical JSONL. SHA-256: train `bdde53e86076bd1cb4541545da8fc81a33a8b6aff9fdf0eb48a1e87b7ae8b35f`; valid `54d30cd75c8b05be0db209dfa53c757dcbadc861e59c07a0b95dbfbd4c84fb58`.
- Template rendering: **not run**; no matching local cached template was found.
- Focused pytest: **3 passed** in 0.10s with the command above.
