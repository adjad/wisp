# LFM2.5 chat dataset v1

This directory contains a deterministic conversion of `../ling-dataset-v3.1-20261013/` for LFM2.5-24B-A2B fine-tuning with MLX-LM. Rows are rendered through the model's real chat template (tokenizer files only; no model weights were loaded, and no training was run).

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

The source's three system messages are combined into one system message. Tool definitions offered at that assistant step are stored in the row's top-level `tools` field; MLX-LM's `ChatDataset` forwards `row["tools"]` to `apply_chat_template(tools=...)`, and the real template appends them to the system prompt as `List of tools: [<JSON>, ...]`. Tool schemas are therefore not written into the system text.

Assistant turns are structured, not pre-rendered strings:

- `reasoning_content` holds a short synthesized thinking text. The template renders it as `<think>TEXT</think>` directly after the role header, which matches the generation prompt's trailing `<think>`. It renders only for assistant turns after the last user message (earlier turns drop it).
- `tool_calls` keeps the source calls with `arguments` as a dict. The template renders them as `<|tool_call_start|>[name(arg=value, ...)]<|tool_call_end|>` after any assistant text. Multiple calls are one list in source order.
- Tool results use `role: "tool"`; the template renders the content only (the `name` field is retained for traceability and ignored).

The template's argument encoding differs from a pure Python literal: a top-level string is single-quoted (escaping `\`, `'`, newline and carriage return); a top-level `None`/`True`/`False`/number is Python `str()`; lists and dicts are JSON (`true`, `null`, double-quoted strings). `convert.py` mirrors this, parses the mirrored call block back (accepting JSON `true`/`false`/`null`), and checks ordered tool names and argument objects against the source. Tool-call IDs are converted to tool names on the paired `tool` messages.

Plain answers and tool-call turns each receive one of two fixed synthesized thinking texts: "I should use the listed tool definitions and conversation to form this call." and "I should answer using only the conversation context." They state how to use the conversation, tool definitions, or tool results and add no claims about the user or the world. Source assistant text containing `<think>`, `</think>`, tool-call markers, or the template's `CONTINUE_FINAL_MESSAGE_TAG` is rejected.

## Template verification

The real files are at `/Users/adijain/Desktop/OMLX_Model_Files/LiquidAI/LFM2.5-24B-A2B-MLX-4bit/` (`chat_template.jinja`, `tokenizer_config.json`, `tokenizer.json`). An earlier revision of this README wrongly said no template was available and emitted pre-rendered text with schemas in the system prompt; that diverged from the template (schemas in the wrong place, `<think>` duplicated in content, calls placed before text, nested values in Python rather than JSON form, and a generation-prompt misalignment under `mask_prompt`), and was corrected.

Verified with `transformers` 5.15.0 `AutoTokenizer.from_pretrained(dir, local_files_only=True)` over all 2,705 rows, calling `apply_chat_template` the way `mlx_lm.tuner.datasets.ChatDataset.process` does (with `tools=row.get("tools")`):

- For every row, the render of `messages[:-1]` with `add_generation_prompt=True` (ending in `<|im_start|>assistant\n<think>`) is an exact string and token-id prefix of the full render. The `mask_prompt=True` offset therefore lands exactly at the start of the final thinking text, and the trained span is `THINK</think>[content][tool call block]<|im_end|>\n`.
- For every row whose final assistant turn has tool calls, the converter's mirrored call block appears verbatim in the real render.
- Special tokens resolve to single ids: `<think>` 124901, `</think>` 124902, `<|tool_call_start|>` 124905, `<|tool_call_end|>` 124906, `<|im_end|>` 124900.
- 660 rows (V2 replay) carry no `tools`; the other 2,045 do.

Not verifiable without loading the model: whether the checkpoint's thinking style after `<think>` suits these one-line synthetic thoughts, and the fine-tuned model's tool-call behavior. The trailing newline after `<|im_end|>` is part of the trained span, as in the template.

## Rebuild and validate

Run from the repository root:

```sh
/Users/adijain/Desktop/MOE_Project/.venv/bin/python training/lfm-dataset-v1-20261014/convert.py
```

The script copies the source generator inputs to a temporary directory, regenerates V3 replay rows there, runs the V3.1 generator, and writes `train.jsonl` and `valid.jsonl` here. The checked-in V2 replay files are copied as source inputs. It does not modify the source directory. `train.jsonl` and `valid.jsonl` are gitignored generated outputs. The script is standard-library-only and fails if split counts change from 2,426 / 279.

For every user message it reuses `generate.max_heldout_overlap` and `generate.OVERLAP_LIMIT` from the source generator. The source exclusion limit is 0.4; any prompt at or above it stops conversion.

Focused converter tests (the real-template test runs only when the tokenizer files above exist and `transformers` is importable; otherwise it is skipped):

```sh
HOME="$PWD/.codex-home" WISP_HOME="$PWD/.codex-home/.moe" \
  /Users/adijain/Desktop/MOE_Project/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_lfm_dataset_convert.py
```

## Validation results

Recorded after conversion and focused tests:

- Source rows: train **2,426**, valid **279** (2,705 total). Split composition is train 1,205 V3.1 + 621 V3 replay + 600 V2 replay; valid 143 + 76 + 60. IDs are unique within both splits.
- Tool calls: every mirrored call block parses back to the original ordered function name and JSON argument object, and appears verbatim in the real template render.
- Held-out prompts: every converted user message is checked using the source exclusion algorithm at the 0.4 threshold; maximum Jaccard was **0.333** for train and **0.000** for valid.
- Reproducibility: two full conversions produced byte-identical JSONL. SHA-256: train `513d5358b5366e2b3d0aa0985763e3bfe332b7c616d1cf88b0cf1b797caf7ab4`; valid `56f4b1a20ce95e50b91c273e537cf0a8a4791dfa458c6f29196f21b1061e9a74`.
- Template rendering: **run on all 2,705 rows**, prefix and mask alignment as described above, 0 mismatches.
- Focused pytest: **6 passed** (including the real-template test) with the command above.
