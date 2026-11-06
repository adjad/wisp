"""Round-5 debug-log family: its logic, on made-up seeds. The user's real seeds never enter this public repo."""
from __future__ import annotations

import os
import random
import re

import pytest

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


def _views(lead):
    for seed in range(12):
        ctx = M.new_ctx(random.Random(seed))
        try:
            subject, _day, code = M.mail_list(ctx, "parking permit", size=1000, lead=lead)
        except ValueError:
            continue    # past what the model reads: the builder retries with another world
        yield subject, code, M.model_view(ctx, "parking permit")


def _needs_ranked_host():
    """The ranked search exists from PR #476. The repo's conftest may load an older pinned service first."""
    from service.tools import email_tools
    if not hasattr(email_tools, "_search_rows"):
        pytest.skip("this service has no ranked search; run with --noconftest and WISP_REPO at a pin with PR #476")


def test_a_subject_match_ranks_first():
    _needs_ranked_host()
    for subject, code, view in _views(False):
        assert code in view
        assert re.findall(r"^Subject: (.*)$", view, re.M)[0] == subject


def test_newsletters_with_the_topic_in_the_subject_rank_ahead_of_the_real_email():
    _needs_ranked_host()
    seen = 0
    for subject, code, view in _views(True):
        seen += 1
        assert code in view
        assert re.findall(r"^Subject: (.*)$", view, re.M)[-1] == subject
    assert seen


def test_no_match_never_claims_all_were_read_when_the_list_was_cut():
    ctx = M.new_ctx(random.Random(2))
    M.mail_list(ctx, "parking permit", has_match=False, size=2500)
    assert "cut to fit" in M.model_view(ctx, "parking permit")
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


def test_heldout_wordings_fold_and_skip_placeholders(monkeypatch):
    seeds = {"shapes": [{"id": "x", "train": [], "heldout": ["What's up?", "text {person} that"]}]}
    monkeypatch.setattr(M, "load_seeds", lambda: seeds)
    assert M.heldout_folds() == frozenset({M.leak.fold("What's up?")})


def test_new_shapes_all_have_handlers():
    needed = {"news_today", "place_news", "place_news_send", "email_overview", "reminders_overview", "calendar_range", "plan_named_day",
              "stock_quote", "attention", "know_me", "capability", "weather_week", "find_time", "add_reminder_said", "add_event_said",
              "send_summary", "send_calendar_range", "draft_reply"}
    assert needed <= set(M.HANDLERS)


def test_reminder_and_event_wording_parses():
    assert M._title("create a reminder for tomorrow from 6-7pm to go to chess club in room 206") == "Go to chess club in room 206"
    assert M._title("create a calendar event this wednesday for chess club meeting from 7-8pm") == "Chess club meeting"
    assert M.TIME.search("from 6-7pm").groups() == ("6", "7", "pm")
