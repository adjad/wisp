# Wisp 1.3.0 model-led routing

## Delivered behavior

Ordinary `/agent` turns go to the configured managed local agent model before
positive rules, tool retrieval, direct-read shortcuts or new typed task/workflow
compilers. The model receives the original request, bounded conversation history,
current clock and a compact capability index built from the actual registry.
It can answer without tools, clarify, or request authoritative schemas with
`get_tool_schemas`. Real tools become available on the following inference step.
The model chooses families, tools, arguments, order and subsequent reads.

This includes typos, calendar follow-ups, short clarification replies and active
skills. Model filenames are not admission rules. A renamed V2 merged/quantized
model uses the configured agent binding. Unavailable tools and user-excluded
capabilities do not appear in the admitted catalog. Discovery is limited to two
expansions, 24 schemas and bounded schema/index bytes; rejection never falls back
to the entire registry. Schema discovery itself executes no source tool.

The successor defaults to this path. `WISP_MODEL_LED_ROUTING=0` explicitly selects
the legacy path for rollback and comparison. Invalid overrides or an unmanaged,
external or mismatched configured connection fail with an explanation rather
than silently selecting tools through legacy rules. No installed configuration,
model binding, application bundle or server activation is changed by this PR.

## Deterministic execution boundaries

Wisp still validates schema arguments, availability, source exclusions, policy,
grants and exact confirmation before executing the proposed call. These checks
can reject a proposal; they do not choose a positive tool route. A bare assent or
channel fragment without an existing action owner cannot authorize a new effect.
An unqualified family communication request such as “what did mom say today”
excludes public web and opaque egress instead of substituting a web search.

Identity/preferences and history help interpret references. They are labeled
unverified context. Recognized current personal questions require a fresh
personal read or a clarification; a direct old-history answer or unrelated
public search is withheld. An explicit memory prohibition also disables injected
memory and opaque memory-access escape paths. Publisher display-only evidence
remains outside model context and cannot become model-authored action arguments.

Already-persisted legacy actions retain an owner-only recovery path. That mode
can advance a bound clarification or stop an uncertain/closed action, but cannot
compile a fresh request or replacement payload. Ordinary requests declined by
that owner proceed to model discovery. This is an execution/recovery exception
for pre-existing owners, not a new rule-based routing shortcut.

Free-form recovery now requires a tool-free local-model interpretation of
whether the original input answers the old task. The host binds a valid
continuation decision to that exact task ID, revision, missing slots and input;
the task engine rechecks the current snapshot before any filling or execution.
There is no request-verb blacklist granting ownership to all other prose.
Literal title/body answers still use the old exact task and its existing
argument/approval/claim checks after model admission. Exact cancel/retry and
bound outbound-channel controls do not need this extra interpretation.
An independent new request proceeds unchanged to model discovery. An absent,
malformed, stale or ambiguous decision leaves the owner unchanged, blocks
mutations for that turn and asks the model to clarify. The current negative
source envelope still precedes every recovery reader, contact resolution or
Mail warm-up, and excluded turns skip recovery entirely.

This adds one bounded, tool-free generation only when an old task awaits a
free-form reply. It reuses the same owned local transport and closes the stream
and client even on refusal, error or cancellation. Scripted tests establish
host behavior, not actual Ling disambiguation accuracy or latency. Those remain
model qualification work; a wrong model interpretation is still possible.
The interpretation decoder rejects duplicate JSON member names before any
dictionary normalization, including contradictory and repeated identical
values. A unique response must still have exactly the three decision/owner/
revision fields, the current owner and integer revision, one final message and
no tool calls. Contradictory responses cannot become last-member-wins approval.

## Durable model actions

Approved mutations claim an exact session/request/revision and canonical
argument fingerprint in the existing workflow/claim tables before the tool
function starts. There is no new database migration and no extra copy of private
arguments. A concurrent, cancelled, interrupted or unverified attempt stays
consumed across a restart. Changing arguments cannot evade an uncertain
same-tool mutation; an uncertain outbound/opaque effect blocks retargeting.
Known successful completion permits a new explicit request in a later turn,
while repeated calls in the same request remain blocked.

The model and legacy paths share the unresolved-owner barrier. Explicit rollback
cannot retry an unsettled model action. Unknown extension action prose is not a
verified receipt; actions without a supported completion contract are refused
before dispatch. Existing built-in contracts and explicit successful receipt
prefixes remain completion receipts; a failed attempt stays uncertain. Arbitrary
Shortcuts count as potentially outbound even though their category is app
control. Read-only shell commands use the existing safety parser; file
organization previews do not acquire commit claims.
Those receipts are not independent proof of physical-world state. Pure reads
and computation do not acquire action claims. Pending records give the model
bounded factual status, never instructions or replay arguments.

The six working built-ins `run_shell`, `run_applescript`, `software_update`,
`uninstall_app`, `http_request` and `manage_contacts` now produce unchanged
display text with immutable host completion metadata. The registry preserves
that carrier only for the exact loaded built-in function, name, code and phase;
copied extension attributes, generic module prefixes and arbitrary stdout/body
are insufficient. Process exit zero proves process completion, and HTTP 2xx
proves the request's successful response boundary, not all external consequences.
Missing/malformed/failed receipts keep attempted mutations uncertain. Read-only
shell, update checks and update-install refusal before confirm remain claim-free.
The five previously unavailable compatibility tools remain excluded.

User-facing cancellation or supersession cannot clear a legacy consumed attempt
whose result remains unverified. Its barrier survives restart and routing-mode
changes. Cancellation without a consumed attempt and verified completed owners
remain nonblocking.

## Validation and limits

The focused tests exercise the actual SSE entrypoint, discovery state, policy,
argument validation, fake tool dispatch, temporary SQLite claims and cleanup.
Inference clients, identity, memory, skills and tools are synthetic. A scripted
successful choice proves plumbing and boundaries, not that Ling will make that
choice. No real user source, native bridge, outbound action or model runs in
these tests. The private controller records source/test/runtime identities,
denies network/subprocess/native effects and bounds one owned job.

Actual model accuracy and latency require separate isolated inference
qualification. This change normally adds one model discovery generation before
the real-tool proposal compared with a deterministic direct shortcut. A second
discovery can add another generation. It adds no separate classifier model.
Warm/cold timings and speed/accuracy tradeoffs are not claimed here.

Raw contact searches require the first returned native sender header to match
the requested contact; a body mention or an outgoing message is insufficient.
This is conservative when the first row belongs to someone else, and does not
validate every claim in the final answer. Safe no-proof clarifications cannot
include an unverified factual premise.

The fresh-evidence check covers recognized personal requests and relevant
date follow-ups; it is not a universal semantic correctness proof. Source tools
may still have cache/sync or coverage defects. Current-source relevance and
answer quality beyond these boundaries need model evaluation. Uncertain actions
are intentionally conservative: the app does not automatically clear their
claim merely because time passed or the user changed the recipient.

## Handoff

Owned source: `service/main.py`, `service/router/model_led.py`,
`service/agent/loop.py`, `service/memory/store.py`, `service/tasks/engine.py`,
`service/tasks/reply_engine.py`, `service/workflows/engine.py`.
The parity repair also owns the six named emitters across
`service/tools/builtin.py`, `automation_tools.py`, `system_extras2.py`,
`action_tools.py`, `contacts_tools.py`, and `registry.py` result normalization.
Focused tests: the five `tests/test_model_led*.py` modules. The simulation
manifest adds the two new suites. `tests/conftest.py` explicitly selects rollback
for 16 named legacy-router contract suites; the default-on suites remain outside
that fixture and test the unset override through the actual SSE entrypoint.
Historical CI failures are preserved: two established legacy endpoint suites
needed explicit rollback classification; the missing-model test's zero-delay
poll created a race between two real deadlines. Its existing timeout/no-chat
assertions remain, using the real poll sleep. Production readiness is unchanged.
Fresh CI must verify these changes.

Draft repairs cover durable shared claims, rollback/owner-only isolation, opaque
retargeting, read/compute/preview phases, exact timer/memory/window receipts and
fixture provenance, full memory exclusions, native sender/body attribution, safe
no-proof clarifications, source readiness and short date-followup obligations.
The exact `e93deed` candidate subsequently received independent `BLOCK` findings
MLR-OWNER-01, MLR-UNCERTAINTY-02, MLR-PARITY-03 and MLR-SOURCE-04. The same source
owner repaired those scopes with separate recorded tool-emitter/registry
amendments and new controls. That review and its mechanical pass remain historical;
the repaired candidate requires fresh mechanical, CI, independent review and
distinct simulation QA. No earlier passing run or pending QA preparation counts
as qualification for it.
The completed independent review of `7e93669` closed three findings but kept
MLR-OWNER-01 blocked on an alternative calendar-overview request. The focused
same-owner successor replaces lexical continuation inference with the bound
model interpretation described above. The earlier 213 top-level/15 subtest pass
and both CI successes are historical once source changes. This successor needs
fresh exact-head mechanical validation, CI, independent review and distinct QA.
The first residual-owner attempt at `602edf7` actually collected 245 top-level
cases: 223 passed, 22 failed and all 15 subtests passed, with zero denied effects
and verified teardown. The failures were added assertions expecting recovery
interpretation even where the earlier private-source envelope correctly skipped
recovery. Owner equality, no-effect/no-baseline and raw-input checks preceding
that failure passed; the discovery-event assertion following it was not reached
in those 22 cases and must not be reported as passed. The correction explicitly
distinguishes the 22 skipped private cases (zero interpretation/recovery and
four named egress exclusions) from the two admitted event requests (exactly one
tool-free interpretation). All original ownership and discovery assertions
remain. Independent disposition also found MLR-CONTINUATION-JSON-05, repaired
by duplicate-member rejection plus conflicting/both-order/same-value controls.
The failed run and all prior proposals are preserved; the remaining bounded
attempt requires a fresh committed candidate, assessment and exact admission.
The first controller run was unqualified because subtests were incorrectly
counted as collected top-level cases. The second run had one fake timer module
metadata failure (119 of 120 top-level cases passed and 15 subtests passed). Both
failures are retained in the private controller artifacts. Final qualification
uses a fresh reconciled candidate and verifies top-level identities and subtest
outcomes separately; those earlier runs are not final pass evidence.
Delivery must reconcile current `main`, freeze and push one candidate, then bind
mechanical validation, configured CI and independent risk review to that exact
SHA. The original preview's passing evidence does not qualify this successor.
Installation, model activation and release publication are separate actions.
