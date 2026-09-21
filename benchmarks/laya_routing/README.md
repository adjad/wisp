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

## Ling oQ6e comparison baseline

`benchmark_ling.py` measures Wisp's current first Ling tool-selection step with
the exact production prompt and tool schemas while preventing tool execution.
It uses an isolated temporary Wisp data home and a fixed clock.

With `Ling-3.0-tiny-oQ6e` loaded in the local oMLX server, run:

```bash
python benchmarks/laya_routing/benchmark_ling.py \
  --model Ling-3.0-tiny-oQ6e --reps 2
```

The checked-in [`ling_comparison_manifest.json`](ling_comparison_manifest.json)
is frozen after the measured run. A future Laya comparison must read that file
rather than regenerate it, and must preserve its canonical state plus ordered
candidate tool names byte for byte. Model-specific wrappers may differ when
they are saved and reported separately.

See [LING_TOOL_SELECTION_REPORT.md](LING_TOOL_SELECTION_REPORT.md) for results
and methodology. Raw per-run timings and tool calls are in
`results/ling-tool-selection.json`.
