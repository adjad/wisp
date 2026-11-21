# Ideate and Customize Wisp mockups

Real SwiftUI, rendered by the notch QA renderer at 2x (`WISP_NOTCH_QA`). Invented data only; nothing contacts the service.
The strip at the top of each notch page is the real `OverlayView` header, mounted and cropped (`NotchQAIdeate.headerImage`).
Setup pages are the real `FirstRunSetupView` on its `.customize` step.

## Render recipe

```
# all of app/Sources/WispApp (incl. main.swift) with -D WISP_NOTCH_QA, then, with network denied:
swiftc -disable-sandbox -swift-version 5 -D WISP_NOTCH_QA -target arm64-apple-macosx14.0 <sources> -lsqlite3 -o ideate-qa
sandbox-exec -p '(version 1) (allow default) (deny network*)' env WISP_NOTCH_QA_IDEATE=1 WISP_QA_OFFSCREEN=1 WISP_NOTCH_QA_OUT=<dir> WISP_HOME=<scratch> ./ideate-qa
sandbox-exec -p '(version 1) (allow default) (deny network*)' env WISP_NOTCH_QA_ONBOARDING=1 WISP_QA_OFFSCREEN=1 WISP_NOTCH_QA_OUT=<dir> WISP_HOME=<scratch> ./ideate-qa
```

Run the binary under `sandbox-exec` with network denied: mounting `OverlayView` refreshes against the service otherwise.
Name the binary something other than the app so its saved choices (`UserDefaults`) never touch the real app's; both drivers clear their own domain at the end.

Sources: `app/Sources/WispApp/NotchQAIdeate.swift` (Ideate, strip, Tabs row), `NotchQAOnboarding.swift` (You step; `youScenes`).
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
- `ideate-general-needs-link.png`: `.needsLink`, Inkling 2 Mini is on this Mac but not in the engine's folder; "Use the copy on this Mac" clones it in (APFS clone, no extra disk).

## Tab strip (`NotchQAIdeate.headerImage`, real `OverlayView` header)

- `tabs-ask-brief-day-ideate-study.png`: `WildcardTabs.set([.ideate, .study])`, Ideate selected.
- `tabs-ask-brief-day-meet-ideate.png`: `WildcardTabs.set([.meet, .ideate])`, Ideate selected.
- `tabs-ask-brief-day-only.png`: `WildcardTabs.set([])`, Ask selected.

## Quick settings

- `settings-tabs-row.png`: `QuickSettingsView(section: .general)` with Ideate and Study on: the Tabs row (Study, Meet, Ideate chips, Meet dimmed at the two-tab cap) and the "Ideate is set up for" row.

## Setup, "You" step (`FirstRunSetupView(initialStep: .customize, qaFixture: true)`, 640 pt wide)

- `onboarding-you-personas.png`: nothing chosen yet, the seven personas.
- `onboarding-you-builder-subtypes.png`: `HarborPersonaStore.choose(.builderUI)`, "I build things" open with its five subtypes, and Your tabs (Ask, Brief, Day, Ideate).
- `onboarding-you-change-list.png`: `choose(.builderLearn)` (Ideate and Study), Change pressed by a real click: the checklist with Meet blocked, "Two at most", the Ideate variants, Study's cloud disclosure and Ideate's folder note.

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
