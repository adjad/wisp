# Wisp / A little presence. Room to think.

Design exploration · 9 September 2026 · **Concept only, not production behavior**

## Scope and sequence

Base: `origin/main` at `c3b5afe26a1744dd7927586262be0533322443af`. Branch: `codex/apple-ui-refresh-concepts`. Own only new files beneath `docs/design/apple-ui-refresh/`. No production Swift, AppKit, backend, packaging, or Local checkout changes.

1. Audit the current surfaces and resolve the design constraints below.
2. Build a dependency-free, standalone browser prototype with fixture content; no Wisp connection, external requests, or real send/approval operations.
3. Export readable PNGs of notch idle, processing and approval, conversation/research/memory windows, settings and search, including appearance and display variants.
4. Inspect the actual renders; check local assets, keyboard paths, state transitions, resizing, contrast and reduced motion. Record native-only validation gaps explicitly.
5. Commit and push the artifact branch for review. No merge, deployment or archive.

## Concise current-surface audit

Wisp's identity is intentional: pure black, white/gray labels, a soft radial orb, and bottom-only 28 pt notch corners (`app/Sources/WispApp/Theme.swift`). Keep these, especially the hardware-fused silhouette. The existing reveal mask expands without scaling the content (`OverlayPanel.swift`); that is a useful continuity cue.

The overlay is a borderless nonactivating panel, elevated to status-bar level on notched screens and floating otherwise. It joins Spaces and full-screen contexts. This geometry and focus machinery is load-bearing; a visual refresh must not casually replace it. Expanded content currently mixes chat, compact research, memory, sessions, activity and approval. Small fixed type and icon-only actions create a hierarchy and discoverability opportunity.

Research normally opens within the overlay (`AppDelegate.swift:561–565`, `OverlayView.swift:29–31`). `ResearchWindow.swift` has a 900×720 resizable window with a 720×560 minimum, but `ResearchView.swift:6–8` describes it as a development surface. A unified standard window is **proposed new navigation**, not a claim about current shipping behavior. Settings and Smart Search are separate surfaces; preserve their familiar entry points while giving them the same typography, spacing and selection language.

This is a source audit, not a live-app accessibility certification. See [AUDIT.md](AUDIT.md) for evidence and integration cautions.

## Apple grounding and Wisp decisions

Official sources checked 9 September 2026. Apple principles are summarized here; all exact dimensions, timings and navigation choices below are Wisp proposals rather than Apple mandates.

| Official Apple guidance | Proposed Wisp interpretation |
| --- | --- |
| [Designing for macOS](https://developer.apple.com/design/human-interface-guidelines/designing-for-macos): support sustained work, multiple windows/displays and keyboard workflows. | Two connected scales: the notch for quick requests and a standard window for longer reading. Preserve one conversation identity during transfer. |
| [Layout](https://developer.apple.com/design/human-interface-guidelines/layout): adapt to available space and keep important content outside the camera housing. | Camera area is empty black. All controls sit below its safe area. Use screen geometry, never a hardcoded camera width. On non-notched displays use a detached panel below the menu bar, rounded on all corners. |
| [Typography](https://developer.apple.com/design/human-interface-guidelines/typography): system fonts, legible sizes and control-specific variants; macOS default 13 pt, minimum 10 pt; macOS does not support Dynamic Type. | Semantic system roles, 13 pt UI, 15 pt conversation body and 12 pt metadata. Offer app-level reading size up to 200%; reflow rather than shrink. Do not claim automatic iOS-style Dynamic Type on Mac. |
| [Accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility): adequate contrast, keyboard navigation, assistive technology and no unnecessary timed dismissal. | Target ≥4.5:1 for all text and ≥3:1 for focus/control boundaries. Keep approval open until resolved or explicitly hidden; preserve it when hidden. Label icons and statuses, never rely on brightness or motion alone. |
| [Motion](https://developer.apple.com/design/human-interface-guidelines/motion): brief, purposeful, optional transitions. | Retain anchored reveal, target 180–220 ms without bounce or text scaling. Reduce Motion: immediate state changes, stationary status symbol, no shimmer or perpetual orb pulse. |
| [Keyboards](https://developer.apple.com/design/human-interface-guidelines/keyboards): respect standard shortcuts and Full Keyboard Access. | Retain ⌥Space and ⌘⇧F, keep ⌘F scoped to find, propose ⌘, for Settings and normal ⌘W window close. Tab follows reading order; focus ring always visible for keyboard use. |
| [Windows](https://developer.apple.com/design/human-interface-guidelines/windows): system window chrome and familiar resizing/relocation. | Native titled resizable window with traffic lights, standard toolbar, sidebar and inspector. Closing a window does not stop an active task. No floating window level for the workspace. |
| [SF Symbols](https://developer.apple.com/design/human-interface-guidelines/sf-symbols): match text weight/scale and check OS availability. | Use standard symbols for search, stop, sidebar, settings and open-window actions. Keep the custom orb as Wisp's identity. Browser mockups use original line glyph approximations, not redistributed SF assets. |
| [Materials](https://developer.apple.com/design/human-interface-guidelines/materials): choose materials by semantic purpose, not sampled color; distinguish navigation from content. | Solid black notch, opaque reading canvas, sidebar/toolbar material only. Use native system controls to adopt current OS styling; no fabricated glass on every message. Light/dark follow the system outside the notch. |
| [Liquid Glass overview](https://developer.apple.com/documentation/technologyoverviews/liquid-glass): standard controls/navigation provide platform consistency. | Wisp currently targets macOS 14 (`app/Package.swift`). Gate newer material APIs by OS; retain standard materials on older systems. CSS blur is illustrative, not a native material implementation. |

## Surface plan

### 01 · Notch: presence without distraction

Expanded nominal width 640 pt, content below the camera safe area; height follows the state with a bounded scroll region. A compact black bridge keeps the silhouette joined to the hardware. Do not occupy menu items or camera pixels with controls. Offer a menu-bar entry and keyboard entry; hover is convenience, never the only route. The standalone browser desktop is a schematic and cannot validate physical camera geometry.

- Idle: orb + Wisp, Open window and close; a focused multiline composer, two useful starter requests, recent-context hint. Avoid a permanent wall of tools.
- Processing: preserve the original request, show a human-readable operation and completed steps, offer Stop and Open window. No fabricated percentage. Streaming content must not drag the scroll position when the reader scrolls back.
- Approval: separate “Review before sending” from progress. Show exact account, recipient, subject and full editable draft; attach any actual attachment list in production. “Send this email” is explicit; “Not now” leaves pending. Return in a composer never approves. In production, approval must bind to the displayed payload/version; fixture approval in this prototype only changes text.
- Error/denied/canceled: concise state, retained user input, recover or edit action. No automatic retry of a side effect. The prototype includes a canceled state; full error/retry flows remain future work.

### 02 · Workspace: give thought a place

Nominal 1120×740 pt; proposed minimum 720×560 pt. Stable sidebar for Conversations, Research and Memory; one content column with a readable measure, optional source/detail inspector. At narrow widths collapse the inspector first and sidebar second; neither becomes a tiny unreadable column. The prototype has width presets and a drag-resizable frame; future production uses real NSWindow resizing and restores a valid frame on the available display.

Open window transfers the selected session, draft, task/approval state and source context; it never launches a duplicate request. Window toolbar carries key navigation/actions so moving the bottom offscreen does not strand people. Composer can stay at the bottom for conversation convention, with a toolbar “Write” focus action. Research shows readable evidence and source passages rather than raw tool output. Memory shows provenance and explicit correction affordances. Example research, names, dates and memory are synthetic fixtures.

### 03 · Settings and search: one vocabulary

Settings uses stable categories and grouped rows, a search field, clear labels and right-aligned controls. General, Appearance, Connections, Privacy and Models are proposed groupings, not a claim all shown settings exist. Avoid burying policy mode in decoration. Appearance follows System by default; accessibility follows macOS with optional additional reduction, never forces motion on against the system.

Smart Search retains the black notch-fused 640 pt presentation, ⌘⇧F and a clear source/document scope. On non-notched displays it becomes the same detached black panel. Prefer readable result labels and cited answers, with retrieval tiers moved to optional detail. Query, selected result and empty results work locally in the prototype. Production needs distinct “indexing”, “unavailable source”, “no matches” and partial-failure states; do not collapse these into an empty result.

## Shared design specification

| Role | Proposal |
| --- | --- |
| Type | System font (`-apple-system` in prototype). SwiftUI `.body`, `.headline`, `.caption`, `.title2` with explicit readable role tokens; app reading-size preference for long text. UI 13, body 15, metadata 12, headings 20–28 pt; avoid thin weights. |
| Rhythm | 4/8 pt spacing grid, 16–24 pt section padding, 8–12 pt row gaps; restrained separators. |
| Controls | Prototype minimum 32×32 CSS px, primary actions 36–40 px, ≥8 px between neighbors; pointer-first Mac sizing, not a blanket iOS 44 pt assertion. Native hit regions and assistive use require verification. |
| Focus | 2 px high-contrast ring + 3 px offset. Native production should use system focus appearance; it may use the user's accent even though Wisp's surfaces are monochrome. Selection also uses fill and text/shape. |
| Appearance | Notch stays black in both modes. Dark window #171819, light #faf9f6, quiet gray secondary labels. Opaque content; simulated material for navigation only. Increased contrast strengthens borders, reduced transparency removes blur. |
| Symbols | Proposed `magnifyingglass`, `sidebar.left`, `stop.fill`, `arrow.up.right.square`, `gearshape`, `checkmark`, `xmark`; audit availability against macOS 14 before implementation. Decorative symbols hidden from VoiceOver; action names remain accessible. |

## Interaction and accessibility contract

On keyboard opening, move focus to composer; on explicit close return it to the invoker. Hover expansion does not take key focus. Escape hides ordinary panels, preserving drafts; in approval it means Not now, never approval. Outside clicks may hide ordinary content but must preserve pending decisions and must not send. Focus, selection and scroll position survive state transitions and notch/window transfer.

VoiceOver order: surface title/status → user request → response/evidence → action details → decisions → composer. Announce coarse task transitions politely, not every streamed token. Approval gets a titled region and focus at its heading/details; action buttons are reached intentionally. Source rows expose title and provenance; memory exposes fact, origin and last edited date. Use semantic headings, buttons, fields, landmarks and text labels in the HTML as inspectable intent. Native AX groups, announcements, Full Keyboard Access, VoiceOver rotor and actual focus restoration still require native testing.

Respect Reduce Motion and Reduce Transparency; Increase Contrast strengthens boundaries. At 200% reading text, wrap rows, grow or scroll content, and open the workspace if the notch cannot contain it. Test RTL, long translated labels, input methods, multiple displays, display removal, full screen, Stage Manager, hidden menu bar and active app restoration before shipping. Never cover the camera or claim these cases are proven by browser renders.

## Review and eventual integration

First review silhouette, content hierarchy and decision clarity using the exports. Then review the interactive width/appearance/accessibility controls. Only after direction approval, implement semantic tokens and native controls, followed by shared session navigation, then screen geometry/focus behavior. Do not move app-wide state ownership as an incidental styling change.

Likely production dependencies: `Theme.swift`; `OverlayView.swift` and `OverlayPanel.swift`; `AppDelegate.swift` activation/hover/Spaces; `OverlayModel.swift` session and approval state; `ResearchView.swift`/`ResearchWindow.swift`; `SettingsView.swift`; `SearchPanel.swift`/`SearchModel.swift`; `MemoryView.swift`. Those files may belong to other workers and are read-only here. Artifact-only integration has no runtime dependency; future production work conflicts with any concurrent changes to those paths.

See [README.md](README.md) for running/reviewing the artifacts and [VALIDATION.md](VALIDATION.md) for the completed checks and limitations.
