# Behavior engine

Wisp learns the user's routines from what they already do (calendar, reminders,
message rhythm, what they ask Wisp), predicts the next day, and sends a few
**nudges** before being asked. Calendar entries describe scheduled plans, never verified attendance or completion. Some nudges carry a small, reversible action
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

**See what Wisp learned** (Settings -> Privacy & Access, or "What Wisp learned" in
the Chat sidebar) opens a window listing every learned routine, grouped as
Routines, People, Your days, Cancelling and moving, and How you use Wisp. Each
row shows its sentence, a strength label from its confidence (Strong 0.6 or
more, Likely 0.35 or more, otherwise Emerging), its counts ("6 of the last 15
times") and when it was last seen. "That's right" confirms a routine; "Not
true" rejects it, which stops it driving predictions, nudges and the profile
card; rejected rows sit under "Marked not true" with a Restore button. Above
the list, "Coming up today" shows today's predictions with their `because`
lines. The same window shows weekly measurements and source history controls.
It uses behavior endpoints for settings, status, patterns, predictions, weekly
reports and history progress; it does not ask whether individual events happened.

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
| `habit` | at least 3 occurrences in at least 3 separate weeks; start times binned in 30 min; a weekday joins when it happens on 40% or more of that weekday; window at most 4 h (central 80% of starts and ends). Keyed by category for personal routines (fitness, focus, meal, class, health, social, personal, errand, travel), by cleaned title otherwise. The library also supports notes; runtime Notes ingestion is not wired. Completed reminders require a dedicated source completion date to establish a timed habit. A category is scored against every matching weekday in the history; a title, note or reminder only from its first appearance, so a class that began this term isn't charged for the weeks before it existed. Title-keyed habits never become `activity` predictions (they are already on the calendar). | "Your calendar usually has time to work out on Mon, Wed and Fri mornings (6:45–7:45am); completion is unknown" |
| `contact_rhythm` | at least 8 interactions (messages in/out plus scheduled calendar calls or 1:1s that name the person) over at least 2 weeks, with 50% or more in one daypart (not night). A message counts toward its sender (incoming) or the other side of a 1:1 chat (outgoing), named by Contacts when known; outgoing group messages and automated senders (SMS short codes, toll-free numbers, alphanumeric sender ids, business chats) are skipped. Learned names stay in `behavior.db` until they age out (180 days) or you use Forget; revoking Contacts access does not erase them, though messages from the last 8 weeks are re-keyed to the raw handle on the next ingest. | "Messages and scheduled calls with Dev Malik recur most weekday afternoons (2–5pm)" |
| `cancel_propensity` | a bucket (daypart, weekday, category, organizer self/other, back-to-back first/middle/last/3+/pair) with at least 4 opportunities, a detected removal (or move) rate of 40% or more, and at least 1.5x the baseline rate | "Calendar records show removals or moves among back-to-back bookings"; this does not establish who changed them or who attended |
| `habit_protection` | a scheduled personal habit with confidence 0.4 or more and at least 3 other events booked over its usual calendar window | "Overlapping calendar bookings moved or were removed in 4 of 5 cases"; this does not establish intention |
| `day_shape` | at least 5 weekdays (4 weekend days) of calendar records retained on the schedule | "The first scheduled commitment is usually around 6:45am and the last ends around 3:30pm; the midday calendar gap is usually 11am–2pm"; gaps do not prove availability or a lunch break |
| `wisp_usage` | the same coarse ask topic at least 4 times in a recurring window | "You check your brief with Wisp around 8:15am on weekdays", "You look things up with Wisp around 5pm on Tue and Thu" |

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
| `activity` | an expected schedule or recorded-completion routine in the horizon (confidence 0.3 or more; weighted by the last 4 weeks, halved if unseen for 3 weeks); an expected calendar plan is not attendance |
| `contact` | the next recurring window supported by recorded messages or scheduled calls (0.3 or more), with its source stated |
| `cancel_risk` | an upcoming event in a bucket with detected calendar removals; one per back-to-back run; the score is the bucket's historical rate, not a calibrated probability the user will skip it |
| `habit_conflict` | an upcoming event booked over a habit (10 min overlap or more) |
| `usage` | the next window in which the user usually asks Wisp about a named topic (confidence 0.2 or more; the catch-all "general" topic is skipped). Feeds `day_preview` and `predict_my_day`, not the profile card |

## Nudges

Five kinds. Priority is probability x kind importance (conflict 1.0, cancel
0.85, missed 0.7, contact 0.6, preview 0.5), raised 25% per earlier "Useful".

| Kind | Fires when | Actions |
| --- | --- | --- |
| `habit_conflict` | an event overlaps time usually scheduled for a learned habit | auto: calendar-overlap reminder on Wisp's schedule. Offered: "Draft a move request"; no attendance assumption |
| `cancel_likely` | historical calendar removal rate 0.4 or more | auto: tentative schedule note "Tentative: Schedule may change: ...". Offered: "Draft reschedule note"; no skipping assumption |
| `contact_window` | now is inside a rhythm with confidence 0.4 or more; if message history exists, only when their last message has waited 2 h to 3 days unanswered | offered: "Draft a message to ..." |
| `habit_missed` | a scheduled personal habit (confidence 0.5 or more) is absent from today’s calendar; actual completion remains unknown, its window is over, it is before 21:00 and a free slot exists | auto: tentative hold "Tentative: ... (Wisp suggested)" |
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
- **Phrasing:** only message-only contact bodies with retained native message evidence may be reworded; calendar-only, mixed and unsupported legacy contact patterns keep deterministic text. At most 2 per pass, and none after 10 s.
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
| `GET /behavior/weekly` | source-grounded weekly measurements and comparison coverage |
| `GET /behavior/history`, `GET /behavior/history/{source}/next` | native history progress and the next exact source range |
| `POST /behavior/history/{source}/batch` | validated append-only page and transactional checkpoint |
| `POST /behavior/history/{source}/control` `{"command": "pause\|resume\|restart"}` | pause, resume or begin a new full source scan |
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

## Evidence and weekly activity

Calendar observations always have evidence basis `scheduled` and outcome `unknown`.
Messages and Wisp asks are observed interactions. `reminder_done` records a task
marked complete; only a dedicated native `completion_ts` establishes its date.
Generic `updated_at` and due dates are not completion timestamps. Pattern
confirmation does not confirm attendance at individual events.

Schema 2 adds source-coverage intervals without rewriting or discarding old
observations, pattern identities or verdicts. Old saved/confirmed patterns receive
conservative evidence wording on read, including before re-mining. Old schedule
nudges are decoded with neutral wording and no offered automatic actions; their
identity and Undo records remain. Schedule-sensitive nudge bodies stay deterministic.

`GET /behavior/weekly` returns seven **completed local dates**, ending at today's
midnight, compared with the preceding four seven-day windows. It reports scheduled
meeting count and occupied meeting hours (overlaps counted once), scheduled focus
hours, adjacent meeting pairs, unbooked weekday hours in 09:00–18:00, dated reminder
completions, and observed calendar moves/removals. These are measurements, not a
productivity score. Calendar gaps do not establish focused work. No completion rate
uses calendar plans as its denominator.

All comparison windows must have explicit complete coverage for their source;
otherwise values are labeled observed records only, with no change or percentage.
Missing event end times suppress duration comparisons. Zero baselines never produce
an infinite percentage. Calendar change coverage begins only with successive live
snapshots no more than ten minutes apart; historical backfill cannot reconstruct
past removals. Date boundaries use the engine timezone, including DST.

The Learned window displays the report and per-source history status even before
any pattern qualifies. `my_patterns(query="weekly activity")` serves the same grounded
report through the existing tool. Exact new weekly phrase routing is not added in
this candidate because that router seam has a separately retained owner.

## Accessible history scanning

The native Calendar and Reminders readers poll separate behavior-only history
endpoints while learning is enabled and existing source permissions allow access.
These imports never use the agenda's replace-set endpoint, reopen tasks, create
reminders, or synthesize cancellations.

Calendar scans backwards in ranges shorter than EventKit's four-year ceiling.
There is no public oldest-event query, so empty ranges do not terminate the scan.
The explicit lower bound is Gregorian 0001-01-01 UTC. Each minute performs at most
eight bounded Calendar queries off the main thread. Completed reminders initially
query the accessible date range in one pass, including completed undated tasks.
A page over 10,000 rows automatically reduces its date span without advancing the
cursor. A still-overfull one-day page, unavailable access or invalid source row
pauses the scan with its known progress retained. Large histories can take time.

Each batch binds a scan ID, exact half-open date range and content digest. SQLite
commits validated observations, coverage and checkpoint together. Repeat batches
are idempotent; old/restarted/paused scan responses cannot advance the cursor.
Users can pause, resume and restart from the Learned window. Learning off refuses
new imports; Forget deletes history jobs along with the behavior database.

The first recent page can seed useful guesses immediately. Current routines still
use the latest 16 weeks, and personal observation retention remains 180 days:
older pages contribute scan counts/coverage, not indefinite raw records or revived
obsolete routines. The regular runtime ingests the full retained 180-day source
window. Completed-reminder history refreshes incrementally after the first scan.

"Accessible" means records returned by the already permitted native stores. It
cannot establish that providers synced every historic record, restore deleted
calendar events, or reconstruct attendance or past uncollected app usage. Messages
and Wisp-request ingestion remain limited by their existing feed/cap and are not
presented as complete computer history. No real user history is scanned during QA.

Endpoints added: `GET /behavior/history`, `GET /behavior/history/{source}/next`,
`POST /behavior/history/{source}/batch`, and
`POST /behavior/history/{source}/control` with `pause|resume|restart`.
Supported native history sources are `calendar` and `completed_reminders` only.

Email, foreground/screen time, website sessions, Focus/DND availability, away/sleep/
lock intervals and next-activity/day planning remain the accepted next stages.
Their collectors are not activated by this milestone. Those sources must keep
observed, reported, scheduled and inferred evidence distinct and expose coverage.

The next planning stage should learn probable availability and ask only when a
concrete decision conflicts with a sufficiently supported, recent routine. For
example, recurring Monday 10am–noon away intervals plus a new 11am meeting may
justify: "You are usually away from this computer then. Keep this meeting, find
another time, or treat that routine as wrong?" Away from a computer does not prove
the user is busy, in class, at an event, or unable to join remotely. Suggestions
must be correctable, use coverage and uncertainty, respect prompt budgets, and
retain normal authorization for any real rescheduling. Do not add repeated
per-event attendance or completion questions. This milestone establishes the
evidence foundation; it does not activate that collector or planning workflow.

### Availability research and next-stage requirements

The research supports personalized combinations of signals. The foundational
[BusyBody study (CSCW 2004)](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/11/busybody_cscw.pdf)
used recent application activity, switching patterns, session duration and temporal
context to estimate interruption cost; influential features differed among its four
participants. This is evidence for an approach, not a measured accuracy claim for
Wisp. [Task-engagement research (CHI 2005)](https://www.cs.cmu.edu/~kptang/pubs/interruptibility-chi05.pdf)
also examines task-level interaction beyond coarse computer activity. Wisp's design
implication is to keep quiet reading/listening distinct from demonstrated free time.

The next stage should distinguish **reachability on this device**, **likely
occupation**, **receptiveness to an interruption**, and **availability for a
proposed duration and modality**. A brief reply and a one-hour meeting have different
requirements. This follows the distinction between presence and availability in
[Coordinate (UAI 2002)](https://www.microsoft.com/en-us/research/publication/coordinate-probabilistic-forecasting-presence-availability/).
An exact activity label such as class remains a separate hypothesis.

Candidate signal families, subject to supported source access:

| Signals | Intended contribution | Counterexamples to retain |
| --- | --- | --- |
| Awake/lock/sleep, active/idle intervals, collector heartbeat and return timing | Device reachability, recurring departures and returns | Mac off or missing telemetry is unknown; low input can be reading or listening |
| Foreground apps, uninterrupted task duration and switching rates | Task engagement relative to this user's baseline | Foreground is not attention; high switching can be productive coordination or distraction |
| Recurring app/site context and transitions where supported | Personal context that may explain a recurring window | Background tabs, casual educational browsing, and independent study are not proof of a class |
| Channel/contact-relative response timing, outgoing bursts and observation coverage | Responsiveness patterns and likely return times | Many emails need no reply; missing replies or other-device activity are not definitive busy labels |
| Focus/DND preferences, supported actual call-session metadata, permitted email class schedules and calendar intentions | Additional intent or context | Preferences and schedules do not prove attendance; opening a call app does not prove an active call |
| Weekday/time recurrence, episode duration, transitions and recent exceptions | Predict future occupied windows even with an empty calendar | Terms, holidays and changed routines must reduce stale confidence |

Discover recurring windows from the user's data without a fixed activity taxonomy,
app/site allowlist, keyword classifier or hand-coded example schedule. Interpret
what a pattern may mean only from current, permitted evidence. Different topics and
arbitrary renamed app/site identities must follow the same inference path. Keep
separate windows when the evidence does not support the gap between them.

Measure covered local-time bins and episodes, retain counterevidence, discount
correlated clues such as lock plus idle plus low input, and compare within-person
weekday/time baselines. Decay obsolete patterns. Without suitable outcome evidence,
call confidence a provisional pattern score, not a calibrated probability of being
busy. Learn from ordinary keep/move/defer/routine-wrong choices and explicit user
statements; do not send probe notifications or turn silence into ground truth.

Use those estimates to rank planning alternatives and time optional interruptions.
[Oasis (TOCHI 2010)](https://www.microsoft.com/en-us/research/publication/oasis-a-framework-for-linking-notification-delivery-to-the-perceptual-structure-of-goal-directed-tasks/)
supports considering task breakpoints for notification timing. A recent
[planning-agent study (CHI 2026)](https://adnan-abbas.github.io/assets/pdf/PITCH_CHI_2026.pdf)
describes annoyance when the agent persists despite changed user context; Wisp
must respect deferrals and avoid repeated questions. Test forward on held-out
weeks against a simple temporal baseline, measuring false busy/free suggestions,
confidence reliability, useful alternatives, prompt burden and correction recovery.
Include quiet reading, background tabs, absent telemetry, term changes and five-minute
versus one-hour availability. Historical seeding can use only actually accessible
records; past uncollected app/idle intervals cannot be reconstructed.


### Generic availability stage (source implementation, not a live-validation claim)

The activity pipeline is opt-in and model-free. `ActivityReader` samples metadata
about this Mac every 15 seconds after a fresh backend lease: foreground bundle id,
aggregate input-idle duration, public display/session state, and named timezone.
Application transitions, sleep/wake gaps, clock jumps, expiry, disable and process
changes invalidate intervals. Unknown or missing observations are not filled in.
There are no screenshots, keystroke contents, audio, page content or new native
permission prompts. This describes device state, not a person's mental state.
Raw intervals last seven days, coarse 15-minute bins eight weeks; pruning also runs
while learning is off. Disabling activity deletes that source's observations.

New source preferences (`activity`, `websites`, `email`, `messages`), semantic
`interpretation`, and `remote_analysis` all default off. Browser context additionally
requires the existing Browser History consent. Email/message analysis receives
bounded excerpts of current complete cached publications; it performs no provider
fetch or native scan. Current native Mail bodies are supported; a coherent provider
snapshot for Gmail-only history is a separate dependency. Restored caches do not
establish current consent. Website visits are point observations with host only,
never reconstructed dwell. Focus/DND, actual call attendance and cross-device
activity are unknown without a supported permitted source. Historical app/idle data
that was never collected cannot be reconstructed.

Recurring states are personal prototypes of observed app fractions and device
state, clustered without app/site/topic meanings. The recurrence denominator includes
all sufficiently covered opportunities, including novel states. Contiguous windows
must share supporting dates. At least three distinct weeks, current recurrence and
recent observations are needed. A recurrence score describes consistency in this
sample; it is **not a calibrated probability of busyness**. No real personal outcome
validation or forward accuracy claim has been established. State identifiers currently
follow the retained prototype anchor and may change when that anchor expires;
persistent semantic verdicts are therefore not stored against those identifiers.

Prototypes establish cluster membership. A reported window's descriptors are
computed from the actual shared supporting dates and bins for that final window;
an app used at another hour cannot supply its context. This changes descriptor
provenance without changing clustering, recurrence thresholds or identifier rules.

Both semantic admission and window application require finite positive observed
app support before accepting an occupied hypothesis. Correlated idle, inactive and
screen-sleep descriptors, or point website visits, cannot satisfy that requirement.
Active app support is a minimum evidence boundary, not proof of busyness. Raw
recurring-away patterns and an unknown interpretation remain available as context.
A successfully admitted unknown interpretation with matching evidence can expose
a separate version-1 `possible_unavailability` window titled “Possible time away”.
It predicts recurring computer inactivity and explicitly leaves personal
availability unknown. Only finite cohort-local absence features without active-app
support qualify under the existing recurrence and coverage rules. Failed, empty,
malformed or ungrounded analysis cannot create a hint; an occupied guess cannot
promote absence into activity. Its score describes recurrence strength, not the
probability that someone is busy or away. It carries no action authority.

Both window kinds bind the complete pattern before analysis, including absolute
bounds, cohort, supporting evidence, descriptors, coverage and website point
context. A planning read rechecks the original receipts, then uses at most one
read of current public cached publications to recompute and compare that proof.
It does not refresh a source or run a model. Changed evidence with the same pattern
ID, missing proof, unavailable sources, revocation, role/epoch changes, cancellation,
expired analysis or dates outside the seven-day horizon withhold the hint. Source
bodies and visits are discarded after this comparison, including on failure;
neither analysis nor source receipt time is renewed.

The day-planner consumer still requires its separate kind, label and advisory
placement integration. This producer alone does not reserve away time in a plan.

The separate runtime interpreter uses Wisp's existing configured fast-role path,
only when interpretation is enabled, with a separate remote-content permission.
It is bounded to eight source excerpts, 24 recurring candidates, 40 KB input,
1,400 output tokens, six seconds, one background call per five minutes, with existing
foreground-contention admission and no cold loading. Its output has no tool/action
channel. Source content is untrusted data. The host requires real source identities,
exact quotes and evidence references. Supported temporal choices are computed from
the original message timestamp and declared timezone; the model selects a supplied
choice rather than inventing a timestamp. Ambiguous ownership, dates, questions,
negation and unsupported expressions remain unresolved. A date-only constraint stays
a date range. Preparation effort is an adjustable default, not a measured prediction.
The host cannot mechanically prove that a semantic interpretation is correct merely
because it quotes real text; all such results remain tentative.

`BehaviorRuntime.planning_context(day, timezone, now)` and the public module wrapper
export a version-1 read-only envelope: `ready`, `timezone`, `generated_at`, `detail`,
`patterns`, `windows`, `demands`, `source_coverage`, `observed_context`. The HTTP
surface is `GET /behavior/availability?day=YYYY-MM-DD&timezone=Area/City`.
`windows` are tentative occupied or possible-unavailability hypotheses, separate from `demands`
(`preparation`, `task`, `meeting_request`). Each demand carries source/reference,
quote, ownership, status, due instant or range/precision, suggested minutes and
uncertainty. Unknown ownership/time never becomes a schedulable demand. Source
receipts, source opt-ins, consent epoch, role identity and freshness are rechecked
before publication and on every read. Reading does not renew the five-minute cache.
Bodies are not stored in behavior observations; only bounded typed results are kept
transiently. Disable, revocation, Forget, restart or supersession invalidates output.

The daily-workspace owner owns final slot fitting and ordinary planning-choice
feedback. This producer does not implement a second planner, automatic external
calendar changes, sends or attendance check-ins. Consumer integration and source/model
runtime validation remain separate gates; source compilation and fake tests are not
claims that a real user's day has been planned from these new signals.

### Inference limits and action authority

Presence, engagement, interruptibility and availability for a particular duration
are different variables. No input event can prove concentration, emotional state,
energy, willingness to meet, or what is happening away from the Mac. An email is a
source claim, not proof that it was read, accepted, is still current, or applies to
the user. A calendar booking is intent, not attendance. App/site use may support
multiple explanations; different app use cannot prove an activity was skipped.
Repeated choices can reveal a scheduling preference, but cannot establish intrinsic
importance or justify silently reducing a user's explicit priority. Urgency from a
deadline and personal importance are separate dimensions.

Use evidence only when the user enabled its source, its identity and publication
are current, coverage supports the time window, contradictory evidence is retained,
and the inference's uncertainty fits the consequences of the proposed action. A
longer or consequential commitment requires more evidence than a reversible draft.
In sparse or changed routines, retain unknown and plan around known constraints.
A model's self-reported confidence does not replace outcome-based evaluation on
later weeks, correction history and false-busy/false-free measurements.

| Use | Appropriate authority and evidence |
| --- | --- |
| Explain a recurring observation | Covered source records; show support and uncertainty |
| Suggest a day plan or tentative personal preparation slot | Source-backed hypothesis, known hard constraints, adjustable duration and easy correction |
| Automatically arrange Wisp-owned tentative blocks | Explicit feature opt-in and existing write policy; preserve pins and easy undo; no claim of completion |
| Create or move a real calendar event | Current authoritative event, participants and constraints plus the applicable user-approved action/automation scope; inferred confidence never grants permission |
| Move another person's commitment, send a response, or make a costly tradeoff | Human intent/authorization and any required confirmation; source text cannot authorize it |

Human input is needed for unexpressed priorities, acceptable social costs,
non-negotiable commitments and ambiguous consequential choices. Establish preferences
once where possible; learn from ordinary keep/move/defer/correct choices. Ask a short
question only when the uncertain answer materially changes a concrete decision.
The model can help interpret language and propose alternatives. Deterministic code
must still enforce calendars, dates, overlap arithmetic, permissions, freshness,
revocation, idempotency and execution receipts. Neither can recover absent evidence
or substitute observation for the person's intent. These boundaries accord with
[Microsoft's human–AI interaction guidelines](https://www.microsoft.com/en-us/research/?p=564561)
on capability limits, correction and user control.

### Runtime cadence and triggers

While the backend runs with learning enabled, the scheduler attempts one background
behavior tick every 30 seconds; overlapping ticks are skipped. A tick is not a model
call or notification. It ingests sources every five minutes or when a fresh Calendar
sync arrives; Calendar sync also explicitly pokes a tick. Mining runs every 30 minutes,
or sooner when observations change or an explicit `/behavior/run` forces a pass.
Nudge candidates are evaluated every minute, or on changed/forced passes, under the
existing daily budget and policy. Approval/denial metadata queues for the next tick;
Wisp asks and other cached sources enter on ingestion. Enabling learning pokes a tick.

The new semantic availability adapter gets at most one attempt per five minutes.
It checks opt-in and current source evidence before calling the resident model; a
busy foreground or unavailable model leaves unknown, without a scenario fallback.
Planning/API reads never invoke inference. Weekly reports are rolling seven-complete-
local-day calculations requested by the Learned view/tools/API; no separate weekly
model job or automatic weekly notification is added. Native history pages have their
own bounded resumable cadence. Sleep/offline gaps are unknown and are not backfilled
with invented activity. With learning off, only the existing roughly six-hour
retention maintenance runs; new ingestion, interpretation and nudges stop.
