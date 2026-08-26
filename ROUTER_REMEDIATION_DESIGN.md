# Wisp router remediation design

Date: 2026-08-24

## Implementation status

Implemented and installed on 2026-08-24. The production shape uses the design's
semantics with an incremental representation: `required_tool_groups`,
`forbidden_tools`, and `conditional_tools` live directly on `RouteDecision`,
while `ToolOutcome` and the per-turn ledger live at the registry/agent-loop
boundary. This avoids a flag-day conversion of every registered tool.

The final intercepted acceptance run passed 20/20 contracts; the full local
regression suite passed. See `ROUTER_ADVERSARIAL_LIVE_REPORT.md` and
`router_adversarial_final_results.json`.

## Outcome

The rerun confirms that Wisp needs two layers of repair:

1. A tactical router patch for deterministic misroutes and missing tools.
2. A structural execution contract that prevents partial work, ungrounded
   outbound content, and false success from being presented as completion.

Prompt-only rules are not an adequate fix. Several failures occur because the
needed tool is absent or withheld, the wrong tool is router-direct dispatched,
or the loop has no machine-readable definition of "done."

## Evidence from the intercepted rerun

Twenty distinct adversarial cases were run through the production router and
resident model with `test_mode=true`. Four met their declared tool contract;
sixteen failed. No assistant tool executed.

Stable failures included:

- Calendar was forced for stock and generic “about my …” payloads.
- Bare calendar lookup still used `get_upcoming({})`, preserving its seven-day
  default.
- Conditional Low Power Mode stopped after `get_battery_status` because the
  router-direct path exposed no conditional write step.
- File/PDF, weather, and plain-text-file clauses lost their required tools when
  combined with email, reminders, or Notes.
- Imperative “complete the laundry reminder” became a calendar read instead of
  `complete_reminder`.
- “Without opening my inbox” still opened the email domain.
- Capability questions were answered from the retrieved per-turn subset rather
  than a deterministic inventory.
- Multi-step writes could be described as successful even though the dry-run
  result proved no action ran.

`test_mode` returns synthetic data, so later-call omissions in a chain are less
conclusive than first-call selection. The wrong first tool, missing schemas,
router-direct arguments, and false-success narration are conclusive.

## Root causes

### 1. The router returns capability, not obligations

`RouteDecision` currently carries `tool_subset`, `force_first_tool`,
`multi_round`, and `narration_after`. These fields control availability and
when reasoning can be skipped, but they do not describe:

- which user clauses must be satisfied;
- whether one tool or all tools in a group are required;
- dependencies between reads and writes;
- forbidden tools implied by “do not,” “without,” or “not X”;
- exact arguments such as a two-week period or a 60-day calendar horizon;
- what successful effect must exist before Wisp may say “sent,” “queued,”
  “created,” or “completed.”

Consequently, a clean result from the first selected tool can look like a
finished turn even when it covers only one clause.

### 2. Broad patterns assign the wrong source

`_TOPIC_LOOKUP_RE` treats any compose request matching “about/with/regarding/on
my|the <word>” as calendar-first. That is why stock prompts force
`get_upcoming`. It also gives a move-in-date request the default seven-day
window, which cannot find a later event.

### 3. Router-direct dispatch is allowed on conditional requests

`_direct_device_call` can resolve `get_battery_status` as the only direct call
even when the user also specified a condition and a write action. The direct
read is valid, but it is not the complete request.

### 4. Claims do not yet cover all domains

The claim-merging refactor covers personal-data domains well, but document,
file, web/weather, and device clauses are still partly handled by later
exclusive routes or retrieval. Email can therefore claim a PDF request before
file tools are added; Notes can claim “plain-text file” without `write_file`;
and reminders can claim a weather-conditioned request without weather tools.

### 5. Tool results are prose, not typed outcomes

The loop infers success with `is_tool_error(result)`. A non-error string is not
enough to prove an effect. Dry-run results, no-match results, user denial, and
“nothing was scheduled” need distinct statuses. This is also the reason final
prose cannot currently be checked against actual effects.

## Proposed architecture

Keep the single resident model and existing rule router. Extend the existing
claim system into a small deterministic plan/verification layer; do not add an
LLM classifier or a second planning model.

### A. Add a turn execution contract

Add these internal types near `RouteDecision` in `service/router/router.py`:

```python
@dataclass(frozen=True)
class ToolRequirement:
    id: str
    any_of: frozenset[str]
    min_successes: int = 1
    required_args: tuple[tuple[str, object], ...] = ()
    depends_on: frozenset[str] = frozenset()
    effect: str = ""  # read, draft, sent, scheduled, created, completed


@dataclass(frozen=True)
class TurnContract:
    requirements: tuple[ToolRequirement, ...] = ()
    forbidden_tools: frozenset[str] = frozenset()
    forbidden_effects: frozenset[str] = frozenset()
    grounding_sources: frozenset[str] = frozenset()
```

Add `contract: TurnContract` to `RouteDecision`. Extend `_Claim` so each claim
contributes tools, requirements, forbidden tools/effects, and optional resolved
calls. `_merge_claims` must union the tools and obligations without turning a
write cue in one clause into permission to mutate every matched domain.

Requirement semantics:

- `any_of={view_emails, summarize_emails}` means either successful tool covers
  one inbox-read obligation.
- Four distinct requirements for calendar, notes, email, and messages mean all
  four sources are required for an aggregate request.
- `depends_on={source_lookup}` prevents a draft/send step from counting as
  valid before its factual source has succeeded.
- `effect="sent"` is satisfied only by a successful send outcome, never by a
  lookup or a model sentence.

`narration_after` can then be derived from satisfied requirements and gradually
retired. This avoids maintaining two definitions of completion.

### B. Add a typed outcome ledger

Introduce an internal `ToolOutcome` in the agent loop/registry boundary:

```python
@dataclass(frozen=True)
class ToolOutcome:
    status: Literal[
        "succeeded", "no_match", "needs_input", "denied", "failed", "planned"
    ]
    text: str
    effect: str = ""
    facts: dict[str, object] = field(default_factory=dict)
```

The model may continue receiving `outcome.text`, preserving the current chat
format. The loop stores the full outcome in a per-turn ledger keyed by tool
call ID. Migrate high-risk and routing-critical tools first:

- `send_message`, `send_email`, `schedule_send`, and draft tools;
- `add_reminder`, `complete_reminder`, calendar mutations;
- `get_upcoming`, `get_stock_price`, `get_weather`;
- `search_notes`, file discovery/read/write;
- `get_battery_status`, `toggle_setting`.

Compatibility wrappers can classify legacy string results until the remaining
tools are migrated. `test_mode` must record `status="planned"`; it must not add
the tool to the successful-answer set.

Before final narration, evaluate the contract against the ledger:

- If all requirements are satisfied, narrate normally.
- If data is missing or ambiguous, ask the specific required question.
- If an action was denied or failed, state that outcome deterministically.
- If the model stops early, re-enter selection with only unmet requirements'
  tools, up to the existing step limit.
- Never use a read/list result as evidence that a write succeeded.

### C. Ground outbound actions before confirmation

Before showing a confirmation card for send/schedule tools, validate the exact
arguments against the turn contract and ledger:

1. Resolve the recipient non-mutatingly. Reject zero or multiple matches.
2. Require every date, time, price, address, code, and other structured fact in
   the outbound body to come from either the user's literal prompt or a
   successful source outcome in this turn.
3. Verify exact-span requests against typed source facts. A request for two
   weeks cannot accept a `1mo` stock result.
4. Bind the confirmation to a fingerprint of channel, resolved recipient,
   subject/body, and delivery time. Execute those exact approved arguments.
5. Record `sent` or `scheduled` only from the action tool's successful outcome.

Start with structured facts rather than attempting to prove every prose phrase.
This closes the dangerous failures—wrong dates, prices, recipients, and claimed
effects—without another model call.

### D. Make final action status deterministic

After the model drafts its prose, append or replace the action-status sentence
from the ledger. Examples:

- `Message sent to Mom.` only when a matching `sent` outcome exists.
- `Nothing was scheduled; the requested time was in the past.` on a failed
  schedule outcome.
- `Dry run only; no reminder or email was created.` in `test_mode`.

This status line should not be free-generated. The model can still write the
human explanation around it.

## Tactical router patch

Ship these deterministic changes before the full outcome migration. They are
small, independently testable, and address the highest-frequency wrong routes.

### 1. Replace generic topic lookup with payload-source claims

Remove `_TOPIC_LOOKUP_RE` as a reason to force calendar. Add narrowly mapped
payload claims:

| Payload language | Source tools | Initial arguments |
|---|---|---|
| schedule, meeting, deadline, move-in date | `get_upcoming`, then Notes/Messages fallback | `days=60` when no narrower horizon is stated |
| stock, price, trading, ticker | `get_stock_price` | preserve companies and exact period text |
| rain, weather, temperature | `get_weather` | derive place/time from the referenced event before acting |
| PDF, report, document, file | file discovery + `read_file` | preserve named folder/type |
| password, door code, saved note | `search_notes` | preserve the named fact as query |

If “about my X” has no recognized source, offer semantically retrieved read
tools and require clarification rather than defaulting to calendar.

### 2. Resolve time/range arguments in Python

- Bare “what's on my calendar?” -> router-direct
  `get_upcoming({"days": 60})`.
- Explicit today/tomorrow/week horizons -> resolved values from
  `_calendar_window_days`.
- Exact stock “two weeks” -> `period="2 weeks"`, never a month bucket.
- Preserve the user's exact span string when the stock tool already supports
  it; do not make the small model translate it.

For a compound request, a resolved read may be placed in `direct_calls` while
the remaining action tools stay in `tool_subset`. Direct reads must not imply
that the full contract is complete.

### 3. Restrict direct device dispatch to complete requests

Decline `_direct_device_call` when the prompt contains an if/unless/otherwise
condition, a second action, or a dependency. For the battery case expose:

- `get_battery_status` as the required first read;
- `toggle_setting(setting="low_power_mode", on=True)` as a conditional write;
- a contract branch allowing a percentage-only answer when the threshold is
  not met.

Longer term, return battery percentage as a typed fact so the branch is
evaluated in Python rather than inferred from prose.

### 4. Promote file, document, weather, and device to mergeable claims

Move these domains into the same claim collection used for personal data. This
specifically makes the following reachable:

- PDF -> discover/read -> `draft_email`;
- next outdoor event -> weather -> conditional reminder;
- Notes -> `write_file` for a requested plain-text file;
- `system_status` + `wisp_status` when the user asks why Wisp affects the Mac.

### 5. Add hard negative constraints

Parse explicit negative clauses before tool union:

- “without opening my inbox” forbids inbox tools;
- “do not send” removes send tools but leaves drafts;
- “do not add or change anything” forbids mutations;
- “not the calendar event” removes calendar mutation for that clause;
- “read; don't modify” forbids writes.

Forbidden tools must be enforced by the loop in addition to being absent from
the advertised schema.

### 6. Fix reminder completion and continuation maps

- Extend `_COMPLETE_RE` with imperative `complete`, `mark ... complete/done`,
  and the named-reminder form.
- Force `complete_reminder` when the target is a reminder and explicitly
  subtract calendar-event mutation when the user says “not the event.”
- Add `create_note`/`append_note` to `_WRITE_TOOL_DOMAIN` so “yes, go ahead” can
  inherit a Notes write.
- Prefer prior channel/action context for “reply” and queue-cancellation
  continuations before generic compose rules.

### 7. Add deterministic capability inventory

Create `wisp_capabilities`, generated from the actual registry plus explicit
capability metadata (read, draft, send, schedule, destructive, unsupported).
Router-direct capability-matrix questions to it and short-circuit the returned
answer. Do not let the model infer global capability from the seven schemas
retrieved for one turn. Banking and vision must be explicitly represented as
unsupported unless a real tool is installed and available.

### 8. Make self-delivery consistent

Treat “email/text it to me” as draft-only unless the user explicitly confirms
a resolved self-recipient through the existing self-send guard. Do not expose
immediate send tools on the first pass. This keeps sensitive data such as a
Wi-Fi password out of an accidental send path.

## File-level implementation map

| File | Change |
|---|---|
| `service/router/router.py` | Contract types; claim obligations/forbids; payload-source mapping; exact arguments; direct-dispatch restrictions; completion/capability fixes |
| `service/main.py` | Pass contract to `run_agent`; expose a redacted contract summary in routed debug events |
| `service/agent/loop.py` | Outcome ledger; unmet-requirement retries; forbidden-tool enforcement; deterministic action status; test-mode `planned` semantics |
| `service/tools/registry.py` | Normalize typed and legacy tool outcomes |
| `service/tools/action_tools.py` | Typed outbound outcomes and resolved-recipient/fingerprint metadata |
| `service/tools/assistant_tools.py` | Typed calendar/reminder facts and effects |
| `service/tools/web_tools.py` | Typed actual period/start/end facts for stocks and event-time facts for weather |
| `service/tools/search_coverage.py` or a new `capability_tools.py` | Deterministic full capability inventory |
| `tests/router_adversarial_cases.py` | Add exact-argument, forbidden-tool, effect, and dependency expectations |
| `scripts/eval_router_adversarial.py` | Score argument predicates and contract completion, not only called tool names |

## Test strategy

### Static tests

Add standalone tests consistent with the repository's existing test style:

- `tests/test_router_execution_contract.py`
- `tests/test_tool_outcomes.py`
- `tests/test_outbound_grounding.py`
- `tests/test_action_status_truthfulness.py`

Use `tests/stub_embedder.py`; no model invocation is needed for router shape.

For each adversarial case, assert:

- required and forbidden tool availability;
- forced/direct tool and exact arguments;
- required obligation groups and dependencies;
- clarification flags;
- no send tool on draft-only/self-delivery prompts;
- no dangerous tool introduced by an unrelated write clause.

### Agent-loop simulations

Use fake tools/outcomes to cover:

- first source succeeds, second obligation remains unmet;
- denied send cannot become “sent”;
- failed schedule cannot become “queued”;
- test-mode `planned` cannot become “created”;
- exact period mismatch blocks outbound confirmation;
- negative constraint rejects a hallucinated but registered tool call;
- conditional battery branch writes only below threshold.

### Intercepted live acceptance

Run all 85 prompts with `test_mode=true`, three repetitions for the critical
twenty. Because dry-run outcomes are `planned`, score the selected plan and
contract rather than expecting factual synthesis.

Minimum gate before packaging:

- 20/20 critical cases meet tool, argument, forbidden-tool, and dependency
  contracts in all three repetitions.
- Stock cases never call calendar and always preserve `period="2 weeks"`.
- Bare calendar always emits `days=60`.
- Mutating prompts never execute in the evaluator.
- No final response claims an effect absent from the ledger.

Then run the existing standalone regression scripts, followed by the 42-prompt
replay only inside an isolated Wisp home with confirmation denials and action
adapters stubbed. Repackage/relaunch the installed app only after source tests
pass, because the running service uses a copied bundle.

## Recommended delivery order

1. Land contract data structures and static assertions without changing live
   behavior.
2. Land tactical router fixes 1–8 and rerun the intercepted suite.
3. Add typed outcomes for outbound/calendar/device tools and the ledger gate.
4. Add outbound grounding and deterministic status narration.
5. Migrate remaining tools incrementally, then run the isolated replay and
   package the service.

This sequence gives immediate routing gains while making the structural safety
work reviewable in smaller patches. The contract and ledger are the durable
fix; the regex and subset changes are necessary compatibility repairs, not the
final reliability boundary.
