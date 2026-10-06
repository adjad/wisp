# Wisp PR #160 independent release audit — BLOCK

Candidate: `44babb70ee2eea99897b9437b2f6d49b2d59adff`. Base: `4994caa15533c0cf84c07208c9097e4197f2815b`. Branch: `codex/router-update-1-3`.
[PR #160](https://github.com/adjad/wisp/pull/160). Recorded 2026-10-06T01:46:39.873857+00:00.

## Finding R10 — P2: quoted reminder data is treated as negation

`Complete reminder called "Archive: don’t check this item"` is a supported positive completion request for an exact reminder title. The new Unicode `_NEGATED` regex at `service/tasks/compiler.py:20` is searched against raw request text at line409, including the quoted title. `compile_task` returns None. Actual `main.agent` runs zero fake tools, creates no task or workflow, and answers:

> I couldn't complete every requested step. Still missing one of: complete_reminder.

All four supported Unicode apostrophes reproduce this with `check` and `delete` inside the quoted title (8/8 failures). A same-head, in-memory counterfactual replacing only `_NEGATED` with the previous ASCII regex restores exact target parsing and one expected fake completion call for each form. Four current ordinary Unicode-name positive controls also pass. The ASCII quoted-negation limitation is pre-existing; these Unicode failures are newly introduced. The known unsupported `Check off reminder called` observation remains disclosed and was not used as a positive fixture.

Apply negation checks to quote-masked instruction text and preserve original literal target bytes. Add positive quoted-target controls while retaining real negative-instruction and source-exclusion coverage. No candidate files were edited.

Evidence: [R10-proof.json](R10-proof.json), [boundary-results.json](boundary-results.json), [supplemental-results.json](supplemental-results.json).

## Current repair and retained contracts

R9 source exclusions now pass all395 independent core/application controls: ASCII plus four Unicode apostrophes, five domains, read/include/check, correct completion and malicious omitted/extra replies, coordinated exclusions and independently requested later reads, literal query/name/target bytes and supported positive task controls. Pure reads retain exact fake calls/answers and neither a delivery workflow nor an active task.

Fresh logical controls: **622/630 pass**. The eight failures are one R10 family. Application/core484/484 includes373 actual-main runs; public40/40; runtime/privacy/overview66/66; supplemental32/32. Eleven initial auditor fixture errors were corrected in a bounded rerun: five guessed completion argument shapes and six overbroad no-workflow expectations for an explicit later send. Initial scripts/results/logs, exact explanations and11/11 corrected evidence are preserved. No candidate assertion, gold or production code changed.

R7 negative deliveries12/12 and R8 later-clause controls60/60 pass. R1 tuple mutations, R2 inherited query replacement and R4 governed titles pass. Supplemental actual-main controls cover calendar-only/aliases, mixed source completeness, dates/unread/count/accounts/query/duration, unsupported filters, context exclusions, Unicode literal rejection and honest source errors. Public checks retain frozen queries, literal-address/contact exclusion, inherited/leading payloads, cancellation and revoked delivery. G1 passes at all three clocks, and its injected text leak fails the oracle. Identity checks occur before fake I/O; deadline/outer cancellation tests cover only the structured planner. Overview/strict-message coverage, manifest drift179, DST23/25-hour spans, default-disabled config/kill switch and typed-metric caveat pass.

The full intended production/schema/harness/overview review from earlier audits is retained only for unchanged source, verified against the full363-path diff and current72-path delta. The four current source/test files and relevant parser/executor contexts were reviewed. Earlier pass claims do not transfer. Current provenance774/774 checks verify all changed file bytes, worker/assignment/HANDOFF/progress pins, opaque corpus/gold continuity,21 archive manifests with450 entries and12 replay bundles. Archived payloads stayed opaque; no archived script ran.

## Mechanical gates and release boundaries

Parent local full gate: **179/179 PASS**, exit0, head unchanged/clean; completed2026-10-06T01:37:36.695077Z in579.96s. Receipt and opaque raw-log SHA256 verified: `5b09a3939d61d26350e86c71c87bad63309ab3122ab762051e54f3c8560b9305`. The auditor did not duplicate it.

The latest saved parent-fetched CI metadata at this head remains `in_progress` for `python-regressions` run37399273554/job112062543794 and `Verified macOS artifact` run37399273475/job112062543801. No terminal CI success or old-head result is credited. Passing mechanical gates would not override R10.

All model/status/tool interfaces and state were synthetic. Positive task fakes do not mutate reminder readback, so exact calls and truthful unverified-effect response/stored failed task are expected; success is not invented. These checks do not establish resident Ling accuracy, real source/native behavior, resource residency, installed-app behavior or release performance. No real models/native/user data/outbound/network/endpoints, provider-cancellation diagnosis, full gate duplication, corpus/author/heldout decoding or source edits occurred.

Retained6df provider-cancellation cause/owner ACK/relay remains unresolved. Resource/training/activation/Live QA/performance/shipping and installed replacement are separate holds. Planner stays default-disabled. **Simulation QA remains HELD** because this audit is BLOCK; any repair needs a new freeze and fresh exact-head gates.
