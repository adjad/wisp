# Wisp UI exploration

**Conceptual design artifacts — no production UI behavior changed.**

Start with [the design plan and official Apple references](PLAN.md), then open [the interactive prototype](prototype/index.html) in a browser. Double-clicking `prototype/index.html` works offline; there are no installation steps, remote fonts, libraries, assets or service connections. All names, messages, memories and research passages are synthetic.

The reviewer controls above the desktop switch surfaces, states, appearance, display shape, contrast and material behavior. They are not proposed Wisp product chrome. Notch → Open window opens a fixed conversation concept; it demonstrates visual navigation, not the state transfer proposed in the plan. Workspace navigation opens Research or Memory; Settings has category search and local preference controls. Search supports query filtering, arrows and Return. Approval drafts are editable; “Send this email” changes fixture state only. Use state choices to advance to Approval: there is no timed fake processing completion.

Workspace supports 1120/720 pt review presets and dragging its lower-right corner. CSS pixels approximate logical points at the default review scale; these are browser windows, not NSWindows. 200% reading text can be tested from the width controls. The fixed sample traffic lights are decorative; real production would use native window controls. Global keyboard shortcuts, native activation, Spaces and hardware notch positioning are specification only.

## Visual review

PNG exports include the surrounding review controls and concept labeling. The notch idle export shows actual keyboard focus; approval focuses “Not now”.

| Export | What to review |
| --- | --- |
| [01 · Notch idle](exports/01-notch-idle.png) | Silhouette, entry point, visible input focus |
| [02 · Processing](exports/02-notch-processing.png) | Progress without invented percentage, Stop |
| [03 · Approval](exports/03-notch-approval.png) | Account/recipient/draft, scoped explicit action |
| [04 · External display](exports/04-external-display.png) | Detached panel below menu bar, no camera cutout |
| [05 · Conversation](exports/05-workspace-conversation.png) | Standard window, reading measure, source inspector |
| [06 · Research](exports/06-workspace-research.png) | Reading/evidence hierarchy |
| [07 · Memory](exports/07-workspace-memory.png) | Saved facts and provenance |
| [08 · Settings](exports/08-settings.png) | Categories, grouped rows, quieter defaults |
| [09 · Search](exports/09-search.png) | Scope, query, passages and selected result |
| [10 · Light window](exports/10-workspace-light.png) | Opaque reading content and adaptive chrome |
| [11 · Narrow window](exports/11-workspace-narrow.png) | Inspector collapses, content remains readable |
| [12 · Accessible appearance](exports/12-accessible-appearance.png) | More contrast, opaque materials, Reduce Motion |
| [13 · Large reading text](exports/13-reading-large.png) | 200% conversation text, scrollable reading surface |
| [14 · Light settings](exports/14-settings-light.png) | Appearance preferences in a light window |

See [source audit](AUDIT.md), [validation](VALIDATION.md), and [handoff](HANDOFF.md).

## Reproduce exports and checks

Requires Node.js, Playwright and an installed Google Chrome. The prototype itself needs none of these. The script resolves its input/output relative to its own location and runs offline via `file:` URLs. It launches a fresh temporary browser profile, never the user’s profile.

```sh
NODE_PATH=/path/to/node_modules node docs/design/apple-ui-refresh/verify.cjs
```

For this Codex host, packages were at `/Users/adijain/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules`. Verification regenerates PNGs and `validation-results.json`. Native validation remains a future implementation gate, not part of this artifact’s claims.
