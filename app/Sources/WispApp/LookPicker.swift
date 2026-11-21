import AppKit
import CoreGraphics
import ScreenCaptureKit

/// "Look" through Apple's content-sharing picker: the person chooses a window or
/// a display in macOS's own picker each time, and Wisp captures one still image
/// of exactly that. Used only when LookCapturePath.choose says `.picker`.
///
///  - Nothing is captured until a selection arrives; cancelling the picker ends
///    the Look quietly (no notice, no attachment).
///  - One still image (SCScreenshotManager), memory only, then the picker is
///    deactivated and its observer removed on every ending.
///  - A password manager's window, or a window with a private-browsing title
///    marker, is refused even though it was picked (the same rule as the
///    existing path: ScreenLook.isSensitive / privateTitle). Window metadata
///    needs macOS 15.2; on 14 to 15.1 the check can't run and the person's own
///    pick stands.
final class LookPicker: NSObject, SCContentSharingPickerObserver, @unchecked Sendable {
    enum Result {
        case captured(Data, label: String?)
        case refused(String)
        case cancelled
        case failed(String)
    }

    /// The picker exists on every macOS Wisp supports (14+); asked anyway so the
    /// decision stays honest if the floor moves.
    static var available: Bool { ProcessInfo.processInfo.isOperatingSystemAtLeast(OperatingSystemVersion(majorVersion: 14, minorVersion: 0, patchVersion: 0)) }

    // Held while a pick is open so the observer outlives the call; main thread only.
    nonisolated(unsafe) private static var current: LookPicker?

    private let lock = NSLock()
    private var continuation: CheckedContinuation<SCContentFilter?, Error>?

    /// Present the picker, then capture what was chosen.
    static func pickAndCapture() async -> Result {
        let filter: SCContentFilter?
        do {
            filter = try await LookPicker().present()
        } catch {
            return .failed("Wisp couldn't open the window picker.")
        }
        guard let filter else { return .cancelled }
        return await capture(filter)
    }

    private func present() async throws -> SCContentFilter? {
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<SCContentFilter?, Error>) in
            lock.lock(); continuation = cont; lock.unlock()
            DispatchQueue.main.async {
                LookPicker.current = self
                let picker = SCContentSharingPicker.shared
                var config = SCContentSharingPickerConfiguration()
                config.allowedPickerModes = [.singleWindow, .singleDisplay]
                config.allowsChangingSelectedContent = false
                if let bundle = Bundle.main.bundleIdentifier { config.excludedBundleIDs = [bundle] }
                picker.defaultConfiguration = config
                picker.maximumStreamCount = 1
                picker.add(self)
                picker.isActive = true
                picker.present()
            }
        }
    }

    /// Ends the pick exactly once: picker off, observer removed, caller resumed.
    private func finish(_ filter: SCContentFilter?, error: Error? = nil) {
        lock.lock()
        let cont = continuation
        continuation = nil
        lock.unlock()
        guard let cont else { return }
        DispatchQueue.main.async {
            let picker = SCContentSharingPicker.shared
            picker.remove(self)
            picker.isActive = false
            if LookPicker.current === self { LookPicker.current = nil }
        }
        if let error { cont.resume(throwing: error) } else { cont.resume(returning: filter) }
    }

    // MARK: SCContentSharingPickerObserver

    func contentSharingPicker(_ picker: SCContentSharingPicker, didUpdateWith filter: SCContentFilter,
                              for stream: SCStream?) {
        finish(filter)
    }

    func contentSharingPicker(_ picker: SCContentSharingPicker, didCancelFor stream: SCStream?) {
        finish(nil)
    }

    func contentSharingPickerStartDidFailWithError(_ error: Error) {
        finish(nil, error: error)
    }

    // MARK: Capture

    private static func capture(_ filter: SCContentFilter) async -> Result {
        var label: String?
        if #available(macOS 15.2, *), let window = filter.includedWindows.first {
            let app = window.owningApplication
            let seen = ScreenLook.Window(number: window.windowID, ownerPID: app?.processID ?? 0,
                                         ownerName: app?.applicationName ?? "", title: window.title ?? "",
                                         bounds: window.frame, layer: 0, alpha: 1,
                                         bundleID: app?.bundleIdentifier ?? "")
            if ScreenLook.isSensitive(seen) {
                let name = ScreenLook.label(app: seen.ownerName, title: "") ?? "That app"
                return .refused("\(name) holds passwords, so Wisp doesn't look at it.")
            }
            if ScreenLook.isBrowser(seen) && ScreenLook.privateTitle(seen.title) {
                let name = ScreenLook.label(app: seen.ownerName, title: "") ?? "a browser"
                return .refused("That's a private browsing window in \(name). Wisp doesn't look at private windows.")
            }
            label = ScreenLook.label(app: seen.ownerName, title: seen.title)
        }
        let config = SCStreamConfiguration()
        let scale = CGFloat(filter.pointPixelScale)
        let rect = filter.contentRect
        if rect.width > 0, rect.height > 0 {
            config.width = max(1, Int((rect.width * scale).rounded()))
            config.height = max(1, Int((rect.height * scale).rounded()))
        }
        config.showsCursor = false
        do {
            let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
            guard let jpeg = MediaAttachment.detailJPEG(image) else {
                return .failed("Wisp couldn't capture that.")
            }
            return .captured(jpeg, label: label)
        } catch {
            return .failed("Wisp couldn't capture that. If macOS asked for Screen Recording, Look needs it on this Mac.")
        }
    }
}
