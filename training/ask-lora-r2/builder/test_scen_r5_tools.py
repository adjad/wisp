"""tool_choice_r5's second pass (r4's gate failures): what each group teaches and what it must never do.

Run from this directory with a pinned service checkout (about 30 s: the family is built once, every row dry-run through /agent):

    ASK_LORA_ROUND=4 ASK_LORA_R5=1 WISP_REPO=<repo>/training/ask-lora-r5/data/pin-722e08b9 \\
        ~/Desktop/MOE_Project/.venv/bin/python -m pytest --noconftest --rootdir=. -q test_scen_r5_tools.py
"""
from __future__ import annotations

import os
import random
import re

import pytest

os.environ.setdefault("ASK_LORA_ROUND", "4")
os.environ.setdefault("ASK_LORA_R5", "1")

import scen  # noqa: E402,F401  (registers the families in the right import order)
import harness as H  # noqa: E402
import leak  # noqa: E402
import scen_r5_debuglog  # noqa: E402
import scen_r5_tools as M  # noqa: E402
import writes  # noqa: E402
from harness import Final  # noqa: E402

BROAD = {"clear_reminders", "forget", "toggle_setting", "organize_files", "move_path", "email_triage", "run_shell", "run_applescript",
         "remember", "delete_path"}


@pytest.fixture(scope="module")
def rows():
    out = M.fam_tool_choice_r5(random.Random("tool_choice_r5-r3"))
    return [s for s in out if getattr(s, "r5grp", None)]


def calls(sc):
    return [c for st in sc.steps if isinstance(st, list) for c in st]


def names(sc):
    return [c.name for c in calls(sc)]


def by_cat(rows, cat):
    return [s for s in rows if s.r5cat == cat]


def test_counts_by_group(rows):
    got = {}
    for s in rows:
        wrote = any(H.is_write(c.name, c.args) for c in calls(s))
        got.setdefault(s.r5grp, [0, 0])[0 if wrote else 1] += 1
    assert got["unsafe"][0] >= 60 and got["unsafe"][1] >= 60, got
    assert got["compose"][0] + got["compose"][1] >= 25, got
    assert got["safety"][0] + got["safety"][1] >= 25, got
    assert sum(sum(v) for v in got.values()) >= 200


def test_only_allowed_write_tools(rows):
    allowed = writes.WRITE_ALLOW[M.FAMILY]
    for s in rows:
        for c in calls(s):
            if H.is_write(c.name, c.args):
                assert c.name in allowed, (s.prompt, c.name)
    assert M.FAMILY in writes.MULTITURN


def test_restraint_rows_call_nothing_broad(rows):
    for cat in ("keep or clear", "broad ask", "remember"):
        for s in by_cat(rows, cat):
            for c in calls(s):
                if c.name in BROAD:
                    # the only broad-looking calls are the named, narrow ones: one setting, one remember on a "remember" ask
                    assert c.name in ("toggle_setting", "remember"), (s.prompt, c.name)
                    if c.name == "toggle_setting":
                        assert c.args["setting"] in ("bluetooth", "airdrop", "low_power_mode")
                    else:
                        assert re.match(r"(please )?remember", s.prompt.strip(), re.I), s.prompt


def test_vague_deletes_and_mentions_act_on_nothing(rows):
    quiet = [s for s in rows if s.steps and isinstance(s.steps[0], Final)]
    assert len(quiet) >= 40
    texts = " ".join(s.steps[-1].text.lower() for s in quiet)
    for s in quiet:
        assert not calls(s)
    # a decline says what did not happen
    declines = [s for s in rows if s.r5cat == "broad ask" and not calls(s)]
    assert len(declines) >= 15
    for s in declines:
        assert re.search(r"haven't|hasn't|untouched|nothing|still where|as it was|unchanged|as they were|not ", s.steps[-1].text, re.I), s.steps[-1].text
    assert texts


def test_mail_forget_lists_then_stops(rows):
    mine = [s for s in by_cat(rows, "forget") if "recall" in names(s)]
    assert len(mine) >= 4
    for s in mine:
        assert names(s) == ["recall"]
        assert "Memory" in s.steps[-1].text and s.steps[-1].text.count("\n- ") >= 2


def test_timers_and_single_reminders_use_the_narrow_tool(rows):
    t = [s for s in rows if "manage_timers" in names(s) or "set_timer" in names(s)]
    assert len(t) >= 9
    for s in t:
        assert "clear_reminders" not in names(s) and "cancel_event" not in names(s)
    one = [s for s in rows if "clear_past_reminders" in names(s) and s.r5cat == "timer or reminder"]
    for s in one:
        assert calls(s)[0].args.get("query")        # a named stale reminder carries its word


def test_scheduled_and_immediate_sends_are_not_swapped(rows):
    for s in rows:
        if "schedule_send" in names(s):
            assert "send_message" not in names(s) and "send_email" not in names(s)
            assert re.search(r"\d|tonight|tomorrow|friday|saturday", calls(s)[0].args["when"])
        if "add_calendar_event" in names(s):
            assert "schedule_send" not in names(s)
    assert sum("schedule_send" in names(s) for s in rows) >= 5


def test_compose_reads_before_one_send(rows):
    comp = [s for s in rows if s.r5grp == "compose"]
    sends = [s for s in comp if any(n in names(s) for n in ("send_message", "send_email"))]
    assert len(sends) >= 12
    for s in sends:
        flat = names(s)
        assert flat[-1] in ("send_message", "send_email") and sum(n in ("send_message", "send_email") for n in flat) == 1
        assert flat[0] in ("get_upcoming", "daily_brief")
        assert not any(re.search(r"[*_#`]|^- ", c.args.get(k, ""), re.M) for c in calls(s) for k in ("text", "body"))
    drafts = [s for s in comp if "draft_email" in names(s) or "draft_message" in names(s)]
    assert len(drafts) >= 8
    for s in drafts:
        assert not any(n in names(s) for n in ("send_message", "send_email", "schedule_send"))
    chat = [s for s in comp if names(s) == ["summarize_emails"]]
    assert chat and all("send" not in " ".join(names(s)) for s in chat)


def test_access_denied_finals_say_so_and_invent_nothing(rows):
    acc = by_cat(rows, "access denied")
    assert len(acc) >= 12
    for s in acc:
        text = s.steps[-1].text
        assert re.search(r"couldn't check|can't|isn't connected", text), text
        assert not re.search(r"nothing (scheduled|due)|no (events|reminders|new)|empty|clear|free", text, re.I), text


def test_applescript_and_negation_never_run_a_script_or_a_send(rows):
    for s in by_cat(rows, "script send") + by_cat(rows, "negation"):
        assert not {"run_applescript", "run_shell", "schedule_send"} & set(names(s))
    for s in by_cat(rows, "negation"):
        assert not {"send_email", "send_message", "reply_to_email", "cancel_event", "toggle_setting"} & set(names(s))
    assert any("draft_email" in names(s) for s in by_cat(rows, "negation"))


def test_weather_without_a_place_asks_and_never_guesses(rows):
    w = by_cat(rows, "weather")
    ask = [s for s in w if "get_weather" not in names(s)]
    assert len(ask) >= 4
    for s in ask:
        assert s.steps[-1].text.endswith("?")
    assert any("get_weather" in names(s) for s in w)


def test_prediction_uses_predict_my_day(rows):
    p = by_cat(rows, "predict")
    assert len(p) >= 3
    for s in p:
        assert names(s) == ["predict_my_day"] and "get_my_day_plan" not in names(s)
        assert "probab" in s.steps[-1].text


def test_no_eval_or_heldout_wording_and_no_repeats(rows):
    index = leak.LeakIndex()
    held = scen_r5_debuglog.heldout_folds()
    seen = set()
    for s in rows:
        assert not index.leaks(s.prompt)[0], s.prompt
        assert leak.fold(s.prompt) not in held, s.prompt
        assert s.prompt.strip().lower() not in seen, s.prompt
        seen.add(s.prompt.strip().lower())


def test_finals_follow_the_formatting_rule(rows):
    for s in rows:
        if not isinstance(s.steps[-1], Final):
            continue
        t = s.steps[-1].text
        assert "|" not in t and not re.search(r"^#", t, re.M) and not re.search(r"^\s{2,}[-*] ", t, re.M), t
        if "**" in t:
            assert re.search(r"^- \*\*[^*]+:\*\*", t, re.M), t
        assert "let me know" not in t.lower()


def test_second_pass_leaves_the_first_pass_alone():
    os.environ["ASK_LORA_R5_FIRST_PASS_ONLY"] = "1"
    try:
        first = M.fam_tool_choice_r5(random.Random("tool_choice_r5-r3"))
    finally:
        del os.environ["ASK_LORA_R5_FIRST_PASS_ONLY"]
    assert len(first) == 184 and not any(getattr(s, "r5grp", None) for s in first)
