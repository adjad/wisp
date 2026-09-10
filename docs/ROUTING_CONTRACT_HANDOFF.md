# Routing contract repair: SIM-ROUTE-1..4 / ROUTE19-1..9

## Candidate and authorization

- Branch: `codex/maintainer-routing-contracts`; PR #19; isolated worktree. The exact final SHA is recorded in the PR and Orchestrator handoff.
- Original base: `51fa3ec937df7a19961fbb2103a7654bb058ec74`. Reconciled base: `32fc3346b0a8c8591b3cb736b8ff75387d8e52be`, imported without conflicts in worker merge `18ccb614745d9b834726d87cbb6fa1d42fb97ca4`. No PR or main-branch merge was performed.
- Sole writer: Repository Maintainer, explicitly reactivated by the Orchestrator for ROUTE19-6..9, with SIM19-RESCHEDULE-1 consolidated under ROUTE19-8. This replaces blocked candidate `7f7b0bafc39f41e6725e6b0713168b4936abf599`.
- The original scope expanded, before edits, to typed compiler/engine, loop/registry, and legacy workflow engine. The Orchestrator then acknowledged `service/tools/assistant_tools.py` as the fourteenth file, limited to `update_event` and directly related preparation if needed.
- The approved Calendar repair is the bounded Python fail-closed alternative. No token protocol, native Swift changes, store/schema changes, or edits to `cancel_event` / `_retire` are included. Reconciliation imports upstream work without claiming ownership of it.

## Outcome and capability limitation

Wisp Calendar event updates are unavailable. The old title-matched cancel-and-recreate backend could retire a Reminder or duplicate and reset event title, location, duration, calendar, and other metadata. The synchronized store does not retain enough metadata to guarantee preservation. Supplying more defaults or granting approval cannot make this path safe.

`update_event` retains its compatibility registration/signature but always returns an explicit no-effect limitation. Its model-facing description/schema disclose unavailability; the old retrieval description remains only to avoid unrelated lexical ranking changes. Registry and agent guards stop before approval/dispatch, including required routes and entire eligible model/direct batches. Previously completed unrelated action receipts remain visible. Ordinary Calendar reads, additions, cancellations, and Reminder updates are not disabled. A compound reschedule including an unavailable Calendar update stops before partially applying the request.

Native time-only rescheduling with preserved metadata is a separate future outcome. This candidate does not perform it and does not claim native completion.

## Repairs and evidence

| Finding | Delivered contract |
| --- | --- |
| ROUTE19-1 / SIM19-CLOCK-2 | Separate outer command, subject, and clock evidence. Explicit schedules retain subject counts and colon-containing titles; subject answers are not consumed as time answers. |
| ROUTE19-2 / SIM19-CLOCK-1 | Validate unsupported clocks during initial compilation and actual pending-time consumption. Correction prefixes cannot bypass the guard; pending identity/time stays unchanged until a precise answer. |
| ROUTE19-3 / SIM19-TYPED-1 | Shared bounded capability inventories decline typed creation and preserve pending snapshots. |
| ROUTE19-4 / SIM19-CONTENT-1 | Outer future reminder creation takes precedence over operation verbs inside its subject. Actual present-tense operations retain distinct typed intents. |
| ROUTE19-5 / SIM19-NOTE-1 | Singular/plural note edits and bullet/sentence/line/agenda additions reach search/append without Calendar creation authority. |
| ROUTE19-6 | Identify attempted clock spans independently of supported parsing and courtesy text. Half-six, invalid numeric, word-minute, and oh-five forms clarify rather than default. Subsequent subject prose stays out of validation; token boundaries and idempotent extraction are tested. |
| ROUTE19-7 / SIM19-REMINDER-SOURCE-1 | Source heads and independent read clauses define Notes/Reminders obligations. Source-looking nouns and quoted clauses inside query content do not add or redirect sources. Explicit compounds and shared-source lists still require both. Actual async → legacy → router → loop Notes entry succeeds without calling Reminders. |
| ROUTE19-8 / SIM19-RESCHEDULE-1 | No executable Calendar update remains: no source-blind lookup, cancellation, duplicate retirement, approval, or Hub/native publication. Empty/degraded discovery classifies as no-match/needs-input/failure, not successful identity proof. |
| ROUTE19-9 | No title query or guessed 60-minute/blank metadata bindings. Stored Calendar and Reminder fields remain unchanged. Insufficient preservation metadata yields honest unavailability even at direct backend entry. |
| SIM19-LEGACY-1 | Inventories decline before legacy mutation; valid continuations resume the same workflow. Literal capability words inside messages/reminder subjects remain content. |

Earlier alias/test repairs remain: valid reschedule inflections survive typo normalization; overdue reads reach `search_reminders`; inventories include “send texts”; alarm aliases assert the shared reminder clarification contract. Bulk-file tests enforce exact `find_files` + `organize_files` discovery/organization obligations and bindings. Memory alias tests prove final-route reachability instead of counting absent metadata. Retrieval exception/empty tests instrument lexical and embedding providers separately. No failing check was merely skipped or broadly allowlisted.

## Owned changed files

| Path | Responsibility |
| --- | --- |
| `service/router/router.py` | Intent precedence, clarification, source/query scope, unavailable event-update contract |
| `service/reminder_intent.py` | Shared capability/outer-command predicates and clock evidence validation |
| `service/tasks/compiler.py` | Typed outer-intent precedence and safe time compilation |
| `service/tasks/engine.py` | Preserve pending identity/time roles; decline inventories |
| `service/agent/loop.py` | Effect ordering/deduplication and unavailable preflight with truthful prior receipts |
| `service/tools/registry.py` | Update effects, unavailable dispatch guard, empty/degraded discovery outcomes |
| `service/tools/assistant_tools.py` | Disclosed unconditional no-effect update compatibility boundary |
| `service/workflows/engine.py` | Decline inventories before legacy mutation |
| `tests/test_alias_reachability.py` | Strict aliases and shared alarm clarification |
| `tests/test_forced_step_withholding.py` | Exact bulk contract and retained execution-boundary assertions |
| `tests/test_router_scoping.py` | Aliasless reachability and negative gate proof |
| `tests/test_semantic_routing.py` | Provider-specific failure/empty safety nets |
| `tests/test_routing_contract_regressions.py` | Actual async/legacy/loop/backend fixtures, continuations, source roles, metadata/no-effect assertions |
| `docs/ROUTING_CONTRACT_HANDOFF.md` | This handoff |

## Builder verification

Focused final source check: **35 pytest parent methods passed; 194 subtests passed**, using disposable `WISP_HOME` before imports, fake model/approval, real disposable SQLite, intercepted Hub/native effects, and a wrapper blocking network/subprocess access and real Wisp/model state. This includes the integrated C2 store. Builder evidence is not an independent gate.

The Calendar matrix covers **24 actual entry/fixture combinations**: routed async/legacy/loop, direct Python backend, and registry entry over empty, Reminder-only, Calendar metadata, cross-source duplicate, ambiguous recurring occurrences, syncing, unavailable, and stale fixtures. Every combination leaves all SQLite row fields unchanged, never asks approval, and never publishes mutations. Mixed model/direct batches stop before approvals/effects; prior-round unrelated receipts remain visible.

Before repairs this activation reproduced **13 failing clock/source checks** and **24 failing Calendar backend subcases**. Read-only helpers additionally found courtesy word clocks, subject-tail/token-boundary false positives, single-quoted query/source-location errors, and mixed-batch/prior-effect narration gaps; each has a regression. Final review reproduced three overlapping clock/date idempotence failures and repaired them by preserving source-order span unions. A misplaced test insertion briefly caused a collection error and was corrected before rerunning. Obsolete successful cancel-and-recreate assertions were replaced with the expressly approved unavailable contract, not waived.

Integrated runner snapshot at clean `18ccb614745d9b834726d87cbb6fa1d42fb97ca4`:

- Unchanged combined seven-profile Python command: **55 gates passed**, **1,595 reported passing checks, 1 skip**; one gate does not publish counts. `test_paths_override.py` passed **24 checks** with the integrated interpreter fix. Report: `/private/tmp/wisp-pr19-18ccb61-targeted.json`.
- Unchanged `full` command: **blocked before test execution** by reviewed-manifest drift for `test_assistant_migrations.py`, `test_broad_web_search.py`, `test_routing_contract_regressions.py`, and `test_shell_boundary.py`. It exits before writing a JSON report. No manifest was patched or full-profile pass inferred. The Orchestrator retains this dependency for the runner owner after that owner's current independent review.
- Unchanged sandbox `--only-native`: **6 of 7 gates passed**; Mail script compilation failed with the sandbox's `com.apple.hiservices-xpcservice` connection restriction. Four gates reported **164 passing checks**; compile gates do not publish test counts. Report: `/private/tmp/wisp-pr19-18ccb61-native.json`. Mail scripts were compile-only; no real Mail actions ran.
- Before the Orchestrator restricted continuation to unchanged profiles, a supplemental isolated run of the runner's additional tests plus the four new fixture files completed successfully (23 files). This included **12 migration**, **53 broad-web**, **34 then-current routing-regression**, **12 shell-boundary**, and **17 runner** parent checks. This was supplemental builder evidence, not a substituted full profile or independent verdict.

The final source includes the subsequent clock-span union correction. Exact published-SHA targeted/native rerun results and evidence paths are recorded in the final PR/Orchestrator handoff; earlier snapshots above are not asserted as exact-SHA approval. The full manifest dependency remains a release blocker until its owner's repair is reviewed and integrated.

## Integration and residual limits

- Reconciled main contains externally merged C1, C2, web-search, runner, and coordination work. Protected original worktrees were not edited.
- Runner manifest work remains with its existing owner, outside these fourteen files. The old interpreter-portability failure is not carried forward: its actual integrated targeted check passed. The unchanged full profile is independently blocked by manifest drift, not interpreter portability.
- This clock guard is bounded, not a general natural-language temporal parser. Shared temporal parsing and outbound scheduling were not changed.
- Missing memory alias metadata and unrelated historical defects are not repaired here.
- Builder/helper evidence is not release approval. Fresh exact-SHA Release Audit and Simulation QA, then Live QA, remain required. No merge into main, deployment, installed-app replacement, archive, force push, or real-world effect is authorized.
