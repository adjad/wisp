"""Reply phrasing helpers: Wisp's voice (gentle, plain, lead with the answer).

House rules these helpers keep: say only what a tool result shows; short lists
read as one sentence and only four or more items become bullets; no names, no
ritual closers; few em dashes (list rows use a colon)."""
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


def plural(n: int, word: str, many: str | None = None) -> str:
    """'1 email' / '3 emails'."""
    return f"{n} {word if n == 1 else (many or word + 's')}"


def join(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def with_others(items, k: int = 4) -> str:
    """'A, B and C' up to k names; beyond that 'A, B, C, D and a few others' (never 'and X and others')."""
    items = list(items)
    if len(items) <= k:
        return join(items)
    return ", ".join(items[:k]) + " and a few others"


def bullets(lines) -> str:
    return "\n".join(f"- {l}" for l in lines)


def listing(prose_head: str, items, list_head: str | None = None) -> str:
    """Bullets only for four or more items; shorter lists go into one sentence:
    '<prose_head> A, B and C.'  `list_head` defaults to the prose head with a colon."""
    items = [i for i in items if i]
    if len(items) >= 4:
        return (list_head or prose_head.rstrip(":.") + ":") + "\n" + bullets(items)
    return prose_head.rstrip(":. ") + ": " + join(items) + "."


def row_line(r, now, with_day=False) -> str:
    t = ts(r)
    when = "All day" if r.get("all_day") else clock(t)
    if with_day:
        when = f"{t:%a} {when}"
    title = r["title"]
    loc = f" ({r['location']})" if r.get("location") and r.get("location") not in ("Home",) else ""
    if r["source"] != "calendar":
        return f"{when}: {title} (reminder)"
    return f"{when}: {title}{loc}"


def row_prose(r, now, with_day=False, loc=True) -> str:
    """One agenda row inside a sentence: 'MATH 241 at 2 PM in Avery Science 110' (`loc=False` drops the place)."""
    t = ts(r)
    if r.get("all_day"):
        at = f"on {t:%a}, all day" if with_day else "all day"
    else:
        at = f"on {t:%a} at {clock(t)}" if with_day else f"at {clock(t)}"
    if r["source"] != "calendar":
        return f"the reminder “{r['title']}” {at}"
    where = f" in {r['location']}" if loc and r.get("location") and r.get("location") not in ("Home",) else ""
    return f"{r['title']} {at}{where}"


def agenda_reply(rows, phrase: str, now: dt.datetime, rng, *, empty=None, lead=None, total=None) -> str:
    """rows: upcoming_rows output. phrase: 'today', 'tomorrow', 'on Friday', 'this weekend'..."""
    cap = phrase[:1].upper() + phrase[1:]
    if not rows:
        return empty or rng.choice([
            f"Nothing is scheduled {phrase}, and there are no reminders either.",
            f"You have nothing on the calendar {phrase}, and no reminders either.",
            f"{cap} is clear, with no events or reminders.",
            f"There are no events or reminders {phrase}.",
            f"Your calendar and reminders are both clear {phrase}.",
            f"There's nothing planned {phrase}, and no reminders are due.",
        ])
    multi_day = len({ts(r).date() for r in rows}) > 1
    ended = [r for r in rows if r["source"] == "calendar" and r.get("end_ts", r["when_ts"]) <= now.timestamp()]
    live = [r for r in rows if r not in ended]
    if ended and not live:
        names = join(r["title"] for r in ended)
        return f"You're done for {phrase}. Earlier you had {names}, and nothing else is scheduled."
    if len(live) == 1 and not ended:
        r = live[0]
        t = ts(r)
        dw = day_word(t.date(), now)
        day = f" {dw}" if dw.replace("on ", "") not in phrase else ""
        if r["source"] == "calendar":
            loc = f" in {r['location']}" if r.get("location") and r["location"] != "Home" else ""
            return rng.choice([
                f"Just one thing {phrase}: {r['title']} at {clock(t)}{loc}.",
                f"You have {r['title']} at {clock(t)}{loc}{day}, and that's the only thing {phrase}.",
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
        parts.append(plural(n_ev, "event"))
    if n_rem:
        parts.append(plural(n_rem, "reminder"))
    both = " and ".join(parts)
    bare = phrase[3:] if phrase.startswith("on ") else phrase
    if lead:
        head = lead
    elif total is not None and len(counted) > len(live):
        head = f"You have {both} {phrase}. The next few"
    else:
        opts = [f"You have {both} {phrase}"]
        if not phrase.startswith("in "):
            # a plural period ("the next few days") takes "have", a day or week takes "has"
            verb = "have" if bare.rstrip().lower().endswith("s") and not bare.lower().endswith(("ss", "us")) else "has"
            opts.append(f"{bare[:1].upper() + bare[1:]} {verb} {both}")
        head = rng.choice(opts)
    if len(live) >= 4:
        body = listing(head, [row_line(r, now, with_day=multi_day) for r in live])
    else:
        body = listing(head, [row_prose(r, now, with_day=multi_day, loc=len(live) <= 2) for r in live])
    if ended:
        sep = "\n" if "\n" in body else " "
        body += sep + ("Earlier " + ("today " if phrase == "today" else "") + "you also had "
                       + join(r["title"] for r in ended) + ".").replace("  ", " ")
    return body
