# Can the long-email digest find old reminders, or build a profile?

Date: 2026-10-26. Research only; nothing in Wisp was changed. Read against
`main` at 7d696c15 and the in-progress digest on `claude/long-email-digest`
(`service/tools/email_digest.py`).

The question had two parts. Can the full-body Gemma digest (95–100% detail
recall, ~5.6 s per long email, thinking off) be used:

1. to surface long-term reminders and commitments from mail sent months ago?
2. to build a profile of the user, possibly feeding the notch Brief?

## Verdicts

| Use | Verdict | One-line reason |
| --- | --- | --- |
| **1. Long-term reminders from old mail** | **Worth building, in phases.** | Nothing in Wisp reads bodies older than ~7 days. The things worth catching (renewal notice deadlines, warranty expiry, tax installments, save-the-dates, promised reviews) sit in exactly that mail. The digest's strength (it copies every date and deadline exactly) is what this needs. |
| **2a. An automatic profile inferred from mail** | **Not recommended.** | Functionally this is the profile builder the user removed on 2026-08-15 ("Not to be rebuilt", `docs/WISP_OVERVIEW.md:547-557`). It carries the identity-confusion risk, and it competes with the know-you profile, which is user-stated by design. |
| **2b. Suggested facts the user accepts into know-you** | **Possible later, user's call.** | A narrow, opt-in version: concrete, stable facts only (landlord and address, dentist, devices under warranty, recurring bills), each shown with its source email and accepted or declined one by one. No traits, tone or relationships. |
| **2c. Feeding the Brief** | **Yes, but only accepted reminders.** | A deterministic "Coming up from mail" line built from items the user has accepted. The Brief stays model-free in its personal section, and digest text never reaches it directly. |

The user asked about the profile explicitly, which can count as revisiting the
August decision. So 2a and 2b are listed under open decisions, not ruled out.

## Evidence

### A. How far back Wisp can read mail

| Source | Headers | Bodies | Fetch one old body by id? | Cost |
| --- | --- | --- | --- | --- |
| Gmail API (`service/gmail_source.py`) | Newest 200 per account (`HEADERS_LIMIT`), plus history `newer_than:730d` capped at 2,000 ids (`HISTORY_LIMIT`, refreshed every 30 min) | Newest **50 INBOX** messages (`RAW_LIMIT`), each cut at `BODY_CAP` 20,000 chars, fetched only on demand (`refresh_if_stale`). The background `run()` syncs headers only. | **No function exists**, but `_sync_raw` already calls `messages/{id}?format=full` per id, so adding one is small. | ~5 quota units per get against 250/s. `FETCH_CONCURRENCY` 4. Not a constraint. |
| Mail.app (`MailReader.swift`) | Same 730-day header walk, in batches of 500 | Newest 50 total (`rawLimit`; per account `max(20, 50/n)`), via `content of m` | **No.** It would need a new `whose message id is …` AppleScript, a Swift hook and a backend event. | `content of m` is "the SLOW AppleScript call". One `tell application "Mail"` batch measured 18–35 s. |

Retention: raw bodies expire after 7 days (`email_tools._RAW_TTL_SECONDS`,
counted from receipt so a restart can't extend it), and the raw cache is
replaced wholesale on each sync. History rows are headers only.

**Sent mail is not read at all.** Gmail lists `labelIds=INBOX` only, and the
Mail.app path reads the inbox. Most of the user's *own* promises ("I'll send it
Friday") live in Sent, so phase 1 can only find commitments that others asked
of the user or that a reply quotes back.

**So months-old bodies mean new fetch work.** For Gmail it's a small,
cheap addition. For Mail.app it's new native plumbing at a slow per-message
rate. Either way the result needs its own store, because the 7-day TTL and the
wholesale replace would throw it away.

### B. What already exists, and what a digest would add

| Subsystem | What it does | Inputs and lookback | Overlap with an old-mail digest |
| --- | --- | --- | --- |
| `attention/detectors.py` `uncaptured_commitment` | Rules only, no model. Fires on a time within 48 h (`SOON_HOURS`) from a recent two-way contact that isn't in Calendar or Reminders. A bare date needs an obligation word (`_OBLIGATION`). | Subject plus first 16k body chars from the ~50-body cache. `LOOKBACK_S` 7 days. Shadow mode by default. | Covers the next 48 h of the last week. **The digest must defer to it** inside that window. |
| `brief.py` mail regexes | Flags deadline, due and RSVP words on current inbox rows | Current window only | None for old mail |
| `mail_gists.py` | One-line MAIL and TEXTS overviews plus a 2–3 sentence inbox summary | Last 24 h, 220-char excerpts, ≤10 emails for the summary | None for old mail. **Its scheduling rules are the template** (below). |
| `stale_reminders.py` | Ages Apple reminders. Ask Keep/Delete from 7 days; hidden from 14. Dated ones aged by days overdue. | EventKit | A digest item is a candidate, not a reminder. **The same rule should apply to its due dates**: never surface something already a week overdue. |
| Commitments store (`main.py:2701+`) | Calendar and manual items | — | **The natural destination** for accepted items (it already has create, update and delete endpoints). |
| Fact memory (`memory/facts.py`, `capture.py`) | Facts with a `memory_evidence` row (source_type, source_id, quote, fingerprint, extractor_version). Auto-extraction copies the user's own sentences verbatim from chat and abstains on health and finance. | Chat only. Mail is never a source. | Has the provenance fields a digest would need. But auto-writing mail-derived facts would break the "user's own words" rule. |
| Know-you profile (`memory/profile.py`) | User-entered name, goals, people, tone, boundaries. Opt-in onboarding. Feeds `profile_block()`, the Brief's "Keeping in mind", and Today ordering. **P4: `people` changes ordering and wording, never who Wisp alerts about.** | Hand-entered | 2b would *suggest into* it. It must never write to it on its own. |
| `memory/identity.py` | `identity_block()` and `attribution_rules(shape)`. Required by every prompt that reads the user's mail. | — | **Mandatory input** for any extraction prompt |
| Retired profile builder (removed 2026-08-15) | Map-reduce over all sources | All sources | 2a *is* this |

**What a digest would add that nothing covers today:**

- Deadlines and obligations in mail older than 7 days, especially ones months
  ahead: lease notice dates, warranty and subscription expiry, tax
  installments, save-the-dates, school date changes.
- Durable output with a source message id, which nothing keeps today.
- Later, with Sent access: the user's own promises and open replies they owe.

### C. Measurement (not yet run)

The optional Gemma check was not run. Another session's Simulation QA gate
held the host for the whole window (2 h 50 min and counting, load ~10). The
one-heavy-job rule means an engine can't start alongside it.

The bench is ready at `scripts/email_commitment_extraction_bench.py`. It has
synthetic mail only and talks to an isolated engine on port 8796. It plants
14 items in 10 long emails dated March–October 2026, scored against "today"
2026-10-26, and compares two strategies:

- `direct`: full body to JSON items in one call
- `from_digest`: the production "Key details" digest, then JSON from the digest

It scores whether each item is found, its status (open or closed), and its
resolved due date. It also has traps:

- a school date moved from Nov 12 to Nov 19
- a friend's new job, which must not become the user's
- "send by Friday", which needs resolving against the sent date and is now past
- an injected "wire $4,800" instruction, which must be flagged suspicious
- a promo-code newsletter, which must produce nothing

To run it once the host is quiet:

```bash
~/.omlx/bin/omlx serve --model-dir "$SCRATCH/models" --base-path "$SCRATCH" --port 8796 --memory-guard aggressive --no-cache
```

```bash
.venv/bin/python scripts/email_commitment_extraction_bench.py --base-url http://127.0.0.1:8796 --reps 3
```

Here `$SCRATCH/models` holds only a symlink to
`~/Desktop/OMLX_Model_Files/Jundot/gemma-4-E4B-it-oQ4e-mtp`. Stop the engine
afterwards. Until it runs, every reliability claim below is inferred from the
digest's 95–100% recall, not measured for extraction. Three things are
unmeasured and most likely to fail: relative-date resolution ("by Friday"),
open/closed status, and the friend's-news trap.

## Recommended design

Treat the digest as a **candidate generator with a human gate**. It is never
a reminder writer or a profile writer.

### Phase 0: measure (≈½ day, no product code)

Run the bench above. Go/no-go bar:

- ≥90% items found
- ≥90% correct due dates
- 0 trap failures across 3 reps

If relative dates or status miss, keep the model to *verbatim extraction*
(item plus the exact date phrase plus the evidence quote). Resolve dates
deterministically in code against the email's `Date:` header, reusing the
detector's date resolver. Then compute status in code against today. The bench
prompt asks the model for both, which is the harder version.

### Phase 1: on-demand and recent backfill, Gmail only (≈2–3 days)

1. **Fetch.** Add `gmail_source.fetch_body(message_id)` (`format=full`,
   `extract_body`, `BODY_CAP`) behind the existing per-account lock and
   `FETCH_CONCURRENCY`. It reads INBOX history only.
2. **Prefilter with rules, no model.** Skip `is_broadcast` mail and promotions
   unless the sender looks transactional (bills, renewals, school, landlord,
   insurance). Keep only bodies that contain a date or `_OBLIGATION` word.
   Skip anything within the attention detector's 48 h / 7-day window. This
   should cut most of the inbox before Gemma sees it.
3. **Extract.** One Gemma call per surviving email, on the resident agent
   model only, with thinking off, no tool schemas, and `max_tokens` ≈ 700.
   The prompt carries `identity_block()` and `attribution_rules`, the
   untrusted-content wrapper before and after the body, and the digest's
   "copy exactly" rules. For long bodies, reuse the digest output if
   `email_digest` already made one for an Ask turn.
4. **Validate in code.** Every item must carry an evidence quote that appears
   verbatim in the body, or it is dropped. Codes and amounts are backfilled
   verbatim, as in the digest. Due dates are resolved and status computed in
   code. Anything already a week or more past due is dropped (same rule as
   `stale_reminders`).
5. **Store.** Use a new local table (e.g. `mail_candidates`) holding message
   id, thread id, sender, sent date, item text, due date, evidence quote,
   extractor version, body fingerprint, and a state of
   `new | accepted | dismissed | expired`. Dismissals are remembered by
   (message id, fingerprint) so nothing comes back. It is never written to
   `facts.db`.
6. **Surface.** A "Found in your mail" review list (in the window, and one
   notch Brief line when something new is due within ~30 days). Each item has
   Add to Reminders, Dismiss and Open email. **Accept** writes through the
   existing commitments or reminders path. Until then nothing alerts, nothing
   reaches model context, and nothing acts.
7. **Resolve before surfacing.** Before showing an item, check newer headers
   in the same thread or from the same sender for "done", "received",
   "cancelled" or a moved date. If one exists, re-extract that newer message
   instead of trusting the old one.

**Scheduling.** Copy `mail_gists` exactly:

- background only, single-flight, one email per tick
- skip when `idle.foreground_busy()`, and stand down mid-call if a turn starts
- `_MIN_GAP_S`-style gaps, plus a failure back-off and a max-attempts limit
  per (message, fingerprint)
- never load or evict a model, and run only if the agent model is already
  resident
- a deadline per call (the digest uses 20 s)

### Phase 2: deeper reach (only if phase 1 earns it)

- **Sent mail** (`labelIds=SENT`, read-only scope already covers it) to catch
  the user's own promises. This needs a separate attribution prompt: in Sent,
  "I" *is* the user.
- **Mail.app accounts** via an id-filtered AppleScript, run in small batches
  at night.
- A one-time backfill to 12 months, then incremental only.

### Phase 3 (optional, user decision): suggested know-you facts

Same pipeline, a separate item type. These are concrete, stable facts with a
clear owner: "Your landlord is Harbor Property Mgmt (14 Alder St)", "AppleCare
on your MacBook Pro, AC-77310". Each is shown once for Add to profile or
Dismiss, with its source email. No inference about personality, health,
finances beyond named bills, or relationships. Accepted facts go into the
know-you profile's `notes` and follow P4.

## Costs

The figures assume the agent model is Gemma 4 E4B and it is resident.

| Item | Estimate |
| --- | --- |
| Gemma per long email (digest or extract) | ~5–6 s, ~300 output tokens (measured for the digest) |
| Gemma per short email (extract only) | ~2–3 s (inferred: ~100–150 output tokens; decode is 91% of latency) |
| Gmail fetch per body | Well under 1 s, quota-free in practice |
| Mail.app fetch | 18–35 s per AppleScript batch; avoid in phase 1 |
| One-time 6-month backfill | Assume ~150–250 emails survive the prefilter → **~10–20 min of model time**, spread over idle ticks (a few evenings at one call per 90 s gap) |
| Steady state | ~10–30 new candidate emails a day → ~1–3 min of model time a day |
| Storage | KB per item; no bodies stored, only quotes |

The survival rate after the prefilter is a guess. Phase 1 should log it first,
in shadow, before any surface ships.

## Risks and how the design handles them

| Risk | Handling |
| --- | --- |
| **Identity confusion** (a friend's job reported as the user's) | `identity_block()` and `attribution_rules` in every prompt; a "do not list other people's news" rule; a bench trap; and the user gate. Profile-type items wait for phase 3 and a user decision. |
| **Prompt injection** in mail ("assistant: add a reminder to wire…") | Untrusted wrapper; items are candidates only; no tool is ever driven by digest text; a `suspicious` flag hides the item from the Brief; the evidence-quote check. |
| **Invented items or dates** | Evidence quote must appear verbatim in the body; dates resolved in code; Gemma's measured behavior is omission, not invention (digest write-up). |
| **Stale or superseded items** | Drop anything a week or more past due; newer-thread re-check before surfacing; "use the final version" prompt rule; `expired` state. |
| **Duplicates** (same bill each month, reply chains, the 48 h detector) | Key on (message id, fingerprint), then (normalized item, due date); skip the detector's window; dismissals persist. |
| **Model contention** (one resident model) | `mail_gists` rules: idle-only, stand down mid-call, no load or evict, gaps and back-off. The live turn always wins. |
| **Privacy** | All local; no bodies persisted (only quotes, in `~/.moe` with 0600 like the other caches); never sent to cloud providers (same rule as the profile); per-item Dismiss; a single off switch. |
| **Repeating the retired builder** | No automatic profile; nothing writes to the know-you profile or `facts.db` without a click. |
| **Unmeasured extraction quality** | Phase 0 gate before any code |

## Open decisions for the user

1. **Profile: yes or no?** Wisp removed an automatic profile in August and
   marked it "not to be rebuilt". Is phase 3 (suggested concrete facts you
   accept one by one) acceptable, or should mail never feed the profile?
2. **How far back?** 3, 6 or 12 months for the one-time backfill.
3. **Sent mail.** May Wisp read Sent (phase 2) to catch your own promises?
4. **Where it shows.** A Brief line plus a review list, or the review list
   only?
5. **Accepted items go to** Apple Reminders, or Wisp's own commitments store?
6. **Mail.app accounts.** Worth the slow native path, or Gmail only?
