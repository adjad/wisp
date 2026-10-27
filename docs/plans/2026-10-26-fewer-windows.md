# Fewer windows in Wisp: inventory and plan

**Date:** 2026-10-26.
**Read against:**
- `origin/main` dc02c5d6
- `claude/first-run-setup` 521a00aa
- `claude/rc1-first-run` 984d7006, re-checked at 92a2d6a8

**Method:** read-only. There were three inventories (code, user journeys, history and decisions) covering 39 surfaces, 15 journeys and 13 standing decisions. Three independent designs (notch-first, one-window hub, highest-ROI) were each attacked by a reviewer against the code, then merged into one plan.

**Correction after the run:** fix #3b below (launch prompts landing over the Welcome window) is already fixed on `claude/rc1-first-run` by 0395bbe2 (`FirstRunReaderGate`). Fixes #3a and #3c still apply.

**Already settled, so not re-litigated:**
- The user rejected a "one main window" design. Notch-first stays.
- Memory stays in the Wisp window only.
- Research stays a hidden mode of Ask.
- The notch tabs stay Ask, Brief and Day.

# Plan: fewer windows in Wisp

**How windows are counted (after the critique corrections):**
- Each journey counts the distinct surfaces a person sees, and the notch counts once per journey.
- Menus and tab switches are not counted.
- A prompt that moves to first use is counted as **deferred**, not saved.
- A notarized build still shows one Gatekeeper confirmation, so notarizing saves **−2**, not −3.

**Where first run stands now:** `claude/first-run-setup` is no longer paused. `origin/claude/rc1-first-run` (984d7006) is shipping the Welcome window and has wired `native/first-run-setup-contract` into Simulation QA. Commit e73952ab already fixed the resume-after-quit trap. Once rc1 ships, journey J2 (the Welcome window) becomes the first-run path for new Macs, and the J1 numbers apply only to builds from before rc1.

## 1. Ranked changes

| # | Change | Windows saved (corrected) | Effort | Risk / gates | Owner | Order |
|---|---|---|---|---|---|---|
| 1 | Notarized Developer ID build | J1 and J2: −2 on every fresh install (3 Gatekeeper surfaces become 1 confirmation) | M | Release Auditor + specialist QA. Needs the user's Apple credentials and permission to publish | unowned — new worktree, only after the user supplies credentials | Escalate now; runs in parallel |
| 2 | Model download inside the notch setup card | J1 on main: 9→8. After rc1 ships: −1 on the paths that land on the card (see detail) | M | Low–medium. Run a short Auditor pass, or record why none is triggered. Needs a yes on D1 | unowned — new worktree (rc1 first-run owner acknowledges via the Orchestrator) | Wave 0; before #8 |
| 3 | Fixes to rc1's Welcome window | −1 stacked Setup window (optional path). 3 launch dialogs move into step 2 (reordered, count unchanged) | S–M | Auditor + clean-install Simulation QA | first-run-setup session | Notes sent now |
| 4 | **Fork:** Welcome steps move into the notch card | J2: −1 window on every fresh install | L | High: reverses an active owner's design. Auditor + Simulation QA | first-run-setup session (rc1 onboarding session for the KnowYou hand-off and copy) | Only if D2 = B; after rc1 ships and after #2 |
| 5 | "Recent" list in the notch's Research mode | J6g, recent job: 2 distinct windows → 1 (the Wisp window is never opened) | S–M | Low | unowned — new worktree | Wave 1 |
| 6 | Permission quick fixes | J3: 6→5. −1 System Settings trip for anyone who already has FDA. J6e: two surfaces at once become one at a time (−1 only if the person denies) | S | One Auditor pass (permissions) | unowned — new worktree | Wave 0 |
| 7 | Reports and exports finish in place | J8 report: 2→1. J8 export: modal alert or Finder becomes a notch notice (count unchanged) | S | Low | unowned — new worktree | Next free slot in Wave 0, else Wave 1 |
| 8 | Setup window becomes Settings › Models › Setup | J5: 2→1. Window types −1 | M | Medium. Auditor recommended | unowned — new worktree | After #2 and after rc1's SettingsView hunks |
| 9 | One row per grant instead of the "Grant More Access…" burst | J3: 5→4 (option A, Settings) or 5→3 (option B, notch) | M | Auditor + Live QA on a throwaway user | rc1 onboarding session (first-run-setup session extracts FirstRunPermissions) | After rc1 and #6 |
| 10 | Settings becomes a section of the Wisp window (Setup goes with it) | J6f from Settings and J7b: 2→1. Window types −1. Journeys that start in the notch: 0 | L | High: R1 Release Auditor (privacy controls); full restyle | unowned — new worktree | Needs a yes on D3; after #8 and rc1 |
| 11 | Report and export from a Wisp-window chat | J8 started in the window: 3→1 (with #7) | M | Auditor (conversation privacy) | unowned — new worktree | After #7 and #10 |
| 12 | Saved research reports open in the Wisp window | J6g started in the window: 2→1. Started in the notch: 0 | M–L | Medium (restyle, shared ResearchModel) | unowned — new worktree | After #10 |
| 13 | Port conflict: a "Check again" that works | 0 windows. Removes a relaunch; −1 duplicate alert only if both alerts really appear (unverified) | S | Auditor (BackendTrust/PortGuard) | unowned — new worktree | After rc1 |
| 14 | In-app alert relay; morning-brief click opens Brief | 0 windows. Saves a tab switch and recovers alerts that are dropped while Wisp is frontmost | M | Auditor (outbound send results) | unowned — new worktree | Serialize with #7 and #11 |
| 15 | Move TodayWindow to the test target; fix stale docs | 0 (housekeeping) | S | Low | unowned — new worktree | After rc1 |

## 2. Detail for each change (critique fixes already applied)

**#1 Notarized build**
- **What:**
  - Dispatch `wisp-build.yml` with `publish=true` instead of `publish_ad_hoc`. The signing, notarizing and stapling path already exists: `release.py:72-88, 363, 532-543` and `pipeline.py:913-937`.
  - Every bundled Mach-O needs the hardened runtime and a Developer ID signature. That includes the Python runtimes and the private oMLX engine, whose `engine.entitlements` sets allow-jit, unsigned-exec-memory and disable-library-validation.
  - This path has never run end to end.
  - Remove `FIRST_LAUNCH_NOTE` (`release.py:120-123`) and the Open Anyway text (`README.md:174-179`).
- **Release notes:** the signing identity changes once, so existing installs (including the user's Wisp Dev re-sign installs) re-grant permissions once. After that, a stable identity should stop permission re-prompts after each update (inferred).
- **Gates:**
  - The user's Apple Developer membership, the six secrets, and release-environment approval.
  - Ad-hoc and notarized publishes are mutually exclusive (`wisp-build.yml:330-333`), so coordinate with the 0.5.0 release owner (#273).

**#2 Model download inside the notch setup card**
- **Replace the chip:** `Download a model ⤢` (`NotchSetupView.swift:135-142`) becomes an inline row that mirrors `SetupGuideView.downloadCard` (`770-813`):
  - the model's label and **size on the button**;
  - Download, Cancel, Retry, and "Download again";
  - a ProgressView with bytes and speed.
- **Shared helper:** move `downloadProgress` (`SetupGuideView.swift:903-909`) into a Foundation-only extension on `SetupGuideModel.DownloadTask` inside `SetupGuideView.swift`. That file is compiled with only Theme.swift (`tests/test_setup_guide.py:864-866`, `tests/test_engine_downloads.py:284-287`).
- **Keep ⤢ for three cases:**
  - an external oMLX engine;
  - a bundled engine with no public download (`check.action == 'add_local_model'`, `SetupGuideView.swift:686-694`);
  - the optional footer chip.
- **Add a download host:** while any transfer is active, SetupGuideModel keeps a `download` host registered.
  - Without it, collapsing the panel (Later, ⌥Space, or a tab switch) would stop the 3 s poll and the auto-apply, which only runs while a host is shown (`isShown` guard at `:204`).
  - With it, the panel can collapse while progress shows on the bar or menu-bar icon (#360).
- **QA fixture:** add idle, downloading, failed and done states in a **new** NotchQA fixture file and register it in `build-support/simulation.py`. `NotchQA.swift` itself has +285 changed lines on rc1. Check the card's height on a small built-in display: the panel is 640 pt wide, and taller content clips (`OverlayView.swift:113-121`).
- **Docs:** update `NotchSetupView.swift:3-6, 138` and `CHAT_WINDOW.md:46-49`.
- **Who saves a window:**
  - On today's main, the Setup window leaves the required path.
  - After rc1 ships, these paths still save one: Welcome closed early (`firstRunClosed`), "Start using Wisp" pressed before the model is ready (`showSetupCard` already assumes the card carries the download), Set Up Inference… with no model installed, and people who chose Later.
  - On the Welcome window's own path it saves 0.
- **Files:** `NotchSetupView.swift`, `SetupGuideView.swift`, the new NotchQA fixture, `simulation.py`, `CHAT_WINDOW.md`.

**#3 Fixes to rc1's Welcome window** (notes for the first-run-setup session, sent through the Orchestrator)
- **(a) No stacked setup window.** "Connect it instead" and "Open detailed setup" (`FirstRunSetupView.swift:428-454` @984d7006, which calls `openSetupGuide`, `AppDelegate.swift:309`) should either swap to the detailed content inside the Welcome window or close it first. After #8, they should route to Settings › Models › Setup.
- **(b) Launch dialogs land over the Welcome window.** `createPanelIfNeeded` (`:291`) runs before `openFirstRunSetup` (`:296`), and `startNativeSourceReaders` (`:987`) still asks for Calendar, Reminders and Contacts over Welcome.
  - Hold the readers' access requests while `FirstRunSetup.shouldShow` is true.
  - Start the readers on finish, on early close and on skip.
  - **Keep `wisp.didRequestPermissions` set at launch.** Otherwise `Permissions.bootstrap` fires at launch and again after every Full Disk Access "Quit & Reopen".
- **(c) Activate first.** Every permission row should call `NSApp.activate` before it asks (the `RemindersWriter.swift:482-490` pattern), or the system prompt can end up behind other windows.

**#4 Fork B: Welcome steps in the notch card**
- **What moves:** FirstRunBrain and FirstRunPermissions render as the card's first-launch mode.
- **Behaviour:**
  - Key focus on the very first launch only.
  - `holdsOpen` keeps the card up through macOS dialogs and System Settings trips.
  - **During every TCC request and System Settings trip, the card shrinks to one line.** The panel sits at `.statusBar` level and could otherwise cover centred alerts or the System Settings window.
  - Activate the app before each ask.
  - Show "Starting Wisp…" while the backend cold-starts, instead of the "isn't answering" error (`SetupGuideView.swift:302-303`). The Permissions step can run before the backend is up.
  - Rely on the 2 s poll, because `didBecomeActive` does not fire from the non-activating panel.
  - The tour replaces the 10 s "Click Wisp in the Dock" tip.
  - Keep e73952ab's resume keys.
- **Not counted as savings:** deferring the Mail/Notes Automation prompts and Contacts would make them appear out of context later.
- **Rejected alternative:** the hub's full-window first-run mode (one-window proposal C3). It gives the same count as fixed Welcome plus #3a, and Dock/⌘1 `show()` resets it to Chats and calls `newChat()`.

**#5 "Recent" research in the notch**
- **What:** Research mode lists the 3 most recent saved jobs under the prompt (`OverlayView.swift:94-101`) and opens them in the compact view.
- **Guard:** `openJob` wipes an unsaved plan or a running view (`ResearchModel.swift:45-62`), so disable the list or confirm inline in that state.
- **Compile list:** add no dependencies to `ResearchLibraryModel` or `ResearchLibraryView` (`scripts/test_research_library_contract.sh:13-22`).
- **Merge:** `OverlayView.swift` is edited by #360 and rc1, so expect a small rebase.

**#6 Permission quick fixes**
- **(a) Remove the unused Input Monitoring request:** delete `IOHIDRequestAccess` and `import IOKit.hid` (`Permissions.swift:6, 44`).
  - Verified unused: the hotkey uses Carbon, the only global monitor watches mouseMoved (`AppDelegate.swift:701`), and Messages sends need Accessibility.
  - Confirm that the `ActivityReader` idle timer (`ActivityReader.swift:110`) still sees typing.
- **(b) Skip the FDA pane when FDA is already on.** Probe before opening it, using the errno classification in `MessagesReader.swift:605-623`.
  - ENOENT is common on a Mac that has never used Messages, so the Auditor should pick a probe file that always exists and needs Full Disk Access.
- **(c) Smart Search Accessibility, one surface per click.**
  - The first click shows the system prompt only.
  - Later clicks, while Wisp is still untrusted, open `Privacy_Accessibility`.
  - Set the "prompted" flag inside `Permissions.requestAccessibility`, so a prompt already used up by "Grant More Access…" does not leave a dead first click.
  - Leave the button label alone: permission copy belongs to the rc1 onboarding session.
- **(d) Comments:** fix the stale ones at `Permissions.swift:17-28`, since ContactsReader asks for Contacts on purpose.
- **Keep signatures:** `openFullDiskAccessPane` and `openSettingsPane` are called by rc1.

**#7 Reports and exports finish in place**
- **(a) Report Save writes straight to `~/Downloads`** with `ReportDraft.write(to:)` (`DiagnosticReport.swift:356-365`). The card then shows "Saved · Reveal in Finder", tap only.
  - If the write is denied, fall back to NSSavePanel and keep `pickingFile` set. The first write to Downloads may raise a macOS prompt (unverified; the app is not sandboxed).
- **(b) "Nothing to export yet" becomes a notch notice.** In order: `openAssistant()`, set `tab = .ask`, set `showingQuickSettings = false`, then show the notice.
  - If KnowYou is open, end that flow first or keep the alert.
  - Use a neutral colour, about 6 s, and say the export covers the notch conversation.
- **(c) A successful export shows a tappable notice** with "Reveal" instead of opening Finder by itself. `showTransientNotice` is a plain 4 s string today, so it needs a tappable variant.
- **Compile lists:** new code must stay out of the fixed lists for `DiagnosticReport.swift` and `ReportDraftView.swift` (`test_diagnostic_report.sh:12-15`, `test_today_contract.sh:9-19`).

**#8 Setup window becomes Settings › Models › Setup**
- **Subpane:** a new subpane next to Local models… and Cloud… (`SettingsView.swift:1302-1306, 1531`). It hosts `SetupGuideView.pageContent` plus a "Check again" row, but **not the Setup footer**: its "Settings…" button and default-action Done don't belong there.
- **Where the wrapper lives:** in SettingsView.swift. SetupGuideView.swift must still compile with only Theme.swift.
- **Host lifecycle:** AppDelegate owns one boolean `settings` host.
  - Show it when the window opens or the pane changes into Setup.
  - Hide it on `willClose` or when the pane changes away.
  - Do not count SwiftUI `onAppear`/`onDisappear`: the window is built once and never released.
- **`openSetupGuide`:**
  - Keep the name, because rc1 calls it.
  - Set `WispSettingsPane=models` and `WispModelsPane=setup`. Verify that an already-open window follows `@AppStorage`; if it doesn't, post a notification.
  - **Still set `setupGuideShownKey`.**
  - Then call `openSettings()`.
- **Know-you gate** (main `:1244`, rc1 `:1431`): include the settings host. A `setupWindow` that is always nil silently drops the "not over setup" guard.
- **Set Up Inference…** stays on the notch card.
- **Layout:** the column is about 515 pt (about 467 pt inside pageContent's padding) against the 592 pt the guide was designed for, so check with a WISP_SETTINGS_QA render. Give SettingsView an optional SetupGuideModel parameter that defaults to nil, so the QA driver still works.
- **Docs:** `AppDelegate.swift:303-305`, `README.md:164-166`, `CHAT_WINDOW.md:46-49, 83-84, 150-151`.

**#9 One row per grant**
- **What:** "Grant More Access…" stops calling `requestExtras` (`Permissions.swift:38-47`) and opens a row list instead: Screen Recording, Accessibility, Full Disk Access, Calendar/Reminders, Notifications, and Mail & Notes Automation.
  - Each row shows live status and has one button.
  - Each button activates the app before it asks.
  - The rows reuse FirstRunPermissions and its 2 s poll.
- **Where the list lives:** option **A** puts it in Settings › Privacy (no rule change). Option **B** puts it in a notch access card, which needs D4.
- **Contacts row:** follows D5.
- **Compile list:** the extracted FirstRunPermissions file goes into `simulation.py`.

**#10 Settings becomes a section of the Wisp window**
- **Routing:**
  - Use a **new route case**, or update every bare `.settings` caller (`QuickSettingsView.swift:55`, `AppDelegate.swift:43`). A defaulted associated value does not compile.
  - Add the section to `Section` (`WispWindow.swift:104`), to ChatRootView's exhaustive switch (`ChatViews.swift:35-48`), to the sidebar, and to the ChatQA renders.
  - Point ⌘,, the menu's Settings… and the footer gear there, and delete the NSWindow builder (`AppDelegate.swift:1068-1082`).
- **Embedded mode:**
  - Drop the 164 pt sidebar, the fixed frame and `.preferredColorScheme(.dark)`. Dropping forced dark is **mandatory**: it would darken the whole window.
  - Fully restyle the hard-coded black and white Theme (`Theme.swift:6-17`). An environment-only stopgap is unverified for AppKit-backed controls.
- **Leave alone:**
  - The routed links for Research, Memory and "See what Wisp learned": `test_behavior_runtime.py:1564` asserts their literal text.
  - The source markers that `test_behavior_activity_native.py:111-121` slices out.
  - The standalone WISP_SETTINGS_QA render.
- **`show()` must not reset a visible window to Chats** (`WispWindow.swift:26-31`).
- **Dropped from the proposal:** activating Wisp after a Google connect.
- **Rider:** remove the "Search everything ⇧⌘F" sidebar row (`ChatViews.swift:132`). From the window it reads Wisp's own pid and returns nothing (`PageReader.swift:68-71`). ⌘K stays.
- **Trade-off:** Settings now replaces the main column, so Settings and a chat can't sit side by side.

**#11 Report and export from a Wisp-window chat**
- Window replies get "Report a problem", hosted by ReportDraftCard (already used in Today).
- The menu's Report and Export act on the **last-focused** Wisp surface, because `keyWindow` is nil while Wisp is inactive.
- Window chats have no trace ids, so the report is text and metadata only, with DiagnosticReport's redaction.
- Build it in ChatViews.swift, or update every fixed compile list it touches (`test_chat_contract.sh`, `run_simulation_qa.py:1026-1030`).

**#12 Saved research reports open in the Wisp window**
- Only saved or finished reports move. New research, plan approval and live runs stay in the notch, per `CHAT_WINDOW.md:22-24`.
- Use the compact layout or a restyle: the full layout needs about 816 pt, and the column has 551 pt.
- Keep the notch dark with `.environment(\.colorScheme,.dark)` in the compact host.
- Keep the `ResearchLibraryView(model:onOpen:onNew:)` initializer unchanged.

**#13 Port conflict "Check again"**
- **Re-check:** re-run `settledStartupVerdict(check: { PortGuard.check(port: 8765, receipt: .none)…` using that exact literal, because `test_backend_ownership_no_disk_receipt.py:120` matches it by regex.
- **`.unknown` verdict:** handle it exactly as startup does. The two critiques disagree here, so the Auditor decides.
- **Still in conflict:** show the result **inline** in the notch card, never as a new modal. `presentPortConflict` is not deduplicated, so a new modal could loop.
- **On recovery:** also start `todayModel.startBackgroundRefresh`.
- **Duplicate alert:** skip the refusal alert while the conflict alert is up.
- **Window footer:** show "Check again" only when `portConflict` is set, and a plain retry otherwise.
- **Unchanged:** Wisp never signals the program holding the port.

**#14 In-app alert relay; morning-brief click opens Brief**
- **Relay:** drive it from `willPresent`. Show the alert inline when a Wisp surface can display it; otherwise return `[.banner,.list]`. Leave the acknowledgements and AssistantDelivery.swift unchanged (fixed compile list).
- **Brief click:** tag brief notifications with `userInfo` so a click opens Brief. The evening check-in still opens Day.

**#15 Housekeeping**
- Move TodayWindow to the test target.
- Define `didShowNotification` as a `Notification.Name` extension **in TodayView.swift**, because `test_today_contract.sh` compiles that file without WispWindow.swift.
- Fix only the docs that are stale now: `AppDelegate.swift:303-305`, `README.md:49-50, 331`. The rest change with #8 and #10.

## 3. Not adopted

| Proposal item | Why |
|---|---|
| Engine "Restart now" (notch-first C8) | Saves 0 windows, adds backend-lifecycle risk, and depends on rc1's `.wispRestartBackend`. Belongs in a separate program |
| Ask for Contacts once Full Disk Access is on (highest-ROI R8); Contacts only from its toggle (notch-first C4, one-window C5) | Contacts also backs `lookup_contact` before a send, `list_contacts`, birthdays and memory connections. Gating it would break "text Mom" for most new users. The toggle defaults ON, so the toggle trigger never fires. Moved to decision D5; only the stale comment gets fixed (#6d) |
| Full-window first-run mode in the hub (one-window C3) | Same count as Welcome with #3a. A Dock click or ⌘1 resets it, it loads sessions anyway, and ⌘N/⌘B/⌘K escape the onboarding |
| "Set Up Inference…" opens the hub (one-window C2 step 8) | Adds a window to setups the notch card already handles |
| Bring Wisp forward after a Google connect (one-window C1 step 9) | Breaks the rule that an offer nobody asked for never takes the keyboard (`AppDelegate.swift:1249-1250`) |
| New research and plan approval in the window (one-window C4) | Reverses the user-approved "Research is a hidden mode of Ask" |
| Defer Mail/Notes Automation prompts to first use | Only a deferral: the prompt would land over Mail at a sync tick, out of context. Left to the first-run owner |
| Stop the reader prompts at launch on main without #3 | `Permissions.bootstrap` still asks at launch. Asking for Calendar "once" saves 0 dialogs, because macOS shows one prompt either way |

## 4. End state: what remains and why

| Surface | When it appears | Why it stays |
|---|---|---|
| Notch bar and panel: Ask, Brief, Day, Research mode, quick settings, setup card with inline download, KnowYou, inline cards, report card, notices | Always (it is the anchor) | Notch-first decision. It is not a window the user opens |
| Smart Search overlay | ⌘⇧F | It reads, and jumps within, the app behind it. A window would cover the document being searched |
| **Wisp window**: Chats, Today, Research (library and saved reports), Memory, Settings (General, Models with Local/Cloud/Setup, Google, Activity, Privacy with Permissions) | Only on request | The only place for sheets (task editor, daily email, memory editor), destructive confirmations, long reading, and controls that widen access (kept out of the notch) |
| Welcome window | First launch on a new Mac only, if D2 = A | rc1's onboarding. With #3 it never stacks another window and is never re-shown |
| 3 modal alerts: backend refusal, port conflict, data recovery | Startup or security failures | Standing rule that these stay modal |
| Sheets and confirmations inside the Wisp window | Edits and destructive actions | Attached to a window that is already open; they open no new window |
| Open panel (attach image); save panel only as a fallback | On click | A file choice needs the system picker |
| Notification Center banners | While Wisp is in the background | Delivery when no Wisp surface is visible |
| Mail/Messages drafts (after approval); Calendar, Mail or Finder on a tap | When the user acts | The user asked for that app |

**Retired:**
- the Settings window and the Setup window;
- the Welcome window, if D2 = B;
- TodayWindow (now test-only);
- the "Nothing to export" alert, the report save panel by default, and the automatic Finder reveal;
- the Input Monitoring prompt, and the Accessibility prompt plus pane that opened together.

**Wisp's own window types:** main goes from 3 to 1. rc1 goes from 4 to 2 (Welcome window kept, first launch only) or to 1 (D2 = B).

## 5. Before and after, per journey

| Journey | Before | After (recommended: #1–3 and #5–15, D2 = A, #9 option A) | Best case (D2 = B, #9 option B) |
|---|---|---|---|
| J1: first run on main (before rc1) | 9 | 6 (8 without notarization) | 6 |
| J2: first run with the rc1 Welcome window, every permission row tapped | 14 | 12 (14 without notarization) | 11 (13 without) |
| J2 optional: "Connect it instead" | +1 stacked window | +0 | +0 |
| J3: Full Disk Access through Grant More Access… | 6 | 4 | 3 |
| J3 for someone who already has Full Disk Access | System Settings opens for nothing | No System Settings trip | same |
| J4: connect Gmail | 4 | 4 (3 only with a shipped OAuth client, D8) | 4 |
| J5: change to a model that needs a download | 2 | 1 (plus a browser for an external oMLX) | 1 |
| J5 path C: Set Up Inference… with no model installed | notch + Setup window | notch | notch |
| J6a: morning brief | 2 | 2 (lands on Brief) | 2 |
| J6b, J6c, J6d | 1, 1, 2 | 1, 1, 2 | same |
| J6e: Smart Search, first use, granting / denying | 4 / 3 | 4 one at a time / 2 | same |
| J6f: memory, entered from Settings | 2 | 1 | 1 |
| J6g: recent research job / full library from the window / full library from the notch | 2 / 2 / 2 distinct (3 visits) | 1 / 1 / 2 | same |
| J7a: Settings-only setting, starting in the notch | 2 | 2 | 2 |
| J7b: Settings-only setting, starting in the Wisp window | 2 | 1 | 1 |
| J8: report / export / report from a window chat / both together | 2 / 1 / 3 / 3 | 1 / 1 (non-modal) / 1 / 1 | same |
| J9: port conflict | 4 (5 if the refusal alert also shows) | 4, with no relaunch (4 worst case) | same |

## 6. Windows that cannot be removed (macOS forces them)

| Surface | Why it stays | How to keep the trip short |
|---|---|---|
| Gatekeeper | macOS checks every downloaded app | #1 turns 3 surfaces into one "downloaded from the Internet" confirmation. Until then, `README.md` gives the exact Open Anyway path. On macOS 15+ the Control-click shortcut probably no longer works (inferred), so the README should not lead with it |
| Full Disk Access (System Settings, authentication, Quit & Reopen) | It can't be prompted for; the person must flip the toggle | One deep link from the row or tile that needs it (existing `OverlayView.swift:933-943`, `BriefSummary.swift:162-164`). Skip the trip when it is already on (#6b). Resume setup after Quit & Reopen (e73952ab). Re-check every 2 s. Never open it in a burst (#6, #9). A held notch card shrinks to one line so it doesn't cover System Settings (#4, #9B) |
| Permission prompts (Notifications, Calendar, Reminders, Contacts, Automation, Accessibility, Screen Recording) | macOS owns consent | Ask from a row the person tapped, one at a time. Activate Wisp first so the prompt isn't hidden. Never at launch over another window (#3b). Never for something unused (#6a). A stable Developer ID identity should stop re-prompts after updates (#1, inferred). Automation prompts appear only while the target app is running |
| Accessibility and Screen Recording panes | Granting requires System Settings | One surface per click (#6c). Ask only on demand (#9). Some apps need a relaunch after the grant (`PageReader.swift:54-64`) |
| Google sign-in in the browser | Google requires browser consent | One sign-in per account (rc1 946a30fc). The Cloud Console steps and the 7-day testing expiry go away only with a shipped, verified OAuth client (D8). Switching back to Wisp stays manual (pulling Wisp forward was rejected) |
| Activity Monitor during a port conflict | Wisp never signals another program, by design | The alert names the pid, and #13 removes the relaunch afterwards |

## 7. Decisions for the user

| # | Standing decision affected | Changed by | Recommendation | If no answer |
|---|---|---|---|---|
| D1 | Placement rule: setup-once flows go to the window (`wisp-notch-first-ui.md:30`) | #2 narrows it to the model download only; #4 goes further | Yes for #2 | #2 waits |
| D2 | "Launch is notch-only" (e8f5e562, fb65a3af) versus rc1's actively shipping Welcome window. Whether the user chose the window form is unverified | #3 (A) or #4 (B) | A for rc1 with the #3 fixes; reconsider B after rc1 ships, once #2 is in | A |
| D3 | "Settings deliberately not moved: needs a restyle plus an R1 audit" (`wisp-notch-first-ui.md:26`, `CHAT_WINDOW.md:83-84, 150-151`). Settings would also follow the Mac's appearance instead of forced dark | #10 | Yes, after rc1 and #8 | #10 stays deferred |
| D4 | Controls that widen access stay out of the notch (`QuickSettingsView.swift:3-7`) | #9 option B (buttons that trigger macOS permission prompts in the notch). Also rc1's notch Google group (`NotchConnectGoogle.swift`, from 07ac1d0d), which puts OAuth in the notch | #9 option A. rc1 should record whether the Google group was user-approved before it reaches main | A; Google group flagged |
| D5 | Contacts is asked at first launch. `ContactsReader` asks by design, and `Permissions.swift:17-18` is the stale part | Not dispatched. Moving the ask to first use of a contact tool needs a backend→app request path and an Auditor review | Keep as is; fix the comment (#6d) | unchanged |
| D6 | Security and startup alerts stay modal | #13 shows the re-check result inline and suppresses the duplicate refusal alert | Yes; the Auditor confirms | #13 keeps modals only |
| D7 | Release and publishing are outside the standing authorization | #1 (credentials, one-time permission reset) | Yes | #1 blocked |
| D8 | Google OAuth through the user's own Cloud Console client | Not in this plan | Product call: a shipped, verified client removes the Cloud Console steps and the 7-day expiry | unchanged |

**Untouched:** notch-first; tabs Ask · Brief · Day; Memory only in the window; Research as a hidden Ask mode; Dock icon; the 640 pt panel; hover reopens the last tab; Day never writes to Calendar or Reminders; Getting to know you stays short; chats live in the service; the black notch while the window follows the Mac.

## 8. Dispatch order

Every change needs the Orchestrator's acknowledgement first. At most 2 proactive Worktrees at once. #3, #4 and #9 go to their existing owners, not new workers. One gate or build runs on the host at a time, with `--jobs 2`.

| Wave | Items | Notes |
|---|---|---|
| 0 (now) | Worktree 1: #6. Worktree 2: #2 (after the D1 yes and the rc1 first-run owner's acknowledgement). #7 takes the next free slot | #1 escalated to the user. #3 notes sent to the first-run-setup session. D2 asked |
| 1 (after rc1-integration lands) | #7 if not done, #5, #8 (after #2), #13, #15. #9 by the rc1 onboarding session | Rebase on rc1's hunks in SettingsView, AppDelegate (:127, :154), OverlayView and TodayView |
| 2 (after the D3 yes) | #10 → #11 → #12, one writer for AppDelegate, ChatViews and WispWindow. #14 serialized with #7 and #11 on OverlayModel | R1 Release Auditor for #10 |
| Fork | #4 only if D2 = B, after rc1 ships and after #2 | Auditor + clean-account Simulation QA |

**Gate reminders:**
- Any new Swift file also goes into `build-support/simulation.py`.
- Respect these fixed compile lists: SetupGuideView (Theme only), ResearchLibrary, Today, DiagnosticReport, Chat, privacy-sync (ContactsReader), backend-recovery and AssistantDelivery.
- Several Python tests assert exact source text: `test_behavior_runtime.py:1564`, `test_behavior_activity_native.py:111-121` and `test_backend_ownership_no_disk_receipt.py:120`.