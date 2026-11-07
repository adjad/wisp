"""Round-5 ``write_contrast_r5``: each topic with a write, and with close requests that only read, draft, ask or do nothing.

Run: ``ASK_LORA_ROUND=4 ASK_LORA_R5=1 WISP_REPO=<pinned service> pytest --noconftest --rootdir=. -q test_scen_r5_contrast.py``
"""
from __future__ import annotations

import os
import random
from collections import Counter

import pytest

os.environ.setdefault("ASK_LORA_ROUND", "4")
os.environ.setdefault("ASK_LORA_R5", "1")

import scen  # noqa: E402,F401
import scen_r5_contrast as M  # noqa: E402
import scen_r5_debuglog as D  # noqa: E402
import writes  # noqa: E402
from harness import Final  # noqa: E402


@pytest.fixture(scope="module")
def rows():
    return M.fam_write_contrast_r5(random.Random("write_contrast_r5-r3"))


def calls(sc):
    return [c for st in sc.steps if isinstance(st, list) for c in st]


def test_allowed_writes_stay_in_step():
    assert writes.WRITE_ALLOW[M.FAMILY] == M.WRITES


def test_every_mode_is_covered(rows):
    c = Counter(r.mode for r in rows)
    assert all(c[m] >= 5 for m in ("write", "draft", "word", "read", "clarify", "remark")), c
    assert 80 <= len(rows) <= 130


def test_only_a_plain_instruction_writes(rows):
    allowed = {"write": M.WRITES - {"draft_email"}, "draft": {"draft_email"}, "read": {"view_messages", "view_emails", "search_reminders", "get_upcoming"}}
    for sc in rows:
        names = [c.name for c in calls(sc)]
        if sc.mode in allowed:
            assert len(names) == 1 and names[0] in allowed[sc.mode], sc.prompt
        else:
            assert names == [], sc.prompt


def test_sends_go_through_a_card_and_drafts_dont(rows):
    for sc in rows:
        names = [c.name for c in calls(sc)]
        if {"send_message", "send_email", "add_calendar_event"} & set(names):
            assert sc.expect_cards == names and sc.approve == [True], sc.prompt
        else:
            assert not sc.expect_cards, sc.prompt


def test_base_writes_every_final_and_contracts_are_set(rows):
    for sc in rows:
        last = sc.steps[-1]
        assert isinstance(last, Final) and last.text == D.BASE_WRITES, sc.prompt
        assert sc.base_ref["kind"] == "write_contrast" and sc.base_ref["mode"] == sc.mode


def test_a_read_finds_its_fact(rows):
    for sc in rows:
        if sc.mode == "read":
            assert sc.base_ref["facts"], sc.prompt


def test_a_plain_final_with_a_list_keeps_the_note_on_its_own_line():
    import scen_r3_picture as P
    out = P.note_inj(random.Random(1), "Here it is:\n- **Size:** S", "a planted line")
    assert "\n\nOne line in" in out
    assert " S One line" not in out
    assert P.note_inj(random.Random(1), "Hana Ito's form.", None) == "Hana Ito's form."
