---
name: wisp-control-center
description: Coordinate Wisp Codex Worktree tasks from one control task, including delegation, progress dashboards, approval-gated integration, GitHub synchronization, pull requests, merging, and cleanup. Use in the Wisp Control Center and its scheduled heartbeat; do not use inside implementation tasks.
---

# Wisp Control Center

Operate as the coordinator for `/Users/adijain/Desktop/MOE_Project`. Keep implementation in separate top-level Worktree tasks and make the pinned **Wisp Control Center** the user's only routine control surface.

## User commands

Accept natural language, with these short forms as the clearest interface:

- `Delegate: <outcome>` creates and monitors a new top-level Worktree task.
- `Status` returns the current dashboard.
- `Review: <task>` returns its handoff, test results, risks, and diff or pull-request link.
- `Ship: <task>` means the user is happy with that task and authorizes its normal Git integration through merge into `main`.
- `Pause: <task>` or `Resume: <task>` changes execution, not Git history.

Do not ask the user to run Git commands. Explain Git only when a decision or failure requires it.

## State model

Track each task as one of: `Active`, `Needs input`, `Ready for review`, `Shipping`, `PR open`, `Merged`, `Failed`, or `Paused`.

`Idle` is an app execution status, not proof of completion. Mark work `Ready for review` only when its handoff identifies the outcome, changed files, validation, risks, and a clean committed branch pushed to `origin`.

Render a compact dashboard grouped by state. For every nonterminal task show its exact title, one-line progress, and one next action. Put tasks requiring the user first.

## Delegate

Before dispatch, inspect current MOE_Project tasks to avoid duplicates, fetch `origin`, resolve the latest clean base (normally `origin/main`), and record its commit SHA. Do not mutate or discard an unclear Local working tree.

Create a separate user-owned Codex task in an isolated Worktree. Choose its model and reasoning using the repository's quality-first routing policy. Include this completion contract in the worker prompt:

- own one explicit outcome and named paths where practical;
- preserve unrelated work;
- fetch and reconcile the latest `origin/main` before final validation;
- run relevant tests;
- create a `codex/<short-slug>` branch if necessary;
- commit the complete result and push the branch to `origin` without force;
- finish with the standard handoff and remain unarchived for review.

Report the task title, model, reasoning, rationale, exact base SHA, and link to the created task.

## Monitor

Use compact task snapshots first; inspect full history only when a task completes, needs input, fails, or has ambiguous status. Never treat lack of recent commentary as completion.

The scheduled heartbeat runs in status mode. While any tracked task is nonterminal, post one concise dashboard on every scheduled run even if progress is unchanged. Stay quiet only when there is no active, review-ready, blocked, or shipping work. A scheduled run may monitor and report, but must not interpret silence as shipping approval or merge code without a previously recorded `Ship` instruction from the user.

## Review

Present product impact before Git details. Include validation results, known risks, unexpected files, and the branch or PR link. If the worker is missing a clean pushed commit, send it a follow-up to finish the completion contract and continue monitoring; do not make the user coordinate this.

## Ship

Treat `Ship: <task>` as the single explicit approval for routine delivery of that exact task. It authorizes fetch, safe fast-forward pull where applicable, branch reconciliation, normal commits, push, pull-request creation or update, waiting for required checks, and a non-force merge into `main`. It does not authorize destructive cleanup, force push, bypassing required checks, merging other tasks, or changing production systems.

For the approved task:

1. Resolve the exact task, branch, commit, and remote; refuse ambiguous matches.
2. Fetch `origin` and verify both the worker and Local working trees have no unexplained changes.
3. If `origin/main` advanced, reconcile it into the task branch without force, resolve conflicts deliberately, rerun affected tests, and push the updated branch.
4. Create or reuse a pull request targeting `main`. Verify that it contains only the approved task plus required conflict resolution.
5. Wait for required checks in bounded intervals. If they fail, return the task to `Needs input` or `Failed` with the exact failure; never bypass checks.
6. Merge normally once checks pass. If asynchronous auto-merge is available, enable it only because the recorded `Ship` instruction already authorized this task.
7. Fetch the merged remote state. Fast-forward a checked-out local `main` only when Local is clean and doing so will not disrupt another active integration; otherwise keep future task bases on current `origin/main`.
8. Verify the merged commit is reachable from `origin/main`, then archive the completed task. Do not delete its remote branch automatically.

Report the PR, merged commit, checks, Local synchronization state, and any remaining action. Never claim GitHub is updated until the remote reference has been verified.

## Safety boundaries

- Never use force push, hard reset, destructive clean, or blanket checkout/revert.
- Never discard or overwrite changes with unclear ownership.
- Never mix multiple approvals into one PR unless the user explicitly requests a bundle.
- Keep secrets and ignored local configuration out of commits.
- If authentication expires, start the supported sign-in flow and ask the user only for the unavoidable browser approval.
- When a safe automated step is blocked, preserve the current state and report one concrete action rather than handing the entire Git workflow back to the user.
