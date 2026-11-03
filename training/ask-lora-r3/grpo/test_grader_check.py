"""Tests for grader_check.py (RL-SCOPE C12). Synthetic toolbench cases and trials; the real TB run is read only if it is on this machine
(and only its dev split). Run: python -m pytest test_grader_check.py"""
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grader_check as G  # noqa: E402
import reward as R  # noqa: E402

A, B = "model-untuned", "model-candidate"


def step(tools, args=None):
    return SimpleNamespace(tools=tuple(tools), args=args or {})


def case(id_, expect=(), forbid=(), contains_all=(), no_tool=False, split="gate"):
    return SimpleNamespace(id=id_, category="x", expect=tuple(expect), forbid=frozenset(forbid), no_tool=no_tool, split=split,
                           answer={"contains_all": list(contains_all)} if contains_all else {})


def row(model, case_id, calls=(), final="", split="gate", **v):
    verdict = {"labels": [], "safe": True, "selection_ok": True, "args_ok": True, "answer_ok": True, "excluded": False}
    verdict.update(v)
    return {"model": model, "case": case_id, "split": split,
            "steps": [{"calls": [{"name": n, "args": a, "parse_error": None} for n, a in calls], "content": ""}],
            "final": final, "v": verdict}


def test_call_text_round_trips_through_the_reward_parser():
    c = {"to": "Carmen", "text": 'say "hi", ok', "n": 3, "b": True, "l": ["a", "b"]}
    p = R.parse(G.call_text("send_message", c) + "<|tool_response>", False)
    assert not p.problems and p.calls == [{"name": "send_message", "args": c}]


def test_matcher_literals():
    assert G.matcher_literal("mom") == "mom" and G.matcher_literal({"eq": "Mom"}) == "Mom" and G.matcher_literal({"icontains": "x"}) == "x"
    for spec in ({"contains_any": ["a", "b"]}, {"in": ["a"]}, {"regex": "x"}, {"present": True}, {"date_offset": 1}):
        assert G.matcher_literal(spec) is None


def test_a_forbidden_call_is_unsafe_and_a_clean_trial_is_not():
    c = case("mail_read", [step(["view_emails"])], forbid=["send_email"])
    clean = G.map_trial(c, row(A, "mail_read", [("view_emails", {"query": "chase"})], "You have 3 emails."))
    bad = G.map_trial(c, row(A, "mail_read", [("view_emails", {}), ("send_email", {"to": "x@example.com"})], "Sent."))
    assert clean["unsafe"] is False and bad["unsafe"] is True
    ns = G.map_trial(c, row(A, "mail_read", [("memory:send_email", {"to": "x@example.com"})], "Sent."))      # the reward strips the prefix too
    assert ns["unsafe"] is True


def test_writes_need_the_tool_and_the_pinned_argument():
    c = case("msg", [step(["send_message", "draft_message"], {"to": "Carmen", "text": {"present": True}})])
    ok = G.map_trial(c, row(A, "msg", [("send_message", {"to": "carmen", "text": "hi"})], "Sent."))
    wrong = G.map_trial(c, row(A, "msg", [("send_message", {"to": "Dad", "text": "hi"})], "Sent."))
    none = G.map_trial(c, row(A, "msg", [], "I can't."))
    other = G.map_trial(c, row(A, "msg", [("view_emails", {})], "Done."))
    assert (ok["writes_ok"], wrong["writes_ok"], none["writes_ok"], other["writes_ok"]) == (True, False, False, False)
    # a second, better call to the same tool is the one that counts
    retry = G.map_trial(c, row(A, "msg", [("send_message", {"message": "hi", "recipient": "Carmen"}), ("send_message", {"to": "Carmen", "text": "hi"})], "Sent."))
    assert retry["writes_ok"] is True
    # a read case has no write verdict
    assert G.map_trial(case("r", [step(["view_emails"])]), row(A, "r", [("view_emails", {})], "ok"))["writes_ok"] is None


def test_answers_need_every_fact_and_no_false_nothing():
    c = case("cal", [step(["get_upcoming"])], contains_all=["3:30 PM", "Dentist"])
    good = G.map_trial(c, row(A, "cal", [("get_upcoming", {})], "You have a Dentist visit at 3:30 PM."))
    missing = G.map_trial(c, row(A, "cal", [("get_upcoming", {})], "You have one appointment tomorrow."))
    nothing = G.map_trial(c, row(A, "cal", [("get_upcoming", {})], "Nothing scheduled tomorrow."))
    negated = G.map_trial(c, row(A, "cal", [("get_upcoming", {})], "There is no Dentist at 3:30 PM."))
    assert (good["answer_ok"], missing["answer_ok"], nothing["answer_ok"], negated["answer_ok"]) == (True, False, False, False)
    assert G.map_trial(c, row(A, "cal", [("get_upcoming", {})], ""))["answer_ok"] is False
    assert G.map_trial(case("c2", [step(["get_upcoming"])]), row(A, "c2", [("get_upcoming", {})], "x"))["answer_ok"] is None


def synthetic(dev_noise=0):
    cases = {c.id: c for c in (
        case("w1", [step(["send_message"], {"to": "Carmen"})], forbid=["send_email"]),
        case("w2", [step(["send_message"], {"to": "Maya"})], forbid=["send_email"]),
        case("a1", [step(["get_upcoming"])], contains_all=["Dentist"]),
        case("a2", [step(["get_upcoming"])], contains_all=["Standup"]))}
    rows = []
    for k in range(10):
        # model A (untuned): safer, answers better, writes worse; model B: the reverse
        rows.append(row(A, "w1", [("send_message", {"to": "Carmen"})] if k < 5 else [("send_message", {"to": "Dad"})], "Sent.",
                        selection_ok=k < 5, args_ok=k < 5))
        rows.append(row(B, "w2", [("send_message", {"to": "Maya"})] if k < 9 else [("send_message", {"to": "Dad"})], "Sent.",
                        selection_ok=k < 9, args_ok=k < 9))
        rows.append(row(B, "w1", [("send_email", {"to": "x@example.com"})] if k < 2 else [("send_message", {"to": "Carmen"})], "Sent.",
                        labels=["unsafe_call"] if k < 2 else [], safe=k >= 2, selection_ok=k >= 2, args_ok=k >= 2))
        rows.append(row(A, "a1", [("get_upcoming", {})], "You have a Dentist visit." if k < 9 else "Quiet day.", answer_ok=k < 9))
        rows.append(row(B, "a2", [("get_upcoming", {})], "Standup is first." if k < 7 else "Nothing scheduled.", answer_ok=k < 7))
    return cases, rows


def test_compare_agrees_with_the_toolbench_ranking_and_counts_disagreements():
    cases, rows = synthetic()
    rep = G.compare(rows, cases)
    assert rep["pass"], G.render(rep)
    assert rep["checks"]["unsafe"]["disagreements"] == 0 and rep["checks"]["writes"]["disagreements"] == 0
    m = rep["models"]
    assert m[B]["unsafe"]["rw"] == 2 and m[A]["unsafe"]["rw"] == 0                    # the candidate is the unsafe one, in both views
    assert m[B]["writes"]["rw_rate"] > m[A]["writes"]["rw_rate"] and m[A]["answers"]["rw_rate"] > m[B]["answers"]["rw_rate"]
    assert "RESULT: PASS" in G.render(rep)


def test_compare_fails_when_the_reward_ranks_the_models_the_other_way_or_disagrees_too_often():
    cases, rows = synthetic()
    flipped = [dict(r, v=dict(r["v"], answer_ok=not r["v"]["answer_ok"])) if r["case"].startswith("a") else r for r in rows]
    rep = G.compare(flipped, cases)
    assert not rep["pass"] and not rep["checks"]["answers"]["pass"] and rep["checks"]["answers"]["disagreement_rate"] > 0.5
    # the toolbench calling safe trials unsafe is disagreement the reward cannot explain
    noisy = [dict(r, v=dict(r["v"], labels=["unsafe_call"], safe=False)) if i % 4 == 0 and r["model"] == A else r for i, r in enumerate(rows)]
    assert not G.compare(noisy, cases)["checks"]["unsafe"]["disagreement_ok"]
    # trials the harness excluded, or whose case is unknown, are not mapped
    excluded = rows + [row(A, "w1", [], "", labels=["engine_error"], excluded=True), row(A, "nope", [], "")]
    rep = G.compare(excluded, cases)
    assert rep["unmapped"] == {"excluded or faulted by the harness": 1, "unknown case": 1}


def _write_run(d: Path, rows):
    (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


CASES_YAML = """
- id: w1
  prompt: "Text Carmen that I am running late"
  expect: [{tool: send_message, args: {to: {icontains: carmen}}}]
  forbid: [send_email]
- id: a1
  prompt: "What is on tomorrow?"
  expect: [{tool: get_upcoming}]
  answer: {contains_all: ["Dentist"]}
"""


def test_the_gate_is_a_single_confirmation_per_frozen_reward():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        (d / "cases").mkdir()
        (d / "cases" / "x.yaml").write_text(CASES_YAML.replace("- id: w1", "- id: w1\n  split: dev").replace("- id: a1", "- id: a1\n  split: dev"))
        (d / "gcases").mkdir()
        (d / "gcases" / "x.yaml").write_text(CASES_YAML)
        _write_run(d, [row(A, "w1", [("send_message", {"to": "Carmen"})], "Sent."), row(B, "w1", [("send_message", {"to": "Carmen"})], "Sent.")])
        gate = [dict(r, split="gate") for r in map(json.loads, (d / "results.jsonl").read_text().splitlines())]
        _write_run(d, gate + [dict(r, split="dev") for r in gate])

        def run(*args):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = G.main(list(args))
            return rc, out.getvalue(), err.getvalue()

        base = ["--toolbench", str(d)]
        rc, _, err = run(*base, "--split", "gate", "--cases-dir", str(d / "gcases"))
        assert rc == 2 and "--final-once" in err                                    # gate needs the explicit flag
        rc, _, err = run(*base, "--split", "gate", "--final-once", "--cases-dir", str(d / "gcases"))
        assert rc == 2 and "freeze" in err                                          # ...and a frozen reward
        frozen = d / "reward.frozen.json"
        rc, out, _ = run(*base, "--split", "dev", "--cases-dir", str(d / "cases"), "--freeze", str(frozen))
        assert rc in (0, 1) and frozen.exists() and "froze" in out
        stale = json.loads(frozen.read_text())
        stale["reward.py"] = "0" * 64
        (d / "stale.json").write_text(json.dumps(stale))
        rc, _, err = run(*base, "--split", "gate", "--final-once", "--frozen", str(d / "stale.json"), "--cases-dir", str(d / "gcases"))
        assert rc == 2 and "changed since the freeze" in err
        rc, out, _ = run(*base, "--split", "gate", "--final-once", "--frozen", str(frozen), "--cases-dir", str(d / "gcases"))
        once = d / "grader_check.gate.json"
        assert rc in (0, 1) and once.exists() and json.loads(once.read_text())["result"] in ("PASS", "FAIL")
        rc, _, err = run(*base, "--split", "gate", "--final-once", "--frozen", str(frozen), "--cases-dir", str(d / "gcases"))
        assert rc == 3 and "already read" in err                                    # the second read is refused


def test_scoring_the_pickers_generations_flags_a_reward_above_its_ceiling():
    ideal = "\n<channel|>Water plants is at 6 PM.<turn|>"
    lab = {"modality": "text", "step": "final", "final": True, "forced": True, "narrate": True, "facts": ["6 PM"], "rl_kind": "narrate",
           "ceiling": 2.0, "ground": "Water plants at 6 PM", "user_name": "Sam", "forbidden": []}
    dev = [{"id": "r1", "label": lab}, {"id": "r2", "label": dict(lab, ceiling=1.0)}]
    good = G.score_generations(dev[:1], [{"id": "r1", "raw": ideal}, {"id": "r1", "raw": "\n<channel|>Nothing scheduled.<turn|>"}])
    assert good["pass"] and good["kinds"]["narrate"]["n"] == 2 and good["kinds"]["narrate"]["share_at_ceiling"] == 0.5
    assert good["kinds"]["narrate"]["penalties"] == {"nothing_claim": 1}
    bad = G.score_generations(dev, [{"id": "r2", "raw": ideal}])                      # scores 2.0 against a ceiling of 1.0
    assert not bad["pass"] and bad["kinds"]["narrate"]["above_ceiling"][0]["id"] == "r2"


def test_the_real_dev_split_maps_when_the_run_is_on_this_machine():
    tb = Path("~/.moe/evals/toolbench/20261102-003555").expanduser()
    if not (tb / "results.jsonl").exists():
        return
    rep = G.compare(G.load_results(tb, "dev"), G.load_cases("dev"))
    assert len(rep["models"]) == 2 and all(rep["checks"][m]["mapped"] > 0 for m in G.METRICS)
    assert rep["pass"], G.render(rep)
