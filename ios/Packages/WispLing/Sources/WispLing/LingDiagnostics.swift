// LingDiagnostics.swift
//
// Deterministic probe used by the `ling-parity` tool to compare this port
// against the Python mlx-lm reference (parity/reference.py runs the identical
// procedure).

import Foundation
import MLX
import MLXLMCommon

public struct LingProbeResult: Sendable {
    public var promptTokenIds: [Int]
    /// (token id, logit) for the five largest logits at the last prompt position.
    public var top5: [(id: Int, logit: Float)]
    public var greedyTokenIds: [Int]
    /// Top-5 at every decoded position (index 0 = last prompt position); filled
    /// only when teacher forcing.
    public var stepTop5: [[(id: Int, logit: Float)]]
    public var text: String
    public var jinjaMatchesFallback: Bool
    public var prefillSeconds: Double
    public var decodeSeconds: Double
}

extension LingRuntime {
    /// One forward pass over the whole rendered prompt with a fresh cache,
    /// top-5 of the last position, then `generate` greedy tokens one at a time
    /// (no EOS stop). With `forced`, those tokens are fed instead of the argmax
    /// (teacher forcing on the reference's sequence); `greedyTokenIds` still
    /// reports this model's argmax at each position.
    public func probe(_ messages: [LingMessage], generate count: Int, forced: [Int]? = nil) throws
        -> LingProbeResult
    {
        guard let model = loadedModel, let tokenizer = loadedTokenizer,
            let template = loadedChatTemplate
        else { throw LingRuntimeError.notLoaded }

        let prompt = try LingChatFormat.encode(
            messages, tokenizer: tokenizer, chatTemplate: template)
        let fallback = tokenizer.encode(
            text: LingChatFormat.render(messages), addSpecialTokens: false)

        let cache = model.makeCache()
        let t0 = Date()
        var logits = model(MLXArray(prompt.map(Int32.init))[.newAxis], cache: cache)[0, -1]
        let logits32 = logits.asType(.float32)
        let order = argSort(-logits32)[..<5]
        eval(order, logits32)
        let prefill = Date().timeIntervalSince(t0)
        let top5 = order.asArray(Int32.self).map { id in
            (id: Int(id), logit: logits32[Int(id)].item(Float.self))
        }

        func topFive(_ logits: MLXArray) -> [(id: Int, logit: Float)] {
            let l32 = logits.asType(.float32)
            let order = argSort(-l32)[..<5]
            eval(order, l32)
            return order.asArray(Int32.self).map {
                (id: Int($0), logit: l32[Int($0)].item(Float.self))
            }
        }
        var stepTop5 = [[(id: Int, logit: Float)]]()
        var generated = [Int]()
        var token = argMax(logits, axis: -1)
        let t1 = Date()
        for step in 0 ..< count {
            if forced != nil { stepTop5.append(topFive(logits)) }
            eval(token)
            let id = token.item(Int.self)
            generated.append(id)
            if step == count - 1 { break }
            let feed = forced.map { $0[step] } ?? id
            logits = model(MLXArray([Int32(feed)]).reshaped(1, 1), cache: cache)[0, -1]
            token = argMax(logits, axis: -1)
        }
        let decode = Date().timeIntervalSince(t1)

        return LingProbeResult(
            promptTokenIds: prompt, top5: top5, greedyTokenIds: generated, stepTop5: stepTop5,
            text: tokenizer.decode(tokens: generated, skipSpecialTokens: false),
            jinjaMatchesFallback: prompt == fallback,
            prefillSeconds: prefill, decodeSeconds: decode)
    }
}

extension LingRuntime {
    /// Debug: per-layer attention output (`attn.N`), MLP output (`mlp.N`) and
    /// residual stream (`h.N`) for one full-prompt forward pass, plus final
    /// logits, saved as safetensors (float32). Mirrors parity/trace.py.
    public func trace(_ messages: [LingMessage], to url: URL) throws {
        guard let model = loadedModel, let tokenizer = loadedTokenizer,
            let template = loadedChatTemplate
        else { throw LingRuntimeError.notLoaded }
        let prompt = try LingChatFormat.encode(
            messages, tokenizer: tokenizer, chatTemplate: template)
        let inner = model.model
        let cache = model.makeCache()
        var out = [String: MLXArray]()
        var h = inner.wordEmbeddings(MLXArray(prompt.map(Int32.init))[.newAxis])
        out["embed"] = h
        let mask = createCausalMask(n: h.dim(1), offset: 0)
        // Layer-0 (KDA) internals.
        if let kda = inner.layers[0].attention as? BailingLinearAttention {
            let x = inner.layers[0].inputLayerNorm(h)
            let (B, L) = (x.dim(0), x.dim(1))
            let H = kda.numHeads
            let D = kda.headDim
            out["l0.norm"] = x
            let qp = kda.qProj(x)
            out["l0.q_proj"] = qp
            let (qc, _) = kda.qConv(qp, cache: nil)
            let (kc, _) = kda.kConv(kda.kProj(x), cache: nil)
            let (vc, _) = kda.vConv(kda.vProj(x), cache: nil)
            out["l0.q_conv"] = qc
            out["l0.k_conv"] = kc
            out["l0.v_conv"] = vc
            let f = kda.fProj(x).reshaped(B, L, H, D)
            out["l0.f"] = f
            let beta = kda.bProj(x)
            out["l0.b"] = beta
            let (o, st) = kda.recurrentKDA(
                q: qc.reshaped(B, L, H, D), k: kc.reshaped(B, L, H, D), v: vc.reshaped(B, L, H, D),
                g: f, beta: beta, state: nil)
            out["l0.kda"] = o
            out["l0.state"] = st
            let gate = kda.gProj(x).reshaped(B, L, H, D)
            let normed = kda.oNorm(o, gate: gate)
            out["l0.onorm"] = normed
            out["l0.oproj"] = kda.oProj(normed.reshaped(B, L, -1))
        }
        for (i, layer) in inner.layers.enumerated() {
            let normed = layer.inputLayerNorm(h)
            let r: MLXArray
            if layer.isGlobal {
                r = (layer.attention as! BailingMultiLatentAttention)(
                    normed, mask: mask, cache: cache[i])
            } else {
                r = (layer.attention as! BailingLinearAttention)(
                    normed, cache: cache[i] as? ArraysCache)
            }
            h = h + r
            let m = layer.mlp(layer.postAttentionLayerNorm(h))
            h = h + m
            out["attn.\(i)"] = r
            out["mlp.\(i)"] = m
            out["h.\(i)"] = h
            eval(h)
        }
        let logits = model.lmHead!(inner.norm(h))
        out["logits"] = logits
        try save(arrays: out.mapValues { $0.asType(.float32) }, url: url)
    }
}
