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
            if let prompts = controller.prompts {
                NotchPromptsPanel(controller: controller, status: prompts)
            } else if let conversation = controller.conversation {
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
            Text(controller.prompts == nil ? "Local model · bounded reads · writes and sends blocked"
                 : "Your prompts · the normal Ask path · sends and changes declined")
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

/// The service's prompts job: progress, the case it is on, and each finished
/// prompt with Wisp's whole answer, as Ask shows it.
private struct NotchPromptsPanel: View {
    @ObservedObject var controller: NotchCorpusController
    let status: CorpusPromptsStatus

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let run = status.run {
                HStack {
                    Text("\(run.completed)/\(run.total) prompts")
                    Spacer()
                    Text(run.statusLabel)
                }.font(.system(size: 10.5, design: .monospaced)).foregroundStyle(Theme.textSecondary)
                ProgressView(value: Double(run.completed), total: Double(max(run.total, 1))).tint(Theme.accent)
                HStack(spacing: 12) {
                    Text("\(run.ok) OK")
                    Text("\(run.declined) declined")
                    Text("\(run.errors) failed")
                    Spacer()
                    if let eta = CorpusPromptsStatus.eta(run.eta_s), run.active { Text("\(eta) left") }
                }.font(.system(size: 10.5, design: .monospaced)).foregroundStyle(Theme.textSecondary)
                if let current = run.current {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Now · \(current.case_id) · \(current.section)"
                             + (current.turns > 1 ? " · turn \(current.turn) of \(current.turns)" : ""))
                            .font(.system(size: 10.5, weight: .semibold))
                        Text(current.prompt).font(.system(size: 11)).lineLimit(2)
                    }.foregroundStyle(Theme.textPrimary)
                }
                HStack(spacing: 16) {
                    if run.active {
                        Button("Stop prompts") { controller.runPrompts(.stop) }
                    } else {
                        if run.status != "finished" {
                            Button("Resume") { controller.runPrompts(.resume) }
                        }
                        if run.errors > 0 {
                            Button("Retry \(run.errors) failed") { controller.runPrompts(.retryFailed) }
                        }
                    }
                    Button(controller.exporting ? "Exporting…" : "Export prompts") { controller.runPrompts(.export) }
                        .disabled(controller.exporting || run.completed == 0)
                        .help("Save this run to Downloads as Markdown and CSV, with full answers")
                }
                .font(.system(size: 11)).buttonStyle(.plain).foregroundStyle(Theme.textPrimary)
                .disabled(controller.promptsBusy)
            } else {
                Text(status.summary).font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
                    .textSelection(.enabled)
            }
            if !controller.promptResults.isEmpty {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        ForEach(controller.promptResults) { result in
                            PromptResultRow(result: result)
                        }
                    }
                }
                .frame(height: 300)
            }
        }
    }
}

private struct PromptResultRow: View {
    let result: CorpusPromptsResult

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text([result.case_id, result.category, result.tag ?? "", result.outcome,
                  result.latency_s.map { String(format: "%.1f s", $0) } ?? ""]
                    .filter { !$0.isEmpty }.joined(separator: " · "))
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(result.status == "error" ? Theme.warn : Theme.textSecondary)
            ForEach(Array(result.exchanges.enumerated()), id: \.offset) { _, turn in
                Text(turn.prompt).font(.system(size: 11, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                    .textSelection(.enabled)
                MarkdownView(text: turn.answer.isEmpty ? (result.error ?? "(no answer)") : turn.answer)
                    .textSelection(.enabled)
            }
            Divider().opacity(0.4)
        }
    }
}
