"""V4 categories: read the user's own day before answering open-ended personal questions, and warm small talk.

The 2026-10-24 voice work showed Ling V3 answering "what should I do today" with generic ideas (drawing, a walk, a
podcast) and never looking at the calendar. These families teach the opposite: an open-ended question about the
user's own life is answered from a fresh `get_upcoming` read, naming the real items and times before any suggestion.
`chat_warm` covers greetings, thanks and good news, which need no tool at all.

Built with V3.2's generate.Builder, like ext_v33_corpus.py, so the protocol (stubs or get_tool_schemas, receipts,
loading budget, offer order) is identical to every other row. Tool results use the live `get_upcoming` formats from
service/tools/assistant_tools.py (`_format_forward_agenda`, the empty-window line); check_formats.py compares them with
the real formatter. All people, events and places are synthetic.

The final answers written here are plain factual skeletons. build_v4.py replaces every final answer in V4 (these and
V3.3's) with the reviewed energetic rewrite in voice_rewrites.jsonl, so the voice lives in one place.

| family | prompts | gold behaviour |
|---|---|---|
| day_plan | "what should I do today", "where do I start today" | get_upcoming(period='today'), then the real items, the open stretches, a suggestion |
| evening_free | "am I free tonight?" | get_upcoming(period='today'), then whether anything is on from 5 PM |
| week_shape | "how's my week looking?" | get_upcoming(days=7), then the shape of the week and its biggest item |
| tomorrow_prep | "anything to get ready for tomorrow?" | get_upcoming(period='tomorrow'), then what needs prep |
| forgetting | "am I forgetting anything?" | get_upcoming(days=3), then the reminders and events in the next few days |
| chat_warm | greetings, thanks, good news | no tool; a warm reply |
"""
import datetime as dt
import random

CATEGORY = "v4_personal_context"
SEED = 20261024
N_TRAIN, N_DEV = 150, 20

EVENTS = [("Design review", "Room 4B"), ("Lunch with Priya", "Blue Door Cafe"), ("Study group", "Library 2nd floor"),
          ("Team standup", None), ("Dentist", "Bright Smile Dental"), ("Advising appointment", "Hall C 210"),
          ("Soccer practice", "North Field"), ("Piano lesson", None), ("Budget sync", None), ("Coffee with Marcus", "Ritual"),
          ("Physics midterm", "Hall B"), ("Volunteer shift", "Food bank"), ("Yoga class", "Studio 3"),
          ("Project check-in", None), ("Book club", "Maple Street Books"), ("Haircut", None), ("Lab section", "Sci 104"),
          ("Dinner with Hana", "Osteria Lume"), ("Career fair", "Student Center"), ("1:1 with Dana", None)]
REMINDERS = ["Pick up dry cleaning", "Pay the water bill", "Call the landlord", "Submit the lab form", "Renew parking permit",
             "Water the plants", "Return library books", "Book flights home", "Send Jonah the slides", "Buy a birthday card"]
EVENING_ONLY = [("Dinner with Hana", "Osteria Lume"), ("Book club", "Maple Street Books"), ("Concert with Leo", "The Fillmore"),
                ("Movie night", None), ("Pickup basketball", "Rec center")]

PROMPTS = {
    "day_plan": (["what should I do today", "what should i do today?", "where should I start today", "help me plan my day",
                  "what should I focus on today", "how should I spend today", "what's the plan for today?",
                  "give me a game plan for today", "what do I tackle first today"],
                 ["map out my day for me", "what's worth doing today?"]),
    "evening_free": (["am I free tonight?", "anything on this evening?", "can I squeeze in a movie tonight?",
                      "do I have plans tonight", "is my evening open?"],
                     ["any chance my night is clear?"]),
    "week_shape": (["how's my week looking?", "what's my week like", "anything big this week?", "give me the rundown of my week",
                    "how busy am I this week"],
                   ["what does the week ahead hold?"]),
    "tomorrow_prep": (["anything I should prep for tomorrow?", "what do I need to get ready for tomorrow",
                       "should I be preparing for anything tomorrow?", "what's tomorrow looking like, anything to prep?"],
                      ["help me get set for tomorrow"]),
    "forgetting": (["am I forgetting anything?", "anything slipping through the cracks?", "what am I missing this week?",
                    "is there anything I'm overlooking in the next few days"],
                   ["did I drop any balls lately?"]),
}
CHAT = (["hey wisp", "hi!", "good morning!", "hey there", "yo wisp", "thanks so much!", "you're the best", "thank you!!",
         "how are you doing?", "how's it going wisp", "I aced my exam!", "I finally finished my project!", "I got the job!!",
         "good night wisp", "that was super helpful"],
        ["heyyy", "wisp you rock", "just passed my driving test!"])
CHAT_REPLIES = {
    "greet": "Hi! What can I help you with today?",
    "thanks": "You're welcome. Anything else I can help with?",
    "how": "I'm doing well, thanks for asking. What can I help you with?",
    "news": "Congratulations, that's great news! Want to tell me about it?",
    "night": "Good night! Sleep well.",
}


def _rng(*parts):
    return random.Random("|".join(str(p) for p in (SEED,) + parts))


def _now(r, hour_range=(7, 11)):
    d = dt.date(2026, 10, 19) + dt.timedelta(days=r.randint(0, 34))
    t = dt.datetime(d.year, d.month, d.day, r.randint(*hour_range), r.choice([4, 12, 26, 38, 47, 55]))
    sentence = (f"The current date and time is {t.strftime('%A, %B')} {t.day}, {t.year} at {(t.hour - 1) % 12 + 1}:{t.minute:02d} "
                f"{'AM' if t.hour < 12 else 'PM'}. Resolve relative dates/times against this.")
    return t, sentence


def _tz(d):
    # US Pacific: daylight time ends on the first Sunday of November (2026-11-01).
    return "PDT-0700" if d < dt.date(2026, 11, 1) else "PST-0800"


def _clock(h, m):
    return f"{(h - 1) % 12 + 1}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _day_label(d, today):
    if d == today:
        return f"Today · {d.strftime('%a %b')} {d.day}"
    if d == today + dt.timedelta(days=1):
        return f"Tomorrow · {d.strftime('%a %b')} {d.day}"
    return f"{d.strftime('%A · %b')} {d.day}"


def agenda(t, window_label, items, *, schedule):
    """get_upcoming's live format. items: [(date, hour, minute, title, place, kind)] with kind 'calendar'|'reminders'."""
    if not items:
        return f"Today is {t.strftime('%A, %B')} {t.day}, {t.year}. Nothing scheduled in {window_label}."
    cal = sum(i[5] == "calendar" for i in items)
    rem = len(items) - cal
    blocks = []
    for d in sorted({i[0] for i in items}):
        lines = [_day_label(d, t.date())]
        for kind, head in (("calendar", "  Calendar events"), ("reminders", "  Reminders")):
            day_items = sorted((i for i in items if i[0] == d and i[5] == kind), key=lambda i: (i[1], i[2]))
            if day_items:
                lines.append(head)
                lines.extend(f"  - {_clock(h, m)} {_tz(d)} — {title}{f' @ {place}' if place else ''}"
                             for _, h, m, title, place, _ in day_items)
        blocks.append("\n".join(lines))
    native = " Apple Reminders deletion status is not independently verified." if rem else ""
    return (f"{'Schedule' if schedule else 'Upcoming'} — {window_label} ({len(items)} item(s))\n"
            f"Calendar events: {cal}; Wisp/Apple reminders: {rem}. [Calendar event] and [Reminder] are shown in separate sections."
            + native + "\n\n" + "\n\n".join(blocks))


def _say(h, m):
    return _clock(h, m)


def _items_today(r, t, n, after_now=True, evening=False):
    out, used = [], set()
    pool = EVENING_ONLY if evening else EVENTS
    for title, place in r.sample(pool, n):
        for _ in range(20):
            h = r.randint(17, 21) if evening else r.randint(max(t.hour + 1, 8), 19)
            m = r.choice([0, 15, 30, 45])
            if (h, m) not in used and (not after_now or (h, m) > (t.hour, t.minute)):
                used.add((h, m))
                out.append((t.date(), h, m, title, place, "calendar"))
                break
    return out


def _reminder(r, t, d=None, h=None):
    d = d or t.date()
    h = h if h is not None else r.randint(max(t.hour + 1, 9), 20)
    return (d, h, 0, r.choice(REMINDERS), None, "reminders")


def _load(G, b, r, tools):
    need = [x for x in tools if x not in b.loaded]
    if not need:
        return
    if r.random() < 0.7:
        b.stub([(x, {}) for x in need])
    else:
        b.explicit(list(dict.fromkeys(G.FAMILY[x] for x in need)), need)


def _list_items(items):
    return "; ".join(f"{title} at {_say(h, m)}" + (f" ({place})" if place else "") for _, h, m, title, place, _ in
                     sorted(items, key=lambda i: (i[0], i[1], i[2])))


# ------------------------------------------------------------------ families: fn(G, r, b, t, dev) -> (prefix, text, meta, hint)

def f_day_plan(G, r, b, t, dev):
    text = r.choice(PROMPTS["day_plan"][dev])
    shape = r.choice(["light", "light", "busy", "empty"])
    n = {"light": r.randint(1, 2), "busy": r.randint(3, 4), "empty": 0}[shape]
    items = _items_today(r, t, n)
    if shape != "empty" and r.random() < 0.5:
        items.append(_reminder(r, t))
    _load(G, b, r, ["get_upcoming"])
    b.call([("get_upcoming", {"period": "today"}, agenda(t, "today", items, schedule=True))])
    if not items:
        b.final("Nothing is on your calendar or reminders today, so the whole day is open. Suggest how to use it.")
    else:
        first = min(items, key=lambda i: (i[1], i[2]))
        b.final(f"Today you have: {_list_items(items)}. Nothing is scheduled before {_say(first[1], first[2])}. "
                f"Suggest how to use the open time around them.")
    return [], text, {"shape": shape, "n_items": len(items)}, "An open-ended question about the user's own day: read today first."


def f_evening_free(G, r, b, t, dev):
    text = r.choice(PROMPTS["evening_free"][dev])
    busy = r.random() < 0.5
    items = _items_today(r, t, r.randint(0, 2))
    items = [i for i in items if i[1] < 17]
    if busy:
        items += _items_today(r, t, 1, evening=True)
    _load(G, b, r, ["get_upcoming"])
    b.call([("get_upcoming", {"period": "today"}, agenda(t, "today", items, schedule=True))])
    evening = [i for i in items if i[1] >= 17]
    if evening:
        b.final(f"Not completely: tonight you have {_list_items(evening)}.")
    elif items:
        b.final(f"Yes, your evening is free. Today's only plans are earlier: {_list_items(items)}.")
    else:
        b.final("Yes, nothing is on your calendar tonight, or all day.")
    return [], text, {"busy": busy}, "Whether they're free tonight is a question about their own calendar."


def f_week_shape(G, r, b, t, dev):
    text = r.choice(PROMPTS["week_shape"][dev])
    items = []
    for k in r.sample(range(0, 7), r.randint(2, 5)):
        d = t.date() + dt.timedelta(days=k)
        title, place = r.choice(EVENTS)
        h = r.randint(max(t.hour + 1, 8), 19) if k == 0 else r.randint(8, 19)
        items.append((d, h, r.choice([0, 30]), title, place, "calendar"))
    if r.random() < 0.4:
        items.append(_reminder(r, t, t.date() + dt.timedelta(days=r.randint(1, 6)), 9))
    items = list({(i[0], i[3]): i for i in items}.values())
    _load(G, b, r, ["get_upcoming"])
    b.call([("get_upcoming", {"days": 7}, agenda(t, "next 7 day(s)", items, schedule=False))])
    lines = "; ".join(f"{i[3]} {i[0].strftime('%A')} at {_say(i[1], i[2])}" for i in sorted(items, key=lambda i: (i[0], i[1], i[2])))
    b.final(f"This week: {lines}.")
    return [], text, {"n_items": len(items)}, "The shape of their week comes from their calendar for the next 7 days."


def f_tomorrow_prep(G, r, b, t, dev):
    text = r.choice(PROMPTS["tomorrow_prep"][dev])
    d = t.date() + dt.timedelta(days=1)
    items = []
    for title, place in r.sample(EVENTS, r.randint(0, 3)):
        items.append((d, r.randint(8, 19), r.choice([0, 30]), title, place, "calendar"))
    items = list({i[3]: i for i in items}.values())
    _load(G, b, r, ["get_upcoming"])
    b.call([("get_upcoming", {"period": "tomorrow"}, agenda(t, "tomorrow", items, schedule=False))])
    if items:
        b.final(f"Tomorrow you have: {_list_items(items)}.")
    else:
        b.final("Nothing is scheduled tomorrow, so there's nothing to prep for.")
    return [], text, {"n_items": len(items)}, "Prep for tomorrow starts from what is actually on tomorrow."


def f_forgetting(G, r, b, t, dev):
    text = r.choice(PROMPTS["forgetting"][dev])
    items = []
    for k in range(r.randint(1, 3)):
        items.append(_reminder(r, t, t.date() + dt.timedelta(days=r.randint(0, 2)), r.randint(max(t.hour + 1, 9), 20)))
    if r.random() < 0.6:
        d = t.date() + dt.timedelta(days=r.randint(1, 2))
        title, place = r.choice(EVENTS)
        items.append((d, r.randint(9, 18), 0, title, place, "calendar"))
    items = list({i[3]: i for i in items}.values())
    _load(G, b, r, ["get_upcoming"])
    b.call([("get_upcoming", {"days": 3}, agenda(t, "next 3 day(s)", items, schedule=False))])
    lines = "; ".join(f"{i[3]} ({'reminder' if i[5] == 'reminders' else 'event'}) {i[0].strftime('%A')} at {_say(i[1], i[2])}"
                      for i in sorted(items, key=lambda i: (i[0], i[1], i[2])))
    b.final(f"In the next three days: {lines}.")
    return [], text, {"n_items": len(items)}, "What they might be forgetting is in their reminders and calendar."


def f_chat_warm(G, r, b, t, dev):
    text = r.choice(CHAT[dev])
    low = text.lower()
    kind = ("thanks" if any(w in low for w in ("thank", "helpful", "best", "rock")) else
            "how" if "how" in low else
            "night" if "night" in low else
            "news" if "!" in text and any(w in low for w in ("aced", "finished", "got", "passed")) else "greet")
    b.final(CHAT_REPLIES[kind])
    return [], text, {"kind": kind}, None


FAMILIES = {   # fid: (fn, weight, variant)
    "day_plan": (f_day_plan, 8, "personal_no_web"), "evening_free": (f_evening_free, 4, "personal_no_web"),
    "week_shape": (f_week_shape, 5, "personal_no_web"), "tomorrow_prep": (f_tomorrow_prep, 4, "personal_no_web"),
    "forgetting": (f_forgetting, 3, "personal_no_web"), "chat_warm": (f_chat_warm, 6, "web"),
}


def build(G, held, limit=0.4):
    """-> list of dicts: split, fid, j, prefix, user_text, now, variant, builder, meta, hint, overlap."""
    weights = {f: w for f, (_, w, _) in FAMILIES.items()}
    out = []
    for split, total in (("train", N_TRAIN), ("dev", N_DEV)):
        s = sum(weights.values())
        alloc = {f: max(1, round(total * w / s)) for f, w in weights.items()}
        for fid in sorted(alloc):
            fn, _, variant = FAMILIES[fid]
            taken = set()
            for j in range(alloc[fid]):
                for attempt in range(80):
                    r = _rng(split, fid, j, attempt)
                    t, now = _now(r)
                    b = G.Builder(r, variant)
                    try:
                        prefix, text, meta, hint = fn(G, r, b, t, split == "dev")
                    except AssertionError:
                        continue
                    key = (text, t.date(), fid)
                    ov = G.heldout_overlap(held, text)
                    if ov >= limit or (key in taken and fid != "chat_warm"):
                        continue
                    taken.add(key)
                    out.append({"split": split, "fid": fid, "j": j, "prefix": prefix, "user_text": text, "now": now,
                                "variant": variant, "builder": b, "meta": meta, "hint": hint, "overlap": ov, "attempt": attempt})
                    break
    return out
