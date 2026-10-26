"""Source checks for the onboarding window's promises that a pure Swift check
can't see: Wisp never asks macOS for a permission on its own, every Wisp window
can be minimized, a Dock click brings forward one window, and Getting to know
you lives in the window rather than in a notch offer. Reads source only; no
app, service or network."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app/Sources/WispApp"

# Every API that makes macOS show a privacy prompt.
PROMPTING = re.compile(
    r"requestFullAccessToEvents|requestFullAccessToReminders|requestWriteOnlyAccessToEvents"
    r"|requestAccess\(to:|requestAccess\(for:|requestAuthorization\("
)


def _sources() -> dict[str, str]:
    return {p.relative_to(APP).as_posix(): p.read_text() for p in APP.rglob("*.swift")}


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


def test_prompts_only_follow_a_click():
    """A prompt may only come from the setup window's row button
    (FirstRunPermissions.request) or ContactsReader's guarded request, which
    the app starts with mayAsk false."""
    hits = {}
    for name, text in _sources().items():
        for match in PROMPTING.finditer(text):
            hits.setdefault(name, []).append(text.count("\n", 0, match.start()) + 1)
    assert set(hits) == {"FirstRunSetupView.swift", "ContactsReader.swift"}, hits
    setup = _sources()["FirstRunSetupView.swift"]
    request = _body(setup, "func request(_ item: Item) async")
    assert len(PROMPTING.findall(request)) == len(PROMPTING.findall(setup)), \
        "every prompt in the setup window is inside the row's request()"
    # Automation is asked (ask: true) only from that same click.
    assert setup.count("automationState(ask: true)") == 1 and "automationState(ask: true)" in request


def test_nothing_asks_at_launch():
    sources = _sources()
    joined = "\n".join(sources.values())
    for gone in ("Permissions.bootstrap", "requestFirstRun", "didRequestPermissions", "didRequestExtraPermissions"):
        assert gone not in joined, gone
    app = sources["AppDelegate.swift"]
    assert "ContactsReader(mayAsk: { false })" in app
    # Calendar and Reminders readers report instead of requesting.
    for reader in ("CalendarReader.swift", "RemindersWriter.swift"):
        text = sources[reader]
        assert "Access.passive(" in text and "requestAccessTracked" not in text, reader
    # Mail and Notes skip scripted reads until Automation has an answer, asking nothing.
    for reader in ("MailReader.swift", "NotesReader.swift"):
        text = sources[reader]
        helper = _body(text, "static func automationDecided(")
        assert "AEEventID(typeWildCard), false)" in helper, reader
        assert "Self.automationDecided(" in text, reader


def test_every_wisp_window_can_be_minimized():
    for name, text in _sources().items():
        if name in {"main.swift", "NotchQA.swift", "Chat/ChatQA.swift"}:
            continue  # QA harness windows, never shown to a person
        for match in re.finditer(r"styleMask:\s*(\[[^\]]*\]|Self\.onboardingStyle)", text):
            mask = match.group(1)
            if mask == "Self.onboardingStyle":
                mask = re.search(r"static let onboardingStyle: NSWindow.StyleMask = (\[[^\]]*\])", text).group(1)
            if ".titled" in mask:
                assert ".miniaturizable" in mask, f"{name}: {mask}"
    app = _sources()["AppDelegate.swift"]
    assert '"Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m"' in app


def test_dock_click_brings_forward_one_window():
    app = _sources()["AppDelegate.swift"]
    body = _body(app, "func applicationShouldHandleReopen(")
    assert "WispReopen.choose(" in body
    # Returning true would let AppKit bring back a minimized window as well.
    assert re.search(r"return false\s*}\s*$", body) and "return true" not in body


def test_know_you_is_a_window_step_not_a_notch_offer():
    setup = _sources()["FirstRunSetupView.swift"]
    assert "KnowYouView(model: knowYou, inWindow: true)" in setup
    quiet = _body(setup, "static func quiet(_ m: Moment)")
    assert "m.aboutYouDecidedInWindow" in quiet
    app = _sources()["AppDelegate.swift"]
    assert "aboutYouDecidedInWindow: FirstRunSetup.aboutYouDecidedInWindow" in app
    # The key is set only by the step's own Done or Skip for now, never by showing it.
    assert app.count("FirstRunSetup.aboutYouDecidedKey") == 1
    assert setup.count("aboutYouDecided()") == 2
    # Finishing setup no longer opens Getting to know you in the notch.
    finish = _body(app, "private func finishFirstRun(")
    assert "startKnowYou" not in finish and "tellWispAboutYou" not in finish


def test_reopened_window_leaves_the_model_alone():
    """Opened again from Permissions… or a Brief link, the window must not
    switch the model or run its test by itself."""
    setup = _sources()["FirstRunSetupView.swift"]
    drive = _body(setup, "private func drive(_ state: FirstRunBrain)")
    assert drive.lstrip("{ \n").startswith("guard FirstRunSetup.drivesModel(setupDone: setupDone) else { return }")
    app = _sources()["AppDelegate.swift"]
    assert "setupDone: defaults.bool(forKey: FirstRunSetup.doneKey)" in app
