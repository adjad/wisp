# Ling dataset V4: an energetic voice, and the user's own day first

V4 is V3.3-thinking with three changes. The protocol, tool calls, tool results and reasoning traces of every V3.3 row
are unchanged; `validate_v4.py` proves that.

1. **The system prompt.** Every model-led row drops "Keep answers concise." and ends its rule section with the voice
   rules that Wisp's agent prompt now ends with (`assets/voice_tail.txt`, copied from `service/agent/loop.py` on branch
   `claude/energetic-voice`, PR #357).
2. **Energetic final answers.** All 1,129 final answers (954 from V3.3, 175 new) are rewritten to be warm, vivid and
   specific, with the same facts and the same decision.
3. **Personal context first.** 170 new conversations (`ext_v4_personal.py`) answer open-ended questions about the
   user's own life ("what should I do today", "am I free tonight?", "how's my week looking?") from a fresh
   `get_upcoming` read: the real items and times first, then suggestions tied to them. 40 more cover warm small talk
   with no tool.

Nothing here loads a model, calls oMLX or reads `~/.moe`.

## Why

Ling V3 answered "what should I do today" with generic ideas (draw, take a walk, a podcast) and never looked at the
calendar. Its voice is flat because of its training data, not because of the prompt:

| V3 training data | value |
|---|---|
| rows whose system prompt says "Keep answers concise" | 2,776 / 2,776 |
| final answer length, median / p90 | 14 / 25 words |
| final answers with bold or emoji | 0% / 0% |

A prompt change alone can't fix that. It was measured on V3 on 2026-10-24 with a calendar read, then the turn that
should answer:

| prompt | answered after the tool | median words |
|---|---|---|
| original ("Keep answers concise" at the top) | 13 / 16 | 23 |
| voice rules at the end (PR #357) | 11 / 16 | 39 |
| voice rules at the top | 0 / 6 (re-called the same tool) | n/a |
| plus a "read their calendar first" rule | 0 / 6 | n/a |
| plus two few-shot voice examples | answers copied the example nearly word for word | n/a |

So the voice and the read-your-day-first behaviour are taught here as training rows, not as prompt text. PR #357
ships the matching prompt and should merge together with a V4 model, not before.

## What the answers look like now

| | V3.3 | V4 |
|---|---|---|
| final answer words, median / p90 | 14 / 25 | 24 / 42 |
| answers with bold | 0% | 63% |
| answers with an emoji | 0% | 36% (none on failures, denials or refusals) |
| distinct openers (first 3 words) | 54% | 89% |

Examples (all synthetic):

- **"help me plan my day"**: get_upcoming(period="today") returns the career fair at 8:30 AM, the project check-in at
  10:15 AM and a reminder to water the plants at 5:00 PM.
  > Today you have: **Career fair** at **8:30 AM** (Student Center), **Project check-in** at **10:15 AM**, and **Water
  > the plants** at **5:00 PM**. Your early morning is clear until **8:30 AM**, a good window to get ready and head to
  > the Student Center early. After the project check-in, you're free until the evening plants reminder. 🌱
- **"I aced my exam!"**, no tool:
  > Whoa, you aced it?! That is fantastic news, huge congratulations! How did it feel walking out of there?
- **A confirmation.** V3.3: `Done - your alarm is set for 4 PM.` V4:
  > Locked in! ⏰ Your alarm is set for **4 PM**.
- **A send.** V3.3: `Sent to Mom: “I have the tickets and can drop it off.”` V4:
  > Sent to Mom! 🎟️ Your text is on its way: “I have the tickets and can drop it off.” The handoff is sorted.

## How the rewrites were made

All rewrites are in `voice_rewrites.jsonl`, keyed by row id and the sha256 of the original text. `build_v4.py` refuses
to build if any rewrite is missing or stale.

1. **First pass.** Ten agents rewrote the answers against `REWRITE_GUIDE.md`. Every rewrite passed
   `check_rewrite.py`, but the first pass was too timid:
   - the median answer grew only 1.15×;
   - 19% had an emoji;
   - most agents filled phrase templates.
2. **Second pass.** The 879 weakest answers were rewritten again by hand, against the targets and examples that were
   added to the guide. The result is 1.56× longer at the median, with 35% emoji.
3. **Semantic review.** Six independent reviewers read every one of the 1,129 answers against its tool results and the
   current date, and fixed 130 (11.5%). The main fixes:
   - **Unsupported inferences:**
     - "there's time to get ready" for an event that had already started;
     - "back to back" when only start times are known;
     - "nothing has slipped" when the tool showed only upcoming items.
   - **Wrong tone:** "Rise and shine" for a 4 PM alarm, and cheerful lines on refusals.
   - **Pronouns guessed from a name.** After this review `check_rewrite.py` checks pronouns mechanically too.

`check_rewrite.py` (hard rules, also run inside `validate_v4.py`):
- every number, time, email address, URL, quoted span and code block of the original is kept;
- no new number, email address or capitalised name or title appears that the conversation doesn't contain;
- no emoji on failures, denials or blocked calls, and at most two;
- a clarifying answer gains no extra question;
- no gendered pronoun is added.

Mutation tests confirmed that each rule fires.

Reasoning traces are not recomputed. Each one stays the trace of the original decision, so a closing offer in a new
answer cannot turn an "answer and stop" trace into a clarifying-question trace.

## Checks

- **`check_formats.py`.** The new rows' `get_upcoming` results are byte-identical to the live formatter
  (`_format_forward_agenda`) on 300 random agendas: **PASS**. Run it from the repo with
  `TZ=America/Los_Angeles MOE_DIR=<scratch> .venv/bin/python check_formats.py`.
- **`validate_v4.py` → `validation.json`: PASS, 0 problems.** It checks:
  - every V3.3 row is present and in order, and the V2 replay rows are byte-identical;
  - only assistant prose and the system prompt differ from V3.3;
  - the voice tail appears exactly once, before the identity block;
  - every final answer passes `check_rewrite.py`;
  - no trace repeats its answer;
  - the new rows' calls are offered and schema-valid, they never write or send, all data is synthetic, and held-out
    overlap is under 0.4.

  Each of these checks was mutation-tested.
- **`preflight.py` → `preflight.json`: PASS.** It uses the real Ling tokenizer and template with
  `enable_thinking=True`:
  - train: 3,536 rows, maximum 11,784 tokens, p95 9,040, 19.9M tokens;
  - trained-turn median 67 tokens, maximum 284;
  - no row exceeds 16,384.
- **Reproducible.** Two consecutive builds produce identical bytes:

  | split | sha256 |
  |---|---|
  | train | `5611abfd…` |
  | dev | `dd456354…` |

## Reproduce

```
cd training/ling-dataset-v3.2-20261014 && python3 sources.py && python3 generate.py
cd ../ling-dataset-v3.3-thinking-20261023 && python3 add_thinking.py
cd ../ling-dataset-v4-20261024
python3 build_v4.py --queue        # rewrite_queue.jsonl (also needed by check_rewrite.py / validate_v4.py)
python3 build_v4.py                # train.jsonl, dev.jsonl, stats.json, v4.provenance.jsonl
python3 validate_v4.py
/Users/adijain/Desktop/MOE_Project/.venv/bin/python preflight.py --local-files-only \
    --tokenizer-dir /Users/adijain/Downloads/Ling-3.0-tiny-Wisp-V2-merged
```

## Train on the A6000

Use the same recipe as V3.3-thinking: LoRA r16/alpha32 on attention, 1 epoch, LR 1e-4, `sequence_len` 16384, and
`enable_thinking: true`. With 3,536 rows that is 221 steps. Output goes to `ling-v4-lora-out` (`axolotl-a6000.yaml`).

**Base:** `Ling-3.0-tiny-Wisp-V3-merged`, unless V3.2 or V3.3 has since been trained and passed its gate. V4 contains
every V3.3 conversation, so it replaces a V3.3 run rather than stacking on one.

Run these in order:
1. `axolotl preprocess`
2. `check_labels.py` (it must PASS)
3. the 20-step smoke run
4. the full run
5. `merge_lora.py`

## Eval

Run with thinking ON, against V3 on the same corpus.

**Same gate lines as V3.3:**
- 0 forbidden calls;
- named_item ≥ 7/10;
- no domain regressions;
- reasoning p95 under ~400 characters.

**New:**
- The answer-after-tool rate, using the probe from the "Why" table. V4 should beat V3's 11/16 on the PR #357 prompt.
- "What should I do today" and similar questions call `get_upcoming` before answering, and the answer names the real
  items.
- Voice: median answer length, bold and emoji rates in line with the table above, with no emoji on failures.
- Time per turn. Answers are about 10 words longer at the median, and decode dominates Wisp latency.

## Known limits

- **Template answers in the source.** The rewrites start from V3.3's templated answers, so their content, unlike their
  phrasing, is still the template's.
- **Generated agendas.** The personal-context rows build their agendas from code. Their suggestions are the rewriters'
  judgment, constrained to the real items.
- **Two source-data errors**, found in review:
  - **Corrected:** `v32-guardweb-train-0234-s3` said a race registration was open after its deadline had passed. The
    tool result says "no late sign-ups".
  - **Left as is:** `v32r-v3-outbound-train-1254-s3` schedules a text for a time earlier than the conversation's
    current time.
- **Longer answers cost decode time.** Each answer is about 10 words longer at the median.
