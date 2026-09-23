import AppKit
import SwiftUI

@main
struct LingLocalApp: App {
    @StateObject private var controller = EngineProcessController()
    @NSApplicationDelegateAdaptor(AppLifecycle.self) private var lifecycle

    var body: some Scene {
        WindowGroup {
            ContentView(controller: controller)
                .frame(minWidth: 900, minHeight: 640)
                .onAppear { lifecycle.controller = controller }
        }
        .windowResizability(.contentSize)
    }
}

final class AppLifecycle: NSObject, NSApplicationDelegate {
    weak var controller: EngineProcessController?

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let controller, controller.isOwnedProcessRunning else { return .terminateNow }
        controller.shutdownOwnedProcess { shouldTerminate in
            NSApp.reply(toApplicationShouldTerminate: shouldTerminate)
        }
        return .terminateLater
    }
}
