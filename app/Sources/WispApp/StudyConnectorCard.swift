import SwiftUI
import AppKit

/// The Study connector, inline in the tab: which cloud provider writes study
/// material, its key (kept in the Keychain, never shown again) and the model.
/// Everything stays in the panel: no sheets, no pop-up menus.
struct StudyConnectorCard: View {
    @ObservedObject var study: StudyModel
    /// Refits the panel when the card grows or shrinks (the key field, the
    /// saving row, the remove confirmation).
    var onResize: () -> Void = {}
    @State private var providerID = ""
    @State private var modelID = ""
    @State private var key = ""
    /// Whether this Mac's Keychain holds a Study key for the chosen provider.
    @State private var hasKey = false
    /// A saved key is being replaced: the field shows.
    @State private var replacing = false
    /// "Remove key" was pressed once and waits for the second, inline yes.
    @State private var confirmingRemove = false
    @FocusState private var keyFocused: Bool

    private var connector: StudyConnectorState? { study.state?.connector }
    private var providers: [StudyProvider] { connector?.providers ?? [] }
    private var provider: StudyProvider? {
        providers.first { $0.id == providerID } ?? providers.first
    }
    private var saving: Bool {
        if case .saving? = study.busy { return true }
        return false
    }
    private var showsField: Bool { !hasKey || replacing }
    private var trimmedKey: String { key.trimmingCharacters(in: .whitespacesAndNewlines) }
    /// Save needs a new key unless one is saved and only the model changes.
    private var canSave: Bool {
        guard provider != nil, !saving else { return false }
        return !trimmedKey.isEmpty || hasKey
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            Text(StudyText.connectorIntro)
                .font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if connector?.usesCloudFallback == true { cloudLine }
            if providers.count > 1 { providerSwitcher }
            if let provider {
                keyArea(provider)
                modelRow(provider)
            }
            actions
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(Theme.raised))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.hairline, lineWidth: 1))
        .studyReportsHeight(onResize)
        .onAppear(perform: loadChoice)
        .onChange(of: providerID) { _, _ in providerChanged() }
        .onChange(of: keyFocused) { _, focused in study.editing = focused }
        .onDisappear { study.editing = false; key = "" }
    }

    // MARK: Pieces

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(StudyText.connectorTitle).font(.wisp(.callout, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                .accessibilityAddTraits(.isHeader)
            Spacer()
            NotchChrome.iconButton("xmark", help: "Close the Study connector") { close() }
        }
    }

    private var cloudLine: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: "cloud").font(.wisp(size: 10.5)).foregroundStyle(Theme.textMuted)
                .accessibilityHidden(true)
            Text(StudyText.connectorFallback)
                .font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var providerSwitcher: some View {
        HStack(spacing: 0) {
            ForEach(providers) { option in
                providerButton(option)
            }
        }
        .padding(1)
        .overlay(Capsule().strokeBorder(Theme.chipStroke, lineWidth: 1))
        .fixedSize()
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Provider")
    }

    private func providerButton(_ option: StudyProvider) -> some View {
        let chosen = option.id == provider?.id
        return Button { providerID = option.id } label: {
            Text(option.label)
                .font(.wisp(.body, weight: chosen ? .semibold : .regular))
                .foregroundStyle(chosen ? Color.black : Theme.textSecondary)
                .padding(.horizontal, 12).padding(.vertical, 4)
                .frame(minHeight: 24)
                .background(Capsule().fill(chosen ? Color.white : Color.clear))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
        .disabled(saving)
        .accessibilityAddTraits(chosen ? [.isButton, .isSelected] : .isButton)
    }

    @ViewBuilder private func keyArea(_ provider: StudyProvider) -> some View {
        if showsField {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    SecureField(provider.keyHint.isEmpty ? StudyText.keyPlaceholder : provider.keyHint, text: $key)
                        .textFieldStyle(.plain)
                        .font(.wisp(size: 13, design: .monospaced)).foregroundStyle(Theme.textPrimary)
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
                        .overlay(RoundedRectangle(cornerRadius: 8)
                            .stroke(keyFocused ? Color.white.opacity(0.35) : Theme.chipStroke, lineWidth: 1))
                        .focused($keyFocused)
                        .onSubmit(save)
                        .disabled(saving)
                        .accessibilityLabel("\(provider.label) API key")
                    getKeyLink(provider)
                }
                Text(hasKey ? StudyText.keyKept : Copy.keyPlace)
                    .font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
            }
        } else if confirmingRemove {
            removeConfirm(provider)
        } else {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "key.fill").font(.wisp(size: 10.5)).foregroundStyle(Theme.textSecondary)
                    .accessibilityHidden(true)
                Text(Copy.keySaved).font(.wisp(.body)).foregroundStyle(Theme.textPrimary)
                Spacer(minLength: 8)
                NotchChrome.small("Replace key") {
                    replacing = true
                    keyFocused = true
                }
                .disabled(saving)
                Button(StudyText.removeKeyButton) { confirmingRemove = true }
                    .buttonStyle(.plain)
                    .font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                    .frame(minHeight: 24)
                    .disabled(saving)
                    .help("Remove the saved \(provider.label) key from this Mac")
            }
        }
    }

    /// The second step of "Remove key", inline like the tab's "Remove school data".
    private func removeConfirm(_ provider: StudyProvider) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(StudyText.removeKeyConfirm)
                .font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button { removeKey(provider) } label: {
                    Text(StudyText.removeKeyYes).font(.wisp(.footnote, weight: .medium)).foregroundStyle(Theme.bad)
                        .frame(minHeight: 24)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(saving)
                StudyChrome.pill(StudyText.removeKeyNo) { confirmingRemove = false }
            }
        }
    }

    private func getKeyLink(_ provider: StudyProvider) -> some View {
        Button {
            if let url = provider.keyLink { NSWorkspace.shared.open(url) }
        } label: {
            HStack(spacing: 3) {
                Text(StudyText.getKey)
                Image(systemName: "arrow.up.right").font(.wisp(size: 8.5, weight: .semibold))
            }
            .font(.wisp(.footnote, weight: .medium)).foregroundStyle(Theme.textSecondary)
            .frame(minHeight: 24)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .fixedSize()
        .help("Opens \(provider.label)'s key page in your browser")
    }

    @ViewBuilder private func modelRow(_ provider: StudyProvider) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("Model").font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
            if provider.models.count > 1 {
                modelChoices(provider)
            } else {
                Text(provider.models.first?.label ?? modelLabel(provider))
                    .font(.wisp(.body)).foregroundStyle(Theme.textPrimary)
            }
            Spacer(minLength: 0)
        }
    }

    /// Several models: small chips that wrap onto a second line when they need to.
    private func modelChoices(_ provider: StudyProvider) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 6) { modelChips(provider) }
            VStack(alignment: .leading, spacing: 6) { modelChips(provider) }
        }
    }

    @ViewBuilder private func modelChips(_ provider: StudyProvider) -> some View {
        ForEach(provider.models) { option in
            let chosen = option.id == currentModelID(provider)
            Button { modelID = option.id } label: {
                Text(option.label)
                    .font(.wisp(.footnote, weight: chosen ? .semibold : .regular))
                    .foregroundStyle(chosen ? Color.black : Theme.textSecondary)
                    .padding(.horizontal, 10)
                    .frame(minHeight: 24)
                    .background(Capsule().fill(chosen ? Color.white : Theme.chipFill))
                    .overlay(Capsule().stroke(chosen ? Color.clear : Theme.chipStroke, lineWidth: 1))
                    .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .fixedSize()
            .disabled(saving)
            .accessibilityAddTraits(chosen ? [.isButton, .isSelected] : .isButton)
        }
    }

    private var actions: some View {
        HStack(spacing: 8) {
            if saving {
                ProgressView().controlSize(.small)
                Text("Saving…").font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
            }
            Spacer()
            StudyChrome.pill("Cancel") { close() }
                .keyboardShortcut(.cancelAction)
            StudyChrome.pill(StudyText.saveKeyButton, icon: "checkmark", primary: true) { save() }
                .disabled(!canSave)
        }
    }

    // MARK: Choices

    private func currentModelID(_ provider: StudyProvider) -> String {
        if provider.models.contains(where: { $0.id == modelID }) { return modelID }
        return provider.defaultModel
    }

    private func modelLabel(_ provider: StudyProvider) -> String {
        let id = currentModelID(provider)
        return provider.models.first { $0.id == id }?.label ?? id
    }

    private func loadChoice() {
        guard let connector else { return }
        providerID = connector.provider
        modelID = connector.modelID
        refreshHasKey()
    }

    private func providerChanged() {
        replacing = false
        confirmingRemove = false
        key = ""
        if let provider, !provider.models.contains(where: { $0.id == modelID }) {
            modelID = provider.defaultModel
        }
        refreshHasKey()
    }

    private func refreshHasKey() {
        guard let provider else { hasKey = false; return }
        #if WISP_NOTCH_QA
        hasKey = connector?.hasOwnKey == true && connector?.provider == provider.id
        #else
        hasKey = StudyCredentialStore.has(provider: provider.id)
        #endif
    }

    private func save() {
        guard canSave, let provider else { return }
        let newKey: String? = trimmedKey.isEmpty ? nil : trimmedKey
        let chosenModel = currentModelID(provider)
        keyFocused = false
        Task {
            // The field keeps the key until the Keychain has it, so a failed
            // save can be retried without pasting again.
            let saved = await study.saveConnector(provider: provider.id, key: newKey, modelID: chosenModel)
            if saved {
                key = ""
                replacing = false
            }
            refreshHasKey()
        }
    }

    private func removeKey(_ provider: StudyProvider) {
        let chosenModel = currentModelID(provider)
        Task {
            await study.removeKey(provider: provider.id, modelID: chosenModel)
            confirmingRemove = false
            replacing = false
            key = ""
            refreshHasKey()
        }
    }

    private func close() {
        key = ""
        replacing = false
        confirmingRemove = false
        keyFocused = false
        study.editing = false
        study.showingConnector = false
    }

    // MARK: Copy

    private enum Copy {
        static let keySaved = "Key saved in your Keychain"
        static let keyPlace = "Kept in your Keychain. Wisp never shows it again."
    }
}
