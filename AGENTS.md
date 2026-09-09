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
