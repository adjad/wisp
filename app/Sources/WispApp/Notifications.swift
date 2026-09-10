import Foundation
import UserNotifications

// Thin wrapper over UNUserNotificationCenter for reminder alerts. Requests
// permission once at launch; posting is a no-op until (and unless) granted.
enum Notifications {
    static func requestAuthorization() {
        UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .sound]) { _, _ in }
    }

    static func deliver(id: String, title: String, body: String) async -> Bool {
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else { return false }
        if !id.isEmpty {
            let delivered = await center.deliveredNotifications()
            if delivered.contains(where: { $0.request.identifier == id }) { return true }
        }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
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
}
