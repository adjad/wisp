import Foundation

/// PanelKeyPolicy: an open the person asks for always takes the keyboard; a press in the panel
/// always does; "without the keyboard" never outlives one open. Pure; compiled with the policy alone.
@main
struct PanelKeyPolicyChecks {
    static func main() {
        var count = 0, failed = 0
        func check(_ c: Bool, _ m: String) { count += 1; if !c { failed += 1; print("FAIL: \(m)") } }
        typealias P = PanelKeyPolicy

        check(P.opening(revealsWithoutKey: false) == .takeKeyboard, "an asked-for open takes the keyboard")
        check(P.opening(revealsWithoutKey: true) == .showOnly, "an unasked open leaves the keyboard alone")
        check(!P.flagAfterReveal(true) && !P.flagAfterReveal(false), "the flag is cleared by every open and close")

        // A sequence: an unasked open, then the person opens it again: the second one takes the keyboard.
        var flag = true
        var outcomes: [P.Opening] = []
        for _ in 0..<3 {
            outcomes.append(P.opening(revealsWithoutKey: flag))
            flag = P.flagAfterReveal(flag)
        }
        check(outcomes == [.showOnly, .takeKeyboard, .takeKeyboard], "without-keyboard lasts exactly one open")

        // Every press while the panel isn't key, or while another app is in front, takes the keyboard.
        for key in [false, true] {
            for front in [false, true] {
                let takes = P.pressTakesKeyboard(isPress: true, isKey: key, wispFrontmost: front)
                check(takes == (!key || !front), "press: key=\(key) frontmost=\(front)")
                check(!P.pressTakesKeyboard(isPress: false, isKey: key, wispFrontmost: front),
                      "a non-press event never changes the keyboard")
            }
        }
        check(P.pressTakesKeyboard(isPress: true, isKey: true, wispFrontmost: false),
              "key in AppKit but another app in front (macOS 27.3): the press still takes it")

        check(P.activates(wispFrontmost: false) && !P.activates(wispFrontmost: true), "activate only when not frontmost")
        check(P.returnsKeyboard(otherWispWindowKey: false) && !P.returnsKeyboard(otherWispWindowKey: true),
              "close returns the keyboard unless another Wisp window is in use")

        if failed > 0 { print("PanelKeyPolicyChecks: \(failed) of \(count) failed"); exit(1) }
        print("PanelKeyPolicyChecks: \(count) checks passed")
    }
}
