# Wisp 1.3.3 engine setup audit and follow-up

Owner: `Verify Wisp 1.3.3 oMLX setup` (`01a13e10-14c9-7e13-aedd-c2021f00ede0`).
Branch: `codex/omlx-seamless-setup`.
Base: `ec7c8d60984e85af8e9949cd7c339a7db5734fe4`.
The Orchestrator's live ownership receipt is authoritative; this is a handoff.

## Released artifact

- Release: `v1.3.3`, build 1673, source `f5ae7709bf0c9c7044c62d7129fe130eb8c5ffe1`.
- Published ZIP: `01-Wisp-1.3.3-1673-arm64.zip`, 380,976,000 bytes.
- Downloaded ZIP SHA-256 matches GitHub: `3405372fc9eba8fe1695521c2928a007aaa60823908f76757485c902e81fd0c3`.
- The extracted app passes `codesign --verify --deep --strict --verbose=2`.
- Its manifest pins oMLX 0.7.0, with minimum macOS 26.2. The Wisp app minimum remains 14.0.
- `build-info.json` records engine smoke **skipped** on the macOS 15.7.9 build host. Packaging/signature success is not a runtime or model-response pass.
- The published engine supports `<model directory>/<organization>/<model>` and registers the leaf model ID. No directory repair is needed.
- The 16 GB catalogue already offers public oQ4e when unpublished oQ5e is unavailable. The initial oversized-download suspicion was withdrawn; catalogue ranking is unchanged.

## Findings and implementation

1. **Private downloader login blocks fresh setup.** The engine generates a private API key, while the setup guide opens an admin login requiring that unknown key. A narrow Wisp API now authenticates internally through the existing attributed transport and requests only public catalogue models. Keys, cookies, upstream errors, arbitrary repositories and destination paths are never returned to the UI.
2. **Failed engines appear ready.** Bundled setup now requires a successful live model-list response, offers Start/Retry, and cannot establish readiness from cached inventory alone.
3. **Old tests certify new configurations.** Setup invalidates test results on role/provider/engine changes and lost readiness. Generation checks reject delayed status/test responses from earlier configurations. Separate refresh errors clear on automatic recovery without erasing unrelated action errors.
4. **Smoke accepts unusable replies.** The smoke validator now requires a complete, nonempty visible assistant response and uses a bounded 512-token budget. Startup and completion timings are reported only when an actual smoke runs.

The native guide adds download/progress/cancel/retry, automatic discovery refresh, and redownload recovery when a completed task's model remains absent. Engine Settings opens this guide for the bundled engine. External oMLX retains its own existing dashboard flow. Closing a retained setup window explicitly stops polling.

Browser writes require JSON and fixed loopback origin/host validation, independently of the request's Host. Download operations share a lock with one 165-second outer deadline, including queueing; the native request allows 180 seconds. Interrupted requests reconcile with engine task state before retrying, preventing duplicate active downloads. No credential export, generic admin proxy, authentication bypass, core transport change or package repinning was added.

## Files

- `service/engine/downloads.py`, `service/engine/api.py`: bounded private downloader/start API.
- `service/setup/guide.py`: live readiness and bundled metadata.
- `app/Sources/WispApp/SetupGuideView.swift`, `EngineSettingsView.swift`: native setup flow and evidence lifecycle.
- `scripts/engine_smoke.py`: useful-answer validation and timing.
- `tests/test_engine_downloads.py`, `tests/SetupGuideChecks.swift`: synthetic upstream and executable native model-state contracts; every native HTTP request is intercepted.
- `tests/test_setup_guide.py`, `tests/test_bundled_engine_api.py`, `tests/test_engine_packaging.py`: regression coverage.
- `scripts/run_simulation_qa.py`: one new synthetic-test classification entry.
- This handoff.

## Validation and limits

Development checks passed: isolated setup/downloader pytest coverage including an executable Swift model-state fixture; mode, engine API, fake startup and synthetic packaging checks; full Swift app build. The initial native build exposed a brace error in the new view, corrected before the successful build. Existing unrelated Swift deprecation/concurrency warnings remain. Final exact-SHA commands, counts, logs and CI belong in the candidate's gate receipt and PR.

Read-only helpers `/root/engine_runtime_review` and `/root/setup_ux_review` inspected the release and repair. Their preflight findings were recorded, repaired by the sole builder and re-reviewed. Those advisory reviews do not replace the independent Release Auditor or specialist QA.

**Actual engine launch, model download, first response, memory fit and measured setup latency remain unverified.** The existing Claude model-work resource reservation requires Wisp closed and forbids competing engine/model work. This investigation did not launch the released app/engine, query or modify the running external engine, access model weights or user state, replace the installed app, release, deploy or merge.

Required before claiming working runtime delivery: exact frozen-head mechanical evidence and configured CI, independent Release Auditor, synthetic specialist QA, and a separately scheduled resource-safe real-model run with isolated state. Exercise engine start, a fresh public download, discovery, Apply, a complete visible response, restart/reopen, failure/retry/cancel and timings. The real-model gate is **INCONCLUSIVE** until that run occurs.

The shared Local checkout and email/behavior workstreams remain untouched. No `service/main.py`, general Settings, model catalogue, inference transport, engine manager or packaging-pipeline changes are included. Reconcile with current main before final candidate freeze; any subsequent source change invalidates commit-bound evidence.
