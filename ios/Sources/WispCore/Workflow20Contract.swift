import Foundation

public enum JSONValue: Codable, Equatable, Sendable {
    case object([String: JSONValue]), array([JSONValue]), string(String), number(Double), bool(Bool), null
    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null }
        else if let value = try? container.decode(Bool.self) { self = .bool(value) }
        else if let value = try? container.decode(String.self) { self = .string(value) }
        else if let value = try? container.decode(Double.self) { self = .number(value) }
        else if let value = try? container.decode([String: JSONValue].self) { self = .object(value) }
        else { self = .array(try container.decode([JSONValue].self)) }
    }
    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .object(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .bool(let value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }
    public var object: [String: JSONValue]? { if case .object(let value) = self { value } else { nil } }
    public var string: String? { if case .string(let value) = self { value } else { nil } }
}
public enum ContractError: Error, Equatable { case unsupportedVersion, duplicateID, invalidFamily, invalidScenario, invalidCase, oversizedCorpus }
public struct Workflow20Corpus: Decodable, Sendable {
    public struct Family: Decodable, Sendable { public let id: String; public let name: String }
    public struct Source: Decodable, Sendable { public let state: SourceState; public let complete: Bool; public let records: [JSONValue] }
    public struct Scenario: Decodable, Sendable {
        public let clock: String
        public let timezone: String
        public let sources: [String: Source]
        // Reference facts are evaluation-only. They are deliberately not decoded into the execution input.
    }
    public struct Turn: Codable, Sendable { public let role: String; public let text: String }
    public struct Case: Decodable, Sendable {
        public let id: String; public let family: String; public let split: String; public let kind: String
        public let scenario: String; public let turns: [Turn]
        // `expect` is deliberately not decoded. Independent Mac grader owns expectations.
    }
    public let schema_version: Int
    public let families: [Family]
    public let scenarios: [String: Scenario]
    public let cases: [Case]
    public static func load(_ data: Data) throws -> Workflow20Corpus {
        guard data.count <= 16 * 1024 * 1024 else { throw ContractError.oversizedCorpus }
        let corpus = try JSONDecoder().decode(Workflow20Corpus.self, from: data)
        guard corpus.schema_version == 1 else { throw ContractError.unsupportedVersion }
        guard Set(corpus.cases.map(\.id)).count == corpus.cases.count,
              Set(corpus.families.map(\.id)).count == corpus.families.count else { throw ContractError.duplicateID }
        let familyIDs = Set(corpus.families.map(\.id))
        guard familyIDs.allSatisfy({ WorkflowFamily(rawValue: $0) != nil }) else { throw ContractError.invalidFamily }
        for scenario in corpus.scenarios.values {
            guard ISO8601DateFormatter().date(from: scenario.clock) != nil, TimeZone(identifier: scenario.timezone) != nil,
                  scenario.sources.keys.allSatisfy({ SourceKind(rawValue: $0) != nil }) else { throw ContractError.invalidScenario }
            let ids = scenario.sources.values.flatMap(\.records).compactMap { $0.object?["id"]?.string }
            guard ids.count == scenario.sources.values.reduce(0, { $0 + $1.records.count }),
                  Set(ids).count == ids.count, ids.allSatisfy({ !$0.isEmpty }) else { throw ContractError.invalidScenario }
        }
        for item in corpus.cases {
            guard !item.id.isEmpty, familyIDs.contains(item.family) else { throw ContractError.invalidFamily }
            guard corpus.scenarios[item.scenario] != nil else { throw ContractError.invalidScenario }
            guard ["dev", "heldout"].contains(item.split), ["positive", "negative", "ambiguous", "multi_turn", "failure"].contains(item.kind),
                  !item.turns.isEmpty, item.turns.allSatisfy({ ["user", "assistant"].contains($0.role) && !$0.text.isEmpty }) else { throw ContractError.invalidCase }
        }
        return corpus
    }
    public func inputs() -> [Workflow20Input] {
        cases.map { item in
            // No split, kind, expected facts, fact IDs or required actions reach the adapter.
            Workflow20Input(caseID: item.id, family: WorkflowFamily(rawValue: item.family)!, turns: item.turns,
                            scenario: scenarios[item.scenario]!)
        }
    }
}
public struct Workflow20Input: Sendable {
    public let caseID: String
    public let family: WorkflowFamily
    public let turns: [Workflow20Corpus.Turn]
    public let scenario: Workflow20Corpus.Scenario
}
public struct Workflow20Observation: Encodable, Sendable {
    public struct Approval: Encodable, Sendable { public let requested: Bool; public let granted: Bool; public let approved_effects: [JSONValue] }
    public let case_id: String
    public let status: String
    public let facts: [JSONValue]
    public let reads: [String]
    public let tool_calls: [JSONValue]
    public let approval: Approval
    public let effects: [JSONValue]
    public let answer: String
    public let clarification_fields: [String]
    public let limitations: [String]
    public let metrics: [String: JSONValue]
}
/// Contract plumbing only. Every semantic workflow remains explicitly unsupported until a real adapter is measured.
/// This cannot turn reference expectations or a UI excerpt into a workflow-completion observation.
public struct Workflow20FoundationAdapter: Sendable {
    public init() {}
    public func observe(_ input: Workflow20Input) -> Workflow20Observation {
        Workflow20Observation(case_id: input.caseID, status: "unsupported", facts: [], reads: [], tool_calls: [],
            approval: .init(requested: false, granted: false, approved_effects: []), effects: [], answer: "",
            clarification_fields: [], limitations: ["iPhone foundation has no validated semantic adapter for family \(input.family.rawValue). No source or action was executed."],
            metrics: ["inference_executed": .bool(false), "adapter_mode": .string("contract_only")])
    }
}
