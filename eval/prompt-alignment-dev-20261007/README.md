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
