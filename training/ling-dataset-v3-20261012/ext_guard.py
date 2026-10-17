"""Repeated-read-guard families (Wisp 1.3.2): after a read ran, the model re-issues it with identical arguments, Wisp's
guard refuses ("already ran ... NOT run again"), and the target is the RECOVERY step: answer from the earlier result,
call with genuinely different arguments, or move on to the next tool. The guarded repeat itself is context only
(Conv.repeat_read) and is never a training target. Only non-volatile reads are used (never Wisp's _VOLATILE_READS)."""

CAT = "guard"
FAIL = ("repeated_read_guard",)

_NAMES = ("family", "pick_day", "clean_day", "make_day", "make_event", "agenda_result", "dw", "cap", "t12", "dshort",
          "WEEKDAYS", "messy", "first", "lc_first", "EMAIL_SUBJ_BANK", "NOTE_BANK", "REMINDER_BANK", "CITIES",
          "weather_result", "_mail_listing", "_msgid", "pick_contact", "name_forms", "_name_arg", "lookup_result",
          "phone_for", "sent_msg", "_fin_sent", "Q", "ev_when", "RELATIVES", "PEOPLE", "dt", "NOTE_DEV_KEYS", "CUR")

WEEKDAY_FREE = None


def register(G):
    g = {n: getattr(G, n) for n in _NAMES}
    globals().update(g)
    # ------------------------------------------------------------------ shared pieces
    def _notes(dev):
        return [n for n in NOTE_BANK if (n[2] in NOTE_DEV_KEYS) == dev]

    def _two_days(r, now):
        d1 = clean_day(pick_day(r, now, kinds=("tomorrow", "weekday", "weekday")))
        for _ in range(50):
            d2 = clean_day(pick_day(r, now, kinds=("tomorrow", "weekday", "weekday")))
            if d2.date != d1.date:
                break
        if d2.date < d1.date:
            d1, d2 = d2, d1
        return d1, d2

    def _same_spoken(d1, d2):
        return d1.spoken.lower() == d2.spoken.lower()

    # ------------------------------------------------------------------ 1. calendar read -> guard -> answer
    CAL_TPL = [("start", "what's my first meeting {d}"), ("start", "when does my day start {d}"),
               ("count", "how many things are on my calendar {d}"), ("count", "how busy am I {d}"),
               ("event", "what time is my {a}"), ("event", "when's the {a}"),
               ("end", "what's my last meeting {d}"), ("end", "when am I done {d}")]
    CAL_DEV = [("start", "what's the earliest thing on my plate {d}"), ("count", "give me the number of events I have {d}"),
               ("event", "remind me what time the {a} is"), ("end", "which item closes out my day {d}")]

    def _cal_answer(r, c, now, tpls):
        kind, tpl = r.choice(tpls)
        if kind == "event":
            d = now.date() + dt.timedelta(days=r.randint(1, 12))
            ev = make_event(r, d, h=r.randint(9, 17), m=r.choice([0, 30]))
            c.user(messy(r, tpl.format(a=ev.alias), protect=(ev.alias,)))
            c.discover(["calendar"], ["get_upcoming"])
            args = {"days": 14, "query": ev.key}
            c.call("get_upcoming", args, agenda_result(now, "next 14 day(s)", [ev]))
            c.repeat_read("get_upcoming", args)
            c.final(f"Your {ev.alias} is {WEEKDAYS[d.weekday()]}, {dshort(d).split(', ')[1]} at {ev.tstr}"
                    + (f" ({ev.loc})." if ev.loc else "."))
            return {"template": tpls.index((kind, tpl)), "slots": {"kind": kind}}
        day = pick_day(r, now, kinds=("tomorrow", "weekday", "weekday"))
        n = r.randint(2, 4)
        evs = make_day(r, day.date, n)
        c.user(messy(r, tpl.format(d=day.spoken)))
        c.discover(["calendar"], ["get_upcoming"])
        args = {"period": day.period}
        c.call("get_upcoming", args, agenda_result(now, day.period if day.kind == "date" else day.kind, evs))
        c.repeat_read("get_upcoming", args)
        if kind == "start":
            e = evs[0]
            c.final(f"{cap(dw(day))} you start with {e.title} at {e.tstr}" + (f" ({e.loc})" if e.loc else "") + ".")
        elif kind == "end":
            e = evs[-1]
            c.final(f"Your last item {dw(day)} is {e.title} at {e.tstr}" + (f" ({e.loc})" if e.loc else "") + ".")
        else:
            c.final(f"You have {n} items {dw(day)}: " + ", ".join(f"{e.title} at {e.tstr}" for e in evs) + ".")
        return {"template": tpls.index((kind, tpl)), "slots": {"kind": kind, "n": n}}

    @family(CAT, "gd_cal_answer", weight=10, failure=FAIL)
    def f_gd_cal_answer(r, c, now):
        return _cal_answer(r, c, now, CAL_TPL)

    @family(CAT, "gd_cal_answer_dev", split="dev", weight=4, failure=FAIL)
    def f_gd_cal_answer_dev(r, c, now):
        return _cal_answer(r, c, now, CAL_DEV)

    # ------------------------------------------------------------------ 2. notes / reminder / mail read -> guard -> answer
    INFO_TPL = [("note", "find my note about {k}"), ("note", "what did I write down about {k}"), ("note", "pull up the {k} note"),
                ("rem", "do I have a reminder about {k}"), ("rem", "when's my reminder to {k}"),
                ("mail", "what did {s}'s last email say"), ("mail", "has {s} emailed me lately"),
                ("mail", "show me the latest email from {s}")]

    @family(CAT, "gd_info_answer", weight=10, failure=FAIL)
    def f_gd_info_answer(r, c, now):
        kind, tpl = r.choice(INFO_TPL)
        if kind == "note":
            title, body, key = r.choice(_notes(False))
            c.user(messy(r, tpl.format(k=key)))
            c.discover(["notes"], ["search_notes"])
            args = {"query": key}
            c.call("search_notes", args, f"1 note: {title}\n{body}")
            c.repeat_read("search_notes", args)
            c.final(r.choice([f"Your note “{title}” says: {body}.", f"Found “{title}”: {body}."]))
        elif kind == "rem":
            rem = r.choice(REMINDER_BANK)
            when = now.date() + dt.timedelta(days=r.randint(0, 5))
            h = r.randint(8, 18)
            arg = rem if "to {k}" in tpl else rem.split()[-1]
            c.user(messy(r, tpl.format(k=arg)))
            c.discover(["calendar"], ["search_reminders"])
            args = {"query": arg, "scope": "all"}
            c.call("search_reminders", args, f"1 active reminder: {rem} (due {dshort(when)}, {t12(h, 0)})")
            c.repeat_read("search_reminders", args)
            c.final(f"Yes - “{rem}” is due {dshort(when)} at {t12(h, 0)}.")
        else:
            s, subj = r.choice(EMAIL_SUBJ_BANK)
            c.user(messy(r, tpl.format(s=first(s)), protect=(first(s),)))
            c.discover(["mail"], ["view_emails"])
            d = now.date() - dt.timedelta(days=r.randint(0, 3))
            body = r.choice(["Can you confirm Thursday works?", "The files are in the shared folder now.",
                             "Reminder that the deadline is next Wednesday.", "Just checking you saw my last note."])
            args = {"query": first(s), "count": 1}
            c.call("view_emails", args, _mail_listing(s, subj, _msgid(r), f"{dshort(d)}, {t12(r.randint(7, 17), r.choice([0, 20, 40]))}", body))
            c.repeat_read("view_emails", args)
            c.final(f"{first(s)}'s latest email, “{subj}” ({dshort(d)}), says: “{body}”")
        return {"template": INFO_TPL.index((kind, tpl)), "slots": {"kind": kind}}

    # ------------------------------------------------------------------ 3. empty result -> guard -> honest "nothing"
    EMP_TPL = [("cal", "anything on my calendar {d}"), ("cal", "is there anything scheduled {d}"),
               ("mail", "any unread emails"), ("mail", "got any new email from {s}"),
               ("notes", "do I have a note about {k}"), ("notes", "find my {k} note"),
               ("rem", "do I have a reminder about {k}"), ("rem", "any reminder to {k}")]
    EMP_DEV = [("cal", "anything booked {d}"), ("mail", "is there fresh mail I haven't read"), ("notes", "did I save anything about {k}"),
               ("rem", "is a {k} reminder set anywhere")]

    def _empty(r, c, now, tpls):
        kind, tpl = r.choice(tpls)
        s, _ = r.choice(EMAIL_SUBJ_BANK)
        k = r.choice(["passport", "taxes", "gym", "invoice", "garage", "plumber", "lease", "visa", "flight", "warranty"])
        day = pick_day(r, now, kinds=("tomorrow", "weekday", "today"))
        c.user(messy(r, tpl.format(d=day.spoken, s=first(s), k=k), protect=(first(s),)))
        dev = CUR["split"] == "dev"
        if kind == "cal":
            c.discover(["calendar"], ["get_upcoming"])
            args = {"period": day.period}
            c.call("get_upcoming", args, f"Today is {dshort(now.date())}. Nothing scheduled for {day.period if day.kind == 'date' else day.kind}.")
            c.repeat_read("get_upcoming", args)
            c.final(f"Nothing is booked {dw(day)}." if dev else f"Nothing - your calendar is clear {dw(day)}.")
        elif kind == "mail":
            c.discover(["mail"], ["view_emails"])
            if "{s}" in tpl:
                args = {"query": first(s), "count": 5, "strict_match": True}
                c.call("view_emails", args, f"No emails matched {first(s)!r}.")
                c.repeat_read("view_emails", args)
                c.final(f"I didn't find any emails matching {first(s)}.")
            else:
                args = {"unread": True, "count": 5}
                c.call("view_emails", args, "No unread emails.")
                c.repeat_read("view_emails", args)
                c.final("No fresh mail - everything is read." if dev else "You have no unread emails.")
        elif kind == "notes":
            c.discover(["notes"], ["search_notes"])
            args = {"query": k}
            c.call("search_notes", args, f"No notes matched {k!r}.")
            c.repeat_read("search_notes", args)
            c.final(f"Nothing saved about {k} in your notes." if dev else f"I couldn't find a note about {k}.")
        else:
            c.discover(["calendar"], ["search_reminders"])
            args = {"query": k, "scope": "all"}
            c.call("search_reminders", args, f"No active reminders match {k!r}.")
            c.repeat_read("search_reminders", args)
            c.final(f"No active reminder about {k} turned up." if dev else f"No - you don't have an active reminder about {k}.")
        return {"template": tpls.index((kind, tpl)), "slots": {"kind": kind}}

    @family(CAT, "gd_empty", weight=8, failure=FAIL + ("empty_result_retry_loop",))
    def f_gd_empty(r, c, now):
        return _empty(r, c, now, EMP_TPL)

    @family(CAT, "gd_empty_dev", split="dev", weight=3, failure=FAIL + ("empty_result_retry_loop",))
    def f_gd_empty_dev(r, c, now):
        return _empty(r, c, now, EMP_DEV)

    # ------------------------------------------------------------------ 4. user says "check again" -> guard -> answer, say so
    AGAIN = [("cal", "what's on my calendar {d}? check it twice to be sure"), ("cal", "are you sure about my first meeting {d}? check again"),
             ("cal", "double check how many events I have {d}"),
             ("note", "look up my {k} note again to make sure it's right"), ("note", "find my note about {k} - and check it a second time"),
             ("rem", "check again whether I have a reminder about {k}"), ("rem", "are you sure there's a reminder to {k}? look again"),
             ("mail", "check the latest email from {s} again, are you sure what it says")]

    @family(CAT, "gd_check_again", weight=8, failure=FAIL + ("loop_exact",))
    def f_gd_check_again(r, c, now):
        kind, tpl = r.choice(AGAIN)
        pre = r.choice(["I already checked this turn and the result hasn't changed: ", "That lookup already ran this turn, so going by it: ",
                        "I've already checked just now, and it shows: "])
        if kind == "cal":
            day = pick_day(r, now, kinds=("tomorrow", "weekday", "weekday"))
            evs = make_day(r, day.date, r.randint(2, 3))
            c.user(messy(r, tpl.format(d=day.spoken)))
            c.discover(["calendar"], ["get_upcoming"])
            args = {"period": day.period}
            c.call("get_upcoming", args, agenda_result(now, day.period if day.kind == "date" else day.kind, evs))
            c.repeat_read("get_upcoming", args)
            if "first" in tpl:
                body = f"{evs[0].title} at {evs[0].tstr} is first {dw(day)}."
            elif "how many" in tpl:
                body = f"{len(evs)} events {dw(day)}: " + ", ".join(f"{e.title} at {e.tstr}" for e in evs) + "."
            else:
                body = ", ".join(f"{e.title} at {e.tstr}" for e in evs) + f" {dw(day)}."
            c.final(pre + body)
        elif kind == "note":
            title, nbody, key = r.choice(_notes(False))
            c.user(messy(r, tpl.format(k=key)))
            c.discover(["notes"], ["search_notes"])
            args = {"query": key}
            c.call("search_notes", args, f"1 note: {title}\n{nbody}")
            c.repeat_read("search_notes", args)
            c.final(pre + f"“{title}” says {nbody}.")
        elif kind == "rem":
            rem = r.choice(REMINDER_BANK)
            when = now.date() + dt.timedelta(days=r.randint(0, 5))
            h = r.randint(8, 18)
            arg = rem if "to {k}" in tpl else rem.split()[-1]
            c.user(messy(r, tpl.format(k=arg)))
            c.discover(["calendar"], ["search_reminders"])
            args = {"query": arg, "scope": "all"}
            c.call("search_reminders", args, f"1 active reminder: {rem} (due {dshort(when)}, {t12(h, 0)})")
            c.repeat_read("search_reminders", args)
            c.final(pre + f"“{rem}” is due {dshort(when)} at {t12(h, 0)}.")
        else:
            s, subj = r.choice(EMAIL_SUBJ_BANK)
            c.user(messy(r, tpl.format(s=first(s)), protect=(first(s),)))
            c.discover(["mail"], ["view_emails"])
            d = now.date() - dt.timedelta(days=r.randint(0, 3))
            mb = r.choice(["The files are in the shared folder now.", "Can you confirm Thursday works?"])
            args = {"query": first(s), "count": 1}
            c.call("view_emails", args, _mail_listing(s, subj, _msgid(r), f"{dshort(d)}, {t12(r.randint(7, 17), 20)}", mb))
            c.repeat_read("view_emails", args)
            c.final(pre + f"{first(s)}'s email “{subj}” says: “{mb}”")
        return {"template": AGAIN.index((kind, tpl)), "slots": {"kind": kind}}

    # ------------------------------------------------------------------ 5. chains: read -> guard -> lookup -> send -> final
    CC_TPL = ["text {n} what time my {a} is {d}", "let {n} know when my {a} is {d}", "find my {a} {d} and text {n} when it starts",
              "message {n} the start time of my {a} {d}"]
    CC_DEV = ["{n} asked about my {a} {d} - look it up and text them", "send {n} a text with the time for my {a} {d}"]

    def _cal_chain(r, c, now, tpls):
        nm = pick_contact(r, 0.1)
        said, _ = name_forms(r, nm)
        day = pick_day(r, now, kinds=("tomorrow", "weekday"))
        ev = make_event(r, day.date, h=r.randint(9, 18), m=r.choice([0, 30]))
        tpl = r.choice(tpls)
        c.user(messy(r, tpl.format(n=said, a=ev.alias, d=day.spoken), protect=(said, ev.alias)))
        c.discover(["calendar", "messages_contacts"], ["get_upcoming", "lookup_contact", "send_message"])
        res_ev = agenda_result(now, day.period if day.kind == "date" else day.kind, [ev])
        args = {"period": day.period, "query": ev.key}
        parallel = r.random() < 0.4
        lk = ("lookup_contact", {"name": _name_arg(said, nm)}, lookup_result(nm))
        if parallel:
            c.calls([("get_upcoming", args, res_ev), lk])
            c.repeat_read("get_upcoming", args)
        else:
            c.call("get_upcoming", args, res_ev)
            c.repeat_read("get_upcoming", args)
            c.call(*lk)
        body = f"My {lc_first(ev.title)} is {ev_when(now, ev)}."
        c.call("send_message", {"to": phone_for(nm), "text": body}, sent_msg(c, nm))
        f = nm if nm in RELATIVES else first(nm)
        c.final(_fin_sent(r, "message", f, body))
        return {"template": tpls.index(tpl), "slots": {"event": ev.key, "contact": nm, "parallel": parallel}}

    @family(CAT, "gd_chain_cal_text", weight=7, failure=FAIL + ("compound_never_completes",))
    def f_gd_chain_cal_text(r, c, now):
        return _cal_chain(r, c, now, CC_TPL)

    @family(CAT, "gd_chain_cal_text_dev", split="dev", weight=3, failure=FAIL + ("compound_never_completes",))
    def f_gd_chain_cal_text_dev(r, c, now):
        return _cal_chain(r, c, now, CC_DEV)

    MC_TPL = ["find the latest email from {s} and text {n} what it's about", "check {s}'s last email and tell {n} by text what it covers",
              "look up the newest email from {s}, then text {n} the topic"]

    @family(CAT, "gd_chain_mail_text", weight=5, failure=FAIL + ("compound_never_completes",))
    def f_gd_chain_mail_text(r, c, now):
        s, subj = r.choice(EMAIL_SUBJ_BANK)
        nm = pick_contact(r, 0.1)
        for _ in range(20):
            if nm != s:
                break
            nm = pick_contact(r, 0.1)
        said, _ = name_forms(r, nm)
        tpl = r.choice(MC_TPL)
        c.user(messy(r, tpl.format(s=first(s), n=said), protect=(said, first(s))))
        c.discover(["mail", "messages_contacts"], ["view_emails", "lookup_contact", "send_message"])
        d = now.date() - dt.timedelta(days=r.randint(0, 3))
        args = {"query": first(s), "count": 1}
        c.call("view_emails", args, _mail_listing(s, subj, _msgid(r), f"{dshort(d)}, {t12(r.randint(7, 17), 20)}",
                                                   r.choice(["Please take a look when you can.", "Details are in the shared folder."])))
        c.repeat_read("view_emails", args)
        c.call("lookup_contact", {"name": _name_arg(said, nm)}, lookup_result(nm))
        body = f"The email from {first(s)} is about {subj}."
        c.call("send_message", {"to": phone_for(nm), "text": body}, sent_msg(c, nm))
        f = nm if nm in RELATIVES else first(nm)
        c.final(_fin_sent(r, "message", f, body))
        return {"template": MC_TPL.index(tpl), "slots": {"sender": s, "contact": nm}}

    # ------------------------------------------------------------------ 6. different data needed -> guard -> different arguments
    DD_TPL = ["what's on my calendar {a} and {b}", "do I have anything {a} or {b}", "how does my schedule look {a} versus {b}",
              "check my calendar for {a} and also {b}"]
    DD_DEV = ["compare my agenda {a} with {b}", "tell me what's booked {a} as well as {b}"]

    def _diff_days(r, c, now, tpls):
        for _ in range(30):
            d1, d2 = _two_days(r, now)
            if not _same_spoken(d1, d2):
                break
        tpl = r.choice(tpls)
        c.user(messy(r, tpl.format(a=d1.spoken, b=d2.spoken)))
        c.discover(["calendar"], ["get_upcoming"])
        e1 = make_day(r, d1.date, r.randint(1, 3))
        e2 = make_day(r, d2.date, r.randint(1, 3))
        a1 = {"period": d1.period}
        c.call("get_upcoming", a1, agenda_result(now, d1.period if d1.kind == "date" else d1.kind, e1))
        c.repeat_read("get_upcoming", a1)
        a2 = {"period": d2.period}
        c.call("get_upcoming", a2, agenda_result(now, d2.period if d2.kind == "date" else d2.kind, e2))
        fmt = lambda es: ", ".join(f"{e.title} at {e.tstr}" for e in es)
        c.final(f"{cap(dw(d1))}: {fmt(e1)}. {cap(dw(d2))}: {fmt(e2)}.")
        return {"template": tpls.index(tpl), "slots": {"d1": d1.kind, "d2": d2.kind}}

    @family(CAT, "gd_diff_days", weight=6, failure=FAIL)
    def f_gd_diff_days(r, c, now):
        return _diff_days(r, c, now, DD_TPL)

    WD_TPL = ["what's the weather in {a} and {b}", "compare the weather in {a} with {b}", "is it nicer in {a} or {b}"]

    @family(CAT, "gd_diff_weather_dev", split="dev", weight=3, failure=FAIL, web=True)
    def f_gd_diff_weather_dev(r, c, now):
        (q1, n1), (q2, n2) = r.sample(CITIES, 2)
        tpl = r.choice(WD_TPL)
        c.user(messy(r, tpl.format(a=n1, b=n2), protect=(n1, n2)))
        c.discover(["public_web"], ["get_weather"])
        day = G.Day("today", now.date(), "today", now)
        w1, _, (h1, l1, cd1, p1) = weather_result(r, q1, day)
        w2, _, (h2, l2, cd2, p2) = weather_result(r, q2, day)
        a1 = {"location": q1}
        c.call("get_weather", a1, w1)
        c.repeat_read("get_weather", a1)
        c.call("get_weather", {"location": q2}, w2)
        c.final(f"{n1}: {cd1.lower()}, high {h1}F, {p1}% chance of rain. {n2}: {cd2.lower()}, high {h2}F, {p2}% chance of rain.")
        return {"template": WD_TPL.index(tpl), "slots": {"c1": n1, "c2": n2}}

    # ------------------------------------------------------------------ 7. different tool after the guard
    OT_TPL = ["what do I have {d}, and do I have a reminder to {k}", "check my calendar {d} and also look for a reminder about {k}",
              "anything scheduled {d}? and is there a reminder to {k}"]

    @family(CAT, "gd_other_tool", weight=6, failure=FAIL)
    def f_gd_other_tool(r, c, now):
        tpl = r.choice(OT_TPL)
        day = pick_day(r, now, kinds=("tomorrow", "weekday", "weekday"))
        rem = r.choice(REMINDER_BANK)
        evs = make_day(r, day.date, r.randint(1, 3))
        c.user(messy(r, tpl.format(d=day.spoken, k=rem)))
        c.discover(["calendar"], ["get_upcoming", "search_reminders"])
        a1 = {"period": day.period}
        c.call("get_upcoming", a1, agenda_result(now, day.period if day.kind == "date" else day.kind, evs))
        c.repeat_read("get_upcoming", a1)
        has = r.random() < 0.5
        when = now.date() + dt.timedelta(days=r.randint(0, 5))
        h = r.randint(8, 18)
        c.call("search_reminders", {"query": rem, "scope": "all"},
               f"1 active reminder: {rem} (due {dshort(when)}, {t12(h, 0)})" if has else f"No active reminders match {rem!r}.")
        cal = f"{cap(dw(day))} you have " + ", ".join(f"{e.title} at {e.tstr}" for e in evs) + "."
        c.final(cal + (f" And “{rem}” is due {dshort(when)} at {t12(h, 0)}." if has else f" There's no active reminder to {rem}."))
        return {"template": OT_TPL.index(tpl), "slots": {"has_reminder": has}}
