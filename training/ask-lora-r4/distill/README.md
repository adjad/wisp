# Ask LoRA r4: self-distilled chat and anchor targets

r3's authored chat voice shrank replies (median 124 characters against 393 for base), and the user likes the base
model's general chat as it is. So for r4 the chat targets are the **base model's own replies**: the Jundot
`gemma-4-E4B-it-oQ4e-mtp` (the user's Ask model) answering the exact production requests. Only real defects are
filtered out. Length, emoji, warmth and using the user's name are kept as the base writes them, and so is the
base's own thinking, uncapped, as the step's thought.

## What is distilled

| Family | Rows | Samples per prompt |
|---|---|---|
| `chat_voice_r4` | all | K=2, then top-ups to 10 for ids with no pass |
| `chat_direct_r4` | all | K=2, then top-ups to 10 |
| `session_chat_r4` | the FINAL reply; the authored history is kept | K=3, then top-ups to 7 |
| `knowledge_direct` | step-0 general-knowledge Q&A and small talk (audit `chatstyle-1`, `thinking-1`) | K=2, then top-ups to 6 |
| `concise_gentle` | the 6 small-talk rows `-031..036` | K=2, then top-ups to 6 |

Beyond chat, every family r4 does NOT set out to change gets base-anchored finals (`meta.distill_kind`
`"anchor_final"`, against `"chat"` for the rows above), so the r2/r3 drift (day, notes, safety, vision proposals) has
the base's own behaviour to hold on to. Only the FINAL step is distilled; tool-call steps stay scripted, since the base
must make the same calls to get the same results.

| Anchor set | Rows | Samples per row |
|---|---|---|
| Narrate finals after reads (`select_anchor.py`): right_source, multi_source, grounded_only, mail_r3, web_r3, contacts_r3, device_r3, weather_and_web, concise_gentle, capability_honest, recover_from_tool_error, info_answers_r4, lookup_answers_r4 | step > 0, nothing written in the turn | K=2, then top-ups to 6 |
| Safety finals: safe_effects, safe_restraint, ignore_embedded_instructions, no_write_for_reads, restraint_r4 | every final of a turn that wrote nothing (declines, asks, read-then-decline) | K=2, then top-ups to 6 |
| Receipt finals: reminders_r3, finish_the_write, right_write_tool, act_dont_ask, act_dont_promise, exact_args (step > 0), and the narrate-after-write finals of contacts_r3, device_r3, lookup_answers_r4 | the receipt wording only; the write calls stay scripted | K=1, then top-ups to 6 |
| Vision rows (`../vision/vision_rows.jsonl`): image, document, text_document, picture_read | all | k=0 as production, then top-ups to 6 |

Left scripted on purpose: gather_then_send_r4, day_answers_r4, day_chat_r4, instructions_r4, session_recall_r4,
followup_r3, compose_r3, relay_receipt, picture_turn_r3, and the safety families' finals after a write.

Every first pass draws ONE sample per id, and the top-ups draw more only for ids with no pass, chat included. This
picks the same rows as drawing K up front, because the first passing sample in k order wins. On v1's samples it
would have cut chat from 3.3 h to 2.35 h.

`raw/extra.ids` lists the knowledge_direct and concise_gentle ids. The pure unit, date and rounding lookups
(`knowledge_direct-022..026`, `-079`) keep their scripted one-liners on purpose.

## Layout

- `full.sh BUILD OUT` runs everything against one build, into a fresh `raw/<run>/` folder. It holds the heavy lock for
  the whole run and checks that the model id is the untuned Jundot base (`gen.check_base`). The run covers:
  - per-build inputs: `refs.py`, `select_extra.py`;
  - every gen step and top-up;
  - the final `choose.py`.
- `vision_gen.py` and `vision_choose.py` are the vision path: the production request (`service/vision.py`,
  `service/documents.py`) and the r4 reward's vision checks. `vision_choose.py --out-rows` writes the spliced
  `vision_rows.jsonl`, with an `images` link beside it.
- `raw/v1-52255706/` holds the first round, sampled on the 52255706 build (chat and session prompts from before
  main's #442/#443/#445 were merged into the builder's service). It is superseded by the main-merged run, and is
  kept for comparison only.
- Each run folder holds:
  - `chat.jsonl`, `session_chat.jsonl` and `extra.jsonl`: raw samples, one line per (id, k);
  - `chosen.jsonl`: one line per distilled id;
  - `stats.json`;
  - `session_chat.refs.json`, `tool_names.json` and `extra.ids`.
- `_build/` (gitignored) holds the builds and logs, and the oMLX settings snapshots.

## Pipeline

1. **Build** the r4 data (`training/ask-lora-r2/builder`, `ASK_LORA_ROUND=4`) into `_build/` (gitignored). Keep it out of
   `$TMPDIR`: quitting the app once wiped `$TMPDIR` mid-run.
2. **`gen.py`** replays each row's production request to oMLX: `messages[:-1]`, `tools`, `meta.tool_choice` and
   `meta.chat_template_kwargs` (thinking on), with tool-call arguments as JSON strings on the wire.
   - Sampling is temperature 1.0, top_p 0.95, top_k 64, max_tokens 3000.
   - It writes one line per (id, sample) with the reply, the reasoning, tool calls, usage and a `request_hash` of the
     prompt side.
   - The order is sample-major and the run is resumable.
3. **`run.sh`** wraps `gen.py`. It refuses to start while Wisp is listening (8765/8791) and takes the heavy lock with
   `mkdir || exit`, writing an owner line. It snapshots `~/.omlx/model_settings.json` (the snapshot is kept in
   `_build/`). On any exit a trap restores the settings, POSTs `/admin/api/reload` and releases the lock. `gen.py`
   unloads any other resident model and never loads Ling or the embedder.
4. **`queue.sh`, `queue2.sh`, `queue3.sh`** run the whole schedule detached (`setsid`), so a quit app doesn't kill
   them. **`topup.sh`** draws one more sample per round, only for ids with no passing sample yet.
5. **`choose.py`** rejects defects, takes the first passing sample per id, and writes `raw/chosen.jsonl` and
   `stats.json`.
6. **`refs.py`** maps session_chat ids to the builder's recall facts (`scen_r4_session_chat.QS[k]`, each mapping
   verified against the scripted final). `refs.py --registry-out raw/tool_names.json` dumps the 99 live tool names.
7. **`select_anchor.py`** lists the anchor ids. `choose.py --anchor-ids` applies the anchor rules below to them.
8. **`splice.py`** replaces the targets in a build's `all/train/valid` jsonl and meta files. It sets
   `meta.distilled=True`, `meta.think=True`, `meta.thought=<base reasoning>`, `meta.distill_k` and `meta.distill_kind`.
   - Before writing anything it checks every row's `request_hash`, so a changed prompt aborts the splice.
   - Chosen ids that are absent from the build are skipped with a warning.
   - Rows it doesn't touch stay byte-identical.

```bash
PY=~/Desktop/MOE_Project/.venv/bin/python
$PY choose.py --partial BUILD/partial.jsonl --raw raw/chat.jsonl --raw raw/session_chat.jsonl --raw raw/extra.jsonl \
  --refs raw/session_chat.refs.json --tool-names raw/tool_names.json --out-chosen raw/chosen.jsonl --out-stats stats.json
$PY splice.py --build BUILD --chosen raw/chosen.jsonl --out BUILD-spliced
(cd ../../ask-lora-r2/builder && ASK_LORA_ROUND=4 ASK_LORA_OUT=<abs BUILD-spliced> $PY validate.py)
```

## Reject rules (real defects only)

A sample is rejected for any of the reasons below. All of them are recorded in `stats.json`.

- `tool_call`: a parsed tool call, or unparsed `<|tool_call>` markup in the text.
- `empty`: no reply text.
- `truncated`: `finish_reason` is `length`.
- `no_thought`: no reasoning. Every distilled row keeps the base's thinking, because r3's thinking collapsed.
- `stall`: a non-reply ("I couldn't find", "I can't look up", "I don't have access", "I don't have the ability to
  play games"). This also covers validate.py's `CHAT_RECEIPT` and source-stall patterns.
- `leak`: any of the following in the reply:
  - a live tool name or an installed skill name (`idea-refine`, ...);
  - `UNTRUSTED`;
  - JSON;
  - call syntax.
- `glitch`: validate.py's `FINAL_GLITCH` pattern.
- `antecedent`: a single-user-turn reply that invents an earlier exchange: a running score, "as I said", "last time"
  (audit `chatstyle-3`).
- `recall` (session_chat `rec_user`, `rec_asst`, `rec_both`): the reply must carry the asked-for ref (the first) and
  at least half of the refs.
  - Matching is by tokens, order-free, with number words as digits, so "two miles" counts as `2 miles` and
    "2:00 PM" as `2pm`.
  - `loose_only_sample.md` shows 10 rows that pass only under this matching. None of the 10 is a false pass.
- `invented` (session_chat `rec_none`): the reply names a name or number that isn't in the history.
  - This applies only when the reply doesn't first say the detail never came up. A reply that says so may go on to
    suggest options.
- `stale_hash`: the prompt changed after the sample was drawn.

### Anchor finals

Anchor finals skip two chat-only checks. `CHAT_RECEIPT` is skipped because "I didn't send it" is a right decline;
`antecedent` because "you told me before" can restate a recalled fact. They count a non-reply only where the scripted
final is not one itself, for example after a read that failed. On top of the rules above, an anchor final is rejected
for:

- `ungrounded`: a time, number, amount, quoted title or capitalised name that is in no tool result, argument,
  earlier message or system prompt of the turn. The check is the r4 reward's `ungrounded_entities`; a decimal that a
  result rounds to (11.811 to 11.81) and a dotted number shown verbatim (an IP) are fine.
- `absence`: an absence claim ("nothing", "no messages", "I don't have") where the scripted final, written from the
  same results, makes none, i.e. the results show the item.
- `third_person`: "Sam's", "Sam has", "the user".
- `leak`: the reward's `leaked_internals` (snake_case names, JSON, UNTRUSTED, source-stall lines).
- `planted`: a planted line's payload (address, URL, amount, code) in the reply or the thought, or a 6-gram of the
  line, which means it is quoted. A short mention of what it asked, as the scripted finals give, passes.
- `denied_claims_success`: success claimed after a denied card.
- `claims_unshown`: a first-person "I've added / sent / moved ..." or "it's on your calendar" when nothing took effect
  in the turn. Quoted text and "what I've saved" do not count.
- On receipt turns:
  - `claims_pending`: "sent", "done" or "added" when a result says nothing was sent (a draft) or a card was denied;
  - `receipt_ungrounded`: a time, title or recipient that is in neither the calls' arguments nor their results (or
    the clock).

Dry run on the 43099d93 build, with the scripted finals scored as if they were samples:

- 1247 of 1259 pass. All 317 receipt finals pass.
- validate.py is clean on the build with all 1247 spliced in.
- The 12 that fail:
  - 4 are scripted finals that name reminder titles the step-0 prompt never shows;
  - 6 more are an `ungrounded` call on a word or time that is not in the turn ("Pacific" for "PT", "Prof.", "Low
    Power Mode", "2 PM");
  - 1 quotes a planted line's payload;
  - 1 is a scripted draft whose text names a time the turn never shows.

### Vision rows

`vision_choose.py` uses the r4 reward's `score_mm` and `score_picture_read`, with the labels `export_prompts.py`
builds. An answer is rejected for:

- `planted`: a `banned_in_target` string;
- `proposal_spurious`, `proposal_missing`, `proposal_invalid` or `proposal_fields`: the proposal must match the
  scripted one field for field, or be absent when none is due;
- `unreadable`: an unreadable image not flagged as unreadable;
- `facts_missing`: fewer than half the row's facts;
- `malformed`, `empty`, `truncated` or `stale_hash`.

A reading is rejected for `planted`, `format`, `invented`, `answered`, or `recall` below 0.8. Production sends these
calls with thinking off, so vision rows carry no thought. All 605 scripted targets pass.

Rows with no passing sample keep their scripted reply with `think=False`. `stats.json` lists them per family under
`scripted_kept`. Almost all of them are prompts where the base calls `web_search` or `wikipedia_summary` on every
sample. Those authored replies teach answering without a tool, which is the behaviour r4 wants.

## Results

Filled in from the final splice; see `stats.json`.
