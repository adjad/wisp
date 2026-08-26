# Wisp — Session Handoff

A complete state-of-the-project summary for continuing in a fresh chat. Written 2026-07-11.

---

## 1. What this project is

**Wisp** (formerly "MOE") is a local, privacy-first, Siri-style AI assistant for macOS. It lives in the
MacBook notch (Notchbox-style), is summoned by hover or ⌥Space, and routes each request to the best
local model (a Mixture-of-Experts dispatcher). Everything runs on-device via **oMLX** (an MLX-based
inference server exposing an OpenAI-compatible API on `127.0.0.1:8000`).

- **Hardware:** M5 Pro, 24 GB unified memory, ~18 GB GPU wired cap. (A secondary M4 Air, 16 GB, is being
  explored as a side-compute "router server" — see §8.)
- **Three processes:** Swift menu-bar/notch app (frontend) → Python FastAPI service on `:8765` (agent
  loop, router, tools, safety, memory) → oMLX server on `:8000` (model serving).
- **Working dir:** `/Users/adijain/Desktop/MOE_Project` (folder still named `MOE_Project` — deliberately
  left as infra; the *product* is Wisp).

---

## 2. Models (current roster)

Defined in `service/config/models.yaml` under `roles:`. The router emits a *role*; this file maps role →
concrete oMLX model id.

| Role | Model | Notes |
|------|-------|-------|
| `fast` / `router` | `gemma-4-e4b-it-4bit` (~5.5 GB) | Trivial replies + the LLM classifier. **Not** tool-capable. |
| `coding` | `Qwen2.5-Coder-14B-Instruct-4bit` (~8.3 GB) | Code authoring/fixing. Called as plain completion + via `write_code` delegation. |
| `reasoning` | `gpt-oss-20b-MXFP4-Q8` | **Phi-4 was retired** (see §4). Now gpt-oss at `reasoning_effort=medium`. |
| `vision` | `Qwen3-VL-8B-Instruct-4bit` (~5.8 GB) | Screenshots / images. |
| `agent` / `general` | `gpt-oss-20b-MXFP4-Q8` (~12.7 GB) | The **only** tool-capable model; catch-all. |

- `tool_capable:` lists **only** `gpt-oss-20b-MXFP4-Q8`. gemma's tool calls proved unreliable → removed.
  The router *forces* any tool-using request onto gpt-oss.
- **Installed on oMLX but not in the roster:** `gemma-4-12B-it-qat-6bit` (and historically DeepSeek-R1,
  GLM-4.7-Flash appeared in a status check — roster only uses what's mapped above).
- **Memory reality:** gpt-oss (12.7 GB) and Coder-14B (8.3 GB) **cannot co-reside** under the ~18 GB cap;
  oMLX 507s rather than auto-evicting. Model swaps cost ~1.1–3.2 s. This constraint drives many design
  decisions (idle-unload, `ensure_only` polling, the coder-delegation swap dance).

---

## 3. Current runtime config / flags

- `service/config/policy.yaml`: `read_only: false`, `full_access: true` (every action auto-runs, **no**
  confirmation prompts). A catastrophic-command deny floor still blocks `sudo`, `rm -rf /`, fork bombs,
  disk wipes unless `disable_safety_floor: true`.
- `service/config/models.yaml`: `idle_unload_minutes: 20`, `air_compute.enabled: false`.
- **IMPORTANT — runtime vs. persisted state:** `full_access`, `read_only`, and `idle_timeout` are toggled
  in-memory at runtime (via `/mode`, `/idle_timeout`). On a **backend restart they revert to the YAML
  values.** Config is read once at import time — editing YAML requires a restart to take effect.

---

## 4. Everything built/changed this session (chronological-ish)

1. **App-control tools** — `service/tools/apps.py`: `open_app`, `quit_app`, `spotify` (play_uri/play/
   pause/next/previous). Flipped `read_only` off.
2. **Full-access mode** — `service/safety/policy.py` gained a `full_access` short-circuit + runtime
   setters; `/mode` GET/POST expose it; Settings has a "Full access" toggle. Catastrophic deny floor kept.
3. **Conversation memory** — new `service/memory/` package:
   - `store.py` — SQLite at `~/.moe/sessions.db` (sessions + turns tables; rolling summary; `pinned_role`/
     `pinned_model` for sticky routing).
   - `context.py` — token-budgeted window (last ~12 turns, ≤3K tokens) + rolling summary via the resident
     model; tool outputs digested, not replayed. Keeps working set flat (~1.5 GB KV) regardless of chat
     length.
   - `/agent` now takes `session_id`; frontend round-trips it. New `/sessions` GET/GET-by-id/DELETE.
   - Frontend renders the transcript; "New chat" button; session survives collapse/expand.
4. **Tool-routing correctness** — the big recurring theme:
   - Router forces gpt-oss for **all** `needs_tools` work (`_finalize` in `router.py`).
   - `write_code` delegation tool (`service/tools/codegen.py`): the gpt-oss agent delegates code authoring
     to Coder-14B, then applies output with `write_file`. Swaps coder in, agent loop re-`ensure_only`s
     gpt-oss on its next step.
   - `needs_code_delegation` flag: set when a request both authors code (`CODE_AUTHOR_RE`) and is agentic.
     Agent loop **forces `write_code` as the only first-step tool** + retries with a nudge (oMLX's forced
     `tool_choice` is only a soft nudge for gpt-oss harmony format — verified empirically).
   - Router `LOCATION_RE`: detects "save it to downloads/desktop/…" verb-agnostically. A code request that
     also names a filesystem destination → routes to `agent` (so it can actually write the file). This
     fixed the reported "coding model can't leave a file in Downloads" bug.
   - `CODE_RE` broadened (html/css/website/script/program + bare language names python/rust/etc).
5. **Retired Phi-4 reasoning** — empirical probes showed gpt-oss at `reasoning_effort=medium` matched
   Phi-4's rigor (same decision-tree proof) at ~5–8× the speed, and Phi-4 sometimes didn't finish.
   `reasoning` role now → gpt-oss. **Key probe finding:** for gpt-oss, `reasoning_effort` (via
   `chat_template_kwargs`) is the real lever; `thinking_budget` is cosmetic; `reasoning_effort=high` burned
   the whole token budget on reasoning and returned **empty** content (avoid). `medium` is the sweet spot.
6. **Reasoning heartbeat** — the reasoning branch runs `chat` as a background task and emits `heartbeat`
   SSE events every 12 s so a long silent generation doesn't look hung / trip client idle timeouts. Swift
   client timeout raised to 600 s; UI shows an elapsed counter.
7. **Idle model auto-unload** — `service/idle.py` (last-used tracking) + `service/idle_unloader.py`
   (30 s poll loop, unloads models idle past the timeout). `/idle_timeout` GET/POST; Settings "Auto-unload
   idle models" stepper (0–120 min, 0 = off). Default 20 min.
8. **Rename MOE → Wisp** — full rename: Swift module `MOEApp`→`WispApp`, `MOEClient`→`WispClient`, bundle
   id `com.wisp.assistant`, `/Applications/Wisp.app`, all visible strings, README/BUILD_PLAN titles,
   Carbon hotkey signature. New **tessera diamond icon** (mint/emerald facets + thin gold edge on the
   top-right facet, no glint) in `scripts/make_icon.swift`. Fixed a stale Dock question-mark (old
   `/Applications/MOE.app` reference).
9. **Notch UI (Notchbox-style)** — the app lives in the notch:
   - Collapsed = a small black bar fused with the physical notch (`barScale = 0.5`).
   - Hover the notch → expands with a **notch-stretch reveal** (a `CALayer` mask grows from the bar's rect
     to the full panel; `CASpringAnimation` for bounce; animated corner radius 14→28; content rasterized
     during the animation to avoid frame drops).
   - Window level: `.statusBar` when collapsed on a notch screen (required to render fused with the menu
     bar), else normal. `isFloatingPanel = true`.
   - **Not always-on-top:** collapses when it loses key focus (you click another app) after a **2-second
     grace** (cancellable timer; re-hover/re-focus cancels; won't hide an unsent draft).
   - Hover detection uses a real AppKit `NSTrackingArea` (`HoverView`), **not** SwiftUI `.onHover` (which
     dropped exit events in a borderless panel — this was the "it won't retreat" bug).
   - Alignment (left/center/right) options were **removed**; panel is always centered on the notch.
   - Corner radius bumped (panel 28, bar 14) for a rounder look.
10. **oMLX experimental-settings review** — concluded for the current roster: TurboQuant KV Cache is the
    only immediately-usable one (no draft-model dependency); SpecPrefill/VLM MTP need a smaller
    same-tokenizer draft model (none installed — gpt-oss has no smaller sibling; VLM would need a small
    Qwen-VL); DFlash/Native MTP are inert (wrong architecture). Nothing was enabled in code.
11. **MacBook Air side-compute (routing) — Pro side already built.** See §8.

---

## 5. Architecture / key files

```
service/
  main.py              FastAPI app + all endpoints + the /agent SSE runner
  config/
    __init__.py        role_to_model, air_compute_config/set/status, set_role, omlx_base_url
    models.yaml        roles, tool_capable, idle_unload_minutes, air_compute
    policy.yaml        read_only, full_access, disable_safety_floor
  router/router.py     rule_route (regex) → air_route (optional) → llm_route; _finalize invariants
  inference/omlx_client.py   OMLXClient: chat/stream/ensure_only/load/unload; touches idle on success
  agent/
    loop.py            ReAct loop; force_first_tool delegation; ensure_only per step
    approver.py        InteractiveApprover (confirm-tier → SSE → /agent/approve)
  tools/
    registry.py        @register decorator, tool schemas, run_tool
    builtin.py         run_shell, read_file, list_dir, write_file, delete_path
    apps.py            open_app, quit_app, spotify
    codegen.py         write_code (delegates to Coder-14B)
    vision.py          see_screen, describe_image (swaps to VLM and back)
  safety/
    policy.py          decide() → allow/confirm/deny; read_only/full_access; shell allow/deny/mutate lists
    audit.py           append-only ~/.moe/audit.jsonl
  memory/
    store.py           SQLite sessions/turns
    context.py         build_messages (budgeted), maybe_summarize
  idle.py              last-used tracking + idle-minutes getter/setter
  idle_unloader.py     30s poll loop → unload idle models

app/Sources/WispApp/
  main.swift           NSApplication accessory bootstrap
  AppDelegate.swift    status item, hotkey, panel lifecycle, expand/collapse, 2s grace timer, focus observers
  OverlayPanel.swift   NSPanel; HoverView tracking area; notch geometry; dropOpen/rollUp mask animation
  OverlayModel.swift   ObservableObject: phases, turns, session id, submit(), SSE handling
  OverlayView.swift    SwiftUI: notchBar + expandedPanel, header, input, transcript, confirm card
  WispClient.swift     HTTP/SSE client; runAgent, mode, idleTimeout, airCompute, models
  SettingsView.swift   Models pickers, Full access, MacBook Air Compute, Auto-unload, Memory sections
  Theme.swift          colors + notchCorners shape
  BackendManager.swift spawns the Python backend if :8765 isn't already healthy
  GlobalHotKey.swift, MarkdownView.swift, Math.swift

scripts/
  run.sh               dev backend: uvicorn service.main:app --port 8765 --reload (localhost only)
  package_app.sh       release build → bundle → install to /Applications/Wisp.app
  make_icon.swift      generates the tessera diamond iconset
```

---

## 6. Build / run / package workflow (CRITICAL GOTCHAS)

- **Editing source does NOT update the running app.** The installed `/Applications/Wisp.app` is a packaged
  bundle. To see changes: re-run `./scripts/package_app.sh` **from the project root** (a `cd app` earlier
  in the shell makes `./scripts/...` fail — always `cd /Users/adijain/Desktop/MOE_Project` first), then
  quit + relaunch. Standard relaunch sequence used all session:
  ```bash
  cd /Users/adijain/Desktop/MOE_Project && ./scripts/package_app.sh
  osascript -e 'tell application "Wisp" to quit'; pkill -f "Wisp.app/Contents/MacOS/Wisp"; sleep 1
  open /Applications/Wisp.app
  ```
- **For fast dev iteration** (skip packaging): run the backend with `./scripts/run.sh` (or
  `.venv/bin/uvicorn service.main:app --port 8765`) and launch the debug binary
  `app/.build/debug/WispApp` directly. `BackendManager` reuses whatever's already healthy on :8765.
- **Backend config is read once at import.** Changing `models.yaml`/`policy.yaml` requires a backend
  restart (`--reload` only watches `.py`, not YAML).
- **oMLX must be running** on :8000 (its own menu-bar app). `ensure_omlx()` will try to start it via
  `/Applications/oMLX.app/Contents/MacOS/omlx-cli start` if down.
- Swift build: `cd app && swift build` (debug) or `swift build -c release` (packaging does this).

---

## 7. Known issues / bugs found — ALL FIXED 2026-07-12

Findings 1–6 from the cut-off code review are now fixed (verified):

1. ~~**`store.add_turn` idx race**~~ **FIXED** — idx is now `SELECT COALESCE(MAX(idx)+1,0)` computed
   *inside* the lock. Tested: 20 concurrent `add_turn`s on one session → unique, contiguous idxs.
2. ~~**`SESSIONS[sid]` clobber**~~ **FIXED** — the in-flight registry is keyed by a per-request
   `uuid4` (`SESSIONS[req_id] = {"sid", "queue", "approver"}`); `/agent/approve` resolves by the
   globally-unique `action_id`, filtered by `session_id`. Tested: two overlapping same-session requests
   resolve independently.
3. ~~**Deprecated `asyncio.get_event_loop()`**~~ **FIXED** — `approver.py` and `omlx_client.py`
   (`ensure_only`, now caches `loop = get_running_loop()`) use `get_running_loop()`.
4. ~~**Sticky-routing pins `vision`**~~ **FIXED** — `_STICKY_ROLES` no longer includes `vision`, so one
   image no longer pins the whole conversation to the VLM. (Vision was the only sticky role that changed
   the model; coding/reasoning/agent all pin to gpt-oss.)
5. ~~**Stale "MOE" self-identity**~~ **FIXED** — `agent/loop.py` SYSTEM, `ROLE_SYSTEM["general"]`, and the
   FastAPI app title now say "Wisp". Verified live: "what is your name?" → "I'm Wisp". Also closed a
   related fail-unsafe: `_from_remote` (Air router responses) mapped `fast`→`general` so an Air
   classification can't downgrade an already-ambiguous prompt onto tool-less gemma.
6. ~~**Dead code `store.touch()`**~~ **FIXED** — removed (`add_turn` updates `last_used` directly).

7. **Stale docs (still open, low priority):** `README.md` and `BUILD_PLAN.md` still reference "MOE" and
   retired models/roles (Phi-4, coding_max, reasoning_max, Coder-14B). The two handoff docs in
   `~/Downloads/` describe *aspirational* architectures that do **not** match the real code — exploration
   notes, not ground truth.

---

## 8. MacBook Air side-compute (routing) — status & assessment

**The Pro side is already fully implemented** (was built earlier this session):
- `service/router/router.py`: `route()` = local rules → Air (if enabled) → local LLM fallback (graceful,
  never a single point of failure). `air_route()`, `classify_local()`, `_from_remote()`.
- `service/main.py`: `/router/classify`, `/air_compute` (GET/POST), `/air_compute/check`.
- `service/config/__init__.py`: `air_compute_config`, `set_air_compute`, `set_air_compute_status`,
  `DEFAULT_AIR_COMPUTE` (base_url `http://m4air.local:8766`, timeout 700 ms, caps routing/summaries/draft).
- `app/.../WispClient.swift` + `SettingsView.swift`: "MacBook Air Compute" section (enable, URL, routing/
  summaries/draft toggles, Check button, status label). Streamed route metadata: `route_source`
  (`rules`/`air_router`/`local_router`/`fallback`) + `route_host`.

**To bring the Air online (V1):** it's the *same repo* run headless on the Air:
`.venv/bin/uvicorn service.main:app --host 0.0.0.0 --port 8766` (MUST bind 0.0.0.0, not localhost), with
`models.yaml` `roles.router` pointing at an installed model (e.g. `gemma-4-e4b-it-4bit`) and oMLX running
on the Air. Then in Wisp Settings → MacBook Air Compute: enable, set URL, Check. Use LAN or Tailscale;
never public-expose 8766. Full V1 spec is in `~/Downloads/CLAUDE_AIR_BACKEND_HANDOFF.md`.

**Honest assessment (given this session):** feasible and cheap (client is done), but the latency win is
narrower than that doc implies — only *ambiguous* prompts reach the LLM router at all (regex catches most),
and the specialist-model load cost is paid either way. The real benefit is keeping the router model warm
off the Pro (gemma otherwise competes for the 18 GB cap and gets evicted). **Higher-leverage** alternative
if you want the second Mac to actually make things faster: permanently host a full specialist (Coder-14B
or the VLM) on the Air so the Pro avoids its own swap cost — bigger lift (streaming full generations over
the network), deferred out of V1.

---

## 9. Suggested next steps (roughly prioritized)

1. **Fix the confirmed bugs in §7** — especially the `add_turn` race and `SESSIONS` clobber (real
   correctness), the vision-sticky pin (real UX), and the "MOE"→"Wisp" identity strings (user-facing).
2. **Finish the codebase review** — I only got partway; worth a clean pass before the Air work.
3. **MacBook Air V1** — provision the Air backend per §8; it's mostly ops, not code.
4. **Embedding-based router (BUILD_PLAN v1, never built)** — an earlier discussion: replace/augment the
   gemma LLM classifier with an embedding + nearest-centroid or logistic-regression classifier. oMLX has
   `/v1/embeddings` but **no embedding model is installed** (would need a small one, ~50–150 MB). Cheaper
   to retrain than fine-tuning gemma; more robust to roles changing. Prerequisite: **log routing decisions
   to disk** (currently the `routed` event is ephemeral — no dataset exists to train on).
5. **Air memory/retrieval phases** (from the handoff doc) — summaries + fact extraction on the Air SSD,
   retrieval-before-answering with a strict ~150–250 ms timeout.

---

## 10. Quick reference — endpoints & data locations

- Backend: `http://127.0.0.1:8765` — `/health`, `/models`, `/chat`, `/agent` (SSE), `/agent/approve`,
  `/mode`, `/idle_timeout`, `/unload_all`, `/config`, `/sessions*`, `/air_compute*`, `/router/classify`,
  `/shutdown_omlx`.
- oMLX: `http://127.0.0.1:8000` (OpenAI-compatible; api key in `~/.omlx/settings.json`).
- Data: `~/.moe/sessions.db` (conversations), `~/.moe/audit.jsonl` (tool-action audit log).
- Installed app: `/Applications/Wisp.app`. Dev binary: `app/.build/debug/WispApp`.

---

## 11. Environment notes for the new chat

- The folder is not a git repo (as of session start). Consider `git init` before further work so changes
  are diffable/revertable — a lot of iterative UI/animation work happened without version control.
- Auto-memory exists for this project (`moe-project.md`, `moe-ui-direction.md`) — may want to update the
  UI-direction memory (it predates the notch/Wisp rename).
