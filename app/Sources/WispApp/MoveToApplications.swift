import AppKit
import Foundation

// "Move Wisp to Applications?", the way other Mac apps ask when they are opened
// from Downloads. Wisp from the release ZIP runs from Downloads, or from a
// temporary read-only copy macOS makes for a downloaded app (App Translocation),
// where it can't stay put, open at login reliably or be found again.
//
// The rules:
//   - Asked once at launch, before anything else starts, and only from a place a
//     download lands (Downloads, Desktop, Documents, a disk image, or macOS's
//     temporary copy). A build run from anywhere else is never asked.
//   - Nothing moves without the person's click. "Don't ask again" is remembered.
//   - A Wisp already in Applications is replaced only after its own question,
//     and goes to the Trash, never deleted. Chats, memory and settings live in
//     the home folder (~/.moe and Wisp's preferences), so a move never touches them.
//   - When Applications needs an administrator, Wisp says so and shows both
//     folders in Finder for a drag, instead of asking for a password itself.
enum MoveToApplications {
    static let declinedKey = "wisp.moveToApplications.declined"
    static let destination = "/Applications/Wisp.app"

    enum Place: Equatable {
        /// /Applications or ~/Applications: nothing to offer.
        case applications
        /// macOS's temporary copy of a downloaded app.
        case translocated
        /// Downloads, Desktop or Documents.
        case downloaded
        /// A mounted disk image (/Volumes): an app there is meant to be dragged out,
        /// and often carries no quarantine mark of its own.
        case diskImage
        /// Anywhere else (a build folder): never asked.
        case elsewhere
    }

    /// Pure: where a bundle at `bundlePath` runs from.
    static func place(bundlePath: String, home: String, translocated: Bool) -> Place {
        if translocated || bundlePath.contains("/AppTranslocation/") { return .translocated }
        let home = home.hasSuffix("/") ? String(home.dropLast()) : home
        for folder in ["/Applications/", home + "/Applications/"] where bundlePath.hasPrefix(folder) {
            return .applications
        }
        if bundlePath.hasPrefix("/Volumes/") { return .diskImage }
        for folder in [home + "/Downloads/", home + "/Desktop/", home + "/Documents/"]
            where bundlePath.hasPrefix(folder) {
            return .downloaded
        }
        return .elsewhere
    }

    /// Pure: whether launch asks. A disk image is always asked about (Release
    /// Auditor, 4743f581). Otherwise only a real download is: macOS marks
    /// one with com.apple.quarantine (and runs a marked app from a temporary copy).
    /// A preview build, or one built on this Mac (no mark, say in a repo on the
    /// Desktop), is never asked, so a move can't Trash build output or replace the
    /// re-signed local install and its permissions.
    static func offers(place: Place, declined: Bool, isAppBundle: Bool,
                       quarantined: Bool, previewBuild: Bool) -> Bool {
        guard isAppBundle, !declined, !previewBuild else { return false }
        return place == .translocated || place == .diskImage || (place == .downloaded && quarantined)
    }

    /// Whether macOS marked this bundle as downloaded.
    static func isQuarantined(_ url: URL) -> Bool {
        getxattr(url.path, "com.apple.quarantine", nil, 0, 0, 0) >= 0
    }

    /// The offer's sentence, saying where Wisp is running from. Pure.
    static func offerDetail(place: Place) -> String {
        let from = place == .translocated ? "Wisp is running from a temporary copy macOS made of the download."
            : place == .diskImage ? "Wisp is running from a disk image."
            : "Wisp is running from the folder it was downloaded to."
        return from + " In Applications it stays put, can open when you log in, and is easy to find. "
            + "Wisp reopens by itself after the move. Your chats, memory and settings aren't affected."
    }

    /// The question when Applications already has a Wisp. Pure.
    static func replaceDetail(existing: String?, this: String?) -> String {
        let versions: String
        switch (existing, this) {
        case let (old?, new?) where old != new: versions = "The one there is \(old); this one is \(new). "
        case let (old?, _?): versions = "Both are \(old). "
        default: versions = ""
        }
        return versions + "Replace it with this one? The old one goes to the Trash, so you can get it back. "
            + "Your chats, memory and settings are kept in your home folder and stay as they are."
    }

    static let adminDetail = "Applications on this Mac needs an administrator's OK. In the Finder windows that "
        + "open, drag Wisp into Applications (macOS asks for the password), then open it from there."

    /// What happens when a step fails for a reason other than permissions, saying
    /// truthfully whether the Wisp that was in Applications is now in the Trash. Pure.
    static func failureDetail(_ reason: String, oldWispTrashed: Bool) -> String {
        "Wisp couldn't move itself: \(reason) "
            + (oldWispTrashed ? "The Wisp that was in Applications is in the Trash; put it back from there if you need it. "
                              : "Nothing was removed. ")
            + "You can drag Wisp into Applications in Finder instead."
    }

    /// A move that stopped part-way, and whether it had already put the old Wisp in the Trash.
    struct MoveFailure: Error {
        let underlying: Error
        let oldWispTrashed: Bool
    }

    /// Whether a failed write means "needs an administrator". Pure.
    static func needsAdmin(_ error: Error) -> Bool {
        let error = error as NSError
        if error.domain == NSCocoaErrorDomain,
           [NSFileWriteNoPermissionError, NSFileReadNoPermissionError, NSFileWriteVolumeReadOnlyError].contains(error.code) {
            return true
        }
        let underlying = error.userInfo[NSUnderlyingErrorKey] as? NSError
        return [error, underlying].contains { $0?.domain == NSPOSIXErrorDomain && ($0?.code == Int(EACCES) || $0?.code == Int(EPERM)) }
    }

    /// "0.5.0 (3075)" from a bundle's Info.plist, or nil.
    static func version(at path: String) -> String? {
        guard let info = Bundle(path: path)?.infoDictionary,
              let short = info["CFBundleShortVersionString"] as? String else { return nil }
        return (info["CFBundleVersion"] as? String).map { "\(short) (\($0))" } ?? short
    }

    // MARK: Launch

    enum Outcome { case stayed, relaunching }

    /// Asks, and on the person's click moves Wisp into Applications and arranges
    /// the relaunch. `.relaunching` means the caller must quit now (nothing has
    /// started yet). Runs at the top of launch, on the main thread.
    @MainActor static func offerIfNeeded(defaults: UserDefaults = .standard) -> Outcome {
        let bundle = Bundle.main.bundleURL
        let translocation = Translocation.original(of: bundle)
        let place = place(bundlePath: bundle.path, home: NSHomeDirectory(), translocated: translocation.translocated)
        guard offers(place: place, declined: defaults.bool(forKey: declinedKey),
                     isAppBundle: bundle.pathExtension == "app",
                     quarantined: isQuarantined(bundle),
                     previewBuild: Bundle.main.object(forInfoDictionaryKey: "WispPreviewBuild") as? Bool == true)
        else { return .stayed }
        NSApp.activate(ignoringOtherApps: true)
        let ask = NSAlert()
        ask.messageText = "Move Wisp to Applications?"
        ask.informativeText = offerDetail(place: place)
        ask.addButton(withTitle: "Move to Applications")
        ask.addButton(withTitle: "Not Now")
        ask.showsSuppressionButton = true
        ask.suppressionButton?.title = "Don't ask again"
        let answer = ask.runModal()
        if ask.suppressionButton?.state == .on { defaults.set(true, forKey: declinedKey) }
        guard answer == .alertFirstButtonReturn else {
            OnboardingTimeline.record("move to Applications declined")
            return .stayed
        }
        // The folder the person sees: the download, not macOS's temporary copy.
        let original = translocation.original ?? (place == .translocated ? nil : bundle)
        let fm = FileManager.default
        if fm.fileExists(atPath: destination) {
            let replace = NSAlert()
            replace.messageText = "There's already a Wisp in Applications"
            replace.informativeText = replaceDetail(existing: version(at: destination), this: version(at: bundle.path))
            replace.addButton(withTitle: "Replace")
            replace.addButton(withTitle: "Cancel")
            guard replace.runModal() == .alertFirstButtonReturn else {
                OnboardingTimeline.record("move to Applications kept the existing Wisp")
                return .stayed
            }
        }
        do {
            try move(from: bundle, original: original)
        } catch {
            let trashed = (error as? MoveFailure)?.oldWispTrashed == true
            let error = (error as? MoveFailure)?.underlying ?? error
            NSLog("Wisp: move to Applications failed: \(error.localizedDescription)")
            let failed = NSAlert()
            if needsAdmin(error) && !trashed {
                failed.messageText = "Wisp needs an administrator to move it"
                failed.informativeText = adminDetail
                failed.addButton(withTitle: "Show in Finder")
                failed.addButton(withTitle: "Not Now")
                if failed.runModal() == .alertFirstButtonReturn {
                    NSWorkspace.shared.open(URL(fileURLWithPath: "/Applications", isDirectory: true))
                    NSWorkspace.shared.activateFileViewerSelecting([original ?? bundle])
                }
            } else {
                failed.messageText = "Wisp couldn't move itself"
                failed.informativeText = failureDetail(error.localizedDescription, oldWispTrashed: trashed)
                failed.addButton(withTitle: "OK")
                failed.runModal()
            }
            OnboardingTimeline.record("move to Applications failed")
            return .stayed
        }
        OnboardingTimeline.record("moved to Applications")
        relaunchAfterQuit(URL(fileURLWithPath: destination))
        return .relaunching
    }

    /// Copies the running bundle beside the destination, then swaps it in: an
    /// existing Wisp goes to the Trash only once the new copy is complete. The
    /// downloaded original goes to the Trash last, so a failure leaves it as it was.
    /// `target` is the real destination except in tests/FirstRunSetupChecks.swift.
    static func move(from bundle: URL, original: URL?, target: URL = URL(fileURLWithPath: destination)) throws {
        let fm = FileManager.default
        let staging = target.deletingLastPathComponent()
            .appendingPathComponent(".Wisp-moving-\(UUID().uuidString).app")
        var oldTrashed = false
        do {
            try fm.copyItem(at: bundle, to: staging)
            if fm.fileExists(atPath: target.path) {
                try fm.trashItem(at: target, resultingItemURL: nil)
                oldTrashed = true
            }
            try fm.moveItem(at: staging, to: target)
        } catch {
            // Wisp's own half-made copy (a copy can stop part-way), never the person's.
            if fm.fileExists(atPath: staging.path) { try? fm.removeItem(at: staging) }
            throw MoveFailure(underlying: error, oldWispTrashed: oldTrashed)
        }
        // The quarantine mark a downloaded app carries is what makes macOS run it
        // from a temporary copy; the person has now chosen where it lives. The app
        // is the same signed bundle macOS already checked at this launch.
        let xattr = Process()
        xattr.executableURL = URL(fileURLWithPath: "/usr/bin/xattr")
        xattr.arguments = ["-dr", "com.apple.quarantine", target.path]
        try? xattr.run()
        xattr.waitUntilExit()
        if let original, original.standardizedFileURL != target.standardizedFileURL {
            try? fm.trashItem(at: original, resultingItemURL: nil)
        }
    }

    /// Opens the moved Wisp once this one has fully quit, so the new copy never
    /// meets a running one (it would hand over to it and quit).
    private static func relaunchAfterQuit(_ app: URL) {
        let wait = Process()
        wait.executableURL = URL(fileURLWithPath: "/bin/sh")
        wait.arguments = ["-c", "while /bin/kill -0 \"$1\" 2>/dev/null; do /bin/sleep 0.2; done; /usr/bin/open \"$2\"",
                          "sh", String(ProcessInfo.processInfo.processIdentifier), app.path]
        try? wait.run()
    }
}

/// macOS's App Translocation, read through Security's own calls (looked up at
/// run time: they are not in the public headers). Nil original when it can't say.
enum Translocation {
    private typealias IsTranslocated = @convention(c) (CFURL, UnsafeMutablePointer<Bool>,
                                                       UnsafeMutablePointer<Unmanaged<CFError>?>?) -> Bool
    private typealias OriginalPath = @convention(c) (CFURL, UnsafeMutablePointer<Unmanaged<CFError>?>?)
        -> Unmanaged<CFURL>?

    static func original(of url: URL) -> (translocated: Bool, original: URL?) {
        guard let security = dlopen("/System/Library/Frameworks/Security.framework/Security", RTLD_LAZY) else {
            return (url.path.contains("/AppTranslocation/"), nil)
        }
        defer { dlclose(security) }
        guard let isSym = dlsym(security, "SecTranslocateIsTranslocatedURL"),
              let originalSym = dlsym(security, "SecTranslocateCreateOriginalPathForURL") else {
            return (url.path.contains("/AppTranslocation/"), nil)
        }
        var translocated = false
        guard unsafeBitCast(isSym, to: IsTranslocated.self)(url as CFURL, &translocated, nil), translocated else {
            return (false, nil)
        }
        let original = unsafeBitCast(originalSym, to: OriginalPath.self)(url as CFURL, nil)?.takeRetainedValue() as URL?
        return (true, original)
    }
}
