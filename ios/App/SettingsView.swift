import SwiftUI

struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        @Bindable var model = model
        NavigationStack {
            Form {
                Section {
                    Picker("Wisp runs on", selection: Binding(get: { model.engine }, set: { model.select($0) })) {
                        ForEach(EngineKind.allCases) { Label($0.title, systemImage: $0.symbol).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    Text(model.engine.privacyLine).font(.footnote).foregroundStyle(.secondary)
                } header: { Text("Where Wisp runs") }

                Section {
                    NavigationLink { MacSettingsView() } label: {
                        row("Mac", symbol: "laptopcomputer", detail: macDetail)
                    }
                    NavigationLink { CloudSettingsView() } label: {
                        row("Cloud", symbol: "cloud", detail: model.hasCloudKey ? model.cloudSettings.provider.title : "Not set up")
                    }
                    NavigationLink { OnDeviceSettingsView() } label: {
                        row("iPhone", symbol: "iphone", detail: onDeviceDetail)
                    }
                } header: { Text("Engines") }

                Section {
                    LabeledContent("Version", value: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "–")
                }
            }
            .navigationTitle("Settings")
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } } }
        }
    }

    private func row(_ title: String, symbol: String, detail: String) -> some View {
        HStack {
            Label(title, systemImage: symbol)
            Spacer()
            Text(detail).foregroundStyle(.secondary).lineLimit(1)
        }
    }

    private var macDetail: String {
        switch model.macStatus {
        case .notPaired: "Not paired"
        case .checking: "Checking…"
        case .connected(let name): name
        case .backendDown: "Wisp not running"
        case .problem: "Can't connect"
        }
    }

    private var onDeviceDetail: String {
        if !OnDeviceModel.isSupportedHere { return "Unavailable here" }
        switch model.onDevice.state {
        case .installed: return "Ling ready"
        case .downloading, .verifying: return "Copying…"
        default: return "Not installed"
        }
    }
}

// MARK: Mac

struct MacSettingsView: View {
    @Environment(AppModel.self) private var model
    @State private var link = ""
    @State private var linkError: String?
    @State private var showScanner = false

    var body: some View {
        @Bindable var model = model
        Form {
            if let pairing = model.pairing {
                Section("Paired Mac") {
                    LabeledContent("Mac", value: pairing.macName)
                    LabeledContent("Address", value: pairing.host ?? pairing.ip ?? "–")
                    LabeledContent("Status") { statusView }
                    Button("Check connection") { Task { await model.refreshMac() } }
                    Button("Unpair this iPhone", role: .destructive) { model.unpair() }
                }
            }
            Section {
                if QRScannerView.isAvailable {
                    Button { showScanner = true } label: { Label("Scan pairing code", systemImage: "qrcode.viewfinder") }
                }
                TextField("wisp-pair://v1?…", text: $link, axis: .vertical)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .font(.footnote.monospaced())
                Button("Pair with this link") { pair(link) }
                    .disabled(link.isEmpty)
                if let linkError {
                    Text(linkError).font(.footnote).foregroundStyle(Theme.danger)
                }
            } header: {
                Text(model.pairing == nil ? "Pair your Mac" : "Pair again")
            } footer: {
                Text("""
                1. Install Tailscale on your Mac and this iPhone, signed in to the same account.
                2. On the Mac, in the Wisp project folder: python -m service.phone_gateway pair
                3. Scan the code it shows, or open it with the Camera app.
                4. Keep the gateway running: python -m service.phone_gateway serve
                """)
            }
        }
        .navigationTitle("Mac")
        .sheet(isPresented: $showScanner) {
            QRScannerView { code in
                showScanner = false
                pair(code)
            }
            .ignoresSafeArea()
        }
        .task { if model.pairing != nil { await model.refreshMac() } }
    }

    @ViewBuilder private var statusView: some View {
        switch model.macStatus {
        case .notPaired: Text("Not paired")
        case .checking: ProgressView()
        case .connected: Label("Connected", systemImage: "checkmark.circle.fill").foregroundStyle(Theme.teal)
        case .backendDown: Text("Wisp isn't running").foregroundStyle(.orange)
        case .problem(let message): Text(message).font(.footnote).foregroundStyle(Theme.danger)
        }
    }

    private func pair(_ text: String) {
        do {
            try model.offerPairing(from: text)
            linkError = nil
            link = ""
        } catch {
            linkError = error.localizedDescription
        }
    }
}

// MARK: Cloud

struct CloudSettingsView: View {
    @Environment(AppModel.self) private var model
    @State private var key = ""
    @State private var testResult: String?
    @State private var testing = false

    var body: some View {
        @Bindable var model = model
        Form {
            Section {
                Picker("Provider", selection: Binding(get: { model.cloudSettings.provider }, set: { provider in
                    model.cloudSettings = CloudSettings(provider: provider,
                                                        maxTokens: model.cloudSettings.maxTokens)
                })) {
                    ForEach(CloudProvider.allCases) { Text($0.title).tag($0) }
                }
                TextField("Base URL", text: $model.cloudSettings.baseURL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                TextField("Model ID", text: $model.cloudSettings.model)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Stepper("Max reply: \(model.cloudSettings.maxTokens) tokens",
                        value: $model.cloudSettings.maxTokens, in: 256...8192, step: 256)
            } header: { Text("Provider") } footer: {
                Text("Any OpenAI-compatible chat completions API. Cloud mode has no access to your mail, calendar or files.")
            }
            Section {
                SecureField(model.hasCloudKey ? "Saved. Type to replace" : "API key", text: $key)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Button("Save key") {
                    model.saveCloudKey(key)
                    key = ""
                }
                .disabled(key.isEmpty)
                if model.hasCloudKey {
                    Button("Remove key", role: .destructive) { model.saveCloudKey("") }
                }
            } header: { Text("API key") } footer: {
                Text("Stored only in this iPhone's Keychain. It never syncs to iCloud or your other devices.")
            }
            Section {
                Button {
                    testing = true
                    Task {
                        testResult = await model.testCloud()
                        testing = false
                    }
                } label: {
                    HStack { Text("Test connection"); if testing { Spacer(); ProgressView() } }
                }
                .disabled(!model.hasCloudKey || testing)
                if let testResult { Text(testResult).font(.footnote) }
            }
        }
        .navigationTitle("Cloud")
    }
}

// MARK: iPhone

struct OnDeviceSettingsView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Form {
            Section {
                switch model.onDevice.state {
                case .notInstalled:
                    Text("Ling isn't on this iPhone yet.")
                case .downloading(let file, let received, let total):
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Copying \(file) from your Mac")
                        ProgressView(value: total > 0 ? Double(received) / Double(total) : 0)
                        Text("\(OnDeviceModel.format(received)) of \(OnDeviceModel.format(total)). Keep Wisp open.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Button("Cancel", role: .destructive) { model.onDevice.cancelDownload() }
                case .verifying(let file):
                    HStack { Text("Verifying \(file)"); Spacer(); ProgressView() }
                case .installed(let id, let bytes):
                    LabeledContent("Model", value: id)
                    LabeledContent("Size", value: OnDeviceModel.format(bytes))
                    Button("Delete from iPhone", role: .destructive) { model.onDevice.delete() }
                case .failed(let message):
                    Text(message).foregroundStyle(Theme.danger)
                }
            } header: { Text("Ling on this iPhone") } footer: {
                Text("Ling runs entirely on the phone with MLX. Nothing you type in iPhone mode leaves the device. It has no tools, so it can't read your mail or calendar; use Mac mode for that.")
            }

            if !OnDeviceModel.isSupportedHere {
                Section {
                    Text(OnDeviceError.unsupported.localizedDescription).font(.footnote)
                }
            }

            Section {
                Button {
                    if let gateway = model.gateway { model.onDevice.download(from: gateway) }
                } label: { Label("Copy Ling from my Mac", systemImage: "arrow.down.circle") }
                .disabled(model.gateway == nil || model.onDevice.isBusy)
            } footer: {
                Text(model.gateway == nil
                     ? "Pair your Mac first. The Mac sends the model over Tailscale, about 4.3 GB, verified file by file."
                     : "About 4.3 GB over Tailscale. Every file's SHA-256 is checked against your Mac's copy before it's used.")
            }
        }
        .navigationTitle("iPhone")
        .onAppear { if !model.onDevice.isBusy { model.onDevice.refresh() } }
    }
}
