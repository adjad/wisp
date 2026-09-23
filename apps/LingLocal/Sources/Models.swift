import Foundation

struct CheckpointMetadata: Decodable {
    struct Quantization: Decodable {
        let bits: Int
        let group_size: Int
        let mode: String
    }

    let model_path: String
    let model_type: String
    let layers: Int
    let quantization: Quantization
    let quantized_overrides: Int
}

struct EngineHealth: Decodable {
    let status: String
    let model: String
}

struct ChatTurn: Encodable, Identifiable, Equatable {
    let id: UUID
    let role: String
    let content: String

    enum CodingKeys: String, CodingKey {
        case role
        case content
    }

    init(role: String, content: String) {
        self.id = UUID()
        self.role = role
        self.content = content
    }
}

struct ChatCompletionResponse: Decodable {
    struct Choice: Decodable {
        struct Message: Decodable {
            let role: String
            let content: String
        }

        let message: Message
        let finish_reason: String
    }

    struct Usage: Decodable {
        let prompt_tokens: Int
        let completion_tokens: Int
        let total_tokens: Int
    }

    struct Metrics: Decodable {
        let cached_tokens: Int?
        let prefill_seconds: Double?
        let decode_seconds: Double?
        let total_seconds: Double?
        let peak_memory_bytes: Int64?
        let implementation: String?
    }

    let model: String
    let choices: [Choice]
    let usage: Usage
    let ling_metrics: Metrics?
}

struct ChatLine: Identifiable {
    let id = UUID()
    let role: String
    let content: String
    let metrics: ChatCompletionResponse.Metrics?
}

enum EngineStatus: Equatable {
    case stopped
    case inspecting
    case starting
    case ready
    case stopping
    case failed

    var title: String {
        switch self {
        case .stopped: "Stopped"
        case .inspecting: "Checking checkpoint"
        case .starting: "Starting"
        case .ready: "Ready"
        case .stopping: "Stopping"
        case .failed: "Needs attention"
        }
    }
}
