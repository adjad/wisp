# Wisp hackathon demo: script and checklist (2026-10-24, 12:00)

## The pitch (one sentence)
Wisp lives in your Mac's notch and does the catching up before you ask. It reads your mail, texts,
calendar, reminders and notes on your own Mac with a local model, then hands you what needs doing.
Every draft is ready to send, and nothing goes out until you tap.

## Before you walk up (10 minutes before)
1. **Plug in power.** The model is local, and a low battery slows it down. Close heavy apps (Xcode, browsers with many tabs).
2. **Check Wisp is up.** Click the notch. Brief should show "Before you ask" cards and summaries.
   - If Brief says *Working…*, wait for it to finish.
   - If it says *unavailable*, quit and reopen Wisp (`open /Applications/Wisp.app`), then wait about 30 s.
3. **Analyze now.** In Brief, tap **Analyze now** and wait for "Wisp read N emails and texts…" to refresh, about 1–2 min.
   This makes the cards reflect the morning's mail.
4. **Clean up the test reminders.** In Reminders, delete the leftover test items ("Wisp live reminder test 2102",
   "Wisp verification …"). Wisp doesn't delete your reminders for you. Then tap Analyze now again.
5. **Pre-warm Ask.** Ask "good morning wisp" once, so the first judge question isn't a cold start.
6. **Judge account.** Google Cloud ▸ Audience is either:
   - **Testing**, with the judge's address added as a test user, or
   - **published / unverified**. The judge will see "Google hasn't verified this app": click **Advanced ▸ Go to Wisp (unsafe)**.
   - Keep a spare personal @gmail.com of your own signed in as a backup. School or Workspace accounts can be blocked by their admin.

## The 3-minute walkthrough

### 1. Brief: "everything I need to know today" (60 s)
- Open the notch ▸ **Brief**.
- **Lede**: one line about the day. Today it reads "CruzHacks Octoberfest Hack Day at 11:45. Week 4 Reading: Strings for CSE 20 is due today, and I found you half an hour for it."
- **Work line**: "Wisp read 133 emails and texts · skipped 121 newsletters · found 3 things". *Say:* "It read everything and threw away the noise, all on this laptop. Nothing went to the cloud."
- **Before you ask** cards: e.g. *DUE TODAY · CALENDAR. Week 4 Reading: Strings for CSE 20. Block 9:30–10 AM?*
  - Tap the button. The card turns ✓ and the time lands on your Day.
  - *Say:* "It found the deadline, checked my calendar for a free slot, and asked. One tap."
- **Summaries**: email and text threads summarized one per line, with what needs attention marked.
  For example, Ed: "STAT 5 Exam 1 is next Wednesday, closed-book…" and Mom: "asked if you want to do a practice pitch…".
- **Reply** on a mail thread: Wisp drafts a reply in your voice. Edit it, then **Send**, or close it.
  - Only tap Send if you mean it: it really sends from Mail.
  - Safest demo: open the draft, show it, then Cancel.

### 2. Day: "what do I do, visually" (30 s)
- **Day** tab: the hour lane with calendar events, plus the blocks Wisp just booked (e.g. the reading at 9:30).
- All-day items Wisp has already held show "✓ 9:30 AM", dimmed.
- "Still to fit" lists untimed to-dos with their source icons.

### 3. Ask: anything else (45 s)
Known-good prompts, checked live on build 2522 at 11:30:
- "good morning wisp" (5 s)
- "What should I do today?" gives a day plan from every source (3 s)
- "What did Mom text me?" (3 s)
- "What's on my calendar tomorrow?" (3 s)
- "How do I connect my Google account to you?" gives the Connect Google steps (2 s)
- "Write a short thank-you note to my professor" (1 s)
- "Why is the sky blue?" (1 s)

Avoid these, which were bad live: "Which emails need a reply from me?" (lists Indeed job spam),
"Anything important in my email?" (picks ads), weather (location lookup fails), "When is the W-9 due?".

Avoid on stage: long compose requests (they can take 20 s+), and anything that sends.

### 4. The judge moment (60 s)
1. Notch ▸ gear (Quick Settings) ▸ **Google ▸ Connect Google**.
2. The browser opens. The judge signs in and allows read-only Gmail and Calendar.
3. Back in the notch, the account chip row shows **All · You · judge@…**. Tap the judge's chip.
4. In about 15–30 s their Brief shows *their* to-dos and cards, built from their own inbox and calendar.
   (On the dry run: ready in 13 s, cards at 21 s.)
5. *Say:* "Read-only, local, and when we're done, I erase it."
6. **Disconnect & erase**, in the same Google section. All their mail, events and proactive state are deleted.
   Verified on the dry run.

## If something goes wrong
- **Brief is empty or stuck on Working.** Tap Analyze now. Still stuck after 2 min: quit and reopen Wisp.
- **Ask says "loading model…".** That's the local model swapping. Wait about 10 s.
- **The judge's sign-in is blocked.** Use your spare Gmail.
- **Wisp won't start.** Roll back to the previous build:
  ```bash
  osascript -e 'quit app "Wisp"'; mv /Applications/Wisp.app ~/Desktop/Wisp-broken.app && cp -R ~/Desktop/MOE_Project-local-backup-20261017/Wisp-0.5.0-2509-previous.app /Applications/Wisp.app && open /Applications/Wisp.app
  ```
  The folder `~/Desktop/MOE_Project-local-backup-20261017/` has every earlier build. The newest
  `Wisp-0.5.0-*-previous.app` is the one before the current install.

## What's real vs. not (if a judge asks)
- **Everything runs locally:** a fine-tuned 4-bit model on oMLX on this MacBook. No cloud calls for reading or summarizing.
- **Pipeline:** per-item triage (junk / sensitive / asks of me), then a judge that ranks at most 8 items, then
  at most 5 prepared cards. It only acts on a tap.
- **Guards:** quotes are copied from the source by code, never generated. Invented sources are dropped. A PII filter blocks
  card numbers, passwords and codes. Email content is fenced as untrusted, so "ignore previous instructions" emails can't
  steer it.
- **Sends:** every send needs the tap, goes through a signed single-use envelope, and has its recipient locked.

## Gemma 4 E4B version (live since 12:46)
The model is `gemma-4-E4B-it-oQ4e-mtp`: about 5 GB, 4-bit, served by oMLX on this Mac.

**Talking point:** "Every word you see is written by Gemma 4 E4B running on this laptop. It reads my mail, texts and
calendar, picks Wisp's tools itself, and nothing leaves the machine."

**Order:**
1. **Brief:** the lede, the cards (tap one block), and the summaries.
2. **Ask, the best prompts on Gemma:**
   - "good morning wisp"
   - "How do I connect my Google account to you?"
   - "Write a short thank-you note to my professor"
   - "What's on my calendar tomorrow?"
   - "Why is the sky blue?"
3. **Judge:** Connect Google; their cards appear in about 20–30 s; then Disconnect & erase.
4. **Optional proof it's local, done last:** turn Wi-Fi off and ask "What's on my calendar tomorrow?". It still answers.
   Connect Google and web/weather questions need Wi-Fi, so do this after the judge step.

**Avoid on Gemma:**
- "What should I do today?": 11 s, and it opens with a wrong "nothing scheduled".
- Anything that sends.
- Weather.

**Back to Ling:** run `rollback_live.sh` from the gemma scratch folder, or copy `~/.moe/config.yaml.pre-gemma-*` back to `~/.moe/config.yaml` and restart Wisp.
