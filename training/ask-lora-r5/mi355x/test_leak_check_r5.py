"""leak_check_r5.py offline: synthetic indexes and rows only. A history-turn hit is caught, a whole turn goes, the results
converter has validate.private_denylist's shape, and the report holds no source text."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import leak_check_r5 as L  # noqa: E402
import seal_r5  # noqa: E402

PROBE = "Please summarize every unread email from the quarterly vendor review thread"
SECRET = "Zorblaxington"


def stub_index(prompt=PROBE, name="private"):
    import leak
    idx = seal_r5.Index.__new__(seal_r5.Index)
    idx.leak = leak
    idx.sets = {name: leak.LeakIndex([leak._entry("case:one", prompt)], toolbench=False)}
    idx.sizes = {name: 1, "private": 1, "audit": 1}
    idx.memo = {}
    return idx


def row(rid, users, split="train"):
    msgs = [{"role": "system", "content": "s"}]
    for u in users:
        msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": "ok"}]
    return split, {"messages": msgs}, {"id": rid, "family": "f"}


def test_history_turn_hit_is_caught_with_its_index():
    rows = [row("t-001-s1", [PROBE, "and what about tomorrow afternoon please"])]
    hits = L.check_overlap(rows, stub_index())
    assert hits["private"] == [["t-001-s1", 0, "case:one", 1.0]]


def test_short_history_turn_is_skipped_but_short_current_turn_counts():
    idx = stub_index("thanks a lot")
    assert not L.check_overlap([row("t-002-s0", ["thanks a lot", "something else entirely different here"])], idx)["private"]
    assert L.check_overlap([row("t-003-s0", ["thanks a lot"])], stub_index("thanks a lot"))["private"]


def test_near_duplicate_hits_by_threshold():
    near = PROBE.replace("quarterly", "quarterly's")
    assert L.check_overlap([row("t-004-s0", [near])], stub_index())["private"]


def test_whole_turn_is_removed_including_siblings_and_other_split():
    rows = [row("t-010-s0", ["unrelated question about the weather outside today"]),
            row("t-010-s1", [PROBE]),
            row("t-010-s2", ["unrelated question about the weather outside today"], split="valid"),
            row("t-011-s0", ["a perfectly fine question about gardening tips"])]
    hits = L.check_overlap(rows, stub_index())
    plan = L.removal_plan(rows, hits)
    assert plan["turns"] == 1 and plan["rows"] == 3
    assert plan["removed_turns"] == [{"turn": "t-010", "rows": ["t-010-s0", "t-010-s1", "t-010-s2"]}]


def test_results_row_shape_and_strings():
    r = {"final": "Done.", "message_draft": "Draft body", "error": None,
         "steps": [{"content": "step text", "tool_errors": ["bad"], "calls": [{"name": "t", "args": {"to": "a@b.co", "n": 3}}]}],
         "effects": [{"kind": "send", "to": "x@y.co"}], "asked": ["which one?"]}
    out = L.results_row(r)
    assert set(out) == {"text", "results"} and out["text"] == "Done."
    assert all(isinstance(x, str) for x in out["results"])
    assert {"Draft body", "step text", "bad", "a@b.co", "x@y.co", "which one?"} <= set(out["results"])


def test_denylist_catches_name_email_and_leaves_no_temp_file(tmp_path):
    results = [L.results_row({"final": f"Ask {SECRET} at {SECRET.lower()}@private-mail.test", "steps": []})]
    deny = L.load_denylist(results, tmp_path)
    assert SECRET in deny[0] and f"{SECRET.lower()}@private-mail.test" in deny[1]
    assert list(tmp_path.iterdir()) == []
    rows = [row("t-020-s0", ["hi"]), ("train", {"messages": [{"role": "assistant", "content": f"hello {SECRET}"}]}, {"id": "t-021-s0"})]
    hits = L.denylist_hits(rows, deny)
    assert [h["row"] for h in hits] == ["t-021-s0"] and hits[0]["hits"] == ["assistant:real_name"]


def test_report_holds_ids_and_counts_only(tmp_path, monkeypatch):
    build = tmp_path / "b"
    build.mkdir()
    rows = [row("t-030-s0", [PROBE]), row("t-031-s0", ["a perfectly fine question about gardening tips"], "valid")]
    for name, split in (("train", "train"), ("valid", "valid")):
        mine = [r for r in rows if r[0] == split]
        (build / f"{name}.jsonl").write_text("".join(json.dumps(r[1]) + "\n" for r in mine))
        (build / f"{name}.meta.jsonl").write_text("".join(json.dumps(r[2]) + "\n" for r in mine))
    runs = tmp_path / "pd" / "runs" / "r1"
    runs.mkdir(parents=True)
    (runs / "results.jsonl").write_text(json.dumps({"final": f"Call {SECRET}", "steps": []}) + "\n")
    monkeypatch.setattr(L, "build_index", lambda p, a: stub_index())
    monkeypatch.setattr(L, "find_audit_dir", lambda: None)
    monkeypatch.setattr(L, "synthetic_world_text", lambda: "")
    out = tmp_path / "out"
    rc = L.main(["--build", str(build), "--out", str(out), "--private-dir", str(tmp_path / "pd")])
    assert rc == 1
    text = (out / "leak_report.json").read_text()
    rep = json.loads(text)
    assert rep["sources"]["private"]["hits"] == 1 and rep["would_remove"]["turns"] == 1
    assert rep["denylist"]["names"] >= 1
    for secret in (PROBE, "quarterly vendor", SECRET, "gardening"):
        assert secret not in text
    assert [p.name for p in out.iterdir()] == ["leak_report.json"]


def test_no_results_is_incomplete_not_a_pass(tmp_path, monkeypatch):
    build = tmp_path / "b"
    build.mkdir()
    for n in ("train", "valid"):
        (build / f"{n}.jsonl").write_text(json.dumps(row("t-040-s0", ["fine question about gardening tips"])[1]) + "\n")
        (build / f"{n}.meta.jsonl").write_text(json.dumps({"id": f"t-040-s0{n}", "family": "f"}) + "\n")
    monkeypatch.setattr(L, "build_index", lambda p, a: stub_index("something wholly unrelated to the rows"))
    monkeypatch.setattr(L, "find_audit_dir", lambda: None)
    rep = L.run(build, tmp_path / "o", str(tmp_path / "none"), None, None)
    assert not rep["complete"] and any("denylist" in x for x in rep["incomplete"])


def _deny_build(tmp_path, word, monkeypatch, synthetic):
    build = tmp_path / "b"
    build.mkdir()
    r = row("t-040-s0", [f"tell {word} the plan please"])
    (build / "train.jsonl").write_text(json.dumps(r[1]) + "\n")
    (build / "train.meta.jsonl").write_text(json.dumps(r[2]) + "\n")
    (build / "valid.jsonl").write_text("")
    (build / "valid.meta.jsonl").write_text("")
    runs = tmp_path / "pd" / "runs" / "r1"
    runs.mkdir(parents=True)
    (runs / "results.jsonl").write_text(json.dumps({"final": f"Call {word}", "steps": []}) + "\n")
    monkeypatch.setattr(L, "build_index", lambda p, a: stub_index())
    monkeypatch.setattr(L, "find_audit_dir", lambda: None)
    monkeypatch.setattr(L, "check_overlap", lambda rows, index: {})
    monkeypatch.setattr(L, "synthetic_world_text", lambda: synthetic)
    return L.main(["--build", str(build), "--out", str(tmp_path / "out"), "--private-dir", str(tmp_path / "pd")])


def test_a_name_only_found_in_synthetic_sources_exits_4_and_is_still_counted(tmp_path, monkeypatch):
    assert _deny_build(tmp_path, "Zorbalina", monkeypatch, "a world with zorbalina in it") == 4
    rep = json.loads((tmp_path / "out" / "leak_report.json").read_text())["denylist"]
    assert rep["rows_hit"] == 1 and rep["rows_hit_only_synthetic_world_names"] == 1


def test_a_synthetic_match_must_be_a_whole_word(tmp_path, monkeypatch):
    assert _deny_build(tmp_path, "Zorba", monkeypatch, "a world of zorbalina") == 1
