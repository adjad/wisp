import Foundation
import Testing
@testable import WispCore

private func fixture(version: Int = 1, cases: [[String: Any]]? = nil, sources: [String: Any]? = nil) throws -> Data {
    let item: [String: Any] = ["id": "test-1", "family": "01", "split": "heldout", "kind": "positive", "scenario": "s", "turns": [["role": "user", "text": "Overview"]], "expect": "EXPECTATION_SENTINEL"]
    return try JSONSerialization.data(withJSONObject: ["schema_version": version, "families": [["id": "01", "name": "Overview"]],
        "scenarios": ["s": ["clock": "2026-10-10T10:00:00-07:00", "timezone": "America/Los_Angeles", "facts": "REFERENCE_FACT_SENTINEL",
            "sources": sources ?? ["email": ["state": "ready", "complete": true, "records": [["id": "m1", "body": "Synthetic source"]]]]]], "cases": cases ?? [item]])
}
@Test func referenceOracleNeverDecodedIntoExecutionInput() throws {
    let corpus = try Workflow20Corpus.load(fixture())
    let input = try #require(corpus.inputs().first)
    #expect(input.scenario.sources["email"]?.records.first?.object?["body"]?.string == "Synthetic source")
    let observation = Workflow20FoundationAdapter().observe(input)
    let text = String(decoding: try JSONEncoder().encode(observation), as: UTF8.self)
    #expect(!text.contains("EXPECTATION_SENTINEL") && !text.contains("REFERENCE_FACT_SENTINEL"))
    #expect(observation.status == "unsupported" && observation.effects.isEmpty && observation.facts.isEmpty)
    #expect(!observation.approval.granted && observation.approval.approved_effects.isEmpty)
    #expect(observation.metrics["inference_executed"] == .bool(false))
}
@Test func unsupportedContractVersionRejected() throws {
    #expect(throws: ContractError.unsupportedVersion) { try Workflow20Corpus.load(fixture(version: 2)) }
}
@Test func duplicateCaseIDRejected() throws {
    let data = try fixture()
    var root = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
    let cases = try #require(root["cases"] as? [[String: Any]])
    root["cases"] = cases + cases
    #expect(throws: ContractError.duplicateID) { try Workflow20Corpus.load(JSONSerialization.data(withJSONObject: root)) }
}
@Test func malformedRecordProvenanceRejected() throws {
    #expect(throws: ContractError.invalidScenario) { try Workflow20Corpus.load(fixture(sources: ["email": ["state": "ready", "complete": true, "records": [["body": "Missing id"]]]])) }
    #expect(throws: ContractError.invalidScenario) { try Workflow20Corpus.load(fixture(sources: ["unsupported-source": ["state": "ready", "complete": true, "records": []]])) }
}
@Test func missingScenarioAndFamilyRejected() throws {
    var root = try #require(JSONSerialization.jsonObject(with: fixture()) as? [String: Any])
    var cases = try #require(root["cases"] as? [[String: Any]])
    cases[0]["scenario"] = "missing"; root["cases"] = cases
    #expect(throws: ContractError.invalidScenario) { try Workflow20Corpus.load(JSONSerialization.data(withJSONObject: root)) }
    cases[0]["family"] = "99"; root["cases"] = cases
    #expect(throws: ContractError.invalidFamily) { try Workflow20Corpus.load(JSONSerialization.data(withJSONObject: root)) }
}
@Test func JSONValueRoundTripsTypedData() throws {
    let value = JSONValue.object(["flag": .bool(true), "value": .number(3.5), "nested": .array([.null, .string("x")])])
    #expect(try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(value)) == value)
}
