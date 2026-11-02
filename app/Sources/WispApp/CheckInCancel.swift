import Foundation

/// The one-tap Cancel on the Day tab's check-in card (CheckInView.swift): its
/// wording, the request it sends, how the service's answer is read, and the
/// Undo windows themselves. Kept free of SwiftUI so
/// tests/CheckInCancelChecks.swift can run all of it offline.
///
/// Tapping Cancel does not cancel yet: the row turns into an Undo bar for
/// `undoWindow` seconds, and only then is POST /assistant/checkin/cancel_event
/// sent (service/assistant/checkin_api.py), which removes exactly that
/// occurrence through the app's Calendar handler and reads back that it is gone.
enum CheckInCancel {
    /// How long Undo stays before the cancel is sent (user decision, 2026-10-18).
    static let undoWindow: TimeInterval = 5

    /// How long the card waits for the service's answer. The service itself
    /// waits up to 8 s for the app to reconnect and 45 s for the app's answer
    /// (service/assistant/outbox.py), so giving up sooner would call a cancel
    /// that may still complete a failure.
    static let requestTimeout: TimeInterval = 70

    /// The bar while Undo is still possible. The count is what the service
    /// synced for this occurrence; the same number goes back in the request.
    static func undoLabel(inviteeCount: Int) -> String {
        guard inviteeCount > 0 else { return "Cancelled" }
        return "Cancelled · its \(inviteeCount) invitee\(inviteeCount == 1 ? "" : "s") will be notified"
    }

    /// What the card says once the service verified the cancel.
    static func doneLabel(title: String, inviteeCount: Int) -> String {
        let base = "“\(title)” cancelled"
        guard inviteeCount > 0 else { return base }
        return base + " · \(inviteeCount) invitee\(inviteeCount == 1 ? "" : "s") notified"
    }

    /// Why a row offers no Cancel: the service's reason when it gave one, else
    /// the row predates the synced invitee count the service insists on.
    static func blockedReason(_ serviceReason: String) -> String {
        serviceReason.isEmpty ? "Wisp hasn't synced this event's details yet; cancel it in Calendar." : serviceReason
    }

    /// What the row shows in Cancel's place when a one-tap Cancel isn't offered.
    enum BlockedControl: Equatable {
        case cancel        // offered
        case readOnly      // a subscribed/read-only calendar: nothing can be done from here
        case decline       // someone else's event: "Decline in Calendar"
        case open          // not synced / organizer unknown: "Open in Calendar"
    }

    /// A read-only calendar wins over everything (even the service's reason), then an
    /// invitation gets Decline; anything else that blocks Cancel opens the event.
    static func blockedControl(canCancel: Bool, calendarWritable: Bool?, organizerIsUser: Bool?) -> BlockedControl {
        if calendarWritable == false { return .readOnly }
        if canCancel { return .cancel }
        return organizerIsUser == false ? .decline : .open
    }

    /// One entry of the Day timeline's ⋯ menu for a Calendar block (NotchDayView.swift).
    enum MenuEntry: Equatable {
        case cancel            // "Cancel event": the 5-second Undo, then the verified cancel
        case move              // "Move…": the check-in card's Move sub-panel
        case reason(String)    // a disabled line: why neither is offered
        case decline           // "Decline in Calendar ↗": someone else's event
        case open              // "Open in Calendar ↗": the exact occurrence
    }

    /// What the timeline's ⋯ menu offers for one Calendar block, in order: the
    /// check-in card's rules (blockedControl, CheckInEvent.canMove), and two of
    /// its own. An event that is already over gets nothing to change, only
    /// Open; and `described` is false when the service didn't send the block's
    /// Cancel/Move facts (an older service), which must never read as allowed.
    /// A read-only calendar says so on a disabled line; any other block on
    /// Cancel gives the service's reason the same way.
    static func menuEntries(described: Bool, canCancel: Bool, canMove: Bool, calendarWritable: Bool?,
                            organizerIsUser: Bool?, cancelReason: String, isPast: Bool) -> [MenuEntry] {
        guard described, !isPast else { return [.open] }
        switch blockedControl(canCancel: canCancel, calendarWritable: calendarWritable, organizerIsUser: organizerIsUser) {
        case .cancel: return [.cancel] + (canMove ? [.move] : []) + [.open]
        case .readOnly: return [.reason("On a read-only calendar"), .open]
        case .decline: return [.decline]
        case .open: return [.reason(blockedReason(cancelReason)), .open]
        }
    }

    /// The Cancel entry's words. An event with no invitees is only the person's
    /// own, so removing it reads as "Delete event"; with invitees Calendar tells
    /// them, so it stays "Cancel event". Not synced yet (nil) never reads as none.
    static func cancelLabel(inviteeCount: Int?, retry: Bool) -> String {
        retry ? "Try cancelling again" : inviteeCount == 0 ? "Delete event" : "Cancel event"
    }

    /// The body of POST /assistant/checkin/cancel_event, or nil when the row lacks
    /// what the service rejects without: the calendar id, the start the card
    /// showed, and a synced invitee count (null means not synced).
    static func requestBody(sourceId: String?, startTs: Double?, inviteeCount: Int?) -> [String: Any]? {
        guard let sourceId, !sourceId.isEmpty,
              let startTs, startTs.isFinite, startTs > 0,
              let inviteeCount, inviteeCount >= 0 else { return nil }
        return ["source_id": sourceId, "expected_start_ts": startTs, "expected_invitees": inviteeCount]
    }

    struct Failure: Equatable {
        /// The service's detail.outcome (not_found, not_allowed, not_current,
        /// unknown, unavailable, failed), or from this side: unreachable (the
        /// request never left), no_answer (it may have), invalid, changed.
        let outcome: String
        let message: String

        /// The cancel may have happened: the request reached the service (or
        /// may have) and no verified answer came back. The card then offers no
        /// tap that would send another one; the person checks Calendar.
        var isUncertain: Bool { outcome == "unknown" || outcome == "no_answer" }

        /// Try again is offered only when nothing was cancelled and a retry
        /// could succeed. Never after an uncertain outcome, for a missing event,
        /// for one that isn't the user's to cancel, or for a malformed body.
        var canRetry: Bool { ["not_current", "unavailable", "failed", "unreachable", "changed"].contains(outcome) }
    }

    /// Transport errors that prove the request never reached the service. A
    /// connection lost mid-request is not one: the service may have handed the
    /// cancel to the app and died while waiting for the answer.
    static let notSent: Set<URLError.Code> = [.cannotConnectToHost, .cannotFindHost, .dnsLookupFailed,
                                              .notConnectedToInternet]

    /// Reads the answer. nil on a verified 200 {ok: true}; otherwise what to
    /// show. Every deliberate refusal carries detail {outcome, message}; a 422
    /// from a malformed body carries pydantic's list instead and gets a plain
    /// line. No status with a transport error in `notSent` means nothing was
    /// cancelled. Any other missing answer (a timeout, a dropped connection)
    /// and any error without a structured outcome (a 500 from an unknown point
    /// in the handler, possibly after the app already cancelled) is uncertain.
    static func failure(status: Int?, body: [String: Any]?, transport: URLError.Code? = nil) -> Failure? {
        let uncertain = Failure(outcome: "unknown", message: "Wisp couldn't confirm whether it was cancelled. Check Calendar before trying again.")
        guard let status else {
            if let transport, notSent.contains(transport) {
                return Failure(outcome: "unreachable", message: "Wisp's service isn't reachable, so nothing was cancelled.")
            }
            return Failure(outcome: "no_answer", message: "Wisp's service didn't answer in time. Check Calendar: it may have been cancelled.")
        }
        if (200..<300).contains(status) {
            return body?["ok"] as? Bool == true ? nil : uncertain
        }
        if status == 422 {
            return Failure(outcome: "invalid", message: "Couldn't cancel that. Nothing was cancelled.")
        }
        let detail = body?["detail"] as? [String: Any]
        guard let outcome = detail?["outcome"] as? String, let message = detail?["message"] as? String else {
            return uncertain
        }
        return Failure(outcome: outcome, message: message)
    }

    /// The row the card reloaded no longer matches the one Cancel was tapped
    /// on, so that cancel is dropped before it is sent.
    static func changed(title: String) -> Failure {
        Failure(outcome: "changed", message: "“\(title)” changed since you tapped Cancel; nothing was cancelled.")
    }

    /// The invitee count a verified answer reports, falling back to the row's.
    static func notifiedCount(body: [String: Any]?, rowCount: Int?) -> Int {
        if let count = (body?["invitee_count"] as? NSNumber)?.intValue, count >= 0 { return count }
        return max(0, rowCount ?? 0)
    }
}

/// The Undo windows: one timer per event id, all on the main actor. A window
/// opens with `begin`, closes with `undo`, or runs out and calls `commit` once,
/// after which the id is in flight until `commit` returns. The sleep and the
/// commit are injected so the checks can drive them without waiting.
@MainActor
final class CheckInCancelQueue {
    struct Entry: Equatable {
        let id: String
        let startedAt: Date
    }

    private(set) var pending: [String: Entry] = [:]
    private(set) var inFlight: Set<String> = []
    private var timers: [String: Task<Void, Never>] = [:]
    let window: TimeInterval
    private let sleep: @MainActor (TimeInterval) async throws -> Void
    private let commit: @MainActor (String) async -> Void
    /// Called after every change to `pending` or `inFlight`.
    var onChange: (@MainActor () -> Void)?

    init(window: TimeInterval = CheckInCancel.undoWindow,
         sleep: @escaping @MainActor (TimeInterval) async throws -> Void = { try await Task.sleep(for: .seconds($0)) },
         commit: @escaping @MainActor (String) async -> Void) {
        self.window = window
        self.sleep = sleep
        self.commit = commit
    }

    var isEmpty: Bool { pending.isEmpty && inFlight.isEmpty }
    func isActive(_ id: String) -> Bool { pending[id] != nil || inFlight.contains(id) }

    /// Opens the window. false when one is already open for this id or its
    /// cancel is being sent, so a second tap never sends a second request.
    @discardableResult
    func begin(_ id: String, now: Date = Date()) -> Bool {
        guard !isActive(id) else { return false }
        pending[id] = Entry(id: id, startedAt: now)
        timers[id] = Task { [weak self] in
            guard let self else { return }
            do { try await self.sleep(self.window) } catch { return }  // Undo cancelled the wait
            // Undo may have landed while the sleep was ending: then nothing is sent.
            guard !Task.isCancelled, self.pending[id] != nil else { return }
            self.pending[id] = nil
            self.timers[id] = nil
            self.inFlight.insert(id)
            self.onChange?()
            await self.commit(id)
            self.inFlight.remove(id)
            self.onChange?()
        }
        onChange?()
        return true
    }

    /// Closes the window without sending. false when none is open (too late).
    @discardableResult
    func undo(_ id: String) -> Bool {
        guard pending[id] != nil else { return false }
        timers[id]?.cancel()
        timers[id] = nil
        pending[id] = nil
        onChange?()
        return true
    }
}
