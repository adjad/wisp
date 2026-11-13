"""Offline tests for proactive_probe: a fake engine, no network, WISP_HOME in a temp dir.

    cd <checkout with service/ and scripts/> && WISP_HOME=<empty temp dir> \\
        .venv/bin/python -m pytest -q -p no:cacheprovider training/ask-lora-r5/demo/test_proactive_probe.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from argparse import Namespace
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
MOE = Path.home() / ".moe"


def _snapshot() -> dict:
    out = {}
    if MOE.exists():
        for entry in os.scandir(MOE):
            out[entry.name] = entry.stat().st_mtime_ns
            if entry.is_dir(follow_symlinks=False):
                for sub in os.scandir(entry.path):
                    out[entry.name + "/" + sub.name] = sub.stat().st_mtime_ns
    return out


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    home = os.environ.get("WISP_HOME", "")
    assert home and Path(home).resolve() != MOE.resolve(), "set WISP_HOME to an empty temp dir"
    before = _snapshot()
    spec = importlib.util.spec_from_file_location("proactive_probe", HERE / "proactive_probe.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["proactive_probe"] = mod
    spec.loader.exec_module(mod)
    if str(mod.REPO) not in sys.path:
        sys.path.append(str(mod.REPO))               # fallback only: the cwd checkout wins
    mod.P.load()
    assert _snapshot() == before, "importing the probe touched ~/.moe"
    return mod


# --- a fake engine whose replies come from the gold labels ------------------------------------


class FakeClient:
    def __init__(self, replies: dict[str, str], default: str):
        self.replies, self.default, self.calls = replies, default, []

    async def chat(self, model, messages, *, max_tokens=900, temperature=None, **extra):
        user = messages[1]["content"]
        self.calls.append({"model": model, "max_tokens": max_tokens, "temperature": temperature, "extra": extra})
        text = self.default
        for key, reply in self.replies.items():
            if key in user:
                text = reply
                break
        return {"choices": [{"message": {"content": text}, "finish_reason": "stop" if text else "stop"}],
                "usage": {"completion_tokens": max(1, len(text) // 4),
                          "completion_tokens_details": {"reasoning_tokens": 0}}}


def triage_reply(item, *, junk=False, sensitive=False, ask=False):
    asks = item["lines"][-1] if ask else ""
    return json.dumps({"title": item["title"], "summary": "", "junk": junk, "sensitive": sensitive,
                       "asks_of_me": asks})


def perfect_replies(probe) -> dict[str, str]:
    P = probe.P
    replies: dict[str, str] = {}
    for job in probe.jobs():
        for case in job["cases"]:
            name, cid = job["name"], case["id"]
            if name in ("triage", "autoadd", "hold", "followup"):
                item = case["item"]
                if name == "triage":
                    kw = dict(junk=case["gold"] == "junk", sensitive=case["gold"] == "sensitive",
                              ask=case["gold"] in ("ask", "deadline"))
                elif name == "autoadd":
                    kw = dict(junk=cid == "promo", sensitive=cid == "sensitive_date", ask=case["gold"][0] is not None)
                elif name == "hold":
                    kw = dict(ask=case["gold"])
                else:
                    kw = dict(ask=case["gold"])
                replies[P.triage.material(item)] = triage_reply(item, **kw)
            elif name == "judge":
                sc = case["scenario"]
                material, ids = P.judge.build_input(sc["survivors"], sc["sched"], probe.NOW_TS, probe.TZ)
                picks = [k for k, v in ids.items() if v["kind"] == "item" and v["survivor"]["tier"] == "A"]
                items = [{"title": "Reply to " + ids[k]["survivor"]["item"]["who"], "action": "reply", "why": "",
                          "due": "", "sources": [k], "urgency": "high"} for k in picks]
                replies["<<<LINES\n" + material + "\nLINES>>>"] = json.dumps({"summary": "", "items": items})
            elif name == "draft_bg":
                replies["<<<ITEM\n" + case["item"]["text"] + "\nITEM>>>"] = "Thanks, let me check and get back to you soon."
            elif name == "draft_mail":
                replies[case["body"]] = "Thanks for asking. I will check and confirm soon."
    return replies


def args(**kw):
    return Namespace(**{"models": "BASE,CAND", "base_url": "http://unused.test", "api_key": None, "reps": 2,
                        "quiet": True, "out": None, **kw})


def run(probe, client, **kw):
    return asyncio.run(probe.run(args(**kw), client=client))


# --- the tests --------------------------------------------------------------------------------


def test_gold_is_consistent_with_production_code(probe):
    """A model that answers exactly the gold labels scores 100% on every job, so the gold and the
    production parsers/gates agree (a miss here is a gold or probe bug, not a model result)."""
    client = FakeClient(perfect_replies(probe), default="")
    result = run(probe, client, reps=1)
    for model in ("BASE", "CAND"):
        for name, job in result["models"][model]["jobs"].items():
            wrong = [(c["id"], c["reps"][0]["pred"], c["reps"][0].get("gold"), c["reps"][0].get("checks"),
                      c["reps"][0].get("why")) for c in job["cases"] if not c["reps"][0]["correct"]]
            assert not wrong, f"{name}: {wrong}"
    assert all(v["verdict"] == "CAND not worse" for v in result["compare"].values())


def test_case_counts(probe):
    counts = {j["name"]: len(j["cases"]) for j in probe.jobs()}
    assert counts == {"triage": 12, "judge": 8, "autoadd": 10, "hold": 8, "followup": 10, "draft_bg": 8, "draft_mail": 5}


def test_budget_and_thinking_are_production(probe):
    """Whatever the checkout's roster says, the call carries production's budget, thinking kwargs, temperature 0."""
    P = probe.P
    client = FakeClient(perfect_replies(probe), default="")
    run(probe, client, reps=1)
    allowed = {tuple(t) for t in (P.triage.TOKENS, P.judge.TOKENS, P.prepare.DRAFT_TOKENS, (probe.MAIL_DRAFT_TOKENS,) * 2)}
    for c in client.calls:
        assert c["temperature"] == 0
        assert c["extra"] == P.config.thinking_kwargs(c["model"], on=False)
        assert any(c["max_tokens"] in pair for pair in allowed)
    for model in probe.ALIASES.values():
        listed = P.mail_gists._thinking_off(model)
        assert probe.budget(model, (160, 1400)) == (160 if listed else 1400)
    assert 320 in {c["max_tokens"] for c in client.calls}


def test_worse_candidate_is_flagged(probe):
    """CAND answers the judge with prose and triages everything as junk: both jobs are CAND WORSE."""
    class Split(FakeClient):
        async def chat(self, model, messages, **kw):
            if model == probe.ALIASES["CAND"]:
                user = messages[1]["content"]
                if user.startswith("<<<LINES"):
                    self.default, self.replies = "I would start with the coffee one.", {}
                else:
                    self.default, self.replies = '{"title":"x","summary":"","junk":true,"sensitive":false,"asks_of_me":""}', {}
                return await FakeClient.chat(self, model, messages, **kw)
            self.default, self.replies = "", perfect
            return await FakeClient.chat(self, model, messages, **kw)

    perfect = perfect_replies(probe)
    result = run(probe, Split({}, ""), reps=1)
    assert result["compare"]["judge"]["verdict"].startswith("CAND WORSE")
    assert result["compare"]["triage"]["verdict"].startswith("CAND WORSE")
    assert result["compare"]["judge"]["delta"] < 0
    assert result["compare"]["judge"]["cand_invalid"] == 8


def test_verdict_threshold(probe):
    base = {"cases": 10, "accuracy": 0.8, "empty": 0, "invalid": 0}
    assert probe.verdict(base, {**base, "accuracy": 0.7})["verdict"] == "CAND not worse"       # exactly one case
    assert probe.verdict(base, {**base, "accuracy": 0.6})["verdict"].startswith("CAND WORSE")
    assert "empties" in probe.verdict(base, {**base, "empty": 1})["verdict"]
    assert "invalids" in probe.verdict(base, {**base, "invalid": 1})["verdict"]
    assert probe.verdict(base, {**base, "accuracy": 1.0})["delta"] == 0.2


def test_aggregate_math(probe):
    row = lambda ok, parsed=True, chars=5, finish="stop", s=1.0: dict(  # noqa: E731
        correct=ok, parsed=parsed, chars=chars, finish=finish, seconds=s, tokens=10, reasoning=0, json_valid=parsed)
    cases = [{"id": "a", "reps": [row(True), row(True)]},
             {"id": "b", "reps": [row(True), row(False, parsed=False, chars=0, finish="length", s=3.0)]}]
    agg = probe.aggregate(cases)
    assert (agg["reps"], agg["correct"], agg["accuracy"]) == (4, 3, 0.75)
    assert (agg["invalid"], agg["empty"], agg["not_stop"], agg["flaky_cases"]) == (1, 1, 1, 1)
    assert agg["mean_seconds"] == 1.5 and agg["json_valid_rate"] == 0.75


def test_draft_checks(probe):
    case = {"banned": [r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b"]}
    assert probe.draft_score(case, "Thanks, I will check and confirm.", "stop", "x")["correct"]
    assert not probe.draft_score(case, "Meet at 3pm.", "stop", "x")["correct"]
    assert not probe.draft_score(case, "I've sent it already.", "stop", "x")["correct"]
    assert not probe.draft_score(case, "**Sure**, thanks.", "stop", "x")["correct"]
    assert not probe.draft_score(case, "Thanks.", "length", "x")["correct"]
    assert not probe.draft_score(case, "", "stop", "")["correct"]


def test_judge_overlap_rule(probe):
    sc = probe.judge_scenario("s2")
    assert sorted(s["key"] for s in sc["survivors"] if s["tier"] == "A") == ["priya", "ravi", "rent"]
    assert len(sc["gold"]) == 3


def test_requires_wisp_home(probe, monkeypatch):
    monkeypatch.delenv("WISP_HOME")
    with pytest.raises(SystemExit):
        probe.require_wisp_home()
    monkeypatch.setenv("WISP_HOME", str(MOE))
    with pytest.raises(SystemExit):
        probe.require_wisp_home()


def test_parse_models(probe):
    assert probe.parse_models("BASE,CAND") == [("BASE", probe.ALIASES["BASE"]), ("CAND", probe.ALIASES["CAND"])]
    assert probe.parse_models("x=some-model,y") == [("x", "some-model"), ("y", "y")]


def test_whole_run_leaves_home_untouched(probe, tmp_path):
    before = _snapshot()
    out = tmp_path / "r.json"
    result = run(probe, FakeClient(perfect_replies(probe), default=""), reps=1)
    out.write_text(json.dumps(result))
    assert json.loads(out.read_text())["compare"]
    assert _snapshot() == before
    table = probe.markdown(result)
    assert "| triage |" in table and "CAND not worse" in table
