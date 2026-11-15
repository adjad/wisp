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
    /// Add, Dismiss and Open under the reminder rather than beside it (the
    /// proactive Brief's narrower column), so they never squeeze its title.
    var buttonsBelow = false

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
        return VStack(alignment: .leading, spacing: buttonsBelow ? 5 : 2) {
            if buttonsBelow {
                summary(item)
                HStack(spacing: 8) { buttons(item, busy: busy) }
            } else {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    summary(item)
                    Spacer(minLength: 8)
                    buttons(item, busy: busy)
                }
            }
            if let failure = memory.failures[item.id] {
                Text(failure).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func summary(_ item: MailSuggestion) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(item.what).font(.wisp(size: 13)).foregroundStyle(Theme.textPrimary).lineLimit(2)
            Text("\(item.dueLabel()) · \(item.detail)").font(.wisp(size: 11))
                .foregroundStyle(Theme.textMuted).lineLimit(1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Suggested reminder: \(item.what), \(item.dueLabel()). \(item.detail)")
    }

    @ViewBuilder private func buttons(_ item: MailSuggestion, busy: Bool) -> some View {
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
}

/// Settings → Privacy & Access: whether mail suggestions run, whether the Brief shows
/// them, whether Wisp learns facts from mail, and the button that forgets those facts
/// and the text kept from attachments.
struct MailMemorySettingsRow: View {
    @ObservedObject var memory: MailMemoryModel = MailMemoryCards.model
    /// Every answer comes from a cloud model (remote-only, or the Intel build): the
    /// mail is read by that model, not on this Mac.
    var cloud = false
    @State private var confirmForgetMail = false

    /// What Mail suggestions reads, and where. On the cloud the whole history may be
    /// read, so no 90-day promise. Pure.
    nonisolated static func readsLine(cloud: Bool) -> String {
        cloud
            ? "Reads your Sent mail and Mail's Primary, Transactions and Updates while you're idle, and "
                + "sends them to Wisp's cloud model to find what matters. Never spam, trash, archive or promotions."
            : "Reads your Sent mail and Mail's Primary, Transactions and Updates from the last "
                + "90 days, on this Mac, while you're idle. Never spam, trash, archive or promotions."
    }

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
            Text(Self.readsLine(cloud: cloud))
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
            factsControls.padding(.top, 6)
        }
        .task { await memory.refresh() }
    }

    /// The facts switch and "Forget what Wisp learned from mail…". The switch stays
    /// disabled until the service has said whether it is on; forgetting asks first.
    private var factsControls: some View {
        VStack(alignment: .leading, spacing: 6) {
            Toggle(isOn: Binding(
                get: { memory.factsEnabled == true },
                set: { value in Task { await memory.setFacts(value) } })) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(MailFactsText.title).font(.system(size: 14, weight: .medium))
                    Text(MailFactsText.caption(known: memory.known, enabled: memory.factsEnabled, mode: memory.mode))
                        .font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .toggleStyle(.switch)
            .disabled(!memory.known || memory.factsEnabled == nil || memory.factsChanging)
            HStack(spacing: 10) {
                Button(MailFactsText.forgetButton, role: .destructive) { confirmForgetMail = true }
                    .disabled(!memory.known || memory.factsChanging)
                    .help(MailFactsText.forgetHelp)
                if let status = memory.factsStatus {
                    Text(status).font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .confirmationDialog(MailFactsText.forgetTitle, isPresented: $confirmForgetMail, titleVisibility: .visible) {
            Button("Forget", role: .destructive) {
                // The chats that read that mail may still be held for the debug log: drop them too.
                NotificationCenter.default.post(name: .wispForgetChatDebugArchive, object: nil)
                Task { await memory.forgetFromMail() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(MailFactsText.forgetMessage)
        }
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
