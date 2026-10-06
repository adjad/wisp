# Independent re-audit: BLOCK

PR [#160](https://github.com/adjad/wisp/pull/160), branch `codex/router-update-1-3`.

Candidate: `40ee88a409eaec943ae4474130bd87263ea794e1`; base: `4994caa15533c0cf84c07208c9097e4197f2815b`.

**This exact commit is not approved.** The original individual omission, shortcut, manifest and timezone repairs work in fresh controls, but two source-scope defects remain. Candidate source stayed clean and unchanged.

Reused the completed prior base-to-3f86187 review, read the full intervening production/harness/test/documentation delta and current surrounding execution context, and verified every current changed-file hash. Reviewed all current source changes; corpus authoring/payloads and archived raw replay rows stayed opaque under the explicit assignment restriction.

## Blocking findings

### P1 R1: Bind dates to each repeated source read, not an independent domain-wide set

The repaired validator collects all time scopes for a sole domain, validates membership/coverage as a set, and separately validates query fields against another domain-wide set. It never binds a sender/query to that occurrence's requested date. Swapping the dates between two individually valid email queries preserves both sets, so the local validator reports compiled and main.agent executes two incorrectly scoped reads. Rendering labels merely disclose the wrong compiled scopes; they do not restore authorization.

**Latest request:** Read email from Elara for 2026-10-03; read email from Tobias for 2026-10-04

**Actual compiled and executed calls:** `[["view_emails", {"query": "Elara", "strict_match": true, "period": "2026-10-04"}], ["view_emails", {"query": "Tobias", "strict_match": true, "period": "2026-10-03"}]]`.

**Positive/control behavior:** The identical request with Elara/2026-10-03 and Tobias/2026-10-04 compiles and executes the two correct scoped reads. Different-domain date swaps are rejected; this is specific to repeated domain entries, not an inability to support valid date scopes.

**Locations:** service/router/intent/validation.py:265, 266, 268, 341, 342, 349, 353, 493, 505.

**Required behavior:** Retain clause/entry identity and validate the complete query/date/account/count tuple for each requested read. Reject ambiguous or altered bindings before any source call; do not take the Cartesian product of authorized filter/date sets.

**Evidence:** `fresh-scope-results.json`, case `repeated-domain-date-swapped`; full exact supplied intent, fake model output, SSE, scopes and fixture execution receipts are retained.

### P2 R2: Invalidate the previous search when a source-free correction supplies a new query

The current correction inherits the notes domain from context, but _unquoted_queries requires a source word in the same prompt. 'Actually about ...' therefore fails current_named; filter_evidence retains the prior request, and the stale prior query passes completeness validation. main.agent executes the previous search instead of asking for clarification or using the current query.

**Latest request:** Actually about birch diagrams

**Prior context:** `Find notes about cedar manuscripts` followed by a synthetic `search_notes` receipt.

**Actual compiled and executed calls:** `[["search_notes", {"query": "cedar manuscripts"}]]`.

**Positive/control behavior:** 'Actually find notes about birch diagrams' compiles and executes the new query. The source-free fragment supplied with the correct new query currently clarifies; that is an acceptable conservative fallback. Executing the old query for the same fragment is not.

**Locations:** service/router/intent/validation.py:226, 400, 401, 409, 410, 488, 493, 508.

**Required behavior:** Recognize a new query cue in a source-free correction using the established contextual domain, or clarify without a read when its full bounds cannot be established. Do not let the prior query remain valid after an explicit replacement.

**Evidence:** `fresh-scope-results.json`, case `fragment-query-stale`; full exact supplied intent, fake model output, SSE, scopes and fixture execution receipts are retained.

## Prior findings and UTC repair

- **F1 — PARTIALLY_REPAIRED:** Original isolated date/month omission, reminder-scope omission/all contradiction, and free-slot operation/duration omission now clarify with zero tool calls. Valid constrained reads succeed. R1 remains a blocking source-entry binding defect.
- **F2 — PARTIALLY_REPAIRED:** Complete source-named unquoted queries and date/count suffixes now survive; substrings are rejected. R2 remains a blocking contextual replacement defect.
- **F3 — RESOLVED:** Both shared router and compile_read use personal_agenda_args. Actual-main controls preserve calendar_only=True for tomorrow planning and explicit calendar while keeping a combined agenda request combined.
- **F4 — RESOLVED:** Six modules are explicitly admitted and their fake interfaces/isolation safety reviewed; full manifest selection succeeds. Fresh unknown/missing manifest controls still fail closed.
- **P3 — RESOLVED_DOCUMENTATION:** README accurately describes legacy Python numeric equality and separately typed supplemental comparisons. Fresh bool/float-versus-int scorer cases retain original compatibility results and correctly fail typed supplemental metrics. Frozen originals were not rewritten.
- **UTC_CI — RESOLVED_WITHIN_SYNTHETIC_SCOPE:** Reversible dedicated-process Pacific helper produces exact 23h/25h DST epochs in both UTC and LA process zones, preserves wrong-day inequality, restores prior/absent TZ after exceptions, and fails closed without tzset. Production timeranges source is unchanged.

## Independent verification

- `PYTHONDONTWRITEBYTECODE=1 /Users/adijain/Desktop/MOE_Project/.venv/bin/python /private/tmp/wisp-router-40ee88a-gates-20261005/auditor/fresh_scope_checks.py` (exit 0): 25 independently authored synthetic planner and actual-main cases: 23 expectations met; R1 and R2 negative expectations fail because incorrect reads execute. No SSE errors. Exit 0 means the reproduction program completed; it is not a product PASS.
- `PYTHONDONTWRITEBYTECODE=1 /Users/adijain/Desktop/MOE_Project/.venv/bin/python /private/tmp/wisp-router-40ee88a-gates-20261005/auditor/fresh_boundary_checks.py` (exit 0): 25/25 independent assertion controls passed: timezone, full manifest and unknown/missing drift, default-disabled and mismatched endpoint/provider/transport before I/O, action-vs-read applicability, schema/exclusion rejection and typed scorer.
- `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 /private/tmp/wisp-router-40ee88a-gates-20261005/auditor/verify_provenance.py` (exit 0): Clean exact HEAD; all 78 current changed-file hashes; four corpus/author/seal files byte-identical to prior candidate; four replay bundles verified against declared hashes; all 17 archived evidence bundle entries verified; handoff pins and exact registry assignment verified.

The initial scope run expected an explicit empty query on query-free search_notes; actual registered/compiler default omission is valid. That expectation was corrected. A correctly supplied source-free query may safely clarify, so the positive control permits that fallback. Original initial raw results remain preserved in fresh-scope-results-initial.json. No candidate code, sealed input or builder fixture changed.

First boundary invocation omitted the scorer helper's required expected mapping and exited 1 with KeyError before controls completed. The independently authored test input was corrected; the next run passed 25/25. This was not a product defect or candidate repair.

**Skips:** none in the fresh checks. All source-call receipts are fake; no real tool or model body executes. A reproduction program exiting zero does not mean its negative cases passed; the two wrong-call cases are explicitly false in the sealed results.

The opaque heldout, development, seal and author bytes are unchanged from the prior candidate. All four saved raw/gzip replay bundles and17 archived first-candidate evidence files match declared hashes. Handoff/progress/development-result pins and the exact registry assignment match. See `provenance.json` and `checksums.json`.

## External gates and limits

{
  "full_regression": {
    "status": "FINAL_SUMMARY_OBSERVED",
    "log": "/private/tmp/wisp-router-update-40ee88a-regression.log",
    "description": "Parent log final summary observed; no independent duplicate full gate. Exact receipt/CI remain parent responsibilities.",
    "summary": "Regression gate: 179/179 test modules passed",
    "observed_log_sha256": "b2a186f08eb8ef73d9a29d9a73e38acd76c52c874384f5b554015c86e2cbfad9"
  },
  "required_ci": {
    "status": "UNVERIFIED_BY_AUDITOR",
    "description": "Assignment snapshot queued/starting; no current exact-SHA passing receipt inspected."
  },
  "simulation_qa": {
    "status": "HELD",
    "description": "A nonblocking independent audit and parent release are required before execution; this verdict does not release QA."
  }
}

- The single-read repairs reject omissions while retaining legitimate filters rather than forcing every request to clarify.
- Different-domain dates remain distinct and wrong swaps are rejected.
- Default-disabled planner and identity mismatch controls perform no fake status/chat I/O.
- Current parser/compiler changes do not add outbound capability; active actions retain prior workflow authority.
- Existing digest/rendering and diagnostic boundaries are unchanged by repair commits; prior review of counts/direction/duplicate provenance/coverage/error honesty remains applicable.
- Scripted development 80/80 exact/source and original/guarded E2E72/80 retain eight annotation limitations and zero-denominator caveats; no actual model-validity claim is accepted.

- Fresh injected synthetic intent responses measure local validation/orchestration, not the resident model's error frequency.
- No real model, model endpoint/status request, native source tool, personal data/settings/credentials, real outbound action, training, load/unload, deployment, feature activation, installed-app replacement or merge.
- No heldout opening, evaluation, comparison, failure-derived tuning or corpus regeneration.
- Builder 401/783 test reports and saved scripted replay are prior-stage evidence, not independent approval of this exact commit.
- Actual model validity/generalization, latency/energy, native correctness and training/resource qualification remain unmeasured and separate.
- Any candidate source change/new SHA invalidates this commit-bound audit.

BLOCK at exact 40ee88a. Route R1/R2 through Orchestrator for recorded sole repair ownership; auditor makes no product edit or repair assignment. Obtain independent review and required gates on a newly frozen repaired SHA. Preserve the corpus and all original failed/pass-stage evidence.

Simulation QA remains held while this verdict is BLOCK.
