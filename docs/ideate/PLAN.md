# Ideate + Customize Harbor — plan and decision log

Base: the installed 0.1.8 source, b261fce8 (build 54), on its own feature branch.
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

- **D1 Base and isolation.** Own checkout from b261fce8; the 0.1.7 and 0.1.8 release lines and main untouched.
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
- **D14 Engine ceiling (user, 2026-11-21: "raise ceiling to 90% of the devices memory").** The bundled engine moves
  from "aggressive" (dynamic ceiling, 15.9 GB on a 24 GB Mac with apps open) to oMLX's "custom" tier with
  `memory_guard_custom_ceiling_gb` = 90% of physical memory, rounded down to 0.1 GB (21.6 GB here; this Mac's
  `iogpu.wired_limit_mb` is 22528, so the Metal cap doesn't lower it). An earlier Wisp's recorded "aggressive", and
  Wisp's own ceiling when the memory size changes, migrate; a tier or ceiling the user picks in oMLX's admin UI is
  kept. Ideate's 1.4k-token budget is unchanged until it is measured under the new ceiling.
- **D15 Only Inkling while Ideate is in use (user, 2026-11-21: "unload any other harbor model while ideate is is in
  use with inkling").**
  - *Start:* a run waits up to 30 s for an Ask turn that is answering right now, then holds the engine for Inkling
    (`service/inference/engine_hold.py`) and always calls `ensure_only(Inkling, exclusive=True)`, even when Inkling
    is already loaded. On the bundled engine that unloads every other model (Gemma-Wisp and the keep-warm model
    included), waits until they are gone, then loads Inkling.
  - *"No longer in use" rule:* the run ended (done, failed or stopped). Inkling stays 90 s (`RELEASE_AFTER_S`) for a
    follow-up message, then is unloaded unless another run has started. Leaving the tab doesn't end a run (it keeps
    building in the background), so the tab is not the signal. Ask's model is not loaded back; the next Ask, Brief
    or Day turn loads it as usual.
  - *Ask during a run:* neither wait nor swap. While the hold is set, loading or making resident any other model is
    refused at once with a plain message ("Ideate is using Wisp's model for a build right now… Ask again when the
    build finishes, or stop it in Ideate"), before anything is unloaded. Waiting could leave Ask silent for minutes;
    swapping would pull Inkling out mid-run and reload it on Pi's next request. Background work already stands down
    (foreground). During the 90 s grace Ask swaps Inkling out the normal way.
  - Bundled engine only for unloading: on the user's own oMLX.app Wisp never unloads (other apps may use it).
- **D16 GPU memory limit (user, 2026-11-21: "let the user run the terminal command for increasing the memory
  allocation before using ideate").** The bundled oMLX 0.8.2 clamps its ceiling to the Metal cap:
  `iogpu.wired_limit_mb` when it is set, else Apple's `max_recommended_working_set_size` (a smaller share of
  memory). That is the same knob, so the command is `sudo sysctl iogpu.wired_limit_mb=<90% of memory in MB>`
  (22118 on a 24 GB Mac; oMLX warns only above 95%). `IdeateGPULimit` reads the limit and memory without a
  password (sysctlbyname, ProcessInfo) when the tab appears and before a run. If the limit is 0 or below the need,
  the empty tab shows one row: "Ideate needs more memory for the GPU", the reset-on-restart note, "Show the
  command" and "Skip". The command is shown as a `bash` block, so it reaches Terminal only through MarkdownView's
  card ("Run this script in Terminal?", Cancel is the default, a second press runs it in a visible Terminal
  window, where macOS asks for the password). Wisp never runs sudo, never sees the password and never makes it
  permanent. While the command is open the tab re-reads the limit every 3 s, hides the row once it is raised and
  says so once. Skip hides it until Wisp restarts; Ideate then keeps the small budget. This Mac's limit is already
  22528, so the row does not show here.
- **D17 Free memory before Inkling loads (after a crash, 2026-11-21 13:09).** The isolated measurement (a copy of
  the bundled engine on port 8894 with the 21.6 GB custom ceiling) loaded Inkling while the user's own oMLX server
  also held a 12.5 GB model. 13.2 + 12.5 GB on a 24 GB Mac, with `iogpu.wired_limit_mb` at 22528, hung macOS for
  92 s and the watchdog restarted it (panic-full-2026-11-21-130921). Each engine's 90% ceiling assumes it is alone,
  and the bundled engine can't see another app's model. So before Inkling loads, after Wisp's own models are gone,
  Ideate reads free memory (vm_stat: free + inactive + speculative + purgeable). Below 15.1 GB + 3 GB it stops with
  "There isn't enough free memory for Inkling 2 Mini right now… Quit oMLX or other big apps", and loads nothing.
  The prompt-size measurement under the new ceiling was NOT completed and is not re-run without the user's yes.
