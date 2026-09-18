# Mini runtime completion handoff

Base: `a8eff22332fc8b5ecbccf8a67a0e7c4d9f742a93`.
Branch: `codex/mini-runtime-completion`.
Draft PR target: `codex/pr46-boundary-repair` (remote rechecked at the exact base).
The final head and PR URL are recorded in the worker's final response and PR body;
this handoff is committed with the implementation, without a self-referential SHA.

## Delivered

- Gateway is the default entrypoint and admits exactly one request; an upstream
  that outlives cancellation retains admission. Unqualified inference refuses.
- Versioned exact 60/2/20 GB resource contract, 150 GB startup free-space minimum,
  permanent 50 GB reserve, model/tokenizer/runtime/profile revision fields,
  8k-then-16k progression, no expert offload or sub-four-bit quantization.
  Fresh supervisor attestation, runtime watchdog, predicted-growth checks and
  same-volume cross-process leases fail closed. No cache deletion exists.
- Disabled scheduler with atomic all-missed-occurrence staging, immutable input
  snapshots/revision, deterministic IDs, clock rollback behavior, pending-first
  recovery, bounded reads, collision refusal and atomic completion/publication.
- Four disabled pure Canvas/study/stocks/research snapshot adapters with explicit
  individual portable qualification, a strict one-tool allowlist and no provider,
  private-data, native, shell, filesystem or outbound execution capability.
- SQLite online backup and validated new-directory restore, atomic no-replace
  publication, default cursor identity rotation or explicit preservation, crash
  recovery and capacity/state refusal. Exact v1-to-v2 transactional migration.
- Arrival qualification and operator backup/restore runbook; no activation.

## Owned files

`mini/README.md`, `mini/RUNBOOK.md`, `mini/COMPLETION_HANDOFF.md`,
`mini/__main__.py`, `mini/build_bundle.py`, `mini/gateway.py`, `mini/http.py`,
`mini/node.py`, `mini/store.py`, `mini/resources.py`,
`mini/resource-contract.json`, `mini/runtime.py`, `mini/adapters.py`,
`mini/backup.py`, `tests/test_mini_http.py`, `tests/test_mini_contract.py`,
`tests/test_mini_resources.py`, `tests/test_mini_backup.py`,
`tests/test_node_runtime_completion.py`, `tests/test_mini_store.py`.

No infrastructure, app, build-support, workflow, central manifest or Local
source paths were edited. Existing mini test fixture edits were explicitly
confirmed by the Control Center. Orchestrator acknowledgements retain this
worker as sole implementation and helper-finding repair owner.

## Mechanical evidence

Interpreter: `/Users/adijain/Desktop/MOE_Project/.venv/bin/python` (Python 3.14).
Only that existing interpreter was used; Local source/state was not modified.

- `python -B -m pytest -q tests/test_mini_resources.py tests/test_mini_backup.py tests/test_node_runtime_completion.py tests/test_mini_http.py tests/test_mini_store.py tests/test_mini_contract.py`:
  **218 passed, 1 failed**. The sole failure is the out-of-scope infrastructure
  exact-file allowlist rejecting new bundle members before reaching the expected
  source-only activation denial (`unsupported_artifact_type` versus
  `qualified_offline_candidate_required`). The artifact remains rejected.
- `python -B scripts/test_replay_failure_fixes.py`: **96/98 modules passed**.
  Failing modules: `tests/test_mini_contract.py` (above) and
  `tests/test_simulation_qa_runner.py` (unclassified new test modules).
- `git diff --check`: passed.
- Mini fixture capacity is explicitly synthetic, including child interpreters;
  volume leases use private test roots. Production preflight is unchanged, and
  tests do not assume the runner has 150 GB free or use deployed lock state.
- Tests exercise malformed/overflow telemetry, byte boundaries, forecast
  allocation, stale samples, unavailable state, immutable model policy,
  cross-process/shared-volume exclusion, cancellation-resistant and pre-start
  cancelled transports, SQL-size refusal, batch capacity rollback, concurrent
  ticks, restart, abrupt process exit before/after snapshot publication, live WAL
  backup, restore identity/cursors, schema corruption, links and failed writes.
- Bounded read-only helper reviews identified and rechecked the crash,
  admission, payload-size and shared-volume fixes. These are not independent
  Release Auditor approval. No model or provider was contacted.

Full-gate logs were retained under `/tmp/mini-runtime-regression-final.log` and
focused output under `/tmp/mini-runtime-focused-final.log`; these are local
execution evidence, not portable artifact files or a CI pass.

## Exact integration requests and release blockers

The separate Primary Provisioning Completion owner acknowledged these changes;
they are not copied into this worker's branch:

1. Mirror new `mini/build_bundle.py` members in
   `infra/mac-mini/bundle_contract.py`: `resources.py`, `runtime.py`,
   `adapters.py`, `backup.py`, `resource-contract.json`, `RUNBOOK.md`.
2. Register `tests/test_mini_resources.py`, `tests/test_mini_backup.py`, and
   `tests/test_node_runtime_completion.py` in
   `scripts/run_simulation_qa.py` reliability/full-profile classification.
3. Consume the optional paired `--resource-contract` / `--resource-telemetry`
   interface. Existing argv/environment schema and health-only artifact probes
   remain compatible. The build-support health probe must inject synthetic
   `os.statvfs` and a private `mini.resources.LOCK_ROOT` before constructing
   Store, so low-disk/sandboxed CI is not mistaken for hardware qualification.
   Unconfigured POST inference is intentionally unavailable.
4. Re-run the full exact-head mechanical gate after integration; obtain required
   CI for that remote head, independent Release Audit and applicable
   persistence/privacy Simulation/Live QA. **This isolated draft is not
   merge-ready while its integration gates fail.** CI has not been claimed as
   passing; zero configured checks must be recorded unavailable/non-passing.

## Outstanding external qualification and risks

- No real mini, oMLX weight/profile verification, memory/SSD pressure, measured
  request-growth bounds, tokenizer/context qualification, 8k/16k benchmarks,
  sleep/wake, Keychain/Serve, hardware reboot or release artifact qualification.
- Supervisor resource telemetry is an explicit trusted integration interface,
  not a fabricated engine measurement. No oMLX telemetry adapter is included.
  It must independently enforce engine caps, verify exact revisions, own the
  sole backend and storage volume, and reserve storage against unrelated
  processes. Software leases only serialize cooperating owned components.
  Until those facts are qualified, the shipped empty model contract refuses.
- Portable adapters transform explicitly supplied snapshots only. Provider
  fetchers are absent and always refused, including after portable qualification.
  Real provider integration/credentials and primary effect execution require
  separate qualification; jobs/connectors remain disabled and presentation-only.
- Restore never replaces active/existing state. Operator coordination must stop
  the old producer and manage consumer cursor reset/replay before switching the
  supervisor. Full rollback detection requires an external monotonic anchor.
- Atomic publication is implemented for macOS/Linux and refuses unsupported
  OS/filesystem primitives. Private orphan staging artifacts after abrupt death
  are retained for deliberate operator inspection; no silent cleanup/deletion.
- No deployment, service activation, app replacement, force push or merge.
