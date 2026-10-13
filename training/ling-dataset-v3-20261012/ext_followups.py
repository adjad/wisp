"""Follow-ups with history (category key "followup"): multi-turn conversations where the earlier turns matter.

Covers: references resolved from history, corrections, answering the assistant's own clarifying question, answers
available FROM HISTORY (no tool), follow-ups that need a fresh read, topic switches, and follow-ups after a declined send.
All synthetic. Defined inside register(G); uses only the per-conversation rng `r`.
"""
import datetime as dt


def register(G):
    first, cap, lc_first, Q = G.first, G.cap, G.lc_first, G.Q
    PEOPLE, RELATIVES, WEEKDAYS = G.PEOPLE, G.RELATIVES, G.WEEKDAYS
    family, messy, iso, t12, dw = G.family, G.messy, G.iso, G.t12, G.dw
    long_when, pick_day, clean_day, pick_time = G.long_when, G.pick_day, G.clean_day, G.pick_time
    phone_for, email_for, lookup_result = G.phone_for, G.email_for, G.lookup_result
    EMAIL_FRAME, EMAIL_SUBJ_BANK = G.EMAIL_FRAME, G.EMAIL_SUBJ_BANK

    # ---------------------------------------------------------------- shared banks / helpers
    TASKS_TRAIN = ["call the plumber", "pick up dry cleaning", "pay the electricity bill", "water the plants", "renew passport",
                   "send the invoice", "take out the recycling", "submit expense report", "refill prescription", "book flights",
                   "return library books", "back up photos", "order printer ink", "call the bank"]
    TASKS_DEV = ["book the dentist", "mail the package", "buy a birthday card", "feed the neighbor's cat", "fix the bike tire"]
    BODY_SNIPS = ["Please review before Friday.", "Here are the numbers we discussed.", "Can you confirm the time?",
                  "Sharing the draft for your thoughts.", "Let me know if the new date works."]
    DEV_REPLY = [("sounds great, count me in", "Sounds great, count me in."), ("sorry, I'll need another day", "Sorry, I'll need another day."),
                 ("please send the updated version", "Please send the updated version."), ("I'll call you this afternoon", "I'll call you this afternoon.")]
    ORD = [(0, "first"), (1, "second"), (2, "third"), (2, "last")]
    PAIRS_TRAIN = [("Dentist cleaning", "Dentist consult", "dentist"), ("Yoga class", "Yoga workshop", "yoga"),
                   ("Client call", "Client demo", "client"), ("Budget review", "Budget workshop", "budget")]
    PAIRS_DEV = [("Piano lesson", "Piano recital", "piano"), ("Team standup", "Standup retro", "standup"), ("Vet appointment", "Vet checkup", "vet")]

    def fn(nm):
        return nm if nm in RELATIVES else first(nm)

    def pd(r, now, kinds):
        return clean_day(pick_day(r, now, kinds=kinds))

    def two_times(r, lo=8, hi=19):
        a = pick_time(r, lo, hi, minutes=(0, 30))
        for _ in range(30):
            b = pick_time(r, lo, hi, minutes=(0, 30))
            if (a.h, a.m) != (b.h, b.m):
                return a, b
        raise AssertionError("times")

    def mail_list_text(picks):
        return "Your 3 latest emails: " + "; ".join(f"{i}) {s} - {subj}" for i, (s, subj) in enumerate(picks, 1)) + "."

    def mail_hist(r, c):
        picks = r.sample(EMAIL_SUBJ_BANK, 3)
        c.hist("user", r.choice(["show my latest emails", "what are my newest emails", "list my last three emails"]))
        c.hist("assistant", mail_list_text(picks))
        return picks

    def ordinal_phrase(r, picks):
        idx, word = r.choice(ORD)
        k = idx + 1
        return idx, word, k, picks[idx]

    def reminder_hist(r, c, now, day, tm, task):
        c.hist("user", r.choice(["remind me to {t} {d} at {s}", "set a reminder to {t} {d} at {s}"]).format(t=task, d=day.spoken, s=tm.spoken))
        c.hist("assistant", f"Done - I'll remind you to {task} on {long_when(day.date, tm.h, tm.m)}.")

    def wk_day(r, now, avoid=()):
        for _ in range(40):
            d = pd(r, now, ("weekday",))
            if d.date not in avoid:
                return d
        raise AssertionError("day")

    # ================================================================ 1. "text them that" (content lives in history)
    LEADS = ["I want {n} to know {c}", "thinking about telling {n} {c}", "{n} is waiting to hear back - {c}", "I should let {n} know {c}"]
    LEADS_DEV = ["I probably ought to tell {n} that {c}", "{n} still doesn't know {c}"]
    OFFERS = ["Want me to send that to {n}?", "I can text {n} if you like.", "Should I pass that along to {n}?"]
    TT_TRAIN = ["text them that", "yes, send that by text", "ok text {n} that", "go ahead and message {n}",
                "yep, shoot {n} a text with that", "send it as a text"]
    TT_DEV = ["do it over iMessage", "send that to {n} by text, please", "{n} - message them that", "yes, ping {n} with that"]

    def _text_that(r, c, now, tpls, leads):
        nm = G.pick_contact(r, 0.12)
        it = G.text_intent(r)
        f = fn(nm)
        c.hist("user", r.choice(leads).format(n=f, c=it["clause"]))
        c.hist("assistant", r.choice(OFFERS).format(n=f))
        tpl = r.choice(tpls)
        c.user(messy(r, tpl.format(n=f), protect=(f,)))
        c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
        c.call("lookup_contact", {"name": f}, lookup_result(nm))
        c.call("send_message", {"to": phone_for(nm), "text": it["body"]}, G.sent_msg(c, nm))
        c.final(G._fin_sent(r, "message", f, it["body"]))
        return {"template": tpls.index(tpl), "slots": {"contact": nm, "topic": it["topic"]}}

    @family("followup", "fu_text_that", weight=3, failure=("followup_context_lost", "send_on_vague_assent"))
    def f_fu_text_that(r, c, now):
        return _text_that(r, c, now, TT_TRAIN, LEADS)

    @family("followup", "fu_text_that_dev", split="dev", weight=2, failure=("followup_context_lost", "send_on_vague_assent"))
    def f_fu_text_that_dev(r, c, now):
        return _text_that(r, c, now, TT_DEV, LEADS_DEV)

    # ================================================================ 2. reply / forward to "the second one"
    RO_TRAIN = ["reply to the {o} one and say {p}", "write back to the {o} one - {p}", "answer the {o} email: tell them {p}",
                "respond to number {k} saying {p}"]
    RO_DEV = ["the {o} one needs an answer - say {p}", "get back to whoever sent the {o} one: {p}", "send a reply on the {o} thread saying {p}"]

    def _reply_ordinal(r, c, now, tpls, bodies):
        picks = mail_hist(r, c)
        idx, word, k, (s, subj) = ordinal_phrase(r, picks)
        phrase, content = r.choice(bodies)
        tpl = r.choice(tpls)
        c.user(messy(r, tpl.format(o=word, k=k, p=phrase), protect=(phrase,)))
        c.discover(["mail"], ["reply_to_email", "view_emails"])
        mid = G._msgid(r)
        day = now.date() - dt.timedelta(days=r.randint(0, 2))
        c.call("view_emails", {"query": subj, "count": 3, "strict_match": True},
               G._mail_listing(s, subj, mid, f"{G.dshort(day)}, {t12(r.randint(7, 17), 0)}", "Could you let me know when you have a moment?"))
        body = EMAIL_FRAME.format(first=first(s), content=content)
        c.call("reply_to_email", {"message_id": mid, "body": body}, f"Reply sent to {s} <{email_for(s)}>.")
        c.final(r.choice([f"Replied to {first(s)} about {subj}:\n{Q(body)}", f"Done - your reply to {first(s)} ({subj}) is sent:\n{Q(body)}"]))
        return {"template": tpls.index(tpl), "slots": {"ordinal": word, "sender": s}}

    @family("followup", "fu_reply_ordinal", weight=3, failure=("followup_context_lost", "wrong_referent"))
    def f_fu_reply_ordinal(r, c, now):
        return _reply_ordinal(r, c, now, RO_TRAIN, G.REPLY_BODIES)

    @family("followup", "fu_reply_ordinal_dev", split="dev", weight=2, failure=("followup_context_lost", "wrong_referent"))
    def f_fu_reply_ordinal_dev(r, c, now):
        return _reply_ordinal(r, c, now, RO_DEV, DEV_REPLY)

    FO_TPL = ["forward the {o} one to {n}", "send the {o} email along to {n}", "pass number {k} on to {n}", "can you forward the {o} one to {n}"]

    @family("followup", "fu_forward_ordinal", weight=3, failure=("followup_context_lost", "wrong_referent"))
    def f_fu_forward_ordinal(r, c, now):
        picks = mail_hist(r, c)
        idx, word, k, (s, subj) = ordinal_phrase(r, picks)
        nm = r.choice([p for p in PEOPLE if p != s])
        tpl = r.choice(FO_TPL)
        c.user(messy(r, tpl.format(o=word, k=k, n=first(nm)), protect=(first(nm),)))
        c.discover(["mail", "messages_contacts"], ["forward_email", "lookup_contact", "view_emails"])
        mid = G._msgid(r)
        day = now.date() - dt.timedelta(days=r.randint(1, 4))
        c.calls([("view_emails", {"query": subj, "count": 3, "strict_match": True},
                  G._mail_listing(s, subj, mid, f"{G.dshort(day)}, {t12(r.randint(7, 17), 0)}", "Please see the details below.")),
                 ("lookup_contact", {"name": first(nm)}, lookup_result(nm, via="email"))])
        c.call("forward_email", {"message_id": mid, "to": email_for(nm)}, f"Forwarded to {email_for(nm)}.")
        c.final(r.choice([f"Forwarded {first(s)}'s “{subj}” email to {first(nm)}.", f"Done - {first(nm)} now has the {subj} email."]))
        return {"template": FO_TPL.index(tpl), "slots": {"ordinal": word, "to": nm}}

    # ================================================================ 3. "move it to 4" / "change it to Thursday"
    MT_TRAIN = ["move it to {t}", "push that to {t}", "change it to {t} instead", "actually, shift it to {t}", "reschedule that for {t}"]
    MT_DEV = ["reschedule it, {t} works better for me", "I'd rather have it at {t} - change it", "shift that one over to {t}", "update it to {t} please"]

    def _move_time(r, c, now, tpls, tasks):
        task = r.choice(tasks)
        key = task.split()[-1]
        day = pd(r, now, ("tomorrow", "weekday"))
        t1, t2 = two_times(r, 7, 20)
        reminder_hist(r, c, now, day, t1, task)
        tpl = r.choice(tpls)
        c.user(messy(r, tpl.format(t=t2.spoken), protect=(t2.spoken,)))
        c.discover(["calendar"], ["update_reminder"])
        c.call("update_reminder", {"title": key, "when_iso": iso(day.date, t2.h, t2.m)},
               f"Updated “{G.reminder_title(task)}” - now {long_when(day.date, t2.h, t2.m)}.")
        c.final(r.choice([f"Done - “{task}” is now at {t2.long} on {WEEKDAYS[day.date.weekday()]}.", f"Moved your “{task}” reminder to {t2.long}."]))
        return {"template": tpls.index(tpl), "slots": {"task": task}}

    @family("followup", "fu_move_reminder_time", weight=3, failure=("followup_context_lost", "duplicate_instead_of_update"))
    def f_fu_move_time(r, c, now):
        return _move_time(r, c, now, MT_TRAIN, TASKS_TRAIN)

    @family("followup", "fu_move_reminder_dev", split="dev", weight=2, failure=("followup_context_lost", "duplicate_instead_of_update"))
    def f_fu_move_time_dev(r, c, now):
        return _move_time(r, c, now, MT_DEV, TASKS_DEV)

    MD_WK = ["actually, change it to {d}", "make it {d} instead - reschedule it", "push that to {d}", "move it to {d}, same time"]
    MD_TOM = ["push that to tomorrow", "actually move it to tomorrow", "change it to tomorrow instead"]

    @family("followup", "fu_move_reminder_day", weight=3, failure=("followup_context_lost", "duplicate_instead_of_update"))
    def f_fu_move_day(r, c, now):
        task = r.choice(TASKS_TRAIN)
        key = task.split()[-1]
        day = wk_day(r, now)
        t1 = pick_time(r, 7, 20, minutes=(0, 30))
        reminder_hist(r, c, now, day, t1, task)
        title = G.reminder_title(task)
        if r.random() < 0.35:
            tpl = r.choice(MD_TOM)
            c.user(messy(r, tpl))
            c.discover(["calendar"], ["update_reminder"])
            c.call("update_reminder", {"title": key, "day": "tomorrow"}, f"Updated “{title}” - now tomorrow, same time.")
            c.final(f"Moved your “{task}” reminder to tomorrow at {t1.long}.")
            return {"template": 10 + MD_TOM.index(tpl), "slots": {"task": task}}
        nd = wk_day(r, now, avoid=(day.date,))
        tpl = r.choice(MD_WK)
        c.user(messy(r, tpl.format(d=nd.spoken), protect=(nd.spoken,)))
        c.discover(["calendar"], ["update_reminder"])
        c.call("update_reminder", {"title": key, "when_iso": iso(nd.date, t1.h, t1.m)}, f"Updated “{title}” - now {long_when(nd.date, t1.h, t1.m)}.")
        c.final(f"Done - “{task}” is now {WEEKDAYS[nd.date.weekday()]} at {t1.long}.")
        return {"template": MD_WK.index(tpl), "slots": {"task": task}}

    # ================================================================ 4. same action for another day / person
    SF_TPL = ["same for {d}", "do the same {d}", "add one more for {d} too, same time", "and again on {d}"]

    @family("followup", "fu_same_for_reminder", weight=3, failure=("followup_context_lost", "wrong_slot"))
    def f_fu_same_for(r, c, now):
        task = r.choice(TASKS_TRAIN)
        d1 = wk_day(r, now)
        t1 = pick_time(r, 7, 20, minutes=(0, 30))
        reminder_hist(r, c, now, d1, t1, task)
        d2 = wk_day(r, now, avoid=(d1.date,))
        tpl = r.choice(SF_TPL)
        c.user(messy(r, tpl.format(d=d2.spoken), protect=(d2.spoken,)))
        c.discover(["calendar"], ["add_reminder"])
        title = G.reminder_title(task)
        c.call("add_reminder", {"title": title, "when_iso": iso(d2.date, t1.h, t1.m)},
               f"Reminder set: “{title}” — {long_when(d2.date, t1.h, t1.m)} in Apple Reminders and Wisp.")
        c.final(f"Added a second reminder to {task} for {long_when(d2.date, t1.h, t1.m)}.")
        return {"template": SF_TPL.index(tpl), "slots": {"task": task}}

    SO_TPL = ["send the same thing to {b}", "text {b} that too", "also tell {b}", "and {b} as well"]

    @family("followup", "fu_text_same_other", weight=3, failure=("followup_context_lost", "wrong_body"))
    def f_fu_text_same_other(r, c, now):
        a = G.pick_contact(r, 0.1)
        b = r.choice([p for p in PEOPLE if p != a])
        it = G.text_intent(r)
        c.hist("user", r.choice(["text {n} that {c}", "tell {n} {c}"]).format(n=fn(a), c=it["clause"]))
        c.hist("assistant", f"Sent to {fn(a)}: {Q(it['body'])}")
        tpl = r.choice(SO_TPL)
        c.user(messy(r, tpl.format(b=first(b)), protect=(first(b),)))
        c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
        c.call("lookup_contact", {"name": first(b)}, lookup_result(b))
        c.call("send_message", {"to": phone_for(b), "text": it["body"]}, G.sent_msg(c, b))
        c.final(G._fin_sent(r, "message", first(b), it["body"]))
        return {"template": SO_TPL.index(tpl), "slots": {"first": a, "second": b}}

    # ================================================================ 5. the assistant asked; the user answers
    def pair_events(r, now, pairs):
        t1, t2 = two_times(r, 8, 18)
        if (t1.h, t1.m) > (t2.h, t2.m):
            t1, t2 = t2, t1
        a, b, key = r.choice(pairs)
        return a, b, key, t1, t2

    CC_TRAIN = ["no, the {t} one", "the {t} one", "I meant the {t} one", "the {w} one", "the {e} one please"]
    CC_DEV = ["the {e} one, not the other", "neither of those - kidding, the {t} one", "go with the {w} one", "it's the {t} one"]

    def _cancel_clar(r, c, now, tpls, pairs):
        a, b, key, t1, t2 = pair_events(r, now, pairs)
        day = pd(r, now, ("tomorrow", "weekday"))
        c.hist("user", f"cancel my {key} {day.spoken}")
        c.hist("assistant", f"You have two {key} events {dw(day)}: {a} at {t1.long} and {b} at {t2.long}. Which one should I cancel?")
        pickb = r.random() < 0.5
        chosen, tm = (b, t2) if pickb else (a, t1)
        tpl = r.choice(tpls)
        timeword = f"{tm.h % 12 or 12}{'am' if tm.h < 12 else 'pm'}" if tm.m == 0 else f"{tm.h % 12 or 12}:{tm.m:02d}{'am' if tm.h < 12 else 'pm'}"
        text = tpl.format(t=timeword, w="later" if pickb else "earlier", e=chosen.split()[-1].lower())
        c.user(messy(r, text, protect=(timeword,)))
        c.discover(["calendar"], ["cancel_event"])
        c.call("cancel_event", {"title": chosen}, f"Cancelled “{chosen}”.")
        c.final(r.choice([f"Cancelled “{chosen}” ({tm.long}).", f"Done - {chosen} at {tm.long} is off your calendar. The other one is untouched."]))
        return {"template": tpls.index(tpl), "slots": {"pair": key, "chosen": chosen}}

    @family("followup", "fu_cancel_clarified", weight=3, failure=("followup_context_lost", "cancel_wrong_event"))
    def f_fu_cancel_clar(r, c, now):
        return _cancel_clar(r, c, now, CC_TRAIN, PAIRS_TRAIN)

    @family("followup", "fu_cancel_clarified_dev", split="dev", weight=2, failure=("followup_context_lost", "cancel_wrong_event"))
    def f_fu_cancel_clar_dev(r, c, now):
        return _cancel_clar(r, c, now, CC_DEV, PAIRS_DEV)

    RC_TPL = ["{d} at {t}", "make it {d} at {t}", "{t} {d}", "let's say {d}, {t}"]

    @family("followup", "fu_remind_clarified", weight=3, failure=("followup_context_lost", "guess_time"))
    def f_fu_remind_clar(r, c, now):
        task = r.choice(TASKS_TRAIN)
        c.hist("user", r.choice(["remind me to {t}", "I need a reminder to {t}"]).format(t=task))
        c.hist("assistant", r.choice(["When should I set that for?", "What day and time works for you?"]))
        day = pd(r, now, ("tomorrow", "weekday"))
        tm = pick_time(r, 7, 20, minutes=(0, 30))
        tpl = r.choice(RC_TPL)
        c.user(messy(r, tpl.format(d=day.spoken, t=tm.spoken), protect=(day.spoken, tm.spoken)))
        c.discover(["calendar"], ["add_reminder"])
        title = G.reminder_title(task)
        c.call("add_reminder", {"title": title, "when_iso": iso(day.date, tm.h, tm.m)},
               f"Reminder set: “{title}” — {long_when(day.date, tm.h, tm.m)} in Apple Reminders and Wisp.")
        c.final(f"Reminder set: {task}, {long_when(day.date, tm.h, tm.m)}.")
        return {"template": RC_TPL.index(tpl), "slots": {"task": task}}

    EC_TPL = ["{d} at {t}", "it's {d}, {t}", "{t} on {d}", "put it at {t} {d}"]

    @family("followup", "fu_event_clarified", weight=3, failure=("followup_context_lost", "guess_time"))
    def f_fu_event_clar(r, c, now):
        ev = r.choice(G.EVENT_BANK)
        title = ev[0].format(p=first(r.choice(PEOPLE)))
        c.hist("user", r.choice(["put {a} on my calendar", "add {a} to my calendar"]).format(a=title.lower() if r.random() < 0.4 else title))
        c.hist("assistant", "What day and time?")
        day = pd(r, now, ("tomorrow", "weekday"))
        tm = pick_time(r, 8, 19, minutes=(0, 30))
        tpl = r.choice(EC_TPL)
        c.user(messy(r, tpl.format(d=day.spoken, t=tm.spoken), protect=(day.spoken, tm.spoken)))
        c.discover(["calendar"], ["add_calendar_event"])
        endm = tm.h * 60 + tm.m + 60
        c.call("add_calendar_event", {"title": title, "when_iso": iso(day.date, tm.h, tm.m)},
               f"Added “{title}” to your calendar for {long_when(day.date, tm.h, tm.m)} - {t12(endm // 60, endm % 60)}.")
        c.final(f"Added “{title}” to your calendar for {long_when(day.date, tm.h, tm.m)}.")
        return {"template": EC_TPL.index(tpl), "slots": {"title": title}}

    # ================================================================ 6. answerable FROM HISTORY (no tool call)
    def agenda_loc_sentence(day, evs):
        return f"{cap(dw(day))} you have " + ", ".join(f"{e.title} at {e.tstr}" + (f" ({e.loc})" if e.loc else "") for e in evs) + "."

    HA_TRAIN = [("time", "what time is the {t}"), ("time", "when is the {t} again"), ("where", "where is the {t}?"),
                ("where", "what was the address for the {t}"), ("count", "how many things is that"), ("first", "what's the first one"),
                ("last", "which one is last")]
    HA_DEV = [("time", "sorry, when does the {t} start?"), ("where", "remind me where the {t} is"), ("first", "which is the earliest of those?"),
              ("last", "what's the final one that day?"), ("count", "so how many events altogether?")]

    def _hist_agenda(r, c, now, tpls):
        day = pd(r, now, ("tomorrow", "weekday"))
        evs = G.make_day(r, day.date, r.randint(2, 3), hmin=8, hmax=18)
        c.hist("user", r.choice(["what's on my calendar {d}", "run me through {d}"]).format(d=day.spoken))
        c.hist("assistant", agenda_loc_sentence(day, evs))
        kind, tpl = r.choice(tpls)
        if kind == "where":
            cand = [e for e in evs if e.loc]
            if not cand:
                raise AssertionError("no location")
            ev = r.choice(cand)
        else:
            ev = r.choice(evs)
        c.user(messy(r, tpl.format(t=lc_first(ev.title)), protect=(ev.title,)))
        if kind == "time":
            c.final(f"{ev.title} is at {ev.tstr}.")
        elif kind == "where":
            c.final(f"{ev.title} is at {ev.loc}.")
        elif kind == "count":
            c.final(f"{len(evs)} events {dw(day)}.")
        elif kind == "first":
            c.final(f"The first one is {evs[0].title} at {evs[0].tstr}.")
        else:
            c.final(f"The last one is {evs[-1].title} at {evs[-1].tstr}.")
        return {"template": tpls.index((kind, tpl)), "slots": {"kind": kind}}

    @family("followup", "fu_hist_agenda", weight=4, failure=("tool_on_answered_followup", "loop_tool"))
    def f_fu_hist_agenda(r, c, now):
        return _hist_agenda(r, c, now, HA_TRAIN)

    @family("followup", "fu_hist_agenda_dev", split="dev", weight=2, failure=("tool_on_answered_followup", "loop_tool"))
    def f_fu_hist_agenda_dev(r, c, now):
        return _hist_agenda(r, c, now, HA_DEV)

    HM_TPL = ["who sent the {o} one again", "what was the {o} email about", "remind me who the {o} one was from", "number {k} - what's the subject?",
              "what was {f}'s email about", "which number was {f}'s email"]

    @family("followup", "fu_hist_mail", weight=3, failure=("tool_on_answered_followup", "loop_tool"))
    def f_fu_hist_mail(r, c, now):
        picks = mail_hist(r, c)
        idx, word, k, (s, subj) = ordinal_phrase(r, picks)
        tpl = r.choice(HM_TPL)
        c.user(messy(r, tpl.format(o=word, k=k, f=first(s)), protect=(first(s),)))
        c.final(r.choice([f"The {word} one is “{subj}”, from {s}.", f"That's {s} - “{subj}”."]))
        return {"template": HM_TPL.index(tpl), "slots": {"ordinal": word}}

    HW_TPL = [("umbrella", "do I need an umbrella in {c}"), ("umbrella", "should I bring an umbrella then"), ("high", "how warm does it get"),
              ("low", "and the low?"), ("rain", "is it going to rain in {c}")]

    @family("followup", "fu_hist_weather", weight=3, failure=("tool_on_answered_followup", "loop_tool"))
    def f_fu_hist_weather(r, c, now):
        cq, cname = r.choice(G.CITIES)
        day = G.Day("today", now.date(), "today", now)
        _, rain, (hi, lo, cond, pct) = G.weather_result(r, cq, day)
        c.hist("user", f"what's the weather in {cname}")
        c.hist("assistant", f"{cname} today: {cond.lower()}, high {hi}F, low {lo}F, {pct}% chance of rain.")
        kind, tpl = r.choice(HW_TPL)
        c.user(messy(r, tpl.format(c=cname), protect=(cname,)))
        if kind == "umbrella":
            c.final(f"Probably - there's a {pct}% chance of rain." if pct >= 40 else f"Probably not - only a {pct}% chance of rain.")
        elif kind == "rain":
            c.final(f"There's a {pct}% chance of rain, with {cond.lower()} expected." if pct >= 40 else f"Unlikely - just a {pct}% chance of rain.")
        elif kind == "high":
            c.final(f"The high in {cname} is {hi}F.")
        else:
            c.final(f"The low is {lo}F.")
        return {"template": HW_TPL.index((kind, tpl)), "slots": {"city": cname}}

    HC_TPL = [("email", "what was {n}'s email again"), ("phone", "and the number?"), ("phone", "read {n}'s number back to me"), ("email", "what email did you say for {n}")]

    @family("followup", "fu_hist_contact", weight=3, failure=("tool_on_answered_followup", "loop_tool"))
    def f_fu_hist_contact(r, c, now):
        nm = r.choice(PEOPLE)
        c.hist("user", f"what's {first(nm)}'s number and email")
        c.hist("assistant", f"{nm}: {phone_for(nm)}, {email_for(nm)}.")
        kind, tpl = r.choice(HC_TPL)
        c.user(messy(r, tpl.format(n=first(nm)), protect=(first(nm),)))
        c.final(f"{first(nm)}'s email is {email_for(nm)}." if kind == "email" else f"{first(nm)}'s number is {phone_for(nm)}.")
        return {"template": HC_TPL.index((kind, tpl)), "slots": {"contact": nm}}

    # ================================================================ 7. the earlier answer does not cover it: fresh read
    FA_TPL = ["where's the {t}?", "what's the address for the {t}", "where do I go for the {t}"]

    @family("followup", "fu_fresh_agenda_detail", weight=3, failure=("followup_context_lost", "invented_detail"))
    def f_fu_fresh_agenda(r, c, now):
        day = pd(r, now, ("tomorrow", "weekday"))
        evs = G.make_day(r, day.date, r.randint(2, 3), hmin=8, hmax=18)
        cand = [e for e in evs if e.loc]
        if not cand:
            raise AssertionError("no location")
        ev = r.choice(cand)
        c.hist("user", r.choice(["what's on my calendar {d}", "run me through {d}"]).format(d=day.spoken))
        c.hist("assistant", G._agenda_sentence(now, day, evs))
        tpl = r.choice(FA_TPL)
        c.user(messy(r, tpl.format(t=lc_first(ev.title)), protect=(ev.title,)))
        c.discover(["calendar"], ["get_upcoming"])
        c.call("get_upcoming", {"period": day.period, "query": ev.key}, G.agenda_result(now, day.period if day.kind == "date" else day.kind, [ev]))
        c.final(f"{ev.title} is at {ev.loc}, starting {ev.tstr}.")
        return {"template": FA_TPL.index(tpl), "slots": {"event": ev.key}}

    FM_TPL = ["open the {o} one", "what does the {o} email say", "read me number {k}", "what did the {o} one say?",
              "open the one from {f}", "what does {f}'s email say", "read me what {f} sent"]

    @family("followup", "fu_fresh_mail_body", weight=3, failure=("followup_context_lost", "invented_detail"))
    def f_fu_fresh_mail(r, c, now):
        picks = mail_hist(r, c)
        idx, word, k, (s, subj) = ordinal_phrase(r, picks)
        tpl = r.choice(FM_TPL)
        c.user(messy(r, tpl.format(o=word, k=k, f=first(s)), protect=(first(s),)))
        c.discover(["mail"], ["view_emails"])
        body = r.choice(BODY_SNIPS)
        day = now.date() - dt.timedelta(days=r.randint(0, 3))
        c.call("view_emails", {"query": subj, "count": 3, "strict_match": True}, G._mail_listing(s, subj, G._msgid(r), G.dshort(day), body))
        c.final(f"{first(s)} wrote: {Q(body)}")
        return {"template": FM_TPL.index(tpl), "slots": {"ordinal": word}}

    CD_TPL = ["and their email?", "what about {n}'s email address", "email too?", "and the email for {n}?"]

    @family("followup", "fu_contact_detail", weight=3, failure=("followup_context_lost", "invented_detail"))
    def f_fu_contact_detail(r, c, now):
        nm = r.choice(PEOPLE)
        c.hist("user", f"what's {first(nm)}'s number")
        c.hist("assistant", f"{nm}: {phone_for(nm)}.")
        tpl = r.choice(CD_TPL)
        c.user(messy(r, tpl.format(n=first(nm)), protect=(first(nm),)))
        c.discover(["messages_contacts"], ["lookup_contact"])
        c.call("lookup_contact", {"name": first(nm)}, lookup_result(nm))
        c.final(f"{first(nm)}'s email is {email_for(nm)}.")
        return {"template": CD_TPL.index(tpl), "slots": {"contact": nm}}

    # ================================================================ 8. topic switch: history must be ignored
    TS_PRE = ["totally different thing: {q}", "unrelated - {q}", "ok never mind all that, {q}", "different question: {q}"]

    def switch_history(r, c, now):
        k = r.choice(["sent", "agenda", "mail"])
        if k == "sent":
            nm = r.choice(PEOPLE)
            it = G.text_intent(r)
            c.hist("user", f"text {first(nm)} that {it['clause']}")
            c.hist("assistant", f"Sent to {first(nm)}: {Q(it['body'])}")
        elif k == "agenda":
            d = pd(r, now, ("tomorrow",))
            evs = G.make_day(r, d.date, 2, hmin=9, hmax=17)
            c.hist("user", "what's on my calendar tomorrow")
            c.hist("assistant", G._agenda_sentence(now, d, evs))
        else:
            mail_hist(r, c)
        return k

    TS_KINDS = ["timer", "agenda", "reminder", "weather"]

    @family("followup", "fu_topic_switch", weight=4, failure=("followup_context_lost", "stale_history_applied"))
    def f_fu_topic_switch(r, c, now):
        switch_history(r, c, now)
        kind = r.choice(TS_KINDS)
        pre = r.choice(TS_PRE)
        if kind == "timer":
            x = r.choice([5, 10, 15, 20, 25])
            c.user(messy(r, pre.format(q=f"set a timer for {x} minutes")))
            c.discover(["automation"], ["set_timer"])
            c.call("set_timer", {"duration": f"{x} minutes"}, f"Timer set for {x} minutes.")
            c.final(f"Your {x}-minute timer is running.")
        elif kind == "agenda":
            d = pd(r, now, ("tomorrow", "weekday"))
            c.user(messy(r, pre.format(q=f"what's on my calendar {d.spoken}"), protect=(d.spoken,)))
            c.discover(["calendar"], ["get_upcoming"])
            ev = G.make_day(r, d.date, r.randint(1, 3))
            c.call("get_upcoming", {"period": d.period}, G.agenda_result(now, d.period, ev))
            c.final(G._agenda_sentence(now, d, ev))
        elif kind == "reminder":
            task = r.choice(TASKS_TRAIN)
            d = pd(r, now, ("tomorrow", "weekday"))
            tm = pick_time(r, 7, 20, minutes=(0, 30))
            c.user(messy(r, pre.format(q=f"remind me to {task} {d.spoken} at {tm.spoken}"), protect=(task, d.spoken, tm.spoken)))
            c.discover(["calendar"], ["add_reminder"])
            title = G.reminder_title(task)
            c.call("add_reminder", {"title": title, "when_iso": iso(d.date, tm.h, tm.m)},
                   f"Reminder set: “{title}” — {long_when(d.date, tm.h, tm.m)} in Apple Reminders and Wisp.")
            c.final(f"Reminder set: {task}, {long_when(d.date, tm.h, tm.m)}.")
        else:
            cq, cname = r.choice(G.CITIES)
            c.user(messy(r, pre.format(q=f"what's the weather in {cname} tomorrow"), protect=(cname,)))
            c.discover(["public_web"], ["get_weather"])
            tmr = G.Day("tomorrow", now.date() + dt.timedelta(days=1), "tomorrow", now)
            w, rain, (hi, lo, cond, pct) = G.weather_result(r, cq, tmr)
            c.call("get_weather", {"location": cq, "period": "tomorrow"}, w)
            c.final(f"{cname} tomorrow: {cond.lower()}, high {hi}F and low {lo}F, with a {pct}% chance of rain.")
        return {"template": TS_PRE.index(pre), "slots": {"kind": kind}}

    # ================================================================ 9. after a declined send
    DECL = ["I didn't send it - you declined, so nothing went to {n}.", "Understood. The text to {n} was not sent."]
    DA_TPL = [("ack", "ok forget it, don't text {n}"), ("did", "did that go through to {n}?"), ("did", "so {n} never got it?"),
              ("again", "should I try {n} again?")]

    @family("followup", "fu_declined_ack", weight=3, failure=("resend_after_denial", "false_sent_claim"))
    def f_fu_declined_ack(r, c, now):
        nm = r.choice(PEOPLE)
        it = G.text_intent(r)
        c.hist("user", f"text {first(nm)} that {it['clause']}")
        c.hist("assistant", r.choice(DECL).format(n=first(nm)))
        kind, tpl = r.choice(DA_TPL)
        c.user(messy(r, tpl.format(n=first(nm)), protect=(first(nm),)))
        if kind == "ack":
            c.final(f"Okay, nothing was sent to {first(nm)}.")
        elif kind == "did":
            c.final(f"No - you declined it, so nothing was sent to {first(nm)}.")
        else:
            c.final(f"Nothing went out to {first(nm)} - you declined the send. Tell me if you want me to send it.")
        return {"template": DA_TPL.index((kind, tpl)), "slots": {"contact": nm, "kind": kind}}

    DS_TPL = ["ok then email it to {n} instead", "fine, send it to {n} by email", "email {n} that instead"]

    @family("followup", "fu_declined_switch", weight=3, failure=("resend_after_denial", "followup_context_lost"))
    def f_fu_declined_switch(r, c, now):
        nm = r.choice(PEOPLE)
        it = G.text_intent(r)
        c.hist("user", f"text {first(nm)} that {it['clause']}")
        c.hist("assistant", r.choice(DECL).format(n=first(nm)))
        tpl = r.choice(DS_TPL)
        c.user(messy(r, tpl.format(n=first(nm)), protect=(first(nm),)))
        c.discover(["mail", "messages_contacts"], ["lookup_contact", "send_email"])
        c.call("lookup_contact", {"name": first(nm)}, lookup_result(nm, via="email"))
        body = EMAIL_FRAME.format(first=first(nm), content=it["body"])
        c.call("send_email", {"to": email_for(nm), "subject": "Message", "body": body}, G.sent_email(c, email_for(nm)))
        c.final(G._fin_sent(r, "email", first(nm), body))
        return {"template": DS_TPL.index(tpl), "slots": {"contact": nm}}
