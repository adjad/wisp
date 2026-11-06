"""Round-5 picture family: structure of the rows, on made-up data (no model, no images)."""
from __future__ import annotations

import os
import random
import re

os.environ.setdefault("ASK_LORA_ROUND", "4")
os.environ.setdefault("ASK_LORA_R5", "1")

import scen  # noqa: E402,F401
import writes  # noqa: E402
import scen_r5_picture as M  # noqa: E402
from harness import Final  # noqa: E402

_ROWS = M.fam_picture_r5(random.Random(5))


def _final(s):
    return next(st.text for st in s.steps if isinstance(st, Final)) if any(isinstance(st, Final) for st in s.steps) else ""


def test_counts_and_registration():
    assert len(_ROWS) == 40
    assert M.FAMILY in scen.FAMILIES and M.FAMILY in writes.PICTURE_TURN
    assert writes.WRITE_ALLOW[M.FAMILY] == {"add_calendar_event", "add_reminder", "create_note"}


def test_every_prompt_takes_the_agent_path():
    import service.vision as v
    assert all(v.wants_action(s.prompt) for s in _ROWS)


def test_documents_list_every_fact_with_no_tables_or_headings():
    docs = [s for s in _ROWS if s.notes.startswith("document")]
    assert len(docs) == 20
    for s in docs:
        f = _final(s)
        assert "|" not in f and not re.search(r"^#", f, re.M) and not re.search(r"^\s+- ", f, re.M)
        assert len(re.findall(r"^- ", f, re.M)) >= 5
        assert "let me know" not in f.lower()
        reading = s.world.picture["reading"]
        for tok in re.findall(r"\d[\d:.,%]*", f):
            assert tok.rstrip(".,:") in reading or "unreadable" in s.notes, (tok, s.notes)


def test_proposals_write_and_contrasts_do_not():
    kinds = {"add_calendar_event": 0, "add_reminder": 0, "create_note": 0}
    for s in _ROWS:
        if s.notes.startswith("document") or s.notes.startswith("nothing"):
            assert not s.expect_cards
        else:
            assert len(s.expect_cards) == 1
            kinds[s.expect_cards[0]] += 1
    assert all(kinds.values()), kinds
