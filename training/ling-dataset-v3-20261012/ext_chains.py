"""Compound-chain add-on families for the Ling V3 generator (category key "compound").

Loaded by generate.load_extensions(); everything lives inside register(G). Randomness comes only from the per-conversation
rng `r`. Chains: read->send, read->write, conditional acts (both branches), 3-step chains, multi-source-then-act, dead-end
first reads (stop and tell the user), dependent ordering and parallel independent calls.
"""
import datetime as dt


def register(G):
    first, Q, cap, lc_first, dshort, iso, t12 = G.first, G.Q, G.cap, G.lc_first, G.dshort, G.iso, G.t12
    RELATIVES = G.RELATIVES
    fam = G.family

    # ------------------------------------------------------------------ shared helpers
    def disc(c, tools):
        fams = list(dict.fromkeys(G.TOOL_FAMILY[t] for t in tools))
        c.discover(fams, list(tools))

    def contact(r, rel=0.1):
        nm = G.pick_contact(r, rel)
        said, _ = G.name_forms(r, nm)
        f = nm if nm in RELATIVES else first(nm)
        return nm, said, f, G._name_arg(said, nm)

    def when_word(day):
        return day.kind if day.kind in ("today", "tomorrow") else "on " + G.WEEKDAYS[day.date.weekday()]

    def window_word(day):
        return day.period if day.kind == "date" else day.kind

    def read_lookup(c, r, read, lk, proceed, p=0.4):
        """read and lookup in one parallel step (p) or read first, lookup only if the chain continues."""
        if r.random() < p:
            c.calls([read, lk])
        else:
            c.call(*read)
            if proceed:
                c.call(*lk)

    def send_text(c, r, nm, body, deny=False):
        res = G.DECLINED if deny else G.sent_msg(c, nm)
        c.call("send_message", {"to": G.phone_for(nm), "text": body}, res)

    def email_body(f_, content):
        return G.EMAIL_FRAME.format(first=f_, content=content)

    def agenda(now, day, evs):
        return G.agenda_result(now, window_word(day), evs)

    def make_ev_for(r, day, now, h=None, m=None):
        return G.make_event(r, day.date, h=h if h is not None else r.randint(9, 18), m=m if m is not None else r.choice([0, 30]))

    # ------------------------------------------------------------------ banks (train / dev disjoint)
    NOTES_TR = [  # (query key, title, body, phrase the user says)
        ("wifi", "Guest wifi and gate", "wifi password Maple2026, gate code 4471", "wifi and gate code"),
        ("packing", "Camping packing list", "tent, sleeping bags, headlamp, bug spray", "camping packing list"),
        ("pancake", "Pancake recipe", "2 cups flour, 2 eggs, 1 cup milk, pinch of salt", "pancake recipe"),
        ("reading", "Reading list", "Braiding Sweetgrass; The Dispossessed; Klara and the Sun", "reading list"),
        ("offsite", "Offsite agenda", "9 AM kickoff, 11 AM workshops, 1 PM lunch, 3 PM demos", "offsite agenda"),
        ("venue", "Venue address", "Harbor Hall, 220 Pier Street, doors at 7 PM", "venue address"),
        ("grocery", "Grocery list", "oat milk, eggs, spinach, lemons, rice", "grocery list"),
        ("gift", "Gift ideas for Dana", "ceramic mug, plant, concert tickets", "gift ideas for Dana")]
    NOTES_DV = [
        ("playlist", "Road trip playlist", "Midnight City; Dreams; Electric Feel; Sunset", "road trip playlist"),
        ("hardware", "Hardware store list", "wood glue, sandpaper, two hinges, a level", "hardware store list"),
        ("hotel", "Hotel booking", "Cedar Inn, confirmation 88213, check in after 3 PM", "hotel booking"),
        ("chores", "Weekend chores", "laundry, vacuum, water the ferns, call the plumber", "weekend chores")]
    MAILS_TR = [  # (sender, subject, body)
        ("Priya Raman", "Design review notes", "The review moved to Thursday at 2 PM in Room 4B."),
        ("Jonas Weber", "Lease renewal", "The new lease starts on the first and rent stays the same."),
        ("Camila Reyes", "Workshop schedule", "Doors open at 9 AM and the first session starts at 9:30 AM."),
        ("Marcus Bell", "Contract draft", "The signed draft is attached and the deadline is next Friday."),
        ("Hana Sato", "Trip photos", "The photo album is ready and the link works until the end of the month."),
        ("Luca Romano", "Quarterly numbers", "Revenue is up eight percent and costs are flat.")]
    MAILS_DV = [
        ("Aisha Karimi", "Garage quote", "The repair quote is 420 dollars and the work takes two days."),
        ("Owen Pritchard", "Gym membership", "Your membership renews on the 15th and includes the pool."),
        ("Zoe Tanaka", "School trip form", "The permission slip is due Monday and the bus leaves at 8 AM."),
        ("Elena Petrov", "Catering order", "The order is confirmed for twenty guests and arrives at noon.")]
    INVITES = [  # (sender, subject, invite body, non-invite body)
        ("Dev Malhotra", "Project sync", "Can we meet next week to go over the roadmap?", "Attached is the final invoice for last month."),
        ("Noor Haddad", "Planning chat", "Would you be free for a quick meeting about the launch?", "Just a reminder that the office is closed Monday."),
        ("Felix Armand", "Coffee catch-up", "Want to meet for coffee sometime soon?", "Here are the slides from yesterday's talk."),
        ("Ines Duarte", "Vendor call", "Could we set up a call with the vendor this week?", "The vendor shipped the order and sent a tracking link.")]
    INVITES_DV = [
        ("Rohan Gill", "Budget talk", "Can we sit down and go over the budget together?", "The budget spreadsheet is attached for reference."),
        ("Tessa Brandt", "Mentoring session", "Are you up for a mentoring meeting next week?", "Thanks again for the book recommendation.")]
    MEVENTS_TR = [  # (subject, sender, title, location, duration)
        ("Eye exam booking", "Clearview Optics", "Eye exam", "Clearview Optics", 30),
        ("Tax meeting", "Dana Whitfield", "Tax meeting", "Suite 210", 60),
        ("Piano recital", "Noor Haddad", "Piano recital", "Maple Hall", 90),
        ("Plumber visit", "Harbor Plumbing", "Plumber visit", "Home", 60)]
    ITEM_NOTES_TR = [  # (key, title, items, phrase)
        ("garden", "Garden to-do", ["prune roses", "order mulch", "fix the gate latch"], "garden to-do"),
        ("moving", "Moving checklist", ["box books", "cancel internet", "forward mail"], "moving checklist"),
        ("errands", "Weekend errands", ["post office", "dry cleaning", "pick up keys"], "weekend errands")]
    ACTIVITIES = [("Dinner", "Luigi's"), ("Lunch", "Cafe Verde"), ("Coffee", "the corner cafe"), ("Brunch", "Daisy's"),
                  ("Hike", "Ridgeline Park"), ("Game night", "Theo's place")]
    CAL_NOTES = [("appointments", "Appointments", "Dentist Oct 3; haircut Oct 9"), ("schedule", "Week plan", "Gym Mon; piano Wed"),
                 ("calendar", "Calendar log", "Car service done; vet booked")]

    # ================================================================ 1. note -> email
    NOTE_EMAIL_TR = ["email {n} my {p} note", "find my {p} note and email it to {n}", "send {n} the {p} from my notes by email",
                     "{n} needs the {p} - pull it from my notes and email it", "grab the note about the {p} and email {n} what it says",
                     "can you look up my {p} note, then email it over to {n}?", "email what's in my {p} note to {n}"]
    NOTE_EMAIL_DV = ["look in Notes for the {p} and shoot {n} an email with it", "I'd like {n} to get an email containing my {p} note",
                     "mail {n} the contents of my {p} note", "my {p} note - could you email it to {n}?"]

    def note_email(r, c, now, tpls, bank):
        nm, said, f, arg = contact(r, 0.05)
        key, title, body, phrase = r.choice(bank)
        tpl = r.choice(tpls)
        c.user(G.messy(r, tpl.format(n=said, p=phrase), protect=(said, phrase)))
        disc(c, ["search_notes", "lookup_contact", "send_email"])
        found = r.random() >= 0.2
        res = f"1 note: {title}\n{body}" if found else f"No notes matched {key!r}."
        read_lookup(c, r, ("search_notes", {"query": key}, res), ("lookup_contact", {"name": arg}, G.lookup_result(nm, via="email")), found)
        if not found:
            c.final(r.choice([f"I couldn't find a note about {phrase}, so I haven't emailed {f}. Want me to try a different keyword?",
                              f"Nothing in your notes matches “{key}”, so no email went to {f}. Should I search another way?"]))
            return {"template": tpls.index(tpl), "slots": {"found": False, "note": key}}
        content = G.fix_caps(f"{title}: {body}.")
        mail = email_body(f if nm not in RELATIVES else nm, content)
        deny = r.random() < 0.08
        c.call("send_email", {"to": G.email_for(nm), "subject": title, "body": mail},
               G.DECLINED if deny else G.sent_email(c, G.email_for(nm)))
        c.final(G._fin_declined(r, f, "email") if deny else G._fin_sent(r, "email", f, mail))
        return {"template": tpls.index(tpl), "slots": {"found": True, "note": key, "deny": deny}}

    @fam("compound", "cx_note_email", weight=7, failure=("compound_never_completes", "wrong_chain_order", "invented_content"))
    def f_note_email(r, c, now):
        return note_email(r, c, now, NOTE_EMAIL_TR, NOTES_TR)

    @fam("compound", "cx_dev_note_email", split="dev", weight=3, failure=("compound_never_completes", "wrong_chain_order"))
    def f_dev_note_email(r, c, now):
        return note_email(r, c, now, NOTE_EMAIL_DV, NOTES_DV)

    # ================================================================ 2. email -> text
    MAIL_TEXT_TR = ["{s} emailed me about {subj} - text {n} what it says", "check {s}'s {subj} email and text the gist to {n}",
                    "text {n} what {s} wrote in the {subj} email", "forward the news from {s}'s {subj} email to {n} by text",
                    "look up the {subj} email from {s}, then message {n} the details", "{n} is waiting to hear about {subj} - read {s}'s email and text them",
                    "find {s}'s email about {subj} and text {n} what it said"]
    MAIL_TEXT_DV = ["I got a {subj} email from {s}; pass what it says along to {n} via text", "relay {s}'s {subj} message to {n} as a text",
                    "see what {s} said about {subj} in email and drop {n} a text with it", "get {n} up to speed by text on {s}'s {subj} email"]

    def mail_text(r, c, now, tpls, bank):
        nm, said, f, arg = contact(r, 0.1)
        s, subj, body = r.choice(bank)
        tpl = r.choice(tpls)
        c.user(G.messy(r, tpl.format(s=first(s), subj=subj.lower(), n=said), protect=(said, first(s), subj.lower())))
        disc(c, ["view_emails", "lookup_contact", "send_message"])
        found = r.random() >= 0.2
        d = now.date() - dt.timedelta(days=r.randint(0, 3))
        res = G._mail_listing(s, subj, G._msgid(r), dshort(d), body) if found else f"No emails matched {subj!r}."
        read_lookup(c, r, ("view_emails", {"query": subj, "count": 2, "strict_match": True}, res),
                    ("lookup_contact", {"name": arg}, G.lookup_result(nm)), found, p=0.3)
        if not found:
            c.final(r.choice([f"I couldn't find an email about {subj.lower()} from {first(s)}, so I didn't text {f} anything. Want me to search differently?",
                              f"No email matching “{subj}” turned up, so nothing was sent to {f}."]))
            return {"template": tpls.index(tpl), "slots": {"found": False, "subject": subj}}
        deny = r.random() < 0.08
        send_text(c, r, nm, body, deny)
        c.final(G._fin_declined(r, f) if deny else G._fin_sent(r, "message", f, body))
        return {"template": tpls.index(tpl), "slots": {"found": True, "subject": subj, "deny": deny}}

    @fam("compound", "cx_mail_text", weight=7, failure=("compound_never_completes", "wrong_chain_order", "invented_content"))
    def f_mail_text(r, c, now):
        return mail_text(r, c, now, MAIL_TEXT_TR, MAILS_TR)

    @fam("compound", "cx_dev_mail_text", split="dev", weight=3, failure=("compound_never_completes", "wrong_chain_order"))
    def f_dev_mail_text(r, c, now):
        return mail_text(r, c, now, MAIL_TEXT_DV, MAILS_DV)

    # ================================================================ 3. calendar -> reminder before the event
    REM_TR = [("check when my {a} is {d} and remind me to leave {lead} minutes before", "Leave for"),
              ("set a reminder to leave {lead} min before my {a} {d}", "Leave for"),
              ("remind me to leave for my {a} {d}, {lead} minutes ahead of the start", "Leave for"),
              ("look up my {a} {d}, then set a leave-by reminder {lead} minutes early", "Leave for"),
              ("find my {a} {d} and remind me to head out {lead} minutes before it begins", "Head out for")]
    REM_DV = [("get me to my {a} {d} on time: find it and remind me to set off {lead} minutes prior", "Set off for"),
              ("{a} {d}? check the time and nudge me to go {lead} minutes beforehand", "Go to"),
              ("when is my {a} {d}? whatever time it is, set a reminder to depart {lead} min earlier", "Depart for")]

    def remind_before(r, c, now, tpls):
        day = G.pick_day(r, now, kinds=("tomorrow", "weekday"))
        ev = make_ev_for(r, day, now)
        lead = r.choice([15, 30, 45, 60])
        tpl, prefix = r.choice(tpls)
        c.user(G.messy(r, tpl.format(a=ev.alias, d=day.spoken, lead=lead), protect=(ev.alias,)))
        disc(c, ["get_upcoming", "add_reminder"])
        found = r.random() >= 0.18
        args = {"period": day.period, "query": ev.key}
        if not found:
            c.call("get_upcoming", args, G.agenda_result(now, window_word(day), []))
            c.final(r.choice([f"You don't have a {ev.key} {when_word(day)}, so I haven't set a reminder. Want me to look at another day?",
                              f"Nothing matching “{ev.key}” is on your calendar {when_word(day)}, so no reminder was created."]))
            return {"template": tpls.index((tpl, prefix)), "slots": {"found": False, "event": ev.key}}
        c.call("get_upcoming", args, agenda(now, day, [ev]))
        start = dt.datetime.combine(ev.date, dt.time(ev.h, ev.m)) - dt.timedelta(minutes=lead)
        assert start > now
        title = f"{prefix} {ev.title}"
        c.call("add_reminder", {"title": title, "when_iso": iso(start.date(), start.hour, start.minute)},
               f"Reminder set: “{title}” — {dshort(start.date())}, {t12(start.hour, start.minute)} in Apple Reminders and Wisp.")
        c.final(f"Your {ev.title} starts at {ev.tstr} {when_word(day)}. I set a reminder “{title}” for {t12(start.hour, start.minute)}, {lead} minutes before.")
        return {"template": tpls.index((tpl, prefix)), "slots": {"found": True, "event": ev.key, "lead": lead}}

    @fam("compound", "cx_remind_before", weight=7, failure=("compound_never_completes", "wrong_chain_order", "conditional_action_ignored"))
    def f_remind_before(r, c, now):
        return remind_before(r, c, now, REM_TR)

    @fam("compound", "cx_dev_remind_before", split="dev", weight=3, failure=("compound_never_completes", "wrong_chain_order"))
    def f_dev_remind_before(r, c, now):
        return remind_before(r, c, now, REM_DV)

    # ================================================================ 4. find free time -> block it on the calendar
    FREE_EV = [("Focus time", "focus time"), ("Gym session", "a gym session"), ("Study block", "a study block"),
               ("Deep work", "deep work"), ("Run", "a run"), ("Reading hour", "reading time")]
    FREE_TPL = ["find an hour {d} {part} and block it off for {w}", "is there a free hour {d} {part}? if so put {w} on my calendar",
                "schedule {w} {d} {part} in the first open hour you can find", "look for a gap {d} {part} and book {w} in it",
                "if I have an hour free {d} {part}, add {w} to my calendar"]

    @fam("compound", "cx_free_event", weight=8, failure=("compound_never_completes", "conditional_action_ignored", "wrong_chain_order"))
    def f_free_event(r, c, now):
        day = G.pick_day(r, now, kinds=("tomorrow", "weekday"))
        title, w = r.choice(FREE_EV)
        part, ds, de = r.choice([("morning", 9, 12), ("afternoon", 13, 18), ("afternoon", 12, 17)])
        tpl = r.choice(FREE_TPL)
        c.user(G.messy(r, tpl.format(d=day.spoken, part=part, w=w)))
        disc(c, ["find_free_time", "add_calendar_event"])
        free = r.random() < 0.72
        wins = []
        if free:
            s0 = ds * 60 + r.choice([0, 30, 60])
            wins = [(s0, min(de * 60, s0 + r.choice([60, 90, 120])))]
            if wins[0][1] - wins[0][0] < 60:
                wins = [(ds * 60, ds * 60 + 60)]
            if r.random() < 0.4 and wins[0][1] + 120 <= de * 60:
                wins.append((wins[0][1] + 60, min(de * 60, wins[0][1] + 120)))
        c.call("find_free_time", {"period": day.period, "minutes": 60, "day_start": ds, "day_end": de},
               G.free_result(60, ds, de, wins, day.label))
        if not wins:
            c.final(r.choice([f"There's no free hour {part} {when_word(day)}, so I didn't add anything. Want me to check another time of day?",
                              f"Your {part} is fully booked {when_word(day)} - no calendar event was created."]))
            return {"template": FREE_TPL.index(tpl), "slots": {"free": False}}
        a = wins[0][0]
        d_ = day.date
        c.call("add_calendar_event", {"title": title, "when_iso": iso(d_, a // 60, a % 60), "duration_min": 60},
               f"Added “{title}” to your calendar for {dshort(d_)}, {t12(a // 60, a % 60)}.")
        c.final(f"Found a free slot starting at {t12(a // 60, a % 60)} {when_word(day)} and added “{title}” for an hour.")
        return {"template": FREE_TPL.index(tpl), "slots": {"free": True, "title": title}}

    # ================================================================ 5. messages -> calendar (only if agreed)
    MSG_EV_TR = ["check my texts with {n} - if we settled on a plan, put it on my calendar", "did {n} and I agree on a time? if so add it to my calendar",
                 "look at my messages with {n}; when we've locked something in, create the calendar event",
                 "{n} and I were sorting out plans by text - if it's confirmed, calendar it"]

    @fam("compound", "cx_msgs_event", weight=7, failure=("conditional_action_ignored", "compound_never_completes", "invented_content"))
    def f_msgs_event(r, c, now):
        nm, said, f, arg = contact(r, 0.05)
        tpl = r.choice(MSG_EV_TR)
        act, place = r.choice(ACTIVITIES)
        d = now.date() + dt.timedelta(days=r.randint(2, 8))
        tm = G.pick_time(r, 11, 20, minutes=(0, 30))
        wd = G.WEEKDAYS[d.weekday()]
        c.user(G.messy(r, tpl.format(n=said), protect=(said,)))
        disc(c, ["view_messages", "add_calendar_event"])
        agreed = r.random() < 0.65
        lines = [f"Messages with {nm}:", f"{f} (2:14 PM): {act} at {place} on {wd} at {tm.long}?"]
        lines.append(f"Me (2:20 PM): {r.choice(['Works for me!', 'Yes, perfect', 'Sounds good']) if agreed else 'Let me check my week and get back to you'}")
        if agreed:
            lines.append(f"{f} (2:21 PM): Great, see you then")
        c.call("view_messages", {"conversation": arg, "count": 8}, "\n".join(lines))
        if not agreed:
            c.final(f"{f} suggested {act.lower()} at {place} on {wd} at {tm.long}, but you haven't confirmed yet, so I didn't add anything to your calendar.")
            return {"template": MSG_EV_TR.index(tpl), "slots": {"agreed": False}}
        title = f"{act} at {place}"
        c.call("add_calendar_event", {"title": title, "when_iso": iso(d, tm.h, tm.m), "duration_min": 90, "location": place},
               f"Added “{title}” to your calendar for {dshort(d)}, {tm.long}.")
        c.final(f"You and {f} agreed on {act.lower()} at {place}, so I added it for {wd} at {tm.long}.")
        return {"template": MSG_EV_TR.index(tpl), "slots": {"agreed": True, "activity": act}}

    # ================================================================ 6. calendar -> text, either branch acts
    BUSY_TR = [("check my calendar {d} at {t}: if something's on, tell {n} I can't make it, and if it's clear tell {n} I'll be there", "I can't make it.", "I'll be there."),
               ("am I busy {d} at {t}? text {n} that I'm unavailable if so, or that I'm good to go if not", "I'm unavailable.", "I'm good to go."),
               ("{n} asked about {d} at {t} - look at my calendar and text back no thanks if I'm booked, otherwise count me in", "No thanks.", "Count me in."),
               ("see if I've got anything {d} at {t}; if so text {n} sorry, can't, and if not text {n} yes", "Sorry, can't.", "Yes.")]
    BUSY_DV = [("peek at {d} around {t} - booked means text {n} I have a conflict, open means text {n} I'm free", "I have a conflict.", "I'm free."),
               ("{n} wants me {d} at {t}; if the calendar shows a clash message {n} that I'm out, otherwise that I'm coming", "I'm out.", "I'm coming.")]

    def busy_text(r, c, now, tpls):
        nm, said, f, arg = contact(r, 0.1)
        day = G.clean_day(G.pick_day(r, now, kinds=("tomorrow", "weekday")))
        tm = G.pick_time(r, 10, 17, minutes=(0,))
        tpl, bb, fb = r.choice(tpls)
        c.user(G.messy(r, tpl.format(n=said, d=day.spoken, t=tm.spoken), protect=(said,)))
        disc(c, ["get_upcoming", "lookup_contact", "send_message"])
        busy = r.random() < 0.5
        evs = []
        if busy:
            evs.append(G.make_event(r, day.date, h=tm.h, m=0, dur=60))
        others = [h for h in range(8, 19) if abs(h - tm.h) >= 2]
        if r.random() < 0.7 and others:
            evs.append(G.make_event(r, day.date, h=r.choice(others), m=0, dur=45, avoid=evs, used_keys=[e.key for e in evs]))
        evs.sort(key=lambda e: e.start)
        read_lookup(c, r, ("get_upcoming", {"period": day.period}, agenda(now, day, evs)),
                    ("lookup_contact", {"name": arg}, G.lookup_result(nm)), True, p=0.5)
        body = bb if busy else fb
        send_text(c, r, nm, body)
        why = f"You have {evs[[e.start for e in evs].index(tm.h * 60)].title} at {tm.long}" if busy else f"Nothing is booked at {tm.long}"
        c.final(f"{why} {when_word(day)}, so I texted {f}: {Q(body)}")
        return {"template": [t[0] for t in tpls].index(tpl), "slots": {"busy": busy}}

    @fam("compound", "cx_busy_text", weight=8, failure=("conditional_action_ignored", "compound_never_completes", "wrong_chain_order"))
    def f_busy_text(r, c, now):
        return busy_text(r, c, now, BUSY_TR)

    @fam("compound", "cx_dev_busy_text", split="dev", weight=3, failure=("conditional_action_ignored", "compound_never_completes"))
    def f_dev_busy_text(r, c, now):
        return busy_text(r, c, now, BUSY_DV)

    # ================================================================ 7. agenda -> text / email
    AG_TXT = ["text {n} what's on my calendar {d}", "send {n} my schedule for {d} by text", "let {n} know what I've got going on {d} - check my calendar",
              "message {n} my agenda for {d}", "look at my calendar {d} and text {n} the rundown"]
    AG_MAIL = ["email {n} my agenda for {d}", "send {n} an email with my schedule {d}", "check my calendar {d} and email {n} what's on it",
               "mail {n} the list of what I've got {d}"]

    def agenda_share(r, c, now, tpls, email):
        nm, said, f, arg = contact(r, 0.05)
        day = G.clean_day(G.pick_day(r, now, kinds=("tomorrow", "weekday")))
        tpl = r.choice(tpls)
        c.user(G.messy(r, tpl.format(n=said, d=day.spoken), protect=(said,)))
        disc(c, ["get_upcoming", "lookup_contact", "send_email" if email else "send_message"])
        has = r.random() >= 0.2
        evs = G.make_day(r, day.date, r.randint(2, 3), hmin=8, hmax=18) if has else []
        lk = G.lookup_result(nm, via="email" if email else "phone")
        read_lookup(c, r, ("get_upcoming", {"period": day.period}, agenda(now, day, evs)), ("lookup_contact", {"name": arg}, lk), has, p=0.4)
        if not has:
            c.final(f"Your calendar is empty {when_word(day)}, so I haven't {'emailed' if email else 'texted'} {f}. Do you still want me to send them something?")
            return {"template": tpls.index(tpl), "slots": {"has": False}}
        content = G.fix_caps(f"{day.spoken}: " + ", ".join(f"{e.title} at {e.tstr}" for e in evs) + ".")
        if email:
            body = email_body(f if nm not in RELATIVES else nm, content)
            c.call("send_email", {"to": G.email_for(nm), "subject": f"My schedule {day.spoken}", "body": body}, G.sent_email(c, G.email_for(nm)))
            c.final(G._fin_sent(r, "email", f, body))
        else:
            send_text(c, r, nm, content)
            c.final(G._fin_sent(r, "message", f, content))
        return {"template": tpls.index(tpl), "slots": {"has": True, "n": len(evs)}}

    @fam("compound", "cx_agenda_text", weight=6, failure=("compound_never_completes", "wrong_chain_order", "invented_content"))
    def f_agenda_text(r, c, now):
        return agenda_share(r, c, now, AG_TXT, False)

    @fam("compound", "cx_agenda_email", weight=5, failure=("compound_never_completes", "wrong_chain_order", "invented_content"))
    def f_agenda_email(r, c, now):
        return agenda_share(r, c, now, AG_MAIL, True)

    # ================================================================ 8. email -> conditional reply
    REPLY_TR = [("if {s}'s email about {subj} is asking to meet, reply that I'm in; otherwise leave it alone", "I'm in."),
                ("read {s}'s {subj} email - when it wants a meeting reply yes, and if it doesn't, don't reply", "Yes."),
                ("check the {subj} email from {s} and reply \"sounds good\" only if they're proposing a call or meeting", "Sounds good."),
                ("look at {s}'s {subj} message: if it's a request to get together, reply that I'd love to", "I'd love to.")]

    @fam("compound", "cx_mail_cond_reply", weight=7, failure=("conditional_action_ignored", "compound_never_completes", "wrong_chain_order"))
    def f_mail_cond_reply(r, c, now):
        s, subj, inv, other = r.choice(INVITES)
        tpl, content = r.choice(REPLY_TR)
        sf = first(s)
        c.user(G.messy(r, tpl.format(s=sf, subj=subj.lower()), protect=(sf, subj.lower())))
        disc(c, ["view_emails", "reply_to_email"])
        invite = r.random() < 0.6
        mid = G._msgid(r)
        d = now.date() - dt.timedelta(days=r.randint(0, 2))
        c.call("view_emails", {"query": subj, "count": 2, "strict_match": True},
               G._mail_listing(s, subj, mid, dshort(d), inv if invite else other))
        if not invite:
            c.final(f"{sf}'s email isn't a meeting request - it says: {Q(other)} I didn't send a reply.")
            return {"template": [t for t, _ in REPLY_TR].index(tpl), "slots": {"invite": False}}
        body = email_body(sf, content)
        c.call("reply_to_email", {"message_id": mid, "body": body}, f"Reply sent to {s} <{G.email_for(s)}>.")
        c.final(f"{sf} is asking to meet, so I replied:\n{Q(body)}")
        return {"template": [t for t, _ in REPLY_TR].index(tpl), "slots": {"invite": True}}

    # ================================================================ 9. note items -> reminders
    NR_TR = [("make a reminder for the first item on my {p} note, {d} at {t}", "first"),
             ("take the top thing on my {p} note and remind me {d} at {t}", "first"),
             ("turn each item on my {p} note into a reminder {d} at {t}", "each"),
             ("read my {p} note and set a reminder for every item on it {d} at {t}", "each"),
             ("remind me {d} at {t} about the first task in my {p} note", "first")]
    NR_DV = [("pull up my {p} note and create a reminder per entry for {d} at {t}", "each"),
             ("set a {d} {t} reminder for the opening entry of the {p} note", "first")]
    ITEM_NOTES_DV = [("trip", "Trip prep", ["buy adapter", "print tickets", "refill prescription"], "trip prep"),
                     ("party", "Party prep", ["order cake", "blow up balloons", "make playlist"], "party prep")]

    def note_reminders(r, c, now, tpls, bank):
        key, title, items, phrase = r.choice(bank)
        day = G.pick_day(r, now, kinds=("tomorrow", "weekday"))
        tm = G.pick_time(r, 8, 18, minutes=(0, 30))
        tpl, mode = r.choice(tpls)
        c.user(G.messy(r, tpl.format(p=phrase, d=day.spoken, t=tm.spoken), protect=(phrase,)))
        disc(c, ["search_notes", "add_reminder"])
        found = r.random() >= 0.15
        c.call("search_notes", {"query": key}, f"1 note: {title}\n{', '.join(items)}" if found else f"No notes matched {key!r}.")
        if not found:
            c.final(f"I couldn't find a note matching “{key}”, so I didn't create any reminders.")
            return {"template": tpls.index((tpl, mode)), "slots": {"found": False}}
        chosen = items[:1] if mode == "first" else items
        specs = []
        for it in chosen:
            t_ = cap(it)
            specs.append(("add_reminder", {"title": t_, "when_iso": iso(day.date, tm.h, tm.m)},
                          f"Reminder set: “{t_}” — {dshort(day.date)}, {tm.long} in Apple Reminders and Wisp."))
        if len(specs) > 1 and r.random() < 0.7:
            c.calls(specs)
        else:
            for sp in specs:
                c.call(*sp)
        c.final(f"Set {len(chosen)} reminder{'s' if len(chosen) > 1 else ''} for {dshort(day.date)} at {tm.long}: " + "; ".join(cap(i) for i in chosen) + ".")
        return {"template": tpls.index((tpl, mode)), "slots": {"found": True, "mode": mode, "note": key}}

    @fam("compound", "cx_note_reminders", weight=7, failure=("compound_never_completes", "multi_source_partial", "loop"))
    def f_note_reminders(r, c, now):
        return note_reminders(r, c, now, NR_TR, ITEM_NOTES_TR)

    @fam("compound", "cx_dev_note_reminders", split="dev", weight=3, failure=("compound_never_completes", "multi_source_partial"))
    def f_dev_note_reminders(r, c, now):
        return note_reminders(r, c, now, NR_DV, ITEM_NOTES_DV)

    # ================================================================ 10. email -> event -> text (3-step)
    MET_TR = ["add the {subj} from my email to my calendar and let {n} know when it is", "put the {subj} email on my calendar, then text {n} the details",
              "my {subj} email has the time - calendar it and tell {n} by text", "make a calendar event from the {subj} email and message {n} about it"]

    @fam("compound", "cx_mail_event_text", weight=8, failure=("compound_never_completes", "wrong_chain_order", "multi_source_partial"))
    def f_mail_event_text(r, c, now):
        subj, s, title, loc, dur = r.choice(MEVENTS_TR)
        nm, said, f, arg = contact(r, 0.05)
        tpl = r.choice(MET_TR)
        d = now.date() + dt.timedelta(days=r.randint(2, 9))
        tm = G.pick_time(r, 9, 17, minutes=(0, 30))
        c.user(G.messy(r, tpl.format(subj=subj.lower(), n=said), protect=(said, subj.lower())))
        disc(c, ["view_emails", "add_calendar_event", "lookup_contact", "send_message"])
        found = r.random() >= 0.15
        lk = ("lookup_contact", {"name": arg}, G.lookup_result(nm))
        res = (f"1 email:\nFrom: {s} <{G.email_for(s)}>\nDate: {dshort(now.date())}\nSubject: {subj}\nMessage-ID: {G._msgid(r)}\n"
               f"Body: Your {title} is confirmed for {dshort(d)} at {tm.long}. Location: {loc}.") if found else f"No emails matched {subj!r}."
        if not found:
            c.call("view_emails", {"query": subj, "count": 2, "strict_match": True}, res)
            c.final(f"I couldn't find an email about {subj.lower()}, so I didn't add an event or text {f}.")
            return {"template": MET_TR.index(tpl), "slots": {"found": False}}
        add = ("add_calendar_event", {"title": title, "when_iso": iso(d, tm.h, tm.m), "duration_min": dur, "location": loc},
               f"Added “{title}” to your calendar for {dshort(d)}, {tm.long}.")
        body = f"{title} is {dshort(d)} at {tm.long}."
        read = ("view_emails", {"query": subj, "count": 2, "strict_match": True}, res)
        if r.random() < 0.5:
            c.call(*read)
            c.calls([add, lk])
        else:
            c.calls([read, lk])
            c.call(*add)
        send_text(c, r, nm, body)
        c.final(f"Added {title} for {dshort(d)} at {tm.long} ({loc}) and texted {f}: {Q(body)}")
        return {"template": MET_TR.index(tpl), "slots": {"found": True, "title": title}}

    # ================================================================ 11. dead ends: stop and tell / ask
    DEAD_TR = {
        "contact": ["text {n} when my {a} is {d}", "find my {a} {d} and message {n} the time", "tell {n} what time my {a} is {d}, check the calendar first"],
        "ambiguous": ["look up my {a} {d} and text {q} the time", "let {q} know when my {a} is {d}", "check my calendar for {a} {d} and message {q} about it"],
        "calendar": ["text {n} the start time of my {a} {d}", "find my {a} {d} and let {n} know when it is", "look up the time of my {a} {d} and send it to {n}"],
        "email": ["{s} emailed about {subj} - remind me {d} to get back to them", "find {s}'s {subj} email and set a reminder {d} to respond",
                  "remind me {d} to answer the {subj} email from {s}"]}
    DEAD_DV = {
        "contact": ["I need {n} to know when my {a} is {d}: check my calendar then text them", "grab the time of my {a} {d} and pass it to {n} via text"],
        "calendar": ["what time is my {a} {d}? find it and tell {n} by text", "get the {a} time off my calendar {d} and shoot {n} a message"],
        "email": ["pull up {s}'s {subj} email and set a reminder {d} to reply", "set a reminder {d} to reply to {s} about {subj}, after finding their email"]}

    def dead_end(r, c, now, table):
        kind = r.choice(sorted(table))
        tpls = table[kind]
        tpl = r.choice(tpls)
        day = G.pick_day(r, now, kinds=("tomorrow", "weekday"))
        if kind == "email":
            s, subj = r.choice(G.EMAIL_SUBJ_BANK)
            tm = G.pick_time(r, 8, 18, minutes=(0, 30))
            c.user(G.messy(r, tpl.format(s=first(s), subj=subj.lower(), d=f"{day.spoken} at {tm.spoken}"), protect=(first(s), subj.lower())))
            disc(c, ["view_emails", "add_reminder"])
            c.call("view_emails", {"query": subj, "count": 2, "strict_match": True}, f"No emails matched {subj!r}.")
            c.final(r.choice([f"I couldn't find an email from {first(s)} about {subj.lower()}, so I haven't set a reminder. Do you want a reminder anyway, or should I search differently?",
                              f"No email about “{subj}” turned up, so no reminder was created. Want me to try another search?"]))
            return {"template": tpls.index(tpl), "slots": {"kind": kind, "subject": subj}}
        ev = make_ev_for(r, day, now)
        nm = G.pick_contact(r, 0.0)
        said, _ = G.name_forms(r, nm)
        a_ = ev.alias
        if kind == "ambiguous":
            base = first(r.choice(G.PEOPLE))
            q = base
            two = [f"{base} Ortega", f"{base} Whitfield"]
            c.user(G.messy(r, tpl.format(a=a_, d=day.spoken, q=q), protect=(q, a_)))
            disc(c, ["get_upcoming", "lookup_contact", "send_message"])
            c.calls([("get_upcoming", {"period": day.period, "query": ev.key}, agenda(now, day, [ev])),
                     ("lookup_contact", {"name": q}, G.ambiguous_lookup(q, two))])
            c.final(f"Your {ev.title} is {G.ev_when(now, ev)}, but {q} matches two contacts: A) {two[0]} or B) {two[1]}. Which one should I text? Nothing has been sent.")
            return {"template": tpls.index(tpl), "slots": {"kind": kind, "event": ev.key}}
        c.user(G.messy(r, tpl.format(n=said, a=a_, d=day.spoken), protect=(said, a_)))
        disc(c, ["get_upcoming", "lookup_contact", "send_message"])
        f = nm if nm in RELATIVES else first(nm)
        if kind == "calendar":
            c.call("get_upcoming", {"period": day.period, "query": ev.key}, G.agenda_result(now, window_word(day), []))
            c.final(r.choice([f"I don't see a {ev.key} on your calendar {when_word(day)}, so I didn't text {f}. Is it under a different name or day?",
                              f"Nothing matching “{ev.key}” is scheduled {when_word(day)}, so nothing was sent to {f}."]))
        else:
            qn = G._name_arg(said, nm)
            c.calls([("get_upcoming", {"period": day.period, "query": ev.key}, agenda(now, day, [ev])),
                     ("lookup_contact", {"name": qn}, G.no_contact(qn))])
            c.final(f"Your {ev.title} is {G.ev_when(now, ev)}, but I don't have a saved contact for {qn}, so I haven't texted anyone. What's their number or email?")
        return {"template": tpls.index(tpl), "slots": {"kind": kind, "event": ev.key}}

    @fam("compound", "cx_dead_ends", weight=9, failure=("invented_recipient", "empty_result_acted", "compound_never_completes"))
    def f_dead_ends(r, c, now):
        return dead_end(r, c, now, DEAD_TR)

    @fam("compound", "cx_dev_dead_ends", split="dev", weight=3, failure=("invented_recipient", "empty_result_acted"))
    def f_dev_dead_ends(r, c, now):
        return dead_end(r, c, now, DEAD_DV)

    # ================================================================ 12. calendar -> text + reminder (read -> act -> act, parallel acts)
    CTR_TR = ["find my {a} {d}, text {n} the time and set a reminder an hour before to get ready",
              "look up my {a} {d}; message {n} when it is and remind me an hour ahead to get ready",
              "check my calendar for {a} {d}, tell {n} the time by text, and set a get ready reminder one hour before"]

    @fam("compound", "cx_cal_text_remind", weight=7, failure=("compound_never_completes", "wrong_chain_order", "multi_source_partial"))
    def f_cal_text_remind(r, c, now):
        nm, said, f, arg = contact(r, 0.05)
        day = G.pick_day(r, now, kinds=("tomorrow", "weekday"))
        ev = make_ev_for(r, day, now, h=r.randint(10, 18))
        tpl = r.choice(CTR_TR)
        c.user(G.messy(r, tpl.format(n=said, a=ev.alias, d=day.spoken), protect=(said, ev.alias)))
        disc(c, ["get_upcoming", "lookup_contact", "send_message", "add_reminder"])
        found = r.random() >= 0.15
        read = ("get_upcoming", {"period": day.period, "query": ev.key}, agenda(now, day, [ev]) if found else G.agenda_result(now, window_word(day), []))
        lk = ("lookup_contact", {"name": arg}, G.lookup_result(nm))
        read_lookup(c, r, read, lk, found, p=0.5)
        if not found:
            c.final(f"I couldn't find a {ev.key} {when_word(day)}, so I didn't text {f} or set a reminder.")
            return {"template": CTR_TR.index(tpl), "slots": {"found": False}}
        body = f"My {lc_first(ev.title)} is {G.ev_when(now, ev)}."
        start = dt.datetime.combine(ev.date, dt.time(ev.h, ev.m)) - dt.timedelta(hours=1)
        assert start > now
        title = f"Get ready for {ev.title}"
        c.calls([("send_message", {"to": G.phone_for(nm), "text": body}, G.sent_msg(c, nm)),
                 ("add_reminder", {"title": title, "when_iso": iso(start.date(), start.hour, start.minute)},
                  f"Reminder set: “{title}” — {dshort(start.date())}, {t12(start.hour, start.minute)} in Apple Reminders and Wisp.")])
        c.final(f"Texted {f}: {Q(body)} I also set a reminder “{title}” for {t12(start.hour, start.minute)}.")
        return {"template": CTR_TR.index(tpl), "slots": {"found": True, "event": ev.key}}

    # ================================================================ 13. weather -> conditional text
    WX_TR = [("text {n} to bring an umbrella if it's going to rain in {city} {d}", "Bring an umbrella."),
             ("if {city} is forecast to rain {d}, message {n} to pack a raincoat", "Pack a raincoat."),
             ("check the weather in {city} {d} and only if it's wet tell {n} to wear boots", "Wear boots."),
             ("will it rain in {city} {d}? if yes, text {n} to cover the plants", "Cover the plants.")]

    @fam("compound", "cx_weather_text", weight=5, failure=("conditional_action_ignored", "compound_never_completes"), web=True)
    def f_weather_text(r, c, now):
        nm, said, f, arg = contact(r, 0.1)
        cq, cname = r.choice(G.CITIES)
        day = G.pick_day(r, now, kinds=("tomorrow", "weekday"))
        tpl, body = r.choice(WX_TR)
        c.user(G.messy(r, tpl.format(n=said, city=cname, d=day.spoken), protect=(said, cname)))
        disc(c, ["get_weather", "lookup_contact", "send_message"])
        rain = r.random() < 0.55
        wres, _, (hi, lo, cond, pct) = G.weather_result(r, cq, day, rainy=rain)
        read_lookup(c, r, ("get_weather", {"location": cq, "period": day.period}, wres), ("lookup_contact", {"name": arg}, G.lookup_result(nm)), rain, p=0.4)
        if not rain:
            c.final(f"No rain is expected in {cname} {when_word(day)} ({cond.lower()}, {pct}% chance), so I didn't text {f}.")
        else:
            send_text(c, r, nm, body)
            c.final(f"Rain is forecast in {cname} ({cond.lower()}, {pct}% chance), so I texted {f}: {Q(body)}")
        return {"template": WX_TR.index((tpl, body)), "slots": {"rain": rain, "city": cname}}

    # ================================================================ 15. emails + calendar -> note (multi-source then act)
    BRIEF_TR = [("save a note called \"{t}\" with today's emails and my meetings", "Morning brief"),
                ("make a note titled \"{t}\" listing today's emails and what's on my calendar", "Day snapshot"),
                ("create a note \"{t}\" - put my email digest and today's schedule in it", "Inbox and agenda"),
                ("write today's mail summary and calendar into a new note called \"{t}\"", "Daily recap")]

    @fam("compound", "cx_brief_note", weight=6, failure=("multi_source_partial", "compound_never_completes", "wrong_chain_order"))
    def f_brief_note(r, c, now):
        tpl, t = r.choice(BRIEF_TR)
        c.user(G.messy(r, tpl.format(t=t)))
        disc(c, ["summarize_emails", "get_upcoming", "create_note"])
        n = r.randint(2, 3)
        dig, items = G.email_digest(r, n, "today")
        evs = G.make_day(r, now.date(), r.randint(1, 3), hmin=max(8, min(17, now.hour + 1)), hmax=19)
        specs = [("summarize_emails", {"period": "today"}, dig), ("get_upcoming", {"period": "today"}, G.agenda_result(now, "today", evs))]
        if r.random() < 0.5:
            specs.reverse()
        c.calls(specs)
        body = "Emails: " + "; ".join(f"{s} ({subj})" for s, subj, _ in items) + "\nCalendar: " + ", ".join(f"{e.title} at {e.tstr}" for e in evs)
        c.call("create_note", {"title": t, "body": body}, f"Created the note \"{t}\" in Notes.")
        c.final(f"Saved the note “{t}” with {n} emails and {len(evs)} calendar item{'s' if len(evs) != 1 else ''} from today.")
        return {"template": [x for x, _ in BRIEF_TR].index(tpl), "slots": {"n_mail": n, "n_events": len(evs)}}

    # ================================================================ 17. calendar + note -> append
    APP_TR = ["add my {a} {d} to my {k} note", "find my {a} {d} and jot it down in the {k} note", "put the time of my {a} {d} into my {k} note",
              "append my {a} {d} to the {k} note"]

    @fam("compound", "cx_event_append", weight=6, failure=("multi_source_partial", "compound_never_completes", "conditional_action_ignored"))
    def f_event_append(r, c, now):
        day = G.pick_day(r, now, kinds=("tomorrow", "weekday"))
        ev = make_ev_for(r, day, now)
        key, ntitle, nbody = r.choice(CAL_NOTES)
        tpl = r.choice(APP_TR)
        c.user(G.messy(r, tpl.format(a=ev.alias, d=day.spoken, k=key), protect=(ev.alias, key)))
        disc(c, ["get_upcoming", "search_notes", "append_note"])
        ev_ok = r.random() >= 0.12
        note_ok = r.random() >= 0.15
        c.calls([("get_upcoming", {"period": day.period, "query": ev.key}, agenda(now, day, [ev]) if ev_ok else G.agenda_result(now, window_word(day), [])),
                 ("search_notes", {"query": key}, f"1 note: {ntitle}\n{nbody}" if note_ok else f"No notes matched {key!r}.")])
        if not ev_ok:
            c.final(f"I couldn't find a {ev.key} {when_word(day)} on your calendar, so I didn't change your {key} note.")
            return {"template": APP_TR.index(tpl), "slots": {"ev_ok": False, "note_ok": note_ok}}
        if not note_ok:
            c.final(f"Your {ev.title} is {G.ev_when(now, ev)}, but I can't find a note matching “{key}”. Should I create one?")
            return {"template": APP_TR.index(tpl), "slots": {"ev_ok": True, "note_ok": False}}
        text = f"{ev.title} {G.ev_when(now, ev)}"
        c.call("append_note", {"title": ntitle, "text": text}, f"Added it to \"{ntitle}\".")
        c.final(f"Added “{text}” to your note “{ntitle}”.")
        return {"template": APP_TR.index(tpl), "slots": {"ev_ok": True, "note_ok": True}}

    # ================================================================ 18. two contacts: parallel lookups, parallel sends
    PAR_TR = [("c", "text {a} and {b} that {c}"), ("c", "let {a} and {b} both know {c}"), ("c", "message {a} and {b}: {c}"),
              ("q", 'text {a} and {b} "{q}"'), ("c", "tell both {a} and {b} that {c}"), ("c", "send {a} and {b} a text saying {c}")]

    @fam("compound", "cx_two_texts", weight=6, failure=("compound_never_completes", "multi_source_partial"))
    def f_two_texts(r, c, now):
        n1 = G.pick_contact(r, 0.05)
        n2 = n1
        while n2 == n1:
            n2 = G.pick_contact(r, 0.05)
        s1, _ = G.name_forms(r, n1)
        s2, _ = G.name_forms(r, n2)
        it = G.text_intent(r)
        mode, tpl = r.choice(PAR_TR)
        body = it["quote"] if mode == "q" else it["body"]
        prompt = tpl.format(a=s1, b=s2, q=body) if mode == "q" else tpl.format(a=s1, b=s2, c=it["clause"])
        c.user(G.messy(r, prompt, protect=(s1, s2)))
        disc(c, ["lookup_contact", "send_message"])
        c.calls([("lookup_contact", {"name": G._name_arg(s1, n1)}, G.lookup_result(n1)),
                 ("lookup_contact", {"name": G._name_arg(s2, n2)}, G.lookup_result(n2))])
        c.calls([("send_message", {"to": G.phone_for(n1), "text": body}, G.sent_msg(c, n1)),
                 ("send_message", {"to": G.phone_for(n2), "text": body}, G.sent_msg(c, n2))])
        f1 = n1 if n1 in RELATIVES else first(n1)
        f2 = n2 if n2 in RELATIVES else first(n2)
        c.final(r.choice([f"Texted {f1} and {f2}: {Q(body)}", f"Both sent - {f1} and {f2} got: {Q(body)}"]))
        return {"template": PAR_TR.index((mode, tpl)), "slots": {"a": n1, "b": n2, "topic": it["topic"]}}
