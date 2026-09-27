import Foundation
import Security

// Synthetic credentials only. No Keychain, browser, network, app launch or effects.
private final class FixtureRuntime {
    var available = true
    var profileID = "profile.1"
    var taskID: String? = "task.1"
    var snapshotID: String? = "snapshot.1"
    var proposalID: String? = "proposal.1"
    var enabled = true
    var privateContext = false
    var background = true
    var origins: Set<String> = ["https://school.example.invalid"]

    func context() throws -> BrowserBridgeRuntimeContext {
        try BrowserBridgeWire.check(available, "Synthetic context unavailable")
        return BrowserBridgeRuntimeContext(profileID: profileID, enabled: enabled, privateContext: privateContext,
            background: background, allowedOrigins: origins, url: "https://school.example.invalid/assignments/42",
            taskID: taskID, snapshotID: snapshotID, approvalProposalID: proposalID)
    }
}

@main struct BridgeHarness {
    static func main() async {
        var bridge: BrowserBridge?
        var runtime: FixtureRuntime?
        while let line = readLine() {
            do {
                let request = try BrowserBridgeWire.parse(Data(line.utf8))
                let op = request["op"] as! String
                var response: [String: Any] = ["ok": true]
                switch op {
                case "credential_queries":
                    let identity = BrowserBridgeIdentity(credentialID: "synthetic", peer: "extension",
                        credentialRole: "native_bridge", profileID: "profile.1")
                    response["operations"] = try BrowserBridgeCredentials.Operation.allCases.map { operation in
                        let q = try BrowserBridgeCredentials.query(identity, operation: operation)
                        return ["data_protection": q[kSecUseDataProtectionKeychain as String] as? Bool == true,
                                "no_sync": q[kSecAttrSynchronizable as String] as? Bool == false,
                                "account_bound": q[kSecAttrAccount as String] as? String == identity.credentialID,
                                "device_only": operation != .add || q[kSecAttrAccessible as String] as? String == kSecAttrAccessibleWhenUnlockedThisDeviceOnly as String,
                                "no_prompt": operation != .lookup || q[kSecUseAuthenticationUI as String] as? String == kSecUseAuthenticationUIFail as String]
                    }
                case "reset":
                    let role = request["role"] as? String ?? "native_bridge"
                    let identity = BrowserBridgeIdentity(credentialID: role == "native_bridge" ? "adapter" : "approval",
                        peer: role == "native_bridge" ? "extension" : "app", credentialRole: role, profileID: "profile.1")
                    let current = FixtureRuntime()
                    current.privateContext = request["private"] as? Bool ?? false
                    runtime = current
                    bridge = try BrowserBridge(identity: identity, key: Data(repeating: role == "native_bridge" ? 7 : 9, count: 32)) {
                        try current.context()
                    }
                case "context":
                    if let value = request["available"] as? Bool { runtime!.available = value }
                    if let value = request["profile"] as? String { runtime!.profileID = value }
                    if request.keys.contains("task") { runtime!.taskID = request["task"] as? String }
                    if request.keys.contains("snapshot") { runtime!.snapshotID = request["snapshot"] as? String }
                    if request.keys.contains("proposal") { runtime!.proposalID = request["proposal"] as? String }
                    if let value = request["enabled"] as? Bool { runtime!.enabled = value }
                    if let value = request["private"] as? Bool { runtime!.privateContext = value }
                    if let value = request["background"] as? Bool { runtime!.background = value }
                    if let value = request["origins"] as? [String] { runtime!.origins = Set(value) }
                case "register":
                    response["data"] = try await bridge!.register(challenge: Data(base64Encoded: request["data"] as! String)!).base64EncodedString()
                case "receive":
                    let (kind, _) = try await bridge!.receive(Data(base64Encoded: request["data"] as! String)!)
                    response["kind"] = kind
                case "publish":
                    response["data"] = try await bridge!.publish(kind: request["kind"] as! String,
                        payload: request["payload"] as! [String: Any]).base64EncodedString()
                case "decide":
                    response["data"] = try await bridge!.decide(request["payload"] as! [String: Any]).base64EncodedString()
                case "disconnect":
                    let (raw, uncertain) = try await bridge!.disconnect()
                    response["data"] = raw.base64EncodedString()
                    response["uncertain"] = uncertain
                case "close":
                    response["uncertain"] = await bridge!.close()
                case "verify":
                    _ = try BrowserBridgeWire.open(Data(base64Encoded: request["data"] as! String)!,
                        key: Data(repeating: 7, count: 32), direction: request["direction"] as? String ?? "to_peer")
                default: throw BrowserContractViolation(message: "Invalid fixture operation", code: "invalid_payload")
                }
                print(String(data: try BrowserBridgeWire.json(response), encoding: .utf8)!)
            } catch let error as BrowserBridgeClosed {
                let response: [String: Any] = ["ok": false, "uncertain": error.uncertainActionIDs]
                print(String(data: try! BrowserBridgeWire.json(response), encoding: .utf8)!)
            } catch let error as BrowserContractViolation {
                let response: [String: Any] = ["ok": false, "code": error.code]
                print(String(data: try! BrowserBridgeWire.json(response), encoding: .utf8)!)
            } catch { print("{\"ok\":false}") }
            fflush(stdout)
        }
    }
}
