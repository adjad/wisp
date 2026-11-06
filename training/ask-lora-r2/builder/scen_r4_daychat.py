"""Round-4 family ``day_chat_r4``: a conversation about the user's days, over a world whose Brief holds more than the calendar.

Why. In the user's live test of r3 (2026-11-02) the model answered "what is my plan for saturday" from the calendar only and
missed a family lunch that lived in a text; "and friday?" made no call at all and missed a homework due at 11:59 PM that lived
in an email; "i mean this week" read the calendar only; and "draft a message responding to <person>" did not read the thread,
although an earlier turn had listed the person. The user's rule: every source counts, and the Brief is a source in itself.

Each scenario seeds 0-3 earlier exchanges the way production stores them (the user's text and the final assistant text only,
``scen_r3_followup.turn``) over a world that holds dated Brief items read from texts, mail and notes, people waiting on a reply,
calendar events and reminders. The target turn is one of:

- a day or range question ("how's saturday shaping up", "anything due this week"): ONE get_my_day_plan call for that day or
  range, and a final that names the calendar items AND the Brief items (text / email / note) the plan listed;
- a follow-up that changes the day ("and friday?", "what about sunday", "i mean this week"): a FRESH get_my_day_plan call for the
  new day or range, never an answer from the earlier turn;
- a reply to a person the plan listed as waiting ("draft something back to maya", "write a reply to theo"): view_messages for that
  person's thread first, then draft_message (the host ends the turn on the editable card, so there is no final); "reply to X saying
  ..." reads the thread, then send_message behind its card, with a short receipt;
- a recall of what was just said ("wait what time was the sink repair again"): answered from the conversation, no tool, because the
  earlier final stated it;
- positive controls (about 30%): an explicit calendar ask ("which events do i have friday") reads get_upcoming and the final also
  names the dated Brief items it lists apart; a past day reads get_past_events.

The final of every plan answer is written from the plan the world printed (``scen_r4_day.Plan``): no item is named that the
plan does not list, a count comes from the printed lines, and a truncated list says "and N more". Round 5 (ASK_LORA_R5=1)
lists every item the tool returned and, where the host cut a list, says so plainly with how to see the rest, never "and N more"
(``scen_r4_day.cut_note``).
"""
from __future__ import annotations

import datetime as dt
import random
import re

import harness as H
from harness import Call, Final
from replies import join
from scen import Scenario
import scen_r3_followup as F
import scen_r4_day as S
from scen_r3_followup import turn
from scen_r2_args import WDN
from world import Msg

D = dt.timedelta
FAMILY = "day_chat_r4"
WRITE_ALLOW_R4 = {FAMILY: frozenset({"draft_message", "send_message"})}

DROPPED: list = []
ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d")

# ------------------------------------------------------------------ wording
WD_TYPO = {"monday": ["mondya", "mon"], "tuesday": ["tuseday", "tues"], "wednesday": ["wensday", "wed"],
           "thursday": ["thrusday", "thurs"], "friday": ["fridya", "fri"], "saturday": ["satruday", "saterday", "sat"],
           "sunday": ["sunady", "sun"]}
# Forms the host reads as a day-plan question (model_only.day_plan_question); the builder filters every prompt through it.
STEMS = [
    "what's planned for {x}", "what's going on {x}", "what's happening {x}", "anything i need to handle {x}",
    "anything coming up {x}", "what's on {x}", "my plans for {x}?", "anything due {x}", "what's up for {x}",
    "what is planned {x}", "anything i should know {x}", "what's on for {x}", "anything i have to do {x}",
    "whats going on {x}", "what is happening {x}", "what's coming up {x}", "what do i have planned {x}",
    "what do i have coming up {x}", "what am i missing {x}", "anything i need to get done {x}", "what is coming up {x}",
    "whats happening {x}", "anything planned {x}", "what's on my agenda {x}",
    "what is my plan for {x}", "what is my plan for {x}", "whats my plan for {x}", "tell me my plan for {x}",
    "what is my plan on {x}", "what do i need to do {x}", "what is my plan for {x}",
]
PRE = ["", "", "", "", "hey ", "ok so ", "quick one: ", "um ", "hi, ", "so "]
SUF = ["", "", "", "", "?", " pls", " thanks", " for me", " please"]
# Follow-up stems. Each one holds a phrase the host reads as a day-plan question (model_only.day_plan_question), so the bare
# "and <day>?" family stays out of training: those wordings (and every one a single word from them) belong to the toolbench gate
# (scripts/toolbench/cases/brief.yaml), and ``follow_prompt`` drops any candidate within one token of a gate prompt.
FOLLOW = ["and what's on {y}?", "what's going on {y} then?", "ok and what's happening {y}?", "and anything coming up {y}?",
          "then what's planned {y}", "so what's up for {y}?", "what's on {y} then", "now what's going on {y}?",
          "and what about my plans for {y}?", "and anything due {y}", "what's coming up {y} then?", "ok now anything planned {y}?",
          "and what am i missing {y}?", "anything i should know {y} too?", "{y} next: anything i need to handle?",
          "now check {y}: anything coming up", "{y} instead, what's on my plate?", "how's {y}? anything due?",
          "what do i have coming up {y}?", "what do i have planned {y} then?", "and what's on for {y}?", "wait, what's on {y}",
          "and for {y}: anything coming up?", "also what's up for {y}", "same for {y}: what's coming up?",
          "okay, {y}: what's coming up?", "switching to {y}, what's on my plate?", "{y} instead, anything i should know?",
          "can you do {y} as well, what's on my plate?", "for {y} then, anything due?", "let's do {y} now, anything planned?",
          "what's happening {y} then?", "anything i have to do {y} too?", "ok so anything coming up {y}", "{y} too: what am i missing?",
          "then {y}: anything i need to get done?", "and my plans for {y}?", "what is my plan for {y} then?",
          "sure, what's coming up {y}?", "next, anything i should handle {y}?", "what about my to-dos {y}: anything due?"]
FPRE = ["", "", "", "", "ok ", "hmm ", "so ", "cool, ", "alright "]

CORRECT = ["no, i meant {y}: anything coming up?", "sorry i mean {y}, what's on my plate?", "i meant {y}, anything due?",
           "oops, i mean {y}, anything planned?", "actually i want {y}: anything i should know?", "wait, i mean {y}, what am i missing?",
           "i mean {y}, what's coming up", "no i meant what's on {y}", "sorry, i meant what's going on {y}",
           "i was asking about {y}, what's coming up?", "not that day, i mean {y}: anything planned?", "my bad, i meant {y}. anything due?",
           "i meant {y} instead, anything coming up?", "no no, {y}: what's on my plate?", "i mean {y}, what's up for me",
           "sorry, wrong day, i mean {y}: anything i need to handle?", "ah i meant {y}, what's happening",
           "i'm asking about {y}, what's coming up?", "no, {y}. what am i missing?", "wait no, i mean what's planned {y}"]
STEMS += ["how does {x} look: anything coming up?", "what do i have to get done {x}", "is there anything i should handle {x}",
          "tell me what's coming up {x}", "check {x} for me: what's coming up?", "walk me through {x}: anything planned?",
          "what's on my plate for {x}", "then anything i need to do {x}", "what's the story {x}, anything due?",
          "what's my schedule {x}: anything due?", "how about {x}: anything planned?", "and what's up {x}, anything planned?"]
FOLLOW += ["ok what about my schedule {y}: anything due?", "how does {y} look: anything coming up?", "and what's up {y}, anything planned?",
           "check {y} for me: what's coming up?", "what do i have to get done {y}?", "how about {y}: anything planned?",
           "is there anything i should handle {y}?", "tell me what's coming up {y}", "walk me through {y}: anything planned?",
           "and what's on my plate for {y}?", "then anything i need to do {y}?", "what's the story {y}, anything due?"]
CORRECT += ["oops i mean what's on my plate {y}", "not that one, {y}: what's coming up?", "i said {y}, anything planned?",
            "no i meant {y}, anything i need to handle", "the other one: {y}, what's coming up?", "hold on, i meant {y}: anything due?"]


def _spellings(stems):
    """Each stem again with the way people type it ('whats', 'what is'), so a day-normalised stem has several distinct wordings."""
    out = list(stems)
    for t in stems:
        for a, b in (("what's", "whats"), ("what's", "what is"), ("anything i need to", "anything i have to"),
                     ("coming up", "going on"), ("planned", "scheduled"), ("anything due", "is anything due"),
                     ("what am i missing", "what am i forgetting")):
            if a in t and t.replace(a, b) not in out:
                out.append(t.replace(a, b))
    return out


FOLLOW, CORRECT, STEMS = _spellings(FOLLOW), _spellings(CORRECT), _spellings(STEMS)

# Corrections the host's follow-up pattern does not cover are kept only when the dry-run still reaches the plan tool.
CORRECT_ALT = ["no, i meant {y}", "sorry, i meant {y}", "i meant {y}", "actually {y}", "i was asking about {y}", "oh i meant {y}"]
DRAFT = ["draft a message responding to {p}", "write a reply to {p}", "draft something back to {p}", "respond to {p} for me, just a draft",
         "can you put together a reply to {p}", "draft a text back to {p}", "answer {p}, draft it first", "write {p} back",
         "prepare a reply to {p}", "draft a response to {p}", "put together a text for {p}, replying to what they said",
         "compose a reply to {p}, i'll look it over", "make me a draft reply to {p}"]
SAYING = ["reply to {p} saying {say}", "text {p} back, {say}", "tell {p} {say}", "message {p}: {say}", "answer {p}, {say}",
          "write back to {p} and say {say}"]
RECALL_T = ["wait what time was {ref} again", "sorry, what time is {ref}", "remind me what time {ref} is", "what time did you say {ref} was",
            "{ref} was at what time again", "hold on, when is {ref}"]
RECALL_D = ["wait which day was {ref}", "sorry, what day is {ref}", "remind me which day {ref} is", "what day did you say {ref} was",
            "{ref} was which day again"]
CAL_Q = ["which events do i have {y}", "anything on my calendar {y}?", "check my calendar for {y}", "what appointments do i have {y}",
         "any meetings on my calendar {y}"]
CAL_Q += ["what's on my calendar {y}", "show me my calendar for {y}", "do i have any events {y}", "list my events for {y}",
          "what meetings do i have {y}", "pull up my calendar for {y}", "any events on my calendar {y}?", "what's in my calendar {y}"]
PAST_Q = ["what was on my calendar {y}", "what did i have {y}", "what events did i have {y}", "anything on my calendar {y}?",
          "what meetings did i have {y}", "what appointments did i have {y}", "show me my calendar from {y}", "what was i doing {y}",
          "which events did i have {y}", "list my events from {y}"]

# What a person might answer, in their own words (first person). The text sent is these words, capitalised and ended.
SAY = {
    "Maya Chen": ["yes i'll grab it", "sorry i forgot, i'll get it tomorrow", "on it, i'll stop on the way home", "i can get it after class"],
    "Theo Park": ["yes i'm still in", "sorry i can't make it", "still in, see you there", "can we push it to friday"],
    "Jordan Ellis": ["i'm finishing it tonight", "it's done", "almost done, sending it over soon", "not yet, i'll finish after dinner"],
    "Nadia Khan": ["yes they're great", "not yet, i'll look tonight", "loved them, thanks for sending", "i'll look at them on my break"],
    "Carmen Rivera": ["i'm still deciding", "i'll tell you friday", "i was thinking a scarf", "can we pick something together"],
    "Ben Ortiz": ["i'm in", "thanks but i'll skip it", "count me in, what time", "can't this time, maybe next week"],
    "Aisha Karim": ["i'll send them tonight", "i'll send them tomorrow", "sending them in a few minutes", "i'll email them after my lab"],
    "Dad": ["not yet, booking it friday", "yes it's booked", "i'll book it this weekend", "booked it, i land at noon"],
    "Hana Kowalczyk": ["not yet, i'll check", "yes it went through", "i haven't heard back, i'll check tomorrow", "it did, thank you"],
    "Naomi Brandt": ["next week works", "can we do thursday", "sure, monday morning works", "how about tuesday at 2"],
}
WEEKDAY_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")
OPENERS = re.compile(r"^(yes|no|sorry|thanks|sure|okay|ok|loved them|on it)\b(?!,)(?= \w)", re.I)


def said_text(phrase):
    """'i'll send them tonight' -> "I'll send them tonight." (the user's words: capitalised, weekdays too, ended)."""
    t = re.sub(r"\bi\b", "I", phrase.strip())
    t = WEEKDAY_RE.sub(lambda m: m.group(1).capitalize(), t)
    t = OPENERS.sub(lambda m: m.group(1) + ",", t, count=1)
    t = t[:1].upper() + t[1:]
    return t if t.endswith(("?", "!", ".")) else t + ("?" if re.match(r"(?i)can we\b|how about\b", phrase) else ".")


# What a drafted reply might say when the user gave no words. The user told the draft nothing about their own state, so a body
# asserts nothing about it (not done, not booked, not seen, not coming) and decides nothing for them: it acknowledges the message,
# holds ("I'll get back to you"), or asks back, and it names the topic the thread raised. A committing answer exists only where the
# user supplies the content (SAY, below).
DRAFTS = {
    "Maya Chen": ["Thanks for the heads up about the milk. Can I get back to you in a bit?", "Saw your note about the milk. Let me check and I'll text you back.",
                  "Got your message about the milk. Give me a little while and I'll reply properly.", "Got your note about the milk. Is it needed tonight? I'll reply properly soon."],
    "Theo Park": ["Thanks for checking about coffee. Can I get back to you shortly?", "Saw your message about coffee. Let me check my day and I'll reply soon.",
                  "About coffee, were you hoping for a particular time? Let me check and I'll confirm.", "Got your text about coffee. Give me a bit and I'll confirm."],
    "Jordan Ellis": ["Got your message about the analysis part. Can I get back to you in a bit?", "Thanks for checking in on the analysis part. Let me look and I'll reply shortly.",
                     "About the analysis part, when do you need it by?", "Saw your question about the analysis. I'll reply properly in a little while."],
    "Nadia Khan": ["Thanks for sending the photos. Can I get back to you about them in a bit?", "Got your message about the photos. I'll reply properly soon.",
                   "About the photos, which ones were you most keen to hear about?", "Saw your note about the photos. Give me a little while to reply."],
    "Carmen Rivera": ["Got your message about Mom's gift. Can I get back to you in a bit?", "Thanks for asking about Mom's gift. Let me think and I'll reply soon.",
                      "About Mom's gift, do you have any ideas?", "Saw your text about Mom's gift. Give me a little while to reply."],
    "Ben Ortiz": ["Thanks for the invite to the long run. Can I get back to you in a bit?", "Got your message about the long run. Let me check and I'll reply soon.",
                  "About the long run, can you tell me more about it? I'll get back to you.", "Saw your text about the long run. Give me a little while to reply."],
    "Aisha Karim": ["Got your message about the practice problems. Can I get back to you in a bit?", "Thanks for the reminder about the practice problems. Let me check and I'll reply soon.",
                    "Which practice problems did you mean?", "Saw your text about the practice problems. Give me a little while to reply."],
    "Dad": ["Thanks for checking in about the flight. Can I get back to you in a bit?", "Got your message about the flight home. Let me check and I'll text you back.",
            "About the flight, when do you need to know by?", "Saw your text about the flight. Give me a little while and I'll reply properly."],
    "Hana Kowalczyk": ["Got your message about the regrade. Can I get back to you in a bit?", "Thanks for checking in on the regrade. Let me look and I'll reply soon.",
                       "About the regrade, have you heard anything on your end?", "Saw your note about the regrade. Give me a little while to reply."],
    "Naomi Brandt": ["Got your message about moving our call. Can I get back to you shortly?", "Thanks for asking about moving the call. Let me check my calendar and I'll reply soon.",
                     "About moving our call, let me check my calendar and I'll get back to you.", "Saw your note about the call. Give me a little while to reply."],
}
GREET = ["", "", "Hi {nm}, ", "Hey {nm}, ", "Hey {nm}! ", "{nm}, "]


def greeted(rng, nm, body):
    """``body`` (a capitalised sentence) behind an optional greeting, capitalised correctly after a comma or a bang."""
    g = rng.choice(GREET).format(nm=nm)
    if not g:
        return body
    if g.endswith(", ") and not re.match(r"I(\b|')", body):
        body = body[:1].lower() + body[1:]
    return g + body


def first_name(who):
    return who.split()[0]


def say_day(rng, d, now, *, typo=False):
    """(the day as the user types it, the day argument the model passes) for ``d`` seen from ``now``."""
    delta = (d - now.date()).days
    if delta == 0:
        return "today", "today"
    if delta == 1:
        return "tomorrow", "tomorrow"
    name = WDN[d.weekday()]
    word = name
    if typo and rng.random() < 0.3:
        word = rng.choice(WD_TYPO[name])
    elif rng.random() < 0.12:
        word = name.capitalize()
    return word, name


RANGES = {"this_week": ["this week"], "this weekend": ["this weekend", "the weekend"], "next week": ["next week"]}


def range_days(w, arg):
    """The dates a range argument covers from now (today on, as the plan lists them)."""
    s, e, _lab = w._span("this week" if arg == "this_week" else arg, True)
    out, d = [], max(s.date(), w.now.date())
    while d < e.date() and len(out) < 8:
        out.append(d)
        d += D(days=1)
    return out


def first_prompt(rng, x, *, typo_ok=True):
    """A day question the host reads as a day-plan question and that leaks no eval or toolbench prompt."""
    from scen_r2_act import _leaks
    from service.model_only import day_plan_question
    pool = []
    for _ in range(40):
        stem = rng.choice(STEMS).format(x=x)
        p = rng.choice(PRE) + stem + (rng.choice(SUF) if not stem.endswith("?") else "")
        if day_plan_question(p) and not _leaks().leaks(p)[0] and not near_gate(p):
            pool.append(p)
    if not pool:
        raise ValueError(f"no day question for {x!r}")
    return pool[0]


_norm_tokens, _tok_dist, gate_prompts, near_gate, stem_key = S._norm_tokens, S._tok_dist, S.gate_prompts, S.near_gate, S.stem_key


def follow_prompt(rng, y, previous, *, correct=False):
    from scen_r2_act import _leaks
    from service.model_only import day_plan_question
    tmpl = CORRECT if correct else FOLLOW
    opts = []
    for t in tmpl:
        p = t.format(y=y)
        if not correct and not re.match(r"(?i)(and|ok|so|now|then|also|wait|can|same|sure|next|okay|alright|hmm|cool)\b", t):
            p = rng.choice(FPRE) + p
        if rng.random() < 0.12:
            p = p[:1].upper() + p[1:]
        opts.append(p)
    rng.shuffle(opts)
    for p in opts:
        if day_plan_question(p, previous) and not _leaks().leaks(p)[0] and not near_gate(p):
            return p
    if correct:
        alt = [t.format(y=y) for t in CORRECT_ALT]
        rng.shuffle(alt)
        for p in alt:
            if not _leaks().leaks(p)[0] and not near_gate(p):
                return p
    raise ValueError(f"no follow-up for {y!r}")


# ------------------------------------------------------------------ worlds
def populate(rng, w, now, days, *, brief=None, waiting=1, overdue=None, undated=None):
    """A Brief-rich world: events, a reminder or two, dated mail / text / note items and people waiting on a reply, with
    something on every day in ``days`` so a follow-up on any of them has an answer."""
    S.tidy_sources(w)
    used = set()
    for d in days:
        n = rng.choice([0, 1, 1, 2, 2, 3])
        S.put_events(rng, w, d, n, lo=9, hi=19, min_start=(now + D(minutes=100)) if d == now.date() else None)
        if rng.random() < 0.35:
            S.add_rem(w, rng.choice(S.REM_TITLES), S.at(d, rng.choice([9, 12, 17, 20])) if d > now.date() else
                      S.at(d, 21))
        if rng.random() < 0.8:
            kinds = rng.choice([["text"], ["mail"], ["note"], ["text", "mail"], ["mail", "note"], ["text"]]) if brief is None else brief
            for kind in kinds:
                S.seed_brief(rng, w, now, d, kind, used=used)
    # reminders dated today must not already be behind the clock when a plan lists them as time-bound
    w.reminders = [r for r in w.reminders if not (r.due and r.due.date() == now.date() and r.due < now)]
    S.fill_plan_extras(rng, now, w, rng.choice([0, 0, 1, 2]) if overdue is None else overdue, 0,
                       rng.choice([0, 0, 1]) if undated is None else undated)
    S.seed_waiting(rng, w, now, waiting)
    return w


def day_final(rng, w, now, arg):
    """(plan, final) for get_my_day_plan(day=arg), written from what the world printed."""
    plan = S.Plan(w.t_get_my_day_plan(arg))
    if plan.error:
        raise ValueError(plan.error)
    if plan.kind == "range":
        return plan, S.narrate_range(rng, plan)
    if plan.word == "today":
        return plan, S.narrate_today(rng, plan, now, "left")
    return plan, S.narrate_day(rng, plan)


def hist_turn(prompt, final):
    return turn(prompt, final, "get_my_day_plan", [])


def mk(prompt, now, w, steps, notes, history, qkind, tid, *, cards=None, approve=None):
    w.history = history
    sc = Scenario(FAMILY, prompt, now, w, steps, notes)
    sc.history = history
    sc.thread, sc.qkind = tid, qkind
    if cards is not None:
        sc.expect_cards = list(cards)
    if approve is not None:
        sc.approve = approve
    return sc


def guard_final(sc):
    for st in sc.steps:
        if isinstance(st, Final) and ISO.search(st.text):
            raise ValueError("ISO timestamp in a final")
    return sc


_REPEAT: dict = {}
REPEAT_CAP = 2


def attempt(make, rng, tries=40):
    """A scenario that runs as scripted through the real host (dry-run), built twice from one seed so the kept copy is untouched.
    Follow-up wordings repeat across conversations (``and friday?`` folds to the same prompt), so the cap is per wording."""
    last = None
    for _ in range(tries):
        seed = rng.random()
        try:
            sc = guard_final(make(random.Random(seed)))
        except Exception as exc:  # noqa: BLE001
            last = f"build {exc!r:.160}"
            continue
        key = stem_key(sc.prompt)
        if _REPEAT.get(key, 0) >= REPEAT_CAP:
            last = "wording used too often"
            continue
        ok, why = F._check(sc)
        if ok:
            _REPEAT[key] = _REPEAT.get(key, 0) + 1
            return make(random.Random(seed))
        last = why
    DROPPED.append((FAMILY, last))
    return None


# ------------------------------------------------------------------ patterns
def mk_clock(rng, hour):
    """A world clocked so the user's failing days (saturday, friday, sunday) are often 2-5 days out: Mon-Wed 55% of the time."""
    wd = rng.choice([0, 1, 2]) if rng.random() < 0.55 else None
    return S.mk(rng, weekday=wd, hour=hour)


WEEKDAY_WEIGHT = {5: 8, 4: 4, 6: 4}     # saturday, friday, sunday; every other weekday weighs 1


def pick_weekday_off(rng, now):
    """Days ahead (2-6) of a weekday, biased toward saturday, friday and sunday."""
    offs = list(range(2, 7))
    weights = [WEEKDAY_WEIGHT.get((now.weekday() + o) % 7, 1) for o in offs]
    return rng.choices(offs, weights)[0]


def pick_day_kind(rng, now, kinds):
    """One of ``kinds`` that the clock allows: (kind, arg, how it is said, days it covers)."""
    for _ in range(30):
        kind = rng.choice(kinds)
        if kind in ("this_week", "this weekend") and now.weekday() > 4:
            continue
        if kind == "weekday":
            return kind, None, None, pick_weekday_off(rng, now)
        return kind, None, None, None
    raise ValueError("no day kind")


def describe(rng, w, now, kind, off, *, typo=False):
    """(said, arg, [dates]) for a day kind on this world's clock."""
    if kind == "today":
        return "today", "today", [now.date()]
    if kind == "tomorrow":
        return "tomorrow", "tomorrow", [now.date() + D(days=1)]
    if kind == "weekday":
        d = now.date() + D(days=off)
        said_, arg = say_day(rng, d, now, typo=typo)
        return said_, arg, [d]
    said_ = rng.choice(RANGES[kind])
    return said_, kind, range_days(w, kind)


def make_pivot(rng, kinds_a, kinds_b, *, correct=False, two=False, force=None):
    """A day question, then a follow-up that changes the day or range; the follow-up reads the plan again. ``force`` is a pair
    of weekday numbers (5 = saturday, 4 = friday): the user's live failures, "what is my plan for saturday" then "and friday?"."""
    hour = rng.choice([7, 8, 9, 10, 13, 17])
    if force:
        now, w = S.mk(rng, weekday=rng.choice([0, 1, 2]), hour=hour)
        ka, kb = "weekday", "weekday"
        offa, offb = [(wd - now.weekday()) % 7 for wd in force]
    else:
        now, w = mk_clock(rng, hour=hour)
        ka, _a, _b, offa = pick_day_kind(rng, now, kinds_a)
        kb, _a2, _b2, offb = pick_day_kind(rng, now, kinds_b)
    if ka == kb and offa == offb:
        raise ValueError("same day twice")
    said_a, arg_a, days_a = describe(rng, w, now, ka, offa, typo=True)
    said_b, arg_b, days_b = describe(rng, w, now, kb, offb, typo=True)
    extra = []
    if two:
        k2, _x, _y, off2 = pick_day_kind(rng, now, ["weekday", "tomorrow", "today"])
        said_2, arg_2, days_2 = describe(rng, w, now, k2, off2)
        if arg_2 in (arg_a, arg_b):
            raise ValueError("repeat")
        extra = [(said_2, arg_2, days_2)]
    focus = sorted(set(days_a + days_b + sum([e[2] for e in extra], []))) if len(days_a + days_b) < 12 else days_a[:3] + days_b[:3]
    populate(rng, w, now, focus)
    history = []
    prev_prompt = None
    for said_i, arg_i, _d in [(said_a, arg_a, days_a)] + extra:
        q = first_prompt(rng, said_i)
        _plan, fin = day_final(rng, w, now, arg_i)
        history.append(hist_turn(q, fin))
        prev_prompt = q
    prompt = follow_prompt(rng, said_b, prev_prompt, correct=correct)
    plan, fin = day_final(rng, w, now, arg_b)
    note = ("a correction ('i mean ...'): a fresh get_my_day_plan call for the range the person meant, never the earlier answer"
            if correct else "a follow-up that changes the day: a fresh get_my_day_plan call for it (calendar, reminders and the mail / text / note items), not an answer from the turn before")
    return mk(prompt, now, w, [[Call("get_my_day_plan", day=arg_b)], Final(fin)], note, history,
              "pivot_correct" if correct else "pivot", f"daychat-{rng.random():.9f}")


def make_single(rng):
    """A day or range question with wide wording and nothing before it."""
    now, w = mk_clock(rng, hour=rng.choice([7, 8, 9, 11, 13, 16, 19]))
    kind, _a, _b, off = pick_day_kind(rng, now, ["weekday", "weekday", "tomorrow", "today", "this_week", "this weekend", "next week"])
    said_, arg, days = describe(rng, w, now, kind, off, typo=True)
    populate(rng, w, now, days[:4] if len(days) > 4 else days)
    prompt = first_prompt(rng, said_)
    plan, fin = day_final(rng, w, now, arg)
    return mk(prompt, now, w, [[Call("get_my_day_plan", day=arg)], Final(fin)],
              "a day or range question: get_my_day_plan once; the answer names calendar items and the Brief's mail / text / note items",
              [], "single", f"daychat-{rng.random():.9f}")


def make_draft(rng, mode):
    """A reply to a person the plan listed as waiting: read the thread, then draft (mode 'draft') or send (mode 'say')."""
    now, w = mk_clock(rng, hour=rng.choice([8, 9, 10, 13, 17]))
    kind, _a, _b, off = pick_day_kind(rng, now, ["weekday", "tomorrow", "this_week", "today"])
    said_, arg, days = describe(rng, w, now, kind, off, typo=True)
    populate(rng, w, now, days[:3], waiting=rng.choice([1, 1, 2]))
    plan, fin = day_final(rng, w, now, arg)
    if not plan.waiting:
        raise ValueError("nobody waiting in the plan")
    who = rng.choice([x[0] for x in plan.waiting])
    if who not in fin:
        raise ValueError("the final left the person out")
    q = first_prompt(rng, said_)
    history = [hist_turn(q, fin)]
    p = first_name(who).lower() if rng.random() < 0.8 else who.lower()
    if mode == "draft":
        prompt = rng.choice(DRAFT).format(p=p)
        text = greeted(rng, first_name(who), rng.choice(DRAFTS[who]))
        steps = [[Call("view_messages", conversation=who)], [Call("draft_message", to=who, text=text)]]
        sc = mk(prompt, now, w, steps,
                "a reply to a person the plan listed as waiting: read the thread (view_messages) before drafting, then draft_message; the host ends the turn with the draft card",
                history, "draft_reply", f"daychat-{rng.random():.9f}", cards=[])
        return sc
    say_user = rng.choice(SAY[who])
    say_text = said_text(say_user)
    prompt = rng.choice(SAYING).format(p=p, say=say_user)
    nm = first_name(who)
    body = say_text          # a dictated send carries the user's words exactly: no greeting is added
    deny = rng.random() < 0.2
    steps = [[Call("view_messages", conversation=who)], [Call("send_message", to=who, text=body)]]
    if not deny:
        steps.append(Final(rng.choice([f"I texted {nm}: “{body}”", f"Done, {nm} has your text: “{body}”", f"{nm} got your reply: “{body}”",
                                       f"Sent. {nm} will see: “{body}”"])))
    return mk(prompt, now, w, steps,
              "a reply with the user's words to a person the plan listed as waiting: read the thread, then ONE send_message behind its card"
              + ("; the card is declined, the turn ends at the call" if deny else ""),
              history, "send_reply", f"daychat-{rng.random():.9f}", cards=["send_message"], approve=[not deny])


def make_recall(rng):
    """A recall of something the earlier final stated: answered from the conversation, no tool."""
    now, w = mk_clock(rng, hour=rng.choice([8, 9, 10, 13]))
    kind, _a, _b, off = pick_day_kind(rng, now, ["weekday", "weekday", "tomorrow", "this_week"])
    said_, arg, days = describe(rng, w, now, kind, off)
    populate(rng, w, now, days[:3])
    plan, fin = day_final(rng, w, now, arg)
    cands = []
    for title, entries in plan.days:
        for when, t, src in entries:
            if src in ("email", "text", "note") and f"“{t}”" in fin:
                cands.append((title, when, t, src))
    if not cands:
        raise ValueError("no Brief item in the answer")
    sect, when, t, src = rng.choice(cands)
    ref = S.ref_of(t)
    if plan.kind == "range":
        day = {"Today": "today", "Tomorrow": "tomorrow"}.get(sect) or {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday",
                                                                          "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"}[sect.split()[0]]
    else:
        day = sect if sect in ("today", "tomorrow") else sect
    q = first_prompt(rng, said_)
    history = [hist_turn(q, fin)]
    on = f"on {day}" if day not in ("today", "tomorrow") else day
    if when != "all day":
        prompt = rng.choice(RECALL_T).format(ref=ref)
        clock = S.clock12(when)
        final = rng.choice([f"“{t}” is at {clock} {on}.", f"That one is at {clock} {on}: “{t}”.", f"{clock} {on}, that's “{t}”."])
    else:
        prompt = rng.choice(RECALL_D).format(ref=ref)
        final = rng.choice([f"“{t}” is listed {on}, without a set time.", f"That's {on}: “{t}”, with no time given.",
                            f"{S.cap1(on)}. “{t}” has no set time."])
    return mk(prompt, now, w, [Final(final)],
              "a recall of what the earlier answer stated: answered from the conversation, no tool",
              history, "recall", f"daychat-{rng.random():.9f}")


def make_calendar(rng, reminder_only=False):
    """After a plan answer, an explicit calendar ask: get_upcoming; the final adds the dated Brief items it lists apart."""
    now, w = mk_clock(rng, hour=rng.choice([8, 9, 12, 17]))
    k1, _a, _b, o1 = pick_day_kind(rng, now, ["weekday", "tomorrow"])
    said1, arg1, days1 = describe(rng, w, now, k1, o1)
    k2, _a, _b, o2 = pick_day_kind(rng, now, ["weekday", "tomorrow"])
    said2, arg2, days2 = describe(rng, w, now, k2, o2)
    if arg1 == arg2:
        raise ValueError("same day")
    populate(rng, w, now, days1 + days2)
    d2 = days2[0]
    if reminder_only and d2 not in days1:
        # a day with no event and only reminders (one or two): the calendar answer says the calendar is clear, then names them
        w.events = [e for e in w.events if not (e.start.date() <= d2 < (e.start + D(minutes=e.minutes)).date() or e.start.date() == d2)]
        w.reminders = [r for r in w.reminders if not (r.due and r.due.date() == d2)]
        taken = {r.title for r in w.reminders}
        for title in [t for t in rng.sample(S.REM_TITLES, 10) if t not in taken][:rng.choice([1, 2, 2])]:
            S.add_rem(w, title, S.at(d2, rng.choice([9, 12, 15, 17, 20])))
    q = first_prompt(rng, said1)
    _p, fin1 = day_final(rng, w, now, arg1)
    from scen_r2_act import _leaks
    opts = [t.format(y=said2) for t in CAL_Q]
    rng.shuffle(opts)
    from service.model_only import day_plan_question
    prompt = next((p for p in opts if not day_plan_question(p, q) and not _leaks().leaks(p)[0] and not near_gate(p)), None)
    if prompt is None:
        raise ValueError("no calendar prompt")
    res = w.t_get_upcoming(period=arg2)
    day = days2[0]
    items = S.day_items(w, day)
    reply = S.with_brief(rng, S.calendar_reply(rng, items, day, now), res)
    return mk(prompt, now, w, [[Call("get_upcoming", period=arg2)], Final(reply)],
              "the calendar asked for after a plan answer: get_upcoming; its items, then the dated Brief items it lists apart",
              [hist_turn(q, fin1)], "calendar_ask", f"daychat-{rng.random():.9f}")


def make_past(rng):
    """After a plan answer, a day that has passed: get_past_events (the plan tool points there for a past day)."""
    now, w = S.mk(rng, weekday=rng.choice([2, 3, 4, 5]), hour=rng.choice([9, 14, 19]))
    k1, _a, _b, o1 = pick_day_kind(rng, now, ["tomorrow", "weekday"])
    said1, arg1, days1 = describe(rng, w, now, k1, o1)
    populate(rng, w, now, days1)
    back = rng.randint(1, 2)
    d = now.date() - D(days=back)
    pool = S.POOL[:]
    rng.shuffle(pool)
    got = []
    for title, mins, loc, hours in pool:
        if len(got) >= rng.choice([1, 2, 2, 3]):
            break
        ev = S.Event(title, S.at(d, rng.choice(hours), rng.choice([0, 30])), mins, loc)
        if not any(ev.start < g.end and ev.end > g.start for g in got):
            got.append(ev)
    w.past.extend(got)
    w.past.sort(key=lambda e: e.start)
    q = first_prompt(rng, said1)
    _p, fin1 = day_final(rng, w, now, arg1)
    word = "yesterday" if back == 1 else S.wd_name(d).lower()
    opts = [t.format(y=word if word == "yesterday" else f"last {word}") for t in PAST_Q]
    from scen_r2_act import _leaks
    prompt = next((p for p in opts if not _leaks().leaks(p)[0]), None)
    if prompt is None:
        raise ValueError("no past prompt")
    evs = sorted(got, key=lambda e: e.start)
    lab = "yesterday" if back == 1 else f"on {S.wd_name(d)}"
    body = join(f"{e.title} at {S.T(e.start)}{S._at(e)}" for e in evs)
    reply = rng.choice([f"{S.cap1(lab)} you had {body}.", f"You had {body} {lab}."])
    return mk(prompt, now, w, [[Call("get_past_events", days=8)], Final(reply)],
              "a day that has passed: get_past_events, past tense, only that day's items",
              [hist_turn(q, fin1)], "past_ask", f"daychat-{rng.random():.9f}")


# ------------------------------------------------------------------ family
def fam_day_chat_r4(rng):
    out, DROPPED[:] = [], []
    _REPEAT.clear()
    plan = (
        [("single",)] * 30
        + [("pivot", ["weekday", "weekday", "tomorrow", "today"], ["weekday", "weekday", "tomorrow", "today"], False, False)] * 44
        + [("pivot", ["weekday", "tomorrow", "today"], ["weekday", "tomorrow"], False, True)] * 14
        + [("pivot", ["this_week", "this weekend"], ["weekday", "weekday", "tomorrow"], False, False)] * 16
        + [("pivot", ["weekday", "weekday", "tomorrow"], ["this_week", "this weekend", "next week"], False, False)] * 14
        + [("pivot", ["this weekend"], ["next week"], False, False)] * 4
        + [("pivot", None, None, False, False, (5, 4))] * 6 + [("pivot", None, None, False, False, (4, 5))] * 5
        + [("pivot", None, None, False, False, (5, 6))] * 4 + [("pivot", None, None, False, False, (6, 4))] * 3
        + [("pivot", None, None, True, False, (5, 4))] * 3
        + [("pivot", ["tomorrow", "today", "weekday"], ["this_week", "next week", "this weekend"], True, False)] * 20
        + [("draft",)] * 36 + [("say",)] * 14 + [("recall",)] * 16 + [("calendar",)] * 8 + [("calendar_rem",)] * 6 + [("past",)] * 10
    )
    rng.shuffle(plan)
    for spec in plan:
        if spec[0] == "single":
            make = make_single
        elif spec[0] == "pivot":
            _t, ka, kb, correct, two, *force = spec
            make = lambda r, ka=ka, kb=kb, correct=correct, two=two, force=(force[0] if force else None): make_pivot(
                r, ka, kb, correct=correct, two=two, force=force)
        elif spec[0] == "draft":
            make = lambda r: make_draft(r, "draft")
        elif spec[0] == "say":
            make = lambda r: make_draft(r, "say")
        elif spec[0] == "recall":
            make = make_recall
        elif spec[0] == "calendar":
            make = make_calendar
        elif spec[0] == "calendar_rem":
            make = lambda r: make_calendar(r, reminder_only=True)
        else:
            make = make_past
        sc = attempt(make, rng)
        if sc is not None:
            out.append(sc)
    if len(DROPPED) > len(plan) * 0.08:
        raise RuntimeError(f"day_chat_r4 lost {len(DROPPED)} of {len(plan)} scenarios: {DROPPED[:5]}")
    return out


FAMILIES_R4 = {FAMILY: fam_day_chat_r4}
