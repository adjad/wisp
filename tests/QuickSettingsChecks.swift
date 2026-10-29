// Contract for quick settings' lines (app/Sources/WispApp/QuickSettingsText.swift).
// Compiled with NotchTabChecks.swift, which calls quickSettingsChecks. Pure logic:
// never launches Wisp or contacts its service.
import Foundation

func quickSettingsChecks(_ check: (Bool, String) -> Void) {
    // Full access: on is amber "On", unconfirmed is never "Off", pending waits.
    check(QuickSettingsText.fullAccess(on: true, pending: false) == ("On", "Wisp acts without asking first"), "Full access on")
    check(QuickSettingsText.fullAccess(on: false, pending: false).status == "Off", "Full access off")
    let unconfirmed = QuickSettingsText.fullAccess(on: nil, pending: false)
    check(unconfirmed.status == nil && unconfirmed.rest.hasPrefix("Not confirmed"), "unconfirmed Full access is never off")
    check(QuickSettingsText.fullAccess(on: true, pending: true).status == nil, "a pending change shows no status word")

    // Save power when away: says it caps background work only while away;
    // unconfirmed is never "off"; the help names every cap and the 10 minutes.
    check(QuickSettingsText.saveWhenAway(on: true, pending: false).contains("on battery")
          && QuickSettingsText.saveWhenAway(on: true, pending: false).contains("while you're away"), "Save power on")
    check(QuickSettingsText.saveWhenAway(on: false, pending: false) == "Wisp always runs at full speed", "Save power off")
    check(QuickSettingsText.saveWhenAway(on: nil, pending: false) == "Not confirmed", "unconfirmed Save power is never off")
    check(QuickSettingsText.saveWhenAway(on: true, pending: true).hasPrefix("Updating"), "a pending Save power change waits")
    let saveHelp = QuickSettingsText.saveWhenAwayHelp
    check(saveHelp.contains("10 minutes") && saveHelp.contains("10 W"), "Save power help names when and how much")
    check(saveHelp.contains("On battery") && saveHelp.contains("15 W") && saveHelp.contains("chat reply")
          && saveHelp.contains("Brief"), "Save power help names the battery cap and what it covers")
    check(saveHelp.contains("ends as soon as"), "Save power help says the cap ends with the work")
    check(saveHelp.contains("another app") && saveHelp.contains("GPU"), "Save power help says a busy GPU lifts the cap")
    check(!saveHelp.contains("20 W") && !saveHelp.contains("full power"),
          "Save power help no longer says replies always get full power")

    // Learn my routines: "stays on this Mac" only when nothing learned leaves it.
    let local = QuickSettingsText.learning(enabled: true, changing: false, status: nil, remote: false, phraser: true)
    check(local == ("From calendar, reminders and how you use Wisp", "stays on this Mac"), "local learning stays on this Mac")
    for phraser in [true, false] {
        let remote = QuickSettingsText.learning(enabled: true, changing: false, status: nil, remote: true, phraser: phraser)
        check(remote.local == nil && remote.text.contains("inference server"), "remote learning never claims it stays on this Mac")
    }
    check(QuickSettingsText.learning(enabled: true, changing: false, status: nil, remote: true, phraser: true).text.contains("titles"),
          "the phraser sends titles and says so")
    check(QuickSettingsText.learning(enabled: nil, changing: false, status: nil, remote: false, phraser: true).text.hasPrefix("Not confirmed"),
          "unknown learning is not confirmed, never off")
    check(QuickSettingsText.learning(enabled: false, changing: false, status: nil, remote: false, phraser: true).text.hasPrefix("Off"),
          "learning off")
    check(QuickSettingsText.learning(enabled: true, changing: true, status: "Turning learning on. Waiting for confirmation…",
                                     remote: false, phraser: true).text.hasPrefix("Turning learning on"), "a change in flight shows its status")

    check(QuickSettingsText.brief(pending: false, confirmed: true) == "Shows up in Brief, and in a notification", "brief line")
    check(QuickSettingsText.brief(pending: false, confirmed: false).contains("not confirmed"), "unconfirmed brief times")
    check(QuickSettingsText.brief(pending: true, confirmed: true).hasPrefix("Saving"), "brief times saving")

    // Brief time pills and labels.
    check(QuickSettingsText.pill(8) == ("AM", "8:00") && QuickSettingsText.pill(20) == ("PM", "8:00"), "AM 8:00 and PM 8:00")
    check(QuickSettingsText.pill(0) == ("AM", "12:00") && QuickSettingsText.pill(12) == ("PM", "12:00"), "midnight and noon")
    check(QuickSettingsText.hourLabel(0) == "12 AM" && QuickSettingsText.hourLabel(13) == "1 PM", "hour labels")

    // Model memory: only measured sizes ever become a number or a bar.
    let gib: Int64 = 1_073_741_824
    let physical = UInt64(24 * gib)
    let one = ModelMemory(models: [.init(id: "Mellum2.1-8B-oQ4e", bytes: 6 * gib)])
    let line = QuickSettingsText.memory(one, physicalBytes: physical)
    check(line.text == "Mellum2.1 · 6.0 GB in use · reloads on next ask", "a measured model shows its GB")
    check(line.fraction.map { abs($0 - 0.25) < 0.0001 } == true, "the bar is the share of this Mac's memory")
    let unmeasured = QuickSettingsText.memory(ModelMemory(models: [.init(id: "Ling-3.0-tiny", bytes: 4 * gib), .init(id: "embed", bytes: nil)]),
                                              physicalBytes: physical)
    check(unmeasured.text == "Ling +1 · loaded · reloads on next ask" && unmeasured.fraction == nil,
          "an unmeasured model shows no number and no bar")
    check(QuickSettingsText.memory(ModelMemory(models: []), physicalBytes: physical) == ("Not loaded", nil), "nothing loaded")
    let unknown = QuickSettingsText.memory(nil, physicalBytes: physical)
    check(unknown.fraction == nil && !unknown.text.contains("GB"), "unknown memory never shows a number")
    check(QuickSettingsText.memory(ModelMemory(models: [.init(id: "huge", bytes: 99 * gib)]), physicalBytes: physical).fraction == 1,
          "the bar never overflows")
    check(QuickSettingsText.shortName("mlx-community/Qwen3.6-35B") == "Qwen3.6" && QuickSettingsText.shortName("-x") == "x",
          "short model names")

    // The service's reply.
    check(ModelMemory.parse(["available": false, "models": []]) == nil, "an unavailable engine is unknown")
    check(ModelMemory.parse(nil) == nil, "an unreadable reply is unknown")
    let parsed = ModelMemory.parse(["available": true, "models": [
        ["id": "a", "bytes": 5], ["id": "b", "bytes": NSNull()], ["id": "c", "bytes": true], ["id": "d", "bytes": 0], ["bytes": 1],
    ]])
    check(parsed?.models == [.init(id: "a", bytes: 5), .init(id: "b", bytes: nil), .init(id: "c", bytes: nil), .init(id: "d", bytes: nil)],
          "only positive measured sizes count")
    check(parsed?.totalBytes == nil && ModelMemory(models: [.init(id: "a", bytes: 5)]).totalBytes == 5, "a total needs every size")
}
