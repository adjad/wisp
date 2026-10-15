"""Deterministic weekly measurements. Calendar load never means attendance.

Compare seven completed local dates with four preceding weeks. Coverage is an
explicit source assertion, never inferred from the timestamp of the oldest row.
"""

from __future__ import annotations

from datetime import timedelta

from service.behavior.evidence import completion_time
from service.behavior.timeutil import at_minute, day_start, local_dt


def _union(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def coverage_fraction(ranges, source, start, end):
    intervals = [
        (max(start, r["start_ts"]), min(end, r["end_ts"]))
        for r in ranges
        if r["source"] == source and r["state"] == "complete"
    ]
    seconds = sum(b - a for a, b in _union(intervals))
    return min(1.0, seconds / (end - start)) if end > start else 0.0


def weekly_report(observations, coverage, now, tz):
    today = local_dt(now, tz).date()
    windows = []
    for i in range(5):
        end_day = today - timedelta(days=7 * i)
        start_day = end_day - timedelta(days=7)
        windows.append(
            (start_day, end_day, day_start(start_day, tz), day_start(end_day, tz))
        )
    specs = (
        ("scheduled_meetings", "Scheduled meetings", "count", "calendar"),
        (
            "scheduled_meeting_hours",
            "Hours occupied by scheduled meetings",
            "hours",
            "calendar",
        ),
        ("scheduled_focus_hours", "Scheduled focus hours", "hours", "calendar"),
        (
            "adjacent_meeting_pairs",
            "Back-to-back scheduled meeting pairs",
            "count",
            "calendar",
        ),
        ("calendar_gap_hours", "Unbooked weekday hours, 9am–6pm", "hours", "calendar"),
        (
            "completed_reminders",
            "Reminders marked complete",
            "count",
            "completed_reminders",
        ),
        (
            "calendar_changes",
            "Observed calendar moves or removals",
            "count",
            "calendar_changes",
        ),
    )
    values = {key: [] for key, *_ in specs}
    fractions = {s: [] for s in {r[3] for r in specs}}
    evidence = {key: [] for key, *_ in specs}
    missing_ends = 0
    duration_complete = []
    count_complete = []
    for index, (first, last, start, end) in enumerate(windows):
        missing_in_window = sum(
            o.end_ts is None
            for o in observations
            if o.kind == "event" and o.ts < end
        )
        duration_complete.append(missing_in_window == 0)
        count_complete.append(not any(o.kind == "event" and o.end_ts is None and o.ts < start
                                      and o.category in {"meeting", "one_on_one", "call"} for o in observations))
        events = [
            o
            for o in observations
            if o.kind == "event" and o.ts < end
            and ((o.end_ts is None and o.ts >= start) or (o.end_ts is not None and o.end_ts > start))
        ]
        meetings = [
            o
            for o in events
            if o.category in {"meeting", "one_on_one", "call"}
            and not o.attrs.get("all_day")
        ]
        focus = [
            o for o in events if o.category == "focus" and not o.attrs.get("all_day")
        ]

        def duration(rows):
            return round(
                sum(
                    b - a
                    for a, b in _union(
                        (max(start, o.ts), min(end, o.end_ts))
                        for o in rows
                        if o.end_ts is not None and o.end_ts > o.ts
                    )
                )
                / 3600,
                3,
            )

        done = [
            o
            for o in observations
            if (t := completion_time(o)) is not None and start <= t < end
        ]
        changed = [
            o
            for o in observations
            if o.kind in {"event_moved", "event_cancelled"}
            and isinstance(o.attrs.get("detected_at"), (int, float))
            and start <= o.attrs["detected_at"] < end
        ]
        ordered = sorted(
            (o for o in meetings if o.end_ts is not None), key=lambda o: o.ts
        )
        adjacent = sum(
            1 for a, b in zip(ordered, ordered[1:]) if -300 <= b.ts - a.end_ts <= 600
        )
        gaps = 0.0
        for n in range(7):
            day = first + timedelta(days=n)
            if day.weekday() >= 5:
                continue
            a, b = at_minute(day, 540, tz), at_minute(day, 1080, tz)
            busy = _union(
                (max(a, o.ts), min(b, o.end_ts))
                for o in events
                if o.end_ts is not None and o.ts < b and o.end_ts > a
            )
            gaps += b - a - sum(y - x for x, y in busy)
        metrics = {
            "scheduled_meetings": len(meetings),
            "scheduled_meeting_hours": duration(meetings),
            "scheduled_focus_hours": duration(focus),
            "calendar_gap_hours": round(gaps / 3600, 3),
            "completed_reminders": len(done),
            "adjacent_meeting_pairs": adjacent,
            "calendar_changes": len(changed),
        }
        if index == 0:
            missing_ends = missing_in_window
            for key, rows in (
                ("scheduled_meetings", meetings),
                ("scheduled_meeting_hours", meetings),
                ("scheduled_focus_hours", focus),
                ("completed_reminders", done),
                ("calendar_changes", changed),
            ):
                evidence[key] = [o.id for o in rows[-5:]]
        for key, value in metrics.items():
            values[key].append(value)
        for source in fractions:
            fractions[source].append(coverage_fraction(coverage, source, start, end))
    metrics = []
    for key, label, unit, source in specs:
        current, *history = values[key]
        full = all(f >= 0.999999 for f in fractions[source])
        needs_ends = key in {
            "scheduled_meeting_hours",
            "scheduled_focus_hours",
            "calendar_gap_hours",
            "adjacent_meeting_pairs",
        }
        if needs_ends and not all(duration_complete):
            full = False
        if key == "scheduled_meetings" and not all(count_complete):
            full = False
        average = round(sum(history) / 4, 3) if full else None
        delta = round(current - average, 3) if average is not None else None
        value_reason = (
            "missing_event_end_times" if (needs_ends and not duration_complete[0])
            or (key == "scheduled_meetings" and not count_complete[0]) else
            "incomplete_calendar_coverage" if key == "calendar_gap_hours"
            and fractions[source][0] < 0.999999 else None
        )
        metrics.append(
            {
                "id": key,
                "label": label,
                "unit": unit,
                "source": source,
                "value": None if value_reason else current,
                "value_reason": value_reason,
                "coverage": round(fractions[source][0], 4),
                "baseline_weekly_average": average,
                "change": delta,
                "change_percent": round(delta / average * 100, 1) if average else None,
                "evidence": evidence[key],
                "comparison": "available" if full else "insufficient_coverage",
            }
        )
    return {
        "timezone": str(tz),
        "start_date": windows[0][0].isoformat(),
        "end_date_exclusive": today.isoformat(),
        "baseline_start_date": windows[-1][0].isoformat(),
        "metrics": metrics,
        "missing_event_ends": missing_ends,
        "note": "Seven completed local dates. Calendar records describe plans; task completions require a source completion date. Missing history is not zero activity.",
    }


def render_report(report):
    lines = [
        f"Weekly activity: {report['start_date']} to {report['end_date_exclusive']} (end exclusive)."
    ]
    for m in report["metrics"]:
        if m["value"] is None:
            reason = ("Calendar coverage is incomplete" if m.get("value_reason") == "incomplete_calendar_coverage"
                      else "event end times are missing")
            lines.append(f"- {m['label']}: unknown because {reason}")
            continue
        text = f"- {m['label']}: {m['value']:g} {m['unit']}"
        if m["baseline_weekly_average"] is not None:
            text += f"; previous four-week average {m['baseline_weekly_average']:g}; change {m['change']:+g}"
        else:
            text += "; observed records only — insufficient coverage for comparison"
        lines.append(text)
    return "\n".join(lines + [report["note"]])
