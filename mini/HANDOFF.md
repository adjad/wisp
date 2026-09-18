# Mini runtime implementation handoff

Base: `c23e9c9e8860222a8aa4b070364a7e17236b3ec8` (verified PR #42 head).
Branch: `codex/mini-only-runtime`. Draft targets the unchanged
`codex/mac-pro-inference-foundation` dependency branch. Final commit/remote head
and PR URL are recorded in the task completion message.

## Delivered

- Standalone `mini` package with fixed-loopback authenticated inference gateway
  and read-only proactive node entrypoints; dedicated consumed environment keys.
- Gateway supports the three remote OMLXClient routes and verified PR #42
  embeddings/rerank callers. No administration, lifecycle, URL aliases, queries,
  forwarded identities, ambient proxy, multimedia fetches or connector execution.
- Bounded intake, concurrency, total request deadline, cancellation/cleanup,
  response size and SSE-line validation. Upstream errors and logs are sanitized.
- Private SQLite WAL/FULL persistence with immutable job schedules and IDs,
  transactional occurrence completion/result publication, collision rejection,
  HMAC cursors, bounded pages and refusal at capacity without eviction.
- Five disabled job kinds, presentation-only effect proposals, no effect path.
- Deterministic mini-only bundle builder with per-file digest manifest. Service
  contract sent directly to primary provisioning task
  `01a0993d-9242-78a2-a2da-2e94b8f884c9`; it owns installer/receiver/Keychain/launchd.

## Changed files

Runtime: `mini/__init__.py`, `mini/__main__.py`, `mini/http.py`,
`mini/gateway.py`, `mini/node.py`, `mini/protocol.py`, `mini/store.py`.
Bundle/docs: `mini/build_bundle.py`, `mini/bundle.json`,
`mini/requirements.txt`, `mini/README.md`, `mini/HANDOFF.md`.
Tests: `tests/test_mini_http.py`, `tests/test_mini_store.py`,
`tests/test_mini_contract.py`.

No native/launcher, `service/**`, infra, root dependencies/locks, PR #42 branch,
Local checkout, live data or installed application was modified.

## Mechanical evidence

Interpreter: `/Users/adijain/Desktop/MOE_Project/.venv/bin/python`, Python 3.14.3;
httpx 0.28.1, uvicorn 0.49.0, pytest 9.1.1. Python 3.13 was not exercised.

- `python -B -m pytest -q tests/test_mini_http.py tests/test_mini_store.py tests/test_mini_contract.py`:
  **81 passed**. All HTTP uses ASGI/MockTransport, no socket listeners. Store tests
  use private temporary synthetic state. Coverage includes credentials, raw paths,
  headers, oversize/deadlines/disconnects, fragmented SSE errors, output limits,
  cleanup grace, restart/corruption/SQLITE_FULL, concurrent capacity, transactional
  rollback, HMAC tampering/rollback, immutable duplicates/collisions and disabled
  jobs. Actual Pro OMLXClient and NodeInbox/poll_once consume synthetic responses.
  Extracted bundle imports run in isolated Python without repository imports.
- `python -B scripts/test_replay_failure_fixes.py`: **90/91 modules passed**,
  overall **FAIL**. The sole failure is `tests/test_simulation_qa_runner.py`;
  details below. Log: `/private/tmp/wisp-mini-regression.log`.
- Python AST syntax and known-secret-pattern scans passed; credential canaries
  also checked in HTTP errors, headers, logs, startup and bundle tests. No dedicated
  gitleaks/detect-secrets executable was available. No credential values printed.
- `python -B -m mini.build_bundle --output NEW_PATH`: deterministic archive built
  and validated by tests (member inventory/digests; refuses overwrite).
- `git diff --check`: passed (final staged check also required before commit).

Initial synthetic tests exposed the truncated-store reset and unsafe SSE error
forwarding; these were fixed and independently re-reviewed. Tests also corrected
WAL keeper and subprocess-import fixtures; final focused tests pass.

## Combined gate integration

The combined candidate includes the mini HTTP, store, and contract suites in the
Simulation QA manifest. Re-run the full gate for every repaired candidate SHA;
prior worker evidence is historical and does not approve this repair.

## Independent review and remaining gates

Read-only helpers covered gateway security/privacy, SQLite durability/concurrency,
PR #42 protocol compatibility and test gaps. Fresh Astra medium reviews returned
security `PASS_WITH_NOTES`, durability `PASS`, compatibility `PASS_WITH_NOTES`.
They used inspected working files and synthetic tests; final exact-SHA top-level
Release Auditor approval and applicable specialist QA remain required. PR CI is
reported separately after creation; no unavailable check is treated as passing.

Provisioning, actual oMLX/Serve exposure, Python 3.13, Keychain, launchd, power-loss,
sleep/wake, performance/model qualification and packaging are untested here. Full
DB/key rollback requires an external monotonic anchor to detect universally;
earlier valid cursors intentionally replay immutable results. SQLite disk stalls
cannot be preempted by asyncio; lock contention is bounded at 250ms. Successful
generated content is untrusted content, not arbitrary diagnostic/secret redaction.

Job execution is deliberately absent: schedules/occurrences are durable seams,
not enabled connectors or a catch-up worker. Capacity maintenance/restore and
future worker enablement require separately reviewed procedures. No deployment,
installation, real effects, merge, force push or self-archive occurred.
