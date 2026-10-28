import AppKit
import ApplicationServices
import Carbon
import Contacts
import CoreGraphics
import EventKit
import ServiceManagement
import SwiftUI
import UserNotifications

// The one window a new user sees on Wisp's first launch, with every setup step
// in it, in order (OnboardingStep): get the model, allow permissions one click
// at a time, connect Google, Getting to know you, and how to use Wisp. The
// model download starts first and keeps going in the background, because it is
// the longest wait; the other steps happen while it runs. Engine, download and
// "use this model" all go through the shared SetupGuideModel, so the notch's
// setup card and the detailed setup window see the same state.

extension Notification.Name {
    /// Opens the first-run window again (at the step the person left) while
    /// setup is unfinished: the notch's Continue setup posts it.
    static let wispContinueFirstRun = Notification.Name("WispContinueFirstRun")
}

/// Whether to show it, and what it takes over from the old first launch.
enum FirstRunSetup {
    static let doneKey = "wisp.firstRun.done"
    /// Set when the window first opens. Quitting before it is closed leaves this
    /// without doneKey, and the next launch opens the window again.
    static let startedKey = "wisp.firstRun.started"
    static let setupGuideShownKey = "wisp.setupGuide.shown"
    static let notchTipShownKey = "wisp.notchTip.shown"
    /// The step on screen, so a quit (or macOS reopening Wisp after Full Disk
    /// Access) comes back to it.
    static let stepKey = "wisp.firstRun.step"
    /// Launches that opened the window again by themselves after the person
    /// closed it early (FirstRunSetup.launchCountsTowardCap).
    static let reopensKey = "wisp.firstRun.reopens"
    /// Wisp ended while the window was up (a quit, a logout, macOS's Quit & Reopen
    /// after Full Disk Access), or System Settings was opened for Full Disk Access.
    /// The next launch opens the window again at its step whatever the cap says,
    /// without counting it, and clears this.
    static let resumeKey = "wisp.firstRun.resumeAtLaunch"
    /// Set when the window's About you step was finished (Done) or explicitly
    /// skipped. Only then does the notch stop offering Getting to know you: being
    /// shown the step, or closing the window before it, doesn't count.
    static let aboutYouDecidedKey = "wisp.firstRun.aboutYouDecided"
    /// After this many, an unfinished setup stops opening at launch and is carried
    /// by the notch's quiet setup cue instead; Continue setup there still opens it.
    static let maxAutomaticReopens = 3

    /// On a Mac that has never run Wisp, and again while setup is unfinished:
    /// always after Wisp ended with the window up (`resumeKey`: a quit, a restart,
    /// macOS's Quit & Reopen after Full Disk Access), and after the person closed
    /// it early up to `maxAutomaticReopens` times. Anyone who finished it, or saw
    /// the old setup card or the notch tip without ever starting this window, is
    /// an existing user and never gets it.
    static func shouldShow(_ flag: (String) -> Bool, reopens: Int = 0) -> Bool {
        guard !flag(doneKey) else { return false }
        if flag(startedKey) { return flag(resumeKey) || reopens < maxAutomaticReopens }
        return !flag(setupGuideShownKey) && !flag(notchTipShownKey)
    }

    /// A launch that opens the window by itself uses up one of the reopens only
    /// when the person had closed it early. Wisp ending under it (`resumeKey`)
    /// is not the person putting setup aside, and a fresh Mac isn't a reopen.
    static func launchCountsTowardCap(_ flag: (String) -> Bool) -> Bool {
        flag(startedKey) && !flag(resumeKey)
    }

    /// How the window went away.
    enum Ending: Equatable {
        /// Done — open Wisp, or one of the last step's prompts.
        case finished
        /// The red button or ⌘W, before finishing.
        case closedEarly
        /// Wisp itself is ending (a quit, a logout, Quit & Reopen): the window
        /// goes with it, or never sees a close at all.
        case appQuit
    }

    /// The explicit finish wins over a quit that happens at the same moment; a
    /// close while Wisp is ending is the quit, never the person.
    static func ending(finishing: Bool, terminating: Bool) -> Ending {
        finishing ? .finished : (terminating ? .appQuit : .closedEarly)
    }

    /// Only the explicit finish ends setup. A ready model no longer does: the
    /// window has steps after the model, and a quit or an early close with Wisp
    /// already able to answer used to throw them away for good.
    static func marksDone(_ ending: Ending) -> Bool { ending == .finished }

    /// Whether the next launch reopens the window at its step without counting
    /// toward the cap (and past it): only when Wisp ended with the window up.
    static func resumesAtNextLaunch(_ ending: Ending) -> Bool { ending == .appQuit }

    /// How long a relaunched Wisp waits for an old copy that is quitting: longer
    /// than that copy's own 20 s bound on stopping its engine.
    static let peerQuitWait: TimeInterval = 25

    /// A second Wisp normally hands over to the running one. It waits for that
    /// one to quit instead (then launches) when the running one said it is
    /// shutting down, or setup expects a relaunch (macOS's Quit & Reopen after
    /// Full Disk Access): handing over to a dying copy leaves nothing running.
    static func waitsForPeerToQuit(resumePending: Bool, peerShuttingDown: Bool) -> Bool {
        resumePending || peerShuttingDown
    }

    /// Before System Settings opens for Full Disk Access during setup: switching
    /// Wisp on there makes macOS offer Quit & Reopen, and that quit (or a kill
    /// that never reaches applicationShouldTerminate) must bring setup back here.
    static func expectRelaunch(_ flag: (String) -> Bool, _ set: (String) -> Void) {
        if isUnfinished(flag) { set(resumeKey) }
    }

    /// The window's size: 600 pt tall where it fits, else what the screen has
    /// under the menu bar and above the Dock (less a margin), never under the
    /// minimum. The steps scroll; the footer stays pinned.
    static let width: CGFloat = 640
    static let minimumHeight: CGFloat = 420
    static func windowHeight(room: CGFloat) -> CGFloat { max(minimumHeight, min(600, room - 60)) }

    /// Started and not finished: Continue setup opens this window again.
    static func isUnfinished(_ flag: (String) -> Bool) -> Bool { flag(startedKey) && !flag(doneKey) }

    /// About you was answered or skipped in the window, so the notch never offers
    /// Getting to know you on its own.
    static func aboutYouDecidedInWindow(_ flag: (String) -> Bool) -> Bool { flag(aboutYouDecidedKey) }

    /// Whether the window may act on the model by itself (switch Wisp over to a
    /// downloaded model, run its one test). Only during first-run setup: opened
    /// later from Permissions… or a Brief link, it never touches the model of
    /// someone already set up.
    static func drivesModel(setupDone: Bool) -> Bool { !setupDone }

    /// Wisp can answer: a local model is ready, or every role runs on a remote
    /// provider (/setup/status `remote_only`). SetupStatus holds the rule.
    static func canAnswer(ready: Bool, remoteOnly: Bool) -> Bool {
        SetupStatus.canAnswer(ready: ready, remoteOnly: remoteOnly)
    }

    /// The notch's setup cue after one /setup/status answer, on every launch: while
    /// Wisp can't answer and no first-run window is up, the quiet cue (the `!` and
    /// a one-line row on Ask), never the full card; it goes once Wisp can answer.
    /// An open card is left to the person (it waits for Done or Later).
    static func setupCue(_ current: NotchSetupCard, canAnswer: Bool, firstRunWindowUp: Bool) -> NotchSetupCard {
        if canAnswer { return current.afterReadiness(true) }
        return !firstRunWindowUp && current == .hidden ? .later : current
    }

    /// Whether the menu bar icon's menu shows Finish Setup…: while setup is under
    /// way (the first-run window is up or was left unfinished, the notch carries a
    /// setup card or cue, or the service says Wisp can't answer yet). Once set up,
    /// the model and engine are changed in Settings › Models instead. `canAnswer`
    /// is nil until the service has answered once, which shows nothing by itself.
    static func menuOffersSetup(canAnswer: Bool?, card: NotchSetupCard, firstRunWindowUp: Bool,
                                unfinished: Bool) -> Bool {
        firstRunWindowUp || unfinished || card != .hidden || canAnswer == false
    }

    /// No Wisp notification while the setup window is on screen, even with
    /// System Settings or the browser in front of it: "Wisp is ready" is said
    /// only once the window was closed, finished or minimized.
    static func notifiesReady(promised: Bool, windowUp: Bool, windowMinimized: Bool) -> Bool {
        promised && (!windowUp || windowMinimized)
    }

    /// The old first launch showed a tip before Wisp could answer and opened the
    /// setup card. This window does both, in order, so mark them as handled.
    static func takeOverLaunchPrompts(_ set: (String) -> Void) {
        for key in [startedKey, setupGuideShownKey, notchTipShownKey] { set(key) }
    }
}

/// When Wisp offers Getting to know you on its own. The service decides whether
/// it is due (GET /profile status.offer: never finished, no recent Later, at most
/// twice); this decides whether now is a moment to show it. Someone set up by
/// the onboarding window answered (or skipped) it there, so the notch holds it
/// back, apart from one later offer a few days after a skip (KnowYouSecondOffer,
/// still only while the service says it's due); /about and Memory › About you remain.
enum KnowYouOffer {
    struct Moment: Equatable {
        var firstRunWindowUp = false
        var setupWindowUp = false
        var setupCardOpen = false
        var replying = false
        var flowOpen = false
        /// Open, or held open by typed text or a pin, or showing Search.
        var panelInUse = false
        var dismissedToMenuBar = false
        /// The person chose Tell Wisp about you this session: never offer it again.
        var chosenThisSession = false
        /// The service said About you is complete: no more checks this session.
        var profileComplete = false
        /// Answered or skipped as the onboarding window's About you step.
        var aboutYouDecidedInWindow = false
    }

    /// Nothing else on screen and nothing the person is doing. Checked before
    /// asking the service, and again with its answer.
    static func quiet(_ m: Moment) -> Bool {
        !(m.firstRunWindowUp || m.setupWindowUp || m.setupCardOpen || m.replying || m.flowOpen
          || m.panelInUse || m.dismissedToMenuBar || m.chosenThisSession || m.profileComplete
          || m.aboutYouDecidedInWindow)
    }

    static func shouldOffer(_ m: Moment, serviceOffers: Bool) -> Bool { serviceOffers && quiet(m) }
}

/// Step 1's state, from the setup status and the engine's downloads. Pure, so it
/// can be checked without a service.
enum FirstRunBrain: Equatable {
    case checking
    case unavailable(String)
    case startingEngine
    case needsDownload(model: String, label: String, sizeGb: Double?)
    case downloading(model: String, label: String, fraction: Double, doneGb: Double, totalGb: Double, secondsLeft: Int?)
    /// `paused`: cancelled by the person, not failed.
    case failed(model: String, label: String, message: String, paused: Bool)
    /// The download finished but the engine doesn't list the model yet: wait,
    /// never apply. Lasting long means its files are missing (Download again).
    case waitingForEngine(model: String, label: String)
    /// Installed and not in use yet: switch Wisp over to it.
    case switching(model: String, label: String)
    /// The person's own oMLX serves a model, but switching to it would replace their
    /// choice (or it isn't the recommended one): one click, never automatic.
    case offered(model: String, label: String)
    case ready(label: String)

    var isReady: Bool { if case .ready = self { return true } else { return false } }

    /// The model a "Switch"/"Use" click applies.
    var modelToUse: String? {
        switch self {
        case .switching(let model, _), .offered(let model, _): return model
        default: return nil
        }
    }

    /// Step 1 is waiting on the person's one click: Download, or Retry after a stop.
    var waitsForDownloadClick: Bool {
        switch self {
        case .needsDownload, .failed: return true
        default: return false
        }
    }

    /// The model a Download or Retry click fetches.
    var modelToFetch: String? {
        switch self {
        case .needsDownload(let model, _, _), .failed(let model, _, _, _), .waitingForEngine(let model, _): return model
        default: return nil
        }
    }

    /// The state without its numbers: a download's progress is not a new stage.
    var stage: Int {
        switch self {
        case .checking: return 0
        case .unavailable: return 1
        case .startingEngine: return 2
        case .needsDownload: return 3
        case .downloading: return 4
        case .failed: return 5
        case .waitingForEngine: return 6
        case .switching: return 7
        case .ready: return 8
        case .offered: return 9
        }
    }

    /// The model is on its way with nothing more to click, so the person can move on.
    var isArriving: Bool {
        switch self {
        case .downloading, .waitingForEngine, .switching: return true
        default: return false
        }
    }

    /// What the last step's footer says about the model, and the one thing it
    /// offers when the model isn't on its way. A spinner only while something is
    /// really happening, so the step never waits on nothing.
    enum Footer: Equatable {
        case ready
        case working(String)
        case download
        case retry
        case tryAgain
        case detailedSetup
        /// Wisp's service hasn't answered for longer than a slow first launch takes.
        case checkAgain

        /// A click here starts a download, so the disk-space warning belongs beside it.
        var fetches: Bool { self == .download || self == .retry }
    }

    /// `stalled`: the step Wisp takes by itself here stopped and needs a click (the
    /// service or the engine didn't start, switching to the model failed, or a
    /// finished download never showed up in the engine).
    func footer(stalled: Bool) -> Footer {
        switch self {
        case .ready: return .ready
        case .checking: return stalled ? .checkAgain : .working("Starting Wisp…")
        case .startingEngine: return stalled ? .tryAgain : .working("Getting Wisp ready…")
        case .downloading(_, _, let fraction, _, _, _): return .working("Downloading · \(Int(fraction * 100))%")
        case .switching: return stalled ? .tryAgain : .working("Finishing setup…")
        case .waitingForEngine: return stalled ? .retry : .working("Finishing setup…")
        case .needsDownload: return .download
        case .failed: return .retry
        case .unavailable: return .detailedSetup
        case .offered: return .tryAgain
        }
    }

    /// Return belongs to the card's button while the card waits on a click
    /// (Download, Retry, Use, or a stuck Check again / Try again / Download again),
    /// and to the footer's Continue otherwise: exactly one default action per state.
    func cardTakesReturn(stalled: Bool) -> Bool {
        switch self {
        case .needsDownload, .failed, .offered: return true
        case .checking, .startingEngine, .switching, .waitingForEngine: return stalled
        default: return false
        }
    }

    /// Step 1's heading line. A setup that already answers with a remote model has
    /// nothing to download, so it isn't told about one (or that the model is local).
    static func welcomeSubtitle(remoteOnly: Bool) -> String {
        remoteOnly
            ? "Wisp already answers with the model you set up, so there's nothing to download here."
            : "The AI model runs on this Mac. Your mail, calendar and messages stay here; when you ask "
                + "for something from the web, only the search terms are sent. First Wisp needs to "
                + "download that model once."
    }

    /// `stillLooking`: the person's own oMLX is still inside its search window
    /// (`connectGrace`). Its server often answers before it has listed any model, so a
    /// running oMLX with no Wisp model yet is still being looked for, not stuck. Off by
    /// default, so the notch's setup card keeps its own reading.
    static func from(status: SetupStatus?, downloads: [SetupGuideModel.DownloadTask],
                     stillLooking: Bool = false) -> FirstRunBrain {
        guard let status else { return .checking }
        if status.ready {
            let model = status.roles.first(where: { $0.endpoint == "local" })?.model ?? status.models.recommended
            let label = status.models.installed.first(where: { $0.id == model })?.label ?? model
            return .ready(label: plainLabel(label))
        }
        // Every role on a remote provider: Wisp already answers, so nothing to download.
        if status.canAnswer {
            return .ready(label: plainLabel(status.roles.first?.model ?? "Your model"))
        }
        // The oMLX app this Mac already has (a fresh install picks it when oMLX.app is
        // installed). Until its server answers it is connecting, never a failure: it may
        // be starting or loading the model. Only a running oMLX without the model is stuck.
        switch status.existingOmlx {
        case .connecting: return .startingEngine
        case .serving(let model, let label): return .switching(model: model, label: plainLabel(label))
        case .offering(let model, let label): return .offered(model: model, label: plainLabel(label))
        case nil: break
        }
        if stillLooking, status.usesOwnOmlx { return .startingEngine }
        // The engine's own reason first: a Mac whose macOS is too old for it also
        // falls back to external mode, and "set to use another app" would be untrue.
        if let engine = status.bundledEngine, !engine.available {
            return .unavailable(engine.unavailableReason ?? "Wisp can't run its AI model on this Mac.")
        }
        guard status.omlx.bundled == true else {
            if status.omlx.installed, status.omlx.running {
                let wanted = plainLabel(status.models.recommendedLabel ?? status.models.recommended)
                return .unavailable("Your oMLX app is running but doesn't have \(wanted) or another of Wisp's models. "
                                    + "Use the detailed setup to pick one of its models.")
            }
            return .unavailable("Wisp is set to use another app for its model. Use the detailed setup to finish.")
        }
        // No model Wisp can download fits this Mac's memory (or it couldn't be
        // read): the engine would refuse to load one, so offer no Download.
        if status.models.fits == false, status.models.installed.isEmpty, status.models.download == nil {
            return .unavailable(status.models.warning ?? "No model Wisp can download fits this Mac.")
        }
        if !status.omlx.running { return .startingEngine }
        let model = status.models.download?.model ?? status.models.recommended
        let label = plainLabel(status.models.download?.label ?? status.models.recommendedLabel ?? model)
        if status.models.installed.contains(where: { $0.id == model }) {
            return .switching(model: model, label: label)
        }
        if let task = downloads.first(where: { $0.model == model }) {
            switch task.state {
            case "pending", "downloading":
                let total = task.totalBytes > 0 ? task.totalBytes : (status.models.download?.sizeGb ?? 0) * 1e9
                let fraction = total > 0 ? min(1, task.downloadedBytes / total) : task.progress / 100
                let left = task.bytesPerSecond > 0 && total > task.downloadedBytes
                    ? Int((total - task.downloadedBytes) / task.bytesPerSecond) : nil
                return .downloading(model: model, label: label, fraction: fraction,
                                    doneGb: task.downloadedBytes / 1e9, totalGb: total / 1e9, secondsLeft: left)
            case "completed":
                return .waitingForEngine(model: model, label: label)
            default:
                // The words name the card's own button: Resume after a Stop, Retry after a failure.
                let paused = task.state == "cancelled"
                return .failed(model: model, label: label,
                               message: paused ? stoppedMessage : (task.message ?? "Choose Retry to try again."),
                               paused: paused)
            }
        }
        return .needsDownload(model: model, label: label, sizeGb: status.models.download?.sizeGb)
    }

    /// A download the person stopped. Stop cancels the transfer; Resume starts it again.
    static let stoppedMessage = "You stopped the download. Choose Resume to start it again."

    /// The model's name without its build details: "Gemma 4 E4B", not
    /// "Gemma 4 E4B · 4-bit (oQ4e) + MTP". The detailed setup keeps the full label.
    static func plainLabel(_ label: String) -> String {
        label.components(separatedBy: " · ").first.map { $0.trimmingCharacters(in: .whitespaces) } ?? label
    }

    /// Space a download needs while it runs: the model, plus room for the engine
    /// to unpack it. About 8 GB for Gemma 4 E4B.
    static func spaceNeeded(sizeGb: Double?) -> Double { max(8, (sizeGb ?? 5.5) * 1.45) }

    /// Said before Download when the Mac is short of space, instead of failing
    /// part-way through with a generic message.
    static func diskWarning(freeBytes: Int64?, sizeGb: Double?) -> String? {
        guard let freeBytes else { return nil }
        let free = Double(freeBytes) / 1e9, needed = spaceNeeded(sizeGb: sizeGb)
        guard free < needed else { return nil }
        return String(format: "This Mac has %.1f GB free; the download needs about %.0f GB. Free up some space first.",
                      free, needed)
    }

    /// How long Wisp looks for the person's own oMLX before saying anything is wrong:
    /// a start from cold, or one that lists its models late, can take a while. Past it
    /// the step offers Try again even when Wisp never got to start oMLX by itself, so
    /// the step never spins with no button.
    static let connectGrace: TimeInterval = 30

    /// Whether the search for the person's own oMLX is still inside `connectGrace`.
    /// `waited`: nil before the search has begun, which counts as just started.
    static func stillLooking(waited: TimeInterval?) -> Bool {
        waited.map { $0 < connectGrace } ?? true
    }

    /// `.startingEngine` needs a click. The built-in engine: Wisp's own start finished
    /// without an engine, or a start failed. The person's own oMLX (`ownOmlx`): only
    /// once the search window has passed, so an automatic start that returned early
    /// or failed doesn't show Try again before oMLX has had its time to answer.
    static func startStalled(autoStarted: Bool, failed: Bool, ownOmlx: Bool, waited: TimeInterval) -> Bool {
        ownOmlx ? waited >= connectGrace : (autoStarted || failed)
    }

    /// The step-1 card's title while the engine isn't up. The person's own oMLX is
    /// being looked for, not failing, however far into the search it is.
    static func startingTitle(ownOmlx: Bool) -> String {
        ownOmlx ? "Looking for your oMLX…" : "Getting Wisp ready…"
    }

    /// What step 1 does by itself while it looks for the person's own oMLX (`connecting`:
    /// setup is on their oMLX). The wait is always timed, so Try again appears after `connectGrace` even when
    /// setup was finished and the window opened again (Permissions…). Only an
    /// unfinished first run starts their oMLX by itself (FirstRunSetup.drivesModel);
    /// after that, only a click on Try again starts it.
    static func whileConnecting(setupDone: Bool, connecting: Bool) -> (startsOmlx: Bool, timesGrace: Bool) {
        (connecting && FirstRunSetup.drivesModel(setupDone: setupDone), connecting)
    }

    /// The connecting card's line. Once Try again shows after setup was finished,
    /// nothing is starting their oMLX, so it says plainly that it isn't running.
    static func connectingDetail(setupDone: Bool, stalled: Bool) -> String {
        setupDone && stalled
            ? "Your oMLX isn't running. Choose Try again to start it, or open the oMLX app."
            : "Wisp uses the oMLX app you already have, so there's nothing to download."
    }

    /// How long the window waits for Wisp's service to answer before it says it is
    /// slow and offers Check again: the 3 minutes the notch's own wait allows a cold
    /// first launch (AppDelegate's reachability watch).
    static let serviceStartLimit: TimeInterval = 180

    /// The service has had longer than a slow first launch takes.
    static func serviceSlow(waited: TimeInterval) -> Bool { waited >= serviceStartLimit }

    /// How long a cold start has taken, once it is long enough to be worth saying.
    static func waited(_ seconds: Int) -> String? {
        guard seconds >= 20 else { return nil }
        return seconds < 120 ? "\(seconds) s so far" : "\(seconds / 60) min so far"
    }

    /// A transfer that hasn't moved in this long gets "No progress".
    static let noProgressAfter: TimeInterval = 30

    /// "about 6 minutes left", rounded the way a person would say it.
    static func timeLeft(_ seconds: Int?) -> String? {
        guard let seconds, seconds > 0 else { return nil }
        if seconds < 60 { return "less than a minute left" }
        let minutes = Int((Double(seconds) / 60).rounded())
        if minutes < 60 { return minutes == 1 ? "about a minute left" : "about \(minutes) minutes left" }
        let hours = Double(minutes) / 60
        return String(format: "about %.1f hours left", hours)
    }
}

/// Whether a download's bytes are still moving. Pure, so it can be checked.
struct DownloadProgressWatch: Equatable {
    private(set) var bytes: Double = -1
    private(set) var movedAt: Date?

    mutating func observe(doneGb: Double, at date: Date) {
        if doneGb != bytes { bytes = doneGb; movedAt = date }
    }

    mutating func reset() { self = DownloadProgressWatch() }

    func stalled(at date: Date) -> Bool {
        guard let movedAt else { return false }
        return date.timeIntervalSince(movedAt) >= FirstRunBrain.noProgressAfter
    }
}

// MARK: - Permissions

@MainActor
final class FirstRunPermissions: ObservableObject {
    /// `mail` is Automation for Mail and Notes, which only helps while those apps
    /// are open; `messages` is Automation for Messages, which sends the texts the
    /// person approves in Ask (OutboundSender.sendMessage), asked here so the
    /// first send doesn't stop on a macOS prompt; `fullDiskAccess` is what reads
    /// Messages, and Mail and Notes when they are closed. `screenRecording` and
    /// `accessibility` are asked through Permissions' own click-time requests, so
    /// only the microphone and per-app Automation wait until first use.
    enum Item: String, CaseIterable, Identifiable {
        case calendar, reminders, mail, messages, fullDiskAccess, contacts, notifications, screenRecording, accessibility
        var id: String { rawValue }
    }
    enum State: Equatable { case checking, notAsked, allowed, notAllowed, notOnThisMac }

    @Published private(set) var states: [Item: State] = [:]
    /// Rows with a question up right now. Per row, not one lock: the Mail & Notes check
    /// waits until the person answers, and its prompt can sit behind Mail (opened hidden
    /// to ask), so a single lock left every other Allow button, Reminders included,
    /// doing nothing until then.
    @Published private(set) var asking: Set<Item> = []
    /// Mail & Notes or Messages whose question macOS still has up after
    /// `automationPatience`: the row is let go, says where the question is, and can
    /// ask again, while the first ask finishes whenever the person answers.
    @Published private(set) var waiting: Set<Item> = []
    /// Which click owns a row's "Asking…", so an ask that was let go can't clear
    /// the next one's when it finally returns.
    private var askTokens: [Item: UUID] = [:]
    /// Calendar or Reminders asked, and macOS recorded no answer (see eventKitUnanswered).
    @Published private(set) var unanswered: [Item: Bool] = [:]
    /// Mail & Notes or Messages whose grant macOS has dropped since it was last
    /// Allowed (an update that changed how Wisp is signed does this once), so the
    /// row can say why it asks again instead of looking random.
    @Published private(set) var resetItems: Set<Item> = []
    /// Read with the rest, so the Contacts row and the footer's count agree on it.
    @Published private(set) var messagesReadable = false
    private var activeObserver: NSObjectProtocol?
    private var poll: Task<Void, Never>?

    func state(_ item: Item) -> State { states[item] ?? .checking }
    /// The step's footer: every row is optional, so no "N of M" score, only how
    /// many could still be switched on (a Mac without Messages data is not one).
    /// A Contacts row waiting on Full Disk Access has no button of its own, so
    /// Full Disk Access carries it and it isn't counted twice.
    static func summary(_ states: [Item: State], messagesReadable: Bool = true) -> String {
        let waiting = contactsWaits(states, messagesReadable: messagesReadable)
        let off = Item.allCases.filter {
            !(waiting && $0 == .contacts) && (states[$0] == .notAsked || states[$0] == .notAllowed)
        }.count
        return off == 0 ? "All optional · nothing left off" : "All optional · \(off) still off"
    }

    /// Every one has an answer from macOS, whichever it was. A Contacts row waiting
    /// on Full Disk Access follows that answer instead of needing its own.
    static func allAnswered(_ states: [Item: State], messagesReadable: Bool) -> Bool {
        let waiting = contactsWaits(states, messagesReadable: messagesReadable)
        return Item.allCases.allSatisfy { (waiting && $0 == .contacts) || isDecided(states[$0] ?? .checking) }
    }

    var allAnswered: Bool { Self.allAnswered(states, messagesReadable: messagesReadable) }

    /// An answer the row can't change by asking again.
    static func isDecided(_ state: State) -> Bool {
        state == .allowed || state == .notAllowed || state == .notOnThisMac
    }

    /// What a row says about itself, next to its button.
    static func label(_ item: Item, _ state: State) -> String {
        switch state {
        case .checking: return ""
        case .notAsked: return "Not set"
        case .allowed: return "Allowed"
        case .notAllowed: return settingsThroughRequest(item) ? "Needs System Settings" : "Denied"
        case .notOnThisMac: return "Not needed"
        }
    }

    /// The Full Disk Access row's promise, before System Settings opens.
    static let fullDiskAccessRelaunchNote =
        "macOS may ask Wisp to quit and reopen. That's expected: setup picks up right here."

    /// Screen Recording only takes effect once Wisp quits and reopens.
    static let screenRecordingRelaunchNote =
        "It starts working once Wisp quits and reopens. That's expected: setup picks up right here."

    /// Rows whose turned-down state is fixed in System Settings, through the row's
    /// own request(): Full Disk Access can't be asked for, and Screen Recording and
    /// Accessibility show macOS's question only once (Permissions.surface).
    static func settingsThroughRequest(_ item: Item) -> Bool {
        item == .fullDiskAccess || item == .screenRecording || item == .accessibility
    }

    /// Screen Recording and Accessibility, from whether macOS trusts Wisp and whether
    /// Wisp already showed its one question (Permissions' prompted keys). macOS can't
    /// tell "not yet switched on" from "turned down", so once asked, a grant that
    /// isn't there needs System Settings. Pure.
    static func promptOnceState(granted: Bool, promptedBefore: Bool) -> State {
        granted ? .allowed : (promptedBefore ? .notAllowed : .notAsked)
    }

    /// The Screen Recording row's sentence: what it is for, and that it needs Wisp
    /// to quit and reopen before it works.
    static func screenRecordingDetail(_ state: State) -> String {
        let base = "So Wisp can look at your screen when you ask about it."
        switch state {
        case .notAsked: return base + " " + screenRecordingRelaunchNote
        case .notAllowed: return base + " In the page that opens, switch on Wisp. " + screenRecordingRelaunchNote
        default: return base
        }
    }

    /// The Accessibility row's sentence.
    static func accessibilityDetail(_ state: State) -> String {
        let base = "So Wisp can read the window you're asking about."
        return state == .notAllowed ? base + " In the page that opens, switch on Wisp." : base
    }

    /// Contacts puts names on Messages, so it is asked for only once Messages can
    /// be read (the rule ContactsReader kept at launch, now the row's).
    static func contactsUseful(messagesReadable: Bool) -> Bool { messagesReadable }

    /// The Contacts row shows "After Full Disk Access" (no button) only while Full
    /// Disk Access is still off and Messages can't be read. Once Full Disk Access is
    /// on, or this Mac has no Messages, Mail or Notes data for it to read ("Not
    /// needed"), there is nothing to wait for and Contacts can be allowed on its own.
    static func contactsWaits(_ states: [Item: State], messagesReadable: Bool) -> Bool {
        guard states[.contacts] == .notAsked, !contactsUseful(messagesReadable: messagesReadable) else { return false }
        let fullDiskAccess = states[.fullDiskAccess] ?? .checking
        return fullDiskAccess != .allowed && fullDiskAccess != .notOnThisMac
    }

    /// Re-read while the step is visible: grants made in System Settings arrive
    /// when the person comes back to Wisp.
    func start() {
        guard poll == nil else { return }
        activeObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { _ = Task { await self?.refresh() } }
        }
        // Ends with its owner too, not only on stop(): a view torn down without
        // onDisappear must not leave a 2 s loop running for the life of the app.
        poll = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    func stop() {
        poll?.cancel()
        poll = nil
        if let activeObserver { NotificationCenter.default.removeObserver(activeObserver) }
        activeObserver = nil
    }

    func refresh() async {
        var next: [Item: State] = [:]
        next[.calendar] = Self.eventKitState(.event)
        next[.reminders] = Self.eventKitState(.reminder)
        next[.contacts] = Self.contactsState()
        next[.fullDiskAccess] = Self.fullDiskAccessState()
        next[.notifications] = await Self.notificationsState()
        // Read only (never a prompt): CGPreflight and AXIsProcessTrusted just report.
        let defaults = UserDefaults.standard
        next[.screenRecording] = Self.promptOnceState(
            granted: CGPreflightScreenCaptureAccess(),
            promptedBefore: defaults.bool(forKey: Permissions.screenRecordingPromptedKey))
        next[.accessibility] = Self.promptOnceState(
            granted: AXIsProcessTrusted(),
            promptedBefore: defaults.bool(forKey: Permissions.accessibilityPromptedKey))
        // Remembered across closes and relaunches (see resolveAutomation): a closed
        // Mail or Notes can't be asked about, and that is not a removed grant.
        next[.mail] = await Self.automationState(ask: false)
        next[.messages] = await Self.messagesAutomationState(ask: false)
        resetItems = Set([Item.mail, .messages].filter { Self.storedAutomation($0) == .reset })
        messagesReadable = Self.messagesReadable()
        waiting = waiting.filter { !Self.isDecided(next[$0] ?? .checking) }
        states = next
    }

    func request(_ item: Item) async {
        guard !asking.contains(item) else { return }
        let token = UUID()
        asking.insert(item)
        askTokens[item] = token
        defer { if askTokens[item] == token { asking.remove(item); askTokens[item] = nil } }
        // A prompt for an app that isn't frontmost can open behind whatever the
        // person clicked into during the download (the RemindersWriter pattern).
        NSApp.activate(ignoringOtherApps: true)
        switch item {
        case .calendar, .reminders:
            // The prompt stays in this method (test_onboarding_window): only a click reaches it.
            let entity: EKEntityType = item == .calendar ? .event : .reminder
            let before = EKEventStore.authorizationStatus(for: entity)
            switch Self.eventKitClick(before) {
            case .alreadyAllowed:
                unanswered[item] = nil
            case .openSettings:
                unanswered[item] = nil
                Permissions.openSettingsPane(FirstRunSetupView.settingsPane(item))
            case .ask:
                let store = eventStore(entity)
                do {
                    _ = item == .calendar ? try await store.requestFullAccessToEvents()
                                          : try await store.requestFullAccessToReminders()
                } catch {
                    NSLog("Wisp: macOS did not answer the \(item.rawValue) request: \(error.localizedDescription)")
                }
                unanswered[item] = Self.eventKitUnanswered(before: before, after: EKEventStore.authorizationStatus(for: entity))
            }
        case .contacts:
            _ = try? await CNContactStore().requestAccess(for: .contacts)
        case .notifications:
            _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
        case .mail:
            // macOS can't ask about an app that isn't running, so Mail and Notes are
            // started in the background first, and closed again once asked.
            var launched: [NSRunningApplication] = []
            for id in Self.automationTargets { if let app = await Self.launchHidden(id) { launched.append(app) } }
            // Only an app Wisp just started needs a moment to come up before it can be asked.
            if !launched.isEmpty { try? await Task.sleep(nanoseconds: 1_500_000_000) }
            NSApp.activate(ignoringOtherApps: true)
            let patience = letGoIfUnanswered(item, token: token)
            _ = await Self.automationState(ask: true)
            patience.cancel()
            waiting.remove(item)
            Self.quitAfterAsking(launched)
        case .messages:
            // The same for Messages, on its own target list: its answer never
            // changes the Mail & Notes row or what Mail's reader reports.
            var launched: [NSRunningApplication] = []
            for id in Self.messagesAutomationTargets { if let app = await Self.launchHidden(id) { launched.append(app) } }
            if !launched.isEmpty { try? await Task.sleep(nanoseconds: 1_500_000_000) }
            NSApp.activate(ignoringOtherApps: true)
            let patience = letGoIfUnanswered(item, token: token)
            _ = await Self.messagesAutomationState(ask: true)
            patience.cancel()
            waiting.remove(item)
            Self.quitAfterAsking(launched)
        case .fullDiskAccess:
            // Full Disk Access can't be asked for: open its page and let the person
            // switch Wisp on. The poll notices when they come back. Switching it on
            // makes macOS offer Quit & Reopen, so setup is marked to come back
            // here first (FirstRunSetup.expectRelaunch).
            let defaults = UserDefaults.standard
            FirstRunSetup.expectRelaunch({ defaults.bool(forKey: $0) }, { defaults.set(true, forKey: $0) })
            Permissions.openFullDiskAccessPane()
        case .screenRecording:
            // macOS's question the first time, its Settings page after that
            // (Permissions.requestScreenRecording). It works only after Wisp quits
            // and reopens, which macOS offers once it's switched on, so setup is
            // marked to come back here first, as for Full Disk Access.
            let defaults = UserDefaults.standard
            FirstRunSetup.expectRelaunch({ defaults.bool(forKey: $0) }, { defaults.set(true, forKey: $0) })
            Permissions.requestScreenRecording()
        case .accessibility:
            // The same once-then-Settings rule (Permissions.requestAccessibility).
            Permissions.requestAccessibility()
        }
        await refresh()
    }

    /// What a click on the Calendar or Reminders button does, from macOS's answer
    /// right now. macOS asks once: after a Don't Allow it never shows the question
    /// again, so a second request would look like a button that does nothing.
    enum EventKitClick: Equatable { case ask, openSettings, alreadyAllowed }

    static func eventKitClick(_ status: EKAuthorizationStatus) -> EventKitClick {
        switch status {
        case .notDetermined: return .ask
        case .fullAccess: return .alreadyAllowed
        default: return .openSettings   // denied, restricted, or write-only (Wisp needs full access)
        }
    }

    /// An ask that left macOS with no answer recorded: its question never showed
    /// (or was dismissed without a choice), so Wisp is still absent from the list.
    static func eventKitUnanswered(before: EKAuthorizationStatus, after: EKAuthorizationStatus) -> Bool {
        before == .notDetermined && after == .notDetermined
    }

    /// The Calendar or Reminders row's sentence: what it is for, or, once macOS
    /// has turned it down or never answered, what to do next.
    static func eventKitDetail(_ item: Item, base: String, state: State, unanswered: Bool) -> String {
        let page = item == .reminders ? "Reminders" : "Calendars"
        switch state {
        case .notAllowed:
            return "Wisp is off in System Settings. Click Open System Settings, then switch Wisp on under "
                + "Privacy & Security > \(page)."
        case .notAsked where unanswered:
            return "macOS didn't show its question. Quit and reopen Wisp, then click Allow again."
        default:
            return base
        }
    }

    /// One store per kind, kept for the life of the window: the request is
    /// answered against it, so it is not dropped while macOS is asking.
    private var eventStores: [EKEntityType: EKEventStore] = [:]

    private func eventStore(_ entity: EKEntityType) -> EKEventStore {
        let store = eventStores[entity] ?? EKEventStore()
        eventStores[entity] = store
        return store
    }

    /// Calendar and Reminders are two permissions with two Settings pages, so two
    /// rows: one row for both sent a person who denied only Reminders to Calendars.
    private static func eventKitState(_ entity: EKEntityType) -> State {
        switch EKEventStore.authorizationStatus(for: entity) {
        case .fullAccess: return .allowed
        case .notDetermined: return .notAsked
        default: return .notAllowed
        }
    }

    private static func contactsState() -> State {
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .authorized: return .allowed
        case .notDetermined: return .notAsked
        default: return .notAllowed
        }
    }

    /// What Wisp reads with Full Disk Access: Messages always, Mail and Notes when
    /// those apps are closed (MailDBReader, NotesDBReader).
    nonisolated static let fullDiskAccessPaths = [
        "Library/Messages/chat.db",
        "Library/Mail",
        "Library/Group Containers/group.com.apple.notes/NoteStore.sqlite",
    ]

    /// Whether Messages' history really opens (TCC, not file modes: access(2)
    /// says yes either way).
    nonisolated static func messagesReadable(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> Bool {
        let fd = open(home.appendingPathComponent("Library/Messages/chat.db").path, O_RDONLY)
        if fd >= 0 { close(fd); return true }
        return false
    }

    /// Full Disk Access shows only in whether those files really read: any one that
    /// does means it is on. None of them existing means nothing here needs it yet.
    nonisolated static func fullDiskAccessState(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> State {
        var refused = false
        for relative in fullDiskAccessPaths {
            let url = home.appendingPathComponent(relative)
            var isDirectory: ObjCBool = false
            if FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory), isDirectory.boolValue {
                // A folder is read by listing it: that is what the privacy check guards.
                do { _ = try FileManager.default.contentsOfDirectory(atPath: url.path); return .allowed }
                catch { refused = true; continue }
            }
            let fd = open(url.path, O_RDONLY)
            if fd >= 0 { close(fd); return .allowed }
            if errno != ENOENT { refused = true }
        }
        return refused ? .notAllowed : .notOnThisMac
    }

    private static func notificationsState() async -> State {
        // UNUserNotificationCenter throws outside an app bundle (NotchQA's renders).
        guard Bundle.main.bundleURL.pathExtension == "app" else { return .notAsked }
        switch await UNUserNotificationCenter.current().notificationSettings().authorizationStatus {
        case .authorized, .provisional: return .allowed
        case .notDetermined: return .notAsked
        default: return .notAllowed
        }
    }

    nonisolated static let automationTargets = ["com.apple.mail", "com.apple.Notes"]

    /// Allowed only when both Mail and Notes are. A target that isn't running can't
    /// be asked about (procNotFound), which keeps the last answer macOS gave.
    private static func automationState(ask: Bool) async -> State {
        await Task.detached { automationStateNow(ask: ask) }.value
    }

    /// The same answer, on the calling thread (quick unless `ask` shows a prompt).
    nonisolated static func automationStateNow(ask: Bool) -> State {
        automationStateNow(of: automationTargets, item: .mail, ask: ask)
    }

    /// Messages, which OutboundSender scripts to send a text. Kept apart from
    /// `automationTargets` so the Mail & Notes row and its reader checks never
    /// depend on it. System Events (typing a draft) stays asked at use.
    nonisolated static let messagesAutomationTargets = ["com.apple.MobileSMS"]

    private static func messagesAutomationState(ask: Bool) async -> State {
        await Task.detached {
            automationStateNow(of: messagesAutomationTargets, item: .messages, ask: ask)
        }.value
    }

    /// What one probe of the targets found, before the last answer is weighed in.
    enum AutomationRead: Equatable {
        /// Every target answered "allowed".
        case granted
        /// A target running and refused (errAEEventNotPermitted, -1743).
        case denied
        /// A target running and macOS holds no answer (errAEEventWouldRequireUserConsent,
        /// -1744): never asked, or the grant was reset.
        case notAsked
        /// macOS can't say: a target isn't running (procNotFound, -600), or anything else.
        case unknown
    }

    /// What the last definite probe found, kept in UserDefaults so a closed app, a
    /// closed window or a relaunch can't turn it into "Not set". `reset` is a grant
    /// macOS has since dropped (only a real -1744 from a running app says so).
    enum AutomationAnswer: String, Equatable { case allowed, denied, reset }

    nonisolated static func automationKey(_ item: Item) -> String { "wisp.automation.answer.\(item.rawValue)" }

    nonisolated static func storedAutomation(_ item: Item, defaults: AutomationAnswerStore = UserDefaults.standard) -> AutomationAnswer? {
        defaults.string(forKey: automationKey(item)).flatMap(AutomationAnswer.init(rawValue:))
    }

    /// Pure: the OSStatus of each target's probe, as one read. A refusal or a missing
    /// answer from any running target outranks one that can't be reached.
    nonisolated static func automationRead(_ statuses: [OSStatus]) -> AutomationRead {
        if statuses.contains(OSStatus(errAEEventNotPermitted)) { return .denied }
        if statuses.contains(OSStatus(errAEEventWouldRequireUserConsent)) { return .notAsked }
        if !statuses.isEmpty && statuses.allSatisfy({ $0 == noErr }) { return .granted }
        return .unknown
    }

    /// Pure: what the row shows and what is remembered. Only a probe that got a
    /// definite answer from a running app changes the remembered one; "can't say"
    /// (an app that is closed) never reads as a removed permission.
    nonisolated static func resolveAutomation(_ read: AutomationRead, stored: AutomationAnswer?)
        -> (state: State, stored: AutomationAnswer?) {
        switch read {
        case .granted: return (.allowed, .allowed)
        case .denied: return (.notAllowed, .denied)
        case .notAsked: return (.notAsked, stored == .allowed || stored == .reset ? .reset : nil)
        case .unknown:
            switch stored {
            case .allowed: return (.allowed, stored)
            case .denied: return (.notAllowed, stored)
            case .reset, nil: return (.notAsked, stored)
            }
        }
    }

    /// Allowed only when every target is. A target that isn't running can't be
    /// asked about, so it keeps the answer remembered from the last time one could.
    nonisolated static func automationStateNow(of targets: [String], item: Item, ask: Bool,
                                               defaults: AutomationAnswerStore = UserDefaults.standard) -> State {
        var statuses: [OSStatus] = []
        for id in targets {
            var target = AEAddressDesc()
            let created = id.withCString { pointer in
                AECreateDesc(DescType(typeApplicationBundleID), pointer, strlen(pointer), &target)
            }
            guard created == noErr else { statuses.append(OSStatus(procNotFound)); continue }
            statuses.append(AEDeterminePermissionToAutomateTarget(&target, AEEventClass(typeWildCard),
                                                                  AEEventID(typeWildCard), ask))
            AEDisposeDesc(&target)
        }
        return recordAutomation(automationRead(statuses), item: item, defaults: defaults)
    }

    /// Weighs a probe against the remembered answer, remembers the result, and
    /// returns what the row shows.
    nonisolated static func recordAutomation(_ read: AutomationRead, item: Item,
                                             defaults: AutomationAnswerStore = UserDefaults.standard) -> State {
        let stored = storedAutomation(item, defaults: defaults)
        let resolved = resolveAutomation(read, stored: stored)
        // Write only a change, and never on a read that can't say: the 2 s poll's
        // stale read must not overwrite an answer request() just recorded.
        if read != .unknown, resolved.stored != stored {
            if let answer = resolved.stored { defaults.set(answer.rawValue, forKey: automationKey(item)) }
            else { defaults.removeObject(forKey: automationKey(item)) }
        }
        return resolved.state
    }

    /// How long an Automation question may stay up before its row is let go.
    /// macOS waits for the answer with no time limit, and its dialog can end up
    /// behind other windows, which left the row on "Asking…" with no way out.
    nonisolated static let automationPatience: Duration = .seconds(15)

    /// After `automationPatience`, the row that `token`'s click still holds moves
    /// from "Asking…" to waiting. The ask itself carries on.
    private func letGoIfUnanswered(_ item: Item, token: UUID) -> Task<Void, Never> {
        Task { [weak self] in
            try? await Task.sleep(for: Self.automationPatience)
            guard !Task.isCancelled, let self, self.askTokens[item] == token else { return }
            self.asking.remove(item)
            self.askTokens[item] = nil
            self.waiting.insert(item)
        }
    }

    /// The row's button while macOS has no answer: Allow, Asking… while the
    /// question is new, Ask again once it has been up a while.
    static func allowTitle(_ item: Item, asking: Bool, waiting: Bool) -> String {
        if asking { return "Asking…" }
        if item == .fullDiskAccess { return "Open System Settings" }
        return waiting ? "Ask again" : "Allow"
    }

    /// The row's words while its question is still waiting for an answer.
    static let automationWaitingNote =
        "macOS is still waiting for your answer, and its question may be behind another window. "
            + "Answer it there, or click Ask again."

    /// The row's words when macOS has dropped a grant it once gave.
    static let automationResetNote =
        "macOS forgot this answer, which can happen after Wisp updates. Allow it once more."

    /// Starts the app in the background, unseen, and returns it, so Wisp can close
    /// it again. nil when it was already running (the person's own, never closed
    /// here) or could not be started.
    private static func launchHidden(_ bundleID: String) async -> NSRunningApplication? {
        guard !NSWorkspace.shared.runningApplications.contains(where: { $0.bundleIdentifier == bundleID }),
              let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) else { return nil }
        let config = NSWorkspace.OpenConfiguration()
        config.activates = false
        config.hides = true
        config.addsToRecentItems = false
        return try? await NSWorkspace.shared.openApplication(at: url, configuration: config)
    }

    /// Whether to close an app Wisp opened just to ask: only one Wisp started itself,
    /// that is still out of sight, and that the person didn't bring forward while the
    /// prompt was up. Anything they opened or began using stays open.
    nonisolated static func quitsAfterAsking(launchedByWisp: Bool, isHidden: Bool, isActive: Bool) -> Bool {
        launchedByWisp && isHidden && !isActive
    }

    /// Closes what Wisp opened to ask, politely: the app saves and quits like any
    /// other, and is never forced.
    private static func quitAfterAsking(_ launched: [NSRunningApplication]) {
        for app in launched where !app.isTerminated
            && quitsAfterAsking(launchedByWisp: true, isHidden: app.isHidden, isActive: app.isActive) {
            app.terminate()
        }
    }
}

// MARK: - Open at login

/// "Open Wisp when you log in", on the last step: opt-in, off until the person
/// switches it on. Wisp never adds itself to Login Items: only the toggle calls set(_:).
@MainActor
final class LaunchAtLogin: ObservableObject {
    @Published private(set) var status: SMAppService.Status = .notRegistered
    @Published private(set) var problem: String?

    func read() { status = SMAppService.mainApp.status }

    func set(_ on: Bool) {
        problem = nil
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            OnboardingTimeline.record(on ? "open at login on" : "open at login off")
        } catch {
            NSLog("Wisp: login item \(on ? "register" : "unregister") failed: \(error.localizedDescription)")
            problem = Self.problem(turningOn: on)
        }
        read()
    }

    /// The switch reads on while macOS holds the request, approved or not, so
    /// switching it off takes it back.
    static func isOn(_ status: SMAppService.Status) -> Bool { status == .enabled || status == .requiresApproval }

    /// Said under the switch when macOS wants the person's own yes first.
    static func note(_ status: SMAppService.Status) -> String? {
        status == .requiresApproval
            ? "macOS needs your OK: switch Wisp on in System Settings › General › Login Items."
            : nil
    }

    static func problem(turningOn: Bool) -> String {
        turningOn
            ? "Wisp couldn't add itself just now. You can add it in System Settings › General › Login Items."
            : "Wisp couldn't take itself off just now. You can remove it in System Settings › General › Login Items."
    }
}

// MARK: - View

struct FirstRunSetupView: View {
    typealias Step = OnboardingStep

    @ObservedObject var setup: SetupGuideModel
    @ObservedObject var knowYou: KnowYouModel
    @ObservedObject var google: GoogleAccountModel
    @StateObject private var permissions = FirstRunPermissions()
    @StateObject private var launchAtLogin = LaunchAtLogin()
    @ObservedObject private var notices = NotchSetupNotices.shared
    @ObservedObject private var display = DisplayGeometry.shared
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var step: Step
    @State private var appliedModel: String?
    @State private var openedAt = Date()
    @State private var progressWatch = DownloadProgressWatch()
    /// A finished download the engine still hadn't listed after 30 s.
    @State private var stuckDownload: String?
    /// When step 1 began looking for the person's own oMLX (FirstRunBrain.connectGrace).
    @State private var connectingSince: Date?
    @State private var connectGraceTick = 0
    /// Redraws once FirstRunBrain.serviceStartLimit has passed, so the last step's
    /// footer offers Check again on time too.
    @State private var serviceSlowTick = 0
    /// ⌥Space opened Ask while this window was up (Notification.Name.wispAskHotkeyPressed).
    @State private var askKeyPressed = false
    /// The one automatic test once Wisp is ready (it also loads the model).
    @State private var testedOnce = false
    @State private var showAdvanced = false
    var openDetailedSetup: () -> Void
    /// Closes the window and opens the notch on Ask; with a prompt, Ask's box
    /// holds it, unsent.
    var finish: (_ prompt: String?) -> Void
    /// Every permission read, so a new grant is read at once.
    var permissionsChanged: ([FirstRunPermissions.Item: FirstRunPermissions.State]) -> Void
    /// Where the person is, saved so setup picks up there after a quit.
    var stepChanged: (Step) -> Void
    /// Whether ⌥Space and ⌥⇧Space really belong to Wisp on this Mac.
    var askKeyRegistered: Bool
    var voiceKeyRegistered: Bool
    /// First-run setup already finished: the window was opened again (Permissions…,
    /// a Brief link), so it leaves the model alone (FirstRunSetup.drivesModel).
    var setupDone: Bool
    /// About you was finished or explicitly skipped here.
    var aboutYouDecided: () -> Void
    /// NotchQA's renders: the Google step shows its fixture and never asks the service.
    var qaFixture = false

    init(setup: SetupGuideModel, knowYou: KnowYouModel, google: GoogleAccountModel,
         initialStep: Step = .brain,
         openDetailedSetup: @escaping () -> Void,
         finish: @escaping (_ prompt: String?) -> Void,
         permissionsChanged: @escaping ([FirstRunPermissions.Item: FirstRunPermissions.State]) -> Void = { _ in },
         stepChanged: @escaping (Step) -> Void = { _ in },
         askKeyRegistered: Bool = true, searchKeyRegistered: Bool = true, voiceKeyRegistered: Bool = true,
         setupDone: Bool = false, aboutYouDecided: @escaping () -> Void = {}, qaFixture: Bool = false) {
        self.setup = setup
        self.setupDone = setupDone
        self.aboutYouDecided = aboutYouDecided
        self.qaFixture = qaFixture
        self.knowYou = knowYou
        self.google = google
        _step = State(initialValue: initialStep)
        self.openDetailedSetup = openDetailedSetup
        self.finish = finish
        self.permissionsChanged = permissionsChanged
        self.stepChanged = stepChanged
        self.askKeyRegistered = askKeyRegistered
        // Smart Search is retired, so ⌘⇧F is no longer taught: `searchKeyRegistered`
        // is accepted only until AppDelegate stops passing it.
        self.voiceKeyRegistered = voiceKeyRegistered
    }

    private var brain: FirstRunBrain {
        FirstRunBrain.from(status: setup.status, downloads: setup.downloads, stillLooking: stillLooking)
    }

    /// The search for the person's own oMLX is inside its window (connectGraceTick
    /// redraws at the end, so the failure copy appears on time).
    private var stillLooking: Bool {
        _ = connectGraceTick
        return FirstRunBrain.stillLooking(waited: connectingSince.map { Date().timeIntervalSince($0) })
    }

    var body: some View {
        VStack(spacing: 0) {
            stepBar
            Divider().overlay(Theme.hairline)
            ScrollView {
                Group {
                    switch step {
                    case .brain: brainStep
                    case .permissions: permissionsStep
                    case .google: OnboardingGoogleStep(model: google, refreshes: !qaFixture)
                    case .knowYou: knowYouStep
                    case .tour: tourStep
                    }
                }
                .padding(.horizontal, 28).padding(.vertical, 24)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider().overlay(Theme.hairline)
            footer
        }
        .frame(width: FirstRunSetup.width)
        .frame(minHeight: FirstRunSetup.minimumHeight, maxHeight: .infinity)
        .background(OnboardingBackdrop())
        .foregroundStyle(Theme.textPrimary)
        .preferredColorScheme(.dark)
        .onChange(of: brain) { _, next in
            drive(next)
            if case .downloading(_, _, _, let done, _, _) = next {
                progressWatch.observe(doneGb: done, at: Date())
            } else {
                progressWatch.reset()
            }
        }
        // An error belongs to the stage it happened in: a red line from a failed
        // start or download must not sit under the next one.
        .onChange(of: brain.stage) { _, _ in
            announce(brain)
            record(brain)
            // The search window belongs to the whole look for their oMLX, not to one
            // stage of it: ending it when the card changes would restart it forever.
            if setup.status?.usesOwnOmlx != true { connectingSince = nil }
            if setup.busy == nil, setup.messageIsError {
                setup.message = nil
                setup.messageIsError = false
            }
        }
        .onAppear {
            drive(brain)
            OnboardingTimeline.record("window opened at \(step)")
            // A window that opens on a model already ready (or stuck) never sees the
            // stage change that records it.
            record(brain)
            DispatchQueue.main.asyncAfter(deadline: .now() + FirstRunBrain.serviceStartLimit + 0.1) {
                serviceSlowTick += 1    // redraw, so `stalled` sees the service's time pass
            }
            if !qaFixture { launchAtLogin.read() }
            // One read up front: the last step's prompts follow what is allowed.
            Task { await permissions.refresh() }
            if step == .permissions { permissions.start() }
            if !qaFixture { Task { await knowYou.load() } }
            knowYou.onClose = { _ in
                aboutYouDecided()
                step = .tour
            }
        }
        .onReceive(permissions.$states) { states in if !states.isEmpty { permissionsChanged(states) } }
        .onChange(of: step) { _, next in
            if next == .permissions { permissions.start() } else { permissions.stop() }
            if next == .tour {
                Task { await permissions.refresh() }
                if !qaFixture { launchAtLogin.read() }
            }
            OnboardingTimeline.record("step \(next)")
            stepChanged(next)
        }
        .onReceive(NotificationCenter.default.publisher(for: .wispAskHotkeyPressed)) { _ in
            if !askKeyPressed { OnboardingTimeline.record("ask shortcut worked") }
            askKeyPressed = true
        }
        .onDisappear { permissions.stop() }
        // The automatic start of the person's oMLX may have been skipped (a request in
        // flight, a stale status) while the brain stayed the same: try again as either clears.
        .onChange(of: setup.statusIsCurrent) { _, _ in retryStart() }
        .onChange(of: setup.busy) { _, _ in retryStart() }
    }

    private func retryStart() {
        if case .startingEngine = brain { drive(brain) }
    }

    /// The parts of step 1 that need no click: switch the model on as soon as it
    /// has arrived, and notice a finished download the engine never lists. Starting
    /// the engine is SetupGuideModel's own (autoAdvanceFirstSetup), once a session.
    private func drive(_ state: FirstRunBrain) {
        if case .startingEngine = state {
            // The person's own oMLX hasn't answered. Time the wait in every case, so
            // Try again appears; start it once only during an unfinished first run.
            let steps = FirstRunBrain.whileConnecting(setupDone: setupDone,
                                                      connecting: setup.status?.usesOwnOmlx == true)
            if steps.startsOmlx { setup.startExistingOmlxOnce() }
            if steps.timesGrace, connectingSince == nil {
                connectingSince = Date()
                DispatchQueue.main.asyncAfter(deadline: .now() + FirstRunBrain.connectGrace + 0.1) {
                    connectGraceTick += 1    // redraw, so `stalled` sees the time pass
                }
            }
        }
        guard FirstRunSetup.drivesModel(setupDone: setupDone) else { return }
        switch state {
        case .switching(let model, _) where appliedModel != model && setup.busy == nil:
            appliedModel = model
            // Atomic: applied only if the configuration is still the one this saw.
            Task { await setup.apply(model, expectedConfiguration: setup.status?.configurationToken) }
        case .ready where !testedOnce && setup.busy == nil && setup.statusIsCurrent:
            // "Ready" only means the engine lists the model. One short question
            // proves it answers, and loads it while the person reads the rest, so
            // the first real question doesn't pay the cold load.
            testedOnce = true
            Task { await setup.test() }
        case .waitingForEngine(let model, _):
            DispatchQueue.main.asyncAfter(deadline: .now() + 30) {
                if case .waitingForEngine(let current, _) = brain, current == model { stuckDownload = model }
            }
        default:
            break
        }
    }

    // MARK: Chrome

    /// A step is ticked for what is true, not for having been passed: the model
    /// once Wisp can answer with it, permissions once every one has an answer,
    /// Google once an account is connected, About you once it was finished.
    private func isDone(_ s: Step) -> Bool {
        switch s {
        case .brain: return brain.isReady
        case .permissions: return permissions.allAnswered
        case .google: return !google.status.accounts.isEmpty
        case .knowYou: return knowYouFinished
        case .tour: return false
        }
    }

    private var stepBar: some View {
        HStack(spacing: 4) {
            ForEach(Step.allCases, id: \.self) { s in
                let done = isDone(s)
                Button { step = s } label: {
                    Text(done ? "✓ \(s.title)" : "\(s.rawValue + 1)  \(s.title)")
                        .font(.system(size: 11.5, weight: s == step ? .semibold : .regular))
                        .foregroundStyle(s == step ? Theme.textPrimary : (done ? Color.green.opacity(0.8) : Theme.textMuted))
                        .lineLimit(1)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Capsule().fill(s == step ? Theme.chipFill : .clear))
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Step \(s.rawValue + 1) of \(Step.allCases.count), \(s.title)")
                .accessibilityValue(done ? "Done" : "")
                .accessibilityAddTraits(s == step ? [.isSelected, .isHeader] : [])
            }
            Spacer(minLength: 4)
            if step != .brain, let progress = compactProgress {
                Text(progress).font(.system(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1)
            }
        }
        .padding(.leading, 14).padding(.trailing, 16).padding(.top, 30).padding(.bottom, 10)
    }

    /// The download, in a word or two, on the steps after the first.
    private var compactProgress: String? {
        switch brain {
        case .downloading(_, _, let fraction, _, _, _): return "Model · \(Int(fraction * 100))%"
        case .waitingForEngine, .switching: return "Finishing…"
        case .ready: return "Model ready"
        case .failed(_, _, _, let paused): return paused ? "Download stopped" : "Download failed"
        default: return nil
        }
    }

    private func go(_ s: Step?) { if let s { step = s } }

    /// About you is finished by the service's word (refreshed by Done), so the
    /// tick and the thank-you survive closing and reopening the window.
    private var knowYouFinished: Bool { knowYou.status.completed }

    private var footer: some View {
        HStack(spacing: 10) {
            switch step {
            case .brain:
                // The heading already says the model runs on this Mac; the footer
                // only says what is happening.
                if brain.isArriving {
                    Text("You can keep going while this downloads.")
                        .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                }
                Spacer()
                if brain.cardTakesReturn(stalled: stalled) {
                    // Return belongs to the card's button here (Download, Retry, Use,
                    // a stuck Try again), so it can't skip past the step Wisp can't
                    // answer without.
                    Button("Skip for now") { go(step.next) }
                        .buttonStyle(FirstRunButtonStyle())
                } else {
                    Button("Continue") { go(step.next) }
                        .buttonStyle(FirstRunButtonStyle(primary: true))
                        .keyboardShortcut(.defaultAction)
                }
            case .permissions:
                Text(FirstRunPermissions.summary(permissions.states, messagesReadable: permissions.messagesReadable))
                    .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                Spacer()
                Button("Back") { go(step.previous) }.buttonStyle(FirstRunButtonStyle())
                Button("Continue") { go(step.next) }
                    .buttonStyle(FirstRunButtonStyle(primary: true))
                    .keyboardShortcut(.defaultAction)
            case .google:
                Text("Optional").font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                Spacer()
                Button("Back") { go(step.previous) }.buttonStyle(FirstRunButtonStyle())
                if google.status.accounts.isEmpty {
                    Button("Skip for now") { go(step.next) }.buttonStyle(FirstRunButtonStyle())
                } else {
                    Button("Continue") { go(step.next) }
                        .buttonStyle(FirstRunButtonStyle(primary: true))
                        .keyboardShortcut(.defaultAction)
                }
            case .knowYou:
                // The questions carry their own Continue (and Return); the window
                // only moves between steps, so it never takes Return from them.
                Button("Back") { go(step.previous) }.buttonStyle(FirstRunButtonStyle())
                Spacer()
                if knowYouFinished {
                    Button("Continue") { go(step.next) }
                        .buttonStyle(FirstRunButtonStyle(primary: true))
                        .keyboardShortcut(.defaultAction)
                } else {
                    Button("Skip for now") {
                        aboutYouDecided()
                        go(step.next)
                    }
                    .buttonStyle(FirstRunButtonStyle())
                    .help("Nothing more is saved. Type /about in Ask to answer these any time.")
                }
            case .tour:
                Button("Back") { go(step.previous) }.buttonStyle(FirstRunButtonStyle())
                tourModelStatus
                Spacer(minLength: 6)
                // Doesn't wait for the model: the notch's quiet setup cue carries
                // the download from here, with a notification once Wisp can answer.
                Button("Done — open Wisp") {
                    OnboardingTimeline.record("setup done")
                    finish(nil)
                }
                    .buttonStyle(FirstRunButtonStyle(primary: true))
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(.horizontal, 22).padding(.vertical, 12)
    }

    /// A missed poll, said for this window: it has no Check again button (the
    /// shared model's wording names one), and it does try again by itself. While
    /// nothing has answered yet, the Starting Wisp card already says so.
    private var transientNote: String? {
        if setup.downloadError != nil {
            return "Wisp couldn't check the download just now. It tries again by itself."
        }
        if setup.refreshError != nil, setup.status != nil {
            return "Wisp didn't answer just now. This updates by itself."
        }
        return nil
    }

    /// Still inside the search for the person's own oMLX: nothing is said to have
    /// failed yet, including the error line of an automatic start.
    private var lookingForOmlx: Bool {
        guard case .startingEngine = brain else { return false }
        return setup.status?.usesOwnOmlx == true && stillLooking
    }

    /// Wisp's own step for this state stopped and needs the person's click.
    private var stalled: Bool {
        guard setup.busy == nil else { return false }
        switch brain {
        case .checking:
            _ = serviceSlowTick
            return FirstRunBrain.serviceSlow(waited: Date().timeIntervalSince(openedAt))
        case .startingEngine:
            _ = connectGraceTick
            return FirstRunBrain.startStalled(
                autoStarted: setup.autoStartedEngine, failed: setup.messageIsError,
                ownOmlx: setup.status?.usesOwnOmlx == true,
                waited: connectingSince.map { Date().timeIntervalSince($0) } ?? 0)
        case .switching(let model, _): return appliedModel == model && setup.messageIsError
        case .waitingForEngine(let model, _): return stuckDownload == model
        default: return false
        }
    }

    /// The model, on the last step: a spinner only while it is really on its way,
    /// otherwise the one click that gets it going, so the step never waits on nothing.
    @ViewBuilder private var tourModelStatus: some View {
        switch brain.footer(stalled: stalled) {
        case .ready:
            Circle().fill(Theme.orb).frame(width: 9, height: 9).accessibilityHidden(true)
            Text("Model ready").font(.system(size: 12)).foregroundStyle(Theme.textMuted)
        case .working(let text):
            ProgressView().controlSize(.small)
            Text(text).font(.system(size: 12)).foregroundStyle(Theme.textMuted).lineLimit(1)
        case .download:
            fetchButton("Download the model")
        case .retry:
            if case .waitingForEngine = brain { fetchButton("Download again") } else { fetchButton("Retry download") }
        case .tryAgain:
            if let model = brain.modelToUse {
                Button("Switch to the model") { Task { await setup.apply(model) } }
                    .buttonStyle(FirstRunButtonStyle())
                    .disabled(setup.busy != nil)
            } else {
                Button("Try again") { Task { await setup.startEngine() } }
                    .buttonStyle(FirstRunButtonStyle())
                    .disabled(setup.busy != nil)
            }
        case .detailedSetup:
            Button("Open detailed setup") { openDetailedSetup() }.buttonStyle(FirstRunButtonStyle())
        case .checkAgain:
            Text("Wisp is slow to start").font(.system(size: 12)).foregroundStyle(Theme.textMuted).lineLimit(1)
            checkAgainButton()
        }
    }

    /// Asks the service once more when it has been slow to come up. Wisp keeps
    /// starting it in the background either way. `takesReturn` on step 1's card
    /// only: on the last step Return stays with Done.
    @ViewBuilder private func checkAgainButton(takesReturn: Bool = false) -> some View {
        let button = Button(setup.loading ? "Checking…" : "Check again") { Task { await setup.refresh() } }
            .buttonStyle(FirstRunButtonStyle(primary: takesReturn))
            .disabled(setup.loading)
        if takesReturn { button.keyboardShortcut(.defaultAction) } else { button }
    }

    /// Every Download, Retry, Resume and Download again goes through here, so the
    /// timeline sees each start.
    private func startDownload(_ model: String) {
        OnboardingTimeline.record("download started")
        Task { await setup.download(model) }
    }

    /// The disk-space warning, wherever a click would start a download (the
    /// card's button or the last step's footer), not only before the first one.
    private var diskWarning: String? {
        guard brain.footer(stalled: stalled).fetches else { return nil }
        return FirstRunBrain.diskWarning(freeBytes: Self.freeBytes(), sizeGb: setup.status?.models.download?.sizeGb)
    }

    @ViewBuilder private var diskWarningLine: some View {
        if let warning = diskWarning {
            Text(warning).font(.system(size: 12)).foregroundStyle(Color.orange).padding(.bottom, 4)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// Step 1's Download or Retry. While the request to start it is in flight
    /// (up to a few minutes when the engine is starting too) it shows that,
    /// instead of a button that has merely gone grey.
    @ViewBuilder private func downloadButton(_ title: String, model: String) -> some View {
        if setup.busy == "Starting download…" {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("Starting download…").font(.system(size: 12)).foregroundStyle(Theme.textMuted)
            }
        } else {
            Button(title) { startDownload(model) }
                .buttonStyle(FirstRunButtonStyle(primary: true))
                .keyboardShortcut(.defaultAction)
                .disabled(setup.busy != nil)
        }
    }

    /// Free space on the volume the engine keeps its models on (the home folder's).
    static func freeBytes() -> Int64? {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return try? home.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
            .volumeAvailableCapacityForImportantUsage
    }

    @ViewBuilder private func fetchButton(_ title: String) -> some View {
        if let model = brain.modelToFetch {
            Button(title) { startDownload(model) }
                .buttonStyle(FirstRunButtonStyle())
                .disabled(setup.busy != nil)
        }
    }

    // MARK: Step 1: Model

    private var brainStep: some View {
        VStack(alignment: .leading, spacing: 0) {
            OnboardingHeading(title: "Welcome to Wisp",
                              subtitle: FirstRunBrain.welcomeSubtitle(remoteOnly: setup.status?.remoteOnly == true))
            if let hw = setup.status?.hardware {
                OnboardingCard(icon: "cpu", title: "Picked for your Mac",
                               detail: "\(hw.chip) · \(hw.ramGb > 0 ? "\(hw.ramGb) GB memory" : "memory unknown")") {
                    EmptyView()
                }
            }
            modelCard
            // The can't-set-this-up card may already be saying this very line.
            if let warning = setup.status?.models.warning, brain != .unavailable(warning) {
                Text(warning).font(.system(size: 12)).foregroundStyle(Theme.textSecondary).padding(.top, 4)
            }
            if case .unavailable = brain {} else {
                // Everything a first run needs is above. Other apps and the full
                // model list stay one disclosure away, so the detailed setup is
                // never a second setup surface by default.
                DisclosureGroup(isExpanded: $showAdvanced) {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Already use Ollama or LM Studio, or want a different model? The detailed setup "
                             + "connects another app or lists every model. This window steps aside while it is open.")
                            .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                            .fixedSize(horizontal: false, vertical: true)
                        Button("Open detailed setup…") { openDetailedSetup() }.buttonStyle(FirstRunButtonStyle())
                    }
                    .padding(.top, 8)
                } label: {
                    Text("Advanced").font(.system(size: 12, weight: .medium)).foregroundStyle(Theme.textSecondary)
                }
                .padding(.top, 12)
            }
        }
    }

    @ViewBuilder private var modelCard: some View {
        switch brain {
        case .checking:
            // Wisp's service is still starting: a cold first launch (bundle checks,
            // first imports) can take minutes, so say so and show the time. Past the
            // 3 minutes the notch allows too, it stops promising and offers a click.
            TimelineView(.periodic(from: openedAt, by: 5)) { context in
                let elapsed = context.date.timeIntervalSince(openedAt)
                // `stalled` too, so the card and the footer change over together
                // (Return moves from the footer to Check again at that moment).
                if stalled || FirstRunBrain.serviceSlow(waited: elapsed) {
                    OnboardingCard(icon: "hourglass", title: "Wisp is taking longer than usual to start",
                                   detail: "It keeps trying in the background. Check again in a moment, "
                                       + "or open the detailed setup to see what's going on.") {
                        HStack(spacing: 8) {
                            checkAgainButton(takesReturn: true)
                            Button("Open detailed setup") { openDetailedSetup() }.buttonStyle(FirstRunButtonStyle())
                        }
                    }
                } else {
                    let waited = FirstRunBrain.waited(Int(elapsed))
                    OnboardingCard(icon: "hourglass", title: "Starting Wisp…",
                                   detail: "The first launch can take a few minutes." + (waited.map { " (\($0))" } ?? "")) {
                        ProgressView().controlSize(.small)
                    }
                }
            }
        case .startingEngine:
            let ownOmlx = setup.status?.usesOwnOmlx == true
            OnboardingCard(icon: "bolt", title: FirstRunBrain.startingTitle(ownOmlx: ownOmlx),
                           detail: ownOmlx ? FirstRunBrain.connectingDetail(setupDone: setupDone, stalled: stalled)
                                           : "It's built into Wisp, so there's nothing else to install.") {
                if stalled {
                    HStack(spacing: 8) {
                        Button("Try again") {
                            // A fresh look: the person's oMLX gets the whole search window again.
                            if ownOmlx { connectingSince = nil }
                            Task { await setup.startEngine() }
                        }
                        .buttonStyle(FirstRunButtonStyle(primary: true))
                        .keyboardShortcut(.defaultAction)
                        if ownOmlx {
                            Button("Open detailed setup") { openDetailedSetup() }.buttonStyle(FirstRunButtonStyle())
                        }
                    }
                } else {
                    ProgressView().controlSize(.small)
                }
            }
        case .unavailable(let reason):
            OnboardingCard(icon: "exclamationmark.triangle", title: "Wisp can't set this up on its own", detail: reason) {
                Button("Open detailed setup") { openDetailedSetup() }.buttonStyle(FirstRunButtonStyle(primary: true))
            }
        case .needsDownload(let model, let label, let size):
            OnboardingCard(icon: "arrow.down.circle", title: label + (size.map { String(format: " · %.1f GB", $0) } ?? ""),
                           detail: "Fast and private. It runs inside Wisp, so there's nothing else to install.") {
                HStack(spacing: 8) {
                    OnboardingPill(text: "Recommended")
                    downloadButton("Download", model: model)
                }
            }
        case .downloading(let model, let label, let fraction, let done, let total, let left):
            VStack(alignment: .leading, spacing: 0) {
                OnboardingCard(icon: "arrow.down.circle", title: label + String(format: " · %.1f GB", total),
                               detail: "Fast and private. It runs inside Wisp, so there's nothing else to install.") {
                    EmptyView()
                }
                if let slept = notices.sleepLine(brain, downloads: setup.downloads) {
                    Text(slept).font(.system(size: 11)).foregroundStyle(.secondary).padding(.bottom, 2)
                }
                ProgressView(value: fraction).tint(OnboardingPalette.lilac).padding(.top, 2)
                    .accessibilityLabel("Model download")
                    .accessibilityValue("\(Int(fraction * 100)) percent")
                HStack {
                    TimelineView(.periodic(from: .now, by: 5)) { context in
                        if progressWatch.stalled(at: context.date) {
                            Text(String(format: "%.1f of %.1f GB", done, total) + " · No progress — check your connection")
                                .foregroundStyle(Color.orange)
                        } else {
                            Text(String(format: "%.1f of %.1f GB", done, total)
                                 + (FirstRunBrain.timeLeft(left).map { " · \($0)" } ?? ""))
                        }
                    }
                    Spacer()
                    // A cancel, not a pause: the transfer ends, and Resume starts it again.
                    Button("Stop") {
                        OnboardingTimeline.record("download stopped")
                        Task { await setup.download(model, cancel: true) }
                    }
                    .buttonStyle(.plain).foregroundStyle(Theme.textMuted)
                    .help("Stops the download. Resume starts it again.")
                }
                .font(.system(size: 12)).foregroundStyle(Theme.textMuted).padding(.top, 6)
            }
        case .failed(let model, let label, let message, let paused):
            OnboardingCard(icon: "arrow.clockwise.circle", title: (paused ? "Download stopped" : "Download failed") + " · " + label,
                           detail: message) {
                downloadButton(paused ? "Resume" : "Retry", model: model)
            }
        case .waitingForEngine(let model, let label):
            if stuckDownload == model {
                OnboardingCard(icon: "exclamationmark.triangle", title: label,
                               detail: "Downloaded, but Wisp can't find its files. Download it again to fix that.") {
                    Button("Download again") { startDownload(model) }
                        .buttonStyle(FirstRunButtonStyle(primary: true))
                        .keyboardShortcut(.defaultAction)
                        .disabled(setup.busy != nil)
                }
            } else {
                OnboardingCard(icon: "checkmark.circle", title: label, detail: "Downloaded. Getting it ready…") {
                    ProgressView().controlSize(.small)
                }
            }
        case .offered(let model, let label):
            OnboardingCard(icon: "checkmark.circle", title: label,
                           detail: "Found in your oMLX. Wisp can use it for chat, tools, coding and reasoning.") {
                Button("Use \(label)") { Task { await setup.apply(model) } }
                    .buttonStyle(FirstRunButtonStyle(primary: true))
                    .keyboardShortcut(.defaultAction)
                    .disabled(setup.busy != nil)
            }
        case .switching(let model, let label):
            if stalled {
                OnboardingCard(icon: "exclamationmark.triangle", title: label,
                               detail: (setup.status?.omlx.bundled == true ? "Downloaded" : "Found in your oMLX")
                                   + ", but Wisp couldn't switch over to it.") {
                    Button("Try again") { Task { await setup.apply(model) } }
                        .buttonStyle(FirstRunButtonStyle(primary: true))
                        .keyboardShortcut(.defaultAction)
                        .disabled(setup.busy != nil)
                }
            } else {
                OnboardingCard(icon: "checkmark.circle", title: label, detail: (setup.status?.omlx.bundled == true ? "Downloaded" : "Found in your oMLX")
                                   + ". Switching Wisp over to it…") {
                    ProgressView().controlSize(.small)
                }
            }
        case .ready(let label):
            OnboardingCard(icon: "checkmark.circle.fill", title: "\(label) is ready",
                           detail: setup.testing ? "Checking it answers…" : (setup.testResult ?? "Wisp can answer now.")) {
                if setup.testing {
                    ProgressView().controlSize(.small)
                } else if setup.testResult != nil && !setup.testPassed {
                    Button("Try again") { Task { await setup.test() } }.buttonStyle(FirstRunButtonStyle())
                } else {
                    OnboardingPill(text: "Ready")
                }
            }
        }
        diskWarningLine
        if setup.messageIsError, let message = setup.message, !lookingForOmlx {
            Text(message).font(.system(size: 12)).foregroundStyle(Theme.bad).padding(.top, 6)
        } else if let note = transientNote {
            Text(note).font(.system(size: 12)).foregroundStyle(Theme.textSecondary).padding(.top, 6)
        }
    }

    // MARK: Step 2: Permissions

    private var permissionsStep: some View {
        VStack(alignment: .leading, spacing: 0) {
            OnboardingHeading(title: "Let Wisp see your day",
                              subtitle: "Each one is optional, and nothing is asked until you click its button. "
                                  + "Wisp reads these to help; it asks before sending or changing anything.")
            permissionRow(.calendar, icon: "calendar", title: "Calendar",
                          detail: FirstRunPermissions.eventKitDetail(
                              .calendar, base: "Your meetings and events, for your day plan.",
                              state: permissions.state(.calendar), unanswered: permissions.unanswered[.calendar] == true))
            permissionRow(.reminders, icon: "checklist", title: "Reminders",
                          detail: FirstRunPermissions.eventKitDetail(
                              .reminders, base: "What's due, and the reminders Wisp sets for you.",
                              state: permissions.state(.reminders), unanswered: permissions.unanswered[.reminders] == true))
            permissionRow(.mail, icon: "envelope", title: "Mail & Notes",
                          detail: "Emails that need a reply, and your notes, while those apps are open. "
                              + "Opens Mail and Notes for a moment to ask, then closes them.")
            permissionRow(.messages, icon: "message", title: "Messages",
                          detail: "Sends the texts you approve in Ask. Opens Messages for a moment to ask, then closes it.")
            permissionRow(.fullDiskAccess, icon: "internaldrive", title: "Full Disk Access",
                          detail: fullDiskAccessDetail)
            contactsRow
            permissionRow(.notifications, icon: "bell", title: "Notifications",
                          detail: "Alerts for reminders you set, and when Wisp is ready.")
            permissionRow(.screenRecording, icon: "rectangle.dashed.badge.record", title: "Screen Recording",
                          detail: FirstRunPermissions.screenRecordingDetail(permissions.state(.screenRecording)))
            permissionRow(.accessibility, icon: "accessibility", title: "Accessibility",
                          detail: FirstRunPermissions.accessibilityDetail(permissions.state(.accessibility)))
            Text("Skipped one? Open Wisp's Settings and choose Permissions… under Privacy, any time. Only the "
                 + "microphone, asked the first time you use voice, and macOS's questions about Wisp controlling "
                 + "another app wait until first use.")
                .font(.system(size: 12)).foregroundStyle(Theme.textMuted).padding(.top, 8)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// Said in the row before the Settings page opens (never a modal): switching
    /// it on can make macOS quit and reopen Wisp, and setup comes back at this
    /// step when it does (FirstRunSetup.resumeKey).
    private var fullDiskAccessDetail: String {
        switch permissions.state(.fullDiskAccess) {
        case .notAllowed, .notAsked:
            return "So Wisp can read Messages, and Mail and Notes when those apps are closed. In the page "
                + "that opens, switch on Wisp. " + FirstRunPermissions.fullDiskAccessRelaunchNote
        case .notOnThisMac: return "Nothing here needs it yet: no Messages, Mail or Notes data on this Mac."
        default: return "So Wisp can read Messages, and Mail and Notes when those apps are closed."
        }
    }

    /// Contacts puts names on Messages: it waits for Full Disk Access, which is
    /// what reads Messages, unless macOS already has an answer or Full Disk Access
    /// has nothing to read here (FirstRunPermissions.contactsWaits).
    @ViewBuilder private var contactsRow: some View {
        if FirstRunPermissions.contactsWaits(permissions.states, messagesReadable: permissions.messagesReadable) {
            OnboardingCard(icon: "person.crop.circle", title: "Contacts",
                           detail: "Names instead of phone numbers in Messages. It's useful once Full Disk Access lets Wisp read Messages.") {
                Text("After Full Disk Access").font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
            }
        } else {
            permissionRow(.contacts, icon: "person.crop.circle", title: "Contacts",
                          detail: "Names instead of phone numbers in Messages.")
        }
    }

    private func permissionRow(_ item: FirstRunPermissions.Item, icon: String, title: String, detail: String) -> some View {
        let state = permissions.state(item)
        let label = FirstRunPermissions.label(item, state)
        let detail = permissions.waiting.contains(item) ? detail + " " + FirstRunPermissions.automationWaitingNote
            : permissions.resetItems.contains(item) ? detail + " " + FirstRunPermissions.automationResetNote : detail
        return OnboardingCard(icon: icon, title: title, detail: detail) {
            HStack(spacing: 8) {
                switch state {
                case .allowed:
                    Label(label, systemImage: "checkmark").font(.system(size: 12)).foregroundStyle(Color.green.opacity(0.85))
                case .checking:
                    ProgressView().controlSize(.small)
                case .notOnThisMac:
                    Text(label).font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                case .notAllowed:
                    Text(label).font(.system(size: 11.5)).foregroundStyle(Color.orange.opacity(0.9))
                    Button("Open System Settings") {
                        if FirstRunPermissions.settingsThroughRequest(item) { Task { await permissions.request(item) } }
                        else { Permissions.openSettingsPane(Self.settingsPane(item)) }
                    }
                    .buttonStyle(FirstRunButtonStyle())
                    .accessibilityLabel("Open System Settings for \(title)")
                case .notAsked:
                    Text(label).font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
                    Button(FirstRunPermissions.allowTitle(item, asking: permissions.asking.contains(item),
                                                          waiting: permissions.waiting.contains(item))) {
                        Task { await permissions.request(item) }
                    }
                    .buttonStyle(FirstRunButtonStyle(primary: true))
                    .disabled(permissions.asking.contains(item))
                    .accessibilityLabel(item == .fullDiskAccess ? "Open System Settings for \(title)"
                                        : permissions.waiting.contains(item) ? "Ask again for \(title)" : "Allow \(title)")
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityValue(label)
    }

    /// VoiceOver hears the moments that matter without hunting for them: the
    /// model ready, or its download stopping.
    private func announce(_ state: FirstRunBrain) {
        let text: String
        switch state {
        case .ready(let label): text = "\(label) is ready"
        case .failed(_, _, _, let paused): text = paused ? "Download stopped" : "Download failed"
        case .unavailable: text = "Wisp can't set this up on its own"
        default: return
        }
        NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested,
                             userInfo: [.announcement: text, .priority: NSAccessibilityPriorityLevel.high.rawValue])
    }

    /// The moments a stuck setup is told apart by (OnboardingTimeline): event names only.
    private func record(_ state: FirstRunBrain) {
        switch state {
        case .ready: OnboardingTimeline.record("model ready")
        case .failed(_, _, _, let paused): if !paused { OnboardingTimeline.record("download failed") }
        case .unavailable: OnboardingTimeline.record("model unavailable")
        default: break
        }
    }

    /// Where to change a permission that was turned down.
    static func settingsPane(_ item: FirstRunPermissions.Item) -> String {
        let base = "com.apple.preference.security?"
        switch item {
        case .calendar: return base + "Privacy_Calendars"
        case .reminders: return base + "Privacy_Reminders"
        case .mail, .messages: return base + "Privacy_Automation"
        case .fullDiskAccess: return Permissions.fullDiskAccessPane
        case .contacts: return base + "Privacy_Contacts"
        case .notifications: return "com.apple.preference.notifications"
        case .screenRecording: return base + "Privacy_ScreenCapture"
        case .accessibility: return base + "Privacy_Accessibility"
        }
    }

    // MARK: Step 4: Getting to know you

    @ViewBuilder private var knowYouStep: some View {
        if knowYouFinished {
            VStack(alignment: .leading, spacing: 0) {
                OnboardingHeading(title: knowYou.profile.name.isEmpty ? "Thank you" : "Thank you, \(knowYou.profile.name)",
                                  subtitle: "Wisp will put your answers to work. To change or erase any of them, "
                                      + "open About you in the Wisp window's Memory, or type /about in Ask.")
                Label(knowYou.status.whereItLives, systemImage: knowYou.status.localOnly ? "lock.fill" : "cloud")
                    .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
            }
        } else {
            VStack(alignment: .leading, spacing: 0) {
                OnboardingHeading(title: "Getting to know you",
                                  subtitle: "Three quick questions, all optional. Each answer is saved when you continue.")
                KnowYouView(model: knowYou, inWindow: true)
            }
        }
    }

    // MARK: Step 5: How to use

    /// What can be read right now, for the prompts.
    private var live: OnboardingLiveSources {
        let mailFolder = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Mail").path
        let connected = google.status.accounts.filter { $0.state == .connected }
        return OnboardingLiveSources(
            calendar: permissions.state(.calendar) == .allowed,
            mail: permissions.state(.mail) == .allowed
                || (permissions.state(.fullDiskAccess) == .allowed && FileManager.default.fileExists(atPath: mailFolder)),
            google: !connected.isEmpty,
            googleCalendar: connected.contains { $0.calendar == true })
    }

    private var tourStep: some View {
        VStack(alignment: .leading, spacing: 0) {
            OnboardingHeading(title: "Wisp lives at the top of your screen",
                              subtitle: "No window to find. It's always one gesture away.")
            NotchLocationIllustration(style: display.layout.style)
                .padding(.bottom, 10)
            OnboardingCard(icon: "hand.point.up.left", title: "Where to find it",
                           detail: Self.findLine(display.layout.style)) { EmptyView() }
            OnboardingCard(icon: "keyboard", title: "Keyboard",
                           detail: Self.shortcutsLine(ask: askKeyRegistered, voice: voiceKeyRegistered)) { EmptyView() }
            if let tryIt = Self.shortcutTry(registered: askKeyRegistered, pressed: askKeyPressed) {
                Label(tryIt, systemImage: askKeyPressed ? "checkmark.circle.fill" : "keyboard")
                    .font(.system(size: 12))
                    .foregroundStyle(askKeyPressed ? Color.green.opacity(0.85) : Theme.textSecondary)
                    .padding(.leading, 4).padding(.bottom, 9)
                    .accessibilityElement(children: .combine)
            }
            Text("Open, it has three tabs").font(.system(size: 13, weight: .medium)).padding(.top, 6).padding(.bottom, 8)
            OpenNotchIllustration(tabs: Self.tabLines)
                .padding(.bottom, 14)
            if case .working = brain.footer(stalled: stalled) {
                Text("You can start now. Wisp lets you know when its model is ready.")
                    .font(.system(size: 12)).foregroundStyle(Theme.textSecondary).padding(.bottom, 10)
            }
            diskWarningLine
            // Until Wisp can answer, a prompt would only leave a question it can't
            // send, and it must not end setup either.
            let canAnswer = setup.status?.canAnswer == true
            VStack(alignment: .leading, spacing: 6) {
                Text("Try asking").font(.system(size: 13, weight: .medium))
                Text(canAnswer ? "Click one to open Ask with it typed in. Press Return to send it."
                               : "These work once the model is ready.")
                    .font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
                ForEach(OnboardingPrompts.prompts(live), id: \.self) { prompt in
                    Button {
                        guard canAnswer else { return }
                        OnboardingTimeline.record("try asking")
                        finish(prompt)
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: "text.cursor").accessibilityHidden(true)
                            Text("“\(prompt)”")
                            Spacer(minLength: 0)
                        }
                        .font(.system(size: 12.5)).foregroundStyle(Theme.textPrimary)
                        .padding(.horizontal, 12).padding(.vertical, 8)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Color.white.opacity(0.04)))
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.hairline))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(!canAnswer)
                    .opacity(canAnswer ? 1 : 0.5)
                    .help(canAnswer ? "Opens Ask with this typed in, ready to send" : "Works once the model is ready")
                    .accessibilityLabel("Try asking: \(prompt)")
                    .accessibilityHint(canAnswer ? "Opens Ask with this typed in. It isn't sent until you press Return."
                                                 : "Works once the model is ready.")
                }
            }
            .padding(.bottom, 14)
            launchAtLoginRow
        }
    }

    /// Opt-in, off by default: macOS's answer is read, never assumed, and only
    /// the switch changes it.
    private var launchAtLoginRow: some View {
        let status = launchAtLogin.status
        return VStack(alignment: .leading, spacing: 0) {
            OnboardingCard(icon: "power", title: "Open Wisp when you log in",
                           detail: "So it's there at the top of your screen whenever you start your Mac.") {
                Toggle("Open Wisp when you log in", isOn: Binding(
                    get: { LaunchAtLogin.isOn(status) },
                    set: { launchAtLogin.set($0) }))
                    .toggleStyle(.switch).labelsHidden().controlSize(.small)
                    .disabled(qaFixture)
            }
            if let note = launchAtLogin.problem ?? LaunchAtLogin.note(status) {
                HStack(alignment: .top, spacing: 10) {
                    Text(note).font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    Button("Open Login Items") { SMAppService.openSystemSettingsLoginItems() }
                        .buttonStyle(FirstRunButtonStyle())
                }
                .padding(.bottom, 9)
            }
        }
    }

    /// One line per notch tab, in the notch's order.
    static let tabLines: [(tab: String, line: String)] = [
        ("Ask", "Ask anything. With your permission it reads your mail, calendar, reminders and notes."),
        ("Brief", "What needs you today, prepared before you ask."),
        ("Day", "Your day hour by hour, with to-dos placed on the timeline."),
    ]

    /// Where Wisp is on this display, in words that match the picture.
    static func findLine(_ style: SurfaceLayout.Style) -> String {
        switch style {
        case .notch:
            return "Hover or click the notch at the top centre of your screen. Wisp's icon is in the menu bar too: "
                + "click it to open Wisp, right-click it for Settings."
        case .menuBar:
            return "Push the pointer against the top centre of your screen, or click Wisp's icon in the menu bar. "
                + "Right-click the icon for Settings."
        case .floating:
            return "Hover the small bar at the top centre of your screen, or click Wisp's icon in the menu bar. "
                + "Right-click the icon for Settings."
        }
    }

    /// The shortcuts this Mac really has. A combination another app owns does
    /// nothing in Wisp, so it is not taught; the top of the screen always works.
    static func shortcutsLine(ask: Bool, voice: Bool) -> String {
        switch (ask, voice) {
        case (true, true): return "⌥ Space to ask  ·  ⌥ ⇧ Space to ask by voice"
        case (true, false): return "⌥ Space to ask"
        case (false, true): return "Another app uses ⌥ Space, so open Wisp from the top of the screen  ·  ⌥ ⇧ Space to ask by voice"
        case (false, false): return "Another app uses Wisp's shortcuts, so open it from the top of the screen or the menu bar"
        }
    }

    /// The live check under the shortcuts: an invitation, then the proof once
    /// ⌥Space has really opened Ask. Nothing when another app owns the key.
    static func shortcutTry(registered: Bool, pressed: Bool) -> String? {
        guard registered else { return nil }
        return pressed ? "That's it — ⌥ Space works." : "Try it now: press ⌥ Space."
    }

    /// How to open Wisp on this display, in words that match what's drawn there.
    static func openHint(_ style: SurfaceLayout.Style) -> String {
        switch style {
        case .notch: return "Wisp lives here — hover or click"
        case .menuBar: return "Wisp lives here — push the pointer to the top"
        case .floating: return "Wisp lives here — hover or click"
        }
    }
}

struct FirstRunButtonStyle: ButtonStyle {
    var primary = false
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(primary ? Color.white : Theme.textPrimary)
            .padding(.horizontal, 13).padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: 7)
                .fill(primary ? OnboardingPalette.violet : Color.white.opacity(0.06)))
            .overlay(RoundedRectangle(cornerRadius: 7).stroke(primary ? .clear : Theme.chipStroke))
            .opacity(enabled ? (configuration.isPressed ? 0.8 : 1) : 0.4)
    }
}

/// What the remembered Automation answers need from UserDefaults, so their checks
/// can run on an in-memory store instead of writing a preferences file.
protocol AutomationAnswerStore: AnyObject {
    func string(forKey defaultName: String) -> String?
    func set(_ value: Any?, forKey defaultName: String)
    func removeObject(forKey defaultName: String)
}

extension UserDefaults: AutomationAnswerStore {}
