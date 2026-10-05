# Wisp PR #160 — fourth independent audit

**Verdict: BLOCK** at `273d2748d01ed978b4e4fb56c8ee503b18143bc4`, base `4994caa15533c0cf84c07208c9097e4197f2815b`, branch `codex/router-update-1-3`. Recorded 2026-10-05T21:59:29.140134+00:00.

The original mixed-source lookup defect and two-sender email reproducer are repaired. The privacy-test timestamp collision repair preserves its content/model/debug assertions and passes the three fixed clocks. The parent full gate passes179/179. Fresh independent positives still reveal three blocking execution gaps.

## Blocking findings

### R4 — P2 — unquoted query preposition becomes a delivery recipient

Request: **Read email from Lyra and email about road to recovery**.

The correct intent and standalone planner compile strict email reads for `Lyra` and `road to recovery`. The new helper at `service/router/web_request.py:553–557` rejects a coordinated read noun whenever its whole unquoted complement contains `to`. `_delivery` then treats `recovery` as a recipient and grants `send_email` metadata. `service/router/router.py:6967–6974` suppresses the structured planner, and actual `main.agent` falls back to `rules / email lookup -> scoped tools (6)`.

Quoting `"road to recovery"` makes the same two reads execute correctly. Original shared-date sender requests, quoted sender variants and genuine addressee/later-action controls pass. Repair the boundary between a bounded search literal and a destination relation; retain genuine `email from Orion to Mom` and later `send` actions.

No send occurred. The observed fallback exposed six legacy email tools, **not** `send_email`; no executable outbound expansion is claimed. The defect is incorrect action metadata and bypass of structured read validation. Evidence: `followup-results.json`, `unquoted-preposition-data` and paired `quoted-preposition-data`.

### R5 — P2 — email recap is intercepted by a delivery workflow

**Recap email** is a supported overview: the validator accepts it, standalone planning compiles `summarize_emails`, and the web classifier grants no effects. However, the workflow noun mask at `service/workflows/compiler.py:43–48` omits `recap`; `outbound_verb` returns true. The typed preflight creates a `deliver_summary / waiting_for_content` workflow with no recipient/channel, emits “I couldn't tell exactly what to send”, and returns before routing (`service/main.py:1492–1510`). No intent generation or source read runs.

**Recap email; find notes named harbor sketches** loses both supported reads. Paired **Recap my email**, **Recap my email; find notes …**, and **Summarize email; find notes …** execute correctly. Recognize the read head consistently in workflow admission without hiding real delivery clauses. No actual outbound effect is alleged. This is a full execution-scope defect, although the mask is unchanged in the newest repair delta.

Evidence: `application-results.json`, `R3-overview-plus-lookup`; `followup-results.json`, `bare-email-recap` and its paired controls.

### R6 — P2 — unquoted search data remains required source authority

**Find notes about my calendar**, supplied with the complete notes query `my calendar`, fails “Missing requested source”. `source_requirements(prompt)` at `validation.py:593` treats the query word `calendar` as a second required domain; lines683–685 reject the correct notes-only intent before the new instruction-text check. Adding calendar satisfies that earlier condition but the new check at691–693 correctly denies the literal-derived extra read. No valid representation remains for this supported query.

**Find notes about our messages** has the same failure. Quoting either literal executes a single exact notes lookup. Derive required and extra-source authority from the same bounded instruction/literal distinction. Keep literal-derived extra-read rejection and genuine mixed-source authority checks.

This is deterministic over-abstention, with no extra source read or privacy expansion observed. Evidence: `application-results.json`, `literal-source-unquoted-positive`, `literal-source-quoted-positive`, `literal-source-extra-rejected`; `followup-results.json`, `unquoted-source-data` and `quoted-source-data`.

## Repair assessment and independent evidence

- R3 original and fresh scope: calendar/notes in either order and repeated notes with only one filtered occurrence execute. Missing, truncated, invented and misbound filters/date swaps reject.
- R4 original reproducer: natural and quoted two-sender shared-date reads execute; explicit second read retains its own date. Genuine recipient, draft, message and later delivery clauses keep the planner guard; quoted/negated action controls grant no effects. The new unquoted-preposition variant above remains blocking.
- G1: actual repaired target test passes clean,1234-collision and5678-collision clocks. It preserves exact timestamps/source positions/two occurrences, cache selection and four output summaries with original model/debug/text privacy checks. A synthetic injected private body fails the new textual assertion. Production redaction code is unchanged.
- R1/R2 and aliases: whole tuple swaps/omissions reject; valid order/duplicate handling works. Current source-free query replaces stale text while retaining valid inherited constraints; ambiguous context clarifies. Natural calendar/inbox/messages aliases work.
- Original omissions/shortcut: date, reminder scope, duration, truncated query and unsupported daypart reject. A valid90-minute slot runs. Actual tomorrow shortcut remains calendar-only; its unused empty synthetic intent is not separately validator-valid.
- Fresh identity/runtime: config/defaultoff/kill switch/wrong origin/provider/role/transport and nonresident guards; one overall deadline, outer cancellation propagation and bounded clean-context repair pass.
- Manifest/scorer/timezone: six modules admitted, unknown/missing fail closed; typed supplemental numeric mismatch rejection retains disclosed legacy equality; UTC/Pacific23h/25h DST and restoration/fail-closed controls pass.
- Fresh overview helpers preserve source counts, duplicate attribution, actor direction, partial coverage and independent merged results.

Actual-main plus standalone-planner batch: **51/53** expectations met. Followup paired controls: **5/8**; these repeat affected families to establish causes, not independent accuracy samples. Boundary/privacy/overview helpers: **33/33**. The five failed positive cases correspond to the three findings. Raw inputs, supplied fake replies, SSE routes, schemas and registered fake calls are preserved. No genuine source bodies or real model ran.

## Mechanical gate and provenance

The parent once-only full regression passes **179/179 modules**, exit0, on this exact SHA. Receipt `parent-gate-receipt.json`; complete external log `/private/tmp/wisp-router-update-273d274-regression.log`, independently verified SHA256 `b7c4b99573d6285388493808469f29214ec5f460f089cf81405df9a40f0a4c4c`. Completed at 2026-10-05T21:55:42.482152+00:00. The auditor did not duplicate this gate or rerun DEV. Required CI was still running in the parent receipt; no independent current passing receipt inspected.

Complete intended **141-path** base-to-head scope assessed through prior completed full reviews plus the full **49-path** delta/context. New production changes are `validation.py` and `web_request.py`; the privacy repair changes one test file. Final clean HEAD and every changed-file hash verified. `provenance.json` retains exact registered assignment/handoff pins, full paths/hashes, eight replay bundles and66 preserved archive entry checks. Opaque corpus/author bytes are unchanged.

Two auditor setup errors are explicitly recorded in `auditor-fixture-repairs.json`: direct invocation of a decorated pytest fixture, corrected to its underlying setup; and a verifier expecting flat checksum keys, extended to the declared nested checksum schema. Both repaired scripts completed successfully. Only auditor-owned files changed; candidate and archived evidence stayed immutable.

## Limits and handoff

Heldout/corpus author/raw replay/archive payloads stayed opaque; hashes/metadata only, no archived script execution. No real models/endpoints/native/user data/settings/credentials/outbound/network effects, training/residency/activation, installed-app changes, deployment or merge. Scripted DEV80/80source/exact and72/80E2E are stage evidence with unchanged annotation limits, not model quality; zero-applicable measures do not pass a property. The default-off control is separately preserved. Planner default-disabled/resource limits persist.

The local pass does not override these findings. Distinct Simulation QA remains held. Parent reads the formal files; route R4/R5/R6 to the Orchestrator for one recorded repair owner each before edits. A read-only diagnosis reservation is not edit authority. Repairs require a new frozen candidate, ACK and independent re-review. No release, activation or merge approval follows from this BLOCK.
