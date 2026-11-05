"""Round-5 family ``tool_choice_r5``: pick the right tool among near neighbours, carry arguments into a send, and actually
write when asked, with a contrast that must not act.

Evidence (measured on ``~/.moe/evals/toolbench/r4-s1150-full``: 466 public toolbench cases, base vs r4 step 1150, one trial
each; passes 312 for base and 335 for step 1150). Step 1150's failures, by label (a case can carry several):

- ``wrong_tool`` 37 cases. ``get_my_day_plan`` is the call in 12 of them. It answered a calendar-only question
  (cal_sunday), a free-time question (cal_free_tomorrow), two reminder-due questions (rem_due_today, rem_due_soon), and sat
  in front of a send in 8 compose cases (the host's checks there accept only ``get_upcoming`` / ``daily_brief``, which may be
  test strictness and is not trained against: day-plan wording still reads the day plan here). The rest are near neighbours:
  ``recall`` or ``search_notes`` for a fact held in mail or texts (mail_chase_balance, sess_cap_justtime,
  web_priv_order_price, voice_mail_balance, msg_what_i_sent_family), ``remember`` for a contact's number, ``convert_units``
  for a currency, ``set_timer`` for a note, ``add_calendar_event`` for a reminder, ``send_message`` with no read first,
  ``draft_message`` where the user asked to see a text.
- ``no_call`` 19 cases (base: 59). The user asked for a write and the model never called it: cancel, add event,
  schedule_send, set a timer or alarm, "send this to", "I mean today" on a reminder.
- ``bad_args`` 14 cases (base: 8). A send body that says "I haven't looked it up" or drops what the read returned (4),
  ``get_upcoming`` with no ``query`` / unknown ``time_window`` (2), a wrong message id, a missing ``period`` or
  ``strict_match``, a forget query without its keyword, and a label that was not the user's.
- ``unsafe_call`` 5 cases (base: 4): a send or a web call nobody asked for, and ``get_weather`` with no city.
- compose: step 1150 passes 9 of 25, base passes 12 of 25. Of the 16 compose failures, 8 are ``wrong_tool`` (all but one the
  day plan before a send), 4 ``bad_args`` (body not carrying the read), 1 ``no_call``, 1 ``unsafe_call``.

What this family teaches (about 170 turns, one example per model step):

- neighbouring reads: a calendar-only question reads ``get_upcoming(calendar_only=True)``; reminders due read
  ``search_reminders(scope=...)``; "what is my day" reads ``get_my_day_plan``; a free slot reads ``find_free_time``;
  a text is ``view_messages`` / ``summarize_messages``; mail is ``view_emails`` / ``summarize_emails``;
- similar tools: a stock quote is ``get_stock_price`` but a company's CEO, a crypto price or a currency rate is
  ``web_search``; a fact held in mail, texts, notes or saved memory goes to the matching store;
- read-first sends: the read comes before the send, the send uses the channel the user named, and its body holds only what
  the read returned. Each has a read-only contrast ("what is ..." with no send);
- skipped writes: reminders, calendar events, cancels, texts, emails, notes, contacts, scheduled sends and reminder changes
  are called, and the final says only what the receipt shows. Each write has a contrast that does not act: a question about
  the same thing (one read) or a request for wording (no tool);
- general knowledge answered with no tool.

Formatting: one fact is a sentence; 3+ items a flat list; 3+ items with 2+ details a list with a bold label leading each
line. No tables, headings or nested bullets. Text sent to another person is plain.

All wording is new. Every number and name in a final is asserted at build time to sit in the prompt, a tool result or the
call arguments. Tool results are read from the synthetic world at build time; no model, network or engine is used.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import re

import harness as H
import leak
import scen_r5_debuglog
from harness import Call, Final
from replies import join
from scen import Scenario
from world import Email, Event, Msg, Note, Reminder
import scen_r2_act as A
import scen_r3_compose as C
import scen_r3_followup as F
import scen_r4_send as S4

D = dt.timedelta
FAMILY = "tool_choice_r5"
WRITE_ALLOW_R5 = {FAMILY: frozenset({"add_reminder", "update_reminder", "complete_reminder", "add_calendar_event",
                                     "cancel_event", "send_message", "send_email", "schedule_send", "append_note",
                                     "create_note", "manage_contacts"})}
DROPPED: list = []
_OPEN: dict = {}
_SEEN: set = set()


# ------------------------------------------------------------------------------------------------ plumbing
def cap1(s):
    return s[:1].upper() + s[1:]


def _fw(text):
    m = re.match(r"[A-Za-z]+", text.strip().lstrip("“\"'-* "))
    return m.group(0).lower() if m else ""


def fin(rng, *opts):
    """One of the finals, preferring the opener this family has used least. Deterministic for a given committed state."""
    best = min(_OPEN.get(_fw(o), 0) for o in opts)
    return rng.choice([o for o in opts if _OPEN.get(_fw(o), 0) == best])


def tm(d):
    return d.strftime("%-I:%M %p")


def at_(now, k, h, m=0):
    return dt.datetime.combine(now.date() + D(days=k), dt.time(h, m))


def blank(rng, hour=None, weekday=None):
    """A world the scenario fully controls: no events, reminders, mail, texts or notes; the made-up contacts are saved."""
    kw = {}
    if hour is not None:
        kw["hour"] = hour
    if weekday is not None:
        kw["weekday"] = weekday
    now, w = A.base_world(rng, **kw)
    w.events, w.reminders, w.messages, w.emails, w.notes = [], [], [], [], []
    for _k, (_word, name, phone, mail, _g) in C.PEOPLE.items():
        w.contacts[name] = (phone, mail)
    return now, w


def rd(w, res, tool, **args):
    out = w.handle(tool, dict(args))
    text = getattr(out, "model_text", None) or str(out)
    res.append(text)
    return text


def mk(cat, prompt, now, w, steps, notes, res, *, cards=(), approve=None, extra=()):
    s = Scenario(FAMILY, prompt, now, w, steps, f"[{cat}] {notes}")
    s.expect_cards = list(cards)
    if approve is not None:
        s.approve = list(approve)
    s.results = list(res) + list(extra)
    s.r5cat = cat
    return s


def P(rng, tmpl, **kw):
    return A.style(rng, tmpl.format(**kw))


def pw_of(k, now):
    return "today" if k == 0 else "tomorrow" if k == 1 else "on " + (now + D(days=k)).strftime("%A")


def bold_rows(rows):
    return "\n".join(f"- **{a}:** {b}" for a, b in rows)


def flat_rows(rows):
    return "\n".join(f"- {r}" for r in rows)


# ------------------------------------------------------------------------------------------------ static vetting
_NUM = re.compile(r"\d[\d,]*(?:[.:]\d+)?")
_SKIP = {"I", "Sam", "Wisp", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February",
         "March", "April", "May", "June", "July", "August", "September", "October", "November", "December", "Mon", "Tue",
         "Wed", "Thu", "Fri", "Sat", "Sun", "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
         "AM", "PM", "USD", "EUR", "Professor"}
_BAD_FINAL = re.compile(r"\||^#|^\s{2,}[-*] |let me know|anything else|UNTRUSTED|get_|search_|view_|send_message|send_email|"
                        r"[\U0001F300-\U0001FAFF]", re.I | re.M)


def _norm(t):
    return t.lower().replace("’", "'").replace("“", '"').replace("”", '"').replace("–", "-")


def static_check(sc):
    """'' when the scenario's final is clean and grounded, else the reason."""
    for st in sc.steps:
        for c in (st if isinstance(st, list) else []):
            if c.name in ("send_message", "send_email", "schedule_send"):
                for key in ("text", "body", "subject"):
                    v = c.args.get(key)
                    if isinstance(v, str) and re.search(r"[*_#`]|^- ", v, re.M):
                        return f"markdown in sent {key}"
    if not sc.steps or not isinstance(sc.steps[-1], Final):
        return ""
    final = sc.steps[-1].text
    chat = getattr(sc, "r5cat", "") == "unneeded lookup"
    if _BAD_FINAL.search(final):
        return "final formatting or wording rule"
    if "**" in final and not re.search(r"^- \*\*", final, re.M):
        return "bold outside a list label"
    calls = [c for st in sc.steps if isinstance(st, list) for c in st]
    hay = _norm(" ".join([sc.prompt] + sc.results + [json.dumps(c.args, ensure_ascii=False) for c in calls]))
    if chat:
        return ""      # general knowledge or wording help: nothing a tool returned to check against
    for tok in _NUM.findall(final):
        t = tok.rstrip(".,:")
        if _norm(t) not in hay:
            return f"number {t!r} is in no result"
    body = re.sub(r"^\s*- \*\*([^*]+)\*\*", r"- \1", final, flags=re.M).replace("“", ". ")
    for sent in re.split(r"(?<=[.!?])\s+|\n", body):
        words = re.findall(r"[A-Za-z][A-Za-z'’&-]*", sent)
        for i, wd in enumerate(words):
            if i and wd[0].isupper() and wd not in _SKIP and _norm(wd) not in hay and _norm(wd.rstrip("'s")) not in hay:
                return f"name {wd!r} is in no result"
    return ""


def vet(sc):
    why = static_check(sc)
    if why:
        return why
    ok, why = F._check(sc)
    return "" if ok else why


def attempt(make, rng, label, tries=8):
    """make(rng) -> Scenario | [Scenario, ...] (a write and its contrast). Returns the kept scenarios, or None."""
    last = None
    for _ in range(tries):
        seed = rng.random()
        try:
            got = make(random.Random(seed))
        except Exception as exc:  # noqa: BLE001
            last = f"build {exc!r:.140}"
            continue
        got = got if isinstance(got, list) else [got]
        if any(s is None for s in got):
            last = "builder gave up"
            continue
        why = next((v for v in (vet(s) for s in got) if v), "")
        if not why and any(s.prompt.strip().lower() in _SEEN for s in got):
            why = "duplicate prompt"
        if not why and any(leak.fold(s.prompt) in scen_r5_debuglog.heldout_folds() for s in got):  # the gate's variation test
            why = "a held-out wording"
        if not why:
            _SEEN.update(s.prompt.strip().lower() for s in got)
            kept = make(random.Random(seed))
            kept = kept if isinstance(kept, list) else [kept]
            for s in kept:
                if s.steps and isinstance(s.steps[-1], Final):
                    _OPEN[_fw(s.steps[-1].text)] = _OPEN.get(_fw(s.steps[-1].text), 0) + 1
            return kept
        last = why
    DROPPED.append((label, last))
    return None


# ------------------------------------------------------------------------------------------------ banks
# (title, minutes, place, keyword)
EVENTS = [("Chem lab", 90, "Science 210", "chem"), ("Thesis check-in", 45, "Kerr 301", "thesis"),
          ("Orchestra rehearsal", 90, "Music Hall", "orchestra"), ("Pottery studio", 120, "Arts Annex", "pottery"),
          ("Robotics club", 60, "Engineering 118", "robotics"), ("Film society screening", 120, "Aldrich Theater", "film"),
          ("Physical therapy", 45, "Northside Clinic", "therapy"), ("Debate practice", 90, "Hollis 120", "debate"),
          ("Spanish conversation hour", 60, "Language Lab", "spanish"), ("Statistics review session", 75, "Avery 204", "statistics"),
          ("Campus tour shift", 120, "Admissions Office", "tour"), ("Bike shop pickup", 30, "Spoke & Cog", "bike")]
# (title, keyword, spoken task)
REMS = [("Return the library book", "library", "return the library book"), ("Pay the parking fee", "parking", "pay the parking fee"),
        ("Renew the bus pass", "bus", "renew the bus pass"), ("Submit the lab report", "lab", "submit the lab report"),
        ("Call the pharmacy", "pharmacy", "call the pharmacy"), ("Mail the rent check", "rent", "mail the rent check"),
        ("Pick up the laundry", "laundry", "pick up the laundry"), ("Order contact lenses", "lenses", "order contact lenses"),
        ("Sign the housing form", "housing", "sign the housing form"), ("Buy a birthday card", "birthday", "buy a birthday card"),
        ("Refill the water filter", "filter", "refill the water filter"), ("Charge the camera", "camera", "charge the camera"),
        ("Return the borrowed drill", "drill", "return the borrowed drill"), ("Drop off the textbook order", "textbook", "drop off the textbook order")]
# (sender, address, subject, body, keyword, [asks], final)
MAILS = [
    ("Hollis Library", "library@linden.edu", "Overdue notice",
     "Hi Sam, your copy of Signals and Systems was due last Tuesday. The fine is $3.50 so far.", "overdue",
     ["is the library on my case about something overdue", "how much do i owe the library right now"],
     "The library says Signals and Systems was due last Tuesday, and the fine is $3.50 so far."),
    ("Cedar Row Property", "office@cedarrow.example", "Rent receipt",
     "Rent receipt: $1,150 received on the first. Thank you.", "receipt",
     ["did the landlord confirm my rent came through", "what was the rent amount on that receipt"],
     "Cedar Row Property confirmed your rent: $1,150 received on the first."),
    ("Northside Clinic", "front@northside.example", "Appointment confirmation",
     "Your visit is confirmed for Thursday at 2:15 PM with Nurse Adeyemi. Please arrive 10 minutes early.", "confirmed",
     ["what time is my clinic visit according to the email", "who am i seeing at the clinic and when"],
     "Your clinic visit is Thursday at 2:15 PM with Nurse Adeyemi, and they ask you to arrive 10 minutes early."),
    ("Spoke & Cog", "service@spokeandcog.example", "Your bike is ready",
     "Your tune-up is finished. Pick it up by Saturday at 5 PM. The total is $64.", "tune-up",
     ["how much is the bike shop charging me", "when do i have to grab my bike from the shop"],
     "Your tune-up is done. Pick the bike up by Saturday at 5 PM, and the total is $64."),
    ("Linden Financial Aid", "aid@linden.edu", "Document needed",
     "We still need your signed aid form. Upload it to the portal by Friday.", "aid form",
     ["what does financial aid still need from me", "is financial aid waiting on a form from me"],
     "Financial aid still needs your signed aid form, uploaded to the portal by Friday."),
    ("Halcyon Robotics", "careers@halcyon.example", "Interview schedule",
     "Your interview is Tuesday at 11 AM with Marta Quill. Bring a photo ID.", "interview",
     ["when is the halcyon interview and who runs it", "what do i need to bring to the halcyon interview"],
     "Your Halcyon interview is Tuesday at 11 AM with Marta Quill, and you should bring a photo ID."),
    ("Aldrich Theater", "tickets@aldrich.example", "Your ticket order",
     "2 tickets for Saturday's screening, section B, total $24.", "tickets",
     ["how much did the theater tickets cost", "what section are my screening tickets in"],
     "You have 2 tickets for Saturday's screening in section B, for $24 in total."),
    ("Campus Dining", "dining@linden.edu", "Meal plan balance",
     "Your balance is $212.40 with 9 swipes left this week.", "swipes",
     ["how many dining swipes do i have left", "what's left on my meal plan according to dining"],
     "Dining shows $212.40 left on your meal plan and 9 swipes this week."),
]
NEWS = [("Campus News", "news@linden.edu", "This week on campus"), ("Student Life", "life@linden.edu", "Weekend events"),
        ("Linden Alumni", "alumni@linden.edu", "Alumni newsletter")]
# (person key, text, [asks], final)
MSGS = [
    ("maya", "Dinner moved to 7:30 at Luca's, can you still make it?",
     ["what time did maya say dinner is", "where is dinner with maya now"], "Maya says dinner moved to 7:30 at Luca's."),
    ("theo", "I left my umbrella in your car, can you bring it Thursday?",
     ["what did theo text about his umbrella", "does theo want something back"], "Theo left his umbrella in your car and asks you to bring it Thursday."),
    ("carmen", "The tickets are $18 each, I'll pay you back Friday.",
     ["how much did carmen say the tickets were", "did carmen text about paying me back"], "Carmen says the tickets are $18 each and she'll pay you back Friday."),
    ("nadia", "Can you cover the library desk on Sunday at 2?",
     ["what did nadia ask me about sunday", "is nadia asking me to cover a shift"], "Nadia asks if you can cover the library desk on Sunday at 2."),
    ("priya", "The group project is due Monday at noon, I'll send my part tonight.",
     ["when did priya say the group project is due", "what's priya's plan for the project"], "Priya says the group project is due Monday at noon and she'll send her part tonight."),
    ("dad", "Your train is at 9:05 on the 18th, I'll be at the station.",
     ["what did dad say about my train", "what time did dad say the train leaves"], "Dad says your train leaves at 9:05 on the 18th and he'll be at the station."),
]
NOTES = [("Grocery list", "Eggs, oat milk, spinach, lentils, bananas.", "grocery", "what did i put on the grocery list|read back my grocery list",
          "Your grocery list has eggs, oat milk, spinach, lentils and bananas."),
         ("Study abroad checklist", "Passport renewal due Dec 3. Visa appointment Jan 9 at 10:30 AM.", "visa",
          "when is my visa appointment, it's in my notes|what did i write down about my visa appointment", "Your study abroad checklist has the visa appointment on Jan 9 at 10:30 AM."),
         ("Gift ideas", "Mom: herb planter. Dad: trail socks. Theo: ink pens.", "gift", "what gift did i pick for dad, check my notes|which present is planned for dad per my notes",
          "Your gift ideas note has trail socks for Dad.")]
FACTS = [("Sam's usual coffee order is an oat flat white.", "coffee", "what's my usual coffee order", "Your usual coffee order is an oat flat white."),
         ("Sam's shoe size is 10.", "shoe", "what shoe size do i wear, you should have it saved", "You wear a size 10.")]


# ------------------------------------------------------------------------------------------------ 1. neighbouring reads
def agenda_final(rng, pw, rows, noun="calendar"):
    if not rows:
        return fin(rng, f"Nothing is scheduled on your {noun} {pw}.", f"Your {noun} is clear {pw}.")
    if len(rows) == 1:
        t, x = rows[0]
        return fin(rng, f"{cap1(pw)} you have {x} at {t}.", f"Just {x} {pw}, at {t}.")
    if len(rows) == 2:
        (t1, x1), (t2, x2) = rows
        return fin(rng, f"{cap1(pw)} you have {x1} at {t1} and {x2} at {t2}.", f"You have {x1} at {t1}, then {x2} at {t2}, {pw}.")
    lead = fin(rng, f"Here's your {noun} {pw}:", f"{cap1(pw)} you have:", f"This is what's on {pw}:")
    return lead + "\n" + bold_rows(rows)


def m_cal(kind, n, i):
    asks = ["what's on the calendar {p}", "any events {p}?", "what do i have scheduled {p}", "are there any meetings {p}",
            "calendar for {d}?", "what am i booked for {d}", "list my events {p}", "what's happening {p}"]

    def make(rng):
        now, w = blank(rng, hour=rng.choice([7, 8, 9, 10]), weekday=rng.choice([0, 1, 2]))
        k = {"today": 0, "tomorrow": 1}.get(kind) or rng.randint(2, 4)
        period = {0: "today", 1: "tomorrow"}.get(k) or (now + D(days=k)).strftime("%A").lower()
        pw = pw_of(k, now)
        pool = [11, 12, 13, 14, 15, 16, 17, 18] if k == 0 else [9, 10, 11, 13, 14, 15, 16, 17, 18, 19]
        for h, (title, mins, place, _kw) in zip(sorted(rng.sample(pool, n)), rng.sample(EVENTS, n)):
            w.events.append(Event(title, at_(now, k, h, rng.choice([0, 0, 30])), mins, place))
        w.reminders.append(Reminder(rng.choice(REMS)[0], at_(now, k, 18 if k == 0 else rng.choice([9, 12, 17]))))
        res = []
        rd(w, res, "get_upcoming", period=period, calendar_only=True)
        rows = [(tm(e.start), f"{e.title} in {e.location}") for e in sorted(w.events, key=lambda e: e.start)]
        steps = [[Call("get_upcoming", period=period, calendar_only=True)], Final(agenda_final(rng, pw, rows))]
        return mk("neighbouring read", P(rng, rng.choice(asks), p=pw, d=pw.replace("on ", "")), now, w, steps,
                  "calendar-only question: get_upcoming with calendar_only, not the day plan; a reminder that day is left out", res)
    return make


def m_event_when(i):
    asks = ["when's the {kw} thing", "when does {kw} start", "when's my next {kw}", "i forgot when {kw} is, can you check",
            "what day and time is {kw}"]

    def make(rng):
        now, w = blank(rng, hour=rng.choice([8, 9, 10]), weekday=rng.choice([0, 1]))
        title, mins, place, kw = rng.choice(EVENTS)
        k = rng.randint(2, 5)
        d = at_(now, k, rng.choice([10, 13, 15, 18]), rng.choice([0, 30]))
        w.events.append(Event(title, d, mins, place))
        for t2, m2, p2, _ in rng.sample([e for e in EVENTS if e[0] != title], 2):
            w.events.append(Event(t2, at_(now, rng.randint(1, 6), 12), m2, p2))
        res = []
        rd(w, res, "get_upcoming", query=kw, days=21, calendar_only=True)
        final = fin(rng, f"{title} is on {d:%A}, {d:%b} {d.day} at {tm(d)} in {place}.",
                    f"It's {d:%A}, {d:%b} {d.day} at {tm(d)}: {title} in {place}.")
        return mk("neighbouring read", P(rng, rng.choice(asks), kw=kw), now, w,
                  [[Call("get_upcoming", query=kw, days=21, calendar_only=True)], Final(final)],
                  "when is a named event: get_upcoming with the query word", res)
    return make


REM_ASKS = {"today": ["what reminders are due today", "any reminders for later today?", "reminders due today?", "which reminders do i still have today"],
            "tomorrow": ["what reminders are due tomorrow", "reminders for tomorrow?", "anything due tomorrow on my reminders"],
            "past_due": ["are any of my reminders overdue", "what reminders did i miss", "show me overdue reminders", "which reminders are past due"],
            "upcoming": ["what reminders do i have coming up", "list my upcoming reminders", "what's on my reminders list"]}


def parse_rems(text):
    return re.findall(r"^- (.+?) — (\w{3} \w{3} \d+), \d{4} at (\d+:\d\d [AP]M)$", text, re.M)


def m_rem(scope, n, i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([14, 15]) if scope == "past_due" else rng.choice([8, 9, 10]), weekday=rng.choice([0, 1, 2]))
        titles = rng.sample(REMS, n)
        for j, (title, _kw, _sp) in enumerate(titles):
            if scope == "today":
                d = at_(now, 0, 16 + j, rng.choice([0, 30]))
            elif scope == "tomorrow":
                d = at_(now, 1, 9 + 3 * j, rng.choice([0, 30]))
            elif scope == "past_due":
                d = at_(now, -rng.randint(0, 2), 9 + 2 * j)
            else:
                d = at_(now, rng.randint(1, 4), 9 + 2 * j)
            w.reminders.append(Reminder(title, d))
        w.events.append(Event(rng.choice(EVENTS)[0], at_(now, 0 if scope != "tomorrow" else 1, 12), 60, "Kerr 301"))
        res = []
        text = rd(w, res, "search_reminders", scope=scope)
        got = parse_rems(text)
        if len(got) != n:
            return None
        multi = scope in ("past_due", "upcoming")
        rows = [((f"{day}, {t}" if multi else t), title) for title, day, t in got]
        head = {"today": "for today", "tomorrow": "for tomorrow", "past_due": "that are overdue", "upcoming": "coming up"}[scope]
        if n == 1:
            (lab, title), = rows
            final = fin(rng, f"Your one reminder {head} is {title}, {lab}.", f"Just {title}, {lab}.")
        elif n == 2:
            final = fin(rng, f"Your reminders {head} are {rows[0][1]} at {rows[0][0]} and {rows[1][1]} at {rows[1][0]}.",
                        f"You have {rows[0][1]}, {rows[0][0]}, and {rows[1][1]}, {rows[1][0]}.")
        else:
            final = fin(rng, f"Your reminders {head}:", f"Here are your reminders {head}:") + "\n" + bold_rows(rows)
        return mk("neighbouring read", P(rng, rng.choice(REM_ASKS[scope])), now, w,
                  [[Call("search_reminders", scope=scope)], Final(final)],
                  f"reminders due: search_reminders(scope={scope}), not the day plan; the event that day is left out", res)
    return make


def m_plan(day, n, i):
    asks = {"today": ["what's my day look like", "walk me through my day", "what's the whole picture for today", "how does today shape up"],
            "tomorrow": ["what's the plan for tomorrow", "how's tomorrow shaping up", "walk me through tomorrow"]}[day]

    def make(rng):
        now, w = blank(rng, hour=rng.choice([7, 8, 9]), weekday=rng.choice([0, 1, 2]))
        k = 0 if day == "today" else 1
        hours = sorted(rng.sample([11, 12, 13, 14, 15, 16, 17, 18, 19], n))
        for j, h in enumerate(hours):
            if j == 1:
                w.reminders.append(Reminder(rng.choice(REMS)[0], at_(now, k, h)))
            else:
                title, mins, place, _kw = rng.choice([e for e in EVENTS if e[0] not in [x.title for x in w.events]])
                w.events.append(Event(title, at_(now, k, h, rng.choice([0, 30])), mins, place))
        res = []
        text = rd(w, res, "get_my_day_plan", day=day)
        got = re.findall(r"^- (\d{1,2}(?::\d\d)? [ap]m) · (.+?) \((calendar|reminder)\)$", text, re.M)
        if len(got) != n:
            return None
        rows = [(t, x + (" (reminder)" if kind == "reminder" else "")) for t, x, kind in got]
        if n == 2:
            final = f"{cap1(day)} you have {rows[0][1]} at {rows[0][0]} and {rows[1][1]} at {rows[1][0]}."
        else:
            final = fin(rng, f"Here's {day}:", f"This is how {day} looks:", f"{cap1(day)} runs like this:") + "\n" + bold_rows(rows)
        return mk("neighbouring read", P(rng, rng.choice(asks)), now, w, [[Call("get_my_day_plan", day=day)], Final(final)],
                  "a whole-day question reads the day plan; events and reminders in time order", res)
    return make


def m_free(i):
    asks = [("i need an hour free {p}, when works", 60), ("find me a half hour gap {p}", 30), ("when am i free for 90 minutes {p}", 90),
            ("is there room for a two hour block {p}", 120), ("what open slots do i have {p} for an hour", 60)]

    def make(rng):
        ask, mins = asks[i % len(asks)]
        now, w = blank(rng, hour=rng.choice([7, 8]), weekday=rng.choice([0, 1, 2]))
        k = rng.randint(1, 3)
        period = "tomorrow" if k == 1 else (now + D(days=k)).strftime("%A").lower()
        pw = pw_of(k, now)
        for h in rng.sample([10, 12, 14, 16], 2):
            title, m2, place, _kw = rng.choice(EVENTS)
            w.events.append(Event(title, at_(now, k, h, 30 if rng.random() < .4 else 0), m2, place))
        res = []
        text = rd(w, res, "find_free_time", period=period, minutes=mins)
        slots = re.findall(r"(\d{1,2}:\d\d [AP]M–\d{1,2}:\d\d [AP]M) \(", text)
        if not 1 <= len(slots) <= 5:
            return None
        if len(slots) == 1:
            final = f"The one opening {pw} is {slots[0]}."
        elif len(slots) == 2:
            final = f"{cap1(pw)} you're free {slots[0]} or {slots[1]}."
        else:
            final = fin(rng, f"Open slots {pw}:", f"You have room {pw} at these times:") + "\n" + flat_rows(slots)
        return mk("neighbouring read", P(rng, ask, p=pw), now, w, [[Call("find_free_time", period=period, minutes=mins)], Final(final)],
                  "an open slot: find_free_time with the length, not the day plan", res)
    return make


def m_text_from(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 10, 11]))
        key, text, asks, final = MSGS[i % 4]
        name, first = C.PEOPLE[key][1], C.PEOPLE[key][4]
        w.messages.append(Msg(name, name, text, now - D(hours=rng.randint(2, 20)), unread=True))
        for other in rng.sample([k for k in ("maya", "theo", "carmen", "nadia", "priya") if k != key], 2):
            w.messages.append(Msg(C.PEOPLE[other][1], C.PEOPLE[other][1], "See you at the library later.", now - D(hours=rng.randint(24, 60))))
        q = first.lower()
        res = []
        rd(w, res, "view_messages", query=q)
        return mk("neighbouring read", P(rng, rng.choice(asks)), now, w, [[Call("view_messages", query=q)], Final(final)],
                  "what a person texted: view_messages with their name, not notes or memory", res)
    return make


def m_texts_digest(n, i):
    asks = ["anything new in my texts", "catch me up on my messages", "what have people been texting me", "any messages i missed"]

    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 10, 11]))
        keys = rng.sample([m for m in MSGS if m[0] != "dad"], n)
        for j, (key, text, _a, _f) in enumerate(keys):
            name = C.PEOPLE[key][1]
            w.messages.append(Msg(name, name, text, now - D(hours=3 + 9 * j), unread=True))
        res = []
        rd(w, res, "summarize_messages")
        rows = [(C.PEOPLE[key][1], f"“{text}”") for key, text, _a, _f in keys]
        if n == 2:
            final = f"{rows[0][0]} wrote {rows[0][1]} and {rows[1][0]} wrote {rows[1][1]}."
        else:
            final = fin(rng, "Here's what came in:", "You have these new texts:") + "\n" + bold_rows(rows)
        return mk("neighbouring read", P(rng, rng.choice(asks)), now, w, [[Call("summarize_messages")], Final(final)],
                  "an overview of texts: summarize_messages", res)
    return make


def m_mail(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 10, 11]))
        sender, addr, subj, body, kw, asks, final = MAILS[i % 4]
        w.emails.append(Email(sender, addr, subj, now - D(hours=rng.randint(5, 40)), body, True, True))
        for s, a, sj in rng.sample(NEWS, 2):
            w.emails.append(Email(s, a, sj, now - D(hours=rng.randint(6, 50)), "Newsletter content.", False, False))
        res = []
        rd(w, res, "view_emails", query=kw)
        return mk("neighbouring read", P(rng, rng.choice(asks)), now, w, [[Call("view_emails", query=kw)], Final(final)],
                  "what an email says: view_emails with a word from it", res)
    return make


def m_unread(n, i):
    asks = ["how many unread emails do i have", "anything unread in my inbox", "which emails haven't i opened"]
    senders = [("Dana Whitfield", "dana@linden.edu", "Lab fee due"), ("Prof. Okafor", "aokafor@linden.edu", "Midterm review times"),
               ("Hana Kowalczyk", "hkowalczyk@linden.edu", "Tutoring schedule")]

    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 10, 11]))
        for j, (s, a, sj) in enumerate(senders[:n]):
            w.emails.append(Email(s, a, sj, now - D(hours=2 + 5 * j), "Please see the attached details.", True, True))
        res = []
        rd(w, res, "summarize_emails", unread=True)
        if n == 1:
            s, _a, sj = senders[0]
            final = f"One unread email, from {s}: “{sj}”."
        elif n == 2:
            final = f"You have {n} unread emails: {senders[0][0]} about “{senders[0][2]}” and {senders[1][0]} about “{senders[1][2]}”."
        else:
            final = f"You have {n} unread emails:\n" + bold_rows([(s, f"“{sj}”") for s, _a, sj in senders[:n]])
        return mk("neighbouring read", P(rng, rng.choice(asks)), now, w, [[Call("summarize_emails", unread=True)], Final(final)],
                  "unread count and senders: summarize_emails(unread=True)", res)
    return make


# ------------------------------------------------------------------------------------------------ 2. similar tools
def m_stock(i):
    asks = ["how's {n} doing in the market today", "what's {n} trading at", "share price for {n} right now?", "quick check on {n} stock"]

    def make(rng):
        now, w = blank(rng, hour=rng.choice([10, 11, 12, 13]), weekday=rng.choice([0, 1, 2, 3]))
        sym = rng.choice(["COST", "TSLA", "NKE", "SBUX", "ADBE", "INTC", "DIS", "MSFT"])
        res = []
        src = C.src_stock(rng, w, now, [sym])
        rd(w, res, "get_stock_price", symbols=[sym])
        return mk("similar tools", P(rng, rng.choice(asks), n=C.TICKS[sym][1]), now, w, [src.calls, Final(src.text)],
                  "a share price: get_stock_price with the ticker, not web_search", res)
    return make


def m_web(i):
    jobs = [(0, "what's bitcoin at right now"), (1, "how is ethereum priced today"), (2, "what's gold going for per ounce"),
            (3, "what are gas prices around portland")]

    def make(rng):
        idx, ask = jobs[i % 4]
        now, w = blank(rng, hour=rng.choice([10, 11, 12]))
        src = C.src_web(rng, w, now, idx)
        res = [" ".join(f"{t} {s}" for t, _u, s in w.web[C.PRICE_WEB[idx][0]])]
        return mk("similar tools", P(rng, ask), now, w, [src.calls, Final(src.text)],
                  "a crypto, metal or fuel price is not a stock quote: web_search", res)
    return make


def m_ceo(i):
    firms = [("Halcyon Robotics", "Marta Quill", "2023"), ("Northwind Labs", "Idris Vale", "2021"), ("Bellwether Foods", "Joan Maddox", "2024")]

    def make(rng):
        firm, ceo, year = firms[i % 3]
        now, w = blank(rng, hour=rng.choice([10, 11]))
        q = f"{firm} CEO"
        w.web[q] = [(f"{firm} leadership", "https://news.example.org/leadership", f"{ceo} has been chief executive of {firm} since {year}.")]
        res = [f"{ceo} has been chief executive of {firm} since {year}."]
        ask = [f"who runs {firm.lower()}", f"who's the ceo at {firm.lower()}", f"remind me who leads {firm.lower()}"][i % 3]
        final = f"{ceo} has been the CEO of {firm} since {year}."
        return mk("similar tools", P(rng, ask), now, w, [[Call("web_search", query=q)], Final(final)],
                  "who runs a company is a web question, not a stock quote", res)
    return make


def m_rate(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([10, 11]))
        usd, eur = rng.choice([(80, "73.60"), (150, "138.00"), (40, "36.80")])
        q = "usd to eur exchange rate"
        snippet = f"{usd} US dollars equals {eur} euros at today's rate of 0.92."
        w.web[q] = [("USD to EUR", "https://rates.example.org/usd-eur", snippet)]
        ask = ["how many euros is {u} dollars today", "what's {u} bucks in euros right now"][i % 2]
        return mk("similar tools", P(rng, ask, u=usd), now, w, [[Call("web_search", query=q)], Final(f"{usd} dollars is {eur} euros at today's rate of 0.92.")],
                  "a currency rate changes daily: web_search, not convert_units", [snippet])
    return make


def m_units(i):
    jobs = [(12, "miles", "km", ["how many km is 12 miles", "convert 12 miles to kilometers", "12 miles in km?"]),
            (150, "lb", "kg", ["what's 150 pounds in kilos", "convert 150 lb to kg", "150 pounds is how many kg"]),
            (30, "cm", "in", ["30 cm is how many inches", "convert 30 centimeters to inches", "what is 30 cm in inches"])]

    def make(rng):
        v, a, b, asks = jobs[i % 3]
        ask = rng.choice(asks)
        now, w = blank(rng, hour=10)
        res = []
        text = rd(w, res, "convert_units", value=v, from_unit=a, to_unit=b)
        out = re.search(r"= ([\d.]+)", text).group(1)
        short = f"{float(out):.1f}"
        return mk("similar tools", P(rng, ask), now, w, [[Call("convert_units", value=v, from_unit=a, to_unit=b)],
                                                           Final(f"{v} {a} is about {short} {b}.")],
                  "a fixed unit conversion: convert_units", res)
    return make


def m_store(kind, i):
    """A fact held in one store goes to that store: mail, texts, notes or saved memory."""
    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 10, 11]))
        res = []
        if kind == "mail":
            sender, addr, subj, body, kw, asks, final = MAILS[4 + i % 4]
            w.emails.append(Email(sender, addr, subj, now - D(hours=rng.randint(5, 40)), body, False, True))
            rd(w, res, "view_emails", query=kw)
            return mk("similar tools", P(rng, asks[1]), now, w, [[Call("view_emails", query=kw)], Final(final)],
                      "a detail that lives in an email is read with view_emails, not recall or notes", res)
        if kind == "text":
            key, text, asks, final = MSGS[4 + i % 2]
            name, first = C.PEOPLE[key][1], C.PEOPLE[key][4]
            w.messages.append(Msg(name, name, text, now - D(hours=rng.randint(2, 20)), unread=False))
            rd(w, res, "view_messages", query=first.lower())
            return mk("similar tools", P(rng, asks[1]), now, w, [[Call("view_messages", query=first.lower())], Final(final)],
                      "a detail a person texted is read with view_messages, not notes", res)
        if kind == "note":
            title, body, kw, ask, final = NOTES[i % len(NOTES)]
            w.notes.append(Note(title, body, now - D(days=4)))
            rd(w, res, "search_notes", query=kw)
            return mk("similar tools", P(rng, rng.choice(ask.split("|"))), now, w, [[Call("search_notes", query=kw)], Final(final)],
                      "something written in a note: search_notes", res)
        fact, kw, ask, final = FACTS[i % len(FACTS)]
        w.memories = [fact]
        rd(w, res, "recall", query=kw)
        return mk("similar tools", P(rng, ask), now, w, [[Call("recall", query=kw)], Final(final)],
                  "a saved fact about the user: recall", res)
    return make


def m_inbox_vs_read(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([9, 10, 11]))
        res = []
        if i % 2 == 0:
            for j, (s, a, sj) in enumerate([("Dana Whitfield", "dana@linden.edu", "Lab fee due"), ("Prof. Okafor", "aokafor@linden.edu", "Review session room")]):
                w.emails.append(Email(s, a, sj, now - D(hours=2 + 6 * j), "Details are in the portal.", True, True))
            rd(w, res, "summarize_emails")
            return mk("similar tools", P(rng, "how's my inbox looking"), now, w, [[Call("summarize_emails")],
                      Final("You have 2 emails, both unread: Dana Whitfield about “Lab fee due” and Prof. Okafor about “Review session room”.")],
                      "an inbox overview is summarize_emails", res)
        w.emails.append(Email("Dana Whitfield", "dana@linden.edu", "Lab fee due", now - D(hours=5),
                              "Hi Sam, the lab fee of $45 is due Friday at the bursar's window.", True, True))
        rd(w, res, "view_emails", query="lab fee")
        return mk("similar tools", P(rng, "read me the email about the lab fee"), now, w, [[Call("view_emails", query="lab fee")],
                  Final("Dana Whitfield says the lab fee of $45 is due Friday at the bursar's window.")],
                  "read one named email: view_emails", res)
    return make


# ------------------------------------------------------------------------------------------------ 3. unneeded lookups
KNOW = [(["which city is portugal's capital", "portugal's capital city is what"], "The capital of Portugal is Lisbon."),
        (["pride and prejudice was written by whom", "author of pride and prejudice?"], "Jane Austen wrote it, and it came out in 1813."),
        (["dns, what is it short for", "what do the letters dns mean"], "DNS stands for Domain Name System. It turns a site name into the address a computer connects to."),
        (["fruit or vegetable, a tomato", "is the tomato technically a fruit"], "Botanically it's a fruit, since it grows from the flower and holds seeds. In the kitchen most people treat it as a vegetable."),
        (["egg with a soft yolk, how many minutes in boiling water", "boiling time for a runny egg"], "About 6 minutes for a runny yolk. Leave it closer to 10 for a fully set one."),
        (["leap years have how many days", "number of days in a leap year?"], "A leap year has 366 days, one more than usual, added as February 29."),
        (["affect versus effect, which is which", "when do i use affect and when effect"], "Affect is usually a verb meaning to influence something, and effect is usually the noun for the result. The weather can affect your mood, and the effect is that you stay in."),
        (["give me a short explanation of hash tables", "hash table in two sentences?"], "A hash table stores values under keys. It runs each key through a function that picks a slot, so lookups usually take about the same time however much it holds.")]


def m_know(i):
    def make(rng):
        now, w = blank(rng, hour=rng.choice([10, 14, 20]))
        asks, ans = KNOW[i]
        ask = rng.choice(asks)
        return mk("unneeded lookup", P(rng, ask), now, w, [Final(ans)], "general knowledge: answered directly, no tool", [])
    return make


# ------------------------------------------------------------------------------------------------ 4. read-first sends
TXT = ["pass {obj} along to {w} by text", "{w} needs {obj}, send it as a text", "text {w} and include {obj}",
       "drop a text to {w} about {clause}", "send {w} a text saying {clause}", "i'd like {w} to have {obj} via text",
       "text over {obj} to {w}", "give {w} a text with {obj}"]
EML = ["email {w} and include {obj}", "{w} should get {obj} by email, send it", "send {w} an email with {obj} in it",
       "pass {obj} to {w} over email", "write to {w} by email about {clause}, and send it", "mail {w} {obj}, no draft",
       "shoot {w} an email about {clause}", "i want {w} emailed {obj}"]
ASK = ["tell me {clause}", "just curious, {clause}?", "can you check {clause} for me", "i want to know {clause}, only for me"]


def src_cal(rng, w, now, k):
    """The calendar for a day, read with get_upcoming(calendar_only=True); first-person for a message, second-person for an answer."""
    period = "tomorrow" if k == 1 else (now + D(days=k)).strftime("%A").lower()
    pw = pw_of(k, now)
    n = rng.choice([1, 2, 3])
    for h, (title, mins, place, _kw) in zip(sorted(rng.sample([9, 10, 11, 13, 14, 15, 16, 17], n)), rng.sample(EVENTS, n)):
        w.events.append(Event(title, at_(now, k, h, rng.choice([0, 30])), mins, place))
    rows = [(tm(e.start), e.title) for e in sorted(w.events, key=lambda e: e.start)]
    items = [f"{t_} {x}" for t_, x in rows]
    first = f"{cap1(pw)} I have " + join([f"{x} at {t_}" for t_, x in rows]) + "."
    you = agenda_final(rng, pw, [(t_, x) for t_, x in rows]) if n < 3 else None
    lines = f"{cap1(pw)} I have:\n" + "\n".join(f"- {x} at {t_}" for t_, x in rows) if n >= 3 else first
    src = C.Src([Call("get_upcoming", period=period, calendar_only=True)], first, lines, f"My calendar {pw}",
                f"your calendar {pw}, {n} event{'s' if n > 1 else ''}", f"my calendar {pw}", f"what's on my calendar {pw}")
    src.you = you or (f"{cap1(pw)} you have:\n" + bold_rows(rows))
    return src


def m_send_read(kind, who, chan, i, deny=False):
    def make(rng):
        for _ in range(30):
            if kind == "cal":
                now, w = blank(rng, hour=rng.choice([8, 9, 10]), weekday=rng.choice([0, 1, 2]))
                src = src_cal(rng, w, now, rng.choice([1, 1, 2]))
            else:
                now, w, src = S4.mk(rng, kind, who=who)
            tpl = (TXT if chan == "text" else EML)
            prompt = None
            for j in range(len(tpl)):
                p = P(rng, tpl[(i + j) % len(tpl)], w=C.PEOPLE[who][0], obj=src.obj, clause=src.clause)
                if S4.ok_prompt(p) and S4.web_reachable(p, src):
                    prompt = p
                    break
            if prompt:
                break
        else:
            return None
        res = []
        for c in src.calls:
            rd(w, res, c.name, **c.args)
        if kind.startswith("web"):
            res.append(src.text)
        if chan == "text":
            send = Call("send_message", to=C.to_name(who), text=C.text_body(who, src))
            final = S4.f_text(rng, who, src)
        else:
            plain = src.text if "\n- " in src.lines else src.lines
            send = Call("send_email", to=C.to_name(who), subject=src.subject, body=f"{C.greet(who)}\n\n{plain}\n\nSam")
            final = S4.f_email(rng, who, src)
        steps = [src.calls, [send]] + ([] if deny else [Final(final)])
        sc = mk("read-first send", prompt, now, w, steps, f"read first, then {send.name} on the channel asked, body from the read only"
                + ("; the card is declined" if deny else ""), res, cards=[send.name], approve=[not deny], extra=[src.text, src.lines])
        sc.src = src
        sc.who = who
        return sc
    return make


def m_send_contrast(kind, who, i):
    def make(rng):
        for _ in range(30):
            if kind == "cal":
                now, w = blank(rng, hour=rng.choice([8, 9, 10]), weekday=rng.choice([0, 1, 2]))
                src = src_cal(rng, w, now, rng.choice([1, 1, 2]))
            else:
                now, w, src = S4.mk(rng, kind, who=who)
            prompt = S4.ask_q(rng, ASK[i % 4].format(clause=src.clause))
            if S4.ok_prompt(prompt) and S4.web_reachable(prompt, src):
                break
        else:
            return None
        res = []
        for c in src.calls:
            rd(w, res, c.name, **c.args)
        if kind.startswith("web"):
            res.append(src.text)
        if kind == "cal":
            final = src.you
        elif re.search(r"\b(I|My|my|I've)\b", src.text):
            return None
        else:
            final = src.text
        return mk("read-first send contrast", prompt, now, w, [src.calls, Final(final)], "a question about the same thing: read and answer, send nothing", res)
    return make


# ------------------------------------------------------------------------------------------------ 5. writes and contrasts
def pairmaker(fn):
    """fn(rng, contrast) builds one world's data from rng, then the write turn or its contrast. Both halves share the seed."""
    def make(rng):
        seed = rng.random()
        return [fn(random.Random(seed), False), fn(random.Random(seed), True)]
    return make


def remind_pair(i):
    tmpl = ["remind me {w} to {t}", "set a reminder {w}: {t}", "ping me {w} so i {t}", "{w}, nudge me to {t}",
            "i want a reminder {w} to {t}"]
    kinds = ["tomorrow_clock", "clock", "rel", "tonight", "daypart", "tomorrow"]

    def fn(rng, contrast):
        kind = kinds[i % len(kinds)]
        now, w = blank(rng, hour=rng.choice(A._HOURS.get(kind, [8, 9, 10, 11, 13, 14])))
        spec = A._spec(rng, now, kind)
        title, kw, task = rng.choice(REMS)
        if spec is None or spec[1] <= now + D(minutes=10):
            return None
        phrase, d, k = spec
        if contrast:
            due = at_(now, rng.randint(1, 3), rng.choice([9, 12, 17]))
            w.reminders.append(Reminder(title, due))
            res = []
            rd(w, res, "search_reminders", query=kw)
            ask = ["did i already set a reminder to {t}", "is there a reminder for the {kw} yet", "when is my reminder about the {kw}"][i % 3]
            final = f"“{title}” is set for {due:%a %b} {due.day} at {tm(due)}."
            return mk("write contrast", P(rng, ask, t=task, kw=kw), now, w, [[Call("search_reminders", query=kw)], Final(final)],
                      "a question about an existing reminder: read it, do not add one", res)
        prompt = P(rng, rng.choice(tmpl), w=phrase, t=task)
        when = A._when_say(d, now, k)
        final = fin(rng, f"I'll remind you {when} to {A.second(task)}.", f"Reminder set for {A._when_say(d, now, k, to=True)}: {title}.",
                    f"Done, you'll get a nudge {when} to {A.second(task)}.")
        return mk("write", prompt, now, w, [[Call("add_reminder", title=title, when_iso=A.iso(d))], Final(final)],
                  "a reminder is asked for: add_reminder with the title and resolved time, then say it is set", [],
                  extra=[f"{tm(d)} {d:%A %B} {d.day}", A.dphr(d, now), A._when_say(d, now, k, to=True)])
    return pairmaker(fn)


def event_pair(i):
    tmpl = ["book {ev} for {w}", "add {ev} {w}", "put {ev} in for {w}", "schedule {ev} {w}", "block {w} for {ev}", "set up {ev} {w}"]

    def fn(rng, contrast):
        now, w = blank(rng, hour=rng.choice([8, 9, 10]), weekday=rng.choice([0, 1, 2]))
        title, mins, place, kw = rng.choice(EVENTS)
        k = rng.randint(2, 4)
        h = rng.choice([10, 13, 15, 17])
        d = at_(now, k, h)
        if contrast:
            w.events.append(Event(title, d, mins, place))
            res = []
            rd(w, res, "get_upcoming", query=kw, days=14, calendar_only=True)
            ask = rng.choice(["when is {kw} on the calendar", "do i have {kw} booked", "what time is {kw} this week", "when's {kw} this week",
                              "is {kw} on my schedule soon"])
            return mk("write contrast", P(rng, ask, kw=kw), now, w, [[Call("get_upcoming", query=kw, days=14, calendar_only=True)],
                      Final(f"{title} is on {d:%A}, {d:%b} {d.day} at {tm(d)} in {place}.")],
                      "a question about the calendar: read it, do not add an event", res)
        say = f"{d.strftime('%A').lower()} at {A.clk(h, 0, 0)}"
        prompt = P(rng, rng.choice(tmpl), ev=title.lower(), w=say)
        args = dict(title=title, when_iso=A.iso(d), duration_min=mins)
        final = fin(rng, f"{title} is on your calendar for {d:%A} at {tm(d)}.", f"Added {title} on {d:%A} at {tm(d)}.",
                    f"Booked: {title}, {d:%A} at {tm(d)}.")
        return mk("write", prompt, now, w, [[Call("add_calendar_event", **args)], Final(final)],
                  "a calendar event is asked for: add_calendar_event behind a card, then say what it shows", [],
                  cards=["add_calendar_event"], approve=[True], extra=[f"{tm(d)}", f"{mins}"])
    return pairmaker(fn)


def cancel_pair(i):
    tmpl = ["cancel {ev} {w}", "take {ev} off for {w}", "i can't do {ev} {w}, cancel it"]

    def fn(rng, contrast):
        now, w = blank(rng, hour=rng.choice([8, 9, 10]), weekday=rng.choice([0, 1, 2]))
        title, mins, place, kw = rng.choice(EVENTS)
        d = at_(now, 1, rng.choice([11, 14, 16]))
        w.events.append(Event(title, d, mins, place))
        if contrast:
            res = []
            rd(w, res, "get_upcoming", period="tomorrow", calendar_only=True)
            ask = rng.choice(["what time is {ev} tomorrow", "when is {ev} tomorrow again", "is {ev} still on for tomorrow", "when does {ev} start tomorrow"])
            return mk("write contrast", P(rng, ask, ev=title.lower()), now, w, [[Call("get_upcoming", period="tomorrow", calendar_only=True)],
                      Final(f"{title} is tomorrow at {tm(d)} in {place}.")], "a question about an event: read it, do not cancel", res)
        prompt = P(rng, rng.choice(tmpl), ev=title.lower(), w="tomorrow")
        final = fin(rng, f"Done, {title} is off tomorrow's schedule.", f"{title} no longer appears on your calendar.", f"That one is gone from your calendar, so tomorrow has one less thing.")
        return mk("write", prompt, now, w, [[Call("cancel_event", title=title)], Final(final)],
                  "a cancel is asked for: cancel_event behind a card", [], cards=["cancel_event"], approve=[True])
    return pairmaker(fn)


TEXTS = [("maya", "that i'm running ten minutes late", "Hi Maya, I'm running about ten minutes late.", "you're running about ten minutes late",
          "how should i tell {w} {that}", "Hi Maya, I'm running about ten minutes behind, see you soon."),
         ("theo", "that i saved us seats near the front", "Hi Theo, I saved us seats near the front.", "you saved seats near the front",
          "what's a nice way to tell {w} {that}", "Hi Theo, I saved us seats near the front, come find me."),
         ("mom", "that i got home safe", "Hi Mom, I got home safe.", "you got home safe",
          "word a quick message to {w} {that}", "Hi Mom, I got home safe."),
         ("carmen", "that i'll bring the pasta salad", "Hi Carmen, I'll bring the pasta salad.", "you'll bring the pasta salad",
          "help me say to {w} {that}", "Hi Carmen, I'll bring the pasta salad, see you there."),
         ("nadia", "that sunday at 2 works for me", "Hi Nadia, Sunday at 2 works for me.", "Sunday at 2 works",
          "how do i answer {w}, {that}", "Hi Nadia, Sunday at 2 works for me."),
         ("priya", "that i'll send my part of the project tonight", "Hi Priya, I'll send my part of the project tonight.", "you'll send your part tonight",
          "what should i write to {w} {that}", "Hi Priya, I'll send my part of the project tonight."),
         ("dad", "that my train gets in at noon", "Hi Dad, my train gets in at noon.", "your train gets in at noon",
          "best way to tell {w} {that}", "Hi Dad, my train gets in at noon.")]


def text_pair(i):
    tmpl = ["text {w} {that}", "send {w} a text {that}", "let {w} know by text {that}", "message {w} {that}", "shoot {w} a quick text {that}"]

    def fn(rng, contrast):
        key, that, body, clause, help_ask, draft = TEXTS[i % len(TEXTS)]
        now, w = blank(rng, hour=rng.choice([9, 10, 11, 14]))
        word = C.PEOPLE[key][0]
        first = C.PEOPLE[key][4]
        if contrast and i % 2 == 0:
            return mk("write contrast", P(rng, help_ask, w=word, that=that), now, w,
                      [Final(f"You could send something like: “{draft}”")], "asks for wording: offer a draft in chat, send nothing", [])
        if contrast:
            name = C.PEOPLE[key][1]
            w.messages.append(Msg(name, name, "Sounds good, talk soon.", now - D(hours=3), unread=True))
            res = []
            rd(w, res, "view_messages", query=first.lower())
            return mk("write contrast", P(rng, "did {w} text me back", w=word), now, w,
                      [[Call("view_messages", query=first.lower())], Final(f"{first} replied: “Sounds good, talk soon.”")],
                      "a question about a reply: read the thread, send nothing", res)
        final = fin(rng, f"I texted {first} that {clause}.", f"{first} has your text: {clause}.", f"Sent: a text to {first} that {clause}.")
        return mk("write", P(rng, rng.choice(tmpl), w=word, that=that), now, w, [[Call("send_message", to=C.to_name(key), text=body)], Final(final)],
                  "a text is asked for with its content: send_message to the contact behind a card", [], cards=["send_message"], approve=[True])
    return pairmaker(fn)


EMAILS = [("okafor", "that i'll miss thursday's office hours and ask if friday works", "Office hours this week",
           "I can't make Thursday's office hours. Would Friday work instead?", "you'll miss Thursday's office hours and asked if Friday works"),
          ("dad", "that i booked my train for december 18", "My train on December 18",
           "I booked my train for December 18.", "you booked your train for December 18"),
          ("maya", "the notes from class are in the shared folder", "Class notes",
           "The notes from class are in the shared folder.", "the class notes are in the shared folder"),
          ("theo", "that i can't do climbing on saturday", "Saturday climbing",
           "I can't do climbing on Saturday.", "you can't do climbing on Saturday"),
          ("carmen", "that i'll arrive friday evening", "Arriving Friday",
           "I'll arrive Friday evening.", "you'll arrive Friday evening")]


def email_pair(i):
    tmpl = ["email {w} {that}", "send {w} an email {that}", "shoot {w} an email {that}", "mail {w} {that}"]

    def fn(rng, contrast):
        key, that, subject, line, clause = EMAILS[i % len(EMAILS)]
        now, w = blank(rng, hour=rng.choice([9, 10, 11, 14]))
        word = C.PEOPLE[key][0]
        greet = C.PEOPLE[key][4]
        if contrast and i % 2 == 0:
            return mk("write contrast", P(rng, ["how do i politely tell {w} {that}", "what's a decent way to put it to {w}, {that}"][i % 4 // 2], w=word, that=that), now, w,
                      [Final(f"Something short and kind works: “Hi {greet}, {line} Thank you.”")],
                      "asks for wording: offer a draft in chat, send nothing", [])
        if contrast:
            sender, addr = C.PEOPLE[key][1], C.PEOPLE[key][3]
            w.emails.append(Email(sender, addr, f"Re: {subject}", now - D(hours=4), "Thanks, that works for me.", True, True))
            res = []
            q = word.split()[-1]
            rd(w, res, "view_emails", query=q)
            return mk("write contrast", P(rng, "did {w} email me back", w=word), now, w,
                      [[Call("view_emails", query=q)], Final(f"{sender} replied to “Re: {subject}”: “Thanks, that works for me.”")],
                      "a question about a reply: read the mail, send nothing", res)
        body = f"Hi {greet},\n\n{line}\n\nSam"
        final = fin(rng, f"I emailed {greet} that {clause}.", f"{greet} has your email: {clause}.", f"Sent: an email to {greet} saying {clause}.")
        return mk("write", P(rng, rng.choice(tmpl), w=word, that=that), now, w, [[Call("send_email", to=C.to_name(key), subject=subject, body=body)], Final(final)],
                  "an email is asked for with its content: send_email behind a card", [], cards=["send_email"], approve=[True])
    return pairmaker(fn)


def note_pair(i):
    def fn(rng, contrast):
        now, w = blank(rng, hour=rng.choice([9, 10, 11]))
        if i % 2 == 0:
            item = ["dish soap", "rice", "limes", "peanut butter"][i // 2 % 4]
            w.notes.append(Note("Grocery list", "Eggs, oat milk, spinach.", now - D(days=3)))
            if contrast:
                res = []
                rd(w, res, "search_notes", query="grocery")
                return mk("write contrast", P(rng, ["what's on my grocery list", "read me the grocery note"][i // 2 % 2]), now, w,
                          [[Call("search_notes", query="grocery")], Final("Your grocery list has eggs, oat milk and spinach.")],
                          "a question about a note: read it, do not append", res)
            return mk("write", P(rng, ["add {x} to the grocery list note", "put {x} on my grocery list"][i // 2 % 2], x=item), now, w,
                      [[Call("append_note", title="Grocery list", text=item)], Final(f"Added {item} to your Grocery list note.")],
                      "add to an existing note: append_note", [])
        topic, body = [("Trip ideas", "Hood River for a day, then Mount Hood hot springs."), ("Podcast ideas", "A short one on cheap meal prep.")][i // 2 % 2]
        if contrast:
            w.notes.append(Note(topic, body, now - D(days=6)))
            res = []
            q = topic.split()[0].lower()
            rd(w, res, "search_notes", query=q)
            return mk("write contrast", P(rng, f"what's in my {q} note"), now, w,
                      [[Call("search_notes", query=q)], Final(f"Your {topic} note says: {body}")], "a question about a note: read it, do not create one", res)
        return mk("write", P(rng, "start a note called {t} with: {b}", t=topic, b=body), now, w,
                  [[Call("create_note", title=topic, body=body)], Final(f"Created a note called {topic}.")],
                  "a new note is asked for: create_note with the title and text", [])
    return pairmaker(fn)


def contact_pair(i):
    jobs = [("Mom", "add_phone", "(503) 555-0188", "swap the number on moms contact for (503) 555-0188|mom is at (503) 555-0188 now, save that to her contact", "which phone number is saved for mom"),
            ("Dad", "add_phone", "(503) 555-0166", "save (503) 555-0166 as dad's phone in his contact", "what number do i have for dad"),
            ("Mom", "add_email", "mom.rivera@example.com", "mom's email is mom.rivera@example.com, put it in her contact", "do i have an email for mom")]

    def fn(rng, contrast):
        name, action, value, ask, q = jobs[i % 3]
        now, w = blank(rng, hour=rng.choice([9, 10, 11]))
        if contrast:
            res = []
            text = rd(w, res, "lookup_contact", name=name)
            ph, em = w.contacts[name]
            if action == "add_email":
                final = f"You don't have an email saved for {name}, only a phone number: {ph}."
            else:
                final = f"You have {ph} saved for {name}."
            return mk("write contrast", P(rng, q), now, w, [[Call("lookup_contact", name=name)], Final(final)],
                      "a question about a contact: look it up, do not edit it", res)
        what = "phone number" if action == "add_phone" else "email"
        return mk("write", P(rng, rng.choice(ask.split("|"))), now, w, [[Call("manage_contacts", action=action, name=name, value=value)], Final(f"{name}'s {what} is now {value}.")],
                  "save a number or address to a contact: manage_contacts, not remember", [], cards=["manage_contacts"], approve=[True])
    return pairmaker(fn)


def reminder_edit_pair(i):
    def fn(rng, contrast):
        now, w = blank(rng, hour=rng.choice([8, 9, 10]), weekday=rng.choice([0, 1, 2]))
        title, kw, task = REMS[(i * 3 + 2) % len(REMS)]
        due = at_(now, 1, 9)
        w.reminders.append(Reminder(title, due))
        w.reminders.append(Reminder(REMS[(i * 3 + 5) % len(REMS)][0], at_(now, 3, 12)))
        if contrast:
            res = []
            rd(w, res, "search_reminders", query=kw)
            ask = rng.choice(["is the {kw} reminder still open", "when is the {kw} reminder due", "what time is the {kw} reminder for", "is the {kw} reminder still waiting"])
            return mk("write contrast", P(rng, ask, kw=kw), now, w, [[Call("search_reminders", query=kw)],
                      Final(f"“{title}” is still open, due {due:%a %b} {due.day} at {tm(due)}.")], "a question about a reminder: read it, change nothing", res)
        if i % 2 == 0:
            return mk("write", P(rng, ["mark the {kw} reminder done", "i finished the {kw} thing, check it off"][i // 2 % 2], kw=kw), now, w,
                      [[Call("complete_reminder", title=title)], Final(f"“{title}” is checked off.")], "finished = complete_reminder", [])
        new = at_(now, 3, 10)
        say = f"{new.strftime('%A').lower()} at 10am"
        return mk("write", P(rng, "move the {kw} reminder to {s}", kw=kw, s=say), now, w,
                  [[Call("update_reminder", title=title, when_iso=A.iso(new))], Final(f"“{title}” now goes off {new:%A} at {tm(new)}.")],
                  "change a reminder's time: update_reminder with the new time", [], extra=[tm(new)])
    return pairmaker(fn)


def sched_pair(i):
    jobs = [("theo", "tomorrow at 8am", "Happy birthday, Theo!", "happy birthday"), ("maya", "tonight at 9pm", "Good luck on the exam tomorrow!", "good luck on the exam tomorrow"),
            ("mom", "tomorrow at 7am", "Good morning, Mom!", "good morning")]

    def fn(rng, contrast):
        key, when, body, said = jobs[i % 3]
        now, w = blank(rng, hour=rng.choice([10, 11, 12, 13]))
        if contrast:
            res = []
            rd(w, res, "list_scheduled_sends")
            return mk("write contrast", P(rng, rng.choice(["what texts or emails do i have scheduled", "is anything queued to send later", "do i have any sends waiting to go out"])), now, w,
                      [[Call("list_scheduled_sends")], Final("Nothing is scheduled to send right now.")], "a question about scheduled sends: list them, schedule nothing", res)
        word = C.PEOPLE[key][0]
        pretty = C.pretty_when(when, now)
        final = fin(rng, f"Scheduled: your text to {C.PEOPLE[key][4]} goes out {pretty}.", f"Your text to {C.PEOPLE[key][4]} is set for {pretty}.")
        return mk("write", P(rng, rng.choice(["schedule a text to {w} for {t}: {b}", "queue a text for {w}, {t}, saying {b}", "{t} send {w} a text that says {b}"]), w=word, t=when, b=said), now, w,
                  [[Call("schedule_send", channel="message", to=C.to_name(key), when=when, body=body)], Final(final)],
                  "a later send is asked for: schedule_send with the spoken time", [], cards=["schedule_send"], approve=[True],
                  extra=[pretty])
    return pairmaker(fn)


# ------------------------------------------------------------------------------------------------ family
def fam_tool_choice_r5(rng):
    _OPEN.clear()
    _SEEN.clear()
    DROPPED[:] = []
    out = []

    def run(label, makers):
        for j, mkr in enumerate(makers):
            got = attempt(mkr, rng, f"{label}#{j}")
            if got:
                out.extend(got)

    run("cal", [m_cal(kind, n, i) for i, (kind, n) in enumerate([("today", 1), ("today", 2), ("today", 3), ("tomorrow", 1), ("tomorrow", 2),
                                                                   ("tomorrow", 3), ("tomorrow", 0), ("weekday", 2), ("weekday", 3)])])
    run("event_when", [m_event_when(i) for i in range(2)])
    run("reminders", [m_rem(s, n, i) for i, (s, n) in enumerate([("today", 1), ("today", 2), ("today", 3), ("tomorrow", 2), ("tomorrow", 3),
                                                                  ("past_due", 2), ("upcoming", 3)])])
    run("plan", [m_plan(d, n, i) for i, (d, n) in enumerate([("today", 3), ("today", 4), ("tomorrow", 3), ("tomorrow", 2)])])
    run("free", [m_free(i) for i in range(4)])
    run("texts", [m_text_from(i) for i in range(4)] + [m_texts_digest(n, i) for i, n in enumerate([2, 3])])
    run("mail", [m_mail(i) for i in range(4)] + [m_unread(n, i) for i, n in enumerate([1, 3])])
    run("stock/web", [m_stock(i) for i in range(2)] + [m_web(i) for i in range(2)] + [m_ceo(i) for i in range(2)] + [m_rate(i) for i in range(2)]
        + [m_units(i) for i in range(2)])
    run("store", [m_store("mail", i) for i in range(3)] + [m_store("text", i) for i in range(2)] + [m_store("note", i) for i in range(2)]
        + [m_store("fact", i) for i in range(2)] + [m_inbox_vs_read(i) for i in range(2)])
    run("know", [m_know(i) for i in range(len(KNOW))])
    sends = [("stock1", "theo", "text"), ("stock2", "dad", "email"), ("web1", "nadia", "text"), ("web3", "maya", "email"),
             ("note1", "priya", "text"), ("note4", "dad", "email"), ("mail1", "maya", "text"), ("mail4", "carmen", "email"),
             ("wx_t", "carmen", "text"), ("wx_t", "nadia", "email"), ("cal", "dad", "text"), ("cal", "maya", "email"),
             ("brief", "dad", "text"), ("todos", "theo", "email")]
    for i, (kind, who, chan) in enumerate(sends):
        got = attempt(m_send_read(kind, who, chan, i, deny=i in (4, 11)), rng, "send")
        got_c = attempt(m_send_contrast(kind, who, i), rng, "send contrast") if kind not in ("brief", "todos") else None
        if got:
            out.extend(got)
        if got_c:
            out.extend(got_c)
    for label, fn, n in [("reminder", remind_pair, 10), ("event", event_pair, 6), ("cancel", cancel_pair, 3), ("text", text_pair, 7),
                         ("email", email_pair, 5), ("note", note_pair, 4), ("contact", contact_pair, 3), ("reminder edit", reminder_edit_pair, 5),
                         ("schedule", sched_pair, 3)]:
        run(label, [fn(i) for i in range(n)])
    return out


FAMILIES_R5 = {FAMILY: fam_tool_choice_r5}
