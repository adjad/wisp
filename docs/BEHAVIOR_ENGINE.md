# Behavior engine

Wisp learns the user's routines from what they already do (calendar, reminders,
message rhythm, what they ask Wisp), predicts the next day, and sends a few
**nudges** before being asked. Some nudges carry a small, reversible action
that Wisp performs on its own, always with an **Undo**.

**Code mines, the model only talks.** Patterns, predictions and nudge decisions
are deterministic statistics with evidence ids. An optional LLM (the
*phraser*) may reword a nudge body and label ambiguous event titles from a
fixed list. It never decides, and never adds facts.

**On by default; turn off in Settings or with `WISP_BEHAVIOR=0`.** Settings ->
Privacy & Access -> "Learn my routines" saves `behavior.enabled` in
`~/.moe/config.yaml` and takes effect at once, without a restart. Off stops
ingesting, mining and nudging and hides the behavior tools; what was already
learned stays in `behavior.db` until you erase it with **Forget what Wisp
learned** (below), and it is still aged out while off. A launch-time
`WISP_BEHAVIOR=1` or `0` overrides the saved setting. If `config.yaml` exists
but cannot be read (or is not a mapping), the saved setting is unknown and
learning is treated as **off** (fail closed) until the file is fixed;
`PUT /behavior/settings` answers 409 instead of overwriting it.

## Data flow

```
 Mac sources                 Mac runtime (service/assistant/           Engine (service/behavior)
 -----------                 behavior_runtime.py)                       -------------------------
 Calendar (EventKit)  --+    ingest every 5 min + after a Calendar     BehaviorEngine in-process
 Reminders            --+--> sync; mine every 30 min or on change;  --> <WISP_HOME>/behavior.db
 Messages (metadata)  --+    generate nudges every 60 s
 Wisp asks (topic)    --+    (tick every 30 s)
 Approvals/denials    --+           |
                                    v
                          new Nudge objects  <-- observe / mine / predict / generate_nudges / feedback
                                   |
        perform auto actions (add_reminder / add_hold on Wisp's OWN schedule), record them for Undo
                                   |
                  hub event  type=nudge  (dedupe_key behavior:<id>, expires_at)
                                   |
        Swift app (NudgeViews.swift, Notifications.swift): macOS notification + card in the panel
        buttons: Undo . Do it / offered action . Useful . Not now . Wrong
                                   |
        POST /behavior/nudges/{id}/{feedback|undo|act}  -->  runtime  -->  engine
```

The engine package (`service/behavior`) is pure Python + SQLite and must not
import `service.assistant`, `service.tools`, `service.main`, `service.config`
or `service.inference`. A test pins this, so the engine stays a pure library
the Mac runtime calls in-process.

## What it learns

Every pattern has a plain-English statement, raw counts (`support` of
`opportunities`), up to 5 evidence observation ids, and a confidence. Confidence
is the **Wilson lower bound** (one-sided, about 90%) of support/opportunities,
so "4 of 4" is not trusted more than "40 of 44". Mining looks back 16 weeks.
Observations are kept 180 days, nudges 30 days. This age-out also runs while
learning is off: on the first scheduler tick after launch, about every six
hours, and when you switch learning off.

| Pattern kind | Needs | Example statement |
| --- | --- | --- |
| `habit` | at least 4 occurrences over at least 3 weeks; start times binned in 30 min; a weekday joins when it happens on 40% or more of that weekday; window at most 4 h (central 80% of starts and ends). Keyed by category for personal routines (fitness, focus, meal, class, health, social, personal, errand, travel), by cleaned title otherwise. Also notes and completed reminders. | "You usually work out on Mon, Wed and Fri mornings (6:45–7:45am)" |
| `contact_rhythm` | at least 8 interactions (messages in/out plus calendar calls or 1:1s that name the person) over at least 2 weeks, with 50% or more in one daypart (not night). A message counts toward its sender (incoming) or the other side of a 1:1 chat (outgoing), named by Contacts when known; outgoing group messages and automated senders (SMS short codes, toll-free numbers, alphanumeric sender ids, business chats) are skipped. Learned names stay in `behavior.db` until they age out (180 days) or you use Forget; revoking Contacts access does not erase them, though messages from the last 8 weeks are re-keyed to the raw handle on the next ingest. | "You talk to Dev Malik most weekday afternoons (2–5pm)" |
| `cancel_propensity` | a bucket (daypart, weekday, category, organizer self/other, back-to-back first/middle/last/3+/pair) with at least 4 opportunities, a cancel (or move) rate of 40% or more, and at least 1.5x the baseline rate | "When you have 3+ back-to-back meetings you cancel one about 60% of the time — usually the last one" |
| `habit_protection` | a personal habit with confidence 0.4 or more and at least 3 other events booked over it | "When something lands on your workout you move it (4 of 5 times)" |
| `day_shape` | at least 5 weekdays (4 weekend days) of kept events | "On weekdays your first commitment is usually around 6:45am and your last ends around 3:30pm; lunch is usually free 11am–2pm" |
| `wisp_usage` | the same coarse ask topic at least 4 times in a recurring window | "You check your brief with Wisp around 8:15am on weekdays" |

A pattern's `status` is `active`, `confirmed` or `rejected`. The user's verdict
survives re-mining (pattern ids use a coarse window key on purpose). Rejected
patterns never drive predictions, nudges or the profile card.

Calendar cancellations and moves are *detected*, not read: the runtime sends
the Calendar as a replace-set snapshot, and an upcoming event that vanished (or
reappeared elsewhere) becomes `event_cancelled` / `event_moved`.

## Predictions

`predict()` covers at least 24 h and **always through the end of tomorrow**
(local time), so an early-afternoon run still sees tomorrow afternoon's
meetings. Each prediction has a probability and a one-line `because` composed
from counts (for example "20 of the last 24 Mon/Wed/Fri"), never by a model.

| Kind | Meaning |
| --- | --- |
| `activity` | a habit expected in the horizon (confidence 0.3 or more; weighted by the last 4 weeks, halved if unseen for 3 weeks) |
| `contact` | the next window in which the user usually talks to someone (0.3 or more) |
| `cancel_risk` | an upcoming event in a bucket the user tends to cancel; one per back-to-back run; probability is the bucket's historical rate |
| `habit_conflict` | an upcoming event booked over a habit (10 min overlap or more) |
| `usage` | the next window in which the user usually asks Wisp about a named topic (confidence 0.2 or more; the catch-all "general" topic is skipped). Feeds `day_preview` and `predict_my_day`, not the profile card |

## Nudges

Five kinds. Priority is probability x kind importance (conflict 1.0, cancel
0.85, missed 0.7, contact 0.6, preview 0.5), raised 25% per earlier "Useful".

| Kind | Fires when | Actions |
| --- | --- | --- |
| `habit_conflict` | an event overlaps a learned habit | auto: reminder "Remind me" on Wisp's schedule. Offered: "Draft a move request" |
| `cancel_likely` | cancel probability 0.4 or more | auto: tentative hold "Tentative: Likely skip: ...". Offered: "Draft reschedule note" |
| `contact_window` | now is inside a rhythm with confidence 0.4 or more; if message history exists, only when their last message has waited 2 h to 3 days unanswered | offered: "Draft a message to ..." |
| `habit_missed` | a personal habit (confidence 0.5 or more) did not happen today, its window is over, it is before 21:00 and a free slot exists | auto: tentative hold "Tentative: ... (Wisp suggested)" |
| `day_preview` | 07:00-10:00, top four predictions for today; usage windows only fill slots left after the other kinds (one per topic, at most two); expires at noon | none |

Attention rules (`nudges.select`):

- **Budget:** at most 4 nudges per local day. `day_preview` is free.
- **Cooldown:** one per kind per 3 h. `habit_conflict` and `cancel_likely` are
  exempt because each is about one specific event (id = kind + event id,
  expires when the event starts). They still count toward the budget.
- **Quiet hours:** nothing is generated from 22:00 to 07:00.
- **Dedupe:** ids are deterministic, so the same situation never fires twice.
  Expired nudges are never created.
- **Mute:** two negative verdicts (`not_now`, `wrong` or `undo`) for the same
  kind and pattern within 7 days mute it for 7 days.
- **Undo:** an `undo` also turns that kind and pattern's auto actions into
  offers.
- **Horizon:** candidates are built for events through the end of tomorrow.
- **Phrasing:** at most 2 nudges per pass are reworded, and none after 10 s.
  Title and `because` stay code-composed; the body is capped at 400 characters.

## Autonomy

| Action | Who does it | Rules |
| --- | --- | --- |
| `add_reminder`, `add_hold` (`auto: true`) | Wisp, immediately | Only on **Wisp's own schedule**, never Apple Calendar or Apple Reminders. Holds are events titled `Tentative: ...`. Marked "Added by Wisp from your routine". Idempotent per nudge and action, skipped if an identical item already exists at that minute or its time has passed. Recorded so **Undo** deletes exactly those items. |
| `open_prompt` | the user | Opens the panel with a **prefilled, unsent** request ("Draft a note to ... asking to move ..."). Nothing is sent or changed. |

Anything that sends a message or email, or changes a real meeting, still goes
through Wisp's normal confirmation card. Behavior learning never writes to
Apple Calendar.

## Personal context

The engine produces an always-on **profile card** of 700 UTF-8 bytes or less:
confirmed patterns first, then the best of each kind, then today's predictions,
whole lines only, code-composed. `prompt_blocks.profile_block()` adds it to the
system prompt with a header that says the patterns "may be wrong, never state
them as certain facts". It is cached 60 s and never blocks the event loop.
It rides along wherever remembered facts do, including remote inference
([REMOTE_INFERENCE.md](REMOTE_INFERENCE.md)).

## Privacy: what it reads and where it goes

- While it is on it reads Calendar events, Reminders, **Messages
  metadata** (direction, contact, time; never text), the topic label of the
  user's Wisp asks (never the text), and approvals/denials.
- Learned data lives in `<WISP_HOME>/behavior.db` (default `~/.moe/behavior.db`),
  next to `behavior_runtime.db` (delivered nudges and auto actions). Nothing
  else leaves the Mac, except phrasing facts (below).
- With a phraser, only the few facts of one nudge (title, body, evidence line,
  button labels) go to the inference endpoint the `fast` role uses: the local
  model. Event titles may go too, to label unknown titles. The timeline does
  not. **Under remote inference the phraser is off by default**: nudges keep
  their template text and unknown titles stay unlabelled, so no calendar title
  or nudge fact is sent for wording or categorization. Set
  `WISP_BEHAVIOR_PHRASER=1` at launch to opt in; those requests then go to your
  configured server.
- **Forget what Wisp learned** (Settings, or `DELETE /behavior/data`): stops the
  runtime, closes its connections, deletes `behavior.db`, `behavior_runtime.db`
  (with `-wal`/`-shm`) and the profile card cache, and drops pending behavior
  nudge events. It also removes the reminders and holds Wisp itself created on
  its own schedule that are still active and exactly as Wisp made them
  (same identity record Undo uses). Anything you edited, moved, completed or
  dismissed is kept. If learning is still on, Wisp starts over from the next
  ingest.
- **Safety policy.** Every automatic or tapped write asks the same policy as the
  `add_reminder` tool (`decide("assistant_write", ...)`). Under View Only, or
  any decision short of allow, nothing is written: an automatic action becomes
  an offer and the nudge is still delivered.

## What goes to a remote model

Only under remote inference (`WISP_REMOTE_ONLY=1`,
[REMOTE_INFERENCE.md](REMOTE_INFERENCE.md)): the server receives memory facts,
the profile card, recent conversation history (bounded by the profile's byte
budget), full tool results (including mail and message text), and behavior
phrasing facts (event titles, contact names) only if you set
`WISP_BEHAVIOR_PHRASER=1`; the phraser is off by default under remote
inference.

## Switches

Precedence, highest first: `WISP_BEHAVIOR` in the launch environment, then the
saved Settings choice, then the default (on).

| Switch | Meaning |
| --- | --- |
| Settings -> "Learn my routines" | Saved as `behavior.enabled` (default true). Live; no restart. |
| `WISP_BEHAVIOR=1` | Forced on, whatever Settings says. |
| `WISP_BEHAVIOR=0` | Forced off (any other non-empty value also means off). Empty or unset defers to Settings. |
| `WISP_BEHAVIOR_PHRASER=0` | Template text, no phrasing calls. |
| `WISP_BEHAVIOR_PHRASER=1` | Under remote inference, allow phrasing and event labeling (sent to your server). Off by default there; on by default with local models. |
| `WISP_HOME` | Moves all of Wisp's state, including `behavior.db`. |

Tick cadence: runtime tick 30 s, ingest 5 min (and right after a Calendar
sync, which the app does about once a minute), mine 30 min or on change,
nudges 60 s. The assistant scheduler starts each tick only while behavior
learning is on, so turning it off stops the next tick.

## Endpoints

Mac backend (`service/behavior_api.py`, `127.0.0.1:8765`). Every route answers
`409` while behavior learning is off, except `GET /behavior/status`,
`/behavior/settings` and `DELETE /behavior/data`.

| Route | Purpose |
| --- | --- |
| `GET /behavior/status` | enabled, last runs, errors, counts |
| `GET /behavior/settings`, `PUT /behavior/settings` `{"enabled": bool}` | the "Learn my routines" setting; returns `enabled` (in effect), `setting` (saved), `env_override`, `settings_readable` (false when `config.yaml` cannot be read), `remote_active` and `phraser` |
| `DELETE /behavior/data` `{"confirm": true}` (JSON content type required) | forget what Wisp learned; works while learning is off; returns `removed_items`, `kept_items`, `files_deleted`, `files_failed` (anything that could not be deleted; `ok` is then false), `nudge_events_deleted`. While it runs, other `/behavior/*` routes answer 503 and `GET /behavior/status` reports `erasing` |
| `POST /behavior/run` | ingest + mine + nudge + deliver now |
| `GET /behavior/patterns?status=&kind=`, `POST /behavior/patterns/{id}` `{"status"}` | list, confirm, reject, reactivate |
| `GET /behavior/predict?day=today\|tomorrow\|next24h` | predictions |
| `GET /behavior/profile?subject=` | profile card, patterns, predictions, summary |
| `GET /behavior/nudges?limit=` | delivered nudges with verdicts and Undo state |
| `POST /behavior/nudges/{id}/feedback` `{"verdict": useful\|not_now\|wrong}` | teach it |
| `POST /behavior/nudges/{id}/undo` | remove what Wisp added; counts as `undo` feedback |
| `POST /behavior/nudges/{id}/act` `{"action_index"}` | offered action: returns the prompt, or creates the reversible item |

## Tools and routing

Four tools, hidden while behavior is off: `describe_me`, `predict_my_day` (today or tomorrow),
`my_patterns` (query) and `correct_pattern` (confirm, reject or restore; only
changes Wisp's learned profile). The router claims whole-utterance phrases
before other routes, for example "What do you know about me?", "Predict my
day", "What will tomorrow look like?", "When do I usually work out?", "Which
meetings do I usually cancel?", "Who do I usually talk to in the afternoon?".
"That's wrong" corrects a pattern only right after one of those answers (or
when it names a pattern, habit or routine). Compound requests ("... and check
my email") keep the normal router.

## Testing

```sh
WISP_HOME=$(mktemp -d) python -m pytest -q tests/test_behavior_*.py
```

| File | Covers |
| --- | --- |
| `test_behavior_engine.py` | facade, snapshot diffing, pattern status, phraser fallbacks, cursors |
| `test_behavior_miners.py`, `test_behavior_persona.py` | miners recover the routines planted in the fictional test persona (`tests/fixtures/behavior_persona.py`) |
| `test_behavior_predict_nudges.py` | predictions, budget, quiet hours, cooldown, dedupe, mute, undo |
| `test_behavior_phraser.py` | `OpenAIPhraser` contract, fail-closed behavior |
| `test_behavior_runtime.py`, `test_behavior_api.py` | ingest to hub, auto actions and Undo, endpoints |
| `test_behavior_privacy.py` | forget, retention while off, the safety policy on writes, remote-mode wording, fail-closed settings, in-flight switch-off |
| `test_behavior_tools.py`, `test_behavior_routing.py` | tools and router phrases |
| `test_behavior_boundary.py` | `service.behavior` stays portable |

## Known limitations

- **Cancellation history starts at install.** Calendar apps do not keep what
  was deleted, so on real data cancel/move patterns only accumulate from the
  day Wisp first saw the calendar.
- **Phrasing can fall back to templates.** The Mac phraser gives each call 6 s,
  allows 4 calls per minute, and skips while the user is waiting on a turn. A
  slow server therefore shows the code-composed text. That is intentional.
- **A stale hold stays.** If the user really cancels a meeting that has a
  "Tentative: Likely skip" hold, the hold is not removed automatically.
- **Approvals, denials and declines are recorded but not mined yet.**
- **Time zones.** The engine mines in the Mac's zone; snapshots carry only
  epoch times.
- **Single user.** One engine database per Mac.
