"""Round-5 chat contrast family and the r5 changes to session_recall_r4 / session_chat_r4.

Run: ``WISP_REPO=<pinned service> pytest --noconftest --rootdir=. test_scen_r5_chat.py``
"""
from __future__ import annotations

import os
import random
import re

import pytest
import yaml

os.environ.setdefault("ASK_LORA_ROUND", "4")
os.environ.setdefault("ASK_LORA_R5", "1")

import leak  # noqa: E402
import scen  # noqa: E402,F401  (registers the families in the right import order)
import scen_r4_session_chat as SC  # noqa: E402
import scen_r4_session_recall as SR  # noqa: E402
import scen_r5_chat as M  # noqa: E402
import scen_r5_debuglog as D  # noqa: E402

EVAL_YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ask-lora-r4", "chateval", "prompts_r4.yaml")
OFFER = re.compile(r"tell me and|i'll (?:keep|remember|hold)|if you (?:share|give|tell)|for next time", re.I)


@pytest.fixture(scope="module")
def rows():
    return M.fam_chat_contrast_r5(random.Random(7))


def _eval_prompts():
    units = yaml.safe_load(open(EVAL_YAML))["units"]
    out = []
    for u in units:
        out += [u["prompt"]] if "prompt" in u else []
        out += list(u.get("prompts", []))
        out += list(u.get("turns", []))
    return out


def test_family_is_registered_only_under_r5():
    assert M.FAMILY in scen.FAMILIES
    assert scen.FAMILIES[M.FAMILY] is M.fam_chat_contrast_r5


def test_about_150_rows_all_written_by_base_with_no_tool_call(rows):
    assert 150 <= len(rows) <= 200, len(rows)
    for sc in rows:
        assert len(sc.steps) == 1 and sc.steps[0].text == D.BASE_WRITES, sc.prompt
        assert sc.family == M.FAMILY and sc.qkind == "chat_contrast"
    assert len(D.unwritten([{"messages": [{"content": sc.steps[0].text}]} for sc in rows])) == len(rows)


def test_covers_every_topic_and_has_multi_turn_histories(rows):
    cats = {c for c, _ in M.SINGLES} | {c for c, _ in M.THREADS.values()}
    assert {"vent", "hobby", "trip", "gift", "dinner", "small", "plan"} <= cats
    assert sum(1 for sc in rows if getattr(sc, "history", None)) >= 40
    assert sum(1 for sc in rows if not getattr(sc, "history", None)) >= 90


def test_no_memory_talk_in_any_prompt_or_scripted_history_turn(rows):
    for sc in rows:
        texts = [sc.prompt] + [t["user"] + " " + t["assistant"] for t in getattr(sc, "history", [])]
        for t in texts:
            assert not M.MEMORY_TALK.search(t), t
            assert not re.search(r"\b(nothing (?:is )?saved|haven't talked about|memory has nothing)\b", t, re.I), t


def test_no_table_markup_or_lists_in_scripted_history():
    for _, pairs in M.THREADS.values():
        for _, a in pairs:
            assert not re.search(r"[|*#\n]|^\s*[-\d]+[.)]\s", a or "")


def test_wording_does_not_overlap_chat_eval_toolbench_or_heldout(rows):
    idx = leak.LeakIndex()
    ev = _eval_prompts() + ["rough day", "i'm thinking about picking up a new hobby"]
    ev_tri = [(p, leak.trigrams(p)) for p in ev]
    held = D.heldout_folds()
    for sc in rows:
        for p in [sc.prompt] + [t["user"] for t in getattr(sc, "history", [])]:
            assert leak.fold(p) not in held, p
            leaked, score, q = idx.leaks(p)
            assert not leaked, (p, score, q and q["prompt"])
            t = leak.trigrams(p)
            for e, et in ev_tri:
                j = len(t & et) / max(1, len(t | et))
                assert j < 0.5, (p, e, j)


def test_family_adds_no_row_outside_its_prompts(rows):
    own = {p for _, p in M.SINGLES} | {u for _, (_, pairs) in M.THREADS.items() for u, _ in pairs}
    assert {sc.prompt for sc in rows} <= own


# ---- session_recall_r4 under r5
def test_r5_never_said_finals_match_the_recognizer_and_never_offer_to_save():
    for t in SR.NEVER_USER_FINALS_R5:
        f = t.format(x="your locker number", X="Your locker number")
        assert SR.NONE_ACK.search(f), f
        assert "this chat" in f, f
        assert not OFFER.search(f), f
    for t in SR.NEVER_ASST_FINALS_R5:
        f = t.format(x="a plumber", t="falling asleep faster")
        assert SR.NONE_ACK.search(f), f
        assert not re.search(r"saved|stored|memory|on file", f, re.I), f
        assert not OFFER.search(f), f
    for t in SR.UNSTORED_FINALS_R5:
        f = t.format(x="your parking spot", X="Your parking spot")
        assert not OFFER.search(f), f
    assert len({" ".join(t.split()[:3]) for t in SR.NEVER_ASST_FINALS_R5}) == len(SR.NEVER_ASST_FINALS_R5)


def test_r4_lists_are_untouched():
    assert len(SR.NEVER_USER_FINALS) == 3 and len(SR.NEVER_ASST_FINALS) == 3 and len(SR.UNSTORED_FINALS) == 5
    assert any("Tell me and I'll keep it" in t for t in SR.NEVER_USER_FINALS)


def test_r5_flag_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv("ASK_LORA_R5", "0")
    assert not SR._r5() and not SC._base_writes(dict(kind="emo", prompt="x"))
    monkeypatch.setenv("ASK_LORA_R5", "1")
    assert SR._r5() and SC._base_writes(dict(kind="emo", prompt="x"))


# ---- session_chat_r4 under r5
def test_chat_kinds_move_to_base_but_recall_stays_scripted(monkeypatch):
    monkeypatch.setenv("ASK_LORA_R5", "1")
    out = SC.fam_session_chat_r4(random.Random(3))
    moved = [sc for sc in out if sc.steps[0].text == D.BASE_WRITES]
    kept = [sc for sc in out if sc.steps[0].text != D.BASE_WRITES]
    assert 40 <= len(moved) < len(out)
    assert {sc.qkind for sc in moved} <= SC.BASE_KINDS
    assert not any(sc.qkind in SC.RECALL_KINDS or sc.qkind == "rec_none" for sc in moved)
    assert all(sc.qkind in {"rec_user", "rec_asst", "rec_both", "rec_none"} or SC.RECALLISH.search(sc.prompt) or sc.qkind not in SC.BASE_KINDS
               for sc in kept)
    for sc in moved:   # the scripted reply that passed every guard stays as the reference
        assert sc.base_ref["scripted"] and len(sc.base_ref["scripted"]) >= 60
    assert dict(SC.BASE_MOVED) and sum(SC.BASE_MOVED.values()) == len(moved)


def test_chat_family_is_unchanged_without_r5(monkeypatch):
    monkeypatch.setenv("ASK_LORA_R5", "0")
    out = SC.fam_session_chat_r4(random.Random(3))
    assert not any(sc.steps[0].text == D.BASE_WRITES for sc in out)
