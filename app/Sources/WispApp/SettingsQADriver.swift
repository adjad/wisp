#if WISP_SETTINGS_QA
import AppKit
import Foundation
import Vision

@MainActor
enum SettingsQADriver {
    private static var checks = 0

    private static func require(_ condition: Bool, _ message: String) throws {
        guard condition else { throw Failure.check(message) }
        checks += 1
    }

    private enum Failure: LocalizedError {
        case check(String)
        var errorDescription: String? {
            if case let .check(message) = self { return message }
            return nil
        }
    }

    private static func setMode(_ mode: String) async throws {
        var request = URLRequest(url: SettingsQAEnvironment.baseURL.appendingPathComponent("__qa/mode"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["mode": mode])
        let (_, response) = try await URLSession.shared.data(for: request)
        try require((response as? HTTPURLResponse)?.statusCode == 200, "Fixture mode \(mode) failed")
    }

    private static func waitForWrite(_ active: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(8)
        while active() && Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        try require(!active(), "Settings write timed out")
    }

    private static func credentialCount() throws -> Int {
        try SettingsQAEnvironment.credentials().count
    }

    private static func saveScreenshot(_ window: NSWindow) throws -> URL {
        guard let view = window.contentView,
              let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
            throw Failure.check("Could not capture the QA Settings window")
        }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        guard let png = bitmap.representation(using: .png, properties: [:]) else {
            throw Failure.check("Could not encode the QA Settings window")
        }
        let url = SettingsQAEnvironment.home.appendingPathComponent("settings.png")
        try png.write(to: url)
        return url
    }

    private static func visibleText(in image: URL) throws -> String {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        try VNImageRequestHandler(url: image).perform([request])
        return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
            .joined(separator: " ")
    }

    static func run(loader: SettingsLoader, window: NSWindow) async {
        let phase = ProcessInfo.processInfo.environment["WISP_SETTINGS_QA_PHASE"] ?? "first"
        let reportURL = SettingsQAEnvironment.home.appendingPathComponent("qa-\(phase)-report.json")
        var report: [String: Any] = [
            "phase": phase,
            "pid": ProcessInfo.processInfo.processIdentifier,
            "bundle_id": Bundle.main.bundleIdentifier ?? "",
            "bundle_path": Bundle.main.bundleURL.path,
            "fixture_home": SettingsQAEnvironment.home.path,
            "fixture_endpoint": SettingsQAEnvironment.baseURL.absoluteString,
        ]
        do {
            try require(Bundle.main.bundleIdentifier == "com.wisp.settings-qa",
                        "Wrong QA bundle identity")
            try await Task.sleep(nanoseconds: 350_000_000)
            if phase == "first" {
                try await firstPhase(loader: loader, window: window)
            } else if phase == "restart" {
                try await restartPhase(loader: loader)
            } else {
                throw Failure.check("Unexpected QA phase")
            }
            report["status"] = "PASS"
        } catch {
            report["status"] = "FAIL"
            report["error"] = error.localizedDescription
        }
        report["checks"] = checks
        report["credentials"] = (try? credentialCount()) ?? -1
        report["cloud_unknown"] = loader.cloudStateUnknown
        report["local_unknown"] = loader.localProviderStateUnknown
        report["cloud_status"] = loader.cloudStatus
        report["local_status"] = loader.localProviderStatus
        let data = try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
        try? data?.write(to: reportURL, options: .atomic)
    }

    private static func firstPhase(loader: SettingsLoader, window: NSWindow) async throws {
        try await setMode("reset")
        await loader.refreshCloud()
        _ = await loader.refreshLocalProvider()
        try require(!loader.cloudStateUnknown && !loader.cloudConnected, "Baseline Cloud state unknown")

        loader.cloudModelID = "qa-model"
        loader.cloudRoles = ["reasoning"]
        loader.cloudAPIKey = "synthetic-key-1"
        try await setMode("pre_reject")
        loader.connectCloud()
        try await waitForWrite { loader.cloudSaving }
        try require(try credentialCount() == 0, "Pre-save rejection left staged key")
        try require(!loader.cloudConnected && !loader.cloudStateUnknown,
                    "Pre-save rejection was reported as connected or unknown")

        loader.cloudAPIKey = "synthetic-key-1"
        try await setMode("post_500")
        loader.connectCloud()
        try await waitForWrite { loader.cloudSaving }
        try require(try credentialCount() == 1, "Post-save error deleted referenced key")
        try require(loader.cloudConnected && !loader.cloudStateUnknown,
                    "Post-save error did not recover saved Cloud state")
        try require(loader.cloudStatus.localizedCaseInsensitiveContains("test failed"),
                    "Explicit post-save error was falsely reported as a successful test")
        try await setMode("pre_reject")
        loader.connectCloud()
        try await waitForWrite { loader.cloudSaving }
        try require(loader.cloudStatus.localizedCaseInsensitiveContains("test failed")
                    && (try credentialCount()) == 1,
                    "Failed re-test of unchanged saved config was falsely reported connected")
        try await setMode("cloud_partial_post")
        loader.connectCloud()
        try await waitForWrite { loader.cloudSaving }
        try require(loader.cloudConnected && !loader.cloudStateUnknown
                    && (try credentialCount()) == 1,
                    "Partial Cloud POST was not reconciled through complete GET")
        loader.cloudRoles = ["coding"]
        try require(loader.savedCloudRoles == ["reasoning"], "Draft roles replaced saved roles")

        try await setMode("partial_cloud_get")
        await loader.refreshCloud()
        try require(loader.cloudStateUnknown && (try credentialCount()) == 1,
                    "Partial Cloud GET changed known state or removed key")
        try await setMode("empty_cloud_identity")
        await loader.refreshCloud()
        try require(loader.cloudStateUnknown && (try credentialCount()) == 1,
                    "Empty live Cloud credential identity removed key or became known")
        try await setMode("normal")
        await loader.refreshCloud()
        try require(!loader.cloudStateUnknown, "Complete Cloud GET did not recover state")

        loader.localProviderBaseURL = SettingsQAEnvironment.baseURL.absoluteString
        loader.localProviderModelID = "qa-local"
        loader.localProviderRoles = ["reasoning"]
        try await setMode("local_post_500")
        loader.connectLocalProvider()
        try await waitForWrite { loader.localProviderSaving }
        try require(loader.localProviderSavedAssigned && !loader.localProviderStateUnknown,
                    "Post-save Local error did not recover Reasoning assignment")
        loader.localProviderRoles = []
        try require(loader.localProviderSavedAssigned,
                    "Local draft role removed saved Reasoning assignment")
        loader.localProviderRoles = ["reasoning"]
        try await setMode("local_partial_post")
        loader.connectLocalProvider()
        try await waitForWrite { loader.localProviderSaving }
        try require(loader.localProviderSavedAssigned && !loader.localProviderStateUnknown,
                    "Partial Local POST was not reconciled through complete GET")

        try await setMode("local_pre_reject")
        loader.connectLocalProvider()
        try await waitForWrite { loader.localProviderSaving }
        try require(loader.localProviderSavedAssigned && !loader.localProviderStateUnknown,
                    "Rejected re-test erased the previously saved Local binding")
        try require(loader.localProviderStatus.localizedCaseInsensitiveContains("test failed")
                    && !loader.localProviderStatus.localizedCaseInsensitiveContains("recovered"),
                    "Rejected unchanged Local re-test was falsely reported as recovered success")

        try await setMode("local_delete_500")
        loader.disconnectLocalProvider()
        try await waitForWrite { loader.localProviderSaving }
        try require(!loader.localProviderConnected && !loader.localProviderStateUnknown,
                    "Post-disable Local error did not recover disabled state")

        try await setMode("normal")
        loader.localProviderBaseURL = SettingsQAEnvironment.baseURL.absoluteString
        loader.localProviderModelID = "qa-local"
        loader.localProviderRoles = ["reasoning"]
        loader.connectLocalProvider()
        try await waitForWrite { loader.localProviderSaving }
        try require(loader.localProviderSavedAssigned, "Local rebind failed")
        try await setMode("local_bad_delete_2xx")
        loader.disconnectLocalProvider()
        try await waitForWrite { loader.localProviderSaving }
        try require(loader.localProviderSavedAssigned,
                    "Enabled Local DELETE response erased saved Reasoning assignment")

        try await setMode("local_partial_get")
        _ = await loader.refreshLocalProvider()
        try require(loader.localProviderStateUnknown, "Partial Local GET did not show unknown")
        try await Task.sleep(nanoseconds: 300_000_000)
        let screenshot = try saveScreenshot(window)
        let text = try visibleText(in: screenshot)
        try require(text.localizedCaseInsensitiveContains("Assignment unknown"),
                    "Assignment unknown is missing from rendered Settings window: \(text.prefix(300))")

        try await setMode("ambiguous")
        loader.cloudAPIKey = "synthetic-key-2"
        loader.connectCloud()
        try await waitForWrite { loader.cloudSaving }
        try require(loader.cloudStateUnknown && (try credentialCount()) == 2,
                    "Ambiguous GET did not preserve staged key and unknown state")
        try require(SettingsQAEnvironment.pendingStore.string(forKey: "WispPendingCloudCredentialName") != nil,
                    "Ambiguous Cloud operation did not persist pending metadata")
    }

    private static func restartPhase(loader: SettingsLoader) async throws {
        try await setMode("normal")
        await loader.refreshCloud()
        _ = await loader.refreshLocalProvider()
        try require(!loader.cloudStateUnknown && loader.cloudConnected,
                    "Restart did not reconcile saved Cloud state")
        try require(try credentialCount() == 1,
                    "Restart did not retire the previously referenced synthetic key")
        try require(SettingsQAEnvironment.pendingStore.string(forKey: "WispPendingCloudCredentialName") == nil,
                    "Restart did not clear pending metadata")
        try require(!loader.localProviderStateUnknown && loader.localProviderConnected
                    && loader.localProviderActive && loader.localProviderSavedAssigned,
                    "Restart did not recover the saved Local Reasoning assignment")
        try require(loader.localProviderBaseURL == SettingsQAEnvironment.baseURL.absoluteString
                    && loader.localProviderModelID == "qa-local",
                    "Restart did not restore the saved Local endpoint and model")
    }
}
#endif
