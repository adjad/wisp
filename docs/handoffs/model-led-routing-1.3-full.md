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
Focused tests: the five `tests/test_model_led*.py` modules. The simulation
manifest adds the two new suites. `tests/conftest.py` explicitly selects rollback
for 14 named legacy-router contract suites; the default-on suites remain outside
that fixture and test the unset override through the actual SSE entrypoint.
Delivery must reconcile current `main`, freeze and push one candidate, then bind
mechanical validation, configured CI and independent risk review to that exact
SHA. The original preview's passing evidence does not qualify this successor.
Installation, model activation and release publication are separate actions.
