"""Reply phrasing helpers: Wisp's voice (gentle, plain, lead with the answer)."""
from __future__ import annotations

import datetime as dt

from world import short_clock


def clock(t: dt.datetime) -> str:
    return short_clock(t)


def ts(row) -> dt.datetime:
    return dt.datetime.fromtimestamp(row["when_ts"])


def day_word(d: dt.date, now: dt.datetime) -> str:
    """Relative day for prose: today / tomorrow / yesterday / on Friday / on Friday, Nov 6."""
    delta = (d - now.date()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 0 < delta < 7:
        return "on " + d.strftime("%A")
    if -7 < delta < 0:
        return "last " + d.strftime("%A") if d.weekday() >= now.weekday() else "on " + d.strftime("%A")
    return "on " + d.strftime("%A, %B %-d")


def row_line(r, now, with_day=False) -> str:
    t = ts(r)
    when = "All day" if r.get("all_day") else clock(t)
    if with_day:
        when = f"{t:%a} {when}"
    title = r["title"]
    if r["source"] != "calendar":
        title = f"Reminder: {title}"
    loc = f" ({r['location']})" if r.get("location") and r.get("location") not in ("Home",) else ""
    return f"- {when} — {title}{loc}"


def agenda_reply(rows, phrase: str, now: dt.datetime, rng, *, empty=None, lead=None, total=None) -> str:
    """rows: upcoming_rows output. phrase: 'today', 'tomorrow', 'on Friday', 'this weekend'..."""
    if not rows:
        return empty or rng.choice([
            f"Nothing's scheduled {phrase} — no events or reminders.",
            f"You have nothing on the calendar {phrase}, and no reminders either.",
            f"{phrase[:1].upper() + phrase[1:]} is clear: no events or reminders.",
        ])
    multi_day = len({ts(r).date() for r in rows}) > 1
    ended = [r for r in rows if r["source"] == "calendar" and r.get("end_ts", r["when_ts"]) <= now.timestamp()]
    live = [r for r in rows if r not in ended]
    if ended and not live:
        names = ", ".join(r["title"] for r in ended)
        return (f"You're done for {phrase}: earlier you had {names}. Nothing else is scheduled.")
    if len(live) == 1 and not ended:
        r = live[0]
        t = ts(r)
        dw = day_word(t.date(), now)
        day = f" {dw}" if dw.replace("on ", "") not in phrase else ""
        if r["source"] == "calendar":
            loc = f" in {r['location']}" if r.get("location") and r["location"] != "Home" else ""
            return rng.choice([
                f"Just one thing {phrase}: {r['title']} at {clock(t)}{loc}.",
                f"You have {r['title']} at {clock(t)}{loc}{day} — that's the only thing {phrase}.",
            ])
        return rng.choice([
            f"No events {phrase}, just one reminder: {r['title']} at {clock(t)}.",
            f"Only a reminder {phrase}: {r['title']}, due at {clock(t)}.",
        ])
    counted = [r for r in (total if total is not None else live) if r not in ended]
    n_ev = sum(r["source"] == "calendar" for r in counted)
    n_rem = len(counted) - n_ev
    parts = []
    if n_ev:
        parts.append(f"{n_ev} event{'s' if n_ev != 1 else ''}")
    if n_rem:
        parts.append(f"{n_rem} reminder{'s' if n_rem != 1 else ''}")
    bare = phrase[3:] if phrase.startswith("on ") else phrase
    head = lead or rng.choice([f"Here's {bare}: {' and '.join(parts)}.",
                               f"You have {' and '.join(parts)} {phrase}:",
                               f"{bare[:1].upper() + bare[1:]} you've got {' and '.join(parts)}:"])
    if total is not None and len(counted) > len(live):
        head = f"You have {' and '.join(parts)} {phrase}. The next few:"
    lines = [row_line(r, now, with_day=multi_day) for r in live]
    out = head + "\n" + "\n".join(lines)
    if ended:
        out += "\nEarlier " + ("today" if phrase == "today" else "") + " you also had " + ", ".join(
            r["title"] for r in ended) + "."
        out = out.replace("Earlier  you", "Earlier you")
    return out


def join(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]
