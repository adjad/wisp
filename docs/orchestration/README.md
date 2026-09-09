# Wisp execution supervision

The pinned **Wisp Control Center — Delegate · Status · Ship** is the user-facing intake and dashboard. **Wisp Orchestrator** is the live ownership record: it assigns one owner, tracks the exact candidate SHA, and records repairs. Repository documents are durable policy, not a scheduler or a source of live task state.

## Operating path

1. The Control Center checks for duplicate work, fetches `origin/main`, records the base SHA, and gets the Orchestrator's acknowledgement before it dispatches one isolated Worktree builder.
2. The builder owns its declared paths, validates the change, pushes a non-force branch, and supplies a concise handoff with its final SHA.
3. Mandatory CI runs for that exact remote SHA. One independent, read-only **Wisp Release Auditor** then returns `PASS`, `PASS_WITH_NOTES`, or `BLOCK` for the complete diff.
4. A failed check or `BLOCK` returns to the Orchestrator for exactly one repair owner. A repaired or reconciled commit is a new candidate and repeats CI and independent review.
5. `Ship: <task>` remains the only routine merge authorization. Immediately before a synchronous merge, re-fetch and match the pull request head to its CI, review, and any required specialist-QA evidence. Never infer shipping approval from silence.

## Specialist QA

Simulation QA or Live QA is added only for security/privacy boundaries, data migrations, native or external integrations, outbound actions, release/packaging work, or a risk identified by CI or the Auditor. The Orchestrator records the trigger, scope, and candidate SHA before dispatch. Specialists are read-only, use isolated or synthetic state, and cannot send real communications, mutate user data, replace the installed app, deploy, force-push, or merge.

## Evidence and boundaries

Record the task title and ID, owner, base SHA, Worktree, owned paths, branch, candidate SHA, PR, tests, CI, independent-review verdict, any specialist QA, risk, and next action. App task status alone is not evidence of completion. Preserve unclear work, keep one primary writer per path, and never use force pushes, bypass checks, write directly to `main`, or perform destructive cleanup.

The former `initial-state.json` and `initial-overview.md` were one-time setup snapshots. They had no runtime consumers and their only references were within this directory, so they were retired rather than presented as current evidence. Read the live Control Center and Orchestrator task records for current state.
