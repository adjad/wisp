# Prompt alignment preparation — Wisp 1.3.0

This is an incomplete, unmerged preparation checkpoint on
`codex/prompt-alignment-1-3`. Original base:
`c313a459f6ab259e55981ffcb1bbe3df11fe9307`; repair base:
`bf4055bb980648368e2b3edba3a90d881654e71f`. Existing draft PR160 and the Local
checkout remain untouched. The explicit 1.3 no-merge/no-activation hold remains.
Current main reconciliation and exact candidate gates have not occurred.

The pure stdlib request module shares the published SYSTEM, clock/prior-tool
suffix, selected natural history, literal latest request, decoder options and
repair message with the planner and offline input builder. Future training can
use the identical prefix with final-answer annotations; tokenizer/end-token masks
still require independent auditing. Published V2, original validation corpora and
adapters are unchanged. Sixteen synthetic development cases produce three
prompt-text variants. HF generation remains unconstrained; API forwarding does
not prove schema enforcement. No model accuracy uplift is claimed.

Five bounded validator/grammar failures are repaired: PA003 temporal follow-up;
PA004 complete subject-free `weej` agenda; PA008 terminal unquoted sender/date
separation; PA009 count suffix source noun; PA010 complete source exclusion and
coordinated recap date scope. Exact tools/arguments and negative controls retain
source, date, literal, effect and exclusion checks. Original failures remain in
bf4055. Initial repairs left two failures (PA009, PA010); both were preserved in
command output and resolved within the assigned scope.

Actual router_core writer released only validation.py and grammar.py. Parent
accepted and the Orchestrator recorded the release before edits. Receipt:
`/Users/adijain/Documents/Codex/2026-10-05/new-chat/WISP_PROMPT_ALIGNMENT_VALIDATION_GRAMMAR_ACTUAL_RELEASE_2026-10-07.json`
SHA256 `adf6d247a47785d320d6a275fb95a89ec8ceaccb2c73a780f13db30f0cac9412`.
The other owned paths remain request.py, planner.py, the two request test files,
the eval prefix and this handoff. No general agent loop, native integration,
credentials, production settings, resident model or installed-app change.

The shared client remains unchanged at SHA256
`ee76f3cb2acb3bf52d6ca1d7af18280cb21532b9fd07345d075854e429301cca`.
Its actual retained Claude writer has not supplied the mandatory exact-file
release. Current fitting can grow 900 output tokens to 2,000, drop selected
history and omit response-format overhead. Three strict expected-failure tests
preserve these failures. The pure generated `STRICT_FITTING_PROPOSAL.patch` is
unapplied and passes applicability checking only. It proposes an explicit
preserve-request option with full-request rejection; behavior and independent
review remain required after release. Do not bypass this hold by changing target
metadata, using private HTTP or modifying the agent loop.

Validation after the five repairs: **1,206 passed, one benchmark skip and three
strict expected failures**, 8.09 seconds, in the relevant seven-file CPU suite:

```sh
/Users/adijain/Desktop/MOE_Project/.venv/bin/python -m pytest tests/test_router_intent_request.py tests/test_router_intent_request_transport.py tests/test_router_intent_core.py tests/test_router_intent_main.py tests/test_router_intent_workflow.py tests/test_fit_window.py tests/test_inference_endpoints.py -q --tb=short
```

Tests use disposable Wisp/credential state and mocked HTTP. Earlier targeted
repair runs: 540 passed/one skipped/two failed, then 542 passed/one skipped,
then 560 passed/one skipped. These are not real tool/model execution. The
nonisolated benchmark guard and clock correction are included in this result. The unstaged source `git diff --check` and proposal `git apply --check` passed.
Staging the previously untracked proposal then exposed one trailing-space blank
context marker. The shell continued to commit/push ef3408c despite that check
failure. A separate non-force follow-up removes only that marker and records the
failure; applicability and staged whitespace checks must pass for its checkpoint.
There was no product-source change in this follow-up. Previous advisory review does not approve
these later edits; no formal current-candidate review or required CI is claimed.

Offline latency scope was accepted before capture. The first CPU capture has
one warmup and 50 repetitions per path per case, but omitted clock control in the
legacy calendar helper. Its raw outputs stay immutable under cpu-latency-1 and
must not be represented as the corrected protocol. The repaired harness passes
explicit now and freezes only isolated reads.datetime outside timing. Parent accepted separately acknowledged corrective capture 2 before execution:
`WISP_PROMPT_ALIGNMENT_CPU_CAPTURE2_FIXED_CLOCK_ACK_2026-10-07.json`, SHA256
`85f1021aa08e974efe65a8ff0d0146a3f82dc901cf3b29b92e156815c6afac0a` under the same
Orchestrator receipt directory. Capture completed once: one passed/41 deselected,
0.75 seconds. Its 800 samples/path show selection p50/p95 0.216/0.349 ms and
supplied-intent CPU p50/p95 0.294/0.700 ms, with status counts unchanged. Raw and
summary provenance remains under ignored artifacts/cpu-latency-2; the report
records their exact hashes. This is not an end-to-end measured latency delta.
`LATENCY_REPORT.md` distinguishes supplied-gold CPU stages, archived Mac base
Ling inference and archived GPU V2 inference. No V2 merged/quantized Mac artifact
is installed or measured. The source/quantized base models do not establish that
artifact. No new inference or model state operation occurred.

Before final gates: obtain actual shared-client baton and parent ACCEPT, finish
strict fitting, remove its three markers after actual fixes, reconcile current
main only within acknowledged ownership, freeze/push one candidate and obtain
required exact-SHA independent review and CI. Existing PR160 remains untouched;
no duplicate PR or merge is authorized. Latency qualification also needs a
verified V2 export and a separately admitted serialized synthetic Mac window.


## Readiness continuation checkpoint — October 7, after actual-oMLX comparison

Direct human continuation (“ok work on all of these till they are ready to be released”) is recorded in `WISP_1_3_ALL_SIX_OUTCOMES_CONTINUATION_ACK_2026-10-07.json` and its owner plan under the Orchestrator. This parent continues the existing bounded alignment outcome only. Completed comparison outputs are closed; no new run, actor, training, source transfer, production change, or PR160 update is admitted by this document.

Inventory before this documentation-only continuation: clean/pushed alignment branch head `d3bec4decfd845f992e6f58165ec5eada4606e7d`, original base `c313a459f6ab259e55981ffcb1bbe3df11fe9307`, remote main freshly observed at `6ee9e8b76990fb22dbbc46b0be583fa9481da713`. No main reconciliation occurred. The fifteen cumulative changed paths are all in the existing request/planner/validator/grammar/test/development/handoff reservation. This checkpoint updates only this handoff, the development README and LATENCY_REPORT.md; no behavior change or new model measurements.

The shared-builder and five assigned validation fixes are complete in their owned paths. Remaining blocking implementation is exactly the retained `OMLXClient.chat/_fit_request` seam: output-floor growth, selected-history preservation and omitted response_format overhead. The client remains SHA256 `ee76f3cb2acb3bf52d6ca1d7af18280cb21532b9fd07345d075854e429301cca`; the unapplied proposal is SHA256 `6b1fbe994e9816b22b6ccdb8972d7bcde638e8462bd96ed9d67afb5330dca130`. Its exact previous-owner baton, unpublished-work disposition, parent acceptance and Root record remain required. No duplicate handoff delivery or takeover is authorized. Three strict xfails remain visible. The earlier 1,206-pass synthetic result is historical evidence for source bytes, not fresh gates for a new combined SHA; docs-only checks do not qualify the missing client fix.

The separate actual-oMLX diagnostic now verifies base/V2 oQ4e exports: exact routing 0/80 versus 13/80, schema-valid 12/80 versus 49/80; warm p50/p95 0.583/0.935 s versus 0.519/0.840 s. Its original harness bypassed Wisp fitting, used literal schema text and 512 routing output tokens. This branch uses shared suffix/history construction, response_format and 900 tokens. The diagnostic therefore does not qualify this serving seam or a deterministic-versus-Ling latency delta. Unequal quantization and answer regressions are preserved in LATENCY_REPORT.md, which links the closed full report. No model-quality or release-ready claim is made.

README.md now contains a concrete **conditional** actual-client development protocol: 16 existing synthetic cases/model, two exact exports, 32 decisions maximum and at most one repair each (64 model attempts), normal attributed client, 900 tokens, 2.5 s overall decision deadline, separate load/first/warm/planner latency, raw effective payload receipts, no real tools, strict stop/cleanup controls. It is proposed only, pending repaired transport, exact frozen combined SHA, independent review/CI and newly acknowledged resource window. Formal product quality/safety, packaged Live QA and final-artifact performance requirements remain separate; no scores or benchmark gates are weakened.

Next actions: actual retained client owner release -> narrow fitting repair + planner opt-in -> targeted synthetic tests without xfails -> acknowledged integration/current-main reconciliation -> freeze/push selected combined SHA -> required mechanical/CI/independent/QA/performance gates. Runtime availability must be freshly checked after admission, not inferred from the prior comparison reservation release. The explicit 1.3 no-merge/activation/deployment/publication hold remains.
