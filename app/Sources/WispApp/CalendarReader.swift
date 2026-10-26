import AppKit
import Foundation
import EventKit

// Reads the next N days from macOS Calendar (EventKit) and pushes them to the
// backend as commitments. Lives in the Swift app because Calendar (TCC) access
// is per-process: the app has the clean "Wisp" identity and the Info.plist
// usage strings, so the permission prompt reads "Wisp" and, once granted,
// actually works — unlike the separate Python backend binary.
final class CalendarReader {
    private let store = EKEventStore()
    private let horizonDays = 30   // far enough to catch assignments/exams weeks out
    // How far back to sync PAST events, so questions like "what did I have
    // last month" and the profile builder (service/memory/profile.py) have a
    // real history to work from, not just what's upcoming. EventKit's own
    // predicate limit is 4 years, so 395 days total (see `sync`) is nowhere
    // close — this is a deliberate scope choice, not a technical ceiling.
    private let historyDays = 365
    private var timer: Timer?
    private var historyTask: Task<Void, Never>?

    // Main-thread state for the permission request. The same rules as
    // Reminders (RemindersWriter.Access): an unanswered prompt is a short wait,
    // then a reason with a next step, never "syncing" forever.
    private typealias Access = RemindersWriter.Access
    private var accessRequestOutstanding = false
    private var lastAccessRequestAt: TimeInterval?

    // Sync now (which asks for access only while macOS has no answer), then
    // keep a repeating timer regardless of the result, so a permission granted
    // AFTER launch is picked up within a minute instead of needing a restart.
    // sync() itself no-ops (but reports diagnostics) when unauthorized.
    func start() {
        DispatchQueue.main.async { [weak self] in self?.sync() }
        // Beat the backend-startup race: the Python backend may not be listening
        // yet when the app launches, so the earliest sync POSTs fail silently.
        // Retry on a short ramp until it lands, then settle into a 60s cadence.
        for delay in [1.0, 3.0, 6.0, 12.0, 25.0] {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                self?.sync()
            }
        }
        DispatchQueue.main.async { [weak self] in
            self?.timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in
                self?.sync()
            }
        }
    }

    func requestAccess(_ done: @escaping (Bool) -> Void) {
        if #available(macOS 14.0, *) {
            store.requestFullAccessToEvents { granted, _ in done(granted) }
        } else {
            store.requestAccess(to: .event) { granted, _ in done(granted) }
        }
    }

    /// Ask for access and remember that we did, so a request that finishes
    /// without a decision is reported as such instead of as an endless sync. A
    /// menu-bar app is not frontmost, and macOS can leave its prompt hidden
    /// behind other windows, so Wisp is brought forward first. Main thread only.
    private func requestAccessTracked() {
        guard !accessRequestOutstanding else { return }
        accessRequestOutstanding = true
        lastAccessRequestAt = Date().timeIntervalSince1970
        NSApplication.shared.activate(ignoringOtherApps: true)
        requestAccess { [weak self] _ in
            DispatchQueue.main.async {
                self?.accessRequestOutstanding = false
                self?.sync()
            }
        }
    }

    var isAuthorized: Bool {
        let s = EKEventStore.authorizationStatus(for: .event)
        if #available(macOS 14.0, *) { return s == .fullAccess }
        return s == .authorized
    }

    func sync() {
        // The permission-request state is main-thread state, and a backend
        // refresh request can arrive on another queue.
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in self?.sync() }
            return
        }
        let snapshotStartedAt = Date().timeIntervalSince1970
        guard isAuthorized else {
            let raw = EKEventStore.authorizationStatus(for: .event)
            let report = Access.report(status: Access.status(of: raw),
                                       requestOutstanding: accessRequestOutstanding,
                                       lastRequestAt: lastAccessRequestAt, now: snapshotStartedAt,
                                       source: .calendar)
            if report.needsRequest { requestAccessTracked() }
            var diagnostics = Access.diagnostics(for: report, snapshotStartedAt: snapshotStartedAt)
            diagnostics["status"] = raw.rawValue
            post(events: [], diagnostics: diagnostics)
            return
        }
        startHistoryScan()
        // Start a full `historyDays` back (not just "today") so past events
        // sync too — see AssistantStore.history/service/memory/profile.py.
        // sync_source() replaces the active set for this source on every
        // call, so shrinking/growing this window is always safe: whatever
        // falls outside it on the next sync is simply dropped, exactly like
        // the forward horizon already worked before this change.
        guard let start = Calendar.current.date(byAdding: .day, value: -historyDays, to: Date()),
              let end = Calendar.current.date(byAdding: .day, value: horizonDays, to: Date())
        else { return }
        let cals = store.calendars(for: .event)
        let pred = store.predicateForEvents(withStart: start, end: end, calendars: nil)
        let events = store.events(matching: pred)
        // Declined and cancelled events are not on the person's day.
        let kept = events.filter { ev in
            CalendarEventPolicy.include(
                cancelled: ev.status == .canceled,
                attendees: (ev.attendees ?? []).map {
                    CalendarEventPolicy.Attendee(isCurrentUser: $0.isCurrentUser,
                                                 declined: $0.participantStatus == .declined)
                })
        }

        let payload: [[String: Any]] = kept.compactMap { ev in
            guard let sd = ev.startDate else { return nil }
            let title = ev.title ?? "(untitled)"
            let base = CalendarEventPolicy.classify(title)
            let kind = (!ev.isAllDay && base == "event") ? "meeting" : base
            // NOTE: EventKit gives every occurrence of a RECURRING event the
            // SAME eventIdentifier. We keep source_id as the raw identifier
            // (deleteEvent needs it unchanged); the store disambiguates
            // occurrences by pairing (source_id, when_ts) instead — see
            // AssistantStore.sync_source.
            //
            // "context" is the CALENDAR's name (e.g. "Work", or — commonly —
            // the account owner's own name, since that's a normal default
            // name for a personal calendar). It is NOT who the meeting is
            // with. Using it as if it were the organizer produced "meeting
            // with Adi Jain" for the user's own events. `organizer` is the
            // real EKParticipant, only set when they're not the current user,
            // so the UI can show an actual "with <person>" only when true.
            var row: [String: Any] = [
                "source_id": ev.eventIdentifier ?? "\(title)-\(sd.timeIntervalSince1970)",
                "kind": kind,
                "title": title,
                "context": ev.calendar?.title ?? "",
                "when_ts": sd.timeIntervalSince1970,
                "all_day": ev.isAllDay,
                "location": ev.location ?? "",
            ]
            // JSONSerialization chokes on a bridged Optional<String>.none inside
            // an [String: Any] — omit the key entirely rather than write nil.
            if let o = ev.organizer, !o.isCurrentUser, let name = o.name {
                row["organizer"] = name
            }
            // For moves: an event with invitees may be moved only by its organizer
            // (CalendarMove.refusal). No organizer_is_user key means "unknown".
            row["invitee_count"] = Self.inviteeCount(ev)
            if let o = ev.organizer { row["organizer_is_user"] = o.isCurrentUser }
            // A subscribed or otherwise read-only calendar: the card then offers neither
            // Cancel nor Move (CalendarMove.refusal would refuse them anyway).
            row["calendar_writable"] = ev.calendar?.allowsContentModifications ?? false
            // `source` is the ACCOUNT this calendar belongs to (e.g. "iCloud",
            // or a Google account's own label) — distinct from `context`
            // (the calendar's own name within that account, e.g. "Work").
            // Lets questions be scoped to one linked account once more than
            // one exists (see get_upcoming/get_past_events's `account` param).
            if let eventEnd = ev.endDate { row["end_ts"] = eventEnd.timeIntervalSince1970 }
            if let src = ev.calendar?.source?.title, !src.isEmpty {
                row["account"] = src
            }
            return row
        }
        post(events: payload, diagnostics: [
            "authorized": true,
            "snapshot_started_at": snapshotStartedAt,
            "coverage_start": start.timeIntervalSince1970,
            "coverage_end": end.timeIntervalSince1970,
            "calendar_count": cals.count,
            "calendars": cals.map { $0.title },
            "events_found": events.count,
            "events_declined_or_cancelled": events.count - kept.count,
            "event_ids": payload.map { "\($0["title"] ?? "") | \($0["source_id"] ?? "")" },
        ])
    }

    /// History has its own append-only endpoint: a backfill page must never
    /// replace the agenda or manufacture cancellation events.
    private func startHistoryScan() {
        guard historyTask == nil else { return }
        historyTask = Task { @MainActor [weak self] in
            defer { self?.historyTask = nil }
            // Bound work per invocation; startup may invoke multiple passes.
            // A failed upload leaves the backend cursor
            // unchanged; the next pass re-reads the same page.
            for _ in 0..<8 {
                guard self?.isAuthorized == true,
                      let job = await WispClient.behaviorNativeHistoryRequest("GET", "behavior/history/calendar/next"),
                      job["state"] as? String == "scanning",
                      let start = job["page_start"] as? Double,
                      let end = job["page_end"] as? Double else { return }
                let capture: ([[String: Any]]?, String) = await Task.detached(priority: .utility) {
                    let source = EKEventStore()
                    guard EKEventStore.authorizationStatus(for: .event) == .fullAccess,
                          !source.calendars(for: .event).isEmpty else { return (nil,"access_unavailable") }
                    let predicate = source.predicateForEvents(withStart: Date(timeIntervalSince1970: start),
                        end: Date(timeIntervalSince1970: end), calendars: nil)
                    let events = source.events(matching: predicate)
                    guard events.count <= 10000 else { return (nil,"row_limit") }
                    var result: [[String: Any]] = []
                    for ev in events {
                        guard let id = ev.eventIdentifier, !id.isEmpty,
                              let first = ev.startDate, let last = ev.endDate else { return (nil,"invalid_record") }
                        guard first.timeIntervalSince1970 < end && last.timeIntervalSince1970 > start else { continue }
                        guard let row = Self.historyEventRow(id: id, title: ev.title ?? "(untitled)",
                            start: first.timeIntervalSince1970, end: last.timeIntervalSince1970,
                            allDay: ev.isAllDay, calendar: ev.calendar?.title ?? "") else { return (nil,"invalid_record") }
                        result.append(row)
                    }
                    guard EKEventStore.authorizationStatus(for: .event) == .fullAccess else { return (nil,"access_unavailable") }
                    return (result,"")
                }.value
                let rows = capture.0
                guard await WispClient.behaviorNativeHistoryBatch("calendar", job: job, rows: rows ?? [],
                    complete: rows != nil, reason: capture.1) else { return }
                if rows == nil { return }
                await Task.yield()
            }
        }
    }

    static func historyEventRow(id: String, title: String, start: Double, end: Double,
                                allDay: Bool, calendar: String) -> [String: Any]? {
        guard !id.isEmpty, id.count <= 1000, start.isFinite, end.isFinite, end >= start else { return nil }
        return ["source_id": id, "title": String((title.isEmpty ? "(untitled)" : title).prefix(1000)),
                "when_ts": start, "end_ts": end, "all_day": allDay, "context": calendar]
    }

    // Create a real event in the default calendar (write access comes with the
    // full-access grant). The backend receipt records it before the next sync.
    func createEvent(title: String, startTs: Double, durationMin: Int, location: String) -> [String: Any] {
        guard isAuthorized, let cal = store.defaultCalendarForNewEvents else {
            return ["ok": false, "error": "Calendar access denied or no writable default calendar"]
        }
        let ev = EKEvent(eventStore: store)
        ev.title = title
        ev.startDate = Date(timeIntervalSince1970: startTs)
        ev.endDate = ev.startDate.addingTimeInterval(Double(max(1, durationMin) * 60))
        if !location.isEmpty { ev.location = location }
        ev.calendar = cal
        return verifiedCreation(ev)
    }

    /// A date-only commitment never borrows an arbitrary meeting start or duration.
    func createAllDayEvent(title: String, day: String, timezone: String, location: String) -> [String: Any] {
        guard isAuthorized, let cal = store.defaultCalendarForNewEvents,
              cal.allowsContentModifications,
              !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let zone = TimeZone(identifier: timezone),
              let bounds = AssistantDelivery.allDayCalendarBounds(day: day, timezone: timezone) else {
            return ["ok": false, "error": "All-day Calendar access or date is unavailable"]
        }
        let ev = EKEvent(eventStore: store)
        ev.title = title
        ev.calendar = cal
        ev.timeZone = zone
        ev.isAllDay = true
        ev.startDate = Date(timeIntervalSince1970: bounds.start)
        ev.endDate = ev.startDate.addingTimeInterval(Double(bounds.minutes) * 60)
        if !location.isEmpty { ev.location = location }
        return verifiedCreation(ev)
    }

    private func verifiedCreation(_ event: EKEvent) -> [String: Any] {
        let expected = CalendarCreationFacts(title: event.title, start: event.startDate.timeIntervalSince1970,
            end: event.endDate.timeIntervalSince1970, allDay: event.isAllDay)
        return CalendarCreationReceipt.perform(expected: expected, save: {
            try store.save(event, span: .thisEvent)
            return event.eventIdentifier
        }, readback: { id in
            guard let observed = store.event(withIdentifier: id),
                  let start = observed.startDate, let end = observed.endDate else { return nil }
            return CalendarCreationFacts(title: observed.title ?? "", start: start.timeIntervalSince1970,
                end: end.timeIntervalSince1970, allDay: observed.isAllDay)
        })
    }

    // Remove a real event from macOS Calendar. `occurrenceTs` disambiguates
    // WHICH occurrence to delete: EventKit gives every instance of a recurring
    // event the same identifier, and `event(withIdentifier:)` alone only ever
    // returns one representative occurrence — not necessarily the one the user
    // meant. Searching a narrow window around the occurrence's own start time
    // and matching the identifier there finds the exact instance.
    func deleteEvent(identifier: String, occurrenceTs: Double? = nil) -> [String: Any] {
        guard isAuthorized else { return ["ok": false, "error": "Calendar access denied"] }
        let target: EKEvent?
        if let ts = occurrenceTs {
            let day: TimeInterval = 86400
            let windowStart = Date(timeIntervalSince1970: ts - day)
            let windowEnd = Date(timeIntervalSince1970: ts + day)
            let pred = store.predicateForEvents(withStart: windowStart, end: windowEnd, calendars: nil)
            let candidates = store.events(matching: pred).filter {
                $0.eventIdentifier == identifier && abs(($0.startDate?.timeIntervalSince1970 ?? .infinity) - ts) < 1
            }
            // Prefer the candidate whose start matches most closely (handles a
            // recurring series where several instances fall in the window).
            target = candidates.min { a, b in
                abs((a.startDate?.timeIntervalSince1970 ?? .infinity) - ts)
                    < abs((b.startDate?.timeIntervalSince1970 ?? .infinity) - ts)
            }
        } else {
            target = store.event(withIdentifier: identifier)
        }
        guard let ev = target else { return ["ok": false, "error": "Exact Calendar occurrence was not found"] }
        do {
            try store.remove(ev, span: .thisEvent)
            return ["ok": true]
        } catch {
            return ["ok": false, "error": "Calendar delete failed: \(error.localizedDescription)"]
        }
    }

    // Move one exact occurrence to a new start (CalendarMove has the rules). The event is
    // found by identifier AND its current start, like deleteEvent, and must still carry
    // the title Wisp saw, and the invitee count the confirmation card stated. An event
    // with invitees moves only when the user organizes it (Calendar then sends them the
    // new time); a start another occurrence of the series already has is refused. Only startDate/endDate change, with span
    // .thisEvent so a repeating series never moves. The saved event is read back and
    // compared field by field; anything other than the requested move reads as
    // "unknown", not done.
    func moveEvent(identifier: String, expectedTitle: String, expectedStart: Double,
                   newStart: Double, durationMin: Int?, expectedInvitees: Int) -> [String: Any] {
        guard isAuthorized else { return ["ok": false, "error": "Calendar access denied"] }
        guard let ev = occurrence(identifier, at: expectedStart) else {
            return ["ok": false, "error": "That event is no longer at the time Wisp last read; nothing was moved"]
        }
        let before = Self.facts(ev)
        let startTaken = abs(newStart - expectedStart) >= 1 && occurrence(identifier, at: newStart) != nil
        if let reason = CalendarMove.refusal(before, expectedTitle: expectedTitle, expectedStart: expectedStart,
                                              calendarWritable: ev.calendar?.allowsContentModifications ?? false,
                                              invitees: Self.inviteeCount(ev), expectedInvitees: expectedInvitees,
                                              organizerIsUser: ev.organizer?.isCurrentUser, startTaken: startTaken) {
            return ["ok": false, "error": reason]
        }
        let newEnd = CalendarMove.newEnd(before, newStart: newStart, durationMin: durationMin)
        ev.startDate = Date(timeIntervalSince1970: newStart)
        ev.endDate = Date(timeIntervalSince1970: newEnd)
        do {
            try store.save(ev, span: .thisEvent, commit: true)
        } catch {
            ev.rollback()
            return ["ok": false, "error": "Calendar didn't save the move: \(error.localizedDescription)"]
        }
        // A fresh lookup at the new start, else the saved object refreshed from the store.
        let readBack = occurrence(identifier, at: newStart) ?? (ev.refresh() ? ev : nil)
        return CalendarMove.savedResult(before: before, after: readBack.map(Self.facts),
                                        newStart: newStart, newEnd: newEnd)
    }

    // Cancel one exact occurrence (a one-tap Cancel on the Day tab). Found by identifier
    // AND start, with the title and invitee count the card showed as preconditions, and the
    // same organizer rules as a move. Removed with span .thisEvent, so a repeating series
    // keeps every other occurrence; then read back that this one is gone.
    func cancelEvent(identifier: String, expectedTitle: String, expectedStart: Double,
                     expectedInvitees: Int) -> [String: Any] {
        guard isAuthorized else { return ["ok": false, "error": "Calendar access denied"] }
        guard let ev = occurrence(identifier, at: expectedStart) else {
            return ["ok": false, "error": "That event is no longer at the time Wisp last read; nothing was cancelled"]
        }
        if let reason = CalendarMove.cancelRefusal(Self.facts(ev), expectedTitle: expectedTitle,
                                                    expectedStart: expectedStart,
                                                    calendarWritable: ev.calendar?.allowsContentModifications ?? false,
                                                    invitees: Self.inviteeCount(ev), expectedInvitees: expectedInvitees,
                                                    organizerIsUser: ev.organizer?.isCurrentUser) {
            return ["ok": false, "error": reason]
        }
        do {
            try store.remove(ev, span: .thisEvent, commit: true)
        } catch {
            return ["ok": false, "error": "Calendar didn't cancel it: \(error.localizedDescription)"]
        }
        return CalendarMove.cancelledResult(stillThere: occurrence(identifier, at: expectedStart) != nil)
    }

    /// Attendees other than the user: who a move would notify. EventKit's list often
    /// includes the user (as organizer), which must not be counted as an invitee.
    static func inviteeCount(_ ev: EKEvent) -> Int {
        (ev.attendees ?? []).filter { !$0.isCurrentUser }.count
    }

    /// A link that shows this occurrence in Calendar (its calendarItemIdentifier, which
    /// Calendar's ical://ekevent URLs take), or nil when it is no longer in the store.
    func showInCalendarURL(identifier: String, at ts: Double) -> URL? {
        guard isAuthorized, let ev = occurrence(identifier, at: ts) else { return nil }
        return CalendarMove.showURL(calendarItemIdentifier: ev.calendarItemIdentifier)
    }

    /// The occurrence of `identifier` starting at `ts` (to the second), or nil.
    private func occurrence(_ identifier: String, at ts: Double) -> EKEvent? {
        let pred = store.predicateForEvents(withStart: Date(timeIntervalSince1970: ts - 86400),
                                            end: Date(timeIntervalSince1970: ts + 86400), calendars: nil)
        return store.events(matching: pred).first {
            $0.eventIdentifier == identifier && abs(($0.startDate?.timeIntervalSince1970 ?? .infinity) - ts) < 1
        }
    }

    private static func facts(_ ev: EKEvent) -> CalendarEventFacts {
        CalendarEventFacts(
            title: ev.title ?? "(untitled)",
            start: ev.startDate?.timeIntervalSince1970 ?? .nan,
            end: ev.endDate?.timeIntervalSince1970 ?? .nan,
            isAllDay: ev.isAllDay,
            calendarID: ev.calendar?.calendarIdentifier ?? "",
            timeZone: ev.timeZone?.identifier ?? "floating",
            notes: ev.notes ?? "", url: ev.url?.absoluteString ?? "<none>", location: ev.location ?? "",
            structuredLocation: ev.structuredLocation.map { place in
                let geo = place.geoLocation.map { "\($0.coordinate.latitude),\($0.coordinate.longitude)" } ?? ""
                return (place.title ?? "") + "|" + geo
            } ?? "",
            attendees: (ev.attendees ?? []).map { $0.url.absoluteString }.sorted(),
            alarms: (ev.alarms ?? []).map { alarm in
                alarm.absoluteDate.map { "absolute:\($0.timeIntervalSince1970)" } ?? "relative:\(alarm.relativeOffset)"
            }.sorted(),
            availability: ev.availability.rawValue)
    }

    private func post(events: [[String: Any]], diagnostics: [String: Any] = [:]) {
        let url = WispClient.baseURL.appendingPathComponent("assistant/sync/calendar")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["events": events,
                                                                    "diagnostics": diagnostics])
        URLSession.shared.dataTask(with: req).resume()
    }
}

/// Which Calendar events Wisp keeps, and what kind each one is. Pure rules,
/// kept apart from EventKit so tests/CalendarReaderChecks.swift can check them
/// without Calendar access or an event store.
///
/// Rules:
/// - IMPORTANT: an event the person declined, or one the organizer cancelled,
///   is not on their day. It is dropped, never announced or planned around.
/// - IMPORTANT: kinds match whole words only. Matching inside words made
///   "Latest", "Contest", "Ashwin" and "Southwest" read as exams and
///   assignments, and each one raised a deadline notification.
enum CalendarEventPolicy {
    /// Words that make an event an exam.
    static let examWords: Set<String> = ["exam", "exams", "midterm", "midterms", "quiz", "quizzes"]
    /// Words that make an event an assignment.
    static let assignmentWords: Set<String> = ["assignment", "assignments", "homework", "hw", "due"]

    /// "exam", "assignment" or "event". A word may carry a trailing number
    /// ("HW3", "Quiz2"), which still counts as that word.
    static func classify(_ title: String) -> String {
        let words = title.lowercased()
            .split { !$0.isLetter && !$0.isNumber }
            .map { word -> String in
                var stem = Substring(word)
                while let last = stem.last, last.isNumber { stem = stem.dropLast() }
                return stem.isEmpty ? String(word) : String(stem)
            }
        if words.contains(where: examWords.contains) { return "exam" }
        if words.contains(where: assignmentWords.contains) { return "assignment" }
        return "event"
    }

    /// One attendee as EventKit reports it, reduced to what the rule needs.
    struct Attendee {
        let isCurrentUser: Bool
        let declined: Bool
    }

    /// Whether an event belongs on the person's calendar feed: not cancelled,
    /// and not declined by them.
    static func include(cancelled: Bool, attendees: [Attendee]) -> Bool {
        guard !cancelled else { return false }
        return !attendees.contains { $0.isCurrentUser && $0.declined }
    }
}
