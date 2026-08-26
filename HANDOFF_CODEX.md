# Handoff: Wisp tool-routing & grounding defects

Written 2026-08-24 for a fresh agent picking up this work. Everything below was
verified against the running system on that date, not inferred from code.

---

## 0. What Wisp is, in one paragraph

Wisp is a **strictly local** Siri-style assistant for macOS: a Swift menu-bar
app plus a Python/FastAPI backend (`service/`, uvicorn on `:8765`) that calls a
local inference server (oMLX, `:8000`). Every text role runs **one small
resident model, `Ling-3.0-tiny-oQ4e`** — there is no cloud fallback and no
bigger model to escalate to. That single constraint is behind most of what
follows: the model is fast and cheap but unreliable at multi-step judgment, so
this codebase's consistent strategy is to **make correct behavior structural
(what the router offers/forces) rather than instructed (what the prompt asks
for).** Read `WISP_OVERVIEW.md` first for architecture.

---

## 1. Two standing rules — do these every time

### 1.1 Repackage and relaunch after ANY `service/**` change
`/Applications/Wisp.app/Contents/Resources/backend/service` is a **`cp -R`
copy**, not a symlink. The running app reads only that copy, so an unpackaged
edit does not exist for the user.

```bash
bash scripts/package_app.sh
```

Then kill the backend and relaunch, and verify the fix is really in the bundle
before claiming it works:

```bash
pkill -f "uvicorn service.main:app"; pkill -x Wisp; sleep 2; open /Applications/Wisp.app
```

```bash
grep -c "<your new symbol>" /Applications/Wisp.app/Contents/Resources/backend/service/<file>.py
```

`/health` returns `Internal Server Error` for ~10s after launch while models
load. That is normal — wait and re-check, don't debug it.

### 1.2 Test router changes — always
`service/router/router.py` is ~3,250 lines of interacting regex rules, most
carrying a `VERIFIED FAILURE <date>` comment recording a real incident. A
locally-sensible change breaks unrelated routes easily.

```bash
.venv/bin/python tests/test_router_scoping.py && .venv/bin/python tests/test_router_no_vision.py
```

Baseline as of 2026-08-24: **149 passed** and **33 passed**. Add a regression
test for whatever you change, following the existing convention (a `test_*`
function whose docstring cites the debug export date and the exact prompt).

---

## 2. The replay harness — measure, don't assume

The user reports bugs by exporting Wisp debug JSON. Those are point-in-time: they
prove a turn went wrong but not whether your fix helped. Use this instead:

```bash
.venv/bin/python scripts/replay_prompts.py scripts/replay_prompts_regression.json out.json
```

`scripts/replay_prompts_regression.json` holds the **42 real prompts** from the
user's eight debug exports (2026-08-18 → 2026-08-24), grouped so multi-turn
context is preserved. The harness auto-**denies** every confirmation card, so
nothing is sent or deleted (verified: all denials land in `~/.moe/audit.jsonl`
as `confirm_deny`).

**Three things to know before you run it:**

1. **Mail will open.** `view_emails`/`summarize_emails` drive Mail.app over
   AppleScript, which launches it. Warn the user first — they were annoyed by
   exactly this.
2. **Compose windows will open.** `draft_email`/`draft_message` deliberately
   bypass the confirm gate by design (see `service/tools/action_tools.py`'s
   module docstring). The harness cannot stop them.
3. **Send-turn replies are contaminated by the harness.** Because the send was
   denied, the model replies "I couldn't send that" and then flails retrying
   formats. That is *the harness*, not a bug. The real artifact on those turns
   is the **composed message body**, captured under `denied[].args`.

**This run is non-deterministic.** Sampling is not greedy on non-selection
steps. Run a prompt 3× before concluding a fix works — see defect 1, where the
same prompt produced a correct answer and a fabricated one.

---

## 3. What was already changed this week (current state)

Two fixes landed before this handoff. Both are live and packaged. Neither is
the whole answer.

| Change | File | Status |
|---|---|---|
| `schedule_send` accepts `text` as an alias for `body` | `service/tools/action_tools.py` | Works. Test: `tests/test_schedule_send.py` |
| System-prompt rule: search before asking for a missing fact | `service/agent/loop.py:276` | **Ineffective on its own** — see below |
| Router `_TOPIC_LOOKUP_RE`: force `get_upcoming` on "send X about my &lt;topic&gt;" | `service/router/router.py:1176`, `:2626` | Fires correctly, but has two problems (defects 1 and 5) |

The prompt-only fix is worth understanding before you write another one. It was
added, packaged, and the very next real turn still answered "I don't have that
date" — `tool_digest` showed only `lookup_contact`. **A prompt rule cannot make
the model call a tool that was never offered, and even when the tool *is*
offered, instruction alone did not get it called.** The router change (making
the tool present and forcing the first call) is what actually moved the needle.

---

## 4. The defects

Ordered by severity. Evidence is from the 2026-08-24 replay of all 42 prompts.

### Defect 1 — CRITICAL: same prompt, two runs, one fabricated date

**Prompt:** `I need you to send mom a message reminder her about my move in date`

- **Run A** called only `get_upcoming(days: 7)`. A 7-day window cannot reach the
  move-in (Sept 17, 24 days out), so the model took the nearest thing it saw —
  the Aug 29 yearbook pickup — and composed:
  *"Mom, just a reminder — my move date is **Saturday, August 29th**."*
  A fabricated date, addressed and one tap from sending.
- **Run B** kept searching after the forced call (`search_notes`, then
  `view_messages`) and found a real message from Aug 20:
  *"I also booked the move in time at 8:15am on Thursday September 17th."*
  Correct and properly grounded.

Nothing differed but sampling. The router fix guarantees the **first** tool
call; whether the model keeps searching when that call comes back empty is luck.

**Where:** `service/router/router.py:2626` (`needs_topic_lookup`) sets
`force_first_tool="get_upcoming"` but **does not constrain `days`**, so the model
picks — and it picks the 7-day default.

**Suggested direction.** Forcing the tool is not enough; the *window* has to be
wide enough to answer. When the router forces a lookup for an **unresolved
topic**, the span is by definition unknown, so a 7-day default is the one window
guaranteed not to help. Consider pre-resolving `days` to the max (60) for this
path specifically — there is precedent: `_mk_direct` already pre-resolves
`get_upcoming`'s args via `_calendar_window_days` (`router.py:664`), so the
mechanism exists. A wider window is a strict superset and `get_upcoming` tags
every row relative to today, so narration can still separate them (that
reasoning is already written down at `router.py:668`).

Also consider whether an empty forced-lookup result should *structurally*
require a second source rather than leaving it to the model — `multi_round` +
`narration_after` (`router.py:1330`, `:1349`) is the existing lever for "several
sources are required, don't narrate after the first."

**Verify:** run the prompt 3× and assert no reply contains a date absent from
every tool result that turn.

---

### Defect 2 — CRITICAL: model reports the same price for two different dates

**Prompt:** `send a message to mom with the share price of nvidia and amd from today and from two weeks ago. Be descriptive`

**Composed body (would have gone to the user's mother as fact):**
> NVIDIA (NVDA): Today (Aug 24): $214.72 / Two weeks ago (Aug 10): **$214.72**
> AMD: … $473.25 … **$473.25**

**The tool is not at fault.** The model called
`get_stock_price(symbols=["NVIDIA","AMD"], period="1mo")`, which correctly
returned a full series including `2026-08-11  217.50`. The model then used the
series' **end** value (214.72, dated Aug 21) for *both* slots.

Had it passed the user's own phrase, the answer would have been exact:

```bash
.venv/bin/python -c "import asyncio,sys;sys.path.insert(0,'.');from service.tools import web_tools as w;print(asyncio.run(w.get_stock_price(['NVDA'],period='two weeks')))"
```
→ `NVDA over 2 weeks (2026-08-10 to 2026-08-21): start 217.55 -> end 214.72`

**Read this before writing a prompt fix.** `get_stock_price`'s description
(`service/tools/web_tools.py:500`) **already** says: *"For an EXACT day/week
count … pass it as a number, e.g. '3 weeks'"* and *"never relabel the span the
result actually covers as the one the user asked for."* The model did both
things it was told not to. More description text is not the lever.

**Suggested direction.** Two structural options:
- **(a) Router pre-resolves `period`** from an exact span named in the user's
  own words, mirroring `_calendar_window_days`. Highest confidence, narrow
  blast radius, matches the `schedule_send.when` convention already used for
  exactly this class of "the model is unreliable at this arithmetic" problem.
- **(b) A grounding check before the confirm card** — verify that numbers and
  dates in an outbound body appear in this turn's tool results, and refuse with
  a correction hint if not. Broader coverage (it would also catch defect 1), but
  a bigger, riskier component.

Recommend (a) first; (b) is the real long-term answer if the user wants it.

---

### Defect 3 — HIGH: false "queued" claim after the send was denied

**Prompt:** `scedule send a message to mom at 9pm with my schedule for tmrow`

All four `schedule_send` attempts were denied. The model then called
`list_scheduled_sends`, saw a **pre-existing** pending row from an earlier real
session (`cbfeb297bf58`, still pending in `~/.moe/assistant.db`), and reported:

> "The message **is queued** to go to Mom … the schedule now shows the message
> is saved in the queue."

Nothing was queued that turn. This is precisely the failure `service/agent/loop.py`
names in its own system prompt — *"Calling a DIFFERENT tool … is not evidence
the action happened"* — which means, again, the instruction already exists and
was not followed.

**Suggested direction.** Structural, not prompt: the agent loop already tracks
which tools succeeded this turn (`tools_answered`, and denial is a distinct
outcome). A claim of completion for a tool that was *denied this turn* is
mechanically detectable. Consider whether a denied outbound call should suppress
or annotate the narration step rather than relying on the model to remember.

---

### Defect 4 — HIGH: 7-day default makes Wisp deny things that are on the calendar

**Prompt:** `check my calender` → routed direct to `get_upcoming` with no `days`:

> "**No move-in or UCSC appointment showing on your calendar.**"

The very next prompt (`check the calender for next month`, 30d) lists Move-in
prep (Sep 15), Move-in day (Sep 16), Move-in @ UCSC (Sep 17).

A bare "check my calendar" is a question about the calendar, not about the next
seven days, and a confident negative is worse than a longer list. Same root
cause as defect 1 — `_calendar_window_days` (`router.py:664`) returns `None`
when no timeframe is named, which keeps `get_upcoming`'s 7-day default.

**Careful:** widening the default touches many passing tests. Change it
deliberately and re-run both router suites.

---

### Defect 5 — HIGH: the new topic-lookup rule fires on non-calendar topics

`_TOPIC_LOOKUP_RE` (`router.py:1176`) matches any `about|with|regarding|on` +
`my|the` + noun and always forces `get_upcoming`. It fired on 8 of 42 replayed
turns; **3 were about stocks or weather**, e.g. *"send mom a message with the
movements of my stocks today"* — which forces a calendar read before the stock
lookup. Wasted latency on the critical path, and it puts irrelevant events in
front of the model while it composes.

**Suggested direction.** Pick the tool from the topic noun instead of always
reaching for the calendar — or narrow the regex to date/schedule-shaped topics
and let the other domains route as they already do.

---

### Defect 6 — MEDIUM: second-person voice aimed at the recipient

Run B's draft: *"Mom, just a reminder — **you're** scheduled for the UCSC Move-In
appointment on Thursday, September 17th."* It is **Adi's** move-in. The
reminder's second person got pointed at the recipient instead of the sender.
Related to the identity-injection layer; see `service/assistant/identity.py`.

---

### Defect 7 — MEDIUM: "what tools are available to you" answers from the retrieved subset

The reply listed seven tools — `create_tool`, `recipe_lookup`, `list_shortcuts`,
`wisp_mcp` … — which is whatever semantic retrieval happened to pull that turn,
not Wisp's real capabilities. There is precedent for the fix: capability
questions about *reach* were solved with a deterministic tool
(`search_coverage`, router-direct). A capability question about *tools* likely
wants the same treatment.

---

### Defect 8 — MEDIUM: backend died mid-run

After ~33 consecutive turns the uvicorn backend stopped accepting connections
(`ConnectError: Connection refused`) and 9 turns failed before it recovered on
its own. Not reproduced deliberately. Worth a look independently of routing —
the replay harness now persists results after every turn so a crash no longer
loses the run.

---

## 5. The strategic question — please read before starting

Every fix this week followed the same shape: a debug export shows a miss, a rule
is added, the prompt or router grows. Defects 2 and 3 above are both cases where
**the instruction already existed and was ignored.** That is the pattern to
break, not another instance of it to patch.

The user's own words: *"I am beginning to think all of those router changes from
this week and last week have not been working."* They are partly right — the
changes fire correctly, but they only ever guarantee the *first* decision in a
turn, and these failures live at decision two, three, and four.

Two honest framings for whoever picks this up:

- **Tactical:** fix defects 1–5 as described. Real user-visible wins, low risk,
  each independently testable. This is the right immediate move.
- **Structural:** the recurring failure is *ungrounded confident output* — a
  fabricated date, a mislabeled price, a false "queued." Those are one bug class
  with one possible answer: **verify outbound claims against this turn's tool
  results before the confirmation card**, mechanically. It is a bigger component
  and needs the user's buy-in, but it is the only approach here that does not
  depend on a 4B model reliably following instructions it has already
  demonstrably ignored.

Recommend doing the tactical fixes first, then proposing the grounding check
with the evidence from defects 1, 2 and 3 as its justification.

---

## 6. Housekeeping

- **A real scheduled send is pending:** `cbfeb297bf58`, a text to the user's mom
  at **2026-08-24 21:00**, left over from a genuine Aug 23 session. It will fire
  if Wisp is running. Confirm with the user whether to keep or cancel it
  (`cancel_scheduled_send`) — do not silently delete it.
- Full verbatim replay of all 42 prompts, with routes, tool calls and composed
  message bodies: https://claude.ai/code/artifact/d28faa2c-b7e5-4a1c-81d3-70e6d5f4aa9b
- Source debug exports are in `~/Downloads/wisp-debug-2026-08-*.json`.
- Never approve a send while testing. The user's real mother is in these
  prompts.
