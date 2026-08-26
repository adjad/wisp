# Router remediation changelog summary

## 2026-08-24 — Scoped reminder deletion

- Fixed “delete my reminders for today” treating `today` as title text and
  incorrectly calling the past-due-only bulk clearer.
- Fixed “clear all of my reminders” silently meaning only past-due items.
- Added confirmation-gated reminder scopes for today, tomorrow, past-due,
  upcoming, and all active reminders.
- Bulk reminder deletion excludes calendar events and previews the exact
  reminders before anything is removed.
- Added router and store regressions from debug export
  `wisp-debug-2026-08-24_18-22-45.json`.

Date: 2026-08-24

## Outcome

Implemented, packaged, and installed the adversarial-router remediation for
Wisp. The final 20-case intercepted acceptance run passed 20/20 declared
routing contracts, the complete `tests/test_*.py` suite passed, and the static
85-prompt audit reported zero flagged concrete routes.

All live acceptance prompts ran with Wisp `test_mode`; no assistant tool
executed and no email, message, file, reminder, calendar item, or system setting
was changed.

## Router changes

- Added machine-checkable required tool groups, forbidden tools, conditional
  actions, and resolved direct calls to `RouteDecision`.
- Added deterministic clause coverage for compound PDF/email, weather/reminder,
  Notes/plain-text-file, reminder/self-email, move-in-date, stock/message, and
  scheduled-send requests.
- Narrowed overloaded-word handling so code messages, email syntax, ordinary
  prose, Notes.app, SMS, calendar, and memory requests are not conflated.
- Enforced explicit negative language such as “without opening my inbox,”
  “do not send,” read-only requests, and “not the calendar event.”
- Added exact argument resolution for bare calendar windows (60 days),
  tomorrow scheduling (2 days), named reminder completion, Low Power Mode, and
  exact stock spans such as two weeks.
- Replaced generic calendar-first topic guessing with payload-specific sources.
  Generic project context now searches notes/conversations; stock, weather,
  files, and calendar-shaped payloads use their corresponding source tools.
- Guaranteed safe `move_path`/`organize_files` availability for file
  reorganization so semantic retrieval cannot leave only a shell fallback.
- Guaranteed file discovery and `read_file` for document-content requests.
- Added a deterministic, registry-backed `wisp_capabilities` response that does
  not claim unavailable vision or banking access.

## Agent-loop safety changes

- Added typed tool outcomes: succeeded, no match, needs input, denied, failed,
  and planned.
- Added a per-turn outcome ledger and prevented final prose while required
  clauses remain unmet.
- Enforced source-before-effect ordering for drafts, sends, schedules, writes,
  and other mutations.
- Evaluated battery and weather conditional actions from successful source
  results rather than model prose.
- Blocked outbound dates, prices, and other structured facts that are not
  grounded in the user prompt or successful current-turn reads.
- Bound confirmations to exact action arguments with fingerprints.
- Rejected duplicate effect calls after a successful or planned action.
- Prevented denied, failed, and dry-run actions from being narrated as
  completed; test mode now reports deterministic dry-run status.

## Evaluation and test changes

- Expanded the adversarial corpus to 85 single- and multi-turn prompts with
  required tools, alternatives, forbidden tools, clarification expectations,
  and required arguments.
- Added static contract, typed-outcome, conditional-action, grounding, dry-run,
  direct-dispatch, alias-reachability, and duplicate-effect regression tests.
- Updated the live evaluator to preserve route execution contracts through the
  SSE normalization layer.
- Live scoring now distinguishes observed calls from declared downstream
  obligations. This is necessary when synthetic test-mode source data prevents
  a later action body from being constructed.
- Added automatic redaction of email addresses and phone numbers in saved live
  results.

## Final verification

- Final intercepted acceptance: 20/20 contracts passed.
- Full local regression suite: all `tests/test_*.py` scripts passed.
- Static corpus audit: 85 prompts, 55 concrete rule routes, 30 provisional
  offline retrieval routes, zero concrete-route flags.
- Final result artifact: `router_adversarial_final_results.json`.
- Installed application: `/Applications/Wisp.app`.
- Backend health: healthy on `127.0.0.1:8765` using
  `Ling-3.0-tiny-oQ4e` at verification time.

## Main implementation files

- `service/router/router.py`
- `service/agent/loop.py`
- `service/tools/registry.py`
- `service/tools/misc_t1.py`
- `service/main.py`
- `scripts/eval_core.py`
- `scripts/eval_router_adversarial.py`
- `scripts/audit_router_prompts.py`
- `tests/router_adversarial_cases.py`
- `tests/test_router_execution_contract.py`
- `tests/test_execution_contract_loop.py`
- `tests/test_tool_outcomes.py`
