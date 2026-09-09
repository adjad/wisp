# Wisp Simulation QA

Simulation QA is an independent, non-mutating release gate for the exact final
commit of every major Wisp candidate. It complements live UI QA and release
audit; it does not replace either one.

## Safety envelope

Allowed: temporary `WISP_HOME`/`WISPAIR_HOME` directories, repository fixtures,
mocked tool bodies, in-memory sandbox worlds, temporary SQLite databases, and
compile-only or fixture-backed Swift contracts.

Never run as Simulation QA: `scripts/test_all_tools.py`, live prompt replay,
`scripts/test_mail_reply_live.sh --live-prepare`, real-app seed/clear scripts,
cache install/restore scripts, the production backend, local-model evaluations,
or any command that sends, drafts, deploys, installs the app, or mutates Mail,
Messages, Reminders, Calendar, Notes, contacts, or user data.

## Candidate protocol

1. Fetch the candidate ref and record its full remote head SHA. Check out that
   exact commit in the dedicated Simulation QA Worktree. The checkout must be
   clean.
2. Record the base SHA and inspect `git diff --stat <base>..<candidate>` plus
   `git diff --name-only <base>..<candidate>`. Map the changed paths to the risk
   profiles below.
3. Run every applicable targeted profile. Then run the combined `full` profile.
   The runner executes each Python file in a fresh process and fresh state
   directory to prevent process-global fixture leakage.
4. Inspect failures, assign one repair owner per finding, and send reproducible
   commands plus evidence to that owner. A reviewer never patches the candidate.
5. After a revised commit is pushed, repeat from step 1 with the new SHA. Before
   issuing a verdict, confirm the remote ref still resolves to the tested SHA.

Example, from a clean candidate checkout:

```bash
python scripts/run_simulation_qa.py \
  --expected-sha <40-character-candidate-sha> \
  --base-sha <40-character-base-sha> \
  --profile outbound --profile sources \
  --report /tmp/wisp-simqa-targeted.json

python scripts/run_simulation_qa.py \
  --expected-sha <40-character-candidate-sha> \
  --base-sha <40-character-base-sha> \
  --profile full \
  --report /tmp/wisp-simqa-full.json
```

Prefer the single combined run. If a constrained reviewer sandbox blocks the
macOS AppleScript compilation service, run `full --skip-native` in the sandbox
and repeat with `--only-native` under the narrow permission needed for local
script compilation. Retain both reports; neither half alone is a full verdict.

The invoking interpreter must provide Wisp's runtime dependencies, `pytest`,
and `pytest-asyncio`. Worktrees do not need their own `.venv`; the runner uses
the interpreter that launches it.

The runner's only safety mode is `offline` (the default). `--report` is
required and is the caller-owned destination for machine-readable suite
outcomes, captured stdout/stderr, timings, changed paths, SHA stability, and
the explicit live/mutating exclusion list. It exits nonzero for a failed gate,
an unsafe/dirty candidate checkout, or a SHA mismatch.

## Reusable simulation matrix

| Risk surface | Required profile | Representative scenarios |
|---|---|---|
| Conversation/task continuity, workflows | `conversation`, `outbound` | multi-turn slot filling; unrelated conversation; clarification interruption; restart continuity; task/workflow binding |
| Memory retrieval and review | `conversation` | capture/restart/recall; bounded relevance; review/edit/reject; forget and source deletion; stale/concurrent claims; malformed extraction; rollback/retry dedupe |
| Source readers and synchronization | `sources`, `reliability` | restored vs current-launch state; completed-empty vs unavailable; pending permission; timeout/terminal failure; malformed wire rows; partial accounts; local-cache warning |
| Email reply and sending contracts | `outbound`, `sources`, native gates | exact source identity; reply-all; malformed metadata; changed selection; approval binding; cancellation; receipt verification; pure Mail script compilation, never a live draft/send |
| Messages/reminders/calendar | `outbound`, `reliability` | contact/channel ambiguity; scheduled vs immediate send; reminder CRUD; mirrored dedupe; recurring occurrence selection; no real native mutation |
| Approval envelopes and safety | `safety`, `outbound` | preview/payload identity; view-only/full-access policy; always-confirm effects; denial/timeout; failed source withholding; no false success claims |
| Cancellation, retry, duplicates | `safety`, `reliability`, `outbound` | late approval; cancellation before/after dispatch; retry nudge; stale completion; duplicate plan/action suppression; reminder and scheduled-send dedupe |
| Backend failure, readiness, latency | `reliability`, `sources` | connection/load errors; lazy readiness; one-wait-per-request; stalled/dropped sandbox result; handler failure; unavailable source; no false-empty response |
| Router or tool-selection changes | `routing`, plus affected domain | compound ordering; forced steps; channel ambiguity; direct dispatch; historic adversarial regressions; tool availability and scoping |
| Broad/cross-cutting changes | all applicable targeted profiles, then `full` | every deterministic Python test in process isolation, Air simulation, and non-sending native contracts |

The `full` profile runs an explicit reviewed allowlist of all current
`tests/test_*.py` files plus `air/tests/test_air.py`. It fails closed when a
test is added, removed, or renamed until the manifest is reviewed; a newly
added live test can therefore never enter the offline gate by filename alone.
Native gates cover the pure Mail reply contract, fixture-only Mail SQLite
reader, and source-sync label contract.

`offline` describes the reviewed suite selection and state isolation, not an
operating-system security boundary. When a CI or build runner needs defense in
depth against accidental network, subprocess, oMLX, Codex, or unrelated user
data access, it should wrap this command in its kernel sandbox and audit guards.
The Simulation QA runner does not replace those controls.

## Verdict contract

Issue exactly one verdict for one exact, unchanged 40-character SHA:

- `SIM_PASS`: all selected and combined gates pass; no material coverage gap is
  relevant to the candidate.
- `SIM_PASS_WITH_NOTES`: all gates pass, but a bounded coverage gap or residual
  risk remains and is stated explicitly.
- `SIM_FAIL`: any required gate fails, SHA stability fails, the checkout is
  dirty, or a relevant material risk lacks a safe simulation.

Every verdict includes: candidate SHA; base SHA; changed paths/risk surface;
targeted and full profiles; scenario names; pass/fail/skip counts and durations;
reproduction commands; evidence paths; coverage gaps; residual risks; and a
final remote-SHA recheck. A major change is not delivery-ready without a verdict
for its exact final commit.

## Known baseline gaps

Carry these forward as residual risks, and require a candidate-specific test if
the touched code makes one relevant:

- no direct `service.assistant.outbox.request` timeout, late-result, and
  duplicate-result contract;
- no one-run, all-source sandbox sync through the actual assistant sync HTTP
  endpoints, including non-2xx retry, `latency_ms`, and `sync_paused` states;
- no same-action-ID duplicate delivery assertion in the sandbox consumer;
- limited cross-process idempotency coverage and no memory scale/latency budget;
- native Calendar, Reminders, and Messages contracts are primarily covered by
  Python parser/task/sandbox simulations rather than dedicated Swift fixtures.

These gaps do not authorize live testing or user-data mutation. Escalate to the
candidate owner for a safe deterministic fixture or simulator instead.
