import AppKit
import Darwin
import Foundation

/// Actual production interaction seams, supplied completions and recording
/// actions only. Never creates an app, a default OverlayModel or a live client.
@main
enum ReleaseInteractionChecks {
    struct Failure: Error, CustomStringConvertible { let description: String }

    @MainActor static func main() async throws {
        if CommandLine.arguments.contains("--print-real-home") {
            print(FileManager.default.homeDirectoryForCurrentUser.path)
            return
        }
        try isolationChecks()
        try await attachmentChecks()
        try researchChecks()
        try await setupChecks()
        print("Release interactions: isolation, delayed attachments, job-bound confirmation and queued setup passed")
    }

    static func check(_ condition: Bool, _ message: String) throws {
        if !condition { throw Failure(description: message) }
    }

    static func isolationChecks() throws {
        let env = ProcessInfo.processInfo.environment
        guard let root = env["WISP_RELEASE_INTERACTIONS_ROOT"], root.hasPrefix("/private/tmp/wisp-release-interactions-"),
              let denied = env["WISP_RELEASE_INTERACTIONS_DENIED"], denied.hasPrefix(root + "/denied/"),
              let realHome = env["WISP_RELEASE_INTERACTIONS_REAL_HOME"], realHome.hasPrefix("/Users/") else {
            throw Failure(description: "Missing owned fixture and real-home denial configuration")
        }
        do {
            _ = try Data(contentsOf: URL(fileURLWithPath: denied))
            throw Failure(description: "Synthetic denied-file control was readable")
        } catch let failure as Failure { throw failure }
        catch {
            let error = error as NSError
            try check((error.domain == NSCocoaErrorDomain && error.code == CocoaError.fileReadNoPermission.rawValue)
                || (error.domain == NSPOSIXErrorDomain && [EPERM, EACCES].contains(Int32(error.code))),
                "Denied fixture must fail with permission denial")
        }
        let control = URL(fileURLWithPath: root).appendingPathComponent("positive-control")
        let data = Data("synthetic fixture only".utf8)
        try data.write(to: control)
        try check(try Data(contentsOf: control) == data, "Owned fixture must remain readable/writable")
        // A nonexistent random path tests the real-home policy without reading
        // an existing user file or creating anything if the sandbox is absent.
        let fd = open(realHome + "/.wisp-release-denial-" + UUID().uuidString, O_RDONLY)
        let homeError = errno
        if fd >= 0 { close(fd); throw Failure(description: "Real-home read denial is absent") }
        try check([EPERM, EACCES].contains(homeError), "Real-home reads must be denied")
        let socketFD = socket(AF_INET, SOCK_STREAM, 0)
        if socketFD >= 0 {
            defer { close(socketFD) }
            var address = sockaddr_in()
            address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
            address.sin_family = sa_family_t(AF_INET)
            address.sin_port = UInt16(1).bigEndian
            address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
            let result = withUnsafePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    connect(socketFD, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
            try check(result == -1 && [EPERM, EACCES].contains(errno), "OS network denial must be active")
        } else {
            try check([EPERM, EACCES].contains(errno), "Socket creation must fail only by policy")
        }
    }

    actor DeferredImage {
        private var completion: CheckedContinuation<String?, Never>?
        private var observers: [CheckedContinuation<Void, Never>] = []
        func read() async -> String? {
            await withCheckedContinuation { completion in
                self.completion = completion
                observers.forEach { $0.resume() }; observers = []
            }
        }
        func started() async {
            if completion != nil { return }
            await withCheckedContinuation { observers.append($0) }
        }
        func finish(_ value: String?) {
            guard let completion else { fatalError("Fixture read was not started") }
            self.completion = nil
            completion.resume(returning: value)
        }
    }

    @MainActor static func attachmentChecks() async throws {
        var debugWrites: [Bool] = []
        var recentReads = 0
        func model() -> OverlayModel {
            OverlayModel(loadDebugMode: { false }, saveDebugMode: { debugWrites.append($0) },
                         recentChatsLoader: { _ in recentReads += 1; return [] })
        }
        let positive = model()
        positive.debugMode = true
        try check(debugWrites == [true], "Debug persistence must use the supplied fixture")
        let normal = positive.loadAttachment(name: "normal.png") { "data:image/png;base64,Zml4dHVyZQ==" }
        await normal.value
        try check(positive.attachedImageName == "normal.png", "Ordinary supplied image completion must attach")

        for action in ["reset", "new-chat", "submit", "remove", "replacement"] {
            let current = model(), gate = DeferredImage()
            let pending = current.loadAttachment(name: "late-A.png") { await gate.read() }
            await gate.started()
            var researchStarts = 0
            switch action {
            case "reset": current.reset()
            case "new-chat": current.newChat()
            case "submit":
                current.researchMode = true
                current.onStartResearch = { _ in researchStarts += 1 }
                current.input = "Synthetic research fixture"
                current.submit()
                try check(researchStarts == 1, "Consumption must reach actual submit dispatch with an inert action")
            case "remove": current.removeAttachment()
            default: current.attach(name: "replacement-B.png", dataURL: "data:image/png;base64,Qg==")
            }
            // Intentionally ignores cancellation, like a filesystem completion
            // already in flight. The actual model's ownership check must win.
            await gate.finish("data:image/png;base64,QQ==")
            await pending.value
            try check(current.attachedImageName == (action == "replacement" ? "replacement-B.png" : nil),
                      "Late completion survived " + action)
            current.reset()
        }
        let current = model(), first = DeferredImage(), second = DeferredImage()
        let a = current.loadAttachment(name: "A.png") { await first.read() }
        await first.started()
        let b = current.loadAttachment(name: "B.png") { await second.read() }
        await second.started()
        await second.finish("data:image/png;base64,Qg=="); await b.value
        await first.finish("data:image/png;base64,QQ=="); await a.value
        try check(current.attachedImageName == "B.png", "A must not overwrite the newer completed B selection")
        current.removeAttachment()
        try check(current.attachedImageName == nil, "Removal must clear a completed selection")
        for _ in 0..<100 where recentReads == 0 { await Task.yield() }
        try check(recentReads > 0, "New chat must use the injected recent-chat positive control")
    }

    static func researchChecks() throws {
        var state = ResearchDeleteConfirmation(), deleted: [String] = []
        state.arm(jobID: "A")
        try check(!state.confirm(jobID: "B", delete: { deleted.append($0) }), "A confirmation cannot delete B")
        try check(deleted.isEmpty, "Selection switch must perform no deletion")
        state.arm(jobID: "B")
        try check(state.confirm(jobID: "B", delete: { deleted.append($0) }), "Same-job second tap must confirm")
        try check(deleted == ["B"], "Only B's valid two-click action may dispatch")
        try check(!state.confirm(jobID: "B", delete: { deleted.append($0) }), "Confirmation must be consumed")
        state.arm(jobID: "A"); let oldGeneration = state.generation
        state.arm(jobID: "B"); state.expire(generation: oldGeneration)
        try check(state.isArmed(for: "B"), "A's timeout must not clear a newer B arm")
        state.expire(generation: state.generation)
        try check(!state.isArmed(for: "B"), "Current timeout must clear its own arm")
        state.arm(jobID: "A"); state.clear()
        try check(!state.confirm(jobID: "A", delete: { deleted.append($0) }), "Selection/disappearance reset must disarm")
        state.arm(jobID: "")
        try check(!state.isArmed(for: ""), "Empty job cannot be armed")
    }

    final class SetupTransport: URLProtocol {
        private static let lock = NSLock()
        private static var captured: [[String: Any]] = []
        static var bodies: [[String: Any]] { lock.lock(); defer { lock.unlock() }; return captured }
        static func clear() { lock.lock(); captured = []; lock.unlock() }
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            guard let url = request.url, url.path == "/setup/status" || url.path == "/setup/apply" else {
                fatalError("Unexpected fixture request")
            }
            if url.path == "/setup/apply" {
                var payload = request.httpBody
                if payload == nil, let stream = request.httpBodyStream {
                    stream.open(); defer { stream.close() }
                    var bytes = [UInt8](repeating: 0, count: 1024), data = Data()
                    while stream.hasBytesAvailable {
                        let count = stream.read(&bytes, maxLength: bytes.count)
                        if count <= 0 { break }
                        data.append(contentsOf: bytes.prefix(count))
                    }
                    payload = data
                }
                let body = payload.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
                Self.lock.lock(); Self.captured.append(body); Self.lock.unlock()
            }
            let data = try! JSONSerialization.data(withJSONObject: snapshot(needsApply: false))
            let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil,
                                           headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
        override func stopLoading() {}
    }

    static func snapshot(needsApply: Bool) -> [String: Any] {
        ["ready": !needsApply, "configuration_token": "fixture-configuration",
         "hardware": ["chip": "Synthetic", "ram_gb": 16, "tier": "standard"],
         "omlx": ["installed": true, "running": true, "bundled": true, "min_macos": 15,
                  "url": "https://example.invalid", "admin_url": "http://127.0.0.1:1/admin"],
         "models": ["source": "live", "recommended": "fixture-model", "use": "fixture-model", "installed": [
            ["id": "fixture-model", "label": "Fixture", "fit": "unknown", "tested": false, "note": "Synthetic"]]],
         "roles": ["fast", "general", "coding", "reasoning"].map {
             ["role": $0, "label": $0, "endpoint": "local", "model": needsApply ? "missing" : "fixture-model"] },
         "engines": [], "checks": needsApply ? [["id": "model", "state": "todo", "title": "Use model",
                                                    "detail": "Synthetic", "action": "apply_model"]] : [],
         "disclosure": "Synthetic only"]
    }

    @MainActor static func setupChecks() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [SetupTransport.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let next = try decoder.decode(SetupStatus.self, from: JSONSerialization.data(withJSONObject: snapshot(needsApply: true)))
        SetupTransport.clear()
        let stale = SetupGuideModel(session: session)
        stale.qaAcceptAutomaticStatus(next)
        stale.configurationChanged() // synchronous actor mutation before the queued apply can run
        try await wait { stale.statusIsCurrent }
        try check(SetupTransport.bodies.isEmpty, "Queued stale automatic apply must never reach the transport")
        stale.stopMonitoring()
        let valid = SetupGuideModel(session: session)
        valid.qaAcceptAutomaticStatus(next)
        try await wait { !SetupTransport.bodies.isEmpty && valid.busy == nil }
        try check(SetupTransport.bodies.count == 1, "Unchanged first-setup configuration must apply once")
        let body = SetupTransport.bodies[0]
        try check(body["automatic"] as? Bool == true && body["expected_configuration"] as? String == "fixture-configuration",
                  "Automatic apply must carry the server precondition")
        valid.stopMonitoring()
        SetupTransport.clear()
        let manual = SetupGuideModel(session: session)
        await manual.apply("fixture-model")
        try check(SetupTransport.bodies.count == 1 && SetupTransport.bodies[0]["automatic"] == nil,
                  "Manual apply must remain an explicit override")
    }

    @MainActor static func wait(_ ready: () -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(3))
        while !ready() {
            if ContinuousClock.now >= deadline { throw Failure(description: "Synthetic interaction did not finish") }
            try await Task.sleep(for: .milliseconds(1))
        }
    }
}
