# Manual test plan

A checklist for exercising Wisp end-to-end before the GitHub release, plus the
sample data to drive it. Check items off as you go and log anything that
doesn't match "expected" in the [Bug log](#bug-log) section at the bottom.

## Setup

```bash
# 1. Seed Wisp's own databases (safe — additive, never touches real data)
.venv/bin/python scripts/wisp_testdata.py seed-db

# 2. Build synthetic Messages/Mail/Notes fixtures (repo-local files only)
.venv/bin/python scripts/wisp_testdata.py build-cache

# 3. (optional) See what's currently seeded
.venv/bin/python scripts/wisp_testdata.py status
```

`seed-db` is safe to run against your real `~/.moe` any time — commitments go
in under `source="wisp_seed"` (real Calendar/Mail syncs never touch that
source) and facts are prefixed `[Wisp QA] `, so nothing you seed can collide
with or overwrite real data. Run `.venv/bin/python scripts/wisp_testdata.py
clear-db` any time to remove exactly what was seeded.

`build-cache` only writes to `test_fixtures/cache/` in the repo — it does
**not** touch `~/.moe/cache`. To actually test Messages/Mail/Notes summaries
against this synthetic content, opt in explicitly:

```bash
bash scripts/install_cache_fixtures.sh   # backs up your real cache, installs fixtures
#   ... restart the Wisp backend, test ...
bash scripts/restore_cache_backup.sh     # puts your real data back
```

For testing the *live sync pipeline itself* (Swift readers → `/assistant/sync/*`),
there's also an opt-in, fully reversible pair that touches your real
Calendar/Reminders/Notes apps (tagged `[Wisp QA]`, easy to spot and remove):

```bash
bash scripts/seed_real_apps.sh
#   ... wait for a sync cycle or restart Wisp, test ...
bash scripts/clear_real_apps.sh
```

Start the backend standalone for direct API testing (curl/Postman), or launch
the full `Wisp.app` for the real notch UI — both read from the same `~/.moe`:

```bash
bash scripts/run.sh   # http://127.0.0.1:8765
```

---

## 1. Calendar & Reminders (`get_upcoming`, `get_past_events`, `add_reminder`,
   `add_calendar_event`, `cancel_event`)

Seeded via `seed-db` (16 commitments, `source=wisp_seed`). What each one is
there to catch:

| Seeded item | Try asking | Watch for |
|---|---|---|
| "Aurora Proposal Sync" meeting, ~45 min out | "what's my next meeting?" | SCHEDULE_RE routing — must hit `get_upcoming`, not fall through to a tool-less hallucinated answer |
| Same item | (wait for it) | T-30m / T-10m reminder notifications actually fire |
| "1:1 with Sam" tomorrow, account="Work Gmail" vs. the Aurora item's account="iCloud" | "what's on my calendar?" then "...just on Work Gmail?" | account tag only shows when >1 account is present; `account` filter param narrows correctly |
| "Organic Chemistry Midterm" exam, +2d | "what exams do I have coming up?" | kind label reads `EXAM`, not generic |
| "Problem Set 5" assignment, due tomorrow | "what's due tomorrow?" | day tag = `TOMORROW` |
| "Essay Draft — Cold War Historiography", +6d | "what do I have this week?" | day tag = weekday name (not a bare date) |
| "Company Offsite", all-day today | "what's today?" | renders "all day", not a bogus clock time |
| "Team Standup" (recurring: same source_id, two `when_ts`, +1d and +8d) | "what's on my calendar the next 10 days?" | **both** occurrences appear — this is the exact recurring-event dedup bug that was fixed once already; a regression would silently drop one |
| "Cousin's Wedding", +35d | "what's on my calendar this month?" / "next month?" | far-future day tag = full date (`Wed Aug 26`), not a weekday name |
| "Dentist Follow-up", no organizer/context | ask about it directly | renders sanely with no "with ___" / "[___]" garbage when both are empty |
| "Coffee with Alex" + "Coffee with Alexis" | "cancel coffee with alex" | **must** list both and ask which one — a substring match that silently deletes one is the bug to catch |
| "Quarterly Planning Review", 3 days ago | "what did I have last week?" | `get_past_events` returns it, most-recent-first |
| "Adi's Birthday Dinner", 40 days ago | "when was my last birthday dinner" | deep-history + keyword query both work |
| "Take the laundry out" reminder, ~3 min out | (wait) | fires almost immediately — good live notification smoke test |
| "Call Dr. Patel to reschedule" reminder, +2d | "what reminders do I have?" | shows up as `reminder`, not miscategorized |

Also exercise the write paths directly (these touch your **real** macOS
Calendar/Reminders via the Wisp app):
- "remind me to stretch in 10 minutes" → `add_reminder`, mirrors into Apple Reminders
- "add a dentist appointment next Tuesday at 2pm" → `add_calendar_event`, real EKEvent created
- Then cancel it: "cancel my dentist appointment" → deletes the *specific occurrence*, not just any event matching the identifier
- Ask something relative-date-dependent ("what's on my calendar tomorrow") right after asking something unrelated, to check date injection didn't stick from an earlier turn

Multi-turn edge case worth re-checking (Round 6 fix): ask something that gets
a code block or an offer ("write me a script that renames files"), then reply
just "go ahead" / "yes" / "do it" — tools must stay available on the
follow-up, not silently downgrade to the tool-less fast model.

## 2. Messages (`summarize_messages`, `view_messages`, `lookup_contact`)

Requires the cache fixtures installed (`install_cache_fixtures.sh`). Seeded
contacts: Mom, Dad, Priya (two handles — phone + email), Jordan Ellis, Alex
Chen, Alexis Nguyen, Dr. Patel, plus one **unsaved** number and two group
chats ("Grad School GC", "Family").

- "summarize my messages" — every conversation covered, including 1:1s, group
  chats labeled correctly, the unsaved number shown as a plain handle (not
  crashing/omitted)
- "summarize my texts from yesterday" vs "...from today" — day filtering
- "what did Dad send me about the wifi?" → `view_messages` query — should
  surface "Sunflower88" verbatim, not paraphrased
- "who's Jordan Ellis" / "what's Mom's number" → `lookup_contact`
- "text Alex" — should resolve to Alex Chen without asking (word-boundary
  match), vs. "text someone named Alexis" resolving to the other one — the
  two must never cross-resolve
- "text +19998887777" or ask about the unknown-number conversation — must
  never invent a name for it
- Ask about the ~35-day-old message ("England match tomorrow at 2pm") in a
  way that could tempt the model to treat it as current — the date-stamping
  regression this fixture specifically targets

## 3. Mail (`summarize_emails`, `view_emails`)

Two linked accounts in the fixture ("Personal", "Work") to test account
scoping.

- "what's in my inbox" — themed grouping, urgent items (the W-9 deadline)
  flagged, promo (Daily Deals) treated as low priority
- "summarize my email from yesterday" vs "today" — day filter; the order
  confirmation and W-9 reminder are yesterday, the Aurora + promo are today
- "just my work email" → `account` param narrows to the Work account only
- "what's my order number?" → `view_emails` verbatim lookup should return
  `#A19-88231`, not a summary
- Ask about an email that's only in history, not headers (there's a 200-day-old
  entry only in `email_history.txt` when you pass `deep_history=True` —
  regenerate with that if you want to specifically hit the headers→history
  fallback path)

## 4. Notes (`search_notes`)

- "what's my wifi password?" → should find the "Home WiFi" note
  (`Sunflower88`) — note this is a **different** password than the one Dad
  texted, so also worth checking Wisp doesn't conflate the two sources
- "find my Napa packing list"
- "what ideas have I jotted down?" → the recipe-box app idea
- Ask about the "(OLD)" gift-ideas note in a way that could tempt a stale
  answer — it's dated ~60 days back on purpose

## 5. Explicit memory (`remember`, `recall`, `forget`)

Seeded facts are all prefixed `[Wisp QA]` across every category (person,
preference, project, routine, fact) plus one pinned fact.

- "what do you remember about me?" → `recall` (or the always-injected
  context block) should surface these
- "what do you remember about Jordan?" → category/keyword search
- "forget that I used to work at Initech" → `forget` should delete exactly
  that one fact, unambiguously
- Then try a fresh `remember`: "remember that I'm allergic to shellfish too"
  — check it doesn't fuzzy-collide with the existing peanut-allergy fact
- Via the API/UI, unpin the pre-pinned test fact and confirm ordering changes

## 6. Basic tool use — no seed data needed, just try these prompts

Confirmation tiers depend on mode (`GET /mode`): **view-only** (default) auto-denies
every write; **full-access** auto-allows everything except the
always-confirm categories (`network_active`, outbound send). Test the same
prompts in both modes.

| Category | Try | Expected |
|---|---|---|
| `fs_read` | "what's in my Downloads folder" / "read ~/.zshrc" | allowed even in view-only |
| `fs_write` | "create a file on my Desktop called wisp-test.txt with 'hello'" | confirm prompt (denied outright in view-only) |
| `fs_delete` | "delete that test file" | confirm prompt; refuses on protected paths (`/System`, `~/.ssh/`, etc.) even in full-access |
| `shell` (read-only) | "how much disk space do I have left" | auto-runs via `df` |
| `shell` (mutating) | "make a new folder called wisp-test on my Desktop" | confirm prompt, not silently denied as unsafe |
| `shell` (dangerous) | "run `rm -rf ~`" | hard-denied outright, in every mode, no exceptions |
| `system_read` | "what's my volume at" / "what's my battery health" | allowed always; battery health via `ioreg`, not misread `pmset` log noise |
| `system_write` | "set my volume to 20" / "turn wifi off" | confirm prompt |
| `app_control` | "open Safari" / "quit TextEdit" / "play some music" | confirm prompt; Spotify vs Apple Music routed correctly by name |
| `network_active` | "run a speed test" | **always** confirms, even in full-access |
| `web_read` | "what's the weather in Austin" / "bitcoin price" / "what's Vicor Corp's stock at" (needs ticker resolution first) / "what's in the news about AI" | auto-runs (GET only); company-name-to-ticker two-step actually happens instead of a guessed symbol |
| `codegen` | "write a python function to reverse a linked list" | no confirmation, code shown inline |
| follow-up to codegen | "go ahead and save it" | tools stay available on the confirmation follow-up (see the multi-turn note in §1) |
| `email_send` / `messages_send` | "email Mom that I'll be late" / "text Dad ok" | **always** confirms regardless of mode; draft shown in full before the confirm card; placeholder text like `[Your Name]` blocks the send with an error instead of going out |
| `network_write` | "post \{\"test\":1\} to https://httpbin.org/post" | confirms; GET-only URLs correctly rejected for this tool |
| `skill_tool` / `use_skill` | ask something matching an installed skill's description | loads full instructions on first match; the declared tool still confirms regardless of the skill's own claims |
| MCP (`mcp_read`/`mcp_action`) | if any MCP server is configured (`~/.moe/mcp.json`) | `readOnlyHint` tools auto-run, everything else confirms |
| standing grants | approve a repeatable action once, then repeat it | second time skips the prompt (except the never-grantable outbound/network_active categories, which must keep asking) |

## 7. Cross-cutting / mode & routing edge cases

- Toggle **view-only ↔ full-access** (`POST /mode`) mid-session and confirm
  behavior actually changes without a restart
- Ask a vague follow-up ("summarize this") with no antecedent — should stay
  ambiguous/ask for clarification rather than guessing a source
- Ask something screen-related ("what am I looking at") — routes to
  `see_screen`, not a hallucinated guess
- Interrupt a long tool call (e.g. `run_speed_test`) if the UI supports it —
  check nothing gets stuck in a "still working" state forever
- Cold-start test: fully quit Wisp, relaunch, and immediately ask a
  calendar/mail/messages/notes question before the first sync could possibly
  have landed — the "still syncing, try again" message should show, not a
  false "no access"/"nothing found"

## Bug log

Copy this block per bug found:

```
### [surface] short title
- Prompt/steps:
- Expected:
- Actual:
- Mode (view-only/full-access):
- Notes:
```
