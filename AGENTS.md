# Wisp Task Coordination

## Working model

- Reserve the Local checkout for integration, final verification, and small follow-up fixes.
- Start each independent implementation outcome in its own Codex Worktree from a clean, known base.
- Keep one primary writer responsible for a file at a time. Do not edit paths owned by another active task.
- Before editing, state the intended outcome, base commit, owned paths, expected validation, and likely integration dependencies.

## Delegation

- Use subagents for independent read-heavy work such as codebase exploration, test-gap analysis, risk review, and summarization.
- Keep subagents read-only unless the parent task assigns an explicit, non-overlapping write scope.
- When parallel work would touch the same files, serialize it through the primary writer instead.
- The parent task must wait for delegated work, reconcile the results, and remain responsible for the final implementation and verification.

## Handoffs

- Finish every implementation task with a concise handoff containing:
  - the outcome delivered;
  - files changed;
  - tests and checks run, including failures;
  - incomplete work and known risks;
  - integration dependencies or possible conflicts;
  - the commit or branch containing the work, when applicable.
- Do not revert, discard, overwrite, or clean up changes that may belong to another task.
- If ownership is unclear, stop writing and ask the Local integration coordinator to resolve it.

## Integration coordinator

- The Local integration coordinator inventories the shared working tree, attributes changes to workstreams, resolves overlaps, runs cross-cutting checks, and organizes coherent commits.
- Feature work resumes in separate Worktrees only after the shared Local checkout has been stabilized.

## Control Center dispatch

- The pinned **Wisp Control Center** is the user's single routine surface for delegation, progress, review, and shipping. Load and follow `$wisp-control-center` from `.agents/skills/wisp-control-center/SKILL.md` for every coordinator action and scheduled status run.
- In the Wisp Control Center, treat requests to "assign", "delegate", "start", or "create" a new task as requests for a separate user-owned Codex task.
- Create each implementation task as a new top-level Worktree task in the MOE_Project project unless the user explicitly requests Local or the work is integration-only.
- Do not perform the delegated implementation inside the Control Center and do not substitute a nested subagent for a top-level task.
- Use nested subagents only when the user explicitly asks for subagents or when they are bounded, read-heavy helpers inside an already-created implementation task.
- After dispatch, report the new task title and keep its progress in the Control Center's status summary.

## Automated delivery

- Do not ask the user to perform routine Git fetch, safe pull, commit, push, pull-request, or merge mechanics.
- A worker must finish with a clean committed branch pushed to `origin`; the Control Center follows up automatically when this completion contract is missing.
- `Ship: <task>` is the user's explicit approval to reconcile and validate that exact task, create or update its pull request, wait for required checks, merge it normally into `main`, verify the remote result, and archive the task.
- Scheduled monitoring reports progress but never invents shipping approval. Destructive Git operations, force pushes, bypassed checks, unrelated changes, and production deployment remain outside this authorization.

## Proactive product autonomy

- The Control Center may autonomously identify and prepare additional high-value Wisp features without feature-by-feature ideation approval.
- Prefer evidence-backed daily-use improvements over speculative scope. Record the user benefit, evidence, bounded outcome, ownership, base commit, and validation plan before dispatch.
- Avoid duplicates and active ownership overlap. Cap proactive implementation at two concurrent Worktrees, reduce that number when safe monitoring would be weak, and prioritize explicit user tasks.
- Proactive work stops at a merge-ready pull request. It does not authorize real-world effects, deployment, installed-app replacement, destructive Git, direct writes to `main`, or shipping without task-specific `Ship` approval.

## Historical backlog recovery

- Inventory accessible MOE_Project tasks across active, idle, not-loaded, and archived states plus remote branches and pull requests. Read enough history and handoffs to classify actual state; titles and summaries are not evidence.
- Exclude outcomes the user explicitly rejected, declined, cancelled, or paused unless the user later reverses that decision with `Resume` or a new request. Before recovering an unfinished valuable outcome, verify it is absent from current `origin/main` and not already covered by an open pull request. Create a fresh Worktree from current `origin/main`; never resume or overwrite an unclear historical Local checkout.
- Prioritize user impact, data or safety risk, and shippability. Cap recovered implementation at three concurrent Worktrees separately from the two-Worktree proactive-feature cap, queue the rest, and reduce either cap when their combined workload would exceed safe ownership, gate, or monitoring capacity. Avoid duplicates and ownership overlap; apply the standard review path and the specialist QA triggers below.
- Archive recovered workers only after verified delivery. Escalate only material product forks, unavoidable credentials or permissions, overwrite-risk ownership ambiguity, or destructive or external action.

## Autonomous Orchestrator

- Use the existing top-level **Wisp Autonomous Orchestrator** as the backend execution supervisor. Its live coordination record is the sole authoritative ownership registry for exact tasks, dependencies, conflicts, stalls, follow-ups, one repair owner per finding, gates, and state changes. Dashboards, scheduled summaries, and initial JSON snapshots are read-only mirrors, never claim authority.
- Feed it all current workers, standing quality roles, proactive work, historical recovery, and production-automation work. Every proposed assignment, claim, or transfer requires the Orchestrator's explicit acknowledgement in that record before dispatch or editing. It coordinates existing owners and must not create duplicate workers for already-owned outcomes.
- The pinned **Wisp Control Center** remains the only user-facing intake and dashboard. The Orchestrator cannot expand repository or external-action authority, weaken quality gates, bypass the task-specific `Ship` requirement, or override any safety boundary.

## Review and validation

The default delivery path is deliberately small: one builder, the repository's mandatory CI checks, and one independent **Wisp Release Auditor** review. The builder provides the exact base, branch, commit SHA, changed-file list, validation, risks, and handoff; CI must pass for that exact remote head; and the read-only Auditor returns `PASS`, `PASS_WITH_NOTES`, or `BLOCK`. A `BLOCK`, failed CI, incomplete diff, or insufficient validation stops the candidate. A code change or reconciliation creates a new candidate and requires fresh CI and Auditor evidence for its new SHA.

Use specialist QA only when the change creates a material specialist risk: security or privacy boundaries, persisted-data migrations, native or external integrations, outbound actions, release/packaging work, or an Auditor/CI finding that cannot be resolved from normal tests. The Orchestrator records the applicable scope and exact SHA before dispatching Simulation QA or Live QA. These specialists remain read-only, use isolated/synthetic state, never send real communications or mutate user data, and return a blocking result on failure or inconclusive evidence.

Route every blocking finding to the Orchestrator for one recorded repair owner before editing. The repair is then independently re-reviewed; builders and repair owners do not approve their own changes.

## Autonomous repository maintainer

- The separate top-level **Wisp Repository Maintainer** may propose claims for unassigned actionable audit findings, Simulation QA failures, Live QA failures, reproducible failed checks, explicit GitHub review feedback, and issues labeled `autofix`. It must wait for the Orchestrator to acknowledge and record it as sole owner before editing; a Control Center snapshot is only a mirror. An item assigned to an active builder is ineligible unless the Orchestrator records an explicit transfer first.
- It works in its own Worktree and may commit, push non-force `codex/maintainer-*` branches, and open or update draft PRs. It never writes directly to `main`, merges, force-pushes, closes issues, deploys, or changes secrets.
- Maintainer work must cite its trigger and remain one bounded change at a time. Every result follows the standard review path and any applicable specialist QA.
- Shipping must re-fetch and match the pull request's remote head SHA to its CI, independent review, and any required specialist-QA evidence immediately before a synchronous expected-head merge. Do not use asynchronous auto-merge for conversation-gated releases.

## Model routing (single policy)

The Control Center classifies every task and explicitly sets its model and reasoning effort. This table is the only routing policy; choose the stronger row when uncertain, and honor an explicit user selection.

| Work | Model | Reasoning |
| --- | --- | --- |
| Ambiguous architecture, cross-system integration, security/privacy, migrations, concurrency, or difficult performance work | `gpt-6-astra` | `high` or `xhigh` (`max` only when exceptional depth is necessary) |
| Complex implementation/debugging, substantial refactoring, production review, or careful research | `gpt-5.6-sol` | `high` (or `xhigh` for unusual risk) |
| Well-scoped feature, fix, tests, or repository documentation | `gpt-5.6-terra` | `medium` (or `high` when edge cases matter) |
| Mechanical formatting, extraction, approved fixture generation, or bounded bulk transformation | `gpt-5.6-luna` | `low` or `medium` |

Use `gpt-5.3-codex-spark` only when the user explicitly prioritizes near-instant iteration, `gpt-5.5` only for requested compatibility testing, and `ultra` only when the user explicitly requests nested parallel work. After dispatch, report the model, reasoning, rationale, base SHA, and task title.
