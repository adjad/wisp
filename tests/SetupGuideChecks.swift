import AppKit
import Foundation

// This executable never launches an application. Every HTTP request is
// intercepted below; no socket, native data source or real engine is used.
enum WispClient {
    static let baseURL = URL(string: "http://127.0.0.1:54321")!
}

final class SetupHTTP: URLProtocol {
    typealias Handler = (URLRequest) -> (Int, [String: Any], Double)
    private static let lock = NSLock()
    private static var handler: Handler = { _ in (500, [:], 0) }
    private static var paths: [String] = []

    static func configure(_ next: @escaping Handler) {
        lock.lock(); defer { lock.unlock() }
        handler = next
        paths = []
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
        Self.lock.unlock()
        let response = HTTPURLResponse(url: request.url!, statusCode: answer.0,
                                       httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        let data = try! JSONSerialization.data(withJSONObject: answer.1)
        DispatchQueue.global().asyncAfter(deadline: .now() + answer.2) { [self] in
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
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
        func check(_ value: @autoclosure () -> Bool, _ message: String) {
            precondition(value(), message)
            checks += 1
        }
        func configure(_ model: String, delayChat: Double = 0) {
            SetupHTTP.configure { request in
                if request.url!.path == "/chat" { return (200, ["content": "OK"], delayChat) }
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
        print("\(checks) setup model-state checks passed; all HTTP intercepted")
    }
}
