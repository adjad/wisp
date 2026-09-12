# Wisp stability plan — toward a release build

Written 2026-08-09, after the performance pass. Goal: use Wisp daily without
hitting bugs or glitches. Ordered by **how likely a real user is to hit it**,
not by how interesting the fix is.

Everything marked **[verified]** was reproduced or measured on this machine.
Everything marked **[to confirm]** is a suspicion worth an hour, not a fact.

---

## S1. Raw Python errors reach the user — the whole agent path has one handler

**[verified]** `service/main.py:877-878` is the *only* error handling for an
agent request:

```python
except Exception as e:                      # noqa: BLE001
    await emit({"type": "error", "message": str(e)})
```

Anything that goes wrong anywhere — routing, model load, a tool, persistence,
the engine being down — is shown to the user as a raw Python/httpx string.
Historical examples, all of which a user can hit today:

| what actually happened | what the user sees |
|---|---|
| prompt exceeded the window | `Client error '400 Bad Request' for url 'http://127.0.0.1:8000/v1/chat/completions'` |
| oMLX not listening | `ConnectError: [Errno 61] Connection refused` |
| engine returned a malformed body | `KeyError: 'choices'` *(fixed at source — see S2)* |
| memory guard rejected the prompt | `Client error '400 Bad Request' …` (identical to the first row) |

**Fix.** A small error-translation layer between the exception and the `error`
event. Map the known classes to a plain sentence plus, where one exists, an
action:

- `httpx.ConnectError` / `ConnectTimeout` → "The local AI engine isn't
  running. Wisp tried to restart it — try again in a few seconds." (and
  actually call `ensure_omlx()` before saying so)
- HTTP 400 containing `prefill_memory_exceeded` → "That request needed more
  memory than Wisp is allowed to use. Try a shorter question or fewer
  attachments."
- HTTP 400 containing `exceeds max context window` → "That conversation got too
  long for the model's memory. Starting a new chat will fix it." (this should
  now be unreachable — `_fit_window` prevents it — so also log it loudly,
  because reaching it means the budgeter has a hole)
- `ModelLoadError` → already a good message; pass it through unchanged
- anything else → a generic apology plus the exception text behind a
  "Details" disclosure, never as the headline

Keep the raw text in the debug export. The user gets a sentence; the export
keeps the traceback.

**Why first:** it is the difference between "Wisp broke" and "Wisp told me what
happened". It doesn't fix the underlying failures, but it changes every one of
them from a glitch into a message.

---

## S2. Unguarded response indexing — the crash class behind `KeyError: 'choices'`

**[verified, fixed]** Under concurrent load oMLX answers 200 with a body that
has **no `choices` key at all**. Reproduced deliberately on 2026-08-08 by
issuing two `summarize_*` calls at once: `KeyError: 'choices'` out of
`imessage_tools._summarize`. It had previously been seen in the wild from
`brief.py`.

There were **13 sites across 11 modules** indexing `resp["choices"][0]`
directly. Fixed once at the source — `OMLXClient.chat()` now normalizes a
malformed body to a well-formed empty response via `_ensure_choices()`, so every
caller's existing "no content" fallback handles it instead of crashing.

**Remaining work:**
- `stream_events()` uses `chunk.get("choices", [{}])[0]`, which is already safe
  — but confirm the *final* assembled message can't be malformed the same way.
- Sweep for the same shape elsewhere: `data["..."][0]` on any external
  response (oMLX embeddings/rerank in `search/`, the Air node's
  `/router/classify` in `router._from_remote` — that one already validates).

---

## S3. 26 silent `except Exception: pass` blocks

**[verified]** `grep -c` finds 26. Most are deliberate and correct — a corrupt
profile must not break a turn. But "correct to continue" and "correct to say
nothing" are different decisions, and right now they're the same code.

The dangerous subset is the one where swallowing produces a *wrong answer*
rather than a missing one. Known example already documented in the codebase:
`_demote_unclosed_think` catches a truncated think-block leak, but the user
still loses their summary and silently gets raw header lines instead.

**Fix.** Triage all 26 into:
1. genuinely inert (keep as-is, add a one-line "why silent" comment),
2. should surface a degraded-mode note to the user ("I couldn't read your
   profile, so this answer is less personal than usual"),
3. should be recorded in the debug export even when swallowed.

Add a tiny `debug_capture.record("swallowed", ...)` for category 3 so these stop
being invisible in exports.

---

## S4. Engine lifecycle — the failures the oMLX log actually shows

**[verified]** Across the whole server log: **60 tracebacks/500s**, and the
engine process started **615 times**. Recurring named failures:

| class | count | note |
|---|---|---|
| model unavailable after a previous load | 27 | needs an explicit reload path |
| cannot load — projected memory would exceed | 23+ | user picked a model that can't fit |
| model not found / not installed | 7+ | stale roster entry |
| wrong model type on the endpoint | 7 | embedding model sent to chat |
| prefill memory guard rejected | 7 | see S1 mapping |

`ensure_omlx()` already handles the "menu-bar alive, server not listening" case
by falling back to `restart` — that one is solved and documented.

**Fix.**
- **"unavailable after a previous load"** is the biggest and has no handler.
  Add one retry that unloads and reloads the model before failing the turn.
- **Pre-flight the roster.** On startup, and after any Settings change, check
  every configured role's model against `/v1/models` and its size against the
  memory ceiling. Surface "the model set for Coding isn't installed" in
  Settings, rather than at the moment the user asks a coding question.
  `config.is_tool_capable` already has the "roster disagrees with reality"
  problem documented; this is the same class.

---

## S5. Memory ceiling under concurrency — **FIXED 2026-08-09**

**[verified]** A single request peaks **~4.6GB even at 20k prompt tokens** —
comfortable against the 8.0GB guard. But **overlapping** requests drove the
process to **10.9GB** and the guard aborted one.

This is why parallel tool execution was measured and rejected (see the note in
`agent/loop.py`). But Wisp doesn't have to create the concurrency itself:
oMLX's own keepalive prefill can overlap with a user request, which is how the
10.9GB spike was first produced.

**[verified, fixed]** It was not just a memory question — it was the dominant
LATENCY bug. The same `summarize_messages` call measured **12.8s and 319.7s** on
consecutive runs, and a harness task hit **374.6s**, with oMLX logging
`adaptive_prefill_throttle` and `prefill LRU eviction` throughout. The 2-hourly
profile rotation (~50 sequential model calls) and the daily brief had no
awareness of whether a user was waiting.

Fixed with a foreground gate in `service/idle.py`: `begin_foreground` /
`end_foreground` bracket the `/agent` runner, and `scheduler.py` skips the brief
and profile rotation while a turn is in flight. `_fire_scheduled_sends` stays
ungated — no model work, and a scheduled send should keep its time.

Note the gate covers the `/agent` ENDPOINT; a harness calling `run_agent`
directly bypasses it and will still show contention.

---

## S6. Quality-degradation bugs that don't crash

These produce a *wrong or thin answer*, which is worse than an error because
the user can't tell.

- **[verified, fixed]** `_fit_window` cut the answer budget first and never
  restored it — a turn needing one stale history turn dropped still returned a
  third less answer. Fixed; `tests/test_fit_window.py` locks the ordering.
- **[verified, fixed]** The profile digest spent 92% of its budget on
  "relationship not yet characterized" contact lines and never reached
  Work/school, Routines or Preferences. Fixed; `tests/test_profile_digest.py`.
- **[verified, fixed]** An over-long `run_shell` description made it the
  default answer — `files_list` called `run_shell` instead of `list_dir` in
  4/4 runs before, 0/4 after.
- **[open]** `_demote_unclosed_think` catches the leak but the user still loses
  the summary and gets raw header lines. Worth a "couldn't summarize, here are
  the raw messages" note so the degradation is visible.
- **[open, spawned]** `"what's scheduled"` routes to the outbound send queue
  rather than the calendar (pre-existing; a background task is on it).

---

## S7. State across restart and sleep

- **[verified]** Sessions persist — `~/.moe/sessions.db`, SQLite on disk.
- **[to confirm]** In-memory-only settings deliberately reset on restart:
  `thinking_level`, `read_only`/`full_access`, `idle_minutes`,
  `super_model_active`, and now `narration_thinking`. That is intentional for
  the safety-shaped ones. **Confirm `full_access` resetting to off is the
  documented intent** — if a user turns it on and a crash silently turns it
  off, that's a surprise in the safe direction, which is fine, but it should be
  stated in the UI.
- **[to confirm]** Lid-close/sleep: sources backfill on wake, so data is not
  lost, only timely. Worth an explicit "last synced N minutes ago" indicator so
  a stale answer is never mistaken for a current one.

---

## Suggested order

1. **S1** error translation — biggest perceived-quality win per hour spent
2. **S4** the "unavailable after previous load" retry + roster pre-flight
3. **S5** background/foreground gate
4. **S3** triage the 26 swallows
5. **S6** the two open quality bugs
6. **S2** finish the sweep
7. **S7** confirm and surface

## How to verify a release build

- `for t in tests/test_*.py; do .venv/bin/python "$t"; done` — currently
  **222 passing, 0 failing**
- `.venv/bin/python scripts/test_model.py Agents-A1-4B-oQe6` — currently
  **22/22**, and it drives the real router + agent loop + tools
- A **failure-injection pass** is the thing that doesn't exist yet and should:
  kill oMLX mid-turn, point a role at an uninstalled model, feed a 30k-token
  prompt, and run a turn with the disk full. Each should produce a sentence,
  not a traceback. That is the actual acceptance test for S1.
