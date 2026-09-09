# Wisp Typed Task Engine Plan

## Objective

Replace Wisp's free-form tool selection for common assistant tasks with a persistent, typed task engine. The language model may interpret what the user said, but application code decides which tools may run, in what order, with which grounded arguments, and what counts as success.

This is an incremental replacement of the current routing path. It must preserve working behavior, run in shadow mode before taking control, and fail closed whenever a request cannot be compiled safely.

## Why the current design keeps producing new bugs

Wisp currently has three overlapping decision systems:

1. Regex and semantic routing choose a tool subset for the current utterance.
2. The agent model selects and sequences tools from that subset.
3. A specialized workflow engine preserves state only for summary-delivery requests.

This leaves semantic roles and cross-turn state implicit for most tasks. In a request such as "remind me before my move-in date to ask Trishy for a Mac Mini," the same text contains an owner, a reference event, a person mentioned in the reminder body, and a missing lead time. Without a typed representation, any routing layer can mistake Trishy for an outbound recipient or treat the follow-up as a new task.

The permanent fix is to represent those distinctions explicitly and keep them authoritative until the task reaches a verified terminal state.

## Target architecture

### 1. Typed task representation

Add a versioned `TaskPlan` with these core fields:

- `intent`: the requested operation, such as `reminder.create`, `message.send`, or `calendar.query`.
- `owner`: whose data or event the task concerns. It defaults to the user only when that default is valid for the intent.
- `subject`: the content or object being acted on.
- `recipient`: the destination of an outbound action, separate from people merely mentioned in the subject.
- `channel`: Wisp UI, Messages, email, Reminders, Calendar, or another explicit destination.
- `temporal`: the original phrase, normalized value, timezone, and whether it was explicit, resolved, or defaulted.
- `references`: unresolved or resolved references such as "my move-in date."
- `sources`: data that must be read before composing an answer or outbound body.
- `slots`: all values plus provenance, confidence, and the turn that supplied each value.
- `steps`: a deterministic dependency graph of allowed tool calls.
- `approval`: the exact effect step and payload that require user approval.
- `completion`: machine-checkable postconditions.
- `status`, `revision`, and `idempotency_key`: persistent execution state and duplicate protection.

Example:

```json
{
  "intent": "reminder.create",
  "owner": {"value": "user", "source": "explicit"},
  "subject": {
    "value": "Ask Trishy for a 48 or 64GB Mac Mini",
    "source": "explicit"
  },
  "recipient": null,
  "channel": {"value": "reminders", "source": "intent_default"},
  "temporal": {
    "reference": "my move-in date",
    "lead_time": null,
    "timezone": "America/Los_Angeles"
  },
  "missing_slots": ["temporal.lead_time"],
  "status": "waiting_for_input"
}
```

Every slot records whether it came from explicit user text, a deterministic default, a tool result, or a later correction. Inferences that could change the target, recipient, channel, content, or date are never silently promoted to facts.

### 2. Bounded intent compiler

Build a compiler with four ordered layers:

1. Normalize spelling, shorthand, date phrases, and channel aliases.
2. Apply deterministic recognizers for supported high-value intents.
3. When needed, ask the local model to emit only a schema-constrained candidate task object. It does not receive tool schemas and cannot choose tools.
4. Validate the candidate against intent-specific invariants. Invalid or ambiguous candidates become one targeted clarification, with no effect tools exposed.

Embeddings or the reranker may shortlist an intent family for genuinely open-ended phrasing. They cannot authorize tools, fill sensitive slots, select a recipient, or choose an execution graph.

### 3. Intent specifications and deterministic planning

Each supported intent gets a declarative specification containing:

- Required, optional, and defaultable slots.
- Semantic-role constraints.
- Allowed source tools and one allowed effect tool.
- Argument builders that consume only typed slots or verified prior results.
- Step dependencies and maximum call counts.
- Approval requirements.
- Success, no-match, denied, and failure postconditions.
- A user-facing clarification for every missing required slot.

The planner compiles a valid task into an exact graph. The model may write prose from successful source results, but it cannot add steps or substitute tools.

### 4. Persistent turn handling

Store the active task object in the existing SQLite workflow tables with a `schema_version` and append-only transition events.

For each new turn, the engine must decide whether the text:

- Fills or corrects a slot on the active task.
- Approves or denies the exact pending effect.
- Cancels the task.
- Starts a clearly separate task and supersedes the old one.
- Is unrelated conversation and should leave the task untouched.

Short replies such as "Messages," "the day before," "mine," and "do it in Reminders" are interpreted against the typed missing slot rather than routed as isolated prompts. A denied action immediately enters a terminal `denied` state and cannot ask for or attempt approval again unless the user gives a new explicit send command.

### 5. Temporal resolver

Create one shared temporal resolver for reminders, alarms, calendar events, and scheduled sends. It will:

- Resolve relative dates against the user's timezone and current time.
- Resolve event-relative phrases only from verified calendar matches.
- Use standardized daypart defaults: morning, afternoon, evening, and night.
- Preserve whether a time was explicit or defaulted so the UI can state what it chose.
- Reject past, impossible, or multiply ambiguous times before any write.
- Produce canonical timestamps for tools, avoiding retries caused by passing raw phrases such as "8:00pm today."

The initial defaults will be centralized configuration rather than scattered prompt instructions, so they can be changed without retraining or editing routing rules.

### 6. Controlled executor

Add an executor that runs the compiled graph rather than handing a group of tools to the agent loop.

- A step can run only when its dependencies and required slots are satisfied.
- Arguments are constructed by code and checked against the registered tool schema before execution.
- Tool results are converted to typed outcomes using the existing `ToolOutcome` layer, then strengthened with per-tool receipts.
- Source results are stored separately from outbound content.
- The outbound body can use only successful, recorded source results.
- Each effect uses an idempotency key and a maximum call count of one per task revision.
- Approval binds to an exact rendered payload. Editing creates a new revision and requires approval of that revision.
- Denial terminates that revision without another prompt.

### 7. Result verification

Completion is based on evidence, not model narration. Each effect intent defines a receipt contract, for example:

- Reminder creation: returned reminder identifier plus canonical due timestamp and title.
- Message/email send: confirmed channel, resolved recipient, payload digest, and native bridge success.
- Calendar creation/update/cancel: event identifier and a read-back matching the requested fields.
- Read/search: a typed result set, including an explicit valid `no_match` outcome.

If a postcondition cannot be proven, Wisp reports failure or uncertainty and keeps an auditable failed task. It never reports success from a nonempty string alone.

## Implementation sequence

### Phase 0: Freeze evidence and add observability

1. Convert every supplied debug failure and the existing historical suites into a versioned golden corpus.
2. Record expected intent, semantic roles, slots, exact step graph, forbidden tools, clarification behavior, and terminal result.
3. Add structured trace events for compile, validate, clarify, plan, approve/deny, execute, and verify stages.
4. Add latency timing for each stage and include the task plan in debug exports with sensitive values redacted.

Exit gate: the existing installed Wisp behavior can be replayed and scored reproducibly without sending, deleting, or changing real data.

### Phase 1: Build the engine around reminders

Implement `reminder.create`, `reminder.update`, `reminder.complete`, and `reminder.delete` first because they exercise ownership, reference resolution, date handling, follow-ups, mutations, and verification.

This phase includes the shared temporal resolver and specifically covers:

- "Remind me at night..." using the configured night time without asking again.
- "Remind me before my move-in date to ask Trishy..." with `owner=user`, `recipient=null`, and Trishy retained inside the reminder subject.
- Follow-ups such as "the day before" and "mine, in Reminders."
- Multiple matching events or reminders producing one precise clarification.
- Denial and retry behavior with duplicate-effect protection.

Exit gate: 100% pass on all reminder golden cases and effect-safety invariants; no reminder mutation is reachable outside a valid reminder task plan.

### Phase 2: Messages and email

Implement `message.draft`, `message.send`, `email.draft`, `email.send`, `email.reply`, and scheduled delivery.

Key rules:

- Recipient, mentioned people, sender, account owner, and content subjects are distinct fields.
- "Send me my email summaries" compiles to an inline `email.summarize` read with Wisp UI as its output channel.
- Drafts render as editable task revisions; Send executes the exact visible revision.
- A denied send becomes terminal and never prompts again.
- No-match email searches remain `no_match`; they cannot be transformed into invented summaries.

Exit gate: 100% pass on historical recipient/channel/denial/no-match cases and all mocked effect tests.

### Phase 3: Calendar and source reads

Implement calendar query/create/update/cancel plus email, Messages, notes, contacts, weather, stock, and news reads.

Calendar result policies, including holiday-calendar exclusion, live in the calendar intent specification rather than model prompts. Reference resolution returns typed candidate IDs and requires clarification if more than one candidate remains.

Exit gate: exact argument and no-match behavior for all supported reads; all calendar mutations have read-back verification.

### Phase 4: Compound assistant tasks

Generalize the existing summary-delivery workflow into a composition plan:

```text
calendar.query ─┐
stock.query ────┼─> compose.grounded ─> contact.resolve ─> message.send
weather.query ──┘
```

The engine owns dependencies and ordering. Composition receives a bounded collection of typed source results. Failed or empty sources are represented honestly and cannot be filled in by the model.

Exit gate: exact graph, argument provenance, and grounded-content checks for all supplied compound prompts, including calendar summaries sent to Mom.

### Phase 5: Expand by tool family

Migrate the remaining tool families in risk order:

1. Read-only search and retrieval.
2. App/window/media control.
3. File and note writes.
4. System settings and automation.
5. Destructive file, reminder, calendar, or scheduled-action operations.

Until an intent family is migrated, it stays on the current router. High-impact tools may not be selected through the semantic fallback once their typed intent is available.

## Validation strategy

### Test layers

1. **Compiler tests:** prompt to exact typed task, including field provenance.
2. **Conversation tests:** multi-turn corrections, pronouns, short replies, cancellation, denial, and topic changes.
3. **Planner tests:** task to exact ordered graph, exact arguments, forbidden tools, and call limits.
4. **Executor tests:** mocked tool outcomes, approval binding, duplicate prevention, retries, and partial-source failures.
5. **Postcondition tests:** success cannot be claimed without the required receipt; no-match is not success with fabricated data.
6. **Historical regressions:** all supplied debug prompts and manually reported failures.
7. **Generated stress tests:** the existing 1,000-prompt suite plus paraphrases, misspellings, distractor entities, conflicting channels, and five-step compound tasks.
8. **Latency tests:** cold and warm compile, clarification, planning, first tool, and total task time.
9. **Installed-app smoke tests:** dry-run or mock bridge only for effects; real read sources may be exercised when safe.

### Strict grading

A test fails if any of the following occurs:

- Wrong intent or semantic role.
- A required tool is missing, an extra tool is offered/called, or call order is wrong.
- Any tool argument is invented, stale, attached to the wrong entity, or not traceable to a slot/tool result.
- Wisp asks for information that has already been supplied or has an approved deterministic default.
- Wisp silently defaults a sensitive field that requires clarification.
- An effect is attempted more than once, after denial, or without approval of the exact payload.
- A no-match result becomes a positive claim.
- A tool error, internal state, or raw fallback text reaches the user.
- Wisp claims completion without a verified postcondition.
- The test exceeds its latency budget.

Uncertain, ungraded, malformed, timed-out, and partially observed runs count as failures. Release is blocked by any critical safety or historical-regression failure. There are no partial passes for selecting four correct tools and one wrong tool.

### Initial performance gates

- 100% on user-reported historical regressions.
- 100% on mutation safety, recipient/channel correctness, denial, idempotency, and postcondition tests.
- 100% exact graph match for the reviewed core-intent corpus.
- At least 99.5% on generated read-only paraphrase tests; every remaining failure must be reviewed and added to the golden corpus before release.
- Warm deterministic compile and plan p95 under 50 ms, excluding source/tool execution.
- No extra model invocation for prompts handled by deterministic compilation.

## Shadow rollout and rollback

1. Run the task compiler in shadow mode beside the current router. It records a plan but cannot execute tools.
2. Compare intent, tools, arguments, and latency on real local requests.
3. Enable execution one intent at a time behind feature flags, starting with reminder creation.
4. Keep the legacy path available per intent for rollback during migration.
5. Disable legacy high-impact routing only after the typed path clears its gates.
6. Package and relaunch Wisp after each promoted phase, then run the installed-app smoke suite.

Shadow disagreements count as failures for review; they are not resolved automatically in favor of either system.

## Proposed code layout

```text
service/tasks/
  models.py          # TaskPlan, SlotValue, StepPlan, typed outcomes
  intents.py         # declarative intent specifications
  compiler.py        # utterance/conversation -> candidate TaskPlan
  validator.py       # invariants and missing-slot decisions
  temporal.py        # shared date/daypart/reference resolver
  planner.py         # validated TaskPlan -> deterministic step graph
  executor.py        # graph execution, approval binding, idempotency
  verifier.py        # intent-specific receipts and postconditions
  engine.py          # persistent turn/state coordinator
  tracing.py         # redacted debug/audit events and latency
```

The current `service/workflows` API remains as a compatibility wrapper during migration. Existing SQLite workflow rows remain readable through `schema_version` adapters. The router becomes the fallback for unsupported intents rather than being rewritten all at once.

## First build slice

The first implementation slice will deliver:

1. Versioned task models and SQLite compatibility.
2. Compiler, validator, planner, and state merging for `reminder.create`.
3. Central daypart defaults and event-relative time resolution.
4. A controlled reminder executor using the existing tools and approval policy.
5. Typed postcondition verification and idempotency.
6. Golden tests for all reminder failures already supplied by the user.
7. Shadow-mode comparison against the current route and installed-app dry-run smoke tests.

This slice is intentionally end-to-end. It proves that typed state, deterministic planning, safe execution, and strict grading work together before the same machinery is applied to messages, email, calendar mutations, and compound summaries.
