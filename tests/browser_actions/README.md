# A13 preparatory synthetic executor

Base: A05 PR #103, `a48f0fa51e6d9f1842cd1fcbac9421c813a49bd4`.
Owned paths: new `browser-extension/shared/action-executor.js` and this directory.
No existing adapter, bridge, extractor, service, or app file is changed.
A separate Orchestrator ACK authorizes only the new test classification in
`scripts/run_simulation_qa.py`; runner ownership is released after commit.

The module validates A01 actions against snapshot IDs, task IDs, target IDs,
roles, and editability. It runs eight fixed verbs against private in-memory
state with structured pre/post checks. It accepts only reserved `.invalid`
URLs. It has no DOM, network, timers, callbacks, arbitrary script, or live
adapter. `prepare` returns action data (including text); keep it private.
Execution results omit text and URLs and never constitute an ActionReceipt.

`approveForTest` creates a synthetic, instance-local, one-use handle bound to
the complete action/approval and current epoch. A caller-supplied wire approval
alone cannot execute a gated action. Click, fill, select, navigation, back, and
open-tab always require a handle; flags can only increase requirements. Every
successful action or context update invalidates prior handles. Every action
changes snapshot ID; navigation/back discard targets. Action IDs cannot repeat
within the instance. Failed prechecks have no effects; post-dispatch uncertainty
forbids retry. No result can complete an obligation.

Synthetic semantics: click increments a counter; fill/select store an exact
string without returning it; scroll advances 600 units; wait advances logical
time by 1000 ms; navigate/back use an in-memory history; open-tab appends to an
in-memory list. Select does not model actual option membership. Runtime context
and clock controls are explicitly test-only. A05's approved-manifest controls
remain noneditable and fail fill/select. There is no live target resolution.

## Validation

- `node --test tests/browser_actions/action-executor.test.cjs`
- `python -m pytest -q tests/browser_actions tests/browser_dom tests/test_browser_contracts.py tests/test_discovery_contracts.py`
- `python scripts/check_browser_contracts.py`

The Python wrapper uses the runner's validated Node runtime and removes host
Node preload variables. Shared runner classification requires a separately
acknowledged sequential owner; an unclassified test is a blocked gate, not a
waiver. Release review must use the final SHA and include the privacy Auditor
and synthetic Live QA. Do not run any real browser action.

## Remaining milestones

Full A13 still needs reviewed live target resolution, supported DOM control and
option handling, fresh trusted runtime permissions, A03 durable proposal/item
revision and atomic one-use app approval integration, structured observation
and evidence-based postconditions, uncertain-effect recovery, and adapter
integration. In-memory handles are not production authority and may never be
used as such. A14 owns durable budgets, cancellation, tab ownership, and restart
recovery. This foundation does not implement A10's real Chrome-to-Today flow.

PR #103 must land first. Then reconcile final main, freeze a new candidate, and
repeat mechanical checks, required CI, independent review and specialist QA.
No merge, installation, deployment, or live effect is authorized by this slice.
