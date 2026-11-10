"""Calendar's year of events is read off the main thread (CalendarReader.swift).

Pure text checks on the Swift source; tests/CalendarReaderChecks.swift checks
the ordering rules (CalendarReadGate) when it is compiled by the gates.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app" / "Sources" / "WispApp" / "CalendarReader.swift"


def _body(text: str, signature: str) -> str:
    start = text.index(signature)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[open_at:i + 1]
    raise AssertionError(f"unbalanced body after {signature!r}")


def test_the_event_read_is_not_on_the_main_thread():
    """sync() ran store.events(matching:) over 395 days and built every row on
    the main thread, every two minutes and on every Calendar change."""
    sync = _body(SOURCE.read_text(), "func sync()")
    assert "events(matching:" not in sync and "compactMap" not in sync
    assert "Task.detached(priority: .utility)" in sync
    assert "CalendarReader.readSnapshot(start: start, end: end" in sync
    # Permission state and the no-access report stay on the main thread.
    assert sync.index("guard Thread.isMainThread") < sync.index("guard isAuthorized")
    assert sync.index("post(events: [], diagnostics: diagnostics)") < sync.index("Task.detached")


def test_the_read_uses_its_own_store_and_never_posts_an_unreadable_calendar():
    read = _body(SOURCE.read_text(), "static func readSnapshot(")
    assert "let store = EKEventStore()" in read
    assert "store.events(matching: pred)" in read
    # Access is checked before and after; a store with no calendars loaded posts nothing.
    assert read.count("guard EKEventStore.authorizationStatus(for: .event) == .fullAccess else { return nil }") == 2
    assert "guard !cals.isEmpty || expectedCalendars == 0 else { return nil }" in read
    # The same payload keys and diagnostics as before.
    for key in ('"source_id"', '"kind"', '"title"', '"context"', '"when_ts"', '"all_day"', '"location"',
                '"organizer"', '"invitee_count"', '"organizer_is_user"', '"calendar_writable"', '"end_ts"',
                '"account"', '"authorized"', '"snapshot_started_at"', '"coverage_start"', '"coverage_end"',
                '"calendar_count"', '"calendars"', '"events_found"', '"events_declined_or_cancelled"',
                '"event_ids"'):
        assert key in read, key


def test_reads_are_one_at_a_time_and_posted_in_order():
    source = SOURCE.read_text()
    sync = _body(source, "func sync()")
    assert "guard readGate.begin() else { return }" in sync
    assert "let again = self.readGate.finish()" in sync
    assert "if let snapshot, self.readGate.accept(number) {" in sync
    # The no-access report takes its turn too, so an older read cannot follow it.
    refused = _body(sync, "guard isAuthorized else")
    assert "_ = readGate.accept(number)" in refused
    gate = _body(source, "struct CalendarReadGate")
    assert "guard number > posted else { return false }" in gate
    # The synced window is unchanged.
    assert "private let historyDays = 365" in source and "private let horizonDays = 30" in source
    checks = (ROOT / "tests" / "CalendarReaderChecks.swift").read_text()
    assert "readGateChecks()" in checks and "the older read is dropped" in checks
