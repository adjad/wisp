import AppKit
import SwiftUI

struct ContentView: View {
    @ObservedObject var controller: EngineProcessController
    @State private var copied = ""

    var body: some View {
        HStack(spacing: 0) {
            sidebar
                .frame(width: 330)
            Divider()
            chat
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Ling Local").font(.system(size: 25, weight: .semibold))
                Text("Local inference on this Mac").foregroundStyle(.secondary)
            }
            GroupBox("Checkpoint") {
                VStack(alignment: .leading, spacing: 10) {
                    Text(controller.checkpointPath)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .lineLimit(3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    HStack {
                        Button("Choose…", action: controller.chooseCheckpoint)
                            .accessibilityLabel("Choose checkpoint folder")
                            .disabled(controller.isOwnedProcessRunning || controller.status == .starting || controller.status == .inspecting || controller.status == .stopping)
                        Button("Inspect", action: controller.inspectCheckpoint)
                            .disabled(controller.status == .inspecting || controller.status == .starting || controller.status == .stopping || controller.isOwnedProcessRunning)
                    }
                    if let metadata = controller.metadata {
                        LabeledContent("Model", value: metadata.model_type)
                        LabeledContent("Layers", value: "\(metadata.layers)")
                        LabeledContent("Quantization", value: "\(metadata.quantization.bits)-bit · group \(metadata.quantization.group_size)")
                    }
                }
                .padding(.top, 4)
            }
            GroupBox("Engine") {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(spacing: 8) {
                        Circle().fill(statusColor).frame(width: 9, height: 9)
                        Text(controller.status.title).fontWeight(.medium)
                    }
                    if let health = controller.health {
                        Text("Serving \(health.model)").font(.caption).foregroundStyle(.secondary)
                    }
                    HStack {
                        Button("Start", action: controller.start)
                            .buttonStyle(.borderedProminent)
                            .disabled(controller.status == .starting || controller.status == .inspecting || controller.status == .stopping || controller.isOwnedProcessRunning)
                        Button("Stop", action: controller.stop)
                            .disabled(!controller.isOwnedProcessRunning)
                    }
                }
                .padding(.top, 4)
            }
            GroupBox("Connect a client") {
                VStack(alignment: .leading, spacing: 10) {
                    endpointRow(title: "API root", value: "http://127.0.0.1:8767/v1")
                    endpointRow(title: "Wisp base URL", value: "http://127.0.0.1:8767")
                    Text("Wisp adds /v1 automatically. Keep the base URL without that suffix.")
                        .font(.caption).foregroundStyle(.secondary)
                    if !copied.isEmpty { Text("Copied \(copied)").font(.caption).foregroundStyle(.green) }
                }
                .padding(.top, 4)
            }
            Toggle("Enable thinking", isOn: $controller.thinking)
                .accessibilityLabel("Enable model thinking")
            Spacer(minLength: 0)
            Text("Loopback only · no API key required")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(20)
    }

    private var chat: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Test chat").font(.title2.weight(.semibold))
                    Text("Requests stay on this Mac.").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if controller.status == .ready {
                    Label("Ready", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                }
            }
            .padding(20)
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    if controller.chatLines.isEmpty {
                        ContentUnavailableView("Start a local chat", systemImage: "bubble.left.and.bubble.right", description: Text("Start the engine, then send a message to test the checkpoint."))
                            .frame(maxWidth: .infinity, minHeight: 330)
                    } else {
                        LazyVStack(alignment: .leading, spacing: 16) {
                            ForEach(controller.chatLines) { line in
                                messageBubble(line)
                                    .id(line.id)
                            }
                        }
                        .padding(22)
                    }
                }
                .onChange(of: controller.chatLines.count) { _, _ in
                    if let last = controller.chatLines.last { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
            if let message = controller.message {
                Label(message, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout).foregroundStyle(.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 20).padding(.vertical, 10)
                    .textSelection(.enabled)
            }
            Divider()
            HStack(alignment: .bottom, spacing: 12) {
                TextField("Message Ling…", text: $controller.draft, axis: .vertical)
                    .lineLimit(2...6)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Message")
                    .onSubmit(controller.send)
                Button {
                    controller.send()
                } label: {
                    if controller.isSending { ProgressView().controlSize(.small) }
                    else { Label("Send", systemImage: "arrow.up") }
                }
                .buttonStyle(.borderedProminent)
                .disabled(controller.status != .ready || controller.isSending || controller.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityLabel("Send message")
            }
            .padding(18)
        }
    }

    private func endpointRow(title: String, value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.caption.weight(.medium))
                Text(value).font(.system(.caption2, design: .monospaced)).textSelection(.enabled)
            }
            Spacer(minLength: 4)
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(value, forType: .string)
                copied = title
            } label: {
                Image(systemName: "doc.on.doc")
            }
            .buttonStyle(.borderless)
            .accessibilityLabel("Copy \(title)")
        }
    }

    private func messageBubble(_ line: ChatLine) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(line.role.uppercased()).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Text(line.content).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
            if let metrics = line.metrics {
                HStack(spacing: 12) {
                    metric("Prefill", seconds: metrics.prefill_seconds)
                    metric("Decode", seconds: metrics.decode_seconds)
                    metric("Total", seconds: metrics.total_seconds)
                    if let bytes = metrics.peak_memory_bytes { Text("Peak \(ByteCountFormatter.string(fromByteCount: bytes, countStyle: .memory))") }
                    if let implementation = metrics.implementation { Text(implementation) }
                }
                .font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(14)
        .background(line.role == "You" ? Color.accentColor.opacity(0.09) : Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
    }

    @ViewBuilder private func metric(_ label: String, seconds: Double?) -> some View {
        if let seconds { Text("\(label) \(seconds, specifier: "%.2f")s") }
    }

    private var statusColor: Color {
        switch controller.status {
        case .ready: .green
        case .starting, .inspecting, .stopping: .orange
        case .failed: .red
        case .stopped: .secondary
        }
    }
}
