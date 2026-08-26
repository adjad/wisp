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

    private static let examWords = ["exam", "midterm", "final", "quiz", "test"]
    private static let assignWords = ["due", "assignment", "homework", "hw",
                                      "problem set", "pset", "lab", "project",
                                      "essay", "paper", "submission", "deadline"]

    // Request access, then sync — and keep a repeating timer regardless of the
    // initial grant result, so a permission granted AFTER launch (or a flaky
    // first callback) is picked up within a minute instead of needing a
    // restart. sync() itself no-ops (but reports diagnostics) when unauthorized.
    func start() {
        requestAccess { [weak self] _ in
            DispatchQueue.main.async { self?.sync() }
        }
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

    var isAuthorized: Bool {
        let s = EKEventStore.authorizationStatus(for: .event)
        if #available(macOS 14.0, *) { return s == .fullAccess }
        return s == .authorized
    }

    private func classify(_ title: String) -> String {
        let t = title.lowercased()
        if Self.examWords.contains(where: t.contains) { return "exam" }
        if Self.assignWords.contains(where: t.contains) { return "assignment" }
        return "event"
    }

    func sync() {
        guard isAuthorized else {
            post(events: [], diagnostics: ["authorized": false,
                                           "status": EKEventStore.authorizationStatus(for: .event).rawValue])
            return
        }
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

        let payload: [[String: Any]] = events.compactMap { ev in
            guard let sd = ev.startDate else { return nil }
            let title = ev.title ?? "(untitled)"
            let base = classify(title)
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
            // `source` is the ACCOUNT this calendar belongs to (e.g. "iCloud",
            // or a Google account's own label) — distinct from `context`
            // (the calendar's own name within that account, e.g. "Work").
            // Lets questions be scoped to one linked account once more than
            // one exists (see get_upcoming/get_past_events's `account` param).
            if let src = ev.calendar?.source?.title, !src.isEmpty {
                row["account"] = src
            }
            return row
        }
        post(events: payload, diagnostics: [
            "authorized": true,
            "calendar_count": cals.count,
            "calendars": cals.map { $0.title },
            "events_found": events.count,
            "event_ids": payload.map { "\($0["title"] ?? "") | \($0["source_id"] ?? "")" },
        ])
    }

    // Create a real event in the default calendar (write access comes with the
    // full-access grant), then re-sync so it lands in the store / countdown chip.
    func createEvent(title: String, startTs: Double, durationMin: Int, location: String) {
        guard isAuthorized, let cal = store.defaultCalendarForNewEvents else { return }
        let ev = EKEvent(eventStore: store)
        ev.title = title
        ev.startDate = Date(timeIntervalSince1970: startTs)
        ev.endDate = ev.startDate.addingTimeInterval(Double(max(1, durationMin) * 60))
        if !location.isEmpty { ev.location = location }
        ev.calendar = cal
        do {
            try store.save(ev, span: .thisEvent)
            sync()
        } catch {
            // Save failed (e.g. write-only access) — the backend already told the
            // user it was added; nothing else to do here.
        }
    }

    // Remove a real event from macOS Calendar. `occurrenceTs` disambiguates
    // WHICH occurrence to delete: EventKit gives every instance of a recurring
    // event the same identifier, and `event(withIdentifier:)` alone only ever
    // returns one representative occurrence — not necessarily the one the user
    // meant. Searching a narrow window around the occurrence's own start time
    // and matching the identifier there finds the exact instance.
    func deleteEvent(identifier: String, occurrenceTs: Double? = nil) {
        guard isAuthorized else { return }
        let target: EKEvent?
        if let ts = occurrenceTs {
            let day: TimeInterval = 86400
            let windowStart = Date(timeIntervalSince1970: ts - day)
            let windowEnd = Date(timeIntervalSince1970: ts + day)
            let pred = store.predicateForEvents(withStart: windowStart, end: windowEnd, calendars: nil)
            let candidates = store.events(matching: pred).filter { $0.eventIdentifier == identifier }
            // Prefer the candidate whose start matches most closely (handles a
            // recurring series where several instances fall in the window).
            target = candidates.min { a, b in
                abs((a.startDate?.timeIntervalSince1970 ?? .infinity) - ts)
                    < abs((b.startDate?.timeIntervalSince1970 ?? .infinity) - ts)
            }
        } else {
            target = store.event(withIdentifier: identifier)
        }
        guard let ev = target else { return }
        do {
            try store.remove(ev, span: .thisEvent)
            sync()
        } catch {
            // remove failed (write-only access); nothing else to do here
        }
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
