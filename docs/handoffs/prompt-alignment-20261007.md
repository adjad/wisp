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
nonisolated benchmark guard and clock correction are included in this result. `git diff --check` and
proposal `git apply --check` passed. Previous advisory review does not approve
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
