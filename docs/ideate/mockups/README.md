# Ideate and Customize Wisp mockups

Real SwiftUI, rendered by the notch QA renderer at 2x (`WISP_NOTCH_QA`). Invented data only; nothing contacts the service.
The strip at the top of each notch page is the real `OverlayView` header, mounted and cropped (`NotchQAIdeate.headerImage`).
Setup pages are the real `FirstRunSetupView` on its `.customize` step. The Setup permissions step (`onboarding-2-permissions.png`) and Brief's access rows come from the same renderer.

## Render recipe

```
# all of app/Sources/WispApp (incl. main.swift) with -D WISP_NOTCH_QA, then, with network denied:
swiftc -disable-sandbox -swift-version 5 -D WISP_NOTCH_QA -target arm64-apple-macosx14.0 <sources> -lsqlite3 -o ideate-qa
sandbox-exec -p '(version 1) (allow default) (deny network*)' env WISP_NOTCH_QA_IDEATE=1 WISP_QA_OFFSCREEN=1 WISP_NOTCH_QA_OUT=<dir> WISP_HOME=<scratch> ./ideate-qa
sandbox-exec -p '(version 1) (allow default) (deny network*)' env WISP_NOTCH_QA_ONBOARDING=1 WISP_QA_OFFSCREEN=1 WISP_NOTCH_QA_OUT=<dir> WISP_HOME=<scratch> ./ideate-qa
```

Run the binary under `sandbox-exec` with network denied: mounting `OverlayView` refreshes against the service otherwise.
Name the binary something other than the app so its saved choices (`UserDefaults`) never touch the real app's; both drivers clear their own domain at the end.

Sources: `app/Sources/WispApp/NotchQAIdeate.swift` (Ideate, strip, Extra tabs row), `NotchQAOnboarding.swift` (You step; `youScenes`).
Ideate fixture: `IdeateModel.qaFixture(kind:state:)` shown in `NotchIdeateView(ideate:cap:)`.

## Ideate tab (`NotchQAIdeate.ideatePages`)

Each is `NotchIdeateView(ideate: IdeateModel.qaFixture(kind: K, state: S), cap: room)` under the header with Ideate selected.

- `ideate-general-empty.png`, `ideate-ui-empty.png`, `ideate-debug-empty.png`, `ideate-learn-empty.png`, `ideate-script-empty.png`: the home page for each kind (`.empty`): its prompt, three starters, recent projects.
- `ideate-ui-running.png`, `ideate-debug-running.png`: `.running`, the timeline and a Stop button.
- `ideate-ui-approval.png`, `ideate-debug-approval.png`: `.approval`, an inline amber card "Ideate wants to run `npm install`" with Allow once and Don't allow.
- `ideate-ui-done.png`, `ideate-debug-done.png`: `.done`, the files made and Open folder, Open preview, New idea.
- `ideate-general-failed.png`: `.failed`, one soft-red line and Try again.
- `ideate-general-unavailable.png`: `.unavailable`.
- `ideate-general-model-swap.png`: `.modelSwap`, the note that Ask's model steps aside while Ideate loads its own.
- `ideate-general-gpu-low.png`, `ideate-general-gpu-command.png`: the GPU-limit row, low and with the command shown (`ideatePages`, `.general`).
- `ideate-general-needs-link.png`: `.needsLink`, Inkling 2 Mini is on this Mac but not in the engine's folder; "Use the copy on this Mac" clones it in (APFS clone, no extra disk).

## Tab strip (`NotchQAIdeate.headerImage`, real `OverlayView` header)

- `tabs-ask-brief-day-ideate-study.png`: `WildcardTabs.set([.ideate, .study])`, Ideate selected.
- `tabs-ask-brief-day-meet-ideate.png`: `WildcardTabs.set([.meet, .ideate])`, Ideate selected.
- `tabs-ask-brief-day-only.png`: `WildcardTabs.set([])`, Ask selected.

## Quick settings

- `settings-tabs-row.png`: `QuickSettingsView(section: .general)` with Ideate and Study on: the "Extra tabs" row. Title, "Add up to 2 tabs next to Ask, Brief and Day", then "Ideate and Study are on · 2 of 2". Picked chips are filled with a checkmark, free ones are outlines with a plus, and Meet is dimmed at the two-tab cap. Below it, the "Ideate is set up for" row.
- `settings-tabs-row-blocked.png`: the same, with Meet pressed at the cap: the amber note "Remove one to add Meet" under the chips (the press state is seeded with `tabsBlockedNote`; hovering shows the same text as a tooltip). The words and the press decision live in `WildcardTabs` (`rowTitle`, `rowSubtitle`, `stateLine`, `countText`, `blockedNote`, `press`).

## Setup, "You" step (`FirstRunSetupView(initialStep: .customize, qaFixture: true)`, 640 pt wide)

- `onboarding-you-personas.png`: nothing chosen yet, the seven personas.
- `onboarding-you-builder-subtypes.png`: `HarborPersonaStore.choose(.builderUI)`, "I build things" open with its five subtypes, and the Extra tabs card (the strip, then "Ideate is on · 1 of 2").
- `onboarding-you-change-list.png`: `choose(.builderLearn)` (Ideate and Study), Change pressed by a real click: the checklist with Meet dimmed, "Ideate and Study are on · 2 of 2", the Ideate variants, Study's cloud disclosure and Ideate's folder note.
- `onboarding-you-change-list-blocked.png`: the same, then the dimmed Meet row pressed by a real click: "Remove one to add Meet" appears under the list.

## Key excerpts

```swift
// Ideate pages
let model = IdeateModel.qaFixture(kind: .ui, state: .approval)
NotchIdeateView(ideate: model, cap: screen.pageContentCap(headerHeight: header))

// Strip: choose tabs, mount the real header
WildcardTabs.set([.ideate, .study])
OverlayView(model: model, corpus: ..., researchModel: ..., todayModel: today, settings: ..., setup: ..., onClose: {})

// You step
HarborPersonaStore.choose(.builderLearn)   // sets tabs [.ideate, .study] and the Ideate kind
FirstRunSetupView(setup: s, knowYou: k, google: g, initialStep: .customize, openDetailedSetup: {}, finish: { _ in }, qaFixture: true)
```

## Setup asks for two permissions; the rest at first use

Setup's Permissions step lists Calendar and Reminders only (`FirstRunPermissions.Item.setup`), with a footnote saying the rest are asked when a task needs them. Mail & Notes and Full Disk Access stay in Settings › Privacy and on the notch permission page.

- `onboarding-2-permissions.png`: `FirstRunSetupView(initialStep: .permissions, qaFixture: true)` (`NotchQAOnboarding`, `WISP_NOTCH_QA_ONBOARDING=1`). The two rows show whatever macOS says to the QA binary; nothing is asked.

Brief's quiet rows (`BriefAccessRows`, `NotchQAIdeate.briefRows`, `WISP_NOTCH_QA_IDEATE=1`): the real header with Brief selected, `BriefAccessRowsView` on `BriefAccessRowsModel.qaFixture(...)` (fixed rows, reads nothing from this Mac), and one invented sentence under them.

- `brief-rows-allow-mail.png`: "Allow Wisp to read Mail" with Allow…. Its button is the only thing that asks for Mail & Notes Automation. Shown while Automation is undecided, Google isn't covering mail and the Envelope Index can't be read.
- `brief-rows-see-texts.png`: "See your texts" with Allow…, which opens the notch permission page on Full Disk Access. Shown when the live probe says Messages can't be opened.
- `brief-rows-all.png`: all three, the third being "See your mail and notes when they're closed" (Mail and Notes closed, stores unreadable).

Every row has a small x that hides it until tomorrow. Day shows the same rows.
