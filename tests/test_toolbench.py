"""Offline tests for scripts/toolbench: matchers, case loading, scoring, fakes.

No model and no network: the scorer is driven with hand-built trajectories,
and the fakes are called directly against a fresh world.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.toolbench.cases import Case, Step, load_cases, parse_case  # noqa: E402
from scripts.toolbench.matchers import match_args, match_value  # noqa: E402
from scripts.toolbench.report import compare, render, summarize  # noqa: E402
from scripts.toolbench.scorer import score  # noqa: E402
from scripts.toolbench.world import build_world, generic_fake, make_fakes  # noqa: E402

TODAY = date(2026, 10, 14)  # a Wednesday
NAMES = {"get_upcoming", "view_emails", "reply_to_email", "send_email", "draft_email",
         "lookup_contact", "add_reminder", "search_notes"}


def traj(*steps, final="ok", route=None, **kw):
    return {"route": route or {"needs_tools": True, "tool_subset": None},
            "steps": [{"calls": [{"name": n, "args": a} for n, a in s]} for s in steps],
            "final": final, "seconds": 1.0, **kw}


def case(**kw):
    return Case(id=kw.pop("id", "c"), category="t", prompt="p", **kw)


# ----------------------------------------------------------------- matchers

def test_matchers():
    assert match_value("Mom", "mom", TODAY) is None
    assert match_value({"icontains": "mom"}, "mom@example.com", TODAY) is None
    assert match_value({"contains_any": ["linda", "mom"]}, "Linda Test", TODAY) is None
    assert match_value({"regex": r"T0?9:00"}, "2026-10-21T09:00:00", TODAY) is None
    assert match_value({"in": ["a", "b"]}, "B", TODAY) is None
    assert match_value({"absent": True}, None, TODAY) is None
    assert match_value({"present": True}, "", TODAY)
    assert match_value({"date_offset": 1}, "2026-10-15T12:00:00", TODAY) is None
    assert match_value({"date_offset": 1}, "2026-10-16T12:00:00", TODAY)
    assert match_value({"weekday": "friday"}, "2026-10-16T12:30:00", TODAY) is None
    assert match_value({"weekday": "friday"}, "2026-10-23T12:30:00", TODAY)  # not the coming one
    assert match_value({"icontains": "x"}, None, TODAY) == "missing"
    assert match_args({"to": {"icontains": "mom"}}, {"to": "dad"}, TODAY)
    with pytest.raises(ValueError):
        match_value({"eq": 1, "in": [1]}, 1, TODAY)
    with pytest.raises(ValueError):
        match_value({"nope": 1}, 1, TODAY)


# -------------------------------------------------------------------- cases

def test_parse_case_validation():
    c = parse_case({"id": "x", "prompt": "p", "expect": [{"any_of": ["a", "b"], "args": {"q": "z"}}]}, "cat")
    assert c.expect == (Step(("a", "b"), {"q": "z"}),) and c.call_budget == 4
    for bad in ({"id": "x", "prompt": "p", "expect": [{"tool": "a"}], "no_tool": True},
                {"id": "x", "prompt": "p", "expect": [{"tool": "a", "any_of": ["b"]}]},
                {"id": "x", "prompt": "p", "bogus": 1},
                {"id": "x", "prompt": "p", "answer": {"contains": ["a"]}},
                {"prompt": "p"}):
        with pytest.raises(ValueError):
            parse_case(bad, "cat")


def test_shipped_cases_load_and_name_real_tools():
    cases = load_cases()
    assert len(cases) >= 50
    import os
    import tempfile
    os.environ.setdefault("WISP_HOME", tempfile.mkdtemp())
    import service.tools  # noqa: F401
    from service.tools.registry import REGISTRY
    for c in cases:
        for s in c.expect:
            unknown = set(s.tools) - set(REGISTRY)
            assert not unknown, f"{c.id} expects unknown tools {unknown}"


# ------------------------------------------------------------------- scorer

def test_pass_with_interleaved_calls_and_args():
    c = case(expect=(Step(("reply_to_email",), {"message_id": "m-1"}),), forbid=frozenset({"send_email"}))
    v = score(c, traj([("view_emails", {})], [("reply_to_email", {"message_id": "m-1"})]),
              registry_names=NAMES, today=TODAY)
    assert v.passed and v.selection_ok and v.args_ok and v.model_calls == 2


def test_bad_args_wrong_tool_no_call_out_of_order():
    c = case(expect=(Step(("reply_to_email",), {"message_id": "m-1"}),))
    v = score(c, traj([("reply_to_email", {"message_id": "m-9"})]), registry_names=NAMES, today=TODAY)
    assert not v.passed and v.labels == ["bad_args"] and v.selection_ok and not v.args_ok
    v = score(c, traj([("view_emails", {})]), registry_names=NAMES, today=TODAY)
    assert v.labels == ["wrong_tool"] and not v.selection_ok
    v = score(c, traj(), registry_names=NAMES, today=TODAY)
    assert "no_call" in v.labels
    c2 = case(expect=(Step(("lookup_contact",)), Step(("draft_email",))))
    v = score(c2, traj([("draft_email", {})], [("lookup_contact", {})]), registry_names=NAMES, today=TODAY)
    assert v.labels == ["out_of_order"]


def test_router_direct_call_satisfies_expectation():
    c = case(expect=(Step(("get_upcoming",)),))
    route = {"needs_tools": True, "tool_subset": ["get_upcoming"], "direct_calls": [["get_upcoming", {}]]}
    v = score(c, traj(route=route), registry_names=NAMES, today=TODAY)
    assert v.passed and v.router_direct and v.model_calls == 0


def test_route_fault_when_needed_tool_not_offered():
    c = case(expect=(Step(("get_upcoming",)),))
    v = score(c, traj([("search_notes", {})], route={"needs_tools": True, "tool_subset": ["search_notes"]}),
              registry_names=NAMES, today=TODAY)
    assert v.labels == ["route_fault"]
    v = score(c, traj(route={"needs_tools": False, "tool_subset": None}), registry_names=NAMES, today=TODAY)
    assert v.labels == ["route_fault"]


def test_unsafe_from_model_call_or_effect():
    c = case(forbid=frozenset({"send_email"}))
    v = score(c, traj([("send_email", {})]), registry_names=NAMES, today=TODAY)
    assert not v.passed and not v.safe and "unsafe_call" in v.labels
    v = score(c, traj(effects=[{"tool": "send_email", "args": {}}]), registry_names=NAMES, today=TODAY)
    assert "unsafe_call" in v.labels


def test_no_tool_hallucinated_unoffered_and_budget():
    v = score(case(no_tool=True), traj([("search_notes", {})]), registry_names=NAMES, today=TODAY)
    assert v.labels == ["unneeded_call"]
    v = score(case(), traj([("set_wifi", {})]), registry_names=NAMES, today=TODAY)
    assert "hallucinated_tool" in v.labels and not v.format_ok
    v = score(case(), traj([("send_email", {})], route={"needs_tools": True, "tool_subset": ["view_emails"]}),
              registry_names=NAMES, today=TODAY)
    assert "unoffered_tool" in v.labels
    many = [[("view_emails", {})]] * 5
    v = score(case(expect=(Step(("view_emails",)),)), traj(*many), registry_names=NAMES, today=TODAY)
    assert v.passed and v.labels == ["over_call"]  # soft


def test_runaway_format_answer_and_errors():
    eight = [[("view_emails", {})]] * 8
    v = score(case(), traj(*eight), registry_names=NAMES, today=TODAY)
    assert "runaway" in v.labels
    v = score(case(), traj(final="<think>hmm</think> hi"), registry_names=NAMES, today=TODAY)
    assert "think_leak" in v.labels
    t = traj()
    t["steps"] = [{"calls": [], "error": "IncompleteStreamError: Incomplete tool arguments"}]
    assert "malformed" in score(case(), t, registry_names=NAMES, today=TODAY).labels
    v = score(case(answer={"contains_all": ["1,284.17"], "not_contains": ["forwarded"]}),
              traj(final="I forwarded it; balance 1,284.17"), registry_names=NAMES, today=TODAY)
    assert v.labels == ["ungrounded_answer"] and not v.answer_ok
    assert score(case(), traj(final=""), registry_names=NAMES, today=TODAY).labels == ["empty_answer"]
    assert score(case(), traj(final="", timeout=True), registry_names=NAMES, today=TODAY).labels == ["timeout"]
    assert "error" in score(case(), traj(error="Boom"), registry_names=NAMES, today=TODAY).labels
    t = traj(final="fallback")
    t["steps"] = [{"calls": [], "content": ""}]
    v = score(case(), t, registry_names=NAMES, today=TODAY)
    assert v.passed and v.labels == ["empty_response"]  # soft on its own


# -------------------------------------------------------------------- world

def test_fakes_are_query_relevant_and_record_effects():
    w = build_world(TODAY)
    f = make_fakes(w)
    assert "Patel" in f["get_upcoming"](period="tomorrow")
    assert "Brunch with Sam" in f["get_upcoming"](period="sunday")
    assert "Patel" not in f["get_upcoming"](period="sunday")
    assert "Lakeside" in f["get_past_events"](period="last week")
    assert "1,284.17" in f["view_emails"](query="chase")
    assert "Priya" in f["view_emails"](account="work") and "Chase" not in f["view_emails"](account="work")
    assert "oat milk" in f["search_notes"](query="latte")
    assert "No notes match" in f["search_notes"](query="zebra")
    assert "0101" in f["lookup_contact"](name="Mom")
    assert f["calculate"](expression="17*23") == "391"
    assert "231.45" in f["get_stock_price"](symbols=["Apple"])
    assert "no quote" in f["get_stock_price"](symbols="ZZZZ")
    assert "error" in f["calculate"](expression="9**999999")
    assert w.effects == []
    f["send_email"](to="mom@example.com", subject="s", body="b")
    assert w.effects == [{"tool": "send_email", "args": {"to": "mom@example.com", "subject": "s", "body": "b"}}]
    g = generic_fake(w, "toggle_setting_x", "system_write")
    g(setting="wifi")
    assert w.effects[-1]["tool"] == "toggle_setting_x"
    assert "no matching" in generic_fake(w, "x", "fs_read")()


# ------------------------------------------------------------------- report

def test_report_summarize_render_compare():
    def row(model, case_id, passed, labels=()):
        return {"model": model, "case": case_id, "category": "mail", "trial": 1, "no_tool": False,
                "steps": [], "final": "", "escapes": [],
                "v": {"passed": passed, "labels": list(labels), "notes": [], "selection_ok": passed,
                      "args_ok": passed, "safe": "unsafe_call" not in labels, "format_ok": True,
                      "answer_ok": True, "seconds": 2.0, "steps": 1, "prompt_tokens_est": 100}}
    base = [row("A", "x", True), row("A", "y", True), row("B", "x", False, ["unsafe_call"])]
    cur = [row("A", "x", False, ["wrong_tool"]), row("A", "y", True), row("B", "x", True)]
    s = summarize(base)
    assert s["A"]["pass"] == 2 and s["B"]["unsafe"] == 1 and s["A"]["consistent"] == 2
    text = render(base)
    assert "SCORECARD" in text and "unsafe_call" in text
    diff = compare(base, cur)
    assert "REGRESSED: 1" in diff and "IMPROVED: 1" in diff


# ------------------------------------------------------------- phase 0 additions

def test_leak_labels():
    for final in ('Okay {"name": "get_upcoming", "arguments": {"period": "tomorrow"}}',
                  "<tool_call>\n{\"name\": \"x\"}</tool_call>"):
        v = score(case(), traj(final=final), registry_names=NAMES, today=TODAY)
        assert "text_tool_call" in v.labels and not v.format_ok and not v.passed
    for final in ('We need to check what the user asked: "do i have anything on sunday".',
                  "The user is asking about mail. Let me check.", "Let me check your calendar first."):
        assert "reasoning_leak" in score(case(), traj(final=final), registry_names=NAMES, today=TODAY).labels
    for final in ("Let me know if you want more.", "You have 3 emails. Let me know!",
                  "We had a busy week: dentist tomorrow."):
        assert score(case(), traj(final=final), registry_names=NAMES, today=TODAY).passed, final


def test_reasoning_chars_summed():
    t = traj(final="ok")
    t["steps"] = [{"calls": [], "content": "a", "reasoning_chars": 120},
                  {"calls": [], "content": "b", "reasoning_chars": 30}]
    assert score(case(), t, registry_names=NAMES, today=TODAY).reasoning_chars == 150


def test_bind_fills_known_placeholders_only():
    c = parse_case({"id": "x", "prompt": "What's my {bank} balance? {unknown}",
                    "history": [{"user": "hi {friend_first}", "assistant": "ok", "tools": "t"}],
                    "expect": [{"tool": "view_emails", "args": {"q": {"icontains": "{bank}"},
                                                                 "r": {"regex": "T0?9:00"}}}],
                    "answer": {"contains_any": ["{balance}", "x"]}}, "mail")
    b = c.bind({"bank": "Citi", "balance": "642.90", "friend_first": "Maya"})
    assert b.prompt == "What's my Citi balance? {unknown}"
    assert b.history[0]["user"] == "hi Maya"
    assert b.expect[0].args == {"q": {"icontains": "Citi"}, "r": {"regex": "T0?9:00"}}
    assert b.answer == {"contains_any": ["642.90", "x"]}
    assert c.prompt.startswith("What's my {bank}")  # original untouched


def test_seeded_worlds_vary_and_canonical_is_stable():
    from scripts.toolbench.world import CANONICAL, make_facts
    assert make_facts(None) == make_facts(None)
    assert all(make_facts(None)[k] == v for k, v in CANONICAL.items() if k not in ("friend_email",))
    assert make_facts(5) == make_facts(5)
    seen = {make_facts(i)["dentist"] for i in range(30)}
    assert len(seen) >= 4
    w = build_world(TODAY, seed=5)
    f = w.facts
    fakes = make_fakes(w)
    assert f["dentist"] in fakes["get_upcoming"](period="tomorrow")
    assert f["balance"] in fakes["view_emails"](query=f["bank"])
    assert f["mom_phone_tail"] in fakes["lookup_contact"](name="Mom")
    assert f["friend_email"] in fakes["lookup_contact"](name=f["friend_first"])


def test_every_case_binds_in_every_world_and_splits_are_valid():
    from scripts.toolbench.world import make_facts
    import re
    cases = load_cases()
    assert {c.split for c in cases} == {"gate", "dev"}
    assert sum(c.split == "gate" for c in cases) >= 90
    for facts in (make_facts(None), make_facts(1), make_facts(2)):
        for c in cases:
            b = c.bind(facts)
            blob = repr((b.prompt, b.history, [s.args for s in b.expect], b.answer))
            left = set(re.findall(r"\{([a-z_0-9]+)\}", blob))
            assert not left, f"{c.id}: unfilled {left}"


def test_heldout_shingles_match_v31_format():
    from scripts.toolbench.cases import heldout_shingles
    from scripts.toolbench.world import make_facts
    gate = [parse_case({"id": "g", "prompt": "What's my {bank} balance?"}, "mail"),
            parse_case({"id": "d", "prompt": "dev only prompt", "split": "dev"}, "mail")]
    data = heldout_shingles(gate, [make_facts(None), make_facts(1)])
    assert data["n_prompts"] == len(data["items"]) >= 1
    item = data["items"][0]
    assert set(item) == {"ntok", "full", "sh"} and len(item["full"]) == 12
    assert set(item["sh"]) <= {"1", "2", "3", "4", "5", "6"}
    import hashlib
    want = hashlib.sha1("what's my chase balance".encode()).hexdigest()[:12]
    assert want in {i["full"] for i in data["items"]}
    dev = hashlib.sha1("dev only prompt".encode()).hexdigest()[:12]
    assert dev not in {i["full"] for i in data["items"]}
