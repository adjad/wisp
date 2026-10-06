# Ling comparison harness handoff

Outcome: standalone synthetic model-comparison tooling in `training/ling-comparison-harness-20261006/**`. New namespace only; no app/service/version/settings, 1.2.0, training-data, production model or native-tool changes.

Accountable writer: 1.3.0 work (`01a112be-3e4e-7833-99e8-eda89e84c53b`). Corpus author `/root/comparison_cases` owns `cases.jsonl` and `CASES.md` and explicitly quiesced. `/root/comparison_review` is a read-only advisory helper, not formal release approval. Both GPT-6.1 Sol/high. Root scope/registration/repair records are in Wisp Orchestrator; parent repaired H1 runtime binding, H2 absent-journal reporting and H3 worker startup marker under its explicit ACK.

Branch: `codex/ling-comparison-harness-v1`. Integration base: `20e256d91bcb0bc34f5eb3a6c506bb9edc90f6fe`. Prep branch `codex/ling-comparison-harness` was preserved from `c313a459f6ab259e55981ffcb1bbe3df11fe9307`; its older production-router changes were not imported. Contracts retain explicit frozen c313 provenance. The final exact candidate SHA and remote head are recorded separately in Git and gate receipts, avoiding a self-referential committed SHA.

Files: `runner.py`, `scoring.py`, `backends.py`, `test_harness.py`, `bundle.py`, `cases.jsonl`, `CASES.md`, `README.md`, copied `router-system.txt`/`intent.schema.v1.json`, `.gitignore`, and this handoff.

Validation commands:

```bash
python3 training/ling-comparison-harness-20261006/runner.py validate
python3 -m unittest discover -s training/ling-comparison-harness-20261006 -p test_harness.py -v
python3 training/ling-comparison-harness-20261006/runner.py run --config <mock-config> --output <new-tmp-root> --arm mock-base
python3 training/ling-comparison-harness-20261006/runner.py run --config <mock-config> --output <same-root> --arm mock-tuned
python3 training/ling-comparison-harness-20261006/runner.py run --config <mock-config> --output <same-root> --arm mock-base --resume
python3 training/ling-comparison-harness-20261006/runner.py report --output <same-root> --left mock-base --right mock-tuned
```

The author verified 100 unique cases: 80 route +20 overview, one exact supplied phrase and 99 honestly synthetic prompts, all valid under schema/semantic and row checks. The initial18-unit/fullmock/resume/report checks passed; advisory review then found two blockers, both routed to the parent. The repaired source adds runtime/startup and output-root-wide orphan/remote-unknown negative controls (23 total); all passed locally. The second advisory pass closed H1/H2 and identified the remaining H3 cross-arm gap, repaired by the same recorded parent owner. A first schema-regex concern was a display-escaping misunderstanding, directly disproved and withdrawn without changing contracts/gold. Prior preparation's old receipts remain intact; new source versions use new output roots. No model inference, GPU allocation, live client/socket, tool execution, personal-data import or cloud spending was performed. Mock oracle exact scores verify the runner only, not Ling accuracy. Further final exact-SHA checks/re-review/CI status live in external gate evidence.

Remaining work: run both real model arms manually on the user's Brev VM, examine wins/regressions and raw failures, independently review 20 overview pairs, and then qualify merge/export/MLX/quantization before Mac comparison. HTTP model/revision/template identity is declared, not independently loaded-weight proof. Warm timings use different backend definitions and need comparable hardware/load/quant policy; no TTFT/prefill/cold-cache, repeated stability, production recovery or full-system RAM measurement is claimed. The visible small correlated corpus is not a blind holdout and cannot establish other-user population accuracy. Free-form unsupported strings and weekly reminder constraints have documented gold conventions. Manual overview ratings are not automatically aggregated.

Release/integration dependencies: independent formal exact-SHA ReleaseAuditor review for backend/provenance/privacy/resume integrity and configured same-head PR CI remain required before integration eligibility. Zero checks are unavailable/non-passing. This handoff does not assert those future gates passed. No merge or activation is authorized for this 1.3.0 preparation; the unreleased1.2.0 successor hold remains. Production routing/admission, final1.2.0 reconciliation, visual chat QA, serving export, and model-quality evidence are separate gates.
