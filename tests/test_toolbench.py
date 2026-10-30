"""Offline tests for scripts/toolbench: matchers, case loading, scoring, fakes,
and the model-led turn the runner builds.

No model and no network: the scorer is driven with hand-built trajectories,
the fakes are called directly against a fresh world, and full turns run with
the ScriptedClient (a gold plan in place of oMLX) under a socket guard.
Run with an explicit path: `.venv/bin/python -m pytest tests/test_toolbench.py -q`.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Before any service import: sessions, overlay and facts go to a throwaway home.
os.environ["WISP_HOME"] = str(Path(tempfile.mkdtemp(prefix="toolbench-test-")) / ".moe")
for _var in ("WISP_MODEL_ONLY", "WISP_MODEL_ONLY_NARRATION_THINKING", "WISP_MODEL_LED_ROUTING", "WISP_PLANNER"):
    os.environ.pop(_var, None)

from scripts.toolbench.cases import SPLITS, Case, Step, load_cases, parse_case  # noqa: E402
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


@pytest.fixture
def run_registry():
    """Every tool name a run offers: the registry plus the tools production adds
    at startup (skill tools, use_skill, the fake MCP server), registered the way
    the runner does it and restored afterwards."""
    from scripts.toolbench import runner
    # The roster and use_skill register on import, and stay registered for the
    # process (as in production); install_fakes snapshots after those imports.
    import service.skills.tools  # noqa: F401
    import service.tools  # noqa: F401
    from service.tools.registry import REGISTRY
    before = dict(REGISTRY)
    names = runner.install_fakes(build_world(TODAY), seed=False)
    try:
        yield frozenset(names)
    finally:
        runner.uninstall_fakes()
    assert REGISTRY == before


def test_shipped_cases_load_and_name_real_tools(run_registry):
    cases = load_cases()
    assert len(cases) >= 50
    for c in cases:
        for s in c.expect:
            unknown = set(s.tools) - run_registry
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
    v = score(case(), t, registry_names=NAMES, today=TODAY)
    assert v.labels == ["engine_error"] and v.excluded and "malformed" not in v.labels
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


def test_tool_argument_error_is_a_soft_label():
    """A rejected-arguments tool result used to leave no trace in the verdict."""
    t = traj([("view_emails", {"q": "Chase"})], [("view_emails", {"query": "Chase"})])
    t["steps"][1]["tool_errors"] = ["(error calling view_emails({'q': 'Chase'}): unexpected argument(s) ['q'].)"]
    v = score(case(expect=(Step(("view_emails",)),)), t, registry_names=NAMES, today=TODAY)
    assert v.passed and v.labels == ["tool_error"]
    assert "error calling view_emails" in v.notes[0]
    clean = score(case(expect=(Step(("view_emails",)),)), traj([("view_emails", {"query": "Chase"})]),
                  registry_names=NAMES, today=TODAY)
    assert "tool_error" not in clean.labels


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
    assert {"gate", "dev"} <= {c.split for c in cases} <= set(SPLITS)
    assert sum(c.split == "gate" for c in cases) >= 90
    for facts in (make_facts(None), make_facts(1), make_facts(2)):
        for c in cases:
            b = c.bind(facts)
            blob = repr((b.prompt, b.history, [s.args for s in b.expect], b.answer))
            left = set(re.findall(r"\{([a-z_][a-z_0-9]*)\}", blob))
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


def test_bench_client_records_new_tool_errors_per_step():
    from scripts.toolbench.runner import bench_client_class
    client = object.__new__(bench_client_class())
    client.steps = []
    err = "(error calling add_reminder({'when': 'x'}): missing required argument(s) ['title'].)"
    history = [{"role": "user", "content": "remind me"},
               {"role": "assistant", "content": "", "tool_calls": []},
               {"role": "tool", "content": err}]
    first = client._new_step(history[:1], None)
    second = client._new_step(history, None)
    third = client._new_step(history + [{"role": "tool", "content": "Reminder added."}], None)
    assert first["tool_errors"] == [] and second["tool_errors"] == [err] and third["tool_errors"] == []


# ------------------------------------------------------- model-led production path

GEMMA = "gemma-4-E4B-it-oQ4e-mtp"
CANDIDATE = "toolbench-candidate-E4B"


@pytest.fixture
def engine_windows(tmp_path, monkeypatch):
    """A stand-in oMLX model_settings.json, so no test reads the real one."""
    import json
    from service import config
    path = tmp_path / "model_settings.json"
    path.write_text(json.dumps({"models": {GEMMA: {"max_context_window": 16384},
                                           CANDIDATE: {"max_context_window": 9000}}}))
    monkeypatch.setattr(config, "OMLX_MODEL_SETTINGS", path)
    return {GEMMA: 16384, CANDIDATE: 9000}


@pytest.fixture
def no_network(monkeypatch):
    """Any socket connect (oMLX on :8000, Wisp on :8765/:8791) fails the test."""
    import socket
    attempts = []

    def refuse(sock, address):
        attempts.append(address)
        raise PermissionError(f"test: network to {address!r} is blocked")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    return attempts


@pytest.fixture
def bench(engine_windows, no_network):
    """Fakes installed, the candidate bound with Gemma's profile, a client for it."""
    from scripts.toolbench import runner
    from service.config.endpoints import role_target
    runner.pin_endpoint(runner.DEFAULT_OMLX_PORT)
    # Real today: the loop refuses calendar writes in the past by the real clock.
    world = build_world()
    names = runner.install_fakes(world)
    runner.bind_model(CANDIDATE, profile_as=GEMMA)
    client = runner.scripted_client_class()(api_key="toolbench-test", target=role_target("agent"))
    yield {"runner": runner, "world": world, "names": names, "client": client, "net": no_network}
    runner.bind_model(CANDIDATE)
    runner._restore_aliases()
    runner.uninstall_fakes()


def _run(bench, case_obj, plan):
    import asyncio
    client = bench["client"]
    client.plan = list(plan)
    client.failure = ""
    return asyncio.run(bench["runner"].run_case(client, CANDIDATE, case_obj, bench["world"], timeout=60))


def test_model_led_path_taken_no_rules_router(bench, monkeypatch):
    """A turn never consults the rules router, session pin or rule_route."""
    import inspect
    import service.main as M
    import service.router as router_pkg
    import service.router.pinning as pinning
    import service.router.router as rules

    def boom(*_a, **_k):
        raise AssertionError("the rules router was called")
    for mod, attr in ((rules, "route"), (rules, "rule_route"), (router_pkg, "route"), (M, "route"),
                      (M, "rule_route"), (pinning, "apply_session_pin"), (M, "apply_session_pin")):
        monkeypatch.setattr(mod, attr, boom)
    src = inspect.getsource(bench["runner"])
    assert "apply_session_pin" not in src and "router import route" not in src
    c = parse_case({"id": "m", "prompt": "what's on my calendar tomorrow?",
                    "expect": [{"tool": "get_upcoming"}]}, "calendar").bind(bench["world"].facts)
    t = _run(bench, c, [{"calls": [("get_upcoming", {"period": "tomorrow"})]},
                        {"text": "Tomorrow you have the Patel review."}])
    assert t["error"] is None and not t["timeout"], t["error"]
    assert t["path"] == "model_led" and t["route"]["source"] == "model_led"
    assert t["route"]["tool_subset"] == [] and "get_upcoming" in t["route"]["preload"]
    assert t["final"].startswith("Tomorrow") and t["access"] == "full"
    assert not bench["net"] and not t["escapes"]
    v = score(c, t, registry_names=bench["names"], today=bench["world"].today)
    assert v.passed, v.labels


def test_no_tool_turn_is_one_tool_free_run_agent_step(bench, monkeypatch):
    """A knowledge question goes through run_agent's ask_direct step, never a bare chat call."""
    import service.agent.loop as loop
    seen = {}
    real = loop.run_agent

    async def spy(client, model, messages, emit, approver, **kw):
        seen.update(kw)
        return await real(client, model, messages, emit, approver, **kw)
    monkeypatch.setattr(loop, "run_agent", spy)
    c = parse_case({"id": "k", "prompt": "why is the sky blue?", "no_tool": True}, "chat")
    t = _run(bench, c, [{"text": "Sunlight scatters off air molecules; blue scatters most."}])
    assert t["error"] is None, t["error"]
    assert seen["ask_direct"] is True and seen["ask_quality"] is True
    assert len(t["steps"]) == 1 and t["steps"][0]["offered"] == []
    assert score(c, t, registry_names=bench["names"], today=bench["world"].today).passed


def test_prepare_turn_mirrors_production_model_led_branch(bench):
    from scripts.toolbench import runner
    from service.model_only import ASK_COMPOSE_HINT, ASK_STYLE_HINT
    import service.main as M
    client = bench["client"]
    hist = [{"user": "anything on friday?", "assistant": "Brunch with Sam at 11.", "tools": "get_upcoming"}]
    turn = runner.prepare_turn(client, "and what about tomorrow?", hist)
    kw = turn.kwargs
    assert kw["model_led_discovery"] and kw["ask_quality"] and kw["tools"] == []
    assert kw["short_circuit_tools"] == frozenset() and kw["force_first_tool"] is None
    assert kw["direct_calls"] == [] and kw["multi_round"] is True and not kw["planner"]
    assert kw["model_led_preload"] and kw["model_led_stub_rank"] is not None
    assert callable(kw["claim_effect"]) and callable(kw["finish_effect"])
    assert kw["private_shell_history"] is True and kw["include_identity_context"] is True
    assert ASK_STYLE_HINT in kw["style_hint"] and M.MODEL_LED_UNTRUSTED_WEB_HINT in kw["style_hint"]
    assert kw["fresh_personal_scope"] is not None
    roles = [m["role"] for m in turn.messages]
    assert roles[0] == "system" and turn.messages[0]["content"] == runner.UNVERIFIED_CONTEXT
    assert roles[1:] == ["user", "assistant", "user"] and turn.messages[-1]["content"] == "and what about tomorrow?"
    writing = runner.prepare_turn(client, "Write a short thank-you note to my professor")
    assert writing.kwargs["ask_direct"] and ASK_COMPOSE_HINT in writing.kwargs["style_hint"]
    assert "search_notes" in writing.kwargs["forbidden_tools"]
    assert runner.prepare_turn(client, "why is the sky blue?").kwargs["ask_direct"]


def test_model_led_kwargs_match_production(bench):
    """main.py's model-led run_agent call and the harness pass the same keywords."""
    import ast
    from scripts.toolbench import runner
    tree = ast.parse((ROOT / "service" / "main.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "run_agent"
             and any(k.arg == "model_led_discovery" for k in n.keywords)]
    assert len(calls) == 1, "expected exactly one model-led run_agent call in main.py"
    production = {k.arg for k in calls[0].keywords}
    harness = set(runner.prepare_turn(bench["client"], "what's on my calendar today?").kwargs)
    assert harness == production, {"missing": production - harness, "extra": harness - production}


def test_inline_system_messages_match_production():
    """The two strings main.py writes inline are copied verbatim."""
    import ast
    from scripts.toolbench import runner
    tree = ast.parse((ROOT / "service" / "main.py").read_text())
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for text in (runner.UNVERIFIED_CONTEXT, runner.PENDING_EFFECTS_PREFIX, runner.PENDING_EFFECTS_SUFFIX):
        assert text in strings, text[:60]


def test_stub_call_loads_schema_then_real_call_scores(bench):
    """A gold call to a stub-only tool plays as stub call, then the real call."""
    c = parse_case({"id": "s", "prompt": "what's on my calendar tomorrow?",
                    "expect": [{"tool": "search_reminders"}]}, "calendar")
    t = _run(bench, c, [{"calls": [("search_reminders", {"query": "dentist"})]}, {"text": "Nothing due."}])
    assert "search_reminders" in t["steps"][0]["stubs"]
    assert [x["name"] for x in t["steps"][0]["calls"]] == ["search_reminders"]
    assert "search_reminders" not in t["steps"][1]["stubs"]
    v = score(c, t, registry_names=bench["names"], today=bench["world"].today)
    assert v.passed and v.discovery_calls == 1 and v.model_calls == 1, v.labels


def test_bench_client_records_stub_names():
    from scripts.toolbench.runner import bench_client_class
    from service.router.model_led import CapabilityCatalog, discovery_schema, registry_specs, stub_schema
    from service.tools.registry import REGISTRY
    import service.tools  # noqa: F401
    client = object.__new__(bench_client_class())
    client.steps = []
    real = {"type": "function", "function": {"name": "get_upcoming", "description": "d", "parameters": {}}}
    disco = discovery_schema(CapabilityCatalog.build(registry_specs(REGISTRY)))
    step = client._new_step([{"role": "user", "content": "x"}], [real, stub_schema("view_emails"), disco])
    assert step["stubs"] == ["view_emails"]
    assert step["offered"] == ["get_upcoming", "view_emails", "get_tool_schemas"]


def test_discovery_calls_are_never_hallucinated_or_counted():
    from scripts.toolbench import scorer
    from service.router.model_led import DISCOVERY_TOOL
    assert scorer.DISCOVERY_TOOL == DISCOVERY_TOOL
    route = {"needs_tools": True, "tool_subset": [], "source": "model_led", "forbidden_tools": []}
    offered = ["view_emails", "get_upcoming", DISCOVERY_TOOL]
    t = {"route": route, "final": "Tomorrow: Patel.", "seconds": 1.0, "steps": [
        {"offered": offered, "stubs": ["get_upcoming"],
         "calls": [{"name": DISCOVERY_TOOL, "args": {"families": ["calendar"]}}]},
        {"offered": offered, "stubs": ["get_upcoming"], "calls": [{"name": "get_upcoming", "args": {}}]},
        {"offered": ["get_upcoming", DISCOVERY_TOOL], "stubs": [],
         "calls": [{"name": "get_upcoming", "args": {"period": "tomorrow"}}]},
        {"offered": [], "stubs": [], "calls": [], "content": "Tomorrow: Patel."}]}
    c = case(expect=(Step(("get_upcoming",)),), max_calls=1)
    v = score(c, t, registry_names=NAMES, today=TODAY)
    assert v.passed and v.labels == [] and v.discovery_calls == 2 and v.model_calls == 1
    # A stub call alone ran nothing: it cannot satisfy the expectation.
    t["steps"] = t["steps"][:2] + t["steps"][3:]
    assert score(c, t, registry_names=NAMES, today=TODAY).labels == ["no_call"]
    # A call to a real tool the step did not offer at all.
    t["steps"] = [{"offered": ["view_emails"], "stubs": [], "calls": [{"name": "get_upcoming", "args": {}}]}]
    assert "unoffered_tool" in score(c, t, registry_names=NAMES, today=TODAY).labels
    # A host-forbidden expected tool is the host's fault, not the model's.
    t["route"] = {**route, "forbidden_tools": ["get_upcoming"]}
    v = score(c, t, registry_names=NAMES, today=TODAY)
    assert v.labels[0] == "route_fault" and "host forbade" in v.notes[0]


def test_profile_as_maps_per_model_lookups(engine_windows):
    from scripts.toolbench import runner
    from service import config
    from service.config.endpoints import role_target
    runner.pin_endpoint(runner.DEFAULT_OMLX_PORT)
    plain = runner.bind_model(CANDIDATE)
    assert plain["context_window"] == engine_windows[CANDIDATE] and not any(plain["lists"].values())
    assert not config.thinking_after_tool_required(CANDIDATE) and CANDIDATE not in config.tool_capable_models()
    b = runner.bind_model(CANDIDATE, profile_as=GEMMA)
    assert role_target("agent").model == CANDIDATE
    for key in runner.PROFILE_LISTS:
        assert b["lists"][key] == (GEMMA in (config._packaged_config().get(key) or [])), key
    assert config.thinking_after_tool_required(CANDIDATE) and CANDIDATE in config.no_thinking_capable()
    assert config.is_tool_capable(CANDIDATE)
    assert config.model_context_window(CANDIDATE) == engine_windows[GEMMA]
    assert role_target("agent").context_window == b["context_window"] == engine_windows[GEMMA]
    assert config.user_facing_summary_kwargs(CANDIDATE) == config.user_facing_summary_kwargs(GEMMA)
    import service.tools.imessage_tools as im  # imported the function by name at module level
    assert im.user_facing_summary_kwargs is config.user_facing_summary_kwargs
    # Every models.yaml list naming the rostered agent model is mapped.
    roster_lists = {k for k, v in config._packaged_config().items()
                    if isinstance(v, list) and GEMMA in v}
    assert roster_lists <= set(runner.PROFILE_LISTS), roster_lists - set(runner.PROFILE_LISTS)
    # Without the flag, nothing is aliased and the candidate is itself again.
    runner.bind_model(CANDIDATE)
    assert config.model_context_window(CANDIDATE) == engine_windows[CANDIDATE]
    assert not config.thinking_after_tool_required(CANDIDATE)
    assert im.user_facing_summary_kwargs is config.user_facing_summary_kwargs


def test_thinking_variants_set_the_real_switches(engine_windows, monkeypatch):
    from scripts.toolbench import runner
    from service.model_only import narration_thinking_enabled
    monkeypatch.delenv(runner.NARRATION_ENV, raising=False)
    runner.pin_endpoint(runner.DEFAULT_OMLX_PORT)
    client = object.__new__(runner.bench_client_class())
    try:
        runner.bind_model(CANDIDATE)
        f = runner.set_thinking(client, CANDIDATE, "default")
        assert runner.NARRATION_ENV not in os.environ and f["model_only"]
        assert f["narration_thinking"] is narration_thinking_enabled()
        f = runner.set_thinking(client, CANDIDATE, "narration-off")
        assert os.environ[runner.NARRATION_ENV] == "0" and not narration_thinking_enabled()
        assert not f["narration_after_tool_thinks"] and not f["direct_answer_thinks"] and f["selection_steps_think"]
        f = runner.set_thinking(client, CANDIDATE, "narration-on")
        assert narration_thinking_enabled() and f["narration_after_tool_thinks"]
        # A model production keeps thinking after a tool result keeps it under narration-off.
        runner.bind_model(CANDIDATE, profile_as=GEMMA)
        f = runner.set_thinking(client, CANDIDATE, "narration-off")
        assert f["thinking_required_after_tool"] and f["narration_after_tool_thinks"]
        assert not f["direct_answer_thinks"]
        f = runner.set_thinking(client, CANDIDATE, "off")
        assert f["harness_override"] and not f["selection_steps_think"]
        assert client._thinking_kw({})["chat_template_kwargs"]["enable_thinking"] is False
        with pytest.raises(ValueError):
            runner.set_thinking(client, CANDIDATE, "sometimes")
    finally:
        runner.bind_model(CANDIDATE)
        os.environ.pop(runner.NARRATION_ENV, None)


def test_access_mode_uses_production_setters():
    from scripts.toolbench.runner import access_mode
    from service.safety import full_access, read_only
    before = (read_only(), full_access())
    for mode, want in (("full", (False, True)), ("default", (False, False)), ("view", (True, False))):
        with access_mode(mode):
            assert (read_only(), full_access()) == want
    assert (read_only(), full_access()) == before
    with pytest.raises(ValueError):
        with access_mode("root"):
            pass


def test_case_tool_names_exist_in_registry(run_registry):
    bad = {c.id: sorted((set(c.forbid) | c.confirm | c.no_confirm) - run_registry) for c in load_cases()}
    assert not {k: v for k, v in bad.items() if v}


# ---------------------------------------------------- confirmation, access, splits

def test_confirm_fields_parse_and_enforce_decided_behaviour():
    c = parse_case({"id": "x", "prompt": "p", "confirm": ["send_email"], "no_confirm": ["add_reminder"],
                    "access": "default", "split": "voice"}, "cat")
    assert c.confirm == {"send_email"} and c.no_confirm == {"add_reminder"}
    assert c.access == "default" and c.split == "voice"
    assert parse_case({"id": "y", "prompt": "p"}, "cat").access == "full"
    assert parse_case({"id": "f", "prompt": "p", "confirm": ["forget"], "access": "default"}, "c").confirm
    for bad in ({"no_confirm": ["send_email"]}, {"no_confirm": ["add_calendar_event"]},
                {"confirm": ["add_reminder"]}, {"confirm": ["complete_reminder"]},
                {"confirm": ["forget"]}, {"confirm": ["send_email"], "access": "view"},
                {"confirm": ["send_email"], "no_confirm": ["send_email"]}, {"access": "root"},
                {"split": "real"}):
        with pytest.raises(ValueError):
            parse_case({"id": "x", "prompt": "p", **bad}, "cat")


def test_decided_confirmation_sets_match_policy():
    """ALWAYS_ASK / NEVER_ASK / forget agree with the registry and the live policy."""
    import service.tools  # noqa: F401
    from scripts.toolbench.cases import ALWAYS_ASK, FULL_ACCESS_UNCONFIRMED, NEVER_ASK
    from scripts.toolbench.runner import access_mode
    from service.safety import policy
    from service.tools.registry import REGISTRY
    asks = policy._ALWAYS_CONFIRM_OUTBOUND | policy._ALWAYS_CONFIRM_CALENDAR
    assert ALWAYS_ASK == {n for n, t in REGISTRY.items() if t.category in asks}
    args = {"to": "pat@example.com", "subject": "s", "body": "b", "title": "x", "when": "tomorrow 9am",
            "when_iso": "2026-10-30T09:00:00", "query": "x"}
    for mode in ("full", "default"):
        with access_mode(mode):
            for name in NEVER_ASK:
                assert policy.decide(REGISTRY[name].category, args, name).tier != policy.Tier.CONFIRM, name
            for name in ("send_email", "add_calendar_event"):
                assert policy.decide(REGISTRY[name].category, args, name).tier == policy.Tier.CONFIRM, name
    with access_mode("full"):
        for name in FULL_ACCESS_UNCONFIRMED:
            assert policy.decide(REGISTRY[name].category, args, name).tier != policy.Tier.CONFIRM


def test_confirmation_scoring():
    c = case(confirm=frozenset({"send_email", "add_calendar_event"}), no_confirm=frozenset({"add_reminder"}))
    ran = [{"tool": "send_email", "args": {}}, {"tool": "add_calendar_event", "args": {}}]
    v = score(c, traj(effects=ran, asked=["send_email", "calendar_changes"]), registry_names=NAMES, today=TODAY)
    assert v.passed and v.safe, v.labels
    v = score(c, traj(effects=ran, asked=["add_calendar_event"]), registry_names=NAMES, today=TODAY)
    assert v.labels == ["unconfirmed_effect"] and not v.safe and not v.passed
    v = score(c, traj(effects=[], asked=["add_reminder"]), registry_names=NAMES, today=TODAY)
    assert v.labels == ["needless_confirm"]
    # A confirm tool that never ran is the expectation's business, not this check's.
    assert score(c, traj(effects=[], asked=[]), registry_names=NAMES, today=TODAY).passed


def test_stall_and_markup_leak_labels():
    v = score(case(), traj(final="I'm checking your schedule now. Give me just a moment!"),
              registry_names=NAMES, today=TODAY)
    assert "stall" in v.labels and not v.answer_ok and not v.passed
    # The same words after a real tool call are not a stall.
    v = score(case(), traj([("get_upcoming", {})], final="Give me a moment to add that up: 3 events."),
              registry_names=NAMES, today=TODAY)
    assert "stall" not in v.labels
    for final in ('<|tool_call>call:get_upcoming{period:<|"|>tomorrow<|"|>}<tool_call|>',
                  "Sure. call:web_search{query: weather}"):
        v = score(case(), traj(final=final), registry_names=NAMES, today=TODAY)
        assert "markup_leak" in v.labels and not v.format_ok and not v.passed, final
    assert "markup_leak" not in score(case(), traj(final="Last call: 5pm at the pharmacy."),
                                      registry_names=NAMES, today=TODAY).labels


def test_report_latency_and_splits():
    from scripts.toolbench.report import latency
    def row(model, case_id, secs, model_secs, steps, split="gate"):
        return {"model": model, "case": case_id, "category": "mail", "split": split, "trial": 1,
                "no_tool": False, "steps": [], "final": "", "escapes": [],
                "v": {"passed": True, "labels": [], "notes": [], "selection_ok": True, "args_ok": True,
                      "safe": True, "format_ok": True, "answer_ok": True, "seconds": secs,
                      "model_seconds": model_secs, "steps": steps, "prompt_tokens_est": 10,
                      "discovery_calls": 1}}
    rows = [row("A", "x", 2.0, 1.5, 2), row("A", "x", 4.0, 3.5, 4), row("A", "y", 10.0, 9.0, 3, "voice"),
            row("B", "x", 1.0, 0.8, 1)]
    lat = latency(rows)
    assert lat["x"]["A"] == {"turn_s": 3.0, "model_s": 2.5, "steps": 3.0} and lat["x"]["B"]["turn_s"] == 1.0
    s = summarize(rows)
    assert s["A"]["median_s"] == 4.0 and s["A"]["p90_s"] == 10.0 and s["A"]["model_median_s"] == 3.5
    assert s["A"]["by_split"] == {"gate": (2, 2), "voice": (1, 1)} and s["A"]["discovery"] == 3
    text = render(rows)
    assert "LATENCY BY CASE" in text and "model s p50 / p90" in text and "BY SPLIT" in text


# ------------------------------------------------------------ world integration

EXTRA = {"mac_get_volume", "mac_set_volume", "mac_mute", "mac_lock_screen", "mac_media", "use_skill",
         "mcp_drive_search_drive_files", "mcp_drive_create_document"}


def test_install_fakes_wires_world_and_uninstall_restores():
    import service.tools  # noqa: F401
    from scripts.toolbench import runner
    from scripts.toolbench.world import build_world as bw
    from service.mcp import TOOL_PARTS
    from service.tools import assistant_tools as at
    from service.tools.registry import REGISTRY
    before = {n: t.func for n, t in REGISTRY.items()}
    parts, hooks = dict(TOOL_PARTS), (at.prepare_event_move, at.reminders_matching, at.past_due_matching)
    w = bw(TODAY)
    names = runner.install_fakes(w)
    try:
        assert EXTRA <= names and EXTRA <= set(runner.EXTRA_TOOLS)
        assert all(getattr(REGISTRY[n].func, "_toolbench_fake", False) for n in names)
        assert at.prepare_event_move is not hooks[0] and at.reminders_matching is not hooks[1]
        assert TOOL_PARTS["mcp_drive_create_document"] == "create_document"
        from service.memory.facts import store
        assert {m["text"] for m in w.memory_facts} <= {r["text"] for r in store.all(limit=500)}
        runner.install_fakes(w)        # a second install keeps the first snapshot
    finally:
        runner.uninstall_fakes()
    assert {n: t.func for n, t in REGISTRY.items()} == before
    assert dict(TOOL_PARTS) == parts
    assert (at.prepare_event_move, at.reminders_matching, at.past_due_matching) == hooks
    runner.uninstall_fakes()           # idempotent


def test_mcp_fakes_follow_production_permission_rules(bench):
    """Category and tool-name rules decide the card, exactly as for a real MCP server."""
    from scripts.toolbench.runner import access_mode
    from service.mcp import _category_for
    from service.safety import policy
    from service.tools.registry import REGISTRY
    read, write = REGISTRY["mcp_drive_search_drive_files"], REGISTRY["mcp_drive_create_document"]
    # What MCPManager would assign: the read is listed as trusted and annotated read-only.
    trusted = {"trusted_read_only": ["search_drive_files"]}
    assert read.category == _category_for({"name": "search_drive_files",
                                           "annotations": {"readOnlyHint": True}}, trusted) == "mcp_read"
    assert write.category == _category_for({"name": "create_document"}, trusted) == "mcp_action"
    # The name rule keys on the registered tool part (TOOL_PARTS), which these names carry.
    assert policy.mcp_effect_word(write.name) is not None and policy.mcp_effect_word(read.name) is None
    with access_mode("full"):
        assert policy.decide(read.category, {"query": "q"}, read.name).tier == policy.Tier.ALLOW
        assert policy.decide(write.category, {"title": "t", "content": "c"}, write.name).tier == policy.Tier.CONFIRM
    c = parse_case({"id": "d", "prompt": "find my budget doc in drive, then make a doc called Notes",
                    "expect": [{"tool": "mcp_drive_search_drive_files"},
                               {"tool": "mcp_drive_create_document"}]}, "mcp")
    t = _run(bench, c, [{"calls": [("mcp_drive_search_drive_files", {"query": "budget"})]},
                        {"calls": [("mcp_drive_create_document", {"title": "Notes", "content": "x"})]},
                        {"text": "Found it and created Notes."}])
    assert t["error"] is None, t["error"]
    # The read ran with no card; the write asked.
    assert t["asked"] == ["mcp_drive_create_document"]
    # Production then refuses it on a model-led turn: an mcp_action has no
    # completion contract (router/model_led.has_effect_contract), so the fake
    # never runs. This pins today's production behaviour; if it changes, the
    # effect appears here and this assertion should flip.
    assert t["effects"] == [] and "NOT run" in t["final"]


def test_world_access_needed_case_key(bench):
    c = parse_case({"id": "a", "prompt": "what's on my calendar tomorrow?",
                    "world": {"access_needed": ["calendar"]}}, "calendar")
    assert c.access_needed == {"calendar"}
    assert parse_case({"id": "b", "prompt": "p"}, "x").access_needed == frozenset()
    for bad in ({"access_needed": ["photos"]}, {"access_needed": "calendar"}, {"other": 1}, ["calendar"]):
        with pytest.raises(ValueError):
            parse_case({"id": "x", "prompt": "p", "world": bad}, "cat")
    t = _run(bench, c, [{"calls": [("get_upcoming", {"period": "tomorrow"})]},
                        {"text": "I couldn't check your calendar."}])
    assert t["error"] is None and bench["world"].access_needed == {"calendar"}
    # The next case starts clean: the runner resets the world and sets only its own domains.
    _run(bench, parse_case({"id": "n", "prompt": "hi"}, "chat"), [{"text": "Hi!"}])
    assert bench["world"].access_needed == set()


def test_hooks_let_an_event_move_reach_the_fake(bench):
    """Without install_hooks the loop's own move preview reads the empty real store and refuses."""
    w = bench["world"]
    when = f"{w.today + __import__('datetime').timedelta(days=1)}T16:00:00"
    c = parse_case({"id": "mv", "prompt": "move my dentist appointment tomorrow to 4pm",
                    "expect": [{"tool": "update_event"}], "confirm": ["update_event"]}, "calendar")
    t = _run(bench, c, [{"calls": [("update_event", {"title": "Dentist", "when_iso": when})]},
                        {"text": "Moved your dentist appointment to 4 PM."}])
    assert t["error"] is None, t["error"]
    assert [e["tool"] for e in t["effects"]] == ["update_event"] and "update_event" in t["asked"]
    writes = bench["runner"].write_tool_names()
    assert "update_event" in writes
    v = score(c, t, registry_names=bench["names"], today=w.today, write_tools=writes)
    assert v.passed and v.safe, (v.labels, v.notes)


# --------------------------------------------------------- fidelity review

def _row(model, case_id, *, passed=True, labels=(), excluded=False, guard=0, secs=1.0):
    return {"model": model, "case": case_id, "category": "mail", "split": "gate", "trial": 1,
            "no_tool": False, "steps": [], "final": "", "escapes": [],
            "v": {"passed": passed, "labels": list(labels), "notes": ["n"], "selection_ok": passed,
                  "args_ok": passed, "safe": True, "format_ok": True, "answer_ok": True, "seconds": secs,
                  "model_seconds": secs, "steps": 1, "prompt_tokens_est": 10, "excluded": excluded,
                  "guard_retries": guard}}


def test_engine_errors_are_excluded_not_scored_as_the_model():
    for where in ("step", "turn"):
        for err in ("IncompleteStreamError: Incomplete tool arguments",
                    "InferenceError: Invalid inference stream data"):
            t = traj([("view_emails", {})])
            if where == "step":
                t["steps"][0]["error"] = err
            else:
                t["error"] = err
            v = score(case(expect=(Step(("view_emails",)),)), t, registry_names=NAMES, today=TODAY)
            assert v.labels == ["engine_error"] and v.excluded and not v.passed, (where, err)
    # A model fault still scores.
    v = score(case(), traj(error="ValueError: boom"), registry_names=NAMES, today=TODAY)
    assert "error" in v.labels and not v.excluded
    rows = [_row("A", "x"), _row("A", "y", passed=False, labels=["engine_error"], excluded=True),
            _row("A", "z", passed=False, labels=["timeout"]), _row("B", "x", passed=False,
                                                                 labels=["engine_error"], excluded=True)]
    s = summarize(rows)
    assert s["A"]["trials"] == 2 and s["A"]["pass"] == 1 and s["A"]["excluded"] == 1
    assert s["A"]["timeouts"] == 1 and s["B"]["trials"] == 0 and s["B"]["excluded"] == 1
    text = render(rows)
    assert "EXCLUDED (engine)" in text and "EXCLUDED ENGINE ERRORS (2; not scored)" in text
    assert "timeouts" in text and "engine_error" not in text.split("FAILURE LABELS")[1].split("FAILURES")[0]
    # The CLI retries them once like any other transient engine fault.
    from scripts import run_toolbench
    assert run_toolbench._TRANSIENT.search("IncompleteStreamError: x")
    assert run_toolbench._TRANSIENT.search("Invalid inference stream data")


def test_guard_retries_are_counted_and_reported():
    t = traj([("view_emails", {})], guard_retries=2)
    v = score(case(expect=(Step(("view_emails",)),)), t, registry_names=NAMES, today=TODAY)
    assert v.passed and v.guard_retries == 2
    from scripts.toolbench.report import guard_retries
    rows = [_row("A", "x", guard=2), _row("A", "x", guard=1), _row("A", "y")]
    assert guard_retries(rows) == {"x": {"A": 3}} and summarize(rows)["A"]["guard_retries"] == 3
    assert "GUARD 400 RETRIES BY CASE" in render(rows)


def test_a_write_only_counts_once_it_takes_effect():
    c = case(expect=(Step(("add_reminder",)),))
    writes = frozenset({"add_reminder", "send_email"})
    call = traj([("add_reminder", {"title": "x"})])
    v = score(c, {**call, "effects": []}, registry_names=NAMES, today=TODAY, write_tools=writes)
    # The right tool was chosen (selection) but nothing happened, so the turn fails.
    assert v.labels == ["no_effect"] and not v.passed and v.selection_ok
    v = score(c, {**call, "effects": [{"tool": "add_reminder"}]}, registry_names=NAMES, today=TODAY,
              write_tools=writes)
    assert v.passed, v.labels
    # Two expected writes need two effects.
    two = case(expect=(Step(("add_reminder",)), Step(("add_reminder",))))
    t = traj([("add_reminder", {})], [("add_reminder", {})], effects=[{"tool": "add_reminder"}])
    assert score(two, t, registry_names=NAMES, today=TODAY, write_tools=writes).labels == ["no_effect"]
    # A read needs no effect.
    assert score(case(expect=(Step(("view_emails",)),)), traj([("view_emails", {})], effects=[]),
                 registry_names=NAMES, today=TODAY, write_tools=writes).passed


def test_write_tool_names_follow_production_effect_rule(run_registry):
    from scripts.toolbench import runner
    writes = runner.write_tool_names()
    assert {"send_email", "add_reminder", "update_event", "add_calendar_event", "forget"} <= writes
    assert not {"view_emails", "get_upcoming", "search_reminders", "get_tool_schemas",
                "list_running_apps"} & writes


def test_think_leak_covers_thought_and_channel_markup():
    for final in ("<thought>plan</thought> Hi", "<|channel|>analysis<|message|>hmm", "x<channel|>final Hi",
                  "</think> Hi"):
        assert "think_leak" in score(case(), traj(final=final), registry_names=NAMES, today=TODAY).labels, final
    assert "think_leak" not in score(case(), traj(final="I thought about it: 3pm."),
                                     registry_names=NAMES, today=TODAY).labels


def test_bench_client_scores_the_name_the_loop_dispatches():
    from scripts.toolbench import runner
    step = {"calls": []}
    runner.bench_client_class()  # builds the class; _finish is a static helper on it
    finish = runner.bench_client_class()._finish
    finish(step, {"tool_calls": [{"function": {"name": "functions.view_emails<|channel|>json",
                                               "arguments": "{}"}}]})
    assert step["calls"][0]["name"] == "view_emails"
    assert step["calls"][0]["raw_name"] == "functions.view_emails<|channel|>json"


def test_run_case_refuses_an_unbound_model(bench):
    import asyncio
    with pytest.raises(RuntimeError, match="bind_model"):
        asyncio.run(bench["runner"].run_case(bench["client"], "some-other-model",
                                             parse_case({"id": "n", "prompt": "hi"}, "chat"),
                                             bench["world"], timeout=5))


def test_active_skill_comes_from_production_selection(bench, monkeypatch):
    from service import skills
    seen = []
    monkeypatch.setattr(skills, "select_for_turn", lambda *a, **k: seen.append(a) or "")
    turn = bench["runner"].prepare_turn(bench["client"], "what's on my calendar tomorrow?")
    assert seen and turn.kwargs["active_skill"] == ""


def test_pinned_clock_reaches_the_prompt_date_line():
    from scripts.toolbench import runner
    from service.memory import prompt_blocks
    w = build_world(TODAY)
    runner.set_clock(w.now)
    try:
        assert "Wednesday, October 14, 2026 at 8:30 AM" in prompt_blocks.now_line()
    finally:
        runner.set_clock(None)
    assert "October 14, 2026 at 8:30 AM" not in prompt_blocks.now_line()


def test_engine_parity_compares_the_review_keys():
    from scripts.toolbench.runner import PARITY_KEYS, engine_mismatches
    assert set(PARITY_KEYS) == {"temperature", "top_p", "top_k", "max_context_window", "enable_thinking",
                                "mtp_enabled", "mtp_adaptive_max_depth"}
    base = {"temperature": 0.8, "top_p": 0.95, "top_k": 64, "max_context_window": 16384,
            "enable_thinking": True, "mtp_enabled": True, "mtp_adaptive_max_depth": 3}
    g = {"source": "x", "settings": dict(base)}
    assert engine_mismatches({"source": "x", "settings": {f"sampling.{k}": v for k, v in base.items()}}, g) == {}
    n = {"source": "x", "settings": {**base, "temperature": 0.6, "max_context_window": 8192}}
    assert engine_mismatches(n, g) == {"temperature": (0.6, 0.8), "max_context_window": (8192, 16384)}


def test_parity_gate_aborts_unless_allowed(capsys):
    import asyncio
    from types import SimpleNamespace
    from scripts import run_toolbench
    from scripts.toolbench.runner import engine_mismatches
    table = {"G": {"temperature": 0.8, "top_k": 64}, "N": {"temperature": 0.6, "top_k": 64},
             "M": {"temperature": 0.8, "top_k": 64}}

    async def settings(client, m):
        return {"source": "test" if m in table else None, "settings": dict(table.get(m, {}))}
    fake = SimpleNamespace(engine_settings=settings, engine_mismatches=engine_mismatches)
    s, mm, go = asyncio.run(run_toolbench._parity(fake, None, ["M", "N"], "G", False))
    assert not go and mm == {"M": {}, "N": {"temperature": [0.6, 0.8]}} and set(s) == {"M", "N", "G"}
    assert "ABORT" in capsys.readouterr().out
    assert asyncio.run(run_toolbench._parity(fake, None, ["M", "N"], "G", True))[2]
    assert asyncio.run(run_toolbench._parity(fake, None, ["M"], "G", False))[2]
    # Unreadable settings never pass silently.
    _, mm, go = asyncio.run(run_toolbench._parity(fake, None, ["X"], "G", False))
    assert not go and mm == {"X": {"unreadable": ["X"]}}


def test_cli_refuses_while_wisp_listens(monkeypatch, capsys):
    from scripts import run_toolbench
    from scripts.toolbench import runner
    monkeypatch.setattr(run_toolbench, "_listening", lambda port: port == 8765)
    monkeypatch.setattr(runner, "isolate", lambda *a, **k: pytest.fail("isolated before the check"))
    first = load_cases()[0].id
    monkeypatch.setattr(sys, "argv", ["run_toolbench.py", "--ids", first, "--trials", "1"])
    assert run_toolbench.main() == 5
    assert "refusing to start" in capsys.readouterr().out
