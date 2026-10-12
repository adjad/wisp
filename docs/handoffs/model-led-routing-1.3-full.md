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

Port to the 1.2.5 base (1.3.0): this path is now OPT-IN. Unset or
`WISP_MODEL_LED_ROUTING=0` selects the legacy router; `=1` selects model-led
discovery. With it on by default, the 1.2.5 Workflow20 entrypoints (typed
reminder tasks, priority evidence collection, knowledge/grocery entry tests)
were bypassed, so the default was turned off. Even when enabled, remote-only
builds (Intel read-only, full remote, Aria demo), literal envelopes (knowledge
fields, grocery selectors, queued-send review) and behavior questions stay on
the legacy router. Invalid overrides or an unmanaged, external or mismatched
configured connection (when enabled) fail with an explanation rather than
silently selecting tools through legacy rules. No installed configuration,
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

## Legacy behaviour changes carried by this branch

Two changes reach the default (routing-off) path and are intentional:

- `service/router/semantic.py` `_gate_open`: a request that begins with
  "uninstall X" (optionally prefixed by "please" or "can/could/would you") now
  opens the file-action tool gate, so the semantic retriever may offer file tools
  for it. Previously only the file-action pattern or an explicit writing intent
  did.
- `service/tools/imessage_tools.py` `_match_conversation`: the "Messages are
  refreshing conversation identities" check now also covers fuzzy contact
  matches. A fuzzy match containing a conversation with no identity yet returns
  the refresh message instead of resolving to it.

## Audit-repair round on the 1.2.5 rebase

- F1: the legacy claims (`claim_effect_call`, `claim_workflow_effect`) are blocked
  only by an unresolved model-led effect, as in 1.2.5. The session-wide barrier
  over earlier legacy plans applies to `claim_model_effect` only, so an earlier
  failed, cancelled, superseded or denied plan no longer blocks an unrelated typed
  effect with routing off.
- F2: in a model-led turn, after the private-read signal, `web_search`,
  `web_fetch` and `http_request` are denied for the rest of the turn unless the
  current request itself is an explicit public lookup.
- F3: in model-led turns, `OPAQUE_OUTBOUND_TOOLS` always require confirmation,
  including under full access and standing grants. A validated read-only
  `run_shell` argv (the existing private-shell read path) is exempt.
- F4: `POST /sessions/{sid}/effects/resolve` lets the user mark running or
  uncertain model effects as checked. It requires `Content-Type: application/json`
  and a JSON body of `{"effect_id": "<id>"}` or `{"all": true}` (see N3 below). It
  lifts the session barrier only; the consumed claim stays, so nothing is re-run.

## Audit-repair round 2

- B1: the explicit-public-lookup exemption covers only private signals present at
  turn start (history or remembered facts). Any private read made during the turn
  latches `private_read_in_turn`; while it is set, `web_search`, `web_fetch` and
  `http_request` require a confirmation card showing the URL or query (a policy
  DENY is kept as DENY). The `execute_tool` recheck blocks an unconfirmed call, so
  sibling calls cannot slip past. Turn-start private content with no explicit
  public lookup is still denied.
- N1: the turn-start web signal no longer includes the identity block or the mere
  availability of the messages tools. A URL the user typed is an explicit public
  lookup. (Shell restrictions keep their existing conservative signal.)
- N3: `POST /sessions/{sid}/effects/resolve` now requires `Content-Type:
  application/json` and a body of `{"effect_id": "<id>"}` or `{"all": true}`; a
  bodiless or form-typed request is rejected (415/422), so a cross-site simple
  request cannot lift the barrier.
- N4: the unresolved-action scan skips non-model-led rows without parsing their
  `state_json` when `model_effects_only` is set.

## Audit-repair round 3 (model-led routing only)

- M1: when a turn starts private (private history or remembered facts) and the
  explicit-public-lookup or typed-URL exemption applies, `web_search` stays
  allowed, but `web_fetch` and `http_request` require a confirmation card showing
  the URL unless the URL's host exactly equals a host the user typed in the current
  message. Subdomains, look-alike hosts, userinfo tricks (`host@evil`) and URLs
  containing backslashes or whitespace never match. `execute_tool` rechecks that an
  unconfirmed call is not run. After an in-turn private read the stricter rule
  (confirm every web call, typed host or not) still applies.
- M2: `open_app` with a URL-like `target` (round 3: scheme `http`, `https`,
  `mailto`, `sms`, `tel`, or anything containing `://`; widened to any scheme in round 4) is treated as a private-web tool. After the
  in-turn private latch, and in turns that start private (unless the host is one the
  user typed), it requires confirmation showing the target, even under full access
  or a tool-wide grant. File-path targets and turns with no private context are
  unchanged.
- L3: a result from `run_applescript`, `run_shortcut`, any `mcp_action` tool or any
  `skill_tool` tool sets the in-turn private-read latch (their output can carry
  private data), once the tool actually ran, including when its receipt was
  unverified. Denied, planned and not-run results do not latch.

## Audit-repair round 4 (model-led routing only)

- `open_app` URL detection: any `scheme:` prefix (case-insensitive, leading
  whitespace ignored) is URL-like: `data:`, `javascript:`, `facetime:`, `imessage:`,
  `shortcuts:`, `slack:` and custom schemes included, not just `http(s)`, `mailto`,
  `sms`, `tel` and `://`. A bare macOS path starts with `/` or `~` and never matches.
- Latch: the in-turn private-read latch for `run_applescript`, `run_shortcut`,
  `mcp_action` and `skill_tool` is set from a structured "the tool body was entered"
  flag raised by `execute_tool` just before `run_tool`, not from the output text. A
  tool whose output imitates `(error: ... NOT run)` still latches; a call refused,
  denied or never dispatched does not.
- Strict-host fetch: `web_fetch` in a model-led turn that is private (at start or via
  the latch) runs under `service.research.web.strict_host_fetch()`. It refuses any
  redirect to a different host and skips the Wayback fallback (and the OpenAlex /
  non-matching Wikipedia API detours), so an authorised URL is never sent to a third
  party. Default behaviour for every other caller is unchanged. `http_request` is a
  network write that is separately gated; its redirect handling is not changed here.
- Browser-rendered local files: `open_app` of a file path in a browser app (Safari,
  Chrome, Chromium, Firefox, Arc, Edge, Brave, Opera, Vivaldi, Orion) or of any
  `.html/.htm/.xhtml/.shtml/.svg/.webarchive/.mht(ml)` file is treated like a URL
  target: confirmation after the latch and in turns that start private. This closes
  the write_file then open_app chain.
- Typed-host exemption (documented trade-off). The exemption for fetch-style calls in
  a turn that starts private applies only to `http(s)` URLs on default ports (none,
  80, 443) with no userinfo, whose host equals a host typed in the current message.
  Typed hosts come from `http(s)://` URLs and from bare domain-like tokens, so a
  file-like name such as `report.pdf`, `notes.md` or `tool.sh` whose suffix is also a
  real TLD is accepted as a typed host. That requires the user to have typed it and an
  attacker to control that exact host, and it is judged acceptable; the strict-host
  mode above further stops a redirect or archive hop from leaving it. Narrowing it
  needs a TLD list and would reject legitimate `docs.rs`/`bun.sh` style requests.

### Known follow-up (N2, not fixed here)

Legacy (non-`model_led_effect`) failed plans that still hold a consumed claim block
new model-led effect tools (`claim_model_effect`) for the session indefinitely, and
`effects/resolve` only lifts `model_led_effect` rows. Extending resolve to those
legacy rows needs a decision on which legacy statuses are safe to mark terminal
without re-running anything, so it is deferred. Workaround: start a new session.
