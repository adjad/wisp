# Behavior engine: overnight handoff (2026-10-11)

Branch `claude/gracious-hypatia-4eqgwm`, [PR #5](https://github.com/adjad/Wisp_Hackathon/pull/5).
Base: `main` @ `5b04a7f`.

## What shipped

Wisp now learns routines from what the user does and acts before being asked.

- **Learns** (code, with evidence counts and Wilson-bounded confidence):
  - workout and other habits by weekday and time;
  - who the user talks to and when;
  - which back-to-back meetings get cancelled or moved;
  - what happens to meetings that land on a habit;
  - day shape;
  - what the user asks Wisp, and when.
- **Predicts** through the end of tomorrow:
  - expected activities;
  - cancellation risk per meeting;
  - expected contacts;
  - habit conflicts.
- **Nudges** about 4 per day, respecting quiet hours, deduplication and feedback muting.
  - Reversible actions are done automatically, with Undo. That means Wisp-schedule reminders and "Tentative:" holds; Apple Calendar is never written.
  - Messages and meeting changes are only prefilled in the composer for the user to send.
- **Personal context:** a ≤700-byte profile card goes into prompts. In the Intel remote-only build it is the only personal context sent.
- **Runs anywhere:** `service/behavior/` is pure Python + SQLite. It runs in-process on the Mac by default, or behind `brain_server/` on the GX10 (`WISP_BRAIN_MODE=remote`), falling back to local if that is unreachable.

Docs:
- `docs/BEHAVIOR_ENGINE.md`: architecture, flags, endpoints, limits.
- `docs/BEHAVIOR_DEMO.md`: the 1 PM script, the rehearsal reset, Plan B.
- `docs/gx10/RUNBOOK.md`: afternoon GX10 steps, including gateway supervision.

## Integration base and side fixes

- **Founder branches merged:** both Intel founder branches (`3067cf4`, `66b2674`) are in, because the working Intel build comes from them. They already carry PR #4's head (`codex/workflow20-collectors`).
- **Not merged:** the PageReader override is on `codex/intel-pagereader-compat`.
- **Founder tests:** classified 9 founder/Intel test modules for Simulation QA, and fixed 4 that could not import under pytest.
- **News drafts:** these ended as "delivery outcome uncertain" after `0cf821a`. `finish_workflow` now trusts the executor's verified draft status.

## Checks performed

- Independent read-only reviews of:
  - the engine and GX10 pieces;
  - the Mac and Swift integration.

  Every finding was fixed with a regression test, including runs at Sunday 1 PM PT and late at night.
- **On Linux:**
  - every new test module, run one file at a time with Simulation QA's child environment;
  - the same files with PATH limited to the macOS sandbox's allowed executables;
  - the router, workflow, founder and Intel suites;
  - the full regression gate. Its remaining failures also fail on the Linux baseline (Node and macOS-native helpers).
- **On macOS CI:** see the PR's checks on the head commit. Merged only when both workflows were green.

## Not verified

- The Swift UI (nudge card and actionable notifications) is compiled only by CI. No app launch or click-through was done.
- The osascript seeding and cleanup (`demo/seed_mac_persona.py`, `wisp_demo.py behavior reset`) have not run against real Calendar, Reminders or Notes.
- No GX10 step has been run: gateway supervision, brain server, Tailscale Serve :8443, Ollama phrasing latency and quality.
- Real-data pattern quality is untested. The demo uses the fictional Aria Solano persona, and cancellation history accumulates only from install.

## Known limits for the demo

- At Sunday 1 PM the "talk to Dev now" nudge will not fire. Dev's learned window is weekday afternoons. The chat answers "who do I talk to in the afternoon?" instead.
- The founder build has no assistant scheduler. Auto reminders sit on Wisp's schedule but do not alert; the nudge notification is what the audience sees.
- After launch, nudges wait up to 3 minutes for the first Calendar read. Launch well before showtime, then run `behavior reset --wait`.
- Wording through the GX10 may fall back to template text (6 s timeout). `WISP_BEHAVIOR_PHRASER=0` forces templates.

## Rollback

Behavior learning is off unless `WISP_BEHAVIOR=1` or the founder demo is on. To disable:

- **Mac builds:** unset the flag.
- **Founder build:** set `WISP_BEHAVIOR=0`.
- **Remove state:** `wisp_demo.py behavior reset` then `persona remove --yes`, or delete `behavior.db` and `behavior_runtime.db` under `WISP_HOME`.

GX10 scripts each ship a rollback; see `docs/gx10/RUNBOOK.md`.
