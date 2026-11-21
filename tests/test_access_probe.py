"""Source and logic checks for Full Disk Access's live probe, the reopen prompt, the
single relaunch path, provisional notifications and per-version privacy names.
Reads source only (the pure Swift logic is tests/AccessProbeChecks.swift, compiled
by the native/access-probe gate); nothing here opens a store, System Settings or Wisp."""
import re
from pathlib import Path

from service.tools.window_tools import _accessibility_names

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app/Sources/WispApp"


def _src(name: str) -> str:
    return (APP / name).read_text()


def _sources() -> dict[str, str]:
    return {p.name: p.read_text() for p in APP.rglob("*.swift")}


def _body(text: str, signature: str) -> str:
    start = text.index(signature)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[open_at:i + 1]
    raise AssertionError(signature)


def test_the_probe_only_opens_and_never_reads_a_store():
    probe = _src("AccessProbe.swift")
    assert "O_RDONLY" in probe
    for forbidden in ("Darwin.read(", " read(fd", "sqlite3", "FileHandle", "contentsOfFile", "Data(contentsOf", "NSLog", "print("):
        assert forbidden not in probe, forbidden
    # Every disk answer reaches the pure rule through one FileSystem, with a path parameter.
    assert "static func probe(home: String" in probe and "static func decide(" in probe


def test_probe_runs_on_activation_and_a_two_second_timer_only_while_a_page_is_open():
    monitor = _src("AccessProbeMonitor.swift")
    assert "NSApplication.didBecomeActiveNotification" in monitor
    assert "pageInterval: UInt64 = 2_000_000_000" in monitor
    opened = _body(monitor, "func pageOpened()")
    assert "pagesOpen == 1" in opened and "Task.sleep(nanoseconds: Self.pageInterval)" in opened
    closed = _body(monitor, "func pageClosed()")
    assert "timer?.cancel()" in closed
    # Both permission surfaces (the notch page and the setup window) run FirstRunPermissions.start/stop.
    setup = _src("FirstRunSetupView.swift")
    assert "AccessProbeMonitor.shared.pageOpened()" in _body(setup, "func start()")
    assert "AccessProbeMonitor.shared.pageClosed()" in _body(setup, "func stop()")


def test_a_probe_that_flips_on_resyncs_readers_without_a_relaunch():
    app = _src("AppDelegate.swift")
    started = _body(app, "private func startAccessProbe()")
    assert ".wispAccessProbeChanged" in started and "resyncReaders(after: .fullDiskAccess)" in started
    assert "AccessProbeMonitor.shared.start()" in started
    assert "startAccessProbe()" in _body(app, "private func startNativeSourceReaders()")
    monitor = _src("AccessProbeMonitor.swift")
    apply = _body(monitor, "private func apply(")
    assert "AccessProbe.gained(" in apply and ".wispAccessProbeChanged" in apply


def test_reopen_prompt_is_a_button_the_person_presses_after_a_failed_probe():
    monitor = _src("AccessProbeMonitor.swift")
    assert "ReopenPrompt.shows(" in _body(monitor, "private func updatePrompt()")
    assert "ReopenPrompt.delay + 0.5" in monitor
    view = _src("NotchPermissionsView.swift")
    assert "probe.reopenPromptVisible" in view and "probe.reopenWisp()" in view
    # reopenWisp is only reached from that button.
    callers = [n for n, t in _sources().items() if "reopenWisp()" in t and n not in ("AccessProbeMonitor.swift",)]
    assert callers == ["NotchPermissionsView.swift"], callers


def test_only_two_places_start_the_relaunch_helper_for_a_privacy_switch():
    """Macs' own Quit & Reopen (relaunchIfSystemSettingsAsked, vetoed by the probe) and the
    person's Reopen button. Reset and Move to Applications are the person's own too."""
    users = sorted(n for n, t in _sources().items() if "WispReset.relaunchShell" in t)
    assert users == ["AccessProbeMonitor.swift", "AppDelegate.swift"], users
    app = _src("AppDelegate.swift")
    relaunch = _body(app, "private func relaunchIfSystemSettingsAsked(")
    assert "accessProbePasses: access.works" in relaunch and "AccessProbe.probe().state" in relaunch
    assert "restartStillNeeded:" in relaunch
    callers = [m.start() for m in re.finditer(r"relaunchIfSystemSettingsAsked\(", app)]
    assert len(callers) == 2   # the call in applicationShouldTerminate, and the definition
    assert "relaunchIfSystemSettingsAsked(PrivacyRelaunch.Quit.current())" in _body(app, "func applicationShouldTerminate(")
    assert "PrivacyRelaunch.Quit.current()" not in app.replace(
        "relaunchIfSystemSettingsAsked(PrivacyRelaunch.Quit.current())", "")


def test_provisional_is_the_first_notification_request():
    notes = _src("Notifications.swift")
    assert re.search(r"firstRequestOptions: UNAuthorizationOptions = \[[^\]]*\.provisional", notes)
    assert "\n    static let fullAlertOptions: UNAuthorizationOptions = [.alert, .sound]\n" in notes
    assert "requestAuthorization(options: firstRequestOptions)" in _body(notes, "static func requestFirst()")
    assert "requestAuthorization(options: fullAlertOptions)" in _body(notes, "static func askForAlerts()")
    assert "requestFirst()" in _body(notes, "static func allowedAskingFirstTime()")
    setup = _src("FirstRunSetupView.swift")
    assert "Notifications.requestFirst()" in _body(setup, "func request(_ item: Item) async")
    # No request for notifications anywhere else, and the full-alert ask is one button.
    for name, text in _sources().items():
        if name != "Notifications.swift":
            assert "UNUserNotificationCenter.current().requestAuthorization" not in text, name
    view = _src("NotchPermissionsView.swift")
    assert "Notifications.getAlertsChip" in view and "Notifications.askForAlerts()" in view


def test_every_privacy_link_for_accessibility_comes_from_privacy_names():
    for name, text in _sources().items():
        if name == "PrivacyNames.swift":
            continue
        assert "preference.security?Privacy_Accessibility" not in text, name
    assert "PrivacyNames.accessibilityPane" in _src("Permissions.swift")
    assert "PrivacyNames.accessibilityPane" in _src("NotchStudyView.swift")


def test_python_names_follow_the_same_versions():
    old = ("Accessibility access", "Accessibility")
    renamed = ("Device Control and Data Access (called Accessibility before macOS 27)", "Device Control and Data Access")
    assert _accessibility_names("26.4") == old
    assert _accessibility_names("27.0") == renamed
    assert _accessibility_names("27.2") == renamed
    assert _accessibility_names("27.2.1") == renamed
    assert _accessibility_names("27.3") == old
    assert _accessibility_names("28.0") == old
    assert _accessibility_names("") == old
