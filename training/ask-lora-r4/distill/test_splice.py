"""Tests for splice.py on a tiny synthetic build:  python -m pytest training/ask-lora-r4/distill/test_splice.py"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gen import request_hash  # noqa: E402
import splice  # noqa: E402

TOOLS = [{"type": "function", "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}]


def ex(i, reply="scripted", tool_last=False):
    msgs = [{"role": "user", "content": f"q{i}"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": "t", "arguments": {"a": i}}}]},
            {"role": "tool", "tool_call_id": "c", "content": "ok"}]
    msgs.append({"role": "assistant", "content": "", "tool_calls": msgs[1]["tool_calls"]} if tool_last
                else {"role": "assistant", "content": reply, "reasoning_content": "old"})
    return {"messages": msgs, "tools": TOOLS}


def meta(i, fam="chat_voice_r4"):
    return {"id": f"{fam}-{i:03d}", "family": fam, "think": False, "thought": None, "x": i}


@pytest.fixture
def build(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    rows = [(ex(i, "é scripted"), meta(i, "chat_voice_r4" if i < 4 else "other")) for i in range(6)]
    for ex_name, meta_name, idx in (("all.jsonl", "meta.jsonl", range(6)),
                                    ("train.jsonl", "train.meta.jsonl", [0, 1, 2, 4, 5]),
                                    ("valid.jsonl", "valid.meta.jsonl", [3])):
        (d / ex_name).write_text("".join(json.dumps(rows[i][0], ensure_ascii=False) + "\n" for i in idx))
        (d / meta_name).write_text("".join(json.dumps(rows[i][1], ensure_ascii=False) + "\n" for i in idx))
    (d / "partial.jsonl").write_text("p\n")
    return d, rows


def chosen_file(tmp_path, rows, picks, reasoning="thinking about it", bad_hash=None):
    p = tmp_path / "chosen.jsonl"
    with open(p, "w") as f:
        for i in picks:
            e, m = rows[i]
            f.write(json.dumps({"id": m["id"], "family": m["family"], "k": 1, "content": f"new {i} ü",
                                "reasoning": reasoning, "request_hash": bad_hash or request_hash(
                                    e["messages"][:-1], e["tools"])}, ensure_ascii=False) + "\n")
    return str(p)


def lines(p):
    return open(p, encoding="utf-8").read().splitlines()


def test_splice_out_and_idempotent(build, tmp_path):
    d, rows = build
    ch = chosen_file(tmp_path, rows, [1, 3])
    before = {n: (d / n).read_bytes() for n in os.listdir(d)}
    r = splice.splice(str(d), [ch], str(tmp_path / "o"))
    assert {n: (d / n).read_bytes() for n in os.listdir(d)} == before          # source untouched
    o = tmp_path / "o"
    assert r["spliced_per_file"]["all.jsonl"] == 2 and r["spliced_per_file"]["valid.jsonl"] == 1
    assert r["unchanged_per_family"] == {"chat_voice_r4": 2} and r["think"] == 2
    a = [json.loads(x) for x in lines(o / "all.jsonl")]
    assert a[1]["messages"][-1] == {"role": "assistant", "content": "new 1 ü"}
    m = json.loads(lines(o / "meta.jsonl")[1])
    assert (m["distilled"], m["think"], m["thought"], m["distill_k"], m["x"]) == (True, True, "thinking about it", 1, 1)
    for i in (0, 2, 4, 5):
        assert lines(o / "all.jsonl")[i] == lines(d / "all.jsonl")[i]
    assert lines(o / "valid.jsonl")[0] == lines(o / "all.jsonl")[3]
    assert (o / "partial.jsonl").read_text() == "p\n"
    snap = {n: (o / n).read_bytes() for n in os.listdir(o)}
    splice.splice(str(o), [ch])                                                 # in place, twice
    assert {n: (o / n).read_bytes() for n in os.listdir(o) if n != "distill.json"} == {
        n: v for n, v in snap.items() if n != "distill.json"}


def test_no_reasoning_means_no_think(build, tmp_path):
    d, rows = build
    splice.splice(str(d), [chosen_file(tmp_path, rows, [0], reasoning="  ")])
    assert json.loads(lines(d / "meta.jsonl")[0])["thought"] is None


def test_hash_mismatch_writes_nothing(build, tmp_path):
    d, rows = build
    before = {n: (d / n).read_bytes() for n in os.listdir(d)}
    with pytest.raises(SystemExit) as e:
        splice.splice(str(d), [chosen_file(tmp_path, rows, [0, 1], bad_hash="0" * 16)], str(tmp_path / "o"))
    assert "chat_voice_r4-000" in str(e.value) and not (tmp_path / "o").exists()
    assert {n: (d / n).read_bytes() for n in os.listdir(d)} == before


def test_tool_call_last_message_aborts(build, tmp_path):
    d, rows = build
    rows[2] = (ex(2, tool_last=True), rows[2][1])
    for n in ("all.jsonl",):
        (d / n).write_text("".join(json.dumps(r[0], ensure_ascii=False) + "\n" for r in rows))
    with pytest.raises(SystemExit, match="not a plain assistant"):
        splice.splice(str(d), [chosen_file(tmp_path, rows, [2])], str(tmp_path / "o"))


def test_missing_ids_warn(build, tmp_path):
    d, rows = build
    p = tmp_path / "c2.jsonl"
    p.write_text(json.dumps({"id": "ghost", "family": "f", "k": 0, "content": "x", "reasoning": None,
                             "request_hash": "h"}) + "\n")
    r = splice.splice(str(d), [chosen_file(tmp_path, rows, [0]), str(p)], str(tmp_path / "o"))
    assert r["missing_ids"] == ["ghost"] and r["spliced_per_file"]["all.jsonl"] == 1
