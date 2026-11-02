"""Round-3 family ``followup_r3``: a follow-up that only makes sense with the turn before it.

Why this family exists
----------------------
r2 trained single-turn only ("follow-ups not covered" in the README). Toolbench
``followup`` was 32/56 for Gemma (run 20261101-151918, 2 trials x 28 cases, 14 of the
28 cases failed both trials). The failing rows fall into these causes:

1. Asks instead of using the turn before. "send this to mom" / "send this to dad" /
   "remind me thursday night ..." got "what would you like the message to say?" or
   "what time on Thursday night?" although the previous assistant turn held the text
   and the user's own time words were enough. (fu_send_this_to_*, fu_remind_pickup)
2. Detours through a lookup. "tell him yes" / "messages pls" / "sure" ran
   ``lookup_contact`` (or ``get_upcoming``) first and stopped, or sent after the
   lookup with invented argument names. (fu_sure_after_offer, fu_channel_messages_*)
3. Wrong argument names on the write. ``message`` / ``recipient`` / ``conversation`` /
   ``contact_number`` instead of ``text`` / ``to``; ``content`` / ``due_date`` or
   ``reminder`` / ``date`` instead of ``title`` / ``when_iso`` on update_reminder.
4. Wrong recipient or channel: the person came from the wrong turn ("+1 555..."
   for "Sam"), or draft_email where send_email was wanted (fu_email_address_answer).
5. Ungrounded follow-up answers. "how much is it?" / "open the second one" answered
   from the header-only digest or from nothing instead of re-reading the email with
   ``view_emails`` (fu_how_much, fu_second_email); "first thing I asked" read the
   wrong store; "compare to last week" used web_search where get_stock_price fits.
6. False receipts. "I haven't sent or scheduled a message in this turn." after a
   send that did run, or "I've updated that" after only a search_reminders.
7. Reads where a write was asked (fu_reminder_i_mean_today stopped at
   search_reminders; the update never came).

Training here: after a short real prior exchange, the ideal answers the follow-up
with the right tool in the first step, the exact schema names, the referent taken
from the prior turn (and from the host's "Current subject" line), the user's own
words passed through, the card shown for sends and calendar writes, and a final
that states only what the receipt says. When the referent is truly missing or two
readings differ in effect, the ideal asks ONE short question.

How a prior turn is carried
---------------------------
``POST /agent`` continues a chat when it gets a ``session_id``; ``harness.run_scenario``
sends none. This module needs NO shared-file edit: it wraps ``store.create_session``
(the real ``SessionStore`` instance ``service.main`` calls for a new turn) so that,
when the world about to run has a ``history`` attribute, the new session is created
with those turns already in it, in the same shape toolbench's ``_seed_session`` uses
(``store.add_turn`` for each side, then ``subject.record_turn`` from the turn's
``calls``). The request the builder captures is therefore exactly what production
builds on a follow-up: the stored history, the "Current subject" system line from
#424's ``service/memory/subject.py``, and the extra name-only tools that subject
offers. The wrapper is a no-op for every world without ``history``.

Host behaviour measured while building (see ``DROPPED``): a bare assent
("yes", "sure", "ok") after a question is a read-only turn
(``continuation_requires_baseline``), so assent examples carry the content of the
answer ("yes, text her I'll be there"); the bare shapes are not trained as writes.

Also measured (dry-run drops, listed in DROPPED): the host refuses ``update_event`` with
"the user did not ask for anything to be moved or rescheduled" on some natural phrasings
("can we do it at 4 pm", "let's say 3pm instead", "bring it forward half an hour", "can
you start it an hour sooner") and treats "do it on Tuesday instead, same time" as a
question. Those are production false negatives, not data: only phrasings the host runs
are trained, and the gap should be fixed in the intent gate.

Needs two shared-file changes outside this module (see the report): ``writes.MULTITURN``
plus the two validate.py edits that accept earlier user turns, and ``followup_r3`` in
``thinking.WRITE_FAMILIES`` so referent-resolving writes think.
"""
from __future__ import annotations

import copy
import datetime as dt
import random
import re

import harness as H
from harness import Call, Final
from replies import clock, day_word, join, agenda_reply
from scen import Scenario, person_texts
from world import Email, Event, Msg, Reminder
import scen_r2_act as A
from scen_r2_act import base_world, style, iso, dphr, bare, clk, WEEKDAYS
import leak

D = dt.timedelta
FAMILY = "followup_r3"
DROPPED: list[tuple[str, str, str]] = []

WRITE_ALLOW_R3 = {FAMILY: frozenset({"send_message", "update_event", "update_reminder", "complete_reminder",
                                     "add_reminder", "add_calendar_event", "reply_to_email", "cancel_event"})}


# =============================================================================
# prior turns: seed the session the /agent route will create
# =============================================================================
from service.memory.store import store as _store  # noqa: E402
from service.memory.subject import record_turn as _record_turn  # noqa: E402

_real_create_session = _store.create_session


def _create_session_with_history() -> str:
    sid = _real_create_session()
    world = H.CURRENT.get("world")
    for turn in getattr(world, "history", None) or ():
        _store.add_turn(sid, "user", turn["user"])
        _store.add_turn(sid, "assistant", turn["assistant"],
                        tool_digest=turn.get("tools") or None)
        calls = [c for c in turn.get("calls") or ()]
        _record_turn(_store, sid, [{"name": c["name"], "args": c.get("args") or {}} for c in calls],
                     [{"name": c["name"], "result": c.get("result") or ""} for c in calls])
    return sid


_store.create_session = _create_session_with_history


def turn(user, assistant, tools=None, calls=None):
    return {"user": user, "assistant": assistant, "tools": tools, "calls": calls or []}


# =============================================================================
# plumbing: dry-run every candidate the way build.py will
# =============================================================================
_SEEN: set[str] = set()
_OPEN: dict[str, int] = {}
OPEN_CAP = 5


def _leaks():
    return A._leaks()


def _hist_text(sc) -> str:
    out = []
    for t in getattr(sc, "history", None) or ():
        out += [t["user"], t["assistant"]]
        for c in t.get("calls") or ():
            out.append(str(c.get("args")))
    return "\n".join(out)


def _check(sc):
    ok, why = A._check(sc)
    if not ok:
        return ok, why
    hits = leak.fact_hits(_hist_text(sc))
    if hits:
        return False, f"toolbench fact in history {hits[:2]}"
    for t in getattr(sc, "history", None) or ():
        leaked, score, q = _leaks().leaks(t["user"])
        if leaked:
            return False, f"history prompt leaks {q['id']} {score:.2f}"
    return True, ""


def _okey(text):
    return A._okey(text)


def _pick(rng, opts):
    opts = list(opts)
    ok = [o for o in opts if _OPEN.get(_okey(o), 0) < OPEN_CAP]
    return rng.choice(ok or opts)


def _attempt(make, rng, tries=5):
    for _ in range(tries):
        seed = rng.random()
        sc = make(random.Random(seed))
        if sc is None or sc.prompt.strip().lower() in _SEEN:
            continue
        ok, why = _check(sc)
        if ok:
            _SEEN.add(sc.prompt.strip().lower())
            kept = make(random.Random(seed))
            if kept.steps and isinstance(kept.steps[-1], Final):
                k = _okey(kept.steps[-1].text)
                _OPEN[k] = _OPEN.get(k, 0) + 1
            return kept
        DROPPED.append((FAMILY, sc.prompt, why))
    return None


def fu(prompt, now, w, steps, notes, history, *, approve=None, cards=(), must_see=()):
    w.history = history
    sc = Scenario(FAMILY, prompt, now, w, steps, notes)
    sc.history = history
    sc.approve = approve
    sc.expect_cards = list(cards)
    sc.must_see = list(must_see)
    return sc


def blank(rng, hour=None):
    """A world the scenario fully controls: no events, reminders, mail, texts or notes."""
    kw = {} if hour is None else {"hour": hour}
    now, w = base_world(rng, **kw)
    w.events, w.reminders, w.messages, w.emails, w.notes = [], [], [], [], []
    return now, w


def nextday(now, wd_index, min_days=1):
    n = (wd_index - now.weekday()) % 7
    while n < min_days:
        n += 7
    return now.date() + D(days=n)


def wd_name(d):
    return d.strftime("%A")


def at_(day, h, m=0):
    return dt.datetime.combine(day, dt.time(h, m))


EVENT_BANK = [
    ("CS 162 lecture", 10, 0, 50, "Kerr Hall 204"), ("Study group with Aisha", 14, 0, 120, "Hollis Library, room 3B"),
    ("Dentist appointment", 15, 30, 60, "Pine Street Dental"), ("Coffee with Theo", 9, 30, 60, "Brewed Awakening"),
    ("Linear algebra class", 13, 0, 75, "Avery Science 110"), ("Library help desk shift", 16, 0, 240, "Hollis Library"),
    ("Haircut", 17, 0, 45, "Clipper Joe's"), ("Advising appointment", 11, 0, 30, "Dean's Office, Mercer Hall"),
    ("Office hours with Prof. Okafor", 15, 0, 45, "Kerr Hall 412"), ("Project sync with Jordan", 12, 30, 60, "Kerr Hall 301"),
    ("Dinner with Carmen", 18, 30, 90, "Golden Lotus"), ("Yoga class", 8, 30, 60, "Rec Center Studio B"),
]


# =============================================================================
# 1. day pivot: "and tomorrow?" after a day's agenda
# =============================================================================
ASK_DAY_HIST = ["what have I got going on {w}", "anything lined up for {w}", "what's my schedule {w}",
                "how does {w} look", "what's planned for {w}"]
PIVOT_DAY = {"and tomorrow?": "tomorrow", "what about tomorrow": "tomorrow", "how about tomorrow?": "tomorrow",
             "and the day after that?": "after", "same question for {wd}": "wd", "and {wd}?": "wd",
             "what about {wd}?": "wd", "how about {wd} then": "wd", "ok and {wd}": "wd"}


def day_pivot(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([6, 7, 8]))
        opts = [("today", 0, "tomorrow", 1), ("today", 0, "wd", rng.randint(2, 4)),
                ("tomorrow", 1, "after", 2), ("tomorrow", 1, "wd", rng.randint(3, 5)),
                ("wd", 2, "tomorrow", 1), ("wd", 3, "wd", 5)]
        a_kind, a_off, tmpl_kind, b_off = opts[i % len(opts)]
        da, db = now.date() + D(days=a_off), now.date() + D(days=b_off)
        if da == db:
            return None
        for day in (da, db):
            for title, h, m, mins, loc in rng.sample(EVENT_BANK, rng.choice([0, 1, 2, 3])):
                w.events.append(Event(title, at_(day, max(h, 9), m), mins, loc))
        w.events.sort(key=lambda e: e.start)
        a_period = {"today": "today", "tomorrow": "tomorrow"}.get(a_kind, wd_name(da).lower())
        b_period = "tomorrow" if db == now.date() + D(days=1) else wd_name(db).lower()
        a_phrase = a_period if a_period in ("today", "tomorrow") else "on " + wd_name(da)
        b_phrase = "tomorrow" if b_period == "tomorrow" else "on " + wd_name(db)
        rows_a, _, _ = w.upcoming_rows(period=a_period)
        rows_b, _, _ = w.upcoming_rows(period=b_period)
        pool = [k for k, v in PIVOT_DAY.items() if v == tmpl_kind]
        prompt = rng.choice(pool).format(wd=wd_name(db).lower() if rng.random() < 0.7 else wd_name(db))
        hist_user = rng.choice(ASK_DAY_HIST).format(w=a_phrase if a_period in ("today", "tomorrow") else f"on {wd_name(da)}")
        hist = [turn(hist_user, agenda_reply(rows_a, a_phrase, now, rng), "get_upcoming",
                     [{"name": "get_upcoming", "args": {"period": a_period}, "result": ""}])]
        final = agenda_reply(rows_b, b_phrase, now, rng)
        return fu(style(rng, prompt), now, w, [[Call("get_upcoming", period=b_period)], Final(final)],
                  "follow-up changes only the day: same read, new period", hist)
    return make


# =============================================================================
# 2. person pivot in texts and mail
# =============================================================================
PEOPLE_TEXT = [("Maya", "Maya Chen", ["are we still on for pizza tonight", "grabbed your package from the lobby",
                                      "can you cover the dishes this week", "landlord is coming by at noon"]),
               ("Theo", "Theo Park", ["climbing at 6 tomorrow?", "did you finish the problem set", "i'm at the coffee place if you want to join",
                                      "bringing the extra rope"]),
               ("Carmen", "Carmen Rivera", ["call me when you're free", "mom wants to know about thanksgiving", "sent you the photos",
                                            "good luck on the midterm"]),
               ("Mom", "Mom", ["did you eat today", "your dad says hi", "send me your flight details when you have them",
                               "don't forget to call grandma"])]


def person_pivot_text(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 11, 13, 15]))
        (a, a_full, a_txt), (b, b_full, b_txt) = rng.sample(PEOPLE_TEXT, 2)
        has_b = i % 4 != 3
        for who, full, bank in ((a, a_full, a_txt), (b, b_full, b_txt)):
            if who == b and not has_b:
                continue
            for k, text in enumerate(rng.sample(bank, 2)):
                w.messages.append(Msg(full, who, text, now - D(hours=rng.randint(2, 20) + 24 * k)))
        theirs_a = sorted([m for m in w.messages if m.conv == a_full], key=lambda m: m.when, reverse=True)
        theirs_b = sorted([m for m in w.messages if m.conv == b_full], key=lambda m: m.when, reverse=True)
        hist = [turn(rng.choice([f"has {a.lower()} messaged me", f"any news from {a.lower()}", f"did {a} text",
                                 f"what did {a} say"]),
                     person_texts(a, theirs_a, now) + ".", "view_messages",
                     [{"name": "view_messages", "args": {"query": a}, "result": ""}])]
        label = b
        prompt = rng.choice([f"what about {b.lower()}?", f"and {b}?", f"{b} too?", f"how about {b.lower()}",
                             f"anything from {b.lower()} as well?"])
        if b == "Carmen" and rng.random() < 0.5:
            prompt = rng.choice(["what about my sister?", "and my sister?"])
        if b == "Mom" and rng.random() < 0.5:
            prompt = rng.choice(["and my mom?", "what about my mom?"])
        reply = (person_texts(b, theirs_b, now) + ".") if theirs_b else \
            _pick(rng, [f"Nothing from {label} lately.", f"I don't see any recent texts from {label}.",
                        f"{label} hasn't texted in the last few days."])
        return fu(style(rng, prompt), now, w, [[Call("view_messages", query=b)], Final(reply)],
                  "same read, other person from the pivot (relation word resolved from the profile)", hist)
    return make


MAIL_BANK = [("Prof. Adaeze Okafor", "aokafor@linden.edu", "Project 2 extension request", "I can give you until Friday at 11:59 PM."),
             ("Linden Registrar", "registrar@linden.edu", "Spring registration opens Monday", "Registration opens Monday at 8 AM."),
             ("Hollis Library", "circulation@linden.edu", "Two items due soon", "Two books are due this Thursday."),
             ("Fernbrook Labs", "recruiting@fernbrooklabs.example.com", "Interview confirmation", "Your interview is confirmed for Wednesday at 2 PM on Zoom."),
             ("Financial Aid Office", "finaid@linden.edu", "Your aid package is ready", "Your spring package is ready to review in the portal.")]


def person_pivot_mail(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 11, 14]))
        (sa, aa, suba, ba), (sb, ab, subb, bb) = rng.sample(MAIL_BANK, 2)
        wa, wb = now - D(hours=rng.randint(3, 30)), now - D(hours=rng.randint(3, 40))
        w.emails = [Email(sa, aa, suba, wa, ba, True, True), Email(sb, ab, subb, wb, bb, True, True)]
        short = lambda s: s.split()[-1] if "Prof." in s else s
        hist_say = f"Yes, {short(sa)} wrote {day_word(wa.date(), now)} at {clock(wa)}: “{suba}”. {ba}"
        hist = [turn(rng.choice([f"is there anything from {short(sa)}", f"did {short(sa)} write back"]),
                     hist_say, "view_emails",
                     [{"name": "view_emails", "args": {"query": short(sa)}, "result": ""}])]
        key = short(sb).split()[-1].lower()
        prompt = rng.choice([f"and {short(sb)}?", f"what about {key}?", f"anything from {key} too?", f"how about the {key} one"])
        final = _pick(rng, [f"{short(sb)} wrote {day_word(wb.date(), now)} at {clock(wb)}: “{subb}”. {bb}",
                            f"Yes. {short(sb)} sent “{subb}” {day_word(wb.date(), now)} at {clock(wb)}. {bb}"])
        return fu(style(rng, prompt), now, w, [[Call("view_emails", query=key)], Final(final)],
                  "mail pivot: same read, new sender", hist)
    return make


# =============================================================================
# 3. place and stock pivots
# =============================================================================
CITIES = [("Seattle", "Seattle,WA", "Seattle, Washington, United States"), ("Boise", "Boise,ID", "Boise, Idaho, United States"),
          ("Denver", "Denver,CO", "Denver, Colorado, United States"), ("Eugene", "Eugene,OR", "Eugene, Oregon, United States"),
          ("Austin", "Austin,TX", "Austin, Texas, United States"), ("Chicago", "Chicago,IL", "Chicago, Illinois, United States")]


def _fline(wx, which, city):
    d = wx["days"][which]
    day = "Today" if which == 0 else "Tomorrow"
    tail = f", {d[4]}% chance of rain" if d[4] else ", no rain expected"
    return f"{day} in {city}: {d[3].lower()}, high {d[1]}° and low {d[2]}°{tail}."


def weather_pivot(i):
    from gen import make_weather
    def make(rng):
        now, w = blank(rng, hour=rng.choice([8, 10, 13]))
        w.weather["Portland,OR"] = make_weather(now, rng)
        if i % 3 == 2:
            hist = [turn(rng.choice(["is it going to rain today", "how's the weather out there"]),
                         _fline(w.weather["Portland,OR"], 0, "Portland"), "get_weather",
                         [{"name": "get_weather", "args": {"location": "Portland,OR"}, "result": ""}])]
            prompt = rng.choice(["and tomorrow?", "what about tomorrow", "tomorrow too?", "how's tomorrow look"])
            final = _fline(w.weather["Portland,OR"], 1, "Portland")
            return fu(style(rng, prompt), now, w, [[Call("get_weather", location="Portland,OR", period="tomorrow")], Final(final)],
                      "weather follow-up keeps the place, changes the day", hist)
        city, code, label = CITIES[(i * 2 + rng.randint(0, 1)) % len(CITIES)]
        w.weather[code] = make_weather(now, rng, code, label)
        hist = [turn(rng.choice(["what's it like outside", "weather check please"]),
                     _fline(w.weather["Portland,OR"], 0, "Portland"), "get_weather",
                     [{"name": "get_weather", "args": {"location": "Portland,OR"}, "result": ""}])]
        prompt = rng.choice([f"and in {city}?", f"what about {city}?", f"how about {city}", f"{city} too?"])
        final = _fline(w.weather[code], 0, city)
        return fu(style(rng, prompt), now, w, [[Call("get_weather", location=code)], Final(final)],
                  "weather pivot: City,ST for the new place", hist)
    return make


STOCK_PAIRS = [("Microsoft", "Costco"), ("Costco", "Nike"), ("Tesla", "Adobe"), ("Starbucks", "Intel"), ("Nike", "Adobe")]


def stock_pivot(i):
    import scen_r2_safe as S
    def make(rng):
        now, w = blank(rng, hour=rng.choice([10, 12, 14]))
        a, b = STOCK_PAIRS[i % len(STOCK_PAIRS)]
        pa, ca = S._stock_world(w, rng, a, now)
        pb, cb = S._stock_world(w, rng, b, now)
        up = lambda c: "up" if c >= 0 else "down"
        hist = [turn(rng.choice([f"how's {a} doing today", f"what is {a} trading at"]),
                     f"{a} is at {pa:.2f}, {up(ca)} {abs(ca):.2f} from yesterday's close.", "get_stock_price",
                     [{"name": "get_stock_price", "args": {"symbols": [a]}, "result": ""}])]
        prompt = rng.choice([f"and {b}?", f"what about {b}", f"how's {b} then", f"{b} too?"])
        final = _pick(rng, [f"{b} is at {pb:.2f}, {up(cb)} {abs(cb):.2f} from yesterday's close.",
                            f"{b} is trading at {pb:.2f} right now, {up(cb)} {abs(cb):.2f} on the day."])
        return fu(style(rng, prompt), now, w, [[Call("get_stock_price", symbols=[b])], Final(final)],
                  "stock pivot: get_stock_price for the new name, not web_search", hist)
    return make


# =============================================================================
# 4. move it / make it / correct it
# =============================================================================
MOVE_TITLES = [("Study group with Aisha", "study group", 120, "Hollis Library, room 3B"),
               ("Coffee with Theo", "coffee with Theo", 60, "Brewed Awakening"),
               ("Project sync with Jordan", "project sync", 60, "Kerr Hall 301"),
               ("Advising appointment", "advising appointment", 30, "Dean's Office, Mercer Hall"),
               ("Haircut", "haircut", 45, "Clipper Joe's"), ("Dinner with Carmen", "dinner with Carmen", 90, "Golden Lotus")]


def move_event(i, deny=False):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([8, 9, 10, 11]))
        title, kw, mins, loc = MOVE_TITLES[i % len(MOVE_TITLES)]
        day = now.date() + D(days=rng.randint(1, 4))
        h = rng.choice([10, 11, 13, 14, 15, 16])
        start = at_(day, h, rng.choice([0, 30]))
        w.events = [Event(title, start, mins, loc)]
        mode = (i + int(deny)) % 5
        if mode == 0:
            nh = rng.choice([x for x in (9, 12, 15, 16, 17) if x != start.hour])
            new = at_(day, nh)
            ask = rng.choice(["move it to {c}", "make it {c} instead", "change it to {c}", "reschedule it to {c}"]).format(
                c=clk(nh, 0, rng.choice([0, 1])))
        elif mode == 1:
            d = rng.choice([30, 60])
            new = start + D(minutes=d)
            amt = "half an hour" if d == 30 else "an hour"
            ask = rng.choice(["push it back {a}", "make it {a} later", "shift that {a} later"]).format(a=amt)
        elif mode == 2:
            d = rng.choice([30, 60])
            new = start - D(minutes=d)
            amt = "half an hour" if d == 30 else "an hour"
            ask = rng.choice(["move it {a} earlier", "make it {a} earlier", "make it start {a} earlier"]).format(a=amt)
        elif mode == 3:
            new = start + D(days=1)
            ask = rng.choice(["move that to the next day", "push it a day, same time", "move it a day later"])
        else:
            other = nextday(now, (day.weekday() + rng.randint(2, 4)) % 7, 1)
            new = dt.datetime.combine(other, start.time())
            if new <= now + D(minutes=30):
                return None
            ask = rng.choice(["move it to {wd}, same time", "change it to {wd} at the same time"]).format(wd=wd_name(other))
        if new <= now + D(minutes=30):
            return None
        hist = [turn(rng.choice([f"when's my {kw}", f"what time is the {kw}"]),
                     f"{title} is {dphr(start, now)}.", "get_upcoming",
                     [{"name": "get_upcoming", "args": {"query": kw}, "result": ""}])]
        move = Call("update_event", title=title, when_iso=iso(new))
        if deny:
            return fu(style(rng, ask), now, w, [[move]], "follow-up move; the card is denied, so the turn ends at the call",
                      hist, approve=[False], cards=["update_event"])
        final = _pick(rng, [f"Moved {title} to {bare(new, now)}.", f"{title} is now {dphr(new, now)}.",
                            f"Okay, {title} starts {dphr(new, now)} now.", f"Done. {title} is set for {bare(new, now)}."])
        return fu(style(rng, ask), now, w, [[move], Final(final)],
                  "move the event the previous turn named; day taken from that turn, card approved", hist,
                  approve=[True], cards=["update_event"])
    return make


def correct_event(i):
    """The event was just added; the user fixes the time or the day."""
    def make(rng):
        now, w = blank(rng, hour=rng.choice([8, 9, 10]))
        title, kw, mins, loc = MOVE_TITLES[(i + 2) % len(MOVE_TITLES)]
        day = nextday(now, rng.randint(0, 6), 1)
        h = rng.choice([9, 11, 13, 16])
        start = at_(day, h)
        w.events = [Event(title, start, mins, loc)]
        added = Call("add_calendar_event", title=title, when_iso=iso(start))
        hist = [turn(f"put {kw} on {wd_name(day).lower()} at {clk(h, 0, 0)}",
                     f"Added {title} on {wd_name(day)} at {clock(start)}.", "add_calendar_event",
                     [{"name": "add_calendar_event", "args": {"title": title, "when_iso": iso(start)}, "result": ""}])]
        if i % 2 == 0:
            nh = rng.choice([x for x in (10, 12, 14, 15, 17) if x != h])
            new = at_(day, nh)
            ask = rng.choice(["no, {c} not {o}", "sorry, I meant {c}", "make that {c} please"]).format(
                c=clk(nh, 0, 0), o=clk(h, 0, 0))
        else:
            other = nextday(now, (day.weekday() + rng.randint(1, 3)) % 7, 1)
            new = dt.datetime.combine(other, start.time())
            ask = rng.choice(["I meant {wd}", "wrong day, it's {wd}", "actually {wd}, not {od}"]).format(
                wd=wd_name(other), od=wd_name(day))
        if new <= now + D(minutes=30):
            return None
        final = _pick(rng, [f"Fixed. {title} is on {wd_name(new)} at {clock(new)}.", f"Changed it: {title}, {dphr(new, now)}.",
                            f"Okay, {title} now starts {dphr(new, now)}."])
        return fu(style(rng, ask), now, w, [[Call("update_event", title=title, when_iso=iso(new))], Final(final)],
                  "correction right after an add: update the event just added (title from the host's subject line)", hist,
                  approve=[True], cards=["update_event"])
    return make


REM_THINGS = ["call the dentist", "return the library books", "submit the lab writeup", "pick up the prescription",
              "renew the bus pass", "print the ethics essay", "buy a birthday card for Carmen", "email Prof. Okafor",
              "bring the tripod to class", "refill the printer paper"]


def reminder_fix(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([8, 9, 10, 11]))
        thing = REM_THINGS[i % len(REM_THINGS)]
        title = thing[:1].upper() + thing[1:]
        day = now.date() + D(days=rng.randint(1, 3))
        h = rng.choice([9, 10, 14, 16, 18])
        due = at_(day, h)
        w.reminders = [Reminder(title, due)]
        hist = [turn(f"remind me to {thing} {day_word(day, now).replace('on ', '')} at {clk(h, 0, 0)}",
                     f"Okay, I'll remind you to {thing} {dphr(due, now)}.", "add_reminder",
                     [{"name": "add_reminder", "args": {"title": title, "when_iso": iso(due)}, "result": ""}])]
        mode = i % 4
        if mode == 0:
            nh = rng.choice([x for x in (8, 11, 13, 15, 17, 19) if x != h])
            new = at_(day, nh)
            ask = rng.choice(["make it {c}", "change it to {c}", "can you do {c} instead"]).format(c=clk(nh, 0, 0))
        elif mode == 1:
            new = due + D(hours=1)
            ask = rng.choice(["an hour later please", "push it an hour", "make it an hour later"])
        elif mode == 2:
            other = day + D(days=rng.randint(1, 3))
            new = at_(other, h)
            ask = rng.choice(["actually {wd}", "no, I meant {wd}", "move it to {wd}"]).format(wd=wd_name(other))
        else:
            new = due - D(hours=1)
            if new <= now + D(minutes=10):
                return None
            ask = rng.choice(["an hour earlier", "make it an hour sooner"])
        if new <= now + D(minutes=10):
            return None
        final = _pick(rng, [f"Okay, I'll remind you to {thing} {dphr(new, now)} instead.", f"Updated: {title}, {dphr(new, now)}.",
                            f"Your reminder to {thing} is now {dphr(new, now)}."])
        return fu(style(rng, ask), now, w, [[Call("update_reminder", title=title, when_iso=iso(new))], Final(final)],
                  "adjust the reminder just created: update_reminder(title, when_iso), no card", hist)
    return make


def reminder_done(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 12, 15]))
        thing = REM_THINGS[(i + 3) % len(REM_THINGS)]
        title = thing[:1].upper() + thing[1:]
        due = now + D(hours=rng.randint(2, 6))
        w.reminders = [Reminder(title, due)]
        hist = [turn(rng.choice(["anything due soon", "which reminders are close"]),
                     f"One thing due soon: {title}, at {clock(due)}.", "search_reminders",
                     [{"name": "search_reminders", "args": {}, "result": ""}])]
        ask = rng.choice(["already did that one, tick it off", "I did that already, mark it done", "that's done, clear it"])
        final = _pick(rng, [f"Marked {title} as done.", f"{title} is checked off.", f"Okay, {title} is done."])
        return fu(style(rng, ask), now, w, [[Call("complete_reminder", title=title)], Final(final)],
                  "complete the one reminder the previous turn listed", hist)
    return make


# =============================================================================
# 5. send that / tell him yes / reply
# =============================================================================
WHO = [("Maya", "Maya"), ("Theo", "Theo"), ("Carmen", "my sister"), ("Mom", "my mom"), ("Maya", "my roommate")]


def send_that(i, deny=False):
    from gen import make_weather
    def make(rng):
        now, w = blank(rng, hour=rng.choice([8, 9, 11, 14]))
        to, spoken = WHO[(i + int(deny)) % len(WHO)]
        kind = i % 4
        if kind == 0:
            w.weather["Portland,OR"] = make_weather(now, rng)
            line = _fline(w.weather["Portland,OR"], 1, "Portland")
            hist = [turn(rng.choice(["how's tomorrow's weather", "weather for tomorrow?"]), line, "get_weather",
                         [{"name": "get_weather", "args": {"location": "Portland,OR"}, "result": ""}])]
            text = line
        elif kind == 1:
            evs = rng.sample(EVENT_BANK, 2)
            day = now.date() + D(days=1)
            evs = sorted(evs, key=lambda e: e[1])
            c = [at_(day, e[1], e[2]) for e in evs]
            line = f"Tomorrow you have {evs[0][0]} at {clock(c[0])} and {evs[1][0]} at {clock(c[1])}."
            text = f"Tomorrow I have {evs[0][0]} at {clock(c[0])} and {evs[1][0]} at {clock(c[1])}."
            hist = [turn(rng.choice(["what's on tomorrow", "what does tomorrow hold"]), line, "get_upcoming",
                         [{"name": "get_upcoming", "args": {"period": "tomorrow"}, "result": ""}])]
        elif kind == 2:
            q, a_, t_ = rng.choice([("how many ounces in a cup", "A cup is 8 fluid ounces.", "A cup is 8 fluid ounces."),
                                    ("what's the capital of Montana", "The capital of Montana is Helena.", "The capital of Montana is Helena."),
                                    ("how long should I boil an egg for a soft yolk", "About 6 minutes in boiling water gives a soft yolk.",
                                     "About 6 minutes in boiling water gives a soft yolk."),
                                    ("how late is it in Tokyo right now", "It's tomorrow morning there, so a good time to call is in the evening your time.",
                                     "It's tomorrow morning in Tokyo, so a good time to call is in the evening here.")])
            hist, text = [turn(q, a_)], t_
        else:
            msg = rng.choice([("dinner at the Golden Lotus is at 7:30 on Saturday", "Dinner at the Golden Lotus is at 7:30 on Saturday."),
                              ("the study room is booked for 3 on Wednesday", "The study room is booked for 3 on Wednesday."),
                              ("parking pass pickup is at the front desk until 5", "Parking pass pickup is at the front desk until 5.")])
            pers = rng.choice([p for p in ("Theo", "Carmen") if p != to])
            hist = [turn(f"what did {pers} text", f"{pers} wrote: “{msg[0]}”.", "view_messages",
                         [{"name": "view_messages", "args": {"query": pers},
                           "result": f"[Sat Oct 31, 3:12 PM] {pers} Park — {msg[0]}"}])]
            text = msg[1]
        prompt = rng.choice(["send that to {s}", "text that to {s}", "forward that to {s}", "share that with {s}",
                             "send it over to {s}"]).format(s=spoken)
        if deny:
            return fu(style(rng, prompt), now, w, [[Call("send_message", to=to, text=text)]],
                      "send what the last answer said; the card is denied", hist, approve=[False], cards=["send_message"])
        final = _pick(rng, [f"Sent. {to} has it.", f"Done, I texted it to {to}.", f"{to} now has that message.",
                            f"Okay, that's on its way to {to}.", f"Your text to {to} went through."])
        return fu(style(rng, prompt), now, w, [[Call("send_message", to=to, text=text)], Final(final)],
                  "send the previous answer as the text, args to/text, card approved", hist,
                  approve=[True], cards=["send_message"])
    return make


REPLY_TEXTS = [
    ("Theo", "ramen on Saturday at 7?", "Yes, Saturday at 7 works for me.", "tell him that works", "told Theo yes"),
    ("Maya", "can you grab milk on your way back", "Sure, I'll grab milk on my way back.", "tell her sure", "told Maya you will"),
    ("Carmen", "are you coming for thanksgiving", "Yes, I'm coming for Thanksgiving.", "reply yes to her", "said yes to Carmen"),
    ("Theo", "want to climb thursday at 6", "Yes, Thursday at 6 works.", "tell him yes thursday works", "told Theo Thursday works"),
    ("Maya", "dinner at 7 or 8", "Let's do 7.", "tell her 7", "told Maya 7"),
    ("Carmen", "did you see the photos i sent", "I did, they're great.", "tell her I loved them", "told Carmen you loved them"),
]


def reply_text(i, deny=False):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 12, 16, 19]))
        who, incoming, text, ask, gist = REPLY_TEXTS[(i + int(deny)) % len(REPLY_TEXTS)]
        full = {"Theo": "Theo Park", "Maya": "Maya Chen", "Carmen": "Carmen Rivera"}[who]
        w.messages = [Msg(full, who, incoming, now - D(minutes=rng.randint(10, 90)), True)]
        hist = [turn(rng.choice([f"did {who.lower()} text", f"what's the latest from {who.lower()}"]),
                     f"{who} wrote “{incoming}”.", "view_messages",
                     [{"name": "view_messages", "args": {"query": who},
                       "result": f"[Sun Nov 1, 9:10 AM] {full} — {who}: {incoming}"}])]
        if deny:
            return fu(style(rng, ask), now, w, [[Call("send_message", to=who, text=text)]],
                      "reply to the person in the last turn; card denied", hist, approve=[False], cards=["send_message"])
        final = _pick(rng, [f"Sent. {who} has your answer.", f"Okay, I {gist}.",
                            f"Done, I {gist}.", f"Texted {who}: “{text}”"])
        return fu(style(rng, ask), now, w, [[Call("send_message", to=who, text=text)], Final(final)],
                  "short reply to the person the last turn read; no lookup first, exact arg names, card approved", hist,
                  approve=[True], cards=["send_message"])
    return make


def reply_email(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 11, 14]))
        s, a, subj, body = MAIL_BANK[i % len(MAIL_BANK)]
        mid = f"m-{2000 + i}"
        w.emails = [Email(s, a, subj, now - D(hours=3), body, True, True, message_id=mid)]
        short = s.split()[-1] if "Prof." in s else s
        res = f"From: {s} <{a}>\nSubject: {subj}\nMessage-ID: {mid}"
        hist = [turn(f"read the latest from {short.lower()}", f"{short} wrote “{subj}”. {body}", "view_emails",
                     [{"name": "view_emails", "args": {"query": short}, "result": res}])]
        reply = rng.choice([("reply saying thank you", "Thank you, I appreciate it."),
                            ("reply and say I'll be there", "Thanks, I'll be there."),
                            ("reply that sounds good", "That sounds good, thank you.")])
        final = _pick(rng, [f"Replied to {short}.", f"Your reply to {short} is sent.", f"Done, {short} has your reply."])
        return fu(style(rng, reply[0]), now, w, [[Call("reply_to_email", message_id=mid, body=reply[1])], Final(final)],
                  "reply to the email the subject line names (Message-ID from view_emails)", hist,
                  approve=[True], cards=["reply_to_email"])
    return make


# =============================================================================
# 6. assent / answer to Wisp's own question (content included: bare assent is read-only on the host)
# =============================================================================
def assent_offer(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 11, 15]))
        kind = i % 4
        if kind == 0:   # offered a reminder
            ev = rng.choice(EVENT_BANK[:6])
            day = now.date() + D(days=rng.randint(1, 3))
            start = at_(day, ev[1], ev[2])
            w.events = [Event(ev[0], start, ev[3], ev[4])]
            hist = [turn(f"when is the {ev[0].lower()}", f"{ev[0]} is {dphr(start, now)}. Want me to set a reminder for it?",
                         "get_upcoming", [{"name": "get_upcoming", "args": {"query": ev[0]}, "result": ""}])]
            lead = rng.choice([30, 60])
            rt = start - D(minutes=lead)
            if rt <= now + D(minutes=10):
                return None
            amt = "half an hour" if lead == 30 else "an hour"
            ask = rng.choice(["yes please, {a} before", "yes, {a} ahead is good", "sure, remind me {a} before"]).format(a=amt)
            call = Call("add_reminder", title=ev[0], when_iso=iso(rt))
            final = _pick(rng, [f"Okay, I'll remind you {dphr(rt, now)}.", f"Reminder set for {bare(rt, now)}: {ev[0]}.",
                                f"Done. You'll get a nudge {dphr(rt, now)}."])
            return fu(style(rng, ask), now, w, [[call], Final(final)],
                      "user accepts Wisp's reminder offer with the lead time; add_reminder, no card", hist)
        if kind == 1:   # offered to text someone
            who, full = rng.choice([("Maya", "Maya Chen"), ("Theo", "Theo Park"), ("Carmen", "Carmen Rivera")])
            late = rng.choice([10, 15, 20])
            hist = [turn(f"I'm running {late} minutes late for {who.lower()}",
                         f"Got it. Want me to let {who} know you'll be about {late} minutes late?")]
            ask = rng.choice(["yes please text {w} that I'm {n} minutes late", "yes, tell {w} {n} minutes"]).format(w=who, n=late)
            text = f"I'm running about {late} minutes late."
            final = _pick(rng, [f"Sent. {who} knows you're about {late} minutes behind.", f"Done, {who} has your message.",
                                f"I let {who} know."])
            return fu(style(rng, ask), now, w, [[Call("send_message", to=who, text=text)], Final(final)],
                      "user accepts the offer to text; send_message, card approved", hist,
                      approve=[True], cards=["send_message"])
        if kind == 2:   # offered to add to calendar
            day = nextday(now, rng.randint(0, 6), 2)
            h = rng.choice([10, 13, 16])
            start = at_(day, h)
            title = rng.choice(["Study session for the OS midterm", "Resume review with career services", "Run with Theo"])
            hist = [turn(f"I should {title.split(' ', 1)[0].lower()} {title.split(' ', 1)[1].lower()} this week",
                         f"A slot on {wd_name(day)} at {clock(start)} looks free. Should I put it on your calendar?",
                         "find_free_time", [{"name": "find_free_time", "args": {}, "result": ""}])]
            ask = rng.choice(["yes add it for {wd} at {c}", "yes, {wd} at {c} works"]).format(wd=wd_name(day), c=clock(start))
            final = _pick(rng, [f"Added {title} on {wd_name(day)} at {clock(start)}.", f"{title} is on your calendar for {bare(start, now)}."])
            return fu(style(rng, ask), now, w, [[Call("add_calendar_event", title=title, when_iso=iso(start))], Final(final)],
                      "user accepts a calendar offer; add_calendar_event, card approved", hist,
                      approve=[True], cards=["add_calendar_event"])
        # kind 3: offered a reminder for a message that came in
        who, full = rng.choice([("Maya", "Maya Chen"), ("Theo", "Theo Park")])
        w.messages = [Msg(full, who, "bring the textbook tomorrow please", now - D(hours=2))]
        hist = [turn(f"did {who.lower()} write", f"{who} asked you to bring the textbook tomorrow. Want a reminder?",
                     "view_messages", [{"name": "view_messages", "args": {"query": who}, "result": ""}])]
        due = at_(now.date() + D(days=1), 8)
        ask = rng.choice(["yes, tomorrow morning", "yes remind me tomorrow morning"])
        final = _pick(rng, ["Okay, I'll remind you tomorrow morning to bring the textbook.", "Reminder set for tomorrow at 8 AM: bring the textbook."])
        return fu(style(rng, ask), now, w, [[Call("add_reminder", title="Bring the textbook", when_iso=iso(due))], Final(final)],
                  "accepted reminder offer; the user's 'tomorrow morning' resolved to a concrete time", hist)
    return make


def slot_answer(i):
    """Wisp asked who/which; the user answers in a few words and the pending text is sent."""
    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 11, 15]))
        w.contacts.update({"Alex Brandt": ("(503) 555-0191", ""), "Alex Moreno": ("(971) 555-0166", "")})
        kind = i % 3
        if kind == 0:
            who, spoken = rng.choice(WHO)
            line = rng.choice([("Study group moved to room 3B at 4.", "text the study group time"),
                               ("I'll be at the library until 6.", "text where I'll be tonight")])
            hist = [turn(line[1], f"Sure. Who should I send it to?")]
            hist[0]["assistant"] = f"Sure, “{line[0]}” it is. Who should I text it to?"
            ask = rng.choice(["{s} please", "to {s}", "send it to {s}"]).format(s=spoken)
            return fu(style(rng, ask), now, w, [[Call("send_message", to=who, text=line[0])], Final(_pick(rng, [f"Sent to {who}.", f"Okay, {who} has it.", f"Texted it to {who}."]))],
                      "slot-fill: the recipient answers Wisp's question; the pending text comes from the last turn", hist,
                      approve=[True], cards=["send_message"])
        if kind == 1:
            pick_ = rng.choice([("Alex Brandt", "the first one", "Alex Brandt"), ("Alex Moreno", "the second one", "Alex Moreno"),
                                ("Alex Moreno", "moreno", "Alex Moreno"), ("Alex Brandt", "brandt", "Alex Brandt")])
            text = rng.choice(["I'm on my way.", "Running about ten minutes late."])
            hist = [turn(f"text alex: {text[:-1].lower()}", "I have two Alexes: Alex Brandt and Alex Moreno. Which one?")]
            ask = rng.choice(["{p}", "{p} please", "{p}, yeah"]).format(p=pick_[1])
            return fu(style(rng, ask), now, w, [[Call("send_message", to=pick_[2], text=text)], Final(_pick(rng, [f"Sent to {pick_[2]}.", f"Okay, {pick_[2]} has it."]))],
                      "disambiguation answer: send to the contact chosen, with the text from the turn before", hist,
                      approve=[True], cards=["send_message"])
        title = "Study group with Aisha"
        day = now.date() + D(days=2)
        s1, s2 = at_(day, 11), at_(day, 15)
        w.events = [Event(title, s1, 60, "Hollis Library"), Event("Study group review", s2, 60, "Hollis Library")]
        new = at_(day, rng.choice([16, 17]))
        hist = [turn("move the study group", f"There are two on {wd_name(day)}: {title} at {clock(s1)} and Study group review at {clock(s2)}. Which one?")]
        ask = rng.choice(["the review one, make it {c}", "the later one, to {c}"]).format(c=clock(new))
        return fu(style(rng, ask), now, w, [[Call("update_event", title="Study group review", when_iso=iso(new))],
                                              Final(f"Moved Study group review to {bare(new, now)}.")],
                  "which-one answer: the named event is moved", hist, approve=[True], cards=["update_event"])
    return make


# =============================================================================
# 7. grounded pronouns: re-read for the detail
# =============================================================================
def email_detail(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 11, 14]))
        trio = rng.sample(MAIL_BANK, 3)
        stamp = [now - D(hours=2 + 3 * k) for k in range(3)]
        w.emails = [Email(s, a, subj, stamp[k], body, True, True, message_id=f"m-{3000 + i * 3 + k}")
                    for k, (s, a, subj, body) in enumerate(trio)]
        mode = i % 3
        if mode == 0:   # "open the second one"
            lines = "\n".join(f"{k + 1}. {s}: {subj}" for k, (s, a, subj, b) in enumerate(trio))
            res = "\n\n---\n\n".join(f"From: {s} <{a}>\nSubject: {subj}\nMessage-ID: m-{3000 + i * 3 + k}"
                                     for k, (s, a, subj, b) in enumerate(trio))
            hist = [turn(rng.choice(["show unread mail", "what's new in the inbox"]),
                         f"You have three unread:\n{lines}", "view_emails",
                         [{"name": "view_emails", "args": {"unread": True}, "result": res}])]
            s, a, subj, body = trio[1]
            key = s.split()[-1].lower() if "Prof." not in s else "okafor"
            ask = rng.choice(["show me the second one", "read me the second one", "what does number two say"])
            final = _pick(rng, [f"{s} wrote: {body}", f"The second one is from {s}, “{subj}”. {body}"])
            return fu(style(rng, ask), now, w, [[Call("view_emails", query=subj, count=1)], Final(final)],
                      "'the second one' = the second email listed; re-read it with view_emails", hist)
        if mode == 1:
            s, a, subj, body = trio[0]
            short = s.split()[-1] if "Prof." in s else s
            res = f"From: {s} <{a}>\nSubject: {subj}\nMessage-ID: m-{3000 + i * 3}"
            hist = [turn(f"did {short.lower()} email me", f"Yes, {short} sent “{subj}” a couple of hours ago.", "view_emails",
                         [{"name": "view_emails", "args": {"query": short}, "result": res}])]
            ask = rng.choice(["what does it say", "can you read it to me", "what's in it"])
            final = _pick(rng, [f"{short} wrote: {body}", f"It says: {body}"])
            return fu(style(rng, ask), now, w, [[Call("view_emails", query=subj, count=1)], Final(final)],
                      "'it' = the one email just found; read the body, don't guess from the subject", hist)
        s, a, subj, body = trio[2]
        short = s.split()[-1] if "Prof." in s else s
        hist = [turn("check my unread mail", "Three unread: " + join(f"{x[0]}" for x in trio) + ".", "summarize_emails",
                     [{"name": "summarize_emails", "args": {"unread": True}, "result": ""}])]
        ask = rng.choice([f"what did {short.lower()} say exactly", f"read me the one from {short.lower()}"])
        final = _pick(rng, [f"{short} wrote: {body}", f"From {short}: {body}"])
        return fu(style(rng, ask), now, w, [[Call("view_emails", query=short.split()[-1].lower(), count=1)], Final(final)],
                  "digest gave headers only; the detail needs view_emails", hist)
    return make


def event_detail(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([8, 9]))
        title, h, m, mins, loc = EVENT_BANK[(i * 2) % len(EVENT_BANK)]
        day = now.date() + D(days=rng.randint(1, 3))
        start = at_(day, h, m)
        w.events = [Event(title, start, mins, loc)]
        hist = [turn(f"what's happening {day_word(day, now).replace('on ', '')}", f"{title} at {clock(start)}.", "get_upcoming",
                     [{"name": "get_upcoming", "args": {"period": wd_name(day).lower()}, "result": ""}])]
        if i % 2 == 0:
            ask = rng.choice(["where is it", "where's that again", "what's the location"])
            final = _pick(rng, [f"{title} is at {loc}.", f"It's at {loc}."])
        else:
            ask = rng.choice(["when does it end", "what time does that finish", "until when is it"])
            end = start + D(minutes=mins)
            final = _pick(rng, [f"It ends at {clock(end)}.", f"{title} runs until {clock(end)}."])
        return fu(style(rng, ask), now, w, [[Call("get_upcoming", query=title.split()[0], days=7)], Final(final)],
                  "'it' = the event just listed; re-read the calendar for the detail", hist)
    return make


# =============================================================================
# 8. the correction of a read
# =============================================================================
def read_correct(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([7, 8]))
        a, b = rng.sample(range(1, 5), 2)
        da, db = now.date() + D(days=a), now.date() + D(days=b)
        for day in (da, db):
            for title, h, m, mins, loc in rng.sample(EVENT_BANK, rng.choice([1, 2])):
                w.events.append(Event(title, at_(day, max(h, 9), m), mins, loc))
        pa = wd_name(da).lower() if a != 1 else "tomorrow"
        pb = wd_name(db).lower() if b != 1 else "tomorrow"
        ra, _, _ = w.upcoming_rows(period=pa)
        rb, _, _ = w.upcoming_rows(period=pb)
        phr = lambda p, d: p if p == "tomorrow" else "on " + wd_name(d)
        hist = [turn(f"what's on {pa}", agenda_reply(ra, phr(pa, da), now, rng), "get_upcoming",
                     [{"name": "get_upcoming", "args": {"period": pa}, "result": ""}])]
        wdb = wd_name(db) if b != 1 else "tomorrow"
        ask = rng.choice(["sorry, I meant {x}", "no, {x}", "I meant {x}", "oops, {x} not {y}"]).format(x=wdb.lower() if rng.random() < .6 else wdb, y=pa)
        return fu(style(rng, ask), now, w, [[Call("get_upcoming", period=pb)], Final(agenda_reply(rb, phr(pb, db), now, rng))],
                  "correction of the period: same read, the corrected day", hist)
    return make


# =============================================================================
# 9. truly ambiguous: ask one short question (no call)
# =============================================================================
def ask_one(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 12, 16]))
        kind = i % 7
        if kind == 0:
            hist = [turn(rng.choice(["morning, quick check-in", "hey there"]), "Good morning. How can I help?")]
            ask = rng.choice(["send it to Maya", "text that to Theo"])
            q = _pick(rng, ["Send what? Tell me the message and I'll text it.", "I don't have anything to send yet. What should the message say?"])
            return fu(style(rng, ask), now, w, [Final(q)], "nothing in the last turn to send: one question", hist)
        if kind == 1:
            hist = [turn("what's two plus two", "Four.")]
            ask = rng.choice(["move it to 4", "push it back an hour"])
            q = _pick(rng, ["Move what? I'm not sure which event you mean.", "Which event should I move?"])
            return fu(style(rng, ask), now, w, [Final(q)], "no event in context", hist)
        if kind == 2:
            ev = rng.choice(EVENT_BANK[:5])
            start = at_(now.date() + D(days=2), ev[1], ev[2])
            w.events = [Event(ev[0], start, ev[3], ev[4])]
            hist = [turn(f"when's the {ev[0].lower()}", f"{ev[0]} is {dphr(start, now)}.", "get_upcoming",
                         [{"name": "get_upcoming", "args": {"query": ev[0]}, "result": ""}])]
            ask = rng.choice(["move it", "can we reschedule it", "change the time"])
            q = _pick(rng, [f"To what time should I move {ev[0]}?", f"What time would you like instead?"])
            return fu(style(rng, ask), now, w, [Final(q)], "event known, new time missing: one question", hist)
        if kind == 3:
            w.messages = [Msg("Maya Chen", "Maya", "can you call me", now - D(hours=1), True),
                          Msg("Carmen Rivera", "Carmen", "call me when you can", now - D(minutes=40), True)]
            hist = [turn("any texts", "Maya wrote “can you call me” and Carmen wrote “call me when you can”.", "summarize_messages",
                         [{"name": "summarize_messages", "args": {}, "result": ""}])]
            ask = rng.choice(["tell her I'll call at 5", "text her I'm calling soon"])
            q = _pick(rng, ["Do you mean Maya or Carmen?", "Which one: Maya or Carmen?"])
            return fu(style(rng, ask), now, w, [Final(q)], "two people in the last turn: ask which", hist)
        if kind == 4:
            hist = [turn(rng.choice(["ok cool", "alright, noted"]), "Anytime.")]
            ask = rng.choice(["and the other one?", "what about the second one"])
            q = _pick(rng, ["The other what? I'm not sure which list you mean.", "Which one do you mean? We haven't looked at a list yet."])
            return fu(style(rng, ask), now, w, [Final(q)], "no list in context", hist)
        if kind == 5:
            w.reminders = [Reminder("Return the library books", at_(now.date() + D(days=1), 10)),
                           Reminder("Call the dentist", at_(now.date() + D(days=1), 14))]
            hist = [turn("which reminders land tomorrow", "Two reminders: Return the library books at 10 AM and Call the dentist at 2 PM.",
                         "search_reminders", [{"name": "search_reminders", "args": {}, "result": ""}])]
            ask = rng.choice(["move it to the evening", "push that to later"])
            q = _pick(rng, ["Which one: the library books or the dentist call?", "Do you mean the library books or the dentist?"])
            return fu(style(rng, ask), now, w, [Final(q)], "two reminders listed, 'it' is ambiguous", hist)
        hist = [turn(rng.choice(["give me a quick fact", "got a random fact for me"]), "Octopuses have three hearts.")]
        ask = rng.choice(["cancel it", "delete that"])
        q = _pick(rng, ["Cancel what? Nothing on your list came up just now.", "I'm not sure what to cancel. Which event or reminder?"])
        return fu(style(rng, ask), now, w, [Final(q)], "nothing cancellable in context", hist)
    return make


# =============================================================================
# the family
# =============================================================================
def fam_followup(rng):
    out = []
    plan = [(day_pivot, 10), (person_pivot_text, 8), (person_pivot_mail, 4), (weather_pivot, 6), (stock_pivot, 5),
            (move_event, 8), (correct_event, 6), (reminder_fix, 8), (reminder_done, 3), (send_that, 10),
            (reply_text, 6), (reply_email, 4), (assent_offer, 8), (slot_answer, 7), (email_detail, 6),
            (event_detail, 4), (read_correct, 4), (ask_one, 14)]
    extra_deny = [(move_event, 2), (send_that, 2), (reply_text, 1)]
    for maker, n in plan:
        got, tries = 0, 0
        while got < n and tries < n * 6:
            sc = _attempt(maker(tries), rng)
            tries += 1
            if sc is not None:
                out.append(sc)
                got += 1
    for maker, n in extra_deny:
        got, tries = 0, 0
        while got < n and tries < n * 6:
            sc = _attempt(maker(tries + 50, deny=True), rng)
            tries += 1
            if sc is not None:
                out.append(sc)
                got += 1
    return out


FAMILIES_R3 = {FAMILY: fam_followup}
