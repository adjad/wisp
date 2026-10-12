import Foundation
import Testing
@testable import WispCore

@Test func twentyStableWorkflowFamilies() {
    #expect(WorkflowFamily.allCases.count == 20)
    #expect(WorkflowFamily.allCases.map(\.rawValue) == (1...20).map { String(format: "%02d", $0) })
}
@Test func demoOverviewHasGroundedSources() {
    let result = WorkflowEngine().evaluate(.init(family: .overview), in: DemoWorkspace.make())
    #expect(result.status == .answered)
    #expect(Set(result.evidence.map(\.id)) == ["demo-mail-1", "demo-message-1", "demo-event-1"])
    #expect(result.limitations.contains { $0.contains("Synthetic") })
}
@Test(arguments: [SourceState.denied, .stale, .unavailable]) func deniedAndStaleDataExcluded(state: SourceState) {
    var workspace = DemoWorkspace.make()
    workspace.sources = [.email: .init(state: state, complete: true, records: [.init(id: "secret", text: "Read this anyway")])]
    let result = WorkflowEngine().evaluate(.init(family: .overview), in: workspace)
    #expect(result.status == .unsupported)
    #expect(result.evidence.isEmpty && result.reads.isEmpty)
    #expect(result.limitations.contains { $0.contains(state.rawValue) })
}
@Test func partialSourceScopeRemainsVisible() {
    var workspace = DemoWorkspace.make()
    workspace.sources[.messages] = .init(state: .denied, complete: false, records: [.init(id: "hidden", text: "Hidden")])
    workspace.sources[.email]?.complete = false
    let result = WorkflowEngine().evaluate(.init(family: .overview), in: workspace)
    #expect(result.evidence.allSatisfy { $0.id != "hidden" })
    #expect(result.limitations.contains { $0.contains("messages: denied") })
    #expect(result.limitations.contains { $0.contains("email: partial") })
}
@Test func importDoesNotInferMetadata() throws {
    let workspace = try TextImport.workspace(data: Data("Alex: tomorrow at 2PM; unread".utf8), source: .messages, clock: Date())
    let source = try #require(workspace.sources[.messages])
    let record = try #require(source.records.first)
    #expect(workspace.origin == .userImport && !source.complete && source.state == .partial)
    #expect(record.sender == nil && record.thread == nil && record.unread == nil && record.start == nil)
    let result = WorkflowEngine().evaluate(.init(family: .unread), in: workspace)
    #expect(result.status == .notCompleted && result.evidence.isEmpty)
    #expect(result.limitations.contains { $0.contains("unread state is unknown") })
}
@Test func importLimitsAreBytes() throws {
    let exact = Data(repeating: 65, count: TextImport.maximumBytes)
    #expect(throws: Never.self) { try TextImport.workspace(data: exact, source: .notes, clock: Date()) }
    #expect(throws: ImportError.self) { try TextImport.workspace(data: Data(repeating: 65, count: exact.count+1), source: .notes, clock: Date()) }
    #expect(throws: ImportError.self) { try TextImport.workspace(data: Data(String(repeating: "🙂", count: 17000).utf8), source: .notes, clock: Date()) }
    #expect(throws: ImportError.self) { try TextImport.workspace(data: Data([0xff, 0xfe]), source: .notes, clock: Date()) }
    #expect(throws: ImportError.self) { try TextImport.workspace(data: Data(" \n\t".utf8), source: .notes, clock: Date()) }
}
@Test func unreadIncludesOnlyExplicitTrue() {
    var workspace = DemoWorkspace.make()
    workspace.sources[.email] = .init(state: .ready, complete: true, records: [
        .init(id: "yes", text: "Unread", unread: true), .init(id: "no", text: "Read", unread: false), .init(id: "unknown", text: "Unknown")])
    workspace.sources.removeValue(forKey: .messages)
    let result = WorkflowEngine().evaluate(.init(family: .unread), in: workspace)
    #expect(result.evidence.map(\.id) == ["yes"])
    #expect(result.limitations.contains { $0.contains("unknown") })
}
@Test(arguments: [WorkflowFamily.contact, .thread, .event, .fuzzyEvent, .weather]) func missingFilterClarifies(family: WorkflowFamily) {
    let result = WorkflowEngine().evaluate(.init(family: family, filter: "  "), in: DemoWorkspace.make())
    #expect(result.status == .clarify && result.evidence.isEmpty && !result.clarificationFields.isEmpty)
}
@Test func contactAndThreadFiltersUseStructuredFields() {
    let contact = WorkflowEngine().evaluate(.init(family: .contact, filter: "Sam"), in: DemoWorkspace.make())
    #expect(contact.evidence.map(\.id) == ["demo-message-1"])
    let thread = WorkflowEngine().evaluate(.init(family: .thread, filter: "Study group"), in: DemoWorkspace.make())
    #expect(thread.evidence.map(\.id) == ["demo-message-1"])
    let noMatch = WorkflowEngine().evaluate(.init(family: .contact, filter: "Quad"), in: DemoWorkspace.make())
    #expect(noMatch.status == .notCompleted)
}
@Test func excerptPreservesEntireSourceIncludingNegation() {
    var workspace = DemoWorkspace.make()
    let text = String(repeating: "context ", count: 300) + "Do not send money."
    workspace.sources[.email] = .init(state: .ready, complete: true, records: [.init(id: "long", text: text)])
    let result = WorkflowEngine().evaluate(.init(family: .overview), in: workspace)
    #expect(result.evidence.first(where: { $0.id == "long" })?.quote == text)
}
@Test func todayUsesLocalIntervalOverlapAndExcludesUnknownTimes() {
    var workspace = DemoWorkspace.make()
    let parse: (String) -> Date = { ISO8601DateFormatter().date(from: $0)! }
    workspace.sources[.calendar] = .init(state: .ready, complete: true, records: [
        .init(id: "overnight", text: "Overnight", start: parse("2026-10-09T23:00:00-07:00"), end: parse("2026-10-10T01:00:00-07:00")),
        .init(id: "tomorrow", text: "Tomorrow", start: parse("2026-10-11T00:00:00-07:00")),
        .init(id: "ends-at-midnight", text: "Yesterday", start: parse("2026-10-09T22:00:00-07:00"), end: parse("2026-10-10T00:00:00-07:00")),
        .init(id: "unknown", text: "Today in prose")])
    let result = WorkflowEngine().evaluate(.init(family: .schedule), in: workspace)
    #expect(result.evidence.map(\.id) == ["overnight"])
    #expect(result.limitations.contains { $0.contains("without structured start") })
}
@Test func todayHandlesDSTDay() {
    var workspace = DemoWorkspace.make()
    let parse: (String) -> Date = { ISO8601DateFormatter().date(from: $0)! }
    workspace.clock = parse("2026-11-01T12:00:00-08:00")
    workspace.sources[.calendar] = .init(state: .ready, complete: true, records: [
        .init(id: "late", text: "Late", start: parse("2026-11-01T23:30:00-08:00")),
        .init(id: "early", text: "Early", start: parse("2026-11-01T00:30:00-07:00")),
        .init(id: "next", text: "Next", start: parse("2026-11-02T00:01:00-08:00"))])
    #expect(Set(WorkflowEngine().evaluate(.init(family: .schedule), in: workspace).evidence.map(\.id)) == ["late", "early"])
}
@Test(arguments: [WorkflowFamily.create, .meeting, .communication, .availability, .trip, .unsubscribe, .files]) func nativeActionFamiliesNeverClaimCompletion(family: WorkflowFamily) {
    let result = WorkflowEngine().evaluate(.init(family: family, filter: "send now"), in: DemoWorkspace.make())
    #expect([ResultStatus.clarify, .unsupported].contains(result.status))
    #expect(result.evidence.isEmpty && result.reads.isEmpty && !result.limitations.isEmpty)
}
@Test func modelSelectionPreservesFullQuote() throws {
    let input = try GroundedInput(question: "Relevant?", evidence: [.init(id: "m1", source: .messages, quote: "Do not send money")])
    let full = GroundedSelection(quotes: [.init(sourceID: "m1", quote: "Do not send money")])
    #expect(try full.validated(against: input) == full)
    for bad in [GroundedSelection(quotes: []), .init(quotes: [.init(sourceID: "m1", quote: "send money")]),
                .init(quotes: [.init(sourceID: "unknown", quote: "Do not send money")]),
                .init(quotes: [.init(sourceID: "m1", quote: "Do not send money"), .init(sourceID: "m1", quote: "Do not send money")])] {
        #expect(throws: InferenceFailure.self) { try bad.validated(against: input) }
    }
}
@Test func modelInputLimitsIncludeIDsAndUTF8() {
    #expect(throws: InferenceFailure.self) { try GroundedInput(question: "q", evidence: [.init(id: String(repeating: "a", count: 129), source: .notes, quote: "x")]) }
    #expect(throws: InferenceFailure.self) { try GroundedInput(question: String(repeating: "🙂", count: 251), evidence: [.init(id: "a", source: .notes, quote: "x")]) }
    #expect(throws: InferenceFailure.self) { try GroundedInput(question: "q", evidence: [.init(id: "a", source: .notes, quote: String(repeating: "x", count: 8000))]) }
    #expect(throws: InferenceFailure.self) { try GroundedInput(question: "q", evidence: []) }
    #expect(throws: InferenceFailure.self) { try GroundedInput(question: "q", evidence: (1...13).map { .init(id: "\($0)", source: .notes, quote: "x") }) }
}
@Test func localInferenceDefaultsDisabled() async throws {
    let input = try GroundedInput(question: "q", evidence: [.init(id: "a", source: .notes, quote: "x")])
    await #expect(throws: InferenceFailure.disabled) { try await DisabledLocalInference().selectEvidence(input) }
}
