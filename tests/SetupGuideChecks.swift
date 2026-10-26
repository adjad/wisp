import AppKit
import Foundation

// This executable never launches an application. Every HTTP request is
// intercepted below; no socket, native data source or real engine is used.
enum WispClient {
    static let baseURL = URL(string: "http://127.0.0.1:54321")!
}


// Explicit test-transport ordering; no response deadline establishes a race.
final class SetupReplyGate {
    private let lock = NSLock()
    private var arrived = false
    private var released = false
    private var delivery: (() -> Void)?

    var captured: Bool {
        lock.lock(); defer { lock.unlock() }
        return arrived
    }
    func capture(_ next: @escaping () -> Void) {
        lock.lock()
        arrived = true
        let sendNow = released
        if !sendNow { delivery = next }
        lock.unlock()
        if sendNow { DispatchQueue.global().async(execute: next) }
    }
    func release() {
        lock.lock()
        released = true
        let next = delivery
        delivery = nil
        lock.unlock()
        if let next { DispatchQueue.global().async(execute: next) }
    }
}

@MainActor
func waitForSetupRequest(_ condition: () -> Bool) async throws -> Bool {
    let deadline = ProcessInfo.processInfo.systemUptime + 10
    while !condition() {
        if ProcessInfo.processInfo.systemUptime >= deadline { return false }
        try await Task.sleep(nanoseconds: 1_000_000)
    }
    return true
}

final class SetupHTTP: URLProtocol {
    typealias Handler = (URLRequest) -> (Int, [String: Any], Double)
    private static let lock = NSLock()
    private static var handler: Handler = { _ in (500, [:], 0) }
    private static var paths: [String] = []
    private static var replyGates: [String: [SetupReplyGate]] = [:]

    static func configure(holds: [String: [SetupReplyGate]] = [:], _ next: @escaping Handler) {
        lock.lock(); defer { lock.unlock() }
        handler = next
        paths = []
        replyGates = holds
    }
    static func saw(_ path: String) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return paths.contains(path)
    }
    static func count(_ path: String) -> Int {
        lock.lock(); defer { lock.unlock() }
        return paths.filter { $0 == path }.count
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock()
        Self.paths.append(request.url!.path)
        let answer = Self.handler(request)
        let path = request.url!.path
        let gate = Self.replyGates[path]?.isEmpty == false ? Self.replyGates[path]?.removeFirst() : nil
        Self.lock.unlock()
        let response = HTTPURLResponse(url: request.url!, statusCode: answer.0,
                                       httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        let data = try! JSONSerialization.data(withJSONObject: answer.1)
        let deliver = { [self] in
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
        if let gate { gate.capture(deliver) }
        else { DispatchQueue.global().asyncAfter(deadline: .now() + answer.2, execute: deliver) }
    }
    override func stopLoading() {}
}

func setupSnapshot(_ model: String = "A", ready: Bool = true, bundled: Bool = true) -> [String: Any] {
    ["ready": ready,
     "hardware": ["chip": "Synthetic Mac", "ram_gb": 16, "tier": "standard"],
     "omlx": ["installed": true, "running": ready, "bundled": bundled,
              "min_macos": 15, "url": "https://example.invalid", "admin_url": "http://127.0.0.1:8791/admin"],
     "models": ["source": ready ? "live" : "saved", "installed": [
        ["id": model, "label": model, "fit": "unknown", "tested": false, "note": "Synthetic"]],
        "recommended": model, "use": model],
     "roles": ["fast", "general", "coding", "reasoning"].map {
        ["role": $0, "label": $0, "model": model, "endpoint": "local"] },
     "engines": [], "checks": [], "disclosure": "Synthetic fixture"]
}

@main
struct SetupGuideChecks {
    @MainActor static func main() async throws {
        var checks = 0
        var failures = 0
        func check(_ value: @autoclosure () -> Bool, _ message: String) {
            if !value() {
                failures += 1
                FileHandle.standardError.write(Data("FAIL: \(message)\n".utf8))
            }
            checks += 1
        }
        func configure(_ model: String, delayChat: Double = 0, chatGate: SetupReplyGate? = nil) {
            SetupHTTP.configure(holds: chatGate.map { ["/chat": [$0]] } ?? [:]) { request in
                if request.url!.path == "/chat" {
                    return (200, ["content": "OK", "setup_test": "complete_visible_assistant_v1"], delayChat)
                }
                if request.url!.path == "/engine/downloads" { return (200, ["tasks": []], 0) }
                if request.url!.path == "/engine/start" { return (200, ["running": true], 0) }
                if request.url!.path == "/inference/local-provider" { return (200, [:], 0) }
                return (200, setupSnapshot(model), 0)
            }
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [SetupHTTP.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let model = SetupGuideModel(session: session)

        configure("A")
        await model.refresh()
        check(model.readinessLabel == "Ready to test" && !model.testPassed,
              "Model discovery cannot certify a successful model test")
        await model.test()
        check(model.testResult?.hasPrefix("Works") == true, "A should pass its synthetic test")
        check(model.testPassed && model.readinessLabel == "Test passed", "Only an actual complete test can certify success")
        await model.refresh()
        check(model.testResult?.hasPrefix("Works") == true, "An unchanged live configuration may retain evidence")

        configure("B")
        await model.apply("B")
        check(model.testResult == nil, "Applying B must discard A's successful test")
        check(!model.testPassed && model.readinessLabel == "Ready to test", "Configuration changes clear the passed badge")
        check(model.status?.roles.first?.model == "B", "Apply should accept the new role bindings")
        await model.test()
        configure("C")
        await model.refresh()
        check(model.testResult == nil, "A changed role discovered by refresh must discard evidence")
        await model.test()
        SetupHTTP.configure { _ in (200, setupSnapshot("C", ready: false), 0) }
        await model.refresh()
        check(model.testResult == nil, "Loss of engine readiness must discard evidence")

        configure("A")
        await model.refresh()
        await model.test()
        SetupHTTP.configure { _ in (503, ["detail": "Synthetic unavailable service"], 0) }
        await model.refresh()
        check(!model.statusIsCurrent && model.testResult == nil, "A failed refresh cannot retain current readiness/evidence")
        check(model.refreshError != nil, "A failed refresh explains that current state is unknown")
        check(!model.testPassed && model.readinessLabel == "Needs attention", "Unknown readiness cannot retain a passed badge")

        configure("A")
        await model.refresh()
        let memoryRefusal = "The selected model could not load within the AI engine's current memory limit. Free some memory, then try again."
        SetupHTTP.configure { request in
            request.url!.path == "/chat" ? (503, ["detail": memoryRefusal], 0) : (200, setupSnapshot(), 0)
        }
        await model.test()
        check(model.testResult == "That didn't work: \(memoryRefusal)", "Memory admission failure retains the safe actionable error")
        check(!model.testing && !model.testPassed && model.readinessLabel == "Ready to test",
              "A refused load is not a passed test and permits a later explicit retry")
        check(SetupHTTP.count("/chat") == 1, "A memory refusal must not automatically replay the request")
        configure("A")
        await model.test()
        check(model.testPassed, "A later explicit test may recover from a memory refusal")

        var compatibility = setupSnapshot(bundled: false)
        compatibility["bundled_engine"] = ["available": false, "minimum_macos": "26.2",
            "unavailable_reason": "The bundled engine needs macOS 26.2 or newer (this Mac has 26.1)"]
        SetupHTTP.configure { _ in (200, compatibility, 0) }
        await model.refresh()
        check(model.status?.bundledEngine?.minimumMacos == "26.2", "The fractional bundled requirement survives decoding")
        check(model.status?.bundledEngine?.unavailableReason?.contains("26.1") == true,
              "Setup retains the explanation after selecting an external engine")

        configure("A")
        await model.refresh(quiet: true)
        check(model.refreshError == nil, "Quiet recovery clears only the refresh error")
        model.message = "An unrelated actionable error"
        model.messageIsError = true
        await model.refresh(quiet: true)
        check(model.message == "An unrelated actionable error", "Quiet recovery preserves unrelated action errors")
        await model.test()
        await model.startEngine()
        check(SetupHTTP.saw("/engine/start"), "Built-in Start must use the private-engine endpoint")
        check(model.testResult == nil, "Starting an engine invalidates its former test")
        await model.test()
        await model.connectExternal(port: 54322, model: "fake-external")
        check(model.testResult == nil, "Changing provider invalidates test evidence")

        configure("A", delayChat: 0.2)
        await model.refresh()
        let pending = Task { await model.test() }
        while !SetupHTTP.saw("/chat") { try await Task.sleep(nanoseconds: 1_000_000) }
        configure("B")
        await model.apply("B")
        await pending.value
        check(model.testResult == nil, "An old in-flight test cannot restore success after applying B")

        // A status GET already in flight must not overwrite the Apply response.
        SetupHTTP.configure { request in
            if request.url!.path == "/setup/status" { return (200, setupSnapshot("OLD"), 0.2) }
            return (200, setupSnapshot("NEW"), 0)
        }
        let oldRefresh = Task { await model.refresh(quiet: true) }
        while !SetupHTTP.saw("/setup/status") { try await Task.sleep(nanoseconds: 1_000_000) }
        await model.apply("NEW")
        await oldRefresh.value
        check(model.status?.roles.first?.model == "NEW" && model.statusIsCurrent,
              "An older GET cannot replace a newly applied configuration")

        SetupHTTP.configure { _ in (503, ["detail": "Old failed GET"], 0.1) }
        let oldFailure = Task { await model.refresh(quiet: true) }
        while !SetupHTTP.saw("/setup/status") { try await Task.sleep(nanoseconds: 1_000_000) }
        configure("LATEST")
        await model.refresh(quiet: true)
        await oldFailure.value
        check(model.status?.roles.first?.model == "LATEST" && model.statusIsCurrent && model.refreshError == nil,
              "An older failed GET cannot invalidate newer successful state")

        configure("B")
        await model.download("B")
        check(SetupHTTP.saw("/engine/downloads"), "Native downloads must use Wisp's authenticated bridge")
        SetupHTTP.configure { request in
            if request.url!.path == "/engine/downloads" {
                return (200, ["tasks": [["model": "MISSING", "state": "completed", "progress": 100,
                                        "downloaded_bytes": 100, "total_bytes": 100, "bytes_per_second": 0]]], 0)
            }
            return (200, setupSnapshot("B"), 0)
        }
        await model.refreshDownloads()
        check(model.shouldOfferRedownload("MISSING"), "A completed download absent from inventory must offer recovery")
        SetupHTTP.configure { _ in (200, setupSnapshot("MISSING"), 0) }
        await model.refresh()
        check(!model.shouldOfferRedownload("MISSING"), "Successful discovery ends the missing-files recovery state")

        func transfer(_ state: String) -> [String: Any] {
            ["tasks": [["model": "A", "state": state, "progress": 25,
                        "downloaded_bytes": 25, "total_bytes": 100, "bytes_per_second": 0]]]
        }
        configure("A")
        let transfers = SetupGuideModel(session: session)
        await transfers.refresh()
        var downloadRequests = 0
        SetupHTTP.configure { _ in
            downloadRequests += 1
            return (200, transfer(downloadRequests == 1 ? "downloading" : "cancelled"),
                    downloadRequests == 1 ? 0.15 : 0)
        }
        let delayedDownload = Task { await transfers.refreshDownloads() }
        while !SetupHTTP.saw("/engine/downloads") { try await Task.sleep(nanoseconds: 1_000_000) }
        await transfers.refreshDownloads()
        await delayedDownload.value
        check(transfers.downloads.first?.state == "cancelled", "An older progress response cannot replace newer cancellation")

        downloadRequests = 0
        SetupHTTP.configure { _ in
            downloadRequests += 1
            return downloadRequests == 1 ? (503, ["detail": "Old download failure"], 0.15)
                                         : (200, transfer("pending"), 0)
        }
        let delayedDownloadError = Task { await transfers.refreshDownloads() }
        while !SetupHTTP.saw("/engine/downloads") { try await Task.sleep(nanoseconds: 1_000_000) }
        await transfers.refreshDownloads()
        await delayedDownloadError.value
        check(transfers.downloads.first?.state == "pending" && transfers.downloadError == nil,
              "A delayed old failure cannot restore an error after a successful status")

        downloadRequests = 0
        SetupHTTP.configure { request in
            if request.url!.path == "/engine/downloads" {
                downloadRequests += 1
                return (200, transfer(downloadRequests == 1 ? "downloading" : "cancelled"),
                        downloadRequests == 1 ? 0.15 : 0)
            }
            if request.url!.path == "/engine/downloads/cancel" { return (200, [:], 0) }
            return (200, setupSnapshot("A"), 0)
        }
        let preCancelPoll = Task { await transfers.refreshDownloads() }
        while !SetupHTTP.saw("/engine/downloads") { try await Task.sleep(nanoseconds: 1_000_000) }
        await transfers.download("A", cancel: true)
        await preCancelPoll.value
        check(transfers.downloads.first?.state == "cancelled", "Cancel invalidates progress requests begun before its mutation")

        SetupHTTP.configure { _ in (200, transfer("downloading"), 0.1) }
        let preClosePoll = Task { await transfers.refreshDownloads() }
        while !SetupHTTP.saw("/engine/downloads") { try await Task.sleep(nanoseconds: 1_000_000) }
        transfers.stopMonitoring()
        await preClosePoll.value
        check(transfers.downloads.first?.state == "cancelled", "Closing the setup window rejects an in-flight progress response")

        configure("A")
        let retryTransfers = SetupGuideModel(session: session)
        await retryTransfers.refresh()
        downloadRequests = 0
        SetupHTTP.configure { request in
            if request.url!.path == "/engine/downloads" {
                if request.httpMethod == "POST" { return (200, [:], 0) }
                downloadRequests += 1
                return (200, transfer(downloadRequests == 1 ? "completed" : "pending"),
                        downloadRequests == 1 ? 0.15 : 0)
            }
            return (200, setupSnapshot("A"), 0)
        }
        let preRetryPoll = Task { await retryTransfers.refreshDownloads() }
        while !SetupHTTP.saw("/engine/downloads") { try await Task.sleep(nanoseconds: 1_000_000) }
        await retryTransfers.download("A")
        await preRetryPoll.value
        check(retryTransfers.downloads.first?.state == "pending", "Retry invalidates old completed status while the engine owns the new task ID")

        SetupHTTP.configure { request in
            if request.url!.path == "/engine/downloads" { return (200, transfer("completed"), 0.15) }
            return (200, setupSnapshot("A", bundled: false), 0)
        }
        let oldEnginePoll = Task { await retryTransfers.refreshDownloads() }
        while !SetupHTTP.saw("/engine/downloads") { try await Task.sleep(nanoseconds: 1_000_000) }
        await retryTransfers.refresh()
        await oldEnginePoll.value
        check(retryTransfers.downloads.isEmpty && retryTransfers.downloadError == nil,
              "A previous engine context cannot publish download state after mode changes")

        configure("B")
        let monitor = Task { await model.monitor() }
        while !SetupHTTP.saw("/engine/downloads") { try await Task.sleep(nanoseconds: 1_000_000) }
        monitor.cancel()
        await monitor.value
        check(monitor.isCancelled, "Closing setup cancels its monitor")
        configure("B")
        let retainedWindowMonitor = Task { await model.monitor() }
        while !SetupHTTP.saw("/engine/downloads") { try await Task.sleep(nanoseconds: 1_000_000) }
        model.stopMonitoring()
        await retainedWindowMonitor.value
        check(!retainedWindowMonitor.isCancelled, "A retained closed window explicitly ends polling without relying on SwiftUI cancellation")

        SetupHTTP.configure { _ in (503, ["detail": "Initial service unavailable"], 0.05) }
        let unavailable = SetupGuideModel(session: session)
        let initialMonitor = Task { await unavailable.monitor() }
        while !SetupHTTP.saw("/setup/status") { try await Task.sleep(nanoseconds: 1_000_000) }
        unavailable.stopMonitoring()
        await initialMonitor.value
        check(SetupHTTP.count("/setup/status") == 1,
              "Stopping during the initial failed request prevents every hidden retry")

        // Hold both replies explicitly. Finish the stale request while the
        // fresh request remains in flight; only then release the fresh reply.
        for code in [200, 503] {
            configure("A")
            let closed = SetupGuideModel(session: session)
            await closed.refresh()
            let oldReply = SetupReplyGate()
            SetupHTTP.configure(holds: ["/chat": [oldReply]]) { _ in
                (code, code == 200 ? ["content": "OK", "setup_test": "complete_visible_assistant_v1"]
                                  : ["detail": "Delayed synthetic failure"], 0)
            }
            let delayed = Task { await closed.test() }
            guard try await waitForSetupRequest({ oldReply.captured }) else {
                throw NSError(domain: "SetupGuideChecks", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "Old test request never arrived"])
            }
            closed.stopMonitoring()
            check(closed.testResult == nil && !closed.testing, "Stop clears test evidence and busy state")
            let freshReply = SetupReplyGate()
            configure("A", chatGate: freshReply)
            let reopen = Task { await closed.monitor() }
            guard try await waitForSetupRequest({ SetupHTTP.saw("/engine/downloads") }) else {
                throw NSError(domain: "SetupGuideChecks", code: 2,
                              userInfo: [NSLocalizedDescriptionKey: "Reopened monitor never reached downloads"])
            }
            let fresh = Task { await closed.test() }
            let freshArrived = try await waitForSetupRequest({ freshReply.captured })
            check(freshArrived, "Reopen can start a test before the old request finishes")
            oldReply.release()
            await delayed.value
            check(closed.testResult == nil, "Stop fences delayed test HTTP\(code)")
            check(closed.testing, "An old completion cannot clear a reopened test's busy state")
            freshReply.release()
            await fresh.value
            check(closed.testResult?.hasPrefix("Works") == true, "Reopened retained model can pass a fresh test")
            closed.stopMonitoring()
            await reopen.value
        }
        configure("A", delayChat: 0.15)
        let cancelled = SetupGuideModel(session: session)
        await cancelled.refresh()
        let cancelledTest = Task { await cancelled.test() }
        while !SetupHTTP.saw("/chat") { try await Task.sleep(nanoseconds: 1_000_000) }
        cancelledTest.cancel()
        await cancelledTest.value
        check(cancelled.testResult == nil && !cancelled.testing, "A cancelled test cannot publish success or an error")

        // Missing/invalid certificates from an older or malformed endpoint
        // never count as a successful setup test.
        for marker: Any in [NSNull(), false, true, 1, "wrong", ["valid": true]] {
            configure("A")
            let uncertified = SetupGuideModel(session: session)
            await uncertified.refresh()
            SetupHTTP.configure { _ in (200, ["content": "OK", "setup_test": marker], 0) }
            await uncertified.test()
            check(uncertified.testResult?.hasPrefix("Works") != true, "Invalid certificate \(marker) must fail closed")
        }
        for content in ["", "  ", "<THINK>secret</THINK>", "OK</think>"] {
            configure("A")
            let malformed = SetupGuideModel(session: session)
            await malformed.refresh()
            SetupHTTP.configure { _ in
                (200, ["content": content, "setup_test": "complete_visible_assistant_v1"], 0)
            }
            await malformed.test()
            check(malformed.testResult?.hasPrefix("Works") != true, "A certificate cannot excuse unusable visible content")
        }
        configure("A")
        let oldEndpoint = SetupGuideModel(session: session)
        await oldEndpoint.refresh()
        SetupHTTP.configure { _ in (200, ["content": "OK"], 0) }
        await oldEndpoint.test()
        check(oldEndpoint.testResult?.hasPrefix("Works") != true, "An older generic endpoint cannot certify setup")

        // One model, two hosts: the notch's setup card and the setup window.
        configure("A")
        let shared = SetupGuideModel(session: session)
        shared.show(in: "notch")
        guard try await waitForSetupRequest({ shared.statusIsCurrent }) else {
            throw NSError(domain: "SetupGuideChecks", code: 3,
                          userInfo: [NSLocalizedDescriptionKey: "The first host never started the poll"])
        }
        await shared.test()
        check(shared.testPassed, "A test passed from one host is the shared model's result")
        shared.show(in: "window")
        shared.hide(from: "notch")
        check(shared.isShown && shared.testPassed,
              "Closing one host keeps the poll and the evidence while another still shows setup")
        // SwiftUI may show a host's new view before the old one disappears.
        shared.show(in: "notch")
        shared.show(in: "notch")
        shared.hide(from: "notch")
        check(shared.hosts["notch"] == 1 && shared.testPassed, "A host's overlapping appear and disappear keep it shown")
        shared.hide(from: "notch")
        shared.hide(from: "window")
        check(!shared.isShown && !shared.testPassed && shared.testResult == nil && !shared.testing,
              "The last host to leave stops setup and drops its test evidence")
        shared.hide(from: "window")
        check(shared.hosts.isEmpty, "An extra hide is harmless")
        let stoppedPolls = SetupHTTP.count("/setup/status")
        await shared.refresh()
        check(SetupHTTP.count("/setup/status") == stoppedPolls, "With no host, setup stops reading status")
        // Shown and hidden before the poll's task ever ran: setup stays stopped.
        shared.show(in: "notch")
        shared.hide(from: "notch")
        await Task.yield()
        await shared.refresh()
        check(SetupHTTP.count("/setup/status") == stoppedPolls, "A host that shows and hides at once leaves setup stopped")
        shared.show(in: "window")
        guard try await waitForSetupRequest({ SetupHTTP.count("/setup/status") > stoppedPolls && shared.statusIsCurrent }) else {
            throw NSError(domain: "SetupGuideChecks", code: 4,
                          userInfo: [NSLocalizedDescriptionKey: "Showing setup again never re-read status"])
        }
        check(shared.readinessLabel == "Ready to test", "Reopening setup starts fresh: a new test is needed")

        // The notch's one button: test the model in use, or use the selected one first.
        let applies = SetupHTTP.count("/setup/apply")
        await shared.useAndTest()
        check(SetupHTTP.count("/setup/apply") == applies && shared.testPassed,
              "Use & test on the model already in use only tests it")
        configure("B")
        shared.selectedModel = "B"
        await shared.useAndTest()
        check(SetupHTTP.count("/setup/apply") == 1 && shared.status?.roles.first?.model == "B" && shared.testPassed,
              "Use & test on another model applies it, then tests that model")
        SetupHTTP.configure { request in
            request.url!.path == "/setup/apply" ? (400, ["detail": "Synthetic refusal"], 0) : (200, setupSnapshot("B"), 0)
        }
        shared.selectedModel = "C"
        await shared.useAndTest()
        check(!shared.testPassed && SetupHTTP.count("/chat") == 0 && shared.messageIsError,
              "A refused apply never sends the test message")

        // Settings changed the models or engine: the test belonged to the old setup.
        configure("A")
        shared.selectedModel = "A"
        await shared.useAndTest()
        check(shared.testPassed, "A applied and tested again")
        let beforeChange = SetupHTTP.count("/setup/status")
        NotificationCenter.default.post(name: .wispInferenceConfigurationChanged, object: nil)
        let dropped = try await waitForSetupRequest({ !shared.testPassed })
        check(dropped && shared.testResult == nil, "A Settings inference change drops setup's passed test")
        let reread = try await waitForSetupRequest({
            SetupHTTP.count("/setup/status") > beforeChange && shared.statusIsCurrent })
        check(reread && shared.readinessLabel == "Ready to test",
              "A shown setup re-reads status after the change, and still needs a fresh test")
        shared.hide(from: "window")
        let hiddenPolls = SetupHTTP.count("/setup/status")
        NotificationCenter.default.post(name: .wispInferenceConfigurationChanged, object: nil)
        _ = try await waitForSetupRequest({ !shared.statusIsCurrent })
        check(!shared.statusIsCurrent && SetupHTTP.count("/setup/status") == hiddenPolls,
              "A change while setup is hidden marks its status stale without reading it")

        if CommandLine.arguments.count == 2 {
            let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
            let cases = try JSONSerialization.jsonObject(with: data) as! [[String: Any]]
            for item in cases {
                configure("A")
                let pipeline = SetupGuideModel(session: session)
                await pipeline.refresh()
                var requestedValidation = false
                SetupHTTP.configure { request in
                    let body = try! JSONSerialization.jsonObject(with: request.httpBody
                        ?? request.httpBodyStream.map { stream in
                            stream.open(); defer { stream.close() }
                            var bytes = [UInt8](repeating: 0, count: 4096)
                            var result = Data()
                            while stream.hasBytesAvailable {
                                let count = stream.read(&bytes, maxLength: bytes.count)
                                if count <= 0 { break }; result.append(bytes, count: count)
                            }
                            return result
                        } ?? Data()) as! [String: Any]
                    requestedValidation = (body["setup_test"] as? Bool) == true
                    return (item["status"] as! Int, item["body"] as! [String: Any], 0)
                }
                await pipeline.test()
                check(requestedValidation, "Native test requests server validation")
                check((pipeline.testResult?.hasPrefix("Works") == true) == (item["expectedWorks"] as! Bool),
                      "Actual managed completion/route/native pipeline: \(item["name"]!)")
            }
        }
        // A transfer keeps setup's poll alive after the notch card or a window
        // goes away, until the model has arrived; it never starts one by itself.
        do {
            func tasks(_ state: String) -> [String: Any] {
                ["tasks": [["model": "G", "state": state, "progress": 40,
                            "downloaded_bytes": 40, "total_bytes": 100, "bytes_per_second": 5]]]
            }
            var transferState = "downloading"
            var readyNow = false
            // Wisp's own engine, running, no model yet; then ready once readyNow.
            SetupHTTP.configure { request in
                if request.url!.path == "/engine/downloads" { return (200, tasks(transferState), 0) }
                var snapshot = setupSnapshot("G", ready: false)
                snapshot["omlx"] = ["installed": true, "running": true, "bundled": true, "min_macos": 15,
                                    "url": "https://example.invalid", "admin_url": "http://127.0.0.1:8791/admin"]
                if readyNow { snapshot = setupSnapshot("G", ready: true) }
                return (200, snapshot, 0)
            }
            let watcher = SetupGuideModel(session: session)
            watcher.show(in: "notch")
            // The poll show() starts runs alongside these calls and may supersede
            // them, so each check waits (bounded) for the state to settle.
            await watcher.refresh()
            await watcher.refreshDownloads()
            let settled1 = try await waitForSetupRequest { watcher.hosts[SetupGuideModel.downloadHost] == 1 }
            check(settled1, "An active transfer seen from a shown setup holds the download host")
            watcher.hide(from: "notch")
            check(watcher.isShown, "Collapsing the notch card leaves the poll running while the transfer does")
            transferState = "completed"
            await watcher.refreshDownloads()
            let settled2 = try await waitForSetupRequest { watcher.downloads.first?.state == "completed" }
            check(settled2 && watcher.hosts[SetupGuideModel.downloadHost] == 1, "A just-finished download keeps the poll until the model arrives")
            readyNow = true
            await watcher.refresh()
            let settled3 = try await waitForSetupRequest { watcher.hosts[SetupGuideModel.downloadHost] == nil && !watcher.isShown }
            check(settled3, "Once Wisp can answer, the download host lets go and the poll stops")

            // Seen with nothing on screen (a direct call), a transfer never starts a poll.
            transferState = "downloading"
            readyNow = false
            let unseen = SetupGuideModel(session: session)
            await unseen.refresh()
            await unseen.refreshDownloads()
            check(unseen.hosts.isEmpty, "No host is taken while setup isn't on screen")

            // Cancelled or failed: nothing is transferring, so the host goes.
            let stopper = SetupGuideModel(session: session)
            stopper.show(in: "window")
            await stopper.refresh()
            await stopper.refreshDownloads()
            let settled4 = try await waitForSetupRequest { stopper.hosts[SetupGuideModel.downloadHost] == 1 }
            check(settled4, "Held while downloading")
            transferState = "cancelled"
            await stopper.refreshDownloads()
            let settled5 = try await waitForSetupRequest { stopper.hosts[SetupGuideModel.downloadHost] == nil }
            check(settled5 && stopper.hosts["window"] == 1,
                  "A paused transfer lets the download host go; the window keeps its own")
            stopper.hide(from: "window")

            func task(_ state: String, bytes: Double = 40) -> SetupGuideModel.DownloadTask {
                let decoder = JSONDecoder()
                decoder.keyDecodingStrategy = .convertFromSnakeCase
                var row = (tasks(state)["tasks"] as! [[String: Any]])[0]
                row["downloaded_bytes"] = bytes
                return try! decoder.decode(SetupGuideModel.DownloadTask.self,
                                           from: JSONSerialization.data(withJSONObject: row))
            }
            let t0 = Date(timeIntervalSince1970: 1000)
            func at(_ seconds: TimeInterval) -> Date { t0.addingTimeInterval(seconds) }
            // Running, starting, finishing, stopped.
            var hold = SetupDownloadHold()
            hold.observe([task("pending")], previous: [], now: t0)
            check(hold.decide([task("pending")], ready: false, now: t0), "Pending holds")
            hold = SetupDownloadHold()
            hold.observe([task("failed")], previous: [], now: t0)
            check(!hold.decide([task("failed")], ready: false, now: t0), "Failed lets go")
            hold = SetupDownloadHold()
            hold.starting = true
            check(hold.decide([], ready: false, now: t0), "Download pressed holds before the engine answers")
            hold = SetupDownloadHold()
            hold.observe([task("completed")], previous: [task("downloading")], now: t0)
            check(hold.decide([task("completed")], ready: false, now: at(60)), "Finishing holds a while")
            check(!hold.decide([task("completed")], ready: false, now: at(SetupDownloadHold.finishingGrace + 1)),
                  "A model that never arrives doesn't poll forever")
            check(!hold.decide([task("completed")], ready: true, now: at(1)), "Ready lets go at once")

            // An engine that leaves a transfer "downloading" at 0 B/s.
            hold = SetupDownloadHold()
            hold.observe([task("downloading", bytes: 40)], previous: [], now: t0)
            for minute in 1...9 {
                hold.observe([task("downloading", bytes: 40)], previous: [task("downloading", bytes: 40)], now: at(Double(minute) * 60))
            }
            check(hold.decide([task("downloading", bytes: 40)], ready: false, now: at(9 * 60)), "Under 10 minutes still: held")
            hold.observe([task("downloading", bytes: 40)], previous: [task("downloading", bytes: 40)], now: at(10 * 60))
            check(!hold.decide([task("downloading", bytes: 40)], ready: false, now: at(10 * 60)),
                  "No progress for 10 minutes lets go")
            hold.released()
            hold.observe([task("downloading", bytes: 41)], previous: [task("downloading", bytes: 40)], now: at(11 * 60))
            check(hold.decide([task("downloading", bytes: 41)], ready: false, now: at(11 * 60)), "Progress again: held again")

            // A stale snapshot: the service stops answering while "downloading" is on screen.
            hold = SetupDownloadHold()
            hold.observe([task("downloading")], previous: [], now: t0)
            hold.failed(); hold.failed()
            check(hold.decide([task("downloading")], ready: false, now: at(6)), "Two failed reads: still held")
            hold.failed()
            check(!hold.decide([task("downloading")], ready: false, now: at(9)), "Three failed reads in a row let go")
            hold.released()
            hold.observe([task("downloading", bytes: 60)], previous: [task("downloading")], now: at(30))
            check(hold.decide([task("downloading", bytes: 60)], ready: false, now: at(30)), "A good read with progress takes it again")

            // The six-hour cap, like the backend's own watch; never retaken by the same transfer.
            hold = SetupDownloadHold()
            hold.observe([task("downloading", bytes: 1)], previous: [], now: t0)
            hold.since = t0
            hold.observe([task("downloading", bytes: 2)], previous: [task("downloading", bytes: 1)], now: at(3600))
            check(hold.decide([task("downloading", bytes: 2)], ready: false, now: at(3600)), "Moving, within the cap: held")
            hold.observe([task("downloading", bytes: 3)], previous: [task("downloading", bytes: 2)], now: at(SetupDownloadHold.cap))
            check(!hold.decide([task("downloading", bytes: 3)], ready: false, now: at(SetupDownloadHold.cap)),
                  "Six hours lets go even while moving")
            hold.released()
            hold.observe([task("downloading", bytes: 4)], previous: [task("downloading", bytes: 3)], now: at(SetupDownloadHold.cap + 60))
            check(!hold.decide([task("downloading", bytes: 4)], ready: false, now: at(SetupDownloadHold.cap + 60)),
                  "The capped transfer doesn't take it again")
            hold.observe([], previous: [task("downloading", bytes: 4)], now: at(SetupDownloadHold.cap + 120))
            hold.observe([task("pending", bytes: 0)], previous: [], now: at(SetupDownloadHold.cap + 180))
            check(hold.decide([task("pending", bytes: 0)], ready: false, now: at(SetupDownloadHold.cap + 180)),
                  "A new transfer after the cap is held")

            // In the model: three failed reads release the host and the sleep assertion.
            transferState = "downloading"
            readyNow = false
            let flaky = SetupGuideModel(session: session)
            flaky.show(in: "notch")
            await flaky.refresh()
            await flaky.refreshDownloads()
            let heldFlaky = try await waitForSetupRequest { flaky.hosts[SetupGuideModel.downloadHost] == 1 }
            check(heldFlaky, "Held while downloading")
            flaky.hide(from: "notch")
            SetupHTTP.configure { request in
                if request.url!.path == "/engine/downloads" { return (503, ["detail": "Engine down"], 0) }
                return (503, ["detail": "Service down"], 0)
            }
            for _ in 0..<SetupDownloadHold.failureLimit { await flaky.refreshDownloads() }
            let releasedFlaky = try await waitForSetupRequest { flaky.hosts.isEmpty }
            check(releasedFlaky && flaky.downloads.first?.state == "downloading",
                  "A stale \"downloading\" snapshot with the service down lets the host and the poll go")

            // Pressing Download in the card holds the poll before the engine answers.
            // The start request answers after a second, as an engine that is busy would.
            SetupHTTP.configure { request in
                if request.url!.path == "/engine/downloads" {
                    return request.httpMethod == "POST" ? (200, [:], 1.0) : (200, tasks("downloading"), 0)
                }
                var snapshot = setupSnapshot("G", ready: false)
                snapshot["omlx"] = ["installed": true, "running": true, "bundled": true, "min_macos": 15,
                                    "url": "https://example.invalid", "admin_url": "http://127.0.0.1:8791/admin"]
                return (200, snapshot, 0)
            }
            let presser = SetupGuideModel(session: session)
            presser.show(in: "notch")
            await presser.refresh()
            let pressed = Task { await presser.download("G") }
            let inFlight = try await waitForSetupRequest { presser.busy == "Starting download…" }
            check(inFlight && presser.hosts[SetupGuideModel.downloadHost] == 1,
                  "Download pressed: the host is held while the start request is in flight")
            presser.hide(from: "notch")
            check(presser.isShown, "Collapsing the card mid-request keeps the poll")
            await pressed.value
            check(presser.hosts[SetupGuideModel.downloadHost] == 1, "The started transfer keeps the host")
            transferState = "cancelled"
            SetupHTTP.configure { request in
                if request.url!.path == "/engine/downloads" { return (200, tasks("cancelled"), 0) }
                return (200, setupSnapshot("G", ready: false), 0)
            }
            await presser.refreshDownloads()
            let presserDone = try await waitForSetupRequest { presser.hosts.isEmpty }
            check(presserDone, "Stopped: the host goes")
            check(task("downloading").progressText == "40% · 40 bytes of 100 bytes · 5 bytes/s"
                  && task("downloading").speedText == "5 bytes/s", "Shared progress text: percent, size and speed")
        }

        if failures > 0 {
            FileHandle.standardError.write(Data("\(failures) of \(checks) setup checks failed\n".utf8))
            exit(1)
        }
        print("\(checks) setup model-state checks passed; all HTTP intercepted")
    }
}
