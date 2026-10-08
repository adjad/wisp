# Routing latency — October 7, 2026

Keep deterministic complete-read shortcuts. Flexible reads require one model interpretation; the new validation/compiler work itself adds less than a millisecond in the CPU development measurement. A separate, completed actual-oMLX comparison now measures the existing V2 merged oQ4e export on this Mac. It does not measure this branch’s shared request through the Wisp client or qualify end-to-end Wisp latency. The earlier statement that no verified V2 Mac export existed was true when this report was first written; it is superseded by the separately recorded comparison below.

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


## Completed actual-oMLX export diagnostic, added October 7

The user separately requested loading both exports through installed oMLX 0.7.0. The completed diagnostic generated 120 requests/model, sequentially, using the frozen real-life validation bundle; no real tools or user data were accessed. All 120 paired messages/request hashes and served prompt-token counts matched. Its scored routing denominator is 80; the 16 ambiguous requests and 24 grounded answers are separate. No extra run occurred to update this document.

| Existing export | Exact routing | Schema valid | Warm routing p50 / p95 | Server TTFT p50 / p95 |
| --- | --- | --- | --- | --- |
| Base Ling oQ4e | 0/80 | 12/80 | 0.583 / 0.935 s | 0.220 / 0.240 s |
| Wisp V2 merged oQ4e | 13/80 | 49/80 | 0.519 / 0.840 s | 0.220 / 0.230 s |

Warm timing is 79 scored routing requests/model, one observation/case, first request excluded, no prompt cache hits reported, base first and OS caches not flushed. Explicit load API times were 1.620/1.936 s and first-request times 1.330/0.803 s (base/V2). Service model-memory estimate was 4.535 GiB for each; it is not total process RAM or allocator peak. No controlled cold-start measurement was made.

The diagnostic includes the intent schema as literal prompt text, synthetic clock and selected context, greedy/no-thinking decoding and a 512-token routing cap. This branch instead declares a 900-token cap, sends schema in response_format, and adds prior-tool source metadata through the shared request builder. The actual comparison bypasses the Wisp OMLXClient fitting path. Therefore **0.519 s is V2 serving latency for that frozen diagnostic, not an aligned-planner latency or a measured hit versus deterministic routing**. No API schema enforcement was requested or qualified in the completed diagnostic.

Both oQ4e exports have different per-layer mixed-bit allocations/calibration, so results do not isolate fine-tuning alone or quantify its quantization loss relative to BF16. Source/argument diagnostics in the unchanged scorer require schema validity. The 0/80 base exact score does not imply zero language understanding.

V2 has real routing improvements but still fails follow-ups, requested source coverage and arguments. Qualitative answer review found stale/denied-source handling failures and regressions in timezone conversion and calendar availability. Neither unrestricted Ling routing nor a general assistant improvement is qualified.

Full report: [actual-oMLX diagnostic report](/Users/adijain/.codex/visualizations/2026/10/05/01a10aef-17bd-7f80-b5c8-f5cc08fe2506/ling-v2-oq4e-comparison-20261007/captureomlx/REPORT.md). Report SHA256 `25686e5d871be3c64c551ce81bbd03d4c678be77b2a595b87a0fffa3baed59bc`; machine summary SHA256 `d8323f70b3faa89887b0bdd266d5be475f472d390a9ff65d7ddd15d70e3baee2`; execution SHA256 `5ab926c0db1a608c304050d7820847a9968dccc08cf74c89e2ceb3cc34877030`. Both evaluator models were unloaded and the reservation released. That past cleanup is not present availability or authority for a new run.
