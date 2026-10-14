"""V3.1 routing families (category key "routing"): reminder-vs-alarm-vs-task, calendar look-alikes and follow-up acceptance.

V3 failures (eval 2026-10-13): "ping me at 6 to ..." -> set_alarm, "remind me later to ..." -> schedule_task, "actually make it 6" ->
set_alarm / add_reminder instead of update_reminder; "what's my day look like" -> predict_my_day; "block off 2-4 for focus" ->
plan_meeting + commit_private_meeting; "sure / ok / sounds good" after an OFFER re-read (search_reminders, view_emails x8) instead of
doing the offered write; a bare channel answer ("email" / "text") to "text or email?" looped.

Gold behaviour here: casual reminder wording -> add_reminder (a default time is chosen and STATED when the user gave only a part of
the day); "make it 6" after a reminder -> update_reminder on that reminder; explicit alarm / wake wording -> set_alarm; a day overview
-> get_upcoming; a solo time block -> add_calendar_event; an accepted offer -> exactly the offered write with the offered details
(lookup_contact first for a send); a declined offer -> no tool, a short acknowledgement; a channel answer -> lookup_contact + the
matching send with the content from history. schedule_task and plan_meeting never appear as targets.

All wording is original (the V3 failing prompts are held out). Short acceptances avoid the exact held-out strings ("sure", "ok",
"sounds good", "yes please", "yeah do it", "email", "text", "nah"). Only the user's own words reach titles and send bodies
(validator write_fidelity / send_body_fidelity); ISO times follow the clock words in the user's last message (slot_consistency).
"""
import datetime as dt

CAT = "routing"

_NAMES = ("family", "messy", "pick_contact", "lookup_result", "phone_for", "email_for", "sent_msg", "sent_email", "_fin_sent", "Q",
          "first", "cap", "PEOPLE", "RELATIVES", "text_intent", "email_intent", "EMAIL_FRAME", "clean_day", "pick_day",
          "pick_time", "iso", "long_when", "reminder_title", "dshort", "t12", "agenda_result", "make_event", "make_day",
          "WEEKDAYS", "CUR", "REMINDER_BANK", "dw", "next_weekday", "dlabel")


def register(G):
    globals().update({n: getattr(G, n) for n in _NAMES})

    # ================================================================ shared helpers
    def is_dev():
        return CUR["split"] == "dev"

    def fn_(nm):
        return nm if nm in RELATIVES else first(nm)

    def wp(now, d, h, m=0):
        """Spoken 'when': tonight at 8 PM / tomorrow at 9 AM / Friday at 9 AM."""
        tt = t12(h, m)
        if d == now.date():
            return f"tonight at {tt}" if h >= 19 else f"today at {tt}"
        if d == now.date() + dt.timedelta(days=1):
            return f"tomorrow at {tt}"
        return f"{WEEKDAYS[d.weekday()]} at {tt}"

    def clock_date(now, h, m, gap=10):
        """Next date on which h:m is at least `gap` minutes ahead."""
        t = dt.datetime.combine(now.date(), dt.time(h, m))
        return now.date() if t >= now + dt.timedelta(minutes=gap) else now.date() + dt.timedelta(days=1)

    def bare_ok(now, h):
        """A bare '6' resolves to hour h today (the next 6 o'clock after now is h)."""
        b = h % 12
        for hh in sorted({b, b + 12}):
            if dt.datetime.combine(now.date(), dt.time(hh, 0)) >= now + dt.timedelta(minutes=10):
                return hh == h
        return False

    def say_time(r, h, m, ref_h=None, bare=False):
        """Spoken clock time that the validator can parse: am/pm always when minutes are present."""
        hh = h % 12 or 12
        ap = "am" if h < 12 else "pm"
        if h == 12 and m == 0 and r.random() < 0.35:
            return "noon"
        if bare and m == 0:
            return str(hh)
        if m == 0 and ref_h is not None and (ref_h < 12) == (h < 12) and r.random() < 0.5:
            return str(hh)
        base = f"{hh}:{m:02d}" if m else f"{hh}"
        return f"{base}{r.choice(['', '', ' '])}{ap}"

    def alarm_str(h, m):
        hh = h % 12 or 12
        ap = "am" if h < 12 else "pm"
        return f"{hh}:{m:02d}{ap}" if m else f"{hh}{ap}"

    def day_word(now, d):
        if d == now.date():
            return "today"
        if d == now.date() + dt.timedelta(days=1):
            return "tomorrow"
        return WEEKDAYS[d.weekday()]

    def yo(task):
        """Second-person form of a task the user phrased in the first person."""
        return task.replace(" my ", " your ").replace("myself", "yourself")

    def join_and(items):
        if len(items) <= 1:
            return "".join(items)
        return ", ".join(items[:-1]) + " and " + items[-1]

    def rem_receipt(title, d, h, m):
        return f"Reminder set: “{title}” — {long_when(d, h, m)} in Apple Reminders and Wisp."

    def upd_receipt(task, d, h, m):
        return f"Updated “{reminder_title(task)}” - now {long_when(d, h, m)}."

    # ================================================================ 1. casual reminder wording -> add_reminder
    TASKS_TRAIN = ["water the herbs", "move the car", "text the babysitter", "pick up the parcel", "take my vitamins", "check the oven",
                   "submit the timesheet", "feed the sourdough starter", "start the laundry", "lock up the shed", "call grandma",
                   "sign the permission slip", "print the boarding pass", "wrap the gift", "pay the parking ticket",
                   "stretch my back", "switch the laundry over", "turn off the sprinklers", "take the casserole out", "restock the first aid kit"]
    TASKS_DEV = ["take the bins out", "email the accountant", "refill the water filter", "pack my gym bag", "charge my earbuds"]

    ABS_TRAIN = ["buzz me at {t} to {task}", "hit me up at {t} so I {task}", "give me a heads-up at {t} to {task}",
                 "I want a ping at {t} to {task}", "set something for {t} so I remember to {task}", "at {t}, nudge me to {task}",
                 "yo can you poke me at {t} to {task}", "shoot me a buzz at {t} - {task}", "ping me at {t} to {task}", "ping me at {t} so I {task}"]
    REL_TRAIN = ["give me a nudge in {n} to {task}", "in {n} tell me to {task}", "{n} from now, remind me to {task}",
                 "shoot me a reminder in {n}: {task}", "poke me in {n} so I {task}", "can you buzz me in {n} to {task}"]
    PART_TRAIN = ["{p} remind me to {task}", "remind me {p} to {task}", "{p} poke me to {task}", "can you ping me {p} so I {task}",
                  "make sure I {task} {p}, remind me", "{p}, nudge me to {task}"]
    NOSUBJ_TRAIN = ["set a reminder for {n} from now", "nudge me in {n}", "can you buzz me in {n}", "give me a heads-up in {n}"]
    PART_NOSUBJ = ["ping me {p}", "set a reminder for {p}", "I need a nudge {p}"]

    ABS_DEV = ["could you tap me on the shoulder at {t} so I {task}", "I'd like an alert at {t}: {task}", "come {t}, flag it for me to {task}"]
    REL_DEV = ["a heads-up {n} from now would help, I need to {task}", "make a note to tap me in {n} about this: {task}"]
    PART_DEV = ["could you flag {p} that I should {task}", "{p} I'd like a reminder so I {task}"]

    # (spoken phrase, default hour, minute, predicate on now)
    PARTS = [("tonight", 20, 0, lambda n: n.hour <= 17), ("later tonight", 21, 0, lambda n: n.hour <= 18),
             ("this evening", 18, 0, lambda n: n.hour <= 15), ("this afternoon", 15, 0, lambda n: n.hour <= 12),
             ("tomorrow morning", 9, 0, lambda n: True), ("first thing tomorrow", 8, 0, lambda n: True),
             ("tomorrow afternoon", 14, 0, lambda n: True), ("tomorrow evening", 18, 0, lambda n: True),
             ("tomorrow night", 20, 0, lambda n: True), ("after lunch", 13, 0, lambda n: n.hour <= 10),
             ("by end of day", 17, 0, lambda n: n.hour <= 14)]
    PARTS_DEV = [("tonight", 20, 0, lambda n: n.hour <= 17), ("tomorrow morning", 9, 0, lambda n: True),
                 ("this evening", 18, 0, lambda n: n.hour <= 15), ("tomorrow afternoon", 14, 0, lambda n: True)]

    def _casual(r, c, now, tasks, abs_t, rel_t, part_t, nosubj_t, parts, with_nosubj):
        kind = r.choice(["abs", "abs", "abs", "abs", "rel", "part", "part", "part"])
        if with_nosubj and r.random() < 0.12:
            return _casual_nosubj(r, c, now, nosubj_t, parts)
        task = r.choice(tasks)
        default = False
        if kind == "abs":
            if r.random() < 0.7:
                h = r.randint(6, 21)
                m = r.choice([0, 0, 30, 30, 15, 45])
                d = clock_date(now, h, m)
                bare = m == 0 and d == now.date() and bare_ok(now, h) and r.random() < 0.5
                sp = say_time(r, h, m, bare=bare)
                if d != now.date():
                    sp = f"{sp} tomorrow"
            else:
                day = clean_day(pick_day(r, now, kinds=("weekday",)))
                tm = pick_time(r, 7, 20, minutes=(0, 30))
                d, h, m = day.date, tm.h, tm.m
                sp = f"{tm.spoken} on {day.spoken}"
            tpl_i = r.randrange(len(abs_t))
            text = abs_t[tpl_i].format(t=sp, task=task)
            idx = tpl_i
        elif kind == "rel":
            if r.random() < 0.6:
                mins = r.choice([10, 15, 20, 25, 40, 45])
                n = r.choice([f"{mins} min", f"{mins} minutes"])
            else:
                mins = r.choice([60, 120, 180])
                n = {60: "an hour", 120: "two hours", 180: "three hours"}[mins]
            t = now + dt.timedelta(minutes=mins)
            t = (t - dt.timedelta(minutes=t.minute % 5)).replace(second=0, microsecond=0)
            assert t.date() == now.date() and t > now
            d, h, m = t.date(), t.hour, t.minute
            tpl_i = r.randrange(len(rel_t))
            text = rel_t[tpl_i].format(n=n, task=task)
            idx = 20 + tpl_i
        else:
            ok = [p for p in parts if p[3](now)]
            assert ok
            ph, h, m, _ = r.choice(ok)
            d = now.date() + dt.timedelta(days=1) if ph.startswith(("tomorrow", "first thing tomorrow")) else now.date()
            if r.random() < 0.18 and not is_dev():                   # "later today": the default depends on the time of day
                ph = "later today"
                assert now.hour <= 18
                h = 17 if now.hour <= 13 else 20
                d = now.date()
            default = True
            tpl_i = r.randrange(len(part_t))
            text = part_t[tpl_i].format(p=ph, task=task)
            idx = 40 + tpl_i
        c.user(messy(r, text, protect=(task,)))
        c.discover(["calendar"], ["add_reminder"])
        title = reminder_title(task)
        c.call("add_reminder", {"title": title, "when_iso": iso(d, h, m)}, rem_receipt(title, d, h, m))
        w = wp(now, d, h, m)
        if default:
            c.final(r.choice([f"Done - you didn't give an exact time, so I set it for {w}: {yo(task)}.",
                              f"Reminder set for {w} to {yo(task)}. I picked that time since you only gave me part of the day.",
                              f"You're set - I'll remind you to {yo(task)} {w} (a default I chose; tell me if you want it different)."]))
        else:
            c.final(r.choice([f"Done - I'll remind you to {yo(task)} {w}.", f"Got it. Reminder set to {yo(task)} {w}.", f"You're set: {yo(task)}, {w}."]))
        return {"template": idx, "slots": {"kind": kind, "task": task, "default_time": default}}

    def _casual_nosubj(r, c, now, nosubj_t, parts):
        if r.random() < 0.55:
            mins = r.choice([20, 30, 45, 60])
            n = "an hour" if mins == 60 else f"{mins} minutes"
            i = r.randrange(len(nosubj_t))
            text = nosubj_t[i].format(n=n)
            idx = 60 + i
        else:
            ok = [p for p in parts if p[3](now)]
            ph = r.choice(ok)[0]
            i = r.randrange(len(PART_NOSUBJ))
            text = PART_NOSUBJ[i].format(p=ph)
            idx = 70 + i
        c.user(messy(r, text))
        c.final(r.choice(["Sure - what should I remind you about?", "Happy to. What is the reminder for?",
                          "What would you like me to remind you about? I'll set it for that time."]))
        return {"template": idx, "slots": {"kind": "no_subject"}}

    @family(CAT, "rm_casual_time", weight=12, failure=("reminder_alarm_confusion",))
    def f_rm_casual_time(r, c, now):
        return _casual(r, c, now, TASKS_TRAIN, ABS_TRAIN, REL_TRAIN, PART_TRAIN, NOSUBJ_TRAIN, PARTS, True)

    @family(CAT, "rm_dev_casual", split="dev", weight=4, failure=("reminder_alarm_confusion",))
    def f_rm_dev_casual(r, c, now):
        return _casual(r, c, now, TASKS_DEV, ABS_DEV, REL_DEV, PART_DEV, [], PARTS_DEV, False)

    # ================================================================ 2. "actually make it 7" after a reminder -> update_reminder
    RU_HIST = ["remind me to {task} {w}", "set a reminder for {w}: {task}", "I need a reminder to {task} {w}", "could you remind me to {task} {w}?"]
    RU_USER = ["actually push it to {t}", "hmm, shift that to {t} instead", "make that {t} instead - my plans changed",
               "can you change it to {t}?", "on second thought, move it to {t}", "update that reminder to {t}, I'll be free later",
               "wait, {t} is better - can you reschedule it", "ugh, change of plans - {t} instead please", "bump it to {t}, plans changed",
               "actually make it {t}, I need to update it", "make it {t} instead, I changed my mind", "{t} would be better - change it to that",
               "make that {t}, I need to reschedule"]
    # bare corrections: no move/push/change/shift/update word, only a weak cue ("make it" / "instead" / "bump")
    RU_BARE = ["make it {t}", "{t} instead", "bump it to {t}", "{t} instead, please", "go with {t} instead", "how about {t} instead",
               "make it {t} rather", "{t} works better instead", "no, make it {t}", "actually let's do {t}", "let's do {t} then", "actually, {t} for that one"]

    def say_time_words(r, h, m, ref_h):
        """Digits, or an hour-relative phrase ("quarter to 8", "half past 7") when the hour shares the reference meridiem."""
        same = (ref_h < 12) == (h < 12)
        if same and r.random() < 0.4:
            hh, nx = h % 12 or 12, (h + 1) % 12 or 12
            if m == 45:
                return f"quarter to {nx}"
            if m == 15:
                return f"quarter past {hh}"
            if m == 30:
                return f"half past {hh}"
        return say_time(r, h, m, ref_h=ref_h)

    RU_ASK = ["Done - I'll remind you to {task} on {lw}.", "Reminder set: {task}, {lw}.", "Got it. You'll get a reminder to {task} at {tt} on {wd}."]

    def _task_key(task):
        return task.split()[-1].strip(".,!?")

    @family(CAT, "rm_update_time", weight=8, failure=("reminder_alarm_confusion", "duplicate_instead_of_update"))
    def f_rm_update_time(r, c, now):
        task = r.choice(TASKS_TRAIN)
        if now.hour <= 16 and r.random() < 0.2:                      # a reminder set for tonight
            d = now.date()
            h1, m1 = r.choice([19, 20]), r.choice([0, 30])
            w = f"tonight at {say_time(r, h1, m1)}"
            h2 = r.choice([x for x in range(18, 22) if x != h1])
            m2 = r.choice([0, 15, 30, 45])
        else:
            day = clean_day(pick_day(r, now, kinds=("tomorrow", "weekday", "weekday")))
            d = day.date
            h1, m1 = r.randint(8, 19), r.choice([0, 30])
            w = f"{day.spoken} at {say_time(r, h1, m1)}"
            h2 = min(21, max(7, h1 + r.choice([-2, -1, 1, 2, 3])))
            m2 = r.choice([0, 15, 30, 45])
        assert (h2, m2) != (h1, m1)
        assert dt.datetime.combine(d, dt.time(h2, m2)) > now + dt.timedelta(minutes=20)
        c.hist("user", r.choice(RU_HIST).format(task=task, w=w))
        c.hist("assistant", r.choice(RU_ASK).format(task=yo(task), lw=long_when(d, h1, m1), tt=t12(h1, m1), wd=WEEKDAYS[d.weekday()]))
        bare = r.random() < 0.5
        sp = say_time_words(r, h2, m2, h1) if bare else say_time(r, h2, m2, ref_h=h1)
        ui = r.randrange(len(RU_BARE if bare else RU_USER))
        text = (RU_BARE if bare else RU_USER)[ui].format(t=sp)
        ui = ui + (50 if bare else 0)
        c.user(messy(r, text, protect=(sp,)))
        c.discover(["calendar"], ["update_reminder"])
        args = {"when_iso": iso(d, h2, m2)}
        mode = r.choice(["key", "key", "full", "full", "none"])
        if mode == "key":
            args = {"title": _task_key(task), **args}
        elif mode == "full":
            args = {"title": task, **args}
        c.call("update_reminder", args, upd_receipt(task, d, h2, m2))
        c.final(r.choice([f"Done - “{task}” is now at {t12(h2, m2)} on {WEEKDAYS[d.weekday()]}.",
                          f"Moved your “{task}” reminder to {t12(h2, m2)}.", f"Updated: I'll remind you to {yo(task)} at {t12(h2, m2)} instead."]))
        return {"template": ui, "slots": {"task": task, "title_arg": mode}}

    # ================================================================ 3. alarm vs reminder contrast
    AL_TPL = ["set an alarm for {t}", "wake me up at {t}", "I need an alarm at {t}", "alarm for {t} please", "get me up at {t}",
              "can you set a wake-up alarm for {t}", "I need to be woken at {t}", "set my alarm to {t}"]
    AL_LABEL = ["set an alarm called {l} for {t}", "alarm at {t}, label it {l}", "put an alarm on for {t} named {l}"]
    AL_DAILY = ["wake me at {t} every day", "set a daily alarm for {t}", "I want an alarm at {t} every single morning"]
    RC_TPL = ["remind me at {t} to {task}", "at {t} remind me to {task}", "I need a reminder at {t} to {task}", "alert me at {t} to {task}",
              "set a reminder for {t} - {task}", "remind me around {t} that I need to {task}"]
    LABELS = ["gym", "flight", "meds", "pickup", "standup", "class"]

    @family(CAT, "rm_alarm_contrast", weight=6, failure=("reminder_alarm_confusion",))
    def f_rm_alarm_contrast(r, c, now):
        if r.random() < 0.55:
            h = r.choice([5, 6, 6, 7, 7, 8, 9]) if r.random() < 0.85 else r.choice([13, 15, 16])
            m = r.choice([0, 0, 30, 45, 15])
            d = clock_date(now, h, m, gap=1)
            sp = say_time(r, h, m)
            tmr = d != now.date() and r.random() < 0.45
            roll = r.random()
            lab = None
            daily = False
            if roll < 0.15:
                lab = r.choice(LABELS)
                i = r.randrange(len(AL_LABEL))
                text = AL_LABEL[i].format(t=sp + (" tomorrow" if tmr else ""), l=lab)
                idx = 20 + i
            elif roll < 0.28:
                daily = True
                i = r.randrange(len(AL_DAILY))
                text = AL_DAILY[i].format(t=sp)
                idx = 40 + i
            else:
                i = r.randrange(len(AL_TPL))
                text = AL_TPL[i].format(t=sp + (" tomorrow" if tmr else ""))
                idx = i
            c.user(messy(r, text, protect=(sp,) + ((lab,) if lab else ())))
            c.discover(["automation"], ["set_alarm"])
            args = {"time": alarm_str(h, m)}
            if lab:
                args["label"] = lab
            if daily:
                args["repeat_daily"] = True
            tt = t12(h, m)
            c.call("set_alarm", args, f"{'Daily alarm' if daily else 'Alarm'} set for {tt}.")
            if daily:
                c.final(f"Done - a daily alarm is set for {tt}.")
            else:
                c.final(r.choice([f"Alarm set for {tt} {day_word(now, d)}.", f"Done - your alarm is set for {tt}" + (f" ({lab})." if lab else "."),
                                  f"You're set: alarm at {tt} {day_word(now, d)}."]))
            return {"template": idx, "slots": {"kind": "alarm", "label": bool(lab), "daily": daily}}
        task = r.choice(TASKS_TRAIN)
        h = r.randint(7, 21)
        m = r.choice([0, 0, 30])
        d = clock_date(now, h, m)
        bare = m == 0 and d == now.date() and bare_ok(now, h) and r.random() < 0.5
        sp = say_time(r, h, m, bare=bare)
        if d != now.date():
            sp = f"{sp} tomorrow"
        i = r.randrange(len(RC_TPL))
        c.user(messy(r, RC_TPL[i].format(t=sp, task=task), protect=(task,)))
        c.discover(["calendar"], ["add_reminder"])
        title = reminder_title(task)
        c.call("add_reminder", {"title": title, "when_iso": iso(d, h, m)}, rem_receipt(title, d, h, m))
        c.final(r.choice([f"Done - I'll remind you to {yo(task)} {wp(now, d, h, m)}.", f"Reminder set: {yo(task)}, {wp(now, d, h, m)}."]))
        return {"template": 60 + i, "slots": {"kind": "reminder", "task": task}}

    # ================================================================ 4. day overview -> get_upcoming
    OV_TODAY = ["how's the rest of my day shaping up", "what's still ahead of me today", "anything else on the books today",
                "what do I have left today", "how much is left on my schedule today"]
    OV_AFT = ["how's my afternoon looking", "am I tied up this afternoon", "do I have much going on this afternoon",
              "what's happening for me this afternoon"]
    OV_TOM = ["anything on deck tomorrow", "what does tomorrow have in store for me", "is tomorrow packed or pretty open",
              "how slammed am I tomorrow", "give me the rundown for tomorrow"]
    OV_DATE = ["run me through {d}", "what's lined up for {d}", "how packed does {d} look", "walk me through {d} real quick",
               "what am I doing {d}"]
    OV_DAYPART = ["how's {d} afternoon shaping up", "what's on for {d} morning", "am I busy {d} afternoon"]
    OV_WEEK = ["how does next week shape up", "what's next week looking like for me", "give me the rundown of next week"]

    def _agenda_line(items):
        return join_and([f"{title} at {tt}" for _, title, tt in items])

    @family(CAT, "cal_day_overview", weight=8, failure=("calendar_lookalike",))
    def f_cal_day_overview(r, c, now):
        roll = r.random()
        if roll < 0.15:                                               # next week
            mon = next_weekday(now, 0)
            if mon <= now.date():
                mon += dt.timedelta(days=7)
            days = sorted(r.sample(range(5), r.randint(2, 4)))
            evs = []
            for k in days:
                evs.append(make_event(r, mon + dt.timedelta(days=k), h=r.randint(9, 16), used_keys=[e.key for e in evs]))
            i = r.randrange(len(OV_WEEK))
            c.user(messy(r, OV_WEEK[i]))
            c.discover(["calendar"], ["get_upcoming"])
            c.call("get_upcoming", {"period": "next week"}, agenda_result(now, "next week", evs))
            by = {}
            for e in evs:
                by.setdefault(e.date, []).append(e)
            parts = [f"{WEEKDAYS[d.weekday()]}: " + join_and([f"{e.title} at {e.tstr}" for e in es]) for d, es in sorted(by.items())]
            c.final("Next week you have " + "; ".join(parts) + ".")
            return {"template": 100 + i, "slots": {"kind": "week", "n": len(evs)}}
        empty = r.random() < 0.12
        if roll < 0.38:
            day = pick_day(r, now, kinds=("today",))
            window = "afternoon" if (now.hour <= 14 and r.random() < 0.5) else "all"
        elif roll < 0.60:
            day = pick_day(r, now, kinds=("tomorrow",))
            window = r.choice(["all", "all", "all", "afternoon", "morning"])
        else:
            day = clean_day(pick_day(r, now, kinds=("weekday",)))
            window = r.choice(["all", "all", "all", "afternoon", "morning"])
        today = day.kind == "today"
        if today:
            assert now.hour <= (14 if window == "afternoon" else 17)
        n_in = 0 if empty else r.randint(1, 3)
        # events inside the asked window, then (sometimes) one outside it that the answer leaves out
        if window == "afternoon":
            lo, hi = (max(12, now.hour + 1), 17) if today else (12, 17)
        elif window == "morning":
            lo, hi = 8, 11
        else:
            lo, hi = (now.hour + 1, 19) if today else (8, 18)
        assert lo <= hi
        evs = make_day(r, day.date, n_in, hmin=lo, hmax=hi) if n_in else []
        keep = list(evs)
        if window != "all" and r.random() < (0.5 if not empty else 0.6):
            olo, ohi = (8, 11) if window == "afternoon" else (13, 17)
            if today:
                olo = max(olo, now.hour + 1)
            if olo <= ohi:
                other = make_event(r, day.date, h=r.randint(olo, ohi), used_keys=[e.key for e in evs], avoid=evs)
                evs = sorted(evs + [other], key=lambda e: e.start)
        reminders = []
        rem_note = ""
        if window == "all" and not empty and r.random() < 0.28:
            rt = r.choice(REMINDER_BANK)
            rh = r.randint(max(lo, 8), 19)
            if not today or rh > now.hour:
                rm = r.choice([0, 30])
                reminders = [(day.date, rh, rm, reminder_title(rt))]
                rem_note = f"{reminder_title(rt)} at {t12(rh, rm)}"
        # prompt
        if window != "all":
            i = r.choice([1] if window == "morning" else [0, 2])
            if today:
                i = r.randrange(len(OV_AFT))
                text, idx = OV_AFT[i], 20 + i
            else:
                text, idx = OV_DAYPART[i].format(d=day.spoken), 30 + i
        elif today:
            i = r.randrange(len(OV_TODAY))
            text, idx = OV_TODAY[i], i
        elif day.kind == "tomorrow":
            i = r.randrange(len(OV_TOM))
            text, idx = OV_TOM[i], 40 + i
        else:
            i = r.randrange(len(OV_DATE))
            text, idx = OV_DATE[i].format(d=day.spoken), 50 + i
        c.user(messy(r, text, protect=(day.spoken,)))
        c.discover(["calendar"], ["get_upcoming"])
        c.call("get_upcoming", {"period": day.period}, agenda_result(now, day.period if day.kind == "date" else day.kind, evs, reminders))
        wd = WEEKDAYS[day.date.weekday()]
        if today:
            when_ph = "the rest of today" if window == "all" else "this afternoon"
        elif day.kind == "tomorrow":
            when_ph = "tomorrow" if window == "all" else f"tomorrow {window}"
        else:
            when_ph = f"on {wd}" if window == "all" else f"{wd} {window}"
        if not keep and not reminders:
            if window == "all":
                c.final("Nothing else on the books today - you're clear." if today else f"Nothing scheduled {when_ph} - it's wide open.")
            else:
                c.final(f"Nothing {when_ph} - that stretch is clear.")
        else:
            body = join_and([f"{e.title} at {e.tstr}" for e in keep])
            if rem_note:
                body += (", plus " if keep else "") + f"a reminder: {rem_note}"
            if window == "all" and len(keep) >= 3:
                c.final(f"{cap(when_ph)} it's a busy one: {body}.")
            else:
                c.final(f"{cap(when_ph)} you have {body}.")
        return {"template": idx, "slots": {"kind": day.kind, "window": window, "n": len(evs), "reminder": bool(reminders)}}

    # ================================================================ 5. solo time block -> add_calendar_event
    BLOCK_TITLES = ["deep work", "focus time", "errands", "writing time", "study session", "meal prep", "inbox cleanup", "reading time",
                    "budget catch-up", "gym session"]
    BLOCK_DEV = ["thesis writing", "piano practice", "spring cleaning", "photo editing"]
    BR_TPL = ["hold {rg} {d} for {title}", "block off {rg} {d} for {title}", "mark {rg} {d} as {title}", "reserve {rg} {d} - {title}",
              "put {title} on my calendar {d} {rg}", "wall off {d} {rg} for {title}"]
    BD_TPL = ["carve out {dur} {d} {part} for {title}", "I need {dur} {d} {part} for {title}, put it on my calendar",
              "set aside {dur} {d} {part} for {title}"]
    BR_DEV = ["make sure nobody books me {d} {rg} - I need it for {title}", "mark me as busy {d} {rg}: {title}"]
    BD_DEV = ["clear {d} {part} for {title}, {dur} is plenty", "I want {dur} {d} {part} to myself for {title}, add it"]
    DURS = [("an hour", 60), ("two hours", 120), ("90 minutes", 90), ("half an hour", 30), ("a couple of hours", 120), ("an hour and a half", 90)]
    PARTS_DEFAULT = {"morning": (9, 0), "afternoon": (13, 0), "evening": (18, 0)}

    def _block(r, c, now, titles, br_t, bd_t):
        title_w = r.choice(titles)
        title = cap(title_w)
        day = clean_day(pick_day(r, now, kinds=("tomorrow", "weekday", "weekday")))
        d = day.date
        if r.random() < 0.58:
            h1 = r.randint(8, 16)
            m1 = r.choice([0, 0, 0, 30])
            span = r.choice([60, 90, 120, 120, 180])
            h2, m2 = divmod(h1 * 60 + m1 + span, 60)
            assert h2 <= 20
            bare_ok_ = m1 == 0 and m2 == 0 and ((h1 < 12 and h2 <= 12) or h1 >= 12) and r.random() < 0.4
            if bare_ok_:
                part = "morning" if h1 < 12 else "afternoon"
                a_, b_ = h1 % 12 or 12, h2 % 12 or 12
                rg = r.choice([f"{a_}-{b_}", f"{a_} to {b_}", f"from {a_} to {b_}"])
                rg = f"{rg} in the {part}" if r.random() < 0.5 else f"{rg} {part}"
            else:
                s1, s2 = say_time(r, h1, m1), say_time(r, h2, m2)
                rg = r.choice([f"{s1} to {s2}", f"from {s1} to {s2}", f"{s1}-{s2}"])
            idx = r.randrange(len(br_t))
            text = br_t[idx].format(rg=rg, d=day.spoken, title=title_w)
            h, m, dur, kind = h1, m1, span, "range"
        else:
            dur_w, dur = r.choice(DURS)
            part = r.choice(["morning", "afternoon", "afternoon", "evening"])
            h, m = PARTS_DEFAULT[part]
            idx = r.randrange(len(bd_t))
            text = bd_t[idx].format(dur=dur_w, d=day.spoken, part=part, title=title_w)
            idx += 20
            kind = "duration"
        assert dt.datetime.combine(d, dt.time(h, m)) > now
        c.user(messy(r, text, protect=(title_w, day.spoken)))
        c.discover(["calendar"], ["add_calendar_event"])
        endm = h * 60 + m + dur
        c.call("add_calendar_event", {"title": title, "when_iso": iso(d, h, m), "duration_min": dur},
               f"Added “{title}” to your calendar for {long_when(d, h, m)} - {t12(endm // 60, endm % 60)}.")
        span_txt = f"{t12(h, m)} to {t12(endm // 60, endm % 60)}"
        wdn = WEEKDAYS[d.weekday()]
        if kind == "duration":
            c.final(r.choice([f"Done - {title} is on your calendar {wdn} from {span_txt}. You didn't give a start time, so I used {t12(h, m)}.",
                              f"Blocked {span_txt} on {wdn} for {title_w} (I picked the {t12(h, m)} start)."]))
        else:
            c.final(r.choice([f"Blocked {span_txt} on {wdn} for {title_w}.", f"Done - {title} is on your calendar, {wdn} {span_txt}.",
                              f"It's on the calendar: {title}, {long_when(d, h, m)} for {dur} minutes."]))
        return {"template": idx, "slots": {"kind": kind, "title": title, "duration": dur}}

    @family(CAT, "cal_block_time", weight=8, failure=("calendar_lookalike",))
    def f_cal_block_time(r, c, now):
        return _block(r, c, now, BLOCK_TITLES, BR_TPL, BD_TPL)

    @family(CAT, "cal_dev_block", split="dev", weight=3, failure=("calendar_lookalike",))
    def f_cal_dev_block(r, c, now):
        return _block(r, c, now, BLOCK_DEV, BR_DEV, BD_DEV)

    # ================================================================ 6. accepted offers -> the offered write
    ACC_TRAIN = ["yep", "go for it", "please do", "sure thing", "that works", "do it", "sounds great", "sure, go for it", "yep, do that",
                 "absolutely", "ok do it", "yeah that works for me", "perfect, do it", "yup", "yes, go ahead and do that", "alright, do it"]
    ACC_DEV = ["affirmative", "yes, that would be great", "i'd appreciate that", "go right ahead", "make it happen", "yeah, please",
               "that'd be perfect, thanks", "do that for me"]
    DEC_TRAIN = ["nah, skip it", "not now", "nope, I'm good", "I'll handle it myself", "no thanks, I'm good", "actually, leave it",
                 "don't bother", "nah I got it"]
    DEC_DEV = ["let's not", "i'll pass", "skip that", "no need, thanks"]
    MODT_TRAIN = ["yes but make it {t}", "sure, but {t} instead", "yep - though {t} works better", "do it, just at {t}"]
    MODT_DEV = ["yes please, though could you do {t} instead", "that works, but {t} would suit me better"]
    MODD_TRAIN = ["yes but on {d} instead", "sure, but make it {d}", "do it, just for {d}"]
    MODD_DEV = ["that works, though {d} would suit me better"]
    CHAN_TRAIN = ["yes, but {ch} it instead", "sure, though {ch} is better", "go for it - {ch} though"]
    CHAN_DEV = ["yes please, only by {ch2} instead"]

    REM_TRAIN = [
        ("I have to {task} by Sunday night. do they charge late fees?", "return the rental car",
         "Many places allow a short grace period and then bill a full extra day, so on time is safest."),
        ("what's a good way to remember to {task}? I always forget", "take my vitamins",
         "Tying it to a fixed daily habit like breakfast tends to work best."),
        ("my passport photo came out weird, I should {task} before the trip", "retake the passport photo",
         "Photo counters usually print in minutes, so it's an easy errand."),
        ("how long can I skip a day and still {task}? the jar looks sleepy", "feed the sourdough starter",
         "A day or two at room temperature is fine; it just turns more sour."),
        ("my landlord wants me to {task} this week, how does that usually work?", "send in the lease renewal",
         "You typically sign, scan the pages and send them back by email or drop them at the office."),
        ("do pharmacies hold a refill for long? I have to {task}", "pick up my prescription",
         "Most hold it for about a week before putting it back in stock."),
        ("it's supposed to freeze this weekend and I need to {task}", "cover the tomato plants",
         "A light sheet or burlap works well - take it off in the morning so they get sun."),
        ("I keep missing the deadline to {task} and the form closes soon", "submit the parking permit form",
         "Permit offices usually process online forms within a couple of business days."),
        ("is it worth it to {task} before the price goes up?", "renew the gym membership",
         "Renewing before the month ends usually keeps the current rate."),
        ("the garden store says I should {task} once a week in summer", "water the ferns",
         "Early morning is best so the leaves dry before evening."),
        ("I want to {task} more but I never remember", "call grandma", "A short call on the same weekday tends to stick."),
        ("how far ahead do I have to {task} for a holiday weekend?", "book the airport shuttle",
         "A week out is usually safe; holiday weekends fill up faster than regular ones."),
    ]
    REM_DEV = [
        ("how early should I {task}? the line gets long", "renew my driver's license", "Mornings right after opening are the quietest."),
        ("I've been meaning to {task} before the season starts", "service the lawn mower", "A basic tune-up takes about a day at most shops."),
        ("what's the best day to {task}? I'm free whenever", "drop off the donations", "Weekday mornings are calmer at most donation centers."),
        ("do I really have to {task} each month?", "update the budget sheet", "A monthly pass keeps the numbers from piling up."),
    ]
    OFFER_REM_X = ["Want me to remind you {w} to {task}?", "Should I set a reminder for {w} to {task}?", "I can remind you to {task} {w} - want that?",
                   "Shall I ping you {w} so you {task}?"]
    OFFER_REM_I = ["Want me to set a reminder for {w}?", "I can remind you {w} if you'd like.", "Should I put a reminder in for {w}?"]
    OFFER_REM_DEV = ["Would you like a reminder {w}?", "I could nudge you {w} about it - shall I?", "Shall I add a reminder to {task} for {w}?"]

    CAL_TRAIN = [
        ("I told {p} I'd meet for coffee {d} at {t}", "Coffee with {p}", "coffee"),
        ("{p} invited me to a pottery class {d} at {t}", "Pottery class with {p}", "pottery"),
        ("I promised to help {p} move boxes {d} at {t}", "Help {p} move boxes", "boxes"),
        ("my car's oil change is {d} at {t}", "Oil change", "oil"),
        ("I'm getting a haircut {d} at {t}", "Haircut", "haircut"),
        ("{p} and I are doing a trail run {d} at {t}", "Trail run with {p}", "trail"),
        ("the vet appointment for Biscuit is {d} at {t}", "Vet appointment for Biscuit", "vet"),
        ("I booked a massage {d} at {t}", "Massage", "massage"),
        ("dinner with {p} is {d} at {t}", "Dinner with {p}", "dinner"),
        ("I'm picking up {p} from the airport {d} at {t}", "Pick up {p} from the airport", "airport"),
    ]
    CAL_DEV = [
        ("I'll be at the farmers market with {p} {d} at {t}", "Farmers market with {p}", "market"),
        ("the plumber is coming {d} at {t}", "Plumber visit", "plumber"),
        ("{p} and I booked a climbing session {d} at {t}", "Climbing session with {p}", "climbing"),
    ]
    CAL_REACT = ["Sounds like a good plan.", "Nice, that should be a good one.", "Got it.", "That sounds fun."]
    OFFER_CAL = ["I can add it to your calendar for {w} - want that?", "Want me to put it on your calendar for {w}?",
                 "Should I block {w} on your calendar for it?"]
    OFFER_CAL_DEV = ["Shall I put that in your calendar for {w}?", "I could add it for {w} if that helps."]
    OFFER_CAL_DUR = ["I can add it to your calendar for {w}, for an hour - want that?", "Want me to put it on your calendar for {w}, one hour long?"]

    ALARM_TRAIN = [("flight", "my flight is at {E} tomorrow and I want to be at the airport early - what time should I get up?"),
                   ("train", "my train leaves at {E} tomorrow, when do I need to get up?"),
                   ("shift", "I start my shift at {E} tomorrow. what time should I get up to not rush?"),
                   ("hike", "the group hike starts at {E} tomorrow, how early should I get up?")]
    ALARM_DEV = [("lesson", "my driving lesson is at {E} tomorrow, what time should I wake up?"),
                 ("ferry", "the ferry leaves at {E} tomorrow and I need time to get there, when should I get up?")]
    OFFER_ALARM = ["I'd aim to be up around {W}. Want me to set an alarm for {W}?", "{W} leaves you a comfortable buffer. Should I set an alarm for it?",
                   "I'd get up at {W}. I can set an alarm for that - want that?"]
    OFFER_ALARM_DEV = ["Around {W} should work. Shall I set the alarm?", "I'd wake up at {W}; I could set that alarm if you like."]

    TIMER_TRAIN = [("steep green tea", 3, "tea"), ("soft boil an egg", 6, "egg"), ("hard boil eggs", 10, "eggs"),
                   ("rest a steak after searing", 5, "steak"), ("let a clay face mask sit", 10, "mask"), ("brew a pour-over", 4, "pour-over")]
    TIMER_DEV = [("proof a quick pizza dough", 15, "dough"), ("cook a pot of couscous", 5, "couscous")]
    OFFER_TIMER = ["About {n} minutes. Want me to start a timer?", "{n} minutes is about right. Should I start a timer for it?",
                   "Around {n} minutes. I can run a timer for you - want that?"]
    OFFER_TIMER_DEV = ["Roughly {n} minutes. Shall I time it?", "{n} minutes should do it. I could start a countdown if you like."]

    # statements that sound natural as a first-person remark (used as the content of an offered text)
    TEXT_TOPICS_OK = {"late", "eta", "dinner", "cant_make", "landed", "outside", "store", "moved", "after_work", "traffic", "parking",
                      "running_early", "safe", "tickets", "dessert", "still_on", "push_call", "lunch", "dinner_move"}
    TEXT_USER_LEAD = ["{n} just texted me - {c}. what should I say back?", "ugh, {n} keeps messaging me. {C} - not sure how to answer",
                      "{n} messaged to check in. {C}. how should I reply?"]
    TEXT_USER_DEV = ["{n} sent a text asking what's up; {c}. what do I write?", "I got a message from {n}. {C}. any idea how to reply?"]
    TEXT_ADVICE = ["Keep it short and direct.", "A quick, plain reply works best.", "Something brief will do."]
    OFFER_TEXT = ["Want me to text {f} that?", "Should I let {f} know by text?", "I can text {f} for you - want me to?"]
    OFFER_TEXT_Q = ["Want me to text {f}? I'd send: {q}", "I can text {f} this: {q} Want me to send it?"]
    OFFER_TEXT_DEV = ["Shall I message {f} for you?", "I could send {f} a text with that if you'd like."]
    EMAIL_USER = ["{n} emailed me asking for an update. {S}. how should I answer?", "I got an email from {n} wanting a status. {S}. what should I reply?"]
    EMAIL_USER_DEV = ["{n} wrote me an email asking where things stand. {S}. what do I say?"]
    OFFER_EMAIL = ["Want me to email {f} back with that?", "I can email {f} a reply covering that - want me to?", "Should I send {f} an email saying so?"]
    OFFER_EMAIL_DEV = ["Shall I reply to {f} by email with that?"]

    def pick_other_time(r, now, d, h, m):
        for _ in range(40):
            h2 = min(21, max(7, h + r.choice([-2, -1, 1, 2, 3])))
            m2 = r.choice([0, 30])
            if (h2, m2) != (h, m) and dt.datetime.combine(d, dt.time(h2, m2)) > now + dt.timedelta(minutes=20):
                return h2, m2
        raise AssertionError("other time")

    def ack_for(kind, nm=None):
        return {"rem": "Okay, I won't set a reminder.", "cal": "No problem, I'll leave it off your calendar.", "alarm": "Okay, no alarm.",
                "timer": "Alright, no timer.", "text": f"Okay, I won't text {nm}.", "email": f"Okay, I won't email {nm}."}[kind]

    _dev_state = {"i": 0}

    def _decline(r, c, kind, nm, dev):
        # dev has few rows, so its short replies rotate (no duplicate prompts within the split)
        c.user(messy(r, DEC_DEV[_dev_state["i"] % len(DEC_DEV)] if dev else r.choice(DEC_TRAIN)))
        c.final(r.choice([ack_for(kind, nm), "No problem - nothing changed.", "Got it, I'll leave it. Just say the word if you change your mind."])
                if kind in ("rem", "cal", "alarm", "timer") else r.choice([ack_for(kind, nm), "No problem - nothing was sent."]))

    def _accept_user(r, dev):
        return ACC_DEV[(_dev_state["i"] * 3 + 1) % len(ACC_DEV)] if dev else r.choice(ACC_TRAIN)

    def _outcome(r):
        p = r.random()
        return "accept" if p < 0.75 else ("modify" if p < 0.89 else "decline")

    # -- reminder offers
    EVT_TRAIN = ["dentist appointment", "car service", "haircut", "piano lesson", "parent-teacher meeting", "vet appointment", "eye exam"]
    EVT_DEV = ["physio session", "dinner reservation", "tax appointment"]
    EVT_Q = ["when's my {e}?", "what day is my {e} again?", "do you know when my {e} is?"]
    EVT_Q_DEV = ["I forgot when my {e} is - any idea?", "which day did I book the {e}?"]
    OFFER_EVT = ["Want me to remind you {w}?", "Should I set a reminder for the day before - {w}?", "I can remind you {w} so you don't miss it - want that?"]
    OFFER_EVT_DEV = ["Would you like a heads-up {w}?", "Shall I remind you {w}?"]

    def _rem_offer(r, c, now, dev, outcome):
        evt = r.random() < 0.3
        if evt:
            e = r.choice(EVT_DEV if dev else EVT_TRAIN)
            eday = clean_day(pick_day(r, now, kinds=("weekday",)))
            et = pick_time(r, 9, 17, minutes=(0, 30))
            d = eday.date - dt.timedelta(days=1)
            h, m = 9, 0
            w = wp(now, d, h, m)
            c.hist("user", r.choice(EVT_Q_DEV if dev else EVT_Q).format(e=e))
            c.hist("assistant", f"Your {e} is on {WEEKDAYS[eday.date.weekday()]} at {et.long}. "
                   + r.choice(OFFER_EVT_DEV if dev else OFFER_EVT).format(w=w))
            task, title = e, cap(e)
        else:
            user_t, task, answer = r.choice(REM_DEV if dev else REM_TRAIN)
            if now.hour <= 16 and r.random() < 0.18:
                d, h, m = now.date(), r.choice([19, 20, 21]), 0
            else:
                day = clean_day(pick_day(r, now, kinds=("tomorrow", "weekday", "weekday")))
                tm = pick_time(r, 8, 18, minutes=(0, 30))
                d, h, m = day.date, tm.h, tm.m
            w = wp(now, d, h, m)
            if dev:
                offer = r.choice(OFFER_REM_DEV).format(w=w, task=yo(task))
            elif r.random() < 0.6:
                offer = r.choice(OFFER_REM_X).format(w=w, task=yo(task))
            else:
                offer = r.choice(OFFER_REM_I).format(w=w)
            c.hist("user", user_t.format(task=task))
            c.hist("assistant", f"{answer} {offer}")
            title = reminder_title(task)
        if outcome == "decline":
            _decline(r, c, "rem", None, dev)
            return 0
        dd, hh, mm = d, h, m
        if outcome == "accept":
            c.user(messy(r, _accept_user(r, dev)))
        elif evt or r.random() < 0.7:
            hh, mm = pick_other_time(r, now, d, h, m)
            sp = say_time(r, hh, mm, ref_h=h)
            c.user(messy(r, r.choice(MODT_DEV if dev else MODT_TRAIN).format(t=sp), protect=(sp,)))
        else:
            for _ in range(30):
                nd = clean_day(pick_day(r, now, kinds=("weekday",)))
                if nd.date != d:
                    break
            else:
                raise AssertionError("day")
            dd = nd.date
            c.user(messy(r, r.choice(MODD_DEV if dev else MODD_TRAIN).format(d=nd.spoken), protect=(nd.spoken,)))
        c.discover(["calendar"], ["add_reminder"])
        c.call("add_reminder", {"title": title, "when_iso": iso(dd, hh, mm)}, rem_receipt(title, dd, hh, mm))
        ww = wp(now, dd, hh, mm)
        if evt:
            c.final(r.choice([f"Done - I'll remind you about your {task} {ww}.", f"Reminder set for {ww}: {title}.", f"All set - you'll get a heads-up about your {task} {ww}."]))
        else:
            c.final(r.choice([f"Done - I'll remind you to {yo(task)} {ww}.", f"Reminder set: {yo(task)}, {ww}.", f"All set - you'll get a nudge to {yo(task)} {ww}."]))
        return 1 if outcome == "accept" else 2

    # -- calendar offers
    def _cal_offer(r, c, now, dev, outcome):
        user_t, title_t, key = r.choice(CAL_DEV if dev else CAL_TRAIN)
        p = first(r.choice(PEOPLE))
        day = clean_day(pick_day(r, now, kinds=("tomorrow", "weekday", "weekday")))
        tm = pick_time(r, 9, 19, minutes=(0, 30))
        d, h, m = day.date, tm.h, tm.m
        title = title_t.format(p=p)
        with_dur = (not dev) and r.random() < 0.4
        w = wp(now, d, h, m)
        offer = r.choice(OFFER_CAL_DEV if dev else (OFFER_CAL_DUR if with_dur else OFFER_CAL)).format(w=w)
        c.hist("user", user_t.format(p=p, d=day.spoken, t=tm.spoken))
        c.hist("assistant", f"{r.choice(CAL_REACT)} {offer}")
        if outcome == "decline":
            _decline(r, c, "cal", None, dev)
            return 0
        hh, mm = h, m
        if outcome == "accept":
            c.user(messy(r, _accept_user(r, dev)))
        else:
            hh, mm = pick_other_time(r, now, d, h, m)
            sp = say_time(r, hh, mm, ref_h=h)
            c.user(messy(r, r.choice(MODT_DEV if dev else MODT_TRAIN).format(t=sp), protect=(sp,)))
        c.discover(["calendar"], ["add_calendar_event"])
        args = {"title": title, "when_iso": iso(d, hh, mm)}
        dur = 60
        if with_dur:
            args["duration_min"] = 60
        endm = hh * 60 + mm + dur
        c.call("add_calendar_event", args, f"Added “{title}” to your calendar for {long_when(d, hh, mm)} - {t12(endm // 60, endm % 60)}.")
        c.final(r.choice([f"Added “{title}” to your calendar for {long_when(d, hh, mm)}.", f"Done - {title} is on your calendar, {WEEKDAYS[d.weekday()]} at {t12(hh, mm)}."]))
        return 1 if outcome == "accept" else 2

    # -- alarm offers
    def _alarm_offer(r, c, now, dev, outcome):
        key, user_t = r.choice(ALARM_DEV if dev else ALARM_TRAIN)
        eh = r.randint(8, 11)
        em = r.choice([0, 30])
        buf = r.choice([90, 105, 120, 150])
        wmin = eh * 60 + em - buf
        wh, wm = wmin // 60, wmin % 60
        assert wh >= 5
        E = f"{eh % 12 or 12}{':%02d' % em if em else ''}{'am' if eh < 12 else 'pm'}"
        Wt = t12(wh, wm)
        c.hist("user", user_t.format(E=E))
        c.hist("assistant", r.choice(OFFER_ALARM_DEV if dev else OFFER_ALARM).format(W=Wt))
        if outcome == "decline":
            _decline(r, c, "alarm", None, dev)
            return 0
        h, m = wh, wm
        if outcome == "accept":
            c.user(messy(r, _accept_user(r, dev)))
        else:
            for _ in range(20):
                h2 = max(5, wh + r.choice([-1, 0, 0, 1]))
                m2 = r.choice([0, 30])
                if (h2, m2) != (wh, wm):
                    break
            else:
                raise AssertionError("alarm")
            h, m = h2, m2
            sp = say_time(r, h, m, ref_h=wh)
            c.user(messy(r, r.choice(MODT_DEV if dev else MODT_TRAIN).format(t=sp), protect=(sp,)))
        c.discover(["automation"], ["set_alarm"])
        d = clock_date(now, h, m, gap=1)
        tt = t12(h, m)
        c.call("set_alarm", {"time": alarm_str(h, m)}, f"Alarm set for {tt}.")
        c.final(r.choice([f"Done - your alarm is set for {tt} {day_word(now, d)}.", f"Alarm set for {tt} {day_word(now, d)}."]))
        return 1 if outcome == "accept" else 2

    # -- timer offers
    def _timer_offer(r, c, now, dev, outcome):
        act, n, label = r.choice(TIMER_DEV if dev else TIMER_TRAIN)
        c.hist("user", f"how long should I {act}?")
        c.hist("assistant", r.choice(OFFER_TIMER_DEV if dev else OFFER_TIMER).format(n=n))
        if outcome == "decline":
            _decline(r, c, "timer", None, dev)
            return 0
        nn = n
        if outcome == "accept":
            c.user(messy(r, _accept_user(r, dev)))
        else:
            nn = r.choice([x for x in (2, 3, 4, 5, 6, 8, 10, 12, 15) if x != n])
            c.user(messy(r, r.choice(MODT_DEV if dev else MODT_TRAIN).format(t=f"{nn} minutes"), protect=(f"{nn} minutes",)))
        c.discover(["automation"], ["set_timer"])
        c.call("set_timer", {"duration": f"{nn} minutes", "label": label}, f"Timer '{label}' set for {nn} minutes.")
        c.final(r.choice([f"Started a {nn}-minute timer named {label}.", f"Timer running: {label}, {nn} minutes."]))
        return 1 if outcome == "accept" else 2

    # -- send offers (text, or email)
    def _statement(r, dev):
        for _ in range(60):
            it = text_intent(r)
            low = it["clause"].lower()
            if it["topic"] in TEXT_TOPICS_OK and " them" not in low and "their" not in low:
                return it
        raise AssertionError("statement")

    def _text_offer(r, c, now, dev, outcome):
        nm = pick_contact(r, 0.12)
        f = fn_(nm)
        it = _statement(r, dev)
        clause = it["clause"]
        user = r.choice(TEXT_USER_DEV if dev else TEXT_USER_LEAD).format(n=f, c=clause, C=cap(clause))
        c.hist("user", user)
        body = it["body"]
        if dev:
            offer = r.choice(OFFER_TEXT_DEV).format(f=f)
        elif r.random() < 0.4:
            offer = r.choice(OFFER_TEXT_Q).format(f=f, q=Q(body))
        else:
            offer = r.choice(OFFER_TEXT).format(f=f)
        c.hist("assistant", f"{r.choice(TEXT_ADVICE)} {offer}")
        if outcome == "decline":
            _decline(r, c, "text", f, dev)
            return 0
        as_email = False
        if outcome == "accept":
            c.user(messy(r, _accept_user(r, dev)))
        else:
            as_email = True
            c.user(messy(r, r.choice(CHAN_DEV).format(ch2="email") if dev else r.choice(CHAN_TRAIN).format(ch="email")))
        if as_email:
            c.discover(["mail", "messages_contacts"], ["lookup_contact", "send_email"])
            c.call("lookup_contact", {"name": nm if nm in RELATIVES else f}, lookup_result(nm, via="email"))
            ebody = EMAIL_FRAME.format(first=f, content=body)
            c.call("send_email", {"to": email_for(nm), "subject": "Quick note", "body": ebody}, sent_email(c, email_for(nm)))
            c.final(_fin_sent(r, "email", f, ebody))
        else:
            c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
            c.call("lookup_contact", {"name": nm if nm in RELATIVES else f}, lookup_result(nm))
            c.call("send_message", {"to": phone_for(nm), "text": body}, sent_msg(c, nm))
            c.final(_fin_sent(r, "message", f, body))
        return 1 if outcome == "accept" else 2

    def _email_offer(r, c, now, dev, outcome):
        nm = pick_contact(r, 0.1)
        f = fn_(nm)
        for _ in range(60):
            ei = email_intent(r, f)
            if ei["clause"].startswith("that "):
                break
        else:
            raise AssertionError("email intent")
        stmt = ei["clause"][5:]
        S = cap(stmt)
        c.hist("user", r.choice(EMAIL_USER_DEV if dev else EMAIL_USER).format(n=f, S=S))
        c.hist("assistant", f"{r.choice(TEXT_ADVICE)} {r.choice(OFFER_EMAIL_DEV if dev else OFFER_EMAIL).format(f=f)}")
        if outcome == "decline":
            _decline(r, c, "email", f, dev)
            return 0
        as_text = False
        if outcome == "accept":
            c.user(messy(r, _accept_user(r, dev)))
        else:
            as_text = True
            c.user(messy(r, r.choice(CHAN_DEV).format(ch2="text") if dev else r.choice(CHAN_TRAIN).format(ch="text")))
        name_arg = nm if nm in RELATIVES else f
        if as_text:
            c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
            c.call("lookup_contact", {"name": name_arg}, lookup_result(nm))
            c.call("send_message", {"to": phone_for(nm), "text": ei["core"]}, sent_msg(c, nm))
            c.final(_fin_sent(r, "message", f, ei["core"]))
        else:
            c.discover(["mail", "messages_contacts"], ["lookup_contact", "send_email"])
            c.call("lookup_contact", {"name": name_arg}, lookup_result(nm, via="email"))
            c.call("send_email", {"to": email_for(nm), "subject": ei["subject"], "body": ei["body"]}, sent_email(c, email_for(nm)))
            c.final(_fin_sent(r, "email", f, ei["body"]))
        return 1 if outcome == "accept" else 2

    OFFER_KINDS = [("rem", 34, _rem_offer), ("text", 20, _text_offer), ("email", 8, _email_offer), ("cal", 20, _cal_offer),
                   ("alarm", 10, _alarm_offer), ("timer", 8, _timer_offer)]

    DEV_OUTCOME = ["accept", "accept", "accept", "modify"]
    DEV_CYCLE = ["rem", "text", "cal", "rem"]       # dev has only a handful of rows: cover the main offer types in a fixed order

    def _accept(r, c, now, dev):
        if dev:
            _dev_state["i"] += 1
            want = DEV_CYCLE[_dev_state["i"] % len(DEV_CYCLE)]
            i = [k for k, _, _ in OFFER_KINDS].index(want)
            k, _, fnc = OFFER_KINDS[i]
        else:
            roll = r.random() * sum(w for _, w, _ in OFFER_KINDS)
            acc = 0
            for i, (k, w, fnc) in enumerate(OFFER_KINDS):
                acc += w
                if roll < acc:
                    break
        outcome = DEV_OUTCOME[_dev_state["i"] % len(DEV_OUTCOME)] if dev else _outcome(r)
        o = fnc(r, c, now, dev, outcome)
        return {"template": i * 10 + o, "slots": {"offer": k, "outcome": outcome}}

    @family(CAT, "fu_accept_offer", weight=16, failure=("followup_accept", "reread_instead_of_act"))
    def f_fu_accept_offer(r, c, now):
        return _accept(r, c, now, False)

    @family(CAT, "fu_dev_accept", split="dev", weight=4, failure=("followup_accept", "reread_instead_of_act"))
    def f_fu_dev_accept(r, c, now):
        return _accept(r, c, now, True)

    # ================================================================ 7. channel answer ("text or email?") -> matching send
    CH_USER = ["I have to get word to {n}: {c}", "can you pass this along to {n}? {C}", "{n} needs to hear this - {c}, tell them",
               "I need to notify {n} that {c}, get a message to them", "let's get a message over to {n}: {c}"]
    CH_ASK = ["Should that go by text or email?", "Text or email?", "Do you want me to text or email {f}?", "Would you like it as a text or an email?"]
    CH_TEXT = ["text it", "by text", "go with text", "imessage is fine", "text is fine", "over text, please", "make it a text"]
    CH_EMAIL = ["email it", "by email", "go with email", "email please", "an email, please", "make it an email", "email works"]

    @family(CAT, "fu_channel_answer", weight=8, failure=("followup_accept", "channel_answer_loop"))
    def f_fu_channel_answer(r, c, now):
        nm = pick_contact(r, 0.12)
        f = fn_(nm)
        it = _statement(r, False)
        c.hist("user", r.choice(CH_USER).format(n=f, c=it["clause"], C=cap(it["clause"])))
        c.hist("assistant", r.choice(CH_ASK).format(f=f))
        chan = "text" if r.random() < 0.5 else "email"
        ans = r.choice(CH_TEXT if chan == "text" else CH_EMAIL)
        c.user(messy(r, ans))
        name_arg = nm if nm in RELATIVES else f
        if chan == "text":
            c.discover(["messages_contacts"], ["lookup_contact", "send_message"])
            c.call("lookup_contact", {"name": name_arg}, lookup_result(nm))
            c.call("send_message", {"to": phone_for(nm), "text": it["body"]}, sent_msg(c, nm))
            c.final(_fin_sent(r, "message", f, it["body"]))
        else:
            c.discover(["mail", "messages_contacts"], ["lookup_contact", "send_email"])
            c.call("lookup_contact", {"name": name_arg}, lookup_result(nm, via="email"))
            body = EMAIL_FRAME.format(first=f, content=it["body"])
            c.call("send_email", {"to": email_for(nm), "subject": "Quick note", "body": body}, sent_email(c, email_for(nm)))
            c.final(_fin_sent(r, "email", f, body))
        return {"template": (CH_TEXT if chan == "text" else CH_EMAIL).index(ans) + (0 if chan == "text" else 10),
                "slots": {"channel": chan, "contact": nm, "topic": it["topic"]}}
