"""r5 picture-direct document rows (`doc5_*`): a plain question about a scanned page goes straight to the vision model,
and the answer must list EVERY fact the question covers (r4's gate miss was a 76-character syllabus answer).

Spec: r5_picture_spec.md. Pages are rendered with r3's renderer (make_vision_data.new_page / scan_finish / save_jpg, so the
size and scan look match images/doc4_*), the messages are built by service.vision._messages through the same
real_messages() that built the doc4 rows (so the system message and the user turn are byte for byte production's), and
the rows are appended to vision_rows.jsonl. Rerunning replaces every doc5 row and image; nothing else is touched.

    ~/Desktop/MOE_Project/.venv/bin/python training/ask-lora-r4/vision/make_doc5.py

Everything on the pages is invented: no real people, companies, phone numbers or addresses.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
R3 = HERE.parents[1] / "ask-lora-r3" / "vision"

os.environ["ASK_LORA_ROUND"] = "4"          # r3's module reads this at import: OUT/IMG_DIR -> ask-lora-r4/vision
sys.argv = sys.argv[:1]
spec = importlib.util.spec_from_file_location("make_vision_data", R3 / "make_vision_data.py")
M = importlib.util.module_from_spec(spec)
sys.modules["make_vision_data"] = M
spec.loader.exec_module(M)
assert M.IMG_DIR == HERE / "images", M.IMG_DIR

PW, PH, FAMS = M.PW, M.PH, M.FAMS
INK, ACCENT, GREY = (28, 28, 30), (110, 40, 40), (96, 96, 100)
SEED = 50505


# ------------------------------------------------------------------ page drawing
def head(dr, fam, title, sub=None, accent=ACCENT):
    dr.text((70, 66), title, font=fam.b(36), fill=INK)
    y = 118
    if sub:
        dr.text((70, y), sub, font=fam.r(25), fill=accent)
        y += 44
    dr.line([(70, y + 6), (PW - 70, y + 6)], fill=INK, width=2)
    return y + 34


def fit(dr, f, s, w):
    assert M.tl(dr, s, f) <= w, (s, M.tl(dr, s, f), w)


def table(dr, fam, y, cols, rows, size=24, gap=58, header=None, accent=ACCENT):
    """cols: [(x, width, align)]; rows: lists of strings. Draws a faint rule under every row."""
    if header:
        for (x, w, al), h in zip(cols, header):
            tx = x if al == "l" else x + w - M.tl(dr, h, fam.b(size - 3))
            dr.text((tx, y), h, font=fam.b(size - 3), fill=accent)
        y += gap - 14
        dr.line([(70, y - 6), (PW - 70, y - 6)], fill=(120, 120, 120), width=1)
    for r in rows:
        for i, ((x, w, al), s) in enumerate(zip(cols, r)):
            f = fam.b(size) if i == 0 else fam.r(size)
            fit(dr, f, s, w)
            tx = x if al == "l" else x + w - M.tl(dr, s, f)
            dr.text((tx, y), s, font=f, fill=INK)
        y += gap
        dr.line([(70, y - 14), (PW - 70, y - 14)], fill=(214, 212, 206), width=1)
    return y


def foot(dr, fam, i, n):
    if n > 1:
        M.page_no(dr, fam, i, n)


def pg():
    return M.new_page()


# ------------------------------------------------------------------ syllabus
def syllabus(fam, course, term, prof, hours, items, split=None, policy="Late work loses 10 percent per day."):
    """items: [(name, date, weight)]; split = number of items on page 1 (None: one page)."""
    chunks = [items] if split is None else [items[:split], items[split:]]
    n = len(chunks)
    pages = []
    cols = [(70, 400, "l"), (480, 230, "l"), (730, 100, "r")]
    for k, chunk in enumerate(chunks):
        img, dr = pg()
        if k == 0:
            y = head(dr, fam, course, f"{term} syllabus")
            for lab, val in (("Instructor", prof), ("Office hours", hours)):
                dr.text((70, y), lab, font=fam.b(24), fill=INK)
                dr.text((260, y), val, font=fam.r(24), fill=INK)
                y += 44
            y += 22
            dr.text((70, y), "Graded work" + (" (continued overleaf)" if n > 1 else ""), font=fam.b(28), fill=ACCENT)
            y += 56
        else:
            y = head(dr, fam, f"{course.split(':')[0]}  graded work", "continued")
        y = table(dr, fam, y, cols, [(a, b, f"{c}%") for a, b, c in chunk], size=24, header=["Item", "Due", "Weight"])
        if k == n - 1:
            M.block(dr, 70, y + 16, policy, fam.r(23), (50, 50, 50), PW - 140, lh=1.4)
        foot(dr, fam, k + 1, n)
        pages.append(img)
    facts = [(a, f"{b}, {c}%") for a, b, c in items]
    return pages, facts


# ------------------------------------------------------------------ schedule
def schedule(fam, title, sub, cols_h, rows, split=None, note=None, widths=None):
    """rows: [(label, a, b, c)] shown as four columns; the fact detail is 'a, b, c'."""
    chunks = [rows] if split is None else [rows[:split], rows[split:]]
    n = len(chunks)
    w = widths or (210, 210, 190, 190)
    xs = [70]
    for ww in w[:-1]:
        xs.append(xs[-1] + ww)
    cols = [(xs[i], w[i] - 14, "l") for i in range(4)]
    pages = []
    for k, chunk in enumerate(chunks):
        img, dr = pg()
        y = head(dr, fam, title, sub if k == 0 else "continued")
        y = table(dr, fam, y, cols, chunk, size=22, gap=66, header=cols_h)
        if note and k == n - 1:
            M.block(dr, 70, y + 16, note, fam.r(23), (50, 50, 50), PW - 140, lh=1.4)
        foot(dr, fam, k + 1, n)
        pages.append(img)
    return pages, [(r[0], ", ".join(r[1:])) for r in rows]


# ------------------------------------------------------------------ receipt
def receipt(fam, shop, when, items, split=None, tail_lines=("Paid by card",), tax=True):
    """items: [(name, qty, unit)]; line total = qty * unit. Returns pages, facts, total."""
    chunks = [items] if split is None else [items[:split], items[split:]]
    n = len(chunks)
    sub = round(sum(q * u for _, q, u in items), 2)
    tx = round(sub * 0.06, 2) if tax else 0.0
    total = round(sub + tx, 2)
    pages = []
    for k, chunk in enumerate(chunks):
        img, dr = pg()
        y = 70
        y = M.centered(dr, PW / 2, y, shop.upper(), fam.b(38), INK)
        y = M.centered(dr, PW / 2, y + 2, when, fam.r(24), GREY)
        y += 18
        dr.line([(110, y), (PW - 110, y)], fill=GREY, width=2)
        y += 30
        for name, q, u in chunk:
            label = name if q == 1 else f"{name}  x{q}"
            fit(dr, fam.r(27), label, 480)
            dr.text((110, y), label, font=fam.r(27), fill=INK)
            v = f"{q * u:.2f}"
            dr.text((PW - 110 - M.tl(dr, v, fam.r(27)), y), v, font=fam.r(27), fill=INK)
            y += 52
        if k == n - 1:
            y += 8
            dr.line([(110, y), (PW - 110, y)], fill=GREY, width=2)
            y += 24
            rows = [("Subtotal", f"{sub:.2f}")] + ([("Tax 6%", f"{tx:.2f}")] if tax else []) + [("TOTAL", f"{total:.2f}")]
            for lab, v in rows:
                f = fam.b(29) if lab == "TOTAL" else fam.r(27)
                dr.text((110, y), lab, font=f, fill=INK)
                dr.text((PW - 110 - M.tl(dr, v, f), y), v, font=f, fill=INK)
                y += 50
            y += 20
            for t in tail_lines:
                y = M.centered(dr, PW / 2, y, t, fam.r(24), GREY)
        else:
            y += 20
            M.centered(dr, PW / 2, y, "continued on next page", fam.r(24), GREY)
        foot(dr, fam, k + 1, n)
        pages.append(img)
    facts = [((name if q == 1 else f"{name} (x{q})"), f"${q * u:.2f}") for name, q, u in items]
    return pages, facts, total


# ------------------------------------------------------------------ form
def form(fam, org, title, fields, split=None, ink=(24, 52, 120), footer="Signature: ______________   Date: ____________"):
    chunks = [fields] if split is None else [fields[:split], fields[split:]]
    n = len(chunks)
    pages = []
    for k, chunk in enumerate(chunks):
        img, dr = pg()
        y = head(dr, fam, org, title if k == 0 else f"{title} (continued)")
        for lab, val in chunk:
            dr.text((70, y), lab, font=fam.r(23), fill=GREY)
            f = fam.b(26)
            fit(dr, f, val, PW - 390)
            dr.text((330, y - 2), val, font=f, fill=ink)
            dr.line([(325, y + 38), (PW - 70, y + 38)], fill=(160, 160, 160), width=1)
            y += 70
        if k == n - 1:
            dr.text((70, y + 30), footer, font=fam.r(23), fill=GREY)
        foot(dr, fam, k + 1, n)
        pages.append(img)
    return pages, list(fields)


# ------------------------------------------------------------------ menu
def menu(fam, venue, title, entries, split=None, note=None, two_prices=None, accent=ACCENT):
    """entries: [(name, price_text, description or None)]."""
    chunks = [entries] if split is None else [entries[:split], entries[split:]]
    n = len(chunks)
    pages = []
    for k, chunk in enumerate(chunks):
        img, dr = pg()
        y = 70
        y = M.centered(dr, PW / 2, y, venue.upper(), fam.b(40), INK)
        y = M.centered(dr, PW / 2, y, title if k == 0 else f"{title} (continued)", fam.r(26), accent)
        y += 14
        dr.line([(110, y), (PW - 110, y)], fill=INK, width=2)
        y += 34
        if two_prices:
            dr.text((PW - 110 - M.tl(dr, two_prices[1], fam.b(21)), y - 26), two_prices[1], font=fam.b(21), fill=accent)
            dr.text((PW - 270 - M.tl(dr, two_prices[0], fam.b(21)), y - 26), two_prices[0], font=fam.b(21), fill=accent)
            y += 14
        for name, price, desc in chunk:
            dr.text((110, y), name, font=fam.b(28), fill=INK)
            if two_prices:
                a, b = price.split(" / ")
                dr.text((PW - 270 - M.tl(dr, a, fam.r(27)), y), a, font=fam.r(27), fill=INK)
                dr.text((PW - 110 - M.tl(dr, b, fam.r(27)), y), b, font=fam.r(27), fill=INK)
            else:
                dr.text((PW - 110 - M.tl(dr, price, fam.b(28)), y), price, font=fam.b(28), fill=INK)
            y += 40
            if desc:
                fit(dr, fam.r(22), desc, PW - 300)
                dr.text((110, y - 2), desc, font=fam.r(22), fill=GREY)
                y += 36
            y += 22
        if note and k == n - 1:
            M.centered(dr, PW / 2, y + 12, note, fam.r(24), GREY)
        foot(dr, fam, k + 1, n)
        pages.append(img)
    facts = []
    for name, price, desc in entries:
        p = price.replace(" / ", " small, ", 1) + " large" if two_prices else price
        facts.append((name, f"{p}, {desc}" if desc else p))
    return pages, facts


# ================================================================== the documents
def build_specs():
    S = []                                  # dicts: kind, name, pages, facts, question, intro, bold, proposal, doc_name, split
    F = FAMS

    def add(kind, nn, doc_name, built, question, intro, bold=True, proposal=None, closer=None, famidx=0):
        S.append(dict(kind=kind, nn=nn, doc_name=doc_name, built=built, question=question, intro=intro, bold=bold,
                      proposal=proposal, closer=closer, famidx=famidx))

    # ---------------- syllabus
    add("syllabus", 0, "SOC 140 syllabus.pdf",
        lambda fam: syllabus(fam, "SOC 140: Urban Life", "Fall 2026", "Prof. Imani Okoye", "Thursdays 1:00 PM-3:00 PM",
                             [("Reading quiz", "Sep 18", 10), ("Field notes", "Oct 2", 10), ("Midterm exam", "Oct 23", 25),
                              ("Group project", "Nov 13", 20), ("Lab report", "Nov 20", 10), ("Final exam", "Dec 11", 25)]),
        "List every graded item with its date and weight",
        "Here are all {n} graded items for SOC 140, with their dates and weights:", famidx=7)
    add("syllabus", 1, "scan_204.pdf",
        lambda fam: syllabus(fam, "GEO 220: Weather and Climate", "Fall 2026", "Prof. Tomas Lindqvist", "Tuesdays 3:00 PM-5:00 PM",
                             [("Weekly map quiz 1", "Sep 22", 5), ("Weekly map quiz 2", "Oct 6", 5), ("Station report", "Oct 15", 15),
                              ("Midterm exam", "Oct 29", 20), ("Data journal", "Nov 12", 15), ("Policy brief", "Nov 24", 15), ("Final exam", "Dec 15", 25)],
                             split=4, policy="Late work loses 5 percent per day, up to three days."),
        "What assignments and exams are there in this course, when is each one, and how much is each worth?",
        "This course has {n} graded pieces. Here is each one with its date and weight:", famidx=4)
    add("syllabus", 2, "PSY105 syllabus.pdf",
        lambda fam: syllabus(fam, "PSY 105: Learning and Memory", "Spring 2027", "Prof. Wen-Li Hartmann", "Mondays 11:00 AM-12:30 PM",
                             [("Journal entry 1", "Feb 8", 5), ("Journal entry 2", "Feb 22", 5), ("Memory experiment", "Mar 3", 15),
                              ("Midterm exam", "Mar 17", 20), ("Journal entry 3", "Apr 5", 5), ("Case analysis", "Apr 14", 15),
                              ("Presentation", "Apr 28", 10), ("Final exam", "May 12", 25)]),
        "give me everything that's graded here with the dates and percentages",
        "Here is everything that counts toward the grade in PSY 105, with dates and percentages:", famidx=2)
    add("syllabus", 3, "scan_611.pdf",
        lambda fam: syllabus(fam, "CHM 130: Kitchen Chemistry", "Fall 2026", "Prof. Dario Castellan", "Wednesdays 2:30 PM-4:00 PM",
                             [("Safety quiz", "Sep 16", 5), ("Lab 1 write-up", "Sep 30", 10), ("Lab 2 write-up", "Oct 14", 10), ("Midterm exam", "Oct 28", 20),
                              ("Lab 3 write-up", "Nov 11", 10), ("Recipe project", "Nov 25", 20), ("Final exam", "Dec 9", 25)]),
        "Which graded items are there and what's each worth? Dates too please.",
        "There are {n} graded items in CHM 130. Here they are with their dates and what each is worth:", famidx=0)

    # ---------------- schedule
    add("schedule", 0, "my timetable.pdf",
        lambda fam: schedule(fam, "My Schedule", "Spring semester", ["Course", "Days", "Time", "Room"],
                             [("MATH 118", "Mon/Wed", "9:00-10:15 AM", "Dale 204"), ("ENG 210", "Tue/Thu", "11:00-12:15 PM", "Pruitt 18"),
                              ("BIO 150 Lab", "Fri", "1:00-3:50 PM", "Annex 3"), ("ART 101", "Mon", "2:00-4:45 PM", "Studio B"),
                              ("HIST 205", "Wed", "6:00-8:30 PM", "Pruitt 40"), ("PE 112", "Tue/Thu", "4:00-5:00 PM", "Rec Hall"),
                              ("MUS 100", "Fri", "9:00-10:30 AM", "Hall 2")],
                             note="Add/drop ends Jan 26.", widths=(190, 190, 270, 150)),
        "What's my whole schedule? I need each class with its days, time and room.",
        "Here is your full schedule, {n} classes with their days, times and rooms:", famidx=1)
    add("schedule", 1, "rota scan.pdf",
        lambda fam: schedule(fam, "Front Desk Rota", "Week of November 9", ["Day", "Hours", "Post", "With"],
                             [("Mon, Nov 9", "8:00 AM-4:00 PM", "Front desk", "Priya"), ("Tue, Nov 10", "12:00-8:00 PM", "Returns", "Jonas"),
                              ("Wed, Nov 11", "8:00 AM-4:00 PM", "Front desk", "Priya"), ("Thu, Nov 12", "10:00 AM-6:00 PM", "Phones", "Mei"),
                              ("Fri, Nov 13", "8:00 AM-2:00 PM", "Front desk", "Jonas"), ("Sat, Nov 14", "9:00 AM-1:00 PM", "Returns", "Mei")],
                             note="Swap requests go to the office manager by Friday.", widths=(190, 270, 170, 140)),
        "list every shift for the week with hours, post and who I'm with",
        "Here are all {n} shifts for the week of November 9, with hours, post and who you're with:", famidx=8)
    add("schedule", 2, "conference program.pdf",
        lambda fam: schedule(fam, "Harbor Teaching Day", "Saturday, March 6 program", ["Session", "Time", "Room", "Speaker"],
                             [("Opening remarks", "9:00 AM", "Hall A", "Rosa Beltran"), ("Quiet classrooms", "9:30 AM", "Room 2", "Kenji Aoki"),
                              ("Feedback that lands", "10:30 AM", "Room 4", "Dana Whitlock"), ("Tools for tutoring", "11:30 AM", "Room 2", "Sef Okafor"),
                              ("Lunch panel", "12:30 PM", "Hall A", "Five guests"), ("Teaching with maps", "2:00 PM", "Room 4", "Ana Duarte"),
                              ("Writing across subjects", "3:00 PM", "Room 2", "Leif Brandt"), ("Closing circle", "4:15 PM", "Hall A", "Rosa Beltran")],
                             split=4, widths=(300, 150, 130, 220)),
        "What are all the sessions at this event, with the time, room and speaker for each?",
        "The program has {n} sessions. Here is each one with its time, room and speaker:", famidx=3)
    add("schedule", 3, "clubs flyer.pdf",
        lambda fam: schedule(fam, "After-School Clubs", "Willow Hill Middle School, fall", ["Club", "Day", "Time", "Room"],
                             [("Chess", "Monday", "3:30-4:30 PM", "Room 12"), ("Robotics", "Tuesday", "3:30-5:00 PM", "Lab 1"),
                              ("Garden crew", "Wednesday", "3:30-4:30 PM", "Courtyard"), ("Choir", "Thursday", "3:15-4:15 PM", "Music room"),
                              ("Art studio", "Thursday", "4:30-5:30 PM", "Room 8"), ("Soccer", "Friday", "3:30-5:00 PM", "North field")],
                             widths=(180, 190, 250, 180)),
        "When does each club meet and where?",
        "Here are the {n} clubs, with the day, time and place each one meets:", famidx=9)

    # ---------------- receipt
    add("receipt", 0, "grocery receipt.pdf",
        lambda fam: receipt(fam, "Alder Grocery", "Thu Oct 22  14:22  Register 3",
                            [("Brown rice 2 lb", 1, 4.29), ("Lentils", 1, 2.49), ("Spinach", 1, 3.19), ("Greek yogurt", 1, 5.99),
                             ("Oat milk", 1, 4.19), ("Bananas", 1, 1.87), ("Dish soap", 1, 3.75)], tax=False),
        "What did I buy and what did each cost?",
        "You bought {n} things. Here is each one with its price:", bold=False,
        closer="The total came to ${total:.2f}.", famidx=7)
    add("receipt", 1, "hardware scan.pdf",
        lambda fam: receipt(fam, "Ferncliff Hardware", "Sat Oct 24  10:05  Reg 1",
                            [("Duct tape", 1, 6.49), ("Picture hooks", 2, 4.99), ("Light bulbs 4pk", 1, 8.29), ("Masking tape", 1, 3.79),
                             ("Extension cord", 1, 12.99), ("Screws, box", 3, 5.49)], tail_lines=("Thank you for shopping local", "Returns within 30 days with receipt")),
        "Can you itemize this receipt for me?",
        "Here is the receipt itemized, {n} lines with what each cost:", bold=False,
        closer="With tax, the total was ${total:.2f}.", famidx=5)
    add("receipt", 2, "market receipt 2 pages.pdf",
        lambda fam: receipt(fam, "Tidewater Market", "Sun Nov 1  16:40  Reg 2",
                            [("Sourdough loaf", 1, 6.50), ("Olive oil 500 ml", 1, 11.95), ("Tomatoes 1 lb", 2, 3.40), ("Basil", 1, 2.25),
                             ("Pasta, penne", 2, 2.89), ("Parmesan wedge", 1, 9.75), ("Lemons", 3, 0.75), ("Dark chocolate", 1, 4.60),
                             ("Sparkling water 6pk", 1, 5.99), ("Paper towels", 1, 7.20)], split=6, tail_lines=("Paid by card", "Thank you!")),
        "which things did I get and what was the price of each",
        "Here is everything on the receipt, {n} items and the price of each:", bold=False,
        closer="The total was ${total:.2f}, tax included.", famidx=3)
    add("receipt", 3, "cafe check.pdf",
        lambda fam: receipt(fam, "Lantern Cafe", "Fri Oct 30  12:48  Table 6",
                            [("Veggie wrap", 1, 9.50), ("Tomato soup", 1, 6.25), ("Iced tea", 2, 3.50), ("Lemon bar", 1, 3.75),
                             ("Cold brew", 1, 4.25), ("Side salad", 1, 4.95)], tail_lines=("Tip not included",)),
        "What was on the check and how much was each thing?",
        "Here is everything on the check, {n} lines with the price of each:", bold=False,
        closer="The total with tax was ${total:.2f}.", famidx=6)

    # ---------------- form
    ev1 = dt.date(2026, 11, 21)
    add("form", 0, "registration form.pdf",
        lambda fam: form(fam, "Riverbend Climbing Studio", "Beginner class registration",
                         [("Name", "Priya Raman"), ("Phone", "(503) 555-0144"), ("Date of birth", "April 3, 2001"),
                          ("Class", "Intro to Bouldering"), ("Session", "Sat, Nov 21, 2026, 10:00 AM-12:00 PM"),
                          ("Shoe size", "8"), ("Emergency contact", "Dev Raman, (503) 555-0188"), ("Waiver signed", "Yes")]),
        "What's filled in on this form?",
        "Here is everything filled in on the registration form:",
        proposal={"kind": "event", "title": "Intro to Bouldering", "date": "2026-11-21", "start_time": "10:00", "end_time": "12:00",
                  "location": "Riverbend Climbing Studio", "notes": None}, famidx=1)
    add("form", 1, "new patient form.pdf",
        lambda fam: form(fam, "Cedar Bend Dental", "New patient intake and booking",
                         [("Patient", "Tobias Wren"), ("Phone", "(971) 555-0163"), ("Insurance", "Northwind Plan, ID NW-48213"),
                          ("Reason for visit", "Routine cleaning"), ("Appointment", "Tue, Dec 1, 2026, 2:30 PM"),
                          ("Allergies", "Penicillin"), ("Preferred contact", "Text message")]),
        "go through everything written on this form and list it",
        "Here is everything written on the intake form:",
        proposal={"kind": "event", "title": "Routine cleaning at Cedar Bend Dental", "date": "2026-12-01", "start_time": "14:30", "end_time": None,
                  "location": "Cedar Bend Dental", "notes": None}, famidx=8)
    add("form", 2, "volunteer sheet.pdf",
        lambda fam: form(fam, "Greenway Community Garden", "Volunteer sign-up",
                         [("Name", "Hana Ito"), ("Phone", "(503) 555-0120"), ("Email", "hana.ito@example.org"), ("Availability", "Weekend mornings"),
                          ("Interests", "Composting, seedlings"), ("Experience", "Two seasons at a school garden"), ("Tools of your own", "Gloves and a hand trowel"),
                          ("How you heard of us", "A flyer at the library")]),
        "List every field and what's written in it",
        "Here are the {n} fields on the sign-up form and what is written in each:", famidx=2)
    add("form", 3, "equipment loan.pdf",
        lambda fam: form(fam, "Eastbrook Media Lab", "Equipment loan request",
                         [("Borrower", "Lina Okonkwo"), ("Student ID", "S-204417"), ("Department", "Film Studies"), ("Item", "Compact camera kit"),
                          ("Accessories", "Tripod, two batteries"), ("Checkout", "Mon, Nov 9"), ("Due back", "Mon, Nov 16"),
                          ("Purpose", "Documentary short"), ("Supervisor", "Prof. Aldo Ferreira")], split=5),
        "Please read out the whole form for me, every field.",
        "Here are all {n} fields from the loan request:", famidx=0)

    # ---------------- menu
    add("menu", 0, "brunch menu.pdf",
        lambda fam: menu(fam, "The Green Door", "Weekend brunch",
                         [("Lemon ricotta toast", "$9.00", "Whipped ricotta, lemon zest, honey"), ("Shakshuka", "$12.50", "Eggs poached in spiced tomato"),
                          ("Herb omelette", "$11.00", "Chives, parsley, soft cheese"), ("Buckwheat pancakes", "$10.50", "Maple butter, berries"),
                          ("Granola bowl", "$8.75", "Yogurt, seeds, stewed pear"), ("Smoked trout plate", "$14.00", "Rye, pickled onion, dill")],
                         note="Kitchen closes at 2 PM"),
        "What dishes are on this menu and what do they cost?",
        "Here are all {n} dishes with their prices and what comes with each:", famidx=4)
    add("menu", 1, "coffee board.pdf",
        lambda fam: menu(fam, "Little Kettle Coffee", "Hot drinks",
                         [("Drip coffee", "$2.75 / $3.50", None), ("Americano", "$3.25 / $4.00", None), ("Latte", "$4.25 / $5.00", None),
                          ("Cappuccino", "$4.25 / $5.00", None), ("Flat white", "$4.50 / $5.25", None), ("Chai latte", "$4.50 / $5.25", None),
                          ("Hot chocolate", "$3.95 / $4.75", None)], two_prices=("Small", "Large"), note="Oat and almond milk 50 cents extra"),
        "I want every drink and the prices for both sizes",
        "Here are the {n} drinks with their small and large prices:", famidx=10)
    add("menu", 2, "dinner menu.pdf",
        lambda fam: menu(fam, "Olive & Ash", "Dinner",
                         [("Charred leeks", "$9", "Hazelnut, brown butter"), ("Burrata", "$13", "Roasted grapes, grilled bread"),
                          ("Beet carpaccio", "$11", "Goat curd, pistachio"), ("Wood-fired trout", "$26", "Fennel, salsa verde"),
                          ("Mushroom risotto", "$22", "Thyme, aged parmesan"), ("Braised short rib", "$29", "Polenta, gremolata"),
                          ("Squash gnocchi", "$21", "Sage, toasted seeds")], split=3),
        "Read me the full menu, each dish with its price and what's in it.",
        "Here is the full menu, {n} dishes with prices and what comes with each:", famidx=9)
    add("menu", 3, "bakery prices.pdf",
        lambda fam: menu(fam, "Fernwood Bakery", "Today's bakes",
                         [("Butter croissant", "$3.75", None), ("Almond croissant", "$4.50", None), ("Morning bun", "$4.00", None),
                          ("Seeded rye loaf", "$7.50", None), ("Cardamom knot", "$3.95", None), ("Oat cookie", "$2.25", None),
                          ("Lemon tart", "$5.25", None), ("Focaccia slice", "$3.50", None)], note="Preorders by 6 PM for next morning"),
        "How much is everything here?",
        "Here is everything on the list, {n} items with their prices:", bold=False, famidx=5)
    return S


# ------------------------------------------------------------------ rows
def list_text(facts, bold):
    return "\n".join((f"- **{a}:** {b}" if bold else f"- {a}: {b}") for a, b in facts)


def build_rows():
    rows = []
    for sp in build_specs():
        rid = f"doc5_{sp['kind']}_{sp['nn']:02d}"
        fam = FAMS[sp["famidx"]]
        rng = random.Random(f"{SEED}-{rid}")
        pages, facts, *extra = sp["built"](fam)
        rels = [M.save_jpg(M.scan_finish(pg_, rng), f"{rid}_p{k}.jpg", 1200) for k, pg_ in enumerate(pages)]
        intro = sp["intro"].format(n=len(facts))
        body = intro + "\n" + list_text(facts, sp["bold"])
        if sp["closer"]:
            body += "\n\n" + sp["closer"].format(total=extra[0])
        target = body
        prop = sp["proposal"]
        if prop:
            target = body + "\n\n" + json.dumps({"proposal": prop}, ensure_ascii=False)
        q = sp["question"]
        rows.append({
            "id": rid, "kind": "document", "images": rels,
            "messages": M.real_messages(q, rels, None, None, sp["doc_name"]), "target": target,
            "meta": {"category": f"doc_{sp['kind']}", "proposal": prop is not None, "injection": None, "abstain": False,
                     "unreadable": False, "banned_in_target": [], "qa": [q, body], "scanned_document": sp["doc_name"],
                     "pages": len(rels), "coverage": "doc5", "no_proposal_reason": None,
                     "proposal_policy": "always" if prop else "no_event"}})
    # about 1 in 10 dev: the two rows with the smallest hash, one proposal-free kind each
    h = lambda r: hashlib.sha1(f"{SEED}|dev|{r['id']}".encode()).hexdigest()
    dev = {r["id"] for r in sorted(rows, key=h)[:max(1, round(len(rows) / 10))]}
    for r in rows:
        r["meta"]["split"] = "dev" if r["id"] in dev else "train"
    return rows


def main():
    path = HERE / "vision_rows.jsonl"
    old = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    for p in HERE.glob("images/doc5_*.jpg"):
        p.unlink()
    keep = [r for r in old if not r["id"].startswith("doc5_")]
    new = build_rows()
    rows = keep + new
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # stats: recompute r3's own counts over the full list; keep the other validators and add this file's row checks
    sc_spec = importlib.util.spec_from_file_location("mm_scoring", R3.parent / "mmeval" / "scoring.py")
    sc = importlib.util.module_from_spec(sc_spec)
    sc_spec.loader.exec_module(sc)
    problems = [(r["id"], p) for r in new for p in M.r4_check_row(r, sc)]
    for rid, p in problems:
        print("FAIL r4", rid, p)
    stats = json.loads((HERE / "vision_stats.json").read_text())
    base = stats["validators"].setdefault("problems_before_doc5", stats["validators"]["problems"])
    stats["validators"]["problems"] = base + len(problems)
    stats["round4"] = M.count_stats(rows)
    stats["eligible_rows"] = sum(r["meta"].get("proposal_policy") == "always" for r in rows if r["kind"] != "picture_read")
    stats["doc5"] = {"rows": len(new), "pages": sum(len(r["images"]) for r in new),
                     "by_kind": dict(Counter(r["meta"]["category"] for r in new)),
                     "by_split": dict(Counter(r["meta"]["split"] for r in new)),
                     "proposals": sum(r["meta"]["proposal"] for r in new)}
    (HERE / "vision_stats.json").write_text(json.dumps(stats, indent=1) + "\n")
    print("doc5 rows:", len(new), stats["doc5"], "problems:", len(problems))
    return len(problems)


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
