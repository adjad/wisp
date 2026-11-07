"""Round-5 ``all_sources_r5``: questions that span several sources read each of them, and single-source questions stay single.

Run: ``ASK_LORA_ROUND=4 ASK_LORA_R5=1 WISP_REPO=<pinned service> pytest --noconftest --rootdir=. -q test_scen_r5_sources.py``
"""
from __future__ import annotations

import os
import random
import re
from collections import Counter

import pytest

os.environ.setdefault("ASK_LORA_ROUND", "4")
os.environ.setdefault("ASK_LORA_R5", "1")

import scen  # noqa: E402,F401  (first: registers the families in the right import order)
import scen_r5_debuglog as D  # noqa: E402
import scen_r5_sources as M  # noqa: E402
import writes  # noqa: E402
from harness import Final, is_write  # noqa: E402

EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐️‍]")
TABLE = re.compile(r"^\s*\|", re.M)
MULTI = ("topic", "person", "prep", "owe", "one_hit", "nothing", "conflict", "weather", "failed", "conflict_amount")


@pytest.fixture(scope="module")
def rows():
    return M.fam_all_sources_r5(random.Random("all_sources_r5-r3"))


def shape_of(rows, shape):
    return [s for s in rows if s.shape == shape]


def turns(sc):
    return len(sc.steps)


def test_registered_only_under_r5_with_no_writes():
    assert scen.FAMILIES[M.FAMILY] is M.fam_all_sources_r5
    assert writes.WRITE_ALLOW.get(M.FAMILY, frozenset()) == frozenset()
    assert M.WRITE_ALLOW_R5[M.FAMILY] == frozenset()


def test_every_final_is_written_by_base_and_nothing_writes(rows):
    for sc in rows:
        assert isinstance(sc.steps[-1], Final) and sc.steps[-1].text == D.BASE_WRITES, sc.prompt
        assert sum(isinstance(s, Final) for s in sc.steps) == 1, sc.prompt
        for c in M.tools_of(sc):
            assert c.name in M.TOOL_SOURCE, (sc.prompt, c.name)
            assert not is_write(c.name, c.args), (sc.prompt, c.name)
    assert len(D.unwritten([{"messages": [{"content": sc.steps[-1].text}]} for sc in rows])) == len(rows)


def test_turn_count_and_every_shape_is_present(rows):
    total = sum(turns(s) for s in rows)
    assert 280 <= total <= 400, total
    per = Counter(s.shape for s in rows)
    for shape, quota in M.QUOTA.items():
        assert per[shape] >= (quota if shape != "single" else 24), (shape, per[shape])
    for shape in MULTI:
        assert per[shape] >= 6, shape


def test_multi_source_rows_read_two_or_more_distinct_sources(rows):
    for shape in MULTI:
        for sc in shape_of(rows, shape):
            srcs = M.sources_of(sc)
            assert len(srcs) >= 2, (shape, sc.prompt, srcs)
            assert not (set(srcs) & M.COMBINED), (shape, sc.prompt)
            assert sc.base_ref["read"], sc.prompt
    # the wide shapes read four or more
    for shape in ("topic", "one_hit", "nothing", "person"):
        for sc in shape_of(rows, shape):
            assert len(M.sources_of(sc)) >= 3, (shape, sc.prompt, M.sources_of(sc))
    for sc in shape_of(rows, "topic") + shape_of(rows, "one_hit"):
        assert {"mail", "messages", "notes", "calendar"} <= set(M.sources_of(sc)), sc.prompt


def test_independent_reads_share_one_step_and_prep_is_two_dependent_steps(rows):
    for shape in MULTI:
        for sc in shape_of(rows, shape):
            steps = [s for s in sc.steps if isinstance(s, list)]
            if shape == "prep":
                assert len(sc.steps) == 3 and len(steps) == 2, sc.prompt
                assert len(steps[0]) == 1 and steps[0][0].name == "get_upcoming", sc.prompt
                assert len(steps[1]) >= 2, sc.prompt
                assert {TOOL_ for TOOL_ in M.sources_of(sc)} >= {"calendar", "mail"}, sc.prompt
            else:
                assert len(sc.steps) == 2 and len(steps[0]) >= 2, (shape, sc.prompt)


def test_prep_second_step_uses_names_from_the_event(rows):
    for sc in shape_of(rows, "prep"):
        title = sc.base_ref["hit"]["calendar"][0].lower()
        second = [c for c in sc.steps[1] if c.name in ("view_emails", "search_notes", "search_reminders")]
        assert second, sc.prompt
        for c in second:
            assert c.args["query"].lower() in title, (sc.prompt, c.args, title)


def test_single_source_contrasts_read_exactly_one_source_and_one_call(rows):
    singles = shape_of(rows, "single")
    assert 0.15 <= len(singles) / len(rows) <= 0.25, len(singles) / len(rows)
    for sc in singles:
        calls = M.tools_of(sc)
        assert len(calls) == 1 and len(M.sources_of(sc)) == 1, (sc.prompt, [c.name for c in calls])
        assert len(sc.steps) == 2, sc.prompt
    seen = Counter(M.tools_of(s)[0].name for s in singles)
    for tool in ("view_emails", "get_upcoming", "get_weather", "view_messages", "search_notes", "search_reminders",
                 "get_my_day_plan", "get_recent_activity"):
        assert seen[tool] >= 2, (tool, seen)
    # the combined tools answer their questions alone
    plan = [s for s in singles if M.tools_of(s)[0].name == "get_my_day_plan"]
    assert plan and all(M.tools_of(s)[0].args["day"] in ("today", "this_week") for s in plan)
    # a calendar question is calendar only
    for sc in singles:
        c = M.tools_of(sc)[0]
        if c.name == "get_upcoming":
            assert c.args.get("calendar_only") is True


def test_single_source_worlds_still_hold_the_other_sources(rows):
    # the contrast is real: the topic or person also sits in other sources, which the answer must leave unread
    tempting = 0
    for sc in shape_of(rows, "single"):
        w = sc.world
        if (w.emails and w.messages) and len(w.notes) + len(w.events) > 1:
            tempting += 1
    assert tempting >= len(shape_of(rows, "single")) * 0.8


def test_hits_empties_and_must_see_are_consistent(rows):
    for sc in rows:
        ref = sc.base_ref
        assert ref["kind"] == "all_sources" and ref["scripted"] is None
        assert set(ref["hit"]).isdisjoint(ref["empty"]), sc.prompt
        results = " ".join(sc.results).lower()
        for src, toks in ref["hit"].items():
            assert toks and toks[0].lower() in results, (sc.prompt, src, toks)
        assert len(sc.must_see) == len(ref["hit"]), sc.prompt
    for sc in shape_of(rows, "topic"):
        assert len(sc.base_ref["hit"]) >= 2, sc.prompt
    for sc in shape_of(rows, "one_hit"):
        assert len(sc.base_ref["hit"]) == 1 and sc.base_ref["empty"], sc.prompt
    for sc in shape_of(rows, "nothing"):
        assert not sc.base_ref["hit"] and len(sc.base_ref["empty"]) >= 4, sc.prompt
        assert not sc.must_see
    for sc in shape_of(rows, "person"):
        assert len(sc.base_ref["hit"]) >= 2, sc.prompt


def test_one_hit_rows_cover_several_different_sources(rows):
    assert len({next(iter(s.base_ref["hit"])) for s in shape_of(rows, "one_hit")}) >= 4


def test_conflict_rows_carry_two_different_times_in_two_sources(rows):
    for sc in shape_of(rows, "conflict"):
        ref = sc.base_ref
        assert ref["old"] != ref["new"], sc.prompt
        assert {"calendar", "mail"} <= set(ref["hit"]) and ref["hit"]["calendar"] == [ref["old"]] and ref["hit"]["mail"] == [ref["new"]]
        assert ref["old"].lower() in sc.results[0].lower() and ref["new"].lower() in sc.results[1].lower()
        assert not re.search(r"(?<![\d:])" + re.escape(ref["new"].lower()), sc.results[0].lower())   # the calendar still shows only the old time


def test_nothing_rows_return_only_empty_results(rows):
    for sc in shape_of(rows, "nothing"):
        for call, text in zip(M.tools_of(sc), sc.results):
            low = text.lower()
            assert re.search(r"no (?:emails|messages|note|active|matching)|found no|no matching|no events|nothing|no saved|no reminders",
                             low) or "0 " in low or low.startswith("next"), (sc.prompt, call.name, text[:120])


def test_weather_rows_read_calendar_and_weather_together(rows):
    for sc in shape_of(rows, "weather"):
        assert [c.name for c in M.tools_of(sc)] == ["get_upcoming", "get_weather"], sc.prompt


def test_prompts_are_unique_lowercase_casual_and_plain(rows):
    seen = set()
    for sc in rows:
        low = sc.prompt.strip().lower()
        assert low not in seen, sc.prompt
        seen.add(low)
        assert not EMOJI.search(sc.prompt) and not TABLE.search(sc.prompt), sc.prompt
        for text in sc.results:
            assert not EMOJI.search(text) and not TABLE.search(text), (sc.prompt, text[:80])
    assert sum(1 for sc in rows if sc.prompt == sc.prompt.lower()) >= len(rows) * 0.5


def test_no_row_asks_a_held_out_wording(rows):
    import leak
    held = D.heldout_folds()
    assert all(leak.fold(sc.prompt) not in held for sc in rows)


def test_build_is_deterministic_for_the_family_seed():
    a = [(s.prompt, [[(c.name, tuple(sorted(c.args.items()))) for c in st] if isinstance(st, list) else "F" for st in s.steps])
         for s in M.fam_all_sources_r5(random.Random("all_sources_r5-r3"))]
    b = [(s.prompt, [[(c.name, tuple(sorted(c.args.items()))) for c in st] if isinstance(st, list) else "F" for st in s.steps])
         for s in M.fam_all_sources_r5(random.Random("all_sources_r5-r3"))]
    assert a == b


def test_tool_names_are_real_tools_in_the_pinned_service():
    from service.tools.registry import REGISTRY
    names = set(REGISTRY) if not hasattr(REGISTRY, "names") else set(REGISTRY.names())
    for tool in M.TOOL_SOURCE:
        assert tool in names, tool


# ---- the gold contract (2026-11-07 audit): each row carries what its answer has to say, for the chooser and the validator
def test_every_row_has_a_contract(rows):
    for sc in rows:
        ref = sc.base_ref
        assert ref["kind"] == "all_sources" and ref["read"], sc.prompt
        for key in ("hit", "empty", "failed", "conflict", "events", "prohibit", "all_of", "must_say_absent"):
            assert key in ref, (key, sc.prompt)
        assert ref["must_say_absent"] == (sc.shape == "nothing"), sc.prompt


def test_contract_facts_are_in_the_tool_results(rows):
    for sc in rows:
        text = " ".join(sc.results).lower()
        for source, facts in sc.base_ref["hit"].items():
            assert any(f.lower() in text for f in facts), (sc.prompt, source, facts)


def test_single_rows_need_facts_and_forbid_what_nothing_read(rows):
    singles = shape_of(rows, "single")
    assert sum(bool(s.base_ref["hit"] or s.base_ref["all_of"]) for s in singles) == len(singles)
    assert sum(bool(s.base_ref["prohibit"]) for s in singles) >= 8
    for sc in singles:
        text = " ".join(sc.results).lower()
        assert not any(p.lower() in text for p in sc.base_ref["prohibit"]), sc.prompt


def test_conflict_rows_name_both_values(rows):
    for sc in shape_of(rows, "conflict") + shape_of(rows, "conflict_amount"):
        c = sc.base_ref["conflict"]
        text = " ".join(sc.results)
        assert c["old"] in text and c["new"] in text and c["old"] != c["new"], sc.prompt


def test_failed_rows_show_the_hosts_own_words(rows):
    failed = shape_of(rows, "failed")
    assert len({next(iter(s.base_ref["failed"])) for s in failed}) >= 4
    for sc in failed:
        source = next(iter(sc.base_ref["failed"]))
        assert source not in sc.base_ref["hit"] and source not in sc.base_ref["read"], sc.prompt
        assert len(sc.base_ref["hit"]) >= 1, sc.prompt
        assert any(("can't read" in r or "could not check" in r or "isn't connected" in r) for r in sc.results), sc.prompt
