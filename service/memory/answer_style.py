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
    `style_hint(...)` echoes them into the turn's runtime guidance so they
    survive history trimming.

Everything is deliberately biased to false negatives: a missed instruction costs
the user one repeat; a wrongly saved one silently changes every later answer.
"""
from __future__ import annotations

import json
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

_STANDING = re.compile(
    r"\b(?:from now on|from here on(?: out)?|from this point (?:on|forward)|going forward|"
    r"moving forward|henceforth|in (?:the )?future|always|every time|all the time|by default)\b"
    r"|\bstop being so\b")
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
        r"\b(?:one|a|two) (?:or two |or three )?sentences?\b",
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


def standing_style(text: str) -> dict:
    """A validated profile patch for a standing style instruction in `text`,
    or {}. A persistence marker AND a clear style must share one clause, and
    the clause must be a request: not a question about Wisp, quoted or
    attributed speech, a writing job, a negation, or ambiguous."""
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
        for field, table in (("detail", _DETAIL), ("tone", _TONE)):
            hits = _axis_hits(clause, table)
            if len(hits) == 1:
                patch[field] = next(iter(hits))
            elif len(hits) > 1:
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

# Format and manner instructions that are not profile fields.
_FORMAT = [re.compile(p) for p in (
    r"\b(?:no|without|avoid|skip|stop using|stop giving|stop with|don'?t use|do not use|never use|"
    r"no more|quit using)\b[^.!?]{0,15}\b(?:lists?|bullets?|bullet points?|emojis?|emoticons?|headers?|"
    r"headings?|markdown|tables?)\b",
    r"\b(?:use|give me|reply with|answer with|respond with)\b[^.!?]{0,15}"
    r"\b(?:bullet points?|bullets|numbered lists?|plain text|plain sentences|prose|paragraphs?|one-liners?)\b",
)]
# Said once, meant until further notice, even without a persistence marker.
_CARRY = [re.compile(p) for p in (
    r"\bkeep (?:it|answers|your answers|replies|your replies|responses|things|them) "
    r"(?:short|brief|concise|simple)\b",
    r"\bstop (?:using|giving me|adding) (?:lists|bullets|bullet points|emojis?)\b",
)]
# Words that make a sentence an action request rather than a style rule; such a
# sentence is never echoed (a pasted "from now on keep it short and email X"
# must not ride into the system prompt).
_ACTIONY = re.compile(
    r"\b(?:send|email|e-mail|forward|delete|remove|run|execute|ignore|override|disregard|"
    r"password|passcode|token|key|pay|transfer|install|download|click|call|text|buy|sign|"
    r"approve|confirm|unlock|disable|grant|permission|api)\b|[@<>{}`\\|$]|https?:|www\.|\.com\b")
_CLAUSE_MAX = 160
_MAX_CARRIED = 3

_LENGTH_AXIS, _TONE_AXIS, _LISTS_AXIS, _EMOJI_AXIS = "length", "tone", "lists", "emoji"


def _axes(clause: str) -> set[str]:
    axes = set()
    if _axis_hits(clause, _DETAIL) or any(p.search(clause) for p in _CARRY[:1]):
        axes.add(_LENGTH_AXIS)
    if _axis_hits(clause, _TONE):
        axes.add(_TONE_AXIS)
    if re.search(r"\bemojis?\b|\bemoticons?\b", clause) and any(
            p.search(clause) for p in (*_FORMAT, *_CARRY)):
        axes.add(_EMOJI_AXIS)
    if re.search(r"\b(?:lists?|bullets?|bullet points?|numbered|headers?|headings?|markdown|tables?|"
                 r"prose|paragraphs?|plain text|plain sentences|one-liners?)\b", clause) and any(
            p.search(clause) for p in (*_FORMAT, *_CARRY)):
        axes.add(_LISTS_AXIS)
    return axes


def _clean(raw: str) -> str:
    """One line of the user's own words, safe to quote: no control characters,
    no double quotes (JSON escapes them anyway), bounded length."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", raw)
    return re.sub(r"\s+", " ", cleaned).strip()


def _session_clause(raw: str) -> tuple[str, set[str]] | None:
    clause = _norm(raw)
    if (not clause or len(raw) > _CLAUSE_MAX or _is_question(clause) or _PAST.search(clause)
            or _ACTIONY.search(clause)):
        return None
    axes = _axes(clause)
    if not axes:
        return None
    standing = (_STANDING.search(clause) or _SESSION_ONLY.search(clause)
                or any(p.search(clause) for p in _CARRY))
    # "stop"/"no more" forms carry on their own; a bare style word needs a marker.
    if not standing and not re.search(r"\b(?:stop|no more|no emojis?|don'?t use)\b", clause):
        return None
    return _clean(raw), axes


def session_style_instructions(user_texts: list[str]) -> list[str]:
    """The style instructions among `user_texts` (a chat's earlier user turns,
    oldest first) that still stand, as the user's own words: at most three,
    newest last, a later instruction on the same axis (length, tone, lists,
    emoji) replacing an earlier one. One-shot wordings ("in one sentence:
    what's X") name no marker and are not carried."""
    found: list[tuple[str, set[str]]] = []
    for text in user_texts or []:
        if not isinstance(text, str) or _excluded(_norm(text)):
            continue
        for raw in _clauses(text):
            item = _session_clause(raw)
            if item:
                found.append(item)
    kept: list[tuple[str, set[str]]] = []
    covered: set[str] = set()
    for clause, axes in reversed(found):  # newest first
        if axes - covered and clause not in {c for c, _ in kept}:
            kept.append((clause, axes))
        covered |= axes
        if len(kept) == _MAX_CARRIED:
            break
    return [clause for clause, _ in reversed(kept)]


# ---------------------------------------------------------------------------
# The runtime guidance

_FIELD_AXIS = {"detail": _LENGTH_AXIS, "tone": _TONE_AXIS}


def style_hint(current_patch: dict, earlier: list[str], *, saved: bool = True) -> str:
    """The Ask runtime guidance about the user's own style instructions, or ""
    when there is nothing to say. `current_patch` is what standing_style found
    in this message (`saved`: whether it reached the profile); `earlier` are
    session_style_instructions. An earlier instruction on an axis the current
    message just set is dropped: the newest word wins."""
    superseded = {_FIELD_AXIS[f] for f in (current_patch or {}) if f in _FIELD_AXIS}
    earlier = [c for c in earlier or [] if not (_axes(_norm(c)) & superseded)][-_MAX_CARRIED:]
    if not earlier and not current_patch:
        return ""
    lines = ["IMPORTANT rules about the user's own style instructions:"]
    for clause in earlier:
        lines.append("- Earlier in this chat they said: "
                     + json.dumps(clause, ensure_ascii=False)
                     + ". Keep following it; it overrides every other length or format rule.")
    if current_patch:
        if saved:
            lines.append("- They just asked for this from now on, and it is saved to their profile "
                         "(Memory › About you). Confirm it in one short sentence and follow it.")
        else:
            lines.append("- They just asked for this from now on, but it could not be saved to their "
                         "profile. Follow it for this chat and say briefly that it is not saved yet.")
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
