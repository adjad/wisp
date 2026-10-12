# Aria demo data (fully fictional)

Synthetic mailbox, iMessage threads, extra contacts and canned replies for Wisp's "Aria demo mode". Persona: Aria Solano, founder and CEO of Cedarloop (fictional multi-site field-service maintenance SaaS). All people, companies and numbers are invented. Emails use only `example.com`, `example.org` and `*.example`; phones are `+1-202-555-01xx`.

Anchor: "now" is **Sunday 2026-10-11, about 17:30 PT** (`as_of` in the files). All dates carry `-07:00` (PDT) and nothing is later than 2026-10-11T23:59-07:00. History spans 2026-08-17 (Mon) to now, which is 8 weeks. A loader that wants to run on a different real date can shift every timestamp by a whole number of weeks, which preserves the weekday rhythm.

Read-only sources this stays consistent with: `service/demo/founder-demo.json` (notes n01-n12, reminders r01-r22, contacts c01-c14), `service/behavior/persona.py`, `demo/seed_mac_persona.py`.

## Files

| File | Contents | Counts |
| --- | --- | --- |
| `contacts_extra.json` | 5 added contacts c15-c19, same shape as founder-demo contacts | 5 |
| `mail.json` | Aria's mailbox | 48 threads, 99 messages (inbox 51, sent 25, archive 23), 12 unread (4 dated today), 10 flagged, 16 with attachments |
| `messages.json` | iMessage-style threads | 275 messages in 6 chats: Dev 102, Mom 52, Cedarloop core 65, Maya 27, Samira 18, Nora 11 (4 unread) |
| `reply_templates.json` | Canned simulated replies | 18 contacts, 71 templates, 5 generic |
| `validate.py` | Stdlib checker (`python3 validate.py`) | - |

### contacts_extra.json
`{schema_version, synthetic, provenance, contacts[]}`. Each contact: `id, given_name, family_name, role, organization, email, phone, relationship, context`. IDs continue the base set: c15 Marisol Solano (Mom), c16 Maya Brandt (friend), c17 Ingrid Lund (board chair), c18 Hana Weiss (outside counsel, Larkspur Legal), c19 Pilar Nunez (recruiter, Alder Search). The base c01-c14 are not repeated.

### mail.json
`{schema_version, synthetic, persona, provenance, as_of, timezone, threads[], messages[]}`.
- Message: `id` (`mail-001`..), `thread_id` (`t01`..), `mailbox` (`inbox|sent|archive`), `from {name,email,contact_id|null}`, `to[]`, `cc[]` (same shape), `subject`, `date`, `body` (plain text), `unread`, `flagged`, `has_attachment`, `attachments[]` (file names only, no content).
- Rules: Aria's own messages are `sent`; received messages in archived threads are `archive`, otherwise `inbox`. Replies get a `Re:` subject. `contact_id` is null for vendors and newsletters.
- Thread: `id, subject, message_ids[], message_count (1-6), last_date, unread_count, mailboxes[]`.

### messages.json
`{schema_version, synthetic, persona, provenance, as_of, timezone, chats[], messages[]}`.
- Chat: `id` (`chat-dev|mom|core|samira|nora|maya`), `name`, `participants[]` (`"me"` plus contact ids), `is_group`, `message_count`.
- Message: `id` (`im-0001`..), `chat_id`, `chat_name`, `participants[]`, `sender` (`"me"` or contact id), `text`, `date`, `is_from_me`, `read`.
- Rhythm: Dev messages land on weekday afternoons (~14:00-17:15) on 34 of 40 workdays, with one Saturday exception. Mom texts and calls cluster on Sunday evenings (17:00-19:40). Nora appears around the biweekly Wednesday 4pm investor-call weeks (Sep 2, Sep 16, Sep 30). The group chat is "Cedarloop core" (Dev, Lena, Samira, Theo, Imani).

### reply_templates.json
`{selection, by_contact{contact_id: [template]}, generic[]}`. Template: `id, channel (message|mail|any), when_contains[], text, delay_seconds (25-84)`. Selection is deterministic and documented in the `selection` field: filter by channel; first template whose `when_contains` keyword appears in the outgoing text wins; otherwise pick among keyword-less templates with `sum(ord(c)) % n`; no contact templates means use `generic`.

## Consistency notes

Shared facts (match founder-demo): 28 paying customers, $42,000 MRR / $504,000 ARR, 79% gross margin, 108% trailing-3-month NRR, cash $840,000, net burn $70,000/month, runway 12.0 months. Jonah West: $162,000 base, about $202,000 loaded, +$16,800/month, runway about 9.7 months, no offer approved. Seed extension target $2.5M, committed $0, nonbinding informal interest about $350,000 (never called a term sheet or a commitment). Pilots (unsigned, not in MRR): Harborline $1,500/mo 6 sites, Rivet Fleet $2,000/mo 4 sites, Foundry $3,000/mo 10 sites ($6,500 combined). History: August close was $39,000 MRR, 27 customers (investor update 09-04); September close $42,000 and 28 customers (+$2,000 new, +$2,000 expansion, -$1,000 churn).

Deliberate departure from the brief: the story has no term sheet (note n08 forbids calling interest one). The "term-sheet back-and-forth" is therefore Nora's explicitly nonbinding interest and use-of-funds thread (t03) plus counsel's review of a blank seed-extension template (t06, nothing signed).

| Thread or chat | Ties to |
| --- | --- |
| t16, t17, t18 Harborline | Note n03; reminders r04 (correct SSO message, still unsent: draft only), r09, r15; Monday Oct 12 07:00 "Harborline SSO check-in" invite overlaps the 06:45 "Morning lift" gym slot (persona habit_conflict) |
| t19, t20 Rivet | Note n04; reminders r12, r13, r20; Wednesday customer-call rotation |
| t21, t22, t23 Foundry | Note n05; reminders r03, r14; Elise declined bespoke dashboards, regional filter instead |
| t24, t25, t26 pricing and pipeline | Note n06; reminders r15, r16 (Aria's decision of 10-08: no 30% off, no unlimited sites, max 10% prepay) |
| t30, t31 isolation evidence | Note n07; reminders r01 (send evidence to Priya), r10 |
| t32 Lena export plan | Notes n11, n12; reminders r02, r03; Lena asks Aria for the revised export plan (retention 13 months, definition of done), which is not yet sent |
| t02, t03, t04, t05, t06 fundraising | Note n08; reminder r07 (investor update Monday Oct 12), r08; Nora biweekly Wednesday 4pm call; chat-nora asks to move Wed Oct 14 to Thursday Oct 15 4:30pm, unanswered |
| t07, t09 board and forecast | Note n10; reminders r05, r06, r19; board meeting Thu Oct 22, deck due Fri Oct 16 |
| t35-t38, chat-dev | Note n09; reminders r17, r18, r22; Pilar wants a decision before Oct 16 |
| t34 meeting hygiene | persona Tue/Thu back-to-back runs ("Pipeline review", "Pricing review", "Ops standup") |
| t45, t47, t48, chat-maya | Saturday "Farmers market" (Ferry Building) calendar event; Maya's Nov 14 wedding; gym renewal Oct 20 ("Morning lift"); check-up Tue Oct 20 08:30 |
| t46, chat-mom | Sunday "Call Mom" 18:00 event; Dad's 65th dinner Sun Oct 25 18:00 |

## Great demo prompts

1. "Summarize my unread email." (12 unread: Owen's forecast, Nora's nonbinding-interest reply, Elise's site-name ask, Priya's review scope, Pilar on Jonah, Caleb's placeholder, Ingrid's "if no pilot converts" slide, plus newsletters and bills)
2. "What did Dev ask me yesterday?" (Saturday Oct 10: whether to mention the clock-skew identity provider to Priya)
3. "What did Dev and I decide about retention defaults?" (13 months, configurable per tenant)
4. "Reply to Nora that Thursday works." (Nora asked Friday for Thu Oct 15, 4:30pm; a simulated reply arrives)
5. "Email Lena the revised export plan." (t32: include 13-month retention and the done criteria; Lena's template reply follows)
6. "Add a reminder to send the board deck Friday." (Ingrid's t07 asks for Fri Oct 16)
7. "Delete the farmers market event." (Saturday 10:00 Ferry Building event, which Aria skipped today)
8. "What's on my calendar Monday, and does anything conflict with the gym?" (Harborline 07:00 invite vs. 06:45 Morning lift)
9. "Did Tessa ever get a corrected SSO message?" (No: Samira's draft exists, unsent; t17)
10. "Draft a reply to Pilar about Jonah." (no offer approved; update this week; runway 12.0 to about 9.7 months)
11. "Text Mom that I'll call at 6." (Mom's unread 17:12 text; Sunday-evening rhythm)
12. "What do I owe investors?" (Monday update to Nora; cohort and concentration appendix for Caleb; qualified security status; use-of-funds line items to Hana)
13. "Which emails mention an invoice or a renewal?" (Cloudhaven, Bellwether, Pinecrest, Ironwood)
14. "Find everything about Foundry." (mail t21-t23, notes n05, reminders r03/r14, core chat)

## Validation
Run `python3 validate.py` in this folder. It checks JSON validity, unique ids, parseable `-07:00` dates not later than 2026-10-11T23:59, reserved email domains (including addresses inside body text), `555-01xx` phones, that every referenced contact exists (c01-c14 base or c15-c19), thread size 1-6, counts, Dev/Mom rhythm, and template coverage.
