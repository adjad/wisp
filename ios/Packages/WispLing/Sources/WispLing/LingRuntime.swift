// LingRuntime.swift
//
// Small app-facing façade over the Ling-3.0 (`bailing_hybrid`) MLX port:
// local-only load, chat-template rendering with thinking disabled, streamed
// generation with `<think>` content hidden, and cooperative cancellation.

import Foundation
import MLX
import MLXLLM
import MLXLMCommon
import MLXNN
import Tokenizers

public struct LingMessage: Sendable {
    public var role: String
    public var content: String
    public init(role: String, content: String) {
        self.role = role
        self.content = content
    }
}

public enum LingRuntimeError: LocalizedError {
    case missingFiles([String])
    case notLoaded
    case unsupportedModelType(String)

    public var errorDescription: String? {
        switch self {
        case .missingFiles(let files):
            "Ling model directory is missing: \(files.joined(separator: ", "))"
        case .notLoaded:
            "The Ling model is not loaded."
        case .unsupportedModelType(let type):
            "Unsupported model_type '\(type)' (expected bailing_hybrid)."
        }
    }
}

public actor LingRuntime {
    /// GPU buffer-cache ceiling. MLX otherwise keeps freed buffers around up to
    /// the device memory limit, which on an iPhone counts against the app's
    /// jetsam budget.
    public static let gpuCacheLimitBytes = 256 * 1024 * 1024

    /// Prompt tokens per prefill forward pass in `stream`.
    public static let prefillChunk = 512

    /// Registers `bailing_hybrid` with mlx-swift-lm's `LLMTypeRegistry`, so
    /// `LLMModelFactory` / `ModelContainer` can also load this architecture.
    public static func registerModelTypes() async {
        await LLMTypeRegistry.shared.registerModelType("bailing_hybrid") { data in
            let configuration = try JSONDecoder().decode(
                BailingHybridConfiguration.self, from: data)
            return BailingHybridModel(configuration)
        }
    }

    /// Files that must exist in the model directory to load.
    public static func requiredFiles(in dir: URL) -> [String] {
        var files = ["config.json", "tokenizer.json", "tokenizer_config.json", "chat_template.jinja"]
        let index = dir.appendingPathComponent("model.safetensors.index.json")
        if let data = try? Data(contentsOf: index),
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let map = json["weight_map"] as? [String: String]
        {
            files.append("model.safetensors.index.json")
            files.append(contentsOf: Set(map.values).sorted())
        } else {
            files.append("model.safetensors")
        }
        return files
    }

    public let modelDirectory: URL

    private var model: BailingHybridModel?
    private var tokenizer: (any Tokenizers.Tokenizer)?
    private var chatTemplate: String?
    private var stopTokenIds: Set<Int> = []
    private var topK: Int = 20

    public init(modelDirectory: URL) {
        self.modelDirectory = modelDirectory
    }

    public var isLoaded: Bool { model != nil && tokenizer != nil }

    var loadedModel: BailingHybridModel? { model }
    var loadedTokenizer: (any Tokenizers.Tokenizer)? { tokenizer }
    var loadedChatTemplate: String? { chatTemplate }

    /// Loads weights and tokenizer from the local directory. Never touches the network.
    public func load() async throws {
        if isLoaded { return }
        let dir = modelDirectory
        let missing = Self.requiredFiles(in: dir).filter {
            !FileManager.default.fileExists(atPath: dir.appendingPathComponent($0).path)
        }
        guard missing.isEmpty else { throw LingRuntimeError.missingFiles(missing) }

        Memory.cacheLimit = Self.gpuCacheLimitBytes

        let configData = try Data(contentsOf: dir.appendingPathComponent("config.json"))
        let configuration = try JSONDecoder().decode(
            BailingHybridConfiguration.self, from: configData)
        guard configuration.modelType == "bailing_hybrid" else {
            throw LingRuntimeError.unsupportedModelType(configuration.modelType)
        }
        let baseConfig = try JSONDecoder().decode(BaseConfiguration.self, from: configData)

        let model = BailingHybridModel(configuration)
        try await loadWeights(
            modelDirectory: dir, model: model,
            perLayerQuantization: baseConfig.perLayerQuantization)

        let tokenizer = try await AutoTokenizer.from(modelFolder: dir)
        let template = try String(
            contentsOf: dir.appendingPathComponent("chat_template.jinja"), encoding: .utf8)

        var stops = Set<Int>()
        if let eos = configuration.eosTokenId { stops.insert(eos) }
        for token in ["<|role_end|>", "<|endoftext|>"] {
            if let id = tokenizer.convertTokenToId(token) { stops.insert(id) }
        }
        if let eos = tokenizer.eosTokenId { stops.insert(eos) }

        if let data = try? Data(contentsOf: dir.appendingPathComponent("generation_config.json")),
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let k = json["top_k"] as? Int, k > 0
        {
            topK = k
        }

        self.model = model
        self.tokenizer = tokenizer
        self.chatTemplate = template
        self.stopTokenIds = stops
        Memory.clearCache()
    }

    public func unload() {
        model = nil
        tokenizer = nil
        chatTemplate = nil
        stopTokenIds = []
        Memory.clearCache()
    }

    /// Token ids for `messages` rendered with the model's chat template,
    /// thinking disabled, generation prompt appended.
    public func promptTokens(_ messages: [LingMessage]) throws -> [Int] {
        guard let tokenizer, let chatTemplate else { throw LingRuntimeError.notLoaded }
        return try LingChatFormat.encode(
            messages, tokenizer: tokenizer, chatTemplate: chatTemplate)
    }

    /// Streams visible text deltas. `<think>…</think>` (including an unclosed
    /// `<think>`) is never yielded. Cancelling the consuming task stops
    /// generation before the next token.
    public func stream(_ messages: [LingMessage], maxTokens: Int, temperature: Float)
        -> AsyncThrowingStream<String, Error>
    {
        let (stream, continuation) = AsyncThrowingStream<String, Error>.makeStream()
        let task = Task {
            do {
                try self.generate(
                    messages, maxTokens: maxTokens, temperature: temperature
                ) { continuation.yield($0) }
                continuation.finish()
            } catch {
                continuation.finish(throwing: error)
            }
        }
        continuation.onTermination = { _ in task.cancel() }
        return stream
    }

    private func generate(
        _ messages: [LingMessage], maxTokens: Int, temperature: Float,
        emit: (String) -> Void
    ) throws {
        guard let model, let tokenizer else { throw LingRuntimeError.notLoaded }
        let prompt = try promptTokens(messages)
        try Task.checkCancellation()

        let cache = model.makeCache()
        var filter = LingThinkFilter()
        var generated = [Int]()
        var emittedText = ""

        // Chunked prefill bounds the transient attention scores (MLA builds a
        // [heads, chunk, context] additive mask) and MoE buffers on long prompts.
        let tokens = prompt.map(Int32.init)
        var start = 0
        while tokens.count - start > Self.prefillChunk {
            try Task.checkCancellation()
            _ = model(MLXArray(Array(tokens[start ..< start + Self.prefillChunk]))[.newAxis], cache: cache)
            eval(cache.flatMap { $0.innerState() })
            start += Self.prefillChunk
            Memory.clearCache()
        }
        var logits = model(MLXArray(Array(tokens[start...]))[.newAxis], cache: cache)[0, -1]
        for _ in 0 ..< maxTokens {
            try Task.checkCancellation()
            let next = sample(logits, temperature: temperature)
            eval(next)
            let id = next.item(Int.self)
            if stopTokenIds.contains(id) { break }
            generated.append(id)

            let text = tokenizer.decode(tokens: generated, skipSpecialTokens: false)
            if !text.hasSuffix("\u{FFFD}") {
                let common = zip(text.unicodeScalars, emittedText.unicodeScalars)
                    .prefix { $0 == $1 }.count
                let delta = String(String.UnicodeScalarView(text.unicodeScalars.dropFirst(common)))
                emittedText = text
                let visible = filter.feed(delta)
                if !visible.isEmpty { emit(visible) }
            }
            logits = model(next.reshaped(1, 1), cache: cache)[0, -1]
        }
        let tail = filter.finish()
        if !tail.isEmpty { emit(tail) }
        Memory.clearCache()
    }

    private func sample(_ logits: MLXArray, temperature: Float) -> MLXArray {
        if temperature <= 0 {
            return argMax(logits, axis: -1)
        }
        var scaled = logits.asType(.float32) * (1 / temperature)
        if topK > 0, topK < scaled.dim(-1) {
            let kth = top(scaled, k: topK, axis: -1).min(axis: -1, keepDims: true)
            scaled = MLX.where(scaled .< kth, MLXArray(-Float.infinity), scaled)
        }
        return categorical(scaled)
    }
}

// MARK: - Chat template

public enum LingChatFormat {
    /// Renders with the checkpoint's Jinja template (enable_thinking=false). If
    /// the Jinja engine rejects the template, falls back to `render(_:)`, a
    /// hand port of the same template for plain (tool-free) conversations.
    public static func encode(
        _ messages: [LingMessage], tokenizer: any Tokenizers.Tokenizer, chatTemplate: String
    ) throws -> [Int] {
        let dicts: [[String: any Sendable]] = messages.map {
            ["role": $0.role, "content": $0.content]
        }
        do {
            return try tokenizer.applyChatTemplate(
                messages: dicts, chatTemplate: .literal(chatTemplate),
                addGenerationPrompt: true, truncation: false, maxLength: nil, tools: nil,
                additionalContext: ["enable_thinking": false])
        } catch {
            return tokenizer.encode(text: render(messages), addSpecialTokens: false)
        }
    }

    /// Hand port of chat_template.jinja for tool-free conversations with
    /// thinking off and add_generation_prompt=true.
    public static func render(_ messages: [LingMessage]) -> String {
        var out = "<role>SYSTEM</role>"
        var rest = messages[...]
        if let first = messages.first, first.role == "system" {
            if first.content.contains("detailed thinking on")
                || first.content.contains("detailed thinking off")
            {
                out += first.content + "<|role_end|>"
            } else {
                out += first.content + "\n" + "detailed thinking off<|role_end|>"
            }
            rest = messages.dropFirst()
        } else {
            out += "detailed thinking off<|role_end|>"
        }
        for message in rest {
            switch message.role {
            case "user":
                out += "<role>HUMAN</role>" + message.content + "<|role_end|>"
            case "system":
                out += "<role>SYSTEM</role>" + message.content + "<|role_end|>"
            case "assistant":
                var content = message.content
                var reasoning = ""
                if let close = content.range(of: "</think>") {
                    var head = String(content[..<close.lowerBound])
                    while head.hasSuffix("\n") { head.removeLast() }
                    if let open = head.range(of: "<think>", options: .backwards) {
                        head = String(head[open.upperBound...])
                    }
                    reasoning = String(head.drop(while: { $0 == "\n" }))
                    let after = content.components(separatedBy: "</think>").last ?? ""
                    content = String(after.drop(while: { $0 == "\n" }))
                }
                if reasoning.isEmpty {
                    out += "<role>ASSISTANT</role>\n<think></think>" + content
                } else {
                    let trimmed = reasoning.trimmingCharacters(in: CharacterSet(charactersIn: "\n"))
                    out += "<role>ASSISTANT</role>\n<think>" + trimmed + "</think>"
                        + String(content.drop(while: { $0 == "\n" }))
                }
                out += "<|role_end|>"
            case "tool":
                out += "<role>OBSERVATION</role>\n<tool_response>\n" + message.content
                    + "\n</tool_response><|role_end|>"
            default:
                break
            }
        }
        out += "<role>ASSISTANT</role>\n<think></think>"
        return out
    }
}

// MARK: - Think filter

/// Removes `<think>…</think>` spans from a streamed text, including a span
/// left unclosed at the end, and stray `</think>` tags. Holds back a partial
/// tag split across deltas.
public struct LingThinkFilter: Sendable {
    private static let open = "<think>"
    private static let close = "</think>"
    private var inThink = false
    private var pending = ""

    public init() {}

    public mutating func feed(_ delta: String) -> String {
        pending += delta
        var output = ""
        while true {
            if inThink {
                if let r = pending.range(of: Self.close) {
                    pending = String(pending[r.upperBound...])
                    inThink = false
                    continue
                }
                // keep only a possible partial "</think>" suffix
                pending = Self.partialSuffix(of: pending, for: [Self.close])
                return output
            }
            let openRange = pending.range(of: Self.open)
            let closeRange = pending.range(of: Self.close)
            if let o = openRange, closeRange.map({ o.lowerBound <= $0.lowerBound }) ?? true {
                output += pending[..<o.lowerBound]
                pending = String(pending[o.upperBound...])
                inThink = true
                continue
            }
            if let c = closeRange {
                output += pending[..<c.lowerBound]
                pending = String(pending[c.upperBound...])
                continue
            }
            let keep = Self.partialSuffix(of: pending, for: [Self.open, Self.close])
            output += pending.dropLast(keep.count)
            pending = keep
            return output
        }
    }

    /// Flushes held-back text at end of stream (discarded while inside a think span).
    public mutating func finish() -> String {
        defer { pending = "" }
        return inThink ? "" : pending
    }

    private static func partialSuffix(of text: String, for tags: [String]) -> String {
        var best = ""
        for tag in tags {
            for length in stride(from: min(tag.count - 1, text.count), to: 0, by: -1) {
                let suffix = String(text.suffix(length))
                if tag.hasPrefix(suffix) {
                    if suffix.count > best.count { best = suffix }
                    break
                }
            }
        }
        return best
    }
}
