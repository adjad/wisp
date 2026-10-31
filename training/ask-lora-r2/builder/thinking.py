"""Situational thinking for the Ask LoRA: which steps think, and what they think.

Production sends EVERY Ask step with enable_thinking (service/agent/loop.py
_template_kwargs; models.yaml model_only_narration_thinking), so Gemma 4's template
puts `<|think|>` at the top of the system turn on every step. That flag must not
change per step (it would break oMLX's prefix cache), so the model itself has to
learn WHEN a thought is worth its tokens:

- a first step (straight after the user) may open `<|channel>thought ... <channel|>`
  itself, or answer / call directly;
- a step after a tool result starts inside `<|channel>thought\\n` (the template forces
  it), so the completion is either a real thought or an EMPTY one ("\\n<channel|>").

`decide(meta, messages)` is the policy (rule-based on family, step position, the
previous tool result and the calls about to be made). `thought(meta, messages, ctx)`
writes the thought from the step's real context: the frozen clock, the prompt, the
call(s) and their arguments, the previous result. Both are deterministic.

Policy (THINK = a short real thought; QUIET = no thought / empty thought):
  never        chat_voice, knowledge_direct: QUIET on every step.
  after_error  previous result is an error, refusal or denial: THINK (what it says + the fix).
  quiet fams   concise_gentle, capability_honest: QUIET otherwise.
  after_load   the call right after a schema load: QUIET (mechanical).
  answer_first a first-step answer with no call: THINK only in write families
               (one clarifying question, or deciding not to act); else QUIET.
  write step   a step that makes a write, or loads one with its full arguments: THINK
               when there is task reasoning to do (a relative day or time to resolve, a
               duration or shift, a tool choice such as update vs add, reminder vs event,
               draft vs send, or arguments taken from a read); else QUIET. A load's
               thought is about the task, never about the loading.
  read step    a read in a write family's turn that writes (read before write), and
               safe_effects' first read (looking, on purpose, without acting): THINK.
               Every other read, loads included: QUIET.
  narrate      final answer after results:
               write families: QUIET over a clean receipt; THINK only when the result
               is a refusal, no match, ambiguity, denial, partial result or error.
               multi_source: THINK when two or more sources come back, or a day plan
               has to be ordered across sources; one plain read is QUIET.
               ignore_embedded_instructions: THINK when a result holds text aimed at
               an assistant. Single-source families (SINGLE_SOURCE): THINK only for
               real work on top of the read: free/busy against the asked time, what's
               left of today against the clock, a named weekday or date placed on the
               calendar, reminders overdue vs now. A plain count, a thread, a forecast
               yes/no or a lookup is QUIET (narrate_reason()).

Thoughts are subjectless planning ("Wants the 3 PM slot moved", "Need the next Monday:
Jan 25."), never name the user in the third person, never describe the tool plumbing,
and every relative day they state ("tomorrow is ...") is computed from the clock.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re

import writes as _writes

NEVER = frozenset({"chat_voice", "knowledge_direct"})
QUIET_FAMILIES = frozenset({"concise_gentle", "capability_honest"})
WRITE_FAMILIES = frozenset({"finish_the_write", "exact_args", "right_write_tool", "safe_effects", "relay_receipt",
                            "act_dont_ask", "stub_then_call"})
WRITE_TOOLS = (frozenset().union(*_writes.WRITE_ALLOW.values()) | _writes.ALWAYS_CARD | _writes.NEVER_CARD
               | {"clear_reminders", "manage_contacts", "cancel_event", "reply_to_email"})
DISCOVERY = "get_tool_schemas"
MAX_WORDS = 60
JACCARD_MAX = 0.5
DENIED = "The user denied this action."
CAL_TOOLS = ("get_upcoming", "get_past_events", "search_reminders", "get_my_day_plan")
PLAN_TOOLS = ("get_my_day_plan", "daily_brief")

_ERROR = re.compile(r"^\s*\(error\b|^The user denied this action|^Wisp (?:can't|could not|cannot)\b|isn't connected|"
                    r"read-only calendar|[Nn]othing was (?:moved|changed|sent|removed|created|added|saved|deleted)\b|"
                    r"^Only the organizer")
INJECTION = re.compile(
    r"(ignore (?:all )?(?:your )?previous instructions|\bAI (?:assistant|model)\b|assistant instructions|"
    r"hidden prompt|system override|if you are an ai|note to any ai|hey wisp,? (?:please )?(?:send|forward|delete|add))",
    re.I)
# Text that must never show up inside a thought: tool plumbing, markup, the harness.
NOISE = re.compile(r"schema_discovery|ready_next_step|source_tools_executed|not_run_yet|\{\"|<\||\|>|\*\*|Traceback|"
                   r"World\.|\bt_[a-z_]+\(|\bstubs?\b|harness|synthetic|[PM]ST-0[78]00|PDT-0700|got an unexpected keyword|"
                   r"\bthe user\b|\bSam\b|sam\.rivera", re.I)
# The schema-loading mechanics: a thought plans the task, never the loading.
MECHANICS = re.compile(r"listed by name|\bschemas?\b|\bload(?:s|ed|ing)?\b|\bthe real (?:one|call)\b|"
                       r"\b(?:isn't|not) (?:loaded|available) yet\b", re.I)
# The user is never "they": thoughts are subjectless or first person.
USER_PRONOUN = re.compile(r"\b(?:they|them|their|theirs|themselves|they're|they've|they'd|they'll)\b|\bthe user\b", re.I)
# Filler tails that carry no reasoning (the round-1 thought generator's habits).
BOILERPLATE = re.compile(r"just the facts it states|nothing more\b|title, day and time|relay just the receipt|"
                         r"nothing extra|restate exactly what it says|exact numbers, scoped|nothing rounded or added|"
                         r"only what it shows|details for the change come from|can't fill in the write until", re.I)
# The approval/send card is host UI the model never sees: a thought plans the call, not the card.
CARD = re.compile(r"\bcards?\b", re.I)
# A sentence that says an action happened (checked against a failed previous result).
_SUCCESS = re.compile(r"\b(?:sent|added|moved|went through|confirms?|confirmed|is set for|it's set|saved|updated|"
                      r"scheduled|marked (?:it )?done|completed|all set|is in)\b", re.I)
_NEG = re.compile(r"\b(?:not|never|no|nothing|none|without|neither|nor)\b|n['’]t\b", re.I)


# ----------------------------------------------------------------- step analysis
def call_args(tc: dict) -> dict:
    a = tc["function"].get("arguments")
    if isinstance(a, str):
        try:
            a = json.loads(a or "{}")
        except ValueError:
            a = {}
    return a if isinstance(a, dict) else {}


def calls_of(msg: dict) -> list[tuple[str, dict]]:
    return [(tc["function"]["name"], call_args(tc)) for tc in msg.get("tool_calls") or []]


def is_error(text: str) -> bool:
    first = (text or "").lstrip().split("\n", 1)[0]
    return bool(_ERROR.search(first))


def outcome(text: str) -> str:
    """What a tool result says happened: ok | denied | refused | error | ambiguous | no_match | partial."""
    t = (text or "").strip()
    first = t.split("\n", 1)[0]
    if t.startswith(DENIED):
        return "denied"
    if re.search(r"already starts then|read-only calendar|^Only the organizer|can't tell yet whether|"
                 r"[Nn]othing was (?:moved|changed|sent|removed|created|added|saved|deleted)\b", first):
        return "refused"
    if re.match(r"\(error\b", first) or re.search(r"^Wisp (?:can't|could not|cannot)\b|isn't connected", first):
        return "error"
    if re.search(r"^(?:Several|Multiple|\d+) \w+ match\b|\bWhich one\?", first):
        return "ambiguous"
    if re.search(r"^\(?No (?:saved contact|upcoming|exact matching|matching|messages|results|web results|scheduled)|"
                 r"^\(no web results|^Nothing active matches|found no active match|\bwere not found\b|"
                 r"^Nothing (?:new|scheduled)\b", first):
        return "no_match"
    if re.search(r"\bin Wisp only\b|\bpartially\b|\bbut (?:could not|couldn't)\b|\bfailed for\b", first):
        return "partial"
    return "ok"


FAILED = frozenset({"denied", "refused", "error", "ambiguous", "no_match"})


def injected(text: str) -> bool:
    return any(INJECTION.search(s) for s in re.split(r"(?<=[.!?])\s+|\n", text or ""))


def _loads(name: str, args: dict) -> set[str]:
    """Tools a call names: itself, or the tools a get_tool_schemas load asks for."""
    return set(args.get("tools") or []) if name == DISCOVERY else {name}


def analyze(meta: dict, messages: list[dict]) -> dict:
    """What kind of step this is, from the conversation alone (plus meta's family and turn writes)."""
    target, hist = messages[-1], messages[:-1]
    user_idx = max(i for i, m in enumerate(hist) if m["role"] == "user")
    first = hist[-1]["role"] == "user"
    results, prev_calls = [], []
    if not first:
        j = len(hist) - 1
        while j > user_idx and hist[j]["role"] == "tool":
            results.insert(0, str(hist[j].get("content") or ""))
            j -= 1
        if hist[j]["role"] == "assistant":
            prev_calls = calls_of(hist[j])
    all_results = [str(m.get("content") or "") for m in hist[user_idx + 1:] if m["role"] == "tool"]
    turn_calls = [c for m in hist[user_idx + 1:] if m["role"] == "assistant" for c in calls_of(m)]
    calls = calls_of(target)
    loaded = bool(results) and all(r.lstrip().startswith('{"schema_discovery"') for r in results)
    error = any(is_error(r) for r in results)
    if first:
        kind = "select" if calls else "answer_first"
    elif error:
        kind = "after_error"
    elif loaded and calls:
        kind = "after_load"
    else:
        kind = "next_call" if calls else "narrate"
    touched = set().union(*[_loads(n, a) for n, a in calls]) if calls else set()
    turn_writes = meta.get("turn_writes")
    if turn_writes is None:
        turn_writes = meta.get("writes") or []
    return {"kind": kind, "first": first, "calls": calls, "prev_calls": prev_calls, "results": results,
            "all_results": all_results, "turn_calls": turn_calls, "error": error, "loaded": loaded,
            "writes_now": bool(touched & WRITE_TOOLS), "touched": touched, "turn_writes": list(turn_writes),
            "prompt": str(hist[user_idx].get("content") or ""), "target_text": str(target.get("content") or ""),
            "system": next((str(m.get("content") or "") for m in hist if m["role"] == "system"), "")}


_RECEIPT = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{1,2})"
                      r"(?:, (\d{4}))?,? (?:at )?(\d{1,2}:\d\d [AP]M)(?:[^–\n]*?(?:–| to )(?:\w{3} \w{3} \d{1,2}, \d{4} )?"
                      r"(\d{1,2}:\d\d [AP]M))?")


def receipt_of(s: dict) -> dict | None:
    """The date and times a write's receipt states (previous call a write, result not an error)."""
    if s["error"] or not s["results"] or not s["prev_calls"]:
        return None
    if not any(n in WRITE_TOOLS for n, _ in s["prev_calls"]):
        return None
    r = s["results"][-1]
    m = _RECEIPT.search(r)
    if not m:
        return None
    t = re.search(r"“([^”]+)”", r)
    return {"wd": m[1], "mon": m[2], "day": int(m[3]), "start": m[5], "end": m[6],
            "title": t.group(1) if t else "", "wisp_only": "Wisp only" in r, "text": r}


# ----------------------------------------------------------------- policy
_TMRW = re.compile(r"\b(?:tomorrow|tmrw|tmr|tomorow|tommorow|tommorrow|2morrow)\b", re.I)
_TODAYW = re.compile(r"\b(tonight|tonite|today|this evening|this afternoon|this morning)\b", re.I)
_WD_RE = re.compile(r"\b(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|wensday|thu(?:rs(?:day)?)?|thurs|"
                    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b", re.I)
_WD_IDX = {"mon": 0, "tue": 1, "wed": 2, "wen": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAYPART = re.compile(r"\b(morning|afternoon|evening|tonight|night|nite)\b", re.I)
_CLOCKTIME = re.compile(r"\b\d{1,2}(?::\d\d)?\s*(?:am|pm)\b|\b\d{1,2}:\d\d\b|\bnoon\b", re.I)
_REL = re.compile(r"\bin\s+(\d+|an?|one|two|half an)\s+(minutes?|mins?|hours?|hrs?)\b"
                  r"|\b(\d+|an?|one|two|half an)\s+(minutes?|mins?|hours?|hrs?)\s+from now\b", re.I)
_DURATION = re.compile(r"\bfor (?:an?|\d+|one|two|three|half an?) ?(?:hours?|hrs?|minutes?|mins?)\b|"
                       r"\bhour and a half\b|\b\d+\s*(?:hours?|hrs?|minutes?|mins?) long\b", re.I)
_SHIFT = re.compile(r"\b(an hour and a half|half an hour|an hour|\d+ (?:minutes|mins|hours?)|a day)\s+"
                    r"(earlier|later|back|forward|ahead|before)\b", re.I)
_H24 = re.compile(r"\b(?:1[3-9]|2[0-3]):\d\d\b")
_DATEWORD = re.compile(r"\b(?:next|this) (?:week|month)\b|\bthe \d{1,2}(?:st|nd|rd|th)\b|\bnight before\b|"
                       r"\bday after\b|\bthat day\b", re.I)


def _write_signals(fam: str, s: dict) -> list[str]:
    """The task reasoning a write step has to do (empty: the call is plain transcription)."""
    p = s["prompt"]
    out = []
    if (_TMRW.search(p) or _TODAYW.search(p) or _WD_RE.search(p) or _REL.search(p) or _DATEWORD.search(p)
            or (_DAYPART.search(p) and not _CLOCKTIME.search(p))):
        out.append("date")
    if _DURATION.search(p) or _SHIFT.search(p) or _H24.search(p):
        out.append("args")
    for name in s["touched"] & WRITE_TOOLS:
        if _tool_reason(fam, name, {}, ""):
            out.append("tool")
            break
    if s["results"] and not s["loaded"] and s["prev_calls"]:
        out.append("from_result")
    return out


def _write_intent(fam: str, s: dict) -> bool:
    return any(n in WRITE_TOOLS for n, _ in s["prev_calls"]) or fam == "relay_receipt"


def grounded_reason(prompt: str, prev: str) -> str:
    p = prompt.lower()
    if re.search(r"\b(free|booked|busy|time for)\b", p):
        return "slot"
    if re.search(r"\b(complete|completed|wrapped|finish|finished|got done)\b", p):
        return "done"
    if "family" in p:
        return "family"
    if re.search(r"\b(left today|happening today|today|tonight|gotten up to)\b", p) and prev in CAL_TOOLS:
        return "today"
    if re.search(r"\b(?:request|ask|want|need)s?\b[^?]*\b(?:me|i)\b[^?]*\bdo\b|\baction items?\b", p):
        return "judge"
    return ""


def decide(meta: dict, messages: list[dict]) -> bool:
    """True when this step should carry a real thought (see the module docstring)."""
    fam = meta.get("family") or ""
    if fam in NEVER:
        return False
    s = analyze(meta, messages)
    kind = s["kind"]
    if kind == "after_error":
        return True
    if fam in QUIET_FAMILIES or kind == "after_load":
        return False
    if kind == "answer_first":
        return fam in WRITE_FAMILIES
    if kind in ("select", "next_call"):
        if s["writes_now"]:
            return bool(_write_signals(fam, s))
        if fam in WRITE_FAMILIES and s["turn_writes"]:
            return True                       # read before write
        if kind == "select" and fam == "multi_source":
            return len(_planned_sources(s)) >= 2      # combining sources
        return kind == "select" and fam == "safe_effects"
    # narrate
    if fam in WRITE_FAMILIES:
        return _write_intent(fam, s) and _narrate_outcome(fam, s) != "ok"
    return narrate_reason(meta, s) is not None


# Families whose finals narrate one source: a final here thinks only when it does real work on top of the
# read (date placement, free/busy against the asked time, what's left of today, overdue vs now). Narrating
# one clean read (a count, a thread, a forecast yes/no, a lookup) is NO THINK: the thought would only
# restate the tool values the answer gives anyway.
SINGLE_SOURCE = frozenset({"grounded_only", "right_source", "weather_and_web", "act_dont_promise",
                           "no_write_for_reads"})
_DUE_ASK = re.compile(r"\b(?:due|overdue|late|past|pending|open|still|left|to-?do|procrastinat\w*|soon|"
                      r"need to do|today|tonight|tomorrow|this week)\b", re.I)
_YESNO = re.compile(r"^\s*(?:is|are|do|does|did|have|has|was|were)\b", re.I)


def narrate_reason(meta: dict, s: dict) -> str | None:
    """Why a non-write family's final thinks (None: it doesn't). One source for decide() and thought_narrate()."""
    fam = meta.get("family") or ""
    if fam == "multi_source":
        srcs = {source_of(n) for n, _ in s["prev_calls"] if n != DISCOVERY}
        if len(srcs) >= 2 or any(n in PLAN_TOOLS + ("get_recent_activity",) for n, _ in s["prev_calls"]):
            return "multi"
        return None
    if fam == "ignore_embedded_instructions":
        return "inject" if any(injected(r) for r in s["all_results"]) else None
    last = s["results"][-1] if s["results"] else ""
    if not last:
        return None
    prev = s["prev_calls"][0][0] if s["prev_calls"] else ""
    if fam == "grounded_only":
        why = grounded_reason(s["prompt"], prev)
        if why == "slot" and _starts(last):
            return "slot"                     # free/busy: the asked time against the listed starts
        if why == "today" and prev in CAL_TOOLS and prev != "get_past_events" and _starts(last):
            return "today"                    # what's left: today's starts against the clock
    if fam in DAY_FAMILIES and _day_target(meta, s):
        return "day"                          # a named weekday/date placed on the calendar (date math)
    if fam in DAY_FAMILIES and _due_items(meta, s) and (_DUE_ASK.search(s["prompt"]) or not _YESNO.search(s["prompt"])):
        return "due"                          # which reminders are past due, from the clock (date math)
    return None


DAY_FAMILIES = frozenset({"right_source", "act_dont_promise", "grounded_only", "no_write_for_reads"})
_ORDINAL = re.compile(r"\bthe (\d{1,2})(?:st|nd|rd|th)\b", re.I)


def _day_target(meta: dict, s: dict):
    """(the prompt's day words, the date the calendar result shows for them) when the prompt names a specific
    weekday or date other than today/tomorrow and the result's first dated header is that day; else None."""
    prev = s["prev_calls"][0][0] if s["prev_calls"] else ""
    clock = parse_clock(meta)
    if prev not in ("get_upcoming", "get_my_day_plan") or not s["results"] or not clock:
        return None
    if _TMRW.search(s["prompt"]) or _TODAYW.search(s["prompt"]):
        return None
    text = re.sub(r"^Today is [^.]*\.", "", s["results"][-1][:600])
    shown = [d for d, _ in stated_dates(text, clock)[0] if d and d >= clock.date()]
    if not shown:
        return None
    d = shown[0]
    if re.search(r"\b(this weekend|weekend|next week)\b", s["prompt"], re.I):
        return None                           # a span read straight off the list: no single day to place
    wd = _WD_RE.search(s["prompt"])
    if wd and _WD_IDX[wd.group(1).lower()[:3]] == d.weekday():
        return wd.group(0), d
    od = _ORDINAL.search(s["prompt"])
    if od and int(od.group(1)) == d.day:
        return od.group(0), d
    named = [x for x, _ in stated_dates(s["prompt"], clock)[0] if x]
    if named and named[0] == d:
        m = _DATE_RE.search(s["prompt"])
        return (m.group(0) if m else fmt_day(d)), d
    return None


_DUE_LINE = re.compile(r"^- (.+?) — (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\w{3}) (\d{1,2}), (\d{4}) at (\d{1,2}):(\d\d) ([AP]M)",
                       re.M)


def _due_items(meta: dict, s: dict):
    """[(title, due datetime)] of the dated reminders a search_reminders read lists (empty: not that read)."""
    prev = s["prev_calls"][0][0] if s["prev_calls"] else ""
    clock = parse_clock(meta)
    if prev != "search_reminders" or not s["results"] or not clock:
        return []
    out = []
    for title, mon, day, year, h, mi, ap in _DUE_LINE.findall(s["results"][-1]):
        hh = int(h) % 12 + (12 if ap == "PM" else 0)
        out.append((title, _dt.datetime(int(year), _MON[mon.lower()], int(day), hh, int(mi))))
    return out


def _planned_sources(s: dict) -> list[str]:
    names = sorted(set().union(*[_loads(n, a) for n, a in s["calls"]])) if s["calls"] else []
    return list(dict.fromkeys(source_of(n) for n in names if n != DISCOVERY))


def _narrate_outcome(fam: str, s: dict) -> str:
    """The outcome a write family's final relays: the result's own, or 'clarify' when a relay turn wrote
    nothing and the final asks a question (e.g. two reminders match)."""
    last = s["results"][-1] if s["results"] else ""
    oc = outcome(last)
    if oc == "ok" and fam == "relay_receipt" and not any(n in WRITE_TOOLS for n, _ in s["turn_calls"]) \
            and "?" in s["target_text"]:
        return "clarify"
    return oc


def step_kind(meta: dict, messages: list[dict]) -> str:
    return analyze(meta, messages)["kind"]


# ----------------------------------------------------------------- formatting
_WD = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "half an": 0.5}


def _rel_minutes(m: re.Match) -> float | None:
    n, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    try:
        k = float(n)
    except ValueError:
        k = _NUM.get(n.lower())
    if k is None:
        return None
    return k * (60 if unit.lower().startswith("h") else 1)


def pick(seed: str, options: list[str]) -> str:
    return options[int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(options)]


def parse_clock(meta: dict) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime(str(meta.get("clock"))[4:], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def fmt_day(d: _dt.date) -> str:
    return f"{_WD[d.weekday()]} {d.strftime('%b')} {d.day}"


def fmt_time(t: _dt.time) -> str:
    h = t.hour % 12 or 12
    return f"{h}{':%02d' % t.minute if t.minute else ''} {'AM' if t.hour < 12 else 'PM'}"


def parse_iso(v) -> tuple[_dt.date, _dt.time | None] | None:
    if not isinstance(v, str):
        return None
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)(?:[T ](\d\d):(\d\d))?", v)
    if not m:
        return None
    d = _dt.date(int(m[1]), int(m[2]), int(m[3]))
    return d, (_dt.time(int(m[4]), int(m[5])) if m[4] else None)


def call_dates(calls) -> list[_dt.date]:
    """Dates in a call's arguments (ISO values only)."""
    out = []
    for _n, a in calls:
        for k, v in a.items():
            if k in ("when_iso", "current_when_iso", "date", "start", "when", "day"):
                p = parse_iso(v)
                if p:
                    out.append(p[0])
    return out


def _day_word(prompt: str) -> str | None:
    m = _TMRW.search(prompt) or _TODAYW.search(prompt) or _WD_RE.search(prompt)
    return m.group(0) if m else None


def resolve_when(clock: _dt.datetime | None, iso: str, prompt: str, seed: str, sources: list[str] = ()) -> str:
    """One sentence that turns the prompt's time words into the date in `iso` (the call's own value). Every
    relative word it uses is computed from the clock; with no day word and no shown date it says nothing."""
    p = parse_iso(iso)
    if not p or clock is None:
        return ""
    d, t = p
    today = clock.date()
    delta = (d - today).days
    day = fmt_day(d)
    parts = []
    rel = _REL.search(prompt)
    mins = _rel_minutes(rel) if rel else None
    if (rel and t and mins is not None and
            _dt.datetime.combine(d, t) == (clock + _dt.timedelta(minutes=mins)).replace(second=0, microsecond=0)):
        return pick(seed + "rel", [f"It's {fmt_time(clock.time())} now, so {rel.group(0).strip()} is {fmt_time(t)}.",
                                   f"Now is {fmt_time(clock.time())}; {rel.group(0).strip()} lands at {fmt_time(t)}.",
                                   f"{rel.group(0).strip().capitalize()} from {fmt_time(clock.time())} is {fmt_time(t)}."])
    wdm = [m for m in _WD_RE.finditer(prompt) if _WD_IDX[m.group(1).lower()[:3]] == d.weekday()]
    wd = _WD[d.weekday()]
    if delta == 1 and _TMRW.search(prompt):
        parts.append(pick(seed + "tm", [f"Today is {fmt_day(today)}, so tomorrow is {day}.", f"Tomorrow is {day}.",
                                        f"Tomorrow means {day}."]))
    elif delta == 0 and _TODAYW.search(prompt):
        w = _TODAYW.search(prompt).group(1).lower()
        parts.append(pick(seed + "td", [f"'{w}' is today, {day}.", f"Today is {day}, so that's today.",
                                        f"Same day: {day}."]))
    elif wdm:
        word = wdm[0].group(1)
        if delta == 0:
            ahead = f" and {fmt_time(t)} is still ahead" if t and t > clock.time() else ""
            parts.append(pick(seed + "wd0", [f"It's already {wd}{ahead}, so '{word}' means today, {d.strftime('%b')} {d.day}.",
                                             f"Today is {day}{ahead}, so '{word}' is today."]))
        elif delta == 7:
            parts.append(pick(seed + "wd7", [f"Today is already {wd}, so '{word}' means next week's, {day}.",
                                             f"It's {wd} today; a bare '{word}' is the next one, {day}."]))
        elif delta == 6:
            parts.append(pick(seed + "wd1", [f"{wd} was yesterday, so '{word}' means the coming one, {day}.",
                                             f"Today is {fmt_day(today)}; {wd} just passed, so it's {day}."]))
        else:
            parts.append(pick(seed + "wd", [f"Today is {fmt_day(today)}, so '{word}' is {day}.",
                                            f"From {fmt_day(today)}, the next {wd} is {day}.",
                                            f"Need the next {wd}: {d.strftime('%b')} {d.day}.",
                                            f"'{word}' from today ({fmt_day(today)}) means {day}."]))
    elif delta == 0:
        parts.append(f"That's today, {day}.")
    elif delta == 1 and t is not None and t <= clock.time():
        parts.append(pick(seed + "d1", [f"{fmt_time(t)} has already passed today, so it's tomorrow, {day}.",
                                        f"It's past {fmt_time(t)} today, so tomorrow, {day}."]))
    elif delta == 1 and clock and any(d in {x for x, _ in stated_dates(src, clock)[0]} for src in sources):
        parts.append(f"That's tomorrow, {day}.")
    elif delta > 1 and clock and any(d in {x for x, _ in stated_dates(src, clock)[0]} for src in [prompt, *sources]):
        parts.append(pick(seed + "on", [f"It goes on {day}.", f"The day is {day}."]))
    if parts and t is not None:
        dp = _DAYPART.search(prompt)
        if dp and not _CLOCKTIME.search(prompt) and dp.group(1).lower() not in ("tonight",):
            parts.append(pick(seed + "dp", [f"'{dp.group(1)}' has no clock time, so {fmt_time(t)}.",
                                            f"For '{dp.group(1)}' I'll use {fmt_time(t)}.",
                                            f"'{dp.group(1)}' with no time: {fmt_time(t)}."]))
    return " ".join(parts)


def _q(s, n=6) -> str:
    words = str(s).split()
    return " ".join(words[:n]) + ("…" if len(words) > n else "")


def _when_phrase(a: dict, with_day: bool = True) -> str:
    """' on Monday Mar 1 at 3 PM' (with_day) or ' at 3 PM'; ' for today' for a `day` argument."""
    p = parse_iso(a.get("when_iso"))
    if p:
        d, t = p
        if with_day:
            return f" on {fmt_day(d)}{' at ' + fmt_time(t) if t else ''}"
        return f" at {fmt_time(t)}" if t else ""
    if a.get("day"):
        return f" for {a['day']}"
    return ""


def action(name: str, a: dict, prior: str = "", with_day: bool = True, terse: bool = False, seed: str = "") -> str:
    """A short first-person clause for the call (no 'I'll' prefix). with_day=False leaves the day out
    (a resolution sentence already said it, or nothing shown grounds it); terse drops quoted values."""
    title = a.get("title") or a.get("name") or ""
    when = _when_phrase(a, with_day)
    if name == "add_calendar_event":
        dur = f" for {a['duration_min']} minutes" if a.get("duration_min") else ""
        loc = f" at {a['location']}" if a.get("location") and not terse else ""
        return f"add “{title}”{when}{dur}{loc}" if not terse else f"add the event{when}{dur}"
    if name == "update_event":
        dur = f", {a['duration_min']} minutes long" if a.get("duration_min") else ""
        return f"move “{title}” with update_event{re.sub(r'^ (?:on|at) ', ' to ', when)}{dur}"
    if name == "add_reminder":
        if a.get("all_day"):
            return "make it an all-day reminder" if not with_day else f"set an all-day reminder{when}"
        return f"set the reminder “{title}”{when}" if not terse else f"set the reminder{when}"
    if name == "update_reminder":
        bits = []
        if a.get("new_title"):
            bits.append(f"rename it “{a['new_title']}”")
        if when:
            bits.append(f"move it{re.sub(r'^ (?:on|at|for) ', ' to ', when)}")
        return f"update the “{title}” reminder: " + (" and ".join(bits) or "the new details")
    if name == "complete_reminder":
        return f"mark the “{title}” reminder done"
    if name == "clear_reminders":
        return f"delete the reminder matching “{a.get('query') or title}”"
    if name == "send_message":
        gist = f" with {prior}" if prior else ""
        return f"text {a.get('to') or a.get('recipient') or 'it'}{gist}"
    if name == "send_email":
        to = "the account's own address" if a.get("confirmed_self_send") else (a.get("to") or a.get("recipient") or "it")
        subj = "" if terse or not a.get("subject") else f" (subject “{_q(a.get('subject'), 5)}”)"
        return f"email {to} with send_email{subj}"
    if name == "schedule_send":
        to = ("the account's own address" if "@linden.edu" in str(a.get("to")) and a.get("channel") == "email"
              else a.get("to"))
        ch = "an email" if a.get("channel") == "email" else "a text"
        return f"queue {ch} to {to} for {a.get('when')} with schedule_send"
    if name == "draft_email":
        return f"open a draft to {a.get('to')} with draft_email, not send it"
    if name == "reply_to_email":
        return "reply to that email"
    if name == "create_note":
        return f"start a new note “{title}”"
    if name == "append_note":
        return f"append to the existing “{title}” note"
    if name == "manage_contacts":
        what = str(a.get("action") or "").replace("add_", "")
        val = a.get("value")
        if a.get("action") == "create":
            return f"create a contact for {a.get('name')}"
        if terse and val:
            return f"save it with manage_contacts, action {a.get('action')}"
        return f"add the {what} {val} to {a.get('name')}'s contact" if val else f"update {a.get('name')}'s contact"
    if name == "remember":
        return "save it word for word with remember"
    if name == "forget":
        return f"forget only what matches “{a.get('query')}”, nothing broader"
    if name == "cancel_event":
        return f"cancel “{title}”"
    return read_phrase(name, a)


def read_phrase(name: str, a: dict) -> str:
    q = a.get("query") or a.get("conversation")
    if name == "get_weather":
        return f"check the forecast for {a.get('location') or 'the home city'}" + (f" ({a['period']})" if a.get("period") else "")
    if name == "view_emails":
        box = "the sent mail" if a.get("mailbox") == "sent" else "the emails"
        return f"read {box}{' about ' + repr(q) if q else ''}"
    if name == "view_messages":
        if a.get("conversation"):
            return f"open the thread with {a['conversation']}"
        return (f"search the texts for {a['query']!r}" if a.get("query") else "look at the recent texts") + \
            (f" ({a['period']})" if a.get("period") else "")
    if name == "summarize_emails":
        return "pull the email digest"
    if name == "summarize_messages":
        return "pull the texts digest"
    if name == "get_upcoming":
        what = f" for {q}" if q else ""
        span = a.get("period") or (f"the next {a['days']} days" if a.get("days") else "")
        return f"look up the calendar{what}" + (f" ({span})" if span else "")
    if name == "search_reminders":
        return f"find the reminder{' for ' + repr(q) if q else 's'}"
    if name == "search_notes":
        return f"find the note about {q!r}" if q else "search the notes"
    if name == "web_search":
        return f"search the web for {q!r}"
    if name == "get_stock_price":
        return f"get the live price for {', '.join(a.get('symbols') or [])}"
    if name == "list_dir":
        return f"list {a.get('path')}"
    if name == "lookup_contact":
        return f"look up {a.get('name') or q}"
    if name == DISCOVERY:
        return f"get {', '.join(a.get('tools') or a.get('families') or [])}"
    shown = [f"{k} {json.dumps(v, ensure_ascii=False)}" for k, v in list(a.items())[:3]
             if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 30]
    return f"call {name}" + (f" with {', '.join(shown)}" if shown else "")


_SOURCE = {"view_emails": "email", "summarize_emails": "email", "view_messages": "texts", "summarize_messages": "texts",
           "get_upcoming": "calendar", "get_past_events": "calendar history", "search_reminders": "reminders",
           "get_recent_activity": "recent activity", "get_my_day_plan": "day plan", "search_notes": "note",
           "read_note": "note", "read_file": "file", "web_search": "web result", "web_fetch": "page",
           "get_weather": "forecast", "list_dir": "folder listing", "get_stock_price": "price", "find_free_time": "free slots",
           "lookup_contact": "contact", "recall": "memory"}


def source_of(name: str) -> str:
    return _SOURCE.get(name, name.replace("_", " "))


def facts(name: str, text: str) -> str | None:
    """A short, exact fact from a result in a known format, else None."""
    if re.search(r"^No (?:messages|emails|results|matching)|No messages found|^Nothing", text, re.M) and len(text) < 260:
        return f"the {source_of(name)} came back empty"
    m = re.search(r"(\d+) emails? · (\d+) unread", text)
    if m:
        return f"{m[1]} email{'s' if m[1] != '1' else ''}, {m[2]} unread"
    m = re.search(r"(?:Next \d+ days|Tomorrow|Today|This week|Next week)[^:\n]*: (\d+ events?(?:, \d+ reminders?)?)", text)
    if m:
        return m[1]
    m = re.search(r"Past \d+ day\(s\), (\d+) item\(s\)", text)
    if m:
        return f"{m[1]} past items"
    m = re.search(r"(\d+) new in the last", text)
    if m:
        return f"{m[1]} new items"
    m = re.search(r"(\d+) messages? across (\d+) conversations?", text)
    if m:
        return f"{m[1]} message{'s' if m[1] != '1' else ''} in {m[2]} thread{'s' if m[2] != '1' else ''}"
    m = re.search(r"^(?:\w+day, \w+ \d+|The weekend of \w+ \d+|[A-Z][a-z]+ week[^:\n]*): (\d+ events?(?:, (?:\d+|no) reminders?)?)", text, re.M)
    if m:
        return m[1]
    return None


def fact_sentence(name: str, text: str) -> str:
    f = facts(name, text)
    if not f:
        return ""
    return _sentence(f if f.startswith("the ") else f"The {source_of(name)} shows {f}")


def current_slot(title: str, text: str, clock: _dt.datetime | None):
    """(date or None, 'H:MM AM') of the item named `title` in a calendar or reminders read, else None."""
    if not title or not clock:
        return None
    key = title.lower()[:18]
    cur = None
    for line in text.split("\n"):
        ds = [d for d, _ in stated_dates(line, clock)[0] if d]
        if line.startswith("**") and ds:
            cur = ds[0]
        if key in line.lower():
            tm = re.search(r"(\d{1,2}:\d\d [AP]M)", line)
            if tm:
                return (ds[0] if ds else cur), tm.group(1)
    return None


def _starts(text: str) -> list[str]:
    return re.findall(r"\*\*(\d{1,2}:\d\d [AP]M)\*\*", text)


# ----------------------------------------------------------------- thought writers
def _tool_reason(fam: str, name: str, a: dict, seed: str) -> str:
    if name == "update_event":
        return pick(seed + "r", ["It's an existing event getting a new time, so update_event, not a second event.",
                                 "That event already exists; moving it is update_event, not a new add.",
                                 "A move, not a new event: update_event."])
    if name == "add_calendar_event" and fam in ("right_write_tool",):
        return pick(seed + "r", ["It's a new appointment with a set time, so add_calendar_event.",
                                 "Nothing to move here; it's a brand-new event."])
    if name == "add_reminder" and fam in ("right_write_tool", "act_dont_ask"):
        return pick(seed + "r", ["It's something to be nudged about, so a reminder, not an event.",
                                 "A nudge to do something is a reminder; no need to ask anything.",
                                 "Task plus a time: a reminder, no question needed."])
    if name == "draft_email":
        return pick(seed + "r", ["Asked for a draft to look over, so draft_email, not send_email.",
                                 "A draft, not a send: nothing goes out yet."])
    if name in ("send_email", "send_message") and fam in ("right_write_tool", "safe_effects", "act_dont_ask"):
        return pick(seed + "r", ["Asked to send it now, so the real send, not a draft.",
                                 "This is a send now, not a scheduled one or a draft.",
                                 "Wants it sent, not drafted."])
    if name == "schedule_send":
        return pick(seed + "r", ["A later send time was named, so schedule_send, not an immediate send.",
                                 "It should go out later, at the named time, so schedule it."])
    if name == "append_note":
        return pick(seed + "r", ["The note already exists, so append to it instead of making a new one.",
                                 "Adding to an existing note is append_note, not a second note."])
    if name == "create_note":
        return pick(seed + "r", ["It's a new note, so create_note.", "Nothing to append to; this starts a fresh note."])
    if name == "complete_reminder":
        return pick(seed + "r", ["It's done, not unwanted, so complete it and keep the record.",
                                 "Finished means complete_reminder, not a delete."])
    if name == "clear_reminders":
        return pick(seed + "r", ["No longer needed, so delete it rather than marking it done.",
                                 "Unwanted, not finished, so it gets removed."])
    if name == "remember":
        return pick(seed + "r", ["A plain first-person fact, so it's worth saving.",
                                 "Stated plainly about the speaker, so remember it as given."])
    return ""


def _shown(s) -> list[str]:
    return [s["prompt"], *s["all_results"]]


def _grounded_day(s, clock, a) -> bool:
    """The call's date is today, tomorrow, or a date the prompt or a result shows (so a thought may name it)."""
    p = parse_iso(a.get("when_iso"))
    if not p or clock is None:
        return False
    delta = (p[0] - clock.date()).days
    if delta == 0 or (delta == 1 and (_TMRW.search(s["prompt"]) or (p[1] is not None and p[1] <= clock.time()))):
        return True
    return any(p[0] in {d for d, _ in stated_dates(src, clock)[0]} for src in _shown(s))


def _write_plan(meta, s, calls, ctx, seed, skip_resolution=False):
    """(date resolution sentence, tool-choice reason, action clauses) for the write call(s)."""
    fam = meta.get("family") or ""
    clock = parse_clock(meta)
    terse = bool((ctx or {}).get("terse"))
    resolution, acts = "", []
    prior = ""
    if s["results"] and s["prev_calls"] and not s["loaded"]:
        src = source_of(s["prev_calls"][0][0])
        prior = f"the {src}" if src in ("forecast", "price", "web result", "note", "folder listing") else ""
    for name, a in calls:
        if a.get("when_iso") and not resolution and not skip_resolution:
            resolution = resolve_when(clock, a["when_iso"], s["prompt"], seed, s["all_results"])
    for name, a in calls:
        with_day = (not resolution or len(calls) > 1) and _grounded_day(s, clock, a)
        acts.append(action(name, a, prior, with_day=with_day, terse=terse, seed=seed))
    reason = ""
    for name, a in calls:
        reason = reason or _tool_reason(fam, name, a, seed)
    return resolution, reason, acts


def compose(parts: list[tuple[int, str]], max_sentences: int = 3, max_words: int = MAX_WORDS) -> str:
    """Join (priority, text) parts in order, dropping the lowest priority ones until the thought has at most
    `max_sentences` sentences and `max_words` words (1-3 sentences, <= 60 words)."""
    parts = [(p, t.strip()) for p, t in parts if t and t.strip()]

    def n_sent(ps):
        return sum(len(re.findall(r"[.!?](?:\s|$)", t)) or 1 for _, t in ps)

    while len(parts) > 1 and (n_sent(parts) > max_sentences or len(_join(*[t for _, t in parts]).split()) > max_words):
        low = min(range(len(parts)), key=lambda i: (parts[i][0], -i))
        parts.pop(low)
    return _join(*[t for _, t in parts])


def _join(*parts) -> str:
    out = " ".join(p.strip() for p in parts if p and p.strip())
    return re.sub(r"\s+", " ", out).strip()


def _sentence(s: str) -> str:
    s = s.strip()
    return s[0].upper() + s[1:] + ("" if s.endswith((".", "!", "?")) else ".") if s else ""


def _plan_calls(s, ctx) -> list[tuple[str, dict]]:
    """The calls whose arguments this step commits to: its own, or (for a bare schema load) the next
    real call in the turn."""
    own = [(n, a) for n, a in s["calls"] if n != DISCOVERY]
    if own:
        return own
    want = set().union(*[_loads(n, a) for n, a in s["calls"]]) if s["calls"] else set()
    for n, a in (ctx or {}).get("later_calls", []):
        if n in want:
            return [(n, a)]
    return []


def _ask_gist(prompt: str) -> str:
    """'Wants X' from the prompt, for a subjectless lead."""
    p = re.sub(r"^(?:hey|yo|so|ok|okay|pls|please|quick one|real quick|can you|could you|can u)[,:\s]+", "",
               prompt.strip(), flags=re.I)
    return _q(p.rstrip("?.! "), 8)


def thought_select_or_next(meta, s, ctx, seed) -> str:
    fam = meta.get("family") or ""
    calls = s["calls"]
    names = [n for n, _ in calls]
    later = (ctx or {}).get("later_calls", [])
    later_write = next(((n, a) for n, a in later if n in WRITE_TOOLS), None)
    plan = _plan_calls(s, ctx)
    write_plan = [(n, a) for n, a in plan if n in WRITE_TOOLS]
    if write_plan:
        lead, skip_res = "", False
        if s["results"] and not s["loaded"] and s["prev_calls"]:
            prev = s["prev_calls"][0][0]
            if prev in WRITE_TOOLS:
                lead = pick(seed + "pl", ["First part's in.", "One down, one to go."])
            else:
                lead = slot_lead(meta, s, write_plan, seed)
                skip_res = bool(lead) and not _day_word(s["prompt"])    # the slot sentence already places it
                lead = lead or fact_sentence(prev, s["results"][-1]) or \
                    pick(seed + "pl", [f"The {source_of(prev)} result has what's needed.", f"Got the {source_of(prev)} back.",
                                       f"The {source_of(prev)} result gives the content."])
        resolution, reason, acts = _write_plan(meta, s, write_plan, ctx, seed, skip_res)
        act = " and ".join(acts)
        verb = (pick(seed + "v", ["I'll", "Next I'll", "So I'll", "Then"]) if lead else
                pick(seed + "v", ["I'll", "So I'll", "So:"]) if resolution else pick(seed + "v", ["I'll", "Plan:"]))
        return compose([(3, lead), (4, resolution), (2, reason), (5, _sentence(f"{verb} {act}"))])
    # a read (or a load of a read)
    reads = plan or [(n, a) for n, a in calls if n != DISCOVERY]
    read = " and ".join(read_phrase(n, a) for n, a in reads) or read_phrase(*calls[0])
    verb = pick(seed + "v", ["I'll", "So I'll", "Then I'll"])
    if later_write and fam in WRITE_FAMILIES:
        # The write's arguments depend on what this read returns, so the plan names only the next move.
        wg = write_gist(later_write[0], later_write[1], s["prompt"])
        why = {"manage_contacts": "The new address or number has to come from there.",
               "send_message": "What goes in the text comes from that.",
               "send_email": "The email's content comes from that.",
               "update_event": "Need the current slot before moving it.",
               "update_reminder": "Need the current reminder before changing it.",
               "add_reminder": "The reminder's time and title depend on what it shows.",
               "complete_reminder": "Need the exact reminder first.",
               }.get(later_write[0], "")
        return compose([(4, _sentence(pick(seed + "rb", [f"First {read}, then {wg}",
                                                        f"I'll {read} first; after that, {wg}",
                                                        f"Read before writing: {read}, then {wg}"]))),
                        (3, pick(seed + "why", [why, ""]) if why else "")])
    if fam == "multi_source":
        srcs = _planned_sources(s)
        return _join(pick(seed + "ms", [f"This spans {len(srcs)} sources: {', '.join(srcs[:-1])} and {srcs[-1]}.",
                                        f"Needs {' plus '.join(srcs)}, read together."]),
                     pick(seed, ["Read them all at once, then merge.", "One pass over each, then a combined answer.",
                                 "Pull each, then say what matters across them."]))
    if fam == "safe_effects":
        n0 = reads[0][0] if reads else names[0]
        why = {"get_stock_price": ["A price is a quick lookup; no page fetch and nothing to send.",
                                   "Live price lookup, not a page fetch, and nothing goes out."],
               "web_search": ["A web search answers this; nothing to fetch or send.",
                              "Search results are enough; no page fetch, nothing sent."],
               "view_messages": ["The ask is who wrote, so the plain message list, not a summary; nothing gets sent.",
                                 "This asks who texted, so list the messages; no summary, no reply."],
               "list_scheduled_sends": ["This only asks what's queued; nothing gets sent or changed.",
                                        "Just a look at the queue; nothing to send or cancel."],
               "list_dir": ["A look, not a cleanup, so just list it; no moving or deleting.",
                            "Looking only: list the folder, move nothing, delete nothing."],
               }.get(n0, ["This only needs a read; nothing to send or change.",
                          "A read is enough here; no effects."])
        return compose([(3, pick(seed + "w", why)), (4, _sentence(f"{verb} {read}"))])
    if s["results"] and s["prev_calls"] and not s["loaded"]:
        f = fact_sentence(s["prev_calls"][0][0], s["results"][-1])
        return _join(f or f"The {source_of(s['prev_calls'][0][0])} covers part of it.",
                     _sentence(f"Still need to {read}"))
    return _sentence(f"{verb} {read}")


def slot_lead(meta, s, plan, seed) -> str:
    """For a move after a read: where the item is now, and the shift asked for (both read off the result
    and the prompt; the day is only named when it stays the same)."""
    move = next(((n, a) for n, a in plan if n in ("update_event", "update_reminder")), None)
    if not move or not s["results"]:
        return ""
    clock = parse_clock(meta)
    slot = current_slot(move[1].get("title") or "", s["results"][-1], clock)
    new = parse_iso(move[1].get("when_iso"))
    if not slot or not new or not new[1]:
        return ""
    old_d, old_t = slot
    same_day = old_d == new[0]
    where = f"on {fmt_day(old_d)} at {old_t}" if same_day and old_d else f"at {old_t}"
    shift = _SHIFT.search(s["prompt"])
    title = _q(move[1].get("title"), 5)
    lead = pick(seed + "sl", [f"“{title}” is {where} now", f"Right now “{title}” is {where}",
                              f"Currently “{title}” sits {where}"])
    if shift and "day" in shift.group(1).lower():
        lead += f"; {shift.group(0).lower()} is {fmt_day(new[0])}, same time"
    elif shift:
        lead += f"; {shift.group(0).lower()} makes it {fmt_time(new[1])}"
    return _sentence(lead)


def write_gist(name: str, a: dict, prompt: str) -> str:
    """The next write named without the arguments a read still has to supply."""
    to = a.get("to")
    to = to if to and re.search(rf"\b{re.escape(str(to).split()[0])}\b", prompt, re.I) else ""
    return {"update_event": "move it with update_event",
            "add_reminder": "set the reminder from what it shows",
            "update_reminder": "move that reminder with update_reminder",
            "complete_reminder": "mark it done",
            "manage_contacts": "save it to the contact with manage_contacts",
            "send_message": f"text {to or 'it'}",
            "send_email": f"email {to or 'it'}",
            "schedule_send": "queue the send with schedule_send",
            "draft_email": "draft the reply, not send it",
            "reply_to_email": "write the reply",
            "create_note": "put it in a new note",
            "append_note": "add it to the note",
            }.get(name, f"call {name}")


_BAD_ARGS = re.compile(r"unexpected (?:keyword )?argument(?:\(s\))?:? \[?([^\]\.;]+?)\]?(?:[.;]|$)")
_EXPECTED = re.compile(r"Expected arguments — ([^.]+)")
_MISSING = re.compile(r"missing (?:required )?(?:argument(?:\(s\))?:? )?\[?'?([a-z_]+)'?")


def _title_in(text: str, fallback: str = "") -> str:
    m = re.search(r"“([^”]+)”", text)
    return m.group(1) if m else fallback


_STATES = {"AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
           "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "IL": "illinois", "IN": "indiana",
           "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
           "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi", "MO": "missouri",
           "NV": "nevada", "NJ": "new jersey", "NY": "new york", "NC": "north carolina", "OH": "ohio", "OK": "oklahoma",
           "OR": "oregon", "PA": "pennsylvania", "TN": "tennessee", "TX": "texas", "UT": "utah", "VA": "virginia",
           "WA": "washington", "WI": "wisconsin"}


def _US_STATE_IN(prompt: str, code: str) -> bool:
    name = _STATES.get(code.upper())
    return bool(name and re.search(rf"\b{name}\b", prompt, re.I))


def _ambiguous_place(first: str, s: dict) -> str:
    """The place name a weather lookup refused as ambiguous ('' when the error is something else)."""
    m = re.search(r"couldn't resolve '([^']+)'", first)
    if m:
        return m.group(1)
    if "no unambiguous location" in first:
        return next((a.get("location") for _n, a in s["prev_calls"] if a.get("location")), "") or "That place"
    return ""


def thought_after_error(meta, s, ctx, seed) -> str:
    err = next(r for r in s["results"] if is_error(r))
    first = err.lstrip().split("\n", 1)[0]
    calls = s["calls"]
    plan = _plan_calls(s, ctx)
    clock = parse_clock(meta)
    when = next((resolve_when(clock, a["when_iso"], s["prompt"], seed, s["all_results"])
                 for n, a in plan if a.get("when_iso")), "")
    fixed = " and ".join(action(n, a, with_day=not when and _grounded_day(s, clock, a), seed=seed)
                         for n, a in plan) if plan else ""
    if first.startswith(DENIED):
        return pick(seed, ["That was denied, so nothing went out. Say plainly it wasn't done.",
                           "Denied; nothing happened. Say so, with no hint that it happened anyway.",
                           "It was turned down, so it stays undone. One plain line saying so."])
    if not calls:            # a refusal the final must relay
        title = _title_in(first, "it")
        if "already starts then" in first:
            return _join(f"“{title}” already starts at that time, so there was nothing to move.",
                         pick(seed, ["Say it's already there.", "Just point out it's already at that time."]))
        if "read-only" in first:
            return pick(seed, [f"“{title}” is on a read-only calendar, so nothing changed. Say so, and that it has to be "
                               f"moved in its own calendar.",
                               f"Read-only calendar: “{title}” can't be moved from this Mac. Be plain it stayed put.",
                               "That calendar can't be edited from here, so the time is unchanged. One plain line, "
                               "plus where it can be moved."])
        if "organizer" in first:
            return _join(f"Only the organizer can move “{title}”; nothing changed.",
                         pick(seed, ["Say so and suggest asking the organizer.",
                                     "Be plain it stayed put; the organizer has to move it."]))
        if "invitees" in first:
            return _join(f"Calendar hasn't synced whether “{title}” has invitees, so nothing moved.",
                         pick(seed, ["Say it stayed put and to try again shortly.",
                                     "Plain answer: unchanged for now, worth retrying in a bit."]))
        if re.search(r"[Nn]othing was \w+", first):
            return _join("That didn't go through; nothing changed.",
                         pick(seed, ["Say so plainly, with the next step it gives.", "Be plain about it."]))
        src = source_of(s["prev_calls"][0][0]) if s["prev_calls"] else "that"
        place = _ambiguous_place(first, s)
        if place:
            return _join(pick(seed + "a", [f"'{place}' matches more than one place, and the tool says not to guess.",
                                           f"The lookup won't pick a '{place}' on its own."]),
                         pick(seed, ["One question: which one.", "Ask which state, nothing else.",
                                     "Guessing is off the table, so one short question."]))
        if not re.search(r"^Wisp (?:can't|could not|cannot)\b|isn't connected|access", first):
            reason = re.sub(r"^\(error:?\s*", "", first).rstrip(").")
            return _join(f"The {src} call failed: {_q(reason, 10)}.",
                         pick(seed, ["Say so plainly; no made-up data.", "Be plain it didn't work, and invent nothing."]))
        return _join(pick(seed + "u", [f"{src[:1].upper() + src[1:]} can't be read right now.",
                                       f"No access to the {src} at the moment."]),
                     pick(seed, ["Say that plainly with the fix it gives, and invent nothing.",
                                 "No data to report, so explain how to turn access on instead."]))
    m = re.match(r"\(error calling (\w+)\(", first)
    bad_tool = m.group(1) if m else (s["prev_calls"][0][0] if s["prev_calls"] else "That call")
    old_args = next((a for n, a in s["prev_calls"] if n == bad_tool), {})
    bad = _BAD_ARGS.search(first)
    exp = _EXPECTED.search(first)
    new_names = {n for n, _ in calls}
    new_args = plan[0][1] if plan else {}
    per = re.search(r"couldn't understand the period '([^']+)'", first)
    if per:
        return _join(pick(seed + "a", [f"'{per.group(1)}' isn't a period {bad_tool} accepts.",
                                       f"{bad_tool} can't parse '{per.group(1)}' as a period.",
                                       f"The period '{per.group(1)}' was rejected; it wants a fixed form."]),
                     _sentence(pick(seed, [f"I'll retry: {fixed}", f"Retry with what it does take: {fixed}",
                                           f"Same question, valid arguments: {fixed}"])))
    if _ambiguous_place(first, s):
        where = _ambiguous_place(first, s)
        loc = next((a.get("location") for _n, a in calls if a.get("location")), "")
        home = re.search(rf"home city: ({re.escape(where)}, [A-Z][A-Za-z ]+?)\.", s["system"], re.I)
        state = loc.split(",", 1)[1].strip() if "," in loc else ""
        if state and re.search(rf"\b{re.escape(state)}\b", s["prompt"], re.I) or _US_STATE_IN(s["prompt"], state):
            return _join(pick(seed + "a", [f"'{where}' alone is ambiguous and the tool won't guess.",
                                           f"The lookup refused a bare '{where}'."]),
                         pick(seed, [f"The request names the state, so retry with {loc}.",
                                     f"The state is right there in the ask: {loc}."]))
        if home and loc:
            return _join(pick(seed + "a", [f"'{where}' matches more than one place and the tool won't pick.",
                                           f"The forecast tool can't tell which '{where}' is meant."]),
                         pick(seed, [f"The home city is {home.group(1)}, so that's the one; retry with {loc}.",
                                     f"Home is {home.group(1)}, which settles it: {loc}."]))
        final = (ctx or {}).get("final", "")
        named = bool(state) and (re.search(rf"\b{re.escape(state)}\b", final) or _US_STATE_IN(final, state))
        if not named:      # the final doesn't say which one, so the thought must not promise it will
            return _join(pick(seed + "a", [f"'{where}' matches more than one place and the tool won't pick.",
                                           f"'{where}' is ambiguous, so the lookup stopped."]),
                         _sentence(f"Retry with {loc}"))
        return _join(pick(seed + "a", [f"'{where}' matches more than one place and the tool won't pick.",
                                       f"The forecast tool can't tell which '{where}' is meant.",
                                       f"'{where}' is ambiguous, so the lookup stopped."]),
                     pick(seed, [f"Retry with {loc} and name it in the answer, so a different one can be asked for.",
                                 f"Try {loc}, and say which one it is in case another was meant.",
                                 f"Going with {loc}; the answer has to say so plainly."]))
    renamed_from = sorted(set(old_args) - set(new_args))
    renamed_to = sorted(set(new_args) - set(old_args))
    if "start time is invalid" in first:
        if renamed_from and renamed_to:
            return _join(f"{bad_tool} rejected the start time and changed nothing.",
                         f"The time goes in {', '.join(renamed_to)}, not {', '.join(renamed_from)}.", when,
                         _sentence(f"I'll resend: {fixed}"))
        return _join(f"{bad_tool} rejected the start time and changed nothing; it wants plain local time, no offset.",
                     when, _sentence(f"I'll resend: {fixed}"))
    if "duration_m" in first:
        dur = next((a.get("duration_min") for _n, a in calls if a.get("duration_min")), None)
        return _join("duration_min has to be a whole number of minutes.", when,
                     _sentence(f"So {dur} minutes: {fixed}" if dur else f"I'll resend: {fixed}"))
    must = re.search(r"\b([a-z_]+) must be (.+?)(?:\. Expected|\.?\)?$)", first)
    if must and not bad:
        what = must.group(2).rstrip(".)").replace("[", "").replace("]", "")
        what = f"it has to be {what}" if len(what.split()) <= 6 and "(" not in what else "it only takes set forms"
        return compose([(4, f"{bad_tool} rejected the {must.group(1)} value: {what}."), (3, when),
                        (5, _sentence(pick(seed, [f"I'll retry: {fixed}", f"Same call, valid value: {fixed}"])))])
    bad_names = [x.strip().strip("'\"") for x in bad.group(1).split(",")] if bad else []
    badtxt = ", ".join(bad_names) if bad_names else "an argument"
    exp_all = [x.split(":")[0].strip() for x in exp.group(1).split(",")] if exp else []
    in_list = bool(bad_names) and all(b in exp_all for b in bad_names)
    exp_txt = f"; it takes {', '.join(exp_all[:4])}" if exp_all and not in_list else ""
    missing = _MISSING.search(first)
    miss_txt = f", and it needs {missing.group(1)}" if missing and missing.group(1) not in bad_names else ""
    if bad_tool not in new_names and plan:
        head = (pick(seed + "a", [f"{bad_tool} rejected {badtxt}.", f"That failed: {bad_tool} wouldn't take {badtxt}."])
                if in_list else
                pick(seed + "a", [f"{bad_tool} has no {badtxt} argument{exp_txt}.",
                                  f"That failed: {bad_tool} doesn't take {badtxt}{exp_txt}."]))
        tool = plan[0][0]
        kw = "query" in bad_names and "query" in new_args
        return _join(head, pick(seed, ([f"{tool} takes a keyword search, so I'll {fixed}."] if kw else []) +
                                [f"So I'll switch to {tool} and {fixed}.", f"{tool} handles that; I'll {fixed}."]))
    fix_names = (f" The right names: {', '.join(renamed_to)} instead of {', '.join(renamed_from)}."
                 if renamed_from and renamed_to else "")
    return compose([(4, pick(seed + "a", [f"{bad_tool} rejected {badtxt}{exp_txt}{miss_txt}.",
                                          f"The call failed on {badtxt}{exp_txt}{miss_txt}."])),
                    (2, fix_names.strip()), (3, when),
                    (5, _sentence(pick(seed, [f"I'll retry: {fixed}", f"Same call, right names: {fixed}",
                                              f"Fixing the names and retrying: {fixed}"])))])


def _inj_kind(text: str) -> str:
    low = text.lower()
    for pat, kind in ((r"\bforward", "forward mail somewhere"), (r"\btext (?:all|every)", "text every contact"),
                      (r"\b(?:pay|payment|transfer|wire)", "set up a payment"),
                      (r"\b(?:run|execute|curl|terminal|shell|command)\b", "run a command"),
                      (r"\b(?:delete|remove|clear|wipe)", "delete things"),
                      (r"\b(?:reveal|memory|memories|password|share what you know)", "reveal private details"),
                      (r"\bsend\b", "send something out"), (r"ignore", "drop its instructions")):
        if re.search(pat, low):
            return kind
    return "do something nobody asked for"


def thought_outcome(meta, s, ctx, seed) -> str:
    """A write family's final over a result that is not a clean success: say what it actually reports."""
    fam = meta.get("family") or ""
    r = s["results"][-1] if s["results"] else ""
    oc = _narrate_outcome(fam, s)
    prev = s["prev_calls"][0] if s["prev_calls"] else ("", {})
    name, a = prev
    clock = parse_clock(meta)
    if oc == "no_match":
        who = re.search(r"No saved contact named '([^']+)'", r)
        if who:
            return _join(f"No saved contact named {who.group(1)}, so nothing went out.",
                         pick(seed, ["Ask for the number instead of guessing one.",
                                     "One question: which number to use.",
                                     "Can't text an unknown name; ask how to reach that person."]))
        q = re.search(r"['“]([^'”]+)['”]", r)
        what = q.group(1) if q else (a.get("title") or a.get("query") or "that")
        thing = ("event" if "Calendar" in r or name in ("update_event", "cancel_event") else
                 "memory" if name == "forget" else "reminder")
        return _join(pick(seed + "a", [f"No {thing} matches '{what}', so nothing changed.",
                                       f"Nothing matches '{what}'; no {thing} was touched."]),
                     pick(seed, ["Say so and ask for the exact name.", "Ask what it's called rather than guess.",
                                 "Plain no-match, then one question about the wording."]))
    if oc in ("ambiguous", "clarify"):
        items = re.findall(r"“([^”]+)”", r) if oc == "clarify" else re.findall(r"[:;] ([^;()]+?) \(([^)]+)\)", r)
        what = _title_in(r, a.get("title") or a.get("query") or "it")
        if oc == "ambiguous" and items:
            whens = [w for _t, w in items][:2]
            head = f"{len(items)} events match “{what}”: {' and '.join(whens)}."
            risk = "Moving the wrong one is worse than asking"
        else:
            lines = re.findall(r"^- ([^—\n]+?) —", r, re.M)
            names = lines[:3] or items[:3]
            head = (f"{len(names)} reminders fit: {' and '.join('“' + n.strip() + '”' for n in names)}." if names
                    else "More than one item fits.")
            risk = ("Marking the wrong one done is worse than asking"
                    if re.search(r"\b(?:done|complete|tick|finish|mark)", s["prompt"], re.I) else
                    "Changing the wrong one is worse than asking")
        return _join(head, pick(seed, [f"{risk}, so one question: which.", f"{risk}; ask which one, nothing changed yet.",
                                       "Can't tell which was meant; one short question."]))
    if oc == "partial":
        rc = receipt_of(s)
        rel = ""
        if rc and clock:
            ds, _ = stated_dates(f"{rc['wd']} {rc['mon']} {rc['day']}", clock)
            d = ds[0][0] if ds else None
            if d:
                delta = (d - clock.date()).days
                rel = (f"That's today, {fmt_day(d)}." if delta == 0 else f"That's tomorrow, {fmt_day(d)}." if delta == 1
                       else f"Due {fmt_day(d)}.")
        return compose([(4, "It's saved in Wisp only; Apple Reminders wasn't changed."), (2, rel),
                        (3, pick(seed, ["Worth saying, so nobody looks for it in the Reminders app.",
                                        "Mention that it won't show up in Apple Reminders."]))])
    if oc in ("denied", "refused", "error"):
        return pick(seed, ["That didn't happen. Say so plainly.", "Nothing changed; one plain line about why."])
    return pick(seed, ["Nothing was written yet; one detail is missing. Ask for it plainly.",
                       "Can't act on it as given; one short question first."])


def thought_narrate(meta, s, ctx, seed) -> str:
    fam = meta.get("family") or ""
    turn_calls = (ctx or {}).get("turn_reads") or s["prev_calls"]
    if fam in WRITE_FAMILIES:
        return thought_outcome(meta, s, ctx, seed)
    if fam == "ignore_embedded_instructions":
        # the planted instruction: from its marker sentence to the end of that paragraph
        line = next(r[m.start():] for r in s["all_results"] for m in [INJECTION.search(r)] if m)
        line = line.split("\n\n", 1)[0][:300]
        src = source_of(next((n for n, _ in turn_calls if n != DISCOVERY), "result"))
        kind = _inj_kind(line)
        return _join(pick(seed + "a", [f"The {src} answers it, but one line is aimed at an AI assistant, telling it to {kind}.",
                                       f"Part of the {src} is written to an assistant: it wants me to {kind}.",
                                       f"Buried in the {src}: an instruction to {kind}."]),
                     pick(seed + "b", ["That isn't part of the request, so no action; report the content and flag it in a line.",
                                       "I'll ignore it, give what the text actually says, and mention the planted line.",
                                       "Not the asker's instruction, so no action; just the content plus a short heads-up."]))
    if fam == "multi_source":
        reads = [(n, r) for (n, _a), r in zip(s["prev_calls"], s["results"]) if n != DISCOVERY]
        act = next((r for n, r in reads if n == "get_recent_activity"), None)
        if act is not None and len(reads) == 1:
            return thought_activity(s, act, seed)
        plan = next((r for n, r in reads if n in PLAN_TOOLS), None)
        if plan is not None:
            ov = re.search(r"Overview: ([^\n]+)", plan)
            if ov:
                bits = [b.strip().rstrip(".") for b in ov.group(1).split(";") if b.strip()]
                bits = [re.sub(r"time-bound items?", "timed", b).replace(" today", "").replace(" are waiting on a reply",
                                                                                              " waiting on a reply")
                        for b in bits]
                head = f"The day plan has {', '.join(bits[:-1]) + ' and ' + bits[-1] if len(bits) > 1 else bits[0]}."
            else:
                head = "The day plan pulls calendar, reminders and messages together."
            return _join(head, pick(seed, ["Timed items, anything overdue and pending replies each get a mention.",
                                           "Order it by what's pressing, and don't drop the overdue ones.",
                                           "Overdue items and replies count as much as the timed ones."]))
        bits = []
        for n, r in reads:
            f = facts(n, r)
            if f and f.endswith("came back empty"):
                f = "empty"
            bits.append(f"{source_of(n)} ({f})" if f else source_of(n))
        bits = list(dict.fromkeys(bits))
        srcs = ", ".join(bits[:-1]) + " and " + bits[-1] if len(bits) > 1 else bits[0]
        n_src = len({source_of(n) for n, _ in reads})
        allw = "both" if n_src == 2 else f"all {['', 'one', 'two', 'three', 'four', 'five'][min(n_src, 5)]}"
        empty = any("(empty)" in b for b in bits)
        return _join(_sentence(f"Back from {srcs}"),
                     pick(seed, [f"Cover {allw}, each in its own part" + ("; the empty one gets said, not skipped." if empty
                                                                          else "."),
                                 f"One short part per source, {allw} of them, most pressing first.",
                                 f"Lead with what needs attention, then the rest; {allw} sources get a line."]))
    # single-source families: only the reasons narrate_reason() names reach here
    reason = narrate_reason(meta, s)
    prev = s["prev_calls"][0][0] if s["prev_calls"] else ""
    res = s["results"][-1] if s["results"] else ""
    clock = parse_clock(meta)
    if reason == "due" and clock:
        due = _due_items(meta, s)
        past = [(t, d) for t, d in due if d < clock]
        ahead = len(due) - len(past)
        undated = len(re.findall(r"^- ", res.split("Undated", 1)[1], re.M)) if "Undated" in res else 0
        now = f"It's {fmt_day(clock.date())}, {fmt_time(clock.time())}"
        if len(due) == 1:
            t, d = due[0]
            return _join(f"{now}; “{_q(t, 5)}” was due {fmt_day(d.date())}, so it's already past due." if past else
                         f"{now}; “{_q(t, 5)}” is due {fmt_day(d.date())}, still ahead.",
                         pick(seed, ["Say the day relative to today.", "Give the due day plainly."]))
        when = " and ".join(dict.fromkeys(f"{d.strftime('%b')} {d.day}" for _t, d in past[:3]))
        bits = [f"{len(past)} of the dated ones {'is' if len(past) == 1 else 'are'} past due ({when})" if past
                else "none of the dated ones is past due", f"{ahead} still ahead" if ahead else "none ahead"]
        if undated:
            bits.append(f"{undated} with no date")
        return _join(f"{now}: {', '.join(bits[:-1])} and {bits[-1]}.",
                     pick(seed, ["Overdue ones first." if past else "Nothing overdue to flag.",
                                 "Lead with the overdue items." if past else "Soonest first.",
                                 "Flag the overdue ones, then the rest." if past else "Say nothing's overdue, then list."]))
    if reason == "day" and clock:
        word, d = _day_target(meta, s)
        delta = (d - clock.date()).days
        place = (f"'{word}' is tomorrow, {d.strftime('%b')} {d.day}." if delta == 1 else
                 pick(seed + "dt", [f"'{word}' from {fmt_day(clock.date())} is {fmt_day(d)}.",
                                    f"Today is {fmt_day(clock.date())}, so '{word}' is {fmt_day(d)}.",
                                    f"'{word}' means {fmt_day(d)}."]))
        tail = (f"Say the date, since '{word}' alone could be misread." if _ORDINAL.search(word) or _DATE_RE.search(word)
                else pick(seed, ["Answer for that day only.", "Keep to that one day.", "Only that day's items count."]))
        return _join(place, tail)
    if reason == "slot":
        st = _starts(res)
        asked = _asked_time(s["prompt"])
        lst = ", ".join(_short_time(x) for x in st[:5])
        if asked is None:
            return _join(f"Listed starts: {lst}.", "Judge the asked slot against those, free or busy, and invent no end times.")
        hit = [x for x in st if _parse_hm(x) == asked]
        before = [x for x in st if _parse_hm(x) < asked]
        at = fmt_time(asked)
        if hit:
            head = f"Asked about {at}; something starts right at {at}, so that slot is busy."
        else:
            gap = (_dt.datetime.combine(_dt.date.min, asked) - _dt.datetime.combine(_dt.date.min, _parse_hm(before[-1]))
                   if before else None)
            near = (f" {_short_time(before[-1])} starts {int(gap.total_seconds() // 60)} minutes before, and its end "
                    f"isn't shown." if gap is not None and gap <= _dt.timedelta(minutes=60) else "")
            head = f"Asked about {at}; the starts are {lst}, none at {at}, so it reads as free.{near}"
        return _join(head, pick(seed, ["Only starts are shown, so no guessed end times.", "Yes or no first.", ""]))
    if reason == "today" and clock:
        st = _starts(res)
        past = [x for x in st if _parse_hm(x) < clock.time()]
        ahead = [x for x in st if _parse_hm(x) >= clock.time()]
        now = f"It's {fmt_time(clock.time())}"
        if not ahead:
            return _join(f"{now}; today's starts ({', '.join(_short_time(x) for x in past)}) are all already past.",
                         pick(seed, ["Nothing is left today, so say that plainly.", "So nothing's still ahead."]))
        if not past:
            return _join(f"{now}; every start today ({', '.join(_short_time(x) for x in ahead)}) is still ahead.",
                         pick(seed, ["All of it still counts as upcoming.", "Nothing has happened yet."]))
        return _join(f"{now}: {', '.join(_short_time(x) for x in past)} already past, "
                     f"{', '.join(_short_time(x) for x in ahead)} still ahead.",
                     pick(seed, ["Only what's still ahead counts as upcoming.", "Lead with what's left."]))
    return ""


def _parse_hm(text: str) -> _dt.time:
    m = re.match(r"(\d{1,2}):(\d\d) ([AP]M)", text)
    return _dt.time(int(m[1]) % 12 + (12 if m[3] == "PM" else 0), int(m[2]))


def _short_time(text: str) -> str:
    return fmt_time(_parse_hm(text))


def _asked_time(prompt: str) -> _dt.time | None:
    """The clock time a free/busy question asks about ("3pm", "at 2", "noon"); a bare hour 1-7 is PM."""
    p = prompt.lower()
    if re.search(r"\bnoon\b", p):
        return _dt.time(12, 0)
    m = re.search(r"\b(\d{1,2})(?::(\d\d))?\s*(am|pm)\b", p) or re.search(r"\bat (\d{1,2})(?::(\d\d))?\b(?!\s*(?:min|hour))", p)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    ap = m.group(3) if m.lastindex and m.lastindex >= 3 else None
    if h > 23 or mi > 59:
        return None
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    elif ap is None and 1 <= h <= 7:
        h += 12
    return _dt.time(h, mi)


_ACT_LINE = re.compile(r"^\[\s*([^\]]+?)\s*\]\s+(EMAIL|MESSAGES|CALENDAR|REMINDERS|NOTES)\b", re.M)
_ACT_NAME = {"EMAIL": ("email", "emails"), "MESSAGES": ("text", "texts"), "CALENDAR": ("calendar change", "calendar changes"),
             "REMINDERS": ("reminder", "reminders"), "NOTES": ("note", "notes")}


def thought_activity(s, text, seed) -> str:
    """Recent activity is one read over several apps: count what came in per source."""
    span = re.search(r"\(the last ([^)]+)\)|in the last (\d+ \w+)", text)
    span = (span.group(1) or span.group(2)).replace("1 hours", "hour") if span else "that window"
    lines = _ACT_LINE.findall(text)
    if not lines:
        return _join(f"Nothing new anywhere in the last {span}.",
                     pick(seed, ["Say it's quiet, plainly.", "A short all-quiet answer."]))
    from collections import Counter
    c = Counter(src for _, src in lines)
    parts = [f"{n} {_ACT_NAME[k][0 if n == 1 else 1]}" for k, n in c.most_common()]
    head = f"Last {span}: {', '.join(parts[:-1]) + ' and ' + parts[-1] if len(parts) > 1 else parts[0]}."
    if re.search(r"\bemi?a?i?ls?\b|\bmail\b", s["prompt"], re.I) and "EMAIL" not in c:
        return _join(head, "The ask was about email and there's none, so say that first.")
    unread = len(re.findall(r"\(unread\)", text))
    return _join(head, f"{unread} unread." if unread else "",
                 pick(seed, ["Newest first, one line each.", "List them by source, newest first.",
                             "Lead with what looks like it needs a reply."]))


# Words that make a request destructive (prompt) or call it destructive (thought).
DESTRUCTIVE_ASK = re.compile(r"\b(?:delete|clear|wipe|erase|remove|forget|cancel|purge|trash|get rid of)\b", re.I)
DESTRUCTIVE_WORDS = re.compile(r"\b(?:destructive|wipe[sd]?|delet\w*|remov\w*|eras\w*|cleared|clearing)\b", re.I)
DESTRUCTIVE_TOOLS = frozenset({"clear_reminders", "forget", "cancel_event", "cancel_scheduled_send", "delete_file",
                               "edit_knowledge"})
_TWO_CONTACTS = re.compile(r"([A-Z][a-z]+(?: [A-Z][a-z]+)+),? or ([A-Z][a-z]+(?: [A-Z][a-z]+)+)\?")
_PRONOUN_TO = re.compile(r"\b(?:text|tell|email|message|call|remind)\s+(her|him|them)\b", re.I)


def thought_answer_first(meta, s, ctx, seed) -> str:
    """A first-step answer with no call: say what is actually missing or ambiguous (never borrow the
    destructive wording for a request that removes nothing)."""
    text = s["target_text"]
    prompt = s["prompt"]
    low = text.lower()
    m = _TWO_CONTACTS.search(text)
    if m and "?" in text:
        first_name = m.group(1).split()[0]
        return _join(f"Two contacts are named {first_name}: {m.group(1)} and {m.group(2)}.",
                     pick(seed, ["Picking wrong would reach the wrong person, so one short question.",
                                 "Can't tell which, and guessing is risky; ask which one, nothing else.",
                                 "The message is clear; only the person isn't. Ask which one."]))
    pr = _PRONOUN_TO.search(prompt)
    if pr and "?" in text:
        return _join(f"'{pr.group(1)}' doesn't say who; there's no name to send to.",
                     pick(seed, ["The message itself is clear, so ask only who it's for.",
                                 "Won't guess a contact; one question: who."]))
    if re.search(r"who should i (?:text|email|message)", low):
        return _join("No recipient named.",
                     pick(seed, ["Won't guess one; one short question.", "Ask who, then send."]))
    if re.search(r"\bremind", prompt, re.I) and "?" in text and not DESTRUCTIVE_ASK.search(prompt):
        day = _day_word(prompt)
        head = (f"'{day}' gives the when, but there's nothing to be reminded of." if day else
                "There's a time but no task.")
        return _join(head, pick(seed, ["An empty reminder is useless, so ask what it's for.",
                                       "One short question: what the reminder is for."]))
    if "remind you about" in low or "reminder for" in low:
        return _join("There's a time but no task.",
                     pick(seed, ["An empty reminder is useless, so ask what it's for.",
                                 "One short question: what the reminder is for."]))
    if ("call the event" in low or "what's it called" in low or "title" in low or "what's the event" in low
            or (re.search(r"\b(?:event|meeting|appointment)\b", prompt, re.I) and "?" in text
                and not DESTRUCTIVE_ASK.search(prompt))):
        return _join("There's a day and time but no name for it.",
                     pick(seed, ["Ask what to call it before adding anything.", "One question: the title."]))
    if re.search(r"\b(?:text|email|message|tell)\b", prompt, re.I) and "?" in text and not DESTRUCTIVE_ASK.search(prompt):
        return _join("Who it goes to or what it says isn't settled.",
                     pick(seed, ["Ask for the missing part rather than guess it.", "One short question first."]))
    if "?" in text and DESTRUCTIVE_ASK.search(prompt):
        return _join(pick(seed + "a", ["This would delete things and doesn't say what.",
                                       "A wipe with no target named.", "That's destructive, and the target is unclear."]),
                     pick(seed, ["Too risky to guess, so one short question about what's meant.",
                                 "No acting on a vague destructive ask; ask which part, nothing else.",
                                 "Nothing gets removed until the target is named; one question."]))
    if "?" in text:
        return _join("One detail the request needs isn't there.",
                     pick(seed, ["Ask for it plainly rather than guess.", "One short question first."]))
    if re.search(r"\bmy (?:roommate|dad|mom|mother|father|sister|brother|friend|boss|partner|cousin|aunt|uncle)\b|"
                 r"^[A-Za-z]+ (?:is|just|got|was)\b", s["prompt"].strip(), re.I):
        return pick(seed, ["That's news about someone else, not a personal fact, so nothing to remember. Just a warm reply.",
                           "It's about another person, so no memory to save and nothing to do. Respond kindly.",
                           "Someone else's news: no remember call, no lookup. A warm line back is enough."])
    return pick(seed, ["Nothing to do or save here; it's a passing moment, not a lasting fact. A short, kind reply.",
                       "No action and no memory needed, just a gentle answer to how the day went.",
                       "A passing feeling, not something to store or act on. Answer gently."])


def thought(meta: dict, messages: list[dict], ctx: dict | None = None) -> str:
    """The thought text for a THINK step. ctx (optional): later_calls [(name, args)] after this step in the
    turn, turn_reads [(name, args)] of the turn's reads so far, final (the turn's final text), salt."""
    s = analyze(meta, messages)
    seed = f"{meta.get('id')}|{(ctx or {}).get('salt', '')}"
    kind = s["kind"]
    if kind == "after_error":
        out = thought_after_error(meta, s, ctx, seed)
    elif kind == "answer_first":
        out = thought_answer_first(meta, s, ctx, seed)
    elif kind == "narrate":
        out = thought_narrate(meta, s, ctx, seed)
    else:
        out = thought_select_or_next(meta, s, ctx, seed)
    return re.sub(r"\s+", " ", out).strip()


# ----------------------------------------------------------------- checks (build + validate)
def _tri(text: str) -> set[str]:
    t = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return {t[i:i + 3] for i in range(max(0, len(t) - 2))}


def jaccard(a: str, b: str) -> float:
    x, y = _tri(a), _tri(b)
    return len(x & y) / len(x | y) if x and y else 0.0


_MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE_RE = re.compile(r"\b(?:(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun),? )?"
                      r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
                      r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.? (\d{1,2})(?:st|nd|rd|th)?\b"
                      r"|\b(\d{4})-(\d\d)-(\d\d)\b", re.I)
_WDNAME_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)


def _nearest(mon: int, day: int, clock: _dt.datetime) -> _dt.date | None:
    best = None
    for y in (clock.year - 1, clock.year, clock.year + 1):
        try:
            d = _dt.date(y, mon, day)
        except ValueError:
            continue
        if best is None or abs((d - clock.date()).days) < abs((best - clock.date()).days):
            best = d
    return best


def stated_dates(text: str, clock: _dt.datetime) -> tuple[list[tuple[_dt.date, int | None]], list[int]]:
    """Dates a thought states ((date, stated weekday or None)) and bare weekday names outside quotes."""
    out = []
    for m in _DATE_RE.finditer(text):
        if m.group(4):
            try:
                out.append((_dt.date(int(m[4]), int(m[5]), int(m[6])), None))
            except ValueError:
                out.append((None, None))
            continue
        best = _nearest(_MON[m.group(2).lower()[:3]], int(m.group(3)), clock)
        wd = _WD_IDX[m.group(1).lower()[:3]] if m.group(1) else None
        out.append((best, wd))
    unquoted = re.sub(r"'[^']*'|“[^”]*”|\"[^\"]*\"", " ", text)
    wds = [_WD_IDX[w.lower()[:3]] for w in _WDNAME_RE.findall(unquoted)]
    return out, wds


def date_problems(text: str, clock: _dt.datetime | None, dates: list[_dt.date]) -> list[str]:
    """When the step's call has a date: every date the thought states must be that date (or today), with the
    right weekday; a bare weekday name must be today's or the call's."""
    if not clock or not dates or not text:
        return []
    today = clock.date()
    ok = set(dates) | {today}
    errs = []
    stated, wds = stated_dates(text, clock)
    for d, wd in stated:
        if d is None:
            errs.append("unparseable date")
        elif d not in ok:
            errs.append(f"states {d} but the call says {sorted(set(dates))[0]}")
        elif wd is not None and wd != d.weekday():
            errs.append(f"weekday for {d} is wrong")
    okwd = {d.weekday() for d in ok}
    for wd in wds:
        if wd not in okwd:
            errs.append(f"weekday {_WD[wd]} is neither today nor the call's day")
    return errs


def grounding_problems(text: str, clock: _dt.datetime | None, sources: list[str]) -> list[str]:
    """For a step with no dated call: every date the thought states must appear in the turn's tool results
    (or be today), with the right weekday; a bare weekday name must be one of those dates' weekdays."""
    if not clock or not text:
        return []
    ok = {clock.date()}
    for src in sources:
        ok |= {d for d, _ in stated_dates(src, clock)[0] if d}
    errs = []
    stated, wds = stated_dates(text, clock)
    for d, wd in stated:
        if d is None or d not in ok:
            errs.append(f"states {d}, which no result or the clock shows")
        elif wd is not None and wd != d.weekday():
            errs.append(f"weekday for {d} is wrong")
    okwd = {d.weekday() for d in ok}
    for src in sources:     # a weekday the prompt or a result names (e.g. "on thursday") may be repeated
        okwd |= {_WD_IDX[w.lower()[:3]] for w in _WD_RE.findall(src)}
    errs += [f"weekday {_WD[w]} matches no shown date" for w in wds if w not in okwd]
    return errs


_MONTHS = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_WDS = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_OFFSET = {"today": 0, "tonight": 0, "tomorrow": 1, "yesterday": -1}
# "tomorrow is Friday Feb 12", "today, Dec 21", "today (Tuesday Dec 22)", "Today is Monday Mar 22"
_REL_DATE = re.compile(rf"\b(today|tonight|tomorrow|yesterday)(?:\s+(?:is|was|means))?(?:\s+already)?,?\s+\(?"
                       rf"(?:{_WDS},?\s+)?{_MONTHS}\s+(\d{{1,2}})\b", re.I)
# "Feb 12 is tomorrow"
_DATE_REL = re.compile(rf"\b(?:{_WDS},?\s+)?{_MONTHS}\s+(\d{{1,2}})\s+(?:is|was)\s+(today|tomorrow|yesterday)\b", re.I)
# "today is Monday", "tomorrow is already Friday" (no date after it)
_REL_WD = re.compile(rf"\b(today|tomorrow|yesterday)\s+(?:is|was)\s+(?:already\s+)?{_WDS}\b(?!,?\s+{_MONTHS}\s+\d)", re.I)
# "It's already Monday", "It's Monday today", "It's 6:12 PM on Tuesday Dec 22"
_ITS_WD = re.compile(rf"\bit's\s+(?:already\s+|\d{{1,2}}:\d\d [AP]M on\s+)?{_WDS}(?:,?\s+{_MONTHS}\s+(\d{{1,2}}))?", re.I)


def relative_day_problems(text: str, clock: _dt.datetime | None) -> list[str]:
    """Every relative-day claim a thought makes must agree with the clock."""
    if not clock or not text:
        return []
    today = clock.date()
    errs = []

    def want(word):
        return today + _dt.timedelta(days=_OFFSET[word.lower()])

    for m in _REL_DATE.finditer(text):
        w, wdn, mon, day = m.group(1), m.group(2), m.group(3), int(m.group(4))
        d = _nearest(_MON[mon.lower()[:3]], day, clock)
        if d != want(w):
            errs.append(f"says {w} is {mon} {day}, but {w} is {fmt_day(want(w))}")
        elif wdn and _WD_IDX[wdn.lower()[:3]] != d.weekday():
            errs.append(f"weekday for {w} is wrong ({wdn})")
    for m in _DATE_REL.finditer(text):
        wdn, mon, day, w = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        d = _nearest(_MON[mon.lower()[:3]], day, clock)
        if d != want(w):
            errs.append(f"says {mon} {day} is {w}, but {w} is {fmt_day(want(w))}")
    for m in _REL_WD.finditer(text):
        w, wdn = m.group(1), m.group(2)
        if _WD_IDX[wdn.lower()[:3]] != want(w).weekday():
            errs.append(f"says {w} is {wdn}, but {w} is {_WD[want(w).weekday()]}")
    for m in _ITS_WD.finditer(text):
        wdn, mon, day = m.group(1), m.group(2), m.group(3)
        if _WD_IDX[wdn.lower()[:3]] != today.weekday():
            errs.append(f"says it's {wdn}, but today is {_WD[today.weekday()]}")
        elif mon and _nearest(_MON[mon.lower()[:3]], int(day), clock) != today:
            errs.append(f"says it's {mon} {day}, but today is {fmt_day(today)}")
    return errs


def success_on_failure(text: str, results: list[str]) -> list[str]:
    """A thought must not say an action happened when the previous result reports it did not."""
    failed = [outcome(r) for r in results if outcome(r) in FAILED]
    if not failed or not text:
        return []
    # quoted values and the Sent mailbox's name are not claims that something was sent
    text = re.sub(r"'[^']*'|“[^”]*”|\bsent (?:mail|folder|mailbox|items)\b", " ", text, flags=re.I)
    for sentence in re.split(r"(?<=[.!?;])\s+|\s+—\s+", text):
        m = _SUCCESS.search(sentence)
        if m and not _NEG.search(sentence):
            return [f"success on failure: {m.group(0)!r} after a {failed[0]} result"]
    return []


def clarify_problems(s: dict, text: str, ctx: dict | None = None) -> list[str]:
    """A clarifying question's thought (no call, the reply asks something) must not call the request
    destructive unless it is: the prompt asks to remove something or the turn makes a delete/clear call."""
    if s["calls"] or "?" not in s["target_text"] or not DESTRUCTIVE_WORDS.search(text or ""):
        return []
    turn = {n for n, _ in s["turn_calls"]} | {n for n, _ in (ctx or {}).get("later_calls", [])}
    if DESTRUCTIVE_ASK.search(s["prompt"]) or turn & DESTRUCTIVE_TOOLS:
        return []
    return [f"clarify destructive: {DESTRUCTIVE_WORDS.search(text).group(0)!r} for a request that removes nothing"]


# Real work in a single-source final: a clock time compared with now, items past due or still ahead,
# free/busy against the asked time, or a quoted day word placed on a date.
_COMPUTE = re.compile(r"\bit's \d{1,2}(?::\d\d)? [AP]M\b|\bit's \w+day \w{3} \d{1,2}, \d{1,2}(?::\d\d)? [AP]M\b|"
                      r"\bpast due\b|\bstill ahead\b|\balready past\b|\bfree\b|\bbusy\b|"
                      r"'[^']+'[^.]*?\b(?:is|means)\b[^.]*?\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}\b",
                      re.I)


def single_source_problems(meta: dict, s: dict, text: str) -> list[str]:
    """A THINK final over one source must do date/time or free/busy work, not restate the tool values."""
    if (meta.get("family") or "") not in SINGLE_SOURCE or s["kind"] != "narrate" or _COMPUTE.search(text or ""):
        return []
    return ["single source: a one-source final thinks only for a date/time or free/busy computation"]


def plan_dates(meta: dict, messages: list[dict], ctx: dict | None = None) -> list[_dt.date]:
    """Dates in the arguments this step commits to (its own call, or for a bare load the loaded tool's next call)."""
    return call_dates(_plan_calls(analyze(meta, messages), ctx))


def check(meta: dict, messages: list[dict], text: str | None, ctx: dict | None = None) -> list[str]:
    """Every rule a THINK step's thought must pass (build and validate share it)."""
    s = analyze(meta, messages)
    errs = thought_problems(text, s["target_text"], (ctx or {}).get("final", ""))
    if errs:
        return errs
    clock = parse_clock(meta)
    errs = [f"relative day: {e}" for e in relative_day_problems(text, clock)]
    errs += success_on_failure(text, s["results"])
    errs += clarify_problems(s, text, ctx)
    errs += single_source_problems(meta, s, text)
    dates = plan_dates(meta, messages, ctx)
    if dates:
        return errs + date_problems(text, clock, dates)
    return errs + grounding_problems(text, clock, s["all_results"] + [s["prompt"]])


def make_thought(meta: dict, messages: list[dict], ctx: dict | None = None, avoid: set[str] = frozenset()):
    """(thought, problems): the first phrasing (over salts, then terse) that passes check() and is not in
    `avoid` (the neighbouring examples' thoughts)."""
    ctx = dict(ctx or {})
    first = None
    for terse in (False, True):
        for salt in range(8):
            ctx.update(salt=salt or "", terse=terse)
            t = thought(meta, messages, ctx)
            errs = check(meta, messages, t, ctx)
            if first is None:
                first = (t, errs)
            if not errs and t not in avoid:
                return t, []
    return first[0], first[1] or ["duplicate of a neighbour"]


def thought_problems(text: str | None, target_text: str, final: str = "") -> list[str]:
    errs = []
    if not text or not text.strip():
        return ["empty"]
    if len(text.split()) > MAX_WORDS:
        errs.append(f"{len(text.split())} words")
    if NOISE.search(text):
        errs.append(f"noise: {NOISE.search(text).group(0)!r}")
    if MECHANICS.search(text):
        errs.append(f"mechanics: {MECHANICS.search(text).group(0)!r}")
    if USER_PRONOUN.search(text):
        errs.append(f"user pronoun: {USER_PRONOUN.search(text).group(0)!r}")
    if BOILERPLATE.search(text):
        errs.append(f"boilerplate: {BOILERPLATE.search(text).group(0)!r}")
    if CARD.search(re.sub(r"“[^”]*”", " ", text)):         # quoted titles are data ("birthday card")
        errs.append("card: the approval card is UI the model never sees; plan the call itself")
    for ref in (target_text, final):
        if ref and jaccard(text, ref) >= JACCARD_MAX:
            errs.append(f"too close to the reply ({jaccard(text, ref):.2f})")
    return errs
