import Foundation

// Actual WispClient settings helpers are compiled in an extension. This class
// supplies controlled transport only, with no HTTP, providers or native readers.
@MainActor final class WispClient {
    struct Read { var reply: CheckedContinuation<[String: Any]?, Never>? }
    struct Write {
        let enabled: Bool
        let epoch: String?
        var reply: CheckedContinuation<[String: Any]?, Never>?
    }
    static var reads: [Read] = []
    static var writes: [Write] = []
    static let epoch = String(repeating: "a", count: 64)

    func behaviorRequest(_ method: String, _ path: String,
                         body: [String: Any]? = nil) async -> [String: Any]? {
        if method == "POST", path == "behavior/settings/check", body == nil {
            return await withCheckedContinuation { Self.reads.append(Read(reply: $0)) }
        }
        precondition(method == "PUT" && path == "behavior/settings")
        let enabled = body!["enabled"] as! Bool
        let epoch = body?["control_epoch"] as? String
        precondition(!enabled || epoch?.count == 64)
        return await withCheckedContinuation {
            Self.writes.append(Write(enabled: enabled, epoch: epoch, reply: $0))
        }
    }
    static func receipt(_ enabled: Bool) -> [String: Any] {
        receipt(enabled, epoch: epoch)
    }
    static func receipt(_ enabled: Bool, epoch: String) -> [String: Any] {
        ["enabled": enabled, "updates_settled": true, "control_epoch": epoch,
         "remote_active": true, "phraser": false]
    }
    static func read(_ index: Int, _ value: [String: Any]?) {
        let continuation = reads[index].reply!
        reads[index].reply = nil
        continuation.resume(returning: value)
    }
    static func writeRaw(_ index: Int, _ value: [String: Any]?) {
        let continuation = writes[index].reply!
        writes[index].reply = nil
        continuation.resume(returning: value)
    }
    static func write(_ index: Int, _ value: Bool?) {
        writeRaw(index, value.map { receipt($0) })
    }
    static func reset() {
        precondition(reads.allSatisfy { $0.reply == nil })
        precondition(writes.allSatisfy { $0.reply == nil })
        reads = []; writes = []
    }
}

@main @MainActor struct BehaviorGlobalSettingsRegression {
    static var checks = 0
    static var failures = 0
    static func check(_ value: @autoclosure () -> Bool, _ message: String) {
        checks += 1
        if !value() {
            failures += 1
            FileHandle.standardOutput.write(Data("FAIL: \(message)\n".utf8))
        }
    }
    static func unknown(_ value: Bool?) -> Bool { value == nil }
    static func wait(_ condition: () -> Bool) async {
        for _ in 0..<10000 {
            if condition() { return }
            await Task.yield()
        }
        fatalError("Expected controlled request did not arrive")
    }
    static func settle() async { for _ in 0..<200 { await Task.yield() } }
    static func confirmed(_ enabled: Bool) async -> SettingsLoader {
        WispClient.reset()
        let loader = SettingsLoader()
        let refresh = Task { await loader.refreshBehavior() }
        await wait { WispClient.reads.count == 1 }
        WispClient.read(0, WispClient.receipt(enabled))
        await refresh.value
        check(loader.behaviorEnabled == enabled, "checked receipt establishes consent")
        return loader
    }
    static func begin(_ loader: SettingsLoader, _ on: Bool) async -> Int {
        let write = WispClient.writes.count
        loader.setBehaviorEnabled(on)
        await wait { WispClient.writes.count == write + 1 }
        return write
    }
    static func main() async {
        check(unknown(SettingsLoader().behaviorEnabled), "first render must not invent consent")

        let failed = await confirmed(true)
        let first = await begin(failed, false)
        check(failed.behaviorEnabled != false, "pending disable must not claim OFF")
        check(!BehaviorPrivacyCopy(loader: failed).text.hasPrefix("Off."),
              "pending write must not display the OFF promise")
        WispClient.write(first, nil)
        await wait { WispClient.reads.count == 2 }
        WispClient.read(1, nil)
        await settle()
        check(unknown(failed.behaviorEnabled), "double failure must be explicitly unknown")
        check(!BehaviorPrivacyCopy(loader: failed).text.hasPrefix("Off."),
              "double failure must not promise that learning stopped")
        let retry = await begin(failed, false)
        WispClient.write(retry, false)
        await settle()
        check(failed.behaviorEnabled == false, "successful retry confirms OFF")
        check(BehaviorPrivacyCopy(loader: failed).text.hasPrefix("Off."),
              "confirmed OFF retains its clear explanation")
        let malformed = Task { await failed.refreshBehavior() }
        await wait { WispClient.reads.count == 3 }
        WispClient.read(2, ["phraser": true])
        await malformed.value
        check(unknown(failed.behaviorEnabled), "malformed consent response is unknown")

        let recovered = await confirmed(true)
        let lost = await begin(recovered, false)
        WispClient.write(lost, nil)
        await wait { WispClient.reads.count == 2 }
        WispClient.read(1, WispClient.receipt(false))
        await settle()
        check(recovered.behaviorEnabled == false, "fenced read reconciles a lost response")

        // A raw OFF snapshot cannot settle an enable that timed out. It has no
        // proof that an older enable completed or was fenced from later applying.
        let delayed = await confirmed(false)
        let enable = await begin(delayed, true)
        WispClient.write(enable, nil)
        await wait { WispClient.reads.count == 2 }
        WispClient.read(1, ["enabled": false])
        await settle()
        check(unknown(delayed.behaviorEnabled), "old OFF cannot settle a timed-out enable")
        check(!BehaviorPrivacyCopy(loader: delayed).text.hasPrefix("Off."),
              "pending server enable cannot be labeled OFF")
        let manual = Task { await delayed.refreshBehavior() }
        await wait { WispClient.reads.count == 3 }
        WispClient.read(2, ["enabled": false])
        await manual.value
        check(unknown(delayed.behaviorEnabled), "ordinary refresh cannot clear uncertainty")
        let reopened = SettingsLoader()
        let reopen = Task { await reopened.refreshBehavior() }
        await wait { WispClient.reads.count == 4 }
        WispClient.read(3, ["enabled": false])
        await reopen.value
        check(unknown(reopened.behaviorEnabled), "reopening cannot turn a raw snapshot into proof")
        let fenced = Task { await reopened.refreshBehavior() }
        await wait { WispClient.reads.count == 5 }
        WispClient.read(4, WispClient.receipt(false))
        await fenced.value
        check(reopened.behaviorEnabled == false, "fresh fence can establish OFF")

        let serial = await confirmed(false)
        serial.setBehaviorEnabled(true)
        serial.setBehaviorEnabled(false)
        await wait { WispClient.writes.count == 1 }
        await settle()
        check(WispClient.writes.count == 1 && WispClient.reads.count == 1,
              "unresolved writes must be serialized")
        WispClient.write(0, true)
        await settle()
        check(serial.behaviorEnabled == true, "admitted write determines confirmed state")

        let stale = await confirmed(true)
        let oldRead = Task { await stale.refreshBehavior() }
        await wait { WispClient.reads.count == 2 }
        let newer = await begin(stale, false)
        WispClient.write(newer, false)
        await settle()
        WispClient.read(1, WispClient.receipt(true))
        await oldRead.value
        check(stale.behaviorEnabled == false, "stale status must not replace newer consent")

        let unavailable = await confirmed(true)
        let refresh = Task { await unavailable.refreshBehavior() }
        await wait { WispClient.reads.count == 2 }
        WispClient.read(1, nil)
        await refresh.value
        check(unknown(unavailable.behaviorEnabled), "failed refresh withdraws stale certainty")

        let unproved = await confirmed(true)
        let off = await begin(unproved, false)
        WispClient.writeRaw(off, ["enabled": false])
        await wait { WispClient.reads.count == 2 }
        WispClient.read(1, nil)
        await settle()
        check(unknown(unproved.behaviorEnabled), "unproved write ACK cannot certify OFF")

        WispClient.reset()
        let noFence = SettingsLoader()
        noFence.setBehaviorEnabled(true)
        await wait { WispClient.reads.count == 1 }
        WispClient.read(0, ["enabled": false, "updates_settled": true, "control_epoch": "invalid"])
        await settle()
        check(WispClient.writes.isEmpty, "enable without a valid control epoch must never be sent")
        check(unknown(noFence.behaviorEnabled), "invalid fence remains unknown")

        // The permit must come from the state visible at the click. A delayed
        // preflight from an old window must not mint a new permit and revive
        // an older enable intent after a newer window has confirmed OFF.
        let boundIntent = await confirmed(false)
        boundIntent.setBehaviorEnabled(true)
        await settle()
        check(WispClient.reads.count == 1, "enable intent must not mint a later permit")
        check(WispClient.writes.count == 1, "enable must send its already captured permit")
        // Drain the old implementation's extra preflight to retain a semantic
        // RED instead of hanging on an intentionally controlled continuation.
        if WispClient.reads.count == 2 { WispClient.read(1, WispClient.receipt(false)) }
        await wait { WispClient.writes.count == 1 }
        WispClient.write(0, true)
        await settle()

        // Delay the old captured ON transport, then confirm OFF in a fresh
        // Settings instance. Releasing the old request may only reconcile; it
        // cannot mint/replay ON with the new window's token.
        let earlier = await confirmed(false)
        earlier.setBehaviorEnabled(true)
        await wait { WispClient.writes.count == 1 }
        let oldEpoch = WispClient.writes[0].epoch
        let latest = SettingsLoader()
        let latestRead = Task { await latest.refreshBehavior() }
        await wait { WispClient.reads.count == 2 }
        let newEpoch = String(repeating: "b", count: 64)
        WispClient.read(1, WispClient.receipt(false, epoch: newEpoch))
        await latestRead.value
        check(latest.behaviorEnabled == false, "new Settings instance confirms fenced OFF")
        check(oldEpoch == WispClient.epoch, "old request retains only its pre-barrier token")
        WispClient.write(0, nil) // Actual backend stale-token refusal is tested over ASGI.
        await wait { WispClient.reads.count == 3 }
        WispClient.read(2, WispClient.receipt(false, epoch: newEpoch))
        await settle()
        check(WispClient.writes.count == 1 && earlier.behaviorEnabled == false,
              "old enable does not replay after receiving a newer OFF receipt")
        latest.setBehaviorEnabled(true) // A distinct, explicit new user action.
        await wait { WispClient.writes.count == 2 }
        check(WispClient.writes[1].epoch == newEpoch, "new action captures its own visible token")
        WispClient.write(1, true)
        await settle()
        check(latest.behaviorEnabled == true, "fresh explicit action remains functional")
        WispClient.reset()
        precondition(failures == 0, "\(failures) consent checks failed")
        print("synthetic global consent checks passed: \(checks)")
    }
}
