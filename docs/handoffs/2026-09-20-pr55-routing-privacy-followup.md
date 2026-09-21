# PR #55 routing/privacy follow-up

- Outcome: compile three synthetic user-reported request families into bounded, strict personal-data reads.
- Base SHA: `ffeb927d87cc5c91334f253017102e5c414c09a8`.
- Sole writer: this PR #55 implementation task in `/private/tmp/wisp-routing-correctness`.
- Owned paths: `service/router/router.py`, `tests/test_routing_semantic_correctness.py`, and this handoff.
- Validation: focused semantic routing, email scoping, routing contracts, PR #54 regressions, full relevant routing tests, and Simulation QA.
- Dependencies: preserve merged `origin/main`, PR #54 behavior, and all existing PR #55 stock/calendar/note-confirmation changes.
- Data policy: synthetic prompts and fixtures only; no real debug export or personal content is read.

## Independent-audit repair

- Blocked candidate: `980a44a5cb1eb29e00c4ae251bde1382941210cb`.
- Base: `9b6d50ef8db957ad726001dd15d3d9af7ae3d725`.
- Expanded owned paths: `service/tools/registry.py` and `service/main.py`, plus the existing router, tests, and this handoff.
- Repair scope: equivalent phrasing and policy clauses, source exclusions, private-read ordering, typed no-match outcomes, and history-free verified-result narration.
- Excluded scope: providers, packaging, real debug files, and personal data.
