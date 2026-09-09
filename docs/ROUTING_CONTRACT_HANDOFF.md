# SIM-ROUTE-1..4 routing contract repair

## Candidate and authorization

- Trigger: twelve reproduced routing check failures blocking Simulation QA #12 and production #17 validation.
- Base: `51fa3ec937df7a19961fbb2103a7654bb058ec74`; fetched and rechecked before publication, with no newer main commit to reconcile.
- Branch: `codex/maintainer-routing-contracts`, isolated worktree. The exact final candidate SHA is recorded in the draft PR and Orchestrator handoff; use that SHA for independent gates.
- Sole writer: Repository Maintainer. Orchestrator acknowledged the original eight paths, then separately acknowledged `service/tasks/compiler.py` and `service/tasks/engine.py` before either was edited.
- Frozen C-1 `3037f1a7d5f6998b6cb44f33e8d979d40520056d` and C-2 `8c1759559b3630f2d0111a7dcbc0ec7dd5b06a00` were preserved. Neither is included in this branch.

## Outcome

Seven genuine alias failures are repaired:

- Adding a line/items to a meeting note reaches note search/append, not calendar creation. A note edit inside a future reminder remains reminder content; ordinary calendar creation remains available.
- Reminder-specific reads reach `search_reminders`, including overdue active records and excluding calendar events. Availability/join and compound-source routes retain their distinct tools.
- Valid reschedule inflections survive typo normalization. Rescheduling exposes updates, not creation. Both event and reminder updates remain available when both targets are requested; missing target/time details can be clarified without a forced ungrounded update.
- Capability lists accept “send texts,” remain registry-backed and non-executing, and do not enter reminder creation/clarification. An addressed send request is not mistaken for a capability list.

The eighth alias exposed a stale tool identity and a real clock bug. The acknowledged contract retains the shared receipt-verified reminder workflow, but recognizable unsupported/ambiguous clock forms such as “half six tomorrow” must clarify rather than silently create a 09:00 reminder.

The same bounded guard applies to all relevant entry points in this repair:

1. Legacy reminder time recognition/resolution and final routing: clarification has only `get_upcoming`, no creation tool, direct call, forced tool, creation obligation, or guessed argument binding.
2. Typed reminder creation/update compilation: unresolved time remains a missing slot; no ready guessed-time plan.
3. Pending typed reminder time replies: return clarification before changing the target, subject, time, or persisted plan. A later precise reply advances the same plan.

The compiler/engine additions matter because typed tasks run before ordinary routing in `main.py`. A router-only change would have left a demonstrated bypass. Supported explicit times, date-only defaults, relative lead times, cancellation, new requests, and colon-containing reminder titles are covered by regression tests. Outbound scheduling and the shared temporal engine were not changed.

## Test-oracle corrections

No failing check was simply skipped to obtain a pass.

| Finding | Evidence-backed replacement |
| --- | --- |
| Old alarm tool identity | Exact final reminder-clarification assertions, plus typed initial/follow-up and attempted-create rejection tests. No general equivalent-tool allowlist. |
| Two bulk-file assertions | Exact `find_files` + `organize_files` menu; separate discovery/organization obligations; discovery bindings `kind=''`, `content=False`; no shell/single-move escape hatch. Existing withheld-versus-ungranted loop assertions remain. |
| Missing memory aliases inferred as unreachability | Actual final-route proofs for `search_conversations` and `clear_memory`, plus a read-negative filesystem-deletion check. Count only each demonstrated target, not its incidental menu. Other inventory protections remain. |
| Embedding failure patched under lexical default | Select and instrument each provider; assert one invocation and exact nonempty static-core fallback for both exception and empty output. Assert lexical independence from an inactive embedding provider. |

The bulk contract comes from `d32e80f` and the preview-token behavior documented in `REPLAY_FAILURE_REMEDIATION.md`. The default lexical provider also came from `d32e80f`. Missing memory aliases are a separate recall regression from `91ebe73`; metadata restoration and write-gate changes are deliberately excluded.

## Changed files

| Path | Responsibility |
| --- | --- |
| `service/router/router.py` | Intent/precedence fixes and exact reminder-clarification routing |
| `service/reminder_intent.py` | Bounded unsupported-clock and time-answer recognition |
| `service/tasks/compiler.py` | Guard typed reminder create/update time compilation |
| `service/tasks/engine.py` | Guard pending typed reminder time replies before mutation |
| `tests/test_alias_reachability.py` | Strict alias checks and explicit shared-alarm clarification contract |
| `tests/test_forced_step_withholding.py` | Exact bulk contract; preserved execution-boundary assertions |
| `tests/test_router_scoping.py` | Demonstrated aliasless-tool reachability and negative gate proof |
| `tests/test_semantic_routing.py` | Provider-specific failure/empty safety-net tests |
| `tests/test_routing_contract_regressions.py` | New final-route, real fixture-source, typed-plan, continuation, and mocked-loop regressions |
| `docs/ROUTING_CONTRACT_HANDOFF.md` | This handoff |

## Verification

All baseline and final runs used a separate process per file, temporary `WISP_HOME` before service imports, temporary filesystem/store fixtures, and intercepted effects. A local validation wrapper blocked network connections, external subprocesses, reads of real `~/.moe`/`~/.omlx`, and file writes outside disposable state (apart from `/dev/null`). It pointed the optional host template at a nonexistent fixture. No models, native apps, real communications, or installed Wisp state were used.

Original scripts were executed directly because their manual counters are not reliably graded by pytest:

| Script | Baseline passed / failed | Final passed / failed |
| --- | ---: | ---: |
| `test_alias_reachability.py` | 81 / 8 | 92 / 0 |
| `test_forced_step_withholding.py` | 14 / 2 | 22 / 0 |
| `test_router_scoping.py` | 147 / 1 | 151 / 0 |
| `test_semantic_routing.py` | 19 / 1 | 32 / 0 |

Final targeted pytest matrix: **486 passed, 1 skipped, 41 subtests passed**, across 29 files. The skip is the existing opt-in local Ling integration test in `test_reminder_creation.py`; it is not counted as a pass. The matrix covers:

- Replay failures, workflow engine/bindings, typed reminder create/update/complete/delete, typed message/email/reply flows, reply bridge, outbound clarification and scheduled-send claims.
- Tool-calling invariants, email scoping, briefing/source sync/readiness-adjacent contracts, outbound payloads and daily-summary delivery.
- User-reported regressions for September 2, 3, 3 noon, and 8; lexical retrieval; router execution contracts; historical, compound and contextual outbound routing.
- The new regression file: **14 test methods and 41 subtests**, also run directly with unittest.

Additional direct legacy runs: router execution contract **20**, execution loop **4**, tool outcomes **6**, sync readiness **10**, reminder bulk clear **6**, email scoping **28**, brief fallback **56**, timeranges **87**, reminder update **18**; all passed. These overlap some pytest-covered behavior and are not added as distinct pytest cases. Adversarial corpus validation passed for **85 prompt records**, including **47 marked intercepted-plan cases**; this checks corpus shape/safety, not live model execution.

AST syntax checks passed for all nine changed Python files; `git diff --check` passed.

Validation failures encountered and resolved before final verification:

- New near-neighbor tests/review found overdue lookup handling, forced incomplete updates, dual-target rescheduling, reminder-content precedence, and clock-guard false positives/new-request interception. Fixes retain the negative assertions and add scripted clarification/creation-rejection checks.
- The temporary validation wrapper initially rejected pytest temporary captures and `/dev/null`; its local guard was corrected without changing repository test infrastructure.
- Four legacy-only files returned pytest exit 5 (“no tests ran”). They were then run directly and passed: tool outcomes, sync readiness, adversarial corpus validation, reminder bulk clear.
- A new mocked dispatch test initially used the wrong registry attribute; corrected to `Tool.func` and rerun successfully.

## Integration and limitations

- This is a bounded clock rejection guard, not a complete natural-language temporal parser or a change to existing numeric-time conventions.
- Memory alias recall regressions are disclosed but not repaired. Memory schemas, metadata, gates, and storage were not edited.
- The web-search builder owns `web_tools.py`; the Simulation QA builder owns runner/count/host-isolation work. None of those paths or branches was modified.
- Router/reminder changes require fresh #12/#17 integration evidence after review. No dependency on merging C-1/C-2 is required.
- Builder tests and read-only helper reviews are not release approval. Independent exact-SHA Release Audit, Simulation QA, and Live QA remain required. No merge, archive, deployment, or installed-app replacement is authorized by this handoff.
