# Structured read intents

This opt-in router interprets flexible reads with the configured resident Ling,
validates its versioned object locally, and compiles registered read calls.
Durable actions, confirmations, source permissions, and exact complete read
shortcuts retain their existing boundaries. The schema never authorizes effects.

Configuration is the existing `models_config()` mapping's `intent_router` key:

```yaml
intent_router:
  enabled: false
  domains: [calendar, reminders, email, messages, notes]
  deadline_seconds: 2.5
  repair: true
```

Absent configuration disables the planner. An empty domain allowlist disables
it. Remove individual domains to return them to existing routing. Set
`WISP_INTENT_ROUTER_KILL=1` for an immediate global rollback. No shadow mode
makes live generation calls. Dry runs and active local skills disable planning.
Neither this configuration nor model output selects a model or endpoint.

`plan_read` borrows the existing attributed client only if it is managed/local,
uses oMLX, targets the configured Ling, and reports that model already loaded.
It never starts an engine, swaps/loads/unloads a model, or retries transport
failures. Its 2.5-second default overall deadline covers resource checks,
generation, validation, and one repair; cancellation propagates. The deadline
is configurable up to five seconds. `now` and the async eligibility seam are
injectable for synthetic tests.

The main agent intercepts compiled reads and limitations before session pinning
and executes reads through the existing registry and policy executor. Complete
digests are returned verbatim. Multiple receipts carry escaped source/scope
labels and keep partial failures. The wire `route_source` remains `model` for
journal compatibility; the additive `intent_disposition` debug enum records
`compiled`, `clarify`, or `declined` without changing journal redaction.

Unsupported constraints produce a limitation or clarification, not a broader
read. Calendar results are active upcoming items; elapsed ranges are refused.
Matching explicitly declared calendar/reminder scopes can use the existing
combined agenda tool. Reminder-only reads support today, tomorrow, overdue,
upcoming, or all. Email summaries cannot combine unread and dates or count with
dates, while records retain those supported filters. Date-scoped message
summaries accept a result limit only with a selected conversation. Exact message
queries require the registry's `strict_match` capability; without it the
compiler refuses the query rather than exposing fallback records. Tonight and
other partial-day filters are not representable. Weekend ranges are resolved
locally to inclusive Saturday–Sunday dates with the existing date resolver.

The JSON schema mirrors `schema.SCHEMA`; tests keep the two synchronized. No
native integration, live inference, user-data access, training, or release
activation is validated by synthetic tests.
