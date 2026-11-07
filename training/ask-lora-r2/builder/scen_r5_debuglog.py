"""Round-5 family ``debug_log_r5``: the user's own Ask questions and variations of them work and look good.

The user's biggest goal for r5. The questions are the user's and live outside this public repo, in a local seeds file
(``ASK_LORA_R5_SEEDS``). This module holds no question wording. With no seeds file the family builds nothing.

- Names and addresses in a seed are placeholders, filled here with the made-up people of ``scen_r3_compose``.
- Worlds, tool results and answers are synthetic. Nothing from a real export enters a row.
- ``chains`` replay the order of the user's chats as seeded conversations. ``shapes`` give one turn per wording.
- A seed's ``heldout`` wordings never build a row. They test r5 on the Mac.
- Knowledge, formula and goal answers are written by base. Their scripted final is ``BASE_WRITES``, which must not survive
  into a final build (``unwritten``).

A final that lists 3+ items uses a flat list, with a bold label when an item has 2+ details. No tables, no headings and no
nested bullets, because the notch can't show them. Text sent to another person stays plain.
"""
from __future__ import annotations

import datetime as dt
import os
import random
import re
from dataclasses import dataclass, field

import yaml

import leak
from harness import Call, Final
from replies import clock, join
from scen import Scenario, fp_items, read_plan
from world import Email, Msg, Reminder
import scen_r3_compose as C
import scen_r3_followup as F
import scen_r4_day as S
from scen_r3_followup import turn
from scen_r4_daychat import populate

D = dt.timedelta
FAMILY = "debug_log_r5"
LIST_FAMILY = "long_email_r5"
WRITE_ALLOW_R5 = {FAMILY: frozenset({"add_reminder", "add_calendar_event", "draft_message", "send_message", "send_email"})}
SEEDS = os.environ.get("ASK_LORA_R5_SEEDS") or os.path.expanduser("~/.moe/evals/private-real-use/r5-seeds/questions.yaml")
BASE_WRITES = "Base writes this answer."
DROPPED: list = []
TEXT_TO = ["mom", "dad", "maya", "theo", "carmen", "nadia", "priya"]
HER = ["mom", "maya", "carmen", "nadia", "priya"]
HIM = ["dad", "theo"]
EMAIL_TO = ["dad", "maya", "theo", "carmen", "nadia", "priya"]
INDEX = {"S&P": ("^GSPC", "S&P 500", 5871.20, 5842.10), "DOW": ("^DJI", "Dow", 43120.50, 43310.25),
         "NASDAQ": ("^IXIC", "Nasdaq", 18944.75, 18790.40), "MARKET": ("^GSPC", "S&P 500", 5871.20, 5842.10)}
STOCKS = {"TESLA": "TSLA", "MICRON": "MU", "SANDISK": "SNDK", "VICOR": "VICR"}
WEB = [(r"president", "current US president", "President Dana Ellison", "The president is Dana Ellison."),
       (r"prime minister", "current prime minister", "Prime Minister Elise Moreau", "The prime minister is Elise Moreau."),
       (r"super bowl", "last super bowl winner", "The Portland Pilots won the last Super Bowl", "The Portland Pilots won the last one."),
       (r"openai", "openai ceo", "Jordan Hale leads the company as CEO", "Jordan Hale runs it as CEO."),
       (r"iphone", "latest iphone", "The newest model is the iPhone 18 Pro", "The newest one is the iPhone 18 Pro.")]
STOP = set("is there are any anything about my in the a an of on for get did i me email emails inbox updates update news "
           "someone anyone from to".split())
MAIL_SUBJECTS = ["Quick note about {t}", "Update on your {t}", "{T}: what happens next"]
FILLER = ("The committee met this week and went through routine scheduling, and asked departments to keep their records "
          "current before the end of the month. ")


@dataclass
class Out:
    steps: list
    final: str | None
    tool: str | None = None
    cards: list = field(default_factory=list)
    approve: list | None = None


@dataclass
class Ctx:
    rng: random.Random
    now: dt.datetime
    w: object
    history: list = field(default_factory=list)
    last_facts: list = field(default_factory=list)
    last_when: str = ""
    who: str | None = None
    channel_asked: bool = False
    offer: tuple | None = None
    draft: str = ""
    draft_read: str = ""     # the plan read behind ``draft`` when no earlier reply shows it, so a resend has to read it again
    last_channel: str = ""   # how the last send went, so "as a text" after a text isn't taken for a switch


def load_seeds():
    if not os.path.isfile(SEEDS):
        return None
    with open(SEEDS) as f:
        return yaml.safe_load(f)


def new_ctx(rng):
    now, w = S.mk(rng, hour=rng.choice([8, 9, 11, 13, 16, 19]))
    for _key, (_word, name, phone, mail, _greet) in C.PEOPLE.items():
        w.contacts[name] = (phone, mail)
    populate(rng, w, now, [now.date() + D(days=i) for i in range(0, 7)])
    ctx = Ctx(rng, now, w)
    seed_messages(ctx)
    return ctx


def seed_messages(ctx):
    rng, now = ctx.rng, ctx.now
    who = rng.sample(["maya", "theo", "carmen", "nadia"], 3)
    lines = [("Are we still on for {d} at {h}?", "study group"), ("Can you send me the notes from class?", "class notes"),
             ("Dinner at Luca's on {d}? I booked {h}.", "dinner"), ("I got the tickets, you owe me $18.", "tickets")]
    rng.shuffle(lines)
    day = (now + D(days=2)).strftime("%A")
    for i, key in enumerate(who):
        text = lines[i][0].format(d=day, h=rng.choice(["6 PM", "7 PM", "5:30 PM"]))
        ctx.w.messages.append(Msg(C.PEOPLE[key][1], C.PEOPLE[key][1], text, now - D(hours=3 + 9 * i), unread=True))


def fill(ctx, q):
    """Fill a seed's placeholders. A pronoun keeps the person the chat is already about."""
    rng = ctx.rng
    if "{person}" in q or "{contact}" in q:
        ctx.who = rng.choice(TEXT_TO if "{person}" in q else EMAIL_TO)
        word = C.PEOPLE[ctx.who][0]
        q = q.replace("{person}", word).replace("{contact}", word)
    if "{address}" in q:
        q = q.replace("{address}", address(ctx))
    return q


def address(ctx):
    first = (C.PEOPLE[ctx.who][4] if ctx.who else "jordan").lower().replace(" ", ".")
    return f"{first}.{ctx.rng.choice(['lane', 'hart', 'ames'])}@example.net"


def name_of(ctx):
    return C.PEOPLE[ctx.who][4]


def contact_of(ctx):
    return C.PEOPLE[ctx.who][1]


def clean_line(text):
    return re.sub(r"\s+", " ", text).strip()


def plan_lines(plan):
    """(label, thing) for each time-bound line a plan prints: one flat list, each item once."""
    out = []
    for title, entries in plan.days:
        for when, thing, src in entries:
            label = str(when) if plan.kind == "day" else f"{title}, {when}"
            thing = clean_line(thing) + (f" ({src})" if src in ("email", "text", "note") else "")
            if (label, thing) not in out:
                out.append((label, thing))
    return out


def bullets(rows):
    return "\n".join(f"- **{a}:** {b}" for a, b in rows)


def plan_answer(ctx, arg, lead):
    plan = read_plan(ctx.w, arg)
    if plan.error:
        raise ValueError(plan.error)
    rows = plan_lines(plan)
    if not rows:
        ctx.last_facts = []
        return plan, f"{lead.split(':')[0]} looks clear."
    ctx.last_facts = rows
    if len(rows) == 1:
        return plan, f"Just one thing: {rows[0][1]}, {rows[0][0]}."
    extra = ""
    if plan.waiting:
        extra = "\n\n" + clean_line(f"Someone is also waiting on a reply: {plan.waiting[0][0] if isinstance(plan.waiting[0], (list, tuple)) else plan.waiting[0]}.")
    return plan, f"{lead}\n{bullets(rows)}{extra}"


def plain_schedule(ctx, plan, day_word):
    pairs = fp_items(plan, ctx.now)
    if not pairs:
        raise ValueError("nothing to send")
    items = [f"{p} {h}" if h else p for h, p in pairs]
    return f"{day_word.capitalize()}: {join(items)}."     # reminders read as commands, so "I have Hand in the form" won't do


# ------------------------------------------------------------------------------------ handlers
def h_greeting(ctx, q):
    return Out([], ctx.rng.choice(["Hey Sam! What's on your mind?", "Hi Sam, good to see you. What can I help with?",
                                   "Hey! How's your day going?"]))


def _plan(ctx, arg, lead, word):
    plan, final = plan_answer(ctx, arg, lead)
    ctx.last_when = word
    return Out([[Call("get_my_day_plan", day=arg)]], final, "get_my_day_plan")


def h_plan_today(ctx, q):
    return _plan(ctx, "today", "Here's what's left today:", "today")


def h_plan_tomorrow(ctx, q):
    return _plan(ctx, "tomorrow", "Here's tomorrow:", "tomorrow")


def h_plan_week(ctx, q):
    return _plan(ctx, "this_week", "Here's your week:", "this week")


def h_month_ahead(ctx, q):
    w, now = ctx.w, ctx.now
    if not any(e.start.month == now.month and e.start > now for e in w.events):
        raise ValueError("empty month")
    rows = []
    for e in sorted(w.events, key=lambda e: e.start):
        if e.start.month == now.month and e.start > now:
            when = f"{e.start:%b} {e.start.day}"
            rows.append((when, f"{e.title} (all day)" if e.all_day else f"{e.title} at {clock(e.start)}"))
    ctx.last_facts = rows[:12]
    return Out([[Call("get_upcoming", period="this month")]], "Here are the key dates this month:\n" + bullets(rows[:12]), "get_upcoming")


def h_spend_day(ctx, q):
    word = "today" if "today" in q.lower() else "tomorrow"
    plan = read_plan(ctx.w, word)
    rows = plan_lines(plan)
    if not rows:
        raise ValueError("clear day")
    first = rows[0]
    title = re.sub(r" \((email|text|note)\)$", "", first[1])
    ctx.offer = (f"Get ready for {title}", word)
    ctx.last_when = word
    ctx.last_facts = rows
    names = [re.sub(r" \((email|text|note)\)$", "", t) for _, t in rows[:3]]
    tail = f"You have {join(names)}, so I'd plan around those and keep the gaps for whatever feels most pressing."
    return Out([[Call("get_my_day_plan", day=word)]],
               f"{tail}\n\nWant me to set a reminder to get ready for {title}?", "get_my_day_plan")


def h_assent(ctx, q):
    title, word = ctx.offer or ("Review my notes", "tomorrow")
    at = (ctx.now + D(days=1 if word == "tomorrow" else 0)).replace(hour=9 if word == "tomorrow" else 20, minute=0, second=0, microsecond=0)
    if at <= ctx.now:
        at = ctx.now + D(hours=2)
    iso = at.strftime("%Y-%m-%dT%H:%M:00")
    ctx.w.reminders.append(Reminder(title, at))
    ctx.offer = None
    ctx.draft = title
    return Out([[Call("add_reminder", title=title, when_iso=iso)]], f"Done, I set a reminder to {title.lower()} at {clock(at)}.", "add_reminder")


def h_dont_see(ctx, q):
    title = ctx.draft or "Review my notes"
    word = max(re.sub(r"[^a-z ]", "", title.lower()).split(), key=len)
    hits = [r for r in ctx.w.reminders if word in r.title.lower() and not r.done]
    if hits:
        return Out([[Call("search_reminders", query=word)]],
                   f"It's there: {hits[0].title}, set for {clock(hits[0].due)}. If it isn't showing, try reopening the Reminders app.", "search_reminders")
    at = (ctx.now + D(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    return Out([[Call("search_reminders", query=word)], [Call("add_reminder", title=title, when_iso=at.strftime("%Y-%m-%dT%H:%M:00"))]],
               f"You're right, it wasn't there. It's set now for {clock(at)} tomorrow.", "add_reminder")


def h_reformat(ctx, q):
    if len(ctx.last_facts) < 2:
        raise ValueError("nothing to reformat")
    return Out([], bullets(ctx.last_facts))


def _msg_rows(ctx):
    rows: dict = {}
    for m in sorted(ctx.w.messages, key=lambda m: m.when):
        if m.sender != "Me" and ctx.now - m.when <= D(days=3) and m.when <= ctx.now:
            rows.setdefault(m.conv, []).append(m.text)
    return rows


def h_messages(ctx, q):
    rows = _msg_rows(ctx)
    if not rows:
        return Out([[Call("summarize_messages")]], "Nothing new in your messages.", "summarize_messages")
    facts = [(k, " ".join(v)) for k, v in rows.items()]
    ctx.last_facts = facts
    return Out([[Call("summarize_messages")]], bullets(facts), "summarize_messages")


def h_person(ctx, q):
    who = ctx.who or ctx.rng.choice(["maya", "theo", "carmen", "nadia"])
    ctx.who = who
    name, first = C.PEOPLE[who][1], C.PEOPLE[who][4]
    if not any(m.conv == name for m in ctx.w.messages):
        ctx.w.messages.append(Msg(name, name, "Want to grab lunch on Friday? I'm free after noon.", ctx.now - D(hours=5), unread=True))
    args = {"query": first}
    if "week" in q.lower():
        args["period"] = "this week"
    said = [m.text for m in ctx.w.messages if m.conv == name and m.sender != "Me"]
    return Out([[Call("view_messages", **args)]], f"{first} said: “{said[-1]}”", "view_messages")


def h_emails(ctx, q):
    w, now = ctx.w, ctx.now
    args = {"period": "today"} if "today" in q.lower() else {}
    text = w.t_summarize_emails(**args)
    m = re.search(r"(\d+) emails?(?: · (\d+) unread)?", text)
    if not m:
        return Out([[Call("summarize_emails", **args)]], "Nothing new in your inbox.", "summarize_emails")
    lead = f"You have {m[1]} email{'s' if m[1] != '1' else ''}" + (f", {m[2]} unread." if m[2] else ".")
    people = [e for e in sorted(w.emails, key=lambda e: e.when, reverse=True) if e.person and e.when <= now][:3]
    rows = [(e.sender, e.subject) for e in people if e.sender in text and e.subject in text]
    return Out([[Call("summarize_emails", **args)]], lead + ("\n" + bullets(rows) if rows else ""), "summarize_emails")


def h_reminders(ctx, q):
    rows = [r for r in ctx.w.reminders if not r.done]
    if not rows:
        return Out([[Call("search_reminders")]], "Your reminders list is empty.", "search_reminders")
    facts = [(r.title, f"{r.due:%a} {clock(r.due)}" if r.due else "no date") for r in rows[:8]]
    ctx.last_facts = facts
    return Out([[Call("search_reminders")]], bullets(facts), "search_reminders")


def h_calendar(ctx, q):
    w, now = ctx.w, ctx.now
    rows = [(clock(e.start), e.title) for e in sorted(w.events, key=lambda e: e.start) if e.start.date() == now.date() and e.start > now and not e.all_day]
    args = {"period": "today", "calendar_only": True}
    if not rows:
        return Out([[Call("get_upcoming", **args)]], "Nothing else on your calendar today.", "get_upcoming")
    ctx.last_facts = rows
    return Out([[Call("get_upcoming", **args)]], bullets(rows), "get_upcoming")


def h_base(ctx, q):
    return Out([], BASE_WRITES)


def h_web(ctx, q):
    low = q.lower()
    for pat, query, hit, final in WEB:
        if re.search(pat, low):
            ctx.w.web[query] = [(hit, "https://example.org/news", f"{hit}, according to today's coverage.")]
            return Out([[Call("web_search", query=query)]], final, "web_search")
    raise ValueError("no web answer for this wording")


def h_market(ctx, q):
    up = q.upper()
    for word, row in INDEX.items():
        if word in up:
            sym, name, px, prev = row
            C.TICKS[sym] = (name, name, px, prev)
            src = C.src_stock(ctx.rng, ctx.w, ctx.now, [sym])
            return Out([src.calls], src.text, "get_stock_price")
    for word, sym in STOCKS.items():
        if word in up:
            if sym not in C.TICKS:
                C.TICKS[sym] = (word.title(), word.title(), 182.40, 180.15)
            src = C.src_stock(ctx.rng, ctx.w, ctx.now, [sym])
            return Out([src.calls], src.text, "get_stock_price")
    raise ValueError("no stock for this wording")


def _sent_final(ctx, how):
    return ctx.rng.choice([f"Sent to {name_of(ctx)} by {how}.", f"{name_of(ctx)} has it, by {how}.", f"Done, that went to {name_of(ctx)} by {how}."])


def _need_person(ctx):
    if not ctx.who:
        ctx.who = ctx.rng.choice(TEXT_TO)


def h_send_this(ctx, q):
    _need_person(ctx)
    if not ctx.last_facts:
        raise ValueError("nothing shown")
    text = "; ".join(f"{a}: {b}" for a, b in ctx.last_facts) + "."
    ctx.draft, ctx.draft_read, ctx.last_channel = text, "", "text"
    return Out([[Call("send_message", to=contact_of(ctx), text=text)], ], _sent_final(ctx, "text"), "send_message", ["send_message"], [True])


def _send_schedule(ctx, q, arg, word):
    _need_person(ctx)
    plan = read_plan(ctx.w, arg)
    text = plain_schedule(ctx, plan, word)
    ctx.draft, ctx.draft_read = text, arg
    ctx.last_when = word
    read = [Call("get_my_day_plan", day=arg)]
    low = q.lower()
    ctx.last_channel = "email" if "email" in low else "text"
    if "email" in low:
        mail = C.PEOPLE[ctx.who][3]
        if not mail:
            raise ValueError("no saved address")
        return Out([read, [Call("send_email", to=contact_of(ctx), subject=f"My schedule {word}", body=text)]],
                   _sent_final(ctx, "email"), "send_email", ["send_email"], [True])
    return Out([read, [Call("send_message", to=contact_of(ctx), text=text)]], _sent_final(ctx, "text"), "send_message", ["send_message"], [True])


def h_send_day(ctx, q):
    low = q.lower()
    word = "today" if "today" in low else "tomorrow"
    return _send_schedule(ctx, q, word, word)


def h_send_week(ctx, q):
    return _send_schedule(ctx, q, "this_week", "this week")


def _resend(ctx, call):
    """Production history keeps only the earlier reply ("Sent to Dad by text."), so a body no reply showed is read again this turn."""
    return ([[Call("get_my_day_plan", day=ctx.draft_read)]] if ctx.draft_read else []) + [[call]]


def h_channel(ctx, q):
    if not ctx.who:
        ctx.who = ctx.rng.choice(EMAIL_TO)
    if not ctx.draft:
        ctx.draft, ctx.draft_read = plain_schedule(ctx, read_plan(ctx.w, "tomorrow"), "tomorrow"), "tomorrow"
    wanted = "text" if "text" in q.lower() or "message" in q.lower() else "email"
    if wanted == ctx.last_channel:
        raise ValueError("already sent that way: this is a repeat, not a switch")
    ctx.last_channel = wanted
    if wanted == "text":
        return Out(_resend(ctx, Call("send_message", to=contact_of(ctx), text=ctx.draft)), _sent_final(ctx, "text"), "send_message", ["send_message"], [True])
    if not C.PEOPLE[ctx.who][3]:
        contact, phone = contact_of(ctx), C.PEOPLE[ctx.who][2]
        ctx.w.overrides["lookup_contact"] = lambda args: f"{contact}: phone {phone}"
        return Out([[Call("lookup_contact", name=contact)]], f"{name_of(ctx)}'s contact only has a phone number, no email. What's the address?", "lookup_contact")
    return Out(_resend(ctx, Call("send_email", to=contact_of(ctx), subject="My schedule", body=ctx.draft)), _sent_final(ctx, "email"), "send_email", ["send_email"], [True])


def h_address(ctx, q):
    found = re.search(r"[\w.]+@[\w.]+\w", q)
    if not found or not ctx.draft:
        raise ValueError("no address or draft")
    return Out(_resend(ctx, Call("send_email", to=found[0], subject="My schedule", body=ctx.draft)), f"Sent to {found[0]}.", "send_email", ["send_email"], [True])


def topic_of(q):
    m = re.search(r"\babout (?:the |my )?(.+?)(?: in my email)?[?.]*$", q, re.I)
    words = (m[1] if m else re.sub(r"[?.]", "", q)).lower().split()
    words = [x for x in words if x not in STOP]
    if not words:
        raise ValueError("no topic")
    return " ".join(words[-2:]) if m else words[-1]


def model_view(ctx, topic):
    """The email result as the model receives it (production's per-result cap in model-led Ask)."""
    from service.agent.loop import _fit_tool_result
    from service.model_only import ASK_TOOL_RESULT_CHARS
    return _fit_tool_result(ctx.w.t_view_emails(query=topic), "view_emails", ASK_TOOL_RESULT_CHARS)


def none_about(ctx, topic):
    """Honest when no result matches: all of them read, or only the part that fit."""
    view = model_view(ctx, topic)
    if any(sign in view for sign in ("cut to fit", "header line", "had no room", "omitted by count")):
        return (f"Nothing about {topic} in the part of the results I could read: those newsletters only mention it. "
                "The list got cut off, so I can't rule out an older email.")
    return f"I read all the results and nothing is really about {topic}. The newsletters that came up only mention it, with nothing to act on."


def mail_list(ctx, topic, *, has_match=True, size=1000, lead=None):
    """Five results through the host's own ranking. Plain: the real email has the topic in its subject and ranks first.
    Lead: newsletters have the topic in their subjects and rank first, and the real email only has it in its text, so
    it comes last and the model has to read past them."""
    rng, now = ctx.rng, ctx.now
    lead = rng.random() < 0.5 if lead is None else lead
    ctx.w.emails = [e for e in ctx.w.emails if topic.lower() not in (e.subject + e.body).lower()]
    senders = rng.sample([("Campus News", "news@linden.edu"), ("Hollis Library", "library@linden.edu"), ("Student Life", "life@linden.edu"),
                          ("Peak Climbing Gym", "hello@peakclimb.example"), ("Linden Alumni", "alumni@linden.edu")], 4)
    passing = [f"{FILLER * (size // len(FILLER))}There's a short mention of {topic} further down this newsletter, but nothing you need to act on."
               for _ in senders]
    subjects = ([f"{topic.capitalize()} and more this week", f"This week: {topic}, events and more", f"Reminders, including {topic}", f"News roundup: {topic}"]
                if lead else ["This week on campus", "Your weekly digest", "Spring events", "Member update"])
    for i, (name, addr) in enumerate(senders):
        ctx.w.emails.append(Email(name, addr, rng.choice(subjects), now - D(hours=2 + 3 * i), passing[i], person=False))
    if not has_match:
        ctx.w.emails.append(Email("Linden Alumni", "alumni@linden.edu", "Alumni news", now - D(days=2), passing[0], person=False))
        return None
    code, day = f"REF-{rng.randint(1000, 9999)}", (now + D(days=rng.randint(3, 9))).strftime("%A, %B %-d")
    subject = "A quick question for you" if lead else rng.choice(MAIL_SUBJECTS).format(t=topic, T=topic.capitalize())
    body = f"Hi Sam, about your {topic}: please confirm by {day}. Your reference is {code}. Reply to this email with any questions."
    ctx.w.emails.append(Email("Dana Whitfield", "dana.whitfield@linden.edu", subject, now - D(hours=3 + 3 * rng.choice([0, 1])), body))
    if code not in model_view(ctx, topic):
        raise ValueError("the match fell past what the model can read")
    return subject, day, code


def h_email_search(ctx, q):
    topic = topic_of(q)
    if ctx.rng.random() < 0.2:
        mail_list(ctx, topic, has_match=False, size=ctx.rng.choice([700, 1000, 1200]))
        return Out([[Call("view_emails", query=topic)]], none_about(ctx, topic), "view_emails")
    subject, day, code = mail_list(ctx, topic, size=ctx.rng.choice([700, 1000, 1200]))
    return Out([[Call("view_emails", query=topic)]],
               f"Yes, Dana Whitfield emailed you: “{subject}”. Please confirm by {day}, and your reference is {code}.\n\nThe others are newsletters that only mention it.",
               "view_emails")


# ------------------------------------------------------------------------- the other debug logs' question types
HEADLINES = [("City council", "The council approved a new transit budget after a long debate."),
             ("Weather", "A cold front is bringing heavy rain to the northwest this weekend."),
             ("Markets", "Stocks ended mixed as investors waited on a central bank decision."),
             ("Science", "Researchers reported a new way to recycle battery materials."),
             ("Sports", "The league announced its schedule for next season."),
             ("Health", "A large study linked regular walking to better sleep.")]
PLACE = re.compile(r"\b(iran|nepal|ukraine)\b")
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
TIME = re.compile(r"(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s*(am|pm)")


def _tomorrow(low):
    return bool(re.search(r"\b(tomorrow|tommorow|tmrow|tmrw)\b", low))


def h_news(ctx, q):
    rows = ctx.rng.sample(HEADLINES, 3)
    query = "top news today"
    ctx.w.web[query] = [(f"{a}: {b}", f"https://example.org/news/{i}", b) for i, (a, b) in enumerate(rows)]
    ctx.last_facts = rows
    return Out([[Call("web_search", query=query)]], "Here's what's in the news today:\n" + bullets(rows), "web_search")


def _place_read(ctx, q):
    m = PLACE.search(q.lower())
    if not m:
        raise ValueError("no place in this wording")
    name = m[1].title()
    query, fact = f"{name} news", f"Talks between officials in {name} resumed this week, and aid groups said deliveries have picked up."
    ctx.w.web[query] = [(f"{name}: talks resume", "https://example.org/world", fact)]
    return Call("web_search", query=query), fact


def h_place_news(ctx, q):
    call, fact = _place_read(ctx, q)
    return Out([[call]], fact, "web_search")


def h_place_news_send(ctx, q):
    _need_person(ctx)
    call, fact = _place_read(ctx, q)
    return Out([[call], [Call("send_message", to=contact_of(ctx), text=fact)]], _sent_final(ctx, "text"), "send_message", ["send_message"], [True])


def _range(low):
    if _tomorrow(low):
        return {"period": "tomorrow"}, "tomorrow", 1
    if "month" in low:
        return {"period": "this month"}, "this month", 31
    return {"period": "this week"}, "this week", 7


def _event_rows(ctx, args, span):
    text = ctx.w.t_get_upcoming(calendar_only=True, **args)
    end = ctx.now + D(days=span)
    return [(f"{e.start:%a} {clock(e.start)}", e.title) for e in sorted(ctx.w.events, key=lambda e: e.start)
            if ctx.now < e.start <= end and e.title in text]


def h_calendar_range(ctx, q):
    args, word, span = _range(q.lower())
    rows = _event_rows(ctx, args, span)
    args = {**args, "calendar_only": True}
    ctx.last_when = word
    if not rows:
        return Out([[Call("get_upcoming", **args)]], f"Nothing on your calendar {word}.", "get_upcoming")
    ctx.last_facts = rows
    return Out([[Call("get_upcoming", **args)]], f"On your calendar {word}:\n" + bullets(rows), "get_upcoming")


def h_plan_named(ctx, q):
    day = next((d for d in DAYS if d in q.lower()), None)
    if not day:
        raise ValueError("no day in this wording")
    return _plan(ctx, day, f"Here's {day.title()}:", day.title())


def h_stock_quote(ctx, q):
    up = q.upper()
    syms = [sym for word, sym in STOCKS.items() if word in up]
    if not syms:
        raise ValueError("no stock in this wording")
    for sym in syms:
        name = next(w for w, s in STOCKS.items() if s == sym).title()
        C.TICKS.setdefault(sym, (name, name, 182.40, 180.15))
    src = C.src_stock(ctx.rng, ctx.w, ctx.now, syms)
    return Out([src.calls], src.text, "get_stock_price")


def h_attention(ctx, q):
    return _plan(ctx, "today", "Here's what needs you first:", "today")


def h_know_me(ctx, q):
    ctx.w.memories = list(ctx.w.memories) + ["You prefer window seats on flights.", "You're allergic to shellfish.", "Your sister's name is Lena."]
    return Out([[Call("recall", query="")]], BASE_WRITES, "recall")


def h_weather_week(ctx, q):
    m = re.search(r"weather in ([a-z ]+?) for", q.lower())
    if not m:
        raise ValueError("no city in this wording")
    city = m[1].title()
    days = [((ctx.now + D(days=i)).strftime("%a"), 58 + i, 44 + i, "Partly cloudy" if i % 2 else "Light rain", 20 + 10 * (i % 3)) for i in range(1, 6)]
    ctx.w.weather[city] = {"label": city, "now_f": 55, "desc": "Cloudy", "feels_f": 53, "hum": 70, "wind": 6, "days": days}
    rows = [(d[0], f"{d[1]}° high, {d[2]}° low, {d[3].lower()}") for d in days]
    return Out([[Call("get_weather", location=city, period="this week")]], f"This week in {city}:\n" + bullets(rows), "get_weather")


def h_find_time(ctx, q):
    m = re.search(r"(\d+)-minute", q)
    mins = int(m[1]) if m else 60
    text = ctx.w.t_find_free_time(period="tomorrow", minutes=mins)
    slots = re.findall(r"(\d{1,2}:\d\d [AP]M–\d{1,2}:\d\d [AP]M) \(", text)
    if not slots:
        raise ValueError("no open slot")
    call = Call("find_free_time", period="tomorrow", minutes=mins)
    if len(slots) == 1:
        return Out([[call]], f"Tomorrow's opening is {slots[0]}.", "find_free_time")
    return Out([[call]], "You have room tomorrow at these times:\n" + "\n".join(f"- {x}" for x in slots[:5]), "find_free_time")


def _when(ctx, low):
    m = TIME.search(low)
    if not m:
        raise ValueError("no time in this wording")
    hour = int(m[1]) % 12 + (12 if m[3] == "pm" else 0)
    end = int(m[2]) % 12 + (12 if m[3] == "pm" else 0) if m[2] else hour + 1
    day = ctx.now.date() + D(days=1)
    for i, name in enumerate(DAYS):
        if re.search(rf"\b{name}\b", low):
            day = ctx.now.date() + D(days=(i - ctx.now.weekday() - 1) % 7 + 1)
    return dt.datetime.combine(day, dt.time(hour)), (end - hour) * 60


def _title(low):
    m = re.search(r"(?:pm|am) to (.+)$", low) or re.search(r"\bfor ((?:(?!this |tomorrow|tommorow)[a-z0-9 ])+?) from ", low)
    if not m:
        raise ValueError("no title in this wording")
    return m[1].strip().capitalize()


def h_add_reminder_said(ctx, q):
    low = q.lower()
    when, _ = _when(ctx, low)
    title = _title(low)
    call = Call("add_reminder", title=title, when_iso=when.strftime("%Y-%m-%dT%H:%M"))
    return Out([[call]], f"Reminder set for {when:%A} at {clock(when)}: {title}.", "add_reminder")


def h_add_event_said(ctx, q):
    low = q.lower()
    when, mins = _when(ctx, low)
    title = _title(low)
    call = Call("add_calendar_event", title=title, when_iso=when.strftime("%Y-%m-%dT%H:%M"), duration_min=mins)
    return Out([[call]], f"{title} is on your calendar for {when:%A} at {clock(when)}.", "add_calendar_event", ["add_calendar_event"], [True])


def _flat(text):
    return re.sub(r"\s*\n- ", "; ", text.replace("**", "")).strip()


def h_send_summary(ctx, q):
    _need_person(ctx)
    read = h_messages(ctx, q) if "messages" in q.lower() else h_emails(ctx, q)
    body = _flat(read.final)
    send = [Call("send_message", to=contact_of(ctx), text=body)]
    return Out([*read.steps, send], _sent_final(ctx, "text"), "send_message", ["send_message"], [True])


def h_send_calendar_range(ctx, q):
    _need_person(ctx)
    m = re.search(r"next (\d+|two) (days|weeks)", q.lower())
    span = 14 if not m else (14 if m[1] == "two" else int(m[1]))
    rows = _event_rows(ctx, {"days": span}, span)
    if not rows:
        raise ValueError("nothing to send")
    body = "My schedule: " + "; ".join(f"{a}: {b}" for a, b in rows) + "."
    read, send = Call("get_upcoming", days=span, calendar_only=True), Call("send_message", to=contact_of(ctx), text=body)
    return Out([[read], [send]], _sent_final(ctx, "text"), "send_message", ["send_message"], [True])


def h_draft_reply(ctx, q):
    _need_person(ctx)
    name, first = C.PEOPLE[ctx.who][1], C.PEOPLE[ctx.who][4]
    said = "Are you around Friday? I could use a hand moving a few boxes."
    ctx.w.messages.append(Msg(name, name, said, ctx.now - D(hours=4), unread=True))
    text = f"Hi {first}, yes, I can help on Friday. What time works for you?"
    steps = [[Call("view_messages", conversation=contact_of(ctx))], [Call("draft_message", to=contact_of(ctx), text=text)]]
    return Out(steps, None, "draft_message")    # the host ends the turn with the editable draft card


HANDLERS = {"greeting": h_greeting, "plan_today": h_plan_today, "plan_tomorrow": h_plan_tomorrow, "plan_week": h_plan_week,
            "month_ahead": h_month_ahead, "spend_day": h_spend_day, "assent_to_offer": h_assent, "dont_see_it": h_dont_see,
            "reformat": h_reformat, "messages_overview": h_messages, "person_said": h_person, "follow_emails": h_emails,
            "follow_reminders": h_reminders, "calendar_today": h_calendar, "knowledge": h_base, "formula": h_base,
            "goal_chip": h_base, "current_fact": h_web, "market_move": h_market, "send_this": h_send_this,
            "send_schedule_day": h_send_day, "send_schedule_week": h_send_week, "channel_switch": h_channel,
            "supplied_address": h_address, "email_topic_search": h_email_search}
HANDLERS.update({"news_today": h_news, "place_news": h_place_news, "place_news_send": h_place_news_send, "email_overview": h_emails,
                 "reminders_overview": h_reminders, "calendar_range": h_calendar_range, "plan_named_day": h_plan_named,
                 "stock_quote": h_stock_quote, "attention": h_attention, "know_me": h_know_me, "capability": h_base,
                 "weather_week": h_weather_week, "find_time": h_find_time, "add_reminder_said": h_add_reminder_said,
                 "add_event_said": h_add_event_said, "send_summary": h_send_summary, "send_calendar_range": h_send_calendar_range,
                 "draft_reply": h_draft_reply})
# A shape that continues a conversation runs this one first when it is built alone.
BEFORE = {"reformat": "plan_week", "send_this": "plan_today", "assent_to_offer": "spend_day", "dont_see_it": "assent_to_offer",
          "channel_switch": "send_schedule_day", "supplied_address": "send_schedule_day", "follow_emails": "messages_overview",
          "follow_reminders": "follow_emails"}


def _key(q):
    return re.sub(r"[^a-z0-9{}@ ]", "", q.lower()).strip()


class Shapes(dict):
    """Question to shape. A chat's message can differ from its seed wording by case or punctuation."""

    def __missing__(self, q):
        raise KeyError(f"a chain message has no shape in the seeds file ({len(q)} chars)")

    def __getitem__(self, q):
        return dict.__getitem__(self, _key(q))


def shape_of(seeds):
    return Shapes({_key(q): s["id"] for s in seeds["shapes"] for q in s["train"]})


def first_q(seeds, shape, rng):
    s = next(x for x in seeds["shapes"] if x["id"] == shape)
    return rng.choice(s["train"])


def history_turn(ctx, shape, q):
    """Run one shape as an earlier turn, the way production stores it: the user's text and the final assistant text."""
    q = fill(ctx, q)
    out = HANDLERS[shape](ctx, q)
    ctx.history.append(turn(q, out.final or "Okay.", out.tool, []))
    return q


def scenario(ctx, shape, q, thread, note):
    q = fill(ctx, q)
    out = HANDLERS[shape](ctx, q)
    steps = list(out.steps)
    if out.final is not None:
        steps.append(Final(out.final))
    ctx.w.history = ctx.history
    sc = Scenario(FAMILY, q, ctx.now, ctx.w, steps, note)
    sc.history = ctx.history
    sc.thread, sc.qkind = thread, shape
    sc.leak_ok = True      # the user's own wording, kept on purpose; the gate reports it apart
    if out.cards:
        sc.expect_cards = list(out.cards)
    if out.approve is not None:
        sc.approve = list(out.approve)
    return sc


def make_chain(chain, i, shapes, tid):
    def make(rng):
        ctx = new_ctx(rng)
        for q in chain[:i]:
            history_turn(ctx, shapes[q], q)
        return scenario(ctx, shapes[chain[i]], chain[i], tid, f"replay of a real chat's order, turn {i + 1}: {shapes[chain[i]]}")
    return make


def make_single(seeds, shape, q, tid):
    def make(rng):
        ctx = new_ctx(rng)
        chain, cur = [], shape
        while cur in BEFORE:
            cur = BEFORE[cur]
            chain.append(cur)
        for prior in reversed(chain):
            history_turn(ctx, prior, first_q(seeds, prior, rng))
        return scenario(ctx, shape, q, tid if ctx.history else None, f"a variation of the user's question: {shape}")
    return make


def attempt(make, rng, label, tries=8):
    last = None
    for _ in range(tries):
        seed = rng.random()
        try:
            sc = make(random.Random(seed))
        except Exception as exc:  # noqa: BLE001
            last = f"build {exc!r:.160}"
            continue
        ok, why = F._check(sc)
        if ok and leak.fold(sc.prompt) in heldout_folds():
            ok, why = False, "a held-out wording"
        if ok:
            return make(random.Random(seed))
        last = why
    DROPPED.append((label, last))
    return None


def fam_debug_log_r5(rng, *, chain_runs=2, single_runs=1):
    seeds = load_seeds()
    if seeds is None:
        return []
    shapes, out, DROPPED[:] = shape_of(seeds), [], []
    for run in range(chain_runs):
        for name, chain in seeds["chains"].items():
            for i, q in enumerate(chain):
                sc = attempt(make_chain(chain, i, shapes, f"r5-{name}-{run}"), rng, shapes[q])
                if sc is not None:
                    out.append(sc)
    # The other logs' chats, cut where a message has no row or is held out. Once each: they lean on the shapes above.
    for name, chain in seeds.get("chains_once", {}).items():
        for i, q in enumerate(chain):
            sc = attempt(make_chain(chain, i, shapes, f"r5-{name}"), rng, shapes[q])
            if sc is not None:
                out.append(sc)
    for run in range(single_runs):
        for shape in seeds["shapes"]:
            for j, q in enumerate(shape["train"]):
                sc = attempt(make_single(seeds, shape["id"], q, f"r5-{shape['id']}-{run}-{j}"), rng, shape["id"])
                if sc is not None:
                    out.append(sc)
    return out


# ------------------------------------------------------------------------------------ long email lists
# No seed wording here. These rows use their own topics and questions and go through the normal leak check.
LIST_TOPICS = ["parking permit", "lab fee", "gym membership", "study abroad", "textbook refund", "bike storage", "meal plan",
               "transcript request", "dorm repair", "tuition payment", "field trip", "locker rental", "club dues",
               "health form", "exam accommodation", "laptop loan", "summer program", "shuttle pass", "printing credit", "tutor signup"]
LIST_ASKS = ["do i have any mail on my {t}", "check my mail for {t} stuff", "did the {t} thing ever come through email",
             "look through my email for {t}", "was there an email on {t}", "find that {t} email"]


def make_list_row(topic, ask, has_match, size, tid):
    def make(rng):
        ctx = new_ctx(rng)
        got = mail_list(ctx, topic, has_match=has_match, size=size)
        if got is None:
            final = none_about(ctx, topic)
        else:
            subject, day, code = got
            final = (f"Found it. Dana Whitfield wrote “{subject}”: please confirm by {day}, and your reference is {code}.\n\n"
                     "The others are newsletters that only mention it.")
        sc = Scenario(LIST_FAMILY, ask, ctx.now, ctx.w, [[Call("view_emails", query=topic)], Final(final)],
                      "a long result list: read every result, prefer the email written to the user over newsletters, and say nothing is relevant only after reading all")
        sc.qkind = "long_list"
        return sc
    return make


def fam_long_email_r5(rng):
    out = []
    for i in range(40):
        topic, template = LIST_TOPICS[i % len(LIST_TOPICS)], LIST_ASKS[i % len(LIST_ASKS)]
        size = [800, 1000, 1500, 2500][i % 4]   # the larger lists share the budget and end in the host's own cut note
        sc = attempt(make_list_row(topic, template.format(t=topic), i % 5 != 4, size, None), rng, "long_list")
        if sc is not None:
            out.append(sc)
    return out


def heldout_folds():
    """Folded held-out wordings. No row of any family may ask one, or the gate's variation test proves nothing."""
    seeds = load_seeds()
    if seeds is None:
        return frozenset()
    return frozenset(leak.fold(q) for shape in seeds["shapes"] for q in shape["heldout"] if "{" not in q)


def unwritten(rows):
    """Finals base still has to write. A final build must have none."""
    return [r for r in rows if r.get("messages", [{}])[-1].get("content") == BASE_WRITES]


FAMILIES_R5 = {FAMILY: fam_debug_log_r5, LIST_FAMILY: fam_long_email_r5}
