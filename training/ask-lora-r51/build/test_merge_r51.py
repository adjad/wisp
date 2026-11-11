import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_r51 as M  # noqa: E402


def ex(tool=None, final=None, user="hi"):
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": user}]
    if tool:
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": tool, "arguments": {"to": "x"}}}]})
    else:
        msgs.append({"role": "assistant", "content": final})
    return {"messages": msgs, "tools": []}


def meta(id_, family, prompt, kind, thread=None, gold=None, clarify=False):
    return {"id": id_, "family": family, "prompt": prompt, "target_kind": kind, "thread": thread, "gold": gold,
            "clarify": clarify}


def write_dir(d, rows, valid_ids=()):
    d.mkdir(parents=True, exist_ok=True)
    for name, sel in (("all", rows), ("train", [r for r in rows if r[1]["id"] not in valid_ids]),
                      ("valid", [r for r in rows if r[1]["id"] in valid_ids])):
        suf = "" if name == "all" else name + "."
        (d / f"{name}.jsonl").write_text("".join(json.dumps(e) + "\n" for e, _ in sel))
        (d / (f"{name}.meta.jsonl" if name != "all" else "meta.jsonl")).write_text(
            "".join(json.dumps(m) + "\n" for _, m in sel))
    (d / "drop_manifest.jsonl").write_text(json.dumps({"id": "old-1-s0", "reason": "semantic_review"}) + "\n")


def jl(p):
    return [json.loads(x) for x in Path(p).read_text().splitlines() if x.strip()]


def fixture(tmp):
    r5 = [
        (ex("send_message", user="text bo"), meta("w-000-s0", "compose_r3", "text bo", "call")),
        (ex(final="done", user="text bo"), meta("w-000-s1", "compose_r3", "text bo", "final")),
        (ex("send_message", user="text cy"), meta("w-001-s0", "compose_r3", "text cy", "call")),
        (ex(final="ok", user="what is up"), meta("r-000-s0", "restraint_r4", "what is up", "final")),
        (ex(final="sure", user="tell me"), meta("a-000-s0", "info_answers_r4", "tell me", "final")),
        (ex("list_dir", user="show"), meta("a-001-s0", "info_answers_r4", "show", "call")),
    ]
    write_dir(tmp / "r5", r5, valid_ids={"a-001-s0"})
    (tmp / "dec.jsonl").write_text(
        json.dumps({"id": "w-000-s1", "turn": "w-000", "decision": "DROP_TURN", "reason": "bad body"}) + "\n"
        + json.dumps({"id": "w-001-s0", "turn": "w-001", "decision": "KEEP_AFTER_CHECK", "reason": "ok"}) + "\n")
    (tmp / "lab.jsonl").write_text(
        json.dumps({"id": "w-000-s0", "intent": "direct_send"}) + "\n" + json.dumps({"id": "w-001-s0", "intent": "draft"}) + "\n")
    return tmp


def args(tmp, new=None):
    class A:
        r5 = str(tmp / "r5"); decisions = str(tmp / "dec.jsonl"); labels = str(tmp / "lab.jsonl")
        out = str(tmp / "out"); wisp_repo = "x"; no_validate = True
    A.new = str(new) if new else None
    return A


def test_turn_drop_no_new(tmp_path):
    fixture(tmp_path)
    stats, v = M.run(args(tmp_path))
    assert v is None
    ids = [m["id"] for m in jl(tmp_path / "out" / "meta.jsonl")]
    assert ids == ["w-001-s0", "r-000-s0", "a-000-s0", "a-001-s0"]
    assert stats["rows_dropped"] == 2 and stats["turns_dropped"] == 1
    man = jl(tmp_path / "out" / "drop_manifest.jsonl")
    assert [m["source"] for m in man] == ["r51_audit", "r51_audit", "r5"]
    assert [m["id"] for m in jl(tmp_path / "out" / "valid.meta.jsonl")] == ["a-001-s0"]   # r5 side kept
    assert len(jl(tmp_path / "out" / "train.jsonl")) == 3


def test_stats_counts(tmp_path):
    fixture(tmp_path)
    stats, _ = M.run(args(tmp_path))
    b, a = stats["before_r5_final"], stats["after_r51"]
    assert b["rows"] == 6 and a["rows"] == 4
    assert a["by_family"] == {"compose_r3": 1, "info_answers_r4": 2, "restraint_r4": 1}
    assert a["by_target_kind"] == {"call": 2, "final": 2}
    assert a["by_target_tool"] == {"list_dir": 1, "send_message": 1}
    assert b["by_intent"] == {"direct_send": 1, "draft": 1, "leave_alone_or_clarify": 1, "answer": 2, "read_or_answer": 1}
    assert a["by_intent"]["draft"] == 1 and "direct_send" not in a["by_intent"]
    assert len(jl(tmp_path / "out" / "sample_labels.jsonl")) == 4
    assert len(stats["combined_sha256"]) == 64


def new_rows():
    rows = []
    for g in range(40):
        for k, intent in enumerate(("direct_send", "draft")):
            p = f"new prompt number {'abcdefghij'[g % 10]}{g} variant {k}"
            rows.append((ex("draft_message", user=p), meta(f"n-{g:03d}-{k}-s0", M.NEW_FAMILY, p, "call",
                                                         gold={"group": f"grp{g}", "intent": intent})))
    # shares a folded prompt with an r5 valid row -> must follow it into valid
    rows.append((ex(final="x", user="Show"), meta("n-clash-s0", M.NEW_FAMILY, "show", "final",
                                                  gold={"group": "grpclash", "intent": "draft"})))
    return rows


def test_groups_stay_together(tmp_path):
    fixture(tmp_path)
    write_dir(tmp_path / "new", new_rows())
    stats, _ = M.run(args(tmp_path, tmp_path / "new"))
    out = tmp_path / "out"
    side = {}
    for s in ("train", "valid"):
        for m in jl(out / f"{s}.meta.jsonl"):
            side[m["id"]] = s
    assert len(side) == stats["after_r51"]["rows"] == 4 + 81
    by_group = {}
    for m in jl(out / "meta.jsonl"):
        g = (m.get("gold") or {}).get("group")
        if g:
            by_group.setdefault(g, set()).add(side[m["id"]])
    assert all(len(v) == 1 for v in by_group.values())
    assert side["n-clash-s0"] == "valid"
    valid_groups = [g for g, v in by_group.items() if v == {"valid"}]
    assert 1 <= len(valid_groups) < 20
    valid_intents = {(m["gold"] or {}).get("intent") for m in jl(out / "valid.meta.jsonl") if m["gold"]}
    assert valid_intents == {"direct_send", "draft"}
    assert stats["after_r51"]["by_intent"]["direct_send"] == 40
    # deterministic
    again, _ = M.run(args(tmp_path, tmp_path / "new"))
    assert again["combined_sha256"] == stats["combined_sha256"]
