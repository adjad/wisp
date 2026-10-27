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
    }

    @ViewBuilder private var content: some View {
        if memory.mode == "live", !memory.suggestions.isEmpty || memory.confirmation != nil {
            VStack(alignment: .leading, spacing: 6) {
                Text("Found in your mail").font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
                    .accessibilityAddTraits(.isHeader)
                ForEach(memory.suggestions.prefix(3)) { item in row(item) }
                if let confirmation = memory.confirmation {
                    Text(confirmation).font(.system(size: 11)).foregroundStyle(Theme.textMuted)
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
                    Text(item.what).font(.system(size: 13)).foregroundStyle(Theme.textPrimary).lineLimit(2)
                    Text("\(item.dueLabel()) · \(item.detail)").font(.system(size: 11))
                        .foregroundStyle(Theme.textMuted).lineLimit(1)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Suggested reminder: \(item.what), \(item.dueLabel()). \(item.detail)")
                Spacer(minLength: 8)
                if memory.answering == item.id {
                    ProgressView().controlSize(.small)
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
                Text(failure).font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}
