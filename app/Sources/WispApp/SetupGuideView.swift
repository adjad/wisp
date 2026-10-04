import AppKit
import SwiftUI

extension Notification.Name {
    /// Posted by Settings (or anything else) to open the setup guide.
    static let wispOpenSetupGuide = Notification.Name("WispOpenSetupGuide")
}

// MARK: - Wire types (GET /setup/status)

struct SetupStatus: Decodable {
    struct Hardware: Decodable { let chip: String; let ramGb: Int; let tier: String }
    struct Omlx: Decodable {
        let installed: Bool; let running: Bool; let modelDir: String?
        let minMacos: Int; let url: String; let adminUrl: String
    }
    struct ModelEntry: Decodable, Identifiable {
        let id: String; let label: String; let sizeGb: Double?
        let fit: String; let tested: Bool; let note: String
    }
    struct Download: Decodable {
        let model: String; let label: String; let sizeGb: Double?
        let repo: String; let url: String; let command: String; let installHf: String
    }
    struct Models: Decodable {
        let source: String; let installed: [ModelEntry]; let recommended: String
        let use: String?; let warning: String?
        let download: Download?; let embeddingDownload: Download?
    }
    struct RoleRow: Decodable, Identifiable {
        let role: String; let label: String; let model: String; let endpoint: String
        var id: String { role }
    }
    struct Engine: Decodable, Identifiable {
        let id: String; let label: String; let port: Int?; let runsTools: Bool
        let summary: String; let howToStart: String; let url: String?
        let running: Bool; let models: Int; let modelIds: [String]?
    }
    struct Check: Decodable, Identifiable {
        let id: String; let state: String; let title: String; let detail: String
        let action: String?; let url: String?
    }
    let ready: Bool
    let hardware: Hardware
    let omlx: Omlx
    let models: Models
    let roles: [RoleRow]
    let engines: [Engine]
    let checks: [Check]
    let disclosure: String
}

// MARK: - Model

@MainActor
final class SetupGuideModel: ObservableObject {
    @Published var status: SetupStatus?
    @Published var loading = false
    @Published var busy: String?
    @Published var message: String?
    @Published var messageIsError = false
    @Published var selectedModel: String?
    @Published var copied: String?
    @Published var testResult: String?
    @Published var testing = false
    // Connecting another local app
    @Published var connectModel: [String: String] = [:]
    @Published var customPort = ""
    @Published var customModels: [String] = []

    private struct APIError: LocalizedError {
        let errorDescription: String?
    }

    private func call(_ method: String, _ path: String, body: [String: Any]? = nil,
                      timeout: TimeInterval = 20) async throws -> Data {
        var request = URLRequest(url: WispClient.baseURL.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body { request.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            throw APIError(errorDescription: detail ?? "Wisp returned an unexpected response.")
        }
        return data
    }

    private func decode(_ data: Data) throws -> SetupStatus {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(SetupStatus.self, from: data)
    }

    private func fail(_ error: Error) {
        message = error.localizedDescription
        messageIsError = true
    }

    /// The backend may still be starting when the guide first opens, so retry briefly.
    func refresh(quiet: Bool = false) async {
        loading = true
        defer { loading = false }
        var lastError: Error?
        // Wait for a starting backend only on the first load; a refresh of what is
        // already on screen tries once and says so if it fails.
        for attempt in 0..<(status == nil ? 6 : 1) {
            do {
                let next = try decode(try await call("GET", "setup/status"))
                status = next
                if selectedModel == nil || !next.models.installed.contains(where: { $0.id == selectedModel }) {
                    selectedModel = next.models.use ?? next.models.installed.first?.id
                }
                if !quiet { message = nil }
                return
            } catch {
                lastError = error
                try? await Task.sleep(nanoseconds: UInt64(500_000_000 * (attempt + 1)))
            }
        }
        if let lastError {
            fail(APIError(errorDescription: status == nil
                ? "Wisp's service isn't answering yet. Give it a few seconds, then choose Check again. (\(lastError.localizedDescription))"
                : "Wisp couldn't refresh this list, so it is showing what it saw last. (\(lastError.localizedDescription))"))
        }
    }

    func startEngine() async {
        busy = "Starting oMLX…"
        defer { busy = nil }
        do {
            status = try decode(try await call("POST", "setup/start-engine", body: [:], timeout: 100))
            message = nil
        } catch { fail(error) }
    }

    func apply(_ model: String) async {
        busy = "Applying…"
        defer { busy = nil }
        do {
            let repointsReasoning = status?.roles.contains { $0.endpoint != "local" } ?? false
            status = try decode(try await call("POST", "setup/apply", body: ["model": model]))
            message = "Wisp will use \(model) for chat, tools, coding, and reasoning."
                + (repointsReasoning ? " Reasoning had been pointed at another app; it now uses this model too." : "")
            messageIsError = false
        } catch { fail(error) }
    }

    func connectExternal(port: Int, model: String, withTools: Bool = false) async {
        busy = withTools ? "Testing tool calling… this can take up to a minute." : "Testing the connection…"
        defer { busy = nil }
        do {
            _ = try await call("POST", "inference/local-provider", body: [
                "base_url": "http://127.0.0.1:\(port)", "api_prefix": "/v1",
                "model_id": model, "context_window": withTools ? 16384 : 8192,
                "roles": withTools ? ["reasoning", "agent"] : ["reasoning"],
            ], timeout: withTools ? 420 : 60)
            message = withTools
                ? "Connected \(model) for Reasoning and Agent. Wisp tested tool calling and saved the context window the app really uses."
                : "Connected \(model) for Reasoning. Tools and everything else stay on oMLX."
            messageIsError = false
            await refresh(quiet: true)
        } catch { fail(error) }
    }

    func findCustomModels() async {
        guard let port = Int(customPort.trimmingCharacters(in: .whitespaces)) else {
            message = "Enter the app's port number, for example 8767."
            messageIsError = true
            return
        }
        busy = "Looking for models…"
        defer { busy = nil }
        do {
            let data = try await call("POST", "inference/local-provider/probe", body: [
                "base_url": "http://127.0.0.1:\(port)", "api_prefix": "/v1"], timeout: 40)
            let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            customModels = (object?["models"] as? [String]) ?? []
            if customModels.isEmpty {
                message = "That app answered but listed no models."
                messageIsError = true
            } else {
                connectModel["custom"] = customModels.first
                message = nil
            }
        } catch { fail(error) }
    }

    func test() async {
        testing = true
        testResult = nil
        defer { testing = false }
        let started = Date()
        do {
            let data = try await call("POST", "chat", body: [
                "role": "general", "prompt": "Reply with the single word OK.", "max_tokens": 512,
            ], timeout: 120)
            let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            let text = (object?["content"] as? String ?? "")
                .replacingOccurrences(of: "(?s)<think>.*?</think>", with: "", options: .regularExpression)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let seconds = String(format: "%.1f", Date().timeIntervalSince(started))
            testResult = text.isEmpty ? "The model replied but gave no visible answer. It may be spending its whole budget thinking; try again or pick another model."
                                      : "Works — replied in \(seconds)s."
        } catch {
            testResult = "That didn't work: \(error.localizedDescription)"
        }
    }

    func copy(_ text: String, tag: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        copied = tag
        Task {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            if copied == tag { copied = nil }
        }
    }

    func open(_ urlString: String?) {
        guard let urlString, let url = URL(string: urlString) else { return }
        NSWorkspace.shared.open(url)
    }
}

// MARK: - View

struct SetupGuideView: View {
    @StateObject private var model: SetupGuideModel
    var onDone: () -> Void
    var openSettings: () -> Void
    private let height: CGFloat

    init(model: SetupGuideModel? = nil, height: CGFloat = 700,
         onDone: @escaping () -> Void = {}, openSettings: @escaping () -> Void = {}) {
        _model = StateObject(wrappedValue: model ?? SetupGuideModel())
        self.height = height
        self.onDone = onDone
        self.openSettings = openSettings
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().overlay(Theme.hairline)
            ScrollView { pageContent }
            Divider().overlay(Theme.hairline)
            footer
        }
        .frame(width: 640, height: height)
        .background(Theme.surface)
        .foregroundStyle(Theme.textPrimary)
        .preferredColorScheme(.dark)
        .task { await model.refresh() }
    }

    /// The scrolling page. Internal so an offscreen renderer, which cannot draw a
    /// ScrollView, can lay it out directly.
    var pageContent: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let status = model.status {
                engineSection(status)
                modelSection(status)
                searchSection(status)
                otherEnginesSection(status)
                testSection(status)
            } else if model.loading {
                HStack(spacing: 8) { ProgressView().controlSize(.small)
                    Text("Checking this Mac…").foregroundStyle(Theme.textSecondary) }
            }
            if let text = model.message {
                Text(text).font(.callout)
                    .foregroundStyle(model.messageIsError ? Color.orange : Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(24)
    }

    // MARK: Header / footer

    var header: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Set up Wisp's AI engine").font(.system(size: 19, weight: .semibold))
                if let s = model.status {
                    Text("\(s.hardware.chip) · \(s.hardware.ramGb > 0 ? "\(s.hardware.ramGb) GB memory" : "memory unknown")")
                        .font(.callout).foregroundStyle(Theme.textSecondary)
                } else {
                    Text("Everything runs on this Mac.").font(.callout).foregroundStyle(Theme.textSecondary)
                }
            }
            Spacer()
            if let s = model.status {
                pill(s.ready ? "Ready" : "Needs attention", on: s.ready)
            }
        }
        .padding(.horizontal, 24).padding(.vertical, 18)
    }

    var footer: some View {
        HStack {
            if let busy = model.busy {
                ProgressView().controlSize(.small)
                Text(busy).font(.caption).foregroundStyle(Theme.textSecondary)
            }
            Spacer()
            Button("Check again") { Task { await model.refresh() } }.disabled(model.busy != nil)
            Button("Settings…") { openSettings() }
            Button("Done") { onDone() }.keyboardShortcut(.defaultAction)
        }
        .padding(.horizontal, 24).padding(.vertical, 14)
    }

    // MARK: 1 · Engine

    private func engineSection(_ s: SetupStatus) -> some View {
        let check = s.checks.first { $0.id == "engine" }
        return card(step: "1", title: "Engine", state: check?.state ?? "todo") {
            if let check {
                Text(check.detail).font(.callout).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack {
                    if check.action == "install_omlx" {
                        action("Get oMLX") { model.open(check.url) }
                        Text("Then open it once and choose Check again.")
                            .font(.caption).foregroundStyle(Theme.textMuted)
                    } else if check.action == "start_engine" {
                        action("Start oMLX") { Task { await model.startEngine() } }
                    }
                }
            }
        }
    }

    // MARK: 2 · Model

    private func modelSection(_ s: SetupStatus) -> some View {
        let check = s.checks.first { $0.id == "model" }
        return card(step: "2", title: "Model", state: check?.state ?? "todo") {
            recommendationText(s)
            if let check, check.state != "ok" {
                Text(check.detail).font(.callout).foregroundStyle(Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let warning = s.models.warning {
                Text(warning).font(.caption).foregroundStyle(Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if s.models.installed.isEmpty {
                Text(s.omlx.running ? "No chat model is installed in oMLX yet."
                                    : "Your models will appear here once oMLX is running.")
                    .font(.callout).foregroundStyle(Theme.textSecondary)
            } else {
                VStack(spacing: 6) {
                    ForEach(s.models.installed) { entry in modelRow(entry, current: currentModels(s)) }
                }
                HStack {
                    action("Use this model") {
                        if let id = model.selectedModel { Task { await model.apply(id) } }
                    }
                    .disabled(model.selectedModel == nil || model.busy != nil || !s.omlx.running)
                    Text("Sets chat, tools, coding, and reasoning.")
                        .font(.caption).foregroundStyle(Theme.textMuted)
                }
            }
            if let download = s.models.download {
                downloadCard(download, headline: s.models.use == nil
                             ? "Recommended for this Mac" : "A better fit for this Mac", tag: "main",
                             omlx: s.omlx)
            }
        }
    }

    private func recommendationText(_ s: SetupStatus) -> some View {
        let text: String
        switch s.hardware.tier {
        case "roomy", "comfortable":
            text = "Ling 3.0 tiny at 6-bit runs comfortably here and gives the best quality."
        case "standard":
            text = "On a 16 GB Mac, the 5-bit or 4-bit Ling 3.0 tiny keeps plenty of memory free for your other apps."
        default:
            text = "Use the 4-bit Ling 3.0 tiny; it is the smallest build that still picks tools reliably."
        }
        return Text(text).font(.callout).foregroundStyle(Theme.textSecondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func currentModels(_ s: SetupStatus) -> Set<String> {
        Set(s.roles.filter { $0.endpoint == "local" }.map(\.model))
    }

    private func modelRow(_ entry: SetupStatus.ModelEntry, current: Set<String>) -> some View {
        let selected = model.selectedModel == entry.id
        return Button { model.selectedModel = entry.id } label: {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(selected ? Theme.textPrimary : Theme.textMuted)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(entry.label).font(.system(size: 13, weight: .medium))
                        if let gb = entry.sizeGb {
                            Text(String(format: "%.1f GB", gb)).font(.caption).foregroundStyle(Theme.textMuted)
                        }
                        if current.contains(entry.id) { pill("In use", on: true) }
                        fitBadge(entry.fit)
                    }
                    Text(entry.note).font(.caption).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10)
                .fill(selected ? Color.white.opacity(0.10) : Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 10)
                .stroke(selected ? Color.white.opacity(0.35) : Theme.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private func fitBadge(_ fit: String) -> some View {
        switch fit {
        case "recommended": pill("Recommended", on: true)
        case "heavy": pill("Heavy for this Mac", on: false)
        case "unknown": pill("Untested", on: false)
        default: EmptyView()
        }
    }

    // MARK: 3 · Smart Search

    private func searchSection(_ s: SetupStatus) -> some View {
        let check = s.checks.first { $0.id == "search" }
        return card(step: "3", title: "Smart Search (optional)", state: check?.state ?? "warn") {
            Text(check?.detail ?? "").font(.callout).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if let download = s.models.embeddingDownload {
                downloadCard(download, headline: "Adds semantic search to ⌘⇧F", tag: "embed", omlx: s.omlx)
            }
        }
    }

    // MARK: Download helper card

    private func downloadCard(_ d: SetupStatus.Download, headline: String, tag: String,
                              omlx: SetupStatus.Omlx) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(headline).font(.system(size: 12, weight: .semibold))
                Spacer()
                Text("\(d.label)\(d.sizeGb.map { String(format: " · %.1f GB", $0) } ?? "")")
                    .font(.caption).foregroundStyle(Theme.textSecondary)
            }
            if omlx.running {
                Text("Wisp doesn't download models for you. The easiest way is oMLX's Model Downloader: "
                     + "open it, search for the name below, and download it.")
                    .font(.caption).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                HStack {
                    action("Open oMLX downloader") { model.open(omlx.adminUrl) }
                    action(model.copied == tag + "-repo" ? "Copied" : "Copy \(d.repo)") {
                        model.copy(d.repo, tag: tag + "-repo")
                    }
                    Spacer()
                }
                Text("Or run this in Terminal:").font(.caption).foregroundStyle(Theme.textMuted)
            } else {
                Text("Wisp doesn't download models for you. Once oMLX is running you can use its Model Downloader, "
                     + "or run this in Terminal:")
                    .font(.caption).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(d.command)
                .font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                .padding(8).frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.white.opacity(0.06)))
            HStack {
                action(model.copied == tag ? "Copied" : "Copy command") { model.copy(d.command, tag: tag) }
                action("Open on Hugging Face") { model.open(d.url) }
                Spacer()
            }
            Text("The Terminal route needs the `hf` tool: \(d.installHf). Then choose Check again.")
                .font(.caption2).foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.chipStroke, lineWidth: 1))
    }

    // MARK: 4 · Other engines

    private func otherEnginesSection(_ s: SetupStatus) -> some View {
        card(step: "4", title: "Other local apps (optional)", state: "info") {
            Text("Ollama, LM Studio, llama.cpp, MTPLX and similar apps on this Mac can answer chat and reasoning. "
                 + "Choose “Connect with tools” and Wisp tests whether the app can run its tools reliably, "
                 + "including the context window it really uses, before turning tool use on.")
                .font(.callout).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Text(s.disclosure).font(.caption).foregroundStyle(Color.orange)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(s.engines.filter { $0.id != "omlx" }) { engine in engineCard(engine) }
        }
    }

    private func engineCard(_ e: SetupStatus.Engine) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(e.label).font(.system(size: 13, weight: .medium))
                if let port = e.port { Text(":" + String(port)).font(.caption).foregroundStyle(Theme.textMuted) }
                pill(e.running ? "Running" : "Not detected", on: e.running)
                Spacer()
                if let url = e.url, !e.running { action("Website") { model.open(url) } }
            }
            Text(e.summary).font(.caption).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if e.running, let port = e.port, let ids = e.modelIds, !ids.isEmpty {
                connectRow(key: e.id, port: port, models: ids)
            } else if e.id == "custom" {
                HStack {
                    TextField("Port", text: $model.customPort).textFieldStyle(.roundedBorder).frame(width: 90)
                    action("Find models") { Task { await model.findCustomModels() } }
                        .disabled(model.busy != nil)
                    Spacer()
                }
                if !model.customModels.isEmpty, let port = Int(model.customPort) {
                    connectRow(key: "custom", port: port, models: model.customModels)
                }
                Text(e.howToStart).font(.caption2).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !e.running {
                Text(e.howToStart).font(.caption2).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.chipStroke, lineWidth: 1))
    }

    private func connectRow(key: String, port: Int, models: [String]) -> some View {
        let binding = Binding<String>(
            get: { model.connectModel[key] ?? models.first ?? "" },
            set: { model.connectModel[key] = $0 })
        return HStack {
            Picker("", selection: binding) {
                ForEach(models, id: \.self) { Text($0).tag($0) }
            }
            .labelsHidden().frame(maxWidth: 260)
            action("Connect for Reasoning") { Task { await model.connectExternal(port: port, model: binding.wrappedValue) } }
                .disabled(model.busy != nil)
            action("Connect with tools") {
                Task { await model.connectExternal(port: port, model: binding.wrappedValue, withTools: true) }
            }
            .disabled(model.busy != nil)
            Spacer()
        }
    }

    // MARK: 5 · Test

    private func testSection(_ s: SetupStatus) -> some View {
        card(step: "5", title: "Try it", state: "info") {
            HStack {
                action(model.testing ? "Testing…" : "Send a test message") { Task { await model.test() } }
                    .disabled(model.testing || model.busy != nil || !s.ready)
                if let result = model.testResult {
                    Text(result).font(.callout).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                } else if !s.ready {
                    Text("Finish steps 1 and 2 first.").font(.caption).foregroundStyle(Theme.textMuted)
                }
            }
        }
    }

    // MARK: Building blocks

    private func card<Content: View>(step: String, title: String, state: String,
                                     @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                stateGlyph(state, step: step)
                Text(title).font(.system(size: 15, weight: .semibold))
                Spacer()
            }
            content()
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 14).fill(Color.white.opacity(0.04)))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.hairline, lineWidth: 1))
    }

    @ViewBuilder
    private func stateGlyph(_ state: String, step: String) -> some View {
        switch state {
        case "ok":
            Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.textPrimary)
        case "warn":
            Image(systemName: "exclamationmark.circle").foregroundStyle(Color.orange)
        case "todo":
            Image(systemName: "\(step).circle").foregroundStyle(Theme.textPrimary)
        default:
            Image(systemName: "\(step).circle").foregroundStyle(Theme.textMuted)
        }
    }

    private func pill(_ text: String, on: Bool) -> some View {
        Text(text).font(.system(size: 10, weight: .semibold))
            .foregroundStyle(on ? Color.black : Theme.textSecondary)
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(Capsule().fill(on ? Color.white : Theme.chipFill))
            .overlay(Capsule().stroke(on ? Color.clear : Theme.chipStroke, lineWidth: 1))
    }

    private func action(_ title: String, perform: @escaping () -> Void) -> some View {
        Button(action: perform) {
            Text(title).font(.system(size: 12, weight: .medium))
                .padding(.horizontal, 10).padding(.vertical, 5)
                .background(Capsule().fill(Color.white.opacity(0.12)))
                .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
    }
}
