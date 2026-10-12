import Foundation
import UserNotifications

// Thin wrapper over UNUserNotificationCenter for reminder alerts. Requests
// permission once at launch; posting is a no-op until (and unless) granted.
enum Notifications {
    static func requestAuthorization() {
        UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .sound]) { _, _ in }
    }

    // `subtitle`, `category` and `userInfo` are only used by behavior nudges
    // (actionable notifications); every other caller keeps the defaults.
    static func deliver(id: String, title: String, body: String, subtitle: String = "",
                        category: String = "", userInfo: [String: String] = [:]) async -> Bool {
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else { return false }
        if !id.isEmpty {
            let delivered = await center.deliveredNotifications()
            if let old = delivered.first(where: { $0.request.identifier == id }) {
                // A replay of the same hub event is a no-op. A NEW event under
                // the same identifier (a nudge re-fired after a reset carries a
                // new event_id) replaces it, so its banner shows again.
                let newEvent = userInfo["event_id"] ?? ""
                let oldEvent = old.request.content.userInfo["event_id"] as? String ?? ""
                if newEvent.isEmpty || newEvent == oldEvent { return true }
                center.removeDeliveredNotifications(withIdentifiers: [id])
            }
        }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        if !subtitle.isEmpty { content.subtitle = subtitle }
        if !category.isEmpty { content.categoryIdentifier = category }
        if !userInfo.isEmpty {
            var info: [AnyHashable: Any] = [:]
            for (key, value) in userInfo { info[key] = value }
            content.userInfo = info
        }
        content.sound = .default
        let request = UNNotificationRequest(identifier: id.isEmpty ? UUID().uuidString : id,
                                             content: content, trigger: nil)
        do { try await center.add(request); return true }
        catch { return false }
    }

    static func post(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        if !body.isEmpty { content.body = body }
        content.sound = .default
        let req = UNNotificationRequest(identifier: UUID().uuidString,
                                        content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req)
    }

    /// Removes an already-delivered notification (e.g. a nudge the user
    /// answered from the overlay card instead).
    static func removeDelivered(id: String) {
        guard !id.isEmpty else { return }
        UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [id])
    }

    // --- behavior nudges: actionable notification categories ---------------
    // Categories are static, so the four combinations are registered up front
    // and BehaviorNudge.notificationCategory picks one: Undo only when the
    // backend already performed an auto action, "Do it" only when there is a
    // non-auto open_prompt action.
    static let nudgeCategory = "WISP_NUDGE"
    static let nudgeCategoryUndo = "WISP_NUDGE_UNDO"
    static let nudgeCategoryAct = "WISP_NUDGE_ACT"
    static let nudgeCategoryUndoAct = "WISP_NUDGE_UNDO_ACT"
    static let nudgeUsefulAction = "WISP_NUDGE_USEFUL"
    static let nudgeNotNowAction = "WISP_NUDGE_NOT_NOW"
    static let nudgeWrongAction = "WISP_NUDGE_WRONG"
    static let nudgeUndoAction = "WISP_NUDGE_UNDO_ACTION"
    static let nudgeDoItAction = "WISP_NUDGE_DO_IT"
    static let nudgeDefaultAction = UNNotificationDefaultActionIdentifier

    /// Set by OverlayModel.startAssistant: (nudge_id, action identifier, userInfo).
    @MainActor static var onNudgeResponse: (@MainActor (String, String, [String: String]) -> Void)?

    /// Registers the nudge categories and the delegate that routes their
    /// button presses. Call once at launch (OverlayModel.startAssistant).
    static func registerNudgeCategories() {
        let useful = UNNotificationAction(identifier: nudgeUsefulAction, title: "Useful", options: [])
        let notNow = UNNotificationAction(identifier: nudgeNotNowAction, title: "Not now", options: [])
        let wrong = UNNotificationAction(identifier: nudgeWrongAction, title: "Wrong", options: [])
        let undo = UNNotificationAction(identifier: nudgeUndoAction, title: "Undo", options: [])
        let doIt = UNNotificationAction(identifier: nudgeDoItAction, title: "Do it", options: [.foreground])
        func category(_ id: String, _ actions: [UNNotificationAction]) -> UNNotificationCategory {
            UNNotificationCategory(identifier: id, actions: actions, intentIdentifiers: [], options: [])
        }
        let center = UNUserNotificationCenter.current()
        center.setNotificationCategories([
            category(nudgeCategory, [useful, notNow, wrong]),
            category(nudgeCategoryUndo, [undo, useful, notNow, wrong]),
            category(nudgeCategoryAct, [doIt, useful, notNow, wrong]),
            category(nudgeCategoryUndoAct, [undo, doIt, useful, notNow, wrong]),
        ])
        center.delegate = NudgeNotificationDelegate.shared
    }
}

/// UNUserNotificationCenter keeps its delegate weakly; `shared` retains it.
/// Only nudge notifications are routed; every other notification keeps the
/// system default (no banner while Wisp is frontmost, no action handling).
final class NudgeNotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NudgeNotificationDelegate()

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        if notification.request.content.categoryIdentifier.hasPrefix(Notifications.nudgeCategory) {
            completionHandler([.banner, .list, .sound])
        } else {
            completionHandler([])
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let content = response.notification.request.content
        guard content.categoryIdentifier.hasPrefix(Notifications.nudgeCategory) else {
            completionHandler()
            return
        }
        var info: [String: String] = [:]
        for (key, value) in content.userInfo {
            if let key = key as? String, let value = value as? String { info[key] = value }
        }
        let action = response.actionIdentifier
        if let nudgeID = info["nudge_id"], !nudgeID.isEmpty {
            let info = info   // immutable copy for the Task (Swift 6 sendability)
            Task { @MainActor in Notifications.onNudgeResponse?(nudgeID, action, info) }
        }
        completionHandler()
    }
}
