import Foundation

/// The one-tap Cancel on the Day tab's check-in card (CheckInView.swift): its
/// wording, the request it sends and how the service's answer is read. Kept
/// free of SwiftUI so tests/CheckInCancelChecks.swift can run it offline.
///
/// Tapping Cancel does not cancel yet: the row turns into an Undo bar for
/// `undoWindow` seconds, and only then is POST /assistant/checkin/cancel_event
/// sent (service/assistant/checkin_api.py), which removes exactly that
/// occurrence through the app's Calendar handler and reads back that it is gone.
enum CheckInCancel {
    /// How long Undo stays before the cancel is sent (user decision, 2026-10-18).
    static let undoWindow: TimeInterval = 5

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
        /// The service's detail.outcome: not_found, not_allowed, not_current,
        /// unknown, unavailable, failed; or no_answer / invalid from this side.
        let outcome: String
        let message: String

        /// Try again is offered when a retry could succeed. An unknown outcome
        /// means "check Calendar first"; a missing event or one that isn't the
        /// user's to cancel will not change on a retry.
        var canRetry: Bool { !["unknown", "not_found", "not_allowed"].contains(outcome) }
    }

    /// Reads the answer. nil on a verified 200 {ok: true}; otherwise what to
    /// show. Every non-2xx body carries detail {outcome, message}; a 422 from
    /// a malformed body carries pydantic's list instead, so it gets a plain line.
    static func failure(status: Int?, body: [String: Any]?) -> Failure? {
        guard let status else {
            return Failure(outcome: "no_answer", message: "Wisp's service didn't answer. Check Calendar before trying again.")
        }
        if (200..<300).contains(status) {
            if body?["ok"] as? Bool == true { return nil }
            return Failure(outcome: "unknown", message: "Wisp couldn't confirm whether it was cancelled. Check Calendar before trying again.")
        }
        let detail = body?["detail"] as? [String: Any]
        let outcome = detail?["outcome"] as? String ?? (status == 422 ? "invalid" : "failed")
        let message = detail?["message"] as? String ?? "Couldn't cancel that. Nothing was cancelled."
        return Failure(outcome: outcome, message: message)
    }

    /// The invitee count a verified answer reports, falling back to the row's.
    static func notifiedCount(body: [String: Any]?, rowCount: Int?) -> Int {
        if let count = (body?["invitee_count"] as? NSNumber)?.intValue, count >= 0 { return count }
        return max(0, rowCount ?? 0)
    }
}
