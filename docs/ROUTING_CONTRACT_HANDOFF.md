# SIM-ROUTE-1..4 routing contract repair

## Candidate and authorization

- Trigger: twelve reproduced routing check failures blocking Simulation QA #12 and production #17 validation.
- Base: `51fa3ec937df7a19961fbb2103a7654bb058ec74`; fetched and rechecked before publication, with no newer main commit to reconcile.
- Branch: `codex/maintainer-routing-contracts`, isolated worktree. The exact final candidate SHA is recorded in the draft PR and Orchestrator handoff; use that SHA for independent gates.
- Sole writer: Repository Maintainer. Orchestrator acknowledged the original eight paths, then separately acknowledged `service/tasks/compiler.py` and `service/tasks/engine.py` before either was edited.
- Repair reactivation: independent Release Audit blocked `f88f42617c03b7df5573b5d2d38968af0d62d6e3` on ROUTE19-1..5; independent Simulation reported seven overlapping/extended findings and supplemental SIM19-LEGACY-1. The Orchestrator assigned every finding to this writer and explicitly acknowledged `service/agent/loop.py` and `service/tools/registry.py` before editing them, solely for `update_event` effect ordering, duplicate prevention, and honest backend-receipt completion. It then acknowledged `service/workflows/engine.py` solely for the confirmed legacy capability-interception boundary. Total scope is thirteen files.
- Frozen C-1 `3037f1a7d5f6998b6cb44f33e8d979d40520056d` and C-2 `8c1759559b3630f2d0111a7dcbc0ec7dd5b06a00` were preserved. Neither is included in this branch.

## Outcome

Seven genuine alias failures are repaired:

- Adding lines/items/bullets/sentences to meeting note(s) reaches note search/append, not calendar creation. A note edit inside a future reminder remains reminder content; ordinary calendar creation remains available.
- Reminder-specific reads reach `search_reminders`, including overdue active records and excluding calendar events. A compound reminder-plus-notes lookup requires both sources. Availability/join routes retain their distinct tools.
- Valid reschedule inflections survive typo normalization. Rescheduling exposes updates, not creation. Both event and reminder updates remain available when both targets are requested; missing target/time details can be clarified without a forced ungrounded update.
- Capability lists accept “send texts,” remain registry-backed and non-executing, and do not enter reminder creation/clarification. An addressed send request is not mistaken for a capability list.

The eighth alias exposed a stale tool identity and a real clock bug. The acknowledged contract retains the shared receipt-verified reminder workflow, but recognizable unsupported/ambiguous clock forms such as “half six tomorrow” must clarify rather than silently create a 09:00 reminder.

The same bounded guard applies to all relevant entry points in this repair:

1. Legacy reminder time recognition/resolution and final routing: clarification has only `get_upcoming`, no creation tool, direct call, forced tool, creation obligation, or guessed argument binding.
2. Typed reminder creation/update compilation: unresolved time remains a missing slot; no ready guessed-time plan.
3. Pending typed reminder time replies: return clarification before changing the target, subject, time, or persisted plan. A later precise reply advances the same plan.

The compiler/engine additions matter because typed tasks run before ordinary routing in `main.py`. A router-only change would have left a demonstrated bypass. Supported explicit times, date-only defaults, relative lead times, cancellation, new requests, and colon-containing reminder titles are covered by regression tests. Outbound scheduling and the shared temporal engine were not changed.

## Independent gate repairs consolidated before replacement freeze

The first candidate's passing tests were insufficient: several checked routing after typed preparation had already intercepted the request. The replacement tests invoke actual `prepare_task_turn_async(..., allow_native=False)` first, inspect the disposable persisted plan, and only route requests that preparation declined.

| Release Audit / Simulation ID | Repair and regression evidence |
| --- | --- |
| ROUTE19-1 / SIM19-CLOCK-2 | Separate the outer reminder command, content and explicit temporal suffix. `tomorrow at 9am ... reserve a table for six` keeps the full subject and 09:00. Subject answers containing `1:1`, `half six`, or `25pm` are not consumed as time answers. Malformed suffix tokens cannot be clipped into valid defaults. |
| ROUTE19-2 / SIM19-CLOCK-1 | Validate clock evidence at the actual pending time-consumption boundary, independently of positive correction-prefix matching. Initial invalid numeric clocks and `six thirty`, plus `actually`, `around`, `please make it`, and `make it around` corrections, remain non-executable with unchanged pending plans. Precise follow-ups still advance the same task. |
| ROUTE19-3 / SIM19-TYPED-1 | Share the bounded capability-inventory predicate between router, compiler and typed engine. Both original inventories and `or` / `Are you able to` dated variants decline typed compilation and remain capability-only routes; pending typed state is preserved. |
| ROUTE19-4 / SIM19-CONTENT-1 | Outer creation commands take precedence over delete/complete/update verbs inside future reminder content. Existing dentist fixtures remain untouched; immediate and two-turn preparations produce only creation steps. Actual present-tense operations retain their distinct typed intents. |
| ROUTE19-5 / SIM19-NOTE-1 | Singular/plural notes and ordinary bullet, sentence, line and agenda-item additions reach note search/append without calendar-creation authority. |
| SIM19-REMINDER-SOURCE-1 | Compound overdue-reminder plus Notes lookup offers only the two requested source tools and requires each separately. A mocked response after one source cannot claim both were checked. |
| SIM19-RESCHEDULE-1 | Complete, single descriptive event targets with supported explicit clocks require ordered calendar lookup and update. Bind current-source scope/title and destination time before approval. `update_event` is classified as an effect, rejects early/duplicate calls, and succeeds only with both cancellation and creation backend receipt stages. No-call, read-only, no-match, ambiguous, denied, planned, error, arbitrary, and partial-receipt cases cannot claim a successful update. Incomplete or linguistically ambiguous requests can still clarify. |
| SIM19-LEGACY-1 | The shared capability predicate declines inventories before legacy workflow creation or slot mutation. Actual typed → legacy → router entry tests preserve pending time/channel/recipient snapshots; a legitimate answer resumes the same workflow. Capability words inside an addressed literal message or reminder subject remain content. |

The backend calendar operation remains cancel-plus-recreate, not atomic. Its creation receipt follows bridge publication, not native EventKit acknowledgement. Successful narration therefore says the request was submitted and native completion is not confirmed; it preserves the backend receipt instead of accepting stronger model prose.

The time-only event route requires complete destination recognition; it cannot drop `October 1`, `next week`, timezone/alternative/duration suffixes and bind a bare-clock default. Optional mutation arguments are fixed to the existing backend defaults, preventing model-injected rename/location/duration edits. This does not preserve existing event metadata: the unchanged backend recreates with the supplied query title, default 60-minute duration and blank location.

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
| `service/reminder_intent.py` | Shared capability/command-content predicates and bounded clock validation |
| `service/tasks/compiler.py` | Typed outer-intent precedence and time compilation |
| `service/tasks/engine.py` | Preserve pending identity/time roles and decline capability inventories |
| `service/agent/loop.py` | `update_event` prerequisite/duplicate guard membership and backend-receipt narration |
| `service/tools/registry.py` | `update_event` effect and two-stage receipt classification only |
| `service/workflows/engine.py` | Decline capability inventories before legacy workflow mutation |
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

Final targeted pytest matrix: **501 passed, 1 skipped, 137 subtests passed**, across 29 files. The skip is the existing opt-in local Ling integration test in `test_reminder_creation.py`; it is not counted as a pass. The matrix covers:

- Replay failures, workflow engine/bindings, typed reminder create/update/complete/delete, typed message/email/reply flows, reply bridge, outbound clarification and scheduled-send claims.
- Tool-calling invariants, email scoping, briefing/source sync/readiness-adjacent contracts, outbound payloads and daily-summary delivery.
- User-reported regressions for September 2, 3, 3 noon, and 8; lexical retrieval; router execution contracts; historical, compound and contextual outbound routing.
- The new regression file: **29 test methods and 137 subtests**, also run directly with unittest. Parent-test and subtest counts are distinct.

Additional direct legacy runs: router execution contract **20**, execution loop **4**, tool outcomes **6**, sync readiness **10**, reminder bulk clear **6**, email scoping **28**, brief fallback **56**, timeranges **87**, reminder update **18**; all passed. These overlap some pytest-covered behavior and are not added as distinct pytest cases. Adversarial corpus validation passed for **85 prompt records**, including **47 marked intercepted-plan cases**; this checks corpus shape/safety, not live model execution.

Additional dispatch-boundary scripts also passed directly: tool dispatch **8**, router-direct dispatch **46**, and direct-dispatch execution **27** checks.

AST syntax checks passed for all twelve changed Python files; `git diff --check` passed.

Validation failures encountered and resolved before final verification:

- New near-neighbor tests/review found overdue lookup handling, forced incomplete updates, dual-target rescheduling, reminder-content precedence, and clock-guard false positives/new-request interception. Fixes retain the negative assertions and add scripted clarification/creation-rejection checks.
- The temporary validation wrapper initially rejected pytest temporary captures and `/dev/null`; its local guard was corrected without changing repository test infrastructure.
- Four legacy-only files returned pytest exit 5 (“no tests ran”). They were then run directly and passed: tool outcomes, sync readiness, adversarial corpus validation, reminder bulk clear.
- A new mocked dispatch test initially used the wrong registry attribute; corrected to `Tool.func` and rerun successfully.
- Audit reactivation reproduced **24 failing async-entry subcases** before source fixes. The consolidated Simulation extensions added **15 failing subcases** before their repairs. Both sets remain in the final regression file.
- Repair review found suffix clipping, title-answer/clock-role regressions, partial date consumption, and unbound optional event mutations; all were fixed with permanent controls. A missing classifier import and misplaced test blocks were corrected before the final matrix. A mistyped legacy test filename was corrected to `test_execution_contract_loop.py`, which passed all four checks.

## Integration and limitations

- This is a bounded clock rejection guard, not a complete natural-language temporal parser or a change to existing numeric-time conventions.
- Backend calendar receipts do not prove native completion; cancel-plus-recreate may be partially applied on failure. Native app code and `assistant_tools.py` are unchanged.
- The earlier `ROUTE19-LEGACY-CAPABILITY-OBS` was independently confirmed as SIM19-LEGACY-1 and repaired only after explicit scope acknowledgement. Existing legacy cancellation/denial/uncertain/duplicate protections were not rewritten and remain covered by the workflow/outbound matrix.
- Memory alias recall regressions are disclosed but not repaired. Memory schemas, metadata, gates, and storage were not edited.
- The web-search builder owns `web_tools.py`; the Simulation QA builder owns runner/count/host-isolation work. None of those paths or branches was modified.
- Router/reminder changes require fresh #12/#17 integration evidence after review. No dependency on merging C-1/C-2 is required.
- Builder tests and read-only helper reviews are not release approval. Independent exact-SHA Release Audit, Simulation QA, and Live QA remain required. No merge, archive, deployment, or installed-app replacement is authorized by this handoff.
