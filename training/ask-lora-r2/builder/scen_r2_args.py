"""Round-2 write families 4, 6 and 7: exact_args, right_write_tool, relay_receipt.

Every scenario is a scripted "ideal model" turn over the Sam Rivera synthetic
world (see scen.py / writes.py). Write turns carry ``approve`` (one entry per
approval card, in order) and ``expect_cards`` (the tools whose card the host
must raise), exactly as writes.py describes.

Host facts this file scripts around (measured on this branch):
- an argument the registry rejects (unknown name, wrong type, missing field) is
  answered with the real "(error calling ...)" text BEFORE any card or claim, so
  a ``Bad(...)`` step raises no card and the turn goes on;
- a refusal from update_event's pre-card check (read-only calendar, not the
  organizer, invitees unsynced, no match, several matches, no-op) also raises no
  card, and the model writes the final;
- after a card, a failed send/note/draft ends the turn with the host's own text,
  so those turns are not scripted;
- draft_message ends its own turn, but the harness stub strips the draft class,
  so a draft_message turn cannot be built; text drafts are left out.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import gen
import scen
from gen import D
from harness import Bad, Call, Final
from replies import clock, join
from scen import Scenario, setup
from world import Event, Note, Reminder

WDN = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
CARD_TOOLS = {"send_email", "send_message", "reply_to_email", "schedule_send", "add_calendar_event",
              "update_event", "cancel_event", "clear_reminders"}


_RNG = [None]


@dataclass
class WScenario(Scenario):
    approve: list | None = None
    expect_cards: list | None = None


def S(family, prompt, now, w, steps, notes, *, deny=None, cards="auto", approve=None):
    """Build a WScenario. ``cards='auto'``: one card per non-Bad call to a card tool;
    ``deny`` = the index of the card that is denied (the script then ends at that call)."""
    expect = cards
    if cards == "auto":
        expect = [c.name for st in steps if isinstance(st, list) for c in st if c.name in CARD_TOOLS]
    if approve is None:
        approve = [True] * len(expect)
        if deny is not None:
            approve[deny] = False
    prompt = casual(_RNG[0], prompt)
    return WScenario(family, prompt, now, w, steps, notes, approve=approve or None,
                     expect_cards=list(expect) or None)


# ----------------------------------------------------------------- helpers
def mk(rng, *, hour=None, weekday=None, **kw):
    kw.setdefault("extra", 0)
    kw.setdefault("exams", False)
    if hour is not None:
        kw["hour"] = hour
    if weekday is not None:
        kw["weekday"] = weekday
    return setup(rng, **kw)


def nxt(now, wd):
    """The next occurrence of weekday index ``wd`` strictly after today."""
    return now.date() + D(days=(wd - now.weekday()) % 7 or 7)


def at(day, h, m=0):
    return dt.datetime.combine(day, dt.time(h, m))


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M")


def cap(s):
    return s[:1].upper() + s[1:]


def low1(s):
    return s[:1].lower() + s[1:]


def t_txt(rng, h, m=0, *, style=None):
    """A spoken clock time: 4pm, 4:30 pm, noon, 16:30 ..."""
    if h == 12 and m == 0 and rng.random() < 0.4:
        return "noon"
    style = style or rng.choice(["a", "a", "b", "b", "c", "d"])
    h12 = h % 12 or 12
    ap = "am" if h < 12 else "pm"
    if style == "c" and m == 0:
        return f"{h12} {ap}"
    if style == "d" and h >= 13 and rng.random() < 0.5:
        return f"{h}:{m:02d}"
    mm = f":{m:02d}" if m else ""
    return f"{h12}{mm}{ap}" if style in ("a", "c", "d") else f"{h12}{mm} {ap}"


def wd_txt(rng, wd):
    name = WDN[wd]
    return name.capitalize() if rng.random() < 0.25 else name


DUR_WORDS = {30: "30 minutes", 45: "45 minutes", 60: "an hour", 90: "an hour and a half", 120: "two hours",
             180: "three hours", 240: "four hours"}


def dur_w(mins):
    return DUR_WORDS.get(mins, f"{mins} minutes")


def dlabel(day, now, *, bare=False):
    """today / tomorrow / Thursday, Nov 5."""
    delta = (day - now.date()).days
    if delta == 0 and not bare:
        return "today"
    if delta == 1 and not bare:
        return "tomorrow"
    return f"{day:%A, %b %-d}"


def span_txt(start, mins):
    end = start + D(minutes=mins)
    a, b = clock(start), clock(end)
    if a[-2:] == b[-2:]:
        a = a[:-3]
    return f"{a} to {b}"


def pick(rng, seq):
    return seq[rng.randrange(len(seq))]


PREF_HOURS = {"Trivia night": [18, 19, 20], "Board game night": [18, 19], "Climbing with Theo": [17, 18, 19],
              "Run with Ben": [7, 8, 16, 17], "Pottery class": [10, 13, 14, 17], "Piano lesson": [15, 16, 17],
              "Eye exam": [9, 10, 11, 14], "Bike tune-up": [9, 10, 13, 15], "Roommate meeting": [12, 19, 20],
              "Call with advisor": [9, 10, 11, 14, 15], "Resume workshop": [10, 11, 14, 16],
              "Tutoring session": [11, 14, 16, 17], "Design review": [10, 11, 14, 15],
              "Lab group check-in": [10, 12, 15, 16]}


def ehour(rng, title, *, daytime=False):
    hs = PREF_HOURS.get(title, [9, 10, 11, 13, 14, 15, 16])
    if daytime:
        hs = [h for h in hs if h <= 14] or [10]
    return pick(rng, hs)


TYPOS = {"tomorrow": "tommorow", "schedule": "schedual", "appointment": "apointment", "saturday": "saterday",
         "wednesday": "wensday", "tonight": "tonite", "reminder": "remindr", "thursday": "thurs", "please": "pls",
         "remind": "remnd", "calendar": "calender", "friday": "fri", "meeting": "meetng"}


def casual(rng, s):
    """Light real-typing noise on ~1 in 4 prompts: a typo, a lead-in or a tail."""
    r = rng.random()
    if r < 0.12:
        words = s.split(" ")
        idx = [k for k, wd_ in enumerate(words) if wd_.lower().strip(",.:") in TYPOS]
        if idx:
            k = pick(rng, idx)
            core = words[k].lower().strip(",.:")
            words[k] = words[k].lower().replace(core, TYPOS[core])
            return " ".join(words)
    elif r < 0.20:
        return pick(rng, ["hey ", "ok ", "quick one: ", "so "]) + s
    elif r < 0.27:
        return s + pick(rng, [" pls", " thanks", " please", " :)"])
    return s


def add_event(w, title, start, mins=60, loc="", **kw):
    w.events.append(Event(title, start, mins, loc, **kw))
    w.events.sort(key=lambda e: e.start)


def ev_phrase(title, start, mins, loc, now):
    """The part of a final that restates an added/moved event, from the receipt."""
    where = f" at {loc}" if loc else ""
    if mins == 60:
        return f"{dlabel(start.date(), now)} at {clock(start)}{where}"
    return f"{dlabel(start.date(), now)} from {span_txt(start, mins)}{where}"


# Events the user might add (prompt noun, calendar title, optional place).
NEW_EVENTS = [
    ("piano lesson", "Piano lesson", ""),
    ("pottery class", "Pottery class", "Clay Studio on 5th"),
    ("a call with my advisor", "Call with advisor", ""),
    ("bike tune-up", "Bike tune-up", "Spoke & Chain"),
    ("the lab group check-in", "Lab group check-in", "Kerr Hall 301"),
    ("resume workshop", "Resume workshop", "Career Center"),
    ("a run with Ben", "Run with Ben", "Riverside Track"),
    ("trivia night", "Trivia night", "The Rusty Anchor"),
    ("an eye exam", "Eye exam", "Lakeview Optical"),
    ("roommate meeting", "Roommate meeting", ""),
    ("climbing with Theo", "Climbing with Theo", "Cascade Climbing Gym"),
    ("tutoring session", "Tutoring session", "Hollis Library"),
    ("a design review", "Design review", ""),
    ("the board game night", "Board game night", "Maya's place"),
]
# Existing one-off events the user can move: (prompt reference, title, key the model passes, place).
OLD_EVENTS = [
    ("the dentist", "Dentist appointment", "Dentist", "Pine Street Dental"),
    ("coffee with Theo", "Coffee with Theo", "Coffee with Theo", "Brewed Awakening"),
    ("the haircut", "Haircut", "Haircut", "Clipper Joe's"),
    ("my advising appointment", "Advising appointment", "Advising", "Dean's Office, Mercer Hall"),
    ("dinner with Carmen", "Dinner with Carmen", "Dinner with Carmen", "Golden Lotus"),
    ("the project sync", "Project sync with Jordan", "Project sync", "Kerr Hall 301"),
    ("yoga", "Yoga class", "Yoga", "Rec Center Studio B"),
    ("the career fair", "Career fair", "Career fair", "Student Union Ballroom"),
    ("office hours with Okafor", "Office hours — Prof. Okafor", "Office hours", "Kerr Hall 412"),
    ("movie night", "Movie night with Maya", "Movie night", "Home"),
]
NEW_REMS = [
    ("call the landlord", "Call the landlord"),
    ("mail the package", "Mail the package"),
    ("take out the recycling", "Take out the recycling"),
    ("charge the camera", "Charge the camera"),
    ("order the textbook", "Order the textbook"),
    ("submit the time sheet", "Submit the time sheet"),
    ("pick up the refill", "Pick up the refill"),
    ("email Hana about the regrade", "Email Hana about the regrade"),
    ("buy a birthday card for Mom", "Buy a birthday card for Mom"),
    ("check the lab results", "Check the lab results"),
    ("pack the gym bag", "Pack the gym bag"),
    ("feed the neighbor's cat", "Feed the neighbor's cat"),
]
# Existing reminders to change: (prompt reference, title, key).
OLD_REMS = [
    ("the dry cleaning", "Pick up dry cleaning", "dry cleaning"),
    ("the router return", "Return the router", "router"),
    ("print boarding pass", "Print boarding pass", "boarding pass"),
    ("the bank call", "Call the bank about the card", "bank"),
    ("the printer ink", "Buy printer ink", "printer ink"),
    ("the lease form", "Send the lease form", "lease form"),
    ("the donation drop-off", "Drop off the donation box", "donation"),
]


def world_with(rng, *, hour=None, weekday=None, evs=(), rems=(), **kw):
    now, w = mk(rng, hour=hour, weekday=weekday, **kw)
    for e in evs:
        add_event(w, *e[:4], **(e[4] if len(e) > 4 else {}))
    for title, due in rems:
        w.reminders.append(Reminder(title, due))
    return now, w


def target_day(rng, rel):
    """Pick (weekday index, now-weekday, now-hour) so the bare weekday is `rel` relative to today.
    Returns (wd, now_wd, hour)."""
    wd = rng.randrange(7)
    if rel == "yesterday":
        return wd, (wd + 1) % 7, None
    if rel == "two_ago":
        return wd, (wd + 2) % 7, None
    if rel == "three_ago":
        return wd, (wd + 3) % 7, None
    if rel == "today":
        return wd, wd, rng.choice([15, 16, 18, 20, 21])
    k = rng.choice([2, 3, 4, 5])
    return wd, (wd - k) % 7, None


def rel_cycle(i):
    return ["yesterday", "ahead", "today", "two_ago", "ahead", "three_ago", "ahead", "yesterday", "today", "ahead"][i % 10]


def ok_tail(rng, kind="event"):
    return rng.choice(["", "", " Anything else?", " Want me to set a reminder too?" if kind == "event" else ""])


def add_final(rng, title, start, mins, loc, now):
    ph = ev_phrase(title, start, mins, loc, now)
    return pick(rng, [f"Added {title} for {ph}.",
                      f"{title} is on the schedule for {ph}.",
                      f"All set: {title} is down for {ph}.",
                      f"Done. {title} is booked for {ph}.",
                      f"Got it, {title} is down for {ph}.",
                      f"Okay, {title} is on for {ph}.",
                      f"You're booked: {title}, {ph}."])


# =============================================================================
# 4. exact_args
# =============================================================================
def _add_event_weekday(rng, i):
    rel = rel_cycle(i)
    wd, now_wd, hr = target_day(rng, rel)
    np_, title, loc = pick(rng, [e for e in NEW_EVENTS if rel != "today" or min(PREF_HOURS.get(e[1], [9])) < 12])
    h = ehour(rng, title, daytime=rel == "today")
    m = rng.choice([0, 0, 0, 30])
    now, w = mk(rng, hour=hr, weekday=now_wd)
    day = nxt(now, wd)
    start = at(day, h, m)
    use_loc = bool(loc) and rng.random() < 0.5
    wdw, tw = wd_txt(rng, wd), t_txt(rng, h, m)
    place = f" at {loc}" if use_loc else ""
    prompt = pick(rng, [
        f"pencil in {np_} {wdw} at {tw}{place}",
        f"can you put {np_} down for {wdw} {tw}{place}",
        f"add {np_} to the schedule, {wdw} at {tw}{place}",
        f"i've got {np_} {wdw} around {tw}{place}, put it in",
        f"{wdw} {tw}{place}: {np_}. add it",
        f"stick {np_} on {wdw} at {tw}{place} for me",
        f"{np_} {wdw} {tw}{place}, can you schedule it?",
        f"schedule {np_} for {wdw} at {tw}{place}",
    ])
    args = {"title": title, "when_iso": iso(start)}
    if use_loc:
        args["location"] = loc
    final = add_final(rng, title, start, 60, loc if use_loc else "", now)
    return S("exact_args", prompt, now, w, [[Call("add_calendar_event", **args)], Final(final)],
             f"bare weekday ({rel}) = next occurrence after today -> {day}")


def _add_event_duration(rng, i):
    wd = rng.randrange(7)
    now, w = mk(rng, weekday=(wd - rng.choice([1, 2, 3, 4])) % 7)
    day = nxt(now, wd)
    blocks = [("deep work", "Deep work"), ("linear algebra study", "Linear algebra study"),
              ("an OS reading block", "OS reading block"), ("a lab write-up session", "Lab write-up session")]
    kind = i % 4
    wdw = wd_txt(rng, wd)
    if kind == 0:   # from A to B
        h = rng.choice([9, 10, 13, 14, 15])
        m1 = rng.choice([0, 30])
        mins = rng.choice([90, 120, 150])
        np_, title = pick(rng, blocks)
        end = at(day, h, m1) + D(minutes=mins)
        prompt = pick(rng, [f"{np_} {wdw} from {t_txt(rng, h, m1, style='b')} to {t_txt(rng, end.hour, end.minute, style='b')}",
                            f"reserve {wdw} {t_txt(rng, h, m1, style='a')} to {t_txt(rng, end.hour, end.minute, style='a')} for {np_}",
                            f"i need {np_} {wdw}, {t_txt(rng, h, m1, style='c')} until {t_txt(rng, end.hour, end.minute, style='c')}"])
        start, loc = at(day, h, m1), ""
    elif kind == 1:  # for N
        np_, title, loc0 = pick(rng, NEW_EVENTS)
        mins = pick(rng, [45, 90, 120, 30])
        h = ehour(rng, title)
        start, loc = at(day, h, 0), ""
        prompt = pick(rng, [f"{np_} {wdw} at {t_txt(rng, h)} for {dur_w(mins)}",
                            f"put {np_} on {wdw} at {t_txt(rng, h)}, it runs {dur_w(mins)}",
                            f"{wdw} {t_txt(rng, h)}, {np_}, {dur_w(mins)} long"])
    elif kind == 2:  # block N starting at
        np_, title = pick(rng, blocks)
        mins = pick(rng, [60, 90, 120, 180])
        h = rng.choice([9, 13, 15, 19])
        start, loc = at(day, h, 0), ""
        prompt = pick(rng, [f"block off {dur_w(mins)} for {np_} on {wdw} starting at {t_txt(rng, h)}",
                            f"hold {dur_w(mins)} on {wdw} from {t_txt(rng, h)} for {np_}",
                            f"carve out {dur_w(mins)} for {np_}, {wdw} {t_txt(rng, h)}"])
    else:            # long, with place
        np_, title, loc = pick(rng, [n for n in NEW_EVENTS if n[2]])
        mins = pick(rng, [120, 180, 240])
        h = pick(rng, [h_ for h_ in PREF_HOURS.get(title, [9, 12, 13]) if h_ <= 18] or [12])
        start = at(day, h, 0)
        prompt = pick(rng, [f"{np_} at {loc} {wdw}, {t_txt(rng, h)} for {dur_w(mins)}",
                            f"add {np_} ({loc}) on {wdw}: {t_txt(rng, h)}, {dur_w(mins)}"])
    args = {"title": title, "when_iso": iso(start), "duration_min": mins}
    if loc:
        args["location"] = loc
    return S("exact_args", prompt, now, w, [[Call("add_calendar_event", **args)],
                                            Final(add_final(rng, title, start, mins, loc, now))],
             f"duration_min {mins}" + (", location" if loc else ""))


def _add_reminder_exact(rng, i):
    np_, title = pick(rng, NEW_REMS)
    kind = i % 6
    ctitle = title
    if kind == 0:   # tonight at N
        now, w = mk(rng, hour=rng.choice([9, 11, 13, 15, 17]))
        h = rng.choice([19, 20, 21])
        due = at(now.date(), h)
        when_say = pick(rng, [f"tonight at {t_txt(rng, h)}", f"this evening around {t_txt(rng, h)}", f"later today at {t_txt(rng, h)}"])
        when_fin = f"tonight at {clock(due)}"
    elif kind == 1:  # in N hours / minutes
        now, w = mk(rng, hour=rng.choice([8, 9, 10, 12, 14, 16]))
        if rng.random() < 0.5:
            n = rng.choice([2, 3])
            due = now.replace(second=0, microsecond=0) + D(hours=n)
            when_say = f"in {n} hours"
        else:
            n = rng.choice([20, 40, 45])
            due = now.replace(second=0, microsecond=0) + D(minutes=n)
            when_say = f"in {n} minutes"
        when_fin = f"today at {clock(due)}"
    elif kind == 2:  # weekday at time
        wd = rng.randrange(7)
        rel = ["yesterday", "today"][i // 6 % 2]
        wd, now_wd, hr = target_day(rng, rel)
        now, w = mk(rng, hour=hr, weekday=now_wd)
        day = nxt(now, wd)
        h, m = rng.choice([8, 9, 10, 12, 15, 17, 18]), rng.choice([0, 0, 15, 30, 45])
        if rel == "today":
            h = rng.choice([7, 8, 9])
        due = at(day, h, m)
        when_say = f"{wd_txt(rng, wd)} at {t_txt(rng, h, m)}"
        when_fin = f"{dlabel(day, now)} at {clock(due)}"
    elif kind == 3:  # on the Nth
        now, w = mk(rng, hour=rng.choice([9, 12, 15]))
        n = rng.choice([8, 12, 17, 22, 25])
        month_start = now.date().replace(day=1)
        d0 = month_start.replace(day=n)
        if d0 <= now.date():
            d0 = (month_start + D(days=32)).replace(day=n)
        h, m = rng.choice([(12, 0), (9, 0), (16, 30), (8, 15)])
        due = at(d0, h, m)
        ordinal = f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"
        when_say = f"on the {ordinal} at {t_txt(rng, h, m)}"
        when_fin = f"{d0:%A, %b %-d} at {clock(due)}"
    elif kind == 4:  # all day
        wd, now_wd, hr = target_day(rng, "ahead")
        now, w = mk(rng, weekday=now_wd)
        day = nxt(now, wd)
        prompt = pick(rng, [f"add an all-day reminder for {wd_txt(rng, wd)}: {low1(np_)}",
                            f"i need to {np_} on {wd_txt(rng, wd)}, no set time, just have it on the day",
                            f"all day {wd_txt(rng, wd)} reminder to {np_}"])
        final = pick(rng, [f"Done, you'll have an all-day reminder on {day:%A, %b %-d} to {np_}. There's no timed alert.",
                           f"Set: {np_} is an all-day reminder for {day:%A, %b %-d}, with no alarm time."])
        return S("exact_args", prompt, now, w,
                 [[Call("add_reminder", title=title, when_iso=day.isoformat(), all_day=True)], Final(final)],
                 "all-day reminder: all_day true + YYYY-MM-DD")
    else:            # tomorrow at N
        now, w = mk(rng)
        h, m = rng.choice([(7, 45), (8, 0), (9, 30), (13, 0), (17, 15), (19, 0)])
        due = at(now.date() + D(days=1), h, m)
        when_say = f"tomorrow at {t_txt(rng, h, m)}"
        when_fin = f"tomorrow at {clock(due)}"
    prompt = pick(rng, [f"remind me to {np_} {when_say}",
                        f"nudge me {when_say}: {np_}",
                        f"set a reminder to {np_} {when_say}",
                        f"don't let me forget to {np_} {when_say}",
                        f"ping me {when_say} to {np_}",
                        f"{when_say} remind me to {np_}",
                        f"can u remind me to {np_} {when_say}"])
    final = pick(rng, [f"Okay, I'll remind you {when_fin} to {np_}.",
                       f"Reminder set for {when_fin}: {np_}.",
                       f"Done, you'll get a nudge {when_fin} to {np_}.",
                       f"Got it. I'll remind you {when_fin}: {np_}."])
    return S("exact_args", prompt, now, w, [[Call("add_reminder", title=ctitle, when_iso=iso(due))], Final(final)],
             f"reminder when_iso from the user's words ({kind == 2 and rel or 'relative'})")


def _update_reminder_exact(rng, i):
    ref, title, key = OLD_REMS[i % len(OLD_REMS)]
    kind = i % 5
    if kind == 0:    # new weekday + time
        wd, now_wd, hr = target_day(rng, rel_cycle(i))
        now, w = mk(rng, hour=hr, weekday=now_wd)
        old = at(now.date() + D(days=rng.randint(1, 3)), 9, 0)
        w.reminders.append(Reminder(title, old))
        day = nxt(now, wd)
        h, m = rng.choice([(8, 0), (10, 0), (12, 30), (16, 0), (18, 45)])
        new = at(day, h, m)
        prompt = pick(rng, [f"change {ref} reminder to {wd_txt(rng, wd)} at {t_txt(rng, h, m)}",
                            f"reschedule the {key} reminder for {wd_txt(rng, wd)} {t_txt(rng, h, m)}",
                            f"make the {key} reminder go off {wd_txt(rng, wd)} at {t_txt(rng, h, m)} instead"])
        steps = [[Call("update_reminder", title=key, when_iso=iso(new))]]
        final = pick(rng, [f"Moved the {key} reminder to {day:%A, %b %-d} at {clock(new)}.",
                           f"Done, your {key} reminder is now {day:%A, %b %-d} at {clock(new)}."])
    elif kind == 1:  # day = tomorrow
        now, w = mk(rng, hour=rng.choice([8, 10, 13, 15]))
        old = at(now.date(), min(23, now.hour + 3), 30)
        w.reminders.append(Reminder(title, old))
        new = old + D(days=1)
        prompt = pick(rng, [f"bump {ref} to tomorrow, same time",
                            f"i can't do {ref} today, shift that reminder to tomorrow",
                            f"{ref} reminder: tomorrow instead, keep the time"])
        steps = [[Call("update_reminder", title=key, day="tomorrow")]]
        final = pick(rng, [f"Moved the {key} reminder to tomorrow at {clock(new)}.",
                           f"Done, it's now tomorrow at {clock(new)}: {title.lower()}."])
    elif kind == 2:  # rename + time
        wd, now_wd, hr = target_day(rng, "ahead")
        now, w = mk(rng, weekday=now_wd)
        old = at(now.date() + D(days=2), 9, 0)
        w.reminders.append(Reminder(title, old))
        day = nxt(now, wd)
        h = rng.choice([11, 14, 17])
        new = at(day, h, 0)
        new_title = rng.choice(["Call back about the card", "Drop it off before 5", "Do this first thing"])
        prompt = pick(rng, [f"rename the {key} reminder to \"{new_title}\" and set it for {wd_txt(rng, wd)} {t_txt(rng, h)}",
                            f"retitle {ref} as {new_title}, and move it to {wd_txt(rng, wd)} at {t_txt(rng, h)}"])
        steps = [[Call("update_reminder", title=key, new_title=new_title, when_iso=iso(new))]]
        final = f"Updated: “{new_title}” is now set for {day:%A, %b %-d} at {clock(new)}."
    elif kind == 3:  # rename only: read the time first, then pass it back
        now, w = mk(rng, hour=rng.choice([9, 11, 14]))
        old = at(now.date() + D(days=rng.randint(2, 4)), rng.choice([9, 12, 16]), 0)
        w.reminders.append(Reminder(title, old))
        new_title = rng.choice(["Pick it up before the weekend", "Handle this one early", "Quick errand"])
        prompt = pick(rng, [f"rename the {key} reminder to \"{new_title}\", keep the time",
                            f"change the wording on {ref} to \"{new_title}\" (same time)"])
        steps = [[Call("search_reminders", query=key, scope="all")],
                 [Call("update_reminder", title=key, new_title=new_title, when_iso=iso(old))]]
        final = f"Renamed it to “{new_title}”. It's still set for {old:%A, %b %-d} at {clock(old)}."
    else:            # day = today
        now, w = mk(rng, hour=rng.choice([8, 9, 10, 11, 12]))
        old = at(now.date() + D(days=1), now.hour + 3, 0)
        w.reminders.append(Reminder(title, old))
        new = at(now.date(), now.hour + 3, 0)
        prompt = pick(rng, [f"actually do {ref} today instead of tomorrow",
                            f"pull the {key} reminder up to today, same hour"])
        steps = [[Call("update_reminder", title=key, day="today")]]
        final = f"Done, the {key} reminder is now today at {clock(new)}."
    return S("exact_args", prompt, now, w, steps + [Final(final)],
             "update_reminder: title + when_iso / day / new_title as the schema names them")


def _update_event_exact(rng, i):
    ref, title, key, loc = OLD_EVENTS[i % len(OLD_EVENTS)]
    kind = i % 5
    now, w = mk(rng, hour=rng.choice([8, 9, 10, 12, 14]))
    old = at(now.date() + D(days=rng.randint(1, 4)), rng.choice([10, 13, 15, 17]), rng.choice([0, 30]))
    mins = rng.choice([45, 60, 60, 90])
    add_event(w, title, old, mins, loc)
    if kind == 0:    # new weekday + time
        wd = rng.randrange(7)
        day = nxt(now, wd)
        if day <= old.date() and False:
            pass
        h, m = rng.choice([(9, 0), (11, 30), (14, 0), (16, 0), (18, 30)])
        new = at(day, h, m)
        prompt = pick(rng, [f"reschedule {ref} for {wd_txt(rng, wd)} at {t_txt(rng, h, m)}",
                            f"can {ref} happen {wd_txt(rng, wd)} {t_txt(rng, h, m)} instead?",
                            f"shift {ref} to {wd_txt(rng, wd)} {t_txt(rng, h, m)}"])
        steps = [[Call("update_event", title=key, when_iso=iso(new))]]
        end = new + D(minutes=mins)
        final = pick(rng, [f"Moved {title} to {day:%A, %b %-d} from {span_txt(new, mins)}.",
                           f"Done, {title} is now {dlabel(day, now)} from {span_txt(new, mins)}.",
                           f"{title} now runs {day:%A, %b %-d}, {span_txt(new, mins)}."])
    elif kind == 1:  # new time + new length
        day = old.date()
        h, m = rng.choice([(9, 0), (12, 0), (16, 30)])
        new = at(day, h, m)
        if new == old:
            new = new + D(hours=1)
        newlen = rng.choice([30, 90, 120])
        prompt = pick(rng, [f"{ref} starts at {t_txt(rng, new.hour, new.minute)} now and runs {dur_w(newlen)}",
                            f"move {ref} to {t_txt(rng, new.hour, new.minute)} that day and make it {dur_w(newlen)} long"])
        steps = [[Call("update_event", title=key, when_iso=iso(new), duration_min=newlen)]]
        final = f"Updated: {title} is now {dlabel(day, now)} from {span_txt(new, newlen)}."
        mins = newlen
    elif kind == 2:  # relative shift: read the current start first
        delta = rng.choice([60, 90, 30, -60])
        new = old + D(minutes=delta)
        word = {60: "an hour later", 90: "an hour and a half later", 30: "30 minutes later", -60: "an hour earlier"}[delta]
        prompt = pick(rng, [f"push {ref} {word}", f"{ref}: move it {word}, please"])
        steps = [[Call("get_upcoming", days=14, query=key, calendar_only=True)],
                 [Call("update_event", title=key, when_iso=iso(new))]]
        final = f"Moved {title} {word}, now {dlabel(new.date(), now)} from {span_txt(new, mins)}."
    elif kind == 3:  # two events share the title: pass the current start
        title2 = title
        old2 = old + D(days=rng.randint(2, 5))
        add_event(w, title2, old2, mins, loc)
        h, m = rng.choice([(9, 0), (12, 30), (18, 0)])
        new = at(old2.date(), h, m)
        if new == old2:
            new = new + D(hours=1)
        prompt = pick(rng, [f"move the {old2:%A} {key.lower()} to {t_txt(rng, h, m)}",
                            f"{ref} on {old2:%A} should start at {t_txt(rng, h, m)} instead"])
        steps = [[Call("get_upcoming", days=14, query=key, calendar_only=True)],
                 [Call("update_event", title=key, when_iso=iso(new), current_when_iso=iso(old2))]]
        final = f"Moved the {old2:%A} {title} to {clock(new)}. The other one is unchanged."
    else:            # date + time spoken as a calendar date
        d0 = now.date() + D(days=rng.randint(5, 12))
        h, m = rng.choice([(10, 0), (13, 30), (16, 0)])
        new = at(d0, h, m)
        ordinal = f"{d0.day}{'th' if 11 <= d0.day % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(d0.day % 10, 'th')}"
        prompt = pick(rng, [f"{ref} needs to go to {d0:%B} {ordinal} at {t_txt(rng, h, m)}",
                            f"put {ref} on the {ordinal} at {t_txt(rng, h, m)} rather than where it is"])
        steps = [[Call("update_event", title=key, when_iso=iso(new))]]
        final = f"Moved {title} to {d0:%A, %b %-d} from {span_txt(new, mins)}."
    return S("exact_args", prompt, now, w, steps + [Final(final)], "update_event: title + when_iso (+duration_min / current_when_iso)")


def _bad_retry(rng, i):
    """A wrong parameter name or value gets the registry's real error; the retry uses the schema's names."""
    kind = i % 9
    if kind == 0:    # add_reminder due= -> when_iso=
        np_, title = pick(rng, NEW_REMS)
        now, w = mk(rng, hour=rng.choice([8, 10, 13]))
        due = at(now.date() + D(days=1), 8, 30)
        prompt = f"tomorrow 8:30am, remind me to {np_}"
        steps = [Bad(Call("add_reminder", title=title, due=iso(due))),
                 [Call("add_reminder", title=title, when_iso=iso(due))]]
        final = f"Okay, I'll remind you tomorrow at 8:30 AM to {np_}."
    elif kind == 1:  # add_calendar_event start= -> when_iso=
        np_, title, loc = pick(rng, NEW_EVENTS)
        wd, now_wd, _ = target_day(rng, "ahead")
        now, w = mk(rng, weekday=now_wd)
        day = nxt(now, wd)
        start = at(day, 14, 0)
        prompt = f"add {np_} on {wd_txt(rng, wd)} at 2pm"
        steps = [Bad(Call("add_calendar_event", title=title, start=iso(start))),
                 [Call("add_calendar_event", title=title, when_iso=iso(start))]]
        final = add_final(rng, title, start, 60, "", now)
    elif kind == 2:  # duration as a string
        np_, title, loc = pick(rng, [n for n in NEW_EVENTS if not n[2]] or NEW_EVENTS)
        wd, now_wd, _ = target_day(rng, "ahead")
        now, w = mk(rng, weekday=now_wd)
        day = nxt(now, wd)
        start = at(day, 16, 0)
        prompt = f"{np_} {wd_txt(rng, wd)} at 4pm for 90 minutes"
        steps = [Bad(Call("add_calendar_event", title=title, when_iso=iso(start), duration_min="90")),
                 [Call("add_calendar_event", title=title, when_iso=iso(start), duration_min=90)]]
        final = add_final(rng, title, start, 90, "", now)
    elif kind == 3:  # update_event new_time= -> when_iso=
        ref, title, key, loc = pick(rng, OLD_EVENTS)
        now, w = mk(rng, hour=rng.choice([8, 10, 12]))
        old = at(now.date() + D(days=2), 11, 0)
        add_event(w, title, old, 60, loc)
        new = at(old.date(), 15, 0)
        prompt = f"{ref} should be at 3pm that day"
        steps = [Bad(Call("update_event", title=key, new_time=iso(new))),
                 [Call("update_event", title=key, when_iso=iso(new))]]
        final = f"Moved {title} to {dlabel(new.date(), now)} at 3 PM."
    elif kind == 4:  # update_reminder time= -> when_iso=
        ref, title, key = pick(rng, OLD_REMS)
        now, w = mk(rng, hour=rng.choice([8, 10]))
        w.reminders.append(Reminder(title, at(now.date() + D(days=1), 9, 0)))
        new = at(now.date() + D(days=2), 17, 0)
        prompt = f"set {ref} reminder to {new:%A} at 5pm"
        steps = [Bad(Call("update_reminder", title=key, time=iso(new))),
                 [Call("update_reminder", title=key, when_iso=iso(new))]]
        final = f"Done, the {key} reminder is now {new:%A, %b %-d} at 5 PM."
    elif kind == 5:  # create_note content= -> body=
        now, w = mk(rng)
        topic, body = pick(rng, [("Trip ideas", "- hiking at Silver Falls\n- tacos in the Pearl"),
                                 ("Gift thoughts", "- candle for Maya\n- book for Theo"),
                                 ("Interview prep", "- review hash maps\n- practice system design")])
        prompt = f"make a note called {topic} and put this in it: " + body.replace("\n", ", ").replace("- ", "")
        steps = [Bad(Call("create_note", title=topic, content=body)),
                 [Call("create_note", title=topic, body=body)]]
        final = f"Made a note called “{topic}” with your list."
    elif kind == 6:  # append_note content= -> text=
        now, w = mk(rng)
        w.notes = [Note("Grocery list", "- oat milk\n- eggs\n- spinach", now - D(days=3), "Notes")]
        item = pick(rng, ["bananas", "rice", "dish soap", "coffee beans"])
        prompt = f"{item} too, on the grocery list"
        steps = [Bad(Call("append_note", title="Grocery list", content=f"- {item}")),
                 [Call("append_note", title="Grocery list", text=f"- {item}")]]
        final = f"Added {item} to your grocery list."
    elif kind == 7:  # send_message recipient=/message= -> to=/text=
        now, w = mk(rng)
        who, text, said = pick(rng, [("Maya Chen", "I'll be home by 8", "that you'll be home by 8"),
                                     ("Theo Park", "Library at 3 works for me", "that the library at 3 works"),
                                     ("Carmen Rivera", "Call you tonight after dinner", "you'll call tonight after dinner")])
        prompt = f"let {who.split()[0]} know {said.replace('you', 'i')}" if "you" in said else f"tell {who.split()[0]} {said}"
        steps = [Bad(Call("send_message", recipient=who, message=text)),
                 [Call("send_message", to=who, text=text)]]
        final = f"Sent to {who.split()[0]}: “{text}.”"
    else:            # add_reminder name=/time= -> title=/when_iso=
        np_, title = pick(rng, NEW_REMS)
        now, w = mk(rng, hour=rng.choice([9, 11]))
        due = at(now.date() + D(days=1), 18, 0)
        prompt = f"tomorrow at 6pm: {np_}"
        steps = [Bad(Call("add_reminder", name=title, time=iso(due))),
                 [Call("add_reminder", title=title, when_iso=iso(due))]]
        final = f"Reminder set for tomorrow at 6 PM: {np_}."
    return S("exact_args", prompt, now, w, steps + [Final(final)], "wrong parameter name/value -> real error -> retry with the schema's names")


def fam_exact_args(rng):
    _RNG[0] = rng
    out = []
    out += [_add_event_weekday(rng, i) for i in range(10)]
    out += [_add_event_duration(rng, i) for i in range(6)]
    out += [_add_reminder_exact(rng, i) for i in range(10)]
    out += [_update_reminder_exact(rng, i) for i in range(5)]
    out += [_update_event_exact(rng, i) for i in range(6)]
    out += [_bad_retry(rng, i) for i in range(9)]
    return out


# =============================================================================
# 6. right_write_tool
# =============================================================================
def _move_vs_new(rng, i):
    if i % 2 == 0:   # move an existing event
        ref, title, key, loc = OLD_EVENTS[(i // 2) % len(OLD_EVENTS)]
        now, w = mk(rng, hour=rng.choice([8, 10, 12, 15]))
        old = at(now.date() + D(days=rng.randint(1, 4)), rng.choice([11, 14, 16]), 0)
        add_event(w, title, old, 60, loc)
        new = at(old.date() + D(days=rng.choice([0, 0, 1])), rng.choice([9, 13, 17, 18]), rng.choice([0, 30]))
        if new == old:
            new += D(hours=2)
        prompt = pick(rng, [f"{ref} got pushed to {t_txt(rng, new.hour, new.minute)}" + (f" {new:%A}" if new.date() != old.date() else "") + ", update it",
                            f"i need {ref} at {t_txt(rng, new.hour, new.minute)}" + (f" on {new:%A}" if new.date() != old.date() else "") + " instead of when it's set",
                            f"the time for {ref} changed, make it {t_txt(rng, new.hour, new.minute)}" + (f" {new:%A}" if new.date() != old.date() else ""),
                            f"{ref}: {t_txt(rng, new.hour, new.minute)}" + (f" {new:%A}" if new.date() != old.date() else "") + " now, not the old time"])
        final = pick(rng, [f"Moved {title} to {dlabel(new.date(), now)} at {clock(new)}.",
                           f"Done, {title} now starts {dlabel(new.date(), now)} at {clock(new)}."])
        return S("right_write_tool", prompt, now, w, [[Call("update_event", title=key, when_iso=iso(new))], Final(final)],
                 "existing event with a new time -> update_event, not a second event")
    np_, title, loc = NEW_EVENTS[(i // 2) % len(NEW_EVENTS)]
    wd = rng.randrange(7)
    now, w = mk(rng, weekday=(wd - rng.choice([1, 2, 3])) % 7)
    day = nxt(now, wd)
    h = rng.choice([10, 12, 15, 18])
    start = at(day, h, 0)
    prompt = pick(rng, [f"new thing: {np_}, {wd_txt(rng, wd)} at {t_txt(rng, h)}",
                        f"i'm adding {np_} on {wd_txt(rng, wd)} at {t_txt(rng, h)}",
                        f"got invited to {np_} {wd_txt(rng, wd)} {t_txt(rng, h)}, mark the time",
                        f"mark off {wd_txt(rng, wd)} {t_txt(rng, h)} for {np_}"])
    return S("right_write_tool", prompt, now, w, [[Call("add_calendar_event", title=title, when_iso=iso(start))],
                                                  Final(add_final(rng, title, start, 60, "", now))],
             "brand-new event -> add_calendar_event")


def _reminder_vs_event(rng, i):
    mode = i % 5
    wd = rng.randrange(7)
    now, w = mk(rng, weekday=(wd - rng.choice([2, 3, 4])) % 7)
    day = nxt(now, wd)
    if mode == 0:    # reminder
        np_, title = pick(rng, NEW_REMS)
        h = rng.choice([8, 12, 16, 20])
        due = at(day, h)
        prompt = pick(rng, [f"give me a heads-up {wd_txt(rng, wd)} at {t_txt(rng, h)} to {np_}",
                            f"alert me to {np_} on {wd_txt(rng, wd)}, {t_txt(rng, h)}",
                            f"make sure i remember to {np_} {wd_txt(rng, wd)} {t_txt(rng, h)}"])
        final = f"I'll remind you {dlabel(day, now)} at {clock(due)} to {np_}."
        return S("right_write_tool", prompt, now, w, [[Call("add_reminder", title=title, when_iso=iso(due))], Final(final)],
                 "'remind/alert me' -> add_reminder")
    if mode == 1:    # calendar event, appointment wording
        np_, title, loc = pick(rng, NEW_EVENTS)
        h = rng.choice([9, 11, 14, 16])
        start = at(day, h)
        prompt = pick(rng, [f"i have {np_} {wd_txt(rng, wd)} at {t_txt(rng, h)}, so block that time on the schedule",
                            f"reserve {wd_txt(rng, wd)} {t_txt(rng, h)} for {np_}, it's an appointment",
                            f"{np_} is {wd_txt(rng, wd)} at {t_txt(rng, h)}, add it as an event"])
        return S("right_write_tool", prompt, now, w, [[Call("add_calendar_event", title=title, when_iso=iso(start))],
                                                      Final(add_final(rng, title, start, 60, "", now))],
                 "appointment on the schedule -> add_calendar_event, not a reminder")
    if mode in (2, 3):  # same chore, both phrasings
        np_, title = pick(rng, NEW_REMS)
        h = rng.choice([9, 14, 17])
        when = at(day, h)
        if mode == 2:
            prompt = pick(rng, [f"{wd_txt(rng, wd)} {t_txt(rng, h)}: remind me, {np_}", f"tell me to {np_} on {wd_txt(rng, wd)} at {t_txt(rng, h)}"])
            return S("right_write_tool", prompt, now, w, [[Call("add_reminder", title=title, when_iso=iso(when))],
                                                          Final(f"Reminder set: {np_}, {dlabel(day, now)} at {clock(when)}.")],
                     "task to be nudged about -> add_reminder")
        prompt = pick(rng, [f"put {np_} on the schedule {wd_txt(rng, wd)} at {t_txt(rng, h)} as a calendar entry",
                            f"make {np_} a real calendar event {wd_txt(rng, wd)} at {t_txt(rng, h)}"])
        return S("right_write_tool", prompt, now, w, [[Call("add_calendar_event", title=title, when_iso=iso(when))],
                                                      Final(add_final(rng, title, when, 60, "", now))],
                 "asked for a calendar entry -> add_calendar_event")
    # both: an event plus a reminder an hour before
    np_, title, loc = pick(rng, NEW_EVENTS)
    h = rng.choice([14, 16, 18])
    start = at(day, h)
    rem = start - D(hours=1)
    prompt = pick(rng, [f"add {np_} on {wd_txt(rng, wd)} at {t_txt(rng, h)} and also remind me an hour before",
                        f"{np_} {wd_txt(rng, wd)} {t_txt(rng, h)}: schedule it, and nudge me an hour ahead"])
    final = (f"Added {title} for {dlabel(day, now)} at {clock(start)}, and I'll remind you at {clock(rem)}.")
    return S("right_write_tool", prompt, now, w,
             [[Call("add_calendar_event", title=title, when_iso=iso(start)),
               Call("add_reminder", title=f"Get ready for {low1(title)}", when_iso=iso(rem))], Final(final)],
             "event AND reminder in one step: each to its own tool")


CONTACT_MAIL = [("Carmen Rivera", "Carmen", "carmen.rivera@example.com"), ("Maya Chen", "Maya", "maya.chen@example.com"),
                ("Theo Park", "Theo", "theo.park@example.com"), ("Prof. Okafor", "Professor Okafor", "aokafor@linden.edu")]


def _draft_vs_send(rng, i):
    now, w = mk(rng)
    who, first, addr = CONTACT_MAIL[i % len(CONTACT_MAIL)]
    mode = i % 8
    if mode in (0, 1, 2):   # draft email, never sent
        subj, body, what = pick(rng, [
            ("Lake photos", f"Hi {first},\n\nCould you send me the photos from Saturday when you get a chance?\n\nThanks,\nSam", "asking for the photos"),
            ("Quick question about project 2", f"Hi {first},\n\nI wanted to ask whether the extension also covers the writeup.\n\nThanks,\nSam", "asking if the extension covers the writeup"),
            ("Study session", f"Hi {first},\n\nAre you free to study at the library on Sunday afternoon?\n\nSam", "asking about a study session Sunday"),
        ])
        prompt = pick(rng, [f"write an email to {first} {what}, i want to read it over first",
                            f"can you draft something to {first} {what}? don't send it",
                            f"put together an email for {first} {what} and leave it as a draft"])
        final = pick(rng, [f"I drafted it to {first} in Mail. Nothing is sent yet, so you can read it and hit send when you're ready.",
                           f"Your draft to {first} is open in Mail and hasn't been sent."])
        return S("right_write_tool", prompt, now, w, [[Call("draft_email", to=who, subject=subj, body=body)], Final(final)],
                 "'draft/read it first' -> draft_email, never send_email", cards=[])
    if mode in (3, 4):      # send email behind a card
        subj, body, what = pick(rng, [
            ("Count me in", f"Hi {first},\n\nCount me in for Saturday.\n\nSam", "i'm in for saturday"),
            ("Running a bit behind", f"Hi {first},\n\nI'm running about 15 minutes behind, sorry.\n\nSam", "i'm running 15 minutes behind"),
            ("Thanks", f"Hi {first},\n\nThanks for the notes, they helped a lot.\n\nSam", "thanks for the notes"),
        ])
        prompt = pick(rng, [f"shoot {first} an email saying {what}",
                            f"email {first} now: {what}",
                            f"send {first} a quick email, {what}"])
        final = pick(rng, [f"Sent your email to {first}.", f"Emailed {first}: “{subj}”.", f"Done, the email to {first} is on its way."])
        return S("right_write_tool", prompt, now, w, [[Call("send_email", to=who, subject=subj, body=body)], Final(final)],
                 "'send/shoot an email' -> send_email behind a card, not a draft")
    text, said = pick(rng, [("I'll be home by 8", "i'll be home by 8"), ("Library at 3 works for me", "the library at 3 works"),
                            ("Call you after dinner", "i'll call after dinner"), ("Running 10 minutes behind", "i'm running ten minutes behind"),
                            ("Got the tickets!", "i got the tickets")])
    who, first, addr = pick(rng, [c for c in CONTACT_MAIL if c[0] != "Prof. Okafor"])
    prompt = pick(rng, [f"tell {first} {said}", f"let {first} know {said}", f"message {first}: {said}", f"{first} needs to hear that {said}, send it"])
    final = pick(rng, [f"Sent to {first}: “{text}.”", f"Told {first}: “{text}.”", f"Done, {first} has your message."])
    return S("right_write_tool", prompt, now, w, [[Call("send_message", to=who, text=text)], Final(final)],
             "'tell/let X know' -> send_message behind a card")


def _note_create_append(rng, i):
    now, w = mk(rng)
    w.notes = [Note("Grocery list", "- oat milk\n- eggs\n- spinach", now - D(days=2), "Notes"),
               Note("Gift ideas", "Mom: scarf or a cookbook\nCarmen: concert tickets", now - D(days=9), "Notes"),
               Note("Books to read", "- The Pragmatic Programmer\n- Educated", now - D(days=20), "Notes")]
    mode = i % 6
    if mode in (0, 1, 2):
        title, text, ref, what = pick(rng, [
            ("Grocery list", "- bananas", "the grocery list", "bananas"),
            ("Grocery list", "- dish soap", "my grocery note", "dish soap"),
            ("Gift ideas", "Theo: climbing gym pass", "the gift ideas note", "Theo: climbing gym pass"),
            ("Books to read", "- Project Hail Mary", "my reading list", "Project Hail Mary"),
            ("Grocery list", "- rice", "grocery list", "rice"),
        ])
        prompt = pick(rng, [f"tack {what} onto {ref}", f"add {what} to {ref}", f"{what}, on {ref} please", f"append {what} to {ref}"])
        final = pick(rng, [f"Added {what} to “{title}”.", f"Done, {what} is on “{title}” now.", f"{cap(what)} is on “{title}” now."])
        return S("right_write_tool", prompt, now, w, [[Call("append_note", title=title, text=text)], Final(final)],
                 "add to an existing note -> append_note, not a new note", cards=[])
    topic, body, said = pick(rng, [
        ("Trip ideas", "- hiking at Silver Falls\n- tacos in the Pearl", "hiking at silver falls and tacos in the pearl"),
        ("Interview questions", "- tell me about a hard bug\n- design a rate limiter", "tell me about a hard bug, design a rate limiter"),
        ("Move-out checklist", "- return keys\n- clean oven\n- forward mail", "return keys, clean oven, forward mail"),
    ])
    prompt = pick(rng, [f"start a new note called {topic}: {said}", f"new note \"{topic}\" with {said}",
                        f"i want a fresh note titled {topic}, contents: {said}"])
    final = pick(rng, [f"Made a new note called “{topic}”.", f"Created “{topic}” in Notes with what you listed."])
    return S("right_write_tool", prompt, now, w, [[Call("create_note", title=topic, body=body)], Final(final)],
             "start a new note -> create_note", cards=[])


def _complete_vs_delete(rng, i):
    # (title as stored, user's reference, the key the model passes)
    cands = [("Pay phone bill", "the phone bill", "phone bill"), ("Return library books", "the library books", "library books"),
             ("Renew bus pass", "the bus pass", "bus pass"), ("Pick up prescription", "the prescription", "prescription"),
             ("Print the ethics essay", "the ethics essay printing", "ethics essay"), ("Buy groceries", "groceries", "groceries"),
             ("Finish linear algebra problem set 6", "problem set 6", "problem set 6"), ("Call Mom", "calling mom", "call mom")]
    title, ref, key = cands[i % len(cands)]
    now, w = mk(rng, hour=rng.choice([9, 12, 16]), n_rem=0, overdue=0, undated=0)
    w.reminders = [Reminder(title, at(now.date() + D(days=rng.randint(0, 3)), rng.choice([13, 17, 19]), 0))]
    w.reminders += [Reminder("Water the cactus", at(now.date() + D(days=2), 9, 0))]
    if i % 2 == 0:   # done -> complete_reminder
        prompt = pick(rng, [f"finished {ref}, cross it off", f"{ref} is done now", f"all done with {ref}, tick it off the list",
                            f"i took care of {ref}"])
        final = pick(rng, [f"Marked “{title}” done.", f"Nice, “{title}” is checked off."])
        return S("right_write_tool", prompt, now, w, [[Call("complete_reminder", title=key)], Final(final)],
                 "finished -> complete_reminder (keeps the record), not a delete")
    prompt = pick(rng, [f"scrap the {key} reminder, i don't need it anymore", f"delete the reminder about {ref}",
                        f"i'm not doing {ref} after all, remove that reminder"])
    final = pick(rng, [f"Deleted the {key} reminder.", f"Done, the reminder about {ref.replace('the ', '')} is gone."])
    return S("right_write_tool", prompt, now, w, [[Call("clear_reminders", scope="all", query=key)], Final(final)],
             "no longer wanted -> clear_reminders (delete behind a card), not complete_reminder")


def fam_right_write_tool(rng):
    _RNG[0] = rng
    out = []
    out += [_move_vs_new(rng, i) for i in range(12)]
    out += [_reminder_vs_event(rng, i) for i in range(10)]
    out += [_draft_vs_send(rng, i) for i in range(8)]
    out += [_note_create_append(rng, i) for i in range(6)]
    out += [_complete_vs_delete(rng, i) for i in range(6)]
    return out


# =============================================================================
# 7. relay_receipt
# =============================================================================
def _relay_add_event(rng, i):
    np_, title, loc = NEW_EVENTS[i % len(NEW_EVENTS)]
    wd = rng.randrange(7)
    now, w = mk(rng, weekday=(wd - rng.choice([1, 2, 3, 4])) % 7)
    day = nxt(now, wd)
    h, m = ehour(rng, title), rng.choice([0, 0, 30])
    mins = rng.choice([60, 60, 45, 90, 120, 30])
    start = at(day, h, m)
    use_loc = bool(loc) and i % 2 == 0
    place = f" at {loc}" if use_loc else ""
    dur = "" if mins == 60 else f" for {dur_w(mins)}"
    prompt = pick(rng, [f"book {np_}{place} {wd_txt(rng, wd)} {t_txt(rng, h, m)}{dur}",
                        f"i want {np_}{place} on {wd_txt(rng, wd)}, {t_txt(rng, h, m)}{dur}",
                        f"{wd_txt(rng, wd)} at {t_txt(rng, h, m)}{dur}: {np_}{place}, add that"])
    args = {"title": title, "when_iso": iso(start)}
    if mins != 60:
        args["duration_min"] = mins
    if use_loc:
        args["location"] = loc
    final = add_final(rng, title, start, mins, loc if use_loc else "", now)
    return S("relay_receipt", prompt, now, w, [[Call("add_calendar_event", **args)], Final(final)],
             "final restates the receipt: title, day, start-end, place")


def _relay_reminder(rng, i):
    np_, title = NEW_REMS[(i + 4) % len(NEW_REMS)]
    now, w = mk(rng, hour=rng.choice([8, 10, 13]))
    day = now.date() + D(days=rng.choice([1, 1, 2]))
    h, m = rng.choice([(8, 0), (9, 30), (17, 0), (20, 15)])
    due = at(day, h, m)
    when = f"{dlabel(day, now)} at {clock(due)}"
    wsay = f"tomorrow at {t_txt(rng, h, m)}" if day == now.date() + D(days=1) else f"{day:%A} at {t_txt(rng, h, m)}"
    prompt = pick(rng, [f"remind me to {np_} {wsay}", f"{wsay}, a reminder to {np_}"])
    if i % 2 == 1:   # Apple Reminders not reachable: the receipt says Wisp only
        w.unavailable = {"reminders"}
        final = (f"Set for {when}: {np_}. It's saved in Wisp only, since Wisp doesn't have access to Apple "
                 f"Reminders yet.")
        note = "receipt says Wisp only (Apple Reminders not changed): the final says so"
    else:
        final = pick(rng, [f"Reminder set for {when}: {np_}.", f"I'll remind you {when} to {np_}."])
        note = "reminder receipt relayed"
    return S("relay_receipt", prompt, now, w, [[Call("add_reminder", title=title, when_iso=iso(due))], Final(final)], note)


def _relay_move(rng, i):
    ref, title, key, loc = OLD_EVENTS[(i + 3) % len(OLD_EVENTS)]
    now, w = mk(rng, hour=rng.choice([8, 10, 13]))
    old = at(now.date() + D(days=rng.randint(1, 3)), rng.choice([10, 14, 16]), 0)
    mins = rng.choice([60, 90])
    variant = i % 6
    kw = {}
    if variant == 3:
        kw["invitees"] = 2
    if variant == 4:
        kw["series"] = "weekly"
    add_event(w, title, old, mins, loc, **kw)
    if variant == 4:
        add_event(w, title, old + D(days=7), mins, loc, series="weekly")
    new = at(old.date() + D(days=rng.choice([0, 1, 2])), rng.choice([9, 12, 17]), rng.choice([0, 30]))
    if new == old:
        new += D(hours=3)
    args = {"title": key, "when_iso": iso(new)}
    newlen = mins
    if variant == 2:
        newlen = rng.choice([30, 120])
        args["duration_min"] = newlen
    prompt = pick(rng, [f"move {ref} to {t_txt(rng, new.hour, new.minute)}" + (f" on {new:%A}" if new.date() != old.date() else ""),
                        f"{ref} needs to start at {t_txt(rng, new.hour, new.minute)}" + (f" {new:%A}" if new.date() != old.date() else "") + (f" and run {dur_w(newlen)}" if variant == 2 else ""),
                        f"i'd rather do {ref} at {t_txt(rng, new.hour, new.minute)}" + (f" {new:%A}" if new.date() != old.date() else "")])
    tail = ""
    if variant == 3:
        tail = " The 2 invitees were notified of the new time."
    if variant == 4:
        tail = " Only this one moved, not the rest of the series."
    final = f"Moved {title} to {new:%A, %b %-d} from {span_txt(new, newlen)}.{tail}"
    return S("relay_receipt", prompt, now, w, [[Call("update_event", **args)], Final(final)],
             "move receipt: new start-end, invitees notified / only this occurrence")


def _relay_send(rng, i):
    now, w = mk(rng)
    if i % 2 == 0:
        who, first, addr = CONTACT_MAIL[i % 3]
        text, said = pick(rng, [("Landed safe, heading to baggage claim", "i landed safe and i'm heading to baggage claim"),
                                ("Can you grab the mail today?", "can you grab the mail today"),
                                ("Dinner at 7 works!", "dinner at 7 works")])
        prompt = pick(rng, [f"text {first} that {said}".replace("that can you", "to ask if they can").replace("that dinner", "that dinner"),
                            f"send {first} a text saying {said}"])
        if said.startswith("can you"):
            prompt = f"text {first}: {said}?"
        phone = w.contacts[who][0]
        final = pick(rng, [f"Sent to {first} at {phone}: “{text}.”", f"Your text to {first} went through: “{text}.”"])
        return S("relay_receipt", prompt, now, w, [[Call("send_message", to=who, text=text)], Final(final)],
                 "send receipt names the recipient (and the resolved number)")
    who, first, addr = pick(rng, [c for c in CONTACT_MAIL if c[0] != "Maya Chen"])
    subj, body, said = pick(rng, [("Meeting Thursday", f"Hi {first},\n\nCan we meet Thursday afternoon to go over the project?\n\nSam", "asking to meet Thursday afternoon about the project"),
                                  ("Receipt", f"Hi {first},\n\nI sent the $30 for the gift just now.\n\nSam", "that i sent the $30 for the gift")])
    prompt = pick(rng, [f"email {first} {said}", f"send an email to {first}, {said}"])
    final = pick(rng, [f"Emailed {first} at {addr} with the subject “{subj}”.", f"Sent. The email to {first} ({addr}) is on its way: “{subj}”."])
    return S("relay_receipt", prompt, now, w, [[Call("send_email", to=who, subject=subj, body=body)], Final(final)],
             "email receipt names the address the contact resolved to")


def _relay_move_refused(rng, i):
    """update_event's pre-card check refuses; no card, the model reports plainly that nothing moved."""
    ref, title, key, loc = OLD_EVENTS[(i + 5) % len(OLD_EVENTS)]
    now, w = mk(rng, hour=rng.choice([8, 10, 13]))
    old = at(now.date() + D(days=rng.randint(1, 3)), rng.choice([10, 14, 16]), 0)
    new = old + D(hours=rng.choice([2, 3]))
    variant = i % 6
    kw = {0: {"writable": False}, 1: {"organizer": False, "invitees": 3}, 2: {"invitees": None}}.get(variant, {})
    if variant == 3:   # no such event
        add_event(w, title, old, 60, loc)
        prompt = pick(rng, [f"move my pottery thing to {t_txt(rng, new.hour)}", f"the rock climbing session should be at {t_txt(rng, new.hour)}"])
        args = {"title": "Pottery" if "pottery" in prompt else "Rock climbing", "when_iso": iso(new)}
        final = pick(rng, ["I don't see an upcoming event with that name, so nothing was moved. Which event did you mean?",
                           "Nothing upcoming matches that, so I haven't changed anything. Can you tell me the event's exact name?"])
        return S("relay_receipt", prompt, now, w, [[Call("update_event", **args)], Final(final)],
                 "no match: nothing moved, said plainly (precard refusal)", cards=[])
    if variant == 4:   # two events match
        add_event(w, title, old, 60, loc)
        add_event(w, title, old + D(days=rng.randint(2, 4)), 60, loc)
        prompt = f"move {ref} to {t_txt(rng, new.hour)}"
        other = w.events[-1] if w.events[-1].title == title else None
        final = pick(rng, [f"There are two matching events, {old:%A} and {sorted([e.start for e in w.events if e.title == title])[-1]:%A}, so I haven't moved either. Which one should I change?"])
        return S("relay_receipt", prompt, now, w, [[Call("update_event", title=key, when_iso=iso(new))], Final(final)],
                 "several match: asks which, nothing moved (precard refusal)", cards=[])
    if variant == 5:   # no-op: already starts then
        add_event(w, title, old, 60, loc)
        prompt = pick(rng, [f"move {ref} to {t_txt(rng, old.hour)} on {old:%A}", f"set {ref} for {t_txt(rng, old.hour)} {old:%A}"])
        final = f"{title} already starts at {clock(old)} on {old:%A}, so nothing needed to move."
        return S("relay_receipt", prompt, now, w, [[Call("update_event", title=key, when_iso=iso(old))], Final(final)],
                 "already at that time: nothing moved (precard refusal)", cards=[])
    add_event(w, title, old, 60, loc, **kw)
    prompt = pick(rng, [f"move {ref} to {t_txt(rng, new.hour)}" + (f" on {new:%A}" if new.date() != old.date() else ""),
                        f"{ref} should be at {t_txt(rng, new.hour)} instead"])
    if variant == 0:
        final = pick(rng, [f"I couldn't move {title}. It's on a read-only calendar, so Wisp can't change it. You can move it in Calendar.",
                           f"{title} is on a read-only calendar, so it wasn't moved. You'd need to change it in Calendar."])
    elif variant == 1:
        final = pick(rng, [f"I didn't move {title}, because only its organizer can change the time. You could ask them or propose a new time in Calendar.",
                           f"Nothing moved: someone else organizes {title}, so only they can change it."])
    else:
        final = pick(rng, [f"I couldn't move {title} yet. Calendar hasn't finished syncing who's invited, so nothing changed. Try again shortly or move it in Calendar.",
                           f"Nothing was moved. Calendar is still syncing the invitee list for {title}, so I held off."])
    return S("relay_receipt", prompt, now, w, [[Call("update_event", title=key, when_iso=iso(new))], Final(final)],
             "refused move reported as NOT done (precard refusal)", cards=[])


def _relay_reminder_refused(rng, i):
    now, w = mk(rng, hour=rng.choice([9, 12, 15]), n_rem=0, overdue=0, undated=0)
    w.reminders = [Reminder("Pay phone bill", at(now.date() + D(days=2), 9, 0)),
                   Reminder("Pay parking ticket", at(now.date() + D(days=3), 9, 0))]
    mode = i % 3
    if mode == 0:
        prompt = pick(rng, ["mark the oil change as done", "tick off the oil change reminder"])
        steps = [[Call("complete_reminder", title="oil change")]]
        final = "I couldn't find a reminder about the oil change, so nothing was marked done."
    elif mode == 1:
        prompt = pick(rng, ["i paid it, check off the pay reminder", "mark the pay one complete"])
        steps = [[Call("search_reminders", query="pay", scope="all")]]
        final = "Two reminders start with pay: “Pay phone bill” and “Pay parking ticket”. Which one did you finish? Nothing is marked done yet."
    else:
        prompt = pick(rng, ["make the oil change reminder 5pm", "oil change reminder should be 5pm today"])
        t = at(now.date(), 17) if now.hour < 16 else at(now.date() + D(days=1), 17)
        steps = [[Call("update_reminder", title="oil change", when_iso=iso(t))]]
        final = "I couldn't change it, because there's no active reminder about the oil change. Nothing was updated."
    return S("relay_receipt", prompt, now, w, steps + [Final(final)], "no/ambiguous match reported plainly, nothing changed")


def _relay_denied(rng, i):
    """The user taps Deny: the host ends the turn, so the script stops at the write call."""
    now, w = mk(rng)
    mode = i % 5
    if mode == 0:
        np_, title, loc = pick(rng, NEW_EVENTS)
        wd = rng.randrange(7)
        now, w = mk(rng, weekday=(wd - 2) % 7)
        start = at(nxt(now, wd), 15)
        prompt = f"put {np_} on {wd_txt(rng, wd)} at 3pm"
        steps = [[Call("add_calendar_event", title=title, when_iso=iso(start))]]
    elif mode == 1:
        prompt = "text maya i'm on my way"
        steps = [[Call("send_message", to="Maya Chen", text="I'm on my way")]]
    elif mode == 2:
        prompt = "email carmen that i'll take the bus tomorrow"
        steps = [[Call("send_email", to="Carmen Rivera", subject="Bus tomorrow",
                       body="Hi Carmen,\n\nI'll take the bus tomorrow.\n\nSam")]]
    elif mode == 3:
        ref, title, key, loc = pick(rng, OLD_EVENTS)
        now, w = mk(rng, hour=9)
        old = at(now.date() + D(days=1), 11, 0)
        add_event(w, title, old, 60, loc)
        prompt = f"move {ref} to 4pm tomorrow"
        steps = [[Call("update_event", title=key, when_iso=iso(at(old.date(), 16)))]]
    else:
        np_, title, loc = pick(rng, NEW_EVENTS)
        now, w = mk(rng)
        start = at(now.date() + D(days=1), 18)
        prompt = f"add {np_} tomorrow at 6pm to the schedule"
        steps = [[Call("add_calendar_event", title=title, when_iso=iso(start))]]
    return S("relay_receipt", prompt, now, w, steps, "card denied: the turn ends at the call (host text), no final", deny=0)


def _relay_retry(rng, i):
    mode = i % 4
    if mode == 0:    # duration as text on add_calendar_event
        np_, title, loc = pick(rng, NEW_EVENTS)
        wd = rng.randrange(7)
        now, w = mk(rng, weekday=(wd - 2) % 7)
        start = at(nxt(now, wd), 12)
        prompt = f"{np_} {wd_txt(rng, wd)} noon for 2 hours"
        steps = [Bad(Call("add_calendar_event", title=title, when_iso=iso(start), duration_min="2 hours")),
                 [Call("add_calendar_event", title=title, when_iso=iso(start), duration_min=120)]]
        final = add_final(rng, title, start, 120, "", now)
    elif mode == 1:  # update_event: wrong param name
        ref, title, key, loc = pick(rng, OLD_EVENTS)
        now, w = mk(rng, hour=9)
        old = at(now.date() + D(days=2), 10, 0)
        add_event(w, title, old, 60, loc)
        new = at(old.date(), 13)
        prompt = f"{ref} moves to 1pm"
        steps = [Bad(Call("update_event", title=key, start_time=iso(new))), [Call("update_event", title=key, when_iso=iso(new))]]
        final = f"Moved {title} to {old:%A, %b %-d} from {span_txt(new, 60)}."
    elif mode == 2:  # create_note: wrong name
        now, w = mk(rng)
        topic, body = pick(rng, [("Packing list for Denver", "- jacket\n- charger\n- notebook"),
                                 ("Questions for Okafor", "- extension scope\n- midterm format")])
        prompt = f"make a note \"{topic}\": " + body.replace("- ", "").replace("\n", ", ")
        steps = [Bad(Call("create_note", name=topic, text=body)), [Call("create_note", title=topic, body=body)]]
        final = f"Made the note “{topic}”. It may not show up in a note search until tomorrow's sync."
    else:            # send_email: to= / message= wrong
        now, w = mk(rng)
        steps = [Bad(Call("send_email", recipient="Theo Park", subject="Study spot", message="Library at 3?")),
                 [Call("send_email", to="Theo Park", subject="Study spot", body="Hi Theo,\n\nLibrary at 3?\n\nSam")]]
        prompt = "email theo asking if the library at 3 works"
        final = "Sent your email to Theo (theo.park@example.com): “Study spot”."
    return S("relay_receipt", prompt, now, w, steps + [Final(final)], "tool error -> one retry with the schema's names -> relay the retry's receipt")


def _relay_unknown_recipient(rng, i):
    now, w = mk(rng)
    name, text = pick(rng, [("Priya", "I'm outside"), ("Uncle Dev", "Happy birthday!"), ("Ellis", "Running late")])
    prompt = pick(rng, [f"text {name.lower()} {text.lower().rstrip('!')}", f"tell {name} {text.lower().rstrip('!')}"])
    final = pick(rng, [f"I don't have a contact named {name}, so nothing was sent. What number should I use?",
                       f"Nothing sent: there's no {name} in your contacts. Give me a number or email and I'll send it."])
    return S("relay_receipt", prompt, now, w, [[Call("lookup_contact", name=name)], Final(final)],
             "unknown recipient: look up, report not sent, ask for the number", cards=[])


def fam_relay_receipt(rng):
    _RNG[0] = rng
    out = []
    out += [_relay_add_event(rng, i) for i in range(7)]
    out += [_relay_reminder(rng, i) for i in range(4)]
    out += [_relay_move(rng, i) for i in range(6)]
    out += [_relay_send(rng, i) for i in range(4)]
    out += [_relay_move_refused(rng, i) for i in range(6)]
    out += [_relay_reminder_refused(rng, i) for i in range(3)]
    out += [_relay_denied(rng, i) for i in range(5)]
    out += [_relay_retry(rng, i) for i in range(4)]
    out += [_relay_unknown_recipient(rng, i) for i in range(2)]
    return out


FAMILIES_R2 = {"exact_args": fam_exact_args, "right_write_tool": fam_right_write_tool,
               "relay_receipt": fam_relay_receipt}
WRITE_ALLOW_R2 = {
    "exact_args": frozenset({"add_calendar_event", "update_event", "add_reminder", "update_reminder",
                             "create_note", "append_note", "send_message"}),
    "right_write_tool": frozenset({"add_calendar_event", "update_event", "add_reminder", "draft_email", "send_email",
                                   "send_message", "create_note", "append_note", "complete_reminder",
                                   "clear_reminders"}),
    "relay_receipt": frozenset({"add_calendar_event", "update_event", "add_reminder", "update_reminder",
                                "complete_reminder", "send_message", "send_email", "create_note"}),
}
