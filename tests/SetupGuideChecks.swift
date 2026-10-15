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
        await model.test()
        check(model.testResult?.hasPrefix("Works") == true, "A should pass its synthetic test")
        await model.refresh()
        check(model.testResult?.hasPrefix("Works") == true, "An unchanged live configuration may retain evidence")

        configure("B")
        await model.apply("B")
        check(model.testResult == nil, "Applying B must discard A's successful test")
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
        if failures > 0 {
            FileHandle.standardError.write(Data("\(failures) of \(checks) setup checks failed\n".utf8))
            exit(1)
        }
        print("\(checks) setup model-state checks passed; all HTTP intercepted")
    }
}
