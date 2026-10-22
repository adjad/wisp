import SwiftUI
import UIKit

@main
struct WispPhoneApp: App {
    @State private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ChatView()
                .environment(model)
                .preferredColorScheme(.dark)
                .tint(Theme.teal)
                // Opening a `wisp-pair://` link (e.g. scanning the Mac's code with Camera) offers to pair.
                .onOpenURL { url in try? model.offerPairing(from: url.absoluteString) }
                .alert("Pair with \(model.pairingOffer?.macName ?? "your Mac")?",
                       isPresented: Binding(get: { model.pairingOffer != nil }, set: { if !$0 { model.pairingOffer = nil } })) {
                    Button("Pair") { model.confirmPairing() }
                    Button("Cancel", role: .cancel) { model.pairingOffer = nil }
                } message: {
                    Text("This iPhone will send its Wisp requests to \(model.pairingOffer?.addressDescription ?? "?") over Tailscale and can use that Mac's mail, calendar and tools. Pair only with a code you just made on your own Mac.")
                }
                .task { await model.refreshMac() }
                .onReceive(NotificationCenter.default.publisher(for: UIApplication.didReceiveMemoryWarningNotification)) { _ in
                    model.didReceiveMemoryWarning()
                }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .background { model.didEnterBackground() }
            if phase == .active, model.engine == .mac { Task { await model.refreshMac() } }
        }
    }
}
