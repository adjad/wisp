# M5 Pro compute and Metal: implications for Ling

Research date: 23 September 2026. This memo separates locally observed configuration, published capabilities, and engineering hypotheses. It does not claim a measured hardware utilization ceiling.

## The actual machine

Local System Information reports a MacBook Pro, model Mac17,8, with an **18-core CPU (6 super + 12 performance cores), 20-core GPU, 24 GB unified memory, and Metal 4 support**. macOS is 27.0 build 26A428. AC power was attached during inspection. Hardware serial numbers and device identifiers are intentionally excluded.

Apple specifies **307 GB/s memory bandwidth**, a **16-core Neural Engine**, hardware ray tracing, video encode/decode, ProRes acceleration, and AV1 decoding for this M5 Pro. The M5 Pro family supports up to 64 GB, but this machine has 24 GB; the larger family maximum is not available memory here. The laptop has Thunderbolt 5 ports rated up to 120 Gb/s. These interface figures are not measured application bandwidth. [Apple technical specifications](https://support.apple.com/en-us/126319)

Apple describes M5 Pro as a two-die Fusion Architecture integrating the CPU, GPU, unified-memory controller, Neural Engine and I/O. It advertises over 4× peak GPU AI compute versus M4 Pro. That is a peak-compute comparison, not a promise of 4× tokens per second for Ling or of 4× speed in every kernel. [Apple M5 Pro announcement](https://www.apple.com/newsroom/2026/03/apple-debuts-m5-pro-and-m5-max-to-supercharge-the-most-demanding-pro-workflows/)

## The compute blocks have different jobs

| Resource | Useful work in this project | Current evidence |
| --- | --- | --- |
| CPU | Tokenization, HTTP/SSE, app UI, command encoding, routing and tools | Used by the local runtime; no per-core profile yet |
| GPU shader cores | Quantized matrix/vector operations, attention, reductions, elementwise operations | MLX/Metal execution; kernel-specific occupancy unmeasured |
| GPU Neural Accelerators | Suitable matrix multiplications, especially larger prompt-processing batches | MLX supports this hardware path; not a blanket guarantee for every Ling operation |
| Separate Neural Engine | Converted and supported on-device inference graphs | Not used by our current Ling implementation |
| Unified memory and caches | Weights, activations, hybrid attention state, other apps | Shared capacity creates cross-task contention; 24 GB is the practical total |
| Media engine | Audio/video product pipelines when supported, video codecs | Not a text-token acceleration path |
| SSD/I/O | Model startup, files, cache persistence | Not a replacement for RAM during steady generation |

Apple’s MLX presentation says it selects hardware-appropriate kernels and can use M5 GPU Neural Accelerators without a special application flag. It also describes continuous batching and distributed inference. Those are framework capabilities, not features automatically implemented by this prototype’s single-request server. [Apple MLX session](https://developer.apple.com/videos/play/wwdc2026/232/)

The separate Neural Engine is not another generic Metal shader device. Core ML exposes choices including CPU+Neural Engine, but a model still needs a compatible representation and operations. Splitting Ling between GPU and ANE would need conversion, state management and measurements; using every block concurrently is not itself an optimization. [Core ML compute units](https://developer.apple.com/documentation/coreml/mlcomputeunits/cpuandneuralengine)

For numerical CPU work, Accelerate supplies optimized BLAS/LAPACK, vector DSP and related routines. Use its supported interfaces rather than assuming an undocumented AMX instruction path or guessing M5-specific matrix-unit throughput. The public sources reviewed here do not establish exact M5 Pro CPU clock/cache details, sustained TDP, or a complete precision-specific TFLOPS/TOPS table. [Apple Accelerate](https://developer.apple.com/accelerate/)

## How Metal executes an inference step

An application creates GPU resources and compute pipeline states, encodes work into command buffers, and submits it asynchronously. The CPU can prepare subsequent work while the GPU executes earlier commands; forcing a CPU wait after every small operation can destroy that overlap. This is why host submission and synchronization deserve measurement alongside kernel math. [Apple GPU compute introduction](https://developer.apple.com/documentation/metal/performing-calculations-on-a-gpu)

A compute grid contains threadgroups; each group contains threads with a shared memory region. Threadgroup sizing must account for the pipeline’s execution width and resource limits. More threads or bigger tiles are not automatically faster: register pressure and per-group memory can reduce concurrent work. A shape-specialized Ling kernel can choose its layout around actual matrix sizes, but needs bounds handling and parity validation. [Threadgroups](https://developer.apple.com/documentation/metal/creating-threads-and-threadgroups), [grid sizing](https://developer.apple.com/documentation/metal/calculating-threadgroup-and-grid-sizes)

Dependencies still matter in unified memory. Read-after-write conflicts between passes require correct ordering; overly broad barriers can serialize independent work. Removing all barriers would create races, not a valid speedup. A persistent multi-stage kernel must establish legal cross-stage synchronization and forward progress; a CPU-style global barrier cannot simply be assumed across all resident and nonresident GPU groups. [Apple resource synchronization](https://developer.apple.com/documentation/metal/resource-synchronization)

Metal 4 offers lower-overhead submission facilities, reusable allocation structures, tensor resources and explicit synchronization tools. Adopting the API only helps when the runtime actually changes its submission/resource behavior; wrapping an existing Python call in a new app does not rewrite its GPU scheduling. [Metal 4 core API](https://developer.apple.com/documentation/metal/understanding-the-metal-4-core-api)

Apple’s M5 GPU guidance explains the core performance distinction: large matrix operations often benefit from compute throughput, whereas skinny operations can be dominated by memory traffic. Its TensorOps primitives target GPU Neural Accelerators and can combine matrix operations with custom shader work. Cooperative tensors retain intermediate values on chip, avoiding some write/read round trips. Support expanded across macOS 26 releases to include bfloat and quantized tensor forms. Profiling should combine Metal System Trace for CPU/GPU gaps with the Metal debugger for per-kernel statistics. These mechanisms are relevant to Ling; Apple’s illustrative speedups are not Ling measurements. [Apple M5 GPU technical talk](https://developer.apple.com/videos/play/tech-talks/111432/)

The 2026 Metal guidance additionally covers quantized tensors and scale factors. A weight being called 4-bit does not prove it already uses the optimal tensor layout: kernel format, scaling, grouping and supported operations must agree. Ling’s 5-, 6-, and 8-bit overrides must remain valid rather than being silently converted to fit a 4-bit path. [Apple Metal 2026 guide](https://developer.apple.com/wwdc26/guides/metal/)

MLX arrays share a memory pool accessible to CPU and GPU, so ordinary array placement does not require a discrete-GPU-style copy. This removes one source of overhead; it does not remove cache misses, synchronization, allocation cost or memory-bandwidth limits. [MLX unified-memory documentation](https://ml-explore.github.io/mlx/build/html/usage/unified_memory.html)

## What this means for Ling

The following are engineering deductions from the locally inspected model/runtime, not externally established speedups:

1. **Separate prefill and decode.** Our 4K case spends most of its measured time before the first token; the 128-token prose case spends most time decoding. One aggregate number conceals two different optimization problems.
2. **Measure expert traffic.** Ling routes 8 of 128 experts per token and has a shared expert. It does not read every expert weight on every decode token, so dividing total model size by bandwidth is not a valid precise decode ceiling. We need actual active-weight traffic, gather locality, recurrent-state traffic and overhead.
3. **Preserve the hybrid cache.** Ling’s recurrent KDA state cannot be safely shortened with ordinary KV slicing. Prefix caching needs state snapshots at valid boundaries. Speculative rejection similarly requires correct rollback of recurrent and convolution state, not just attention tensors.
4. **Consider projection fusion.** Paired gate/up projections can potentially share input reads and dispatches. Compatibility depends on individual quantization metadata and tensor shapes. A custom Metal implementation must maintain the exact original values and reference semantics.
5. **Keep the M5 workaround until verified.** Installed oMLX has a sorted quantized-gather correctness workaround used by this prototype. An accelerated path that bypasses it may appear fast while being incorrect. Removing it requires a targeted proof against the actual supported shapes.
6. **Use real workload mixtures.** Short chat, JSON, long tool histories, code edits and large documents reward different techniques. Requesting fewer output tokens or testing only copy-heavy edits can exaggerate an apparent win.
7. **Measure occupancy before promising saturation.** This round currently has wall-clock timings and allocation diagnostics, not hardware-counter traces. A second-stage kernel project should begin with a bounded profiler capture, then address the dominant cost.

A useful approximate bound is `step_time >= max(bytes_moved / effective_bandwidth, operations / effective_compute_rate)`, plus exposed scheduling and synchronization costs. The effective rates are workload-dependent. It is an analytical guide, not a token-rate prediction; 307 GB/s is a specification ceiling, not a measured effective rate for scattered MoE gathers.

## Scaling, power and coexistence

MLX’s JACCL backend supports RDMA over Thunderbolt 5 on supported macOS versions, allowing distributed workloads across multiple Macs. It requires configuration, and the communication fabric is not one shared local RAM pool. Model sharding can make larger models fit, but small-model decode can lose to communication overhead. No RDMA or additional-machine setup was performed here. [MLX JACCL documentation](https://github.com/ml-explore/mlx/blob/main/mlx/distributed/jaccl/lib/README.md), [Apple distributed MLX session](https://developer.apple.com/videos/play/wwdc2026/233/)

For this 24 GB machine, simultaneous resident models, caches, app memory and temporary buffers compete. We coordinate local measurements through Wisp Hub’s explicit request/grant/release ledger and serialize inference. Light code/review and remote CI can proceed; heavy builds, large downloads and competing inference wait for a handoff. GPU utilization alone is insufficient: latency stability, memory pressure and energy per useful request matter. We have not measured watts or joules/token, so neither battery life nor efficiency improvements are claimed.

## What Splash actually implements

The public repository was inspected at commit `edb4b8fa4eee5fef624809cd7f30f0651c58a167`; its license file is Apache 2.0. This was read-only source inspection, not an execution of Splash or an independent verification of its published throughput.

| Source inspected | Concrete mechanism | Possible relevance to Ling |
| --- | --- | --- |
| [Q4 MPP tiles](https://github.com/incoai/splash/blob/edb4b8fa4eee5fef624809cd7f30f0651c58a167/runtime/metal/kernels/common/q4_mpp_tiles.h) | Group-64 packed 4-bit tiles, `matmul2d`, FP32 accumulation, scale/bias epilogue, configurable SIMD-group scope and paired projections | A reference for a new compatible 4-bit projection kernel; cannot directly cover Ling’s mixed 5/6/8-bit modules |
| [WeightStore](https://github.com/incoai/splash/blob/edb4b8fa4eee5fef624809cd7f30f0651c58a167/runtime/model/WeightStore.cpp) | Read-only `MAP_SHARED` mappings for immutable packed weight files; validates files before mapping | Avoid unnecessary private dirty copies, but first measure the existing MLX loader’s actual memory behavior |
| [CommandGraph](https://github.com/incoai/splash/blob/edb4b8fa4eee5fef624809cd7f30f0651c58a167/runtime/metal/CommandGraph.hpp) | Ordered dispatch list for one command buffer; owns parameter payload storage until submission | Batch submission and correct parameter lifetime; this class alone is not evidence of a whole-model persistent kernel |
| [StateCache](https://github.com/incoai/splash/blob/edb4b8fa4eee5fef624809cd7f30f0651c58a167/runtime/engine/StateCache.cpp) | Composite-state leases attached to resident KV blocks; deepest reusable boundary; pinning and eviction accounting | Design pattern for hybrid prefix reuse with exact state ownership |
| [QwenState](https://github.com/incoai/splash/blob/edb4b8fa4eee5fef624809cd7f30f0651c58a167/runtime/model/QwenState.cpp) | Explicit convolution/recurrent buffers and exact snapshot copies | Confirms recurrent-state caching is more than keeping attention keys and values; Ling requires its own state layout |
| [MemoryPlan](https://github.com/incoai/splash/blob/edb4b8fa4eee5fef624809cd7f30f0651c58a167/runtime/engine/MemoryPlan.cpp) | Combines recommended working set, configured cap, fixed weights/scratch, dynamic state/KV and minimum viable capacity; checked arithmetic and fail-closed admission | Strong pattern for a 24 GB app sharing memory with Wisp and other models |

Splash’s published design combines a shared serving layer with specialized Qwen model backends, target/draft execution, paged cache and packed weights. It requires its model packages; a plain Ling MLX directory is not an interchangeable Splash package. Its publisher benchmarks use different model/configuration/memory combinations, so their ratios are not forecasts for this machine. [Splash design article](https://inco.ai/blog/splash/), [developer architecture](https://github.com/incoai/splash/blob/edb4b8fa4eee5fef624809cd7f30f0651c58a167/DEVELOPMENT.md)

One particularly valuable lesson is in Splash’s own follow-up notes: kernel-level gains can shrink to near-zero whole-model gains. A changed floating-point reduction order passed numerical tests but altered speculative acceptance and was rejected. The notes describe alternating-order measurements, preserving arithmetic order, and unsuccessful fusion/register-residency experiments. That argues for measuring complete requests and token behavior, not promoting an attractive microbenchmark alone. [Splash decode follow-up](https://github.com/incoai/splash/blob/edb4b8fa4eee5fef624809cd7f30f0651c58a167/dev/benchmarks/remaining-decode-optimizations.md)

## What Husky contributes

Husky’s publisher describes Woof-specific kernels, packed 4-bit weights, INT8 cache, recurrent-state persistence and pipelined submission. Its largest gains use eight-token verification: prompt lookup proposes copied text, or a separately trained draft proposes new text. The article reports 16 greedy tasks on an M5 Max 40-core/128 GB system; plain writing without the draft is often close to MLX. It also reports an MLP persistent-dispatch experiment that produced identical output without getting faster. These are publisher results, not reproduced Ling results. The public article does not supply a reviewed implementation to transplant. [Husky technical article](https://husky.underdog.ai/)

The implication for our design is to separate three optimizations: making a normal target step cheaper, avoiding repeated prompt work, and obtaining multiple accepted tokens per target verification. They have different correctness obligations and should never be combined into one unexplained speed multiplier.

## Speculative decoding and the hybrid-state problem

A small draft proposes several token IDs. The target evaluates that proposed block and accepts the valid prefix, then supplies a correction where needed. Under greedy decoding, accepted tokens must match the target’s own choices. Exact stochastic sampling requires the proper acceptance/correction distribution; a plain equality test is not a general proof of distribution preservation.

The speed benefit depends on accepted tokens per verification, draft cost and verification cost. If proposals are mostly rejected, the draft adds work. Prompt lookup can work well on copying/editing without a learned draft, but offers little when new text differs from the prompt. These are algorithmic design consequences, not a claim of a trained draft for Ling.

For Ling, verification that advances KDA’s recurrent state must be reversible to the accepted boundary. Target KV, KDA recurrence and convolution history all need to agree after rejection. A fast but incomplete rollback can corrupt subsequent tokens while superficially passing a short example. A future implementation needs rejection-position tests, long continuation tests, tool-history tests and exact-token baselines.

## A practical next research sequence

1. Capture a small representative Metal trace to determine whether expensive work is quantized projections, expert dispatch, KDA, MLA, allocation or CPU submission gaps. Keep recording overhead out of throughput comparisons.
2. Add request-level memory admission and hybrid-state cache accounting before increasing concurrency or adding a draft model.
3. Prototype one exact-shape projection/fusion at a time with mixed-quant compatibility guards and stock fallback. Validate activation/weight layout as well as output tokens.
4. Implement exact hybrid prefix snapshots; compare genuinely continued chats against oMLX with its cache enabled.
5. Evaluate prompt-lookup speculation on edit-heavy tasks after rollback correctness exists. A learned draft is a separate training/conversion/resource project.
6. Evaluate native command scheduling only where traces show exposed submission overhead. Keep end-to-end latency, memory and quality as acceptance criteria.

A general framework is not necessarily executing generic slow code. MLX already compiles functions, fuses operations and caches compiled variants; the useful question is which remaining cost a model-specific implementation removes. Our first compiled-routing experiment was correct but did not improve the aggregate benchmark. [MLX compilation documentation](https://ml-explore.github.io/mlx/build/html/usage/compile.html)

No technique above is presented as a completed custom Metal optimization in the current app. The current prototype and its measured limits are documented separately in the engine evaluation report.
