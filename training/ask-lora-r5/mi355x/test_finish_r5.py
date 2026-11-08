"""finish_r5.py, seal_r5.py and r5_stats.py: dropped rows leave every split and stay aligned; a surviving placeholder is caught;
stats describe the rows on disk; an unfinished build can't be packed; overlap has no exemption."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import finish_r5 as F  # noqa: E402
import r5_stats  # noqa: E402

TOOL_MSG = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "t", "arguments": {}}}]},
            {"role": "tool", "tool_call_id": "c", "content": "r"}]


@pytest.fixture(autouse=True)
def _no_real_validator(request, monkeypatch):
    """The real validate.py imports the pinned service; these tests stub it unless they test it."""
    if request.node.name != "test_validator_failures_maps_lines_to_base_rows_only":
        monkeypatch.setattr(F, "validator_failures", lambda out, repo: {})


def _write(d, ids, content="ok", extra=None):
    for s, meta in F.SPLITS.items():
        (d / f"{s}.jsonl").write_text("".join(json.dumps({"messages": [{"role": "assistant", "content": content}]}) + "\n" for _ in ids))
        (d / meta).write_text("".join(json.dumps({"id": i, **(extra or {})}) + "\n" for i in ids))


def _row(i, family="fam", content="Answer.", msgs=None, think=False, thought=None, kind="final", **meta):
    ex = {"messages": (msgs or [{"role": "user", "content": "q"}]) + [{"role": "assistant", "content": content}], "tools": []}
    m = {"id": i, "family": family, "target_kind": kind, "think": think, "thought": thought, "prompt": "q", "chat_template_kwargs": {"enable_thinking": True}, **meta}
    return ex, m


def _build(d, rows, stats=None):
    d.mkdir(parents=True, exist_ok=True)
    for ex_name, meta_name in (("all.jsonl", "meta.jsonl"), ("train.jsonl", "train.meta.jsonl")):
        (d / ex_name).write_text("".join(json.dumps(e) + "\n" for e, _ in rows))
        (d / meta_name).write_text("".join(json.dumps(m) + "\n" for _, m in rows))
    (d / "valid.jsonl").write_text("")
    (d / "valid.meta.jsonl").write_text("")
    (d / "stats.json").write_text(json.dumps(stats or {"examples": 999, "code_head": "abc"}))


def test_drop_keeps_alignment(tmp_path):
    _write(tmp_path, ["a", "b", "c"])
    assert F.drop_rows(tmp_path, {"b"}) == {s: 1 for s in F.SPLITS}
    for s, meta in F.SPLITS.items():
        assert [json.loads(m)["id"] for m in open(tmp_path / meta)] == ["a", "c"]
        assert sum(1 for _ in open(tmp_path / f"{s}.jsonl")) == 2


def test_placeholder_found(tmp_path):
    _write(tmp_path, ["a"], content=F.PLACEHOLDER)
    assert F.placeholders_left(tmp_path) == ["a"] * len(F.SPLITS)


def test_placeholder_matches_builder():
    builder = F.TRAINING / "ask-lora-r2" / "builder" / "scen_r5_debuglog.py"
    assert f'BASE_WRITES = "{F.PLACEHOLDER}"' in builder.read_text()


def test_stats_count_the_rows_not_the_old_file(tmp_path):
    _build(tmp_path, [_row("a-s0"), _row("b-s0", family="g")])
    s = r5_stats.refresh(tmp_path, status="complete", step="finish")
    assert s["examples"] == 2 and s["train"] == 2
    # this step's code, never the build's: the build's head stays under its own name
    assert s["code_head"] == r5_stats.step_provenance()["code_head"] != "abc"
    assert s["provenance"]["build"]["code_head"] == "abc" and s["provenance"]["finish"]["code_head"] == s["code_head"]
    assert s["by_origin"] == {"scripted": 2}
    assert s["files_sha256"]["all.jsonl"] == r5_stats.sha256(tmp_path / "all.jsonl")
    F.drop_rows(tmp_path, {"b-s0"})
    again = r5_stats.refresh(tmp_path, status="complete", step="finish")
    assert again["examples"] == 1 and again["provenance"]["build"]["code_head"] == "abc"


def test_origins_are_visible():
    assert r5_stats.origin_of(*_row("a", content=F.PLACEHOLDER)) == "placeholder"
    assert r5_stats.origin_of(*_row("a")) == "scripted"
    assert r5_stats.origin_of(*_row("a", distilled=True, origin="base_r4_reuse")) == "base_r4_reuse"
    assert r5_stats.origin_of(*_row("a", distilled=True)) == "base_generated"
    assert r5_stats.trace_origin(_row("a")[1]) == "none"
    assert r5_stats.trace_origin(_row("a", think=True, thought="x")[1]) == "scripted"
    assert r5_stats.trace_origin(_row("a", think=True, thought="x", distilled=True)[1]) == "base"


def test_forced_empty_is_found(tmp_path):
    _build(tmp_path, [_row("a-s1", msgs=TOOL_MSG), _row("b-s1", msgs=TOOL_MSG, think=True, thought="looked"), _row("c-s0"),
                      _row("d-s1", msgs=TOOL_MSG, content=F.PLACEHOLDER)])
    assert F.forced_empty_rows(tmp_path) == {"a-s1"}


def test_never_sampled_is_not_sampled_and_failed(tmp_path):
    stats = tmp_path / "s.json"
    stats.write_text(json.dumps({"r5": {"families": {"a": {"no_samples": ["x-s0"]}, "b": {"no_samples": ["y-s0", "z-s1"]}}}}))
    assert F.never_sampled_ids(stats) == {"x-s0", "y-s0", "z-s1"}
    stats.write_text(json.dumps({"families": {}}))
    with pytest.raises(SystemExit):
        F.never_sampled_ids(stats)


def _finish_inputs(tmp_path, rows, no_samples, drop):
    build = tmp_path / "b"
    _build(build, rows)
    (tmp_path / "chosen.jsonl").write_text("")
    (tmp_path / "drop.ids").write_text("".join(i + "\n" for i in drop))
    (tmp_path / "stats.json").write_text(json.dumps({"r5": {"families": {"f": {"no_samples": no_samples}}}}))
    return ["--build", str(build), "--chosen", str(tmp_path / "chosen.jsonl"), "--drop", str(tmp_path / "drop.ids"),
            "--choose-stats", str(tmp_path / "stats.json"), "--out", str(tmp_path / "o")]


def test_a_build_with_unsampled_finals_never_says_complete(tmp_path, monkeypatch):
    """The bug this pins: choose_r5's drop list holds never-sampled placeholders too, so they used to count as answered and dropped."""
    monkeypatch.setattr(F, "leaking_turns", lambda out: set())
    rows = [_row("a-s0", content=F.PLACEHOLDER), _row("b-s0", content=F.PLACEHOLDER), _row("c-s0")]
    args = _finish_inputs(tmp_path, rows, ["a-s0", "c-s0"], ["a-s0", "b-s0"])
    with pytest.raises(SystemExit):                                      # refuses without the flag
        F.main(args)
    assert not (tmp_path / "o").exists()
    assert F.main(args + ["--allow-incomplete"]) == 0
    stats = json.load(open(tmp_path / "o" / "stats.json"))
    assert stats["status"] == "incomplete_no_base_answers"
    assert stats["placeholders_left"] == 1 and stats["unsampled_scripted_finals"] == 1
    assert stats["drops"] == {"no_base_answer_passed": 1}                # b was sampled and failed; a was never sampled and stays


def test_a_fully_sampled_build_is_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "leaking_turns", lambda out: set())
    rows = [_row("a-s0", content=F.PLACEHOLDER), _row("c-s0")]
    assert F.main(_finish_inputs(tmp_path, rows, [], ["a-s0"])) == 0
    stats = json.load(open(tmp_path / "o" / "stats.json"))
    assert stats["status"] == "complete" and stats["examples"] == 1 and stats["placeholders_left"] == 0


def test_an_unsampled_forced_row_waits_and_a_failed_one_drops(tmp_path, monkeypatch):
    """A scripted final with no thought only drops once base was asked. Dropping it earlier would also hide that it was never sampled."""
    monkeypatch.setattr(F, "leaking_turns", lambda out: set())
    rows = [_row("a-s1", msgs=TOOL_MSG), _row("b-s1", msgs=TOOL_MSG)]
    assert F.main(_finish_inputs(tmp_path, rows, ["a-s1"], []) + ["--allow-incomplete"]) == 0
    stats = json.load(open(tmp_path / "o" / "stats.json"))
    assert stats["status"] == "incomplete_no_base_answers" and stats["unsampled_scripted_finals"] == 1
    assert stats["forced_empty_awaiting_base"] == 1 and stats["drops"] == {"forced_empty_thought": 1} and stats["examples"] == 1


def test_placeholder_finals_after_a_tool_result_are_counted_as_waiting(tmp_path, monkeypatch):
    """The coordinator's catch: placeholder finals with no thought after a tool result were left out of the waiting count, so it
    didn't add up to rendered_shapes forced_empty."""
    monkeypatch.setattr(F, "leaking_turns", lambda out: set())
    rows = [_row("p-s1", msgs=TOOL_MSG, content=F.PLACEHOLDER), _row("a-s1", msgs=TOOL_MSG)] + _chain("t", TOOL_MSG)
    assert F.main(_finish_inputs(tmp_path, rows, ["p-s1", "a-s1"], ["p-s1", "a-s1"]) + ["--allow-incomplete"]) == 0
    stats = json.load(open(tmp_path / "o" / "stats.json"))
    assert stats["forced_empty_awaiting_base"] == 2
    assert stats["forced_empty_awaiting_base_by_text"] == {"scripted_final": 1, "placeholder_final": 1}
    assert stats["forced_tool_steps_awaiting_traces"] == 1
    assert stats["rendered_shapes"]["forced_empty"] == 3 and stats["drops"] == {}


def test_chat_rows_base_answered_with_a_tool_drop_with_their_own_reason_and_counts(tmp_path, monkeypatch):
    """The user's decisions on chat_direct_r4 / knowledge_direct: a row base answered with any tool call drops, never back to
    its scripted text, and the finished stats count each family's drops by tool and the rows it has left."""
    monkeypatch.setattr(F, "leaking_turns", lambda out: set())
    rows = [_row("c-s0", family="chat_direct_r4"), _row("d-s0", family="chat_direct_r4"), _row("k-s0", family="knowledge_direct"),
            _row("w-s0", family="knowledge_direct"), _row("x-s0", content=F.PLACEHOLDER)]
    args = _finish_inputs(tmp_path, rows, [], ["c-s0", "k-s0", "w-s0", "x-s0"])
    (tmp_path / "stats.json").write_text(json.dumps({"r5": {"families": {
        "chat_direct_r4": {"no_samples": [], "drop_tool": {"c-s0": ["web_search"]}},
        "knowledge_direct": {"no_samples": [], "drop_tool": {"k-s0": ["web_search"], "w-s0": ["calculate", "wikipedia_summary"]}},
        "fam": {"no_samples": [], "drop": ["x-s0"]}}}}))
    assert F.main(args) == 0
    stats = json.load(open(tmp_path / "o" / "stats.json"))
    assert stats["status"] == "complete" and stats["examples"] == 1
    assert stats["drops"] == {"base_answered_with_tool": 3, "no_base_answer_passed": 1}
    assert stats["tool_drop"] == {"dropped": 3, "families": {
        "chat_direct_r4": {"dropped": 1, "by_tool": {"web_search": 1}, "rows_left": 1},
        "knowledge_direct": {"dropped": 2, "by_tool": {"web_search": 1, "calculate": 1, "wikipedia_summary": 1}, "rows_left": 0}}}
    reasons = {r["id"]: r["reason"] for r in map(json.loads, open(tmp_path / "o" / "drop_manifest.jsonl"))}
    assert reasons == {"c-s0": "base_answered_with_tool", "k-s0": "base_answered_with_tool", "w-s0": "base_answered_with_tool",
                       "x-s0": "no_base_answer_passed"}


def test_a_tool_drop_missing_from_drop_ids_stops_the_build(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "leaking_turns", lambda out: set())
    args = _finish_inputs(tmp_path, [_row("c-s0", family="chat_direct_r4")], [], [])
    (tmp_path / "stats.json").write_text(json.dumps({"r5": {"families": {"chat_direct_r4": {"no_samples": [], "drop_tool": {"c-s0": ["calculate"]}}}}}))
    with pytest.raises(SystemExit):
        F.main(args)


def _chain(prefix, final_msgs):
    first = _row(f"{prefix}-s0", kind="load", content="", think=True, thought="Load it for Monday, February 15, 2027.")
    call = _row(f"{prefix}-s1", msgs=TOOL_MSG, kind="call", content="")
    final = _row(f"{prefix}-s2", msgs=final_msgs, think=True, thought="read it")
    return [first, call, final]


def test_a_tool_step_with_no_trace_takes_its_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "leaking_turns", lambda out: set())
    rows = _chain("t", TOOL_MSG) + [_row("u-s0")]
    assert F.main(_finish_inputs(tmp_path, rows, [], [])) == 0
    assert [json.loads(m)["id"] for m in open(tmp_path / "o" / "meta.jsonl")] == ["u-s0"]
    reasons = {r["id"]: r["reason"] for r in map(json.loads, open(tmp_path / "o" / "drop_manifest.jsonl"))}
    assert reasons == {"t-s1": "forced_empty_thought", "t-s0": "turn_lost_a_tool_step", "t-s2": "turn_lost_a_tool_step"}


def test_a_pipeline_check_without_traces_keeps_tool_steps_and_counts_them(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "leaking_turns", lambda out: set())
    assert F.main(_finish_inputs(tmp_path, _chain("t", TOOL_MSG), [], []) + ["--allow-incomplete"]) == 0
    stats = json.load(open(tmp_path / "o" / "stats.json"))
    assert stats["examples"] == 3 and stats["forced_tool_steps_awaiting_traces"] == 1 and stats["drops"] == {}


def test_pack_refuses_an_unfinished_or_stale_build(tmp_path):
    sys.path.insert(0, str(F.TRAINING / "ask-lora-r4" / "a6000"))
    import pack_r4
    _build(tmp_path, [_row("a-s0")], stats={"status": "incomplete_no_base_answers", "examples": 1})
    assert "not complete" in pack_r4.stats_problem(tmp_path)
    _build(tmp_path, [_row("a-s0")], stats={"status": "complete", "examples": 8873})
    assert "8873" in pack_r4.stats_problem(tmp_path)
    _build(tmp_path, [_row("a-s0")], stats={"status": "complete", "examples": 1})
    assert pack_r4.stats_problem(tmp_path) is None
    _build(tmp_path, [_row("a-s0")], stats={"examples": 99})      # r4's build, untouched
    assert pack_r4.stats_problem(tmp_path) is None


def test_seal_policy():
    import seal_r5 as S
    clean = {"family": "x", "current": {}, "history": {}}
    private_only = {"family": "debug_log_r5", "current": {"private": ("p", 1.0)}, "history": {}}
    public = {"family": "debug_log_r5", "current": {"public": ("tb:x", 1.0)}, "history": {}}
    other_family = {"family": "chat_voice_r4", "current": {"private": ("p", 1.0)}, "history": {}}
    assert not S.removal(clean, False)
    assert S.removal(private_only, False) and S.removal(public, False) and S.removal(other_family, False)
    assert not S.removal(private_only, True)                 # only the user can ask for this
    assert S.removal(public, True) and S.removal(other_family, True)


def test_seal_refuses_the_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("WISP_REPO", "/some/pin")
    r = subprocess.run([sys.executable, str(F.HERE / "seal_r5.py"), "--build", str(tmp_path)], capture_output=True, text=True,
                       env={"WISP_REPO": "/some/pin", "PATH": "/usr/bin"})
    assert r.returncode != 0
    assert "WISP_REPO" in r.stderr or "ModuleNotFoundError" in r.stderr


@pytest.mark.parametrize("name", ["finish_r5.py", "seal_r5.py"])
def test_docstrings_name_the_rule(name):
    assert "test question" in (F.HERE / name).read_text()


def test_base_rows_the_validator_flags_drop_and_scripted_ones_stay(tmp_path, monkeypatch):
    """The user's call, 2026-11-08 ("Drop them"): a base answer that passed the chooser but fails the builder's validator leaves
    the build with its keys; a flagged base tool step takes its turn; a flagged scripted row stays so validation still fails."""
    monkeypatch.setattr(F, "leaking_turns", lambda out: set())
    rows = [_row("a-s0", distilled=True), _row("s-s0")] + _chain("t", TOOL_MSG)
    rows[3][1].update(think=True, thought="Read it now.", distilled=True)           # t-s1: base's tool-step thought
    flagged = {"a-s0": ["injection:payload_in_reply"], "t-s1": ["think:injection_payload"]}
    monkeypatch.setattr(F, "validator_failures", lambda out, repo: flagged)
    assert F.main(_finish_inputs(tmp_path, rows, [], [])) == 0
    assert [json.loads(m)["id"] for m in open(tmp_path / "o" / "meta.jsonl")] == ["s-s0"]
    man = {r["id"]: r for r in map(json.loads, open(tmp_path / "o" / "drop_manifest.jsonl"))}
    assert {i: r["reason"] for i, r in man.items()} == {"a-s0": "validator_failed_after_choose", "t-s1": "validator_failed_after_choose",
                                                       "t-s0": "turn_lost_a_tool_step", "t-s2": "turn_lost_a_tool_step"}
    assert man["a-s0"]["problems"] == ["injection:payload_in_reply"]


def test_validator_failures_maps_lines_to_base_rows_only(tmp_path, monkeypatch):
    stub = tmp_path / "validate.py"
    stub.write_text("import json, sys\n"
                    "out = sys.argv[sys.argv.index('--problems-out') + 1]\n"
                    "rows = [{'file': 'all.jsonl', 'line': 1, 'key': 'x', 'where': 'all.jsonl:1 a'},\n"
                    "        {'file': 'all.jsonl', 'line': 2, 'key': 'y', 'where': 'all.jsonl:2:3 b'},\n"
                    "        {'file': 'all.jsonl', 'line': 1, 'key': 'z', 'where': 'all.jsonl:1 c'}]\n"
                    "open(out, 'w').write(''.join(json.dumps(r) + '\\n' for r in rows)); sys.exit(1)\n")
    monkeypatch.setattr(F, "VALIDATE", stub)
    _build(tmp_path / "b", [_row("base-s0", distilled=True), _row("scripted-s0")])
    assert F.validator_failures(tmp_path / "b", "/pin") == {"base-s0": ["x", "z"]}
    with pytest.raises(SystemExit):
        F.validator_failures(tmp_path / "b", None)                                 # never against this checkout


def test_validate_writes_every_problem_line():
    src = (F.VALIDATE).read_text()
    assert "--problems-out" in src and "on_bad=record if sink else None" in src
