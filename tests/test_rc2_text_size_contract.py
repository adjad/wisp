"""Source contracts: the hours step, the report draft card and Smart Search follow Text size.

These read the Swift sources only: no compiler, no app, no service.
"""
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app" / "Sources" / "WispApp"
RAW_FONT = ".system(size:"


def _swift(name: str) -> str:
    return (APP / name).read_text()


def test_report_draft_card_and_smart_search_have_no_fixed_fonts():
    for name in ("ReportDraftView.swift", "SearchPanel.swift"):
        text = _swift(name)
        assert RAW_FONT not in text, name
        assert ".wisp(size:" in text, name


def test_report_draft_preview_height_scales():
    assert "(larger ? 340 : 118) * Theme.textScale" in _swift("ReportDraftView.swift")


def test_hours_editor_scales_and_onboarding_window_opts_out():
    hours = _swift("UserHoursView.swift")
    assert hours.count(RAW_FONT) == 1  # only the onboarding window's opt-out path
    assert "Font.system(size: size, weight: weight)" in hours
    assert "Font.wisp(size: size, weight: weight)" in hours
    assert "var scalesText = true" in hours
    assert "104 * sizeScale" in hours
    assert "24 * sizeScale" in hours
    know_you = _swift("KnowYouView.swift")
    assert "UserHoursEditor(hours: $model.hours, scalesText: !inWindow)" in know_you
