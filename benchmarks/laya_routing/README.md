# Laya routing benchmark

This benchmark compares Wisp's unchanged router with three pinned Laya Core ML
exports using synthetic requests only. It measures classification and candidate
recall; it does not execute tools, use personal data, or claim that a classifier
can generate tool arguments or complete a Wisp task.

The shared request schema has three independent, deliberately compact questions:
whether a tool is needed, the request domain, and the primary operation. Compact
criteria let the same schema fit short requests in the ANE model instead of
giving the longer-context models richer descriptions. Laya's Core ML exports use
batch size one, so the three questions are three forward passes. The fixed ANE
export applies its 96-token capacity to each state/question pair.

The two 1,024-token exports can also be tested with `--schema rich`. That variant
uses detailed label definitions to measure whether the longer context improves
application accuracy. It is intentionally reported separately from the shared
compact-schema comparison.

Run each model in a fresh process so cold load and memory remain attributable:

```bash
python benchmarks/laya_routing/benchmark.py baseline --output baseline.json
python benchmarks/laya_routing/benchmark.py model \
  --name multilingual-ane --model-dir /path/to/model --output multilingual-ane.json
```

The corpus contains English and multilingual examples, effects, negations,
compound requests, no-tool conversation, and contextual follow-ups. Confidence
threshold results are selective classification measurements; they do not grant
authorization for effects.

See [REPORT.md](REPORT.md) for the measured results. Full machine-readable
outputs are under `results/`.
