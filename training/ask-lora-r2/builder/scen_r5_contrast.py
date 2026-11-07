"""Round-5 family ``write_contrast_r5``: the same request with and without the go-ahead to change something.

Why. r4 made more unsafe writes than base (23 trials against 3 in its gate). Tool rows mostly show a write being right, and
``restraint_r4`` covers files, outbound web requests and weather, but messages, email, reminders and the calendar had few close
pairs. So each topic here sits on every side of the line:

- ``write``     a plain instruction with everything it needs. One write. The host's card applies where it does, and it's approved.
- ``draft``     "draft it, don't send". ``draft_email``, no card, and the answer calls it a draft.
- ``word``      "how should I word it, I'll send it myself". No tool: the wording is the answer.
- ``read``      a question about the same topic. A read, no write, and the answer states what it found.
- ``clarify``   an instruction missing who or when. No tool: one short question.
- ``remark``    the topic comes up as a passing thought. No tool and no action.

This doesn't replace the executor's rules. A card still decides whether a send runs, and rows follow that policy rather than
teaching around it. There's no row for a declined card: the host writes "I didn't send it" itself, so no model answer exists to train. Every final is ``BASE_WRITES``, so the voice stays base's; ``sc.base_ref`` is the contract in
``gpu/source_contract.py``. All wording and tool results are fresh and synthetic, and none comes from an evaluation prompt, answer
or failure transcript. ``seal_r5.py`` checks the finished build against every test set.
"""
from __future__ import annotations

import datetime as dt

import scen  # noqa: F401  (first: an import cycle otherwise)
import scen_r3_compose as C
from harness import Call, Final
from scen import Scenario
from scen_r2_act import _check as _dry_run
from scen_r5_debuglog import BASE_WRITES
from world import Email, Event, Msg, Reminder

FAMILY = "write_contrast_r5"
WRITES = frozenset({"send_message", "send_email", "draft_email", "add_reminder", "add_calendar_event"})
WRITE_ALLOW_R5 = {FAMILY: WRITES}
D = dt.timedelta
DROPPED: list = []

# who, topic, what to say, what they said (a fact the read must state)
TEXTS = [
    ("maya", "the study group", "the study group moved to the library", "study group is in room 214 now", "214"),
    ("theo", "the ride to the airport", "i can pick you up at 6 instead of 7", "flight got pushed, it's gate C9 now", "C9"),
    ("carmen", "the potluck", "i'm bringing the lentil salad to the potluck", "the potluck starts at 5:30", "5:30"),
    ("nadia", "the climbing gym", "the climbing gym is closed on sunday", "my gym pass code is 8841", "8841"),
    ("priya", "the group project", "my half of the group project is in the shared folder", "our slot got moved to Thursday", "Thursday"),
    ("dad", "the car", "the car needs an oil change before the trip", "the shop quoted me $84", "$84"),
    ("mom", "the weekend visit", "we're getting in around noon on saturday", "aunt Lena is coming too", "Lena"),
    ("maya", "the concert tickets", "i got the concert tickets, row F", "doors open at 7:15", "7:15"),
]
# who, topic, subject, body, what their email said, the fact
MAILS = [
    ("okafor", "the lab report", "Lab report", "My lab report will be in by Friday noon.", "Your lab section moved to Sherman 118.", "Sherman 118"),
    ("nadia", "the sublet", "Sublet", "I can take over the sublet from June first.", "Rent for the sublet is $640 a month.", "$640"),
    ("theo", "the hackathon", "Hackathon", "I'm in for the hackathon if you still need a fourth.", "Team check-in closes at 9:30 AM.", "9:30"),
    ("carmen", "the wedding gift", "Wedding gift", "I'll chip in for the wedding gift, just send me the total.", "The registry code is WREN42.", "WREN42"),
    ("priya", "the internship form", "Internship form", "I signed the internship form and put it in your box.", "The form goes to Dean Alvarez.", "Alvarez"),
    ("dad", "the tax papers", "Tax papers", "I found the tax papers, they were in the blue folder.", "Your refund came to $212.", "$212"),
]
# reminder title, the passing thought, the question
REMINDERS = [
    ("pick up the dry cleaning", "dry cleaning", "i always forget the dry cleaning until i have nothing to wear"),
    ("renew the parking permit", "parking permit", "parking permits at this school are such a racket"),
    ("call the dentist", "dentist", "i've been putting off the dentist for like a year"),
    ("water the balcony plants", "plants", "my balcony plants are barely hanging on"),
    ("return the library books", "library books", "those library books have been on my floor for weeks"),
    ("order printer ink", "printer ink", "printer ink costs more than the printer, it's absurd"),
]
# event title, topic, the passing thought
EVENTS = [
    ("Coffee with Theo", "coffee with theo", "i should really grab coffee with theo sometime"),
    ("Haircut", "haircut", "my hair has gotten out of control, i need a haircut at some point"),
    ("Advising meeting", "advising meeting", "advising meetings always make me anxious"),
    ("Bike tune-up", "bike tune-up", "my bike has been making a weird clicking sound for a while"),
    ("Dinner with Carmen", "dinner with carmen", "carmen and i keep saying we'll get dinner and never do"),
    ("Gym orientation", "gym orientation", "thinking about finally joining the rec center gym"),
]

ASK_TEXT = ["text {name} that {say}", "shoot {name} a text: {say}", "send {name} a message saying {say}", "tell {name} by text that {say}"]
ASK_WORD = ["how should i tell {name} that {say}? i'll text it myself", "help me word a text to {name} saying {say}, i'll send it",
            "what's a nice way to let {name} know {say}, don't send anything", "give me a casual line for telling {name} that {say}"]
ASK_READ = ["did {name} text me anything about {topic}", "what did {name} say about {topic} in messages", "check my texts, did {name} mention {topic}",
            "any texts from {name} on {topic}"]
ASK_CLARIFY = ["text them that {say}", "send that to her: {say}", "shoot him a message that {say}", "let them know {say}"]
ASK_REMARK = ["i keep meaning to tell {name} that {say}", "i should probably mention to {name} that {say} at some point",
              "not sure when i'll get around to telling {name} that {say}", "note to self i guess, {name} still doesn't know {say}"]
MAIL_WRITE = ["email {name} about {topic}: {body}", "send {name} an email saying: {body}", "write to {name} over email and say {body}"]
MAIL_DRAFT = ["draft an email to {name} about {topic}, don't send it yet: {body}", "put together a draft for {name} on {topic}, i want to read it first: {body}",
              "write up an email to {name} but leave it as a draft: {body}"]
MAIL_READ = ["what did {name}'s email say about {topic}", "look in my mail for what {name} wrote on {topic}", "did {name} email me about {topic}"]
MAIL_CLARIFY = ["email them about {topic}", "send the email about {topic}", "fire off that email on {topic}"]
MAIL_REMARK = ["i owe {name} an email about {topic}, ugh", "i've been dreading the email to {name} about {topic}", "{name} is probably waiting to hear from me about {topic}"]
REM_WRITE = ["remind me to {title} tomorrow at 5pm", "set a reminder to {title} tomorrow at 5pm", "put a reminder in for tomorrow at 5pm: {title}"]
REM_READ = ["do i have a reminder about the {topic}", "is the {topic} on my reminders", "what reminders do i have for the {topic}"]
REM_CLARIFY = ["set one of those reminders for me", "set a reminder for that", "remind me later about it"]
CAL_WRITE = ["put {title} on my calendar tomorrow at 3pm", "add {title} tomorrow at 3pm", "schedule {title} for tomorrow at 3pm"]
CAL_READ = ["when is the {topic} on my calendar", "is {topic} on my calendar", "what day did i put {topic} on"]
CAL_CLARIFY = ["put {title} on my calendar", "add {title} to my calendar", "schedule {title} for me"]


def _scenario(mode, topic, prompt, now, w, steps, gold, cards=(), approve=None, must_see=()):
    sc = Scenario(FAMILY, prompt, now, w, steps, f"write contrast ({mode}): {topic}")
    sc.expect_cards = list(cards)
    sc.approve = list(approve) if approve is not None else None
    sc.must_see = list(must_see)
    sc.base_ref = dict(kind="write_contrast", mode=mode, scripted=None, topic=topic, facts=list(gold))
    sc.mode, sc.qkind = mode, "write_contrast"
    return sc


def _name(key):
    return C.PEOPLE[key][4]


def _pick(options, i):
    return options[i % len(options)]


def text_rows(rng, i, row):
    who, topic, say, said, fact = row
    name, to = _name(who), C.to_name(who)
    ctx = dict(name=name.lower() if who in ("mom", "dad") else name, say=say, topic=topic)
    out = []
    now, w = C.world(rng)
    out.append(_scenario("write", topic, _pick(ASK_TEXT, i).format(**ctx), now, w, [[Call("send_message", to=to, text=say)], Final(BASE_WRITES)], [name],
                         cards=["send_message"], approve=[True]))
    now, w = C.world(rng)
    out.append(_scenario("word", topic, _pick(ASK_WORD, i).format(**ctx), now, w, [Final(BASE_WRITES)], []))
    now, w = C.world(rng)
    sender = C.PEOPLE[who][1]
    w.messages.append(Msg(sender, sender, said, now - D(hours=rng.randint(4, 40)), unread=False))
    out.append(_scenario("read", topic, _pick(ASK_READ, i).format(**ctx), now, w, [[Call("view_messages", query=name.split()[0])], Final(BASE_WRITES)], [fact], must_see=[fact]))
    now, w = C.world(rng)
    out.append(_scenario("clarify", topic, _pick(ASK_CLARIFY, i).format(**ctx), now, w, [Final(BASE_WRITES)], []))
    now, w = C.world(rng)
    out.append(_scenario("remark", topic, _pick(ASK_REMARK, i).format(**ctx), now, w, [Final(BASE_WRITES)], []))
    return out


def mail_rows(rng, i, row):
    who, topic, subject, body, said, fact = row
    name, to = _name(who), C.to_name(who)
    ctx = dict(name=name, topic=topic, body=body)
    out = []
    now, w = C.world(rng)
    out.append(_scenario("write", topic, _pick(MAIL_WRITE, i).format(**ctx), now, w, [[Call("send_email", to=to, subject=subject, body=body)], Final(BASE_WRITES)], [name],
                         cards=["send_email"], approve=[True]))
    now, w = C.world(rng)
    out.append(_scenario("draft", topic, _pick(MAIL_DRAFT, i).format(**ctx), now, w, [[Call("draft_email", to=to, subject=subject, body=body)], Final(BASE_WRITES)], []))
    now, w = C.world(rng)
    person = C.PEOPLE[who]
    w.emails.append(Email(person[1], person[3] or "x@example.com", subject, now - D(hours=rng.randint(5, 40)), said, True, True))
    out.append(_scenario("read", topic, _pick(MAIL_READ, i).format(**ctx), now, w, [[Call("view_emails", query=topic.replace("the ", ""))], Final(BASE_WRITES)], [fact], must_see=[fact]))
    now, w = C.world(rng)
    out.append(_scenario("clarify", topic, _pick(MAIL_CLARIFY, i).format(**ctx), now, w, [Final(BASE_WRITES)], []))
    now, w = C.world(rng)
    out.append(_scenario("remark", topic, _pick(MAIL_REMARK, i).format(**ctx), now, w, [Final(BASE_WRITES)], []))
    return out


def _tomorrow(now, hour):
    return (now + D(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)


def reminder_rows(rng, i, row):
    title, topic, remark = row
    out = []
    now, w = C.world(rng)
    out.append(_scenario("write", topic, _pick(REM_WRITE, i).format(title=title), now, w,
                         [[Call("add_reminder", title=title, when_iso=_tomorrow(now, 17).strftime("%Y-%m-%dT%H:%M"))], Final(BASE_WRITES)], [topic.split()[-1]]))
    now, w = C.world(rng)
    due = _tomorrow(now, 18) + D(days=2)
    w.reminders.append(Reminder(title, due))
    out.append(_scenario("read", topic, _pick(REM_READ, i).format(topic=topic), now, w, [[Call("search_reminders", query=topic.split()[-1])], Final(BASE_WRITES)], [topic.split()[-1]]))
    now, w = C.world(rng)
    out.append(_scenario("clarify", topic, _pick(REM_CLARIFY, i), now, w, [Final(BASE_WRITES)], []))
    now, w = C.world(rng)
    out.append(_scenario("remark", topic, remark, now, w, [Final(BASE_WRITES)], []))
    return out


def calendar_rows(rng, i, row):
    title, topic, remark = row
    out = []
    now, w = C.world(rng)
    out.append(_scenario("write", topic, _pick(CAL_WRITE, i).format(title=title.lower()), now, w,
                         [[Call("add_calendar_event", title=title, when_iso=_tomorrow(now, 15).strftime("%Y-%m-%dT%H:%M"))], Final(BASE_WRITES)], [title.split()[0]],
                         cards=["add_calendar_event"], approve=[True]))
    now, w = C.world(rng)
    w.events.append(Event(title, _tomorrow(now, 11) + D(days=2), 45, ""))
    out.append(_scenario("read", topic, _pick(CAL_READ, i).format(topic=topic), now, w, [[Call("get_upcoming", query=topic.split()[-1], days=14, calendar_only=True)], Final(BASE_WRITES)], [title.split()[-1]]))
    now, w = C.world(rng)
    out.append(_scenario("clarify", topic, _pick(CAL_CLARIFY, i).format(title=title.lower()), now, w, [Final(BASE_WRITES)], []))
    now, w = C.world(rng)
    out.append(_scenario("remark", topic, remark, now, w, [Final(BASE_WRITES)], []))
    return out


def fam_write_contrast_r5(rng):
    DROPPED.clear()
    made = []
    for i, r in enumerate(TEXTS):
        made += text_rows(rng, i, r)
    for i, r in enumerate(MAILS):
        made += mail_rows(rng, i, r)
    for i, r in enumerate(REMINDERS):
        made += reminder_rows(rng, i, r)
    for i, r in enumerate(EVENTS):
        made += calendar_rows(rng, i, r)
    kept = []
    for sc in made:
        ok, why = _dry_run(sc)
        if ok:
            kept.append(sc)
        else:
            DROPPED.append((sc.mode, sc.prompt, why))
    return kept


FAMILIES_R5 = {FAMILY: fam_write_contrast_r5}
