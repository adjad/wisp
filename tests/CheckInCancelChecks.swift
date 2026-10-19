import Foundation

/// Offline checks for CheckInCancel (app/Sources/WispApp/CheckInCancel.swift): the
/// Undo-bar wording, the exact request the service accepts, and how each outcome
/// of POST /assistant/checkin/cancel_event is shown.
@main struct CheckInCancelCheck {
    static func main() {
        precondition(CheckInCancel.undoWindow == 5, "the user confirmed a 5-second Undo")
        precondition(CheckInCancel.undoLabel(inviteeCount: 0) == "Cancelled")
        precondition(CheckInCancel.undoLabel(inviteeCount: 1) == "Cancelled · its 1 invitee will be notified")
        precondition(CheckInCancel.undoLabel(inviteeCount: 3) == "Cancelled · its 3 invitees will be notified")
        precondition(CheckInCancel.doneLabel(title: "Dentist", inviteeCount: 0) == "“Dentist” cancelled")
        precondition(CheckInCancel.doneLabel(title: "Sync", inviteeCount: 2) == "“Sync” cancelled · 2 invitees notified")
        precondition(CheckInCancel.blockedReason("Only the organizer can cancel it.") == "Only the organizer can cancel it.")
        precondition(CheckInCancel.blockedReason("").contains("Calendar"), "an older service gives no reason")

        // The request: exactly the three fields the service accepts, from the row.
        let body = CheckInCancel.requestBody(sourceId: "abc", startTs: 1_900_000_000, inviteeCount: 2)
        precondition(body?.count == 3)
        precondition(body?["source_id"] as? String == "abc")
        precondition(body?["expected_start_ts"] as? Double == 1_900_000_000)
        precondition(body?["expected_invitees"] as? Int == 2)
        precondition(CheckInCancel.requestBody(sourceId: "", startTs: 1, inviteeCount: 0) == nil, "no calendar id")
        precondition(CheckInCancel.requestBody(sourceId: "abc", startTs: nil, inviteeCount: 0) == nil, "no start")
        precondition(CheckInCancel.requestBody(sourceId: "abc", startTs: .nan, inviteeCount: 0) == nil)
        precondition(CheckInCancel.requestBody(sourceId: "abc", startTs: 0, inviteeCount: 0) == nil)
        precondition(CheckInCancel.requestBody(sourceId: "abc", startTs: 1, inviteeCount: nil) == nil, "invitees not synced")
        precondition(CheckInCancel.requestBody(sourceId: "abc", startTs: 1, inviteeCount: -1) == nil)

        // The answer: only {ok: true} on 2xx is a verified cancel.
        precondition(CheckInCancel.failure(status: 200, body: ["ok": true, "invitee_count": 2]) == nil)
        precondition(CheckInCancel.failure(status: 200, body: ["ok": false])?.outcome == "unknown")
        precondition(CheckInCancel.failure(status: 200, body: nil)?.outcome == "unknown")
        let noAnswer = CheckInCancel.failure(status: nil, body: nil)
        precondition(noAnswer?.outcome == "no_answer" && noAnswer?.canRetry == true)
        func failure(_ status: Int, _ outcome: String, _ message: String = "msg.") -> CheckInCancel.Failure? {
            CheckInCancel.failure(status: status, body: ["detail": ["outcome": outcome, "message": message]])
        }
        precondition(failure(404, "not_found", "Gone.")?.message == "Gone.", "the service's message is shown as is")
        precondition(failure(404, "not_found")?.canRetry == false)
        precondition(failure(403, "not_allowed")?.canRetry == false)
        precondition(failure(409, "not_allowed")?.canRetry == false, "invitees not synced yet")
        precondition(failure(409, "not_current")?.canRetry == true, "a refreshed card may cancel it")
        precondition(failure(409, "unknown")?.canRetry == false, "check Calendar first; never a retry button")
        precondition(failure(503, "unavailable")?.canRetry == true)
        precondition(failure(502, "failed")?.canRetry == true)
        let invalid = CheckInCancel.failure(status: 422, body: ["detail": [["loc": ["body"], "msg": "x"]]])
        precondition(invalid?.outcome == "invalid" && invalid?.message.contains("Nothing was cancelled") == true,
                     "pydantic's list detail gets a plain line")
        precondition(CheckInCancel.failure(status: 500, body: nil)?.outcome == "failed")

        precondition(CheckInCancel.notifiedCount(body: ["invitee_count": 3], rowCount: 1) == 3, "the service's count wins")
        precondition(CheckInCancel.notifiedCount(body: [:], rowCount: 1) == 1)
        precondition(CheckInCancel.notifiedCount(body: nil, rowCount: nil) == 0)
        print("CheckInCancel checks passed")
    }
}
