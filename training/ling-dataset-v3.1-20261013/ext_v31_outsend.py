"""V3.1 outbound look-alike families (category key "outsend").

V3 failure: outbound requests worded with a look-alike word ("send a note to Sam", "remind Sam about the meeting",
"say happy birthday to Priya", "slack Dan the file", "invite Sam to dinner", "decline the meeting and tell him why",
"update the team that standup moved") picked a self-directed tool (create_note, add_reminder, text_to_speech,
airdrop_file, add_calendar_event, cancel_event only, update_reminder). Every family here mixes ~70% real sends with
~30% CONTRAST rows that use the same trigger word for its self-directed meaning, so the model learns the distinction.

Recipients come only from lookup_contact results (never invented); message/e-mail bodies reuse only words from the
user's request (the validator's send_body_fidelity rule). File sharing: the REG send_email/send_message schemas have no
attachment parameter, so a "share/send X the <file>" row is find_files -> lookup_contact -> a text/e-mail that NAMES the
file (it never claims to attach it) and the final answer says plainly that Wisp cannot attach files and offers
AirDrop; airdrop_file is used only when the user says AirDrop.
"""
import datetime as dt

CAT = "outsend"
FAIL = ("outbound_lookalike",)

_NAMES = ("family", "messy", "pick_contact", "name_forms", "lookup_result", "phone_for", "email_for", "sent_msg", "sent_email",
          "_fin_sent", "Q", "first", "cap", "lc_first", "PEOPLE", "RELATIVES", "text_intent", "email_intent", "EMAIL_FRAME",
          "fix_caps", "clean_day", "pick_day", "pick_time", "iso", "long_when", "reminder_title", "dshort", "t12",
          "agenda_result", "ev_when", "Ev", "EVENT_BANK", "HOUR_RANGE", "_name_arg", "WEEKDAYS", "CUR")


def register(G):
    globals().update({n: getattr(G, n) for n in _NAMES})

    def dev():
        return CUR["split"] == "dev"

    _SEEN = {}

    def contrast(label):
        """Deterministic ~30% contrast share per family (positions 2, 5, 8 of every ten builds)."""
        n = _SEEN.get(label, 0)
        _SEEN[label] = n + 1
        return n % 10 in (2, 5, 8)

    _EM = {}

    def email_turn(label):
        """Deterministic ~30% e-mail share among the non-contrast builds of a family."""
        n = _EM.get(label, 0)
        _EM[label] = n + 1
        return n % 10 in (1, 4, 7)

    def who(nm):
        return nm if nm in RELATIVES else first(nm)

    def contact(r, relative_p=0.12):
        nm = pick_contact(r, relative_p)
        said, _ = name_forms(r, nm)
        return nm, said

    def person(r):
        nm = r.choice(PEOPLE)
        said, _ = name_forms(r, nm)
        return nm, said

    def lk(said, nm, via="phone"):
        return ("lookup_contact", {"name": _name_arg(said, nm)}, lookup_result(nm, via=via))

    def send_text(c, nm, body):
        c.call("send_message", {"to": phone_for(nm), "text": body}, sent_msg(c, nm))

    def send_mail(c, nm, subject, body):
        c.call("send_email", {"to": email_for(nm), "subject": subject, "body": body}, sent_email(c, email_for(nm)))

    def frame(nm, content):
        return EMAIL_FRAME.format(first=first(nm), content=content)

    def day_time(r, now, lo=9, hi=20):
        day = clean_day(pick_day(r, now, kinds=("tomorrow", "weekday", "weekday")))
        tm = pick_time(r, lo, hi, minutes=(0, 30))
        return day, tm

    # ================================================================== 1. "note" means send
    NS_MSG = [("c", "drop {n} a line saying {c}"), ("c", "write {n} a quick note saying {c}"), ("q", 'send {n} a quick note: "{q}"'),
              ("c", "send a note to {n} saying {c}"), ("c", "leave {n} a note - {c}"), ("q", 'pass {n} a note that says "{q}"'),
              ("c", "shoot {n} a note to say {c}"), ("c", "fire {n} a short note: {c}"), ("c", "write a note to {n} saying {c}"),
              ("q", 'drop {n} a line: "{q}"')]
    NS_EML = ["send {n} a short note by email {c}", "write {n} a note over email {c}", "email {n} a quick note {c}",
              "drop {n} a line by email {c}"]
    NS_SELF = ["make a note that {x}", "add a note saying {x}", "jot down a note: {x}", "note to self - {x}",
               "write a note to myself that {x}", "take a note: {x}", "start a note about this: {x}", "keep a note that {x}"]
    NS_MSG_DEV = [("c", "scribble {n} a note, {c}"), ("c", "compose a brief note for {n}: {c}"), ("c", "get a note off to {n} saying {c}"),
                  ("q", 'put a note in front of {n}: "{q}"')]
    NS_EML_DEV = ["compose a short note to {n} by email {c}", "get an email note out to {n} {c}"]
    NS_SELF_DEV = ["put it in my notes: {x}", "save a note for me that {x}", "log a note to myself: {x}"]
    SELF_NOTES = [
        ("the dentist moved my cleaning to Thursday", "Dentist cleaning"), ("Priya recommended a book called Piranesi", "Book Piranesi"),
        ("we should try the new ramen place on Elm", "Ramen place"), ("the landlord wants the lease signed by Friday", "Landlord lease"),
        ("the vendor quote is good until the end of the month", "Vendor quote"), ("my passport expires next spring", "Passport"),
        ("the plumber says the valve needs replacing", "Plumber valve"), ("Theo can lend us his tent for the trip", "Tent trip"),
        ("the book club meets at Maya's place next time", "Book club"), ("the gym closes early on Sunday", "Gym Sunday"),
        ("the wifi router needs a reset every few weeks", "Wifi router"), ("the parking permit renews in November", "Parking permit"),
        ("our anniversary dinner reservation is at eight", "Anniversary dinner")]
    SELF_NOTES_DEV = [
        ("the car needs an oil change before the road trip", "Car oil change"), ("Lena suggested a podcast about sleep", "Lena podcast"),
        ("the neighbors are away until the twentieth", "Neighbors away"), ("the contractor will send the estimate on Monday", "Contractor estimate"),
        ("the pharmacy closes at six on weekdays", "Pharmacy closes")]

    def self_note(r, c, tpls):
        clause, title = r.choice(SELF_NOTES_DEV if dev() else SELF_NOTES)
        tpl = r.choice(tpls)
        c.user(messy(r, tpl.format(x=clause)))
        c.discover(["notes"], ["create_note"])
        c.call("create_note", {"title": title, "body": cap(clause) + "."}, f'Created the note "{title}" in Notes.')
        c.final(r.choice([f"Created the note “{title}” in Notes.", f"Done - “{title}” is in your Notes: {clause}."]))
        return {"template": tpls.index(tpl), "slots": {"kind": "contrast_note"}}

    def note_send(r, c, now, msg_t, eml_t, self_t, self_off):
        if contrast("note" + str(dev())):
            m = self_note(r, c, self_t)
            m["template"] += self_off
            return m
        if email_turn("note" + str(dev())):                      # e-mail
            nm = r.choice(PEOPLE)
            said, _ = name_forms(r, nm)
            it = email_intent(r, first(nm))
            tpl = r.choice(eml_t)
            c.user(messy(r, tpl.format(n=said, c=it["clause"]), protect=(said,)))
            c.discover(["mail", "messages_contacts"], ["lookup_contact", "send_email"])
            c.call(*lk(said, nm, "email"))
            body = frame(nm, it["core"])
            send_mail(c, nm, it["subject"], body)
            c.final(_fin_sent(r, "email", first(nm), body))
            return {"template": 100 + eml_t.index(tpl), "slots": {"kind": "email", "contact": nm, "topic": it["topic"]}}
        nm, said = contact(r)
        it = text_intent(r)
        mode, tpl = r.choice(msg_t)
        body = it["quote"] if mode == "q" else it["body"]
        c.user(messy(r, tpl.format(n=said, c=it["clause"], q=it["quote"]), protect=(said,)))
        c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
        c.call(*lk(said, nm))
        send_text(c, nm, body)
        c.final(_fin_sent(r, "message", who(nm), body))
        return {"template": msg_t.index((mode, tpl)), "slots": {"kind": "message", "contact": nm, "topic": it["topic"]}}

    @family(CAT, "op_note_means_send", weight=10, failure=FAIL)
    def f_note_means_send(r, c, now):
        return note_send(r, c, now, NS_MSG, NS_EML, NS_SELF, 200)

    @family(CAT, "op_dev_note_send", split="dev", weight=3, failure=FAIL)
    def f_dev_note_send(r, c, now):
        return note_send(r, c, now, NS_MSG_DEV, NS_EML_DEV, NS_SELF_DEV, 200)

    # ================================================================== 2. remind / nudge someone else
    TASKS = ["bring the signed permission slip", "pick up the cake", "pay the deposit", "book the venue", "submit the expense report",
             "water the plants", "renew the parking permit", "buy the tickets", "bring the camera", "confirm the reservation",
             "print the boarding passes", "return the library books"]
    TASKS_DEV = ["return the borrowed drill", "sign up for the 5k", "bring the folding chairs", "send the signed contract",
                 "feed the fish", "pack the swim gear"]
    THINGS = ["the report", "the team lunch", "tonight's dinner", "the dentist appointment", "the budget review", "the school pickup",
              "the quarterly numbers", "the game night", "the parent forms"]
    THINGS_DEV = ["the fundraiser", "the vendor call", "the carpool schedule", "the potluck", "the client deadline"]
    # (slot, user template, message body template); {x} is the task ("to ...") or the thing ("about ...")
    RO = [("task", "remind {n} to {x}", "Just reminding you to {x}."), ("thing", "remind {n} about {x}", "Just reminding you about {x}."),
          ("thing", "nudge {n} about {x}", "Just nudging you about {x}."), ("task", "nudge {n} to {x}", "Just nudging you to {x}."),
          ("thing", "give {n} a heads-up about {x}", "Heads-up about {x}."), ("task", "give {n} a nudge to {x}", "Just a nudge to {x}."),
          ("task", "send {n} a reminder to {x}", "Reminder to {x}."), ("thing", "message {n} a quick reminder about {x}", "Quick reminder about {x}."),
          ("task", "tell {n} not to forget to {x}", "Do not forget to {x}."), ("task", "text {n} to make sure they remember to {x}", "Remember to {x}.")]
    RO_DEV = [("task", "{n} needs reminding to {x} - let them know", "Reminding you to {x}."), ("thing", "ask {n} if they remember {x}", "Do you remember {x}?"),
              ("thing", "text {n} so they don't forget about {x}", "Don't forget about {x}."),
              ("task", "{n} tends to forget to {x} - text them", "Do not forget to {x}.")]
    RM_ME = ["remind me to {x} {w}", "nudge me to {x} {w}", "give me a heads-up {w} to {x}", "remind me {w} to {x}",
             "set me a reminder to {x} {w}"]
    RM_ME_DEV = ["I need a nudge {w} to {x}", "I need reminding {w} to {x}"]

    def remind_other(r, c, now, tpls, me_tpls):
        if contrast("remind" + str(dev())):                       # contrast: remind ME -> add_reminder
            task = r.choice(TASKS_DEV if dev() else TASKS)
            day = pick_day(r, now, kinds=("tomorrow", "weekday", "weekday"))
            tm = pick_time(r, 7, 20, minutes=(0, 30))
            tpl = r.choice(me_tpls)
            c.user(messy(r, tpl.format(x=task, w=f"{day.spoken} at {tm.spoken}"), protect=(task,)))
            c.discover(["calendar"], ["add_reminder"])
            title = reminder_title(task)
            c.call("add_reminder", {"title": title, "when_iso": iso(day.date, tm.h, tm.m)},
                   f"Reminder set: “{title}” — {long_when(day.date, tm.h, tm.m)} in Apple Reminders and Wisp.")
            c.final(r.choice([f"Done - I'll remind you to {task} on {long_when(day.date, tm.h, tm.m)}.",
                              f"Reminder set: {task}, {long_when(day.date, tm.h, tm.m)}."]))
            return {"template": 100 + me_tpls.index(tpl), "slots": {"kind": "contrast_reminder_me", "task": task}}
        nm, said = contact(r)
        slot, tpl, btpl = r.choice(tpls)
        if slot == "task":
            x = r.choice(TASKS_DEV if dev() else TASKS)
            if r.random() < 0.4:
                x += " " + clean_day(pick_day(r, now, kinds=("tomorrow", "weekday"))).spoken
        else:
            x = r.choice(THINGS_DEV if dev() else THINGS)
            if r.random() < 0.3:
                day, tm = day_time(r, now, 9, 17)
                x = f"the meeting {day.spoken} at {tm.spoken}"
        body = fix_caps(btpl.format(x=x))
        c.user(messy(r, tpl.format(n=said, x=x), protect=(said, x)))
        c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
        c.call(*lk(said, nm))
        send_text(c, nm, body)
        c.final(_fin_sent(r, "message", who(nm), body))
        return {"template": tpls.index((slot, tpl, btpl)), "slots": {"kind": "remind_other", "contact": nm, "slot": slot}}

    @family(CAT, "op_remind_other", weight=12, failure=FAIL)
    def f_remind_other(r, c, now):
        return remind_other(r, c, now, RO, RM_ME)

    @family(CAT, "op_dev_remind_other", split="dev", weight=4, failure=FAIL)
    def f_dev_remind_other(r, c, now):
        return remind_other(r, c, now, RO_DEV, RM_ME_DEV)

    # ================================================================== 3. "say" something to a person
    SAY = [("happy birthday", "!"), ("congrats", "!"), ("happy anniversary", "!"), ("good luck tomorrow", "!"), ("get well soon", "!"),
           ("welcome back", "!"), ("thanks for dinner last night", "!"), ("congrats on the new job", "!"), ("nice work on the launch", "!"),
           ("sorry about earlier", "."), ("happy holidays", "!"), ("thank you for the help", "!"), ("safe travels", "!")]
    SAY_DEV = [("happy birthday", "!"), ("congrats", "!"), ("thanks", "!"), ("good luck", "!"), ("well done", "!"), ("happy anniversary", "!")]
    SAY_T = ["say {p} to {n}", "tell {n} {p} from me", "go ahead and say {p} to {n}", "say {p} to {n} by text", "can you say {p} to {n}",
             "just say {p} to {n} for me"]
    SAY_T_DEV = ["shoot {n} a quick {p}", "pass along my {p} to {n}", "{n} deserves a {p} from me - send one"]
    SAY_LOUD = ["say {p} out loud", "say {p} out loud so I can hear it", "say this out loud: {p}", "speak {p} through the speakers"]
    SAY_LOUD_DEV = ["voice {p} aloud for me", "speak {p} out loud"]
    # (history user, history assistant, text to speak)
    SAY_HIST = [("what does my packing note say", "Your note says: passport, charger, rain jacket, sunscreen.", "passport, charger, rain jacket, sunscreen"),
                ("what's my next reminder", "Your next reminder is to pick up the dry cleaning at 5 PM.", "pick up the dry cleaning at 5 PM"),
                ("what's the recipe I saved", "Lentil soup: 1 cup lentils, 2 carrots, cumin, lemon.", "Lentil soup: 1 cup lentils, 2 carrots, cumin, lemon")]
    LOUD_REF = ["read that out loud", "say that out loud", "read that aloud to me", "say that aloud"]
    LOUD_REF_DEV = ["speak that for me", "read that back to me out loud"]
    YES = [("say yes", "Yes."), ("tell them yes", "Yes."), ("say sure", "Sure."), ("say I'm in", "I'm in."),
           ("tell them I'll be there", "I'll be there."), ("say no, I can't make it", "No, I can't make it."),
           ("just tell them yes please", "Yes please.")]
    YES_DEV = [("answer them with a yes", "Yes."), ("give them a yes", "Yes."), ("respond that I'm in", "I'm in."),
               ("go with a no, I can't make it", "No, I can't make it.")]
    ASKED = ["asked if you can come to the potluck on Sunday", "asked if you're free for lunch this week", "wants to know if you're joining the hike",
             "asked if you're in for game night"]

    def say_to(r, c, now, say, tpls, loud, loud_ref, yes):
        if contrast("say" + str(dev())):                          # contrast: say it out loud -> text_to_speech
            if r.random() < 0.35:
                hu, ha, speak = r.choice(SAY_HIST)
                c.hist("user", hu).hist("assistant", ha)
                c.user(messy(r, r.choice(loud_ref)))
                text = speak
            else:
                ph, punct = r.choice(say)
                c.user(messy(r, r.choice(loud).format(p=ph)))
                text = cap(ph) + punct
            c.discover(["apps_media"], ["text_to_speech"])
            c.call("text_to_speech", {"text": text}, "Spoke the text aloud.")
            c.final(r.choice(["Said it out loud.", "Done - I read it out loud.", "I said it through the speakers."]))
            return {"template": 100, "slots": {"kind": "contrast_tts"}}
        nm, said = contact(r)
        if r.random() < 0.5:                                      # "say <phrase> to <name>"
            ph, punct = r.choice(say)
            tpl = r.choice(tpls)
            body = cap(ph) + punct
            c.user(messy(r, tpl.format(p=ph, n=said), protect=(said,)))
            idx = tpls.index(tpl)
        else:                                                     # "tell her yes" with history naming the person
            a, body = r.choice(yes)
            fn = who(nm)
            c.hist("user", f"what did {fn} say in the last text")
            c.hist("assistant", f"{fn} {r.choice(ASKED)}.")
            c.user(messy(r, a))
            idx = 50 + yes.index((a, body))
            said = fn if nm in RELATIVES else first(nm)
            c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
            c.call("lookup_contact", {"name": nm if nm in RELATIVES else first(nm)}, lookup_result(nm))
            send_text(c, nm, body)
            c.final(_fin_sent(r, "message", fn, body))
            return {"template": idx, "slots": {"kind": "say_history", "contact": nm}}
        c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
        c.call(*lk(said, nm))
        send_text(c, nm, body)
        c.final(_fin_sent(r, "message", who(nm), body))
        return {"template": idx, "slots": {"kind": "say_to", "contact": nm}}

    @family(CAT, "op_say_to", weight=10, failure=FAIL)
    def f_say_to(r, c, now):
        return say_to(r, c, now, SAY, SAY_T, SAY_LOUD, LOUD_REF, YES)

    @family(CAT, "op_dev_say_to", split="dev", weight=3, failure=FAIL)
    def f_dev_say_to(r, c, now):
        return say_to(r, c, now, SAY_DEV, SAY_T_DEV, SAY_LOUD_DEV, LOUD_REF_DEV, YES_DEV)

    # ================================================================== 4. share / send a file
    FILES = [("budget spreadsheet", "budget", "~/Documents/budget_2026.xlsx"), ("lease PDF", "lease", "~/Documents/lease_2026.pdf"),
             ("trip itinerary", "itinerary", "~/Downloads/itinerary_lisbon.pdf"), ("slide deck", "deck", "~/Desktop/kickoff_deck.key"),
             ("meeting minutes", "minutes", "~/Documents/meeting-minutes.docx"), ("resume", "resume", "~/Downloads/resume_v3.docx"),
             ("project proposal", "proposal", "~/Documents/proposal_draft.docx"), ("invoice", "invoice", "~/Downloads/invoice_2048.pdf"),
             ("photo album", "photos", "~/Downloads/trip-photos.zip"), ("team roster", "roster", "~/Desktop/roster.xlsx")]
    SH_MSG = ["send {n} the {f}", "share the {f} with {n}", "slack {n} the {f}", "slack the {f} to {n}", "get the {f} over to {n}", "pass {n} the {f}", "text {n} the {f}"]
    SH_EML = ["email {n} the {f}", "send the {f} to {n} by email"]
    SH_AIR = ["airdrop {n} the {f}", "airdrop the {f} to {n}", "AirDrop my {f} over to {n}"]

    def find_result(r, now, path):
        return f"1 match:\n{path} (modified {dshort(now.date() - dt.timedelta(days=r.randint(2, 20)))})"

    @family(CAT, "op_share_file", weight=8, failure=FAIL + ("airdrop_vs_send",))
    def f_share_file(r, c, now):
        spoken, q, path = r.choice(FILES)
        fname = path.split("/")[-1]
        fres = find_result(r, now, path)
        if contrast("share"):                                     # contrast: the user said AirDrop -> airdrop_file
            nm, said = contact(r, 0.05)
            tpl = r.choice(SH_AIR)
            c.user(messy(r, tpl.format(n=said, f=spoken), protect=(said,)))
            c.discover(["files"], ["find_files", "airdrop_file"])
            c.call("find_files", {"query": q}, fres)
            c.call("airdrop_file", {"path": path}, f"AirDrop sheet opened for {path}. Choose the recipient's device to finish the transfer.")
            c.final(r.choice([f"Opened the AirDrop sheet for {fname}. Pick {who(nm)}'s device in it to finish sending.",
                              f"The AirDrop sheet for {fname} is open - choose {who(nm)}'s device there to complete the transfer."]))
            return {"template": 100 + SH_AIR.index(tpl), "slots": {"kind": "contrast_airdrop", "file": q}}
        email = r.random() < 0.3
        nm = r.choice(PEOPLE) if email else pick_contact(r, 0.1)
        said, _ = name_forms(r, nm)
        tpl = r.choice(SH_EML if email else SH_MSG)
        c.user(messy(r, tpl.format(n=said, f=spoken), protect=(said, spoken)))
        c.discover(["files", "mail", "messages_contacts"] if email else ["files", "messages_contacts"],
                   ["find_files", "lookup_contact", "send_email" if email else "send_message"])
        flook = ("find_files", {"query": q}, fres)
        look = lk(said, nm, "email" if email else "phone")
        if r.random() < 0.5:
            c.calls([flook, look])
        else:
            c.call(*flook)
            c.call(*look)
        content = f"I have the {spoken} for you." if r.random() < 0.5 else f"I have the {spoken} ({fname}) for you."
        f = who(nm)
        if email:
            body = frame(nm, content)
            send_mail(c, nm, cap(spoken), body)
        else:
            body = content
            send_text(c, nm, body)
        note = r.choice([f"Wisp can't attach files, so the {spoken} itself hasn't gone to {f} - say the word and I'll open AirDrop for it.",
                         f"One catch: I can't attach files from here, so {f} only has the note. I can open AirDrop for {fname} if you want to send the file itself.",
                         f"Heads up: the {fname} file wasn't attached because Wisp can't attach files. I can set it up in AirDrop if you like."])
        lead = "Emailed" if email else "Texted"
        c.final(f"Found {fname} and {lead.lower()} {f}:\n{Q(body)}\n{note}" if email else f"Found {fname} and texted {f}: {Q(body)}\n{note}")
        return {"template": (SH_EML if email else SH_MSG).index(tpl) + (50 if email else 0),
                "slots": {"kind": "share_email" if email else "share_message", "file": q, "contact": nm}}

    # ================================================================== 5. invite someone
    INV_TITLES = ["dinner at my place", "game night", "a movie night", "birthday drinks", "a hike", "brunch", "coffee", "lunch",
                  "a barbecue", "a picnic"]
    INV_HOURS = {"dinner at my place": (18, 20), "game night": (18, 20), "a movie night": (18, 20), "birthday drinks": (17, 20),
                 "a hike": (8, 14), "brunch": (9, 12), "coffee": (8, 15), "lunch": (11, 13), "a barbecue": (12, 18), "a picnic": (11, 15)}
    # (kind, user template, body template)
    INV = [("m", "invite {n} to {x}", "You're invited to {x}."), ("m", "ask {n} to join me for {x}", "Can you join me for {x}?"),
           ("m", "text {n} an invite to {x}", "You're invited to {x}."), ("m", "send {n} an invite for {x}", "You're invited to {x}."),
           ("m", "see if {n} is up for {x}", "Are you up for {x}?"), ("m", "invite {n} over for {x}", "You're invited over for {x}."),
           ("e", "email {n} to invite them to {x}", "You're invited to {x}."),
           ("e", "send {n} an email inviting them to {x}", "You're invited to {x}.")]
    CAL_SUFFIX = [" and put it on my calendar", " and add it to my calendar too"]
    ON_CAL = ["put {t} with {n} on my calendar {w}", "add {t} with {n} to my calendar for {w}", "calendar {t} with {n} {w}",
              "block {w} on my calendar for {t} with {n}"]

    def cal_title(t):
        return cap(t[2:] if t.startswith("a ") else t)

    @family(CAT, "op_invite", weight=10, failure=FAIL + ("calendar_instead_of_invite",))
    def f_invite(r, c, now):
        title = r.choice(INV_TITLES)
        lo, hi = INV_HOURS[title]
        day, tm = day_time(r, now, lo, hi)
        w = f"{day.spoken} at {tm.spoken}"
        if contrast("invite"):                                    # contrast: calendar entry only
            nm, said = person(r)
            tpl = r.choice(ON_CAL)
            ttl = cal_title(title) + f" with {first(nm)}"
            c.user(messy(r, tpl.format(t=title, n=said, w=w), protect=(said,)))
            c.discover(["calendar"], ["add_calendar_event"])
            c.call("add_calendar_event", {"title": ttl, "when_iso": iso(day.date, tm.h, tm.m)},
                   f"Added “{ttl}” to your calendar for {long_when(day.date, tm.h, tm.m)}.")
            c.final(r.choice([f"Added “{ttl}” to your calendar for {long_when(day.date, tm.h, tm.m)}.",
                              f"Done - {ttl} is on your calendar, {WEEKDAYS[day.date.weekday()]} at {tm.long}."]))
            return {"template": 100 + ON_CAL.index(tpl), "slots": {"kind": "contrast_calendar", "contact": nm}}
        kind, tpl, btpl = r.choice(INV)
        email = kind == "e"
        nm = r.choice(PEOPLE) if email else pick_contact(r, 0.1)
        said, _ = name_forms(r, nm)
        x = f"{title} {w}"
        cal = r.random() < 0.3
        c.user(messy(r, tpl.format(n=said, x=x) + (r.choice(CAL_SUFFIX) if cal else ""), protect=(said,)))
        tools = ["lookup_contact", "send_email" if email else "send_message"] + (["add_calendar_event"] if cal else [])
        fams = (["mail"] if email else []) + ["messages_contacts"] + (["calendar"] if cal else [])
        c.discover(fams, tools)
        body_c = fix_caps(btpl.format(x=x))
        look = lk(said, nm, "email" if email else "phone")
        ttl = cal_title(title)
        add = ("add_calendar_event", {"title": ttl, "when_iso": iso(day.date, tm.h, tm.m)},
               f"Added “{ttl}” to your calendar for {long_when(day.date, tm.h, tm.m)}.")
        if cal:
            c.calls([add, look])
        else:
            c.call(*look)
        f = who(nm)
        if email:
            body = frame(nm, body_c)
            send_mail(c, nm, f"Invitation: {ttl}", body)
        else:
            body = body_c
            send_text(c, nm, body)
        base = _fin_sent(r, "email" if email else "message", f, body)
        c.final((f"Added {ttl} to your calendar for {dshort(day.date)} at {tm.long}, and " + base[:1].lower() + base[1:]) if cal else base)
        return {"template": INV.index((kind, tpl, btpl)), "slots": {"kind": "invite", "contact": nm, "calendar": cal, "email": email}}

    # ================================================================== 6. cancel / decline and tell
    REASONS = ["I'm sick", "something came up", "my flight got delayed", "I have a conflict", "the kids are home sick", "my car won't start"]
    # (person_event_only, user template, body mode, email)
    DT = [(False, "cancel my {a} {d} and let {n} know", "plain", False), (True, "cancel on {n} {d} and let them know", "plain", False),
          (True, "cancel {a} {d} and tell {n} I can't make it", "cant", False),
          (False, "scrap my {a} {d} and message {n} to say I can't make it", "cant", False),
          (False, "cancel my {a} {d} and tell {n} it's because {r}", "because", False),
          (False, "cancel the {a} {d} and let {n} know {r}", "reason", False),
          (False, "cancel my {a} {d} and email {n} that {r}", "reason", True),
          (False, "bail on my {a} {d} and text {n} that I can't make it", "cant", False),
          (False, "decline my {a} {d} and tell {n} I can't make it", "cant", False),
          (True, "decline {a} {d} and let {n} know I can't make it", "cant", False),
          (False, "back out of the {a} {d} and tell {n} I can't make it, {r}", "cantr", False),
          (False, "pull out of my {a} {d} and message {n}: {r}, so I can't make it", "cantr", False),
          (False, "bail on the {a} {d} and send {n} an email saying {r}, so I can't make it", "cantr", True),
          (True, "decline {a} {d}: cancel it and let {n} know why, {r}", "reason", False),
          (False, "get rid of my {a} {d} and tell {n} I have to cancel: {r}", "reason", False)]
    CANCEL_ONLY = ["scrap the {a} {d}", "drop my {a} {d} from the schedule", "get rid of my {a} {d}", "cancel my {a} {d}",
                   "I'm skipping the {a} {d} - cancel it"]

    def build_event(r, now, nm, person_event, kinds=("tomorrow", "weekday")):
        day = clean_day(pick_day(r, now, kinds=kinds))
        pool = [e for e in EVENT_BANK if ("{p}" in e[0]) == person_event]
        for _ in range(60):
            t, loc, key, aliases = r.choice(pool)
            p = first(nm)
            lo, hi = HOUR_RANGE.get(key, (8, 18))
            h = r.randint(max(lo, 9), max(hi, 9))
            m = r.choice([0, 30])
            ev = Ev(day.date, h, m, r.choice([30, 60]), t.format(p=p), loc, key, r.choice(aliases).format(p=p))
            return ev, day
        raise RuntimeError("event")

    @family(CAT, "op_decline_and_tell", weight=8, failure=FAIL + ("cancel_without_notify",))
    def f_decline_and_tell(r, c, now):
        if contrast("decline"):                                   # contrast: cancel alone -> cancel_event only
            nm = r.choice(PEOPLE)
            ev, day = build_event(r, now, nm, r.random() < 0.4)
            tpl = r.choice(CANCEL_ONLY)
            c.user(messy(r, tpl.format(a=ev.alias, d=day.spoken), protect=(ev.alias,)))
            c.discover(["calendar"], ["cancel_event"])
            c.call("cancel_event", {"title": ev.key}, f"Cancelled “{ev.title}”.")
            c.final(r.choice([f"Cancelled “{ev.title}”.", f"Done - {ev.title} is off your calendar."]))
            return {"template": 100 + CANCEL_ONLY.index(tpl), "slots": {"kind": "contrast_cancel_only", "event": ev.key}}
        pe_only, tpl, mode, email = r.choice(DT)
        person_event = pe_only or r.random() < 0.5
        nm = r.choice(PEOPLE)
        said, _ = name_forms(r, nm)
        ev, day = build_event(r, now, nm, person_event)
        reason = r.choice(REASONS)
        c.user(messy(r, tpl.format(a=ev.alias, d=day.spoken, n=said, r=reason), protect=(said, ev.alias, reason)))
        c.discover(["calendar", "messages_contacts"] + (["mail"] if email else []),
                   ["get_upcoming", "cancel_event", "lookup_contact", "send_email" if email else "send_message"])
        what = ev.title.split(" with ")[0] if person_event else ev.title
        what = lc_first(what)
        base = f"I have to cancel {'our' if person_event else 'my'} {what} {ev_when(now, ev)}"
        if mode == "plain":
            content = base + "."
        elif mode == "cant":
            content = f"I can't make it to {'our' if person_event else 'my'} {what} {ev_when(now, ev)}."
        elif mode == "cantr":
            content = f"{cap(reason)}, so I can't make it to {'our' if person_event else 'my'} {what} {ev_when(now, ev)}."
        elif mode == "because":
            content = base + f" because {reason}."
        else:
            content = f"{cap(reason)}, so {lc_first(base)}."
        content = fix_caps(content)
        look = lk(said, nm, "email" if email else "phone")
        gu = ("get_upcoming", {"period": day.period, "query": ev.key}, agenda_result(now, day.period if day.kind == "date" else day.kind, [ev]))
        parallel = r.random() < 0.4
        if parallel:
            c.calls([gu, look])
        else:
            c.call(*gu)
        c.call("cancel_event", {"title": ev.key}, f"Cancelled “{ev.title}”.")
        if not parallel:
            c.call(*look)
        f = first(nm)
        if email:
            body = frame(nm, content)
            send_mail(c, nm, f"Cancelling {what}", body)
            tail = f"emailed {f}:\n{Q(body)}"
        else:
            body = content
            send_text(c, nm, body)
            tail = f"texted {f}: {Q(body)}"
        c.final(r.choice([f"Cancelled “{ev.title}” and {tail}", f"Done - {ev.title} is cancelled, and I {tail}"]))
        return {"template": DT.index((pe_only, tpl, mode, email)), "slots": {"kind": "decline_and_tell", "contact": nm, "event": ev.key, "email": email}}

    # ================================================================== 7. tell the team / everyone
    GROUPS = ["the team", "everyone", "the whole crew", "the group"]
    GROUP_HIST = [("who's coming to the {e}", "{names} are all coming."), ("who said yes to the {e}", "{names} said yes."),
                  ("who's on the {e} list", "On the list: {names}.")]
    GROUP_EVENTS = ["offsite", "team dinner", "workshop", "study session", "volunteer day"]
    GP_HIST = ["let {g} know {c}", "update {g} that {c}", "tell {g} {c}", "send {g} a heads-up: {c}"]
    GP_NAMED = ["let {names} know {c}", "update {names} that {c}", "tell {names} {c}", "message {names} to say {c}"]
    GROUP_MSGS = [("the standup moved to {tm}", "The standup moved to {tm}."), ("the meeting is now at {tm}", "The meeting is now at {tm}."),
                  ("dinner is at {tm} at the usual place", "Dinner is at {tm} at the usual place."),
                  ("I'll be ten minutes late", "I'll be ten minutes late."), ("the call is cancelled today", "The call is cancelled today."),
                  ("the deadline moved to Friday", "The deadline moved to Friday."), ("lunch is on me", "Lunch is on me.")]

    @family(CAT, "op_update_group", weight=6, failure=FAIL + ("update_reminder_for_message",))
    def f_update_group(r, c, now):
        k = r.choice([2, 3, 3])
        people = r.sample(PEOPLE, k)
        tm = pick_time(r, 9, 17, minutes=(0, 30))
        cl, bt = r.choice(GROUP_MSGS)
        clause, body = cl.format(tm=tm.spoken), fix_caps(bt.format(tm=tm.spoken))
        firsts = [first(p) for p in people]
        spoken_names = ", ".join(firsts[:-1]) + " and " + firsts[-1]
        if r.random() < 0.55:                                     # group word, names come from the previous turn
            g = r.choice(GROUPS)
            ev = r.choice(GROUP_EVENTS)
            hu, ha = r.choice(GROUP_HIST)
            c.hist("user", hu.format(e=ev)).hist("assistant", ha.format(names=spoken_names))
            tpl = r.choice(GP_HIST)
            c.user(messy(r, tpl.format(g=g, c=clause)))
            idx = GP_HIST.index(tpl)
        else:
            tpl = r.choice(GP_NAMED)
            c.user(messy(r, tpl.format(names=spoken_names, c=clause), protect=tuple(firsts)))
            idx = 20 + GP_NAMED.index(tpl)
        c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
        c.calls([("lookup_contact", {"name": first(p)}, lookup_result(p)) for p in people])
        c.calls([("send_message", {"to": phone_for(p), "text": body}, sent_msg(c, p)) for p in people])
        c.final(r.choice([f"Texted {spoken_names}: {Q(body)}", f"Sent to {spoken_names}: {Q(body)}",
                          f"Done - {spoken_names} each got your message: {Q(body)}"]))
        return {"template": idx, "slots": {"kind": "group", "people": firsts}}
