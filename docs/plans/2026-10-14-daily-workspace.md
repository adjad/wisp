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

The development consumer composes the public
`service.assistant.behavior_runtime.planning_context(dayISO, timezoneIANA, now)`
read lazily through `availability_snapshot`. The host/test override accepts that
same public v1 shape. The actual envelope uses `ready`, `generated_at` (oldest
analysis), `evidence_checked_at` (current read), `windows`, `demands`,
`associations`, and a descriptive `source_coverage` map. It does not provide the
placeholder's global `available/status/coverage` proof. Missing, disabled,
exceptional, malformed, stale or revoked results remain unavailable; the
consumer never caches or renews evidence age and never asks for inference.

Windows retain their own `evidence` IDs, coverage, observation time, explanation,
and unscaled recurrence strength. The existing 0.65 admission threshold applies
only to these windows; it is not a calibrated probability or a demand threshold.
Calendar commitments, explicit pins and day-local corrections still take
precedence. No new collection, scheduler, sender, native or policy path exists.

Durable work demands have `confidence=None` and `importance="unknown"`. The
consumer preserves the exact quote, opaque source ID, structured reference,
source/revision/span/hash endpoint, original publication receipt, original
analysis time, source observation time (including unknown Notes prose time),
temporal proof, deadline precision/range, duration provenance, priority dimensions
and uncertainty. The existing native attention decoder receives a string JSON
locator in `reference`; the original dictionary is additive `source_reference`.
No native URL, source timestamp, confidence, priority or date instant is invented.
`due_ts` is used only for an exact instant; date ranges stay ranges, with explicit
half-open bounds in rendered text. Unknown ownership or deadline stays review
attention without flexible work placement. A tentative user-owned demand with a
future grounded date/instant and valid adjustable default duration may propose a
`Review:` block. It never becomes a scheduled event or stored assigned task.
User-authored work and explicit priorities continue to win; an inferred importance
hypothesis cannot override them. No automatic topic-specific task kind exists.

Consumer development base: `bebf005e45af3b3c0dd5bbfc45078c60e998c28e`;
worktree `/Users/adijain/.codex/worktrees/daily-behavior-context/MOE_Project`,
branch `codex/daily-behavior-context`, single writer
`/root/consumer_adapter_builder`, under the root's
`WISP_DAILY_BEHAVIOR_CONSUMER_SINGLE_WRITER_HELPER_ACK_20261015.json`.
Only this module, its tests and this plan are in the assigned delta. Frozen PR232
and the separately owned producer are not modified.

The actual producer is absent from this base. Its development snapshot
`587961a5885865e5254e4053c9c541f7952fdcc5` was inspected statically, including the
public wrapper, `BehaviorRuntime.planning_context`, availability windows,
commitments, source adapters, seed materialization and association work proofs.
Only wire-shaped fakes and a fake public wrapper are executed here. Durable
`associations.materialize_work` supplies the proof fields above; the older
cache-only `commitments.validate` shape does not supply original analysis,
publication receipt or a saved endpoint, so those demands are withheld. Supporting
that older path requires the producer owner to expose these exact proofs rather
than synthesizing them in this consumer.

Admission conservatively retains the daily consumer's 20-minute analysis limit
and applies it independently to original publication/analysis and read timestamps;
new reads cannot extend it. Original source time can be older (or unknown for
Notes), while recurrent window observations retain the existing 21-day limit.
This can withhold otherwise reusable durable hypotheses until fresh analysis is
published. Source coverage remains descriptive, never a blanket adequacy claim.
Consent, generation, role, and current source-revision checks remain the public
producer's responsibility on every call; consumer fakes verify the read seam and
revocation behavior, not the producer's implementation of those checks.

The producer's root-owned retention/checkpoint repair, producer qualification,
and a root-acknowledged combined/current-main candidate remain integration
barriers. This branch does not claim assembled producer qualification or an
end-to-end beta. Final integration requires testing the actual public producer
and consumer together, then all required same-SHA mechanical, CI, independent
review and specialist QA gates.

Development consumer validation uses `/usr/local/bin/python3` to launch the
qualified `/private/tmp/wisp-behavior-py313.fxCgTl/bin/python` (3.13.14) with
`subprocess.run`, bounded timeouts, exact worktree `PYTHONPATH`, private fresh
`HOME`/`WISP_HOME`, `PYTHONDONTWRITEBYTECODE=1`, and `WISP_BEHAVIOR=0`.
The final child harness installs a fake public producer module before pytest
collection, asserts that it remains installed, and checks zero forbidden runtime,
source, model or action calls. This also fences existing Today GET fixtures;
earlier runs could import the inert base module while discovering the absent
public method, but never instantiated a runtime or called its sources.
`python -m pytest -q -rs -p no:cacheprovider tests/test_daily_workspace.py tests/test_today_plan.py`
passes 207 cases, with zero skips and two existing dependency deprecation
warnings. The initial development attempt stopped at collection because the
malformed-window fixture helper tried to iterate `None`; the helper now retains
that invalid shape for admission tests. An intermediate run passed 205 cases;
two additional original-time/HTTP GET cases bring the focused suite to 207.
These checks include synthetic HTTP GET composition, no inference/action calls,
public-read revocation, freshness, endpoint provenance, generic topics, date
ranges, unknown owner/time/effort, recurrence strength, source-first Calendar
admission, pins and corrections. No actual producer runtime, native reader,
model/provider, credentials, real user state or external action is exercised.

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
Workspace mutations require a loopback peer and Wisp's fixed local host/port
(`8765`); explicit browser origins must match that trusted local origin.
Foreign/null origins, forged hosts, cross-site fetch metadata and duplicate
security headers are rejected before mutation. Native URLSession requests
without an Origin header retain their existing format.

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

- 236 focused Python tests passed (new workspace/email tests plus existing
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

The `72015ac4` candidate was also held before independent review after a simple
foreign-origin identity-begin request cleared synthetic verification. The
router-local admission repair covers all six workspace mutations. Regression
cases first reproduced 42 boundary failures; the corrected suite passes.
Two initial IPv6 positive cases hit an upstream TestClient URL-parser error;
they now supply IPv6 Host/peer metadata in ASGI instead, without weakening
their capture assertions. This is synthetic admission coverage, not live IPv6
transport or installed-app QA. `72015ac4`'s local 254-module/Swift/native and
required CI passes remain historical; the new candidate requires fresh gates.

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

The `08ee09f9` candidate was held after additional isolated helper tests found
identity expiry could be hidden by a same-account refresh, and the Today
workspace assessed Calendar freshness differently from the direct workspace.
Identity expiry now breaks consent continuity before beginning or replacing a
capture, including time spent pending. Expired or clock-invalid captures revoke
verification with a handled validation error. Continuous fresh same-account
refreshes preserve consent; a verification gap requires another preview and
confirmation. Both workspace entry points share native Calendar admission,
while the original Today plan and debug replay retain their existing contract.
Synthetic controls cover the actual persisted receipt writer accepting missing
or future read-starts and clamping stale read-starts into stale sync times.
These are development results; the successor requires fresh complete gates.

PR #219's reviewed email provenance changes were subsequently merged into
`main` as `dcb0ed5f`. The daily branch reconciles that base and preserves the
union of Simulation QA registrations. Composition fixtures exercise the actual
public `email_source_details` and `header_rows` against private synthetic
backing: fresh/empty, stale/future/corrupt/restored/error receipts, account
separation, duplicate native account labels, and a receipt changed during a
read. Unverified body globals never supply text; an eligible immutable body
snapshot retains precedence. Header text remains review context without a
fabricated deadline or assignment. Fresh final gates use the pinned 3.13.14
runtime through its verified normal subprocess launcher, with their own skip
ledger; historical 3.14 runs are retained separately.

Independent review of `7f5e93f4` blocked three additional integration paths.
That candidate's green mechanical/CI evidence is historical, not approval of
these repairs. The complete Auditor and Simulation findings were routed to one
writer in `WISP_PR232_7F_THREE_P2_SINGLE_PRIMARY_REPAIR_ACK_20261015.json`.

The native header scan now obtains its server capture fence before reading any
account, index or header. A missing acknowledgement aborts that scan and releases
its claim. Every acknowledged scan settles its generation: empty accounts,
closed Mail, duplicate/unknown inventory, partial/failed headers and invalid
identity reads cannot retain sending eligibility. Legacy display publications
remain separate; a successful unified-inbox fallback cannot verify an account.
Only a genuine complete account-address read restores eligibility. Existing
expiry, supersession and continuously fresh same-account consent rules remain.

The narrowly owned receipt writer retains typed Calendar completeness metadata
and records an incomplete latest snapshot as unavailable. Malformed supplied
flags become conservative boolean proof, rather than rolling back and leaving
an older successful receipt eligible. Date coverage and original read-start
ordering remain atomic; equal/older results cannot downgrade a newer receipt.
Authored work is planned around commitments and supported tentative holds first.
Actual fitting placements are reserved only in local copies before generated
suggestions fill remaining gaps; stored pins, revisions and replay inputs do not
change.

After the fixture correction, development checks passed 319 focused cases with no skips, including the
existing Today contracts. They compile exact
production native scan/capture/post bodies and transport into
`NativeMailIdentityFlowChecks.swift`'s inert collaborators, then replay recorded
requests through actual API handlers into a fake submission bridge. They also
exercise actual Calendar ASGI/store round trips through Today, direct workspace
and email readiness, including malformed flags after a successful receipt;
constrained-day, remaining-gap and cross-day-pin controls cover authored work.
The native fixture never invokes Foundation networking, Mail or AppleScript;
its result is synthetic caller qualification, not native runtime QA. Equal/older
partial results also preserve a newer complete receipt. The frozen successor
needs fresh evidence for every final check.

The first repair candidate `40e94aa7` passed its configured mechanical run but
remained held: its native fixture used an overly broad initial-closed branch
that shadowed two mid-read scenarios. The corrected fixture distinguishes those
branches with exact execution traces and adds closure between two account reads;
its successor requires fresh validation rather than inheriting the old result.

The separately committed behavior v1 adapter remains held and is not grafted
here. Its fake-producer tests do not establish integration: durable analysis
ages, source-specific receipt eligibility and cross-timezone evidence still
need their separately owned contract repair and actual-wrapper qualification.

Final delivery requires a clean current-main-reconciled pushed candidate,
exact-SHA mechanical checks, required CI and distinct independent review plus
synthetic specialist QA. No app launch, real model/provider/native access,
communications, installed-app replacement or deployment is part of validation.

Independent review of `cda9008d` found that the raw and history readers also
observe account inventory. A raw empty-account observation updated legacy
content caches without revoking the feature identity. The sealed reproduction
reached the fake send bridge with old consent, so that candidate remains held.
The single-owner successor extends the same capture boundary to all three
native entry points, including early exits and helper-controlled interruption.
Raw and history content failures alone do not mean an account disappeared:
a known nonempty inventory may recover only through a genuine complete live
address read. Closed, unknown, duplicate or empty inventory cannot preserve old
sending eligibility. A closure actually observed during a loop stays failed
for that capture even if Mail reopens before settlement. Cached index/history
content cannot verify identity.

Begin response admission tracks server session/generation and local request
ordering without holding a lock during native work. An older acknowledgement
cannot begin new work after a newer acknowledged capture; a restarted backend
can issue a new-session generation one, while retired-session responses remain
ineligible. Each terminal request retains its own original capture. Server
ordering, original read-start expiry, account-set epochs, final consent and
claim-once admission remain authoritative. Existing scan coalescing and history
yielding remain separate from identity generations.

`NativeMailObserverFlowChecks.swift` executes byte-exact production header,
raw and history callers, capture/order logic, inventory parser, posting methods,
history helpers and pure history state with inert script/database/URLSession
collaborators. Its request traces are replayed through the actual legacy and
feature API handlers, real private legacy cache functions, real Decision/Tier
policy and final scoped admission into a fake bridge. Controls cover empty and
failed inventory, closures between accounts/batches and reopening, body-only
failures, index/commit supersession, malformed/capped/deferred history, failed
begin acknowledgement and retry, cross-entry delayed receipts, reordered begin
responses, restart, expiry, duplicate completion, account-set changes retaining
the original self address, continuous fresh same-account receipts, and yielding
to a pending raw scan. The original 17-branch header fixture is preserved.
All runtime state is disposable; this does not qualify actual native readers,
real inbox delivery or a running Wisp app. The behavior adapter remains separate.


The static ordering helper also caught a cold-start compatibility gap in the
working repair: a newer raw capture could suppress a delayed initial header
acknowledgement. The source-bound cold-start control first failed on that
working snapshot. Within the existing claimed header invocation, a valid but
superseded same-session acknowledgement may now reacquire one fresh capture.
Both requests share the original eight-second deadline; no new scan or timer
is created. Failure, malformed response, retired session or another
supersession releases the claim without native work. Existing later header
invocations can recover readiness. Raw/history captures do not reacquire.
The fixture checks successful readiness recovery, bounded failure paths,
subsequent recovery, no third header begin, and real final fake admission.
This bounds a startup interleaving; it is not a universal starvation guarantee
under unbounded competing requests.

The full `64bf99ad` audit closed the earlier identity, Calendar and authored-work
findings but blocked malformed begin acknowledgements. Under fault injection,
an empty or short session could retire the valid backend session locally and
prevent subsequent valid header/raw/history calls from recovering. Backend
receipt validation and final sending remained closed; this was a persistent
client-readiness failure, not an unauthorized-send reproduction. The sealed
complete finding is `DAILY-64BF-R1-MALFORMED-CAPTURE-SESSION-POISON`.

The capture handshake now checks the complete session/generation envelope
before accepted ordering or retired sessions can change. Sessions must be
strings of 20–100 characters. Generations must be representable positive
integers; CoreFoundation type checks reject JSON booleans, and floating numeric
representations are rejected before integer bridging. Invalid responses release
the existing claim, perform no native work or header reacquisition, and leave
the legitimate session available to future invocations. Genuine backend restart,
retired-session refusal and the original capture deadline remain intact.

The actual-caller fixture first reproduced the failure before correction.
Recovery controls cover an invalid initial acknowledgement and valid-session A,
malformed acknowledgement, then two valid A invocations in every reader. Shapes
include session length/type/null/missing and generation boolean, zero, negative,
string, null, missing, fractional and unrepresentable values. Session boundaries
20/100 and the maximum representable generation retain positive controls.
Recorded requests pass through real private legacy caches, feature handlers,
policy and final fake admission without a status read. Separate malformed
terminal-receipt controls preserve backend rejection and later genuine native
recovery. JSON `1.0` reserialized as `1` is not counted as a distinct malformed
wire case. The successor requires fresh full gates and independent reviews;
actual app/native/provider execution and the separate consumer remain outside
this qualification.

The first protocol-validation successor `3f54c7ac` remained held after self-review
found that Swift grapheme counts differed from backend Unicode length. Its
already-running checks completed before further editing; they are historical.
The exact-caller Unicode control first reproduced native work after a malformed
100-grapheme/200-scalar acknowledgement. Session bounds now count Unicode
scalars, matching the backend. Controls cover 19/20/100/101/200 scalar cases,
including valid 20- and 100-scalar sessions containing one grapheme, across
header/raw/history recovery and actual backend admission. Canonical ASCII
server tokens retain their original behavior. This is synthetic protocol
resilience evidence, not a report of a live backend sending malformed tokens.
