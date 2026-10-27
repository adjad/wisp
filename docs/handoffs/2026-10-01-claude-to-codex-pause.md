# Wisp 1.2.0: Claude to Codex pause handoff

Date: October 1, 2026 (written at the end of Claude's session)
From: Claude Code (builder and repair owner for the 1.2.0 PRs below)
To: Codex (Hub / Orchestrator / Release Auditor / integration coordinator)

## Why this exists

The user is pausing for the night and asked Codex to take over. Everything below is **pushed, committed and gate-verified at the exact heads listed**. Nothing is half-edited in any worktree. This is a status snapshot, not a live dashboard: recheck remote heads before acting. A code change, repair or reconciliation creates a new candidate and invalidates earlier commit-bound evidence.

Standing rules still apply (AGENTS.md): one writer per file, no force-push, no destructive git, no direct writes to `main`, never auto-merge, no installed-app replacement, no real sends, do not launch the installed or dev Wisp against production listeners, do not read or scrub the user's real `~/.moe` data. Commit messages carry no model credit and no `Co-Authored-By`.

## Open PRs (11 Claude PRs; #139 is Codex's own)

All of these had both required checks (`python-regressions`, `Verified macOS artifact`) passing at the heads shown when this was written, except #141 and #142, which were opened minutes earlier and whose first CI run had not reported. "Local QA" means `scripts/run_simulation_qa.py --expected-sha <head>`: status PASS, 0 failed gates.

| PR | Head | Base | Local full QA at this head | Independent review still owed |
| --- | --- | --- | --- | --- |
| [#129](https://github.com/adjad/wisp/pull/129) routing: shared fast-path repair | `f1c9133` | main | PASS (20170 tests) | Auditor on the repair (new candidate) |
| [#130](https://github.com/adjad/wisp/pull/130) local providers + context qualification repair | `c31361b` | #129 branch | PASS (agent run, 20164) | Auditor (full audit was already resumed on the OLD head) |
| [#131](https://github.com/adjad/wisp/pull/131) everyday prompts (inherits #129 by merge) | `22cd0a9` | #129 branch | PASS (20293) | Auditor on the inherited repair |
| [#132](https://github.com/adjad/wisp/pull/132) approvals, request routing, PortGuard | `e1ea49f` | #129 branch | unchanged since Auditor/QA started | Auditor (pending) |
| [#134](https://github.com/adjad/wisp/pull/134) email digest + review repairs | `073ddd1` | #129 branch | PASS (agent run, 20184) | ordinary review re-pass |
| [#135](https://github.com/adjad/wisp/pull/135) protected-path floor, MCP trust | `001c105` | #129 branch | unchanged | Auditor (pending) |
| [#136](https://github.com/adjad/wisp/pull/136) notch hitbox | `53d9775` | #129 branch | unchanged | ordinary review PASS; needs reconciliation |
| [#137](https://github.com/adjad/wisp/pull/137) M-3 + H-16 backend trust | `a6feca0` | #132 branch | PASS at earlier commit; CI green at this head | Auditor + bounded Simulation QA |
| [#138](https://github.com/adjad/wisp/pull/138) H-1 category-aware grants | `8d49fa5` | #135 branch | PASS | Auditor + bounded Simulation QA |
| [#140](https://github.com/adjad/wisp/pull/140) H-9 manual reminders survive sync | `268c2c1` | main | PASS (20041) | ordinary review |
| [#141](https://github.com/adjad/wisp/pull/141) Reminders stuck on "syncing" | `fe70dae` | main | PASS (20061, run at this exact head) | Auditor (native permission handling) |
| [#142](https://github.com/adjad/wisp/pull/142) mail history from local index + incremental | `29503dd` | main | PASS (20082) | Auditor + a supervised live check |

`main` was `d421519` when this was written. The #129 / #131 / #132 / #134 / #135 / #136 stack branches still sit on the old #129 base; they need reconciling when #129 lands (expect overlapping manifest lines in `scripts/run_simulation_qa.py` and separate regions of `service/main.py` / `service/agent/loop.py`; one writer resolves them).

## What changed in this session (and what is still unproven)

**Routing repair (#129/#131)** - answers the earlier Hub handoff. One shared definition (`service/utterance_shape.py`) decides plain-request vs "a sentence about words" vs a prohibition; `route()`, `compile_task`, `compile_new` and `compile_read` use it. Structured calendar/inbox shortcuts run only when they fully understand the request (exact dates and "today and tomorrow" resolved, reminder exclusion narrows to `calendar_only`, second source / unresolvable date / second request go to the model). Authored prose no longer forces `get_upcoming`. Lesson: the router already has tested negation handling for web lookups and notes checklists; a blanket prohibition gate broke 143 tests, so the route-level gate is limited to mentions and device-action prohibitions. Details: comments on #129 and #131.

**#130** `service/inference/qualify.py` now records only the context window the engine demonstrated (14,000-token engine recorded 13,824, not 16,384). Risk: an engine that reports its token count before truncating would still be over-trusted.

**#134** social sign-in security alerts stay visible; counts are made before per-sender clipping.

**#140 (H-9)** `/assistant/sync/calendar` accepts only Calendar/Reminders; `sync_source("manual", ...)` raises. Verified on current main: H-10 is already fixed; H-11 (title+minute collapse) and M-1 (moved event gets a new id) are STILL OPEN. C-1 and C-2 are already merged.

**#141 Reminders stuck on "syncing"** - live finding (read-only GET of the running backend): the app re-posts Reminders every minute with `authorized:false, syncing:true`, i.e. macOS has never recorded an answer (`notDetermined`); Calendar is fine. The backend now treats a wait that exceeds 90 s as unavailable with an instruction; the app tracks its permission request, activates itself before asking, and retries with throttling. **Not proven:** why macOS has not recorded an answer on this machine (the permission database is not readable from here). If, after this PR, Wisp is absent from System Settings > Privacy & Security > Reminders, the user may need `tccutil reset Reminders com.wisp.assistant` and a relaunch. I did not run it (it changes system privacy settings). The installed Wisp is an ad-hoc-signed 1.1.0, so a re-signed build also resets permissions.

**#142 Mail history** - measured from the app's own call log (22 days): the 2-year history walk took a median 4.8 min (p90 10.5, max 17) every 30 min, a flat ~19 s per 500-message batch (about 14 Apple Events per message), re-reading the same two years each time, with ~30% of walks aborting and restarting. Now: history is read from Mail's on-disk index when it is trusted (no Apple Events); the AppleScript fallback is incremental with a full walk at least every 6 h; Daily Summary independence is enforced by tests (it never read history; tests poison the history cache). **Not verified:** the real Mail index was never timed or read (not readable from the build environment); the speedup is expected, not measured; nothing was exercised against real Mail/AppleScript. Account labels are matched to index accounts by position, so the index path is trusted only when the account counts match and every label resolved; that guard is the part most worth the Auditor's attention. For Gmail the index history comes from All Mail (includes archived/sent), unlike the old INBOX-only walk.

## Update after the handoff was written

`main` moved: #139 and #140 were merged by Codex. #141 reported a conflict; Codex had already merged `main` into the branch (head is now `fe70dae`, a merge commit). Claude resolved the same conflict independently (only `scripts/run_simulation_qa.py`: keep both test registrations), found the resulting tree byte-identical, did not push, dropped its redundant local merge, and re-ran the full gate on `fe70dae`: PASS, 0 failed gates, 20061 tests. #142 is still conflict-free against the new `main`.

## Next steps, in order of value

1. **Release Auditor** verdicts, one per exact head, in the order already queued (#130 -> #132 -> #135 -> #137 -> #138), then #129/#131 (the routing repair), #141 and #142. Triggers are recorded in each PR body.
2. **#142 live check**, supervised, with Full Disk Access: confirm the index path labels accounts correctly and measure the real time. This is the single biggest unverified claim.
3. **Merge order** once reviews pass: #129 first (the stack's foundation), then retarget and reconcile #130-#136; #137 after #132; #138 after #135; #140, #141, #142 are independent (based on main) and only share `scripts/run_simulation_qa.py` manifest lines.
4. **Audit findings (docs/WISP_BUG_AUDIT_2026-09-09.md).** Handled in the PRs above: H-1, H-2, H-3, H-4, H-5, H-6, H-9, H-16, M-3, M-8. Verified already merged on main: C-1, C-2, H-10. Verified STILL OPEN, not started: H-11 and M-1 (store identity; change identity semantics, so serialize through one `store.py`/`main.py` writer). **Not re-verified by Claude** (may already be fixed by Codex work on main): H-7, H-8, H-12 to H-15, H-17 to H-19, M-2, M-4 to M-7, M-9, M-10, TG-1 to TG-3. Also open prompt leftovers: delivery grammar gaps, stock daily change, model-swap latency, no repeat guard for identical failing tool calls.
5. **Security housekeeping the user must decide:** an old pasted OpenRouter key exists in `~/.moe` history (`sessions.db`, `audit.jsonl`); it should be rotated, and a scrub needs the user's explicit go-ahead. I did not touch it.

## Ownership and environment notes

- CI monitors (auto-fix) were turned on for the 1.2.0 PRs from Claude's session; those wake Claude's session, not Codex. Overnight failures will not be repaired until a session is open again. Codex should treat CI state as its own to watch; it must not duplicate a repair that Claude owns without a recorded transfer.
- Claude worktrees (all clean, nothing uncommitted): `/Users/adijain/Desktop/wisp-1.2.0*` (one per branch `claude/v1.2.0-*`). The two stashes in the Local checkout are not Claude's.
- Do not run `codex review`/`exec` against these PRs without the user's OK (it sends code to OpenAI).
- Test live only through the isolated backend (port 8775 + sandbox 8766 with a throwaway `WISP_HOME`); never replay prompts against 8765 and never launch the built app. macOS has no `timeout`; bare `pytest tests/` runs zero tests, use the venv python with explicit files.
- Privacy note for the transcript: while diagnosing I once printed the `/assistant/status` calendar diagnostics, which included event titles from the user's calendar. Nothing was written to a file or sent anywhere. The history/cache work read only timestamps, counts and tool names.
- A shell slip during this session briefly staged a revert of Claude's own #141 commit; it was restored from HEAD, verified clean, and nothing was lost. No other task's changes were touched.

## Requested reply

Confirm receipt; say which review (Auditor) starts first; say who takes the #142 live check; record the transfer of CI watching for #129-#142 so Claude does not double-repair when its session resumes.
