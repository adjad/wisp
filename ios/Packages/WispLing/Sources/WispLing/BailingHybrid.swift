// BailingHybrid.swift
//
// MLX Swift port of Ling 3.0 (`model_type: bailing_hybrid`,
// `BailingMoeV3ForCausalLM`) for single-sequence inference.
//
// Ported from the module oMLX actually serves Ling 3.0 with:
//   /Applications/oMLX.app/Contents/Resources/omlx/patches/bailing_hybrid/bailing_hybrid_model.py
// (vendored from scaryrawr/mlx-lm `ling-3.0-flash`, d719464f). mlx-lm's own
// `bailing_hybrid.py` is the older Ling 2.6 architecture (GLA linear attention
// with ALiBi-style slopes) and does not match these weights; oMLX replaces it.
//
// Architecture:
//   * every `layer_group_size`-th layer (and any tail layers) is Multi-Latent
//     Attention with a head-wise sigmoid output gate; the rest are KDA
//     (Kimi-style delta attention: short depthwise convs on q/k/v, L2-normed
//     q/k, per-channel decay gate, gated-delta recurrence, gated RMSNorm out)
//   * layer 0 (< first_k_dense_replace) has a dense SwiGLU MLP, the rest a
//     128-expert top-8 sigmoid router with grouped selection (noaux_tc) plus a
//     shared expert
//   * optional trained SwiGLU clamps (`expert_swiglu_limit_list`,
//     `share_expert_swiglu_limit_list`) are honoured.

import Foundation
import MLX
import MLXLLM
import MLXLMCommon
import MLXNN

// MARK: - Configuration

public struct BailingHybridConfiguration: Codable, Sendable {
    public var modelType: String
    public var hiddenSize: Int
    public var intermediateSize: Int
    public var moeIntermediateSize: Int
    public var numHiddenLayers: Int
    public var numAttentionHeads: Int
    public var numKeyValueHeads: Int
    public var numExperts: Int?
    public var numExpertsPerTok: Int
    public var numSharedExperts: Int
    public var nGroup: Int
    public var topkGroup: Int
    public var firstKDenseReplace: Int
    public var layerGroupSize: Int
    public var vocabSize: Int
    public var rmsNormEps: Float
    public var ropeTheta: Float
    public var maxPositionEmbeddings: Int
    public var routedScalingFactor: Float
    public var headDim: Int
    public var kvLoraRank: Int
    public var qkRopeHeadDim: Int
    public var qkNopeHeadDim: Int
    public var vHeadDim: Int
    public var qLoraRank: Int?
    public var ropeInterleave: Bool
    public var ropeScaling: [String: StringOrNumber]?
    public var useQkvBias: Bool
    public var useBias: Bool
    public var scoreFunction: String
    public var normTopkProb: Bool
    public var moeRouterEnableExpertBias: Bool
    public var tieWordEmbeddings: Bool
    public var gatedAttentionProjGranularityType: String?
    public var kdaSafeGate: Bool
    public var kdaLowerBound: Float?
    public var shortConvKernelSize: Int
    public var expertSwigluLimitList: [Float?]?
    public var shareExpertSwigluLimitList: [Float?]?
    public var eosTokenId: Int?

    enum CodingKeys: String, CodingKey {
        case modelType = "model_type"
        case hiddenSize = "hidden_size"
        case intermediateSize = "intermediate_size"
        case moeIntermediateSize = "moe_intermediate_size"
        case numHiddenLayers = "num_hidden_layers"
        case numAttentionHeads = "num_attention_heads"
        case numKeyValueHeads = "num_key_value_heads"
        case numExperts = "num_experts"
        case numExpertsPerTok = "num_experts_per_tok"
        case numSharedExperts = "num_shared_experts"
        case nGroup = "n_group"
        case topkGroup = "topk_group"
        case firstKDenseReplace = "first_k_dense_replace"
        case layerGroupSize = "layer_group_size"
        case vocabSize = "vocab_size"
        case rmsNormEps = "rms_norm_eps"
        case ropeTheta = "rope_theta"
        case maxPositionEmbeddings = "max_position_embeddings"
        case routedScalingFactor = "routed_scaling_factor"
        case headDim = "head_dim"
        case kvLoraRank = "kv_lora_rank"
        case qkRopeHeadDim = "qk_rope_head_dim"
        case qkNopeHeadDim = "qk_nope_head_dim"
        case vHeadDim = "v_head_dim"
        case qLoraRank = "q_lora_rank"
        case ropeInterleave = "rope_interleave"
        case ropeScaling = "rope_scaling"
        case useQkvBias = "use_qkv_bias"
        case useBias = "use_bias"
        case scoreFunction = "score_function"
        case normTopkProb = "norm_topk_prob"
        case moeRouterEnableExpertBias = "moe_router_enable_expert_bias"
        case tieWordEmbeddings = "tie_word_embeddings"
        case gatedAttentionProjGranularityType = "gated_attention_proj_granularity_type"
        case kdaSafeGate = "kda_safe_gate"
        case kdaLowerBound = "kda_lower_bound"
        case shortConvKernelSize = "short_conv_kernel_size"
        case expertSwigluLimitList = "expert_swiglu_limit_list"
        case shareExpertSwigluLimitList = "share_expert_swiglu_limit_list"
        case eosTokenId = "eos_token_id"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        modelType = try c.decodeIfPresent(String.self, forKey: .modelType) ?? "bailing_hybrid"
        hiddenSize = try c.decode(Int.self, forKey: .hiddenSize)
        intermediateSize = try c.decode(Int.self, forKey: .intermediateSize)
        moeIntermediateSize = try c.decode(Int.self, forKey: .moeIntermediateSize)
        numHiddenLayers = try c.decode(Int.self, forKey: .numHiddenLayers)
        numAttentionHeads = try c.decode(Int.self, forKey: .numAttentionHeads)
        numKeyValueHeads = try c.decode(Int.self, forKey: .numKeyValueHeads)
        numExperts = try c.decodeIfPresent(Int.self, forKey: .numExperts)
        numExpertsPerTok = try c.decode(Int.self, forKey: .numExpertsPerTok)
        numSharedExperts = try c.decodeIfPresent(Int.self, forKey: .numSharedExperts) ?? 0
        nGroup = try c.decodeIfPresent(Int.self, forKey: .nGroup) ?? 1
        topkGroup = try c.decodeIfPresent(Int.self, forKey: .topkGroup) ?? 1
        firstKDenseReplace = try c.decodeIfPresent(Int.self, forKey: .firstKDenseReplace) ?? 0
        layerGroupSize = try c.decode(Int.self, forKey: .layerGroupSize)
        vocabSize = try c.decode(Int.self, forKey: .vocabSize)
        rmsNormEps = try c.decode(Float.self, forKey: .rmsNormEps)
        ropeTheta = try c.decode(Float.self, forKey: .ropeTheta)
        maxPositionEmbeddings = try c.decode(Int.self, forKey: .maxPositionEmbeddings)
        routedScalingFactor = try c.decode(Float.self, forKey: .routedScalingFactor)
        headDim = try c.decode(Int.self, forKey: .headDim)
        kvLoraRank = try c.decode(Int.self, forKey: .kvLoraRank)
        qkRopeHeadDim = try c.decode(Int.self, forKey: .qkRopeHeadDim)
        qkNopeHeadDim = try c.decode(Int.self, forKey: .qkNopeHeadDim)
        vHeadDim = try c.decode(Int.self, forKey: .vHeadDim)
        qLoraRank = try c.decodeIfPresent(Int.self, forKey: .qLoraRank)
        ropeInterleave = try c.decodeIfPresent(Bool.self, forKey: .ropeInterleave) ?? true
        ropeScaling = try c.decodeIfPresent([String: StringOrNumber].self, forKey: .ropeScaling)
        useQkvBias = try c.decodeIfPresent(Bool.self, forKey: .useQkvBias) ?? false
        useBias = try c.decodeIfPresent(Bool.self, forKey: .useBias) ?? false
        scoreFunction = try c.decodeIfPresent(String.self, forKey: .scoreFunction) ?? "sigmoid"
        normTopkProb = try c.decodeIfPresent(Bool.self, forKey: .normTopkProb) ?? true
        moeRouterEnableExpertBias =
            try c.decodeIfPresent(Bool.self, forKey: .moeRouterEnableExpertBias) ?? true
        tieWordEmbeddings = try c.decodeIfPresent(Bool.self, forKey: .tieWordEmbeddings) ?? false
        gatedAttentionProjGranularityType = try c.decodeIfPresent(
            String.self, forKey: .gatedAttentionProjGranularityType)
        kdaSafeGate = try c.decodeIfPresent(Bool.self, forKey: .kdaSafeGate) ?? false
        kdaLowerBound = try c.decodeIfPresent(Float.self, forKey: .kdaLowerBound)
        shortConvKernelSize = try c.decodeIfPresent(Int.self, forKey: .shortConvKernelSize) ?? 4
        expertSwigluLimitList = try c.decodeIfPresent([Float?].self, forKey: .expertSwigluLimitList)
        shareExpertSwigluLimitList = try c.decodeIfPresent(
            [Float?].self, forKey: .shareExpertSwigluLimitList)
        eosTokenId = try? c.decodeIfPresent(Int.self, forKey: .eosTokenId)
    }

    /// Python `DecoderLayer.is_global`.
    public func isGlobal(layer i: Int) -> Bool {
        (i + 1) % layerGroupSize == 0
            || i >= (numHiddenLayers / layerGroupSize) * layerGroupSize
    }
}

/// Per-layer SwiGLU clamp, or nil when absent or zero (Python `layer_swiglu_limit`).
func layerSwigluLimit(_ list: [Float?]?, _ layer: Int) -> Float? {
    guard let list, layer < list.count, let limit = list[layer], limit != 0 else { return nil }
    return limit
}

/// `silu(gate).clamp(max=limit) * x.clamp(-limit, limit)` (Ling's trained clamp).
func clampedSwiGLU(_ gate: MLXArray, _ x: MLXArray, limit: Float) -> MLXArray {
    minimum(silu(gate), limit) * clip(x, min: -limit, max: limit)
}

/// Sigmoid with the same rounding as MLX's precompiled (metallib) `Sigmoid`
/// kernel: `y = 1 / (1 + exp(|x|))`, `x < 0 ? y : 1 - y`, each step rounded in
/// the input dtype. mlx-swift builds MLX in JIT mode, where the stock sigmoid
/// kernel is compiled at run time and its `metal::exp` rounds differently from
/// the ahead-of-time metallib Python MLX ships (1 ulp on ~1/3 of fp32 inputs).
/// The element-wise ops used here are bit-identical between the two builds.
public func lingSigmoid(_ x: MLXArray) -> MLXArray {
    let y = 1 / (1 + exp(abs(x)))
    return MLX.where(x .< 0, y, 1 - y)
}

/// `mx.finfo(dtype).min` as a scalar of that dtype.
func finfoMin(_ dtype: DType) -> MLXArray {
    switch dtype {
    case .float16: return MLXArray(Float(-65504.0)).asType(.float16)
    // bfloat16 max = (2 - 2^-7) * 2^127, exactly representable in Float32.
    case .bfloat16: return MLXArray(Float(-3.3895313892515355e38)).asType(.bfloat16)
    default: return MLXArray(-Float.greatestFiniteMagnitude).asType(dtype)
    }
}

// MARK: - MultiLinear (per-head projection used by MLA)

/// Python `mlx_lm.models.mla.MultiLinear`: weight `[heads, out, in]`.
class LingMultiLinear: Module, Quantizable {
    @ParameterInfo(key: "weight") var weight: MLXArray

    init(inputDims: Int, outputDims: Int, numHeads: Int) {
        let scale = sqrt(1.0 / Float(inputDims))
        _weight.wrappedValue = MLXRandom.uniform(
            low: -scale, high: scale, [numHeads, outputDims, inputDims])
        super.init()
    }

    init(weight: MLXArray) {
        _weight.wrappedValue = weight
        super.init()
    }

    func callAsFunction(_ x: MLXArray, transpose: Bool = true) -> MLXArray {
        transpose ? matmul(x, weight.swappedAxes(-1, -2)) : matmul(x, weight)
    }

    func toQuantized(groupSize: Int, bits: Int, mode: QuantizationMode) -> Module {
        LingQuantizedMultiLinear(weight: weight, groupSize: groupSize, bits: bits, mode: mode)
    }
}

/// Python `mlx_lm.models.mla.QuantizedMultiLinear`.
final class LingQuantizedMultiLinear: LingMultiLinear, Quantized {
    let groupSize: Int
    let bits: Int
    let mode: QuantizationMode

    @ParameterInfo(key: "scales") var scales: MLXArray
    @ParameterInfo(key: "biases") var biases: MLXArray?

    init(weight: MLXArray, groupSize: Int, bits: Int, mode: QuantizationMode) {
        self.groupSize = groupSize
        self.bits = bits
        self.mode = mode
        let (wq, scales, biases) = MLX.quantized(
            weight, groupSize: groupSize, bits: bits, mode: mode)
        _scales.wrappedValue = scales
        _biases.wrappedValue = biases
        super.init(weight: wq)
        self.freeze()
    }

    override func callAsFunction(_ x: MLXArray, transpose: Bool = true) -> MLXArray {
        quantizedMM(
            x, weight, scales: scales, biases: biases, transpose: transpose,
            groupSize: groupSize, bits: bits, mode: mode)
    }
}

// MARK: - MLP

final class BailingMLP: Module, UnaryLayer {
    @ModuleInfo(key: "gate_proj") var gateProj: Linear
    @ModuleInfo(key: "up_proj") var upProj: Linear
    @ModuleInfo(key: "down_proj") var downProj: Linear
    let swigluLimit: Float?

    init(_ args: BailingHybridConfiguration, intermediateSize: Int? = nil, swigluLimit: Float? = nil)
    {
        let dim = intermediateSize ?? args.intermediateSize
        _gateProj.wrappedValue = Linear(args.hiddenSize, dim, bias: args.useBias)
        _upProj.wrappedValue = Linear(args.hiddenSize, dim, bias: args.useBias)
        _downProj.wrappedValue = Linear(dim, args.hiddenSize, bias: args.useBias)
        self.swigluLimit = swigluLimit
        super.init()
    }

    func callAsFunction(_ x: MLXArray) -> MLXArray {
        let gate = gateProj(x)
        let up = upProj(x)
        if let swigluLimit {
            return downProj(clampedSwiGLU(gate, up, limit: swigluLimit))
        }
        return downProj(compiledSiluProduct(gate, up))
    }
}

// MARK: - Multi-Latent Attention (global layers)

final class BailingMultiLatentAttention: Module {
    let numHeads: Int
    let qLoraRank: Int?
    let qkRopeHeadDim: Int
    let kvLoraRank: Int
    let vHeadDim: Int
    let qkNopeHeadDim: Int
    let qkHeadDim: Int
    let gateGranularity: String?
    var scale: Float

    @ModuleInfo(key: "q_proj") var qProj: Linear?
    @ModuleInfo(key: "q_a_proj") var qAProj: Linear?
    @ModuleInfo(key: "q_a_layernorm") var qALayerNorm: RMSNorm?
    @ModuleInfo(key: "q_b_proj") var qBProj: Linear?
    @ModuleInfo(key: "kv_a_proj_with_mqa") var kvAProjWithMqa: Linear
    @ModuleInfo(key: "kv_a_layernorm") var kvALayerNorm: RMSNorm
    @ModuleInfo(key: "embed_q") var embedQ: LingMultiLinear
    @ModuleInfo(key: "unembed_out") var unembedOut: LingMultiLinear
    @ModuleInfo(key: "dense") var dense: Linear
    @ModuleInfo(key: "g_proj") var gProj: Linear?

    let rope: RoPELayer

    init(_ args: BailingHybridConfiguration) {
        numHeads = args.numAttentionHeads
        qLoraRank = args.qLoraRank
        qkRopeHeadDim = args.qkRopeHeadDim
        kvLoraRank = args.kvLoraRank
        vHeadDim = args.vHeadDim
        qkNopeHeadDim = args.qkNopeHeadDim
        qkHeadDim = args.qkNopeHeadDim + args.qkRopeHeadDim
        gateGranularity = args.gatedAttentionProjGranularityType
        scale = Float(pow(Double(qkHeadDim), -0.5))

        if let qLoraRank {
            _qAProj.wrappedValue = Linear(args.hiddenSize, qLoraRank, bias: args.useQkvBias)
            _qALayerNorm.wrappedValue = RMSNorm(dimensions: qLoraRank, eps: args.rmsNormEps)
            _qBProj.wrappedValue = Linear(qLoraRank, numHeads * qkHeadDim, bias: false)
        } else {
            _qProj.wrappedValue = Linear(args.hiddenSize, numHeads * qkHeadDim, bias: false)
        }
        _kvAProjWithMqa.wrappedValue = Linear(
            args.hiddenSize, kvLoraRank + qkRopeHeadDim, bias: args.useQkvBias)
        _kvALayerNorm.wrappedValue = RMSNorm(dimensions: kvLoraRank, eps: args.rmsNormEps)
        _embedQ.wrappedValue = LingMultiLinear(
            inputDims: qkNopeHeadDim, outputDims: kvLoraRank, numHeads: numHeads)
        _unembedOut.wrappedValue = LingMultiLinear(
            inputDims: kvLoraRank, outputDims: vHeadDim, numHeads: numHeads)
        _dense.wrappedValue = Linear(numHeads * vHeadDim, args.hiddenSize, bias: args.useQkvBias)
        switch args.gatedAttentionProjGranularityType {
        case "head_wise":
            _gProj.wrappedValue = Linear(args.hiddenSize, numHeads, bias: false)
        case "element_wise":
            _gProj.wrappedValue = Linear(args.hiddenSize, numHeads * vHeadDim, bias: false)
        default:
            break
        }

        if let rs = args.ropeScaling,
            let mscaleAllDim = rs["mscale_all_dim"]?.asFloat(),
            let factor = rs["factor"]?.asFloat(),
            mscaleAllDim != 0, factor > 1
        {
            let s = 0.1 * mscaleAllDim * log(factor) + 1.0
            scale = scale * s * s
        }

        rope = initializeRope(
            dims: qkRopeHeadDim, base: args.ropeTheta, traditional: args.ropeInterleave,
            scalingConfig: args.ropeScaling, maxPositionEmbeddings: args.maxPositionEmbeddings)
        super.init()
    }

    func callAsFunction(_ x: MLXArray, mask: MLXArray?, cache: KVCache?) -> MLXArray {
        let (B, L) = (x.dim(0), x.dim(1))

        var q: MLXArray
        if let qProj {
            q = qProj(x)
        } else {
            q = qBProj!(qALayerNorm!(qAProj!(x)))
        }
        q = q.reshaped(B, L, numHeads, qkHeadDim).transposed(0, 2, 1, 3)
        let qParts = split(q, indices: [qkNopeHeadDim], axis: -1)
        var qNope = qParts[0]
        var qPe = qParts[1]

        let kvParts = split(kvAProjWithMqa(x), indices: [kvLoraRank], axis: -1)
        var kPe = kvParts[1].reshaped(B, L, 1, qkRopeHeadDim).transposed(0, 2, 1, 3)
        var kvLatent = kvALayerNorm(kvParts[0])

        let offset = cache?.offset ?? 0
        qPe = rope(qPe, offset: offset)
        kPe = rope(kPe, offset: offset)

        kvLatent = expandedDimensions(kvLatent, axis: 1)
        if let cache {
            (kvLatent, kPe) = cache.update(keys: kvLatent, values: kPe)
        }

        var peScores = matmul(qPe * scale, kPe.swappedAxes(-1, -2))
        if let mask {
            peScores = MLX.where(mask, peScores, finfoMin(peScores.dtype))
        }

        let keys: MLXArray
        let values: MLXArray
        if L == 1 {
            qNope = embedQ(qNope)
            keys = kvLatent
            values = kvLatent
        } else {
            keys = embedQ(kvLatent, transpose: false)
            values = unembedOut(kvLatent)
        }

        var output = MLXFast.scaledDotProductAttention(
            queries: qNope, keys: keys, values: values, scale: scale, mask: .array(peScores))
        if L == 1 {
            output = unembedOut(output)
        }

        if let gProj {
            let gate = lingSigmoid(gProj(x).asType(.float32)).asType(output.dtype)
            if gateGranularity == "head_wise" {
                output = output * expandedDimensions(gate.transposed(0, 2, 1), axis: -1)
            } else {
                output =
                    output * gate.reshaped(B, L, numHeads, vHeadDim).transposed(0, 2, 1, 3)
            }
        }

        output = output.transposed(0, 2, 1, 3).reshaped(B, L, -1)
        return dense(output)
    }
}

// MARK: - KDA linear attention

/// Python `DepthwiseConv1d`: weight `[channels, 1, kernel]`, silu output,
/// cache `[B, channels, kernel]` holding the last `kernel` inputs.
final class BailingDepthwiseConv1d: Module {
    @ParameterInfo(key: "weight") var weight: MLXArray

    init(channels: Int, kernelSize: Int) {
        let scale = sqrt(1.0 / Float(channels))
        _weight.wrappedValue = MLXRandom.uniform(
            low: -scale, high: scale, [channels, 1, kernelSize])
        super.init()
    }

    func callAsFunction(_ x: MLXArray, cache: MLXArray?) -> (MLXArray, MLXArray) {
        let (B, _, C) = (x.dim(0), x.dim(1), x.dim(2))
        let K = weight.dim(-1)
        let cache = cache ?? MLXArray.zeros([B, C, K], dtype: x.dtype)

        let history = cache[0..., 0..., (1 - K)...].transposed(0, 2, 1)
        let convInput = concatenated([history, x], axis: 1)
        let w = weight.movedAxis(source: 2, destination: 1).asType(x.dtype)
        let output = silu(convGeneral(convInput, w, groups: C))

        let cacheInput = concatenated([cache, x.transposed(0, 2, 1)], axis: 2)
        let nextCache = contiguous(cacheInput[0..., 0..., (-K)...])
        return (output, nextCache)
    }
}

final class BailingGatedRMSNorm: Module {
    @ParameterInfo(key: "weight") var weight: MLXArray
    let eps: Float

    init(dimensions: Int, eps: Float) {
        _weight.wrappedValue = MLXArray.ones([dimensions])
        self.eps = eps
        super.init()
    }

    func callAsFunction(_ x: MLXArray, gate: MLXArray) -> MLXArray {
        let y = MLXFast.rmsNorm(x, weight: weight, eps: eps)
        return y * lingSigmoid(gate.asType(.float32)).asType(y.dtype)
    }
}

final class BailingLinearAttention: Module {
    let numHeads: Int
    let headDim: Int
    let safeGate: Bool
    let lowerBound: Float?

    @ModuleInfo(key: "q_proj") var qProj: Linear
    @ModuleInfo(key: "k_proj") var kProj: Linear
    @ModuleInfo(key: "v_proj") var vProj: Linear
    @ModuleInfo(key: "q_conv1d") var qConv: BailingDepthwiseConv1d
    @ModuleInfo(key: "k_conv1d") var kConv: BailingDepthwiseConv1d
    @ModuleInfo(key: "v_conv1d") var vConv: BailingDepthwiseConv1d
    @ParameterInfo(key: "A_log") var aLog: MLXArray
    @ParameterInfo(key: "dt_bias") var dtBias: MLXArray
    @ModuleInfo(key: "f_proj") var fProj: Linear
    @ModuleInfo(key: "b_proj") var bProj: Linear
    @ModuleInfo(key: "g_proj") var gProj: Linear
    @ModuleInfo(key: "o_norm") var oNorm: BailingGatedRMSNorm
    @ModuleInfo(key: "o_proj") var oProj: Linear

    init(_ args: BailingHybridConfiguration) {
        numHeads = args.numAttentionHeads
        headDim = args.headDim
        safeGate = args.kdaSafeGate
        lowerBound = args.kdaLowerBound
        let p = numHeads * headDim
        _qProj.wrappedValue = Linear(args.hiddenSize, p, bias: false)
        _kProj.wrappedValue = Linear(args.hiddenSize, p, bias: false)
        _vProj.wrappedValue = Linear(args.hiddenSize, p, bias: false)
        _qConv.wrappedValue = BailingDepthwiseConv1d(channels: p, kernelSize: args.shortConvKernelSize)
        _kConv.wrappedValue = BailingDepthwiseConv1d(channels: p, kernelSize: args.shortConvKernelSize)
        _vConv.wrappedValue = BailingDepthwiseConv1d(channels: p, kernelSize: args.shortConvKernelSize)
        _aLog.wrappedValue = MLXArray.zeros([numHeads], dtype: .float32)
        _dtBias.wrappedValue = MLXArray.zeros([p], dtype: .float32)
        _fProj.wrappedValue = Linear(args.hiddenSize, p, bias: false)
        _bProj.wrappedValue = Linear(args.hiddenSize, numHeads, bias: false)
        _gProj.wrappedValue = Linear(args.hiddenSize, p, bias: false)
        _oNorm.wrappedValue = BailingGatedRMSNorm(dimensions: headDim, eps: args.rmsNormEps)
        _oProj.wrappedValue = Linear(p, args.hiddenSize, bias: false)
        super.init()
    }

    /// Cache slots: 0 = recurrent state `[B, H, Dv, Dk]` (fp32, kernel layout),
    /// 1/2/3 = q/k/v conv history.
    func callAsFunction(_ x: MLXArray, cache: ArraysCache?) -> MLXArray {
        let (B, L) = (x.dim(0), x.dim(1))

        let (qc, convQ) = qConv(qProj(x), cache: cache?[1])
        let (kc, convK) = kConv(kProj(x), cache: cache?[2])
        let (vc, convV) = vConv(vProj(x), cache: cache?[3])
        let q = qc.reshaped(B, L, numHeads, headDim)
        let k = kc.reshaped(B, L, numHeads, headDim)
        let v = vc.reshaped(B, L, numHeads, headDim)
        let f = fProj(x).reshaped(B, L, numHeads, headDim)
        let beta = bProj(x)

        let (output, state) = recurrentKDA(
            q: q, k: k, v: v, g: f, beta: beta, state: cache?[0])
        if let cache {
            cache[0] = state
            cache[1] = convQ
            cache[2] = convK
            cache[3] = convV
            cache.offset += L
        }

        let gate = gProj(x).reshaped(B, L, numHeads, headDim)
        let normed = oNorm(output, gate: gate)
        return oProj(normed.reshaped(B, L, -1))
    }

    /// Python `recurrent_kda` (state kept in the fused kernel's `[Dv, Dk]` layout).
    func recurrentKDA(
        q: MLXArray, k: MLXArray, v: MLXArray, g: MLXArray, beta: MLXArray, state: MLXArray?
    ) -> (MLXArray, MLXArray) {
        let (B, H, D) = (q.dim(0), q.dim(2), q.dim(3))
        let state = state ?? MLXArray.zeros([B, H, D, D], dtype: .float32)

        // normalize_qk: l2-normalize via rms_norm (eps folded) and fold the
        // head_dim**-0.5 readout scale into q.
        let invScaleD = pow(Double(D), -0.5)
        let invScale = Float(invScaleD)
        let rmsEps = Float(1e-6 * invScaleD * invScaleD)
        // Python passes weight=None; a ones weight in the input dtype is bit-identical.
        let ones = MLXArray.ones([D], dtype: q.dtype)
        let qn = Float(invScaleD * invScaleD) * MLXFast.rmsNorm(q, weight: ones, eps: rmsEps)
        let kn = invScale * MLXFast.rmsNorm(k, weight: ones, eps: rmsEps)

        let gateInput =
            g.asType(.float32) + dtBias.reshaped(1, 1, H, D).asType(.float32)
        let aLog = self.aLog.reshaped(1, 1, H, 1).asType(.float32)
        let logDecay: MLXArray
        if safeGate, let lowerBound {
            logDecay = lowerBound * lingSigmoid(exp(aLog) * gateInput)
        } else {
            logDecay = -exp(aLog) * softplus(gateInput)
        }
        let decay = exp(logDecay)
        let betaS = lingSigmoid(beta)

        if D % 32 == 0, let kernel = KDAKernel.shared.kernel {
            return kdaKernel(kernel, q: qn, k: kn, v: v, g: decay, beta: betaS, state: state)
        }
        return kdaOps(q: qn, k: kn, v: v, g: decay, beta: betaS, state: state)
    }
}

// MARK: - Gated-delta recurrence with per-channel (vector) decay

/// Same Metal source as Python mlx-lm's `_make_gated_delta_kernel(vectorized=True)`
/// (plain simd_sum reductions, no Kahan compensation) so the recurrence rounds
/// identically to the reference.
private final class KDAKernel: Sendable {
    static let shared = KDAKernel()
    let kernel: MLXFast.MLXFastKernel?

    private init() {
        let source = """
                auto n = thread_position_in_grid.z;
                auto b_idx = n / Hv;
                auto hv_idx = n % Hv;
                auto hk_idx = hv_idx / (Hv / Hk);
                constexpr int n_per_t = Dk / 32;

                // q, k: [B, T, Hk, Dk]
                auto q_ = q + b_idx * T * Hk * Dk + hk_idx * Dk;
                auto k_ = k + b_idx * T * Hk * Dk + hk_idx * Dk;

                // v, y: [B, T, Hv, Dv]
                auto v_ = v + b_idx * T * Hv * Dv + hv_idx * Dv;
                y += b_idx * T * Hv * Dv + hv_idx * Dv;

                auto dk_idx = thread_position_in_threadgroup.x;
                auto dv_idx = thread_position_in_grid.y;

                // state_in, state_out: [B, Hv, Dv, Dk]
                auto i_state = state_in + (n * Dv + dv_idx) * Dk;
                auto o_state = state_out + (n * Dv + dv_idx) * Dk;

                float state[n_per_t];
                for (int i = 0; i < n_per_t; ++i) {
                  auto s_idx = n_per_t * dk_idx + i;
                  state[i] = static_cast<float>(i_state[s_idx]);
                }

                // g: [B, T, Hv, Dk]
                auto g_ = g + (b_idx * T * Hv + hv_idx) * Dk;
                auto beta_ = beta + b_idx * T * Hv;

                for (int t = 0; t < T; ++t) {
                  if (true) {
                    float kv_mem = 0.0f;
                    for (int i = 0; i < n_per_t; ++i) {
                      auto s_idx = n_per_t * dk_idx + i;
                      state[i] = state[i] * g_[s_idx];
                      kv_mem += state[i] * k_[s_idx];
                    }
                    kv_mem = simd_sum(kv_mem);

                    auto delta = (v_[dv_idx] - kv_mem) * beta_[hv_idx];

                    float out = 0.0f;
                    for (int i = 0; i < n_per_t; ++i) {
                      auto s_idx = n_per_t * dk_idx + i;
                      state[i] = state[i] + k_[s_idx] * delta;
                      out += state[i] * q_[s_idx];
                    }
                    out = simd_sum(out);
                    if (thread_index_in_simdgroup == 0) {
                      y[dv_idx] = static_cast<InT>(out);
                    }
                  } else {
                    y[dv_idx] = static_cast<InT>(0);
                  }
                  // Increment data pointers to next time step
                  q_ += Hk * Dk;
                  k_ += Hk * Dk;
                  v_ += Hv * Dv;
                  y += Hv * Dv;
                  g_ += Hv * Dk;
                  beta_ += Hv;
                }
                for (int i = 0; i < n_per_t; ++i) {
                  auto s_idx = n_per_t * dk_idx + i;
                  o_state[s_idx] = static_cast<StT>(state[i]);
                }
            """
        kernel = MLXFast.metalKernel(
            name: "gated_delta_step_vec",
            inputNames: ["q", "k", "v", "g", "beta", "state_in", "T"],
            outputNames: ["y", "state_out"],
            source: source)
    }
}

private func kdaKernel(
    _ kernel: MLXFast.MLXFastKernel,
    q: MLXArray, k: MLXArray, v: MLXArray, g: MLXArray, beta: MLXArray, state: MLXArray
) -> (MLXArray, MLXArray) {
    let (B, T, Hk, Dk) = (k.dim(0), k.dim(1), k.dim(2), k.dim(3))
    let (Hv, Dv) = (v.dim(2), v.dim(3))
    let out = kernel(
        [q, k, v, g, beta, state, MLXArray(Int32(T))],
        template: [
            ("InT", q.dtype), ("StT", state.dtype),
            ("Dk", Dk), ("Dv", Dv), ("Hk", Hk), ("Hv", Hv),
        ],
        grid: (32, Dv, B * Hv),
        threadGroup: (32, 4, 1),
        outputShapes: [[B, T, Hv, Dv], state.shape],
        outputDTypes: [q.dtype, state.dtype])
    return (out[0], out[1])
}

/// Ops fallback (Python `gated_delta_ops`), used only when Metal is unavailable
/// or the head dim is not a multiple of 32.
private func kdaOps(
    q: MLXArray, k: MLXArray, v: MLXArray, g: MLXArray, beta: MLXArray, state: MLXArray
) -> (MLXArray, MLXArray) {
    var state = state
    var ys = [MLXArray]()
    for t in 0 ..< q.dim(1) {
        let qt = q[0..., t]
        let kt = k[0..., t]
        let vt = v[0..., t]
        let gt = g[0..., t]
        let bt = beta[0..., t]
        state = state * expandedDimensions(gt, axis: -2)
        let kvMem = (state * expandedDimensions(kt, axis: -2)).sum(axis: -1)
        let delta = (vt - kvMem) * expandedDimensions(bt, axis: -1)
        state = state + expandedDimensions(kt, axis: -2) * expandedDimensions(delta, axis: -1)
        ys.append((state * expandedDimensions(qt, axis: -2)).sum(axis: -1).asType(q.dtype))
    }
    return (stacked(ys, axis: 1), state)
}

// MARK: - MoE

final class BailingGate: Module {
    let topK: Int
    let nGroup: Int
    let topkGroup: Int
    let normTopkProb: Bool
    let routedScalingFactor: Float
    let scoreFunction: String

    @ModuleInfo(key: "gate_proj") var gateProj: Linear
    @ParameterInfo(key: "expert_bias") var expertBias: MLXArray?

    init(_ args: BailingHybridConfiguration) {
        topK = args.numExpertsPerTok
        nGroup = args.nGroup
        topkGroup = args.topkGroup
        normTopkProb = args.normTopkProb
        routedScalingFactor = args.routedScalingFactor
        scoreFunction = args.scoreFunction
        let numExperts = args.numExperts ?? 0
        _gateProj.wrappedValue = Linear(args.hiddenSize, numExperts, bias: false)
        if args.moeRouterEnableExpertBias {
            _expertBias.wrappedValue = MLXArray.zeros([numExperts])
        }
        super.init()
    }

    /// Python `group_expert_select`.
    func callAsFunction(_ x: MLXArray) -> (MLXArray, MLXArray) {
        let gates = gateProj(x)
        let inType = gates.dtype
        var scores =
            scoreFunction == "sigmoid"
            ? lingSigmoid(gates.asType(.float32)) : softmax(gates.asType(.float32), axis: -1)
        let origScores = scores
        if let expertBias {
            scores = scores + expertBias
        }
        if nGroup > 1 {
            scores = unflatten(scores, axis: -1, shape: [nGroup, -1])
            let groupScores = top(scores, k: 2, axis: -1).sum(axis: -1, keepDims: true)
            let k = nGroup - topkGroup
            let groupIdx = argPartition(groupScores, kth: k - 1, axis: -2)[.ellipsis, ..<k, 0...]
            scores = putAlong(
                scores, stopGradient(groupIdx), values: MLXArray(Float(0)), axis: -2)
            scores = flattened(scores, start: -2, end: -1)
        }
        let inds = argPartition(-scores, kth: topK - 1, axis: -1)[.ellipsis, ..<topK]
        var selected = takeAlong(origScores, inds, axis: -1)
        if topK > 1 && normTopkProb {
            selected = selected / (selected.sum(axis: -1, keepDims: true) + Float(1e-20))
        }
        selected = selected * routedScalingFactor
        return (inds, selected.asType(inType))
    }
}

/// Python `SwitchGLU` (sorted gather above 64 assignments) with Ling's optional clamp.
final class BailingSwitchGLU: Module {
    @ModuleInfo(key: "gate_proj") var gateProj: SwitchLinear
    @ModuleInfo(key: "up_proj") var upProj: SwitchLinear
    @ModuleInfo(key: "down_proj") var downProj: SwitchLinear
    let swigluLimit: Float?

    init(inputDims: Int, hiddenDims: Int, numExperts: Int, bias: Bool, swigluLimit: Float?) {
        _gateProj.wrappedValue = SwitchLinear(
            inputDims: inputDims, outputDims: hiddenDims, numExperts: numExperts, bias: bias)
        _upProj.wrappedValue = SwitchLinear(
            inputDims: inputDims, outputDims: hiddenDims, numExperts: numExperts, bias: bias)
        _downProj.wrappedValue = SwitchLinear(
            inputDims: hiddenDims, outputDims: inputDims, numExperts: numExperts, bias: bias)
        self.swigluLimit = swigluLimit
        super.init()
    }

    func callAsFunction(_ x: MLXArray, _ indices: MLXArray) -> MLXArray {
        var x = expandedDimensions(x, axes: [-2, -3])
        let doSort = indices.size >= 64
        var idx = indices
        var inverseOrder = MLXArray()
        if doSort {
            (x, idx, inverseOrder) = gatherSort(x: x, indices: indices)
        }
        let xUp = upProj(x, idx, sortedIndices: doSort)
        let xGate = gateProj(x, idx, sortedIndices: doSort)
        let activated =
            if let swigluLimit {
                clampedSwiGLU(xGate, xUp, limit: swigluLimit)
            } else {
                compiledSiluProduct(xGate, xUp)
            }
        x = downProj(activated, idx, sortedIndices: doSort)
        if doSort {
            x = scatterUnsort(x: x, invOrder: inverseOrder, shape: indices.shape)
        }
        return x.squeezed(axis: -2)
    }
}

final class BailingSparseMoeBlock: Module, UnaryLayer {
    @ModuleInfo(key: "switch_mlp") var switchMLP: BailingSwitchGLU
    @ModuleInfo(key: "gate") var gate: BailingGate
    @ModuleInfo(key: "shared_experts") var sharedExperts: BailingMLP?

    init(_ args: BailingHybridConfiguration, layer: Int) {
        _switchMLP.wrappedValue = BailingSwitchGLU(
            inputDims: args.hiddenSize, hiddenDims: args.moeIntermediateSize,
            numExperts: args.numExperts ?? 0, bias: args.useBias,
            swigluLimit: layerSwigluLimit(args.expertSwigluLimitList, layer))
        _gate.wrappedValue = BailingGate(args)
        if args.numSharedExperts > 0 {
            _sharedExperts.wrappedValue = BailingMLP(
                args, intermediateSize: args.moeIntermediateSize * args.numSharedExperts,
                swigluLimit: layerSwigluLimit(args.shareExpertSwigluLimitList, layer))
        }
        super.init()
    }

    func callAsFunction(_ x: MLXArray) -> MLXArray {
        let (inds, weights) = gate(x)
        var out = switchMLP(x, inds)
        out = (out * expandedDimensions(weights, axis: -1)).sum(axis: -2)
        if let sharedExperts {
            out = out + sharedExperts(x)
        }
        return out
    }
}

// MARK: - Decoder

final class BailingDecoderLayer: Module {
    let isGlobal: Bool

    @ModuleInfo(key: "attention") var attention: Module
    @ModuleInfo(key: "mlp") var mlp: UnaryLayer
    @ModuleInfo(key: "input_layernorm") var inputLayerNorm: RMSNorm
    @ModuleInfo(key: "post_attention_layernorm") var postAttentionLayerNorm: RMSNorm

    init(_ args: BailingHybridConfiguration, layer: Int) {
        isGlobal = args.isGlobal(layer: layer)
        _attention.wrappedValue =
            isGlobal ? BailingMultiLatentAttention(args) : BailingLinearAttention(args)
        if args.numExperts != nil && layer >= args.firstKDenseReplace {
            _mlp.wrappedValue = BailingSparseMoeBlock(args, layer: layer)
        } else {
            _mlp.wrappedValue = BailingMLP(args)
        }
        _inputLayerNorm.wrappedValue = RMSNorm(dimensions: args.hiddenSize, eps: args.rmsNormEps)
        _postAttentionLayerNorm.wrappedValue = RMSNorm(
            dimensions: args.hiddenSize, eps: args.rmsNormEps)
        super.init()
    }

    func callAsFunction(_ x: MLXArray, attentionMask: MLXArray?, cache: KVCache?) -> MLXArray {
        let normed = inputLayerNorm(x)
        let r: MLXArray
        if isGlobal {
            r = (attention as! BailingMultiLatentAttention)(normed, mask: attentionMask, cache: cache)
        } else {
            r = (attention as! BailingLinearAttention)(normed, cache: cache as? ArraysCache)
        }
        let h = x + r
        return h + mlp(postAttentionLayerNorm(h))
    }
}

public final class BailingHybridModelInner: Module {
    @ModuleInfo(key: "word_embeddings") var wordEmbeddings: Embedding
    let layers: [BailingDecoderLayer]
    @ModuleInfo(key: "norm") var norm: RMSNorm
    let attentionIndex: Int

    init(_ args: BailingHybridConfiguration) {
        _wordEmbeddings.wrappedValue = Embedding(
            embeddingCount: args.vocabSize, dimensions: args.hiddenSize)
        layers = (0 ..< args.numHiddenLayers).map { BailingDecoderLayer(args, layer: $0) }
        _norm.wrappedValue = RMSNorm(dimensions: args.hiddenSize, eps: args.rmsNormEps)
        attentionIndex = layers.firstIndex(where: { $0.isGlobal }) ?? 0
        super.init()
    }

    func callAsFunction(_ inputs: MLXArray, cache: [KVCache]?) -> MLXArray {
        var h = wordEmbeddings(inputs)
        let n = h.dim(1)
        // Causal mask as a boolean array (Python create_attention_mask(...,
        // return_array=True)); single-token steps need none. KDA layers get no
        // mask for an unpadded single sequence (Python create_ssm_mask -> None).
        var attentionMask: MLXArray? = nil
        if n > 1 {
            let offset = cache?[attentionIndex].offset ?? 0
            attentionMask = createCausalMask(n: n, offset: offset)
        }
        for (i, layer) in layers.enumerated() {
            h = layer(h, attentionMask: attentionMask, cache: cache?[i])
        }
        return norm(h)
    }
}

public final class BailingHybridModel: Module, LLMModel {
    public let configuration: BailingHybridConfiguration
    public let vocabularySize: Int

    @ModuleInfo(key: "model") var model: BailingHybridModelInner
    @ModuleInfo(key: "lm_head") var lmHead: Linear?

    public init(_ args: BailingHybridConfiguration) {
        configuration = args
        vocabularySize = args.vocabSize
        _model.wrappedValue = BailingHybridModelInner(args)
        if !args.tieWordEmbeddings {
            _lmHead.wrappedValue = Linear(args.hiddenSize, args.vocabSize, bias: false)
        }
        super.init()
    }

    public func callAsFunction(_ inputs: MLXArray, cache: [KVCache]?) -> MLXArray {
        let out = model(inputs, cache: cache)
        if let lmHead {
            return lmHead(out)
        }
        return model.wordEmbeddings.asLinear(out)
    }

    /// KVCacheSimple for MLA layers (stores the compressed latent as "keys" and
    /// the roped k_pe as "values"), a 4-slot ArraysCache for KDA layers.
    public func newCache(parameters: GenerateParameters?) throws -> [KVCache] {
        makeCache()
    }

    public func makeCache() -> [KVCache] {
        model.layers.map { $0.isGlobal ? KVCacheSimple() : ArraysCache(size: 4) }
    }

    public func sanitize(weights: [String: MLXArray]) -> [String: MLXArray] {
        let args = configuration
        let n = args.numHiddenLayers
        var weights = weights.filter { key, _ in
            let parts = key.split(separator: ".")
            if key.hasPrefix("model.layers."), parts.count > 2, let idx = Int(parts[2]) {
                return idx < n
            }
            return true
        }
        if args.tieWordEmbeddings {
            weights["lm_head.weight"] = nil
        }
        for l in 0 ..< n {
            let prefix = "model.layers.\(l)"
            if l >= args.firstKDenseReplace, let numExperts = args.numExperts {
                for m in ["gate_proj", "down_proj", "up_proj"] {
                    for k in ["weight", "scales", "biases"] {
                        guard weights["\(prefix).mlp.experts.0.\(m).\(k)"] != nil else { continue }
                        let stackedWeights = (0 ..< numExperts).map { e in
                            weights.removeValue(forKey: "\(prefix).mlp.experts.\(e).\(m).\(k)")!
                        }
                        weights["\(prefix).mlp.switch_mlp.\(m).\(k)"] = stacked(stackedWeights)
                    }
                }
                for suffix in ["weight", "bias", "scales", "biases"] {
                    if let v = weights.removeValue(forKey: "\(prefix).mlp.gate.\(suffix)") {
                        weights["\(prefix).mlp.gate.gate_proj.\(suffix)"] = v
                    }
                }
            }
            // Unquantized checkpoints ship MLA's kv_b_proj; split it into the
            // absorbed embed_q / unembed_out per-head projections.
            if let v = weights.removeValue(forKey: "\(prefix).attention.kv_b_proj.weight") {
                let headDim = args.qkNopeHeadDim + args.vHeadDim
                let r = v.reshaped(args.numAttentionHeads, headDim, -1)
                weights["\(prefix).attention.embed_q.weight"] = contiguous(
                    r[0..., ..<args.qkNopeHeadDim, 0...].swappedAxes(-1, -2))
                weights["\(prefix).attention.unembed_out.weight"] = contiguous(
                    r[0..., args.qkNopeHeadDim..., 0...])
            }
        }
        return weights
    }

    public var loraLayers: [Module] { model.layers }
}
