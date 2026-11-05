"""Offline tests for the PRIVATE real-use eval loader (scripts/toolbench/private.py) and its self-check.

Everything here runs on a SYNTHETIC fixture written to tmp_path: invented names, invented sources, no personal
data. The real private directory (~/.moe/evals/private-real-use) is never read by a test; PRIVATE_EVAL_DIR is
always set explicitly or removed.

Run with an explicit path: `.venv/bin/python -m pytest tests/test_toolbench_private.py -q`.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["WISP_HOME"] = str(Path(tempfile.mkdtemp(prefix="toolbench-test-")) / ".moe")
for _var in ("WISP_MODEL_ONLY", "WISP_MODEL_ONLY_NARRATION_THINKING", "WISP_MODEL_LED_ROUTING", "WISP_PLANNER"):
    os.environ.pop(_var, None)

from scripts.toolbench import private  # noqa: E402
from scripts.toolbench.cases import HELD_OUT_SPLITS, SEALED_SPLITS, SPLITS, heldout_shingles, load_cases, select  # noqa: E402
from scripts.toolbench.world import make_facts, make_fakes  # noqa: E402

# ------------------------------------------------------------------ the synthetic fixture
WORLD = {
    "today": "2026-11-02", "now": "09:00",
    "summary": "One class today.",
    "events": [
        {"title": "Pottery class", "day": "2026-11-02", "time": "6:00 PM", "location": "Studio"},
        {"title": "Dentist cleaning", "day": "2026-11-03", "time": "2:00 PM"},
        {"title": "Farmers market", "day": "2026-11-07", "all_day": True},
    ],
    "reminders": [{"title": "Return the library book", "due": "2026-10-30", "time": "9:00 AM", "created": "2026-10-25"}],
    "emails": [
        {"id": "s-1", "from": "Club Treasurer <treasurer@club.example.com>", "subject": "Budget form due",
         "day": "2026-11-02", "time": "8:00 AM", "body": "Please submit the club budget form by Friday 5:00 PM.",
         "cat": "attention"},
    ],
    "messages": [
        {"from": "Robin", "sender": "Robin", "text": "Are you bringing the cake on Saturday?", "day": "2026-11-02", "time": "8:30 AM"},
        {"from": "Robin", "sender": "Me", "text": "Yes, lemon.", "day": "2026-11-02", "time": "8:31 AM"},
    ],
    "contacts": [{"name": "Robin", "full": "Robin Example", "phone": "+1 555-555-0166"}],
    "brief": [
        {"title": "Submit the club budget form", "source": "mail", "who": "Club Treasurer", "due": "2026-11-06",
         "clock": "17:00"},
        {"title": "Bring the lemon cake to the Harper picnic", "source": "messages", "who": "Robin",
         "due": "2026-11-07", "clock": None},
    ],
    "waiting": [{"who": "Robin", "source": "messages", "summary": "Asked whether you are bringing the cake"}],
}
DAY = ["get_my_day_plan", "get_upcoming", "daily_brief"]
HIST = [{"user": "whats on today", "assistant": "Pottery class at 6 PM.", "tools": "get_my_day_plan"}]
CASES = [
    {"id": "synth_plan_saturday", "prompt": "what is my plan for saturday", "history": HIST,
     "expect": [{"any_of": DAY}], "answer": {"contains_all": ["lemon cake"]}, "trap": "synthetic"},
    {"id": "synth_and_friday", "prompt": "and friday?", "history": HIST,
     "expect": [{"any_of": DAY}], "answer": {"contains_all": ["budget form"], "regex_all": ["5(?::00)? ?pm"]},
     "trap": "synthetic"},
    {"id": "synth_draft_robin", "prompt": "draft a reply to robin", "history": HIST,
     "expect": [{"any_of": ["view_messages", "summarize_messages"],
                 "args_any": [{"conversation": {"icontains": "robin"}}, {"query": {"icontains": "robin"}}]},
                {"tool": "draft_message", "args": {"to": {"icontains": "robin"}, "text": {"regex": "cake|saturday|lemon"}}}],
     "forbid": ["send_message"], "trap": "synthetic"},
]
PLANS = [
    {"id": "synth_plan_saturday",
     "ideal": [{"calls": [["get_my_day_plan", {"day": "saturday"}]]}, {"text": "Saturday: bring the lemon cake to the Harper picnic."}],
     "fail": [{"name": "calendar only", "labels_any": ["ungrounded_answer"],
               "plan": [{"calls": [["get_upcoming", {"period": "saturday", "calendar_only": True}]]},
                        {"text": "Saturday you have the farmers market."}]}]},
    {"id": "synth_and_friday",
     "ideal": [{"calls": [["get_my_day_plan", {"day": "friday"}]]}, {"text": "Friday the club budget form is due at 5 PM."}],
     "fail": [{"name": "no call", "labels_any": ["no_call"],
               "plan": [{"text": "Friday the club budget form is due at 5 PM."}]}]},
    {"id": "synth_draft_robin",
     "ideal": [{"calls": [["view_messages", {"conversation": "Robin"}]]},
               {"calls": [["draft_message", {"to": "Robin", "text": "Yes, the lemon cake is coming on Saturday."}]]},
               {"text": ""}],
     "fail": [{"name": "draft without a read",
               "plan": [{"calls": [["draft_message", {"to": "Robin", "text": "Hi Robin"}]]}, {"text": ""}]}]},
]


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    d = tmp_path / "private-eval"
    d.mkdir()
    (d / "world.json").write_text(json.dumps(WORLD))
    (d / "cases.yaml").write_text(yaml.safe_dump(CASES, sort_keys=False))
    (d / "ideal.yaml").write_text(yaml.safe_dump(PLANS, sort_keys=False))
    monkeypatch.setenv(private.ENV_VAR, str(d))
    return d


# ------------------------------------------------------------------ fail closed
def test_private_dir_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv(private.ENV_VAR, raising=False)
    with pytest.raises(private.PrivateEvalError, match="not set"):
        private.private_dir()
    monkeypatch.setenv(private.ENV_VAR, "   ")
    with pytest.raises(private.PrivateEvalError, match="not set"):
        private.private_dir()
    monkeypatch.setenv(private.ENV_VAR, str(tmp_path / "missing"))
    with pytest.raises(private.PrivateEvalError, match="not a directory"):
        private.private_dir()
    monkeypatch.setenv(private.ENV_VAR, str(tmp_path))          # a real directory: accepted
    assert private.private_dir() == tmp_path.resolve()


def test_private_dir_refuses_a_git_checkout(tmp_path, monkeypatch):
    repo_inside = ROOT / "tests"
    monkeypatch.setenv(private.ENV_VAR, str(repo_inside))
    with pytest.raises(private.PrivateEvalError, match="git checkout"):
        private.private_dir()
    other = tmp_path / "some-clone"
    (other / ".git").mkdir(parents=True)
    (other / "evals").mkdir()
    monkeypatch.setenv(private.ENV_VAR, str(other / "evals"))
    with pytest.raises(private.PrivateEvalError, match="git checkout"):
        private.private_dir()
    with pytest.raises(private.PrivateEvalError, match="git checkout"):
        private.require_outside_checkout(ROOT / "scripts" / "out", "--out")
    assert private.require_outside_checkout(tmp_path / "runs", "--out") == (tmp_path / "runs").resolve()


def test_missing_files_are_errors_not_fallbacks(tmp_path):
    for loader in (private.load_private_cases, private.load_world_spec, private.load_plans):
        with pytest.raises(private.PrivateEvalError, match="missing"):
            loader(tmp_path)
    (tmp_path / "world.json").write_text(json.dumps({"events": []}))
    with pytest.raises(private.PrivateEvalError, match="today"):
        private.load_world_spec(tmp_path)
    (tmp_path / "world.json").write_text(json.dumps({"today": "2026-11-02", "bogus": 1}))
    with pytest.raises(private.PrivateEvalError, match="unknown keys"):
        private.load_world_spec(tmp_path)


# ------------------------------------------------------------------ the private split is sealed
def test_cases_load_as_the_private_split_and_nowhere_else(pdir):
    cases = private.load_private_cases(pdir)
    assert [c.id for c in cases] == [c["id"] for c in CASES]
    assert {c.split for c in cases} == {"private"} and {c.category for c in cases} == {"private"}
    assert "private" in SPLITS and "private" in SEALED_SPLITS and "private" not in HELD_OUT_SPLITS
    shipped = load_cases()
    assert not {c.id for c in cases} & {c.id for c in shipped}
    assert all(c.split != "private" for c in shipped)           # the shipped loader never reads private data
    # An unfiltered selection never includes them, even from a mixed list; only the explicit split does.
    mixed = shipped + cases
    assert not [c for c in select(mixed) if c.split == "private"]
    assert not [c for c in select(mixed, only="private")]
    assert {c.id for c in select(mixed, split="private")} == {c.id for c in cases}


def test_a_private_case_cannot_name_another_split_or_a_shipped_id(pdir):
    raw = [{**CASES[0], "split": "gate"}]
    (pdir / "cases.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(private.PrivateEvalError, match="split"):
        private.load_private_cases(pdir)
    shipped_id = load_cases()[0].id
    (pdir / "cases.yaml").write_text(yaml.safe_dump([{**CASES[0], "id": shipped_id}]))
    with pytest.raises(private.PrivateEvalError, match="duplicate or shipped"):
        private.load_private_cases(pdir)
    (pdir / "cases.yaml").write_text(yaml.safe_dump([CASES[0], CASES[0]]))
    with pytest.raises(private.PrivateEvalError, match="duplicate or shipped"):
        private.load_private_cases(pdir)


def test_private_prompts_never_reach_the_shingle_export(pdir):
    cases = private.load_private_cases(pdir)
    facts = [make_facts(None)]
    base = heldout_shingles(load_cases(), facts)
    mixed = heldout_shingles(load_cases() + cases, facts)
    assert mixed == base                                         # not one hash differs
    blob = json.dumps(mixed)
    assert "private" not in blob


def test_run_toolbench_private_flag_is_fail_closed(pdir, monkeypatch, capsys):
    import scripts.run_toolbench as rt
    monkeypatch.setattr(sys, "argv", ["run_toolbench.py", "--private", "--list"])
    assert rt.main() == 0
    out = capsys.readouterr().out
    assert "synth_plan_saturday" in out and "3 cases" in out and "private" in out
    monkeypatch.delenv(private.ENV_VAR)
    assert rt.main() == 2
    assert "refusing to run the private eval" in capsys.readouterr().out
    monkeypatch.setenv(private.ENV_VAR, str(pdir))
    for extra in (["--export-shingles", str(pdir / "x.json")], ["--world-seed", "3"], ["--split", "gate"]):
        monkeypatch.setattr(sys, "argv", ["run_toolbench.py", "--private", "--list", *extra])
        with pytest.raises(SystemExit):
            rt.main()
    assert not (pdir / "x.json").exists()
    monkeypatch.setattr(sys, "argv", ["run_toolbench.py", "--split", "private", "--list"])
    with pytest.raises(SystemExit):
        rt.main()                                                # the split alone never reads private data
    # A bare listing and a gate listing never show a private case.
    monkeypatch.setattr(sys, "argv", ["run_toolbench.py", "--list"])
    assert rt.main() == 0
    assert "synth_" not in capsys.readouterr().out


# ------------------------------------------------------------------ the world
def test_world_replaces_the_canonical_sources_with_the_spec(pdir):
    w = private.private_world(pdir)
    assert w.today == date(2026, 11, 2) and w.now.hour == 9
    assert [e["title"] for e in w.events] == ["Pottery class", "Dentist cleaning", "Farmers market"]
    assert [m["id"] for m in w.emails] == ["s-1"] and len(w.messages) == 2
    assert w.messages[0]["day_offset"] == 0 and w.emails[0]["day"] == w.today
    assert {c["name"] for c in w.contacts} == {"Robin"}
    assert [b["source"] for b in w.brief] == ["mail", "messages"] and w.brief[0]["due_label"] == "Fri 5:00 PM"
    assert w.cards == [] and w.notes == [] and w.memory_facts == [] and w.timers == []
    fk = make_fakes(w)
    sat = fk["get_my_day_plan"](day="saturday")
    assert "Bring the lemon cake to the Harper picnic" in sat and "Farmers market" in sat
    # Nothing of the canonical synthetic world leaks into a private read.
    canonical = {"block party", "Diwali", "silk scarf", "Priya", "Sam Rivera", "Patel", "Q3 budget"}
    for read in (sat, fk["get_my_day_plan"](), fk["get_my_day_plan"](day="this_week"),
                 fk["get_upcoming"](period="this week"), fk["view_emails"](), fk["view_messages"](),
                 fk["search_reminders"]()):
        assert not [t for t in canonical if t in read], read[:200]
    assert "Return the library book" in fk["search_reminders"]()
    assert "5:00 PM" in fk["get_upcoming"](period="friday") or "5 pm" in fk["get_my_day_plan"](day="friday")


@pytest.mark.parametrize("patch,match", [
    ({"emails": [{"id": "x", "from": "a", "subject": "s", "day": "2026-11-02", "time": "8:00 AM", "body": "b", "cat": "nope"}]}, "cat"),
    ({"brief": [{"title": "t", "source": "carrier-pigeon", "due": "2026-11-06"}]}, "source"),
    ({"events": [{"title": "t", "day": "tomorrow"}]}, "ISO date"),
    ({"brief": [{"title": "t", "source": "mail", "due": "2026-11-06", "clock": "noonish"}]}, "clock"),
    ({"waiting": [{"who": "x"}]}, "missing"),
])
def test_world_spec_is_validated(patch, match):
    with pytest.raises(private.PrivateEvalError, match=match):
        private.build_private_world({**WORLD, **patch})


# ------------------------------------------------------------------ the offline self-check
@pytest.fixture
def selfcheck_env(tmp_path, monkeypatch):
    import socket
    from scripts.toolbench import runner
    from scripts.toolbench.private_selfcheck import CANDIDATE, GEMMA
    from service import config
    path = tmp_path / "model_settings.json"
    path.write_text(json.dumps({"models": {GEMMA: {"max_context_window": 16384},
                                           CANDIDATE: {"max_context_window": 9000}}}))
    monkeypatch.setattr(config, "OMLX_MODEL_SETTINGS", path)
    attempts = []

    def refuse(sock, address):
        attempts.append(address)
        raise PermissionError(f"test: network to {address!r} is blocked")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    runner.pin_endpoint(runner.DEFAULT_OMLX_PORT)
    yield attempts
    runner._restore_aliases()


def test_selfcheck_every_case_passes_its_ideal_and_fails_its_recorded_failure(pdir, selfcheck_env):
    from scripts.toolbench.private_selfcheck import render, run_selfcheck
    results = run_selfcheck(pdir)
    assert [r["id"] for r in results] == [c["id"] for c in CASES]
    assert all(r["ok"] and r["ideal"]["passed"] and not r["host_blocked"] for r in results), render(results)
    assert sum(len(r["fails"]) for r in results) == 3 and all(f["ok"] and not f["passed"] for r in results for f in r["fails"])
    assert not selfcheck_env                                      # no socket was opened
    text = render(results)
    assert "3/3 cases behave" in text and "lemon" not in text     # ids and labels only, never the data


def test_selfcheck_catches_a_case_the_failure_would_pass(pdir, selfcheck_env):
    from scripts.toolbench.private_selfcheck import run_selfcheck
    plans = [dict(p) for p in PLANS]
    # A "failure" that is in fact a good answer: the check must flag the case (the case is too lax).
    plans[0] = {**plans[0], "fail": [{"name": "actually fine", "plan": PLANS[0]["ideal"]}]}
    (pdir / "ideal.yaml").write_text(yaml.safe_dump(plans))
    results = run_selfcheck(pdir, only={"synth_plan_saturday"})
    assert not results[0]["ok"] and results[0]["fails"][0]["passed"]
    # A case whose ideal is wrong is flagged too.
    plans[1] = {**plans[1], "ideal": [{"text": "Friday the budget form is due."}]}
    (pdir / "ideal.yaml").write_text(yaml.safe_dump(plans))
    assert not run_selfcheck(pdir, only={"synth_and_friday"})[0]["ok"]
    # Plans and cases must match one to one.
    (pdir / "ideal.yaml").write_text(yaml.safe_dump(PLANS[:2]))
    with pytest.raises(private.PrivateEvalError, match="without a scripted ideal"):
        run_selfcheck(pdir)


def test_selfcheck_reports_a_host_blocked_case_apart_and_the_pending_fix_unblocks_it(pdir, selfcheck_env):
    """'draft a message responding to <name>' is pure writing for the host today, so view_messages is withheld
    and no model can pass it (tests/test_toolbench.py xfails the shipped twins). The self-check labels that
    host_blocked, and --assume-host-fix shows the case passable once service/model_only.py learns the -ing form."""
    from scripts.toolbench.private_selfcheck import render, run_selfcheck
    from service.model_only import pure_writing
    case = {**CASES[2], "id": "synth_draft_responding", "prompt": "draft a message responding to robin"}
    plan = {**PLANS[2], "id": "synth_draft_responding"}
    (pdir / "cases.yaml").write_text(yaml.safe_dump([case]))
    (pdir / "ideal.yaml").write_text(yaml.safe_dump([plan]))
    if not pure_writing(case["prompt"]):
        pytest.skip("service/model_only.py already keeps reads for 'responding to'; nothing is host-blocked")
    blocked = run_selfcheck(pdir)
    assert blocked[0]["host_blocked"] and not blocked[0]["ok"]
    assert "blocked by the host" in render(blocked)
    fixed = run_selfcheck(pdir, assume_host_fix=True)
    assert fixed[0]["ok"] and not fixed[0]["host_blocked"] and fixed[0]["fails"][0]["ok"]
    assert pure_writing(case["prompt"])                           # the patch was undone afterwards


def test_a_case_can_have_its_own_world(pdir):
    """worlds.json gives one case its own date and pins; other cases stay on world.json."""
    from scripts.toolbench import private, world as W
    (pdir / "worlds.json").write_text(json.dumps(
        {"own": {"today": "2026-09-20", "now": "17:09", "tool_results": {"get_stock_price": "Pinned quote."}}}))
    private._case_worlds.clear()
    own = private.case_world("own", pdir)
    assert own.today.isoformat() == "2026-09-20"
    assert W.make_fakes(own)["get_stock_price"](symbols=["X"]) == "Pinned quote."
    assert private.case_world("own", pdir) is own
    assert private.case_world("other", pdir) is None
    (pdir / "worlds.json").write_text(json.dumps({"bad": {"today": "2026-09-20", "nope": 1}}))
    private._case_worlds.clear()
    with pytest.raises(private.PrivateEvalError, match="worlds.json"):
        private.case_world("bad", pdir)
    private._case_worlds.clear()


def test_a_private_world_can_pin_a_real_tool_result(pdir, monkeypatch):
    """Wording the fakes do not reproduce reaches the model exactly; other tools stay fake."""
    from scripts.toolbench import private, world as W
    spec = {"today": "2026-11-04", "tool_results": {"get_stock_price": "Change: unavailable; do not infer."}}
    w = private.build_private_world(spec)
    fakes = W.make_fakes(w)
    assert fakes["get_stock_price"](symbols=["X"]) == "Change: unavailable; do not infer."
    assert "get_upcoming" in fakes and callable(fakes["get_upcoming"])
    w.reset()
    assert w.fixed_results == spec["tool_results"]
    with pytest.raises(private.PrivateEvalError, match="tool_results"):
        private.build_private_world({"today": "2026-11-04", "tool_results": {"get_stock_price": 1}})
