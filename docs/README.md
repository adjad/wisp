# Wisp documentation

Start with the root [README](../README.md) for what Wisp is and how to run it.

## System reference

- [WISP_OVERVIEW.md](WISP_OVERVIEW.md) — the long-form system reference: every
  subsystem, what's broken, and how it got that way.
- [ASSISTANT_ARCHITECTURE.md](ASSISTANT_ARCHITECTURE.md) — the assistant layer
  (commitments, scheduler, brief, outbox, connectors).
- [SMART_SEARCH_DESIGN.md](SMART_SEARCH_DESIGN.md) — the four-tier ⌘⇧F search.
- [RESEARCH_TOOL_PLAN.md](RESEARCH_TOOL_PLAN.md) — Research mode's design and
  implementation status.
- [TYPED_TASK_ENGINE_PLAN.md](TYPED_TASK_ENGINE_PLAN.md) — the typed task
  engine that replaced free-form tool selection for common tasks.
- [CONVERSATION_MEMORY_PLAN.md](CONVERSATION_MEMORY_PLAN.md) — conversation
  memory design (proposed; no extraction or migration has run).

## Tools and routing

- [WISP_TOOL_ACTIVATION_GUIDE.md](WISP_TOOL_ACTIVATION_GUIDE.md) — the tool
  inventory and activation checklist, backed by
  [WISP_TOOL_ACTIVATION_INVENTORY.json](WISP_TOOL_ACTIVATION_INVENTORY.json).
- [LING_TOOL_CALLING_RELIABILITY.md](LING_TOOL_CALLING_RELIABILITY.md) — how
  the resident model behaves under tool pressure, and what compensates.
- [routing_stress/](routing_stress/) — the 1,000-prompt routing corpus. These
  Markdown files are the **source**; `scripts/build_routing_stress_suite.py`
  compiles them into `test_fixtures/routing_stress/`, and
  `scripts/report_routing_stress_suite.py` writes results back into
  `routing_stress/results/` (generated, not tracked).

## Build, release and QA

- [build-release.md](build-release.md) — the sealed local candidate build.
- [SIMULATION_QA.md](SIMULATION_QA.md) — the non-mutating release gate.
- [../TESTING.md](../TESTING.md) — manual test plan.

## Audits and backlog

- [WISP_BUG_AUDIT_2026-09-09.md](WISP_BUG_AUDIT_2026-09-09.md)
- [WISP_LATENCY_AUDIT_2026-09-08.md](WISP_LATENCY_AUDIT_2026-09-08.md)
- [OPTIMIZATION_BACKLOG.md](OPTIMIZATION_BACKLOG.md)
- [STABILITY_PLAN.md](STABILITY_PLAN.md)

## Working in this repo

- [../AGENTS.md](../AGENTS.md) — task coordination and worktree rules.
- [CODEX_WORKTREE_BEGINNER_GUIDE.md](CODEX_WORKTREE_BEGINNER_GUIDE.md)
- [orchestration/](orchestration/) — orchestration notes.
- [design/apple-ui-refresh/](design/apple-ui-refresh/) — UI concept prototype
  and exports.

Completed per-branch handoffs are intentionally **not** kept here. They are in
git history; `CHANGELOG.md` is the durable record of what shipped.
