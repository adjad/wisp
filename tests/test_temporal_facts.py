"""Synthetic captured-source examples; no providers, user state, or effects."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import string

import pytest

from service.temporal_facts import (
    EvidenceContext, MAX_FACTS, MAX_TEXT_LENGTH, extract_temporal_facts,
)

CAPTURE = datetime(2026, 9, 25, 19, 17, tzinfo=timezone.utc)
EVIDENCE = EvidenceContext("synthetic", "fixture-001", "fixture://temporal/001")
CASES = json.loads((Path(__file__).resolve().parents[1] /
                    "test_fixtures/temporal_facts/cases.json").read_text())


def extract(text, **kwargs):
    return extract_temporal_facts(text, **{
        "captured_at": CAPTURE, "timezone": "America/Los_Angeles", "evidence": EVIDENCE,
        **kwargs,
    })


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["text"])
def test_synthetic_fixtures(case):
    result = extract(case["text"])
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact.kind == case["kind"]
    assert fact.status == case["status"]
    assert [fact.start.year, fact.start.month, fact.start.day] == case["date"]
    if "instant" in case:
        assert fact.start.instants == (case["instant"],)
    if "end" in case:
        assert fact.end.instants == (case["end"],)
    if "uncertainty" in case:
        assert case["uncertainty"] in fact.uncertainties
    assert fact.span.quote == case["text"][fact.span.start:fact.span.end]
    assert fact.context_span.quote == case["text"][fact.context_span.start:fact.context_span.end]
    assert fact.provenance.evidence == EVIDENCE
    assert fact.provenance.captured_at == CAPTURE
    assert result == extract(case["text"])


def test_fold_preserves_both_candidates_and_gap_has_none():
    fold = extract("Meeting 2026-11-01 at 01:30.").facts[0]
    assert fold.start.instants == ("2026-11-01T08:30:00+00:00", "2026-11-01T09:30:00+00:00")
    assert extract("Meeting 2026-03-08 at 02:30.").facts[0].start.instants == ()
    assert extract("Meeting 2026-03-08 at 03:30.").facts[0].start.instants == ("2026-03-08T10:30:00+00:00",)


def test_dst_range_validates_both_endpoints():
    fact = extract("Available 2026-03-08 from 01:30 to 02:30.").facts[0]
    assert fact.start.instants == ("2026-03-08T09:30:00+00:00",)
    assert fact.end.instants == ()
    assert "nonexistent_local_time" in fact.uncertainties


@pytest.mark.parametrize("zone, instant", [
    ("UTC", "2026-10-03T15:00:00+00:00"),
    ("+02:30", "2026-10-03T12:30:00+00:00"),
    ("UTC-0400", "2026-10-03T19:00:00+00:00"),
    ("Europe/London", "2026-10-03T14:00:00+00:00"),
])
def test_explicit_source_zone_overrides_fallback(zone, instant):
    fact = extract(f"Meeting 2026-10-03 at 3pm {zone}.").facts[0]
    assert fact.status == "resolved"
    assert fact.start.timezone == zone
    assert fact.start.instants == (instant,)
    assert fact.span.quote.endswith(zone)
    assert fact.provenance.timezone == "America/Los_Angeles"


@pytest.mark.parametrize("zone", ["Mars/Olympus", "UTC+24:00", "+04:99", "PST"])
def test_invalid_or_ambiguous_explicit_zone_never_falls_back(zone):
    fact = extract(f"Meeting 2026-10-03 at 3pm {zone}.").facts[0]
    assert fact.status == "partial"
    assert fact.start.instants == ()
    assert set(fact.uncertainties) & {"invalid_timezone", "ambiguous_timezone"}


def test_capture_zone_does_not_supply_missing_source_zone():
    fact = extract("Meeting 2026-10-03 at 3pm.", timezone=None).facts[0]
    assert "missing_timezone" in fact.uncertainties
    assert fact.start.instants == ()
    relative = extract("Report due tomorrow.", timezone=None).facts[0]
    assert relative.start.year is None
    assert "missing_timezone" in relative.uncertainties
    assert extract("Report due 2026-10-03.", timezone=None).facts[0].status == "resolved"


def test_relative_anchor_uses_local_date_at_midnight_and_year_boundary():
    capture = datetime(2027, 1, 1, 1, tzinfo=timezone.utc)  # Dec 31 in LA
    tomorrow = extract("Report due tomorrow.", captured_at=capture).facts[0]
    assert (tomorrow.start.year, tomorrow.start.month, tomorrow.start.day) == (2027, 1, 1)
    today = extract("Report due today.", captured_at=capture).facts[0]
    assert (today.start.year, today.start.month, today.start.day) == (2026, 12, 31)


def test_elapsed_hours_and_local_days_diverge_at_dst():
    capture = datetime(2026, 3, 7, 20, tzinfo=timezone.utc)
    elapsed = extract("Meeting in 24 hours.", captured_at=capture).facts[0]
    wall = extract("Meeting tomorrow at noon.", captured_at=capture).facts[0]
    assert elapsed.start.instants == ("2026-03-08T20:00:00+00:00",)
    assert wall.start.instants == ("2026-03-08T19:00:00+00:00",)


@pytest.mark.parametrize("raw", [None, "yesterday", 0, True, datetime(2026, 1, 1)])
def test_invalid_capture_retains_absolute_date_but_does_not_anchor_relative(raw):
    absolute = extract("Report due 2026-10-03.", captured_at=raw)
    assert "invalid_captured_at" in absolute.issues
    assert absolute.facts[0].start.year == 2026
    relative = extract("Report due tomorrow.", captured_at=raw)
    assert relative.facts[0].start.year is None
    assert "missing_capture_anchor" in relative.facts[0].uncertainties


@pytest.mark.parametrize("raw", [None, {}, EvidenceContext("", "x"), EvidenceContext("fixture", 3)])
def test_invalid_evidence_is_visible(raw):
    result = extract("Report due 2026-10-03.", evidence=raw)
    assert "invalid_evidence" in result.issues
    assert result.facts[0].status == "partial"
    assert result.facts[0].provenance.evidence is None


@pytest.mark.parametrize("raw", ["invalid", 13, [], ""])
def test_invalid_timezone_context_is_visible(raw):
    result = extract("Meeting tomorrow at noon.", timezone=raw)
    assert "invalid_timezone_context" in result.issues
    assert result.facts[0].start.instants == ()


def test_date_only_has_no_invented_time_or_timezone():
    fact = extract("Report due 2026-10-03.").facts[0]
    assert fact.start.hour is None
    assert fact.start.timezone is None
    assert fact.start.precision == "date"
    assert fact.start.instants == ()


def test_yearless_leap_day_is_partial_not_invalid():
    fact = extract("Report due February 29.").facts[0]
    assert fact.uncertainties == ("missing_year",)
    assert fact.start.year is None
    assert "invalid_date" in extract("Report due February 29, 2026.").facts[0].uncertainties


def test_each_mention_has_local_kind_and_exact_unicode_offsets():
    text = "📅 Café meeting 2026-10-03 at 3pm, report due 2026-10-04;\nfree 2026-10-05 from 09:00 to 11:00."
    result = extract(text)
    assert [fact.kind for fact in result.facts] == ["event", "due", "availability"]
    for fact in result.facts:
        assert text[fact.span.start:fact.span.end] == fact.span.quote
        assert text[fact.context_span.start:fact.context_span.end] == fact.context_span.quote
        assert fact.context_span.start <= fact.span.start < fact.span.end <= fact.context_span.end


def test_repeated_mentions_preserve_positions_and_provenance_is_immutable():
    result = extract("Report due 2026-10-03. Report due 2026-10-03.")
    assert len(result.facts) == 2
    assert result.facts[0].span.start != result.facts[1].span.start
    with pytest.raises(FrozenInstanceError):
        result.facts[0].provenance.evidence.source_id = "changed"


def test_conflicting_mentions_preserved_without_selecting_winner():
    result = extract("Deadline changed from 2026-10-03 to 2026-10-05, or 2026-10-07.")
    assert len(result.facts) == 2
    assert result.facts[0].end.day == 5
    assert all("conflicting_mentions" in fact.uncertainties for fact in result.facts)
    assert all(fact.status != "resolved" for fact in result.facts)


def test_unrelated_sentences_are_not_conflicts():
    result = extract("Meeting 2026-10-03. Meeting 2026-10-04.")
    assert len(result.facts) == 2
    assert all(fact.status == "resolved" for fact in result.facts)


@pytest.mark.parametrize("word", ["morning", "tonight", "soon", "in two days"])
def test_recognized_unsupported_expression_is_unknown(word):
    fact = extract(f"Meeting {word}.").facts[0]
    assert fact.status == "unknown"
    assert fact.start.instants == ()
    assert "unsupported_expression" in fact.uncertainties


def test_unknown_kind_is_explicit():
    fact = extract("The date 2026-10-03 appears here.").facts[0]
    assert fact.kind == "unknown"
    assert "unknown_kind" in fact.uncertainties


@pytest.mark.parametrize("text, relation", [
    ("Report due before 2026-10-03.", "before"),
    ("Available after 3pm.", "after"),
    ("Report due by 2026-10-03.", "by"),
])
def test_boundaries_are_preserved(text, relation):
    assert extract(text).facts[0].relation == relation


def test_date_range_preserves_date_precision():
    fact = extract("Available 2026-10-03 through 2026-10-05.").facts[0]
    assert fact.relation == "range"
    assert fact.start.day == 3 and fact.end.day == 5
    assert fact.start.hour is None and fact.end.hour is None


@pytest.mark.parametrize("raw", [None, 23, [], {}, b"tomorrow"])
def test_invalid_text(raw):
    assert extract(raw).issues == ("invalid_text",)


def test_empty_and_non_temporal_text_are_unknown():
    for text in ("", "Please review the document."):
        assert extract(text).facts == ()
        assert extract(text).status == "unknown"


def test_input_and_fact_limits_are_explicit():
    assert extract("x" * (MAX_TEXT_LENGTH + 1)).issues == ("text_limit_exceeded",)
    result = extract("Report due 2026-10-03. " * (MAX_FACTS + 1))
    assert len(result.facts) == MAX_FACTS
    assert "fact_limit_exceeded" in result.issues


def test_overflow_relative_and_calendar_extremes_do_not_throw():
    for text in ("Meeting in 999999999999999999 days.", "Meeting in 999999999999999 hours.",
                 "Meeting 0000-01-01.", "Meeting 9999-12-31 at 23:00 UTC-0400."):
        result = extract(text)
        assert result.status in {"partial", "unknown"}
        assert result.facts[0].start.instants == ()


def test_deterministic_synthetic_noise_does_not_throw_or_corrupt_spans():
    rng = random.Random(8)
    alphabet = string.ascii_letters + string.digits + " /:-;\n📅"
    for _ in range(200):
        text = "".join(rng.choices(alphabet, k=200))
        result = extract(text)
        assert result == extract(text)
        assert all(text[f.span.start:f.span.end] == f.span.quote for f in result.facts)


def test_correction_with_only_two_dates_is_not_a_resolved_range():
    fact = extract("Deadline changed from 2026-10-03 to 2026-10-05.").facts[0]
    assert fact.status == "partial"
    assert "conflicting_mentions" in fact.uncertainties


@pytest.mark.parametrize("suffix", ["UTC+2", "UTCfoo", "utc+2", "utcfoo", "CST", "cst", "UTC+02:99", "UTC+02:000"])
def test_unsupported_zone_suffix_is_not_silently_truncated(suffix):
    result = extract(f"Meeting 2026-10-03 at 3pm {suffix}.")
    assert result.status != "resolved"
    assert all(not f.start.instants for f in result.facts)


@pytest.mark.parametrize("clock", ["15:00:30Z", "03:00:30", "9:999", "3pmfoo"])
def test_unsupported_clock_tail_does_not_become_resolved_date(clock):
    result = extract(f"Meeting 2026-10-03 at {clock}.")
    assert result.status != "resolved"


def test_zone_at_each_range_endpoint_is_not_lost():
    result = extract("Available 2026-10-03 from 09:00 UTC to 11:00 UTC.")
    assert result.status == "resolved"
    assert len(result.facts) == 1
    assert result.facts[0].start.instants == ("2026-10-03T09:00:00+00:00",)
    assert result.facts[0].end.instants == ("2026-10-03T11:00:00+00:00",)


def test_time_only_window_preserves_range_and_explicit_zone():
    result = extract("Available 9am–11am UTC.")
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact.end is not None
    assert fact.start.hour == 9 and fact.end.hour == 11
    assert fact.start.timezone == fact.end.timezone == "UTC"
    assert "missing_date" in fact.uncertainties


def test_yearless_ambiguous_slash_date_labels_both_omissions():
    fact = extract("Report due 03/04.").facts[0]
    assert {"missing_year", "ambiguous_date_order"} <= set(fact.uncertainties)


def test_unsupported_common_daypart_attached_to_date_is_visible():
    result = extract("Meeting tomorrow morning.")
    assert result.status == "partial"
    assert all(f.start.hour is None for f in result.facts)


def test_until_retains_exclusive_boundary():
    fact = extract("Available until 3pm.").facts[0]
    assert fact.relation == "until"


def test_kind_cues_require_word_boundaries():
    fact = extract("Submitter signed on 2026-10-03.").facts[0]
    assert fact.kind == "unknown"


def test_relative_elapsed_duration_retains_capture_seconds():
    capture = CAPTURE.replace(second=45, microsecond=123)
    fact = extract("Meeting in 3 minutes.", captured_at=capture).facts[0]
    assert fact.start.instants == ("2026-09-25T19:20:45.000123+00:00",)
    assert fact.start.precision == "instant"


def test_split_datetime_range_is_explicitly_partial():
    result = extract("Meeting 2026-10-03 at 3pm to 2026-10-04 at 4pm.")
    assert result.status == "partial"
    assert all("unsupported_range" in f.uncertainties for f in result.facts)


def test_range_end_boundary_retains_until_vs_through():
    until = extract("Available 2026-10-03 until 2026-10-05.").facts[0]
    through = extract("Available 2026-10-03 through 2026-10-05.").facts[0]
    assert until.end_boundary == "exclusive"
    assert through.end_boundary == "inclusive"


def test_large_numeric_relative_input_stays_unknown_without_exception():
    result = extract("Meeting in " + "9" * 5000 + " days.")
    assert result.facts[0].uncertainties == ("out_of_range",)


@pytest.mark.parametrize("prefix", ["Freeway closure", "Duet", "Submitter"])
def test_kind_prefixes_do_not_match(prefix):
    assert extract(f"{prefix} 2026-10-03.").facts[0].kind == "unknown"


def test_explicit_zone_anchors_relative_calendar_date():
    capture = datetime(2026, 9, 26, 1, tzinfo=timezone.utc)
    fact = extract("Meeting tomorrow at 3pm UTC.", captured_at=capture).facts[0]
    assert fact.start.instants == ("2026-09-27T15:00:00+00:00",)


@pytest.mark.parametrize("text, expected", [
    ("Report due yesterday.", (2026, 9, 24)),
    ("Report due in 2 weeks.", (2026, 10, 9)),
    ("Report due in 0 days.", (2026, 9, 25)),
    ("Report due 3 Oct 2026.", (2026, 10, 3)),
    ("Report due October 3rd, 2026.", (2026, 10, 3)),
    ("Report due 04/04/2026.", (2026, 4, 4)),
])
def test_supported_calendar_forms(text, expected):
    fact = extract(text).facts[0]
    assert fact.status == "resolved"
    assert (fact.start.year, fact.start.month, fact.start.day) == expected


def test_calendar_dates_cannot_be_coerced_by_bad_capture_or_zone():
    fact = extract("Report due October 3.", captured_at=None, timezone="bad").facts[0]
    assert fact.start.year is None
    assert fact.start.hour is None
    assert {"missing_year", "invalid_captured_at", "invalid_timezone_context"} <= set(fact.uncertainties)


def test_no_application_dependencies():
    import ast
    from service import temporal_facts
    tree = ast.parse(Path(temporal_facts.__file__).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add(node.module.split(".")[0])
    assert roots <= {"__future__", "dataclasses", "datetime", "re", "zoneinfo"}


def test_explicit_zone_without_clock_controls_relative_date():
    capture = datetime(2026, 9, 26, 1, tzinfo=timezone.utc)
    fact = extract("Report due tomorrow UTC.", captured_at=capture).facts[0]
    assert (fact.start.year, fact.start.month, fact.start.day) == (2026, 9, 27)
    assert fact.start.timezone == "UTC"
    assert fact.span.quote == "tomorrow UTC"


def test_conflicting_adjacent_zones_cannot_produce_resolved_instant():
    result = extract("Meeting 2026-10-03 at 3pm UTC PST.")
    assert result.status != "resolved"
    assert "conflicting_timezones" in result.facts[0].uncertainties
    assert result.facts[0].start.instants == ()


def test_approximate_time_is_not_reported_as_certain():
    fact = extract("Meeting around 2026-10-03 at 3pm.").facts[0]
    assert fact.status == "partial"
    assert "approximate_expression" in fact.uncertainties


@pytest.mark.parametrize("zone, instant", [
    ("Etc/GMT+2", "2026-10-03T17:00:00+00:00"),
    ("America/Port-au-Prince", "2026-10-03T19:00:00+00:00"),
    ("America/Argentina/Buenos_Aires", "2026-10-03T18:00:00+00:00"),
])
def test_complete_iana_zone_tokens_with_punctuation(zone, instant):
    fact = extract(f"Meeting 2026-10-03 at 3pm {zone}.").facts[0]
    assert fact.status == "resolved"
    assert fact.start.timezone == zone
    assert fact.start.instants == (instant,)


@pytest.mark.parametrize("zone", ["America//Los_Angeles", "Mars/Phobos+2"])
def test_malformed_slash_zone_does_not_fall_back(zone):
    fact = extract(f"Meeting 2026-10-03 at 3pm {zone}.").facts[0]
    assert "invalid_timezone" in fact.uncertainties
    assert fact.start.instants == ()


def test_explicit_date_range_zone_is_retained_on_both_endpoints():
    fact = extract("Available 2026-10-03 through 2026-10-05 UTC.").facts[0]
    assert fact.start.timezone == fact.end.timezone == "UTC"
    assert fact.start.instants == fact.end.instants == ()


def test_range_with_different_zones_compares_instants_not_wall_clocks():
    fact = extract("Available 2026-10-03 from 11:00 +02:00 to 10:00 UTC.").facts[0]
    assert fact.status == "resolved"
    assert fact.start.instants == ("2026-10-03T09:00:00+00:00",)
    assert fact.end.instants == ("2026-10-03T10:00:00+00:00",)


def test_uppercase_range_connectors_are_not_timezone_abbreviations():
    fact = extract("AVAILABLE 2026-10-03 FROM 09:00 TO 11:00 UTC.").facts[0]
    assert fact.status == "resolved"
    assert fact.start.hour == 9 and fact.end.hour == 11


def test_time_range_does_not_infer_meridiem_for_bare_first_hour():
    fact = extract("Available 2026-10-03 from 9 to 11am.").facts[0]
    assert "ambiguous_meridiem" in fact.uncertainties
    assert fact.start.instants == ()


def test_date_range_does_not_infer_year_from_other_endpoint():
    fact = extract("Available October 3 through October 5, 2026.").facts[0]
    assert fact.status == "partial"
    assert fact.start.year is None
    assert fact.end.year == 2026


@pytest.mark.parametrize("expression", ["09:00-11:00", "09:00 -11:00", "09:00-10:00"])
def test_negative_offset_cannot_silently_replace_hyphen_range(expression):
    fact = extract(f"Available 2026-10-03 from {expression}.").facts[0]
    assert fact.status == "partial"
    assert "ambiguous_offset_or_range" in fact.uncertainties
    assert fact.start.instants == ()


def test_iso_datetime_unambiguously_retains_negative_offset():
    fact = extract("Meeting 2026-10-03T09:00-07:00.").facts[0]
    assert fact.status == "resolved"
    assert fact.start.instants == ("2026-10-03T16:00:00+00:00",)


@pytest.mark.parametrize("cue", ["moved", "rescheduled", "postponed", "corrected", "revised"])
def test_audit_correction_verbs_do_not_assert_valid_range(cue):
    text = f"Deadline {cue} from 2026-10-03 to 2026-10-05."
    result = extract(text)
    fact = result.facts[0]
    assert result.status == fact.status == "partial"
    assert fact.relation == "alternatives"
    assert fact.end_boundary is None
    assert "conflicting_mentions" in fact.uncertainties
    assert fact.start.day == 3 and fact.end.day == 5
    assert fact.span.quote == "2026-10-03 to 2026-10-05"
    assert text[fact.span.start:fact.span.end] == fact.span.quote
    assert fact.context_span.quote == text
    assert fact.provenance.evidence == EVIDENCE


@pytest.mark.parametrize("zone", ["bst", "cet", "cest", "ist", "BST", "CET", "CEST", "IST", "Bst", "CesT"])
def test_audit_ambiguous_zone_abbreviations_are_preserved_in_all_cases(zone):
    text = f"Meeting 2026-10-03 at 3pm {zone}."
    fact = extract(text, timezone="UTC").facts[0]
    assert fact.status == "partial"
    assert "ambiguous_timezone" in fact.uncertainties
    assert fact.start.instants == ()
    assert fact.start.timezone == zone
    assert fact.span.quote.endswith(zone)
    assert text[fact.span.start:fact.span.end] == fact.span.quote
    assert fact.provenance.timezone == "UTC"


@pytest.mark.parametrize("suffix", ["-ish", " or so", " approx.", "-ISH", " OR SO", " APPROX."])
def test_audit_approximation_suffix_is_not_an_exact_instant(suffix):
    text = f"Meeting 2026-10-03 at 3pm{suffix}"
    fact = extract(text, timezone="UTC").facts[0]
    assert fact.status == "partial"
    assert "approximate_expression" in fact.uncertainties
    assert fact.start.instants == ()
    assert fact.span.quote == f"2026-10-03 at 3pm{suffix}"
    assert text[fact.span.start:fact.span.end] == fact.span.quote
    assert fact.context_span.quote == text
    assert fact.provenance.evidence == EVIDENCE


def test_approximate_range_or_so_is_not_a_conflicting_alternative():
    fact = extract("Available 2026-10-03 from 09:00 to 11:00 or so.").facts[0]
    assert fact.status == "partial"
    assert fact.relation == "range"
    assert "approximate_expression" in fact.uncertainties
    assert "conflicting_mentions" not in fact.uncertainties
    assert fact.start.instants == fact.end.instants == ()


@pytest.mark.parametrize("zone", ["jst", "aest", "hkt", "eest", "akst", "wib", "zz", "qxyz", "abcde"])
@pytest.mark.parametrize("case", [str.lower, str.upper, str.title], ids=["lower", "upper", "title"])
def test_audit_generic_zone_shape_never_silently_uses_fallback(zone, case):
    token = case(zone)
    text = f"Meeting 2026-10-03 at 3pm {token}."
    fact = extract(text, timezone="UTC").facts[0]
    assert fact.status == "partial"
    assert "ambiguous_timezone" in fact.uncertainties
    assert fact.start.instants == ()
    assert fact.start.timezone == token
    assert fact.span.quote == f"2026-10-03 at 3pm {token}"
    assert text[fact.span.start:fact.span.end] == fact.span.quote
    assert fact.context_span.quote == text
    assert fact.provenance.timezone == "UTC"


@pytest.mark.parametrize("wording", [
    "shifted", "pushed", "updated", "amended", "delayed", "advanced",
    "was unexpectedly reconfigured", "was adjusted again", "", "frobnicated",
])
def test_audit_due_from_to_is_uncertain_without_a_correction_verb_dictionary(wording):
    text = f"Deadline {wording} from 2026-10-03 to 2026-10-05."
    fact = extract(text).facts[0]
    assert fact.status == "partial"
    assert fact.relation == "alternatives"
    assert fact.end_boundary is None
    assert "ambiguous_due_range" in fact.uncertainties
    assert fact.start.day == 3 and fact.end.day == 5
    assert fact.span.quote == "2026-10-03 to 2026-10-05"
    assert text[fact.span.start:fact.span.end] == fact.span.quote
    assert fact.context_span.quote == text
    assert fact.provenance.evidence == EVIDENCE


def test_genuine_availability_from_to_remains_a_resolved_range():
    fact = extract("Available from 2026-10-03 to 2026-10-05.").facts[0]
    assert fact.status == "resolved"
    assert fact.relation == "range"
    assert "ambiguous_due_range" not in fact.uncertainties


@pytest.mark.parametrize("subject", ["Meeting", "Deadline", "Available"])
@pytest.mark.parametrize("zone, instant", [
    ("", "2026-10-03T15:00:00+00:00"),
    (" UTC", "2026-10-03T15:00:00+00:00"),
    (" Europe/London", "2026-10-03T14:00:00+00:00"),
])
@pytest.mark.parametrize("suffix", [
    "with Alice", "for lunch", "via video", "today", "sharp",
    "near the auditorium", "inside Room 12", "after the briefing", "on the dot",
])
def test_audit_sentence_suffix_does_not_become_a_zone(subject, zone, instant, suffix):
    text = f"{subject} 2026-10-03 at 3pm{zone} {suffix}."
    fact = extract(text, timezone="UTC").facts[0]
    assert fact.status == "resolved"
    assert fact.start.instants == (instant,)
    assert fact.span.quote == f"2026-10-03 at 3pm{zone}"
    assert text[fact.span.start:fact.span.end] == fact.span.quote
    assert fact.context_span.quote == text


@pytest.mark.parametrize("zone", ["jst", "AEST", "hKt", "qxyz"])
@pytest.mark.parametrize("suffix", ["with Dr Jones", "for review", "via Teams", "near the river", "sharp"])
def test_audit_explicit_unknown_zone_survives_following_prose(zone, suffix):
    text = f"Meeting 2026-10-03 at 3pm {zone} {suffix}."
    fact = extract(text, timezone="UTC").facts[0]
    assert fact.status == "partial"
    assert "ambiguous_timezone" in fact.uncertainties
    assert "conflicting_timezones" not in fact.uncertainties
    assert fact.start.instants == ()
    assert fact.start.timezone == zone
    assert fact.span.quote == f"2026-10-03 at 3pm {zone}"


@pytest.mark.parametrize("subject", ["Meeting", "Appointment", "Event", "Available"])
@pytest.mark.parametrize("wording", ["shifted", "pushed back", "delayed", "was unexpectedly reworked", "frobnicated"])
@pytest.mark.parametrize("window", ["2026-10-03 to 2026-10-05", "09:00 to 11:00"])
def test_audit_transition_context_is_uncertain_across_kinds(subject, wording, window):
    text = f"{subject} {wording} from {window}."
    fact = extract(text).facts[0]
    assert fact.status == "partial"
    assert fact.relation == "alternatives"
    assert fact.end_boundary is None
    assert "ambiguous_range_context" in fact.uncertainties
    assert fact.start.instants == fact.end.instants == ()
    assert text[fact.span.start:fact.span.end] == fact.span.quote
    assert fact.context_span.quote == text


@pytest.mark.parametrize("prefix", ["Meeting", "Event is", "Available", "Availability is"])
@pytest.mark.parametrize("window", [
    "from 2026-10-03 to 2026-10-05",
    "2026-10-03 from 09:00 to 11:00",
    "on 2026-10-03 from 09:00 to 11:00",
])
def test_audit_direct_window_grammar_remains_resolved(prefix, window):
    fact = extract(f"{prefix} {window}.").facts[0]
    assert fact.status == "resolved"
    assert fact.relation == "range"
    assert fact.end is not None


@pytest.mark.parametrize("suffix", [
    "with Professor Lin", "for a planning session", "via a voice call",
    "at the studio", "near Building B", "around the corner", "under the awning",
    "today", "sharp", "on the dot",
])
@pytest.mark.parametrize("case", [str.lower, str.upper, str.title], ids=["lower", "upper", "title"])
def test_prose_suffix_grammar_is_case_insensitive_after_explicit_zone(suffix, case):
    text = f"Meeting 2026-10-03 at 3pm Europe/London {case(suffix)}."
    fact = extract(text, timezone="UTC").facts[0]
    assert fact.status == "resolved"
    assert fact.start.instants == ("2026-10-03T14:00:00+00:00",)
    assert fact.span.quote == "2026-10-03 at 3pm Europe/London"
    assert fact.context_span.quote == text


@pytest.mark.parametrize("subject", ["Meeting", "Availability"])
@pytest.mark.parametrize("predicate", ["runs", "lasts", "spans"])
def test_supported_duration_predicates_describe_windows(subject, predicate):
    fact = extract(f"{subject} {predicate} from 2026-10-03 to 2026-10-05.").facts[0]
    assert fact.status == "resolved"
    assert fact.relation == "range"


@pytest.mark.parametrize("prefix", [
    "We shifted the meeting", "They delayed the event", "Delayed appointment",
    "We shifted availability", "The system frobnicated the meeting", "Reconfigured availability",
])
@pytest.mark.parametrize("window", ["2026-10-03 to 2026-10-05", "09:00 to 11:00"])
def test_transition_predicate_before_kind_does_not_look_like_a_direct_window(prefix, window):
    fact = extract(f"{prefix} from {window}.").facts[0]
    assert fact.status == "partial"
    assert fact.relation == "alternatives"
    assert "ambiguous_range_context" in fact.uncertainties


@pytest.mark.parametrize("prefix", ["The meeting", "Our event", "We are available", "I am available"])
def test_direct_subject_and_copula_window_prefixes_are_supported(prefix):
    fact = extract(f"{prefix} from 2026-10-03 to 2026-10-05.").facts[0]
    assert fact.status == "resolved"
    assert fact.relation == "range"


@pytest.mark.parametrize("suffix", ["off campus", "per schedule", "past noon", "down the hall", "as planned"])
def test_closed_preposition_class_prevents_zone_capture(suffix):
    fact = extract(f"Meeting 2026-10-03 at 3pm Europe/London {suffix}.", timezone="UTC").facts[0]
    assert fact.status == "resolved"
    assert fact.start.instants == ("2026-10-03T14:00:00+00:00",)
    assert fact.span.quote == "2026-10-03 at 3pm Europe/London"


@pytest.mark.parametrize("zone, offset", [("America/New_York", 4), ("America/Los_Angeles", 7)])
@pytest.mark.parametrize("clock, hour", [("15:00", 15), ("01:30", 1), ("3pm", 15), ("1:30am", 1)])
def test_audit_meridiem_boundary_preserves_complete_iana_zone(zone, offset, clock, hour):
    text = f"Meeting 2026-10-03 at {clock} {zone}."
    fact = extract(text, timezone="UTC").facts[0]
    assert fact.status == "resolved"
    assert fact.start.timezone == zone
    minute = 30 if "30" in clock else 0
    assert fact.start.instants == (f"2026-10-03T{hour + offset:02d}:{minute:02d}:00+00:00",)
    assert fact.span.quote == f"2026-10-03 at {clock} {zone}"


@pytest.mark.parametrize("zone", ["America/New_York", "America/Los_Angeles"])
@pytest.mark.parametrize("day, clock, issue", [
    ("2026-11-01", "01:30", "ambiguous_local_time"),
    ("2026-03-08", "02:30", "nonexistent_local_time"),
])
def test_24_hour_explicit_america_zone_preserves_dst_validation(zone, day, clock, issue):
    fact = extract(f"Meeting {day} at {clock} {zone}.", timezone="UTC").facts[0]
    assert fact.status == "partial"
    assert fact.start.timezone == zone
    assert fact.uncertainties == (issue,)
    assert len(fact.start.instants) == (2 if issue == "ambiguous_local_time" else 0)


@pytest.mark.parametrize("name", [
    "Eastern time", "Pacific time", "Central time", "Mountain Time",
    "British summer time", "New Zealand standard time", "local time", "Example timezone",
])
@pytest.mark.parametrize("introducer", ["", "in "])
@pytest.mark.parametrize("clock", ["3pm", "15:00"])
def test_audit_named_source_zone_is_preserved_as_ambiguity(name, introducer, clock):
    source_zone = introducer + name
    text = f"Meeting 2026-10-03 at {clock} {source_zone}."
    fact = extract(text, timezone="UTC").facts[0]
    assert fact.status == "partial"
    assert "ambiguous_timezone" in fact.uncertainties
    assert fact.start.instants == ()
    assert fact.start.timezone == source_zone
    assert fact.span.quote == f"2026-10-03 at {clock} {source_zone}"
    assert text[fact.span.start:fact.span.end] == fact.span.quote


@pytest.mark.parametrize("zone", ["", " UTC", " Europe/London"])
@pytest.mark.parametrize("continuation", ["4pm", "25:99", "noon", "midnight", "100:30"])
def test_audit_temporal_at_continuation_is_not_prose(zone, continuation):
    text = f"Meeting 2026-10-03 at 3pm{zone} at {continuation}."
    fact = extract(text, timezone="UTC").facts[0]
    assert fact.status == "partial"
    assert "unsupported_time_expression" in fact.uncertainties
    assert fact.start.instants == ()
    assert fact.span.quote.rstrip(".").endswith(f"at {continuation}")
    assert text[fact.span.start:fact.span.end] == fact.span.quote


@pytest.mark.parametrize("venue", ["the studio", "our office", "a shared desk"])
def test_prose_at_venue_does_not_become_temporal_continuation(venue):
    fact = extract(f"Meeting 2026-10-03 at 3pm UTC at {venue}.").facts[0]
    assert fact.status == "resolved"
    assert fact.start.instants == ("2026-10-03T15:00:00+00:00",)
    assert fact.span.quote == "2026-10-03 at 3pm UTC"


def test_24_hour_range_keeps_each_america_zone():
    fact = extract(
        "Available 2026-10-03 from 09:00 America/New_York to 11:00 America/Los_Angeles.",
        timezone="UTC",
    ).facts[0]
    assert fact.status == "resolved"
    assert fact.start.timezone == "America/New_York"
    assert fact.end.timezone == "America/Los_Angeles"
    assert fact.start.instants == ("2026-10-03T13:00:00+00:00",)
    assert fact.end.instants == ("2026-10-03T18:00:00+00:00",)


def test_standalone_24_hour_clock_has_intact_zone_and_only_missing_date():
    fact = extract("Meeting at 15:00 America/New_York.", timezone="UTC").facts[0]
    assert fact.start.hour == 15
    assert fact.start.timezone == "America/New_York"
    assert fact.uncertainties == ("missing_date",)


@pytest.mark.parametrize("named_zone", ["Eastern time", "in Pacific time", "New Zealand standard time"])
@pytest.mark.parametrize("suffix", ["with Alice", "at the studio", "for review"])
def test_named_zone_span_stops_before_prose(named_zone, suffix):
    text = f"Meeting 2026-10-03 at 15:00 {named_zone} {suffix}."
    fact = extract(text, timezone="UTC").facts[0]
    assert fact.status == "partial"
    assert fact.start.timezone == named_zone
    assert fact.start.instants == ()
    assert fact.span.quote == f"2026-10-03 at 15:00 {named_zone}"


def test_relative_date_cannot_use_fallback_when_source_named_zone_is_unknown():
    fact = extract("Report due tomorrow in Eastern time.", timezone="UTC").facts[0]
    assert fact.status == "unknown"
    assert fact.start.year is None
    assert "ambiguous_timezone" in fact.uncertainties
    assert fact.span.quote == "tomorrow in Eastern time"


@pytest.mark.parametrize("continuation", ["4 pm", "4:00 PM", "next Monday", "25:99 PM"])
def test_multiword_temporal_continuation_retains_complete_span(continuation):
    text = f"Meeting 2026-10-03 at 3pm UTC at {continuation}."
    fact = extract(text, timezone="UTC").facts[0]
    assert fact.status == "partial"
    assert fact.start.instants == ()
    assert fact.span.quote == f"2026-10-03 at 3pm UTC at {continuation}"
    assert text[fact.span.start:fact.span.end] == fact.span.quote


@pytest.mark.parametrize("zone, instant", [
    ("Europe/London", "2026-10-03T14:00:00+00:00"),
    ("America/New_York", "2026-10-03T19:00:00+00:00"),
    ("UTC", "2026-10-03T15:00:00+00:00"),
    ("+02:00", "2026-10-03T13:00:00+00:00"),
])
@pytest.mark.parametrize("clock", ["15:00", "3pm"])
@pytest.mark.parametrize("introducer", ["in", "IN"])
def test_audit_introduced_explicit_zone_overrides_fallback(zone, instant, clock, introducer):
    source_zone = f"{introducer} {zone}"
    text = f"Meeting 2026-10-03 at {clock} {source_zone}."
    fact = extract(text).facts[0]
    assert fact.status == "resolved"
    assert fact.start.instants == (instant,)
    assert fact.start.timezone == source_zone
    assert fact.span.quote == f"2026-10-03 at {clock} {source_zone}"
    assert text[fact.span.start:fact.span.end] == fact.span.quote
    assert fact.provenance.timezone == "America/Los_Angeles"


@pytest.mark.parametrize("zone", ["Europe/London", "America/New_York", "UTC"])
def test_introduced_zone_anchors_relative_date(zone):
    capture = datetime(2026, 9, 26, 1, tzinfo=timezone.utc)
    fact = extract(f"Report due tomorrow in {zone}.", captured_at=capture).facts[0]
    expected_day = 26 if zone == "America/New_York" else 27
    assert fact.status == "resolved"
    assert (fact.start.year, fact.start.month, fact.start.day) == (2026, 9, expected_day)
    assert fact.span.quote == f"tomorrow in {zone}"


@pytest.mark.parametrize("zone", ["Mars/Olympus", "America//New_York", "UTC+24:00"])
def test_invalid_introduced_zone_does_not_fall_back(zone):
    fact = extract(f"Meeting 2026-10-03 at 15:00 in {zone}.").facts[0]
    assert fact.status == "partial"
    assert "invalid_timezone" in fact.uncertainties
    assert fact.start.instants == ()
    assert fact.span.quote.endswith(f"in {zone}")


def test_conflicting_introduced_zone_is_preserved_and_uncertain():
    text = "Meeting 2026-10-03 at 3pm Europe/London in America/New_York."
    fact = extract(text).facts[0]
    assert fact.status == "partial"
    assert "conflicting_timezones" in fact.uncertainties
    assert fact.start.instants == ()
    assert fact.span.quote == "2026-10-03 at 3pm Europe/London in America/New_York"


@pytest.mark.parametrize("venue", ["the studio", "our office", "Rome", "the main hall"])
def test_introduced_zone_grammar_does_not_capture_ordinary_in_venues(venue):
    fact = extract(f"Meeting 2026-10-03 at 3pm Europe/London in {venue}.").facts[0]
    assert fact.status == "resolved"
    assert fact.start.instants == ("2026-10-03T14:00:00+00:00",)
    assert fact.span.quote == "2026-10-03 at 3pm Europe/London"


@pytest.mark.parametrize("expression", ["tomorrow", "at 15:00"])
def test_introduced_zone_conflict_is_retained_without_dated_clock(expression):
    text = f"Meeting {expression} in UTC in America/New_York."
    fact = extract(text, captured_at=datetime(2026, 9, 26, 1, tzinfo=timezone.utc)).facts[0]
    assert fact.status != "resolved"
    assert "conflicting_timezones" in fact.uncertainties
    assert fact.start.instants == ()
    assert fact.span.quote == f"{expression} in UTC in America/New_York"
    assert text[fact.span.start:fact.span.end] == fact.span.quote


@pytest.mark.parametrize("day, clock, issue", [
    ("2026-11-01", "01:30", "ambiguous_local_time"),
    ("2026-03-08", "02:30", "nonexistent_local_time"),
])
def test_introduced_america_zone_still_validates_dst(day, clock, issue):
    fact = extract(f"Meeting {day} at {clock} in America/New_York.").facts[0]
    assert fact.uncertainties == (issue,)
    assert fact.start.timezone == "in America/New_York"


def test_introduced_per_endpoint_zones_preserve_range_instants():
    fact = extract("Available 2026-10-03 from 09:00 in America/New_York to 11:00 in Europe/London.").facts[0]
    assert fact.start.instants == ("2026-10-03T13:00:00+00:00",)
    assert fact.end.instants == ("2026-10-03T10:00:00+00:00",)
    assert fact.uncertainties == ("reversed_range",)


@pytest.mark.parametrize("zone", [
    "PST", "BST", "CST", "pst", "bSt", "IST", "CEST", "NZDT",
    "JST", "AEST", "HKT", "EEST", "AKST", "WIB", "WITA",
])
@pytest.mark.parametrize("expression", ["2026-10-03 at 15:00", "at 15:00", "tomorrow"])
def test_audit_introduced_abbreviations_preserve_uncertain_source_zone(zone, expression):
    text = f"Meeting {expression} in {zone}."
    fact = extract(text, captured_at=datetime(2026, 9, 26, 1, tzinfo=timezone.utc)).facts[0]
    assert fact.status != "resolved"
    assert "ambiguous_timezone" in fact.uncertainties
    assert fact.start.instants == ()
    assert fact.span.quote == f"{expression} in {zone}"
    assert text[fact.span.start:fact.span.end] == fact.span.quote
    if expression == "tomorrow":
        assert (fact.start.year, fact.start.month, fact.start.day) == (None, None, None)


@pytest.mark.parametrize("zone", ["PST", "BST", "CST", "pst"])
@pytest.mark.parametrize("expression", ["2026-10-03 at 15:00", "at 15:00", "tomorrow"])
def test_introduced_abbreviation_after_explicit_zone_remains_conflicting(zone, expression):
    text = f"Meeting {expression} UTC in {zone}."
    fact = extract(text).facts[0]
    assert fact.status != "resolved"
    assert "conflicting_timezones" in fact.uncertainties
    assert fact.start.instants == ()
    assert fact.span.quote == f"{expression} UTC in {zone}"


def test_introduced_abbreviation_range_endpoints_do_not_use_fallback():
    fact = extract("Available 2026-10-03 from 09:00 in PST to 11:00 in BST.").facts[0]
    assert fact.status == "partial"
    assert "ambiguous_timezone" in fact.uncertainties
    assert fact.start.instants == fact.end.instants == ()
    assert fact.start.timezone == "in PST"
    assert fact.end.timezone == "in BST"


@pytest.mark.parametrize("expression", ["2026-10-03 at 15:00", "at 15:00", "tomorrow"])
def test_introduced_abbreviation_then_structured_zone_remains_conflicting(expression):
    fact = extract(f"Meeting {expression} in PST in UTC.").facts[0]
    assert "conflicting_timezones" in fact.uncertainties
    assert fact.start.instants == ()
    assert fact.span.quote == f"{expression} in PST in UTC"


@pytest.mark.parametrize("venue", ["Studio", "Rome", "the studio", "our office"])
def test_introduced_abbreviation_requires_a_whole_token(venue):
    fact = extract(f"Meeting 2026-10-03 at 15:00 in {venue}.").facts[0]
    assert fact.status == "resolved"
    assert fact.start.instants == ("2026-10-03T22:00:00+00:00",)
    assert fact.span.quote == "2026-10-03 at 15:00"


@pytest.mark.parametrize("zone", ["PST+2", "pst-08:00", "PST2"])
def test_malformed_introduced_abbreviation_does_not_restore_fallback(zone):
    fact = extract(f"Meeting 2026-10-03 at 15:00 in {zone}.").facts[0]
    assert fact.status == "partial"
    assert fact.start.instants == ()
    assert "ambiguous_timezone" in fact.uncertainties
    assert fact.span.quote == f"2026-10-03 at 15:00 in {zone}"


def test_introduced_abbreviation_preserves_following_prose_boundary():
    fact = extract("Meeting 2026-10-03 at 15:00 in PST with Alice.").facts[0]
    assert fact.status == "partial"
    assert fact.start.timezone == "in PST"
    assert fact.start.instants == ()
    assert fact.span.quote == "2026-10-03 at 15:00 in PST"


@pytest.mark.parametrize("zone", [
    "HAST", "HADT", "AKT", "PSTfoo", "PSTudio", "QX", "QZXabc+2", "THEST", "OURTZ", "ROME",
])
@pytest.mark.parametrize("expression", ["2026-10-03 at 15:00", "at 15:00", "tomorrow"])
@pytest.mark.parametrize("prior_zone", ["", " UTC"])
def test_audit_unknown_introduced_abbreviation_is_never_dropped(zone, expression, prior_zone):
    source = f"{expression}{prior_zone} in {zone}"
    fact = extract(f"Meeting {source}.", captured_at=datetime(2026, 9, 26, 1, tzinfo=timezone.utc)).facts[0]
    assert fact.status != "resolved"
    assert fact.start.instants == ()
    expected_issue = "conflicting_timezones" if prior_zone else "ambiguous_timezone"
    assert expected_issue in fact.uncertainties
    assert fact.span.quote == source
    if expression == "tomorrow" and not prior_zone:
        assert (fact.start.year, fact.start.month, fact.start.day) == (None, None, None)


@pytest.mark.parametrize("introducer", ["in", "IN", "In"])
def test_generated_unknown_introduced_abbreviations_never_inherit_fallback(introducer):
    rng = random.Random(806)
    # Vary uppercase stems and attached malformed tails independently of the
    # parser vocabulary. No generated token has a known intended offset.
    zones = {
        "".join(rng.choices(string.ascii_uppercase, k=size)) + tail
        for size in range(2, 11)
        for tail in ("", "foo", "2", "+02:00", "_unknown")
        for _ in range(3)
    }
    for zone in sorted(zones):
        for fallback in ("UTC", "America/Los_Angeles"):
            source = f"2026-10-03 at 15:00 {introducer} {zone}"
            fact = extract(f"Meeting {source} with Alice.", timezone=fallback).facts[0]
            assert fact.status == "partial", zone
            assert "ambiguous_timezone" in fact.uncertainties, zone
            assert fact.start.instants == (), zone
            assert fact.start.timezone == f"{introducer} {zone}", zone
            assert fact.span.quote == source, zone


@pytest.mark.parametrize("venue", [
    "Rome", "London", "Studio", "the studio", "our office", "a shared desk",
    "the main hall", "THE STUDIO", "OUR OFFICE", "A SHARED DESK", "the HQ", "our LAB",
])
@pytest.mark.parametrize("prior_zone", ["", " Europe/London"])
def test_structural_introduced_abbreviation_preserves_ordinary_venues(venue, prior_zone):
    source = f"2026-10-03 at 15:00{prior_zone}"
    fact = extract(f"Meeting {source} in {venue}.").facts[0]
    assert fact.status == "resolved"
    expected_hour = "14" if prior_zone else "22"
    assert fact.start.instants == (f"2026-10-03T{expected_hour}:00:00+00:00",)
    assert fact.span.quote == source


@pytest.mark.parametrize("zone", ["HAST", "QZX", "PSTfoo"])
def test_unknown_introduced_abbreviation_on_explicit_date_is_uncertain(zone):
    fact = extract(f"Report due 2026-10-03 in {zone}.").facts[0]
    assert fact.status == "partial"
    assert "ambiguous_timezone" in fact.uncertainties
    assert fact.span.quote == f"2026-10-03 in {zone}"


@pytest.mark.parametrize("zone", ["HADT", "QZX", "PSTfoo"])
def test_unknown_introduced_abbreviation_range_preserves_both_endpoints(zone):
    source = f"2026-10-03 from 09:00 in {zone} to 11:00 in UTC"
    fact = extract(f"Available {source}.").facts[0]
    assert fact.status == "partial"
    assert "ambiguous_timezone" in fact.uncertainties
    assert fact.start.instants == ()
    assert fact.end.instants == ("2026-10-03T11:00:00+00:00",)
    assert fact.span.quote == source
