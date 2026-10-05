# Independent audit: BLOCK

PR [#160](https://github.com/adjad/wisp/pull/160), branch `codex/router-update-1-3`.

Candidate: `3f86187ccfca1a20423506c883acecc6fec59ae1`; base: `4994caa15533c0cf84c07208c9097e4197f2815b`.

**This candidate is not approved.** Four actionable blocking findings follow. Candidate source remained clean and unchanged.

Complete candidate production/schema/harness/test/documentation diff and relevant surrounding context; corpus authoring structure and provenance only. Heldout payloads excluded by assignment. Reused prior review reads; no duplicate full gate.

## Blocking findings

### P1 F1: Reject omitted date, reminder-scope and duration constraints before execution

Validation grounds provided time/scope/minutes fields but does not reliably require requested fields. The shared-time omission check depends on _date_range, which does not recognize explicit ISO dates or named month/year requests. Missing reminder scope becomes all, and supplied all is exempt from grounding. Missing minutes is accepted and the runtime defaults to 30.

**Reproduction:** Actual main.agent executes Read email for 2026-10-01 and Read email for October 2026 as view_emails({}); Read overdue reminders as search_reminders(query='', scope='all'); a 90 minute free-slot request as find_free_time(period='tomorrow') without minutes. Pure planner also accepts explicit scope='all' for the overdue prompt. All report compiled rather than repair/clarification.

**Locations:** service/router/intent/validation.py:236, 248, 256, 317, 333; service/router/intent/compiler.py:90, 110, 132; service/tools/schedule_extras.py:168.

**Required correction:** Validate the presence and equality of every requested supported date/scope/duration before compilation; reject or explicitly clarify unsupported constraints. Bind constraints to their source, and do not rely on the model to declare its own omissions.

**Evidence:** targeted-results.json; main-results.json.

### P2 F2: Preserve complete unquoted query literals

A supplied literal is accepted when it appears anywhere in the request, and lookup validation requires only some query/conversation. Complete-fidelity checks apply to quoted literals and certain sender phrases, so ordinary multiword query text can be truncated.

**Reproduction:** Find notes about audit blueprints with a model intent query='audit' is accepted and actual main.agent executes search_notes(query='audit'), broadening the requested search without a clarification.

**Locations:** service/router/intent/validation.py:234, 282, 284, 290.

**Required correction:** Preserve the complete requested search phrase for supported query forms, or ask for clarification when its bounds cannot be established. A nonempty substring does not prove filter fidelity.

**Evidence:** targeted-results.json; main-results.json.

### P2 F3: Preserve the existing calendar-only tomorrow shortcut

The new shared agenda grammar runs before the existing exact tomorrow rules and emits get_upcoming with period only. The established route for what is up tomorrow? requires calendar_only=True. Omitting it permits reminder readiness/reads as well as calendar reads, and the unconditional shortcut is active independently of the opt-in model planner.

**Reproduction:** Fresh actual-main synthetic run executes get_upcoming(period='tomorrow') with no calendar_only. Parent full regression independently fails tests/test_tomorrow_planning.py on the same argument difference.

**Locations:** service/workflows/reads.py:308, 315; service/router/router.py:6948, 6960; service/tools/assistant_tools.py:335, 339; tests/test_tomorrow_planning.py:18.

**Required correction:** Preserve the established source scope when adding the shared deterministic shortcut, consistently in main read compilation and router routing; retain separate combined-agenda behavior where explicitly intended.

**Evidence:** main-results.json; /private/tmp/wisp-router-update-3f86187-regression.log:2746.

### P2 F4: Classify new test modules in the explicit full Simulation QA manifest

The six new router/overview test modules are absent from SAFE_FULL_TESTS. The full profile correctly fails closed on manifest drift; the candidate cannot satisfy this required gate.

**Reproduction:** Parent completed regression log reports RuntimeError: unsafe full-profile manifest drift and enumerates test_router_intent_core, test_router_intent_main, test_router_intent_workflow, test_router_overview_grounding, test_router_overview_message_scope, and test_router_update_eval.

**Locations:** scripts/run_simulation_qa.py:746, 754; tests/test_simulation_qa_runner.py:19.

**Required correction:** The sole owner assigned by Orchestrator must review and classify the six modules without weakening the fail-closed manifest check. Parent is routing this known mechanical repair; auditor made no edit.

**Evidence:** /private/tmp/wisp-router-update-3f86187-regression.log:2488.

## Checks and evidence

- `PYTHONDONTWRITEBYTECODE=1 /Users/adijain/Desktop/MOE_Project/.venv/bin/python /private/tmp/wisp-router-3f86187-gates-20261005/auditor/targeted_checks.py` (exit 0): Seven fresh synthetic planner cases and two pure scorer cases; omissions reproduced; replacement-sender control rejected with no source calls.
- `PYTHONDONTWRITEBYTECODE=1 /Users/adijain/Desktop/MOE_Project/.venv/bin/python /private/tmp/wisp-router-3f86187-gates-20261005/auditor/main_checks.py` (exit 0): Six fresh synthetic main.agent cases; all compiled broadened/changed requests reach fixture tool execution; no SSE errors.
- `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 /private/tmp/wisp-router-3f86187-gates-20261005/auditor/verify_evidence.py` (exit 0): Both opaque corpus hashes match seal; all three stored replay bundles match their declared raw/gzip hashes and, where declared, manifest hash and byte counts. Initial verifier assumed one checksum schema; auditor fixed its own verifier to support baseline raw_gzip_sha256/byte-count schema. No product/evidence repair.

The parent full gate completed **175/179 modules passing**. Its four recorded failures are:

- `tests/test_multi_source_fallback.py`: Existing heading assertion expects From your calendar and reminders; new output is Agenda. Results themselves remain present. Resolve intended heading contract rather than claiming content loss.
- `tests/test_runtime_peer.py`: Original nested native gate failed before collection because sandbox-exec sandbox_apply was denied by the outer Codex sandbox. Separate same-SHA compatible-launch receipt now reports PASS for all nine synthetic cases, with clean start/end. This resolves that diagnosis; the original 175/179 full-gate result remains unchanged.
- `tests/test_simulation_qa_runner.py`: F4: missing manifest classifications.
- `tests/test_tomorrow_planning.py`: F3: existing calendar-only exact tomorrow contract is broadened.

The separate native-peer PASS resolves its environment diagnosis, not the failed full-suite result. The parent plans a complete compatible-environment gate after repairs. Required CI is unverified in this audit; no pass or unavailability is inferred from the queued assignment snapshot.

Both opaque corpus byte hashes match the existing seal. All three stored replay bundles match their declared raw/gzip hashes and, where declared, manifest hashes and byte counts. No corpus or compressed raw replay rows were deserialized. Handoff/progress/development-result pins match; see `provenance.json`. The auditor verifier initially assumed one checksum convention and was corrected for the baseline raw_gzip_sha256/byte-count schema; no checked-in evidence was altered.

## Nonblocking scorer note

**P3: Original legacy exact metrics do not distinguish numeric JSON types.** Fresh pure-score checks show actual count=True or 1.0 against gold count=1 passes original exact_arguments/first_call/end_to_end under Python equality. Supplemental request_argument and guarded_end_to_end correctly reject both. Preserving originals is explicitly required, so this is a compatibility/interpretation caveat, not a demand to rewrite frozen baselines. Clarify the broad README type-preservation claim and use the typed supplemental metric for type fidelity.

Locations: scripts/eval_router_update.py:221, 228, 240; eval/router-update-20261005/README.md:59. Evidence: targeted-results.json.

## Positive observations and limits

- Model planner remains disabled absent explicit configuration; no activation approval implied.
- Planner validates role target and attributed client/credential transport before status/generation and bounds repair/cancellation.
- Compiler emits registered read capabilities only; active action workflows precede planning and effects remain subject to existing boundaries.
- Repeated read receipts retain source/scope labels and partial errors; inspected digest changes retain counts, actor direction, duplicate provenance and coverage caveats.
- Stored replay is explicitly scripted and not model accuracy; original 72/80 end-to-end and eight annotation limitations are retained.

- No heldout prompt/annotation parsing, comparisons, failure-based tuning or heldout execution. Only opaque hashes and seal/provenance were inspected.
- No actual model call, training, load/unload, native source body, personal data/settings/credentials, real outbound tool, network, application activation, deployment or merge.
- Synthetic intent injection proves local validation/orchestration behavior for the reproduced cases; it does not measure the probability of model omissions.
- Actual resident-Ling validity, model quality, latency/energy and native synchronization remain unmeasured/resource-dependent.
- No duplicate full regression; parent log is evidence and final required-CI/specialist gates remain separate.
- Candidate source unchanged; this verdict applies only to the stated SHA and is invalidated by any repair/new commit.

The bad-intent injection proves a local validation/orchestration defect; it does not measure how often the resident model makes that mistake. The existing sender-replacement control was rejected without a source read. No additional actionable endpoint/credential, outbound or diagnostic-privacy defect was established.

BLOCK; route F1-F4 through Orchestrator for recorded sole repair ownership, then obtain independent review and required exact-new-SHA gates. Preserve frozen corpus/baseline metrics. No repair owner may approve its own changes.

Simulation QA remains held while this verdict is BLOCK.
