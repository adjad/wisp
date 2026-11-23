"""Counter-only usage analytics: how the user uses the app, with no content.

The store keeps one row per (local day, local hour, event, detail) with a count
and a summed duration. ``detail`` is a closed, code-chosen label (a tab id, a
tool name, ``local`` or ``cloud``): never a prompt, a title, a name or a path.
Everything shown to the user (patterns in the Learned window, the weekly report)
is composed from these counters.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta, tzinfo
from typing import Any, Iterable

from service.behavior.miners import pattern_id, wilson_lower
from service.behavior.timeutil import fmt_range, local_dt
from service.behavior.types import Pattern, Window

EVENTS = ("ask", "tab_opened", "tool_used", "approval", "denial")
ASK_DETAILS = ("local", "cloud")
_DETAIL = re.compile(r"^[a-z0-9_.:-]{1,48}$")
LOOKBACK_DAYS = 28
MIN_USES = 30           # busiest hours need this many uses
MIN_LIST = 10           # a top-3 list needs this many events
MIN_ASKS = 20           # local-vs-cloud and response time need this many asks


def clean_detail(event: str, detail: Any) -> str | None:
    """A validated detail label, or None when the event is not countable."""
    if event not in EVENTS:
        return None
    value = str(detail or "").strip().lower().replace(" ", "_")
    if event == "ask":
        return value if value in ASK_DETAILS else None
    return value if _DETAIL.match(value) else None


def _name(label: str) -> str:
    return label.replace("_", " ").replace("-", " ")


def _top(counter: Counter, k: int = 3) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:k]


def _since(rows: Iterable[dict], first: date) -> list[dict]:
    iso = first.isoformat()
    return [r for r in rows if r["day"] >= iso]


def _sum(rows: Iterable[dict], event: str) -> Counter:
    out: Counter = Counter()
    for r in rows:
        if r["event"] == event:
            out[r["detail"]] += int(r["n"])
    return out


def _uses_by_hour(rows: Iterable[dict]) -> Counter:
    hours: Counter = Counter()
    for r in rows:
        if r["event"] in ("ask", "tab_opened"):
            hours[int(r["hour"])] += int(r["n"])
    return hours


def _pattern(subject: str, text: str, *, n: int, support: int, opportunities: int, label: str,
             confidence: float, last: float | None, window: Window | None = None, **stats) -> Pattern:
    return Pattern(
        id=pattern_id("usage_stat", subject), kind="usage_stat", subject=subject, statement=text,
        window=window or Window(), support=support, opportunities=opportunities, confidence=confidence,
        last_seen=last, stats={"samples": n, "evidence_label": label, "basis": "observed",
                               "evidence_version": 2, **stats})


def mine_usage(rows: list[dict], now: float, tz: tzinfo) -> list[Pattern]:
    today = local_dt(now, tz).date()
    rows = _since(rows, today - timedelta(days=LOOKBACK_DAYS))
    if not rows:
        return []
    last = now
    out: list[Pattern] = []

    hours = _uses_by_hour(rows)
    total = sum(hours.values())
    if total >= MIN_USES:
        start, best = max(((h, hours[h] + hours[h + 1]) for h in range(23)), key=lambda kv: (kv[1], -kv[0]))
        share = best / total
        if share >= 0.3:
            out.append(_pattern(
                "busiest_hours",
                f"You use Wisp most between {fmt_range(start * 60, (start + 2) * 60)} "
                f"({round(100 * share)}% of {total} uses in the last four weeks).",
                n=total, support=best, opportunities=total, confidence=wilson_lower(best, total),
                label=f"{best} of {total} uses in the last four weeks", last=last,
                window=Window(start_minute=start * 60, end_minute=(start + 2) * 60)))

    tabs = _sum(rows, "tab_opened")
    if sum(tabs.values()) >= MIN_LIST:
        top = _top(tabs)
        n = sum(tabs.values())
        out.append(_pattern("tabs", "The tabs you open most: " + ", ".join(
            f"{_name(t).capitalize()} ({c})" for t, c in top) + ".", n=n, support=sum(c for _, c in top),
            opportunities=n, confidence=round(n / (n + 20), 4), last=last,
            label=f"from {n} tab opens in the last four weeks"))

    tools = _sum(rows, "tool_used")
    if sum(tools.values()) >= MIN_LIST:
        top = _top(tools)
        n = sum(tools.values())
        out.append(_pattern("tools", "The tools Wisp uses most for you: " + ", ".join(
            f"{_name(t)} ({c})" for t, c in top) + ".", n=n, support=sum(c for _, c in top),
            opportunities=n, confidence=round(n / (n + 20), 4), last=last,
            label=f"from {n} tool uses in the last four weeks"))

    ok, no = sum(_sum(rows, "approval").values()), sum(_sum(rows, "denial").values())
    if ok + no >= MIN_LIST:
        out.append(_pattern("approvals", f"You approve {round(100 * ok / (ok + no))}% of the actions Wisp asks "
                            f"about ({ok} of {ok + no}).", n=ok + no, support=ok, opportunities=ok + no,
                            confidence=wilson_lower(ok, ok + no), last=last,
                            label=f"{ok} of {ok + no} approvals in the last four weeks"))

    asks = _sum(rows, "ask")
    n_asks = sum(asks.values())
    if n_asks >= MIN_ASKS:
        local, cloud = asks.get("local", 0), asks.get("cloud", 0)
        text = (f"All {n_asks} of your recent questions were answered on this Mac." if not cloud else
                f"{round(100 * local / n_asks)}% of your recent questions were answered on this Mac "
                f"and {round(100 * cloud / n_asks)}% by a cloud model.")
        out.append(_pattern("local_cloud", text, n=n_asks, support=local, opportunities=n_asks,
                            confidence=wilson_lower(local, n_asks), last=last,
                            label=f"{local} of {n_asks} questions answered on this Mac", local=local, cloud=cloud))
        ms: dict[str, list[int]] = {"local": [0, 0], "cloud": [0, 0]}
        for r in rows:
            if r["event"] == "ask" and r["detail"] in ms and int(r["total_ms"]) > 0:
                ms[r["detail"]][0] += int(r["total_ms"])
                ms[r["detail"]][1] += int(r["n"])
        parts, stats = [], {}
        for mode, where in (("local", "on this Mac"), ("cloud", "from a cloud model")):
            total_ms, count = ms[mode]
            if count >= MIN_ASKS:
                seconds = round(total_ms / count / 1000, 1)
                parts.append(f"{seconds:g} seconds {where}")
                stats[f"{mode}_seconds"] = seconds
        if parts:
            counted = sum(c for _, c in ms.values())
            out.append(_pattern("response_time", "Answers take about " + " and about ".join(parts)
                                + " on average.", n=counted, support=counted, opportunities=counted,
                                confidence=round(counted / (counted + 20), 4), last=last,
                                label=f"averaged over {counted} timed questions", **stats))
    return out


# ---------------------------------------------------------------- weekly report
def usage_report(rows: list[dict], now: float, tz: tzinfo) -> dict[str, Any]:
    """Seven completed local days ending at today's midnight, against the four weeks before."""
    today = local_dt(now, tz).date()
    start = today - timedelta(days=7)
    base_start = start - timedelta(days=28)
    week = [r for r in rows if start.isoformat() <= r["day"] < today.isoformat()]
    base = [r for r in rows if base_start.isoformat() <= r["day"] < start.isoformat()]
    first_day = min((r["day"] for r in rows), default=None)
    report: dict[str, Any] = {"available": bool(week), "start_date": start.isoformat(),
                              "end_date_exclusive": today.isoformat(), "lines": [], "note": ""}
    if not week:
        report["note"] = ("Usage counts are collected while learning is on. A weekly summary appears after "
                          "the first full day of use.")
        return report
    lines: list[str] = []
    asks = _sum(week, "ask")
    n_asks = sum(asks.values())
    have_baseline = first_day is not None and first_day <= base_start.isoformat()

    def compare(value: int, event: str) -> str:
        if not have_baseline:
            return ""
        average = sum(_sum(base, event).values()) / 4
        return f" (previous four-week average {average:g}; change {value - average:+g})"

    lines.append(f"Questions asked: {n_asks}{compare(n_asks, 'ask')}")
    if n_asks:
        local, cloud = asks.get("local", 0), asks.get("cloud", 0)
        lines.append(f"Answered on this Mac: {local} of {n_asks}" + (f"; by a cloud model: {cloud}" if cloud else ""))
        timed = [(int(r["total_ms"]), int(r["n"])) for r in week if r["event"] == "ask" and int(r["total_ms"]) > 0]
        count = sum(c for _, c in timed)
        if count:
            lines.append(f"Average response time: {sum(m for m, _ in timed) / count / 1000:.1f} seconds")
    tabs = _sum(week, "tab_opened")
    if tabs:
        lines.append(f"Tabs opened: {sum(tabs.values())} ("
                     + ", ".join(f"{_name(t).capitalize()} {c}" for t, c in _top(tabs)) + ")")
    tools = _sum(week, "tool_used")
    if tools:
        lines.append(f"Tool uses: {sum(tools.values())} (" + ", ".join(f"{_name(t)} {c}" for t, c in _top(tools)) + ")")
    ok, no = sum(_sum(week, "approval").values()), sum(_sum(week, "denial").values())
    if ok or no:
        lines.append(f"Actions approved: {ok}; declined: {no}")
    hours = _uses_by_hour(week)
    if hours:
        lines.append("Busiest hours: " + ", ".join(
            f"{fmt_range(h * 60, (h + 1) * 60)} ({c})" for h, c in sorted(
                hours.items(), key=lambda kv: (-kv[1], kv[0]))[:2]))
    if not have_baseline:
        lines.append("Earlier weeks are not compared: counting began on " + (first_day or today.isoformat()) + ".")
    report["lines"] = lines
    return report


def render_lines(report: dict[str, Any]) -> list[str]:
    if not report.get("available"):
        return [report.get("note", "")] if report.get("note") else []
    return ["Wisp usage (counts only):"] + [f"- {line}" for line in report["lines"]]
