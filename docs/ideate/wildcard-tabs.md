# Wildcard tabs, personas and the "Customize Harbor" step

Research notes for the Ideate tab and the onboarding step where the user says who they are
and the strip gets one or two wildcard tabs. Read-only research against the repo at the time
of writing. All examples are invented.

## What exists today (checked in code)

- **Strip** (`NotchTab.swift`): `strip = [ask, brief, day, meet, study]`, ⌘1 to ⌘5. Research is a
  hidden mode of Ask, not a tab. Any tab added to `NotchTab` gets the same page slide
  (`OverlayView` loops over `NotchTab.strip`). A 640 pt panel fits about six short titles.
- **Ask**: conversation plus about 100 tools (`service/tools/`): calendar and reminders
  (`add_calendar_event`, `find_free_time`, `add_reminder`), mail (`view_emails`, `summarize_emails`,
  `triage_inbox`, `draft_email`, `send_email` with approval), messages, notes (`create_note`,
  `search_notes`), files (`read_file`, `write_file`, `find_files`, `organize_files`), documents
  (`write_document`, `spreadsheet_ops`), web (`web_search`, `web_fetch`, `wikipedia_summary`),
  places and travel (`find_place`, `route_to_arrive`, `travel_time`), timers, weather, stocks,
  system control, `run_shell` and `run_applescript` (always ask), `screen_capture`, a browser
  assistant, memory (`save_knowledge`), `create_tool` and skills/MCP (`wisp_skills`, `wisp_mcp`),
  and two Study tools (`study_slides`, `study_make`). Plus `/research` for cited multi-round web reports.
- **Brief**: read-only. One sentence plus source lines: who is waiting, deadlines and bills found
  in mail, today's to-dos, Study due lines, "Wisp did this" with Undo.
- **Day**: hour-by-hour timeline of calendar, reminders, tasks, open time, hold blocks, Study
  blocks to Calendar, suggestions.
- **Meet** (`MeetingNotes.swift`, `service/meetings*.py`): listens through the mic (plus system
  sound if Screen Recording is allowed), local transcript, then summary, decisions, action items
  (each can become a reminder on one press), follow-up draft. No audio kept.
- **Study** (`Study.swift`, `service/study/`, `docs/STUDY_TAB.md`): reads school pages open in the
  browser (Canvas first) through Accessibility, stores courses, grades, assignments, slides and
  Drive docs in `study.db`, feeds Brief and Day, makes guides, quizzes, lessons, flash cards.
  Privacy note: **Study material is written by a cloud "Study connector"** (user's own
  OpenRouter or Gemini key in Keychain), unlike the rest of the app which is local by default.
  This matters for personas that need Study but want strictly local.
- **Ideate (new)**: a coding harness for sketching and building small projects. Not built yet.

Consequence for the design: only three wildcard tabs exist after this work (Meet, Study,
Ideate). Everything else is a candidate, and Ask plus Brief plus Day already cover most
"personal organizer" ground.

## 1. Personas for "Who are you?"

Eight top-level choices maximum. Subtypes appear as a second line only where they change
something real (the Ideate variant, or Study depth). Copy is plain, first person.

| # | Choice | Subtypes | Default tabs |
|---|---|---|---|
| 1 | Student | High school, College, Grad or researcher | Study (+ Ideate for CS students) |
| 2 | I build things | UI developer, Debugger, CS student, Tinkerer, Maker, Writer who codes, Just ideas | Ideate (+ Study for CS student) |
| 3 | Meetings fill my day | none | Meet |
| 4 | I manage a team | none | Meet |
| 5 | I write or create | none | Ideate (writer variant) or Meet, see table |
| 6 | Home and life | none | none (fallback) |
| 7 | Just the basics | none | none |

Seven top-level choices. A separate "Researcher" row is folded into Student > Grad or
researcher. Row 6 is real but gets no wildcard tab; see section 3.

### 1a. Student subtypes (change: Study tone and whether Ideate joins)

- **High school**: Canvas or Google Classroom pages, short assignments, flash cards and quizzes
  most used. Study only. Plain-language lesson style. Note: parent-managed accounts mean the
  student may not have a Study key; Study then shows its connect card (see fallback below).
- **College**: Study as above, plus lecture slides and Drive docs; Day study blocks matter most.
  Study only, unless the next question says CS (see below).
- **Grad or researcher**: long PDFs and papers, not weekly quizzes. Study with long-context
  sources; Ask's `/research` for literature. Second tab: Meet (advisor and lab meetings).
- **CS major** is not a Student subtype; it is chosen under "I build things > CS student" and gets
  Study + Ideate. Keep one path to avoid two questions.

### 1b. Ideator ("I build things") subtypes and Ideate variants

A variant is a preset over four dials: starter templates, default tools, panel emphasis and
step size. All variants use the same Ideate tab; the variant just sets defaults the user can
change.

| Subtype | Starter templates | Default tools | Panel emphasis | Step style |
|---|---|---|---|---|
| UI developer | Single-file HTML page, SwiftUI view, React-style component scaffold, form, card list | `write_file`, `read_file`, `run_shell` (sandboxed folder), `screen_capture` | Live preview on top, code below | Change one visual thing per turn, show before and after |
| Debugger | Paste box: error, stack trace or log; "minimal repro" file | `read_file`, `find_files`, `run_shell` (read-only first), `web_search` | Repro and hypothesis list; diff of the proposed fix | Reproduce first, then explain cause, then smallest fix; no edits until a repro fails |
| CS student | Algorithm exercise, data-structure stub, small CLI, test file | `write_file`, `run_shell`, test runner | Tests always visible, pass/fail strip | Explain as you go; small steps; hints before answers; never hands over a full solution unless asked |
| General ideator | "Idea to plan to scaffold": one-paragraph brief, task list, folder skeleton | `write_file`, `create_note`, `add_reminder` | Plan list first, code second | Idea, plan, scaffold, then first runnable stub |
| Tinkerer (scripts and data) | CSV cleaner, rename-files script, API fetch, spreadsheet formula helper | `spreadsheet_ops`, `read_file`, `run_shell`, `organize_files` | Input sample and output sample side by side | Run on a copy, show the preview, only then touch the real file |
| Maker (hardware) | Arduino sketch, pin table, serial-monitor parser, BOM list | `write_file`, `web_fetch` (datasheets), `calculate` | Pin and parts table; wiring notes | Check voltage and pin limits before code; flag anything that can damage a part |
| Writer who codes | Static site or blog scaffold, Markdown build script, text-processing script | `write_document`, `write_file`, `run_shell` | Document preview | Prose-first prompts, code stays in the background |

Judgment: **Tinkerer, Maker and Writer-who-codes are real but small**. Ship them as one
catch-all "Something else I build" with a short follow-up ("scripts and data", "hardware",
"writing and sites") rather than three more rows. Keep UI developer, Debugger, CS student and
General ideator as the first four rows since each changes the panel. That keeps the second
screen to 5 choices.

### 1c. Other personas

- **Meetings-heavy professional** (sales, consultant, PM): Meet is the main benefit. Second tab:
  none by default; Study and Ideate are the wrong fit. Brief and Day already serve the rest.
- **Manager**: same need as above plus follow-ups to many people. Meet again; the one-press
  reminder and follow-up draft are exactly the manager loop. A future "Follow-ups" view would be
  a fold into Brief, not a tab (see section 2).
- **Creator or writer** (newsletters, scripts, posts): needs drafting and notes. Ask plus
  `write_document` and Apple Notes cover most. Best existing tab: Ideate (writer variant) only if
  they build sites or scripts; otherwise Meet for interviews. Honest answer: no wildcard fits;
  see fallback.
- **Home and life manager** (family logistics, bills, errands): Brief and Day are the product
  for this person already ("Found in your mail", bills, reminders). No wildcard needed.
- **Job seeker, retired, hobbyist**: collapse into "Just the basics".

## 2. Candidate wildcard tabs

Verdict key: BUILD LATER, FOLD INTO <tab>, DROP. Privacy cost notes new data sources and
permissions. Overlap is judged against Ask, Brief, Day, Study, Meet and Ideate.

| # | Candidate | What it would do | Wanted by | Overlap | Verdict | Privacy or permission cost |
|---|---|---|---|---|---|---|
| 1 | Write | A drafting pad: outline, rewrite, tone, word count, saved drafts | Creator, student, manager | Ask `write_document`, `draft_email`, `/research`; Ideate writer variant; Study makes guides. A pad adds only persistent drafts | FOLD INTO Ask (a "Draft" action and saved drafts in the chat window) | None new; local files |
| 2 | Read / Inbox zero | Triage inbox into reply, read, ignore | Manager, professional | Brief already shows who is waiting with drafts; Ask has `triage_inbox`, `email_triage`, `summarize_emails`, `scan_subscriptions` | FOLD INTO Brief | Uses Mail permission already granted |
| 3 | Focus | Timer, blocked apps, one task at a time | Student, builder | Ask `manage_timers`, `set_timer`, `toggle_setting` (Do Not Disturb); Day holds free hours and study blocks; `quit_app` | FOLD INTO Day (a "start focus" action on a block) | Focus mode and app control already exist; no new data |
| 4 | Habits | Streaks and daily check-ins | Anyone | Reminders recur; Brief's check-in (`checkin.py`); behavior engine (`service/behavior`) already mines patterns. Streak UI is new but thin | DROP (gamified streaks conflict with "calm over clever"; recurring reminders cover the need) | Would need its own store; no gain |
| 5 | Money | Bills, subscriptions, spending | Home and life | Brief shows bills and renewals from mail; Ask `scan_subscriptions`, `calculate`, `spreadsheet_ops`, `get_stock_price` | FOLD INTO Brief (bills) and Ask (subscriptions). Do not read bank data | A bank or statement import is a new, sensitive source; refuse by default |
| 6 | Travel | Trip itinerary, confirmations, time to leave | Professional, home | Ask `travel_time`, `route_to_arrive`, `find_place`, `get_weather`; Day shows events with leave times; mail finds bookings | FOLD INTO Day (trip entries) | None new |
| 7 | Health | Medication, appointments, sleep | Home | Appointments are Calendar; medication is a recurring reminder. HealthKit is a new, sensitive source and a big permission | DROP for now (do not take health data into a notch tab) | HealthKit permission and a very sensitive store |
| 8 | Research | Library of topics, sources, notes | Grad student, creator, analyst | Ask `/research` (hidden mode) and the window's Research library already exist; Study Drive docs and long-context sources cover readings | FOLD INTO Ask (it already is a mode; optionally promote its entry) and Study (grad) | None new |
| 9 | Clips / Scrapbook | Collect snippets, links, screenshots | Creator, student, ideator | Ask `clipboard_read`, `screen_capture`, `save_knowledge`, `create_note`; Smart Search (⌘⇧F) | BUILD LATER, small: a "Save to Harbor" collection. Genuinely missing: a capture inbox. Narrow first: clipboard history capped and local | Clipboard history needs explicit opt-in; local only |
| 10 | Home | Smart home, chores, shopping | Home and life | `toggle_setting`, `run_applescript`, Reminders lists cover chores and shopping; HomeKit would be a new permission | DROP (no HomeKit integration planned; reminders cover lists) | HomeKit permission |
| 11 | Code review | Review a diff or PR | Ideator, professional dev | Ask can read files and `run_shell git diff`; Ideate Debugger variant covers reading code | FOLD INTO Ideate (a "review this" template) | Local repo only; shell asks |
| 12 | Learn a language | Vocabulary, spaced review, speaking practice | Student, hobbyist | Study makes flash cards and quizzes from school pages; Ask `text_to_speech` and voice ask; `wikipedia_summary` | FOLD INTO Study (a "make cards from this text" source that is not school). Do not build a separate tab | Study's cloud connector rule applies; say so |
| 13 | Shop | Price compare, wishlists, receipts | Home | Ask web search and browser assistant; mail receipts found by Brief; purchases need approval | DROP (commerce is outside the product; risk of wrong spend) | Payments are prohibited territory; none |
| 14 | Call notes (one-to-one, phone) | Notes for calls and voice memos | Manager, professional | Meet already records any audio the Mac hears and makes action items | FOLD INTO Meet (a "voice memo" mode) | Mic permission already used by Meet |
| 15 | People / follow-ups | Who I owe a reply or a promise | Manager | Brief lists who waits; behavior engine `commitments.py`; Meet action items to reminders; contacts tools | FOLD INTO Brief | None new |
| 16 | Daily review (journal) | Evening recap, notes | Anyone | Brief and `checkin.py`; `create_note`; Day past-purpose | FOLD INTO Brief (evening line) | None new |

Honest count: **1 BUILD LATER (Clips), 11 FOLD, 4 DROP (Habits, Health, Home, Shop)**. Ideate, Study and Meet already take the three
open strip slots; a 4th or 5th wildcard would push the strip past a notch's comfortable
width, so the bar for adding one is that it cannot live in Ask, Brief or Day.

Rule of thumb used: a candidate is a tab only if (a) it has its own state that Ask, Brief and
Day do not hold, (b) it needs a screen that is not a conversation or a timeline, and (c) it
needs no new sensitive permission. Only Clips passes all three, and only narrowly.

## 3. What ships now: persona to tabs

Tabs available: Study, Meet, Ideate. The strip always shows Ask, Brief, Day; the wildcard tabs
are added after Day. Maximum two.

| Persona | Tabs shipped | Why | If the best tab is not built |
|---|---|---|---|
| Student, high school | Study | Canvas and Classroom due dates, quizzes and flash cards | n/a. If no Study key, the Study tab opens on its connect card; user can skip and Study stays hidden |
| Student, college | Study | Slides, grades, Day study blocks | n/a |
| Student, grad or researcher | Study + Meet | Long-context sources and papers; advisor and lab meetings | If Study is unwanted: Meet only; research stays in Ask `/research` |
| I build things, UI developer | Ideate (UI variant) | Preview and component scaffolds | none needed |
| I build things, Debugger | Ideate (debugger variant) | Paste error, repro first | none needed |
| I build things, CS student | Ideate (CS variant) + Study | Tests visible, explain as you go; coursework in Study | If Study declined: Ideate only |
| I build things, General ideator | Ideate (general variant) | Idea, plan, scaffold | none needed |
| I build things, something else (scripts, hardware, writing) | Ideate (tinkerer variant) | Closest existing tab | Writer: Ideate writer variant; Maker: Ideate with pin table template |
| Meetings fill my day | Meet | Notes, decisions, action items to reminders | none needed |
| I manage a team | Meet | Action items, follow-up drafts; Brief covers who waits | none needed; Brief's "who is waiting" is the rest of the loop |
| I write or create | Ideate (writer variant) | No dedicated Write tab; a docs and site scaffold is the nearest | If they decline coding: Meet (interviews) or none; Ask drafts |
| Home and life | none (strip stays Ask, Brief, Day) | Brief and Day are the product for them | Offer Meet only if they say they attend meetings |
| Just the basics | none | Strip stays Ask, Brief, Day | Everything is reachable later in Settings > Tabs |

Rules for the onboarding step:
- The default is a suggestion; one tap to accept, and a visible "Change" that lists the three
  tabs with checkboxes (two max, so unticking is needed to add a third).
- Choosing Study shows its cloud-connector disclosure on that screen, because it is the only
  wildcard whose generation leaves the Mac (user's own key). If the user says no, do not add
  Study; give them Meet or Ideate or none as the fallback above.
- Choosing Meet does not ask for the microphone in onboarding. Per `MeetingNotes.swift` the
  permission belongs to Ask's mic button; Meet starts only once it is allowed.
- Ideate's shell tool, like Ask's `run_shell`, runs in a project folder and asks before
  anything that could send data off the Mac.
- No personal data is asked. The persona is stored as one local choice and drives only the
  tab strip and Ideate defaults.
