# Handoff — Apple-guided Wisp UI exploration

## Delivered

A source-grounded design plan and audit, an offline interactive prototype, and 14 exported high-fidelity PNGs. Covers notch idle/processing/approval, non-notched display, standard conversation/research/memory window, settings, search, light/dark, narrow window, 200% reading text and accessibility appearance. Keeps the black notch silhouette, white orb and monochrome hierarchy. All simulated content and proposed behavior are labeled.

## Files changed

Only new files under `docs/design/apple-ui-refresh/`:

- `PLAN.md`, `AUDIT.md`, `README.md`, `VALIDATION.md`, `HANDOFF.md`.
- `prototype/index.html`, `prototype/style.css`, `prototype/app.js`.
- `exports/01-notch-idle.png` through `exports/14-settings-light.png` (the README links all 14 individually).
- `verify.cjs`, `validation-results.json`.

No production Swift/AppKit/backend/packaging files or Local checkout files changed.

## Checks and failures

14 exported renders; visual inspection; recorded browser assertions covering interactions, keyboard focus, control sizing, four viewport widths, local links/assets, reduced motion, reading-size reflow, contrast and runtime/network errors. JS syntax and Git whitespace/scope checks passed. See [VALIDATION.md](VALIDATION.md) for exact scope and resolved failures. Initial Git ref creation and Chrome launch were blocked by the filesystem sandbox, then succeeded with approved escalation. No unresolved artifact failure is known. Native and service suites were not run because production was untouched.

## Incomplete work and risks

Production implementation is intentionally out of scope. Native AX/VoiceOver, hardware positioning, system window behavior and cross-surface session/task migration are not validated by a browser mockup. Standard workspace navigation is a proposed new behavior: current research is normally inside the notch. The existing generic permission grant and richer memory review semantics must be preserved in any implementation. See the plan for native acceptance gates and the validation document for fixture limitations.

## Integration

Artifact-only changes have no runtime dependencies. A later implementation must coordinate ownership of Theme, OverlayPanel/View/Model, AppDelegate, ResearchWindow/View, MemoryView, SettingsView and SearchPanel/Model. In particular, screen geometry, activation/close behavior, approval payload binding and shared session ownership should be designed together. Avoid merging this as a production UI implementation.

Branch: `codex/apple-ui-refresh-concepts`. Base: `c3b5afe26a1744dd7927586262be0533322443af`. The containing Git commit identifies this deliverable; exact final hash and remote verification appear in the task completion message (avoids a self-referential hash inside its own commit).

Review: [branch comparison](https://github.com/adjad/wisp/compare/c3b5afe26a1744dd7927586262be0533322443af...codex/apple-ui-refresh-concepts). Branch pushed without force. No merge, deployment or archive requested or performed.
