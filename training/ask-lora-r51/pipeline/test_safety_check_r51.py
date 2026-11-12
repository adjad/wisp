"""Offline tests for safety_check_r51.py rules. Run: python -m pytest training/ask-lora-r51/pipeline/test_safety_check_r51.py -q"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import safety_check_r51 as S  # noqa: E402


@pytest.mark.parametrize("it,txt,rule", [
    ("leave_alone", "I've made a note that Priya will hear it from you.", "leave_alone_claims_a_note"),
    ("leave_alone", "Understood. I have made a note that the library reminder stays.", "leave_alone_claims_a_note"),
    ("leave_alone", "I understand that you'd like to push that reminder. When was it set for?", "leave_alone_takes_remark_as_task"),
    ("leave_alone", "It sounds like you want to refine the schedule. To get started, who attends?", "leave_alone_takes_remark_as_task"),
    ("leave_alone", "I hear you; I'm here whenever you want to pick that thought up again.", None),
    ("leave_alone", "Concerts can be a lot. Would you like a reminder to decide by Friday?", None),
    ("read_only", "I've cancelled the event.", "claims_action_on_no_write_turn"),
    ("draft", "I've sent it to Jordan.", "draft_claims_sent"),
    ("clarify", "Sure, I'll wait.", "clarify_asks_nothing"),
    ("direct_send", "I've sent it to Maya.", None),
])
def test_rules(it, txt, rule):
    assert S.rule_for(it, txt) == rule


def test_filter_raw_keeps_tool_calls_and_unknown_rows(tmp_path, capsys):
    raw = tmp_path / "raw.jsonl"
    rows = [{"id": "a", "k": 0, "content": "I've noted that."}, {"id": "a", "k": 1, "content": "Okay, no rush."},
            {"id": "a", "k": 2, "content": "", "tool_calls": [{"x": 1}]}, {"id": "zzz", "k": 0, "content": "I've noted that."}]
    raw.write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = tmp_path / "safe.jsonl"
    S.filter_raw({"a": "leave_alone"}, raw, out)
    kept = [json.loads(l) for l in open(out)]
    assert [(r["id"], r["k"]) for r in kept] == [("a", 1), ("a", 2), ("zzz", 0)]
