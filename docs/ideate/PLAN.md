# Ideate + Customize Harbor — plan and decision log

Base: Wisp `claude/harbor-0.1.8` b261fce8 (installed 0.1.8, build 54). Branch `claude/harbor-ideate`.
Harbor stage branch: local `stage-ideate` from `stage-0.1.8` 1ed0716. Never pushed, never published.
Version stays 0.1.8 (pipeline patch 0019). Preview staged as `/Applications/Harbor.app.new` only.

## Outcome

1. **Ideate** wildcard tab: a small coding harness in the notch. You type an idea, it builds a small
   project in its own folder, streams what it does, and asks before anything risky.
2. **Customize Harbor**: a "Who are you?" onboarding step that picks one or two wildcard tabs
   (Study, Meet, Ideate) and, for ideators, the Ideate variant. Changeable later.
3. **Ask routing**: a complex coding request in Ask goes to Ideate (when Ideate is on); a minor
   coding question stays in Ask.
4. **Mockups** as real SwiftUI, rendered by the notch QA renderer, in `docs/ideate/mockups/`.
5. A preview build with Simulation QA, staged as `Harbor.app.new`.

## Work split (one writer per file)

| Piece | Owner | Files |
| --- | --- | --- |
| Research: harness static | Sonnet R1 | `docs/ideate/harness-static.md` |
| Research: live model + harness bench (only agent on oMLX) | Sonnet R2 | `docs/ideate/model-eval.md`, `harness-live.md`, `bench-results.json` |
| Research: wildcard tabs + personas | Sonnet R3 | `docs/ideate/wildcard-tabs.md` |
| Ideate tab UI + client model + variants | Sonnet B1 | `NotchIdeateView.swift`, `IdeateModel.swift`, `IdeateKind.swift`, `tests/IdeateChecks.swift` |
| Harness bridge service + bundling | Sonnet/Opus B2 | `service/ideate/**`, `service/main.py` (one router line), `tests/test_ideate_*.py`, build-support bundling + validation |
| Customize step + wildcard strip | Sonnet B3 | `NotchTab.swift`, `WildcardTabs.swift`, `Onboarding*.swift`, `OverlayView.swift`, `QuickSettings*`, `tests/WildcardTabsChecks.swift`, onboarding tests |
| Ask routing | Sonnet B4 | `IdeateRouting.swift`, `tests/IdeateRoutingChecks.swift`, the submit hook in `OverlayModel.swift` |
| QA render pages | Sonnet B5 (after B1/B3) | `NotchQAPages.swift`, `NotchQA.swift`, `NotchQAIdeate.swift` |
| Compile lists, Simulation QA manifest, integration, review | Orchestrator | `scripts/run_simulation_qa.py`, `scripts/test_first_run_setup.sh`, `build-support/simulation.py` |

## Contracts fixed up front

- `IdeateKind` (`general`, `ui`, `debug`, `learn`, `script`) is shared with the service by raw value.
- `NotchTab.ideate` is a new case appended at the end (raw values of existing tabs unchanged).
- `IdeateModel.shared.start(prompt:kind:)` starts a project; Ask routing calls it and selects `.ideate`.
- Service API under `/ideate` (B2 implements, B1 consumes):
  - `GET  /ideate/status` → `{available, reason, harness, model, model_loaded, projects_root}`
  - `GET  /ideate/projects` → `{projects:[{id,name,kind,status,created_at,updated_at}]}`
  - `POST /ideate/projects` `{prompt, kind, name?}` → project, starts a run
  - `GET  /ideate/projects/{id}?after=<seq>` → project + `events` after seq + `files` + `approvals`
  - `POST /ideate/projects/{id}/message` `{text}` · `POST /ideate/projects/{id}/cancel`
  - `POST /ideate/projects/{id}/approvals/{approval_id}` `{allow: bool}`
  - Event: `{seq, kind: text|thinking|tool|approval|file|done|error, text, tool, path, status, ts}`
  - Status: `idle|starting|running|waiting|done|failed|cancelled`

## Decision log

- **D1 Base and isolation.** Own worktree from b261fce8; 0.1.7/0.1.8 branches, h017 worktrees and main untouched.
- **D2 Strip size.** Ask · Brief · Day + at most two wildcards (same five-tab width as today). Never-asked
  installs keep Meet + Study, so nothing changes for the current user until they pick.
- **D3 Onboarding place.** "Customize" goes after About you and before How to use, so the tour shows the
  tabs you picked. A saved raw step of the old tour lands on Customize (one extra screen, harmless).
- **D4 Routing is local and pure.** A Swift classifier, no model call, so Ask stays instant. It routes only
  when Ideate is in the strip; otherwise Ask answers as before. "Build/make/scaffold … app/site/script"
  with a multi-part spec routes; "what does this error mean", "regex for …", one-liners stay.
- **D5 Model residency.** Inkling 2 Mini is ~13–16 GB. Ideate loads it only when a run starts and lets it go
  after the run idles, so Ask's model can come back; the tab says so. (Details from B2.)
- **D6 Safety.** Harness runs with cwd = its project folder under Harbor's data folder, a `sandbox-exec`
  profile that allows writes only there, HOME/XDG pointed inside it, and every shell command and every
  write outside the project routed through Harbor's approval layer (`service/safety`). Nothing is auto-run
  that Harbor would ask about.
- **D7 Model (user, relayed 2026-11-21).** Inkling 2 Mini **oQ3e MTP** is Ideate's model and the test model;
  the oQ3e vs oQ4e comparison was dropped at the user's word ("test with the oq3e mtp inkling 2 mini model not oq4e").
- **D8 Swap (user, relayed 2026-11-21).** "swap in once done": once the preview passes its checks and Simulation
  QA, back up `/Applications/Harbor.app` as `.Harbor-backup-0.1.8-54-<stamp>.app`, move `Harbor.app.new` in,
  launch by path, then test Ideate end to end with oQ3e (one model resident). No swap if checks fail.
- **D9 Harness: Pi** (`--mode rpc`, headless child process). Evidence: harness-static.md (stdio JSONL, no port/db,
  `tool_call` extension hook for approvals, 126 MB idle, 0.14 s ready, no egress with offline env) and harness-live.md
  (on oQ3e: Pi 3/4 vs OpenCode 3/4, but Pi 135–160 MB vs 400–425 MB, first event 0.15 s vs 4–6 s; OpenCode's default
  prompt was rejected on turn one, writes via heredocs, takes cwd from PWD, needs a port + password + shared SQLite db).
  OpenCode stays the fallback behind the bridge's adapter interface.
- **D10 Engine prefill ceiling (blocker, not changed).** With oQ3e resident (15.1 GB), oMLX's prefill guard (limit
  19.95 GB, ceiling 21 GB) rejects prompts over ~1.5–2k tokens. Raising `custom_ceiling_bytes` is the user's engine
  setting, so Ideate ships a lean profile instead: short system prompt, 4 tools, ~1.4k-token context budget with
  truncation/compaction, and an honest "ran out of memory room" error. This limits Ideate to small, few-step builds
  until the user raises the ceiling. Question for the user at the end.
- **D12 Engine re-pin to oMLX 0.8.2.** The installed Harbor runs its bundled engine (0.7.3), which cannot run
  Inkling 2 (`inkling2_loop` support arrived in oMLX 0.8.x). The user updated /Applications/oMLX.app to 0.8.2 on
  2026-11-20, so the Harbor pipeline's engine-guard check fails (4 of 11 patches) and no 0.7.3 copy is left on disk.
  Per the user's standing rule ("use my version of oMLX as the bundled version"), the preview re-pins the bundled
  engine to 0.8.2 and ports the guard patches (docs/ideate/engine-0.8.2-repin.md). Needs a Release Auditor before
  any release; the DMG pins can't be verified without a download.
- **D13 Model folder.** The bundled engine sees only its own models folder. Ideate offers "Use the copy on this
  Mac": an APFS clone (no extra disk) of the Inkling folder from the oMLX folder into Wisp's engine models folder.
- **D11 Tabs.** wildcard-tabs.md: of 16 candidates only Clips is BUILD LATER; the rest fold into Ask/Brief/Day/Study/
  Meet/Ideate or drop. Ships with Study, Meet, Ideate as the only wildcards.
