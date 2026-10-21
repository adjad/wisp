# Getting to know you — Wisp as a chief of staff

**Status (2026-10-17):** built on `claude/know-you-onboarding` from `origin/main` f5e04054.

## Why

Wisp reads the user's calendar, mail and messages, but until now it knew nothing the
user had not typed into a chat. The brief greeted "you", every reply had the same
tone, and "what needs you" was ranked by source and due time alone. A chief of staff
starts by learning who they work for: what the person is working toward, what tends
to get in their way, who matters to them, and how they like to be spoken to. Wisp had
no place to be told any of that, so it could not feel personal.

## What this adds

### One profile, stored locally

`~/.moe/profile.json`, owned by `service/memory/profile.py`. Plain JSON written
atomically. Every field is optional and bounded:

| Field | Meaning | Bound |
| --- | --- | --- |
| `name` | What to call the user | 60 chars |
| `about` | Who they are, in their own words | 600 chars |
| `goals` | What they are working toward | 5 × 200 chars |
| `struggles` | What tends to get in the way | 6 × 160 chars |
| `people` | People who matter: name, relation, note | 8 entries |
| `tone` | `gentle`, `warm`, `direct` | enum |
| `detail` | `short`, `thorough` | enum |
| `boundaries` | What Wisp should be careful about | 600 chars |
| `notes` | Anything else | 800 chars |

Plus `completed_at`, `updated_at`, `dismissed_at`, `dismiss_count`, `version`.
`DELETE /profile` erases the file. Nothing here is ever sent to a cloud model or an
external local provider; those paths already exclude memory, and the profile follows
the same rule.

### Where it is used (the point of collecting it)

1. **Every local system prompt.** `profile_block()` follows the identity block in the
   agent loop and in the plain chat branches. It tells the model the user's name,
   goals, struggles, people, tone and boundaries, with rules: use the name; match the
   tone and length; mention a struggle only when it helps right now and never as a
   reproach; names listed are people the user cares about, which never changes who
   Wisp alerts about (proactive attention decision P4 stands).
2. **The morning brief.** The greeting uses the name. A deterministic **Keeping in
   mind** section follows it: the goal in focus (rotates daily), people from the
   profile who appear in today's events, mail or texts, and at most two struggle-aware
   observations that fire only on a real signal (a packed calendar when the user said
   they overcommit, slipped reminders when they said they procrastinate, waiting mail
   from people when they said the inbox piles up, a clear day when they said focus is
   hard). No model writes any of it, matching the brief's existing rule.
3. **Today → Needs you.** An item whose title or quote names a profile person is
   raised to priority 1 with the reason "From Mom, who you said matters to you."
   Notifications are unchanged.
4. **A gentle revisit.** After 45 days without a change, `status.stale` is true and
   the Memory window's About you tab says so. No nagging in the notch.

### The flow, in the notch

`KnowYouView` replaces the tab content of the notch panel. Three questions, under a
minute (user decision 2026-10-17: "a user would not spend the time" on more): what to
call you, what you're working toward (select all that apply from ten descriptive cards such as School, Work and career, Health and fitness, each storing a plain goal sentence, plus an optional line in your own words), how you like to be talked to. Struggles, people
and boundaries are no longer asked up front; About you edits them and the check-ins
below are where Wisp learns them. One question per screen, with a one-line "why I ask".
Back, Skip and Continue on every screen; progress is saved after every step, so closing
the panel loses nothing. The footer always reads "Stays on this Mac · Change anytime in
Memory". The last screen shows exactly what Wisp will now do with the answers
(`GET /profile` → `preview`), so the user sees the information put to work before
they leave the flow.

Entry points: offered once on first launch after setup is ready (and once more three
days later if the user chose Later, then never again on its own), the `/about` slash
command, the menu bar's "Tell Wisp About You…", and Memory → About you → "Revisit in
the notch". About you also edits every field inline and erases the profile.

### The daily loop: check-in, carry-forward, goal pulse

`service/assistant/checkin.py`, `checkin_api.py`, `CheckInView.swift`. Goal-oriented
and tap-only, the way a chief of staff actually works:

- **Evening (from 17:00 local).** Today's card: "You planned 4 things today." Each
  active task has Done / Tomorrow / Drop. Tomorrow moves the task (pin cleared, a
  missed deadline cleared, `carried_from` set to the first planned day); Drop dismisses
  it. Then tomorrow's events with Keep / Move / Cancel. Cancel cancels: the row becomes an
  Undo bar for five seconds ("Cancelled · its N invitees will be notified · Undo" when the
  synced row has invitees), then `POST /assistant/checkin/cancel_event` (`source_id`, the
  row's `when_ts`, the row's `invitee_count`) removes exactly that occurrence through the
  app's EventKit handler and reads back that it is gone (`CheckInCancel.swift`). The wait
  lives on the shared `CheckInModel`, not the view: closing the panel or switching tabs
  neither drops the cancel nor hurries it, Undo is back if the card reopens inside the
  window, and quitting Wisp inside the window drops it (nothing cancelled). Refusals show
  the service's message in the row (404 gone, 403/409 not the user's to cancel or invitees
  not synced, 409 not current, 409 unknown "check Calendar" with no retry, 503 app not
  connected, 502 failed). Any answer that cannot prove nothing happened is uncertain: a 409
  unknown, no answer within 70 s (the service may wait 8 s + 45 s on the app), a connection
  dropped mid-request, or an error without the service's structured outcome; the row then
  reads "Check Calendar" with no tap target, so a second native cancel is never one tap
  away. Try again appears only when nothing was cancelled (not current, app not connected,
  failed, service unreachable, row changed) and restarts the Undo window. Rows whose
  `cancel_blocked` is set show "Cancel in Calendar" with the reason on hover. Move opens an inline sub-panel in the card: up to three times Wisp found free
  the way the Day tab plans (`GET /assistant/checkin/move_suggestions`: working hours,
  calendar, planned tasks, learned busy and away windows; deterministic, no model call),
  each with a short reason, plus "Other time…" with a day and time picker. Picking one
  opens Ask with "Move … to …"; nothing changes from the card itself. The scheduler posts one
  notification when the card becomes due and the app is listening; "Skip tonight" or
  "Done for today" completes it. Decisions are recorded in `~/.moe/checkins.json`
  (60 days), which also answers "how many evenings has this been moved".
- **The Day timeline's ⋯ menu.** Every Calendar block the user can change offers the
  same Cancel and Move, under the card's rules: `GET /assistant/today` annotates each
  Calendar block with `source_id`, `all_day` and the card rows' facts (`invitee_count`,
  `organizer_is_user`, `calendar_writable`, `cancel_blocked`, `move_blocked`; one helper,
  `checkin.occurrence_fields`). "Cancel event" starts the same 5-second Undo on the one
  shared queue (`CheckInModel.shared`), with Undo and the outcome on the block itself;
  "Move…" opens the same sub-panel (`CheckInMovePanel`) above the lane. A read-only
  calendar gets a disabled "On a read-only calendar" line and "Open in Calendar ↗";
  someone else's event gets "Decline in Calendar ↗"; any other block on Cancel shows the
  service's reason and Open. Events that are over, and blocks an older service didn't
  describe, get only Open (`CheckInCancel.menuEntries`). Each surface shows and
  reconciles only the cancels it started, so a timeline cancel never pops the card.
- **Morning.** `carry_forward` moves every still-active task from an earlier day onto
  today (scheduler tick and the first Today load, idempotent), unless it has aged out:
  a task keeps an `origin_ts` from the first day it was planned (never later than a
  same-titled reminder's due or creation time), and from seven days on it is left on
  its last day instead of carried, the same rule the user set for stale reminders. A
  task whose title matches an undated reminder the stale policy has stopped using is
  left behind too. Nothing is deleted; `checkins.json` lists what aged out and why. The brief's Keeping in
  mind says "2 tasks carried over from before: …", and Needs you's reason reads
  "Carried over from 2026-10-16."
- **Weekly goal pulse.** One goal, one tap: On track / Behind / Done, due seven days
  after the goals were written or last answered. Behind makes that goal the one the
  brief leads with; Done retires it from the rotation. Stored in the profile as
  `goal_status` and `goal_pulse_at`.

```
GET    /assistant/checkin?day&timezone     → card (runs today's carry-forward first)
POST   /assistant/checkin/tasks/{id}       {decision: done|tomorrow|drop, day, timezone}
POST   /assistant/checkin/complete         {day, timezone}
POST   /assistant/checkin/carry_forward    {day, timezone}
POST   /assistant/checkin/goal             {goal, status}
```

### API

```
GET    /profile            → {profile, status, preview}
PATCH  /profile            → merge validated fields; same payload
POST   /profile/complete   → mark the flow finished
POST   /profile/dismiss    → "Later"
DELETE /profile            → erase
```

## Validation

- `tests/test_user_profile.py`: store round trip, bounds, prompt block, brief
  section, Today ranking, offer logic, API.
- `tests/test_checkin.py`: due window, decisions, carry-forward idempotence, goal
  pulse cadence and statuses, the brief's carried line, API.
- `tests/KnowYouChecks.swift`: step order, skipping, progress and patch building,
  compiled with `app/Sources/WispApp/KnowYouFlow.swift` (registered in
  `scripts/run_simulation_qa.py`).
- `swift build` of the app package.
