# Ling dataset V3: Wisp model-led tool calling

Synthetic supervised data that teaches Ling (base: `Ling-3.0-tiny-Wisp-V2-merged`) the protocol Wisp runs in
MODEL-LED mode: discover first, then use only the loaded tools, chain in the right order, stop when the question
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
| `DATA_CARD.md`, `stats.json` | counts, lengths, failure classes, limitations (generated) |

## Row format

```
{"id": "v3-<category>-<split>-NNNN-sK", "conversation_id": "v3-<category>-<split>-NNNN",
 "tools": [<schemas offered at THIS step>],
 "messages": [system x3, (history user/assistant)*, user, (assistant tool_calls, tool)*, <target assistant>]}
```

- Because the offered tools change each round, each assistant step is its own row (`sK` = step index). Round 0 offers
  only `get_tool_schemas`; after a successful discovery the offered set is the loaded REAL schemas plus
  `get_tool_schemas` while fewer than two discovery calls have been made.
- Only the LAST message of a row is the target (`training: true`). Every earlier assistant message (history or
  in-turn) is `training: false`. System, user and tool messages carry no flag.
- Tool-call turns have `content: ""` and OpenAI-style `tool_calls` (id, `type: function`, `function.arguments` as a
  JSON string); tool results are `role: tool` with the matching `tool_call_id`.
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
3. Rows are ~4,000 estimated tokens (chars/4, no tokenizer run) because each carries the full system prompt and the
   offered schemas; targets are ~50 tokens. 2,914 train rows are about 11.6M estimated tokens per epoch. Check the
   real maximum length against your context cap before launching.
4. `dev.jsonl` is for development only. The Ling eval corpus stays held out; evaluate with the model-led harness, not
   with these rows. No quality improvement has been measured.

## Reproduce and validate

```
python3 generate.py                     # rewrites all data files + DATA_CARD.md + stats.json
python3 validate.py --check-repro       # full checks incl. byte-identical regeneration; writes validation.json
```

`validate.py` replays every conversation: discovery receipts against the host's cumulative expansion rules, the
offered set per step, JSON-schema validity of every call against the schema offered at that step (types, required,
enums, no unknown keys), id pairing, no repeated identical call, protocol order, final answer present, row-prefix
consistency, family split integrity, held-out overlap, exact category counts, authority cues for sends/destructive
tools, honest handling of denials and errors, slot consistency (weekday/today/tomorrow/clock time vs arguments),
synthetic-only PII and tool-result length.

See `DATA_CARD.md` for distribution and limitations.
