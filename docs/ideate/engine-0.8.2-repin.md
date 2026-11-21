# Bundled engine re-pin: oMLX 0.7.3 to 0.8.2

Why: the user's standing rule is that the bundled engine follows the installed oMLX, and `/Applications/oMLX.app` is now 0.8.2 (build 3849). Only 0.8.2 runs the Ideate model (Inkling 2 Mini, `omlx/patches/inkling2`). Everything below was computed read-only from `/Applications/oMLX.app`. The engine was never started and no model was loaded.

This is a risk-triggered change (native integration, memory guard, release pins). It needs an independent Release Auditor verdict on the exact candidate SHA before any release. A preview build may use it before that.

## How the 0.7.3 baseline was recovered

No 0.7.3 oMLX.app is installed, but the shipped Harbor.app holds the overlaid 0.7.3 engine. Reversing each guard edit (with the Wisp to Harbor rename) rebuilt all eight 0.7.3 inputs, and each matched its pinned `input_sha256` exactly. Every 0.8.2 change near an edit was then read as a real diff against that baseline, not guessed. (0.7.3 DMGs are also still cached under a few `.wisp-build/engine-cache` folders.)

## Per patch

- `omlx/__init__.py`, `omlx/process_memory_enforcer.py`, `omlx/patches/gemma4_verify_attention.py`, `omlx/patches/gemma4_verify_kernel.py`: byte-identical upstream. No change.
- `omlx/engine/vlm.py` (3 edits, encoders on demand): all edits apply unchanged, so only the checksums move. 0.8.2 adds LoRA adapters, the Inkling 2 loader, Hy4 thinking modes and a Prefill Accelerator helper that `stop()` closes. The encoder attach still runs after `_inject_tool_calling` and just before `_loaded = True`, and detach still runs first in `stop()`. mlx-vlm is still 0.7.11 and its gemma4 model code is byte-identical, so `wisp_encoders` (bit-exact or resident) is unaffected. Inkling 2 is not in `SUPPORTED_CLASSES`, so it stays fully resident.
- `omlx/engine_pool.py` (24 edits, admission and owned unload): 23 edits apply unchanged. One is ported (edit 6). 0.8.2 put `early_helper = ... self._start_prefill_helper_early(...)` between the Splash registration and `try: await self._start_engine(engine)`, which broke the anchor. The port keeps the same two lines (`entry._wisp_active_engine = engine` and `_wisp_guard.bind_start(engine, _wisp_check_load)`) directly after the Splash registration, before the early helper. The engine is therefore bound to the admission check as soon as it is built, as before. The early helper only starts a Prefill Accelerator on another paired Mac, and only when `prefill_helper_node_id` is set. It allocates nothing locally, and the up-front `_wisp_check_load()` before `is_loading` still runs first. The other new memory users (the Inkling 2 DSpark draft and LoRA adapters) are added to `_entry_runtime_resident_size`, which is the size the guard admits against. `_unload_engine`, `_stop_and_unload_engine` and `_start_engine` are unchanged upstream. No new call site anywhere in `omlx/` starts an engine outside the pool or server paths the guard already wraps (grep of `.start(`, `_start_engine(` and `_load_engine(` compared between versions).
- `omlx/patches/mlx_lm_mtp/batch_generator.py` (4 edits, MTP failure falls back): all edits apply unchanged, and `deque` and `SimpleNamespace` are still imported. 0.8.2 adds a cluster `share_batch_plan` sync before the batched MTP try. That path is only for distributed ranks, which the bundled engine does not run.
- `omlx/server.py` (14 edits, guard wiring, `start_bound` for 3 `engine.start()` sites, encoder routes): all edits apply unchanged at the same counts. 0.8.2 adds Prefill Accelerator admin routes (behind admin and distributed inference), `/tokenize` on embedding engines, prompt preparation off the event loop, and a keep-warm idle window. None of these adds a model start.
- Additions (`wisp_guard`, `wisp_encoders`, `wisp_kernel_guard`): unchanged. Every private upstream attribute they read (for example `_unloading_models`, `_stop_and_unload_engine`, `_memory_guard_tier`, `_prefill_memory_guard`, `_propagate_memory_limit` and `omlx.exceptions.OMLXError`) still exists in 0.8.2, and no upstream `omlx/wisp_*` file collides.

No edit was dropped and no guard was weakened.

## Pins

- `engine.lock.json`: 0.8.2, tree `07367fc8...4648`, minimum macOS 26.2, from `engine.py lock --app /Applications/oMLX.app`. The codesign Team ID check passed.
- `engine_guard/manifest.json`: `upstream_tree_sha256` equals the lock. The patch, input and output hashes of the four changed patches were recomputed. The other four rows are unchanged.
- `engine-source.json`: **UNVERIFIED.** It has tag `v0.8.2`, version `0.8.2` and asset `oMLX-0.8.2-macos26-27.dmg` (assumed from the 0.7.3 naming). `size` and `sha256` are placeholders (1 and all zeros), and an `unverified` note says so. The schema needs an integer and a hex digest, so a `--fetch-engine` or CI download fails closed on the size check until someone sets these three fields from GitHub's v0.8.2 release asset. Local `--engine-app /Applications/oMLX.app` builds are unaffected.
- `locks.json`: no engine entries, unchanged, all 5 pins valid.
- `engine-prune.json`: unchanged. All 35 dropped names still exist in 0.8.2. The newly kept top-level packages are `ifaddr` (MIT) and `zeroconf`. aiohttp, yarl, multidict, frozenlist, propcache, aiosignal, aiohappyeyeballs and six left upstream. The prune list must still be re-validated by `scripts/engine_smoke.py` (it starts the engine, so it was not run here).
- `engine-licenses.json`: added a reviewed weak-copyleft entry for `zeroconf` 0.148.0 (LGPL-2.1-or-later). It is new in 0.8.2, imported lazily by cluster discovery, and ships as Python sources plus Cython extensions. With it the licence gate over the selected 0.8.2 tree reports no problems. `engine-licenses/oMLX-LICENSE.txt` is unchanged: 0.8.2 ships no top-level oMLX licence file to compare against.

## Risks for the Auditor

- The DMG size and SHA-256 are unverified, so a release CI fetch cannot pass until they are filled.
- The engine_pool edit 6 port is the only edit that changed, and its ordering relative to the early Prefill Accelerator start needs review.
- The zeroconf licence classification is new.
- The engine smoke run and the real-GPU kernel test (`test_real_kernels_on_this_gpu_with_a_448_thread_cap`) were not run: no engine or GPU work was allowed in this task.
- 0.8.2 is a large upstream jump (LoRA, Inkling 2, Prefill Accelerator, cluster changes). Memory estimates for new model families depend on upstream's size estimators, which the guard trusts as before.
