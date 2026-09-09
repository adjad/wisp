# Source audit

Read-only at `c3b5afe`. Paths below are relative to `app/Sources/WispApp/`. No live-app inspection or native accessibility certification was performed.

| Surface | Evidence | Implication |
| --- | --- | --- |
| Identity | `Theme.swift:3–31` | Pure black, gray/white, diffuse orb, 28 pt bottom corners. Preserve the notch silhouette. |
| Panel | `OverlayPanel.swift:39–54,147–160`; `SearchPanel.swift:4–9` | 640 pt shared width, borderless/nonactivating, status-bar level on notched screens, floating otherwise. Shared geometry is load-bearing. |
| Display variants | `OverlayPanel.swift:133–143`; `OverlayView.swift:43–47` | Content sits below the camera; non-notched compact fallback is a 160×40 pill below the menu bar. Show both variants. |
| Header density | `OverlayView.swift:107–188` | Research, Daily Summary, AM/PM and custom controls compete. Custom controls are 19×19; AM/PM targets 22×18. Simplify and increase hit regions. |
| Type | `OverlayView.swift:216–247,263,324–328,570–582`; `SearchPanel.swift:74–96` | 19 pt composer; fixed 9–12 pt metadata/actions; transcript capped at 360 pt. Establish legible roles and move extended work to a window. |
| Approval | `OverlayView.swift:431–557`; `OverlayModel.swift:7–46` | Current permission cards include reason, scope, code preview, Deny, Always allow and Allow once. Message drafts have review/edit flows. Do not flatten persistent grants into a generic confirm. The concept illustrates a one-email approval only. |
| Research | `AppDelegate.swift:561–565`; `OverlayView.swift:29–31`; `ResearchView.swift:6–8`; `ResearchWindow.swift:30–38` | Normal flow uses compact notch research; standalone controller is developmental, 900×720 with 720×560 minimum. Unified workspace is new proposed behavior. |
| Memory | `MemoryView.swift:133–138,160–207,262–327` | Existing 780×650 resizable memory window, min 650×500; saved/proposed facts, evidence, correction flows. Reuse these distinctions; concept shows saved facts, not the entire review workflow. |
| Settings | `AppDelegate.swift:547–555`; `SettingsView.swift:103–120,332` | Fixed 560×620 scrolling disclosure groups; Models expanded, Automation/Privacy/Advanced collapsed. Search/category grouping proposed. |
| Search shortcuts | `AppDelegate.swift:62–72,98–105,152–172` | ⌥Space, ⌘⇧F; contextual capture before activation. Arrows navigate, Return advances the hit, ⌘Return asks anyway. Prototype’s Return views the selected passage: deliberate simplified concept behavior, not current parity. |
| Focus/accessibility | `OverlayView.swift:12–22,68,181–191,642–653`; `SearchPanel.swift:12,47,62` | Fields receive focus; Escape collapses. Many icon tooltips, few explicit AX labels; SourceSyncTip has a grouped VoiceOver treatment. Audit is not proof controls are inaccessible; runtime testing needed. |
| Motion | `OverlayView.swift:194–201,403–405,604–619`; `OverlayPanel.swift:208–228` | Repeating orb/progress effects and spring reveal; no Reduce Motion handling found in app Swift sources. Add stationary status and reduced transitions. |
| Lifecycle | `AppDelegate.swift:40,301–313,416–449` | Hover/key-loss collapse after 2 seconds; close dismisses, clears chat and unloads model. Proposed “hide preserves work” needs intentional lifecycle changes. |

## Future implementation risks

- Panel geometry prefers the first notched screen; SearchView uses `NSScreen.main`; chat metrics are initialized once (`OverlayModel.swift:181`). Test display changes and active-screen selection together.
- Coordinate OverlayPanel, OverlayView and SearchView width/safe-area changes. Never independently tune one hardcoded width.
- Standard conversation window needs shared model/session ownership, draft transfer and reopening behavior. Close must be distinguished from clear/unload.
- Preserve source-capture ordering, permission scope, memory evidence/corrections, streaming size behavior and research job continuity.
- Native tests must cover Full Keyboard Access, VoiceOver, Reduce Motion, Increase Contrast, Reduce Transparency, real display hardware, Spaces/full screen and restoration. Browser artifacts cannot prove these.
