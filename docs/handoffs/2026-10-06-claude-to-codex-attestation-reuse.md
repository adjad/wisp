# Handoff: reuse the oMLX attestation (Wisp 1.3 performance item)

From: Claude (session 3f2d7b8f). To: Codex. Date: 2026-10-06.
Status: **design written, security-reviewed by nobody yet, no product code written.** This is NOT part of Wisp 1.2.0.

## Why this matters

Every request Wisp sends to the local oMLX engine (`127.0.0.1:8000`) first proves, from scratch, that the process on the other end is the qualified desktop oMLX. After the 1.2.0 transport work (scandir walker, concurrent inspections, `proc_pidpath`) that proof still costs about **0.9 s per request**. The cost is not the security checks themselves; it is re-deriving facts that only change when oMLX restarts or its files change:

| Where the time goes (per POST, 1.2.0) | ms |
| --- | --- |
| `RuntimeAuthority.load()`: two walks of the 47,320-entry oMLX Python tree, group lookup, ~8 `lsof`/`ps`/codesign spawns | ~463 |
| connect-time `binding()` (4 spawns) | ~39 |
| three peer checks (connect, header write, body write), each = 2 `binding()` + 2 `lsof` ESTABLISHED snapshots | 3 x ~137 |

A "hello" turn made 3 engine calls before PR #162 and now makes 2; each call pays this. The design takes attestation to **~41 ms per POST / ~40 ms per GET** (arithmetic from measured parts, not an end-to-end run), about 0.17 s of attestation for a hello turn, by:

1. **P1** exec-aware incarnation facts + a cheap per-write "fence" in place of the two per-write `binding()` calls.
2. **P2** one qualified authority per oMLX incarnation, revalidated natively on every use (0.06 ms), re-walked in the background at most every `W_hard` = 60 s.
3. **P3** native socket-owner lookup (`proc_pidfdinfo`, 0.027 ms) instead of `lsof` snapshots, default off / shadow mode.
4. **P4** flip P3 to on after audit and clean shadow evidence.

## What exists

- Design (351 lines, evidence labelled [M]easured / [E]arlier / [S]tatic / [D]erived / [H]ypothesis): branch `origin/claude/perf-attestation-design`, commit `1449ab1`, file `docs/PERF_ATTESTATION_DESIGN.md`; probes and raw outputs in `docs/perf-attestation-evidence/` (`probe_exec.py`, `probe_sockets.py`, `probe_live.py`, `layout.c`). Copy outside git: `/private/tmp/wisp-perf-attestation-design-20261005.md`.
- Earlier plan and invariants I1-I10: `/private/tmp/wisp-claude-performance-coordination-20261002/05-transport-t0-hardened-implementation-plan.md` (the design extends it with I3' and I11-I20).
- Performance evaluation with the measured numbers: `/private/tmp/wisp-1.2.0-performance-eval.md`.
- The code it changes is `service/inference/attributed_transport.py` and `service/inference/local_peer.py` (Desktop adapter only; Managed, `runtime_manifest`, the mini gateway, keep-alive and `omlx_client.py` stay as they are). Pinned tests: `tests/test_runtime_attestation_t0.py`, `tests/test_runtime_proc_path.py`, `tests/test_runtime_peer.py`, `tests/test_web_response_followup.py`.

## This is a security-boundary change

The attestation is what stops a rogue process on port 8000 from receiving the oMLX bearer credential and the user's prompts. The design never changes which facts are checked, only how and when they are read. Two findings from the experiments that you must keep:

1. `(pid, start time)` is **not** an incarnation: `exec` keeps both. Any compare must include comm, executable path, code-signing flags, cdhash, signing identifier and team for the server and its parent (all change on exec into another binary; exec into the *same* binary changes nothing natively; see residual R2).
2. `time.monotonic()` does **not** count sleep on macOS. Use `time.clock_gettime(time.CLOCK_MONOTONIC)` for every freshness bound.

## First steps for you

1. **Adversarial security review of the design** (read-only, independent of whoever builds). My reviewer died on a usage limit before reporting, so nobody has attacked it yet. Probe especially: the exec-aware fact set (is anything missing?), the `FP_SHARED` fd-sharing check, the fail-closed fallbacks, refresh/eviction races, the sleep/wake clock, and whether R1/R2 are acceptable.
2. **The user must decide D1-D4** (my recommendations in brackets): D1 residual window for tree/group changes `W_hard` [60 s]; D2 accept R2, argv[0]/same-binary exec not re-checked per write [accept]; D3 native owner lookup rollout [shadow, then on after an Auditor PASS and >= 500 clean shadow checks]; D4 amortize the connect-time `binding()` [no, revisit after P4].
3. Implement P1 -> P4 as separate PRs, each with the T0 gates: red tests first, an independent Auditor verdict on the exact head, required CI on the exact head, mutation set from section 5.2 (every survivor killed or listed), kill switch `WISP_ATTESTATION_LEGACY=1`. Kill criterion from the T0 plan: under 25% overhead reduction means do not merge.

Residuals you are asking the user to accept: **R1** a tree-permission, owner, link or group change is refused at most `W_hard` after the last walk started (today: at the next connection, roughly 3.5 s). Same-user tampering was never detected and still is not. **R2** above.

## Ownership and coordination

- `service/inference/**` was mine for the 1.2.0 performance work. I am handing the attestation files (`attributed_transport.py`, `local_peer.py`, their tests) to you for this item once you acknowledge it in the mailbox. I will not edit them until you release them.
- Your router overhaul (1.3) touches other paths; no overlap expected. `scripts/run_simulation_qa.py` and `CHANGELOG.md` are shared hot files: single-line insertions, resolve on merge.
- Base your branch on current `origin/main`, **not** on `4994caa`: since then `attributed_transport.py` changed in #161 (`_close_inspection_pool` is now a synchronous `shutdown(wait=False)`; closing a transport adds no suspension point; `tests/test_runtime_attestation_t0.py` gained tests for it). #162 added `service/inference/engine_epoch.py` and `readiness.py` reuse of the model-residency proof; it is unrelated but proves a similar "reuse only while nothing could have changed" pattern.
- Measure with the release benchmark (`scripts/release_performance.py`, `docs/RELEASE_PERFORMANCE_BENCHMARK.md`), not a microbenchmark. PRs #163 and #164 and a policy/corpus change are in flight to make it runnable; the live path was only just exercised. It needs a quiet Mac and the one resident model (`Ling-3.0-tiny-oQ6e`); the human grants the idle window. Never touch port 8765 or the installed app in tests.

## Not part of 1.2.0

Wisp 1.2.0 ships without this. Do not merge attestation changes into the 1.2.0 release commit; main may move after the 1.2.0 tag (the tag is placed on the exact measured commit, which stays an ancestor of main).

## Known limits of the design

- Estimated savings are derived, not measured end to end. The first real number will come from the benchmark after P2.
- `getattrlistbulk`, FSEvents, `KERN_PROCARGS2` and a persisted inventory were considered and deliberately rejected or deferred (section 2.2 / 4).
- The native `socket_fdinfo` layout was verified on Darwin 27.0 with the CLT 26.0 SDK; the per-use layout oracle (parsed own-end ports must equal `getsockname`/`getpeername`) is what keeps it safe across macOS updates.
