# Wisp routing prompt alignment development requests

This is an offline input builder with 16 explicitly synthetic development cases.
It uses the same `service/router/intent/request.py` module as the planner. It does
not run a model or tools and its fixtures are not a blind validation set or a V3
training dataset. Original 140-case/real-life corpora and V2 data stay unchanged.

Run from this checkout with a new output directory:

```sh
python3 eval/prompt-alignment-dev-20261007/build_requests.py --output eval/prompt-alignment-dev-20261007/artifacts/requests-1
```

The `serving` variant exactly preserves the shared serving message builder.
`core` removes clock/prior-tool suffixes; `schema_text` adds the canonical schema
to serving text. All three keep the same history, latest prompt, 900-token cap,
temperature zero, thinking disabled and declared API response format. Compare
base/V1/V2 on every unchanged case using a serialized resource window; report
results by arm and variant, not a best-of-per-case score. Future training adapters
can use `training_messages` for the identical prefix and final-answer annotation,
with independent token/end-token mask validation before any training.

HF greedy generation does not enforce the API response schema. API forwarding
alone does not establish oMLX schema support. Record actual tokenizer/template,
model/base/adapter hashes, backend, effective wire budget, context window,
response format support, deadline, raw output and finish reason. Score intent,
schema, exact arguments and compiler acceptance separately, alongside latency and
memory. Never treat input-bundle creation as a model accuracy result.

The shared inference client fix is pending its retained performance owner's exact
file handoff. Strict expected-failure transport regressions preserve the
900-to-2000 floor, small-context rejection and omitted schema overhead.
History/repair pressure checks require every selected message unchanged or
honest no-wire rejection; the current floor may cause that rejection before
a wire request exists. Passing those checks does not qualify a future floor-only fix. Do not qualify the candidate as
complete or merge-ready until they are repaired and pass without xfail. Ordinary
assistant fitting policy is outside this bounded preparation scope.

Five validator failures in these unchanged cases are now repaired: PA003
(tomorrow follow-up), PA004 (weej typo), PA008 (sender/date query binding), PA009
(repeated texts/messages aliases) and PA010 (leave-messages-out exclusion).
Positive tests assert the exact compiled tools/arguments. Negative controls cover
wrong dates, counts and senders, omitted exclusions, quoted literal data, public
subjects, real send instructions and independent second reads. These are supplied
synthetic intent checks, not model accuracy measurements. The previous checkpoint
`bf4055bb980648368e2b3edba3a90d881654e71f` preserves the original failures.

See `LATENCY_REPORT.md` for CPU measurements and separately attributed archived
model timings. `cpu_latency.py` is restricted to the repository's isolated pytest
state and a fresh admitted output directory; it executes no model or tool. The
first capture is preserved but superseded because a legacy calendar clock was
not controlled. The corrected harness freezes that clock outside timed calls.
`STRICT_FITTING_PROPOSAL.patch` is reviewable, unapplied work for the held shared
client; passing `git apply --check` does not qualify its behavior.


## Conditional actual-client qualification proposal — not admitted or run

The shared-builder preparation is finished in its released paths. Its remaining implementation dependency is the retained owner's exact `service/inference/omlx_client.py` handoff, parent acceptance and Orchestrator recording. Then apply/review the narrow preserved-request seam, opt the planner into it, and fix the three strict expected failures without weakening assertions. Preserve every selected history/message and the 900-token output cap or reject before the wire; account for response_format. A floor-only change is insufficient. Ordinary assistant fitting, provider/credential behavior, native integration and 1.2 performance changes remain outside this seam.

After that repair and authorized current-main/1.3 assembly reconciliation, freeze one combined candidate SHA, affected source hashes, tests, required CI and independent review. **No combined qualification SHA exists yet.** The proposed protocol below must be registered, accepted, and given a fresh exclusive resource window before runtime execution. It does not reopen any closed comparison outputs and creates no authority to load or change production models.

Proposed initial development protocol:

- Inputs: the existing 16 explicit synthetic PA cases, case SHA256 `0ddee36ddcce5bb7b0d7145acb4d5904dd7887d4f8a9eee16df2f329676ae3f4`, shared request.py SHA256 `ba0b3b4ff63b45c5d4d35f4f04e521896f0524af136c7de21e3f4c6526f6211e`, schema SHA256 `85186431c4d3e8f1c38ca7d2aa899714da1dd5b275de97998432a6de76705678`. Future fitting edits must update the frozen request/planner/client identity. Keep case clocks, gold routes, selected history and prior-tool metadata unchanged. Use the `serving` variant only; no per-case prompt selection, corpus tuning, training or model changes.
- Models: existing base `Ling-3.0-tiny-oQ4e` weights SHA256 `15dd1e8725e1678afc1ea136ceb42db07074c63f332b300a600e0ebb7635982a`, and `Ling-3.0-tiny-Wisp-V2-merged-oQ4e` weights SHA256 `5f23caa2122c40331e94762481a451593d277ab52c982b9cd432f4ec0c734648`. Tokenizer/template/runtime/quantization identities must be freshly verified and recorded, with unequal quantization disclosed.
- Backend: actual acknowledged oMLX service through the candidate's normal OMLXClient and attributed transport. Bind an isolated synthetic router Target without changing persisted/global role settings; no private HTTP generation bypass. The separate load/status supervisor may act only on its own admitted models. Exactly one model resident for the current arm; check ownership, no competing jobs, adequate memory and server configuration before loading. Installed oMLX app source/defaults stay untouched.
- Decoder/attempt budget: temperature 0, thinking disabled, response_format exactly completion_options(SCHEMA), requested 900 output tokens. Default overall planner deadline 2.5 s, at most one repair within the same deadline. 16 decisions/model, 32 total, one pass each, at most 64 total model attempts. Proposed load/startup cap 600 s, per-arm wall cap 600 s. Stop on configuration drift, timeout with uncertain active generation, identity mismatch, conflict or failed admission; no automatic retry/restart. Never count a short deadline as proof server generation was canceled.
- Record: selected builder messages, actual effective wire messages/budget/schema/thinking flags, request/model/runtime hashes, raw outputs and finish reason for each attempt, repair diagnosis, end disposition, compiled tool names/arguments (without executing them), validation rejection and late/uncertain completions. Gold answers and labels are not sent to the model.
- Score: raw schema/intent/source/operation/exact arguments separately from validated compiler acceptance, clarification, disabled-domain rejection and overall successful resolution. Count no-wire budget rejection separately from bad inference. Any emitted call for a disallowed/unrequested source, altered literal/date/count, or unauthorized effect is a blocking safety finding. A safe clarification is not a correct route or evidence of useful coverage. Existing product correctness/performance gates and any quality floor remain mandatory; register them before the run and do not weaken them after seeing results.
- Timing: report explicit loads and first requests separately; 15 warm first-attempt requests/model for request p50/p95 where completed. Also report 16 planner decision latencies/model including repairs and clarifications, counts of attempts/timeouts, and effective server prompt-cache use. Two small heterogeneous samples are descriptive, not release-performance evidence; memory estimate is not a peak without allocator/process measurement.
- Schema support: forwarding response_format proves only that it was sent. Record actual server behavior and any support/rejection evidence; do not claim constrained decoding from valid outputs alone. A separate bounded support-control protocol needs its own registration if required.
- Termination/cleanup: no real tool/user-data/native/outbound calls; unload only evaluator-owned models after known quiescence. Preserve failures and partial receipts, end at the admitted pass/budget, and release the resource window. No activation/merge/publication follows automatically.

This 32-decision development protocol tests the actual serving seam, not general model accuracy. Later product qualification requires a separately frozen, unchanged diagnostic/held-out corpus and isolated Wisp/source/presentation tests against the selected combined artifact. Existing 120-case results are exposed diagnostic evidence and cannot become a fresh blind test. Do not author a full V3 dataset or expand runtime scope under this proposal.
