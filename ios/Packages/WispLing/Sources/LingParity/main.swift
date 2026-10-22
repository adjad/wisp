// ling-parity: run the fixed parity prompts through the Swift port and print
// one JSON object per prompt (same schema as parity/reference.py).
//
//   ling-parity /path/to/Ling-3.0-tiny-Wisp-V3-merged-oQ4e > parity/swift.jsonl

import CryptoKit
import Foundation
import MLX
import MLXLMCommon
import WispLing

// Keep in sync with parity/reference.py.
let prompts: [(String, [LingMessage])] = [
    ("greeting", [LingMessage(role: "user", content: "Hi! Who are you?")]),
    (
        "system_math",
        [
            LingMessage(role: "system", content: "You are Wisp, a concise assistant."),
            LingMessage(role: "user", content: "What is 17 * 23? Answer with just the number."),
        ]
    ),
    (
        "multi_turn",
        [
            LingMessage(role: "user", content: "Give me one tip for better sleep."),
            LingMessage(role: "assistant", content: "Keep a consistent bedtime, even on weekends."),
            LingMessage(role: "user", content: "Why does that help? Explain briefly."),
        ]
    ),
]
let genTokens = 32

func emit(_ object: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    print(String(decoding: data, as: UTF8.self))
    fflush(stdout)
}

func log(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}

func maxRSSBytes() -> Int {
    var usage = rusage()
    getrusage(RUSAGE_SELF, &usage)
    return Int(usage.ru_maxrss)  // bytes on Darwin
}

/// Think-filter checks that need no model: deltas split at awkward places.
func thinkFilterSelfTest() -> Bool {
    let cases: [([String], String)] = [
        (["Hello"], "Hello"),
        (["<think>secret</think>Answer"], "Answer"),
        (["A<thi", "nk>hid", "den</th", "ink>B"], "AB"),
        (["Visible <think>never closed ..."], "Visible "),
        (["x</think>y"], "xy"),
        (["a <", "b"], "a <b"),
        (["<think>", "</think>", "\n\nHi"], "\n\nHi"),
    ]
    var ok = true
    for (deltas, expected) in cases {
        var f = LingThinkFilter()
        var out = deltas.map { f.feed($0) }.joined()
        out += f.finish()
        if out != expected {
            log("think filter FAIL: \(deltas) -> \(out.debugDescription), expected \(expected.debugDescription)")
            ok = false
        }
    }
    return ok
}

if CommandLine.arguments.contains("--selftest") {
    let ok = thinkFilterSelfTest()
    log(ok ? "# think filter self-test: PASS" : "# think filter self-test: FAIL")
    exit(ok ? 0 : 1)
}

func quantizedOpTest() -> [String: MLXArray] {
    let key = MLXRandom.key(0)
    let keys = MLXRandom.split(key: key, into: 2)
    let w = MLXRandom.uniform(low: -0.05, high: 0.05, [4608, 1536], key: keys[0]).asType(.bfloat16)
    let x = MLXRandom.uniform(low: -1, high: 1, [27, 1536], key: keys[1]).asType(.bfloat16)
    let (wq, s, b) = MLX.quantized(w, groupSize: 64, bits: 4)
    let qmm = quantizedMM(x, wq, scales: s, biases: b, transpose: true, groupSize: 64, bits: 4)
    let qmv = quantizedMM(x[..<1], wq, scales: s, biases: b, transpose: true, groupSize: 64, bits: 4)
    let rms = MLXFast.rmsNorm(x, weight: MLXArray.ones([1536], dtype: .bfloat16), eps: 1e-6)
    let up = MLXRandom.uniform(low: -1, high: 1, [27, 1536], key: MLXRandom.key(1)).asType(.bfloat16)
    return [
        "q_w": w, "q_x": x, "qmm_bf16": qmm, "qmv_bf16": qmv, "rms_bf16": rms,
        "swiglu_bf16": compiledSiluProduct(x, up),
    ]
}

// Debug: `--optest <file>` evaluates a few element-wise ops on a fixed input
// (no model) so parity/trace.py optest can compare MLX Swift against Python MLX.
if let i = CommandLine.arguments.firstIndex(of: "--optest"), i + 1 < CommandLine.arguments.count {
    let x = MLXArray(stride(from: Float(-12), to: Float(12), by: Float(24.0 / 1_048_576)).map { $0 })
    let xb = x.asType(.bfloat16)
    let ops: [String: MLXArray] = [
        "x": x,
        "exp_f32": exp(x),
        "sigmoid_f32": sigmoid(x),
        "exp_bf16": exp(xb),
        "sigmoid_bf16": sigmoid(xb),
        "silu_bf16": xb * sigmoid(xb),
        "mul_f32": x * Float(-5),
        "lingsig_f32": lingSigmoid(x),
        "lingsig_bf16": lingSigmoid(xb),
    ].merging(quantizedOpTest()) { a, _ in a }
    try save(arrays: ops.mapValues { $0.asType(.float32) }, url: URL(fileURLWithPath: CommandLine.arguments[i + 1]))
    log("# optest written")
    exit(0)
}

guard CommandLine.arguments.count > 1 else {
    log("usage: ling-parity <model-dir> [--stream] | ling-parity --selftest")
    exit(2)
}
let modelDir = URL(fileURLWithPath: CommandLine.arguments[1])
let runStream = CommandLine.arguments.contains("--stream")
    && !CommandLine.arguments.contains("--teacher")

await LingRuntime.registerModelTypes()
let runtime = LingRuntime(modelDirectory: modelDir)
let t0 = Date()
try await runtime.load()
let loadSeconds = Date().timeIntervalSince(t0)
log("# loaded in \(String(format: "%.2f", loadSeconds)) s, active \(Memory.activeMemory / 1_000_000) MB")

// Debug: `--trace <dir>` dumps per-layer activations (see parity/trace.py) and exits.
if let i = CommandLine.arguments.firstIndex(of: "--trace"), i + 1 < CommandLine.arguments.count {
    let dir = URL(fileURLWithPath: CommandLine.arguments[i + 1])
    for (name, messages) in prompts {
        try await runtime.trace(messages, to: dir.appendingPathComponent("swift_\(name).safetensors"))
    }
    log("# traces written to \(dir.path)")
    exit(0)
}

// `--teacher <reference.jsonl>`: feed the reference's greedy tokens (teacher
// forcing) and report per-position top-5, so divergence is measured per step
// instead of compounding after the first differing token.
var forcedByPrompt = [String: [Int]]()
if let i = CommandLine.arguments.firstIndex(of: "--teacher"), i + 1 < CommandLine.arguments.count {
    let text = try String(contentsOfFile: CommandLine.arguments[i + 1], encoding: .utf8)
    for line in text.split(separator: "\n") where line.hasPrefix("{") {
        if let obj = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
            let name = obj["prompt"] as? String, let ids = obj["greedy_ids"] as? [Int]
        {
            forcedByPrompt[name] = ids
        }
    }
}

var totalDecode = 0.0
for (name, messages) in prompts {
    let r = try await runtime.probe(messages, generate: genTokens, forced: forcedByPrompt[name])
    totalDecode += r.decodeSeconds
    let sha = SHA256.hash(data: Data(r.promptTokenIds.map(String.init).joined(separator: ",").utf8))
        .map { String(format: "%02x", $0) }.joined()
    emit([
        "prompt": name,
        "prompt_tokens": r.promptTokenIds.count,
        "prompt_ids_sha256": sha,
        "top5": r.top5.map { [$0.id, Double($0.logit)] as [Any] },
        "greedy_ids": r.greedyTokenIds,
        "text": r.text,
        "jinja_matches_fallback": r.jinjaMatchesFallback,
        "teacher_forced": forcedByPrompt[name] != nil,
        "step_top5": r.stepTop5.map { $0.map { [$0.id, Double($0.logit)] as [Any] } },
        "prefill_tok_per_s": Double(r.promptTokenIds.count) / r.prefillSeconds,
        "decode_tok_per_s": Double(genTokens) / r.decodeSeconds,
    ])
}

if runStream {
    var streamed = ""
    for try await delta in await runtime.stream(prompts[0].1, maxTokens: genTokens, temperature: 0) {
        streamed += delta
    }
    log("# stream(): \(streamed.debugDescription)")

    // Long prompt (> prefillChunk tokens) exercises chunked prefill.
    let filler = String(repeating: "The quick brown fox jumps over the lazy dog. ", count: 120)
    let long = [LingMessage(role: "user", content: filler + "\nHow many times did the fox jump? Answer briefly.")]
    let longCount = try await runtime.promptTokens(long).count
    var longText = ""
    let t = Date()
    for try await delta in await runtime.stream(long, maxTokens: 24, temperature: 0) {
        longText += delta
    }
    log("# stream(long, \(longCount) prompt tokens, \(String(format: "%.2f", Date().timeIntervalSince(t))) s): \(longText.debugDescription)")

    // Cancellation: stop consuming after 3 deltas; the producer must stop too.
    let consumer = Task {
        var n = 0
        for try await _ in await runtime.stream(prompts[2].1, maxTokens: 200, temperature: 0.7) {
            n += 1
            if n == 3 { break }
        }
        return n
    }
    let seen = try await consumer.value
    let tc = Date()
    _ = try await runtime.promptTokens(prompts[0].1)  // actor is free again once generation stopped
    log("# cancel: consumer stopped after \(seen) deltas; actor free after \(String(format: "%.3f", Date().timeIntervalSince(tc))) s")
}

emit([
    "summary": true,
    "load_s": loadSeconds,
    "decode_tok_per_s": Double(genTokens * prompts.count) / totalDecode,
    "mlx_peak_memory_gb": Double(Memory.peakMemory) / 1e9,
    "max_rss_gb": Double(maxRSSBytes()) / 1e9,
])
await runtime.unload()
