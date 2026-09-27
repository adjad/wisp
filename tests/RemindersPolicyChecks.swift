import Foundation

enum WispClient { static let baseURL = URL(string: "http://offline.fixture/")! }

@main struct PolicyCheck {
    static func main() {
        let active: Set<String> = ["active-list"]
        precondition(RemindersWriter.eligibleForActiveSync(
            calendarID: "active-list", activeCalendarIDs: active,
            completed: false, hasDueDate: true))
        precondition(!RemindersWriter.eligibleForActiveSync(
            calendarID: "deleted-list", activeCalendarIDs: active,
            completed: false, hasDueDate: true))
        precondition(!RemindersWriter.eligibleForActiveSync(
            calendarID: nil, activeCalendarIDs: active,
            completed: false, hasDueDate: true))
        precondition(!RemindersWriter.eligibleForActiveSync(
            calendarID: "active-list", activeCalendarIDs: active,
            completed: true, hasDueDate: true))
        precondition(!RemindersWriter.eligibleForActiveSync(
            calendarID: "active-list", activeCalendarIDs: active,
            completed: false, hasDueDate: false))
        // A list removed from the published set disappears, then a restored
        // list can appear again without a title-based exception.
        precondition(!RemindersWriter.eligibleForActiveSync(
            calendarID: "active-list", activeCalendarIDs: [],
            completed: false, hasDueDate: true))
        precondition(RemindersWriter.eligibleForActiveSync(
            calendarID: "active-list", activeCalendarIDs: active,
            completed: false, hasDueDate: true))
        print("7 synthetic native policy checks passed")
    }
}
