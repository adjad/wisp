import Foundation

public enum WorkflowFamily: String, CaseIterable, Codable, Sendable, Identifiable {
    case overview = "01", daily = "02", schedule = "03", weather = "04", priorities = "05"
    case contact = "06", thread = "07", groceries = "08", event = "09", create = "10"
    case trip = "11", communication = "12", unsubscribe = "13", files = "14", knowledge = "15"
    case meeting = "16", fuzzyEvent = "17", unread = "18", availability = "19", pending = "20"
    public var id: String { rawValue }
    public var title: String {
        switch self {
        case .overview: "Inbox overview"
        case .daily: "Daily briefing"
        case .schedule: "Schedule"
        case .weather: "Weather"
        case .priorities: "Priorities"
        case .contact: "What a contact said"
        case .thread: "Thread recap"
        case .groceries: "Shopping list"
        case .event: "Event time"
        case .create: "Reminder or event"
        case .trip: "SFO trip"
        case .communication: "Grounded draft"
        case .unsubscribe: "Unsubscribe"
        case .files: "Organize files"
        case .knowledge: "Local knowledge"
        case .meeting: "Arrange a meeting"
        case .fuzzyEvent: "Find a test or exam"
        case .unread: "Unread catch-up"
        case .availability: "Availability reply"
        case .pending: "Pending actions"
        }
    }
}

public enum SourceKind: String, Codable, CaseIterable, Sendable {
    case email, messages, calendar, reminders, notes, contacts, weather, files, queue, memory, location, traffic
}
public enum SourceState: String, Codable, Sendable { case ready, unavailable, denied, stale, partial }
public enum DataOrigin: String, Codable, Sendable { case synthetic, userImport }
public struct SourceRecord: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var text: String
    public var sender: String?
    public var thread: String?
    public var start: Date?
    public var end: Date?
    public var unread: Bool?
    public init(id: String, text: String, sender: String? = nil, thread: String? = nil,
                start: Date? = nil, end: Date? = nil, unread: Bool? = nil) {
        self.id = id; self.text = text; self.sender = sender; self.thread = thread
        self.start = start; self.end = end; self.unread = unread
    }
}
public struct SourceSnapshot: Sendable {
    public var state: SourceState
    public var complete: Bool
    public var records: [SourceRecord]
    public init(state: SourceState, complete: Bool, records: [SourceRecord]) {
        self.state = state; self.complete = complete; self.records = records
    }
}
public struct Workspace: Sendable {
    public var origin: DataOrigin
    public var clock: Date
    public var timezone: TimeZone
    public var sources: [SourceKind: SourceSnapshot]
    public init(origin: DataOrigin, clock: Date, timezone: TimeZone, sources: [SourceKind: SourceSnapshot]) {
        self.origin = origin; self.clock = clock; self.timezone = timezone; self.sources = sources
    }
}
public struct Evidence: Equatable, Sendable, Identifiable {
    public var id: String
    public var source: SourceKind
    public var quote: String
    public init(id: String, source: SourceKind, quote: String) { self.id = id; self.source = source; self.quote = quote }
}
public enum ResultStatus: String, Codable, Sendable { case answered, clarify, prepared, notCompleted = "not_completed", unsupported }
public struct WorkflowResult: Sendable {
    public var status: ResultStatus
    public var title: String
    public var evidence: [Evidence] = []
    public var limitations: [String] = []
    public var clarificationFields: [String] = []
    public var reads: [SourceKind] = []
}
public struct WorkflowRequest: Sendable {
    public var family: WorkflowFamily
    /// Explicit search terms from the app form, not arbitrary tool instructions.
    public var filter: String
    public init(family: WorkflowFamily, filter: String = "") { self.family = family; self.filter = filter }
}
public enum ImportError: Error, LocalizedError {
    case tooLarge, empty, invalidText
    public var errorDescription: String? {
        switch self { case .tooLarge: "Import is limited to 64 KiB."; case .empty: "Choose nonempty text."; case .invalidText: "Choose a UTF-8 text file." }
    }
}
public enum TextImport {
    public static let maximumBytes = 65_536
    public static func workspace(data: Data, source: SourceKind, clock: Date) throws -> Workspace {
        guard data.count <= maximumBytes else { throw ImportError.tooLarge }
        guard let text = String(data: data, encoding: .utf8) else { throw ImportError.invalidText }
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw ImportError.empty }
        // Text imports carry no implied unread, identity, time, or full-account coverage.
        return Workspace(origin: .userImport, clock: clock, timezone: .current,
                         sources: [source: .init(state: .partial, complete: false,
                         records: [.init(id: "import-1", text: text)])])
    }
}
