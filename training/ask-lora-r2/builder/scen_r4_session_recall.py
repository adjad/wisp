"""Round-4 family ``session_recall_r4``: a follow-up that depends on earlier turns of the SAME chat.

Why. The r3 Mac gate lost points on follow-ups it could have answered or should have re-read:
repeating the old answer on "really?", opening the wrong item for "the second one", saying "I haven't read an
amount yet" instead of re-reading, "Nothing has been said in our chats before" for "what was the first thing I
asked", and narrowing a recall query before denying a stored fact. followup_r3 teaches the turn right after
the one that matters; this family puts the earlier turn 2-7 exchanges back, behind ordinary chatter.

What production keeps of an earlier turn (service/main.py, service/memory): only the user text and the final
assistant text. Tool results and thinking are never stored; a one-line "Current subject" survives two assistant
turns. So a fact an earlier reply STATED can be answered from the conversation with no call, and a detail it did
NOT state needs the tool again. Seeded histories are 2-10 exchanges and stay under ~9,000 characters, so the host
keeps all of them in the request (checked at build time: every history message must reach the request); the
long-chat sub-skill 6 below goes past that on purpose and exercises the condensed note.

Sub-skills (counts in the build output):
1. answer from the conversation (no call): a time, title, name, price or total the earlier reply stated, asked
   again by name, by order ("which came first"), or by ordinal ("the 2nd item you listed"). Plus a few writes
   that need the earlier answer (a reminder before "the second one").
2. re-read when the detail was NOT stated: the earlier reply listed senders and subjects, titles, names or
   topics; the question asks for the body, the place, the due time, the exact text, the low or the rain chance.
   The ordinal resolves against the earlier reply's order ("the second one" is the 2nd item as listed).
3. challenges ("are you sure?", "double check that", "hm that doesn't sound right"): re-read with the same
   tool, answer from the fresh result (it confirms, or the world changed and the final says what it is now);
   a bare "what?" / "huh?" after Wisp's own clarifying question restates that question, no call.
4. session meta: "what was the first thing I asked", "what did we cover", "what did I ask right before this",
   "go back to what you said about Friday", "did we talk about the weather": answered from the history.
5. stored facts: a broad ``recall`` (one stem word, never narrowed) and the stored fact stated; a fact the user
   said earlier in THIS chat is answered from the chat; an absent fact is said plainly.
6. long chats (round 4 review): 8-10 exchanges of base-length replies (60-150 words, 2,500-9,000 characters), and condensed
   chats of 21-24 exchanges, which production cuts to its newest 40 messages plus the "Earlier in this conversation" note
   (service/memory/context.py). Asked at the end: a fact the user stated in an early message, a figure the assistant gave
   in its first long reply, "what was the first thing I asked", a personal fact never said (broad recall, then "not in this
   chat, not saved") and something the assistant never suggested. In the condensed chats the asked fact must survive into
   the note's own text and not be verbatim anywhere (checked on the captured request); the final states only the core of
   the fact there (the "core||tail" finals drop their tail)

Host finding that shapes the no-call prompts. When the model answers a prompt with no tool at all, the Ask loop
re-prompts once with a tool required if ``service.model_only.asks_personal_data(prompt)`` is true (a question with
"my <word>", "did/do/am/was I", "due", "arrive", "unread mail", "when is the X <day>", ...), and the builder then
fails with "loop asked for step 2 past the script". So every no-call turn here is phrased without those shapes
("what time was the dentist again", "which came first, the X or the Y", "what was the question right before
this one", "the second one you listed, when is it") and ``_check`` drops any that the guard would catch. In
production a prompt like "where did I say the dinner is?" therefore gets a forced tool call first; those are not
trained as no-call turns. (Dev switches: SESSION_RECALL_SOFT=1 turns authoring assertions into drops and dumps
the drop reasons, SESSION_RECALL_STATS=1 prints the distribution.)

``_check`` runs on every scenario: the rules above are asserted mechanically (no call where none is wanted, the
fact string in both the earlier reply and the final, the read call's result holds the target and not the other
items, the referent is the right ordinal), the voice rules are asserted on every final and every earlier
assistant turn, and the host must keep every history message. Wordings avoid toolbench and chat-eval prompts.
"""
from __future__ import annotations

import copy
import datetime as dt
import os
import random
import re
from decimal import Decimal, ROUND_HALF_UP

import harness as H
import leak
import scen_r2_act as A
from gen import make_weather
from harness import Call, Final
from replies import clock, day_word, join
from scen import Scenario
from scen_r2_act import iso, style
from scen_r3_chat import BANNED_OPENERS, CLOSERS, EMOJI
from scen_r3_followup import at_, blank, turn, wd_name
import scen_r4_instructions as I
from scen_r4_session_chat import NONE_ACK
from service.memory.context import DIGEST_HEADER, digest_line
from service.model_only import asks_personal_data
from world import Email, Event, Msg, Note, Reminder

D = dt.timedelta
FAMILY = "session_recall_r4"
WRITE_ALLOW_R4 = {FAMILY: frozenset({"add_reminder"})}

DROPPED: list[tuple[str, str, str]] = []
_SEEN: set[str] = set()
_FIRST: dict[str, int] = {}


# =============================================================================
# voice and mechanical rules
# =============================================================================
TOOLWORDS = re.compile(r"\b(get_upcoming|view_emails|summarize_emails|search_reminders|view_messages|search_notes|"
                       r"get_weather|add_reminder|recall|tool|tools|json|untrusted|receipt|schema|function|mcp)\b", re.I)
STALL = re.compile(r"can[’']?t look up|cannot look up|don[’']?t have access|do not have access|nothing has been said|"
                   r"haven[’']?t (read|checked)|have not (read|checked)|not able to|unable to|as an ai|"
                   r"couldn[’']?t find any web|i can[’']?t (see|check|read)|haven[’']?t checked a current", re.I)
MEMNARRATE = re.compile(r"\bi[’']?(ve| have) (saved|stored|noted|remembered)|saved that|in my memory|i remember that", re.I)
THIRD = re.compile(r"\bSam\b")


def _voice(text, where):
    low = text.lower()
    assert not TOOLWORDS.search(text), f"{where}: tool word in {text!r}"
    assert not STALL.search(text), f"{where}: stall phrase in {text!r}"
    assert not CLOSERS.search(text), f"{where}: ritual closer in {text!r}"
    assert not EMOJI.search(text), f"{where}: emoji in {text!r}"
    assert not THIRD.search(text), f"{where}: third person in {text!r}"
    assert not MEMNARRATE.search(text), f"{where}: narrates memory in {text!r}"
    assert not any(low.startswith(b) for b in BANNED_OPENERS), f"{where}: banned opener {text!r}"
    assert "**" not in text and "#" not in text, f"{where}: markup {text!r}"


def _norm(s):
    return s.lower().replace("’", "'").replace("“", '"').replace("”", '"')


def _has(text, needle):
    return _norm(needle) in _norm(text)


def _fw(text):
    words = re.findall(r"[a-z']+", text.lower())
    return words[0] if words else ""


def fin(rng, *opts):
    """One of the candidate finals, preferring a first word this family has used least."""
    best = min(_FIRST.get(_fw(o), 0) for o in opts)
    pool = [o for o in opts if _FIRST.get(_fw(o), 0) <= best + 1]
    o = rng.choice(pool)
    return o


def _used(final):
    _FIRST[_fw(final)] = _FIRST.get(_fw(final), 0) + 1


def cap1(s):
    return s[:1].upper() + s[1:]


def lowfirst(s):
    return s if len(s) > 1 and (s[1].isupper() or s[1].isdigit()) else s[:1].lower() + s[1:]


def noun(title):
    return lowfirst(title)


# =============================================================================
# the checker
# =============================================================================
def _hist_chars(sc):
    return sum(len(t["user"]) + len(t["assistant"]) for t in sc.history)


def _check(sc):
    """(ok, why). Rule violations raise (a broken scenario fails the build); host and leak problems return False."""
    final = sc.steps[-1].text if isinstance(sc.steps[-1], Final) else ""
    # ---- authoring rules (assertions)
    mode = getattr(sc, "long_mode", None)
    if mode == "digest":
        # production keeps 40 messages verbatim and condenses the rest: 21+ exchanges always condense the first
        assert 21 <= len(sc.history) <= 24, f"digest chat of {len(sc.history)} exchanges"
        assert _hist_chars(sc) <= 30000, f"history of {_hist_chars(sc)} chars"
    elif mode == "long":
        assert 7 <= len(sc.history) <= 10, f"long chat of {len(sc.history)} exchanges"
        assert 2500 <= _hist_chars(sc) <= 9000, f"long chat of {_hist_chars(sc)} chars"
    else:
        assert 2 <= len(sc.history) <= 10, f"history of {len(sc.history)} exchanges"
        if _hist_chars(sc) > 9000:      # longer replies in the filler banks can push a session past the cap: drop it
            return False, f"history of {_hist_chars(sc)} chars"
    for i, t in enumerate(sc.history):
        _voice(t["assistant"], f"history[{i}]")
        # a chain follow-up or ack must sit right after its question (or the exchange before it in the same chain)
        if t.get("crole") in ("fu", "ack"):
            prev = sc.history[i - 1] if i else {}
            want = ("q", "fu")
            assert prev.get("cblk") == t["cblk"] and prev.get("crole") in want, f"history[{i}] {t['user']!r} is separated from its question"
        elif t.get("crole") == "q":
            nxt = sc.history[i + 1] if i + 1 < len(sc.history) else {}
            if nxt.get("cblk") == t["cblk"]:
                assert nxt["crole"] in ("fu", "ack"), f"history[{i}] chain out of order"
    if sc.kind == "s4_first":
        assert not sc.history[0].get("crole") in ("fu", "ack"), f"s4_first: the first message {sc.history[0]['user']!r} is a follow-up"
    if sc.kind == "s4_before":
        assert sc.history[-1].get("crole") != "ack", f"s4_before: the last message {sc.history[-1]['user']!r} is an ack"
    if final:
        _voice(final, "final")
    if sc.kind in NEVER_QKIND:
        assert NONE_ACK.search(final), f"{sc.kind}: a never-said final must match distill/choose.py NONE_ACK: {final!r}"
    kind, rules = sc.kind, sc.rules
    if rules.get("no_call"):
        assert len(sc.steps) == 1 and isinstance(sc.steps[0], Final), f"{kind}: a call where none is wanted"
        assert len(final) >= 20, f"{kind}: final too short {final!r}"
    else:
        assert isinstance(sc.steps[0], list), f"{kind}: no first call"
        call = sc.steps[0][0]
        want = rules.get("first")
        assert want and call.name == want, f"{kind}: first call {call.name} != {want}"
        for k, v in (rules.get("args") or {}).items():
            assert call.args.get(k) == v, f"{kind}: arg {k}={call.args.get(k)!r} != {v!r}"
        if call.name == "recall":
            q = str(call.args.get("query") or "").lower()
            assert q and q in sc.prompt.lower(), f"{kind}: recall stem {q!r} is not in the prompt {sc.prompt!r}"
        if call.name != "add_reminder":
            result = str(sc.world.handle(call.name, dict(call.args)))
            for need in rules.get("in_result", ()):
                assert _has(result, need), f"{kind}: {need!r} not in the {call.name} result:\n{result[:500]}"
            for bad in rules.get("not_in_result", ()):
                assert not _has(result, bad), f"{kind}: {bad!r} should not be in the {call.name} result:\n{result[:500]}"
            if rules.get("min_final", 55):
                assert len(final) >= rules.get("min_final", 55), f"{kind}: read final too short {final!r}"
            assert len(final) <= 420, f"{kind}: final too long {final!r}"
    for need in rules.get("must", ()):
        assert _has(final, need), f"{kind}: {need!r} not in final {final!r}"
    for bad in rules.get("must_not", ()):
        assert not _has(final, bad), f"{kind}: {bad!r} should not be in final {final!r}"
    for need in rules.get("reply_must", ()):
        reply = sc.history[rules["anchor"]]["assistant"] if "anchor" in rules else ""
        assert _has(reply, need), f"{kind}: {need!r} not in the earlier reply {reply!r}"
    if "anchor" in rules:
        anchor = sc.history[rules["anchor"]]
        for need in rules.get("anchor_user", ()):
            if mode != "digest" or kind != "l_first":
                assert _has(anchor["user"], need), f"{kind}: {need!r} not in the anchor message {anchor['user']!r}"
        for need in rules.get("anchor_asst", ()):
            assert _has(anchor["assistant"], need), f"{kind}: {need!r} not in the anchor reply"
    if rules.get("ends_question"):
        assert final.rstrip().endswith("?"), f"{kind}: not a question {final!r}"
    # a position claim is true only for an anchor at that position (r3 verifier: 'your first message' on exchange 9)
    apos = rules.get("anchor") if isinstance(rules.get("anchor"), int) else None
    if kind in ("l_user", "l_asst"):
        if re.search(r"\bfirst (message|answer)\b", final, re.I):
            assert apos == 0, f"{kind}: 'first message/answer' claimed for an anchor at exchange {apos}: {final!r}"
        if re.search(r"\bnear the start\b", final, re.I):
            assert apos in (0, 1), f"{kind}: 'near the start' claimed for an anchor at exchange {apos}: {final!r}"
    # ---- host and leak checks (drops)
    if rules.get("no_call") and asks_personal_data(sc.prompt):
        return False, "personal-data guard: the host re-prompts for a tool"
    ok, why = A._check(sc)
    if not ok:
        return ok, why
    for t in sc.history:
        why = _leaky_why(t["user"], t["assistant"])
        if why:
            return False, why
    # every history message must reach the request (the host trims long sessions)
    policy, _ev, _fin = H.run_scenario(sc.prompt, copy.deepcopy(sc.steps), sc.world, sc.when,
                                       profile=sc.profile, approve=getattr(sc, "approve", None))
    msgs = policy.requests[0]["messages"]
    sent = "\n".join(m["content"] if isinstance(m["content"], str) else str(m["content"])
                     for m in msgs[:-1] if m["role"] in ("user", "assistant"))
    notes = [m["content"] for m in msgs if m["role"] == "system" and isinstance(m.get("content"), str) and m["content"].startswith(DIGEST_HEADER)]
    if mode == "digest":
        # the oldest exchanges must be condensed, and what the answer states must survive into the note
        if not notes:
            return False, "no 'Earlier in this conversation' note in the request"
        needles = list(rules.get("anchor_user", ())) + list(rules.get("anchor_asst", ()))
        for nd in needles:
            if not _has(notes[0], nd):
                if os.environ.get("SESSION_RECALL_DEBUG"):
                    tot = sum(len(str(m.get("content"))) for m in msgs) // 4 + len(str(policy.requests[0].get("tools"))) // 4
                    print("DEBUG digest lines", len(notes[0].split("\n")) - 1, "of", len(sc.history), "est tokens", tot, "needle", nd, "anchor", rules.get("anchor"))
                return False, f"{nd!r} did not survive into the condensed note"
            if _has(sent, nd):
                return False, f"{nd!r} is still verbatim in the request"
        if not all(t["user"] in sent and t["assistant"] in sent for t in sc.history[-12:]):
            return False, "the host trimmed recent history"
        return True, ""
    if notes:
        return False, "the host condensed a chat meant to stay verbatim"
    for t in sc.history:
        if t["user"] not in sent or t["assistant"] not in sent:
            return False, "the host trimmed the history"
    return True, ""


_LEAKY_WHY: dict = {}


def _leaky_why(user, assistant):
    key = (user, assistant)
    if key not in _LEAKY_WHY:
        why = ""
        hits = leak.fact_hits(user + "\n" + assistant)
        if hits:
            why = f"toolbench fact in history {hits[:2]}"
        else:
            leaked, score, q = A._leaks().leaks(user)
            if leaked:
                why = f"history prompt leaks {q['id']} {score:.2f}"
        _LEAKY_WHY[key] = why
    return _LEAKY_WHY[key]


def _attempt(make, rng, tries=8):
    for _ in range(tries):
        seed = rng.random()
        snap = (dict(_FUSED), dict(_FIRST))      # the filler bank counters: a dropped try must not consume them
        sc = make(random.Random(seed))
        if sc is None or sc.prompt.strip().lower() in _SEEN:
            _FUSED.clear(); _FUSED.update(snap[0]); _FIRST.clear(); _FIRST.update(snap[1])
            continue
        try:
            ok, why = _check(sc)
        except AssertionError as exc:
            if not os.environ.get("SESSION_RECALL_SOFT"):
                raise
            ok, why = False, f"ASSERT {exc}"
        if ok:
            _SEEN.add(sc.prompt.strip().lower())
            _FUSED.clear(); _FUSED.update(snap[0]); _FIRST.clear(); _FIRST.update(snap[1])
            kept = make(random.Random(seed))    # a fresh copy (the check ran the host on `sc`); same seed, same counters
            if isinstance(kept.steps[-1], Final):
                _used(kept.steps[-1].text)
            return kept
        _FUSED.clear(); _FUSED.update(snap[0]); _FIRST.clear(); _FIRST.update(snap[1])
        DROPPED.append((FAMILY, sc.prompt, why))
    return None


OPENERS = ["ok so ", "hey, ", "quick one, ", "um, ", "so ", "hm, ", "okay, "]
WH = ("what", "when", "where", "who", "which", "how")
NO_OPENER = ("sorry", "wait", "ok", "okay", "hm", "hmm", "and", "so", "hey", "um", "quick", "can", "could", "going", "back", "about", "on")


def _typo(rng, text, protect=""):
    words = text.split(" ")
    cands = [i for i, w in enumerate(words) if len(w) >= 6 and w.isalpha() and not (protect and protect in w.lower())]
    if not cands:
        return text
    i = rng.choice(cands)
    w = words[i]
    j = rng.randrange(1, len(w) - 2)
    mode = rng.choice(["drop", "swap", "double"])
    if mode == "drop":
        w = w[:j] + w[j + 1:]
    elif mode == "swap":
        w = w[:j] + w[j + 1] + w[j] + w[j + 2:]
    else:
        w = w[:j] + w[j] + w[j:]
    words[i] = w
    return " ".join(words)


def speak(rng, text, protect=""):
    """A little more of how people type: a typo, a spoken opener, a polite form, a capital and a question mark."""
    first = re.findall(r"[a-z']+", text.lower())[:1]
    first = first[0] if first else ""
    if rng.random() < 0.12:
        text = _typo(rng, text, protect)
    if first in WH and rng.random() < 0.08:
        text = rng.choice(["can you tell me ", "could you tell me ", "do you remember "]) + text
        first = "can"
    if first not in NO_OPENER and rng.random() < 0.14:
        text = rng.choice(OPENERS) + text
    if rng.random() < 0.05 and not text.endswith("?"):
        text += rng.choice([" please", " pls"])
    if rng.random() < 0.13:
        text = text[:1].upper() + text[1:]
        if first in WH + ("is", "are", "was", "does", "do", "can", "could", "did") and not text.endswith(("?", "!")):
            text += "?"
    return text


def sc_(rng, prompt, now, w, steps, notes, history, kind, **rules):
    w.history = history
    sc = Scenario(FAMILY, speak(rng, style(rng, prompt), rules.get("stem", "")) if rules.pop("styled", True) else prompt, now, w, steps, notes)
    sc.history = history
    sc.approve = rules.pop("approve", None)
    sc.expect_cards = list(rules.pop("cards", ()))
    sc.must_see = []
    sc.kind = kind
    sc.rules = rules
    # meta.anchor_pos: where the answer's anchor sits; a never-said kind has no anchor (its anchor=0 only picks the history)
    sc.anchor_pos = rules["anchor"] if isinstance(rules.get("anchor"), int) and kind not in NEVER_QKIND else None
    _recall_labels(sc, kind, rules)
    return sc


# RL-SCOPE C6/C7: the recall labels follow the question KIND. A recall kind carries the refs the answer must bring
# back (the asked-for detail first); a never-said kind carries the detail that never came up; every other kind
# (agenda/mail/weather re-reads, challenges, recaps, memory lookups) carries none. `thread` is left to build.py,
# which derives it from the seeded history (build.conversation_key) so one rule serves every session family.
RECALL_QKIND = {"l_user": "rec_user", "l_first": "rec_user", "s4_first": "rec_user", "l_asst": "rec_asst", "s4_goback": "rec_asst"}
NEVER_QKIND = {"l_never_user", "l_never_asst"}


def _recall_labels(sc, kind, rules):
    if kind in RECALL_QKIND:
        refs = tuple(rules.get("must") or ())
        assert refs, f"{kind}: a recall kind without refs"
        sc.qkind, sc.recall_refs = RECALL_QKIND[kind], refs
    elif kind in NEVER_QKIND:
        sc.qkind = "rec_none"
        sc.recall_absent = rules.get("stem") or (rules.get("absent") or ("",))[0]
        assert sc.recall_absent, f"{kind}: a never-said kind without the absent detail"


# =============================================================================
# earlier exchanges that have nothing to do with the question
# =============================================================================
# (user, assistant, topic: a noun phrase that follows "about", first keyword)
FILL = [
    ("what's 18% tip on 64 dollars", "An 18% tip on $64 comes to $11.52, so $75.52 in total.", "the tip on a $64 bill", "tip"),
    ("how many miles is a 10k", "A 10k is about 6.2 miles.", "how many miles a 10k is", "10k"),
    ("any trick for remembering names", "Repeat the name back right away, then use it once more before the conversation ends. Linking it to a picture in your mind helps too.", "a way to remember names", "names"),
    ("how do you say thank you in spanish", "Gracias, or muchas gracias when you want to mean it a little more.", "saying thank you in Spanish", "spanish"),
    ("how hot is 400 fahrenheit in celsius", "400°F is about 204°C.", "converting 400°F to Celsius", "celsius"),
    ("is rice ok after 3 days in the fridge", "Cooked rice is usually fine for three or four days if it was cooled quickly. Reheat it until it is steaming hot.", "whether three-day-old rice is safe", "rice"),
    ("what does ephemeral mean", "Ephemeral means lasting only a short time, like morning mist or a soap bubble.", "the meaning of ephemeral", "ephemeral"),
    ("name ideas for a lazy orange cat", "Marmalade suits the color and Biscuit suits the napping. Waffle covers both.", "a name for a lazy orange cat", "cat"),
    ("how long do i steep green tea", "About two minutes in water that has cooled a little from the boil. Longer or hotter turns it bitter.", "steeping green tea", "tea"),
    ("how many pints are in a quart", "There are 2 pints in a quart.", "pints in a quart", "pints"),
    ("help me say no politely to a group project invite", "Try something like: “Thank you for thinking of me. I'm at capacity this term, so I'll pass, but I hope it goes well.”", "a polite no to a project invite", "polite"),
    ("a quieter word for happy?", "Content or pleased, both softer than happy. “At ease” is gentler still.", "a quieter word for happy", "happy"),
    ("capital of new zealand?", "Wellington, on the southern tip of the North Island.", "the capital of New Zealand", "zealand"),
    ("why are sunsets orange", "At a low angle the light travels through more air, which scatters the blue away and leaves the reds and oranges.", "why sunsets look orange", "sunsets"),
    ("square root of 144", "12.", "the square root of 144", "144"),
    ("how much caffeine is in a cup of coffee", "Roughly 95 milligrams in a regular 8-ounce cup, though it varies a lot from brew to brew.", "the caffeine in a cup of coffee", "caffeine"),
    ("quick breakfast ideas", "Greek yogurt with fruit and honey, or toast with peanut butter and banana. Both take under five minutes.", "quick breakfast ideas", "breakfast"),
    ("how do i stop hiccups", "Slow sips of cold water, or holding your breath for a count of ten, often help. They usually fade on their own within minutes.", "stopping hiccups", "hiccups"),
    ("stretch for a stiff neck?", "Tilt your ear toward one shoulder and hold for twenty seconds, then switch sides. Keep your shoulders low and breathe slowly.", "a stretch for a stiff neck", "neck"),
    ("double 3/4 cup for me", "Doubling 3/4 cup gives you 1 1/2 cups.", "doubling 3/4 cup", "3/4"),
    ("tell me a fun fact about octopuses", "An octopus has three hearts, and the main one stops beating while it swims, which is partly why it prefers to crawl.", "a fun fact about octopuses", "octopuses"),
    ("how do you spell restaurant", "R-E-S-T-A-U-R-A-N-T. The tricky part is the silent-looking U in the middle.", "how to spell restaurant", "restaurant"),
    ("what time zone is denver in", "Denver is on Mountain Time, one hour ahead of Pacific.", "Denver's time zone", "denver"),
    ("a good book for a long flight", "Project Hail Mary keeps a long flight moving, with a lonely astronaut, a puzzle and an unexpected friend.", "a book for a long flight", "flight"),
    ("fewer vs less, which one", "Use fewer for things you can count, like apples, and less for amounts, like water. Fewer apples, less water.", "fewer versus less", "fewer"),
    ("how do i boil an egg with a soft yolk", "Lower the egg into boiling water for six to seven minutes, then move it into cold water to stop the cooking.", "boiling an egg with a soft yolk", "egg"),
    ("protein in one egg?", "About six grams in a large egg.", "protein in an egg", "protein"),
    ("pick a number between 1 and 10", "Seven.", "a number between 1 and 10", "number"),
    ("one line goodbye for a coworker who's leaving", "It won't be the same without you, and we're all better for having worked with you.", "a goodbye line for a coworker", "goodbye"),
    ("how long does a 3 mile walk take", "At a steady pace of about three miles an hour, it takes roughly an hour.", "how long a three-mile walk takes", "walk"),
    ("is a peanut really a nut", "Botanically it is a legume, closer to a pea than to an almond, though cooks and shoppers treat it as a nut.", "whether a peanut is a nut", "peanut"),
    ("what can i use instead of buttermilk", "Stir a tablespoon of lemon juice or vinegar into a cup of milk and let it sit for five minutes.", "a buttermilk substitute", "buttermilk"),
    ("name the primary colors", "Red, yellow and blue for paint, and red, green and blue for light.", "the primary colors", "colors"),
    ("a 5 minute mindfulness exercise", "Sit comfortably and follow your breath, counting each exhale up to ten, then start over. When your mind wanders, simply return to one.", "a five minute mindfulness exercise", "mindfulness"),
    ("7 times 13", "91.", "seven times thirteen", "thirteen"),
    ("how do you pronounce quinoa", "KEEN-wah.", "how to pronounce quinoa", "quinoa"),
    ("tips for sleeping on a plane", "A neck pillow, an eye mask and skipping caffeine for several hours beforehand all help. Switching your phone to airplane mode early keeps you from scrolling.", "sleeping on a plane", "plane"),
    ("seconds in an hour?", "3,600.", "seconds in an hour", "seconds"),
    ("what's a good houseplant that survives neglect", "A snake plant is nearly unkillable. It likes bright light but tolerates dim corners, and it only needs water every couple of weeks.", "a houseplant that survives neglect", "houseplant"),
    ("at what temperature does water freeze in fahrenheit", "32°F.", "the freezing point of water", "freezing"),
]



# Longer answers, the way a full Ask reply reads (the Ask prompt asks for full answers): they make the
# histories realistic in length and keep the recalled fact buried a few hundred characters deep.
LONG = [
    ("how do i start a sourdough starter", "Mix equal parts flour and water by weight in a jar, about 50 grams of each, and leave it loosely covered at room temperature. Each day, discard half and feed it the same amounts again. After five to seven days it should double within a few hours of feeding and smell pleasantly sour, which means it is ready to bake with.", "starting a sourdough starter", "sourdough"),
    ("what does inflation do to savings over time", "Inflation slowly shrinks what each dollar can buy. If prices rise 3% a year, $1,000 in a plain savings account that earns nothing buys about $970 worth of today's goods after twelve months. Interest from a savings account or bonds can offset some of it, which is why money you will not need for years is usually kept somewhere that earns more than it loses.", "inflation and savings", "inflation"),
    ("how can i focus better when studying", "Start with a single small goal for the session, like finishing one problem set section, and put your phone in another room. Work in blocks of about 25 minutes with a five minute break between them. A short walk or a glass of water at each break helps more than scrolling, and it keeps the next block from feeling like a slog.", "focusing while studying", "focus"),
    ("why does bread dough need to rest", "Resting lets the flour absorb the water fully, which makes the dough smoother and easier to shape. It also gives the gluten time to relax, so the dough stops springing back when you roll it. For yeast breads, the rest is also when the flavor develops, so a longer, cooler rise usually tastes better.", "why bread dough needs to rest", "dough"),
    ("how do i ask a professor for an extension without sounding flaky", "Write early, say plainly what happened and offer a new date. Something like: “I'm running behind on the project because of two overlapping deadlines. Could I submit it by Friday at noon? I can send a draft of what I have by Wednesday.” Short, specific and with a plan reads as responsible rather than flaky.", "asking a professor for an extension", "extension"),
    ("give me a simple beginner workout week", "Three days a week is plenty to start: Monday a 20 minute walk or jog, Wednesday bodyweight squats, push-ups and a plank for three rounds, and Friday another walk with some stretching. Rest on the other days. After three weeks, add five minutes to the cardio or one more round to the strength day.", "a beginner workout week", "workout"),
    ("why do dogs tilt their heads", "Dogs tilt their heads partly to hear better, since the angle helps them pinpoint where a sound comes from. It may also help them see your mouth and face around their muzzle. Many dogs learned that the gesture gets a warm reaction from people, so some of it is simply a habit that works.", "why dogs tilt their heads", "dogs"),
    ("how do i make popcorn on the stove", "Heat two tablespoons of oil in a large pot with a lid over medium-high heat, and drop in three kernels. When they pop, add half a cup of kernels, cover, and shake the pot gently every few seconds. Once the popping slows to a couple of seconds between pops, take it off the heat and add salt while it is still warm.", "making stovetop popcorn", "popcorn"),
    ("how does a thermostat work, briefly", "A thermostat measures the air temperature and compares it with the number you set. When the room drifts below it, the thermostat switches the heating on, and when the room reaches the target it switches it off again. Many have a small buffer so the system does not flick on and off every few minutes.", "how a thermostat works", "thermostat"),
    ("how should i prepare for an interview in two days", "Spend the first day learning the company: what it makes, who its customers are and one recent piece of news. The second day, practice answers to three common questions out loud, including why you want the role, and write down two questions to ask them. Lay out your clothes and the route the night before so the morning is calm.", "preparing for an interview", "interview"),
    ("how do i keep basil alive indoors", "Give it at least six hours of bright light, like a south-facing window, and water when the top inch of soil feels dry. Pinch off the top leaves regularly so it grows bushy instead of tall, and remove any flower buds as soon as they appear. If the leaves turn yellow, you are probably watering too often.", "keeping basil alive indoors", "basil"),
    ("what should i bring for a day hike", "Water is the main thing, about a liter for every two hours, plus snacks like nuts or fruit. Add a light rain layer, sun protection, a small first aid kit and a charged phone with an offline map. Wear shoes with decent grip, and tell someone your route and when you expect to be back.", "what to bring on a day hike", "hike"),
]


# More short answers (round 4 review: the first bank was too small and repeated across scenarios).
FILL_B = [
    ("how many ounces in a cup", "There are 8 fluid ounces in a cup.", "ounces in a cup", "ounces"),
    ("is it ok to run on a sore knee", "Light walking is usually fine, but running on a sore knee tends to make it worse. If it still hurts after a couple of days of rest, it is worth having someone look at it.", "running on a sore knee", "knee"),
    ("spell necessary for me", "N-E-C-E-S-S-A-R-Y: one collar and two sleeves, meaning one C and two S's.", "spelling necessary", "necessary"),
    ("what's the plural of cactus", "Either cacti or cactuses. Both are accepted, though cacti is more common.", "the plural of cactus", "cactus"),
    ("difference between affect and effect", "Affect is usually the verb and effect is usually the noun: the weather affects your mood, and it has an effect on it.", "affect versus effect", "affect"),
    ("how long to roast a whole chicken", "About 20 minutes per pound at 425°F, so a four pound bird takes roughly 80 minutes. It is done when the thickest part of the thigh reads 165°F.", "roasting a whole chicken", "chicken"),
    ("convert 5 feet 10 to centimeters", "5 feet 10 inches is about 178 centimeters.", "converting a height to centimeters", "centimeters"),
    ("what's a good stretch for tight hips", "A low lunge with the back knee down works well. Hold it for thirty seconds on each side and keep your torso upright.", "a stretch for tight hips", "hips"),
    ("whats the boiling point of water at altitude", "It drops about one degree Fahrenheit for every 500 feet you climb, so at 5,000 feet water boils near 202°F.", "boiling water at altitude", "altitude"),
    ("synonym for important", "Significant, crucial or essential, depending on how strong you want it to sound.", "a synonym for important", "synonym"),
    ("why do we have leap years", "The year is about 365.25 days long, so adding a day every four years keeps the calendar lined up with the seasons.", "why we have leap years", "leap"),
    ("what does gdp stand for", "Gross domestic product: the total value of goods and services a country produces in a year.", "what GDP stands for", "gdp"),
    ("how do i get a red wine stain out of a shirt", "Blot it, don't rub. Then cover it with salt, let it sit a few minutes, and rinse with cold water before washing.", "removing a red wine stain", "stain"),
    ("how many calories in a banana", "A medium banana has about 105 calories.", "calories in a banana", "banana"),
    ("what is a haiku", "A haiku is a short Japanese-style poem of three lines, traditionally with five, seven and five syllables.", "what a haiku is", "haiku"),
    ("is it ok to microwave leftover pizza", "Yes, though it turns soft. A skillet on medium heat with a lid crisps the bottom and warms the top.", "reheating leftover pizza", "pizza"),
    ("how far away is the moon", "About 384,000 kilometers, or 239,000 miles, on average.", "how far away the moon is", "moon"),
    ("how often should i water a succulent", "Roughly every two weeks, once the soil has dried out completely. In winter, less often.", "watering a succulent", "succulent"),
    ("what's the difference between baking soda and baking powder", "Baking soda needs an acid in the recipe to work, while baking powder already contains one. Swapping them without adjusting usually gives flat or bitter results.", "baking soda versus baking powder", "baking"),
    ("who painted the starry night", "Vincent van Gogh, in 1889.", "who painted The Starry Night", "starry"),
    ("how many minutes in a day", "1,440.", "minutes in a day", "minutes"),
    ("what kind of podcast helps you fall asleep", "Slow, quiet storytelling or a soft nature-sound show tends to work well. Anything with a steady voice and no sudden jumps in volume.", "a calm podcast for sleep", "podcast"),
    ("how many steps does the average person walk a day", "Most adults take between 4,000 and 6,000 steps on an ordinary day, and 7,000 to 8,000 is a common health target.", "steps in a day", "steps"),
    ("is it rude to be early to a dinner party", "A few minutes early is fine, but arriving much sooner can catch a host mid-preparation. Ten minutes after the start time is a safe window.", "arriving early to a dinner party", "party"),
    ("meaning of the word serendipity", "Serendipity is finding something good by chance while looking for something else.", "the meaning of serendipity", "serendipity"),
    ("how many players on a basketball team on court", "Five per side, so ten on the court at once.", "players on a basketball court", "basketball"),
    ("what's a good first language to learn programming", "Python is a gentle start, with readable syntax and plenty of beginner material. JavaScript is a good alternative if you want to build web pages.", "a first programming language", "programming"),
    ("how cold is too cold to run outside", "Many people run comfortably down to around 20°F with layers, gloves and a hat. Ice is a bigger concern than the temperature itself.", "running in the cold", "cold"),
    ("what are good snacks for a long study session", "Nuts, an apple with peanut butter, or yogurt keep your energy steady without a crash. Water alongside helps more than a second coffee.", "study snacks", "snacks"),
    ("tell me a short riddle", "What has keys but can't open locks? A piano.", "a short riddle", "riddle"),
    ("is a tomato a fruit or a vegetable", "Botanically it is a fruit, since it grows from a flower and holds seeds, but in the kitchen it is treated as a vegetable.", "whether a tomato is a fruit", "tomato"),
    ("what's the longest river in the world", "The Nile and the Amazon are the two contenders, and the Nile is traditionally listed as the longest at about 6,650 kilometers.", "the longest river", "river"),
    ("how do i say good morning in french", "Bonjour, which works until the evening, when bonsoir takes over.", "good morning in French", "french"),
    ("my laptop feels slow, quick fixes?", "Restart it, close unused browser tabs and check which apps are using the most memory. Keeping about 10 percent of storage free also helps.", "speeding up a slow laptop", "laptop"),
    ("good warm up before lifting", "Five minutes of easy cardio, then a few light sets of the first exercise you plan to do. Add weight gradually.", "a warm up before lifting", "lifting"),
    ("what is the speed of light", "About 300,000 kilometers per second, or 186,000 miles per second.", "the speed of light", "speed"),
    ("difference between a crocodile and an alligator", "Alligators have a wide, rounded snout and crocodiles have a narrower, pointed one. Crocodiles also show their lower teeth when their mouths are closed.", "alligators versus crocodiles", "alligator"),
    ("can you explain compound interest simply", "Compound interest is interest earned on your interest. Put $100 at 5% a year and you earn $5 the first year, then $5.25 the next, and the gap widens as the years go by.", "compound interest", "compound"),
    ("how long should i rest between sets", "About 60 to 90 seconds for lighter lifts and two to three minutes for heavy ones.", "rest between sets", "between sets"),
    ("what is mise en place", "It's a French cooking term for having everything chopped, measured and in place before you start cooking.", "what mise en place means", "mise"),
    ("a good name for a small bakery", "Something warm and short, like Crumb & Co, The Rising Loaf or Little Oven.", "a name for a bakery", "bakery"),
    ("how much water should i drink a day", "A common guide is about two liters, or eight cups, though it depends on your size, activity and the weather. Pale yellow urine is a simple sign you're doing fine.", "daily water intake", "water"),
    ("why is the ocean salty", "Rivers carry small amounts of dissolved minerals into the sea, and when water evaporates the salt stays behind. Over millions of years it builds up.", "why the ocean is salty", "ocean"),
    ("how do i politely end a long phone call", "Signal it early: “I'm so glad we caught up. I have to get going, but let's talk again soon.” Then say a warm goodbye.", "ending a long call politely", "phone call"),
    ("how many strings does a standard guitar have", "Six: E, A, D, G, B and E, from lowest to highest.", "strings on a guitar", "guitar"),
    ("whats a good tempo for a running playlist", "Around 160 to 180 beats per minute matches a steady running cadence for most people. A slightly slower mix works for easy days.", "a tempo for running music", "tempo"),
    ("how do i tell if an egg is still fresh", "Drop it in a glass of water. A fresh egg sinks and lies flat, while an older one tilts or floats and should be thrown out.", "checking if an egg is fresh", "fresh"),
    ("why do cats purr", "Cats purr when they are content, but also to soothe themselves when they are stressed or hurt. The low vibration seems to be calming in both cases.", "why cats purr", "purr"),
    ("what's the opposite of generous", "Stingy or miserly, with selfish or tight-fisted as other choices depending on the tone.", "the opposite of generous", "opposite"),
    ("how tall is mount rainier", "Mount Rainier is 14,411 feet, the highest peak in Washington State.", "the height of Mount Rainier", "rainier"),
    ("what's a decent warm drink that isn't coffee or tea", "Hot cocoa, warm apple cider or hot water with lemon and honey are all comforting. Golden milk with turmeric is a good evening option.", "a warm drink besides coffee", "cider"),
    ("is it better to stretch before or after a run", "A few dynamic moves like leg swings before, and longer holds after, once your muscles are warm.", "stretching around a run", "stretch"),
    ("how many weeks are in a year", "There are 52 weeks in a year, plus a day or two left over.", "weeks in a year", "weeks"),
    ("a word for someone who loves books", "A bibliophile, or more casually a bookworm.", "a word for a book lover", "bibliophile"),
]

LONG_B = [
    ("how do i make cold brew at home", "Combine one cup of coarse ground coffee with four cups of cold water in a jar, stir, and leave it in the fridge for 12 to 18 hours. Strain through a fine filter or a clean cloth, then dilute it with water or milk to taste. It keeps about a week in the fridge.", "making cold brew", "brew",
     "Do you like it strong or on the mild side?"),
    ("what's the best way to learn a new language on my own", "Short daily sessions beat long weekly ones, so aim for twenty minutes each day. Mix listening, speaking and reading, and start talking early even if it feels clumsy. A language exchange partner or a weekly tutor session gives you real feedback, which solo apps can't.", "learning a language alone", "language", ""),
    ("how do i get better at public speaking", "Practice out loud, ideally recording yourself, so you can hear where you rush or fill the silence. Start with a single clear point and build the talk around it. Slow down at the start; nerves make everyone speed up, and a deliberate first sentence settles the rest.", "public speaking", "speaking",
     "Is there a talk coming up?"),
    ("how should i organize my notes for a big exam", "Group them by topic rather than by date, then shrink each topic to one page of key ideas and examples. Quizzing yourself from that page does more than rereading. Keep a short list of the questions you got wrong for the last evening.", "organizing notes for an exam", "organizing", ""),
    ("what's a good routine for sleeping better", "Keep the same wake time every day, even on weekends, and dim the lights an hour before bed. Cut caffeine after noon and keep your phone out of reach once you're in bed. If you can't sleep after twenty minutes, get up and do something quiet until you feel drowsy.", "a routine for better sleep", "routine", ""),
    ("how do i deal with a roommate who leaves dishes out", "Pick a calm moment rather than the moment you see the dishes, and describe the problem without blame: “The sink has been full a few days and it's getting to me.” Then propose something concrete, like each person washing up the same evening they use something.", "a roommate and the dishes", "dishes",
     "Has it come up between you before?"),
    ("how do i save money on groceries as a student", "Plan a few meals before you shop and buy staples like rice, beans and oats in bulk. Store brands are usually just as good, and frozen vegetables cost less and keep longer. The shelves at knee height and below tend to hold the cheaper options.", "saving on groceries", "groceries", ""),
    ("how can i be more productive in the morning", "Pick the one task that matters most the night before and do it first, before email. Natural light and a glass of water help you wake up properly. Keep the first hour free of meetings if you can.", "a productive morning", "productive",
     "Are you more of an early riser or a night person?"),
    ("how does a vaccine work", "A vaccine shows your immune system a harmless piece or copy of a germ, so it can practice. Your body then keeps a memory of it, and when the real germ shows up, it responds much faster.", "how vaccines work", "vaccine", ""),
    ("how do i start journaling", "Keep it small: a few lines each evening about what happened and how you felt. There's no wrong way, and missing a day doesn't matter. Prompts like “what went well today?” help when you're staring at a blank page.", "starting a journal", "journaling",
     "Would you rather write it by hand or on your phone?"),
    ("what makes a good thesis statement", "A good thesis makes one clear claim that someone could reasonably disagree with, and it previews the reasons you'll give. “Social media is bad” is too vague, while “Short-form video shortens teens' attention on homework because it rewards constant switching” gives you something to argue.", "writing a thesis statement", "thesis statement", ""),
    ("how do i train a puppy to stop biting", "Redirect to a chew toy the moment teeth touch skin, and stop the play for a few seconds if it continues so the puppy learns biting ends the fun. Short, calm sessions work better than long ones, and tired puppies bite more, so make sure there's a nap in the afternoon.", "stopping puppy biting", "puppy",
     "How old is the puppy?"),
]

# Round 4 review pass: a wider bank, so no exchange recurs in more than a handful of the 151 histories.
FILL_C = [
    ("how many tablespoons are in a quarter cup", "There are 4 tablespoons in a quarter cup.", "tablespoons in a quarter cup", "tablespoons"),
    ("what makes a decent password", "A long passphrase of several unrelated words, a different one for each important account, and a password manager to remember them all.", "good passwords", "passwords"),
    ("how do you say hello in japanese", "Konnichiwa is the usual daytime greeting, and ohayou is good morning.", "hello in Japanese", "japanese"),
    ("how tall is mount everest", "About 8,849 meters, or 29,032 feet, above sea level.", "the height of Everest", "everest"),
    ("is it fine to eat eggs every day", "For most healthy people one or two a day is fine. If you have heart disease or high cholesterol, it's worth asking your doctor.", "eating eggs every day", "eggs every day"),
    ("what's the difference between weather and climate", "Weather is what's happening outside right now, and climate is the pattern of weather over many years.", "weather versus climate", "climate"),
    ("what do you call a baby kangaroo", "A joey.", "a baby kangaroo", "kangaroo"),
    ("how many bones does an adult have", "An adult has 206 bones, though babies are born with around 300 that fuse over time.", "bones in an adult", "bones"),
    ("how do i convert miles to kilometers", "Multiply by 1.6. Ten miles is about 16 kilometers.", "miles to kilometers", "kilometers"),
    ("what's a good gift for a coworker under 20 dollars", "A small plant, a nice notebook or a bag of good coffee all land well and don't feel like too much.", "a coworker gift", "coworker gift"),
    ("why do onions make you cry", "Cutting an onion releases a gas that reacts with the moisture in your eyes and forms a mild irritant, so your eyes tear to flush it out.", "why onions make you cry", "onions"),
    ("how long does it take to hard boil an egg", "About ten to twelve minutes in boiling water, then straight into ice water so the shell comes off easily.", "hard boiling an egg", "hard boil"),
    ("what's the plural of mouse", "Mice for the animal, and mouses or mice for the computer kind, with mice more common.", "the plural of mouse", "plural of mouse"),
    ("what does rsvp stand for", "It comes from the French phrase répondez s'il vous plaît, meaning please reply.", "what RSVP stands for", "rsvp"),
    ("how do you get gum out of hair", "Rub peanut butter or oil into the gum, wait a minute, and comb it out gently from the ends toward the scalp.", "gum in hair", "gum"),
    ("how many continents are there", "Seven by the most common count: Africa, Antarctica, Asia, Australia, Europe, North America and South America.", "how many continents there are", "continents"),
    ("what is a good bedtime for a teenager", "Most teenagers need eight to ten hours, so a bedtime between 9:30 and 11 PM fits a typical school morning.", "a teenager's bedtime", "bedtime"),
    ("is dark chocolate healthy", "In small amounts, yes. The darker kinds have more cocoa and less sugar, and they contain some helpful antioxidants.", "whether dark chocolate is healthy", "chocolate"),
    ("what's the word for fear of heights", "Acrophobia.", "the word for a fear of heights", "heights"),
    ("how do i make my own salad dressing", "Whisk three parts olive oil with one part vinegar or lemon juice, then add a pinch of salt, a little mustard and a touch of honey.", "making salad dressing", "dressing"),
    ("how many days are in december", "Thirty-one.", "the days in December", "december"),
    ("what is the smallest prime number", "Two, and it's also the only even prime.", "the smallest prime number", "prime"),
    ("what's a good first aid step for a minor burn", "Cool it under running water for ten to twenty minutes, then cover it loosely with a clean, non-stick dressing.", "treating a minor burn", "burn"),
    ("what's the difference between a latte and a cappuccino", "A latte has more steamed milk and just a thin layer of foam, while a cappuccino is a bit stronger, with equal parts espresso, milk and foam.", "latte versus cappuccino", "cappuccino"),
    ("how often should i replace my toothbrush", "About every three months, or sooner if the bristles look frayed.", "replacing a toothbrush", "toothbrush"),
    ("what does a bee do with pollen", "Worker bees carry it back to the hive and mix it with nectar to feed the colony, especially the young.", "what bees do with pollen", "pollen"),
    ("how do you pronounce worcestershire", "WUSS-ter-sher, with the middle part nearly swallowed.", "how to pronounce Worcestershire", "worcestershire"),
    ("how long is a marathon", "26.2 miles, which is 42.195 kilometers.", "the length of a marathon", "marathon"),
    ("what's the best way to reheat rice", "Sprinkle a spoonful of water over it, cover, and microwave until it's steaming hot all the way through.", "reheating rice", "reheating"),
    ("what should i do if my houseplant's leaves are turning brown", "Check for overwatering first, since soggy soil is the common cause. Dry air and too much direct sun can also scorch the tips.", "brown leaves on a houseplant", "brown leaves"),
    ("how many hours of sleep do adults need", "Most adults do best with seven to nine hours a night.", "sleep for adults", "hours of sleep"),
    ("how do i say sorry for a late reply", "Keep it short: “Sorry for the slow reply, and thank you for waiting.” Then answer the question right away.", "apologizing for a late reply", "late reply"),
    ("what is the freezing point of water in kelvin", "273.15 kelvin.", "water's freezing point in kelvin", "kelvin"),
    ("which is bigger, a kilobyte or a megabyte", "A megabyte, which is about a thousand kilobytes.", "kilobytes versus megabytes", "megabyte"),
    ("what's a good warm up song for a morning run", "Anything with a steady beat around 150 beats per minute works. Upbeat pop or an easy electronic mix tends to suit early starts.", "a morning run song", "run song"),
    ("tell me a fun fact about honey", "Honey never really spoils. Jars found in ancient tombs were still edible after thousands of years.", "a fact about honey", "fact about honey"),
    ("what is the plural of fish", "Fish, most of the time. Fishes is used when you mean several different species.", "the plural of fish", "plural of fish"),
    ("how far is a light year", "About 9.46 trillion kilometers, or nearly 6 trillion miles.", "how far a light year is", "light year"),
    ("is it okay to wash a cast iron pan with soap", "A little mild soap is fine on a seasoned pan. Dry it right away and wipe on a thin layer of oil.", "washing cast iron", "cast iron"),
    ("what is a good afternoon snack with protein", "Cottage cheese with fruit, a hard-boiled egg, or a handful of almonds each give you a steady lift.", "a protein snack", "protein snack"),
    ("how many seasons are there in a year", "Four: spring, summer, autumn and winter, at least in the temperate parts of the world.", "the number of seasons", "seasons"),
    ("how do i remove a splinter", "Wash the area, then use clean tweezers to pull it out along the angle it went in. Wash again afterward.", "removing a splinter", "splinter"),
    ("what's the capital of australia", "Canberra, not Sydney or Melbourne.", "the capital of Australia", "australia"),
    ("how do i fold a fitted sheet", "Tuck the corners into each other, then lay it flat and fold the edges into a rectangle before folding it in thirds.", "folding a fitted sheet", "fitted sheet"),
    ("how many grams in an ounce", "About 28.35 grams.", "grams in an ounce", "grams"),
    ("what plants are safe for cats", "Spider plants, Boston ferns and parlor palms are all considered safe. Lilies are dangerous for cats and best avoided.", "cat-safe plants", "cat-safe"),
    ("why is the sky blue", "Sunlight scatters off the air, and blue light scatters the most because of its short wavelength, so blue reaches your eyes from every direction.", "why the sky is blue", "sky"),
    ("how long should i let steak rest", "About five minutes for a regular steak, and ten for a thick one, so the juices settle back into the meat.", "resting a steak", "steak"),
    ("what's a good title for a poem about autumn", "“What the Leaves Keep,” “October Light” or “The Slow Gold” all fit.", "a title for an autumn poem", "autumn"),
    ("how many teaspoons make a tablespoon", "Three teaspoons make one tablespoon.", "teaspoons in a tablespoon", "teaspoons"),
    ("what's the difference between a virus and a bacterium", "Bacteria are living single cells that can multiply on their own, while viruses need a host cell to reproduce.", "viruses versus bacteria", "bacteria"),
    ("how do you spell accommodate", "A-C-C-O-M-M-O-D-A-T-E: two C's and two M's.", "spelling accommodate", "accommodate"),
    ("what is a leap second", "An extra second occasionally added to clocks to keep them in step with the Earth's slightly uneven rotation.", "what a leap second is", "leap second"),
    ("how do i stop my glasses from fogging with a mask", "Pinch the mask's nose wire tightly and tuck the top edge under your glasses, or wash the lenses with a drop of soapy water and let them dry.", "glasses fogging with a mask", "fogging"),
    ("what's a good rainy day activity", "A puzzle, a long movie or baking something simple. A walk with a good umbrella is nice too if you're up for it.", "a rainy day activity", "rainy day"),
    ("name a few classic board games", "Chess, Scrabble, Monopoly, Catan and Clue are all long-time favorites.", "classic board games", "board games"),
    ("how many time zones does russia have", "Eleven.", "Russia's time zones", "russia"),
    ("what is a good way to store fresh herbs", "Trim the stems and stand them in a glass of water in the fridge, loosely covered, except basil, which prefers the counter.", "storing fresh herbs", "herbs"),
    ("what's the plural of octopus", "Octopuses is the most common, and octopi is also used, though it's technically a misfit from Latin.", "the plural of octopus", "plural of octopus"),
]

LONG_C = [
    ("how do i choose a good watermelon", "Look for one that feels heavy for its size, with a creamy yellow spot on one side where it sat in the field. A dull rind is better than a shiny one, and a hollow sound when you tap it is a good sign.", "picking a watermelon", "watermelon", ""),
    ("what should i do before a long road trip", "Check the tires, oil and fluid levels, and top up the windshield washer. Plan your fuel stops and save an offline map, and pack water, snacks and a basic emergency kit. Sharing your route with someone at home is a good habit too.", "preparing for a road trip", "road trip", "Where are you headed?"),
    ("how can i make my apartment feel cozier", "Warm, low lighting does the most, so swap a harsh overhead bulb for a lamp or two. Add a soft throw and a plant, and keep surfaces clear enough that the room feels calm. Candles or a rug under the couch help as well.", "making an apartment cozier", "cozier", ""),
    ("how do i choose running shoes", "Go to a store in the afternoon, when your feet are a little swollen, and leave about a thumb's width in front of your toes. Cushioned shoes suit longer easy runs, while lighter ones suit speed work. Fit matters more than the brand.", "choosing running shoes", "running shoes", "Do you run on roads or trails?"),
    ("what's the best way to learn to cook as an adult", "Start with five or six simple dishes and repeat them until they feel automatic, such as eggs, pasta, a stir-fry and a sheet-pan dinner. Learn to season as you go, and taste constantly. A sharp knife and one good pan make everything easier.", "learning to cook", "learn to cook", ""),
    ("how should i prepare for a job fair", "Bring several copies of your resume and research three or four employers ahead of time so you can ask real questions. Prepare a ten second introduction about who you are and what you're looking for, and follow up with a short email the next day.", "preparing for a job fair", "job fair", ""),
    ("how can i fall asleep faster", "Keep the room cool and dark, and stop screens about thirty minutes before bed. A steady wind-down routine tells your body what's next. If your mind races, write tomorrow's to-do list on paper so you can set it down.", "falling asleep faster", "asleep faster", "Is it racing thoughts that keep you up?"),
    ("how do i write a good email subject line", "Make it specific and short enough to read in a preview, with the action or deadline up front, like “Draft due Friday: your comments?” Avoid vague words like “quick question” and all caps.", "writing an email subject line", "subject line", ""),
    ("how long does it take to form a habit", "Research suggests a median of about two months, though it ranges from three weeks to nearly a year depending on the habit. Tying it to something you already do each day helps it stick, and missing one day doesn't reset the clock.", "forming a habit", "habit", ""),
    ("what's the easiest way to start recycling better", "Learn what your local program accepts, since rules differ a lot, and keep recyclables clean and dry. A small second bin next to the trash makes it automatic. When in doubt about an item, leave it out so one wrong thing doesn't spoil a batch.", "recycling better", "recycling", ""),
    ("how do i keep my laptop battery healthy", "Avoid letting it sit at 0% or at 100% for long stretches, and keep it out of hot places like a car or under a blanket. Most laptops have a battery-care setting that stops the charge at around 80%, which helps a lot over the years.", "laptop battery health", "laptop battery", ""),
    ("what are good ways to meet people in a new city", "Join something that meets regularly, like a climbing gym, a class, a volunteer shift or a weekly run club, since repeat meetings turn into friendships. Say yes to the first few invitations, and follow up on a plan within a day or two.", "meeting people in a new city", "new city", "Have you just moved?"),
]

CHAINS_C = [
    ([("what's a good plant for a bathroom", "A pothos, a fern or a snake plant all like the humidity. Does the bathroom have a window?"),
      ("a small one", "Then a snake plant or a ZZ plant is safer, since both tolerate low light without complaint.")], "a bathroom plant", "bathroom"),
    ([("how do i start running again after a break", "Begin with run-walk intervals, such as one minute of running and two of walking, for twenty minutes. How long has it been?"),
      ("about three months", "Then give yourself two weeks of gentle intervals before you add distance, and your legs will adjust quickly.")], "running again after a break", "running again"),
    ([("what should i make for a potluck", "A dish that travels and tastes fine at room temperature, like a pasta salad, a tray of roasted vegetables or a big batch of cookies. Is it a dinner or a daytime event?"),
      ("evening", "Then a hearty pasta salad or a baked dish like mac and cheese will go fast.")], "a potluck dish", "potluck"),
    ([("how do i say no to extra shifts", "Be warm and direct: “I can't pick that up this week, but thank you for asking.” You don't need a long reason. Do you want to stay open to future requests?"),
      ("yes, just not this week", "Then say exactly that, and mention a day next week when you might have room.")], "turning down extra shifts", "extra shifts"),
    ([("recommend a podcast for a commute", "A narrative show like a true-story podcast or a short daily news recap fits well in twenty minutes. How long is your commute?"),
      ("about 25 minutes", "Then a show with episodes of 20 to 30 minutes means each ride finishes with a clean ending.")], "a podcast for a commute", "podcast for a commute"),
    ([("how do i keep cut flowers fresh", "Trim the stems at an angle, use clean water with the packet of flower food, and change the water every two days. Where will you put them?"),
      ("on the kitchen table by the window", "Move them out of direct sun and away from the fruit bowl, since ripening fruit gives off a gas that wilts blooms.")], "keeping cut flowers fresh", "cut flowers"),
    ([("what's a good stretch before bed", "A gentle child's pose and a seated forward fold, held for a minute each, help you unwind. Is your back or your legs more tense?"),
      ("my lower back", "Add a knees-to-chest hug while lying down, and rock gently side to side for a minute.")], "a stretch before bed", "before bed"),
    ([("what's the best way to read more", "Keep a book where you'd normally pick up your phone, and set a small target such as ten pages a day. Do you prefer fiction or nonfiction?"),
      ("mostly fiction", "Then pick short novels that you actually look forward to, since finishing a few quickly builds the habit.")], "reading more", "read more"),
    ([("how do i make iced coffee at home", "Brew it strong and let it cool, or make cold brew, then pour it over ice and add milk. Do you take it sweet?"),
      ("a little sweet", "Stir a spoonful of simple syrup into the coffee while it's still warm, since sugar dissolves poorly in cold drinks.")], "iced coffee at home", "iced coffee"),
    ([("how can i learn the guitar chords faster", "Start with four open chords, G, C, D and E minor, and practice switching between them for ten minutes a day. Are you using a metronome yet?"),
      ("no", "Try one at a slow tempo and change chords on every fourth click, then speed up a little each week.")], "learning guitar chords", "guitar chords"),
]

# Short topical chains: a question, a reply that asks something back, the person's answer and a reply.
CHAINS = [
    ([("how much pasta feeds a group", "A pound of dry pasta usually feeds four to six people as a main course. How many are you cooking for?"),
      ("about six, plus big appetites", "Then a pound and a quarter is a safe amount, and it stretches nicely with bread or a salad.")], "pasta for a group", "pasta"),
    ([("i need a gift for a friend who likes plants", "A small potted herb or a pothos would suit most people, since both are forgiving. Do they have a sunny spot at home?"),
      ("not sure, they live in a small apartment", "Then a pothos or a snake plant is safer, since both handle low light well.")], "a gift for a plant lover", "gift"),
    ([("how do i cook rice without a rice cooker", "Rinse one cup of rice, add two cups of water, bring to a boil, then cover and simmer on low for 15 minutes. Do you want it fluffy or a little sticky?"),
      ("fluffy", "Rinse it until the water runs mostly clear, and let it rest covered for five minutes off the heat before fluffing it with a fork.")], "cooking rice on the stove", "stove"),
    ([("what's a good beginner camera", "A used mirrorless camera with a kit lens is a great start, and a recent phone also takes excellent photos. What do you want to shoot most?"),
      ("mostly landscapes and some street stuff", "Then a wide kit lens is perfect. Learn aperture priority and the camera will handle the rest while you practice composition.")], "a beginner camera", "camera"),
    ([("is it too late to start learning piano at 30", "Not at all. Adults often progress quickly because they practice with more focus. Do you have a keyboard at home?"),
      ("not yet", "A basic weighted-key digital piano is enough to start with, and fifteen minutes a day will take you a long way.")], "learning piano as an adult", "piano"),
    ([("how do i get a stuck jar open", "Run the lid under hot water for thirty seconds, or wrap a rubber band around it for grip. Is it a metal lid?"),
      ("yeah metal", "Then tapping around the edge of the lid with a spoon breaks the seal, and the hot water will help it loosen.")], "opening a stuck jar", "jar"),
    ([("any tips for a first 5k", "Alternate running and walking, like two minutes of each, and add a little more running every week. How many weeks do you have before the race?"),
      ("six weeks", "Six weeks is plenty. Run three days a week, lengthen the running intervals each week, and keep an easy day before race day.")], "training for a first 5k", "5k"),
    ([("what's a polite way to decline a wedding invite", "Reply promptly and warmly: “Thank you so much for inviting me. I'm sorry I can't make it, and I'm wishing you both a wonderful day.” Do you know the couple well?"),
      ("pretty well, a college friend", "Then add a personal line about their day, and maybe send a small gift or a note ahead of it.")], "declining a wedding invite", "wedding"),
    ([("how long can sushi sit out", "Raw fish shouldn't sit out for more than about two hours, or one hour in a warm room. Is it from a grocery store or a restaurant?"),
      ("grocery store", "Then keep it chilled until you eat, and throw out anything left after the two hours.")], "leaving sushi out", "sushi"),
    ([("whats a good stretching routine for desk work", "Roll your shoulders, stretch your chest in a doorway and stand up to stretch your hip flexors every hour or so. Does your back or your neck bother you more?"),
      ("neck mostly", "Add a slow ear-to-shoulder stretch and chin tucks, and check that your screen sits at eye level.")], "desk stretches", "desk"),
    ([("what's a good card game for a long car ride", "Twenty questions or the license plate game need no cards, but for cards, Uno is easy to play on a lap. How many people are in the car?"),
      ("three of us", "Then Uno works well with three, and Crazy Eights is a good backup.")], "a game for a car ride", "ride"),
    ([("how do i stop procrastinating on essays", "Start with a ten minute timer and write the worst possible first paragraph. Momentum usually does the rest. Which part feels hardest?"),
      ("starting the intro", "Skip it for now. Write the body first, since the intro is easier once you know what you actually argued.")], "procrastinating on essays", "procrastinating"),
    ([("can dogs eat grapes", "No, grapes and raisins can be toxic to dogs, even in small amounts. Did your dog get into some?"),
      ("no, just wondering", "Good to check. If it ever happens, call a vet or a poison hotline right away rather than waiting for symptoms.")], "grapes and dogs", "grapes"),
    ([("how do i remove a coffee ring from a wood table", "Rub it gently with a paste of baking soda and a little water, wipe dry, then rub a drop of olive oil over the spot. Is the finish glossy or matte?"),
      ("glossy", "Then keep it gentle, since rubbing hard can dull the shine. Finish with furniture polish once the ring fades.")], "removing a ring from a table", "table"),
    ([("what's the best way to cut an onion without crying", "Chill the onion for fifteen minutes first and use a very sharp knife, which crushes fewer cells and releases less of the irritant. Do you use a chef's knife or a small paring knife?"),
      ("small paring knife", "A chef's knife is much easier here. A longer, sharper blade makes cleaner cuts, so it's worth switching.")], "cutting onions", "onion"),
    ([("how do i explain a gap in my resume", "Keep it brief and honest, then move to what you did or learned in that time: “I took a year to care for family and kept my skills up through a course.” Was it a long gap?"),
      ("about eight months", "That's fairly short. A single line in the cover letter is enough, and many hiring managers won't ask about it.")], "a gap on a resume", "gap"),
    ([("how many pushups should i be able to do", "It varies with age, but ten to twenty clean push-ups is a solid baseline for most adults. Are you building toward a goal?"),
      ("just want to hit twenty", "Add two or three reps every few days, and do sets with rest in between rather than maxing out each time.")], "doing twenty push-ups", "push-ups"),
    ([("is it worth getting a standing desk", "Many people find them useful for breaking up long sitting stretches, though the benefit comes from changing position, not standing all day. Do you sit for most of your work?"),
      ("yeah like eight hours", "Then it's worth trying. Alternate every thirty to sixty minutes, and keep the screen at eye level either way.")], "a standing desk", "standing"),
    ([("quick dinner with eggs and spinach", "Scramble the eggs gently with wilted spinach and a bit of cheese, or bake them in a skillet with tomato sauce. Do you want it quick or a bit more special?"),
      ("quick", "Wilt the spinach in a hot pan for a minute, pour in the beaten eggs, stir until just set and season with salt and pepper.")], "a dinner with eggs and spinach", "spinach"),
    ([("what should i pack for a weekend camping trip", "A tent, sleeping bag, warm layers, a headlamp, water and food are the essentials. Will it be cold at night?"),
      ("probably around 40 degrees", "Then bring a sleeping pad as well as a bag rated to at least 30°F, and a warm hat to sleep in.")], "packing for camping", "camping"),
    ([("how do i make my phone battery last longer", "Lower the screen brightness, turn on low power mode and close apps you aren't using. Is it an older phone?"),
      ("two years old", "That's still young for a battery. Check which apps use the most power in settings, and avoid leaving it at 0% or plugged in at 100% all night.")], "making a phone battery last", "battery"),
    ([("synonyms for tired", "Weary, drained, worn out and exhausted all fit, with exhausted being the strongest. Are you describing a person or a mood?"),
      ("a character in my story", "Then weary or worn out feels more human than drained, which sounds clinical. Exhausted works if the scene is a low point.")], "words for tired", "words for tired"),
    ([("how do you make a good cup of tea with milk", "Boil fresh water, steep a black tea bag for three to four minutes, then add the milk after. Do you take sugar?"),
      ("a little honey", "Stir the honey in while the tea is hot so it dissolves, then add milk to taste.")], "a cup of milk tea", "milk tea"),
    ([("how do i know when an avocado is ripe", "Press gently near the stem: it should give slightly. You can also flick off the little cap, and green underneath means it's ready. Are you eating it today?"),
      ("tomorrow", "Then pick one that is slightly firm today and leave it on the counter, and it should be right by tomorrow.")], "picking a ripe avocado", "avocado"),
    ([("is it better to study in the morning or at night", "It depends on when you're most alert, but many people retain more with a morning session and a quick review in the evening. When do you feel sharpest?"),
      ("probably afternoon", "Then use the afternoon for the hardest material and keep mornings for lighter reading.")], "the best time to study", "best time to study"),
]
ACKS = [("ok thanks", "Anytime."), ("perfect, thank you", "You're welcome."), ("great, that makes sense", "Good to hear it."),
        ("cool thanks", "Sure thing."), ("got it, thanks", "Good luck with it."), ("nice, thanks", "You're welcome.")]

_FUSED: dict[str, int] = {}
_BANKS: dict = {}


def _leaky(user, assistant=""):
    """True when an earlier-turn text sits too close to a toolbench case or a round-1 eval question."""
    if leak.fact_hits(user + "\n" + assistant):
        return True
    return bool(A._leaks().leaks(user)[0])


def banks():
    """The filler banks with every exchange removed that the leak index would drop (done once, so a clean bank is
    what the least-used picker draws from)."""
    if not _BANKS:
        _BANKS["short"] = [f for f in FILL + FILL_B + FILL_C if not _leaky(f[0], f[1]) and f[3].lower() in f[2].lower()]
        _BANKS["longx"] = [f for f in LONGX if not _leaky(f[0], f[1]) and f[3].lower() in f[2].lower()]
        _BANKS["long"] = [f for f in LONG + LONG_B + LONG_C if not _leaky(f[0], f[1]) and f[3].lower() in f[2].lower()] + _BANKS["longx"]
        _BANKS["chains"] = [c for c in CHAINS + CHAINS_C if not any(_leaky(u, a) for u, a in c[0]) and c[2].lower() in c[1].lower()]
        _BANKS["acks"] = [a for a in ACKS if not _leaky(a[0], a[1])]
        if os.environ.get("SESSION_RECALL_SOFT"):
            print("BANKS short", len(_BANKS["short"]), "/", len(FILL + FILL_B + FILL_C), "long", len(_BANKS["long"]), "/", len(LONG + LONG_B + LONG_C),
                  "chains", len(_BANKS["chains"]), "/", len(CHAINS + CHAINS_C))
    return _BANKS


def _least_used(rng, pool, key):
    """A member of `pool` that has been used the fewest times in this build (ties broken at random)."""
    low = min(_FUSED.get(key(p), 0) for p in pool)
    return rng.choice([p for p in pool if _FUSED.get(key(p), 0) == low])


def fillers(rng, k, exclude=(), chains=True):
    """k earlier exchanges that have nothing to do with the question: short answers, longer answers (some end
    with a question of their own) and two-turn chains with a follow-up. The least-used exchange first, over all
    three banks together, so the whole bank is spread evenly over the build (no exchange recurs in more than a
    handful of histories)."""
    out, used = [], set(exclude)
    bk = banks()
    while len(out) < k:
        left = k - len(out)
        cands = [("short", f, f[0]) for f in bk["short"] if f[0] not in used]
        cands += [("long", f, f[0]) for f in bk["long"] if f[0] not in used]
        if chains and left >= 2:
            cands += [("chain", c, c[0][0][0]) for c in bk["chains"] if c[0][0][0] not in used]
        low = min(_FUSED.get(c[2], 0) for c in cands)
        kind, f, key = rng.choice([c for c in cands if _FUSED.get(c[2], 0) == low])
        used.add(key)
        _FUSED[key] = _FUSED.get(key, 0) + 1
        if kind == "chain":
            blk = f"chain:{f[0][0][0]}"
            ex = [dict(turn(u, a), topic=f[1], keys=[f[2]], filler=True, cblk=blk, crole="q" if j == 0 else "fu") for j, (u, a) in enumerate(f[0])]
            if left >= 3 and rng.random() < 0.4:
                u, a = rng.choice(bk["acks"])
                ex.append(dict(turn(u, a), topic=f[1], keys=[f[2]], filler=True, cblk=blk, crole="ack"))
            out += ex[:left]
            continue
        u, a, t, kw = f[:4]
        fq = f[4] if len(f) > 4 else ""
        if kind == "long" and fq and rng.random() < 0.75:
            a = f"{a} {fq}"
        out.append(dict(turn(u, a), topic=t, keys=[kw], filler=True))
    return out


def session(rng, anchors, *, before=None, after=None, total_max=9, total_min=3):
    """(history, index of the first anchor): fillers before, the anchor exchange(s), fillers after."""
    n_before = rng.choice([0, 1, 1, 2, 2, 3, 4]) if before is None else before
    n_after = rng.choice([1, 2, 2, 3, 3, 4, 5, 6]) if after is None else after
    while n_before + len(anchors) + n_after > total_max:
        if n_after > 0:
            n_after -= 1
        else:
            n_before -= 1
    while n_before + len(anchors) + n_after < total_min:
        if n_before <= n_after:
            n_before += 1
        else:
            n_after += 1
    users = {a["user"] for a in anchors}
    pre = fillers(rng, n_before, exclude=users)
    post = fillers(rng, n_after, exclude=users | {p["user"] for p in pre})
    return pre + list(anchors) + post, len(pre)


# =============================================================================
# anchors: the earlier exchange whose reply holds (or lacks) the fact
# =============================================================================
EV = [  # title, hour, minute, minutes, location, short
    ("CS 162 lecture", 10, 30, 50, "Kerr Hall 204", "CS 162"),
    ("Study group with Aisha", 14, 15, 90, "Hollis Library, room 3B", "study group"),
    ("Dentist appointment", 15, 30, 60, "Pine Street Dental", "dentist"),
    ("Coffee with Theo", 9, 45, 45, "Brewed Awakening", "coffee"),
    ("Linear algebra class", 13, 15, 75, "Avery Science 110", "linear algebra"),
    ("Library help desk shift", 16, 30, 180, "Hollis Library", "help desk"),
    ("Haircut", 17, 15, 45, "Clipper Joe's", "haircut"),
    ("Advising appointment", 11, 30, 30, "Dean's Office, Mercer Hall", "advising"),
    ("Meeting with Prof. Okafor", 15, 45, 45, "Kerr Hall 412", "Okafor"),
    ("Project sync with Jordan", 12, 45, 60, "Kerr Hall 301", "project sync"),
    ("Dinner with Carmen", 18, 30, 90, "Golden Lotus", "dinner"),
    ("Yoga class", 8, 30, 60, "Rec Center Studio B", "yoga"),
    ("Physics lab", 14, 0, 120, "Avery Science 220", "physics lab"),
    ("Career fair", 10, 0, 180, "Student Union ballroom", "career fair"),
    ("Orchestra rehearsal", 19, 15, 120, "Meyer Hall", "rehearsal"),
    ("Pottery class", 18, 0, 90, "Studio 9 on Alder Street", "pottery"),
    ("Food pantry volunteer shift", 9, 30, 150, "Eastside Pantry", "food pantry"),
    ("Midterm review session", 17, 30, 75, "Kerr Hall 120", "review session"),
    ("Meeting with the TA", 13, 30, 30, "Kerr Hall 305", "meeting with the TA"),
    ("Climbing session", 19, 0, 90, "Basalt Climbing Gym", "climbing"),
    ("Bike repair pickup", 12, 15, 30, "Wheelhouse Cycles", "bike repair"),
    ("Lunch with Mom", 12, 0, 60, "Cafe Verde", "lunch"),
    ("Philosophy seminar", 11, 0, 90, "Mercer Hall 18", "seminar"),
    ("Interview prep session", 16, 15, 60, "Career Center, room 4", "interview prep"),
    ("Chemistry recitation", 8, 45, 50, "Avery Science 140", "recitation"),
    ("Writing center appointment", 11, 15, 45, "Mercer Hall 6", "writing center"),
    ("Volleyball practice", 20, 0, 90, "Rec Center Court 2", "volleyball"),
    ("Group project meeting", 15, 15, 60, "Hollis Library, room 2A", "group project"),
    ("Grocery run with Carmen", 17, 45, 60, "Greenleaf Market", "grocery run"),
    ("Statistics exam", 9, 15, 120, "Avery Science 300", "stats exam"),
    ("Piano lesson", 16, 45, 45, "Maple Music Studio", "piano lesson"),
    ("Resume workshop", 12, 30, 60, "Career Center, room 2", "resume workshop"),
    ("Eye exam", 10, 45, 45, "Clearview Optical", "eye exam"),
    ("Board game night", 20, 15, 150, "Student Union room 5", "game night"),
    ("Car inspection", 8, 15, 60, "Valley Auto Care", "car inspection"),
    ("Language exchange", 18, 45, 60, "Cafe Verde", "language exchange"),
    ("Volunteer orientation", 13, 45, 90, "Eastside Pantry", "orientation"),
    ("Hackathon kickoff", 19, 30, 60, "Kerr Hall auditorium", "hackathon"),
    ("Lab safety training", 10, 15, 60, "Avery Science 105", "safety training"),
    ("Teaching assistant interview", 14, 45, 30, "Kerr Hall 210", "TA interview"),
]
ORD = {0: ["the first", "the 1st"], 1: ["the second", "the 2nd"], 2: ["the third", "the 3rd"], 3: ["the fourth", "the 4th"],
       -1: ["the last"]}
ORD_NOUN = ["one", "one", "thing", "item"]
MAIL_NOUN = ["one", "email", "message", "one"]
REM_NOUN = ["one", "reminder", "item", "one"]


def ordinal(rng, k, n):
    """A way to say item k of n, and the lead-in that makes a deeper question clear."""
    if k == n - 1 and n >= 2 and rng.random() < 0.35:
        k = -1
    return rng.choice(ORD[k]), k % n


def agenda_ex(rng, now, w, *, nonzero=True, n=None, off=None, with_loc=False, bullets=False, brief=0.0):
    """An earlier agenda exchange. A time-scoped ask ("what's lined up for friday") reads the Brief's day plan
    (get_my_day_plan), the way the live host routes it, so the reply is built from the PARSED plan: the events, and
    any Brief item (a text, note or email with a date) the plan also lists, which `brief` (a probability) adds to the
    world. A location-asking exchange (with_loc) stays a calendar read: the plan carries no places."""
    n = n or rng.choice([3, 3, 4, 2])
    off = off or rng.choice([1, 1, 2, 3, 4])
    day = now.date() + D(days=off)
    pool = [e for e in EV if e[2] != 0 or not nonzero]
    chosen, used = [], set()
    for e in rng.sample(pool, len(pool)):
        if (e[1], e[2]) in used or any(abs((e[1] * 60 + e[2]) - (c[1] * 60 + c[2])) < 50 for c in chosen):
            continue
        chosen.append(e)
        used.add((e[1], e[2]))
        if len(chosen) == n:
            break
    chosen.sort(key=lambda e: (e[1], e[2]))
    w.events = [Event(t, at_(day, h, m), mins, loc) for t, h, m, mins, loc, _ in chosen]
    period = "tomorrow" if off == 1 else wd_name(day).lower()
    pw = "tomorrow" if off == 1 else wd_name(day)
    Cp = "Tomorrow" if off == 1 else "On " + wd_name(day)
    Cn = "Tomorrow" if off == 1 else wd_name(day)
    on_pw = "tomorrow" if off == 1 else "on " + wd_name(day)
    items = [dict(title=t, noun=noun(t), c=clock(at_(day, h, m)), start=at_(day, h, m), loc=loc, short=sh)
             for t, h, m, mins, loc, sh in chosen]
    plan = not with_loc
    extra = []
    if plan:
        if brief:
            I.maybe_brief(rng, w, now, day, p=brief)
        result = w.handle("get_my_day_plan", {"day": period})
        parsed = I.parse_plan(str(result))
        cal = [x for x in parsed if x.src == "calendar"]
        assert [x.title for x in cal] == [i["title"] for i in items] and [x.t for x in cal] == [i["c"] for i in items], \
            f"the day plan does not list the world's events: {result!r}"
        extra = [x for x in parsed if x.src != "calendar"]
    if bullets:
        lines = "\n".join(f"- {i['c']}: {i['title']}" + (f" ({i['loc']})" if with_loc else "") for i in items)
        reply = f"{Cp} you have {len(items)} things:\n{lines}"
    else:
        lst = join([f"your {i['noun']} at {i['c']}" + (f" ({i['loc']})" if with_loc else "") for i in items])
        reply = rng.choice([f"{Cp} you have {lst}.", f"You have {lst} {on_pw}.", f"{Cn} looks like this: {lst}."])
    if extra:
        also = I.plan_prose(extra)
        reply += ("\n" if bullets else " ") + rng.choice([f"Also {on_pw}: {also}.", f"It also lists {also}."])
    user = rng.choice(["what does {p} look like", "what's lined up for {p}", "walk me through {p}", "how's {p} shaping up",
                       "what's on the books for {p}", "what's happening {p}", "give me the rundown for {p}"]).format(p=pw.lower())
    if not plan:
        result = w.handle("get_upcoming", {"period": period})
    tool, args = ("get_my_day_plan", {"day": period}) if plan else ("get_upcoming", {"period": period})
    ex = dict(turn(user, reply, tool, [{"name": tool, "args": args, "result": result}]),
              topic=f"your schedule {on_pw}", keys=["schedule", pw], cb=pw.lower(),
              say=join([f"{i['noun']} at {i['c']}" for i in items]), facts=[f"{i['noun']} at {i['c']}" for i in items] if not bullets else [i["c"] for i in items],
              kind="agenda", st=dict(items=items, period=period, pw=pw, Cp=Cp, on_pw=on_pw, day=day, bullets=bullets, extra=extra, plan=plan))
    return ex


MAILS = [
    dict(s="Pine Street Dental", a="frontdesk@pinestreetdental.example", subj="Cleaning reminder", qn="the dentist",
         body="Your cleaning is on Thursday at 2:30 PM with Dr. Alvarez. Please arrive ten minutes early and bring your insurance card.",
         brief="confirmed your cleaning for Thursday at 2:30 PM", keyb="Thursday at 2:30 PM",
         qb=["when's the dentist thing", "what day and time was the cleaning"],
         say="your cleaning is on Thursday at 2:30 PM with Dr. Alvarez, and they ask you to arrive ten minutes early with your insurance card",
         keys=["Thursday at 2:30 PM", "Dr. Alvarez", "insurance card"],
         q2=[("who am i seeing for the cleaning", "Dr. Alvarez"), ("what do i need to bring to the cleaning", "insurance card")]),
    dict(s="Cascade Air", a="trips@cascadeair.example", subj="Check-in is open", qn="the airline",
         body="Check-in is open for your flight to Denver. Your gate is B14 and boarding starts at 6:05 AM.",
         brief="says check-in is open for your flight to Denver", keyb="flight to Denver",
         qb=["where's the flight going again", "which flight was checking in"],
         say="check-in is open for your flight to Denver, your gate is B14 and boarding starts at 6:05 AM",
         keys=["B14", "6:05 AM"], q2=[("what gate am i at", "B14"), ("when does boarding start", "6:05 AM")]),
    dict(s="Hollis Library", a="circulation@linden.edu", subj="Two items due soon", qn="the library",
         body="Two books are due on Monday. The late fee is $0.25 per day for each item.",
         brief="reminds you that two books are due on Monday", keyb="two books are due on Monday",
         qb=["what was the library emailing about", "how many books did the library mention"],
         say="two books are due on Monday, and the late fee is $0.25 per day for each item",
         keys=["Monday", "$0.25 per day"], q2=[("what's the late fee", "$0.25 per day")]),
    dict(s="Lakeview Property Mgmt", a="office@lakeviewproperties.example", subj="Rent payment received", qn="the landlord",
         body="We received your November rent payment of $1,150.00. Thank you.",
         brief="confirmed your November rent payment", keyb="November rent",
         qb=["what was the landlord's email about"],
         say="they received your November rent payment of $1,150.00", keys=["$1,150.00"],
         q2=[("how much was the rent payment", "$1,150.00")]),
    dict(s="Soundwave Premium", a="billing@soundwave.example", subj="Your student plan renews soon", qn="the music app",
         body="Your student plan renews on the 14th for $5.99 a month. You can change plans in your account settings.",
         brief="says your student plan renews on the 14th", keyb="renews on the 14th",
         qb=["when does the music plan renew", "what was soundwave saying"],
         say="your student plan renews on the 14th for $5.99 a month", keys=["the 14th", "$5.99"],
         q2=[("how much does the music plan cost", "$5.99")]),
    dict(s="Northgate Labs", a="recruiting@northgatelabs.example", subj="Interview confirmation", qn="the recruiter",
         body="Your interview is on Wednesday at 2 PM on Zoom. Your interviewer is Elena Ruiz.",
         brief="confirmed your interview for Wednesday at 2 PM", keyb="Wednesday at 2 PM",
         qb=["when's the interview", "what time was my interview"],
         say="your interview is on Wednesday at 2 PM on Zoom with Elena Ruiz", keys=["Wednesday at 2 PM", "Zoom", "Elena Ruiz"],
         q2=[("who's interviewing me", "Elena Ruiz"), ("where is the interview happening", "Zoom")]),
    dict(s="Linden Registrar", a="registrar@linden.edu", subj="Spring registration opens", qn="the registrar",
         body="Registration opens on Monday at 8 AM. Your assigned time ticket is 8:40 AM.",
         brief="says spring registration opens on Monday at 8 AM", keyb="Monday at 8 AM",
         qb=["when does registration open", "what did the registrar say about spring"],
         say="registration opens on Monday at 8 AM, and your assigned time ticket is 8:40 AM", keys=["Monday at 8 AM", "8:40 AM"],
         q2=[("what's my time ticket for registration", "8:40 AM")]),
    dict(s="Hana Kowalczyk", a="hkowalczyk@linden.edu", subj="Homework 5 regrade", qn="the TA",
         body="I added 3 points to question 4, so your homework 5 score is now 47 out of 50.",
         brief="wrote back about your homework 5 regrade", keyb="homework 5",
         qb=["what was hana writing about"],
         say="3 points were added to question 4, which puts your homework 5 score at 47 out of 50", keys=["3 points", "47 out of 50"],
         q2=[("what's my new homework score", "47 out of 50")]),
    dict(s="Basalt Climbing Gym", a="hello@basaltclimbing.example", subj="Your day pass", qn="the climbing gym",
         body="Your day pass is valid through Sunday. Bring a photo ID and a pair of socks.",
         brief="sent your day pass, valid through Sunday", keyb="valid through Sunday",
         qb=["how long is the day pass good for", "until when is the climbing pass valid"],
         say="your day pass is valid through Sunday, and you should bring a photo ID and a pair of socks", keys=["Sunday", "photo ID"],
         q2=[("what do i need to bring to the gym", "photo ID")]),
    dict(s="Brightside Pharmacy", a="rx@brightsidepharmacy.example", subj="Refill ready", qn="the pharmacy",
         body="Your refill is ready for pickup until the 20th. Pickup hours are 9 AM to 7 PM.",
         brief="says your refill is ready until the 20th", keyb="until the 20th",
         qb=["how long is the refill held", "until when can i pick up the refill"],
         say="your refill is ready for pickup until the 20th, with pickup hours of 9 AM to 7 PM", keys=["the 20th", "9 AM to 7 PM"],
         q2=[("what are the pharmacy's pickup hours", "9 AM to 7 PM")]),
    dict(s="Campus Parking", a="parking@linden.edu", subj="Permit renewal", qn="parking",
         body="Your parking permit expires on the 30th. Renewal costs $84 and takes a few minutes online.",
         brief="warns that your parking permit expires on the 30th", keyb="expires on the 30th",
         qb=["when does the parking permit run out"],
         say="your parking permit expires on the 30th, and renewing it costs $84", keys=["the 30th", "$84"],
         q2=[("how much is it to renew the parking permit", "$84")]),
    dict(s="Orchard Bookshop", a="orders@orchardbooks.example", subj="Order shipped", qn="the bookshop",
         body="Your order shipped and should arrive by Friday. The tracking number is OB-33817.",
         brief="says your order shipped and should arrive by Friday", keyb="arrive by Friday",
         qb=["when is the book order showing up", "when should the bookshop order get here"],
         say="your order should arrive by Friday, and the tracking number is OB-33817", keys=["Friday", "OB-33817"],
         q2=[("what's the tracking number for the book order", "OB-33817")]),
    dict(s="Theo Park", a="tpark@example.com", subj="Climbing Saturday?", qn="theo",
         body="Climbing at 9 AM on Saturday at Basalt. I can drive and pick you up at 8:15.",
         brief="asks about climbing at 9 AM on Saturday", keyb="9 AM on Saturday",
         qb=["when does theo want to climb"],
         say="climbing is at 9 AM on Saturday at Basalt, with a pickup at 8:15", keys=["9 AM on Saturday", "8:15"],
         q2=[("when's theo picking me up", "8:15")]),
    dict(s="Linden Housing", a="housing@linden.edu", subj="Maintenance visit", qn="housing",
         body="A technician will visit on Wednesday between 1 PM and 3 PM to fix the heater.",
         brief="says a technician will visit on Wednesday", keyb="visit on Wednesday",
         qb=["which day is the heater getting fixed"],
         say="a technician will visit on Wednesday between 1 PM and 3 PM to fix the heater", keys=["Wednesday", "1 PM and 3 PM"],
         q2=[("what time window is the heater repair", "1 PM and 3 PM")]),
    dict(s="Garnet Credit Union", a="alerts@garnetcu.example", subj="Statement ready", qn="the bank",
         body="Your October statement is ready to view. The closing balance is $1,842.30.",
         brief="says your October statement is ready", keyb="October statement",
         qb=["which statement is ready"],
         say="your October statement is ready, with a closing balance of $1,842.30", keys=["October statement", "$1,842.30"],
         q2=[("what's the balance on the october statement", "$1,842.30")]),
    dict(s="Aisha Karim", a="akarim@linden.edu", subj="Study group room", qn="aisha",
         body="We booked room 3B at Hollis Library for 7 PM on Tuesday.",
         brief="booked a room for Tuesday's study group", keyb="Tuesday",
         qb=["what day is the study group room booked for"],
         say="the study group has room 3B at Hollis Library at 7 PM on Tuesday", keys=["room 3B", "7 PM on Tuesday"],
         q2=[("which room is the study group in", "room 3B"), ("what time is the study group", "7 PM on Tuesday")]),
    dict(s="Linden Dining", a="dining@linden.edu", subj="Meal plan balance", qn="dining",
         body="Your meal plan balance is $42.10. Add funds online before the 15th to avoid a hold.",
         brief="says your meal plan balance is running low", keyb="meal plan balance",
         qb=["what was dining emailing about", "what did the dining hall want"],
         say="your meal plan balance is $42.10, and funds should be added online before the 15th to avoid a hold", keys=["$42.10", "the 15th"],
         q2=[("how much is left on the meal plan", "$42.10")]),
    dict(s="Wheelhouse Cycles", a="service@wheelhousecycles.example", subj="Your bike is ready", qn="the bike shop",
         body="Your bike repair is done and ready for pickup. The total is $68.50 and the shop closes at 6 PM.",
         brief="says your bike is ready for pickup", keyb="ready for pickup",
         qb=["what did the bike shop say"],
         say="your bike repair is done, the total is $68.50 and the shop closes at 6 PM", keys=["$68.50", "6 PM"],
         q2=[("how much is the bike repair", "$68.50")]),
    dict(s="Carmen Rivera", a="carmen.rivera@example.com", subj="Thanksgiving plans", qn="carmen",
         body="Dinner is at 4 PM on Thursday at Grandma's. Could you bring rolls?",
         brief="sent the plan for Thursday's dinner", keyb="Thursday's dinner",
         qb=["what did carmen send about thanksgiving"],
         say="dinner is at 4 PM on Thursday at Grandma's, and you're asked to bring rolls", keys=["4 PM on Thursday", "rolls"],
         q2=[("what time is thanksgiving dinner", "4 PM on Thursday")]),
    dict(s="Linden Rec Center", a="recreation@linden.edu", subj="Court booking confirmed", qn="the rec center",
         body="Your court is booked for Friday at 6:30 PM on court 2. Cancellations need at least four hours' notice.",
         brief="confirmed your court booking for Friday at 6:30 PM", keyb="Friday at 6:30 PM",
         qb=["when's the court booked", "what did the rec center confirm"],
         say="your court is booked for Friday at 6:30 PM on court 2, and cancellations need at least four hours' notice", keys=["Friday at 6:30 PM", "court 2"],
         q2=[("which court did i get", "court 2")]),
    dict(s="Valley Auto Care", a="service@valleyautocare.example", subj="Inspection reminder", qn="the garage",
         body="Your car inspection is on Monday at 8:15 AM. It takes about an hour and costs $35.",
         brief="reminded you about your car inspection on Monday at 8:15 AM", keyb="Monday at 8:15 AM",
         qb=["when's the car inspection"],
         say="your inspection is on Monday at 8:15 AM, takes about an hour and costs $35", keys=["Monday at 8:15 AM", "$35"],
         q2=[("how much is the inspection", "$35")]),
    dict(s="Prof. Lindqvist", a="lindqvist@linden.edu", subj="Office hours moved", qn="the professor",
         body="Office hours move to Wednesday at 3 PM in Kerr Hall 318 this week.",
         brief="says office hours moved to Wednesday at 3 PM", keyb="Wednesday at 3 PM",
         qb=["when are office hours now"],
         say="office hours have moved to Wednesday at 3 PM in Kerr Hall 318 this week", keys=["Wednesday at 3 PM", "Kerr Hall 318"],
         q2=[("where are office hours this week", "Kerr Hall 318")]),
]

# (sender, asked key) -> (the answer as its own sentence, the rest of the body as a clause). The final leads with the answer.
Q2X = {
    ("Pine Street Dental", "Dr. Alvarez"): ("You're seeing Dr. Alvarez.", "your cleaning is on Thursday at 2:30 PM, and you should arrive ten minutes early with your insurance card"),
    ("Pine Street Dental", "insurance card"): ("Bring your insurance card.", "your cleaning with Dr. Alvarez is on Thursday at 2:30 PM, and you should arrive ten minutes early"),
    ("Cascade Air", "B14"): ("Your gate is B14.", "boarding starts at 6:05 AM, and check-in is open for your flight to Denver"),
    ("Cascade Air", "6:05 AM"): ("Boarding starts at 6:05 AM.", "your gate is B14, and check-in is open for your flight to Denver"),
    ("Hollis Library", "$0.25 per day"): ("The late fee is $0.25 per day for each item.", "two books are due on Monday"),
    ("Lakeview Property Mgmt", "$1,150.00"): ("Your November rent payment was $1,150.00.", "they received it and sent their thanks"),
    ("Soundwave Premium", "$5.99"): ("It costs $5.99 a month.", "your student plan renews on the 14th, and you can change plans in your account settings"),
    ("Northgate Labs", "Elena Ruiz"): ("Your interviewer is Elena Ruiz.", "the interview is on Wednesday at 2 PM on Zoom"),
    ("Northgate Labs", "Zoom"): ("It's on Zoom.", "your interview with Elena Ruiz is on Wednesday at 2 PM"),
    ("Linden Registrar", "8:40 AM"): ("Your time ticket is 8:40 AM.", "registration opens on Monday at 8 AM"),
    ("Hana Kowalczyk", "47 out of 50"): ("Your homework 5 score is now 47 out of 50.", "3 points were added to question 4"),
    ("Basalt Climbing Gym", "photo ID"): ("A photo ID and a pair of socks.", "your day pass is valid through Sunday"),
    ("Brightside Pharmacy", "9 AM to 7 PM"): ("Pickup hours are 9 AM to 7 PM.", "your refill is ready for pickup until the 20th"),
    ("Campus Parking", "$84"): ("Renewing costs $84.", "your permit expires on the 30th, and renewal takes a few minutes online"),
    ("Orchard Bookshop", "OB-33817"): ("The tracking number is OB-33817.", "your order shipped and should arrive by Friday"),
    ("Theo Park", "8:15"): ("Theo is picking you up at 8:15.", "climbing is at 9 AM on Saturday at Basalt"),
    ("Linden Housing", "1 PM and 3 PM"): ("The technician comes between 1 PM and 3 PM.", "the visit is on Wednesday, to fix the heater"),
    ("Garnet Credit Union", "$1,842.30"): ("The closing balance is $1,842.30.", "your October statement is ready to view"),
    ("Aisha Karim", "room 3B"): ("It's room 3B at Hollis Library.", "the group meets at 7 PM on Tuesday"),
    ("Aisha Karim", "7 PM on Tuesday"): ("It's at 7 PM on Tuesday.", "the group booked room 3B at Hollis Library"),
    ("Linden Dining", "$42.10"): ("You have $42.10 left.", "funds should be added online before the 15th to avoid a hold"),
    ("Wheelhouse Cycles", "$68.50"): ("The repair comes to $68.50.", "your bike is ready for pickup, and the shop closes at 6 PM"),
    ("Carmen Rivera", "4 PM on Thursday"): ("Dinner is at 4 PM on Thursday.", "it's at Grandma's, and you're asked to bring rolls"),
    ("Linden Rec Center", "court 2"): ("You have court 2.", "it's booked for Friday at 6:30 PM, and cancellations need at least four hours' notice"),
    ("Valley Auto Care", "$35"): ("The inspection costs $35.", "it's on Monday at 8:15 AM and takes about an hour"),
    ("Prof. Lindqvist", "Kerr Hall 318"): ("They're in Kerr Hall 318.", "office hours have moved to Wednesday at 3 PM"),
}
MAIL_Q = ["anything new in the inbox", "what's unread", "got any new mail", "who's emailed since this morning",
          "check what's come in", "any fresh emails", "what mail do i have waiting", "see if anything arrived in my inbox", "inbox check",
          "has anyone emailed me", "what's new in my email", "any unread emails?"]


def bodylines(body):
    """One sentence per line, the way short mails are laid out (and the way a result is read line by line)."""
    return re.sub(r"(?<!\bDr)(?<!\bProf)\.\s+(?=[A-Z])", ".\n", body)


def mail_ex(rng, now, w, *, n=3, brief=False, ids=True, digest_tool=False):
    chosen = rng.sample(MAILS, n)
    base = rng.randint(2, 6)
    emails = []
    for k, m in enumerate(chosen):
        emails.append(Email(m["s"], m["a"], m["subj"], now - D(hours=base + 3 * k, minutes=rng.randint(0, 40)), bodylines(m["body"]),
                            True, True, message_id=f"m-{rng.randint(2000, 8999)}" if ids else ""))
    w.emails = emails
    if brief:
        parts = [f"{m['s']} {m['brief']}" for m in chosen]
        reply = rng.choice([f"You have {n} unread. " + join(parts) + ".", f"{n} unread emails: " + join(parts) + "."])
    else:
        parts = [f"{m['s']} (“{m['subj']}”)" for m in chosen]
        reply = rng.choice([f"You have {n} unread: " + join(parts) + ".", f"{n} unread emails, from " + join(parts) + "."])
    if digest_tool:
        call = {"name": "summarize_emails", "args": {"unread": True}, "result": w.handle("summarize_emails", {"unread": True})}
    else:
        call = {"name": "view_emails", "args": {"unread": True}, "result": w.handle("view_emails", {"unread": True})}
    user = rng.choice(MAIL_Q)
    return dict(turn(user, reply, call["name"], [call]), topic="your unread mail", keys=["unread"], cb="the emails",
                say=join([f"{m['s']} {m['brief']}" if brief else f"{m['s']} (“{m['subj']}”)" for m in chosen]),
                facts=[m["keyb"] if brief else m["subj"] for m in chosen], kind="mail",
                st=dict(mails=chosen, emails=emails, brief=brief, digest=digest_tool))


REM = [  # title, query word
    ("Return the library books", "library"), ("Call the pharmacy about the refill", "pharmacy"),
    ("Submit the lab 4 report", "lab"), ("Pick up the dry cleaning", "cleaning"), ("Renew the bus pass", "bus"),
    ("Email the landlord about the sink", "landlord"), ("Buy a birthday card for Carmen", "birthday"),
    ("Water the plants", "plants"), ("Sign the lease addendum", "lease"), ("Print the boarding pass", "boarding"),
    ("Bring the tripod to class", "tripod"), ("Pay the parking ticket", "parking"), ("Order the textbook", "textbook"),
    ("Defrost the chicken", "chicken"), ("Take out the recycling", "recycling"), ("Book the airport shuttle", "shuttle"),
    ("Return the rented projector", "projector"), ("Email Prof. Okafor the draft", "draft"), ("Refill the water filter", "filter"),
    ("Drop off the package at the post office", "package"), ("Charge the camera battery", "camera"),
    ("Confirm the dinner reservation", "reservation"), ("Fill out the housing form", "housing"), ("Wash the gym clothes", "clothes"),
]
REM_Q = ["list the reminders i have coming up", "list my upcoming reminders", "which reminders are due soon",
         "what's on my reminder list", "any reminders i should know about", "what reminders are set", "show me what's on my reminders",
         "what have i got to remember this week"]


def rain_phrase(r):
    if not r:
        return "no rain expected"
    return f"{'an' if str(r)[0] == '8' or r in (11, 18) else 'a'} {r}% chance of rain"


def when_phrase(t, now):
    return f"{day_word(t.date(), now)} at {clock(t)}"


def rem_ex(rng, now, w, *, n=3, times=True, nonzero=True):
    chosen = rng.sample(REM, n)
    spots = set()
    items = []
    for title, q in chosen:
        while True:
            off = rng.choice([0, 1, 1, 2, 3, 4])
            h = rng.choice([10, 11, 13, 15, 17, 18, 20]) if off else rng.choice([13, 15, 17, 19])
            m = rng.choice([15, 30, 45]) if nonzero else rng.choice([0, 15, 30, 45])
            due = at_(now.date() + D(days=off), h, m)
            if due > now + D(minutes=90) and (due.date(), h) not in spots:
                spots.add((due.date(), h))
                break
        items.append(dict(title=title, q=q, due=due, when=when_phrase(due, now), c=clock(due)))
    items.sort(key=lambda i: i["due"])
    w.reminders = [Reminder(i["title"], i["due"]) for i in items]
    if times:
        lst = join([f"{lowfirst(i['title'])} {i['when']}" for i in items])
        reply = rng.choice([f"You have {n} coming up: {lst}.", f"There are {n} reminders ahead: {lst}."])
    else:
        lst = join([lowfirst(i["title"]) for i in items])
        reply = rng.choice([f"You have {n} coming up: {lst}.", f"There are {n} reminders ahead: {lst}."])
    result = w.handle("search_reminders", {"scope": "upcoming"})
    return dict(turn(rng.choice(REM_Q), reply, "search_reminders", [{"name": "search_reminders", "args": {"scope": "upcoming"}, "result": result}]),
                topic="your upcoming reminders", keys=["reminders"], cb="the reminders",
                say=join([f"{lowfirst(i['title'])} {i['when']}" for i in items]) if times else join([lowfirst(i["title"]) for i in items]),
                facts=[i["when"] for i in items] if times else [lowfirst(i["title"]) for i in items], kind="rem",
                st=dict(items=items, times=times))


MSG = {
    "Maya": ("Maya Chen", [
        ("are we still on for pizza at 7 tonight", "asked if you're still on for pizza at 7 tonight", "7 tonight", "pizza tonight"),
        ("landlord is coming by at noon tomorrow, can you be home", "said the landlord is coming by at noon tomorrow and asked if you can be home", "noon tomorrow", "the landlord's visit"),
        ("grabbed your package from the lobby, it's on the shelf by the mailboxes", "picked up your package and left it on the shelf by the mailboxes", "shelf by the mailboxes", "your package")]),
    "Theo": ("Theo Park", [
        ("climbing at 6 tomorrow? i can drive", "asked about climbing at 6 tomorrow and offered to drive", "6 tomorrow", "climbing"),
        ("bringing the extra rope on saturday", "is bringing the extra rope on Saturday", "extra rope", "the extra rope"),
        ("stuck on number 4 of the problem set, did you get it", "is stuck on number 4 of the problem set", "number 4", "the problem set")]),
    "Carmen": ("Carmen Rivera", [
        ("call me when you're free, it's about thanksgiving", "wants a call when you're free about Thanksgiving", "thanksgiving", "Thanksgiving"),
        ("sent you the photos from the lake, check your email", "sent the photos from the lake to your email", "photos from the lake", "the lake photos")]),
    "Aisha": ("Aisha Karim", [
        ("study group moved to room 3B at 7", "moved the study group to room 3B at 7", "room 3B at 7", "the study group"),
        ("can you bring your linear algebra notes tonight", "asked you to bring your linear algebra notes tonight", "linear algebra notes", "your notes")]),
    "Jordan": ("Jordan Ellis", [
        ("demo rehearsal is at 6:30 in Kerr 301", "said demo rehearsal is at 6:30 in Kerr 301", "6:30 in Kerr 301", "the demo"),
        ("i'll bring the projector cable", "is bringing the projector cable", "projector cable", "the projector cable")]),
    "Priya": ("Priya Nair", [
        ("lab report draft is in the shared folder, check section 3", "put the lab report draft in the shared folder and asked you to check section 3", "section 3", "the lab report"),
        ("do you still have the extra ticket for friday's show", "asked if you still have the extra ticket for Friday's show", "Friday's show", "the extra ticket")]),
    "Marcus": ("Marcus Webb", [
        ("running 10 minutes late to the meetup, start without me", "is running 10 minutes late to the meetup", "10 minutes late", "the meetup"),
        ("the bike pump is back on your porch, thanks again", "returned the bike pump to your porch and said thanks", "bike pump", "the bike pump")]),
}
MSG_Q = ["any texts", "did anyone message me", "what's new in messages", "has anyone texted today", "check for new texts"]


def msg_ex(rng, now, w, *, detail=False):
    a, b = rng.sample(list(MSG), 2)
    picks = []
    w.messages = []
    for k, who in enumerate((a, b)):
        full, bank = MSG[who]
        text, gist, key, topic = rng.choice(bank)
        w.messages.append(Msg(full, who, text, now - D(hours=rng.randint(1, 5) + 6 * k, minutes=rng.randint(1, 50))))
        picks.append(dict(who=who, full=full, text=text, gist=gist, key=key, topic=topic))
    if detail:
        reply = rng.choice([f"{a} {picks[0]['gist']}, and {b} {picks[1]['gist']}."])
    else:
        reply = rng.choice([f"{a} wrote about {picks[0]['topic']}, and {b} about {picks[1]['topic']}.",
                            f"Two people texted: {a} about {picks[0]['topic']} and {b} about {picks[1]['topic']}."])
    result = w.handle("summarize_messages", {})
    return dict(turn(rng.choice(MSG_Q), reply, "summarize_messages", [{"name": "summarize_messages", "args": {}, "result": result}]),
                topic="your recent texts", keys=["texts"], cb="the texts",
                say=f"{a} {picks[0]['gist']}, and {b} {picks[1]['gist']}" if detail else f"{a} wrote about {picks[0]['topic']} and {b} about {picks[1]['topic']}",
                facts=[p["key"] for p in picks] if detail else [p["topic"] for p in picks], kind="msg",
                st=dict(picks=picks, detail=detail))


CITY = [("Seattle,WA", "Seattle, Washington, United States", "Seattle"), ("Boise,ID", "Boise, Idaho, United States", "Boise"),
        ("Denver,CO", "Denver, Colorado, United States", "Denver"), ("Eugene,OR", "Eugene, Oregon, United States", "Eugene"),
        ("Austin,TX", "Austin, Texas, United States", "Austin"), ("Chicago,IL", "Chicago, Illinois, United States", "Chicago"),
        ("Spokane,WA", "Spokane, Washington, United States", "Spokane"), ("Salt Lake City,UT", "Salt Lake City, Utah, United States", "Salt Lake City")]
WX_Q = ["what's the weather like in {c}", "how's {c} looking weather-wise", "weather for {c}?", "is it nice in {c}",
        "forecast for {c} please"]


def wx_ex(rng, now, w, *, full=True, idx=None):
    loc, label, city = rng.choice(CITY)
    wx = make_weather(now, rng, loc, label)
    w.weather[loc] = wx
    idx = rng.choice([0, 1]) if idx is None else idx
    d = wx["days"][idx]
    pw = "today" if idx == 0 else "tomorrow"
    desc = d[3].lower()
    st = dict(loc=loc, city=city, idx=idx, pw=pw, hi=d[1], lo=d[2], desc=desc, rain=d[4])
    if full:
        rain = rain_phrase(d[4])
        reply = rng.choice([f"{cap1(pw)} in {city} it's {desc}, with a high of {d[1]}°, a low of {d[2]}° and {rain}.",
                            f"{cap1(pw)} in {city}: {desc}, high {d[1]}°, low {d[2]}°, and {rain}."])
        say = f"{desc}, high {d[1]}°, low {d[2]}°"
        facts = [f"{d[1]}°", f"{d[2]}°"]
    else:
        reply = rng.choice([f"{cap1(pw)} in {city} it's {desc} with a high of {d[1]}°.", f"{cap1(pw)} in {city}: {desc}, high {d[1]}°."])
        say = f"{desc} with a high of {d[1]}°"
        facts = [f"{d[1]}°", desc]
    result = w.handle("get_weather", {"location": loc})
    return dict(turn(rng.choice(WX_Q).format(c=city), reply, "get_weather", [{"name": "get_weather", "args": {"location": loc}, "result": result}]),
                topic=f"the weather in {city}", keys=["weather", city], cb="the weather", say=say, facts=facts, kind="wx", st=st)


PRICE = [  # user, reply, [(name, price, spec phrase)], topic, keys
    ("compare the Cascade Air and Harbor Jet fares to Denver", "Cascade Air is $212 with one stop, and Harbor Jet is $189 nonstop.",
     [("Cascade Air", 212, "with one stop"), ("Harbor Jet", 189, "nonstop")], "comparing the Cascade Air and Harbor Jet fares", ["Cascade", "Harbor"]),
    ("which is the better deal, the Aerobook or the Lumen 14", "The Aerobook is $849 with 16 GB of memory, and the Lumen 14 is $799 with 8 GB.",
     [("Aerobook", 849, "with 16 GB of memory"), ("Lumen 14", 799, "with 8 GB")], "comparing the Aerobook and the Lumen 14", ["Aerobook", "Lumen"]),
    ("what do the Riverside Gym and Peak Fitness cost a month", "Riverside Gym is $39 a month with a $50 sign-up fee, and Peak Fitness is $45 a month with no sign-up fee.",
     [("Riverside Gym", 39, "a month with a $50 sign-up fee"), ("Peak Fitness", 45, "a month with no sign-up fee")], "what Riverside Gym and Peak Fitness cost a month", ["Riverside", "Peak"]),
    ("compare the Orbit and Nimbus phone plans", "Orbit is $25 a month with 10 GB of data, and Nimbus is $32 a month with unlimited data.",
     [("Orbit", 25, "a month with 10 GB of data"), ("Nimbus", 32, "a month with unlimited data")], "comparing the Orbit and Nimbus phone plans", ["Orbit", "Nimbus"]),
    ("is it cheaper to rent or buy the statistics textbook", "Renting is $48 for the semester, and buying new is $132.",
     [("Renting", 48, "for the semester"), ("Buying new", 132, "")], "whether to rent or buy the statistics textbook", ["renting", "buying"]),
    ("how do the Willow and Birch storage units compare", "Willow is $64 a month for a five by five unit, and Birch is $58 a month for a four by six.",
     [("Willow", 64, "a month for a five by five unit"), ("Birch", 58, "a month for a four by six")], "comparing the Willow and Birch storage units", ["Willow", "Birch"]),
    ("headphones: Sonar Pro or the Vale Lite, price check", "The Sonar Pro is $179 with noise cancelling, and the Vale Lite is $99 without it.",
     [("Sonar Pro", 179, "with noise cancelling"), ("Vale Lite", 99, "without it")], "the Sonar Pro and the Vale Lite prices", ["Sonar", "Vale"]),
]


def price_ex(rng, now, w):
    user, reply, items, topic, pkeys = rng.choice(PRICE)
    (an, ap, asp), (bn, bp, bsp) = items
    cheaper, dearer = (items[0], items[1]) if ap < bp else (items[1], items[0])
    return dict(turn(user, reply, "web_search"), topic=topic, keys=pkeys, cb="that comparison",
                say=f"{an} at ${ap} and {bn} at ${bp}", facts=[f"${ap}", f"${bp}"], kind="price",
                st=dict(items=items, cheaper=cheaper, dearer=dearer))


def calc_ex(rng, now, w):
    k = rng.choice(["split", "tip", "wage", "scale"])
    q = lambda x: Decimal(x).quantize(Decimal("0.01"), ROUND_HALF_UP)
    if k == "split":
        n = rng.choice([3, 4, 5, 6])
        each = rng.choice([Decimal("23.50"), Decimal("31.25"), Decimal("47.50"), Decimal("18.75"), Decimal("26.40")])
        total = each * n
        num = {3: "three", 4: "four", 5: "five", 6: "six"}[n]
        user = rng.choice([f"split a ${total} dinner bill {num} ways", f"we owe ${total}, {num} of us are splitting it evenly"])
        reply = f"Each of you pays ${each}."
        return dict(turn(user, reply, "calculate"), topic=f"splitting a ${total} bill", keys=[f"${total}"], cb="the bill",
                    say=f"each of you pays ${each}", facts=[f"${each}"], kind="calc",
                    st=dict(ask=["how much was each share again", "what was each share of the bill again"], ans=f"Each share is ${each}, from the ${total} bill split {num} ways.", must=[f"${each}"]))
    if k == "tip":
        bill = rng.choice([Decimal("64.00"), Decimal("86.50"), Decimal("42.80"), Decimal("120.00"), Decimal("58.40")])
        pct = rng.choice([15, 18, 20])
        tip = q(bill * pct / 100)
        total = q(bill + tip)
        user = rng.choice([f"{pct}% tip on ${bill}?", f"what's a {pct} percent tip on ${bill}"])
        reply = f"A {pct}% tip on ${bill} is ${tip}, which makes ${total} in all."
        return dict(turn(user, reply, "calculate"), topic=f"a {pct}% tip on ${bill}", keys=[f"{pct}%"], cb="the tip",
                    say=f"a {pct}% tip on ${bill} is ${tip}, which makes ${total} in all", facts=[f"${tip}", f"${total}"], kind="calc",
                    st=dict(ask=["what was the total with the tip again", "how much did that come to with tip included"],
                            ans=f"With the {pct}% tip, the total comes to ${total}.", must=[f"${total}"]))
    if k == "wage":
        rate = rng.choice([Decimal("18.50"), Decimal("16.75"), Decimal("21.00"), Decimal("19.25")])
        hours = rng.choice([22, 26, 14, 30])
        pay = q(rate * hours)
        user = rng.choice([f"i make ${rate} an hour, what's {hours} hours come to", f"${rate}/hr for {hours} hours, how much"])
        reply = f"{hours} hours at ${rate} comes to ${pay}."
        return dict(turn(user, reply, "calculate"), topic=f"pay for {hours} hours", keys=[f"{hours} hours"], cb="the pay",
                    say=f"{hours} hours at ${rate} comes to ${pay}", facts=[f"${pay}"], kind="calc",
                    st=dict(ask=["how much was that again", "remind me what the pay came to"], ans=f"It came to ${pay} for the {hours} hours.", must=[f"${pay}"]))
    cups, mult = rng.choice([("2/3 cup", 3), ("3/4 cup", 2), ("1/2 cup", 5), ("1/3 cup", 4)]), None
    base_txt, mult = cups
    frac = {"2/3 cup": Decimal(2) / 3, "3/4 cup": Decimal(3) / 4, "1/2 cup": Decimal(1) / 2, "1/3 cup": Decimal(1) / 3}[base_txt]
    word = {3: "triple", 2: "double", 5: "multiply by five", 4: "quadruple"}[mult]
    out = {("2/3 cup", 3): "2 cups", ("3/4 cup", 2): "1 1/2 cups", ("1/2 cup", 5): "2 1/2 cups", ("1/3 cup", 4): "1 1/3 cups"}[(base_txt, mult)]
    user = f"{word} {base_txt} of flour for me"
    reply = f"{cap1(word)}d, {base_txt} of flour becomes {out}." if mult in (2,) else f"{base_txt} of flour times {mult} is {out}."
    return dict(turn(user, reply, "calculate"), topic=f"scaling {base_txt} of flour", keys=["flour"], cb="the flour",
                say=f"{base_txt} of flour times {mult} is {out}", facts=[out], kind="calc",
                st=dict(ask=["how much flour did that end up being", "what was the flour amount again"], ans=f"The flour comes to {out}.", must=[out]))


NOTES = [
    ("denver trip", "trip", [("Denver trip: packing list", "Passport, phone charger, rain jacket, two pairs of socks and a camera.", "Travel", "packing"),
                              ("Denver trip: flight details", "Cascade Air 218 to Denver, departs Friday at 6:05 AM, confirmation K7QP2.", "Travel", "flight")],
     ["Friday at 6:05 AM", "K7QP2"], ["rain jacket", "camera"]),
    ("the apartment", "apartment", [("Apartment: move-in checklist", "Photograph every wall, test the smoke detector and ask about the parking permit.", "Home", "checklist"),
                                    ("Apartment: utilities", "Electric account opens on the 1st, and the internet install is Tuesday between 9 AM and noon.", "Home", "utilities")],
     ["Tuesday between 9 AM and noon", "the 1st"], ["smoke detector", "parking permit"]),
    ("the thesis", "thesis", [("Thesis: outline", "Motivation, related work, method and results. The draft of chapter 2 is due in two weeks.", "School", "outline"),
                              ("Thesis: reading list", "Kahneman's Thinking, Fast and Slow, Gigerenzer's Risk Savvy and the 1974 Tversky papers.", "School", "reading")],
     ["chapter 2", "two weeks"], ["Gigerenzer", "1974"]),
]


def notes_ex(rng, now, w):
    topic, q, notes, k2, k1 = rng.choice(NOTES)
    order = list(range(2))
    rng.shuffle(order)
    w.notes = [Note(t, b, now - D(days=2 + i), f) for i, (t, b, f, _) in enumerate(notes)]
    ns = [notes[i] for i in order]
    reply = rng.choice([f"I found two notes about {topic}: “{ns[0][0]}” and “{ns[1][0]}”.", f"There are two notes on {topic}, “{ns[0][0]}” and “{ns[1][0]}”."])
    result = w.handle("search_notes", {"query": q})
    return dict(turn(rng.choice([f"do i have notes about {topic}", f"any notes on {topic}"]), reply, "search_notes",
                     [{"name": "search_notes", "args": {"query": q}, "result": result}]),
                topic=f"your notes on {topic}", keys=["notes", q], cb=f"the notes on {topic}", say=f"two notes, “{ns[0][0]}” and “{ns[1][0]}”",
                facts=[ns[0][0], ns[1][0]], kind="notes", st=dict(notes=ns, topic=topic, q=q))


# =============================================================================
# shared bits for the generators
# =============================================================================
def base(rng, hour=None):
    return blank(rng, hour=hour if hour is not None else rng.choice([7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]))


def lead_in(rng, deep, word):
    """When the earlier list is some turns back, the person names it."""
    if not deep or rng.random() < 0.45:
        return ""
    return rng.choice([f"back to {word}: ", f"wait, on {word}, ", f"ok about {word}, ", f"sorry, {word}: ", f"one more on {word}, ",
                       f"going back to {word}, ", f"{word} from earlier, "])


def agenda_world(rng, *, nonzero=True, n=None, with_loc=False, bullets=False, off=None, hour=None, brief=0.0):
    now, w = base(rng, hour)
    ex = agenda_ex(rng, now, w, nonzero=nonzero, n=n, with_loc=with_loc, bullets=bullets, off=off, brief=brief)
    return now, w, ex


# =============================================================================
# SUB-SKILL 1: answer from the conversation (no call)
# =============================================================================
def s1_agenda_time(i):
    def make(rng):
        now, w, ex = agenda_world(rng, nonzero=i % 3 != 0)
        st = ex["st"]
        it = rng.choice(st["items"])
        hist, ai = session(rng, [ex])
        prompt = rng.choice(["what time was the {s} again", "when did you say the {s} was", "when does the {s} start",
                             "sorry, what time is the {s}", "and the {s} was at what time", "what time did you give for the {s}",
                             "{s} was when, sorry", "what time was it for the {s}"]).format(s=it["short"])
        final = fin(rng, f"Your {it['noun']} is {st['on_pw']} at {it['c']}.", f"The {it['noun']} starts at {it['c']} {st['on_pw']}.",
                    f"It's at {it['c']} {st['on_pw']}: your {it['noun']}.")
        return sc_(rng, prompt, now, w, [Final(final)], "the earlier reply stated the time: answer from the conversation, no call",
                   hist, "s1_time", no_call=True, must=[it["c"], it["noun"]], reply_must=[it["c"]], anchor=ai)
    return make


def s1_agenda_order(i):
    def make(rng):
        now, w, ex = agenda_world(rng, n=rng.choice([3, 4]))
        st = ex["st"]
        its = st["items"]
        hist, ai = session(rng, [ex])
        kind = i % 3
        if kind == 0:
            a, b = rng.sample(range(len(its)), 2)
            a, b = min(a, b), max(a, b)
            x, y = its[a], its[b]
            prompt = rng.choice(["which comes first, the {x} or the {y}", "is the {y} before or after the {x}"]).format(x=x["short"], y=y["short"])
            final = fin(rng, f"Your {x['noun']} comes first, at {x['c']}, and your {y['noun']} is later, at {y['c']}.",
                        f"The {x['noun']} is earlier, at {x['c']}. The {y['noun']} follows at {y['c']}.")
            must = [x["c"], y["c"], x["noun"], y["noun"]]
        elif kind == 1:
            x = its[-1]
            prompt = rng.choice(["what's the last thing {p}", "what's the final thing on {p}", "what ends the day {p}"]).format(p=st["pw"].lower())
            final = fin(rng, f"The last thing {st['on_pw']} is your {x['noun']} at {x['c']}.", f"Your day finishes with the {x['noun']} at {x['c']}.")
            must = [x["c"], x["noun"]]
        else:
            x = its[0]
            prompt = rng.choice(["what's the first thing {p}", "what do i start with {p}", "what's my earliest one {p}"]).format(p=st["pw"].lower())
            final = fin(rng, f"The first thing {st['on_pw']} is your {x['noun']} at {x['c']}.", f"You start with the {x['noun']} at {x['c']}.")
            must = [x["c"], x["noun"]]
        return sc_(rng, prompt, now, w, [Final(final)], "order questions over the listed times: answered from the earlier reply", hist,
                   "s1_order", no_call=True, must=must, reply_must=must[:2] if kind == 0 else [must[0]], anchor=ai)
    return make


def s1_agenda_loc(i):
    def make(rng):
        now, w, ex = agenda_world(rng, with_loc=True, n=rng.choice([2, 3]))
        st = ex["st"]
        it = rng.choice(st["items"])
        hist, ai = session(rng, [ex])
        prompt = rng.choice(["where's the {s}", "where was the {s} again", "what's the location for the {s}", "which room is the {s} in, did you say"]).format(s=it["short"])
        final = fin(rng, f"Your {it['noun']} is at {it['loc']}, and it starts at {it['c']}.", f"The {it['noun']} is at {it['loc']} ({it['c']} {st['on_pw']}).")
        return sc_(rng, prompt, now, w, [Final(final)], "the earlier reply stated the place: answer from the conversation", hist, "s1_loc",
                   no_call=True, must=[it["loc"], it["c"]], reply_must=[it["loc"], it["c"]], anchor=ai)
    return make


def s1_agenda_ordinal(i):
    def make(rng):
        now, w, ex = agenda_world(rng, n=rng.choice([3, 4]), bullets=i % 2 == 1)
        st = ex["st"]
        its = st["items"]
        hist, ai = session(rng, [ex])
        k = [1, 2, 0, 1][i % 4] if len(its) > [1, 2, 0, 1][i % 4] else 1
        o, idx = ordinal(rng, k, len(its))
        it = its[idx]
        prompt = rng.choice(["{o} {n} you listed, when is it", "{o} {n} on that list, what time", "and {o} {n}, when does it start",
                             "wait, when's {o} {n}", "{o} {n}, what time", "what time is {o} {n} again", "ok and {o} {n}? when",
                             "when does {o} {n} start", "what about {o} {n}, time-wise", "hm, {o} {n}?? when is that", "{o} {n} is at what time"]).format(o=o, n=rng.choice(ORD_NOUN))
        final = fin(rng, f"That's your {it['noun']} {st['on_pw']} at {it['c']}.", f"Your {it['noun']} is {st['on_pw']} at {it['c']}.")
        return sc_(rng, prompt, now, w, [Final(final)], "an ordinal over the earlier reply's order: the item as listed, no call", hist,
                   "s1_ordinal", no_call=True, must=[it["c"], it["noun"]], reply_must=[it["c"], it["title"]], anchor=ai)
    return make


def s1_agenda_write(i):
    def make(rng):
        now, w, ex = agenda_world(rng, n=rng.choice([3, 4]))
        st = ex["st"]
        its = st["items"]
        hist, ai = session(rng, [ex])
        mins, phrase = rng.choice([(60, "an hour before"), (30, "half an hour before"), (15, "15 minutes before"), (45, "45 minutes before"), (120, "2 hours before")])
        if i % 2 == 0:
            it = rng.choice(its)
            ref = f"the {it['short']}"
        else:
            o, idx = ordinal(rng, rng.choice([1, 2, 0]) % len(its), len(its))
            it = its[idx]
            ref = f"{o} {rng.choice(ORD_NOUN)}"
        when = it["start"] - D(minutes=mins)
        prompt = rng.choice(["remind me {p} {r}", "set a reminder {p} {r}", "can you remind me {p} {r}"]).format(p=phrase, r=ref)
        final = fin(rng, f"Okay, I'll remind you at {clock(when)} {st['on_pw']}, ahead of your {it['noun']}.",
                    f"Done. You'll get a reminder at {clock(when)} {st['on_pw']} for your {it['noun']}.")
        return sc_(rng, prompt, now, w, [[Call("add_reminder", title=it["title"], when_iso=iso(when))], Final(final)],
                   "the referent comes from the earlier reply (the item by name or ordinal) and its start time from the same reply; reminders never ask",
                   hist, "s1_write", first="add_reminder", args={"title": it["title"], "when_iso": iso(when)}, must=[clock(when)],
                   reply_must=[it["c"]], anchor=ai, min_final=1)
    return make


def s1_mail(i):
    def make(rng):
        now, w = base(rng)
        ex = mail_ex(rng, now, w, n=3, brief=True, ids=i % 2 == 0, digest_tool=False)
        st = ex["st"]
        m = rng.choice(st["mails"])
        hist, ai = session(rng, [ex])
        prompt = rng.choice(m["qb"])
        final = fin(rng, f"{m['s']} {m['brief']}.", f"The email from {m['s']} {m['brief']}.", f"Going by what I listed, {m['s']} {m['brief']}.")
        return sc_(rng, prompt, now, w, [Final(final)], "the earlier digest stated the detail: answer from the conversation, no call", hist,
                   "s1_mail", no_call=True, must=[m["s"], m["keyb"]], reply_must=[m["s"], m["keyb"]], anchor=ai)
    return make


def s1_rem(i):
    def make(rng):
        now, w = base(rng)
        ex = rem_ex(rng, now, w, n=rng.choice([3, 4]), times=True, nonzero=False)
        st = ex["st"]
        its = st["items"]
        hist, ai = session(rng, [ex])
        if i % 2 == 0:
            it = rng.choice(its)
            prompt = rng.choice(["what time was the {q} one set for", "what time did the {q} reminder say", "when was the {q} reminder going off"]).format(q=it["q"])
        else:
            o, idx = ordinal(rng, rng.choice([1, 2, 0]) % len(its), len(its))
            it = its[idx]
            prompt = rng.choice(["{o} {n} on the reminder list, when's it set for", "what time is {o} {n} you listed set for",
                                 "and {o} {n}, when does that go off", "{o} {n}, what time was that"]).format(o=o, n=rng.choice(REM_NOUN))
        final = fin(rng, f"Your reminder to {lowfirst(it['title'])} is due {it['when']}.", f"It's due {it['when']}, to {lowfirst(it['title'])}.",
                    f"The reminder to {lowfirst(it['title'])} goes off {it['when']}.")
        return sc_(rng, prompt, now, w, [Final(final)], "a reminder time the earlier reply listed: answer from the conversation", hist, "s1_rem",
                   no_call=True, must=[it["c"], lowfirst(it["title"])], reply_must=[it["when"]], anchor=ai)
    return make


def s1_msg(i):
    def make(rng):
        now, w = base(rng)
        ex = msg_ex(rng, now, w, detail=True)
        st = ex["st"]
        p = rng.choice(st["picks"])
        hist, ai = session(rng, [ex])
        if i % 2 == 0:
            prompt = rng.choice(["what was it {n} wanted again", "what did {n} say, i forgot", "{n} said what, sorry"]).format(n=p["who"].lower())
            final = fin(rng, f"{p['who']} {p['gist']}.", f"That was {p['who']}, who {p['gist']}.")
        else:
            prompt = rng.choice(["who texted about {t}", "which of them mentioned {t}"]).format(t=p["topic"])
            final = fin(rng, f"That was {p['who']}, who {p['gist']}.", f"It was {p['who']}, who {p['gist']}.")
        return sc_(rng, prompt, now, w, [Final(final)], "who said what, stated in the earlier reply: answer from the conversation", hist,
                   "s1_msg", no_call=True, must=[p["who"], p["key"]], reply_must=[p["who"], p["key"]], anchor=ai)
    return make


def s1_wx(i):
    def make(rng):
        now, w = base(rng)
        ex = wx_ex(rng, now, w, full=True)
        st = ex["st"]
        hist, ai = session(rng, [ex])
        if i % 3 == 0:
            prompt = rng.choice(["what was the high again", "and the high there was what", "how warm did you say it gets"])
            final = fin(rng, f"The high in {st['city']} {st['pw']} is {st['hi']}°, with a low of {st['lo']}°.", f"It tops out at {st['hi']}° in {st['city']} {st['pw']}.")
            must = [f"{st['hi']}°"]
        elif i % 3 == 1:
            prompt = rng.choice(["and how cold was it going to get overnight", "what was the low again"])
            final = fin(rng, f"The low in {st['city']} {st['pw']} is {st['lo']}°, after a high of {st['hi']}°.", f"It drops to {st['lo']}° in {st['city']}.")
            must = [f"{st['lo']}°"]
        else:
            prompt = rng.choice(["so what's {c} like then, remind me", "{c} was what kind of weather again"]).format(c=st["city"])
            final = fin(rng, f"{cap1(st['pw'])} in {st['city']} it's {st['desc']}, with a high of {st['hi']}° and a low of {st['lo']}°.",
                        f"It's {st['desc']} in {st['city']} {st['pw']}: high {st['hi']}°, low {st['lo']}°.")
            must = [st["desc"], f"{st['hi']}°"]
        return sc_(rng, prompt, now, w, [Final(final)], "a weather figure the earlier reply stated: answer from the conversation", hist,
                   "s1_wx", no_call=True, must=must, reply_must=must, anchor=ai)
    return make


def s1_price(i):
    def make(rng):
        now, w = base(rng)
        ex = price_ex(rng, now, w)
        st = ex["st"]
        (an, ap, asp), (bn, bp, bsp) = st["items"]
        hist, ai = session(rng, [ex])
        k = i % 4
        ch, de = st["cheaper"], st["dearer"]
        if k == 0:
            prompt = rng.choice(["which one was cheaper", "which was the cheaper one again", "remind me which is less"])
            final = fin(rng, f"{ch[0]} was cheaper, at ${ch[1]}, against ${de[1]} for {de[0]}.", f"The cheaper one was {ch[0]} at ${ch[1]}, while {de[0]} came to ${de[1]}.")
            must = [ch[0], f"${ch[1]}", f"${de[1]}"]
        elif k == 1:
            prompt = rng.choice(["and the price of the second one?", "what was the second one's price"])
            final = fin(rng, f"{bn} was ${bp}" + (f" {bsp}." if bsp else "."), f"The second was {bn}, at ${bp}" + (f" {bsp}." if bsp else "."))
            must = [bn, f"${bp}"]
        elif k == 2:
            prompt = rng.choice(["how much was the first option again", "what did the first one cost"])
            final = fin(rng, f"{an} was ${ap}" + (f" {asp}." if asp else "."), f"The first was {an}, at ${ap}" + (f" {asp}." if asp else "."))
            must = [an, f"${ap}"]
        else:
            prompt = rng.choice(["which was the more expensive one", "and the pricier one was which"])
            final = fin(rng, f"{de[0]} was the pricier one, at ${de[1]}, compared with ${ch[1]} for {ch[0]}.", f"That was {de[0]} at ${de[1]}, against {ch[0]} at ${ch[1]}.")
            must = [de[0], f"${de[1]}", f"${ch[1]}"]
        return sc_(rng, prompt, now, w, [Final(final)], "a price comparison stated earlier: pick the right item from the conversation", hist,
                   "s1_price", no_call=True, must=must, reply_must=[m for m in must if m.startswith("$")], anchor=ai)
    return make


def s1_calc(i):
    def make(rng):
        now, w = base(rng)
        ex = calc_ex(rng, now, w)
        st = ex["st"]
        hist, ai = session(rng, [ex])
        prompt = rng.choice(st["ask"])
        final = fin(rng, st["ans"])
        return sc_(rng, prompt, now, w, [Final(final)], "a computed figure stated earlier: answer from the conversation, do not recompute with a call", hist,
                   "s1_calc", no_call=True, must=st["must"], reply_must=st["must"], anchor=ai)
    return make


# =============================================================================
# SUB-SKILL 2: re-read when the detail was not stated
# =============================================================================
def s2_agenda_loc(i):
    def make(rng):
        now, w, ex = agenda_world(rng, nonzero=True, n=rng.choice([3, 4]), with_loc=False, bullets=i % 4 == 3)
        st = ex["st"]
        its = st["items"]
        deep = False
        hist, ai = session(rng, [ex], after=rng.choice([0, 1, 2, 3, 4]), before=rng.choice([1, 2, 3]))
        deep = len(hist) - 1 - ai >= 1
        if i % 2 == 0:
            it = rng.choice(its)
            ref = f"the {it['short']}"
            prompt = rng.choice(["where's {r}", "where is {r} again", "what room or address is {r} at"]).format(r=ref)
            query = it["short"] if (i % 4 == 0 and re.sub(r"\W", "", it["short"].lower()) in re.sub(r"\W", "", it["title"].lower())) else it["title"]
        else:
            o, idx = ordinal(rng, rng.choice([1, 2, 0]) % len(its), len(its))
            it = its[idx]
            prompt = lead_in(rng, deep, "the schedule") + rng.choice(["where is {o} {n} you listed", "where's {o} {n} on that list",
                                                                      "and {o} {n}, where is it", "wait, where's {o} {n}", "{o} {n}, where does that happen", "what about {o} {n}, where"]).format(o=o, n=rng.choice(ORD_NOUN))
            query = it["title"]
        others = [x["loc"] for x in its if x is not it and x["loc"] != it["loc"]]
        dstr = it["start"].strftime("%b %-d")
        final = fin(rng, f"Your {it['noun']} is at {it['loc']}, and it starts at {it['c']} {st['on_pw']}, {dstr}.", f"That's {it['loc']}: your {it['noun']} {st['on_pw']} ({dstr}) at {it['c']}.")
        return sc_(rng, prompt, now, w, [[Call("get_upcoming", period=st["period"], query=query)], Final(final)],
                   "the earlier reply gave times only: the place needs the calendar again, aimed at the item the person pointed to", hist, "s2_agenda",
                   first="get_upcoming", args={"period": st["period"], "query": query}, in_result=[it["loc"], it["c"], dstr], not_in_result=others,
                   must=[it["loc"], it["c"], dstr], reply_must=[it["c"]], anchor=ai)
    return make


MAIL_ASK = ["what does {o} {n} say", "read me {o} {n}", "what's in {o} {n}", "what was in {o} {n} again", "give me the details on {o} {n}",
            "wait, {o} {n}, what does it say", "{o} {n}? what's it about", "open {o} {n} for me", "what about {o} {n}", "and {o} {n}? i didn't see what it said",
            "skip to {o} {n}"]
MAIL_BY_SENDER = ["what did {s} say", "read me the one from {s}", "what does the {s} email say", "open the {s} one", "{s}, what's in that"]


def s2_mail_ord(i):
    def make(rng):
        now, w = base(rng)
        ex = mail_ex(rng, now, w, n=3, brief=False, ids=i % 2 == 0, digest_tool=i % 3 == 2)
        st = ex["st"]
        ms = st["mails"]
        hist, ai = session(rng, [ex], after=rng.choice([0, 1, 2, 3, 4]), before=rng.choice([1, 2, 3]))
        deep = len(hist) - 1 - ai >= 1
        o, idx = ordinal(rng, [1, 0, 2, 1, 2][i % 5], 3)
        m = ms[idx]
        if i % 4 == 3:
            prompt = rng.choice(MAIL_BY_SENDER).format(s=m["s"])
        else:
            prompt = lead_in(rng, deep, "the emails") + rng.choice(MAIL_ASK).format(o=o, n=rng.choice(MAIL_NOUN))
        others = [x["subj"] for x in ms if x is not m]
        query = m["subj"] if i % 3 else m["s"].split()[0]
        if query != m["subj"]:
            # a sender word must pick out one email only
            if sum(_has(x["s"] + x["a"] + x["subj"] + x["body"], query) for x in ms) != 1:
                query = m["subj"]
        final = fin(rng, f"{m['s']} says {m['say']}.", f"The email from {m['s']} says {m['say']}.", f"From {m['s']}: {m['say']}.",
                    f"{m['s']} wrote that {m['say']}.", f"It's from {m['s']}, and it says {m['say']}.", f"In “{m['subj']}”, {m['s']} says {m['say']}.")
        return sc_(rng, prompt, now, w, [[Call("view_emails", query=query, count=1)], Final(final)],
                   "the ordinal is the item as listed in the earlier reply; the body was never stated, so the email is read again", hist, "s2_mail_ord",
                   first="view_emails", in_result=[m["subj"]] + m["keys"], not_in_result=others, must=m["keys"], anchor=ai)
    return make


def s2_mail_detail(i):
    def make(rng):
        now, w = base(rng)
        ex = mail_ex(rng, now, w, n=3, brief=False, ids=True, digest_tool=False)
        st = ex["st"]
        m = rng.choice(st["mails"])
        q, key = rng.choice(m["q2"])
        hist, ai = session(rng, [ex], after=rng.choice([0, 1, 2, 3]), before=rng.choice([1, 2, 3]))
        others = [x["subj"] for x in st["mails"] if x is not m]
        lead, rest = Q2X[(m["s"], key)]
        final = fin(rng, f"{lead} The same email adds that {rest}.", f"{lead} It also says {rest}.", f"{lead} Beyond that, {rest}.",
                    f"{lead} The email also mentions that {rest}.")
        return sc_(rng, q, now, w, [[Call("view_emails", query=m["subj"], count=1)], Final(final)],
                   "a detail only the body holds (the digest listed senders and subjects): read that email, then state the asked detail", hist,
                   "s2_mail_detail", first="view_emails", in_result=[m["subj"], key], not_in_result=others, must=[key], anchor=ai)
    return make


def s2_rem_when(i):
    def make(rng):
        now, w = base(rng)
        ex = rem_ex(rng, now, w, n=rng.choice([3, 4]), times=False, nonzero=True)
        st = ex["st"]
        its = st["items"]
        hist, ai = session(rng, [ex], after=rng.choice([0, 1, 2, 3, 4]), before=rng.choice([1, 2, 3]))
        deep = len(hist) - 1 - ai >= 1
        if i % 2 == 0:
            it = rng.choice(its)
            prompt = rng.choice(["when is the {q} one due", "what time is the {q} reminder for", "when's the {q} thing due"]).format(q=it["q"])
        else:
            o, idx = ordinal(rng, rng.choice([1, 2, 0]) % len(its), len(its))
            it = its[idx]
            prompt = lead_in(rng, deep, "the reminders") + rng.choice(["when is {o} {n} due", "what time is {o} {n} you listed",
                                                                       "and {o} {n}, when's that due", "wait, when does {o} {n} go off", "what about {o} {n}, due when"]).format(o=o, n=rng.choice(REM_NOUN))
        others = [x["title"] for x in its if x is not it]
        dstr = it["due"].strftime("%a %b %-d")
        final = fin(rng, f"Your reminder to {lowfirst(it['title'])} is due {it['when']}, which is {dstr}.",
                    f"That one is due {it['when']} ({dstr}): {lowfirst(it['title'])}.")
        return sc_(rng, prompt, now, w, [[Call("search_reminders", query=it["q"])], Final(final)],
                   "the earlier reply listed reminder titles without times: read the reminders again for the one pointed at", hist, "s2_rem",
                   first="search_reminders", in_result=[it["title"], it["c"], dstr], not_in_result=others, must=[it["c"], lowfirst(it["title"]), dstr], anchor=ai)
    return make


def s2_msg_exact(i):
    def make(rng):
        now, w = base(rng)
        ex = msg_ex(rng, now, w, detail=False)
        st = ex["st"]
        p = rng.choice(st["picks"])
        other = [x for x in st["picks"] if x is not p][0]
        hist, ai = session(rng, [ex], after=rng.choice([0, 1, 2, 3, 4]), before=rng.choice([1, 2, 3]))
        prompt = rng.choice(["what exactly did {n} say", "read me {n}'s text", "what was {n}'s message, word for word", "{n}'s message, what did it say"]).format(n=p["who"].lower())
        msg = [m for m in w.messages if m.sender == p["who"]][0]
        sent = f"{msg.when:%-I:%M %p}"
        final = fin(rng, f"{p['full']} wrote: “{p['text']}”. It came in at {sent}.", f"{p['full']}'s text, sent at {sent}, says “{p['text']}”.")
        return sc_(rng, prompt, now, w, [[Call("view_messages", query=p["who"])], Final(final)],
                   "the earlier reply gave the topic only: the exact words need the messages again", hist, "s2_msg",
                   first="view_messages", in_result=[p["text"], sent], not_in_result=[other["text"]], must=[p["text"], sent], anchor=ai)
    return make


def s2_wx(i):
    def make(rng):
        now, w = base(rng)
        ex = wx_ex(rng, now, w, full=False)
        st = ex["st"]
        hist, ai = session(rng, [ex], after=rng.choice([0, 1, 2, 3, 4]), before=rng.choice([1, 2, 3]))
        if i % 2 == 0:
            prompt = rng.choice(["and how low does it go that night", "what's the low there", "how cold does it get overnight"])
            rain = rain_phrase(st["rain"])
            final = fin(rng, f"The low in {st['city']} {st['pw']} is {st['lo']}°, with a high of {st['hi']}° and {rain}.",
                        f"{cap1(st['pw'])} in {st['city']} it drops to {st['lo']}° after a high of {st['hi']}°, and there's {rain}.")
            must = [f"{st['lo']}°", f"{st['hi']}°"]
        else:
            prompt = rng.choice(["will it rain there", "how likely is rain in {c}", "any rain expected in {c}"]).format(c=st["city"])
            rain = rain_phrase(st["rain"])
            final = fin(rng, f"{cap1(st['pw'])} in {st['city']} there's {rain}, with a high of {st['hi']}° and a low of {st['lo']}°.",
                        f"For {st['city']} {st['pw']}: {rain}, with temperatures between {st['lo']}° and {st['hi']}°.")
            must = [f"{st['rain']}%"] if st["rain"] else ["no rain"]
            must += [f"{st['hi']}°"]
        return sc_(rng, prompt, now, w, [[Call("get_weather", location=st["loc"])], Final(final)],
                   "the earlier reply gave the headline only: the low or the rain chance comes from the forecast again, same city", hist, "s2_wx",
                   first="get_weather", args={"location": st["loc"]}, in_result=[m for m in must if m != "no rain"], must=must, anchor=ai)
    return make


def s2_notes(i):
    def make(rng):
        now, w = base(rng)
        ex = notes_ex(rng, now, w)
        st = ex["st"]
        nts = st["notes"]
        hist, ai = session(rng, [ex], after=rng.choice([0, 1, 2, 3, 4]), before=rng.choice([1, 2, 3]))
        deep = len(hist) - 1 - ai >= 1
        idx = i % 2
        title, body, folder, qword = nts[idx]
        if i % 3 == 0:
            o = "the second one" if idx == 1 else "the first one"
            prompt = lead_in(rng, deep, "those notes") + rng.choice([f"what's in {o}", f"open {o}", f"and {o}, what does it say"])
        else:
            prompt = rng.choice(["what's in the {q} one", "open the {q} note and tell me what it says", "what did i write in the {q} note"]).format(q=qword)
        # state two facts from the body, in the body's words
        parts = [p.strip() for p in re.split(r",| and ", body.rstrip("."))]
        facts = [p for p in parts if len(p) > 6][:2]
        final = fin(rng, f"Your “{title}” note says: {body}", f"In “{title}”: {body}", f"That note, “{title}”, reads: {body}")
        other = nts[1 - idx][1]
        return sc_(rng, prompt, now, w, [[Call("search_notes", query=qword)], Final(final)],
                   "the earlier reply named two notes without their contents: read the one pointed at", hist, "s2_notes",
                   first="search_notes", in_result=[title, facts[0]], not_in_result=[other], must=[body.rstrip(".")], min_final=40, anchor=ai)
    return make


# =============================================================================
# SUB-SKILL 3: challenges (re-read) and "what?" after Wisp's own question
# =============================================================================
CHALLENGE = ["are you sure?", "can you double check that", "hm that doesn't sound right", "wait, is that right?",
             "double-check it for me", "that doesn't match what i remember", "hold on, check again", "you sure about that?",
             "i thought it was different, can you look again", "hmm are you certain", "that seems off, take another look"]


# A challenge that names what it doubts reads like a person; the bare ones only work right after the reply.
TOPIC_CHALLENGE = {
    "agenda": ["is that really everything on the list", "that doesn't look like the whole day, look again", "wait, are those the right times",
               "hm, i thought there was another one, check", "are the times you gave right?"],
    "mail": ["is that really all that's unread?", "only three? look again", "i thought more had come in, check the inbox", "wait, is that the whole inbox"],
    "rem": ["are those the right times for the reminders", "hm those times seem off, look again", "wait, is that the right order?",
            "is that really everything i've got coming up"],
    "wx": ["really? that forecast seems off, look again", "hm, is it really going to be that, check", "that's not what the app says, can you check?",
           "wait, is that the right forecast"],
}


def _challenge_prompt(rng, deep, key):
    if deep or rng.random() < 0.4:
        return rng.choice(TOPIC_CHALLENGE[key])
    return rng.choice(CHALLENGE)


def s3_agenda(changed):
    def make(rng):
        now, w, ex = agenda_world(rng, nonzero=True, n=rng.choice([3, 3, 4]), brief=0.45)
        st = ex["st"]
        its = st["items"]
        old = {id(x): x["c"] for x in its}
        deep = rng.random() < 0.25
        hist, ai = session(rng, [ex], after=1 if deep else 0, before=rng.choice([1, 2, 3, 4]))
        prompt = _challenge_prompt(rng, deep, "agenda")
        reply_must = [x["c"] for x in its]
        if not changed:
            lst = join([f"your {x['noun']} at {x['c']}" for x in its])
            final = fin(rng, f"Yes, it's still the same: {st['on_pw']} you have {lst}.", f"I checked again and nothing has changed: {lst} {st['on_pw']}.",
                        f"It holds up. {cap1(st['pw'])} is {lst}.", f"Same as before, {st['on_pw']}: {lst}.")
            must = [x["c"] for x in its]
            if st["extra"]:       # the plan also lists a Brief item: the re-read names it too
                final += " " + rng.choice([f"The plan also still lists {I.plan_prose(st['extra'])}.", f"It also still has {I.plan_prose(st['extra'])}."])
                must += [x.title for x in st["extra"]]
            kinds = "s3_agenda_same"
        else:
            it = rng.choice(its[:-1] if rng.random() < .5 and len(its) > 2 else its)
            mode = rng.choice(["shift", "shift", "add"])
            if mode == "shift":
                new_start = None
                for shift in rng.sample([45, 60, 90, 105, 135], 5):
                    cand = it["start"] + D(minutes=shift)
                    if cand.minute == 0:
                        cand += D(minutes=15)
                    if all(abs((cand - x["start"]).total_seconds()) >= 3600 for x in its if x is not it) and cand.hour < 21:
                        new_start = cand
                        break
                if new_start is None:
                    return None
                for ev in w.events:
                    if ev.title == it["title"]:
                        ev.start = new_start
                nc = clock(new_start)
                final = fin(rng, f"It has changed since I last looked: your {it['noun']} now starts at {nc} {st['on_pw']}, not {it['c']}. The rest of {st['pw']} is as I said.",
                            f"That has moved. Your {it['noun']} is now at {nc} instead of {it['c']}, and everything else {st['on_pw']} is the same.")
                must = [nc, it["noun"]]
                kinds = "s3_agenda_moved"
            else:
                extra = [e for e in EV if e[2] != 0 and e[0] not in {x["title"] for x in its}]
                t, h, m, mins, loc, sh = rng.choice(extra)
                start = at_(st["day"], h, m)
                w.events.append(Event(t, start, mins, loc))
                nc = clock(start)
                final = fin(rng, f"There's one more now: your {noun(t)} at {nc} {st['on_pw']}, added since I last looked. The rest is as I said.",
                            f"Something new has appeared {st['on_pw']}: your {noun(t)} at {nc}. Everything else is unchanged.")
                must = [nc, noun(t)]
                kinds = "s3_agenda_added"
        # a time-scoped ask is read through the Brief's day plan (the live hint), so the re-read is the same call as the
        # earlier turn's: get_my_day_plan(day=<that day>)
        return sc_(rng, prompt, now, w, [[Call("get_my_day_plan", day=st["period"])], Final(final)],
                   "a challenge is a re-read against the source: the same read (the day plan), then the fresh result (confirmed, or what it is now)", hist,
                   kinds, first="get_my_day_plan", args={"day": st["period"]}, in_result=must, must=must, reply_must=reply_must[:1], anchor=ai)
    return make


def s3_mail(changed):
    def make(rng):
        now, w = base(rng)
        ex = mail_ex(rng, now, w, n=3, brief=False, ids=False, digest_tool=True)
        st = ex["st"]
        ms = st["mails"]
        deep = rng.random() < 0.25
        hist, ai = session(rng, [ex], after=1 if deep else 0, before=rng.choice([1, 2, 3, 4]))
        prompt = _challenge_prompt(rng, deep, "mail")
        if not changed:
            final = fin(rng, f"Yes, you still have 3 unread: " + join([f"{m['s']} (“{m['subj']}”)" for m in ms]) + ".",
                        f"I looked again and it's the same, 3 unread, from " + join([m["s"] for m in ms]) + ".",
                        f"Nothing new has come in: 3 unread, from " + join([m["s"] for m in ms]) + ".",
                        f"That's right. The inbox shows 3 unread, from " + join([m["s"] for m in ms]) + ".")
            must = [m["s"] for m in ms]
            kind = "s3_mail_same"
        else:
            avail = [m for m in MAILS if m not in ms]
            new = rng.choice(avail)
            w.emails.insert(0, Email(new["s"], new["a"], new["subj"], now - D(minutes=rng.randint(5, 30)), bodylines(new["body"]), True, True))
            final = fin(rng, f"Yes, one more has come in: {new['s']} sent “{new['subj']}”, so you have 4 unread now.",
                        f"It has changed. {new['s']} just sent “{new['subj']}”, which makes 4 unread.")
            must = [new["s"], new["subj"], "4"]
            kind = "s3_mail_new"
        return sc_(rng, prompt, now, w, [[Call("summarize_emails", unread=True)], Final(final)],
                   "a challenge about a count is a fresh read of the mail: confirmed, or what changed", hist, kind,
                   first="summarize_emails", args={"unread": True}, in_result=must[:2], must=must, reply_must=[ms[0]["s"]], anchor=ai)
    return make


def s3_rem(changed):
    def make(rng):
        now, w = base(rng)
        ex = rem_ex(rng, now, w, n=3, times=True, nonzero=True)
        st = ex["st"]
        its = st["items"]
        deep = rng.random() < 0.25
        hist, ai = session(rng, [ex], after=1 if deep else 0, before=rng.choice([1, 2, 3, 4]))
        prompt = _challenge_prompt(rng, deep, "rem")
        if not changed:
            lst = join([f"{lowfirst(x['title'])} {x['when']}" for x in its])
            final = fin(rng, f"Yes, that's right: {lst}.", f"I checked again and it holds: {lst}.", f"They're unchanged: {lst}.",
                        f"Those times are right: {lst}.")
            must = [x["c"] for x in its]
            kind = "s3_rem_same"
        else:
            it = rng.choice(its)
            new_due = it["due"] + D(minutes=rng.choice([45, 90, 135]))
            if new_due.minute == 0:
                new_due += D(minutes=15)
            for r in w.reminders:
                if r.title == it["title"]:
                    r.due = new_due
            nw = when_phrase(new_due, now)
            final = fin(rng, f"It has changed: your reminder to {lowfirst(it['title'])} is now due {nw}, not {it['when']}.",
                        f"That has moved. The reminder to {lowfirst(it['title'])} is now due {nw}, where I said {it['c']} before.")
            must = [clock(new_due), lowfirst(it["title"])]
            kind = "s3_rem_moved"
        return sc_(rng, prompt, now, w, [[Call("search_reminders", scope="upcoming")], Final(final)],
                   "a challenge about a due time is a fresh read of the reminders", hist, kind,
                   first="search_reminders", args={"scope": "upcoming"}, in_result=[m for m in must if m != lowfirst(it["title"] if changed else "")] if changed else must,
                   must=must, reply_must=[its[0]["c"]], anchor=ai)
    return make


def s3_wx(changed):
    def make(rng):
        now, w = base(rng)
        ex = wx_ex(rng, now, w, full=True, idx=1)
        st = ex["st"]
        wx = w.weather[st["loc"]]
        deep = rng.random() < 0.25
        hist, ai = session(rng, [ex], after=1 if deep else 0, before=rng.choice([1, 2, 3, 4]))
        prompt = _challenge_prompt(rng, deep, "wx")
        if not changed:
            rain = rain_phrase(st["rain"])
            final = fin(rng, f"Yes, it still holds: {st['pw']} in {st['city']} it's {st['desc']}, high {st['hi']}°, low {st['lo']}°, with {rain}.",
                        f"I looked again and it's unchanged: {st['desc']}, a high of {st['hi']}°, a low of {st['lo']}° and {rain}.",
                        f"The forecast hasn't moved: {st['pw']} in {st['city']} is {st['desc']}, with {st['hi']}° and {st['lo']}° and {rain}.",
                        f"That's still what it shows: {st['desc']}, high {st['hi']}°, low {st['lo']}°, {rain}.")
            must = [f"{st['hi']}°", f"{st['lo']}°"]
            kind = "s3_wx_same"
        else:
            hi = st["hi"] - rng.choice([4, 5, 6])
            lo = st["lo"] - rng.choice([3, 4])
            d = list(wx["days"][1])
            d[1], d[2], d[3], d[4] = hi, lo, "Showers", 80
            wx["days"][1] = tuple(d)
            final = fin(rng, f"It looks different now: {st['pw']} in {st['city']} it's showers, high {hi}°, low {lo}°, with an 80% chance of rain. I said {st['hi']}° and {st['lo']}° before.",
                        f"The forecast has changed to showers, with a high of {hi}°, a low of {lo}° and an 80% chance of rain, where I said {st['hi']}° and {st['lo']}° before.")
            must = [f"{hi}°", f"{lo}°", "80%"]
            kind = "s3_wx_changed"
        return sc_(rng, prompt, now, w, [[Call("get_weather", location=st["loc"])], Final(final)],
                   "a challenge about a forecast is a fresh read: confirmed, or the new numbers", hist, kind,
                   first="get_weather", args={"location": st["loc"]}, in_result=must, must=must, reply_must=[f"{st['hi']}°"], anchor=ai)
    return make


CLARIFY = [  # (earlier user, assistant context/question, restated question, keys)
    ("move it to the evening", "Which one: the {a} or the {b}?", "I was asking which one you meant, the {a} or the {b}.", ["{a}", "{b}"]),
    ("tell them i'm running late", "Do you mean {a} or {b}?", "I asked which one you mean, {a} or {b}.", ["{a}", "{b}"]),
    ("shoot that off for me", "Send what, and to whom?", "I was asking what you'd like to send and who it should go to.", ["send", "who"]),
    ("set a reminder for me", "What should I remind you about, and when?", "I was asking what to remind you about and when.", ["remind", "when"]),
    ("cancel the study session", "I see two: the Tuesday one at {a} and the Thursday one at {b}. Which should I cancel?", "I was asking which study session to cancel, Tuesday at {a} or Thursday at {b}.", ["Tuesday", "Thursday"]),
    ("text her the address", "Which address, and which of them: {a} or {b}?", "I asked which address to send, and whether it goes to {a} or {b}.", ["{a}", "{b}"]),
]
BARE = ["what?", "huh?", "sorry, what was that", "come again?", "hm?", "wait what did you ask", "sorry?", "what do you mean"]


def s3_restate(i):
    def make(rng):
        now, w = base(rng)
        kind = i % 6
        user, q, restate, keys = CLARIFY[kind]
        if kind == 0:
            a, b = rng.choice([("library books", "dentist call"), ("pharmacy call", "lab report"), ("recycling reminder", "bus pass renewal")])
            lead = turn("which reminders land tomorrow", f"Two reminders tomorrow: {lowfirst(a)} and {lowfirst(b)}.")
        elif kind == 1:
            a, b = rng.choice([("Theo", "Jordan"), ("Maya", "Carmen"), ("Aisha", "Jordan"), ("Marcus", "Priya")])
            lead = turn("any texts", f"{a} and {b} both wrote to you.")
        elif kind == 4:
            a, b = rng.choice([("3:15 PM", "5 PM"), ("1:30 PM", "4:45 PM")])
            lead = turn("what study sessions do i have this week", f"You have two study sessions: Tuesday at {a} and Thursday at {b}.")
        elif kind == 5:
            a, b = rng.choice([("Maya", "Carmen"), ("Priya", "Aisha"), ("Carmen", "Priya")])
            lead = turn("who's coming to dinner", f"{a} and {b} are both coming.")
        else:
            a = b = ""
            lead = dict(turn(*rng.choice([(u, t) for u, t, *_ in banks()["short"][:10]])))
        question = q.format(a=a, b=b)
        ask = dict(turn(user if kind != 4 else user, question))
        hist = fillers(rng, rng.choice([2, 3, 4]))
        hist = hist + [lead, ask] if kind in (0, 1, 4, 5) else hist + [ask]
        hist = hist[-9:]
        prompt = rng.choice(BARE)
        final = restate.format(a=a, b=b)
        mk_keys = [k.format(a=a, b=b) for k in keys]
        return sc_(rng, prompt, now, w, [Final(final)], "a bare 'what?' after Wisp's own clarifying question restates that question briefly, no call", hist,
                   "s3_restate", no_call=True, must=mk_keys, anchor=len(hist) - 1, styled=False)
    return make


# =============================================================================
# SUB-SKILL 4: the session itself
# =============================================================================
ANCHOR_KINDS = ["agenda", "mail", "rem", "msg", "wx", "price", "calc", "notes"]


def _anchor(rng, kind, now, w):
    if kind == "agenda":
        return agenda_ex(rng, now, w, nonzero=False)
    if kind == "mail":
        return mail_ex(rng, now, w, n=3, brief=rng.random() < 0.5, ids=True)
    if kind == "rem":
        return rem_ex(rng, now, w, n=3, times=True, nonzero=False)
    if kind == "msg":
        return msg_ex(rng, now, w, detail=rng.random() < 0.5)
    if kind == "wx":
        return wx_ex(rng, now, w, full=True)
    if kind == "price":
        return price_ex(rng, now, w)
    if kind == "calc":
        return calc_ex(rng, now, w)
    return notes_ex(rng, now, w)


def _s4_history(rng, n_anchor, n_fill):
    now, w = base(rng)
    kinds = rng.sample(ANCHOR_KINDS, n_anchor)
    exs = []
    for k in kinds:
        # one world holds every anchor's data; anchors of one kind never repeat
        exs.append(_anchor(rng, k, now, w))
    pad = fillers(rng, n_fill, exclude={e["user"] for e in exs})
    # shuffle whole blocks: a chain's question, its follow-up and any ack stay adjacent and in order
    blocks = []
    for e in pad + exs:
        if blocks and e.get("cblk") and blocks[-1][0].get("cblk") == e["cblk"]:
            blocks[-1].append(e)
        else:
            blocks.append([e])
    rng.shuffle(blocks)
    seq = [e for b in blocks for e in b]
    return now, w, seq, exs


def _uniq(seq):
    """The exchanges with one of each topic (a two-turn chain is one topic)."""
    seen, out = set(), []
    for e in seq:
        if e["topic"] not in seen:
            seen.add(e["topic"])
            out.append(e)
    return out


def _topic_ok(seq):
    """Topics must be distinguishable: no two topics share a first keyword, and no topic's keyword appears
    inside another's topic (the recap would name both)."""
    seq = _uniq(seq)
    keys = [e["keys"][0].lower() for e in seq]
    tops = [e["topic"].lower() for e in seq]
    if len(set(keys)) != len(keys):
        return False
    return not any(i != j and keys[i] in tops[j] for i in range(len(seq)) for j in range(len(seq)))


def s4_first(i):
    def make(rng):
        now, w, seq, exs = _s4_history(rng, rng.choice([1, 2, 2]), rng.choice([2, 3, 4]))
        if not _topic_ok(seq):
            return None
        first = seq[0]
        prompt = rng.choice(["which question started this chat", "remind me what the very first question was",
                             "what was the opening question of this conversation", "what did we look at first here", "what came first in this chat, question-wise"])
        final = fin(rng, f"You started by asking about {first['topic']}.", f"The first thing you asked was about {first['topic']}.",
                    f"Your opening question was about {first['topic']}.")
        others = [e["keys"][0] for e in seq[1:] if e["topic"] != first["topic"]]
        return sc_(rng, prompt, now, w, [Final(final)], "the first user message of THIS chat, found in the history; never 'nothing has been said before'",
                   seq, "s4_first", no_call=True, must=first["keys"] if first.get("kind") else first["keys"], must_not=others, anchor=0)
    return make


def s4_cover(i):
    def make(rng):
        now, w, seq, exs = _s4_history(rng, rng.choice([2, 2, 3]), rng.choice([2, 3, 4]))
        if not _topic_ok(seq) or len(_uniq(seq)) > 5:
            return None
        topics = join([e["topic"] for e in _uniq(seq)])
        prompt = rng.choice(["what have we covered so far", "give me a quick recap of this chat", "what did we talk about so far", "can you sum up what we've done here",
                             "so what have we been through in this chat", "run back over what we've talked about"])
        final = fin(rng, f"So far we've gone over {topics}.", f"We've covered {topics}.", f"This chat has been about {topics}.")
        return sc_(rng, prompt, now, w, [Final(final)], "a recap of the session from its history, every topic named",
                   seq, "s4_cover", no_call=True, must=[e["keys"][0] for e in _uniq(seq)], anchor=0, min_final=1)
    return make


def s4_before(i):
    def make(rng):
        now, w, seq, exs = _s4_history(rng, rng.choice([1, 2, 3]), rng.choice([2, 3, 4]))
        if not _topic_ok(seq):
            return None
        last = seq[-1]
        if last.get("crole") in ("ack", "fu"):   # the literal previous message is a thanks or a short reply, not a question
            return None
        prompt = rng.choice(["what was the question right before this one", "what was the last question before this", "and the question before this one was what",
                             "what was the thing i asked you just before"])
        final = fin(rng, f"Right before this you asked about {last['topic']}.", f"Your last question was about {last['topic']}.")
        others = [e["keys"][0] for e in seq[:-1] if e["topic"] != last["topic"]]
        return sc_(rng, prompt, now, w, [Final(final)], "the previous user message of this chat", seq, "s4_before", no_call=True,
                   must=last["keys"], must_not=others, anchor=len(seq) - 1)
    return make


def hist_reply(seq, e):
    return next(t["assistant"] for t in seq if t is e)


def s4_goback(i):
    def make(rng):
        now, w, seq, exs = _s4_history(rng, rng.choice([2, 2, 3]), rng.choice([2, 3, 4, 5]))
        if not _topic_ok(seq) or len(seq) > 6:
            return None
        e = rng.choice([x for x in exs if x["kind"] in ("agenda", "mail", "rem", "msg", "wx", "price", "calc")])
        cb = e["cb"]
        prompt = rng.choice(["go back to what you said about {x}", "what did you tell me about {x} again", "can you repeat what you said about {x}",
                             "i missed what you said about {x}"]).format(x=cb)
        reply = hist_reply(seq, e)
        final = fin(rng, f"Here it is again. {reply}", f"This is what I told you about {cb}. {reply}", f"Going back to {cb}. {reply}")
        must = e["facts"]
        return sc_(rng, prompt, now, w, [Final(final)], "'go back to what you said about X': the earlier reply restated from the history, no call",
                   seq, "s4_goback", no_call=True, must=must, reply_must=must, anchor=seq.index(e), min_final=1)
    return make


def s4_didask(yes):
    def make(rng):
        now, w, seq, exs = _s4_history(rng, rng.choice([1, 2, 2]), rng.choice([2, 3]))
        if not _topic_ok(seq):
            return None
        if yes:
            e = rng.choice([x for x in exs if x["kind"] in ("agenda", "mail", "rem", "msg", "wx", "price", "calc", "notes")])
            word = {"agenda": "the schedule", "mail": "the emails", "rem": "the reminders", "msg": "the texts", "wx": "the weather",
                    "price": "a comparison", "calc": "the numbers", "notes": "the notes"}[e["kind"]]
            prompt = rng.choice(["have we already talked about {w}", "did {w} come up in this chat", "has {w} come up yet"]).format(w=word)
            reply = hist_reply(seq, e)
            final = fin(rng, f"Yes, you asked about {e['topic']} earlier. Here it is again. {reply}", f"Yes, we covered {e['topic']} a few messages back. {reply}")
            return sc_(rng, prompt, now, w, [Final(final)], "'did I ask about X': yes, from the history, with what was said", seq, "s4_did_yes",
                       no_call=True, must=e["facts"], reply_must=e["facts"], anchor=seq.index(e), min_final=1)
        # a topic counts as absent only when none of its words (the user's or the assistant's) is anywhere in the chat
        aliases = {"weather": ("weather", "forecast", "rain", "temperature", "sunny", "cloud"),
                   "unread": ("unread", "email", "mail", "inbox"),
                   "reminders": ("reminder", "remind", "due"),
                   "texts": ("text", "message", "messaged", "texted")}
        absent = [k for k, al in aliases.items() if not any(_has(t["user"] + " " + t["assistant"], a) for t in seq for a in al)]
        if not absent:
            return None
        k = rng.choice(absent)
        word = {"weather": "the weather", "unread": "the emails", "reminders": "the reminders", "texts": "the texts"}[k]
        topics = join([e["topic"] for e in _uniq(seq)])
        if any(_has(topics, a) for a in aliases[k]):     # the recap must not name the topic it denies
            return None
        prompt = rng.choice(["have we talked about {w} yet", "did {w} come up earlier", "has {w} come up in this chat"]).format(w=word)
        opts = [f"No, we haven't touched on {word} in this chat. So far we've covered {topics}.", f"Not yet. We've only been over {topics}."]
        if _r5():
            opts += [t.format(W=cap1(word), word=word, topics=topics) for t in DID_NO_R5]
        final = fin(rng, *opts)
        return sc_(rng, prompt, now, w, [Final(final)], "'did I ask about X' when it did not come up: a plain no with what was covered", seq, "s4_did_no",
                   no_call=True, must=[e["keys"][0] for e in _uniq(seq)], anchor=0, min_final=1)
    return make


# =============================================================================
# SUB-SKILL 5: stored facts
# =============================================================================
FACT = [  # key, stored text, recall stems (each must be in the prompt AND in the stored text), prompts, finals, must
    ("allergy", "Allergic to penicillin and shellfish.", ("allerg",),
     ["which allergies do i have again", "list my allergies", "remind me what i'm allergic to"],
     ["You're allergic to penicillin and shellfish.", "Your saved allergies are penicillin and shellfish."], ["penicillin", "shellfish"]),
    ("work", "Works at the Hollis Library help desk.", ("work",),
     ["who do i work for again", "what's my workplace", "where do i work, remind me"],
     ["You work at the Hollis Library help desk.", "You're at the Hollis Library help desk."], ["Hollis Library help desk"]),
    ("bus", "Takes the 7:40 bus to campus on weekdays.", ("bus", "campus"),
     ["which bus do i take in the mornings", "what time is my usual bus", "how do i get to campus in the morning"],
     ["You take the 7:40 bus to campus on weekdays.", "Your usual bus is the 7:40 to campus on weekdays."], ["7:40"]),
    ("coffee", "Usual coffee order: flat white with oat milk, no sugar.", ("coffee",),
     ["what's my usual coffee order", "how do i take my coffee", "what do i usually get at the coffee shop"],
     ["Your usual is a flat white with oat milk and no sugar.", "You take it as a flat white with oat milk, no sugar."], ["flat white", "oat milk"]),
    ("advisor", "Advisor is Dr. Nguyen, office in Mercer Hall 220.", ("advisor",),
     ["who's my advisor", "where is my advisor's office", "what's my academic advisor called"],
     ["Your advisor is Dr. Nguyen, and the office is in Mercer Hall 220.", "Dr. Nguyen is your advisor, with an office in Mercer Hall 220."], ["Dr. Nguyen", "Mercer Hall 220"]),
    ("shoe", "Wears a size 9 shoe.", ("shoe",),
     ["what shoe size am i", "do you know what size shoes i wear"],
     ["You wear a size 9 shoe.", "Your shoes are size 9."], ["size 9"]),
    ("roommate", "Roommate is Dev, who has the room by the kitchen.", ("roommate",),
     ["who's my roommate", "what's my roommate's name again"],
     ["Your roommate is Dev, who has the room by the kitchen.", "That's Dev, the roommate in the room by the kitchen."], ["Dev"]),
    ("hometown", "Hometown is Tacoma.", ("hometown",),
     ["what's my hometown", "remind me what my hometown is"],
     ["Your hometown is Tacoma.", "Tacoma is your hometown."], ["Tacoma"]),
    ("spare key", "Keeps a spare key with the neighbor in 3B.", ("spare", "key"),
     ["who has my spare key", "where did i leave the extra key"],
     ["Your neighbor in 3B has your spare key.", "The spare key is with the neighbor in 3B."], ["3B"]),
    ("diet", "Doesn't eat pork.", ("eat",),
     ["is there anything i don't eat", "what foods do i not eat"],
     ["You don't eat pork.", "That's pork: you don't eat it."], ["pork"]),
    ("languages", "Languages: English and some Spanish.", ("languages",),
     ["which languages can i get by in", "which languages do i know"],
     ["You speak English and some Spanish.", "English, and some Spanish."], ["English", "Spanish"]),
    ("major", "Major: computer science. Minor: philosophy.", ("major",),
     ["what am i majoring in", "what's my major and minor"],
     ["You're majoring in computer science, with a minor in philosophy.", "Computer science, with a minor in philosophy."], ["computer science", "philosophy"]),
    ("pet", "Has a pet cat named Biscuit.", ("cat", "pet"),
     ["what's my cat called", "what's the name of my pet"],
     ["Your cat is named Biscuit.", "You have a cat called Biscuit."], ["Biscuit"]),
    ("emergency", "Emergency contact is Carmen.", ("emergenc",),
     ["who's my emergency contact", "who do i list for emergencies"],
     ["Your emergency contact is Carmen.", "Carmen is your emergency contact."], ["Carmen"]),
    ("takeout", "Favorite takeout: Golden Lotus, usually the cold sesame noodles.", ("takeout",),
     ["what's my favorite takeout", "where do i usually order takeout from"],
     ["Your favorite takeout is Golden Lotus, usually the cold sesame noodles.", "You usually order from Golden Lotus, the cold sesame noodles."], ["Golden Lotus", "cold sesame noodles"]),
]
# how a person points back at a fact that was stated earlier in the chat
FACT_REF = {"allergy": ["the allergies", "the allergy list"], "work": ["the workplace", "where i work"], "bus": ["the bus", "the morning bus"],
            "coffee": ["the coffee order", "the coffee"], "advisor": ["the advisor", "the advisor's office"], "shoe": ["the shoe size", "the shoes"],
            "roommate": ["the roommate", "the roommate's name"], "hometown": ["the hometown", "where i'm from"], "spare key": ["the spare key", "the key"],
            "diet": ["the food thing", "what i don't eat"], "languages": ["the languages", "the language list"], "major": ["the major", "the major and minor"],
            "pet": ["the cat", "the pet's name"], "emergency": ["the emergency contact", "the emergency person"], "takeout": ["the takeout place", "the takeout order"]}


def fact_ask(rng, fact):
    """(prompt, stem): the stem is a word of the prompt that also appears in the stored text, so the recall query is a single
    word the person actually said."""
    pool = [(p, st) for p in fact[3] for st in fact[2] if st in p.lower() and st in fact[1].lower()]
    assert pool, fact[0]
    return rng.choice(pool)


OTHER_MEM = ["Prefers morning study sessions at Hollis Library.", "Runs with the Saturday club at Riverside Track.",
             "Takes notes by hand in a green notebook.", "Keeps Sunday evenings free for calling home."]
UNSTORED = [
    ("what's my cousin's birthday", "birthday", "your cousin's birthday"),
    ("do you know what high school i went to", "school", "your high school"),
    ("which parking spot is mine", "spot", "your parking spot"),
    ("what's my sister's phone number", "sister", "your sister's number"),
    ("what's my library card number", "card", "your library card number"),
]
UNSTORED_FINALS = [
    "I don't have {x} saved. If you tell me, I'll keep it for next time.",
    "{X} isn't something I have stored. Tell me and I'll hold onto it.",
    "Nothing is saved about {x} yet. I can remember it if you share it.",
    "That one isn't saved: I have nothing on {x}. Tell me and I'll keep it.",
    "I can't find {x} anywhere in what I've saved. If you give it to me, I'll hold onto it.",
]
TOLD = [  # earlier user statement, assistant acknowledgement, question, final, must
    ("my cousin Ines is visiting from Lisbon next month, so i should plan something", "A visit from Lisbon next month is worth planning around. Is there anything you want to set up for it?",
     "what's the cousin's name again, and where is she coming from", "Your cousin is Ines, and she's coming from Lisbon.", ["Ines", "Lisbon"]),
    ("my landlord is called Mr. Ostrow and he said the heater gets fixed wednesday", "Got it: the landlord is Mr. Ostrow, and the heater gets fixed on Wednesday.",
     "who was the landlord again and what day is the heater being fixed", "Your landlord is Mr. Ostrow, and he said the heater gets fixed on Wednesday.", ["Mr. Ostrow", "Wednesday"]),
    ("the wifi at my aunt's place is Cedar-5G, no password on the guest network", "Easy enough for visiting, then: Cedar-5G, with an open guest network.",
     "what was the wifi name at her place again", "It's Cedar-5G, and the guest network has no password.", ["Cedar-5G"]),
    ("my flight lands at 9:45 PM on the 22nd, gate C7", "Got it: you land at 9:45 PM on the 22nd, at gate C7.",
     "when does the flight land again and which gate", "Your flight lands at 9:45 PM on the 22nd, at gate C7.", ["9:45 PM", "C7"]),
    ("i'm meeting my thesis advisor Dr. Mbeki on thursday at 4 in her office, room 218", "Got it, Dr. Mbeki on Thursday at 4, room 218. Is there anything you want to bring along?",
     "where is the meeting with dr mbeki again", "You're meeting Dr. Mbeki on Thursday at 4, in room 218 of her office.", ["Mbeki", "room 218"]),
]


def _memworld(rng, fact, extra=2):
    now, w = base(rng)
    others = [m for m in OTHER_MEM if not any(st in m.lower() for st in fact[2])]
    w.memories = [fact[1]] + rng.sample(others, extra)
    rng.shuffle(w.memories)
    return now, w


def _recall_ok(w, stem, fact):
    text = w.handle("recall", {"query": stem})
    assert text.count("[memory") == 1, text
    return text


def s5_plain(i):
    def make(rng):
        fact = FACT[i % len(FACT)]
        now, w = _memworld(rng, fact)
        hist = fillers(rng, rng.choice([3, 4, 5, 6]))
        prompt, stem = fact_ask(rng, fact)
        final = fin(rng, *fact[4])
        text = _recall_ok(w, stem, fact)
        return sc_(rng, prompt, now, w, [[Call("recall", query=stem)], Final(final)],
                   "a stored fact asked in the middle of a chat: one broad recall (a single word of the question), then the fact", hist, "s5_plain",
                   first="recall", args={"query": stem}, in_result=fact[5], must=fact[5], anchor=0, min_final=15, stem=stem)
    return make


def s5_a_then_b(i):
    def make(rng):
        fa, fb = rng.sample(FACT, 2)
        now, w = base(rng)
        w.memories = [fa[1], fb[1]] + rng.sample([m for m in OTHER_MEM], 1)
        rng.shuffle(w.memories)
        pa, sa = fact_ask(rng, fa)
        pb, sb = fact_ask(rng, fb)
        if any(st in fb[1].lower() for st in fa[2]) or any(st in fa[1].lower() for st in fb[2]):
            return None
        res = w.handle("recall", {"query": sa})
        a_turn = dict(turn(pa, rng.choice(fa[4]), "recall", [{"name": "recall", "args": {"query": sa}, "result": res}]),
                      topic=f"your {fa[0]}", keys=[fa[0]], kind="fact")
        hist = fillers(rng, rng.choice([1, 2, 3])) + [a_turn] + fillers(rng, rng.choice([2, 3, 4, 5]))
        final = fin(rng, *fb[4])
        _recall_ok(w, sb, fb)
        return sc_(rng, pb, now, w, [[Call("recall", query=sb)], Final(final)],
                   "a second stored fact: its detail was never stated earlier in the chat, so recall runs again (broad stem), then the fact",
                   hist, "s5_ab", first="recall", args={"query": sb}, in_result=fb[5], must=fb[5], anchor=0, min_final=15, stem=sb)
    return make


def s5_a_again(i):
    def make(rng):
        fa = FACT[(i * 3 + 1) % len(FACT)]
        now, w = base(rng)
        w.memories = [fa[1]] + rng.sample(OTHER_MEM, 2)
        pa, sa = fact_ask(rng, fa)
        res = w.handle("recall", {"query": sa})
        a_turn = dict(turn(pa, fa[4][0], "recall", [{"name": "recall", "args": {"query": sa}, "result": res}]),
                      topic=f"your {fa[0]}", keys=[fa[0]], kind="fact")
        hist = fillers(rng, rng.choice([1, 2, 3])) + [a_turn] + fillers(rng, rng.choice([2, 3, 4, 5]))
        prompt = rng.choice(["sorry, say {k} again", "what was that about {k}", "i didn't catch {k}, once more", "{k} again? i missed it"]).format(k=rng.choice(FACT_REF[fa[0]]))
        final = fin(rng, fa[4][1])
        return sc_(rng, prompt, now, w, [Final(final)], "the fact was stated earlier in this chat: answer from the conversation", hist, "s5_again",
                   no_call=True, must=fa[5], reply_must=fa[5], anchor=hist.index(a_turn))
    return make


def s5_told(i):
    def make(rng):
        u, a, q, f, must = TOLD[i % len(TOLD)]
        now, w = base(rng)
        told = dict(turn(u, a), topic="a plan", keys=["plan"], kind="told")
        hist = fillers(rng, rng.choice([1, 2, 3])) + [told] + fillers(rng, rng.choice([2, 3, 4, 5]))
        final = fin(rng, f)
        return sc_(rng, q, now, w, [Final(final)], "a fact the person said earlier in THIS chat: answered from the conversation, no lookup", hist, "s5_told",
                   no_call=True, must=must, anchor=hist.index(told))
    return make


def s5_missing(i):
    def make(rng):
        prompt, stem, x = UNSTORED[i % len(UNSTORED)]
        now, w = base(rng)
        w.memories = rng.sample(OTHER_MEM, 2)
        text = w.handle("recall", {"query": stem})
        assert "No matching memory" in text, text
        hist = fillers(rng, rng.choice([3, 4, 5]))
        final = fin(rng, *[t.format(x=x, X=cap1(x)) for t in (UNSTORED_FINALS_R5 if _r5() else UNSTORED_FINALS)])
        return sc_(rng, prompt, now, w, [[Call("recall", query=stem)], Final(final)],
                   "nothing stored: one broad recall, then a plain 'I don't have it'" + ("" if _r5() else " with an offer to remember") + ", no write", hist, "s5_missing",
                   first="recall", args={"query": stem}, min_final=40, anchor=0)
    return make


# =============================================================================
# round 4, long chats: base-length replies (80-150 words), the way a capable chat model answers in this app
# =============================================================================
# (user, assistant, topic: a noun phrase that follows "about", first keyword)
LONGX = [
    ("what's the difference between baking soda and baking powder",
     "They both make baked goods rise, but they work differently. Baking soda is pure sodium bicarbonate, so it needs an acid in the recipe, like buttermilk, yogurt, honey or brown sugar, to start fizzing. Baking powder already contains its own acid, which means it only needs moisture and heat. That is why cookie recipes with brown sugar lean on soda and plain vanilla cakes often use powder. If you are ever stuck, you can make a rough substitute for one teaspoon of powder by mixing a quarter teaspoon of soda with half a teaspoon of cream of tartar. Check the dates on both tins too, since old leaveners go flat and quietly ruin a bake.",
     "baking soda versus baking powder", "powder"),
    ("how do i get better at remembering what i read",
     "Most of the forgetting happens because the reading was passive, so the fix is to make your brain do a little work afterward. When you finish a chapter, close the book and write down three things you remember in your own words before you look anything up. Then check what you missed. Doing that the next day, and again a week later, matters more than rereading ever will. It also helps to read with one question in mind, such as what the author wants me to believe, because a question gives the details somewhere to land. Keep the notes short, or you will end up with a second book to forget.",
     "remembering what you read", "remembering"),
    ("how do tides work",
     "Tides come mostly from the moon's gravity pulling on the ocean. The water on the side of Earth facing the moon is pulled into a bulge, and a second bulge forms on the far side because the whole planet is also being tugged. As Earth rotates, a coastline passes through both bulges, which is why most places get two high tides and two low tides a day. The sun adds its own, smaller pull. When the sun and moon line up, at new and full moon, you get the biggest swings, called spring tides, and when they are at right angles the swings are gentler. Local coastline shape then changes everything, which is why some bays have tides twice as tall as others.",
     "how tides work", "tides"),
    ("is it worth buying a cast iron pan",
     "For most home cooks, yes. A cast iron pan is cheap for what it does, it holds heat evenly, and it goes from the stove straight into the oven, which makes it great for searing a steak, baking cornbread or crisping potatoes. It lasts decades if you wipe it dry after washing and rub on a thin coat of oil. The trade-offs are weight, a slower heat-up, and the fact that acidic sauces like tomato can strip the seasoning if they simmer for a long time. If you mostly cook eggs and delicate fish, a nonstick pan will be easier. Many people end up owning both and reaching for the cast iron more often than they expected.",
     "buying a cast iron pan", "cast"),
    ("explain compound interest like i'm new to it",
     "Compound interest means you earn interest on your interest, not just on the money you put in. Say you put $1,000 in an account paying 5% a year. After the first year you have $1,050. In the second year the 5% is applied to $1,050 rather than $1,000, so you earn $52.50 and end up with $1,102.50. The difference looks tiny at first, but it keeps stacking, and after 30 years that same $1,000 grows to about $4,300 without you adding anything. The lesson most people take from it is to start early, since time does more of the work than the size of the deposit.",
     "compound interest", "compound"),
    ("what should i do on a rainy sunday with nothing planned",
     "A rainy Sunday is a good excuse for a slow one. You could start with a long breakfast, something like pancakes or eggs on toast, and then pick one small project that has been waiting around, such as sorting a drawer or finally fixing the wobbly chair. In the afternoon, a movie you have been meaning to watch or a few chapters of a book fits the weather nicely, ideally under a blanket with tea. If you need to get out, a cafe, a bookshop or a museum works well when it is wet. Finish with a simple dinner, maybe a soup, and an early night so Monday does not feel so abrupt.",
     "a rainy sunday", "sunday"),
    ("how do i politely end a conversation that won't stop",
     "The kindest way is to be warm and clear at the same moment. Wait for a natural pause, then say something like, I have really enjoyed this, and I need to get going, but let's pick it up next time. Naming the thing you enjoyed makes it land as a compliment instead of a brush-off. If the person keeps going, you can stand up or shift your body toward the door while you say it, since people pick up on movement. In a meeting or on a call, offering a concrete next step works too, such as I'll send you that link this afternoon. Most people are relieved when someone else makes the exit easy.",
     "ending a long conversation politely", "conversation"),
    ("why is my sourdough so dense",
     "Dense sourdough usually comes from one of three things. The first is a starter that is not active enough, so check that it doubles within about six hours of feeding before you bake with it. The second is underproofing, where the dough has not had enough time to rise, which is common in a cool kitchen. A good test is to poke it gently, and if the dent springs back slowly rather than instantly, it is ready. The third is weak shaping, which leaves the loaf without the tension it needs to hold its height. Try a longer bulk rise, a hotter preheated pot, and a wetter dough, but change one thing at a time so you can tell what helped.",
     "dense sourdough", "dense"),
    ("how often should i water a succulent",
     "Less often than you would think. Most succulents like to dry out completely between waterings, so the soil should be bone dry a couple of inches down before you give them a thorough soak. In a bright room that is usually every two weeks in summer and every three to four weeks in winter, when growth slows. Water until it runs out of the drainage hole, then empty the saucer so the roots do not sit wet. Signs of too much water are soft, translucent or yellowing leaves, while a thirsty plant shows wrinkled, deflated ones. If you are ever unsure, wait a few more days, because underwatering is far easier to fix than rot.",
     "watering a succulent", "succulent"),
    ("can you explain how vaccines train the immune system",
     "A vaccine shows your immune system a harmless piece or imitation of a germ so it can practice before the real thing arrives. That might be a weakened virus, a protein from its surface, or instructions that make your own cells produce that protein briefly. Your body responds by making antibodies and, importantly, memory cells that stay around for months or years. If you meet the real germ later, those memory cells recognize it quickly and respond within hours, rather than the days it would take to start from nothing. Often that means you never get sick at all, or you get a much milder illness. Some vaccines need boosters, because the memory fades over time.",
     "how vaccines work", "vaccines"),
    ("tips for a first time at a hot yoga class",
     "Hydrate well for the whole day beforehand, not just in the hour before the class. Eat something light two or three hours earlier, since a full stomach and a hot room are an unpleasant mix. Wear fabric that breathes and lies flat, and bring a large towel to lay over your mat, because it gets slippery. Arrive a little early to tell the teacher it is your first time and to find a spot near the door, where it is slightly cooler. Rest in child's pose whenever you feel dizzy, and do not worry about keeping up. The heat makes everyone feel a bit wobbly in the first few sessions, and it does get easier.",
     "a first hot yoga class", "yoga"),
    ("what makes a good thank you note",
     "A good thank you note is specific and short. Start with the thing itself, such as thank you for the scarf, and then say one concrete thing about it, like how you wore it on the first cold morning and got three compliments. That small detail tells the person you really noticed what they did. If the gift was a favor or someone's time, say what difference it made. Keep it to a few sentences, write it by hand if you can, and send it within a week or two. Skip the apology for being late, just send it. People remember a warm, specific note much longer than a long one.",
     "writing a thank you note", "thank"),
    ("how do airplanes stay up",
     "A wing is shaped and tilted so that as it moves forward, it pushes air downward. The air pushes back up on the wing in response, and that upward force is lift. The curved top surface also speeds the airflow and lowers the pressure above the wing, which adds to the effect. Lift grows with speed, which is why a plane needs a long run down the runway before it can leave the ground. Once it is up, the engines only have to overcome drag, and the wings do the rest. Pilots adjust flaps on the wings during takeoff and landing, which change the wing's shape so it can make enough lift at lower speeds.",
     "how airplanes stay up", "airplanes"),
    ("how do i stop procrastinating on a big project",
     "Big projects stall because the first step feels shapeless, so make it very small and very specific. Instead of work on the report, try open the document and write three bullet points for the introduction. Then set a timer for 15 minutes and promise yourself only that. Starting is the hard part, and once you are in it, momentum often carries you further. It also helps to decide in advance when and where you will work, and to put your phone out of sight. When you finish a session, jot down the next step so tomorrow's start is easy. Treat a slow day as information, not failure, and just do the next small thing.",
     "procrastinating on a big project", "procrastinating"),
    ("what's a good way to learn the guitar chords without getting frustrated",
     "Start with just four chords, G, C, D and E minor, because they unlock hundreds of songs. Practice switching between two of them for a minute at a time, slowly, and keep your fingers close to the strings as you move. Your fingertips will hurt for the first couple of weeks, so short sessions of ten or fifteen minutes spread through the day work better than one long one. A cheap clip-on tuner keeps everything in tune, which matters more than people expect, because out-of-tune strings sound wrong even when your fingers are right. Pick one simple song you love and learn it all the way through, since that gives the practice a reward.",
     "learning guitar chords", "guitar"),
    ("how can i make my small apartment feel bigger",
     "Light and clear surfaces do most of the work. Pull curtains fully open in the day, choose sheer ones if you need privacy, and place a mirror across from the window so it bounces light around the room. Keep furniture on legs so you can see the floor underneath, and keep large pieces against the walls. Use one or two colors throughout, with lighter shades on the walls, which blurs the edges of the room. Vertical storage, like tall shelves, draws the eye up and frees the floor. Finally, edit what sits on open surfaces, since three pretty objects look calm, while twelve look like clutter.",
     "making a small apartment feel bigger", "apartment"),
    ("why do we get goosebumps",
     "Goosebumps are a leftover reflex from our furry ancestors. When you feel cold, tiny muscles at the base of each hair contract and make the hair stand up. On an animal with thick fur, that traps a layer of warm air against the skin. We have lost most of our fur, so now it only produces bumps. The same reflex fires with strong emotion, like hearing a beautiful piece of music or feeling afraid, because the same nervous system that handles fight or flight also controls those muscles. In furry animals, raised hair also makes them look larger to a threat, which is the other half of why the reflex stuck around.",
     "why we get goosebumps", "goosebumps"),
    ("how long should i let a steak rest",
     "Rest it for about five to ten minutes for a typical steak an inch or so thick, a little longer for a thick cut or a roast. While it sits, the juices that were pushed toward the center during cooking spread back through the meat. If you cut right away, much of that liquid ends up on the board instead of in your mouth. Rest it on a warm plate or board, loosely covered with foil if the kitchen is cold, and do not wrap it tightly, which steams the crust you worked to build. The meat also keeps cooking a little as it rests, so pull it a few degrees under your target.",
     "resting a steak", "steak"),
    ("what's a decent way to learn a language in the evenings after work",
     "Short and consistent beats long and occasional. Twenty to thirty minutes a night, split between listening, a bit of vocabulary and speaking out loud, does more than a weekend binge. Pick content you actually enjoy, like a podcast or a show with subtitles in the target language, because enjoyment is what keeps you coming back after a long day. Learn the 500 most common words first, since they cover most everyday speech. Try to speak from the first week, even if it is just describing your dinner to yourself. A weekly chat with a tutor or language partner is the best accelerator, because it forces you to use what you have learned.",
     "learning a language in the evenings", "language"),
    ("explain what an index fund is",
     "An index fund is a single investment that holds a small slice of every company in a particular list, called an index, such as the 500 largest US companies. Instead of someone picking winners, the fund simply copies the list, which keeps its fees very low. When you buy one share of the fund, you own a tiny piece of hundreds of businesses at once, so no single company can sink your savings. Over long periods, low fees and broad ownership have beaten most actively managed funds. The main caveat is that it still moves with the market, so it can fall sharply in a bad year, which is why it suits money you will not need for a long while.",
     "index funds", "index"),
    ("how do i get red wine out of a carpet",
     "Act quickly and blot, never rub, using a clean white cloth or paper towel to soak up as much as you can. Then sprinkle a little cold water on the spot and keep blotting, which dilutes the stain. Mix a tablespoon of dish soap with a tablespoon of white vinegar and two cups of warm water, sponge it on, and blot again until the color stops transferring. If a pink shadow remains, a paste of baking soda and water left for an hour and then vacuumed can lift it. Avoid hot water and strong scrubbing, which set the dye. For an old stain, a carpet cleaner from the hardware store is worth trying.",
     "removing red wine from a carpet", "carpet"),
    ("what's the best way to prepare for a long flight with a layover",
     "Treat the layover as part of the plan. Check the connection time, and if it is under 90 minutes at a big airport, keep your boarding pass and the next gate on your phone so you can walk straight there. Carry a small bag with the essentials, such as a charger, snacks, a water bottle you fill after security, a light jacket and any medication, because checked bags can run late. Choose an aisle seat if you like to stretch, and set your watch to the destination time as soon as you board to start shifting your body clock. A few minutes of walking each hour helps your legs and your mood on the second leg.",
     "preparing for a long flight", "flight"),
    ("how do you know when an avocado is ripe",
     "Press gently in the palm of your hand, not with your fingertips, which leave bruises. A ripe avocado gives slightly under steady pressure, a bit like a ripe peach, while a hard one feels like a rock. Another trick is to flick off the little stem cap. If it comes away easily and the spot underneath is green, the fruit is ready, but if it is brown, the avocado is overripe, and if it will not budge, it needs more time. A dark skin is a clue for some varieties like Hass, but it is not reliable alone. To speed up a hard one, put it in a paper bag with a banana.",
     "ripe avocados", "avocado"),
    ("what's the story with daylight saving time",
     "Daylight saving time moves clocks forward an hour in spring and back in autumn, so that evenings stay lighter during the summer months. It was adopted widely during the world wars to save fuel, and people have argued about it ever since. Supporters like the extra evening light for outdoor activity and shopping, while critics point to the disruption of sleep in the days after the change, with studies linking it to a short rise in accidents and heart problems. Many places have debated staying on permanent standard time or permanent summer time, but changing the rules takes legislation, so for now the clocks keep switching in most of the US and Europe.",
     "daylight saving time", "daylight"),
    ("how do i make my commute less miserable",
     "Give the commute a purpose so it stops feeling like lost time. If you take transit, treat it as a reading slot or a podcast hour, and save a favorite series just for that ride. If you drive, a good audiobook or a call to a friend can turn the same traffic into something you look forward to. Leaving ten minutes earlier or later can change the traffic dramatically, so try a few start times for a week and note which feels best. Keep a small kit in your bag, with water, a snack and headphones, so delays do not wreck your mood. And if you can work from home even one day, that tends to help more than any trick.",
     "making a commute less miserable", "commute"),
    ("is it better to run in the morning or evening",
     "The best time is whichever one you will actually do consistently. That said, each has its perks. Morning runs tend to be easier to protect from a busy day, they leave you feeling accomplished, and the air is often cooler and cleaner. Evening runs are when your body is warmest and your muscles looser, so many people feel stronger and faster, and a run can be a good way to let go of the day. Running too close to bedtime can leave some people wired, so try finishing a couple of hours before sleep. Try each for two weeks and see which suits your schedule and energy.",
     "running in the morning or evening", "running"),
    ("what is the difference between a virus and a bacteria",
     "Bacteria are living single-celled organisms that can reproduce on their own, and most of them are harmless or even helpful, like the ones in your gut. A few cause illnesses such as strep throat or urinary infections, and those can be treated with antibiotics. Viruses are much smaller and are not cells at all. They are bits of genetic material in a protein coat that can only multiply by getting inside your cells and borrowing their machinery. Colds, flu and measles are viral, and antibiotics do nothing against them. The best protections against viruses are vaccines, rest and hygiene, and sometimes antiviral drugs for specific infections.",
     "viruses versus bacteria", "bacteria"),
    ("how can i be a better listener",
     "Good listening mostly comes down to patience and curiosity. Put your phone away, face the person, and let them finish a thought without planning your reply while they talk. When they stop, you can repeat back the heart of what you heard, such as, it sounds like the move has been exhausting, which lets them know you followed and gives them a chance to correct you. Ask one open follow-up question rather than offering advice straight away, since many people just want to be understood first. Comfortable silence is part of it, too. If you notice you keep waiting for your turn to speak, take a breath and ask one more question instead.",
     "being a better listener", "listener"),
    ("what's the difference between weather and climate",
     "Weather is what the atmosphere is doing right now or this week, like a rainy Tuesday or a heat wave in July. Climate is the long-term pattern for a place, usually averaged over thirty years or more, such as the fact that Seattle is wet in winter and fairly dry in summer. A handy way to remember it is that climate is what you expect and weather is what you get. One cold winter does not disprove a warming climate, in the same way that one unusually cold day does not make summer cold. Scientists track climate through averages, extremes and trends across many years and many places.",
     "weather versus climate", "climate"),
    ("how should i store fresh herbs so they last",
     "Treat soft herbs like cilantro, parsley and dill as a bouquet. Trim the stems, stand them in a jar with an inch of water, cover the leaves loosely with a bag, and keep them in the fridge, changing the water every couple of days. Basil is the exception, since the cold blackens it, so keep it on the counter in water like flowers. Woody herbs such as rosemary and thyme prefer being wrapped in a slightly damp paper towel inside a container in the fridge. If you cannot use them in time, chop and freeze them in olive oil in an ice cube tray, which gives you ready-made flavor for soups and pan sauces.",
     "storing fresh herbs", "herbs"),
    ("why do cats purr",
     "Cats purr when they are content, and that is the part everyone knows, but they also purr when they are nervous, in pain or recovering. The sound comes from rapid twitching of the muscles in the voice box, which opens and closes the airway as the cat breathes in and out. Kittens purr to tell their mother they are fine, and mothers purr back to soothe them. Some researchers think the low vibration may even help bones and tissue heal, since it falls in a range linked to repair. If your cat purrs while hiding or not eating, it may be self-soothing, so watch the whole picture rather than the sound alone.",
     "why cats purr", "purr"),
    ("how do i choose a good watermelon",
     "Look for one that feels heavy for its size, which means it is full of juice. Turn it over and find the field spot, the patch where it rested on the ground. A creamy yellow patch is a good sign, while a white or pale green one suggests it was picked early. The rind should be dull rather than shiny, and the shape should be fairly even. Give it a tap, and a ripe one makes a deep, hollow sound, a bit like a low drum. Skip any with soft spots, cuts or dents. If it is a cut half, look for deep red flesh without white streaks or a mealy texture.",
     "choosing a watermelon", "watermelon"),
    ("what are some ways to save money on groceries without eating worse",
     "Planning is the biggest lever. Check what is already in your fridge, write a short list of meals for the week, and shop with it, which cuts impulse buys and food waste. Buy staples like rice, oats, beans and frozen vegetables in larger sizes, since they are cheap and keep for a long time. Store brands are often made in the same factories as name brands for much less. Check unit prices on the shelf tag, because the biggest package is not always the best value. Finally, cook once and eat twice, turning one night's roast chicken into tomorrow's tacos or soup. Many people save a fifth of their bill with just those habits.",
     "saving money on groceries", "groceries"),
    ("how does noise cancelling work",
     "Noise cancelling headphones listen to the sound around you with small microphones, then play back a mirror image of that sound. When a sound wave meets another wave that is exactly opposite, the two cancel each other out, a bit like adding plus one and minus one. It works best on steady, low sounds, like the hum of an airplane engine, an air conditioner or a train, because they are easy to predict. Sudden sounds such as a voice or a door slam are harder, so you still hear some of those. Better headphones also seal around your ear, which blocks the higher sounds the electronics cannot handle.",
     "noise cancelling headphones", "headphones"),
    ("what's a good routine for winding down before bed",
     "Pick a consistent time to start winding down, about an hour before you want to sleep. Dim the lights, put your phone out of reach and do something calm, such as a warm shower, light stretching or a few pages of a paperback. Keep the room cool and dark. If your mind keeps racing, write a short list of tomorrow's tasks on paper, which moves them out of your head. Avoid caffeine after early afternoon and heavy meals close to bedtime. It can take a couple of weeks for the routine to really work, because the body learns the pattern, so be patient and keep the order the same each night.",
     "winding down before bed", "winding"),
    ("what makes a good first impression at a new job",
     "People tend to remember warmth and reliability most. Arrive a little early on the first few days, learn names and write them down, and ask questions, since new colleagues like being asked. Listen more than you talk in the first week, and take notes so you do not ask the same thing twice. Offer help with small things, such as covering a quick task or sharing a useful link, which builds goodwill quickly. Follow through on anything you promise, even something minor like sending an article. Dress one notch smarter than the team's usual style at first, then adjust. A friendly, curious attitude carries more weight than trying to look impressive.",
     "making a good impression at a new job", "impression"),
    ("how do i deal with a noisy neighbor",
     "Start with a friendly, direct conversation, because many people have no idea how far sound carries. Pick a calm moment rather than the middle of the noise, and describe the specific problem, such as the bass through the wall after 11, instead of accusing them. Offer a simple compromise, like keeping it down after a certain hour. If that does not work, a polite written note comes next, then your landlord or building manager, who often has rules about quiet hours. Keep a short log of dates and times in case you need to escalate. In the meantime, white noise or earplugs can take the edge off.",
     "dealing with a noisy neighbor", "neighbor"),
    ("what should i know before adopting a rescue dog",
     "Expect an adjustment period. Many rescue dogs need two or three weeks to settle, and a few months to show their real personality, so keep the first days quiet and predictable, with a bed in a calm corner and a steady routine for meals and walks. Ask the shelter everything they know about its history, energy and how it behaves around other animals and children. Budget for the first vet visit, vaccines, a microchip and some training classes, which help bonding as much as obedience. Accidents and chewing are normal at first, so dog-proof your space. Patience pays off, and the dogs that start out nervous often become the most loyal.",
     "adopting a rescue dog", "rescue"),
    ("how do solar panels actually make electricity",
     "Each panel is a grid of silicon cells. When sunlight hits a cell, its energy knocks electrons loose from the silicon atoms, and the cell is built with an electric field that pushes those free electrons in one direction. That flow is direct current. An inverter then converts it to the alternating current that your outlets use. Output depends on sunlight, so panels make the most power on clear days around midday and a smaller amount under cloud. Any surplus can charge a battery or be sent to the grid, depending on your setup. Panels slowly lose a small percentage of output each year, but most keep working well for twenty-five years or more.",
     "how solar panels work", "solar"),
    ("how do i start journaling without it feeling forced",
     "Lower the bar so far that you cannot fail. Try writing just three lines at the end of the day, such as one thing that happened, one thing you felt and one thing you want tomorrow. There is no need to write well or fill a page. If a blank page feels intimidating, use a prompt, like what is taking up the most space in my head right now. Keep the notebook and a pen by your bed so there is no setup. Some people like a morning page instead, which clears the mental noise before the day begins. After a few weeks, you will probably find yourself writing more without trying.",
     "starting a journaling habit", "journaling"),
    ("what's the best way to cool down a hot bedroom in summer",
     "Block the heat before it comes in. Keep blinds or blackout curtains closed on sunny windows during the day, then open the windows at night once the outdoor air is cooler than the room. Place a box fan in one window facing out and open another window on the opposite side, which pulls cooler air through. A bowl of ice in front of a regular fan adds a little extra chill. Lighter, breathable bedding, such as cotton or linen, helps, and so does a cool shower before bed. Avoid using the oven in the evening. If it is still too warm, sleeping on the lowest floor of the house makes a real difference.",
     "cooling a hot bedroom", "bedroom"),
]


# Exchange 1 of a long chat: the person states facts, and the long reply builds on them. Each seed carries its
# own questions (asked many exchanges later) with the final that states the fact from the conversation.
# (user, reply, topic, keyword, [(prompt, final, must)])
USERFACT = [
    ("i'm planning a surprise 40th for my brother Tomas on October 18th at the Harbor Grill, and he's obsessed with sailing",
     "A surprise party for a sailor has a lot of room to play. For the Harbor Grill, you could ask whether the host will keep the guest list quiet, and have people arrive twenty minutes before Tomas so he walks in on a full room. A nautical theme is easy to pull off without being corny: navy and white tablecloths, a few rope-wrapped centerpieces and a chart of his favorite sailing spots on the wall. Since October 18th falls in the cooler part of the year, a toast on the deck at sunset could be a lovely moment if the restaurant has one. A short slideshow of his sailing photos would give people something to laugh about during dinner.",
     "a surprise party", "surprise",
     [("which date was the party again", "You said the party is on October 18th, at the Harbor Grill.", ["October 18th"]),
      ("and the guest of honor, what was his name again", "Your brother is Tomas, turning 40, and the whole thing is built around his love of sailing.", ["Tomas"]),
      ("where was the surprise dinner going to be", "The surprise dinner is at the Harbor Grill, on October 18th.||Tomas arrives after the guests.", ["Harbor Grill"])]),
    ("we're moving to Denver on March 3rd and my partner Ines will drive the truck while i fly out with the dog, Pepper",
     "Moving day is easier when the jobs are split clearly, and your plan already does that. With Ines driving the truck, it is worth agreeing on a daily mileage limit and a fixed time to check in, so neither of you worries along the way. For the flight with Pepper, check the airline's pet rules early, since many limit how many animals fit in the cabin and want a carrier that fits under the seat. Take a recent vet certificate, a familiar blanket and a few treats. Landing in Denver on March 3rd, you could pick up a key and sit with Pepper in the empty home before the truck arrives.",
     "a move to Denver", "denver",
     [("who was driving the truck again", "Ines is driving the truck, while you fly out with Pepper.", ["Ines"]),
      ("what's the dog called, i forgot i told you", "The dog is called Pepper, and Pepper flies with you.", ["Pepper"]),
      ("what day is the move", "March 3rd is the day you're moving to Denver.", ["March 3rd"])]),
    ("my sister Annika is getting married in Lisbon on June 21st and i'm giving the toast, so i need to start soon",
     "That is a lovely thing to be asked, and starting early gives you time to make it good. A toast for a sister works best when it is short and has one real story at its center, rather than a list of facts. Think of a moment when you saw who Annika really is, maybe a childhood adventure or a time she showed up for you, and let that carry the speech. Aim for about two minutes, finish by raising a glass and wishing the couple something specific, and practice it out loud a few times. In Lisbon on June 21st, the evening light will be long, so you will have a warm crowd.",
     "a wedding toast", "toast",
     [("which city is the wedding in", "The wedding is in Lisbon, on June 21st.||You are giving the toast there.", ["Lisbon"]),
      ("who's the bride in that toast i'm writing", "That's your sister Annika, getting married in Lisbon on June 21st.", ["Annika"]),
      ("remind me the wedding date", "It is on June 21st, in Lisbon.||Your toast fits somewhere in the evening.", ["June 21st"])]),
    ("i just started at a new job at Fernbrook Labs, my manager is Dana Whitfield and my first day is Monday the 9th",
     "Congratulations on the new role. A first week goes best when you decide a few things in advance. For Monday the 9th, aim to arrive about ten minutes early, bring a notebook and plan to ask Dana Whitfield what a good first month looks like, since managers love that question. Learn names as you go and write them down with a detail beside each. Do not worry about proving yourself in week one. Mostly you are there to listen, to find out how things really work and to say yes to lunch invitations. By Friday, you will already feel like you know where the coffee is kept at Fernbrook Labs.",
     "a new job", "job",
     [("what was the manager's name again", "Your manager is Dana Whitfield.||A good question for her is what the first month should look like.", ["Dana Whitfield"]),
      ("what was the name of the company again", "You're joining Fernbrook Labs, starting Monday the 9th.", ["Fernbrook Labs"]),
      ("first day, what was it", "Your first day is Monday the 9th, at Fernbrook Labs.", ["Monday the 9th"])]),
    ("i'm training for the Cedar Ridge half marathon on April 12th and my goal time is 1 hour 55",
     "A goal of 1:55 for a half marathon is ambitious and realistic if you build it steadily. That works out to about 8:44 per mile, so most of your training can be at an easier effort, with one weekly session at or just under that pace. Add a long run that grows by about a mile a week and lets you practice fueling, and take an easy week every fourth week. In the final fortnight before April 12th, reduce the volume but keep a few short, sharp efforts so your legs stay lively. Race day is about trusting the work, so start a touch slower than goal pace and finish strongly in the last three miles.",
     "half marathon training", "marathon",
     [("what was the target time for the race", "Your goal is 1 hour 55 for the Cedar Ridge half marathon.", ["1 hour 55"]),
      ("when's the race again", "The Cedar Ridge half marathon is on April 12th.", ["April 12th"])]),
    ("my landlord Mr. Ostrow says the heater gets replaced Wednesday between 9 and noon, and somebody has to be home",
     "That window is manageable if you plan around it. Since someone needs to be home between 9 and noon on Wednesday, it helps to clear a path to the heater beforehand and move anything breakable out of the way. Ask Mr. Ostrow if the installer will turn off the water or power during the work, so you can plan showers and charging. Take a quick photo of the old unit and the new one for your records, and make sure you get the model number and warranty details afterward. If the visit runs long, keep a book handy. Replacements like this often take a couple of hours, but the finish is rarely on schedule.",
     "a heater replacement", "heater",
     [("when's the heater getting replaced, what was the window", "The heater is replaced on Wednesday between 9 and noon, and someone needs to be home.", ["Wednesday", "9 and noon"]),
      ("what's the landlord called again", "The landlord is Mr. Ostrow.||He is sending someone Wednesday to replace the heater.", ["Mr. Ostrow"])]),
    ("i've got a dentist appointment on Thursday at 3:45 with Dr. Lindqvist and i'm honestly kind of dreading it",
     "Dreading a dentist visit is very common, and a few small things can make it easier. When you arrive for your Thursday appointment, tell the staff and Dr. Lindqvist that you are nervous, which gets you a slower pace and clearer warnings before anything uncomfortable. Agree a hand signal for a pause. Bring headphones and play something calm or funny, and breathe out slowly when you feel tense, because a long exhale settles the body. Try not to schedule anything stressful right after, and plan a little treat, like a warm drink. Most of the worry is anticipation, and the real visit is usually shorter and gentler than your mind has been predicting.",
     "a dentist appointment", "dentist",
     [("what time was the dentist visit", "The appointment with Dr. Lindqvist is on Thursday at 3:45.", ["3:45"]),
      ("who's the dentist i'm seeing", "You're seeing Dr. Lindqvist on Thursday at 3:45.", ["Dr. Lindqvist"])]),
    ("our group project is a podcast about city falcons, and my part is the interview with a woman named Rosalind who watches a nest on an office tower",
     "That is a lovely subject for audio, because a falcon nest gives you drama, patience and story all at once. For the interview with Rosalind, start with how she got into it, then move to the moments only she can describe, such as the first chick she saw fledge or a dive she still remembers. Record near the tower if she is comfortable, since the city sound around her will give your listeners a real sense of place. Prepare eight or ten questions, but leave room to follow tangents, because the best lines often come unplanned. Ask her to explain one surprising fact about falcons, and let her take her time.",
     "a podcast on city falcons", "podcast",
     [("who was the person i'm interviewing for the podcast", "You're interviewing Rosalind, who watches a falcon nest on an office tower.", ["Rosalind"]),
      ("what's the podcast about, the topic i mean", "It's a podcast about city falcons, and your part is the interview with Rosalind.", ["city falcons"])]),
    ("i'm learning to bake and want to make my grandmother's Sicilian lemon cake for her 80th birthday on the 14th",
     "What a thoughtful gift, and a cake with that history will carry a lot of meaning. Because the 14th is an important date, bake a practice version a week before so you can adjust the timing and sweetness in your own oven. Use fresh lemons for both the zest and the juice, rub the zest into the sugar to release the oils, and do not overmix the batter, which toughens the crumb. Let the cake cool completely before glazing. If you can, ask your grandmother about the original recipe, since the stories she tells while you ask will become part of the present too.",
     "a lemon cake for a birthday", "lemon",
     [("which cake was it, the one for her birthday", "A Sicilian lemon cake, your grandmother's recipe, for her 80th birthday.", ["Sicilian lemon cake"]),
      ("which birthday is it, the number", "It's her 80th birthday, on the 14th.||The lemon cake has a big occasion to live up to.", ["80th"])]),
    ("my flight to Osaka leaves Saturday at 6:20 AM from gate B14 and i haven't packed at all",
     "A 6:20 AM departure means the real deadline is Friday evening, so pack tonight if you can. Start with a list sorted by category, such as documents, clothing, toiletries and electronics, and lay everything out on the bed before you put it in the bag. For Osaka, check the weather for next week, and pack layers that you can mix and match. Keep your passport, charger and anything fragile in your carry-on, along with one change of clothes in case your bag is delayed. Set two alarms, arrange a ride in advance, and aim to be at the airport about two and a half hours early, since gate B14 may be a long walk after security.",
     "a flight to Osaka", "osaka",
     [("what time was the flight leaving again", "Your flight leaves Saturday at 6:20 AM.||That makes Friday evening the real packing deadline.", ["6:20 AM"]),
      ("which gate was it", "Your flight leaves from gate B14.||It could be a long walk after security.", ["B14"])]),
    ("i adopted a rescue whippet named Biscuit last week and he refuses to go up the stairs",
     "That is a very common quirk in a rescue dog, and it gets better with patience. Many rescue whippets have never lived with stairs, and their long legs and light frames make the first steps feel strange. Start by letting Biscuit sniff the bottom step, then reward any interest with a treat. Place a treat on the first step and then the second, and move slowly. Some owners guide the back legs with a hand under the hips for the first few tries. Keep each session short, stay relaxed, and avoid pulling on the lead. Within a couple of weeks, most dogs learn it, and then many of them treat the staircase as a favorite lounging spot.",
     "a rescue whippet", "whippet",
     [("what's the whippet's name", "His name is Biscuit, the rescue whippet you adopted last week.", ["Biscuit"]),
      ("what was the problem he has with the house", "Biscuit refuses to go up the stairs.||That is a common quirk for a rescue whippet.", ["stairs"])]),
    ("the book club meets Tuesday the 5th at Marguerite's place to talk about The Remains of the Day, and i haven't finished it",
     "You still have time, and this novel rewards a steady pace more than a sprint. If you have around a hundred pages left, try reading thirty or forty pages a night, preferably in a quiet block before bed. It helps to focus on how the butler Stevens tells his own story, since the gap between what he says and what he feels is what the group will probably debate. Jot a few notes on moments that struck you, so you have something to say even if you do not finish in time. Nobody at a book club minds an honest, partial reading, and a thoughtful question is often more useful than a perfect summary.",
     "a book club meeting", "club",
     [("which book are we discussing at the club", "The Remains of the Day, at Marguerite's place on Tuesday the 5th.", ["The Remains of the Day"]),
      ("whose place is the meeting at", "It's at Marguerite's place, on Tuesday the 5th.", ["Marguerite"])]),
    ("my car's registration expires on the 30th and the DMV appointment i found is Monday at 8:15",
     "Booking that appointment early is smart, because a Monday morning slot at 8:15 usually means shorter waits. Before you go, collect your current registration card, proof of insurance, your license and any smog or inspection paperwork your state requires. Many renewals can also be done online, so check whether yours qualifies, because that could save the visit entirely. If you do attend, bring a pen, a payment method they accept and a book for the line. After you renew, keep the new sticker or card in the car and set a reminder a month before next year's expiry, so the 30th does not sneak up again.",
     "a car registration", "registration",
     [("when does the registration run out", "The registration expires on the 30th.||The Monday appointment comes in just ahead of it.", ["30th"]),
      ("what time is the DMV slot", "Your DMV appointment is Monday at 8:15.||It is a good early slot for a shorter wait.", ["8:15"])]),
    ("i'm hosting thanksgiving for 11 people this year and my cousin Wen is bringing the pies",
     "Eleven is a good size, big enough to feel festive and small enough to manage on your own. The key is to work backward from the meal. Order or thaw the turkey early, make cranberry sauce and stuffing components a day ahead, and set the table the night before. With Wen bringing the pies, you can cross dessert off your list and spend that oven space on side dishes. Ask a couple of other guests to bring drinks or a vegetable, since people usually want to contribute. Keep a printed timeline on the fridge, with oven times for each dish, so the afternoon runs smoothly and you actually get to sit down.",
     "hosting thanksgiving", "thanksgiving",
     [("how many people are coming to the dinner", "You said 11 people are coming for Thanksgiving.", ["11"]),
      ("who's bringing dessert", "Your cousin Wen is bringing the pies.||That frees the oven for your side dishes.", ["Wen"])]),
    ("i booked a cabin near Lake Tahoe for the weekend of February 7th, there are four of us and it has a hot tub",
     "A winter weekend like that is easy to look forward to. In early February, check the road conditions and chain rules before you drive up, and pack a set of chains or confirm your tires are suitable. With four of you, it is worth deciding on meals in advance, since a grocery run once you arrive costs time and the cabin kitchen makes group cooking easy. The hot tub is wonderful after a day of snow, but keep the evening soaks to about twenty minutes at a time and drink plenty of water. Bring layers, slippers and a deck of cards, and you have the ingredients for a very good weekend of February 7th.",
     "a cabin weekend", "cabin",
     [("how many of us are going to the cabin", "There are four of you going.||The cabin has a hot tub for the evenings.", ["four"]),
      ("which lake was it near", "The cabin is near Lake Tahoe, for the weekend of February 7th.", ["Lake Tahoe"])]),
    ("my daughter Mira has her piano recital on Saturday the 22nd at 2 PM and she is playing a Chopin nocturne",
     "How exciting for Mira. A recital is a big moment at any age, and a Chopin nocturne asks for calm and expressive playing, which is a lot to hold in front of an audience. In the days before Saturday the 22nd, encourage her to play the piece through for friends or family at least a couple of times, so the nerves of an audience feel familiar. On the day, have her warm up with scales, eat something light before the 2 PM start and arrive with time to feel the piano. Afterward, tell her one specific thing you loved about the performance, such as a phrase that made you stop and listen.",
     "a piano recital", "recital",
     [("what time does the recital begin", "Mira's recital is on Saturday the 22nd at 2 PM.", ["2 PM"]),
      ("what's the piece she's playing", "She is playing a Chopin nocturne.||It asks for calm and expressive playing.", ["Chopin nocturne"])]),
    ("i'm trying to cut down on coffee, i usually have 4 cups a day and want to get to 1 by the end of the month",
     "A gradual cut is much kinder than stopping abruptly, which can bring headaches and a foggy afternoon. Going from 4 cups to 1 over a month works out to dropping roughly one cup every week. Start by dropping the afternoon cup first, since it is the one most likely to affect your sleep, then replace it with tea or a warm drink so the ritual stays. You can also blend half-decaf into a cup to soften the step. Drink water first thing in the morning, and keep an eye on how you feel. Many people find that their energy steadies after a week or two, once the cycle of peaks and dips fades.",
     "cutting down on coffee", "coffee",
     [("how many cups a day was that before", "You were having 4 cups a day, and you want to get down to 1 by the end of the month.", ["4 cups"]),
      ("what was the target number", "You want to get down to 1 cup a day by the end of the month.", ["by the end of the month"])]),
    ("my best friend Priya is visiting from Toronto the first week of May and i want to show her around Portland",
     "A first visit is a good chance to see your own city freshly. In the first week of May, Portland is usually mild and green, so plan some outdoor time, such as the Japanese Garden or a wander along the waterfront. Food carts and a bakery breakfast are easy ways to taste the city without much planning. Ask Priya what she likes, whether it is bookstores, coffee, hiking or music, and build two or three highlights around that, leaving room for a slow afternoon. Keep a rainy-day backup, since May can surprise you, and save one evening for just the two of you to talk and catch up.",
     "a friend's visit", "visit",
     [("where is she coming from again", "Priya is coming from Toronto, the first week of May.", ["Toronto"]),
      ("which friend is visiting", "Your best friend Priya, the first week of May.", ["Priya"])]),
]


# Exchange 1 of a long chat: the person asks, and the long reply states specifics (a number, a name, a place) in
# its first sentence and in later ones. The questions ask for one of them many exchanges later.
# (user, reply, topic, keyword, [(prompt, final, must)])
ASSTFACT = [
    ("how should i split a two day drive from chicago to nashville with a toddler",
     "I'd stop in Louisville, Kentucky for the night, which splits the trip into roughly five hours on day one and about three on day two. Toddlers do best with a drive broken into short stretches, so plan a stop every ninety minutes and make the first one a real playground rather than a gas station. A quiet hotel with a pool in Louisville gives your little one a chance to burn off energy before bed. Leave in the morning right after breakfast so the longer leg falls over the nap, and keep a small bag of new stickers and board books within reach. If the day gets rough, shorten the next leg and do not worry about the schedule.",
     "a drive to Nashville", "nashville",
     [("which town did you suggest for the overnight stop", "I suggested Louisville, Kentucky for the night, with about five hours of driving on day one and three on day two.", ["Louisville"]),
      ("how long did you say the first day's drive would be", "About five hours on the first day, then roughly three on the second.", ["five hours"])]),
    ("what's a good starter set of herbs for a sunny kitchen windowsill",
     "I'd start with basil, chives and mint, because all three grow quickly and forgive the occasional missed watering. Basil wants the most sun, so give it the spot closest to the glass, and pinch the top leaves often to keep it bushy. Chives are nearly unkillable and regrow after you snip them, which makes them a good confidence builder. Mint spreads aggressively, so keep it in its own pot, otherwise it will take over the others. Use pots with drainage holes and a saucer, and water when the top of the soil feels dry. Rosemary is worth adding later, but it dislikes soggy roots, so it prefers a slightly separate routine.",
     "windowsill herbs", "windowsill",
     [("which three herbs did you say to begin with", "Basil, chives and mint.||All three grow quickly and forgive a missed watering.", ["basil", "chives", "mint"]),
      ("what was the herb i should keep in its own pot", "Mint, since it spreads and takes over the other pots.", ["mint"])]),
    ("can you suggest a simple weeknight dinner for four that's cheap",
     "A big pot of chickpea and spinach curry with rice comes to about $2.50 a serving and takes 35 minutes. Brown a chopped onion in oil, add two tablespoons of curry paste, then a can of chopped tomatoes, two cans of drained chickpeas and a cup of water. Simmer for fifteen minutes, then stir in a few handfuls of spinach until it wilts. Serve over rice with a spoonful of yogurt on top. It reheats well, so leftovers make tomorrow's lunch, and you can add a diced sweet potato if you want it heartier. Squeeze lemon over it right at the end for brightness.",
     "a cheap weeknight dinner", "weeknight",
     [("what was the cheap dinner you suggested", "A chickpea and spinach curry with rice, about $2.50 a serving and ready in 35 minutes.", ["chickpea and spinach curry"]),
      ("how much per serving did you say it cost", "About $2.50 a serving.||It takes around 35 minutes.", ["$2.50"])]),
    ("what should i read before visiting japan, something not a guidebook",
     "Start with Pico Iyer's The Lady and the Monk, a quiet memoir about a year in Kyoto that helps you notice small things when you arrive. If you want something lighter, Haruki Murakami's short story collections are very approachable, and they give you a feel for modern Tokyo. For history, Donald Keene's essays are generous and clear without being heavy. I would also try Kamome Shirahama's manga Delicious in Dungeon only if you like fantasy, because it is playful and full of food. A single paperback in your bag is plenty. Read a few pages on the plane and you will arrive in a mood for the place.",
     "reading before visiting Japan", "japan",
     [("what was the memoir you mentioned about kyoto", "Pico Iyer's The Lady and the Monk, a quiet memoir about a year in Kyoto.", ["The Lady and the Monk"]),
      ("who was the author of that kyoto book", "The author was Pico Iyer.||It is a quiet memoir about a year in Kyoto.", ["Pico Iyer"])]),
    ("how many hours of sleep do teenagers need and how do i get my 15 year old to bed earlier",
     "Teenagers need about eight to ten hours a night, and most get far less. Their body clocks shift later during adolescence, so an 11 PM bedtime feels like early evening to a 15 year old. Rather than forcing a strict lights-out, try moving the bedtime back by fifteen minutes every few nights. Agree a screens-off time with them, perhaps 9:30, and charge phones outside the bedroom. Bright light in the morning helps reset the clock, so open the curtains right away. Explain the reasons rather than ordering, since teens respond better when they are part of the plan. A calm, consistent routine usually works within a couple of weeks.",
     "sleep for teenagers", "teenagers",
     [("how many hours of sleep did you say teens need", "Eight to ten hours a night.||Most teenagers get far less.", ["eight to ten hours"]),
      ("what time did you suggest for the screens to go off", "Around 9:30, with phones charging outside the bedroom.", ["9:30"])]),
    ("what's a realistic budget for a week in lisbon, not fancy",
     "Plan on about $1,400 for a week in Lisbon for one person, not counting the flight. A comfortable guesthouse or a small apartment runs roughly $90 a night, which comes to $630 for the week. Meals at neighborhood restaurants cost around $20 a day if you eat the lunch special, adding about $140. Transport is cheap: a 24 hour pass is about $7, so plan $50 or so for the week. Add around $150 for museums, a day trip to Sintra and a fado evening, and keep about $300 aside for coffee, snacks, souvenirs and surprises. The numbers shift in summer, when rooms cost much more.",
     "a budget for Lisbon", "budget",
     [("what was the nightly cost you gave for the guesthouse", "About $90 a night, which comes to $630 for the week.", ["$90"]),
      ("what total did you suggest for lisbon", "About $1,400 for a week in Lisbon for one person, not counting the flight.", ["$1,400"])]),
    ("how do i pick the right size ladder for painting a room with ten foot ceilings",
     "An 8 foot stepladder is the right choice for ten foot ceilings. The rule is that your reach, standing on the second-to-top step, should land you comfortably at the ceiling line, and an 8 footer gives roughly 12 feet of reach for an average adult. Look for a ladder rated Type 1A or higher, which holds at least 300 pounds, and check that it has a wide base and a paint tray shelf. Never stand on the top step or the one below it. Place the ladder on flat, dry ground, keep your belt buckle between the rails and move it rather than leaning to the side.",
     "a ladder for painting", "ladder",
     [("what ladder size did you recommend", "An 8 foot stepladder.||It gives roughly 12 feet of reach for an average adult.", ["8 foot"]),
      ("what rating should it have, you said a type", "Type 1A or higher, which holds at least 300 pounds.", ["Type 1A"])]),
    ("is there a way to make my laptop battery last longer",
     "Yes, the biggest change is to keep the charge between about 20 and 80 percent most of the time, since lithium cells age faster at the extremes. Many laptops have a setting that caps charging at 80 percent, which is worth switching on if you are plugged in at a desk all day. Lower the screen brightness, which is usually the largest drain, and close apps that run in the background. Avoid leaving the machine in a hot car, because heat does lasting damage. After two or three years, even good batteries lose capacity, and a replacement can be cheaper than a new laptop. A quick battery health check in your system settings tells you where yours stands.",
     "laptop battery life", "laptop",
     [("what range did you say to keep the charge in", "Between about 20 and 80 percent.||Lithium cells age faster at the extremes.", ["20 and 80"]),
      ("what was the biggest drain on the battery you mentioned", "The screen brightness, which is usually the largest drain.", ["brightness"])]),
    ("which questions should i ask when the interviewers say do you have any questions for us",
     "A strong first question is, what does success look like in this role after six months? It shows you are thinking about results, and the answer tells you how the team measures work. A second one is, what is the biggest challenge the team is facing right now, which invites an honest answer. You can also ask how they handle feedback, what a typical week looks like and what the person before you went on to do. Avoid asking about salary in a first interview unless they raise it. Choose three questions and write them down, so you look prepared, and listen carefully to the answers because they might lead to a better follow-up.",
     "interview questions to ask", "interview",
     [("what was the first question you suggested asking", "What does success look like in this role after six months?", ["six months"]),
      ("how many questions did you say to prepare", "Three, written down so you look prepared.||That also gives you something to follow up on.", ["three"])]),
    ("how long should i soak dried beans and is it worth it",
     "Soak them for eight to twelve hours in plenty of cold water, with about three inches of water above the beans, because they will swell to roughly double their size. It is worth doing for most varieties, since soaked beans cook faster, more evenly and are often easier to digest. If you forgot, use the quick method: cover with water, boil for two minutes, turn off the heat and let them sit for an hour. Discard the soaking water, rinse and cook in fresh water, adding salt once they begin to soften. Lentils and split peas do not need soaking at all, so you can skip it for those.",
     "soaking dried beans", "beans",
     [("how many hours did you say to soak them", "Eight to twelve hours in cold water.||Use about three inches of water above the beans.", ["eight to twelve hours"]),
      ("what was the quick method for the beans", "Boil for two minutes, turn off the heat and let them sit for an hour.", ["two minutes"])]),
    ("what's the best order to do a house deep clean so i don't redo things",
     "Work from the top of each room to the bottom and from the back of the house to the front. That means ceilings and light fixtures first, then walls, shelves, windows and furniture, and the floors last, so any dust falling from above lands on a surface you will clean later. Do the bathrooms and kitchen with the strongest products and let sprays sit for a few minutes while you move on. Save the vacuuming and mopping for the very end. Open a window as you go. If your home is big, tackle one room at a time and finish each completely, which gives a sense of progress and avoids a pile of half-done jobs.",
     "deep cleaning a house", "cleaning",
     [("what was the rule on the order of cleaning", "Work from the top of each room to the bottom and from the back of the house to the front.", ["top of each room to the bottom"]),
      ("what goes last in the clean", "The floors, vacuuming and mopping at the very end.", ["floors"])]),
    ("how do i figure out if i should buy or rent a place, i'm in my late twenties",
     "A common rule of thumb is to compare the cost of buying a home to renting something similar, and look for a price-to-rent ratio below about 15 to make buying attractive. Divide the home's price by a year of rent for a comparable place. If it comes out at 20 or more, renting is usually cheaper. Also consider how long you plan to stay, since transaction costs mean you generally need five years to break even. Factor in the down payment, property tax, insurance and repairs, which often add one to two percent of the home's price every year. Renting also gives you flexibility while your career and location are still changing.",
     "buying or renting", "renting",
     [("what ratio number did you mention for deciding", "A price-to-rent ratio below about 15 makes buying attractive, and 20 or more usually means renting is cheaper.", ["15"]),
      ("how many years to break even did you say", "Around five years, because of transaction costs.", ["five years"])]),
    ("what should be in a basic first aid kit for a family car",
     "Pack a compact kit with adhesive bandages in three sizes, sterile gauze pads, medical tape, antiseptic wipes, a pair of tweezers and small scissors. Add two elastic bandages, an instant cold pack, disposable gloves and a small bottle of saline for flushing eyes. Include any personal medication, a thermometer and a pack of pain relievers. A space blanket weighs almost nothing and can be a lifesaver in cold weather. Keep the kit in the cabin rather than the trunk, since heat in the trunk ruins some supplies. Check expiry dates twice a year, replace what you have used and keep a card with emergency numbers and allergies in the lid.",
     "a car first aid kit", "aid",
     [("where did you say to store the kit", "In the cabin rather than the trunk, since heat in the trunk can ruin some supplies.", ["cabin"]),
      ("how often should i check the expiry dates, you gave a number", "Check them twice a year.||Replace anything used or expired.", ["twice a year"])]),
    ("how can i improve my posture when i work at a desk all day",
     "Start with the setup, because it does most of the work. Place your screen so the top is at eye level, about an arm's length away, and keep your elbows at roughly 90 degrees with forearms level. Your feet should rest flat on the floor, and your lower back should be supported by the chair, so add a small rolled towel if needed. Then build in movement: stand up every thirty minutes, even for a minute, and do a couple of shoulder rolls and chest stretches. Strengthening your upper back with rows or resistance bands helps in the long run. Posture improves when you feel comfortable, so aim for position changes rather than a perfect pose.",
     "improving desk posture", "posture",
     [("how often did you say to stand up", "Every thirty minutes, even for a minute.||Add a couple of shoulder rolls.", ["thirty minutes"]),
      ("where should the top of the screen be, you gave a spot", "At eye level, about an arm's length away.||Keep your elbows at roughly 90 degrees.", ["eye level"])]),
    ("what's a simple way to start composting in a small apartment",
     "A worm bin is the best fit for a small space. A 10 gallon plastic bin with a lid, some holes for air and a bed of shredded newspaper gives red wiggler worms a home that stays odorless when managed well. Start with a pound of worms, which you can buy online or at a garden store, and feed them vegetable scraps, coffee grounds and eggshells in small amounts, burying them under the bedding. Avoid meat, dairy, oily food and citrus peels in big quantities. The bin can live under the sink or in a closet. After about three months, you will have rich compost for your plants.",
     "composting in an apartment", "composting",
     [("what size bin did you suggest", "A 10 gallon plastic bin with a lid and some holes for air.", ["10 gallon"]),
      ("what kind of worms were they", "They were red wigglers.||About a pound is enough to start the bin.", ["red wiggler"])]),
    ("how do i figure out how much paint i need for my bedroom, it's 12 by 14 with 8 foot walls",
     "For a 12 by 14 foot room with 8 foot walls, you need about two gallons for two coats. The perimeter is 52 feet, and multiplied by 8 feet it gives 416 square feet of wall. A gallon covers roughly 350 square feet, so one coat takes just over a gallon, and two coats take just over two. Subtract about 20 square feet for each door and 15 for each average window if you want to be exact, though most people simply round up. Buy a quart extra for touch-ups, and ask the store to tint all the cans together from one batch so the color matches.",
     "paint for a bedroom", "paint",
     [("how many gallons did you work out for the room", "About two gallons, for two coats.||Add a quart extra for touch-ups.", ["two gallons"]),
      ("what was the total wall area you calculated", "416 square feet of wall.||It comes from a 52 foot perimeter times the 8 foot height.", ["416"])]),
]


# =============================================================================
# SUB-SKILL 6: long chats (7-10 exchanges of base-length replies) and condensed chats (the digest path)
# =============================================================================
# Production keeps the last ~19,500 characters of a chat verbatim (history budget of Gemma's 16,384-token window),
# at most 40 messages, and condenses everything older into one "Earlier in this conversation" note
# (service/memory/context.py: the user's words, clipped at 160 characters, and an assistant reply's lead sentence
# plus the sentences that carry a number or a name, 200 characters). The builder runs the real loop, so a chat of
# about 21 exchanges with base-length replies shows that note by itself. These scenarios ask for something the
# first exchanges said:
#   long   7-10 exchanges, 2,500-9,000 characters, everything verbatim
#   digest 21-23 exchanges, the anchor exchange sits in the condensed note; the fact asked for must survive in the
#          note's text (checked on the captured request), and the answer uses only that.
# Anchors: a fact the USER stated in exchange 1, a fact the ASSISTANT stated in its first long reply, "what was the
# first thing I asked", a personal fact never said (broad recall, then an honest "not in this chat, not saved"),
# and a suggestion the assistant never made (answered from the conversation, with what was covered).
# A position claim ("first message", "near the start") is made only when the anchor really sits there; any other
# position gets a position-neutral lead. meta.anchor_pos records the anchor's exchange index (0 = the first).
USER_LEADS_FIRST = ["", "", "You mentioned it near the start of our chat. ", "That came up in your first message. "]
ASST_LEADS_FIRST = ["", "", "That was in my first answer. ", "I said that near the start. "]
USER_LEADS_NEAR = ["", "", "You mentioned it near the start of our chat. ", "You said so early in our chat. "]
ASST_LEADS_NEAR = ["", "", "I said that near the start. ", "I said that early in our chat. "]
USER_LEADS_ANY = ["", "", "You mentioned it earlier in our chat. ", "That came up earlier. "]
ASST_LEADS_ANY = ["", "", "I said that earlier in our chat. ", "That was in one of my earlier answers. "]


def leads_for(pos, user):
    """The lead-in pool whose position claim is true for an anchor at exchange index `pos`."""
    if pos == 0:
        return USER_LEADS_FIRST if user else ASST_LEADS_FIRST
    if pos == 1:
        return USER_LEADS_NEAR if user else ASST_LEADS_NEAR
    return USER_LEADS_ANY if user else ASST_LEADS_ANY


def _core_tail(final, digest):
    """"core||tail": the core states the asked fact from words the condensed note keeps; the tail adds a detail from
    the earlier reply, so it is used only when the chat is verbatim."""
    core, _, tail = final.partition("||")
    return core if (digest or not tail) else core + " " + tail


def _lowfirst_keep(s):
    return s if len(s) > 1 and (s[1].isupper() or s[1].isdigit()) else s[:1].lower() + s[1:]


def _long_fillers(rng, k, exclude_users, needles=()):
    """k long exchanges, the least-used first; none mentions any of `needles`."""
    bk = banks()
    pool = bk["longx"] * 2 + bk["long"]
    seen, cands = set(exclude_users), []
    for f in pool:
        if f[0] in seen:
            continue
        seen.add(f[0])
        if any(_has(f[0] + " " + f[1], nd) for nd in needles):
            continue
        cands.append(f)
    out, used = [], set()
    while len(out) < k:
        left = [f for f in cands if f[0] not in used]
        if not left:
            return None
        low = min(_FUSED.get(f[0], 0) for f in left)
        f = rng.choice([x for x in left if _FUSED.get(x[0], 0) == low])
        used.add(f[0])
        _FUSED[f[0]] = _FUSED.get(f[0], 0) + 1
        out.append(dict(turn(f[0], f[1]), topic=f[2], keys=[f[3]], filler=True))
    return out


def long_history(rng, anchors, pos, n_total, needles=()):
    fill = _long_fillers(rng, n_total - len(anchors), {a["user"] for a in anchors}, needles)
    if fill is None:
        return None
    return fill[:pos] + list(anchors) + fill[pos:]


def mkl(rng, prompt, now, w, steps, notes, hist, kind, digest, **rules):
    sc = sc_(rng, prompt, now, w, steps, notes, hist, kind, **rules)
    sc.long_mode = "digest" if digest else "long"
    return sc


def _seq_sizes(rng, digest):
    # 41+ messages: production keeps the newest 40 verbatim, so exchanges 0..n-21 are condensed
    return rng.choice([21, 22, 22, 23, 24]) if digest else rng.choice([8, 9, 9, 10, 10])


def _anchor_pos(rng, digest, n):
    return rng.randint(0, n - 21) if digest else rng.choice([0, 0, 0, 0, 1])


def l_user_fact(i, digest):
    def make(rng):
        u, r, topic, kw, qa = USERFACT[i % len(USERFACT)]
        prompt, final, must = qa[(i // len(USERFACT)) % len(qa)]
        anchor = dict(turn(u, r), topic=topic, keys=[kw], filler=False, kind="ufact")
        n = _seq_sizes(rng, digest)
        pos = _anchor_pos(rng, digest, n)
        hist = long_history(rng, [anchor], pos, n, needles=must)
        if hist is None:
            return None
        now, w = base(rng)
        final = fin(rng, rng.choice(leads_for(pos, True)) + _core_tail(final, digest))
        return mkl(rng, prompt, now, w, [Final(final)], "a fact the person stated in an early message of a LONG chat: answered from the conversation, no call",
                   hist, "l_user", digest, no_call=True, must=must, anchor=pos, anchor_user=must, min_final=1)
    return make


def l_asst_fact(i, digest):
    def make(rng):
        u, r, topic, kw, qa = ASSTFACT[i % len(ASSTFACT)]
        cands = [x for x in qa if not digest or all(_has(digest_line("assistant", r), n) for n in x[2])]
        if not cands:
            return None
        prompt, final, must = cands[(i // len(ASSTFACT)) % len(cands)]
        anchor = dict(turn(u, r), topic=topic, keys=[kw], filler=False, kind="afact")
        n = _seq_sizes(rng, digest)
        pos = _anchor_pos(rng, digest, n)
        hist = long_history(rng, [anchor], pos, n, needles=must)
        if hist is None:
            return None
        now, w = base(rng)
        final = fin(rng, rng.choice(leads_for(pos, False)) + _core_tail(final, digest))
        return mkl(rng, prompt, now, w, [Final(final)], "a fact the ASSISTANT stated in its first long reply of a LONG chat: answered from the conversation, no call",
                   hist, "l_asst", digest, no_call=True, must=must, anchor=pos, anchor_asst=must, min_final=1)
    return make


FIRST_Q = ["what was the first thing i asked you in this chat", "remind me what i opened this chat with", "what was the very first question in this chat",
           "which question started this whole conversation", "what was asked first, way back", "what was the opening question of this chat"]


def l_first(i, digest):
    def make(rng):
        # the first exchange is a plain question from the long bank (or an assistant-fact seed), never a statement
        pool = [(f[0], f[1], f[2], f[3]) for f in banks()["longx"]] + [(a[0], a[1], a[2], a[3]) for a in ASSTFACT]
        f = pool[(i * 7 + rng.randrange(len(pool))) % len(pool)]
        if len(f[0]) > 150:
            return None
        anchor = dict(turn(f[0], f[1]), topic=f[2], keys=[f[3]], filler=True, kind="first")
        fill = _long_fillers(rng, _seq_sizes(rng, digest) - 1, {f[0]}, needles=(f[3],))
        if fill is None:
            return None
        seq = [anchor] + fill
        if not _topic_ok(seq):
            return None
        now, w = base(rng)
        prompt = rng.choice(FIRST_Q)
        final = fin(rng, f"You started by asking about {f[2]}.", f"The first thing you asked was about {f[2]}.", f"Your opening question was about {f[2]}.")
        others = [e["keys"][0] for e in seq[1:]]
        return mkl(rng, prompt, now, w, [Final(final)], "'what was the first thing I asked' in a LONG chat: the first user message, found in the history",
                   seq, "l_first", digest, no_call=True, must=[f[3]], must_not=others, anchor=0, anchor_user=[f[0]], min_final=1)
    return make


UNSTORED_L = [
    ("what's my gym locker number", "locker", "your locker number"),
    ("which airline am i flying with", "airline", "the airline you're flying with"),
    ("what's my passport expiry date", "passport", "your passport's expiry date"),
    ("what's my doctor's name", "doctor", "your doctor's name"),
    ("what's my sister's address", "sister", "your sister's address"),
    ("which hotel did i book", "hotel", "the hotel you booked"),
    ("what's my mother's maiden name", "maiden", "your mother's maiden name"),
    ("what's my employee id", "employee", "your employee ID"),
    ("what do i owe on my student loan", "loan", "your student loan balance"),
    ("what's my vet's phone number", "vet", "your vet's number"),
]
# Every never-said final acknowledges it in the form distill/choose.py's NONE_ACK recognises ("haven't mentioned",
# "haven't talked about", "haven't told me", ...), so the SFT picker and the GRPO reward judge it the same way.
NEVER_USER_FINALS = [
    "You haven't mentioned {x} in this chat, and I don't have it saved either. If you tell me, I'll hold onto it.",
    "We haven't talked about {x} in this chat, and there's nothing saved about it. Tell me and I'll keep it for next time.",
    "You haven't told me {x} in this chat, and nothing is saved either. If you share it, I'll remember it.",
]
# Round 5 (ASK_LORA_R5=1). The r4 Mac gate answered ordinary chat as a memory lookup ("Our memory has nothing saved for
# 'rough day'", 29 of 333 replies against base's 1), and 23 finals of the two lists above and below are the likely teacher.
# The r5 lists keep the honest "not in this chat" only where the prompt really asks about something earlier or about a
# personal fact, vary the wording, never offer to save, and name memory only for the prompts that ask about stored facts.
NEVER_USER_FINALS_R5 = [
    "You haven't mentioned {x} in this chat, and I don't have it on file either.",
    "I don't have {x}. You haven't told me in this chat, and it isn't in anything I keep.",
    "We haven't talked about {x} in this chat, and I can't find it on my side.",
    "Not that I can see. You haven't shared {x} in this chat, and nothing I hold covers it.",
    "I don't have {x} yet, since you haven't said in this chat. It isn't stored on my side either.",
]
NEVER_ASST_FINALS_R5 = [
    "We haven't talked about {x} in this chat, so I haven't recommended one. We've covered {t}.",
    "I didn't mention {x}, at least not in this chat. What we've been over is {t}.",
    "I haven't said anything about {x} here. So far the chat has been about {t}.",
    "I don't think I mentioned {x} in this chat. We've covered {t}, if one of those is what you were after.",
    "I haven't mentioned {x} anywhere in this chat. What we've been through is {t}.",
]
UNSTORED_FINALS_R5 = [
    "I don't have {x} saved anywhere.",
    "{X} isn't something I have stored.",
    "I can't find {x} in anything I've saved.",
    "There's nothing on file for {x} yet.",
    "I don't have {x}, and it isn't in what I've saved.",
]
DID_NO_R5 = ["{W} hasn't come up in this chat. So far we've covered {topics}.", "No, not {word}. We've only been over {topics}.",
             "I haven't brought up {word} here. We've covered {topics}."]


def _r5() -> bool:
    return os.environ.get("ASK_LORA_R5") == "1"


def l_never_user(i, digest):
    def make(rng):
        prompt, stem, x = UNSTORED_L[i % len(UNSTORED_L)]
        hist = long_history(rng, [], 0, _seq_sizes(rng, digest), needles=(stem,))
        if hist is None:
            return None
        now, w = base(rng)
        w.memories = rng.sample(OTHER_MEM, 2)
        text = w.handle("recall", {"query": stem})
        assert "No matching memory" in text, text
        final = fin(rng, *[t.format(x=x, X=cap1(x)) for t in (NEVER_USER_FINALS_R5 if _r5() else NEVER_USER_FINALS)])
        return mkl(rng, prompt, now, w, [[Call("recall", query=stem)], Final(final)],
                   "a personal fact never said in a LONG chat and not stored: one broad recall, then an honest 'not in this chat, not saved', no invention",
                   hist, "l_never_user", digest, first="recall", args={"query": stem}, min_final=40, anchor=0, stem=stem, must=["this chat"])
    return make


NEVER_ASST = [
    ("which hotel did you recommend", ("hotel",), "a hotel"),
    ("what was the plumber you mentioned", ("plumber",), "a plumber"),
    ("what airline did you suggest", ("airline",), "an airline"),
    ("which brand of laptop did you point me to", ("laptop",), "a laptop brand"),
    ("what was the website you told me about", ("website", "www"), "a website"),
    ("which restaurant did you suggest for tonight", ("restaurant",), "a restaurant"),
    ("what was the movie you recommended", ("movie", "film"), "a movie"),
    ("which app did you say i should download", ("download", "app "), "an app"),
    ("what was that podcast you suggested", ("podcast",), "a podcast"),
    ("who was the author you mentioned", ("author",), "an author"),
]
NEVER_ASST_FINALS = [
    "We haven't talked about {x} in this chat, so I haven't recommended one. We've covered {t}.",
    "I haven't mentioned {x} here, so there's nothing to point you back to. So far we've talked about {t}.",
    "I don't think I mentioned {x}. What we've been over is {t}, among other things.",
]


def l_never_asst(i, digest):
    def make(rng):
        prompt, absent, x = NEVER_ASST[i % len(NEVER_ASST)]
        hist = long_history(rng, [], 0, _seq_sizes(rng, digest), needles=absent)
        if hist is None:
            return None
        now, w = base(rng)
        picks = rng.sample(hist, 3)
        topics = join([e["topic"] for e in picks])
        final = fin(rng, *[t.format(x=x, t=topics) for t in (NEVER_ASST_FINALS_R5 if _r5() else NEVER_ASST_FINALS)])
        return mkl(rng, prompt, now, w, [Final(final)], "something the assistant never suggested, in a LONG chat: a plain 'I didn't', with what was covered",
                   hist, "l_never_asst", digest, no_call=True, must=[e["keys"][0] for e in picks], min_final=1, anchor=0,
                   absent=absent)
    return make




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
def fam_session(rng):
    _FIRST.clear()
    _FUSED.clear()
    out = []
    plan = [
        # 1. answer from the conversation
        (s1_agenda_time, 6), (s1_agenda_order, 3), (s1_agenda_loc, 3), (s1_agenda_ordinal, 4), (s1_agenda_write, 6),
        (s1_mail, 6), (s1_rem, 4), (s1_msg, 4), (s1_wx, 3), (s1_price, 5), (s1_calc, 3),
        # 2. re-read when the detail was not stated
        (s2_agenda_loc, 8), (s2_mail_ord, 7), (s2_mail_detail, 6), (s2_rem_when, 5), (s2_msg_exact, 4), (s2_wx, 3), (s2_notes, 3),
        # 3. challenges
        (lambda i: s3_agenda(False), 4), (lambda i: s3_agenda(True), 2), (lambda i: s3_mail(False), 3), (lambda i: s3_mail(True), 2),
        (lambda i: s3_rem(False), 2), (lambda i: s3_rem(True), 1), (lambda i: s3_wx(False), 2), (lambda i: s3_wx(True), 1), (s3_restate, 6),
        # 4. the session itself
        (s4_first, 5), (s4_cover, 5), (s4_before, 3), (s4_goback, 5), (lambda i: s4_didask(True), 2), (lambda i: s4_didask(False), 2),
        # 5. stored facts
        (s5_plain, 10), (s5_a_then_b, 3), (s5_a_again, 2), (s5_told, 5), (s5_missing, 3),
        # 6. long chats (everything verbatim) and condensed chats (the "Earlier in this conversation" note)
        (lambda i: l_user_fact(i, False), 12), (lambda i: l_asst_fact(i, False), 12), (lambda i: l_first(i, False), 8),
        (lambda i: l_never_user(i, False), 6), (lambda i: l_never_asst(i, False), 6),
        (lambda i: l_user_fact(i + 7, True), 18), (lambda i: l_asst_fact(i + 5, True), 15), (lambda i: l_first(i + 3, True), 12),
        (lambda i: l_never_user(i + 4, True), 4), (lambda i: l_never_asst(i + 5, True), 4),
    ]
    for maker, n in plan:
        got, tries = 0, 0
        while got < n and tries < n * 12:
            sc = _attempt(maker(tries), rng)
            tries += 1
            if sc is not None:
                out.append(sc)
                got += 1
    out = _interleave(out, lambda sc: "digest" if getattr(sc, "long_mode", None) == "digest" else sc.kind, rng)
    if os.environ.get("SESSION_RECALL_STATS"):
        from collections import Counter
        lens = Counter(len(sc.history) for sc in out)
        chars = sorted(_hist_chars(sc) for sc in out)
        subs = Counter(sc.kind.split("_")[0] for sc in out)
        kinds = Counter(sc.kind for sc in out)
        calls = Counter("write" if sc.rules.get("first") == "add_reminder" else ("no call" if sc.rules.get("no_call") else "read") for sc in out)
        print("STATS scenarios", len(out), "by sub-skill", dict(sorted(subs.items())), "calls", dict(calls))
        print("STATS exchanges per scenario", dict(sorted(lens.items())), "chars min/median/max", chars[0], chars[len(chars) // 2], chars[-1])
        print("STATS kinds", dict(sorted(kinds.items())))
    if os.environ.get("SESSION_RECALL_SOFT"):
        from collections import Counter
        why = Counter(w.split(":")[0][:90] if w.startswith("ASSERT") else w[:60] for _, _, w in DROPPED)
        print("DROPPED", len(DROPPED), why.most_common(40))
        import json as _j
        with open(os.path.join(os.environ.get("TMPDIR", "/tmp"), "session_recall_dropped.json"), "w") as f:
            _j.dump(DROPPED, f, indent=1)
    return out


FAMILIES_R4 = {FAMILY: fam_session}
