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

    // Save memory: neutral lines; unconfirmed is never "off"; the help names the
    // 5.5 GB, both timers, the reload on opening Ask, the ~6 s wait, the hourly
    // catch-up and what off means.
    check(QuickSettingsText.saveMemory(on: true, pending: false).contains("memory")
          && QuickSettingsText.saveMemory(on: true, pending: false).contains("not using"), "Save memory on")
    check(QuickSettingsText.saveMemory(on: false, pending: false) == "Wisp's model stays loaded", "Save memory off")
    check(QuickSettingsText.saveMemory(on: nil, pending: false) == "Not confirmed", "unconfirmed Save memory is never off")
    check(QuickSettingsText.saveMemory(on: true, pending: true).hasPrefix("Updating"), "a pending Save memory change waits")
    let memoryHelp = QuickSettingsText.saveMemoryHelp
    check(memoryHelp.contains("5.5 GB") && memoryHelp.contains("30 minutes without using Wisp")
          && memoryHelp.contains("sooner when your Mac runs low on memory"),
          "Save memory help names the size and when it unloads")
    check(memoryHelp.contains("Research and Smart Search keep it loaded"), "Save memory help says running work holds it")
    check(memoryHelp.contains("Opening Ask") && memoryHelp.contains("6 seconds") && memoryHelp.contains("once an hour"),
          "Save memory help names the reload, the wait and the catch-up")
    check(memoryHelp.contains("Off keeps the model loaded, except after you hide the notch"),
          "Save memory help says what off still does")

    // Save memory on the user's oMLX: the row never promises memory it can't free.
    let omlx = SaveMemoryDetail(applies: true, onOMLX: true)
    check(QuickSettingsText.saveMemory(on: true, pending: false, detail: omlx).contains("Gemma memory"),
          "on oMLX it names the Gemma")
    let omlxHelp = QuickSettingsText.saveMemoryHelp(detail: omlx)
    check(omlxHelp.contains("only ever unloads that one model") && omlxHelp.contains("another app has used")
          && omlxHelp.contains("unless another model is loaded"), "the oMLX help says what it leaves alone")
    let holding = SaveMemoryDetail(applies: true, otherApp: true, onOMLX: true)
    check(QuickSettingsText.saveMemory(on: true, pending: false, detail: holding).contains("another app"),
          "another app's use is what keeps it loaded")
    check(QuickSettingsText.saveMemory(on: false, pending: false, detail: holding) == "Wisp's model stays loaded",
          "off stays off while another app uses it")
    for why in ["not_gemma", "pinned", "not_omlx", "not_reported"] {
        let blocked = SaveMemoryDetail(applies: false, whyNot: why)
        for on in [true, false] {
            let line = QuickSettingsText.saveMemory(on: on, pending: false, detail: blocked)
            check(line.hasPrefix("Not active") && !line.contains("Frees"), "\(why) says it isn't active")
        }
        check(QuickSettingsText.saveMemoryHelp(detail: blocked).contains("isn't freeing any memory"),
              "\(why) help doesn't promise memory")
        check(QuickSettingsText.saveMemory(on: nil, pending: false, detail: blocked) == "Not confirmed",
              "\(why) with an unconfirmed switch stays unconfirmed")
    }
    check(QuickSettingsText.saveMemoryHelp(detail: SaveMemoryDetail(applies: true)) == memoryHelp,
          "the built-in engine keeps the general help")
    check(QuickSettingsText.saveMemoryHelp(detail: nil) == memoryHelp, "no reply keeps the general help")
    let reply = #"{"save_memory":true,"applies":false,"why_not":"not_gemma","engine":"omlx","other_app":false}"#
    check(SaveMemoryDetail(json: Data(reply.utf8))
          == SaveMemoryDetail(applies: false, whyNot: "not_gemma", onOMLX: true), "the reply parses")
    check(SaveMemoryDetail(json: Data(#"{"save_memory":true}"#.utf8)) == nil, "no applies, no detail")

    // Save power: says it saves power on battery and while away; unconfirmed
    // is never "off"; the help names the 15 W and 10 W caps and what they cover
    // (background work always, the Brief after 10 minutes away), that light GPU
    // use by other apps doesn't stop them and a busier app lifts them within
    // about a second, and that only the built-in engine is capped.
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
    check(saveHelp.contains("another app") && saveHelp.contains("GPU") && saveHelp.contains("within about a second"),
          "Save power help says another app on the GPU lifts the cap within about a second")
    check(!saveHelp.contains("never slow"), "Save power help doesn't promise other apps never slow down")
    check(saveHelp.contains("use little of the GPU") && !saveHelp.contains("no other app"),
          "Save power help says light use by other apps doesn't stop the cap")
    check(saveHelp.contains("background work always") && saveHelp.contains("plugged in too")
          && saveHelp.contains("built-in engine") && saveHelp.contains("oMLX"),
          "Save power help names the 10 W background cap, always, and the engine it needs")
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

    check(QuickSettingsText.brief(pending: false, confirmed: true) == "In Brief, and as a notification", "daily summary line")
    // The Brief's own pace: the row says the person's hours, and never invents any.
    check(QuickSettingsText.briefUpdates(hours: "9 AM to 5 PM") == "About 4 times a day, 9 AM to 5 PM", "Brief updates line")
    check(QuickSettingsText.briefUpdates(hours: nil) == "About 4 times a day, across your working hours",
          "no answer from the service names no hours")
    check(QuickSettingsText.dailySummaryHelp.contains("doesn't change when the Brief updates"),
          "the summary's times don't claim to set the Brief")

    // One section at a time; a Text size change rebuilds the panel and must keep it.
    check(QuickSettingsSection.allCases.map(\.title) == ["General", "Brief", "Voice", "Power", "Advanced"], "the five sections")
    check(QuickSettingsSection.voice.contents.contains("Text size"), "the Voice section says it holds Text size")
    let sectionSuite = "wisp.quick-settings-section-check"
    if let sectionDefaults = UserDefaults(suiteName: sectionSuite) {
        sectionDefaults.removePersistentDomain(forName: sectionSuite)
        check(QuickSettingsSection.saved(in: sectionDefaults) == .general, "General until one is chosen")
        QuickSettingsSection.voice.save(in: sectionDefaults)
        check(QuickSettingsSection.saved(in: sectionDefaults) == .voice, "the last section comes back")
        sectionDefaults.set("gone", forKey: QuickSettingsSection.defaultsKey)
        check(QuickSettingsSection.saved(in: sectionDefaults) == .general, "an unknown section reads as General")
        sectionDefaults.removePersistentDomain(forName: sectionSuite)
    }
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

    // The Models page's line about the user's own oMLX: only a loaded model warns
    // about memory; an idle or silent server is plain information.
    check(QuickSettingsText.otherEngine(running: false, loaded: true, modelBytes: 4 * gib) == nil, "no other engine, no line")
    let idleOther = QuickSettingsText.otherEngine(running: true, loaded: false, modelBytes: nil)
    check(idleOther?.warns == false && idleOther?.text.contains("no model loaded") == true
          && idleOther?.text.contains("memory") == false, "an idle oMLX never claims memory")
    let silentOther = QuickSettingsText.otherEngine(running: true, loaded: nil, modelBytes: nil)
    check(silentOther?.warns == false && silentOther?.text.contains("memory") == false, "an unknown oMLX never claims memory")
    let busyOther = QuickSettingsText.otherEngine(running: true, loaded: true, modelBytes: 4 * gib)
    check(busyOther?.warns == true && busyOther?.text.contains("(4.0 GB)") == true
          && busyOther?.text.contains("both engines use memory") == true, "a loaded oMLX warns with its size")
    check(QuickSettingsText.otherEngine(running: true, loaded: true, modelBytes: nil)?.text.contains("GB") == false,
          "an unmeasured model shows no size")

    // The Models page's encoder line (/engine's `encoders`).
    check(EncoderResidency.parse(nil) == nil && EncoderResidency.parse(NSNull()) == nil
          && EncoderResidency.parse(["on_demand": 1, "towers": [:]]) == nil, "an unreadable encoders row is unknown")
    check(QuickSettingsText.encoders(nil) == nil, "no encoders row, no line")
    let released = EncoderResidency.parse(["on_demand": true, "idle_release_s": 300, "towers": [
        "audio": ["state": "released", "bytes": 611_900_000], "vision": ["state": "released", "bytes": 335_800_000]]])
    check(released?.towers.count == 2 && released?.towers.allSatisfy { !$0.resident } == true, "both released")
    check(QuickSettingsText.encoders(released)?.contains("none in memory now") == true, "released encoders say so")
    let hearing = EncoderResidency.parse(["on_demand": true, "idle_release_s": 300, "towers": [
        "audio": ["state": "resident", "bytes": 611_900_000], "vision": ["state": "released", "bytes": 335_800_000]]])
    check(QuickSettingsText.encoders(hearing) == "Encoders in memory: voice 612 MB; they leave after 5 min unused",
          "a resident encoder shows its size and when it leaves")
    let loading = EncoderResidency.parse(["on_demand": true, "towers": [
        "vision": ["state": "loading", "bytes": true]]])
    check(QuickSettingsText.encoders(loading) == "Encoders in memory: pictures; they leave when unused",
          "a loading encoder counts as in memory, a bad size shows none")
    let resident = EncoderResidency.parse(["on_demand": false, "reason": "unsupported_model", "towers": [
        "audio": ["state": "resident", "bytes": 5]]])
    check(QuickSettingsText.encoders(resident) == "Voice and picture encoders stay loaded with the model",
          "a model without on-demand encoders keeps them loaded")
    check(QuickSettingsText.encoders(EncoderResidency.parse(["on_demand": false, "towers": [:]])) == nil,
          "a model without encoders has no line")

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
