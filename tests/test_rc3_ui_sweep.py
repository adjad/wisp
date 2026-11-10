"""Source contracts for the rc3 notch UI sweep.

These read the Swift sources only: no compiler, no app, no service. The logic
itself is checked natively by tests/NotchDayChecks.swift.
"""
from pathlib import Path
import re

APP = Path(__file__).resolve().parents[1] / "app" / "Sources" / "WispApp"
RAW_FONT = ".system(size:"


def _swift(name: str) -> str:
    return (APP / name).read_text()


def _between(text: str, start: str, end: str | None = None) -> str:
    assert start in text, start
    tail = text[text.index(start):]
    if end is None:
        return tail
    assert end in tail, end
    return tail[:tail.index(end)]


# --- Text size reaches the notch's cards (a09-ui-lens#2) ---------------------

# Drawn only in the notch: every point size goes through Font.wisp, which
# multiplies by the Text size setting.
NOTCH_ONLY = (
    "NudgeViews.swift",
    "CheckInView.swift",
    "NotchSetupView.swift",
    "NotchSetupDownload.swift",
    "NotchCorpusView.swift",
    "BriefReplyComposer.swift",
)


def test_notch_only_cards_name_no_raw_point_size():
    for name in NOTCH_ONLY:
        source = _swift(name)
        assert RAW_FONT not in source, name
        assert ".font(.wisp(size:" in source, name


def test_the_reset_card_follows_text_size():
    card = _between(_swift("ResetWisp.swift"), "struct ResetWispCard: View {", "\n}\n")
    assert RAW_FONT not in card
    assert card.count(".font(.wisp(size:") >= 7


def test_getting_to_know_you_scales_in_the_notch_and_not_in_the_window():
    source = _swift("KnowYouView.swift")
    # One raw size is left: the helper's window branch.
    assert source.count(RAW_FONT) == 1
    helper = _between(source, "private func textFont(", "\n    }\n")
    assert "inWindow ? Font.system(size: size, weight: weight) : Font.wisp(size: size, weight: weight)" in helper
    assert source.count(".font(textFont(") >= 25


def test_research_in_the_notch_follows_text_size():
    source = _swift("ResearchView.swift")
    # Everything from the notch's running view to the end of the file is drawn
    # in compact mode: the report, citations, sources, notices and its field.
    compact = _between(source, "private var inlineRunningView: some View {")
    assert RAW_FONT not in compact
    header = _between(source, "private var header: some View {", "private func performDelete()")
    assert header.count(RAW_FONT) == 1 and "Export Markdown" in header   # the unhosted full-size button
    assert ".font(.wisp(size: 15, weight: .semibold))" in header


def test_check_in_columns_widen_with_the_text():
    source = _swift("CheckInView.swift")
    # A clock at 1.3x does not fit a fixed 62 pt column.
    assert ".frame(width: 62 * Theme.textScale, alignment: .leading)" in source
    assert ".frame(width: 150 * Theme.textScale, alignment: .leading)" in source
    assert not re.search(r"\.frame\(width: (62|150), alignment", source)


# --- Icon-only buttons say what they do (a09-ui-lens#5) ----------------------

def test_icon_only_buttons_have_accessibility_labels():
    nudge = _between(_swift("NudgeViews.swift"), 'Image(systemName: "xmark")', "if !nudge.body.isEmpty")
    assert '.help("Dismiss")' in nudge and '.accessibilityLabel("Dismiss")' in nudge

    copy = _between(_swift("SearchPanel.swift"), "Button { model.copyAnswer() } label: {", "if let n = model.expandedCitation")
    assert 'Image(systemName: "doc.on.doc")' in copy
    assert '.help("Copy answer with source")' in copy and '.accessibilityLabel("Copy answer with source")' in copy

    refresh = _between(_swift("ResearchLibraryView.swift"), "Button(action: model.refresh) {", "HStack {")
    assert 'Image(systemName: "arrow.clockwise")' in refresh
    assert '.help("Refresh saved research")' in refresh and '.accessibilityLabel("Refresh saved research")' in refresh
    assert ".disabled(model.loading)" in refresh


# --- A date-only reminder on the Day tab (a05-day#2) -------------------------

def test_day_needs_carry_the_all_day_flag():
    view = _swift("NotchDayView.swift")
    needs = _between(view, "var dayNeeds: [DayNeedInput] {", "\n    }\n")
    assert "due: $0.due_ts, allDay: $0.all_day == true" in needs
    spoken = _between(view, "private func spokenTime(_ item: DayItem) -> String {", "\n    }\n")
    assert 'item.allDay ? (today.isShowingCurrentDay ? "due today" : "due that day") : "due " + today.clock(due)' in spoken


def test_day_timeline_never_formats_a_date_only_due_as_a_clock_time():
    logic = _swift("NotchDay.swift")
    need = _between(logic, "struct DayNeedInput: Equatable {", "\n}\n")
    assert "var allDay = false" in need
    item = _between(logic, "struct DayItem: Identifiable, Equatable {", "\n}\n")
    assert "var allDay = false" in item
    build = _between(logic, "for need in needs where need.source != \"calendar\"", "// A first day can bring a long overdue backlog.")
    assert 'need.allDay ? (isToday ? "today" : "that day") : clock(due)' in build
    assert "need.allDay ? overdueDay(due, dayStart: dayStart, dayClock: dayClock)" in build
    assert "due: due, allDay: need.allDay" in build
    assert '"Due " + clock(due)' not in build
    gutter = _between(logic, "static func gutter(_ item: DayItem", "\n    }\n")
    assert 'item.allDay ? "All day" : clock(due), "Due"' in gutter
    # The Swift contract that runs the logic covers it.
    checks = (APP.parents[2] / "tests" / "NotchDayChecks.swift").read_text()
    assert "allDay: true" in checks and '"All day", "Due"' in checks and "Overdue since earlier" in checks
