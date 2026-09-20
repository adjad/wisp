# Laya Core ML routing benchmark for Wisp

Date: 2026-09-20

Repository base: `411516dfb47a1ce637362f992ad1f5f8800c97a4` (`origin/main` at run start)

Fixture SHA-256: `5a54df79ab043f50fd5a838a9faf58f24c4dff2109a6f1f9bef40ac04ba03d4b`

Result: **none of the three candidates is safe as an authoritative Wisp router.**

## Recommendation

Do not put any candidate on the production routing path, including as a gate
that can suppress Wisp's existing fail-open-to-agent behavior. The two
multilingual checkpoints miss 23 of 28 safety-critical tool routes; the
typed-decisions checkpoint misses 27 of 28. This violates the benchmark's
fail-closed requirement regardless of latency.

If a measurement-only shadow is still useful, use
`aac6fef/laya-multilingual-coreml-ane` only for inputs whose complete encoded
question plus request fits its 96-token export. Treat every over-capacity
request, runtime warning, or low-confidence output as an abstention. Log the
shadow result for offline analysis only: it must not select tools, suppress the
agent path, change permissions, execute effects, or become a fallback for the
production router. Do not silently substitute the 1,024-token GPU model when
the ANE model abstains.

The ANE choice is operational, not an endorsement of its accuracy. It produced
the same discrete decisions as the multilingual GPU export while its five-
question warm latency was about 25 times lower and its compute plan preferred
the Neural Engine rather than the GPU. The GPU multilingual model is therefore
dominated for fitting shadow inputs, and the typed-decisions checkpoint is both
slower and substantially worse on tool recall.

Before reconsidering authority, fine-tune on a larger, held-out Wisp routing
corpus and require zero safety-critical misses, materially better negative-case
behavior, explicit over-capacity abstention, and independent validation on the
exact exported revision. The present probabilities do not support a threshold
fix: for the multilingual model, lowering the post-hoc tool threshold from 0.5
to 0.1 raises recall only from 36.7% to 71.4% while reducing specificity from
55.6% to 16.7% on this same corpus. This is descriptive, not a tuned result.

## Models and immutable inputs

| Candidate | Hub revision | Source revision | Verified bundle bytes | Export / requested placement |
| --- | --- | --- | ---: | --- |
| `aac6fef/laya-multilingual-coreml-ane` | `39d6a9b3d0f67f06da74fbade6121ea134cbdb21` | `052592a15d198d9ad47da779604259b10b47b7aa` | 679,898,808 | B1, L96, K32 / CPU+NE |
| `aac6fef/laya-multilingual-coreml` | `8139e9089273319512c730218903784074133187` | `052592a15d198d9ad47da779604259b10b47b7aa` | 679,869,459 | B1, enumerated L16–1024, K32 / CPU+GPU |
| `aac6fef/laya-typed-decisions-coreml` | `28d24fa8d67a3264556b23391ec6c3fd98573056` | `f9ab0b228f0fc0f14d873dbc99038f135c2da1b2` | 848,128,155 | B1, enumerated L16–1024, K32 / CPU+GPU |

Every file named by each bundle's `coreml_config.json` was hashed after
download and matched its pinned manifest. Package tree digests were, in table
order, `53ba84d9…3651`, `64e84856…b383`, and `517a8071…c85`.

Runtime: Python 3.11.15, `laya-coreml==0.1.0`, `coremltools==9.0`,
`huggingface-hub==1.32.0`, `numpy==2.1.3`, `tokenizers==0.23.2`, and
`psutil==7.0.0`. Host: Apple M5 Pro MacBook Pro, 18 CPU cores, 20 GPU cores,
24 GB unified memory, macOS 27.0. The environment artifact intentionally omits
serial numbers and other unique hardware identifiers.

## Evaluation contract

The new fixture contains 67 synthetic Wisp-style requests: 49 tool-positive
and 18 tool-negative; 12 are safety-sensitive false-route traps and 28 are
fail-closed safety-critical positive routes. It spans calendar, email,
messages, notes/memory, files, device control, media, web, weather/finance,
coding/computation, multi-domain requests, unsupported capabilities, and seven
languages. No personal mail, messages, calendar data, files, or contacts were
read.

Each Laya model answered five typed questions per request, serially at batch
size one:

1. whether a tool is required;
2. primary domain;
3. requested operation;
4. whether multiple distinct tool steps are required;
5. the safe fallback: answer, use tools, clarify, or decline.

Boolean decisions use the public `noul >= 0.5` rule and choices use argmax. A
safety-critical miss is a positive fail-closed case that says no tool is needed
or chooses a fallback other than tools/clarification. A false-route trap counts
as safe only when it avoids a tool route, a mutating operation, and a tools
fallback.

The Wisp baseline calls `service.router.router.route()` only. It does not call
the resident Ling model, enter the agent loop, or execute tools. Its domain and
operation labels are a deterministic projection of route metadata and offered
tool menus; that projection is lossy, so its domain/operation scores are not an
end-to-end Wisp task-success measure. Tool-needed recall and the fail-closed
comparison are the most direct baseline comparisons.

## Routing quality

| Candidate | Tool accuracy | Tool recall | No-tool specificity | Domain | Operation | Fallback | Multi-tool recall / specificity |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Wisp baseline | 76.1% | **100.0%** | 11.1% | 37.3% | 29.9% | **79.1%** | **83.3% / 39.3%** |
| Multilingual ANE | 41.8% | 36.7% | 55.6% | **50.7%** | **31.3%** | 14.9% | 16.7% / 90.2% |
| Multilingual GPU | 41.8% | 36.7% | 55.6% | **50.7%** | **31.3%** | 14.9% | 16.7% / 90.2% |
| Typed-decisions GPU | 29.9% | 6.1% | **94.4%** | 35.8% | 28.4% | 35.8% | 16.7% / **96.7%** |

The baseline deliberately over-routes ambiguous requests by making a bounded
tool menu available to the agent; this explains its low no-tool specificity and
false-route envelope score. It nevertheless retained all 28 fail-closed paths.
The candidates' improved specificity comes from declining or directly answering
many genuine tool requests, which is the wrong trade for Wisp.

| Candidate | Safe false-route envelopes | Safety-critical misses |
| --- | ---: | ---: |
| Wisp baseline | 1 / 12 | **0 / 28** |
| Multilingual ANE | 2 / 12 | 23 / 28 |
| Multilingual GPU | 2 / 12 | 23 / 28 |
| Typed-decisions GPU | **5 / 12** | 27 / 28 |

All 40 repeated outputs were byte-identical within each candidate. The ANE and
GPU multilingual exports produced identical discrete predictions on all 67
cases; their floating-point probabilities were close but not byte-identical.

## Capacity and over-capacity behavior

The capacity probe uses a minimal boolean question whose question/options and
special tokens consume 19 tokens.

- The ANE package accepted exactly 96 total tokens: 77 state tokens plus 19
  question/special tokens. Adding one state token constructed a 97-token input
  and raised `ValueError: Input has 97 tokens, but this export supports at most
  96`. This confirms the package's actual limit; the underlying multilingual
  encoder's longer context is irrelevant to this export.
- Both 1,024-token packages accepted a raw 1,025-token constructed input after
  truncating the prepared sequence to 1,024. Two overlong inputs with identical
  prefixes and different tail sentinels returned identical results. Therefore
  their effective semantic capacity is 1,024 tokens, but the current runtime
  silently drops the tail rather than rejecting over-capacity state.

For real routing questions the capacity left for the user request varies with
the instruction and option list. A shadow integration must test the complete
prepared length, not the state token count alone.

## Performance, memory, and placement

Cold load is first load in a fresh worker with snapshots already downloaded;
it is not a reboot-cold filesystem measurement. Warm p50/p95 uses 40 repeats of
one fixed short prompt. Candidate latency includes all five typed questions.

| Candidate | Cold load | Warm p50 / p95 | Quality-set p50 / p95 | Process CPU p50 / p95 | Peak RSS delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| Wisp baseline | 0.040 s | **10.12 / 10.39 ms** | 1.44 / 13.61 ms | n/a | **30.0 MiB** |
| Multilingual ANE | 14.54 s | 18.87 / 19.01 ms | 18.96 / 19.14 ms | **2.23 / 2.45 ms** | 931.7 MiB |
| Multilingual GPU | 1.95 s | 468.06 / 498.66 ms | 242.56 / 472.26 ms | 273.8 / 524.4 ms | 1,576.2 MiB |
| Typed-decisions GPU | 1.95 s | 640.51 / 662.38 ms | 333.23 / 647.59 ms | 343.8 / 666.1 ms | 1,150.3 MiB |

Core ML's compute plan assigned all operations for which it returned a device
preference to the requested accelerator:

- multilingual ANE: 6,390 preferred Neural Engine operations, approximately
  100% of reported estimated cost weight;
- multilingual GPU: 1,340 preferred GPU operations, approximately 100% of
  reported estimated cost weight;
- typed-decisions GPU: 1,676 preferred GPU operations, approximately 100% of
  reported estimated cost weight.

The two GPU candidates would compete with Wisp's GPU-resident generation path
in shadow mode. The ANE candidate avoids that GPU competition, but still uses
CPU-side tokenization, embedding lookup, and its host action head, adds roughly
0.93 GiB peak process RSS, and takes 14.5 seconds to load. The ANE worker also
recorded three NumPy runtime warnings at `laya_coreml/ane.py:163`—divide by
zero, overflow, and invalid value during the host action-head matrix multiply.
All public outputs remained finite and stable, but the warnings are a release
blocker for any authoritative use until explained.

Runs were intentionally serialized, so this benchmark did not inject concurrent
CPU/GPU/ANE stress. Placement plus process CPU time identifies the likely
contention boundary; it does not quantify latency under a live Ling generation.

## Reproduction and raw artifacts

From repository root:

```bash
uv venv /private/tmp/wisp-laya-bench-venv --python /opt/homebrew/bin/python3.11
uv pip install --python /private/tmp/wisp-laya-bench-venv/bin/python \
  -r requirements-runtime.txt -r benchmarks/laya_coreml_routing/requirements.txt
/private/tmp/wisp-laya-bench-venv/bin/python \
  benchmarks/laya_coreml_routing/run.py \
  --output test_results/laya_coreml_routing/20260920 \
  --models-root test_results/laya_coreml_routing/models
```

The runner refuses to execute unless the recorded base is intact: it accepts
the exact base or a descendant whose only changes are this benchmark and
report. It has a 2.2 GB pinned download scope, gives downloads and workers a
shared hard 45-minute wall-time budget, and starts the baseline and each model
in separate serial workers.

Raw outputs are under `test_results/laya_coreml_routing/20260920/`:

- `environment.json`: sanitized host/runtime metadata;
- `model_manifests.json`: pinned revisions, local paths, byte counts, declared
  shapes, and every post-download checksum;
- `baseline.json`: every Wisp route and projected decision;
- `multilingual_ane.json`, `multilingual_gpu.json`, and
  `typed_decisions_gpu.json`: every answer/probability, timing, RSS sample,
  capacity result, warning, and compute plan;
- `summary.json` and `summary.md`: aggregate machine-readable and generated
  summaries;
- `../models/`: exact local snapshots used for offline inference.

Generated results and model snapshots are intentionally ignored by Git. The
fixture, runner, pinned benchmark requirements, and this report are tracked.

## Limitations

- The 67 cases are new synthetic fixtures, not production traffic, and are too
  small to estimate rare-event safety rates.
- The candidates were evaluated zero-shot with concise typed questions. They
  were not fine-tuned for Wisp's taxonomy; this benchmark decides suitability
  of these exact published artifacts, not the ceiling of the architecture.
- The same fixture defines and evaluates the task. Threshold sweeps are
  diagnostic only and cannot support a tuned deployment decision.
- Baseline domain/operation labels are projected from tool-menu metadata and
  are less direct than Laya's explicit labels. No tool-selection model or tool
  execution was evaluated.
- RSS is process memory, not a complete device-level unified-memory accounting.
  Cold load excludes download and does not clear OS/Core ML caches.
- Compute plans provide expected placement, not live utilization. Serialization
  precluded an empirical contention stress test.
- No installed app was replaced, no real effect was executed, and no production
  routing, workflow compiler, build/release path, or CI fixture was changed.
