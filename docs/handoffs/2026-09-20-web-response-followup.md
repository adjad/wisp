# Web response follow-up handoff

- Outcome: repair the 2026-09-20 current-news presentation, immediate stock
  reference continuation, and stale closed-delivery follow-up regression while
  preserving Ling-3.0-tiny-oQ6e as the configured model.
- Exact base SHA: `32d98aad7796a34c3ad9258914b710b8ee8b1d1d`.
- Sole writer: Codex in `/private/tmp/wisp-web-response-repair` on
  `codex/web-response-followup`.
- Owned paths: `service/main.py`, `service/memory/store.py`,
  `service/workflows/engine.py`, `service/workflows/reads.py`,
  `scripts/run_simulation_qa.py`, and focused regression tests under `tests/`;
  this handoff is the only additional path.
- Validation plan: focused web-search, contextual stock, and workflow-engine
  regression tests; then the repository regression gate where the environment
  permits it.
- Dependencies: PR #51 (`codex/pr51-news-provenance`) independently owns the
  same runtime paths for stored-news provenance, including `main.py`,
  `store.py`, and `reads.py`. This repair must preserve its changes during
  post-#51 reconciliation and does not modify model selection or configuration.

## Audit follow-up

- Outcome: preserve Ling-3.0-tiny-oQ6e for dedicated web/news routing, bound
  stock-reference carryover to the immediately preceding stock exchange, and
  enforce compact Markdown link rendering for news payloads.
- Base candidate: `9071277263d1b16925571f590cf4f650f0f55312`.
- Added owned paths: `service/router/router.py`, `service/tools/web_tools.py`,
  and focused routing/news tests. Sole writer remains this worktree.
- Validation plan: focused router, web, stock-follow-up, and replay tests.
- Dependency: PR #51 remains unmerged; do not push this follow-up before its
  reconciliation preserves the provenance boundary.

## Post-#51 reconciliation audit

- Outcome: preserve trusted display-only news artifacts across the agent return
  and persistence boundary; distinguish topical `without` wording from an
  explicit browsing opt-out while retaining dedicated Ling-oQ6e routing.
- Base candidate: `1481dc87bf444dacfafd1b57a5b083ec14bc2ad0`.
- Added owned paths: `service/router/web_request.py`, `service/main.py`, and
  endpoint/router provenance regressions. Sole writer remains this worktree.
- Validation plan: focused endpoint, router, web, workflow, and replay suites.
- Dependency: retain PR #51's display-only provenance model and do not push
  before the hub approves the reconciled candidate.

## Topical-negation audit follow-up

- Outcome: distinguish topic-level network negation from an instruction to
  avoid browsing across explicit searches and current-news questions.
- Base candidate: `d16f07f8695dda14caf5302a62d5e77a22fb0942`.
- Added owned paths: `service/router/web_request.py` and the focused routing
  regression matrix. Sole writer remains this worktree.
- Validation plan: full focused endpoint/router/web/workflow/replay suite.
- Dependency: preserve the display/provenance repair and no-push boundary.
