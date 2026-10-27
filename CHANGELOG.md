# Changelog

All notable changes to Wisp are documented here.

## [Unreleased]

### Changed
- **Versions renumbered down by 1.0.** Wisp is not yet a finished product, so every release is now 0.x (1.4.0 became 0.4.0, 1.3.3 became 0.3.3, and so on). Release tags and GitHub releases were renamed to match, and the old v0.1.0 is now v0.0.0-initial. The next release is 0.5.0.

## [0.5.0-rc3] - Unreleased

Third release candidate for testers; it builds on rc2. See [docs/releases/0.5.0-rc3.md](docs/releases/0.5.0-rc3.md) for the release notes.

### Changed
- **A shorter menu-bar menu.** Right-clicking the menu-bar icon now shows Open Wisp Window, Settings…, Report a Problem… and Quit Wisp, plus **Finish Setup…** only while setup isn't finished. The other items moved:
  - change the model or engine in **Settings → Models**;
  - **Permissions…** and **More Access…** (Screen Recording, Accessibility, Full Disk Access; it was Grant More Access…) are in **Settings → Privacy & Access**;
  - **Export Chat Debug Log…** is in **Settings → General**;
  - Getting to know you is `/about`, or Memory → About you.

  Messages that named a removed item now point to its new place.
- **Brief cards stay put.** A shown card stays until it is handled, dismissed, answered or past due, and new cards are added after it, instead of every cycle re-ranking from scratch. A card whose source drops out of one read is kept for 2 hours. An ask is keyed by the conversation and the time of its latest incoming message, so only a new message makes a new card. An unchanged Brief is re-checked against the clock without another model call.
- **Mail suggestions read Mail's list of emails without waiting for the model.** The 6-hourly index refresh no longer waits for a quiet model; reading each email and the model call still do. Low Power Mode or a battery under 30% still holds everything off.

### Fixed
- **Allow on Reminders and Calendar no longer does nothing.**
  - While one permission question was up, every other Allow button was disabled; now only the row being asked is.
  - The click reads macOS's answer first. Undecided asks; denied or restricted opens that row's privacy page.
  - The ask's error is logged, and a request macOS left unanswered says to quit and reopen Wisp.
- **Mail & Notes and Messages keep their answer across launches.** The last definite Automation answer is saved, so a closed app (macOS error -600) no longer reads "Not set" after a relaunch. A denial (-1743) always replaces it, and a grant macOS dropped (-1744 after Allowed) says macOS forgot the answer. `/resetwisp` clears the saved answers.
- **A dismissed Brief card or cleared Needs-you item never comes back without something new.**
  - A dismissal no longer depends on a content hash that changed as old lines aged out.
  - A cycle no longer writes back a card dismissed while it ran.
  - A cleared to-do or thread also removes that conversation's card.
  - A "texts from X" Needs-you row keeps its id as texts age out.
  - Items cleared in an earlier build stay cleared.
- **Impossible clock times are refused.** "at 28", "25:30" or "13pm" no longer crash the date resolver (used by mail suggestions, reminders from messages, proactive suggestions and event capture), and "13pm" is no longer read as 1 pm.
- **One bad email can't stall mail suggestions.** An email whose processing fails is retried once, then set aside, and the queue moves on.
- **A failed Mail read is no longer silent.** It is logged as a warning, at most once an hour while the same reason repeats, and shown as `health` in the mail-suggestions status. **Settings → Privacy & Access → Mail suggestions** shows the reason in plain words ("Couldn't read Mail: … Turn on Full Disk Access …") until a read works again. Logs and Settings carry only short reason codes and exception type names, never email text.
- **Email summaries list each email once.** A combined digest no longer repeats the per-account ones, and an email present in both Gmail and Mail is shown once.
- **`/resetwisp` stops background work before erasing.** It also stops mail suggestions, the Brief's mail and text gists, the attention runner, the proactive cycle and the daily-plan email, so none can write files back, or send, after the erase.

## [0.5.0-rc2] - Unreleased

Second release candidate for testers; it builds on rc1. See [docs/releases/0.5.0-rc2.md](docs/releases/0.5.0-rc2.md) for the release notes.

### Added
- **`/resetwisp`.** Typed in full in the notch and confirmed on a destructive card, it returns Wisp to its first launch: Google is disconnected and revoked, `~/.moe` is erased except the engine, the selected model and their provisioning files, Wisp's own Keychain items, defaults, caches and notifications are cleared, `tccutil` resets Wisp's permissions, and Wisp relaunches into the setup window. A `.reset-pending` marker makes an interrupted reset finish on the next launch (at most three passes, never touching data created after the first). Only the Wisp app that started the service can call the route; the agent and shell commands can't. Internet Accounts and the data in Mail, Calendar, Notes, Reminders and Messages are never touched.
- **Ask by voice.** Press **⌥⇧Space** from any app, or click the mic in Ask (or in Brief and Day to ask about your day), and say your question. Wisp listens until you pause (at most 30 seconds), and the words appear in Ask and send like typed text. Your Ask model hears the clip on this Mac and turns it into text: Gemma 4 E4B, the default, does this in about a second. Wisp keeps no copy of the recording, and it never leaves your Mac. A model that can't hear (one with no audio encoder, or a cloud model) is refused before any audio is sent, so a made-up transcript never becomes your question. The microphone permission is asked for the first time you press the mic, never before.
- **Answers read aloud.** Answers to spoken questions are read with your Mac's own voice (System Settings ▸ Accessibility ▸ Spoken Content). **Read answers aloud** in the notch's settings can switch this to every answer or to never. The mic button stops the voice. VoiceOver now announces every answer and error in the notch.
- **Accessibility in the notch.** Reduce Motion (System Settings ▸ Accessibility ▸ Display) stops the looping animations (the breathing orb, the thinking dots, the listening pulse) and drops the fades. VoiceOver reads labels on the notch's controls and announces every answer and error. **Increase Contrast** makes secondary text, hairlines and outlines brighter. **Settings ▸ Voice & accessibility ▸ Text size** (Default, Large, Extra large) enlarges the notch's text by up to 30%; Settings and the Wisp window are unchanged.

- **Suggestions from older mail.** Between questions and while you're away, Wisp reads the last 90 days of Sent mail and Mail's Primary, Transactions and Updates (never Promotions, spam, trash or archive; with Google connected, Gmail's Promotions, Social and Forums tabs are skipped too when Wisp can list them), one email at a time on the local model, and finds dated things to do. The Brief's **Found in your mail** shows up to three that are due within 30 days and important: your own promise, a person's request, money, something that expires or renews, or an appointment. **Add** writes an Apple Reminder (or a Wisp reminder, and says so, when Reminders won't take it); **Dismiss** and **Open** too. Due dates are computed in code from the email's words, and an item must quote the email verbatim. The app reads Mail's Envelope Index and `.emlx` files (Full Disk Access) one request at a time; the backend never loads or swaps a model, stands down for any turn or other model call, and stores findings in `~/.moe/mail_memory/` (0600). **Settings ▸ Privacy & Access ▸ Mail suggestions**: Show important (default), Record only, Off.

### Changed
- **Wisp takes about 2 GB less disk.** The engine's prompt cache is capped at 2 GB instead of 4 GB (an existing 4 GB limit is lowered the next time the engine starts; a smaller limit you set stays), and its image cache at 256 MB instead of 1 GB. The built-in engine also drops 108 MB of build-only files it never runs. Replies may be a little slower on the first turn after the model switches.
- **Gentle is Wisp's default voice.** Replies are now calm and kind instead of upbeat: they lead with what is going well, use at most one emoji (none is fine) and no stacked exclamation marks, and come back organized, with the direct answer first, short paragraphs, lists for several items, key facts in bold and a short heading only for clearly separate parts. Getting to know you lists **Gentle** first and says it is the default; **Upbeat** and **Straight to the point** are still there. Your saved profile moves to version 3: a saved tone of Upbeat is read as the new default, Gentle, because earlier versions saved Upbeat for everyone who finished the questions, so a deliberate choice of it can't be told apart and has to be picked again. A saved Gentle or Straight to the point is kept. Detail is unchanged.
- The Mail & Notes and Messages rows in setup now say Wisp "opens Mail and Notes for a moment to ask, then closes them".

### Fixed
- Setup now looks for your own oMLX for 30 seconds before it says anything is wrong. The step-1 card reads "Looking for your oMLX…" (it used to read "Connecting to your oMLX…" and offer **Try again** after 10 seconds), and an oMLX that is already running but hasn't listed its models yet counts as still being looked for, so no "can't run" text and no error from an automatic start shows during the window. After 30 seconds the card offers **Try again** and says your oMLX isn't running; **Try again** restarts the whole 30 seconds. Reopening setup from **Permissions…** after finishing it uses the same 30-second window and never shows "Connecting…" forever; the difference is that Wisp starts your oMLX by itself only during an unfinished first run, so a reopened window waits for your oMLX and for a click on **Try again**.
- Allowing Mail and Notes, or Messages, in setup no longer opens those apps for you to see. Wisp starts an app only if it isn't already running, keeps it hidden, and closes it again once macOS has asked (never an app you had open, brought forward or began using). Wisp remembers your answer for the rows, so Mail & Notes and Messages no longer flip back to "Not set" when the app is closed again; a real Allowed or Denied read from macOS always replaces the remembered answer. The remembered answer lasts while Wisp is open.
- Questions with a stable answer, such as "what makes the sky blue", are answered from the model's own knowledge when web search isn't offered for that question, instead of being refused as "not available". Live data (weather, prices, scores, news, a specific page) still says it can't be looked up, and Wisp never says it searched.
- The first Brief starts when setup finishes, not at its usual hour. When setup reaches its last step during an unfinished first run and the model answers, the app tells the service, and the first launch day's morning Brief is composed then instead of at its hour (not before 5 a.m., and never twice). A permission you skipped in setup no longer makes the day-one summary wait for it. If the model isn't ready at the last step, the Brief comes at its normal time.
- A reply card's quote and one-line summary now match its draft. When a conversation went on after an earlier ask you had already answered, the card quotes the latest lines you haven't answered, and says "They texted again after your last reply." only when its summary doesn't mention them. A card for a text you never answered keeps its own summary and quote, and no longer claims they texted "after your last reply".

## [0.5.0-rc1] - 2026-10-26

First release candidate for testers; see [docs/releases/0.5.0-rc1.md](docs/releases/0.5.0-rc1.md) for the release notes. It is ad-hoc signed, not notarized: on macOS 15 and later the first open is blocked and must be allowed under System Settings ▸ Privacy & Security ▸ **Open Anyway**. Apple silicon only; the built-in engine needs macOS 26.2 or newer; 16 GB of memory is the minimum and 24 GB is recommended.

### Added
- **First-run setup window.** The first launch opens one window with five steps: Model, Permissions, Google, About you and How to use. Every step is optional and can be finished later from the menu bar (**Finish Setup...**).
  - It picks Gemma 4 E4B (about 5.5 GB) and says so plainly, warns before downloading if the Mac is short of disk space, shows progress and "No progress, check your connection", and titles a stopped download "Download paused" or "Download failed".
  - Return on the first step downloads. Quitting or closing early brings the window back, at the same step, on the next launches; after three the notch's setup cue carries it.
  - When the model is ready, Wisp checks that it answers, and posts a "Wisp is ready" notification only if the window is closed or minimized.
  - Calendar and Reminders are separate rows; Mail and Notes (Automation) and Full Disk Access are separate rows, and Full Disk Access is checked by a real read of Messages, Mail or Notes. Wisp never asks for a permission by itself, at launch or in the background: each prompt follows a click on that row's **Allow** button. If Brief or Day says a source isn't allowed, its **Allow in Wisp…** link opens the Permissions step, and **Permissions…** in the menu-bar icon's menu reopens it at any time.
  - VoiceOver labels and announcements; the window fits small displays; the tour teaches only the shortcuts that really work on this Mac.
- **Gemma 4 E4B is the default model**, with its engine settings (sampling, thinking, multi-token prediction, a 16k context window) written by Wisp for the bundled engine. A model's engine settings are applied once per version, so later tuning in the engine's admin UI is kept.
- **Proactive Brief ("Before you ask").** Brief opens with up to five cards for things Wisp already looked into across your mail, texts, notes, calendar and reminders, with security and account alerts first and one-tap actions that draft, remind, add or hold time. A card clears itself within about a minute when you handle it yourself. × clears a Brief row at once and ✓ marks a reminder done. Day places timed to-dos on its timeline.
- **Connect Google for testers.** A tester build can carry a built-in Google sign-in. The account must be on the developer's tester list, Google shows an "unverified app" warning (choose Continue), and the sign-in expires every 7 days while Wisp is in testing: Wisp posts one "Google sign-in expired" notification and Settings ▸ Google offers to reconnect. One sign-in asks, read-only, for Gmail (required), Calendar, Drive, Docs, Sheets, Slides, Chat, Tasks and Contacts; any but Gmail can be unticked and added later with Reconnect. The Google tools run through a pinned `workspace-mcp` 2.2.0 that the first connection sets up, with a `uv` 0.12.23 bundled in the app (pinned by size and SHA-256), so nothing needs installing. A client you paste into Settings ▸ Google is always used before the built-in one. See [docs/google-cloud-setup.md](docs/google-cloud-setup.md) and [docs/build-release.md](docs/build-release.md).
- **Brief and Day on a first day.** A source Wisp couldn't read is never an all-clear: the row says why and links to the System Settings page that fixes it (Calendars, Reminders, Full Disk Access, Automation, Internet Accounts). A Google account whose mail wasn't checked is named, as "hasn't finished checking" or "reconnect". The day-one Daily Summary waits up to 15 minutes after first launch for permissions still unanswered, then is rebuilt once when a missing source arrives or Getting to know you finishes. Routine actions are only offered for the first 3 days (or until you tap Useful or Undo once). A reply card opens its draft first, and the service refuses to send a reply without the shown words.
- **Ask answers questions about Wisp.** "What can you do?", "Who are you?", "Is my data private?" and "How do I use Wisp?" are answered by Wisp itself, without the model, from what is live on this Mac. Getting to know you stays on this Mac, goes only to Wisp's own model or your verified tailnet Mac (never an optional cloud model), and Erase deletes every copy.
- **Long-email digests in Ask.** An email too long for Ask's tool result is digested whole by the on-Mac model (thinking off, no tools, one at a time, 20 s limit) instead of cut off at about 6,000 characters; dates, amounts, codes and times are repeated verbatim. Mail is never sent off the Mac for it, and any failure returns the email as before.
- **Model download in the notch's setup card.** Progress, speed and time left; the Mac is kept awake while bytes move and let go after 10 minutes with no progress, 90 s of failed reads or 6 hours; Wisp still switches to the model when it finishes. Macs under 16 GB are not offered a model the engine would refuse to load.
- **Wisp on a Mac without a notch** (M1 Air, 13" Pro, iMac): no bar is drawn. The menu-bar icon shows the status, and pushing the pointer against the top-centre edge opens the panel as a card under the menu bar.

### Changed
- Automatic calendar and reminder adds from past mail and texts are off by default. The same items arrive as tap-to-add cards. Set `proactive_auto_add: true` to turn them on.
- The Google sign-in is now one connection with the read-only Workspace scopes up front, instead of mail and calendar first and a reconnect for the rest.
- Quitting unloads every resident model and then stops the engine, for the bundled engine and for an oMLX you run yourself. A model call that arrives during quit can no longer start the engine again.
- The bundled engine's memory guard starts on "aggressive", so Gemma loads on a 24 GB Mac with other apps open. An install that had "balanced" is moved once; other tiers you chose are kept.
- The menu item "Set Up Inference..." is now **Finish Setup...** until Wisp can answer and **Model & Engine...** after that.
- **Grant More Access...** asks only for what is off, one system surface at a time, and no longer requests Input Monitoring.
- New **Permissions...** item in the menu-bar icon's right-click menu. It opens the setup window at the Permissions step.
- Calendar, Reminders, Notifications and Contacts are no longer requested at launch or by their readers. Contacts is offered once Messages is readable.
- Setup appears in one place at a time: while the first-run window is up, the Dock icon, the menu and the notch's Continue setup bring it forward instead of opening a second surface. A Dock click brings forward exactly one window, and every window can be minimized.
- Setup uses an oMLX you already run instead of downloading again: it starts it once, shows "Connecting to your oMLX…", and offers **Try again** after about 10 seconds. Setup finishes only on **Done**; a quit, an early close or macOS's Quit & Reopen after Full Disk Access resumes it at the same step. Permissions has a **Messages** row. Ask's **Try another** row keeps unused starters reachable.
- Priority email evidence cuts each body to 400 characters and gives mail a larger budget, so one long newsletter no longer hides the rest; each record carries its unread flag.
- The Mellum R2 dataset is archived and its source-contract check retired, since Wisp no longer uses Mellum.
- Accessibility requests from the search panel show one system surface instead of a prompt and a Settings page together.

### Security and privacy
- Shell and AppleScript commands that name Wisp's local service on a loopback address are denied, including percent-encoded and variable-built paths, so a command can't trigger Wisp's own send and approval routes, even under full access.
- The cloud "Everything" option is removed from Settings. A config set by hand still works and says so.
- Under full access, Wisp still asks first before running AppleScript, or a shell command that could send something off the Mac, drive another app or read a key or saved sign-in, and before opening Shortcuts, Terminal, non-web links or files that run something; the card shows the command, script or the script file a command runs. After a turn has read a file, page, mail or message, opening a web link, fetching a page, a Shortcut, call, web request or MCP or skill action asks too. A web page can no longer make Wisp's local service act.

### Known issues
- Not notarized (see above). Google sign-in is limited to the tester list and expires every 7 days. The first Google connection downloads the Google tools, so it needs internet.
- Reopening setup from **Permissions…** after finishing it, with your own oMLX off, can show "Connecting…" with no **Try again**; the Mail and Notes and Messages rows read "Not set" while those apps are closed. A long-email digest adds about 5 seconds.

## [0.5.0] - In preparation

Wisp 0.5.0 adds a first-run setup window with the built-in engine and Gemma 4 E4B, optional onboarding, proactive Brief cards, common-prompt routing, and notch refinements. The first release candidate, 0.5.0-rc1, is described above. A final fresh-install run, live model qualification and installed-app qualification remain pending; catalog recommendations are not model qualification. Public ad-hoc builds are not notarized. The entries below describe the prepared source behavior and some were refined for rc1, subject to validation of the final candidate.

### Added
- **Getting to know you.** Once Wisp can answer, the notch asks three optional questions: what to call you, your goals and how you like replies. Each answer says what Wisp does with it, and everything stays on this Mac (`~/.moe/profile.json`). Your name and goals shape the brief's greeting and a "Keeping in mind" section, people you name rank higher in Day, and your tone shapes local replies. It is never sent to an optional cloud model. Reopen it with `/about`, **Tell Wisp About You…** in the menu bar, or Memory ▸ About you. (In 0.5.0-rc1 a new install answers it as the setup window's About you step.)
- **Evening check-in and weekly goal pulse** on the Day tab. Mark each task Done, Tomorrow or Drop, and each event for tomorrow Keep, Move or Cancel. Move and Cancel only prefill Ask; nothing changes in Calendar on its own. Once a week, mark each goal On track, Behind or Done. A goal that is behind leads the brief.
- **First launch** steps in the README and on the release page for opening an app that isn't notarized.
- When Wisp's service isn't running, Ask says so in red with **Retry** and **Report**, instead of a green "Running locally".
- The menu-bar icon, the attach and send buttons, the quick-settings switches and the setup steps have VoiceOver labels.
- **Chat switcher in the notch.** The chip under the header names the conversation and lists recent saved chats, so you can continue one without opening the window.
- **Slash commands in the notch.** Type `/today`, `/memory`, `/research`, `/settings`, `/report`, `/window` or `/new` alone in the Ask field. A path or any other text starting with "/" is still sent as a prompt.
- **Details under each reply** show the model, route, timing and tool calls. Debug Mode now opens them by default instead of adding a separate line.
- **Keep open (pin, ⌘P).** A pinned notch doesn't tuck away when the pointer leaves or you switch apps. Any explicit collapse unpins it. A message draft you're editing also keeps the notch open.
- Notch shortcuts: ⌘N new chat, ⌘O open the current tab in the Wisp window, ⌘1 to ⌘3 switch tabs.
- **Tabs in the notch: Ask, Brief and Day.** The tab is kept when the notch tucks away. ⌥Space and the menu-bar icon always open Ask. Hovering opens the tab you last used if the notch closed in the last ten minutes, otherwise Ask.
- **Quick settings in the notch.** The gear (or `/settings`) shows the switches you flip most in three cards with one line each: **Permissions** (Full access, with an amber switch and "On" while it's on, and Learn my routines), **Daily brief** (the Morning and Evening times; each opens an inline hour list) and **Advanced** (**Reply details**, which is Debug Mode, and **Model memory**). Model memory names the loaded model and the memory the engine measured for it, with a bar against this Mac's memory and **Unload**; it shows no number when the engine measured none, and "Not loaded" when nothing is. Done goes back to the tab you were on. Everything else, including models, Google, cloud keys and the privacy source toggles, stays in Settings (**All settings**, ⌘,). Quick settings and Settings share one source, so a change waiting for confirmation looks the same in both and survives the notch tucking away.
- **Brief in the notch.** One plain sentence about your day, then a short version of today's Daily Summary: what Wisp is keeping in mind and the rest of today's events (time · title, all-day first), without the mail and messages write-ups. Without a summary from today yet, it's just the sentence. Below that are always four rows, **CAL**, **MAIL**, **TEXTS** and **SOON**. The calendar row says how busy the rest of the day is ("Light day: just PA 2 (all day)", "Busy afternoon: 3 meetings, 1–5 PM", "Packed: back-to-back 10 AM–4 PM", "Free all day"), from your calendar and fixed Wisp events only (never learned away times), with "about" when an event has no end time. MAIL counts the mail that needs you without unmistakable newsletters and promos ("You're subscribed…", "50% off…"), and says how many it left out ("+3 promos"). TEXTS says "No texts need you" when nothing does. SOON is the nearest calendar event or reminder in the next week, or "Nothing in the next week". A source Wisp can't read yet (a calendar that hasn't synced, mail or messages not checked yet, a week ahead not loaded) shows a dim row saying so instead of disappearing, and SOON says "Nothing in the next week" only once calendar and reminders are read. Click a row to open its details in place: today's events with times, the mail that needs you plus the summary's mail notes (and how many promos were left out), the summary's messages notes plus texts that need you, or the next five upcoming items, each ending with **Open Calendar/Mail/Messages/Reminders**. One row opens at a time. The sentence opens with the same word as the calendar row ("A light Saturday."), or with just the day when the calendar hasn't synced. **Ask about this** continues from it, and **See my day →** opens Day. Brief only reads; doing things happens in Ask and Day. Refresh composes the scheduled brief now.
- **Day in the notch: the rest of today, hour by hour.** A two-line note on what's up and why the day is laid out that way, an all-day strip, and a timeline with a "now" line. On today it starts two hours before the current hour (never before midnight), with what already happened there drawn dim, and runs at least three hours ahead, to the end of working hours or your last item. Your calendar (solid), your own tasks, Wisp's suggestions (dashed, **Keep** or a later time from ⋯), learned "usually busy/away" times (hatched, **Not today**), plans caught in your texts (amber, **Add** or **Ignore**) and reminders due today (at their due time, red once overdue, **Plan time**) share one clock. A legend lists only what's shown. Below it: **Make time for…**, **Replan**, **Full week ↗** (the planner in the window), and ⋯ for Running 15 minutes late (only once your tasks have times) and Report a problem. Nothing reaches Calendar or Reminders from here: Keep, Add and Plan time put tasks on Wisp's plan, and Not today sets a learned window aside for that day only. The planner's warnings read in plain words, and placeholder reminders such as "nothing" are left out. Brief and Day load while they are open, never at launch.
- **Old undated reminders: keep or delete, in Day.** A reminder with no due date that is about a week old is asked about once, under Day's timeline: **Keep** (Wisp won't ask again) or **Delete** (removed from Reminders through the same verified delete as every other reminder change, only when you click it). Nothing else in Wisp deletes a reminder. If Reminders hasn't synced, or Wisp can't confirm the delete, the row says so plainly and offers no second Delete, so a retry can't act on something that already changed. The questions refresh every minute while Day is open and right after you answer.
- **A setup card in the notch.** When Wisp can't answer yet and the first-run window is closed, the notch shows a setup card with the two required steps, an engine and a model, plus a test (**Use & test**). It keeps the notch open until you choose **Done** or **Later**. Later leaves a one-line reminder on Ask and an orange "!" on the collapsed bar, on every launch until Wisp can answer. The menu-bar item reads **Finish Setup...** until Wisp can answer and **Model & Engine...** after that. Smart Search, other local apps and model downloads continue in the setup window (⤢). Settings still opens the full guide. (The first launch itself now opens the first-run window; see 0.5.0-rc1.)
- **The collapsed notch shows what's due.** The bar and the Day tab count what is due today or overdue (from the day's last load; the bar never loads anything itself). Undated items stay in the Brief. An orange "!" for setup set aside takes its place.
- Clicking a Wisp notification other than a nudge (a reminder, something Wisp added for you, a notice about a scheduled message) now opens Day in the notch. Before, it only brought Wisp forward. Nudge buttons work as before.
- **Report a problem, inline.** Report a problem now unfolds under the reply it is about, in the notch, instead of opening a new window on every click. Today's report unfolds in the notch's Day tab (⋯ ▸ Report a problem) and in the planner window. Each place keeps one report open at a time. A report on another reply starts over, with an empty note and details off, so neither carries over. The preview shows exactly the bytes **Save report…** writes, and **Larger** shows more of it. An open report keeps the notch open (Today's, while the notch shows Day). New chat closes a report on the conversation it leaves. **Report a Problem…** in the menu bar and `/report` open it under the last reply. Reports are still saved only where you choose, privately, and never uploaded. A report opened from the menu bar or `/report` scrolls into view under its reply, and reopening the report on a dropped reply keeps its note.
- **The full planner is in the Wisp window.** Today's planner (Needs my attention, Day plan, the task editor, pins, working hours and Email my day plan) is now the window's Today section instead of a separate Daily Summary window. **Full week ↗** and ⌘O in the notch's Brief and Day tabs and the sidebar's Today open it. It shares one day with the notch's Brief and Day tabs.
- **Research in the Wisp window.** Saved research jobs are now the window's Research section (the window's sidebar, **Research library** on the notch's research prompt, or Settings) instead of their own window. Opening a job shows it in the notch, and **New research** opens the notch's research prompt instead of a separate dialog.
- **Memory in the Wisp window.** Memory and What Wisp has learned are one Memory section with three tabs: **Facts** (saved memories and their corrections), **Routines** (what Wisp learned from your activity) and **Review** (new memories to confirm, edit or set aside). Open it from the window's sidebar, `/memory` in the notch, the notch's review notice, or Settings.

### Changed
- Full access now shows only what Wisp's service confirmed. If it can't be read or a change can't be confirmed, Settings, quick settings and the Wisp window say it is unconfirmed instead of showing "off", and offer Check status and Turn off. Before, an unreachable service read as "off" even if Full access was still on. The Wisp window's Full access pill follows changes made anywhere.
- The setup card and the setup window share one setup state, so a running action, readiness and the test result are the same in both. "Test passed" now clears when Settings changes a role's model, a model provider or the engine mode, not only when setup notices the change. It also clears when the last place showing setup closes, as closing the setup window always did.
- The menu bar's Debug Mode item is gone; Debug Mode is **Reply details** in the notch's quick settings. The unused Research window and the New Research dialog are removed.
- The separate Memory and What Wisp has learned windows are gone; both open the Wisp window's Memory section on the matching tab. What each one stores, and how review, correction and Forget work, are unchanged.
- The Daily Summary now composes itself **every morning and every evening** (8 AM and 8 PM by default), so a fresh summary is waiting without pressing the button. Before, it ran once a day at the time the AM/PM toggle chose. A run missed while the Mac slept or Wisp was closed arrives on the next launch or wake, until the next run is due. Only one run happens at a time. A run never starts while you are waiting on Wisp, and it steps aside if you ask something mid-run, then retries afterwards. Change the hours with `POST /assistant/summary_schedule` `{"times": {"morning": 7, "evening": 18}}` (morning 0–11, evening 12–23). The AM/PM toggle no longer chooses a time.
- The Daily Summary brief now appears on the Brief tab instead of in the chat. **Refresh** composes a new one, and **Ask about this** continues from it in Ask. A brief you compose is saved as its own chat, no longer added to the open one.
- The notch header's Research, Today and Daily Summary chips and the AM/PM brief toggle, which no longer picks a time, are replaced by the tabs. Research is a mode of Ask rather than a tab: `/research`, a job opened from the window, or New research turns the prompt into a research plan, and choosing a tab leaves it.
- Wisp now starts in the notch. Launching it no longer opens the Wisp window, except that the very first launch opens the first-run setup window. Open the window from the Dock icon, the menu bar's **Open Wisp Window**, or **Window ▸ Wisp Window** (⌘1).

### Fixed

#### A fresh install
- The built-in engine answers its first question. Its memory guard refused every model it had just loaded ("The AI engine's memory guard could not be verified"), because a background check stayed pending for the whole load. A fresh install with no oMLX.app could finish setup and still never answer.
- Switching models right after a reply (for example, a code question that loads the coding model) is no longer refused by the same memory guard while the new model loads.
- Quitting Wisp always stops the built-in engine and unloads the models. A reply or background task still running at quit could start it again, leaving it holding the model's memory after Wisp closed. This also covers an oMLX you run yourself.
- Wisp no longer stops at launch on a Mac where oMLX was never opened or isn't installed. A missing or unreadable `~/.omlx/settings.json` used to stop the service before setup could appear.
- A Mac with no model is offered a public download, and the setup guide's first choice on every Mac is Gemma 4 E4B (about 5.5 GB; changed from Ling 3.0 tiny in rc1). Ling 3.0 tiny stays available: 6-bit on 24 GB or more, and 4-bit below that; on 16 GB, an already installed 5-bit build remains preferred. Existing role overrides, working models and embedding settings are kept. Mellum 2.1 Wisp R1 remains optional and marked validation pending; no model is downloaded unasked.
- After a download, the new model is put to use automatically when no role points at an installed model. The built-in engine starts on its own the first time setup is open. **Start engine** stays for retries.
- On macOS older than 26, setup no longer sends you to download an oMLX that can't run there. It says what's supported and points to Ollama, LM Studio, llama.cpp or a remote model.
- The first-run window is the only thing that asks for access while it is open, and every permission in it is optional (Calendar, Reminders, Mail and Notes, Full Disk Access, Contacts, Notifications). Screen Recording and Accessibility are asked for when a feature first needs them, instead of six prompts at once on top of the setup card.
- **Grant More Access…** in the menu bar lists only what is still off (Screen Recording, Accessibility, Full Disk Access for Messages, Mail and Notes), each with its reason, and asks for one at a time. Each request shows one system surface: the prompt the first time, its Settings page after that.
- A slow first launch shows "Starting Wisp…" instead of a red "Wisp's service isn't running", and a service busy answering is never called down. Retry shows that it's starting.
- Opening oMLX.app after Wisp started (or changing its key or port) takes effect without restarting Wisp.
- Setup no longer repoints a role you pointed at LM Studio or Ollama on purpose when it puts a new model to use.
- If the service takes a while to start on first launch, Wisp waits up to about three minutes instead of 30 seconds. If it still hasn't started, the notch says so instead of showing nothing.

#### Getting to know you
- The questions are offered whenever Wisp is quiet and the service is up, for as long as Wisp runs (checked at start, after the panel closes, on wake and every half hour), not by a one-shot wait that gave up. A Later is offered again after three days.
- Leaving from the summary screen counts as finished, so it doesn't come back at every launch.
- Return can't save twice or skip a step, and an empty name never erases a saved one.
- Choosing another tab, a shortcut, a notification or closing the notch leaves the questions at once. Closing counts as Later.
- An offer nobody asked for doesn't take the keyboard from the app you're typing in.
- It never opens over a notch you're using, and closing with ⌥Space counts as Later, like the chevron and Esc.
- If your answers can't be saved, **Show my brief** says so and stays, instead of quietly asking again later.
- Wisp calls you by the name you gave it, even when it differs from your Mac account name, and "what's my name?" or "what are my goals?" are answered from what you told it instead of searching your calendar.
- If the service is down, the questions show an error with Retry, and never say "Thank you" when nothing was saved.
- The goals step scrolls, so its buttons are never cut off on a short screen.
- A damaged `profile.json` is kept as `profile.json.bad-<time>` instead of being overwritten.

#### Everyday asks
These now go straight to the right tool:
- Timers ("set a timer for 10 minutes") and "remind me in 20 minutes to…".
- The date and time ("what's the date today", "what time is it in Tokyo"). These used to search the web. "What's the date today" answers for this Mac's time zone.
- Weather asked many ways: "how's the weather in Boston", "weather tomorrow in Boston", "will it rain tomorrow", "should I bring an umbrella", "what should I wear today".
- The brief ("what's my day look like", "give me my morning brief", "brief me"). Wisp answers in plain words. Before, "give me my daily brief" showed the raw notes Wisp gathers for itself, such as "no cached absence is verified". That also no longer happens before Calendar has finished its first sync.
- Maths and conversions ("what's 15% of 240", "5 miles to km") and definitions ("define serendipity").
- "add a meeting with Sam tomorrow at 3pm" adds the event, and "move my 2pm to 4pm" moves it. Both still ask before changing your calendar.
- Mail: "summarize my unread mail" summarizes, and "who emailed me about the invoice" searches your mail.
- Volume and music ("turn it down", "next song", "set volume to 30").

Also fixed:
- "Tell me a joke" and "translate hello to Spanish" are answered without tools. They used to be offered email and shell tools.
- Personal "should I…" questions ("what should I be doing today") no longer search the web.
- A second request after a comma is kept: "find emails from Ana, delete them" and "what time is it in Tokyo, call Sam" are no longer cut down to just the lookup. "Portland, OR" and "Paris, France" still read as places.
- "What time is it in Paris right now" asks for Paris, not "Paris right now".
- "What's my schedule in Spanish" reads your calendar and answers in Spanish, instead of answering with no calendar at all. "The weather in Spanish" isn't a city.
- A timer is set only when you give a length or a time; "set a timer to remind me to email Bob" makes a reminder. "Remind me in 2 hours" asks what to remind you about.
- "9/11", "24/7", "what does Sam mean" and "a joke about my meeting" are no longer treated as maths, a definition or a plain joke.
- A skill's trigger matches whole words only: "my commute the day after" no longer offers the mute and volume controls.
- Weather errors say "check the spelling" only when the place wasn't found. Other errors say to try again in a moment, and ambiguous city names ask which one you mean without suggesting a place that may not exist.

#### Notch and window
- Deleting a research job takes a second click (**Delete?** then **Confirm**), so one stray click can't erase a report.
- Times follow your 12- or 24-hour setting in quick settings, Day and the planner. The Brief's times follow it too, and every 24-hour time reads the same way ("09:00").
- The Day tab's **Make time for…** field is ready to type into, and a failed load shows **Try again**.
- Switching between Brief and Day no longer reloads the day each time.
- The evening check-in loads once, not twice, so a task you just decided can't briefly come back.
- The research delete confirmation says **Keep**, not a second **Cancel** next to the one that stops research.
- Research in the notch uses the notch's own buttons and fields.
- "Daily Summary" is now "brief" throughout, and the window planner is titled Today.
- Brief-sync progress stops polling when the service is down instead of polling every second for the rest of the session.
- Attaching a large photo no longer stalls the notch, and JPEGs are sent as `image/jpeg`.
- Wisp limits oMLX's prompt cache to 4 GB, keeping any smaller limit you set, and deletes cache files unused for 7 days. Before, it could grow to half your free disk.

#### Brief
- The brief times in quick settings name their run, **Morning 8:00 AM** and **Evening 8:00 PM**. Two pills led by "AM" and "PM" looked like the old AM/PM switch, but both briefs run.
- An older Daily Summary the app hadn't acknowledged (for example, because its notification failed) no longer comes back on reconnect and replaces a newer one on the Today tab. A newer brief now retires any older one that is still waiting.

## [0.4.0] - 2026-10-16

Wisp now **learns your routines** and uses them to **suggest how to spend your day**. It reads your Calendar, completed reminders, Mail, Messages and Notes, and optionally your Gmail and your app/idle activity. From these it infers tentative patterns ("you usually reply to email around 1pm") and tentative demands ("this looks like it needs prep time"). It proposes time for them in **Today**.

Rules that never change in this release:
- **Scheduled is not done.** A calendar entry never proves you attended or finished something.
- **Everything inferred is tentative, cited, and has no authority.** Wisp cannot send messages or create, move or cancel real events because of an inference.
- **Your plans win.** Pins, your edits and calendar commitments always come first.
- **Off means off.** Disabling, revoking, expiry, changed content and Forget make dependent suggestions disappear.

### Added

#### Weekly behavior reports
- Weekly reports compare the last seven completed local dates against earlier covered periods.
- Weeks are compared only where source coverage supports it. Incomplete coverage or missing event end times stay "unknown" instead of being guessed.
- Calendar entries are kept as scheduled evidence and are separate from dated task completions.
- Long events are kept by their valid end time. Ambiguous carry-in events no longer produce precise weekly values.

#### Device-use patterns and day planning
- Opt-in **app, idle, away and screen-state** observations support recurring patterns, found without app, site or scenario rules.
- Today can show **tentative busy windows**, **"possible time away"** (recurring computer inactivity), and **preparation suggestions** backed by a source.
- Time away never claims you are unavailable. Your personal availability stays unknown.
- Absence of activity alone, or a single point visit, can never become a confident "busy" label.
- Website visits give context only. They do not measure time spent on a site.
- Focus/DND and device activity Wisp never collected remain unknown.
- Planning fits your authored work first and puts optional away guidance after it. Pins, edits and calendar constraints are preserved.
- A new public planning context lets the daily planner read this behavior without refreshing sources or calling a model.

#### History seeding
- **Calendar and completed-reminder history** scans use resumable, permission-bound checkpoints, with a limit of eight native queries per run.
- **Bounded cross-source memory** indexes permitted Mail, Messages, Notes and retained Calendar/reminder evidence. It records metadata and source proofs, not copied bodies, and it trains nothing.
- Messages records that are individually complete are kept even when a whole publication is capped or partial.
- **Gmail history** is opt-in, per account, bounded, and started explicitly:
  - It needs a fresh Start after every restart.
  - Message bodies live only in an in-memory cache. There is no disk store and no permanent mailbox memory.
  - Removing a Gmail account is rejected if the request carries a browser Origin header.
- Progress separates **fetched**, **usable** and **analyzed** records, and partial coverage is always visible.
- Expired behavior memory recovers on its own, and capacity retries are bounded.

#### Consent and control
- Separate switches for **source evidence**, **semantic interpretation** and **remote analysis**.
- Suggestions require current source and permission evidence. Changed, expired or revoked proof withdraws them.
- Original analysis dates stay separate from the date a source was freshly verified.
- **Undo and Forget** use durable ownership receipts:
  - They keep work you edited, completed, or that another source owns.
  - They surface incomplete recovery instead of hiding it.
  - Forget removes both behavior databases, their WAL/SHM files and nudge events.
- **Settings are safe under rapid changes.** The Settings UI shows learning on/off only after the backend confirms:
  - A failed or unclear confirmation shows as **unknown**, never as Off.
  - Pending writes are serialized and stale replies are ignored.
  - The backend orders accepted changes through a bounded queue (16 pending) and a one-time control epoch. An older enable delayed in flight cannot switch learning back on after you turned it off.
  - Turning learning off never needs a successful status read first.
  - New `POST /behavior/settings/check` returns a settled receipt. Legacy GET remains a snapshot without proof.

#### Setup and models
- **Bundled-engine setup:** public model download with progress, cancel and retry, and no exposure of the engine's private key.
  - Setup guards check Host/Origin, require JSON, cap bodies at 4 KB and accept only catalogue models.
  - Upstream errors are scrubbed before reaching the client.
- Readiness requires a live model-list response. The setup test accepts only a complete visible assistant reply.
- Closing or changing the setup guide invalidates older test results.
- Setup distinguishes "Ready to test" from "Test passed" and shows the bundled engine's exact macOS requirement, including fractional versions. A missing or malformed packaged requirement cannot enable the engine, and a working compatible external engine stays usable.
- **Engine memory guard.** The bundled engine reports verifiable memory-guard health and Wisp checks it before guarded model allocation, including startup and lazy/fallback loads. Missing or stale enforcement evidence causes a safe refusal with no memory-setting change and no automatic retry. Package metadata separately binds Wisp's reproducible overlay and the original upstream engine.
- In 0.4.0, fresh installs defaulted to **`Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged-oQ4e`** for text roles, provisioned from a local folder. Existing overrides and embedding settings were kept. The prepared 0.5.0 behavior above replaces that default with Ling.
- Model-load memory refusals are reported as a clear app error (**HTTP 503**) before chat streaming or setup certification starts. A refused optional warmup no longer invalidates a successful model. Memory-fit estimates are separate from load and quality checks, existing engine memory settings are preserved, and a refused load needs an explicit retry.

#### Routing (from #239)
- Fixes for about 20 prompt families where no model could answer because the right tool was never offered, or a tool was forced on a plain chat request. These include weather, stock lookups, "reply to my landlord", weekday and tomorrow reminders, calendar offsets, notes, contacts and file searches.
- Offline coverage rose from partial to full on every audited family. For example, weekday reminders went from 112/168 to 168/168 and landlord replies from 0/9 to 9/9.
- Adds `scripts/toolbench/family_route_audit.py` to check routing offline.
- Follow-up repairs (#241) from the independent audit: a request after a dictated reply body is no longer dropped ("reply to Sam saying yes. Then quit Slack" keeps quitting the app), and a dictated question is no longer mistaken for a reply to the prior thread.

### Changed
- Planner context is rebuilt from live source evidence at read time, not from stored text.
- Native behavior snapshots, Gmail records and runtime publication now carry publication-authority stamps and ordered per-record witnesses. A changed or withdrawn source invalidates what depends on it.
- Planning times are stored as absolute times and survive restarts.

### Fixed
- **"What should I do tomorrow"** no longer shows raw tool JSON or an empty reply. Malformed or cut-off structured output falls back to plain text. Message verification codes are never quoted.
- A bare "tomorrow" passed as a search query is ignored instead of filtering every row away.
- **Concurrent database startup:** two parts of Wisp opening the private SQLite store at once no longer fail on a half-created file or a sidecar being deleted.
- Stale history rescans and unsupported outputs are corrected so every output is grounded in its source.
- A fresh-start setup test could pass on a stale result. The ordering is now enforced and tested.

### Security and privacy
- Inferred demands carry `action_authority: none`, and the daily workspace rejects any that do not.
- Disable, revoke and expiry withdraw suggestions from the plan. Queued behavior delivery is guarded against stale state.
- Store initialization needs write admission and the volume lease, and opens files with `O_NOFOLLOW`.
- Untrusted-browser request guards protect daily-workspace changes and setup downloads.
- Credential recovery withdraws publications in order, and a clock rollback is latched instead of trusted.

### Known limits
- History scans make eight native queries per run, with no per-minute cap.
- Live Calendar results, native transport cost, real-provider behavior and real-model behavior were not qualified on the exact final build.
- The selected Mellum model passed on the aggressive memory profile only. The balanced profile refused to load it twice earlier.
- A 503 for a model-load failure cannot be returned once a chat stream has already started.
- A stale settings epoch can reject a disable request (retrying without the epoch works).
- A 503 can include raw error text from the bundled engine, which should be checked for file paths.
- Setting WAL mode can change four header bytes in a valid empty database before an unsupported schema is refused (existing behavior).
- Some test totals are incomplete, and a few sandbox skips are disclosed in CI.

## [0.3.3] - 2026-10-14

### Added

- Wisp now includes its own copy of the oMLX AI engine. A Mac with no oMLX installed uses it automatically; one that already has oMLX keeps using it unless you switch in Settings > Models > Engine. The built-in engine runs privately on port 8791 with its own models folder (`~/.moe/engine`), so it never touches your oMLX settings or models, and Wisp refuses to send anything to a process on that port that isn't the engine it started. It needs macOS 26.2 or newer; older Macs keep using an installed oMLX. The app includes `Documentation/THIRD-PARTY-NOTICES.md` with the licences of everything the engine ships, including the copyleft ones and where to get their source.
- An opt-in planner (`WISP_PLANNER=1`, off by default) replaces free-form tool calling with a grammar-constrained JSON plan, a menu of the most relevant tools and explicit stopping rules (`not_found`, `cannot_do`, `clarify`). It reads before it writes, drops calls that already ran and caps repeated reads. If the first plan answers or asks a question without looking anything up, it re-plans once with a lookup required; after a miss it looks once more in families nothing has tried yet. On a 59-case sample, Ling V3 passed 52 cases with it against 40 without, using about half the tool calls; it is off by default until it has been run on more real use.

### Changed

- When every call a step made was refused as a repeat of an identical read, the next step now runs with tools off and a one-line nudge to answer from the results already in hand. Before, a model could keep re-calling the refused tool until it ran out of steps, then end on a raw dump of tool output ("What do I need to do today?" took 8 steps and 35 seconds; it now takes 4 and answers in plain language). A step that mixes a refused repeat with a new call, or that still owes a required tool, is unchanged.
- The rules in the system prompt now describe only the tools the model is actually offered. When a tight context window drops tools, the rules are rebuilt from the survivors once per turn, so a turn spends fewer tokens on instructions for tools it cannot call. The installed-skills catalog is added only when `use_skill` is offered; a matched or active skill still loads on every route.
- Proactive attention reminders (still in shadow mode by default: they record what they would do and create nothing until you switch them to live):
  - A plan you proposed with a guessed time is no longer treated as settled when the other person agrees. "Dinner tomorrow at 9?" answered "sure" doesn't say whether you meant 9am or 9pm, so no reminder is made until you reply yourself. "Dinner tomorrow at 7pm?" answered "sure" still makes one.
  - One plan gets one reminder. A second message about the same plan is recognised by when the plan happens, not when its reminder fires, so it no longer makes a second reminder or uses a second slot of the daily limit, whatever the reminder's lead time.
    - A reminder you undid isn't made again by a later message about the same plan.
    - Two different people's plans at nearby times stay separate.
    - A crash while a reminder is being made can't lead to a duplicate later.
  - The audit log records reminder writes by id and a short fingerprint of the title, never the title itself, which quotes the message. Each line says whether the write succeeded, failed or ended unknown.
  - If the Mac's time zone can't be read, Wisp uses the last zone it read, but only if the Mac clock agrees with that zone's offset on every day of the coming year. A move to a zone that only shares today's offset (Los Angeles to Phoenix in summer) is caught, and the reminders pause instead.

### Fixed

- Proactive attention reminders no longer fall back to UTC when the Mac's time zone can't be determined, which would have read every time in a message hours off and filed its reminder at the wrong time. With no zone they now pause, and when the zone is known again they start fresh instead of making reminders for the messages that arrived in between.
- A daylight-saving change between two clock readings could make the known zone look unknown for one pass. The zone and the clock are now compared at the same instant.

## [0.3.2] - 2026-10-14

### Added

- Model-led routing (still off by default, `WISP_MODEL_LED_ROUTING=1`) now loads full schemas up front for the 12 tools that best match the request, and offers every other catalog tool by name only. Calling a name-only tool runs nothing: it loads that tool's schema, and the model calls it again on the next step with full arguments. Ling V2 used to call real tools directly and get an empty reply. Name-only tools are dropped first when a request doesn't fit the context window.
- The morning preview (7–10am) can now include the times you usually turn to Wisp, for example "checking email with Wisp around 1:35pm". These come from your learned Wisp-usage routines and only fill space the calendar, habit and contact predictions leave free: at most two, one per topic, and never the catch-all "general" topic. `predict_my_day` lists them too.
- "What Wisp has learned": a window listing every routine Wisp learned from your calendar, messages and Wisp use, grouped by kind, with the counts behind each one and today's predictions. Mark a routine "That's right" or "Not true" (a rejected routine stops shaping predictions, nudges and answers, and can be restored). Open it from Settings → Privacy & Access → "See what Wisp learned", or from the Chat sidebar.

### Changed

- Behavior learning picks up routines from sparser calendars. A habit now needs 3 occurrences in at least 3 separate weeks (was 4), and a recurring event, note or reminder is judged from when it first appeared, so office hours that started three weeks ago count as three for three instead of three of seven. Category routines (gym, focus time) still count every week of history, and Wisp-usage routines still need 4 asks.
- Wisp-usage routines read naturally: "You usually use Wisp around 12:15pm" and "You look things up with Wisp around 5pm" instead of "You ask Wisp about general / web".
- Wisp has 100 built-in tools instead of 182. Related tools were merged into one tool with a mode, so old phrasings still work: weather alerts, air quality and rain are `get_weather`; timers, alarms, sleep timers and the stopwatch are `set_timer` and `manage_timers`; battery, network and Bluetooth status are `system_status`; Wi-Fi, VoiceOver and dark mode join `toggle_setting`; Music and Spotify are `music`; podcasts, audiobooks, radio and ambient sound are `play_audio`; archive, mark read and flag are `email_triage`. "Always allow" and "never allow" choices made for a merged tool still cover only what they covered before.
- Within one turn, a read the model already made with identical arguments isn't run again. The model gets "already ran with these exact arguments… It was NOT run again" and answers from the earlier result. Status reads (battery, volume, network, running apps, clipboard, screen and similar) still run every time, and any successful write or action clears the record, so a read after a write sees fresh data.
- Requests to the local oMLX engine no longer re-prove the engine from scratch each time. Wisp keeps one verified record per running oMLX for at most 60 seconds, re-checks the engine process natively before every request and every write, and still checks the listener once per connection and the connection's owner (lsof) before every write. A restart, an exec into another binary, the authorization file appearing or any check failure falls back to the full check. `WISP_ATTESTATION_LEGACY=1` restores the previous per-request check.

### Removed

- Directions, currency conversion, app uninstall, sending files by AirDrop (the AirDrop on/off setting stays), unsubscribe, raw HTTP requests, display brightness and a few other low-use or duplicate tools. Currency, definition, recipe and sunset questions now go to web search or Wikipedia. Placeholder tools that did nothing are gone.

### Fixed

- Smart Search no longer answers "not found" when the model's answer is correct but has no [n] citation. A sentence grounded in a retrieved passage keeps its place and gets that passage cited, if its figures match exactly, its negation matches, and at least 80% of its content words appear in the passage.
- Behavior learning now sees Messages. Every message was dropped on ingest (the Messages feed sends RFC 3339 timestamps and the runtime parsed them as numbers), so no contact rhythm could ever form. Replies now count toward the same person as their texts, named by Contacts, and automated senders (short codes, toll-free numbers, alphanumeric sender ids, business chats) are skipped.
- Web lookups (news, weather, stock and crypto prices, "search the web for…") now run on the model configured for the `agent` role instead of a model id written into the router. The written id (`Ling-3.0-tiny-oQ6e`) was no longer installed, so oMLX answered its load with 404, reloaded the resident model, and the next turns failed with "The local AI engine returned an error (409)".
- An approved Calendar, Reminders or send action no longer fails with "Wisp app is not connected" when it lands while the app is reconnecting its event stream, which happens about every 25 seconds. Wisp now waits up to 8 seconds for an app that disconnected within the last minute; an app that never connected, or left long ago, still fails at once with nothing sent.
- A failed or denied write, and a drafted message, no longer show "The assistant didn't respond." in place of Wisp's actual reply. Every early end of a turn now tells the app the turn is done.
- "What did you remember about me?" goes to memory recall like "What do you know about me?".
- Typo-tolerant tool search returns the same matches on every launch; it used to vary with Python's hash seed.

## [0.3.1] - 2026-10-12

### Added

- Added model-led routing behind `WISP_MODEL_LED_ROUTING=1` (off by default): the local model reads each request and picks tools from a capability catalog instead of the rules router. Remote-only mode, behavior questions and literal inputs keep the rules router, and sends still always ask first. It stays off by default because Ling V2 does not yet follow its tool-loading step reliably.

### Changed

- Pull-request CI runs a preview build and Simulation QA as parallel jobs, and Simulation QA runs test modules concurrently (`--jobs N`). Tags and releases still run the full strict build with every gate.
- Remote inference is now a generic, user-configured mode with no built-in server. It starts only when Wisp is launched with `WISP_REMOTE_ONLY=1` and `~/.moe/config.yaml` has a complete `inference.remote_only` profile (an HTTPS `base_url`, a `model_id`, a `keychain:<name>` credential and every request limit). With the flag set and no valid profile, the backend refuses to start. See docs/REMOTE_INFERENCE.md.
- Remote inference is now read/write. Every built-in tool that doesn't need Wisp's on-Mac models (so not `create_tool`, `use_skill`, `wisp_skills`, `wisp_mcp` or skill and MCP tools) runs under the normal safety policy: sends always confirm and can't be granted, writes ask once, always or never. This is what `WISP_REMOTE_FULL=1` used to turn on. The separate read-only remote mode is gone, along with its 403s on the behavior and approval endpoints. For a read-only Mac, use View Only, which still denies writes in remote mode. `WISP_REMOTE_FULL` is now ignored (it logs one deprecation line).
- Remote inference refuses a `base_url` with spaces, control characters or non-ASCII characters (a leading space or trailing newline used to pass).
- Behavior learning is now on by default. Turn it off with "Learn my routines" in Settings (Privacy & Access), which takes effect at once and keeps what was already learned, or launch with `WISP_BEHAVIOR=0`. `WISP_BEHAVIOR=1` or `0` overrides the saved setting. It still learns only from your calendar, reminders, message timing (never message text) and how you use Wisp, nudges at most 4 a day, acts automatically only on Wisp's own schedule, and keeps everything on this Mac. New endpoints: `GET/PUT /behavior/settings`.
- Behavior learning privacy controls: Settings has **Forget what Wisp learned** (`DELETE /behavior/data`, JSON body `{"confirm": true}`), which deletes the learned data and removes the untouched reminders and holds Wisp added on its own (anything you edited or completed is kept). The 180-day and 30-day age-out now runs while learning is off too. Automatic actions ask the safety policy like the `add_reminder` tool, so View Only turns them into offers. If `config.yaml` is unreadable, learning is treated as off and saving the setting returns a clear error. A pass already running when you switch learning off publishes and performs nothing. Under remote inference, nudge wording and event labeling are off by default (`WISP_BEHAVIOR_PHRASER=1` opts in), and the Settings text says what goes to your server.

### Removed

- Removed the GX10 server support: `brain_server/`, `deploy/gx10/`, the GX10 runbooks, the remote behavior brain (`WISP_BRAIN_MODE`, `WISP_BRAIN_URL`, `WISP_BRAIN_TOKEN`) and the pinned GX10 remote-inference profile.
- Removed the Intel Mac builds: the `WISP_REMOTE_ONLY_DEMO`, `WISP_FOUNDER_DEMO` and `WISP_REMOTE_FULL` app compile flags and their code paths.
- Removed every demo mode:
  - the founder demo and its fictional fixtures (`WISP_FOUNDER_DEMO`);
  - the Aria demo world (`WISP_ARIA_DEMO`, the `/demo/aria/*` endpoints, the menu toggle and the separate demo state directory);
  - the behavior engine's synthetic persona mode (`WISP_BEHAVIOR_SYNTHETIC_ONLY`, `POST /behavior/reset`);
  - the Catch demo (`WISP_ATTENTION_DEMO`). The proactive attention runner is unchanged;
  - the `demo/` tooling.

An older install may still have an unused `~/.moe/aria-demo/` folder from the Aria demo. Wisp no longer reads it, so you can delete it.

### Fixed

- `http_request` no longer follows redirects. A 307 or 308 used to re-send the request body and custom headers to a host you never approved; a redirect is now reported as not followed.
- "What do you know about me?" answers from what you asked Wisp to remember again, now that behavior learning is on by default. "What have you learned about me?" and questions about your habits still list learned routines.

## [0.3.0] - 2026-10-11

### Added

- Added proactive attention: Wisp watches incoming messages for plans or commitments that aren't on your calendar ("Can we do Thursday at 3?") and, in shadow mode (the default), records what it would do without acting. Opting in to live mode creates a verified Apple Reminder for each one, at most 3 a day, never 22:00–08:00, only when Wisp's safety policy allows the write (view-only blocks it), with Undo that only removes what Wisp created. Message quotes are cleared from its ledger after 7 days.
- Added `./scripts/wisp-build preview`: an installable, ad-hoc-signed preview build in minutes (no Simulation QA), never eligible for release.

### Changed

- Model-backed tests use Ling 3.0 tiny V2 (oQ4e).
- `correct_pattern` now asks before changing a learned pattern, and the behavior write endpoints are refused in read-only remote builds.
- Wisp is now a regular Mac app instead of a menu-bar-only one. It has a Dock icon, an app menu (Settings, Hide, Close Window, Quit) and a Window menu, opening it shows the Chat window, and clicking the Dock icon brings the window back. Closing the window leaves Wisp running in the notch and menu bar; ⌘Q quits it through the same shutdown as the menu-bar Quit. The notch and the menu-bar icon are unchanged.

### Fixed

- The Chat thread no longer pulls you back to the bottom while you scroll up to read.
- Fixed `wisp-build all --allow-dirty` failing its own final check on a clean checkout.

## [0.2.5] - 2026-10-11

Merges the Wisp hackathon work (adjad/Wisp_Hackathon) into Wisp.

### Added

- Added the behavior engine: Wisp learns routines from Calendar, Reminders, message metadata and what you ask it, predicts the day, and sends a few nudges (at most 4 a day, quiet 22:00–07:00). Patterns are deterministic statistics with evidence counts; the model only phrases them. Reversible actions (a reminder, a "Tentative:" hold on Wisp's own schedule) are done automatically with Undo; messages and meeting changes are only drafted. Off unless `WISP_BEHAVIOR=1`. Ask "What do you know about me?", "What will tomorrow look like?" or "Which meetings do I usually cancel?". See docs/BEHAVIOR_ENGINE.md.
- Added nudge cards and actionable notifications (Undo, Do it, Useful, Not now, Wrong) in the app.
- Added Catch: Wisp notices a plan stated in a text ("Dinner at 7, I booked a table") that isn't on your schedule and adds it to Wisp's schedule with an alert. Off unless `WISP_ATTENTION_DEMO=1`.
- Added Workflow20 grounded multi-source workflows with guarded action execution, a personal knowledge index, calendar planning, file organization and grocery-list tools.
- Added a remote-inference mode for Intel Macs: every model role runs on a paired GX10 (or other OpenAI-compatible server) over Tailscale with a Keychain token, while data and tools stay on the Mac. A read-only founder profile and a full profile (all tools with normal approvals, the scheduler and 32k context) are available.
- Added the Aria demo mode for remote builds: a fully synthetic, interactive demo world (mail, messages, notes, reminders, contacts, calendar) where sends land in synthetic threads with scripted replies and nothing leaves the Mac, with proactive items in the Daily Summary and the top bar.
- Added an optional brain server and GX10 deploy scripts (gateway supervision, 32k context) under brain_server/ and deploy/gx10/.
- Added the iPhone app foundation (ios/): WispCore, the Workflow20 contract and a local-inference interface.
- Added demo tooling: `demo/seed_mac_persona.py` and `demo/wisp_demo.py` (persona seeding, behavior and Catch demos, rehearsal reset).

- Added the Wisp Chat window: a real, resizable window for long conversations, opened from the menu-bar menu ("Open Wisp Chat") or from the notch panel's new "Open in Wisp Chat" button, which continues the current conversation there. A sidebar lists every saved chat by day with search and delete; the thread shows which tools Wisp used above each answer; confirmation cards and message drafts work as they do in the notch, in the thread; and a details panel (⌘I) shows the route, model, speed, and tools for any answer. It follows the Mac's light or dark appearance. Today, Research, Memory, Search, and Settings open from its sidebar. See docs/CHAT_WINDOW.md.
- Added `GET /chats`, a conversation list with a title (the first prompt) and a one-line preview for each saved chat.

### Fixed

- Fixed the sandboxed Today and Diagnostic Report contract gates under Xcode 27, where SwiftUI's `@State` is a compiler macro whose plugin server cannot start inside Simulation QA's sandbox. Those scripts now ask `swiftc` not to add a second, nested sandbox when it supports the flag; the outer sandbox is unchanged.

## [0.2.0] - 2026-10-06

### Added

- Added local inference apps (Ollama, LM Studio, llama.cpp, MTPLX) as tool-capable engines. Wisp measures the context window the app really honors, runs a synthetic tool-calling probe on the server, and only then lets the app drive Agent and Coding work. Settings has a "Test Tool Calling" check with per-check results and the exact fix when one fails.
- Added local problem reports: a metadata-only request journal with stage timing, and a reviewable report that is never uploaded. Detailed content is optional, shown in a preview, and redacted on a best-effort basis. Today refreshes and background kinds keep their own small retention so they cannot evict chat traces.
- Added a persistent debugging timeline and an opt-in capture of Today planner inputs for deterministic replay (see docs/DEBUGGING.md).
- Added undated Apple reminders to reminder search: incomplete reminders with no due date are now synced and listed.
- Added an inbox digest sorted by what needs attention (people first, then automated senders), replacing the dense statistics paragraph and per-sender lists.
- Added a local coworker mailbox for correlated agent messages with acknowledgments and duplicate-delivery protection.
- Added a release performance gate and benchmark harness: publication requires attributable, live-measured performance evidence for the exact release commit. The harness's first live runs happened during this release and could not yet produce a passing receipt on the build host, so 1.2.0 is published under an explicit, committed waiver limited to this version (see docs/RELEASE_PERFORMANCE_BENCHMARK.md#waiver); a passing benchmark is a 1.3 requirement.

### Changed

- The notch is easier to open: the top edge of the screen now counts as part of the hover zone, and most of the notch (all but the bottom strip) opens the menu.
- Mail history is read from Mail's local index when Full Disk Access is granted, with no Apple Events, and otherwise synced incrementally instead of re-reading two years on every pass. Gmail accounts, whose Inbox is a label, are included.
- A native source (such as Reminders) that has been syncing for 90 seconds without progress is reported as unavailable with the exact next step instead of reading "syncing" forever.
- Engine errors, memory limits and over-long conversations explain themselves in plain language, with the technical reason kept in the debug detail.
- Tool routing is measured: a frozen 361-prompt corpus and a CI ratchet now guard routing quality, which rose from 64% to 82% on that corpus (see docs/ROUTING_DIAGNOSIS.md). Fallback menus no longer offer send, reply, call or bulk-delete tools unless the request asks for them.
- Wisp starts inference with one bounded engine status read and reuses a recent proof of which model is resident across the steps of a turn, instead of asking the engine before every step. A simple greeting now makes 2 engine calls instead of 3. The proof is dropped on any model change Wisp makes or observes, and is re-checked at least every 10 seconds, so a change made outside Wisp is noticed within that bound.

### Performance

- Verifying the local oMLX engine before each request does the same checks with the same verdicts in less time: the engine's Python tree is read with `scandir` (2.2x faster on the real 47,320-entry tree), the independent process inspections run concurrently (about 1.4x on a request-shaped total), and executable paths use the native `proc_pidpath` call instead of spawning `lsof`.
- Routing reuses each exact query's lexical ranking within a request (about 1.9x faster routing for ambiguous requests).

### Security

- Fixed an unreliable "Local inference peer attribution unavailable" refusal on about half of live turns when other clients were connected to the engine.
- Pasted API keys and similar secrets in Agent chats are redacted before they reach the turn store, the audit log or a model. Keys already stored in older history are not rewritten and should be rotated.
- Approvals are strict: only an exact `true`/`false` is accepted, and an answer can only resolve the request that raised the confirmation.
- Launch no longer terminates unrelated programs on Wisp's ports, and the app verifies the backend's identity before trusting it.
- The protected-path floor now checks every path argument (including lists, symlinks and `/private` aliases), and an MCP server can no longer mark its own tool read-only.
- "Always allow" can no longer be granted for tools that must confirm every time (calendar writes, outbound actions, network and tool-authoring tools).
- A sync can no longer erase reminders you created in Wisp.

### Fixed

- Fixed authored emails and texts ("email Sam asking to move our meeting") being compiled into data deliveries that mailed the user's calendar.
- Fixed everyday prompts that dead-ended or routed to the wrong tools, including "summarize my emails and text it to Mom".
- Fixed a closed connection leaving a turn "running" forever.
- Fixed tool menus for requests that match no routing rule offering send, reply, call and bulk-delete tools when the request plainly asks for neither.
- Fixed a bare "sure", "ok" or "go ahead" to an assistant's offer reaching the model with no tools. An assent to an offer cannot create a reminder.
- Fixed "any new emails?" and "any new texts?" being answered with a web search, and email write requests offering the file "move to Trash" tool.
- Fixed "draft a reply … but don't send it" losing the draft tool along with the send tools.
- Fixed common typos and shorthand ("tmrw", "calender", "txt", "remeber") sending a request to a broad, generic tool list, without ever rewriting recipients, message text or file names.
- Fixed tomorrow planning ("any plans for tommorow?", "what is on my do list tmrow") using another day or stale memory, and study-slot requests returning a whole free window as a short slot.
- Fixed rejected study-reminder invitations producing a false creation promise after "sure" or a dated reply.
- Fixed a freshly created Note not being found while an unrelated old record surfaced, and "what's my Amazon order" searching only conversation history.
- Fixed long-overdue Wisp-only records reading as current reminders (they are left out of search and counted, never deleted).

## [0.1.5] - 2026-09-30

### Added

- Added a confirmation step for shell commands that delete irreversibly — recursive deletes, wildcard deletes, and their equivalents. These now always show the exact command for approval, in every access mode including full access, and cannot be pre-approved with "always allow". Deleting a single named file still runs without prompting.
- Added a move action for files and folders, so reorganizing, sorting, and filing things away no longer depends on shell commands. It creates the destination folder as needed and refuses to overwrite anything that already exists.
- Added router-direct dispatch: when a routing rule resolves a tool call in full — name and arguments both — Wisp now runs it before the first model call instead of spending a model step re-deriving it. Covers zero-argument device reads, calendar-only lookups with the window resolved in Python, and unqualified inbox or message summaries.
- Added an error-translation layer so a failed request explains itself in plain language — a stopped engine, a memory limit, or an over-long conversation each get their own sentence and, where one exists, a next step. The raw error is kept in the debug export rather than shown as the headline.
- Added a regression suite for Daily Summary fallbacks, live mail-cache row shapes, prompt leakage, prompt-only glyph cleanup, and completion-token headroom.
- Added a shape-stable `header_rows()` API for cached email metadata so cross-module consumers can read named fields without depending on internal tuple layouts.
- Added a training-data harvester that runs prompts through the real router, agent loop, and tool pipeline, then records successful trajectories as SFT examples and routing/tool-selection failures as DPO pairs.
- Added guarded self-target execution, dry-run and no-execution modes, resumable flow selection, and compound multi-tool workflow coverage to the training harvester.
- Added a dataset merge utility that combines harvested and real-usage examples while removing samples contaminated by harness-generated denial messages.

### Changed

- Daily Summary now separates mail from people from newsletters, notifications, and other automated senders. Automated mail is reduced to an optional sender/count roll-up instead of competing with actionable messages.
- Daily Summary email context now preserves unread state, includes account labels when multiple accounts are present, uses the last 24 hours when available, and falls back to the newest messages on quiet days.
- Daily Summary and message prompts now use explicit output skeletons and placeholder-only examples, with tighter rules for dates, attribution, section structure, reply status, and grounding.
- The deterministic Daily Summary fallback now prioritizes human mail, groups automated arrivals separately, and renders calendar, email, and message data in a user-facing format.
- The macOS client now allows up to 240 seconds for Daily Summary generation, covering cold model loads without presenting them as immediate failures.
- Real-usage LoRA dataset generation now re-runs the production router for each triggering user turn and stores the same routed tool subset the model would receive at inference time.
- Email and message routing now recognizes self-directed requests such as “email me,” “text me,” “email it to me,” and “text it to me,” enabling chained read-then-send workflows.

### Fixed

- Fixed reorganizing files being able to delete them. Wisp had no way to move a file, so a request to tidy a folder fell back to raw shell and could remove the originals instead of relocating them. Moving is now a first-class action, and it cannot delete anything.
- Fixed message summaries hiding entire conversations. The recent view took the newest messages across all chats at once, so a single busy group chat could fill the whole window and quieter threads never reached the summary. Recent messages are now sampled per conversation, and any conversation still left out is named rather than silently dropped.
- Fixed inbox summaries implying they covered everything. A summary of the most recent mail now says how much of the inbox it actually looked at, and how to ask for more.
- Fixed confirmation prompts hanging indefinitely when left unanswered. An unanswered prompt now expires after five minutes and is declined, since a stalled prompt also held back the daily brief and profile updates for as long as it sat there. An expired prompt is cleared from the window rather than left waiting on an answer that no longer has anywhere to go.
- Fixed Daily Summary failures caused by positional unpacking after cached email rows gained a read/unread field.
- Fixed inflated inbox counts and repeated brief items by collapsing byte-identical cached mail-header lines before downstream processing.
- Fixed the Daily Summary endpoint so an unavailable local model returns a displayable deterministic brief instead of an unhandled server error.
- Fixed on-demand and scheduled summaries so source, cache, or model failures degrade to a non-empty fallback rather than dropping the entire brief.
- Fixed empty or truncated model responses leaking raw prompt scaffolding and source rows into the user-facing summary.
- Fixed realistic examples in summary prompts being copied as fabricated events or message details by replacing them with non-content placeholders.
- Fixed unread bullets and message-routing markers leaking from prompt context into rendered summaries.
- Fixed message-summary failures from preventing calendar and email sections from being generated.

### Validation

- Destructive-delete confirmation: 29 checks passing, including the exact command from the incident, every recursive and wildcard variant, and proof that targeted single-file deletes still run unprompted. Verified live twice — a test folder survived both attempts to delete it.
- Move action and summary coverage: 17 checks passing. Measured against the real export that prompted this: 21 of the 30 messages shown came from one group chat, and the new per-conversation sampling surfaces every recent thread instead.
- Router-direct dispatch: 45 routing checks and 24 execution checks passing. Verified live — a battery question answers in 1.42s and a calendar lookup in 2.88s, while "summarize my inbox" completes with no agent-loop model call at all. Requests that must not pre-dispatch (compound device requests, qualified summaries, calendar writes and past-tense questions) all still take the ordinary route.
- Error translation: 14 checks passing, covering a stopped engine, both 400 shapes, and the unmapped fallback.
- Confirmation timeout: 8 checks passing, including that a real answer beating the clock still wins and a late answer is a harmless no-op.
- Daily Summary regression suite: 32 checks passing.
- Router scoping regression suite: 133 checks passing.
- Updated Python modules and dataset scripts compile successfully; the harvester dry-run discovers 84 flows, 117 runs, and 122 model-facing turns.
