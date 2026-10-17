# Wisp's notch and window

Wisp lives in the notch. Asking, the Brief, your Day, research, quick settings, first-run
setup and problem reports all happen in the black panel under the notch, and
Wisp starts there: launching it opens no window. One Wisp window holds the work
that needs room (long chats, Memory and saved research), and it opens only when
asked for. Both talk to the same service and the same saved chats.

## The notch

- **At rest** it is a black bar around the notch (a small capsule on a monitor).
  Hover or ⌥Space opens it. ⌥Space and the menu-bar icon always open Ask; hover
  opens the tab you last used if the panel closed in the last ten minutes. A dot
  shows Wisp is working, a count shows what is due today or overdue, and an
  orange "!" means setup was set aside. Clicking a Wisp notification (other than
  a nudge's buttons) opens Day.
- **Tabs** (⌘1 to ⌘3): **Ask**, the conversation; **Brief**, one sentence about
  the day and up to four source lines, read only; **Day**, the rest of today on
  one hour-by-hour timeline (calendar, your tasks, suggestions, learned busy
  times, plans caught in texts, reminders due) under a two-line blurb.
  Research is a mode of Ask (`NotchTab.research`, not in the strip): `/research`,
  a job opened from the window or New research turn the prompt into an editable
  research plan, then show the run and its report. Choosing a tab leaves it.
- **Day's actions** never write to Calendar or Reminders. Keep (or a later time
  from ⋯) pins a suggestion as a Wisp task; Add does the same for a plan caught
  in texts, and Ignore hides it on this Mac; Plan time adds a reminder as a task
  the planner fits in; Not today is the planner's one-day ignore of a learned
  window. The logic (what lands where, the badge, the Brief's sentence, the
  blurb, plain-language warnings, junk titles) is pure code in `NotchDay.swift`.
- **Ask** has a chat switcher (recent saved chats from `GET /chats`), New chat
  (⌘N), slash commands (`/today`, `/memory`, `/research`, `/settings`, `/report`,
  `/window`, `/new`), per-reply Details (route, model, timing, tool calls; open by
  default in Debug Mode) and the same confirmation card and message draft as the
  window.
- **Keep open** (pin, ⌘P). Any edit in progress also holds the panel open: typed
  input, a message draft, a Day task, an open setup card or an open report.
- **Quick settings** (the gear or `/settings`): Full access, Learn my routines,
  the brief times, Show Details (Debug Mode) and Free up memory. Everything that
  needs a form, a key, OAuth or widens what Wisp reads stays in Settings (⌘,).
- **First-run setup**: on a Mac that can't answer yet, the first launch opens Ask
  on a setup card (engine, model, Use & test). Later shrinks it to a line until
  Wisp is ready. Smart Search, other local apps and downloads continue in the
  setup window.
- **Report a problem** unfolds under the reply (or under the Day tab), with a
  preview of exactly what is saved. Nothing is uploaded.
- The panel is a borderless, non-activating `NSPanel` at status-bar level, so
  notch views never use sheets or alerts: confirmations and editors are inline.

## The Wisp window

- Sidebar: New chat (⌘N), search over chat titles (⌘K), Today, Research,
  Memory, Search everything, and every saved chat grouped Today / Yesterday /
  Previous 7 days / Earlier.
- Thread: your messages, then Wisp's. Tool calls are listed above the answer
  (collapsed once finished). Answers render through `MarkdownView`.
- Approvals: the same confirmation card as the notch (Deny / Always allow /
  Allow once), and the same editable message draft (Discard / Edit / Send).
  Nothing sends without the Send button.
- Details (⌘I): route, model, routed-by, speed, time, why that route, tools used.
- Composer: Return sends, Option-Return adds a line, paperclip attaches an image,
  and the pill shows Full access as the service confirmed it ("Access
  unconfirmed" when it can't be read).
- **Today**: the full planner (Needs my attention, Day plan, the task editor,
  pins, working hours, Email my day plan and its inline report), sharing one
  `TodayModel` with the notch's Brief and Day tabs.
- **Memory**: Facts (saved memories and corrections), Routines (what Wisp learned
  from your activity) and Review (new memories to confirm).
- **Research**: your 30 most recent research jobs and every pinned one. Opening
  a job shows it in the notch without starting a new search; New research opens
  the notch's research prompt.
- Appearance: follows the Mac. Dark is the aurora; light is a pale daylight blue.
- It opens from the Dock icon (or opening Wisp again while it runs), the menu
  bar's Open Wisp Window, Window ▸ Wisp Window (⌘1), the notch's ⤢ or ⌘O (on
  whatever the tab shows), and every route below. Closing it leaves Wisp running
  in the notch and menu bar; ⌘Q stops it.

Settings and the full setup guide are still their own windows, opened only on
request.

## How it fits together

| Piece | File |
| --- | --- |
| Routes into the app (`WispRoute`, `WispRouter`); every control opens one | `app/Sources/WispApp/WispRoute.swift` |
| Where each route lands (`AppDelegate.open(_:)`) | `app/Sources/WispApp/AppDelegate.swift` |
| Notch panel, tabs, Ask | `OverlayView.swift`, `OverlayModel.swift`, `NotchTab.swift` |
| Brief and Day tabs | `NotchBriefView.swift`, `NotchDayView.swift`, logic in `NotchDay.swift` (share `TodayModel` with the planner window) |
| Quick settings | `QuickSettingsView.swift` (shares the app's one `SettingsLoader`) |
| Setup card | `NotchSetupView.swift` (shares the app's one `SetupGuideModel` with `SetupGuideView`) |
| Inline reports | `ReportDraftView.swift`, `DiagnosticReport.swift` (`ReportDraft`) |
| Slash commands | `NotchCommand.swift` |
| Window (`WispWindowController`, `WispWindowNavigation`) | `app/Sources/WispApp/WispWindow.swift` |
| Window chats: types, store, service adapter, views, colors | `app/Sources/WispApp/Chat/` |
| Window Today (the full planner) | `TodayHomeView.swift`, `TodayView.swift` |
| Window Memory | `MemoryHomeView.swift`, `MemoryView.swift`, `LearnedView.swift` |
| Window Research | `ResearchHomeView.swift`, `ResearchLibraryView.swift`, `ResearchLibraryModel.swift` |
| Visual QA harness (`WISP_CHAT_QA` only) | `app/Sources/WispApp/Chat/ChatQA.swift` |
| Chat list endpoint | `service/memory/store.py` `list_chats`, `service/main.py` `GET /chats` |

Chats are not stored in the app. The service persists every conversation in
`~/.moe/sessions.db`; both surfaces list them with `GET /chats`, load one with
`GET /sessions/{id}`, delete with `DELETE /sessions/{id}`, and continue one by
passing its `session_id` to `/agent`. Closing the window loses nothing.

Shared state lives in app-scoped objects owned by `AppDelegate`, never in a view,
so the notch collapsing or a tab being rebuilt can't drop a change in flight:

- `TodayModel`: the notch's Brief and Day tabs and the planner window show the
  same day. It loads while Brief or Day is on screen, never at launch or on hover.
- `SettingsLoader`: quick settings, the Settings window and the window's Full
  access pill share one source. Full access is a `Bool?`: nil is "unconfirmed",
  never "off". A change shows only once the service confirms it, and a lost reply
  is followed by a re-read.
- `SetupGuideModel`: the setup card and the setup window. Each host calls
  `show(in:)` and `hide(from:)` (counted); the first starts the status poll, the
  last stops it and drops the test result. The window's host is its window
  object. Settings posts `wispInferenceConfigurationChanged` after writing a
  role's model (`POST /config`), a provider or the engine mode, which also drops
  the test result. A card set aside with Later leaves only when it sees Wisp
  ready, so its "!" can outlast readiness until Ask is next opened.
- `ReportDraft`: one per surface (the notch's chat, Today). It holds a
  `ReportSnapshot` taken at open (metadata, details scrubbed once) and never
  changed, plus the note and details switch, which reset whenever a report
  opens on a different key. Chat drafts are keyed by trace
  (`OverlayModel.reportKey(for:)`), and New chat closes one. The preview and the
  saved file both come from `ReportDraft.encoded()`; `DiagnosticReport.save`
  writes it privately and atomically. The save panel runs with
  `OverlayModel.pickingFile` set. Turning off planner-detail capture closes a
  Today report holding those details.
- `WispWindowNavigation`: the window's section (chats, Today, Memory,
  Research), the planner's and Memory's tabs, the Routines model (it lives as
  long as the window, so a consent write in flight reports its outcome), the
  research library and the shared `TodayModel`. `TodayWindow` remains only as a
  standalone host for the mounted planner checks.

`MarkdownView` reads its colors from `\.markdownPalette`; the default is the
notch's monochrome look.

## Known limits

- A reloaded chat shows which tools an answer used but not their results or
  timing; the service keeps only the tool names. Live answers show everything.
- There is no per-message model picker: the router chooses the model.
- Settings and the setup guide are separate windows rather than sections of
  the Wisp window.

## Testing

```bash
scripts/test_chat_contract.sh            # window reducer, history, titles, grouping, store (scripted backend)
scripts/test_overlay_transition.sh       # notch motion, collapse holds, tabs, quick settings, setup card, reports
scripts/test_today_contract.sh           # Today model and planner, including its inline report
scripts/test_diagnostic_report.sh        # redaction, journals, private saves, report drafts
.venv/bin/python tests/test_chat_list.py # GET /chats storage
```

`scripts/run_simulation_qa.py --only-native` runs these with the pure notch
checks (`tests/NotchTabChecks.swift` with `tests/NotchDayChecks.swift`, and
`tests/NotchCommandChecks.swift`).

Render the real window against a scripted fixture, in light and dark, without
starting Wisp or touching its port:

```bash
cd app
swift build -Xswiftc -DWISP_CHAT_QA
WISP_CHAT_QA_OUT=/some/dir .build/debug/WispApp
```

Render the notch's Brief and Day tabs from a fixture day the same way (it
prints each tab's size, to check it fits the 640 pt panel):

```bash
cd app
swift build -Xswiftc -DWISP_NOTCH_QA
WISP_NOTCH_QA_OUT=/some/dir .build/debug/WispApp
```

On a machine whose `swift` is the Command Line Tools toolchain, SwiftUI's `@State`
macro plugin is missing; add
`-Xswiftc -plugin-path -Xswiftc /Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/usr/lib/swift/host/plugins`
to the build command.
