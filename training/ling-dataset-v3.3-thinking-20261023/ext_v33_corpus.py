"""V3.3 categories authored from the 2026-10-23 corpus run (prompts-20261023-184747-dcf6aa, V3 thinking-on, model-only).

Each family targets a failure that run showed; nothing from the run is copied (all people, events, mail and phrasing are
synthetic, and every prompt is checked against both the held-out eval shingles and the run's own prompts). Tool results
and host errors use the exact formats the live host produced in that run. Built with V3.2's generate.Builder, so the
protocol (stubs or get_tool_schemas, receipts, loading budget, offer order) is identical to every other row.

| family | run evidence | gold behaviour |
|---|---|---|
| arg_period | `period='next 48 hours'`, `'Friday'`, `'next 10 days'` rejected; Ling gave up and asked the user to rephrase | days=N or an exact YYYY-MM-DD, first time or after one error |
| arg_scope / arg_unexpected | `scope='overdue'`, get_past_events(period=), summarize_emails(query=) | correct the argument (or switch to view_emails) once and answer |
| date_named | "Veterans Day"/"Election Day" searched as a query over 7 days, then "nothing" (once with an invented date) | resolve the date, read that day |
| query_retry | a long query missed a title that the short name matched | retry once with the distinctive name, then answer |
| empty_honest | answers invented items or escalated count 5 -> 1000 | say plainly what was searched and that nothing matched; no second identical search |
| web_blocked | 8 identical blocked web_search calls | stop at the first BLOCKED result and explain |
| read_not_write | "remind me what I said..." created a reminder; "show me what you'd delete" called clear_past_reminders | read only; offer the action |
| email_address | invented addresses, sent to the user's own address | email needs an address from lookup_contact; phone-only, no match or several matches -> ask |
| personal_not_web | "my Glassdoor alerts", "my study session" sent to web_search | personal sources first |
"""
import datetime as dt
import random

CATEGORY = "v33_corpus_fixes"
SEED = 20261023
N_TRAIN, N_DEV = 190, 24

PERIOD_HELP = ("Use one of: 'today', 'yesterday', 'this week', 'last week', 'this month', 'last month', 'this year', 'last year', "
               "'last N days/weeks/months/quarters/years' (N as a digit or spelled out, e.g. '3 weeks' or 'six months'), "
               "'YYYY-MM' for a specific month, or 'YYYY-MM-DD' for a specific day.")
UPCOMING_ARGS = "days: integer, period: string, calendar_only: boolean, query: string, account: string, include_holidays: boolean"
BLOCKED_WEB = ("BLOCKED by safety policy: Private data is in context; web access is unavailable unless the request explicitly "
               "asks for a public lookup.")
DENIED = "The user denied this action."

PEOPLE = ["Marta Lind", "Jonah Reyes", "Keiko Sato", "Dev Malhotra", "Lena Fischer", "Omar Haddad", "Ines Duarte",
          "Tomas Novak", "Ruth Adeyemi", "Felix Brandt", "Nadia Sorensen", "Caleb Frost", "Hana Sato", "Yara Nasser"]
EVENTS = ["Design review", "Lab section", "Study group", "Budget sync", "Physics tutoring", "Volunteer shift",
          "Team standup", "Dentist", "Advising appointment", "Book club", "Piano lesson", "Project check-in"]
SENDERS = [("Glassdoor", "Glassdoor Jobs", "noreply@glassdoor.example.invalid", ["Junior data analyst roles near you", "New: product intern openings",
                                                                     "Analyst jobs you might like"]),
           ("housing office", "Campus Housing", "housing@campus.example.invalid", ["Move-out checklist", "Room inspection window", "Lease renewal reminder"]),
           ("Northwind Bank", "Northwind Bank", "alerts@northwind.example.invalid", ["Your statement is ready", "Card payment due Friday"]),
           ("library", "Riverside Library", "notices@library.example.invalid", ["Hold ready for pickup", "Item due in 3 days"])]
NOTES = [("Weekly focus", "Finish the grant draft; call the landlord; two gym sessions"),
         ("This week", "Ship the slides by Thursday; book flights; reply to Hana"),
         ("Priorities", "Lab report first, then the scholarship essay; clean the garage on Sunday")]


def _rng(*parts):
    return random.Random("|".join(str(p) for p in (SEED,) + parts))


def _now(r):
    d = dt.date(2026, 10, 19) + dt.timedelta(days=r.randint(0, 34))
    t = dt.datetime(d.year, d.month, d.day, r.choice([8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19]), r.choice([4, 12, 26, 38, 47, 55]))
    sentence = (f"The current date and time is {t.strftime('%A, %B')} {t.day}, {t.year} at {(t.hour - 1) % 12 + 1}:{t.minute:02d} "
                f"{'AM' if t.hour < 12 else 'PM'}. Resolve relative dates/times against this.")
    return t, sentence


def _today_line(t):
    return f"Today is {t.strftime('%A, %B')} {t.day}, {t.year}."


def _hdr(t, d):
    if d == t.date():
        return f"**Today · {d.strftime('%a, %b')} {d.day}**"
    if d == t.date() + dt.timedelta(days=1):
        return f"**Tomorrow · {d.strftime('%a, %b')} {d.day}**"
    return f"**{d.strftime('%A')} · {d.strftime('%b')} {d.day}**"


def _clock(h, m):
    return f"{(h - 1) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _listing(t, label, items):
    """get_upcoming's live listing format. items: [(date, hour, minute, title)]."""
    n = len(items)
    out = [f"{label}: {n} event{'s' if n != 1 else ''}, no reminders", ""]
    for d in sorted({i[0] for i in items}):
        out.append(_hdr(t, d))
        for _, h, m, title in sorted(i for i in items if i[0] == d):
            out.append(f"- **{_clock(h, m)}** — {title}")
    return "\n".join(out)


def _say_date(d):
    return f"{d.strftime('%A, %B')} {d.day}"


def _load(G, b, r, tools):
    need = [t for t in tools if t not in b.loaded]
    if not need:
        return
    if r.random() < 0.7:
        b.stub([(t, {}) for t in need])
    else:
        b.explicit(list(dict.fromkeys(G.FAMILY[t] for t in need)), need)


def _email(sender, addr, subject, date_text, body, mid):
    return (f"1 email:\nFrom: {sender} <{addr}>\nDate: {date_text}\nSubject: {subject}\nMessage-ID: <{mid}@mail.example.invalid>\n"
            f"Body: {body}")


# ------------------------------------------------------------------ families: fn(G, r, b, t, dev) -> (prefix, text, meta, hint)

def f_arg_period(G, r, b, t, dev):
    kind = r.choice(["hours", "days", "weekday"])
    if kind == "hours":
        text = r.choice(["what's due in the next 48 hours", "anything due within 48 hours?", "what do I have over the next two days"]
                        if not dev else ["what lands on me in the coming 48 hours"])
        good, bad, label, hint = {"days": 2}, {"period": "next 48 hours"}, "Next 2 days", "days=2 covers the next 48 hours."
        span = 2
    elif kind == "days":
        n = r.choice([5, 10, 12, 14])
        text = r.choice([f"show my schedule for the next {n} days", f"what's coming up in the next {n} days",
                         f"list my events for the coming {n} days"] if not dev else [f"walk me through the next {n} days"])
        good, bad, label, hint = {"days": n}, {"period": f"next {n} days"}, f"Next {n} days", f"days={n} is the way to ask for {n} days."
        span = n
    else:
        wd = r.choice(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
        ahead = (["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].index(wd) - t.weekday()) % 7 or 7
        d = t.date() + dt.timedelta(days=ahead)
        text = r.choice([f"what's on my calendar for {wd}", f"am I busy on {wd}?", f"what does {wd} look like"]
                        if not dev else [f"run me through {wd}'s plans"])
        good, bad = {"period": d.isoformat()}, {"period": wd}
        label = f"{d.strftime('%A, %B')} {d.day}"
        hint = f"{wd} is {d.isoformat()}; period takes an exact date."
        span = None
    _load(G, b, r, ["get_upcoming"])
    first = t.date() if span else dt.date.fromisoformat(good["period"])
    items = [(first + dt.timedelta(days=r.randint(0, (span or 1) - 1)), r.choice([9, 10, 13, 15, 16]), r.choice([0, 30]), e)
             for e in r.sample(EVENTS, r.randint(1, 3))]
    listing = _listing(t, label, items)
    if r.random() < 0.5:
        err = f"(error: (couldn't understand the period '{bad['period']}'. {PERIOD_HELP}))"
        b.call([("get_upcoming", bad, err)], trainable=False)
    b.call([("get_upcoming", good, listing)])
    lines = "; ".join(f"{e} {_say_date(d)} at {_clock(h, m)}" for d, h, m, e in sorted(items))
    b.final(f"Here's what's on: {lines}.")
    return [], text, {"kind": kind, "good": good}, hint


def f_arg_scope(G, r, b, t, dev):
    text = r.choice(["which reminders are overdue", "anything past due on my reminders?", "show me the reminders I missed"]
                    if not dev else ["which to-dos slipped past their date"])
    _load(G, b, r, ["search_reminders"])
    if r.random() < 0.6:
        b.call([("search_reminders", {"query": "", "scope": "overdue"},
                 "(error calling search_reminders({'query': '', 'scope': 'overdue'}): scope must be one of ['today', 'tomorrow', "
                 "'past_due', 'upcoming', 'all']. Expected arguments — query: string (required), scope: string. Call it again "
                 "with corrected arguments.)")], trainable=False)
    titles = r.sample(["Return library books", "Pay the water bill", "Renew parking permit", "Email the landlord", "Submit the lab form"],
                      r.randint(1, 3))
    days = [t.date() - dt.timedelta(days=r.randint(1, 9)) for _ in titles]
    res = "Apple Reminders incomplete-item matches:\n" + "\n".join(
        f"- {x} — {d.strftime('%a %b')} {d.day}, {d.year} at 9:00 AM" for x, d in zip(titles, days))
    b.call([("search_reminders", {"query": "", "scope": "past_due"}, res)])
    b.final("Overdue: " + "; ".join(f"{x} (was due {_say_date(d)})" for x, d in zip(titles, days)) + ".")
    return [], text, {"n": len(titles)}, "The scope for overdue items is past_due."


def f_arg_unexpected(G, r, b, t, dev):
    if r.random() < 0.5:
        text = r.choice(["what meetings did I have last week", "what was on my calendar last week"] if not dev
                        else ["recap last week's meetings for me"])
        _load(G, b, r, ["get_past_events"])
        b.call([("get_past_events", {"period": "last week"},
                 "(error calling get_past_events({'period': 'last week'}): unexpected argument(s) ['period']. Expected arguments — "
                 "days: integer, query: string, account: string, include_holidays: boolean. Call it again with corrected arguments.)")],
               trainable=False)
        evs = r.sample(EVENTS, 2)
        ds = sorted(t.date() - dt.timedelta(days=r.randint(1, 7)) for _ in evs)
        res = (_today_line(t) + f" Past 7 day(s), {len(evs)} item(s), most recent first:\n" +
               "\n".join(f"- {d.strftime('%a %b')} {d.day}, {d.year} 10:00 AM meeting: {e}" for d, e in zip(reversed(ds), evs)))
        b.call([("get_past_events", {"days": 7}, res)])
        b.final("Last week you had " + " and ".join(f"{e} on {_say_date(d)}" for d, e in zip(reversed(ds), evs)) + ".")
        return [], text, {"tool": "get_past_events"}, "get_past_events takes days, not period: days=7."
    short, sender, addr, subjects = r.choice(SENDERS)
    text = r.choice([f"summarize my {short} emails this month", f"what did {short} email me about this month"] if not dev
                    else [f"brief me on this month's {short} mail"])
    _load(G, b, r, ["summarize_emails", "view_emails"])
    key = sender.split()[-1] if short != "Northwind Bank" else "Northwind"
    b.call([("summarize_emails", {"period": "this month", "query": key},
             f"(error calling summarize_emails({{'period': 'this month', 'query': '{key}'}}): unexpected argument(s) ['query']. "
             "Expected arguments — period: string, day: string, count: integer, unread: boolean, account: string. Call it again "
             "with corrected arguments.)")], trainable=False)
    subj = r.choice(subjects)
    d = t.date() - dt.timedelta(days=r.randint(1, 10))
    body = r.choice(["Please review the details below before the deadline.", "Action may be needed on your account.",
                     "Here is a quick update for this month."])
    b.call([("view_emails", {"query": key, "period": "this month", "count": 5},
             _email(sender, addr, subj, f"{d.strftime('%A, %B')} {d.day}", body, f"{r.getrandbits(40):010x}"))])
    b.final(f"One {sender} email this month: “{subj}” ({_say_date(d)}). It says: {body}")
    return [], text, {"tool": "view_emails"}, "summarize_emails can't filter by sender, so view_emails with a query does it."


HOLIDAYS = {"Halloween": (10, 31), "Veterans Day": (11, 11), "Christmas Eve": (12, 24), "Christmas": (12, 25),
            "New Year's Eve": (12, 31)}


def _thanksgiving(year):
    d = dt.date(year, 11, 1)
    first_thu = d + dt.timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + dt.timedelta(days=21)


def _election_day(year):
    d = dt.date(year, 11, 1)
    first_mon = d + dt.timedelta(days=(0 - d.weekday()) % 7)
    return first_mon + dt.timedelta(days=1)


def f_date_named(G, r, b, t, dev):
    cands = {k: dt.date(t.year, *md) for k, md in HOLIDAYS.items()}
    cands["Thanksgiving"] = _thanksgiving(t.year)
    cands["Election Day"] = _election_day(t.year)
    future = {k: d for k, d in cands.items() if d > t.date()}
    name = r.choice(sorted(future))
    d = future[name]
    text = r.choice([f"what do I have on {name}?", f"am I free on {name}", f"anything on my calendar for {name}?"]
                    if not dev else [f"is {name} clear on my schedule"])
    _load(G, b, r, ["get_upcoming"])
    items = [(d, r.choice([10, 12, 18]), 0, e) for e in r.sample(EVENTS, r.randint(1, 2))]
    b.call([("get_upcoming", {"period": d.isoformat()}, _listing(t, f"{d.strftime('%A, %B')} {d.day}", items))])
    lines = " and ".join(f"{e} at {_clock(h, m)}" for _, h, m, e in sorted(items))
    b.final(f"On {name} ({_say_date(d)}) you have {lines}.")
    return [], text, {"holiday": name, "date": d.isoformat()}, f"{name} is {d.isoformat()}; read that exact day."


def f_query_retry(G, r, b, t, dev):
    person = r.choice(PEOPLE)
    first = person.split()[0]
    ev = r.choice(["design review", "office hours", "advising meeting", "project check-in"])
    text = r.choice([f"when is my {ev} with {first}?", f"find the {ev} with {first} on my calendar"] if not dev
                    else [f"look up when I'm meeting {first} for the {ev}"])
    _load(G, b, r, ["get_upcoming"])
    b.call([("get_upcoming", {"query": f"{ev} with {first}"}, _today_line(t) + " Nothing scheduled in next 7 day(s).")])
    d = t.date() + dt.timedelta(days=r.randint(1, 6))
    title = f"{ev.title()} — {person}"
    h = r.choice([9, 11, 14, 16])
    b.call([("get_upcoming", {"query": first}, _listing(t, "Next 7 days", [(d, h, 0, title)]))])
    b.final(f"It's “{title}” on {_say_date(d)} at {_clock(h, 0)}.")
    return [], text, {"person": person}, f"Titles are often worded differently; the name {first} alone is the safer query."


def f_empty_honest(G, r, b, t, dev):
    what = r.choice(["email", "reminder"])
    thing = r.choice(["the parking refund", "my gym membership", "the concert tickets", "the passport appointment",
                      "the bike repair", "the scholarship form"])
    key = thing.replace("the ", "").replace("my ", "")
    if what == "email":
        text = r.choice([f"find the email about {thing}", f"did anyone email me about {thing}?"] if not dev
                        else [f"dig up whatever mail mentions {thing}"])
        _load(G, b, r, ["view_emails"])
        b.call([("view_emails", {"query": key, "count": 5}, f"No emails matching “{key}” were found in the recent inbox Wisp searched.")])
        b.final(f"I didn't find any email about {key} in the recent inbox Wisp searched. If it's older, tell me roughly when it came "
                "or who sent it and I'll look again.")
    else:
        text = r.choice([f"do I have a reminder about {thing}?", f"is there a reminder for {thing}"] if not dev
                        else [f"check whether {thing} is on my reminders"])
        _load(G, b, r, ["search_reminders"])
        b.call([("search_reminders", {"query": key, "scope": "all"},
                 f"A current Reminders read found no active match for '{key}'. An older remembered item does not establish a "
                 "current reminder.")])
        b.final(f"No, there's no current reminder about {key}.")
    return [], text, {"what": what}, None


def f_web_blocked(G, r, b, t, dev):
    ev = r.choice(["conference", "career fair", "hackathon", "concert", "workshop"])
    city = r.choice(["Denver", "Austin", "Portland", "Chicago", "Boston"])
    text = r.choice([f"when is my {city} {ev}, and what's worth doing there that week?",
                     f"my {ev} in {city}: what day is it and is there anything fun nearby?"] if not dev
                    else [f"what date is the {city} {ev}, and what's around there to see"])
    _load(G, b, r, ["get_upcoming", "web_search"])
    d = t.date() + dt.timedelta(days=r.randint(8, 30))
    title = f"{ev.title()} — {city}"
    b.call([("get_upcoming", {"query": ev, "days": 31}, _listing(t, "Next 31 days", [(d, 9, 0, title)]))])
    b.call([("web_search", {"query": f"things to do in {city}"}, BLOCKED_WEB)], trainable=False)
    b.final(f"Your {title} is on {_say_date(d)}. I can't search the web in the same request that read your calendar; ask me "
            f"separately (for example “search the web for things to do in {city}”) and I'll look it up.")
    return [], text, {"city": city}, None


def f_read_not_write(G, r, b, t, dev):
    kind = r.choice(["focus", "stale", "dismiss"])
    if kind == "focus":
        title, body = r.choice(NOTES)
        text = r.choice(["remind me what I said I'd focus on this week", "what did I say my priorities were?",
                         "remind me what's on my focus list"] if not dev else ["jog my memory on this week's priorities"])
        _load(G, b, r, ["search_notes"])
        b.call([("search_notes", {"query": "focus" if "focus" in text else "priorities"},
                 f"Title: {title}\nModified: {(t.date() - dt.timedelta(days=3)).strftime('%a %b')} {(t.date() - dt.timedelta(days=3)).day}\n{body}")])
        b.final(f"From your note “{title}”: {body}.")
        return [], text, {"kind": kind}, "This asks me to recall something, not to set a reminder; reading is enough."
    titles = r.sample(["Return library books", "Pay the water bill", "Renew parking permit", "Email the landlord"], 2)
    days = [t.date() - dt.timedelta(days=r.randint(10, 40)) for _ in titles]
    res = "Apple Reminders incomplete-item matches:\n" + "\n".join(
        f"- {x} — {d.strftime('%a %b')} {d.day}, {d.year} at 9:00 AM" for x, d in zip(titles, days))
    text = (r.choice(["show me the stale reminders you'd suggest I delete", "which old reminders should I get rid of?"] if not dev
                     else ["point out the reminders that look abandoned"]) if kind == "stale"
            else r.choice(["which reminders should I dismiss?", "what can I clear off my reminders?"] if not dev
                          else ["tell me which reminders are safe to drop"]))
    _load(G, b, r, ["search_reminders"])
    b.call([("search_reminders", {"query": "", "scope": "past_due"}, res)])
    b.final("These are long past due: " + "; ".join(f"{x} ({_say_date(d)})" for x, d in zip(titles, days)) +
            ". Want me to delete them?")
    return [], text, {"kind": kind}, "The user asked which ones, not to delete; list them and offer."


def f_email_address(G, r, b, t, dev):
    kind = r.choice(["phone_only", "no_match", "several", "no_name"])
    person = r.choice(PEOPLE)
    first = person.split()[0]
    topic = r.choice(["the meeting notes", "the slides", "the trip itinerary", "the budget sheet", "the reading list"])
    if kind == "no_name":
        role = r.choice(["advisor", "landlord", "manager", "coach"])
        text = r.choice([f"email my {role} that I'm dropping the evening class", f"send my {role} an email saying I'll be late"]
                        if not dev else [f"shoot my {role} a quick email that I'm running behind"])
        b.final(f"Who is your {role}? Give me their name or email address and I'll write it.")
        return [], text, {"kind": kind}, f"No one is named and I don't know who the {role} is; never guess an address."
    text = r.choice([f"email {first} {topic}", f"send {first} an email with {topic}"] if not dev
                    else [f"get {topic} over to {first} by email"])
    _load(G, b, r, ["lookup_contact", "send_email"])
    if kind == "phone_only":
        b.call([("lookup_contact", {"name": first}, f"{person}: +1 555 01{r.randint(10, 99)}")])
        b.final(f"I only have a phone number for {person}, no email address. Want me to text it instead, or give me the email "
                "address to use?")
        return [], text, {"kind": kind}, "An email needs an email address, and the lookup returned only a phone number."
    if kind == "no_match":
        b.call([("lookup_contact", {"name": first},
                 f"(No saved contact matches '{first}'. Ask the user for the number or email — do not guess one.)")])
        b.final(f"I couldn't find {first} in your contacts. What's their email address?")
        return [], text, {"kind": kind}, None
    other = r.choice([p for p in PEOPLE if p != person])
    two = [f"{first} {person.split()[1]}", f"{first} {other.split()[1]}"]
    b.call([("lookup_contact", {"name": first},
             f"Several contacts match '{first}' — ask the user which one they mean before sending anything:\n" +
             "\n".join(f"- {n}: {n.split()[0].lower()}.{n.split()[1].lower()}@example.invalid" for n in two))])
    b.final(f"There are two {first}s in your contacts: {two[0]} and {two[1]}. Which one should get it?")
    return [], text, {"kind": kind}, None


def f_personal_not_web(G, r, b, t, dev):
    if r.random() < 0.5:
        short, sender, addr, subjects = r.choice(SENDERS[:2])
        text = r.choice([f"what do my {short} emails have in common this week?", f"summarize the {short} alerts I got this week"]
                        if not dev else [f"what's the gist of this week's {short} messages in my inbox"])
        _load(G, b, r, ["view_emails"])
        subj = r.choice(subjects)
        d = t.date() - dt.timedelta(days=r.randint(0, 4))
        b.call([("view_emails", {"query": sender.split()[0], "period": "this week", "count": 5},
                 _email(sender, addr, subj, f"{d.strftime('%A, %B')} {d.day}", "See the listings and dates below.",
                        f"{r.getrandbits(40):010x}"))])
        b.final(f"This week's {sender} email is “{subj}” ({_say_date(d)}); that's the only one Wisp found.")
        return [], text, {"source": "mail"}, "These are the user's own emails, so the inbox, not the web."
    ev = r.choice(["study session", "piano lesson", "tutoring", "book club"])
    text = r.choice([f"when does my {ev} start?", f"what time is my {ev}"] if not dev else [f"remind me of the {ev} time"])
    _load(G, b, r, ["get_upcoming"])
    d = t.date() + dt.timedelta(days=r.randint(0, 5))
    h = r.choice([10, 15, 17, 19])
    b.call([("get_upcoming", {"query": ev}, _listing(t, "Next 7 days", [(d, h, 0, ev.title())]))])
    b.final(f"Your {ev} is {_say_date(d)} at {_clock(h, 0)}.")
    return [], text, {"source": "calendar"}, "It's the user's own schedule, so the calendar, not a web search."


FAMILIES = {   # fid: (fn, weight, variant)
    "arg_period": (f_arg_period, 7, "personal_no_web"), "arg_scope": (f_arg_scope, 3, "personal_no_web"),
    "arg_unexpected": (f_arg_unexpected, 4, "personal_no_web"), "date_named": (f_date_named, 4, "personal_no_web"),
    "query_retry": (f_query_retry, 4, "personal_no_web"), "empty_honest": (f_empty_honest, 4, "personal_no_web"),
    "web_blocked": (f_web_blocked, 3, "web"), "read_not_write": (f_read_not_write, 5, "personal_no_web"),
    "email_address": (f_email_address, 6, "personal_no_web"), "personal_not_web": (f_personal_not_web, 4, "web"),
}


def _tokens(text):
    import re
    return re.findall(r"[a-z0-9']+", text.lower())


def _shingles(text, n=4):
    import hashlib
    t = _tokens(text)
    return {hashlib.sha1(" ".join(t[i:i + n]).encode()).hexdigest()[:12] for i in range(max(1, len(t) - n + 1))}


def run_overlap(text, run_prompts):
    """Max Jaccard of `text`'s hashed 4-token shingles against assets/corpus_run_shingles.json (sets)."""
    a = _shingles(text)
    return max((len(a & b) / len(a | b) for b in run_prompts if a and b), default=0.0)


def build(G, held, run_prompts, limit=0.4):
    """-> list of dicts: split, fid, j, prefix, user_text, now, variant, builder, meta, hint, overlap."""
    weights = {f: w for f, (_, w, _) in FAMILIES.items()}
    out = []
    for split, total in (("train", N_TRAIN), ("dev", N_DEV)):
        s = sum(weights.values())
        alloc = {f: max(1, round(total * w / s)) for f, w in weights.items()}
        for fid in sorted(alloc):
            fn, _, variant = FAMILIES[fid]
            taken = set()
            for j in range(alloc[fid]):
                for attempt in range(60):
                    r = _rng(split, fid, j, attempt)
                    t, now = _now(r)
                    b = G.Builder(r, variant)
                    try:
                        prefix, text, meta, hint = fn(G, r, b, t, split == "dev")
                    except AssertionError:
                        continue
                    key = (text, t.date())
                    ov = max(G.heldout_overlap(held, text), run_overlap(text, run_prompts))
                    if key in taken or ov >= limit:
                        continue
                    taken.add(key)
                    out.append({"split": split, "fid": fid, "j": j, "prefix": prefix, "user_text": text, "now": now,
                                "variant": variant, "builder": b, "meta": meta, "hint": hint, "overlap": ov, "attempt": attempt})
                    break
    return out
