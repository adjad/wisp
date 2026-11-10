"""Toolbench scoring of a turn that ends with no answer text.

A soft label (empty_response, over_call, tool_error) does not fail a case by
itself, so it must not stop the scorer from failing a blank reply. No model
and no network: the scorer is driven with hand-built trajectories.
"""
from __future__ import annotations

from datetime import date

import pytest

from scripts.toolbench.cases import Case, Step
from scripts.toolbench.scorer import SOFT, score

TODAY = date(2026, 10, 14)
NAMES = {"view_emails", "send_email", "search_notes"}
EMPTY_STEP = {"calls": [], "content": ""}


def traj(*steps, final="ok", **kw):
    return {"route": {"needs_tools": True, "tool_subset": None},
            "steps": [{"calls": [{"name": n, "args": a} for n, a in s]} for s in steps],
            "final": final, "seconds": 1.0, **kw}


def case(**kw):
    return Case(id="c", category="t", prompt="p", **kw)


def verdict(c, t):
    return score(c, t, registry_names=NAMES, today=TODAY)


def test_right_tool_then_a_blank_reply_fails():
    c = case(expect=(Step(("view_emails",)),), answer={"contains_all": ["1,284.17"]})
    t = traj([("view_emails", {"query": "Chase"})], final="")
    t["steps"].append(dict(EMPTY_STEP))

    v = verdict(c, t)

    assert v.labels == ["empty_response", "empty_answer"]
    assert not v.passed and not v.answer_ok
    assert v.selection_ok and v.args_ok and v.safe  # the tool choice itself was right
    assert "ungrounded_answer" not in v.labels  # one label for a blank reply, not two


def test_plain_chat_turn_with_a_blank_reply_fails():
    t = traj(final="")
    t["steps"] = [dict(EMPTY_STEP)]

    v = verdict(case(no_tool=True), t)

    assert v.labels == ["empty_response", "empty_answer"]
    assert not v.passed and not v.answer_ok


@pytest.mark.parametrize("final", ["", "  \n"])
def test_every_soft_label_still_lets_a_blank_reply_fail(final):
    c = case(expect=(Step(("view_emails",)),), max_calls=1)
    t = traj([("view_emails", {"q": "Chase"})], [("view_emails", {"query": "Chase"})], final=final)
    t["steps"][1]["tool_errors"] = ["(error calling view_emails({'q': 'Chase'}): unexpected argument(s) ['q'].)"]
    t["steps"].append(dict(EMPTY_STEP))

    v = verdict(c, t)

    assert set(v.labels) == SOFT | {"empty_answer"}
    assert not v.passed and not v.answer_ok


def test_a_soft_label_with_an_answer_still_passes():
    t = traj(final="fallback")
    t["steps"] = [dict(EMPTY_STEP)]
    v = verdict(case(), t)
    assert v.passed and v.answer_ok and v.labels == ["empty_response"]

    many = [[("view_emails", {})]] * 5
    v = verdict(case(expect=(Step(("view_emails",)),)), traj(*many, final="Two new emails."))
    assert v.passed and v.labels == ["over_call"]


def test_a_message_draft_card_is_still_an_answer():
    t = traj(final="", message_draft={"to": "Sam Rivera", "body": "Running late."})
    t["steps"] = [dict(EMPTY_STEP)]

    v = verdict(case(), t)

    assert v.passed and v.labels == ["empty_response"]


def test_a_blocking_label_is_not_doubled_with_empty_answer():
    assert verdict(case(), traj(final="", timeout=True)).labels == ["timeout"]

    t = traj(final="", error="Boom")
    t["steps"] = [dict(EMPTY_STEP)]
    labels = verdict(case(), t).labels
    assert "error" in labels and "empty_answer" not in labels

    t = traj(final="")
    t["steps"] = [dict(EMPTY_STEP)]
    labels = verdict(case(expect=(Step(("view_emails",)),)), t).labels
    assert "no_call" in labels and "empty_answer" not in labels

    assert verdict(case(), traj(final="")).labels == ["empty_answer"]
