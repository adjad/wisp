"""Round-5 ``memory_contrast_r5``: the same topic as chat, as something this chat said, and as a real question about saved memory.

Run: ``ASK_LORA_ROUND=4 ASK_LORA_R5=1 WISP_REPO=<pinned service> pytest --noconftest --rootdir=. -q test_scen_r5_memory.py``
"""
from __future__ import annotations

import os
import random
from collections import Counter

import pytest

os.environ.setdefault("ASK_LORA_ROUND", "4")
os.environ.setdefault("ASK_LORA_R5", "1")

import scen  # noqa: E402,F401
import scen_r5_debuglog as D  # noqa: E402
import scen_r5_memory as M  # noqa: E402
import writes  # noqa: E402
from harness import Final  # noqa: E402


@pytest.fixture(scope="module")
def rows():
    return M.fam_memory_contrast_r5(random.Random("memory_contrast_r5-r3"))


def calls(sc):
    return [c for st in sc.steps if isinstance(st, list) for c in st]


def test_registered_with_no_writes():
    assert scen.FAMILIES[M.FAMILY] is M.fam_memory_contrast_r5
    assert writes.WRITE_ALLOW.get(M.FAMILY, frozenset()) == frozenset()
    assert M.FAMILY in writes.MULTITURN


def test_every_topic_is_on_each_side_of_the_line(rows):
    assert not M.DROPPED, M.DROPPED[:3]
    per = Counter(s.base_ref["topic"] for s in rows)
    assert len(per) == len(M.BUNDLES) >= 20
    for topic in per:
        modes = {s.mode for s in rows if s.base_ref["topic"] == topic}
        assert modes == set(M.MODES), topic


def test_only_a_real_memory_question_calls_recall(rows):
    for sc in rows:
        names = [c.name for c in calls(sc)]
        if sc.mode in ("saved_hit", "saved_miss"):
            assert names == ["recall"], sc.prompt
        else:
            assert names == [], sc.prompt


def test_base_writes_every_final_and_contracts_are_set(rows):
    for sc in rows:
        assert isinstance(sc.steps[-1], Final) and sc.steps[-1].text == D.BASE_WRITES
        assert sc.base_ref["kind"] == "memory_contrast" and sc.base_ref["mode"] == sc.mode
        if sc.mode in ("in_chat", "saved_hit"):
            assert sc.base_ref["facts"], sc.prompt


def test_chat_prompts_dont_ask_about_memory(rows):
    for sc in rows:
        if sc.mode in ("chat", "chat_saved", "how_to"):
            low = sc.prompt.lower()
            # A friend who remembered something is ordinary talk, and exactly the trap this family trains against.
            assert "saved" not in low and "memory" not in low and "you remember" not in low, sc.prompt


def test_in_chat_fact_is_in_the_seeded_history(rows):
    for sc in rows:
        if sc.mode == "in_chat":
            said = " ".join(t["user"] + " " + t["assistant"] for t in sc.history).lower()
            assert any(f.lower() in said for f in sc.base_ref["facts"]), sc.prompt


def test_saved_hit_finds_the_fact_and_miss_finds_nothing(rows):
    for sc in rows:
        if sc.mode == "saved_hit":
            assert sc.must_see, sc.prompt
