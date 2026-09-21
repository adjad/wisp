# Ling oQ6e tool-selection latency

Date: 2026-09-20

## Result

With `Ling-3.0-tiny-oQ6e` already loaded in oMLX, Ling completed a parseable
first tool call in 40 of 44 measured runs. Completion latency was **1,268.05 ms
p50 / 2,662.42 ms p95**. Its first streamed model output arrived at **540.73 ms
p50 / 1,456.34 ms p95**.

The unchanged Wisp candidate router previously measured **0.50 ms p50 / 11.05
ms p95** on the full 86-case routing corpus. The current first-step latency is
therefore dominated by Ling inference rather than candidate routing. These
numbers measure selection and argument generation through the first tool-call
boundary; no tool body ran.

| Measurement | Runs | p50 | p95 | Mean |
|---|---:|---:|---:|---:|
| First model delta | 44 | 540.73 ms | 1,456.34 ms | 648.69 ms |
| First tool delta | 40 | 1,268.05 ms | 2,662.13 ms | 1,415.02 ms |
| Complete first tool call | 40 | 1,268.05 ms | 2,662.42 ms | 1,415.05 ms |
| Complete stream | 44 | 1,367.98 ms | 3,624.32 ms | 1,664.13 ms |

The model was resident before and after the run. Model readiness took less than
one microsecond because no load was needed. The separately recorded first probe
completed its tool call in 1,580.92 ms. This run does not measure cold model
load or compilation.

## Cache and request-shape effects

The benchmark ran every case twice, keeping each captured Ling request exactly
the same. The first pass represents a unique-prompt pass within this run; the
second pass is an exact replay that can benefit from oMLX caching.

| Slice | Runs | Calls emitted | Complete-call p50 | Complete-call p95 |
|---|---:|---:|---:|---:|
| First pass | 22 | 20 | 1,555.38 ms | 3,346.60 ms |
| Exact replay | 22 | 20 | 944.50 ms | 2,450.26 ms |
| `tool_choice: required` | 24 | 24 | 1,081.19 ms | 1,764.84 ms |
| `tool_choice: auto` | 20 | 16 | 1,555.38 ms | 3,346.60 ms |
| English | 38 | 34 | 1,091.50 ms | 2,450.57 ms |
| Non-English | 6 | 6 | 2,015.17 ms | 4,692.97 ms |

The median request contained 4,441 prompt tokens and 2,048 cached tokens. oMLX
reported 0.43 s p50 / 1.44 s p95 prompt evaluation and 0.70 s p50 / 2.56 s
p95 generation. Median generation throughput was 107.78 tokens per second.

The non-English slice has only six runs across Spanish, French, and German, so
its latency difference is directional rather than conclusive.

## Selection behavior

Ling emitted at least one offered tool with parseable object arguments in
40/44 runs, a **90.9% tool-call emission rate**. It emitted no tool for both
`travel_directions` runs, the first `music_play` run, and the second
`followup_email_channel` run.

The raw output includes a simple first-tool label check. It scored 23/36
eligible runs, or 63.9%. This is a diagnostic rather than task accuracy:
`lookup_contact` before sending an email, `view_emails` before replying, and
`list_dir` before reading a named file can be valid precursor steps. Some
responses also emitted extra tools that would need execution-level review.

Four cases could not emit the corpus's labeled first tool because the Wisp
router omitted it from the candidate menu. Across two repetitions this accounts
for eight runs:

| Case | Missing labeled tool |
|---|---|
| `calendar_update` | `update_event` |
| `message_send` | `send_message` or `draft_message` |
| `file_read` | `read_file` |
| `es_message` | `send_message` or `draft_message` |

This candidate-recall limitation belongs to the current router and must be
reported separately from Ling or Laya selection quality.

## Frozen Laya comparison contract

[`ling_comparison_manifest.json`](ling_comparison_manifest.json) is the
authoritative input for the later Laya run. Its SHA-256 is
`341acb5a2e5105dd132b84eaa146cab0e28dd3c5fcd3b62ea41c6ea0cfe9711c`.
For every case it stores:

- the canonical user state and its hash;
- the ordered candidate tool names and their hash;
- the frozen tool schemas;
- conversation context and expected tool groups;
- the exact captured Ling request hash.

The Laya test must read this checked-in manifest. It must use byte-identical
canonical state and byte-identical candidate names in the same order. A
model-specific wrapper or classification question is allowed only if the
wrapper is saved and reported separately. Regenerating the manifest would no
longer be the same comparison.

## Method and limits

The tested artifact was the local `djrsystemservices/Ling-3.0-tiny-oQ6e`
conversion of `inclusionAI/Ling-3.0-tiny`. Its model card records oQ6e
importance-matrix quantization (128 samples by 512 tokens) produced with oMLX
0.6.0.dev1. The local directory has no Hub revision metadata, so the two weight
shards identify the exact tested bytes:

- `model-00001-of-00002.safetensors`:
  `1db4787dea5e0b22fd045ded7500a259132fc78639d897537fb5d6c6a3d3b4eb`
- `model-00002-of-00002.safetensors`:
  `582beca5c95ea1c3a6eead2dbd16e91782e5d85fc7d513c2306279a4fc7bd11c`

The Wisp source was exact commit
`41ff03128fb54d8092d7ee5be685aa3e19591862`.

The 22 synthetic cases are a frozen cross-section of the existing Laya corpus:
reads, effects, narrow and broad menus, compound work, contextual follow-up,
and three non-English requests. The clock was fixed at September 20, 2026,
10:00 AM PDT. Wisp used an isolated temporary data home, with memory and skills
context disabled, so the run did not read or write personal Wisp state.

The runner captures the real first model request produced by Wisp's agent loop,
then sends that request to the local oMLX OpenAI-compatible endpoint. It uses
the configured local bearer credential because this oMLX development runtime's
attributed client transport was unavailable. Traffic stayed on `127.0.0.1`.

This is one run on an Apple M5 Pro MacBook Pro with 24 GB memory, macOS 27.0,
oMLX 0.7.0.dev4, and Python 3.14.3. It excludes model load, tool execution,
multi-step planning, approval handling, and task completion. The 22 cases are
too small to establish production accuracy.

## Recommendation for trained Laya

Train Laya first as a candidate shortlist or first-tool ranker, with a fallback
that preserves every current safe candidate. Evaluate it against this manifest
before changing routing. A useful local classifier should aim well below 100 ms
and must improve candidate recall, especially for multilingual requests.

If Ling still generates arguments after Laya selects a tool, Laya adds its own
latency and may save only Ling prompt-prefill time. Measure that combined path
end to end. The relevant success criterion is a lower time to a valid complete
tool call without reducing candidate recall or increasing unsafe effects.
