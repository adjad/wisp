import AppKit

/// The one place the Brief opens a school link in the browser. Called only
/// from a button the person clicked (a READY FOR YOU due row's Open, a SCHOOL
/// row's open icon): never on its own, never prefetched, and nothing here
/// reads the page. The address is checked again here (StudyLinkRule), so a
/// stale or tampered payload cannot widen what the service allowed.
@MainActor
enum StudyLinks {
    /// Opens `raw` in the default browser when it passes the rule; false when refused.
    @discardableResult
    static func open(_ raw: String) -> Bool {
        guard let checked = StudyLinkRule.check(raw) else { return false }
        return NSWorkspace.shared.open(checked.url)
    }
}
