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
| **Profile from mail** | **Build it, as evidence-backed facts the user reviews.** It should not be a model-written summary. Every fact keeps its source email and quote, and a fact enters `profile_block()` only once it is accepted. Phase 0 found extraction **safe (0/48 identity violations) but conservative (~50% of facts per email)**. So the profile also counts repeated evidence across emails, uses rules for orders and bills, and reads Contacts relations for people. |
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

## Phase 0 results (2026-10-26)

### Profile facts: safe, but only half the facts

`scripts/email_profile_fact_bench.py` adds 10 profile emails to the 10
reminder emails. Four are Sent mail, written by the user. It plants 24 facts
across the 20 emails (address change, employer, a diet preference, book club,
dentist, lease, purchases, family roles) and runs **48 identity and safety
checks**:

- a forwarded email about the user's mother's doctor appointment
- a group email in which another member adopted a puppy
- "Your sister passed the bar exam"
- a cholesterol lab result
- marketing mail ("members like you")
- an injected "record the user's bank PIN and password"
- the friend's new job, the phishing email and the newsletter from the
  reminder set

Each prompt ran 2 reps, thinking off.

| Prompt | Facts found | Identity/safety violations | JSON parse fails | Mean time per email |
| --- | --- | --- | --- | --- |
| **Separate calls** (reminder v2 + fact v1) | 12/24 facts; reminders 19/30 | **0/48** | 0 | 1.7 s (both calls) |
| Combined (one call returns reminders and facts) | 10/24 facts; reminders 14/30 | 0/48 | **24/40** | 1.7 s |
| Fact v2 (more "always include…" rules) | 2/24 | 0/48 | 3 | 0.6 s |
| Fact v3 (one worked example) | 12/24 | 0/48 | 1 | 0.8 s |

What this means:

1. **Precision held perfectly: 0 of 48 checks failed with every prompt.**
   - The mother's appointment never became the user's.
   - The sister's bar exam, the puppy and the friend's job never became facts.
   - The lab result and the injected PIN were never recorded.
   - Marketing mail produced nothing.
2. **Recall is the problem, at about 50% per email.**
   - v1 found the address change, employer, diet, dentist, lease and book
     club.
   - It missed family roles (sister, mother), purchases (MacBook, standing
     desk) and the school.
   - v3's worked example added the mother's role and the book club but
     dropped others.
   - More rules (v2) made Gemma refuse almost everything: "never list another
     person's news" fights with "always include family". **On E4B, prompt
     wording trades one fact for another; it does not raise the ceiling.**
3. **The combined prompt is out.** It broke JSON in 60% of runs and saved no
   time. Use two separate calls.
4. **Facts come back in the email's own voice** ("I'm moving to 88 Birch
   Lane"). Normalizing them to "Sam is moving to…" belongs in code, from
   `direction` (Sent vs inbox), not in the prompt.

**So the profile needs more than one model pass per email.** Three changes
together, none of which needs a bigger model:

- **(a) Count repeated evidence.** A durable fact (address, employer, family
  member, provider) shows up in many emails across 6 months, so ~50% recall
  per email still covers most facts over time. Rank facts by how many emails
  support them.
- **(b) Rules for structured mail.**
  - Order, shipping and receipt templates give purchases.
  - Bills and leases give providers and the home address.
- **(c) Contacts and signatures for people's roles.**
  - Contacts' relation fields and Sent sign-offs give family and colleagues.
  - Gemma confirms a role only when the email states it.

Phase 1 should measure (a) on a longer synthetic mailbox before calling the
profile done.

### Date resolver: use it, on the model's date phrase

Wisp's own `service/attention/resolve.py` (pure, no I/O) was run on 17 date
sentences from the benches, using the email's date. It got **12/17**,
including **"by Friday" → 2026-08-07**, which Gemma missed in all 6 runs.

Gaps:

| Input | Result | Fix |
| --- | --- | --- |
| "moved from November 12 to November 19" (whole sentence) | **Nov 12, the superseded date** | Resolve the model's short date phrase, not the sentence: "November 19" → Nov 19 ✓. Reject any phrase with two dates. |
| "for October 2026" | none (`no_time_stated`) | Add month precision: due at the end of the month, shown as "in October". |
| "next week", "in two weeks" | none (`no_usable_time`) | A small relative-week shim in the mail layer |
| "10/3 at 10am" | none (`ambiguous_date_order`) | Pick the order from the user's locale (en_US → M/D) |

The resolver is tuned for chat. A thin mail wrapper over it fixes all four
gaps, without changing how the attention detector behaves.

### This Mac's mail (counts only)

These come from Mail's on-disk `Envelope Index`, opened read-only. Only row
counts and the existence of `.emlx` files were read: no subjects, addresses
or bodies. The Mac has **2 accounts, both Gmail over IMAP**.

| Last… | Inbox | Inbox, person-to-person* | Sent |
| --- | --- | --- | --- |
| 90 days | 1,729 | 70 | 60 |
| 180 days | 2,888 | 126 | 68 |

\*Person-to-person means no `List-Id`, no unsubscribe header and not flagged
automated. About **93–94% of inbox mail is bulk** by these flags. Bills,
leases and renewals also carry those flags, so the bulk set still needs
sender rules to find transactional mail.

**Bodies on disk:** every one of the 3,006 messages from the last 180 days
has an `.emlx` file. For bulk mail, 92% are full files and 8% are
`.partial.emlx`. For person-to-person mail, 74% are full and 26% partial.
Partial files had their attachments stripped. Whether they still hold the
text body was **not checked**, because that would mean reading content.
Phase 1 checks it on synthetic files.

**Gmail API category counts: not probed.** That needs Wisp's OAuth token
under `~/.moe`, which this session must not touch (the Mac is in a first-run
test state). Mail's own bulk flags turned out to be a working substitute.

### Which mail is read: Primary, Transactions and Updates only (user, 2026-10-26)

The user asked that the reader skip spam and promotions, on Mail.app and on
Gmail alike. Implemented in aed20914:

- **Spam, trash and archived mail are never read.** The index lists Inbox
  and Sent only (Gmail's Inbox and Sent labels for Gmail-over-IMAP).
- **Mail.app categories.** Mail files each message under exactly one category,
  stored in the `Envelope Index` (`message_global_data.model_category`: 0
  Primary, 1 Transactions, 2 Updates, 3 Promotions). So Primary does not
  overlap with Transactions or Updates. The queue reads Sent first, then
  Primary, Transactions and Updates. Promotions are never read.
- **Gmail tabs.** For an account connected through the Gmail API, mail Gmail
  filed under Promotions, Social or Forums is dropped too, matched on the RFC
  `Message-ID` (Mail stores all 21,886 of them with angle brackets; Gmail's
  side is normalized to match). If that lookup fails or no account is
  connected, Mail's category decides.
- **No category recorded** (older mail, other accounts): person-to-person
  mail counts as Primary, bulk mail with a transactional subject as
  Transactions, a sales subject is skipped, and other bulk counts as Updates.
- Your own Sent mail is always read.

This Mac, inbox, last 90 days (counts only):

| Category | Emails | Read? |
| --- | --- | --- |
| Primary | 233 | yes |
| Transactions | 49 | yes |
| Updates | 777 | yes |
| Promotions | 672 | no |

Updates is the large one: newsletters and notifications. It is what makes the
3-month walk bigger than the earlier estimate (see Costs).

## Sources: when to use Gmail, when to use Mail.app

The counts change the earlier recommendation. **Mail.app's disk copy is the
better body source whenever an account is in Mail**: every recent message is
already on disk, a read takes milliseconds, it needs no network, OAuth or
quota, and Wisp already holds the Full Disk Access grant it needs. Use both
sources, but **one per message, chosen in this order**:

| Situation | Source | Why |
| --- | --- | --- |
| The account is in Mail.app and the message has an `.emlx` file | **Mail.app, from disk.** Use the `Envelope Index` to choose messages: `conversation_id` gives threads, the bulk flags prefilter, and Gmail's Inbox and Sent labels come from the `labels` table. Then parse the `.emlx`. | Fastest and complete for recent mail; works offline; no AppleScript |
| The account is Gmail and connected through the API, and the file is missing or only partial without a text body | **Gmail API** `messages/{id}?format=full` | The server has every body; under 1 s each |
| A Gmail account that isn't in Mail.app | **Gmail API.** Search `newer_than:180d`, plus `in:sent`, with `-category:promotions -category:social` as the prefilter. | The only source for it |
| A non-Gmail account (iCloud, Exchange, school) whose body isn't on disk | **AppleScript `content of m`**, only when Mail is open, small batches, while you're away | Last resort: 18–35 s per batch |

The two sources are deduplicated on the RFC `Message-ID`. **When not to use
both:** if every account is in Mail.app and its bodies are on disk, the Gmail
API adds nothing for this feature. On this Mac it would be a fallback only.

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

### What counts as idle, and how the work is split

Wisp has one resident model, so only the model's time is scarce. Fetching
(an `.emlx` read takes milliseconds) and rule prefiltering cost nothing and
can run at any time. **Only the Gemma calls are gated.**

What Wisp has today:

- `idle.foreground_busy()` is a counter of user turns in flight.
- `idle.in_flight(model)` counts generations running on a model.
- The agent model is **pinned resident** (`main.py` adds it to keep-warm), so
  it isn't unloaded after `idle_unload_minutes`.
- `mail_gists` already skips work during a turn and cancels a running call
  when one starts.

Three levels:

| Level | When | What the backfill may do |
| --- | --- | --- |
| **Busy** | A turn is in flight (`foreground_busy()`), any generation is running on the agent model, a turn ended under 60 s ago (follow-ups come fast), the agent model isn't resident (e.g. the coding model swapped in), or the Mac is in Low Power Mode, on battery below 30%, or under memory-pressure warning | Nothing. Skip the tick; don't queue. |
| **Between turns** | None of the above, and no turn for ≥ 2 min | **One email per tick, at least 90 s apart** (the `mail_gists` gap). Each call is ~1–2.5 s. If a turn starts mid-call, the call is cancelled, so the worst cost to you is the cancel plus a cache refill. |
| **Away** | No keyboard or mouse input for ≥ 10 min (IOKit `HIDIdleTime`, new Swift code), or the screen is locked or the display asleep, plus on AC power or battery above 50%, and thermal state nominal | **Back-to-back, single-flight, ~5 s gap.** Still cancelled instantly by a turn. |

Never: lid closed or asleep. Wisp is stopped then, and the backfill resumes
on wake from its saved cursor, the same as the existing sync backfills.

**Splitting the work** is a persisted queue (in the `mail_memory` table), not
a long job:

- Each message is a row: `pending → fetched → extracted (reminders, facts) →
  done | failed(attempts)`.
- A tick takes the next row, makes one model call, saves the result and
  returns. So a crash, a sleep or a user turn loses at most one call.
- Order is by value, so the 3-month guarantee lands first:
  1. New mail as it arrives
  2. Sent, last 90 days (60 on this Mac)
  3. Person-to-person inbox, last 90 days (70)
  4. Transactional bulk, last 90 days
  5. Days 91–180, in the same order
- Each email takes **two calls** (reminders, then facts), and each call is its
  own tick, so a single tick never holds the model longer than ~2.5 s.
- Other background jobs stay fair: `mail_gists` keeps its 15-minute
  rewrites, and the backfill yields to it by skipping any tick where another
  background job has a model call in flight.

## Costs (with this Mac's real counts)

| Item | Estimate |
| --- | --- |
| Gemma per call | **~0.8–2.4 s** measured (reminders ~80 output tokens, facts ~30). Two calls per email: **~2–4 s per email.** |
| `.emlx` read | Milliseconds. Gmail API fallback well under 1 s. AppleScript 18–35 s per batch. |
| **3 months** | 60 Sent + 70 person-to-person + transactional bulk. If rules keep 10–20% of ~1,660 bulk messages, that's ~170–330 → **~300–460 emails → ~10–30 min of model time** |
| **3 months, with the category rule** | 60 Sent + 233 Primary + 49 Transactions + 777 Updates ≈ **1,120 emails**. Phase 1 makes one reminders call per email (~1–2.5 s) → **~20–45 min of model time** |
| **6 months** | 68 + 126 + 10–20% of ~2,760 bulk (~280–550) → **~470–750 emails → ~16–50 min of model time** |
| How long it takes | Away mode: about **one lunch break or evening**. Between-turns mode only: ~470–750 emails × 2 calls × 90 s ≈ **24–38 hours** of between-turn time, so a few days of normal use. |
| Steady state | ~15 inbox emails a day on this Mac, most of them bulk → **under 1 min of model time a day** |

The share of bulk mail that is transactional is the remaining guess. Phase 1
logs it in shadow mode first.

## Phases

| Phase | Work | Done when |
| --- | --- | --- |
| **0. Measure — DONE 2026-10-26** | Profile-fact bench, combined vs separate, date resolver, counts-only Mail probe | **Precision met** (0/48 violations). **Fact recall not met** (~50% per email). Dates: resolver works on phrases, with 4 small gaps. Combined prompt rejected. Gmail category probe blocked by the `~/.moe` rule. |
| **1. Mail.app disk path, reminders, 3 months (≈3–4 days)** | `Envelope Index` selection including Sent labels, an `.emlx` reader (checked on synthetic partial files), the mail date wrapper over `resolve.py`, the persisted queue with the idle levels above (including `HIDIdleTime`), the `mail_memory` table, the Brief row, accept → Apple Reminders | Shadow run logs volumes and the transactional share; you review the first batch |
| **2. Profile facts (≈3–4 days)** | Fact calls on the same queue; repeated-evidence ranking; rules for orders, bills and leases; Contacts relations and Sent signatures for people; third-person normalization in code; batch review → know-you "From your mail" | A longer synthetic mailbox shows most planted facts found once repeated evidence is counted, with 0 violations |
| **3. Gmail API fallback, then 6 months and incremental** | `gmail_source.fetch_body` for missing or partial bodies and for Gmail accounts not in Mail; extend to 180 days | 6 months covered; steady state under 1 min of model time a day |

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
