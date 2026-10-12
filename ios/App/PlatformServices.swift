import Foundation
import EventKit
import Contacts

struct PlatformCapability {
    let name: String
    let detail: String
    static let baseline: [PlatformCapability] = [
        .init(name: "Messages and Mail", detail: "Selected text import and paste only. Full native inbox access and unread synchronization are not connected."),
        .init(name: "Calendar and Reminders", detail: "Permission-aware adapters are included for future integration. Reading requires full access. This build requests no access and performs no native reads or writes."),
        .init(name: "Contacts", detail: "Permission states include limited access. Native contact lookup is not connected."),
        .init(name: "Maps and Weather", detail: "Synthetic weather only. Live location, traffic, WeatherKit and Maps handoff are not connected."),
        .init(name: "Communication and scheduling", detail: "No sends, background delivery, calendar writes or subscriptions. A reminder notification would not establish that a message was sent."),
        .init(name: "Files and memory", detail: "Explicit text import remains in memory. No directory scans, file moves, persistent index or shared extension target yet.")
    ]
}

enum PermissionState: Equatable { case notDetermined, denied, restricted, limited, authorized, writeOnly }
/// No permission requests are made automatically. These readers are intentionally not wired to the demo UI.
@MainActor
final class NativeReadBoundary {
    private let events = EKEventStore()
    private let contacts = CNContactStore()
    func calendarPermission() -> PermissionState {
        switch EKEventStore.authorizationStatus(for: .event) {
        case .notDetermined: .notDetermined
        case .denied: .denied
        case .restricted: .restricted
        case .writeOnly: .writeOnly
        case .fullAccess: .authorized
        @unknown default: .denied
        }
    }
    func contactsPermission() -> PermissionState {
        let status = CNContactStore.authorizationStatus(for: .contacts)
        if #available(iOS 18.0, *), status == .limited { return .limited }
        return switch status {
        case .notDetermined: .notDetermined
        case .denied: .denied
        case .restricted: .restricted
        case .authorized: .authorized
        @unknown default: .denied
        }
    }
    func requestCalendarReadAccess() async throws -> Bool { try await events.requestFullAccessToEvents() }
    func requestReminderReadAccess() async throws -> Bool { try await events.requestFullAccessToReminders() }
    func requestContactsAccess() async throws -> Bool { try await contacts.requestAccess(for: .contacts) }
    func calendarRecords(start: Date, end: Date) throws -> [SourceRecord] {
        guard calendarPermission() == .authorized else { throw NativeReadError.permissionRequired }
        guard end > start, end.timeIntervalSince(start) <= 31 * 86400 else { throw NativeReadError.invalidRange }
        let predicate = events.predicateForEvents(withStart: start, end: end, calendars: nil)
        return events.events(matching: predicate).prefix(100).map { event in
            SourceRecord(id: event.eventIdentifier ?? UUID().uuidString, text: event.title ?? "Untitled event", start: event.startDate, end: event.endDate)
        }
    }
}
enum NativeReadError: Error { case permissionRequired, invalidRange }
