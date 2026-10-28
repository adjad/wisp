"""Source checks for Settings and Setup as sections of the Wisp window: there is
no Settings or Setup window left to open, every way into them lands in the Wisp
window, the setup guide's host is counted from the window rather than SwiftUI's
onAppear, and the embedded Settings never forces dark on the whole window.
Reads source only; no app, service or network."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app/Sources/WispApp"


def _read(name: str) -> str:
    return (APP / name).read_text()


def _body(text: str, signature: str) -> str:
    """The brace-balanced body that follows `signature`."""
    start = text.index(signature)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[open_at:i + 1]
    raise AssertionError(f"unbalanced body after {signature!r}")


def test_no_settings_or_setup_window_is_built():
    app = _read("AppDelegate.swift")
    # KnowYouOffer.Moment's `setupWindowUp:` label stays; it now carries settingsSetupShown.
    assert "settingsWindow" not in app and not re.search(r"\bsetupWindow\b", app)
    assert '"Wisp Settings"' not in app and '"Wisp Setup"' not in app
    # Wisp's own window types: the Wisp window and the first-run window, plus
    # TodayWindow, which only the mounted planner checks host. QA harnesses
    # (main.swift's QA branches included) are never packaged as Wisp.app.
    built = {name for name in (p.relative_to(APP).as_posix() for p in APP.rglob("*.swift"))
             if re.search(r"\bNSWindow\(contentRect:", (APP / name).read_text())
             and not re.search(r"QA|Checks", name) and name != "main.swift"}
    assert built <= {"AppDelegate.swift", "WispWindow.swift", "TodayView.swift"}, built


def test_every_way_into_settings_lands_in_the_wisp_window():
    app = _read("AppDelegate.swift")
    assert _body(app, "@objc private func openSettings()").strip("{} \n") == "wispWindow.showSettings()"
    route = _body(app, "private func open(_ route: WispRoute)")
    assert "case .settings:\n            openSettings()" in route
    # ⌘, in the app menu and the menu bar icon's Settings… both use openSettings.
    assert app.count('#selector(openSettings), keyEquivalent: ","') == 2
    window = _read("WispWindow.swift")
    assert "case chats, today, memory, research, settings" in window
    assert "navigation.section = .settings" in _body(window, "func showSettings()")
    views = _read("Chat/ChatViews.swift")
    assert "case .settings:" in _body(views, "struct ChatRootView: View")
    assert 'title: "Settings", selected: navigation.section == .settings' in views


def test_a_visible_window_is_not_reset_to_chats():
    window = _read("WispWindow.swift")
    show = _body(window, "func show() {")
    assert show.index("guard !isOpen else { present(); return }") < show.index("showChats()")
    assert "navigation.section = .chats" in _body(window, "func showChats() {")


def test_notch_hand_offs_to_chats_switch_an_open_window_to_chats():
    # The corpus ⤢ / ⌘O / /window selects the run in the window's store, and the chat
    # menu's "All chats" asks for the list: both must land on Chats even when the
    # window is open on Settings. The Dock, ⌘1 and the menu keep .chat(nil), which
    # brings an open window forward as it is.
    app = _read("AppDelegate.swift")
    corpus = _body(app, "model.onOpenCorpus = { [weak self] in")
    assert "self.wispWindow.showChats()" in corpus and "wispWindow.show()" not in corpus
    route = _body(app, "private func open(_ route: WispRoute)")
    assert "case .chatList:\n            wispWindow.showChats()" in route
    assert 'Button("All chats in the Wisp window") { WispRouter.shared.open(.chatList) }' in _read("OverlayView.swift")
    # The notch Ask ⤢ passes the session id (empty for a new chat), never nil, so it
    # goes through show(sessionId:), which always selects Chats.
    assert "var onOpenChat: ((_ sessionId: String) -> Void)?" in _read("OverlayModel.swift")
    assert "navigation.section = .chats" in _body(_read("WispWindow.swift"), "func show(sessionId: String) {")


def test_setup_guide_opens_as_settings_models_setup():
    app = _read("AppDelegate.swift")
    guide = _body(app, "@objc private func openSetupGuide()")
    assert 'defaults.set("models", forKey: SettingsView.paneKey)' in guide
    assert 'defaults.set("setup", forKey: SettingsView.modelsPaneKey)' in guide
    assert "defaults.set(true, forKey: Self.setupGuideShownKey)" in guide
    assert guide.rstrip("} \n").endswith("openSettings()")
    # The first-run window still steps aside for it and comes back when it is left.
    assert "welcome.orderOut(nil)" in guide and "firstRunHiddenForGuide = true" in guide
    host = _body(app, "private func updateSettingsSetupHost()")
    assert 'setupModel.show(in: "settings")' in host and 'setupModel.hide(from: "settings")' in host
    assert "self.openFirstRunSetup()" in host
    # Counted from the window and the stored panes, never from SwiftUI's appear events.
    assert "wispWindow.isOpen" in host and "onAppear" not in host
    assert "window.onClose = { [weak self] in self?.updateSettingsSetupHost() }" in app


def test_know_you_waits_for_settings_setup_too():
    moment = _body(_read("AppDelegate.swift"), "private var knowYouMoment: KnowYouOffer.Moment")
    assert "setupWindowUp: settingsSetupShown" in moment


def test_setup_pane_hosts_the_guide_without_its_footer():
    settings = _read("SettingsView.swift")
    pane = _body(settings, "struct SettingsSetupPane: View")
    assert "guide.header" in pane and "guide.pageContent" in pane
    assert "guide.footer" not in pane and 'Button("Check again")' in pane
    assert 'Button("Done")' not in pane and 'Button("Settings…")' not in pane
    # Theme.swift is all SetupGuideView may lean on: its colours come from the palette.
    guide = _read("SetupGuideView.swift")
    view = guide[guide.index("struct SetupGuideView: View"):]
    assert "Theme." not in view and "Color.white" not in view and "Color.black" not in view


def test_embedded_settings_follow_the_macs_appearance():
    settings = _read("SettingsView.swift")
    body = _body(settings, "    var body: some View {\n        if embedded {")
    embedded, standalone = body.split("} else {", 1)
    assert ".preferredColorScheme" not in embedded and "frame(width: 760" not in embedded
    assert "sidebar" not in embedded and ".environment(\\.themePalette, palette)" in embedded
    # The standalone form, which only the WISP_SETTINGS_QA render uses, is unchanged.
    assert ".preferredColorScheme(.dark)" in standalone and ".frame(width: 760, height: 620)" in standalone
    assert "private var palette: ThemePalette { embedded ? .wispWindow : .notch }" in settings
    assert ".tint(Theme.accent)" not in _body(settings, "private var paneContent: some View")
    google = _read("GoogleSettingsPane.swift")
    assert "Theme." not in google[google.index("struct GoogleSettingsPane: View"):]


def test_search_everything_left_the_window_sidebar():
    views = _read("Chat/ChatViews.swift")
    assert "Search everything" not in views and "searchEverything" not in views
    assert "searchEverything" not in _read("AppDelegate.swift")


def test_local_label_rereads_cloud_use_after_in_window_settings():
    # Settings is a section of the Wisp window, so no window becomes key when a
    # model changes there; the Local / Local + cloud label must re-read anyway.
    body = _body(_read("Chat/ChatViews.swift"), "var body: some View")
    assert re.search(r"publisher\(for: \.wispInferenceConfigurationChanged\)\)\s*\{[^}]*refreshCloudUse", body)
    assert re.search(r"onChange\(of: navigation\.section\)[^}]*\.chats[^}]*refreshCloudUse", body)
    for poster in ("SettingsView.swift", "EngineSettingsView.swift"):
        assert "post(name: .wispInferenceConfigurationChanged" in _read(poster), poster
