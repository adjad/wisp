"""Conservative typo/shorthand normalization — a ROUTE-DETECTION signal only.

Used after every rule has declined the original text (see
router._route_request): the same tested rules get one more look at a
normalized copy of "whats on my calender tmrw" before the request falls to the
generic retrieved menu.

The normalized copy is never content. Only the route (domain, tool menu,
forced tool name) may come from it; the resolved request, tool-argument
bindings, recipients, message bodies and anything shown to the user always use
the user's ORIGINAL text. See router._typo_route_hint.

What is never rewritten (independent review of PR #159):
  * a token touching @ . / \\ ~ _ - # : or a digit — addresses, hosts, file
    names ("budget.txt", "bob@msg.com", "appt@clinic.org"), paths, handles;
  * anything inside quotes, backticks or a URL;
  * meta-linguistic questions about the word itself ("what does tmr stand
    for", "what does appt mean", "the word emial", "how do you spell txt") —
    the whole text is returned unchanged.

Entries are unambiguous misspellings or chat shorthand only; words that are
also names or real words ("cal", "emil", "whether", "u") are absent.
"""
from __future__ import annotations

import re

_TYPOS: tuple[tuple[str, str], ...] = (
    (r"tmrw|tmr|tmrow|tmw|tommorow|tomorow|tommorrow|tomorrw|2moro|2morrow", "tomorrow"),
    (r"tdy|2day", "today"),
    (r"calender|calandar|calander|calendr", "calendar"),
    (r"schedual|scedule|shedule|schedul", "schedule"),
    (r"emial|emali|e-mial|emal", "email"),
    (r"emials|emals", "emails"),
    (r"txt", "text"),
    (r"txts", "texts"),
    (r"remeber|rember|remmeber|remembr", "remember"),
    (r"rmind|remnd|remid|remine|remindr", "remind"),
    (r"remiders|remindrs|reminers", "reminders"),
    (r"serch|seach|srch", "search"),
    (r"chek|chk", "check"),
    (r"drft", "draft"),
    (r"mesage|messege|msg", "message"),
    (r"msgs|mesages", "messages"),
    (r"appt", "appointment"),
    (r"appts", "appointments"),
    (r"pls|plz", "please"),
)
_LOOKUP = {}
for _pattern, _word in _TYPOS:
    for _alt in _pattern.split("|"):
        _LOOKUP[_alt.lower()] = _word

# A word token and the characters that make a neighbor part of an
# address/path/file name/handle rather than prose.
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z]+)?")
_GLUE = set("@./\\~_-#:") | set("0123456789")
_PROTECTED_SPAN_RE = re.compile(
    r"\"[^\"]*\"|“[^”]*”|‘[^’]*’|`[^`]*`|(?<!\w)'[^']*'(?!\w)|"
    r"\b[a-z][a-z0-9+.-]*://\S+|\bwww\.\S+|\S+@\S+|(?:~|\.{1,2})?/\S+", re.I)
_META_RE = re.compile(
    r"\b(?:what\s+(?:does|do|did|is|'s)|whats|what's)\s+[\"'“‘`]?\S+[\"'”’`]?\s+"
    r"(?:mean|means|stand\s+for|short\s+for|an?\s+abbreviation)|"
    r"\b(?:the\s+)?(?:word|words|term|abbreviation|acronym|spelling|typo|misspell\w*)\b|"
    r"\bspell(?:ed|ing|s)?\b|\bmeaning\s+of\b|\bdefin(?:e|ition)\b|\bshort\s+for\b|"
    r"\bstands?\s+for\b|\babbreviat\w*\b", re.I)


def normalize_typos(text: str) -> str:
    """Return a normalized copy of `text` for ROUTE DETECTION only (else unchanged)."""
    if not text or _META_RE.search(text):
        return text
    protected = [m.span() for m in _PROTECTED_SPAN_RE.finditer(text)]

    def inside(start: int, end: int) -> bool:
        return any(a <= start and end <= b for a, b in protected)

    out, last = [], 0
    for match in _WORD_RE.finditer(text):
        start, end = match.span()
        word = _LOOKUP.get(match.group(0).lower())
        if word is None or inside(start, end):
            continue
        before = text[start - 1] if start else " "
        after = text[end] if end < len(text) else " "
        if before in _GLUE or after in _GLUE and not (after in ".:" and (end + 1 >= len(text) or text[end + 1].isspace())):
            continue
        out.append(text[last:start])
        out.append(word)
        last = end
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)
