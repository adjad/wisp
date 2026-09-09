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

## Independent release audit

- Every candidate must be reviewed by the separate top-level **Wisp Release Auditor** task after it is committed and pushed, and before the Control Center labels it ready or ships it.
- Give the auditor the exact base, branch, commit SHA, handoff, and changed-file list. The auditor is read-only and must inspect the complete diff plus relevant surrounding code.
- The auditor reports prioritized `P0`-`P3` findings and a verdict of `PASS`, `PASS_WITH_NOTES`, or `BLOCK`. Any actionable `P0`, `P1`, or `P2`, incomplete diff, or insufficient validation blocks release.
- Record exactly one repair owner per blocking finding. Prefer the active original builder; the Maintainer may claim only an unassigned finding, one whose builder is unavailable or stalled, or one explicitly transferred by the Control Center. Never dispatch the same finding to two writers. The auditor then re-reviews the new commit; repair owners and coordinators do not approve their own fixes.
- Audit approval is commit-specific. Any code change, including conflict resolution or a main-branch reconciliation, invalidates the earlier pass and requires re-audit.

## Live QA gate

- After audit passes, the separate top-level **Wisp Live QA** task builds and exercises the exact candidate commit with isolated Wisp state and synthetic fixtures.
- Live QA is read-only with respect to the repository. It must not send real communications, mutate real user data or system settings, or replace the installed Wisp app without separate deployment approval.
- `LIVE_BLOCK` and `INCONCLUSIVE` block release. Any new code commit invalidates both the audit and Live QA verdicts.

## Autonomous repository maintainer

- The separate top-level **Wisp Repository Maintainer** may autonomously claim and fix unassigned actionable audit findings, unassigned actionable Live QA failures, reproducible failed checks, explicit GitHub review feedback, and issues labeled `autofix`. It must check recorded ownership first; an item assigned to an active builder is ineligible unless the Control Center explicitly transfers it.
- It works in its own Worktree and may commit, push non-force `codex/maintainer-*` branches, and open or update draft PRs. It never writes directly to `main`, merges, force-pushes, closes issues, deploys, or changes secrets.
- Maintainer work must cite its trigger and remain one bounded change at a time. Every result goes through the independent auditor and Live QA before shipping.
- Shipping must re-fetch and match the pull request's remote head SHA to both gate approvals immediately before a synchronous expected-head merge. Do not use asynchronous auto-merge for conversation-gated releases.

## Quality-first model routing

- The Wisp Control Center must classify each requested task before dispatch and explicitly set both the model and reasoning effort on the new task. Prefer quality over token conservation; the user has a generous Pro usage allowance.
- Use `gpt-6-astra` for the hardest end-to-end work: ambiguous architecture, cross-cutting integration, security or privacy boundaries, data migrations, concurrency, difficult performance investigations, and changes spanning multiple systems. Use `high` or `xhigh`; use `max` only when exceptional depth is materially useful.
- Use `gpt-5.6-sol` for complex implementation, difficult debugging, production reviews, substantial refactors, and research that needs careful judgment or polish. Use `high` by default and `xhigh` for unusually difficult or risk-sensitive work.
- Use `gpt-5.6-terra` for everyday, well-scoped engineering such as isolated features, ordinary bug fixes, test additions, documentation grounded in the repository, and straightforward tool use. Use `medium` by default and `high` when edge cases matter.
- Use `gpt-5.6-luna` only for clear, repetitive, mechanical, or high-volume tasks with an objective output, such as formatting, extraction, fixture generation from an approved specification, or simple bulk transformations. Use `low` or `medium`.
- Do not automatically use `gpt-5.3-codex-spark` in this quality-first workflow. Use it only when the user explicitly prioritizes near-instant iteration over depth.
- Do not select previous-generation models such as `gpt-5.5` unless the user explicitly requests compatibility testing.
- Do not select `ultra` automatically because it can create nested subagents and blur the top-level Worktree control model. Use `ultra` only when the user explicitly requests nested parallel agents for a meaningfully decomposable task.
- When classification is uncertain, route upward to the stronger model or reasoning effort. An explicit user model or reasoning choice always overrides this policy.
- After creating a task, report: selected model, reasoning effort, one-sentence rationale, Worktree base, and task title.
