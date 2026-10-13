"""MESSY / REALISTIC tool-result families for the Ling V3 generator.

Real Wisp results are long, noisy, partial or truncated. These families keep the user-visible answer short and
derived (count / earliest / who / latest value) while the tool result carries quoted replies, signatures,
newsletter noise, near-duplicates, junk markdown, all-day items, truncation markers and failed sources.

Formats imitate service/tools/{email_tools,imessage_tools,notes_tools,assistant_tools,files_tools}.py; the
truncation marker is the verbatim text of service/agent/loop.py::_fit_tool_result (there it fires at 12,000
chars; here the budget is scaled down so the whole result stays under G.MESSY_RESULT_LIMIT).

Everything uses only the per-conversation `r`. All names/addresses are synthetic (.invalid).
"""
import datetime as dt
import re


def register(G):
    WD, MO = G.WEEKDAYS, G.MONTHS
    MX = "messy_result"
    LIM = G.MESSY_RESULT_LIMIT
    ME = "Test User <test.user@example.invalid>"

    # ------------------------------------------------------------ small helpers
    def fdt(d, h, m=0):
        return f"{WD[d.weekday()][:3]} {MO[d.month - 1][:3]} {d.day}, {d.year} {h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"

    def fsh(d, h, m=0):
        return f"{WD[d.weekday()][:3]} {MO[d.month - 1][:3]} {d.day}, {h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"

    def tt(h, m=0):
        return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"

    def ago(now, n):
        return now.date() - dt.timedelta(days=n)

    def cut(name, full, budget):
        """Wisp's real truncation marker (service/agent/loop.py::_fit_tool_result), budget scaled down."""
        assert len(full) > budget + 120
        kept = full[:budget].rsplit("\n", 1)[0]
        return (f"{kept}\n\n[... {len(full) - len(kept):,} more characters were cut to fit. This is NOT all of it — "
                f"do not describe what you have as complete. If you need what's missing, call {name} again with "
                f"narrower arguments (e.g. a `query`, a `day`, or a smaller `count`) rather than assuming the rest "
                f"is empty.]"), kept

    def start(c):
        c.result_limit = LIM

    def pick_one(r, seq):
        return seq[r.randrange(len(seq))]

    def cnt(n, word):
        return f"{n} {word}{'' if n == 1 else 's'}"

    def dw(day):
        return day.kind if day.kind in ("today", "tomorrow") else "on " + WD[day.date.weekday()]

    def mail_block(r, sender, subject, d, h, m, body, to=ME):
        return (f"From: {sender} <{G.email_for(sender)}>\nTo: {to}\nSubject: {subject}\nDate: {fdt(d, h, m)}\n"
                f"Message-ID: {G._msgid(r)}\n\n{body}")

    def quote(text, depth=1):
        pre = "> " * depth
        return "\n".join((pre + ln).rstrip() for ln in text.split("\n"))

    SIGS = ["Sent from my iPhone", "Thanks,\n{f}", "-- \n{f}\nOperations | Harbor & Finch",
            "Best,\n{f}\n\nThis message and any attachments may contain confidential information.",
            "Cheers,\n{f}\nSent via Outlook for iOS"]
    FILL = ["Lunch will be provided.", "Please wear comfortable shoes.", "Parking is free after 5.", "The side door sticks, pull hard.",
            "No need to bring anything.", "Dress is casual.", "I'll send photos afterwards.", "Coffee is on me."]

    # ------------------------------------------------------------ fact generator (old value -> updated value)
    NOUN = {"time": ("the time", "time"), "room": ("the room", "room"), "amount": ("the amount", "amount"),
            "deadline": ("the deadline", "deadline"), "code": ("the confirmation number", "confirmation number")}

    def fact(r, kind):
        if kind == "time":
            a, b = r.sample([(9, 0), (9, 30), (10, 0), (11, 30), (13, 0), (14, 0), (15, 30), (16, 0), (17, 30)], 2)
            o, n = tt(*a), tt(*b)
            return o, n, f"Right now I have us down for {o}.", f"Quick update: we're moving it to {n}."
        if kind == "room":
            a, b = r.sample(["Room 4B", "Room 2A", "Room 3C", "the Maple room", "Studio 5", "the annex"], 2)
            return a, b, f"We're booked into {a}.", f"Room change - it's {b} now."
        if kind == "amount":
            x = r.randrange(900, 1800, 20)
            y = x + r.choice([60, 80, 120, 140, 240])
            o, n = f"${x:,}", f"${y:,}"
            return o, n, f"The estimate came to {o}.", f"Revised total is {n} after the permit fee."
        if kind == "deadline":
            a, b = r.sample(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], 2)
            return a, b, f"Please send it by {a}.", f"Correction, I need it by {b} instead."
        a = "".join(r.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(2)) + "-" + str(r.randint(1000, 9999))
        b = "".join(r.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(2)) + "-" + str(r.randint(1000, 9999))
        return a, b, f"Your confirmation number is {a}.", f"They reissued it - the confirmation number is now {b}."

    SUBJ = [("Q4 offsite", "offsite"), ("kitchen remodel walkthrough", "remodel"), ("vendor contract renewal", "contract"),
            ("school auction volunteers", "auction"), ("garden fence quote", "fence"), ("workshop catering", "catering"),
            ("team retreat", "retreat"), ("studio lease", "lease")]

    # ------------------------------------------------------------ source builders (shared by several families)
    def mail_thread(r, now, dev=False, kind=None):
        """Three-message thread with quoted replies + signatures; the LAST message carries the current value."""
        s = G.pick_people(r, 1)[0]
        f = G.first(s)
        subj, kw = pick_one(r, SUBJ if not dev else [("neighborhood potluck", "potluck"), ("moving day crew", "crew"),
                                                    ("choir fundraiser", "fundraiser"), ("tax prep session", "tax")])
        kind = kind or r.choice(list(NOUN))
        old, new, so, sn = fact(r, kind)
        f1, f2 = r.sample(FILL, 2)
        sig = pick_one(r, SIGS).format(f=f)
        d1, d2, d3 = ago(now, 4), ago(now, 3), ago(now, 1)
        t1 = (r.randint(8, 11), r.choice([5, 20, 40]))
        t2 = (r.randint(12, 14), r.choice([2, 15, 33]))
        t3 = (r.randint(8, 16), r.choice([10, 25, 50]))
        wrote1 = f"On {fsh(d1, *t1)}, {s} wrote:"
        wrote2 = f"On {fsh(d2, *t2)}, Test User wrote:"
        b1 = f"Hi,\n\n{so} {f1}\n\n{sig}"
        b2 = f"Sounds good, thanks!\n\n{wrote1}\n{quote(so)}\n{quote(f1)}"
        b3 = (f"Hi again,\n\n{sn} {f2}\n\n{sig}\n\n{wrote2}\n{quote('Sounds good, thanks!')}\n>\n> {wrote1}\n{quote(so, 2)}")
        text = "\n\n---\n\n".join([
            mail_block(r, s, subj[0].upper() + subj[1:], d1, *t1, b1),
            mail_block(r, "Test User", f"Re: {subj}", d2, *t2, b2, to=f"{s} <{G.email_for(s)}>"),
            mail_block(r, s, f"Re: {subj}", d3, *t3, b3)])
        noun, short = NOUN[kind]
        return dict(fam="mail", tool="view_emails", args={"query": kw, "count": 3}, result=text, s=s, f=f, subj=subj, kw=kw,
                    kind=kind, old=old, new=new, noun=noun, short=short, when=fsh(d3, *t3).rsplit(",", 1)[0], d3=d3,
                    prompt=f"what {short} {f} gave for the {subj}", summary=f"{noun} from {f} for the {subj} is {new} (earlier email said {old})",
                    fail=("(No raw email content cached right now — either Mail.app hasn't synced yet, or the cache expired "
                          "after a day and is waiting on the next daily sync. Try again shortly, or use summarize_emails for "
                          "a digest in the meantime.)"),
                    label="your email", reason="Mail hasn't synced yet", cant=f"what {short} {f} gave")

    PLACES = ["Luigi's", "the corner cafe", "Harbor Grill", "the park pavilion", "Osteria Blu", "the rooftop bar", "Cafe Verde", "the boathouse"]
    FOODS = ["the salad", "two bottles of wine", "dessert", "the garlic bread", "ice and drinks", "a veggie tray", "the folding chairs"]
    FILL_TXT = [("them", "haha yes"), ("me", "omg same"), ("them", "sent you the pics btw"), ("me", "looking now, so good"),
                ("them", "can you grab milk if you pass the store?"), ("me", "sure"), ("them", "lol"), ("me", "ok ok"),
                ("them", "did you see the game"), ("me", "not yet")]

    def msg_thread(r, now, dev=False, kind=None):
        nm = G.pick_contact(r, 0.12)
        f = nm if nm in G.RELATIVES else G.first(nm)
        ev = pick_one(r, ["dinner", "the barbecue", "game night", "the hike", "brunch", "movie night"] if not dev
                      else ["the potluck", "the road trip", "trivia night", "the picnic"])
        kind = kind or r.choice(["place", "time", "bring"])
        mine = "Me"
        lines = []
        if kind == "place":
            a, b, cc = r.sample(DEV_PLACES if dev else PLACES, 3)
            script = [("them", f"ok so {ev} - how about {a}?"), ("me", "works for me"), ("them", f"ugh {a} is booked actually"),
                      ("them", f"{b}?"), ("me", "too far for me"), ("them", f"fine, {cc} then. final answer"),
                      ("me", "perfect")]
            summary = f"{f} settled on {cc} for {ev} (after first suggesting {a} and {b})"
            prompt = f"where {f} and I are meeting for {ev}"
            final = cc
        elif kind == "time":
            ts = r.sample([(17, 30), (18, 0), (18, 30), (19, 0), (19, 30), (20, 0)], 3)
            a, b, cc = [tt(*x) for x in ts]
            script = [("them", f"{ev} at {a}?"), ("me", "can we do a bit later"), ("them", f"{b} then"),
                      ("me", "traffic might be rough"), ("them", f"ok making it {cc}, I'll tell the others"), ("me", "great")]
            summary = f"{ev} is now at {cc} per {f} (moved from {a} and {b})"
            prompt = f"what time {f} said {ev} is"
            final = cc
        else:
            a, b = r.sample(FOODS, 2)
            script = [("them", f"I'll bring {a} for {ev}"), ("me", "nice"), ("them", f"actually swapping, bringing {b} instead"),
                      ("them", f"Sam is doing {a}"), ("me", "got it")]
            summary = f"{f} is bringing {b} to {ev} (not {a}, which Sam has)"
            prompt = f"what {f} is bringing to {ev}"
            final = b
        pads = r.sample(FILL_TXT, r.randint(2, 4))
        pos = r.sample(range(1, len(script) + 1), len(pads))
        seq = list(script)
        for p, x in sorted(zip(pos, pads), reverse=True):
            seq.insert(p, x)
        d0 = ago(now, 2)
        h, m = r.randint(9, 14), r.randint(0, 20)
        for i, (who, txt) in enumerate(seq):
            m += r.randint(2, 9)
            if m >= 60:
                h, m = h + 1, m - 60
            d = d0 if h < 21 else d0
            sender = nm if who == "them" else mine
            lines.append(f"[{fsh(d, h, m)}] {nm} — {sender}: {txt}")
        conv = nm if nm in G.RELATIVES else (G.first(nm) if r.random() < 0.7 else nm)
        return dict(fam="messages_contacts", tool="view_messages", args={"conversation": conv, "count": 15}, result="\n".join(lines),
                    f=f, ev=ev, kind=kind, final=final, summary=summary, prompt=prompt,
                    fail=("(No message data yet. Grant Wisp Full Disk Access — System Settings > Privacy & Security > Full Disk "
                          "Access > Wisp — then try again.)"),
                    label="your texts", reason="Messages needs Full Disk Access", cant=f"what {f} said", nm=nm)

    LISTS = [("packing", "Packing list - Lisbon trip", ["passport", "phone charger", "rain jacket", "sunscreen", "adapter", "toothbrush", "camera", "sandals", "headphones"]),
             ("party", "Birthday party prep", ["order cake", "buy balloons", "confirm guest list", "borrow speakers", "ice", "paper plates", "playlist", "candles"]),
             ("moving", "Moving checklist", ["box books", "cancel internet", "forward mail", "book movers", "pack kitchen", "return keys", "label boxes", "change address"]),
             ("camping", "Camping trip list", ["tent", "sleeping bags", "stove", "headlamps", "first aid kit", "water filter", "bug spray", "map"])]
    DEV_LISTS = [("garage", "Garage cleanup", ["sort tools", "donate paint cans", "sweep floor", "hang shelves", "recycle boxes", "oil the door", "label bins", "fix light"]),
                 ("school", "Back to school list", ["backpack", "lunchbox", "pencils", "gym shoes", "water bottle", "notebooks", "calculator", "locker lock"])]
    DEV_PLACES = ["the lakeside deck", "Rosa's kitchen", "the community hall", "the old mill", "Pier 9 cafe", "the hilltop lookout"]
    DEV_CAL = [("Quarterly planning", "Room 5"), ("Tutor session", None), ("Tentative: ??? call", None), ("Oil change", "Garage on 5th"),
               ("Pottery class", "Clay House"), ("RE: RE: budget thing", None), ("Piano recital rehearsal", "Hall B"), ("Dog groomer", "Fluff & Co"),
               ("Phone w/ accountant", None), ("Team lunch (maybe)", "Cafe Verde"), ("Parent call", None), ("Standing desk delivery", None)]
    JUNK = ["Links: https://maps.example.invalid/p/8f3k", "TODO?? ask Dana", "(see other note)", "---", "idea: laminate this", "~~old plan scrapped~~"]

    def checklist_note(r, now, dev=False):
        key, title, items = pick_one(r, DEV_LISTS if dev else LISTS)
        n = r.randint(6, 8)
        its = r.sample(items, n)
        done = r.sample(range(n), r.randint(2, n - 2))
        body = [f"## {title.split(' - ')[0]}"]
        junk = r.sample(JUNK, 2)
        for i, it in enumerate(its):
            body.append(f"- [{'x' if i in done else ' '}] {it}")
            if i == 2:
                body.append(junk[0])
        body.append(junk[1])
        left = [it for i, it in enumerate(its) if i not in done]
        # near-duplicate OLDER copy with different ticks
        old = [f"## {title.split(' - ')[0]} (copy)"] + [f"- [{r.choice('x ')}] {it}" for it in its[:5]]
        t1, t2 = (r.randint(8, 18), r.choice([4, 31, 52])), (r.randint(8, 18), r.choice([9, 27, 44]))
        text = (f"Title: {title} [Notes]\nModified: {fdt(ago(now, 1), *t1)}\n\n" + "\n".join(body) + "\n\n---\n\n"
                f"Title: {title} (old) [Notes]\nModified: {fdt(ago(now, 9), *t2)}\n\n" + "\n".join(old))
        return dict(fam="notes", tool="search_notes", args={"query": key}, result=text, key=key, title=title, left=left,
                    prompt=f"how many things are left on my {key} checklist",
                    summary=f"{cnt(len(left), 'item')} left on your {key} checklist ({', '.join(left)})",
                    fail="Wisp is still syncing your notes. Try again once the sync finishes.", label="your notes",
                    reason="Notes is still syncing", cant=f"how many items are left on your {key} checklist")

    CAL_NORMAL = [("Standup", None), ("HOLD - do not book", None), ("Tentative: vendor sync?", None), ("FW: Q4 numbers review", "Room 2A"),
                  ("Dentist (reschedule!!)", "Brightside Dental"), ("1:1 w/ Priya", None), ("Pick up Zoe", "School gate"),
                  ("Lunch - Luigi's", None), ("Sprint demo", "Zoom"), ("Gym", "Lotus Studio"), ("Call w/ landlord", None),
                  ("Board mtg prep", "Room 4B"), ("Vet - Biscuit's shots", "Paws Vet"), ("Design crit", "Room 4B"),
                  ("Travel: SFO -> PDX", "Gate B12"), ("Coffee chat", "Cafe Verde")]
    CAL_ALLDAY = ["Out of office - Dev", "Zoe's birthday", "Conference (day 2)", "Holiday: office closed"]

    def day_heading(d, now):
        if d == now.date() + dt.timedelta(days=1):
            return f"Tomorrow · {WD[d.weekday()][:3]} {MO[d.month - 1][:3]} {d.day}"
        return f"{WD[d.weekday()]} · {MO[d.month - 1][:3]} {d.day}"

    def agenda_lines(evs, rems):
        out = ["  Calendar events"]
        for e in evs:
            if e["allday"]:
                out.append(f"  - All day — {e['title']}")
            else:
                out.append(f"  - {tt(e['h'], e['m'])} — {e['title']}" + (f" @ {e['loc']}" if e["loc"] else ""))
        if rems:
            out.append("  Reminders")
            out += [f"  - {tt(h, m)} — {t}" for h, m, t in rems]
        return out

    def agenda_head(window, evs, rems):
        return (f"Upcoming — {window} ({len(evs) + len(rems)} item(s))\nCalendar events: {len(evs)}; Wisp/Apple reminders: {len(rems)}. "
                "[Calendar event] and [Reminder] are shown in separate sections.\n\n")

    def make_events(r, n, allday=True, avoid=None, titles=None):
        pool = list(titles or CAL_NORMAL)
        r.shuffle(pool)
        slots = [(h, m) for h in range(8, 19) for m in (0, 30)]
        r.shuffle(slots)
        evs, used = [], []
        for h, m in slots:
            if len(evs) >= n:
                break
            if avoid and any(abs((h * 60 + m) - a) < 60 for a in avoid):
                continue
            t, loc = pool[len(evs)]
            evs.append(dict(h=h, m=m, title=t, loc=loc, allday=False))
        evs.sort(key=lambda e: (e["h"], e["m"]))
        if allday:
            evs.insert(0, dict(h=0, m=0, title=r.choice(CAL_ALLDAY), loc=None, allday=True))
        return evs

    def cal_day(r, now, dev=False, kind=None):
        day = G.clean_day(G.pick_day(r, now, kinds=("tomorrow", "weekday")))
        n = r.randint(5, 8)
        evs = make_events(r, n, allday=(kind == "allday" or r.random() < 0.55), titles=DEV_CAL if dev else None)
        rems = sorted([(r.randint(8, 20), r.choice([0, 30]), x) for x in r.sample(G.REMINDER_BANK, r.randint(1, 3))])
        window = day.kind if day.kind != "date" else day.period
        text = agenda_head(window, evs, rems) + day_heading(day.date, now) + "\n" + "\n".join(agenda_lines(evs, rems))
        timed = [e for e in evs if not e["allday"]]
        return dict(fam="calendar", tool="get_upcoming", args={"period": day.period}, result=text, day=day, evs=evs, timed=timed, rems=rems,
                    dw=dw(day), fail="Wisp could not check Calendar. Check Calendar access in Settings, then try again.",
                    label="your calendar", reason="Calendar access isn't working", window=window)

    # ------------------------------------------------------------ family 1: long mail thread, one fact matters
    T_THREAD = ["in {s}'s emails about the {subj}, what's {noun}", "what {short} did {s} land on in the {subj} thread",
                "dig through the {subj} emails from {s} and tell me {noun}", "{s} sent a few emails on the {subj} - what's {noun} now",
                "what's the latest {short} for the {subj}, per {s}'s email", "check {s}'s {subj} thread: what is {noun}?"]

    @G.family("reads", "mx_mail_thread", weight=15, failure=(MX, "stale_fact_from_quote", "derived_answer_dumped_raw"))
    def f_mx_mail_thread(r, c, now):
        start(c)
        S = mail_thread(r, now)
        tpl = r.choice(T_THREAD)
        c.user(G.messy(r, tpl.format(s=S["f"], subj=S["subj"], noun=S["noun"], short=S["short"]), protect=(S["f"],)))
        c.discover(["mail"], ["view_emails"])
        c.call(S["tool"], S["args"], S["result"])
        c.final(r.choice([f"{S['f']}'s latest email ({S['when']}) says {S['noun']} is {S['new']} - that replaced the earlier {S['old']}.",
                          f"As of {S['f']}'s newest message, {S['noun']} for the {S['subj']} is {S['new']} (it was {S['old']} before)."]))
        return {"template": T_THREAD.index(tpl), "slots": {"kind": S["kind"], "subj": S["kw"]}}

    # ------------------------------------------------------------ family 2: inbox digest with newsletter noise
    BULK = [("Harbor Weekly", "This week in Harbor City"), ("ShopLane Deals", "48-hour flash sale"), ("Transit Alerts", "Service change on Line 4"),
            ("Lotus Studio", "New class schedule"), ("BankNote", "Your statement is ready"), ("Parcel Tracker", "Out for delivery"),
            ("Cafe Verde Rewards", "You're 2 stamps away")]
    PSUBJ = [("Design review notes", "design"), ("Lease renewal", "lease"), ("Workshop schedule", "workshop"), ("Conference travel", "conference"),
             ("Invoice 2048", "invoice"), ("Team lunch", "lunch"), ("Contract draft", "contract"), ("Garage quote", "garage"), ("Gym membership", "gym")]

    def digest(r, now, psubj, bulk, n_people, n_bulk, heavy=False):
        people = G.pick_people(r, n_people)
        items = []
        subs = r.sample(psubj, n_people)
        for p, (sj, kw) in zip(people, subs):
            items.append(dict(sender=p, subject=sj, kw=kw, bulk=False))
        if heavy:  # first person writes again (Re:) so there is a unique 'most'
            extra = r.randint(1, 2)
            for k in range(extra):
                items.append(dict(sender=people[0], subject=("Re: " * (k + 1)) + subs[0][0], kw=subs[0][1], bulk=False))
        for s, sj in r.sample(bulk, n_bulk):
            items.append(dict(sender=s, subject=sj, kw=None, bulk=True))
        times = sorted(r.sample([(h, m) for h in range(6, 23) for m in (4, 17, 31, 46, 58)], len(items)), reverse=True)
        for it, (h, m) in zip(items, times):
            it["h"], it["m"], it["un"] = h, m, r.random() < 0.55
        r.shuffle(items)
        items.sort(key=lambda it: (-it["h"], -it["m"]))
        y = ago(now, 1)
        lines = [f"{len(items)} email(s) yesterday ({G.dshort(y)}):"]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}) {tt(it['h'], it['m'])} · {it['sender']} — {it['subject']}" + (" (unread)" if it["un"] else ""))
        return items, "\n".join(lines)

    T_DIG = [("people", "how many of yesterday's emails are from actual people, not newsletters or receipts"),
             ("people", "yesterday's inbox - count the ones written by real humans"),
             ("first", "what was the first email I got yesterday"),
             ("first", "which email came in earliest yesterday"),
             ("find", "did anybody email me about {kw} yesterday"),
             ("find", "who wrote to me about the {kw} yesterday, if anyone"),
             ("most", "who emailed me the most yesterday"),
             ("most", "which person was blowing up my inbox yesterday")]

    @G.family("reads", "mx_mail_digest", weight=12, failure=(MX, "derived_answer_dumped_raw", "irrelevant_items_summarized"))
    def f_mx_mail_digest(r, c, now):
        start(c)
        kind, tpl = r.choice(T_DIG)
        items, text = digest(r, now, PSUBJ, BULK, r.randint(3, 5), r.randint(4, 6), heavy=(kind == "most"))
        ppl = [it for it in items if not it["bulk"]]
        target = None
        if kind == "find":
            target = pick_one(r, [it for it in ppl if not it["subject"].startswith("Re:")])
        kw = target["kw"] if target else ""
        c.user(G.messy(r, tpl.format(kw=kw)))
        c.discover(["mail"], ["summarize_emails"])
        c.call("summarize_emails", {"day": "yesterday"}, text)
        if kind == "people":
            names = list(dict.fromkeys(G.first(it["sender"]) for it in ppl))
            c.final(f"{len(ppl)} of the {len(items)} are from real people ({', '.join(names)}); the rest are newsletters, receipts and alerts.")
        elif kind == "first":
            it = items[-1]
            c.final(f"{it['sender']} - “{it['subject']}” at {tt(it['h'], it['m'])}.")
        elif kind == "find":
            c.final(f"Yes - {G.first(target['sender'])} sent “{target['subject']}” at {tt(target['h'], target['m'])}.")
        else:
            tally = {}
            for it in ppl:
                tally[it["sender"]] = tally.get(it["sender"], 0) + 1
            top = max(tally, key=tally.get)
            c.final(f"{G.first(top)} - {tally[top]} emails (counting the “Re:” follow-ups).")
        return {"template": T_DIG.index((kind, tpl)), "slots": {"kind": kind}}

    # ------------------------------------------------------------ family 3: long text thread, many back-and-forths
    T_MSG = [("place", "where {n} and I ended up deciding to meet for {ev}, check our texts"),
             ("place", "check my texts with {n}: where are we going for {ev} in the end"),
             ("time", "what time did {n} say {ev} is, it's buried in our texts"),
             ("time", "look through my messages with {n} - what's the final time for {ev}"),
             ("bring", "what did {n} say they're bringing to {ev}"),
             ("bring", "go through my texts with {n}, what are they bringing for {ev} now")]

    @G.family("reads", "mx_msg_thread", weight=12, failure=(MX, "first_value_instead_of_final", "derived_answer_dumped_raw"))
    def f_mx_msg_thread(r, c, now):
        start(c)
        kind, tpl = r.choice(T_MSG)
        S = msg_thread(r, now, kind=kind)
        said, _ = G.name_forms(r, S["nm"])
        c.user(G.messy(r, tpl.format(n=said, ev=S["ev"]), protect=(said,)))
        c.discover(["messages_contacts"], ["view_messages"])
        c.call(S["tool"], S["args"], S["result"])
        c.final(S["summary"][:1].upper() + S["summary"][1:] + ".")
        return {"template": T_MSG.index((kind, tpl)), "slots": {"kind": kind, "ev": S["ev"]}}

    # ------------------------------------------------------------ family 4: notes with markdown / junk / near-duplicate copy
    BUDGET = [("Venue", 450), ("Catering", 780), ("Cake", 65), ("Decor", 120), ("Photographer", 300), ("Invitations", 90), ("Music", 150), ("Flowers", 210)]
    T_NOTE = [("left", "how many things are left on my {key} checklist"), ("left", "what's still unchecked on my {key} list"),
              ("left", "go through my {key} note - what do I still need to do"),
              ("sum", "add up the party budget in my notes"), ("sum", "what's my total on the party budget note"),
              ("sum", "tally the budget lines in my party note")]

    @G.family("reads", "mx_notes_list", weight=12, failure=(MX, "stale_copy_used", "derived_answer_dumped_raw"))
    def f_mx_notes_list(r, c, now):
        start(c)
        kind, tpl = r.choice(T_NOTE)
        if kind == "left":
            S = checklist_note(r, now)
            c.user(G.messy(r, tpl.format(key=S["key"])))
            c.discover(["notes"], ["search_notes"])
            c.call(S["tool"], S["args"], S["result"])
            left = S["left"]
            c.final(f"{cnt(len(left), 'thing')} left on the current “{S['title']}” note: {', '.join(left)}. (There's also an older copy; I ignored it.)")
        else:
            lines = r.sample(BUDGET, r.randint(5, 7))
            cancel = r.randrange(len(lines))
            body = ["## Party budget", "Total: ???"]
            tot = 0
            gone = None
            for i, (nm_, v) in enumerate(lines):
                if i == cancel:
                    body.append(f"- ~~{nm_}: ${v}~~ cancelled")
                    gone = (nm_, v)
                else:
                    body.append(f"- {nm_}: ${v}")
                    tot += v
                if i == 2:
                    body.append("Links: https://pay.example.invalid/q/77af")
            body.append("idea: ask Dana to split the cake")
            old = ["## Party budget (draft)"] + [f"- {n_}: ${v + 40}" for n_, v in lines[:3]]
            text = (f"Title: Party budget [Notes]\nModified: {fdt(ago(now, 2), 18, 12)}\n\n" + "\n".join(body) + "\n\n---\n\n"
                    f"Title: Party budget (draft) [Notes]\nModified: {fdt(ago(now, 12), 9, 40)}\n\n" + "\n".join(old))
            c.user(G.messy(r, tpl))
            c.discover(["notes"], ["search_notes"])
            c.call("search_notes", {"query": "party budget"}, text)
            c.final(f"${tot:,} - that's the current note's lines, not counting the cancelled {gone[0]} (${gone[1]}).")
        return {"template": T_NOTE.index((kind, tpl)), "slots": {"kind": kind}}

    # ------------------------------------------------------------ family 5: busy calendar with all-day / odd titles
    T_CAL = [("first", "what's my first meeting {d}"), ("first", "what time do I start {d}"),
             ("last", "what's the last thing on my calendar {d}"), ("count", "how many calendar events do I have {d}, not counting reminders"),
             ("allday", "is there anything all day {d}"), ("allday", "do I have an all-day thing {d}"),
             ("after", "what's on right after my {anchor} {d}"), ("after", "what comes next after {anchor} {d}")]

    @G.family("reads", "mx_cal_busy", weight=15, failure=(MX, "derived_answer_dumped_raw", "reminders_counted_as_events"))
    def f_mx_cal_busy(r, c, now):
        start(c)
        kind, tpl = r.choice(T_CAL)
        S = cal_day(r, now, kind=kind)
        timed = S["timed"]
        anchor = ""
        if kind == "after":
            idx = r.randrange(len(timed) - 1)
            anchor = timed[idx]["title"]
        c.user(G.messy(r, tpl.format(d=S["day"].spoken, anchor=anchor), protect=(anchor,) if anchor else ()))
        c.discover(["calendar"], ["get_upcoming"])
        c.call(S["tool"], S["args"], S["result"])
        d = S["dw"]
        if kind == "first":
            e = timed[0]
            c.final(f"{e['title']} at {tt(e['h'], e['m'])} {d}.")
        elif kind == "last":
            e = timed[-1]
            c.final(f"{e['title']}, starting at {tt(e['h'], e['m'])} {d}.")
        elif kind == "count":
            ad = sum(1 for e in S["evs"] if e["allday"])
            c.final(f"{len(S['evs'])} calendar events {d}" + (f" (one is all-day)" if ad else "") + f", plus {cnt(len(S['rems']), 'reminder')} that I didn't count.")
        elif kind == "allday":
            e = next(e for e in S["evs"] if e["allday"])
            c.final(f"Yes - “{e['title']}” is all day {d}.")
        else:
            e = timed[idx + 1]
            c.final(f"{e['title']} at {tt(e['h'], e['m'])}.")
        return {"template": T_CAL.index((kind, tpl)), "slots": {"kind": kind}}

    # ------------------------------------------------------------ family 6 (compound): two long reads, both succeed
    PAIR_JOIN = ["{a}, and also {b}", "{a} - and {b}", "two things: {a}, and {b}", "{a}; separately, {b}"]

    def src(r, now, which, dev=False):
        if which == "cal":
            S = cal_day(r, now, dev)
            e0 = S["timed"][0]
            S.update(prompt=f"what my first meeting is {S['dw']}",
                     summary=f"your first meeting {S['dw']} is {e0['title']} at {tt(e0['h'], e0['m'])}",
                     cant=f"what your first meeting is {S['dw']}")
            return S
        if which == "notes":
            return checklist_note(r, now, dev)
        if which == "msgs":
            return msg_thread(r, now, dev, kind="place")
        return mail_thread(r, now, dev)

    SRC_FAMS = {"cal": "calendar", "notes": "notes", "msgs": "messages_contacts", "mail": "mail"}

    def pair_prompt(r, A, B, join):
        a, b = A["prompt"], B["prompt"]
        return join.format(a="tell me " + a, b=b)

    @G.family("compound", "mx_cmp_pair", weight=24, failure=(MX, "multi_source_partial", "derived_answer_dumped_raw"))
    def f_mx_cmp_pair(r, c, now):
        start(c)
        wa, wb = r.sample(["cal", "notes", "msgs", "mail"], 2)
        A, B = src(r, now, wa), src(r, now, wb)
        join = r.choice(PAIR_JOIN)
        prot = tuple(x for x in (A.get("f"), B.get("f")) if x)
        c.user(G.messy(r, pair_prompt(r, A, B, join), protect=prot))
        c.discover(list(dict.fromkeys([A["fam"], B["fam"]])), [A["tool"], B["tool"]])
        c.calls([(A["tool"], A["args"], A["result"]), (B["tool"], B["args"], B["result"])])
        sa, sb = A["summary"], B["summary"]
        c.final(f"{sa[:1].upper() + sa[1:]}; {sb}.")
        return {"template": PAIR_JOIN.index(join), "slots": {"a": wa, "b": wb}}

    # ------------------------------------------------------------ family 7 (compound): email vs text disagree
    T_CONF = ["{s} emailed and texted me about the {subj} - do the two agree on {noun}", "compare what {s} said by email and by text about the {subj}: same {short}",
              "{s} and I went back and forth by email and text on the {subj}, what's {noun} really"]

    @G.family("compound", "mx_cmp_conflict", weight=18, failure=(MX, "conflicting_sources_ignored", "multi_source_partial"))
    def f_mx_cmp_conflict(r, c, now):
        start(c)
        S = mail_thread(r, now, kind=r.choice(["time", "room", "deadline"]))
        f, s = S["f"], S["s"]
        agree = r.random() < 0.3
        text_newer = r.random() < 0.7
        kind = S["kind"]
        tv = S["new"] if agree else fact(r, kind)[1]
        if tv == S["new"] and not agree:
            raise AssertionError("need a different value")
        td = ago(now, 0 if text_newer else 5)
        h, m = r.randint(7, 9), r.randint(0, 50)
        verb = {"time": "the time is", "room": "we're in", "deadline": "it's due"}[kind]
        lines = [f"[{fsh(td, h, m)}] {s} — {s}: hey, quick thing on the {S['subj']}",
                 f"[{fsh(td, h, m + 3)}] {s} — {s}: {verb} {tv}",
                 f"[{fsh(td, h, m + 5)}] {s} — Me: ok thanks"]
        if not text_newer:   # email thread is already ago(1); make text clearly older
            pass
        mres = "\n".join(lines)
        tpl = r.choice(T_CONF)
        c.user(G.messy(r, tpl.format(s=f, subj=S["subj"], noun=S["noun"], short=S["short"]), protect=(f,)))
        c.discover(["mail", "messages_contacts"], ["view_emails", "view_messages"])
        c.calls([(S["tool"], S["args"], S["result"]), ("view_messages", {"conversation": f, "count": 8}, mres)])
        email_d = G.dshort(S["d3"])
        if agree:
            c.final(f"Yes - the email (latest {email_d}) and the text both say {S['noun']} is {S['new']}.")
        else:
            newer = tv if text_newer else S["new"]
            c.final(f"They don't agree: the latest email ({email_d}) says {S['new']}, the text ({G.dshort(td)}) says {tv}. "
                    f"The {'text' if text_newer else 'email'} is newer, so {newer} - worth confirming with {f}.")
        return {"template": T_CONF.index(tpl), "slots": {"kind": kind, "agree": agree}}

    # ------------------------------------------------------------ family 8 (compound): email proposes a day -> check calendar
    T_MC = ["{s} proposed a day for the {subj} by email - does it clash with anything on my calendar",
            "check {s}'s email about the {subj}, then see whether the day they suggest is free",
            "is the date {s} emailed for the {subj} open on my calendar"]

    @G.family("compound", "mx_cmp_mail_cal", weight=18, failure=(MX, "compound_never_completes", "stale_fact_from_quote"))
    def f_mx_cmp_mail_cal(r, c, now):
        start(c)
        s = G.pick_people(r, 1)[0]
        f = G.first(s)
        subj, kw = pick_one(r, SUBJ)
        day = G.clean_day(G.pick_day(r, now, kinds=("weekday",)))
        old_day = day.date + dt.timedelta(days=r.choice([-2, 1, 2]))
        if old_day <= now.date():
            raise AssertionError("old day in the past")
        ph, pm = r.choice([(10, 0), (11, 30), (13, 0), (14, 0), (15, 30), (16, 0)])
        new_s = f"Can we do {WD[day.date.weekday()]}, {MO[day.date.month - 1]} {day.date.day} at {tt(ph, pm)}?"
        old_s = f"How about {WD[old_day.weekday()]}, {MO[old_day.month - 1]} {old_day.day} at {tt(ph + 1 if ph < 16 else ph - 1, pm)}?"
        sig = pick_one(r, SIGS).format(f=f)
        d1, d2 = ago(now, 3), ago(now, 1)
        t1, t2 = (r.randint(8, 11), 12), (r.randint(9, 16), 41)
        b1 = f"Hi,\n\n{old_s} {r.choice(FILL)}\n\n{sig}"
        b2 = f"Hi - scratch that.\n\n{new_s} {r.choice(FILL)}\n\n{sig}\n\nOn {fsh(d1, *t1)}, {s} wrote:\n{quote(old_s)}"
        mres = "\n\n---\n\n".join([mail_block(r, s, subj[0].upper() + subj[1:], d1, *t1, b1), mail_block(r, s, f"Re: {subj}", d2, *t2, b2)])
        clash = r.random() < 0.55
        avoid = None if clash else [ph * 60 + pm]
        evs = make_events(r, r.randint(5, 7), allday=r.random() < 0.4, avoid=avoid)
        if clash:
            near = pick_one(r, [x for x in range(len(evs)) if not evs[x]["allday"]])
            off = r.choice([-30, 0, 30])
            tot = ph * 60 + pm + off
            evs[near]["h"], evs[near]["m"] = tot // 60, tot % 60
            evs = [e for i, e in enumerate(evs) if i == near or e["allday"] or abs(e["h"] * 60 + e["m"] - (ph * 60 + pm)) >= 60]
            evs.sort(key=lambda e: (not e["allday"], e["h"], e["m"]))
            evs.sort(key=lambda e: (0 if e["allday"] else 1, e["h"], e["m"]))
        rems = sorted([(r.randint(8, 20), 0, x) for x in r.sample(G.REMINDER_BANK, 2)])
        cal = agenda_head(day.period, evs, rems) + day_heading(day.date, now) + "\n" + "\n".join(agenda_lines(evs, rems))
        tpl = r.choice(T_MC)
        c.user(G.messy(r, tpl.format(s=f, subj=subj), protect=(f,)))
        c.discover(["mail", "calendar"], ["view_emails", "get_upcoming"])
        c.call("view_emails", {"query": kw, "count": 3}, mres)
        c.call("get_upcoming", {"period": day.period}, cal)
        conf = [e for e in evs if not e["allday"] and abs(e["h"] * 60 + e["m"] - (ph * 60 + pm)) < 60]
        when = f"{WD[day.date.weekday()]}, {MO[day.date.month - 1]} {day.date.day} at {tt(ph, pm)}"
        if conf:
            e = conf[0]
            c.final(f"{f}'s latest email proposes {when} (it replaced an earlier date). That clashes with “{e['title']}” at {tt(e['h'], e['m'])}.")
        else:
            c.final(f"{f}'s latest email proposes {when} (it replaced an earlier date), and nothing on your calendar overlaps it.")
        return {"template": T_MC.index(tpl), "slots": {"clash": bool(conf), "kw": kw}}

    # ------------------------------------------------------------ truncation (errors): real marker
    VENDORS = [("Skyline Air", "Skyline", "reservation code"), ("Brightside Hotels", "Brightside", "booking number"),
               ("ParcelPost", "ParcelPost", "tracking number"), ("Maple Rentals", "Maple Rentals", "rental confirmation code")]
    PROMO = ["Fall deals are here - save up to 30% on weekend trips. Unsubscribe: https://mail.example.invalid/u/48812",
             "We miss you! Come back and enjoy double points this month. View in browser: https://mail.example.invalid/v/5521",
             "Your monthly newsletter: five hidden gems, a packing guide and member stories. Manage preferences: https://mail.example.invalid/p/902"]
    T_TR = ["what's the {thing} in the latest {vendor} email", "find the {thing} from {vendor}'s most recent email",
            "I need the newest {thing} that {vendor} sent me", "grab the {thing} out of the last {vendor} email"]

    def code(r):
        return "".join(r.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(3)) + str(r.randint(100, 999))

    @G.family("errors", "mx_trunc_requery", weight=21, failure=(MX, "truncation_ignored", "answered_from_partial", "identical_retry"))
    def f_mx_trunc_requery(r, c, now):
        start(c)
        if r.random() < 0.65:
            vend, kw, thing = pick_one(r, VENDORS)
            tpl = r.choice(T_TR)
            new, oldc = code(r), code(r)
            blocks = []
            ds = [ago(now, k) for k in (30, 24, 17, 9, 2)]
            for i, d in enumerate(ds):
                if i == 1:
                    body = f"Thanks for booking with {vend}. Your {thing} is {oldc}. Manage trip: https://mail.example.invalid/t/{r.randint(1000, 9999)}"
                elif i == 4:
                    body = f"Your trip is confirmed. Your {thing} is {new}. Have a great journey!\n\n{vend} Customer Care"
                else:
                    body = pick_one(r, PROMO)
                blocks.append(mail_block(r, vend, f"{vend}: " + ("Booking confirmed" if i in (1, 4) else "Deals for you"), d, 8 + i, 10, body))
            full = "Result coverage: returned 5 of 7 available matching emails; 2 older emails were omitted by count=5.\n\n" + "\n\n---\n\n".join(blocks)
            first, kept = cut("view_emails", full, r.randint(560, 700))
            c.user(G.messy(r, tpl.format(thing=thing, vendor=vend), protect=(vend,)))
            c.discover(["mail"], ["view_emails"])
            c.call("view_emails", {"query": kw, "count": 5}, first)
            second = (f"Result coverage: returned 1 of 7 available matching emails; 6 older emails were omitted by count=1.\n\n" +
                      blocks[4])
            c.call("view_emails", {"query": kw, "count": 1}, second)
            c.final(f"The latest {vend} email ({G.dshort(ds[4])}) gives the {thing} {new}. The first result was cut off before it, so I re-ran it for just the newest message.")
            return {"template": T_TR.index(tpl), "slots": {"kind": "mail", "vendor": kw}}
        topic = r.choice(["invoice", "statement", "receipt", "contract"])
        y = r.choice([2023, 2024, 2025])
        paths = []
        folders = ["Finance", "Taxes", "Archive", "Home"]
        for yy in (2022, 2023, 2024, 2025):
            for mm in r.sample(range(1, 13), 6):
                paths.append(f"~/Documents/{r.choice(folders)}/{topic}_{yy}-{mm:02d}.pdf")
        paths = sorted(set(paths))
        news = sorted({f"~/Documents/Finance/{topic}_2026-{mm:02d}.pdf" for mm in r.sample(range(1, 10), 2)})
        allp = paths + news
        full = f"{len(allp)} matches:\n" + "\n".join(allp)
        first, kept = cut("find_files", full, r.randint(520, 640))
        tpl = ["where's my most recent {t} pdf", "find the newest {t} file in my documents", "which {t} file is the latest one"]
        tp = r.choice(tpl)
        c.user(G.messy(r, tp.format(t=topic)))
        c.discover(["files"], ["find_files"])
        c.call("find_files", {"query": topic, "kind": "pdf"}, first)
        c.call("find_files", {"query": f"{topic} 2026", "kind": "pdf"}, f"{len(news)} match{'es' if len(news) != 1 else ''}:\n" + "\n".join(news))
        c.final(f"The newest is {news[-1]}. The first search listed {len(allp)} matches but was cut off partway, so I narrowed it to 2026.")
        return {"template": 4 + tpl.index(tp), "slots": {"kind": "files", "topic": topic}}

    # honest partial: a single truncated list, answer must say so
    def trunc_msgs(r, now):
        names = G.pick_people(r, 6)
        lines = []
        for i in range(r.randint(14, 18)):
            n_ = r.choice(names)
            d = ago(now, r.choice([0, 1, 1, 2]))
            lines.append((n_, f"[{fsh(d, r.randint(7, 21), r.randint(0, 59))}] {n_} — {n_}: " + r.choice(
                ["are you around?", "call me when you can", "did you get my last one", "ok see you then", "lol that's wild", "sent the file", "running late", "thanks!!"])))
        return lines

    T_HON = [("msgs", "how many unread texts do I have"), ("msgs", "count my unread messages"),
             ("mail", "list everything unread in my inbox"), ("mail", "give me all my unread email senders"),
             ("cal", "walk me through everything on my calendar this month"), ("cal", "what's my whole month look like")]

    @G.family("errors", "mx_trunc_honest", weight=15, failure=(MX, "truncation_ignored", "claimed_complete_after_truncation"))
    def f_mx_trunc_honest(r, c, now):
        start(c)
        kind, tpl = r.choice(T_HON)
        c.user(G.messy(r, tpl))
        if kind == "msgs":
            rows = trunc_msgs(r, now)
            full = "\n".join(t for _, t in rows)
            res, kept = cut("view_messages", full, r.randint(560, 680))
            c.discover(["messages_contacts"], ["view_messages"])
            c.call("view_messages", {"unread": True, "count": 40}, res)
            k = len(kept.split("\n"))
            c.final(f"At least {k} unread texts - the result was cut off after {k}, so there may be more. "
                    f"I can narrow it by person or day if you want an exact count.")
        elif kind == "mail":
            total = r.randint(22, 30)
            ppl = [G.pick_people(r, 1)[0] for _ in range(total)]
            subs = [x[1] for x in G.EMAIL_SUBJ_BANK]
            ls = [f"{i}) {p} - {r.choice(subs)} (unread)" for i, p in enumerate(ppl, 1)]
            full = f"{total} unread email(s):\n" + "\n".join(ls)
            res, kept = cut("summarize_emails", full, r.randint(540, 660))
            c.discover(["mail"], ["summarize_emails"])
            c.call("summarize_emails", {"unread": True}, res)
            seen = [ln for ln in kept.split("\n")[1:]]
            senders = list(dict.fromkeys(G.first(ln.split(") ", 1)[1].split(" - ")[0]) for ln in seen))
            c.final(f"The header says {total} unread, but the list was cut off after {len(seen)}. Senders in the visible part: {', '.join(senders)}. "
                    f"Want me to narrow it to today or to one sender to see the rest?")
        else:
            evs_by_day = []
            total = 0
            d = now.date() + dt.timedelta(days=1)
            while len(evs_by_day) < 9 and d.month == now.month:
                if r.random() < 0.7:
                    evs = make_events(r, r.randint(2, 4), allday=False)
                    evs_by_day.append((d, evs))
                    total += len(evs)
                d += dt.timedelta(days=1)
            blocks = []
            for d_, evs in evs_by_day:
                blocks.append(day_heading(d_, now) + "\n" + "\n".join(agenda_lines(evs, [])))
            full = (f"Upcoming — this month ({total} item(s))\nCalendar events: {total}; Wisp/Apple reminders: 0. "
                    "[Calendar event] and [Reminder] are shown in separate sections.\n\n" + "\n\n".join(blocks))
            res, kept = cut("get_upcoming", full, r.randint(560, 680))
            c.discover(["calendar"], ["get_upcoming"])
            c.call("get_upcoming", {"period": "this month"}, res)
            heads = re.findall(r"^(?:Tomorrow|[A-Z][a-z]+day) · (.+)$", kept, re.M)
            shown = len(re.findall(r"^  - ", kept, re.M))
            c.final(f"The calendar reports {total} events, but the result was cut off: I only see {shown}, through {heads[-1]}. "
                    f"Later days aren't shown, so I can't say what's open after that - ask for a later week and I'll pull it.")
        return {"template": T_HON.index((kind, tpl)), "slots": {"kind": kind}}

    # ------------------------------------------------------------ partial multi-source: one source failed
    T_PART = ["{a}, and also {b}", "{a} - and {b}", "two things: {a}, and {b}", "{a}; also, {b}"]

    @G.family("errors", "mx_partial", weight=18, failure=(MX, "multi_source_partial", "silent_on_error", "invented_missing_source"))
    def f_mx_partial(r, c, now):
        start(c)
        if r.random() < 0.28:   # one tool, partial notice inside the result
            S = cal_day(r, now)
            S["result"] = ("Wisp could not check Reminders. Check its access in Settings; this schedule may be incomplete.\n" +
                           agenda_head(S["window"], S["evs"], []) + day_heading(S["day"].date, now) + "\n" + "\n".join(agenda_lines(S["evs"], [])))
            tp = r.choice(["what's on my calendar {d}", "give me my {d} schedule", "what do I have going on {d}"])
            c.user(G.messy(r, tp.format(d=S["day"].spoken)))
            c.discover(["calendar"], ["get_upcoming"])
            c.call("get_upcoming", S["args"], S["result"])
            ev = S["evs"]
            tim = S["timed"]
            c.final(f"{cnt(len(ev), 'calendar event')} {S['dw']}, first is {tim[0]['title']} at {tt(tim[0]['h'], tim[0]['m'])} and last is "
                    f"{tim[-1]['title']} at {tt(tim[-1]['h'], tim[-1]['m'])}. Wisp couldn't check Reminders, so reminders may be missing from that.")
            return {"template": 4 + ["what's on my calendar {d}", "give me my {d} schedule", "what do I have going on {d}"].index(tp), "slots": {"kind": "notice"}}
        wa, wb = r.sample(["cal", "notes", "msgs", "mail"], 2)
        A, B = src(r, now, wa), src(r, now, wb)
        join = r.choice(T_PART)
        prot = tuple(x for x in (A.get("f"), B.get("f")) if x)
        c.user(G.messy(r, join.format(a="tell me " + A["prompt"], b=B["prompt"]), protect=prot))
        c.discover(list(dict.fromkeys([A["fam"], B["fam"]])), [A["tool"], B["tool"]])
        bad_a = r.random() < 0.5
        ra, rb = (A["fail"], B["result"]) if bad_a else (A["result"], B["fail"])
        c.calls([(A["tool"], A["args"], ra), (B["tool"], B["args"], rb)])
        ok, bad = (B, A) if bad_a else (A, B)
        c.final(f"{ok['summary'][:1].upper() + ok['summary'][1:]}. I couldn't check {bad['label']} ({bad['reason']}), so I can't tell you {bad['cant']}.")
        return {"template": T_PART.index(join), "slots": {"a": wa, "b": wb, "failed": "a" if bad_a else "b"}}

    # ================================================================ DEV families (different scenarios, dev-only banks)
    MEET = [("Design sync", "design sync", "navigation redesign", "ship the new tab bar", "the icon refresh"),
            ("Vendor call", "vendor call", "shipping carrier", "switch to Parcelly", "the insurance add-on"),
            ("Parent committee", "parent committee", "bake sale", "hold it on the 18th", "the raffle"),
            ("Roof inspection debrief", "roof debrief", "repair scope", "patch the north side first", "full replacement")]
    TASKS = ["send the draft", "book the room", "email the quote", "order the signage", "collect RSVPs", "confirm the caterer"]
    T_MEET = [("decision", "what did we decide on {topic} in the {kw} notes"), ("decision", "pull the {topic} decision out of my {kw} notes"),
              ("mine", "what are my action items from the {kw} notes"), ("mine", "list what I'm on the hook for after the {kw}"),
              ("owner", "who's supposed to {task} per the {kw} notes"), ("owner", "per my {kw} notes, who owns the part where we {task}")]

    @G.family("reads", "mx_dev_notes_meet", split="dev", weight=6, failure=(MX, "stale_copy_used", "derived_answer_dumped_raw"))
    def f_mx_dev_notes_meet(r, c, now):
        start(c)
        kind, tpl = r.choice(T_MEET)
        title, kw, topic, choice, other = pick_one(r, MEET)
        people = G.pick_people(r, 3)
        fs = [G.first(p) for p in people]
        tasks = r.sample(TASKS, 4)
        owners = [fs[0], "me", fs[1], "me"]
        r.shuffle(owners)
        if "me" not in owners[:3]:
            owners[0] = "me"
        acts = [f"- @{o}: {t}" + (f" (due {r.choice(['Wed', 'Fri', 'Mon'])})" if r.random() < 0.6 else "") for o, t in zip(owners, tasks)]
        body = [f"Attendees: {', '.join(fs)}, me", "Agenda", f"- status", f"- {topic}", "Discussion",
                f"- {fs[0]} worried about cost; {fs[1]} prefers waiting", "- side note: parking validation??",
                "Decisions", f"- Going with: {choice}", f"- Postpone {other} to next quarter", "Action items"] + acts
        old = [f"Attendees: {', '.join(fs)}", "Decisions", f"- Going with: {other} (tentative)", "Action items", "- TBD"]
        text = (f"Title: {title} notes [Work]\nModified: {fdt(ago(now, 2), 16, 40)}\n\n" + "\n".join(body) + "\n\n---\n\n"
                f"Title: {title} notes (draft) [Work]\nModified: {fdt(ago(now, 6), 9, 15)}\n\n" + "\n".join(old))
        if kind == "owner":
            tgt = r.randrange(len(tasks))
            task = tasks[tgt]
        else:
            task = ""
        c.user(G.messy(r, tpl.format(topic=topic, kw=kw, task=task)))
        c.discover(["notes"], ["search_notes"])
        c.call("search_notes", {"query": kw}, text)
        if kind == "decision":
            c.final(f"For {topic}: {choice}; {other} is postponed to next quarter. (The draft note had an older, tentative call.)")
        elif kind == "mine":
            mine = [t for o, t in zip(owners, tasks) if o == "me"]
            c.final(f"You have {len(mine)}: " + "; ".join(mine) + ".")
        else:
            c.final(f"{owners[tgt] if owners[tgt] != 'me' else 'You'} - the notes assign “{task}” to {'you' if owners[tgt] == 'me' else owners[tgt]}.")
        return {"template": T_MEET.index((kind, tpl)), "slots": {"kind": kind, "kw": kw}}

    DEV_SUBJ = [("Re: potluck signup", "potluck"), ("Choir bus", "choir"), ("Tax docs", "tax"), ("Crew for Saturday", "crew"), ("Raffle prizes", "raffle")]
    DEV_BULK = [("Pantry Co", "Your order shipped"), ("Daily Digest", "Ten stories you missed"), ("Cloud Backup", "Storage almost full"),
                ("Pool & Spa Club", "Summer hours")]
    T_DCMP = ["who reached out the most yesterday, counting email and texts together", "across yesterday's email and messages, who contacted me the most",
              "combine yesterday's inbox and texts - which person got in touch most often"]

    @G.family("compound", "mx_dev_cmp_digest", split="dev", weight=6, failure=(MX, "derived_answer_dumped_raw", "multi_source_partial"))
    def f_mx_dev_cmp_digest(r, c, now):
        start(c)
        people = G.pick_people(r, 4)
        ec = [r.randint(1, 2), 1, 1, 0]
        mc = [r.randint(2, 3), 1, 0, 2]
        tot = [e + m for e, m in zip(ec, mc)]
        if sorted(tot)[-1] == sorted(tot)[-2]:
            raise AssertionError("tie")
        items = []
        for p, e in zip(people, ec):
            for k in range(e):
                sj = pick_one(r, DEV_SUBJ)[0]
                items.append(dict(sender=p, subject=("Re: " * k) + sj, bulk=False))
        for s, sj in r.sample(DEV_BULK, 3):
            items.append(dict(sender=s, subject=sj, bulk=True))
        times = sorted(r.sample([(h, m) for h in range(6, 23) for m in (6, 22, 41, 55)], len(items)), reverse=True)
        for it, (h, m) in zip(items, times):
            it["h"], it["m"], it["un"] = h, m, r.random() < 0.5
        y = ago(now, 1)
        el = [f"{len(items)} email(s) yesterday ({G.dshort(y)}):"] + [
            f"{i}) {tt(it['h'], it['m'])} · {it['sender']} — {it['subject']}" + (" (unread)" if it["un"] else "") for i, it in enumerate(items, 1)]
        ml = []
        mt = ["are you coming?", "call me", "ok", "thanks!", "can you check your email", "see you soon", "got it"]
        mrows = []
        for p, m in zip(people, mc):
            for _ in range(m):
                mrows.append((r.randint(7, 21), r.randint(0, 59), p, r.choice(mt)))
        mrows.sort()
        ml = [f"[{fsh(y, h, mi)}] {p} — {p}: {t}" for h, mi, p, t in mrows]
        tpl = r.choice(T_DCMP)
        c.user(G.messy(r, tpl))
        c.discover(["mail", "messages_contacts"], ["summarize_emails", "view_messages"])
        c.calls([("summarize_emails", {"day": "yesterday"}, "\n".join(el)),
                 ("view_messages", {"day": "yesterday", "count": 20}, "\n".join(ml))])
        top = people[tot.index(max(tot))]
        i = tot.index(max(tot))
        c.final(f"{top} - {tot[i]} contacts ({cnt(ec[i], 'email')} and {cnt(mc[i], 'text')}).")
        return {"template": T_DCMP.index(tpl), "slots": {"top": top}}

    T_DPART = ["{a}, and also {b}", "{a}, plus {b}", "I want two answers: {a}, then {b}"]

    @G.family("errors", "mx_dev_partial", split="dev", weight=6, failure=(MX, "multi_source_partial", "silent_on_error", "invented_missing_source"))
    def f_mx_dev_partial(r, c, now):
        start(c)
        wa, wb = r.sample(["cal", "notes", "msgs", "mail"], 2)
        A, B = src(r, now, wa, dev=True), src(r, now, wb, dev=True)
        join = r.choice(T_DPART)
        prot = tuple(x for x in (A.get("f"), B.get("f")) if x)
        c.user(G.messy(r, join.format(a="tell me " + A["prompt"], b=B["prompt"]), protect=prot))
        c.discover(list(dict.fromkeys([A["fam"], B["fam"]])), [A["tool"], B["tool"]])
        bad_a = r.random() < 0.5
        bad = A if bad_a else B
        ok = B if bad_a else A
        timeout = f"({bad['tool']} timed out after 20s; nothing was returned)"
        fail = timeout if r.random() < 0.5 else bad["fail"]
        calls = [(A["tool"], A["args"], fail if bad_a else A["result"]), (B["tool"], B["args"], B["result"] if bad_a else fail)]
        c.calls(calls)
        c.final(f"{ok['summary'][:1].upper() + ok['summary'][1:]}. The other lookup ({bad['label']}) failed, so I can't say {bad['cant']}.")
        return {"template": T_DPART.index(join), "slots": {"a": wa, "b": wb}}
