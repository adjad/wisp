import Foundation

// Pure checks for in-app updates (app/Sources/WispApp/SoftwareUpdate.swift),
// compiled without Sparkle: a build updates only with an https feed, a real
// EdDSA key and every privacy and signing switch the pipeline sets; betas follow
// the build's channel until the person chooses; no system profile is sent.
// Nothing here reads defaults or reaches the network.
@main
struct SoftwareUpdateChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }

    // What build-support/pipeline.py writes (a made-up key: 32 zero bytes).
    static let shipped: [String: Any] = [
        "SUFeedURL": "https://raw.githubusercontent.com/adjad/wisp/appcast/appcast.xml",
        "SUPublicEDKey": Data(count: 32).base64EncodedString(),
        "SUEnableAutomaticChecks": false, "SUEnableSystemProfiling": false,
        "SUAutomaticallyUpdate": false, "SURequireSignedFeed": true,
        "SUVerifyUpdateBeforeExtraction": true, "WispUpdateChannel": "beta",
    ]

    static func with(_ key: String, _ value: Any?) -> [String: Any] {
        var info = shipped
        info[key] = value
        return info
    }

    static func main() {
        guard let config = SoftwareUpdate.configuration(info: shipped) else {
            preconditionFailure("The pipeline's Info.plist keys configure updates")
        }
        check(config.feedURL.absoluteString == shipped["SUFeedURL"] as? String && config.buildChannel == "beta",
              "The feed and the build's channel are read")
        check(SoftwareUpdate.configuration(info: with("WispUpdateChannel", nil))?.buildChannel == "stable",
              "No channel recorded means stable")

        // Fail closed: any missing or loosened switch means no updater at all.
        check(SoftwareUpdate.configuration(info: [:]) == nil, "A build without keys cannot update")
        check(SoftwareUpdate.configuration(info: with("SUPublicEDKey", nil)) == nil, "No EdDSA key, no updates")
        check(SoftwareUpdate.configuration(info: with("SUPublicEDKey", "")) == nil, "An empty key is no key")
        check(SoftwareUpdate.configuration(info: with("SUPublicEDKey", Data(count: 16).base64EncodedString())) == nil,
              "A key must be 32 bytes")
        check(SoftwareUpdate.configuration(info: with("SUFeedURL", "http://example.com/appcast.xml")) == nil,
              "The feed must be https")
        check(SoftwareUpdate.configuration(info: with("SUFeedURL", nil)) == nil, "No feed, no updates")
        check(SoftwareUpdate.configuration(info: with("SUFeedURL", "http://127.0.0.1:8123/appcast.xml")) != nil,
              "The local end-to-end test feed on loopback is allowed")
        check(SoftwareUpdate.configuration(info: with("SUFeedURL", "http://localhost.example.com/appcast.xml")) == nil
              && SoftwareUpdate.configuration(info: with("SUFeedURL", "http://192.168.1.2/appcast.xml")) == nil,
              "Plain http anywhere but loopback is refused")
        for key in ["SUEnableAutomaticChecks", "SUEnableSystemProfiling", "SUAutomaticallyUpdate"] {
            check(SoftwareUpdate.configuration(info: with(key, true)) == nil, "\(key) on is refused")
            check(SoftwareUpdate.configuration(info: with(key, nil)) == nil, "\(key) must be set off explicitly")
        }
        for key in ["SURequireSignedFeed", "SUVerifyUpdateBeforeExtraction"] {
            check(SoftwareUpdate.configuration(info: with(key, false)) == nil, "\(key) off is refused")
        }

        // Betas: the build's channel until the person chooses.
        check(SoftwareUpdate.includesBetas(saved: nil, buildChannel: "beta"), "A beta build offers betas")
        check(!SoftwareUpdate.includesBetas(saved: nil, buildChannel: "stable"), "A stable build does not")
        check(!SoftwareUpdate.includesBetas(saved: false, buildChannel: "beta"), "Turning betas off sticks")
        check(SoftwareUpdate.includesBetas(saved: true, buildChannel: "stable"), "Turning betas on sticks")
        check(SoftwareUpdate.allowedChannels(includeBetas: true) == ["beta"], "Betas on allows the beta channel")
        check(SoftwareUpdate.allowedChannels(includeBetas: false).isEmpty, "Betas off: stable items only")

        check(SoftwareUpdate.feedParameters(sendingSystemProfile: true).isEmpty
              && SoftwareUpdate.feedParameters(sendingSystemProfile: false).isEmpty,
              "No feed parameters, even if Sparkle would send a profile")
        check(SoftwareUpdate.menuTitle == "Check for Updates…", "The menu item's title")

        print("software update checks passed")
    }
}
