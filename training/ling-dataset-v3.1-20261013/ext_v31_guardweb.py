"""V3.1 guard / web-loop families (category "guardweb").

V3 eval failures fixed here:
  (a) a read whose result does NOT answer the question was repeated verbatim (recall("book") x7) even after Wisp's
      repeated-read guard said "NOT run again";
  (b) web_search <-> web_fetch ping-pong until the round cap for simple lookups;
  (c) single status / compute reads (currency, battery, weather, air quality) re-called instead of answering.

Targets:
  gd_irrelevant_result  - unhelpful result -> guarded identical repeat (context only) -> honest "not found" final
  gd_irrelevant_switch  - unhelpful result -> guarded repeat -> a DIFFERENT call (other tool / genuinely new arguments)
                          that succeeds -> final
  web_bounded           - web_search -> (at most) ONE web_fetch -> cited final; ~40% answer from the snippets alone
  stp_status_once       - one status / compute read -> final immediately (web tools)
  stp_status_local      - same, device / compute tools (no web catalog)
Only non-volatile reads are ever guarded (Wisp's _VOLATILE_READS never are). Prompts are fresh wording."""

import re

CAT = "guardweb"
GUARD_FAIL = ("repeated_read_guard",)
WEB_FAIL = ("web_loop",)
STP_FAIL = ("status_reread_loop",)

_NAMES = ("family", "messy", "first", "dshort", "t12", "dt", "make_event", "agenda_result", "weather_result", "CITIES",
          "_msgid", "dw", "pick_day")


def register(G):
    globals().update({n: getattr(G, n) for n in _NAMES})

    def _mail(sender, subj, body, clock="9:20 AM"):
        return (f"1 email:\nFrom: {sender} <{G.email_for(sender)}>\nDate: {{d}}, {clock}\nSubject: {subj}\n"
                f"Message-ID: {{id}}\nBody: {body}")

    def _fill(r, now, text):
        d = dshort(now.date() - dt.timedelta(days=r.randint(2, 30)))
        return text.replace("{d}", d).replace("{id}", _msgid(r))

    # ------------------------------------------------------------------ 1. irrelevant result -> guard -> honest final
    # (users, tool, family, args, result, finals); "{d}" is a past date, "{id}" a message id.
    IRR = [
        (["what shoe size do I wear again", "remind me what size shoes I take"], "recall", "memory", {"query": "shoe size"},
         "Memory 14: User bikes to work on Tuesdays and Thursdays.",
         ["I don't have your shoe size saved. The only memory that came up is about biking to work on Tuesdays and Thursdays, which doesn't help. If you tell me the size I can remember it.",
          "Nothing stored about your shoe size - the one memory I found says you bike to work on Tuesdays and Thursdays. Want to give me the size so I can save it?"]),
        (["what's Nadia's favourite flower", "which flowers does my sister like best"], "recall", "memory", {"query": "sister favourite flower"},
         "Memory 6: User is allergic to shellfish.",
         ["I don't have anything saved about her favourite flower. The only memory that came up is your shellfish allergy, which is unrelated.",
          "That isn't something I have on file - the only thing recall turned up was your shellfish allergy."]),
        (["who is my landlord", "what's my landlord's name"], "recall", "memory", {"query": "landlord"},
         "Memory 22: User's sister Nadia lives in Seattle.",
         ["I don't have your landlord saved anywhere. The only memory that matched was that your sister Nadia lives in Seattle, which isn't related. Check your lease or old emails, or tell me and I'll remember it.",
          "No landlord on file. All I found was a memory that your sister Nadia lives in Seattle - not what you asked."]),
        (["what do I usually order at the coffee place", "what's my usual coffee order"], "recall", "memory", {"query": "usual coffee order"},
         "2 memories:\nMemory 3: User drives a green hatchback.\nMemory 11: User goes to the gym Monday, Wednesday and Friday mornings.",
         ["I don't have a coffee order saved. The two memories that came up are about your green hatchback and your gym mornings, so neither answers it.",
          "Nothing about your usual coffee order is stored - just your car and your gym schedule. Tell me the order and I can remember it for next time."]),
        (["what's the code for the gate, it's somewhere in my notes", "check my notes for the gate code"], "search_notes", "notes",
         {"query": "gate code"}, "1 note: Garden to-do\nprune roses, order mulch, fix the gate latch",
         ["The only note that matched is “Garden to-do”, and it just mentions fixing the gate latch - there's no gate code in it. It may not be written down in your notes.",
          "I didn't find a gate code. The one note that came up, “Garden to-do”, only says to fix the gate latch."]),
        (["what did I jot down about the wedding budget", "find my wedding budget notes"], "search_notes", "notes",
         {"query": "wedding budget"}, "1 note: Birthday party plan\nSaturday 6 PM at 14 Alder Lane, bring cake",
         ["I don't see any wedding budget notes. The only match was “Birthday party plan” (Saturday 6 PM at 14 Alder Lane, bring cake), which isn't about a wedding.",
          "No note about a wedding budget. “Birthday party plan” was the only thing that matched, and it's about a party on Alder Lane."]),
        (["when does my passport expire according to my notes", "does a note of mine say when my passport runs out"], "search_notes", "notes",
         {"query": "passport expiry"}, "1 note: Packing list - Lisbon trip\npassport, charger, rain jacket, sunscreen",
         ["Your notes don't say. The only match, “Packing list - Lisbon trip”, just lists a passport as something to pack, with no expiry date. The date is printed inside the passport itself.",
          "I couldn't find an expiry date. The one note that mentions your passport is the Lisbon packing list, and it only says to bring it."]),
        (["pull up what I wrote about the offsite", "is there a note on the team offsite"], "search_notes", "notes",
         {"query": "offsite"}, "1 note: Project Falcon notes\nKickoff done. Next: vendor shortlist by Friday",
         ["There's no offsite note. The only hit was “Project Falcon notes” (kickoff done, vendor shortlist due Friday), which doesn't mention an offsite.",
          "I don't have anything on an offsite in your notes - just the Project Falcon notes about the vendor shortlist."]),
        (["what did we decide about the vacation dates", "did we ever settle on dates for the holiday"], "search_conversations", "memory",
         {"query": "vacation dates"}, "1 earlier conversation matched:\n- {d}: you asked how long to roast a whole chicken",
         ["I can't find that. The only earlier conversation that came up was about roasting a chicken, so no vacation dates were settled in our chats.",
          "Nothing in our past conversations covers vacation dates - the one match was about roasting a chicken."]),
        (["did I ever ask you for a plumber recommendation", "have we talked about finding a plumber before"], "search_conversations", "memory",
         {"query": "plumber"}, "1 earlier conversation matched:\n- {d}: you asked for the weather in Denver",
         ["Not that I can find. The only earlier conversation that matched was about the weather in Denver, not a plumber.",
          "I don't see a plumber conversation. The single match was a weather question for Denver."]),
        (["when am I supposed to renew the car registration", "is there a reminder for my vehicle registration"], "search_reminders", "calendar",
         {"query": "car registration", "scope": "all"}, "1 active reminder: renew passport (due {d}, 9 AM)",
         ["You don't have a reminder about your car registration. The only active reminder is “renew passport”. Want me to add one for the registration?",
          "No registration reminder is set. The one reminder that came up is “renew passport”. I can create one for the car if you tell me when."]),
        (["do I have a reminder to book the vet", "is there a vet reminder anywhere"], "search_reminders", "calendar",
         {"query": "vet", "scope": "all"}, "1 active reminder: call the bank (due {d}, 10 AM)",
         ["No vet reminder. The only active reminder is “call the bank”.",
          "There isn't one about the vet - the sole active reminder is “call the bank”. Want me to set one up?"]),
        (["find the email confirming my flight to Chicago", "dig up my Chicago flight confirmation email"], "view_emails", "mail",
         {"query": "flight confirmation Chicago", "count": 3},
         _mail("Elena Petrov", "Team lunch", "Are we still doing Thursday at noon?"),
         ["I couldn't find a flight confirmation. The closest email that came up was Elena Petrov's “Team lunch” on {d}, which has nothing to do with a flight. It might be in another folder or under a sender I'm not matching.",
          "No Chicago flight confirmation turned up. The only email returned was Elena Petrov's “Team lunch” ({d}), which isn't about travel."]),
        (["did the bike shop send me a receipt", "look for an emailed receipt from the bike shop"], "view_emails", "mail",
         {"query": "bike shop receipt", "count": 3},
         _mail("Marcus Bell", "Contract draft", "Sharing the draft for your thoughts.", "2:05 PM"),
         ["I don't see a bike shop receipt. The only email returned was Marcus Bell's “Contract draft” from {d}, which is unrelated.",
          "No receipt from the bike shop turned up. The closest thing was Marcus Bell's “Contract draft”, and that isn't a receipt."]),
        (["where did I save my lease agreement", "which file is my signed lease"], "find_files", "files",
         {"query": "lease agreement", "kind": "pdf"}, "1 match:\n~/Downloads/lease-parking-addendum.pdf (modified {d})",
         ["The only match is lease-parking-addendum.pdf in Downloads, which looks like a parking addendum rather than the lease itself. The lease may be saved under another name.",
          "I didn't find the lease. The one file that matched is ~/Downloads/lease-parking-addendum.pdf, just an addendum about parking."]),
        (["find my latest CV", "where's my curriculum vitae saved"], "find_files", "files",
         {"query": "cv", "kind": "document"}, "1 match:\n~/Documents/cv-reading-list.txt (modified {d})",
         ["The only match is cv-reading-list.txt in Documents, which is a reading list rather than a CV. Your CV might be saved under a different name.",
          "I couldn't find a CV. The one file that came up, cv-reading-list.txt, isn't a CV."]),
    ]

    IRR_DEV = [
        (["do you happen to know my blood type"], "recall", "memory", {"query": "blood type"},
         "Memory 5: User's sister Nadia lives in Seattle.",
         ["Nothing about your blood type is stored with me - the only memory that matched was that your sister Nadia lives in Seattle."]),
        (["locate whatever I noted regarding the garage door opener"], "search_notes", "notes", {"query": "garage door opener"},
         "1 note: Moving checklist\nbox books, cancel internet, forward mail",
         ["There's no garage door note. The one result, “Moving checklist”, covers boxing books, cancelling internet and forwarding mail."]),
        (["I need the receipt from the pet store emailed to me"], "view_emails", "mail", {"query": "pet store receipt", "count": 3},
         _mail("Hana Sato", "Photos from the trip", "Sharing the album from last weekend.", "4:40 PM"),
         ["No pet store receipt turned up. The only email returned was Hana Sato's “Photos from the trip” on {d}, which isn't a receipt."]),
        (["track down the scan of my insurance card", "I'm after the photo of my insurance card"], "find_files", "files",
         {"query": "insurance card", "kind": "image"}, "1 match:\n~/Pictures/card-game-night.png (modified {d})",
         ["I didn't find an insurance card scan. The only image that matched was card-game-night.png, which is something else."]),
        (["which street did I say my cousin lives on", "remind me where my cousin Priya is based"], "recall", "memory", {"query": "cousin address"},
         "Memory 17: User takes the 7:40 train on weekdays.",
         ["I have nothing saved about where your cousin lives - the one memory that came up is about your 7:40 weekday train."]),
        (["is there a reminder about the boiler service", "have I set anything for servicing the boiler"], "search_reminders", "calendar",
         {"query": "boiler service", "scope": "all"}, "1 active reminder: order printer ink (due {d}, 11 AM)",
         ["No boiler reminder exists. The only active one is “order printer ink”, due {d}."]),
        (["I wrote something about the thesis deadline somewhere, can you find it"], "search_notes", "notes", {"query": "thesis deadline"},
         "1 note: Apartment search\nShortlist: Maple St 2BR, Harbor View studio",
         ["I couldn't find a thesis note. The only match, “Apartment search”, lists Maple St 2BR and Harbor View studio, so nothing about a deadline."]),
    ]

    def _date_in(res, now):
        """The date already embedded in a tool result (keeps the final consistent with it)."""
        m = re.search(r"((?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day, [A-Z][a-z]+ \d{1,2})", res)
        return m.group(1) if m else dshort(now.date() - dt.timedelta(days=3))

    def _irr(r, c, now, bank, off):
        sc = r.choice(bank)
        users, tool, fam, args, result, finals = sc
        i_user = r.randrange(len(users))
        c.user(messy(r, users[i_user]))
        c.discover([fam], [tool])
        res = _fill(r, now, result)
        c.call(tool, args, res)
        c.repeat_read(tool, args)
        c.final(r.choice(finals).replace("{d}", _date_in(res, now)))
        return {"template": off + bank.index(sc) * 4 + i_user, "slots": {"tool": tool}}

    @family(CAT, "gd_irrelevant_result", weight=14, failure=GUARD_FAIL + ("unhelpful_result_repeat",))
    def f_gd_irrelevant_result(r, c, now):
        return _irr(r, c, now, IRR, 0)

    @family(CAT, "gd_dev_irrelevant", split="dev", weight=3, failure=GUARD_FAIL + ("unhelpful_result_repeat",))
    def f_gd_dev_irrelevant(r, c, now):
        if r.random() < 0.5:
            return _irr(r, c, now, IRR_DEV, 500)
        return _switch(r, c, now, SW_DEV, 500)

    # ------------------------------------------------------------------ 2. irrelevant result -> guard -> a DIFFERENT call
    # (users, (tool1, fam1, args1, result1), (tool2, fam2, args2, result2), finals)
    SW = [
        (["what's the name of the tailor my sister recommended", "who was the tailor Nadia told me about"],
         ("search_notes", "notes", {"query": "tailor"}, "1 note: Gift ideas for Mom\nscarf, tea sampler, a framed photo"),
         ("recall", "memory", {"query": "tailor sister recommended"}, "Memory 31: Nadia recommended a tailor, Mr. Okafor, on Pine Street."),
         ["Nadia recommended Mr. Okafor, a tailor on Pine Street.", "That's Mr. Okafor on Pine Street - I had it saved from when Nadia recommended him."]),
        (["which pizza place do we like", "what's our go-to pizza spot"],
         ("recall", "memory", {"query": "favourite pizza place"}, "Memory 9: User goes to the gym Monday, Wednesday and Friday mornings."),
         ("search_notes", "notes", {"query": "pizza"}, "1 note: Friday dinner spots\nPizza: Luigi's on 5th. Tacos: Casa Verde."),
         ["Your note “Friday dinner spots” lists Luigi's on 5th for pizza.", "Luigi's on 5th - it's the pizza place in your Friday dinner spots note."]),
        (["did the airline email me a boarding pass", "has my boarding pass come through by email"],
         ("view_emails", "mail", {"query": "boarding pass", "count": 3},
          _mail("Dev Malhotra", "Conference travel", "Can you share your arrival time?", "11:15 AM")),
         ("view_emails", "mail", {"query": "airline check-in", "period": "this week", "strict_match": True},
          _mail("Skyline Air", "Your boarding pass for SK 214", "Check-in is open. Your boarding pass is attached.", "6:02 AM")),
         ["Yes - Skyline Air sent “Your boarding pass for SK 214” on {d}. Check-in is open and the pass is attached.",
          "It did arrive: Skyline Air's email “Your boarding pass for SK 214” ({d}) says check-in is open with the pass attached."]),
        (["where's the PDF of my car insurance policy", "find my auto insurance policy document"],
         ("find_files", "files", {"query": "car insurance", "kind": "pdf"}, "1 match:\n~/Downloads/insurance-claim-form-blank.pdf (modified {d})"),
         ("find_files", "files", {"query": "policy", "folder": "~/Documents", "kind": "pdf"}, "1 match:\n~/Documents/Auto/Policy_2026.pdf (modified {d})"),
         ["Found it: ~/Documents/Auto/Policy_2026.pdf.", "Your policy is at ~/Documents/Auto/Policy_2026.pdf. The first search only turned up a blank claim form."]),
        (["what did I want to ask the contractor", "remind me what questions I had for the contractor"],
         ("search_notes", "notes", {"query": "contractor"}, "1 note: Garden to-do\nprune roses, order mulch, fix the gate latch"),
         ("recall", "memory", {"query": "questions for the contractor"}, "Memory 40: User wants to ask the contractor about the permit and the tile delivery date."),
         ["You wanted to ask about the permit and the tile delivery date.", "Two things: the permit, and when the tile will be delivered."]),
        (["what was that podcast you suggested a while back", "which podcast did you recommend before"],
         ("search_conversations", "memory", {"query": "podcast"}, "1 earlier conversation matched:\n- {d}: you asked how to remove a red wine stain"),
         ("search_notes", "notes", {"query": "podcast"}, "1 note: Listening list\nPodcast: Slow Burn Cities. Audiobook: The Overstory."),
         ["The podcast on your Listening list note is Slow Burn Cities.", "Your “Listening list” note has Slow Burn Cities as the podcast."]),
        (["which restaurant did we choose for Dana's birthday dinner", "where are we eating for Dana's birthday"],
         ("recall", "memory", {"query": "restaurant Dana birthday"}, "Memory 8: User's sister Nadia lives in Seattle."),
         ("search_conversations", "memory", {"query": "Dana birthday dinner"}, "1 earlier conversation matched:\n- {d}: settled on Harbor Grill for Dana's birthday dinner, Saturday 7:30 PM"),
         ["You settled on Harbor Grill, Saturday at 7:30 PM.", "Harbor Grill on Saturday at 7:30 PM, according to the conversation where you picked it."]),
        (["what do I need to pack for camping", "what's on my camping checklist"],
         ("search_reminders", "calendar", {"query": "camping", "scope": "all"}, "1 active reminder: charge the camera (due {d}, 7 PM)"),
         ("search_notes", "notes", {"query": "camping"}, "1 note: Camping gear\ntent, headlamp, stove fuel, water filter"),
         ["Your “Camping gear” note lists a tent, headlamp, stove fuel and a water filter.", "From your Camping gear note: tent, headlamp, stove fuel, water filter."]),
        (["did Hana send me the address of the venue", "has Hana told me where the venue is"],
         ("view_emails", "mail", {"query": "venue address", "count": 3}, _mail("Zoe Tanaka", "Parent-teacher night", "Doors open at 6 PM.", "1:30 PM")),
         ("view_messages", "messages_contacts", {"query": "venue", "count": 5}, "Messages matching 'venue':\nHana (3:12 PM): The venue is 88 Mill Road, next to the old cinema."),
         ["Yes - Hana texted that the venue is 88 Mill Road, next to the old cinema.", "Hana messaged it: 88 Mill Road, next to the old cinema."]),
        (["which document says how long the dishwasher warranty is", "find where the dishwasher warranty period is written"],
         ("find_files", "files", {"query": "dishwasher warranty"}, "1 match:\n~/Downloads/dishwasher-manual.pdf (modified {d})"),
         ("find_files", "files", {"query": "warranty period", "folder": "~/Documents", "content": True}, "1 match:\n~/Documents/Appliances/receipts.docx (modified {d})"),
         ["Searching inside your files, the warranty period shows up in ~/Documents/Appliances/receipts.docx.",
          "It's mentioned in ~/Documents/Appliances/receipts.docx - the first search only found the dishwasher manual by name."]),
        (["do I have a reminder about the trip, and when is the flight", "when's the flight out, I think I set a reminder"],
         ("search_reminders", "calendar", {"query": "flight", "scope": "all"}, "1 active reminder: charge the camera (due {d}, 7 PM)"),
         ("get_upcoming", "calendar", {"days": 30, "query": "flight"}, "CAL"),
         ["CAL"]),
    ]
    SW_DEV = [
        (["help me remember the name of the dentist Nadia uses"],
         ("search_notes", "notes", {"query": "dentist"}, "1 note: Moving checklist\nbox books, cancel internet, forward mail"),
         ("recall", "memory", {"query": "dentist Nadia uses"}, "Memory 27: Nadia sees Dr. Lindqvist on Cedar Avenue."),
         ["Nadia goes to Dr. Lindqvist on Cedar Avenue."]),
        (["could you retrieve the invoice from Noor"],
         ("view_emails", "mail", {"query": "invoice", "count": 3}, _mail("Luca Romano", "Quarterly numbers", "Numbers attached for review.", "10:10 AM")),
         ("view_emails", "mail", {"query": "Noor", "strict_match": True, "count": 3},
          _mail("Noor Haddad", "Invoice 2048", "Invoice 2048 attached. Payment due in 14 days.", "8:45 AM")),
         ["Noor Haddad sent “Invoice 2048” on {d}; payment is due in 14 days."]),
        (["what did we say about the book club date a while ago", "bring up our earlier chat on the book club meeting"],
         ("search_conversations", "memory", {"query": "book club"}, "1 earlier conversation matched:\n- {d}: you asked how to reset a home router"),
         ("search_notes", "notes", {"query": "book club"}, "1 note: Book club\nNext meeting: Thursday the 22nd at 7 PM, Mia hosting."),
         ["Your Book club note says the next meeting is Thursday the 22nd at 7 PM, with Mia hosting."]),
        (["whats my usual dentist again", "who do I see for dental checkups"],
         ("recall", "memory", {"query": "usual dentist"}, "Memory 12: User drives a green hatchback."),
         ("search_conversations", "memory", {"query": "dentist"}, "1 earlier conversation matched:\n- {d}: booked a cleaning with Dr. Alvarez at Brook Dental"),
         ["Dr. Alvarez at Brook Dental - that's who you booked your cleaning with."]),
        (["what file has my notes from the kickoff call", "which saved document holds the kickoff call minutes"],
         ("find_files", "files", {"query": "kickoff call"}, "1 match:\n~/Music/kickoff-playlist.m3u (modified {d})"),
         ("find_files", "files", {"query": "kickoff", "folder": "~/Documents", "kind": "document"}, "1 match:\n~/Documents/Falcon/kickoff-minutes.docx (modified {d})"),
         ["The minutes are in ~/Documents/Falcon/kickoff-minutes.docx."]),
    ]

    def _cal_switch(r, c, now, dev):
        ev = make_event(r, now.date() + dt.timedelta(days=r.randint(2, 20)), h=r.randint(9, 17), m=r.choice([0, 30]))
        users = [f"do I have a {ev.alias} coming up, I thought I set a reminder for it", f"when's my {ev.alias}, I may have a reminder set"]
        i_user = r.randrange(len(users))
        c.user(messy(r, users[i_user], protect=(ev.alias,)))
        c.discover(["calendar"], ["search_reminders"])
        a1 = {"query": ev.key, "scope": "all"}
        c.call("search_reminders", a1, f"1 active reminder: charge the camera (due {dshort(now.date() + dt.timedelta(days=1))}, 7 PM)")
        c.repeat_read("search_reminders", a1)
        c.discover(["calendar"], ["get_upcoming"])
        a2 = {"days": 30, "query": ev.key}
        c.call("get_upcoming", a2, agenda_result(now, "next 30 day(s)", [ev]))
        c.final(f"Yes - your {ev.alias} is on {dshort(ev.date)} at {ev.tstr}" + (f" ({ev.loc})." if ev.loc else ".")
                + " It's on the calendar rather than in your reminders.")
        return {"template": 900 + i_user, "slots": {"kind": "cal"}}

    def _switch(r, c, now, bank, off):
        sc = r.choice(bank)
        users, s1, s2, finals = sc
        if s2[3] == "CAL":
            return _cal_switch(r, c, now, off > 0)
        i_user = r.randrange(len(users))
        c.user(messy(r, users[i_user]))
        t1, f1, a1, res1 = s1
        t2, f2, a2, res2 = s2
        assert (t1, a1) != (t2, a2)
        c.discover([f1], [t1])
        res1 = _fill(r, now, res1)
        c.call(t1, a1, res1)
        c.repeat_read(t1, a1)
        if t2 != t1:
            c.discover([f2], [t2])
        res2 = _fill(r, now, res2)
        c.call(t2, a2, res2)
        c.final(r.choice(finals).replace("{d}", _date_in(res2, now)))
        return {"template": off + 50 + bank.index(sc) * 4 + i_user, "slots": {"tools": [t1, t2]}}

    @family(CAT, "gd_irrelevant_switch", weight=10, failure=GUARD_FAIL + ("unhelpful_result_repeat",))
    def f_gd_irrelevant_switch(r, c, now):
        return _switch(r, c, now, SW, 0)

    # ------------------------------------------------------------------ 3. bounded web: search -> (<=1 fetch) -> final
    # item: users, query, top (title, url), other (title, url, snippet), full snippet, tease snippet, page text, final(snippet), final(page)
    WEB = [
        (["who's running Northgate Robotics these days", "tell me who the chief executive of Northgate Robotics is", "which person leads Northgate Robotics"],
         "Northgate Robotics chief executive", ("Northgate Robotics leadership", "https://northgate-robotics.example.com/about"),
         ("Northgate hires new CTO", "https://news.example.com/northgate-cto", "Northgate adds a chief technology officer."),
         "Dana Whitfield has been chief executive of Northgate Robotics since March 2024.",
         "Meet the leadership team guiding Northgate's next chapter of growth.",
         "Leadership: Dana Whitfield, Chief Executive Officer (since March 2024). Marcus Ellery, CFO. Priya Nand, CTO.",
         "Dana Whitfield is the chief executive of Northgate Robotics, in the role since March 2024",
         "Dana Whitfield is Northgate Robotics' chief executive, since March 2024"),
        (["what's the latest on the Harbor Street bridge closure", "any update on the Harbor Street bridge repairs"],
         "Harbor Street bridge closure update", ("Harbor Street bridge update", "https://cityhall.example.com/news/harbor-bridge"),
         ("Detour map for Harbor St", "https://transit.example.com/detours", "Bus routes 12 and 40 are rerouted."),
         "The Harbor Street bridge reopens to traffic Friday at 6 AM after two weeks of repairs.",
         "City officials update residents on the Harbor Street bridge repair schedule.",
         "Update: crews finished the deck repairs early. The bridge reopens Friday at 6 AM; lane restrictions continue through the weekend.",
         "The Harbor Street bridge reopens Friday at 6 AM after two weeks of repairs",
         "The bridge reopens Friday at 6 AM, with lane restrictions lasting through the weekend"),
        (["google the opening hours for the Maple City library", "check when the Maple City library is open"],
         "Maple City Library opening hours", ("Maple City Library - Hours", "https://maplecity-library.example.com/hours"),
         ("Library card FAQ", "https://maplecity-library.example.com/faq", "How to apply for a library card."),
         "Maple City Library: Mon-Thu 9 AM-8 PM, Fri-Sat 9 AM-5 PM, closed Sunday.",
         "Visit us! Hours vary by branch and season - see the schedule.",
         "Main branch: Mon-Thu 9 AM-8 PM, Fri-Sat 9 AM-5 PM, Sun closed. Holiday hours are posted each December.",
         "Maple City Library is open Mon-Thu 9 AM-8 PM and Fri-Sat 9 AM-5 PM, closed Sunday",
         "The main branch is open Mon-Thu 9 AM-8 PM and Fri-Sat 9 AM-5 PM, and closed on Sundays"),
        (["is registration still open for the Ridgeway half marathon", "see whether I can still sign up for the Ridgeway half marathon"],
         "Ridgeway half marathon registration", ("Ridgeway Half Marathon - Register", "https://ridgewayrun.example.com/register"),
         ("Course map and elevation", "https://ridgewayrun.example.com/course", "Rolling hills, 13.1 miles."),
         "Registration for the Ridgeway Half Marathon closes October 25 or when 3,000 runners sign up.",
         "Race day is coming - secure your spot in the Ridgeway Half Marathon.",
         "Registration is open until October 25 or until the 3,000-runner cap is reached. Entry fee $75; no late sign-ups.",
         "Registration for the Ridgeway Half Marathon is open until October 25, or until 3,000 runners sign up",
         "Yes, it's open until October 25 or until the 3,000-runner cap fills, and the entry fee is $75"),
        (["how long does the battery last on the Lumen X2 headphones", "battery life of Lumen X2 headphones?"],
         "Lumen X2 headphones battery life", ("Lumen X2 specs", "https://lumenaudio.example.com/x2/specs"),
         ("Lumen X2 review", "https://soundtest.example.com/lumen-x2", "Comfortable, strong sound for the price."),
         "Lumen X2: up to 38 hours of playback with noise cancelling off, 30 hours with it on.",
         "Lumen X2: all-day comfort and sound that punches above its price.",
         "Battery: 30 hours with noise cancelling on, 38 with it off. A 10-minute quick charge gives about 5 hours.",
         "The Lumen X2 gets up to 38 hours with noise cancelling off and 30 with it on",
         "Around 30 hours with noise cancelling on and 38 with it off, and a 10-minute charge adds about 5 hours"),
        (["who started Brightwell Foods", "find out who founded Brightwell Foods"],
         "Brightwell Foods founder", ("About Brightwell Foods", "https://brightwellfoods.example.com/about"),
         ("Brightwell recalls granola", "https://news.example.com/brightwell-recall", "A limited granola recall was announced."),
         "Brightwell Foods was founded in 1998 by Tomas Idowu in Minneapolis.",
         "Brightwell Foods: our story, our values and the people behind the label.",
         "Founded in 1998 by Tomas Idowu and Leah Brandt in Minneapolis. Now sold in about 4,000 stores.",
         "Brightwell Foods was founded in 1998 by Tomas Idowu in Minneapolis",
         "Tomas Idowu and Leah Brandt founded Brightwell Foods in Minneapolis in 1998"),
        (["when does the Larkfield farmers market open on weekends", "what time is the Larkfield farmers market on Saturday"],
         "Larkfield farmers market hours", ("Larkfield Farmers Market", "https://larkfieldmarket.example.com"),
         ("Vendor sign-up", "https://larkfieldmarket.example.com/vendors", "Applications open each spring."),
         "Larkfield Farmers Market runs Saturdays from 8 AM to 1 PM, May through October.",
         "Fresh produce, bakers and makers - find the market near you.",
         "Open Saturdays 8 AM-1 PM, May through October, at the Town Green. Dogs on leashes welcome.",
         "The Larkfield Farmers Market runs Saturdays 8 AM to 1 PM, May through October",
         "It's Saturdays 8 AM to 1 PM on the Town Green, May through October"),
        (["what changed in the newest Pinewood browser update", "give me the highlights of the latest Pinewood browser release"],
         "Pinewood browser latest release notes", ("Pinewood 41 release notes", "https://pinewood.example.com/releases/41"),
         ("Pinewood extensions", "https://pinewood.example.com/addons", "Browse extensions for Pinewood."),
         "Pinewood 41 adds tab group sync, a faster PDF viewer and fixes 12 security issues.",
         "Pinewood 41 is here - see what's changed in the release notes.",
         "Pinewood 41: tab group sync across devices, faster PDF viewer, 12 security fixes, dark mode in settings.",
         "Pinewood 41 adds tab group sync and a faster PDF viewer, and fixes 12 security issues",
         "Pinewood 41 brings tab group sync across devices, a faster PDF viewer, 12 security fixes and a dark mode for settings"),
        (["how far is it by car from Fairmont to Linden", "driving distance Fairmont to Linden?"],
         "Fairmont to Linden driving distance", ("Fairmont to Linden drive", "https://roadmiles.example.com/fairmont-linden"),
         ("Linden attractions", "https://visitlinden.example.com", "Things to do in Linden."),
         "Fairmont to Linden is about 62 miles, roughly 1 hour 10 minutes by car.",
         "Plan your Fairmont to Linden trip with distance, route and tolls.",
         "Distance 62 miles via Route 9. Typical drive time 1 h 10 min. No tolls.",
         "Fairmont to Linden is about 62 miles, roughly 1 hour 10 minutes by car",
         "It's 62 miles via Route 9, about 1 hour 10 minutes, with no tolls"),
        (["when does the Fenwick Theater season begin", "find the opening date for the new Fenwick Theater season"],
         "Fenwick Theater new season opening date", ("Fenwick Theater 2026-27 season", "https://fenwicktheater.example.com/season"),
         ("Fenwick box office", "https://fenwicktheater.example.com/tickets", "Tickets on sale now."),
         "The Fenwick Theater 2026-27 season opens November 7 with The Lighthouse Keeper.",
         "A bold new season is almost here at the Fenwick Theater.",
         "Season opener: The Lighthouse Keeper, November 7. Subscriber previews start November 4.",
         "The Fenwick Theater's new season opens November 7 with The Lighthouse Keeper",
         "It opens November 7 with The Lighthouse Keeper, and subscriber previews start November 4"),
        (["check on the Calder Valley brush fire", "any news on the Calder Valley brush fire"],
         "Calder Valley brush fire containment", ("Calder Valley fire update", "https://countyfire.example.com/calder-valley"),
         ("Evacuation shelters open", "https://redcross-local.example.com/shelters", "Shelter at the Hill High gym."),
         "The Calder Valley brush fire is 80% contained and evacuation orders were lifted Tuesday.",
         "Latest incident updates for the Calder Valley brush fire.",
         "Containment is at 80%. Evacuation orders were lifted Tuesday evening; Route 4 is open to residents only.",
         "The Calder Valley brush fire is 80% contained, with evacuation orders lifted Tuesday",
         "It's 80% contained, evacuation orders were lifted Tuesday evening, and Route 4 is open to residents only"),
        (["what's the return policy at Marlow Hardware", "look up how returns work at Marlow Hardware"],
         "Marlow Hardware return policy", ("Marlow Hardware - Returns", "https://marlowhardware.example.com/returns"),
         ("Marlow gift cards", "https://marlowhardware.example.com/gift-cards", "Buy or check a balance."),
         "Marlow Hardware accepts returns within 30 days with a receipt; opened power tools within 14 days.",
         "Questions about a purchase? See our return and exchange information.",
         "Returns within 30 days with receipt. Opened power tools: 14 days. Special orders are final sale.",
         "Marlow Hardware takes returns within 30 days with a receipt, or 14 days for opened power tools",
         "30 days with a receipt, 14 days for opened power tools, and special orders are final sale"),
        (["how much is a Quill app subscription", "what does Quill app cost per month"],
         "Quill app pricing", ("Quill pricing", "https://quillapp.example.com/pricing"),
         ("Quill vs other note apps", "https://appcompare.example.com/quill", "A comparison of five note apps."),
         "Quill Pro costs $8 a month or $72 a year; the free tier is limited to 3 notebooks.",
         "Pick the Quill plan that fits how you write.",
         "Free: 3 notebooks. Pro: $8/month or $72/year, unlimited notebooks and sync.",
         "Quill Pro is $8 a month or $72 a year, and the free tier is limited to 3 notebooks",
         "Pro is $8 a month or $72 a year with unlimited notebooks, while the free plan allows 3 notebooks"),
    ]
    WEB_DEV = [
        (["I'd like to know who the current mayor of Ashford is"],
         "Ashford mayor", ("Ashford city government", "https://ashford.example.com/mayor"),
         ("Ashford council agenda", "https://ashford.example.com/council", "Next meeting Monday."),
         "Rosa Delgado is the mayor of Ashford, elected in 2024.", "Meet Ashford's elected officials.",
         "Mayor Rosa Delgado, elected November 2024. Office hours Tuesdays 2-4 PM at City Hall.",
         "Rosa Delgado is the mayor of Ashford, elected in 2024", "Rosa Delgado has been mayor of Ashford since her 2024 election, and she holds office hours Tuesdays 2-4 PM"),
        (["find out what's going on with the Delmar port strike"],
         "Delmar port strike latest", ("Delmar port strike: latest", "https://delmarpost.example.com/port-strike"),
         ("Shipping delays explained", "https://tradewatch.example.com/delmar", "Cargo is being rerouted."),
         "Delmar port workers and operators resumed talks Wednesday; ships remain anchored offshore.",
         "Follow our coverage of the dockworkers' dispute.",
         "Talks resumed Wednesday after a three-day pause. About 20 ships are waiting offshore; no deal is expected before Friday.",
         "Talks at the Delmar port resumed Wednesday and ships remain anchored offshore",
         "Talks resumed Wednesday, around 20 ships are waiting offshore, and no deal is expected before Friday"),
        (["what does admission cost at the Glasshouse museum"],
         "Glasshouse museum admission price", ("Glasshouse Museum - Visit", "https://glasshouse-museum.example.com/visit"),
         ("Glasshouse exhibitions", "https://glasshouse-museum.example.com/exhibits", "Current and upcoming shows."),
         "Glasshouse Museum admission is $18 for adults and free for under-12s.",
         "Plan your visit to the Glasshouse Museum.",
         "Adults $18, students $12, under 12 free. First Thursday of each month: free evening, 5-8 PM.",
         "Admission to the Glasshouse Museum is $18 for adults and free for under-12s",
         "Adults pay $18, students $12 and under-12s are free; the first Thursday evening each month is free"),
        (["get me the closing time of the Orchard Street pharmacy tonight", "how late is the Orchard Street pharmacy open"],
         "Orchard Street pharmacy hours", ("Orchard Street Pharmacy - Hours", "https://orchardrx.example.com/hours"),
         ("Refill a prescription", "https://orchardrx.example.com/refills", "Order refills online."),
         "Orchard Street Pharmacy is open until 9 PM on weekdays and 6 PM on weekends.",
         "Care for your whole family, every day of the week.",
         "Hours: weekdays 8 AM-9 PM, Saturday 9 AM-6 PM, Sunday 10 AM-6 PM. Pharmacist consultations until 30 minutes before close.",
         "Orchard Street Pharmacy is open until 9 PM on weekdays and 6 PM on weekends",
         "Weekdays it closes at 9 PM, Saturday and Sunday at 6 PM"),
        (["see if the new Tidewater ferry schedule is out"],
         "Tidewater ferry winter schedule", ("Tidewater Ferry - Winter Schedule", "https://tidewaterferry.example.com/winter"),
         ("Ferry fares", "https://tidewaterferry.example.com/fares", "Adults $14 each way."),
         "The Tidewater winter ferry schedule starts November 2, with six crossings a day.",
         "Your guide to sailing times this season.",
         "Winter schedule effective November 2: six daily crossings, first departure 6:30 AM, last 8:15 PM.",
         "The Tidewater winter ferry schedule takes effect November 2, with six crossings a day",
         "It takes effect November 2, with six crossings a day from 6:30 AM to 8:15 PM"),
    ]

    def _domain(url):
        return url.split("//", 1)[1].split("/", 1)[0].replace("www.", "")

    def _web(r, c, now, bank, off):
        sc = r.choice(bank)
        users, q, top, other, full, tease, page, ans_a, ans_b = sc
        i_user = r.randrange(len(users))
        c.user(messy(r, users[i_user]))
        direct = r.random() < 0.4
        snippet = full if direct else tease
        res = (f"1. {top[0]}\n   {top[1]}\n   {snippet}\n2. {other[0]}\n   {other[1]}\n   {other[2]}")
        args = {"query": q}
        if r.random() < 0.5:
            args["limit"] = 2
        upfront = (not direct) and r.random() < 0.5
        c.discover(["public_web"], ["web_search", "web_fetch"] if upfront else ["web_search"])
        c.call("web_search", args, res)
        dom = _domain(top[1])
        if direct:
            c.final(r.choice([f"{ans_a}, per {dom}.", f"{ans_a} (source: {dom}).", f"I checked {dom}. {ans_a}."]))
        else:
            if not upfront:
                c.discover(["public_web"], ["web_fetch"])
            c.call("web_fetch", {"url": top[1]}, f"{top[0]}\n{page}")
            c.final(r.choice([f"{ans_b}, according to {dom}.", f"I opened the page on {dom}. {ans_b}.", f"{ans_b} (source: {dom})."]))
        return {"template": off + bank.index(sc) * 4 + i_user, "slots": {"query": q, "fetched": not direct}}

    @family(CAT, "web_bounded", weight=12, failure=WEB_FAIL, web=True)
    def f_web_bounded(r, c, now):
        return _web(r, c, now, WEB, 0)

    @family(CAT, "web_dev_bounded", split="dev", weight=3, failure=WEB_FAIL, web=True)
    def f_web_dev_bounded(r, c, now):
        return _web(r, c, now, WEB_DEV, 500)

    # ------------------------------------------------------------------ 4. one status / compute read -> immediate final
    def _aqi(r):
        a = r.choice([22, 38, 47, 63, 87, 94, 112, 128, 143, 166, 178])
        if a <= 50:
            return a, "Good", "Great for outdoor plans."
        if a <= 100:
            return a, "Moderate", "Fine for most people; sensitive groups may want to ease off long outdoor exercise."
        if a <= 150:
            return a, "Unhealthy for sensitive groups", "Kids, older adults and anyone with asthma should limit time outside."
        return a, "Unhealthy", "Best to keep outdoor activity short today."

    AQ_TPL = ["is the air quality okay in {city} today", "how's the air in {city} right now", "is it smoky in {city}",
              "should I run outside in {city} or is the air bad"]
    AQ_DEV = ["give me the current AQI for {city}"]
    SUN_TPL = ["what time does the sun go down in {city} today", "how much daylight is left in {city}", "when's sunset in {city}"]
    RAIN_TPL = ["is it about to rain in {city}", "will it rain in {city} in the next few hours", "do I have time for a walk in {city} before the rain"]
    WX_TPL = ["do I need a coat in {city} today", "what's it like outside in {city}", "how warm is it going to be in {city} today"]

    def _stp_web(r, c, now):
        cq, cname = r.choice(CITIES)
        kind = r.choice(["aq", "aq", "sun", "rain", "wx"])
        area = cq.split(",")[0]
        if kind == "aq":
            tpl = r.choice(AQ_TPL)
            c.user(messy(r, tpl.format(city=cname), protect=(cname,)))
            c.discover(["public_web"], ["air_quality"])
            a, cat, adv = _aqi(r)
            pm = round(a * r.uniform(0.28, 0.42), 1)
            c.call("air_quality", {"location": cq}, f"{area} - AQI {a} ({cat}). PM2.5 {pm} ug/m3, ozone {r.randint(22, 58)} ppb. Main pollutant: PM2.5.")
            c.final(f"{cname} has an AQI of {a}, which is {cat.lower()}. {adv}")
            return {"template": AQ_TPL.index(tpl), "slots": {"kind": kind, "city": cname}}
        if kind == "sun":
            tpl = r.choice(SUN_TPL)
            c.user(messy(r, tpl.format(city=cname), protect=(cname,)))
            c.discover(["public_web"], ["astronomy"])
            rh, rm = r.randint(6, 7), r.choice([5, 18, 32, 47])
            sh, sm = r.randint(17, 19), r.choice([8, 21, 36, 52])
            mins = (sh * 60 + sm) - (rh * 60 + rm)
            c.call("astronomy", {"location": cq}, f"{area} - sunrise {t12(rh, rm)}, sunset {t12(sh, sm)}, day length {mins // 60} h {mins % 60} min.")
            c.final(f"Sunset in {cname} is at {t12(sh, sm)}. Sunrise was {t12(rh, rm)}, so the day is {mins // 60} hours {mins % 60} minutes long.")
            return {"template": 10 + SUN_TPL.index(tpl), "slots": {"kind": kind, "city": cname}}
        if kind == "rain":
            tpl = r.choice(RAIN_TPL)
            c.user(messy(r, tpl.format(city=cname), protect=(cname,)))
            c.discover(["public_web"], ["rain_radar"])
            dry = r.random() < 0.4
            if dry:
                res, fin = (f"{area} - next 3 hours: dry, no precipitation expected.", f"No rain expected in {cname} for the next three hours - it should stay dry.")
            else:
                mm1, mm2 = round(r.uniform(0.2, 1.2), 1), round(r.uniform(1.4, 3.5), 1)
                res = f"{area} - next 3 hours: 0-1h light rain ({mm1} mm), 1-2h showers ({mm2} mm), 2-3h dry."
                fin = f"Yes, rain is coming in {cname}: light rain in the next hour, heavier showers in the hour after, then dry again."
            c.call("rain_radar", {"location": cq, "hours": 3}, res)
            c.final(fin)
            return {"template": 20 + RAIN_TPL.index(tpl), "slots": {"kind": kind, "city": cname}}
        tpl = r.choice(WX_TPL)
        c.user(messy(r, tpl.format(city=cname), protect=(cname,)))
        c.discover(["public_web"], ["get_weather"])
        day = G.Day("today", now.date(), "today", now)
        wres, rain, (hi, lo, cond, pct) = weather_result(r, cq, day)
        c.call("get_weather", {"location": cq}, wres)
        coat = "bring a coat" if hi < 60 else "a light layer should do" if hi < 72 else "no coat needed"
        c.final(f"{cname} today: {cond.lower()}, high {hi}F and low {lo}F, {pct}% chance of rain - {coat}" + (" and take an umbrella." if rain else "."))
        return {"template": 30 + WX_TPL.index(tpl), "slots": {"kind": kind, "city": cname}}

    @family(CAT, "stp_status_once", weight=5, failure=STP_FAIL, web=True)
    def f_stp_status_once(r, c, now):
        return _stp_web(r, c, now)

    RATES = [("USD", "EUR", 0.92), ("EUR", "GBP", 0.86), ("GBP", "USD", 1.27), ("USD", "JPY", 151.4), ("CAD", "USD", 0.73),
             ("USD", "INR", 83.5), ("AUD", "USD", 0.65), ("USD", "MXN", 17.9), ("CHF", "EUR", 1.04)]
    NAMES = {"USD": "dollars", "EUR": "euros", "GBP": "pounds", "JPY": "yen", "CAD": "Canadian dollars", "INR": "rupees",
             "AUD": "Australian dollars", "MXN": "pesos", "CHF": "Swiss francs"}
    CUR_TPL = ["what would {a} {fn} come to in {tn}", "{a} {f} -> {t}?", "I'm paying {a} {fn}, what's that in {tn}", "quick conversion: {a} {f} to {t}"]
    BAT_TPL = [("pct", "how much juice does my laptop have left"), ("pct", "what percent is the battery at"),
               ("plug", "do I need to plug in soon"), ("time", "how long can I keep working unplugged"),
               ("health", "how worn out is my battery")]
    UNIT_TPL = ["{v} {fu} in {tu}?", "turn {v} {fu} into {tu}", "how many {tu} is {v} {fu}"]
    UNITS = [("F", "C", "degrees Fahrenheit", "degrees Celsius", lambda v: round((v - 32) * 5 / 9, 2), [350, 400, 425, 212, 98.6, 72]),
             ("lb", "kg", "pounds", "kilograms", lambda v: round(v * 0.45359237, 2), [12, 45, 150, 8.5, 33]),
             ("cups", "ml", "cups", "milliliters", lambda v: round(v * 236.588, 1), [1.5, 2, 3, 0.75]),
             ("gallons", "liters", "gallons", "liters", lambda v: round(v * 3.78541, 2), [4, 12, 15.5, 7])]
    CALC_TPL = ["split {b} four ways with a {p}% tip", "what's each person's share of {b} plus {p}% tip, four people", "{b} bill, {p}% tip, divided by four"]
    SYS_TPL = ["how much space is left on my mac", "is my disk about full", "am I low on storage"]

    def _stp_local(r, c, now):
        kind = r.choice(["cur", "cur", "bat", "bat", "unit", "calc", "sys"])
        if kind == "cur":
            f, t, rate = r.choice(RATES)
            a = r.choice([40, 75, 120, 250, 380, 900, 1500])
            tpl = r.choice(CUR_TPL)
            c.user(messy(r, tpl.format(a=a, f=f, t=t, fn=NAMES[f], tn=NAMES[t])))
            c.discover(["compute"], ["convert_currency"])
            out = round(a * rate, 2)
            c.call("convert_currency", {"amount": a, "from_currency": f, "to_currency": t}, f"{a} {f} = {out:,.2f} {t} (rate {rate})")
            c.final(f"{a} {f} is about {out:,.2f} {t} at the current rate of {rate}.")
            return {"template": CUR_TPL.index(tpl), "slots": {"kind": kind}}
        if kind == "bat":
            k, text = r.choice(BAT_TPL)
            c.user(messy(r, text))
            c.discover(["device"], ["get_battery_status"])
            pct = r.choice([9, 18, 27, 38, 52, 64, 81])
            hrs = max(1, round(pct * 0.06))
            mins = r.choice([5, 20, 35, 50])
            cyc = r.randint(90, 480)
            health = r.randint(84, 98)
            c.call("get_battery_status", {}, f"Battery {pct}%, on battery power, about {hrs} h {mins:02d} min remaining. Cycles: {cyc}. Health: {health}% of design capacity.")
            if k == "pct":
                fin = f"Your battery is at {pct}%, with about {hrs} h {mins:02d} min left."
            elif k == "plug":
                fin = (f"Yes - you're at {pct}% with about {hrs} h {mins:02d} min left, so plugging in soon is a good idea." if pct < 30 else
                       f"Not urgently: you're at {pct}% with about {hrs} h {mins:02d} min remaining.")
            elif k == "time":
                fin = f"Roughly {hrs} h {mins:02d} min at the current drain, from {pct}%."
            else:
                fin = f"It's in decent shape: {health}% of design capacity after {cyc} charge cycles."
            c.final(fin)
            return {"template": 10 + BAT_TPL.index((k, text)), "slots": {"kind": kind}}
        if kind == "unit":
            fu, tu, fn_, tn_, fx, vals = r.choice(UNITS)
            v = r.choice(vals)
            tpl = r.choice(UNIT_TPL)
            c.user(messy(r, tpl.format(v=v, fu=fn_, tu=tn_)))
            c.discover(["compute"], ["convert_units"])
            out = fx(v)
            c.call("convert_units", {"value": v, "from_unit": fu, "to_unit": tu}, f"{v} {fu} = {out} {tu}")
            c.final(f"{v} {fn_} is {out} {tn_}.")
            return {"template": 20 + UNIT_TPL.index(tpl), "slots": {"kind": kind}}
        if kind == "calc":
            b = r.choice([84.0, 126.5, 212.4, 98.0, 164.0])
            p = r.choice([15, 18, 20])
            tpl = r.choice(CALC_TPL)
            c.user(messy(r, tpl.format(b=b, p=p)))
            c.discover(["compute"], ["calculate"])
            val = round(b * (1 + p / 100) / 4, 2)
            expr = f"{b}*{1 + p / 100:.2f}/4"
            c.call("calculate", {"expression": expr}, f"{expr} = {val}")
            c.final(f"Each person pays ${val:.2f}.")
            return {"template": 30 + CALC_TPL.index(tpl), "slots": {"kind": kind}}
        tpl = r.choice(SYS_TPL)
        c.user(messy(r, tpl))
        c.discover(["device"], ["system_status"])
        total = r.choice([256, 460, 926])
        free = r.randint(9, total // 3)
        c.call("system_status", {}, f"Disk: {free} GB free of {total} GB. Memory pressure: normal. CPU load {r.randint(6, 30)}%. Uptime {r.randint(1, 9)} days.")
        c.final(f"You have {free} GB free out of {total} GB" + (", so it's getting tight." if free < total * 0.1 else "."))
        return {"template": 40 + SYS_TPL.index(tpl), "slots": {"kind": kind}}

    @family(CAT, "stp_status_local", weight=3, failure=STP_FAIL)
    def f_stp_status_local(r, c, now):
        return _stp_local(r, c, now)
