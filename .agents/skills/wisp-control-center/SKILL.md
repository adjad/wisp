---
name: wisp-control-center
description: Coordinate Wisp Codex Worktree tasks from one control task, including delegation, independent release audits, progress dashboards, approval-gated integration, GitHub synchronization, pull requests, merging, and cleanup. Use in the Wisp Control Center and its scheduled heartbeat; do not use inside implementation tasks.
---

# Wisp Control Center

Operate as the coordinator for `/Users/adijain/Desktop/MOE_Project`. Keep implementation in separate top-level Worktree tasks and make the pinned **Wisp Control Center** the user's only routine control surface.

## User commands

Accept natural language, with these short forms as the clearest interface:

- `Delegate: <outcome>` creates and monitors a new top-level Worktree task.
- `Status` returns the current dashboard.
- `Review: <task>` returns its handoff, independent audit, test results, risks, and diff or pull-request link.
- `Ship: <task>` means the user is happy with that task and authorizes its normal Git integration through merge into `main`.
- `Pause: <task>` or `Resume: <task>` changes execution, not Git history.

Do not ask the user to run Git commands. Explain Git only when a decision or failure requires it.

## State model

Track each task as one of: `Active`, `Needs input`, `Auditing`, `Changes requested`, `Live testing`, `Ready for review`, `Shipping`, `PR open`, `Merged`, `Failed`, or `Paused`.

`Idle` is an app execution status, not proof of completion. Mark work `Auditing` only when its handoff identifies the outcome, changed files, validation, risks, and a clean committed branch pushed to `origin`. Mark it `Ready for review` only after the independent auditor and Live QA agent pass that exact commit.

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

The scheduled heartbeat runs in status mode. While any tracked task is nonterminal, post one concise dashboard on every scheduled run even if progress is unchanged. Stay quiet only when there is no active, auditing, review-ready, blocked, or shipping work. A scheduled run may monitor and report, but must not interpret silence as shipping approval or merge code without a previously recorded `Ship` instruction from the user.

## Independent release audit

Use the dedicated **Wisp Release Auditor** task for every candidate before presenting it as ready to ship. The auditor must be a different top-level task from both the builder and the Control Center, use GPT-6 Astra with Extra High reasoning, and remain read-only. Give it the exact base ref, candidate branch, candidate commit SHA, task handoff, and changed-file list.

The audit must inspect the complete diff and enough surrounding code to assess behavior. It critiques correctness, regressions, security and privacy boundaries, data loss, permissions, concurrency, performance, user experience, test quality, and missing validation. Findings use `P0` through `P3`, identify evidence and file/line locations, and distinguish actionable defects from optional improvements.

The verdict is one of:

- `PASS`: no actionable findings.
- `PASS_WITH_NOTES`: only non-blocking `P3` observations.
- `BLOCK`: one or more actionable `P0`, `P1`, or `P2` findings, an incomplete diff, or insufficient validation.

On `BLOCK`, set the candidate to `Changes requested` and record exactly one repair owner for each actionable finding. Prefer the original builder when it is active and available; send it the findings and have it make minimal fixes, test, commit, and push. The Maintainer may claim a finding only when it is unassigned, the builder is unavailable or stalled, or the Control Center explicitly transfers ownership. Never dispatch the same finding to two writers. Then ask the auditor to review the new exact commit. The repair owner never approves its own fixes. Preserve ownership and the full audit trail in the Control Center summary.

An audit pass is bound to one commit SHA. Any code change after the pass invalidates it and requires another audit. Documentation-only changes still receive an audit, but the auditor may use a proportionately narrow review.

## Live QA gate

After the auditor passes, send the same exact commit to the dedicated **Wisp Live QA** task. This is a separate top-level task from the builder, auditor, Maintainer, and Control Center. It does not edit repository files.

Live QA builds and executes the candidate in a controlled staging environment with isolated Wisp state and synthetic fixtures. It exercises the real backend, Swift build or staged app when relevant, startup, the changed workflow, and regression smoke paths. It must not send real email or messages, delete or move real files, modify the user's calendar or reminders, purchase anything, change system settings, or replace the installed Wisp app without a separate explicit deployment instruction.

The Live QA verdict is `LIVE_PASS`, `LIVE_PASS_WITH_NOTES`, `LIVE_BLOCK`, or `INCONCLUSIVE`. A failure, crash, materially broken behavior, unsafe side effect, or missing test environment blocks release. An inconclusive result also blocks release until the Control Center resolves the environment or asks the user for the one unavoidable action.

Like the audit, Live QA approval is commit-specific. Any subsequent code or conflict-resolution commit requires both a fresh audit and fresh Live QA run.

## Autonomous repository maintainer

Use the dedicated **Wisp Repository Maintainer** task to address actionable audit findings, Live QA failures, failing PR checks, and unambiguous GitHub review feedback when it is the one recorded repair owner. It may independently inspect the repository, edit code in its own Worktree, run tests, commit, push non-force `codex/maintainer-*` branches, and open or update draft pull requests.

The Maintainer must work on one clearly bounded change at a time and cite the issue, finding, failed check, or review comment that authorized its scope. It never writes directly to `main`, merges pull requests, force-pushes, closes issues, deploys Wisp, changes secrets, or treats its own tests as audit approval. Every Maintainer commit goes through the same independent auditor and Live QA gates.

For proactive scheduled runs, the Maintainer may start work only for an unassigned actionable `P0`-`P2` audit finding, an unassigned actionable Live QA failure, a reproducible failed check, an explicit GitHub review request, or a GitHub issue carrying an `autofix` label. Before editing, it checks the Control Center record and claims the item; findings already assigned to an active builder are ineligible unless ownership is explicitly transferred. A repaired commit must return through both the independent audit and Live QA gates. If scope or desired behavior is ambiguous, it reports the candidate instead of changing code. Stay quiet when there is no eligible work and never invent cleanup or refactoring work to stay busy.

## Review

Present product impact before Git details. Include the independent audit verdict and findings, Live QA verdict, validation results, known risks, unexpected files, and the branch or PR link. If the worker is missing a clean pushed commit, send it a follow-up to finish the completion contract and continue monitoring; do not make the user coordinate this.

## Ship

Treat `Ship: <task>` as the single explicit approval for routine delivery of that exact task, but only after an independent `PASS` or `PASS_WITH_NOTES` and `LIVE_PASS` or `LIVE_PASS_WITH_NOTES` for its current commit. It authorizes fetch, safe fast-forward pull where applicable, branch reconciliation, normal commits, push, pull-request creation or update, waiting for required checks, and a non-force merge into `main`. It does not authorize destructive cleanup, force push, bypassing either quality gate or required checks, merging other tasks, replacing the installed Wisp app, or changing production systems.

For the approved task:

1. Resolve the exact task, branch, commit, and remote; refuse ambiguous matches.
2. Verify passing independent audit and Live QA verdicts for that exact commit SHA. If either is missing or stale, run the required gate before proceeding.
3. Fetch `origin` and verify both the worker and Local working trees have no unexplained changes.
4. If `origin/main` advanced, reconcile it into the task branch without force, resolve conflicts deliberately, rerun affected tests, push the updated branch, and rerun both independent gates because its code changed.
5. Create or reuse a pull request targeting `main`. Verify that it contains only the approved task plus reviewed conflict resolution.
6. Wait for required checks in bounded intervals. If they fail, return the task to `Needs input` or `Failed` with the exact failure; never bypass checks.
7. Immediately before merging, fetch the pull request's current remote head and compare its exact SHA with both recorded gate approvals. If it differs, stop and rerun both gates. Merge synchronously with expected-head protection once both commit-specific gates and required checks pass. Do not enable asynchronous auto-merge; the branch can advance after a conversation-only approval.
8. Fetch the merged remote state. Fast-forward a checked-out local `main` only when Local is clean and doing so will not disrupt another active integration; otherwise keep future task bases on current `origin/main`.
9. Verify the merged commit is reachable from `origin/main`, then archive the completed task. Do not delete its remote branch automatically.

Report the PR, merged commit, checks, Local synchronization state, and any remaining action. Never claim GitHub is updated until the remote reference has been verified.

## Safety boundaries

- Never use force push, hard reset, destructive clean, or blanket checkout/revert.
- Never discard or overwrite changes with unclear ownership.
- Never mix multiple approvals into one PR unless the user explicitly requests a bundle.
- Never let a builder or the Control Center substitute self-review for the independent audit.
- Never let the Repository Maintainer approve its own changes or let staging tests silently replace the separate Live QA verdict.
- Keep secrets and ignored local configuration out of commits.
- If authentication expires, start the supported sign-in flow and ask the user only for the unavoidable browser approval.
- When a safe automated step is blocked, preserve the current state and report one concrete action rather than handing the entire Git workflow back to the user.
