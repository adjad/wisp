# Routing latency — October 7, 2026

Keep deterministic complete-read shortcuts. Flexible reads require one model interpretation; the new validation/compiler work itself adds less than a millisecond in this development measurement. **V2 latency on the Mac is unmeasured.** The original source and oQ4e/oQ6e Ling models are installed, but no verified merged V2 MLX serving artifact was found. No model or production setting was changed for this report.

## New CPU measurement

Sixteen unchanged synthetic cases, one warmup per case/path and 50 timed repetitions per case/path, serialized on this Mac in the repository's disposable pytest state. No tool function, HTTP request or model runs. The fixed synthetic clock is saved in `cases.jsonl`, passed explicitly to compilation/validation, and applied to the legacy calendar helper through an isolated `reads.datetime` fixture outside timed calls, restored after each case. The deterministic path uses `compile_read`, then `rule_route` when no shortcut matches. The intent path builds the shared request, parses a supplied gold JSON answer, validates it and compiles supported reads; **it excludes Ling generation, residency checks and transport**.

| CPU stage | Samples | Median | p95 |
|---|---:|---:|---:|
| Deterministic selection | 800 | 0.216 ms | 0.349 ms |
| Shared request + supplied-intent validation/compilation | 800 | 0.294 ms | 0.700 ms |

These are distributions of different stages, not a paired end-to-end latency delta. The deterministic stage returned two read shortcuts, twelve rule decisions and two unmatched requests; timings do not imply those decisions were correct or executable without a later model. Supplied intents validated as twelve reads, three unsupported requests and one inline draft. Largest per-case p95 was 0.371 ms for deterministic selection and 0.754 ms for intent CPU work.

Reproduce the registered capture only with a fresh acknowledged output scope (the current directory cannot be overwritten):

```sh
PROMPT_ALIGNMENT_CPU_OUTPUT="$PWD/eval/prompt-alignment-dev-20261007/artifacts/cpu-latency-2" /Users/adijain/Desktop/MOE_Project/.venv/bin/python -m pytest tests/test_router_intent_request.py -k test_registered_cpu_latency_capture -q
```

`cpu_latency.py` rejects invocation outside the isolated pytest bootstrap. The raw and summary files are ignored local evidence under `artifacts/cpu-latency-2/`. Capture 1 remains immutable and superseded: it omitted the explicit deterministic clock and left the legacy calendar helper on wall time. The correction was separately acknowledged before capture 2. Summary records the working-tree source hashes, initial HEAD, platform/Python, clock resolution, exact case statuses and nanosecond samples. Measurement preceded the next checkpoint commit; source hashes identify the measured bytes. These are development cases, not held-out qualification. Capture 2 summary SHA256 is `62a5e54abacec54273fc7bbd8c20671ad29ab69a2a43d20ac8f75ce171315e77`; raw SHA256 is `2baf8bcd072c42410ed376df73ec3e16a7ca8fa6576f224356e45e46a6f2876d`. Capture receipt: `/Users/adijain/Documents/Codex/2026-10-05/new-chat/WISP_PROMPT_ALIGNMENT_CPU_CAPTURE2_FIXED_CLOCK_ACK_2026-10-07.json`, SHA256 `85f1021aa08e974efe65a8ff0d0146a3f82dc901cf3b29b92e156815c6afac0a`.

## Previously measured model inference, separately attributed

| Existing measurement | Median | p95 | Meaning |
|---|---:|---:|---|
| Mac base Ling oQ6e revised typed-intent prototype | 0.534 s | 1.007 s | Actual resident local model, 36 fresh synthetic requests repeated three times, previous October 5 evaluation |
| Mac rules plus controlled Ling generation baseline, same old corpus | 0.443 s | 1.285 s | Includes model calls where rules required them; **not pure deterministic CPU routing** |
| A6000 base Ling, unquantized HF | 13.791 s | 34.153 s | Archived real-life routing evaluation, 95 warm requests |
| A6000 V1, HF with PEFT adapter | 17.224 s | 28.492 s | Same archived warm request IDs, sequential arm |
| A6000 V2, HF with PEFT adapter | 14.836 s | 25.415 s | Same archived warm request IDs, sequential arm |

The Mac prototype was an improved prompt/compiler, **not the fine-tuned V2 adapter**. Its median was 91 ms higher and its p95 278 ms lower than the old mixed baseline; those differences cannot be transferred to V2 or pure deterministic routing. Cache conditions, generation, backend and corpora differ across the Mac and GPU measurements. No cold model start, real source I/O or final user-response delivery is included in that earlier local report.

The A6000 calculation excludes each arm's first routing request after loading and all 24 overview requests; all remaining 95 routing completions had status `ok` and usable completions. This status does not mean semantic correctness. HF policy used greedy decoding, thinking disabled and a 512-token routing cap; the proposed serving request declares 900. Base was bf16 and the adapters used PEFT's recorded tensor dtype policy. Output lengths and correctness differ across arms, so elapsed times alone do not rank model efficiency. `summarize_archive.py` validates arm/request alignment and saves hashes without exporting prompt contents. Original archives and corpora remain unchanged.

Historical evidence: [October 5 report](/Users/adijain/.codex/worktrees/routing-eval-20261005/MOE_Project/eval/independent-routing-20261005/REPORT.md). Local raw GPU evidence: `/Users/adijain/.codex/datasets/wisp-real-life-validation-20261006/results-review-20261007/raw-copy/results/real-life-1/`. New derivative: `artifacts/cpu-latency-1/archived-model-summary.json`.

## Expected effect on Wisp

- Exact shortcuts retain their existing path and add **zero model calls**. The known complete subject-free `weej` agenda spelling now also resolves through the existing exact agenda grammar.
- Flexible reads add one interpretation call relative to a hypothetical deterministic solution. CPU overhead is small; the model call dominates. A failed interpretation may need one repair within the same overall deadline.
- Compared with today's ambiguous agent path, interpretation can replace tool selection and avoid a later generative rewrite of structured receipts. A net speedup is possible, but has not been measured end to end.
- The existing planner default overall deadline is 2.5 seconds, configurable up to 5 seconds. This bounds waiting before clarification; it does not prove a V2 completion fits that budget or that an in-flight server generation is canceled. The archived A6000 backend would often miss this budget.

**Recommendation:** retain deterministic shortcuts; finish strict request-budget/history/schema handling and validate a merged/quantized V2 export on the actual Mac before enabling it for Wisp 1.3.0, after 1.2.0. These changes remain isolated and unmerged; they do not alter 1.2.0 release settings. Compare identical synthetic cases through the real client, record effective wire payload, output tokens, repairs, p50/p95, timeouts and contention, then include source/result presentation costs in a separate full-turn benchmark. Do not promise a V2 latency hit of half a second from base-model evidence.

The five parser failures are repaired and their original failures retained in the handoff. The shared-client fix is an **unapplied proposal** in `STRICT_FITTING_PROPOSAL.patch`; it awaits the actual retained file-owner release. Three strict transport expected failures remain. This report and branch do not qualify activation, merging or Wisp release performance.
