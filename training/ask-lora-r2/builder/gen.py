"""Random synthetic worlds for Sam Rivera. Everything here is invented."""
from __future__ import annotations

import datetime as dt
import random

from world import Email, Event, Msg, Note, Reminder, World

D = dt.timedelta

# Weekly schedule: weekday -> [(hour, minute, minutes, title, location, kind)]
CLASSES = {
    0: [(10, 0, 50, "CS 162 Operating Systems — Lecture", "Kerr Hall 204", "event"),
        (14, 0, 75, "MATH 241 Linear Algebra", "Avery Science 110", "event")],
    1: [(11, 0, 80, "PHIL 110 Intro to Ethics", "Mercer Hall 12", "event"),
        (16, 0, 240, "Library help desk shift", "Hollis Library", "event")],
    2: [(10, 0, 50, "CS 162 Operating Systems — Lecture", "Kerr Hall 204", "event"),
        (14, 0, 75, "MATH 241 Linear Algebra", "Avery Science 110", "event"),
        (17, 30, 60, "CS 162 Section", "Kerr Hall 118", "event")],
    3: [(11, 0, 80, "PHIL 110 Intro to Ethics", "Mercer Hall 12", "event"),
        (16, 0, 240, "Library help desk shift", "Hollis Library", "event")],
    4: [(10, 0, 50, "CS 162 Operating Systems — Lecture", "Kerr Hall 204", "event"),
        (13, 0, 60, "CS 210 Data Structures Lab", "Kerr Hall 301", "event")],
    5: [(8, 0, 90, "Running club long run", "Riverside Track", "event")],
    6: [],
}

ONE_OFFS = [
    ("Study group with Aisha", 120, "Hollis Library, room 3B", "meeting"),
    ("Coffee with Theo", 60, "Brewed Awakening", "meeting"),
    ("Dentist appointment", 60, "Pine Street Dental", "event"),
    ("Office hours — Prof. Okafor", 45, "Kerr Hall 412", "meeting"),
    ("Internship interview — Fernbrook Labs", 45, "Zoom", "meeting"),
    ("Career fair", 180, "Student Union Ballroom", "event"),
    ("Dinner with Carmen", 90, "Golden Lotus", "event"),
    ("Haircut", 45, "Clipper Joe's", "event"),
    ("Project sync with Jordan", 60, "Kerr Hall 301", "meeting"),
    ("Movie night with Maya", 150, "Home", "event"),
    ("Advising appointment", 30, "Dean's Office, Mercer Hall", "meeting"),
    ("Volunteer shift — food bank", 180, "Eastside Food Bank", "event"),
    ("Yoga class", 60, "Rec Center Studio B", "event"),
    ("Linear algebra review session", 90, "Avery Science 110", "event"),
]

EXAMS = [("CS 162 Midterm", "Kerr Hall 204"), ("MATH 241 Quiz 4", "Avery Science 110"),
         ("PHIL 110 Essay due", ""), ("CS 210 Lab practical", "Kerr Hall 301"),
         ("MATH 241 Midterm 2", "Avery Science 110")]

REMINDER_TITLES = [
    "Submit OS project 2", "Pay phone bill", "Email Prof. Okafor about the project extension",
    "Return library books", "Buy groceries", "Finish linear algebra problem set 6",
    "Renew bus pass", "Call Mom", "Read PHIL 110 chapter 5", "Update resume for Fernbrook",
    "Book flight home for winter break", "Pick up prescription", "Venmo Maya for utilities",
    "Sign up for spring classes", "Water the plants", "Laundry", "Submit the financial aid form",
    "Print the ethics essay", "Reply to Jordan about the lab writeup", "Schedule a haircut",
]

PEOPLE_MAIL = [
    ("Prof. Adaeze Okafor", "aokafor@linden.edu",
     ["Project 2 extension request", "CS 162 midterm logistics", "Re: question about page tables"],
     ["Hi Sam, I can give you until Friday at 11:59 PM for project 2. Please don't slip further — Prof. Okafor",
      "The midterm is in Kerr Hall 204. Bring a pencil and one page of handwritten notes.",
      "Good question. The TLB is flushed on a context switch unless the entries are tagged with an ASID."]),
    ("Hana Kowalczyk", "hkowalczyk@linden.edu",
     ["Section 4 slides", "Regrade for homework 3", "Office hours moved"],
     ["Slides from today's section are posted on LindenLearn. See you Wednesday!",
      "I looked at your regrade request and added 4 points to question 2.",
      "My office hours move to Thursday 3–4 PM this week only."]),
    ("Renata Szabo", "rszabo@fernbrooklabs.com",
     ["Fernbrook Labs — interview next steps", "Following up on your application"],
     ["Thanks for applying to the summer SWE internship. Could you share three 45-minute slots next week for a technical interview?",
      "Just following up — are you still interested in the internship? Let me know by Friday."]),
    ("Dana Whitfield", "dwhitfield@linden.edu",
     ["Help desk schedule for next week", "Can you cover Saturday?"],
     ["Next week's help desk schedule is attached. You're on Tuesday and Thursday 4–8 PM as usual.",
      "Hi Sam, could you cover the Saturday 10–2 shift? Let me know by Thursday."]),
    ("Carmen Rivera", "carmen.rivera@example.com",
     ["Mom's birthday plan", "Photos from the weekend"],
     ["Can you chip in $30 for Mom's birthday gift? I'm ordering it Sunday.",
      "Here are the photos from Saturday! The one at the lake is my favorite."]),
    ("Jordan Ellis", "jellis@linden.edu",
     ["Lab writeup draft", "Data structures project split"],
     ["I put our lab writeup draft in the shared folder. Can you do the analysis section by Wednesday?",
      "Want to split the project: I take the hash map, you take the tests?"]),
]

NEWSLETTERS = [
    ("Linden Career Center", "careers@linden.edu", ["Career fair this Thursday — 60+ employers", "Resume review drop-ins"]),
    ("LindenLearn", "notifications@lindenlearn.edu", ["New grade posted: CS 162 Homework 4", "Assignment due tomorrow: MATH 241 PS6", "Announcement: PHIL 110 room change"]),
    ("Northline Bank", "alerts@northlinebank.com", ["Your statement is ready", "Low balance alert", "Direct deposit received"]),
    ("Trailhead Running Co.", "hello@trailheadrunning.example", ["20% off trail shoes this weekend", "Your training plan for week 6"]),
    ("Soundwave Premium", "billing@soundwave.example", ["Your student plan renews soon", "Receipt for your Soundwave Premium payment"]),
    ("DevDigest Weekly", "news@devdigest.example", ["This week: Rust 2027 edition, WASM tips", "Top 10 interview questions for interns"]),
    ("Cascade Air", "noreply@cascadeair.example", ["Fare alert: Portland to Denver from $89", "Your trip receipt"]),
    ("Pine Street Pharmacy", "rx@pinestreetpharmacy.example", ["Your prescription is ready for pickup"]),
]

TEXTS = [
    ("Maya Chen", ["Can you grab milk on your way home?", "Rent is due on the 1st btw", "I'm making pasta tonight if you want some",
                   "Did you take my charger lol", "Landlord is coming Thursday at 10 to fix the sink"]),
    ("Theo Park", ["Still on for coffee?", "Bro did you see the game", "Want to study at the library Sunday?",
                   "Running late, 10 min"]),
    ("Mom", ["Call me when you get a chance ❤️", "Did you book your flight for winter break?",
             "Dad says hi. Eat something green this week!"]),
    ("Carmen Rivera", ["Can you send me $30 for Mom's gift?", "Ordering it Sunday!", "Lol look at this dog"]),
    ("Jordan Ellis", ["Are you done with the analysis part?", "Lab writeup due Wednesday 11:59",
                      "I pushed my half of the hash map"]),
    ("Aisha Karim", ["Study group moved to 7 tonight", "Can you bring your linear algebra notes?"]),
    ("Ben Ortiz", ["Long run Saturday 8am at Riverside, you in?", "Bring water, it's 10 miles"]),
]

MY_TEXTS = ["On my way", "Sounds good!", "Yeah I'll grab it", "Can't tonight, sorry — tomorrow?",
            "Thanks!", "I'll send it tonight", "Lol yes"]

NOTES = [
    ("OS midterm study plan", "Topics: processes vs threads, scheduling (RR, MLFQ), page tables, TLB, deadlock.\n- Mon: scheduling\n- Tue: virtual memory\n- Wed: practice exam", "School"),
    ("Grocery list", "- oat milk\n- eggs\n- spinach\n- rice\n- coffee", "Notes"),
    ("Internship apps", "Fernbrook Labs — applied, interview pending\nHarborline — applied Oct 2\nNimbus Data — need referral", "Career"),
    ("Half marathon training", "Week 6: 3 easy runs + long run 10 mi\nWeek 7: tempo 4 mi, long run 11 mi", "Running"),
    ("Ethics essay ideas", "Trolley problem variations; autonomous cars; consent in data collection", "School"),
    ("Gift ideas", "Mom: scarf or a cookbook\nCarmen: concert tickets\nTheo: climbing gym pass", "Notes"),
    ("Books to read", "- The Pragmatic Programmer\n- Educated\n- Project Hail Mary", "Notes"),
    ("Wifi + apartment info", "Router: second shelf in the hallway. Landlord: call the office for repairs.", "Home"),
]

PATTERNS = [
    "You usually go for a run on Saturday mornings around 8am (seen 6 of 8 weeks; confidence 70%)",
    "On weekdays your first commitment is usually around 10am and your last ends around 8pm on Tue/Thu (seen 18 of 24 days; confidence 62%)",
    "You usually text Maya Chen in the evenings (6–9pm) (seen 14 of 20 days; confidence 58%)",
    "You often move or skip the Friday CS 210 lab prep block (seen 3 of 6 times; confidence 41%)",
    "You usually study at Hollis Library on Sunday afternoons (seen 5 of 8 weeks; confidence 52%)",
]

FILES = {
    "~/Downloads": [("SamRivera_Resume_2026.pdf", "96KB"), ("cs162_project2_starter.zip", "2MB"),
                    ("Wisp-installer.dmg", "148MB"), ("Zoom.pkg", "112MB"), ("lease_agreement.pdf", "410KB"),
                    ("PS6_linear_algebra.pdf", "220KB"), ("IMG_2231.HEIC", "3MB"), ("budget_fall.xlsx", "38KB")],
    "~/Documents": [("Ethics essay draft.docx", "44KB"), ("Resume_old.docx", "31KB"),
                    ("Internship tracker.xlsx", "22KB"), ("cs162", "--"), ("taxes 2025", "--")],
    "~/Desktop": [("Screenshot 2026-10-02 at 9.14.11 PM.png", "512KB"), ("todo.txt", "1KB")],
}

WEATHER = {
    "Portland,OR": {"label": "Portland, Oregon, United States", "now_f": 52, "desc": "Light rain", "feels_f": 49,
                    "hum": 88, "wind": 9,
                    "days": None},
}


def make_weather(now: dt.datetime, rng: random.Random, location="Portland,OR", label="Portland, Oregon, United States"):
    descs = [("Light rain", 80), ("Overcast", 20), ("Partly cloudy", 10), ("Sunny", 0), ("Showers", 70)]
    base = rng.randint(44, 62)
    days = []
    for i in range(3):
        d, rain = rng.choice(descs)
        if i == 0:
            today_desc, today_rain = d, rain
        hi = base + rng.randint(2, 8) if i == 0 else base + rng.randint(-3, 6)
        days.append(((now + D(days=i)).strftime("%Y-%m-%d"), hi, hi - rng.randint(8, 14), d, rain))
    return {"label": label, "now_f": base, "desc": today_desc, "feels_f": base - rng.randint(0, 4),
            "hum": rng.randint(55, 92) if today_rain else rng.randint(35, 60), "wind": rng.randint(2, 15), "days": days}


def _at(day: dt.date, h: int, m: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(h, m))


def schedule(now: dt.datetime, rng: random.Random, days_back=14, days_fwd=21, extra=4, busy=1.0):
    events, past = [], []
    start = now.date() - D(days=days_back)
    for i in range(days_back + days_fwd):
        day = start + D(days=i)
        for h, m, mins, title, loc, kind in CLASSES[day.weekday()]:
            if rng.random() > busy:
                continue
            ev = Event(title, _at(day, h, m), mins, loc, kind)
            (past if ev.start < now else events).append(ev)
    for _ in range(extra):
        title, mins, loc, kind = rng.choice(ONE_OFFS)
        day = now.date() + D(days=rng.randint(-10, 14))
        h = rng.choice([9, 12, 15, 17, 18, 19])
        ev = Event(title, _at(day, h, rng.choice([0, 30])), mins, loc, kind)
        (past if ev.start < now else events).append(ev)
    return events, past


def reminders(now, rng, n=5, overdue=2, undated=1):
    titles = rng.sample(REMINDER_TITLES, n + overdue + undated)
    out = []
    for t in titles[:overdue]:
        out.append(Reminder(t, _at(now.date() - D(days=rng.randint(1, 5)), rng.choice([9, 12, 17, 20]), 0)))
    for t in titles[overdue:overdue + n]:
        out.append(Reminder(t, _at(now.date() + D(days=rng.randint(0, 9)), rng.choice([9, 12, 17, 20, 21]), 0)))
    for t in titles[overdue + n:]:
        out.append(Reminder(t, None))
    return [r for r in out if r.due is None or r.due.date() != now.date() or r.due > now
            or r.due.date() < now.date()] or out


def emails(now, rng, n_people=3, n_news=6, unread_frac=0.6, hours=96):
    out = []
    for sender, addr, subjects, bodies in rng.sample(PEOPLE_MAIL, min(n_people, len(PEOPLE_MAIL))):
        k = rng.randrange(len(subjects))
        out.append(Email(sender, addr, subjects[k], now - D(minutes=rng.randint(20, hours * 60)),
                         bodies[k % len(bodies)], rng.random() < unread_frac, True))
    for _ in range(n_news):
        sender, addr, subjects = rng.choice(NEWSLETTERS)
        out.append(Email(sender, addr, rng.choice(subjects), now - D(minutes=rng.randint(10, hours * 60)),
                         "Newsletter content.", rng.random() < unread_frac, False))
    return out


def texts(now, rng, n_convs=3, per=2, hours=60):
    out = []
    for name, lines in rng.sample(TEXTS, min(n_convs, len(TEXTS))):
        for line in rng.sample(lines, min(per, len(lines))):
            out.append(Msg(name, name, line, now - D(minutes=rng.randint(15, hours * 60)), rng.random() < 0.5))
        if rng.random() < 0.5:
            out.append(Msg(name, "Me", rng.choice(MY_TEXTS), now - D(minutes=rng.randint(5, hours * 60))))
    return out


def notes(now, rng, n=4):
    out = []
    for title, body, folder in rng.sample(NOTES, n):
        out.append(Note(title, body, now - D(hours=rng.randint(2, 24 * 40)), folder))
    return out


def make_world(now: dt.datetime, rng: random.Random, *, busy=1.0, extra=4, n_rem=5, overdue=2, undated=1,
               people=3, news=6, convs=3, n_notes=4, exams=True) -> World:
    ev, past = schedule(now, rng, extra=extra, busy=busy)
    if exams:
        for title, loc in rng.sample(EXAMS, 2):
            day = now.date() + D(days=rng.randint(1, 12))
            ev.append(Event(title, _at(day, rng.choice([10, 14, 18]), 0), 80, loc, "exam"))
    w = World(now=now, events=sorted(ev, key=lambda e: e.start), past=past,
              reminders=reminders(now, rng, n_rem, overdue, undated),
              emails=emails(now, rng, people, news), messages=texts(now, rng, convs),
              notes=notes(now, rng, n_notes), files={k: list(v) for k, v in FILES.items()},
              patterns=list(PATTERNS),
              contacts={"Mom": ("(503) 555-0142", ""), "Maya Chen": ("(503) 555-0187", "maya.chen@example.com"),
                        "Theo Park": ("(971) 555-0110", "theo.park@example.com"),
                        "Carmen Rivera": ("(503) 555-0163", "carmen.rivera@example.com"),
                        "Prof. Okafor": ("", "aokafor@linden.edu")})
    w.weather["Portland,OR"] = make_weather(now, rng)
    return w


def random_now(rng: random.Random, *, hour=None, weekday=None) -> dt.datetime:
    base = dt.date(2026, 9, 21) + D(days=rng.randint(0, 210))
    if weekday is not None:
        base += D(days=(weekday - base.weekday()) % 7)
    h = hour if hour is not None else rng.choice([7, 8, 8, 9, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23])
    return dt.datetime.combine(base, dt.time(h, rng.randint(0, 59)))
