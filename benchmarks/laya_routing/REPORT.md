# Three-model Laya routing evaluation

Date: 2026-09-20

## Decision

Do not replace Ling's tool-selection and argument-generation role with any of
these three Laya exports. Do not add any of them as an authoritative
`needs_tools` gate. Their out-of-box request classification is far below the
reliability needed to remove tools, enable effects, or select a Wisp execution
contract.

The multilingual ANE export is the only one with attractive hot-path latency,
but its 96-token graph rejects ordinary short requests in some scripts and its
classification accuracy is not usable. It may be worth revisiting only after a
Wisp-specific fine-tune, a smaller hierarchical label schema, explicit
pre-tokenization, and a shadow-mode evaluation with deterministic fallback.

The general multilingual and typed-decisions exports should remain unused for
Wisp routing in their current form. They are slower than Wisp's current
candidate router on this machine, consume about 1.2–1.6 GB of additional
resident memory after load, and do not produce tool names, arguments,
authorization, or multi-step plans.

## Models and environment

The test downloaded and executed these exact Hub snapshots locally:

| Export | Hub revision | Upstream source revision | Shape |
|---|---|---|---|
| `aac6fef/laya-multilingual-coreml-ane` | `39d6a9b3d0f67f06da74fbade6121ea134cbdb21` | `052592a15d198d9ad47da779604259b10b47b7aa` | batch 1, fixed 96 tokens, 32 options |
| `aac6fef/laya-multilingual-coreml` | `8139e9089273319512c730218903784074133187` | `052592a15d198d9ad47da779604259b10b47b7aa` | batch 1, enumerated 16–1,024 tokens, 32 options |
| `aac6fef/laya-typed-decisions-coreml` | `28d24fa8d67a3264556b23391ec6c3fd98573056` | `f9ab0b228f0fc0f14d873dbc99038f135c2da1b2` | batch 1, enumerated 16–1,024 tokens, 32 options |

Hardware was an Apple M5 Pro MacBook Pro with 24 GB memory, running macOS 27.0
and Python 3.12.14. `laya-coreml==0.1.0` and Core ML Tools 9.0 were used.
Models ran sequentially in fresh processes. Loading and the first warmup were
excluded from warm latency.

The repository baseline was exact commit
`411516dfb47a1ce637362f992ad1f5f8800c97a4`. No production routing code was
changed and no tools were executed.

## Corpus and scoring

The synthetic corpus has 86 cases: 68 English and 18 in Spanish, French,
German, Chinese, Japanese, Hindi, Arabic, Korean, Portuguese, or Italian. It
includes conversation that needs no tool, reads, effects, negation, compounds,
and four contextual follow-ups.

Each Laya model answered three independent typed questions:

1. Does the request need external data or a device/application action?
2. Which of 12 request domains applies?
3. Which of 10 operations applies?

The shared compact schema fits short ANE inputs and was used for the primary
comparison. The two 1,024-token exports were also tested with richer label
descriptions. Because the exports have batch size one, one classified request is
three forward passes.

"Joint" means all three labels are correct. "Safe overtrigger" means a read,
search, calculation, or conversational request was classified as an effect.
"Effect downgrade" means a requested effect was classified as non-effecting or
as needing no tool. These labels measure classification risk; the benchmark
never executes a predicted effect.

## Primary results: shared compact schema

| System | Tool-needed | Domain | Operation | Joint | Safe overtrigger | Effect downgrade | Errors | Warm p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Multilingual ANE | 32.6% | 50.0% | 39.5% | 10.5% | 14.0% | 27.9% | 5/86 | 11.50 / 11.61 ms |
| Multilingual 1,024 | 34.9% | 53.5% | 43.0% | 10.5% | 15.1% | 30.2% | 0/86 | 480.13 / 675.95 ms |
| Typed-decisions 1,024 | 25.6% | 57.0% | 37.2% | 5.8% | 23.3% | 37.2% | 0/86 | 640.95 / 949.82 ms |

The multilingual models usually predicted `no_tool`: 62 of 81 accepted ANE
cases and 66 of 86 general-export cases. Typed-decisions predicted `no_tool` on
72 of 86. This makes each model unsafe as a gate that can hide Wisp tools before
Ling sees a request.

The multilingual source checkpoint did not turn its advertised language breadth
into useful Wisp routing accuracy. Joint multilingual accuracy was 6.7% for ANE
over accepted cases and 5.6% for the 1,024-token export. Typed-decisions, whose
upstream model is English-only and specialized for four unrelated workflows,
also achieved 5.6%.

## Rich-schema results

| System | Tool-needed | Domain | Operation | Joint | Warm p50 / p95 |
|---|---:|---:|---:|---:|---:|
| Multilingual 1,024, rich | 14.0% | 53.5% | 45.3% | 2.3% | 691.57 / 731.84 ms |
| Typed-decisions 1,024, rich | 25.6% | 57.0% | 50.0% | 9.3% | 959.26 / 1,020.32 ms |

Longer definitions did not improve domain accuracy. They improved operation
accuracy modestly while making latency worse and leaving joint accuracy below
10%.

## Confidence, stability, capacity, and memory

Confidence does not rescue the classifiers. At a minimum 0.7 probability across
all three compact decisions, ANE covered 32.6% of the corpus with 21.4% joint
accuracy; general multilingual covered 34.9% with 20.0% joint accuracy;
typed-decisions covered only 1.2%, and that one covered case was wrong. The
models' probabilities therefore cannot safely decide when to bypass fallback.

All three exports produced byte-for-byte identical public answers in 25/25
repeat comparisons across five representative cases. The problem is task fit,
not sampling instability.

| Export | Cold load | RSS increase after load | Peak RSS | Capacity behavior |
|---|---:|---:|---:|---|
| Multilingual ANE | 14.86 s | 0.95 GB | 1.38 GB | Rejected 160-word and 1,400-word probes; rejected 5 ordinary corpus cases at 97–99 tokens |
| Multilingual 1,024 | 1.93 s | 1.63 GB | 2.34 GB | Accepted 247 tokens; truncated the long probe to exactly 1,024 tokens |
| Typed-decisions 1,024 | 1.82 s | 1.20 GB | 2.11 GB | Accepted 249 tokens; truncated the long probe to exactly 1,024 tokens |

The five ANE corpus failures included two contextual follow-ups, two Hindi
requests, and an Arabic email request. The short multilingual failures matter
more than the artificial long probe: state plus label schema can cross 96 tokens
even when the user's visible sentence is brief.

Both long-context exports silently used only 1,024 tokens for the long probe.
Any future integration would need to pre-tokenize and reject or deliberately
summarize overflow so a trailing negation, recipient, or authorization cue is
not silently discarded.

The ANE runtime emitted NumPy overflow/divide warnings in its host action head
during each process start, although the library's finite-output check passed and
all accepted public answers were stable. This is another reason to treat the
runtime as an experimental component until understood.

## Current Wisp baseline

The unchanged Wisp router completed all 86 cases in-process at 0.50 ms p50 and
11.05 ms p95. It exposed at least one acceptable tool from every required group
on 69.7% of tool cases: 79.0% for English and 28.6% for multilingual cases. Its
effect-case tool-group recall was 69.2%.

That baseline is candidate availability, not end-to-end task success. It does
not measure whether Ling ultimately selects the right offered tool or produces
correct arguments. Conversely, Wisp's 10% no-tool specificity is deliberate:
ambiguous conversational requests receive a small tool menu with automatic
choice left to Ling. It must not be read as a 90% effect rate. The Laya
`tool_needed` label would change that policy by hiding tools, which is why its
false negatives are directly material.

The baseline also exposes a separate existing weakness: multilingual candidate
recall is poor. The tested Laya checkpoints do not solve it. A multilingual
retrieval improvement should be evaluated independently of request
classification and should preserve the current safe fallback.

## Integration implications

Laya returns labels and probabilities. Wisp's `RouteDecision` also needs exact
tool availability, direct calls, arguments, required tool groups, forbidden
tools, conditional actions, multi-step completion, clarification state, and
conversation context. Ling or deterministic compilers would still be required
after a correct Laya classification.

The practical choices are:

- **Ling replacement:** reject for all three models.
- **Authoritative request classifier or no-tool gate:** reject for all three.
- **Candidate-retrieval replacement:** reject out of the box; domain accuracy of
  50–57% is too low, and the long exports are slower than current routing.
- **Shadow-mode research:** only the ANE architecture is interesting because
  11.5 ms covers three decisions. Pursue it only with Wisp-specific training,
  hierarchical labels that fit comfortably below 96 tokens, explicit unknown
  and overflow handling, and evaluation on much more held-out multilingual and
  contextual data.
- **Typed-decisions:** do not pursue for Wisp. Its specialization is unrelated,
  its multilingual performance is weak, and it is the slowest configuration.

## Reproducibility and limitations

The corpus, runner, schemas, and full raw responses are stored beside this
report. The raw files include every per-case probability, latency, route
decision, capacity probe, memory measurement, and repeat comparison.

This is one synthetic 86-case corpus on one machine. It is intentionally broader
than the upstream conversion-fidelity fixtures but is not a production traffic
sample, a calibration study, or an end-to-end Ling generation benchmark.
Labels were manually authored and some requests admit alternative valid tools.
The conclusions are strong enough to reject immediate adoption because margins
are large, but a fine-tuned successor would require a fresh held-out evaluation.

The Codex app twice created Git worktrees for the delegated benchmark without
registering accessible tasks. The orphaned worktrees were left untouched. After
ownership was resolved by the Wisp Hub, the benchmark ran in the already
isolated `Analyze Laya model routing` worktree on branch
`codex/laya-three-model-benchmark-direct`. This app-level registration failure
did not affect model measurements.
