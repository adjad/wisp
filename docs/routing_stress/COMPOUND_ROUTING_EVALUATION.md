# Clause-level compound routing evaluation

Date: 2026-09-01

## Decision

Retain the clause-level router. It raises strict tool-menu accuracy from 40/1,000 to
757/1,000 while keeping routing below 100 ms at p95. The result is still not an
end-to-end task-success claim: this benchmark grades tool reachability and schema
withholding before model execution.

## What changed

- Explicit multi-action requests are split into action clauses.
- Bare semicolons split actions except when they introduce authorization, scope, or
  clarification text such as `I authorize`, `only if`, or `ask me`.
- Each clause is routed independently.
- Exact deterministic routes become ordered singleton obligations.
- Unresolved clauses retain at most five candidates from their scoped route.
- Read clauses cannot admit write, send, delete, shell, network-active, or tool-
  authoring categories without a write/action cue.
- Router-direct calls in a compound request are converted to obligations. They are
  not pre-executed, so dependencies, normal safety policy, and confirmation cards
  remain under the agent loop.
- Quoted and hypothetical explanation-only prompts are excluded from task
  decomposition.

## Strict results

All figures use the same 1,000 reviewed prompts and count uncertainty as failure.

| Router | Strict | All required tools reachable | No forbidden schema offered | Median menu | p95 menu | Median route | p95 route |
|---|---:|---:|---:|---:|---:|---:|---:|
| Whole-request lexical router | 40 | 319 | 462 | — | 22 | 1.68 ms | 50.12 ms |
| Final clause-level router | **757** | **796** | **942** | 13 | 21 | 43.38 ms | 85.53 ms |

The final router improves strict passes by 717 cases. The added routing cost is
about 42 ms at the median and 35 ms at p95. This is small relative to model decode
latency and far below the roughly one-second-per-request reranker measurement.

## Remaining failures

- 204 cases are missing at least one required tool from the offered menu.
- 58 cases expose at least one schema marked forbidden by the corpus.
- 52 cases require one or more tools that are absent from the 166-tool runtime
  registry: `ascii_art_generator`, `business_days_between`, `count_vowels`,
  `human_shape`, and `use_skill`. They remain failures; no substitute is credited.
- Excluding only those 52 structurally impossible cases, strict routing is
  757/948 (79.9%), coverage is 796/948 (84.0%), and schema safety is 890/948
  (93.9%). This adjusted view is diagnostic only; the official result remains
  757/1,000.
- Ten `send_message` and nine `cancel_event` schema failures are conditional
  false-branch cases. The action schema is reachable so the agent can execute it
  if the source result proves the condition true; the fixture proves it false at
  execution time. The menu-only grader still marks these as failures, matching
  the requested preference for false failures over false passes.

## Validation

- Focused automated tests: 63 passed, 1 skipped.
- New compound-routing regressions cover ordered five-action obligations,
  read-only schema withholding, unsent draft isolation, and authorization text
  attached across semicolons.
- Historical debug-prompt fixture: all 22 routes still meet their strict
  contracts through the aggregate historical regression test.
- Final machine-readable result:
  `test_results/routing_stress_compound_router_final.json`.

## Interpretation limits

This run does not call the agent model or execute tools. It verifies that the
right capabilities can be selected, forbidden capabilities are withheld, and the
agent loop receives ordered completion groups. Argument extraction, permission
cards, conditional result handling, tool latency, retries, and final response
quality still require the existing synthetic end-to-end harness. The preserved
full 1,000-case run remains the baseline for those later-stage failures.

## Packaged runtime

The release bundle was rebuilt, signed with `Wisp Dev`, installed at
`/Applications/Wisp.app`, and relaunched. The packaged router and reranker files
are byte-identical to the tested source files. The packaged backend reports
healthy on port 8765, and its effective retrieval configuration is
`{"provider": "lexical"}`. Strict `codesign --verify` still reports
`CSSMERR_TP_NOT_TRUSTED` for the local self-signed development certificate; the
process launches and the backend health check succeeds.
