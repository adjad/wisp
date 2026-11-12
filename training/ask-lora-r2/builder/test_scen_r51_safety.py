"""Round-5.1 ``safety_contrast_r51``: act exactly when asked; draft, reply, ask or leave alone otherwise.

Run: ``ASK_LORA_ROUND=4 ASK_LORA_R5=1 ASK_LORA_R51=1 WISP_REPO=<pinned service> pytest --noconftest --rootdir=. -q test_scen_r51_safety.py``
"""
from __future__ import annotations

import os
import random
import re
import subprocess
import sys
from collections import Counter, defaultdict

import pytest

os.environ.setdefault("ASK_LORA_ROUND", "4")
os.environ.setdefault("ASK_LORA_R5", "1")
os.environ.setdefault("ASK_LORA_R51", "1")

import scen  # noqa: E402,F401
import scen_r51_safety as M  # noqa: E402
import scen_r5_debuglog as D  # noqa: E402
import writes  # noqa: E402
from harness import Final  # noqa: E402

READS = {"view_emails", "view_messages", "search_notes", "search_reminders", "get_upcoming", "lookup_contact", "get_weather", "find_place"}
WRITE_TOOLS = M.WRITES


@pytest.fixture(scope="module")
def rows():
    return M.fam_safety_contrast_r51(random.Random("safety_contrast_r51-r3"))


def calls(sc):
    return [c for st in sc.steps if isinstance(st, list) for c in st]


def names(sc):
    return [c.name for c in calls(sc)]


def test_registered_only_when_r51_is_set():
    assert "safety_contrast_r51" in scen.FAMILIES
    assert writes.WRITE_ALLOW[M.FAMILY] == M.WRITES
    env = dict(os.environ)
    env.pop("ASK_LORA_R51")
    here = os.path.dirname(os.path.abspath(__file__))
    out = subprocess.run([sys.executable, "-c", "import scen; print('safety_contrast_r51' in scen.FAMILIES)"], cwd=here, env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.stdout.strip().splitlines()[-1] == "False", out.stderr[-400:]


def test_size_and_every_intent_is_covered(rows):
    groups = {r.base_ref["group"] for r in rows}
    intents = Counter(r.base_ref["intent"] for r in rows)
    assert 70 <= len(groups) <= 95 and 300 <= len(rows) <= 420, (len(groups), len(rows))
    assert set(intents) == set(M.INTENTS), intents
    assert all(n >= 12 for n in intents.values()), intents


def test_groups_hold_several_sides_and_are_whole(rows):
    by = defaultdict(list)
    for r in rows:
        by[r.base_ref["group"]].append(r)
    assert all(len(v) >= 2 for v in by.values())
    assert all(len({r.base_ref["category"] for r in v}) == 1 for v in by.values())


def test_about_a_third_end_in_a_write(rows):
    # scripted writes meant to take effect (the probe now runs on a copy, so r.world.effects stays empty)
    wrote = sum(1 for r in rows if not r.no_effect and any(n in WRITE_TOOLS for n in names(r)))
    assert wrote / len(rows) >= 0.3, wrote


def test_forgetting_is_left_out(rows):
    for r in rows:
        assert "forget" not in names(r) and "remember" not in names(r), r.prompt
        assert "forget" not in r.prompt.lower(), r.prompt


def test_only_asked_for_rows_write(rows):
    for r in rows:
        w = [n for n in names(r) if n in WRITE_TOOLS]
        intent = r.base_ref["intent"]
        if intent in ("clarify", "leave_alone", "read_only", "embedded_instruction"):
            assert not w, (intent, r.prompt)
        if intent == "draft":
            assert set(w) <= {"draft_email"}, r.prompt
        if intent == "reply":
            assert w and w[-1] in ("reply_to_email", "send_message"), r.prompt
        if intent in ("direct_send", "authorized_write"):
            assert len(w) >= 1, r.prompt
        if intent == "refused":
            assert not r.world.effects, r.prompt        # the tool or the host refused: nothing changed


def test_a_reply_is_on_the_thread_not_a_new_send(rows):
    for r in rows:
        if r.base_ref["intent"] == "reply" and r.base_ref["category"] in ("email",) or "reply_to_email" in names(r):
            assert "send_email" not in names(r) and "reply_to_email" in names(r), r.prompt
            assert names(r)[0] == "view_emails", r.prompt


def test_reminder_and_event_never_swap(rows):
    for r in rows:
        if r.base_ref["category"] != "reminder_vs_event" or r.base_ref["intent"] != "authorized_write":
            continue
        p = r.prompt.lower()
        n = [x for x in names(r) if x in WRITE_TOOLS]
        if "remind" in p or "nudge" in p:
            assert n == ["add_reminder"], r.prompt
        else:
            assert n == ["add_calendar_event"], r.prompt


def test_missing_channel_looks_up_then_asks(rows):
    for r in rows:
        if r.base_ref["category"] == "missing_channel" and r.base_ref["intent"] == "clarify":
            assert names(r) == ["lookup_contact"], r.prompt
            assert r.base_ref["mode"] == "clarify"


def test_keep_never_clears(rows):
    for r in rows:
        if r.base_ref["category"] == "keep_vs_clear" and r.base_ref["intent"] == "leave_alone":
            assert names(r) == [], r.prompt
        if r.base_ref["category"] == "keep_vs_clear" and r.base_ref["intent"] == "authorized_write" and "except" in r.prompt + "keep":
            kept_titles = {c.args.get("query") for c in calls(r) if c.name == "clear_reminders"}
            left = [x.title for x in r.world.reminders]
            assert left and len(kept_titles) >= 1, r.prompt          # something is still there afterwards


def test_no_place_means_a_question_and_a_place_means_the_tool(rows):
    for r in rows:
        if r.base_ref["category"] != "missing_location":
            continue
        if r.base_ref["intent"] == "clarify":
            assert names(r) == [], r.prompt
        else:
            assert names(r) and names(r)[0] in ("get_weather", "find_place"), r.prompt


def test_refusals_state_the_tool_result_and_add_no_substitute(rows):
    for r in rows:
        if r.base_ref["intent"] == "refused":
            assert "add_calendar_event" not in names(r) and "add_reminder" not in names(r) and "send_message" not in names(r) \
                and "send_email" not in names(r), r.prompt


def test_embedded_content_never_sends_unasked(rows):
    for r in rows:
        if r.base_ref["intent"] == "embedded_instruction":
            assert not [n for n in names(r) if n in WRITE_TOOLS], r.prompt


def test_cards_match_the_sends(rows):
    for r in rows:
        carded = [n for n in names(r) if n in {"send_message", "send_email", "reply_to_email", "add_calendar_event", "update_event", "cancel_event",
                                                     "clear_reminders"}]
        if r.base_ref["intent"] == "refused" and r.base_ref["category"] in ("read_only_calendar", "not_organizer"):
            continue                                              # the host refuses some before any card
        if carded:
            assert r.expect_cards == carded and all(r.approve), r.prompt
        else:
            assert not r.expect_cards, r.prompt


def test_the_probe_leaves_the_world_unchanged(rows):
    # The family's probe replay must not apply its writes to the world build.py replays: a moved event would read back
    # at its new time, the real move would be a no-op without a card, and the meta would keep the probe's effect.
    moves = [r for r in rows if "update_event" in names(r) and not r.no_effect]
    assert moves
    for r in moves:
        c = next(c for c in calls(r) if c.name == "update_event")
        ev = [e for e in r.world.events if e.title == c.args["title"]]
        assert ev and all(e.start.strftime("%Y-%m-%dT%H:%M") != c.args["when_iso"] for e in ev), r.prompt
        assert not getattr(r.world, "effects", None), r.prompt


def test_every_scripted_send_or_draft_has_content(rows):
    for r in rows:
        for c in calls(r):
            if c.name in ("send_message", "send_email", "draft_email", "draft_message", "reply_to_email"):
                body = str(c.args.get("text") or c.args.get("body") or "")
                # a short real reply ("Sure, around six.") is fine; "here is <thing>" with nothing in it is not
                assert len(body.split()) >= 2 and not re.match(r"(?:hello,?\s*)?here (?:is|are)\b", body, re.I), (r.prompt, body)


def test_finals_are_base_writes_unless_the_host_needed_a_question(rows):
    scripted = 0
    for r in rows:
        last = r.steps[-1]
        assert isinstance(last, Final)
        if r.base_ref.get("scripted_final"):
            scripted += 1
            assert r.base_ref["intent"] in ("clarify", "leave_alone") and last.text.endswith("?"), r.prompt
        else:
            assert last.text == D.BASE_WRITES, r.prompt
    assert scripted <= 0.1 * len(rows)


def test_contract_is_set_for_the_source_checker(rows):
    for r in rows:
        b = r.base_ref
        assert b["kind"] == "write_contrast" and b["mode"] in ("write", "draft", "word", "read", "clarify", "remark"), r.prompt
        assert b["group"].startswith("r51-g") and b["intent"] in M.INTENTS
        if b["mode"] in ("write", "read") and b["intent"] not in ("refused", "embedded_instruction"):
            pass
        if b["mode"] == "draft":
            assert "draft_email" in names(r), r.prompt


def test_prompts_are_unique_within_a_group_and_across_families(rows):
    seen = defaultdict(set)
    for r in rows:
        key = (r.base_ref["group"], r.base_ref["intent"])
        assert r.prompt not in seen[key], r.prompt
        seen[key].add(r.prompt)
