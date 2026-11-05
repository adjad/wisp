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
WRITE_ALLOW_R5 = {FAMILY: frozenset({"add_reminder", "send_message", "send_email"})}
SEEDS = os.environ.get("ASK_LORA_R5_SEEDS") or os.path.expanduser("~/.moe/evals/private-real-use/r5-seeds/questions.yaml")
BASE_WRITES = "Base writes this answer."
DROPPED: list = []
TEXT_TO = ["mom", "dad", "maya", "theo", "carmen", "nadia", "priya"]
HER = ["mom", "maya", "carmen", "nadia", "priya"]
HIM = ["dad", "theo"]
EMAIL_TO = ["dad", "maya", "theo", "carmen", "nadia", "priya"]
INDEX = {"S&P": ("^GSPC", "S&P 500", 5871.20, 5842.10), "DOW": ("^DJI", "Dow", 43120.50, 43310.25),
         "NASDAQ": ("^IXIC", "Nasdaq", 18944.75, 18790.40), "MARKET": ("^GSPC", "S&P 500", 5871.20, 5842.10)}
STOCKS = {"TESLA": "TSLA"}
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
    return f"{day_word.capitalize()} I have {join(items)}."


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
               f"{tail}\n\nWant a reminder to get ready for {title}?", "get_my_day_plan")


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
            ctx.w.web[query] = [{"title": hit, "url": "https://example.org/news", "snippet": f"{hit}, according to today's coverage."}]
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
    ctx.draft = text
    return Out([[Call("send_message", to=contact_of(ctx), text=text)], ], _sent_final(ctx, "text"), "send_message", ["send_message"], [True])


def _send_schedule(ctx, q, arg, word):
    _need_person(ctx)
    plan = read_plan(ctx.w, arg)
    text = plain_schedule(ctx, plan, word)
    ctx.draft = text
    ctx.last_when = word
    read = [Call("get_my_day_plan", day=arg)]
    low = q.lower()
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


def h_channel(ctx, q):
    if not ctx.who:
        ctx.who = ctx.rng.choice(EMAIL_TO)
    if not ctx.draft:
        ctx.draft = plain_schedule(ctx, read_plan(ctx.w, "tomorrow"), "tomorrow")
    if "text" in q.lower():
        return Out([[Call("send_message", to=contact_of(ctx), text=ctx.draft)]], _sent_final(ctx, "text"), "send_message", ["send_message"], [True])
    if not C.PEOPLE[ctx.who][3]:
        return Out([], f"I don't have an email saved for {name_of(ctx)}. What's the address?")
    return Out([[Call("send_email", to=contact_of(ctx), subject="My schedule", body=ctx.draft)]], _sent_final(ctx, "email"), "send_email", ["send_email"], [True])


def h_address(ctx, q):
    found = re.search(r"[\w.]+@[\w.]+\w", q)
    if not found or not ctx.draft:
        raise ValueError("no address or draft")
    return Out([[Call("send_email", to=found[0], subject="My schedule", body=ctx.draft)]], f"Sent to {found[0]}.", "send_email", ["send_email"], [True])


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
    if "were cut" in model_view(ctx, topic):
        return (f"Nothing about {topic} in the part of the results I could read: those newsletters only mention it in passing. "
                "The list got cut off, so I can't rule out an older email.")
    return f"I read all the results and nothing is really about {topic}. A few newsletters mention it in passing, with nothing to act on."


def mail_list(ctx, topic, *, has_match=True, size=1000):
    """Five results: the newest mention the topic in passing, and the real match, if any, is older."""
    rng, now = ctx.rng, ctx.now
    ctx.w.emails = [e for e in ctx.w.emails if topic.lower() not in (e.subject + e.body).lower()]
    senders = rng.sample([("Campus News", "news@linden.edu"), ("Hollis Library", "library@linden.edu"), ("Student Life", "life@linden.edu"),
                          ("Peak Climbing Gym", "hello@peakclimb.example"), ("Linden Alumni", "alumni@linden.edu")], 4)
    passing = [f"{FILLER * (size // len(FILLER))}There's a short mention of {topic} further down this newsletter, but nothing you need to act on."
               for _ in senders]
    for i, (name, addr) in enumerate(senders):
        ctx.w.emails.append(Email(name, addr, rng.choice(["This week on campus", "Your weekly digest", "Spring events", "Member update"]),
                                  now - D(hours=2 + 3 * i), passing[i], person=False))
    if not has_match:
        ctx.w.emails.append(Email("Linden Alumni", "alumni@linden.edu", "Alumni news", now - D(days=2), passing[0], person=False))
        return None
    code, day = f"REF-{rng.randint(1000, 9999)}", (now + D(days=rng.randint(3, 9))).strftime("%A, %B %-d")
    subject = rng.choice(MAIL_SUBJECTS).format(t=topic, T=topic.capitalize())
    body = f"Hi Sam, about your {topic}: please confirm by {day}. Your reference is {code}. Reply to this email with any questions."
    # Second or third newest: never first, and still inside what the model gets (checked below).
    ctx.w.emails.append(Email("Dana Whitfield", "dana.whitfield@linden.edu", subject, now - D(hours=3 + 3 * rng.choice([0, 1])), body))
    if code not in model_view(ctx, topic):
        raise ValueError("the match fell past the model's cap")
    return subject, day, code


def h_email_search(ctx, q):
    topic = topic_of(q)
    if ctx.rng.random() < 0.2:
        mail_list(ctx, topic, has_match=False, size=ctx.rng.choice([700, 1000, 1200]))
        return Out([[Call("view_emails", query=topic)]], none_about(ctx, topic), "view_emails")
    subject, day, code = mail_list(ctx, topic, size=ctx.rng.choice([700, 1000, 1200]))
    return Out([[Call("view_emails", query=topic)]],
               f"Yes, Dana Whitfield emailed you: “{subject}”. Please confirm by {day}, and your reference is {code}.\n\nThe others only mention it in passing.",
               "view_emails")


HANDLERS = {"greeting": h_greeting, "plan_today": h_plan_today, "plan_tomorrow": h_plan_tomorrow, "plan_week": h_plan_week,
            "month_ahead": h_month_ahead, "spend_day": h_spend_day, "assent_to_offer": h_assent, "dont_see_it": h_dont_see,
            "reformat": h_reformat, "messages_overview": h_messages, "person_said": h_person, "follow_emails": h_emails,
            "follow_reminders": h_reminders, "calendar_today": h_calendar, "knowledge": h_base, "formula": h_base,
            "goal_chip": h_base, "current_fact": h_web, "market_move": h_market, "send_this": h_send_this,
            "send_schedule_day": h_send_day, "send_schedule_week": h_send_week, "channel_switch": h_channel,
            "supplied_address": h_address, "email_topic_search": h_email_search}
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
                     "The newer results only mention it in passing.")
        sc = Scenario(LIST_FAMILY, ask, ctx.now, ctx.w, [[Call("view_emails", query=topic)], Final(final)],
                      "a long result list: read every result, prefer a subject match, and say nothing is relevant only after reading all")
        sc.qkind = "long_list"
        return sc
    return make


def fam_long_email_r5(rng):
    out = []
    for i in range(40):
        topic, template = LIST_TOPICS[i % len(LIST_TOPICS)], LIST_ASKS[i % len(LIST_ASKS)]
        size = [800, 1000, 1200, 1500][i % 4]   # the larger lists pass the model's 6,000-char cap and end in the real cut note
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
