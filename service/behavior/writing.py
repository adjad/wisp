"""Deterministic writing measurements: how the user writes, as numbers.

Text is read in memory only. ``measure`` turns one message into a small dict of
integers plus at most two short phrases that come from a CLOSED lexicon of
greetings and sign-offs, so a name, an address or a sentence can never be kept
as a "phrase". The text itself is dropped by the caller. No model is involved
anywhere in this module: counts, ratios and regular expressions only.

The module is pure standard library, like the rest of ``service.behavior``.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable

CHANNELS = ("email", "message", "note", "reminder")
CHANNEL_LABEL = {"email": "sent emails", "message": "sent messages", "note": "notes",
                 "reminder": "reminder titles"}
CHANNEL_UNIT = {"email": "emails", "message": "messages", "note": "notes", "reminder": "reminders"}
# Below this many samples a channel shows nothing at all (not "0%", not a guess).
MIN_SAMPLES = 20
# A greeting or sign-off is shown only once it has been used this often.
MIN_PHRASE_USES = 3
TOP_PHRASES = 3
# Only the newest samples are summarized: how you write changes. The store holds more
# (integers only) so a message trimmed away is never "unseen" and read again.
MAX_SAMPLES_PER_CHANNEL = 1500
MAX_STORED_PER_CHANNEL = 20_000
MAX_TEXT_CHARS = 20_000          # a longer text is measured on its first part only
MAX_PHRASE_CHARS = 40
PHRASE_CHANNELS = ("email", "message")

# ---------------------------------------------------------------- lexicons
_GREETING_WORDS = ("good morning", "good afternoon", "good evening", "hi there", "hey there",
                   "hello there", "hiya", "howdy", "hello", "hey", "hi", "yo", "dear",
                   "greetings", "morning", "afternoon", "evening", "heya", "hola")
_GREETING_TAIL = ("all", "everyone", "everybody", "team", "folks", "guys", "both", "there", "friends")
_GREETING = re.compile(
    r"^(?P<g>(?:" + "|".join(re.escape(w) for w in _GREETING_WORDS) + r")"
    r"(?:\s+(?:" + "|".join(_GREETING_TAIL) + r"))?)(?![A-Za-z])", re.I)

_SIGNOFF_WORDS = ("thanks so much", "thank you so much", "thanks again", "many thanks",
                  "thank you", "thanks", "thx", "ty", "with thanks", "best regards", "best wishes",
                  "all the best", "kind regards", "warm regards", "warmest regards", "regards",
                  "warmly", "best", "cheers", "sincerely", "yours sincerely", "yours truly",
                  "yours", "respectfully", "take care", "talk soon", "talk later", "see you",
                  "see you soon", "ttyl", "love", "xoxo", "appreciate it", "much appreciated")
_SIGNOFF_ALT = "|".join(re.escape(w) for w in sorted(_SIGNOFF_WORDS, key=len, reverse=True))
_SIGNOFF = re.compile(r"^(?P<s>(?:" + _SIGNOFF_ALT + r"))\s*(?P<p>[,!.]*)\s*(?P<rest>.*)$", re.I)

# ---------------------------------------------------------------- stripping
_REPLY_HEADER = re.compile(
    r"^\s*(?:on\s.{3,200}\swrote:?|le\s.{3,200}\sa écrit\s*:?|-{2,}\s*(?:original|forwarded) message\s*-{2,}"
    r"|begin forwarded message:?|_{5,}|from:\s.+|sent from my .+|get outlook for .+)\s*$", re.I)
_ON_WROTE_START = re.compile(r"^\s*on\s.{3,200}$", re.I)
_WROTE_END = re.compile(r"wrote:?\s*$", re.I)
_SIGNATURE_DELIM = re.compile(r"^\s*(?:--|__|—)\s*$")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE_LINE = re.compile(r"^[\s\d()+.\-x]{7,}$")


def strip_quoted(text: str) -> str:
    """Drop quoted replies, forwards and mobile footers; keep what the user typed."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        if _REPLY_HEADER.match(line):
            break
        # "On Tue, 3 Nov, Someone <x@y> wrote:" wrapped over two lines.
        if _ON_WROTE_START.match(line) and i + 1 < len(lines) and _WROTE_END.search(lines[i + 1]):
            break
        if _SIGNATURE_DELIM.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _trim_signature(lines: list[str]) -> tuple[list[str], str]:
    """Split the sign-off (and everything after it: the name, title, phone) from the body.

    Returns (body_lines, signoff_phrase)."""
    window = range(len(lines) - 1, max(-1, len(lines) - 9), -1)
    for i in window:
        phrase = _signoff_phrase(lines[i])
        if phrase:
            return lines[:i], phrase
    # No sign-off: drop trailing lines that are only a phone number, URL, or address.
    end = len(lines)
    while end and (not lines[end - 1].strip() or _PHONE_LINE.match(lines[end - 1].strip())
                   or _URL.fullmatch(lines[end - 1].strip()) or _EMAIL.fullmatch(lines[end - 1].strip())):
        end -= 1
    return lines[:end], ""


def _signoff_phrase(line: str) -> str:
    s = line.strip()
    if not s or len(s) > 60:
        return ""
    m = _SIGNOFF.match(s)
    if not m:
        return ""
    rest = m.group("rest").strip()
    # "Thanks" alone, or "Thanks," / "Best," followed by at most a short name.
    if not rest:
        return _tidy(m.group("s"))
    if m.group("p") and "," in m.group("p") and len(rest.split()) <= 3 and not re.search(r"[.!?]$", rest):
        return _tidy(m.group("s"))
    return ""


def _tidy(phrase: str) -> str:
    phrase = " ".join(phrase.split())
    return phrase[:MAX_PHRASE_CHARS]


def _greeting_phrase(first_line: str) -> str:
    m = _GREETING.match(first_line.strip())
    return _tidy(m.group("g")) if m else ""


# ---------------------------------------------------------------- counting
_WORD = re.compile(r"[^\W_][\w'’\-]*", re.UNICODE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")
_ELLIPSIS = re.compile(r"\.{3,}|…")
_CONTRACTION = re.compile(
    r"\b(?:\w+n['’]t|(?:i|you|we|they|he|she|it|that|there|what|who|let)['’](?:m|re|ve|ll|d|s)"
    r"|y['’]all|ain['’]t|gonna|wanna|gotta)\b", re.I)
_EXPANDED = re.compile(
    r"\b(?:(?:do|does|did|is|are|was|were|have|has|had|would|could|should|will|can)\s+not|cannot"
    r"|i\s+(?:am|will|have|would)|(?:you|we|they)\s+(?:are|will|have|would)"
    r"|(?:it|that|there|he|she)\s+(?:is|will|would)|let\s+us)\b", re.I)


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return (0x1F300 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x1F000 <= cp <= 0x1F2FF
            or 0x2B50 <= cp <= 0x2B55 or cp in (0x2764, 0x203C, 0x2049))


def measure(channel: str, text: str) -> dict[str, Any] | None:
    """One message -> integers (and two lexicon phrases). None when there is nothing to measure.

    Never returns or keeps any part of the text beyond the lexicon phrases."""
    if channel not in CHANNELS or not isinstance(text, str):
        return None
    text = text[:MAX_TEXT_CHARS]
    body = strip_quoted(text) if channel in ("email", "message") else text.replace("\r", "").strip()
    lines = [ln for ln in body.split("\n")]
    greeting = signoff = ""
    if channel in PHRASE_CHANNELS:
        first = next((ln for ln in lines if ln.strip()), "")
        greeting = _greeting_phrase(first)
        lines, signoff = _trim_signature(lines)
    body = "\n".join(lines).strip()
    body = _URL.sub(" ", body)
    body = _EMAIL.sub(" ", body)
    words = _WORD.findall(body)
    if not words:
        return None
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(body) if s and s.strip()]
    sentences = [s for s in sentences if _WORD.search(s)]
    if not sentences:
        return None
    # The greeting opens the first line; it is not a sentence of the message.
    caps = sum(1 for s in sentences if (c := next((ch for ch in s if ch.isalpha()), "")) and c.isupper())
    lows = sum(1 for s in sentences if (c := next((ch for ch in s if ch.isalpha()), "")) and c.islower())
    letters = [ch for ch in body if ch.isalpha()]
    return {
        "chars": len(body), "words": len(words), "sentences": len(sentences),
        "emoji": sum(1 for ch in body if _is_emoji(ch)),
        "caps": caps, "lows": lows,
        "all_lower": 1 if letters and not any(ch.isupper() for ch in letters) else 0,
        "excl": 1 if "!" in body else 0, "quest": 1 if "?" in body else 0,
        "ellipsis": 1 if _ELLIPSIS.search(body) else 0,
        "contr": len(_CONTRACTION.findall(body)), "expanded": len(_EXPANDED.findall(body)),
        "greeting": greeting, "signoff": signoff,
    }


# ---------------------------------------------------------------- aggregation
def _pct(num: int, den: int) -> int:
    return int(round(100 * num / den)) if den else 0


def _top(counter: Counter, samples: int) -> list[dict[str, Any]]:
    out = []
    for phrase, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0].lower(), kv[0])):
        if n >= MIN_PHRASE_USES and n * 20 >= samples:           # used at least once in 20 messages
            out.append({"phrase": phrase, "count": n})
        if len(out) == TOP_PHRASES:
            break
    return out


def summarize(channel: str, samples: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Aggregate stored per-message integers. None below MIN_SAMPLES (show nothing)."""
    rows = [s for s in samples if isinstance(s, dict) and isinstance(s.get("words"), int)]
    n = len(rows)
    if n < MIN_SAMPLES:
        return None

    def total(key: str) -> int:
        return sum(int(r.get(key) or 0) for r in rows)

    sentences, words = total("sentences"), total("words")
    starts = total("caps") + total("lows")
    contractions, expanded = total("contr"), total("expanded")
    greetings: Counter = Counter(r["greeting"] for r in rows if r.get("greeting"))
    signoffs: Counter = Counter(r["signoff"] for r in rows if r.get("signoff"))
    stats: dict[str, Any] = {
        "channel": channel, "samples": n,
        "avg_words": round(words / n, 1), "avg_sentences": round(sentences / n, 1),
        "avg_sentence_words": round(words / sentences, 1) if sentences else 0.0,
        "emoji_pct": _pct(sum(1 for r in rows if r.get("emoji")), n),
        "caps_pct": _pct(total("caps"), starts),
        "all_lower_pct": _pct(total("all_lower"), n),
        "excl_pct": _pct(total("excl"), n), "quest_pct": _pct(total("quest"), n),
        "ellipsis_pct": _pct(total("ellipsis"), n),
        "contraction_pct": _pct(contractions, contractions + expanded) if contractions + expanded >= 5 else None,
        "greetings": _top(greetings, n) if channel in PHRASE_CHANNELS else [],
        "signoffs": _top(signoffs, n) if channel in PHRASE_CHANNELS else [],
    }
    return stats


def length_word(avg_words: float, channel: str) -> str:
    short, long = {"email": (40, 140), "message": (8, 25)}.get(channel, (12, 60))
    return "short" if avg_words < short else "long" if avg_words > long else "medium"


def statement(stats: dict[str, Any]) -> str:
    """The sentence the Learned window shows. Every number in ``stats`` that the
    user can see is here; nothing else is stored."""
    channel = stats["channel"]
    unit = CHANNEL_UNIT[channel]
    parts = [f"About {stats['avg_words']:g} words in {stats['avg_sentences']:g} "
             f"{'sentence' if stats['avg_sentences'] == 1 else 'sentences'} each"]
    if stats["all_lower_pct"] >= 50:
        parts.append(f"{stats['all_lower_pct']}% are written all in lowercase")
    else:
        parts.append(f"{stats['caps_pct']}% of sentences start with a capital")
    marks = [f"{stats['excl_pct']}% have an exclamation mark", f"{stats['quest_pct']}% a question mark"]
    if stats["ellipsis_pct"]:
        marks.append(f"{stats['ellipsis_pct']}% an ellipsis")
    parts.append(", ".join(marks))
    parts.append(f"emoji in {stats['emoji_pct']}%")
    if stats["contraction_pct"] is not None:
        parts.append(f"{stats['contraction_pct']}% of verbs contracted")
    text = f"Your {CHANNEL_LABEL[channel]} ({stats['samples']} {unit}): " + "; ".join(parts)
    if stats["greetings"]:
        text += "; you open with " + _quoted_list(stats["greetings"])
    if stats["signoffs"]:
        text += "; you close with " + _quoted_list(stats["signoffs"])
    return text + "."


def _quoted_list(items: list[dict[str, Any]]) -> str:
    return ", ".join(f"“{i['phrase']}” ({i['count']}×)" for i in items)


# ---------------------------------------------------------------- style card
STYLE_HEADER = "How the user writes (measured, not guessed):"


def style_line(stats: dict[str, Any]) -> str:
    """One short line of advice about a channel, composed from numbers."""
    channel = stats["channel"]
    bits = [f"{length_word(stats['avg_words'], channel)} (~{round(stats['avg_words']):d} words)"]
    if stats["all_lower_pct"] >= 50:
        bits.append("all lowercase")
    elif stats["caps_pct"] >= 85:
        bits.append("capitalized")
    if stats["contraction_pct"] is not None:
        bits.append("uses contractions" if stats["contraction_pct"] >= 60
                    else "avoids contractions" if stats["contraction_pct"] <= 25 else "mixes contractions")
    bits.append("rarely uses !" if stats["excl_pct"] < 10 else "often uses !" if stats["excl_pct"] >= 35 else "sometimes !")
    bits.append("emoji common" if stats["emoji_pct"] >= 25 else "no emoji" if stats["emoji_pct"] < 5 else "some emoji")
    if stats["greetings"]:
        bits.append(f"opens “{stats['greetings'][0]['phrase']}”")
    if stats["signoffs"]:
        bits.append(f"closes “{stats['signoffs'][0]['phrase']}”")
    return f"{'Emails' if channel == 'email' else 'Messages'}: " + ", ".join(bits)


def style_card(channel_stats: dict[str, dict[str, Any]], max_bytes: int = 300) -> str:
    """The style card for local drafting: whole lines, UTF-8 bytes <= max_bytes.

    Only the email and message channels are used (notes and reminders are not drafts)."""
    lines = [STYLE_HEADER]
    for channel in ("email", "message"):
        stats = channel_stats.get(channel)
        if not stats:
            continue
        line = style_line(stats)
        while len("\n".join(lines + [line]).encode("utf-8")) > max_bytes and ", " in line:
            line = line.rsplit(", ", 1)[0]            # drop the least important clause
        if len("\n".join(lines + [line]).encode("utf-8")) <= max_bytes:
            lines.append(line)
    return "\n".join(lines) if len(lines) > 1 else ""
