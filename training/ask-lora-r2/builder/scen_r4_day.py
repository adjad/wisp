"""Round-4 family ``day_answers_r4``: calendar and day questions answered with the facts.

r3 lost 18 points on the toolbench "day" category and 5 on "calendar". The failures were
answers that glanced at a result and dropped what it held: "Nothing scheduled tonight"
beside a listed 6 PM reminder, "Yes, an hour is open" with no time, an hour guessed on a
day that holds a trip. Every turn here is a READ, and every final carries each fact the
question asked for, in the form the tool printed it:

- free / busy: find_free_time ranges verbatim (a "yes" always names the ranges), the
  conflicting items with their times when nothing is open, and get_upcoming for the days a
  holiday, a no-classes marker or a multi-day trip decides. A read-only holiday or a
  "no classes" day counts as free; the user's own all-day trip makes its days busy. No end
  time is stated that the result does not print (get_upcoming shows starts only).
- tonight / what's left today: today's list against the frozen clock; earlier items are not
  "left", a 6 PM reminder IS tonight, "nothing left" only when nothing is ahead.
- a named day ("friday", "the 14th", "next tuesday", "nov 9"): that date only, every item with
  its time and title, a count with its noun, an empty day said plainly.
- the whole day (get_my_day_plan): timed items, overdue, who is waiting, undated to-dos.
- the past ("last week", "yesterday"): get_past_events, past tense.

Worlds are built per scenario (titles, hours, weekdays, lists of 0-6 items), and every
final is computed from the world's own tool output, so it never states what the result
does not show.
"""
from __future__ import annotations

import datetime as dt
import re

import gen
import harness as H
from gen import D
from harness import Call, Final
from replies import join, plural
from scen import Scenario, dedupe_mail, setup
from scen_r2_args import WDN, at
from scen_r3_safe import _fw
from world import Event, Msg, Reminder, short_clock

FAMILY = "day_answers_r4"
WRITE_ALLOW_R4 = {FAMILY: frozenset()}

_FIRST: dict = {}
_CAP = [5]


def fin(rng, *opts):
    """One of ``opts``, preferring a first word this family has not used _CAP times."""
    ok = [o for o in opts if _FIRST.get(_fw(o), 0) < _CAP[0]]
    o = rng.choice(ok or list(opts))
    _FIRST[_fw(o)] = _FIRST.get(_fw(o), 0) + 1
    return o


T = short_clock                       # 10 AM / 1:30 PM
USED_PROMPTS: set = set()


def pickp(rng, stems, **kw):
    """A prompt from ``stems`` (formatted with ``kw``) not used yet in this family."""
    return uniq(rng, *[x.format(**kw) for x in stems])


def uniq(rng, *opts):
    """A prompt not used yet in this family."""
    ok = [o for o in opts if o not in USED_PROMPTS] or list(opts)
    o = rng.choice(ok)
    USED_PROMPTS.add(o)
    return o


# ------------------------------------------------------------------ pools
# (title, minutes, location, plausible start hours)
POOL = [
    ("Study group with Aisha", 120, "Hollis Library, room 3B", [10, 13, 15, 18]),
    ("Coffee with Theo", 60, "Brewed Awakening", [8, 9, 10, 15, 16]),
    ("Dentist appointment", 60, "Pine Street Dental", [9, 10, 11, 14, 15]),
    ("Office hours — Prof. Okafor", 45, "Kerr Hall 412", [11, 13, 14, 15]),
    ("Internship interview — Fernbrook Labs", 45, "Zoom", [9, 10, 11, 14, 15, 16]),
    ("Dinner with Carmen", 90, "Golden Lotus", [18, 19]),
    ("Haircut", 45, "Clipper Joe's", [9, 10, 12, 16, 17]),
    ("Project sync with Jordan", 60, "Kerr Hall 301", [11, 13, 15, 16]),
    ("Movie night with Maya", 150, "Home", [19, 20]),
    ("Advising appointment", 30, "Dean's Office, Mercer Hall", [9, 10, 11, 14, 15]),
    ("Volunteer shift — food bank", 180, "Eastside Food Bank", [9, 13]),
    ("Yoga class", 60, "Rec Center Studio B", [7, 8, 12, 17, 18]),
    ("Linear algebra review session", 90, "Avery Science 110", [12, 16, 17]),
    ("MATH 241 Linear Algebra", 75, "Avery Science 110", [9, 11, 14]),
    ("CS 162 Operating Systems — Lecture", 50, "Kerr Hall 204", [9, 10, 13]),
    ("PHIL 110 Intro to Ethics", 80, "Mercer Hall 12", [10, 11, 14]),
    ("Library help desk shift", 240, "Hollis Library", [16, 17]),
    ("CS 210 Data Structures Lab", 60, "Kerr Hall 301", [13, 14, 15]),
    ("Running club long run", 90, "Riverside Track", [7, 8]),
    ("Climbing with Theo", 90, "Cascade Climbing Gym", [17, 18, 19]),
    ("Piano lesson", 45, "", [15, 16, 17]),
    ("Pottery class", 120, "Clay Studio on 5th", [10, 13, 18]),
    ("Vet visit", 45, "Cedar Animal Clinic", [9, 10, 14]),
    ("Eye exam", 45, "Lakeview Optical", [9, 10, 11, 14]),
    ("Call with advisor", 30, "", [9, 10, 11, 14, 15]),
    ("Resume workshop", 60, "Career Center", [10, 11, 14, 16]),
    ("Tutoring session", 60, "Hollis Library", [11, 14, 16, 17]),
    ("Roommate meeting", 30, "", [12, 19, 20]),
    ("Trivia night", 120, "The Rusty Anchor", [19, 20]),
    ("Board game night", 150, "Maya's place", [18, 19]),
    ("Career fair", 180, "Student Union Ballroom", [10, 13]),
    ("Bike tune-up", 45, "Spoke & Chain", [9, 10, 13, 15]),
    ("Lunch with Hana", 60, "Student Union", [12]),
    ("Grad school info session", 60, "Mercer Hall 12", [12, 16]),
    ("Oil change", 45, "Quick Lube on Burnside", [8, 9, 13]),
    ("Group project meeting", 90, "Kerr Hall 118", [13, 15, 17]),
]
CHAIN = [p for p in POOL if p[1] in (30, 60, 90)]       # neat durations for back-to-back stretches
DENSE = [p for p in POOL if p[1] >= 90]                  # long events for packed days

REM_TITLES = ["Pay phone bill", "Return library books", "Buy groceries", "Renew bus pass", "Call Mom",
              "Pick up prescription", "Water the plants", "Laundry", "Submit the financial aid form",
              "Print the ethics essay", "Schedule a haircut", "Venmo Maya for utilities", "Take out the recycling",
              "Mail the package", "Charge the camera", "Order the textbook", "Submit the time sheet",
              "Check the lab results", "Pack the gym bag", "Refill the water filter", "Renew my parking permit",
              "Email Hana about the regrade", "Drop off the donation box", "Return the router"]
PEOPLE_WAIT = [("Maya Chen", "Can you grab milk on your way home?"), ("Theo Park", "Still on for coffee?"),
               ("Mom", "Call me when you get a chance"), ("Jordan Ellis", "Are you done with the analysis part?"),
               ("Aisha Karim", "Can you bring your linear algebra notes?"), ("Carmen Rivera", "Ordering it Sunday!"),
               ("Ben Ortiz", "Long run Saturday 8am at Riverside, you in?")]

HOLIDAYS = [  # (date, title) read-only calendar entries on their real 2026-27 dates
    (dt.date(2026, 10, 12), "Indigenous Peoples' Day"), (dt.date(2026, 11, 11), "Veterans Day"),
    (dt.date(2026, 11, 26), "Thanksgiving Day"), (dt.date(2026, 12, 25), "Christmas Day"),
    (dt.date(2027, 1, 1), "New Year's Day"), (dt.date(2027, 1, 18), "Martin Luther King Jr. Day"),
    (dt.date(2027, 2, 15), "Presidents' Day"), (dt.date(2027, 4, 2), "Good Friday"),
]
NO_CLASS = ["No classes — reading day", "No classes — faculty development day", "Campus closed — no classes",
            "No classes — Founders' Day", "No classes — advising day", "No classes — university holiday"]
TRIPS = [("Family trip", "Seattle"), ("Ski weekend with Theo", "Mt. Hood"), ("Conference trip", "Denver"),
         ("Road trip with Maya", ""), ("Visit to Carmen", "Eugene"), ("Hiking trip", "Silver Falls")]


# ------------------------------------------------------------------ world helpers
def mk(rng, weekday=None, hour=None):
    """A Sam world with an empty calendar, reminders and texts, so each scenario holds exactly what it states."""
    now, w = setup(rng, weekday=weekday, hour=hour, extra=0, exams=False)
    w.events, w.past, w.reminders, w.messages = [], [], [], []
    return now, w


def mk_on(rng, now):
    """Like mk, at an exact clock (holiday worlds)."""
    H.set_clock(now)
    w = dedupe_mail(gen.make_world(now, rng, extra=0, exams=False))
    w.events, w.past, w.reminders, w.messages = [], [], [], []
    return w


def span_day(w, period):
    s, _e, _l = w._span(period, True)
    return s.date()


def add_ev(w, title, start, mins=60, loc="", **kw):
    ev = Event(title, start, mins, loc, kw.pop("kind", "event"), **kw)
    w.events.append(ev)
    w.events.sort(key=lambda e: e.start)
    return ev


def add_rem(w, title, due):
    r = Reminder(title, due)
    w.reminders.append(r)
    w.reminders.sort(key=lambda r: (r.due is None, r.due))
    return r


def put_events(rng, w, day, n, *, lo=8, hi=21, min_start=None, max_end=None):
    """n distinct pool events on ``day`` that don't overlap; returns them sorted."""
    pool = POOL[:]
    rng.shuffle(pool)
    got = []
    for title, mins, loc, hours in pool:
        if len(got) >= n:
            break
        h = rng.choice([x for x in hours if lo <= x <= hi] or [hours[0]])
        m = rng.choice([0, 0, 30]) if mins != 150 else 0
        start = at(day, h, m)
        end = start + D(minutes=mins)
        if min_start and start < min_start or max_end and end > max_end:
            continue
        if end > at(day, hi) + D(hours=1):
            continue
        if any(start < e2 and end > s2 for s2, e2 in [(g.start, g.end) for g in got]):
            continue
        if any(g.title == title for g in w.events):
            continue
        got.append(add_ev(w, title, start, mins, loc))
    return sorted(got, key=lambda e: e.start)


def chain(rng, w, day, start_h, end_h):
    """Back-to-back events from start_h until end_h (no gap left in between)."""
    cursor = at(day, start_h)
    limit = at(day, end_h)
    used = set()
    out = []
    while cursor < limit and len(out) < 6:
        pool = [p for p in CHAIN if p[0] not in used]
        near = [p for p in pool if any(abs(h - cursor.hour) <= 2 for h in p[3])] or pool
        if not near:
            break
        title, mins, loc, _ = rng.choice(near)
        used.add(title)
        out.append(add_ev(w, title, cursor, mins, loc))
        cursor += D(minutes=mins)
    return out


def free_gaps(w, period, **kw):
    """(result text, ['9:00 AM–11:00 AM', ...] of the first day line, header) from the world's find_free_time."""
    res = w.t_find_free_time(period=period, **kw)
    gaps = []
    for ln in res.splitlines():
        if ": free " in ln:
            gaps = ln.split(": free ", 1)[1].split(", ")
            break
    return res, gaps


def win(rng, res, p=0.75):
    """A sentence naming the window and slot length find_free_time printed ('30-minute candidate slots, 09:00–18:00'),
    or '' (the result's own figures, in the clock form the answer uses)."""
    m = re.search(r"(\d+)-minute candidate slots, (\d\d):(\d\d)–(\d\d):(\d\d)", res)
    if not m or rng.random() > p:
        return ""
    ck = lambda h, mi: f"{int(h) % 12 or 12}{':' + mi if mi != '00' else ''} {'AM' if int(h) < 12 else 'PM'}"
    a, b, n = ck(m.group(2), m.group(3)), ck(m.group(4), m.group(5)), m.group(1)
    return " " + rng.choice([f"I checked {a} to {b} for stretches of {n} minutes or more.",
                             f"That covers {a} to {b}, counting only stretches of {n} minutes or more."])


def rng_txt(g):
    """'9:00 AM–11:00 AM' -> '9:00 AM to 11:00 AM'."""
    return g.replace("–", " to ")


def ranges(gaps):
    return join(rng_txt(g) for g in gaps)


def gap_bounds(g, day):
    a, b = g.split("–")
    p = lambda s: dt.datetime.combine(day, dt.datetime.strptime(s.strip(), "%I:%M %p").time())
    return p(a), p(b)


def wd_name(d):
    return d.strftime("%A")


def dword(d, now):
    """today / tomorrow / Friday."""
    delta = (d - now.date()).days
    return "today" if delta == 0 else "tomorrow" if delta == 1 else wd_name(d)


def on_word(d, now):
    delta = (d - now.date()).days
    return "today" if delta == 0 else "tomorrow" if delta == 1 else "on " + wd_name(d)


def long_date(d):
    return d.strftime("%A, %B %-d")


def ev_item(e):
    return (e.start, e.title, "allday" if e.all_day else "event", e.location)


def rem_item(r):
    return (r.due, r.title, "reminder", "")


def phrase(it, loc=False):
    t, title, kind, where = it
    if kind == "allday":
        return f"{title}, all day"
    if kind == "reminder":
        return f"the reminder “{title}” at {T(t)}"
    s = f"{title} at {T(t)}"
    if loc and where and where != "Home":
        s += f" ({where})"
    return s


def bullet(it, loc=False):
    """loc=True only when the tool result prints the place (get_upcoming does; get_my_day_plan does not)."""
    t, title, kind, where = it
    if kind == "allday":
        return f"All day: {title}"
    place = f" ({where})" if loc and where and where != "Home" and kind == "event" else ""
    return f"{T(t)}: {title}" + place + (" (reminder)" if kind == "reminder" else "")


def counts(items):
    ne = sum(i[2] != "reminder" for i in items)
    nr = len(items) - ne
    bits = []
    if ne:
        bits.append(plural(ne, "event"))
    if nr:
        bits.append(plural(nr, "reminder"))
    return " and ".join(bits)


def agenda(items, head, tail_colon=None, loc=False):
    """Prose for 1-3 items, a counted bullet list for 4 or more. ``head`` e.g. 'On Friday you have'.
    loc=True names each event's place (only for a get_upcoming read, which prints it)."""
    items = sorted(items, key=lambda i: (i[2] != "allday", i[0]))
    if len(items) >= 4:
        lead = tail_colon or (head + f" {counts(items)}")
        return lead.rstrip(":") + ":\n" + "\n".join("- " + bullet(i, loc) for i in items)
    return head + " " + join(phrase(i, loc=loc or len(items) == 1) for i in items) + "."


def day_items(w, day, *, reminders=True, after=None):
    items = [ev_item(e) for e in w.events if e.start.date() == day or (e.all_day and e.start.date() <= day < (e.start + D(minutes=e.minutes)).date())]
    if reminders:
        items += [rem_item(r) for r in w.reminders if r.due and not r.done and r.due.date() == day]
    if after is not None:
        items = [i for i in items if i[0] >= after]
    return items


def sc(prompt, now, w, steps, notes):
    return Scenario(FAMILY, prompt, now, w, steps, notes)


def tx(rng, h, m=0):
    """A spoken clock for a prompt: 3pm, 3:30 pm, noon."""
    if h == 12 and m == 0 and rng.random() < 0.4:
        return "noon"
    h12 = h % 12 or 12
    ap = "am" if h < 12 else "pm"
    mm = f":{m:02d}" if m else ""
    return rng.choice([f"{h12}{mm}{ap}", f"{h12}{mm} {ap}"])


def wspell(rng, d):
    n = wd_name(d).lower()
    r = rng.random()
    if r < 0.12:
        return n[:3] if n[:3] != "sun" else "sun"
    if r < 0.2:
        return {"thursday": "thurs", "tuesday": "tues", "wednesday": "wednesday"}.get(n, n)
    return n.capitalize() if r > 0.88 else n


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def date_period(rng, d, now):
    """(how the user writes the day, the period the model passes). Both resolve to ``d``."""
    k = rng.randrange(4)
    if k == 0:
        s = f"{d:%b} {d.day}".lower()
        return s, s
    if k == 1:
        s = f"{d:%B} {ordinal(d.day)}"
        return s, s.lower()
    if k == 2:
        s = f"the {ordinal(d.day)}"
        return s, s
    s = f"{d:%b} {d.day}".lower()
    return s, s


PARTS = {"morning": (9, 12), "afternoon": (12, 18), "evening": (18, 22)}


def pick_day(rng, now, lo=2, hi=6):
    return now.date() + D(days=rng.randint(lo, hi))


# ================================================================== A. free / busy
def period_for(w, day):
    """The period words a model would pass for ``day``: the bare weekday when it resolves there, else 'next <weekday>',
    else the ISO date."""
    wd = WDN[day.weekday()]
    for cand in (wd, f"next {wd}"):
        try:
            if span_day(w, cand) == day:
                return cand
        except Exception:       # noqa: BLE001
            pass
    return day.isoformat()


def gap_n(n):
    return "a gap" if n == 1 else f"{n} gaps"


def frm(gaps):
    return "from " + ranges(gaps)


def dense_day(rng, w, day, minutes):
    """A day 9-17 packed so that no gap reaches ``minutes`` (gaps of 0/15/30 minutes between long events)."""
    pool = DENSE[:]
    rng.shuffle(pool)
    cursor = at(day, 9) + D(minutes=rng.choice([0, 15, 30]))
    while cursor < at(day, 17, 15) and pool:
        near = [p for p in pool if any(abs(h - cursor.hour) <= 3 for h in p[3])] or pool
        p = rng.choice(near)
        pool.remove(p)
        add_ev(w, p[0], cursor, p[1], p[2])
        cursor += D(minutes=p[1] + rng.choice([0, 15, 30]))
        if len([e for e in w.events if e.start.date() == day]) >= 6:
            break


YN_PART = ["am i free {wd} {part}", "is there any gap in my {wd} {part}", "do i have time {wd} {part}?",
           "will i have a break {wd} {part} or is it packed", "could i fit something in {wd} {part}",
           "got any room {wd} {part}", "is my {wd} {part} clear", "{part} on {wd}, am i clear"]
WHAT_PART = ["what's open {wd} {part}", "which bits of {wd} {part} are free", "show me the free stretches {wd} {part}"]


def free_part_of_day(rng, out):
    """'am i free thursday afternoon': find_free_time with day_start/day_end, the ranges in the answer."""
    cases = [("afternoon", "free", "yn"), ("morning", "gaps", "yn"), ("evening", "free", "what"), ("afternoon", "gaps", "yn"),
             ("morning", "free", "yn"), ("afternoon", "booked", "yn"), ("evening", "gaps", "what"), ("morning", "booked", "yn"),
             ("afternoon", "gaps", "what"), ("evening", "free", "yn")]
    for k, (part, mode, form) in enumerate(cases):
        lo, hi = PARTS[part]
        for _try in range(80):
            now, w = mk(rng, hour=rng.choice([7, 9, 10, 11, 13, 16, 20]))
            day = pick_day(rng, now)
            if mode == "booked":
                chain(rng, w, day, lo, hi)
            elif mode == "gaps":
                put_events(rng, w, day, rng.choice([1, 2]), lo=max(lo, 8), hi=hi - 1, min_start=at(day, lo), max_end=at(day, hi))
            elif rng.random() < 0.5 and lo > 9:
                put_events(rng, w, day, 1, lo=7, hi=21, max_end=at(day, lo))
            period = period_for(w, day)
            res, gaps = free_gaps(w, period, day_start=lo, day_end=hi)
            if (mode == "booked") == (not gaps) and len(gaps) <= 3 and (mode != "gaps" or len(gaps) >= 1):
                break
        else:
            continue
        label = wd_name(day)
        prompt = pickp(rng, WHAT_PART if form == "what" else YN_PART, wd=wspell(rng, day), part=part)
        call = Call("find_free_time", period=period, day_start=lo, day_end=hi)
        if not gaps:
            evs = sorted(day_items(w, day, reminders=False))
            if len(evs) >= 4:
                reply = (f"No, {label} {part} has no open gap of 30 minutes or more. You have:\n"
                         + "\n".join("- " + bullet(i) for i in evs))
            else:
                body = join(phrase(i) for i in evs)
                reply = fin(rng, f"No, there's no 30-minute gap {label} {part}. You have {body}.",
                            f"{label} {part} is booked solid: {body}.",
                            f"Not really. {label} {part} holds {body}, with no open stretch between.")
            steps = [[call], [Call("get_upcoming", period=period, calendar_only=True)], Final(reply)]
            note = "part of day with no gap: find_free_time, then read the day and name the conflicts with their times"
        else:
            whole = len(gaps) == 1 and gap_bounds(gaps[0], day) == (at(day, lo), at(day, hi))
            if form == "what":
                reply = (fin(rng, f"{label} {part} is open {frm(gaps)}.", f"You're free {frm(gaps)} {label} {part}.") if len(gaps) == 1
                         else fin(rng, f"{label} {part} has {len(gaps)} gaps: {ranges(gaps)}.",
                                  f"You're free {frm(gaps)} {label} {part}."))
            elif whole:
                reply = fin(rng, f"Yes, {label} {part} is wide open, {frm(gaps)}.",
                            f"{label} {part} is clear, {frm(gaps)}.",
                            f"Nothing is in the way {label} {part}; you're free {frm(gaps)}.")
            else:
                reply = fin(rng, f"Partly. You're free {frm(gaps)} {label} {part}.",
                            f"Yes, there's room {label} {part}, {frm(gaps)}.",
                            f"You have {gap_n(len(gaps))} {label} {part}: {ranges(gaps)}.")
            reply += win(rng, res)
            steps = [[call], Final(reply)]
            note = "part of day: find_free_time with day_start/day_end, the actual free ranges in the answer"
        out.append(sc(prompt, now, w, steps, note))


YN_HOUR = ["got {dur} free on {d}?", "is there {dur} open on {d}", "can i squeeze in something for {dur} on {d}",
           "do i have a window of {dur} on {d}", "could i fit {dur} on {d}", "is there {dur} anywhere on {d} or am i swamped"]


def free_hour_on_date(rng, out):
    """'do i have an hour on nov 3': find_free_time(period=date, minutes=N); a yes always names the time."""
    mins_cycle = [(60, "an hour", True), (60, "an hour", True), (45, "45 minutes", True), (90, "an hour and a half", True),
                  (60, "an hour", True), (30, "half an hour", True), (60, "an hour", False), (90, "90 minutes", False),
                  (120, "two hours", True), (60, "an hour", False)]
    for k, (mins, dur, yes) in enumerate(mins_cycle):
        for _try in range(80):
            now, w = mk(rng, hour=rng.choice([7, 8, 11, 14, 17, 21]))
            day = pick_day(rng, now, 3, 18)
            if yes:
                put_events(rng, w, day, rng.choice([1, 2, 2, 3]), lo=9, hi=17)
            else:
                dense_day(rng, w, day, mins)
            said, period = date_period(rng, day, now)
            try:
                if span_day(w, period) != day:
                    continue
            except Exception:       # noqa: BLE001
                continue
            res, gaps = free_gaps(w, period, minutes=mins)
            if yes and gaps and len(gaps) <= 3 or not yes and not gaps:
                break
        else:
            continue
        prompt = pickp(rng, YN_HOUR, dur=dur, d=said)
        label = long_date(day)
        call = Call("find_free_time", period=period, minutes=mins)
        if gaps:
            reply = fin(rng,
                        f"Yes. On {label} you're free {frm(gaps)}, which fits {dur}.",
                        f"You do. {label} is open {frm(gaps)}.",
                        f"Yes, {label} has room, {frm(gaps)}.")
            reply += win(rng, res)
            steps = [[call], Final(reply)]
            note = "a duration on a date: find_free_time(minutes=N); a yes states the actual ranges"
        else:
            evs = sorted(day_items(w, day, reminders=False))
            if len(evs) >= 4:
                reply = f"No, nothing that long is open on {label}. You have {len(evs)} events:\n" + "\n".join("- " + bullet(i) for i in evs)
            else:
                body = join(phrase(i) for i in evs)
                reply = fin(rng, f"No, nothing that long is open on {label}. You have {body}.",
                            f"Not on {label}: there's no gap of {dur}. The day holds {body}.")
            steps = [[call], [Call("get_upcoming", period=period, calendar_only=True)], Final(reply)]
            note = "no gap long enough: find_free_time, then read the day and name the items with their times"
        out.append(sc(prompt, now, w, steps, note))


def free_when(rng, out):
    """'when am i free tomorrow': the actual ranges from find_free_time, also for the rest of today."""
    plans = [("tomorrow", 1), ("tomorrow", 2), ("today", 0), ("tomorrow", 0), ("weekday", 2), ("today", 1), ("weekday", 1)]
    stems = {
        "tomorrow": ["what's open tomorrow", "which parts of tomorrow are free", "free windows tomorrow?", "tomorrow, where are my gaps"],
        "today": ["any free time left today", "what's open for the rest of today", "when could i fit something in today", "where are my gaps for the rest of today"],
        "weekday": ["what does {wd} have open", "which stretches of {wd} are free", "where are the gaps on {wd}", "{wd}, when am i around"],
    }
    for k, (kind, n_ev) in enumerate(plans):
        for _try in range(80):
            now, w = mk(rng, hour=rng.choice([8, 9, 10, 12, 13]) if kind == "today" else rng.choice([8, 12, 15, 18, 21]))
            day = now.date() if kind == "today" else now.date() + D(days=1) if kind == "tomorrow" else pick_day(rng, now, 2, 5)
            lo_t = max(now + D(minutes=40), at(day, 9)) if kind == "today" else at(day, 9)
            if n_ev:
                put_events(rng, w, day, n_ev, lo=max(lo_t.hour + 1, 9) if kind == "today" else 9, hi=17,
                           min_start=lo_t + D(minutes=30) if kind == "today" else None)
            period = "today" if kind == "today" else "tomorrow" if kind == "tomorrow" else period_for(w, day)
            res, gaps = free_gaps(w, period)
            if gaps and len(gaps) <= 3:
                break
        else:
            continue
        prompt = pickp(rng, stems[kind], wd=wspell(rng, day))
        cap = "Today" if kind == "today" else "Tomorrow" if kind == "tomorrow" else wd_name(day)
        onw = on_word(day, now)
        if len(gaps) == 1:
            reply = fin(rng, f"{cap} is open {frm(gaps)}.", f"You're free {onw} {frm(gaps)}.")
        else:
            reply = fin(rng, f"{cap} you're free {frm(gaps)}.", f"The open stretches {onw} are {ranges(gaps)}.",
                        f"You have {plural(len(gaps), 'gap')} {onw}: {ranges(gaps)}.")
        reply += win(rng, res)
        out.append(sc(prompt, now, w, [[Call("find_free_time", period=period)], Final(reply)],
                      "when am I free: find_free_time(period), the free ranges listed in the answer"))


FREE_AT = [("free", "can i do {t} {wd}"), ("free", "am i free at {t} {wd}?"), ("free", "does {t} on {wd} work for me"),
           ("free", "{wd} at {t}: free or not"), ("free", "i'd like to book {dur} at {t} on {wd}, is that open"),
           ("free", "could i meet someone {wd} at {t}"), ("busy", "will i be busy at {t} {wd}"),
           ("busy", "is {t} {wd} taken"), ("busy", "do i have something at {t} on {wd}")]


def free_at_time(rng, out):
    """'am i free at 3 on friday': is that time inside a free range? say which range, or the ones that are open."""
    plans = [(15, 0, 30, True, 0), (11, 0, 30, True, 6), (14, 30, 60, True, 4), (16, 0, 30, False, 1), (10, 0, 30, False, 7),
             (13, 0, 60, True, 3), (17, 0, 30, True, 8), (12, 0, 60, False, 5), (9, 30, 30, False, 2), (15, 30, 30, True, 6)]
    for k, (h, m, mins, want, si) in enumerate(plans):
        pol, stem = FREE_AT[si]
        for _try in range(160):
            now, w = mk(rng, hour=rng.choice([8, 10, 13, 18]))
            day = pick_day(rng, now, 2, 6)
            put_events(rng, w, day, rng.choice([1, 2, 3]), lo=9, hi=17)
            period = period_for(w, day)
            res, gaps = free_gaps(w, period, minutes=mins)
            if not gaps or len(gaps) > 3:
                continue
            t0 = at(day, h, m)
            inside = [g for g in gaps if gap_bounds(g, day)[0] <= t0 and t0 + D(minutes=mins) <= gap_bounds(g, day)[1]]
            if bool(inside) == want:
                break
        else:
            continue
        dur = "an hour" if mins == 60 else "30 minutes"
        prompt = uniq(rng, stem.format(t=tx(rng, h, m), wd=wspell(rng, day), dur=dur))
        label = wd_name(day)
        call = Call("find_free_time", period=period, minutes=mins) if mins != 30 else Call("find_free_time", period=period)
        if pol == "free" and want:
            reply = fin(rng, f"Yes, {T(t0)} works. {label} is open {frm(inside[:1])}.",
                        f"You can: {label} is free {frm(inside[:1])}, which covers {T(t0)}.")
        elif pol == "free":
            reply = fin(rng, f"No, {T(t0)} on {label} isn't open. You're free {frm(gaps)}.",
                        f"{T(t0)} is taken {label}. The open time is {frm(gaps)}.",
                        f"Not at {T(t0)}. Your open time {label} is {ranges(gaps)}.")
        elif want:
            reply = fin(rng, f"No, {T(t0)} is clear {label}. You're free {frm(inside[:1])}.",
                        f"Nothing at {T(t0)} on {label}; that time sits in a free stretch, {rng_txt(inside[0])}.")
        else:
            reply = fin(rng, f"Yes, {T(t0)} on {label} is taken. You're free {frm(gaps)}.",
                        f"{T(t0)} is spoken for {label}. The open stretches are {ranges(gaps)}.")
        reply += win(rng, res)
        out.append(sc(prompt, now, w, [[call], Final(reply)],
                      "a specific time: find_free_time, then say whether that time falls in a free range, with the ranges"))


HOL_STEMS = {"free": ["free on {h} or not", "is {h} open for plans", "am i around on {h}"],
             "any": ["do i have anything on {h}", "got plans on {h}?", "anything going on {h}"]}
HOL_NAME = {"Martin Luther King Jr. Day": "mlk day", "Indigenous Peoples' Day": "indigenous peoples day"}


def free_holiday(rng, out):
    """A read-only holiday or a no-classes marker never blocks a day: free, with the marker named."""
    plans = [  # (kind, holiday index, stem kind, extra event, ranges too)
        ("hol", 1, "free", 0, 0), ("hol", 2, "any", 1, 0), ("hol", 3, "free", 0, 0), ("hol", 5, "any", 0, 0),
        ("hol", 6, "free", 1, 0), ("hol", 4, "any", 1, 1), ("hol", 0, "free", 1, 1),
        ("noc", 0, "class", 0, 0), ("noc", 1, "class", 0, 0), ("noc", 2, "class", 0, 0), ("noc", 3, "off", 1, 0),
    ]
    for k, (kind, idx, form, n_ev, both) in enumerate(plans):
        if kind == "hol":
            hd, name = HOLIDAYS[idx]
            now = dt.datetime.combine(hd - D(days=rng.randint(2, 9)), dt.time(rng.choice([8, 10, 14, 19]), rng.randint(0, 59)))
            title = name
        else:
            now = dt.datetime.combine(dt.date(2026, 9, 21) + D(days=rng.randint(10, 190)), dt.time(rng.choice([9, 12, 17]), rng.randint(0, 59)))
            hd = now.date() + D(days=rng.randint(2, 6))
            while hd.weekday() > 4:
                hd += D(days=1)
            title = NO_CLASS[(k + idx) % len(NO_CLASS)]
        w = mk_on(rng, now)
        add_ev(w, title, dt.datetime.combine(hd, dt.time()), 24 * 60, "", all_day=True, writable=False,
               context="US Holidays" if kind == "hol" else "")
        extra = put_events(rng, w, hd, 1, lo=10, hi=15) if n_ev else []
        period = hd.isoformat() if kind == "hol" else period_for(w, hd)
        label = long_date(hd)
        if kind == "hol":
            nm = HOL_NAME.get(name, name.lower())
            prompt = pickp(rng, HOL_STEMS[form], h=nm)
        elif form == "class":
            prompt = pickp(rng, ["do i have class {wd}", "is there school {wd}", "do i have lectures {wd}"], wd=wspell(rng, hd))
        else:
            prompt = uniq(rng, f"am i booked {wspell(rng, hd)} or is it a day off")
        # get_upcoming hides subscribed holiday calendars unless include_holidays=true, which the person's naming
        # the holiday asks for; a no-classes marker is on the school calendar and shows without it
        steps = [[Call("get_upcoming", period=period, include_holidays=True) if kind == "hol" else Call("get_upcoming", period=period)]]
        if extra:
            e = extra[0]
            if both:
                res, gaps = free_gaps(w, period)
                steps.append([Call("find_free_time", period=period)])
                reply = fin(rng,
                            f"{title} is on the calendar all day, and the only real plan is {e.title} at {T(e.start)}. Around that you're free {frm(gaps)}.",
                            f"You're free {frm(gaps)} on {label}. {title} doesn't take any time, so {e.title} at {T(e.start)} is the only commitment.")
            elif form == "free":
                reply = fin(rng, f"Mostly open. {title} is on all day, and the one real plan is {e.title} at {T(e.start)}.",
                            f"You're nearly free on {label}: {title} is all day, and {e.title} at {T(e.start)} is the only thing with a time.")
            elif form == "any":
                reply = fin(rng, f"Just {e.title} at {T(e.start)}. {title} is on the calendar all day, but it doesn't take any time.",
                            f"One real thing on {label}: {e.title} at {T(e.start)}, plus {title} all day.")
            else:
                reply = fin(rng, f"A day off, apart from {e.title} at {T(e.start)}. {title} is on the calendar all day.",
                            f"{title} is all day, and the only item with a time is {e.title} at {T(e.start)}.")
        elif kind == "hol":
            if form == "free":
                reply = fin(rng, f"Yes, you're free. {title} is the only entry on {label}, an all-day holiday with nothing else scheduled.",
                            f"You're clear on {label}: only {title} is listed, and it's a holiday.")
            else:
                reply = fin(rng, f"Nothing besides {title}, which is all day. {label} has no other events.",
                            f"No plans. {title} is the only entry on {label}.")
        else:
            reply = fin(rng, f"No class on {label}: it's marked “{title}”, and nothing else is scheduled.",
                        f"Nothing on {label} apart from “{title}”, so there's no class.")
        steps.append(Final(reply))
        out.append(sc(prompt, now, w, steps,
                      "a read-only holiday / no-classes marker counts as free; the marker and any real item are named"))


def free_trip(rng, out):
    """The user's own multi-day trip makes each of its days busy; later days read 'continued into'."""
    plans = [(0, 3), (1, 3), (2, 3), (0, 2), (1, 2), (0, 4), (1, 4)]
    stems = ["i'm thinking {wd}, am i tied up", "can i meet someone {wd} for lunch", "do i have any free time {wd}",
             "is {wd} open for a call", "could i book something {wd} afternoon", "what's {wd} like, any room"]
    for k, (offset, nights) in enumerate(plans):
        title, where = TRIPS[k % len(TRIPS)]
        for _try in range(40):
            now, w = mk(rng, hour=rng.choice([9, 13, 18]))
            start = now.date() + D(days=rng.randint(1, 3))
            day = start + D(days=offset)
            if (day - now.date()).days <= 6:
                break
        else:
            continue
        add_ev(w, title, dt.datetime.combine(start, dt.time()), nights * 24 * 60, where, all_day=True)
        period = period_for(w, day)
        prompt = pickp(rng, stems, wd=wspell(rng, day))
        label = wd_name(day)
        place = f" in {where}" if where else ""
        if offset == 0:
            reply = fin(rng, f"{label} is taken: {title}{place} is on your calendar all day.",
                        f"{label} is blocked by {title}{place}, an all-day entry.",
                        f"You're away {label}. {title}{place} runs all day.")
        else:
            reply = fin(rng, f"{label} is taken. {title}{place} is still going and continues into that day.",
                        f"You're away: {title}{place} continues into {label}, so the day is busy.",
                        f"{label} is busy because {title}{place} carries on through it.")
        out.append(sc(prompt, now, w, [[Call("get_upcoming", period=period)], Final(reply)],
                      "the user's own multi-day trip blocks its days (all-day entry, 'continued into' on later days)"))


# ================================================================== B. tonight / what's left today
def make_today(rng, now, w, n_ahead, n_back, *, rem_ahead=0, rem_back=0, tonight=False):
    """Items ahead of the clock (events and reminders) and some already behind it."""
    day = now.date()
    cur = now.replace(second=0, microsecond=0)
    # earlier events: end well before now
    back = []
    pool = POOL[:]
    rng.shuffle(pool)
    for title, mins, loc, hours in pool:
        if len(back) >= n_back:
            break
        hs = [h for h in hours if at(day, h) + D(minutes=mins + 15) <= cur and h >= 7]
        if not hs:
            continue
        back.append(add_ev(w, title, at(day, rng.choice(hs), rng.choice([0, 30])), mins, loc))
    used = {e.title for e in w.events}
    ahead = []
    lo_h = 17 if tonight else 0
    for title, mins, loc, hours in pool:
        if len(ahead) >= n_ahead:
            break
        if title in used:
            continue
        hs = [h for h in hours if at(day, h) >= cur + D(minutes=25) and h >= lo_h and h <= 21]
        if not hs:
            continue
        h = rng.choice(hs)
        start = at(day, h, rng.choice([0, 30]))
        end = start + D(minutes=mins)
        if start < cur + D(minutes=25) or any(start < e2.end and end > e2.start for e2 in w.events):
            continue
        ahead.append(add_ev(w, title, start, mins, loc))
    rems = rng.sample(REM_TITLES, rem_ahead + rem_back)
    r_ahead, r_back = [], []
    for t in rems[:rem_ahead]:
        cands = [h for h in (12, 14, 16, 17, 18, 19, 20, 21) if at(day, h) > cur + D(minutes=20) and (not tonight or h >= 17)]
        if cands:
            r_ahead.append(add_rem(w, t, at(day, rng.choice(cands), rng.choice([0, 0, 30]))))
    for t in rems[rem_ahead:]:
        cands = [h for h in (7, 8, 9, 10, 12, 14) if at(day, h, 30) < cur]
        if cands:
            r_back.append(add_rem(w, t, at(day, rng.choice(cands), 0)))
    return back, ahead, r_ahead, r_back


def left_today(rng, out):
    """'what's left today': today's list against the clock. Earlier items are not left; reminders count."""
    plans = [  # (hour, ahead events, earlier events, ahead reminders, earlier reminders)
        (9, 3, 0, 1, 0), (14, 2, 2, 0, 0), (16, 0, 2, 0, 0), (11, 1, 1, 1, 0), (19, 0, 1, 1, 1), (13, 4, 1, 1, 0),
        (17, 1, 2, 0, 1), (21, 0, 0, 0, 0), (10, 2, 0, 2, 0), (12, 3, 2, 1, 0),
    ]
    stems = [
        "what's left on my plate today", "anything still ahead of me today", "what else is on today", "what have i got left today",
        "is there more today or am i done", "what's still coming up today", "how much of today is left, anything scheduled",
        "any more today?", "whats still to do today", "what remains on my day",
    ]
    for k, (hour, na, nb, ra, rb) in enumerate(plans):
        for _try in range(60):
            now, w = mk(rng, hour=hour)
            back, ahead, r_ahead, r_back = make_today(rng, now, w, na, nb, rem_ahead=ra, rem_back=rb)
            if len(ahead) == na and len(r_ahead) == ra and len(back) == nb:
                break
        else:
            continue
        items = [ev_item(e) for e in ahead] + [rem_item(r) for r in r_ahead]
        items.sort(key=lambda i: i[0])
        earlier = sorted(back, key=lambda e: e.start)
        prompt = uniq(rng, *stems)
        if not items:
            if earlier:
                reply = fin(rng,
                            f"Nothing is left today. {join(phrase(ev_item(e)) for e in earlier)} " + ("was" if len(earlier) == 1 else "were") + " earlier, and nothing else is on.",
                            f"You're done for today. Earlier you had {join(phrase(ev_item(e)) for e in earlier)}, and nothing else is scheduled.")
            else:
                reply = fin(rng, "Nothing is scheduled today, and no reminders are due either.",
                            "Today is clear: no events and no reminders.")
        else:
            reply = agenda(items, fin(rng, "Still ahead today you have", "Left today you have", "For the rest of today you have"), loc=True)
            if len(items) >= 4:
                reply = f"Still ahead today, {counts(items)}:\n" + "\n".join("- " + bullet(i, True) for i in items)
            if earlier and len(items) < 4 and rng.random() < 0.6:
                reply += f" {earlier[0].title} at {T(earlier[0].start)} is already behind you."
        out.append(sc(prompt, now, w, [[Call("get_upcoming", period="today")], Final(reply)],
                      "what's left today: read today, keep only what is ahead of the clock (reminders included), say so plainly when none"))


def tonight(rng, out):
    """'anything tonight': the evening's items, a 6 PM reminder included; nothing is claimed that the result lacks."""
    plans = [
        (14, 0, 1, 1, 0), (15, 1, 0, 1, 0), (11, 0, 0, 1, 0), (16, 2, 1, 0, 0), (13, 1, 1, 2, 0), (17, 0, 1, 0, 0),
        (12, 0, 0, 0, 0), (18, 1, 2, 1, 1), (10, 3, 0, 1, 0), (20, 0, 1, 0, 0),
    ]
    stems = [
        "anything lined up tonight?", "what's on tonight", "do i have plans this evening", "is anything happening tonight",
        "what've i got tonight", "am i booked tonight or free", "tonight - anything i need to remember",
        "what's the plan for tonight", "anything this evening?", "got anything on later tonight",
    ]
    for k, (hour, na, nb, ra, rb) in enumerate(plans):
        for _try in range(60):
            now, w = mk(rng, hour=hour)
            back, ahead, r_ahead, r_back = make_today(rng, now, w, na, nb, rem_ahead=ra, rem_back=rb, tonight=True)
            if len(ahead) == na and len(r_ahead) == ra and len(back) == nb:
                break
        else:
            continue
        items = sorted([ev_item(e) for e in ahead] + [rem_item(r) for r in r_ahead], key=lambda i: i[0])
        prompt = uniq(rng, *stems)
        if not items:
            if back:
                reply = fin(rng, f"Nothing tonight. {back[0].title} at {T(back[0].start)} was earlier today, and nothing else is on.",
                            f"Nothing is on for tonight. The only entry today, {back[0].title} at {T(back[0].start)}, is already behind you.")
            else:
                reply = fin(rng, "Nothing is on tonight, and no reminders are due.", "Tonight is clear, with no events or reminders.")
        else:
            if len(items) == 1 and items[0][2] == "reminder":
                reply = fin(rng, f"Nothing on the calendar tonight, just the reminder “{items[0][1]}” at {T(items[0][0])}.",
                            f"No events tonight, only a reminder: “{items[0][1]}” at {T(items[0][0])}.")
            elif all(i[2] == "reminder" for i in items):
                reply = fin(rng, "No events tonight, just reminders: " + join(f"“{i[1]}” at {T(i[0])}" for i in items) + ".")
            elif len(items) >= 4:
                reply = f"Tonight you have {counts(items)}:\n" + "\n".join("- " + bullet(i, True) for i in items)
            else:
                reply = agenda(items, fin(rng, "Tonight you have", "This evening you have", "On for tonight:").replace("On for tonight:", "Tonight you have"), loc=True)
        out.append(sc(prompt, now, w, [[Call("get_upcoming", period="tonight")], Final(reply)],
                      "tonight: the evening's events and reminders (a 6 PM reminder counts), earlier items are not tonight"))


def left_plan(rng, out):
    """'what do i still have to do today' through get_my_day_plan: time-bound items still ahead, then the rest."""
    plans = [(10, 2, 1, 1, 0), (13, 1, 1, 0, 1), (15, 2, 0, 1, 0), (18, 0, 1, 0, 0)]
    stems = ["what do i still need to get done today", "anything else i have to do today", "what's left for me to handle today",
             "run me through what's still on for today"]
    for k, (hour, na, ra, n_over, n_wait) in enumerate(plans):
        for _try in range(60):
            now, w = mk(rng, hour=hour)
            back, ahead, r_ahead, _rb = make_today(rng, now, w, na, 1, rem_ahead=ra, rem_back=0)
            if len(ahead) == na and len(r_ahead) == ra:
                break
        else:
            continue
        fill_plan_extras(rng, now, w, n_over, n_wait, 1 if k % 2 == 0 else 0)
        reply = plan_text(rng, now, w, "today")
        prompt = uniq(rng, stems[k])
        out.append(sc(prompt, now, w, [[Call("get_my_day_plan")], Final(reply)],
                      "what's left to do today: get_my_day_plan; the still-ahead items, overdue, who waits, to-dos"))


# ================================================================== C. a named day
def named_day(rng, out):
    """'what's on friday' / 'the 14th' / 'next tuesday' / 'nov 9': that date only, every item with time and title."""
    sizes = [2, 3, 1, 0, 4, 2, 5, 3, 1, 6, 2, 0, 3, 4, 1, 2, 3]
    kinds = ["wd", "wd", "ordinal", "wd", "date", "next", "wd", "date", "ordinal", "wd", "next", "date", "wd", "ordinal", "wd",
             "date", "wd", "next", "ordinal", "wd"]
    stems = {
        "wd": ["what's lined up {x}", "is there anything on the calendar {x}", "{x}: what's happening", "tell me about {x}'s plans",
               "what am i doing {x}", "run me through {x}", "what does {x} look like", "give me {x}'s schedule"],
        "next": ["what does {x} hold", "what am i up to {x}", "anything booked {x}", "what's scheduled {x}"],
        "date": ["what's down for {x}", "anything on {x}", "what am i doing on {x}", "list everything on {x}"],
        "ordinal": ["what's on the calendar for {x}", "what's happening on {x}", "anything planned {x}", "what's going down {x}"],
    }
    for k, (n, kind) in enumerate(zip(sizes, kinds)):
        for _try in range(80):
            now, w = mk(rng, hour=rng.choice([8, 11, 14, 17, 20, 22]))
            if kind == "wd":
                day = pick_day(rng, now, 2, 6)
                said, period = f"{wspell(rng, day)}", WDN[day.weekday()]
            elif kind == "next":
                day = now.date() + D(days=rng.randint(4, 8))
                wdn = WDN[day.weekday()]
                said, period = f"next {wdn}", f"next {wdn}"
            elif kind == "ordinal":
                day = now.date() + D(days=rng.randint(3, 16))
                said, period = f"the {ordinal(day.day)}", f"the {ordinal(day.day)}"
            else:
                day = now.date() + D(days=rng.randint(3, 18))
                said, period = date_period(rng, day, now)[0], None
                period = said.lower()
            try:
                if span_day(w, period) != day:
                    continue
            except Exception:       # noqa: BLE001
                continue
            break
        else:
            continue
        n_ev = max(n - (1 if n >= 3 and k % 3 == 0 else 0), 0)
        put_events(rng, w, day, n_ev, lo=8, hi=20)
        n_rem = n - n_ev
        for r in rng.sample(REM_TITLES, n_rem):
            add_rem(w, r, at(day, rng.choice([9, 12, 17, 20])))
        # events on neighbouring days that must not appear in the answer
        for off in (-1, 1):
            if rng.random() < 0.6:
                put_events(rng, w, day + D(days=off), 1, lo=9, hi=18)
        items = day_items(w, day)
        prompt = pickp(rng, stems[kind], x=said)
        label = long_date(day)
        if not items:
            reply = fin(rng, f"Nothing is scheduled on {label}, and no reminders are due.",
                        f"{label} is clear: no events and no reminders.",
                        f"There's nothing on the calendar for {label}.")
        elif len(items) >= 4:
            reply = f"{label} has {counts(items)}:\n" + "\n".join("- " + bullet(i, True) for i in sorted(items))
        else:
            head = fin(rng, f"On {label} you have", f"{label} has", f"For {label} there's")
            reply = agenda(items, head, loc=True)
            if len(items) == 1:
                it = items[0]
                if it[2] == "event":
                    reply = fin(rng, f"Just one thing on {label}: {phrase(it, loc=True)}.",
                                f"{label} holds only {phrase(it, loc=True)}.")
                else:
                    reply = f"No events on {label}, only a reminder: {phrase(it)}."
        out.append(sc(prompt, now, w, [[Call("get_upcoming", period=period)], Final(reply)],
                      "a named day or date: that date only, every item with its time and title, an empty day said plainly"))


# ================================================================== D. the whole day
def fill_plan_extras(rng, now, w, n_over, n_wait, n_undated):
    titles = rng.sample(REM_TITLES, n_over + n_undated + 6)
    taken = {r.title for r in w.reminders}
    titles = [t for t in titles if t not in taken]
    for i in range(n_over):
        add_rem(w, titles[i], at(now.date() - D(days=rng.randint(1, 6)), rng.choice([9, 12, 17, 20])))
    for j in range(n_undated):
        w.reminders.append(Reminder(titles[n_over + j], None))
    for who, text in rng.sample(PEOPLE_WAIT, n_wait):
        w.messages.append(Msg(who, who, text, now - D(hours=rng.randint(2, 30)), True))


def plan_text(rng, now, w, day="today"):
    """The whole-day answer from what get_my_day_plan prints."""
    target = now.date() + D(days=1 if day == "tomorrow" else 0)
    evs = [e for e in w.events if e.start.date() == target and (day == "tomorrow" or e.end > now)]
    rems = [r for r in w.reminders if r.due and not r.done and r.due.date() == target and r.due >= now]
    overdue = sorted([r for r in w.reminders if r.due and not r.done and r.due < now and r.due.date() < now.date()], key=lambda r: r.due)
    waiting = [m for m in w.messages if m.sender != "Me" and m.unread][:2]
    undated = [r for r in w.reminders if r.due is None and not r.done][:3]
    timed = sorted([ev_item(e) for e in evs] + [rem_item(r) for r in rems], key=lambda i: i[0])
    when = "tomorrow" if day == "tomorrow" else "today"
    parts = []
    if timed:
        if len(timed) >= 4:
            parts.append(f"{when.capitalize()} you have {counts(timed)}:\n" + "\n".join("- " + bullet(i) for i in timed))
        else:
            parts.append(f"{when.capitalize()} you have " + join(phrase(i) for i in timed) + ".")
    else:
        parts.append(rng.choice([f"Nothing is time-bound {when}.", f"Nothing is on the clock {when}."]))
    if overdue:
        shown = overdue[:3]
        items = [f"{r.title} ({(now.date() - r.due.date()).days} day{'s' if (now.date() - r.due.date()).days != 1 else ''})" for r in shown]
        if len(overdue) > 3:
            items.append(f"{len(overdue) - 3} more")
        n = len(overdue)
        lead = rng.choice([f"{plural(n, 'item')} {'is' if n == 1 else 'are'} overdue: ", f"Overdue: "])
        parts.append(lead + join(items) + ".")
    if waiting:
        # the plan prints each waiting person with the text (Mom (text): Call me when you get a chance)
        names = [f"{m.conv} (“{m.text}”)" for m in waiting]
        parts.append(f"{join(names)} {'is' if len(names) == 1 else 'are'} waiting on a reply.")
    if undated:
        parts.append(rng.choice(["Also on your list: ", "Whenever there's time: "]) + join(r.title for r in undated) + ".")
    sep = "\n" if "\n" in parts[0] else " "
    return sep.join(parts)


def whole_day(rng, out):
    """'how does my day look': timed items, overdue, who is waiting and the open to-dos, in a few sentences."""
    plans = [  # (hour, day, ahead events, reminders ahead, overdue, waiting, undated)
        (8, "today", 3, 0, 1, 1, 0), (9, "today", 2, 1, 2, 0, 1), (13, "today", 1, 1, 0, 1, 1), (7, "today", 4, 1, 2, 1, 1),
        (11, "today", 0, 0, 2, 0, 0), (15, "today", 2, 0, 0, 2, 0), (10, "today", 0, 1, 0, 0, 0), (19, "today", 1, 0, 3, 1, 0),
        (21, "tomorrow", 3, 1, 1, 0, 1), (22, "tomorrow", 2, 0, 0, 1, 0), (20, "tomorrow", 5, 1, 0, 0, 0), (18, "tomorrow", 0, 0, 1, 0, 1),
        (12, "today", 2, 2, 4, 1, 2), (9, "tomorrow", 1, 1, 0, 2, 0), (16, "today", 3, 0, 0, 0, 0),
    ]
    stems = {
        "today": ["how's my day looking", "walk me through today", "what's the shape of my day", "so what's today like for me",
                  "give me the rundown for today", "what does today look like", "how does today shape up", "okay, my day, go",
                  "what is my day made of", "give me the picture for today", "my day, in a nutshell?",
                  "what am i dealing with today", "sum up my day for me"],
        "tomorrow": ["how's tomorrow shaping up", "walk me through tomorrow", "give me the lay of the land for tomorrow",
                     "what does tomorrow have in store", "tomorrow, what am i dealing with", "sum up tomorrow for me",
                     "what's tomorrow made of"],
    }
    for k, (hour, day, ne, nr, n_over, n_wait, n_und) in enumerate(plans):
        for _try in range(60):
            now, w = mk(rng, hour=hour)
            target = now.date() + D(days=1 if day == "tomorrow" else 0)
            if day == "tomorrow":
                evs = put_events(rng, w, target, ne, lo=8, hi=20)
                got_e = len(evs) == ne
                rems_ = []
                for t in rng.sample(REM_TITLES, nr):
                    rems_.append(add_rem(w, t, at(target, rng.choice([9, 12, 17, 20]))))
                ok = got_e
            else:
                back, ahead, r_ahead, _rb = make_today(rng, now, w, ne, 1 if hour > 11 and k % 3 == 0 else 0, rem_ahead=nr)
                ok = len(ahead) == ne and len(r_ahead) == nr
            if ok:
                break
        else:
            continue
        fill_plan_extras(rng, now, w, n_over, n_wait, n_und)
        reply = plan_text(rng, now, w, day)
        prompt = uniq(rng, *stems[day])
        call = Call("get_my_day_plan") if day == "today" else Call("get_my_day_plan", day="tomorrow")
        out.append(sc(prompt, now, w, [[call], Final(reply)],
                      "the whole day: get_my_day_plan; timed items, overdue, who is waiting and open to-dos with counts and nouns"))


# ================================================================== E. the past
def _at(e, inner=False):
    """' at <place>' for a past event whose place get_past_events prints ('event: Title @ Place')."""
    if not e.location or e.location == "Home":
        return ""
    return f", {e.location}" if inner else f" ({e.location})"


def the_past(rng, out):
    """'what did i have last week' / 'yesterday' / 'monday': get_past_events, past tense, the asked window only."""
    plans = [("last week", 3), ("last week", 5), ("yesterday", 2), ("last week", 1), ("monday", 2), ("yesterday", 0), ("last week", 4),
             ("yesterday", 1), ("monday", 1), ("last week", 0)]
    stems = {
        "last week": ["what did my calendar hold last week", "what did my last week look like", "remind me what i had last week",
                      "what happened on my calendar last week", "list what i had last week"],
        "yesterday": ["what was on my plate yesterday", "what was on the books yesterday", "anything on my calendar yesterday",
                      "what was my day yesterday"],
        "monday": ["what did i have on monday", "what was my monday"],
    }
    for k, (kind, n) in enumerate(plans):
        for _try in range(80):
            now, w = mk(rng, weekday=rng.choice([2, 3, 4, 5]) if kind == "monday" else rng.choice([1, 2, 3, 4, 5, 6]),
                        hour=rng.choice([9, 14, 19]))
            if kind == "last week":
                monday = now.date() - D(days=now.weekday() + 7)
                days = [monday + D(days=i) for i in range(7)]
            elif kind == "yesterday":
                days = [now.date() - D(days=1)]
            else:
                days = [now.date() - D(days=now.weekday())]
            pool = POOL[:]
            rng.shuffle(pool)
            got = []
            for _ in range(n):
                d = rng.choice(days)
                for title, mins, loc, hours in pool:
                    if any(g.title == title for g in w.past):
                        continue
                    ev = Event(title, at(d, rng.choice(hours), rng.choice([0, 30])), mins, loc)
                    if ev.start < now - D(hours=3) and not any(ev.start < g.end and ev.end > g.start for g in w.past):
                        w.past.append(ev)
                        got.append(ev)
                        break
            # items outside the window that the answer must leave out
            outside = [d for d in (days[0] - D(days=rng.randint(1, 3)), days[-1] + D(days=1)) if d < now.date()]
            for d in outside[:1]:
                for title, mins, loc, hours in pool:
                    if all(title != g.title for g in w.past):
                        w.past.append(Event(title, at(d, hours[0]), mins, loc))
                        break
            if len(got) == n:
                break
        else:
            continue
        w.past.sort(key=lambda e: e.start)
        window = sorted([e for e in w.past if e.start.date() in days], key=lambda e: e.start)
        days_back = {"last week": 14, "yesterday": 2, "monday": 7}[kind]
        prompt = uniq(rng, *stems[kind])
        if kind == "last week":
            if not window:
                reply = fin(rng, "Nothing was on your calendar last week.", "Last week's calendar was empty.")
            elif len(window) >= 4:
                reply = f"Last week you had {plural(len(window), 'event')}:\n" + "\n".join(f"- {e.start:%A} at {T(e.start)}: {e.title}{_at(e)}" for e in window)
            else:
                reply = fin(rng, "Last week you had " + join(f"{e.title} on {e.start:%A} at {T(e.start)}{_at(e)}" for e in window) + ".",
                            "On your calendar last week: " + join(f"{e.title} ({e.start:%A}, {T(e.start)}{_at(e, True)})" for e in window) + ".")
        else:
            lab = "yesterday" if kind == "yesterday" else "Monday"
            if not window:
                reply = fin(rng, f"Nothing was on your calendar {lab}.", f"You had nothing scheduled {lab}.")
            elif len(window) >= 4:
                reply = f"{lab.capitalize()} you had {plural(len(window), 'event')}:\n" + "\n".join(f"- {T(e.start)}: {e.title}{_at(e)}" for e in window)
            else:
                body = join(f"{e.title} at {T(e.start)}{_at(e)}" for e in window)
                reply = fin(rng, f"{lab.capitalize()} you had {body}.", f"You had {body} {lab}.")
        out.append(sc(prompt, now, w, [[Call("get_past_events", days=days_back)], Final(reply)],
                      "past days: get_past_events wide enough for the window, only that window's items, in the past tense"))


# ================================================================== family
def fam_day_answers_r4(rng):
    out: list = []
    _FIRST.clear()
    USED_PROMPTS.clear()
    for fn in (free_part_of_day, free_hour_on_date, free_when, free_at_time, free_holiday, free_trip,
               left_today, tonight, left_plan, named_day, whole_day, the_past):
        fn(rng, out)
    return out


FAMILIES_R4 = {FAMILY: fam_day_answers_r4}
