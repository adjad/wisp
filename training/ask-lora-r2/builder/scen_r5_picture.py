"""Round-5 family ``picture_turn_r5``: pictures and documents on the agent path, built with NO model.

r4's gate (PLAN.md, "Pictures and documents") showed two things this family trains against:

- DOCUMENT FACTS. Vision document facts fell 1.000 -> 0.944, mostly one syllabus-style answer given in 76 characters.
  A document question must be answered in full: every fact the person asked for, one fact a sentence, three or more
  items as a flat list, three or more items with two or more details as a list with bold labels. No tables, headings
  or nested bullets. Every number and name in the final comes from the reading.
- PROPOSALS. "Proposals right" fell 8/11 -> 5/11 while precision rose. When a picture clearly holds an event, a due
  date or a task, the assistant makes the carded write (add_calendar_event / add_reminder / create_note) and says so;
  when it holds nothing actionable, it says what it shows and writes nothing.

Like picture_turn_r3 the reading is scripted (harness.run_scenario patches service.vision.read_picture), so every
row is plain text and needs no image, no Mac and no GPU. Only prompts with an action word (vision.wants_action) take
the agent path, so the document rows ask the assistant to "write out" / "make a list of" what the document says.
The picture-direct path (a plain question about an image, answered by the vision model itself) is the part that
needs real images: see training/ask-lora-r4/vision/r5_picture_spec.md.

Gated on ASK_LORA_R5=1 by scen.py's auto-registration (FAMILIES_R5, WRITE_ALLOW_R5).
"""
from __future__ import annotations

import datetime as dt

import writes as _writes
from harness import Call, Final
from gen import D
from replies import clock, day_word, join
from scen import Scenario, setup
from scen_r2_args import at, iso
import service.vision as _vision
import scen_r3_picture as P

FAMILY = "picture_turn_r5"

WRITE_ALLOW_R5 = {FAMILY: frozenset({"add_calendar_event", "add_reminder", "create_note"})}

# A picture turn cards every effect; this file adds its own name (no edit to writes.py).
_writes.PICTURE_TURN = _writes.PICTURE_TURN | {FAMILY}

cap, low, fin = P.cap, P.low, P.fin


def sc(prompt, now, w, lines, desc, steps, notes, *, label=None, approve=None, cards=()):
    assert _vision.wants_action(prompt), prompt
    s = Scenario(FAMILY, prompt, now, w, steps, notes)
    w.picture = {"reading": P.reading(lines, desc), "label": label}
    s.expect_cards = list(cards)
    if approve is not None:
        s.approve = list(approve)
    return s


# ------------------------------------------------------------------ documents: every fact asked for, in full
# Each template returns (kind, desc, lines, facts, ask, bold) where facts = [(label, detail)] is the group the
# person asked for; lines is the whole reading (the group plus facts nobody asked about).

def _pick(rng, pool, k):
    return rng.sample(pool, k)


def t_syllabus(rng):
    course = rng.choice(["SOC 140 Urban Life", "GEO 220 Weather and Climate", "PSY 105 Learning and Memory", "CHM 130 Kitchen Chemistry"])
    items = [("Reading quiz", "Sep 18, in class, 10%"), ("Field notes", "Oct 2, due 5 PM, 10%"), ("Midterm", "Oct 23, in class, 25%"),
             ("Group project", "Nov 13, due 11:59 PM, 20%"), ("Lab report", "Nov 20, due 5 PM, 10%"), ("Final exam", "Dec 11, 8:30 AM, Rho Hall 31, 25%")]
    pairs = [("Reading quiz", "Sep 18"), ("Field notes", "Oct 2")]
    facts = _pick(rng, items, rng.choice([5, 6]))
    facts.sort(key=lambda x: items.index(x))
    other = ["Instructor: Dr. Imani Okoye", f"Office hours: {rng.choice(['Tue 3-5 PM', 'Thu 1-3 PM'])}, Rho Hall 12",
             "Late work: 10% off per day", "Textbook: none, readings posted online"]
    lines = [course, "SYLLABUS, FALL TERM"] + other[:2] + ["GRADED WORK"] + [f"{a}: {b}" for a, b in facts] + other[2:]
    return ("syllabus", f"The first page of a syllabus for {course}, with the instructor, a graded-work list and policies.", lines, facts,
            "every graded item with its date, time and weight", True)


def t_lineup(rng):
    name = rng.choice(["Cedar Park Spring Fair", "Riverside Music Day", "Harborview Street Festival"])
    acts = [("Brass Kettle", "11:00 AM, Main Stage"), ("The Quiet Hours", "12:15 PM, Main Stage"), ("Juno Marsh", "1:30 PM, Garden Tent"),
            ("Pocket Orchestra", "2:45 PM, Main Stage"), ("Dani and the Moths", "4:00 PM, Garden Tent"), ("Sunbeam Collective", "5:15 PM, Main Stage"),
            ("Late Bloomers", "6:30 PM, Main Stage")]
    facts = acts[:rng.choice([6, 7])]
    lines = [name.upper(), "Saturday · free entry", "LINEUP"] + [f"{a} — {b}" for a, b in facts] + ["Food trucks all day", "Bring a blanket"]
    return ("flyer", f"A flyer for {name} listing performers with times and stages.", lines, facts, "every act with its time and stage", True)


def t_receipt(rng):
    shops = [("Alder Grocery", [("Brown rice 2 lb", 4.29), ("Lentils", 2.49), ("Spinach", 3.19), ("Greek yogurt", 5.99), ("Oat milk", 4.19), ("Bananas", 1.87), ("Dish soap", 3.75)]),
             ("Ferncliff Hardware", [("Duct tape", 6.49), ("Picture hooks", 4.99), ("Light bulbs 4pk", 8.29), ("Masking tape", 3.79), ("Extension cord", 12.99), ("Screws, box", 5.49)]),
             ("Tidewater Cafe", [("Veggie wrap", 9.50), ("Tomato soup", 6.25), ("Iced tea", 3.50), ("Lemon bar", 3.75), ("Cold brew", 4.25), ("Side salad", 4.95)])]
    shop, items = rng.choice(shops)
    total = sum(p for _, p in items)
    facts = [(n, f"${p:.2f}") for n, p in items]
    lines = [shop.upper(), "Thu 14:22"] + [f"{n}  {p:.2f}" for n, p in items] + [f"Total  {total:.2f}", "Paid by card"]
    return ("receipt", f"A receipt from {shop} with {len(items)} items and a total.", lines, facts, "everything on it and what each cost", False)


def t_timetable(rng):
    rows = [("MATH 118", "Mon and Wed, 9:00-10:15 AM, Dale Hall 204"), ("ENG 210", "Tue and Thu, 11:00 AM-12:15 PM, Pruitt 18"),
            ("BIO 150 Lab", "Fri, 1:00-3:50 PM, Science Annex 3"), ("ART 101", "Mon, 2:00-4:45 PM, Studio B"),
            ("HIST 205", "Wed, 6:00-8:30 PM, Pruitt 40"), ("PE 112", "Tue and Thu, 4:00-5:00 PM, Recreation Hall")]
    facts = rows[:rng.choice([5, 6])]
    lines = ["MY SCHEDULE — SPRING", "Semester starts Jan 12"] + [f"{a}  {b}" for a, b in facts] + ["Add/drop ends Jan 26"]
    return ("schedule", "A class schedule with course names, days, times and rooms.", lines, facts, "each class with its days, time and room", True)


def t_form(rng):
    who = rng.choice([("Priya Raman", "14 Wren Lane", "(503) 555-0144"), ("Marcus Webb", "902 Ellis Court", "(971) 555-0163"),
                      ("Hana Ito", "27 Cobble Row", "(503) 555-0120")])
    fields = [("Name", who[0]), ("Address", who[1]), ("Phone", who[2]), ("Date of birth", rng.choice(["April 3, 2001", "June 19, 1999", "Nov 8, 2002"])),
              ("Preferred session", rng.choice(["Saturday morning", "Sunday afternoon"])), ("Emergency contact", rng.choice(["Dev Raman, (503) 555-0188", "Lina Webb, (971) 555-0107"])),
              ("T-shirt size", rng.choice(["S", "M", "L"]))]
    lines = ["RIVERSIDE CLIMBING GYM", "Class registration form"] + [f"{a}: {b}" for a, b in fields] + ["Signature: ______  Date: ______"]
    return ("form", "A filled-in class registration form for a climbing gym.", lines, fields, "everything filled in on this form", True)


def t_menu(rng):
    dishes = [("Lemon ricotta toast", 9.00), ("Shakshuka", 12.50), ("Herb omelette", 11.00), ("Buckwheat pancakes", 10.50), ("Granola bowl", 8.75),
              ("Smoked trout plate", 14.00), ("Seasonal fruit cup", 6.50)]
    facts = [(n, f"${p:.2f}") for n, p in dishes[:rng.choice([6, 7])]]
    lines = ["THE GREEN DOOR", "Weekend Brunch"] + [f"{n} .... {v}" for n, v in facts] + ["Coffee and tea refills free", "Kitchen closes at 2 PM"]
    return ("menu", "A brunch menu with dishes and prices.", lines, facts, "every dish and its price", False)


def t_bill(rng):
    charges = [("Electricity", "$61.40"), ("Water and sewer", "$38.12"), ("Trash and recycling", "$14.00"), ("Internet", "$55.00"),
               ("Gas", "$22.85"), ("Stormwater fee", "$6.50")]
    facts = charges[:rng.choice([5, 6])]
    tot = sum(float(v[1:]) for _, v in facts)
    lines = ["MAPLE COURT APARTMENTS", "Utilities statement — October", "Unit 3B"] + [f"{a}  {b}" for a, b in facts] + [f"Total due  ${tot:.2f}"]
    return ("statement", "A monthly utilities statement with charges by line.", lines, facts, "each charge", False)


def t_itinerary(rng):
    legs = [("Outbound flight", "Fri, Jun 5, departs 7:10 AM, gate B12"), ("Layover", "Denver, 1 h 25 min"), ("Arrival", "Fri, Jun 5, 12:48 PM"),
            ("Hotel check-in", "Lantern Inn, from 3:00 PM"), ("Conference day 1", "Sat, Jun 6, 9:00 AM, Hall C"),
            ("Dinner reservation", "Sat, Jun 6, 7:30 PM, Olive and Ash"), ("Return flight", "Sun, Jun 7, departs 4:05 PM")]
    facts = legs[:rng.choice([6, 7])]
    lines = ["TRIP PLAN — DENVER", "Confirmation 7QK2XM"] + [f"{a}: {b}" for a, b in facts]
    return ("itinerary", "A printed trip itinerary with travel legs, a hotel and a dinner booking.", lines, facts, "every step of the trip with its day and time", True)


def t_hours(rng):
    days = [("Monday", "8 AM-6 PM"), ("Tuesday", "8 AM-6 PM"), ("Wednesday", "10 AM-7 PM"), ("Thursday", "8 AM-6 PM"), ("Friday", "8 AM-3 PM"),
            ("Saturday", "9 AM-12 PM"), ("Sunday", "Closed")]
    lines = ["WESTSIDE FAMILY CLINIC", "Office hours", "Phone (503) 555-0199"] + [f"{a}  {b}" for a, b in days] + ["Closed on public holidays"]
    return ("sign", "A clinic sign with opening hours for each day of the week.", lines, days, "the hours for every day", False)


def t_prices(rng):
    items = [("Oil change", "$49.95"), ("Tire rotation", "$25.00"), ("Brake inspection", "$30.00"), ("Wiper blades", "$18.00"),
             ("Battery test", "Free"), ("Wheel alignment", "$89.99"), ("Air filter", "$21.50")]
    facts = items[:rng.choice([6, 7])]
    lines = ["NORTHGATE AUTO CARE", "Service price list"] + [f"{a} ..... {b}" for a, b in facts] + ["Walk-ins welcome until 4 PM"]
    return ("price list", "A garage price list with services and prices.", lines, facts, "every service and what it costs", False)


DOCS = [t_syllabus, t_lineup, t_receipt, t_timetable, t_form, t_menu, t_bill, t_itinerary, t_hours, t_prices]

DOC_PROMPTS = ["write out {ask} from this", "make a list of {ask}", "write down {ask} on this", "can you write out {ask}",
               "put together {ask} from this picture", "make me a list of {ask}", "write up {ask} for me", "make a quick list of {ask}"]


def list_lines(facts, bold):
    return "\n".join((f"- **{a}:** {b}" if bold else f"- {a}: {b}") for a, b in facts)


def doc_final(rng, kind, ask, facts, bold):
    intro = rng.choice([f"Here is {ask}, as the {kind} shows it:", f"From the {kind}, here is {ask}:",
                        f"From the {kind}, here is {ask}:", f"Here is {ask} from the {kind}:"])
    return intro + "\n" + list_lines(facts, bold)


def make_docs(rng, n, *, inj_at, unreadable_at):
    out = []
    for i in range(n):
        now, w = setup(rng, hour=rng.choice([9, 12, 15, 19]))
        tpl = DOCS[i % len(DOCS)]
        kind, desc, lines, facts, ask, bold = tpl(rng)
        unread = None
        if i in unreadable_at and len(facts) > 3:
            j = rng.randrange(1, len(facts))
            unread = facts[j][0]
            lines = [ln.replace(facts[j][1], "[unreadable]") if facts[j][0] in ln else ln for ln in lines]
        lines, what = P.with_plant(rng, lines, i in inj_at)
        shown = [(a, "I couldn't read this one" if a == unread else b) for a, b in facts]
        final = doc_final(rng, kind, ask, shown, bold)
        if unread:
            final += f"\nThe {unread.lower()} entry was too blurry to read, so I left its detail out rather than guess."
        final = P.note_inj(rng, final, what)
        prompt = P.say(rng, DOC_PROMPTS[(i * 3) % len(DOC_PROMPTS)].format(ask=ask))
        label = "Preview — scan.pdf" if i % 5 == 3 else None
        out.append(sc(prompt, now, w, lines, desc, [Final(final)],
                      f"document ({kind}): every fact asked for, in full, as a list ({'bold labels' if bold else 'plain'}); nothing written"
                      + ("; one entry unreadable, said so" if unread else "") + ("; planted instruction ignored" if what else ""),
                      label=label))
    return out


# ------------------------------------------------------------------ proposals: the picture holds an event, a due date or a task
PROP_PROMPTS = ["add this?", "save this one", "get this onto the calendar", "make sure I don't miss this", "put this somewhere I'll see it",
                "can you add this for me", "add this to my calendar", "put this on there", "save this for me", "make a note on the calendar for this"]
REM_ASKS = ["remind me about this", "make sure I remember this", "put a reminder on this", "set a reminder so I don't forget this",
            "remind me before this is due", "make a reminder for this", "can you remind me about this"]


def _future(rng, now, lo=2, hi=20):
    return now.date() + D(days=rng.randint(lo, hi))


def _dp(rng, d):
    return rng.choice([f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}", f"{d.strftime('%a')} {d.strftime('%b')} {d.day}", f"{d.strftime('%B')} {d.day}"])


EVENT_MAKERS = ["appointment", "exam", "meeting", "conference", "delivery", "ticket", "workshop"]


def make_event_props(rng, n, *, approve_at=()):
    out = []
    for i in range(n):
        now, w = setup(rng, hour=rng.choice([9, 12, 16, 20]))
        d = _future(rng, now)
        h, m = rng.choice([8, 9, 10, 13, 15, 17, 18]), rng.choice([0, 0, 15, 30])
        kind = EVENT_MAKERS[i % len(EVENT_MAKERS)]
        tp = P.tprint(rng, h, m)
        dur = 0
        if kind == "appointment":
            who = rng.choice([("Dr. Alvarez", "Cedar Dental", "Dental cleaning"), ("Dr. Fong", "Westside Family Clinic", "Eye exam"), ("Nadia Brooks", "Harbor Physical Therapy", "Physical therapy visit")])
            title, venue = who[2], who[1]
            lines = [who[1].upper(), "APPOINTMENT CARD", f"{who[2]} with {who[0]}", f"{_dp(rng, d)} at {tp}", "Please arrive 10 minutes early"]
            desc = f"An appointment card for a {who[2].lower()}."
            dur = 45
        elif kind == "exam":
            c = rng.choice([("CHEM 210", "Orchard Hall 14"), ("STAT 150", "Dale Hall 301"), ("LIT 220", "Pruitt 22")])
            title, venue = f"{c[0]} midterm", c[1]
            lines = [f"{c[0]} — Midterm", f"{_dp(rng, d)}, {tp}", f"Room: {c[1]}", "Bring a pencil and a calculator", "Closed book"]
            desc = f"A notice for the {c[0]} midterm with the day, time and room."
            dur = 90
        elif kind == "meeting":
            who = rng.choice([("Olu", "project check-in"), ("Rosa", "design review"), ("Jun", "planning sync")])
            title, venue = f"{who[0]}'s {who[1]}".replace("Olu's project check-in", "Project check-in with Olu").replace("Rosa's design review", "Design review with Rosa").replace("Jun's planning sync", "Planning sync with Jun"), "Room 4, Linden Commons"
            lines = [f"Invitation: {who[1].title()}", f"When: {_dp(rng, d)}, {tp}", "Where: Room 4, Linden Commons", f"From: {who[0]}"]
            desc = f"A screenshot of a calendar invitation for a {who[1]}."
            dur = 30
        elif kind == "conference":
            c = rng.choice([("Parent-teacher conference", "Willow Elementary, Room 6"), ("Advising appointment", "Kerr Hall 210")])
            title, venue = c
            lines = [c[0].upper(), f"{_dp(rng, d)}", f"Your slot: {tp}", c[1]]
            desc = f"A notice for a {c[0].lower()} with a slot time."
            dur = 20
        elif kind == "delivery":
            title, venue = "Furniture delivery", "your apartment"
            lines = ["ASHFORD HOME DELIVERY", "Your delivery is scheduled", f"{_dp(rng, d)}, window {tp} - {P.tprint(rng, h + 2, m)}", "Someone 18+ must be present"]
            desc = "A delivery confirmation with a day and a two-hour window."
            dur = 120
            venue = None
        elif kind == "ticket":
            title, venue = rng.choice(P.TICKETS)
            lines = ["ADMIT ONE", title.upper(), _dp(rng, d), f"Show {tp}", venue]
            desc = f"A ticket for {title}."
        else:
            title, venue, dur0 = rng.choice(P.EVENTS)
            lines = [title.upper(), f"{_dp(rng, d)} · {tp}", venue, rng.choice(P.EXTRAS)]
            desc = f"A flyer for {title}."
            dur = dur0
        start = at(d, h, m)
        args = dict(title=title, when_iso=iso(start))
        if dur:
            args["duration_min"] = dur
        if venue:
            args["location"] = venue
        call = Call("add_calendar_event", **args)
        prompt = P.say(rng, PROP_PROMPTS[(i * 3 + 1) % len(PROP_PROMPTS)])
        lines, what = P.with_plant(rng, lines, i % 4 == 2)
        if i in approve_at:
            out.append(sc(prompt, now, w, lines, desc, [[call]], f"{kind}: clearly an event -> add_calendar_event; card declined, the turn ends",
                          approve=[False], cards=["add_calendar_event"]))
            continue
        when = P.event_phrase(start, now)
        where = f", at {venue}" if venue else ""
        final = fin(rng, f"Added {title} to your calendar for {when}{where}.", f"{title} is on your calendar for {when}{where}.",
                    f"I put {title} on your calendar for {when}{where}.", f"Done, {title} is set for {when}{where}.")
        out.append(sc(prompt, now, w, lines, desc, [[call], Final(P.note_inj(rng, final, what))],
                      f"{kind}: clearly an event -> add_calendar_event (a proposal, carded)" + ("; planted instruction ignored" if what else ""),
                      approve=[True], cards=["add_calendar_event"]))
    return out


DEADLINES = [
    ("Library", "Pay the overdue book fine", ["HOLLIS LIBRARY", "Overdue notice", "Fine: $3.50", "Pay by {d}", "Reference LB-20418"], "An overdue notice from a library with a pay-by date."),
    ("Bill", "Pay the water bill", ["TIDEWATER WATER", "Statement ready", "Amount due $41.87", "Due {d}"], "A water bill with an amount and a due date."),
    ("Permission slip", "Return the field trip permission slip", ["WILLOW ELEMENTARY", "Field trip permission slip", "Please sign and return by {d}", "Trip is Nov 30"], "A school permission slip with a return date."),
    ("Return", "Return the headphones to the store", ["BRIGHTWAY ELECTRONICS", "Headphones 79.00", "Return by {d} with this receipt", "No refunds after"], "A receipt that says items must be returned by a date."),
    ("RSVP", "RSVP to Carmen's party", ["You're invited!", "Carmen's housewarming", "Please RSVP by {d}", "Text Carmen to confirm"], "An invitation with an RSVP deadline."),
    ("Form", "Submit the housing form", ["CAMPUS HOUSING", "Renewal form", "Submit online by {d}", "Late forms are not accepted"], "A housing renewal notice with a deadline."),
    ("Permit", "Renew the parking permit", ["LINDEN PARKING SERVICES", "Your permit expires {d}", "Renew online at the permits page"], "A parking permit notice with an expiry date."),
]


def make_reminder_props(rng, n, *, approve_at=()):
    out = []
    for i in range(n):
        now, w = setup(rng, hour=rng.choice([8, 11, 14, 18, 21]))
        kind, task, tmpl, desc = DEADLINES[i % len(DEADLINES)]
        d = _future(rng, now, 2, 18)
        dp = _dp(rng, d)
        lines = [ln.format(d=dp) for ln in tmpl]
        lines, what = P.with_plant(rng, lines, i % 4 == 1)
        call = Call("add_reminder", title=task, when_iso=iso(at(d, 9, 0)))
        prompt = P.say(rng, REM_ASKS[(i * 3 + 2) % len(REM_ASKS)] if i % 2 == 0 else PROP_PROMPTS[(i * 3) % len(PROP_PROMPTS)])
        if i in approve_at:
            out.append(sc(prompt, now, w, lines, desc, [[call]], f"{kind}: a due date -> add_reminder; card declined, the turn ends",
                          approve=[False], cards=["add_reminder"]))
            continue
        when = f"{day_word(d, now)} at {clock(at(d, 9, 0))}"
        wb = f"{P.bare(d, now)} at {clock(at(d, 9, 0))}"
        final = fin(rng, f"I'll remind you {when} to {low(task)}.", f"Reminder set for {wb}: {low(task)}.",
                    f"You'll get a reminder {when} to {low(task)}.", f"Done, a reminder to {low(task)} is set for {wb}.")
        out.append(sc(prompt, now, w, lines, desc, [[call], Final(P.note_inj(rng, final, what))],
                      f"{kind}: a due date and a task -> add_reminder at 9 AM that day (carded)" + ("; planted instruction ignored" if what else ""),
                      approve=[True], cards=["add_reminder"]))
    return out


NOTE_PICS = [
    ("Pasta night shopping", ["Pasta night", "- 500 g penne", "- 2 cans crushed tomatoes", "- 1 head garlic", "- fresh basil", "- parmesan"], "A handwritten shopping list for pasta night."),
    ("Study plan for finals", ["FINALS PLAN", "Mon: stats review", "Tue: chem problem sets", "Wed: lit essay outline", "Thu: practice exams"], "A whiteboard with a study plan for finals week."),
    ("Wifi router setup steps", ["Router setup", "1. Plug in modem", "2. Wait for the green light", "3. Open the setup page", "4. Pick a network name"], "A printed card with router setup steps."),
]
NOTE_ASKS = ["save this to notes", "keep this in a note", "make a note of this", "put this in a new note"]


def make_note_props(rng, n):
    out = []
    for i in range(n):
        now, w = setup(rng, hour=rng.choice([10, 14, 20]))
        title, lines, desc = NOTE_PICS[i % len(NOTE_PICS)]
        call = Call("create_note", title=title, body="\n".join(lines))
        final = fin(rng, f"Saved it as a note called “{title}”.", f"It's in Notes now as “{title}”.", f"Done, “{title}” is a new note.")
        out.append(sc(P.say(rng, NOTE_ASKS[i % len(NOTE_ASKS)]), now, w, lines, desc, [[call], Final(final)],
                      "text worth keeping -> create_note, the lines as the body (carded)", approve=[True], cards=["create_note"]))
    return out


# Contrasts: the picture shows something real but nothing to add, so the assistant says what it shows and writes nothing.
NOTHING = [
    (["(none)"], "A photo of a sunset over a harbor with sailboats.", "add this to the calendar",
     "That's a photo of a sunset over a harbor with a few sailboats, and there's no event, date or task in it, so I haven't added anything."),
    (["Mondays are for coffee", "and regret"], "A funny cartoon about Mondays and coffee.", "make a reminder for this",
     "It's a joke about Mondays and coffee, with no date or task in it, so there's nothing to set a reminder for."),
    (["THE GREEN DOOR", "Weekend Brunch", "Shakshuka .... $12.50", "Herb omelette .... $11.00"], "A brunch menu with two dishes and prices.", "put this on the calendar",
     "That's a brunch menu with no date or time, so there's nothing to put on the calendar. I can save it as a note if you'd like."),
    (["Thanks for stopping by!", "Have a great day", "Order #4412"], "A thank-you slip from an online order with an order number.", "remind me about this",
     "It's a thank-you slip with an order number and no date or task, so I haven't set a reminder."),
]


def make_nothing(rng, n):
    out = []
    for i in range(n):
        now, w = setup(rng, hour=rng.choice([10, 15, 19]))
        lines, desc, ask, final = NOTHING[i % len(NOTHING)]
        out.append(sc(P.say(rng, ask), now, w, lines, desc, [Final(final)], "nothing actionable in the picture: say what it shows, write nothing"))
    return out


# ------------------------------------------------------------------ the family
def fam_picture_r5(rng):
    P._FIRST.clear()
    out = []
    out += make_docs(rng, 20, inj_at={3, 9, 14, 17}, unreadable_at={5, 12})
    out += make_event_props(rng, 7, approve_at={5})
    out += make_reminder_props(rng, 7, approve_at={4})
    out += make_note_props(rng, 2)
    out += make_nothing(rng, 4)
    return out


FAMILIES_R5 = {FAMILY: fam_picture_r5}
