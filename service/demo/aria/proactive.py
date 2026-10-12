"""The Aria demo's proactive surfaces: the Daily Summary and the top-bar chip.

Both are composed from the synthetic world (world.py) and the REAL behavior
engine's output for the demo — delivered nudges (with their auto-performed
reminder/hold and Undo state) and today's predictions — never from native
data or hard-coded strings. A nudge shown here is the same nudge the hub
delivered as a card, so Undo / Useful / Not now keep working.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any

from service.demo.aria.world import USER_NAME, get_world

# Look-ahead the demo's behavior engine uses (behavior_runtime factory).
DEMO_NUDGE_HORIZON_S = 7 * 86400
FRESH_TICK_TIMEOUT_S = 20.0
_KIND_ORDER = {"habit_conflict": 0, "cancel_likely": 1, "contact_window": 2, "habit_missed": 3,
               "day_preview": 4}


def _tz():
    return datetime.now().astimezone().tzinfo


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, _tz()).strftime("%-I:%M %p").replace(":00 ", " ")


def _day_clock(ts: float, now: float) -> str:
    d = datetime.fromtimestamp(ts, _tz())
    today = datetime.fromtimestamp(now, _tz()).date()
    if d.date() == today:
        day = "today"
    elif d.date() == today + timedelta(days=1):
        day = "tomorrow"
    else:
        day = d.strftime("%A")
    return f"{day} {_clock(ts)}"


def _runtime():
    from service.behavior.flags import env_enabled
    if not env_enabled():
        return None
    from service.assistant import behavior_runtime
    return behavior_runtime.get_runtime()


async def refresh_behavior(store, hub) -> None:
    """Run one forced behavior tick (bounded) so the surfaces show current nudges."""
    if _runtime() is None:
        return
    from service.assistant import behavior_runtime
    try:
        await asyncio.wait_for(behavior_runtime.tick(store, hub, force=True), FRESH_TICK_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — a slow or failed tick just means older nudges
        pass


def heads_up(now: float | None = None, limit: int = 3) -> list[dict[str, Any]]:
    """Live delivered nudges, most important first: kind, title, body, the
    auto-performed action (and whether it was undone), and the hub event the
    app needs to show the nudge's card."""
    now = time.time() if now is None else now
    runtime = _runtime()
    if runtime is None:
        return []
    items = []
    for n in runtime.delivered(50):
        if n.get("verdict") in {"wrong", "undo", "not_now"}:
            continue
        expires = n.get("expires_at")
        if expires is not None and float(expires) <= now:
            continue
        if n.get("kind") == "day_preview" and (now - float(n.get("delivered_at") or 0)) > 18 * 3600:
            continue
        performed = [a for a in n.get("auto_performed") or [] if not a.get("undone")]
        items.append({
            "nudge_id": n["id"], "kind": n.get("kind", ""), "title": n.get("title", ""),
            "body": n.get("body", ""), "expires_at": expires,
            "performed": performed[0]["type"] if performed else "",
            "performed_title": performed[0].get("title", "") if performed else "",
            "event": {**{k: n.get(k) for k in ("kind", "title", "body", "because", "priority", "actions",
                                                "expires_at", "auto_performed")},
                      "type": "nudge", "nudge_id": n["id"], "event_id": n.get("event_id") or ""},
        })
    items.sort(key=lambda i: (_KIND_ORDER.get(i["kind"], 9), i["expires_at"] or float("inf")))
    return items[:limit]


def _chip(item: dict, now: float) -> dict | None:
    when = item.get("expires_at")
    at = f" {_clock(float(when))}" if when else ""
    if item["kind"] == "habit_conflict":
        label = f"⚠ Gym conflict{at}"
    elif item["kind"] == "cancel_likely":
        label = f"Likely skip{at}"
    elif item["kind"] == "contact_window":
        label = item["title"].rstrip("?")
    else:
        return None
    return {"label": label[:28], "kind": item["kind"], "nudge_id": item["nudge_id"], "event": item["event"]}


def topbar(now: float | None = None) -> dict[str, Any]:
    """Data for the collapsed top-center bar: the next synthetic event and at
    most one proactive chip (the most important live nudge)."""
    now = time.time() if now is None else now
    upcoming = get_world().events(now, now + 7 * 86400)
    nxt = next((e for e in upcoming if e["start_ts"] >= now), None)
    chip = next((c for c in (_chip(i, now) for i in heads_up(now, limit=5)) if c), None)
    return {
        "next": ({"title": nxt["title"], "when_ts": nxt["start_ts"], "label": _day_clock(nxt["start_ts"], now)}
                 if nxt else None),
        "chip": chip,
    }


def _heads_up_lines(now: float) -> list[str]:
    lines = []
    for item in heads_up(now):
        done = ""
        if item["performed"] == "add_reminder":
            done = " — reminder added (Undo on the card)"
        elif item["performed"] == "add_hold":
            done = " — tentative hold placed (Undo on the card)"
        lines.append(f"- **{item['title']}**{done}. {item['body']}")
    runtime = _runtime()
    if runtime is not None and len(lines) < 3:
        try:
            from service.assistant.behavior_runtime import predictions_for
            for p in predictions_for(runtime, "today", now):
                if p.kind == "contact" and p.ts + 3 * 3600 >= now:
                    lines.append(f"- {p.subject} usually gets in touch around {_clock(p.ts)}.")
                    break
        except Exception:  # noqa: BLE001
            pass
    return lines


def summary_sections(now: float | None = None) -> dict[str, str]:
    """The Daily Summary for the demo day, in brief._sections' shape."""
    now = time.time() if now is None else now
    w = get_world()
    tz = _tz()
    day = datetime.fromtimestamp(now, tz)
    start = datetime.combine(day.date(), datetime.min.time(), tz).timestamp()
    end_tomorrow = start + 2 * 86400

    out = [f"## {USER_NAME}'s day — {day:%A, %B %-d}", "_Aria demo — fictional data._", ""]
    heads = _heads_up_lines(now)
    if heads:
        out += ["### Heads up", *heads, ""]

    events = [e for e in w.events(now, end_tomorrow)]
    today_events = [e for e in events if e["start_ts"] < start + 86400]
    tomorrow_events = [e for e in events if e["start_ts"] >= start + 86400]
    out.append("### Schedule")
    if today_events:
        out += [f"- {_clock(e['start_ts'])} {e['title']}" + (f" ({e['location']})" if e.get("location") else "")
                for e in today_events[:8]]
    else:
        out.append("- Nothing else on the calendar today.")
    if tomorrow_events:
        out.append("- Tomorrow: " + "; ".join(f"{_clock(e['start_ts'])} {e['title']}" for e in tomorrow_events[:6]))
    out.append("")

    due = [r for r in w.reminders("", "all") if r["due_ts"] is not None and r["due_ts"] < start + 86400]
    if due:
        out.append("### Reminders")
        for r in due[:6]:
            tag = "overdue" if r["due_ts"] < now else _clock(r["due_ts"])
            out.append(f"- {r['title']} ({tag})")
        out.append("")

    unread = w.mail(folder="inbox", unread=True, count=12)
    out.append(f"### Mail — {len(unread)} unread")
    contacts = {(c.get("email") or "").casefold() for c in w.contacts()}
    people = [m for m in unread if (m["from_addr"] or "").casefold() in contacts]
    for m in (people or unread)[:5]:
        out.append(f"- **{m['from_name'] or m['from_addr']}** — {m['subject']}")
    if people:
        out.append(f"- Needs a reply: {', '.join(dict.fromkeys(m['from_name'] or m['from_addr'] for m in people[:4]))}")
    out.append("")

    msgs = [m for m in w.messages(unread=True, count=20) if m["direction"] == "in"]
    out.append(f"### Messages — {len(msgs)} unread")
    seen = set()
    for m in msgs:
        if m["contact"] in seen:
            continue
        seen.add(m["contact"])
        who = m.get("sender") or m["contact"]
        out.append(f"- **{who}**" + (f" ({m['contact']})" if who != m["contact"] else "") + f": {m['text'][:140]}")
        if len(seen) >= 4:
            break
    text = "\n".join(out).strip()
    today_line = f"{len(today_events)} events today" + (f"; {len(heads)} heads-up" if heads else "")
    return {"FULL": text, "TODAY": today_line, "MESSAGES": f"{len(msgs)} unread messages", "READY": "1"}
