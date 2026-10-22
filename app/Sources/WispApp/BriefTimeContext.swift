import Foundation

/// Presentation-only time context. It never fetches data or performs an action.
struct BriefTimeContext {
    enum Period: String, CaseIterable {
        case morning, afternoon, evening, night
    }

    struct Suggestion: Equatable, Identifiable {
        let id: String
        let title: String
        /// An editable draft for Ask, not an automatically submitted request.
        let prompt: String
    }

    let date: Date
    let timezone: TimeZone
    let hour: Int
    let period: Period

    init(date: Date, timezone: TimeZone) {
        self.date = date
        self.timezone = timezone
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timezone
        hour = calendar.component(.hour, from: date)
        switch hour {
        case 5..<12: period = .morning
        case 12..<17: period = .afternoon
        case 17..<21: period = .evening
        default: period = .night
        }
    }

    var beforeDawn: Bool { period == .night && hour < 5 }

    /// The existing composer keeps responsibility for factual due/overdue clauses.
    /// A date selected outside today must continue to use its neutral wording.
    func duePhrase(isToday: Bool) -> String {
        guard isToday else { return "that day" }
        return beforeDawn ? "later today" : "today"
    }

    /// Nil lets the existing day-only wording describe a non-current selection.
    func opening(weekday: String, isToday: Bool, load: DayLoad.Verdict?) -> String? {
        guard isToday else { return nil }
        switch period {
        case .morning:
            switch load {
            case .free: return "A clear start to \(weekday)."
            case .light: return "A light start to \(weekday)."
            case .steady: return "A steady \(weekday) ahead."
            case .busy: return "A busy \(weekday) ahead."
            case .packed: return "A packed \(weekday) ahead."
            case nil: return "Here's your \(weekday) morning."
            }
        case .afternoon:
            switch load {
            case .free: return "Your calendar is clear for the rest of \(weekday)."
            case .light: return "The rest of \(weekday) looks light."
            case .steady: return "A steady rest of \(weekday) ahead."
            case .busy: return "Still a busy \(weekday)."
            case .packed: return "Still a packed \(weekday)."
            case nil: return "Here's where \(weekday) stands."
            }
        case .evening:
            return "Here's what's left of \(weekday)."
        case .night:
            if beforeDawn {
                if load == .free || load == .light { return "A quiet start to \(weekday)." }
                return "A new \(weekday) is getting started."
            }
            return "Here's where \(weekday) stands tonight."
        }
    }

    /// These prompts anchor relative dates to the same civil clock as the Brief.
    /// Historical/future day selections retain the existing neutral actions.
    func suggestions(isToday: Bool) -> [Suggestion] {
        guard isToday else { return [] }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timezone
        let tomorrow = calendar.date(byAdding: .day, value: 1, to: date) ?? date
        let todayLabel = dayLabel(date)
        let tomorrowLabel = dayLabel(tomorrow)
        let context = "The date shown in my Brief is \(todayLabel), in \(timezone.identifier). "
        func suggestion(_ id: String, _ title: String, _ prompt: String) -> Suggestion {
            Suggestion(id: id, title: title, prompt: context + prompt)
        }
        switch period {
        case .morning:
            return [
                suggestion("plan-today", "Plan my day", "Help me plan \(todayLabel) around my calendar and what's due. Suggest a manageable first step."),
                suggestion("due-today", "Review what’s due", "Show what is due on \(todayLabel) and what is overdue, using my current sources."),
                suggestion("first-event", "See my first event", "What is my next calendar event on \(todayLabel), and is there anything I should prepare?")
            ]
        case .afternoon:
            return [
                suggestion("next-priority", "What needs me next?", "What needs my attention next on \(todayLabel)? Consider my remaining calendar and open items."),
                suggestion("focus-time", "Find time to focus", "Find an available focus window in the rest of \(todayLabel). Suggest it without changing my calendar."),
                suggestion("catch-up", "Catch me up", "Catch me up on relevant changes and open threads for \(todayLabel), using the current information you can read.")
            ]
        case .evening:
            return [
                suggestion("wrap-up", "Wrap up today", "Help me review \(todayLabel): what is finished, what is still open, and what needs a next step?"),
                suggestion("open-items", "Review open items", "Show the remaining open items for \(todayLabel). Help me decide what needs finishing and what can wait, without changing anything yet."),
                suggestion("tomorrow", "Look at tomorrow", "Preview my calendar and deadlines for \(tomorrowLabel).")
            ]
        case .night:
            let morningLabel = beforeDawn ? todayLabel : tomorrowLabel
            return [
                suggestion("essentials", "Show today’s essentials", "Show the essential calendar items and deadlines for \(todayLabel), and any overdue items I should know about."),
                suggestion("morning-plan", "Plan for the morning", "Help me prepare for the morning of \(morningLabel), using my calendar and deadlines."),
                Suggestion(id: "capture-thought", title: "Save a thought", prompt: "I'd like to capture a thought for later: ")
            ]
        }
    }

    private func dayLabel(_ value: Date) -> String {
        let format = DateFormatter()
        format.calendar = Calendar(identifier: .gregorian)
        format.locale = Locale(identifier: "en_US_POSIX")
        format.timeZone = timezone
        format.dateFormat = "EEEE, MMMM d, yyyy"
        return format.string(from: value)
    }
}
