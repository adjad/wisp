# Laya Core ML routing benchmark

This benchmark compares Wisp's checked-in router with three immutable Laya
Core ML snapshots. It uses synthetic prompts only and never invokes a tool,
reads a personal store, calls the Wisp agent loop, or changes production
configuration.

The runner gives Wisp an isolated `WISP_HOME`, resolves every model by its
recorded Hub commit, downloads into the requested result directory, and loads
only local snapshots during inference. Model workers run serially in fresh
processes. Downloads and workers share a hard 45-minute wall-time limit.

```bash
uv venv /private/tmp/wisp-laya-bench-venv --python /opt/homebrew/bin/python3.11
uv pip install --python /private/tmp/wisp-laya-bench-venv/bin/python \
  -r requirements-runtime.txt -r benchmarks/laya_coreml_routing/requirements.txt
/private/tmp/wisp-laya-bench-venv/bin/python \
  benchmarks/laya_coreml_routing/run.py \
  --output test_results/laya_coreml_routing/run
```

The output directory contains the pinned model snapshots, per-case predictions,
timings, memory samples, capacity probes, Core ML compute-plan summaries, a
machine-readable aggregate, and a generated Markdown summary. The tracked
report records the exact artifact path from the measured run.
