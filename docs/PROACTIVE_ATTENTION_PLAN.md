# Wisp Proactive Attention — Plan

**Status:** Slice 0 built (this PR); Slices 1+ not started. Base: `origin/main` @ `21903b6`.

**Goal.** Wisp reads Messages and Mail (and later Canvas) in the background and catches the
thing you haven't put on your calendar yet, before you'd have to ask. Few alerts, each one
checkable against a source quote.

## Decisions (user, 2026-10-08)

| # | Decision |
| --- | --- |
| P1 | **Separate track** from the Browser program; consumes A02/A08/A09 unchanged. |
| P2 | **The notification rule.** Alert only when a text or email states something **coming up soon** that is in **neither Calendar nor Reminders**. Wisp then **adds the reminder** and alerts. Example: "Mom: meet me in the Quad at 6PM" → not on file → reminder added → alert. Nothing else notifies. |
| P3 | **Promotional and scam messages are excluded**, always. A false alert from one is the worst failure. |
| P4 | **Importance is derived from recent two-way contact:** who you last texted and who last messaged you. No VIP list. |
| P5 | **Sent mail:** not known to be cached (no evidence in the Swift readers). Unanswered-email detection is therefore off the table; under P2 it is no longer a notification trigger anyway. |
| P6 | **Start with Slice 0** (labelling and measurement) before any detector. |

What P2 changes in the earlier draft: v1 has exactly **one notifying reason code**,
`uncaptured_commitment` (stated, soon, not on file). The other detectors in Slice 2
(`conflict`, `unanswered_ask`, `deadline_unplanned`, `stale_followup`) are demoted to
Today/brief-only and ship off by default. They cannot interrupt you.

**Non-goals (v1).** No sending, no changing existing events or reminders, no browser/Canvas
navigation, no cloud models. The one effect Wisp may perform on its own is **creating a
reminder for an uncaptured commitment** (P2), through the existing verified path
(`add_reminder` → native `create_reminder` with readback, `verified_reminders.py`), never
by writing to Calendar and never silently: the alert says what was added.

---

## 1. Where this sits relative to what exists

Most of the plumbing is built. The judgment layer is not.

| Layer | State on `main` | Where |
| --- | --- | --- |
| Source sync (Calendar, Reminders, Mail, Messages, Notes) | Built; caches in `~/.moe/cache/` | `service/tools/*`, Swift readers |
| Commitments store, lead-time reminders, hub/SSE, notch chip | Built | `service/assistant/{store,reminders,scheduler,hub}.py` |
| Daily brief (deterministic, no model) | Built | `service/assistant/brief.py` |
| Day planner + Today UI (tasks, blocks, deadlines, unscheduled) | Built | `service/assistant/today*.py`, `TodayView.swift` |
| Structured Messages feed (A12 foundation) | Built | `imessage_tools.structured_messages_snapshot()` |
| Grounded obligation extraction (A08) and reconciliation (A09) | Built as **libraries**; quotes-only model schema, fail-closed | `service/discovery/{extraction,reconciliation,temporal}.py` |
| Discovery store, durable jobs, one-use approvals (A02/A03) | Built, not driven by any runtime | `service/discovery/{store,jobs,approvals}.py` |
| **Ingestion from Mail/Messages into discovery** | **Missing** (A11 is a MIME parser only) | — |
| **Detectors, ranking, interrupt budget** | **Missing** (this is A22) | — |
| **"Needs attention" in Today, brief and notifications** | **Missing** | — |

The existing Browser program (`docs/BROWSER_ASSISTANT_IMPLEMENTATION_PLAN.md`) reaches
proactivity (A22) only through its critical path **A05/A06 → A08 → A09 → A10 → A15 → A16 →
A17 → A22**, which needs a working Chrome/Safari bridge first. This plan is a **re-sequencing
of that program**: reach a useful A22 from Mail + Messages + Calendar, which have no
browser dependency, and let Canvas/browser sources plug into the same pipeline later.

**Decided (P1):** a separate track that consumes A02/A08/A09 unchanged. The program table
gets a one-line pointer so the browser work is not re-gated.

## 2. Architecture

```
 Calendar ─┐                         ┌─ deterministic detectors ─┐
 Mail ─────┼─▶ Ingest ─▶ Observation ┤                           ├─▶ AttentionItem ─▶ Rank ─▶ Surfaces
 Messages ─┘   (new)     (A01/A02)   └─ A08 model extraction ────┘     (evidence,       (score,   Today section
                                          (only on prefiltered)         reason, state)   budget)   Brief section
                                                                              ▲                     Notification
                                                       A09 reconcile ────────┘                     Notch chip
                                                       user feedback (confirm / dismiss / snooze) ◀──┘
```

New package `service/attention/` (kept out of `service/discovery/` so A-series PRs don't
overlap with it):

- `ingest.py` — turns cache rows into `SourceObservation`s with stable source IDs and revisions.
- `detectors.py` — pure functions, no model. One detector = one reason code.
- `extract.py` — thin driver around `discovery.extraction` for the model-judged remainder.
- `rank.py` — scoring, dedupe, interrupt budget, quiet hours.
- `feedback.py` — records confirm/dismiss/snooze; adjusts per-sender weights with counters (no model).
- `api.py` — `/assistant/attention` endpoints, mirroring `today_api.py`.

Storage: additive tables on `assistant.db` through `discovery.store` (items and evidence
already exist there). No new database.

## 3. Principles carried over (do not weaken)

1. **Code first, model last.** A detector that is plain code beats a model call. The model
   only handles what code can't (free-text asks, implied deadlines).
2. **Grounded or absent.** Every item carries a verbatim source quote located by code
   (A08's rule). No quote, no item. The model never supplies IDs, offsets, or timestamps.
3. **Fail closed.** Ambiguous date, competing dates, or an unresolvable sender → item is
   shown as "unconfirmed" or dropped; never a confident alert.
4. **Explain every alert.** Reason code + quote + the rule that fired. The "why" line is
   composed by code from the reason code, not written by a model (the brief's no-model rule
   exists because model-written briefs misattributed the user's own messages).
5. **Identity-aware.** Anything that reasons about "you" injects `service/identity.py`, or other
   people's news gets reported as the user's. Direction (sent vs received) comes from the
   structured feed, not from the model.
6. **Restored ≠ fresh.** Only a current-launch read counts as ready (`messages_sync_state()`,
   brief hold-back). Cached rows from a prior launch can seed but never trigger an alert.
7. **Quiet by default.** An assistant you mute is dead: hard daily interrupt cap, quiet hours,
   dedupe through the hub's `dedupe_key`.
8. **Read-only.** Nothing in this track writes to any source or sends anything.

## 4. Slices

Each slice is one worktree PR from a fresh `origin/main`, with a single owner for its paths.
Sizes: S ≈ a day, M ≈ 2–4 days, L ≈ a week+.

### Slice 0 — Ground truth and replay harness (M) · **built**

You can't tune alerts without knowing what "should have been caught."

**Delivered**

- `scripts/attention_snapshot.py` freezes the Messages and Mail caches plus the commitments
  table into `~/.moe/attention/snapshots/<stamp>/` (0700/0600, hash-checked on load,
  refuses to write inside the repo). Read-only on every source.
- `scripts/replay_attention.py` with four commands: `sample`, `label`, `status`, `report`.
- `service/attention/{corpus,labels,evaluate}.py`: the snapshot reader, the label store
  (append-only, last write wins) and the scorer.
- The labelling question, one per item: *if Wisp had seen this when it arrived, should it
  have interrupted me?* Keys: **m** missing (soon, not on file, should alert) · **c** known
  (soon, already on file) · **l** later (not soon) · **n** nothing · **p** promo · **s** scam
  · **u** unsure. The screen shows the preceding thread and anything already on your
  calendar/reminders within 48h, so "known" vs "missing" is decidable at a glance.
- The sample has three strata: **cue** (texts with a time/place cue, all of them), **control**
  (a random draw of texts *without* a cue, which measures what the cue prefilter misses),
  and **mail** (subjects with a cue). The report scales the control's miss rate to the whole
  rejected population.
- Two baseline predictors (`never`, `cue_baseline`) so the harness can score anything.
  `cue_baseline` is the floor a real detector must beat; its false alerts are broken out by
  label (`promo`, `scam`, `known`, `later`, `none`).
- Reports contain counts and item ids only, never message text.

**Run it** (the only manual step is the labelling, about 177 items on current data):

```bash
python scripts/replay_attention.py label      # resumable; q to stop, b to go back
python scripts/replay_attention.py report --errors
```

**Known limits.** Mail is header-only (sender, subject): the cache keeps full bodies for
about a week. Outgoing messages appear as context but are never candidates, so a commitment
*you* made by text ("I'll be there at 6") is not scored. "On file" is judged from the
commitments table as it is now; a reminder deleted since the message arrived is invisible.

*Owns:* `service/attention/{corpus,labels,evaluate}.py`, `scripts/{attention_snapshot,replay_attention}.py`,
`tests/test_attention_slice0.py`, `test_fixtures/attention/`.

**Design note.** The harness deliberately does not import `service.tools` or
`service.assistant`: importing either constructs the live `AssistantStore` against
`~/.moe/assistant.db`. A test pins this, and another pins the local mail parser to
`email_tools`' output. Later slices that need the store should receive it by injection.

### Slice 1 — Ingest Mail and Messages (M)

- Messages: consume `structured_messages_snapshot()` (conversation, sender, direction, time,
  GUID). Source ID = message GUID; revision = content hash.
- Mail: consume `email_tools.header_rows()` plus the raw-mail cache for bodies of candidates
  only (bounded). Source ID = account + RFC Message-ID. Reuse the A11 MIME parser.
- Set `coverage` honestly (`partial` unless the sync proves otherwise). Absence is never
  completion (A09 rule).
- Honor sync state: no ingestion trigger from restored rows.
- Sent mail is not cached (P5): the Swift reader exposes direction for Messages but nothing
  equivalent for Mail, so "have I already replied?" is answerable for texts only.

*Owns:* `service/attention/ingest.py`. *Depends on:* A12 foundation (merged), A11 parser (merged).
*Overlaps:* A11/A12 follow-ups — coordinate, don't duplicate.

### Slice 2 — Deterministic detectors (M)

All pure code, unit-testable with synthetic fixtures.

**The notifying detector (v1):**

| Code | Fires when | Inputs |
| --- | --- | --- |
| `uncaptured_commitment` | A text/email from a real person states a time (via `discovery.temporal`) or a time-and-place cue within the "soon" window, **and no Calendar event or Reminder matches it** | Messages, Mail, commitments store |

Matching against what is already on file is the heart of it and must err toward *not
alerting*: a title/time/place near-match counts as known (A09's source-first,
similarity-second reconciliation applies). The "soon" window starts at 48h, a placeholder
until Slice 0's `later` labels show where your real boundary is.

**Exclusions, applied before anything can alert (P3):** promotional and automated senders
(reuse the brief's human-vs-automated split), anything matching a scam screen (look-alike
links, urgency plus a payment or verification ask, unknown short-code senders), and senders
you have never exchanged a message with. A false alert from one of these is the worst
failure in Slice 0's report.

**Demoted to Today/brief only, off by default (P2):**

| Code | Detects |
| --- | --- |
| `conflict` | Overlapping or back-to-back events |
| `deadline_unplanned` | Known deadline with no scheduled block |
| `schedule_change` | Text references an existing commitment with a *different* date (uses A09) |
| `unanswered_ask` | Inbound request with no reply after N hours (Messages only; Sent mail isn't cached, P5) |

**Built (core, offline).** `uncaptured_commitment` exists as a pure function over a frozen
snapshot, scored by the Slice 0 harness (`replay_attention.py report --predictor
uncaptured_commitment`). It is a fixed sequence of gates; the first "no" is recorded as
`blocked_by`, and the report shows `missed_by_gate` so every labelled miss names the gate that
caused it:

| Gate | Module | Rule |
| --- | --- | --- |
| direction | `detectors` | Incoming only |
| screen | `exclusions` | Promo, scam, automated senders never alert (P3) |
| contact | `contacts` | Recent two-way contact, computed as of arrival, never from later messages (P4) |
| resolve | `resolve` | A concrete time read the way a person reads a chat; every default it applies is recorded in `inferred` |
| soon | `detectors` | Starts within 48h of arrival (date-only: within 2 calendar days) |
| confirmed | `detectors` | A question or guessed meridiem alerts only if the user replied in-thread within 6h without declining |
| on_file | `matching` | Not already in Calendar or Reminders; errs toward "known" |

Things the resolver deliberately refuses: picking between competing times, treating "in 20
minutes" as a plan, resolving a cancelled or past-tense clause. It also corrects one
shortcoming of the shared parser for chat: "7:30" with no am/pm is read as 07:30 with no
ambiguity flag, which would turn "dinner tomorrow at 7:30" into breakfast.

**Volume check on real data (pre-label, counts only).** Over 26.5 days the detector fires 15
times (0.57/day, all from texts), well under a cap of 3. Mail fires zero times: 169 of 400
headers are blocked at `unknown_mail_sender` (the name-match rule almost never links a mail
sender to someone you text) and 263 as automated. Mail therefore contributes nothing in v1
unless that rule changes; see open question 4. Precision is unknown until the labels exist.

**Known limits.** Undated Reminders are not in the commitments table, so they cannot match.
English only. The past-tense and decline word lists are short and untuned. "Soon" is a fixed
48h placeholder. Not wired to any live source and creates nothing.

*Owns:* `service/attention/{detectors,resolve,exclusions,contacts,matching,prediction}.py`,
`tests/test_attention_detector.py`. Detectors must not import the model client.

### Slice 3 — Model extraction for the remainder (M)

- Prefilter with cheap signals (human sender, temporal or request cue, not automated) so the
  model sees a small fraction of items.
- Drive `discovery.extraction` (quotes-only schema, fail-closed on ambiguity). Items from
  here are always `unconfirmed` and need Today confirmation (A08's stated residual risk).
- **Resource rules** (from prior measurements): the resident model is single-threaded and
  background work stalls foreground turns badly, so: run only when `idle.foreground_busy()`
  is false, one request at a time, abort on foreground arrival, hard per-scan item cap, and
  cap context/`max_tokens` (KV cost is context-driven, not weight-driven). Measure
  attribution transport overhead (~1.8 s/call) when sizing the scan budget.
- Later option: move this pass to the mini (see `moe-mini-proactive-node`); the interface
  stays the same.

*Owns:* `service/attention/extract.py`. *Depends on:* Slice 1.

### Slice 4 — Ranking, budget, feedback (M)

- Score = time-until-event × sender importance × confidence. **Sender importance is
  recency of two-way contact (P4):** when you last sent to them, when they last sent to you,
  weighted so a reply from you in the last few days outranks a one-way blast. Computed from
  the Messages feed (direction is already there) with no model. A sender with no outgoing
  message from you and no history never reaches the notifying path.
- Hard caps: interrupts/day (proposal: 3; your call), quiet hours, one notification per item
  per state change.
- Feedback (confirm / dismiss / snooze) updates per-sender weights with plain counters.
  A dismissal is remembered per source revision and is never a hard delete (browser program
  D5 semantics).
- **Shadow mode** is built in here: score and log, don't notify.

*Owns:* `service/attention/{rank,feedback}.py`. *Depends on:* Slice 2.

### Slice 5 — Surfaces (M)

- **Today:** new "Caught for you" section next to the existing deadline/unscheduled lists,
  each row showing the source quote, reason, and Confirm / Correct / Dismiss / Snooze.
  Same pattern browser decision D5 already approved for "From your pages."
- **Brief:** a "Needs attention" section rendered deterministically (quote from source,
  reason from code). Keeps the no-model brief rule.
- **Delivery:** a hub event `attention` through the existing SSE path, with
  `AssistantDelivery` receipts so a missed event replays. Reuse notch chip and native
  notifications.
- Swift work is the only part needing the app build; use the known SwiftUI macro
  plugin-path workaround.

*Owns:* `service/attention/api.py`, `TodayView.swift` section, brief section, delivery type.
*Overlaps:* `brief.py`, `TodayView.swift`, `scheduler.py` are shared files — serialize.

### Slice 6 — Reminder creation and day-plan integration (L)

- **Creating the reminder (P2)** is the first effect in this track, so it gets its own gate.
  It goes through `add_reminder` → native `create_reminder` with verified readback
  (`verified_reminders.py`), keyed by a deterministic action id derived from the source
  message GUID so a retry or a restart can never create a second one. Re-check "not on file"
  immediately before writing, since the user may have added it in the meantime.
  An uncertain outcome is reconciled, never retried blind.
- The alert states what was added and the source quote, and offers one-tap **Undo** (complete
  or delete the reminder it created, and only that one).
- Reminders only; never Calendar events, and never an edit to something that already exists.
  Calendar writes wait for A19/A20.
- Confirming a lower-confidence ("unconfirmed") item can create a Today task with a labelled
  effort estimate; `replan` places it. A morning proposal: "here's the plan; 3 things I caught."
- This is where A17 (multi-day Today) pays off; do a single-day version first.

### Slice 7 — Shadow run, then graduate (S + calendar time)

- Run in shadow mode for ~2 weeks. Each day the user marks hits and misses from the shadow log.
- Graduate a reason code to live notifications only when its precision clears the
  threshold below. Everything else stays in Today/brief only.

## 5. Order and parallelism

```
Slice 0 ─┬─▶ Slice 1 ─▶ Slice 3 ─┐
         └─▶ Slice 2 ────────────┼─▶ Slice 4 ─▶ Slice 5 ─▶ Slice 6 ─▶ Slice 7
```

Slices 1 and 2 can run in parallel after Slice 0 (disjoint files). Slice 5's Swift UI can
start against a stub API once Slice 4's response shape is fixed. First user-visible value is
at **Slices 1 + 2 + the reminder-creation core of Slice 6 + a minimal alert**: the
rule-based `uncaptured_commitment` detector already handles the clear cases ("meet me in the
Quad at 6PM") with no model in the loop. Ship that first, in shadow mode, and treat Slice 3
(model extraction for vaguer phrasing) as the riskier second wave.

## 6. Validation

- **Unit:** each detector with synthetic fixtures including adversarial ones: user's own
  outgoing message, other people's news, quoted reply chains, forwarded mail, DST edges,
  competing dates.
- **Replay:** `scripts/replay_attention.py` precision/recall per reason code on the labeled
  snapshot (Slice 0). A slice that lowers an existing code's precision doesn't merge.
- **Initial live-graduation thresholds (proposal, to be tuned with real data):** precision
  ≥ 80% over ≥ 30 shadow items per reason code, and no alert in the "wrongly attributed
  to the user" class.
- **Safety:** isolated backend on port 8775 with a sandbox home, never the production port;
  no live replay against 8765. If `router.py` is touched, run the standing router checks.
  Python tests via the project's replay scripts (a bare `pytest tests/` runs zero tests).
- **Delivery gates:** per `AGENTS.md`: exact-SHA mechanical evidence, required CI, and an
  independent Auditor review for this track (persisted data, outbound-adjacent surfaces,
  cross-component behavior). After any `service/` change, repackage and relaunch Wisp.
- **Process:** enable the CI monitor on every PR; never auto-merge.

## 7. Risks

| Risk | Mitigation |
| --- | --- |
| False alerts destroy trust | Reason-coded detectors first, shadow mode, per-code graduation, daily cap |
| Small local model misreads mail | Quotes-only schema, code locates quotes, always `unconfirmed`, prefilter |
| Misattributing the user's own messages | Direction from structured feed; identity injection; adversarial fixtures |
| Mac asleep / app closed → late scan | Sources backfill on wake; scan catches up, labeled "caught at <time>"; mini scheduler later |
| Scans stall interactive chat | Idle-gated, one request at a time, foreground abort, item caps |
| Sensitive text persisted in evidence | Store minimal quotes; retention like browser D6 (raw text 30 days); never in logs or problem reports |
| Overlap with Browser program A-series | Separate package; consume A02/A08/A09 unchanged; coordinate A11/A12 follow-ups |
| Restored cache treated as fresh | Trigger only on current-launch sync state |

## 8. Definition of done (v1)

- A text or email from a person you actually talk to says "meet me at 6" and it is on
  neither Calendar nor Reminders: a reminder appears (verified, created once), you get one
  alert naming it and quoting the source, and Undo removes exactly that reminder.
- If it is already on your calendar or reminders, nothing happens. Promo and scam messages
  never alert. Measured on your own labelled history, not assumed.
- Conflicts and unplanned deadlines appear in Today and the brief only; they never interrupt.
- The morning brief has a "Needs attention" section with at most a handful of items, each
  with source and reason.
- Dismissing an item is remembered; it doesn't return unless its source changes.
- Shadow-run precision data exists for every live reason code.

## 9. Open questions for the user

Resolved 2026-10-08: track placement (P1), importance signal (P4), Sent mail (P5), and the
notification rule (P2/P3). Still open:

1. **Interrupt budget and quiet hours.** Proposal: at most 3 proactive alerts a day, quiet
   overnight. Your call; Slice 4 needs a number.
2. **What "soon" means.** Placeholder is 48h. The `later` labels from Slice 0 will show where
   your real boundary is (a 6PM-today meetup and a next-Friday dinner are different asks).
3. **Reminder time.** For "Quad at 6PM", is the reminder at 6PM, or ahead of it (travel
   time)? Proposal: an alert at event time minus a default lead, with the event time in the title.
4. **Mail scope for v1.** Mail is header-only today (subject + sender). Is subject-level
   enough to start, or is body access (a week cached) worth pulling into Slice 1?
5. **Canvas.** Stay on the browser program's path, or add the Canvas ICS feed as a cheap
   early source here?
6. **Mac asleep at 6PM.** Reminders ride iCloud/EKReminder so the alert fires even when
   Wisp is not running; confirm that is the behavior you want for alerts created this way.
