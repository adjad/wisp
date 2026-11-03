import AppKit
import Combine
import SwiftUI

extension Notification.Name {
    /// Posted by Settings (or anything else) to open the setup guide.
    static let wispOpenSetupGuide = Notification.Name("WispOpenSetupGuide")
    /// Posted after Settings writes inference configuration (a provider, its
    /// roles, the engine mode), whether or not the write succeeded. Setup's
    /// "Test passed" belongs to the configuration it was earned on.
    static let wispInferenceConfigurationChanged = Notification.Name("WispInferenceConfigurationChanged")
}

// MARK: - Wire types (GET /setup/status)

struct SetupStatus: Decodable {
    struct Hardware: Decodable { let chip: String; let ramGb: Int; let tier: String }
    struct Omlx: Decodable {
        let installed: Bool; let running: Bool; let modelDir: String?
        let minMacos: Int; let url: String; let adminUrl: String
        let bundled: Bool?
    }
    struct BundledEngine: Decodable {
        let available: Bool
        let minimumMacos: String?
        let unavailableReason: String?
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
        let recommendedLabel: String?
        /// False when no model Wisp can download fits this Mac's memory (or the
        /// memory couldn't be read): setup offers another route, not a download.
        /// Absent from an older service, which never said.
        let fits: Bool?
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
    let bundledEngine: BundledEngine?
    let models: Models
    let roles: [RoleRow]
    let engines: [Engine]
    let checks: [Check]
    let disclosure: String
    let configurationToken: String?
    /// Every role on a remote provider: nothing to download (service/setup/guide.py).
    var remoteOnly: Bool? = nil
    var canAnswer: Bool { Self.canAnswer(ready: ready, remoteOnly: remoteOnly == true) }
    /// Wisp can answer: a ready local model, or nothing to download. The one rule
    /// for finishing setup, the menu, the setup cue and the readers.
    static func canAnswer(ready: Bool, remoteOnly: Bool) -> Bool { ready || remoteOnly }

    /// Wisp is set to use the oMLX app this Mac already has (a fresh install picks it
    /// when oMLX.app is installed), at least one role runs on it, and it isn't ready
    /// yet. `nil`: not that case (the built-in engine, no oMLX app, every role on another
    /// app, or a running oMLX without any of Wisp's models — the detailed setup).
    enum ExistingOmlx: Equatable {
        /// Its server hasn't answered yet: it is starting (Wisp's last quit stops it),
        /// or busy loading a model. A connection in progress, never a failure.
        case connecting
        /// Running and serving `model`, safe to switch to without asking: the model Wisp
        /// recommends (or, with nothing to download, a tested one), every role is local
        /// and none already names an installed model, so no choice is overwritten.
        case serving(model: String, label: String)
        /// Running and serving `model`, but switching would replace a choice (or the
        /// model isn't Wisp's recommended one): the person decides with one click.
        case offering(model: String, label: String)
    }

    /// Setup is on the person's own oMLX app: not the built-in engine, oMLX.app is
    /// installed, Wisp can't answer yet, and at least one role runs locally. Whether
    /// its model list has filled in is a separate question (`existingOmlx`).
    var usesOwnOmlx: Bool {
        omlx.bundled != true && omlx.installed && !ready && roles.contains { $0.endpoint == "local" }
    }

    var existingOmlx: ExistingOmlx? {
        let local = roles.filter { $0.endpoint == "local" }
        guard usesOwnOmlx else { return nil }
        guard omlx.running else { return .connecting }
        guard let model = models.use,
              let entry = models.installed.first(where: { $0.id == model }) else { return nil }
        let installed = Set(models.installed.map(\.id))
        let wisps = model == models.recommended || (models.download == nil && entry.tested)
        let nothingOverwritten = local.count == roles.count && !local.contains { installed.contains($0.model) }
        return wisps && nothingOverwritten ? .serving(model: model, label: entry.label)
                                           : .offering(model: model, label: entry.label)
    }
}

// MARK: - Download hold

/// How setup keeps watching a transfer while no setup is on screen, through its
/// "download" host. Pure, so each rule can be checked with a clock.
///
/// - `hold`: the host, the normal 3 s poll, and an idle-sleep assertion. While a
///   transfer is starting or moving, and for `finishingGrace` after one finishes
///   until Wisp can answer.
/// - `watch`: the host with a slow poll (`slowPoll`), and no sleep assertion. When
///   a transfer last seen running has not moved for `stallLimit` (an engine left
///   "downloading" at 0 B/s), or reads have failed `failureLimit` times in a row
///   over at least `failureSpan` (the service or engine is down, not restarting).
///   The slow poll still sees the download finish, so Wisp switches to the model,
///   and goes back to `hold` when the bytes move.
/// - `none`: nothing is transferring, or the hold has lasted `cap`, the backend's
///   own watch (_WATCH_FOR_S). The cap lasts until nothing is transferring, so the
///   same transfer never retakes it.
///
/// Sleep is not a stall or an outage. It is told apart from slow reads by the
/// two clocks: wall time keeps running while the Mac sleeps and uptime pauses,
/// so a gap where wall time ran more than `sleepThreshold` ahead of uptime was
/// sleep. After one, the stall clock and any run of failures start over at the
/// next read, and nothing is judged until a read has happened since. Reads that
/// are merely slow leave both clocks in step, so the stall rule still applies.
struct SetupDownloadHold: Equatable {
    enum Level: Equatable { case none, watch, hold }

    static let finishingGrace: TimeInterval = 120
    static let stallLimit: TimeInterval = 10 * 60
    static let failureLimit = 3
    static let failureSpan: TimeInterval = 90
    static let cap: TimeInterval = 6 * 3600
    static let fastPoll: TimeInterval = 3
    static let slowPoll: TimeInterval = 60
    static let sleepThreshold: TimeInterval = 30

    /// When the host was taken.
    var since: Date?
    /// Download pressed; the request to start it hasn't answered yet.
    var starting = false
    private(set) var level = Level.none
    private(set) var movedAt: Date?
    private(set) var lastReadAt: Date?
    private(set) var lastReadUptime: TimeInterval?
    private(set) var signature: [String] = []
    private(set) var failures = 0
    private(set) var firstFailureAt: Date?
    private(set) var capped = false
    private(set) var finishedAt: Date?

    /// Whether the Mac slept since the last read: wall time ran ahead of uptime.
    func slept(now: Date, uptime: TimeInterval) -> Bool {
        guard let lastReadAt, let lastReadUptime else { return false }
        return now.timeIntervalSince(lastReadAt) - (uptime - lastReadUptime) > Self.sleepThreshold
    }

    /// A read happened, good or not. After sleep, the stall clock and the run of
    /// failures start over.
    private mutating func noteRead(_ now: Date, uptime: TimeInterval) {
        if slept(now: now, uptime: uptime) {
            movedAt = now
            failures = 0
            firstFailureAt = nil
        }
        lastReadAt = now
        lastReadUptime = uptime
    }

    /// A fresh read of the transfers.
    mutating func observe(_ downloads: [SetupGuideModel.DownloadTask],
                          previous: [SetupGuideModel.DownloadTask], now: Date, uptime: TimeInterval) {
        noteRead(now, uptime: uptime)
        failures = 0
        firstFailureAt = nil
        let active = downloads.filter(\.active)
        let next = active.map { "\($0.model):\($0.state):\(Int64($0.downloadedBytes))" }.sorted()
        if next != signature || movedAt == nil {
            signature = next
            movedAt = now
        }
        let wasActive = Set(previous.filter(\.active).map(\.model))
        if downloads.contains(where: { $0.state == "completed" && wasActive.contains($0.model) }) {
            finishedAt = now
        }
        if active.isEmpty { capped = false }
    }

    /// A read that failed: what is on screen is the last snapshot.
    mutating func failed(now: Date, uptime: TimeInterval) {
        noteRead(now, uptime: uptime)
        if failures == 0 { firstFailureAt = now }
        failures += 1
    }

    /// The level now, from the last snapshot. Notes the cap when it is reached.
    mutating func decide(_ downloads: [SetupGuideModel.DownloadTask], ready: Bool,
                         now: Date, uptime: TimeInterval) -> Level {
        level = next(downloads, ready: ready, now: now, uptime: uptime)
        return level
    }

    private mutating func next(_ downloads: [SetupGuideModel.DownloadTask], ready: Bool,
                               now: Date, uptime: TimeInterval) -> Level {
        if let since, now.timeIntervalSince(since) >= Self.cap { capped = true }
        if capped { return .none }
        if starting { return .hold }
        if downloads.contains(where: \.active) {
            // Slept since the last read: nothing to judge by until the next one.
            if slept(now: now, uptime: uptime) { return level == .none ? .hold : level }
            if failures >= Self.failureLimit, let firstFailureAt,
               now.timeIntervalSince(firstFailureAt) >= Self.failureSpan { return .watch }
            guard let movedAt else { return .hold }
            return now.timeIntervalSince(movedAt) < Self.stallLimit ? .hold : .watch
        }
        guard let finishedAt, !ready else { return .none }
        return now.timeIntervalSince(finishedAt) < Self.finishingGrace ? .hold : .none
    }

    /// The host was let go.
    mutating func released() {
        since = nil
        finishedAt = nil
        level = .none
    }
}

// MARK: - Model

@MainActor
final class SetupGuideModel: ObservableObject {
    struct DownloadTask: Decodable, Identifiable {
        let model: String; let state: String; let progress: Double
        let downloadedBytes: Double; let totalBytes: Double; let bytesPerSecond: Double
        let message: String?
        var id: String { model }
        var active: Bool { state == "pending" || state == "downloading" }

        /// "45% · 2.5 GB of 5.5 GB · 10 MB/s", or "Preparing download…" before the
        /// engine knows the size. Shared by the full guide and the notch card.
        var progressText: String {
            guard totalBytes > 0 else { return "Preparing download…" }
            let received = ByteCountFormatter.string(fromByteCount: Int64(downloadedBytes), countStyle: .file)
            let total = ByteCountFormatter.string(fromByteCount: Int64(totalBytes), countStyle: .file)
            return "\(Int(progress))% · \(received) of \(total)" + (speedText.map { " · \($0)" } ?? "")
        }

        /// "10 MB/s" while bytes are arriving; nil when nothing is moving.
        var speedText: String? {
            guard bytesPerSecond > 0 else { return nil }
            return ByteCountFormatter.string(fromByteCount: Int64(bytesPerSecond), countStyle: .file) + "/s"
        }
    }
    private struct Downloads: Decodable { let tasks: [DownloadTask] }

    @Published var status: SetupStatus?
    @Published var loading = false
    @Published var busy: String?
    @Published var message: String?
    @Published var messageIsError = false
    @Published var selectedModel: String?
    @Published var copied: String?
    @Published var testResult: String?
    @Published var testing = false
    @Published private(set) var testPassed = false
    @Published var downloads: [DownloadTask] = []
    @Published var downloadError: String?
    @Published var statusIsCurrent = false
    @Published var refreshError: String?
    private var testGeneration = 0
    // Automatic first-setup steps run at most once per app session each; the
    // visible buttons stay for retries.
    /// Read by the first-run window: a start Wisp made by itself that left nothing
    /// running offers Try again there.
    private(set) var autoStartedEngine = false
    private var autoAppliedModels = Set<String>()
    private var statusGeneration = 0
    private var downloadGeneration = 0
    private var monitoringStopped = false
    private let session: URLSession
    // Where setup is on screen right now: the notch card, the first-run window,
    // Settings › Models › Setup.
    // Polling runs while at least one shows it, and the last to leave stops it
    // and drops the test evidence. Each host says when it shows and hides setup,
    // so nothing depends on a window's title or on SwiftUI tearing a view down.
    // Counted, so a view that appears before its predecessor disappears (SwiftUI
    // doesn't order the two) never stops the poll it still needs.
    @Published private(set) var hosts: [String: Int] = [:]
    /// The host a transfer holds while it runs, so collapsing the notch panel
    /// or closing a window doesn't stop the poll (and with it the automatic
    /// switch to the new model). It is taken only while setup is on screen;
    /// SetupDownloadHold says when it is let go.
    static let downloadHost = "download"
    private(set) var downloadHold = SetupDownloadHold()
    private var keepAwake: NSObjectProtocol?
    /// Whether the idle-sleep assertion is held (only at the `hold` level).
    var keepsMacAwake: Bool { keepAwake != nil }
    /// Only a transfer being watched keeps setup polling, so the poll slows down.
    var pollsSlowly: Bool {
        hosts.keys.allSatisfy { $0 == Self.downloadHost } && downloadHold.level == .watch
    }
    private var monitorTask: Task<Void, Never>?
    private var configurationObserver: AnyCancellable?
    /// Wall time and uptime (which pauses while the Mac sleeps), injectable so
    /// the hold's rules can be checked without waiting.
    private let clock: () -> Date
    private let uptime: () -> TimeInterval
    /// False only in checks that drive every read themselves: showing setup then
    /// starts no background poll.
    private let pollsByItself: Bool

    init(session: URLSession = .shared, clock: @escaping () -> Date = Date.init,
         uptime: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
         pollsByItself: Bool = true) {
        self.session = session
        self.clock = clock
        self.uptime = uptime
        self.pollsByItself = pollsByItself
        configurationObserver = NotificationCenter.default
            .publisher(for: .wispInferenceConfigurationChanged)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in MainActor.assumeIsolated { self?.configurationChanged() } }
    }

    var isShown: Bool { !hosts.isEmpty }

    /// A host started showing setup. The first one starts the poll.
    func show(in host: String) {
        let wasShown = isShown
        hosts[host, default: 0] += 1
        guard !wasShown else { return }
        monitorTask?.cancel()
        guard pollsByItself else { monitoringStopped = false; return }
        monitorTask = Task { [weak self] in await self?.monitor() }
    }

    /// A host stopped showing setup. The last one stops the poll and clears the
    /// test, exactly as closing the old setup window always did.
    func hide(from host: String) {
        guard let count = hosts[host] else { return }
        hosts[host] = count > 1 ? count - 1 : nil
        guard !isShown else { return }
        monitorTask?.cancel()
        monitorTask = nil
        stopMonitoring()
    }

    /// Take, slow down or let go of the download host after each look at the
    /// transfers, successful or not. `previous`: the transfers before a fresh read.
    private func updateDownloadHost(previous: [DownloadTask]? = nil) {
        let now = clock(), up = uptime()
        if let previous { downloadHold.observe(downloads, previous: previous, now: now, uptime: up) }
        let level = downloadHold.decide(downloads, ready: statusIsCurrent && status?.ready == true,
                                        now: now, uptime: up)
        if level != .none, hosts[Self.downloadHost] == nil {
            // Taken only while setup is on screen; otherwise there is nothing to hold.
            guard isShown else { downloadHold.released(); return }
            downloadHold.since = now
            show(in: Self.downloadHost)
        }
        if level == .hold, keepAwake == nil {
            // Idle sleep would pause the transfer; a closed lid still sleeps.
            keepAwake = ProcessInfo.processInfo.beginActivity(
                options: [.idleSystemSleepDisabled], reason: "Downloading Wisp's model")
        } else if level != .hold, let keepAwake {
            ProcessInfo.processInfo.endActivity(keepAwake)
            self.keepAwake = nil
        }
        if level == .none, hosts[Self.downloadHost] != nil {
            downloadHold.released()
            hide(from: Self.downloadHost)
        }
    }

    /// Something outside setup changed which models or engine Wisp uses. What is
    /// on screen no longer describes it: drop the test and the current status,
    /// fence any request in flight, and re-read if setup is showing.
    func configurationChanged() {
        beginMutation()
        if isShown, busy == nil { Task { await refresh(quiet: true) } }
    }

    var readinessLabel: String {
        guard statusIsCurrent, status?.ready == true else { return "Needs attention" }
        return testPassed ? "Test passed" : "Ready to test"
    }

    private func invalidateTest() {
        testGeneration += 1
        testResult = nil
        testPassed = false
        testing = false
    }

    private func beginMutation() {
        statusGeneration += 1
        downloadGeneration += 1
        statusIsCurrent = false
        loading = false
        invalidateTest()
    }

    private func configuration(_ status: SetupStatus?) -> [String] {
        guard let status else { return [] }
        return [status.omlx.adminUrl, String(status.omlx.bundled ?? false)]
            + status.roles.map { "\($0.role):\($0.endpoint):\($0.model)" }.sorted()
    }

    private func accept(_ next: SetupStatus) {
        if next.omlx.adminUrl != status?.omlx.adminUrl || next.omlx.bundled != status?.omlx.bundled {
            downloadGeneration += 1
            downloads = []
            downloadError = nil
        }
        if !next.ready || configuration(next) != configuration(status) { invalidateTest() }
        status = next
        statusIsCurrent = true
        if selectedModel == nil || !next.models.installed.contains(where: { $0.id == selectedModel }) {
            selectedModel = next.models.use ?? next.models.installed.first?.id
        }
        autoAdvanceFirstSetup(next)
        updateDownloadHost()
    }

    #if WISP_NOTCH_QA || WISP_CHAT_QA || WISP_SETTINGS_QA
    /// Inject a supplied status into the actual accept/queue path without a
    /// polling host or an app delegate. Production cannot call this seam.
    func qaAcceptAutomaticStatus(_ next: SetupStatus) {
        hosts["release-interaction-fixture"] = 1
        monitoringStopped = false
        accept(next)
    }
    #endif

    /// First setup should not stall on steps whose only choice is "yes".
    private func autoAdvanceFirstSetup(_ next: SetupStatus) {
        guard busy == nil, isShown, !monitoringStopped else { return }
        // The built-in engine is ours: start it once if it is present but idle.
        if next.omlx.bundled == true, !next.omlx.running, !autoStartedEngine,
           next.checks.contains(where: { $0.id == "engine" && $0.action == "start_engine" }) {
            autoStartedEngine = true
            Task { await startEngine() }
            return
        }
        // A model was just installed and nothing points at one: use it. Only when
        // no local role already names an installed model, so a deliberate choice
        // is never overwritten.
        // Only the model setup itself recommends (ranked from the catalog), never
        // whichever installed model happens to be first.
        guard next.omlx.running, let id = next.models.use, id == selectedModel, !autoAppliedModels.contains(id),
              let expected = next.configurationToken, !expected.isEmpty,
              next.checks.contains(where: { $0.id == "model" && $0.action == "apply_model" }) else { return }
        let installed = Set(next.models.installed.map(\.id))
        let local = next.roles.filter { $0.endpoint == "local" }
        guard !local.isEmpty, !local.contains(where: { installed.contains($0.model) }) else { return }
        // Applying sets every text role, so a role pointed at another app
        // (LM Studio, Ollama) on purpose would be repointed: leave that to the person.
        guard local.count == next.roles.count else { return }
        autoAppliedModels.insert(id)
        let generation = statusGeneration
        Task { [weak self] in
            guard let self, self.statusGeneration == generation, self.statusIsCurrent,
                  self.isShown, !self.monitoringStopped, !Task.isCancelled,
                  self.selectedModel == id else { return }
            await self.apply(id, expectedConfiguration: expected)
        }
    }
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
        let (data, response) = try await session.data(for: request)
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
        guard !monitoringStopped else { return }
        statusGeneration += 1
        let generation = statusGeneration
        loading = true
        defer { if generation == statusGeneration { loading = false } }
        var lastError: Error?
        // Wait for a starting backend only on the first load; a refresh of what is
        // already on screen tries once and says so if it fails.
        for attempt in 0..<(status == nil ? 6 : 1) {
            guard !monitoringStopped, !Task.isCancelled else { return }
            do {
                let next = try decode(try await call("GET", "setup/status"))
                guard generation == statusGeneration, !monitoringStopped, !Task.isCancelled else { return }
                accept(next)
                refreshError = nil
                if !quiet {
                    message = nil
                    messageIsError = false
                }
                return
            } catch {
                guard generation == statusGeneration, !monitoringStopped, !Task.isCancelled else { return }
                lastError = error
                do { try await Task.sleep(nanoseconds: UInt64(500_000_000 * (attempt + 1))) }
                catch { return }
            }
        }
        if let lastError, generation == statusGeneration, !monitoringStopped {
            statusIsCurrent = false
            invalidateTest()
            refreshError = status == nil
                ? "Wisp's service isn't answering yet. Give it a few seconds, then choose Check again. (\(lastError.localizedDescription))"
                : "Wisp couldn't refresh this list, so it is showing what it saw last. (\(lastError.localizedDescription))"
        }
    }

    /// First-run setup only (FirstRunSetupView drives it while setup is unfinished):
    /// the person's own oMLX app hasn't answered (Wisp's last quit stops its server),
    /// so start it once a session. A plain start — the service never restarts or
    /// reconfigures their app for a start nobody clicked. Never when no role uses it.
    func startExistingOmlxOnce() {
        guard busy == nil, !autoStartedEngine, statusIsCurrent, let status,
              status.existingOmlx == .connecting,
              status.checks.contains(where: { $0.id == "engine" && $0.action == "start_engine" }) else { return }
        autoStartedEngine = true
        Task { await startEngine(automatic: true) }
    }

    /// `automatic`: Wisp started it by itself, so the person's own oMLX gets a plain
    /// start and never the restart a click may fall back to.
    func startEngine(automatic: Bool = false) async {
        guard busy == nil else { return }
        beginMutation()
        busy = "Starting engine…"
        defer { busy = nil }
        do {
            if status?.omlx.bundled == true {
                _ = try await call("POST", "engine/start", body: [:], timeout: 160)
                await refresh()
            } else {
                accept(try decode(try await call("POST", "setup/start-engine",
                                                 body: automatic ? ["automatic": true] : [:], timeout: 100)))
                message = nil
            }
        } catch { fail(error) }
    }

    /// Poll only while this guide is visible. The engine owns transfer/resume
    /// state, so closing and reopening setup does not cancel a download.
    func monitor() async {
        // A host that showed and hid before this task ran must not undo its stop.
        guard !Task.isCancelled else { return }
        monitoringStopped = false
        await refresh()
        var lastRead = clock()
        while !Task.isCancelled && !monitoringStopped {
            if status?.omlx.bundled == true && busy == nil { await refreshDownloads() }
            // Wake every 3 s either way, so a setup shown again polls at once; a
            // transfer only being watched is read once a minute.
            repeat {
                do { try await Task.sleep(nanoseconds: 3_000_000_000) }
                catch { return }
                guard !monitoringStopped else { return }
            } while pollsSlowly && clock().timeIntervalSince(lastRead) < SetupDownloadHold.slowPoll
            lastRead = clock()
            if busy == nil { await refresh(quiet: true) }
        }
    }

    func stopMonitoring() {
        monitoringStopped = true
        downloadGeneration += 1
        invalidateTest()
    }

    func shouldOfferRedownload(_ model: String) -> Bool {
        downloads.first(where: { $0.model == model })?.state == "completed"
            && !(status?.models.installed.contains(where: { $0.id == model }) ?? false)
    }

    func refreshDownloads(duringAction: Bool = false) async {
        guard status?.omlx.bundled == true, !monitoringStopped,
              busy == nil || duringAction else { return }
        downloadGeneration += 1
        let generation = downloadGeneration
        do {
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            let next = try decoder.decode(Downloads.self, from: await call("GET", "engine/downloads")).tasks
            guard generation == downloadGeneration, !monitoringStopped, !Task.isCancelled else { return }
            let previous = downloads
            downloads = next
            downloadError = nil
            updateDownloadHost(previous: previous)
        } catch {
            guard generation == downloadGeneration, !monitoringStopped, !Task.isCancelled else { return }
            downloadError = "Wisp couldn't check the download. Choose Check again to retry."
            // The last snapshot is stale now: a service that stays down must not
            // keep the poll and the sleep assertion forever.
            downloadHold.failed(now: clock(), uptime: uptime())
            updateDownloadHost()
        }
    }

    func download(_ model: String, cancel: Bool = false) async {
        guard busy == nil else { return }
        downloadGeneration += 1
        busy = cancel ? "Cancelling download…" : "Starting download…"
        defer { busy = nil }
        // Pressing Download holds the poll at once, so collapsing the panel while
        // the request is in flight (minutes, if the engine is starting too) can't
        // leave the transfer unwatched.
        if !cancel {
            downloadHold.starting = true
            updateDownloadHost()
        }
        defer {
            if !cancel {
                downloadHold.starting = false
                updateDownloadHost()
            }
        }
        do {
            _ = try await call("POST", cancel ? "engine/downloads/cancel" : "engine/downloads",
                               body: ["model": model], timeout: 180)
            await refreshDownloads(duringAction: true)
            await refresh(quiet: true)
            message = nil
        } catch { fail(error) }
    }

    func apply(_ model: String, expectedConfiguration: String? = nil) async {
        guard busy == nil else { return }
        beginMutation()
        let generation = statusGeneration
        busy = "Applying…"
        defer { busy = nil }
        do {
            let repointsReasoning = status?.roles.contains { $0.endpoint != "local" } ?? false
            var body: [String: Any] = ["model": model]
            if let expectedConfiguration {
                body["automatic"] = true
                body["expected_configuration"] = expectedConfiguration
            }
            let next = try decode(try await call("POST", "setup/apply", body: body))
            guard generation == statusGeneration, !monitoringStopped, !Task.isCancelled else { return }
            accept(next)
            message = "Wisp will use \(model) for chat, tools, coding, and reasoning."
                + (repointsReasoning ? " Reasoning had been pointed at another app; it now uses this model too." : "")
            messageIsError = false
        } catch {
            guard generation == statusGeneration, !monitoringStopped, !Task.isCancelled else { return }
            fail(error)
        }
    }

    func connectExternal(port: Int, model: String, withTools: Bool = false) async {
        guard busy == nil else { return }
        beginMutation()
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
        guard !testing, !monitoringStopped, !Task.isCancelled, statusIsCurrent, status?.ready == true else { return }
        testing = true
        testResult = nil
        testPassed = false
        let generation = testGeneration
        defer { if generation == testGeneration { testing = false } }
        let started = Date()
        do {
            let data = try await call("POST", "chat", body: [
                "role": "general", "prompt": "Reply with the single word OK.", "max_tokens": 512,
                "setup_test": true,
            ], timeout: 120)
            let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            guard generation == testGeneration, !Task.isCancelled else { return }
            let text = (object?["content"] as? String ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard object?["setup_test"] as? String == "complete_visible_assistant_v1" else {
                testResult = "Wisp couldn't verify this test reply. Restart Wisp to use the matching backend, then try again."
                return
            }
            let seconds = String(format: "%.1f", Date().timeIntervalSince(started))
            if text.isEmpty || text.lowercased().contains("<think") || text.lowercased().contains("</think") {
                testResult = "The model replied but gave no visible answer. It may be spending its whole budget thinking; try again or pick another model."
            } else {
                testResult = "Works — replied in \(seconds)s."
                testPassed = true
            }
        } catch {
            if generation == testGeneration, !Task.isCancelled { testResult = "That didn't work: \(error.localizedDescription)" }
        }
    }

    /// Every local text role already uses this model.
    func inUse(_ id: String) -> Bool {
        let local = status?.roles.filter { $0.endpoint == "local" } ?? []
        return !local.isEmpty && local.allSatisfy { $0.model == id }
    }

    /// The notch card's one button: point the text roles at the selected model
    /// unless they already use it, then send the test message.
    func useAndTest() async {
        guard let id = selectedModel, busy == nil, !testing else { return }
        if !inUse(id) {
            await apply(id)
            guard !messageIsError else { return }
        }
        await test()
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

/// The full guide. Settings › Models › Setup in the Wisp window shows its
/// `header` and `pageContent` (SettingsSetupPane); `body` is the standalone
/// form. It shares the app's one SetupGuideModel with the notch's setup card,
/// and its host (AppDelegate) calls `show(in:)` and `hide(from:)` as it comes
/// and goes. Its colours come from `\.themePalette`: black and white unless
/// the host supplies another.
struct SetupGuideView: View {
    @ObservedObject private var model: SetupGuideModel
    @Environment(\.themePalette) private var environmentPalette
    /// Set by a host that draws `header` and `pageContent` itself (SettingsSetupPane):
    /// those are read off a value SwiftUI never installs, so its environment can't reach them.
    private let paletteOverride: ThemePalette?
    private var palette: ThemePalette { paletteOverride ?? environmentPalette }
    var onDone: () -> Void
    var openSettings: () -> Void
    private let height: CGFloat
    /// The margin around the header and the page: 24 in the standalone form,
    /// 0 where the host already pads its column.
    private let inset: CGFloat

    init(model: SetupGuideModel, height: CGFloat = 700, inset: CGFloat = 24, palette: ThemePalette? = nil,
         onDone: @escaping () -> Void = {}, openSettings: @escaping () -> Void = {}) {
        self.model = model
        self.height = height
        self.inset = inset
        self.paletteOverride = palette
        self.onDone = onDone
        self.openSettings = openSettings
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().overlay(palette.hairline)
            ScrollView { pageContent }
            Divider().overlay(palette.hairline)
            footer
        }
        .frame(width: 640, height: height)
        .background(palette.surface)
        .foregroundStyle(palette.textPrimary)
        .preferredColorScheme(.dark)
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
                    Text("Checking this Mac…").foregroundStyle(palette.textSecondary) }
            }
            if let text = model.message {
                Text(text).font(.callout)
                    .foregroundStyle(model.messageIsError ? Color.orange : palette.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error = model.refreshError {
                Text(error).font(.callout).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(inset)
    }

    // MARK: Header / footer

    var header: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Set up Wisp's AI engine").font(.system(size: 19, weight: .semibold))
                if let s = model.status {
                    Text("\(s.hardware.chip) · \(s.hardware.ramGb > 0 ? "\(s.hardware.ramGb) GB memory" : "memory unknown")")
                        .font(.callout).foregroundStyle(palette.textSecondary)
                } else {
                    Text("Everything runs on this Mac.").font(.callout).foregroundStyle(palette.textSecondary)
                }
            }
            Spacer()
            if let s = model.status {
                pill(model.readinessLabel,
                     on: s.ready && model.statusIsCurrent)
            }
        }
        .padding(.horizontal, inset).padding(.vertical, inset * 0.75)
    }

    var footer: some View {
        HStack {
            if let busy = model.busy {
                ProgressView().controlSize(.small)
                Text(busy).font(.caption).foregroundStyle(palette.textSecondary)
            }
            Spacer()
            Button("Check again") { Task { await model.refresh(); await model.refreshDownloads() } }
                .disabled(model.busy != nil)
            Button("Settings…") { openSettings() }
            Button("Done") { onDone() }.keyboardShortcut(.defaultAction)
        }
        .padding(.horizontal, 24).padding(.vertical, 14)
    }

    // MARK: 1 · Engine

    private func engineSection(_ s: SetupStatus) -> some View {
        let check = s.checks.first { $0.id == "engine" }
        return card(step: "1", title: "Engine", state: check?.state ?? "todo") {
            if let bundled = s.bundledEngine, !bundled.available,
               let reason = bundled.unavailableReason {
                Text(reason).font(.callout).foregroundStyle(palette.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                Text("You can use a compatible external engine. Macs that cannot run local models can use Wisp's separately configured remote-only mode.")
                    .font(.caption).foregroundStyle(palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let check {
                Text(check.detail).font(.callout).foregroundStyle(palette.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack {
                    if check.action == "unsupported_macos" {
                        Text("Connect another app under Other engines below, or use remote-only mode in Settings.")
                            .font(.caption).foregroundStyle(palette.textMuted)
                    } else if check.action == "install_omlx" {
                        action("Get oMLX") { model.open(check.url) }
                        Text("Then open it once and choose Check again.")
                            .font(.caption).foregroundStyle(palette.textMuted)
                    } else if check.action == "start_engine" {
                        action(s.omlx.bundled == true ? "Start engine" : "Start oMLX") {
                            Task { await model.startEngine() }
                        }
                        .disabled(model.busy != nil)
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
            // The no-fit check repeats the memory warning just below; say it once.
            if let check, check.state != "ok", check.detail != s.models.warning {
                Text(check.detail).font(.callout).foregroundStyle(Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let warning = s.models.warning {
                Text(warning).font(.caption).foregroundStyle(Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if s.models.installed.isEmpty {
                Text(!s.omlx.running ? "Start the engine above to see your models."
                     : s.models.fits == false ? "Connect a model you run in another app under Other local apps below."
                     : s.models.download == nil ? "Add a local model to the engine's model folder to get started."
                     : "Download a model below to get started.")
                    .font(.callout).foregroundStyle(palette.textSecondary)
            } else {
                VStack(spacing: 6) {
                    ForEach(s.models.installed) { entry in modelRow(entry, current: currentModels(s)) }
                }
                HStack {
                    action("Use this model") {
                        if let id = model.selectedModel { Task { await model.apply(id) } }
                    }
                    .disabled(model.selectedModel == nil || model.busy != nil || model.testing || !s.omlx.running || !model.statusIsCurrent)
                    Text("Sets chat, tools, coding, and reasoning.")
                        .font(.caption).foregroundStyle(palette.textMuted)
                }
            }
            if let download = s.models.download {
                downloadCard(download, headline: s.models.use == nil
                             ? "Recommended for this Mac" : "A better fit for this Mac", tag: "main",
                             omlx: s.omlx)
            }
            if check?.action == "add_local_model" {
                Text("This recommended model has no public download. Add its folder (\(recommendedName(s))) to your engine's model directory, then choose Check again and Use this model.")
                    .font(.caption).foregroundStyle(palette.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let folder = s.omlx.modelDir {
                    Text(folder).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                }
                action("Open engine settings") { model.open(s.omlx.adminUrl) }
            }
        }
    }

    private func recommendedName(_ s: SetupStatus) -> String {
        s.models.recommendedLabel ?? s.models.recommended
    }

    private func recommendationText(_ s: SetupStatus) -> some View {
        let name = recommendedName(s)
        if s.models.fits == false {
            return Text("None of the models Wisp can download fits this Mac.").font(.callout)
                .foregroundStyle(palette.textSecondary).fixedSize(horizontal: false, vertical: true)
        }
        let text = s.models.installed.contains(where: { $0.id == s.models.recommended })
            ? "\(name) is the best fit for this Mac, and it is installed."
            : "Recommended for this Mac (\(s.hardware.ramGb > 0 ? "\(s.hardware.ramGb) GB" : "memory unknown")): \(name). Wisp also works with the models you already have."
        return Text(text).font(.callout).foregroundStyle(palette.textSecondary)
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
                    .foregroundStyle(selected ? palette.textPrimary : palette.textMuted)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(entry.label).font(.system(size: 13, weight: .medium))
                        if let gb = entry.sizeGb {
                            Text(String(format: "%.1f GB", gb)).font(.caption).foregroundStyle(palette.textMuted)
                        }
                        if current.contains(entry.id) { pill("In use", on: true) }
                        fitBadge(entry.fit)
                        if !entry.tested && entry.fit != "unknown" { pill("Not validated", on: false) }
                    }
                    Text(entry.note).font(.caption).foregroundStyle(palette.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 10)
                .fill(selected ? palette.selectedFill : palette.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 10)
                .stroke(selected ? palette.selectedStroke : palette.chipStroke, lineWidth: 1))
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
            Text(check?.detail ?? "").font(.callout).foregroundStyle(palette.textSecondary)
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
                    .font(.caption).foregroundStyle(palette.textSecondary)
            }
            if omlx.bundled == true {
                Text("Download from Hugging Face directly into Wisp. The engine handles progress and resumes interrupted transfers.")
                    .font(.caption).foregroundStyle(palette.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let task = model.downloads.first(where: { $0.model == d.model }) {
                    if task.active {
                        ProgressView(value: task.progress, total: 100)
                        HStack {
                            Text(task.progressText).font(.caption).foregroundStyle(palette.textSecondary)
                            Spacer()
                            action("Cancel") { Task { await model.download(d.model, cancel: true) } }
                                .disabled(model.busy != nil)
                        }
                    } else if task.state == "failed" || task.state == "cancelled" {
                        Text(task.message ?? "Download cancelled. You can resume it here.")
                            .font(.caption).foregroundStyle(.orange)
                        action("Retry download") { Task { await model.download(d.model) } }
                            .disabled(model.busy != nil)
                    } else {
                        Text("Download finished. Choose the installed model above, then Use this model.")
                            .font(.caption).foregroundStyle(palette.textSecondary)
                        if model.shouldOfferRedownload(d.model) {
                            Text("The model hasn't appeared yet. If its files are missing, download it again.")
                                .font(.caption).foregroundStyle(palette.textSecondary)
                            action("Download again") { Task { await model.download(d.model) } }
                                .disabled(model.busy != nil)
                        }
                    }
                } else {
                    action("Download model") { Task { await model.download(d.model) } }
                        .disabled(model.busy != nil)
                }
                if let error = model.downloadError {
                    Text(error).font(.caption).foregroundStyle(.orange)
                }
            } else if omlx.running {
                Text("Wisp doesn't download models for you. The easiest way is oMLX's Model Downloader: "
                     + "open it, search for the name below, and download it.")
                    .font(.caption).foregroundStyle(palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                HStack {
                    action("Open oMLX downloader") { model.open(omlx.adminUrl) }
                    action(model.copied == tag + "-repo" ? "Copied" : "Copy \(d.repo)") {
                        model.copy(d.repo, tag: tag + "-repo")
                    }
                    Spacer()
                }
                Text("Or run this in Terminal:").font(.caption).foregroundStyle(palette.textMuted)
            } else {
                Text("Wisp doesn't download models for you. Once oMLX is running you can use its Model Downloader, "
                     + "or run this in Terminal:")
                    .font(.caption).foregroundStyle(palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if omlx.bundled != true {
                Text(d.command)
                    .font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                    .padding(8).frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 8).fill(palette.codeFill))
                HStack {
                    action(model.copied == tag ? "Copied" : "Copy command") { model.copy(d.command, tag: tag) }
                    action("Open on Hugging Face") { model.open(d.url) }
                    Spacer()
                }
                Text("The Terminal route needs the `hf` tool: \(d.installHf). Then choose Check again.")
                    .font(.caption2).foregroundStyle(palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(palette.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(palette.chipStroke, lineWidth: 1))
    }

    // MARK: 4 · Other engines

    private func otherEnginesSection(_ s: SetupStatus) -> some View {
        card(step: "4", title: "Other local apps (optional)", state: "info") {
            Text("Ollama, LM Studio, llama.cpp, MTPLX and similar apps on this Mac can answer chat and reasoning. "
                 + "Choose “Connect with tools” and Wisp tests whether the app can run its tools reliably, "
                 + "including the context window it really uses, before turning tool use on.")
                .font(.callout).foregroundStyle(palette.textSecondary)
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
                if let port = e.port { Text(":" + String(port)).font(.caption).foregroundStyle(palette.textMuted) }
                pill(e.running ? "Running" : "Not detected", on: e.running)
                Spacer()
                if let url = e.url, !e.running { action("Website") { model.open(url) } }
            }
            Text(e.summary).font(.caption).foregroundStyle(palette.textSecondary)
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
                Text(e.howToStart).font(.caption2).foregroundStyle(palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !e.running {
                Text(e.howToStart).font(.caption2).foregroundStyle(palette.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(palette.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(palette.chipStroke, lineWidth: 1))
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
                    .disabled(model.testing || model.busy != nil || !s.ready || !model.statusIsCurrent)
                if let result = model.testResult {
                    Text(result).font(.callout).foregroundStyle(palette.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                } else if !s.ready {
                    Text("Finish steps 1 and 2 first.").font(.caption).foregroundStyle(palette.textMuted)
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
        .background(RoundedRectangle(cornerRadius: 14).fill(palette.cardFill))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(palette.hairline, lineWidth: 1))
    }

    @ViewBuilder
    private func stateGlyph(_ state: String, step: String) -> some View {
        switch state {
        case "ok":
            Image(systemName: "checkmark.circle.fill").foregroundStyle(palette.textPrimary)
        case "warn":
            Image(systemName: "exclamationmark.circle").foregroundStyle(Color.orange)
        case "todo":
            Image(systemName: "\(step).circle").foregroundStyle(palette.textPrimary)
        default:
            Image(systemName: "\(step).circle").foregroundStyle(palette.textMuted)
        }
    }

    private func pill(_ text: String, on: Bool) -> some View {
        Text(text).font(.system(size: 10, weight: .semibold))
            .foregroundStyle(on ? palette.onAccent : palette.textSecondary)
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(Capsule().fill(on ? palette.accent : palette.chipFill))
            .overlay(Capsule().stroke(on ? Color.clear : palette.chipStroke, lineWidth: 1))
    }

    private func action(_ title: String, perform: @escaping () -> Void) -> some View {
        Button(action: perform) {
            Text(title).font(.system(size: 12, weight: .medium))
                .padding(.horizontal, 10).padding(.vertical, 5)
                .background(Capsule().fill(palette.actionFill))
                .overlay(Capsule().stroke(palette.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
    }
}
