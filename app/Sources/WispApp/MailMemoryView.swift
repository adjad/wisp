import AppKit
import SwiftUI

/// One instance for the app, so a tab switch keeps what was loaded.
@MainActor
enum MailMemoryCards {
    static var model = MailMemoryModel(baseURL: WispClient.baseURL)
}

/// The Brief's "Found in your mail" rows: up to three long-term reminders from older
/// mail, each with Add to Reminders, Dismiss and Open email. Hidden unless the
/// person switched mail suggestions on and there is something to show.
struct MailMemorySection: View {
    @ObservedObject var memory: MailMemoryModel
    var onResize: () -> Void = {}

    var body: some View {
        // A zero-height stack when there's nothing to show, so it still refreshes.
        VStack(alignment: .leading, spacing: 0) { content }
            .task { await memory.refresh(); onResize() }
            .onReceive(NotificationCenter.default.publisher(for: .wispAssistantChanged)) { _ in
                Task { await memory.refresh(); onResize() }
            }
            .onChange(of: memory.confirmation) { _, text in
                if let text { Self.announce(text) }
            }
            .onChange(of: memory.failures) { old, new in
                for (id, text) in new where old[id] != text { Self.announce(text) }
            }
    }

    /// Says a result aloud to VoiceOver, since these lines appear without focus moving.
    private static func announce(_ text: String) {
        NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested,
                             userInfo: [.announcement: text, .priority: NSAccessibilityPriorityLevel.high.rawValue])
    }

    @ViewBuilder private var content: some View {
        if memory.mode == "live", !memory.suggestions.isEmpty || memory.confirmation != nil {
            VStack(alignment: .leading, spacing: 6) {
                Text("Found in your mail").font(.wisp(size: 11.5)).foregroundStyle(Theme.textMuted)
                    .accessibilityAddTraits(.isHeader)
                ForEach(memory.suggestions.prefix(3)) { item in row(item) }
                if let confirmation = memory.confirmation {
                    Text(confirmation).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
                }
            }
            .padding(.top, 2)
            .accessibilityElement(children: .contain)
        }
    }

    private func row(_ item: MailSuggestion) -> some View {
        let busy = memory.answering != nil
        return VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(item.what).font(.wisp(size: 13)).foregroundStyle(Theme.textPrimary).lineLimit(2)
                    Text("\(item.dueLabel()) · \(item.detail)").font(.wisp(size: 11))
                        .foregroundStyle(Theme.textMuted).lineLimit(1)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Suggested reminder: \(item.what), \(item.dueLabel()). \(item.detail)")
                Spacer(minLength: 8)
                if memory.answering == item.id {
                    ProgressView().controlSize(.small).accessibilityLabel("Adding")
                } else {
                    NotchChrome.small("Add") { Task { await memory.accept(item); onResize() } }
                        .disabled(busy)
                        .help("Add this to Reminders, due \(item.dueLabel())")
                        .accessibilityLabel("Add \(item.what) to Reminders")
                    NotchChrome.small("Dismiss") { Task { await memory.dismiss(item); onResize() } }
                        .disabled(busy)
                        .help("Don't suggest this again")
                        .accessibilityLabel("Dismiss \(item.what)")
                    if let url = item.mailURL {
                        NotchChrome.small("Open") { NSWorkspace.shared.open(url) }
                            .help("Open the email in Mail")
                            .accessibilityLabel("Open the email about \(item.what)")
                    }
                }
            }
            if let failure = memory.failures[item.id] {
                Text(failure).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// Settings → Privacy & Access: whether mail suggestions run, and whether the Brief shows them.
struct MailMemorySettingsRow: View {
    @ObservedObject var memory: MailMemoryModel = MailMemoryCards.model

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Mail suggestions").font(.system(size: 14, weight: .medium))
            Picker("Mail suggestions", selection: Binding(
                get: { memory.mode },
                set: { value in Task { await memory.setMode(value) } })) {
                Text("Show important").tag("live")
                Text("Record only").tag("shadow")
                Text("Off").tag("off")
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .disabled(!memory.known || memory.modeChanging)
            .accessibilityLabel("Mail suggestions")
            Text(caption).font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Text("Reads your Sent mail and Mail's Primary, Transactions and Updates from the last "
                 + "90 days, on this Mac, while you're idle. Never spam, trash, archive or promotions.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let status = memory.modeStatus {
                Text(status).font(.caption).foregroundStyle(.secondary)
            }
            if memory.mode != "off", let problem = memory.readError {
                Label(problem, systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .task { await memory.refresh() }
    }

    private var caption: String {
        guard memory.known else { return "Checking…" }
        switch memory.mode {
        case "live": return "Finds dates and deadlines in older mail and shows the important ones on the Brief, "
            + "where Add puts them in Reminders."
        case "shadow": return "Finds them but shows nothing, so you can check what it would suggest first."
        default: return "Off. Wisp doesn't read your mail for suggestions."
        }
    }
}
