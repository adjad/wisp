import Foundation

/// LookCapturePath: the picker is used only when it exists and is confirmed to work without the grant;
/// otherwise Look behaves as before. Pure; compiled with the decision alone.
@main
struct LookCapturePathChecks {
    static func main() {
        var count = 0, failed = 0
        func check(_ c: Bool, _ m: String) { count += 1; if !c { failed += 1; print("FAIL: \(m)") } }
        typealias P = LookCapturePath

        for allowed in [false, true] {
            // Default setting (false): shipped behaviour, picker never chosen.
            check(P.choose(pickerAvailable: true, pickerWorksWithoutGrant: false, screenRecordingAllowed: allowed)
                  == (allowed ? .legacy : .needsScreenRecording), "default keeps the old path, allowed=\(allowed)")
            check(P.choose(pickerAvailable: false, pickerWorksWithoutGrant: false, screenRecordingAllowed: allowed)
                  == (allowed ? .legacy : .needsScreenRecording), "no picker, not confirmed, allowed=\(allowed)")
            // Confirmed but no picker on this system: still the old path.
            check(P.choose(pickerAvailable: false, pickerWorksWithoutGrant: true, screenRecordingAllowed: allowed)
                  == (allowed ? .legacy : .needsScreenRecording), "no picker even if confirmed, allowed=\(allowed)")
            // Confirmed and available: picker, with or without the grant (no Screen Recording ask).
            check(P.choose(pickerAvailable: true, pickerWorksWithoutGrant: true, screenRecordingAllowed: allowed)
                  == .picker, "confirmed picker wins, allowed=\(allowed)")
        }

        // The stored setting: absent means false; a written true is read back.
        let suite = "LookCapturePathChecks.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        check(!P.storedPickerWorksWithoutGrant(defaults), "unset setting is false")
        defaults.set(true, forKey: P.pickerWorksWithoutGrantKey)
        check(P.storedPickerWorksWithoutGrant(defaults), "setting true is read")
        defaults.set(false, forKey: P.pickerWorksWithoutGrantKey)
        check(!P.storedPickerWorksWithoutGrant(defaults), "setting false is read")
        defaults.removePersistentDomain(forName: suite)
        check(P.pickerWorksWithoutGrantKey == "wisp.lookPickerWorksWithoutGrant", "key is stable")

        print("look-capture-path: \(count) checks, \(failed) failed")
        exit(failed == 0 ? 0 : 1)
    }
}
