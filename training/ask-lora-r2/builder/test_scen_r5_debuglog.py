"""Round-5 debug-log family: its logic, on made-up seeds. The user's real seeds never enter this public repo."""
from __future__ import annotations

import os
import random

os.environ.setdefault("ASK_LORA_ROUND", "4")
os.environ.setdefault("ASK_LORA_R5", "1")

import scen  # noqa: E402,F401  (registers the families in the right import order)
import scen_r5_debuglog as M  # noqa: E402


def test_no_seeds_builds_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "SEEDS", str(tmp_path / "missing.yaml"))
    assert M.fam_debug_log_r5(random.Random(1)) == []


def test_every_continued_shape_has_a_handler():
    assert set(M.BEFORE) <= set(M.HANDLERS)
    assert set(M.BEFORE.values()) <= set(M.HANDLERS)


def test_chain_message_finds_its_shape_despite_case_and_punctuation():
    shapes = M.shape_of({"shapes": [{"id": "plan_week", "train": ["and for the week"]}]})
    assert shapes["And for the week?"] == "plan_week"


def test_topic_from_about_and_from_a_plain_question():
    assert M.topic_of("is there mail about the parking permit in my email") == "parking permit"
    assert M.topic_of("are there any fee updates") == "fee"


def test_a_match_stays_inside_what_the_model_can_read_and_is_never_first():
    for seed in range(12):
        ctx = M.new_ctx(random.Random(seed))
        try:
            subject, _day, code = M.mail_list(ctx, "parking permit", size=1000)
        except ValueError:
            continue    # past the cap: the builder retries with another world
        view = M.model_view(ctx, "parking permit")
        assert code in view
        assert view.index("Subject:") < view.index(subject)


def test_no_match_never_claims_all_were_read_when_the_list_was_cut():
    ctx = M.new_ctx(random.Random(2))
    M.mail_list(ctx, "parking permit", has_match=False, size=2000)
    assert "were cut" in M.model_view(ctx, "parking permit")
    assert "read all" not in M.none_about(ctx, "parking permit")


def test_plan_rows_have_no_duplicates_and_keep_the_source():
    class Plan:
        kind = "day"
        days = [("Today", [("3 PM", "Lab", "calendar"), ("3 PM", "Lab", "calendar"), ("6 PM", "Dinner", "text")])]
    assert M.plan_lines(Plan()) == [("3 PM", "Lab"), ("6 PM", "Dinner (text)")]


def test_unwritten_finds_finals_base_still_owes():
    done = {"messages": [{"role": "assistant", "content": "Sure."}]}
    owed = {"messages": [{"role": "assistant", "content": M.BASE_WRITES}]}
    assert M.unwritten([done, owed]) == [owed]
