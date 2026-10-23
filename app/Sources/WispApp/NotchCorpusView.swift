import AppKit
import SwiftUI

/// The same guarded runner and review cards as the window, sized for the notch.
struct NotchCorpusView: View {
    @ObservedObject var controller: NotchCorpusController
    var openWindow: () -> Void
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Assistant corpus").font(.system(size: 12, weight: .semibold))
                Spacer()
                if controller.busy {
                    Button("Stop", action: controller.stop)
                        .accessibilityLabel("Stop corpus run")
                }
                Button("Open in window", action: openWindow)
                    .disabled(controller.conversation == nil)
            }
            .buttonStyle(.plain)
            .foregroundStyle(Theme.textPrimary)
            if controller.loading {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Loading corpus cases…").font(.caption)
                }.foregroundStyle(Theme.textSecondary)
            }
            if !controller.notice.isEmpty {
                Text(controller.notice).font(.caption).foregroundStyle(Theme.warn)
            }
            if let conversation = controller.conversation {
                NotchCorpusThread(store: controller.store, conversation: conversation)
            }
            if let files = controller.exported {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("Saved \(files.markdown.lastPathComponent) and .csv to \(files.markdown.deletingLastPathComponent().lastPathComponent)"
                         + (files.saved ? "" : " (from the run on screen)"))
                        .font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
                        .lineLimit(2).textSelection(.enabled)
                    Spacer(minLength: 0)
                    Button("Reveal in Finder") {
                        NSWorkspace.shared.activateFileViewerSelecting([files.markdown, files.csv])
                    }
                    .font(.system(size: 11)).buttonStyle(.plain).foregroundStyle(Theme.textPrimary)
                }
                .help(files.markdown.path)
            }
            HStack(spacing: 16) {
                Button("Browse cases") { controller.submit("/corpus list") }
                Button("Run smoke") { controller.submit("/corpus smoke") }
                Button("Run all 100") { controller.submit("/corpus all") }
            }
            .font(.system(size: 11)).buttonStyle(.plain)
            .foregroundStyle(Theme.textSecondary).disabled(controller.busy)
            HStack(spacing: 16) {
                Button(controller.exporting ? "Exporting…" : "Export results", action: controller.export)
                    .disabled(controller.exporting)
                    .help("Save the latest run to Downloads as Markdown and CSV (/corpus export)")
                Button(copied ? "Copied" : "Copy results") {
                    guard let markdown = controller.resultsMarkdown else { return }
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(markdown, forType: .string)
                    copied = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) { copied = false }
                }
                .disabled(controller.resultsMarkdown == nil)
                .help("Copy the run on screen as Markdown")
            }
            .font(.system(size: 11)).buttonStyle(.plain).foregroundStyle(Theme.textSecondary)
            Text("Local model · bounded reads · writes and sends blocked")
                .font(.system(size: 10)).foregroundStyle(Theme.textMuted)
        }
    }
}

private struct NotchCorpusThread: View {
    @ObservedObject var store: ChatStore
    @ObservedObject var conversation: ChatConversation

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if conversation.corpusTotal > 0 {
                HStack {
                    Text("\(conversation.corpusFinishedCount)/\(conversation.corpusTotal) finished")
                    Spacer()
                    Text("\(conversation.corpusVerifiedCount) with verified inference")
                }.font(.system(size: 10.5, design: .monospaced)).foregroundStyle(Theme.textSecondary)
                ProgressView(value: Double(conversation.corpusFinishedCount), total: Double(conversation.corpusTotal))
                    .tint(Theme.accent)
            }
            ChatThread(store: store, conversation: conversation)
                .frame(height: 360)
                .id(conversation.id)
        }
    }
}
