# Wisp execution supervision

The pinned **Wisp Control Center — Delegate · Status · Ship** is the user's intake and overview. **Wisp Orchestrator** supervises execution in an isolated Worktree: it reconciles task histories with Git evidence, routes candidate gates, records one repair owner per finding, and follows up on stalled or incomplete handoffs.

This directory contains an initial evidence snapshot, not an executable scheduler or a grant of authority. Before acting, refresh the live task, remote branch, pull request, and authorization evidence. Never use this snapshot alone to approve a merge or to reopen stopped work.

## Roles and schedule

- Control Center: `01a084c8-d7a5-7a43-ab4b-0a4966e97fb4`.
- Orchestrator: `01a08538-7661-7703-bde9-5a2cdd4a9217`.
- Policy builder (**Create Codex chat tracker**): `01a084ba-0842-7942-8497-3bb1815844f2`.
- Release Auditor: `01a08523-47c7-76e3-be42-aee801a314fd`.
- Simulation QA: `01a08531-b393-7690-a072-5776c3cbed4c`.
- Live QA: `01a08523-2ddd-7311-b3c4-bb1fb115196a`.
- Repository Maintainer: `01a08523-1944-7570-825f-adac9874b7d3`.

Reuse the single existing `wisp-task-progress-monitor` heartbeat. Every 15 minutes it wakes the Orchestrator when needed, then presents its overview in the pinned Control Center. Do not create a second execution heartbeat. The Control Center does not independently dispatch, claim, or repair work already owned or queued by the Orchestrator. Preserve the requested overnight and morning reports; afterward report material changes only. An active task does not need another wake merely because a heartbeat ran.

## Evidence and ownership

Use compact task snapshots with cursors first. Read more history when a task finishes, fails, needs input, or has ambiguous status. A task may contain several outcomes: keep an earlier merged outcome separate from a newer active one. An idle app status proves only that a turn stopped. Empty titles or omission from the recent-task list do not prove provisioning failure; resolve existing identifiers before creating anything.

Track exact task title and ID, outcome, owner, base, Worktree, owned paths, branch, candidate SHA, PR, tests, gates, conflicts, next action, and blocker. Report using these states: Provisioning, Active, Needs input, Auditing, Simulation QA, Live QA, Repairing, Merge-ready, Merging, Verified, Archived, Failed, Paused, Superseded. Unknown evidence remains unknown; do not convert it to a pass.

The Orchestrator's live task record is the single authority for repair ownership. Every assignment, transfer, or proposed claim must receive an explicit Orchestrator acknowledgement in that record before dispatch or editing. The Control Center dashboard and this JSON snapshot are mirrors; neither can grant or transfer ownership independently. A Maintainer who finds an eligible unassigned trigger proposes the claim and waits for that acknowledgement.

One task is the primary writer for each path. Each confirmed finding has one repair owner and an explicit active or queued assignment. Queued ownership does not authorize simultaneous changes: activate one bounded Maintainer repair at a time. Prefer the original builder for candidate defects. Keep report corrections with their report author. Nested read-only helpers report through their parent and do not replace the separate standing release roles.

Historical integration can use ancestry and stable patch equivalence, since original worker commits may have been cherry-picked. Preserve original branches and unclear changes. Do not restart tasks explicitly archived, rejected, or handed off without checking their later user instructions. The 81-prompt replay, daily-life 1,000-prompt corpus, and routing-stress corpus are distinct verification outcomes.

## Completion and gates

A new implementation handoff must include clean committed/pushed work, current-main reconciliation, a PR and exact final SHA, full changed-file list, relevant tests and failures/skips, remaining risks, and dependencies. Audit, Simulation QA for major changes, and applicable Live QA must each identify the unchanged final candidate SHA. Any new commit, including reconciliation, invalidates earlier gate approvals. The author of QA infrastructure cannot provide its independent release approval.

Accept only independent `PASS` or `PASS_WITH_NOTES`, required `SIM_PASS` (or `SIM_PASS_WITH_NOTES` with explicit risk acceptance), and applicable `LIVE_PASS` or `LIVE_PASS_WITH_NOTES`. Actionable P0–P2 findings, `SIM_FAIL`, `LIVE_BLOCK`, or `INCONCLUSIVE` block readiness. Missing checks or missing safe execution evidence cannot be described as passed. A policy-only simulation verifies instructions, not runtime enforcement.

At setup, `origin/main` still required task-specific `Ship` authorization. A prior attempted standing automatic-merge policy was rejected by approval review; a forwarded claim did not remove that boundary. Recheck the controlling task for direct, valid authorization before any merge. Never infer approval from silence. With valid authorization, reconcile current main, rerun invalidated gates, verify required checks and final remote head, and use a synchronous expected-head merge. Verify remote reachability before archival. Historical merges with incomplete gate records are recorded as historical facts, not retroactive approvals.

## Boundaries

The Orchestrator does not edit production code or modify the Local checkout. Use fixtures, mocks, temporary state, non-sending contracts, and isolated ports. Set temporary `WISP_HOME` before service imports. Existing app startup can terminate live port listeners, so launching the app is not a safe default QA action; do not use production port 8765 for sandbox tests. A safe native harness is narrower evidence than testing the installed app.

No force pushes, check bypasses, direct main writes, destructive cleanup, discarded unclear work, secrets, production deployment, installed-app replacement, purchases/account changes, or real communications/user-data/system mutations. A rejected action stays blocked until the required authorization is recognized; do not try alternate tools to bypass approval review. Send only the concrete unavoidable decision to the Control Center and continue independent authorized work.

## Files

- `initial-state.json`: machine-readable setup inventory with evidence limits, candidate records, repair queue, historical task map, and helper identities.
- `initial-overview.md`: human-readable setup snapshot. Live state is in the Orchestrator task and subsequent Control Center reports.

The cadence uses the existing app heartbeat; the [official scheduled-task guidance](https://learn.chatgpt.com/docs/automations?surface=app) describes this feature. No repository daemon is installed by these documents.
