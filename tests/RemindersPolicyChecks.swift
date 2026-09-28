import Foundation

enum WispClient { static let baseURL = URL(string: "http://offline.fixture/")! }

@main struct PolicyCheck {
    static func main() {
        let active: Set<String> = ["active-list"]
        precondition(RemindersWriter.eligibleForIncompleteSync(
            calendarID: "active-list", reminderCalendarIDs: active,
            completed: false, hasDueDate: true))
        precondition(!RemindersWriter.eligibleForIncompleteSync(
            calendarID: "deleted-list", reminderCalendarIDs: active,
            completed: false, hasDueDate: true))
        precondition(!RemindersWriter.eligibleForIncompleteSync(
            calendarID: nil, reminderCalendarIDs: active,
            completed: false, hasDueDate: true))
        precondition(!RemindersWriter.eligibleForIncompleteSync(
            calendarID: "active-list", reminderCalendarIDs: active,
            completed: true, hasDueDate: true))
        precondition(!RemindersWriter.eligibleForIncompleteSync(
            calendarID: "active-list", reminderCalendarIDs: active,
            completed: false, hasDueDate: false))
        // A list removed from the published set disappears, then a restored
        // list can appear again without a title-based exception.
        precondition(!RemindersWriter.eligibleForIncompleteSync(
            calendarID: "active-list", reminderCalendarIDs: [],
            completed: false, hasDueDate: true))
        precondition(RemindersWriter.eligibleForIncompleteSync(
            calendarID: "active-list", reminderCalendarIDs: active,
            completed: false, hasDueDate: true))
        // An empty list set after a snapshot with rows is not authoritative.
        precondition(RemindersWriter.reminderListsLookTransientlyMissing(
            calendarCount: 0, previousRowCount: 3))
        precondition(!RemindersWriter.reminderListsLookTransientlyMissing(
            calendarCount: 0, previousRowCount: 0))
        precondition(!RemindersWriter.reminderListsLookTransientlyMissing(
            calendarCount: 2, previousRowCount: 3))
        print("10 synthetic native policy checks passed")
    }
}
