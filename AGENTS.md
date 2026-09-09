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

- In the Wisp Control Center, treat requests to "assign", "delegate", "start", or "create" a new task as requests for a separate user-owned Codex task.
- Create each implementation task as a new top-level Worktree task in the MOE_Project project unless the user explicitly requests Local or the work is integration-only.
- Do not perform the delegated implementation inside the Control Center and do not substitute a nested subagent for a top-level task.
- Use nested subagents only when the user explicitly asks for subagents or when they are bounded, read-heavy helpers inside an already-created implementation task.
- After dispatch, report the new task title and keep its progress in the Control Center's status summary.
