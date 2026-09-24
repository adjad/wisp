# Living Today plan — implementation handoff

## Scope and ownership

- Builder: Describe Wisp to investors (`01a0d095-64bd-7cb0-b6cd-d5d6550c047b`).
- Worktree: `/Users/adijain/.codex/worktrees/wisp-living-today/MOE_Project`.
- Branch: `codex/living-today-plan`.
- Base: `80fdc07e0fb2d0e59358f0fcb44e1c7b844b88ba`.
- Orchestrator acknowledged the original Today paths and this builder as sole repair owner for in-scope review findings. Local checkout was not edited.

## Delivered

The overlay's **Today** button opens a native planning window. A user can choose a date, create study/project/general tasks, estimate duration, set priority and deadline, edit tasks, mark them done, remove them, and pin a specific time. Working hours and a running-late floor are persisted per day and timezone. Task edits use optimistic revisions; conflicting writes return HTTP 409.

The deterministic planner uses actual Calendar intervals, including overnight and recurring occurrences, without display dedupe hiding separate meetings. It preserves pinned blocks and reports conflicts, past pins, and deadline violations. It fills uninterrupted gaps with flexible tasks and explains every unscheduled task. Reminders are deadlines; mirrored Wisp/Reminders rows are collapsed only for display. Manual fixed events without a known duration stop confident automatic placement.

Calendar/Reminders freshness is visible. Calendar must have synced within three minutes and confirm coverage of the entire selected local day. Missing durations on the selected day or incomplete calendar coverage withhold flexible placements. Older events with missing end times leave suggested slots explicitly provisional without treating those events as occupying every future day. All-day events are treated conservatively: their presence withholds automatic placement, while the task editor lets the user explicitly pin time. Stale reminders mark the plan provisional but do not imply calendar occupancy.

No model or cloud service is called. This feature does not write native Calendar/Reminders records or send communications. All task and planning state stays in the existing local AssistantStore database.

## Storage and compatibility

- `today_tasks`: durable task JSON, day index, revision, update time.
- `today_preferences`: day/timezone working bounds, running-late floor, revision.
- `calendar_event_ends`: exclusive end timestamps keyed by commitment ID.
- `today_source_sync`: freshness and coverage receipts committed atomically with source rows; older native snapshots are rejected. Only planning metadata is persisted, not diagnostic title lists.
- Existing `commitments` schema and recovery format are unchanged. Legacy rows without duration are presented conservatively until the updated native app syncs.
- Sync updates commitments and durations in a single transaction, including rollback for invalid ends. Source replacement does not delete Wisp tasks. Native creation receipts also record known duration.
- Snapshot reads use a SQLite transaction. Returned `revision` covers planning preferences; tasks have their own revisions. Calendar remains read-only live source data. An itinerary is recomputed from these inputs and the current clock, not stored as a promise that future availability cannot change.

## Files

- `service/assistant/today.py`: validation, timezone boundaries, source health, deterministic planner.
- `service/assistant/today_api.py`: local Today GET, task create/edit, replan endpoints.
- `service/assistant/store.py`: additive auxiliary tables, transactions, snapshots, task/preference revisions, duration ingestion.
- `service/main.py`: Today router registration and validation/ingestion of event end timestamps.
- `app/Sources/WispApp/CalendarReader.swift`: end timestamps and calendar coverage bounds.
- `app/Sources/WispApp/TodayView.swift`: native window, models, task editor, itinerary, freshness and conflict UI.
- `app/Sources/WispApp/OverlayView.swift`: Today entry button only.
- `tests/test_today_plan.py`: synthetic planner, persistence, migration compatibility, API and ingestion contracts.
- `tests/TodayPlanChecks.swift`, `scripts/test_today_contract.sh`: actual Swift model/view compilation and injected-transport contracts.
- `scripts/run_simulation_qa.py`: one-line registration of the new Today test only, under the Orchestrator’s explicit ownership transfer from Simulation QA.
- This handoff.

## Validation and integration

All Python runs use a temporary `WISP_HOME` and `PYTHONDONTWRITEBYTECODE=1`. Swift contracts inject an inert HTTP transport and never launch the app or use native data. The full app is compiled to `/tmp/wisp-today-swift-build`; it is not installed or launched.

Commands:

```sh
WISP_HOME=$(mktemp -d /tmp/wisp-today-checks.XXXXXX) PYTHONDONTWRITEBYTECODE=1 \
  /Users/adijain/Desktop/MOE_Project/.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_today_plan.py tests/test_assistant_delivery.py \
  tests/test_assistant_migrations.py tests/test_assistant_recovery.py
bash scripts/test_today_contract.sh
swift build --package-path app --scratch-path /tmp/wisp-today-swift-build
WISP_HOME=$(mktemp -d /tmp/wisp-today-gate.XXXXXX) PYTHONDONTWRITEBYTECODE=1 \
  /Users/adijain/Desktop/MOE_Project/.venv/bin/python scripts/test_replay_failure_fixes.py
git diff --check
```

The first full gate passed 117/118 modules; the sole failure was the Simulation QA manifest rejecting the newly added Today test as unclassified. This is a real integration dependency, not an exempted baseline failure. The exact final commit, subsequent gate results, remote CI, and independent review status are reported to Hub after commit. A first sandboxed SwiftPM build failed because its nested manifest sandbox could not start; the approved unrestricted compile passed. Existing Swift deprecation warnings and a Starlette/httpx deprecation warning remain. Strict release-toolchain preflight does not pass on this host: the repository pins Xcode 16.4 / Swift 6.1.2; installed Xcode is 26.3 / Swift 6.2.4. No toolchain pin was changed. A normal full-app compile is not a release-package qualification.

## Limits and remaining program

This is the first living Today slice, not the full feature program. It does not yet extract new tasks from messages/course systems, carry incomplete tasks across dates automatically, track an active focus session, or generate morning/evening briefs. Flexible slots are recalculated as time advances; pinning retains a slot. Apple source access still depends on the existing app permission and sync flow. Date/time input uses explicit IANA zones and rejects ambiguous/nonexistent working-hour boundaries rather than guessing through DST transitions.

The broader source streaming/memory, proactive brief, study, news/markets/campus, people/opportunities, automation/scripting, voice/artifact, connector, background/remote, transaction, and beta-distribution milestones remain separate work. No external account integrations, real messages, stock trades, purchases, deployments, or installed-app replacement were performed.

Integration should check overlap in `service/main.py`, `store.py`, and the small overlay entry change against concurrent work. Native integration and additive persisted state require the repository's independent release review and applicable isolated specialist QA before delivery is declared complete.
