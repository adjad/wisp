# Wisp 1.4.0 — changelog and status (as of 2026-10-16)

## The short version

**1.4.0 is one pull request: [#238](https://github.com/adjad/MOE_Project/pull/238)** (branch `codex/wisp-1-4-0`, draft, 79 commits, 116 files). It already contains six smaller PRs that were built first and stacked up. It is **not merged and not released**. It is waiting on CI, one independent review, and a real-app acceptance run.

| Bucket | What it means |
| --- | --- |
| ✅ Completed | Built, tested, audited. Nothing left to do except ship inside #238 |
| 🔄 In progress | Someone or something is still running |
| 🚨 Needs you | A decision or action only you can make |

---

## 🚨 Needs your attention

1. **Decide when #238 ships.** It is a draft beta. Two required checks (Preview build, Simulation QA) are still running on the latest head. After they pass, the PR needs your go-ahead to mark ready and merge. Nothing has been installed or released.
2. **Real-app acceptance has not happened.** The PR says the final app still needs: artifact verification, an end-to-end "Mail → model → Today (and withdrawal)" run, native consent verification, and a clean private cleanup. This requires running the built app. The safe-testing rule says not to launch it against port 8765, so this needs your OK on how to run it.
3. **Mellum memory refusals.** The selected default model (Mellum2.1-12B-A2.5B) worked on the aggressive memory profile only. Two earlier runs on the balanced profile were refused for memory. The PR does not claim balanced defaults work. Decide whether to ship that as a known limit or hold 1.4.0 for it.
4. **Mellum is not a proven default.** The model is provisioned from a local folder, with no public download. Separately, the R1 fine-tune scored 85% against a 92% bar, because it calls tools it was not offered and follows instructions found inside files. Round 2 of the dataset is being built now. Decide if 1.4.0 ships on Mellum R1 or stays on the current roster.
5. **Clean up the stack.** #231, #233, #235, #236, #237, #230 and #225 are all already inside #238. Merging #238 makes them redundant. Close them afterward. #233 and #235 show merge conflicts, which won't matter if you close them.
6. **Two PRs are outside 1.4.0 and need a call:**
   - [#239](https://github.com/adjad/MOE_Project/pull/239), router fixes, is ready with CI green. It lifts needed-tool coverage across about 20 prompt families. Do you want it in 1.4.0 or 1.4.1?
   - [#199](https://github.com/adjad/MOE_Project/pull/199), the Ling V3 dataset, is a draft untouched since 10-13. It may be superseded by Mellum. Close it or keep it?
7. **Changelog gaps.** The draft changelog on the branch does not mention the default-model change, the memory-refusal error, or the consent fix. A fuller version is below. Version and changelog edits are yours to approve.
8. **Your local `main` is behind origin and has uncommitted edits.** It has uncommitted edits in the scheduler, store, sync status, email tools and more, plus untracked demo and docs files. Review those before integrating, so nothing gets overwritten.

---

## 🔄 In progress

| Item | State |
| --- | --- |
| #238 CI | `python-regressions` ✅. `Preview build` and `Simulation QA` are running. Full build, signed release and publish are skipped on a draft, as expected |
| #238 independent review | Bounded source/helper review is **pending** on the final head |
| #238 acceptance run | Not started (see Needs you #2) |
| Mellum R2 dataset | A session is running ("Build Mellum R2 dataset from R1 gate failures") |
| Router fixes #239 | A session is still running on it. It is ready for review |

---

## ✅ Completed (inside #238)

All of these are done, tested and audited, with their commits contained in the 1.4.0 branch.

| Piece | Original PR | Evidence |
| --- | --- | --- |
| Weekly history: calendar separated from completed work, 7-day comparison, resumable history import | #231 | 284 local gates passed, 4 CI checks passed, audit PASS_WITH_NOTES, simulation SIM_PASS_WITH_NOTES |
| Availability patterns and "possible time away" | #233 | 445 behavior cases passed, 12 new controls fail before the fix and pass after |
| Bounded cross-source history memory (Mail, Messages, Notes, Calendar) | #235 | 2,132 synthetic cases passed |
| Behavior and day-planning beta | #237 | Regression reproduces the original bug and covers failure and recovery |
| Concurrency-safe private SQLite store | #236 | 282 local gates passed, 4 CI checks passed, audit PASS_WITH_NOTES, simulation SIM_PASS_WITH_NOTES (125 cases) |
| Bundled-engine first-run setup | #230 | Fourteen files, with a repaired regression fixture |
| Raw-JSON and empty-reply fix for "what should I do tomorrow" | #225 | CI green |
| Consent queue and settings-state repair | in #238 | 149 focused tests passed, same-head regression CI green |

Already shipped before this (not part of 1.4.0): 1.3.3 on 10-14, plus Mellum fine-tune plan and dataset (#229, #234), Daily attention and self-email (#232), toolbench (#228), terse schemas (#227) and mac-controls (#226) on main.

---

## 📝 1.4.0 changelog (proposed)

Items marked ➕ are missing from the draft on the branch.

### Added

- **Weekly behavior reports.** They separate scheduled commitments from dated task completions and compare weeks only where source coverage supports it. Calendar entries never establish attendance or completed work.
- **Device-use patterns.** Opt-in app and idle observations support recurring patterns. The day planner can show tentative busy windows, "possible time away" and source-backed preparation suggestions. It preserves calendar commitments, pins and your edits. Website visits give context, not time on a site. Focus/DND and activity Wisp never collected stay unknown.
- **History seeding.** Calendar and completed-reminder history scans, plus explicitly started, bounded per-account Gmail scans, can seed evidence. Progress shows fetched, usable and analyzed records, and partial coverage stays visible. Gmail scans need a fresh Start after restart, and nothing trains the model or creates permanent mailbox memory.
- **Separate controls** for source evidence, semantic interpretation and remote analysis. Suggestions need current source and permission evidence. Disable, revoke, expiry, changed content and Forget withhold dependent suggestions. Inferences cannot change real events or send communications.
- ➕ **Undo and Forget** use durable ownership receipts. They keep work you edited or completed, and show incomplete recovery.

### Changed

- **Bundled-engine setup** supports public model download, progress, cancel and retry without exposing the engine's private key. Readiness needs a live model list, and the setup test accepts only a complete visible reply.
- ➕ **Default text-role model** is now `Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged-oQ4e` on fresh installs. Existing overrides and embedding settings are kept. It is provisioned from a local folder.
- ➕ **Memory refusals** while loading a model now return a clear app error (HTTP 503) before chat streaming or setup certification. An optional warmup refusal no longer invalidates a successful model.
- ➕ **Learning consent** shows only after the backend confirms it. A failed or unclear confirmation shows as unknown. Settings changes are queued, and stale replies are ignored. Turning learning off never needs a successful status read first.

### Fixed

- **Task-evidence replies** recover from malformed structured output instead of showing raw data.
- **Availability windows** require shared supporting dates and covered observations. Computer inactivity alone can no longer become a confident busy label.
- ➕ **Concurrent store startup.** Two parts of Wisp opening the private database at once no longer fail on a half-created file.

- ➕ **Tool routing.** Fixes missing or forced tools for about 20 prompt families (weather, stock, reply-to-landlord, weekday reminders, calendar offsets, notes). Merged to main as #239 (`0ddbe873`) and reaches 1.4.0 when #238 is reconciled with main. Its sessions are still running on follow-ups.

### Known limits (state in release notes)

- History scans do 8 native queries per run, with no per-minute cap.
- Live Calendar, native transport cost and real-model behavior were not qualified.
- WAL mode can change four header bytes in an empty database before an unsupported schema is refused (existing behavior).

---

## Gate checklist for #238

| Gate | Status |
| --- | --- |
| Mechanical regression CI | ✅ |
| Preview build and Simulation QA CI | 🔄 running |
| Independent review on final head | 🔄 pending |
| Real-app acceptance (Mail → Today) | ⬜ not started |
| Version and changelog edit approved | ⬜ needs you |
| Performance gate for the release | ❓ not shown in the PR, so check before release |
