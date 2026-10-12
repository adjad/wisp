import Foundation
public enum InferenceFailure: Error, Equatable { case disabled, unavailable(String), inputTooLarge, invalidEvidence, invalidOutput }
public struct GroundedInput: Sendable {
    public let question: String
    public let evidence: [Evidence]
    public init(question: String, evidence: [Evidence]) throws {
        guard question.utf8.count <= 1000, evidence.count <= 12,
              evidence.allSatisfy({ $0.id.utf8.count <= 128 }),
              evidence.reduce(0, { $0 + $1.quote.utf8.count + $1.id.utf8.count }) <= 8000 else { throw InferenceFailure.inputTooLarge }
        guard !evidence.isEmpty, Set(evidence.map(\.id)).count == evidence.count,
              evidence.allSatisfy({ !$0.id.isEmpty && !$0.quote.isEmpty }) else { throw InferenceFailure.invalidEvidence }
        self.question = question; self.evidence = evidence
    }
}
/// Runtime output may select exact source excerpts, never action parameters or new facts.
public struct GroundedSelection: Codable, Equatable, Sendable {
    public struct Quote: Codable, Equatable, Sendable {
        public var sourceID: String
        public var quote: String
        public init(sourceID: String, quote: String) { self.sourceID = sourceID; self.quote = quote }
    }
    public var quotes: [Quote]
    public init(quotes: [Quote]) { self.quotes = quotes }
    public func validated(against input: GroundedInput) throws -> GroundedSelection {
        guard !quotes.isEmpty, quotes.count <= 12, Set(quotes.map(\.sourceID)).count == quotes.count else { throw InferenceFailure.invalidOutput }
        for quote in quotes {
            guard !quote.quote.isEmpty, let source = input.evidence.first(where: { $0.id == quote.sourceID }),
                  source.quote == quote.quote else { throw InferenceFailure.invalidOutput }
        }
        return self
    }
}
public protocol LocalInferenceProvider: Sendable {
    func selectEvidence(_ input: GroundedInput) async throws -> GroundedSelection
}
public struct DisabledLocalInference: LocalInferenceProvider {
    public init() {}
    public func selectEvidence(_ input: GroundedInput) async throws -> GroundedSelection { throw InferenceFailure.disabled }
}
