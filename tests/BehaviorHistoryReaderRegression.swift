import Foundation

// Only pure mapping methods are called. No EKEventStore, native reader, app or
// live transport is instantiated by this executable.
enum WispClient { static let baseURL = URL(string: "http://offline.fixture/")! }

@main struct HistoryReaderChecks {
    static func main() {
        let event = CalendarReader.historyEventRow(id: "event", title: "Gym", start: 100,
                                                   end: 200, allDay: false, calendar: "Home")!
        precondition(event["when_ts"] as? Double == 100)
        precondition(event["completion_ts"] == nil)
        precondition(event["completed"] == nil)
        precondition(CalendarReader.historyEventRow(id: "", title: "X", start: 1,
            end: 2, allDay: false, calendar: "") == nil)
        precondition(CalendarReader.historyEventRow(id: "x", title: "X", start: .nan,
            end: 2, allDay: false, calendar: "") == nil)
        precondition(CalendarReader.historyEventRow(id: "x", title: "X", start: 3,
            end: 2, allDay: false, calendar: "") == nil)
        let allDay = CalendarReader.historyEventRow(id: "holiday", title: "Holiday", start: 0,
            end: 86400, allDay: true, calendar: "Home")!
        precondition(allDay["all_day"] as? Bool == true)
        let completed = RemindersWriter.historyCompletionRow(id: "task", title: "Read", completedAt: 150,
                                                              list: "Personal")!
        precondition(completed["completion_ts"] as? Double == 150)
        precondition(completed["when_ts"] == nil && completed["due_ts"] == nil)
        precondition(RemindersWriter.historyCompletionRow(id: "task", title: "Read", completedAt: nil,
                                                          list: "") == nil)
        precondition(RemindersWriter.historyCompletionRow(id: "task", title: "Read", completedAt: .infinity,
                                                          list: "") == nil)
        precondition(RemindersWriter.historyCompletionRow(id: "", title: "Read", completedAt: 150,
                                                          list: "") == nil)
        print("synthetic behavior history mapping checks passed")
    }
}
