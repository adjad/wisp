"""Round-4 family ``instructions_r4``: follow what the person asked for, in the form they asked for it.

r3 trained almost no instruction following: no persistent preference, no length cap, no yes/no-first,
no language, no format limit, and corrections only one turn deep. The Ask prompt says "Give full answers
... Do NOT trim an answer down just to keep it short"; an explicit instruction from the person overrides
that, and a preference given earlier in the conversation keeps holding for as long as it is in the history
(Wisp has no tool for tone or length preferences, so it lives in the session only: each earlier Ask turn
is stored as the user text and the final assistant text, nothing else).

Ten sub-skills, every rule checked mechanically in ``rules_ok`` before a scenario is kept:

 1. length caps        "in one sentence", "under 15 words", "just the time", "one word", "just the number"
 2. answer first       "yes or no, ..." : the final starts with Yes or No and agrees with the tool result
 3. format             bullet list (every line a bullet), numbered steps, one per line, "no list, just tell me"
 4. negative limits    "don't mention the price", "no numbers", "leave out the location", "no emoji"
 5. scope of actions   "only the school email", "just draft it", "calendar only, no reminder",
                       "move only the 3pm one", "mark just the laundry one", "texts only"
 6. exact text         "text Maya exactly: ..." : the sent text equals the quoted text, no greeting or sign-off
 7. several parts      every part gets its call and its own sentence in the final
 8. corrections        "no, I meant Friday", "make it 30 minutes", "not that Maya" after 1-4 earlier turns
 9. standing wishes    an earlier turn sets "one sentence", "no questions", "no bullets", "bullets only",
                       "casual", "Spanish", "just the figure", "under twenty words" or "yes or no first"; several
                       turns later an ordinary question (general knowledge or a tool read) gets answered that way;
                       every earlier assistant turn after it obeys it too; some are lifted or changed
                       ("back to normal", "make it casual instead") and the latest wish wins
10. standing "just do it" does not lift safety: sends still show their card, an ambiguous name is still
    asked about, a clear still runs only with the scope the person gave.

11. long chats (round 4 review) a wish for no questions, plain sentences, casual talk, bullets, one sentence or Spanish is set
    in exchange 1 of 8-10 exchanges of long replies (every earlier reply obeys it); the last turn is general knowledge or a
    tool read and still obeys it.

Seeded sessions use scen_r3_followup.turn; the host trims history the way production does, so every
scenario keeps its earlier turns under ~9,000 characters with the wish 2-8 exchanges back.
"""
from __future__ import annotations

import copy
import datetime as dt
import difflib
import random
import re
import zlib

import harness as H
import thinking
import scen_r2_act as A
from harness import Call, Final
from replies import clock, join
from scen_r2_act import iso, style
from scen_r3_chat import BANNED_OPENERS, CLOSERS, EMOJI
from scen_r3_followup import at_, blank, nextday, turn
from world import BriefItem, Email, Event, Msg, Note, Reminder

FAMILY = "instructions_r4"
WRITE_ALLOW_R4 = {FAMILY: frozenset({"add_reminder", "add_calendar_event", "update_reminder", "update_event",
                                     "complete_reminder", "send_message", "send_email", "draft_email",
                                     "clear_reminders"})}

D = dt.timedelta
DROPPED: list[tuple[str, str, str]] = []
_SEEN: list[str] = []
HIST_CHARS_MAX = 9000


# =============================================================================
# text measures
# =============================================================================
_ABBR = re.compile(r"\b(Prof|Dr|Mr|Ms|Mrs|St|vs|etc)\.")
BULLET_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S")
CONTRACTION = re.compile(r"\b[A-Za-z]+(?:'(?:s|re|ve|ll|d|t|m)|n't)\b")
FORMAL = re.compile(r"\b(however|additionally|furthermore|therefore|regarding|kindly|henceforth|moreover)\b", re.I)
_ES_MARK = {"el", "la", "los", "las", "de", "que", "y", "en", "para", "una", "un", "es", "por", "con", "se", "no",
            "tu", "tus", "si", "al", "del", "hoy", "mañana", "tienes", "hay", "son", "te", "lo", "más", "muy", "ver"}
_EN_MARK = {"the", "and", "you", "your", "is", "are", "it", "to", "of", "for", "with", "this", "that", "have", "has",
            "at", "on", "in", "from", "will", "was", "its", "it's", "you're", "you've"}
WEEKDAYS_ABBR = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def sentences(text: str) -> int:
    t = _ABBR.sub(lambda m: m.group(1), re.sub(r"\s+", " ", text.strip()))
    return len([p for p in re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ“\"¿¡])", t) if p.strip()])


def words(text: str) -> int:
    return len([w for w in text.split() if re.search(r"\w", w)])


def lines(text: str) -> list[str]:
    return [l for l in text.split("\n") if l.strip()]


def has_bullets(text: str) -> bool:
    return any(BULLET_LINE.match(l) for l in lines(text))


def all_bullets(text: str) -> bool:
    ls = lines(text)
    return bool(ls) and all(re.match(r"^\s*-\s+\S", l) for l in ls)


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-záéíóúñü']+", text.lower())


def is_spanish(text: str, keep=()) -> bool:
    t = text
    for k in keep:
        t = t.replace(k, " ")
    toks = tokens(t)
    return sum(w in _ES_MARK for w in toks) >= 3 and not any(w in _EN_MARK for w in toks)


def cap1(s: str) -> str:
    return s[:1].upper() + s[1:]


def num_word_stub(n: int) -> str:
    return ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"][n]


# =============================================================================
# plumbing: build one scenario, check its rules, dry-run it through the real /agent
# =============================================================================
from service.tools.registry import REGISTRY as _REGISTRY  # noqa: E402

_TOOL_NAME = re.compile(r"\b(?:" + "|".join(sorted((t for t in _REGISTRY if "_" in t), key=len, reverse=True)) + r")\b")
_FINAL_LEAK = re.compile(r"\{\"|\bUNTRUSTED\b|haven['’]t checked (?:a |any )?current|current personal source|"
                         r"\b[a-z]+_[a-z_]+\(|\bSam['’]s\b", re.I)
_WHITELIST = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february",
              "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
              "yes", "no", "mañana", "hoy", "am", "pm", "spanish", "english", "wifi", "mac", "ok", "okay", "sure"}


def flat_calls(steps) -> list[Call]:
    out = []
    for st in steps:
        if isinstance(st, Final):
            continue
        out += list(st.calls if isinstance(st, H.Bad) else st)
    return out


def final_text(sc) -> str:
    return sc.steps[-1].text if sc.steps and isinstance(sc.steps[-1], Final) else ""


class Ctx:
    """What a rule may look at: the scripted final, the calls, and what the tools really returned."""

    def __init__(self, sc, events, shown):
        self.sc = sc
        self.prompt = sc.prompt
        self.final = final_text(sc)
        self.calls = flat_calls(sc.steps)
        self.results = [str(e.get("result")) for e in events if e.get("type") == "tool_result"]
        self.res = "\n".join(self.results)
        self.hist = list(getattr(sc, "history", None) or [])
        self.shown = shown

    def called(self, name) -> list[Call]:
        return [c for c in self.calls if c.name == name]


def _corpus(c: Ctx) -> str:
    parts = [c.prompt, c.res, re.sub(r"\b(\d+)h ahead", r"\1 hours ahead", c.res)]
    for t in c.hist:
        parts += [t["user"], t["assistant"]]
    for call in c.calls:
        for v in call.args.values():
            parts.append(str(v))
            if isinstance(v, str) and re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d", v):
                d = dt.datetime.fromisoformat(v)
                parts.append(clock(d))
    return "\n".join(parts)


def grounded(c: Ctx) -> str | None:
    """Every time, amount and name the final states is in the prompt, the earlier turns or a tool result."""
    if not c.calls:
        return None                                 # general knowledge: nothing to ground against
    corpus = _corpus(c)
    low = corpus.lower()
    for text in [c.final]:
        for tm in thinking.time_tokens(text) - thinking.time_tokens(corpus):
            return f"time {tm} not in the corpus: {text[:70]!r}"
        for am in thinking.amount_tokens(text) - thinking.amount_tokens(corpus):
            if am not in low.replace(" ", "").replace(",", ""):
                return f"amount {am} not in the corpus: {text[:70]!r}"
    if not is_spanish(c.final, keep=getattr(c.sc, "keep", ())):
        for nm in thinking._caps(c.final):
            if nm not in _WHITELIST and nm not in low:
                return f"name {nm!r} not in the corpus: {c.final[:70]!r}"
    for n in re.findall(r"\b\d{2,}\b", c.final):
        if n not in corpus and n not in {"15", "20", "30", "60"}:
            return f"number {n} not in the corpus: {c.final[:70]!r}"
    return None


def voice(text: str, *, lenient=False) -> str | None:
    low = text.lower()
    if not text.strip():
        return "empty"
    if EMOJI.search(text):
        return f"emoji: {text[:60]!r}"
    if CLOSERS.search(text):
        return f"closer: {text[:60]!r}"
    if any(low.startswith(b) for b in BANNED_OPENERS):
        return f"banned opener: {text[:60]!r}"
    if _FINAL_LEAK.search(text) or _TOOL_NAME.search(text):
        return f"leak: {text[:60]!r}"
    if "wisp" in low or re.search(r"\bsam\b", low):
        return f"names the product or the user: {text[:60]!r}"
    return None


def calls_are(c: Ctx, expect: list[tuple[str, dict]], *, extra_ok=()) -> str | None:
    """Exactly these tools, once each (plus ``extra_ok`` names), each with at least the given arguments."""
    names = [x.name for x in c.calls]
    want = [n for n, _ in expect]
    rest = list(names)
    for n in want:
        if n not in rest:
            return f"missing call {n}; calls {names}"
        rest.remove(n)
    bad = [n for n in rest if n not in extra_ok]
    if bad:
        return f"unexpected calls {bad}"
    for n, args in expect:
        hit = [x for x in c.calls if x.name == n and all(x.args.get(k) == v for k, v in args.items())]
        if not hit:
            return f"{n} lacks {args}; has {[x.args for x in c.calls if x.name == n]}"
    return None


def rules_ok(c: Ctx) -> str | None:
    e = voice(c.final) if (c.sc.steps and isinstance(c.sc.steps[-1], Final)) else None
    if e:
        return e
    for t in c.hist:
        e = voice(t["assistant"])
        if e:
            return "history: " + e
    e = grounded(c)
    if e:
        return e
    if sum(len(t["user"]) + len(t["assistant"]) for t in c.hist) > HIST_CHARS_MAX:
        return "history too long"
    if len(c.hist) > 10:
        return "history over 10 exchanges"
    for name, fn in getattr(c.sc, "rules", []):
        e = fn(c)
        if e:
            return f"{name}: {e}"
    return None


def _check(sc):
    """(ok, why): the family's own rules first (on a private run), then the host dry-run every family uses."""
    import scen_r3_followup as FU
    try:
        policy, events, final = H.run_scenario(sc.prompt, copy.deepcopy(sc.steps), sc.world, sc.when,
                                               profile=sc.profile, approve=getattr(sc, "approve", None))
    except Exception as exc:  # noqa: BLE001
        return False, f"harness {exc!r:.120}"
    errs = [e for e in events if e.get("type") == "error"]
    if errs:
        return False, f"host: {errs[0].get('detail') or errs[0].get('message')}"[:200]
    e = rules_ok(Ctx(sc, events, final))
    return (False, e) if e else (True, "")


_IMPERATIVE = {"text", "send", "remind", "add", "put", "move", "tell", "give", "list", "clear", "mark", "tick", "draft", "write",
               "convert", "count", "check", "delete", "show", "message", "email"}
_LEADS = ["hey, ", "okay so ", "um, ", "quick one, ", "hi, ", "so "]
_NO_LEAD = {"no", "sorry", "oops", "wrong", "not", "actually", "make", "it's", "i", "ok", "okay", "yes", "hey", "hi", "um", "so", "can",
            "could", "please", "pls", "plz"}


def spoken(prompt: str) -> str:
    """About a third of the prompts get a spoken opener (the choice is a function of the text, so it is stable)."""
    h = zlib.crc32(prompt.encode()) % 100
    parts = prompt.split(None, 1)
    first = parts[0].lower().strip(",.:;!?") if parts else ""
    if not parts or first in _NO_LEAD or len(parts) < 2:
        return prompt
    body = prompt[0].lower() + prompt[1:] if first not in ("i", "i'm", "i'll", "i've") else prompt
    if h < 14 and first in _IMPERATIVE:
        return "can you " + body
    if h < 12 + 14:
        lead = _LEADS[h % len(_LEADS)]
        if "quick" in prompt.lower()[:30] and lead.startswith("quick"):
            lead = "hey, "
        return lead + body
    return prompt


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def _near_dup(prompt: str) -> bool:
    """The same ask up to a typo or one changed word: the exact text, or >= 0.9 similar to a kept prompt."""
    n = _norm(prompt)
    for o in _SEEN:
        if n == o or (abs(len(n) - len(o)) < 8 and difflib.SequenceMatcher(None, n, o).ratio() >= 0.9):
            return True
    return False


def _attempt(make, rng, tries=6):
    import scen_r3_followup as FU
    for _ in range(tries):
        seed = rng.random()
        sc = make(random.Random(seed))
        if sc is None or _near_dup(sc.prompt):
            continue
        ok, why = _check(sc)
        if ok:
            sc = make(random.Random(seed))
            ok, why = FU._check(sc)
        if ok:
            _SEEN.append(_norm(sc.prompt))
            return make(random.Random(seed))
        DROPPED.append((FAMILY, sc.prompt, why))
    return None


def mk(prompt, now, w, steps, notes, *, rules=(), history=None, approve=None, cards=(), keep=()):
    if history:
        w.history = history
    prompt = spoken(prompt)
    sc = A.mk(FAMILY, prompt, now, w, steps, notes, approve=approve, cards=list(cards))
    sc.history = history
    sc.rules = list(rules)
    sc.keep = list(keep)
    return sc


def pick(rng, opts):
    return rng.choice(list(opts))


# ---- rule factories (each returns (name, fn(ctx) -> error | None)) --------------------------------------
def r_sentences(n):
    return ("sentences", lambda c: None if sentences(c.final) <= n and "\n" not in c.final.strip()
            else f"{sentences(c.final)} sentences: {c.final!r}")


def r_words_lt(n):
    return (f"words<{n}", lambda c: None if words(c.final) < n else f"{words(c.final)} words: {c.final!r}")


def r_must(*pats, flags=re.I):
    def fn(c):
        for p in pats:
            if not re.search(p, c.final, flags):
                return f"final lacks /{p}/: {c.final!r}"
        return None
    return ("must", fn)


def r_never(*pats, flags=re.I):
    def fn(c):
        for p in pats:
            if re.search(p, c.final, flags):
                return f"final has /{p}/: {c.final!r}"
        return None
    return ("never", fn)


def r_calls(expect, extra_ok=()):
    return ("calls", lambda c: calls_are(c, expect, extra_ok=extra_ok))


def r_none_of(*names):
    return ("forbidden", lambda c: next((f"called {n}" for n in names if c.called(n)), None))


def r_pred(name, fn):
    return (name, fn)


# =============================================================================
# worlds
# =============================================================================
TITLES = [("CS 162 lecture", "Kerr Hall 204"), ("Study group with Aisha", "Hollis Library, room 3B"),
          ("Pottery class", "Clay Studio on 5th"), ("Orchestra rehearsal", "Fenner Hall"),
          ("Lab meeting", "Avery Science 220"), ("Climbing session", "Summit Gym"),
          ("Piano lesson", "Ms. Alvarez's studio"), ("Career fair", "Student Union Ballroom"),
          ("Coffee with Priya", "Brewed Awakening"), ("Dentist appointment", "Pine Street Dental"),
          ("Volunteer shift", "Eastside Food Bank"), ("Advising appointment", "Mercer Hall"),
          ("Yoga class", "Rec Center Studio B"), ("Haircut", "Clipper Joe's"),
          ("Linear algebra review", "Avery Science 110"), ("Project sync with Jordan", "Kerr Hall 301"),
          ("Dinner with Carmen", "Golden Lotus"), ("Bike repair pickup", "Wheelhouse Cycles"),
          ("Writing center visit", "Hollis Library"), ("Roommate meeting", "Apartment 4B")]


def make_events(rng, now, day, n, *, lo=9, hi=18, exclude=()):
    """n events on `day`, distinct titles, starts at least two hours apart, in time order."""
    titles = [t for t in TITLES if t[0] not in exclude]
    chosen = rng.sample(titles, n)
    hours = sorted(rng.sample(range(lo, hi, 2), n))
    out = []
    for (title, loc), h in zip(chosen, hours):
        out.append(Event(title, at_(day, h, rng.choice([0, 0, 30])), rng.choice([45, 60, 90]), loc))
    return out


def world(rng, hour=None):
    """A world the scenario controls: no events, reminders, mail, texts or notes, but the usual contacts."""
    now, w = blank(rng, hour=hour)
    w.contacts.update({"Dad": ("(503) 555-0102", "dad.rivera@example.com"),
                       "Nadia Ortiz": ("(971) 555-0152", "nadia.ortiz@example.com"),
                       "Priya Nair": ("(503) 555-0129", "priya.nair@example.com")})
    return now, w


def tomorrow(now):
    return now.date() + D(days=1)


def ev_line(e: Event) -> str:
    return f"{e.title} at {clock(e.start)}"


def agenda_prose(events) -> str:
    return join(ev_line(e) for e in events)


def fin(rng, *opts):
    return rng.choice(list(opts))


# ---- get_my_day_plan (the Brief as a source) ------------------------------------------------------------
# A time-scoped question ("what's on tomorrow", "anything before 10") reads the person's Brief through
# get_my_day_plan, not the calendar alone; the final names what the PLAN lists (events, reminders, and any dated
# email, text or note the world carries), parsed from the real tool result so a Brief item can never be dropped.
# Explicit calendar asks ("events only", "just the calendar") stay get_upcoming(calendar_only=True).
_PLAN_ROW = re.compile(r"^- (all day|\d{1,2}(?::\d\d)? [ap]m) · (.+) \((calendar|reminder|email|text|note)\)$")
_SRC_WORD = {"reminder": "reminder", "email": "email", "text": "text", "note": "note"}


class PlanItem:
    def __init__(self, label, title, src):
        self.title, self.src = title.strip(), src
        if label == "all day":
            self.t, self.minutes = None, None
        else:
            m = re.fullmatch(r"(\d{1,2})(?::(\d\d))? ([ap])m", label)
            self.minutes = (int(m[1]) % 12 + (12 if m[3] == "p" else 0)) * 60 + int(m[2] or 0)
            self.t = f"{int(m[1])}{':' + m[2] if m[2] else ''} {m[3].upper()}M"

    @property
    def when(self):
        return self.t or "all day"

    @property
    def label(self):
        """The title as a list line shows it: a Brief item says where it came from."""
        return self.title if self.src == "calendar" else f"{self.title} ({_SRC_WORD[self.src]})"

    @property
    def prose(self):
        at = f" at {self.t}" if self.t else ""
        if self.src == "calendar":
            return f"{self.title}{at}" if self.t else f"{self.title} all day"
        if self.src == "reminder":
            return f"a reminder to {self.title[:1].lower() + self.title[1:]}{at}"
        return f"{self.title} (from your {'texts' if self.src == 'text' else 'notes' if self.src == 'note' else 'email'}){at}"

    @property
    def noun(self):
        return {"calendar": "event", "reminder": "reminder"}.get(self.src, "item")


def plan_items(w, day="tomorrow"):
    """The time-bound lines of get_my_day_plan(day) for this world, in the order the tool prints them."""
    return parse_plan(str(w.handle("get_my_day_plan", {"day": day})))


def parse_plan(text):
    out, section = [], ""
    for ln in text.splitlines():
        if not ln.startswith("- "):
            section = ln
            continue
        m = _PLAN_ROW.match(ln)
        if m and section.startswith("Time-bound"):
            out.append(PlanItem(m[1], m[2], m[3]))
    return out


def plan_prose(items):
    return join(i.prose for i in items)


def plan_must(items):
    """r_must patterns: every title and every clock time the plan lists."""
    return [re.escape(i.title) for i in items] + [re.escape(i.t) for i in items if i.t]


def plan_call(day="tomorrow"):
    return Call("get_my_day_plan", day=day)


# A dated to-do the Brief read from the person's texts, notes or mail (world.BriefItem): the plan lists it beside the
# calendar, and the final names it. {d} is the weekday of the day asked about.
BRIEF_BANK = [  # (title, source, who, source text, timed)
    ("Bring dessert to dinner at Mom's", "messages", "Mom", "can you bring a dessert to dinner at my place on {d}? the cousins are coming", False),
    ("Bring the folding chairs to Theo's", "messages", "Theo", "bring the folding chairs over on {d}, we're short", False),
    ("Pick up the cake for Maya's birthday", "messages", "Maya", "don't forget the cake for my birthday on {d}!", False),
    ("Call the landlord about the heater", "notes", "", "call the landlord about the heater on {d}", False),
    ("Return the borrowed drill", "notes", "", "return the borrowed drill on {d}", False),
    ("Finish Problem Set 5, Linear Algebra", "mail", "Canvas", "Problem Set 5 is due {d} at 11:59 PM", True),
    ("Submit the lab writeup", "mail", "Prof. Okafor", "the lab writeup is due {d} at 11:59 PM", True),
]


_ALSO_ROW = re.compile(r"^- [^·]+?(?:, due (?P<t>\d{1,2}(?::\d\d)? [AP]M))? · (?P<title>.+) \((?P<src>text|email|note)(?: from [^)]+)?\)$")


def brief_also(result):
    """(title, source, due clock or None) of every line under get_upcoming's "Also from your Brief" block."""
    out, on = [], False
    for ln in str(result).splitlines():
        if ln.startswith("*Also from your Brief*"):
            on = True
        elif on and ln.startswith("- "):
            m = _ALSO_ROW.match(ln)
            if m:
                out.append((m["title"], m["src"], m["t"]))
        elif on and not ln.strip():
            on = False
    return out


def brief_pack(rng, now, day, sources=("messages", "notes", "mail")):
    """(BriefItem, source object) for a Brief to-do due on `day`, or None."""
    pool = [b for b in BRIEF_BANK if b[1] in sources]
    title, src, who, text, timed = rng.choice(pool)
    wd = day.strftime("%A")
    text = text.format(d=wd)
    due = at_(day, 23, 59)
    if src == "messages":
        ref = Msg(who, who, text, now - D(hours=rng.randint(10, 30)))
    elif src == "notes":
        ref = Note(title, text, now - D(days=1))
    else:
        ref = Email(who, "noreply@school.example.com", f"{title} due {wd}", now - D(hours=rng.randint(10, 30)), text, False, False)
    return BriefItem(title, due, src, who, timed, ref), ref


def attach_brief(w, pack):
    item, ref = pack
    w.brief.append(item)
    {"messages": w.messages, "notes": w.notes, "mail": w.emails}[item.source].append(ref)


def maybe_brief(rng, w, now, day, p=0.4, sources=("messages", "notes", "mail")):
    """With probability p, give the world one Brief to-do due on `day`. The rng is drawn either way."""
    take = rng.random() < p
    pack = brief_pack(rng, now, day, sources)
    if take:
        attach_brief(w, pack)


# =============================================================================
# the chat bank: a question answered from general knowledge, in several registers
#   n normal (2-3 sentences, no question)   q normal + a natural follow-up question
#   one one sentence, at most 20 words      s a short one, at most 12 words (some)
#   cas casual (contractions)               es Spanish (2 sentences)
#   bul bullets (lines)                     fig just the figure (some)
#   steps numbered steps (some)
# =============================================================================
class Fact:
    def __init__(self, asks, n, q, one, cas, es, bul, fig=None, steps=None, s=None):
        self.asks, self.f = asks, {"n": n, "q": n + " " + q, "one": one, "cas": cas, "es": es,
                                   "bul": "\n".join("- " + b for b in bul)}
        if fig:
            self.f["fig"] = fig
        if s:
            self.f["s"] = s
        self.steps = steps
        self.kind = "chat"
        self.keep = ()

    def form(self, reg):
        return self.f[reg]

    def has(self, reg):
        return reg in self.f

    def prompt(self, rng, fig=False):
        if fig and getattr(self, "fig_asks", None):
            return pick(rng, self.fig_asks)
        return pick(rng, self.asks)


FACTS = [
    Fact(["what makes the daytime sky look blue", "how come the sky looks blue in the daytime"],
         "Sunlight holds every color, and the air scatters the short blue wavelengths far more than the longer red ones. That scattered blue light reaches your eyes from every direction, so the whole sky looks blue.",
         "Have you noticed how pale it gets near the horizon?",
         "Air scatters the short blue wavelengths of sunlight far more than red, so blue light reaches you from every direction.",
         "It's all about scattering: the air bounces blue light around way more than red, so you see blue wherever you look. It's also why sunsets turn orange, since the light has to cut through so much more air.",
         "La luz del sol contiene todos los colores, y el aire dispersa mucho más la luz azul que la roja. Por eso ves el cielo azul en todas las direcciones.",
         ["Sunlight contains every color.", "Air scatters blue light far more than red.", "So blue light reaches your eyes from every direction."],
         s="Air scatters blue sunlight more than red, so the sky looks blue."),
    Fact(["how long should i boil an egg for hard boiled", "hard boiled egg timing?"],
         "Lower the eggs into water that is already boiling and cook them for about 10 minutes for a firm, fully set yolk. Move them straight into cold water afterward so they stop cooking and peel more easily.",
         "Do you like the yolk fully set, or a little jammy?",
         "Boil them for about 10 minutes, then cool them in cold water so they peel easily.",
         "Honestly, just drop them in boiling water for about 10 minutes. Then cold water right after, since that's what makes them easy to peel.",
         "Hierve los huevos unos 10 minutos para que queden duros. Después pásalos a agua fría para que se pelen con facilidad.",
         ["Boil for about 10 minutes.", "Move them to cold water straight after.", "Peel them under a little running water."],
         fig="10 minutes", s="About 10 minutes, then into cold water.",
         steps=["Bring a pot of water to a boil.", "Lower the eggs in gently.", "Cook them for about 10 minutes.",
                "Move them into cold water, then peel."]),
    Fact(["weather versus climate, how are they different", "is climate just long-term weather"],
         "Weather is what the sky is doing right now or this week, such as rain, wind or heat. Climate is the long-run pattern for a place, usually averaged over thirty years or more.",
         "Is this for a class, or just curiosity?",
         "Weather is what happens day to day, while climate is the average pattern over decades.",
         "Weather's just what's happening outside today. Climate's the long-term pattern for a place, like what you'd expect over thirty years.",
         "El tiempo es lo que ocurre hoy o esta semana, como lluvia o viento. El clima es el patrón promedio de un lugar durante muchos años.",
         ["Weather is short-term: today's rain, wind or heat.", "Climate is the long-term average, usually over thirty years."]),
    Fact(["how do i fall asleep faster", "any tips to fall asleep quicker?"],
         "A steady bedtime and a dim, screen-free last half hour do most of the work. A cool room and a few slow breaths, longer out than in, help the body settle.",
         "What usually keeps you up, your thoughts or your phone?",
         "Keep a steady bedtime, dim the lights and put screens away for the last half hour.",
         "Try keeping your bedtime steady and ditching your phone for the last half hour. A cool, dark room and some slow breathing help a ton too, and it's easier than it sounds.",
         "Mantén una hora fija para dormir y deja las pantallas media hora antes. Una habitación fresca y oscura también ayuda mucho.",
         ["Keep the same bedtime every night.", "Dim the lights and drop screens half an hour before.", "Keep the room cool and dark."],
         steps=["Pick a bedtime and keep it.", "Put screens away 30 minutes before.", "Dim the lights and cool the room.",
                "Breathe slowly, out longer than in."]),
    Fact(["how much water should i be drinking in a day", "how many cups of water a day is enough"],
         "A common guideline is around eight cups a day, though needs shift with heat, exercise and body size. Pale yellow urine is a simple sign that you're drinking enough.",
         "Do you tend to forget until the afternoon?",
         "Around eight cups a day is a good rule of thumb, more if you're active or it's hot.",
         "Roughly eight cups a day is the usual rule of thumb. You'll need more if you're sweating a lot or it's hot out.",
         "Una guía común es beber unos ocho vasos al día, aunque depende del calor y del ejercicio. Si tu orina es amarillo claro, vas bien.",
         ["About eight cups a day is a common guideline.", "Drink more in heat or when you exercise."],
         fig="8 cups", s="About eight cups, more when active."),
    Fact(["why is my coffee so bitter", "my coffee tastes really bitter, why"],
         "Bitterness usually means over-extraction: the water was too hot, the grind too fine or the brew too long. Try a slightly coarser grind and water just off the boil.",
         "How are you brewing it at the moment?",
         "Try a coarser grind and slightly cooler water, since over-extraction is the usual cause of bitterness.",
         "Most likely it's over-extracted, so try a coarser grind and let the kettle sit a minute after it boils.",
         "Casi siempre es sobreextracción: el molido es muy fino o el agua está demasiado caliente. Prueba con un molido más grueso y deja enfriar el agua un minuto.",
         ["Use a coarser grind.", "Let boiled water rest about a minute.", "Shorten the brew time."],
         s="Over-extraction: try a coarser grind and cooler water.",
         steps=["Grind a little coarser.", "Let the boiled water rest for a minute.", "Brew a bit shorter, then taste."]),
    Fact(["how long does it take to build a habit", "how many days until a habit sticks"],
         "A well-known study found a median of about 66 days, but the range was huge, from a few weeks to most of a year. Doing it at the same time each day makes it stick sooner.",
         "Is there a habit you're trying to start?",
         "Research puts the median at about 66 days, though it varies a lot from person to person.",
         "It's about 66 days on average, but it really depends on the person. Keep it simple and do it at the same time each day.",
         "En promedio son unos 66 días, aunque varía mucho de una persona a otra. Hacerlo a la misma hora cada día ayuda.",
         ["The median in one study was about 66 days.", "The range runs from a few weeks to most of a year."],
         fig="66 days", s="About 66 days, though it varies a lot."),
    Fact(["why does my phone die faster when its cold", "why does the battery drain so fast in cold weather"],
         "Cold slows the chemical reactions inside a lithium-ion battery, so it can't deliver as much power and the percentage drops quickly. It usually bounces back once the phone warms up.",
         "Was it outside for long?",
         "Cold slows the chemistry in a lithium battery, so it delivers less power until it warms up.",
         "The cold slows down the chemistry inside the battery, so it can't put out as much power. It usually recovers once it warms back up.",
         "El frío ralentiza las reacciones químicas de la batería de litio y entrega menos energía. Suele recuperarse cuando el teléfono vuelve a calentarse.",
         ["Cold slows the reactions inside the battery.", "It delivers less power, so the percentage falls fast.", "It usually recovers once warm."]),
    Fact(["is it better to study in the morning or at night", "morning or night, when should i study"],
         "It mostly depends on when you're alert, and that differs from person to person. The best slot is the one you can keep up regularly, ideally after sleep and away from distractions.",
         "When do you feel sharpest?",
         "Whichever time you feel most alert is best, as long as you can keep it consistent.",
         "Honestly, it's whenever you're sharpest. Pick a time you can stick with and it'll work either way.",
         "Depende de cuándo estés más despierto, y eso cambia de una persona a otra. Lo mejor es elegir una hora que puedas mantener.",
         ["Study when you feel most alert.", "Pick a slot you can keep every day.", "Choose a quiet place without distractions."]),
    Fact(["how do i get better at remembering names", "tips for remembering peoples names"],
         "Repeat the name out loud as soon as you hear it, then use it once or twice in the conversation. Linking it to a small detail about the person, such as a hobby, gives it something to hold on to.",
         "Is it for a new class or a new job?",
         "Say the name out loud right away and tie it to one small detail about the person.",
         "Say the name back as soon as you hear it, then slip it into the chat once or twice. Tying it to a small detail about them is what makes it stick, and it's easier than it sounds.",
         "Repite el nombre en voz alta en cuanto lo oigas y úsalo una o dos veces en la conversación. Asociarlo con un detalle de la persona ayuda a recordarlo.",
         ["Repeat the name as soon as you hear it.", "Use it once or twice in the conversation.", "Link it to one small detail about the person."]),
    Fact(["why do we yawn", "what makes people yawn"],
         "Nobody is completely sure, but yawning may help cool the brain and shift alertness when you're tired or bored. It's also contagious, which seems linked to empathy and social bonding.",
         "Did one just sneak up on you?",
         "Researchers think yawns help cool the brain and reset alertness, and seeing one makes you want to join in.",
         "No one's totally sure, but it might help cool your brain and perk you up. And yep, it's contagious, which is probably about connecting with people.",
         "Nadie lo sabe del todo, pero bostezar puede ayudar a enfriar el cerebro y a mantenerte alerta. Además es contagioso, quizá por empatía.",
         ["It may help cool the brain.", "It can shift your alertness when you're tired or bored.", "It's contagious, likely linked to empathy."]),
    Fact(["which city is the capital of canada", "name canada's capital for me"],
         "The capital is Ottawa, in the province of Ontario, right on the border with Quebec. Toronto is bigger, which is why people often guess it first.",
         "Are you planning a trip there?",
         "The capital of Canada is Ottawa.",
         "It's Ottawa. A lot of folks guess Toronto since it's bigger, but Ottawa's the capital.",
         "La capital de Canadá es Ottawa, en la provincia de Ontario. Toronto es más grande, por eso mucha gente la confunde.",
         ["The capital is Ottawa.", "It sits in Ontario, on the Quebec border."],
         s="The capital of Canada is Ottawa."),
    Fact(["good stretch for a stiff neck", "what helps a stiff neck, a quick stretch?"],
         "Sit tall and slowly tilt your ear toward your shoulder, holding for about twenty seconds on each side. Keep your shoulders low and relaxed, and stop if anything feels sharp.",
         "Does it tend to flare up after long desk days?",
         "Tilt your ear toward your shoulder and hold for about twenty seconds on each side.",
         "Sit up tall and tilt your ear toward your shoulder, holding it about twenty seconds each side. Keep your shoulders loose, and don't push through anything sharp.",
         "Siéntate recto e inclina la oreja hacia el hombro, manteniéndolo unos veinte segundos de cada lado. Mantén los hombros relajados y detente si sientes dolor agudo.",
         ["Sit tall and tilt your ear to your shoulder.", "Hold for about twenty seconds each side.", "Keep your shoulders low and relaxed."],
         fig="20 seconds",
         steps=["Sit tall with your shoulders relaxed.", "Tilt your ear toward your shoulder.", "Hold for about twenty seconds.",
                "Switch sides, and stop if anything feels sharp."]),
    Fact(["why is the ocean salty", "how did the sea end up so salty"],
         "Rain slowly wears down rocks on land and washes tiny amounts of dissolved minerals into rivers, which carry them to the sea. When seawater evaporates, the water leaves and the salt stays behind, so over millions of years the ocean has become salty.",
         "Were you wondering about the sea in general, or one beach in particular?",
         "Rivers carry dissolved minerals from rocks into the sea, and evaporation leaves the salt behind.",
         "Basically rivers have been dragging tiny bits of dissolved rock into the sea for millions of years. The water evaporates but the salt doesn't, so it just keeps building up.",
         "La lluvia desgasta las rocas y los ríos llevan los minerales disueltos al mar. Cuando el agua se evapora, la sal se queda, y así el océano se fue volviendo salado durante millones de años.",
         ["Rain wears minerals out of rocks.", "Rivers carry them to the sea.", "Evaporation leaves the salt behind."]),
    Fact(["what actually causes hiccups", "why do i get the hiccups"],
         "Hiccups happen when the diaphragm, the muscle under your lungs, suddenly contracts and your vocal cords snap shut, which makes the little sound. Eating too fast, fizzy drinks or a sudden change in temperature are common triggers, and they usually fade within a few minutes.",
         "Did they start after a meal?",
         "The diaphragm spasms and the vocal cords snap shut, often after eating fast or drinking something fizzy.",
         "It's your diaphragm spasming, and your vocal cords snap shut right after, which is the hic. Eating fast or fizzy drinks set it off a lot, and it usually stops on its own in a few minutes.",
         "El diafragma se contrae de golpe y las cuerdas vocales se cierran, lo que produce el sonido. Comer rápido o tomar bebidas con gas suele provocarlo, y normalmente pasa en unos minutos.",
         ["The diaphragm contracts suddenly.", "The vocal cords snap shut, making the sound.", "Fast eating or fizzy drinks often trigger it."]),
    Fact(["how do tides work", "what makes the tide go in and out"],
         "The moon's gravity pulls on the ocean and lifts a bulge of water on the side facing it, with a second bulge on the far side. As the Earth turns, a coastline passes through those bulges, which is why most places get two high tides and two low tides each day.",
         "Are you heading to the coast soon?",
         "The moon's gravity raises bulges of water, and as the Earth turns, a shoreline passes through them twice a day.",
         "It's mostly the moon's gravity tugging the ocean into bulges. The Earth spins through them, so most coasts get two highs and two lows a day.",
         "La gravedad de la luna levanta un abultamiento de agua en el océano. Mientras la Tierra gira, la costa pasa por esos abultamientos, así que la mayoría de lugares tiene dos mareas altas y dos bajas al día.",
         ["The moon's gravity lifts a bulge of water.", "The Earth turns through the bulges.", "Most coasts get two highs and two lows a day."]),
    Fact(["why do onions make you cry", "how come chopping onions makes my eyes water"],
         "Cutting an onion breaks its cells and releases a chemical that turns into a mild irritant gas. When it reaches your eyes, they sting, so your tear glands make tears to wash it away. A very sharp knife or a chilled onion releases less of it.",
         "Do you chop a lot of them?",
         "Cutting releases an irritant gas that stings your eyes, so they tear up to rinse it out.",
         "Chopping breaks the onion's cells and lets out an irritating gas, and your eyes water to flush it away. A sharp knife helps, since it crushes fewer cells.",
         "Al cortar una cebolla se rompen sus células y se libera un gas irritante. Tus ojos lloran para eliminarlo, y un cuchillo muy afilado ayuda a que salga menos.",
         ["Cutting breaks the onion's cells.", "An irritant gas reaches your eyes.", "Tears wash it away."]),
    Fact(["how do noise cancelling headphones work", "what's the trick behind noise cancelling headphones"],
         "Tiny microphones on the headphones listen to the sound around you, and a chip plays back the opposite wave, so the two cancel each other out before they reach your ear. It works best on steady low sounds like an engine hum, and less well on sudden noises such as voices.",
         "Are you thinking of buying a pair?",
         "Microphones pick up the surrounding sound and the headphones play the opposite wave, which cancels it out.",
         "They listen to the noise around you and play the exact opposite sound, so the two cancel out. It works great on steady hums like a plane engine, but it doesn't do much for voices.",
         "Unos micrófonos captan el ruido del entorno y los auriculares reproducen la onda contraria, así que ambos se anulan. Funciona mejor con sonidos constantes, como el motor de un avión.",
         ["Microphones pick up outside sound.", "The headphones play the opposite wave.", "Steady low hums cancel best."]),
    Fact(["why does bread go stale", "what makes bread get hard after a day or two"],
         "Stale bread isn't really drying out. The starch in it slowly recrystallizes and pushes the moisture out, which turns the crumb firm and dry. Warming it briefly in the oven loosens the starch again, so it softens for a little while.",
         "Is there a loaf you're trying to rescue?",
         "The starch slowly recrystallizes and squeezes out moisture, and a short warm-up in the oven reverses it for a while.",
         "It isn't really drying out. The starch crystallizes and pushes the moisture out, which is why a few minutes in a warm oven makes it soft again for a bit.",
         "El pan no se seca sin más: el almidón se vuelve a cristalizar y expulsa la humedad. Calentarlo un poco en el horno lo ablanda durante un rato.",
         ["Starch recrystallizes as the bread sits.", "That pushes moisture out of the crumb.", "A brief warm-up softens it again."]),
    Fact(["what causes thunder", "why does thunder sound like that after lightning"],
         "Lightning heats the air around it to several times hotter than the surface of the sun in a split second. The air expands explosively and sends out a shock wave, which is the sound you hear. Light travels faster than sound, so you see the flash first and hear the rumble afterward.",
         "Did a storm just roll through where you are?",
         "Lightning heats the air so fast that it expands explosively, and the shock wave is the thunder you hear.",
         "Lightning heats the air around it so fast that it blasts outward, and that shock wave is the boom. You're just seeing the flash first because light beats sound.",
         "El rayo calienta el aire a su alrededor tan rápido que se expande de golpe y crea una onda de choque: ese es el trueno. Ves el relámpago antes porque la luz viaja más rápido que el sonido.",
         ["Lightning superheats the air around it.", "The air expands explosively.", "The shock wave is the sound of thunder."]),
    Fact(["why do cats purr", "what does it mean when a cat purrs"],
         "Cats purr when they are content, but also when they are nervous, hurt or hungry, so it works more like a way to self-soothe and to signal to others than a plain sign of happiness. The vibration comes from the muscles of the larynx twitching rapidly as the cat breathes.",
         "Is it your cat or someone else's?",
         "Purring is usually contentment, though cats also do it to calm themselves when nervous or hurt.",
         "Usually it means they're happy, but cats also purr to calm themselves down when they're stressed or sore. It's the little muscles in their throat twitching as they breathe.",
         "Los gatos ronronean cuando están contentos, pero también cuando están nerviosos o heridos, como una forma de calmarse. El sonido sale de los músculos de la laringe, que vibran al respirar.",
         ["Cats purr when content.", "They also purr to calm themselves when nervous or hurt.", "The sound comes from the larynx muscles."]),
    Fact(["how does a microwave heat food", "what is a microwave actually doing to my food"],
         "A microwave sends radio waves that make water molecules in the food spin back and forth very fast, and that motion makes heat. Because the waves only reach a short way in, the outside warms first and the middle catches up as the heat spreads, which is why stirring or letting it stand helps.",
         "Is something heating unevenly for you?",
         "It sends waves that make the water molecules in the food vibrate, and the motion turns into heat.",
         "It shakes the water molecules in your food really fast with radio waves, and that friction makes heat. It doesn't reach far inside directly, so letting it sit evens things out.",
         "El microondas envía ondas que hacen vibrar las moléculas de agua de la comida, y ese movimiento produce calor. Remover o dejar reposar ayuda a que se caliente de forma pareja.",
         ["Waves make water molecules spin quickly.", "That motion produces heat.", "Stirring or standing evens it out."]),
    Fact(["what's the difference between a virus and a bacterium", "virus versus bacteria, how are they different"],
         "Bacteria are tiny living cells that can reproduce on their own, and many of them are harmless or even helpful. Viruses are much smaller and can only multiply by taking over the cells of something alive. That is also why antibiotics work against bacteria but not against viruses.",
         "Is this for a class or something you read?",
         "Bacteria are living cells that reproduce on their own, while viruses need a host cell to multiply.",
         "Bacteria are living cells that can do their own thing, and plenty of them are harmless. Viruses are way smaller and need to hijack your cells to multiply, which is why antibiotics don't touch them.",
         "Las bacterias son células vivas que se reproducen solas, y muchas son inofensivas. Los virus son mucho más pequeños y necesitan una célula huésped para multiplicarse, por eso los antibióticos no les hacen nada.",
         ["Bacteria are living cells that reproduce alone.", "Viruses need a host cell to multiply.", "Antibiotics work on bacteria, not viruses."]),
    Fact(["why can i sometimes see the moon in the daytime", "how come the moon shows up during the day"],
         "The moon orbits Earth on its own schedule, so for part of every day it is above the horizon while the sun is still up. It is bright enough to show against the blue sky, especially when it is close to full or just past a quarter.",
         "Did you spot it this morning?",
         "The moon spends part of each day above the horizon, and it's bright enough to show against the sky.",
         "It's just that the moon doesn't keep the sun's hours, so it's often up while it's still light out. It's bright enough that you can see it against the blue.",
         "La luna sale y se pone con su propio horario, así que parte del día está sobre el horizonte mientras el sol sigue alto. Es tan brillante que se ve contra el cielo azul.",
         ["The moon keeps its own schedule.", "It is above the horizon for part of every day.", "It is bright enough to see against the sky."]),
    Fact(["why do leaves change color in the fall", "what turns leaves red and yellow in autumn"],
         "Leaves are green because of chlorophyll, which the tree makes all summer to capture sunlight. When the days shorten, the tree stops making it, and the yellow and orange pigments that were hidden underneath show through. The reds come from sugars that get trapped in the leaf.",
         "Is there a tree near you that has started to turn?",
         "Shorter days stop the tree making green chlorophyll, so the yellow and orange pigments underneath show through.",
         "The green comes from chlorophyll, and the tree quits making it once the days get short. That lets the yellows and oranges that've been hiding there finally show.",
         "Las hojas son verdes por la clorofila, que el árbol deja de producir cuando los días se acortan. Entonces se ven los pigmentos amarillos y naranjas que estaban debajo.",
         ["Chlorophyll makes leaves green.", "Shorter days stop the tree producing it.", "Hidden yellow and orange pigments show through."]),
    Fact(["why does ice float on water", "how come ice cubes float"],
         "Water is unusual because it expands as it freezes, so ice ends up less dense than the liquid around it. The molecules lock into an open, spaced-out pattern as they freeze. That lighter ice then rides on top instead of sinking.",
         "Are you looking at an ice cube right now?",
         "Water expands when it freezes, so ice is less dense than liquid water and rides on top.",
         "Water's weird like that: it expands when it freezes, so ice is less dense than the water around it. That's why the cubes bob at the top.",
         "El agua se expande al congelarse, así que el hielo es menos denso que el agua líquida y se queda arriba.",
         ["Water expands as it freezes.", "Ice is less dense than liquid water.", "So it floats on top."]),
    Fact(["what makes popcorn pop", "why do popcorn kernels pop"],
         "Each kernel has a little water sealed inside a hard shell. When it heats up, the water turns to steam and the pressure builds until the shell bursts. The starch inside puffs out and sets into the fluffy shape you eat.",
         "Are you making a batch tonight?",
         "A bit of water inside the kernel turns to steam, and the pressure finally bursts the hard shell.",
         "There's a drop of water sealed in each kernel, and heat turns it to steam. The pressure builds until the shell can't take it and the whole thing bursts open.",
         "Cada grano tiene un poco de agua dentro de una cáscara dura. Al calentarse se convierte en vapor y la presión rompe la cáscara.",
         ["A little water sits inside each kernel.", "Heat turns it to steam.", "The pressure bursts the shell."]),
    Fact(["how do airplanes stay up in the air", "what keeps a plane in the air"],
         "As a plane moves forward, air flows over and under its wings, which are shaped and tilted so they push the air downward. The air pushes back up on the wing, and that upward force is lift. When the engines give enough speed, the lift is greater than the weight of the plane.",
         "Are you flying somewhere soon?",
         "Wings push air downward, and the air pushes back up with enough lift to hold the plane.",
         "The wings are shaped and tilted to shove air downward, and the air shoves back up. Once the engines get it moving fast enough, that lift beats the plane's weight.",
         "Las alas empujan el aire hacia abajo y el aire empuja hacia arriba, lo que se llama sustentación. Con suficiente velocidad, esa fuerza supera el peso del avión.",
         ["Wings push the air downward.", "The air pushes back up, which is lift.", "Enough speed makes lift beat the weight."]),
    Fact(["why does a straw look bent in a glass of water", "what makes a spoon look broken in water"],
         "Light changes speed and direction a little when it passes from water into air, a bend called refraction. Your eyes trace the rays back in a straight line, so the part under the surface seems to sit in a different place. The straw itself is perfectly straight.",
         "Did you see it in a glass just now?",
         "Light bends as it leaves the water, so your eyes misplace the underwater part of the straw.",
         "That's the light bending when it leaves the water, so your eyes get tricked about where the underwater part sits. The straw's totally straight, I promise.",
         "La luz cambia de dirección al pasar del agua al aire, y por eso la parte sumergida parece estar en otro lugar. El objeto no está doblado de verdad.",
         ["Light bends passing from water to air.", "Your eyes trace the rays in a straight line.", "The submerged part seems shifted."]),
    Fact(["why do we get goosebumps", "what are goosebumps for"],
         "Tiny muscles at the base of each hair tighten and pull the hair upright when you are cold or startled. In animals with thick fur, that traps warm air or makes them look bigger. In people it's mostly a leftover reflex, though it still gives a small hint of warmth.",
         "Did something give you a chill?",
         "Tiny muscles pull each hair upright when you're cold or startled, a reflex from our furrier ancestors.",
         "Tiny muscles tug each hair upright when you're cold or startled. It's an old reflex from when we had fur, and it puffed up our coat or made us look bigger.",
         "Unos músculos diminutos levantan cada vello cuando tienes frío o te asustas. Es un reflejo heredado de cuando teníamos más pelo.",
         ["Tiny muscles pull each hair upright.", "Cold or a startle sets it off.", "It is a leftover reflex from furrier ancestors."]),
    Fact(["how does a refrigerator keep things cold", "what is a fridge actually doing to stay cold"],
         "A refrigerator moves heat out instead of making cold. A fluid circulates through coils, soaking up heat inside the box as it evaporates and then releasing that heat at the back when a compressor squeezes it back into liquid. That's why the back and sides can feel warm.",
         "Is yours making a strange noise?",
         "A fridge pumps heat out of the box with a circulating fluid, and the warmth ends up at the back.",
         "It doesn't really make cold, it moves heat. A fluid soaks up warmth inside, then dumps it out the back, which is why the coils back there feel warm.",
         "El refrigerador no crea frío, sino que saca el calor de dentro. Un líquido absorbe el calor y lo suelta por detrás, por eso la parte de atrás está tibia.",
         ["A fluid circulates through coils.", "It absorbs heat inside the box.", "It releases that heat out the back."]),
]

JOKES = [("tell me a joke about cats, no emoji please", "Why was the cat sitting on the computer? To keep an eye on the mouse."),
         ("a quick joke about coffee, and no emoji", "I tried to write a coffee joke, but it just wasn't brewing."),
         ("give me a pun about books and keep it emoji free", "I'd tell you a book pun, but I'm still working on the plot.")]


def _bank_check():
    for f in FACTS:
        for reg, t in f.f.items():
            if reg == "fig":
                continue
            assert voice(t) is None, (reg, t, voice(t))
            if reg in ("n", "q"):
                assert 2 <= sentences(t) <= 4, (reg, t)
            if reg == "n":
                assert "?" not in t, t
            if reg == "q":
                assert t.rstrip().endswith("?"), t
            if reg == "one":
                assert sentences(t) == 1 and words(t) <= 20, (words(t), t)
            if reg == "s":
                assert sentences(t) == 1 and words(t) <= 12, (words(t), t)
            if reg == "cas":
                assert CONTRACTION.search(t) and not FORMAL.search(t), t
            if reg == "es":
                assert is_spanish(t), t
            if reg == "bul":
                assert all_bullets(t), t
        if f.steps:
            assert 3 <= len(f.steps) <= 5
    for q, a in JOKES:
        assert voice(a) is None and sentences(a) <= 2


FIG_ASKS = {"how long should i boil an egg for hard boiled": ["how many minutes for a hard boiled egg", "hard boiled egg, how many minutes"],
            "how much water should i be drinking in a day": ["how many cups of water a day", "daily water, how many cups"],
            "how long does it take to build a habit": ["how many days until a habit sticks", "habit formation: how many days"],
            "good stretch for a stiff neck": ["how long do i hold a neck stretch", "neck stretch, hold it how long"]}
for _f in FACTS:
    _f.fig_asks = FIG_ASKS.get(_f.asks[0])
    assert (_f.fig_asks is not None) == _f.has("fig"), _f.asks[0]


_bank_check()


# =============================================================================
# shared small banks
# =============================================================================
TIME_KW = [("Dentist appointment", "dentist"), ("Pottery class", "pottery"), ("Orchestra rehearsal", "orchestra"),
           ("Piano lesson", "piano"), ("Yoga class", "yoga"), ("Haircut", "haircut"), ("Climbing session", "climbing")]
KW_LOC = {"Dentist appointment": "Pine Street Dental", "Pottery class": "Clay Studio on 5th",
          "Orchestra rehearsal": "Fenner Hall", "Piano lesson": "Ms. Alvarez's studio", "Yoga class": "Rec Center Studio B",
          "Haircut": "Clipper Joe's", "Climbing session": "Summit Gym"}
DAYS_AHEAD = [1, 2, 3, 4]

PEOPLE_NAMES = [("Maya Chen", "Maya"), ("Theo Park", "Theo"), ("Carmen Rivera", "Carmen"), ("Nadia Ortiz", "Nadia"),
                ("Priya Nair", "Priya")]
TEXT_BANK = ["are we still on for tonight", "i left my umbrella at your place", "can you grab milk on the way",
             "the print shop closes at six", "i booked the study room for friday", "running a few minutes behind",
             "did you see the new syllabus", "thanks for covering my shift", "come by around seven"]

MAIL_SENDERS = [("Prof. Okafor", "aokafor@linden.edu", "Project 2 extension"),
                ("Linden Registrar", "registrar@linden.edu", "Spring registration opens Monday"),
                ("Hollis Library", "circulation@linden.edu", "Two items due soon"),
                ("Fernbrook Labs", "recruiting@fernbrooklabs.example.com", "Interview confirmation"),
                ("Financial Aid Office", "finaid@linden.edu", "Your aid package is ready"),
                ("Rec Center", "rec@linden.edu", "New yoga times this week"),
                ("Dana Whitfield", "dwhitfield@linden.edu", "Lab section swap")]


MAIL_KEY = {"Prof. Okafor": "Okafor", "Linden Registrar": "Registrar", "Hollis Library": "Hollis", "Fernbrook Labs": "Fernbrook",
            "Financial Aid Office": "Financial Aid", "Rec Center": "Rec Center", "Dana Whitfield": "Dana"}


def mail(rng, now, k, *, unread=True, hours=(2, 40), account=None, senders=None):
    pool = rng.sample(senders or MAIL_SENDERS, k)
    out = []
    for j, (s, a, subj) in enumerate(pool):
        e = Email(s, a, subj, now - D(hours=rng.randint(*hours) + j * 3), "Details are in the portal.", unread, True)
        if account:
            e.account = account
        out.append(e)
    return out


def weather_day(w, i=1):
    return w.weather["Portland,OR"]["days"][i]       # (date, hi, lo, desc, rain%)


def set_rain(w, i, desc, rain):
    days = list(w.weather["Portland,OR"]["days"])
    d = days[i]
    days[i] = (d[0], d[1], d[2], desc, rain)
    w.weather["Portland,OR"]["days"] = days


def wx_world(rng, hour=None):
    from gen import make_weather
    now, w = world(rng, hour=hour)
    w.weather["Portland,OR"] = make_weather(now, rng)
    return now, w


def time_minutes(res: str) -> list[tuple[int, str]]:
    """(minutes since midnight, title) of every agenda row the tool printed."""
    out = []
    for m in re.finditer(r"\*\*(\d{1,2}):(\d\d) (AM|PM)\*\*\s+—\s+([^\n·]+)", res):
        h, mi = int(m[1]) % 12 + (12 if m[3] == "PM" else 0), int(m[2])
        out.append((h * 60 + mi, m[4].strip()))
    return out


# =============================================================================
# 1. length caps
# =============================================================================
def s1_agenda_one(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([7, 8, 21]))
        evs = make_events(rng, now, tomorrow(now), rng.choice([2, 3]))
        w.events = evs
        maybe_brief(rng, w, now, tomorrow(now))
        ask = pick(rng, ["what's on tomorrow, one sentence please", "in one sentence, what do i have tomorrow",
                         "give me tomorrow's schedule in a single sentence", "tomorrow's plans in just one sentence",
                         "one sentence only: what's lined up tomorrow"])
        items = plan_items(w)
        if not items:
            return None
        prose = plan_prose(items)
        final = fin(rng, f"Tomorrow you have {prose}.", f"You have {prose} tomorrow.",
                    f"Tomorrow holds {prose}.", f"On tomorrow's plan: {prose}.",
                    f"Lined up for tomorrow: {prose}.")
        return mk(style(rng, ask), now, w, [[plan_call()], Final(final)],
                  "length cap: one sentence, every item of tomorrow's plan with its time (time-scoped asks read the Brief)",
                  rules=[r_sentences(1), r_calls([("get_my_day_plan", {"day": "tomorrow"})]),
                         r_must(*plan_must(items))])
    return make


def s1_just_time(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([7, 8, 9]))
        title, kw = TIME_KW[(i * 2 + rng.randint(0, 1)) % len(TIME_KW)]
        off = rng.choice(DAYS_AHEAD)
        day = now.date() + D(days=off)
        others = make_events(rng, now, day, 2, exclude=[title])
        target = Event(title, at_(day, rng.choice([10, 13, 15, 16]), rng.choice([0, 30])), 60, KW_LOC[title])
        w.events = sorted(others + [target], key=lambda e: e.start)
        if len({e.start for e in w.events}) < 3 or any(abs((e.start - target.start).total_seconds()) < 3600
                                                       for e in others):
            return None
        period = "tomorrow" if off == 1 else day.strftime("%A").lower()
        when_w = "tomorrow" if off == 1 else "on " + day.strftime("%A")
        ask = pick(rng, [f"what time is the {kw} {when_w}? just the time", f"{kw} {when_w}, what time? only the time please",
                         f"just tell me the time of my {kw} {when_w}"])
        t = clock(target.start)
        return mk(style(rng, ask), now, w, [[Call("get_upcoming", period=period, query=kw)], Final(t)],
                  "length cap: just the time, nothing around it",
                  rules=[r_pred("time only", lambda c: None if c.final.strip() == t and thinking.time_tokens(t) <= thinking.time_tokens(c.res)
                                else f"not just the time: {c.final!r}"),
                         r_calls([("get_upcoming", {"query": kw})])])
    return make


def s1_under15(i):
    def make(rng):
        now, w = wx_world(rng, hour=rng.choice([7, 8, 12, 20]))
        d = weather_day(w)
        ask = pick(rng, ["weather tomorrow in under 15 words", "in under fifteen words, how's the weather tomorrow",
                         "tomorrow's forecast, keep it under 15 words"])
        rain = f", {d[4]}% chance of rain" if d[4] else ""
        final = fin(rng, f"{d[3]} tomorrow, high {d[1]}°F and low {d[2]}°F{rain}.",
                    f"Tomorrow: {d[3].lower()}, with a high of {d[1]}°F and a low of {d[2]}°F.")
        final = final[0] + final[1:] if final[0].isupper() else cap1(final)
        return mk(style(rng, ask), now, w, [[Call("get_weather", location="Portland,OR", period="tomorrow")], Final(final)],
                  "length cap: under 15 words, the day's description, high and low",
                  rules=[r_words_lt(15), r_must(str(d[1]), str(d[2])), r_calls([("get_weather", {"location": "Portland,OR"})])])
    return make


def s1_one_word(i):
    def make(rng):
        now, w = wx_world(rng, hour=rng.choice([7, 9, 19]))
        wet = i % 2 == 0
        set_rain(w, 1, "Light rain" if wet else "Sunny", 80 if wet else 0)
        ask = pick(rng, ["will it rain tomorrow? one word answer", "rain tomorrow, yes or no, one word only",
                         "one word: is it raining tomorrow?"])
        return mk(style(rng, ask), now, w, [[Call("get_weather", location="Portland,OR", period="tomorrow")],
                                             Final("Yes." if wet else "No.")],
                  "length cap: one word, and it agrees with the forecast",
                  rules=[r_pred("one word", lambda c: None if words(c.final) == 1 else f"{words(c.final)} words"),
                         r_pred("agrees", lambda c: None if (c.final.strip(". ") == "Yes") == ("80% chance" in c.res and "Light rain" in c.res)
                                else "disagrees with the forecast")])
    return make


def s1_name_only(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([10, 14, 20]))
        who = rng.sample(PEOPLE_NAMES, 3)
        for j, (full, first) in enumerate(who):
            w.messages.append(Msg(full, first, pick(rng, TEXT_BANK), now - D(hours=2 + j * 5, minutes=rng.randint(1, 40))))
        latest = who[0][0]
        ask = pick(rng, ["who texted me last? name only", "just the name of the last person who texted me",
                         "who was my most recent text from, name only"])
        return mk(style(rng, ask), now, w, [[Call("view_messages", count=5)], Final(latest)],
                  "length cap: a name and nothing else",
                  rules=[r_pred("name only", lambda c: None if c.final.strip() == c.results[0].split("\n")[0].split("] ", 1)[1].split(" — ")[0]
                                else f"not the latest sender: {c.final!r}"),
                         r_calls([("view_messages", {})])])
    return make


def s1_number_convert(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([9, 13, 18]))
        v, f, t = rng.choice([(50, "F", "C"), (68, "F", "C"), (86, "F", "C"), (25, "C", "F"), (10, "C", "F"),
                              (4, "GB", "MB"), (20, "in", "cm"), (5, "ft", "m")])
        res = w.handle("convert_units", {"value": v, "from_unit": f, "to_unit": t})
        out = re.search(r"=\s*([\d.]+)", res).group(1)
        names = {"F": "fahrenheit", "C": "celsius", "GB": "gigabytes", "MB": "megabytes", "in": "inches", "cm": "centimeters",
                 "ft": "feet", "m": "meters"}
        ask = pick(rng, [f"{v} {names[f]} in {names[t]}, just the number", f"convert {v} {names[f]} to {names[t]}. only the number",
                         f"how many {names[t]} is {v} {names[f]}? number only"])
        return mk(style(rng, ask), now, w, [[Call("convert_units", value=v, from_unit=f, to_unit=t)], Final(out)],
                  "length cap: just the number the tool returned",
                  rules=[r_pred("number only", lambda c: None if re.fullmatch(r"[\d.]+", c.final.strip())
                                and c.final.strip() == re.search(r"=\s*([\d.]+)", c.res).group(1) else f"not the result: {c.final!r}"),
                         r_calls([("convert_units", {"value": v, "from_unit": f, "to_unit": t})])])
    return make


def s1_number_calc(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([12, 19]))
        pct, amt = rng.choice([(18, "64.5"), (20, "45"), (15, "80"), (18, "52"), (20, "36.5")])
        expr = f"{pct}% of {amt}"
        out = w.handle("calculate", {"expression": expr})
        ask = pick(rng, [f"{pct} percent tip on {amt}? just the number", f"tip at {pct}% on a {amt} bill, answer with only the number"])
        return mk(style(rng, ask), now, w, [[Call("calculate", expression=expr)], Final(out)],
                  "length cap: just the number the calculation returned",
                  rules=[r_pred("number only", lambda c: None if c.final.strip() == c.results[0].strip() else f"{c.final!r} vs {c.results[0]!r}")])
    return make


def s1_count_unread(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([9, 15, 21]))
        k = rng.choice([2, 3])
        w.emails = mail(rng, now, k)
        ask = pick(rng, ["number of unread emails, digits only", "count the unread mail and answer with the number alone",
                         "unread email count, just the number", "just a number: how much unread mail is waiting"])
        return mk(style(rng, ask), now, w, [[Call("view_emails", unread=True, count=20)], Final(str(k))],
                  "length cap: a count, nothing else",
                  rules=[r_pred("count", lambda c: None if c.final.strip() == str(len(re.findall(r"^Status: unread", c.res, re.M)))
                                else f"{c.final!r} vs result"),
                         r_calls([("view_emails", {"unread": True})])])
    return make


# =============================================================================
# 2. answer first
# =============================================================================
def s2_before(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([7, 8, 21]))
        evs = make_events(rng, now, tomorrow(now), rng.choice([2, 3]), lo=9, hi=19)
        w.events = evs
        maybe_brief(rng, w, now, tomorrow(now), sources=("mail",))
        timed = [x for x in plan_items(w) if x.t]
        if not timed:
            return None
        first = timed[0]
        yes = i % 2 == 0
        if yes:
            limit_h = first.minutes // 60 + rng.choice([1, 2, 3])
        else:
            limit_h = first.minutes // 60 - rng.choice([1, 2])
            if limit_h < 8:
                return None
        label = "noon" if limit_h == 12 else clk_word(limit_h)
        only = i % 4 >= 2
        ask = pick(rng, [f"yes or no, do i have anything before {label} tomorrow?",
                         f"is anything on before {label} tomorrow? yes or no"])
        if only:
            ask = pick(rng, [f"just yes or no: anything before {label} tomorrow?", f"yes or no only, anything on before {label} tomorrow"])
            final = "Yes." if yes else "No."
        else:
            final = f"Yes, you have {first.prose}." if yes else f"No, your first {first.noun} tomorrow is {first.prose}."

        def agrees(c):
            rows = [(x.minutes, x.title) for x in parse_plan(c.res) if x.t]
            truth = any(m < limit_h * 60 for m, _ in rows)
            return None if (c.final.startswith("Yes")) == truth and c.final.split()[0].strip(",.") in ("Yes", "No") \
                else f"final {c.final!r} but truth={truth}"
        rules = [r_pred("starts yes/no", lambda c: None if re.match(r"(Yes|No)\b", c.final) else "no yes/no first"),
                 r_pred("agrees with result", agrees), r_calls([("get_my_day_plan", {"day": "tomorrow"})])]
        rules.append(r_pred("only", lambda c: None if re.fullmatch(r"(Yes|No)\.", c.final.strip()) else "not only yes/no") if only
                     else r_sentences(1))
        return mk(style(rng, ask), now, w, [[plan_call()], Final(final)],
                  "answer first: Yes/No agrees with tomorrow's plan" + (", and nothing else" if only else ", then the fact"),
                  rules=rules)
    return make


def clk_word(h):
    return f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"


def s2_rain(i):
    def make(rng):
        now, w = wx_world(rng, hour=rng.choice([7, 9, 19]))
        wet = i % 2 == 0
        pct = rng.choice([70, 80, 90]) if wet else rng.choice([0, 10])
        set_rain(w, 1, rng.choice(["Light rain", "Showers"]) if wet else rng.choice(["Sunny", "Partly cloudy"]), pct)
        d = weather_day(w)
        ask = pick(rng, ["will it rain tomorrow, yes or no first", "yes or no, is it going to rain tomorrow?",
                         "rain tomorrow? start with yes or no"])
        if wet:
            final = f"Yes, {d[3].lower()} with {'an' if pct == 80 else 'a'} {pct}% chance of rain."
        else:
            final = f"No, only a {pct}% chance of rain." if pct else "No, no rain is expected."
        return mk(style(rng, ask), now, w, [[Call("get_weather", location="Portland,OR", period="tomorrow")], Final(final)],
                  "answer first: Yes/No agrees with the rain chance",
                  rules=[r_pred("agrees", lambda c: None if c.final.startswith("Yes") == (pct >= 50) and re.match(r"(Yes|No)\b", c.final)
                                else "yes/no disagrees"), r_must(r"\b%d%%|no rain" % pct if pct else "no rain")])
    return make


def s2_text_from(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([11, 15, 20]))
        (full, first), (ofull, ofirst) = rng.sample(PEOPLE_NAMES, 2)
        got = i % 2 == 0
        text = pick(rng, TEXT_BANK)
        w.messages.append(Msg(ofull, ofirst, pick(rng, TEXT_BANK), now - D(hours=2)))
        if got:
            w.messages.append(Msg(full, first, text, now - D(hours=1, minutes=rng.randint(5, 40))))
        ask = pick(rng, [f"did {first} text me today? yes or no, then what was said", f"yes or no, has {first} messaged today? then the detail"])
        final = f"Yes, {first} wrote “{text}”." if got else f"No, nothing from {first} today."
        return mk(style(rng, ask), now, w, [[Call("view_messages", query=first, period="today")], Final(final)],
                  "answer first: Yes/No agrees with whether the person texted today",
                  rules=[r_pred("agrees", lambda c: None if c.final.startswith("Yes") == (first in c.res and "No messages" not in c.res)
                                and re.match(r"(Yes|No)\b", c.final) else "yes/no disagrees"),
                         r_calls([("view_messages", {"query": first})])])
    return make


def s2_unread_from(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([9, 14, 20]))
        (s, a, subj), (s2, a2, subj2) = rng.sample(MAIL_SENDERS, 2)
        got = i % 2 == 0
        w.emails = [Email(s2, a2, subj2, now - D(hours=7), "Details are in the portal.", True, True)]
        if got:
            w.emails.append(Email(s, a, subj, now - D(hours=3), "Details are in the portal.", True, True))
        key = MAIL_KEY[s]
        ask = pick(rng, [f"anything unread from {key}? yes or no first", f"yes or no, is there an unread email from {key}?"])
        final = f"Yes, {s} wrote “{subj}” and it's still unread." if got else f"No, there's nothing unread from {key}."
        return mk(style(rng, ask), now, w, [[Call("view_emails", query=key, unread=True)], Final(final)],
                  "answer first: Yes/No agrees with the unread search",
                  rules=[r_pred("agrees", lambda c: None if c.final.startswith("Yes") == ("From: " in c.res)
                                and re.match(r"(Yes|No)\b", c.final) else "yes/no disagrees"),
                         r_calls([("view_emails", {"query": key})])])
    return make


# =============================================================================
# 3. format
# =============================================================================
def s3_bullets(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([7, 8, 21]))
        evs = make_events(rng, now, tomorrow(now), rng.choice([2, 3]))
        w.events = evs
        maybe_brief(rng, w, now, tomorrow(now))
        ask = pick(rng, ["list tomorrow's schedule as bullet points", "tomorrow's plan as a bulleted list please",
                         "bullets only: what's on tomorrow"])
        items = plan_items(w)
        if not items:
            return None
        final = "\n".join(f"- {x.label}: {x.when}" for x in items)
        n = len(items)
        return mk(style(rng, ask), now, w, [[plan_call()], Final(final)],
                  "format: a bulleted list, every line a bullet, one per item of tomorrow's plan",
                  rules=[r_pred("all bullets", lambda c: None if all_bullets(c.final) and len(lines(c.final)) == n else "not just n bullets"),
                         r_must(*plan_must(items))])
    return make


def s3_numbered_day(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([7, 8, 20]))
        evs = make_events(rng, now, tomorrow(now), rng.choice([2, 3]))
        w.events = evs
        maybe_brief(rng, w, now, tomorrow(now))
        ask = pick(rng, ["give me tomorrow as a numbered list in time order", "number my plans for tomorrow, earliest first"])
        items = plan_items(w)
        if not items:
            return None
        final = "\n".join(f"{k}. {x.when}: {x.label}" for k, x in enumerate(items, 1))

        def numbered(c):
            ls = lines(c.final)
            for k, l in enumerate(ls, 1):
                if not l.startswith(f"{k}. "):
                    return f"line {k} is {l!r}"
            return None if len(ls) == len(items) else "wrong line count"
        return mk(style(rng, ask), now, w, [[plan_call()], Final(final)],
                  "format: numbered 1..n in time order, one per item of tomorrow's plan", rules=[r_pred("numbered", numbered),
                                                               r_must(*plan_must(items))])
    return make


def s3_steps(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([9, 14, 21]))
        pool = [f for f in FACTS if f.steps]
        f = pool[(i + rng.randint(0, 1)) % len(pool)]
        ask = f"{f.prompt(rng)}? {pick(rng, ['numbered steps please', 'give me numbered steps', 'as numbered steps, nothing else'])}"
        final = "\n".join(f"{k}. {s}" for k, s in enumerate(f.steps, 1))

        def numbered(c):
            ls = lines(c.final)
            for k, l in enumerate(ls, 1):
                if not l.startswith(f"{k}. "):
                    return f"line {k} is {l!r}"
            return None
        return mk(style(rng, ask), now, w, [Final(final)], "format: numbered steps, no other text",
                  rules=[r_pred("numbered", numbered)])
    return make


def s3_no_list(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([7, 8, 20]))
        evs = make_events(rng, now, tomorrow(now), 3, lo=8, hi=20)
        w.events = evs
        maybe_brief(rng, w, now, tomorrow(now), p=0.25)
        ask = pick(rng, ["no list, just tell me what's on tomorrow", "what's on tomorrow? no bullets, talk it through",
                         "tomorrow's schedule in plain sentences, no list"])
        items = plan_items(w)
        if len(items) < 3:
            return None
        mid = ", ".join(f"then {x.prose}" for x in items[1:-1])
        final = f"Tomorrow starts with {items[0].prose}, {mid}, and ends with {items[-1].prose}."
        return mk(style(rng, ask), now, w, [[plan_call()], Final(final)],
                  "format: prose, no list lines even though several items would normally be bulleted",
                  rules=[r_pred("no list", lambda c: None if not has_bullets(c.final) and "\n" not in c.final.strip() else "list lines"),
                         r_must(*plan_must(items))])
    return make


def s3_one_per_line(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([9, 13, 18]))
        k = rng.choice([2, 3])
        w.emails = mail(rng, now, k)
        w.emails.sort(key=lambda m: m.when, reverse=True)
        ask = pick(rng, ["unread emails, one per line, sender then subject only", "list my unread mail one per line, no bullets"])
        final = "\n".join(f"{m.sender}: {m.subject}" for m in w.emails)
        return mk(style(rng, ask), now, w, [[Call("view_emails", unread=True, count=10)], Final(final)],
                  "format: one email per line, no bullets, newest first",
                  rules=[r_pred("lines", lambda c: None if len(lines(c.final)) == k and not has_bullets(c.final) else "wrong lines"),
                         r_must(*[re.escape(m.subject) for m in w.emails])])
    return make


# =============================================================================
# 4. negative constraints
# =============================================================================
SHOPS = [("Brightline Books", "orders@brightlinebooks.example.com", "Your order has shipped", "Order 4417 shipped by UPS and is due Thursday. Order total: $48.20.", r"\$|48|total|price"),
         ("Ticket Harbor", "tickets@ticketharbor.example.com", "Your tickets are ready", "Your two tickets for Saturday's show are in the app. Amount charged: $86.00.", r"\$|86|charged|price"),
         ("Wheelhouse Cycles", "service@wheelhousecycles.example.com", "Your bike is ready", "Your tune-up is finished and the bike is ready for pickup after 10 AM. Invoice total: $72.50.", r"\$|72|invoice|price|total")]


def s4_no_price(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([9, 14, 19]))
        shop, addr, subj, body, ban = SHOPS[i % len(SHOPS)]
        w.emails = [Email(shop, addr, subj, now - D(hours=rng.randint(3, 20)), body, True, False)]
        key = shop.split()[0].lower()
        ask = pick(rng, [f"what's the update from {key}? don't mention the price", f"check the {key} email, but leave the price out",
                         f"did {key} write? tell me what it says without the cost"])
        final = {0: "Your order shipped by UPS and is due Thursday.", 1: "Your two tickets for Saturday's show are ready in the app.",
                 2: "Your bike is ready for pickup after 10 AM."}[i % len(SHOPS)]
        return mk(style(rng, ask), now, w, [[Call("view_emails", query=key)], Final(final)],
                  "negative constraint: the update without the amount that is in the email",
                  rules=[r_never(ban), r_calls([("view_emails", {"query": key})])])
    return make


def s4_no_numbers(i):
    def make(rng):
        now, w = wx_world(rng, hour=rng.choice([7, 8, 20]))
        d = weather_day(w)
        wet = d[4] >= 70
        cool = d[1] <= 57
        if wet:
            final = fin(rng, "Yes, take one: it looks wet tomorrow.", "Yes, bring a jacket, since rain is likely tomorrow.")
        elif cool:
            final = fin(rng, "Yes, a light jacket is a good idea, since tomorrow stays on the cool side.",
                        "Yes, take a light one, because tomorrow is cool.")
        else:
            final = fin(rng, "No, you can skip it, since tomorrow should be mild.", "No, tomorrow looks mild enough without one.")
        ask = pick(rng, ["do i need a jacket tomorrow? no numbers please", "jacket tomorrow, yes or no, and no temperatures",
                         "should i bring a jacket tomorrow, just words no numbers"])
        return mk(style(rng, ask), now, w, [[Call("get_weather", location="Portland,OR", period="tomorrow")], Final(final)],
                  "negative constraint: no digits at all, and the advice still agrees with the forecast",
                  rules=[r_never(r"\d"), r_pred("agrees", lambda c: None if c.final.startswith("Yes") == (wet or cool) else "disagrees")])
    return make


def s4_events_only(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([7, 8, 20]))
        evs = make_events(rng, now, tomorrow(now), 2)
        w.events = evs
        rem_title = pick(rng, ["Return library books", "Pay phone bill", "Renew bus pass", "Buy groceries"])
        w.reminders = [Reminder(rem_title, at_(tomorrow(now), 17))]
        ask = pick(rng, ["what's on tomorrow? events only, skip the reminders", "just calendar events tomorrow, leave out reminders",
                         "tomorrow's events, no reminders please"])
        # half the instances always carry a Brief item (so the calendar-only read that appends it, and the final that
        # mentions it, are in the data); the rest draw one at random
        maybe_brief(rng, w, now, tomorrow(now), p=1.0 if i % 2 == 0 else 0.4)
        final = fin(rng, f"Tomorrow you have {agenda_prose(evs)}.", f"On the calendar tomorrow: {agenda_prose(evs)}.",
                    f"Your events tomorrow are {agenda_prose(evs)}.")
        # an explicit calendar ask stays get_upcoming; the dated Brief items it appends are mentioned, not dropped
        also = brief_also(w.handle("get_upcoming", {"period": "tomorrow", "calendar_only": True}))
        if i % 2 == 0 and not also:
            return None
        for b in also:
            final += " " + {"text": f"Separately, a text mentions “{b[0]}” for tomorrow.",
                            "note": f"Separately, your notes have “{b[0]}” for tomorrow.",
                            "email": f"Separately, an email has “{b[0]}”" + (f", due {b[2]}." if b[2] else " for tomorrow.")}[b[1]]
        return mk(style(rng, ask), now, w, [[Call("get_upcoming", period="tomorrow", calendar_only=True)], Final(final)],
                  "negative constraint: events only; the reminder in the world is left out",
                  rules=[r_calls([("get_upcoming", {"period": "tomorrow", "calendar_only": True})]), r_never("remind", re.escape(rem_title)),
                         r_must(*[re.escape(e.title) for e in evs], *[re.escape(b[0]) for b in also])])
    return make


def s4_no_emoji(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([10, 15, 21]))
        ask, ans = JOKES[i % len(JOKES)]
        return mk(style(rng, ask), now, w, [Final(ans)], "negative constraint: no emoji (and a joke, from general knowledge)",
                  rules=[r_pred("no emoji", lambda c: None if not EMOJI.search(c.final) else "emoji")])
    return make


def s4_no_location(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([8, 9, 12]))
        title, kw = TIME_KW[(i * 3 + 1) % len(TIME_KW)]
        loc = KW_LOC[title]
        day = tomorrow(now)
        ev = Event(title, at_(day, rng.choice([11, 14, 16]), rng.choice([0, 30])), 60, loc)
        w.events = [ev]
        ask = pick(rng, [f"when's the {kw} tomorrow? leave out where it is", f"{kw} tomorrow, time only, skip the location"])
        final = fin(rng, f"{title} is tomorrow at {clock(ev.start)}.", f"It's tomorrow at {clock(ev.start)}.")
        return mk(style(rng, ask), now, w, [[Call("get_upcoming", period="tomorrow", query=kw)], Final(final)],
                  "negative constraint: the time without the place the result shows",
                  rules=[r_never(re.escape(loc)), r_must(re.escape(clock(ev.start)))])
    return make


def s4_skip_part(i):
    def make(rng):
        now, w = wx_world(rng, hour=rng.choice([7, 8, 20]))
        evs = make_events(rng, now, tomorrow(now), rng.choice([2, 3]))
        w.events = evs
        maybe_brief(rng, w, now, tomorrow(now), sources=("messages", "notes"))
        k = rng.choice([2, 3])
        w.emails = mail(rng, now, k)
        w.emails.sort(key=lambda m: m.when, reverse=True)
        senders = join(m.sender for m in w.emails)
        ask = pick(rng, ["quick rundown for tomorrow: schedule, weather and unread mail, but skip the weather part",
                         "give me tomorrow's schedule and my unread mail, skip the weather part"])
        items = plan_items(w)
        if not items:
            return None
        final = fin(rng, f"Tomorrow you have {plan_prose(items)}. Your unread mail is from {senders}.",
                    f"On tomorrow's plan: {plan_prose(items)}. You also have unread mail from {senders}.")
        return mk(style(rng, ask), now, w, [[plan_call(), Call("view_emails", unread=True, count=20)], Final(final)],
                  "negative constraint: the part the person ruled out (the weather) is neither fetched nor mentioned",
                  rules=[r_calls([("get_my_day_plan", {}), ("view_emails", {})]), r_none_of("get_weather"),
                         r_never(r"weather|rain|forecast|°|sunny|cloud"), r_must(*plan_must(items))])
    return make


# =============================================================================
# 5. scope of an action
# =============================================================================
def acct_view(w):
    """view_emails with an account filter, as the real tool has."""
    orig = w.t_view_emails

    def fn(args):
        a = dict(args)
        acct = a.get("account")
        if acct:
            keep = [m for m in w.emails if str(acct).lower() in m.account.lower()]
            saved, w.emails = w.emails, keep
            try:
                out = orig(**a)
            finally:
                w.emails = saved
            # the shared tool labels its cache note "Linden:"; name the account that was actually read
            return out.replace("Linden: Email is from", f"{str(acct).title()}: Email is from", 1)
        return orig(**a)
    w.overrides["view_emails"] = fn


ACCOUNTS = [("school", "sam.rivera@linden.edu", "personal", "sam.r.rivera@example.com",
             [("Prof. Okafor", "aokafor@linden.edu", "Project 2 extension"), ("Linden Registrar", "registrar@linden.edu", "Spring registration opens Monday")],
             [("Soundwave Premium", "billing@soundwave.example", "Your Soundwave receipt"), ("Carmen Rivera", "carmen.rivera@example.com", "Photos from the trip")]),
            ("work", "sam.rivera@hollislibrary.example.com", "school", "sam.rivera@linden.edu",
             [("Desk Coordinator", "desk@hollislibrary.example.com", "Shift swap request"), ("Hollis Library", "hr@hollislibrary.example.com", "Updated schedule for next week")],
             [("Dana Whitfield", "dwhitfield@linden.edu", "Lab section swap"), ("Linden Registrar", "registrar@linden.edu", "Spring registration opens Monday")])]


def s5_one_account(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([9, 13, 18]))
        want_name, want_acct, other_name, other_acct, want_mail, other_mail = ACCOUNTS[i % 2]
        w.emails = []
        want_label, other_label = want_name.title(), other_name.title()       # the Mail account names the person says aloud
        for j, (s, a, subj) in enumerate(want_mail):
            w.emails.append(Email(s, a, subj, now - D(hours=2 + j * 3), "Details inside.", True, True, to=want_acct, account=want_label))
        for j, (s, a, subj) in enumerate(other_mail):
            w.emails.append(Email(s, a, subj, now - D(hours=3 + j * 4), "Details inside.", True, True, to=other_acct, account=other_label))
        acct_view(w)
        ask, only_unread = pick(rng, [(f"only check my {want_name} email, leave the {other_name} one alone", False),
                                      (f"unread in my {want_name} email only please", True),
                                      (f"what's new in the {want_name} inbox? just that account", False)])
        (s1, _, sub1), (s2, _, sub2) = want_mail
        final = f"In your {want_name} email, {s1} wrote “{sub1}” and {s2} wrote “{sub2}”."
        bad = [x[2] for x in other_mail] + [x[0] for x in other_mail if x[0] not in (s1, s2)]
        return mk(style(rng, ask), now, w, [[Call("view_emails", account=want_label, **({"unread": True} if only_unread else {}))], Final(final)],
                  "scope: one mail account only, named as the person said it; the other account's mail is not read out",
                  rules=[r_calls([("view_emails", {"account": want_label})]), r_must(re.escape(sub1), re.escape(sub2)),
                         r_never(*[re.escape(b) for b in bad])])
    return make


DRAFTS = [("Dad", "dad", "write dad an email saying i'll be home friday evening. just draft it, don't send", "Home Friday evening",
           "I'll be home Friday evening.", "Friday"),
          ("Priya Nair", "priya", "draft an email to priya asking if we can move our study session to thursday. draft only", "Moving our study session",
           "Could we move our study session to Thursday?", "Thursday"),
          ("Prof. Okafor", "prof okafor", "put together a note to prof okafor asking if i can come to office hours on wednesday, but don't send it",
           "Office hours on Wednesday", "May I come to office hours on Wednesday?", "Wednesday"),
          ("Nadia Ortiz", "nadia", "email draft for nadia: thanks for lending me the book. leave it as a draft", "Thanks for the book",
           "Thanks for lending me the book.", "book")]


def s5_draft(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([10, 14, 21]))
        full, short, ask, subj, line, key = DRAFTS[i % len(DRAFTS)]
        greet = {"Dad": "Dad", "Priya Nair": "Priya", "Prof. Okafor": "Professor Okafor", "Nadia Ortiz": "Nadia"}[full]
        to = {"Prof. Okafor": "Prof. Okafor"}.get(full, full.split()[0])
        body = f"Hi {greet},\n\n{line}\n\nSam"
        nm = greet
        final = fin(rng, f"The draft to {nm} is ready in Mail for you to look over.", f"I wrote the email to {nm} and left it as a draft in Mail.",
                    f"Your draft to {nm} is open in Mail, ready for you to read and send.")
        return mk(style(rng, ask), now, w, [[Call("draft_email", to=to, subject=subj, body=body)], Final(final)],
                  "scope: draft only; nothing is sent and no card appears",
                  rules=[r_calls([("draft_email", {"to": to})]), r_none_of("send_email", "send_message", "schedule_send"),
                         r_pred("body", lambda c: None if key.lower() in c.called("draft_email")[0].args["body"].lower() else "body lacks the point"),
                         r_never(r"\b(sent it|has been sent|i sent|emailed him|emailed her)\b")])
    return make


EVENT_ONLY = [("Orchestra rehearsal", "thursday", "7pm", "orchestra rehearsal"), ("Pottery class", "saturday", "10am", "pottery class"),
              ("Coffee with Priya", "friday", "3pm", "coffee with priya"), ("Dinner with Carmen", "tuesday", "6:30pm", "dinner with carmen")]


def s5_calendar_only(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([8, 10, 12]))
        title, wd, tm, spoken = EVENT_ONLY[(i * 2 + rng.randint(0, 1)) % len(EVENT_ONLY)]
        d = A.wd_at(now, wd, f"at {tm}")
        ask = pick(rng, [f"put {spoken} on the calendar {wd} at {tm}, no reminder for it", f"calendar only: {spoken} {wd} at {tm}, don't set a reminder",
                         f"add {spoken} {wd} {tm} to the calendar but skip any alert"])
        final = fin(rng, f"Added {title} {A.dphr(d, now)}.", f"{title} is on the calendar {A.dphr(d, now)}.")
        return mk(style(rng, ask), now, w, [[Call("add_calendar_event", title=title, when_iso=iso(d))], Final(final)],
                  "scope: the event only; no reminder alongside it", approve=[True], cards=["add_calendar_event"],
                  rules=[r_calls([("add_calendar_event", {"title": title, "when_iso": iso(d)})]), r_none_of("add_reminder"),
                         r_never("remind")])
    return make


REM_ONLY = [("Call the landlord", "thursday", "9am", "call the landlord"), ("Return the rental skates", "saturday", "11am", "return the rental skates"),
            ("Renew the bus pass", "friday", "8am", "renew the bus pass")]


def s5_reminder_only(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([8, 10, 12]))
        title, wd, tm, spoken = REM_ONLY[(i + rng.randint(0, 1)) % len(REM_ONLY)]
        d = A.wd_at(now, wd, f"at {tm}")
        ask = pick(rng, [f"remind me to {spoken} {wd} at {tm}, just a reminder, nothing on the calendar",
                         f"remind me {wd} at {tm} to {spoken}. no calendar entry, only the reminder"])
        final = fin(rng, f"I'll remind you to {A.second(spoken)} {A.dphr(d, now)}.", f"Okay, a reminder to {A.second(spoken)} is set for {A.bare(d, now)}.")
        return mk(style(rng, ask), now, w, [[Call("add_reminder", title=title, when_iso=iso(d))], Final(final)],
                  "scope: a reminder only; the calendar is left alone", cards=[],
                  rules=[r_calls([("add_reminder", {"title": title, "when_iso": iso(d)})]), r_none_of("add_calendar_event")])
    return make


def s5_move_one(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([8, 9, 10]))
        day = tomorrow(now)
        a, b = rng.sample(TITLES, 2)
        ev_a = Event(a[0], at_(day, 13), 60, a[1])
        ev_b = Event(b[0], at_(day, 15), 60, b[1])
        w.events = [ev_a, ev_b]
        new = at_(day, rng.choice([16, 17]))
        ask = pick(rng, [f"move only the 3pm one tomorrow to {clk_word(new.hour)}, leave the 1pm alone", f"just the 3pm tomorrow, move it to {clk_word(new.hour)}. the 1pm stays"])
        final = fin(rng, f"Moved {ev_b.title} to {A.bare(new, now)}.", f"{ev_b.title} is now {A.dphr(new, now)}.")
        return mk(style(rng, ask), now, w, [[Call("get_upcoming", period="tomorrow")], [Call("update_event", title=ev_b.title, when_iso=iso(new))], Final(final)],
                  "scope: the day is read first, then only the event at the named time moves; the other one is untouched", approve=[True], cards=["update_event"],
                  rules=[r_calls([("get_upcoming", {"period": "tomorrow"}), ("update_event", {"title": ev_b.title})]), r_never(r"moved .*" + re.escape(ev_a.title))])
    return make


def s5_done_one(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([9, 12, 16]))
        titles = rng.sample(["Laundry", "Return library books", "Pay phone bill", "Buy groceries", "Water the plants"], 3)
        target = titles[0]
        w.reminders = [Reminder(t, at_(now.date(), 20) if k == 0 else at_(tomorrow(now), 9 + k)) for k, t in enumerate(titles)]
        ask = pick(rng, [f"mark just the {target.lower()} one as done, leave the others", f"tick off {target.lower()} only, nothing else"])
        final = fin(rng, f"Marked {target} as done.", f"{target} is checked off, and the rest are untouched.")
        return mk(style(rng, ask), now, w, [[Call("complete_reminder", title=target)], Final(final)],
                  "scope: only the named reminder is completed", cards=[],
                  rules=[r_calls([("complete_reminder", {"title": target})]), r_never(re.escape(titles[1]), re.escape(titles[2]))])
    return make


def s5_channel_only(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([10, 14, 19]))
        if i % 3 != 2:
            text = pick(rng, ["i booked the study room for friday", "can you bring your notes to the group", "meeting at 4 in the library"])
            w.messages = [Msg("Study group", "Aisha", text, now - D(hours=3))]
            w.emails = [Email("Dana Whitfield", "dwhitfield@linden.edu", "Study group room change", now - D(hours=5), "Moved to room 3B.", True, True)]
            ask = pick(rng, ["did the study group message me? texts only, don't look at email", "anything in the study group chat? just messages, not mail"])
            final = f"Yes, Aisha posted in Study group: “{text}”."
            call, none = Call("view_messages", query="study group"), "view_emails"
            notes = "scope: messages only; mail is not read"
        else:
            w.messages = [Msg("Aisha Karim", "Aisha", "did you finish the lab report", now - D(hours=3))]
            w.emails = [Email("Dana Whitfield", "dwhitfield@linden.edu", "Lab report format", now - D(hours=5), "Use the new template.", True, True)]
            ask = pick(rng, ["any email about the lab report? email only, not texts", "check just my mail for the lab report, skip messages"])
            final = "Yes, Dana Whitfield wrote “Lab report format” about it."
            call, none = Call("view_emails", query="lab report"), "view_messages"
            notes = "scope: mail only; messages are not read"
        return mk(style(rng, ask), now, w, [[call], Final(final)], notes,
                  rules=[r_calls([(call.name, {})]), r_none_of(none)])
    return make


# =============================================================================
# 6. exact text
# =============================================================================
EXACT = ["running 10 late", "k be there in 5", "Thanks, got it.", "yes!! see you at 7", "pls bring the charger",
         "can't talk, call u tmrw", "lol ok", "Meet at the north entrance", "dinner's at 7:30 - don't be late", "omw",
         "no worries at all", "Heads up: the lab moved to 214"]
EXACT_ASK = ["text {n} exactly: {t}", "send {n} this, word for word: \"{t}\"", "message {n} and don't change a word: {t}",
             "tell {n} verbatim \"{t}\"", "text {n} the following and nothing else: {t}"]
EXACT_PEOPLE = [("Maya", "maya"), ("Theo", "theo"), ("Carmen", "carmen"), ("Mom", "mom"), ("Nadia", "nadia"), ("Priya", "priya")]


def s6_exact_text(i, deny=False):
    def make(rng):
        now, w = world(rng, hour=rng.choice([10, 15, 20]))
        nm, word = EXACT_PEOPLE[(i + rng.randint(0, 1)) % len(EXACT_PEOPLE)]
        t = EXACT[(i * 3 + rng.randint(0, 2)) % len(EXACT)]
        ask = rng.choice(EXACT_ASK).format(n=word, t=t)
        if ask.endswith(".") and t.endswith("."):
            ask = ask
        steps = [[Call("send_message", to=nm, text=t)]]
        if not deny:
            steps.append(Final(fin(rng, f"I texted {nm}.", f"Your text to {nm} is on its way.",
                                   f"I texted {nm} exactly that: “{t}”", f"{nm} now has: “{t}”")))
        return mk(ask, now, w, steps, "exact text: the sent text is the quoted text, no greeting or sign-off" + (" (card denied)" if deny else ""),
                  approve=[not deny], cards=["send_message"],
                  rules=[r_calls([("send_message", {"to": nm, "text": t})]),
                         r_pred("literal", lambda c: None if c.called("send_message")[0].args["text"] == t else "text altered")])
    return make


def s6_exact_email(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([10, 15, 20]))
        subj, body = [("Parking", "the car is in lot C"), ("Quick note", "Running behind, start without me.")][i % 2]
        ask = pick(rng, [f"email dad, subject {subj}, body exactly: {body}", f"email dad with the subject {subj} and say only: {body}"])
        final = fin(rng, "I emailed Dad.", "Dad has your email.", "Your email to Dad is on its way.")
        return mk(ask, now, w, [[Call("send_email", to="Dad", subject=subj, body=body)], Final(final)],
                  "exact text: the email body is the quoted body, with the subject the person gave",
                  approve=[True], cards=["send_email"],
                  rules=[r_calls([("send_email", {"to": "Dad", "subject": subj, "body": body})])])
    return make


DRAFTS_SAID = [("maya", "Maya", "help me word a text to maya saying i'm on my way", "Here's one: On my way, see you soon!", "On my way, see you soon!"),
               ("theo", "Theo", "write me a short text for theo about the lab moving rooms", "How about: Heads up, the lab is moving to a different room.", "Heads up, the lab is moving to a different room."),
               ("priya", "Priya", "draft a text to priya saying thanks for the notes", "Try this: Thanks so much for the notes, they saved me.", "Thanks so much for the notes, they saved me.")]


def s6_send_draft(i):
    """'send that exactly as written': the words are the assistant's own draft from earlier in the chat."""
    def make(rng):
        now, w = world(rng, hour=rng.choice([10, 15, 20]))
        word, nm, ask_h, said, text = DRAFTS_SAID[i % len(DRAFTS_SAID)]
        hist = [turn(ask_h, said)] + filler(rng, rng.choice([1, 2]))
        ask = pick(rng, [f"send that to {word} exactly as written", f"ok send the text to {word}, don't change it"])
        return mk(ask, now, w, [[Call("send_message", to=nm, text=text)], Final(fin(rng, f"I texted {nm}.", f"Your text to {nm} is on its way.", f"I texted {nm} the draft as written: “{text}”"))],
                  "exact text: the words sent are the assistant's own draft from earlier in the chat, unchanged", history=hist,
                  approve=[True], cards=["send_message"],
                  rules=[r_calls([("send_message", {"to": nm, "text": text})])])
    return make


# =============================================================================
# 7. several parts in one request
# =============================================================================
def s7_sched_mail_reminder(i):
    def make(rng):
        now, w = world(rng, hour=9)
        h = rng.choice([11, 13, 15])
        title = pick(rng, [t for t, _ in TITLES])
        w.events = [Event(title, at_(now.date(), h), 60, "Room 12")]
        s, a, subj = pick(rng, MAIL_SENDERS)
        w.emails = [Email(s, a, subj, now - D(hours=2), "Details are in the portal.", rng.random() < 0.5, True)]
        task, spoken = pick(rng, [("Call Mom", "call mom"), ("Start the laundry", "start the laundry"), ("Email Prof. Okafor", "email Prof. Okafor"), ("Take out the recycling", "take out the recycling"), ("Stretch", "stretch")])
        rh = rng.choice([17, 18, 20])
        d = at_(now.date(), rh)
        ask = pick(rng, [f"what's my {clk_word(h)} today, who emailed me, and remind me at {rh - 12} to {spoken}",
                         f"tell me what i have at {clk_word(h)}, who wrote me this morning, and set a reminder for {clk_word(rh)} to {spoken}",
                         f"three things: what's on at {clk_word(h)} today, anything from my inbox this morning, and ping me at {clk_word(rh)} to {spoken}",
                         f"I need the {clk_word(h)} event for today, the latest sender in my mail, and a reminder to {spoken} around {clk_word(rh)}",
                         f"check today's {clk_word(h)} thing and who emailed lately, then remind me to {spoken} at {clk_word(rh)}"])
        final = (f"Your {clk_word(h).upper().replace('AM', ' AM').replace('PM', ' PM')} is {title}. {s} wrote “{subj}”. "
                 f"I'll remind you at {clock(d)} to {A.second(spoken)}.").replace("Your 1 PM", "Your 1 PM")
        calls = [Call("get_upcoming", period="today"), Call("view_emails", period="today"),
                 Call("add_reminder", title=task, when_iso=iso(d))]
        return mk(style(rng, ask), now, w, [calls, Final(final)], "several parts: the schedule, the mail and the reminder are each done and each answered",
                  cards=[], rules=[r_calls([("get_upcoming", {}), ("view_emails", {}), ("add_reminder", {"title": task, "when_iso": iso(d)})]),
                                   r_must(re.escape(title), re.escape(subj), re.escape(clock(d)), "remind")])
    return make


def s7_sched_weather(i):
    def make(rng):
        now, w = wx_world(rng, hour=rng.choice([7, 8, 20]))
        evs = make_events(rng, now, tomorrow(now), rng.choice([2, 3]))
        w.events = evs
        maybe_brief(rng, w, now, tomorrow(now))
        d = weather_day(w)
        ask = pick(rng, ["what's my first thing tomorrow and what's the weather like", "tell me tomorrow's first event, and also the forecast",
                         "first event tomorrow plus the weather, both please"])
        rain = f", with {'an' if d[4] == 80 else 'a'} {d[4]}% chance of rain" if d[4] else ", and no rain is expected"
        items = plan_items(w)
        if not items:
            return None
        first = next((x for x in items if x.t), items[0])      # the first thing on the clock
        final = f"Your first {first.noun} tomorrow is {first.prose}. The forecast is {d[3].lower()}, high {d[1]}°F and low {d[2]}°F{rain}."
        return mk(style(rng, ask), now, w, [[plan_call(), Call("get_weather", location="Portland,OR", period="tomorrow")], Final(final)],
                  "several parts: tomorrow's plan and weather, both read and both answered",
                  rules=[r_calls([("get_my_day_plan", {}), ("get_weather", {})]), r_must(re.escape(first.title), *([re.escape(first.t)] if first.t else []),
                                                                                    str(d[1]), str(d[2]))])
    return make


def s7_mail_texts(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([10, 14, 19]))
        k = rng.choice([2, 3])
        w.emails = mail(rng, now, k)
        (full, first) = pick(rng, PEOPLE_NAMES)
        text = pick(rng, TEXT_BANK)
        w.messages = [Msg(full, first, text, now - D(hours=1, minutes=20))]
        ask = pick(rng, ["how many unread emails do i have, and who texted me last", "count the unread mail and tell me the latest text, both"])
        final = f"You have {num_word_stub(k)} unread emails. The latest text is from {full}: “{text}”."
        return mk(style(rng, ask), now, w, [[Call("view_emails", unread=True, count=20), Call("view_messages", count=5)], Final(final)],
                  "several parts: the unread count and the latest text",
                  rules=[r_calls([("view_emails", {}), ("view_messages", {})]),
                         r_pred("count", lambda c: None if num_word_stub(len(re.findall(r"^Status: unread", c.res, re.M))) in c.final else "wrong count"),
                         r_must(re.escape(full), re.escape(text))])
    return make


def s7_event_and_reminder(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([8, 10, 12]))
        title, wd, tm, spoken = pick(rng, [("Dentist appointment", "thursday", "3pm", "dentist appointment"),
                                           ("Haircut", "friday", "11am", "haircut"), ("Piano lesson", "tuesday", "4pm", "piano lesson")])
        task = pick(rng, [("Bring my insurance card", "bring my insurance card"), ("Print the boarding pass", "print the boarding pass"),
                          ("Pick up the cash", "pick up the cash")])
        d = A.wd_at(now, wd, f"at {tm}")
        rd = (d - D(days=1)).replace(hour=20, minute=0)
        if rd <= now:
            return None
        ask = pick(rng, [f"put {spoken} on the calendar {wd} at {tm} and remind me the evening before at 8 to {task[1]}",
                         f"add {spoken} {wd} {tm}, plus a reminder the night before at 8pm to {task[1]}"])
        final = (f"Added {title} {A.dphr(d, now)}, and I'll remind you {A.dphr(rd, now)} to {A.second(task[1])}.")
        return mk(style(rng, ask), now, w, [[Call("add_calendar_event", title=title, when_iso=iso(d)), Call("add_reminder", title=task[0], when_iso=iso(rd))],
                                            Final(final)], "several parts: the event and the reminder are both made and both confirmed",
                  approve=[True], cards=["add_calendar_event"],
                  rules=[r_calls([("add_calendar_event", {"title": title, "when_iso": iso(d)}), ("add_reminder", {"title": task[0], "when_iso": iso(rd)})]),
                         r_must(re.escape(title), "remind")])
    return make


def s7_text_and_reminder(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([9, 11, 13]))
        nm, word, line, task = pick(rng, [("Theo", "theo", "I'm bringing the tent.", "pack the tent"),
                                           ("Maya", "maya", "I'll save you a seat.", "leave for the library")])
        d = A.wd_at(now, pick(rng, ["friday", "saturday"]), "at 6pm")
        ask = f"text {word} {line[0].lower()}{line[1:]} and remind me {w_day(d, now)} at 6pm to {task}"
        final = f"I texted {nm}, and I'll remind you {A.dphr(d, now)} to {A.second(task)}."
        return mk(style(rng, ask), now, w, [[Call("send_message", to=nm, text=line), Call("add_reminder", title=cap1(task), when_iso=iso(d))], Final(final)],
                  "several parts: the text and the reminder are both done and both confirmed", approve=[True], cards=["send_message"],
                  rules=[r_calls([("send_message", {"to": nm, "text": line}), ("add_reminder", {"title": cap1(task)})]), r_must("texted", "remind")])
    return make


def w_day(d, now):
    return d.strftime("%A").lower()


def s7_read_and_text(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([8, 10]))
        day = tomorrow(now)
        title = "Study group with Aisha"
        ev = Event(title, at_(day, rng.choice([14, 16])), 120, "Hollis Library, room 3B")
        w.events = [ev] + make_events(rng, now, day, 1, exclude=[title], lo=9, hi=12)
        w.events.sort(key=lambda e: e.start)
        w.contacts["Aisha Karim"] = ("(971) 555-0121", "aisha.karim@example.com")
        line = pick(rng, ["I'm bringing snacks.", "I'll be a few minutes late."])
        ask = f"when's the study group tomorrow, and text aisha {line[0].lower()}{line[1:]}"
        final = f"The study group is tomorrow at {clock(ev.start)}. I texted Aisha: “{line}”"
        return mk(style(rng, ask), now, w, [[Call("get_upcoming", period="tomorrow", query="study group"), Call("send_message", to="Aisha", text=line)], Final(final)],
                  "several parts: the lookup is answered and the text is sent", approve=[True], cards=["send_message"],
                  rules=[r_calls([("get_upcoming", {}), ("send_message", {"to": "Aisha", "text": line})]), r_must(re.escape(clock(ev.start)), "texted")])
    return make


# =============================================================================
# 8. corrections after earlier turns
# =============================================================================
def filler(rng, k):
    """k earlier exchanges that have nothing to do with the correction: general-knowledge questions."""
    out = []
    for f in rng.sample(FACTS, k):
        out.append(turn(f.prompt(rng), f.form("n")))
    return out


REM_THINGS = ["call the dentist", "return the library books", "submit the lab writeup", "pick up the prescription",
              "renew the bus pass", "print the ethics essay", "email Prof. Okafor", "refill the printer paper"]


def s8_reminder(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([8, 9, 10, 11]))
        thing = REM_THINGS[(i * 2 + rng.randint(0, 1)) % len(REM_THINGS)]
        title = cap1(thing)
        day = now.date() + D(days=rng.randint(1, 3))
        h = rng.choice([9, 10, 14, 16])
        due = at_(day, h)
        w.reminders = [Reminder(title, due)]
        said = A.dphr(due, now)
        hist = filler(rng, rng.choice([0, 1, 2])) + [turn(f"remind me to {thing} {day.strftime('%A').lower() if (day - now.date()).days > 1 else 'tomorrow'} at {A.clk(h, 0, 0)}",
                                                          f"Okay, I'll remind you to {thing} {said}.", "add_reminder",
                                                          [{"name": "add_reminder", "args": {"title": title, "when_iso": iso(due)}, "result": ""}])]
        if i % 2 == 0:
            other = day + D(days=rng.randint(1, 3))
            new = at_(other, h)
            ask = pick(rng, [f"no, i meant {other.strftime('%A').lower()}", f"wrong day, make it {other.strftime('%A')}"])
        else:
            nh = pick(rng, [x for x in (8, 11, 13, 15, 17) if x != h])
            new = at_(day, nh)
            ask = pick(rng, [f"sorry, make it {A.clk(nh, 0, 0)} instead", f"no, {A.clk(nh, 0, 0)} not {A.clk(h, 0, 0)}"])
        final = fin(rng, f"Fixed. The reminder to {thing} is now set for {A.bare(new, now)}.", f"Done, I'll remind you to {A.second(thing)} {A.dphr(new, now)} instead.")
        return mk(style(rng, ask), now, w, [[Call("update_reminder", title=title, when_iso=iso(new))], Final(final)],
                  "correction: the reminder just made is updated, not duplicated", history=hist, cards=[],
                  rules=[r_calls([("update_reminder", {"title": title, "when_iso": iso(new)})]), r_none_of("add_reminder")])
    return make


def s8_event(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([8, 9, 10]))
        title, spoken = pick(rng, [("Haircut", "a haircut"), ("Study session with Aisha", "a study session with aisha"),
                                   ("Piano lesson", "a piano lesson"), ("Coffee with Priya", "coffee with priya")])
        day = nextday(now, rng.randint(0, 6), 1)
        h = rng.choice([10, 13, 16])
        start = at_(day, h)
        mins = 60
        w.events = [Event(title, start, mins, "")]
        wd = day.strftime("%A")
        said = f"Added {title} on {wd} at {clock(start)}."
        hist = filler(rng, rng.choice([0, 1, 2])) + [turn(f"put {spoken} on {wd.lower()} at {A.clk(h, 0, 0)}", said, "add_calendar_event",
                                                          [{"name": "add_calendar_event", "args": {"title": title, "when_iso": iso(start)}, "result": ""}])]
        if i % 4 == 3:
            call = Call("update_event", title=title, when_iso=iso(start), duration_min=30)
            ask = pick(rng, ["actually make it 30 minutes", "it's only half an hour, change the length"])
            final = fin(rng, f"Okay, {title} is now 30 minutes, still {A.dphr(start, now)}.", f"Changed {title} to 30 minutes.")
            args = {"title": title, "duration_min": 30}
        else:
            nh = pick(rng, [x for x in (11, 12, 14, 15, 17) if x != h])
            new = at_(day, nh)
            call = Call("update_event", title=title, when_iso=iso(new))
            ask = pick(rng, [f"no, {A.clk(nh, 0, 0)} not {A.clk(h, 0, 0)}", f"sorry, I meant {A.clk(nh, 0, 0)}", f"make that {A.clk(nh, 0, 0)} please"])
            final = fin(rng, f"Fixed. {title} is on {wd} at {clock(new)}.", f"{title} is now {A.dphr(new, now)}.")
            args = {"title": title, "when_iso": iso(new)}
        return mk(style(rng, ask), now, w, [[call], Final(final)], "correction: the event just added is changed, not added again",
                  history=hist, approve=[True], cards=["update_event"],
                  rules=[r_calls([("update_event", args)]), r_none_of("add_calendar_event")])
    return make


# first names used nowhere else in this family, so "text <name>" is only ever ambiguous for these
PAIRS = [("Alex Brandt", "Alex Moreno", "Alex"), ("Sasha Petrov", "Sasha Bell", "Sasha"), ("Riley Kwan", "Riley Osei", "Riley")]


def s8_person(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([10, 13, 18]))
        right, wrong, first = PAIRS[i % len(PAIRS)]
        t_right, t_wrong = rng.sample(TEXT_BANK, 2)
        w.messages = [Msg(wrong, first, t_wrong, now - D(hours=1)), Msg(right, first, t_right, now - D(hours=6))]
        hist = filler(rng, rng.choice([0, 1])) + [turn(f"what did {first.lower()} say", f"{wrong} wrote “{t_wrong}”.", "view_messages",
                                                      [{"name": "view_messages", "args": {"query": first}, "result": ""}])]
        ask = pick(rng, [f"not that {first}, {right}", f"no, i meant {right}", f"wrong one, the {first} i want is {right}"])
        final = f"{right} wrote “{t_right}”."
        return mk(style(rng, ask), now, w, [[Call("view_messages", query=right)], Final(final)],
                  "correction: the other person is read, the first read is not repeated", history=hist,
                  rules=[r_calls([("view_messages", {"query": right})]), r_never(re.escape(t_wrong))])
    return make


def s8_day(i):
    def make(rng):
        from gen import make_weather
        from scen_r3_followup import _fline
        if i % 2 == 0:
            now, w = world(rng, hour=rng.choice([8, 10, 12]))
            w.weather["Portland,OR"] = make_weather(now, rng)
            hist = filler(rng, rng.choice([0, 1])) + [turn("weather today?", _fline(w.weather["Portland,OR"], 0, "Portland"), "get_weather",
                                                          [{"name": "get_weather", "args": {"location": "Portland,OR"}, "result": ""}])]
            ask = pick(rng, ["sorry, i meant tomorrow", "no, tomorrow's weather"])
            return mk(style(rng, ask), now, w, [[Call("get_weather", location="Portland,OR", period="tomorrow")],
                                                Final(_fline(w.weather["Portland,OR"], 1, "Portland"))],
                      "correction: same read, the day the person meant", history=hist,
                      rules=[r_calls([("get_weather", {"period": "tomorrow"})]), r_must("tomorrow")])
        now, w = world(rng, hour=rng.choice([7, 8]))
        d1, d2 = now.date(), tomorrow(now)
        w.events = make_events(rng, now, d1, 2, lo=10, hi=20) + make_events(rng, now, d2, rng.choice([2, 3]))
        maybe_brief(rng, w, now, d2)
        items1, items2 = plan_items(w, "today"), plan_items(w, "tomorrow")
        if not items1 or not items2:
            return None
        reply1 = rng.choice([f"Today you have {plan_prose(items1)}.", f"On today's plan: {plan_prose(items1)}."])
        hist = filler(rng, rng.choice([0, 1])) + [turn("what's on today", reply1, "get_my_day_plan",
                                                      [{"name": "get_my_day_plan", "args": {"day": "today"}, "result": ""}])]
        ask = pick(rng, ["no, i meant tomorrow", "sorry, tomorrow", "i meant tomorrow, not today"])
        final = rng.choice([f"Tomorrow you have {plan_prose(items2)}.", f"On tomorrow's plan: {plan_prose(items2)}."])
        return mk(style(rng, ask), now, w, [[plan_call("tomorrow")], Final(final)],
                  "correction: same read (the day plan), the day the person meant", history=hist,
                  rules=[r_calls([("get_my_day_plan", {"day": "tomorrow"})]), r_must(*plan_must(items2))])
    return make


def s8_recipient(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([10, 14]))
        (a, a_word), (b, b_word) = rng.sample([("Theo", "theo"), ("Carmen", "carmen"), ("Maya", "maya"), ("Priya", "priya")], 2)
        line = pick(rng, ["I'll be there at 6.", "I'm running ten minutes behind.", "I got the tickets."])
        hist = filler(rng, rng.choice([0, 1])) + [turn(f"text {a_word} {line[0].lower()}{line[1:]}", f"I texted {a}.", "send_message",
                                                      [{"name": "send_message", "args": {"to": a, "text": line}, "result": ""}])]
        ask = pick(rng, [f"oops, wrong person, send it to {b_word} instead", f"sorry, i meant {b_word}. send that to her"
                         if b in ("Carmen", "Maya", "Priya") else f"sorry, i meant {b_word}. send that to him"])
        return mk(style(rng, ask), now, w, [[Call("send_message", to=b, text=line)], Final(fin(rng, f"I texted {b}.", f"Your text to {b} is on its way.", f"{b} got the same words: “{line}”", f"{b} now has it: “{line}”"))],
                  "correction: the same words go to the right person, once", history=hist, approve=[True], cards=["send_message"],
                  rules=[r_calls([("send_message", {"to": b, "text": line})])])
    return make


# =============================================================================
# 10. "just do it" does not lift the safeguards
# =============================================================================
STANDING_ASK = ["from now on just do things, don't keep asking me to confirm", "stop asking me for confirmation, just do it",
                "i don't want any more questions, just handle things"]
STANDING_ACK = ["Okay, I'll skip the extra questions. Anything that sends a message or changes your calendar will still ask for your tap first.",
                "Understood, fewer questions from here. Sends and calendar changes will still show you a prompt before they happen.",
                "Sure, I'll stop checking in on small things. A message or a deletion will still wait for your tap."]


def s10_just_do_it(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([10, 15, 19]))
        nm, word = pick(rng, [("Theo", "theo"), ("Carmen", "carmen"), ("Priya", "priya"), ("Mom", "mom")])
        line = pick(rng, ["I'm outside.", "I'm parked by the north gate.", "I'm at the front desk."])
        hist = filler(rng, rng.choice([0, 1])) + [turn(pick(rng, STANDING_ASK), pick(rng, STANDING_ACK))] + filler(rng, rng.choice([0, 1]))
        ask = f"text {word} {line[0].lower()}{line[1:]}"
        return mk(style(rng, ask), now, w, [[Call("send_message", to=nm, text=line)], Final(fin(rng, f"I texted {nm}.", f"Your text to {nm} is on its way.", f"I texted {nm}: “{line}”"))],
                  "standing 'just do it': the send still runs normally and still shows its card", history=hist, approve=[True], cards=["send_message"],
                  rules=[r_calls([("send_message", {"to": nm, "text": line})])])
    return make


def s10_ambiguous(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([10, 15, 19]))
        right, wrong, first = PAIRS[i % len(PAIRS)]
        w.contacts[right] = ("(503) 555-0187", f"{right.split()[0].lower()}.{right.split()[1].lower()}@example.com")
        w.contacts[wrong] = ("(971) 555-0166", "")
        hist = filler(rng, rng.choice([0, 1])) + [turn(pick(rng, STANDING_ASK), pick(rng, STANDING_ACK))] + filler(rng, rng.choice([0, 1]))
        ask = f"text {first.lower()} {pick(rng, ['i am outside', 'i will be late', 'i have the tickets'])}"
        q = pick(rng, [f"Which {first} do you mean, {right} or {wrong}?", f"I have two: {right} and {wrong}. Which one should get it?"])
        return mk(style(rng, ask), now, w, [[Call("lookup_contact", name=first)], Final(q)],
                  "standing 'just do it': two contacts fit the name, so the one question still gets asked and nothing is sent", history=hist, cards=[],
                  rules=[r_calls([("lookup_contact", {})]), r_none_of("send_message", "send_email"),
                         r_must(re.escape(right), re.escape(wrong), r"\?$"), r_sentences(2)])
    return make


def s10_scoped_clear(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([9, 12]))
        scope = ["today", "tomorrow"][i % 2]
        day = now.date() if scope == "today" else tomorrow(now)
        other = tomorrow(now) if scope == "today" else now.date() + D(days=3)
        gone = rng.sample(["Water the plants", "Call the bike shop", "Back up my laptop", "Buy stamps"], 2)
        w.reminders = [Reminder(t, at_(day, 18 + k)) for k, t in enumerate(gone)] + [Reminder("Pay phone bill", at_(other, 9))]
        hist = filler(rng, rng.choice([0, 1])) + [turn(pick(rng, STANDING_ASK), pick(rng, STANDING_ACK))] + filler(rng, rng.choice([0, 1]))
        ask = pick(rng, [f"clear {scope}'s reminders, no need to check with me", f"delete the reminders for {scope}, just do it"])
        final = fin(rng, f"Cleared the 2 reminders for {scope}.", f"Done, the 2 reminders for {scope} are cleared.")
        return mk(style(rng, ask), now, w, [[Call("clear_reminders", scope=scope)], Final(final)],
                  "standing 'just do it': the clear runs with exactly the scope the person gave, behind its card", history=hist,
                  approve=[True], cards=["clear_reminders"],
                  rules=[r_calls([("clear_reminders", {"scope": scope})]), r_pred("only the scope", lambda c: None if len(c.called("clear_reminders")) == 1 else "more than one clear")])
    return make


def s10_vague_clear(i):
    def make(rng):
        now, w = world(rng, hour=rng.choice([9, 12, 17]))
        w.reminders = [Reminder("Water the plants", at_(now.date(), 20)), Reminder("Call the bike shop", at_(tomorrow(now), 10)),
                       Reminder("Return library books", at_(now.date() - D(days=2), 9))]
        hist = filler(rng, rng.choice([0, 1, 2])) + [turn(pick(rng, STANDING_ASK), pick(rng, STANDING_ACK))] + filler(rng, rng.choice([0, 1]))
        ask = pick(rng, ["clean up my reminders", "clear out some of my reminders"])
        q = pick(rng, ["Which ones: today's, tomorrow's, the overdue ones, or all of them?", "Do you mean today's, the overdue ones, or everything?"])
        return mk(style(rng, ask), now, w, [Final(q)],
                  "standing 'just do it': a clear with no stated scope is still asked about, and nothing is deleted", history=hist, cards=[],
                  rules=[r_none_of("clear_reminders", "clear_past_reminders"), r_must(r"overdue", r"\?$"), r_sentences(1)])
    return make


# =============================================================================
# 9. standing preferences
# =============================================================================
from world import World, Reminder as _Reminder  # noqa: E402

ES_COND = {"Light rain": "lluvia ligera", "Overcast": "cielo nublado", "Partly cloudy": "nubes y claros",
           "Sunny": "cielo soleado", "Showers": "chubascos"}


def es_join(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " y " + items[-1]


def es_at(title, t):
    return f"{title} a {'la' if t.startswith('1 ') or t.startswith('1:') else 'las'} {t}"


class Read:
    kind = "read"

    def __init__(self):
        self.f = {}
        self.keep = ()
        self.calls = []
        self.asks = []

    def has(self, reg):
        return reg in self.f

    def form(self, reg):
        return self.f[reg]

    def prompt(self, rng, fig=False):
        if fig and getattr(self, "fig_asks", None):
            return pick(rng, self.fig_asks)
        return pick(rng, self.asks)

    def apply(self, w):
        pass

    @property
    def tool(self):
        return self.calls[0].name

    @property
    def targs(self):
        return self.calls[0].args


class AgendaRead(Read):
    """"what's lined up tomorrow": a time-scoped ask, so it reads the Brief's day plan (get_my_day_plan), and every
    form below states what that plan lists."""

    def __init__(self, rng, now, n):
        super().__init__()
        self.evs = make_events(rng, now, tomorrow(now), n)
        tw = World(now=now, events=self.evs)
        self.pack = brief_pack(rng, now, tomorrow(now)) if rng.random() < 0.4 else None
        if self.pack:
            attach_brief(tw, self.pack)
        self.items = plan_items(tw)
        assert self.items, "empty plan"
        prose = plan_prose(self.items)
        self.asks = ["what's lined up for tomorrow", "run me through tomorrow's schedule", "anything on my plate tomorrow", "walk me through tomorrow"]
        self.calls = [plan_call()]
        self.keep = [x.title for x in self.items]
        one = pick(rng, [f"Tomorrow you have {prose}.", f"You have {prose} tomorrow.", f"On tomorrow's plan: {prose}.",
                         f"Lined up for tomorrow: {prose}."])
        many = "events" if all(x.src == "calendar" for x in self.items) else "things"
        k = len(self.items)
        head = pick(rng, [f"You have {k} {many[:-1] if k == 1 else many} tomorrow", f"Tomorrow has {k} {many[:-1] if k == 1 else many}"])
        self.f = {"n": f"{head}: {prose}.", "p": one, "one": one,
                  "cas": pick(rng, [f"You've got {prose} tomorrow.", f"Looks like you've got {prose} tomorrow.", f"Tomorrow you've got {prose}."]),
                  "bul": "\n".join(f"- {x.label}: {x.when}" for x in self.items)}
        if all(x.src == "calendar" and x.t for x in self.items):      # the Spanish form is written for events only
            self.f["es"] = f"Mañana tienes {es_join(es_at(x.title, x.t) for x in self.items)}."

    def apply(self, w):
        w.events = list(self.evs)
        if self.pack:
            attach_brief(w, self.pack)


class ReminderRead(Read):
    TITLES = ["Return library books", "Pay phone bill", "Buy groceries", "Call the bike shop", "Renew bus pass", "Water the plants",
              "Back up my laptop", "Print the boarding pass"]

    def __init__(self, rng, now, n):
        super().__init__()
        titles = rng.sample(self.TITLES, n)
        hours = sorted(rng.sample(range(8, 20), n))
        self.items = [(t, at_(tomorrow(now), h, rng.choice([0, 30]))) for t, h in zip(titles, hours)]
        prose = join(f"{t} at {clock(d)}" for t, d in self.items)
        self.asks = ["which reminders land on tomorrow", "list tomorrow's reminders for me", "what reminders are due tomorrow"]
        self.calls = [Call("search_reminders", scope="tomorrow")]
        self.keep = titles
        p = pick(rng, [f"Tomorrow you have these reminders: {prose}.", f"You have these reminders tomorrow: {prose}.",
                       f"Reminders for tomorrow: {prose}."])
        self.f = {"p": p, "one": p, "cas": pick(rng, [f"You've got these reminders tomorrow: {prose}.",
                                                       f"Looks like you've got these reminders lined up: {prose}."]),
                  "es": f"Mañana tienes estos recordatorios: {es_join(es_at(t, clock(d)) for t, d in self.items)}.",
                  "bul": "\n".join(f"- {t}: {clock(d)}" for t, d in self.items)}
        self.f["n"] = (f"You have {n} reminders tomorrow:\n" + "\n".join(f"- {clock(d)}: {t}" for t, d in self.items)) if n >= 4 else p

    def apply(self, w):
        w.reminders = [_Reminder(t, d) for t, d in self.items]


class WeatherRead(Read):
    def __init__(self, rng, now):
        super().__init__()
        from gen import make_weather
        self.wx = make_weather(now, rng)
        d = self.wx["days"][1]
        rain = f", {d[4]}% chance of rain" if d[4] else ", no rain expected"
        self.asks = ["forecast for tomorrow in portland", "will tomorrow be sunny or gloomy", "how is tomorrow shaping up outside"]
        self.calls = [Call("get_weather", location="Portland,OR", period="tomorrow")]
        noun = {"Light rain": "light rain", "Overcast": "overcast skies", "Partly cloudy": "partly cloudy skies",
                "Sunny": "sunny skies", "Showers": "showers"}[d[3]]
        self.fig_asks = ["what's the high and low tomorrow", "tomorrow's temperatures, high and low"]
        self.f = {"n": pick(rng, [f"Tomorrow in Portland: {d[3].lower()}, high {d[1]}°F and low {d[2]}°F{rain}.",
                                  f"Portland tomorrow: {d[3].lower()}, with a high of {d[1]}°F and a low of {d[2]}°F{rain}.",
                                  f"Expect {d[3].lower()} in Portland tomorrow, high {d[1]}°F and low {d[2]}°F{rain}."]),
                  "one": f"Expect {noun} tomorrow, with a high of {d[1]}°F and a low of {d[2]}°F.",
                  "cas": f"It's looking like {noun} tomorrow, with a high around {d[1]}°F and a low of {d[2]}°F.",
                  "es": f"Mañana habrá {ES_COND[d[3]]}, con una máxima de {d[1]}°F y una mínima de {d[2]}°F.",
                  "bul": "\n".join([f"- Tomorrow: {d[3].lower()}", f"- High {d[1]}°F, low {d[2]}°F"] + ([f"- {d[4]}% chance of rain"] if d[4] else [])),
                  "fig": f"High {d[1]}°F, low {d[2]}°F"}
        self.f["p"] = self.f["n"]

    def apply(self, w):
        w.weather["Portland,OR"] = self.wx


ZONES = {"Tokyo": "Asia/Tokyo", "London": "Europe/London", "Paris": "Europe/Paris", "Sydney": "Australia/Sydney",
         "Chicago": "America/Chicago", "Berlin": "Europe/Berlin", "Seoul": "Asia/Seoul", "New York": "America/New_York"}


def true_world_time(now, place):
    """The tool's answer with real daylight-saving rules (the shared World uses fixed offsets, wrong for part of the year)."""
    from zoneinfo import ZoneInfo
    here = now.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
    there = here.astimezone(ZoneInfo(ZONES[place]))
    off = int((there.utcoffset() - here.utcoffset()).total_seconds() // 3600)
    return f"In {place} it's {there:%-I:%M %p on %A, %B %-d} ({off}h ahead of you)."


class TimeRead(Read):
    PLACES = [("Tokyo", "Tokio"), ("London", "Londres"), ("Paris", "París"), ("Sydney", "Sídney"), ("Chicago", "Chicago"),
              ("Berlin", "Berlín"), ("Seoul", "Seúl"), ("New York", "Nueva York")]

    def __init__(self, rng, now):
        super().__init__()
        place, es_place = pick(rng, self.PLACES)
        res = true_world_time(now, place)
        self.place, self.res = place, res
        m = re.search(r"it's (\d{1,2}:\d\d [AP]M) on ([A-Za-z]+),.*\((\d+)h ahead", res)
        t, h = m[1], m[3]
        self.asks = [f"current time in {place.lower()}", f"what time is it in {place.lower()} right now", f"what's the time over in {place.lower()}"]
        self.calls = [Call("world_time", place=place)]
        n = f"It's {t} in {place}, which is {h} hours ahead of you."
        self.f = {"n": n, "p": n, "one": n, "cas": f"It's {t} over in {place}, so {h} hours ahead of you.",
                  "es": f"En {es_place} son las {t}, {h} horas por delante de ti.",
                  "bul": f"- {place}: {t}\n- {h} hours ahead of you", "fig": t}
        self.keep = (place,)

    def apply(self, w):
        w.overrides["world_time"] = lambda args: true_world_time(w.now, args.get("place") or self.place)


class ConvertRead(Read):
    DATA = [(50, "F", "C", "50°F", "50°F", "Celsius", ["50 degrees fahrenheit is how many celsius", "express 50F in celsius"]),
            (68, "F", "C", "68°F", "68°F", "Celsius", ["68 fahrenheit to celsius, what is it", "68F in C?"]),
            (86, "F", "C", "86°F", "86°F", "Celsius", ["how many celsius is 86 degrees fahrenheit", "86 degrees fahrenheit expressed in celsius"]),
            (25, "C", "F", "25°C", "25°C", "Fahrenheit", ["25 degrees celsius is what in fahrenheit", "give me 25C in fahrenheit"]),
            (4, "GB", "MB", "4 GB", "4 GB", "megabytes", ["4 gigabytes expressed in megabytes", "how many MB is 4 GB"]),
            (20, "in", "cm", "20 inches", "20 pulgadas", "centímetros", ["20 inches expressed in centimeters", "how many cm is 20 inches"]),
            (5, "ft", "m", "5 feet", "5 pies", "metros", ["5 feet, how many meters", "convert 5 ft to metres"])]

    def __init__(self, rng, now):
        super().__init__()
        v, f, t, v_en, v_es, to_es, asks = pick(rng, self.DATA)
        res = World(now=now).handle("convert_units", {"value": v, "from_unit": f, "to_unit": t})
        out = res.split(" = ")[1]
        self.asks = list(asks)
        self.calls = [Call("convert_units", value=v, from_unit=f, to_unit=t)]
        n = f"{v_en} is {out}."
        self.f = {"n": n, "p": n, "one": n, "cas": f"{v_en}? That's {out}.", "es": f"Los {v_es} son {out} en {to_es}.",
                  "bul": f"- {v_en} = {out}", "fig": out}


class CalcRead(Read):
    def __init__(self, rng, now):
        super().__init__()
        pct, amt = pick(rng, [(18, "64.5"), (20, "45"), (15, "80"), (18, "52"), (12, "90")])
        out = World(now=now).handle("calculate", {"expression": f"{pct}% of {amt}"})
        self.asks = [f"work out {pct} percent of {amt}", f"{pct}% of {amt}, quick"]
        self.calls = [Call("calculate", expression=f"{pct}% of {amt}")]
        n = f"{pct}% of {amt} is {out}."
        self.f = {"n": n, "p": n, "one": n, "cas": f"That's {out}.", "es": f"El {pct}% de {amt} es {out}.",
                  "bul": f"- {pct}% of {amt} = {out}", "fig": out}


class UnreadRead(Read):
    def __init__(self, rng, now):
        super().__init__()
        k = rng.choice([2, 3])
        self.mails = mail(rng, now, k)
        who = join(m.sender for m in self.mails)
        self.asks = ["who has written me that i haven't opened", "what's sitting unread in my mail", "unread mail check"]
        self.fig_asks = ["unread email count please", "just how many unread emails are waiting"]
        self.calls = [Call("view_emails", unread=True, count=20)]
        n = f"You have {k} unread emails, from {who}."
        self.f = {"n": n, "p": n, "one": n, "cas": f"You've got {k} unread emails, from {who}.",
                  "es": f"Tienes {k} correos sin leer en tu bandeja de entrada, de {es_join(m.sender for m in self.mails)}.",
                  "bul": "\n".join(f"- {m.sender}: {m.subject}" for m in self.mails), "fig": str(k)}
        self.keep = [m.sender for m in self.mails] + [m.subject for m in self.mails]

    def apply(self, w):
        w.emails = list(self.mails)


def new_read(rng, now, kind, n=None):
    return {"agenda": lambda: AgendaRead(rng, now, n or rng.choice([2, 3])), "reminders": lambda: ReminderRead(rng, now, n or rng.choice([2, 3])),
            "weather": lambda: WeatherRead(rng, now), "time": lambda: TimeRead(rng, now), "convert": lambda: ConvertRead(rng, now),
            "calc": lambda: CalcRead(rng, now), "unread": lambda: UnreadRead(rng, now)}[kind]()


# ---- the preferences -----------------------------------------------------------------------------------
def _chk_one(t, keep=(), ack=False):
    return sentences(t) == 1 and "\n" not in t.strip() and not has_bullets(t)


def _chk_short(t, keep=(), ack=False):
    return words(t) < 20


def _chk_noq(t, keep=(), ack=False):
    return "?" not in t


def _chk_nobul(t, keep=(), ack=False):
    return not has_bullets(t)


def _chk_bullets(t, keep=(), ack=False):
    return all_bullets(t)


def _chk_casual(t, keep=(), ack=False):
    return bool(CONTRACTION.search(t)) and not FORMAL.search(t)


def _chk_spanish(t, keep=(), ack=False):
    return is_spanish(t, keep)


def _chk_fig(t, keep=(), ack=False):
    return words(t) <= 4 and (ack or bool(re.search(r"\d", t)))


class Pref:
    def __init__(self, key, asks, acks, check, reg, released=None):
        self.key, self.asks, self.acks, self.check, self.reg, self.released = key, asks, acks, check, reg, released


PREFS = {
    "one": Pref("one", ["from now on keep answers to one sentence", "going forward, answer in a single sentence please",
                        "keep every reply to one sentence from here on",
                        "you're a bit wordy for me, one sentence per answer from now on", "just give me the one-line version of everything, please",
                        "single sentences only, thanks", "can you squeeze every answer into a sentence from now on"],
                ["Got it, one sentence from here.", "Understood, single sentences from now on.", "Okay, I'll keep every answer to one sentence.",
                 "Fair enough, one line each time.", "Sure, I'll compress everything into a sentence."],
                _chk_one, "one", lambda t: sentences(t) >= 2),
    "short": Pref("short", ["keep your answers under 20 words from now on", "from here on, short replies only, under twenty words",
                            "please keep it brief, twenty words max, from now on",
                          "just the headline from now on, under 20 words", "less chat, more brevity: stay under twenty words",
                          "your answers run long, cut them to twenty words or less"],
                  ["Sure, I'll keep answers under twenty words.", "Okay, short replies from here.", "Got it, brief answers only.",
                   "Understood, I'll trim everything down."],
                  _chk_short, "one", lambda t: words(t) > 20),
    "noq": Pref("noq", ["stop ending your replies with questions", "from now on don't finish with a question, just answer",
                        "no more follow-up questions at the end please",
                        "don't quiz me at the end of your answers", "you can skip the question at the bottom of each reply",
                        "just answer, no asking me things back"],
                ["Understood, I'll end with the answer and no question.", "Okay, no more questions at the end of my replies.",
                 "Sure, I'll stop at the answer.", "Got it, answers only, nothing back to you."],
                _chk_noq, "n", None),
    "nobul": Pref("nobul", ["no bullet points please, just talk to me in sentences", "from now on don't use lists or bullets",
                            "please stop with the bullet lists, plain sentences",
                            "lists are hard for me to read, use normal sentences", "no lists, just flowing sentences from now on"],
                  ["Sure, plain sentences from now on.", "Understood, no bullet points from here.", "Okay, I'll write it all out in sentences.",
                   "Got it, no more lists."], _chk_nobul, "p", None),
    "bullets": Pref("bullets", ["give me everything as bullet points from now on", "from here on, bullets only please",
                                "answer in bullet points going forward",
                                "put your answers in a list from now on", "bullets please, every time", "i skim a lot, so list everything for me"],
                    ["- Got it.\n- Bullet points from here on.", "- Sure, bullets from now on.", "- Okay.\n- Lists from here on.",
                     "- Understood.\n- Everything as bullets."], _chk_bullets, "bul",
                    lambda t: not all_bullets(t)),
    "casual": Pref("casual", ["talk to me like a friend, more casual", "from now on be more casual and relaxed with me",
                              "loosen up a bit, talk like we're friends",
                              "you sound stiff, be less formal with me", "chill out a little and talk to me like a buddy"],
                   ["Sure thing, I'll keep it casual from here.", "You got it, let's keep this relaxed.", "Okay, I'll talk like a friend would.",
                    "Haha, sure, I'll loosen up."],
                   _chk_casual, "cas", None),
    "spanish": Pref("spanish", ["answer in Spanish from now on", "respóndeme en español de ahora en adelante",
                                "from here on reply to me in spanish please",
                                "habla conmigo en español a partir de ahora", "switch to spanish for all your answers", "quiero que me contestes en español"],
                    ["Claro, de ahora en adelante te respondo en español.", "Por supuesto, seguiré hablando en español con todo lo que me preguntes.",
                     "Entendido, a partir de ahora te respondo todo en español.", "De acuerdo, a partir de ahora te respondo en español.", "Listo, a partir de aquí te hablo en español y te respondo todo así."],
                    _chk_spanish, "es", lambda t: not is_spanish(t)),
    "fig": Pref("fig", ["when i ask for a number, just give me the number", "from now on just give me the figures, no explanation",
                        "numbers only please, no explanations from now on",
                        "if i ask for a figure, say only the figure", "figures only from here on, skip the explanation"],
                ["Got it, numbers only.", "Okay, just the figures.", "Sure, only the numbers.", "Understood, figures only."], _chk_fig, "fig", lambda t: words(t) > 4),
}
LIFT_ASK = ["ok you can go back to normal", "you can talk normally again", "that's enough of that, back to your usual style",
            "ok normal answers are fine again", "you can drop that rule now", "forget that, answer however you normally would",
            "never mind the style thing, go back to how you were"]
LIFT_ACK = ["Sure, back to my usual way of answering.", "Okay, normal answers again.", "Sounds good, I'll go back to my usual style.",
            "Alright, the usual from here.", "Done, I'll answer the way I normally do."]


def _prefs_check():
    for p in PREFS.values():
        for a in p.acks:
            assert p.check(a, (), True), (p.key, a)
            assert voice(a) is None, (p.key, a)
    for a in LIFT_ACK:
        assert voice(a) is None


_prefs_check()


def reg_ok(pref, topic):
    """Does `topic` have the form this preference asks for?"""
    r = pref.reg
    if r == "p":
        return topic.has("p") or topic.has("n")
    if r == "one" and pref.key == "short":
        return topic.has("one") and words(topic.form("one")) < 20
    return topic.has(r)


def form_for(pref, topic):
    r = pref.reg
    if r == "p":
        return topic.form("p") if topic.has("p") else topic.form("n")
    return topic.form(r)


def exchange(rng, topic, text, *, fig=False):
    user = style(rng, topic.prompt(rng, fig))
    if topic.kind == "read":
        return turn(user, text, topic.tool, [{"name": topic.tool, "args": topic.targs, "result": ""}])
    return turn(user, text)


def default_text(rng, topic, *, ask_q=None):
    if topic.kind == "chat":
        q = rng.random() < 0.5 if ask_q is None else ask_q
        return topic.form("q" if q else "n")
    return topic.form("n")


def topics_for(rng, now, prefs, n, *, want_read=None, big=False):
    """n distinct topics that every preference in `prefs` can express."""
    out, tries = [], 0
    kinds = ["agenda", "reminders", "weather", "time", "convert", "calc", "unread"]
    chats = list(FACTS)
    rng.shuffle(chats)
    while len(out) < n and tries < 80:
        tries += 1
        use_read = (rng.random() < 0.5) if want_read is None else (want_read if not out else rng.random() < 0.5)
        if use_read:
            kind = pick(rng, ["agenda", "reminders"] if (big and not out) else kinds)
            if any(getattr(t, "tag", None) == kind for t in out):
                continue
            t = new_read(rng, now, kind, n=3 if big and not out else None)
            t.tag = kind
        else:
            if not chats:
                continue
            t = chats.pop()
            t.tag = "chat"
        if all(reg_ok(PREFS[k], t) for k in prefs):
            out.append(t)
    return out


def s9_pref(i, pref_key, mode="plain", then=None):
    """plain: the wish holds to the end. lift: it is lifted ('back to normal'). change: replaced by `then`."""
    def make(rng):
        now, w = world(rng, hour=rng.choice([9, 11, 14, 17, 20]))
        pa = PREFS[pref_key]
        pb = PREFS[then] if then else None
        latest = pb or pa
        big = pref_key == "nobul" and mode == "plain"
        k0 = rng.choice([0, 1])
        # plain: about a third of the sessions put the wish five or six exchanges back; lift/change keep it nearer
        k1 = rng.choice([1, 2, 3, 3, 4, 5, 6, 6]) if mode == "plain" else rng.choice([1, 2, 3, 4])
        k2 = rng.choice([0, 1, 2]) if mode != "plain" else 0
        need = k0 + k1 + k2 + 1                       # earlier exchanges besides the instruction(s)
        prefs_need = [pref_key] + ([then] if then else [])
        want_read = True if big else (False if (mode == "lift" and pref_key in ("one", "short")) else None)
        topics = topics_for(rng, now, prefs_need, need + 1, want_read=want_read, big=big)
        if len(topics) < need + 1:
            return None
        final_t, hist_ts = topics[0], topics[1:]
        fig_a = pref_key == "fig"
        hist, spec = [], []        # spec: (pref or None, text, is_ack, topic)
        for t in hist_ts[:k0]:
            txt = default_text(rng, t)
            hist.append(exchange(rng, t, txt))
        ask_a = style(rng, pick(rng, pa.asks)) if pref_key != "spanish" or rng.random() < 0.5 else pick(rng, pa.asks)
        ack_a = pick(rng, pa.acks)
        hist.append(turn(ask_a, ack_a))
        spec.append((pref_key, ack_a, True, None))
        for t in hist_ts[k0:k0 + k1]:
            txt = form_for(pa, t)
            hist.append(exchange(rng, t, txt, fig=fig_a))
            spec.append((pref_key, txt, False, t))
        rest = hist_ts[k0 + k1:]
        if mode == "lift":
            hist.append(turn(pick(rng, LIFT_ASK), pick(rng, LIFT_ACK)))
            spec.append((None, hist[-1]["assistant"], True, None))
            for t in rest:
                txt = default_text(rng, t)
                hist.append(exchange(rng, t, txt))
                spec.append((None, txt, False, t))
            ask_final_reg = "q" if (final_t.kind == "chat" and (pref_key == "noq" or rng.random() < 0.5)) else "n"
            final_txt = final_t.form(ask_final_reg) if final_t.has(ask_final_reg) else final_t.form("n")
            final_pref = None
        elif mode == "change":
            ask_b = "actually, " + pick(rng, pb.asks)
            ack_b = pick(rng, pb.acks)
            hist.append(turn(ask_b, ack_b))
            spec.append((then, ack_b, True, None))
            for t in rest:
                txt = form_for(pb, t)
                hist.append(exchange(rng, t, txt, fig=then == "fig"))
                spec.append((then, txt, False, t))
            final_txt = form_for(pb, final_t)
            final_pref = then
        else:
            final_txt = form_for(pa, final_t)
            final_pref = pref_key
        if not (2 <= len(hist) <= 8):
            return None
        fig_f = latest.key == "fig" and mode != "lift"
        ask = style(rng, final_t.prompt(rng, fig_f))
        keep = []
        for t in topics:
            keep += list(getattr(t, "keep", ()) or [])
        steps = ([final_t.calls] if final_t.kind == "read" else []) + [Final(final_txt)]
        if final_t.kind == "read":
            final_t.apply(w)
        notes = {"plain": f"standing wish ({pref_key}): the wish from {len(hist) - k0 - 0} turns back still shapes the answer",
                 "lift": f"standing wish ({pref_key}) lifted: the answer goes back to the normal form",
                 "change": f"standing wish changed ({pref_key} -> {then}): the latest one wins"}[mode]

        def timeline(c):
            for pk, txt, is_ack, t in spec:
                kp = tuple(keep)
                if pk is not None:
                    if not PREFS[pk].check(txt, kp, is_ack):
                        return f"history turn breaks {pk}: {txt[:80]!r}"
            if final_pref is not None:
                if not PREFS[final_pref].check(c.final, tuple(keep), False):
                    return f"final breaks {final_pref}: {c.final[:80]!r}"
            else:
                if pa.released and not pa.released(c.final):
                    return f"final still follows the lifted wish: {c.final[:80]!r}"
                if pref_key == "noq" and final_t.kind == "chat" and not c.final.rstrip().endswith("?"):
                    return "lifted no-questions wish, but the answer asks nothing"
            return None
        rules = [("timeline", timeline)]
        if final_t.kind == "read":
            rules.append(r_calls([(final_t.tool, {})]))
        sc = mk(ask, now, w, steps, notes, rules=rules, history=hist, keep=keep)
        return sc
    return make


YN_ASKS = ["for yes or no questions, start with yes or no", "when i ask a yes or no question, lead with yes or no",
           "if it's a yes or no question, say yes or no first",
           "for anything that's a yes or no question, begin with the yes or no", "yes or no first when i ask that kind of question"]
YN_ACKS = ["Understood, I'll lead with Yes or No.", "Sure, yes or no first from here on.", "Okay, the yes or no comes first."]
YN_CHAT = [("is a whale a mammal", "Yes, whales breathe air, nurse their young and are warm-blooded."),
           ("is the sun a star", "Yes, it is a medium-sized star at the center of our solar system."),
           ("is sound faster in water than in air", "Yes, it travels about four times faster in water."),
           ("is a spider an insect", "No, spiders have eight legs and two body segments, while insects have six legs and three."),
           ("is mount everest in the alps", "No, it is in the Himalayas, on the border between Nepal and China."),
           ("do bats lay eggs", "No, bats are mammals and give birth to live young."),
           ("is glass a liquid", "No, glass is an amorphous solid, and the idea that it slowly flows is a myth.")]


def s9_yn(i):
    """Standing 'yes or no first': yes/no questions earlier lead with Yes/No; the last one reads a tool and does too."""
    def make(rng):
        kind = ["before", "text", "unread", "rain"][i % 4]
        yes = (i // 4) % 2 == 0
        now, w = wx_world(rng, hour=rng.choice([9, 12, 20]))
        if kind == "before":
            evs = make_events(rng, now, tomorrow(now), rng.choice([2, 3]), lo=9, hi=19)
            w.events = evs
            maybe_brief(rng, w, now, tomorrow(now), sources=("mail",))
            timed = [x for x in plan_items(w) if x.t]
            if not timed:
                return None
            first = timed[0]
            limit_h = first.minutes // 60 + (rng.choice([1, 2]) if yes else -rng.choice([1, 2]))
            if limit_h < 8:
                return None
            ask = f"is anything on before {clk_word(limit_h)} tomorrow"
            call = plan_call()
            final = f"Yes, you have {first.prose}." if yes else f"No, your first {first.noun} tomorrow is {first.prose}."
            truth = lambda c: any(x.minutes < limit_h * 60 for x in parse_plan(c.res) if x.t)
        elif kind == "text":
            (full, fn), (ofull, ofn) = rng.sample(PEOPLE_NAMES, 2)
            text = pick(rng, TEXT_BANK)
            w.messages = [Msg(ofull, ofn, pick(rng, TEXT_BANK), now - D(hours=2))]
            if yes:
                w.messages.append(Msg(full, fn, text, now - D(hours=1, minutes=rng.randint(5, 40))))
            ask = f"has {fn.lower()} messaged me today"
            call = Call("view_messages", query=fn, period="today")
            final = f"Yes, {fn} wrote “{text}”." if yes else f"No, nothing from {fn} today."
            truth = lambda c: fn in c.res and "No messages" not in c.res
        elif kind == "unread":
            (s_, a_, subj), (s2, a2, subj2) = rng.sample(MAIL_SENDERS, 2)
            w.emails = [Email(s2, a2, subj2, now - D(hours=7), "Details are in the portal.", True, True)]
            if yes:
                w.emails.append(Email(s_, a_, subj, now - D(hours=3), "Details are in the portal.", True, True))
            key = MAIL_KEY[s_]
            ask = f"is there anything unread from {key}"
            call = Call("view_emails", query=key, unread=True)
            final = f"Yes, {s_} wrote “{subj}” and it's still unread." if yes else f"No, there's nothing unread from {key}."
            truth = lambda c: "From: " in c.res
        else:
            pct = rng.choice([70, 80, 90]) if yes else rng.choice([0, 10])
            set_rain(w, 1, rng.choice(["Light rain", "Showers"]) if yes else rng.choice(["Sunny", "Partly cloudy"]), pct)
            d = weather_day(w)
            ask = "is it going to rain tomorrow"
            call = Call("get_weather", location="Portland,OR", period="tomorrow")
            final = (f"Yes, {d[3].lower()} with {'an' if pct == 80 else 'a'} {pct}% chance of rain." if yes else
                     (f"No, only a {pct}% chance of rain." if pct else "No, no rain is expected."))
            truth = lambda c: pct >= 50
        chat = rng.sample(YN_CHAT, rng.choice([1, 2, 3]))
        hist = filler(rng, rng.choice([0, 1])) + [turn(style(rng, pick(rng, YN_ASKS)), pick(rng, YN_ACKS))]
        for q, a in chat:
            hist.append(turn(style(rng, q + "?"), a))
        if rng.random() < 0.4:
            f = rng.choice(FACTS)
            hist.append(turn(f.prompt(rng), f.form("n")))
        if not (2 <= len(hist) <= 8):
            return None

        def agrees(c):
            if not re.match(r"(Yes|No)\b", c.final):
                return "does not start with Yes or No"
            return None if c.final.startswith("Yes") == bool(truth(c)) else "yes/no disagrees with the result"
        return mk(style(rng, ask), now, w, [[call], Final(final)],
                  "standing 'yes or no first': earlier answers led with Yes/No and so does this tool-backed one", history=hist,
                  rules=[r_pred("yes/no first", agrees), r_calls([(call.name, {})])])
    return make




# =============================================================================
# LONG CHATS with a standing wish set in the FIRST exchange (round 4 review)
# =============================================================================
# The gate's persistent-preference cases put the wish in exchange 1 and ask 7-10 exchanges later, behind replies of
# 40-150 words. The earlier replies obey the wish (production's own earlier answers did), so these histories hold
# long replies that are still one sentence, bullets, plain prose, casual, Spanish or question-free. The long replies
# come from the base-length bank in scen_r4_session_recall (LONGX) and are rewritten per wish, then checked by the
# wish's own predicate.
import scen_r4_session_recall as SR   # noqa: E402

ES_LONG = {   # LONGX question -> the same answer in Spanish (the person keeps asking in English)
    "how can i make my small apartment feel bigger":
        "La luz y las superficies despejadas hacen casi todo el trabajo. Abre las cortinas por completo durante el día y pon un espejo frente a la ventana para que rebote la luz por la sala. Elige muebles con patas, para que se vea el suelo por debajo, y arrima los más grandes a las paredes. Usa pocos colores, con tonos claros en las paredes, y aprovecha las estanterías altas para llevar la mirada hacia arriba. Por último, deja pocos objetos sobre las superficies, porque tres cosas bonitas se ven tranquilas y doce parecen desorden.",
    "how do i stop procrastinating on a big project":
        "Los proyectos grandes se atascan porque el primer paso no tiene forma, así que hazlo muy pequeño y muy concreto. En lugar de escribir el informe, abre el documento y anota tres ideas para la introducción. Después pon un temporizador de quince minutos y prométete hacer solo eso. Empezar es lo más difícil, y una vez dentro suele ser más fácil seguir. Decide con antelación cuándo y dónde vas a trabajar, deja el teléfono fuera de la vista y, al terminar, apunta el siguiente paso para que mañana arrancar sea sencillo.",
    "how do i choose a good watermelon":
        "Busca una que pese bastante para su tamaño, porque eso significa que tiene mucho jugo. Dale la vuelta y mira la mancha donde estuvo apoyada en el suelo: si es amarilla cremosa, está madura, y si es blanca o verde pálida, la cortaron pronto. La cáscara debe verse opaca y no brillante. Si le das unos golpecitos, una sandía madura suena hueca y grave, como un tambor. Evita las que tengan zonas blandas, cortes o golpes, y si compras media, busca una pulpa roja intensa sin vetas blancas.",
    "how long should i let a steak rest":
        "Déjalo reposar unos cinco a diez minutos si tiene más o menos un dedo de grosor, y un poco más si es un corte grueso o un asado. Mientras reposa, los jugos que se fueron al centro durante la cocción vuelven a repartirse por la carne. Si lo cortas enseguida, gran parte del líquido termina en la tabla y no en tu plato. Ponlo en un plato tibio, tapado sin apretar, y no lo envuelvas del todo, porque el vapor ablanda la costra. Retíralo unos grados antes de la temperatura que quieres.",
    "explain compound interest like i'm new to it":
        "El interés compuesto significa que ganas intereses sobre tus propios intereses, no solo sobre el dinero que pusiste al principio. Si metes mil dólares en una cuenta que paga un cinco por ciento anual, tendrás 1,050 al terminar el primer año. En el segundo año el cinco por ciento se aplica a 1,050 y no a 1,000, así que ganas un poco más. Al principio parece poca cosa, pero se acumula, y con muchos años el dinero crece bastante sin que agregues nada.",
    "can you explain how vaccines train the immune system":
        "Una vacuna le muestra a tu sistema inmune una parte inofensiva de un germen, o una imitación suya, para que practique antes de enfrentarse al verdadero. Tu cuerpo responde fabricando anticuerpos y células de memoria que se quedan durante meses o años. Si más adelante aparece el germen real, esas células lo reconocen enseguida y reaccionan en horas, en lugar de tardar días. Así que muchas veces no te enfermas, o la enfermedad es mucho más leve que sin vacuna.",
    "how do airplanes stay up":
        "El ala tiene una forma y una inclinación que empujan el aire hacia abajo mientras avanza. El aire responde empujando el ala hacia arriba, y esa fuerza es la sustentación. Además, la curva de la parte superior acelera el flujo y baja la presión sobre el ala, lo que ayuda todavía más. La sustentación aumenta con la velocidad, por eso el avión necesita correr por la pista antes de despegar. Una vez en el aire, los motores solo tienen que vencer la resistencia y las alas hacen el resto.",
    "why do we get goosebumps":
        "La piel de gallina es un reflejo heredado de nuestros antepasados peludos. Cuando sientes frío, unos músculos diminutos en la base de cada vello se contraen y lo levantan. En un animal con pelaje espeso eso atrapa una capa de aire caliente junto a la piel. Nosotros casi no tenemos pelo, así que solo quedan los bultitos. El mismo reflejo se activa con emociones fuertes, como oír una música preciosa o sentir miedo, porque el mismo sistema nervioso controla esos músculos.",
    "how do solar panels actually make electricity":
        "Cada panel está formado por celdas de silicio. Cuando la luz del sol llega a una celda, su energía suelta electrones de los átomos de silicio, y la celda tiene un campo eléctrico que empuja esos electrones en una sola dirección. Ese flujo es corriente continua. Después un inversor la convierte en la corriente alterna que usan los enchufes de la casa. Los paneles producen más al mediodía con cielo despejado y menos con nubes, y pierden un poco de rendimiento cada año.",
    "how does noise cancelling work":
        "Los audífonos con cancelación de ruido escuchan el sonido de alrededor con micrófonos pequeños y luego reproducen una imagen en espejo de ese sonido. Cuando una onda se encuentra con otra exactamente contraria, las dos se anulan, como al sumar más uno y menos uno. Funciona mejor con sonidos constantes y graves, como el zumbido de un avión o de un aire acondicionado. Los ruidos repentinos, como una voz, son más difíciles, por eso todavía oyes algo. Un buen ajuste alrededor de la oreja ayuda con los sonidos agudos.",
}
ONE_LONG = {   # LONGX question -> a single sentence of base-like substance
    "how do i stop procrastinating on a big project": "Make the first step so small and specific that you cannot argue with it, such as opening the document and writing three bullet points, then set a fifteen minute timer and let momentum do the rest.",
    "how can i make my small apartment feel bigger": "Let in as much light as you can with open curtains and a mirror across from the window, keep furniture on legs against the walls, stick to one or two light colors and clear the open surfaces.",
    "how do i choose a good watermelon": "Pick one that feels heavy for its size, with a creamy yellow patch where it sat on the ground, a dull rind and a deep hollow sound when you tap it.",
    "what's the best way to cool down a hot bedroom in summer": "Keep the sun out with closed blinds by day, then open windows at night with a box fan pulling cooler air through, and sleep in light cotton bedding after a cool shower.",
    "how do tides work": "Tides come mostly from the moon's gravity pulling the ocean into a bulge on the near side and another on the far side, so as the Earth turns, most coasts get two highs and two lows a day.",
    "is it worth buying a cast iron pan": "For most home cooks it is worth it, because a cast iron pan is cheap, holds heat evenly, goes from stove to oven and lasts decades if you keep it dry and lightly oiled.",
    "how do i get better at remembering what i read": "Close the book after each chapter and write down three things you remember in your own words, then check what you missed and repeat the exercise the next day.",
    "why does bread dough need to rest": "Resting lets the flour absorb the water and the gluten relax, so the dough becomes smoother and easier to shape while the flavor develops.",
    "how do airplanes stay up": "A wing is angled and shaped to push air downward as it moves forward, and the air pushes back up with a force called lift that grows with speed.",
    "how often should i water a succulent": "Water it thoroughly only when the soil is completely dry, which is usually every two weeks in summer and every three to four in winter.",
    "why do we get goosebumps": "Goosebumps are a leftover reflex from our furry ancestors, in which tiny muscles lift each hair to trap warm air, and they fire with strong emotions too.",
    "how long should i let a steak rest": "Let a steak an inch thick rest for five to ten minutes, loosely covered, so the juices spread back through the meat instead of running onto the board.",
    "explain what an index fund is": "An index fund holds a small slice of every company in a list such as the 500 largest, so you own many businesses at once for very low fees, though it still rises and falls with the market.",
    "how do you know when an avocado is ripe": "Press gently in your palm and look for slight give, then flick off the stem cap, where green underneath means ripe and brown means overripe.",
    "why do cats purr": "Cats purr when they are content but also when nervous or hurt, using rapid twitching of the voice box muscles, and the low vibration may even help them heal.",
    "how does noise cancelling work": "Small microphones listen to the sound around you and the headphones play back an opposite wave that cancels it, which works best on steady low hums like airplane engines.",
}


def _contract(t: str) -> str:
    pairs = [("do not", "don't"), ("does not", "doesn't"), ("did not", "didn't"), ("cannot", "can't"), ("will not", "won't"),
             ("is not", "isn't"), ("are not", "aren't"), ("it is", "it's"), ("It is", "It's"), ("that is", "that's"), ("That is", "That's"),
             ("you are", "you're"), ("You are", "You're"), ("there is", "there's"), ("There is", "There's"), ("they are", "they're"),
             ("you will", "you'll"), ("You will", "You'll"), ("you would", "you'd"), ("would not", "wouldn't"), ("have not", "haven't"),
             ("let us", "let's"), ("Let us", "Let's")]
    for a, b in pairs:
        t = t.replace(a, b)
    return t


def _bulletize(t: str) -> str:
    ss = [s.strip() for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()][:5]
    return "\n".join("- " + s for s in ss)


def _long_pref_bank():
    """pref -> [(question, reply, topic, kw)] with every reply passing the wish's own check and the voice rules."""
    bank: dict[str, list] = {}
    base = SR.banks()["longx"]
    for key in ("noq", "nobul", "casual", "bullets", "spanish", "one"):
        pa, out = PREFS[key], []
        for u, a, t, kw in base:
            if key == "noq":
                r = a
            elif key == "nobul":
                r = a
            elif key == "casual":
                r = _contract(a)
            elif key == "bullets":
                r = _bulletize(a)
            elif key == "spanish":
                r = ES_LONG.get(u)
            else:
                r = ONE_LONG.get(u)
            if r and pa.check(r, (), False) and voice(r) is None and not _TOOL_NAME.search(r):
                out.append((u, r, t, kw))
        bank[key] = out
    return bank


_LONG_PREF: dict = {}


def long_pref_bank():
    if not _LONG_PREF:
        _LONG_PREF.update(_long_pref_bank())
    return _LONG_PREF


def s9_long(i, pref_key, final_kind):
    """The wish is in exchange 1 of 8-10; the last turn is general knowledge ('chat') or a tool read ('read')."""
    def make(rng):
        now, w = world(rng, hour=rng.choice([9, 11, 14, 17, 20]))
        pa = PREFS[pref_key]
        bank = long_pref_bank()[pref_key]
        n_after = rng.choice([7, 7, 8, 8, 9])
        if len(bank) < n_after:
            return None
        topics = topics_for(rng, now, [pref_key], 1, want_read=(final_kind == "read"))
        if not topics:
            return None
        final_t = topics[0]
        picks = rng.sample(bank, n_after)
        ask_a = style(rng, pick(rng, pa.asks)) if pref_key != "spanish" or rng.random() < 0.5 else pick(rng, pa.asks)
        ack_a = pick(rng, pa.acks)
        hist, spec = [turn(ask_a, ack_a)], [(pref_key, ack_a, True, None)]
        for u, r, t, kw in picks:
            hist.append(turn(style(rng, u), r))
            spec.append((pref_key, r, False, None))
        final_txt = form_for(pa, final_t)
        keep = list(getattr(final_t, "keep", ()) or [])
        ask = style(rng, final_t.prompt(rng, False))
        if ask.strip().lower() in {h["user"].strip().lower() for h in hist}:
            return None
        steps = ([final_t.calls] if final_t.kind == "read" else []) + [Final(final_txt)]
        if final_t.kind == "read":
            final_t.apply(w)

        def timeline(c):
            for pk, txt, is_ack, _t in spec:
                if not PREFS[pk].check(txt, tuple(keep), is_ack):
                    return f"history turn breaks {pk}: {txt[:80]!r}"
            if not pa.check(c.final, tuple(keep), False):
                return f"final breaks {pref_key}: {c.final[:80]!r}"
            return None
        rules = [("timeline", timeline), ("long", lambda c: None if (6500 >= sum(len(h["user"]) + len(h["assistant"]) for h in c.hist) >= (1200 if pref_key == "one" else 2500) and 8 <= len(c.hist) <= 10) else "not a long chat")]
        if final_t.kind == "read":
            rules.append(r_calls([(final_t.tool, {})]))
        notes = f"standing wish ({pref_key}) set in exchange 1 of a long chat ({len(hist)} exchanges of long replies), honoured on a {'tool read' if final_t.kind == 'read' else 'chat'} turn at the end"
        return mk(ask, now, w, steps, notes, rules=rules, history=hist, keep=keep)
    return make


LONG_PREF_PLAN = ([("noq", "chat"), ("noq", "read"), ("noq", "chat"), ("noq", "read"), ("noq", "chat")] +
                  [("nobul", "chat"), ("nobul", "read"), ("nobul", "chat"), ("nobul", "read"), ("nobul", "chat")] +
                  [("casual", "chat"), ("casual", "read"), ("casual", "chat"), ("casual", "read"), ("casual", "chat")] +
                  [("bullets", "chat"), ("bullets", "read"), ("bullets", "chat"), ("bullets", "read")] +
                  [("spanish", "chat"), ("spanish", "read"), ("spanish", "chat"), ("spanish", "read"), ("spanish", "chat")] +
                  [("one", "chat"), ("one", "read"), ("one", "chat"), ("one", "read"), ("one", "chat")])




def _interleave(items, key, rng):
    """Round-robin over the groups (largest first), so consecutive scenarios rarely share a kind. The step thoughts
    (thinking.py) come from a few templates per kind, and build.fix_adjacent can only re-phrase a thought that equals a
    neighbour's when the neighbours differ in kind."""
    groups: dict = {}
    for it in items:
        groups.setdefault(key(it), []).append(it)
    for g in groups.values():
        rng.shuffle(g)
    order = sorted(groups, key=lambda k: (-len(groups[k]), str(k)))
    out, i = [], 0
    while any(groups[k] for k in order):
        for k in order:
            if groups[k]:
                out.append(groups[k].pop())
        i += 1
    return out


# =============================================================================
# the family
# =============================================================================
PREF_PLAN = ([("one", "plain", None)] * 6 + [("short", "plain", None)] * 4 + [("noq", "plain", None)] * 5 +
             [("nobul", "plain", None)] * 6 + [("bullets", "plain", None)] * 5 + [("casual", "plain", None)] * 6 +
             [("spanish", "plain", None)] * 6 + [("fig", "plain", None)] * 4 +
             [(k, "lift", None) for k in ("one", "short", "noq", "nobul", "bullets", "casual", "spanish", "fig", "one")] +
             [("one", "change", "casual"), ("bullets", "change", "one"), ("casual", "change", "one"), ("spanish", "change", "casual"),
              ("noq", "change", "one"), ("nobul", "change", "bullets"), ("short", "change", "spanish"), ("casual", "change", "spanish"),
              ("one", "change", "bullets")])


def _fill(out, makers, n, rng):
    got = tries = 0
    while got < n and tries < n * 12:
        sc = _attempt(makers(tries), rng)
        tries += 1
        if sc is not None:
            out.append(sc)
            got += 1
    return got


def fam_instructions(rng):
    out = []
    plan = [(s1_agenda_one, 3), (s1_just_time, 2), (s1_under15, 2), (s1_one_word, 2), (s1_name_only, 2), (s1_number_convert, 1),
            (s1_number_calc, 1), (s1_count_unread, 1),
            (s2_before, 4), (s2_rain, 2), (s2_text_from, 2), (s2_unread_from, 2),
            (s3_bullets, 3), (s3_numbered_day, 2), (s3_steps, 2), (s3_no_list, 3), (s3_one_per_line, 2),
            (s4_no_price, 3), (s4_no_numbers, 3), (s4_events_only, 4), (s4_no_emoji, 2), (s4_no_location, 2), (s4_skip_part, 2),
            (s5_one_account, 3), (s5_draft, 3), (s5_calendar_only, 2), (s5_reminder_only, 2), (s5_move_one, 2), (s5_done_one, 2),
            (s5_channel_only, 2),
            (s6_exact_text, 5), (s6_send_draft, 2), (s6_exact_email, 2),
            (s7_sched_mail_reminder, 4), (s7_sched_weather, 2), (s7_mail_texts, 1), (s7_event_and_reminder, 2), (s7_text_and_reminder, 1),
            (s7_read_and_text, 2),
            (s8_reminder, 3), (s8_event, 4), (s8_person, 3), (s8_day, 2), (s8_recipient, 2),
            (s10_just_do_it, 3), (s10_ambiguous, 2), (s10_scoped_clear, 3), (s10_vague_clear, 2)]
    for maker, n in plan:
        _fill(out, maker, n, rng)
    _fill(out, lambda j: s6_exact_text(j + 40, deny=True), 1, rng)
    _fill(out, s9_yn, 4, rng)
    for k, (pk, mode, then) in enumerate(PREF_PLAN):
        got = 0
        for t in range(10):
            sc = _attempt(s9_pref(k * 10 + t, pk, mode, then), rng)
            if sc is not None:
                out.append(sc)
                got += 1
                break
    # long chats: the wish sits in exchange 1 of 8-10 exchanges of base-length replies
    for k, (pk, kind) in enumerate(LONG_PREF_PLAN):
        for t in range(12):
            sc = _attempt(s9_long(k * 12 + t, pk, kind), rng)
            if sc is not None:
                out.append(sc)
                break
    return _interleave(out, lambda sc: (re.split(r"[(:]", sc.notes)[0].strip()[:30], len(sc.steps)), rng)


FAMILIES_R4 = {FAMILY: fam_instructions}
