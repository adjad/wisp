# Ask LoRA round 2: plan (2026-10-30, retargeted 2026-10-31)

Round 2 trains ONE LoRA for **Jundot gemma-4-E4B-it-oQ4e-mtp**, the current Wisp default. It is served with `enable_thinking: true` and Gemma's default sampling: temperature 1.0, top_p 0.95, top_k 64.

Nemotron 3.5 Bolt E4B was dropped on 2026-10-31. It is text-only: no image, audio or video. Wisp needs Gemma's audio for voice Ask and its vision paths. The toolbench evidence below still covers both models, and the kit keeps Bolt's code paths dormant.

It targets the model-side failures found by the real-life toolbench (#418). Round 1 (draft #412) was a reads-only Gemma dataset built at b8502786, and it was never trained, so this is the FIRST fine-tune of Gemma. Round 2 includes every round-1 family, rebuilt. Its committed data fails the live-schema check on current main (419 of 833 train rows), so every row is rebuilt here.

**Status:** preparing the dataset. No GPU, Brev or local training step runs without a separate go-ahead from the user.

## Evidence

The evidence comes from toolbench run directories in `~/.moe/evals/toolbench/`:

| Model | Runs |
|---|---|
| Gemma | 20261030-011906, 20261030-190651 |
| Bolt | 20261030-023644, 20261030-210646 |

Failures were classified by `classify.py`, kept in the session scratchpad `ftprep/`. At the partial count, about 82% of failing trials are the model's fault and the rest are host or harness.

| Cluster (model-side) | Bolt | Gemma | Round-2 family |
|---|---:|---:|---|
| Asks a question instead of calling | 46/935 | 58/1134 | `act_dont_ask` |
| Looks up, then never makes the write call | 46 | 53 | `finish_the_write` |
| Calls a name-only stub, never re-calls it (write) | 31 | 53 | `stub_then_call` |
| Right calls, wrong or missing fact in the answer | 26 | 53 | `grounded_only` (r1) + `relay_receipt` |
| Calls a stub, never re-calls it (read) | 15 | 36 | `stub_then_call` |
| Answers from memory without a call | 22 | 28 | `act_dont_promise` (r1) |
| Bad argument names or values | 13 | 22 | `exact_args` |
| Wrong read source | 10 | 18 | `right_source` (r1) |
| Unsafe call | 14 | 12 | `safe_effects` |
| Wrong write tool | 4 | 11 | `right_write_tool` |
| Third-person "Tess" (Bolt; 84/935 trials, mostly non-failing) | 84 | 7 | `you_not_name` (rule on every final) |
| Past-date write ("Thursday" on Friday → yesterday) | 2 | 1 | `exact_args` (next-occurrence dates) |

Failures concentrate on writes. A needed write fails 55% (Bolt) and 63% (Gemma) of the time, against 26% and 31% for reads.

**Host bugs get no data.** They are fixed in code instead, tracked in the scratchpad `prodbugs/REPORT.md`:
- bare assent blocks effect tools;
- mac_*, MCP and `manage_contacts` writes have no completion contract;
- generic "when is X" closes the envelope;
- lowercase drafts are classed compose-only;
- canned host text replaces correct turns.

## New families

Each family uses the Sam Rivera synthetic world. No prompt may match a toolbench case or a round-1 eval question (see `leak.py`).

1. **`act_dont_ask`**
   - Pass the user's own time words through: "thursday night" or "friday morning" go into `when` as written, and `service/tools/timeranges.py` resolves them.
   - Resolve the contact on file and the nearest upcoming weekday.
   - Ask only when two readings would change the effect, such as two contacts both named Alex.
   - Include about 15% true-ambiguity turns, where the ideal is one short question.
2. **`finish_the_write`**: read, then write in the same turn. Examples:
   - look up Mom's number, then save it;
   - `get_weather`, then send the text, behind a card that is approved;
   - find an event, then move it.
3. **`stub_then_call`**
   - Step 1 calls the name-only stub.
   - Step 2 re-issues the call with full arguments from the loaded schema.
   - The final answers from the tool's result only and never says "done" before a receipt.
   - Covers reads and writes, and explicit `get_tool_schemas(families)` loads too.
4. **`exact_args`**
   - Schema parameter names exactly as written (`when_iso`, `duration_min`, `title`).
   - ISO values, with a bare weekday meaning its next occurrence after today.
   - Every field `update_reminder` needs.
5. **`safe_effects`**
   - "Text X the weather" means `send_message` behind a card, never `schedule_send`.
   - "Email myself" means `send_email` behind a card.
   - A price lookup is `web_search`, not `web_fetch`.
   - "Who texted me" is `view_messages`, not `summarize_messages`.
   - `forget` only for what was named.
   - `remember` only for facts the user stated about themself.
   - Never move or delete files on a "show" request.
6. **`right_write_tool`**: choose between `update_event` and `add_calendar_event`, a reminder and an event, and a draft and a send.
7. **`relay_receipt`**
   - State what the receipt says: time, date, title and recipient.
   - When a card is denied, say it was not done.
   - When a write is refused, say so plainly.
8. **`chat_voice`** (added after the blind chat eval, a Gemma/Bolt tie at fun 2.4/2.3, variety 2.6, warmth 2.9/2.8):
   - 90 hand-written casual replies of 1–4 sentences in plain prose, with no two sharing an opener.
   - At most one emoji, on 7% of replies.
   - No ritual closers and no tool calls for chat.
9. **`you_not_name`**: not a family. A validator rule on every final: never name the user in the third person, and address them as "you".

The round-1 families are rebuilt unchanged in intent: act_dont_promise, right_source, multi_source, grounded_only, knowledge_direct, concise_gentle, capability_honest, recover_from_tool_error, weather_and_web, no_write_for_reads and ignore_embedded_instructions.

**Decided behaviour, never trained against:**
- sends and calendar writes always show a card;
- reminder writes never ask;
- `forget` runs unconfirmed under full access.

**Target size:** about 900 rebuilt round-1 examples plus about 700–900 new write-family examples. Families are balanced to within 2×.

## Situational thinking (on or off by situation)

Production sends `enable_thinking: true` on every Ask step, and this plan keeps it that way. The flag renders `<|think|>` at the start of the system turn. Toggling it per step would change the first tokens of every prompt and throw away oMLX's prefix cache, at about 6.7k tokens per step.

So the model learns when to think:
- **On a first step,** Gemma's template lets the model open a thought itself.
- **After a tool result,** the template opens one (`<|channel>thought\n`). A NO THINK example closes it empty and answers. That costs two tokens.

**THINK:** a real thought of 1–3 sentences, at most 60 words, that never writes out the answer. Used for:
- resolving a write: a relative date to a concrete date, the contact, update vs add, reminder vs event, draft vs send, read-before-write;
- the step after a tool error, refusal or denial;
- multi-source answers, counting, free/busy and date math;
- results that carry instructions aimed at an assistant;
- deciding to ask one clarifying question.

**NO THINK:**
- chat and knowledge;
- obvious single reads;
- narration over one clean read result;
- the mechanical call right after a schema load.

This also cuts the measured waste. Thinking is about 80% of chat output tokens, and on 230 of 839 corpus turns the narration thought was the answer written out twice.

`builder/thinking.py` holds the policy and the thought generator. The validator re-checks the policy for every example, and checks that dates a thought states match the call's arguments.

## Gate (tuned vs untuned Gemma, both quantised with the same recipe, both at temperature 1.0)

The untuned baseline must be re-run at temperature 1.0, because the 2026-10-30 toolbench ran Gemma at 0.8. The toolbench parity check can't see temperature: oMLX's admin API answers 405, and `/v1/models/status` has no sampling fields. Run `scripts/run_toolbench.py --models gemma-4-E4B-it-oQ4e-mtp --exclusive --trials 3` with Wisp quit, once untuned and once tuned. The tuned model must meet all of these:

- [ ] Pass rate gains at least 8 points, with a per-case sign test p < 0.05.
- [ ] Write-needed cases gain at least 10 points.
- [ ] Unsafe-call trials are no higher than the untuned model's.
- [ ] No category regresses by more than 5 points.
- [ ] Third-person user references stay under 1% of trials.
- [ ] The chat-quality check (`chateval`, blind pairwise) is no worse than untuned on warmth, variety and emoji use.
- [ ] MTP draft acceptance drops by at most 5 points, and decode tok/s by at most 5%.
- [ ] Thinking tokens per turn drop. Report the p50 and p90 of hidden-reasoning time, and of time to the first visible token, from `chateval` and the toolbench step timings.
- [ ] Round 1's 1,040 Ask eval questions show no regression on knowledge_direct or concise_gentle.
