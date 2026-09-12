# Wisp Assistant Framework — Architecture

**Goal:** evolve Wisp from a chat wrapper over local models into a *personal assistant* —
one that watches your calendar, email, and Canvas, knows what's coming up, reminds you at
the right moment, and shows a tidy countdown in the notch. Designed 2026-07-12, informed
by the full-path diagnostic (`tests/diagnostic_2026-07-12.md`).

**Decisions already made (with the user):**
- Background collectors run on the **MacBook Pro backend** (the Air stays an opportunistic
  accelerator — diagnostic F4 showed it cannot be a dependency).
- Canvas via **ICS calendar feed** (school blocks API tokens).
- Email connector is **pluggable** (Gmail IMAP and Mail.app drivers; pick at build time).

---

## ✅ A1 BUILT (2026-07-12)

The store + reminder engine + endpoints + notch countdown chip + native notifications are
implemented and verified live. One design change from the original plan, forced by macOS
**TCC** (Calendar permission is per-process): the calendar is read in the **Swift app**
(`CalendarReader.swift`, EventKit, clean Wisp.app identity + Info.plist usage strings) and
pushed to the backend via `POST /assistant/sync/calendar` — NOT read by the Python backend
(a separate binary that can't get a coherent "Wisp" prompt). `connectors/calendar_mac.py`
remains as reference but is unwired. Everything else matches the design below.

**Ships working today** (manual commitments + reminders + chip); **calendar events flow once
the user grants** Calendar access to Wisp (prompt appears on launch). Endpoints:
`/assistant/next|upcoming|status|events(SSE)`, `/assistant/commitments(+/{id})`,
`/assistant/sync/calendar`. Remaining phases: A3 mail+extractor, A4 morning brief.

---

## 1. Design principles

1. **Local-first, read-only.** Connectors only *read* external sources. Wisp never sends
   email, never writes to Canvas, never syncs data out. Secrets live in the macOS
   Keychain, never in YAML.
2. **Glanceable before conversational.** The notch is the surface. The single most
   valuable pixel is a countdown chip you see without asking anything.
3. **Proactive but quiet.** Reminders fire at deliberate lead times, deduped, with quiet
   hours. No notification spam — an assistant you mute is dead.
4. **Degrade gracefully, everywhere.** Any connector failing = stale data, never a broken
   app. The Air being reachable = faster extraction, never a requirement.
5. **The LLM is a component, not the architecture.** Parsing ICS feeds and calendar APIs
   is deterministic code. The LLM is reserved for what only it can do: pulling structured
   events out of unstructured email, and writing the morning brief.

---

## 2. The one new core concept: Commitments

Everything the assistant tracks normalizes into a **commitment** — a thing with a time.
Assignments, exams, meetings, flights, deadlines mentioned in email: one table, one shape,
one rendering pipeline. This is the keystone; every connector produces commitments, every
surface (chip, notifications, brief, chat tools) consumes them.

SQLite at `~/.moe/assistant.db`:

```sql
CREATE TABLE commitments (
  id            TEXT PRIMARY KEY,           -- uuid
  source        TEXT NOT NULL,              -- 'calendar' | 'canvas' | 'mail' | 'manual'
  source_id     TEXT,                       -- upstream uid (dedupe/update key)
  kind          TEXT NOT NULL,              -- 'event' | 'assignment' | 'exam' | 'meeting' | 'reminder'
  title         TEXT NOT NULL,
  context       TEXT,                       -- course name, sender, calendar name
  starts_at     TEXT,                       -- ISO8601; events/meetings
  due_at        TEXT,                       -- ISO8601; assignments/exams
  location      TEXT,
  url           TEXT,                       -- deep link (Canvas assignment, email msg)
  notes         TEXT,
  status        TEXT DEFAULT 'active',      -- 'active' | 'done' | 'dismissed'
  confidence    REAL DEFAULT 1.0,           -- 1.0 deterministic; <1.0 = LLM-extracted
  extracted_from TEXT,                      -- provenance snippet for LLM extractions
  created_at    TEXT, updated_at TEXT,
  UNIQUE(source, source_id)
);
CREATE TABLE notify_log (                   -- dedupe: one row per (commitment, stage)
  commitment_id TEXT, stage TEXT, sent_at TEXT,
  PRIMARY KEY (commitment_id, stage)
);
```

Upsert semantics: connectors re-poll and `INSERT OR REPLACE` on `(source, source_id)` —
a moved due date updates in place and re-arms its reminders.

---

## 3. Component map

```
                    ┌──────────────────────  MacBook Pro  ─────────────────────┐
                    │                                                          │
  Calendar.app ──▶ CalendarConnector ─┐                                        │
  Canvas ICS URL ─▶ CanvasConnector ──┼─▶ Commitments store (~/.moe/assistant.db)
  Gmail/Mail.app ─▶ MailConnector ────┘         ▲            │                 │
        (raw text)      │                       │            ▼                 │
                        ▼                       │      ReminderEngine ──▶ SSE ─┼─▶ Swift app:
                   Extractor (LLM) ─────────────┘      (lead times,           │   • notch countdown chip
                   gemma/gpt-oss local,                 quiet hours,          │   • native notifications
                   offloadable to Air                   notify_log)           │   • "Up Next" strip
                        ▲                                                     │   • morning brief card
                   Scheduler (asyncio loop,                                   │
                   per-connector intervals)                                   │
                    └─────────────────────────────────────────────────────────┘
```

New Python package:

```
service/assistant/
  __init__.py
  store.py          # commitments + notify_log CRUD (mirrors memory/store.py idioms)
  scheduler.py      # asyncio poll loop (mirrors idle_unloader.py pattern)
  reminders.py      # lead-time rules → notify events over SSE
  extractor.py      # LLM email→commitment extraction (strict JSON out)
  brief.py          # morning digest composer
  connectors/
    base.py         # Connector protocol: name, interval, poll() -> list[Commitment]
    calendar_mac.py # macOS Calendar via EventKit (PyObjC) — needs Calendar TCC perm
    canvas_ics.py   # fetch + parse the Canvas user ICS feed URL
    mail_imap.py    # Gmail/IMAP driver (app password in Keychain)
    mail_applescript.py  # Mail.app driver (no credentials; needs Mail running)
```

### 3.1 Connectors (deterministic layer)

**Calendar (`calendar_mac.py`)** — EventKit via PyObjC, next-14-days window, all visible
calendars (incl. subscribed/iCloud/Google accounts already in Calendar.app — which means
users often get Gmail *events* for free before the mail connector even exists).
`kind='event'|'meeting'`, `confidence=1.0`. Requires the **Calendar TCC permission** to
attribute to Wisp.app — same bundling consideration as Full Disk Access (memory §38):
the permission must be granted to the bundled service's responsible app.

**Canvas — NO dedicated connector needed (2026-07-12 user decision).** The user subscribes
their Canvas *Calendar Feed* (which carries assignment/quiz due dates AND announcements) to
their **Google Calendar**, and Google Calendar is already an account in macOS Calendar.app.
So Canvas commitments arrive through the **CalendarConnector for free** — no ICS parsing,
no separate poll, no token. This collapses the old Phase A2 into A1. The only added work:
the calendar connector tags events whose source calendar is the Canvas-synced Google cal as
`kind='assignment'|'exam'` (title heuristics: quiz/exam/midterm/final/due). If the user ever
wants richer Canvas data (grades, submission state) later, add a real connector then;
unnecessary for the assistant experience they described.

**Mail (`mail_imap.py` / `mail_applescript.py`)** — two drivers behind one interface;
selection deferred per user decision. Both do the same thing: fetch recent unseen
messages (last 48h, INBOX), pass *subject + sender + first ~2KB of body* to the
Extractor. IMAP driver: app password stored via `security add-generic-password` (Keychain),
read-only (`BODY.PEEK`, never marks read). AppleScript driver: no credentials but requires
Mail.app running. **The mail connector is the only one that needs the LLM.**

### 3.2 Extractor (the one LLM stage)

`extractor.py` prompts a local model for **strict JSON**:

```
System: Extract commitments (deadlines, exams, meetings, events) from this email.
Reply ONLY with JSON: {"commitments":[{"kind":..., "title":..., "when_iso":...,
"context":...}], "none_found": bool}. Do not invent dates. Ignore marketing.
```

- Runs on the **fast model** (gemma) first; anything ambiguous can escalate to gpt-oss.
- Never blocks a user request: extraction jobs queue and run only when the service is
  idle (reuse `service/idle.py` last-used signals), so mail polling never causes a model
  swap mid-conversation.
- Results carry `confidence<1.0` and `extracted_from` provenance. Low-confidence items
  are **not auto-reminded** — they surface as a confirm chip in the UI ("Found: *CS
  midterm Oct 12* in email from Prof. Lee — track it?"). One tap promotes to
  `confidence=1.0`.
- **Air offload hook:** this stage is exactly the `summaries` capability stubbed in
  `air_compute.capabilities`. When the Air is reachable, POST the email batch to it;
  when not, run locally. Zero behavior difference, just placement.

### 3.3 Scheduler

Same pattern as `idle_unloader.py`: one asyncio task started in FastAPI lifespan.
Intervals: calendar 5 min, Canvas ICS 30 min, mail 15 min — each jittered ±20%, with
exponential backoff per-connector on failure (a dead ICS URL must not log-spam). Every
poll is wrapped so one connector's exception never touches the others.

### 3.4 ReminderEngine

Pure function of (commitments, notify_log, now, rules):

| kind | stages (before due/start) |
|------|--------------------------|
| exam | 1 week, 1 day, 3 hours |
| assignment | 1 day, 3 hours |
| meeting/event | 30 min, 10 min |
| reminder (manual) | at time |

- Each fired stage inserts into `notify_log` — restart-safe dedupe.
- Quiet hours (default 23:00–08:00): non-urgent stages hold until morning and fold into
  the brief instead.
- Emits over a new SSE channel `/assistant/events`; the Swift app turns them into native
  `UNUserNotificationCenter` notifications (clicking one expands the notch panel).

### 3.5 Brief composer

`brief.py`: on first panel-expand after 5am (or on demand), compose from the store —
deterministic skeleton (today's events, due today/tomorrow, overdue) passed through the
resident model for one short friendly paragraph, rendered as a dismissible card atop the
transcript. No model swap: whatever is resident writes it; gemma is fine.

---

## 4. API surface (new endpoints on :8765)

```
GET  /assistant/next                 → the single soonest active commitment (chip data)
GET  /assistant/upcoming?days=7      → list for the "Up Next" strip / chat tool
GET  /assistant/brief                → composed morning digest (text + items)
GET  /assistant/events               → SSE: reminder + new-commitment + confirm-request events
POST /assistant/commitments          → manual add ("remind me at 4pm to call mom")
POST /assistant/commitments/{id}     → confirm / dismiss / done
GET/POST /assistant/connectors       → connector config + status (last poll, errors)
```

And three new **agent tools** in `service/tools/` so chat becomes assistant-aware:
`get_upcoming(days)`, `add_reminder(title, when_iso)`, `search_commitments(query)`.
The rewritten router (2026-07-12) already sends "remind me…", "check my calendar",
"what's due" style prompts to the agent path via the fail-safe tool gate + the
`calendar`/`assignment`/`deadline` system nouns; these tools make them answerable from the
store in milliseconds instead of a hallucination.

## 5. Swift UI (the visible 20% that makes it feel like an assistant)

1. **Notch countdown chip** — the "nice tidy timer". Collapsed bar gains a compact label:
   `⏱ CS101 Quiz · 2h 14m`, sourced from `/assistant/next`, ticking locally (no polling;
   re-fetch on SSE change events). Urgency color ramps (mint → amber <3h → red <30m).
   Tap = expand panel scrolled to the item.
2. **"Up Next" strip** — top of expanded panel: next 3 commitments as pills with relative
   times. Swipe/✓ to mark done.
3. **Native notifications** — from `/assistant/events`, with the app's tessera icon;
   action buttons *Done* / *Snooze 1h* / *Open*.
4. **Confirm chips** — low-confidence extractions ask before they track (§3.2).
5. **Brief card** — §3.5, once per morning.
6. **Settings → Assistant** — calendar connector toggle, mail driver picker + account
   (**deferred — user doesn't want email connected yet; ship the UI toggle disabled with a
   "Connect email later" affordance**), poll toggles per connector, quiet hours, per-kind
   lead-time steppers. No Canvas field needed (Canvas rides Google Calendar, §3.1).

## 6. Prerequisites — fix before building (from today's diagnostic)

1. **F1, blocking:** move user state out of the bundle. Add `~/.moe/config.yaml` as an
   overlay that `service/config` merges over `models.yaml` defaults; `set_role` /
   `set_air_compute` / all assistant settings write there. Repackaging stops destroying
   settings — connector config *cannot* live in a location that gets wiped.
2. **F2, blocking for trust:** stream agent-loop progress (per-step tool events already
   exist in `/agent` — surface "calling list_dir…" immediately, add heartbeats to agent
   steps, investigate the 139s step cost). An assistant that freezes for 2 minutes on
   "list my downloads" will never be trusted with "what's due tomorrow".
3. **SESSIONS clobber (handoff §7.2):** proactive features add background traffic
   alongside user chats; key in-flight state by request id, not session id, first.
4. Worth taking in passing: F5 pre-warm on notch hover; "MOE"→"Wisp" identity strings.

## 7. Build phases

| Phase | Scope | Outcome |
|-------|-------|---------|
| **P0** | Prereqs above (config overlay, agent streaming, SESSIONS fix) | Safe foundation |
| **A1** | Store + scheduler + CalendarConnector (**covers Canvas via Google Calendar**) + `/assistant/next|upcoming|events` + agent tools + notch chip + notifications | The countdown-timer ask AND "what's due this week?" — both from real calendar+Canvas data |
| **A2** | Brief card + manual reminders ("remind me…") + quiet hours + per-kind lead times | Feels proactive, not a poller |
| **A3 (deferred)** | Mail connector (pick driver) + Extractor + confirm chips + Air offload | Events discovered from email — only when the user opts to connect email |

Because Canvas rides the calendar, **A1 now delivers almost the whole experience** the user
described (calendar + homework/exam deadlines → reminders → notch timer). Email is the only
deferred piece, cleanly isolated in A3.

## 8. Privacy & safety posture

- All connector data stays in `~/.moe/` on this Mac. The only network calls are to *your
  own* accounts (IMAP/ICS over TLS) and optionally your own Air on LAN.
- Secrets (IMAP app password, tokenized ICS URL) go in the macOS **Keychain**, fetched at
  poll time; never written to YAML, never logged.
- Connectors are architecturally read-only; there is no "send email" or "submit to
  Canvas" tool, so no prompt-injection path from email content to outbound action. Email
  bodies fed to the extractor are treated as untrusted *data* — the extractor's output is
  schema-validated JSON, and anything else is discarded.
- Reminder actions (Done/Snooze/Dismiss) only touch the local store.

## 9. Open items (decide during build, none block P0/A1)

- Mail driver: Gmail IMAP (headless, needs app password) vs Mail.app (zero-cred, needs
  Mail running) — deferred by user (A3, "connect email later").
- Canvas: user syncs Canvas Calendar Feed → Google Calendar → macOS Calendar.app. Confirm
  the Canvas-synced Google calendar shows up in Calendar.app so EventKit sees it. No app
  code needed.
- EventKit TCC: granting Calendar access to the bundled service (test whether the perm
  attributes to Wisp.app cleanly; fallback is AppleScript against Calendar.app).
- Whether `exam` detection needs course-specific overrides ("lab" vs "lecture" titles).
