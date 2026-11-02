"""Round-3 family ``compose_r3``: gather the content, then send or draft it in the SAME turn.

Why (compose is Gemma's weakest toolbench category: 10/25 untuned in
20261101-151918, 14/25 for r2 in 20261101-123417). Failing compose rows, by cluster:
 1. Gathers, then stops: get_upcoming / get_weather / lookup_contact is the last call and
    the final is prose ("I'm happy to help you get that to your dad!") with no send.
 2. Claims it sent with no card and no receipt ("I've taken care of that", "sent to Mom").
 3. Wrong channel: send_email for a text request (portfolio_dad, daily_brief_dad, stock
    report, r2); draft_email / draft_message when the user said send (voice_email_dad,
    multipart_report, bitcoin_price_text, draft_events_message: "no verified receipt").
 4. Wrong recipient or contact: texts "Sam" (the user) instead of the friend, "I don't have
    a dad yet", lookup_contact(query=...) instead of name, stops after the lookup.
 5. Bad args: get_upcoming without period, send_email to self without confirmed_self_send
    (host answers "I didn't send it"), schedule_send with content / recipient / when_iso.
 6. Multi-source: sends before every read is done, or fetches stocks with no symbols,
    invents Apple when the held tickers are in memory, asks which stocks.
 7. Body is wrong: written to the user ("Hi Tess, here is your schedule") or generic
    ("Here is my calendar!"), and drops the facts the tools returned (voice_cal_mom).
 8. Final misreports a denied or failed card ("I haven't sent anything").
This module teaches the fixes in new wordings. Texts go through send_message, emails through
send_email, "draft"/"don't send" through draft_email (a text draft cannot be built: the
harness strips draft_message's class), schedule_send only with an explicit later time, a
self email carries confirmed_self_send, recipients resolve from Contacts, the body is
first person (Sam writing to the recipient) with the tool facts, and the final relays the
card outcome: approved means sent, denied ends the turn at the call.
"""
from __future__ import annotations

import datetime as dt

import harness as H
from harness import Call, Final
import gen
from gen import D
from replies import clock, join, ts
from scen import Scenario, setup
from world import Email, Note
from service.tools.timeranges import resolve_when

FAMILY = "compose_r3"
OWN = H.USER_EMAILS[0]

WRITE_ALLOW_R3 = {FAMILY: frozenset({"send_message", "send_email", "schedule_send", "draft_email"})}

# ----------------------------------------------------------------------------- people
# key -> (prompt word, contact name, phone, email, greeting name)
PEOPLE = {
    "mom": ("mom", "Mom", "(503) 555-0142", "", "Mom"),
    "dad": ("dad", "Dad", "(503) 555-0102", "dad.rivera@example.com", "Dad"),
    "maya": ("maya", "Maya Chen", "(503) 555-0187", "maya.chen@example.com", "Maya"),
    "theo": ("theo", "Theo Park", "(971) 555-0110", "theo.park@example.com", "Theo"),
    "carmen": ("carmen", "Carmen Rivera", "(503) 555-0163", "carmen.rivera@example.com", "Carmen"),
    "nadia": ("nadia", "Nadia Ortiz", "(971) 555-0152", "nadia.ortiz@example.com", "Nadia"),
    "priya": ("priya", "Priya Nair", "(503) 555-0129", "priya.nair@example.com", "Priya"),
    "okafor": ("prof okafor", "Prof. Okafor", "", "aokafor@linden.edu", "Professor Okafor"),
}
TEXTERS = ["mom", "dad", "maya", "theo", "carmen", "nadia", "priya"]
EMAILERS = ["dad", "maya", "theo", "carmen", "nadia", "priya", "okafor"]


def to_name(key):
    """The `to` value the model passes: what the user typed, capitalised ('Prof. Okafor')."""
    return {"okafor": "Prof. Okafor"}.get(key, PEOPLE[key][0].capitalize())


def world(rng, **kw):
    now, w = setup(rng, **kw)
    for k, (_, name, ph, em, _) in PEOPLE.items():
        w.contacts[name] = (ph, em)
    w.contacts["Grandma Ruth"] = ("(503) 555-0191", "")
    return now, w


_FIRST: dict = {}
_CAP = [7]


def _fw(text):
    words = text.replace("“", "").replace('"', "").split()
    return words[0].strip(",.:;!?'").lower() if words else ""


def fin(rng, *opts):
    ok = [o for o in opts if _FIRST.get(_fw(o), 0) < _CAP[0]]
    o = rng.choice(ok or list(opts))
    _FIRST[_fw(o)] = _FIRST.get(_fw(o), 0) + 1
    return o


def cap1(s):
    return s[:1].upper() + s[1:]


# ----------------------------------------------------------------------------- sources
class Src:
    """What one gather step reads, plus the words each part of the turn needs."""

    def __init__(self, calls, text, lines, subject, short, obj, clause, lead=""):
        self.calls = calls          # reads, issued together in one step
        self.text = text            # first-person sentence(s) for a text
        self.lines = lines          # first-person facts for an email (bullets when long)
        self.subject = subject      # email subject
        self.short = short          # noun phrase for the final ("tomorrow's forecast, overcast, high of 54°")
        self.obj = obj              # "tomorrow's forecast" (prompt noun phrase)
        self.clause = clause        # "what tomorrow's weather will be" (prompt clause)
        self.lead = lead


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _an(n):
    return "an" if str(n)[:1] == "8" or n in (11, 18) else "a"


def _absdate(d):
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}"


def src_weather(rng, w, now, which, at=None):
    wx = w.weather["Portland,OR"]
    d = wx["days"][which]
    desc, hi, lo, rain = d[3].lower(), d[1], d[2], d[4]
    day = "Today" if which == 0 else "Tomorrow"
    rain_t = f", with {_an(rain)} {rain}% chance of rain" if rain else ", and it should stay dry"
    if which == 0:
        text = f"It's {wx['now_f']}° and {wx['desc'].lower()} in Portland right now, with a high of {hi}° today{rain_t}."
        short = f"today's weather, {wx['now_f']}° and {wx['desc'].lower()}"
    else:
        text = f"Tomorrow in Portland the forecast is {desc}, with a high of {hi}° and a low of {lo}°{rain_t}."
        short = f"tomorrow's forecast, {desc} with a high of {hi}°"
        if at is not None:   # scheduled: name the day, it may be the delivery day itself
            fd = _absdate(now + D(days=1))
            text = f"The forecast for Portland on {fd} is {desc}, with a high of {hi}° and a low of {lo}°{rain_t}."
            day = f"Forecast for {fd}"
    obj = "tomorrow's forecast" if which else "today's weather"
    clause = "what the weather will do tomorrow" if which else "how the weather is today"
    return Src([Call("get_weather", location="Portland,OR")], text, text, day if at is not None and which else f"{day}'s forecast", short, obj, clause, day)


def src_calendar(rng, w, now, period, at=None):
    rows, _, _ = w.upcoming_rows(period=period, calendar_only=True)
    rows = sorted(rows, key=lambda r: r["when_ts"])
    if at is not None:   # scheduled: nothing that has passed by delivery time
        rows = [r for r in rows if ts(r) >= at]
    if not (1 <= len(rows) <= 9):
        return None
    multi = period in ("this week", "next week")
    items = [f"{r['title']} {('on ' + ts(r).strftime('%A') + ' ') if multi else ''}at {clock(ts(r))}" for r in rows]
    lead = {"today": "Today I have", "tomorrow": "Tomorrow I have", "this week": "This week I have",
            "next week": "Next week I have"}.get(period, f"On {period.capitalize()} I have")
    if at is not None and period == "tomorrow":
        lead = f"On {_absdate(now + D(days=1))} I have"
    text = f"{lead} {join(items)}."
    if len(items) >= 3:
        lines = f"{lead}:\n" + "\n".join(f"- {it}" for it in items)
    else:
        lines = text
    label = period if period in ("today", "tomorrow", "this week", "next week") else period.capitalize()
    short = f"your schedule for {label}, {len(rows)} event{'s' if len(rows) != 1 else ''}"
    obj = {"today": "what i have on today", "tomorrow": "my plans for tomorrow", "this week": "my plans for the week",
           "next week": "what's on for next week"}.get(period, f"my {period} lineup")
    clause = {"today": "what my day looks like", "tomorrow": "what i've got going on tomorrow",
              "this week": "what my week looks like", "next week": "what's coming up next week"}.get(
        period, f"what i have on {period}")
    subj = {"today": "My day", "tomorrow": "My schedule for tomorrow", "this week": "My week"}.get(period, f"My {period.capitalize()}")
    if at is not None and period == "tomorrow":
        subj = f"My schedule for {_absdate(now + D(days=1))}"
    return Src([Call("get_upcoming", period=period, calendar_only=True)], text, lines, subj,
               short, obj, clause, lead)


TICKS = {"CRM": ("Salesforce, Inc.", "Salesforce", 268.40, 266.15), "MSFT": ("Microsoft Corporation", "Microsoft", 468.27, 471.05),
         "COST": ("Costco Wholesale Corporation", "Costco", 912.38, 905.60), "TSLA": ("Tesla, Inc.", "Tesla", 288.15, 283.90),
         "NKE": ("NIKE, Inc.", "Nike", 76.42, 77.18), "SBUX": ("Starbucks Corporation", "Starbucks", 84.27, 83.50),
         "ADBE": ("Adobe Inc.", "Adobe", 462.10, 458.90), "AMD": ("Advanced Micro Devices, Inc.", "AMD", 158.72, 156.30),
         "INTC": ("Intel Corporation", "Intel", 24.18, 24.60), "DIS": ("The Walt Disney Company", "Disney", 112.34, 111.02)}


def stocks_world(w, rng, now, syms):
    quotes = {}
    for s in syms:
        nm, _, price, prev = TICKS[s]
        price = round(price + rng.uniform(-3, 3), 2)
        quotes[s] = (price, round(price - prev, 2), nm, prev)

    def handler(args):
        want = args.get("symbols") or []
        want = [want] if isinstance(want, str) else want
        blocks = []
        for sym in want:
            s = str(sym).upper()
            s = s if s in quotes else next((k for k, v in TICKS.items() if v[1].lower() == str(sym).lower()), s)
            if s not in quotes:
                blocks.append(f"{sym}: no quote found.")
                continue
            price, chg, nm, prev = quotes[s]
            pct = round(chg / prev * 100, 2)
            sign = "+" if chg >= 0 else "-"
            stamp = (now - D(minutes=4)).strftime("%Y-%m-%d %H:%M")
            blocks.append(f"{s} ({nm}):\n  Quote: {price:.2f} USD (as of {stamp} EST; intraday regular-session observation).\n"
                          f"  Previous official close: {prev:.2f} USD on {(now - D(days=1)).strftime('%Y-%m-%d')}.\n"
                          f"  Change from previous official close: {sign}{abs(chg):.2f} USD ({sign}{abs(pct):.2f}%).")
        return "\n\n".join(blocks)
    w.overrides["get_stock_price"] = handler
    return quotes


def _move(chg):
    return f"up ${chg:.2f}" if chg >= 0 else f"down ${abs(chg):.2f}"


def src_stock(rng, w, now, syms, at=None):
    q = stocks_world(w, rng, now, syms)
    names = [TICKS[s][1] for s in syms]
    parts = [f"{TICKS[s][1]} is at ${q[s][0]:,.2f}, {_move(q[s][1])} on the day" for s in syms]
    text = "; ".join(parts) + "." if len(parts) > 1 else parts[0] + "."
    text = text.replace("; ", ", and ") if len(parts) == 2 else text
    if at is not None:   # scheduled: a quote is only true as of the time it was read
        stamp = f"{clock(now - D(minutes=4))} on {now.strftime('%A, %B')} {now.day}"
        parts = [f"{TICKS[s][1]} was at ${q[s][0]:,.2f}, {_move(q[s][1])} from the previous close" for s in syms]
        text = f"As of {stamp}, " + (parts[0] if len(parts) == 1 else f"{parts[0]}, and {parts[1]}") + "."
    short = " and ".join(f"{TICKS[s][1]} at ${q[s][0]:,.2f}" for s in syms)
    nm = join(names)
    lines = text if len(parts) == 1 else (f"As of {stamp}:\n" if at is not None else "") + "\n".join(f"- {p}" for p in parts)
    return Src([Call("get_stock_price", symbols=list(syms))], text, lines,
               f"{nm} stock price" + ("s" if len(syms) > 1 else ""), short,
               f"the {nm} share price" if len(syms) == 1 else f"the {join(names)} prices",
               f"where {nm} is trading" if len(syms) == 1 else f"how {nm} are doing today")


NOTES = [
    ("Potluck plan", "Saturday 6 PM at Carmen's place. Bring pasta salad and paper plates. Park behind the building.",
     "The potluck is Saturday at 6 PM at Carmen's place. Bring pasta salad and paper plates, and park behind the building.",
     "the potluck plan", "potluck", ["carmen", "maya", "theo"]),
    ("Trip packing", "Leave Friday 7 AM. Tent, sleeping bag, camp stove, two headlamps, snacks.",
     "The plan is to leave Friday at 7 AM, and the packing list is the tent, sleeping bag, camp stove, two headlamps and snacks.",
     "the trip packing list", "packing", ["dad", "theo", "nadia"]),
    ("Study group", "Wednesday 4 PM, Hollis Library room 3. Chapters 6 to 8. Bring the practice exam.",
     "Study group is Wednesday at 4 PM in Hollis Library room 3. It covers chapters 6 to 8, and the practice exam should come along.",
     "the study group details", "study group", ["maya", "theo", "nadia", "priya"]),
    ("Grocery list", "Eggs, oat milk, spinach, rice, bananas, dish soap.",
     "The grocery list is eggs, oat milk, spinach, rice, bananas and dish soap.",
     "the grocery list", "grocery", ["maya", "mom"]),
    ("Lasagna recipe", "Layer noodles, ricotta and sauce. Bake at 375 for 45 minutes, then rest 10 minutes.",
     "For the lasagna, layer the noodles, ricotta and sauce, bake at 375 for 45 minutes, then rest it for 10 minutes.",
     "the lasagna recipe", "lasagna", ["mom", "dad", "priya"]),
    ("Move-out checklist", "Return keys by Aug 30. Clean the oven. Photograph the walls. Forward the mail.",
     "The move-out list is to return the keys by Aug 30, clean the oven, photograph the walls and forward the mail.",
     "the move-out checklist", "move-out", ["dad", "mom", "maya"]),
    ("Book club picks", "November: Piranesi. December: Station Eleven. Meets the first Sunday at 2 PM.",
     "Book club has Piranesi in November and Station Eleven in December. Book club meets the first Sunday at 2 PM.",
     "the book club picks", "book club", ["priya", "mom", "carmen"]),
]


def src_note(rng, w, now, idx):
    title, body, gist, short, key, _ = NOTES[idx]
    w.notes.append(Note(title, body, now - D(days=rng.randint(2, 20), hours=rng.randint(0, 5))))
    return Src([Call("search_notes", query=key)], gist, gist, title, short,
               f"what's in my {key} note", f"what my {key} note says")


MAILS = [
    ("Prof. Okafor", "aokafor@linden.edu", "Office hours moved", "Hi Sam, office hours this week move to Thursday at 3 PM in Hollis 214. Same sign-up sheet.",
     "Professor Okafor moved office hours to Thursday at 3 PM in Hollis 214.", "the office hours email", "office hours", "what Professor Okafor said about office hours"),
    ("Cedar Row Property", "office@cedarrow.example", "Water shutoff Tuesday", "Water will be off Tuesday from 9 AM to noon for pipe repairs in Building C.",
     "The water is off Tuesday from 9 AM to noon for pipe repairs.", "the water shutoff notice", "shutoff", "what the landlord emailed about the water"),
    ("Linden Registrar", "registrar@linden.edu", "Spring registration opens", "Registration opens November 16 at 8 AM for sophomores. Holds must be cleared first.",
     "Spring registration opens November 16 at 8 AM for sophomores, once any holds are cleared.", "the registration notice", "registration", "what the registrar sent about spring registration"),
    ("Hex Air", "bookings@hexair.example", "Your flight to Denver", "Flight HX 218 departs Dec 19 at 7:05 AM from gate B12. Check in opens 24 hours before.",
     "My flight to Denver is HX 218 on Dec 19, leaving at 7:05 AM from gate B12.", "the flight details", "Denver", "my Denver flight details"),
    ("Linden Housing", "housing@linden.edu", "Parking permit renewal", "Parking permits renew by Friday for $48. Pay at the housing portal.",
     "Parking permits renew by Friday, and it's $48 through the housing portal.", "the parking permit email", "permit", "what housing said about the parking permit"),
]


def src_mail(rng, w, now, idx):
    sender, addr, subj, body, gist, short, key, clause = MAILS[idx]
    w.emails.insert(0, Email(sender, addr, subj, now - D(hours=rng.randint(3, 40)), body, True, True))
    subj_out = subj[5:] if subj.startswith("Your ") else subj
    return Src([Call("view_emails", query=key, count=1)], gist, gist, subj_out, "a summary of " + short, short, clause)


def src_todos(rng, w, now):
    due = sorted([r for r in w.reminders if r.due and not r.done], key=lambda r: r.due)
    und = [r for r in w.reminders if r.due is None and not r.done]
    titles = [r.title for r in (due + und)]
    text = f"My open to-dos are {join(titles)}."
    lines = "Open to-dos:\n" + "\n".join(f"- {t}" for t in titles)
    return Src([Call("search_reminders", query="", scope="all")], text, lines, "My open to-dos",
               f"your {len(titles)} open to-dos", "my open to-dos", "what's still on my to-do list")


PRICE_WEB = [
    ("bitcoin price usd", [("Bitcoin price today", "https://www.coinwatch.example/bitcoin",
                            "Bitcoin (BTC) is trading near $71,950, up 1.2% over the last 24 hours."),
                           ("BTC/USD chart", "https://www.cryptoboard.example/btc-usd", "The 24-hour range is $70,610 to $72,240.")],
     "Bitcoin is around $71,950 right now, up about 1.2% over the last 24 hours.", "the bitcoin price, about $71,950", "the bitcoin price",
     "where bitcoin is right now"),
    ("ethereum price usd", [("Ethereum price live", "https://www.coinwatch.example/ethereum",
                             "Ethereum (ETH) is trading near $3,120, down 0.8% over the last 24 hours.")],
     "Ethereum is near $3,120 right now, down about 0.8% over the last 24 hours.", "the ethereum price, about $3,120", "the ethereum price",
     "what ethereum is trading at"),
    ("gold price per ounce today", [("Gold price today", "https://www.metalsdesk.example/gold",
                                     "Spot gold is trading near $2,418 per troy ounce today, up 0.6% on the day.")],
     "Gold is near $2,418 an ounce today, up about 0.6%.", "the gold price, about $2,418 an ounce", "the price of gold",
     "what gold is going for"),
    ("gas prices portland oregon", [("Portland gas prices", "https://www.fuelboard.example/portland",
                                     "The average price for regular in Portland is $4.19 a gallon, down 3 cents from last week.")],
     "Regular gas in Portland averages $4.19 a gallon, down 3 cents from last week.", "the Portland gas price, $4.19 a gallon",
     "the average gas price in portland", "what gas costs around portland"),
]


def src_web(rng, w, now, idx, at=None):
    q, hits, gist, short, obj, clause = PRICE_WEB[idx]
    w.web[q] = hits
    if at is not None:   # scheduled: say when the number was read, not "right now"
        stamp = f"{clock(now)} on {now.strftime('%A, %B')} {now.day}"
        gist = gist.replace(" right now,", ",").replace(" right now.", ".")
        gist = gist.replace(" is around ", " was around ").replace(" is near ", " was near ").replace(" averages ", " averaged ")
        gist = gist.replace(" is trading", " was trading")
        gist = f"As of {stamp}, " + gist[0].lower() + gist[1:]
    return Src([Call("web_search", query=q)], gist, gist, cap1(obj), short, obj, clause)


def src_brief(rng, w, now):
    rows, _, _ = w.upcoming_rows(period="today", calendar_only=True)
    rows = sorted([r for r in rows if ts(r) >= now], key=lambda r: r["when_ts"])
    if not (1 <= len(rows) <= 6):
        return None
    items = [f"{r['title']} at {clock(ts(r))}" for r in rows]
    n_rem = len([r for r in w.reminders if not r.done])
    text = f"Still ahead today: {join(items)}. I also have {n_rem} open to-dos."
    lines = "Still ahead today:\n" + "\n".join(f"- {i}" for i in items) + f"\nAnd {n_rem} open to-dos."
    return Src([Call("daily_brief")], text, lines, "My day so far", f"the rest of your day, {len(rows)} event{'s' if len(rows) != 1 else ''} and {n_rem} to-dos",
               "a quick rundown of my day", "how the rest of my day looks")


def src_digest(rng, w, now):
    s, e, _ = w._mail_window("today")
    rows = [m for m in w.emails if s <= m.when < e and m.when <= now]
    people = [m for m in rows if m.person]
    if len(rows) < 2 or not people:
        return None
    if len(rows) > 6:
        return None
    each = [f"{m.sender} about {m.subject}" if m.person else m.sender for m in rows]
    text = f"I've had {len(rows)} emails today: {join(each)}."
    lines = f"Emails today ({len(rows)}):\n" + "\n".join(f"- {e}" for e in each)
    return Src([Call("summarize_emails", day="today")], text, lines, "Inbox today", f"today's inbox, {len(rows)} emails",
               "a rundown of my inbox today", "what's come into my inbox today")


# ----------------------------------------------------------------------------- phrasing
PREFIX = ["", "", "", "", "hey, ", "okay so ", "um ", "quick one: ", "wisp, ", "pls "]


def styled(rng, p):
    p = rng.choice(PREFIX) + p
    return p[0].lower() + p[1:] if rng.random() < 0.8 else p


def ask_text(rng, who, src, i):
    w = PEOPLE[who][0]
    return [f"text {w} {src.clause}", f"shoot {w} a text with {src.obj}", f"message {w} {src.obj}",
            f"let {w} know {src.clause} by text", f"send {w} {src.obj} in a text",
            f"drop {w} a quick text about {src.obj}", f"get {src.obj} over to {w} as a text",
            f"i want {w} to have {src.obj}, text it to them"][i % 8]


def ask_email(rng, who, src, i):
    w = PEOPLE[who][0]
    return [f"email {w} {src.clause}", f"shoot {w} an email with {src.obj}", f"send {w} an email with {src.obj}",
            f"mail {w} {src.obj}", f"get {src.obj} to {w} over email",
            f"i need {w} to have {src.obj}, email it", f"drop {w} an email covering {src.obj}",
            f"send an email to {w} about {src.clause}"][i % 8]


def ask_draft(rng, who, src, i):
    w = PEOPLE[who][0]
    return [f"i'll send it myself, but prep a draft email to {w} with {src.obj}",
            f"put {src.obj} in a draft email for {w}, don't send it",
            f"make me a draft email for {w} about {src.clause}, no sending",
            f"leave me a draft to {w} with {src.obj}",
            f"can you prep an email for {w} with {src.obj}? just a draft",
            f"set up a draft to {w} with {src.obj}, i'm going to read it before it goes",
            f"make a draft email for {w} about {src.clause}, just save it",
            f"save a draft email for {w} with {src.obj}, hold off on sending"][i % 8]


def ask_self(rng, src, i):
    return [f"email myself {src.obj}", f"mail me {src.obj}", f"send {src.obj} to my own email",
            f"i want {src.obj} in my inbox, email it to me", f"email me {src.clause}",
            f"put {src.obj} in an email to myself"][i % 6]


def for_who(src, who):
    """Address the recipient directly: the host of the potluck is 'your place' to the host."""
    if who == "carmen":
        for f in ("text", "lines"):
            setattr(src, f, getattr(src, f).replace("Carmen's place", "your place"))
    return src


def greet(who):
    return f"Hi {PEOPLE[who][4]},"


_LOWER_OK = {"As", "Tomorrow", "Today", "This", "Next", "On", "It's", "The", "Still", "We", "For", "My", "I've", "Study", "Spring",
             "Regular", "Gold", "Library", "Flight", "Parking", "Book", "Professor", "Registration"}


def _lower_first(t):
    first = t.split(" ", 1)[0]
    return t[:1].lower() + t[1:] if first in _LOWER_OK and first not in ("Professor", "Registration") else t


def text_body(who, src):
    return f"{greet(who)} {_lower_first(src.text)}"


def email_body(who, src):
    return f"{greet(who)}\n\n{src.lines}\n\nSam"


def _sc(prompt, now, w, steps, notes, *, cards=(), approve=None):
    s = Scenario(FAMILY, prompt, now, w, steps, notes)
    s.expect_cards = list(cards)
    if approve is not None:
        s.approve = list(approve)
    return s


def pretty_when(when, now):
    d, _ = resolve_when(when, now=now)
    delta = (d.date() - now.date()).days
    day = "tonight" if (delta == 0 and d.hour >= 18) else "today" if delta == 0 else "tomorrow" if delta == 1 else d.strftime("%A")
    return f"{day} at {clock(d)}"


# final wordings -------------------------------------------------------------
def final_text(rng, who, src):
    n = PEOPLE[who][4]
    return fin(rng, f"I texted {n} {src.short}.", f"{n} now has {src.short}, by text.", f"Your text to {n} went out with {src.short}.",
               f"Texted {n} {src.short}.", f"Sent: a text to {n} with {src.short}.",
               f"The text to {n} is on its way with {src.short}.")


def final_email(rng, who, src):
    n = PEOPLE[who][4]
    return fin(rng, f"I emailed {n} {src.short}.", f"{n} has your email with {src.short}.", f"Your email to {n} went out with {src.short}.",
               f"Emailed {n} {src.short}.", f"Sent: an email to {n} with {src.short}.")


def final_draft(rng, who, src):
    n = PEOPLE[who][4]
    return fin(rng, f"The draft to {n} is in Mail with {src.short}. Nothing has been sent.",
               f"I drafted it to {n} in Mail, with {src.short}, and left it unsent.",
               f"Your draft to {n} is open in Mail, unsent, with {src.short}.",
               f"Mail has your draft to {n} with {src.short}. It hasn't been sent.",
               f"A draft for {n} with {src.short} is waiting in Mail, unsent.")


def final_self(rng, src):
    return fin(rng, f"I emailed you {src.short}.", f"Emailed you {src.short}.",
               f"Your inbox has {src.short} now.", f"Sent: an email to you with {src.short}.")


def final_sched(rng, who, src, pretty, kind):
    n = PEOPLE[who][4] if who else "you"
    thing = "text" if kind == "message" else "email"
    return fin(rng, f"Scheduled: your {thing} to {n} goes out {pretty}.", f"Your {thing} to {n} is set for {pretty}.",
               f"The {thing} to {n}, with {src.short}, is scheduled for {pretty}.",
               f"The {thing} to {n} is on the schedule for {pretty}.")


# ============================================================================ family
def fam_compose_r3(rng):
    out = []
    _FIRST.clear()
    n = [0]

    def nxt():
        n[0] += 1
        return n[0]

    def pick_src(kind, w, now, i, at=None):
        """A Src of the named kind, or None when the world has no data for it."""
        if kind == "wx_t":
            return src_weather(rng, w, now, 1, at)
        if kind == "wx_0":
            return src_weather(rng, w, now, 0)
        if kind.startswith("cal_"):
            p = kind[4:].replace("_", " ")
            if p == "wd":
                p = WEEKDAYS[(now + D(days=2)).weekday()]
            return src_calendar(rng, w, now, p, at)
        if kind == "stock1":
            sym = rng.choice(["COST", "TSLA", "NKE", "SBUX", "ADBE", "INTC", "DIS", "MSFT"])
            return src_stock(rng, w, now, [sym], at)
        if kind == "stock2":
            a, b = rng.sample(["CRM", "MSFT", "AMD", "COST", "TSLA", "DIS"], 2)
            return src_stock(rng, w, now, [a, b], at)
        if kind.startswith("note"):
            return src_note(rng, w, now, int(kind[4:]))
        if kind.startswith("mail"):
            return src_mail(rng, w, now, int(kind[4:]))
        if kind == "todos":
            return src_todos(rng, w, now)
        if kind.startswith("web"):
            return src_web(rng, w, now, int(kind[3:]), at)
        if kind == "brief":
            return src_brief(rng, w, now)
        if kind == "digest":
            return src_digest(rng, w, now)
        raise KeyError(kind)

    def mk(kind, hour=None, weekday=None, tries=40, when=None, who=None):
        """(now, world, src) for a source kind, resampling until the world has data."""
        for _ in range(tries):
            kw = {}
            if hour is not None:
                kw["hour"] = hour(rng) if callable(hour) else hour
            if weekday is not None:
                kw["weekday"] = weekday(rng) if callable(weekday) else weekday
            now, w = world(rng, **kw)
            at = resolve_when(when, now=now)[0] if when else None
            src = pick_src(kind, w, now, 0, at)
            if src is not None:
                return now, w, for_who(src, who)
        raise RuntimeError(f"no world for source {kind}")

    wk = lambda r: r.choice([0, 1, 2, 3])
    hrs = lambda r: r.randint(8, 16)
    DEN = lambda i: i % 7 == 3          # about one send in seven is declined

    # ------------------------------------------------------------ 1. texts: gather, then send_message
    text_plan = [("wx_t", "carmen"), ("wx_0", "dad"), ("wx_t", "maya"), ("cal_tomorrow", "mom"), ("cal_tomorrow", "theo"),
                 ("cal_today", "maya"), ("cal_this_week", "dad"), ("cal_wd", "nadia"), ("cal_tomorrow", "priya"),
                 ("cal_this_week", "mom"), ("stock1", "theo"), ("stock1", "dad"), ("stock2", "nadia"), ("stock2", "maya"),
                 ("note0", "carmen"), ("note1", "dad"), ("note2", "priya"), ("note3", "maya"), ("note4", "mom"), ("note6", "carmen"),
                 ("mail0", "maya"), ("mail1", "theo"), ("mail3", "mom"), ("mail4", "nadia"), ("todos", "maya"), ("todos", "mom"),
                 ("web0", "theo"), ("web1", "nadia"), ("web2", "dad"), ("web3", "mom"), ("brief", "dad"), ("brief", "maya")]
    for i, (kind, who) in enumerate(text_plan):
        now, w, src = mk(kind, hour=hrs, weekday=wk, who=who)
        den = DEN(i)
        steps = [src.calls, [Call("send_message", to=to_name(who), text=text_body(who, src))]]
        if not den:
            steps.append(Final(final_text(rng, who, src)))
        out.append(_sc(styled(rng, ask_text(rng, who, src, nxt())), now, w, steps,
                       "text request: gather the content, then send_message to the contact, body in first person with the facts"
                       + ("; the card is declined, the turn ends at the call" if den else ""),
                       cards=["send_message"], approve=[not den]))

    # ------------------------------------------------------------ 2. emails: gather, then send_email
    email_plan = [("cal_tomorrow", "dad"), ("cal_this_week", "dad"), ("cal_today", "okafor"), ("cal_wd", "maya"), ("cal_tomorrow", "carmen"),
                  ("wx_t", "nadia"), ("wx_0", "priya"), ("wx_t", "dad"), ("stock1", "okafor"), ("stock2", "dad"), ("stock1", "priya"),
                  ("note1", "theo"), ("note2", "okafor"), ("note5", "dad"), ("note3", "carmen"), ("note6", "priya"),
                  ("mail0", "maya"), ("mail2", "nadia"), ("mail1", "dad"), ("mail3", "carmen"), ("mail4", "theo"),
                  ("todos", "theo"), ("todos", "carmen"), ("web0", "dad"), ("web2", "maya"), ("web1", "priya"),
                  ("brief", "dad"), ("brief", "theo"), ("digest", "dad"), ("digest", "maya")]
    for i, (kind, who) in enumerate(email_plan):
        now, w, src = mk(kind, hour=hrs, weekday=wk, who=who)
        den = DEN(i + 2)
        steps = [src.calls, [Call("send_email", to=to_name(who), subject=src.subject, body=email_body(who, src))]]
        if not den:
            steps.append(Final(final_email(rng, who, src)))
        out.append(_sc(styled(rng, ask_email(rng, who, src, nxt())), now, w, steps,
                       "email request: gather the content, then send_email (not a draft) to the contact's email"
                       + ("; the card is declined, the turn ends at the call" if den else ""),
                       cards=["send_email"], approve=[not den]))

    # ------------------------------------------------------------ 3. draft only: draft_email, never a send
    draft_plan = [("cal_tomorrow", "dad"), ("cal_this_week", "okafor"), ("cal_wd", "maya"), ("wx_t", "carmen"), ("stock1", "dad"),
                  ("stock2", "theo"), ("note0", "maya"), ("note2", "okafor"), ("note5", "dad"), ("mail0", "theo"), ("mail2", "priya"),
                  ("mail3", "nadia"), ("todos", "maya"), ("web0", "nadia"), ("web2", "carmen"), ("cal_this_week", "nadia"), ("digest", "dad"),
                  ("cal_today", "carmen"), ("wx_0", "dad"), ("note4", "priya")]
    for i, (kind, who) in enumerate(draft_plan):
        now, w, src = mk(kind, hour=hrs, weekday=wk, who=who)
        out.append(_sc(styled(rng, ask_draft(rng, who, src, nxt())), now, w,
                       [src.calls, [Call("draft_email", to=to_name(who), subject=src.subject, body=email_body(who, src))],
                        Final(final_draft(rng, who, src))],
                       "draft request: gather, then draft_email only; nothing is sent, no card", cards=[]))

    # ------------------------------------------------------------ 4. schedule_send: only with an explicit later time
    # (kind, who, channel, when)
    sched_plan = [("wx_t", "mom", "message", "tomorrow at 7am"), ("cal_tomorrow", "dad", "message", "tonight at 9pm"),
                  ("cal_tomorrow", "maya", "message", "tonight at 8:30pm"), ("stock1", "theo", "message", "tomorrow at 9am"),
                  ("note2", "priya", "message", "tomorrow at 8am"), ("todos", "maya", "message", "tonight at 9pm"),
                  ("cal_tomorrow", "dad", "email", "tonight at 9pm"), ("cal_tomorrow", "okafor", "email", "tomorrow at 7am"),
                  ("wx_t", "nadia", "email", "tonight at 9pm"), ("mail0", "carmen", "email", "tomorrow at 8am"),
                  ("note3", "maya", "email", "friday at 5pm"), ("web1", "theo", "message", "tomorrow at 10am"),
                  ("stock2", "dad", "email", "monday at 8am"), ("mail1", "priya", "message", "tonight at 9pm")]
    for i, (kind, who, chan, when) in enumerate(sched_plan):
        night = when.startswith("tonight")
        now, w, src = mk(kind, hour=(lambda r: r.randint(8, 16)) if night else hrs, weekday=wk, when=when, who=who)
        pw = pretty_when(when, now)
        den = (i == 5)
        if chan == "message":
            call = Call("schedule_send", channel="message", to=to_name(who), when=when, body=text_body(who, src))
            p = [f"at {when.replace('tonight at ', '').replace('tomorrow at ', '')} {'tonight' if night else 'tomorrow' if 'tomorrow' in when else ''}",
                 ]
            ask = [f"{when} text {PEOPLE[who][0]} {src.clause}", f"set up a text for {PEOPLE[who][0]} {when} with {src.obj}",
                   f"schedule a text to {PEOPLE[who][0]} for {when}: {src.obj}",
                   f"have {PEOPLE[who][0]} get {src.obj} by text {when}"][i % 4]
        else:
            call = Call("schedule_send", channel="email", to=to_name(who), when=when, subject=src.subject, body=email_body(who, src))
            ask = [f"{when} email {PEOPLE[who][0]} {src.clause}", f"schedule an email to {PEOPLE[who][0]} for {when} with {src.obj}",
                   f"queue an email for {PEOPLE[who][0]} {when} covering {src.obj}",
                   f"email {PEOPLE[who][0]} {src.obj} {when}, hold it until then"][i % 4]
        steps = [src.calls, [call]]
        if not den:
            steps.append(Final(final_sched(rng, who, src, pw, chan)))
        out.append(_sc(styled(rng, ask), now, w, steps,
                       "an explicit later time: gather, then schedule_send behind a card"
                       + ("; declined, the turn ends at the call" if den else ""),
                       cards=["schedule_send"], approve=[not den]))

    # ------------------------------------------------------------ 5. email to myself: send_email, confirmed_self_send
    self_plan = [("cal_tomorrow", False), ("cal_today", False), ("wx_t", False), ("stock1", False), ("todos", False), ("note3", False),
                 ("note6", False), ("mail4", False), ("web0", False), ("cal_this_week", True), ("cal_tomorrow", True), ("wx_t", True)]
    for i, (kind, later) in enumerate(self_plan):
        when = rng.choice(["tonight at 9pm", "tomorrow at 6:30am", "tonight at 8pm"]) if later else None
        now, w, src = mk(kind, hour=(lambda r: r.randint(8, 15)), weekday=wk, when=when)
        body = src.lines
        den = (i == 7)
        if later:
            pw = pretty_when(when, now)
            ask = [f"{when} email me {src.obj}", f"queue an email to my own address for {when}: {src.obj}",
                   f"remind me by email {when}: {src.obj}"][i % 3]
            ask = ask.replace("remind me by email", "email me")
            steps = [src.calls, [Call("schedule_send", channel="email", to=OWN, when=when, subject=src.subject, body=body)],
                     Final(fin(rng, f"Scheduled: {src.short} reaches your inbox {pw}.", f"Set for {pw}: an email to you with {src.short}.",
                               f"Your email with {src.short} is scheduled for {pw}."))]
            out.append(_sc(styled(rng, ask), now, w, steps, "self email at a named time: gather, then schedule_send to the user's own address",
                           cards=["schedule_send"], approve=[True]))
        else:
            steps = [src.calls, [Call("send_email", to=OWN, subject=src.subject, body=body, confirmed_self_send=True)]]
            if not den:
                steps.append(Final(final_self(rng, src)))
            out.append(_sc(styled(rng, ask_self(rng, src, nxt())), now, w, steps,
                           "email myself: gather, then send_email to the user's own address with confirmed_self_send=true"
                           + ("; declined, the turn ends at the call" if den else ""),
                           cards=["send_email"], approve=[not den]))

    # ------------------------------------------------------------ 6. several sources in one report (reads together, one send)
    multi_plan = [(["cal_tomorrow", "wx_t"], "mom", "message"), (["cal_today", "stock1"], "dad", "message"),
                  (["wx_t", "cal_tomorrow"], "theo", "email"), (["cal_this_week", "todos"], "dad", "email"),
                  (["cal_tomorrow", "wx_t", "stock2"], "maya", "email"), (["cal_today", "digest"], "dad", "email"),
                  (["wx_0", "stock2"], "nadia", "message"), (["cal_tomorrow", "mail0"], "carmen", "message"),
                  (["cal_this_week", "wx_t", "todos"], "okafor", "email"), (["stock1", "web0"], "theo", "message")]
    for i, (kinds, who, chan) in enumerate(multi_plan):
        for _ in range(40):
            now, w = world(rng, hour=hrs(rng), weekday=wk(rng))
            srcs = [pick_src(k, w, now, 0) for k in kinds]
            if all(srcs):
                break
        if not all(srcs):   # no world held every source for this seed: skip the plan, never build a broken turn
            continue
        calls = [c for s in srcs for c in s.calls]
        text = " ".join(s.text for s in srcs)
        short = join([s.short for s in srcs])
        big = Src(calls, text, "\n\n".join(s.lines for s in srcs), "Update: " + " and ".join(s.subject.lower() for s in srcs[:2]) if False else "My update",
                  short, join([s.obj for s in srcs]), join([s.clause for s in srcs]))
        den = (i == 4)
        if chan == "message":
            asked = f"text {PEOPLE[who][0]} " + join([s.obj for s in srcs])
            asked = [asked, f"message {PEOPLE[who][0]} an update with " + join([s.obj for s in srcs])][i % 2]
            write = Call("send_message", to=to_name(who), text=text_body(who, big))
            fin_ = final_text(rng, who, big)
            card = "send_message"
        else:
            asked = f"email {PEOPLE[who][0]} a rundown with " + join([s.obj for s in srcs])
            asked = [asked, f"send {PEOPLE[who][0]} an email covering " + join([s.obj for s in srcs])][i % 2]
            write = Call("send_email", to=to_name(who), subject="My update", body=email_body(who, big))
            fin_ = final_email(rng, who, big)
            card = "send_email"
        steps = [calls, [write]] + ([] if den else [Final(fin_)])
        out.append(_sc(styled(rng, asked), now, w, steps,
                       "several sources: every read in one step, then one send with all the facts"
                       + ("; declined, the turn ends at the call" if den else ""),
                       cards=[card], approve=[not den]))

    # ------------------------------------------------------------ 7. held tickers come from memory, then prices, then the text
    # The saved memory names the holdings in the words recall is queried with, so recall returns them.
    for i, (who, syms, chan) in enumerate([("dad", ["AMD", "CRM"], "message"), ("mom", ["MSFT", "COST"], "message"),
                                           ("theo", ["TSLA", "DIS"], "message"), ("maya", ["AMD", "MSFT"], "email")]):
        now, w = world(rng, weekday=rng.choice([1, 2, 3]), hour=rng.randint(10, 14))
        q = stocks_world(w, rng, now, syms)
        hold = [TICKS[x][1] if TICKS[x][1] == x else f"{TICKS[x][1]} ({x})" for x in syms]
        w.memories = [f"Sam's stock holdings: {hold[0]} and {hold[1]} shares.",
                      "Sam's home city: Portland, Oregon."]
        parts = [f"{TICKS[s][1]} is at ${q[s][0]:,.2f}, {_move(q[s][1])} on the day" for s in syms]
        txt = f"{parts[0]}, and {parts[1]}."
        src = Src([Call("get_stock_price", symbols=syms)], txt, "\n".join(f"- {p}" for p in parts), "My stocks today",
                  f"{TICKS[syms[0]][1]} at ${q[syms[0]][0]:,.2f} and {TICKS[syms[1]][1]} at ${q[syms[1]][0]:,.2f}", "my stock update", "how my stocks did")
        w_ = PEOPLE[who][0]
        if chan == "message":
            ask = [f"text {w_} how my stocks are doing", f"message {w_} a quick update on my shares",
                   f"give {w_} my holdings update by text"][i % 3]
            write = Call("send_message", to=to_name(who), text=text_body(who, src))
            f_ = final_text(rng, who, src)
            card = "send_message"
        else:
            ask = f"email {w_} a note on how my shares did today"
            write = Call("send_email", to=to_name(who), subject=src.subject, body=email_body(who, src))
            f_ = final_email(rng, who, src)
            card = "send_email"
        out.append(_sc(styled(rng, ask), now, w, [[Call("recall", query="stock holdings")], src.calls, [write], Final(f_)],
                       "no tickers named: recall the held tickers, read both prices, then send the right channel",
                       cards=[card], approve=[True]))

    # recall finds no holdings: ask one short question, never guess tickers, send nothing
    for i, (who, ask) in enumerate([("dad", "text dad how my stocks are doing"), ("mom", "message mom a quick update on my shares"),
                                    ("theo", "give theo my holdings update by text")]):
        now, w = world(rng, weekday=rng.choice([1, 2, 3]), hour=rng.randint(10, 14))
        w.memories = ["Sam's home city: Portland, Oregon."]
        reply = fin(rng, "I don't have your holdings saved. Which stocks should I include?",
                    "I can't find which stocks you hold. Which ones should I check for them?",
                    "Nothing saved says which shares you own. Which tickers do you want me to send?")
        out.append(_sc(styled(rng, ask), now, w, [[Call("recall", query="stock holdings")], Final(reply)],
                       "no tickers named and recall finds no holdings: ask one short question, no guessed tickers, no send", cards=[]))

    # ------------------------------------------------------------ 8. a literal body: no gather, still resolve the contact and use the right channel
    literal = [("nadia", "text", "text nadia saying i'm five minutes out", "I'm five minutes out."),
               ("priya", "text", "tell priya the library closes at 9 tonight", "The library closes at 9 tonight."),
               ("dad", "text", "text dad i landed safe", "I landed safe."),
               ("carmen", "text", "message carmen that dinner's on me saturday", "Dinner's on me Saturday."),
               ("mom", "text", "let mom know i got the package", "I got the package."),
               ("theo", "text", "tell theo to save me a seat", "Save me a seat?"),
               ("okafor", "email", "email prof okafor that i'll turn in the lab report friday", "I'll turn in the lab report on Friday."),
               ("dad", "email", "send dad an email saying thanks for the care package", "Thanks for the care package."),
               ("maya", "email", "email maya that the rent is due on the first", "The rent is due on the first."),
               ("priya", "email", "email priya that i can cover her shift thursday", "I can cover your shift on Thursday.")]
    for i, (who, chan, ask, line) in enumerate(literal):
        now, w = world(rng)
        den = i in (3, 8)
        if chan == "text":
            body = f"{greet(who)} {line}" if who in ("mom", "dad") else line
            steps = [[Call("send_message", to=to_name(who), text=body)]]
            card = "send_message"
            fin_ = fin(rng, f"I texted {PEOPLE[who][4]}.", f"{PEOPLE[who][4]} has your text.", f"Texted {PEOPLE[who][4]} for you.")
        else:
            body = f"{greet(who)}\n\n{line}\n\nSam"
            subj = {"okafor": "Lab report", "dad": "Thank you", "maya": "Rent", "priya": "Thursday shift"}[who]
            steps = [[Call("send_email", to=to_name(who), subject=subj, body=body)]]
            card = "send_email"
            fin_ = fin(rng, f"I emailed {PEOPLE[who][4]}.", f"{PEOPLE[who][4]} has your email.", f"Emailed {PEOPLE[who][4]} for you.")
        if not den:
            steps.append(Final(fin_))
        out.append(_sc(styled(rng, ask), now, w, steps,
                       "a literal body: no gather, send straight away on the named channel"
                       + ("; declined, the turn ends at the call" if den else ""), cards=[card], approve=[not den]))

    # ------------------------------------------------------------ 9. the lookup comes first when the address matters
    for i, (who, chan, kind) in enumerate([("dad", "email", "cal_tomorrow"), ("nadia", "email", "wx_t"), ("priya", "message", "cal_wd"),
                                           ("maya", "email", "stock1"), ("dad", "message", "wx_t")]):
        now, w, src = mk(kind, hour=hrs, weekday=wk)
        contact = PEOPLE[who][1]
        ph, em = PEOPLE[who][2], PEOPLE[who][3]
        w.overrides["lookup_contact"] = (lambda c, p, e: (lambda args: f"{c}: phone {p}" + (f"; email {e}" if e else "")))(contact, ph, em)
        look = [Call("lookup_contact", name=to_name(who))]
        if chan == "email":
            ask = ask_email(rng, who, src, i + 3)
            write = Call("send_email", to=em, subject=src.subject, body=email_body(who, src))
            f_ = final_email(rng, who, src)
            card = "send_email"
        else:
            ask = ask_text(rng, who, src, i + 3)
            write = Call("send_message", to=ph, text=text_body(who, src))
            f_ = final_text(rng, who, src)
            card = "send_message"
        out.append(_sc(styled(rng, ask), now, w, [look + src.calls, [write], Final(f_)],
                       "look up the contact together with the read, then send to the address on file",
                       cards=[card], approve=[True]))

    # ------------------------------------------------------------ 10. an email asked of someone with only a phone: say so, do not text instead
    for i, (who_name, prompt_who, kind) in enumerate([("Mom", "mom", "cal_tomorrow"), ("Grandma Ruth", "grandma ruth", "wx_t"),
                                                      ("Mom", "mom", "note4")]):
        now, w, src = mk(kind, hour=hrs, weekday=wk)
        ph = w.contacts[who_name][0]
        w.overrides["lookup_contact"] = (lambda c, p: (lambda args: f"{c}: phone {p}"))(who_name, ph)
        ask = [f"email {prompt_who} {src.clause}", f"send {prompt_who} an email with {src.obj}", f"mail {prompt_who} {src.obj}"][i]
        reply = fin(rng, f"{who_name} only has a phone number saved, so I can't email her. Want me to text it instead?",
                    f"I don't have an email address for {who_name}, only her number. Should I text it, or do you have her address?")
        out.append(_sc(styled(rng, ask), now, w, [[Call("lookup_contact", name=who_name)], Final(reply)],
                       "email asked of a contact with no email on file: say so and offer the text, no send", cards=[]))

    # ------------------------------------------------------------ 11. two contacts match the name: ask which, no send
    for i, (first, a, b, kind) in enumerate([("alex", "Alex Moreno", "Alex Brandt", "wx_t"), ("chris", "Chris Tan", "Chris Odum", "cal_tomorrow"),
                                              ("jordan", "Jordan Lee", "Jordan Pace", "stock1")]):
        now, w, src = mk(kind, hour=hrs, weekday=wk)
        w.overrides["lookup_contact"] = (lambda a_, b_: (lambda args: f"Several contacts match: {a_}, {b_}. Ask which one."))(a, b)
        ask = ask_text(rng, "mom", src, i).replace("mom", first)
        out.append(_sc(styled(rng, ask), now, w, [[Call("lookup_contact", name=first.capitalize())],
                                                  Final(f"Which {first.capitalize()} do you mean, {a.split()[0]} {a.split()[1]} or {b.split()[0]} {b.split()[1]}?")],
                       "two contacts match: one short question before reading or sending", cards=[]))

    # ------------------------------------------------------------ 12. "send me" means answer here: no send
    for i, kind in enumerate(["cal_today", "wx_t", "todos", "stock1"]):
        now, w, src = mk(kind, hour=hrs, weekday=wk)
        ask = [f"give me {src.obj} right here", f"just tell me {src.clause}", f"pull up {src.obj} for me", f"show me {src.obj}"][i]
        text = (src.text.replace("I have", "you have").replace("I've got", "you've got").replace("I also have", "you also have")
                .replace("My open to-dos are", "Your open to-dos are").replace("Today you have", "Today you have")
                .replace("I'm", "you're").replace("I've", "you've"))
        text = text.replace("Tomorrow you have", "Tomorrow you have")
        out.append(_sc(styled(rng, ask), now, w, [src.calls, Final(text)],
                       "read-only ask: answer in chat, no send or draft", cards=[]))

    return out


FAMILIES_R3 = {FAMILY: fam_compose_r3}
