// Compile alongside app/Sources/WispApp/WispClient.swift. No network calls.
import Foundation

@main
enum SourceSyncLabelRegression {
    static func main() {
        func status(_ pending: [String] = [], unavailable: [String] = [],
                    disabled: [String] = [], local: Bool = false) -> WispClient.AssistantSyncStatus {
            let sources: [WispClient.SourceSyncStatus] = local ? [
                .init(id: "email", label: "Email", state: "ready", progress: 1,
                      detail: "Local read complete", warning: "Newer email may be missing")
            ] : []
            return .init(sources: sources, progress: Double(4 - pending.count) / 4,
                         completed: 4 - pending.count, total: 4, pendingLabels: pending,
                         unavailableLabels: unavailable, disabledLabels: disabled)
        }
        precondition(status(["Email"]).label == "Email syncing · 3/4")
        precondition(status(["Email", "Messages"]).label == "Email, Messages syncing · 2/4")
        precondition(status(["Calendar", "Reminders", "Email", "Messages"]).label == "Sources syncing · 0/4")
        precondition(status().label == "Synced · 4/4")
        precondition(status(local: true).label == "Email cached · 4/4 checked")
        precondition(status(unavailable: ["Email"]).label == "Email unavailable · 4/4 checked")
        precondition(status(disabled: ["Browser History"]).label == "Browser History off · 4/4 checked")
        precondition(status(unavailable: ["Email", "Calendar"]).label == "Some sources unavailable · 4/4 checked")
        print("SourceSyncLabel: 8 regression checks passed")
    }
}
