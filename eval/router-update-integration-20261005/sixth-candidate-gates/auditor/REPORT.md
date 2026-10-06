# Wisp PR #160 — sixth independent audit

**Verdict: BLOCK** at `98916cf5d17fee49c94045e5a0e3a713d47dce39`, base `4994caa15533c0cf84c07208c9097e4197f2815b`, branch `codex/router-update-1-3`.

The original capitalized-title and positive share-tail findings are repaired. Fresh actual-application controls expose two remaining clause-boundary problems. The parent local full gate passes179/179; it does not override these findings.

## R7-negative — P2 — prohibition creates a stored send workflow

**Find notes about amber route and do not share it with Mom** validates and compiles the exact notes read standalone. Actual main performs no interpretation or read. It instead persists a `deliver_summary` workflow with recipient `Mom`, `delivery="send"`, `status="waiting_for_content"`, and asks what to send.

`outbound_verb` (`service/workflows/compiler.py:71`) correctly retains the action text after literal masking, but matches its verb without respecting the negative governor. `compile_new` takes that guard at2491 before structured read admission. Polite do-not/never-share, do-not-forward and never-send reproduce the inversion; “without sharing” and “do not email” paired controls execute the requested exact read. The new endpoint test at `tests/test_router_intent_main.py:619–630` allows zero calls through its `all(...)` assertion and does not check a stored workflow or faithful response.

Keep negative delivery clauses non-authorizing across preflight while retaining the independent positive read and genuine later action guards. Evidence: `application-results.json` negative-54/56/62; `paired-results.json` negative-0/1/3/4/5. No send, confirmation or native execution occurred; the persisted send intention and lost read are proven.

## R8-later-effect — P2 — a later positive action disappears behind negation

**Find notes about amber route and do not delete it and update my reminders** is accepted as a notes-only intent. Actual main emits `intent_disposition="compiled"`, executes registered fake `search_notes(query="amber route")`, and answers from its fixture result. The requested reminder update is neither honored nor clarified. Later “mark my reminders complete” and “clear my reminders” reproduce this reduction.

Query bounds use `_EFFECT_BOUNDARY`, but `_positive_effect_instruction` and `source_requirements` iterate public `_clauses`. Its `_BOUNDARY` excludes supplementary update/mark/clear heads. Those actions stay in the preceding negative clause, which validation skips wholesale at `validation.py:103–104`; the negative span also hides the later reminders noun. Separating the clauses with a semicolon rejects each effect and prevents the read. Negative-only “do not delete it” retains the exact read.

Use complete effect boundaries for intent/action semantics with negation scoped to each clause; preserve the public-routing vocabulary separation. Evidence: `focused-results.json` focus-1/3/5, semicolon controls focus-2/4/6 and negative-only focus-0; `paired-results.json` negative-positive-14/16/17. No actual destructive action or extra-source access occurred.

## Closure and retained checks

Capitalized/lowercase/quoted governed email titles now execute complete strict queries; truncations reject. Ambiguous second unquoted named destinations clarify without any read/send, whole quoted multi-to titles remain data, and explicit personal/address destinations retain guards. Positive share/plus/polite-gerund and local-effect tails never enter structured reads; quoted share remains literal.

Fresh retained controls cover reversed/correct query/date/count tuples and omission rejection, source-free query replacement with inherited date/count, source literals versus extra-domain authority, mixed reads, aliases, calendar-only authority, dates and60minute availability. Original R1–R6/F1–F4/P3 contracts also retain prior full unchanged-code/context review.

Public controls **40/40** pass, including “update me,” explicit search cancellation, punctuation/literal address delivery, payload and later-turn context. Privacy/runtime/overview controls **51/51** pass: three actual G1 clocks, injected-leak rejection, identity before I/O, residency/default-off/kill/repair, planner-only deadline and propagated cancellation, exact counts/provenance/actors/partial coverage, strict message misses and legacy fallback, typed supplemental scoring, UTC/Pacific23h/25h boundaries and restoration. Manifest checks admit179 modules and reject unknown/missing drift; missing-tzset fails closed.

Raw initial expectation counts are application **96/102**, paired **17/38**, focused **14/17**, alongside the passing public/runtime sets. These are correlated synthetic probes, not model accuracy or a release pass rate. Six original date-scope mismatches were auditor fixtures: the existing conservative grammar shares the sole trailing date. Follow-up shared-date and explicit per-source-date controls pass with raw initial outcomes preserved. Some paired probes deliberately test stricter standalone admission despite a separate main workflow guard; only the three focused actual reads above establish R8 execution. The negative-save unsupported-filter clarification is not a third finding. Bootstrap/provenance path corrections and the stalled file-write attempt are preserved in `auditor-fixture-notes.json`; no candidate, gold or scorer edits.

## Provenance, gates and limits

Full **231-path** candidate assessed through prior full independent production/schema/harness/overview reviews plus the complete **58-path** delta/current context. Exactly four authorized source/test paths changed since6df; the remaining delta is owned documentation/evidence. Assignment and handoff pins, unchanged opaque corpus/author bytes and seal, **183** stored/restored archived-entry checks and **10** replay bundles verify. All changed-file hashes and final clean exact HEAD verify. Static helper remains advisory without custody credit.

Parent exact-head full regression receipt verifies exit0, clean head and completion2026-10-05T23:31:16.896049+00:00; parent reports179/179 modules. Receipt SHA256 `29a2187461e29787490c27127aef6ee11b944a514bd224b225cfc1a4067480cc`; complete log SHA256 `966187e0d6a9059b8f4a30bb13deadd48cde419ec04aeae6b15edf784ac8f201`. Auditor did not duplicate it or DEV replay. Python CI run37387940899/job112025875624 is now terminal SUCCESS on this exact head, completed2026-10-05T23:38:44Z; parent reports179/179 modules. Auditor verified the parent-fetched metadata SHA256 `732a0aff1412f72977d158b1c112654b7ed51b587f3b2295941fb10ea63f05f1` and complete opaque log SHA256 `c72f98a6cdf9fa9012dd7f7027dae13a306f361dadb4882e148452521bbb71c4`. macOS artifact CI remains parent-reported running, with no terminal pass inspected. The prior6df Python provider-cancellation CI failure remains unresolved and still requires the retained Claude owner diagnosis/ACK even if new CI passes. No provider-path diagnosis or tests were executed here.

Corpus/author/raw archive/replay payloads remained opaque: hashes and allowed metadata only. No real model/status/endpoint/network/native/personal-data/credential/outbound effects, residency/training/activation, installed-app changes, merge/deploy or publication. Structured planner stays default-disabled; actual-model accuracy, strict JSON support, latency, generalization, resident-resource ACK and applicable live/visual/performance/shipping gates remain separate. Builder80/80 source/exact and72/80 original E2E metadata retain their annotation limitations and zero-applicable measures.

## Handoff

Simulation QA remains held by this BLOCK. Parent should route both findings to the Orchestrator for one recorded repair owner before edits, then freeze a new candidate and obtain fresh exact-SHA gates and independent review. No shipping or QA release follows this report.
