import Foundation
import UserNotifications

// Thin wrapper over UNUserNotificationCenter for reminder alerts. Notifications
// aren't asked in setup: the first alert Wisp has for you asks macOS (deliver),
// once; the answer then holds. Posting is a no-op while they are off.
enum Notifications {

    // `subtitle`, `category` and `userInfo` are only used by behavior nudges
    // (actionable notifications); every other caller keeps the defaults.
    static func deliver(id: String, title: String, body: String, subtitle: String = "",
                        category: String = "", userInfo: [String: String] = [:]) async -> Bool {
        guard await allowedAskingFirstTime() else { return false }
        let center = UNUserNotificationCenter.current()
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

    static func post(id: String = "", title: String, body: String) {
        Task { _ = await postIfAllowed(id: id, title: title, body: body) }
    }

    /// Whether notifications reach the person: allowed (or allowed quietly).
    /// Reads the setting only; asking is the setup window's Notifications row.
    static func allowed() async -> Bool {
        let status = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
        return status == .authorized || status == .provisional
    }

    /// Allowed, or, while macOS has never asked, its question now: the moment Wisp
    /// first has an alert for you is when notifications are needed. macOS shows that
    /// question once; after any answer this only reads the setting.
    static func allowedAskingFirstTime() async -> Bool {
        let center = UNUserNotificationCenter.current()
        guard await center.notificationSettings().authorizationStatus == .notDetermined else { return await allowed() }
        return await requestFirst()
    }

    // Provisional first. What macOS documents for UNAuthorizationOptions.provisional:
    // requesting it shows no permission prompt and grants "provisional" authorization
    // at once; alerts then arrive quietly (in Notification Center, with no banner and
    // no sound), each with Keep and Turn Off buttons, and the person can turn
    // them up from there or in System Settings. A later request without .provisional
    // while the status is provisional is documented to ask normally. We can't
    // confirm either on a live Mac from this tree, so askForAlerts() reads the
    // status back and reports what it found rather than assuming it worked.

    /// The options of the first request: quiet delivery, no question to answer.
    static let firstRequestOptions: UNAuthorizationOptions = [.alert, .sound, .provisional]
    /// The options of the later, in-context request for full alerts (banners and sound).
    static let fullAlertOptions: UNAuthorizationOptions = [.alert, .sound]

    /// The first request, always provisional. Never prompts.
    @discardableResult
    static func requestFirst() async -> Bool {
        (try? await UNUserNotificationCenter.current().requestAuthorization(options: firstRequestOptions)) ?? false
    }

    /// Whether alerts are delivered quietly only (provisional). Reads the setting.
    static func isProvisional() async -> Bool {
        await UNUserNotificationCenter.current().notificationSettings().authorizationStatus == .provisional
    }

    /// The chip beside reminder alerts ("Get alerts for reminders"): asks macOS for
    /// full alerts, once the person has seen quiet delivery and wants more. The
    /// person's own click only. True when alerts are now fully allowed. If macOS
    /// shows no question (it was already answered), System Settings' Notifications
    /// page is where to change it.
    @discardableResult
    static func askForAlerts() async -> Bool {
        let center = UNUserNotificationCenter.current()
        _ = try? await center.requestAuthorization(options: fullAlertOptions)
        return await center.notificationSettings().authorizationStatus == .authorized
    }

    static let getAlertsChip = "Get alerts for reminders"
    static let quietNote = "Reminder alerts arrive quietly in Notification Center, with no banner or sound."

    /// Posts and says whether it was shown, so a caller with a promise to keep
    /// ("Wisp is ready") can say it another way when notifications are off.
    static func postIfAllowed(id: String = "", title: String, body: String) async -> Bool {
        guard await allowed() else { return false }
        let content = UNMutableNotificationContent()
        content.title = title
        if !body.isEmpty { content.body = body }
        content.sound = .default
        let req = UNNotificationRequest(identifier: id.isEmpty ? UUID().uuidString : id,
                                        content: content, trigger: nil)
        do { try await UNUserNotificationCenter.current().add(req); return true }
        catch { return false }
    }

    /// What a Wisp notification shows while Wisp is the app in front. Wisp has
    /// windows of its own (chat, Today, Settings, setup), and with nothing asked
    /// for here macOS shows a front app's notification nowhere: no banner, no
    /// sound, no Notification Center entry. A reminder that came due while the
    /// person was in a Wisp window was lost that way, and still counted as
    /// delivered. So every Wisp notification is shown in full, whatever its category.
    static func presentationOptions(categoryIdentifier: String) -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }

    /// "Wisp is ready" after first-run setup; clicking it opens Ask, not Day.
    static let readyId = "wisp.setup.ready"
    @MainActor static var onOpenAsk: (@MainActor () -> Void)?

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
    /// Clicking any other Wisp notification (a reminder, something Wisp added
    /// for you, a notice about a scheduled message) opens Today in the notch.
    @MainActor static var onOpenToday: (@MainActor () -> Void)?

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
/// Nudge notifications route their buttons; clicking any other notification
/// opens Today in the notch ("Wisp is ready" opens Ask). Every Wisp notification
/// shows its banner, plays its sound and stays in Notification Center while Wisp
/// is frontmost too (Notifications.presentationOptions).
final class NudgeNotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NudgeNotificationDelegate()

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler(Notifications.presentationOptions(
            categoryIdentifier: notification.request.content.categoryIdentifier))
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let content = response.notification.request.content
        guard content.categoryIdentifier.hasPrefix(Notifications.nudgeCategory) else {
            if response.actionIdentifier == UNNotificationDefaultActionIdentifier {
                if response.notification.request.identifier == Notifications.readyId {
                    Task { @MainActor in Notifications.onOpenAsk?() }
                } else {
                    Task { @MainActor in Notifications.onOpenToday?() }
                }
            }
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
