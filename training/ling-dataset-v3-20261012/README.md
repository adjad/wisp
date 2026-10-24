# Ling dataset V3: Wisp model-led tool calling

Synthetic supervised data that teaches Ling (base: `Ling-3.0-tiny-Wisp-V2-merged`) the protocol Wisp runs in
MODEL-LED mode, protocol S (Ling chooses from ALL tools itself; no Wisp shortlist): load schemas first (by calling a tool name through its name-only stub, or via `get_tool_schemas`), then use only the loaded tools, chain in the right order, stop when the question
is answered, handle errors and denials honestly, and ask when the request is ambiguous. It is generated from the
failure classes of the Ling V2 model-led eval (direct real-tool calls with no schema load, loops, chains that never
finish, weak send/draft paraphrases, silence on errors). Nothing here executes a Wisp tool, contacts a network or
loads a model.

This is a different task from V2. V2 trained a JSON intent router and fixture-grounded summaries; V3 trains native
`tool_calls` behaviour. Train V3 on top of V2 (start from the V2-merged weights/adapter), not from scratch, and do
not mix the V2 rows into the same run without a deliberate mixing plan.

## Files

| file | purpose |
|---|---|
| `train.jsonl`, `dev.jsonl` | one row per ASSISTANT STEP (900 / 100 conversations) |
| `train.provenance.jsonl`, `dev.provenance.jsonl` | one record per conversation: category, family_id, scenario_id, template_id, gold call sequence, targeted failure class, reference date, slots, held-out overlap score |
| `conversations.jsonl` | one full conversation per line (both splits) with `tools_per_round`, for inspection |
| `generate.py` | deterministic generator (seed 20261012), standard library only; regenerates every output byte-identically |
| `validate.py`, `validation.json` | independent validator and its last output |
| `assets/` | `tools.json` (165 real schemas + the step-0 discovery schema), `system_parts.json` (captured system prompts, catalog, tool-scoped rule paragraphs), `heldout_shingles.json` (hashed n-grams of the eval prompts; the prompts themselves are not stored) |
| `build_assets.py` | optional: how `assets/` was derived from the read-only eval artifacts |
| `preflight.py`, `preflight.json` | real-tokenizer length + intended-label-mask check, and its saved result |
| `check_labels.py` | audits Axolotl's prepared arrow labels against the intended mask |
| `axolotl-a6000.yaml` | LoRA config for one A6000 48GB |
| `package.py`, `upload-zip.json` | builds the upload ZIP; its sha256/size |
| `DATA_CARD.md`, `stats.json` | counts, lengths, failure classes, limitations (generated) |

## Row format

```
{"id": "v3-<category>-<split>-NNNN-sK", "conversation_id": "v3-<category>-<split>-NNNN",
 "tools": [<schemas offered at THIS step>],
 "messages": [system x3, (history user/assistant)*, user, (assistant tool_calls, tool)*, <target assistant>]}
```

- Because the offered tools change each round, each assistant step is its own row (`sK` = step index). Round 0 offers
  `get_tool_schemas` plus a name-only stub (`{"name": <tool>, "parameters": {"type": "object"}}`, no description or
  properties) for every admitted catalog tool (~160). A call to a stub is NOT executed: the host treats it as an implicit
  discovery and returns the standard receipt as that call's result (several stubs in one message = one discovery; families in
  call order). Stub arguments are `{}` or a partial best guess. After a receipt the offered set is the loaded REAL schemas
  plus, while fewer than two discoveries (explicit or implicit) have happened, `get_tool_schemas` and stubs for the rest;
  the assistant must then re-issue the call with full arguments and never answer from the receipt. About 70% of
  conversations open with a stub call (typically 1-3 stubs for a chain), about 30% with `get_tool_schemas`.
  SP variant (Wisp 1.3.2): in ~30% of tool-using conversations round 0 also carries 6-12 preloaded REAL schemas (no discovery
  attempt consumed; counted toward the 24). Half contain the needed tool, called directly with full args; half are decoys, ignored
  while the right tool is called via its stub. See `preload`, `preload_contains_needed`, `protocol_variant` in provenance.
  The catalog preamble uses the host's new discovery wording.
- Only the LAST message of a row is the target (`training: true`). Every earlier assistant message (history or
  in-turn) is `training: false`. System, user and tool messages carry no flag.
- Tool-call turns have `content: ""` and OpenAI-style `tool_calls` (id, `type: function`, `function.arguments` as a
  JSON object); tool results are `role: tool` with the matching `tool_call_id`.
- The three system messages are the captured model-led prompts (identity block, capability catalog, context note).
  Message 0 is rebuilt per step with the tool-specific rule paragraphs that the loaded tools trigger. Identity is
  synthetic: Test User, test.user@example.invalid; all other people use `.invalid` addresses and `+1 555 01xx` numbers.
- Send bodies are faithful: they contain only what the user asked to say (quoted text verbatim, otherwise converted to the recipient's point of view; emails use only the `Hi <First>,` / `Test User` frame), and the final answer quotes the exact body sent. Do not add greetings or pleasantries when extending this data.
- Rejected discovery calls (tools outside the selected families) appear only as context with the host's rejection
  text; they are never targets. In `conversations.jsonl`, `training: true` marks every message that is the target of
  its own row.

## How to train

1. Use the same pinned Ling revision, chat template and thinking-disabled generation settings as V2. Pass each row's
   `tools` to the template's tool argument and render `messages` as given.
2. Loss on the final assistant message of each row only (the discovery call, the real call(s), or the answer).
   Everything before it is context. Run your preprocessing and inspect the prepared labels BEFORE training: the
   trainer must mask every earlier assistant turn, including earlier tool-call turns. The JSON flags alone are not
   proof that a trainer honours them.
3. Rows are ~8,607 estimated tokens on average (chars/4, no tokenizer run) because each carries the full system prompt, the offered schemas and ~160 stubs; targets are ~50 tokens. 4,387 model-led train rows are about 40M tokens per epoch. Check the
   real maximum length against your context cap before launching. Measured with the real tokenizer (`preflight.json`): median 8,840, max 14,521 tokens (train); see "Train on an A6000".
4. `dev.jsonl` is for development only. The Ling eval corpus stays held out; evaluate with the model-led harness, not
   with these rows. No quality improvement has been measured.

## Train on an A6000

One NVIDIA A6000 48GB, LoRA, `sequence_len` 16384. Real token counts (Ling tokenizer + template, tools rendered, thinking off; see `preflight.json`): train 4,387 model-led rows, max 14,521 / p95 12,490 / median 8,840 (39.9M tokens per epoch) plus 600 V2 replay rows (max 737, 0.34M tokens); dev 474 + 60 rows, max 13,541; no row exceeds 16,384. Labeled tokens are ~151k per epoch.

1. Upload `ling-train-dev-v3-20261012.zip`. Verify `sha256sum` against `upload-zip.json`, then extract into a NEW directory (keep V2 data and adapters untouched).
2. `python preflight.py --local-files-only --tokenizer-dir /path/to/Ling-3.0-tiny-Wisp-V2-merged` (tokenizer + template only; no weights). Must print `"status": "PASS"`. Optionally `python validate.py` for the CPU dataset checks.
3. Edit `base_model` in `axolotl-a6000.yaml` (V2 merged weights; V3 trains on top of V2). Run `axolotl preprocess axolotl-a6000.yaml` (writes `./last_run_prepared`).
4. **Gate:** `python check_labels.py ./last_run_prepared --tokenizer-dir /path/to/Ling-3.0-tiny-Wisp-V2-merged --local-files-only` (add `--sample 200` for a quick pass). It matches each prepared row to its source row by exact `input_ids`, then requires labels != -100 only on the final assistant span (tool-call serialization + `<|role_end|>`). Any FAIL (earlier turns trained, target masked, ids not matching the template/tools/thinking setting) means do not train; fix the config. The YAML's masking keys are assumptions about this Axolotl version, and this gate is the proof. The template needs tool-call `arguments` as dicts, and the shipped rows store them as JSON objects, so no parsing is required of Axolotl.
5. Timed smoke run: add `max_steps: 20` and `evals_per_epoch: 0` (separate `output_dir`), run `axolotl train axolotl-a6000.yaml`. tokens/sec = (20 x 16 rows x ~9,000 tokens) / seconds; record peak memory with `nvidia-smi`. Full epoch = 312 optimizer steps (~40M tokens), so estimate time as smoke time x 15.6.
6. Full run: remove the smoke overrides and run `axolotl train axolotl-a6000.yaml`; the adapter lands in `ling-v3-lora-out`.

Expected memory (estimate, unmeasured): ~16GB bf16 weights + LoRA/optimizer state (small, rank 16 attention only) + activations with gradient checkpointing at 9-14k tokens; roughly 20-30GB if chunked cross-entropy works, noticeably more without it (full logits at 16k x 157k vocab are ~5GB bf16 and ~10GB fp32). Caveats: the HF MoE implementation of this custom model (`trust_remote_code`) may loop over experts and be SLOW, so judge feasibility from the smoke run, not from FLOPs; flash attention and the cut-cross-entropy/Liger plugins may not support `bailing_hybrid` (fallbacks are in the YAML comments). Nothing here has been run against Axolotl or the model weights.

## Reproduce and validate

```
python3 generate.py                     # rewrites all data files + DATA_CARD.md + stats.json
python3 validate.py --check-repro       # full checks incl. byte-identical regeneration; writes validation.json
```

`validate.py` replays every conversation: discovery receipts against the host's cumulative expansion rules, the
offered set per step (loaded schemas + stubs + `get_tool_schemas` while budget remains), JSON-schema validity of every call against the schema offered at that step (types, required,
enums, no unknown keys), id pairing, no repeated identical call, protocol order, final answer present, row-prefix
consistency, family split integrity, held-out overlap, exact category counts, authority cues for sends/destructive
tools, honest handling of denials and errors, slot consistency (weekday/today/tomorrow/clock time vs arguments),
synthetic-only PII and tool-result length.

See `DATA_CARD.md` for distribution and limitations.


## Expansion (V3, 13 Oct 2026)

1,600 conversations (1,444 train / 156 dev; was 1,000) plus 660 V2 replay rows. New add-on family modules, each loaded by `generate.py`:
`ext_chains.py` (multi-step read->act chains, conditional acts, dead ends), `ext_followups.py` (history references, corrections,
answers from history with no tool), `ext_chat.py` (no-tool chat and tool-word temptations), `ext_messy.py` (long, noisy, truncated
and partial tool results, up to 1,199 chars, using Wisp's real truncation marker), `ext_guard.py` (Wisp 1.3.2's repeated-read guard:
the identical repeat is context only, never a target; the target is the recovery). `v2_replay.py` selects 600 train / 60 dev rows
from the V2 dataset (stratified by competency, held-out screened, unchanged messages, `tools: []`), appended last to train/dev.jsonl
so V3 keeps V2's read-intent routing skill. Category mix: outbound 18%, compound 22%, stop 10%, reads 11%, writes 8%, errors 7%,
clarify 5%, chat 8%, follow-ups 7%, repeated-read guard 4%.
