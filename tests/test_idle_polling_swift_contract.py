"""Source contracts for the app's idle-time polling (battery audit, 2026-10-28).

Pure text checks on the Swift sources and on the backend windows the Swift
heartbeats must beat. No compiler, no EventKit, no app. The behavior itself is
checked by tests/TodayPlanChecks.swift, tests/CalendarReaderChecks.swift and
tests/RemindersPolicyChecks.swift.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWIFT = ROOT / "app" / "Sources" / "WispApp"


def swift(name: str) -> str:
    return (SWIFT / name).read_text()


def region(source: str, start: str, end: str) -> str:
    """The text from `start` up to the next `end` after it."""
    begin = source.index(start)
    return source[begin:source.index(end, begin + len(start))]


def number(source: str, pattern: str) -> float:
    match = re.search(pattern, source)
    assert match, f"{pattern!r} not found; update this contract with the code it reads"
    return float(match.group(1))


# --- The day refresh (TodayView / AppDelegate) -------------------------------


def test_day_refresh_pace_is_30s_active_and_5min_away():
    source = swift("TodayView.swift")
    pace = region(source, "enum DayRefreshPace {", "\n}\n")
    assert "static let active: Duration = .seconds(30)" in pace
    assert "static let away: Duration = .seconds(300)" in pace
    assert "static let awayAfter: TimeInterval = 600" in pace
    # Open notch or recent input is active; anything else is away.
    assert "notchOpen || secondsSinceInput < awayAfter ? active : away" in pace
    # The idle read is a number only: no event tap, no monitor.
    assert "CGEventSource.secondsSinceLastEventType(.combinedSessionState" in pace
    assert "CGEventTap" not in source and "addGlobalMonitorForEvents" not in source


def test_background_loop_asks_a_cadence_and_can_be_woken():
    source = swift("TodayView.swift")
    start = region(source, "func startBackgroundRefresh(", "private func refreshBackgroundSnapshot")
    # No fixed interval left in the loop; it asks the cadence after every read.
    assert "interval:" not in start.split("{", 1)[0]
    assert "cadence: (@MainActor () -> Duration)? = nil" in start
    assert "refreshCadence()" in start
    # One loop, however often it is started; the injectable sleep and the 3 s retry stay.
    assert "guard backgroundRefresh == nil else { return }" in start
    assert "sleep: @escaping (Duration) async throws -> Void" in start
    assert "plan == nil ? .seconds(3)" in start
    # A wake cancels only the sleeping wait, never the loop or a read in flight.
    assert "func refreshSoon(ifOlderThan age: TimeInterval = 0)" in start
    assert "napping?.cancel()" in start
    assert "withTaskCancellationHandler" in start
    assert "DayRefreshPace.wakeFloor" in start


def test_app_wires_cadence_and_wake_triggers_once():
    source = swift("AppDelegate.swift")
    assert source.count("todayModel.startBackgroundRefresh(") == 1
    call = region(source, "todayModel.startBackgroundRefresh(", "self.observeDayRefreshTriggers()")
    assert "cadence:" in call
    assert "DayRefreshPace.interval(notchOpen: self?.model.collapsed == false" in call
    assert "DayRefreshPace.secondsSinceInput()" in call
    triggers = region(source, "private func observeDayRefreshTriggers()", "// Monochrome orb glyph")
    # Expanding the notch reads now (unless a read just began) ...
    assert "model.$collapsed" in triggers and "refreshSoon(ifOlderThan: DayRefreshPace.wakeFloor)" in triggers
    # ... and a "changed" event reads now too, but only while the notch is open: every
    # source sync posts one (service/main.py, /assistant/sync), so a closed notch must
    # not let them defeat the slow pace.
    assert ".wispAssistantChanged" in triggers
    assert "!self.model.collapsed" in triggers
    assert triggers.count("refreshSoon(") == 2


def test_no_second_thirty_second_day_poller():
    # The notch's Brief and Day re-render when the shared plan changes; the loop
    # is the app's alone (the service comment calling DayRefreshLoop a 30 s poller is stale).
    loop = region(swift("NotchDayView.swift"), "struct DayRefreshLoop: ViewModifier {", "\n}\n")
    for banned in ("Task.sleep", "Timer.", "startBackgroundRefresh", ".task", "repeatForever"):
        assert banned not in loop, banned
    for name in ("NotchBriefView.swift", "NotchDayView.swift"):
        text = swift(name)
        assert "Timer.scheduledTimer" not in text, name
        assert "startBackgroundRefresh" not in text, name
    # Only the app delegate, the window planner and the QA driver start the loop.
    starters = sorted(path.name for path in SWIFT.rglob("*.swift")
                      if ".startBackgroundRefresh(" in path.read_text())
    assert starters == ["AppDelegate.swift", "NotchQA.swift", "TodayView.swift"], starters


# --- Calendar and Reminders ----------------------------------------------------


def test_heartbeat_beats_the_backend_freshness_windows():
    paces = swift("RemindersWriter.swift")
    heartbeat = number(paces, r"static let heartbeat: TimeInterval = (\d+)")
    planner_window = number(
        (ROOT / "service" / "assistant" / "today.py").read_text(),
        r"0 <= now - stamp <= (\d+)",
    )
    receipt_window = number(
        (ROOT / "service" / "assistant" / "scheduler.py").read_text(),
        r"all\(0 <= now-t < (\d+) for t in metadata",
    )
    # A minute for the EventKit read (on main), the post, timer tolerance and App
    # Nap inside the planner's window: the timer is re-armed only after each read.
    assert heartbeat + 60 <= planner_window, (heartbeat, planner_window)
    assert heartbeat < receipt_window, (heartbeat, receipt_window)
    assert heartbeat > 60, "slower than the old one-minute timers"
    assert number(paces, r"static let withoutAccess: TimeInterval = (\d+)") == 60
    assert number(paces, r"static let changeFloor: TimeInterval = (\d+)") >= 60


def test_calendar_is_event_driven_with_a_heartbeat():
    source = swift("CalendarReader.swift")
    assert "forName: .EKEventStoreChanged, object: store" in source
    assert "scheduleChangeSync()" in source
    assert "NativeSyncPace.heartbeatInterval(authorized: isAuthorized)" in source
    assert "NativeSyncPace.changeDelay(" in source
    # No fixed 60 s repeating timer.
    assert "withTimeInterval: 60" not in source
    assert "repeats: true" not in source
    # The startup retry burst stays.
    assert "for delay in [1.0, 3.0, 6.0, 12.0, 25.0]" in source
    # Wisp's own writes (EventKit does not announce them to the same store) ask for a read.
    assert source.count("defer { scheduleChangeSync(afterOwnWrite: true) }") == 5
    # What is read and posted is unchanged: still the 395-day window.
    assert "private let historyDays = 365" in source and "private let horizonDays = 30" in source


def test_reminders_has_no_sixty_second_poll():
    source = swift("RemindersWriter.swift")
    assert "forName: .EKEventStoreChanged, object: store" in source
    assert "withTimeInterval: 60" not in source
    assert "repeats: true" not in source
    assert "NativeSyncPace.heartbeatInterval(authorized: isAuthorized)" in source
    # The heartbeat replaces the poll rather than removing it: the planner marks
    # Reminders not current once its sync is older than the planner window.
    assert "private func scheduleHeartbeat()" in source
    assert "defer { scheduleChangeSync() }" in source


def test_pace_rules_live_beside_the_shared_access_rules():
    # No new Swift file: every compile list that has CalendarReader.swift already
    # has RemindersWriter.swift (CalendarReader uses RemindersWriter.Access).
    assert "enum NativeSyncPace" in swift("RemindersWriter.swift")
    assert "enum NativeSyncPace" not in swift("CalendarReader.swift")


# --- The orb --------------------------------------------------------------------


def test_orb_pauses_when_it_should():
    source = swift("OverlayView.swift")
    orb = region(source, "private struct WispOrb: View {", "// Animated \"working\" indicator")
    assert "ProcessInfo.processInfo.isLowPowerModeEnabled" in orb
    assert ".NSProcessInfoPowerStateDidChange" in orb
    assert "NSWorkspace.screensDidSleepNotification" in orb
    assert "NSWorkspace.screensDidWakeNotification" in orb
    assert "accessibilityReduceMotion" in orb
    policy = region(source, "enum OrbMotion {", "\n}\n")
    assert "!(reduceMotion || lowPower || screensAsleep || resting)" in policy
    # The repeating animation lives only in the child that is removed to stop it.
    breathing = region(source, "private struct BreathingOrb: View {", "\n}\n")
    assert "repeatForever" in breathing
    assert "repeatForever" not in region(source, "private struct WispOrb: View {", "private struct OrbDisc")
    # The collapsed bar's orb rests; the open panel's header breathes.
    assert source.count("WispOrb(resting: true)") == 4
    assert source.count("WispOrb(resting: false)") == 1
    assert "private var orb:" not in source
