"""Wisp writes no text of its own into a person's Reminders.

The rules themselves run in the native gate (tests/ReminderActionLedgerChecks.swift, on
in-memory fixtures). This pins how RemindersWriter.swift uses them, because that file needs
EventKit: no test here creates or edits a reminder.
"""
from pathlib import Path

ROOT = Path(__file__).parents[1]
WRITER = (ROOT / "app/Sources/WispApp/RemindersWriter.swift").read_text()
LEDGER = (ROOT / "app/Sources/WispApp/ReminderActionLedger.swift").read_text()


def _section(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def test_no_writer_path_builds_marker_text():
    assert '"Wisp action: \\(' not in WRITER
    assert "Wisp kind" not in WRITER
    # The ledger reads the old lines; the only string it builds is a comparison.
    assert "item.notes" not in _section(WRITER, 'if kind == "create_reminder" {',
                                        "// An undated target")


def test_only_the_cleanup_writes_notes():
    assert WRITER.count("item.notes =") == 1
    assert "item.notes = notes" in _section(WRITER, "private func cleanMarkers", "/// Completed history")


def test_update_and_complete_leave_notes_alone():
    body = _section(WRITER, 'if kind == "update_reminder" {', "/// Read-only recovery")
    assert ".notes" not in body


def test_creation_goes_through_the_durable_flow():
    create = _section(WRITER, 'if kind == "create_reminder" {', "// An undated target")
    assert "ReminderCreationFlow.run(" in create
    assert create.count("store.save(") == 1
    # The ID goes to the ledger before the save, so a crash right after it can be proven.
    assert "plannedID: item.calendarItemIdentifier" in create
    assert create.index("plannedID:") < create.index("store.save(")
    # The save is inside the closure the flow runs after the intent is durable.
    flow = _section(LEDGER, "enum ReminderCreationFlow {", "/// Read-only recovery")
    assert flow.index("ledger.begin(") < flow.index("try save()") < flow.index("ledger.resolve(")


def test_reconciliation_is_read_only_and_never_retries():
    create = _section(WRITER, "func reconcileVerified(", 'guard !sourceID.isEmpty')
    assert "ReminderRecovery.find(" in create
    assert "store.save" not in create
    assert "ReminderCreationFlow" not in create
    assert 'failure("Reminder creation remains uncertain", uncertain: true)' in create


def test_sync_reads_kind_from_old_marker_then_ledger():
    kind = _section(WRITER, "private func commitmentKind(", "private func row(")
    assert kind.index("legacyKind") < kind.index("ledger.entry(") < kind.index('"reminder"')
    assert '"kind": self.commitmentKind(r)' in WRITER


def test_ledger_lives_with_wisps_state_and_holds_no_title():
    assert '"reminder-action-ledger.json"' in LEDGER
    assert 'environment["WISP_HOME"]' in LEDGER
    entry = _section(LEDGER, "struct Entry: Codable, Equatable {", "enum Begin")
    assert "title" not in entry.lower()


def test_reset_removes_the_ledger_and_its_set_aside_copy_before_any_reader_loads():
    reset = (ROOT / "app/Sources/WispApp/ResetWisp.swift").read_text()
    files = _section(reset, "static func appFiles(", "/// Why the card can't open now")
    for name in ('static let fileName = "reminder-action-ledger.json"',
                 'static let damagedFileName = "reminder-action-ledger.damaged.json"'):
        assert name in LEDGER
    assert '".moe/reminder-action-ledger.json"' in files
    assert '".moe/reminder-action-ledger.damaged.json"' in files
    # The app part runs at launch, before the Reminders reader exists.
    delegate = (ROOT / "app/Sources/WispApp/AppDelegate.swift").read_text()
    assert delegate.index("WispResetLaunch.run(") < delegate.index("remindersWriter = RemindersWriter()")
    # The service's pass keeps only what it names, so it erases both files too.
    from service.wisp_reset import KEEP_ENTRIES
    assert not {"reminder-action-ledger.json", "reminder-action-ledger.damaged.json"} & KEEP_ENTRIES


def test_the_damaged_ledger_log_line_holds_nothing_from_the_file():
    line = _section(LEDGER, "static let damagedLogLine = ", "\n")
    assert "\\(" not in line
    assert 'NSLog("%@", Self.damagedLogLine)' in LEDGER
    assert LEDGER.count("NSLog(") == 1
