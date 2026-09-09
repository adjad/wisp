# Ling tool-calling reliability

The September 7 debug export was inspected as evidence, not executed as a
request. Its private messages, email contents and contact details were not
copied into fixtures. The implementation extends the existing workflow and
execution-contract layers; it does not change the model or its sampling setup.

## Changes

- **Conversation references:** a send request can bind the previous answer as
  its exact payload. Named reports and “send this” survive the channel question
  without becoming a search through unrelated mail or Notes. Referenced text
  remains payload data. Fresh daily summaries use the daily-brief workflow.
- **Resolved intent:** news topic follow-ups retain the news operation. The
  same resolved request drives generation and routing, and retries cannot
  silently broaden the bound search query.
- **Argument contracts:** temporal phrases are rejected as weather locations
  before network access. Numeric calendar windows are preserved. JSON argument
  types and enums are checked before tool dispatch, including array item types
  and boolean confirmation flags.
- **Execution dependencies:** bulk file organization discovers files first,
  then uses the set-oriented organization tool. A preview does not satisfy the
  move obligation. Other alternative groups retain all valid tool choices.
  Identical successful effect calls are not executed again in the same turn.
- **Verified responses:** outbound tools require their positive success
  receipts. Common delivery claims are checked even when no action ran. A
  scheduled send does not prove immediate delivery. Move results come from
  actual tool receipts, including counts, filenames and skipped items.
  Answer text is buffered until verification; progress heartbeats remain.
- **Data-derived comparisons:** stock histories calculate dated 1-, 7- and
  30-calendar-day comparisons from unsampled observations when a baseline is
  available. They name the actual trading date used instead of asking Ling to
  choose dates and calculate changes from a sparse sample.

## Verification

- 180 targeted pytest checks passed across routing, workflows, tool dispatch,
  task execution and historical regressions. Sixty are new invariant checks
  in `tests/test_tool_calling_invariants.py`.
- The execution-contract and outcome scripts passed another 10 checks.
- Local `Ling-3.0-tiny-oQ4e` passed 9/9 repeated synthetic checks: three each
  for referenced report delivery, news topic continuation, and complete bulk
  file organization. Median times were 1.50 s, 0.66 s and 2.09 s respectively.
  These are small synthetic checks, not production latency estimates.
- Live evidence is saved in
  `test_results/tool_calling_ling_debug_regressions.jsonl`.

The live harness retains production schemas and descriptions, but replaces
tool functions with in-process fakes. Its file model tracks which synthetic
files remain, and its checks require the whole set to be moved. It sends no
messages, changes no user files and performs no external data lookups.

```sh
.venv/bin/python -m pytest -q tests/test_tool_calling_invariants.py
.venv/bin/python tests/test_execution_contract_loop.py
.venv/bin/python tests/test_tool_outcomes.py
.venv/bin/python scripts/verify_tool_calling.py --repeat 3
```

## Scope and remaining limits

These changes address shared failure mechanisms, not every possible phrasing.
The compiler still has bounded language coverage, and the delivery-claim
checker recognizes common status declarations rather than proving arbitrary
prose. New factual summaries still depend on source quality and model
synthesis. A copied conversation answer preserves that answer; it does not
independently verify every fact in it. Weather still supplies the provider's
three-day forecast, not a full seven-day forecast.

## Permission-preserving development deployment

The installed app was ad-hoc signed and has no stable `Wisp Dev` signing
identity. Rebuilding it would force macOS to drop its existing privacy grants.
For development, the current Wisp.app remains unchanged while a per-user
`com.wisp.source-backend` launchd job serves this source checkout on port 8765
with Uvicorn auto-reload. The running app continues to provide its
permission-protected Calendar, Contacts, Mail and Messages syncs to that local
backend.

This deployment lasts until the user logs out, reboots, or relaunches Wisp.app;
the app's port guard then stops the development backend and starts its bundled
copy. A durable packaged deployment still requires a stable code-signing
identity before rebuilding Wisp.app.
