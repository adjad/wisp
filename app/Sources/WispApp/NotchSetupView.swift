import SwiftUI

/// First-run setup in the notch: the two required steps (starting up and a model)
/// and the test, in short form. The model download runs here too, for Wisp's own
/// engine (NotchSetupDownload.swift). Other local apps, an external
/// oMLX's downloads and models with no public download stay in the full guide,
/// Settings › Models › Setup in the Wisp window, which shares this card's SetupGuideModel, so readiness, "Not validated" pills,
/// the download and the test result read the same in both.
///
/// No sheets or alerts: the notch is a non-activating panel.
struct NotchSetupView: View {
    @ObservedObject var model: OverlayModel
    @ObservedObject var setup: SetupGuideModel
    @ObservedObject var notices: NotchSetupNotices = .shared
    static let host = "notch"

    private var ready: Bool { setup.statusIsCurrent && setup.status?.ready == true }

    var body: some View {
        Group {
            if model.setupCard == .open { card } else { laterRow }
        }
        // The card is a host while it is on screen: collapsing the panel, or
        // leaving Ask, takes it off screen and stops the poll.
        .onAppear { setup.show(in: Self.host) }
        .onDisappear { setup.hide(from: Self.host) }
        .onChange(of: ready) { _, ready in model.setupCard = model.setupCard.afterReadiness(ready) }
    }

    // MARK: Set aside

    private var laterRow: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.circle").foregroundStyle(Color.orange)
            VStack(alignment: .leading, spacing: 1) {
                Text("Wisp isn't set up yet").font(.system(size: 12.5, weight: .medium)).foregroundStyle(Theme.textPrimary)
                Text(laterDetail).font(.system(size: 11)).foregroundStyle(Theme.textMuted)
            }
            Spacer(minLength: 8)
            chip("Continue setup") {
                // A first-run setup left part-way goes on where it was, in its window.
                if FirstRunSetup.isUnfinished({ UserDefaults.standard.bool(forKey: $0) }) {
                    NotificationCenter.default.post(name: .wispContinueFirstRun, object: nil)
                } else {
                    model.setupCard = .open
                }
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
        .padding(.top, 8)
    }

    /// Set aside mid-download, the line says how the model is coming along (or
    /// that the Mac slept on it).
    private var laterDetail: String {
        let brain = FirstRunBrain.from(status: setup.status, downloads: setup.downloads)
        if let slept = sleepLine(brain) { return slept }
        if brain.isArriving, case .working(let text) = brain.footer(stalled: false) { return text }
        return "It needs a model before it can answer."
    }

    /// The line a wake mid-download leaves, until the download moves again,
    /// finishes, or half an hour has passed (SleepPause.line).
    private func sleepLine(_ brain: FirstRunBrain) -> String? {
        notices.sleepLine(brain, downloads: setup.downloads)
    }

    // MARK: The card

    /// The download is mentioned only to someone who has no model yet: with one
    /// installed, setup is a Start or a test.
    private var welcomeDetail: String {
        let needsDownload = setup.status?.models.installed.isEmpty ?? true
        return needsDownload
            ? "Wisp needs a model before it can answer: a one-time download. Everything runs on this Mac."
            : "Wisp needs a model before it can answer. Everything runs on this Mac."
    }

    private var card: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Welcome to Wisp").font(.system(size: 14, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                    Text(welcomeDetail)
                        .font(.system(size: 11.5)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                if setup.status != nil { pill(setup.readinessLabel, on: ready) }
            }
            if let status = setup.status {
                engineStep(status)
                modelStep(status)
                testStep(status)
            } else if setup.loading {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Checking this Mac…").font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                }
            }
            if let text = setup.message {
                note(text, warning: setup.messageIsError)
            }
            if let error = setup.refreshError { note(error, warning: true) }
            footer
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 14).fill(Color.white.opacity(0.04)))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.chipStroke, lineWidth: 1))
        .padding(.top, 10)
    }

    private var footer: some View {
        HStack(spacing: 8) {
            chip("More options  ⤢") {
                NotificationCenter.default.post(name: .wispOpenSetupGuide, object: nil)
            }
            Spacer(minLength: 8)
            if let busy = setup.busy {
                ProgressView().controlSize(.small)
                Text(busy).font(.system(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1)
            }
            chip("Check again") { Task { await setup.refresh(); await setup.refreshDownloads() } }
                .disabled(setup.busy != nil)
            if ready {
                chip("Done", primary: true) { model.setupCard = .hidden }
            } else {
                chip("Later") { model.setupCard = .later }
            }
        }
    }

    // MARK: Steps

    private func engineStep(_ s: SetupStatus) -> some View {
        let check = s.checks.first { $0.id == "engine" }
        return step("1", "Start up", state: check?.state ?? "todo") {
            if let bundled = s.bundledEngine, !bundled.available, let reason = bundled.unavailableReason {
                detail(reason)
            }
            if let check {
                detail(check.detail)
                if check.action == "unsupported_macos" {
                    chip("Other options  ⤢") {
                        NotificationCenter.default.post(name: .wispOpenSetupGuide, object: nil)
                    }
                } else if check.action == "install_omlx" {
                    chip("Get oMLX") { setup.open(check.url) }
                } else if check.action == "start_engine" {
                    // The person's own oMLX is an app they installed, so it keeps its name.
                    chip(s.omlx.bundled == true ? "Start" : "Start oMLX") {
                        Task { await setup.startEngine() }
                    }
                    .disabled(setup.busy != nil)
                }
            }
        }
    }

    private func modelStep(_ s: SetupStatus) -> some View {
        let check = s.checks.first { $0.id == "model" }
        let hardware = "\(s.hardware.chip) · \(s.hardware.ramGb > 0 ? "\(s.hardware.ramGb) GB" : "memory unknown")"
        return step("2", "Model", state: check?.state ?? "todo") {
            detail("Wisp uses it for everything it answers. \(hardware)")
            let offer = s.models.installed.isEmpty ? NotchModelOffer.from(status: s, downloads: setup.downloads) : nil
            // The no-fit check repeats the memory warning, and the download row says
            // what "no model yet" would: say each thing once.
            if let check, check.state != "ok", check.detail != s.models.warning {
                if case .download? = offer {} else { note(check.detail, warning: true) }
            }
            if let warning = s.models.warning { note(warning, warning: true) }
            if let offer {
                switch offer {
                case .startEngineFirst:
                    detail("Your models show here once step 1 is done.")
                case .download(let brain):
                    NotchDownloadRow(setup: setup, brain: brain)
                    if let slept = sleepLine(brain) { note(slept, warning: false) }
                case .elsewhere(let title):
                    // The model check above already says no model is installed.
                    chip(title) { NotificationCenter.default.post(name: .wispOpenSetupGuide, object: nil) }
                case .noFit:
                    // The warning above says why; the window can connect another app.
                    chip("Connect another app  ⤢") {
                        NotificationCenter.default.post(name: .wispOpenSetupGuide, object: nil)
                    }
                case .none:
                    detail("No model is installed yet.")
                }
            } else {
                VStack(spacing: 4) {
                    ForEach(s.models.installed.prefix(3)) { entry in modelRow(entry) }
                }
                if s.models.installed.count > 3 {
                    detail("\(s.models.installed.count - 3) more in Settings › Models › Setup.")
                }
            }
        }
    }

    private func testStep(_ s: SetupStatus) -> some View {
        let selected = setup.selectedModel
        let label = setup.testing ? "Testing…"
            : selected.map { setup.inUse($0) } == true ? "Send a test" : "Use & test"
        return step("3", "Test", state: setup.testPassed ? "ok" : "info") {
            HStack(spacing: 8) {
                chip(label, primary: !setup.testPassed) { Task { await setup.useAndTest() } }
                    .disabled(selected == nil || setup.busy != nil || setup.testing
                              || !setup.statusIsCurrent || !s.omlx.running
                              || (selected.map { setup.inUse($0) } == true && !s.ready))
                Text(setup.testResult ?? "Wisp asks the model one question to make sure it works.")
                    .font(.system(size: 11.5)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func modelRow(_ entry: SetupStatus.ModelEntry) -> some View {
        let selected = setup.selectedModel == entry.id
        return Button { setup.selectedModel = entry.id } label: {
            HStack(spacing: 8) {
                Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                    .font(.system(size: 11))
                    .foregroundStyle(selected ? Theme.textPrimary : Theme.textMuted)
                Text(entry.label).font(.system(size: 12, weight: .medium)).foregroundStyle(Theme.textPrimary)
                    .lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 6)
                if setup.inUse(entry.id) { pill("In use", on: true) }
                fitPill(entry.fit)
                if !entry.tested && entry.fit != "unknown" { pill("Not validated", on: false) }
            }
            .padding(.horizontal, 8).padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 8).fill(selected ? Color.white.opacity(0.10) : Theme.chipFill))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    // MARK: Pieces

    private func step<Content: View>(_ number: String, _ title: String, state: String,
                                     @ViewBuilder content: () -> Content) -> some View {
        HStack(alignment: .top, spacing: 10) {
            glyph(state, number)
            VStack(alignment: .leading, spacing: 5) {
                Text(title).font(.system(size: 12.5, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                content()
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder private func glyph(_ state: String, _ number: String) -> some View {
        switch state {
        case "ok": Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.textPrimary)
        case "warn": Image(systemName: "exclamationmark.circle").foregroundStyle(Color.orange)
        case "todo": Image(systemName: "\(number).circle").foregroundStyle(Theme.textPrimary)
        default: Image(systemName: "\(number).circle").foregroundStyle(Theme.textMuted)
        }
    }

    @ViewBuilder private func fitPill(_ fit: String) -> some View {
        switch fit {
        case "recommended": pill("Recommended", on: true)
        case "heavy": pill("Heavy for this Mac", on: false)
        case "unknown": pill("Untested", on: false)
        default: EmptyView()
        }
    }

    private func detail(_ text: String) -> some View {
        Text(text).font(.system(size: 11.5)).foregroundStyle(Theme.textSecondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func note(_ text: String, warning: Bool) -> some View {
        Text(text).font(.system(size: 11.5)).foregroundStyle(warning ? Color.orange : Theme.textSecondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func pill(_ text: String, on: Bool) -> some View {
        Text(text).font(.system(size: 9.5, weight: .semibold))
            .foregroundStyle(on ? Color.black : Theme.textSecondary)
            .padding(.horizontal, 6).padding(.vertical, 1.5)
            .background(Capsule().fill(on ? Color.white : Theme.chipFill))
            .overlay(Capsule().stroke(on ? Color.clear : Theme.chipStroke, lineWidth: 1))
            .fixedSize()
    }

    private func chip(_ title: String, primary: Bool = false, action: @escaping () -> Void) -> some View {
        NotchChip(title, primary: primary, action: action)
    }
}

/// What the card says about something that happened while nobody was looking:
/// the Mac slept mid-download. AppDelegate sets it on wake; the card reads it.
final class NotchSetupNotices: ObservableObject {
    static let shared = NotchSetupNotices()
    @Published var wake: SleepPause.Wake?

    /// The line a wake mid-download leaves, for the notch card and the setup
    /// window's step 1 alike.
    func sleepLine(_ brain: FirstRunBrain, downloads: [SetupGuideModel.DownloadTask]) -> String? {
        let transfer: SleepPause.Transfer
        switch brain {
        case .downloading(let model, _, _, _, _, _):
            transfer = .downloading(model: model,
                                    bytes: downloads.first { $0.model == model }?.downloadedBytes ?? 0)
        case .failed: transfer = .stopped
        default: transfer = .other
        }
        return SleepPause.line(wake, now: transfer, at: Date())
    }
}
