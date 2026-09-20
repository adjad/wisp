# Wisp Laya Core ML routing benchmark

This isolated benchmark compares three pinned Laya Core ML exports with Wisp's
unchanged source router. It uses synthetic prompts only and never dispatches a
tool. `preflight.json` is the immutable pre-download record of revisions, export
shapes, hashes, expected bytes, and resource ceilings.

The run is bounded to four active hours and 3.5 GB of downloads. Model loading
and execution are serial. A missing or failing requested model is reported as a
failure; no substitute model is permitted.

The evaluation separates:

- raw classification: whether the predicted tool need, domain, operation, and
  risk agree with the fixture label;
- candidate retrieval utility: whether the predicted domain and operation can
  safely narrow the current Wisp tool inventory;
- full routing sufficiency: whether the output contains Wisp's complete
  `RouteDecision` contract and grounded tool arguments. Laya's typed answers do
  not generate arguments, order dependent calls, or populate that contract, so
  this is graded separately from raw accuracy.

## Reproduce

Use Python 3.11–3.13 on Apple silicon:

```sh
uv venv benchmarks/laya_coreml_2026-09-20/.venv --python /opt/homebrew/bin/python3.11
uv pip install --python benchmarks/laya_coreml_2026-09-20/.venv/bin/python -r requirements-runtime.txt
uv pip install --python benchmarks/laya_coreml_2026-09-20/.venv/bin/python -r benchmarks/laya_coreml_2026-09-20/requirements-benchmark.txt
benchmarks/laya_coreml_2026-09-20/.venv/bin/python benchmarks/laya_coreml_2026-09-20/benchmark.py validate
```

Download only the revisions in `preflight.json`, then run one model per process
with `HF_HUB_OFFLINE=1`, `HF_HOME` and `LAYA_COREML_CACHE` pointed at the ignored
`model_cache/` directory. The literal commands are preserved in `RUN_LOG.md`.

Committed outputs:

- `REPORT.md`: conclusions, protocol, comparison, recommendations, and limits;
- `summary.json`: machine-readable aggregate metrics;
- `raw/*.json`: probabilities, decisions, token counts, timings, memory, machine
  facts, capacity outcomes, and failures;
- `fixtures.json` and `questions.json`: frozen labels and question schema;
- `benchmark.py` and `test_benchmark.py`: runner, scorer, and protocol tests.
