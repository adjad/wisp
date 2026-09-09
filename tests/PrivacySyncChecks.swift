// Compile with BrowserHistoryReader.swift and ContactsReader.swift, -lsqlite3.
// In-memory defaults, injected source snapshots and URLProtocol interception:
// no history/Contacts enumeration, sockets, real preferences, or communications.
import Foundation
import Contacts

struct WispClient {
    static let baseURL = URL(string: "https://privacy.invalid/")!
}

final class MemoryDefaults: UserDefaults {
    private var values: [String: Any] = [:]
    override func integer(forKey key: String) -> Int { values[key] as? Int ?? 0 }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
}

final class MockHTTP: URLProtocol {
    static var handler: (([String: Any]) -> Int)?
    static var current = true
    static var serverRevision = 0
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var data = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                data.append(contentsOf: buffer.prefix(count))
            }
        }
        let body = data.isEmpty ? ["method": "GET"] :
            (try! JSONSerialization.jsonObject(with: data)) as! [String: Any]
        DispatchQueue.main.async {
            let status = Self.handler!(body)
            let response = HTTPURLResponse(url: self.request.url!, statusCode: status,
                                           httpVersion: nil, headerFields: nil)!
            self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            if let revision = body["revision"] as? Int, status == 200 {
                Self.serverRevision = revision
            }
            let reply: [String: Any] = ["ok": true, "revision": Self.serverRevision, "current": Self.current]
            self.client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: reply))
            self.client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func stopLoading() {}
}

@main
enum PrivacySyncChecks {
    static func main() {
        let defaults = MemoryDefaults()
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockHTTP.self]
        let session = URLSession(configuration: config)
        func pump(until condition: () -> Bool) {
            let deadline = Date().addingTimeInterval(5)
            while !condition(), Date() < deadline {
                RunLoop.main.run(until: Date().addingTimeInterval(0.005))
            }
            precondition(condition(), "Timed out waiting for synthetic sync")
        }
        func channel(_ key: String) -> PrivacySyncChannel {
            PrivacySyncChannel(key: key, endpoint: "synthetic", defaults: defaults,
                               session: session, retryDelay: 0.02)
        }
        var received: [[String: Any]] = []
        var failures = 0
        MockHTTP.handler = { body in
            received.append(body)
            if failures > 0 { failures -= 1; return 500 }
            return 200
        }
        let browserChannel = channel("browser")
        var browserEnabled = false
        var reads = 0
        var hasStarted = false
        let release = DispatchSemaphore(value: 0)
        let browser = BrowserHistoryReader(channel: browserChannel, enabled: { browserEnabled }, snapshot: {
            reads += 1
            DispatchQueue.main.async { hasStarted = true }
            release.wait()
            return ["safari": ["lines": "fixture", "diagnostics": ["available": true]],
                    "chrome": ["lines": "", "diagnostics": ["available": true]]]
        })
        browser.sync()
        pump { !received.isEmpty && !browserChannel.pending }
        precondition(received.last?["enabled"] as? Bool == false && reads == 0)
        let firstRevision = received.last?["revision"] as! Int

        // A read that finishes after a Settings notification must never post.
        browserEnabled = true
        browser.sync()
        pump { hasStarted }
        browserEnabled = false
        failures = 1
        NotificationCenter.default.post(name: BrowserHistoryReader.preferenceChanged, object: nil)
        pump { received.count >= 2 }
        precondition(browserChannel.pending) // HTTP 500 has not confirmed clearing.
        release.signal()
        pump { !browserChannel.pending && received.count >= 3 }
        precondition(received.allSatisfy { $0["enabled"] as? Bool == false })
        precondition((received.last?["revision"] as! Int) > firstRevision)
        precondition(received[1]["revision"] as! Int == received[2]["revision"] as! Int)

        // Relaunch uses persisted consent and a strictly newer ordering fence.
        let relaunched = channel("browser")
        let revision = relaunched.begin()
        precondition(revision > (received.last?["revision"] as! Int))
        relaunched.submit(["enabled": false], revision: revision)
        pump { !relaunched.pending }

        // Authorized empty sync; permission revocation; read error; toggle off.
        let contactChannel = channel("contacts")
        var contactEnabled = true
        var auth = CNAuthorizationStatus.authorized
        var contactReads = 0
        var failRead = false
        let contacts = ContactsReader(channel: contactChannel, enabled: { contactEnabled },
                                      authorization: { auth }, snapshot: {
            contactReads += 1
            if failRead { throw NSError(domain: "synthetic", code: 1) }
            return ([:], [:])
        })
        let before = received.count
        contacts.sync()
        pump { received.count > before && !contactChannel.pending }
        precondition(received.last?["contacts_available"] as? Bool == true)
        precondition((received.last?["contacts"] as? [String: String])?.isEmpty == true)
        auth = .denied
        contacts.checkAccess()
        pump { received.count > before + 1 && !contactChannel.pending }
        precondition(received.last?["contacts_available"] as? Bool == false && contactReads == 1)
        auth = .authorized
        failRead = true
        contacts.checkAccess()
        pump { received.count > before + 2 && !contactChannel.pending }
        precondition(received.last?["contacts_available"] as? Bool == false)
        contactEnabled = false
        NotificationCenter.default.post(name: ContactsReader.preferenceChanged, object: nil)
        pump { received.count > before + 3 && !contactChannel.pending }
        precondition(received.last?["contacts_enabled"] as? Bool == false && contactReads == 2)
        // Backend-only restart: a same-revision no-op is not confirmation;
        // the channel requests a fresh read and reserves a newer revision.
        let lastContactRevision = contactChannel.revision
        MockHTTP.current = false
        MockHTTP.handler = { body in
            received.append(body)
            if (body["revision"] as? Int ?? 0) > lastContactRevision { MockHTTP.current = true }
            return 200
        }
        let beforeRestartRetry = received.count
        contactChannel.submit(["contacts_enabled": false, "contacts_available": false],
                              revision: lastContactRevision)
        pump { received.count >= beforeRestartRetry + 2 && !contactChannel.pending }
        precondition(contactChannel.revision > lastContactRevision && contactReads == 2)

        // Also recover when the previous POST was acknowledged before restart.
        let acknowledgedRevision = contactChannel.revision
        MockHTTP.current = false
        MockHTTP.handler = { body in
            received.append(body)
            if body["method"] as? String != "GET" { MockHTTP.current = true }
            return 200
        }
        contactChannel.checkBackend()
        pump { contactChannel.revision > acknowledgedRevision && !contactChannel.pending }
        precondition(received.last?["contacts_enabled"] as? Bool == false && contactReads == 2)
        withExtendedLifetime((contacts, browser)) {}
        session.invalidateAndCancel()
        print("PrivacySync: 14 synthetic contract scenarios passed")
    }
}
