# Handoff: Auto daily summary, ownership released to Codex

Date: 2026-10-03. From: Claude (primary writer). To: Codex.

## Ownership

Claude releases write ownership of every path below. Claude will not edit them
again unless the Orchestrator records a transfer back. This handoff is the only
record Claude can make: Claude Code cannot reach the Orchestrator's coordination
record, so the Control Center must relay it for an acknowledgement.

Not acknowledged here: the "native repair handoff". No such document was found in
`docs/handoffs/`, the repo, or the session, so nothing was read or accepted.

## Update: OverlayView.swift reclaimed for one fix

The user reported the Daily Summary pill rendering as a large circle in the
installed build. Cause: the divider between the two halves was a bare
`Rectangle()`, which fills all available height. No Codex acknowledgement or
edit existed on this path or branch, so Claude reclaimed `OverlayView.swift`
alone, fixed it (fixed-height divider plus `fixedSize(vertical: true)` on the
pill), and releases it again with this commit. All other paths stay released.

## Outcome delivered

The AM/PM toggle is replaced by an Auto switch on the Daily Summary pill, on by
default. Wisp generates a summary at 8 AM and 8 PM when Auto is on.

- Branch `claude/auto-daily-summary`, commit `f0e5e64`, based on `origin/main` at `73b0a83`.
- Local worktree: `.claude/worktrees/auto-daily-summary`.
- Not pushed. No pull request.
- Installed: Wisp 1.1.5 build 1021 (`f0e5e64`, clean clone, `dirty: false`), live on this Mac.

## Released paths

- `app/Sources/WispApp/OverlayView.swift` (split pill, Auto switch, tooltip)
- `app/Sources/WispApp/OverlayModel.swift` (`summaryAuto`, `summaryNext`, `toggleSummaryAuto`; `dailySummaryRunning` is now `@Published private(set)`)
- `app/Sources/WispApp/WispClient.swift` (`SummarySchedule`, `summarySchedule()`, `setSummaryAuto(_:)`)
- `service/assistant/scheduler.py` (`BRIEF_SLOTS`, `next_brief_at`, per-slot gating)
- `service/assistant/brief.py` (`brief_slot`, `brief_already_published`, slot-keyed dedupe)
- `service/assistant/store.py` (per-slot completion row on `brief` receipts)
- `service/config/__init__.py` (`get/set_daily_summary_auto`; `daily_summary_hour` removed)
- `service/main.py` (`/assistant/summary_schedule` now `{enabled, next}`)
- `tests/test_daily_summary_delivery.py`

## Checks run

- `test_daily_summary_delivery.py`, `test_assistant_delivery.py`, `test_brief_fallback.py`, `test_source_sync_contract.py`: 2,306 passed on the rebased commit.
- Before the rebase: `scripts/test_replay_failure_fixes.py`, 138/138 modules passed.
- `scripts/wisp-build all` on a clean clone of `f0e5e64`: verified candidate, signature verified after launch.
- Live backend: `GET /assistant/summary_schedule` returned `{"enabled": true, "next": "2026-10-03T08:00:00"}`.
- No failures. The 138-module gate was not rerun on the rebased commit.

## Incomplete work and known risks

- The new header button was not looked at in the running app. Layout is unconfirmed.
- No CI, Auditor or specialist-QA evidence exists for `f0e5e64`. The change touches scheduling and persisted completion rows, so the Orchestrator should decide whether the Auditor trigger applies.
- Compatibility: the old `period` field on `/assistant/summary_schedule` is rejected with 400. Only the packaged app calls it.
- A pre-slot `daily_brief` receipt for today still covers the 8 AM slot only. An upgrade after a pre-slot 8 PM brief could repeat it once.
- The installed bundle is ad-hoc re-signed per build. macOS permission grants may need re-granting.
- The commit author is the machine default (`adijain@MacBook-Pro.local`), because git has no email configured here.

## Integration dependencies

- Conflicts: any open work touching the paths above, especially `OverlayView.swift` and `scheduler.py`, which `origin/main` changed recently.
- The branch must be pushed before Codex can see it from `origin`.
- The Local checkout's `main` is stale and the user's `AGENTS.md` and `.claude/settings.local.json` edits are uncommitted there. Do not stash or discard them.
