import SwiftUI
import Foundation

// Behavior-engine nudges (service/behavior): Wisp learns routines, predicts
// tomorrow and pushes a `nudge` hub event (Nudge.to_event()) over the same
// assistant SSE stream as reminders. Each one surfaces as an actionable macOS
// notification (Notifications.swift) and as a small dismissible card at the
// top of the overlay (NudgeStack below). Actions marked `auto` were ALREADY
// performed by the backend when the event arrives, so the UI says so and
// offers Undo; everything else waits for the user.

/// One offered action, keeping its index in the backend's `actions` array —
/// that index is what POST /behavior/nudges/{id}/act expects.
struct NudgeAction: Equatable {
    let index: Int
    let type: String          // add_reminder | add_hold | open_prompt
    let auto: Bool
    let label: String
    let title: String         // params.title, when present
    let prompt: String        // params.prompt for open_prompt (local fallback)

    var isPrompt: Bool { type == "open_prompt" }
    /// Button text for an offered (non-auto) action.
    var buttonTitle: String {
        if !label.isEmpty { return label }
        switch type {
        case "open_prompt": return "Do it"
        case "add_reminder": return "Add reminder"
        case "add_hold": return "Hold the time"
        default: return "Do it"
        }
    }
}

/// What the user chose, from a card button or a notification action.
enum NudgeResponse: Equatable {
    case open                 // clicked the notification itself
    case feedback(String)     // useful | not_now | wrong
    case undo
    case act(Int)             // action_index of an offered (non-auto) action

    static let verdicts: Set<String> = ["useful", "not_now", "wrong"]

    /// Maps a UNNotificationResponse action identifier (see Notifications'
    /// nudge action constants) plus the notification's userInfo.
    static func from(notificationAction identifier: String, info: [String: String]) -> NudgeResponse? {
        switch identifier {
        case Notifications.nudgeDefaultAction: return .open
        case Notifications.nudgeUsefulAction: return .feedback("useful")
        case Notifications.nudgeNotNowAction: return .feedback("not_now")
        case Notifications.nudgeWrongAction: return .feedback("wrong")
        case Notifications.nudgeUndoAction: return .undo
        case Notifications.nudgeDoItAction:
            guard let raw = info["act_index"], let index = Int(raw), index >= 0 else { return nil }
            return .act(index)
        default: return nil
        }
    }
}

struct BehaviorNudge: Identifiable, Equatable {
    let id: String            // nudge_id (deterministic, so re-delivery dedupes)
    let kind: String
    let title: String
    let body: String
    let because: String
    let priority: Double
    let actions: [NudgeAction]
    let expiresAt: Double?
    /// The hub event that carried it. A NEW event_id for a known nudge id
    /// means it re-fired (e.g. after a demo reset), not a replay.
    var eventID = ""
    /// The backend's `can_undo` (whether something it did is still undoable);
    /// nil from an older backend, which falls back to the auto flags.
    var backendCanUndo: Bool? = nil
    // Card-local UI state.
    var status = ""
    var busy = false
    var undone = false
    var performed = false     // an offered reversible action was done via "act"

    var autoActions: [NudgeAction] { actions.filter(\.auto) }
    var hasAuto: Bool { !autoActions.isEmpty }
    /// Actions waiting for the user: open_prompt prefills the composer (never
    /// sends); an offered add_reminder/add_hold is created by the backend.
    var offeredActions: [NudgeAction] { actions.filter { !$0.auto } }
    /// The first non-auto open_prompt action ("Draft reschedule note"); the
    /// notification's "Do it" button maps to it.
    var promptAction: NudgeAction? { actions.first { !$0.auto && $0.isPrompt } }
    var canUndo: Bool { ((backendCanUndo ?? hasAuto) || performed) && !undone }

    func isExpired(now: Double = Date().timeIntervalSince1970) -> Bool {
        guard let expiresAt else { return false }
        return expiresAt < now
    }

    /// Nudge ids travel in URL paths; refuse anything outside a safe ASCII set.
    static func isSafeID(_ id: String) -> Bool { WispClient.behaviorSafeID(id) }

    private static func isJSONTrue(_ value: Any?) -> Bool {
        // NSNumber(1) also bridges to Bool; only JSON `true` counts as auto.
        guard let number = value as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID() else { return false }
        return number.boolValue
    }

    /// Parses a hub `nudge` payload defensively. Required: nudge_id, title,
    /// and string body/because. Malformed action rows are skipped (their
    /// indices are kept so `act` still addresses the backend's array).
    static func parse(_ payload: [String: Any]) -> BehaviorNudge? {
        guard let id = payload["nudge_id"] as? String, isSafeID(id),
              let title = payload["title"] as? String,
              !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let body = payload["body"] as? String,
              let because = payload["because"] as? String else { return nil }
        // "Already done" comes from what the backend actually performed
        // (auto_performed[].action_index) when it says so; else the auto flags.
        var performedIndexes: Set<Int>?
        if let rows = payload["auto_performed"] as? [Any] {
            var found = Set<Int>()
            for case let row as [String: Any] in rows {
                if let number = row["action_index"] as? NSNumber,
                   CFGetTypeID(number) != CFBooleanGetTypeID() { found.insert(number.intValue) }
            }
            if rows.isEmpty || !found.isEmpty { performedIndexes = found }
        }
        var actions: [NudgeAction] = []
        for (index, raw) in (payload["actions"] as? [Any] ?? []).enumerated() {
            guard let row = raw as? [String: Any], let type = row["type"] as? String,
                  !type.isEmpty else { continue }
            let params = row["params"] as? [String: Any] ?? [:]
            let done = performedIndexes.map { $0.contains(index) } ?? isJSONTrue(row["auto"])
            actions.append(NudgeAction(index: index, type: type, auto: done,
                                       label: row["label"] as? String ?? "",
                                       title: params["title"] as? String ?? "",
                                       prompt: params["prompt"] as? String ?? ""))
        }
        var expires: Double?
        if let number = payload["expires_at"] as? NSNumber,
           CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite {
            expires = number.doubleValue
        }
        var priority = 0.0
        if let number = payload["priority"] as? NSNumber,
           CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite {
            priority = number.doubleValue
        }
        var nudge = BehaviorNudge(id: id, kind: payload["kind"] as? String ?? "", title: title,
                                  body: body, because: because, priority: priority,
                                  actions: actions, expiresAt: expires)
        nudge.eventID = payload["event_id"] as? String ?? ""
        if let number = payload["can_undo"] as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() {
            nudge.backendCanUndo = number.boolValue
        }
        return nudge
    }

    /// "Added reminder “Pack gym bag”" — what Wisp already did on its own.
    var autoSummary: String {
        autoActions.map { a -> String in
            if !a.label.isEmpty { return a.label }
            switch a.type {
            case "add_reminder": return a.title.isEmpty ? "Added a reminder" : "Added reminder “\(a.title)”"
            case "add_hold": return a.title.isEmpty ? "Placed a tentative hold" : "Placed a tentative hold “\(a.title)”"
            default: return "Done"
            }
        }.joined(separator: "; ")
    }

    // --- notification shape (Notifications.deliver) ---
    var notificationID: String { "wisp-nudge:" + id }
    var notificationSubtitle: String {
        hasAuto ? "Already done: \(autoSummary) · Undo available" : ""
    }
    var notificationBody: String {
        [body, because].filter { !$0.isEmpty }.joined(separator: "\n")
    }
    var notificationCategory: String {
        switch (hasAuto, promptAction != nil) {
        case (true, true): return Notifications.nudgeCategoryUndoAct
        case (true, false): return Notifications.nudgeCategoryUndo
        case (false, true): return Notifications.nudgeCategoryAct
        default: return Notifications.nudgeCategory
        }
    }
    var notificationInfo: [String: String] {
        var info = ["nudge_id": id, "kind": kind]
        // Lets Notifications.deliver tell a re-fired nudge from a replay.
        if !eventID.isEmpty { info["event_id"] = eventID }
        if let act = promptAction {
            info["act_index"] = String(act.index)
            info["prompt"] = act.prompt
        }
        return info
    }
}

// MARK: - Overlay card

/// Nudge cards for the top of the overlay's chat area. Integration: place
/// `NudgeStack(model: model)` right under `MemoryReviewNotice()` in
/// OverlayView.expandedPanel. Renders nothing while there are no nudges.
struct NudgeStack: View {
    @ObservedObject var model: OverlayModel

    var body: some View {
        if !model.nudges.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(model.nudges) { nudge in
                    NudgeCard(nudge: nudge,
                              respond: { model.respondToNudge(nudge.id, $0) },
                              dismiss: { model.dismissNudge(nudge.id) })
                }
            }
            .padding(.top, 8)
        }
    }
}

struct NudgeCard: View {
    let nudge: BehaviorNudge
    let respond: (NudgeResponse) -> Void
    let dismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: "sparkles").font(.system(size: 12))
                    .foregroundStyle(Theme.textPrimary)
                Text(nudge.title).font(.system(size: 13, weight: .medium))
                    .foregroundStyle(Theme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                Button(action: dismiss) {
                    Image(systemName: "xmark").font(.system(size: 9, weight: .bold))
                }
                .buttonStyle(.plain).foregroundStyle(Theme.textMuted)
                .help("Dismiss")
            }
            if !nudge.body.isEmpty {
                Text(nudge.body).font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !nudge.because.isEmpty {
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Image(systemName: "chart.bar.xaxis").font(.system(size: 10))
                    Text(nudge.because).font(.system(size: 11))
                        .fixedSize(horizontal: false, vertical: true)
                }
                .foregroundStyle(Theme.textMuted)
            }
            if nudge.hasAuto {
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Image(systemName: nudge.undone ? "arrow.uturn.backward.circle" : "checkmark.circle")
                        .font(.system(size: 10))
                    Text(nudge.undone ? "Undone: \(nudge.autoSummary)" : "Already done: \(nudge.autoSummary)")
                        .font(.system(size: 11))
                        .fixedSize(horizontal: false, vertical: true)
                }
                .foregroundStyle(Theme.textSecondary)
            }
            HStack(spacing: 6) {
                if nudge.canUndo {
                    chip("Undo", primary: true) { respond(.undo) }
                }
                ForEach(Array(nudge.offeredActions.enumerated()), id: \.offset) { position, act in
                    if act.isPrompt || !nudge.performed {
                        chip(act.buttonTitle, primary: position == 0 && !nudge.canUndo) {
                            respond(.act(act.index))
                        }
                    }
                }
                Spacer(minLength: 0)
                chip("Useful") { respond(.feedback("useful")) }
                chip("Not now") { respond(.feedback("not_now")) }
                chip("Wrong") { respond(.feedback("wrong")) }
            }
            .disabled(nudge.busy)
            if !nudge.status.isEmpty {
                Text(nudge.status).font(.system(size: 11)).foregroundStyle(Theme.textMuted)
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
    }

    private func chip(_ title: String, primary: Bool = false, _ action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .buttonStyle(.plain)
            .font(.system(size: 11, weight: primary ? .medium : .regular))
            .foregroundStyle(primary ? Color.black : Theme.textSecondary)
            .lineLimit(1)
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(Capsule().fill(primary ? Color.white : Theme.chipFill))
            .overlay(Capsule().stroke(primary ? Color.clear : Theme.chipStroke, lineWidth: 1))
    }
}
