# Wisp onboarding: research and improvement plan

**Date:** 2026-10-25. **Read against:** `origin/main` dc02c5d6, plus the in-progress
`claude/first-run-setup` branch (71e7bcb2 and its uncommitted `FirstRunSetupView.swift`),
owned by the "Wisp notch compatibility across Mac models" session. This document is
read-only research; it edits nothing.

## 1. What good onboarding looks like

The research converges on a few rules (sources at the end):

1. **Get to a real answer fast and teach by doing, not by touring.** Apple's HIG says
   onboarding should be "fast, fun, and optional", and that people should learn by
   using the app. Nielsen Norman Group (NN/g) finds that tutorials people must read
   before using the app make it harder to use. The success metric is
   *time to first useful answer*, not "finished onboarding".
2. **Provide defaults and postpone everything that isn't needed.** Pick a sensible
   default instead of asking. Make accounts (Gmail) optional and offer them later.
3. **Never block on a long wait.** While unavoidable content downloads, let people
   do other useful things. Show honest, determinate progress, and offer Cancel and
   Pause. Bundle enough content that nobody is stuck waiting on a download.
4. **Ask for permissions in context, each with a specific reason.** The HIG says to
   ask when a feature first needs a permission, unless the app can't work without
   it. Each request needs a brief, complete, specific sentence ("to show your day
   plan", not "for a better experience"). Studies show that giving a reason raises
   grant rates.
5. **On macOS, automate what can't be prompted.**
   - Full Disk Access has no request API and no status API. Check it by trying the
     real file, and never by reading `TCC.db`.
   - Guide the user with the exact System Settings pane, plus a draggable app icon
     and a short instruction.
   - Poll while that step is on screen, and advance on its own once access is
     granted.
6. **Ask few personalization questions, each skippable, each with its reason.**
   Drop-off rises with every question. Show the user what was saved and let them
   edit it. Wisp already does this well.
7. **Never rely only on the menu bar or notch.** Per the HIG, the system hides menu
   bar extras when the bar is crowded. Teach the hotkey first and confirm the user
   can open Wisp. Launch at login should be an explicit opt-in.
8. **Measure the steps of the funnel.** Track when the download starts, completes
   or fails. For each permission, track whether it was shown, granted or denied.
   Track questionnaire drop-off by question, time to first answer, and whether the
   user comes back on day 2. Keep these counters on the Mac.

## 2. Wisp's first run today (`origin/main`)

| # | What happens | Problem |
|---|---|---|
| 1 | Gatekeeper blocks the app (not notarized); README still implies right-click ▸ Open | On macOS 15+ the user has to use System Settings ▸ Privacy & Security ▸ Open Anyway |
| 2 | `Permissions.bootstrap()` triggers the Notifications, Calendar and Reminders system dialogs at launch | Three dialogs arrive before the user knows what Wisp is, with no reason given |
| 3 | 0.8 s in, the notch tip says "Press ⌥Space or hover to ask" | Wisp can't answer yet (test-plan hypothesis H1) |
| 4 | Up to 3 min of polling, then the notch setup card opens: "Engine / Model / Test" | Jargon: oMLX, "Not validated", "saves the real context window" |
| 5 | Downloading the model needs "Download a model ⤢", which opens a *second* window | The longest wait sits behind two clicks in two places |
| 6 | Getting to know you waits for the model; it stops checking after about 10 min (H2) | The one model-free task is held back until the wait is over |
| 7 | Ask's starter chips: "Organize files", "Describe an image"… | Generic. None of them shows Wisp knowing your own day |
| 8 | Menu ▸ Grant More Access fires Screen Recording, Accessibility and Input Monitoring prompts, then opens the Full Disk Access pane, all at once | Nothing in Wisp needs Input Monitoring (the code comment says so). Messages silently needs Full Disk Access (H3) |
| 9 | Menu item "Set Up Inference…" | Jargon |
| 10 | No launch-at-login option anywhere in the app | A menu-bar assistant disappears after a restart |

What is already good: the engine starts on its own, the downloaded model is applied
on its own, and everything says it runs on this Mac. Getting to know you is short,
optional, says why it asks each question, and saves as you go.

## 3. The in-progress first-run window (`claude/first-run-setup`)

The other session is replacing rows 2–6 with one window: **Model → Permissions → How
to use**. The model download starts on step 1 and keeps going while the user moves on.
Permissions are grouped by what they unlock, each row has a status that updates by
itself, and Full Disk Access is checked by opening `chat.db`. This already follows
rules 3–5. The review items below were sent to that session on 2026-10-25:

**P1**
1. **Quitting mid-setup leads to a dead end.** At launch the window sets `setupGuide.shown`,
   `notchTip.shown` and `didRequestPermissions`, but `firstRun.done` is set only when the
   window closes. Quitting with ⌘Q, a crash or a restart skips that, so the next launch
   shows no window, no setup card and no tip. Fix: add a `firstRun.started` flag and
   reopen the window while it is started but not done.
2. **Pressing Return skips the download.** "Continue" is the default button even
   before the download has started. Fix: make the default "Download and continue".
3. **Getting to know you is wrongly held back until the model is ready.** The profile
   is plain storage. Run it *during* the download, as its own step.
4. **There is no way to leave and be told when Wisp is ready.** Fix: a "Close — I'll
   let you know" button, and a notification when the model is ready that suggests a
   first question.

**P2**

5. Check free disk space before the 5.5 GB download.
6. Opening Mail hidden on a Mac with no Mail account may bring up Mail's add-account
   wizard. Also offer "Connect Google" on that row for people who read Gmail in a
   browser.
7. The Full Disk Access step should say to click +, add Wisp and switch it on, and
   show a draggable Wisp icon.
8. The System Settings links use two URL forms; check each one on macOS 26.
9. "N of 5 allowed" reads like a score. Every row is optional.
10. Make the "Try asking" examples clickable, so they open the notch with the
    question sent.

**From the research, to add**

11. **"Try it now: press ⌥Space" on the How to use step.** Detect the press and show a
    check mark. This is what makes "where does Wisp live" stick, and the hotkey is
    the one way in the system can't hide.
12. **An "Open Wisp at login" toggle** (`SMAppService.mainApp`) on the last step, off
    by default, with clear wording. Test it from /Applications; it can show up under
    "Allow in the Background" instead.
13. **Pause on the download.** The engine already resumes interrupted transfers. If
    progress stalls for more than 30 s, say so plainly ("No progress — check your
    connection") rather than leave the bar frozen.
14. **Every permission row's reason in one complete, specific sentence**
    (HIG purpose-string rule), and the same wording in `Info.plist`.

## 4. After the window: the first week

Owners still to be agreed; none of this is in the first-run branch.

- **The first answer should come from your own data.** For the first days, replace
  the generic Ask starters (`OverlayModel.swift:412`) with ones that use the
  sources that were granted: "What's on my calendar today?", "Any emails I need to
  reply to?", "What's due this week?". Hide any whose source was denied.
- **One-time tips at first use** (TipKit style, one at a time, at most one a day):
  Brief tab the first morning, Day tab the first time an event is close, ⌘⇧F
  the first time the user types "find".
- **Permissions at first use, for anything skipped.** When a question needs a source
  that wasn't granted ("text from Mom" with no Full Disk Access), answer with one
  line and a "Set up Messages" button. Don't fail silently.
- **Grant More Access** should ask only for what is still missing, explain each one,
  and drop Input Monitoring.
- **Menu wording:** "Set Up Inference…" → "Finish Setup…" while not ready, then
  "Model & Engine…".
- **Local onboarding counters.** Build the `onboarding_timeline.jsonl` poller the
  test plan describes. It logs time to first answer, permission outcomes and
  download failures, with no content, and Report a Problem includes it.
- **Release notes:** replace "right-click ▸ Open" with the macOS 15+ Open Anyway
  path until builds are notarized.

## 5. How to check it

Run the clean-account test in `docs/plans/2026-10-25-new-user-onboarding-test.md`
(branch `claude/onboarding-test-plan`) once the first-run window lands. Add two
variants: **quit during step 2** (item 1) and **press Return through every step**
(item 2). Pass bar: time to first answer under 15 min on 100 Mbps, no dead ends,
nothing asked twice on relaunch.

## Sources

- Apple HIG: [Onboarding](https://developer.apple.com/design/human-interface-guidelines/onboarding),
  [Privacy](https://developer.apple.com/design/human-interface-guidelines/privacy),
  [Loading](https://developer.apple.com/design/human-interface-guidelines/loading),
  [Progress indicators](https://developer.apple.com/design/human-interface-guidelines/progress-indicators),
  [The menu bar](https://developer.apple.com/design/human-interface-guidelines/the-menu-bar),
  [Offering help](https://developer.apple.com/design/human-interface-guidelines/offering-help),
  [Managing accounts](https://developer.apple.com/design/human-interface-guidelines/managing-accounts)
- NN/g, [Onboarding: skip it when possible](https://www.nngroup.com/videos/onboarding-skip-it-when-possible/)
- Elbitar et al., USENIX Security 2021, [permission rationales and grant rates](https://www.usenix.org/conference/usenixsecurity21/presentation/elbitar)
- Material, [Permissions patterns](https://m1.material.io/patterns/permissions.html)
- Apple Developer Forums: [no FDA check API, probe real access](https://developer.apple.com/forums/thread/841091),
  [AXIsProcessTrusted can lie](https://developer.apple.com/forums/thread/794253),
  [login item shows under Allow in the Background](https://developer.apple.com/forums/thread/791948)
- Michael Tsai, [people don't read similar dialogs / drag-the-icon helper](https://mjtsai.com/blog/2019/01/28/people-dont-read-similar-dialogs),
  [AEDeterminePermissionToAutomateTarget](https://mjtsai.com/blog/2018/08/31/aedeterminepermissiontoautomatetarget-added-but-aepocalyse-still-looms)
- [Screenpipe permissions guide](https://docs.screenpipe.com/permissions),
  [Superwhisper quickstart](https://superwhisper.com/docs/get-started/quickstart.md),
  [Raycast screen awareness](https://manual.raycast.com/ai/screen-awareness)
- [SurveyMonkey: question count vs completion](https://www.surveymonkey.com/curiosity/survey_questions_and_completion_rates/),
  [Userpilot onboarding metrics](https://userpilot.com/blog/user-onboarding-metrics/) (vendor data)
- [Apple Intelligence download behaviour](https://www.macobserver.com/tips/apple-intelligence-download-time-duration-speed-up-guide/),
  [Launch at login with SMAppService](https://nilcoalescing.com/blog/LaunchAtLoginSetting)
