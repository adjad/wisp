# Independent Wisp routing evaluation — October 5, 2026

User-requested independent overnight evaluation, report due 9 AM Pacific. Production code stays at `fa66cb3e3b0b0c207fe0dce7cc42de4c912f93b0`. This work owns only this directory. Claude owns the separate routing implementation.

## Frozen comparison

168 synthetic prompts in 50 families: 18 development, 138 held-out strict cases, and 12 descriptive ambiguity/capability challenges. All prompts/gold were authored and independently reviewed before held-out inference. No private debug-log contents are sent to models. The clock is October 5, 2026, 00:55 Pacific; explicit week spans are not accepted as rolling seven-day spans.

Five candidates share a controlled Ling argument-generation prompt and real Wisp tool schemas:

- `current_rules`: frozen structured-read shortcut, then current router, respecting forced tool, resolved instruction and argument bindings.
- `ling_direct`: one Ling call with a compact 25-tool catalog spanning the tested capabilities; this is not the entire Wisp registry.
- `ling_plan`: Ling source classifier, then scoped Ling tool generation.
- `ling_intent`: Ling structured intent with operation/date/filter fields; validated deterministic read-plan compilation, otherwise scoped Ling tool generation.
- `laya_hybrid`: local multilingual Core ML Laya source selection; scoped Ling generation when max probability is at least 0.8 and input/options are intact, otherwise full compact-catalog Ling fallback. This threshold was fixed before held-out testing; it is not calibrated confidence.

The first-call benchmark measures actual local model inference but never executes a proposed native, personal-data, or outbound tool. It is not the complete production agent loop. Strict success requires correct tool multiplicity, schema/runtime arguments, date boundaries, exclusions, and no extra effects. Source/tool correctness and exact arguments must also be reported separately. Some alternative tools could still produce an acceptable final answer; strict workflow accuracy alone does not prove end-to-end success.

`answer_eval.py` continues selected frozen first-call decisions with synthetic complete/partial/failed tool responses. It checks the answer stage and bounded repeated-read handling under a common concise style. It does not reproduce every production summary renderer, installed UI, native provider or permission boundary. Human/independent review must assess the resulting answers before making readability claims.

`laya_stability.py` tests raw source decisions with original, reversed, and seeded-shuffled option order. Report calibration/coverage, option-order disagreement and truncated/collapsed inputs separately.

## Running and evidence

Use `/Users/adijain/Desktop/MOE_Project/.venv/bin/python`. Laya runs in `/private/tmp/wisp-routing-eval-laya-venv` with locked dependencies. Its existing model is `aac6fef/laya-multilingual-coreml@8139e9089273319512c730218903784074133187`; no model download is required. Jev is unmeasured unless explicitly configured authorized credentials become available.

`test_harness.py` contains targeted checks of duplicate actions, timezone equivalence, shortcut precedence, exact ranges, unsupported runtime periods, and read-only intent compilation. `overnight.py` serializes the held-out runs, challenge cases, answer continuations and Laya stability checks. Before restarting, inspect `overnight_status.json` and the PID. OS locks prevent duplicate orchestration/measurement. Every main run refuses to mix changed corpus/source/model/prompts with an existing output manifest.

Raw outputs, requests and manifests are retained locally beside the scripts and ignored by Git. Credentials are never included. Original `development.jsonl` is superseded because its baseline/grader contained defects; preserve it as audit evidence, never include its scores in the recommendation. `development_v2.jsonl` was used only for development. Held-out cases receive three repetitions, but repetitions/paraphrases are correlated: report unique-case and family-level metrics rather than treating every call as an independent sample.

Latency includes routing and model generation, excludes real tool latency. Cold model reloads are prohibited: report initial Core ML load, uncached-prefix requests and cache-hit requests using accurate labels. Desktop contention and server caches remain potential confounders; do not assert exclusive hardware or a diagnosed cache defect from prefix-sensitivity controls alone. All model requests are serialized. Check memory before each case and stop below 12% free. All inference is bounded by October 5, 08:30 Pacific; no production model is unloaded/reloaded.

## Delivery

Read `HANDOFF.json` and `overnight_status.json` for continuation. The scheduled 9 AM report must distinguish measured evidence, simulated execution, unsupported capabilities, and any remaining limits. Recommend the best measured architecture, not a universal optimum. Keep deterministic authorization, exclusions, argument validation, source provenance and action receipts regardless of which model interprets intent.
