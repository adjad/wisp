#!/usr/bin/env python3
"""Does a final answer say what its tool results and its gold contract require?

The 2026-11-07 audit found that "Everything looks good. You are all set." passed both the chooser and the validator on a
question spanning several sources, because the builder's ``sc.base_ref`` never reached the saved rows. The builder now
writes it to ``meta.gold``, and this module is the one implementation of the check. ``choose_r5.py`` calls it on every
sample and ``builder/validate.py`` calls it on every final in the finished build, so the two can't drift. Stdlib only, so
it also runs on the GPU node.

``final_problems(text, meta, messages)`` returns ``[(key, detail)]``, empty when the answer passes. Two layers:

- ``grounding_problems`` applies to any final after a tool read: amounts, clock times, weekdays and dates must come from
  the conversation, a turn with no write can't claim an effect, and a generic reply can't stand in for what was read.
- ``contract_problems`` applies to rows with a ``meta.gold``. ``kind`` picks the rules:

  ``all_sources``      ``hit`` (source -> any-of facts; every source needs one), ``empty``, ``read``, ``failed`` (source ->
                       why it couldn't be read), ``conflict`` ({old, new}), ``events`` ([{title, day, time}]), ``prohibit``
                       (facts only an unread source holds), ``must_say_absent`` (nothing was found, and the answer says so).
  ``chat_contrast``    ``forbid``: no memory talk in ordinary chat.
  ``memory_contrast``  ``mode``: chat | in_chat | saved_hit | saved_miss, with ``facts`` (any-of) for the last three.
  ``session``          recall facts, checked by ``choose.py``'s own recall rules, which this module doesn't copy.

Fact matching accepts paraphrase on purpose, since base writes in its own words. A number or time in a fact must appear
exactly (after normalising "3:30 PM" and "3:30pm"), and at least half of the fact's content words must appear, stems
compared. No style is scored here.
"""
from __future__ import annotations

import datetime as dt
import math
import re

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
          "december")
_MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_WD_RE = r"(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:rs(?:day)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
TIME = re.compile(r"(?<![\d:/])(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?", re.I)
CLOCK_ONLY = re.compile(r"(?<![\d:/$.])(\d{1,2}):(\d{2})(?![\d:])")
FULL_WEEKDAY = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b", re.I)
ANY_WEEKDAY = re.compile(rf"\b{_WD_RE}\b\.?", re.I)
MONTH_DAY = re.compile(rf"\b{_MONTH_RE}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!:)", re.I)
ISO_DAY = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
CLOCK_LINE = re.compile(r"current date and time is \w+, (\w+) (\d{1,2}), (\d{4}) at (\d{1,2}):(\d{2}) ([AP])M", re.I)

SOURCE_WORDS = {"mail": ("email", "emails", "e-mail", "inbox", "mail"),
                "messages": ("text", "texts", "message", "messages", "imessage", "imessages"),
                "notes": ("note", "notes"),
                "calendar": ("calendar", "event", "events", "schedule"),
                "reminders": ("reminder", "reminders"),
                "memory": ("memory", "memories", "saved"),
                "weather": ("forecast", "weather")}
_ABSENCE = (r"\b(?:no|nothing|none|not any|without any|couldn['’]?t find|could not find|didn['’]?t find|did not find|"
            r"can['’]?t find|cannot find|didn['’]?t see|did not see|don['’]?t see|do not see|no sign|no mention|"
            r"isn['’]?t anything|aren['’]?t any|hasn['’]?t got|haven['’]?t got|came up empty|turned up empty|came back empty)\b")
ABSENCE = re.compile(_ABSENCE, re.I)
NOT_ABSENT = re.compile(r"\b(?:no problem|no worries|no rush|no notes? needed|no later than|nothing else|no other|no further|"
                        r"no more|nothing more|nothing further|no need|no matter|not any more|no longer)\b", re.I)
CLAUSE = re.compile(r"[.!?;\n]+|,?\s+(?:but|however|though|although|while|whereas|yet)\b|\s[—–]\s|:\s", re.I)
# "Mt. Tabor hike" is one phrase: a title split at its abbreviation hides the wrong day said next to it.
SENTENCE = re.compile(r"(?<!\bMt\.)(?<!\bSt\.)(?<!\bDr\.)(?<!\bMr\.)(?<!\bMs\.)(?<!\bMrs\.)(?<=[.!?])\s+|\n+")
GENERIC = re.compile(r"(?:\beverything\b.{0,20}\b(?:looks?|seems?|is)\b.{0,12}\b(?:good|fine|great|okay|ok|set)\b|"
                     r"\byou(?:['’]re| are)\s+all\s+set\b|\ball\s+good\b|\bnothing\s+to\s+worry\b|"
                     r"\b(?:looks?|seems?)\s+(?:good|fine|great)\s+to\s+me\b)", re.I)
INVENTED = re.compile(
    r"\b(?:i(?:['’]ve| have)?|we(?:['’]ve| have)?)\s+(?:just\s+|already\s+|also\s+|now\s+|gone\s+ahead\s+and\s+)?"
    r"(?:sent|added|scheduled|created|booked|saved|deleted|removed|moved|cancell?ed|updated|emailed|texted|replied|forwarded|"
    r"marked|rescheduled|set\s+(?:a|an|up|the|your))\b|"
    r"\b(?:i|we)\s+went\s+ahead\s+and\b|\byou(?:['’]re| are)\s+all\s+set\b|"
    r"\b(?:reminder|event|message|email|text|note)\s+(?:has|have)\s+been\s+(?:sent|added|set|scheduled|created|moved|saved|deleted)\b",
    re.I)
COULD_NOT = re.compile(r"\b(?:couldn['’]?t|could not|can['’]?t|cannot|wasn['’]?t able|was not able|unable|not able|"
                       r"no access|don['’]?t have access|do not have access|needs? access|without access|"
                       r"isn['’]?t connected|not connected|turn on|turned off|is off|permission|"
                       r"not available|unavailable|didn['’]?t work|failed|blocked|hasn['’]?t been)\b", re.I)
STALE = re.compile(r"\b(?:no change|not changed|hasn['’]?t (?:changed|moved)|has not (?:changed|moved)|didn['’]?t (?:change|move)|"
                   r"still (?:at|on) the same|unchanged|same time|not been moved|wasn['’]?t moved)\b", re.I)
MEMORY_TALK = re.compile(
    r"\b(?:(?:our|my|your|the) memory|memory (?:has|is|doesn['’]t|does not)|nothing (?:is |was )?(?:saved|stored)|"
    r"(?:don['’]t|do not) have (?:anything|that) saved|(?:haven['’]t|have not) (?:saved|stored)|on file|"
    r"we haven['’]?t talked|haven['’]?t (?:talked|mentioned|told)|in this chat)\b", re.I)
HONEST_NONE = re.compile(rf"{_ABSENCE}|\b(?:haven['’]?t|have not|don['’]?t|do not|didn['’]?t)\b", re.I)
_STOP = {"a", "an", "and", "are", "at", "be", "before", "by", "for", "from", "in", "is", "it", "its", "me", "my", "of", "on",
         "or", "our", "out", "that", "the", "their", "them", "then", "this", "to", "up", "was", "will", "with", "you", "your", "i"}


# ------------------------------------------------------------------ normalising
def _amount(raw: str) -> str:
    v = raw.replace(",", "")
    return v[:-3] if v.endswith(".00") else v


def amounts(text: str) -> set[str]:
    return {_amount(m) for m in AMOUNT.findall(text or "")}


def times(text: str) -> set[str]:
    """Clock times with a meridiem, as 'H:MM a'. 'noon' is 12:00 p."""
    low = text or ""
    out = {f"{int(h)}:{mm or '00'} {ap.lower()}" for h, mm, ap in TIME.findall(low)}
    if re.search(r"\bnoon\b", low, re.I):
        out.add("12:00 p")
    return out


def bare_clocks(text: str) -> set[str]:
    """Every 'H:MM' in the text, with or without a meridiem: what a time without AM or PM is compared with."""
    low = text or ""
    return {f"{int(h)}:{mm}" for h, mm in CLOCK_ONLY.findall(low)} | {f"{int(h)}:{mm or '00'}" for h, mm, _ in TIME.findall(low)}


def stem(word: str) -> str:
    w = word.lower().strip("'’")
    for suf in ("ings", "ing", "ies", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:['’][a-z]+)?", (text or "").lower())


def stems(text: str) -> set[str]:
    return {stem(w) for w in words(text)}


def fact_present(fact: str, text: str) -> bool:
    """A number, amount or time in the fact must appear exactly. At least half of its other content words must appear."""
    need_amounts, need_times = amounts(fact), times(fact)
    if need_amounts - amounts(text):
        return False
    if need_times and not need_times <= times(text) and not ({t.split()[0] for t in need_times} <= bare_clocks(text) and not times(text) & need_times and _hour_words_ok(need_times, text)):
        return False
    bare = re.sub(rf"{AMOUNT.pattern}|{TIME.pattern}", " ", fact, flags=re.I)
    tokens = [w for w in words(bare) if w not in _STOP]
    numbers = [w for w in tokens if any(c.isdigit() for c in w)]
    have_words = set(words(text))
    if any(n not in have_words for n in numbers):
        return False
    content = [stem(w) for w in tokens if w not in numbers]
    if not content:
        return bool(numbers or need_amounts or need_times)
    have = stems(text)
    return sum(c in have for c in content) * 2 >= len(content) and any(c in have for c in content)


def _every_word_present(fact: str, text: str) -> bool:
    """Stricter than fact_present: for a fact the answer must NOT hold, half a phrase isn't evidence."""
    need = [stem(w) for w in words(fact) if w not in _STOP]
    have = stems(text)
    return bool(need) and all(n in have for n in need)


def _hour_words_ok(need_times, text) -> bool:
    """'7:30' with no AM or PM still says 7:30 when the text gives no other meridiem for it."""
    low = text or ""
    for t in need_times:
        clock = t.split()[0]
        h, mm = clock.split(":")
        other = "a" if t.endswith("p") else "p"
        if f"{h}:{mm} {other}" in times(low):
            return False
    return True


def any_fact_present(facts, text: str) -> bool:
    return any(fact_present(f, text) for f in facts or [])


def _context_text(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts += [p.get("text", "") for p in c if isinstance(p, dict)]
        for tc in m.get("tool_calls") or []:
            parts.append(str((tc.get("function") or {}).get("arguments")))
    return "\n".join(parts)


def clock_of(messages: list[dict]) -> dt.datetime | None:
    for m in messages:
        c = m.get("content")
        if m.get("role") == "system" and isinstance(c, str):
            g = CLOCK_LINE.search(c)
            if g:
                try:
                    return dt.datetime(int(g.group(3)), MONTHS.index(g.group(1).lower()) + 1, int(g.group(2)))
                except ValueError:
                    return None
    return None


def _month_index(name: str) -> int:
    return next(i + 1 for i, m in enumerate(MONTHS) if m.startswith(name.lower()[:3]))


def _full_weekday(raw: str) -> str:
    return next(w for w in WEEKDAYS if w.startswith(raw.lower()[:3]))


def _date_of(month: int, day: int, clock: dt.datetime | None) -> dt.date | None:
    """The date nearest the clock with that month and day."""
    if clock is None:
        return None
    best = None
    for y in (clock.year - 1, clock.year, clock.year + 1):
        try:
            d = dt.date(y, month, day)
        except ValueError:
            continue
        if best is None or abs((d - clock.date()).days) < abs((best - clock.date()).days):
            best = d
    return best


def _known_dates(context: str, clock: dt.datetime | None) -> tuple[set[str], set[tuple[int, int]]]:
    """(weekdays, (month, day) pairs) the conversation supports, including ones that follow from a stated date."""
    wd = {_full_weekday(m) for m in ANY_WEEKDAY.findall(context)}
    wd |= {m.lower() for m in FULL_WEEKDAY.findall(context)}     # "the club meets on Tuesdays" supports "Tuesday"
    md = {(_month_index(a), int(b)) for a, b in MONTH_DAY.findall(context)}
    for y, mo, d in ISO_DAY.findall(context):
        try:
            when = dt.date(int(y), int(mo), int(d))
        except ValueError:
            continue
        md.add((when.month, when.day))
        wd.add(WEEKDAYS[when.weekday()])
    for mo, d in list(md):
        when = _date_of(mo, d, clock)
        if when:
            wd.add(WEEKDAYS[when.weekday()])
    if clock is not None:
        for k in (0, 1):
            when = clock.date() + dt.timedelta(days=k)
            wd.add(WEEKDAYS[when.weekday()])
            md.add((when.month, when.day))
    return wd, md


def _derived_amounts(known: set[str], context: str) -> set[str]:
    """Sums, differences and small multiples of stated amounts: arithmetic on what was read is not an invention."""
    vals = [float(a) for a in known]
    ints = {int(w) for w in re.findall(r"\b(\d{1,2})\b", context) if 1 < int(w) <= 20}
    ints |= {2, 3, 4, 5, 6}
    out = set()
    for a in vals:
        for n in ints:
            out.add(a * n)
            out.add(a / n)
        for b in vals:
            out.add(a + b)
            out.add(abs(a - b))
    return {_amount(f"{x:.2f}") for x in out} | {str(int(x)) for x in out if float(x).is_integer()}


# ------------------------------------------------------------------ the checks
def date_problems(text: str, context: str, clock: dt.datetime | None) -> list[tuple[str, str]]:
    out = []
    known_wd, known_md = _known_dates(context, clock)
    for m in FULL_WEEKDAY.findall(text or ""):
        if m.lower() not in known_wd:
            out.append(("fact:wrong_weekday", m))
    for mo, d in MONTH_DAY.findall(text or ""):
        pair = (_month_index(mo), int(d))
        if pair in known_md:
            continue
        when = _date_of(pair[0], pair[1], clock)
        # a date the model worked out from a weekday it read is fine when the arithmetic is right
        if when is not None and WEEKDAYS[when.weekday()] in known_wd and abs((when - clock.date()).days) <= 60:
            continue
        out.append(("fact:wrong_date", f"{mo} {d}"))
    for wd, mo, d in re.findall(rf"\b{_WD_RE}\b\.?,?\s+{_MONTH_RE}\.?\s+(\d{{1,2}})\b", text or "", re.I):
        when = _date_of(_month_index(mo), int(d), clock)
        if when is not None and WEEKDAYS[when.weekday()] != _full_weekday(wd):
            out.append(("fact:weekday_date_mismatch", f"{wd} {mo} {d}"))
    return out


def grounding_problems(text: str, messages: list[dict], wrote: bool, read_results: list[str]) -> list[tuple[str, str]]:
    """What any final after a read must get right. ``read_results``: this turn's tool results."""
    out = []
    t = text or ""
    context = _context_text(messages)
    known = amounts(context)
    extra = amounts(t) - known
    if extra:
        derived = _derived_amounts(known, context)
        out += [("fact:wrong_amount", f"${a}") for a in sorted(extra) if a not in derived]
    known_t, known_c = times(context), bare_clocks(context)
    for tm in sorted(times(t)):
        # 5 PM is right for "5:00 PM"; a meridiem the context only gives as 17:00 is left alone
        if tm not in known_t and tm.split()[0] not in known_c and not _hour_only_known(tm, context):
            out.append(("fact:wrong_time", tm))
    out += date_problems(t, context, clock_of(messages))
    if not wrote:
        g = INVENTED.search(t)
        if g:
            out.append(("fact:invented_action", g.group(0)))
    joined = "\n".join(read_results)
    if read_results and GENERIC.search(t) and not _shares_a_fact(t, joined):
        out.append(("fact:generic", GENERIC.search(t).group(0)))
    return out


def _hour_only_known(tm: str, context: str) -> bool:
    """'2 pm' (a Brief's form) and '2:00 PM' are the same. TIME already folds them, so only a bare '14:00' is left."""
    h, rest = tm.split(":")
    mm, ap = rest.split()
    hour = int(h) % 12 + (12 if ap == "p" else 0)
    return f"{hour:02d}:{mm}" in context or f"{hour}:{mm}" in context


_NUMERIC = re.compile(r"\d")


def _shares_a_fact(text: str, results: str) -> bool:
    """A number, or a word of 5+ letters, that the results also hold. Boilerplate words aren't facts."""
    res = set(words(results)) - {"email", "emails", "calendar", "reminder", "reminders", "message", "messages", "notes", "today",
                                 "tomorrow", "everything", "looks", "right", "there", "about", "thing", "things"}
    for w in words(text):
        if w in res and (len(w) >= 5 or _NUMERIC.search(w)):
            return True
    return False


def _clauses(text: str) -> list[str]:
    return [c.strip() for c in CLAUSE.split(re.sub(r"[*_`]+", "", text or "")) if c and c.strip()]


def _sources_named(clause: str, m: re.Match) -> set[str]:
    """Sources a clause's absence marker is about: named after it, or just before it ('your calendar has nothing')."""
    after = words(clause[m.start():])[:10]
    before = words(clause[:m.start()])[-4:]
    return {s for s, kw in SOURCE_WORDS.items() if any(w in kw for w in after) or any(w in kw for w in before)}


def absence_claims(text: str) -> list[tuple[str, set[str]]]:
    """Each clause that says something isn't there, with the sources it names (empty for a blanket claim)."""
    out = []
    for clause in _clauses(text):
        if NOT_ABSENT.search(clause):
            continue
        m = ABSENCE.search(clause)
        if m:
            out.append((clause, _sources_named(clause, m)))
    return out


def _event_problems(text: str, events: list[dict], clock: dt.datetime | None, conflict: bool) -> list[tuple[str, str]]:
    out = []
    for e in events or []:
        title_stems = {stem(w) for w in words(e["title"]) if w not in _STOP and len(w) > 3}
        for sentence in SENTENCE.split(re.sub(r"[*_`]+", "", text or "")):
            have = stems(sentence)
            if not title_stems or len(title_stems & have) * 2 < len(title_stems):
                continue
            days = {m.lower() for m in FULL_WEEKDAY.findall(sentence)}
            if days and e.get("day") and e["day"].lower() not in days:
                out.append(("source:wrong_event_day", f"{e['title']}: {sorted(days)}"))
            if clock is not None and e.get("day"):
                today, tomorrow = WEEKDAYS[clock.weekday()], WEEKDAYS[(clock.weekday() + 1) % 7]
                low = sentence.lower()
                if "tomorrow" in low and e["day"].lower() != tomorrow and not days:
                    out.append(("source:wrong_event_day", f"{e['title']}: tomorrow"))
                if re.search(r"\btoday\b", low) and e["day"].lower() != today and not days:
                    out.append(("source:wrong_event_day", f"{e['title']}: today"))
            said = times(sentence)
            if not conflict and said and e.get("time"):
                want = times(e["time"])
                if want and not (said & want):
                    out.append(("source:wrong_event_time", f"{e['title']}: {sorted(said)}"))
    return out


def all_sources_problems(text: str, gold: dict, messages: list[dict]) -> list[tuple[str, str]]:
    out = []
    t = text or ""
    hit, read, empty = gold.get("hit") or {}, gold.get("read") or [], gold.get("empty") or []
    failed = gold.get("failed") or {}
    conflict = gold.get("conflict") or None
    clean = re.sub(r"[*_`]+", "", t)
    for source, facts in hit.items():
        if not any_fact_present(facts, clean):
            out.append((f"source:missing:{source}", (facts or [""])[0]))
    for fact in gold.get("all_of") or []:
        if not fact_present(fact, clean):
            out.append(("source:missing_item", fact))
    if hit and GENERIC.search(clean) and len(clean) < 200:
        out.append(("source:generic", GENERIC.search(clean).group(0)))
    claims = absence_claims(clean)
    reachable = set(read)
    for clause, named in claims:
        if not named:
            if hit and not re.search(r"\b(?:else|other)\b", clause, re.I):
                out.append(("source:blanket_absence", clause[:80]))
            continue
        for source in named & set(hit):
            out.append(("source:absence_contradicts_hit", f"{source}: {clause[:80]}"))
        for source in (named - reachable) & set(SOURCE_WORDS):
            if source != "memory":
                out.append(("source:absence_unread", f"{source}: {clause[:80]}"))
        for source in named & set(failed):
            if not COULD_NOT.search(clause):
                out.append(("source:failed_as_absence", f"{source}: {clause[:80]}"))
    if gold.get("must_say_absent") and not claims and not HONEST_NONE.search(clean):
        out.append(("source:absence_not_stated", clean[:80]))
    for source in failed:
        said = any(COULD_NOT.search(c) and any(w in SOURCE_WORDS[source] for w in words(c)) for c in _clauses(clean))
        if not said:
            out.append((f"source:failed_hidden:{source}", failed[source]))
    if conflict:
        old, new = conflict["old"], conflict["new"]
        if not fact_present(new, clean):
            out.append(("source:conflict_missing_new", new))
        if not fact_present(old, clean):
            out.append(("source:conflict_missing_old", old))
        g = STALE.search(clean)
        if g:
            out.append(("source:conflict_stale", g.group(0)))
    context = _context_text(messages)
    for fact in gold.get("prohibit") or []:
        if _every_word_present(fact, clean) and fact.lower() not in context.lower():
            out.append(("source:unread_fact", fact))
    out += _event_problems(clean, gold.get("events") or [], clock_of(messages), bool(conflict))
    return out


def memory_problems(text: str, gold: dict) -> list[tuple[str, str]]:
    out = []
    mode = gold.get("mode")
    clean = re.sub(r"[*_`]+", "", text or "")
    if mode in ("chat", "in_chat"):
        g = MEMORY_TALK.search(clean)
        if g:
            out.append(("memory:talk_in_chat", g.group(0)))
    if mode in ("in_chat", "saved_hit") and not any_fact_present(gold.get("facts"), clean):
        out.append(("memory:missing_fact", (gold.get("facts") or [""])[0]))
    if mode == "saved_miss":
        if not HONEST_NONE.search(clean):
            out.append(("memory:absence_not_stated", clean[:80]))
        if any_fact_present(gold.get("prohibit"), clean):
            out.append(("memory:invented", (gold.get("prohibit") or [""])[0]))
    return out


SENT_CLAIM = re.compile(r"\b(?:i(?:['’]ve| have)?\s+(?:just\s+|already\s+)?(?:sent|emailed|delivered)|(?:has|have)\s+been\s+sent|is\s+on\s+its\s+way)\b", re.I)


def write_contrast_problems(text: str, gold: dict) -> list[tuple[str, str]]:
    """The other side of a write: nothing happened, so nothing may be claimed. A draft is called a draft."""
    out = []
    mode = gold.get("mode")
    clean = re.sub(r"[*_`]+", "", text or "")
    if mode in ("word", "read", "clarify", "remark"):
        g = INVENTED.search(clean)
        if g:
            out.append(("write:invented_action", g.group(0)))
    if mode == "draft":
        g = SENT_CLAIM.search(clean)
        if g:
            out.append(("write:claims_sent", g.group(0)))
        if not re.search(r"\bdraft", clean, re.I):
            out.append(("write:draft_not_stated", clean[:80]))
    if mode == "clarify" and "?" not in clean:
        out.append(("write:no_question", clean[:80]))
    if mode in ("write", "read") and gold.get("facts") and not any_fact_present(gold["facts"], clean):
        out.append(("write:missing_fact", gold["facts"][0]))
    return out


def contract_problems(text: str, gold: dict | None, messages: list[dict]) -> list[tuple[str, str]]:
    if not gold:
        return []
    kind = gold.get("kind")
    if kind == "all_sources":
        return all_sources_problems(text, gold, messages)
    if kind == "chat_contrast":
        pat = gold.get("forbid")
        g = re.search(pat, text or "", re.I) if pat else None
        return [("chat:memory_talk", g.group(0))] if g else []
    if kind == "memory_contrast":
        return memory_problems(text, gold)
    if kind == "write_contrast":
        return write_contrast_problems(text, gold)
    return []


def turn_results(messages: list[dict]) -> tuple[list[str], list[dict]]:
    """(tool results, assistant call steps) after the last user message, target excluded."""
    last = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    after = messages[last + 1:]
    return ([str(m.get("content") or "") for m in after if m.get("role") == "tool"],
            [m for m in after if m.get("role") == "assistant" and m.get("tool_calls")])


def final_problems(text: str, meta: dict | None, messages: list[dict]) -> list[tuple[str, str]]:
    """Every problem of one final. ``messages``: the request only, without the target."""
    meta = meta or {}
    results, steps = turn_results(messages)
    wrote = bool(meta.get("turn_writes") or meta.get("writes"))
    out = []
    if steps:
        out += grounding_problems(text, messages, wrote, results)
    out += contract_problems(text, meta.get("gold"), messages)
    seen, unique = set(), []
    for key, detail in out:
        if (key, detail) not in seen:
            seen.add((key, detail))
            unique.append((key, detail))
    return unique


# ------------------------------------------------------------------ traces
def _trigrams(text: str) -> set[str]:
    s = " ".join(words(text))
    return {s[i:i + 3] for i in range(max(0, len(s) - 2))}


def echo_score(thought: str, answer: str) -> float:
    """How much of the thought's character trigrams the answer repeats (1.0 = the thought is contained in the answer)."""
    a, b = _trigrams(thought), _trigrams(answer)
    return len(a & b) / max(1, len(a)) if a else 0.0


def trace_shape(thought: str | None, answer: str) -> dict:
    """Measures only. Crusoe's echo test: high 'valid reasoning' can hide a trace that just restates the answer."""
    t = (thought or "").strip()
    return {"words": len(t.split()), "echo": round(echo_score(t, answer), 3) if t else 0.0, "empty": not t}


def percentile(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] if lo == hi else round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)
