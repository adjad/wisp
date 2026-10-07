# Prompt alignment preparation — Wisp 1.3.0

This is an incomplete, unmerged preparation checkpoint. Base: `c313a459f6ab259e55981ffcb1bbe3df11fe9307`; branch: `codex/prompt-alignment-1-3`. Existing draft PR160 and the Local checkout remain untouched. The explicit 1.3 no-merge/no-activation hold remains.

The pure stdlib request module shares the published SYSTEM, local clock/prior-tool suffix, bounded natural history, literal latest request, decoder options and repair message with the planner and an offline development input builder. Future training examples can use the identical message prefix with final-answer annotations; actual tokenizer/end-token masks still require independent auditing. Published V2, original validation corpora and adapters are unchanged.

Sixteen synthetic development cases produce three prompt-text variants with the same declared 900-token output limit and API response schema. No inference or tool execution occurs. HF generation is explicitly unconstrained; forwarding an API response format does not prove server enforcement. No routing accuracy improvement is claimed.

The shared client remains unchanged because its actual retained performance writer has not released `service/inference/omlx_client.py`. Current fitting can grow 900 tokens to 2,000, drop selected history and omit response-format overhead. Known failures remain explicit in strict expected-failure transport tests. The branch is not complete or merge-ready. The mandatory exact-file baton and subsequent synthetic repair/review are integration dependencies.

Files owned: `service/router/intent/request.py`, `service/router/intent/planner.py`, two new request/transport test files, `eval/prompt-alignment-dev-20261007/` source fixtures and builder, and this handoff. No changes to the general agent loop, native integrations, credentials, production settings, models or installed Wisp.

Validation: the relevant seven-file CPU suite completed with **1,182 passed and eight strict expected failures** in 7.86 seconds. Command:

```sh
/Users/adijain/Desktop/MOE_Project/.venv/bin/python -m pytest tests/test_router_intent_request.py tests/test_router_intent_request_transport.py tests/test_router_intent_core.py tests/test_router_intent_main.py tests/test_router_intent_workflow.py tests/test_fit_window.py tests/test_inference_endpoints.py -q
```

`git diff --check` passed. Tests use disposable Wisp state and mocked HTTP transport. Five expected failures preserve exact frozen-validator diagnostics for unchanged synthetic cases: PA003 tomorrow follow-up; PA004 weej typo; PA008 sender/date query binding; PA009 repeated texts/messages aliases; PA010 leave-messages-out exclusion. Unrelated validator exceptions fail normally. Three expected failures preserve the 900-to-2000 budget change, small-context rejection and schema-overhead omission. Passing history/repair tests either retain every selected message or reject without wire I/O; roomy-context repair also exercises two actual mocked requests. They do not qualify the future strict fitting fix.

Earlier verification exposed four unmarked validator failures and two strict XPASS results: the latter were corrected to passing honest-rejection regressions, and the five exact semantic rejections were preserved with case-specific strict expected failures. A standalone compiler probe outside pytest isolation failed with readonly database initialization; no successful database write occurred. Compiler checks were subsequently run only under isolated pytest. An initial documentation command failed because bare `python` was unavailable; the successful command used `python3`.

The optional read-only helper reviewed the original frozen ten inputs and found the transport coverage gap. These subsequent repairs invalidate that frozen review for the final checkpoint. It was advisory, not formal candidate gate approval. No actual model inference, tokenization, native tools or cloud training ran.

Before final gates: fetch/reconcile current main, resolve only acknowledged owned paths, finish the held strict fitting seam, remove expected-failure markers after real fixes, freeze/push one candidate and obtain the required exact-SHA independent review and CI. Any unexpected overlap needs ownership resolution. This checkpoint does not update PR160 or create a duplicate PR of its existing changes.
