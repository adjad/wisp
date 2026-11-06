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
import os
import re
import zlib

import gen
import harness as H
from gen import D
from harness import Call, Final
from replies import join, plural
from scen import Scenario, cap1, dedupe_mail, setup
from scen_r2_args import WDN, at
from scen_r3_safe import _fw
from world import BriefItem, Email, Event, Msg, Note, Reminder, Waiting, short_clock

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


def pickp(rng, stems, plan=None, **kw):
    """A prompt from ``stems`` (formatted with ``kw``) not used yet in this family."""
    return uniq(rng, *[x.format(**kw) for x in stems], plan=plan)


def uniq(rng, *opts, plan=None):
    """A prompt not used yet in this family that leaks no eval or toolbench question. plan=True keeps the prompts the host
    reads as a day-plan question (model_only.day_plan_question: the Brief plan tool is offered and hinted), plan=False the
    ones it does not (a calendar ask)."""
    from scen_r2_act import _leaks
    from service.model_only import day_plan_question
    fits = [o for o in opts if not _leaks().leaks(o)[0] and (plan is None or day_plan_question(o) == plan) and not near_gate(o)]
    ok = [o for o in fits if o not in USED_PROMPTS] or fits
    if not ok:
        raise ValueError(f"no usable prompt among {list(opts)[:4]} (plan={plan})")
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
    """(result text, ['9:00 AM–11:00 AM', ...] of the first day line) from the world's find_free_time. Each range runs from
    the slot's start to its 'free until' time, as World.free_ranges reads the live text. A result that lists slots but
    parses to no range raises: a silent empty list would turn a free day into 'no gap'."""
    res, spans = w.free_ranges(period, **kw)
    if "free until" in res and not spans:
        raise ValueError(f"free_gaps: slots listed but no range parsed from {res[:300]!r}")
    gaps = [f"{a}–{b}" for a, b in spans]
    for g in gaps:
        a, b = g.split("–")
        if not (re.fullmatch(r"\d{1,2}:\d\d [AP]M", a) and re.fullmatch(r"\d{1,2}:\d\d [AP]M", b)):
            raise ValueError(f"free_gaps: malformed range {g!r}")
    return res, gaps


def win(rng, res, p=0.75):
    """A sentence naming the window and slot length find_free_time printed ('30-minute candidate slots, 9:00–18:00'), or ''
    (the result's own figures, in the clock form the answer uses)."""
    m = re.search(r"(\d+)-minute candidate slots, (\d{1,2}):(\d\d)–(\d{1,2}):(\d\d)", res)
    if not m:
        raise ValueError(f"win: no candidate-slot header in {res[:200]!r}")
    if rng.random() > p:
        return ""
    ck = lambda h, mi: f"{int(h) % 12 or 12}{':' + mi if mi != '00' else ''} {'AM' if int(h) < 12 else 'PM'}"
    a, b, n = ck(m.group(2), m.group(3)), ck(m.group(4), m.group(5)), m.group(1)
    return " " + rng.choice([f"I checked within {a} to {b} for stretches of {n} minutes or more.",
                             f"I searched within {a} to {b}, counting only stretches of {n} minutes or more."])


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


def calendar_reply(rng, items, day, now):
    """The final for an explicit calendar ask (events, meetings, appointments) over get_upcoming's agenda ``items``. With no
    event but a reminder the answer says the calendar is empty first, then names the reminder; with nothing it says so."""
    label = long_date(day)
    events = [i for i in items if i[2] != "reminder"]
    rems = [i for i in items if i[2] == "reminder"]
    if not items:
        return fin(rng, f"Nothing is on your calendar {on_word(day, now)}, and no reminders are due.")
    if not events:
        one = len(rems) == 1
        lead = rng.choice([f"Nothing on your calendar {on_word(day, now)}, but there's " if one
                           else f"Nothing on your calendar {on_word(day, now)}, but you do have ",
                           f"No events {on_word(day, now)}, though you do have ",
                           (f"Your calendar is clear {on_word(day, now)}; the only thing is " if one
                            else f"Your calendar is clear {on_word(day, now)}; the only things are ")])
        return lead + join(phrase(i) for i in sorted(rems)) + "."
    if len(items) >= 4:
        return f"{label} has {counts(items)}:\n" + "\n".join("- " + bullet(i, True) for i in sorted(items))
    return agenda(items, fin(rng, f"On {label} you have", f"{label} has"), loc=True)


# ---- gate-wording proximity: training prompts stay more than one token from the toolbench prompts (scripts/toolbench/cases/brief.yaml)
_GATE_CACHE: list = []
_DAYWORD = re.compile(r"\b(?:(?:the rest of )?(?:this |next |the )?week(?:end)?|(?:mon|tue|wed|thu|thr|fri|sat|sun)[a-z]{0,8}|today|tomorrow)\b")
_LEAD_FILL = {"ok", "okay", "hmm", "so", "cool", "alright", "hey", "um", "hi", "quick", "one", "now", "then", "also", "wait", "well", "sure", "and"}
_TAIL_FILL = {"pls", "thanks", "please", "then", "too", "now", "instead", "again"}


def _norm_tokens(p):
    t = _DAYWORD.sub("D", p.lower())
    return re.sub(r"[^a-z0-9' ]", " ", t).split()


def _strip_fill(toks):
    """The tokens without leading fillers ('ok so', 'hey', 'and') and trailing ones ('pls', 'for me', 'thanks', 'then')."""
    toks = list(toks)
    while toks and toks[0] in _LEAD_FILL:
        toks.pop(0)
    while toks:
        if toks[-1] in _TAIL_FILL:
            toks.pop()
        elif toks[-2:] == ["for", "me"]:
            toks = toks[:-2]
        else:
            break
    return toks


def _tok_dist(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def gate_prompts():
    """The brief.yaml toolbench prompts, as day-neutral token lists."""
    if not _GATE_CACHE:
        import pathlib
        path = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "toolbench" / "cases" / "brief.yaml"
        text = path.read_text() if path.exists() else ""
        for m in re.finditer(r'^\s*prompt:\s*"((?:[^"\\]|\\.)*)"', text, re.M):
            _GATE_CACHE.append(_norm_tokens(m.group(1)))
        if not _GATE_CACHE:         # the cases file is missing: keep the wordings that were in it on 2026-11-02
            for p in ["and friday?", "and for friday?", "ok and then friday", "and for sunday?", "what about saturday", "i mean this week",
                      "i mean everything, texts and emails too", "and the rest of the week?", "how about this weekend",
                      "anything else I have to do this week", "ok and how does tomorrow look then?",
                      "what is my plan for saturday", "which events do i have this week"]:
                _GATE_CACHE.append(_norm_tokens(p))
    return _GATE_CACHE


def near_gate(p):
    """True when ``p`` is within one token of a gate prompt, after day words (typos too) are normalised and fillers (a prefix, a
    suffix) are stripped from either side; the plain and the stripped forms are both compared."""
    toks = _norm_tokens(p)
    forms = [toks, _strip_fill(toks)]
    for g in gate_prompts():
        for gf in (g, _strip_fill(g)):
            if not gf:
                continue
            for f in forms:
                if f and _tok_dist(f, gf) <= 1:
                    return True
    return False


def stem_key(p):
    """A prompt's repeat key: its day-normalised stem with fillers stripped."""
    return " ".join(_strip_fill(_norm_tokens(p)))


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


# ================================================================== Wisp's Brief: seeds, parsing, narration
# The Brief holds what mail, texts and notes add to the calendar: dated to-dos (World.brief) and the people waiting on a
# reply (World.waiting). Every seeded item is derived from a message the same world holds, so a re-read finds the words
# it came from. Topics here are deliberately not the gate world's.
class TextSeed:
    def __init__(self, who, text, title, clock=None):
        self.who, self.text, self.title, self.clock = who, text, title, clock


TEXT_SEEDS = [
    TextSeed("Aunt Priya", "Housewarming party {d} evening! Bring a plant if you can", "Bring a plant to Aunt Priya's housewarming"),
    TextSeed("Dad", "My flight lands {d} at 4:40 pm. Can you pick me up from the airport?", "Pick up Dad from the airport", (16, 40)),
    TextSeed("Theo Park", "Climbing gym {d} at 6? I'll grab us passes", "Go climbing with Theo", (18, 0)),
    TextSeed("Maya Chen", "The landlord is coming {d} at 10 to fix the sink, can you be home?", "Be home for the sink repair", (10, 0)),
    TextSeed("Jordan Ellis", "Lab writeup is due {d} at 11:59, did you do your half?", "Finish the lab writeup", (23, 59)),
    TextSeed("Aisha Karim", "Study group {d} at 7, bring your linear algebra notes", "Bring linear algebra notes to study group", (19, 0)),
    TextSeed("Ben Ortiz", "Long run {d} at 8am at Riverside, you in?", "Go on the long run at Riverside", (8, 0)),
    TextSeed("Carmen Rivera", "Mom's birthday dinner is {d}! Don't forget the cake", "Pick up the cake for Mom's birthday dinner"),
    TextSeed("Coach Reyes", "Team dinner {d} at 6:30, Luigi's on 5th", "Go to the team dinner", (18, 30)),
    TextSeed("Dana Whitfield", "Can you cover my help desk shift {d} at 10? Need to know soon", "Cover Dana's help desk shift", (10, 0)),
    TextSeed("Hana Kowalczyk", "My office hours moved to {d} at 3pm if you still want to go over the regrade", "Go to Hana's office hours", (15, 0)),
    TextSeed("Nadia Khan", "Game night at mine {d}, 7pm. Bring snacks!", "Bring snacks to game night", (19, 0)),
    TextSeed("Uncle Dev", "Ravi's graduation party is {d} afternoon at the park, 2pm", "Go to Ravi's graduation party", (14, 0)),
    TextSeed("Grandma Ruth", "Call me {d} morning, I want to hear about your classes", "Call Grandma Ruth"),
]
COURSES = [("CS 162", "Project 3"), ("MATH 241", "Problem Set 7"), ("PHIL 110", "Reading Response 4"), ("CS 210", "Lab 6"),
           ("CS 162", "Homework 5"), ("MATH 241", "Quiz Corrections")]


class MailSeed:
    def __init__(self, sender, addr, person, subject, body, title, clock=None):
        self.sender, self.addr, self.person, self.subject, self.body, self.title, self.clock = (
            sender, addr, person, subject, body, title, clock)


MAIL_SEEDS = [
    MailSeed("LindenLearn", "notifications@lindenlearn.edu", False, "Assignment due: {course} {hw}",
             "{hw} for {course} is due {d} at 11:59 PM. Submit it on LindenLearn.", "Finish {hw}, {course}", (23, 59)),
    MailSeed("Prof. Adaeze Okafor", "aokafor@linden.edu", True, "Project 2 extension",
             "You have until {d} at 11:59 PM for project 2. Please don't slip further.", "Submit project 2", (23, 59)),
    MailSeed("Renata Szabo", "rszabo@fernbrooklabs.com", True, "Fernbrook Labs: interview times",
             "Could you send me three 45-minute slots by {d}?", "Send Fernbrook your interview times"),
    MailSeed("Pine Street Pharmacy", "rx@pinestreetpharmacy.example", False, "Your prescription is ready",
             "Your prescription is ready for pickup. We will hold it until {d} at 6 PM.", "Pick up your prescription", (18, 0)),
    MailSeed("Linden Career Center", "careers@linden.edu", False, "RSVP: spring career fair",
             "Please RSVP for the career fair by {d} so we can plan badges.", "RSVP for the career fair"),
    MailSeed("Dana Whitfield", "dwhitfield@linden.edu", True, "Next week's help desk schedule",
             "Please confirm your shifts for next week by {d}.", "Confirm next week's help desk shifts"),
    MailSeed("Hana Kowalczyk", "hkowalczyk@linden.edu", True, "Section 4 quiz",
             "Quiz 4 is {d} in section at 2 PM. Bring a pencil.", "Take Quiz 4 in section", (14, 0)),
]
NOTE_SEEDS = [
    ("Weekend to-do", "{D}: return the library books\nBuy a birthday card", "Return the library books"),
    ("Errands", "{D} - drop off the donation box", "Drop off the donation box"),
    ("Trip prep", "Pack the climbing gear before {d}", "Pack the climbing gear"),
    ("Birthday plans", "Order the cake by {d}", "Order the cake"),
    ("Lab notes", "{D}: hand in the signed lab safety form", "Hand in the lab safety form"),
    ("Bike", "Take the bike to the shop {d} morning", "Take the bike to the shop"),
]
# (who, what their last text says, what the Brief says they asked)
WAIT_SEEDS = [
    ("Maya Chen", "can you grab milk on your way home?", "Asked you to grab milk on your way home."),
    ("Theo Park", "still on for coffee?", "Asked if you're still on for coffee."),
    ("Jordan Ellis", "are you done with the analysis part?", "Asked if you finished the analysis part."),
    ("Nadia Khan", "did you see the photos i sent?", "Asked if you saw the photos."),
    ("Carmen Rivera", "did you decide what to get mom?", "Asked what you decided to get Mom."),
    ("Ben Ortiz", "are you in for the long run?", "Asked if you're in for the long run."),
    ("Aisha Karim", "can you send me the practice problems?", "Asked you to send the practice problems."),
    ("Dad", "did you book your flight home yet?", "Asked if you booked your flight home."),
    ("Hana Kowalczyk", "did the regrade go through?", "Asked whether the regrade went through."),
    ("Naomi Brandt", "can we move our call to next week?", "Asked to move your call to next week."),
]


# How a person would point back at a Brief item they were just told about.
REFS = {
    "Bring a plant to Aunt Priya's housewarming": "the housewarming", "Pick up Dad from the airport": "dad's airport pickup",
    "Go climbing with Theo": "the climbing plan", "Be home for the sink repair": "the sink repair",
    "Finish the lab writeup": "the lab writeup", "Bring linear algebra notes to study group": "the study group",
    "Go on the long run at Riverside": "the long run", "Pick up the cake for Mom's birthday dinner": "the cake pickup",
    "Go to the team dinner": "the team dinner", "Cover Dana's help desk shift": "the shift cover",
    "Go to Hana's office hours": "hana's office hours", "Bring snacks to game night": "game night",
    "Go to Ravi's graduation party": "ravi's party", "Call Grandma Ruth": "the call to grandma",
    "Submit project 2": "project 2", "Send Fernbrook your interview times": "the fernbrook times",
    "Pick up your prescription": "the prescription pickup", "RSVP for the career fair": "the career fair rsvp",
    "Confirm next week's help desk shifts": "the shift confirmation", "Take Quiz 4 in section": "quiz 4",
    "Return the library books": "the library books", "Drop off the donation box": "the donation box",
    "Pack the climbing gear": "the climbing gear", "Order the cake": "the cake order",
    "Hand in the lab safety form": "the safety form", "Take the bike to the shop": "the bike shop trip",
}


def ref_of(title):
    m = re.match(r"Finish (.+), [A-Z]+ \d+$", title)
    return REFS.get(title) or (m.group(1).lower() if m else title.lower())


def ensure_contact(w, name):
    if name not in w.contacts:
        w.contacts[name] = (f"(503) 555-{200 + zlib.crc32(name.encode()) % 800:04d}", "")
    return name


def said(rng, day, now):
    """How a text or note would name ``day`` for a reader on ``now``'s date."""
    delta = (day - now.date()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return rng.choice(["tomorrow", "tomorrow", wd_name(day).lower()])
    return wd_name(day).lower()


def tidy_sources(w):
    """Mail and notes the Brief would turn into dated to-dos are not left lying around unlisted: keep only undated
    newsletters and notes, so a seeded world's Brief is the whole truth of its mail, texts and notes."""
    w.emails = [m for m in w.emails if not m.person and not re.search(r"\bdue\b|rsvp|deadline|expires", m.subject, re.I)][:3]
    w.notes = [n for n in w.notes if not re.search(r"\b(mon|tues?|wed|thu|fri|sat|sun)\w*\b|due|by |tonight|tomorrow", n.body, re.I)][:2]
    w.waiting = []


def seed_brief(rng, w, now, day, kind, *, used=None, timed=None):
    """One dated Brief item on ``day`` from a text, an email or a note (kind), plus the source it came from. The item
    is picked among seeds this world has not used; timed=True/False asks for a stated clock time or none.
    Returns the BriefItem, or None when no seed fits (a clock time already past today)."""
    used = used if used is not None else set()
    d_said = said(rng, day, now)
    sent = now - D(hours=rng.randint(4, 90))
    if kind == "text":
        pool = [s for s in TEXT_SEEDS if s.title not in used and s.who not in {m.conv for m in w.messages}]
    elif kind == "mail":
        pool = [s for s in MAIL_SEEDS if s.title not in used]
    else:
        pool = [s for i, s in enumerate(NOTE_SEEDS) if s[2] not in used]
    if timed is not None and kind != "note":
        pool = [s for s in pool if bool(s.clock) == timed]
    if kind != "note":
        pool = [s for s in pool if not s.clock or at(day, *s.clock) > now + D(minutes=90)]
    if not pool:
        return None
    s = rng.choice(pool)
    if kind == "text":
        text = s.text.format(d=d_said)
        msg = Msg(s.who, s.who, text, sent, False)
        w.messages.append(msg)
        ensure_contact(w, s.who)
        item = BriefItem(s.title, at(day, *(s.clock or (23, 59))), "messages", s.who, bool(s.clock), msg)
    elif kind == "mail":
        course, hw = rng.choice(COURSES)
        fmt = dict(d=d_said.capitalize() if d_said in ("today", "tomorrow") else d_said, course=course, hw=hw)
        body = s.body.format(**fmt)
        em = Email(s.sender, s.addr, s.subject.format(**fmt), sent, body, False, s.person)
        w.emails.append(em)
        item = BriefItem(s.title.format(**fmt), at(day, *(s.clock or (23, 59))), "mail", s.sender, bool(s.clock), em)
    else:
        title, body, brief_title = s
        note = Note(title, body.format(d=d_said, D=d_said.capitalize()), sent - D(hours=rng.randint(1, 30)), "Notes")
        w.notes.append(note)
        item = BriefItem(brief_title, at(day, 23, 59), "notes", "", False, note)
    w.brief.append(item)
    used.add(s[2] if kind == "note" else s.title)
    return item


def seed_waiting(rng, w, now, n=1, *, via_mail=False, who=None):
    """n people waiting on a reply, each with the text (or email) that makes them wait."""
    pool = [s for s in WAIT_SEEDS if s[0] not in {p.who for p in (w.waiting or [])} and s[0] not in {m.conv for m in w.messages}]
    rng.shuffle(pool)
    if who:
        pool = [s for s in WAIT_SEEDS if s[0] == who] + pool
    out = []
    for name, text, summary in pool[:n]:
        when = now - D(hours=rng.randint(2, 40))
        if via_mail and name in ("Hana Kowalczyk", "Naomi Brandt"):
            w.emails.append(Email(name, name.split()[0].lower() + "@linden.edu", True, "Quick question", text.capitalize(), when, True))
            w.waiting.append(Waiting(name, summary, "mail", "Quick question", when))
        else:
            w.messages.append(Msg(name, name, text, when, True))
            w.waiting.append(Waiting(name, summary, "messages", text, when))
        ensure_contact(w, name)
        out.append(w.waiting[-1])
    return out


# ---- reading the tools' text (the finals are written from what the tool printed, never from the world)
CLOCK_P = r"(?:\d{1,2}(?::\d\d)? [ap]m)"
_ENTRY = re.compile(rf"^- (?P<when>all day|{CLOCK_P}) · (?P<title>.+) \((?P<src>calendar|reminder|email|text|note)\)$")
_HEAD_DAY = re.compile(r"^Day plan for (?P<word>[^,]+), (?P<date>\w{3} \w{3} \d+) \(from Wisp's Brief")
_HEAD_RANGE = re.compile(r"^Plan for (?P<label>.+), (?P<a>\w{3} \w{3} \d+)–(?P<b>\w{3} \w{3} \d+) \(from Wisp's Brief")


class Plan:
    """What a get_my_day_plan result lists."""

    def __init__(self, text):
        self.text, self.kind, self.word, self.label = text, None, None, None
        self.overview, self.days, self.overdue, self.waiting, self.cards, self.other = "", [], [], [], [], []
        self.more, self.more_by_day, self.empty, self.error = {}, {}, "", ""
        lines = text.splitlines()
        if text.startswith("(error"):
            self.error = text
            return
        m = _HEAD_DAY.match(lines[0])
        r = _HEAD_RANGE.match(lines[0])
        if m:
            self.kind, self.word, self.label = "day", m["word"], m["date"]
        elif r:
            self.kind, self.word, self.label = "range", r["label"], f"{r['a']}–{r['b']}"
        else:
            raise ValueError(f"not a plan: {lines[0]!r}")
        sec, bucket = None, None
        for ln in lines[1:]:
            if ln.startswith("Overview: "):
                self.overview = ln[len("Overview: "):]
            elif ln.startswith("Mail and texts have not been analyzed"):
                raise ValueError("Brief state unavailable")
            elif ln.startswith("Nothing "):
                self.empty = ln
            elif ln.startswith("- …and "):
                n_more = int(re.match(r"- …and (\d+) more", ln)[1])
                self.more[sec] = n_more
                if sec == "day":
                    self.more_by_day[self.days[-1][0]] = n_more
            elif ln == "- …":
                self.more["truncated"] = 1
            elif ln.startswith("- "):
                if sec == "day":
                    e = _ENTRY.match(ln)
                    if not e:
                        raise ValueError(f"unparsed plan line {ln!r}")
                    bucket.append((e["when"], e["title"], e["src"]))
                elif sec == "overdue" or sec == "other":
                    title, _, label = ln[2:].partition(" · ")
                    (self.overdue if sec == "overdue" else self.other).append((title, label))
                elif sec == "waiting":
                    mm = re.match(r"- (?P<who>.+?) \((?P<src>text|email)\): (?P<sum>.*)$", ln)
                    self.waiting.append((mm["who"], mm["src"], mm["sum"]))
                elif sec == "cards":
                    self.cards.append(ln[2:])
            elif ln.endswith(":"):
                title = ln[:-1]
                if title == "Overdue":
                    sec = "overdue"
                elif title == "Waiting on a reply from you":
                    sec = "waiting"
                elif title == "Suggested next steps":
                    sec = "cards"
                elif title == "Other to-dos":
                    sec = "other"
                else:
                    sec = "day"
                    bucket = []
                    self.days.append((title.replace("Time-bound ", ""), bucket))
            else:
                raise ValueError(f"unparsed plan line {ln!r}")

    @property
    def n_lines(self):
        return sum(len(e) for _t, e in self.days) + len(self.overdue) + len(self.waiting) + len(self.other)


BRIEF_LINE = re.compile(r"^- (?P<day>\w{3}, \w{3} \d+)(?:, due (?P<due>\d{1,2}:\d\d [AP]M))?(?P<over> \(overdue\))? · "
                        r"(?P<title>.+) \((?P<src>email|text|note)(?: from (?P<who>.+))?\)$")


def brief_lines(res):
    """The dated Brief items a get_upcoming, find_free_time, search_reminders or predict_my_day result lists apart."""
    out = []
    if "*Also from your Brief*" not in res:
        return out
    for ln in res.split("*Also from your Brief*", 1)[1].splitlines()[1:]:
        m = BRIEF_LINE.match(ln)
        if m:
            out.append(m.groupdict())
        elif ln.startswith("- "):
            raise ValueError(f"unparsed Brief line {ln!r}")
    return out


def clock12(when):
    return when if when == "all day" else when.upper()


def src_kind(src, who=None):
    return {"email": "an email", "text": "a text", "note": "a note"}[src] + (f" from {who}" if who and src != "note" else "")


def say_entry(e, loc=False):
    """One time-bound plan line, in words."""
    when, title, src = e
    t = None if when == "all day" else clock12(when)
    if src == "calendar":
        return f"{title} at {t}" if t else f"{title}, all day"
    if src == "reminder":
        return f"the reminder “{title}” at {t}" if t else f"the reminder “{title}”"
    return f"“{title}” at {t} (from {src_kind(src)})" if t else f"“{title}” (from {src_kind(src)})"


def r5():
    """Round 5 (ASK_LORA_R5=1): a final lists what the tool returned and never counts what it did not ("and N more")."""
    return os.environ.get("ASK_LORA_R5") == "1"


def cut_note(rng, plan):
    """Round 5: the plain sentence that says a list the host cut stays cut, and how to see the rest. Empty under r4 and when
    nothing was cut. It draws from ``rng`` only when a list was cut, so rows without a cut keep their wording."""
    if not r5() or not plan.more:
        return ""
    m, out = plan.more, []
    if plan.kind == "day" and m.get("day"):
        n = len(plan.days[0][1]) if plan.days else 5
        out.append(rng.choice([
            f"That's the first {n} for {plan.word}; ask for your calendar on its own to see the rest.",
            f"The plan cut the list for {plan.word} after {n} items, so some aren't shown here; ask for just your calendar to see them all.",
            f"The list stops at {n} items for {plan.word} and was cut there; asking for your calendar alone will show everything."]))
    elif plan.kind == "range" and plan.more_by_day:
        names = [d.lower() if d in ("Today", "Tomorrow") else d for d in plan.more_by_day]
        days, one = join(names), len(names) == 1
        that = "that day" if one else "one of those days"
        out.append(rng.choice([
            f"The list was cut off for {days}; ask about {that} to see all of it.",
            f"Not every item for {days} is shown, since the list was cut there; ask for a single day to see the rest.",
            f"The plan lists less than {days} holds, so ask for {'that day' if one else 'one of them'} on its own to see everything."]))
    if m.get("overdue"):
        n = len(plan.overdue)
        out.append(rng.choice([f"Only the first {n} overdue items are listed; ask for your reminders to see the rest.",
                               f"The overdue list was cut after {n}; your reminders will show the rest."]))
    if m.get("waiting"):
        n = len(plan.waiting)
        out.append(rng.choice([f"Only the first {n} people waiting on a reply are listed; ask about your messages to see the rest.",
                               f"The list of people waiting on you was cut after {n}; ask about your messages for the rest."]))
    if m.get("other"):
        n = len(plan.other)
        out.append(rng.choice([f"Only the first {n} other to-dos are listed; ask for your reminders to see the rest.",
                               f"The other to-dos were cut after {n}; your reminders will show the rest."]))
    if m.get("truncated") and not out:
        out.append(rng.choice(["The list was cut off there; ask about a single day to see everything.",
                               "That list was cut short; ask for one day at a time to see all of it."]))
    return " ".join(out)


def join_more(xs, more=0):
    """``join`` for a list that a '..and N more' follows: commas only, so 'A, B, and 2 more' never reads 'A and B, and 2 more'.
    Round 5 says nothing of the count (``cut_note`` does)."""
    xs = list(xs)
    if not more or r5():
        return join(xs)
    return ", ".join(xs) + f", and {more} more"


def say_over(items, more=0):
    return join_more((f"{t} ({lab.replace('Overdue · ', '')} overdue)" for t, lab in items), more)


def lc_first(s):
    return s[:1].lower() + s[1:] if s[1:2].islower() else s


def say_wait(w):
    who, src, summ = w
    return f"{who} is waiting on a reply: {lc_first(summ.rstrip('.'))}"


def say_other(items, with_day=True, more=0):
    """The plan's 'Other to-dos': a due label ('Sat', 'Fri 11:59 PM') stays as the plan printed it. ``more`` trails as ', and N more'."""
    return join_more((f"{t}" + (f" ({lab})" if lab and with_day else "") for t, lab in items), more)


def clock_of(when):
    """'6:30 pm' -> datetime.time(18, 30); None for 'all day'."""
    if when == "all day":
        return None
    return dt.datetime.strptime(when.upper(), "%I:%M %p" if ":" in when else "%I %p").time()


def other_sentence(rng, plan):
    """The plan's 'Other to-dos' in prose. The tool's '...and N more' trails the whole section, so it closes the last sentence."""
    dated = [(t, lab) for t, lab in plan.other if lab]
    undated = [(t, lab) for t, lab in plan.other if not lab]
    more = plan.more.get("other", 0)
    out = []
    if dated:
        out.append(rng.choice(["Coming up after that: ", "Also coming up: "]) + say_other(dated, more=(more if not undated else 0)) + ".")
    if undated:
        out.append(rng.choice(["Also open: ", "Still open: ", "Whenever there's time: "]) + say_other(undated, more=more) + ".")
    return " ".join(out)


PLAN_LEADS_TODAY = ["Today you have {x}.", "On today's list: {x}.", "Today holds {x}."]
PLAN_LEADS_DAY = ["{W} has {x}.", "On {w} you have {x}.", "For {w} there's {x}."]
# 'tomorrow' takes no 'On': 'On tomorrow you have' is not English.
PLAN_LEADS_TOMORROW = ["{W} has {x}.", "{W} you have {x}.", "For {w} there's {x}."]
PLAN_NONE = ["Nothing is on the clock {w}.", "Nothing is time-bound {w}.", "{W} has nothing on the clock."]


def narrate_day(rng, plan, *, bullets=None):
    """A short prioritized answer to a one-day plan: what is on the clock (Brief items from mail, texts and notes beside
    the calendar), what is overdue, who is waiting, what else is open. Everything comes from the plan's lines."""
    word = plan.word
    w = word if word in ("today", "tomorrow") else word
    W = w[:1].upper() + w[1:]
    timed = plan.days[0][1] if plan.days else []
    bullets = plan.n_lines >= 6 if bullets is None else bullets
    parts = []
    if plan.empty:
        return rng.choice([f"Nothing is on the clock, waiting or open {w}.", f"Nothing is time-bound, waiting or open {w}."])
    if timed:
        phrases = [say_entry(e) for e in timed]
        if plan.more.get("day") and not r5():
            phrases.append(f"{plan.more['day']} more")
        if not bullets:
            tmpl = PLAN_LEADS_TODAY if w == "today" else PLAN_LEADS_TOMORROW if w == "tomorrow" else PLAN_LEADS_DAY
            parts.append(rng.choice(tmpl).format(x=join(phrases), W=W, w=w) if w != "today" else rng.choice(tmpl).format(x=join(phrases)))
    else:
        parts.append(rng.choice(PLAN_NONE).format(w=w, W=W))
    extra = []
    if plan.overdue:
        extra.append(("Overdue: " + say_over(plan.overdue, plan.more.get("overdue", 0)) + "."))
    if plan.waiting:
        extra.extend(say_wait(x) + "." for x in plan.waiting) if bullets else extra.append(" ".join(say_wait(x) + "." for x in plan.waiting))
    if plan.other:
        extra.append(other_sentence(rng, plan))
    if bullets:
        n_more = plan.more.get("day", 0)
        counted = f"{W} has {len(timed)} thing{'s' if len(timed) != 1 else ''} lined up:" if not n_more else \
            f"{W} has a full day, here are the first {len(timed)}:"
        lead = rng.choice([counted if timed else f"{W} has nothing on the clock, but there's more:",
                           f"Here's {w}:" if w in ("today", "tomorrow") else f"Here's {W}:"])
        rows = [f"- {say_entry(e)[:1].upper()}{say_entry(e)[1:]}" for e in timed]
        if n_more and not r5():
            rows.append(f"- And {n_more} more after that")
        rows += ["- " + x for x in extra]
        note = cut_note(rng, plan)
        return lead + "\n" + "\n".join(rows) + (("\n\n" + note) if note else "")
    if r5():
        return " ".join(x for x in parts + extra + [cut_note(rng, plan)] if x)
    return " ".join(parts + extra)


def narrate_today(rng, plan, now, mode="left"):
    """'what's left today' / 'anything tonight' from TODAY's plan: only what is not behind the clock. mode='tonight' keeps
    timed items from 5 PM on; an all-day item (a Brief to-do with only a date) is today's, never tonight's, and is
    named as due today. Overdue, who waits and the other open items follow in 'left' mode."""
    timed = plan.days[0][1] if plan.days else []
    ahead, behind, allday = [], [], []
    for e in timed:
        c = clock_of(e[0])
        if c is None:
            allday.append(e)
        elif c < now.time():
            behind.append(e)
        else:
            ahead.append(e)
    if mode == "tonight":
        late = [e for e in ahead if clock_of(e[0]) >= dt.time(17, 0)]
        ahead = late
    parts = []
    if plan.empty:
        word = "tonight" if mode == "tonight" else "today"
        return rng.choice([f"Nothing is on the clock, waiting or open {word}.", f"Nothing is time-bound, waiting or open {word}."])
    if ahead:
        phrases = [say_entry(e) for e in ahead]
        if plan.more.get("day") and not r5():
            phrases.append(f"{plan.more['day']} more")
        lead = rng.choice(["Tonight you have ", "This evening: ", "On for tonight: "]) if mode == "tonight" else \
            rng.choice(["Still ahead today: ", "Left today: ", "For the rest of today you have "])
        parts.append(lead + join(phrases) + ".")
    else:
        parts.append("Nothing is on the clock tonight." if mode == "tonight" else "Nothing timed is left today.")
    if allday:
        parts.append(rng.choice(["Also due today: ", "Today also has "]) + join(say_entry(e) for e in allday) + ".")
    if behind and rng.random() < 0.6:
        e = behind[0]
        parts.append(f"{say_entry(e)[:1].upper()}{say_entry(e)[1:]} is already behind you.")
    if mode == "left":
        if plan.overdue:
            parts.append("Overdue: " + say_over(plan.overdue) + ".")
        if plan.waiting:
            parts.append(" ".join(say_wait(x) + "." for x in plan.waiting))
        if plan.other:
            parts.append(other_sentence(rng, plan))
    if r5() and plan.more:
        parts.append(cut_note(rng, plan))
    return " ".join(p for p in parts if p)


def trim_day(entries, cap=3):
    """At most ``cap`` of a day's lines for a week answer: the mail / text / note items first (they are what the calendar
    does not show), then the earliest of the rest, in the plan's order. Returns (shown, how many were left out).
    Round 5 lists every item the plan returned."""
    if r5():
        return list(entries), 0
    if len(entries) <= cap:
        return list(entries), 0
    brief = [e for e in entries if e[2] in ("email", "text", "note")]
    keep = brief[:cap]
    keep += [e for e in entries if e not in brief][:cap - len(keep)]
    shown = [e for e in entries if e in keep]
    return shown, len(entries) - len(shown)


def narrate_range(rng, plan):
    """A week or weekend plan: each day that has something, then overdue, who waits, what else is open."""
    lines = []
    for title, entries in plan.days:
        shown, hidden = trim_day(entries)
        hidden += plan.more_by_day.get(title, 0)
        lines.append(f"- {title}: " + "; ".join(say_entry(e)[:1].upper() + say_entry(e)[1:] for e in shown)
                     + (f"; and {hidden} more" if hidden and not r5() else ""))
    tail = []
    if plan.overdue:
        tail.append("- Overdue: " + say_over(plan.overdue))
    if plan.waiting:
        tail.append("- " + " ".join(say_wait(x) + "." for x in plan.waiting))
    if plan.other:
        tail.append("- Also open: " + say_other(plan.other, with_day=False, more=plan.more.get("other", 0)))
    if plan.more.get("truncated") and not r5():
        tail.append("- and a few more that the list cut off")
    if not lines and not tail:
        return rng.choice([f"Nothing is scheduled, due or waiting {plan.word}.", f"{cap1(plan.word)} is clear: nothing scheduled, due or waiting."])
    lead = rng.choice([f"Here's {plan.word}:", f"{cap1(plan.word)} looks like this:", f"For {plan.word}:"])
    if not lines:
        lead = f"Nothing is on the calendar {plan.word}, but there's more:"
    note = cut_note(rng, plan)
    return lead + "\n" + "\n".join(lines + tail) + (("\n\n" + note) if note else "")


def brief_sentence(rng, b):
    """One apart-listed Brief item (from get_upcoming and the other schedule reads) as a clause."""
    who = f" from {b['who']}" if b.get("who") else ""
    kind = {"email": f"an email{who}", "text": f"a text{who}", "note": "a note"}[b["src"]]
    due = f" at {b['due']}" if b.get("due") else ""
    late = ", already overdue" if b.get("over") else ""
    day = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday", "Fri": "Friday", "Sat": "Saturday",
           "Sun": "Sunday"}[b["day"].split(",")[0]]
    return f"{kind} adds “{b['title']}” for {day}{due}{late}"


_POINTER = re.compile(r"Wisp's Brief also has (?P<what>[^;]+); get_my_day_plan\(")


def with_brief(rng, reply, res, *, only_days=None):
    """Append the dated Brief items a schedule read listed apart (one sentence), and what its pointer line says the Brief
    also holds (overdue items, people waiting) when it does."""
    items = brief_lines(res)
    if only_days is not None:
        items = [b for b in items if b["day"] in only_days]
    sep = "\n\n" if "\n- " in reply else " "
    add = []
    if items:
        clauses = [brief_sentence(rng, b) for b in items[:3]]
        add.append(rng.choice(["Separately, ", "Beyond the calendar, ", "Also, "]) + join(clauses) + ".")
    ptr = _POINTER.search(res)
    if ptr and rng.random() < 0.6:
        add.append(rng.choice([f"Your Brief also has {ptr['what']}.", f"Besides that, the Brief shows {ptr['what']}."]))
    return reply.rstrip() + (sep + " ".join(add) if add else "")


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


def maybe_seed(rng, w, now, day, p=0.3):
    """With probability p, one dated Brief item (mail, text or note) on ``day``: find_free_time lists it apart, never as busy."""
    if rng.random() < p:
        return seed_brief(rng, w, now, day, rng.choice(["mail", "text", "note"]))
    return None


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
            maybe_seed(rng, w, now, day)
            res, gaps = free_gaps(w, period, day_start=lo, day_end=hi)
            if (mode == "booked") == (not gaps) and len(gaps) <= 3 and (mode != "gaps" or len(gaps) >= 1):
                break
        else:
            dropped("free_part_of_day", k)
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
            steps = [[call], [Call("get_upcoming", period=period, calendar_only=True)], Final(with_brief(rng, reply, res))]
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
            steps = [[call], Final(with_brief(rng, reply, res))]
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
            maybe_seed(rng, w, now, day)
            res, gaps = free_gaps(w, period, minutes=mins)
            if yes and gaps and len(gaps) <= 3 or not yes and not gaps:
                break
        else:
            dropped("free_hour_on_date", k)
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
            steps = [[call], Final(with_brief(rng, reply, res))]
            note = "a duration on a date: find_free_time(minutes=N); a yes states the actual ranges"
        else:
            evs = sorted(day_items(w, day, reminders=False))
            if len(evs) >= 4:
                reply = f"No, nothing that long is open on {label}. You have {len(evs)} events:\n" + "\n".join("- " + bullet(i) for i in evs)
            else:
                body = join(phrase(i) for i in evs)
                reply = fin(rng, f"No, nothing that long is open on {label}. You have {body}.",
                            f"Not on {label}: there's no gap of {dur}. The day holds {body}.")
            steps = [[call], [Call("get_upcoming", period=period, calendar_only=True)], Final(with_brief(rng, reply, res))]
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
            maybe_seed(rng, w, now, day)
            res, gaps = free_gaps(w, period)
            if gaps and len(gaps) <= 3:
                break
        else:
            dropped("free_when", k)
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
        out.append(sc(prompt, now, w, [[Call("find_free_time", period=period)], Final(with_brief(rng, reply, res))],
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
            maybe_seed(rng, w, now, day)
            res, gaps = free_gaps(w, period, minutes=mins)
            if not gaps or len(gaps) > 3:
                continue
            t0 = at(day, h, m)
            inside = [g for g in gaps if gap_bounds(g, day)[0] <= t0 and t0 + D(minutes=mins) <= gap_bounds(g, day)[1]]
            if bool(inside) == want:
                break
        else:
            dropped("free_at_time", k)
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
        out.append(sc(prompt, now, w, [[call], Final(with_brief(rng, reply, res))],
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
            dropped("free_trip", k)
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


DROPPED: list = []         # (builder, what) for every scenario slot that could not be built: the family raises on any


def dropped(name, k, why="no world satisfied the plan"):
    DROPPED.append((name, k, why))


CAL_WORDS = re.compile(r"calendar|schedule|booked|events?|appointments?|meetings?", re.I)


def decorate(rng, now, w, day, *, over=0, wait=0, und=0, brief=(), brief_later=(), via_mail=False):
    """Make ``w`` a Brief-seeded world for ``day``: undated and overdue reminders, people waiting, dated Brief items on
    ``day`` (kinds in ``brief``) and on a later day (``brief_later``). Returns the seeded BriefItems."""
    tidy_sources(w)
    fill_plan_extras(rng, now, w, over, 0, und)
    used, items = set(), []
    for kind in brief:
        it = seed_brief(rng, w, now, day, kind, used=used)
        if it:
            items.append(it)
    for kind in brief_later:
        it = seed_brief(rng, w, now, day + D(days=rng.randint(1, 3)), kind, used=used)
        if it:
            items.append(it)
    seed_waiting(rng, w, now, wait, via_mail=via_mail)
    return items


def day_plan(w, *args, **kw):
    return Plan(w.t_get_my_day_plan(*args, **kw))


def left_today(rng, out):
    """'what's left today': get_my_day_plan reads today's calendar, reminders and the Brief's mail / text / note items;
    only what is not behind the clock is left, reminders and a due-tonight homework email included."""
    plans = [  # (hour, ahead events, earlier events, ahead reminders, earlier reminders, brief kinds, overdue, waiting)
        (9, 3, 0, 1, 0, ("mail",), 0, 0), (14, 2, 2, 0, 0, (), 1, 1), (16, 0, 2, 0, 0, ("text",), 0, 0),
        (11, 1, 1, 1, 0, ("note",), 0, 0), (19, 0, 1, 1, 1, ("mail",), 0, 1), (13, 4, 1, 1, 0, ("text", "mail"), 1, 0),
        (17, 1, 2, 0, 1, (), 0, 0), (21, 0, 0, 0, 0, (), 0, 0), (10, 2, 0, 2, 0, ("mail", "note"), 0, 1),
        (12, 3, 2, 1, 0, ("text",), 2, 1), (8, 1, 0, 1, 0, ("text", "note"), 1, 0), (15, 0, 0, 0, 0, ("mail",), 0, 0),
    ]
    stems = [
        "anything i need to handle for the rest of today", "anything i have to do before bed", "anything left i need to finish today",
        "what's on my to-do today", "anything still coming up today", "what's coming up the rest of today",
        "anything i need to get done before tonight", "what's on my list for the rest of today", "anything else due today",
        "what else is planned for today", "what is still going on today", "anything i should still know today",
        "what's up for the rest of today", "anything coming up later today", "what's planned for the rest of today",
        "anything due before midnight", "what am i missing today", "my plans for the rest of today?",
        "anything i need to do this afternoon", "what's left on my agenda today",
    ]
    for k, (hour, na, nb, ra, rb, bk, ov, wt) in enumerate(plans):
        for _try in range(60):
            now, w = mk(rng, hour=hour)
            back, ahead, r_ahead, r_back = make_today(rng, now, w, na, nb, rem_ahead=ra, rem_back=rb)
            if not (len(ahead) == na and len(r_ahead) == ra and len(back) == nb):
                continue
            decorate(rng, now, w, now.date(), over=ov, wait=wt, brief=bk)
            if len(w.brief) == len(bk):
                break
        else:
            dropped("left_today", k)
            continue
        plan = day_plan(w)
        reply = narrate_today(rng, plan, now, "left")
        call = Call("get_my_day_plan") if k % 3 else Call("get_my_day_plan", day="today")
        out.append(sc(uniq(rng, *stems, plan=True), now, w, [[call], Final(reply)],
                      "what's left today: get_my_day_plan (calendar, reminders, and mail / text / note items from the Brief); only what is ahead of the clock"))


def tonight(rng, out):
    """'anything tonight': the plan's evening items, a 6 PM reminder and a homework due at 11:59 PM included; an item with
    only a date is today's, not tonight's. Earlier items are not tonight."""
    plans = [  # (hour, ahead events, earlier events, ahead reminders, earlier reminders, brief kinds)
        (14, 0, 1, 1, 0, ("mail",)), (15, 1, 0, 1, 0, ()), (11, 0, 0, 1, 0, ("text",)), (16, 2, 1, 0, 0, ("mail",)),
        (13, 1, 1, 2, 0, ("note",)), (17, 0, 1, 0, 0, ()), (12, 0, 0, 0, 0, ("mail", "text")), (18, 1, 2, 1, 1, ("text",)),
        (10, 3, 0, 1, 0, ()), (20, 0, 1, 0, 0, ("mail",)),
    ]
    stems = [
        "what's happening tonight", "what's planned for tonight", "anything due tonight", "anything coming up tonight",
        "what's going on tonight", "anything i need to handle tonight", "my plans for tonight?", "what's up for tonight",
        "what is on tonight", "anything i need to do tonight", "what am i missing tonight", "what's on for tonight",
        "anything planned this evening", "what's going on this evening",
    ]
    for k, (hour, na, nb, ra, rb, bk) in enumerate(plans):
        for _try in range(60):
            now, w = mk(rng, hour=hour)
            back, ahead, r_ahead, r_back = make_today(rng, now, w, na, nb, rem_ahead=ra, rem_back=rb, tonight=True)
            if not (len(ahead) == na and len(r_ahead) == ra and len(back) == nb):
                continue
            decorate(rng, now, w, now.date(), brief=bk)
            if len(w.brief) == len(bk):
                break
        else:
            dropped("tonight", k)
            continue
        plan = day_plan(w)
        reply = narrate_today(rng, plan, now, "tonight")
        out.append(sc(uniq(rng, *stems, plan=True), now, w, [[Call("get_my_day_plan")], Final(reply)],
                      "tonight: get_my_day_plan; the evening's events, reminders and Brief items with times; a date-only item is today's, not tonight's"))


def left_plan(rng, out):
    """'what do i still have to do today' through get_my_day_plan: time-bound items still ahead, then the rest."""
    plans = [(10, 2, 1, 1, 0, ("mail",)), (13, 1, 1, 0, 1, ("text", "note")), (15, 2, 0, 1, 0, ()), (18, 0, 1, 0, 0, ("mail",))]
    stems = ["anything i need to get done today", "anything i need to handle today", "what's on my to-do today",
             "anything i have to do before tonight"]
    for k, (hour, na, ra, n_over, n_wait, bk) in enumerate(plans):
        for _try in range(60):
            now, w = mk(rng, hour=hour)
            back, ahead, r_ahead, _rb = make_today(rng, now, w, na, 1, rem_ahead=ra, rem_back=0)
            if not (len(ahead) == na and len(r_ahead) == ra):
                continue
            decorate(rng, now, w, now.date(), over=n_over, wait=n_wait + 1, und=1 if k % 2 == 0 else 0, brief=bk)
            if len(w.brief) == len(bk):
                break
        else:
            dropped("left_plan", k)
            continue
        reply = narrate_day(rng, day_plan(w))
        out.append(sc(uniq(rng, stems[k], plan=True), now, w, [[Call("get_my_day_plan")], Final(reply)],
                      "what's left to do today: get_my_day_plan; the still-ahead items, overdue, who waits, to-dos"))


# ================================================================== C. a named day
def named_day(rng, out):
    """'what's on friday' / 'the 14th' / 'next tuesday' / 'nov 9': that date only. A time-scoped question reads the plan
    (calendar, reminders and the Brief's mail / text / note items); a question that names the calendar reads
    get_upcoming, and the final adds the dated Brief items that result lists apart."""
    sizes = [2, 3, 1, 0, 4, 2, 5, 3, 1, 6, 2, 0, 3, 4, 1, 2, 3, 2, 1, 3]
    kinds = ["wd", "wd", "ordinal", "wd", "date", "next", "wd", "date", "ordinal", "wd", "next", "date", "wd", "ordinal", "wd",
             "date", "wd", "next", "ordinal", "wd"]
    stems = {
        "wd": ["what's planned for {x}", "what's going on {x}", "what's happening {x}", "anything i need to handle {x}",
               "anything coming up {x}", "what's on {x}", "my plans for {x}?", "anything due {x}", "what's up for {x}",
               "what is planned {x}", "anything i should know {x}", "what's on for {x}", "anything i have to do {x}",
               "is there anything on the calendar {x}", "give me {x}'s schedule", "what events do i have {x}",
               "what's on my calendar {x}", "which meetings do i have {x}", "calendar check for {x}"],
        "next": ["anything i need to handle {x}", "anything due {x}", "anything coming up {x}", "my plans for {x}?",
                 "anything i have to do {x}", "anything planned {x}", "anything booked {x}", "what's scheduled {x}",
                 "what's on my calendar {x}", "which events do i have {x}"],
        "date": ["anything i need to handle on {x}", "anything due on {x}", "anything coming up on {x}", "my plans for {x}?",
                 "anything i have to do on {x}", "anything planned for {x}", "what's on my calendar for {x}",
                 "list the events i have on {x}", "anything booked on {x}"],
        "ordinal": ["anything i need to handle on {x}", "anything due on {x}", "anything coming up on {x}", "anything planned for {x}",
                    "anything i have to do on {x}", "my plans for {x}?", "what's on the calendar for {x}",
                    "any events on {x}", "what appointments do i have on {x}"],
    }
    brief_plan = ["text", "mail", None, "note", "mail", "text", None, "mail", "note", "text", "mail", None, "text", "note", "mail",
                  None, "text", "mail", "note", "text"]
    for k, (n, kind) in enumerate(zip(sizes, kinds)):
        for _try in range(80):
            now, w = mk(rng, hour=rng.choice([8, 11, 14, 17, 20, 22]))
            if kind == "wd":
                day = pick_day(rng, now, 2, 6)
                said_, period = f"{wspell(rng, day)}", WDN[day.weekday()]
            elif kind == "next":
                day = now.date() + D(days=rng.randint(4, 8))
                wdn = WDN[day.weekday()]
                said_, period = f"next {wdn}", f"next {wdn}"
            elif kind == "ordinal":
                day = now.date() + D(days=rng.randint(3, 16))
                said_, period = f"the {ordinal(day.day)}", f"the {ordinal(day.day)}"
            else:
                day = now.date() + D(days=rng.randint(3, 18))
                said_, period = date_period(rng, day, now)[0], None
                period = said_.lower()
            try:
                if span_day(w, period) != day:
                    continue
            except Exception:       # noqa: BLE001
                continue
            break
        else:
            dropped("named_day", k, "no day resolved")
            continue
        n_ev = max(n - (1 if n >= 3 and k % 3 == 0 else 0), 0)
        put_events(rng, w, day, n_ev, lo=8, hi=20)
        for r in rng.sample(REM_TITLES, n - n_ev):
            add_rem(w, r, at(day, rng.choice([9, 12, 17, 20])))
        for off in (-1, 1):            # neighbouring days that must not appear in the answer
            if rng.random() < 0.6:
                put_events(rng, w, day + D(days=off), 1, lo=9, hi=18)
        bk = brief_plan[k]
        decorate(rng, now, w, day, brief=(bk,) if bk else (), wait=1 if k % 4 == 1 else 0, over=1 if k % 5 == 2 else 0)
        stem_pool = [x.format(x=said_) for x in stems[kind]]
        use_cal = k % 3 == 1
        prompt = uniq(rng, *stem_pool, plan=not use_cal)
        label = long_date(day)
        if use_cal:
            res = w.t_get_upcoming(period=period)
            items = day_items(w, day)
            if not items:
                reply = fin(rng, f"Nothing is on your calendar for {label}, and no reminders are due.",
                            f"{label} is clear: no events and no reminders.")
            elif len(items) >= 4:
                reply = f"{label} has {counts(items)}:\n" + "\n".join("- " + bullet(i, True) for i in sorted(items))
            else:
                head = fin(rng, f"On {label} you have", f"{label} has", f"For {label} there's")
                reply = agenda(items, head, loc=True)
            reply = with_brief(rng, reply, res)
            out.append(sc(prompt, now, w, [[Call("get_upcoming", period=period)], Final(reply)],
                          "a named day, calendar asked: get_upcoming; every item with its time and title, then the dated Brief items it lists apart"))
        else:
            plan = day_plan(w, period)
            if plan.error:
                raise ValueError(plan.error)
            out.append(sc(prompt, now, w, [[Call("get_my_day_plan", day=period)], Final(narrate_day(rng, plan))],
                          "a named day: get_my_day_plan(day); calendar, reminders and the mail / text / note items from the Brief, an empty day said plainly"))


# ================================================================== D. the whole day
def fill_plan_extras(rng, now, w, n_over, n_wait, n_undated):
    """Overdue and undated reminders (and, for n_wait > 0 in a world that has not tidied its sources, unread texts)."""
    titles = rng.sample(REM_TITLES, n_over + n_undated + 6)
    taken = {r.title for r in w.reminders}
    titles = [t for t in titles if t not in taken]
    for i in range(n_over):
        add_rem(w, titles[i], at(now.date() - D(days=rng.randint(1, 6)), rng.choice([9, 12, 17, 20])))
    for j in range(n_undated):
        w.reminders.append(Reminder(titles[n_over + j], None))
    for who, text in rng.sample(PEOPLE_WAIT, n_wait):
        w.messages.append(Msg(who, who, text, now - D(hours=rng.randint(2, 30)), True))


def whole_day(rng, out):
    """'how does my day look': timed items (the Brief's mail / text / note items beside the calendar), overdue, who is
    waiting and the open to-dos, in a few sentences or a short list."""
    plans = [  # (hour, day, ahead events, reminders ahead, overdue, waiting, undated, brief kinds, brief later)
        (8, "today", 3, 0, 1, 1, 0, ("text",), ()), (9, "today", 2, 1, 2, 0, 1, ("mail",), ("note",)),
        (13, "today", 1, 1, 0, 1, 1, ("note", "mail"), ()), (7, "today", 4, 1, 2, 1, 1, ("mail", "text"), ()),
        (11, "today", 0, 0, 2, 0, 0, (), ("text",)), (15, "today", 2, 0, 0, 2, 0, ("mail",), ()),
        (10, "today", 0, 1, 0, 0, 0, ("text",), ("mail",)), (19, "today", 1, 0, 3, 1, 0, (), ()),
        (21, "tomorrow", 3, 1, 1, 0, 1, ("text", "mail"), ()), (22, "tomorrow", 2, 0, 0, 1, 0, ("note",), ()),
        (20, "tomorrow", 5, 1, 0, 0, 0, ("mail",), ("text",)), (18, "tomorrow", 0, 0, 1, 0, 1, ("text",), ()),
        (12, "today", 2, 2, 4, 1, 2, ("mail", "text", "note"), ()), (9, "tomorrow", 1, 1, 0, 2, 0, ("mail",), ()),
        (16, "today", 3, 0, 0, 0, 0, ("note",), ()), (8, "today", 1, 0, 0, 1, 0, ("mail", "text"), ("mail",)),
        (20, "tomorrow", 0, 0, 0, 1, 0, ("text", "note"), ()), (12, "today", 0, 0, 1, 0, 0, ("mail", "text"), ()),
    ]
    stems = {
        "today": ["how's my day looking", "walk me through today", "what's the shape of my day", "so what's today like for me",
                  "give me the rundown for today", "what does today look like", "how does today shape up", "okay, my day, go",
                  "what is my day made of", "give me the picture for today", "my day, in a nutshell?",
                  "what am i dealing with today", "sum up my day for me", "what's on my plate today", "anything i should know about today"],
        "tomorrow": ["how's tomorrow shaping up", "walk me through tomorrow", "give me the lay of the land for tomorrow",
                     "what does tomorrow have in store", "tomorrow, what am i dealing with", "sum up tomorrow for me",
                     "what's tomorrow made of", "what do i have to do tomorrow"],
    }
    for k, (hour, day, ne, nr, n_over, n_wait, n_und, bk, bl) in enumerate(plans):
        for _try in range(60):
            now, w = mk(rng, hour=hour)
            target = now.date() + D(days=1 if day == "tomorrow" else 0)
            if day == "tomorrow":
                evs = put_events(rng, w, target, ne, lo=8, hi=20)
                for t in rng.sample(REM_TITLES, nr):
                    add_rem(w, t, at(target, rng.choice([9, 12, 17, 20])))
                ok = len(evs) == ne
            else:
                back, ahead, r_ahead, _rb = make_today(rng, now, w, ne, 1 if hour > 11 and k % 3 == 0 else 0, rem_ahead=nr)
                ok = len(ahead) == ne and len(r_ahead) == nr
            if not ok:
                continue
            decorate(rng, now, w, target, over=n_over, wait=n_wait, und=n_und, brief=bk, brief_later=bl)
            if len(w.brief) == len(bk) + len(bl):
                break
        else:
            dropped("whole_day", k)
            continue
        plan = day_plan(w, day) if day == "tomorrow" else day_plan(w)
        call = Call("get_my_day_plan") if day == "today" else Call("get_my_day_plan", day="tomorrow")
        out.append(sc(uniq(rng, *stems[day]), now, w, [[call], Final(narrate_day(rng, plan))],
                      "the whole day: get_my_day_plan; timed items (mail / text / note items from the Brief beside the calendar), overdue, who is waiting, open to-dos"))


def week_plans(rng, out):
    """'anything i need to do this week' / 'this weekend' / 'next week': get_my_day_plan with a range; each day that has
    something, then overdue, who waits and the rest. The Brief's mail / text / note items sit beside the calendar."""
    plans = [  # (range, weekday of 'now' wanted or None, items per day, brief kinds, waiting, overdue)
        ("this_week", 0, 2, ("mail", "text"), 1, 0), ("this_week", 1, 1, ("mail", "note"), 0, 1), ("this_week", 2, 2, ("text", "mail", "note"), 1, 1),
        ("this_week", 0, 1, ("text",), 0, 0), ("this weekend", 2, 1, ("text", "note"), 1, 0), ("this weekend", 3, 2, ("text",), 0, 1),
        ("next week", 4, 1, ("mail", "text"), 1, 0), ("next week", 5, 1, ("note",), 0, 0), ("this_week", 1, 3, ("mail", "text"), 2, 2),
        ("this weekend", 0, 1, ("text", "text", "mail"), 0, 0), ("next week", 1, 2, ("mail", "text", "note"), 1, 1),
        ("this_week", 3, 1, ("mail",), 1, 0),
    ]
    stems = {
        "this_week": ["anything i need to handle this week", "anything due this week", "anything coming up this week",
                      "what do i have planned this week", "what do i have coming up this week", "what's planned for this week",
                      "what's up for this week", "my plans for this week?", "what is happening this week",
                      "what's on my agenda this week", "anything i should know this week", "what am i missing this week",
                      "anything i have to get done this week", "what's on for this week"],
        "this weekend": ["what's planned for this weekend", "anything coming up this weekend", "my plans for this weekend?",
                         "what do i have planned this weekend", "anything i need to handle this weekend",
                         "what's up for this weekend", "anything due this weekend", "anything i should know this weekend",
                         "what is planned this weekend"],
        "next week": ["what's coming up next week", "anything i need to handle next week", "anything due next week",
                      "what do i have planned next week", "my plans for next week?", "what's planned for next week",
                      "anything i should prep for next week", "what is happening next week", "anything coming up next week"],
    }
    for k, (rg, wd_now, per_day, bk, wt, ov) in enumerate(plans):
        for _try in range(80):
            now, w = mk(rng, weekday=wd_now, hour=rng.choice([8, 10, 13, 17]))
            if rg == "this weekend" and now.weekday() >= 5:
                continue
            start, end, _lab = w._span("this week" if rg == "this_week" else rg, True)
            days = []
            d0 = max(start.date(), now.date())
            while d0 < end.date() and len(days) < 7:
                days.append(d0)
                d0 += D(days=1)
            if len(days) < 2:
                continue
            pick = rng.sample(days, min(len(days), rng.choice([2, 3])))
            for d in pick:
                put_events(rng, w, d, per_day, lo=9, hi=19, min_start=now + D(minutes=90))
            decorate(rng, now, w, pick[0], over=ov, wait=wt)
            used = {i.title for i in w.brief}
            for kind in bk:
                seed_brief(rng, w, now, rng.choice(days), kind, used=used)
            if len(w.brief) == len(bk):
                break
        else:
            dropped("week_plans", k)
            continue
        plan = day_plan(w, rg)
        if plan.error:
            raise ValueError(plan.error)
        out.append(sc(uniq(rng, *stems[rg], plan=True), now, w, [[Call("get_my_day_plan", day=rg)], Final(narrate_range(rng, plan))],
                      "a week or weekend: get_my_day_plan with a range; each day's events, reminders and mail / text / note items, then overdue, who waits and the rest"))


def calendar_with_brief(rng, out):
    """The person names the calendar ('what's on my calendar tomorrow', 'which events do i have friday'): get_upcoming, and
    the final also names the dated mail / text / note items the result lists apart."""
    stems = ["which events do i have {x}", "anything on my calendar {x}?", "check my calendar for {x}",
             "what appointments do i have {x}", "calendar for {x}, what's in it", "any meetings on my calendar {x}",
             "show me the calendar entries for {x}", "what's booked on my calendar {x}"]
    plans = [("tomorrow", 2, "mail"), ("friday", 1, "text"), ("tomorrow", 0, "text"), ("saturday", 2, "note"), ("tomorrow", 1, "mail"),
             ("sunday", 0, "text"), ("monday", 2, "mail"), ("friday", 3, "note"),
             # a day with no event and only reminders: the "calendar is clear, the only thing is the reminder" answer
             ("tomorrow", 0, "mail", 1), ("wednesday", 0, "text", 1), ("tomorrow", 0, "text", 2), ("saturday", 0, "note", 2),
             ("thursday", 0, "mail", 1), ("friday", 0, "text", 2)]
    for k, (when, n, bk, *rest) in enumerate(plans):
        n_rem = rest[0] if rest else 0
        for _try in range(60):
            now, w = mk(rng, hour=rng.choice([8, 12, 17]))
            day = now.date() + D(days=1) if when == "tomorrow" else None
            if day is None:
                day = next((now.date() + D(days=i) for i in range(2, 8) if WDN[(now.date() + D(days=i)).weekday()] == when), None)
            if day is None:
                continue
            put_events(rng, w, day, n, lo=9, hi=19)
            decorate(rng, now, w, day, brief=(bk,))
            if n_rem:
                taken = {r.title for r in w.reminders}
                for title in [t for t in rng.sample(REM_TITLES, 10) if t not in taken][:n_rem]:
                    add_rem(w, title, at(day, rng.choice([9, 12, 15, 17, 20])))
            if len(w.brief) == 1 and (not n_rem or (not [e for e in w.events if e.start.date() == day]
                                                    and len([r for r in w.reminders if r.due and r.due.date() == day]) == n_rem)):
                break
        else:
            dropped("calendar_with_brief", k)
            continue
        when = period_for(w, day) if when != "tomorrow" else when      # a bare weekday on that same weekday is today
        assert span_day(w, when) == day, (when, day)
        res = w.t_get_upcoming(period=when)
        items = day_items(w, day)
        reply = with_brief(rng, calendar_reply(rng, items, day, now), res)
        out.append(sc(uniq(rng, *[x.format(x=when) for x in stems], plan=False), now, w, [[Call("get_upcoming", period=when)], Final(reply)],
                      "the calendar asked for: get_upcoming; its items, then the dated mail / text / note items the result lists apart"))


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
            dropped("the_past", k)
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
ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d")
MIN_SCENARIOS = 121                  # the frozen build's day_answers_r4


def fam_day_answers_r4(rng):
    out: list = []
    _FIRST.clear()
    USED_PROMPTS.clear()
    DROPPED.clear()
    for fn in (free_part_of_day, free_hour_on_date, free_when, free_at_time, free_holiday, free_trip,
               left_today, tonight, left_plan, named_day, whole_day, week_plans, calendar_with_brief, the_past):
        fn(rng, out)
    if DROPPED:
        raise RuntimeError(f"day_answers_r4 lost scenarios: {DROPPED[:6]}")
    if len(out) < MIN_SCENARIOS:
        raise RuntimeError(f"day_answers_r4 has {len(out)} scenarios, the frozen build had {MIN_SCENARIOS}")
    for sc_ in out:
        for st in sc_.steps:
            if isinstance(st, Final) and ISO.search(st.text):
                raise RuntimeError(f"ISO timestamp in a final: {st.text[:200]!r}")
    return out


FAMILIES_R4 = {FAMILY: fam_day_answers_r4}
