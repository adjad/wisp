# Daily Summary and source-first day planning

User outcome: make Wisp's daily responses useful for deciding what to do, with
a **Needs my attention** tab, a realistic **Day plan**, and an optional daily
email to the user's own connected address.

Base: `ec7c8d60984e85af8e9949cd7c339a7db5734fe4`.
Sole builder: existing chat `01a13e33-ce4e-7a53-bf23-deea0f38417a`.
Worktree: `/Users/adijain/.codex/worktrees/daily-attention-planning/MOE_Project`.
Branch: `codex/daily-attention-planning`.

## Planning contract

1. Calendar commitments and explicit authored/pinned tasks establish the day.
2. Due/overdue reminders supply proposed work. Undated reminders remain
   attention candidates without receiving a made-up deadline.
3. Email, Messages and Notes contribute attributed review suggestions. Their
   receipt or modification time is never a task deadline. An email's relevance
   or unread state is not proof that a response, payment or task is owed.
4. The behavior owner's public availability result may reserve otherwise
   unlisted time. Holds are tentative, evidence-backed and correctable for the
   selected day. Missing, stale, weak, incomplete or timezone-mismatched results
   reserve no time. Calendar entries, away time, websites and low interaction
   do not establish attendance, productivity or a specific activity.
5. Scheduling remains a proposal. No Calendar/Reminders writes occur. Source
   task durations and review blocks are explicitly estimates. Existing Today
   freshness, unknown-end, all-day, pin and DST rules remain in force.

The pure `assess` boundary accepts frozen source evidence and availability
windows. Cache adapters read existing source contracts and disclose missing,
partial and bounded coverage. The same grounded renderer serves the daily
answer and email preview.

The version-1 availability interface is a host-owned read-only callback in
`daily_workspace.py`, registered only by the behavior producer. Its envelope
contains `version`, `available`, `status`, `generated_at`, `timezone`, `coverage`,
`reason`, `windows`, and optional generic `demands`. Windows retain an ID,
exact start/end, timezone, current status, confidence, observation date, coverage,
evidence references and explanation. Generic demands retain `kind`, ownership,
source quote/reference, optional due time and `suggested_minutes` with
`duration_basis` (`user`, `source`, `model`, `default`, or unknown). Estimates
retain their uncertainty. Unknown duration stays in attention until the user
chooses an estimate; unknown ownership yields review work, and other people's
tasks are withheld. Importance stays unknown unless the user explicitly supplied
a priority. No topic turns a generic demand into a study task automatically.
The tab provides an editable estimate before adding a Wisp task.

The actual availability producer is **not implemented in this candidate**. It
belongs to the separate behavior stage-2 workstream. Production explicitly says
availability is unknown and reserves no behavioral time until that public
producer is connected. The 14–16 and 18–19 examples exist only in test fixtures.

## Daily email contract

Default off. The user selects one recently verified connected self address,
daily wall time and named timezone, reviews a complete example, then explicitly
enables. Preview tokens expire and bind the exact settings revision. Changing
recipient/time/zone requires another preview. Pause invalidates previews.
The host receipt also binds the feature/generator version, approved source
scope and current connected-account session/set.
The approved scope includes tentative behavior-derived planning suggestions;
it does not authorize new collection or raw behavior export. A receipt created
without this scope cannot enable or submit under the expanded scope.
Fresh native identity uses
server-issued capture sessions/generations; delayed, empty or failed reads
cannot restore old eligibility. Account changes, expiry and backend restart
require explicit review again. Existing policy denial always blocks submission.

A new complete plan is composed inside a 30-minute delivery window. Source or
identity holdback can recover inside that window. Sleep/missed windows and DST
gap/fold times are skipped. A durable daily claim precedes submission, survives
restart and prevents duplicate automatic attempts, including after unknown or
non-attempted results. Changing timezone also observes a 20-hour submission
cooldown. A submission already claimed may finish after Pause.

Mail's success means submission accepted, not inbox delivery. No automatic
retry follows an uncertain native result. Occurrences retain a body hash rather
than the body; expiring previews and state live in a private local SQLite file.
There is no generic send grant, arbitrary recipient, model-derived destination,
or widening of existing outbound policy.

## Integration and validation

The Orchestrator acknowledged the first slice in
`WISP_DAILY_ATTENTION_PLANNING_OWNERSHIP_ACK_20261014.json`. New planning,
consent, API, test and documentation files are owned by this builder. Exact
Today/UI/brief/scheduler/native-identity seams, runner classification and the
pure Swift fixture were subsequently acknowledged in the corresponding
`WISP_DAILY_*_ACK_20261014.json` records. Behavior collectors, Gmail/provider changes, generic outbound
policy, assistant storage and inference/routing remain with existing owners.

Development validation before freeze:

- 188 focused Python tests passed (new workspace/email tests plus existing
  Today contracts).
- 2,206 existing summary, priority, presentation-text and QA-runner cases passed.
- 89 native model/transport/decoding checks passed; the GUI branch was explicitly
  disabled. The original 78 checks remain.
- Full Swift app compilation passed without launching the app. Existing SDK
  deprecation/Sendable warnings remain outside this change.
- Development failures retained: system Python lacked pytest; a missing-end
  fixture accidentally supplied its default; the DST fixture needed the new
  reviewed scope; final-bridge fakes initially imported the package's exported
  Hub instance instead of its module. Corrections kept the intended assertions.
  A pre-existing Starlette/httpx deprecation warning appears in focused tests.

The first frozen candidate was held before review after a fake policy result
with a plain string `confirm` was accepted by a str-backed enum comparison.
The successor requires a real `Decision` and `Tier`; malformed results produce
no fake-bridge effects. The same repair batch aligns the disconnected behavior
consumer with generic work demands. Earlier candidate gates remain historical
and cannot approve the successor.

Historical `6eb5c01c` full mechanical run exited successfully with 254/254
modules passing and 165 skipped cases: 11 link/directory parametrizations,
one missing installed Ling template, 151 copied-interpreter process-path
cases whose interpreter could not start, and two root/filesystem-dependent
attestation cases. None of the daily feature tests were skipped. This ledger
is a coverage limitation, not live attestation or a zero-skip pass. The new
candidate needs its own complete result and independent relevance assessment.

Read-only helper findings were reconciled by the sole builder: flexible work
cannot erase busy windows, native/manual source caps are separate with true
omission counts, partial Calendar reads block placement, Messages retain unique
native provenance when matchable, Notes require read-start freshness, preview
bodies are transient, malformed submission receipts persist unknown, terminal
outcomes survive later sweeps, and identity revocation wins stale/equal replays.

Final delivery requires a clean current-main-reconciled pushed candidate,
exact-SHA mechanical checks, required CI and distinct independent review plus
synthetic specialist QA. No app launch, real model/provider/native access,
communications, installed-app replacement or deployment is part of validation.
