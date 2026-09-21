# PR #55 routing/privacy follow-up

- Outcome: compile three synthetic user-reported request families into bounded, strict personal-data reads.
- Base SHA: `ffeb927d87cc5c91334f253017102e5c414c09a8`.
- Sole writer: this PR #55 implementation task in `/private/tmp/wisp-routing-correctness`.
- Owned paths: `service/router/router.py`, `tests/test_routing_semantic_correctness.py`, and this handoff.
- Validation: focused semantic routing, email scoping, routing contracts, PR #54 regressions, full relevant routing tests, and Simulation QA.
- Dependencies: preserve merged `origin/main`, PR #54 behavior, and all existing PR #55 stock/calendar/note-confirmation changes.
- Data policy: synthetic prompts and fixtures only; no real debug export or personal content is read.
