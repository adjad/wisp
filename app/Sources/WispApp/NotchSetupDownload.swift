import SwiftUI

// The model download, inside the notch's setup card. It starts, shows and
// resumes the same transfer the first-run window does, read through the same
// FirstRunBrain, so both say the same thing. SetupGuideModel keeps a "download"
// host while a transfer runs, so collapsing the panel doesn't stop the poll or
// the automatic switch to the new model. Nothing here assumes a notch: the card
// is the same on a Mac without one.

/// What the card's model step offers while no model is installed. Pure.
enum NotchModelOffer: Equatable {
    /// The engine isn't running yet; step 1 above has the button.
    case startEngineFirst
    /// Wisp's own engine fetches the model here, in one of these states.
    case download(FirstRunBrain)
    /// Only the setup window can do this (an external oMLX engine, or a model with
    /// no public download): a ⤢ chip that opens it, with this title.
    case elsewhere(String)
    /// Nothing Wisp can download fits this Mac: say why, and offer another route.
    case noFit(String)
    case none

    static func from(status s: SetupStatus, downloads: [SetupGuideModel.DownloadTask]) -> NotchModelOffer {
        let check = s.checks.first { $0.id == "model" }
        if check?.action == "no_local_fit" || (s.models.fits == false && s.models.download == nil) {
            return .noFit(s.models.warning ?? check?.detail ?? "No model Wisp can download fits this Mac.")
        }
        guard s.omlx.running else { return .startEngineFirst }
        if check?.action == "add_local_model" { return .elsewhere("Add a model  ⤢") }
        guard s.omlx.bundled == true else { return .elsewhere("Download a model  ⤢") }
        let brain = FirstRunBrain.from(status: s, downloads: downloads)
        switch brain {
        case .needsDownload where s.models.download == nil:
            // No public download to fetch: the window explains what to add.
            return .elsewhere("Add a model  ⤢")
        case .needsDownload, .downloading, .failed, .waitingForEngine:
            return .download(brain)
        case .startingEngine, .checking:
            return .startEngineFirst
        case .unavailable, .switching, .offered, .ready:
            return .none
        }
    }

    /// The line under the progress bar: what has arrived, how fast, how long is
    /// left, or that nothing has moved in a while. Pure.
    static func progressLine(doneGb: Double, totalGb: Double, speed: String?,
                             secondsLeft: Int?, stalled: Bool) -> String {
        if doneGb <= 0 && !stalled { return "Starting download…" }
        let amount = String(format: "%.1f of %.1f GB", doneGb, totalGb)
        if stalled { return amount + " · No progress — check your connection" }
        return ([amount, speed, FirstRunBrain.timeLeft(secondsLeft)] as [String?]).compactMap { $0 }
            .joined(separator: " · ")
    }

    /// Under a stopped download: the engine's own reason when it gave one, and
    /// what the button does, in the button's words. Pure.
    static func stoppedDetail(paused: Bool, engineMessage: String?) -> String {
        if paused { return "Resume picks up where it left off." }
        let reason = engineMessage?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return (reason.isEmpty ? "" : reason + " ") + "Try again picks up where it left off."
    }

    /// How long a finished download may wait for the engine to list it before
    /// the card offers Download again (as the first-run window does).
    static let listingWait: TimeInterval = 30
}

/// The download row in the card's model step.
struct NotchDownloadRow: View {
    @ObservedObject var setup: SetupGuideModel
    let brain: FirstRunBrain
    @State private var progressWatch = DownloadProgressWatch()
    /// When the finished download started waiting for the engine to list it.
    @State private var waitingSince: Date?

    var body: some View {
        content
            .padding(9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.chipStroke, lineWidth: 1))
            .onAppear { observe(brain) }
            .onChange(of: brain) { _, next in observe(next) }
            .onChange(of: brain.stage) { _, _ in announce(brain) }
    }

    private func observe(_ state: FirstRunBrain) {
        if case .downloading(_, _, _, let done, _, _) = state {
            progressWatch.observe(doneGb: done, at: Date())
        } else {
            progressWatch.reset()
        }
        if case .waitingForEngine = state {
            if waitingSince == nil { waitingSince = Date() }
        } else {
            waitingSince = nil
        }
    }

    @ViewBuilder private var content: some View {
        switch brain {
        case .needsDownload(let model, let label, let size):
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    VStack(alignment: .leading, spacing: 1) {
                        title(label + (size.map { String(format: " · %.1f GB", $0) } ?? ""))
                        line("Recommended for this Mac. Wisp downloads it once; it runs here.")
                    }
                    Spacer(minLength: 8)
                    fetch("Download", model: model)
                }
                diskWarning(size)
            }
        case .downloading(let model, let label, let fraction, let done, let total, let left):
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    title("Downloading " + label)
                    Spacer(minLength: 8)
                    NotchChip("Cancel") { Task { await setup.download(model, cancel: true) } }
                        .disabled(setup.busy != nil)
                        .accessibilityLabel("Cancel the model download")
                }
                ProgressView(value: fraction)
                    .tint(Color(red: 0.56, green: 0.56, blue: 0.96))
                    .accessibilityLabel("Model download")
                    .accessibilityValue("\(Int(fraction * 100)) percent")
                TimelineView(.periodic(from: .now, by: 5)) { context in
                    let stalled = progressWatch.stalled(at: context.date)
                    let speed = setup.downloads.first(where: { $0.model == model })?.speedText
                    Text(NotchModelOffer.progressLine(doneGb: done, totalGb: total, speed: speed,
                                                     secondsLeft: left, stalled: stalled))
                        .font(.system(size: 11)).foregroundStyle(stalled ? Color.orange : Theme.textMuted)
                        .lineLimit(1)
                }
                line("You can close this panel; the download keeps going.")
            }
        case .failed(let model, let label, _, let paused):
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    VStack(alignment: .leading, spacing: 1) {
                        title((paused ? "Download paused" : "Download didn't finish") + " · " + label)
                        Text(NotchModelOffer.stoppedDetail(paused: paused,
                                                           engineMessage: setup.downloads.first { $0.model == model }?.message))
                            .font(.system(size: 11)).foregroundStyle(paused ? Theme.textMuted : Color.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 8)
                    fetch(paused ? "Resume" : "Try again", model: model)
                }
                diskWarning(setup.status?.models.download?.sizeGb)
            }
        case .waitingForEngine(let model, let label):
            TimelineView(.periodic(from: .now, by: 5)) { context in
                let stuck = waitingSince.map { context.date.timeIntervalSince($0) >= NotchModelOffer.listingWait } ?? false
                HStack(spacing: 8) {
                    VStack(alignment: .leading, spacing: 1) {
                        title(label)
                        line(stuck ? "Downloaded, but Wisp can't find its files. Download it again to fix that."
                                   : "Downloaded. Getting it ready…")
                    }
                    Spacer(minLength: 8)
                    if stuck { fetch("Download again", model: model) } else { ProgressView().controlSize(.small) }
                }
            }
        default:
            EmptyView()
        }
    }

    /// Download, Resume or Try again. While the request to start is in flight
    /// (minutes, when the engine is starting too) it says so instead of going grey.
    @ViewBuilder private func fetch(_ title: String, model: String) -> some View {
        if setup.busy == "Starting download…" {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("Starting download…").font(.system(size: 11)).foregroundStyle(Theme.textMuted)
            }
        } else {
            // Primary, but no Return shortcut: the Ask box under this card owns Return.
            NotchChip(title, primary: true) { Task { await setup.download(model) } }
                .disabled(setup.busy != nil)
        }
    }

    @ViewBuilder private func diskWarning(_ sizeGb: Double?) -> some View {
        if let warning = FirstRunBrain.diskWarning(freeBytes: FirstRunSetupView.freeBytes(), sizeGb: sizeGb) {
            Text(warning).font(.system(size: 11)).foregroundStyle(Color.orange)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func title(_ text: String) -> some View {
        Text(text).font(.system(size: 12, weight: .medium)).foregroundStyle(Theme.textPrimary)
            .lineLimit(1).truncationMode(.middle)
    }

    private func line(_ text: String) -> some View {
        Text(text).font(.system(size: 11)).foregroundStyle(Theme.textMuted)
            .fixedSize(horizontal: false, vertical: true)
    }

    /// VoiceOver hears the download stop or finish without hunting for it.
    private func announce(_ state: FirstRunBrain) {
        let text: String
        switch state {
        case .failed(_, _, _, let paused): text = paused ? "Download paused" : "Download didn't finish"
        case .waitingForEngine: text = "Model downloaded"
        default: return
        }
        NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested,
                             userInfo: [.announcement: text, .priority: NSAccessibilityPriorityLevel.high.rawValue])
    }
}

/// The card's capsule button.
struct NotchChip: View {
    let title: String
    var primary = false
    let action: () -> Void

    init(_ title: String, primary: Bool = false, action: @escaping () -> Void) {
        self.title = title
        self.primary = primary
        self.action = action
    }

    var body: some View {
        Button(action: action) {
            Text(title).font(.system(size: 11.5, weight: primary ? .semibold : .medium))
                .foregroundStyle(primary ? Color.black : Theme.textSecondary)
                .padding(.horizontal, 9).padding(.vertical, 4)
                .background(Capsule().fill(primary ? Color.white : Theme.chipFill))
                .overlay(Capsule().stroke(primary ? Color.clear : Theme.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .fixedSize()
    }
}
