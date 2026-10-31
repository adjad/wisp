"""Round-2 write families 1-3 (own file; the integrator registers FAMILIES_R2).

    act_dont_ask      the user gave enough: call the tool at once (time words resolved
                      to the next occurrence), plus ~15% true ambiguity -> one question
    finish_the_write  read, then make the write call in the same turn
    stub_then_call    the needed tool starts as a name-only stub (or is not offered):
                      load -> real call with full arguments -> final from the result

Every scenario is dry-run through harness.run_scenario while the family is generated
(`_attempt`): a candidate the host would misroute, reword, or refuse is dropped and
another seed is tried, so a family never hands the build a scenario that cannot run.
Dropped candidates are listed in DROPPED. Prompts avoid every toolbench / eval prompt
(leak.py is consulted too) and the Sam Rivera world is the only world used.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import random
import re

import harness as H
from harness import Call, Final
from replies import clock, day_word, join
from scen import Scenario, setup
from world import Email, Event, Msg, Note, Reminder

D = dt.timedelta
DROPPED: list[tuple[str, str, str]] = []     # (family, prompt, why) of dropped candidates

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ALIASES = {"monday": ["monday", "mon"], "tuesday": ["tuesday", "tues"], "wednesday": ["wednesday", "wed"],
           "thursday": ["thursday", "thurs"], "friday": ["friday", "fri"], "saturday": ["saturday", "sat"],
           "sunday": ["sunday"]}

EXTRA_CONTACTS = {
    "Aisha Karim": ("(971) 555-0121", "aisha.karim@example.com"),
    "Ben Ortiz": ("(971) 555-0147", ""),
    "Jordan Ellis": ("(503) 555-0139", ""),
    "Hana Kowalczyk": ("", "hkowalczyk@linden.edu"),
    "Dana Whitfield": ("", "dwhitfield@linden.edu"),
}


# =============================================================================
# plumbing
# =============================================================================
def _leaks():
    global _LEAK
    try:
        return _LEAK
    except NameError:
        import leak
        _LEAK = leak.LeakIndex()
        return _LEAK


def _text_of(sc) -> str:
    parts = [sc.prompt]
    for st in sc.steps:
        if isinstance(st, Final):
            parts.append(st.text)
        else:
            for c in (st.calls if isinstance(st, H.Bad) else st):
                parts.append(json.dumps(c.args, ensure_ascii=False))
    return "\n".join(parts)


def _check(sc, need_load=False):
    """(ok, why): dry-run `sc` the way build.py will and require a clean, scripted turn."""
    import build
    import leak
    leaked, score, q = _leaks().leaks(sc.prompt)
    if leaked:
        return False, f"leak {q['id']} {score:.2f}"
    hits = leak.fact_hits(_text_of(sc))
    if hits:
        return False, f"toolbench fact {hits[:2]}"
    approve = getattr(sc, "approve", None)
    try:
        policy, events, final = H.run_scenario(sc.prompt, copy.deepcopy(sc.steps), sc.world, sc.when,
                                               profile=sc.profile, approve=approve)
    except Exception as exc:  # noqa: BLE001
        return False, f"harness {exc!r:.120}"
    errs = [e for e in events if e.get("type") == "error"]
    if policy.idx != len(policy.steps) or errs or len(policy.requests) != len(policy.responses):
        return False, f"incomplete {policy.idx}/{len(policy.steps)} errs={errs[:1]} final={final!r:.100}"
    called = build._calls(policy.responses, ("call", "bad"))
    wrote = any(H.is_write(n, a) for n, a in called)
    want = sc.steps[-1].text if isinstance(sc.steps[-1], Final) else None
    if want is not None and (final or "").strip() != want.strip() and not wrote:
        return False, f"host changed final: {final!r:.100}"
    sent = build._calls(policy.responses, ("call",))
    bad_sent = build._calls(policy.responses, ("bad",))
    ran = [(n, a) for n, a in H.CURRENT["calls"] if (n, a) not in bad_sent]
    sent = _norm_sent(sent, ran, sc.when)
    problem = build._match_ran(sent, ran)
    if problem:
        return False, f"host rewrote args: {problem}"
    # a write the script makes must take effect unless its card is denied
    approve_l = approve if isinstance(approve, list) else ([approve] * 8 if isinstance(approve, bool) else [])
    denied = any(a is False for a in approve_l)
    for n, a in sent:
        if H.is_write(n, a) and not denied and not any(t == n for t, _ in sc.world.effects):
            return False, f"write {n} had no effect"
    results = " \n".join(str(e.get("result")) for e in events if e.get("type") == "tool_result")
    for need in getattr(sc, "must_see", []):
        if need.lower() not in results.lower():
            return False, f"tool results lack {need!r}"
    if need_load and not any(r["kind"] == "load" for r in policy.responses):
        return False, "no stub/load step"
    return True, ""


def _norm_sent(sent, ran, now):
    """The host pins add_calendar_event.when_iso to the Mac's UTC offset and turns
    schedule_send.when (the user's own words) into an absolute time before the card;
    build.HOST_BOUND does not know either yet, so accept exactly those rewrites here."""
    from service.tools.assistant_tools import bind_calendar_local_start
    out = []
    for n, a in sent:
        a = dict(a)
        if n == "add_calendar_event" and "when_iso" in a:
            try:
                a["when_iso"] = bind_calendar_local_start(a["when_iso"])
            except Exception:  # noqa: BLE001
                pass
        if n == "schedule_send" and "when" in a:
            ran_when = next((r[1].get("when") for r in ran if r[0] == "schedule_send"), None)
            if ran_when:
                try:
                    want = at(now, a["when"])
                    got = dt.datetime.fromisoformat(str(ran_when)).replace(tzinfo=None)
                    if abs((got - want).total_seconds()) < 60:
                        a["when"] = ran_when
                except Exception:  # noqa: BLE001
                    pass
        out.append((n, a))
    return out


_SEEN: set[str] = set()
_OPEN: dict[str, int] = {}        # first word of every committed final -> count (voice: no opener above ~8%)
OPEN_CAP = 5


def _okey(text: str) -> str:
    m = re.match(r"[A-Za-z]+", text.strip().lstrip("“\"'"))
    return m.group(0).lower() if m else ""


def _pick(rng, opts):
    """rng.choice over final wordings, skipping openers already used OPEN_CAP times in this build.
    Deterministic for a given (seed, committed state), so the dry-run and the kept scenario agree."""
    opts = list(opts)
    ok = [o for o in opts if _OPEN.get(_okey(o), 0) < OPEN_CAP]
    return rng.choice(ok or opts)


def _attempt(make, rng, family, *, tries=5, need_load=False):
    """make(rng) -> Scenario | None. Returns a fresh, dry-run-clean scenario or None."""
    for _ in range(tries):
        seed = rng.random()
        sc = make(random.Random(seed))
        if sc is None:
            continue
        if sc.prompt.strip().lower() in _SEEN:
            continue
        ok, why = _check(sc, need_load)
        if ok:
            _SEEN.add(sc.prompt.strip().lower())
            kept = make(random.Random(seed))
            if kept.steps and isinstance(kept.steps[-1], Final):
                k = _okey(kept.steps[-1].text)
                _OPEN[k] = _OPEN.get(k, 0) + 1
            return kept
        DROPPED.append((family, sc.prompt, why))
    return None


def _fill(out, quota, makers, rng, family, *, need_load=False, tries=4):
    """Draw `quota` scenarios from `makers` (callables rng -> Scenario|None). Each maker is tried once before any
    is tried again, so a family does not repeat a topic while an untried one is left."""
    got, order = 0, list(makers)
    rng.shuffle(order)
    for _round in range(3):
        for mkr in order:
            if got >= quota:
                return got
            sc = _attempt(mkr, rng, family, tries=tries, need_load=need_load)
            if sc is not None:
                out.append(sc)
                got += 1
    return got


def mk(family, prompt, now, w, steps, notes, *, approve=None, cards=None, profile=None, must_see=()):
    kw = {} if profile is None else {"profile": profile}
    sc = Scenario(family, prompt, now, w, steps, notes, **kw)
    sc.approve = approve
    sc.expect_cards = list(cards or [])
    sc.must_see = list(must_see)
    return sc


def style(rng, text):
    """Real typing: mostly lowercase, sometimes a typo or no punctuation."""
    r = rng.random()
    if r < 0.55:
        text = text[:1].lower() + text[1:] if rng.random() < 0.5 else text.lower()
    typos = {"tomorrow": ["tmrw", "tomorow", "tommorow"], "reminder": ["remindr", "reminer"],
             "please": ["pls", "plz"], "thursday": ["thurs"], "tonight": ["tonite"], "message": ["mesage"],
             "appointment": ["apointment", "appt"], "between": ["btwn"], "morning": ["mornin"]}
    if rng.random() < 0.2:
        for k, v in typos.items():
            if k in text.lower() and rng.random() < 0.7:
                low = text.lower()
                at = low.index(k)
                text = text[:at] + rng.choice(v) + text[at + len(k):]
                break
    if text.endswith(".") and rng.random() < 0.6:
        text = text[:-1]
    return text


def base_world(rng, **kw):
    now, w = setup(rng, **kw)
    w.contacts.update(EXTRA_CONTACTS)
    w.overrides["lookup_contact"] = _lookup(w)
    return now, w


def _lookup(w):
    def fn(args):
        name = args.get("name", "")
        found = w.find_contacts(name)
        if not found:
            return (f"(No saved contact matches {name!r}. Ask the user for the number or email "
                    "— do not guess one.)")
        if len(found) > 1:
            listed = "\n".join(f"- {n}: {(h or [''])[0]}" for n, h in found[:8])
            return (f"Several contacts match {name!r} — ask the user which one they mean "
                    f"before sending anything:\n{listed}")
        who, handles = found[0]
        out = f"{who}: {handles[0]}"
        if len(handles) > 1:
            out += f" (also on {', '.join(handles[1:])})"
        return out
    return fn


def at(now, when):
    """Naive local datetime a spoken phrase means, by Wisp's own resolver."""
    from service.tools.timeranges import resolve_when
    d, _ = resolve_when(when, now=now)
    return d.replace(tzinfo=None)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M")


def dphr(d, now):
    w = day_word(d.date(), now)
    if w == "today" and d.hour >= 17:
        w = "tonight"
    return f"{w} at {clock(d)}"


def bare(d, now):
    """dphr without the leading 'on ', for use right after 'to' / 'for' ("moved X to Saturday at 12:30 PM")."""
    s = dphr(d, now)
    return s[3:] if s.startswith("on ") else s


def wd_at(now, wd, tail):
    """The bare-weekday rule (PLAN.md): a weekday word means its next occurrence strictly AFTER today, so
    'mon at 6pm' said on a Monday is next Monday. The time of day is the product resolver's reading of
    '<weekday> <tail>' ('at 9am', 'morning', ...)."""
    probe = at(now, f"{wd} {tail}")
    day = now.date() + D(days=(WEEKDAYS.index(wd) - now.weekday()) % 7 or 7)
    return dt.datetime.combine(day, probe.time())


def second(text):
    """Echo the user's first-person wording back to them: 'back up my laptop' -> 'back up your laptop'."""
    for a, b in ((r"\bmy\b", "your"), (r"\bMy\b", "Your"), (r"\bmine\b", "yours"), (r"\bI'm\b", "you're"),
                 (r"\bI'll\b", "you'll"), (r"\bI've\b", "you've"), (r"\bI\b", "you"), (r"\bme\b", "you")):
        text = re.sub(a, b, text)
    return text


def cap(s):
    return s[:1].upper() + s[1:]


def pick(rng, *xs):
    return rng.choice(xs)


def clk(h, m, st):
    """A clock as people type it: 5pm, 5:30 pm, 17:30."""
    h12 = h % 12 or 12
    ap = "am" if h < 12 else "pm"
    if st == 0:
        return f"{h12}{ap}" if m == 0 else f"{h12}:{m:02d}{ap}"
    if st == 1:
        return f"{h12}:{m:02d} {ap}" if m else f"{h12} {ap}"
    return f"{h:02d}:{m:02d}"


def pname(rng, wd):
    return rng.choice(ALIASES[wd])


# =============================================================================
# FAMILY 1: act_dont_ask
# =============================================================================
REM_TASKS = [
    "return the borrowed drill to Ben", "charge the camera", "print the boarding pass", "buy a birthday card for Carmen",
    "take my vitamins", "feed the neighbor's cat", "bring the tripod to class", "cancel the free trial",
    "pack my gym bag", "pay the parking ticket", "drop the package off at the post office", "back up my laptop",
    "reserve a study room", "sign the lease addendum", "refill the printer paper", "check the flight status",
    "take the recycling out", "wash my running shoes", "review the OS flashcards", "buy stamps",
    "stretch for ten minutes", "bring snacks to the study group", "update the internship tracker",
    "water the fern in my room", "pick up the film from the camera shop", "email the TA the draft",
    "iron my interview shirt", "defrost the chicken", "restring my guitar", "book the campus shuttle",
    "buy oat milk", "swap the laundry over", "send Mom the photos from the hike", "clean out my backpack",
    "pick up my prescription", "fill the water bottle", "ask the registrar about my transcript",
    "sharpen the pencils for the exam", "return the textbook to the bookstore", "put the trash cans out",
    "download the lecture slides", "text Carmen about the concert", "bring my ID to the lab", "cut my nails",
    "order the replacement charger", "pack the climbing shoes", "empty the dishwasher", "buy a stamp for the postcard",
    "call the dentist's office", "charge my headphones", "mail the scholarship form", "clear my downloads folder",
    "get gas before the drive", "pick up the poster tube", "tidy my desk", "write the thank-you note to Aunt Priya",
    "renew my parking permit", "flip the mattress", "pay the phone bill", "check the oil in the car",
    "buy printer ink", "prep the slides for Thursday's presentation", "set out my clothes for the interview",
    "return the borrowed charger to Maya", "pack the camping stove", "record the mock interview",
    "refill my allergy meds", "bring the casserole dish back to Theo", "check my spam folder",
    "shred the old bank statements", "sign up for the career fair", "update my resume",
    "unplug the space heater", "bring cash for the food truck", "turn in the parking survey",
]

REM_TEMPLATES = [
    "remind me {w} to {task}", "{w}: remind me to {task}", "can you ping me {w} to {task}",
    "need a nudge {w} to {task}", "set a reminder {w} to {task}", "dont let me forget to {task} {w}",
    "put in a reminder {w}, {task}", "reminder {w}: {task}", "pls remind me to {task} {w}",
    "i keep forgetting to {task}, remind me {w}", "hey {w} i need to {task}, can you remind me",
    "alert me {w} so i {task}", "make me a reminder to {task} {w}", "yo remind me {w} to {task}",
    "can you remind me to {task} {w}", "nudge me {w}, i have to {task}", "tell me {w} to {task}",
    "add a reminder to {task} {w}",
]


def _spec(rng, now, kind):
    """(prompt phrase, resolved datetime, kind) or None. A bare weekday is its next occurrence AFTER today."""
    wd = rng.choice(WEEKDAYS)
    if kind == "daypart":
        part = rng.choice(["morning", "afternoon", "evening", "night"])
        pp = rng.choice(["night", "nite"]) if part == "night" else part
        d = wd_at(now, wd, part)
        return f"{pname(rng, wd)} {pp}", d, kind
    if kind == "tomorrow_part":
        part = rng.choice(["morning", "afternoon", "evening", "night"])
        return f"{rng.choice(['tomorrow', 'tmrw'])} {part}", at(now, f"tomorrow {part}"), kind
    if kind == "clock":
        h, m = rng.choice([7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]), rng.choice([0, 0, 0, 30, 15, 45])
        st = rng.choice([0, 0, 1, 2])
        c = clk(h, m, st)
        d = wd_at(now, wd, f"at {clk(h, m, 0)}")
        phrase = rng.choice([f"{pname(rng, wd)} at {c}", f"{c} {pname(rng, wd)}", f"on {wd} at {c}"])
        return phrase, d, kind
    if kind == "tomorrow_clock":
        h, m = rng.choice([7, 8, 9, 12, 14, 16, 18, 20]), rng.choice([0, 0, 30])
        c = clk(h, m, rng.choice([0, 1]))
        return f"tomorrow at {c}", at(now, f"tomorrow at {clk(h, m, 0)}"), kind
    if kind == "rel":
        mins = rng.choice([10, 15, 20, 30, 45, 60, 90, 120])
        n = {60: "an hour", 90: "an hour and a half", 120: "2 hours"}.get(mins, f"{mins} minutes")
        phrase = rng.choice([f"in {n}", f"in {n}", f"{n} from now"]) if mins not in (90,) else "in an hour and a half"
        d = now + D(minutes=mins)
        return phrase, d.replace(second=0, microsecond=0), kind
    if kind == "tonight":
        h = rng.choice([None, 21, 22, 20])
        if h is None:
            return "tonight", at(now, "tonight"), kind
        return f"tonight at {h - 12}", now.replace(hour=h, minute=0, second=0, microsecond=0), kind
    if kind == "tomorrow":
        return rng.choice(["tomorrow", "tmrw"]), at(now, "tomorrow"), kind
    if kind == "today_part":
        part = rng.choice(["afternoon", "evening"])
        d = now.replace(hour=15 if part == "afternoon" else 18, minute=0, second=0, microsecond=0)
        return f"this {part}", d, kind
    return None


_HOURS = {"rel": [8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20], "tonight": [8, 9, 11, 13, 14, 16, 17],
          "today_part": [8, 9, 10, 11, 12]}


def _when_say(d, now, kind, *, to=False):
    """The time as prose; `to=True` is for the slot after 'for' / 'to' (no 'on ' prefix)."""
    if kind == "rel":
        return clock(d) if to else f"at {clock(d)}"
    return bare(d, now) if to else dphr(d, now)


def _rem_final(rng, task, d, now, kind):
    t = second(task)
    when, wb = _when_say(d, now, kind), _when_say(d, now, kind, to=True)
    return _pick(rng, [
        f"I'll remind you {when} to {t}.",
        f"You'll get a nudge {when} to {t}.",
        f"Okay, I'll remind you to {t} {when}.",
        f"Reminder set: {t}, {when}.",
        f"Set for {wb}: {t}.",
        f"Sure thing. A reminder to {t} will go off {when}.",
        f"Noted. I'll nudge you {when} to {t}.",
        f"All set, I'll ping you {when} to {t}.",
        f"Got it. I'll bring it up {when}: {t}.",
        f"Done, your reminder is in for {wb}.",
    ])


def _ask_reminder(kind, task, tmpl):
    def make(rng):
        hrs = _HOURS.get(kind)
        now, w = base_world(rng, **({"hour": rng.choice(hrs)} if hrs else {}))
        spec = _spec(rng, now, kind)
        if spec is None:
            return None
        phrase, d, k = spec
        if d <= now + D(minutes=5):
            return None
        prompt = style(rng, tmpl.format(w=phrase, task=task))
        title = cap(task)
        steps = [[Call("add_reminder", title=title, when_iso=iso(d))],
                 Final(_rem_final(rng, task, d, now, k))]
        return mk("act_dont_ask", prompt, now, w, steps, f"reminder, time words resolved ({kind}); no question", cards=[])
    return make


EVENT_BANK = [
    ("Haircut", 45, "", "a haircut"), ("Study session with Aisha", 120, "Hollis Library", "a study session with aisha"),
    ("Call with the advisor", 30, "", "a call with my advisor"), ("Dinner with Carmen", 90, "", "dinner with carmen"),
    ("Climbing with Theo", 120, "", "climbing with theo"), ("Dentist cleaning", 60, "", "a dentist cleaning"),
    ("Oil change", 45, "", "an oil change"), ("Interview prep session", 60, "Career Center", "an interview prep session"),
    ("Pottery class", 120, "", "pottery class"), ("Coffee with Ben", 45, "", "coffee with ben"),
    ("Lab review session", 90, "Kerr Hall 301", "a lab review session"), ("Piano lesson", 45, "", "piano lesson"),
    ("Eye exam", 45, "", "an eye exam"), ("Board game night", 180, "", "board game night"),
    ("Tutoring with Hana", 60, "Hollis Library", "tutoring with hana"), ("Yoga class", 60, "", "yoga class"),
]
EVENT_TEMPLATES = [
    "put {ev} down for {w}", "schedule {ev} {w}", "add {ev} {w}", "can u book {ev} {w}",
    "new event: {ev}, {w}", "i need {ev} on the books {w}", "pencil me in for {ev} {w}", "set up {ev} for {w}",
    "block out {w} for {ev}", "calendar: {ev} {w}", "throw {ev} on my calendar {w}", "i have {ev} {w}, add it",
]


def _ask_event(i):
    def make(rng):
        now, w = base_world(rng)
        title, mins, loc, spoken = EVENT_BANK[(i * 2) % len(EVENT_BANK)]
        wd = rng.choice(WEEKDAYS)
        h, m = rng.choice([8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]), rng.choice([0, 0, 30])
        c = clk(h, m, rng.choice([0, 0, 1]))
        d = wd_at(now, wd, f"at {clk(h, m, 0)}")
        if rng.random() < 0.25:
            d, phrase = at(now, f"tomorrow at {clk(h, m, 0)}"), f"tomorrow at {c}"
        else:
            phrase = f"{pname(rng, wd)} at {c}"
        extra = ""
        args = {"title": title, "when_iso": iso(d)}
        if i % 3 == 0:
            args["duration_min"] = mins
            extra = f" for {mins} minutes" if mins % 60 else f" for {mins // 60} hour{'s' if mins > 60 else ''}"
        if loc and i % 2 == 0:
            args["location"] = loc
            extra += f" at {loc}"
        prompt = style(rng, rng.choice(EVENT_TEMPLATES).format(ev=spoken, w=phrase + extra))
        say = f" ({loc})" if "location" in args else ""
        final = _pick(rng, [
            f"Added {title} {dphr(d, now)}{say}.",
            f"{title} is on your calendar {dphr(d, now)}{say}.",
            f"You're booked: {title}, {dphr(d, now)}{say}.",
            f"Okay, I put {title} down for {bare(d, now)}{say}.",
            f"All set, {title} is {dphr(d, now)}{say}.",
            f"Your calendar now has {title} {dphr(d, now)}{say}.",
            f"Sure. {title} is scheduled for {bare(d, now)}{say}.",
        ])
        return mk("act_dont_ask", prompt, now, w, [[Call("add_calendar_event", **args)], Final(final)],
                  "calendar add with the weekday's next occurrence; a card is raised",
                  approve=[True], cards=["add_calendar_event"])
    return make


# The text is the user's own words, nothing added (no softeners, no exclamation the user did not type).
MSG_BANK = [
    ("tell {to} i'm picking up pizza on the way home", "Maya", "I'm picking up pizza on the way home.",
     "you're picking up pizza on the way home"),
    ("text {to} i'm outside", "Theo", "I'm outside.", "you're outside"),
    ("message {to}: happy birthday!! dinner's on me", "Carmen", "Happy birthday!! Dinner's on me.",
     "you said happy birthday and that dinner's on you"),
    ("let {to} know my train gets in at 6", "Mom", "My train gets in at 6.", "your train gets in at 6"),
    ("send {to} a text saying thanks for covering my shift", "Dana", "Thanks for covering my shift.",
     "you're thankful for the shift cover"),
    ("shoot {to} a quick text, i can't make the book club tonight", "Aisha",
     "I can't make the book club tonight.", "you can't make the book club tonight"),
    ("text {to} that the gym closes at 9", "Ben", "The gym closes at 9.", "the gym closes at 9"),
    ("tell {to} i left the keys on the kitchen counter", "Maya", "I left the keys on the kitchen counter.",
     "you left the keys on the kitchen counter"),
    ("drop {to} a text, i'll bring the camping stove saturday", "Theo", "I'll bring the camping stove Saturday.",
     "you'll bring the camping stove on Saturday"),
    ("tell {to} dinner is at 7 not 6", "Carmen", "Dinner is at 7, not 6.", "dinner is at 7, not 6"),
]


def _ask_message(i):
    def make(rng):
        now, w = base_world(rng)
        tmpl, to, text, gist = MSG_BANK[i % len(MSG_BANK)]
        prompt = style(rng, tmpl.format(to=to.lower() if rng.random() < 0.5 else to))
        final = _pick(rng, [f"Sent. {to} knows {gist}.", f"Texted {to}: “{text}”", f"I let {to} know {gist}.",
                            f"Okay, {to} has your message: “{text}”", f"{to} now knows {gist}.",
                            f"Your text to {to} is on its way."])
        return mk("act_dont_ask", prompt, now, w, [[Call("send_message", to=to, text=text)], Final(final)],
                  "text to a contact on file: resolve the name and send (card)",
                  approve=[True], cards=["send_message"])
    return make


SCHED_BANK = [
    ("text {to} {w}: good luck on the midterm!", "Theo", "Good luck on the midterm!", "good luck message"),
    ("remind {to} by text {w} that the rent is due", "Maya", "Reminder: the rent is due.", "rent reminder"),
    ("text {to} {w} to say happy birthday", "Carmen", "Happy birthday!", "birthday text"),
    ("{w} send {to} a text, don't forget the study guide", "Aisha", "Don't forget the study guide.",
     "study guide reminder"),
]


def _ask_schedule(i):
    def make(rng):
        now, w = base_world(rng, hour=rng.choice([9, 10, 13, 15, 17]))
        tmpl, to, body, what = SCHED_BANK[i % len(SCHED_BANK)]
        wd = rng.choice(WEEKDAYS)
        h, m = rng.choice([7, 8, 9, 12, 18]), rng.choice([0, 30])
        when = rng.choice([f"{wd} at {clk(h, m, 0)}", f"tomorrow at {clk(h, m, 0)}"])
        d = at(now, when)
        if d.date() == now.date():       # the product resolver would read a same-weekday word as today
            return None
        prompt = style(rng, tmpl.format(to=to.lower(), w=when))
        final = _pick(rng, [f"Scheduled. {to} will get your text {dphr(d, now)}.",
                            f"{to} gets that text {dphr(d, now)}.",
                            f"Okay, I'll send {to} your {what} {dphr(d, now)}.",
                            f"Your {what} to {to} goes out {dphr(d, now)}."])
        return mk("act_dont_ask", prompt, now, w,
                  [[Call("schedule_send", channel="message", to=to, when=when, body=body)], Final(final)],
                  "schedule_send with the user's own time words passed through",
                  approve=[True], cards=["schedule_send"])
    return make


NOTE_BANK = [
    ("note to self: moving checklist - return keys, forward mail, cancel internet", "Moving checklist",
     "return keys, forward mail, cancel internet"),
    ("make a note called Climbing trip with harness, shoes and chalk", "Climbing trip", "harness, shoes, chalk"),
    ("jot down gift ideas for dad: wool socks, a good thermos", "Gift ideas for Dad", "wool socks, a good thermos"),
    ("start a note named Interview questions: why fernbrook, my OS project, a conflict story",
     "Interview questions", "why Fernbrook, my OS project, a conflict story"),
    ("save a quick note: library hours are 8 to 10 on weekdays", "Library hours", "8 to 10 on weekdays"),
]


def _ask_note(i):
    def make(rng):
        now, w = base_world(rng)
        text, title, body = NOTE_BANK[i % len(NOTE_BANK)]
        prompt = style(rng, text)
        final = _pick(rng, [f"I saved a new note called “{title}” with {second(body)}.",
                            f"“{title}” is in Notes now.",
                            f"Saved. Your note “{title}” has {second(body)}.",
                            f"Your new note “{title}” is ready."])
        return mk("act_dont_ask", prompt, now, w, [[Call("create_note", title=title, body=body)], Final(final)],
                  "note creation from the user's own words", cards=[])
    return make


def _two_people(name_a, rel_a, name_b, rel_b):
    prof = copy.deepcopy(H.PROFILE)
    prof["people"] = prof["people"] + [{"name": name_a, "relation": rel_a}, {"name": name_b, "relation": rel_b}]
    return prof


# (what the user typed, [ways to ask the one question], profile). The two same-named people in the last three
# are listed in the system prompt's "People who matter to them" block (profile["people"]), so the question
# names people the model can actually see.
AMBIGUOUS = [
    ("remind me tomorrow", ["What should I remind you about?", "Happy to. What's the reminder for?",
                            "Sure, what would you like to be reminded of?"], None),
    ("set a reminder for friday morning", ["What should the reminder say?", "Sure, what's it for?",
                                           "Of course. What should I remind you about?"], None),
    ("text her that i'm on my way", ["Who should I text?", "Who is “her”? Give me a name and I'll send it.",
                                     "Which person do you mean?"], None),
    ("add an event on saturday at 4", ["What should I call the event?", "Sure, what's the event?",
                                       "What is happening on Saturday at 4?"], None),
    ("send him the address", ["Who should I send it to, and which address do you mean?",
                              "Which address, and who is it going to?"], None),
    ("ping me about it later", ["What should I ping you about, and when?",
                                "Sure. What is it about, and what time suits you?"], None),
    ("text alex that i'm running late", ["Which Alex do you mean, Alex Rowe or Alex Moreno?",
                                         "You have two Alexes: Alex Rowe and Alex Moreno. Which one?"],
     _two_people("Alex Rowe", "classmate", "Alex Moreno", "cousin")),
    ("email jordan the slides", ["Which Jordan should I email, Jordan Ellis or Jordan Lee?",
                                 "I see Jordan Ellis and Jordan Lee. Which one gets the slides?"],
     _two_people("Jordan Ellis", "project partner", "Jordan Lee", "cousin")),
    ("tell noor the meeting moved", ["Which Noor do you mean, Noor Haddad or Noor Beck?",
                                     "Is that Noor Haddad or Noor Beck?"],
     _two_people("Noor Haddad", "lab partner", "Noor Beck", "coworker")),
]


def _ask_ambiguous(i):
    def make(rng):
        now, w = base_world(rng)
        text, qs, prof = AMBIGUOUS[i % len(AMBIGUOUS)]
        sc = mk("act_dont_ask", style(rng, text), now, w, [Final(_pick(rng, qs))],
                "genuinely ambiguous: one short question, no call", profile=prof)
        return sc
    return make


def fam_act_dont_ask(rng):
    out = []
    kinds = (["daypart"] * 4 + ["tomorrow_part"] * 2 + ["clock"] * 4 + ["tomorrow_clock"] * 1 + ["rel"] * 3
             + ["tonight"] * 1 + ["tomorrow"] * 2 + ["today_part"] * 1)
    tasks = rng.sample(REM_TASKS, len(kinds))      # one task per prompt: none repeats inside the family
    tmpls = rng.sample(REM_TEMPLATES, len(kinds))   # nor does a prompt shape
    makers = [_ask_reminder(k, t, m) for k, t, m in zip(kinds, tasks, tmpls)]
    _fill_ordered(out, makers, rng, "act_dont_ask")
    _fill_ordered(out, [_ask_event(i) for i in range(7)], rng, "act_dont_ask")
    _fill_ordered(out, [_ask_message(i) for i in range(6)], rng, "act_dont_ask")
    _fill_ordered(out, [_ask_schedule(i) for i in range(3)], rng, "act_dont_ask")
    _fill_ordered(out, [_ask_note(i) for i in range(3)], rng, "act_dont_ask")
    _fill_ordered(out, [_ask_ambiguous(i) for i in (0, 1, 2, 3, 6, 7, 8)], rng, "act_dont_ask")
    return out


def _fill_ordered(out, makers, rng, family, *, need_load=False, tries=5):
    for mkr in makers:
        sc = _attempt(mkr, rng, family, tries=tries, need_load=need_load)
        if sc is not None:
            out.append(sc)


# =============================================================================
# FAMILY 2: finish_the_write
# =============================================================================
def _msgs(w, *items):
    w.messages = [m for m in w.messages if m.conv not in {i.conv for i in items}] + list(items)


# ---- a. contact enrichment: mail/text -> manage_contacts
def _contact_from_mail(i):
    people = [("Jordan Ellis", "jellis@linden.edu", "Lab writeup draft", "I put our lab writeup draft in the shared folder."),
              ("Ben Ortiz", "ben.ortiz@example.com", "Saturday long run", "Long run is at 8am at Riverside. Bring water."),
              ("Jordan Ellis", "jellis@linden.edu", "Data structures project split", "Want to split the project?"),
              ("Ben Ortiz", "ben.ortiz@example.com", "Trail map", "Here's the trail map for Sunday.")]
    prompts = ["grab {first}'s email address from his last email and save it to his contact",
               "{first} emailed me from a new address, add it to his contact card",
               "save the address {first} wrote me from to his contact",
               "take {first}'s email off that thread and put it in his contact"]

    def make(rng):
        now, w = base_world(rng)
        name, addr, subj, body = people[i % len(people)]
        first = name.split()[0]
        w.emails = [Email(name, addr, subj, now - D(hours=rng.randint(3, 40)), body, True, True,
                          message_id=f"<{first.lower()}{rng.randint(100, 999)}@mail.example>")]
        w.emails += [e for e in gen_news(rng, now)]
        prompt = style(rng, prompts[i % len(prompts)].format(first=first.lower()))
        final = _pick(rng, [f"Saved {addr} to {name}'s contact.", f"{addr} is now on {name}'s contact card.",
                            f"{name}'s contact has {addr} now.", f"I added {addr} to {name}'s contact."])
        steps = [[Call("view_emails", query=first, count=1)],
                 [Call("manage_contacts", action="add_email", name=name, value=addr)], Final(final)]
        return mk("finish_the_write", prompt, now, w, steps, "read the email, then add_email to the contact",
                  cards=[], must_see=[addr])
    return make


def gen_news(rng, now):
    import gen
    return [Email(s, a, rng.choice(subs), now - D(minutes=rng.randint(30, 2000)), "Newsletter content.", False, False)
            for s, a, subs in rng.sample(gen.NEWSLETTERS, 2)]


def _contact_from_text(i):
    people = [("Hana Kowalczyk", "(503) 555-0188", "my number is (503) 555-0188 in case the lab moves"),
              ("Dana Whitfield", "(503) 555-0194", "this is my new number, (503) 555-0194"),
              ("Hana Kowalczyk", "(971) 555-0162", "call me at (971) 555-0162 if you can't find the room"),
              ("Dana Whitfield", "(971) 555-0173", "you can reach me on (971) 555-0173 this week")]
    prompts = ["{first} texted her number, put it in her contact",
               "save the number {first} just sent into her contact",
               "{first} sent a new phone number, can you add it to her contact card",
               "add the number from {first}'s text to her contact"]

    def make(rng):
        now, w = base_world(rng)
        name, number, text = people[i % len(people)]
        first = name.split()[0]
        _msgs(w, Msg(name, name, text, now - D(hours=rng.randint(1, 30)), True))
        prompt = style(rng, prompts[i % len(prompts)].format(first=first.lower()))
        final = _pick(rng, [f"Saved {number} to {name}'s contact.", f"{name}'s contact now has {number}.",
                            f"I added {number} to {name}'s contact."])
        steps = [[Call("view_messages", query=first)],
                 [Call("manage_contacts", action="add_phone", name=name, value=number)], Final(final)]
        return mk("finish_the_write", prompt, now, w, steps, "read the text, then add_phone to the contact",
                  cards=[], must_see=[number])
    return make


def _contact_new(i):
    people = [("Renata Szabo", "rszabo@fernbrooklabs.com", "Fernbrook Labs — interview next steps",
               "Thanks for applying. Could you share three 45-minute slots next week?"),
              ("Hana Okoye", "hokoye@linden.edu", "Section swap", "Can you swap to the Tuesday section?")]
    prompts = ["make a contact for {first} from that email with her address",
               "add {first} to my contacts using the email she sent"]

    def make(rng):
        now, w = base_world(rng)
        name, addr, subj, body = people[i % len(people)]
        first = name.split()[0]
        w.emails = [Email(name, addr, subj, now - D(hours=rng.randint(2, 30)), body, True, True,
                          message_id=f"<{first.lower()}{rng.randint(100, 999)}@mail.example>")]
        w.contacts.pop(name, None)
        prompt = style(rng, prompts[i % len(prompts)].format(first=first.lower()))
        final = _pick(rng, [f"{name} is in your contacts now, with {addr}.", f"I created a contact for {name} with {addr}.",
                            f"Added {name} to your contacts with {addr}."])
        steps = [[Call("view_emails", query=first, count=1)],
                 [Call("manage_contacts", action="create", name=name)],
                 [Call("manage_contacts", action="add_email", name=name, value=addr)], Final(final)]
        return mk("finish_the_write", prompt, now, w, steps, "read the email, create the contact, add the address",
                  cards=[], must_see=[addr])
    return make


# ---- b. weather -> text. The text answers the question that was asked (jacket? what to wear? is the hike on?
# how cold tonight?) from the numbers the forecast returned, nothing else.
WX_ASKS = [
    ("rain", ["see if it's going to rain tomorrow and text {who}", "is it going to rain tomorrow? let {who} know"], "tomorrow"),
    ("jacket", ["check tomorrow's forecast and tell {who} whether to bring a jacket",
                "look up tomorrow's weather and text {who} if a jacket is a good idea"], "tomorrow"),
    ("today", ["find out what the weather's doing today then text {who} about it",
               "check today's weather and send {who} a rundown"], "today"),
    ("wear", ["look at the forecast for saturday and message {who} so they know what to wear",
              "text {who} what to wear on saturday, check the forecast first"], "saturday"),
    ("hike", ["weather check, then let {who} know if the hike is on tomorrow",
              "check the forecast and tell {who} whether we're still hiking tomorrow"], "tomorrow"),
    ("umbrella", ["tell {who} if they should pack an umbrella tomorrow, check the forecast first",
                  "do i need an umbrella tomorrow? check, then text {who} the answer"], "tomorrow"),
    ("cold", ["check how cold it gets tonight and send {who} a heads up",
              "find the low for tonight and text it to {who}"], "today"),
    ("picnic", ["get the forecast and text it to {who} for the picnic",
                "check the weather for the picnic tomorrow and send {who} the forecast"], "tomorrow"),
]


def _wx_text(intent, d, when_w, on):
    hi, lo, desc, rain = d[1], d[2], d[3].lower(), d[4]
    rainy = rain >= 40
    if intent == "rain":
        return (f"Yes, rain looks likely {on}: {desc}, {rain}% chance." if rainy
                else f"It doesn't look like rain {on}: {desc}, only a {rain}% chance.")
    if intent == "jacket":
        return (f"Yes, bring a jacket {on}: {desc}, high of {hi}°F, low of {lo}°F." if (hi <= 62 or rainy)
                else f"You probably won't need a jacket {on}: {desc}, high of {hi}°F.")
    if intent == "today":
        return f"{cap(when_w)} in Portland: {desc}, high {hi}°F, low {lo}°F, {rain}% chance of rain."
    if intent == "wear":
        tip = ("a rain jacket and layers" if rainy else "a warm layer" if hi <= 60 else "a light layer")
        return f"{cap(when_w)} in Portland: {desc}, high {hi}°F, low {lo}°F. I'd go with {tip}."
    if intent == "hike":
        return (f"The hike might get rained on {on}: {desc}, {rain}% chance of rain, high of {hi}°F." if rainy
                else f"The hike looks fine {on}: {desc}, high of {hi}°F, {rain}% chance of rain.")
    if intent == "umbrella":
        return (f"Yes, pack an umbrella {on}: {desc}, {rain}% chance of rain." if rainy
                else f"You can skip the umbrella {on}: only a {rain}% chance of rain.")
    if intent == "cold":
        return f"It gets down to {lo}°F tonight in Portland, {desc}."
    return f"Picnic forecast for {when_w} in Portland: {desc}, high of {hi}°F, {rain}% chance of rain."


def _wx_final(rng, intent, who, d, on):
    hi, lo, desc, rain = d[1], d[2], d[3].lower(), d[4]
    rainy = rain >= 40
    pool = {
        "rain": [f"{who} knows {'rain looks likely' if rainy else 'rain looks unlikely'} {on}.",
                 f"I told {who} {'to expect rain' if rainy else 'it should stay dry'} {on}."],
        "jacket": [f"I told {who} {'to bring a jacket' if (hi <= 62 or rainy) else 'a jacket probably is not needed'} {on}.",
                   f"{who} has the answer on the jacket: {'yes, bring one' if (hi <= 62 or rainy) else 'probably not'}."],
        "today": [f"Texted {who} today's forecast: {desc}, high of {hi}°F.", f"{who} has the rundown: {desc} and {hi}°F at most."],
        "wear": [f"{who} has the {on.replace('on ', '')} forecast and a tip on what to wear.",
                 f"Sent {who} the forecast and what to wear: {desc}, high of {hi}°F."],
        "hike": [f"{who} knows the hike {'might get rained on' if rainy else 'looks fine'} {on}.",
                 f"I let {who} know {'the hike could be wet' if rainy else 'the hike is looking good'} {on}."],
        "umbrella": [f"{who} knows {'an umbrella is a good idea' if rainy else 'an umbrella is not needed'} {on}.",
                     f"Sent. {who} has the umbrella answer: {'pack one' if rainy else 'skip it'}."],
        "cold": [f"{who} knows it drops to {lo}°F tonight.", f"Sent. Tonight's low is {lo}°F, and {who} has it."],
        "picnic": [f"Texted {who} the picnic forecast: {desc}, high of {hi}°F {on}.", f"{who} has the forecast for the picnic."],
    }[intent]
    return _pick(rng, pool)


def _weather_text(i, denied=False):
    whos = ["Maya", "Theo", "Mom", "Carmen", "Ben", "Aisha"]

    def make(rng):
        now, w = base_world(rng)
        intent, tmpls, day = WX_ASKS[i % len(WX_ASKS)]
        who = whos[(i + rng.randint(0, 2)) % len(whos)]
        wx = w.weather["Portland,OR"]
        idx = 0 if day == "today" else 1
        if day == "saturday":
            k = (wd_at(now, "saturday", "morning").date() - now.date()).days
            if k not in (1, 2):
                return None
            idx = k
        d = wx["days"][idx]
        when_w = "today" if idx == 0 else ("tomorrow" if idx == 1 else "Saturday")
        on = when_w if idx < 2 else "on Saturday"
        text = _wx_text(intent, d, when_w, on)
        prompt = style(rng, rng.choice(tmpls).format(who=who if who == "Mom" else who.lower()))
        wcall = Call("get_weather", location="Portland,OR", **({"period": when_w.lower()} if idx else {}))
        send = Call("send_message", to=who, text=text)
        if denied:
            return mk("finish_the_write", prompt, now, w, [[wcall], [send]],
                      "weather then a text; the card is denied, so the turn ends at the call",
                      approve=[False], cards=["send_message"])
        final = _wx_final(rng, intent, who, d, on)
        return mk("finish_the_write", prompt, now, w, [[wcall], [send], Final(final)],
                  "get_weather, then send_message answering the question asked (card approved)",
                  approve=[True], cards=["send_message"], must_see=[d[3]])
    return make


# ---- c. find an event, then move it
MOVE_BANK = [("Resume workshop", 90, "Career Center", "resume workshop"),
             ("Mentor coffee", 45, "Brewed Awakening", "mentor coffee"),
             ("Pottery class", 120, "Art Annex", "pottery class"),
             ("Group project check-in", 60, "Kerr Hall 301", "project check-in"),
             ("Physical therapy", 45, "Eastside Clinic", "physical therapy"),
             ("Lab makeup session", 90, "Kerr Hall 301", "lab makeup session"),
             ("Piano lesson", 45, "Music Annex", "piano lesson"),
             ("Tutoring with Hana", 60, "Hollis Library", "tutoring session"),
             ("Career fair prep", 60, "Student Union", "career fair prep"),
             ("Choir rehearsal", 90, "Music Annex", "choir rehearsal"),
             ("Study group", 90, "Hollis Library", "study group"),
             ("Advising appointment", 30, "Mercer Hall", "advising appointment")]

MOVE_LATER = ["push {ev} back {amt}", "{ev} needs to be {amt} later", "slide {ev} {amt} later",
              "delay {ev} by {amt}", "can we nudge {ev} back {amt}"]
MOVE_EARLIER = ["start {ev} {amt} earlier", "pull {ev} up by {amt}", "move {ev} {amt} earlier",
                "get {ev} going {amt} sooner", "{ev} should begin {amt} before it does now"]
MOVE_NEXT_DAY = ["bump {ev} to the next day, same time", "move {ev} a day later, same time",
                 "shift {ev} one day later but keep the time", "{ev} should be the day after, same time"]
MOVE_CLOCK = ["make {ev} start at {c} that day", "change {ev} to {c}, same day", "{ev}: new start is {c} that day",
              "set {ev} for {c} instead, same day"]


def _move_event(i, denied=False):
    def make(rng):
        now, w = base_world(rng, hour=rng.choice([8, 9, 10, 11, 12, 13]))
        title, mins, loc, kw = MOVE_BANK[i % len(MOVE_BANK)]
        day = now.date() + D(days=rng.randint(1, 4))
        h = rng.choice([10, 11, 13, 14, 15, 16])
        start = dt.datetime.combine(day, dt.time(h, rng.choice([0, 30])))
        w.events = [e for e in w.events if e.start.date() != day] + [Event(title, start, mins, loc)]
        mode = rng.choice(["later", "earlier", "next_day", "clock"])
        amt_names = {30: rng.choice(["30 minutes", "half an hour"]), 60: rng.choice(["an hour", "1 hour", "60 minutes"]),
                     90: rng.choice(["90 minutes", "an hour and a half"])}
        if mode == "later":
            delta = rng.choice([30, 60, 90])
            new = start + D(minutes=delta)
            ask = rng.choice(MOVE_LATER).replace("{amt}", amt_names[delta])
        elif mode == "earlier":
            delta = rng.choice([30, 60])
            new = start - D(minutes=delta)
            ask = rng.choice(MOVE_EARLIER).replace("{amt}", amt_names[delta])
        elif mode == "next_day":
            new = start + D(days=1)
            ask = rng.choice(MOVE_NEXT_DAY)
        else:
            hh = rng.choice([x for x in (9, 12, 15, 17, 18) if x != h])
            new = dt.datetime.combine(day, dt.time(hh, 0))
            ask = rng.choice(MOVE_CLOCK).replace("{c}", clk(hh, 0, 0))
        if new <= now + D(minutes=30):
            return None
        prompt = style(rng, ask.format(ev="the " + kw))
        read = Call("get_upcoming", query=kw, days=14, calendar_only=True)
        move = Call("update_event", title=title, when_iso=iso(new))
        if "Nothing scheduled" in w.t_get_upcoming(query=kw, days=14, calendar_only=True):
            # The user's words do not match any event title ("tutoring session" vs "Tutoring with Hana"
            # is a miss for the search). Nothing was found, so nothing is moved: say so, invent nothing,
            # and leave the (unmatched) event out of the world so the answer is true.
            if denied:
                return None
            w.events = [e for e in w.events if e.title != title]
            final = _pick(rng, [f"I couldn't find a {kw} on your calendar in the next two weeks, so I haven't moved anything. If it's listed under another name, tell me and I'll look again.",
                                f"Nothing on your calendar in the next two weeks matches “{kw}”, so nothing has changed. What is it called there?",
                                f"I don't see a {kw} coming up in the next two weeks, so I left your calendar alone. Tell me its title or day and I'll check again."])
            return mk("finish_the_write", prompt, now, w, [[read], Final(final)],
                      "the search finds nothing: no update_event, say so and ask for the name instead of guessing",
                      approve=None, cards=[])
        if denied:
            return mk("finish_the_write", prompt, now, w, [[read], [move]],
                      "find the event, ask to move it; the card is denied so the turn ends at the call",
                      approve=[False], cards=["update_event"], must_see=[title])
        final = _pick(rng, [f"Moved {title} to {bare(new, now)}.", f"{title} now starts {dphr(new, now)}.",
                            f"{title} is now {dphr(new, now)}.", f"I moved {title} to {bare(new, now)}.",
                            f"Okay, {title} is set for {bare(new, now)}."])
        return mk("finish_the_write", prompt, now, w, [[read], [move], Final(final)],
                  "get_upcoming to find the event, then update_event (card approved)",
                  approve=[True], cards=["update_event"], must_see=[title])
    return make


# ---- d. find a reminder, then complete / move it
DONE_BANK = [("Mail the rent check", "rent", "just mailed the rent check, tick it off"),
             ("Submit the lab report", "lab report", "done with the lab report, mark it finished"),
             ("Pick up the dry cleaning", "dry cleaning", "got the dry cleaning, cross that one off"),
             ("Print the ethics essay", "ethics essay", "printed the ethics essay, can you close that out"),
             ("Return the rental skis", "skis", "the skis are back at the shop, that reminder can go"),
             ("Order the cap and gown", "cap and gown", "ordered my cap and gown, check it off"),
             ("Send the group project outline", "outline", "sent the outline, you can mark that done"),
             ("Buy bus tickets to Eugene", "bus tickets", "bus tickets are bought, finish that reminder")]
OTHERS = ["Water the fern", "Call the bike shop", "Stretch for ten minutes", "Back up my laptop", "Wash running shoes"]
PUSH_TITLES = ["Book the lab slot", "Turn in the parking survey", "Call the landlord", "Refill the allergy meds",
               "Pay the phone bill", "Return the camera lens", "Email the TA", "Confirm the dentist visit"]


def _rem_world(rng, now, title, due):
    others = rng.sample(OTHERS, 3)
    rem = [Reminder(title, due)]
    for t in others:
        rem.append(Reminder(t, dt.datetime.combine(now.date() + D(days=rng.randint(2, 6)), dt.time(rng.choice([9, 12, 17]), 0))))
    return rem


def _rem_complete(i):
    def make(rng):
        now, w = base_world(rng)
        title, kw, prompt = DONE_BANK[i % len(DONE_BANK)]
        due = dt.datetime.combine(now.date() + D(days=rng.randint(1, 3)), dt.time(rng.choice([9, 12, 17]), 0))
        w.reminders = _rem_world(rng, now, title, due)
        final = _pick(rng, [f"I checked off “{title}”.", f"Marked “{title}” as done.", f"“{title}” is crossed off.",
                            f"Okay, “{title}” is done.", f"Ticked off “{title}” for you."])
        steps = [[Call("search_reminders", query=kw)], [Call("complete_reminder", title=title)], Final(final)]
        return mk("finish_the_write", style(rng, prompt), now, w, steps, "find the reminder, then complete it",
                  cards=[], must_see=[title])
    return make


def _rem_push(i):
    def make(rng):
        now, w = base_world(rng, hour=rng.choice([8, 9, 10, 11, 12, 13, 14, 15]))
        title = PUSH_TITLES[(i * 2 + rng.randint(0, 1)) % len(PUSH_TITLES)]
        mode = ["tomorrow", "today", "tomorrow"][i % 3]
        others = [Reminder(t, dt.datetime.combine(now.date() + D(days=rng.randint(3, 7)), dt.time(9, 0)))
                  for t in rng.sample(OTHERS, 3)]
        if mode == "tomorrow":
            due = dt.datetime.combine(now.date() + D(days=1), dt.time(rng.choice([9, 12, 17]), 0))
            part_name = rng.choice(["saturday morning", "friday afternoon", "sunday morning"])
            wdn, ptn = part_name.split()
            tgt = wd_at(now, wdn, ptn)
            part = tgt.strftime("%A").lower() + (" morning" if tgt.hour < 12 else " afternoon")
            if tgt.date() <= due.date():
                return None
            prompt = style(rng, rng.choice([f"whatever i have due tomorrow, move it to {part}",
                                             f"the thing due tomorrow can wait, push it to {part}",
                                             f"i can't get to tomorrow's reminder, bump it to {part}"]))
            read, new = Call("search_reminders", scope="tomorrow"), tgt
        else:
            due = now.replace(minute=0, second=0, microsecond=0) + D(hours=3)
            if due.date() != now.date():
                return None
            new = due + D(hours=1)
            prompt = style(rng, rng.choice(["push my next reminder today back an hour",
                                             "the reminder i have later today, make it an hour later",
                                             "later today's reminder can wait an hour, move it"]))
            read = Call("search_reminders", scope="today")
        w.reminders = [Reminder(title, due)] + others
        final = _pick(rng, [f"Moved “{title}” to {bare(new, now)}.", f"“{title}” is now {dphr(new, now)}.",
                            f"I pushed “{title}” to {bare(new, now)}.", f"Okay, “{title}” will go off {dphr(new, now)}."])
        steps = [[read], [Call("update_reminder", title=title, when_iso=iso(new))], Final(final)]
        return mk("finish_the_write", prompt, now, w, steps, "find the reminder, then update_reminder to the new time",
                  cards=[], must_see=[title])
    return make


# ---- e. read an email, then reply
REPLY_BANK = [
    ("Hana Kowalczyk", "hkowalczyk@linden.edu", "Office hours moved",
     "My office hours move to Thursday 3–4 PM this week only.",
     "Thanks, Hana. Thursday 3–4 works for me.", "draft"),
    ("Jordan Ellis", "jellis@linden.edu", "Lab writeup draft",
     "I put our lab writeup draft in the shared folder. Can you do the analysis section by Wednesday?",
     "Yes, I'll have the analysis section done by Wednesday.", "send"),
    ("Prof. Adaeze Okafor", "aokafor@linden.edu", "Project 2 extension request",
     "I can give you until Friday at 11:59 PM for project 2.",
     "Thank you so much, Professor. I'll submit project 2 by Friday at 11:59 PM.", "draft"),
    ("Dana Whitfield", "dwhitfield@linden.edu", "Can you cover Saturday?",
     "Hi, could you cover the Saturday 10–2 shift? Let me know by Thursday.",
     "Yes, I can cover the Saturday 10–2 shift.", "send"),
    ("Renata Szabo", "rszabo@fernbrooklabs.com", "Fernbrook Labs — interview next steps",
     "Could you share three 45-minute slots next week for a technical interview?",
     "Thanks, Renata. I'll send over three 45-minute slots for next week shortly.", "draft"),
    ("Carmen Rivera", "carmen.rivera@example.com", "Mom's birthday plan",
     "Can you chip in $30 for Mom's birthday gift? I'm ordering it Sunday.",
     "Count me in for the $30.", "send"),
]
DRAFT_PROMPTS = ["see what {first} wants and draft a reply saying {gist}", "open {first}'s latest email and write me a reply, {gist}, but don't send it",
                 "read the email from {first} and draft something back: {gist}"]
SEND_PROMPTS = ["answer {first}'s email, tell {them} {gist}", "reply to {first}'s last email and say {gist}",
                "get back to {first} about that email, {gist}"]
GISTS = {"Office hours moved": "thursday works", "Lab writeup draft": "yes i'll do the analysis by wednesday",
         "Project 2 extension request": "thanks and i'll submit by friday",
         "Can you cover Saturday?": "yes i can cover it", "Fernbrook Labs — interview next steps": "i'll send the slots soon",
         "Mom's birthday plan": "i'm in for the $30"}


def _email_reply(i, denied=False):
    def make(rng):
        now, w = base_world(rng)
        name, addr, subj, body, reply, mode = REPLY_BANK[i % len(REPLY_BANK)]
        first = "Okafor" if name.startswith("Prof.") else name.split()[0]      # what the user would type
        disp = "Prof. Okafor" if name.startswith("Prof.") else first
        mid = f"<{first.lower()}{rng.randint(1000, 9999)}@mail.example>"
        w.emails = [Email(name, addr, subj, now - D(hours=rng.randint(2, 30)), body, True, True, message_id=mid)]
        w.emails += gen_news(rng, now)
        gist = GISTS[subj]
        if mode == "draft":
            prompt = style(rng, DRAFT_PROMPTS[i % 3].format(first=first.lower(), gist=gist))
            read = Call("view_emails", query=first, count=1)
            write = Call("draft_email", to=addr, subject=f"Re: {subj}", body=reply)
            final = _pick(rng, [f"I've opened a draft reply to {disp} in Mail. It hasn't been sent, so you can look it over first.",
                                f"Your draft to {disp} is ready in Mail. Nothing's been sent yet.",
                                f"The reply to {disp} is sitting in Mail as a draft, unsent, for you to review."])
            return mk("finish_the_write", prompt, now, w, [[read], [write], Final(final)],
                      "read the email, then draft_email (no send)", cards=[], must_see=[subj])
        prompt = style(rng, SEND_PROMPTS[i % 3].format(first=first.lower(), them="them", gist=gist))
        read = Call("view_emails", query=first, count=1)
        write = Call("reply_to_email", message_id=mid, body=reply)
        if denied:
            return mk("finish_the_write", prompt, now, w, [[read], [write]],
                      "read the email, reply_to_email; the card is denied so the turn ends at the call",
                      approve=[False], cards=["reply_to_email"], must_see=[subj])
        final = _pick(rng, [f"Replied to {disp}: “{reply}”", f"I sent your reply to {disp}: “{reply}”",
                            f"{disp} has your reply now: “{reply}”",
                            f"Your reply to {disp} is on its way: “{reply}”"])
        return mk("finish_the_write", prompt, now, w, [[read], [write], Final(final)],
                  "read the email, then reply_to_email (card approved)", approve=[True], cards=["reply_to_email"],
                  must_see=[subj])
    return make


# ---- f. note -> text
NOTE_SEND = [("Grocery list", "- oat milk\n- eggs\n- spinach\n- rice\n- coffee", "oat milk, eggs, spinach, rice, coffee",
              "grocery", "text {who} the grocery list", "Maya"),
             ("Reading list", "- The Pragmatic Programmer\n- Educated\n- Project Hail Mary",
              "The Pragmatic Programmer, Educated, Project Hail Mary", "reading list",
              "send {who} the reading list i wrote down", "Theo"),
             ("Gift ideas", "Mom: scarf or a cookbook\nCarmen: concert tickets\nTheo: climbing gym pass",
              "scarf or a cookbook", "gift ideas", "tell {who} what gift ideas i had for mom", "Carmen")]


def _note_text(i, denied=False):
    def make(rng):
        now, w = base_world(rng)
        title, body, gist, kw, tmpl, who = NOTE_SEND[i % len(NOTE_SEND)]
        w.notes = [n for n in w.notes if n.title != title] + [Note(title, body, now - D(days=rng.randint(1, 20)), "Notes")]
        prompt = style(rng, tmpl.format(who=who.lower()))
        text = "Gift ideas for Mom: scarf or a cookbook." if title == "Gift ideas" else f"{title}: {gist}"
        read, send = Call("search_notes", query=kw), Call("send_message", to=who, text=text)
        if denied:
            return mk("finish_the_write", prompt, now, w, [[read], [send]],
                      "note lookup, text; the card is denied so the turn ends at the call",
                      approve=[False], cards=["send_message"], must_see=[title])
        what = "the gift idea for Mom" if title == "Gift ideas" else f"the {title.lower()}"
        final = _pick(rng, [f"Sent. {who} has {what}: {gist}.", f"Texted {who} the details: {gist}.",
                            f"{who} now has {what}."])
        return mk("finish_the_write", prompt, now, w, [[read], [send], Final(final)],
                  "search_notes, then send_message (card approved)", approve=[True], cards=["send_message"],
                  must_see=[title])
    return make


def fam_finish_the_write(rng):
    out = []
    mk_ = []
    mk_ += [_contact_from_mail(i) for i in range(4)] + [_contact_from_text(i) for i in range(3)]
    mk_ += [_contact_new(i) for i in range(2)]
    mk_ += [_weather_text(i) for i in range(6)] + [_weather_text(i, denied=True) for i in (1, 6)]
    mk_ += [_move_event(i) for i in (0, 1, 4, 5, 7, 9)] + [_move_event(11, denied=True)]
    mk_ += [_rem_complete(i) for i in (0, 2, 3, 5)] + [_rem_push(i) for i in range(4)]
    mk_ += [_email_reply(i) for i in range(6)] + [_email_reply(3, denied=True)]
    mk_ += [_note_text(i) for i in range(2)] + [_note_text(2, denied=True)]
    _fill_ordered(out, mk_, rng, "finish_the_write")
    return out


# =============================================================================
# FAMILY 3: stub_then_call
# =============================================================================
TOPICS = [("library books", "Return the library books"), ("bike shop", "Call the bike shop"),
          ("package", "Pick up the package"), ("groceries", "Buy groceries"),
          ("bus pass", "Renew the bus pass"), ("study guide", "Print the study guide"),
          ("tax form", "File the tax form"), ("passport photo", "Get the passport photos taken"),
          ("gym membership", "Freeze the gym membership"), ("flu shot", "Book the flu shot"),
          ("room swap", "Email housing about the room swap"), ("storage unit", "Pay the storage unit"),
          ("tire rotation", "Schedule the tire rotation"), ("tuition", "Pay the tuition balance"),
          ("thesis poster", "Pick up the thesis poster"), ("utilities", "Split the utilities with Maya")]


def _topic_world(rng, now, title, *, present=True, when_days=(1, 3)):
    w_rem = []
    if present:
        due = dt.datetime.combine(now.date() + D(days=rng.randint(*when_days)), dt.time(rng.choice([9, 12, 17]), 0))
        w_rem.append(Reminder(title, due))
    pool = [t for _k, t in TOPICS if t != title]
    for t in rng.sample(pool, 3):
        w_rem.append(Reminder(t, dt.datetime.combine(now.date() + D(days=rng.randint(2, 7)), dt.time(9, 0))))
    return w_rem


SEARCH_PROMPTS = ["whats still on my plate for the {t}", "what's left on my plate for the {t}",
                  "anything outstanding on the {t}", "what do i still owe on the {t}",
                  "do i have a reminder about the {t}", "is there anything set for the {t}",
                  "got anything lined up for the {t}", "what's the status of the {t}", "have i set anything for the {t}"]


def _stub_search(i):
    def make(rng):
        now, w = base_world(rng)
        kw, title = TOPICS[i % len(TOPICS)]
        present = rng.random() < 0.75
        rems = _topic_world(rng, now, title, present=present)
        w.reminders = rems
        prompt = style(rng, SEARCH_PROMPTS[i % len(SEARCH_PROMPTS)].format(t=kw))
        if present:
            due = rems[0].due
            final = _pick(rng, [f"Yes, “{title}” is set for {bare(due, now)}.",
                                f"You have “{title}” coming up {dphr(due, now)}.",
                                f"“{title}” is on your list for {bare(due, now)}.",
                                f"I found one: “{title}”, {dphr(due, now)}."])
        else:
            final = _pick(rng, [f"I don't see a reminder about the {kw} right now.",
                                f"Nothing's set for the {kw} at the moment.",
                                f"There's no reminder for the {kw} on your list.",
                                f"Nothing turns up for the {kw}."])
        return mk("stub_then_call", prompt, now, w, [[Call("search_reminders", query=kw)], Final(final)],
                  "read tool starts as a stub: load, then the real call, then an answer from the result",
                  cards=[], must_see=[title] if present else [])
    return make


UPD_PROMPTS = [("the {t} reminder should be tomorrow instead", "tomorrow"), ("shift the {t} reminder to tomorrow", "tomorrow"),
               ("can you move the reminder about the {t} to {wd} at {c}", "clock"),
               ("push the {t} reminder to {wd} at {c}", "clock"), ("the {t} reminder, make it tomorrow", "tomorrow"),
               ("move the {t} reminder to {wd} at {c}", "clock"), ("reschedule the {t} reminder for {wd} at {c}", "clock")]


def _stub_update(i):
    def make(rng):
        now, w = base_world(rng, hour=rng.choice([8, 9, 10, 11, 12, 13, 14, 15]))
        kw, title = TOPICS[(i + 9) % len(TOPICS)]
        tmpl, mode = UPD_PROMPTS[i % len(UPD_PROMPTS)]
        w.reminders = _topic_world(rng, now, title, when_days=(0, 0))
        w.reminders[0].due = now.replace(minute=0, second=0, microsecond=0) + D(hours=3)
        if w.reminders[0].due.date() != now.date():
            return None
        if mode == "tomorrow":
            new = dt.datetime.combine(now.date() + D(days=1), dt.time(w.reminders[0].due.hour, 0))
            args = {"title": kw, "day": "tomorrow"}
            prompt = tmpl.format(t=kw)
        else:
            wd = rng.choice(WEEKDAYS)
            h = rng.choice([8, 10, 12, 15, 17])
            new = wd_at(now, wd, f"at {clk(h, 0, 0)}")
            args = {"title": kw, "when_iso": iso(new)}
            prompt = tmpl.format(t=kw, wd=pname(rng, wd), c=clk(h, 0, 0))
        final = _pick(rng, [f"Moved your “{title}” reminder to {bare(new, now)}.",
                            f"“{title}” is now {dphr(new, now)}.",
                            f"Okay, your “{title}” reminder is set for {bare(new, now)}.",
                            f"Your “{title}” reminder now goes off {dphr(new, now)}."])
        return mk("stub_then_call", style(rng, prompt), now, w, [[Call("update_reminder", **args)], Final(final)],
                  "write tool starts as a stub: load, real call with full args, final from the receipt",
                  cards=[], must_see=[])
    return make


MAIL_REM = [("Dana Whitfield", "dwhitfield@linden.edu", "Can you cover Saturday?", "Hi Sam, could you cover the Saturday 10–2 shift?", "Dana"),
            ("Jordan Ellis", "jellis@linden.edu", "Lab writeup draft", "Can you do the analysis section by Wednesday?", "Jordan"),
            ("Hana Kowalczyk", "hkowalczyk@linden.edu", "Regrade for homework 3", "I looked at your regrade request.", "Hana"),
            ("Ben Ortiz", "ben.ortiz@example.com", "Saturday long run", "Long run is at 8am at Riverside.", "Ben"),
            ("Carmen Rivera", "carmen.rivera@example.com", "Mom's birthday plan", "Can you chip in $30 for Mom's gift?", "Carmen"),
            ("Renata Szabo", "rszabo@fernbrooklabs.com", "Fernbrook Labs — interview next steps", "Could you share three slots next week?", "Renata")]
MAIL_REM_PROMPTS = ["check what {f} emailed and set me a reminder to answer {w}", "see what {f} wrote me and set a reminder to reply {w}",
                    "read {f}'s latest and remind me {w2} to answer it", "check {f}'s email, then ping me {w2} to answer it",
                    "see what {f} emailed about and remind me {w2} to reply"]


def _stub_mail_reminder(i):
    def make(rng):
        now, w = base_world(rng)
        name, addr, subj, body, f = MAIL_REM[i % len(MAIL_REM)]
        w.emails = [Email(name, addr, subj, now - D(hours=rng.randint(2, 30)), body, True, True)] + gen_news(rng, now)
        wd = rng.choice(WEEKDAYS)
        part = rng.choice(["morning", "afternoon", "evening"])
        d = wd_at(now, wd, part)
        w2 = f"{pname(rng, wd)} {part}"
        tmpl = MAIL_REM_PROMPTS[i % len(MAIL_REM_PROMPTS)]
        prompt = style(rng, tmpl.format(f=f.lower(), w=f"by {pname(rng, wd)}", w2=w2))
        if "{w}" in tmpl:
            d = wd_at(now, wd, "morning")
        title = f"Reply to {f} about {subj.lower().rstrip('?')}"
        final = _pick(rng, [f"{f}'s email is about “{subj}”. I've set a reminder for {bare(d, now)} to reply.",
                            f"{f} wrote about “{subj}”, and I'll remind you {dphr(d, now)} to answer.",
                            f"Okay, {f} wrote about “{subj}”. You'll get a reminder {dphr(d, now)} to reply.",
                            f"A reminder to reply to {f} is set for {bare(d, now)}. The email is about “{subj}”."])
        steps = [[Call("view_emails", query=f, count=1)], [Call("add_reminder", title=title, when_iso=iso(d))], Final(final)]
        return mk("stub_then_call", prompt, now, w, steps, "read (stub) then write (stub): two loads, two real calls",
                  cards=[], must_see=[subj])
    return make


UP_REM = [("look at tomorrow and remind me an hour before my first thing", "tomorrow", 60),
          ("see what's on {wd} and remind me 30 minutes before the first event", "wd", 30),
          ("check my first thing tomorrow and ping me an hour ahead", "tomorrow", 60),
          ("look at {wd} and remind me the night before to prep", "wd_night", 0)]
# titles that are NOT in the default Sam Rivera calendar, so the read can only return the one scripted here
UP_EVENTS = [("Advising appointment", "Dean's Office, Mercer Hall"), ("Mentor coffee", "Brewed Awakening"),
             ("Resume workshop", "Career Center"), ("Capstone demo rehearsal", "Kerr Hall 301"),
             ("Fernbrook info session", "Student Union")]


def _stub_upcoming_reminder(i):
    def make(rng):
        now, w = base_world(rng, hour=rng.choice([8, 9, 10, 11, 12, 13, 14, 15, 16]))
        tmpl, mode, off = UP_REM[i % len(UP_REM)]
        if mode == "tomorrow":
            day, period = now.date() + D(days=1), "tomorrow"
        else:
            wd = rng.choice(WEEKDAYS)
            day = wd_at(now, wd, "at 9am").date()
            # 2..6 days out: get_upcoming(period=<weekday>) reads exactly that coming day (a same-weekday word reads TODAY)
            if not 2 <= (day - now.date()).days <= 6:
                return None
            period = wd
        title, loc = UP_EVENTS[(i + rng.randint(0, 3)) % len(UP_EVENTS)]
        first = dt.datetime.combine(day, dt.time(rng.choice([10, 11, 13]), rng.choice([0, 30])))
        w.events = [e for e in w.events if e.start.date() != day] + [
            Event(title, first, 60, loc), Event("Library help desk shift", first + D(hours=5), 180, "Hollis Library")]
        w.reminders = [r for r in w.reminders if not r.due or r.due.date() != day]
        if mode == "wd_night":
            d, rtitle = dt.datetime.combine(day - D(days=1), dt.time(20, 0)), f"Prep for {title}"
            final = _pick(rng, [f"I'll remind you {dphr(d, now)} to prep for {title}, which is {dphr(first, now)}.",
                                f"Set. {title} is {dphr(first, now)}, and your prep reminder is {dphr(d, now)}."])
        else:
            d, rtitle = first - D(minutes=off), f"Get ready for {title}"
            on = "tomorrow" if period == "tomorrow" else "on " + period.capitalize()
            final = _pick(rng, [f"Your first thing {on} is {title} at {clock(first)}. I'll remind you at {clock(d)}.",
                                f"{title} is first {on}, at {clock(first)}, and you'll get a reminder at {clock(d)}."])
        prompt = style(rng, tmpl.format(wd=pname(rng, period) if period != "tomorrow" else ""))
        steps = [[Call("get_upcoming", period=period)], [Call("add_reminder", title=rtitle, when_iso=iso(d))], Final(final)]
        return mk("stub_then_call", prompt, now, w, steps, "read the day (stub), then a reminder timed from the result",
                  cards=[], must_see=[title, first.strftime("%A")] if mode != "tomorrow" else [title])
    return make


def _stub_weather_text(i):
    asks = ["text {who} the weather for friday", "tell {who} what the weather is saturday for the run",
            "text {who} the forecast for tomorrow", "send {who} the weather for sunday",
            "text {who} what its like outside tomorrow", "tell {who} what the weather's doing today"]
    whos = ["Jordan", "Ben", "Aisha", "Carmen", "Theo", "Maya"]

    def make(rng):
        now, w = base_world(rng)
        tmpl = asks[i % len(asks)]
        who = whos[(i + rng.randint(0, 1)) % len(whos)]
        wx = w.weather["Portland,OR"]
        key = next(k for k in ("friday", "saturday", "sunday", "tomorrow", "today") if k in tmpl)
        if key in ("tomorrow", "today"):
            idx = 1 if key == "tomorrow" else 0
        else:
            idx = (wd_at(now, key, "morning").date() - now.date()).days    # a same-weekday word is NEXT week, off the 3-day forecast
            if idx not in (1, 2):
                return None
        d = wx["days"][idx]
        when_w = "today" if idx == 0 else "tomorrow" if idx == 1 else key.capitalize()
        on = when_w if idx < 2 else f"on {when_w}"
        text = (f"{cap(when_w)} in Portland: {d[3].lower()}, high {d[1]}°F, low {d[2]}°F"
                + (f", {d[4]}% chance of rain." if d[4] >= 40 else "."))
        prompt = style(rng, tmpl.format(who=who.lower()))
        wcall = Call("get_weather", location="Portland,OR", **({"period": when_w.lower()} if idx else {}))
        final = _pick(rng, [f"Texted {who} the forecast: {d[3].lower()} with a high of {d[1]}°F {on}.",
                            f"{who} has the forecast: {d[3].lower()}, high of {d[1]}°F {on}.",
                            f"Sent. {who} knows to expect {d[3].lower()} {on}, high of {d[1]}°F.",
                            f"I sent {who} the weather for {when_w}: {d[3].lower()}, high of {d[1]}°F."])
        return mk("stub_then_call", prompt, now, w, [[wcall], [Call("send_message", to=who, text=text)], Final(final)],
                  "send tool is a stub after the weather read: load, then the real send (card approved)",
                  approve=[True], cards=["send_message"], must_see=[d[3]])
    return make


TEXT_REM = [("text {who} that I'm bringing the slides and add a reminder to print them tomorrow", "Hana", "I'm bringing the slides.", "Print the slides", "tomorrow"),
            ("text {who} im on my way and remind me tonight to charge my phone", "Ben", "I'm on my way.", "Charge my phone", "tonight"),
            ("message {who} that i got the tickets and remind me sunday to print them", "Ben", "I got the tickets.", "Print the tickets", "sunday"),
            ("tell {who} i'll be there at 7 and set a reminder for 6 to leave", "Theo", "I'll be there at 7.", "Leave for Theo's", "6")]


def _stub_text_reminder(i):
    def make(rng):
        now, w = base_world(rng, hour=rng.choice([9, 10, 11, 13, 14, 15, 16]))
        tmpl, who, text, rtitle, when = TEXT_REM[i % len(TEXT_REM)]
        if when == "tomorrow":
            d = at(now, "tomorrow")
        elif when == "tonight":
            d = at(now, "tonight")
        elif when == "sunday":
            d = wd_at(now, "sunday", "morning")
        else:
            d = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if d <= now + D(minutes=10):
            return None
        prompt = style(rng, tmpl.format(who=who.lower()))
        t = second(rtitle[0].lower() + rtitle[1:])
        final = _pick(rng, [f"{who} has your text, and I'll remind you {dphr(d, now)} to {t}.",
                            f"I texted {who} and set a reminder to {t} for {bare(d, now)}.",
                            f"Sent to {who}. You'll get a nudge {dphr(d, now)} to {t}.",
                            f"Your text is on its way, and a reminder to {t} is set for {bare(d, now)}."])
        steps = [[Call("send_message", to=who, text=text)], [Call("add_reminder", title=rtitle, when_iso=iso(d))], Final(final)]
        return mk("stub_then_call", prompt, now, w, steps, "two writes, the first a stub (card), then the reminder",
                  approve=[True], cards=["send_message"])
    return make


ORG_MAILS = [
    ("what did the registrar send me", "registrar", "Office of the Registrar", "registrar@linden.edu",
     "Spring registration opens Monday", "Spring registration opens Monday at 8 AM. Please clear any holds before then.",
     "The registrar says spring registration opens Monday at 8 AM, and you should clear any holds before then."),
    ("what did the advising office send me", "advising", "Advising Office", "advising@linden.edu",
     "Book your spring advising meeting", "Please book a 30-minute advising appointment before November 20.",
     "The advising office wants you to book a 30-minute advising appointment before November 20."),
    ("what did the financial aid office send me", "financial aid", "Financial Aid Office", "finaid@linden.edu",
     "Verification documents needed", "We still need a signed copy of your tax form by November 15.",
     "Financial aid still needs a signed copy of your tax form by November 15."),
    ("what did the bookstore send me", "bookstore", "Linden Bookstore", "orders@lindenbookstore.example",
     "Your order is ready for pickup", "Your textbooks are ready for pickup at the front desk.",
     "Your textbooks are ready for pickup at the bookstore's front desk."),
    ("did the professor ever answer about the extension", "okafor", "Prof. Adaeze Okafor", "aokafor@linden.edu",
     "Re: Project 2 extension request", "I can give you until Friday at 11:59 PM for project 2.",
     "Yes, Prof. Okafor replied that you have until Friday at 11:59 PM for project 2."),
    ("what did jordan send me about the writeup", "jordan", "Jordan Ellis", "jellis@linden.edu", "Lab writeup draft",
     "I put our lab writeup draft in the shared folder. Can you do the analysis section by Wednesday?",
     "Jordan put the lab writeup draft in the shared folder and asked if you can do the analysis section by Wednesday."),
]


def _stub_read_email(i):
    def make(rng):
        now, w = base_world(rng)
        prompt, kw, sender, addr, subj, body, final = ORG_MAILS[i % len(ORG_MAILS)]
        w.emails = [Email(sender, addr, subj, now - D(hours=rng.randint(2, 40)), body, True, True)] + gen_news(rng, now)
        return mk("stub_then_call", style(rng, prompt), now, w, [[Call("view_emails", query=kw, count=1)], Final(final)],
                  "read tool starts as a stub: load, then the real read, then an answer from the email",
                  cards=[], must_see=[subj])
    return make


TEXT_READS = [
    ("what did ben text about the long run", "Ben", [("Long run Saturday 8am at Riverside, you in?", 5), ("Bring water, it's 10 miles", 3)],
     "Ben asked if you're in for the long run on Saturday at 8 AM at Riverside, and said to bring water for the 10 miles."),
    ("what did maya send about the landlord", "Maya Chen", [("Landlord is coming Thursday at 10 to fix the sink", 4)],
     "Maya says the landlord is coming Thursday at 10 to fix the sink."),
    ("what did theo text about the game", "Theo Park", [("Bro did you see the game", 6), ("Want to study at the library Sunday?", 3)],
     "Theo asked if you saw the game, and whether you want to study at the library on Sunday."),
    ("what did aisha send about tonight", "Aisha Karim", [("Study group moved to 7 tonight", 3), ("Can you bring your linear algebra notes?", 2)],
     "Aisha says the study group moved to 7 tonight and asked you to bring your linear algebra notes."),
]


def _stub_read_text(i):
    def make(rng):
        now, w = base_world(rng)
        prompt, conv, lines, final = TEXT_READS[i % len(TEXT_READS)]
        w.messages = [m for m in w.messages if m.conv != conv]
        for k, (text, hrs) in enumerate(lines):
            w.messages.append(Msg(conv, conv, text, now - D(hours=hrs + 2 * k), True))
        first = conv.split()[0]
        return mk("stub_then_call", style(rng, prompt), now, w, [[Call("view_messages", query=first)], Final(final)],
                  "read tool starts as a stub: load, then the real read, then an answer from the texts",
                  cards=[], must_see=[lines[0][0]])
    return make


CAL_READS = [
    ("what time did the study group get moved to", "Study group", "study group", "Hollis Library 3B"),
    ("where is the career fair", "Career fair", "career fair", "Student Union Ballroom"),
    ("where is the choir rehearsal", "Choir rehearsal", "choir rehearsal", "Music Annex"),
    ("where did the robotics club move to", "Robotics club", "robotics club", "Kerr Hall 118"),
    ("where is the piano lesson", "Piano lesson", "piano lesson", "Music Annex"),
    ("what room is the project check-in in", "Group project check-in", "project check-in", "Kerr Hall 301"),
]


def _stub_read_cal(i):
    def make(rng):
        now, w = base_world(rng, hour=rng.choice([8, 9, 10, 11, 12, 13]))
        prompt, title, kw, loc = CAL_READS[i % len(CAL_READS)]
        day = now.date() + D(days=rng.randint(1, 5))
        start = dt.datetime.combine(day, dt.time(rng.choice([10, 13, 15, 18]), rng.choice([0, 30])))
        w.events = [e for e in w.events if kw not in e.title.lower()] + [Event(title, start, 90, loc)]
        w.events.sort(key=lambda e: e.start)
        if "where" in prompt or "room" in prompt:
            final = _pick(rng, [f"{title} is at {loc}, {dphr(start, now)}.", f"It's at {loc}, {dphr(start, now)}.",
                                f"You'll find {kw} at {loc} {dphr(start, now)}."])
        else:
            final = _pick(rng, [f"{title} is {dphr(start, now)} at {loc}.", f"You have {kw} {dphr(start, now)}, at {loc}.",
                                f"It moved to {bare(start, now)}, at {loc}."])
        return mk("stub_then_call", style(rng, prompt), now, w,
                  [[Call("get_upcoming", query=kw, days=30, calendar_only=True)], Final(final)],
                  "calendar read starts as a stub (or unoffered): load, then the real read, then the answer",
                  cards=[], must_see=[title])
    return make


# Email bodies hold only what the user said to say (a greeting and a sign-off are the form, not content).
EMAIL_SEND = [
    ("email prof okafor that ill miss lecture", "Prof. Okafor", "Missing lecture", "Hi Professor Okafor,\n\nI'm going to miss lecture.\n\nThanks,\nSam", "Prof. Okafor", "you'll miss lecture"),
    ("mail dana that i can cover saturday", "Dana", "Saturday shift", "Hi Dana,\n\nI can cover the Saturday shift.\n\nThanks,\nSam", "Dana", "you can cover Saturday"),
    ("email theo that i booked the climbing gym and remind me monday to pack my harness", "Theo", "Climbing gym", "Hi Theo,\n\nI booked the climbing gym.\n\nSam", "Theo", "you booked the climbing gym"),
    ("email carmen that i'm in for the gift and remind me sunday to pay her", "Carmen", "Mom's gift", "Hi Carmen,\n\nI'm in for the gift.\n\nSam", "Carmen", "you're in for the gift"),
    ("email hana that i'll be late to office hours and remind me tomorrow to bring my laptop", "Hana", "Running late", "Hi Hana,\n\nI'll be late to office hours.\n\nThanks,\nSam", "Hana", "you'll be late to office hours"),
]
EMAIL_REM = {2: ("Pack my harness", "monday morning"), 3: ("Pay Carmen for the gift", "sunday morning"),
             4: ("Bring my laptop", "tomorrow")}


def _stub_email_send(i):
    def make(rng):
        now, w = base_world(rng, hour=rng.choice([9, 10, 11, 13, 14, 15]))
        prompt, to, subj, body, who, gist = EMAIL_SEND[i % len(EMAIL_SEND)]
        steps = [[Call("send_email", to=to, subject=subj, body=body)]]
        leads = [f"Sent. {who} now knows {gist}", f"I emailed {who} that {gist}", f"Your email to {who} is on its way ({gist})"]
        notes = "email send starts as a stub (card approved): load, real call, final from the receipt"
        if i % len(EMAIL_SEND) in EMAIL_REM:
            rtitle, rwhen = EMAIL_REM[i % len(EMAIL_SEND)]
            d = wd_at(now, rwhen.split()[0], rwhen.split()[1]) if rwhen != "tomorrow" else at(now, rwhen)
            steps.append([Call("add_reminder", title=rtitle, when_iso=iso(d))])
            final = f"{_pick(rng, leads)}, and I'll remind you {dphr(d, now)} to {second(rtitle[0].lower() + rtitle[1:])}."
            notes = "email stub then a reminder: two writes, the first behind a card"
        else:
            final = _pick(rng, leads) + "."
        steps.append(Final(final))
        return mk("stub_then_call", style(rng, prompt), now, w, steps, notes, approve=[True], cards=["send_email"])
    return make


# stub_misc(0): the note's content and the text come from the user's own words, nothing invented
NOTE_TEXT_BANK = [
    ("save a note that the guest wifi is LindenGuest-5G, then text it to {who}", "Guest wifi", "LindenGuest-5G",
     "The guest wifi is LindenGuest-5G."),
    ("jot down that the study room is Hollis 3B and send that to {who}", "Study room", "Hollis 3B",
     "The study room is Hollis 3B."),
    ("make a note that the meeting spot is the north entrance, then message {who} the same thing", "Meeting spot",
     "North entrance", "The meeting spot is the north entrance."),
    ("write down that the potluck is at 6 on Friday and text {who}", "Potluck", "6 on Friday",
     "The potluck is at 6 on Friday."),
]


def _stub_misc(i):
    def make(rng):
        now, w = base_world(rng)
        if i == 0:
            tmpl, title, body, text = rng.choice(NOTE_TEXT_BANK)
            who = rng.choice(["Maya", "Theo", "Carmen", "Ben"])
            steps = [[Call("create_note", title=title, body=body)], [Call("send_message", to=who, text=text)],
                     Final(_pick(rng, [f"Saved a note called “{title}” and texted the same to {who}.",
                                       f"The note “{title}” is saved, and {who} has the text.",
                                       f"I made the note and sent {who} the same details."]))]
            return mk("stub_then_call", style(rng, tmpl.format(who=who.lower())), now, w, steps,
                      "two stubs: note, then a card text; both use only the user's own words",
                      approve=[True], cards=["send_message"])
        if i == 1:
            w.events = [e for e in w.events if e.start.date() != now.date() + D(days=1)]
            start = dt.datetime.combine(now.date() + D(days=1), dt.time(15, 0))
            w.events.append(Event("Study group", start, 60, "Hollis Library"))
            new = start + D(hours=1)
            prompt = "what time is my study group, push it back an hour"
            steps = [[Call("get_upcoming", query="study group", days=14, calendar_only=True)],
                     [Call("update_event", title="Study group", when_iso=iso(new))],
                     Final(_pick(rng, [f"Your study group was at {clock(start)} tomorrow, and it's now at {clock(new)}.",
                                       f"It was at {clock(start)} tomorrow. I moved it to {clock(new)}."]))]
            return mk("stub_then_call", style(rng, prompt), now, w, steps, "read (stub) then update_event (stub, card)",
                      approve=[True], cards=["update_event"], must_see=["Study group"])
        if i == 2:
            prompt = rng.choice(["prepare an email to hana about the regrade for me to review",
                                 "prepare an email to aisha about the study guide for me to review",
                                 "get an email ready for dana about the saturday shift, i want to look it over"])
            if "hana" in prompt:
                args = dict(to="Hana Kowalczyk", subject="Regrade",
                            body="Hi Hana,\n\nI wanted to ask about the regrade.\n\nBest,\nSam")
                who, subj = "Hana", "Regrade"
            elif "dana" in prompt:
                args = dict(to="Dana Whitfield", subject="Saturday shift",
                            body="Hi Dana,\n\nI wanted to ask about the Saturday shift.\n\nBest,\nSam")
                who, subj = "Dana", "Saturday shift"
            else:
                args = dict(to="Aisha Karim", subject="Study guide",
                            body="Hi Aisha,\n\nI wanted to ask about the study guide.\n\nBest,\nSam")
                who, subj = "Aisha", "Study guide"
            final = _pick(rng, [f"I've opened a draft to {who} in Mail with the subject “{subj}”. Nothing's been sent, so you can edit it first.",
                                f"The draft to {who} is in Mail, subject “{subj}”. It hasn't been sent.",
                                f"Your draft for {who} is ready in Mail. Nothing has gone out yet."])
            return mk("stub_then_call", style(rng, prompt), now, w, [[Call("draft_email", **args)], Final(final)],
                      "draft tool starts as a stub: load, then the real draft; never claims it was sent", cards=[])
        prompt = "email aisha that ill bring dessert, and set a reminder for friday to bake it"
        d = wd_at(now, "friday", "morning")
        body = "Hi Aisha,\n\nI'll bring dessert.\n\nThanks,\nSam"
        steps = [[Call("send_email", to="Aisha Karim", subject="Dessert", body=body)],
                 [Call("add_reminder", title="Bake the dessert", when_iso=iso(d))],
                 Final(_pick(rng, [f"Sent to Aisha, and I'll remind you {dphr(d, now)} to bake the dessert.",
                                   f"Your email is on its way to Aisha. The reminder to bake the dessert is set for {bare(d, now)}."]))]
        return mk("stub_then_call", style(rng, prompt), now, w, steps,
                  "send tool not offered (get_tool_schemas), reminder a stub", approve=[True], cards=["send_email"])
    return make


def fam_stub_then_call(rng):
    out = []
    plan = [(_stub_search, 9, 5), (_stub_update, 7, 5), (_stub_read_email, 6, 4), (_stub_read_text, 4, 3),
            (_stub_read_cal, 6, 3), (_stub_mail_reminder, 6, 4), (_stub_upcoming_reminder, 4, 3),
            (_stub_weather_text, 6, 4), (_stub_text_reminder, 4, 3), (_stub_email_send, 5, 4), (_stub_misc, 4, 4)]
    for factory, n_cand, quota in plan:
        makers = [factory(i) for i in range(n_cand)]
        _fill(out, quota, makers, rng, "stub_then_call", need_load=True)
    return out


FAMILIES_R2 = {
    "act_dont_ask": fam_act_dont_ask,
    "finish_the_write": fam_finish_the_write,
    "stub_then_call": fam_stub_then_call,
}

WRITE_ALLOW_R2 = {
    "act_dont_ask": frozenset({"add_reminder", "add_calendar_event", "send_message", "schedule_send", "create_note"}),
    "finish_the_write": frozenset({"manage_contacts", "send_message", "update_event", "complete_reminder",
                                   "update_reminder", "draft_email", "reply_to_email"}),
    "stub_then_call": frozenset({"add_reminder", "update_reminder", "send_message", "send_email", "draft_email",
                                 "create_note", "update_event"}),
}
