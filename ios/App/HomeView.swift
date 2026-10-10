import SwiftUI
import UniformTypeIdentifiers

struct HomeView: View {
    @State private var workspace = DemoWorkspace.make()
    @State private var family = WorkflowFamily.overview
    @State private var filter = ""
    @State private var importSource = SourceKind.messages
    @State private var showImporter = false
    @State private var importError: String?
    @State private var result: WorkflowResult?

    var body: some View {
        TabView {
            NavigationStack {
                List {
                    Section {
                        Label(workspace.origin == .synthetic ? "Synthetic demo" : "Imported text only", systemImage: "lock.shield")
                            .font(.headline)
                        Text("Explore source excerpts and permission boundaries. Native accounts, actions and AI inference are not connected.")
                            .font(.subheadline).foregroundStyle(.secondary)
                    }
                    Section("Choose a workflow") {
                        Picker("Workflow", selection: $family) {
                            ForEach(WorkflowFamily.allCases) { item in Text("\(item.rawValue) · \(item.title)").tag(item) }
                        }
                        TextField("Literal contact, thread, event or city filter", text: $filter)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                        Button("Inspect available sources") { inspect() }
                            .accessibilityIdentifier("inspect-sources")
                    }
                    if let result {
                        Section(result.title) {
                            Text(statusTitle(result.status)).font(.headline)
                            if !result.clarificationFields.isEmpty {
                                Text("Details needed: " + result.clarificationFields.joined(separator: ", "))
                            }
                            ForEach(Array(result.evidence.enumerated()), id: \.offset) { _, evidence in
                                VStack(alignment: .leading, spacing: 6) {
                                    Text(evidence.quote).textSelection(.enabled)
                                    Text("\(evidence.source.rawValue) · \(evidence.id)").font(.caption).foregroundStyle(.secondary)
                                }.padding(.vertical, 4)
                            }
                            ForEach(result.limitations, id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary) }
                        }
                    }
                    Section("Bring selected text") {
                        Picker("Text source", selection: $importSource) {
                            ForEach([SourceKind.messages, .email, .notes, .memory], id: \.self) { Text($0.rawValue).tag($0) }
                        }
                        PasteButton(payloadType: String.self) { values in
                            guard let text = values.first else { return }
                            importData(Data(text.utf8))
                        }
                        Button("Import a UTF-8 text file") { showImporter = true }
                        Text("Imports replace the current workspace and remain in memory. Text carries no assumed sender, dates or unread state. Limit: 64 KiB.")
                            .font(.caption).foregroundStyle(.secondary)
                        Button("Reset to synthetic demo") { workspace = DemoWorkspace.make(); result = nil; filter = "" }
                    }
                }
                .navigationTitle("Wisp")
                .fileImporter(isPresented: $showImporter, allowedContentTypes: [.plainText]) { selection in
                    do {
                        let url = try selection.get()
                        let granted = url.startAccessingSecurityScopedResource()
                        defer { if granted { url.stopAccessingSecurityScopedResource() } }
                        // A bounded stream avoids loading an unbounded user-selected file into memory.
                        let handle = try FileHandle(forReadingFrom: url)
                        defer { try? handle.close() }
                        let data = try handle.read(upToCount: TextImport.maximumBytes + 1) ?? Data()
                        importData(data)
                    } catch { importError = error.localizedDescription }
                }
                .alert("Import could not finish", isPresented: Binding(get: { importError != nil }, set: { if !$0 { importError = nil } })) {
                    Button("OK", role: .cancel) { importError = nil }
                } message: { Text(importError ?? "") }
            }.tabItem { Label("Workspace", systemImage: "text.bubble") }
            NavigationStack {
                List {
                    Section("Platform access") {
                        ForEach(PlatformCapability.baseline, id: \.name) { capability in
                            VStack(alignment: .leading, spacing: 6) {
                                Text(capability.name).font(.headline)
                                Text(capability.detail).font(.subheadline).foregroundStyle(.secondary)
                            }.padding(.vertical, 4)
                        }
                    }
                    Section("Local AI") {
                        Text("The on-device Foundation Models adapter is implemented but disabled in this build. No model has been activated or measured.")
                        Text("A supported device, Apple Intelligence availability and a bounded runtime scope are required before enabling it. Demo source inspection uses deterministic code.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }.navigationTitle("Capabilities")
            }.tabItem { Label("Capabilities", systemImage: "checklist") }
        }.tint(.indigo)
    }
    private func inspect() { result = WorkflowEngine().evaluate(.init(family: family, filter: filter), in: workspace) }
    private func importData(_ data: Data) {
        do { workspace = try TextImport.workspace(data: data, source: importSource, clock: Date()); result = nil; filter = "" }
        catch { importError = error.localizedDescription }
    }
    private func statusTitle(_ status: ResultStatus) -> String {
        switch status {
        case .answered: "Source excerpts"
        case .clarify: "Needs details and an integration"
        case .prepared: "Preview only"
        case .notCompleted: "No result in available scope"
        case .unsupported: "Not connected"
        }
    }
}
