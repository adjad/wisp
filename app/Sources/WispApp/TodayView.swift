import SwiftUI
import AppKit

struct TodayTask: Decodable, Identifiable {
    let id: String
    let title: String
    let day: String
    let timezone: String?
    let kind: String
    let duration_minutes: Int
    let priority: Int
    let due_ts: Double?
    let pinned_start: Double?
    let status: String
    let revision: Int
    /// The day this task was first planned for, when the evening check-in or
    /// the morning carry-forward moved it here (service/assistant/checkin.py).
    let carried_from: String?
    var editingTimeZone: TimeZone { TimeZone(identifier: timezone ?? "UTC") ?? TimeZone(secondsFromGMT: 0)! }

    @MainActor func initialDeadline(fallback: Date) -> Date {
        if let due_ts { return Date(timeIntervalSince1970: due_ts) }
        return TodayModel.defaultDeadline(for: day, in: editingTimeZone) ?? fallback
    }

    func editFields(title: String, kind: String, priority: Int, minutes: Int, due: Date?, pin: Date?) -> [String: Any] {
        // Metadata edits preserve the task's original date/timezone contract.
        ["title": title, "kind": kind, "priority": priority, "duration_minutes": minutes,
         "due_ts": due.map { $0.timeIntervalSince1970 as Any } ?? NSNull(),
         "pinned_start": pin.map { $0.timeIntervalSince1970 as Any } ?? NSNull()]
    }
}
/// One email, message or note a grouped review suggestion covers.
struct TodayBlockMember: Decodable {
    let title: String
    let evidence_id: String
    let sender: String?
}

struct TodayBlock: Decodable, Identifiable {
    let id: String
    let title: String
    let start: Double
    let end: Double?
    let kind: String
    let task_id: String?
    let warnings: [String]
    let pattern_id: String?
    /// For a suggestion: the daily-workspace item it was made from.
    let evidence_id: String?
    /// For a grouped review suggestion ("Review 3 emails"): the items it covers.
    let members: [TodayBlockMember]?
    /// For a Calendar block: which synced occurrence it is and what the Day
    /// timeline's Cancel and Move need, the same facts as the check-in card's
    /// rows (service/assistant/checkin.py annotate_calendar_blocks). All absent
    /// from an older service, which then offers neither.
    let source_id: String?
    let all_day: Bool?
    let invitee_count: Int?
    let organizer_is_user: Bool?
    let calendar_writable: Bool?
    let cancel_blocked: String?
    let move_blocked: String?
    // TodayBlock.calendarEvent (CheckInView.swift) turns these into a CheckInEvent;
    // it lives there because scripts/test_today_contract.sh compiles this file alone.

    var evidenceLabel: String {
        switch kind {
        case "calendar": return "Calendar"
        case "manual": return "Fixed Wisp event"
        case "behavior": return "Tentative busy period · behavioral evidence"
        case "behavior_away": return "Computer inactivity · availability unknown"
        case "suggestion": return "Source-backed suggestion · estimated duration"
        default: return "\(kind.capitalized) · estimated duration"
        }
    }

    var inferenceIgnoreHelp: String {
        kind == "behavior_away" ? "Ignore this possible time away for the selected day" :
            "Ignore this tentative busy period for the selected day"
    }
}
struct TodayDeadline: Decodable, Identifiable {
    let id: String
    let title: String
    let due_ts: Double
    let source: String
    let all_day: Bool?
    let due_date: String?
}
struct TodayUnscheduled: Decodable, Identifiable {
    let task_id: String
    let title: String
    let reason: String
    var id: String { task_id }
}
struct TodayPreferences: Decodable {
    let start_minute: Int
    let end_minute: Int
    let not_before: Double?
    /// The person chose to plan this day although an all-day event reads like time away.
    var plan_all_day: Bool? = nil
}
struct TodaySource: Decodable {
    let ready: Bool
    let last_sync: Double?
    let reason: String
    /// "ready", "syncing" or "unavailable" (service/assistant/today.py with_access).
    var state: String? = nil
    /// What the person must grant, e.g. "calendar_access"; absent when unknown.
    var blocked: String? = nil
}
struct TodayPlan: Decodable {
    let day: String
    let timezone: String
    let generated_at: Double
    let trace_id: String?
    let revision: Int
    let provisional: Bool
    let warnings: [String]
    let preferences: TodayPreferences
    let sources: [String: TodaySource]
    let tasks: [TodayTask]
    let blocks: [TodayBlock]
    let deadlines: [TodayDeadline]
    let all_day: [TodayBlock]
    let unscheduled: [TodayUnscheduled]
    let workspace: DailyWorkspace?
    let workspace_error: String?
    var displayBlocks: [TodayBlock] { workspace?.proposed_plan.blocks ?? blocks }
    var displayUnscheduled: [TodayUnscheduled] { workspace?.proposed_plan.unscheduled ?? unscheduled }
    var displayWarnings: [String] { workspace?.proposed_plan.warnings ?? warnings }

    /// Whether Wisp could read a source and what fixes it when it couldn't:
    /// Calendar and Reminders from the plan's own source health, the rest from
    /// the workspace's coverage. Mail and texts are known only through the
    /// workspace, so without one (it failed to build or to decode) they count
    /// as unread: the Brief never gives an all-clear for them. Nil when the
    /// service said nothing about any other source.
    func access(_ source: String) -> SourceAccess? {
        if let health = sources[source] {
            return SourceAccess(source: source, ready: health.ready, state: health.state, blocked: health.blocked,
                                reason: health.reason)
        }
        if let row = workspace?.source_coverage.first(where: { $0.source == source }) {
            // Mail by the one rule the proactive section uses too
            // (SourceAccess.mail), so the top line, the tile and it agree.
            return source == "email" ? SourceAccess.mail(row.access, readBy: mailReadBy, unchecked: uncheckedMail)
                : row.access
        }
        if Self.workspaceOnly.contains(source) { return SourceAccess(source: source, ready: false, state: "unavailable") }
        return nil
    }

    /// The mail tile's state, from the same rule as access("email"): when a
    /// connected Google account read the mail, the tile reads as ready with no
    /// Mail.app access problem, as the top line says. Nil when the workspace
    /// says nothing about mail.
    /// `ready` means there is read mail to show; with an account unchecked the
    /// access is not readable, and the tile names it over what was read.
    var mailTile: (ready: Bool, access: SourceAccess?)? {
        guard let row = workspace?.source_coverage.first(where: { $0.source == "email" }) else { return nil }
        return (ready: row.ready || !mailReadBy.isEmpty, access: access("email"))
    }

    /// Google accounts whose mail Wisp read, and ones it couldn't check.
    var mailReadBy: [String] { workspace?.proactive?.mailReadBy ?? [] }
    var mailUnreadBy: [String] { workspace?.proactive?.mailUnreadBy ?? [] }
    var uncheckedMail: UncheckedMail { workspace?.proactive?.uncheckedMail ?? UncheckedMail() }

    /// Sources the plan's own health never reports.
    static let workspaceOnly: Set<String> = ["email", "messages"]

    /// The sources behind the Brief Wisp couldn't read, in reading order.
    var unreadSources: [String] {
        ["calendar", "reminders", "email", "messages"].filter { access($0).map { !$0.readable } ?? false }
    }
}

extension TodayPlan {
    private enum CodingKeys: String, CodingKey {
        case day, timezone, generated_at, trace_id, revision, provisional, warnings, preferences, sources,
             tasks, blocks, deadlines, all_day, unscheduled, workspace, workspace_error
    }

    /// What the Brief shows when the service's workspace was there but couldn't be read.
    static let unreadableWorkspace = "Some of today's details couldn't be read; your saved day plan is shown."

    /// Tolerant: only the day and time zone it is for are required. A row that
    /// doesn't decode is dropped, a missing list is empty, and a workspace this
    /// build can't read (a field changed shape) is left out with a note, so one
    /// new or changed field never blanks the whole Brief and Day.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        day = try c.decode(String.self, forKey: .day)
        timezone = try c.decode(String.self, forKey: .timezone)
        generated_at = c.lenient(.generated_at, Double.self) ?? 0
        trace_id = c.lenient(.trace_id, String.self)
        revision = c.lenient(.revision, Int.self) ?? 0
        provisional = c.lenient(.provisional, Bool.self) ?? false
        warnings = c.lenient(.warnings, LossyList<String>.self)?.items ?? []
        preferences = c.lenient(.preferences, TodayPreferences.self)
            ?? TodayPreferences(start_minute: 540, end_minute: 1080, not_before: nil)
        var sources: [String: TodaySource] = [:]
        if let nested = try? c.nestedContainer(keyedBy: AnyKey.self, forKey: .sources) {
            for key in nested.allKeys {
                if let source = nested.lenient(key, TodaySource.self) { sources[key.stringValue] = source }
            }
        }
        self.sources = sources
        tasks = c.lenient(.tasks, LossyList<TodayTask>.self)?.items ?? []
        blocks = c.lenient(.blocks, LossyList<TodayBlock>.self)?.items ?? []
        deadlines = c.lenient(.deadlines, LossyList<TodayDeadline>.self)?.items ?? []
        all_day = c.lenient(.all_day, LossyList<TodayBlock>.self)?.items ?? []
        unscheduled = c.lenient(.unscheduled, LossyList<TodayUnscheduled>.self)?.items ?? []
        workspace = c.lenient(.workspace, DailyWorkspace.self)
        let present = (try? c.decodeNil(forKey: .workspace)) == false
        workspace_error = c.lenient(.workspace_error, String.self)
            ?? (present && workspace == nil ? Self.unreadableWorkspace : nil)
    }
}

/// A composed Daily Summary the service kept (`/assistant/daily_summary/latest`),
/// read again after a relaunch, when the live event and Refresh are gone.
struct StoredBrief: Equatable {
    let text: String
    let composedAt: Date
    /// The day it was asked for, "yyyy-MM-dd".
    let day: String

    static func parse(_ obj: [String: Any], day: String) -> StoredBrief? {
        guard let text = obj["text"] as? String, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let at = (obj["composed_at"] as? NSNumber)?.doubleValue, at.isFinite, at > 0 else { return nil }
        return StoredBrief(text: text, composedAt: Date(timeIntervalSince1970: at), day: day)
    }

    /// The newer of the brief this launch received and the stored one, among
    /// those from `day` (per `sameDay`); nil when neither is.
    static func newest(live: (text: String, at: Date)?, stored: StoredBrief?,
                       sameDay: (Date) -> Bool) -> (text: String, at: Date)? {
        let candidates = [live, stored.map { (text: $0.text, at: $0.composedAt) }].compactMap { $0 }.filter { sameDay($0.at) }
        return candidates.max { $0.at < $1.at }
    }

    /// Whether the brief on screen is the one this launch received, so "Ask
    /// about this" may continue its saved chat. When the Brief shows the stored
    /// brief instead (newer, or the only one from today), the live one is a
    /// different brief, and continuing it would answer about the wrong one.
    static func shownIsLive(shown: (text: String, at: Date)?, live: (text: String, at: Date)?) -> Bool {
        guard let shown, let live else { return false }
        return shown.text == live.text && shown.at == live.at
    }
}

@MainActor
final class TodayModel: ObservableObject {
    // The one problem report about Today, shared by the notch's Day tab and
    // the window's planner. Each shows it inline.
    let report = ReportDraft()
    @Published var plan: TodayPlan?
    /// `/assistant/upcoming`'s reply (the next week's calendar events and
    /// reminders) for the notch Brief's SOON row, read with UpcomingItem.parseList.
    /// Nil until it has been read once.
    @Published private(set) var upcomingReply: [String: Any]?
    /// The day's latest composed Daily Summary as the service kept it
    /// (`/assistant/daily_summary/latest`), so the Brief shows it again after a
    /// relaunch. Nil until read, or when none was composed that day.
    @Published private(set) var storedBrief: StoredBrief?
    /// The kept summary has been asked for at least once, so its absence is known.
    @Published private(set) var storedBriefChecked = false
    @Published var error = ""
    @Published var busy = false
    /// Reads in a row that failed for a passing reason: the service starting,
    /// restarting or busy (a timeout, no connection, a 5xx). These never show
    /// as an error and never clear what is on screen; the read retries itself.
    @Published private(set) var transientFailures = 0
    /// When the current run of quiet failures began; nil while reads succeed.
    @Published private(set) var failingSince: Date?
    /// How a retry waits; checks drive it by hand.
    var retrySleep: (Duration) async throws -> Void = { try await Task.sleep(for: $0) }
    private var retryTask: Task<Void, Never>?
    /// When the plan last loaded, so Brief and Day can skip a refresh right after one.
    private(set) var lastRefreshed: Date?
    /// The person's locale decides 12- or 24-hour clocks; checks pin a fixed one.
    var clockLocale: Locale = .autoupdatingCurrent
    @Published var capturePlannerDetails = false
    private var reportMetadata: [String: Any] = ["surface": "today", "client_outcome": "not_loaded"]
    private var reportDetails: [String: Any]?
    @Published private(set) var selectedDate: Date
    private var generation = 0
    private var refreshing: (key: DayKey, ticket: Int)?
    private var backgroundRefresh: Task<Void, Never>?
    private var backgroundID: UUID?
    private var lastBriefRead: (key: DayKey, at: Date)?
    private var storedBriefKey: DayKey?
    private var afterBackgroundRefresh: (@MainActor () async -> Void)?
    private var manualDay: String?
    private var displayedKey: DayKey
    private let nowProvider: () -> Date
    private let zoneProvider: () -> TimeZone
    var transport: (URLRequest) async throws -> (Data, URLResponse)

    private struct DayKey: Equatable {
        let day: String
        let zone: String
    }

    init(timezone: TimeZone? = nil, now: @escaping () -> Date = Date.init,
         timeZoneProvider: (() -> TimeZone)? = nil,
         transport: @escaping (URLRequest) async throws -> (Data, URLResponse) = { try await URLSession.shared.data(for: $0) }) {
        nowProvider = now
        zoneProvider = timeZoneProvider ?? { timezone ?? .autoupdatingCurrent }
        let current = now()
        let zone = timeZoneProvider?() ?? timezone ?? .autoupdatingCurrent
        selectedDate = current
        displayedKey = DayKey(day: Self.dayString(current, in: zone), zone: zone.identifier)
        self.transport = transport
    }

    var timezone: TimeZone { zoneProvider() }
    /// The model's clock (the real one, or a fixture's).
    var now: Date { nowProvider() }
    var followsToday: Bool { manualDay == nil }
    var day: String { currentKey.day }
    var pickerDay: String { Self.dayString(selectedDate, in: timezone) }
    var isShowingCurrentDay: Bool { day == Self.dayString(nowProvider(), in: timezone) }

    /// Owned by the app/model, so collapsing the notch or changing tabs cannot
    /// cancel a read. Repeated starts (including a retained planner reopening)
    /// keep the same loop and its slower Brief cadence.
    func startBackgroundRefresh(interval: Duration = .seconds(30),
                                sleep: @escaping (Duration) async throws -> Void = { try await Task.sleep(for: $0) },
                                afterRefresh: (@MainActor () async -> Void)? = nil) {
        if let afterRefresh { afterBackgroundRefresh = afterRefresh }
        guard backgroundRefresh == nil else { return }
        let id = UUID()
        backgroundID = id
        backgroundRefresh = Task { [weak self] in
            while !Task.isCancelled {
                guard self != nil else { return }
                await self?.refreshBackgroundSnapshot()
                guard !Task.isCancelled else { break }
                // Nothing on screen yet (the service was still starting, or a read
                // failed): try again soon rather than leave Brief empty for the
                // full interval.
                let empty = self?.plan == nil
                do { try await sleep(empty ? .seconds(3) : interval) } catch { break }
            }
            if self?.backgroundID == id {
                self?.backgroundRefresh = nil
                self?.backgroundID = nil
            }
        }
    }

    func stopBackgroundRefresh() {
        backgroundID = nil
        backgroundRefresh?.cancel()
        backgroundRefresh = nil
    }

    private func refreshBackgroundSnapshot() async {
        guard !Task.isCancelled else { return }
        reconcileClock()
        await refresh()
        guard !Task.isCancelled else { return }
        let key = DayKey(day: Self.dayString(now, in: timezone), zone: timezone.identifier)
        let briefDue = lastBriefRead.map {
            $0.key != key || now.timeIntervalSince($0.at) < 0 || now.timeIntervalSince($0.at) >= 120
        } ?? true
        if briefDue {
            lastBriefRead = (key, now)
            await refreshUpcoming()
            guard !Task.isCancelled else { return }
            await refreshStoredBrief()
        }
        guard !Task.isCancelled else { return }
        await afterBackgroundRefresh?()
    }

    private static func dayString(_ date: Date, in zone: TimeZone) -> String {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = zone
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: date)
    }

    private var currentKey: DayKey {
        let zone = timezone
        return DayKey(day: manualDay ?? Self.dayString(nowProvider(), in: zone), zone: zone.identifier)
    }

    private static func pickerDate(for day: String, in zone: TimeZone) -> Date {
        let parts = day.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return Date() }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        return calendar.date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2], hour: 12)) ?? Date()
    }

    // A manual civil day remains selected even when the system's time zone changes.
    @discardableResult
    func reconcileClock() -> Bool {
        let key = currentKey
        guard key != displayedKey else { return false }
        displayedKey = key
        generation += 1
        plan = nil
        error = ""
        reportMetadata = ["surface": "today", "client_outcome": "not_loaded", "journal_available": false]
        reportDetails = nil
        selectedDate = manualDay == nil ? nowProvider() : Self.pickerDate(for: key.day, in: timezone)
        return true
    }

    func selectDate(_ date: Date) {
        reconcileClock()
        let zone = timezone
        let chosenDay = Self.dayString(date, in: zone)
        let today = Self.dayString(nowProvider(), in: zone)
        manualDay = chosenDay == today ? nil : chosenDay
        selectedDate = date
        reconcileClock()
    }

    func returnToToday() {
        manualDay = nil
        reconcileClock()
        selectedDate = nowProvider()
    }

    func clock(_ timestamp: Double) -> String {
        WispClock.time(timestamp, zone: timezone, locale: clockLocale)
    }

    /// The shown day's midnight-to-midnight bounds.
    var dayInterval: DateInterval {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timezone
        return calendar.dateInterval(of: .day, for: Self.pickerDate(for: day, in: timezone))
            ?? DateInterval(start: calendar.startOfDay(for: Date()), duration: 86400)
    }

    static func workingHourLabel(_ hour: Int, locale: Locale = .autoupdatingCurrent) -> String {
        let displayHour = hour % 12 == 0 ? 12 : hour % 12
        let label = WispClock.uses24Hour(locale) ? String(format: "%02d:00", hour % 24)
            : "\(displayHour):00 \(hour % 24 < 12 ? "AM" : "PM")"
        return label + (hour == 24 ? " (next day)" : "")
    }

    nonisolated static func defaultDeadline(for day: String, in zone: TimeZone) -> Date? {
        let parts = day.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        guard let noon = calendar.date(from: DateComponents(year: parts[0], month: parts[1],
                                                             day: parts[2], hour: 12)) else { return nil }
        return calendar.dateInterval(of: .day, for: noon)?.end
    }

    func request(_ path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> Data {
        guard let url = URL(string: path, relativeTo: WispClient.baseURL) else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.timeoutInterval = 15
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await transport(req)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let detail = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw NSError(domain: "Today", code: (response as? HTTPURLResponse)?.statusCode ?? 0,
                          userInfo: [NSLocalizedDescriptionKey: detail?["detail"] as? String ?? "Could not update your day. Try again."])
        }
        return data
    }

    func refresh(afterMutation: Bool = false) async {
        guard !Task.isCancelled else { return }
        reconcileClock()
        guard !busy || afterMutation else { return }
        let selected = currentKey
        // A visible planner and the app loop share one read. A mutation or a
        // different day may supersede it; the older cleanup cannot clear that read.
        if !afterMutation, refreshing?.key == selected, refreshing?.ticket == generation { return }
        generation += 1
        let ticket = generation
        refreshing = (selected, ticket)
        defer { if refreshing?.ticket == ticket { refreshing = nil } }
        var components = URLComponents()
        components.path = "/assistant/today"
        components.queryItems = [URLQueryItem(name: "day", value: selected.day), URLQueryItem(name: "timezone", value: selected.zone)]
        let capturing = capturePlannerDetails
        if capturing { components.queryItems?.append(URLQueryItem(name: "debug", value: "true")) }
        do {
            let data = try await request(components.string!)
            let next = try JSONDecoder().decode(TodayPlan.self, from: data)
            guard ticket == generation, selected == currentKey, !Task.isCancelled else { return }
            plan = next
            lastRefreshed = nowProvider()
            reportMetadata = ["surface": "today", "client_outcome": "completed", "revision": next.revision,
                              "task_count": next.tasks.count, "block_count": next.blocks.count,
                              "unscheduled_count": next.unscheduled.count, "provisional": next.provisional,
                              "journal_available": false]
            if let id = next.trace_id {
                reportMetadata["trace_id"] = id
                if let journal = DiagnosticReport.journal(traceID: id) {
                    reportMetadata["trace"] = journal
                    reportMetadata["journal_available"] = true
                }
            }
            reportDetails = capturing && capturePlannerDetails ? ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any]) : nil
            error = ""
            transientFailures = 0
            failingSince = nil
            retryTask?.cancel()
            retryTask = nil
        } catch {
            guard ticket == generation, selected == currentKey else { return }
            // View/task cancellation is normal navigation, not a failed day.
            if Task.isCancelled || error is CancellationError || (error as? URLError)?.code == .cancelled { return }
            if Self.isTransient(error) {
                // The service is starting, restarting or busy. Keep the last good
                // plan on screen (or the loading row), say nothing, and try again.
                transientFailures += 1
                if failingSince == nil { failingSince = nowProvider() }
                reportMetadata["client_outcome"] = "refresh_retrying"
                reportMetadata["client_error_code"] = (error as NSError).code
                scheduleRetry()
                return
            }
            self.error = error is DecodingError
                ? "Wisp couldn't read today's plan from its service." : error.localizedDescription
            reportMetadata = ["surface": "today", "client_outcome": "refresh_failed", "journal_available": false,
                              "client_error_code": (error as NSError).code]
            reportDetails = nil
            transientFailures = 0
            failingSince = nil
            // A refused or unreadable plan must not leave stale slots looking actionable.
            plan = nil
        }
    }

    /// Whether a failed read is a passing condition worth retrying quietly: no
    /// connection yet, a timeout, a dropped connection, or the service saying it
    /// is busy or failing (429, 5xx). A refusal (4xx) or an unreadable reply is real.
    nonisolated static func isTransient(_ error: Error) -> Bool {
        let failure = error as NSError
        if failure.domain == NSURLErrorDomain {
            return [NSURLErrorCannotConnectToHost, NSURLErrorTimedOut, NSURLErrorNetworkConnectionLost,
                    NSURLErrorNotConnectedToInternet, NSURLErrorCannotFindHost, NSURLErrorDNSLookupFailed,
                    NSURLErrorBadServerResponse, NSURLErrorResourceUnavailable,
                    NSURLErrorZeroByteResource].contains(failure.code)
        }
        if failure.domain == "Today" {
            return failure.code == 0 || failure.code == 429 || (500...599).contains(failure.code)
        }
        return false
    }

    /// Seconds before the next quiet retry after `failures` failed reads in a row.
    nonisolated static func retryDelay(after failures: Int) -> Duration {
        .seconds(min(15, 1 << min(max(failures - 1, 0), 4)))
    }

    /// Wisp still can't reach its service after several quiet retries, with
    /// nothing to show yet: said once, in the loading row, while it keeps trying.
    var outageNotice: String? {
        plan == nil && transientFailures >= 4 ? "Wisp is taking a while to start. Still trying…" : nil
    }

    /// How long a plan may stay on screen while reads keep failing before the
    /// Day says it is out of date and holds back its changing buttons.
    nonisolated static let staleAfter: TimeInterval = 90

    /// The plan on screen is from before a run of failed reads that has lasted
    /// `staleAfter`: "Wisp isn't answering right now. Showing your day as of 3:42 PM."
    var staleNotice: String? {
        guard plan != nil, let since = failingSince, nowProvider().timeIntervalSince(since) >= Self.staleAfter else { return nil }
        let asOf = lastRefreshed.map { " Showing your day as of " + clock($0.timeIntervalSince1970) + "." } ?? ""
        return "Wisp isn't answering right now." + asOf
    }

    /// Changes are held back while the plan on screen may be out of date.
    var planIsStale: Bool { staleNotice != nil }

    private func scheduleRetry() {
        guard retryTask == nil else { return }
        let delay = Self.retryDelay(after: transientFailures)
        let sleep = retrySleep
        retryTask = Task { [weak self] in
            do { try await sleep(delay) } catch { return }
            guard let self, !Task.isCancelled else { return }
            self.retryTask = nil
            await self.refresh()
        }
    }

    /// A failed read keeps the last reply, so SOON never empties on a hiccup.
    func refreshUpcoming() async {
        let key = DayKey(day: Self.dayString(now, in: timezone), zone: timezone.identifier)
        guard let data = try? await request("/assistant/upcoming?days=7"),
              let reply = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              reply["commitments"] is [[String: Any]], !Task.isCancelled,
              key == DayKey(day: Self.dayString(now, in: timezone), zone: timezone.identifier) else { return }
        upcomingReply = reply
    }

    /// Reads today's latest composed brief. A 404 means none was composed today;
    /// any other failure keeps what was read before.
    func refreshStoredBrief() async {
        let day = Self.dayString(nowProvider(), in: timezone)
        let zone = timezone.identifier
        let key = DayKey(day: day, zone: zone)
        if storedBriefKey != key {
            storedBriefKey = key
            storedBrief = nil
            storedBriefChecked = false
        }
        func stillCurrent() -> Bool {
            day == Self.dayString(nowProvider(), in: timezone) && zone == timezone.identifier && !Task.isCancelled
        }
        do {
            let data = try await request("/assistant/daily_summary/latest?day=\(day)")
            guard let reply = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  let brief = StoredBrief.parse(reply, day: day), stillCurrent() else { return }
            storedBrief = brief
            storedBriefChecked = true
        } catch {
            guard stillCurrent(), !(error is CancellationError), (error as? URLError)?.code != .cancelled else { return }
            let failure = error as NSError
            if failure.domain == "Today", failure.code == 404 { storedBrief = nil }
            // Any answer settles it: a service that can't say still shows the note.
            storedBriefChecked = true
        }
    }

    func diagnosticSnapshot() -> (metadata: [String: Any], details: [String: Any]?) {
        (reportMetadata, capturePlannerDetails ? reportDetails : nil)
    }

    func reportProblem() {
        let snapshot = diagnosticSnapshot()
        let trace = snapshot.metadata["trace_id"] as? String ?? snapshot.metadata["refresh_trace_id"] as? String
        report.open(key: "today:" + (trace ?? UUID().uuidString), metadata: snapshot.metadata, details: snapshot.details)
    }

    /// Turning capture off drops captured planner details, including from an
    /// open report that holds a copy of them.
    func clearPlannerDetails() {
        reportDetails = nil
        if report.snapshot?.hasDetails == true { report.close() }
    }

    @discardableResult
    func mutate(_ path: String, method: String, body: [String: Any]) async -> Bool {
        guard !busy else { return false }
        busy = true
        generation += 1 // invalidate any in-flight refresh before writing
        var failure: String?
        var failureCode: Int?
        do { _ = try await request(path, method: method, body: body) }
        catch { failure = error.localizedDescription; failureCode = (error as NSError).code }
        await refresh(afterMutation: true)
        busy = false
        if let failure {
            error = failure
            if let refreshID = reportMetadata.removeValue(forKey: "trace_id") {
                reportMetadata["refresh_trace_id"] = refreshID
            }
            if let refreshTrace = reportMetadata.removeValue(forKey: "trace") {
                reportMetadata["refresh_trace"] = refreshTrace
            }
            reportMetadata["journal_available"] = false
            reportMetadata["client_outcome"] = "mutation_failed"
            reportMetadata["mutation_method"] = method
            reportMetadata["client_error_code"] = failureCode
            reportDetails = nil
        }
        return failure == nil
    }

    func add(title: String, kind: String, minutes: Int, priority: Int, due: Date?, pinned: Date? = nil,
             origin: Double? = nil) async -> Bool {
        var body: [String: Any] = ["title": title, "day": day, "timezone": timezone.identifier, "kind": kind,
                                   "duration_minutes": minutes, "priority": priority]
        if let due { body["due_ts"] = due.timeIntervalSince1970 }
        if let pinned { body["pinned_start"] = pinned.timeIntervalSince1970 }
        // When what it came from first mattered, so the morning carry-forward ages it from there.
        if let origin { body["origin_ts"] = origin }
        return await mutate("/assistant/today/tasks", method: "POST", body: body)
    }

    @discardableResult
    func edit(_ task: TodayTask, values: [String: Any]) async -> Bool {
        var body = values
        body["revision"] = task.revision
        return await mutate("/assistant/today/tasks/\(task.id)", method: "PATCH", body: body)
    }

    func replan(start: Int, end: Int, delayMinutes: Int? = nil, planAllDay: Bool? = nil) async {
        guard let plan, plan.day == day else { return }
        var body: [String: Any] = ["day": day, "timezone": timezone.identifier,
                                  "revision": plan.revision, "start_minute": start, "end_minute": end]
        // A replan keeps the person's "Plan my day anyway" unless it changes it.
        if planAllDay ?? (plan.preferences.plan_all_day == true) { body["plan_all_day"] = true }
        if let delayMinutes {
            body["not_before"] = Date().addingTimeInterval(Double(delayMinutes * 60)).timeIntervalSince1970
        } else {
            body["not_before"] = NSNull() // explicit replan clears a prior delay
        }
        _ = await mutate("/assistant/today/replan", method: "POST", body: body)
    }
}

struct TodayPresentation: Equatable {
    let day: String
    let pickerDay: String
    let timezone: String
    let followsToday: Bool
    let loading: Bool
    let blockTitles: [String]
}

struct TodayDeadlineDraft {
    private(set) var enabled = false
    private(set) var date = Date()
    private(set) var usesDefault = false
    private var isInitialized = false

    mutating func setEnabled(_ enabled: Bool, day: String, zone: TimeZone) {
        self.enabled = enabled
        guard enabled, !isInitialized else { return }
        resetToDefault(day: day, zone: zone)
    }

    @discardableResult
    mutating func resetToDefault(day: String, zone: TimeZone) -> Bool {
        guard let defaultDate = TodayModel.defaultDeadline(for: day, in: zone) else { return false }
        date = defaultDate
        usesDefault = true
        isInitialized = true
        return true
    }

    mutating func selectDate(_ date: Date) {
        self.date = date
        usesDefault = false
        isInitialized = true
    }

    mutating func rebaseDefault(day: String, zone: TimeZone) {
        guard isInitialized, usesDefault,
              let defaultDate = TodayModel.defaultDeadline(for: day, in: zone) else { return }
        date = defaultDate
    }

    var selectedDate: Date? { enabled ? date : nil }
}

struct TodayView: View {
    @StateObject private var model: TodayModel
    private let onPresentation: ((TodayPresentation) -> Void)?
    @State private var title = ""
    @State private var kind = "study"
    @State private var minutes = 45
    @State private var priority = 2
    @State private var deadline = TodayDeadlineDraft()
    @State private var startHour = 9
    @State private var endHour = 18
    @State private var hoursDirty = false
    @State private var editingTask: TodayTask?
    @State private var selectedTab = 1
    @State private var showingEmailSettings = false

    @MainActor
    init() { self.init(model: TodayModel()) }

    @MainActor
    init(model: TodayModel, onPresentation: ((TodayPresentation) -> Void)? = nil, initialTab: Int = 1) {
        _model = StateObject(wrappedValue: model)
        _selectedTab = State(initialValue: initialTab)
        self.onPresentation = onPresentation
    }

    private var visiblePlan: TodayPlan? {
        guard let plan = model.plan, plan.day == model.day,
              plan.timezone == model.timezone.identifier else { return nil }
        return plan
    }

    private var presentation: TodayPresentation {
        let plan = visiblePlan
        return TodayPresentation(day: model.day, pickerDay: model.pickerDay,
                                 timezone: model.timezone.identifier,
                                 followsToday: model.followsToday, loading: plan == nil,
                                 blockTitles: plan?.blocks.map(\.title) ?? [])
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading) {
                    Text("Today").font(.largeTitle.bold())
                    Text("What matters, and a realistic plan for it.").foregroundStyle(.secondary)
                }
                Spacer()
                DatePicker("Day", selection: Binding(get: { model.selectedDate }, set: { date in
                    model.selectDate(date)
                    rebaseDefaultDeadline()
                    hoursDirty = false
                    Task { await model.refresh() }
                }), displayedComponents: .date).labelsHidden()
                    .environment(\.timeZone, model.timezone)
                    .disabled(model.busy)
                if !model.followsToday {
                    Button("Today") {
                        model.returnToToday()
                        rebaseDefaultDeadline()
                        hoursDirty = false
                        Task { await model.refresh() }
                    }.disabled(model.busy)
                }
                Button("Refresh") { Task { await model.refresh() } }.disabled(model.busy)
                Button("Report a problem") { model.reportProblem() }
            }
            ReportDraftCard(draft: model.report, title: "Report a problem with this plan",
                            detailsLabel: "Include planner details", look: .window,
                            save: { model.report.chooseLocationAndSave() })
            Toggle("Capture planner details for a problem report", isOn: $model.capturePlannerDetails)
                .font(.caption)
                .help("Includes private task and calendar titles. Capture starts on the next refresh; reports ask again before including it.")
                .onChange(of: model.capturePlannerDetails) {
                    model.clearPlannerDetails()
                    Task { await model.refresh() }
                }
            if !model.error.isEmpty { Text(model.error).foregroundStyle(.red).textSelection(.enabled) }
            HStack {
                Picker("Daily summary view", selection: $selectedTab) {
                    Text("Needs my attention").tag(0)
                    Text("Day plan").tag(1)
                }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 460)
                Spacer()
                Button("Email my day plan…", systemImage: "envelope") { showingEmailSettings = true }
            }
            if let plan = visiblePlan {
                sourceStatus(plan)
                if let error = plan.workspace_error { Text(error).font(.caption).foregroundStyle(.orange) }
                if selectedTab == 0 {
                    DailyAttentionView(workspace: plan.workspace, model: model)
                } else {
                workingHours(plan)
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        if !plan.displayWarnings.isEmpty {
                            GroupBox("Plan needs attention") {
                                VStack(alignment: .leading, spacing: 4) {
                                    ForEach(plan.displayWarnings, id: \.self) { Text($0).frame(maxWidth: .infinity, alignment: .leading) }
                                }.padding(4)
                            }
                        }
                        agenda(plan)
                        if let workspace = plan.workspace {
                            Text(workspace.explanation).font(.caption).foregroundStyle(.secondary)
                            Text(workspace.behavior_detail).font(.caption).foregroundStyle(.secondary)
                        }
                        if !plan.displayUnscheduled.isEmpty {
                            GroupBox("Still to fit") {
                                VStack(alignment: .leading, spacing: 10) {
                                    ForEach(plan.displayUnscheduled) { item in
                                        VStack(alignment: .leading) {
                                            Text(item.title).bold()
                                            Text(item.reason).font(.caption).foregroundStyle(.secondary)
                                        }.frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                }.padding(6)
                            }
                        }
                        if !plan.deadlines.isEmpty {
                            GroupBox("Deadlines · these do not reserve time") {
                                VStack(alignment: .leading, spacing: 8) {
                                    ForEach(plan.deadlines) { item in
                                        Text("\((item.all_day == true ? "All day" : model.clock(item.due_ts)))  ·  \(item.title)  (\(item.source))")
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                }.padding(6)
                            }
                        }
                        DisclosureGroup("Make time for something else") { taskForm }
                        taskList(plan)
                        Text("Calendar and Reminders stay unchanged. Task durations are your estimates. Flexible slots update as your day changes; pin one to keep its time.")
                            .font(.caption).foregroundStyle(.secondary)
                    }.padding(.trailing, 6)
                }
                }
            } else {
                ContentUnavailableView("Your day is loading", systemImage: "calendar",
                                       description: Text("Wisp’s local service needs to be running."))
            }
        }
        // Fits the Wisp window's main column beside its sidebar.
        .padding(22).frame(minWidth: 540, minHeight: 520, alignment: .topLeading)
        .onAppear { onPresentation?(presentation) }
        .onChange(of: presentation) { _, next in
            rebaseDefaultDeadline()
            onPresentation?(next)
        }
        .sheet(item: $editingTask) { task in TodayTaskEditor(task: task, model: model) }
        .sheet(isPresented: $showingEmailSettings) { DailyEmailSettingsView(today: model) }
        .task {
            model.startBackgroundRefresh()
            while !Task.isCancelled {
                if model.reconcileClock() { hoursDirty = false }
                rebaseDefaultDeadline()
                do { try await Task.sleep(for: .seconds(30)) } catch { break }
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .NSCalendarDayChanged)) { _ in refreshForClockChange() }
        .onReceive(NotificationCenter.default.publisher(for: .NSSystemTimeZoneDidChange)) { _ in refreshForClockChange() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in refreshForClockChange() }
        .onReceive(NSWorkspace.shared.notificationCenter.publisher(for: NSWorkspace.didWakeNotification)) { _ in refreshForClockChange() }
        .onReceive(NotificationCenter.default.publisher(for: TodayWindow.didShowNotification)) { note in
            if let tab = note.userInfo?["tab"] as? Int { selectedTab = tab }
            refreshForClockChange()
        }
        .onChange(of: model.plan?.generated_at) { _, _ in
            if !hoursDirty, let p = model.plan {
                startHour = p.preferences.start_minute / 60
                endHour = p.preferences.end_minute / 60
            }
        }
    }

    private func refreshForClockChange() {
        if model.reconcileClock() { hoursDirty = false }
        rebaseDefaultDeadline()
        Task { await model.refresh() }
    }

    private func rebaseDefaultDeadline() {
        deadline.rebaseDefault(day: model.day, zone: model.timezone)
    }

    private func sourceStatus(_ plan: TodayPlan) -> some View {
        HStack(spacing: 18) {
            ForEach(["calendar", "reminders"], id: \.self) { source in
                if let info = plan.sources[source] {
                    Label(source.capitalized + ": " + info.reason,
                          systemImage: info.ready ? "checkmark.circle" : "clock.badge.exclamationmark")
                        .font(.caption).foregroundStyle(info.ready ? Color.secondary : Color.orange)
                        .help(info.last_sync.map { "Last sync: \(model.clock($0))" } ?? "No sync yet")
                }
            }
        }
    }

    private func workingHours(_ plan: TodayPlan) -> some View {
        HStack {
            Text("Working hours")
            Picker("Start", selection: $startHour) {
                ForEach(0..<24) { Text(TodayModel.workingHourLabel($0)).tag($0) }
            }.labelsHidden().frame(width: 170)
            Text("to")
            Picker("End", selection: $endHour) {
                ForEach(1..<25) { Text(TodayModel.workingHourLabel($0)).tag($0) }
            }.labelsHidden().frame(width: 170)
            Button("Replan") {
                Task { await model.replan(start: startHour * 60, end: endHour * 60); hoursDirty = false }
            }.disabled(startHour >= endHour || model.busy)
            Spacer()
            Button("Running 15 min late") {
                Task { await model.replan(start: plan.preferences.start_minute, end: plan.preferences.end_minute, delayMinutes: 15) }
            }.disabled(model.busy || !model.isShowingCurrentDay)
        }
        .onChange(of: startHour) { _, _ in hoursDirty = true }
        .onChange(of: endHour) { _, _ in hoursDirty = true }
    }

    private var taskForm: some View {
        GroupBox("Make time for something") {
            VStack(alignment: .leading, spacing: 10) {
                TextField("Study for an exam, finish a project…", text: $title)
                HStack {
                    Picker("Type", selection: $kind) {
                        Text("Study").tag("study"); Text("Project").tag("project"); Text("Task").tag("task")
                    }.frame(width: 150)
                    Stepper("\(minutes) min estimate", value: $minutes, in: 5...480, step: 5)
                    Picker("Priority", selection: $priority) {
                        Text("High").tag(1); Text("Normal").tag(2); Text("Low").tag(3)
                    }.frame(width: 160)
                }
                HStack {
                    Toggle("Deadline", isOn: Binding(get: { deadline.enabled }, set: { enabled in
                        deadline.setEnabled(enabled, day: model.day, zone: model.timezone)
                    }))
                    if deadline.enabled {
                        DatePicker("Due", selection: Binding(get: { deadline.date }, set: { deadline.selectDate($0) }))
                            .labelsHidden()
                            .environment(\.timeZone, model.timezone)
                            .environment(\.locale, Locale(identifier: "en_US"))
                        if !deadline.usesDefault {
                            Button("Reset to day end") {
                                deadline.resetToDefault(day: model.day, zone: model.timezone)
                            }.help("Use midnight at the end of the selected day")
                        }
                    }
                    Spacer()
                    Button("Add to day") {
                        Task {
                            rebaseDefaultDeadline()
                            if await model.add(title: title, kind: kind, minutes: minutes, priority: priority,
                                               due: deadline.selectedDate) {
                                title = ""
                            }
                        }
                    }.disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.busy)
                }
            }.padding(6)
        }.disabled(model.busy)
    }

    private func agenda(_ plan: TodayPlan) -> some View {
        GroupBox("Itinerary") {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(plan.all_day) { block in Text("All day  ·  \(block.title)").foregroundStyle(.secondary) }
                if plan.displayBlocks.isEmpty { Text("No time blocks yet.").foregroundStyle(.secondary) }
                ForEach(plan.displayBlocks) { block in
                    HStack(alignment: .top) {
                        Text(model.clock(block.start) + (block.end.map { "–" + model.clock($0) } ?? "–?"))
                            .font(.caption.monospacedDigit()).frame(width: 148, alignment: .leading)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(block.title).bold()
                            Text(block.evidenceLabel)
                                .font(.caption).foregroundStyle(.secondary)
                            ForEach(Array(block.warnings.enumerated()), id: \.offset) { _, warning in
                                Text(warning).font(.caption).foregroundStyle(.orange)
                            }
                        }
                        Spacer()
                        if let pattern = block.pattern_id {
                            Button("Use this time") {
                                Task {
                                    _ = await model.mutate("/assistant/today/workspace/availability", method: "POST",
                                        body: ["day": model.day, "timezone": model.timezone.identifier,
                                               "window": pattern, "ignored": true])
                                }
                            }.help(block.inferenceIgnoreHelp).disabled(model.busy)
                        }
                        if let id = block.task_id, let task = plan.tasks.first(where: { $0.id == id }) {
                            Button(task.pinned_start == nil ? "Pin" : "Unpin") {
                                Task {
                                    let values: [String: Any] = task.pinned_start == nil
                                        ? ["pinned_start": block.start, "day": model.day, "timezone": model.timezone.identifier]
                                        : ["pinned_start": NSNull()]
                                    await model.edit(task, values: values)
                                }
                            }.disabled(model.busy)
                        }
                    }
                    Divider()
                }
            }.padding(6).frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func taskList(_ plan: TodayPlan) -> some View {
        GroupBox("Your tasks") {
            VStack(alignment: .leading, spacing: 10) {
                if plan.tasks.isEmpty { Text("Add your first study or project block above.").foregroundStyle(.secondary) }
                ForEach(plan.tasks.filter { $0.status != "dismissed" }) { task in
                    HStack {
                        Button {
                            Task { await model.edit(task, values: ["status": task.status == "done" ? "active" : "done"]) }
                        } label: { Image(systemName: task.status == "done" ? "checkmark.circle.fill" : "circle") }
                        .buttonStyle(.plain).help(task.status == "done" ? "Reopen task" : "Mark done")
                        Text(task.title).strikethrough(task.status == "done")
                        Spacer()
                        Text("\(task.duration_minutes) min")
                        Button("Edit") { editingTask = task }
                        Button("−15") { Task { await model.edit(task, values: ["duration_minutes": max(1, task.duration_minutes - 15)]) } }
                        Button("+15") { Task { await model.edit(task, values: ["duration_minutes": min(1440, task.duration_minutes + 15)]) } }
                        Button("Remove") { Task { await model.edit(task, values: ["status": "dismissed"]) } }
                    }.disabled(model.busy)
                }
            }.padding(6)
        }
    }
}

private struct TodayTaskEditor: View {
    let task: TodayTask
    @ObservedObject var model: TodayModel
    @Environment(\.dismiss) private var dismiss
    @State private var title: String
    @State private var minutes: Int
    @State private var priority: Int
    @State private var kind: String
    @State private var hasDue: Bool
    @State private var due: Date
    @State private var pinned: Bool
    @State private var start: Date

    init(task: TodayTask, model: TodayModel) {
        self.task = task
        self.model = model
        _title = State(initialValue: task.title)
        _minutes = State(initialValue: task.duration_minutes)
        _priority = State(initialValue: task.priority)
        _kind = State(initialValue: task.kind)
        _hasDue = State(initialValue: task.due_ts != nil)
        _due = State(initialValue: task.initialDeadline(fallback: model.selectedDate))
        _pinned = State(initialValue: task.pinned_start != nil)
        _start = State(initialValue: Date(timeIntervalSince1970: task.pinned_start ?? model.selectedDate.timeIntervalSince1970))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Edit task").font(.title2.bold())
            Text("Times shown in \(task.editingTimeZone.identifier)").font(.caption).foregroundStyle(.secondary)
            TextField("Title", text: $title)
            Picker("Type", selection: $kind) {
                Text("Study").tag("study"); Text("Project").tag("project"); Text("Task").tag("task")
            }
            Stepper("Estimated duration: \(minutes) min", value: $minutes, in: 1...1440)
            Picker("Priority", selection: $priority) {
                Text("High").tag(1); Text("Normal").tag(2); Text("Low").tag(3)
            }
            Toggle("Has a deadline", isOn: $hasDue)
            if hasDue { DatePicker("Due", selection: $due).environment(\.locale, Locale(identifier: "en_US")) }
            Toggle("Pin a time", isOn: $pinned)
            if pinned {
                DatePicker("Start on \(task.day)", selection: $start)
                    .environment(\.locale, Locale(identifier: "en_US"))
                Text("Pins keep their time during replanning. Conflicts are shown in the itinerary.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if !model.error.isEmpty { Text(model.error).foregroundStyle(.red) }
            HStack {
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Save") {
                    Task {
                        if await model.edit(task, values: task.editFields(title: title, kind: kind, priority: priority,
                            minutes: minutes, due: hasDue ? due : nil, pin: pinned ? start : nil)) { dismiss() }
                    }
                }.keyboardShortcut(.defaultAction)
                    .disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.padding(24).frame(width: 470).disabled(model.busy)
            .environment(\.timeZone, task.editingTimeZone)
    }
}

/// A standalone host for TodayView. The app shows the planner as the Wisp
/// window's Today section (TodayHomeView); this host remains for the mounted
/// window checks in tests/TodayPlanChecks.swift, and its notification tells a
/// planner on screen to switch tabs and re-read the clock.
@MainActor
enum TodayWindow {
    static let didShowNotification = Notification.Name("WispTodayWindowDidShow")
    private static var window: NSWindow?
    @discardableResult
    static func show(model: TodayModel? = nil, onPresentation: ((TodayPresentation) -> Void)? = nil,
                     present: Bool = true, initialTab: Int = 1) -> NSWindow {
        if window == nil {
            let created = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 860, height: 800),
                                   styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            created.title = "Wisp · Today"
            created.contentView = NSHostingView(rootView: TodayView(model: model ?? TodayModel(),
                                                                    onPresentation: onPresentation, initialTab: initialTab))
            created.isReleasedWhenClosed = false
            created.center()
            window = created
        }
        let existing = window!
        if present {
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        NotificationCenter.default.post(name: didShowNotification, object: nil, userInfo: ["tab": initialTab])
        return existing
    }
}
