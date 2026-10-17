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

`KnowYouView` replaces the tab content of the notch panel. One question per screen,
with a one-line "why I ask", chips for the common answers and a free-text field. Back,
Skip and Continue on every screen; progress is saved after every step, so closing the
panel loses nothing. The footer always reads "Stays on this Mac · Change anytime in
Memory". The last screen shows exactly what Wisp will now do with the answers
(`GET /profile` → `preview`), so the user sees the information put to work before
they leave the flow.

Entry points: offered once on first launch after setup is ready (and once more three
days later if the user chose Later, then never again on its own), the `/about` slash
command, the menu bar's "Tell Wisp About You…", and Memory → About you → "Revisit in
the notch". About you also edits every field inline and erases the profile.

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
- `tests/KnowYouChecks.swift`: step order, skipping, progress and patch building,
  compiled with `app/Sources/WispApp/KnowYouFlow.swift` (registered in
  `scripts/run_simulation_qa.py`).
- `swift build` of the app package.
