# Plan: Wisp-managed bundled oMLX engine (Option B)

Status: draft for review. Nothing implemented. Trigger for Wisp Release Auditor review: packaging, signing, and a change to the peer-attribution security boundary.

## Outcome

A fresh Wisp download runs local models with no separate oMLX install. Users who already have oMLX keep it, untouched, and can choose to keep using it. The user (and anyone) can still use oMLX normally.

## Findings that shape the design

- oMLX.app (0.7.1.dev1) is 1.6 GB: 1.4 GB `framework-mlx-base` (MLX 210M, torch-free but heavy: cv2 119M, llvmlite 125M, transformers 114M, scipy 101M, onnxruntime 80M, sklearn 49M...), 55 MB CPython 3.11, 115 MB `omlx` server package, 17 MB web admin UI.
- `omlx serve` already accepts `--port`, `--host`, `--base-path`, `--model-dir`, `--api-key`. `OMLX_BASE_PATH` is honoured by the launcher. So a second, fully isolated instance needs no oMLX patches.
- Wisp today hard-wires one engine: port 8000, `/Applications/oMLX.app/...` paths and Team ID `PSK5Q5T46L` in `service/inference/attributed_transport.py` (`DesktopOmlx`), `omlx-cli start/restart/stop` in `service/main.py:449`. A `ManagedOmlx` path (launchd authorization record at `~/.moe/omlx-runtime-authorization.json`) already exists beside it.
- oMLX.app updates itself (its own updater, `update-prefs.json`). A bundled copy would not.

## Coexistence with an existing oMLX (the key design point)

The bundled engine is a second, private instance. It shares nothing mutable with the user's oMLX:

| Resource | User's oMLX | Wisp engine |
|---|---|---|
| Port | 8000 | fixed private loopback port (proposed 8791; added to the reserved-port sets in `service/config/endpoints.py` and `SettingsView.swift`) |
| Base path / settings / logs / SSD cache | `~/.omlx`, `~/Library/Application Support/oMLX` | `~/Library/Application Support/Wisp/engine/` via `--base-path` |
| Process | `omlx-server` under oMLX.app | `omlx-server` child of Wisp's backend, bearer `--api-key` generated per install (Keychain) |
| Lifecycle | menu-bar app | started and stopped by the Wisp backend; dies with it, restarts on `ensure_engine()` |
| Models | their model dirs | read-only reuse of any existing oMLX model dir (detected from `~/.omlx/settings.json` `model_dirs`) plus Wisp's own download dir, so models are never downloaded twice |

Three engine modes, stored in Wisp config (`engine.mode`):

1. **bundled** (default on fresh installs): private instance above.
2. **external-omlx** (default when an existing oMLX install is detected on upgrade): exactly today's behaviour, port 8000, `DesktopOmlx` attribution. No migration, no behaviour change. This is the "keep using my oMLX" path and it is how development on this Mac continues unchanged.
3. **other** (Ollama, LM Studio, llama.cpp, MTPLX): unchanged.

Setup guide (`service/setup/engines.py`, `guide.py`) offers: "Use Wisp's built-in engine" or "Use my installed oMLX". Switching is a setting, reversible, and never deletes either side's data. Optional one-time "import settings" copies `model_settings.json` and `model_profiles.json` into the private base path (copy, not link).

RAM guard: two resident models double memory (the 24 GB Pro is already near its limit). Rules: the bundled engine never auto-starts while mode is external-omlx; if both are healthy at once, Wisp warns and unloads its own; engine idle-unload stays on.

## Updating

Two separate tracks, no conflict:

- **User's own oMLX.app** keeps updating itself exactly as now. Wisp never touches it.
- **Bundled engine** is pinned per Wisp release (`engine/ENGINE.lock`: oMLX version, git commit, MLX version, wheel hashes). It updates when Wisp updates. This is deliberate: Wisp's tool-calling, thinking-mode and KV behaviour depend on the engine (see the notes on thinking profiles and the 8k window), so silent engine drift is a regression risk.
- **Phase 4 (optional): engine packs.** A signed, notarized `engine-<version>.pack` downloaded into `Application Support/Wisp/engine/versions/`, verified against a pinned hash list and the Wisp Team ID before activation, with the previous version retained for rollback and activation gated on Wisp's existing model qualification tests (`scripts/test_model.py`). This lets engine fixes ship without a full app release. Do not build it until the bundled path is stable.

## Using it as normal

- Wisp-managed engine exposes the oMLX admin dashboard at `http://127.0.0.1:<private port>/admin` (needs the API key; Wisp adds an "Open engine dashboard" button that opens it authenticated).
- The oMLX menu-bar app is not shipped, so there is one menu-bar item (Wisp). Anyone wanting the oMLX app installs it from upstream and runs it beside Wisp on port 8000 with no interference.
- Anything Wisp can do today (roster, profiles, per-model settings, KV settings) is driven through a Wisp-owned `settings.json` template written into the private base path, replacing today's reliance on hand-tuned user settings (e.g. Ctx Window 16k).

## Implementation phases

**Phase 0: size and feasibility spike (read-only, 1 builder)**
- Build a minimal server-only environment: CPython 3.11 + pinned `omlx` + MLX, excluding the menu-bar app and unused extras. Measure size; try dropping cv2/onnxruntime/sklearn/modelscope if the text path does not import them (VLM and embeddings paths must be checked, since Smart Search uses `/v1/embeddings` and `/v1/rerank`).
- Verify `omlx serve --base-path --port --api-key` runs the Wisp roster end to end under a hardened runtime.
- Exit criteria: measured bundle size, list of required dylibs, go/no-go. Target: under ~700 MB added; if it lands above ~1 GB, switch to option C (first-run download) as the default and keep the same engine-mode design.

**Phase 1: packaging**
- `scripts/package_app.sh`: add `Contents/Resources/engine/` (relocatable CPython + site-packages + `omlx`), deterministic from `ENGINE.lock`.
- Codesign every Mach-O (dylibs, `.so`, Metal library, python binary) inside-out with the Wisp Team ID and hardened runtime; required entitlements (JIT / unsigned executable memory / library validation) decided in the spike; notarize and staple; verify with `spctl` and `codesign --verify --deep --strict`.
- Add to release notes/third-party licenses: oMLX and all bundled dependencies' licenses (confirm oMLX's license from its repo before shipping).
- Public release contract unchanged (single app ZIP).

**Phase 2: lifecycle and engine modes**
- New `service/engine/` module: `EngineManager` (start, health, restart, stop as a Wisp child; port and key from config/Keychain; log capture to Wisp's log dir). `ensure_omlx()` dispatches on `engine.mode`; `/shutdown_omlx` stops only the Wisp-owned child, never the user's oMLX.
- Resolve the "quitting Wisp kills the engine and `start` cannot revive it" class of bug by construction: Wisp owns the process.
- Mode detection on first launch and upgrade: existing oMLX.app → default external-omlx with an unobtrusive offer to switch.

**Phase 3: attribution and security boundary (Auditor-reviewed)**
- Add `BundledOmlx` authority beside `DesktopOmlx`/`ManagedOmlx` in `attributed_transport.py`: accept the listener only if it is a direct child of the Wisp backend PID (or its launcher), its executable resolves inside `Wisp.app/Contents/Resources/engine`, and `csops` shows Wisp's Team ID, `CS_VALID|CS_RUNTIME`, plus the per-install bearer key on every request. Keep the existing PID/incarnation/socket four-tuple checks before request bytes.
- Keep `DesktopOmlx` (port 8000, oMLX Team ID) for external mode; never accept one authority's listener on the other's port.
- Tests: refuse an impostor on the private port, refuse the user's oMLX on the private port, refuse a swapped binary, refuse a missing or wrong key.

**Phase 4 (optional): engine packs and in-app engine update**, as above.

## Validation

- Exact-SHA mechanical evidence plus required CI, per AGENTS.md.
- Router/attribution tests extended; new tests for mode selection and no-interference (both engines running at once on different ports with separate base paths).
- Packaged-app smoke on a clean macOS 26 user account with no oMLX installed (fresh path) and on one with oMLX.app running (coexistence path), in an isolated sandbox per the safe-live-testing rules: never against 8765, never replaying against the user's live backend.
- Specialist QA: Simulation QA for engine startup/recovery; Live QA only on isolated synthetic state.

## Risks and open questions

- Size: unknown until Phase 0; the full oMLX stack is 1.4 GB of Python packages.
- Notarizing many third-party native binaries under hardened runtime is the likeliest source of schedule slip.
- macOS floor: oMLX 0.7+ needs macOS 26 (user-stated, unverified), so a bundled engine makes 26 the effective minimum for the default mode; older macOS users stay on "other" engines.
- Upstream license and trademark terms for redistributing and renaming the process: verify before Phase 1.
- Upgrade path for the existing Hackathon/installed builds: mode defaults to external-omlx, so no change for current users.
- Double-RAM risk on 24 GB machines if a user runs both engines with big models resident.

## Ownership

Primary writer: one Worktree from current `origin/main`. Owned paths: `scripts/package_app.sh`, new `service/engine/`, `service/inference/attributed_transport.py`, `service/main.py` (lifecycle functions only), `service/setup/engines.py`, `service/setup/guide.py`, `service/config/endpoints.py`, `app/Sources/WispApp/{SettingsView,SetupGuideView,AppDelegate}.swift`. Conflicts to check: the in-flight Ling V3 profile slices touch `service/main.py` and prompt/window code; coordinate through the Orchestrator before editing.

## Phase 0 results (2026-10-13, scratch prototype, not committed)

Method: traced `import omlx.server` under the installed oMLX's interpreter, copied only the loaded packages plus omlx's lazily imported ones (excluding `cv2`, 124 MB, used only by video utils, and `modelscope`, 53 MB, used only by the ModelScope downloader) into a scratch dir, and ran `omlx serve` from it on port 8791 with an isolated `--base-path`, an empty `--model-dir` and `--api-key`. No model was loaded, no download was made, port 8000 was not touched.

- **Size:** 716 MB uncompressed (540 MB packages incl. all dist-info metadata, 55 MB CPython, 115 MB omlx, 17 MB admin UI) versus 1.6 GB for oMLX.app. Eagerly loaded packages alone are 503 MB. Expect a smaller compressed ZIP. Pruning further (e.g. `transformers` 107 MB) needs testing.
- **It works:** server started, `/v1/models` answered with the bearer key, 401 without it, `/admin` returned 200, RSS small while idle.
- **Entitlements needed** (from the installed app's signature): `allow-jit`, `allow-unsigned-executable-memory`, `disable-library-validation`, hardened runtime. Wisp must carry the same set.
- **Gotcha 1: `--base-path` does not isolate everything.** On every start the server rewrites `~/.omlx/bin/omlx-cluster-python`, pointing it at its own interpreter. The prototype overwrote the user's copy; I restored it to the installed-app paths (oMLX rewrites it on its next start anyway). The Wisp engine must run with a private `HOME` (or the cluster shim must be disabled) so it never writes into the user's `~/.omlx`.
- **Gotcha 2: unexplained model listing.** `/v1/models` listed `mlx-community--Qwen3.5-0.8B-MLX-4bit` even with an empty `--model-dir`. Source not yet identified (possible default discovery path or HF cache). The bundled engine must list only models in Wisp's own directories.
- **Gotcha 3:** `Metal cap (17.8GB) is below the oMLX static ceiling (21.0GB)` warning appeared because `iogpu.wired_limit_mb` is unset in this scratch run; the bundled engine needs its own `settings.json` template with a ceiling that fits the host.
- **Missing metadata breaks imports:** `transformers` calls `importlib.metadata` at import, so all `*.dist-info` directories for kept packages must ship.

## Double-RAM guard (added)

1. Bundled engine never auto-starts in `external-omlx` mode, and vice versa.
2. Startup and periodic check: if a healthy engine is also on port 8000 while Wisp's own engine runs, warn and unload Wisp's models first.
3. Pre-load check: sum resident memory of both engines against `iogpu.wired_limit_mb`; refuse or shrink the load if over.
4. Short default idle-unload on the bundled engine.
Unverified assumption: oMLX's memory guard only sees its own process. Confirm in Phase 2 before relying on it.
