# New-user onboarding test

**Status (2026-10-25):** test design, not yet run. Base: `origin/main` dc02c5d6.

## Why

Every run of Wisp so far has been on a Mac that already had it: grants given,
`~/.moe` full, the setup card dismissed years ago in Wisp time, a model on disk.
Nobody has watched a stranger go from a download link to a first useful answer.
The 0.5.0 clean-install pass found a P0 in the bundled-engine guard, which is
the kind of bug that only shows up from zero. This test makes that pass
repeatable, and measures how the first run *feels*, not just whether it crashes.

## What "good" means

One number and four questions.

- **Time to first answer (TTFA):** from double-clicking the downloaded ZIP to
  the first correct reply to a question about the tester's own day. Target:
  under 15 minutes on a ~100 Mbps connection, most of it the model download.
- **Dead ends:** any point where the tester doesn't know what to do next, or
  the app gives no sign of progress for more than 10 seconds. Target: zero.
- **Interruptions before value:** permission prompts, cards and notices shown
  before the first answer. Fewer is better; each one is listed in the log.
- **Wrong or stale copy:** any instruction that doesn't match what the screen
  shows (for example "right-click ▸ Open" on macOS 26).
- **Second launch:** nothing asked twice, nothing from first launch reappears.

## Environment

A **new standard (non-admin) macOS user account** on the 16" MBP, named
`Wisp Tester`. It's the only way to get a truly clean slate: TCC grants,
Gatekeeper quarantine, UserDefaults, Keychain, `~/.moe` and the engine's model
folder are all per-user, and none of them can be reset convincingly in your
own account.

A macOS VM (Virtualization.framework) would be more repeatable, but MLX
inference in a guest is slow or unsupported and 24 GB can't hold a host and a
guest model. Use a VM later for the parts that need no model (Gatekeeper,
permission prompts, setup copy).

### Before the run (in your account)

1. **Quit Wisp completely** (menu bar ▸ Quit Wisp). Fast user switching keeps
   your Wisp running, and ports are machine-wide: your backend on 8765 would
   make the tester's Wisp report a port conflict, and two engines won't fit in
   24 GB.
2. Confirm nothing holds Wisp's ports: `lsof -nP -iTCP:8765 -iTCP:8000 -sTCP:LISTEN`.
   oMLX on 8000 may be pinned by a test harness; stop that too.
3. Note the build under test (version, build number, commit) and the network
   speed (`networkQuality -s`).

### Test data

Run it twice:

- **Run A, empty account.** No mail, calendar or messages. Tests that every
  empty state explains itself instead of looking broken.
- **Run B, seeded account.** Sign Mail and Calendar into a throwaway test
  Google or iCloud account seeded with ~20 emails (two that ask the tester to
  do something, one security alert, one Canvas notice), ~6 calendar events
  across today and tomorrow, and 3 reminders (one overdue). Never a real
  personal account. Messages is optional (needs an Apple ID and Full Disk
  Access).

## How to get the app in

Download the release ZIP **in the tester account's browser** (or AirDrop it
in). Both add the quarantine flag that triggers Gatekeeper. Copying a locally
built app through `/Users/Shared` skips Gatekeeper and hides the first problem
a real user hits. Because `adjad/wisp` is private, sign in to GitHub in the
tester's browser, or AirDrop the ZIP from your account.

## The script

Record the whole run with QuickTime (File ▸ New Screen Recording) and keep the
log sheet below open on another device. At each step, write down the clock
time, what appeared, and anything confusing.

| # | Step | Expected | Watch for |
|---|------|----------|-----------|
| 1 | Unzip and double-click Wisp.app | Gatekeeper blocks it (ad-hoc, not notarized) | Does the release note's "right-click ▸ Open" work? On macOS 15+ it shouldn't; the real path is System Settings ▸ Privacy & Security ▸ Open Anyway |
| 2 | First launch | Dock icon, notch bar, menu-bar icon | Notifications, Calendar and Reminders prompts: order, wording, and whether they arrive before the tester knows what Wisp is |
| 3 | Wait for the backend | A visible "starting" state; cold start can take up to ~3 minutes | Any silent period over 10 s. Time from launch until `setup/status` answers |
| 4 | First-run tip vs setup card | One clear next step | **Hypothesis H1:** the "Press ⌥Space or hover to ask" tip opens 0.8 s after launch, before Wisp can answer, and the setup card opens on top of it once the backend reports not-ready |
| 5 | Setup card: engine and model | Recommends Ling 6-bit (6.6 GB) on 24 GB | Is the size and wait time stated before the download starts? Is progress visible? Try Cancel, then start again |
| 6 | Model download finishes | Card says Wisp is ready | Time it. Does anything tell the tester what to try first? |
| 7 | Getting to know you | Offered once setup is ready, without stealing the keyboard | **Hypothesis H2:** the offer gives up after ~10 minutes of waiting for setup, so a download longer than that means no offer on first launch |
| 8 | First questions (fixed list below) | Correct, grounded answers | First-use prompts: Automation for Mail, Contacts. Are they explained when they appear? |
| 9 | Brief and Day tabs | Run A: empty states that explain themselves. Run B: cards for the two action emails, security alert first, overdue reminder shown | Anything that looks broken rather than empty |
| 10 | Messages | Run B only | **Hypothesis H3:** reading Messages needs Full Disk Access, which is only reachable from menu bar ▸ Grant More Access. Does a new user ever find out why Messages is missing? |
| 11 | Grant More Access | Screen Recording, Accessibility, Input Monitoring, Full Disk Access pane | Is each one explained? Does the tester know which are optional? |
| 12 | Quit and relaunch | No prompts, no setup card, no tip | Getting to know you offered again only if Later was chosen (at most twice on its own) |
| 13 | (After #360) Notchless mode | `defaults write <bundle id> wisp.debug.surfaceStyle menuBar`, relaunch | Nothing drawn at the top; top-edge push and menu-bar icon both open it; first-run tip names the menu bar |

### Fixed first questions (step 8)

Ask them in this order, in the notch:

1. "What can you do?"
2. "What's on my calendar today?"
3. "Do I have any emails I need to reply to?"
4. "Remind me to call the dentist tomorrow at 10am"
5. "What's the weather like in San Francisco?" (expected: an honest answer about what it can and can't look up)
6. "Draft a reply to the most recent email" (draft only; **do not send**)
7. "What did I say I'm working toward?" (checks the Getting to know you answers are used)
8. One open question the tester writes themselves, without help

## Variants (one per extra run, after A and B pass)

- **Offline mid-download:** turn Wi-Fi off at 50%, back on after a minute.
  Expected: a clear error and a resume or retry, never a stuck bar.
- **Quit mid-download:** quit Wisp at 50% and relaunch.
- **Later on everything:** dismiss the setup card and Getting to know you.
  Expected: Ask shows one line explaining how to finish setup; nothing reopens
  by itself more than designed.
- **Deny Calendar and Reminders:** expected: Brief and Day say what's missing
  and how to grant it, instead of showing an empty day.
- **Low disk:** under 8 GB free before the download. Expected: a warning
  before the download, not a failure partway through.
- **16 GB Mac** (the Air node): expected: recommends Ling 4-bit (4.6 GB), and
  replies stay responsive.

## Evidence to keep

- The screen recording.
- The log sheet (timestamps, prompts, confusion points).
- Menu bar ▸ Report a Problem… output at the end of each run.
- `defaults read <bundle id>` from the tester account (first-run keys:
  `wisp.setupGuide.shown`, `wisp.notchTip.shown`, `wisp.didRequestPermissions`).
- `~/.moe` logs from the tester account.
- A timeline of `GET /setup/status` and `GET /profile`. A small read-only
  poller that writes `onboarding_timeline.jsonl` with a timestamp on every
  state change would make TTFA and H1/H2 exact rather than hand-timed. It
  isn't built yet.

## Scoring

Each step gets **pass**, **friction** (works, but the tester hesitated or the
copy was off) or **fail** (dead end, crash, wrong answer, or something asked
twice). Each friction and fail becomes a finding with the step number, the
recording timestamp, and a severity:

- **P0:** a new user can't reach a first answer.
- **P1:** they get there, but only by guessing or with outside help.
- **P2:** confusing copy, extra prompts, or a slow spot with no feedback.

The run passes when TTFA is under target, there are no P0s or P1s, and step
12 is clean.

## Cleanup

1. Sign out of the test accounts in the tester's Mail and Calendar.
2. Log out of `Wisp Tester`. Deleting the account (System Settings ▸ Users &
   Groups) also deletes its downloaded model and data.
3. In your account, relaunch Wisp and check that 8765 is your backend again
   (not one left from the tester).

## Later

- Run the no-model steps (1–4, 11) in a macOS 26 VM from a snapshot, so
  Gatekeeper and permission copy can be checked on every release.
- Turn H1–H3 into regression checks once confirmed or fixed.
