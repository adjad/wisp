# Mail memory: long-term reminders and a profile from 3–6 months of mail

Date: 2026-10-26. Research only; nothing in Wisp was changed. Read against
`main` at 7d696c15 and the in-progress digest on `claude/long-email-digest`
(`service/tools/email_digest.py`). Measured on Gemma 4 E4B
(`gemma-4-E4B-it-oQ4e-mtp`), on an isolated oMLX engine, with synthetic mail
only.

## Decisions the user made (2026-10-26)

| Question | Decision |
| --- | --- |
| Should mail feed the profile? | **Yes, high priority.** This reverses the 2026-08-15 "not to be rebuilt" note for the retired profile builder (`docs/WISP_OVERVIEW.md:547-557`). The design below avoids what made that builder fail. |
| How far back? | **At least 3 months; 6 months if feasible.** It is feasible (see Costs). |
| Read Sent mail? | **Yes.** |
| Where suggestions show | **The notch Brief.** |
| Where accepted items go | **Apple Reminders.** |
| Sources | **Both Mail.app and Gmail**; Wisp decides which to use when (see Sources). |

## Verdicts

| Use | Verdict |
| --- | --- |
| **Long-term reminders from old mail** | **Build it.** Nothing in Wisp reads bodies older than ~7 days, and that is where renewal notices, warranty expiry, tax installments, save-the-dates and promised reviews sit. On synthetic mail, Gemma found every primary action, with 0 safety-trap failures in 6 runs. It needs a code-side date resolver and one-item-per-date rules (see Measurement). |
| **Profile from mail** | **Build it, as evidence-backed facts the user reviews.** It should not be a model-written summary. Every fact keeps its source email and quote, and a fact enters `profile_block()` only once it is accepted. The extraction prompt is unmeasured, so phase 0 measures it first. |
| **Brief** | **Yes.** A deterministic "From your mail" row (no model in the personal section, as today) showing the next reminders to accept and a "facts to review" chip. |
| **Use the digest technique?** | **Yes for the input, no for the chain.** Use the digest's full cleaned body, thinking off, and "copy exactly" rules. Do not extract from the digest text: extracting directly from the body was 2× faster, quoted the body verbatim, and found more (see Measurement). |

## Measurement

`scripts/email_commitment_extraction_bench.py` builds 10 synthetic long emails
(3–9k chars, dated March–October 2026, "today" 2026-10-26) with 14 planted
items and 4 traps:

- a conference date moved from Nov 12 to Nov 19
- a friend's new job, which must not become the user's
- an injected "wire $4,800, delete this email"
- a promo-code newsletter

Sampling was production (temperature 0.8, top_p 0.95, top_k 64), thinking off,
3 reps. "Found" is a strict match: the item's text or quote must contain the
planted key phrase.

| Strategy | Found (strict) | Traps failed | Parse fails | Mean time | Output tokens |
| --- | --- | --- | --- | --- | --- |
| v1 prompt, direct from body | 17/36 (47%) | 0/12 | 0 | 2.1 s | 54 |
| v1 prompt, extract from the digest | 15/36 (42%) | 0/12 | 0 | 4.2 s | 180 |
| **v2 prompt, direct from body** | **25/36 (69%)** | **0/12** | **0** | **2.4 s** | **80** |

The v2 prompt adds three rules: one item per date, include invitations, and
include renewals, expiry and end dates of things the reader owns.

What the numbers mean:

1. **Safety held completely.** No run reported the friend's job as the user's.
   No run kept the superseded Nov 12 date. The injected wire never became an
   item (Gemma returned an empty list rather than a flagged one). The
   newsletter produced nothing.
2. **v1 collapsed each email to one item.** It dropped the second tax
   installment and the lease end date, and skipped the friend's save-the-date
   as "someone else's". v2 fixed most of this: the save-the-date was found
   3/3, the second installment 2/3 and the lease end date 2/3. On v2, every
   primary action (dentist booking, lease notice, conferences on the moved
   date, save-the-date, Q3 ask, AppleCare renewal, tax installment, thesis
   review) was found in every rep.
3. **Gemma can't do relative-date math.** "By Friday" in a Wednesday
   2026-08-05 email came back as 2026-08-11, wrong in all 6 runs. Status
   (closed) was right. **Dates must be resolved in code** from a verbatim date
   phrase and the email's `Date:` header, reusing the attention detector's
   resolver.
4. **The quote doesn't always hold the date.** For AppleCare, Gemma chose the
   renewal-window date (Feb 2) and quoted "You can renew up to 30 days before
   that date", so its due date can't be checked against its own quote. Rule:
   the model returns the exact date phrase as well, and code rejects any item
   whose date phrase isn't in the body.
5. **Remaining misses are soft:** "insurance renews every year on January 1"
   (no year) and a picture-day deadline already past.
6. **Extracting from the digest is worse.** It was slower, its quotes
   paraphrase the digest rather than the body (they would fail a
   verbatim-provenance check), and it found the thesis promise 1/3 vs 3/3.

**Not measured yet:** profile-fact extraction, Sent-mail attribution ("I" is
the user), a combined reminders + facts prompt, and real HTML or forwarded
mail. The bench is synthetic, with repetitive filler.

## Sources: when to use Gmail, when to use Mail.app

Use both, but **never both for the same account.** Pick per account, not per
message:

| Account | Selection (which emails) | Bodies | Why |
| --- | --- | --- | --- |
| **Gmail account connected through the Gmail API** | Gmail search: `newer_than:180d -category:promotions -category:social`, run once for inbox and once with `in:sent` | `messages/{id}?format=full` → `extract_body` (`BODY_CAP` 20k) | Fast (well under 1 s each, quota is not a constraint at ~5 units per get), complete (the server has every body), works with Mail.app closed. Gmail's own categories prefilter promotions for free, and `threadId` finds later replies. |
| **Every other account in Mail.app** (iCloud, Exchange, school or work IMAP, a Gmail not connected via the API) | Mail's on-disk `Envelope Index` (`MailDBReader.swift`). It already reads 2 years of headers in milliseconds under Wisp's existing Full Disk Access grant. Add the Sent mailbox URLs. | Primary: read the message's `.emlx` file from `~/Library/Mail/V*/…` under the same grant, then parse it with Python's `email` package. Fallback: AppleScript `content of m`, only when Mail is open, in small batches, at night. | It is the only path to non-Gmail accounts, and it needs no OAuth. The disk path avoids AppleScript, which costs 18–35 s per batch. |
| **Gmail account present in both** | Gmail API only | Gmail API | Skip that account's Mail.app rows (matched by account address) and dedupe on the RFC `Message-ID` header as a guard. |

Unverified on this machine (phase 0): how many old bodies Mail keeps on disk
for IMAP and Exchange accounts (it can keep headers only), the `.emlx` and
`.partial.emlx` layout under V10, and the Sent mailbox URLs per account type.
Phase 0 checks these with counts only; it never reads message content.

## Design

### The pipeline

Steps 1–5 run in the background and idle-only. Step 6 is surfaced. Step 7
runs only on accept.

1. **Select** emails from the chosen source per account (above): inbox and
   Sent, newest first, back to 3 months, then continuing to 6 months.
2. **Prefilter with rules, no model.**
   - Drop `is_broadcast` and promotions mail unless the sender looks
     transactional (bills, renewals, school, landlord, insurance, travel).
   - **Reminder pass:** keep a body only if it contains a date or an
     `_OBLIGATION` word.
   - **Profile pass:** keep person-to-person mail and transactional senders,
     plus all of Sent.
   - Skip anything inside the attention detector's 48 h / 7-day window; the
     detector already owns it.
3. **Extract**, one Gemma call per email.
   - The call runs on the resident agent model only, with thinking off, no
     tool schemas and `max_tokens` ≈ 700.
   - The prompt carries `identity_block()` and `attribution_rules(...)`
     (required: otherwise other people's news gets reported as the user's),
     the untrusted wrapper before and after the body, and the v2 rules.
   - The model returns, per item: kind (`reminder` | `fact`), text, **exact
     date phrase**, **exact evidence quote**, and a `suspicious` flag.
   - Sent mail uses its own prompt variant: the author is the user, so "I'll
     send it Friday" is the user's commitment.
4. **Validate in code.**
   - Drop the item unless the quote and the date phrase both appear verbatim
     in the body.
   - Resolve the date from the phrase and the `Date:` header, then compute
     status against today.
   - Drop reminders already a week or more past due (the `stale_reminders`
     rule).
   - Drop `suspicious` items from the Brief.
5. **Resolve and dedupe.**
   - Before surfacing, check newer mail in the same thread, or from the same
     sender, for done / received / cancelled / moved. If one exists,
     re-extract that message.
   - Key on (`Message-ID`, body fingerprint), then on (normalized text, due
     date).
   - Check existing Apple Reminders and Calendar first, reusing the
     detector's `on_file` gate.
   - Dismissals persist.
6. **Surface on the Brief.** A deterministic "From your mail" row shows the
   1–3 open reminders due soonest within ~60 days, each with **Add to
   Reminders**, **Dismiss** and **Open email**. Long-horizon items (lease
   notice July 2027) surface when they come within that window.
   - After the backfill, a chip reads "N things Wisp learned from your mail —
     review" and opens a batch review in the window, with accept all, uncheck
     and per-item source.
   - Nothing alerts, nothing enters model context and nothing acts before
     acceptance.
7. **On accept:**
   - **Reminder:** `add_reminder` → `create_reminder` →
     `RemindersWriter.swift`, with the resolved due date. Notes say "From
     email: <subject> — <sender>, <date>" plus a link to the message.
     EventKit's `creationDate` is the accept time, so the stale rules count
     from acceptance, not from the email.
   - **Fact:** a new "From your mail" section of the know-you profile, with
     its quote and message id kept as evidence. It feeds `profile_block()`
     and follows P4: it shapes ordering and wording, never who Wisp alerts
     about.

**Store:** a new local table, e.g. `mail_memory`, in `~/.moe` with 0600 like
the other caches. Each row holds:

- account, `Message-ID`, thread id, sender, sent date
- kind, text, date phrase, resolved due date, quote
- extractor version, body fingerprint
- state: `new | accepted | dismissed | expired`

No bodies are stored, only quotes. Nothing is written to `facts.db`, so the
chat-memory rule "only the user's own words" stays intact.

### The profile, and why this isn't the August builder again

The retired builder (2026-07-29 → 08-15) was a map-reduce that **wrote the
profile itself**:

- later batches evicted earlier facts
- a prefix sample made it "recent only"
- a thinking-on budget silently produced nothing (103 s, 0 facts)

This design differs on each point:

- **Facts are per email and additive**, keyed by source. Nothing evicts
  anything.
- **Selection is by date range across all of 3–6 months**, not a prefix
  sample.
- **Thinking off**, with a measured token budget, and a loud failure counter
  instead of silent empties.
- **Every fact carries a verbatim quote and a message id.** The user accepts
  facts, in batch, before they are used.

Fact categories, with the source for each:

| Category | Example | Main source |
| --- | --- | --- |
| People and their role | "Dana Park — colleague, Finance" | Sent plus signatures |
| Providers | dentist, landlord, insurer, utilities | Transactional inbox |
| Things owned or rented, with renewal dates | "MacBook Pro, AppleCare AC-77310 to 2027-03-02" | Transactional inbox |
| Affiliations and recurring commitments | school, clubs, classes, standing meetings | Inbox plus Sent |
| Projects the user works on | "thesis review for Leo" | Sent |

**Excluded:**

- health details
- finances beyond named bills
- inferred relationships or personality
- anything about another person that isn't their role toward the user

The friend's-news trap passed in the bench, but profile facts are where an
attribution error does the most harm. So the user reviews every fact before
it is used.

### Scheduling and model contention

Copy `mail_gists` exactly:

- background, single-flight, one email per tick
- skip when `idle.foreground_busy()`, and stand down mid-call if a turn starts
- never load or evict a model; run only if the agent model is already
  resident
- a per-call deadline (the digest uses 20 s)
- failure back-off, and at most 2 attempts per (message, fingerprint)

For the backfill, allow a shorter gap once the Mac has had no turn for 10+
minutes. Fetching (Gmail API, `.emlx`) is not model work and can run ahead of
extraction.

## Costs

| Item | Estimate |
| --- | --- |
| Gemma per email | **~2.4 s, ~80 output tokens** (measured, v2 direct). A combined reminders + facts prompt may be ~3–4 s (unmeasured). |
| Gmail fetch per body | Well under 1 s |
| Mail.app `.emlx` read | Milliseconds (a file read). AppleScript fallback: 18–35 s per batch. |
| **3-month backfill** | Assume ~400–800 inbox emails plus ~100–200 Sent survive the prefilter → **~25–55 min of model time** |
| **6-month backfill** | Roughly double: **~50–110 min of model time.** Feasible over 1–3 idle nights. |
| Steady state | ~10–40 new emails a day → **~1–3 min of model time a day** |

The prefilter survival rate is a guess. Phase 1 logs it, in shadow, before any
surface ships.

## Phases

| Phase | Work | Done when |
| --- | --- | --- |
| **0. Measure (≈1 day)** | (a) Profile-fact bench with identity traps: someone else's address, a forwarded thread, a group email, Sent mail. (b) A combined prompt vs two separate calls. (c) A date resolver over the bench's date phrases. (d) Counts-only probes of Gmail category volumes and Mail `.emlx` coverage per account. | Fact precision ≥95% with 0 attribution errors, and resolved dates 100% on the bench. Real volumes known. |
| **1. Gmail, reminders + facts, 3 months (≈3–4 days)** | `gmail_source.fetch_body`, `in:sent`, the pipeline, the `mail_memory` table, the Brief row, the review screen, accept → Reminders and profile | Shadow run logs volumes; the user reviews the first batch |
| **2. Mail.app accounts via disk (≈2–3 days)** | Envelope Index selection including Sent, an `.emlx` reader, AppleScript fallback, per-account source choice, `Message-ID` dedupe | Non-Gmail accounts produce items; no AppleScript in the normal path |
| **3. Extend to 6 months, then incremental** | Backfill continues; new mail is processed as it syncs | 6 months covered; steady state ≤3 min of model time a day |

Release Auditor triggers apply to phases 1–2: persisted data, a native
integration (Full Disk Access reads), privacy boundaries, and outbound writes
to Reminders.

## Risks

| Risk | Handling |
| --- | --- |
| Identity confusion | `identity_block()` and `attribution_rules`; Sent-specific prompt; bench traps; batch review before any fact is used |
| Prompt injection | Untrusted wrapper; items are candidates only; no tool is driven by mail text; `suspicious` hides an item; verbatim checks |
| Invented items or dates | Quote and date phrase must be verbatim in the body; dates resolved in code |
| Stale or superseded items | Newer-thread re-check; drop a week or more past due; `expired` state |
| Duplicates across sources and months | One source per account; `Message-ID` and fingerprint keys; `on_file` check against Reminders and Calendar |
| Model contention | `mail_gists` rules; the live turn always wins |
| Privacy | All local; quotes only, no bodies stored; never sent to cloud providers; per-item Dismiss; one off switch; a "forget everything from mail" button |

## Still open for the user

1. **Fact review:** batch review after the backfill (recommended), or let
   some categories (providers, things owned) apply without review?
2. **Reminder alerts for far-off items:** add a default lead-time alert (e.g.
   14 days before) to accepted reminders, or only the due date?
