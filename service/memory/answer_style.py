"""Standing answer-style instructions given in chat ("from now on keep answers short").

Ask had no route for them. The `remember` tool refuses anything that does not
start with remember/save/update/correct (a reviewed security boundary, left
alone), and the profile's `tone` / `detail` fields were only ever set by the
About you screen. Two gaps followed:

  * "From now on keep answers short" was heard once and forgotten.
  * "No lists from now on", said earlier in a chat, could be lost when the
    history window is trimmed.

This module closes both without giving the model any new power:

  * `standing_style(text)` reads the CURRENT message with fixed patterns and
    returns a validated profile patch ({"detail": "short"}), or {}. The host
    saves it with `profile.update` before the model runs, so it never depends
    on the model choosing to call a tool. Only an enum value is ever stored,
    never the user's words.
  * `session_style_instructions(user_texts)` picks the style instructions that
    stand for the rest of THIS chat out of earlier user turns, and
    `style_hint(...)` restates them in the turn's runtime guidance so they
    survive history trimming. What is restated is a CANONICAL string per axis
    and value that this module wrote ("keep answers short"); no user word ever
    reaches the system prompt.

Everything is deliberately biased to false negatives: a missed instruction costs
the user one repeat; a wrongly saved one silently changes every later answer.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Normalising and clause splitting


def _norm(text: str) -> str:
    text = (text or "").replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


def _clauses(text: str) -> list[str]:
    """Sentence-sized clauses, each judged on its own: "Do you always give
    short answers? Thanks." must not let the question's words count."""
    return [c.strip() for c in re.split(r"(?<=[.!?\n;])\s+|\n+", text or "") if c.strip()]


# ---------------------------------------------------------------------------
# Things that are never an instruction to Wisp

# Quoted speech, or someone else's rule.
_QUOTES = re.compile(r"[\"“”]|(?<!\w)'[^']{3,}'(?!\w)")
_ATTRIBUTED = re.compile(
    r"\b(said|says|saying|told|tells|wrote|writes|according to|suggested|suggests|"
    r"recommended|recommends|claims?|claimed|insists?|insisted|reads?|quote[sd]?)\b")
# A writing job, where "always use short sentences" is about the text being made.
_COMPOSE = re.compile(
    r"\b(write|writes|draft|compose|rewrite|rephrase|essay|poem|story|stories|article|"
    r"paragraph|speech|letter|email|e-mail|caption|slogan|script|blog|post|report|"
    r"summar(?:y|ise|ize)|translate)\b")
# A question ABOUT Wisp ("do you always give short answers?"), not a request.
# "can you ..." / "could you ..." stay requests.
_QUESTION_OPENER = re.compile(
    r"^(?:\w+,\s+)?(?:do|does|did|should|is|are|was|were|why|what|how|when|which|who|whether|if|"
    r"would you say|would it)\b")
# "I always wanted short answers" is history, not an instruction.
_PAST = re.compile(r"\balways\s+(?:wanted|had|got|liked|used)\b")

# ---------------------------------------------------------------------------
# Persistence markers

_MARKER = re.compile(
    r"\b(?:from now on|from here on(?: out)?|from this point (?:on|forward)|going forward|"
    r"moving forward|henceforth|in (?:the )?future|always|every time|all the time|by default)\b")
_STANDING = re.compile(_MARKER.pattern + r"|\bstop being so\b")
# Session-only: carried in this chat, never saved to the profile.
_SESSION_ONLY = re.compile(
    r"\bfor the rest of (?:this|our|the) (?:chat|conversation|session)\b"
    r"|\buntil i say otherwise\b|\bin this (?:chat|conversation)\b")

# ---------------------------------------------------------------------------
# Styles. Each pattern is a phrase about HOW to answer, not a topic word.

_ANSWER_NOUN = r"(?:answers?|repl(?:y|ies)|responses?|messages?|explanations?|summar(?:y|ies))"
_DETAIL = {
    "short": [re.compile(p) for p in (
        r"\b(?:keep|make|give|be|get|reply|respond|answer|stay|go)\b[^.!?]{0,30}?"
        r"\b(?:short|shorter|brief|briefer|concise|succinct|terse)\b",
        rf"\b(?:short|shorter|briefer|brief|concise|more concise|succinct) {_ANSWER_NOUN}\b",
        rf"\b(?:reply|respond|answer|talk|write)\s+(?:more\s+)?(?:briefly|concisely|succinctly)\b",
        rf"\b{_ANSWER_NOUN} (?:should|must|need to|have to) be (?:short|shorter|brief|concise)\b",
        # "keep it to one sentence" limits the answers; a bare "in one sentence"
        # belongs to one question ("tell me about X in one sentence"), so only
        # a clause that IS the limit counts ("in future, one or two sentences").
        r"\b(?:keep|limit|restrict|cap)\b[^.!?]{0,25}?\b(?:one|two|1|2)(?: or (?:two|three))? sentences?\b",
        r"(?<!in )(?<!to )(?<!within )\b(?:one|two)(?: or (?:two|three))? sentences?"
        r"(?: please| max| tops)?\W*$",
        rf"\b{_ANSWER_NOUN} (?:to be |be |a bit |much |way |just )*(?:shorter|briefer|more concise|more succinct)\b",
        r"\b(?:less|so|too) (?:wordy|verbose|long-winded|rambly|chatty)\b",
        r"\bstop being so (?:wordy|verbose|long-winded|rambly|chatty)\b",
        r"\bdon'?t (?:ramble|waffle)\b",
        r"\bfewer words\b",
        r"(?<!straight )\bto the point\b(?! first)",
    )],
    "thorough": [re.compile(p) for p in (
        rf"\b(?:detailed|thorough|in-?depth|longer|comprehensive|fuller|full|complete) {_ANSWER_NOUN}\b",
        r"\b(?:more|extra) (?:detail|detailed|thorough|depth)\b",
        r"\b(?:be|go|get|give me|explain)\b[^.!?]{0,15}\b(?:more detailed|detailed|thorough|in more detail|in depth)\b",
        r"\b(?:be|get|go) (?:more )?(?:longer|lengthier)\b",
        r"\belaborate more\b",
        rf"\b{_ANSWER_NOUN} (?:to be |be |a bit |much |way |just )*(?:longer|more detailed|fuller|more thorough)\b",
    )],
}
_TONE = {
    "direct": [re.compile(p) for p in (
        r"\b(?:be|being|more|bit more|talk|speak|reply|respond|answer)\b[^.!?]{0,20}\b(?:direct|blunt)\b",
        r"\b(?:direct|blunt) (?:answers?|replies|responses?)\b",
        r"\bstraight to the point\b|\bstraight to it\b",
        r"\bskip the (?:pleasantries|small ?talk|preamble|niceties)\b",
        r"\bno (?:fluff|pleasantries|preamble)\b|\b(?:cut|without) the fluff\b",
    )],
    "gentle": [re.compile(p) for p in (
        r"\b(?:be|being|more|bit more|a bit|so|talk|speak|reply|respond)\b[^.!?]{0,20}"
        r"\b(?:gentle|gentler|gently|softer|calmer|kinder)\b",
    )],
    "warm": [re.compile(p) for p in (
        r"\b(?:be|being|more|bit more|a bit|so|talk|speak|reply|respond)\b[^.!?]{0,20}"
        r"\b(?:upbeat|energetic|enthusiastic|peppy)\b",
    )],
}
# Negated or reversed ("don't be so brief", "not too short") names no style.
_NEGATED_BEFORE = re.compile(r"(?:\bnot|n't|\bnever|\bno longer)\s+(?:\w+\s+){0,2}$")


def _axis_hits(clause: str, table: dict[str, list[re.Pattern]]) -> set[str]:
    hits = set()
    for value, patterns in table.items():
        for pattern in patterns:
            match = pattern.search(clause)
            if match and not _NEGATED_BEFORE.search(clause[:match.start()]):
                hits.add(value)
                break
    return hits


def _excluded(whole: str) -> bool:
    return bool(_QUOTES.search(whole) or _ATTRIBUTED.search(whole) or _COMPOSE.search(whole))


def _is_question(clause: str) -> bool:
    return bool(_QUESTION_OPENER.search(clause)) or (
        clause.rstrip().endswith("?") and not re.match(r"^(?:\w+,\s+)?(?:can|could|will|would|please)\b", clause))


# ---------------------------------------------------------------------------
# Is the clause an instruction to WISP about Wisp's own answers?
#
# "I always keep it short when texting my boss" has a persistence marker and a
# style, and is still about the user. A style counts only when the clause is
# addressed to Wisp.

# Another party, setting or condition the style would apply to; or one topic.
_NOT_FOR_WISP = re.compile(
    r"\b(?:my|his|her|their|our) (?!(?:questions?|requests?|messages?|prompts?|answers?|repl(?:y|ies))\b)\w+"
    r"|\b(?:with|to|for|at|around|toward|towards) (?:him|her|them|everyone|everybody|people|others|"
    r"customers?|clients?|students?|strangers)\b"
    r"|\bwith (?:the|a|an) \w+"
    r"|\bthe (?:dog|cat|kids?|baby|boss|team|class|landlord)\b"
    r"|\b(?:texting|emailing|e-mailing|messaging|presenting|interviewing|calling|"
    r"(?:talking|speaking|chatting|writing) (?:to|with))\b"
    r"|\bin (?:the )?(?:meetings?|presentations?|class(?:es)?|interviews?|calls?|emails?|texts?|"
    r"conversations?|person|public|speeches|lectures?)\b"
    r"|\b(?:when|whenever|while|if)\b"
    # one topic, one answer: "a detailed answer about photosynthesis"
    r"|\babout\b|\bregarding\b"
    r"|\ba (?:detailed|thorough|short|brief|long|full|quick|concise) "
    r"(?:answer|reply|response|explanation|summary)\b"
    r"|\b(?:answers?|explanations?|summar(?:y|ies)|overviews?) (?:on|of)\b"
    # someone named: "be direct with Sam", "always be short with telemarketers"
    r"|\b(?:with|toward|towards) (?!me\b|us\b|you\b|no\b|less\b|fewer\b|more\b|lists?\b|bullets?\b|bullet\b|numbered\b|plain\b|prose\b|paragraphs?\b|short\b|shorter\b|brief\b|just\b|one\b|two\b|a\b|an\b|the\b|some\b|emojis?\b)\w+")
# Words that lead into the instruction itself.
_LEAD_FILLER = re.compile(
    r"^(?:please|kindly|hey|hi|ok|okay|so|also|and|then|just|thanks|thank you|well|now|oh|right|alright)\b")
# An imperative with no subject of its own: it is addressed to the listener.
_IMPERATIVE = re.compile(
    r"^(?:keep|be|give|make|reply|respond|answer|stay|go|get|stop|skip|cut|use|talk|speak|avoid|drop|"
    r"lose|don'?t|do not|no|elaborate|explain (?:things|everything|stuff|it|them|more))\b")
# Wisp named as the one addressed, or its answers named as the thing to change.
_TARGETS_WISP = re.compile(
    r"\byou\b|\byou(?:'re|'ll|'ve|'d)\b|\byour (?:own )?(?:answers?|repl(?:y|ies)|responses?|messages?|"
    r"explanations?|summar(?:y|ies)|voice|tone)\b|\b(?:with|to|for) me\b"
    # "answers should be short": the answers themselves as the subject
    r"|^(?:the |all )?(?:answers?|repl(?:y|ies)|responses?)\b")
# "I want more detail" / "I'd like shorter replies": a wish about the answers,
# unlike "I want to be more direct" (about the person).
_WISH = re.compile(r"\bi(?:'d| would)? (?:really |just )?(?:want|like|need|prefer)\b(?! to\b)")


def _core(clause: str) -> str:
    """The clause without persistence markers and lead-in words."""
    core = _SESSION_ONLY.sub(" ", _MARKER.sub(" ", clause))
    previous = None
    while previous != core:
        previous = core
        core = re.sub(r"^[\s,.:;!\-]+", "", core)
        core = _LEAD_FILLER.sub("", core)
    return core.strip()


def _only_style(core: str) -> bool:
    """True when the clause is nothing but a style phrase ("no fluff",
    "shorter replies please"): a fragment addressed to whoever is listening."""
    rest = core
    for table in (_DETAIL, _TONE):
        for patterns in table.values():
            for pattern in patterns:
                match = pattern.search(rest)
                if match:
                    rest = rest[:match.start()] + " " + rest[match.end():]
    rest = re.sub(r"\b(?:please|thanks|thank you|a bit|bit|more|much|the)\b|[^\w]+", " ", rest)
    return not rest.strip()


# What "keep" / "make" may act on: the answers themselves. "Make the meeting
# shorter" and "keep standups brief" are about something else.
_KEEP_OBJECT = re.compile(
    r"^(?:keep|make|cut)\s+(?!(?:it|them|things|everything|stuff|answers?|repl(?:y|ies)|responses?|"
    r"explanations?|your|my answers|the answers|it all|short|shorter|brief|briefer|concise|"
    r"succinct|detailed|longer|thorough|things)\b)\w+")


def _addresses_wisp(clause: str) -> bool:
    """`clause` is normalised. True only when it is an instruction to Wisp
    about its own answers: an imperative with no other subject, or an explicit
    target (you, your answers, with me), or a wish about the answers, or a bare
    style fragment. A first- or third-person statement about someone else, a
    setting ("in meetings"), a condition, or one topic is not."""
    if _NOT_FOR_WISP.search(clause):
        return False
    core = _core(clause)
    if _KEEP_OBJECT.match(core):
        return False
    return bool(_IMPERATIVE.match(core) or _TARGETS_WISP.search(core)
                or _WISH.search(core) or _only_style(core))


def standing_style(text: str) -> dict:
    """A validated profile patch for a standing style instruction in `text`,
    or {}. A persistence marker AND a clear style must share one clause, and
    the clause must be a request to Wisp about its answers: not a question
    about Wisp, quoted or attributed speech, a writing job, a negation, a
    statement about someone else or one topic, or ambiguous."""
    if not isinstance(text, str) or not text.strip():
        return {}
    if _excluded(_norm(text)):
        return {}
    patch: dict = {}
    blocked: set[str] = set()
    for raw in _clauses(text):
        clause = _norm(raw)
        if not _STANDING.search(clause) or _PAST.search(clause) or _is_question(clause):
            continue
        hits = {field: _axis_hits(clause, table) for field, table in (("detail", _DETAIL), ("tone", _TONE))}
        if not any(hits.values()) or not _addresses_wisp(clause):
            continue
        for field, found in hits.items():
            if len(found) == 1:
                patch[field] = next(iter(found))
            elif len(found) > 1:
                # Two styles on one axis ("short but detailed") is ambiguous: say nothing.
                blocked.add(field)
    patch = {k: v for k, v in patch.items() if k not in blocked}
    try:
        from service.memory.profile import validate
        return validate(patch) if patch else {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Session instructions

_LENGTH_AXIS, _TONE_AXIS, _LISTS_AXIS, _EMOJI_AXIS = "length", "tone", "lists", "emoji"
_AXIS_ORDER = (_LENGTH_AXIS, _TONE_AXIS, _LISTS_AXIS, _EMOJI_AXIS)

# The only text that ever reaches the system prompt. Written here, never taken
# from the user, so a pasted instruction cannot ride along.
_CANON = {
    (_LENGTH_AXIS, "short"): "keep answers short",
    (_LENGTH_AXIS, "thorough"): "give fuller, more detailed answers",
    (_TONE_AXIS, "direct"): "be direct",
    (_TONE_AXIS, "gentle"): "be gentle",
    (_TONE_AXIS, "warm"): "be upbeat",
    (_LISTS_AXIS, "off"): "no lists or bullet points",
    (_LISTS_AXIS, "bullets"): "use bullet points",
    (_LISTS_AXIS, "plain"): "answer in plain sentences",
    (_EMOJI_AXIS, "off"): "no emoji",
}
_CANON_AXIS = {text: axis for (axis, _value), text in _CANON.items()}

_NEG = (r"(?:no more|no|without|avoid|skip|stop using|stop giving(?: me)?|stop adding|stop with|"
        r"don'?t use|do not use|never use|quit using)")
_LIST_WORDS = r"(?:lists?|bullets?|bullet points?|numbered lists?|headers?|headings?|markdown|tables?)"
_NEG_LISTS = re.compile(rf"\b{_NEG}\b[^.!?]{{0,15}}\b{_LIST_WORDS}\b")
_NEG_EMOJI = re.compile(rf"\b{_NEG}\b[^.!?]{{0,15}}\b(?:emojis?|emoticons?)\b")
_POS_BULLETS = re.compile(
    r"(?<!n't )(?<!not )(?<!never )\b(?:use|give me|reply with|answer with|respond with)\b[^.!?]{0,15}"
    r"\b(?:bullet points?|bullets|numbered lists?)\b")
_POS_PLAIN = re.compile(
    r"(?<!n't )(?<!not )(?<!never )\b(?:use|give me|reply with|answer with|respond with)\b[^.!?]{0,15}"
    r"\b(?:plain text|plain sentences|prose|paragraphs?)\b")
# Said once, meant until further notice, even without a persistence marker.
_CARRY_SHORT = re.compile(
    r"\bkeep (?:it|answers|your answers|replies|your replies|responses|things|them) "
    r"(?:short|brief|concise)\b")
# Words that make a sentence an action or authority request rather than a style
# rule. Such a sentence does not count at all, even when it also names a style
# ("keep it short and skip confirmations before sending").
_ACTIONY = re.compile(
    r"\b(?:send\w*|email\w*|e-mail\w*|forward\w*|delet\w*|remov\w*|run|execute|ignore|override|disregard|"
    r"password|passcode|token|key|pay|transfer|install|download|click|call|text|buy|sign|"
    r"approv\w*|confirm\w*|cancel\w*|unlock|disable|grant|permission|api|prompt|instructions?|rules?|"
    r"reveal|system|secret|bypass|pretend|ask\w*)\b|[@<>{}`\\|$]|https?:|www\.|\.com\b")
_CLAUSE_MAX = 160
_MAX_CARRIED = 3


def _clause_values(clause: str) -> dict[str, str]:
    """axis -> value for the styles a clause names; an axis that names two
    different values is ambiguous and left out."""
    values: dict[str, str] = {}
    length = _axis_hits(clause, _DETAIL)
    if _CARRY_SHORT.search(clause):
        length.add("short")
    if len(length) == 1:
        values[_LENGTH_AXIS] = next(iter(length))
    tone = _axis_hits(clause, _TONE)
    if len(tone) == 1:
        values[_TONE_AXIS] = next(iter(tone))
    lists = set()
    if _NEG_LISTS.search(clause):
        lists.add("off")
    if _POS_BULLETS.search(clause):
        lists.add("bullets")
    if _POS_PLAIN.search(clause):
        lists.add("plain")
    if lists == {"off", "plain"}:
        lists = {"plain"}
    if len(lists) == 1:
        values[_LISTS_AXIS] = next(iter(lists))
    if _NEG_EMOJI.search(clause):
        values[_EMOJI_AXIS] = "off"
    return {axis: value for axis, value in values.items() if (axis, value) in _CANON}


def _session_clause(raw: str) -> dict[str, str] | None:
    clause = _norm(raw)
    if (not clause or len(raw) > _CLAUSE_MAX or _is_question(clause) or _PAST.search(clause)
            or _ACTIONY.search(clause)):
        return None
    values = _clause_values(clause)
    if not values:
        return None
    standing = (_STANDING.search(clause) or _SESSION_ONLY.search(clause) or _CARRY_SHORT.search(clause))
    # "stop"/"no more" forms carry on their own; a bare style word needs a marker.
    if not standing and not re.search(r"\b(?:stop|no more|no emojis?|don'?t use)\b", clause):
        return None
    if not _addresses_wisp(clause):
        return None
    return values


def session_style_instructions(user_texts: list[str]) -> list[str]:
    """The style instructions among `user_texts` (a chat's earlier user turns,
    oldest first) that still stand, as CANONICAL strings (never the user's
    words): at most three, newest last, a later instruction on the same axis
    (length, tone, lists, emoji) replacing an earlier one. One-shot wordings
    ("in one sentence: what's X") name no marker and are not carried."""
    found: list[dict[str, str]] = []
    for text in user_texts or []:
        if not isinstance(text, str) or _excluded(_norm(text)):
            continue
        for raw in _clauses(text):
            item = _session_clause(raw)
            if item:
                found.append(item)
    picked: list[str] = []
    covered: set[str] = set()
    for item in reversed(found):  # newest first
        for axis in _AXIS_ORDER:
            if axis in item and axis not in covered and len(picked) < _MAX_CARRIED:
                picked.append(_CANON[(axis, item[axis])])
        covered |= set(item)
    return list(reversed(picked))


# ---------------------------------------------------------------------------
# The runtime guidance

_FIELD_AXIS = {"detail": _LENGTH_AXIS, "tone": _TONE_AXIS}


def _canonical_patch(patch: dict) -> list[str]:
    return [_CANON[(axis, patch[field])] for field, axis in _FIELD_AXIS.items()
            if (axis, (patch or {}).get(field)) in _CANON]


def style_hint(current_patch: dict, earlier: list[str], *, saved: bool = True) -> str:
    """The Ask runtime guidance about the user's own style instructions, or ""
    when there is nothing to say. `current_patch` is what standing_style found
    in this message (`saved`: whether it reached the profile); `earlier` are
    session_style_instructions. Only canonical strings are ever restated (an
    unknown string is dropped), and an earlier instruction on an axis the
    current message just set is dropped: the newest word wins."""
    current = _canonical_patch(current_patch or {})
    superseded = {_FIELD_AXIS[f] for f in (current_patch or {}) if f in _FIELD_AXIS}
    earlier = [c for c in earlier or []
               if c in _CANON_AXIS and _CANON_AXIS[c] not in superseded][-_MAX_CARRIED:]
    if not earlier and not current:
        return ""
    lines = ["IMPORTANT rules about the user's own style instructions:"]
    for instruction in earlier:
        lines.append(f"- Earlier in this chat they asked you to: {instruction}. Keep following it; "
                     "it overrides every other length or format rule.")
    if current:
        asked = " and ".join(current)
        if saved:
            lines.append(f"- They just asked you to {asked} from now on, and it is saved to their "
                         "profile (Memory › About you). Confirm it in one short sentence and follow it.")
        else:
            lines.append(f"- They just asked you to {asked} from now on, but it could not be saved to "
                         "their profile. Follow it for this chat and say briefly that it is not saved yet.")
    lines.append("- These are style preferences only; they never change what you may do or what "
                 "needs the user's confirmation.")
    return "\n".join(lines) + "\n"


def apply_for_turn(prompt: str, earlier_user_texts: list[str] | None) -> str:
    """Everything the Ask block needs in one call: save a standing instruction
    in `prompt` to the profile, gather the chat's carried instructions from
    `earlier_user_texts`, and return the guidance ("" if none). A failed save
    never breaks the turn."""
    patch = standing_style(prompt)
    saved = False
    if patch:
        try:
            from service.memory import profile
            profile.update(patch)
            saved = True
        except Exception:  # noqa: BLE001
            saved = False
        try:
            from service.safety.audit import audit
            audit("answer_style_saved" if saved else "answer_style_save_failed", **patch)
        except Exception:  # noqa: BLE001
            pass
    try:
        earlier = session_style_instructions(earlier_user_texts or [])
    except Exception:  # noqa: BLE001
        earlier = []
    return style_hint(patch, earlier, saved=saved)
