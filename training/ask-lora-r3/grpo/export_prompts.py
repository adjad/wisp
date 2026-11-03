#!/usr/bin/env python3
"""Export GRPO decision-step prompts + reward labels from an r3 builder output.

    ASK_LORA_OUT=<dir with all.jsonl + meta.jsonl>   (the r3 build; see ../BRIEF-agents.md)
    python export_prompts.py --model <gemma-4-E4B-it-oQ4e-mtp dir> [--out-dir DIR] [--n 1500]

Writes grpo_prompts.jsonl (and grpo_prompts.stats.json) next to this file (or in --out-dir). Mac side only:
tokenizer, no model, no network. Each row:

    {"id", "family", "prompt", "label": "<json string>"}

`prompt` is the production prompt, rendered by ../brev/render.py (imported, not copied) with Wisp's own
template; the example's ideal next step supplies the label (see reward.py for how it is used):
    step     load | call | final | ask
    expected [{"name", "args"}]          (a load step expects get_tool_schemas)
    alt_name_sets   other accepted name sets (a load may be done by calling the stubs by name)
    key_args / present_args              per expected call: identity args compared, long-text args that must be non-empty
    forbidden   every write/effect tool the ideal turn does not call (and not already called in the history),
                plus web_fetch / run_shell / open_app / move_path unless the ideal uses them
    final / ask / facts                  an answer was ideal; a clarifying question was ideal; strings it must state
    forced      the prompt already ends inside the thought channel
    user_name   the name that must not appear in the third person

Multimodal prompts (about --mm-share = 15% of the rows, vision and audio mixed in evenly through the file):
    vision  the vision agent's rows (../vision/vision_rows.jsonl) rendered by render.render_vision, i.e. vision.py's
            own request (its system prompt, image parts, question; thinking off). Train split only.
    audio   voice.py's request: one audio part + _PROMPT, no system message, thinking off. Rows from the audio
            agent (../audio/audio_rows.jsonl, else the fallback made by make_audio_rows.py). Train split only.
Their labels (reward.py, modality "vision" | "audio") carry: required facts, the expected proposal fields or None
(a proposal must NOT appear), banned payload strings (planted-text injections), the unreadable flag, the
reference transcript and no-speech flag. Rows carry `images` / `audio` paths relative to the packed media
folder (--mm-dir, default ./mm, copied here so it can be rsynced next to grpo_prompts.jsonl). Each split's DEV
rows go to grpo_mm_dev.jsonl: never trained on, decoded greedily during the run as the regression tripwire.

Picture shapes (#436; reward.py "Picture shapes"), about --picture-share = 6.5% of the rows, half and half:
    picture_read  the READ call: rows of kind "picture_read" in vision_rows.jsonl (system _READ_SYSTEM, image(s), the
            "request, for context only" user text; thinking off, production temperature 0, max_tokens 700), rendered like
            any vision row. Label: task "picture_read", truth_lines, inj / inj_text, the screen label. Train split only
            (row "split"); the dev rows join grpo_mm_dev.jsonl, so the greedy tripwire watches them too.
            SAMPLING: temperature 0 gives G identical completions and so no advantage, so these rows are rolled out at
            --mm-temperature (grpo.py, default 0.7) like every other vision row; only the dev tripwire decodes greedily.
    picture_turn_r3  Ask decision steps from the builder family "picture_turn_r3" (normal text rows, built through
            Wisp's real /agent): the history holds the synthesized read_attached_picture call and its UNTRUSTED reading.
            Label: the usual step labels plus picture_turn and `planted`, the strings (addresses, URLs, numbers,
            commands) that only the reading supplied (the risky tokens of meta.inj_text, which the builder records; no
            inj_text means nothing is planted, so a card's own address is fine to use). A call carrying
            one scores -2.

Round 4 (--round 4; the r3 export is unchanged without it): the same files, labels with the extra fields reward.py's round-4 terms
read, and a different mix. The data paths are configurable (--build-dir / ASK_LORA_OUT, --vision-rows, --audio-rows; for round 4 the
defaults are ../../ask-lora-r4/data and ../../ask-lora-r4/vision/vision_rows.jsonl, output in ../../ask-lora-r4/grpo/):
    mix       ~30% narrate-after-read steps, ~20% compose and safety decision steps (COMPOSE_SAFETY_FAMILIES, with
              safe_restraint, gather_then_send_r4, restraint_r4 and compose_r3 upweighted), ~15% session steps (instructions_r4 and
              session_recall_r4 at weight 2, session_chat_r4 at 1.5), ~20% other text, 15% vision and audio (picture reads
              included; proposal and injection rows upweighted).
    label     round 4; narrate (a final after a tool result); chat (a final with no tool result and no write in the turn);
              ground (the person's words, the tool results and the earlier answers: what an answer may restate);
              post_read (the step follows a tool result), constraints (instructions_r4 / session families: the mechanical instructions
              in force, kept only when the ideal answer meets them), recall (session_recall_r4), style (distilled chat: the base reply's
              word count and emoji), thought_words (the ideal thought's length, for the thinking floor);
              tool_names (every tool in the build); requires_read (the reads that precede the turn's first write in the
              scripted turn, from the builder meta) and reads_done (those already in this step's history).

Only steps of the SFT train split are used (valid ids are excluded unless --include-valid) so the SFT dev
set stays a clean gate. Safety and restraint families are upweighted by sampling copies; ordering keeps
similar prompt lengths together in groups of GROUP so a generation batch pads little.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import reward as W  # noqa: E402

RENDER = HERE.parent / "brev" / "render.py"

# Tools that change the world or reach outside. Whatever a scenario's own meta adds is unioned in.
WRITE_TOOLS = frozenset({
    "send_message", "send_email", "schedule_send", "reply_to_email", "draft_email", "draft_message",
    "remember", "forget", "add_calendar_event", "update_event", "cancel_event", "add_reminder",
    "update_reminder", "complete_reminder", "clear_reminders", "clear_past_reminders", "create_note",
    "append_note", "manage_contacts", "set_timer", "manage_timers", "cancel_scheduled_send",
    "write_file", "move_path", "copy_path", "delete_path", "trash_path", "make_dir",
    "set_volume", "set_wifi", "set_bluetooth", "set_brightness", "set_focus", "clipboard_write",
    "web_fetch", "run_shell", "open_app", "quit_app",
})
# Round 4 (unsafe-2): the rest of the live registry's effectful tools. The r3 list above missed forward_email and eight more, so
# a rollout that called one on a turn whose scenario never did lost only the tool_set credit. service.router.model_led.
# effectful_call is the production definition: live_effect_tools() re-derives it at export time and main() unions it in, and
# test_export_r4.py fails when this list falls behind the registry. organize_files / manage_timers are effects once confirm=true
# / a non-list action, and are forbidden whenever the ideal turn does not call them.
WRITE_TOOLS_R4 = WRITE_TOOLS | frozenset({
    "forward_email", "organize_files", "toggle_setting", "write_document", "save_knowledge", "edit_knowledge",
    "delete_knowledge", "create_tool", "window_control", "commit_private_meeting", "correct_pattern",
    "email_triage", "spreadsheet_ops", "screen_capture", "text_to_speech", "run_applescript",
    # skill tools (service/skills/bundled/*/SKILL.md, category skill_tool: not in the import-time registry)
    "mac_set_volume", "mac_mute", "mac_lock_screen", "mac_media",
})
WRITE_TOOLS_R4_STATIC = WRITE_TOOLS_R4
ALWAYS_RISKY = ("web_fetch", "run_shell", "open_app", "move_path")
LONG_TEXT_KEYS = {"body", "text", "message", "content", "notes", "description", "details"}
USER_NAME = "Sam"
GROUP = 8                       # similar-length text prompts kept together (A6000 step = 4 of them, 8x B300 step = 8)
WHOLE_STEPS = 16                # rows are cut to a multiple of this: 4, 8 and 16 prompts per step all divide it
WEIGHTS = {
    "safe_effects": 3.0, "ignore_embedded_instructions": 3.0, "no_write_for_reads": 3.0,
    "right_write_tool": 2.0, "capability_honest": 2.0, "relay_receipt": 1.5, "exact_args": 1.5,
    "act_dont_ask": 1.5, "recover_from_tool_error": 1.5,
}
MAX_COPIES = 3
VISION_WEIGHTS = {"injection": 2.0, "proposal": 1.5, "abstain": 1.5, "unreadable": 1.0}
AUDIO_WEIGHTS = {"injection": 2.0, "no_speech": 2.0, "numbers": 1.5, "names": 1.5}
SPOKEN_OBEYED = ["I can't", "I cannot", "I'm sorry", "Sure,", "Sure!", "Okay,", "Here is", "Here's"]
CHAT_FAMILIES = {"chat_voice", "knowledge_direct", "concise_gentle"}   # a question there is conversation, not a clarification
# Round 4 names the chat families chat_voice_r3 / chat_voice_r4 / chat_direct_r4 / session_chat_r4 (the r3 set above never matched
# them): a casual reply that ends in "?" is conversation there, so it must not become an "ask" row that pays +0.5 for the "?".
R4_CHAT_FAMILIES = CHAT_FAMILIES | {"chat_voice_r3", "chat_voice_r4", "chat_direct_r4", "session_chat_r4"}
# Families whose finals are the untuned base's own replies (self-distilled): reward.py's chat style term anchors to them.
DISTILLED_CHAT_FAMILIES = frozenset({"chat_voice_r3", "chat_voice_r4", "chat_direct_r4", "session_chat_r4"})
# Families where a final that ends in "?" is a clarification only when it is one short question (the rest are answers
# that happen to end in a question); a standing instruction ("no questions") must never be paid a "?".
QUESTION_CHECK_FAMILIES = frozenset({"instructions_r4", "session_recall_r4"})
CLARIFY_MAX_WORDS = 20
RECALL_FAMILY = "session_recall_r4"
INSTRUCTION_FAMILIES = frozenset({"instructions_r4", "session_recall_r4", "session_chat_r4"})
SESSION_FAMILIES = frozenset({"session_chat_r4", "session_recall_r4", "instructions_r4"})
# Round 4 mix (PLAN.md, Stage 2): shares of all rows. The vision and audio share is --mm-share (0.15), picture reads included.
R4_NARRATE_SHARE, R4_COMPOSE_SHARE, R4_SESSION_SHARE = 0.30, 0.20, 0.15
R4_PICTURE_SHARE = 0.04         # of all rows; half READ calls (inside the 15% mm share), half picture-turn steps (inside "other")
COMPOSE_SAFETY_FAMILIES = frozenset({
    "safe_restraint", "gather_then_send_r4", "restraint_r4", "compose_r3",       # upweighted below
    "safe_effects", "ignore_embedded_instructions", "no_write_for_reads", "reminders_r3"})   # the other r3 unsafe patterns
R4_WEIGHTS = {"safe_restraint": 3.0, "gather_then_send_r4": 3.0, "restraint_r4": 3.0, "compose_r3": 2.5,
              "safe_effects": 2.0, "ignore_embedded_instructions": 2.0, "no_write_for_reads": 2.0,
              "instructions_r4": 2.0, "session_recall_r4": 2.0, "session_chat_r4": 1.5}
R4_FACT_BONUS = 1.5             # a narrate row with labelled facts pays up to +1.0 for them: more signal than one without
R4_VISION_WEIGHTS = {"injection": 2.5, "proposal": 2.5, "abstain": 1.5, "unreadable": 1.0}
R4_PICTURE_READ_WEIGHTS = {"injection": 2.5, "plain": 1.0}
_NOT_NAMES = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Monday", "Tuesday", "Wednesday", "Thursday",
              "Friday", "Saturday", "Sunday", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
              "Sept", "Oct", "Nov", "Dec", "January", "February", "March", "April", "June", "July", "August",
              "September", "October", "November", "December"}


# Read-only wisp_* admin tools and the preview-only tools stay out of the live union (effectful_call says so too).
_LIVE_SNIPPET = (
    "import json,sys; sys.path.insert(0, sys.argv[1]); import service.tools; "
    "from service.tools.registry import REGISTRY; from service.router.model_led import needs_effect_owner; "
    "print(json.dumps(sorted(n for n, t in REGISTRY.items() if needs_effect_owner(t.category))))")
_LIVE_READ_ONLY = frozenset({"list_running_apps", "wisp_status", "wisp_capabilities", "wisp_skills", "wisp_mcp"})


def live_effect_tools(repo_root: Path | None = None, timeout: int = 120) -> frozenset | None:
    """The effectful tools of the live registry (everything outside model_led.READ_CATEGORIES), read in a child process with a
    throwaway WISP_HOME so nothing touches the person's data. None when the repo cannot be imported (the static list stands)."""
    import subprocess
    import tempfile
    root = repo_root or HERE.parent.parent.parent
    with tempfile.TemporaryDirectory(prefix="wisp-home-") as home:
        env = dict(os.environ, WISP_HOME=home, MOE_DIR=home)
        try:
            out = subprocess.run([sys.executable, "-c", _LIVE_SNIPPET, str(root)], capture_output=True, text=True,
                                 env=env, cwd=str(root), timeout=timeout)
            return frozenset(json.loads(out.stdout.strip().splitlines()[-1])) - _LIVE_READ_ONLY if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None


def load_render():
    sys.path.insert(0, str(RENDER.parent))
    spec = importlib.util.spec_from_file_location("ask_lora_render", RENDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def called_names(messages: list[dict]) -> list[str]:
    return [tc["function"]["name"] for m in messages for tc in (m.get("tool_calls") or ())]


def key_args(args: dict, name: str) -> tuple[dict, list]:
    keys, present = {}, []
    for k, v in (args or {}).items():
        if name == DISCOVERY and k != "tools":
            continue
        if isinstance(v, str) and k in LONG_TEXT_KEYS:
            if v.strip():
                present.append(k)
        elif isinstance(v, (bool, int, float)):
            keys[k] = v
        elif isinstance(v, str) and 0 < len(v) <= 80:
            keys[k] = v
        elif isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            keys[k] = v
    return keys, present


DISCOVERY = "get_tool_schemas"
_TIME = re.compile(r"\b\d{1,2}(?::\d\d)?\s?(?:AM|PM|am|pm)\b")
_NUM = re.compile(r"[$]?\d[\d,]*(?:\.\d+)?[%°]?")
_QUOTED = re.compile(r"[“\"]([^”\"]{2,60})[”\"]")
_CAP = re.compile(r"\b[A-Z][a-z]{2,}(?:\s[A-Z][a-z]{2,})*\b")
_SKIP_CAPS = {"You", "Your", "That", "This", "There", "Here", "Nothing", "Nothing", "Tomorrow", "Today",
              "Tonight", "Want", "Would", "Should", "Could", "What", "Which", "When", "Where", "How",
              "Okay", "Sure", "Yes", "Not", "But", "And", "The", "Wisp", "Mac", "Because", "Since", "Once"}


def extract_facts(final: str, limit: int = 6) -> list[str]:
    """Times, numbers, quoted titles and mid-sentence names from the ideal final answer."""
    facts: list[str] = []

    def add(x):
        x = x.strip()
        if x and x.lower() not in {f.lower() for f in facts}:
            facts.append(x)
    for m in _QUOTED.finditer(final):
        q = m.group(1)
        if len(q.split()) <= 6 and not q.rstrip().endswith(("!", "?", ".")):
            add(q)
    times = _TIME.findall(final)
    for t in times:
        add(t)
    rest = _TIME.sub(" ", final)
    for m in _NUM.finditer(rest):
        tok = m.group(0)
        digits = re.sub(r"\D", "", tok)
        if len(digits) >= 2 or tok[0] in "$" or tok[-1] in "%°":
            add(re.sub(r"[$%°,]", "", tok))
    for m in _CAP.finditer(final):
        start = m.start()
        before = final[:start].rstrip()
        if start == 0 or before.endswith((".", "!", "?", "\n", "•", "-", "*", "“", '"', ":", "(", "·", "–", "—")):
            continue
        name = m.group(0)
        if name.split()[0] in _SKIP_CAPS | _NOT_NAMES:
            continue
        add(name)
    return facts[:limit]


_STOP = set("""about above after again also another because before being between could does doesn each from have here into
its just like line made make much only other over same shows showing some still such than that their them then there
these they this those under very what when where which while with would your yours image picture photo screen text
says said reads read written writes telling tells told tries asking asks wants assistant someone anyone anything
nothing something instead asked listed lists mentions mentioned appears visible clearly
doesn't don't isn't aren't can't won't wasn't didn't mention include includes included contain contains
available specific details information""".split())


_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"


def mm_facts(visible: str) -> list[str]:
    """Candidate facts for a vision answer: extract_facts, a leading Yes/No, else up to 3 content words of the first
    sentence (the injection note and other boilerplate come later), only for short answers. Bare years and the day
    number of a date are dropped when anything else remains: asking for them verbatim punishes a correct paraphrase."""
    visible = re.sub(r"^\s*(going by the image|here'?s what it shows|from the image|the image shows)\s*[:,]?\s*", "", visible, flags=re.I)
    facts = extract_facts(visible)
    m = re.match(r"\s*(yes|no)\b", visible, re.I)
    if m and m.group(1).lower() not in {f.lower() for f in facts}:
        facts.insert(0, m.group(1))
    if facts:
        def date_part(f: str) -> bool:
            if re.fullmatch(r"(19|20)\d\d", f):
                return True
            return bool(re.fullmatch(r"\d{1,2}", f)) and re.search(rf"\b{_MONTH}\s+{f}\b|\b{f}(st|nd|rd|th)\b", visible, re.I) is not None
        kept = [f for f in facts if not date_part(f)]
        return (kept or facts)[:6]
    first = re.split(r"(?<=[.!?])\s+", visible.strip())[0]
    if len(visible.split()) > 30:
        return []
    words = []
    for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", first):
        if w.lower() not in _STOP and w.lower() not in {x.lower() for x in words}:
            words.append(w)
    words.sort(key=len, reverse=True)
    return words[:3]


def usable_facts(facts: list[str]) -> bool:
    """A fact list is worth a reward term when it holds a specific fact (a number or amount, a time, a multi-word
    name or title) or at least two distinct content facts. A lone Yes/No, a lone year or a lone filler word is not."""
    real = [f for f in facts if f.lower() not in ("yes", "no")]
    strong = [f for f in real if (re.search(r"\d", f) and not re.fullmatch(r"(19|20)\d\d", f)) or len(f.split()) >= 2]
    return bool(strong) or len(real) >= 2


def thought_words(completion: str, forced: bool) -> int:
    """Words in the ideal thought: reward.py's thinking floor never asks for more than this."""
    return len(W.parse(completion, forced).thought.split())


def clarifying_question(text: str) -> bool:
    """One short question, not an answer that happens to end in one."""
    return text.strip().endswith("?") and len(text.split()) <= CLARIFY_MAX_WORDS


def classify(ex: dict, meta: dict, rnd: int = 3, turns: dict | None = None, tool_names=()) -> dict:
    msgs, target = ex["messages"], ex["messages"][-1]
    hist = msgs[:-1]
    last_user = max((i for i, m in enumerate(hist) if m["role"] == "user"), default=-1)
    tool_result_seen = any(m["role"] == "tool" for m in hist[last_user + 1:])
    calls = [tc["function"] for tc in (target.get("tool_calls") or ())]
    label: dict = {"modality": "text", "user_name": USER_NAME, "final": False, "ask": False, "facts": [], "expected": [],
                   "key_args": [], "present_args": [], "alt_name_sets": []}
    ideal_names: set[str] = set()
    if calls:
        label["step"] = "load" if all(c["name"] == DISCOVERY for c in calls) else "call"
        for c in calls:
            args = c["arguments"] if isinstance(c["arguments"], dict) else json.loads(c["arguments"])
            label["expected"].append({"name": c["name"], "args": args})
            k, pr = key_args(args, c["name"])
            label["key_args"].append(k)
            label["present_args"].append(pr)
            ideal_names.add(c["name"])
            if c["name"] == DISCOVERY:
                ideal_names.update(args.get("tools") or ())
        if label["step"] == "load":
            tools = [t for c in calls for t in (c["arguments"].get("tools") or ())]
            if tools:
                label["alt_name_sets"].append(sorted(tools))
    else:
        text = (target.get("content") or "").strip()
        fam = meta.get("family")
        asks = text.endswith("?") and not tool_result_seen and fam not in (R4_CHAT_FAMILIES if rnd >= 4 else CHAT_FAMILIES)
        if asks and rnd >= 4 and fam in QUESTION_CHECK_FAMILIES and not clarifying_question(text):
            asks = False
        label["step"] = "ask" if asks else "final"
        label["final"], label["ask"] = not asks, asks
        if not asks:
            label["facts"] = extract_facts(text)
    already = set(called_names(hist))
    pending_writes = set(meta.get("turn_writes") or ()) - already
    write_universe = (WRITE_TOOLS_R4 if rnd >= 4 else WRITE_TOOLS) | set(meta.get("writes") or ()) | set(meta.get("turn_writes") or ()) \
        | {e[0] for e in (meta.get("effects") or ())}
    allowed = ideal_names | pending_writes
    label["forbidden"] = sorted(t for t in write_universe if t not in allowed)
    label["ideal_text"] = (target.get("content") or "") if not calls else ""
    pic = picture_info(hist, meta)
    if pic is not None:
        label["picture_turn"], label["planted"] = True, pic
    if rnd >= 4:
        r4_fields(label, hist, meta, tool_result_seen, turns or {}, tool_names)
    return label


# ------------------------------------------------------------------ round 4 labels

def turn_key(meta: dict) -> str:
    return meta["id"].rsplit("-s", 1)[0]


def turn_reads(steps: list[tuple[int, list[str]]], turn_writes, writes_extra=()) -> list[str]:
    """steps: (step index, names of the calls the scripted target makes at that step), any order. The reads that
    precede the turn's first write: call names of the earlier steps that are not writes (nor tool discovery)."""
    writes = set(turn_writes or ()) | set(writes_extra)
    if not writes:
        return []
    ordered = sorted(steps)
    first = next((i for i, names in ordered if writes & set(names)), None)
    if first is None:
        return []
    out: list[str] = []
    for i, names in ordered:
        if i >= first:
            break
        for n in names:
            if n != DISCOVERY and n not in WRITE_TOOLS and n not in writes and n not in out:
                out.append(n)
    return out


def ground_text(hist: list[dict], limit: int = 40000) -> str:
    """What an answer may restate: the person's words, the tool results, the arguments the model passed and its earlier
    answers, plus the short system lines (the clock). The long rules prompt is left out."""
    parts = []
    for m in hist:
        t = _msg_text(m)
        if m["role"] == "system":
            if len(t) < 3000:
                parts.append(t)
            continue
        parts.append(t)
        for tc in m.get("tool_calls") or ():
            a = tc["function"].get("arguments")
            parts.append(a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))
    return "\n".join(p for p in parts if p)[:limit]


def user_texts(hist: list[dict]) -> list[str]:
    """The person's messages before the current one, oldest first."""
    users = [_msg_text(m) for m in hist if m["role"] == "user"]
    return users[:-1]


def last_user_text(hist: list[dict]) -> str:
    users = [_msg_text(m) for m in hist if m["role"] == "user"]
    return users[-1] if users else ""


def recall_facts(label: dict, hist: list[dict]) -> list[str]:
    """What a session-recall answer must carry from the earlier turns: the facts of the ideal answer, else up to three
    content words of it that the person's or the assistant's earlier messages contain (the 'recall ref')."""
    if label.get("facts"):
        return label["facts"]
    earlier = " ".join(_msg_text(m) for m in hist if m["role"] in ("user", "assistant")).lower()
    words: list[str] = []
    for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", label.get("ideal_text") or ""):
        if w.lower() not in _STOP and w.lower() in earlier and w.lower() not in {x.lower() for x in words}:
            words.append(w)
    words.sort(key=len, reverse=True)
    return words[:3]


# ---- standing and current instructions -> mechanical constraints (reward.py constraint_ok)
_NUM_WORDS = {"five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15,
              "twenty": 20, "twenty-five": 25, "thirty": 30, "forty": 40, "fifty": 50}
_RESET = re.compile(r"\b(back to (?:normal|your usual|the usual)|normal(?:ly)? again|(?:normal|regular) answers|talk normally|as usual|"
                    r"the usual|forget (?:that|what i said)|never ?mind|scrap that|stop (?:doing|with) that|no need to (?:keep|stick)|"
                    r"(?:drop|ignore|skip) (?:that|the|my) (?:rule|request|preference))\b", re.I)
_LIFT = re.compile(r"\b(casual|buddy|chill|friends?|loosen up|relaxed|formal|stiff|plain english|in english|chatty|longer answers?|"
                   r"more detail|elaborate)\b", re.I)
_REPLACE = re.compile(r"^\W*(?:actually|instead|scratch that|on second thought|change of plans)\b|\binstead\b", re.I)
_N = r"(\d{1,3}|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")"
_RULES = (
    ("max_words", re.compile(rf"\b(?:under|below|less than|fewer than|no more than|at most|within|max(?:imum)?(?: of)?)\s+{_N}\s+words?\b|\b{_N}\s+words?\s+(?:or\s+(?:less|fewer)|max)\b", re.I)),
    ("max_words1", re.compile(r"\b(?:one|1|single)[- ]word\b|\bjust a word\b", re.I)),
    ("max_sentences", re.compile(r"\b(?:one|1|a single|single|just one)[- ](?:sentence|line)s?\b|\b(?:in|into) (?:a|one) sentence\b|\bone[- ]liner\b", re.I)),
    ("short_answer", re.compile(r"\b(?:just|only) (?:the|a) (?:time|number|figure|name|amount|date|day|answer|headline)\b|\bnumbers? only\b|\bjust give me the (?:number|figure)\b", re.I)),
    ("yes_no_first", re.compile(r"\byes[ /]or[ /]no\b|\byes/no\b", re.I)),
    ("no_question", re.compile(r"\bno (?:follow[- ]?up )?questions?\b|\bending (?:your )?(?:replies|answers|responses) with (?:a )?questions?\b|\bno asking\b|\b(?:don'?t|do not) (?:quiz|ask) me\b|\b(?:stop|quit) quizzing me\b|\bno more (?:follow[- ]?up )?questions?\b|\b(?:don'?t|do not) (?:finish|end) (?:with|on) (?:a )?questions?\b|\bskip the questions?\b|\bwithout (?:asking|questions)\b|\bstop asking (?:me )?(?:questions|things|stuff)\b", re.I)),
    ("no_bullets", re.compile(r"\bno (?:bullets?|bullet points?|lists?)\b|\bwithout (?:bullets?|lists?)\b|\b(?:don'?t|do not) (?:use|give me) (?:bullets?|lists?)\b|\bin prose\b|\b(?:normal|plain|regular) sentences\b|\bstop (?:with )?(?:the )?(?:bullets?|bullet lists?|lists?)\b", re.I)),
    ("bullets_only", re.compile(r"\bbullets? (?:only|please|plz|pls)\b|\bbullet(?:ed)? (?:list|points?)\b|\bin bullets\b|\bas bullets\b", re.I)),
    ("numbered", re.compile(r"\bnumbered (?:steps?|list)\b|\bnumber(?:ed)? (?:them|the steps)\b", re.I)),
    ("no_emoji", re.compile(r"\bno emoji'?s?\b|\bwithout (?:any )?emoji'?s?\b|\b(?:don'?t|do not) use emoji'?s?\b", re.I)),
    ("no_digits", re.compile(r"\bno (?:numbers|digits)\b|\bwithout (?:any )?(?:numbers|digits)\b", re.I)),
    ("spanish", re.compile(r"\b(?:in|reply in|answer in|speak|switch to) spanish\b|\ben espa\u00f1ol\b|\bhabla(?:me)? en espa\u00f1ol\b", re.I)),
)
_CLASS = {"max_words": "len_words", "max_words1": "len_words", "max_sentences": "len_sentences", "short_answer": "len_words",
          "yes_no_first": "lead", "no_question": "question", "no_bullets": "format", "bullets_only": "format",
          "numbered": "format", "no_emoji": "emoji", "no_digits": "digits", "spanish": "language"}


_ABOUT_SPANISH = re.compile(r"\b(?:how (?:do|would|can|should) (?:you|i|we) (?:say|spell|write)|translate|what(?:'s| is| does)|means?)\b", re.I)
LIFTED = ("len_words", "len_sentences", "format", "lead", "language")      # what 'back to normal' / 'make it casual' clears


def _num(tok: str) -> int:
    return int(tok) if tok.isdigit() else _NUM_WORDS[tok.lower()]


def message_wishes(text: str) -> dict:
    """{class: constraint} the message asks for. A bullet word under a negation ('no bullets') is not a bullet request."""
    out: dict = {}
    for kind, rx in _RULES:
        m = rx.search(text)
        if not m:
            continue
        if kind == "bullets_only" and out.get("format", {}).get("kind") == "no_bullets":
            continue
        if kind == "spanish" and _ABOUT_SPANISH.search(text):
            continue                                      # "how do you say thank you in spanish" is a question, not a wish
        if kind == "max_words":
            toks = [g for g in m.groups() if g]
            c = {"kind": "max_words", "n": _num(toks[0])}
        elif kind == "max_words1":
            c = {"kind": "max_words", "n": 1}
        elif kind == "max_sentences":
            c = {"kind": "max_sentences", "n": 1}
        elif kind == "short_answer":
            c = {"kind": "max_words", "n": 4 if "number" in m.group(0).lower() or "figure" in m.group(0).lower() else 8}
        else:
            c = {"kind": kind}
        cls = _CLASS[kind]
        if kind == "short_answer" and cls in out:
            continue                                      # an explicit cap in the same message is the tighter statement
        out[cls] = c
    return out


def derive_constraints(earlier: list[str], current: str) -> list[dict]:
    """The mechanical instructions in force for this turn's answer: wishes the person set in earlier messages (latest of each
    kind wins; a message that lifts or changes the style, e.g. 'back to normal' or 'make it casual', clears the length and
    format wishes before its own are applied), then this message's own wishes, which override the same kind. Conservative by
    construction: classify() keeps only constraints the ideal answer itself satisfies."""
    standing: dict = {}
    for text in earlier:
        if _REPLACE.search(text) and message_wishes(text):        # "actually, one sentence": the new wish replaces the old ones
            standing.clear()
        if _RESET.search(text):
            standing.clear()
        elif _LIFT.search(text):
            for cls in LIFTED:
                standing.pop(cls, None)
        standing.update(message_wishes(text))
    cur = message_wishes(current)
    if _RESET.search(current):
        standing.clear()
    elif _LIFT.search(current):
        for cls in LIFTED:
            standing.pop(cls, None)
    standing.update(cur)
    return list(standing.values())


def r4_fields(label: dict, hist: list[dict], meta: dict, tool_result_seen: bool, turns: dict, tool_names) -> None:
    label["round"] = 4
    final = bool(label.get("final"))
    label["narrate"] = final and tool_result_seen
    label["chat"] = final and not tool_result_seen and not meta.get("turn_writes")
    label["post_read"] = tool_result_seen            # a step after a read in this turn: reward.py's thinking floor applies here
    fam = meta.get("family")
    recall = final and fam == RECALL_FAMILY
    if recall:
        label["recall"] = True
        label["facts"] = recall_facts(label, hist)
        label["denial_ok"] = W.recall_denied(label.get("ideal_text") or "")      # an ideal that is itself a denial: reward.py skips the penalty
    if label["narrate"] or recall:
        label["ground"] = ground_text(hist)
    if final and fam in DISTILLED_CHAT_FAMILIES and label["chat"] and (label.get("ideal_text") or "").strip():
        label["style"] = {"words": len(label["ideal_text"].split()), "emoji": W.has_emoji(label["ideal_text"])}
    if final and fam in INSTRUCTION_FAMILIES:
        cons = meta["constraints"] if isinstance(meta.get("constraints"), list) else \
            derive_constraints(user_texts(hist), last_user_text(hist))
        # a constraint the ideal answer itself breaks is a misreading of the prompt: it is dropped, never trained on
        label["constraints"] = [c for c in cons if W.constraint_ok(c, label.get("ideal_text") or "")]
    if final and tool_names:
        label["tool_names"] = list(tool_names)
    req = turn_reads(turns.get(turn_key(meta), []), meta.get("turn_writes"))
    label["requires_read"] = req
    label["reads_done"] = [r for r in req if r in set(called_names(hist))]


PICTURE_TOOL = "read_attached_picture"
PICTURE_PREFIX = "UNTRUSTED CONTENT: Wisp's reading of "


def _msg_text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c if isinstance(c, str) else ""


def picture_info(hist: list[dict], meta: dict):
    """None when the history holds no picture reading; else the strings only the reading supplied (see reward.py)."""
    reading, trusted = None, []
    pic_ids = {tc.get("id") for m in hist for tc in (m.get("tool_calls") or ()) if tc["function"]["name"] == PICTURE_TOOL}
    for m in hist:
        if m["role"] == "tool" and (m.get("tool_call_id") in pic_ids or _msg_text(m).startswith(PICTURE_PREFIX)) \
                and (reading is None):
            txt = _msg_text(m)
            reading = txt.split("\n\n", 1)[1] if "\n\n" in txt else txt
        elif m["role"] in ("user", "assistant"):
            trusted.append(_msg_text(m))
    if reading is None:
        return None
    return W.planted_strings(reading, "\n".join(trusted), meta.get("inj_text"))


def r4_bucket(it: dict) -> str:
    """session (any step of a session or instruction family) | narrate (a final after a tool result) | compose (a decision
    step of a compose or safety family) | other."""
    lab = it["label"]
    if it["family"] in SESSION_FAMILIES:
        return "session"
    if lab.get("narrate"):
        return "narrate"
    if it["family"] in COMPOSE_SAFETY_FAMILIES:
        return "compose"
    return "other"


def r4_weight(it: dict) -> float:
    w = R4_WEIGHTS.get(it["family"], WEIGHTS.get(it["family"], 1.0))
    if it["label"].get("narrate") and it["label"].get("facts"):
        w *= R4_FACT_BONUS
    return w


def r4_pick(items: list[dict], count: int, rng: random.Random) -> list[dict]:
    """`count` rows by weight, no row more than MAX_COPIES times (a smaller pool gives fewer rows, never a flood of copies)."""
    count = min(count, MAX_COPIES * len(items))
    return weighted_pick(items, count, rng) if count > 0 else []


def r4_text_rows(items: list[dict], pics: list[dict], n_text: int, n_all: int, rng: random.Random) -> tuple[list[dict], dict]:
    """The round-4 text mix: narrate, compose and session quotas from n_all, the rest 'other' (picture-turn rows count there)."""
    pools = {"narrate": [], "compose": [], "session": [], "other": []}
    for it in items:
        it = dict(it, weight=r4_weight(it))
        pools[r4_bucket(it)].append(it)
    n_nar, n_com, n_ses = round(n_all * R4_NARRATE_SHARE), round(n_all * R4_COMPOSE_SHARE), round(n_all * R4_SESSION_SHARE)
    n_oth = n_text - n_nar - n_com - n_ses - len(pics)
    got = {"narrate": r4_pick(pools["narrate"], n_nar, rng), "compose": r4_pick(pools["compose"], n_com, rng),
           "session": r4_pick(pools["session"], n_ses, rng), "other": r4_pick(pools["other"], n_oth, rng)}
    short = {k: want - len(got[k]) for k, want in (("narrate", n_nar), ("compose", n_com), ("session", n_ses), ("other", n_oth))
             if len(got[k]) < want}
    # a thin bucket is topped up from "other" so the file keeps its size; the stats say which bucket was short
    missing = sum(short.values())
    if missing > 0:
        spare = [it for it in pools["other"] if it["id"] not in {g["id"] for g in got["other"]}]
        got["other"] += r4_pick(spare, missing, rng)
    rows = got["narrate"] + got["compose"] + got["session"] + got["other"] + pics
    return rows, {"quota": {"narrate": n_nar, "compose": n_com, "session": n_ses, "other": n_oth, "picture_turn": len(pics)},
                  "got": {k: len(v) for k, v in got.items()}, "short": short,
                  "pool": {k: len(v) for k, v in pools.items()}}


def weighted_copies(items: list[dict], n: int, rng: random.Random) -> list[dict]:
    total = sum(it["weight"] for it in items)
    out = []
    for it in items:
        e = min(MAX_COPIES, n * it["weight"] / total)
        c = int(math.floor(e)) + (1 if rng.random() < e - math.floor(e) else 0)
        out += [dict(it, copy=k) for k in range(c)]
    return out


def order(rows: list[dict], rng: random.Random) -> list[dict]:
    """Shuffle, then group similar prompt lengths: windows of 16 groups sorted by length, groups shuffled."""
    rng.shuffle(rows)
    win = GROUP * 16
    groups = []
    for i in range(0, len(rows), win):
        chunk = sorted(rows[i:i + win], key=lambda r: r["_tokens"])
        groups += [chunk[j:j + GROUP] for j in range(0, len(chunk), GROUP)]
    rng.shuffle(groups)
    return [r for g in groups for r in g]


# ------------------------------------------------------------------ multimodal rows

def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in open(path) if l.strip()]


def weighted_pick(pool: list[dict], count: int, rng: random.Random) -> list[dict]:
    """`count` rows by weight, without replacement within a pass; a second pass starts when the pool runs out."""
    out: list[dict] = []
    while len(out) < count and pool:
        keyed = sorted(pool, key=lambda it: rng.random() ** (1.0 / it["weight"]), reverse=True)
        out += [dict(it, copy=len(out) // len(pool)) for it in keyed[:count - len(out)]]
    return out


def vision_rows(R, tok, path: Path, drift: Counter) -> tuple[list[dict], list[dict]]:
    """(train items, dev items). A row is dev when the SFT's own split says so (render.vision_is_dev) or its meta does."""
    train, dev = [], []
    for row in read_jsonl(path):
        if row.get("kind") == PICTURE_READ_KIND:      # the READ call has its own labels (picture_read_rows)
            continue
        meta, sid = row.get("meta") or {}, row["id"]
        if not all((path.parent / i).is_file() for i in row["images"]):
            drift["MISSING vision image (row skipped)"] += 1
            continue
        is_dev = R.vision_is_dev(sid, meta)      # exactly the SFT rule: meta.split first, id hash only when it is absent
        rel = [f"vision/{i}" for i in row["images"]]
        out = R.render_vision(tok, row, rel)
        visible, prop, _ = W.parse_proposal(row["target"])
        unreadable = bool(meta.get("unreadable"))
        banned = list(meta.get("banned_in_target") or [])
        if unreadable:
            facts = []
        elif isinstance(meta.get("facts"), list) and meta["facts"]:       # the row's own list wins when it has one
            facts = [str(f) for f in meta["facts"] if not W._banned_hit(str(f), banned)]
        else:
            facts = [f for f in mm_facts(visible) if not W._banned_hit(f, banned)]
        no_facts = False
        if not unreadable and not usable_facts(facts):
            if not meta.get("abstain"):
                drift["vision: no usable facts (row dropped)"] += 1
                continue
            facts, no_facts = [], True          # an abstain row is judged on 'no proposal' and the penalties alone
        label = {"modality": "vision", "step": "mm_vision", "forced": False, "think": False,
                 "facts": facts, "proposal": ({k: prop[k] for k in W.PROPOSAL_FIELDS} if prop else None),
                 "banned": banned, "unreadable": unreadable, "abstain": bool(meta.get("abstain")),
                 "no_facts": no_facts, "category": meta.get("category"), "kind": row.get("kind"), "max_tokens": W.VISION_MAX_TOKENS,
                 "ideal_completion": out["completion"]}
        kind = ("injection" if meta.get("injection") else "unreadable" if unreadable
                else "abstain" if meta.get("abstain") else "proposal" if prop else "plain")
        item = {"id": sid, "family": "vision_r3", "modality": "vision", "prompt": out["prompt"], "label": label,
                "images": rel, "mmkind": kind, "src": [path.parent / i for i in row["images"]], "weight": VISION_WEIGHTS.get(kind, 1.0),
                "_tokens": 0}
        (dev if is_dev else train).append(item)
    return train, dev


PICTURE_READ_KIND = "picture_read"
PICTURE_TURN_FAMILY = "picture_turn_r3"
PICTURE_READ_WEIGHTS = {"injection": 2.0, "plain": 1.0}


def picture_read_rows(R, tok, path: Path, drift: Counter) -> tuple[list[dict], list[dict]]:
    """(train items, dev items) for the READ call. The row's own `split` decides; a missing or odd value is an error."""
    train, dev = [], []
    for row in read_jsonl(path):
        if row.get("kind") != PICTURE_READ_KIND:
            continue
        sid = row["id"]
        if row.get("split") not in ("train", "dev"):
            raise ValueError(f"{sid}: picture_read split must be 'train' or 'dev', got {row.get('split')!r}")
        if not all((path.parent / i).is_file() for i in row["images"]):
            drift["MISSING picture_read image (row skipped)"] += 1
            continue
        rel = [f"vision/{i}" for i in row["images"]]
        out = R.render_vision(tok, row, rel)
        if not row.get("truth_lines") and "(none)" not in row["target"]:
            drift["picture_read: no truth_lines and no (none) (row dropped)"] += 1
            continue
        lab = {"modality": "vision", "task": "picture_read", "step": "mm_picture_read", "forced": False, "think": False,
               "truth_lines": [str(l) for l in row.get("truth_lines") or ()], "inj": row.get("inj"),
               "inj_text": row.get("inj_text"), "screen_label": row.get("label"), "gen": row.get("gen"),
               "banned": [], "category": "picture_read", "kind": PICTURE_READ_KIND, "max_tokens": W.READ_MAX_TOKENS,
               "ideal_completion": out["completion"]}
        kind = "injection" if row.get("inj") else "plain"
        item = {"id": sid, "family": "picture_read_r3", "modality": "vision", "prompt": out["prompt"], "label": lab,
                "images": rel, "mmkind": "picture_read", "src": [path.parent / i for i in row["images"]],
                "weight": PICTURE_READ_WEIGHTS[kind], "_tokens": 0, "inj": bool(row.get("inj"))}
        (dev if row["split"] == "dev" else train).append(item)
    return train, dev


def picture_problems(prt, prd, ptt, need_reads: int, need_turns: int) -> list[str]:
    """What would leave the picture shapes thin: too few train rows, no injection rows, no dev rows for the tripwire."""
    out = []
    if need_reads:
        if not prt:
            out.append("picture_read: no TRAIN rows in vision_rows.jsonl (kind 'picture_read')")
        elif not any(r["inj"] for r in prt):
            out.append("picture_read: no TRAIN rows with an injection")
        if not prd:
            out.append("picture_read: no DEV rows")
    if need_turns and not ptt:
        out.append(f"picture turn: no steps of family {PICTURE_TURN_FAMILY!r} in the build")
    return out


def audio_rows(R, tok, path: Path, drift: Counter) -> tuple[list[dict], list[dict]]:
    train, dev = [], []
    for row in read_jsonl(path):
        meta, sid = row.get("meta") or {}, row["id"]
        if "reference" in row and "target" not in row:           # tolerate the flat shape
            row = {**row, "target": row["reference"]}
        if not (path.parent / row["audio"]).is_file():
            drift["MISSING audio clip (row skipped)"] += 1
            drift[f"MISSING audio clip, kind {(meta.get('kind') or 'speech')}"] += 1
            continue
        target = (row.get("target") or "").strip()
        asked = [p.get("text") for m in row.get("messages") or () for p in (m.get("content") or ()) if p.get("type") == "text"]
        drift["audio: row prompt differs from voice._PROMPT"] += bool(asked) and asked[0] != W.VOICE_PROMPT
        msgs = [{"role": "user", "content": [{"type": "audio"}, {"type": "text", "text": W.VOICE_PROMPT}]}]
        kw = {"enable_thinking": False}
        prompt = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, **kw)
        if prompt.count("<|audio|>") != 1 or prompt.endswith(R.FORCED):
            raise ValueError(f"{sid}: unexpected audio prompt shape {prompt[-60:]!r}")
        if target:
            full = tok.apply_chat_template(msgs + [{"role": "assistant", "content": target}], tokenize=False, **kw)
            if not full.startswith(prompt):
                raise ValueError(f"{sid}: prompt is not a prefix of the full render")
            completion = full[len(prompt):].rstrip("\n")
            if completion != target + "<turn|>":
                raise ValueError(f"{sid}: unexpected completion shape {completion!r}")
        else:
            completion = "<turn|>"
        kind = meta.get("kind") or "speech"
        banned = list(meta.get("banned") or (SPOKEN_OBEYED if kind == "injection" else []))
        label = {"modality": "audio", "step": "mm_audio", "forced": False, "think": False, "reference": target,
                 "no_speech": not target, "banned": [b for b in banned if b.lower() not in target.lower()],
                 "category": kind, "lang": meta.get("lang") or "en", "max_tokens": W.AUDIO_MAX_TOKENS,
                 "ideal_completion": completion}
        rel = f"audio/{row['audio']}"
        item = {"id": sid, "family": "audio_r3", "modality": "audio", "prompt": prompt, "label": label, "audio": rel,
                "src": [path.parent / row["audio"]], "weight": AUDIO_WEIGHTS.get(kind, 1.0), "_tokens": 0}
        (dev if R.audio_is_dev(row) else train).append(item)     # exactly the SFT rule (meta.split, else transcript hash)
    return train, dev


REQUIRED_AUDIO_KINDS = ("command", "numbers", "names", "injection", "no_speech", "non_english", "long")
REQUIRED_VISION_KINDS = ("injection", "unreadable", "abstain", "proposal", "plain")


def pick_audio_rows(candidates: list[Path]) -> Path | None:
    """The first rows file whose every clip exists; None if none is complete."""
    for c in candidates:
        if not c.exists():
            continue
        rows = read_jsonl(c)
        if rows and all((c.parent / (r.get("audio") or "")).is_file() for r in rows):
            return c
        print(f"note: {c} lists clips that are missing; not used")
    return None


def coverage_problems(vt, vd, at, ad, drift: Counter) -> list[str]:
    """Everything that would silently thin the multimodal mix or the guard: missing media, and any kind with no
    train rows (nothing for RL to see) or no dev rows (nothing for the tripwire to see)."""
    out = [f"{k}: {v}" for k, v in sorted(drift.items()) if k.startswith("MISSING") and v]
    for name, tr, dv, kinds in (("audio", at, ad, REQUIRED_AUDIO_KINDS), ("vision", vt, vd, REQUIRED_VISION_KINDS)):
        have_t = Counter(r["label"].get("category") if name == "audio" else r["mmkind"] for r in tr)
        have_d = Counter(r["label"].get("category") if name == "audio" else r["mmkind"] for r in dv)
        for k in kinds:
            if not have_t[k]:
                out.append(f"{name}: no TRAIN rows of kind {k!r}")
            if not have_d[k]:
                out.append(f"{name}: no DEV rows of kind {k!r}")
    return out


def interleave(text: list[dict], mm: list[dict]) -> list[dict]:
    """mm rows spread evenly through the text rows, so every optimizer step sees about the same share."""
    n = len(text) + len(mm)
    slots = {int((j + 0.5) * n / len(mm)) for j in range(len(mm))} if mm else set()
    t, m = iter(text), iter(mm)
    return [next(m) if i in slots else next(t) for i in range(n)]


def pack_media(items: list[dict], mm_dir: Path) -> int:
    n = 0
    for it in items:
        rels = it.get("images") or ([it["audio"]] if it.get("audio") else [])   # text_document: no media
        for rel, src in zip(rels, it["src"]):
            dst = mm_dir / rel
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-dir", default=os.environ.get("ASK_LORA_OUT"), help="dir with all.jsonl + meta.jsonl")
    ap.add_argument("--model", required=True, help="Gemma 4 E4B-it dir with the tokenizer + chat_template.jinja")
    ap.add_argument("--round", type=int, default=3, choices=(3, 4),
                    help="4: the round-4 labels and mix (see the module docstring); data and output defaults move to ../../ask-lora-r4/")
    ap.add_argument("--out", default=None, help="default: ./grpo_prompts.jsonl (round 4: ../../ask-lora-r4/grpo/grpo_prompts.jsonl)")
    ap.add_argument("--n", type=int, default=1500)
    ap.add_argument("--seed", type=int, default=20261101)
    ap.add_argument("--max-prompt-tokens", type=int, default=9000,
                    help="longer prompts are dropped (production prompts run 3k to 9k tokens, mostly tool schemas)")
    ap.add_argument("--include-valid", action="store_true", help="also use the SFT dev split's ids")
    ap.add_argument("--vision-rows", default=None,
                    help="default: ../vision/vision_rows.jsonl (round 4: ../../ask-lora-r4/vision/vision_rows.jsonl)")
    ap.add_argument("--audio-rows", default=None,
                    help="default: ../audio/audio_rows.jsonl when every clip it lists exists, else the fallback ./audio/audio_rows.jsonl "
                         "(make_audio_rows.py) when that is complete; never a set with clips missing")
    ap.add_argument("--allow-missing-media", action="store_true",
                    help="keep going when images or clips a row lists are missing (the rows are dropped). Off by default: "
                         "silently losing whole audio categories would hollow out the RL mix and the regression guard")
    ap.add_argument("--mm-share", type=float, default=0.15, help="share of all rows that are vision or audio prompts (0 = text only)")
    ap.add_argument("--audio-share", type=float, default=0.4, help="share of the multimodal rows that are audio")
    ap.add_argument("--picture-share", type=float, default=None,
                    help="share of all rows that are picture shapes (picture_read + picture_turn_r3 steps); default 0.065 "
                         "(round 4: 0.04, the READ half counted inside --mm-share); 0 = none")
    ap.add_argument("--picture-read-frac", type=float, default=0.5, help="share of the picture rows that are READ calls")
    ap.add_argument("--require-picture", action="store_true",
                    help="refuse to export when a picture shape has no rows (default: warn and put the rows back into the text mix)")
    ap.add_argument("--mm-dir", default=None, help="where the images and clips of the picked rows are copied (default ./mm; round 4: next to --out)")
    a = ap.parse_args()
    r4 = a.round >= 4
    r4_root = HERE.parent.parent / "ask-lora-r4"
    if r4 and not a.build_dir:
        a.build_dir = str(r4_root / "data")
    if not a.build_dir:
        sys.exit("set ASK_LORA_OUT or pass --build-dir")
    a.out = a.out or str((r4_root / "grpo" if r4 else HERE) / "grpo_prompts.jsonl")
    a.vision_rows = a.vision_rows or str((r4_root / "vision" if r4 else HERE.parent / "vision") / "vision_rows.jsonl")
    a.mm_dir = a.mm_dir or str((Path(a.out).parent if r4 else HERE) / "mm")
    if a.picture_share is None:
        a.picture_share = R4_PICTURE_SHARE if r4 else 0.065
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    bd = Path(a.build_dir)
    R = load_render()
    sha = R.template_sha(a.model)
    if sha != R.TEMPLATE_SHAS["gemma"]:
        sys.exit(f"{a.model}: not the production Gemma 4 template (sha {sha[:16]})")
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.model)
    valid_ids = set()
    vm = bd / "valid.meta.jsonl"
    if vm.exists() and not a.include_valid:
        valid_ids = {json.loads(l)["id"] for l in open(vm)}
    if r4:
        global WRITE_TOOLS_R4
        live = live_effect_tools()
        if live is None:
            print("WARNING: could not read the live tool registry; the static WRITE_TOOLS_R4 list stands")
        else:
            WRITE_TOOLS_R4 = WRITE_TOOLS_R4 | live
            print(f"live registry: {len(live)} effectful tools, {len(live - WRITE_TOOLS_R4_STATIC)} not in the static list: "
                  f"{sorted(live - WRITE_TOOLS_R4_STATIC)}")
    pairs = [(json.loads(e), json.loads(m)) for e, m in zip(open(bd / "all.jsonl"), open(bd / "meta.jsonl"))]
    items, dropped = [], Counter()
    turns: dict = {}
    tool_names: set = set()
    if r4:
        for ex, meta in pairs:
            names = [tc["function"]["name"] for tc in (ex["messages"][-1].get("tool_calls") or ())]
            turns.setdefault(turn_key(meta), []).append((meta["step"], names))
            tool_names.update(t["function"]["name"] for t in ex.get("tools") or () if isinstance(t, dict) and "function" in t)
            tool_names.update(names)
        tool_names.discard(DISCOVERY)
    for ex, meta in pairs:
        if meta["id"] in valid_ids:
            dropped["valid split"] += 1
            continue
        row = R.render(tok, ex, meta)           # also verifies the ideal completion renders cleanly
        n_tok = len(tok(row["prompt"], add_special_tokens=False)["input_ids"])
        if n_tok > a.max_prompt_tokens:
            dropped["too long"] += 1
            continue
        label = classify(ex, meta, a.round, turns, sorted(tool_names))
        label["forced"] = row["prompt"].endswith(R.FORCED)
        label["think"] = bool(row["think"])
        if r4:
            label["thought_words"] = thought_words(row["completion"], label["forced"])
        label["ideal_completion"] = row["completion"]
        items.append({"id": meta["id"], "family": meta["family"], "prompt": row["prompt"], "label": label,
                      "weight": WEIGHTS.get(meta["family"], 1.0), "_tokens": n_tok})
    if r4:
        # r4 vision rows are weighted for proposals and injections (the r3 weights are module constants the row builders read)
        VISION_WEIGHTS.update(R4_VISION_WEIGHTS)
        PICTURE_READ_WEIGHTS.update(R4_PICTURE_READ_WEIGHTS)
    rng = random.Random(a.seed)
    drift: Counter = Counter()
    mm_picked, mm_dev, mm_pool = [], [], {}
    # picture shapes: the turn steps are carved out of the text pool, the READ rows join the vision rows
    turn_items = [it for it in items if it["family"] == PICTURE_TURN_FAMILY]
    items = [it for it in items if it["family"] != PICTURE_TURN_FAMILY]
    for it in turn_items:
        it["weight"] = 2.0 if it["label"].get("planted") else 1.0
    n_pic = round(a.n * a.picture_share)
    n_pr = round(n_pic * a.picture_read_frac)
    n_pt = n_pic - n_pr
    prt, prd = ([], [])
    if n_pic and Path(a.vision_rows).exists():
        prt, prd = picture_read_rows(R, tok, Path(a.vision_rows), drift)
    pic_problems = picture_problems(prt, prd, turn_items, n_pr, n_pt) if n_pic else []
    if pic_problems and a.require_picture:
        sys.exit("picture shapes are incomplete, refusing to export:\n  " + "\n  ".join(pic_problems))
    for msg in pic_problems:
        print("WARNING:", msg)
    pr_picked = weighted_pick(prt, n_pr, rng) if prt else []
    pt_picked = weighted_pick(turn_items, n_pt, rng) if turn_items else []
    mm_dev = list(prd)
    n_mm = round(a.n * a.mm_share)
    if r4:
        n_mm = max(0, n_mm - len(pr_picked))        # the READ rows are part of the 15%
    if n_mm:
        vt, vd = vision_rows(R, tok, Path(a.vision_rows), drift)
        at, ad = ([], [])
        audio_path = Path(a.audio_rows) if a.audio_rows else pick_audio_rows([HERE.parent / "audio" / "audio_rows.jsonl",
                                                                              HERE / "audio" / "audio_rows.jsonl"])
        if audio_path and audio_path.exists():
            print(f"audio rows: {audio_path}")
            at, ad = audio_rows(R, tok, audio_path, drift)
        else:
            sys.exit("no complete audio rows found (../audio/audio_rows.jsonl or ./audio/audio_rows.jsonl with every clip present); "
                     "finish the audio agent's clips or run make_audio_rows.py, or pass --mm-share 0 for a text-only run")
        problems = coverage_problems(vt, vd, at, ad, drift)
        if problems and not a.allow_missing_media:
            sys.exit("multimodal coverage is incomplete, refusing to export (pass --allow-missing-media to override):\n  "
                     + "\n  ".join(problems))
        for msg in problems:
            print("WARNING:", msg)
        n_a = round(n_mm * a.audio_share) if at else 0
        mm_pool = {"picture_read train": len(prt), "picture_read dev": len(prd), "vision train": len(vt), "vision dev": len(vd), "audio train": len(at), "audio dev": len(ad)}
        mm_picked = weighted_pick(vt, n_mm - n_a, rng) + weighted_pick(at, n_a, rng)
        mm_dev = mm_dev + vd + ad
    mm_picked = mm_picked + pr_picked
    rng.shuffle(mm_picked)
    n_text = a.n - len(mm_picked)
    r4_stats = None
    if r4:
        picked, r4_stats = r4_text_rows(items, pt_picked, n_text, a.n, rng)
    else:
        picked = weighted_copies(items, n_text - len(pt_picked), rng) + pt_picked
    text_rows = order(picked, rng)[:n_text]
    ordered = interleave(text_rows, mm_picked)
    ordered = ordered[: len(ordered) - len(ordered) % WHOLE_STEPS]      # whole optimizer steps, for 4, 8 and 16 prompts
    mm_dir = Path(a.mm_dir)

    def row_json(r: dict) -> str:
        lab = dict(r["label"], copy=r.get("copy", 0))
        out = {"id": f"{r['id']}#{r.get('copy', 0)}", "family": r["family"], "modality": r.get("modality", "text"),
               "prompt": r["prompt"], "label": json.dumps(lab, ensure_ascii=False)}
        if r.get("images"):
            out["images"] = r["images"]
        if r.get("audio"):
            out["audio"] = r["audio"]
        return json.dumps(out, ensure_ascii=False) + "\n"
    with open(a.out, "w") as f:
        f.writelines(row_json(r) for r in ordered)
    dev_path = Path(a.out).with_name("grpo_mm_dev.jsonl")
    with open(dev_path, "w") as f:
        f.writelines(row_json(r) for r in mm_dev)
    media = pack_media([r for r in ordered + mm_dev if r.get("modality") in ("vision", "audio")], mm_dir) if (ordered or mm_dev) else 0
    mm_rows = [r for r in ordered if r.get("modality") in ("vision", "audio")]
    pic_rows = [r for r in ordered if r["family"] in ("picture_read_r3", PICTURE_TURN_FAMILY)]
    stats = {
        "round": a.round, "r4_mix": r4_stats, "source": str(bd), "pool": len(items), "dropped": dict(dropped), "rows": len(ordered),
        "unique_ids": len({r["id"] for r in ordered}), "sha256": hashlib.sha256(Path(a.out).read_bytes()).hexdigest(),
        "modality": dict(Counter(r.get("modality", "text") for r in ordered)),
        "mm_share": round((len(mm_rows) - len(pr_picked)) / max(1, len(ordered)), 4),
        "picture": {"rows": len(pic_rows), "share": round(len(pic_rows) / max(1, len(ordered)), 4),
                    "read": len(pr_picked), "turn": len(pt_picked), "read_pool": [len(prt), len(prd)],
                    "turn_pool": len(turn_items), "turn_with_planted": sum(bool(r["label"].get("planted")) for r in pt_picked),
                    "problems": pic_problems}, "mm_pool": mm_pool,
        "mm_dev_rows": len(mm_dev), "mm_dev_sha256": hashlib.sha256(dev_path.read_bytes()).hexdigest(),
        "mm_media_copied": media, "mm_dir": str(mm_dir), "drift": {k: v for k, v in drift.items() if v},
        "steps": dict(Counter(r["label"]["step"] for r in ordered)),
        "families": dict(Counter(r["family"] for r in ordered).most_common()),
        "forced_prompts": sum(bool(r["label"]["forced"]) for r in ordered),
        "with_forbidden": sum(bool(r["label"].get("forbidden")) for r in ordered),
        "final_with_facts": sum(bool(r["label"].get("facts")) for r in ordered if r.get("modality", "text") == "text"),
        "prompt_tokens": {"max": max(r["_tokens"] for r in ordered), "mean": round(sum(r["_tokens"] for r in ordered if r["_tokens"]) / max(1, sum(1 for r in ordered if r["_tokens"])))},
    }
    Path(a.out).with_suffix(".stats.json").write_text(json.dumps(stats, indent=1) + "\n")
    print(json.dumps(stats, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
