# Validation

Completed 9 September 2026. Reproducible details: [validation-results.json](validation-results.json); runner: `verify.cjs`.

## Completed

- Rendered 14 PNG exports in isolated headless Google Chrome at a 1440×1200 CSS-pixel viewport, device scale 1. Inspected the exports for hierarchy, clipping, control spacing, camera clear area, focused inputs, light/dark surfaces and narrow layouts.
- Verified prototype transitions: multiline composer, processing/Stop, edited approval/Return/Escape/defer/resume/approve, sidebar keyboard focus, memory edit completion, settings filtering/empty results/preference consistency, source drawer at narrow width, source close at wide width, and search query/arrows/Return/empty results.
- Checked page fit at 1440, 1024, 800 and 600 px for all four surfaces. Smaller views are browser reflow tests, not claims that native Wisp supports windows below the proposed 720 pt minimum.
- Confirmed system `prefers-reduced-motion` and the additional review preference stop processing animation. Checked opaque light menu colors, large reading text reflow and all visible surface button targets at ≥32×32 px.
- Verified HTML/CSS/JS dependency paths and local Markdown links. All assets are local or inline. No external requests and no browser runtime errors during verification.
- `node --check` for prototype and verifier; Git whitespace/scope checks. No Swift/backend tests run because all modifications are design artifacts.
- Five base palette text contrast checks pass 4.5:1: notch secondary 10.65, dark secondary 7.95, light secondary 5.36, dark primary 16.02, light primary 14.69. These are flat palette measurements, not exhaustive pixel sampling of simulated materials. More Contrast makes boundaries stronger.

## Problems found and resolved

- Initial branch creation was blocked by filesystem sandboxing because Worktree refs live in the shared Git directory. Authorized escalation succeeded; Local checkout files were not changed.
- Initial localhost preview bind was blocked by the sandbox and required approved escalation. Initial sandboxed Chrome launch failed with SIGABRT/EPERM. An approved isolated browser launch succeeded; no browser-profile reuse.
- Review found a memory Done handler collision, keyboard focus loss on rendering, overly broad search key handling, inaccessible hidden evidence at narrow width, and contradictory search scope. Corrected before final verification.
- Initial renders clipped the email signature and left the workspace composer below the first viewport. Increased the draft review area and kept the workspace composer outside the scrolling transcript.
- Visual QA caught large-text agenda overlap and low-contrast menu text in the opaque light variant. Corrected and added targeted checks.

No known blocking artifact failures remain. The count and full assertion list from the final run are recorded in `validation-results.json`.

## Limits and incomplete production work

This deliverable is a design exploration. Native NSWindow traffic lights, drag/resize physics, titlebar materials, focus return to other apps, camera geometry, multiple screens, menu-bar hiding, Stage Manager/Spaces/full screen, VoiceOver announcements/rotor, Full Keyboard Access and system accessibility API integration were **not** tested. Browser semantic controls and focus checks do not certify native accessibility.

The prototype uses synthetic local content, fixed conversations and simplified transitions. Open window demonstrates visual navigation, not live task/session migration. Memory edits last only in the rendered fixture, while appearance controls last for the page session. Settings category grouping includes proposals, not all production settings. Persistent tool grants, research-running/error paths, memory review/supersession/forget, Smart Search partial failures and streaming scroll behavior remain specified future work. Ordinary hides and native invoker focus restoration are design intent; the browser cannot reproduce OS activation.

Reading-size simulation doubles selected conversation/report body roles, not the entire interface. Full app text enlargement, localization/RTL, actual hardware and native assistive input require production validation. Research source numbers refer to fictional passages in this fixture, not real research findings. Apple guidance is cited separately in the plan.
