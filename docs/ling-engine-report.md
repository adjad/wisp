# Ling oQ4e model-specific engine: M5 Pro evaluation

Status: performance study and native app smoke complete; Wisp integration and release validation in progress. Neither attempted optimization is selected for delivery.

## Scope and machine

A standalone local inference prototype for the installed Ling-3.0-tiny-oQ4e checkpoint on Apple M5 Pro with 24 GB unified memory. It provides direct Python generation, a CLI, and a serialized loopback JSON/SSE endpoint. The prototype has not replaced oMLX or Wisp.

The runtime reuses the installed oMLX 0.7.0.dev4 bundle (Python 3.11, MLX 0.32.2, mlx-lm 0.32.0), including its Ling architecture adapter and M5 quantized-gather correctness workaround. It is not an independently implemented Metal runtime. No new package, model download, weight conversion, or training was required.

Checkpoint: `/Users/adijain/Desktop/OMLX_Model_Files/TheWirelessPhoenix/Ling-3.0-tiny-oQ4e`. Recorded local HF revision: `eefee062847d78804f8c0434554aca187eac4ab4`. Config SHA256: `198a9789522f24a1e1ccd655fcd704c94e85e6bfa98f2f4b91a95a08e21ed12e`. Tokenizer SHA256: `40fb9d7d7795b8bd305aeff39ce9963f3f450915b9553f2938e009be9a1fed60`. Weight LFS metadata was inspected; weight bytes were not independently rehashed.

The checkpoint uses mixed precision: base affine 4-bit/group-64, with 141 overrides (135 eight-bit, four six-bit, two five-bit). These allocations remain unchanged. Architecture: 24-layer Bailing hybrid, with recurrent KDA and MLA attention, 128 routed experts and eight selected per token plus a shared expert.

## Correctness and latency methodology

Six synthetic cases cover JSON, a hypothetical tool schema, prose, approximately 1K and 4K prompts, and a repeated prefix without cache reuse. Every candidate prompt and generated token sequence must match an independently loaded stock reference. EOS token, stopping reason, and sampled-token counts are checked. Three additional held-out prompts gate correctness. Generated tool calls are never executed.

Three candidate repeats per case are timed externally with GPU synchronization. The score is the mean of each case's median wall generation time; lower is better. Model loading and reference generation are excluded. No warm-up repeat is discarded in this optimization score. The median limits first-call effects but is not a statistical confidence interval. Requests use greedy decoding, fresh hybrid state, and no prefix cache.

The benchmark and held-out gate received an independent Sol audit before GPU execution. Seven CPU boundary tests passed. Evaluation fixtures and gates are protected from optimizer edits.

## Recorded stock baseline

Baseline experiment `exp_0000`, commit `3b57cb8a9f45afa13246ff29c1775b3fdb2b4c35`; repository base `6c7bae346e26b6a593ecd7f038bb3e40dddf0afa`.

| Case | Median generation wall time |
| --- | ---: |
| JSON | 0.1543 s |
| Tool schema | 0.4178 s |
| Prose | 0.8029 s |
| Approximately 1K prompt | 0.2388 s |
| Approximately 4K prompt | 0.7437 s |
| Repeated prefix, cache disabled | 0.4020 s |
| Mean of case medians | **0.4599 s** |

All reference parity checks and held-out gates passed. The prose fixture reaches its fixed 128-token bound; this is not evidence of a complete prose answer. The 4K case generates nine visible tokens, so most of its latency is prefill. The recorded diagnostic peak MLX allocation was approximately 6.10 GB; it is neither process RSS nor total system unified-memory use. Contemporaneous status observation showed an idle Qwen model resident in oMLX during this baseline.

## Optimization results

| Variant | Initial score | Follow-up score | Decision |
| --- | ---: | ---: | --- |
| Stock baseline, 2,048-token prefill chunks | 0.4599 s | 0.4225 s | Retain |
| Compiled one-token expert selection | 0.4761 s | Not rerun | Discard: 3.52% slower aggregate |
| 4,096-token prefill chunks | 0.4239 s | 0.4191 s | Reject: broader exact-output regression |

The larger-chunk candidate initially appeared 7.83% faster, but an independent baseline/candidate recheck reduced this to 0.80%, within the project's 5% caution threshold. More importantly, a separate 4,101-token prompt produced a different greedy continuation in all three repeats: the first 13 token IDs matched, then stock generated 235 visible tokens versus 223 in the candidate. Although it passed the initial six-case and held-out gates, it failed the broader exact-output objective. EVO experiment `exp_0002`, commit `e83b58fac22e0334b283a7f1d1fa5022ee64d27e`, was therefore invalid-pruned. Its original 6.75 GB peak allocation was also about 0.65 GB higher than baseline.

This is a bounded negative result, not a failed attempt hidden from the report: two candidate attempts were evaluated, and neither supports a reliable equivalent-output acceleration claim. The delivery candidate uses the stock path. Compiled routing and chunk size results must not be advertised as completed model-specific Metal acceleration.

## Comparison with oMLX

This pilot used the identical checkpoint and rendered prompt bytes, greedy requests, three synthetic fixtures of 271, 1,024, and 4,101 prompt tokens, and three repetitions. Model startup is separate. The direct stock arm ran first with no models resident in oMLX; then Ling was loaded into oMLX for its arm. Requests were serialized. The on-disk Ling oMLX profile enables quantized KV state and its server has filtering/cache behavior absent from the direct path, so this is an installed-product comparison, not a perfectly isolated kernel comparison. Live effective sampling overrides are not independently exposed by the server.

| Fixture | Served text match | Direct stock median | oMLX uncached | oMLX cached |
| --- | ---: | ---: | ---: | ---: |
| JSON, 271 prompt tokens | 3/3 | 0.158 s (n=3) | 0.192 s (n=3) | No hits |
| Tool schema, 1,024 prompt tokens | 0/3 | 0.291 s (n=3) | 0.329 s (n=3) | No hits |
| Prose, 4,101 prompt tokens | 3/3 | 2.060 s (n=3) | 2.242 s (n=1) | 1.705 s (n=2) |

These are client wall times for direct Python generation versus local HTTP completion; they are not equally scoped transport measurements. The JSON and prose outputs matched exactly after decoding. The uncached prose comparison has only one server sample, so it is descriptive, not a stable percentage-speedup claim. Cached oMLX won that continued/repeated-prompt scenario; direct currently has no prefix reuse.

The tool case is not output-equivalent. The server usage counts 21 completion tokens while the direct path returns 21 visible token IDs; raw server token IDs are unavailable, but the direct path returns a native tool-call envelope and oMLX serves empty completion text. Read-only source inspection found `BailingHybridOutputParserSession` filtering tool envelopes before `/v1/completions` emits text, without structured tools configured for that request. This explains the API behavior; raw generated server token IDs were unavailable, so it does not prove raw-model parity. The comparison correctly returns a parity-failure exit code and preserves raw results. No generated tool was executed.

For short JSON, oMLX reported a rounded 0.00 s generation duration and implausibly high reported decode rates. Those fields are excluded from performance conclusions. For prose, the reported server decode rate and direct decode rate also have different token/timing conventions. Client wall time, cache status and output equivalence are the primary observations here.

Startup and memory observations:

- Direct stock process model load: 2.143 s. This is one launch, not a statistical startup result.
- Separate cold oMLX synthetic request: 1.875 s client time, with reported model-load duration 1.2 s. The measured comparison itself began with oMLX's Ling already resident.
- Direct stock MLX peak allocation: approximately 6.154 GB; experimental 4,096-chunk run: 6.795 GB. These are allocation metrics, not total system memory.
- oMLX status reported approximately 4.870 GB resident model memory. This is not directly comparable to the direct process's peak MLX allocation.
- The two raw experimental comparison directories reuse the same oMLX phase explicitly; there was no second independent server measurement.

The experimental larger-chunk path matched only 3/9 served outputs, versus stock 6/9. Its shorter/different prose is another reason not to advertise its elapsed-time reduction as equivalent-output speed.

Raw artifacts: `/private/tmp/ling-engine-comparison-stock-20260923/` (stock candidate setting), `/private/tmp/ling-engine-comparison-20260923/` (rejected candidate), and the EVO workspace's immutable attempt/check records. Final deliverable paths and app/API validation are appended after integration.

## Hardware utilization and limitations

MLX executes through Metal on the GPU. No profiler has yet established GPU occupancy, bandwidth saturation, or an upper bound on attainable M5 Pro throughput. No Neural Engine path, speculative draft, or concurrent batching path is implemented. A measured latency gain would not demonstrate full utilization of every chip component.

The prototype has an 8,192-token prompt cap and 2,048-token generation cap, a single resident checkpoint, serialized requests, and fresh per-request state. HTTP SSE streaming and API validation have passed 17 CPU tests plus repeated real-model streaming checks; Wisp binding validation is still pending. Structured tool-call serving is not qualified. The Python API can render native tool schemas. Prefix reuse needs careful snapshots of recurrent KDA state rather than ordinary KV slicing.

Runtime dependencies are supplied by the installed oMLX app. App updates require renewed parity testing. Production delivery remains subject to independent release review and exact-head GitHub CI; no deployment or merge is claimed by this local report.

## Native app and final engine checks

The corrected arm64 app was built at `/private/tmp/ling-native-20260923-v3/Ling Local.app`. Its deployment target is explicitly macOS 14.0; an inherited macOS 28.0 target in an earlier build was corrected after actual launch failed on this macOS 27 host. The missing executable bundle key was also corrected and checked by the build script.

Verified through the native UI: launch, checkpoint inspection, Start/readiness with the exact model ID, synthetic chat with real timing/memory metrics, and Stop. The listener on port 8767 was confirmed closed after Stop. The app remains unsigned and depends on the installed oMLX runtime; it has not replaced an installed application.

After the API refactor, all three frozen held-out cases again matched stock token IDs and stopping behavior. Seventeen CPU tests cover request validation, repeated Unicode streaming, cancellation and loopback HTTP/SSE. Two live SSE runs matched a nonstream response exactly, each delivered 33 events including usage and `[DONE]`. These checks establish the tested paths, not exhaustive lifecycle or security qualification.

Evidence files: `docs/ling-evidence/heldout-gate.json` and `docs/ling-evidence/live-api.json`. The stable model ID is `Ling-3.0-tiny-oQ4e`. API root: `http://127.0.0.1:8767/v1`; Wisp takes the origin `http://127.0.0.1:8767`. Tool serving is explicitly unsupported. GitHub CI and independent candidate review remain delivery gates.

## Review and repository validation

Candidate `ebc1a120d9934da91d229069d5576e3b446fadad` passed all 117 isolated repository test modules locally after the new Ling module was explicitly added to the mandatory full-profile manifest. The initial artifact CI run correctly rejected the unclassified test. This repair preserves fail-closed test discovery.

Independent review then identified two improvements being addressed before final delivery: reject browser-origin/foreign-host/non-JSON requests before generation, and keep native chat history bounded and recoverable after a failed send. Any resulting candidate needs fresh checks and review; the earlier passing evidence is not final-head approval.

A preserved copy of the launch-tested build is at `apps/LingLocal/dist/Ling Local.app` in the isolated workspace. Build outputs are ignored by Git. The source and report are reviewed through draft PR #75; Wisp local-provider settings are separately owned by Wisp Hub in draft PR #76. Neither PR is merged or deployed.

### Review repair validation

The browser boundary now requires the listener's exact numeric loopback Host, allows absent or exact same-origin Origin, and requires JSON POSTs with unambiguous singleton headers. Nineteen CPU tests pass. Against the actual rebuilt listener, foreign Host and Origin returned 403 and text/plain returned 415; normal native chat succeeded.

The app now retains at most 32 complete exchanges, removes old pairs only after a successful send, restores a failed turn to the draft, and offers New chat. Pure Swift history checks and explicit macOS 14 typechecking pass. Native v4 chat returned `ready`, and New chat cleared the visible conversation. The engine's generation math is unchanged.

The v4 test initially stalled while opening the default checkpoint directory. Stop worked; selecting the same checkpoint via the native Choose dialog resolved startup. File-access consent is a plausible cause, not independently proven. Use Choose to select the model before Start, especially for an unsigned rebuild. No privacy setting was weakened.

Additional evidence: `docs/ling-evidence/http-boundary-live.json`. The v4 test bundle is preserved as `apps/LingLocal/dist/Ling Local Reviewed.app`. Independent final review and fresh exact-head CI remain pending.

### End-to-end Wisp connection

Wisp Hub verified the real `OMLXClient.stream_events` against the running Ling app using an isolated temporary credential gate: exact model ID discovery succeeded, and a synthetic `Reply with OK.` request with a 64-token cap returned one content event and one final event without error. The associated Wisp candidate is PR #76 at `95af457998dac867abfa2d367870a7548cd51953`, with 217 affected CPU tests and a native Swift package build passing. Required CI and independent review are separate gates.

Ling candidate `38fcfa277aae342947c4117bb3f7e89423a48559` passed all 117 repository test modules after the HTTP/history repairs. The independent auditor repeated 19 Ling tests with 38 subtests and pure Swift history checks successfully. A final minor draft-preservation adjustment is being included; it requires fresh final-head checks.

The Mac locked before a quit-from-UI test with a running engine could be completed. The specific app-owned test-server process was terminated after the Wisp check, and its exit and closed listener were confirmed. Earlier Stop-button validation passed. Quit lifecycle under a live engine remains unverified; no lock or privacy control was bypassed.
