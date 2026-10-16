import Foundation

// The actual SettingsLoader consent properties/methods are compiled separately.
// This client exposes controlled continuations only; no HTTP or native readers.
@MainActor final class WispClient {
    struct Read {
        var reply: CheckedContinuation<[String: Any]?, Never>?
    }
    struct Write {
        let enabled: Bool
        var reply: CheckedContinuation<Bool?, Never>?
    }
    static var reads: [Read] = []
    static var writes: [Write] = []

    func behaviorSettings() async -> [String: Any]? {
        await withCheckedContinuation { Self.reads.append(Read(reply: $0)) }
    }
    func setBehaviorEnabled(_ enabled: Bool) async -> Bool? {
        await withCheckedContinuation { Self.writes.append(Write(enabled: enabled, reply: $0)) }
    }
    static func read(_ index: Int, _ value: [String: Any]?) {
        let continuation = reads[index].reply!
        reads[index].reply = nil
        continuation.resume(returning: value)
    }
    static func write(_ index: Int, _ value: Bool?) {
        let continuation = writes[index].reply!
        writes[index].reply = nil
        continuation.resume(returning: value)
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
        WispClient.read(0, ["enabled": enabled, "remote_active": true, "phraser": false])
        await refresh.value
        check(loader.behaviorEnabled == enabled, "authoritative read establishes consent")
        return loader
    }
    static func main() async {
        check(unknown(SettingsLoader().behaviorEnabled), "first render must not invent consent")

        // A rejected/lost disable and an unavailable confirming read cannot
        // establish OFF, even when the user requested it.
        let failed = await confirmed(true)
        failed.setBehaviorEnabled(false)
        await wait { WispClient.writes.count == 1 }
        check(failed.behaviorEnabled != false, "pending disable must not claim OFF")
        check(!BehaviorPrivacyCopy(loader: failed).text.hasPrefix("Off."),
              "pending write must not display the OFF promise")
        WispClient.write(0, nil)
        await wait { WispClient.reads.count == 2 }
        WispClient.read(1, nil)
        await settle()
        check(unknown(failed.behaviorEnabled), "double failure must be explicitly unknown")
        check(!BehaviorPrivacyCopy(loader: failed).text.hasPrefix("Off."),
              "double failure must not promise that learning stopped")

        // A fresh explicit retry can establish OFF; malformed later status
        // cannot preserve a stale confirmed position.
        failed.setBehaviorEnabled(false)
        await wait { WispClient.writes.count == 2 }
        WispClient.write(1, false)
        await settle()
        check(failed.behaviorEnabled == false, "successful retry confirms OFF")
        check(BehaviorPrivacyCopy(loader: failed).text.hasPrefix("Off."),
              "confirmed OFF retains its clear explanation")
        let malformed = Task { await failed.refreshBehavior() }
        await wait { WispClient.reads.count == 3 }
        WispClient.read(2, ["phraser": true])
        await malformed.value
        check(unknown(failed.behaviorEnabled), "malformed consent response is unknown")

        // A lost write response can be reconciled by an authoritative read.
        let recovered = await confirmed(true)
        recovered.setBehaviorEnabled(false)
        await wait { WispClient.writes.count == 1 }
        WispClient.write(0, nil)
        await wait { WispClient.reads.count == 2 }
        WispClient.read(1, ["enabled": false])
        await settle()
        check(recovered.behaviorEnabled == false, "confirming GET reconciles a lost response")

        // Ignore a second conflicting intent while the first request is still
        // unresolved. The UI disables the control over this same interval.
        let serial = await confirmed(false)
        serial.setBehaviorEnabled(true)
        serial.setBehaviorEnabled(false)
        await wait { !WispClient.writes.isEmpty }
        await settle()
        check(WispClient.writes.count == 1, "unresolved writes must be serialized")
        for i in WispClient.writes.indices { WispClient.write(i, WispClient.writes[i].enabled) }
        await settle()
        check(serial.behaviorEnabled == true, "the admitted write is the confirmed state")

        // A status read begun before a mutation may finish last. It must never
        // overwrite that newer mutation's confirmed result.
        let stale = await confirmed(true)
        let oldRead = Task { await stale.refreshBehavior() }
        await wait { WispClient.reads.count == 2 }
        stale.setBehaviorEnabled(false)
        await wait { WispClient.writes.count == 1 }
        WispClient.write(0, false)
        await settle()
        WispClient.read(1, ["enabled": true])
        await oldRead.value
        check(stale.behaviorEnabled == false, "stale status must not replace newer consent")

        // A failed refresh also invalidates the display of previously known ON.
        let unavailable = await confirmed(true)
        let refresh = Task { await unavailable.refreshBehavior() }
        await wait { WispClient.reads.count == 2 }
        WispClient.read(1, nil)
        await refresh.value
        check(unknown(unavailable.behaviorEnabled), "failed refresh must withdraw stale certainty")
        WispClient.reset()
        precondition(failures == 0, "\(failures) consent checks failed")
        print("synthetic global consent checks passed: \(checks)")
    }
}
