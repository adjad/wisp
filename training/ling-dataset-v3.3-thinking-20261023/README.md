# Ling dataset V3.3-thinking: V3.2 with a reasoning trace on every trained turn

Every V3.2 row (2,574 train / 302 dev, V2 replay included) is unchanged except for one addition. The final
(`training: true`) assistant message now has a `reasoning_content` field holding a short trace (1–4 sentences, median
~180 characters, max 470). The set is rendered and trained with `enable_thinking: true`. It also adds 572 train / 72 dev
rows of new conversations aimed at the failures of the 2026-10-23 corpus run (see below). Totals: 3,146 train / 374
dev. Nothing here loads a model,
calls oMLX or reads `~/.moe`.

## Why

V3 was trained with thinking OFF. Wisp's model-only test mode now sends `enable_thinking: true` on every step. Under
that, V3 either rambles (3.3–4k characters on direct probes, about 10× slower, and a planning prompt used up a
1,200-token budget) or emits 1–97 characters of nothing useful. This set teaches the thinking-on rendering with traces
that are short and say something concrete:

- what the user wants;
- what this step does and why;
- where a recipient or message id came from;
- what the last tool returned.

Decode dominates Wisp latency (an output token costs about 137× a prompt token), so the traces stay short on purpose.

## New conversations from the 2026-10-23 corpus run

The rows above are joined by 572 train and 72 dev rows: 189 + 24 conversations in `v33:v33_corpus_fixes`, written by
`ext_v33_corpus.py`. Each family targets a failure that V3 showed with thinking on in the first 257 cases of the
1000-prompt corpus run on build 2457 (run `prompts-20261023-184747-dcf6aa`):

| family | train convs | what the run showed | what the rows teach |
|---|---|---|---|
| `arg_period` | 30 | `period='next 48 hours'`, `'Friday'`, `'next 10 days'` rejected, then "use 'today' or 'this week'" handed back to the user | `days=N` or an exact `YYYY-MM-DD`, first time or after one refused call |
| `arg_scope`, `arg_unexpected` | 13 + 17 | `scope='overdue'`, `get_past_events(period=)`, `summarize_emails(query=)` | correct the argument once, or switch to `view_emails` when only it can filter |
| `date_named` | 17 | "Veterans Day" searched as a 7-day query, answered "nothing"; "Election Day is October 7" invented | resolve the date (Thanksgiving/Election Day computed), read that day |
| `query_retry` | 17 | a long query missed "Office Hours with TA …" that the bare name matched | retry once with the distinctive name |
| `empty_honest` | 17 | invented items, `count` escalated 5 → 1000 | say what was searched and that nothing matched; no repeat |
| `web_blocked` | 13 | 8 identical `web_search` calls after `BLOCKED by safety policy` | the blocked call is context only; the trained step stops and explains |
| `read_not_write` | 22 | "remind me what I said…" created a reminder; "show me what you'd delete" called `clear_past_reminders` | read only, then offer the action |
| `email_address` | 26 | invented addresses, sends to the user's own address | email needs an address from `lookup_contact`; phone-only, no match, several matches or no name → ask |
| `personal_not_web` | 17 | "my Glassdoor alerts" and "my study session" sent to `web_search` | personal sources (offered alongside web_search as a decoy) |

How these rows were made:
- They are built with V3.2's own `generate.Builder`, so the protocol is identical to the other rows: stubs or
  `get_tool_schemas`, receipts, the loading budget and the offer order.
- Tool results and host errors use the exact formats the live host produced in that run.
- Every person, event, email and phrasing is synthetic (`example.invalid`).
- Every prompt is checked against the held-out eval shingles and against `assets/corpus_run_shingles.json` (hashed
  4-token shingles of the run's prompts; no prompt text is stored). The maximum overlap is 0.375, under the 0.4 bar, so
  the next corpus run still measures generalisation rather than memorisation.
- Refused calls are context only (`trainable=False`), as in V3.2's mac-controls rows.
- Each authored conversation carries a `hint`, the one fact its step turns on (for example "days=2 covers the next 48
  hours"). The hint is appended to the trace of the call step, or of the final step for `read_not_write` and
  `email_address`.

The run also showed that 126 of 244 turns (52%) put the whole final answer inside `<think>` and then wrote it again.
`validate_thinking.py` now fails any trace that repeats its answer. Action confirmations are no longer quoted in traces
for the same reason.

Not from the model: cases P-192 to P-216 all failed within 0.3 s with an engine 400 error. That is a
run-infrastructure problem, so it has no rows here.

## How the traces are made

`add_thinking.py` derives each trace from the row itself, with no model involved. The inputs are:

- the user's words, quoted;
- whether each called tool is a name-only stub or a loaded schema;
- this turn's tool results (first lines, with Wisp's evidence wrapper skipped);
- the gold step from V3.2.

| step kind | train | what the trace says |
|---|---|---|
| `discovery` (`get_tool_schemas`) | 128 | request; tools needed and why; not loaded, so load first and nothing runs |
| `stub` (name-only call) | 379 | request; tools needed; the call only loads schemas; any guessed arguments |
| `call` (real tool) | 802 | last result or request; the exact call; for sends, the recipient/message source ("from the lookup_contact result, not a guess") |
| `final_after_tools` | 568 | what came back; done → confirm and stop; denied → say so, no retry; failed → report honestly; repeat guard → answer from the earlier result; ask when only the user can decide |
| `final_direct` | 97 | chat/code/general knowledge needs no tool; vague or destructive request → ask; no referent → ask; capability limits (mac_media can't pick music; no tool can post) |
| `v2_intent` (read-intent JSON) | 510 | the parsed sources/time/query, unsupported constraints, then "JSON only" |
| `v2_summary` | 90 | use only the given results; item count, partial coverage, unavailable sources; keep dates |

Phrasing varies across 2–3 variants per sentence, chosen by a seeded RNG (seed 20261023, keyed on the row id), so the
build is byte-reproducible. There are 1,849 distinct trace skeletons across 2,876 rows.

Context assistant steps carry no reasoning, because Wisp never sends reasoning back in history. With
`preserved_thinking` the template renders them as `<think></think>`, exactly as at inference.

## Checks

- `validate_thinking.py` → `validation.json` (PASS, 3,520 rows):
  - each row equals its V3.2 source once `reasoning_content` is removed;
  - only the trained turn has reasoning, with no template control text, at most 600 characters;
  - every quoted span appears in the row;
  - every tool named is one the row offers;
  - every called tool is named;
  - denials are called denials, and failures are never called successes;
  - no trace repeats its answer;
  - authored rows: provenance present, every trained call offered and schema-valid (required, unknown keys, types,
    enums), no send, no write in `read_not_write`, and no non-synthetic address.
  - A mutation test confirmed that each of these checks fires.
- `preflight.py` → `preflight.json` (PASS) renders every row with the real Ling tokenizer and template using
  `enable_thinking=True`. It asserts:
  - the system turn says `detailed thinking on`;
  - context steps render `<think></think>`;
  - the target opens with the trace followed by `</think>`.

  Real tokens:
  - train max 11,621 / p95 9,091, 16.7M tokens;
  - trained-turn median 63 tokens, max 284;
  - V2 replay rows max 842.
- `check_labels.py` (on the VM, after `axolotl preprocess`) matches prepared rows to source rows by exact input_ids. If
  Axolotl drops `reasoning_content` or ignores `enable_thinking: true`, every row fails with "no matching source row".

## Reproduce

```
cd training/ling-dataset-v3.2-20261014 && python3 sources.py && python3 generate.py   # V3.2, sha-pinned
cd ../ling-dataset-v3.3-thinking-20261023
python3 add_thinking.py            # refuses unless V3.2 matches its pinned sha256; also builds ext_v33_corpus.py rows
python3 validate_thinking.py
/Users/adijain/Desktop/MOE_Project/.venv/bin/python preflight.py --local-files-only \
    --tokenizer-dir /Users/adijain/Downloads/Ling-3.0-tiny-Wisp-V2-merged
```

## Train on the A6000

The recipe is the same as V3.2 (LoRA r16/alpha32 on attention, 1 epoch, LR 1e-4, `sequence_len` 16384), with 197
steps for 3,146 rows and three changes:

- `enable_thinking: true`;
- a `reasoning_content` property mapping;
- output in `ling-v33t-lora-out`.

**Base:** `Ling-3.0-tiny-Wisp-V3-merged`, unless V3.2 has since been trained and passed its gate. This set contains
every V3.2 conversation, so it replaces a V3.2 run rather than stacking on it. Run `axolotl preprocess`, then
`check_labels.py` (it must PASS), then the 20-step smoke run, then the full run, then `merge_lora.py`.

## Eval

Run the eval with thinking ON. On Wisp, that means a model-only build, or `enable_thinking: true` for the role. Compare
against V3 on the same corpus:

- same gate lines as V3.2: 0 forbidden calls, named_item ≥ 7/10, no domain regressions;
- reasoning length per step: target p95 under ~400 characters;
- time per turn versus V3 with thinking OFF.

## Known limits

- The traces are templated. They teach the shape and length of useful thinking and ground every claim in the row, but
  they do not teach new problem-solving. Behaviour is still taught by the V3.2 targets.
- A distillation pass would produce richer traces: a stronger teacher writes a trace per row, and that trace is kept
  only if it leads to the same gold step. That needs a teacher model. It is not done here because the 1000-prompt corpus
  is using oMLX.
- Traces never mention the current date or the private-shell rule, so those decisions stay implicit.
