# Wisp 1.3.2 bundled engine: what can go to Codex

Status: proposal. Nothing has been sent to Codex. Dispatching sends repository code to OpenAI, so it needs the user's go-ahead (standing note in the agent-roles memory).

Base: integration branch `worktree-bundled-omlx-engine` (merged with `origin/main` 18572f6f). Integrator: this session. Codex CLI 0.159.2 is installed and logged in.

## What is left for 1.3.2

| # | Work | Offload? | Why |
|---|---|---|---|
| 1 | Release tooling: engine lock generator, `scripts/engine_smoke.py`, build docs, engine third-party inventory | Codex, Luna xhigh | Mechanical, no security surface, clear tests |
| 2 | Pipeline tests for the engine hooks (`pipeline_checks.py`) | Codex, Luna xhigh | Test-only, one file, product code frozen |
| 3 | Settings screen for engine mode (Swift) | Codex, Luna xhigh | Bounded UI against a frozen API contract |
| 4 | Backend `/engine` API, sticky default mode, double-engine RAM guard, setup-guide awareness | Codex, Sol high | Python with config persistence and process-memory logic; harder than Luna's row |
| 5 | Independent review of the attribution + packaging SHA | Codex, read-only | The builder (this session) must not review its own security change; Codex is independent of me |
| 6 | Preview build and packaged-backend end-to-end run with a real model | Keep | Needs this Mac, real hardware, sandbox rules, judgment on results |
| 7 | Reconciling with `origin/main`, version bump, changelog, PR, CI, merge | Keep | Integrator duties; hot files |
| 8 | oMLX and dependency licence check | Keep (read-only Claude helper, no code leaves the Mac) | Needs only the installed app's metadata |
| 9 | Attestation reuse lease for the bundled engine | Defer past 1.3.2 | Security work on code that PR #200 just stabilised; own audit |

## Ownership (disjoint, one writer per path)

| Slice | Owned paths (only these) | Does not touch |
|---|---|---|
| 1 | `build-support/engine.py` (append a `lock` subcommand and a licence-inventory function), `scripts/engine_smoke.py` (new), `docs/build-release.md`, `tests/test_engine_packaging.py` | `pipeline.py`, `release.py`, `service/` |
| 2 | `tests/build_pipeline/pipeline_checks.py` | everything else |
| 3 | `app/Sources/WispApp/EngineSettingsView.swift` (new), one hook line in `app/Sources/WispApp/SettingsView.swift` | other Swift, `service/` |
| 4 | `service/engine/api.py` (new), `service/engine/mode.py`, `service/setup/guide.py`, `service/setup/engines.py`, `tests/test_bundled_engine_api.py` (new), one `include_router` line in `service/main.py` | attribution, `bundled_peer.py`, `manager.py`, hot routing files |

Integrator keeps `pipeline.py`, `release.py`, `service/inference/*`, `service/engine/manager.py`, `service/engine/runtime.py` while the slices run. Slice 1 takes over `engine.py` and its test file for the duration; I do not edit them until it lands.

Slices 1, 2, 3, 4 can all run at once. Only slice 3 depends on slice 4, through the frozen contract below, so it can start immediately.

## Frozen API contract (slice 3 builds against it, slice 4 implements it)

`GET /engine` returns:

```json
{"mode": "bundled" | "external-omlx",
 "configured_mode": "bundled" | "external-omlx" | null,
 "bundled_available": true,
 "unsupported_reason": null,
 "running": false,
 "omlx_version": "0.7.x" | null,
 "admin_url": "http://127.0.0.1:8791/admin" | null,
 "external_omlx_installed": true,
 "restart_required": false}
```

`POST /engine/mode` with `{"mode": "bundled" | "external-omlx"}` writes `engine.mode` to the user overlay (`~/.moe/config.yaml`), returns the same object with `restart_required: true` when it differs from the running mode, and returns 409 with a plain message when `bundled` is requested but `bundled_available` is false. It accepts no other fields. It uses the same local-auth guard as the other config endpoints.

Sticky default (slice 4): on first launch with no stored mode, resolve `bundled` only if the engine is present and supported and no oMLX.app is installed; otherwise `external-omlx`. Store the result in the overlay so installing oMLX later never flips it.

Double-engine guard (slice 4): when running bundled and a healthy engine answers on port 8000, `GET /engine` adds `"external_engine_running": true` and the backend logs a warning; before a model load in bundled mode, refuse with a plain message if resident memory of both engines would exceed 85 percent of `iogpu.wired_limit_mb`.

## Worktrees and dispatch

Each slice gets its own worktree from the integration branch HEAD:

```bash
git worktree add .claude/worktrees/engine-slice-N -b engine-slice-N worktree-bundled-omlx-engine
```

Luna slices:

```bash
codex exec -C .claude/worktrees/engine-slice-N -s workspace-write -m gpt-6-luna \
  -c model_reasoning_effort=xhigh "<brief N>" < /dev/null
```

Slice 4 (Sol):

```bash
codex exec -C .claude/worktrees/engine-slice-4 -s workspace-write -m gpt-6.1-sol \
  -c model_reasoning_effort=high "<brief 4>" < /dev/null
```

Review (slice 5), read-only, after the freeze commit:

```bash
codex exec -C .claude/worktrees/bundled-omlx-engine -s read-only -m gpt-6.1-sol \
  -c model_reasoning_effort=high "<review brief>" < /dev/null
```

The Codex sandbox cannot commit (worktree git metadata is outside it). The integrator commits each slice's diff after checking it stayed inside its owned paths, then merges the slice branches into the integration branch.

## Briefs

All briefs start with: "You are working in a Wisp worktree. Edit only the files listed under Owned paths. Do not run git commit. Do not run anything that starts a real model, touches ports 8000, 8765 or 8791, or reads the user's ~/.omlx or ~/.moe. Run tests with `/Users/adijain/Desktop/MOE_Project/.venv/bin/python <file>` (standalone scripts that print `N passed, M failed`). Finish with: files changed, tests run with results, anything left undone."

### Brief 1: release tooling (Luna)

Context: `build-support/engine.py` stages a pruned oMLX server into the app bundle (read it and `tests/test_engine_packaging.py` first). Add:
1. `python build-support/engine.py lock --app /Applications/oMLX.app [--output build-support/engine.lock.json]`: verifies the source identity, computes `tree_digest` over `select_files`/`link_entries`, `minimum_macos` via `minimum_macos()`, and writes `{"schema":1,"omlx_version","source_tree_sha256","minimum_macos"}` (the shape `load_lock` validates). Refuse to overwrite an existing lock unless `--force`. Print the digest and version.
2. `engine.third_party_inventory(app)`: for every kept `*.dist-info` return name, version, licence (from METADATA `License-Expression`, `License`, or the licence classifier), sorted; plus the oMLX package's own licence file if present. Add `python build-support/engine.py inventory --app PATH` printing JSON.
3. `scripts/engine_smoke.py --engine-root DIR --model-dir DIR [--model NAME]`: uses `service.engine.EngineManager` on a random free port in a temp data dir, waits until ready, lists models, sends one short chat completion and one embeddings request to the smallest model found, prints a JSON result, always stops the engine. It must refuse ports 8000, 8765, 8791 and exit non-zero on any failure. It must not read `~/.omlx`.
4. A "Bundled engine" section in `docs/build-release.md`: the flag, the lock, the macOS floor, size, signing identifier, entitlements, and how to run the smoke.
5. Tests for 1 and 2 in `tests/test_engine_packaging.py` using its synthetic app fixture.
Owned paths: `build-support/engine.py`, `scripts/engine_smoke.py`, `docs/build-release.md`, `tests/test_engine_packaging.py`.

### Brief 2: pipeline tests (Luna)

Context: `build-support/pipeline.py` gained `--engine-app` handling (`assemble(..., engine_app)`, `validate_structure`, `validate_native` with a per-tree minimum, `prepare_native_for_signing` skipping engine files, `sign_adhoc` identifier args, `engine_smoke`, notes text). Add unittest cases to `tests/build_pipeline/pipeline_checks.py` in its existing style (read neighbouring tests first) covering: engine files held to the engine minimum while app files keep 14.0 (both directions refused); `strip` not run on engine files; the ad-hoc signing command carries `-i com.wisp.engine.python3` only for `engine/py/bin/python3.11`; `validate_structure` rejects a bundle whose ENGINE.json is missing pieces; release notes mention the built-in engine only when `meta["engine"]` is set; `engine_smoke` raises when the runner reports failure or the marker is missing. Do not edit product code; if a test reveals a defect, report it in your final message instead.
Owned paths: `tests/build_pipeline/pipeline_checks.py`.

### Brief 3: settings screen (Luna)

Context: the frozen `/engine` contract above. Add `EngineSettingsView.swift` (a SwiftUI section: current mode, a two-choice picker "Built-in engine" / "My installed oMLX", an unavailable reason when `unsupported_reason` is set, a "restart Wisp to apply" notice when `restart_required`, an "Open engine dashboard" button for `admin_url`, and a warning row when `external_engine_running`). Follow the style and networking helpers already used in `SettingsView.swift` and `SettingsResponseValidator.swift` (read them; responses must be validated the way existing ones are, loopback only, no new network paths). Add one line in `SettingsView.swift` to show it. Typecheck as far as your sandbox allows; the integrator builds it.
Owned paths: `app/Sources/WispApp/EngineSettingsView.swift`, `app/Sources/WispApp/SettingsView.swift` (one hook line).

### Brief 4: backend engine API (Sol)

Context: `service/engine/mode.py`, `manager.py`, `runtime.py`, the contract above, `service/setup/guide.py`, `service/setup/engines.py`, and how existing config endpoints in `service/main.py` guard writes (reuse that guard exactly). Implement `service/engine/api.py` (an `APIRouter` with `GET /engine` and `POST /engine/mode`), the sticky first-launch default, the double-engine guard, and setup-guide awareness (the guide reports the built-in engine as ready when `mode == bundled` and the engine is present). Persist only `engine.mode` in the overlay through the existing overlay writer. Do not change `service/inference/*` or `manager.py`. Tests in `tests/test_bundled_engine_api.py` (new, standalone style like `tests/test_bundled_engine_mode.py`, fresh interpreters for import-time mode) cover: default on a fresh home with and without oMLX.app present (inject the presence check), stickiness, 409 on unsupported, rejected extra fields and bad modes, restart flag, and no secrets in responses.
Owned paths: `service/engine/api.py`, `service/engine/mode.py`, `service/setup/guide.py`, `service/setup/engines.py`, `tests/test_bundled_engine_api.py`, plus one `app.include_router` line in `service/main.py`.

### Brief 5: independent review (read-only)

"Review commit range <base>..<frozen SHA> for the bundled oMLX engine. Read docs/plans/2026-10-13-bundled-omlx-attribution-design.md first. Focus: (1) can a process other than the manager's child receive the bearer key or any request bytes; (2) can a signed Wisp be downgraded to the unsigned-dev rule; (3) does any new code path write into `~/.omlx`, the app bundle, or outside the engine data dir; (4) does packaging stage anything outside the pruned list or accept an unpinned source in a release; (5) are reasons or secrets leaked into user-facing text. Return PASS, PASS_WITH_NOTES or BLOCK with file:line findings only. Do not edit files."

## What I do in parallel

Preview build with `--engine-app /Applications/oMLX.app`, the packaged-backend end-to-end run on an isolated port with a real small model, the licence check, and reconciling with `origin/main` after each slice lands.

## Risks of offloading

- Slice 4 and the existing Ling work both touch `service/main.py`; the single `include_router` line is the only shared edit and I apply it myself if it conflicts.
- Slice 3's Swift cannot be compiled by Codex's sandbox on this machine (known SwiftUI macro plugin path issue), so I compile and screenshot it.
- Code goes to OpenAI for every Codex slice, including the attribution review. The only secrets-adjacent files are tests with fake keys.
