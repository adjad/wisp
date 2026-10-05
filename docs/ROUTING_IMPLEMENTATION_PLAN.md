# Wisp 1.3 routing implementation

Status: implementation in progress. Human approved the overarching plan and multiple GPT-6.1 Sol agents on October 5, 2026. Production base is `4994caa15533c0cf84c07208c9097e4197f2815b` (PR #159 merged). The independent overnight results measured the earlier `fa66cb3` base and are not current production scores.

## Delivery contract

1. Preserve exact read shortcuts and the merged normalization, negation, reminder-assent and durable action contracts. Route supported flexible personal reads through versioned Ling intent, strict local validation, and deterministic argument compilation. Keep source exclusions, authorization, exact date semantics and literal entities under code control.
2. Integrate through the actual request orchestration, including pre-router typed workflows/read shortcuts, session pinning, agent policy/receipts, and digest completion. Start disabled behind reversible configuration with per-domain eligibility; existing write workflows remain authoritative. No live shadow duplication or router-triggered model loading.
3. Represent every requested source, typed filters and contextual corrections. Unsupported constraints need an honest limitation; invalid or ambiguous interpretation must not produce guessed effects. Bound repair and latency, preserve cancellation, and avoid widening safety constraints during fallback.
4. Render compact, grounded calendar, email and message overviews using existing deterministic machinery. Preserve direction, exact dates/counts, partial coverage, and distinct results from repeated tools with different scopes.
5. Establish full production-path synthetic replay and fresh independently authored sealed evaluation. Reused corpora and known failures are regression assets, not fresh test or training data. Score source/menu accuracy, exact arguments, effects/exclusions, literal fidelity, answer grounding, coverage, recovery, latency and memory separately.
6. Freeze the code-only candidate before adapter selection. First prove a reloadable training checkpoint with a small isolated synthetic smoke run. Compare the same frozen integration with original and adapted Ling; checkpoint and stop unproductive runs. Laya remains an optional calibrated shortlist experiment and Jev remains unmeasured without an authorized credential. Training must not consume existing held-out cases or variants generated from their failure logs.
7. Require exact-SHA mechanical evidence and configured CI, independent release audit and scoped synthetic QA. Full-path measurements distinguish real local model inference from simulated tool execution. Feature activation and production deployment are separate from implementation.

## Ownership and dependencies

The Orchestrator registry is authoritative. This document is a mirror, not a claim.

- `router_core`: intent schema/compiler, router/read/main integration, agent-loop merging and focused intent tests.
- `router_eval`: independent harness, scorer, fresh corpus and evaluation tests. Builders must not inspect sealed test prompts before freeze.
- `router_presentation`: calendar/mail/message digest rendering and owned presentation tests.
- Parent: integration, this document and `eval/router-update-integration-20261005/**`; integrates worker commits after their ownership freeze. No concurrent edits to worker-owned files.

Each workstream uses a clean worktree from the recorded base. All three initial agents use GPT-6.1 Sol with high reasoning, as requested. Claude's architecture handoff at `678aab4` is reference material; its schema/tool mappings and fallback assumptions require verification before reuse. Its unified suite is reused regression data and has a documented stress-context preservation defect. Claude retains Wisp 1.2 performance evaluation ownership.

Dependency order: baseline/contracts → router and presentation → integrated synthetic verification → code-only freeze → actual local inference comparison → bounded training feasibility/selection → independent final gates. Evaluation authoring and presentation can proceed alongside router work; model/training measurements are serialized in a coordinated resource window.

## Acceptance targets

These are proposed release targets, not achieved measurements. Freeze detailed metrics and thresholds before candidate comparisons. Target at least 95% exact correctness on supported read scenarios in fresh tests, with per-family reporting; preserve the existing regression ratchet. Require zero observed unauthorized/excluded-source execution and no critical fabricated counts, receipt claims or hidden coverage gaps in the release suite. Record abstention/clarification separately so refusing everything cannot pass. Preserve exact fast-path latency, set the flexible-path budget against the refreshed full-path baseline, and report p50/p95, contention, repair/fallback rate and worker/device memory with measurement limits.

## Evidence and current state

`eval/router-update-integration-20261005/progress.json` records current workstreams, baseline checks and remaining work. Initial baseline: 2,252 tests plus 1,504 subtests passed on the recorded production base; no local-model inference or real tool execution. The original evaluation branch and raw results remain immutable. Native/user-data/outbound tools, production settings, installed app and live model residency are not changed by the test harness.

## Implemented behavior and controlled activation

The implementation uses the existing local `router` role and an already-resident Ling model. The planner interprets a versioned read object; local code validates sources, dates, literals, unsupported filters, and contextual corrections before compiling registered calls. Exact complete shortcuts continue without model inference. Durable action workflows remain the action path. Invalid interpretation gets at most one repair within a bounded total deadline, then an honest clarification.

Configuration and supported tool limits are documented in `service/router/intent/README.md`. No `intent_router` configuration is installed by this branch, so flexible planning remains off. The opt-in has a domain allowlist and a kill switch. This implementation does not change production settings or deploy the app.

Source rendering is deterministic. The agenda is grouped by day; emails fold redundant subject rows while retaining message counts; messages retain explicit sender/recipient direction. Multi-source receipts retain source/scope labels and partial failures. Compiled message queries use `strict_match=True` so a miss cannot broaden into unrelated cached messages.

`eval/router-update-integration-20261005/OVERVIEW_BEFORE_AFTER.md` contains synthetic presentation examples. `TRAINING_FEASIBILITY.md` records the separate read-only training review. The installed checkpoint and current training runtime require compatibility work before an adapter smoke run; no weight update or accuracy uplift has been measured. Actual model comparisons additionally require a coordinated resource window with the existing performance owner.

The production-path harness separates scripted integration checks from real inference. Its sealed corpus and original exact-call scores stay immutable. Supplemental guard metrics distinguish an added query-scope safeguard from the user-visible query/date/count arguments. Builders have not used held-out prompts or results to tune the implementation.

The local validator now independently requires requested time spans, reminder scopes, free-slot operation/duration and complete supported query phrases; a model response cannot establish that its own missing field was optional. Independently dated sources and same-source date corrections have synthetic positive controls. Shared exact shortcuts preserve established calendar-only/day semantics. Evaluation date helpers use a reversible Pacific timezone contract in dedicated serial processes; legacy compatibility metrics remain distinct from typed supplemental fidelity scores. The first failed audit/CI candidate and all repair-stage raw results are preserved rather than replaced.
