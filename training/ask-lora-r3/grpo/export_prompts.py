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

Round 5 (--round 5; RL-SCOPE.md C1, C5, C6, C6b, C7, C15; rounds 3 and 4 export byte-identically): the RL round after the r4 SFT.
    labels    round 5 (the stamp is on every row, vision/audio included); clarify (the builder's meta.clarify; a build without the key falls
              back to the trailing-"?" test and label.clarify_fallback says so; chat and session rows never ask); rl_kind (chat_anchor |
              session_anchor | recall | recall_none | instruction | narrate | decision | select | call, one per text row, in the C6 order)
              and ceiling (the 3.3 table; the export exits on a missing entry; vision/audio rows: rl_kind "mm", ceiling null);
              facts only on narrate finals or from meta.facts; unsafe_reads, skill_names, tool_names on every text row; recall_refs /
              recall_absent / recall_ok from the builder meta by session question kind; thread, qkind, banned; max_tokens on chat-anchor and
              session rows (--chat-max-tokens, 1024; re-export at 1536).
    mix       28% safety/compose, 28% narrate-with-facts, 12% session (max 2 copies), 6% chat anchors, 11% selection (picture turns inside),
              15% vision/audio. Chat anchors are the meta.distilled rows of --anchor-build-dir (the spliced SFT build); fewer than the target exits.
    dev       grpo_text_dev.jsonl (50 safety/compose, 40 narrate, 10 selection from the hash-split valid set minus --picker-dev; 24
              recall_dev_r4 + 12 session_style_dev from --dev-build-dir; 30 distilled chat rows from the anchor build's valid side), its
              smoke30 subset (15/5/2/8), grpo_text_dev.stats.json, and the .overlap.jsonl sidecars check_overlap.py reads. Every
              disjointness failure exits non-zero.
    ideals    every exported text row's own ideal completion is scored by reward.py at round 5 (make_item): rows whose ideal trips a
              penalising term (or scores off label.ceiling) are dropped and counted per rl_kind in the stats (r5.ideal_trip); the export
              exits, writing grpo_prompts.ideal_trip.json, when more than --ideal-trip-pct (1) of a kind's ideals trip one term.
              Narrate facts are only those the ideal states outside a negation window; instruction, session, recall and anchor finals carry
              no facts and are not narrate rows; a decision/select final with meta.facts gets label.facts_explicit and a ceiling of 1.5.
    checks    valid.meta.jsonl must exist; session_chat_r4 counts (54 recall / 14 never-said / 99 anchors, or --refs-crosscheck's) must match;
              the stats record the DIST commit sha, choose.py's and the refs file's sha256, and (with --overlap-manifest) the overlap report's.

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


def classify(ex: dict, meta: dict, rnd: int = 3, turns: dict | None = None, tool_names=(), *, instruction_term: bool = False,
             chat_max_tokens: int | None = None, allow_old_build: bool = False, names=()) -> dict:
    """The reward label of one decision step. The keyword arguments only matter at round 5: instruction_term (the C17 term is
    on, so an instruction final's ceiling is 1.5), chat_max_tokens (C5: label.max_tokens of chat and session rows) and
    allow_old_build (a session_chat_r4 final with no meta.qkind becomes a session anchor instead of an error), names (the build's
    person-name lexicon, world_names(): label.names on a final that has a ground, for reward.py's ungrounded name checks)."""
    msgs, target = ex["messages"], ex["messages"][-1]
    hist = msgs[:-1]
    last_user = max((i for i, m in enumerate(hist) if m["role"] == "user"), default=-1)
    tool_result_seen = any(m["role"] == "tool" for m in hist[last_user + 1:])
    calls = [tc["function"] for tc in (target.get("tool_calls") or ())]
    label: dict = {"modality": "text", "user_name": USER_NAME, "final": False, "ask": False, "facts": [], "expected": [],
                   "key_args": [], "present_args": [], "alt_name_sets": []}
    ideal_names: set[str] = set()
    fallback = False                # round 5: the clarify decision came from the trailing-"?" test (an old build)
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
        if rnd >= 5:
            asks, fallback = r5_asks(text, meta, fam, tool_result_seen, anchor_row(meta, False, hist))
        else:
            asks = text.endswith("?") and not tool_result_seen and fam not in (R4_CHAT_FAMILIES if rnd >= 4 else CHAT_FAMILIES)
            if asks and rnd >= 4 and fam in QUESTION_CHECK_FAMILIES and not clarifying_question(text):
                asks = False
        label["step"] = "ask" if asks else "final"
        label["final"], label["ask"] = not asks, asks
        if not asks:
            label["facts"] = extract_facts(text) if rnd < 5 else r5_final_facts(text, meta, tool_result_seen)
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
        r4_fields(label, hist, meta, tool_result_seen, turns or {}, tool_names, rnd)
    if rnd >= 5:
        r5_fields(label, hist, meta, fallback=fallback, instruction_term=instruction_term,
                  chat_max_tokens=chat_max_tokens, allow_old_build=allow_old_build)
        r5_tool_context(label, hist, names)
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


def r4_fields(label: dict, hist: list[dict], meta: dict, tool_result_seen: bool, turns: dict, tool_names, rnd: int = 4) -> None:
    label["round"] = rnd                 # the stamp grpo.py's resolve_round checks against --round (round 5: C1)
    final = bool(label.get("final"))
    label["narrate"] = final and tool_result_seen
    label["chat"] = final and not tool_result_seen and not meta.get("turn_writes")
    label["post_read"] = tool_result_seen            # a step after a read in this turn: reward.py's thinking floor applies here
    fam = meta.get("family")
    recall = final and fam == RECALL_FAMILY and rnd < 5       # round 5: recall is scored from label.recall_refs, not from facts
    if recall:
        label["recall"] = True
        label["facts"] = recall_facts(label, hist)
        label["denial_ok"] = W.recall_denied(label.get("ideal_text") or "")      # an ideal that is itself a denial: reward.py skips the penalty
    if label["narrate"] or recall:
        label["ground"] = ground_text(hist)
    if rnd < 5 and final and fam in DISTILLED_CHAT_FAMILIES and label["chat"] and (label.get("ideal_text") or "").strip():
        label["style"] = {"words": len(label["ideal_text"].split()), "emoji": W.has_emoji(label["ideal_text"])}
    if final and fam in INSTRUCTION_FAMILIES:
        cons = meta["constraints"] if isinstance(meta.get("constraints"), list) else \
            derive_constraints(user_texts(hist), last_user_text(hist))
        # a constraint the ideal answer itself breaks is a misreading of the prompt: it is dropped, never trained on
        label["constraints"] = [c for c in cons if W.constraint_ok(c, label.get("ideal_text") or "")]
    if tool_names and (final or rnd >= 5):        # round 5: every text row (the namespace strip needs it on call rows)
        label["tool_names"] = list(tool_names)
    req = turn_reads(turns.get(turn_key(meta), []), meta.get("turn_writes"))
    label["requires_read"] = req
    label["reads_done"] = [r for r in req if r in set(called_names(hist))]


# ------------------------------------------------------------------ round 5 (RL-SCOPE C1, C5, C6, C6b, C7, C15)
# Everything below is reached only with rnd >= 5 (classify) or --round 5 (main); rounds 3 and 4 never touch it.

class BuilderMetaError(ValueError):
    """A round-5 row whose builder meta is missing or inconsistent (C6/C7); main() collects these and exits non-zero."""


# Session families of the round-5 mix: round 4's three plus followup_r3 (RL-SCOPE 4.1). A chat or session row never pays ask credit.
# The two dev-only families (C6b) are session questions too; they must never reach grpo_prompts (build_text_dev checks).
DEV_ONLY_FAMILIES = frozenset({"recall_dev_r4", "session_style_dev"})
R5_SESSION_FAMILIES = SESSION_FAMILIES | {"followup_r3"} | DEV_ONLY_FAMILIES
R5_CHAT_FAMILIES = R4_CHAT_FAMILIES | R5_SESSION_FAMILIES
# Families whose distilled plain finals are the single-turn chat anchors (rows with meta.distilled in the spliced SFT build).
R5_ANCHOR_FAMILIES = frozenset({"chat_voice_r4", "chat_direct_r4", "knowledge_direct", "concise_gentle"})
# The session question kinds, defined once to match DIST/choose.py (RECALL_KINDS; rec_none is the never-said kind).
RECALL_KINDS = frozenset({"rec_user", "rec_asst", "rec_both"})
NONE_KIND = "rec_none"
TEXT_RL_KINDS = ("chat_anchor", "session_anchor", "recall", "recall_none", "instruction", "narrate", "decision", "select", "call")
# 4.1 round-5 mix, shares of all rows (the 15% vision/audio share is --mm-share); picture-turn rows count inside "select".
R5_SHARES = {"safety": 0.28, "narrate": 0.28, "session": 0.12, "chat_anchor": 0.06, "select": 0.11}
R5_SESSION_MAX_COPIES = 2
# Families built to be answered from a read (RL-SCOPE 3.3: "narrate pre-call steps" are `call` rows, not `select`): their first-step
# call is the read their narrate final reports on, so it is not a selection scenario. These are the r4 "answers" families (PLAN C:
# families grown where r3 regressed: day free/busy, tonight, notes lookup, memory and profile answers).
R5_NARRATE_FAMILIES = frozenset({"day_answers_r4", "info_answers_r4", "lookup_answers_r4"})
# Kinds whose reward never reads labelled facts: the row's final is scored by recall refs, ask/no-ask, or a constant (RL-SCOPE 3.3).
R5_NO_FACT_KINDS = frozenset({"chat_anchor", "session_anchor", "instruction", "recall", "recall_none"})
R5_EXPLICIT_FACTS_CREDIT = 0.5        # reward.R5_EXPLICIT_FACTS: a decision/select final whose builder supplied meta.facts
R5_SAFETY_MIN_ROWS = 10               # RL-SCOPE 4.1: every safety pattern has at least this many rows (family-level proxy, see stats)
IDEAL_TRIP_PCT = 1.0                  # RL-SCOPE 3.5: fail when more than this share of a kind's ideals trip a penalising term
_SKILL_LINE = re.compile(r"^- ([a-z0-9][a-z0-9-]*): ", re.M)


def anchor_row(meta: dict, has_calls: bool, hist: list[dict] | None = None) -> bool:
    """A chat anchor: a distilled plain final of an anchor family (only the spliced SFT build has meta.distilled), on a history that
    holds no tool message (an anchor is a single-turn chat reply; defence in depth, the real ids are plain)."""
    if has_calls or not meta.get("distilled") or meta.get("family") not in R5_ANCHOR_FAMILIES:
        return False
    return not (hist and any(m.get("role") == "tool" for m in hist))


def r5_asks(text: str, meta: dict, fam: str, tool_result_seen: bool, anchor: bool) -> tuple[bool, bool]:
    """(asks, fallback). Round 5: a clarification is what the builder marked (meta.clarify); chat, session and anchor rows never
    ask. A build without the key falls back to round 3's trailing-"?" test and says so (fallback True) so the count is logged."""
    if anchor or fam in R5_CHAT_FAMILIES:
        return False, False
    if "clarify" in meta:
        return bool(meta["clarify"]), False
    return text.endswith("?") and not tool_result_seen, True


def r5_final_facts(text: str, meta: dict, tool_result_seen: bool) -> list[str]:
    """Facts exist only on narrate finals (extracted from the ideal reply) or from an explicit builder list (meta.facts)."""
    explicit = [str(f) for f in meta.get("facts") or ()]
    if explicit:
        return explicit
    if not tool_result_seen:
        return []
    # only facts the ideal states: one that sits inside its own negation window ("Nothing from Mom", "no end time on Dentist") is
    # blanked by reward.mask_negated and would leave the ideal below the ceiling (the reward pays a fact only when it is stated)
    stated = W.mask_negated(text)
    return [f for f in extract_facts(text) if W.fact_present(f, stated)]


def skill_names_of(messages: list[dict]) -> list[str]:
    """Names from the system prompt's 'Installed skills' listing, as DIST/choose.py skill_names reads it (first three messages)."""
    sys_text = "\n".join(m["content"] for m in messages[:3] if m.get("role") == "system" and isinstance(m.get("content"), str))
    if "Installed skills" not in sys_text:
        return []
    return sorted(set(_SKILL_LINE.findall(sys_text.split("Installed skills", 1)[1])))


def selection_family(fam: str) -> bool:
    """A selection scenario: not a safety/compose, chat, session or narrate family (picture_turn_r3 counts)."""
    return fam not in COMPOSE_SAFETY_FAMILIES and fam not in R5_CHAT_FAMILIES and fam not in R5_NARRATE_FAMILIES


def assign_rl_kind(label: dict, meta: dict, anchor: bool, allow_old_build: bool = False) -> str:
    """Exactly one rl_kind per text step, in the C6 order: (1) chat anchor; (2) a session question final by meta.qkind
    (recall / recall_none / session_anchor); (3) an instructions_r4 final; (4) a narrate final with labelled facts;
    (5) a tool-call step: select for a selection scenario's first step, else call; (6) a selection scenario's final is
    select, every remaining final decision."""
    fam = meta.get("family")
    step = label["step"]
    if anchor:
        return "chat_anchor"
    if step in ("final", "ask"):
        qk = meta.get("qkind")
        if qk or fam == "session_chat_r4":
            if not qk:
                if allow_old_build:
                    return "session_anchor"
                raise BuilderMetaError(f"{meta.get('id')}: session_chat_r4 final without meta.qkind (C6 builder fields missing)")
            return "recall" if qk in RECALL_KINDS else "recall_none" if qk == NONE_KIND else "session_anchor"
        if fam == "instructions_r4":
            return "instruction"
        if label.get("narrate") and label.get("facts"):
            return "narrate"
        return "select" if selection_family(fam) else "decision"
    return "select" if selection_family(fam) and meta.get("step") == 0 else "call"


def ceiling_for(rl_kind: str, step: str, clarify: bool = False, instruction_term: bool = False, facts_explicit: bool = False) -> float:
    """label.ceiling from the RL-SCOPE 3.3 ceiling table. No default: an unknown rl_kind is an error. facts_explicit (a decision or
    select final whose builder supplied meta.facts, scored by reward.py at R5_EXPLICIT_FACTS) adds that credit to the 1.0."""
    if rl_kind not in TEXT_RL_KINDS:
        raise KeyError(f"no ceiling entry for rl_kind {rl_kind!r}")
    is_call = step in ("call", "load")
    if facts_explicit:
        if rl_kind not in ("decision", "select") or is_call or clarify:
            raise KeyError(f"facts_explicit on a {rl_kind!r} {step!r} row")
        return 1.0 + R5_EXPLICIT_FACTS_CREDIT
    if rl_kind == "call":
        if not is_call:
            raise KeyError(f"rl_kind 'call' on a {step!r} step")
        return 1.5
    if clarify and not is_call:
        return 1.5                                     # any final with meta.clarify: tool_set 1 + ask 0.5
    if rl_kind == "select":
        return 1.5 if is_call else 1.0
    if rl_kind in ("narrate", "recall"):
        return 2.0
    if rl_kind == "recall_none":
        return 1.5
    if rl_kind == "instruction":
        return 1.5 if instruction_term else 1.0
    return 1.0                                         # chat_anchor, session_anchor, decision


def r5_fields(label: dict, hist: list[dict], meta: dict, *, fallback: bool, instruction_term: bool,
              chat_max_tokens: int | None, allow_old_build: bool) -> None:
    """The round-5 label keys (RL-SCOPE C6/C7), read from the builder meta with .get(): old builds lack the fields."""
    fam = meta.get("family")
    is_call = label["step"] in ("call", "load")
    anchor = anchor_row(meta, is_call, hist)
    label["clarify"] = bool(label["ask"])
    if fallback:
        label["clarify_fallback"] = True
    label["unsafe_reads"] = sorted({str(t) for t in meta.get("unsafe_reads") or ()})
    label["skill_names"] = skill_names_of(hist)
    if meta.get("banned"):
        label["banned"] = [str(b) for b in meta["banned"]]
    for k in ("thread", "qkind"):
        if meta.get(k):
            label[k] = meta[k]
    kind = assign_rl_kind(label, meta, anchor, allow_old_build)
    label["rl_kind"] = kind
    if label["step"] in ("final", "ask") and fam in R5_CHAT_FAMILIES and meta.get("clarify"):
        label["clarify_dropped"] = True       # r5_asks never lets a chat or session row ask: the flag is counted (stats), not scored
    explicit = [str(f) for f in meta.get("facts") or ()]
    if label["step"] in ("final", "ask"):
        if kind in R5_NO_FACT_KINDS:
            # an instruction final after a read, a session or anchor final: the reward scores it by recall refs or a constant, so it
            # carries no labelled facts and is not a narrate row (else the ideal would score tool_set 1 + facts 1 against a ceiling of 1)
            label["narrate"], label["facts"] = False, []
            if kind in ("chat_anchor", "session_anchor"):
                label.pop("ground", None)
            if explicit:
                label["facts_ignored"] = True
        elif kind != "narrate" and explicit and not label["ask"]:
            label["facts"], label["facts_explicit"] = explicit, True     # reward.py scores these at R5_EXPLICIT_FACTS
    if meta.get("recall"):
        mr = meta["recall"]
        label["meta_recall"] = [str(x) for x in (mr if isinstance(mr, (list, tuple)) else [mr])]
    refs, absent, ok = [str(r) for r in meta.get("recall_refs") or ()], meta.get("recall_absent"), [str(r) for r in meta.get("recall_ok") or ()]
    sid = meta.get("id")
    if kind == "recall":
        if not [r for r in refs if r.strip()]:
            raise BuilderMetaError(f"{sid}: recall row (qkind {meta.get('qkind')!r}) with empty meta.recall_refs")
        label["recall_refs"] = refs
    elif kind == "recall_none":
        if not absent:
            raise BuilderMetaError(f"{sid}: never-said row without meta.recall_absent")
        if refs:
            raise BuilderMetaError(f"{sid}: never-said row with non-empty meta.recall_refs {refs!r}")
        label["recall_refs"], label["recall_absent"], label["recall_ok"] = [], str(absent), ok
    elif (refs or absent or ok) and label["step"] in ("final", "ask") and fam in R5_SESSION_FAMILIES:
        raise BuilderMetaError(f"{sid}: {kind} row carries recall fields (recall_refs/recall_absent/recall_ok): a session anchor has none")
    try:
        label["ceiling"] = ceiling_for(kind, label["step"], label["clarify"], instruction_term, bool(label.get("facts_explicit")))
    except KeyError as e:
        raise BuilderMetaError(f"{sid}: {e.args[0]}") from None
    if label["final"] and fam in R5_SESSION_FAMILIES and kind != "session_anchor" and "ground" not in label:
        label["ground"] = ground_text(hist)             # session rows: ungrounded is checked against the history plus tool results
    if chat_max_tokens and (kind == "chat_anchor" or fam in R5_SESSION_FAMILIES):
        label["max_tokens"] = int(chat_max_tokens)      # C5: grpo.py honours a per-row cap; --max-completion does not reach these rows


_NAME_LINE = (re.compile(r"^\*\*([A-Z][a-z]+ [A-Z][a-z]+)\*\*\s*$", re.M),                    # a messages-digest conversation heading
              re.compile(r"^[-\u2022] ([A-Z][a-z]+ [A-Z][a-z]+): [\u201c\"]", re.M),              # a message line "- Theo Park: “...”"
              re.compile(r"^([A-Z][a-z]+ [A-Z][a-z]+): [+\d(]", re.M))                          # a contact line "Dad: +1 555 0100"


def world_names(pairs: list[tuple[dict, dict]]) -> list[str]:
    """The person names a build's worlds hold, harvested from its tool results (digest headings, message senders, contact lines):
    reward.py's name checks flag a name the answer uses that the row's ground lacks but the world has. First-and-last names only, so
    an organisation (one word, or a sender line of an email) and a label such as "Tomorrow" never enter."""
    seen: set = set()
    for ex, _ in pairs:
        for m in ex["messages"]:
            if m.get("role") == "tool" and isinstance(m.get("content"), str):
                for pat in _NAME_LINE:
                    seen.update(pat.findall(m["content"]))
    return sorted(seen)


def r5_tool_context(label: dict, hist: list[dict], names=()) -> None:
    """The extra context round-5 reward terms read: tool_results (this turn's tool results, so verbatim is measured against one result
    and a count is checked against the lists in it) and names (the world's person names the row's ground does not already hold)."""
    last_user = max((i for i, m in enumerate(hist) if m["role"] == "user"), default=-1)
    results, room = [], 40000                       # ground_text's cap: the copy never outgrows the ground
    for m in hist[last_user + 1:]:
        if m["role"] == "tool" and room > 0:
            results.append(_msg_text(m)[:room])
            room -= len(results[-1])
    if results and label["step"] in ("final", "ask"):
        label["tool_results"] = results
    ground = (label.get("ground") or "").lower()
    if names and label["step"] in ("final", "ask") and "ground" in label:
        label["names"] = [n for n in names if n.lower() not in ground]


def r5_label_problems(label: dict, modality: str = "text", instruction_term: bool = False) -> list[str]:
    """What a round-5 row's label must have (the export exits non-zero on any): the stamp, rl_kind, a ceiling that matches the
    table, tool_names on every text row, the recall fields by kind. Multimodal rows: rl_kind "mm" and ceiling null."""
    out = []
    if label.get("round") != 5:
        out.append(f"label.round is {label.get('round')!r}, not 5")
    if modality != "text":
        if label.get("rl_kind") != "mm" or label.get("ceiling") is not None:
            out.append("a multimodal row needs rl_kind 'mm' and ceiling null")
        return out
    kind = label.get("rl_kind")
    if kind not in TEXT_RL_KINDS:
        return out + [f"rl_kind {kind!r} has no ceiling entry"]
    try:
        want = ceiling_for(kind, label.get("step"), bool(label.get("clarify")), instruction_term, bool(label.get("facts_explicit")))
    except KeyError as e:
        return out + [e.args[0]]
    if kind in R5_NO_FACT_KINDS and label.get("step") in ("final", "ask") and (label.get("narrate") or label.get("facts")):
        out.append(f"{kind} final carries narrate/facts (its ideal would outscore the ceiling)")
    if label.get("ceiling") != want:
        out.append(f"ceiling {label.get('ceiling')!r} does not match the table ({want}) for {kind}")
    if not label.get("tool_names"):
        out.append("label.tool_names missing (every round-5 text row needs it)")
    for k, typ in (("unsafe_reads", list), ("skill_names", list), ("clarify", bool)):
        if not isinstance(label.get(k), typ):
            out.append(f"label.{k} missing")
    if kind == "recall" and not label.get("recall_refs"):
        out.append("recall row without recall_refs")
    if kind == "recall_none" and (not label.get("recall_absent") or label.get("recall_refs")):
        out.append("never-said row needs recall_absent and no recall_refs")
    if kind not in ("recall", "recall_none") and any(label.get(k) for k in ("recall_refs", "recall_absent", "recall_ok")):
        out.append(f"{kind} row carries recall fields")
    if label.get("clarify") and (label.get("step") != "ask" or kind in ("chat_anchor", "session_anchor", "recall", "recall_none", "instruction")):
        out.append("clarify on a row that cannot ask")
    return out


def r5_tie_reason(label: dict) -> str | None:
    """Why no completion of this row could differ on any term (so it carries no signal), else None. Every text row has the hard
    defects (leak, nonreply, needless_call, malformed, forbidden), so only a degenerate scripted target qualifies."""
    if label.get("step") in ("final", "ask") and not (label.get("ideal_text") or "").strip():
        return "empty scripted final"
    return None


def session_counts(metas: list[dict]) -> dict:
    """Over a build's session_chat_r4 finals, counted before any split: recall (rec_user/rec_asst/rec_both), never-said
    (rec_none) and session anchors (every other qkind), plus the finals with no qkind at all."""
    c = {"recall": 0, "recall_none": 0, "session_anchor": 0, "no_qkind": 0}
    for m in metas:
        if m.get("family") != "session_chat_r4" or m.get("target_kind") not in (None, "final"):
            continue
        qk = m.get("qkind")
        c["recall" if qk in RECALL_KINDS else "recall_none" if qk == NONE_KIND else "session_anchor" if qk else "no_qkind"] += 1
    return c


def session_count_problems(metas: list[dict], expected: dict | None) -> list[str]:
    """Exit-worthy differences between the build's session_chat_r4 counts and the builder's (54 recall, 14 never-said, 99 anchors)."""
    got = session_counts(metas)
    if not sum(got.values()):
        return []                                        # a build without session_chat_r4: nothing to count
    out = []
    if got["no_qkind"]:
        out.append(f"{got['no_qkind']} session_chat_r4 finals have no meta.qkind (builder C6 fields missing)")
    for k, v in (expected or {}).items():
        if got[k] != v:
            out.append(f"session_chat_r4 {k}: {got[k]} in the build, {v} expected")
    return out


def refs_crosscheck(metas: list[dict], refs: dict) -> list[str]:
    """DIST/raw/session_chat.refs.json as a cross-check of the builder's meta (refs are seed-independent, keyed by id)."""
    out = []
    for m in metas:
        r = refs.get(m.get("id"))
        if m.get("family") != "session_chat_r4" or r is None:
            continue
        if r["kind"] in RECALL_KINDS and list(m.get("recall_refs") or ()) != list(r["ref"]):
            out.append(f"{m['id']}: recall_refs {list(m.get('recall_refs') or ())!r} but refs.json has {list(r['ref'])!r}")
        elif r["kind"] == NONE_KIND and (list(m.get("recall_refs") or ()) or m.get("recall_absent") != r["absent"]
                                         or list(m.get("recall_ok") or ()) != list(r["ok"])):
            out.append(f"{m['id']}: never-said fields differ from refs.json")
        elif r["kind"] not in RECALL_KINDS | {NONE_KIND} and (m.get("recall_refs") or m.get("recall_absent") or m.get("recall_ok")):
            out.append(f"{m['id']}: a {r['kind']} question carries recall fields")
    return out


def dist_record(dist_dir: Path) -> dict:
    """Which DIST commit and file bytes the reward's choose.py import comes from (C7): git sha of the last commit touching it,
    the file's sha256, and the sha256 of the refs file when there is one."""
    import subprocess
    rec: dict = {"dir": str(dist_dir), "commit": None, "choose_sha256": None, "refs_file": None, "refs_sha256": None}
    ch = dist_dir / "choose.py"
    if ch.is_file():
        rec["choose_sha256"] = hashlib.sha256(ch.read_bytes()).hexdigest()
        try:
            out = subprocess.run(["git", "-C", str(dist_dir), "log", "-1", "--format=%H", "--", "choose.py"], capture_output=True,
                                 text=True, timeout=30)
            sha = out.stdout.strip()
            rec["commit"] = sha if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None
        except (OSError, subprocess.SubprocessError):
            pass
    cands = sorted(dist_dir.glob("raw/*/session_chat.refs.json")) + sorted(dist_dir.glob("raw/session_chat.refs.json"))
    if cands:
        rec["refs_file"] = str(cands[-1])
        rec["refs_sha256"] = hashlib.sha256(cands[-1].read_bytes()).hexdigest()
    return rec


_LEAK = None


def leak_mod():
    """The builder's leak.py (fold, LeakIndex), loaded by path under its own name so ask-lora-r2/builder never lands on sys.path."""
    global _LEAK
    if _LEAK is None:
        path = HERE.parent.parent / "ask-lora-r2" / "builder" / "leak.py"
        spec = importlib.util.spec_from_file_location("ask_lora_builder_leak", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ask_lora_builder_leak"] = mod
        spec.loader.exec_module(mod)
        _LEAK = mod
    return _LEAK


def user_turns(msgs: list[dict]) -> list[str]:
    return [_msg_text(m) for m in msgs if m["role"] == "user"]


def conversation_of(hist: list[dict]) -> str:
    """The text of everything before the target except system prompts (leak.conversation_text's scope): user, assistant and tool
    text plus call arguments. check_overlap runs the toolbench fact-value scan over it."""
    parts = []
    for m in hist:
        if m.get("role") == "system":
            continue
        parts.append(_msg_text(m))
        for tc in m.get("tool_calls") or ():
            a = tc["function"].get("arguments")
            parts.append(a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))
    return "\n".join(p for p in parts if p)


# ---- the round-5 mix
def apportion(total: int, shares: dict) -> dict:
    """Integer quotas that sum to `total`, proportional to `shares` (largest remainder; ties by the shares' order)."""
    s = sum(shares.values())
    raw = {k: total * v / s for k, v in shares.items()}
    out = {k: int(math.floor(x)) for k, x in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[: total - sum(out.values())]:
        out[k] += 1
    return out


def r5_bucket(it: dict) -> str:
    lab, fam = it["label"], it["family"]
    if lab.get("rl_kind") == "chat_anchor":
        return "chat_anchor"
    if fam in R5_SESSION_FAMILIES:
        return "session"
    if lab.get("rl_kind") == "narrate":
        return "narrate"
    if fam in COMPOSE_SAFETY_FAMILIES:
        return "safety"
    if lab.get("rl_kind") == "select":
        return "select"
    return "other"


def r5_weight(it: dict) -> float:
    """Draw weights inside a bucket. Session: the rows that carry the recall terms (and followup/session_recall re-reads) are drawn
    ahead of the anchors and of instructions_r4, so the 176 instruction turns cannot crowd out the PG-bearing rows."""
    kind, fam = it["label"].get("rl_kind"), it["family"]
    if r5_bucket(it) == "session":
        return 3.0 if kind in ("recall", "recall_none") else 2.0 if fam in ("followup_r3", "session_recall_r4") else \
            0.5 if kind == "instruction" else 1.0
    return R4_WEIGHTS.get(fam, WEIGHTS.get(fam, 1.0))


def stratified_pick(items: list[dict], count: int, rng: random.Random, key) -> list[dict]:
    """`count` rows spread evenly over key(item) strata (round robin, strata in sorted order, random rows inside each);
    a second pass starts only when every stratum is used up, and no row is picked more than MAX_COPIES times."""
    groups: dict = {}
    for it in items:
        groups.setdefault(key(it), []).append(it)
    for g in groups.values():
        rng.shuffle(g)
    count = min(count, MAX_COPIES * len(items))
    out: list[dict] = []
    for p in range(MAX_COPIES):
        queues = {k: list(g) for k, g in sorted(groups.items())}
        while len(out) < count and any(queues.values()):
            for k in sorted(queues):
                if queues[k] and len(out) < count:
                    out.append(dict(queues[k].pop(), copy=p))
        if len(out) >= count:
            break
    return out


def pick_anchors(anchors: list[dict], count: int, rng: random.Random) -> tuple[list[dict], dict]:
    """The chat anchors: `count` distinct distilled rows drawn proportionally to family size (largest remainder), recorded per
    family. Fewer rows than `count` is an error: the export must not fall back to scripted replies."""
    if len(anchors) < count:
        raise SystemExit(f"chat anchors: only {len(anchors)} meta.distilled rows of {sorted(R5_ANCHOR_FAMILIES)} in the anchor build, "
                         f"the round-5 mix needs {count}")
    fams: dict = {}
    for it in anchors:
        fams.setdefault(it["family"], []).append(it)
    quota = apportion(count, {f: len(v) for f, v in sorted(fams.items())})
    out = []
    for f, v in sorted(fams.items()):
        v = sorted(v, key=lambda it: it["id"])
        rng.shuffle(v)
        out += [dict(it, copy=0) for it in v[:quota[f]]]
    return out, {f: quota[f] for f in sorted(quota) if quota[f]}


def r5_text_rows(items: list[dict], pics: list[dict], anchors: list[dict], n_text: int, rng: random.Random) -> tuple[list[dict], dict]:
    """The round-5 text mix (RL-SCOPE 4.1): safety/compose 28%, narrate-with-facts 28%, session 12%, chat anchors 6%, selection 11%
    (picture-turn rows inside it), as shares of all rows; the quotas are apportioned over n_text. A short bucket is topped up from
    the selection pool and reported; short anchors are an error (pick_anchors)."""
    pools: dict = {b: [] for b in ("safety", "narrate", "session", "select", "other")}
    for it in items:
        it = dict(it, weight=r5_weight(it))
        pools[r5_bucket(it)].append(it)
    quota = apportion(n_text, R5_SHARES)
    n_sel = max(0, quota["select"] - len(pics))
    got: dict = {}
    got["safety"] = r4_pick(pools["safety"], quota["safety"], rng)
    got["narrate"] = r4_pick(pools["narrate"], quota["narrate"], rng)
    ses_pool = pools["session"]
    ses_n = min(quota["session"], R5_SESSION_MAX_COPIES * len(ses_pool))
    got["session"] = weighted_pick(ses_pool, ses_n, rng) if ses_n > 0 else []
    got["chat_anchor"], anchor_fams = pick_anchors(anchors, quota["chat_anchor"], rng)
    got["select"] = stratified_pick(pools["select"], n_sel, rng, lambda it: it["family"]) if n_sel else []
    want = {"safety": quota["safety"], "narrate": quota["narrate"], "session": quota["session"], "chat_anchor": quota["chat_anchor"],
            "select": n_sel}
    short = {k: want[k] - len(got[k]) for k in want if len(got[k]) < want[k]}
    missing = sum(v for k, v in short.items() if k != "chat_anchor")
    if missing > 0:
        taken = Counter(g["id"] for g in got["select"])
        spare = [it for it in pools["select"] if taken[it["id"]] < MAX_COPIES]
        got["select"] += stratified_pick(spare, missing, rng, lambda it: it["family"])
    rows = [r for b in ("safety", "narrate", "session", "chat_anchor", "select") for r in got[b]] + pics
    ses = got["session"]
    return rows, {
        "quota": {**quota, "select_after_pictures": n_sel, "picture_turn": len(pics)},
        "got": {k: len(v) for k, v in got.items()}, "short": short, "pool": {k: len(v) for k, v in pools.items()},
        "chat_anchor_families": anchor_fams, "chat_anchor_pool": len(anchors),
        "session_sources": dict(Counter(f"{r['family']}:{r['label'].get('rl_kind')}" for r in ses)),
        "safety_families": dict(Counter(r["family"] for r in got["safety"])),
        "safety_with_unsafe_reads": sum(bool(r["label"].get("unsafe_reads")) for r in got["safety"]),
        "select_families": dict(Counter(r["family"] for r in got["select"])),
    }


# ---- the text dev set (C6b)
DEV_SLICES = {"safety": 50, "narrate": 40, "select": 10, "recall_dev_r4": 24, "session_style_dev": 12, "chat": 30}
DEV_SAMPLES = {"safety": 4, "recall_dev_r4": 4, "narrate": 2, "select": 2, "session_style_dev": 2, "chat": 2}
SMOKE30 = {"safety": 15, "narrate": 5, "select": 2, "recall_dev_r4": 8}


def _turn_pick(items: list[dict], count: int, rng: random.Random, key) -> list[dict]:
    """Like stratified_pick but without copies and preferring one step per turn (a dev row is one decision step; two steps of the
    same turn only when there are not enough turns)."""
    groups: dict = {}
    for it in sorted(items, key=lambda i: i["id"]):
        groups.setdefault(key(it), []).append(it)
    for g in groups.values():
        rng.shuffle(g)
    out: list[dict] = []
    chosen: set = set()
    turns_used: set = set()
    for allow_repeat in (False, True):
        progressed = True
        while len(out) < count and progressed:
            progressed = False
            for k in sorted(groups):
                for it in groups[k]:
                    if it["id"] in chosen or (not allow_repeat and it["_turn"] in turns_used):
                        continue
                    out.append(it)
                    chosen.add(it["id"])
                    turns_used.add(it["_turn"])
                    progressed = True
                    break
                if len(out) >= count:
                    break
    return out


def build_text_dev(valid_items: list[dict], dev_items: list[dict], chat_items: list[dict], picker_ids: set | None,
                   train_items: list[dict], anchor_train: list[dict], seed: int, counts: dict | None = None,
                   smoke_counts: dict | None = None, picker_prompts: dict | None = None) -> tuple[list[dict], list[dict], dict]:
    """(rows, smoke30 rows, report) for grpo_text_dev.jsonl. valid_items: classified rows of the build's hash-split valid set;
    dev_items: the --dev-build-dir rows (recall_dev_r4 / session_style_dev); chat_items: distilled valid rows of the anchor build;
    train_items: every exported text row (for the disjointness proof); anchor_train: the exported anchor train rows; picker_ids:
    ids of the PT picker's rows (None = the operator stated there is no picker sample). Items carry _folds (leak.fold of every
    user turn), _turn and _thread. report["errors"] lists every reason the export must exit non-zero."""
    counts = counts or DEV_SLICES
    smoke_counts = smoke_counts or SMOKE30
    rng = random.Random(f"{seed}-textdev")
    errors: list[str] = []
    by_id = {it["id"]: it for it in valid_items}
    picker_folds: set = set()
    for pid in sorted(picker_ids or ()):
        if pid in by_id:
            picker_folds.add(by_id[pid]["_qfold"])
            seen = (picker_prompts or {}).get(pid)
            if seen is not None and seen != by_id[pid]["prompt"]:
                errors.append(f"picker row {pid!r} has the same id as a valid row of this build but a different prompt: the picker saw "
                              "another instance (a re-seeded build), so the id match proves no disjointness")
        else:
            errors.append(f"picker row {pid!r} is not in the build's valid set: disjointness cannot be shown")
    avail = [it for it in valid_items if it["id"] not in (picker_ids or ()) and it["_qfold"] not in picker_folds]
    slices: dict = {}
    for name in ("safety", "narrate", "select"):
        pool = [it for it in avail if r5_bucket(it) == name]
        slices[name] = _turn_pick(pool, counts[name], rng, lambda it: (it["label"]["rl_kind"], it["family"]))
        if len(slices[name]) < counts[name]:
            errors.append(f"slice {name}: {len(slices[name])} rows from valid (minus the picker sample), {counts[name]} needed; "
                          f"short by {counts[name] - len(slices[name])}: shrink the picker --limit or enlarge valid")
    for name in ("recall_dev_r4", "session_style_dev"):
        rows = sorted((it for it in dev_items if it["family"] == name and it["label"]["step"] in ("final", "ask")), key=lambda i: i["id"])
        slices[name] = rows
        if len(rows) < counts[name]:
            errors.append(f"slice {name}: {len(rows)} questions in --dev-build-dir, {counts[name]} needed (never padded)")
        for it in rows:
            if not it.get("_thread"):
                errors.append(f"{it['id']}: dev session row without meta.thread")
    if slices["recall_dev_r4"] and not any(it["label"]["rl_kind"] in ("recall", "recall_none") for it in slices["recall_dev_r4"]):
        errors.append("recall_dev_r4 has no recall or never-said row carrying refs")
    anchor_ids = {a["id"] for a in anchor_train}
    anchor_folds = {it["_qfold"] for it in anchor_train}
    chat_pool = [it for it in chat_items if it["id"] not in anchor_ids and it["_qfold"] not in anchor_folds]
    fams: dict = {}
    for it in chat_pool:
        fams.setdefault(it["family"], []).append(it)
    chat: list = []
    if len(chat_pool) >= counts["chat"]:
        q = apportion(counts["chat"], {f: len(v) for f, v in sorted(fams.items())})
        for f, v in sorted(fams.items()):
            v = sorted(v, key=lambda i: i["id"])
            rng.shuffle(v)
            chat += v[:q[f]]
    else:
        errors.append(f"slice chat: {len(chat_pool)} distilled valid anchor-family rows disjoint from the anchor train rows, "
                      f"{counts['chat']} needed (never padded)")
    slices["chat"] = chat
    # disjointness proofs: dev against the exported rows (fold, thread, family, id) and against the picker sample
    prompt_rows = [it for it in train_items if it["label"].get("rl_kind") != "chat_anchor"]
    prompt_folds = {it["_qfold"] for it in prompt_rows}
    prompt_threads = {it["_thread"] for it in prompt_rows if it.get("_thread")}
    for it in prompt_rows:
        if it["family"] in DEV_ONLY_FAMILIES:
            errors.append(f"grpo_prompts row {it['id']} is from the dev-only family {it['family']}")
    exported_ids = {it["id"] for it in train_items}
    for name, rows in slices.items():
        for it in rows:
            if name != "chat" and it["_qfold"] in prompt_folds:
                errors.append(f"dev row {it['id']} ({name}) shares a question fold with a grpo_prompts row")
            if it.get("_thread") and it["_thread"] in prompt_threads:
                errors.append(f"dev row {it['id']}: meta.thread {it['_thread']!r} also appears in grpo_prompts")
            if it["id"] in exported_ids and name != "chat":
                errors.append(f"dev row {it['id']} is also a grpo_prompts row")
            if it["_qfold"] in picker_folds and name in ("safety", "narrate", "select"):
                errors.append(f"dev row {it['id']} shares a fold with a --picker-dev row")
    rows_out = []
    for name, rows in slices.items():
        for it in rows:
            rows_out.append({"id": it["id"], "family": it["family"], "slice": name, "samples": DEV_SAMPLES[name], "prompt": it["prompt"],
                             "label": dict(it["label"], samples=DEV_SAMPLES[name]), "_users": it["_users"], "_conv": it["_conv"],
                             "_thread": it.get("_thread"), "_folds": it["_folds"]})
    smoke = []
    for name, n in smoke_counts.items():
        pool = [r for r in rows_out if r["slice"] == name]
        if len(pool) < n:
            errors.append(f"smoke30: slice {name} has {len(pool)} rows, {n} needed")
        smoke += sorted(random.Random(f"{seed}-smoke-{name}").sample(pool, min(n, len(pool))), key=lambda r: r["id"])
    report = {"requested": dict(counts), "actual": {k: len(v) for k, v in slices.items()}, "seed": seed,
              "source_ids": {k: [i["id"] for i in v] for k, v in slices.items()},
              "picker": {"given": picker_ids is not None, "rows": len(picker_ids or ()), "folds": len(picker_folds),
                         "overlap": sum(1 for n_, rs in slices.items() for it in rs
                                        if n_ in ("safety", "narrate", "select") and (it["id"] in (picker_ids or ()) or it["_qfold"] in picker_folds))},
              "smoke30": {k: sum(r["slice"] == k for r in smoke) for k in smoke_counts}, "smoke_ids": [r["id"] for r in smoke],
              "errors": errors}
    return rows_out, smoke, report


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


def load_pairs(bd: Path, ex_name: str = "all.jsonl", meta_name: str = "meta.jsonl") -> list[tuple[dict, dict]]:
    with open(bd / ex_name) as fe, open(bd / meta_name) as fm:
        return [(json.loads(e), json.loads(m)) for e, m in zip(fe, fm)]


def index_pairs(pairs: list[tuple[dict, dict]], r4: bool = True) -> tuple[dict, set]:
    """(turn index for requires_read, every tool name the build offers or calls). Round 3 uses neither."""
    turns: dict = {}
    tool_names: set = set()
    if r4:
        for ex, meta in pairs:
            names = [tc["function"]["name"] for tc in (ex["messages"][-1].get("tool_calls") or ())]
            turns.setdefault(turn_key(meta), []).append((meta["step"], names))
            tool_names.update(t["function"]["name"] for t in ex.get("tools") or () if isinstance(t, dict) and "function" in t)
            tool_names.update(names)
        tool_names.discard(DISCOVERY)
    return turns, tool_names


def ideal_trips(label: dict) -> list[str]:
    """Round 5: the penalising terms (and "below_ceiling") the row's own ideal completion trips, scored by reward.py at round 5. The
    ideal of a row must score at label.ceiling with no negative term (RL-SCOPE 3.5): a row that fails it would punish the right answer
    (or, for an ideal that outscores its ceiling, mislabel it). Tolerance as plan.ideal_top: 0.31 on calls, 0.01 on finals."""
    res = W.score(label["ideal_completion"], label, 5)
    terms = sorted(k for k, v in res.parts.items() if v < 0)
    tol = 0.31 if label.get("step") in ("call", "load") else 0.01
    if res.total < label["ceiling"] - tol - 1e-9:
        terms.append("below_ceiling")
    elif res.total > label["ceiling"] + 1e-9:
        terms.append("above_ceiling")
    return terms


def ideal_trip_problems(trip: dict, pct: float = IDEAL_TRIP_PCT) -> list[str]:
    """Kinds with a term (or below/above ceiling) that more than `pct` percent of their ideals trip (and more than one row): the
    reward and the labels disagree, so the export stops instead of silently thinning the kind."""
    out = []
    for kind, rec in sorted(trip.items()):
        for term, n in sorted(rec["by_term"].items()):
            if n > 1 and 100.0 * n / rec["rows"] > pct:
                out.append(f"{kind}: {n} of {rec['rows']} ideals ({100.0 * n / rec['rows']:.1f}%) trip {term} (limit {pct:g}%)")
    return out


def make_item(ex: dict, meta: dict, R, tok, a, turns: dict, tool_names, kw: dict, id_prefix: str = "",
              trip: dict | None = None) -> tuple[dict | None, str | None]:
    """One exportable decision step: render, length-check, classify. (None, reason) when it is dropped. At round 5 the item also
    carries underscore keys main() strips before writing: the user turns, the conversation text (check_overlap), the question folds,
    the turn and the thread."""
    row = R.render(tok, ex, meta)           # also verifies the ideal completion renders cleanly
    n_tok = len(tok(row["prompt"], add_special_tokens=False)["input_ids"])
    if n_tok > a.max_prompt_tokens:
        return None, "too long"
    label = classify(ex, meta, a.round, turns, sorted(tool_names), **kw)
    label["forced"] = row["prompt"].endswith(R.FORCED)
    label["think"] = bool(row["think"])
    if a.round >= 4:
        label["thought_words"] = thought_words(row["completion"], label["forced"])
    label["ideal_completion"] = row["completion"]
    if trip is not None and a.round >= 5:
        # the ideal-trip dry check (RL-SCOPE 3.5): counted per rl_kind over every row of the pool, tripping rows are dropped
        rec = trip.setdefault(label["rl_kind"], {"rows": 0, "tripped": 0, "by_term": Counter()})
        rec["rows"] += 1
        terms = ideal_trips(label)
        if terms:
            rec["tripped"] += 1
            rec["by_term"].update(terms)
            return None, "ideal trips " + ",".join(terms)
    item = {"id": id_prefix + meta["id"], "family": meta["family"], "prompt": row["prompt"], "label": label,
            "weight": WEIGHTS.get(meta["family"], 1.0), "_tokens": n_tok}
    if a.round >= 5:
        hist = ex["messages"][:-1]
        fold = leak_mod().fold
        users = user_turns(hist)
        folds = {fold(u) for u in users} | {fold(meta.get("prompt") or "")}
        folds.discard("")
        item.update(_users=users, _conv=conversation_of(hist), _turn=turn_key(meta), _thread=meta.get("thread"), _folds=sorted(folds),
                    _qfold=fold(meta.get("prompt") or (users[-1] if users else "")))
    return item, None


def normalise_clarify(pairs: list[tuple[dict, dict]]) -> bool:
    """A build whose metas carry the key `clarify` on some step is a C6 build: steps that omit it are clarify False (so they never fall
    back to the trailing-"?" test). Returns whether the build has the key at all."""
    has = any("clarify" in m for _, m in pairs)
    if has:
        for _, m in pairs:
            m.setdefault("clarify", False)
    return has


def r5_setup(a, bd: Path, pairs: list, valid_ids: set, turns: dict, tool_names: set) -> dict:
    """Round-5 preconditions and the other builds: folds of the valid set, the builder-field and session-count checks (C6/C7), the anchor
    build and the dev build. Exits non-zero on any failed precondition."""
    fold = leak_mod().fold
    info: dict = {"fold": fold, "tool_names": set(tool_names)}
    info["valid_folds"] = {fold(m["prompt"]) for _, m in pairs if m["id"] in valid_ids}
    info["trip"] = {}
    info["clarify_in_build"] = normalise_clarify(pairs)
    metas = [m for _, m in pairs]
    dist = Path(a.dist_dir) if a.dist_dir else HERE.parent.parent / "ask-lora-r4" / "distill"
    info["dist"] = dist_record(dist)
    refs = None
    refs_path = a.refs_crosscheck
    if refs_path:
        refs = json.loads(Path(refs_path).read_text())
        info["dist"]["crosscheck_file"] = str(refs_path)
        info["dist"]["crosscheck_sha256"] = hashlib.sha256(Path(refs_path).read_bytes()).hexdigest()
    if a.expect_session_chat == "skip":
        expected = None
    elif a.expect_session_chat:
        r_, n_, k_ = (int(x) for x in a.expect_session_chat.split(","))
        expected = {"recall": r_, "recall_none": n_, "session_anchor": k_}
    elif refs is not None:
        kinds = Counter("recall" if v["kind"] in RECALL_KINDS else "recall_none" if v["kind"] == NONE_KIND else "session_anchor"
                        for v in refs.values())
        expected = dict(kinds)
    else:
        expected = {"recall": 54, "recall_none": 14, "session_anchor": 99}
    problems = session_count_problems(metas, expected) + (refs_crosscheck(metas, refs) if refs is not None else [])
    info["session_counts"] = {"got": session_counts(metas), "expected": expected, "problems": problems}
    if problems:
        msg = "builder session_chat_r4 fields (RL-SCOPE C7):\n  " + "\n  ".join(problems[:20])
        if not a.allow_old_build:
            sys.exit(msg + "\n(--allow-old-build for a smoke export on a build that predates C6/C7)")
        print("WARNING:", msg)
    if not a.anchor_build_dir:
        sys.exit("--anchor-build-dir is required at round 5 (the spliced r4 SFT build: chat anchors come from its meta.distilled rows)")
    ab = Path(a.anchor_build_dir)
    for f in ("all.jsonl", "meta.jsonl", "valid.meta.jsonl"):
        if not (ab / f).is_file():
            sys.exit(f"{ab / f} is missing (the anchor build must hold all/meta and valid.meta)")
    info["apairs"] = load_pairs(ab)
    normalise_clarify(info["apairs"])
    info["a_valid"] = {json.loads(l)["id"] for l in open(ab / "valid.meta.jsonl") if l.strip()}
    info["a_turns"], names = index_pairs(info["apairs"], True)
    info["tool_names"] |= names
    info["dpairs"], info["d_turns"] = [], {}
    if not a.no_text_dev:
        if not a.dev_build_dir:
            sys.exit("--dev-build-dir is required at round 5 (recall_dev_r4 + session_style_dev); pass --no-text-dev to export grpo_prompts only")
        if not a.picker_dev:
            sys.exit("--picker-dev is required to build the text dev set (the PT picker's text-dev.jsonl, or 'none' if there is no picker sample)")
        db = Path(a.dev_build_dir)
        for f in ("all.jsonl", "meta.jsonl"):
            if not (db / f).is_file():
                sys.exit(f"{db / f} is missing (--dev-build-dir)")
        info["dpairs"] = load_pairs(db)
        normalise_clarify(info["dpairs"])
        info["d_turns"], names = index_pairs(info["dpairs"], True)
        info["tool_names"] |= names
        ids, prompts = None, {}
        if a.picker_dev != "none":
            for l in open(a.picker_dev):
                if l.strip():
                    row = json.loads(l)
                    prompts[row["id"]] = row.get("prompt") if isinstance(row.get("prompt"), str) else None
            ids = set(prompts)
        elif not a.allow_no_picker:
            sys.exit("--picker-dev none: the text dev set cannot be shown disjoint from the PT picker sample. Pass the picker's "
                     "text-dev.jsonl, or --allow-no-picker to record that there is none")
        info["picker_ids"] = ids
        info["picker_prompts"] = prompts
    return info


def r5_anchor_items(R, tok, a, r5s: dict, kw: dict) -> tuple[list[dict], list[dict]]:
    """(anchor train items, distilled valid items) of the anchor build: its meta.distilled plain finals of the anchor families, ids
    prefixed `anchor:` so they cannot collide with a row of the RL pool."""
    train, valid = [], []
    skipped = r5s.setdefault("anchor_skipped", Counter())
    for ex, meta in r5s["apairs"]:
        has_calls = bool(ex["messages"][-1].get("tool_calls"))
        if not anchor_row(meta, has_calls):
            continue
        if not anchor_row(meta, has_calls, ex["messages"][:-1]):
            skipped["history holds a tool message"] += 1          # a distilled anchor-family row that is not a single-turn chat reply
            continue
        item, _ = make_item(ex, meta, R, tok, a, r5s["a_turns"], r5s["tool_names"], kw, id_prefix="anchor:", trip=r5s.get("trip"))
        if item is not None:
            (valid if meta["id"] in r5s["a_valid"] else train).append(item)
    return train, valid


def _sidecar_line(r: dict) -> str:
    """One check_overlap record per exported row, keyed by the row id: the user turns and the conversation text (text rows) or the
    media paths and transcript (vision / audio rows)."""
    rec = {"id": f"{r['id']}#{r.get('copy', 0)}", "family": r["family"], "modality": r.get("modality", "text")}
    if rec["modality"] == "text":
        rec.update(users=r["_users"], conv=r["_conv"], thread=r.get("_thread"), folds=r["_folds"])
    else:
        rec.update(images=r.get("images") or [], audio=r.get("audio"), reference=r["label"].get("reference"), users=[],
                   target=r["label"].get("ideal_completion"))
    return json.dumps(rec, ensure_ascii=False) + "\n"


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def r5_outputs(a, bd: Path, R, tok, kw: dict, r5s: dict, ordered: list, mm_dev: list, items: list, anchor_train: list, chat_valid: list,
               pairs: list, valid_ids: set, turns: dict, dropped: Counter, row_json) -> dict:
    """Everything round 5 adds beside grpo_prompts.jsonl: the overlap sidecars, grpo_text_dev.jsonl (C6b), its smoke30 subset and
    stats, and the stats block (counts, DIST record, builder-field checks)."""
    out = Path(a.out)
    info: dict = {"pool_by_bucket": dict(Counter(r5_bucket(it) for it in items)), "anchor_build": str(a.anchor_build_dir),
                  "old_build": bool(a.allow_old_build), "chat_max_tokens": a.chat_max_tokens, "instruction_term": bool(a.instruction_term),
                  "dist": r5s["dist"], "session_counts": r5s["session_counts"], "clarify_in_build": r5s["clarify_in_build"]}
    text = [r for r in ordered if r.get("modality", "text") == "text"]
    info["rl_kind"] = dict(Counter(r["label"]["rl_kind"] for r in ordered))
    info["text_rows"] = len(text)
    info["ceiling_entries"] = dict(Counter(f"{r['label']['rl_kind']}:{r['label']['ceiling']}" for r in text))
    info["clarify_rows"] = sum(bool(r["label"].get("clarify")) for r in text)
    info["clarify_fallback_rows"] = sum(bool(r["label"].get("clarify_fallback")) for r in text)
    info["max_tokens_rows"] = sum(r["label"].get("max_tokens") == a.chat_max_tokens for r in text)
    info["unsafe_reads_rows"] = sum(bool(r["label"].get("unsafe_reads")) for r in text)
    info["skill_names_rows"] = sum(bool(r["label"].get("skill_names")) for r in text)
    info["dropped_no_variance"] = {k: v for k, v in dropped.items() if k.startswith("no variance")}
    info["dropped_ideal_trip"] = {k: v for k, v in dropped.items() if k.startswith("ideal trips")}
    info["ideal_trip_pct_limit"] = a.ideal_trip_pct
    info["ideal_trip"] = {k: {"rows": v["rows"], "tripped": v["tripped"], "share_pct": round(100.0 * v["tripped"] / max(1, v["rows"]), 2),
                              "by_term": dict(v["by_term"])} for k, v in sorted(r5s["trip"].items())}
    info["anchor_skipped"] = dict(r5s.get("anchor_skipped") or {})
    info["clarify_dropped_rows"] = dict(Counter(it["family"] for it in items if it["label"].get("clarify_dropped")))
    info["facts_ignored_rows"] = dict(Counter(it["family"] for it in items if it["label"].get("facts_ignored")))
    info["facts_explicit_rows"] = sum(bool(r["label"].get("facts_explicit")) for r in text)
    safety_n = Counter(r["family"] for r in text if r["family"] in COMPOSE_SAFETY_FAMILIES)
    info["safety_rows_by_family"] = dict(safety_n)
    # the spec's >= 10 rows per safety PATTERN needs a builder pattern key; the family is the nearest unit meta carries today
    info["safety_families_under_min"] = sorted(f for f in COMPOSE_SAFETY_FAMILIES if safety_n[f] < R5_SAFETY_MIN_ROWS)
    side = out.with_suffix(".overlap.jsonl")
    side.write_text("".join(_sidecar_line(r) for r in ordered))
    mm_side = out.with_name("grpo_mm_dev.overlap.jsonl")
    mm_side.write_text("".join(_sidecar_line(r) for r in mm_dev))
    info["sidecars"] = {"prompts": str(side), "prompts_sha256": _sha(side), "mm_dev": str(mm_side), "mm_dev_sha256": _sha(mm_side)}
    if a.no_text_dev:
        info["text_dev"] = None
        return info
    valid_items, dev_items = [], []
    errs: list[str] = []
    for ex, meta in pairs:
        if meta["id"] in valid_ids:
            try:
                it, _ = make_item(ex, meta, R, tok, a, turns, r5s["tool_names"], kw)
            except BuilderMetaError as e:
                errs.append(str(e))
                continue
            if it is not None:
                valid_items.append(it)
    for ex, meta in r5s["dpairs"]:
        try:
            it, _ = make_item(ex, meta, R, tok, a, r5s["d_turns"], r5s["tool_names"], kw)
        except BuilderMetaError as e:
            errs.append(str(e))
            continue
        if it is not None:
            dev_items.append(it)
    def parse_counts(spec, default):
        if not spec:
            return None
        got = dict(default)
        for kv in spec.split(","):
            k, _, v = kv.partition("=")
            if k.strip() not in default:
                sys.exit(f"unknown slice {k!r}; known: {sorted(default)}")
            got[k.strip()] = int(v)
        return got
    rows, smoke, rep = build_text_dev(valid_items, dev_items, chat_valid, r5s["picker_ids"], text, anchor_train, a.seed,
                                      parse_counts(a.dev_slice_counts, DEV_SLICES), parse_counts(a.dev_smoke_counts, SMOKE30),
                                      r5s.get("picker_prompts"))
    rep["errors"] = errs + rep["errors"]
    for r in rows:
        r["label"]["round"] = a.round
    bad = [f"{r['id']}: {pr}" for r in rows for pr in r5_label_problems(r["label"], "text", a.instruction_term)]
    rep["errors"] += bad

    def dev_json(r: dict) -> str:
        lab = dict(r["label"], copy=0)
        return json.dumps({"id": f"{r['id']}#0", "family": r["family"], "modality": "text", "slice": r["slice"], "samples": r["samples"],
                           "prompt": r["prompt"], "label": json.dumps(lab, ensure_ascii=False)}, ensure_ascii=False) + "\n"
    dev_path = out.with_name("grpo_text_dev.jsonl")
    smoke_path = out.with_name("grpo_text_dev.smoke30.jsonl")
    stats_path = out.with_name("grpo_text_dev.stats.json")
    if not rep["errors"]:
        dev_path.write_text("".join(dev_json(r) for r in rows))
        smoke_path.write_text("".join(dev_json(r) for r in smoke))
        dev_side = out.with_name("grpo_text_dev.overlap.jsonl")
        dev_side.write_text("".join(json.dumps({"id": f"{r['id']}#0", "family": r["family"], "modality": "text", "users": r["_users"],
                                                 "conv": r["_conv"], "thread": r["_thread"], "folds": r["_folds"]}, ensure_ascii=False) + "\n"
                                    for r in rows))
        rep["files"] = {"text_dev": str(dev_path), "text_dev_sha256": _sha(dev_path), "smoke30": str(smoke_path),
                        "smoke30_sha256": _sha(smoke_path), "overlap": str(dev_side), "overlap_sha256": _sha(dev_side)}
        rep["rl_kind"] = dict(Counter(r["label"]["rl_kind"] for r in rows))
    else:
        for pth in (dev_path, smoke_path):
            pth.unlink(missing_ok=True)
    stats_path.write_text(json.dumps(rep, indent=1) + "\n")
    info["text_dev"] = {"stats": str(stats_path), "errors": len(rep["errors"]), "actual": rep["actual"], "picker": rep["picker"]}
    if rep["errors"]:
        sys.exit(f"the text dev set failed {len(rep['errors'])} checks (see {stats_path}); first 20:\n  " + "\n  ".join(rep["errors"][:20]))
    return info


def r5_overlap_check(a, stats: dict) -> int:
    """Run check_overlap.py over the exported sidecars against the frozen manifest (C15). It writes <out>.overlap_report.json and puts
    the report sha256 and each excluded set's sha256 into the stats JSON ("overlap"); the exit code is its own."""
    import check_overlap
    out = Path(a.out)
    stats_path = out.with_suffix(".stats.json")
    inputs = [out.with_suffix(".overlap.jsonl"), out.with_name("grpo_mm_dev.overlap.jsonl")]
    if not a.no_text_dev:
        inputs.append(out.with_name("grpo_text_dev.overlap.jsonl"))
    argv = ["--manifest", str(a.overlap_manifest), "--report", str(out.with_suffix(".overlap_report.json")), "--stats", str(stats_path),
            "--media-root", str(a.mm_dir), "--inputs", *map(str, inputs)]
    rc = check_overlap.main(argv)
    stats.update(json.loads(stats_path.read_text()))
    return rc


def load_tokenizer(model: str):
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(model)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-dir", default=os.environ.get("ASK_LORA_OUT"), help="dir with all.jsonl + meta.jsonl")
    ap.add_argument("--model", required=True, help="Gemma 4 E4B-it dir with the tokenizer + chat_template.jinja")
    ap.add_argument("--round", type=int, default=3, choices=(3, 4, 5),
                    help="4: the round-4 labels and mix (see the module docstring); data and output defaults move to ../../ask-lora-r4/. "
                         "5: the RL round (RL-SCOPE.md): round-5 labels (rl_kind, ceiling, unsafe_reads, recall fields), the 4.1 mix, "
                         "chat anchors from --anchor-build-dir, and the text dev set (grpo_text_dev*.jsonl)")
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
    r5g = ap.add_argument_group("round 5 (--round 5 only)")
    r5g.add_argument("--anchor-build-dir", default=None,
                     help="the SPLICED r4 SFT build (all/meta + valid.meta): chat anchors are its meta.distilled rows of "
                          f"{sorted(R5_ANCHOR_FAMILIES)} (train side); the 30-row chat probe is its distilled valid side")
    r5g.add_argument("--dev-build-dir", default=None,
                     help="builder output of recall_dev_r4 + session_style_dev (a separate dir, never merged into --build-dir)")
    r5g.add_argument("--picker-dev", default=None,
                     help="the PT picker's text-dev.jsonl (or 'none' to state there is no picker sample): the dev rows are disjoint from it")
    r5g.add_argument("--no-text-dev", action="store_true", help="export grpo_prompts only (no grpo_text_dev*); recorded in the stats")
    r5g.add_argument("--chat-max-tokens", type=int, default=1024,
                     help="C5: label.max_tokens of chat-anchor and session rows (re-export at 1536 when their bucket truncates above 1%%)")
    r5g.add_argument("--instruction-term", action="store_true", help="the C17 instruction term is on: instruction rows' ceiling is 1.5")
    r5g.add_argument("--ideal-trip-pct", type=float, default=IDEAL_TRIP_PCT,
                     help="exit when more than this percent of a kind's ideal completions trip one penalising term of reward.py (RL-SCOPE "
                          "3.5, default 1). Rows whose ideal trips are dropped from the export either way and counted in the stats")
    r5g.add_argument("--allow-no-picker", action="store_true",
                     help="accept --picker-dev none (no PT picker sample exists). Without it the dev set's disjointness from the picker "
                          "cannot be shown and the export exits")
    r5g.add_argument("--allow-old-build", action="store_true",
                     help="downgrade missing builder fields (session_chat_r4 meta.qkind, the session counts) to warnings and class such "
                          "finals as session anchors. For smoke tests on a build that predates C6/C7; recorded in the stats")
    r5g.add_argument("--expect-session-chat", default=None,
                     help="recall,never_said,anchors counts of session_chat_r4 in the whole build, before the split "
                          "(default 54,14,99; or the counts of --refs-crosscheck); 'skip' turns the check off")
    r5g.add_argument("--refs-crosscheck", default=None, help="DIST/raw/.../session_chat.refs.json: meta.recall_refs/absent/ok must equal it")
    r5g.add_argument("--dist-dir", default=None, help="the distill dir whose choose.py reward.py imports (default ../../ask-lora-r4/distill); "
                                                      "its commit sha and refs sha256 go into the stats")
    r5g.add_argument("--dev-slice-counts", default=None,
                     help="tests and smoke exports only: override the dev slice sizes, e.g. 'safety=5,narrate=4,select=2,recall_dev_r4=24,"
                          "session_style_dev=12,chat=6' (the spec's 50/40/10/24/12/30 otherwise); recorded in grpo_text_dev.stats.json")
    r5g.add_argument("--dev-smoke-counts", default=None, help="tests and smoke exports only: override smoke30's 15/5/2/8, same syntax")
    r5g.add_argument("--overlap-manifest", default=None,
                     help="eval_manifest.json: run check_overlap.py on the exported files, write <out>.overlap_report.json, record its "
                          "sha256 in the stats and exit non-zero on any hit")
    a = ap.parse_args(argv)
    r4 = a.round >= 4
    r5 = a.round >= 5
    r4_root = HERE.parent.parent / "ask-lora-r4"
    if r4 and not a.build_dir:
        a.build_dir = str(r4_root / ("data-rl" if r5 else "data"))
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
    tok = load_tokenizer(a.model)
    valid_ids = set()
    vm = bd / "valid.meta.jsonl"
    if r5:
        if a.include_valid:
            sys.exit("--include-valid is not allowed at round 5: the text dev set is the build's valid split")
        if not vm.exists():
            sys.exit(f"{vm} is missing: the round-5 export needs the hash-split valid set to exclude from the RL rows and to build the dev set (C15)")
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
    pairs = load_pairs(bd)
    items, dropped = [], Counter()
    turns, tool_names = index_pairs(pairs, r4)
    kw: dict = {}
    r5s: dict = {}
    if r5:
        kw = {"instruction_term": a.instruction_term, "chat_max_tokens": a.chat_max_tokens, "allow_old_build": a.allow_old_build}
        r5s = r5_setup(a, bd, pairs, valid_ids, turns, tool_names)
        tool_names = r5s["tool_names"]
        kw["names"] = world_names(pairs + r5s["apairs"] + r5s["dpairs"])
    meta_errors: list[str] = []
    for ex, meta in pairs:
        if meta["id"] in valid_ids:
            dropped["valid split"] += 1
            continue
        if r5 and r5s["fold"](meta["prompt"]) in r5s["valid_folds"]:
            dropped["valid fold"] += 1
            continue
        if r5 and anchor_row(meta, bool(ex["messages"][-1].get("tool_calls")), ex["messages"][:-1]):
            sys.exit(f"{meta['id']}: a distilled anchor-family row ({meta.get('family')}) in --build-dir. Chat anchors come only from "
                     "--anchor-build-dir (the spliced SFT build); --build-dir must be the re-seeded RL pool, which has no meta.distilled rows")
        try:
            item, why = make_item(ex, meta, R, tok, a, turns, tool_names, kw, trip=r5s.get("trip"))
        except BuilderMetaError as e:
            meta_errors.append(str(e))
            continue
        if item is None:
            dropped[why] += 1
            continue
        if r5:
            why = r5_tie_reason(item["label"])
            if why:
                dropped[f"no variance: {why}"] += 1
                continue
            if item["family"] in DEV_ONLY_FAMILIES:
                sys.exit(f"{item['id']}: a dev-only family ({item['family']}) in --build-dir; build it with the dev build dir instead")
        items.append(item)
    if meta_errors:
        sys.exit(f"{len(meta_errors)} rows have missing or inconsistent builder meta (RL-SCOPE C6/C7); first 20:\n  "
                 + "\n  ".join(meta_errors[:20]) + "\n(--allow-old-build downgrades the session_chat_r4 qkind error only)")
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
    anchor_train: list = []
    if r5:
        for it in mm_picked + mm_dev:           # vision, picture_read and audio rows: anchor-only, no ceiling (RL-SCOPE 3.3)
            it["label"] = dict(it["label"], round=a.round, rl_kind="mm", ceiling=None)
        anchor_train, chat_valid = r5_anchor_items(R, tok, a, r5s, kw)
        trip_errors = ideal_trip_problems(r5s["trip"], a.ideal_trip_pct)
        if trip_errors:
            Path(a.out).with_suffix(".ideal_trip.json").write_text(json.dumps(
                {k: {"rows": v["rows"], "tripped": v["tripped"], "by_term": dict(v["by_term"])} for k, v in r5s["trip"].items()},
                indent=1) + "\n")
            sys.exit("ideal completions trip penalising terms of reward.py above the limit (RL-SCOPE 3.5); reward and labels must be "
                     "fixed, not the rows hidden:\n  " + "\n  ".join(trip_errors) + f"\n(counts: {Path(a.out).with_suffix('.ideal_trip.json')})")
        picked, r4_stats = r5_text_rows(items, pt_picked, anchor_train, n_text, rng)
    elif r4:
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
    if r5:
        bad = [f"{r['id']}: {pr}" for r in ordered + mm_dev for pr in r5_label_problems(r["label"], r.get("modality", "text"), a.instruction_term)]
        if bad:
            sys.exit(f"{len(bad)} round-5 label problems; first 20:\n  " + "\n  ".join(bad[:20]))
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
    if r5:
        stats["overlap"] = {"checked": False}          # check_overlap.py (--overlap-manifest, or run by hand with --stats) replaces this
        stats["r5"] = r5_outputs(a, bd, R, tok, kw, r5s, ordered, mm_dev, items, anchor_train, chat_valid, pairs, valid_ids, turns,
                                 dropped, row_json)
    Path(a.out).with_suffix(".stats.json").write_text(json.dumps(stats, indent=1) + "\n")
    if r5 and a.overlap_manifest:
        rc = r5_overlap_check(a, stats)
        if rc:
            return rc
    print(json.dumps(stats, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
