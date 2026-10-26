#if WISP_NOTCH_QA
import SwiftUI

// The notch's setup card in each state of its model download, for NotchQA's
// screenshots and size check (the panel is 640 pt wide and clips what doesn't
// fit). Every request a card could make is refused here: no service, engine or
// network is touched.
@MainActor
enum NotchQASetup {
    /// Answers every request with 503, so nothing in a scene reaches a socket.
    final class Refuse: URLProtocol {
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            let response = HTTPURLResponse(url: request.url!, statusCode: 503, httpVersion: nil, headerFields: nil)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(#"{"detail":"NotchQA fixture"}"#.utf8))
            client?.urlProtocolDidFinishLoading(self)
        }
        override func stopLoading() {}
    }

    static let gemma = "gemma-4-E4B-it-oQ4e-mtp"

    static func status(ram: Int = 24, ready: Bool = false, bundled: Bool = true, installed: Bool = false,
                       fits: Bool = true) -> SetupStatus {
        let label = "Gemma 4 E4B · 4-bit (oQ4e) + MTP"
        let tooSmall = "Wisp's models need a Mac with at least 16 GB of memory, and this one has \(ram) GB. "
            + "If you already run a smaller model in another app, such as Ollama or LM Studio, you can connect it in the detailed setup."
        let models: [String: Any] = [
            "source": "engine",
            "installed": installed ? [["id": gemma, "label": label, "size_gb": 5.5, "fit": "recommended",
                                       "tested": true, "note": ""]] : [],
            "recommended": gemma, "recommended_label": label,
            "use": installed ? gemma : NSNull(), "warning": fits ? NSNull() : tooSmall, "fits": fits,
            "download": fits ? ["model": gemma, "label": label, "size_gb": 5.5, "repo": "Jundot/\(gemma)",
                                "url": "", "command": "hf download Jundot/\(gemma)", "install_hf": ""] as [String: Any]
                             : NSNull(),
            "embedding_download": NSNull(),
        ]
        let modelCheck: [String: Any] = ready
            ? ["id": "model", "state": "ok", "title": "Models are set", "detail": "Every role points at an installed model."]
            : fits ? ["id": "model", "state": "todo", "title": "Download a model", "detail": "No chat model is installed yet.",
                      "action": "download_model"]
                   : ["id": "model", "state": "todo", "title": "No model fits this Mac", "detail": tooSmall,
                      "action": "no_local_fit"]
        let doc: [String: Any] = [
            "ready": ready,
            "hardware": ["chip": "Apple M4", "ram_gb": ram, "tier": ram >= 24 ? "comfortable" : "tight"],
            "omlx": ["installed": true, "running": true, "bundled": bundled, "min_macos": 26,
                     "url": "https://github.com/jundot/omlx", "admin_url": "http://fixture.invalid/admin"],
            "bundled_engine": ["available": true, "minimum_macos": "26.0", "unavailable_reason": NSNull()],
            "models": models,
            "roles": ["fast", "general", "coding", "reasoning"].map {
                ["role": $0, "label": $0.capitalized, "model": gemma, "endpoint": "local"] },
            "engines": [],
            "checks": [["id": "engine", "state": "ok", "title": "Wisp's built-in engine is ready",
                        "detail": "The built-in engine is answering on this Mac."], modelCheck],
            "disclosure": "",
        ]
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try! decoder.decode(SetupStatus.self, from: JSONSerialization.data(withJSONObject: doc))
    }

    static func task(_ state: String, done: Double, total: Double = 5.5e9, speed: Double = 0,
                     message: String? = nil) -> SetupGuideModel.DownloadTask {
        let doc: [String: Any] = ["model": gemma, "state": state, "progress": total > 0 ? done / total * 100 : 0,
                                  "downloaded_bytes": done, "total_bytes": total, "bytes_per_second": speed,
                                  "message": message ?? NSNull()]
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try! decoder.decode(SetupGuideModel.DownloadTask.self, from: JSONSerialization.data(withJSONObject: doc))
    }

    /// One setup model per scene, fed through the real accept path.
    static func model(_ status: SetupStatus, downloads: [SetupGuideModel.DownloadTask] = [],
                      busy: String? = nil) -> SetupGuideModel {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [Refuse.self]
        let setup = SetupGuideModel(session: URLSession(configuration: configuration))
        setup.qaAcceptAutomaticStatus(status)
        setup.downloads = downloads
        setup.busy = busy
        return setup
    }

    static func scenes() -> [(name: String, tab: NotchTab, view: () -> AnyView)] {
        let overlay = OverlayModel()
        overlay.setupCard = .open
        let set: [(String, SetupGuideModel)] = [
            ("setup-download", model(status())),
            ("setup-starting", model(status(), busy: "Starting download…")),
            ("setup-downloading", model(status(), downloads: [task("downloading", done: 2.4e9, speed: 18e6)])),
            ("setup-paused", model(status(), downloads: [task("cancelled", done: 2.4e9)])),
            ("setup-failed", model(status(), downloads: [task("failed", done: 2.4e9,
                                                             message: "The connection to Hugging Face dropped.")])),
            ("setup-finishing", model(status(), downloads: [task("completed", done: 5.5e9)])),
            ("setup-ready", model(status(ready: true, installed: true))),
            ("setup-no-fit", model(status(ram: 8, fits: false))),
            ("setup-external", model(status(bundled: false))),
        ]
        return set.map { name, setup in
            (name, NotchTab.ask, { AnyView(NotchSetupView(model: overlay, setup: setup)) })
        }
    }
}
#endif
