"""Round-5 family ``all_sources_r5``: a question that spans several of the user's sources reads each of them.

Why. The user's words: "better tool calling... using all of my sources would be better." Of 4,650 training turns in the
r5 build, only 50 read two or more distinct sources (mail, messages, calendar, reminders, notes, memory, web, weather).
The model answers from one source when the question spans several ("anything about the dentist" read mail and stopped).

What this family teaches (about 300 turns, one example per model step):

- ``topic``    a topic across everything ("anything about the dentist", "did anyone mention the trip"): mail, messages, notes
               and calendar read in ONE step because they are independent, with reminders and saved memory added where they fit.
- ``person``   a person across everything ("what's going on with theo"): that person's messages, mail, calendar and reminders.
- ``prep``     getting ready ("prep me for my advisor meeting"): the calendar first; the event names a person and a subject, and
               the second step reads mail, notes and reminders using those names. Two dependent steps, then the answer.
- ``owe``      what is still owed ("do i owe maya anything", "what's still open with the housing form"): reminders, mail
               and messages for that person or topic.
- ``one_hit``  the topic is in ONE source and the others come back empty: the answer uses the hit and does not apologise
               for the rest.
- ``nothing``  the topic or person is nowhere: every named source was read and the answer says it looked there.
- ``conflict`` an email moved a time the calendar still shows: calendar and mail are both read, and the answer names both.
- ``weather``  an outdoor event and the forecast: the calendar and the weather in one step.
- ``single``   the contrast, about a fifth of the rows: "any emails from theo" stays mail only, "what's on my calendar
               tomorrow" stays calendar only, "what's the weather" stays weather, and the same for texts, notes and reminders.
               A combined tool that truly covers the question is ONE call to it: ``get_my_day_plan`` for what is due this week
               and who is waiting, ``get_recent_activity`` for "catch me up". Those are not fanned out into per-app reads.

Tools used (all are in the pinned service; canvas has no production tool, so no row reads it): view_emails, view_messages,
search_notes, get_upcoming, search_reminders, recall, get_weather, get_my_day_plan, get_recent_activity. No write tool.

Every final is ``BASE_WRITES`` (scen_r5_debuglog): base writes the answer later, so the user keeps Gemma's own voice and
formatting. The scripted part is the tool calls and the tool results. ``sc.must_see`` lists a fact per source that hit, which
``scen_r2_act._check`` requires in the tool results, and ``sc.base_ref`` carries ``hit`` (source -> any-of tokens a good
answer states), ``empty`` (sources read that returned nothing) and ``read`` so the later check can tell whether base's answer
used each source and whether it apologised for an empty one.

Everything is synthetic (invented names, dates and contents from the world helpers). The family is gated on ASK_LORA_R5=1 by
scen.py's FAMILIES_R5 auto-registration and seeded from the family name (build.py).
"""
from __future__ import annotations

import scen  # noqa: F401  (first: an import cycle otherwise)

import datetime as dt
import random

import leak
import scen_r2_act as A
import scen_r3_compose as C
import scen_r5_debuglog
import scen_r5_tools as T
from harness import Call, Final
from scen import Scenario
from world import Email, Event, Msg, Note, Reminder

D = dt.timedelta
FAMILY = "all_sources_r5"
WRITE_ALLOW_R5 = {FAMILY: frozenset()}
BASE_WRITES = scen_r5_debuglog.BASE_WRITES
DROPPED: list = []
_SEEN: set = set()

# tool -> the source it reads. The two combined tools read several apps themselves, so a row that calls one is a one-call row.
TOOL_SOURCE = {"view_emails": "mail", "summarize_emails": "mail", "view_messages": "messages", "summarize_messages": "messages",
               "search_notes": "notes", "get_upcoming": "calendar", "search_reminders": "reminders", "recall": "memory",
               "web_search": "web", "get_weather": "weather", "get_my_day_plan": "day_plan", "get_recent_activity": "activity"}
COMBINED = frozenset({"day_plan", "activity"})
QUOTA = {"topic": 24, "person": 20, "prep": 20, "owe": 12, "one_hit": 16, "nothing": 12, "conflict": 10, "weather": 6,
         "single": 28}


# ------------------------------------------------------------------------------------------------ banks
# Topic bundles. mail=(sender, address, subject, body, token); msg=(person key, text, token); note=(title, body, token);
# event=(title, minutes, place, hour, minute); rem=title; mem=(text, token). {day} and {time} are the event's weekday and clock.
# The topic word (kw) appears in every field so each read finds it; each token is distinct inside one bundle.
TOPICS = [
    dict(kw="dentist", say=["the dentist", "my dentist appointment", "the dentist thing"],
         mail=("Brightside Dental", "office@brightsidedental.example", "Dentist cleaning reminder",
               "Hi Sam, your dentist visit with Dr. Ahn is {day} at {time}. Please bring your insurance card.", "Dr. Ahn"),
         msg=("theo", "the dentist ran way behind last time, bring a book", "bring a book"),
         note=("Health stuff", "Dentist: ask about the night guard. Insurance ID is on the card in my wallet.", "night guard"),
         event=("Dentist cleaning", 45, "Brightside Dental", 15, 30), rem="Bring insurance card to the dentist",
         mem=("Sam's dentist charges a fee for cancelling inside 24 hours.", "24 hours")),
    dict(kw="housing", say=["the housing thing", "the housing form", "housing stuff"],
         mail=("Linden Housing", "housing@linden.edu", "Housing renewal form",
               "Your housing renewal form must be signed and returned by {day} at {time}. A $200 deposit is due with it.", "$200 deposit"),
         msg=("maya", "did you sign the housing renewal yet? i'm doing mine tonight", "doing mine tonight"),
         note=("Housing", "Housing renewal: single room is $1,150, double is $890. Ask about parking.", "$890"),
         event=("Housing office appointment", 30, "Hollis Hall 12", 11, 0), rem="Sign the housing renewal form",
         mem=("Sam wants a single room in campus housing if the cost stays under $1,200.", "$1,200")),
    dict(kw="trip", say=["the trip", "the ski trip", "the mountain trip"],
         mail=("Mt. Hood Lodge", "bookings@hoodlodge.example", "Your lodge booking for the ski trip",
               "Booking confirmed for 4 guests. Check-in for the trip is {day} at {time}. The balance of $96 per person is due at check-in.", "$96"),
         msg=("priya", "i can drive for the trip, i have room for 3 and skis", "room for 3"),
         note=("Packing", "Ski trip: goggles, thermal layers, tire chains, lift ticket confirmation.", "tire chains"),
         event=("Ski trip departure", 60, "Linden Gate 4", 7, 30), rem="Rent skis before the trip",
         mem=("On any trip Sam prefers the front seat because of carsickness.", "front seat")),
    dict(kw="passport", say=["my passport", "the passport renewal", "the passport situation"],
         mail=("Passport Services", "noreply@passports.example", "Passport renewal received",
               "We received your passport renewal. Expect delivery in 4 to 6 weeks. Your tracking number ends in 4417.", "4417"),
         msg=("dad", "did the passport photos turn out ok? mom can mail the old one", "mail the old one"),
         note=("Abroad checklist", "Passport: need two photos, old passport, $130 fee. Book the visa appointment once it arrives.", "two photos"),
         event=("Passport photo appointment", 20, "Hollis Library desk", 12, 15), rem="Mail the passport renewal",
         mem=("Sam's passport expires in January.", "expires in January")),
    dict(kw="thesis", say=["the thesis", "my thesis draft", "thesis stuff"],
         mail=("Dr. Lindqvist", "lindqvist@linden.edu", "Thesis chapter 3 comments",
               "Sam, comments on chapter 3 are attached. The main issue is that the methods section needs a clearer baseline. Let's discuss {day} at {time}.", "clearer baseline"),
         msg=("nadia", "are you coming to the thesis writing hour in the library? i saved you a seat", "saved you a seat"),
         note=("Writing", "Thesis: chapter 3 due to Dr. Lindqvist, 12 pages. Still need the results table and the figure captions.", "figure captions"),
         event=("Thesis check-in", 45, "Kerr 301", 14, 0), rem="Finish thesis results table",
         mem=("Sam's thesis topic is energy-efficient scheduling for embedded devices.", "embedded devices")),
    dict(kw="wedding", say=["the wedding", "aisha's wedding", "wedding stuff"],
         mail=("Aisha Karim", "aisha.karim@example.com", "Wedding details",
               "Hi Sam! The wedding is {day} at {time} at Rosewood Garden. The dress code is garden formal.", "garden formal"),
         msg=("carmen", "can we split a ride to the wedding? i'll drive if you pay for gas", "pay for gas"),
         note=("Gifts", "Wedding gift: the registry has a cast iron pan and a picnic blanket. Budget $60.", "cast iron pan"),
         event=("Aisha's wedding", 180, "Rosewood Garden", 16, 0), rem="RSVP for the wedding",
         mem=("Sam's one suit jacket needs dry cleaning before any wedding.", "dry cleaning")),
    dict(kw="internship", say=["the internship", "the internship offer", "internship stuff"],
         mail=("Halcyon Robotics", "careers@halcyon.example", "Internship offer details",
               "Congratulations Sam. Your summer internship offer is $28 an hour for 12 weeks. Please reply by {day} at {time}.", "$28 an hour"),
         msg=("theo", "did you hear back about the internship yet? i'd take it", "i'd take it"),
         note=("Career", "Internship decision: compare Halcyon with the campus lab. Ask about relocation and start date.", "relocation"),
         event=("Internship decision call", 30, "Phone", 13, 0), rem="Reply to the internship offer",
         mem=("Sam wants to stay in Portland for the summer internship if possible.", "stay in Portland")),
    dict(kw="laptop", say=["my laptop", "the laptop repair", "the laptop situation"],
         mail=("Cascade Computer Repair", "service@cascaderepair.example", "Laptop repair quote",
               "The battery swap for your laptop is $119 and takes 2 days. Pick-up is {day} at {time}.", "$119"),
         msg=("maya", "you can borrow my charger until your laptop is fixed", "borrow my charger"),
         note=("Tech", "Laptop: the battery swells when it is plugged in overnight. Back up the project folder before drop-off.", "battery swells"),
         event=("Laptop repair pickup", 15, "Cascade Computer Repair", 17, 0), rem="Back up files before the laptop repair",
         mem=("Sam's laptop is under warranty until next March.", "next March")),
    dict(kw="tax", say=["the tax form", "taxes", "my tax paperwork"],
         mail=("Linden Student Accounts", "accounts@linden.edu", "Your tax form 1098-T is ready",
               "Your 1098-T tax form is available in the student portal. Download it by {day}.", "1098-T"),
         msg=("dad", "did you get the tax form from school? i'll file for the family this weekend", "file for the family"),
         note=("Money", "Tax stuff: tuition credit claim, need the form and last year's return.", "tuition credit"),
         event=("Tax help session", 60, "Student Union 2B", 12, 0), rem="Download the tax form",
         mem=("Sam's dad does the family tax filing each spring.", "each spring")),
    dict(kw="concert", say=["the concert", "concert tickets", "the concert thing"],
         mail=("Aldrich Theater", "tickets@aldrich.example", "Your concert tickets",
               "2 tickets for the concert, section C, doors open {day} at {time}. Total $48.", "section C"),
         msg=("nadia", "i can't make the concert, can you sell my ticket?", "sell my ticket"),
         note=("Weekend", "Concert: meet outside the north entrance, bring earplugs and ID.", "earplugs"),
         event=("Indie concert", 150, "Aldrich Theater", 19, 30), rem="Charge phone before the concert",
         mem=("Sam likes standing near the back at a concert.", "near the back")),
    dict(kw="inspection", say=["the car inspection", "the inspection thing", "my inspection"],
         mail=("Westside Auto", "service@westsideauto.example", "Inspection appointment",
               "Your car inspection is booked {day} at {time}. Expect 45 minutes and bring your registration.", "bring your registration"),
         msg=("dad", "did you book the inspection yet? the sticker expires this month", "sticker expires"),
         note=("Car", "Inspection: tire pressure low on the rear left, wiper blades need replacing.", "wiper blades"),
         event=("Car inspection", 45, "Westside Auto", 10, 0), rem="Find registration papers for the inspection",
         mem=("Sam's car for the inspection is a 2014 Corolla.", "2014 Corolla")),
    dict(kw="scholarship", say=["the scholarship", "the scholarship application", "scholarship stuff"],
         mail=("Linden Financial Aid", "aid@linden.edu", "Scholarship application status",
               "Your scholarship application is missing one recommendation letter. The deadline is {day} at {time}.", "recommendation letter"),
         msg=("priya", "did you ask for the scholarship letter yet? i asked mine yesterday", "asked mine yesterday"),
         note=("Essays", "Scholarship essay: 500 words on a challenge overcome. First draft done, needs a conclusion.", "needs a conclusion"),
         event=("Scholarship interview", 30, "Admissions Office", 14, 30), rem="Submit the scholarship essay",
         mem=("Sam's GPA for the scholarship application is 3.7.", "3.7")),
    dict(kw="parking", say=["the parking permit", "parking", "the parking thing"],
         mail=("Linden Parking Services", "parking@linden.edu", "Parking permit renewal",
               "Your parking permit expires at the end of the month. Renewal is $85 and must be done online by {day}.", "$85"),
         msg=("maya", "parking lot C is closed {day}, use lot B", "lot B"),
         note=("Errands", "Parking: the permit number is on the sticker. Ask about the evening rate.", "evening rate"),
         event=("Parking office hours", 60, "Campus Services", 13, 0), rem="Renew the parking permit",
         mem=("Sam's parking permit is a commuter permit.", "commuter permit")),
    dict(kw="climbing", say=["climbing", "the climbing thing", "the climbing gym"],
         mail=("Granite Gym", "hello@granitegym.example", "Climbing membership renewal",
               "Your climbing membership renews {day} at $59 a month. Reply to pause it.", "$59 a month"),
         msg=("theo", "climbing tonight? the new route is set on the east wall", "east wall"),
         note=("Fitness", "Climbing: work on slab technique. Buy chalk and new shoes before spring.", "slab technique"),
         event=("Climbing session", 90, "Granite Gym", 18, 0), rem="Buy chalk for climbing",
         mem=("Sam climbs at about a V3 level.", "V3")),
]
TOPIC_PH = ["anything about {x}", "what do i have on {x}", "did anyone mention {x}", "what's the latest on {x}",
            "pull up everything on {x}", "what do i know about {x}", "where am i at with {x}", "any news on {x}",
            "check everything for {x}", "what's going on with {x}", "is there anything i should know about {x}"]
OWE_TOPIC_PH = ["what's still open with {x}", "what's left to do for {x}", "what do i still need to do about {x}",
                "am i forgetting anything on {x}"]
CONFLICT_PH = ["did {x} get moved", "what time is {x} now, i think it changed", "any change to the time for {x}?",
               "double check the time on {x} for me", "is {x} still at the time my calendar says", "did {x} change at all"]

# Person bundles. mail=(subject, body, token); msg=(text, token) or None; event=(title, minutes, place, hour, minute);
# rem=title with the first name in it. ``q`` is the search word, ``conv`` the Messages conversation filter.
PEOPLE = [
    dict(key="theo", say=["theo"], q="theo", conv="Theo",
         mail=("Borrowing your tent", "Sam, can I borrow your tent for the {day} trip? I'll wash it after.", "borrow your tent"),
         msg=("can you bring my umbrella {day}? it's still in your car", "umbrella"),
         event=("Lunch with Theo", 60, "Union Cafe", 12, 0), rem="Return Theo's drill"),
    dict(key="maya", say=["maya"], q="maya", conv="Maya",
         mail=("Utilities this month", "Hi Sam, the utilities came to $96 so it's $48 each. Can you send yours by {day}?", "$48"),
         msg=("dinner moved to 7:30 at Luca's, can you still make it?", "7:30"),
         event=("Dinner with Maya at Luca's", 90, "Luca's", 19, 30), rem="Pay Maya for utilities"),
    dict(key="carmen", say=["carmen", "my sister"], q="carmen", conv="Carmen",
         mail=("Mom's birthday plan", "Sam, I'm thinking brunch at Fern on {day} for mom. Can you cover dessert?", "cover dessert"),
         msg=("the tickets are $18 each, i'll pay you back friday", "$18"),
         event=("Brunch with Carmen and Mom", 90, "Fern Cafe", 11, 0), rem="Call Carmen about dessert"),
    dict(key="nadia", say=["nadia"], q="nadia", conv="Nadia",
         mail=("Study group notes", "Hi Sam, attaching the notes from the study group. We're meeting again {day} at {time}.", "study group"),
         msg=("can you cover the library desk on sunday at 2?", "library desk"),
         event=("Study group with Nadia", 90, "Avery 204", 16, 0), rem="Send Nadia the lab notes"),
    dict(key="priya", say=["priya"], q="priya", conv="Priya",
         mail=("Group project outline", "Hi Sam, here's the outline for the group project. I took the intro and the data section.", "data section"),
         msg=("the group project is due monday at noon, i'll send my part tonight", "due monday at noon"),
         event=("Project sync with Priya", 45, "Library 2F", 15, 0), rem="Review Priya's project section"),
    dict(key="okafor", say=["prof okafor", "professor okafor"], q="okafor", conv=None,
         mail=("Project 2 extension", "Sam, I can grant a two-day extension on Project 2. Please confirm by {day}.", "two-day extension"),
         msg=None,
         event=("Office hours with Prof. Okafor", 30, "Kerr 214", 14, 0), rem="Email Prof. Okafor about the extension"),
    dict(key="dad", say=["my dad", "dad"], q="dad", conv="Dad",
         mail=("Train tickets", "Sam, your train is at 9:05 on {day}. I booked the aisle seat.", "aisle seat"),
         msg=("your train is at 9:05 on the 18th, i'll be at the station", "at the station"),
         event=("Train home to see Dad", 120, "Union Station", 9, 5), rem="Call Dad about the train"),
]
PERSON_PH = ["what's going on with {n}", "anything from {n} lately", "catch me up on {n}", "have i heard from {n} at all",
             "anything new with {n}", "what's the latest with {n}", "where do things stand with {n}", "did {n} reach out",
             "what do i have on {n} right now"]
OWE_PERSON_PH = ["do i owe {n} anything", "is there anything i still need to do for {n}", "what did i promise {n}",
                 "am i behind on anything with {n}"]
MISSING_PEOPLE = [("jamie", "Jamie"), ("rowan", "Rowan"), ("elliot", "Elliot"), ("sasha", "Sasha"), ("devon", "Devon")]

# Topics that are nowhere in the world: (word, [spoken forms]).
NOTHING = [("plumber", ["the plumber", "the plumber visit"]), ("hackathon", ["the hackathon", "hackathon stuff"]),
           ("raffle", ["the raffle", "the raffle tickets"]), ("jury", ["jury duty", "the jury notice"]),
           ("storage", ["the storage unit", "storage unit stuff"]), ("bakery", ["the bakery order", "the bakery"]),
           ("yoga", ["yoga class", "the yoga thing"]), ("moving", ["the move", "moving day"]),
           ("graduation", ["graduation", "the graduation party"]), ("sublet", ["the sublet", "subletting my room"]),
           ("vaccine", ["my flu shot", "the vaccine appointment"]), ("garage", ["the garage sale", "the garage thing"])]

# Prep bundles: the event, then what the second step reads. mail=(query, sender, address, subject, body, token);
# note=(query, title, body, token) or None; rem=(query, title) or None; msg=(conversation, person key, text, token) or None.
PREPS = [
    dict(say=["advisor meeting"], q1="advisor", title="Advisor meeting: thesis chapter 3 with Dr. Lindqvist", mins=45, place="Kerr 301", hour=14, minute=30,
         mail=("lindqvist", "Dr. Lindqvist", "lindqvist@linden.edu", "Chapter 3 agenda", "Sam, please bring the revised methods section and your results table.", "revised methods section"),
         note=("thesis", "Writing", "Thesis: chapter 3 is 12 pages. Still need the figure captions.", "figure captions"),
         rem=("thesis", "Finish thesis results table"), msg=None),
    dict(say=["review"], q1="review", title="Design review: robotics demo with Marta Quill", mins=60, place="Engineering 118", hour=10, minute=0,
         mail=("quill", "Marta Quill", "mquill@halcyon.example", "Review agenda", "Hi Sam, for the review please bring the latest demo video and the test results.", "demo video"),
         note=("robotics", "Robotics", "Demo notes: the arm stalls on the third pick. Fix the gripper timing before the review.", "gripper timing"),
         rem=("robotics", "Export the robotics demo video"), msg=None),
    dict(say=["interview"], q1="interview", title="Summer internship interview with Jordan Ellis", mins=45, place="Halcyon, Room 4B", hour=11, minute=0,
         mail=("ellis", "Jordan Ellis", "jellis@halcyon.example", "Interview logistics", "Hi Sam, your interview is in room 4B. Bring a photo ID and a copy of your resume.", "photo ID"),
         note=("interview", "Career", "Interview prep: practice the pointer question and the scheduling project story.", "pointer question"),
         rem=("interview", "Print resume for the interview"), msg=None),
    dict(say=["study group", "stats study group"], q1="study group", title="Statistics study group with Nadia", mins=90, place="Avery 204", hour=16, minute=0,
         mail=("nadia", "Nadia Ortiz", "nadia.ortiz@example.com", "Study group problems", "Hi Sam, please bring problems 4 and 7 from the practice set.", "problems 4 and 7"),
         note=("statistics", "Stats", "Statistics midterm covers regression and hypothesis tests. The formula sheet is in the blue folder.", "hypothesis tests"),
         rem=("statistics", "Finish the statistics practice set"), msg=None),
    dict(say=["dinner", "dinner with maya"], q1="dinner", title="Dinner with Maya at Luca's", mins=90, place="Luca's", hour=19, minute=0,
         mail=("luca", "Luca's Trattoria", "reservations@lucas.example", "Reservation confirmed", "Your table for 4 at Luca's is confirmed. We hold tables for 15 minutes.", "table for 4"),
         note=None, rem=("dinner", "Pick up a dessert for dinner"),
         msg=("Maya", "maya", "i'll be 10 min late, grab the table at Luca's", "10 min late")),
    dict(say=["physical therapy", "therapy appointment"], q1="therapy", title="Physical therapy at Northside Clinic", mins=45, place="Northside Clinic", hour=15, minute=15,
         mail=("northside", "Northside Clinic", "front@northside.example", "Appointment confirmation", "Please arrive 10 minutes early and bring your insurance card and exercise log.", "exercise log"),
         note=("therapy", "Health", "Therapy questions: knee clicks on stairs, ask about running again.", "knee clicks"),
         rem=("therapy", "Print the exercise log for therapy"), msg=None),
    dict(say=["career fair"], q1="career fair", title="Career fair at the Student Union", mins=180, place="Student Union Ballroom", hour=13, minute=0,
         mail=("career", "Linden Career Center", "careers@linden.edu", "Career fair checklist", "Bring 10 copies of your resume and register for employer slots on the portal.", "10 copies"),
         note=("fair", "Career", "Career fair: target three robotics companies and ask about summer roles.", "three robotics companies"),
         rem=("fair", "Print resumes for the fair"), msg=None),
    dict(say=["recital", "carmen's recital"], q1="recital", title="Carmen's recital", mins=120, place="Music Hall", hour=18, minute=30,
         mail=("recital", "Linden Music Dept", "music@linden.edu", "Recital program and seating", "Doors open at 6:30. Seating is first come, first served. Flowers can be left at the lobby desk.", "lobby desk"),
         note=None, rem=("recital", "Buy flowers for the recital"),
         msg=("Carmen", "carmen", "save me a seat, i'm in the second row", "second row")),
    dict(say=["midterm", "chem midterm"], q1="midterm", title="Organic chemistry midterm", mins=90, place="Science 120", hour=9, minute=0,
         mail=("chemistry", "Dr. Hale", "hale@linden.edu", "Chemistry midterm room and rules", "Bring a calculator and two pencils to the chemistry midterm. No notes. Calculators are checked at the door.", "two pencils"),
         note=("chemistry", "Orgo", "Review mechanisms for SN1 and SN2. Practice problems 12 through 20.", "SN1 and SN2"),
         rem=("midterm", "Print the midterm review sheet"), msg=None),
    dict(say=["airport pickup", "pickup at the airport"], q1="airport", title="Airport pickup for Aunt Rosa", mins=60, place="PDX arrivals", hour=17, minute=45,
         mail=("rosa", "Aunt Rosa", "rosa.rivera@example.com", "My flight", "Sam, I land at 5:45 PM at terminal C and I'll have two bags.", "terminal C"),
         note=None, rem=("airport", "Fill the gas tank before the airport pickup"), msg=None),
]
PREP_DAY_PH = ["what do i need for {wd}'s {x}", "prep me for {wd}'s {x}", "getting ready for the {x} on {wd}, what should i have handy",
               "what should i have ready for the {x} {wd}", "{wd}'s {x} is coming up, help me prep"]
PREP_TOMORROW_PH = ["prep me for tomorrow's {x}", "what do i need for the {x} tomorrow", "tomorrow's {x}, what should i bring"]
PREP_PH = ["prep me for my {x}", "what should i know before my {x}", "help me get ready for my {x}", "what do i need for the {x}",
           "anything i should have in hand for my {x}"]

# Outdoor events for the weather shape: (query word, spoken, event title, minutes, place, hour)
OUTDOOR = [("hike", "my hike", "Mt. Tabor hike", 120, "Mt. Tabor trailhead", 9), ("picnic", "the picnic", "Picnic with Theo", 120, "Laurelhurst Park", 12),
           ("soccer", "soccer", "Pickup soccer", 90, "Westmoreland Fields", 18), ("farmers", "the farmers market", "Farmers market run", 60, "PSU Farmers Market", 10),
           ("bike", "my bike ride", "Bike ride along the river", 120, "Waterfront Trail", 8), ("barbecue", "the barbecue", "Barbecue at Nadia's", 180, "Nadia's backyard", 17)]
WEATHER_PH = ["will it rain for {x} {d}", "is the weather ok for {x} {d}", "do i need a jacket for {x} {d}", "how's the weather looking for {x} {d}"]

# Noise: items that belong to no bundle, so a miss is a real miss. (sender, address, subject, body)
NOISE_MAIL = [("Campus News", "news@linden.edu", "This week on campus", "Library hours change over the break and the shuttle runs less often."),
              ("Student Life", "life@linden.edu", "Weekend events", "Open mic night on Friday and a board game afternoon on Sunday."),
              ("Linden Alumni", "alumni@linden.edu", "Alumni newsletter", "Read the stories from this season and see what graduates are up to.")]
NOISE_MSG = [("carmen", "see you sunday, i'll bring the board games"), ("nadia", "the cafe closes at 6 today, just so you know"),
             ("priya", "lol did you see the email from the registrar")]
NOISE_NOTE = [("Grocery list", "Eggs, oat milk, spinach, lentils, bananas."), ("Books", "To read: a sci-fi novel from the library, plus the one maya lent me.")]
NOISE_EVENT = [("Chem lab", 90, "Science 210"), ("Orchestra rehearsal", 90, "Music Hall"), ("Pottery studio", 120, "Arts Annex")]
NOISE_REM = ["Pick up the laundry", "Refill the water filter", "Buy a birthday card"]
NEUTRAL_MEM = ["Sam's major: computer science (sophomore) at Linden University.", "Sam's interests: sci-fi novels and running."]


# ------------------------------------------------------------------------------------------------ plumbing
def _free(text, avoid):
    t = text.lower()
    return not any(a and a.lower() in t for a in avoid)


def add_noise(rng, now, w, avoid):
    """A newsletter, a text, a note, an event, a reminder and a memory that mention none of ``avoid``."""
    avoid = [a for a in avoid if a]
    for s, a, sub, body in rng.sample(NOISE_MAIL, 2):
        if _free(s + sub + body, avoid):
            w.emails.append(Email(s, a, sub, now - D(hours=rng.randint(6, 48)), body, False, False))
    for key, text in rng.sample(NOISE_MSG, 2):
        if _free(text + C.PEOPLE[key][1], avoid):
            w.messages.append(Msg(C.PEOPLE[key][1], C.PEOPLE[key][1], text, now - D(hours=rng.randint(4, 60)), unread=True))
    for title, body in [rng.choice(NOISE_NOTE)]:
        if _free(title + body, avoid):
            w.notes.append(Note(title, body, now - D(days=rng.randint(1, 5), hours=rng.randint(0, 6))))
    title, mins, place = rng.choice(NOISE_EVENT)
    if _free(title + place, avoid):
        w.events.append(Event(title, T.at_(now, rng.choice([0, 6]), rng.choice([9, 14])), mins, place))
    title = rng.choice(NOISE_REM)
    if _free(title, avoid):
        w.reminders.append(Reminder(title, T.at_(now, rng.randint(1, 5), 18)))
    w.memories = [m for m in NEUTRAL_MEM if _free(m, avoid)]


def new_world(rng, avoid=()):
    now, w = T.blank(rng, hour=rng.choice([8, 9, 10, 11]), weekday=rng.choice([0, 1, 2]))
    add_noise(rng, now, w, avoid)
    return now, w


def mk(shape, prompt, now, w, steps, notes, res, *, hit=None, empty=(), read=(), profile=None):
    """hit: source -> any-of tokens a good answer states (the first is also required in the tool results)."""
    s = Scenario(FAMILY, prompt, now, w, steps, f"[{shape}] {notes}", **({} if profile is None else {"profile": profile}))
    s.expect_cards = []
    s.approve = None
    s.results = list(res)
    s.r5cat = shape
    hit = {k: list(v) for k, v in (hit or {}).items()}
    s.must_see = [v[0] for v in hit.values()]
    s.history = None
    s.no_effect = True
    s.thread, s.qkind = None, f"sources_{shape}"
    s.shape = shape
    s.base_ref = dict(kind="all_sources", shape=shape, hit=hit, empty=list(empty), read=list(read), scripted=None)
    return s


def tools_of(sc):
    return [c for st in sc.steps if isinstance(st, list) for c in st]


def sources_of(sc):
    return sorted({TOOL_SOURCE[c.name] for c in tools_of(sc)})


def P(rng, tmpl, **kw):
    return A.style(rng, tmpl.format(**kw))


def ctx_of(start):
    return dict(day=start.strftime("%A"), time=T.tm(start))


def put_topic(rng, now, w, b, src, start, ctx):
    """Add the bundle's ``src`` item to the world; return the any-of tokens a good answer states."""
    if src == "mail":
        s, a, sub, body, tok = b["mail"]
        w.emails.append(Email(s, a, sub, now - D(hours=rng.randint(5, 40)), body.format(**ctx), True, True))
        return [tok]
    if src == "messages":
        key, text, tok = b["msg"]
        name = C.PEOPLE[key][1]
        w.messages.append(Msg(name, name, text.format(**ctx), now - D(hours=rng.randint(3, 50)), unread=True))
        return [tok]
    if src == "notes":
        title, body, tok = b["note"]
        w.notes.append(Note(title, body, now - D(days=rng.randint(1, 4), hours=rng.randint(0, 5))))
        return [tok]
    if src == "calendar":
        title, mins, place, _h, _m = b["event"]
        w.events.append(Event(title, start, mins, place))
        return [title, T.tm(start)]
    if src == "reminders":
        due = None if rng.random() < .3 else T.at_(now, rng.randint(1, 4), 17)
        w.reminders.append(Reminder(b["rem"], due))
        return [b["rem"]]
    text, tok = b["mem"]
    w.memories = list(w.memories) + [text]
    return [tok]


CALL_OF = {"mail": lambda kw: Call("view_emails", query=kw), "messages": lambda kw: Call("view_messages", query=kw),
           "notes": lambda kw: Call("search_notes", query=kw),
           "calendar": lambda kw: Call("get_upcoming", query=kw, days=30, calendar_only=True),
           "reminders": lambda kw: Call("search_reminders", query=kw), "memory": lambda kw: Call("recall", query=kw)}
ORDER = ["mail", "messages", "notes", "calendar", "reminders", "memory"]


def run_calls(w, calls, res):
    for c in calls:
        T.rd(w, res, c.name, **c.args)


def pick_called(rng, has_mem):
    called = ["mail", "messages", "notes", "calendar"]
    if rng.random() < .6:
        called.append("reminders")
    if has_mem and rng.random() < .5:
        called.append("memory")
    return called


def order(rng, srcs):
    srcs = [s for s in ORDER if s in srcs]
    if rng.random() < .3:
        rng.shuffle(srcs)
    return srcs


# ------------------------------------------------------------------------------------------------ shapes
def m_topic(shape, bi, pi, si):
    """A topic across everything. shape: topic (2+ hits) | one_hit (1 hit) | nothing (none)."""
    nothing = shape == "nothing"
    b = None if nothing else TOPICS[bi % len(TOPICS)]
    if nothing:
        kw, say_l = NOTHING[bi % len(NOTHING)]
        b = dict(kw=kw, say=say_l, mem=None)
    tmpl = TOPIC_PH[pi % len(TOPIC_PH)]
    say = b["say"][si % len(b["say"])]

    def make(rng):
        now, w = new_world(rng, [b["kw"]])
        called = pick_called(rng, bool(b.get("mem")) or nothing and rng.random() < .5)
        if shape == "topic":
            present = rng.sample(called, min(len(called), rng.choice([2, 3, 3, 4, 5])))
        elif shape == "one_hit":
            present = [rng.choice(called)]
        else:
            present = []
        k = rng.randint(2, 5)
        start = T.at_(now, k, b["event"][3] if not nothing else 12, b["event"][4] if not nothing else 0)
        ctx = ctx_of(start)
        hit = {s: put_topic(rng, now, w, b, s, start, ctx) for s in present} if not nothing else {}
        calls = [CALL_OF[s](b["kw"]) for s in order(rng, called)]
        res = []
        run_calls(w, calls, res)
        empty = [s for s in called if s not in present]
        return mk(shape, P(rng, tmpl, x=say), now, w, [calls, Final(BASE_WRITES)],
                  {"topic": "a topic across everything: every independent source read in one step, then the answer",
                   "one_hit": "the topic is in one source: all read, the empty ones are not apologised for",
                   "nothing": "the topic is nowhere: all read, the answer says where it looked"}[shape],
                  res, hit=hit, empty=empty, read=called)
    return make


def person_world(rng, p, present, *, with_rem_search):
    now, w = new_world(rng, [p["q"], p["say"][0]])
    k = rng.randint(1, 4)
    start = T.at_(now, k, p["event"][3], p["event"][4])
    ctx = ctx_of(start)
    name, addr = C.PEOPLE[p["key"]][1], C.PEOPLE[p["key"]][3]
    hit = {}
    if "mail" in present:
        sub, body, tok = p["mail"]
        w.emails.append(Email(name, addr, sub, now - D(hours=rng.randint(5, 40)), body.format(**ctx), True, True))
        hit["mail"] = [tok]
    if "messages" in present:
        text, tok = p["msg"]
        w.messages.append(Msg(name, name, text.format(**ctx), now - D(hours=rng.randint(3, 50)), unread=True))
        hit["messages"] = [tok]
    if "calendar" in present:
        title, mins, place, _h, _m = p["event"]
        w.events.append(Event(title, start, mins, place))
        hit["calendar"] = [title, T.tm(start)]
    if "reminders" in present:
        w.reminders.append(Reminder(p["rem"], None if rng.random() < .3 else T.at_(now, rng.randint(1, 4), 17)))
        hit["reminders"] = [p["rem"]]
    # another person's texts and mail sit in the world, so a miss on this one is real
    return now, w, hit


def person_calls(p, name_q, conv):
    return [Call("view_messages", conversation=conv), Call("view_emails", query=name_q),
            Call("get_upcoming", query=name_q, days=14, calendar_only=True), Call("search_reminders", query=name_q)]


def m_person(shape, pi, phi, si):
    """shape person (2+ hits), one_hit/nothing are not used for people except via ``missing``."""
    p = PEOPLE[pi % len(PEOPLE)]
    tmpl = PERSON_PH[phi % len(PERSON_PH)]
    n = p["say"][si % len(p["say"])]

    def make(rng):
        avail = ["mail", "calendar", "reminders"] + (["messages"] if p["msg"] else [])
        present = rng.sample(avail, rng.choice([2, 3, 3, 4]) if len(avail) == 4 else rng.choice([2, 3]))
        now, w, hit = person_world(rng, p, present, with_rem_search=True)
        calls = person_calls(p, p["q"], p["conv"] or p["q"].capitalize())
        res = []
        run_calls(w, calls, res)
        empty = [s for s in ("messages", "mail", "calendar", "reminders") if s not in present]
        return mk("person", P(rng, tmpl, n=n), now, w, [calls, Final(BASE_WRITES)],
                  "a person across everything: their texts, mail, calendar and reminders read in one step",
                  res, hit=hit, empty=empty, read=["messages", "mail", "calendar", "reminders"])
    return make


def m_person_missing(mi, phi):
    q, cap = MISSING_PEOPLE[mi % len(MISSING_PEOPLE)]
    tmpl = PERSON_PH[phi % len(PERSON_PH)]

    def make(rng):
        now, w = new_world(rng, [q])
        calls = [Call("view_messages", conversation=cap), Call("view_emails", query=q),
                 Call("get_upcoming", query=q, days=14, calendar_only=True), Call("search_reminders", query=q)]
        res = []
        run_calls(w, calls, res)
        return mk("nothing", P(rng, tmpl, n=q), now, w, [calls, Final(BASE_WRITES)],
                  "a person who is nowhere: every named source read, the answer says it looked and found nothing",
                  res, hit={}, empty=["messages", "mail", "calendar", "reminders"], read=["messages", "mail", "calendar", "reminders"])
    return make


def m_prep(bi, mode, phi, si):
    p = PREPS[bi % len(PREPS)]
    x = p["say"][si % len(p["say"])]

    def make(rng):
        now, w = new_world(rng, [p["q1"], p["mail"][0], p["title"]])
        k = 1 if mode == "tomorrow" else rng.randint(2, 5)
        start = T.at_(now, k, p["hour"], p["minute"])
        ctx = ctx_of(start)
        wd = start.strftime("%A").lower()
        w.events.append(Event(p["title"], start, p["mins"], p["place"]))
        q, sender, addr, sub, body, tok_mail = p["mail"]
        calls2, hit = [Call("view_emails", query=q)], {}
        keep = lambda: rng.random() < .8  # noqa: E731
        w.emails.append(Email(sender, addr, sub, now - D(hours=rng.randint(5, 40)), body.format(**ctx), True, True))
        hit["mail"] = [tok_mail]
        empty = []
        if p["note"]:
            nq, nt, nb, ntok = p["note"]
            calls2.append(Call("search_notes", query=nq))
            if keep():
                w.notes.append(Note(nt, nb, now - D(days=rng.randint(1, 4))))
                hit["notes"] = [ntok]
            else:
                empty.append("notes")
        if p["rem"]:
            rq, rt = p["rem"]
            calls2.append(Call("search_reminders", query=rq))
            if keep() or len(hit) < 2:
                w.reminders.append(Reminder(rt, T.at_(now, max(k - 1, 0), 17)))
                hit["reminders"] = [rt]
            else:
                empty.append("reminders")
        if p["msg"]:
            conv, key, text, mtok = p["msg"]
            calls2.append(Call("view_messages", conversation=conv))
            name = C.PEOPLE[key][1]
            w.messages.append(Msg(name, name, text, now - D(hours=rng.randint(3, 40)), unread=True))
            hit["messages"] = [mtok]
        if len(hit) < 2:
            return None
        if mode == "weekday":
            first = Call("get_upcoming", period=wd, calendar_only=True)
            prompt = P(rng, PREP_DAY_PH[phi % len(PREP_DAY_PH)], wd=wd, x=x)
        elif mode == "tomorrow":
            first = Call("get_upcoming", period="tomorrow", calendar_only=True)
            prompt = P(rng, PREP_TOMORROW_PH[phi % len(PREP_TOMORROW_PH)], x=x)
        else:
            first = Call("get_upcoming", query=p["q1"], days=14, calendar_only=True)
            prompt = P(rng, PREP_PH[phi % len(PREP_PH)], x=x)
        res = []
        run_calls(w, [first], res)
        run_calls(w, calls2, res)
        hit["calendar"] = [p["title"], T.tm(start)]
        return mk("prep", prompt, now, w, [[first], calls2, Final(BASE_WRITES)],
                  "getting ready: the calendar first, then mail, notes and reminders using the names and subject the event gives",
                  res, hit=hit, empty=empty, read=["calendar"] + [TOOL_SOURCE[c.name] for c in calls2])
    return make


def m_owe_person(pi, phi, si):
    p = PEOPLE[pi % len(PEOPLE)]
    tmpl = OWE_PERSON_PH[phi % len(OWE_PERSON_PH)]
    n = p["say"][si % len(p["say"])]

    def make(rng):
        avail = ["reminders", "mail"] + (["messages"] if p["msg"] else [])
        present = rng.sample(avail, rng.choice([2, 3]) if len(avail) == 3 else 2)
        if "reminders" not in present and rng.random() < .6:
            present[0] = "reminders"
        now, w, hit = person_world(rng, p, present, with_rem_search=True)
        calls = [Call("search_reminders", query=p["q"]), Call("view_emails", query=p["q"]),
                 Call("view_messages", conversation=p["conv"] or p["q"].capitalize())]
        res = []
        run_calls(w, calls, res)
        return mk("owe", P(rng, tmpl, n=n), now, w, [calls, Final(BASE_WRITES)],
                  "what is still owed to a person: reminders, mail and texts read in one step",
                  res, hit=hit, empty=[s for s in ("reminders", "mail", "messages") if s not in present],
                  read=["reminders", "mail", "messages"])
    return make


def m_owe_topic(bi, phi, si):
    b = TOPICS[bi % len(TOPICS)]
    tmpl = OWE_TOPIC_PH[phi % len(OWE_TOPIC_PH)]
    say = b["say"][si % len(b["say"])]

    def make(rng):
        now, w = new_world(rng, [b["kw"]])
        present = rng.sample(["reminders", "mail", "messages"], rng.choice([2, 3]))
        k = rng.randint(2, 5)
        start = T.at_(now, k, b["event"][3], b["event"][4])
        ctx = ctx_of(start)
        hit = {s: put_topic(rng, now, w, b, s, start, ctx) for s in present}
        calls = [CALL_OF["reminders"](b["kw"]), CALL_OF["mail"](b["kw"]), CALL_OF["messages"](b["kw"])]
        res = []
        run_calls(w, calls, res)
        return mk("owe", P(rng, tmpl, x=say), now, w, [calls, Final(BASE_WRITES)],
                  "what is still open on a topic: reminders, mail and texts read in one step",
                  res, hit=hit, empty=[s for s in ("reminders", "mail", "messages") if s not in present],
                  read=["reminders", "mail", "messages"])
    return make


CONFLICT_TOPICS = [0, 1, 2, 6, 7, 8, 9, 10, 11]      # topics a person would ask "did it get moved" about


def m_conflict(bi, phi, si, with_text):
    b = TOPICS[CONFLICT_TOPICS[bi % len(CONFLICT_TOPICS)]]
    tmpl = CONFLICT_PH[phi % len(CONFLICT_PH)]
    say = b["say"][si % len(b["say"])]

    def make(rng):
        now, w = new_world(rng, [b["kw"]])
        k = rng.randint(2, 5)
        old = T.at_(now, k, b["event"][3], b["event"][4])
        new = old + D(minutes=rng.choice([60, 90, 120, -60]))
        title, mins, place, _h, _m = b["event"]
        w.events.append(Event(title, old, mins, place))
        s, a, _sub, _body, _tok = b["mail"]
        kw = b["kw"]
        w.emails.append(Email(s, a, f"Time change for your {kw}", now - D(hours=rng.randint(3, 20)),
                              f"Quick update: your {kw} on {old:%A} has moved from {T.tm(old)} to {T.tm(new)}. Sorry for the change.",
                              True, True))
        calls = [CALL_OF["calendar"](kw), CALL_OF["mail"](kw)]
        hit = {"calendar": [T.tm(old)], "mail": [T.tm(new)]}
        if with_text:
            key = b["msg"][0]
            name = C.PEOPLE[key][1]
            w.messages.append(Msg(name, name, f"heads up, the {kw} got pushed to {T.tm(new)} {old:%A}", now - D(hours=rng.randint(2, 10)), unread=True))
            calls.append(CALL_OF["messages"](kw))
            hit["messages"] = [T.tm(new)]
        res = []
        run_calls(w, calls, res)
        sc = mk("conflict", P(rng, tmpl, x=say), now, w, [calls, Final(BASE_WRITES)],
                "the calendar and an email disagree on a time: both read, the answer names both times",
                res, hit=hit, empty=[], read=[TOOL_SOURCE[c.name] for c in calls])
        sc.base_ref["old"], sc.base_ref["new"] = T.tm(old), T.tm(new)
        return sc
    return make


def m_weather(oi, phi, mode):
    q, spoken, title, mins, place, hour = OUTDOOR[oi % len(OUTDOOR)]
    tmpl = WEATHER_PH[phi % len(WEATHER_PH)]

    def make(rng):
        now, w = new_world(rng, [q])
        k = 1 if mode == "tomorrow" else rng.randint(2, 4)
        start = T.at_(now, k, hour, 0)
        w.events.append(Event(title, start, mins, place))
        calls = [Call("get_upcoming", query=q, days=14, calendar_only=True), Call("get_weather", location="Portland,OR")]
        res = []
        run_calls(w, calls, res)
        d = "tomorrow" if k == 1 else start.strftime("%A").lower()
        return mk("weather", P(rng, tmpl, x=spoken, d=d), now, w, [calls, Final(BASE_WRITES)],
                  "an outdoor event and the forecast: the calendar and the weather in one step",
                  res, hit={"calendar": [title, T.tm(start)], "weather": ["forecast"]}, empty=[], read=["calendar", "weather"])
    return make


# ---- single-source contrasts. The world holds the same topic or person in OTHER sources, so reading them would be tempting.
def full_topic_world(rng, b):
    now, w = new_world(rng, [b["kw"]])
    start = T.at_(now, rng.randint(2, 5), b["event"][3], b["event"][4])
    ctx = ctx_of(start)
    hit = {s: put_topic(rng, now, w, b, s, start, ctx) for s in ORDER}
    return now, w, hit


def full_person_world(rng, p):
    avail = ["mail", "calendar", "reminders"] + (["messages"] if p["msg"] else [])
    now, w, hit = person_world(rng, p, avail, with_rem_search=True)
    return now, w, hit


MAIL_FROM = ["any emails from {n}", "did {n} email me", "check my email for anything from {n}", "has {n} sent me an email"]
MAIL_ABOUT = ["any emails about {x}", "did anyone email me about {x}", "check my inbox for {x}", "is there an email about {x}"]
CAL_PH = ["what's on my calendar {d}", "anything on the calendar {d}", "check my calendar for {d}", "what do i have on the calendar {d}"]
MSG_PH = ["any texts from {n}", "what did {n} text me", "did {n} message me", "check my texts from {n}"]
NOTE_PH = ["what's in my notes about {x}", "check my notes for {x}", "did i write anything down about {x}", "look in my notes for {x}"]
REM_PH = ["do i have a reminder about {x}", "any reminders set for {x}", "is there a reminder for {x}"]
WX_PH = ["what's the weather", "what's the weather like today", "how's the weather out there", "is it cold out", "do i need an umbrella today"]
PLAN_PH = [("what's due this week", "this_week"), ("anything due this week?", "this_week"), ("what deadlines do i have this week", "this_week"),
           ("what do i still owe people", "this_week"), ("who's waiting on me", "this_week"), ("what am i behind on", "this_week"),
           ("who's waiting on me today", "today")]
ACT_PH = ["catch me up", "what did i miss", "anything new", "what came in while i was out"]


def m_single(kind, i):
    def make(rng):
        if kind == "mail_person":
            p = PEOPLE[i % len(PEOPLE)]
            now, w, _h = full_person_world(rng, p)
            call = Call("view_emails", query=p["q"])
            prompt = P(rng, MAIL_FROM[(i // len(PEOPLE)) % len(MAIL_FROM)], n=p["say"][0])
            return one(prompt, now, w, call, "mail", "emails from a person: mail only, though texts and calendar mention them too")
        if kind == "mail_topic":
            b = TOPICS[i % len(TOPICS)]
            now, w, _h = full_topic_world(rng, b)
            prompt = P(rng, MAIL_ABOUT[(i // len(TOPICS)) % len(MAIL_ABOUT)], x=b["say"][0])
            return one(prompt, now, w, CALL_OF["mail"](b["kw"]), "mail", "emails about a topic: mail only")
        if kind == "cal":
            b = TOPICS[i % len(TOPICS)]
            now, w, _h = full_topic_world(rng, b)
            k = rng.randint(1, 4)
            for title, mins, place, _kw in rng.sample(T.EVENTS, 2):
                w.events.append(Event(title, T.at_(now, k, rng.choice([9, 13, 16]), 0), mins, place))
            period = "tomorrow" if k == 1 else T.at_(now, k, 9).strftime("%A").lower()
            d = "tomorrow" if k == 1 else "on " + period
            return one(P(rng, CAL_PH[i % len(CAL_PH)], d=d), now, w, Call("get_upcoming", period=period, calendar_only=True),
                       "calendar", "a calendar question: calendar only, not the day plan or mail")
        if kind == "msg":
            p = [q for q in PEOPLE if q["msg"]][i % 6]
            now, w, _h = full_person_world(rng, p)
            return one(P(rng, MSG_PH[(i // 6) % len(MSG_PH)], n=p["say"][0]), now, w, Call("view_messages", conversation=p["conv"]),
                       "messages", "texts from a person: messages only")
        if kind == "notes":
            b = TOPICS[i % len(TOPICS)]
            now, w, _h = full_topic_world(rng, b)
            return one(P(rng, NOTE_PH[i % len(NOTE_PH)], x=b["say"][0]), now, w, CALL_OF["notes"](b["kw"]),
                       "notes", "a notes question: notes only")
        if kind == "rem":
            b = TOPICS[i % len(TOPICS)]
            now, w, _h = full_topic_world(rng, b)
            return one(P(rng, REM_PH[i % len(REM_PH)], x=b["say"][0]), now, w, CALL_OF["reminders"](b["kw"]),
                       "reminders", "a reminders question: reminders only")
        if kind == "weather":
            now, w = new_world(rng, [])
            w.memories = []
            return one(P(rng, WX_PH[i % len(WX_PH)]), now, w, Call("get_weather", location="Portland,OR"), "weather",
                       "a weather question with a known home city: get_weather only")
        if kind == "plan":
            ask, day = PLAN_PH[i % len(PLAN_PH)]
            now, w = new_world(rng, [])
            for title, mins, place, _kw in rng.sample(T.EVENTS, 2):
                w.events.append(Event(title, T.at_(now, rng.randint(1, 4), rng.choice([10, 14, 16]), 0), mins, place))
            w.reminders.append(Reminder(rng.choice(T.REMS)[0], T.at_(now, 0 if day == "today" else rng.randint(1, 3), 17)))
            return one(ask, now, w, Call("get_my_day_plan", day=day), "day_plan",
                       "the day plan covers due dates and people waiting: one call to it, not a read of each app")
        now, w = new_world(rng, [])
        b = TOPICS[i % len(TOPICS)]
        w.emails.append(Email("Hollis Library", "library@linden.edu", "Overdue notice", now - D(hours=3), "Your book is overdue.", True, True))
        w.messages.append(Msg("Maya Chen", "Maya Chen", "are you home for dinner?", now - D(hours=2), unread=True))
        return one(ACT_PH[i % len(ACT_PH)], now, w, Call("get_recent_activity"), "activity",
                   "what's new across apps: the combined activity tool in one call")
    return make


def one(prompt, now, w, call, src, notes):
    res = []
    run_calls(w, [call], res)
    return mk("single", prompt, now, w, [[call], Final(BASE_WRITES)], notes, res, hit={}, empty=[], read=[src])


# ------------------------------------------------------------------------------------------------ family
def _try(rng, make, label):
    last = None
    for _ in range(4):
        seed = rng.random()
        try:
            sc = make(random.Random(seed))
        except Exception as exc:  # noqa: BLE001
            last = f"build {exc!r:.140}"
            continue
        if sc is None:
            last = "builder gave up"
            continue
        why = T.vet(sc)
        if not why and sc.prompt.strip().lower() in _SEEN:
            why = "duplicate prompt"
        if not why and leak.fold(sc.prompt) in scen_r5_debuglog.heldout_folds():
            why = "a held-out wording"
        if why:
            last = why
            continue
        _SEEN.add(sc.prompt.strip().lower())
        return make(random.Random(seed))
    DROPPED.append((label, last))
    return None


def specs():
    """Shape -> candidate makers, more than the quota so a dropped one is replaced."""
    out = {s: [] for s in QUOTA}
    nT, nP = len(TOPICS), len(PEOPLE)
    for n in range(36):
        out["topic"].append(m_topic("topic", n, n * 3 + n // nT, n // nT))
    for n in range(28):
        out["person"].append(m_person("person", n, n * 2 + n // nP, n // nP))
    for n in range(34):
        mode = ("tomorrow", "weekday", "weekday", "nodate", "nodate")[n % 5]
        out["prep"].append(m_prep(n, mode, n // len(PREPS) + n, n // len(PREPS)))
    for n in range(10):
        out["owe"].append(m_owe_person(n, n // nP, n // nP))
        out["owe"].append(m_owe_topic(n * 2 + 1, n // 2, n // 3))
    for n in range(24):
        out["one_hit"].append(m_topic("one_hit", n, n * 2 + 1, n // nT + 1))
    for n in range(14):
        out["nothing"].append(m_topic("nothing", n, n * 2, n // len(NOTHING)))
        if n < 5:
            out["nothing"].append(m_person_missing(n, n * 2 + 1))
    for n in range(16):
        out["conflict"].append(m_conflict(n, n, n // len(CONFLICT_TOPICS), n % 4 == 3))
    for n in range(12):
        out["weather"].append(m_weather(n, n, "tomorrow" if n % 3 == 0 else "weekday"))
    kinds = ["mail_person", "mail_topic", "cal", "msg", "notes", "rem", "weather", "plan", "activity"]
    wanted = {"mail_person": 3, "mail_topic": 3, "cal": 4, "msg": 4, "notes": 3, "rem": 2, "weather": 3, "plan": 5, "activity": 3}
    for kind in kinds:
        for n in range(wanted[kind] + 2):
            out["single"].append((kind, m_single(kind, n), wanted[kind]))
    return out


def fam_all_sources_r5(rng):
    _SEEN.clear()
    DROPPED.clear()
    plan = specs()
    out = []
    for shape in ("topic", "person", "prep", "owe", "one_hit", "nothing", "conflict", "weather"):
        kept = []
        for i, make in enumerate(plan[shape]):
            if len(kept) >= QUOTA[shape]:
                break
            sc = _try(rng, make, f"{shape}#{i}")
            if sc is not None:
                kept.append(sc)
        out += kept
    per_kind: dict = {}
    for i, (kind, make, want) in enumerate(plan["single"]):
        if per_kind.get(kind, 0) >= want:
            continue
        sc = _try(rng, make, f"single:{kind}#{i}")
        if sc is not None:
            per_kind[kind] = per_kind.get(kind, 0) + 1
            out.append(sc)
    return out


FAMILIES_R5 = {FAMILY: fam_all_sources_r5}
