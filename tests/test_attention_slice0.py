"""Slice 0 of proactive attention: snapshot, sample, labels, scoring.

Synthetic fixtures only (test_fixtures/attention). The real caches and the real
assistant.db are never read: freeze_snapshot is pointed at a temp "moe dir".
"""
import json
import shutil
import sqlite3
import stat
from pathlib import Path

import pytest

from service.attention import corpus, evaluate, labels

FIXTURES = Path(__file__).resolve().parents[1] / "test_fixtures" / "attention"
T = 1790000000.0


@pytest.fixture
def moe_dir(tmp_path):
    root = tmp_path / "moe"
    (root / "cache").mkdir(parents=True)
    for name in ("messages.txt", "email_headers.txt"):
        shutil.copy(FIXTURES / name, root / "cache" / name)
    db = sqlite3.connect(root / "assistant.db")
    db.execute("CREATE TABLE commitments (id TEXT, source TEXT, kind TEXT, title TEXT, "
               "when_ts REAL, status TEXT, location TEXT)")
    for c in json.loads((FIXTURES / "commitments.json").read_text()):
        db.execute("INSERT INTO commitments VALUES (?,?,?,?,?,?,?)",
                   (c["id"], c["source"], c["kind"], c["title"], c["when_ts"], c["status"], c["location"]))
    db.commit()
    db.close()
    return root


@pytest.fixture
def snap(moe_dir, tmp_path):
    out = tmp_path / "attention" / "snapshots" / "s1"
    corpus.freeze_snapshot(moe_dir, out, now=T)
    return corpus.load_snapshot(out)


def text_of(snap, item_id):
    return snap.by_id()[item_id].text


# ------------------------------------------------------------------ parsing

def test_messages_parse_skips_malformed_and_attachment_only():
    items, meta = corpus.parse_messages((FIXTURES / "messages.txt").read_text())
    assert [i.id for i in items] == [f"msg:g{n}" for n in (1, 2, 3, 4, 5, 6, 7, 8)]
    assert meta["malformed"] == 2                      # bad JSON and a record with no text
    assert meta["coverage"]["window_days"] == 365
    assert {i.direction for i in items} == {"incoming", "outgoing"}


def test_mail_parse_collapses_identical_duplicate_headers():
    items = corpus.parse_mail((FIXTURES / "email_headers.txt").read_text())
    assert len(items) == 3
    assert all(i.source == "mail" and i.direction == "incoming" for i in items)
    assert len({i.id for i in items}) == 3


def test_mail_parser_matches_email_tools_on_both_header_formats():
    """The local parser exists so attention never imports email_tools (see _mail_rows)."""
    from service.tools.email_tools import _parse_header_records

    h2 = "\x01".join(["H2", f"{T + 50}", "U", "Home", "acct-1", "Registrar", "Reg@School.example",
                       "<mid-1@school.example>", "Quiz | Friday at 3pm"])
    h2_bad = "\x01".join(["H2", "notatime", "U", "Home", "a", "n", "a@b.example", "<m>", "s"])
    pipe_old = f"{T + 60} | Home | Old Sender <old@x.example> | Old format subject"
    pipe_flag_in_subject = f"{T + 70} | R | Home | Pat <pat@x.example> | a | b | c"
    text = "\n".join([(FIXTURES / "email_headers.txt").read_text(), h2, h2, h2_bad,
                      pipe_old, pipe_flag_in_subject, "garbage line"])
    mine = sorted((i.ts, i.sender, i.text) for i in corpus.parse_mail(text))
    theirs = sorted((r["ts"], r["sender"], r["subject"])
                    for r in _parse_header_records(text, snapshot="t"))
    assert mine == theirs
    by_subject = {i.text: i for i in corpus.parse_mail(text)}
    assert by_subject["Quiz | Friday at 3pm"].conversation == "reg@school.example"
    assert by_subject["a | b | c"].text == "a | b | c"


def test_attention_never_imports_the_live_assistant_store():
    """Importing service.assistant builds AssistantStore() on ~/.moe/assistant.db."""
    import subprocess
    import sys

    code = ("import sys; from pathlib import Path\n"
            "from service.attention import corpus, evaluate, labels\n"
            f"msgs, _ = corpus.parse_messages(Path({str(FIXTURES / 'messages.txt')!r}).read_text())\n"
            f"mail = corpus.parse_mail(Path({str(FIXTURES / 'email_headers.txt')!r}).read_text())\n"
            "assert msgs and mail\n"
            "bad = [m for m in sys.modules if m.startswith(('service.assistant', 'service.tools'))]\n"
            "assert not bad, bad\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=corpus.REPO_ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_cue_prefilter_is_loose_but_not_everything():
    assert corpus.has_cue("Meet me in the Quad at 6PM")
    assert corpus.has_cue("dinner tmrw?") and corpus.has_cue("see you Oct 12")
    assert corpus.has_cue("it's due 10/14")
    assert not corpus.has_cue("lol") and not corpus.has_cue("I may go")
    assert not corpus.has_cue("")


# ---------------------------------------------------------------- snapshots

def test_freeze_roundtrip_counts_and_privacy(snap):
    assert snap.manifest["counts"] == {"messages": 8, "mail": 3, "commitments": 3}
    for path in snap.root.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(snap.root.stat().st_mode) == 0o700
    assert [i.ts for i in snap.items] == sorted(i.ts for i in snap.items)


def test_private_tree_is_0700_all_the_way_down(tmp_path):
    base = corpus.ensure_private_tree(tmp_path / "moe")
    for path in (base, base / "snapshots"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_freeze_never_modifies_sources(moe_dir, tmp_path):
    before = {p: p.read_bytes() for p in moe_dir.rglob("*") if p.is_file()}
    corpus.freeze_snapshot(moe_dir, tmp_path / "snaps" / "x")
    assert {p: p.read_bytes() for p in moe_dir.rglob("*") if p.is_file()} == before


def test_freeze_refuses_the_repository_and_nonempty_targets(moe_dir, tmp_path):
    with pytest.raises(ValueError, match="repository"):
        corpus.freeze_snapshot(moe_dir, corpus.REPO_ROOT / "attention-snap")
    out = tmp_path / "taken"
    out.mkdir()
    (out / "file").write_text("x")
    with pytest.raises(FileExistsError):
        corpus.freeze_snapshot(moe_dir, out)


def test_load_detects_a_tampered_snapshot(snap):
    (snap.root / "messages.txt").write_text("")
    with pytest.raises(ValueError, match="changed since it was frozen"):
        corpus.load_snapshot(snap.root)


def test_latest_snapshot_picks_newest_and_errors_when_none(moe_dir):
    with pytest.raises(FileNotFoundError):
        corpus.latest_snapshot(moe_dir)
    base = moe_dir / "attention" / "snapshots"
    corpus.freeze_snapshot(moe_dir, base / "20261001T000000")
    corpus.freeze_snapshot(moe_dir, base / "20261002T000000")
    assert corpus.latest_snapshot(moe_dir).name == "20261002T000000"


def test_thread_and_commitment_context(snap):
    g3 = snap.by_id()["msg:g3"]
    assert [i.id for i in snap.thread_before(g3)] == ["msg:g1", "msg:g2"]
    g8 = snap.by_id()["msg:g8"]
    near = snap.commitments_after(g8)
    assert [c["title"] for c in near] == ["Call dentist", "Chem exam"]   # the trip is >48h out
    assert snap.commitments_after(snap.by_id()["msg:g1"], hours=1) == []


# ----------------------------------------------------------------- sampling

def test_sample_strata_exclude_outgoing_and_are_deterministic(snap):
    a = corpus.sample(snap, seed=1, n_cue=10, n_control=10, n_mail=10)
    b = corpus.sample(snap, seed=1, n_cue=10, n_control=10, n_mail=10)
    assert a == b
    ids = {e["id"]: e["stratum"] for e in a}
    assert "msg:g2" not in ids                                  # outgoing is context only
    assert {k for k, v in ids.items() if v == "cue"} == {"msg:g1", "msg:g4", "msg:g5", "msg:g8"}
    assert {k for k, v in ids.items() if v == "control"} == {"msg:g3", "msg:g6", "msg:g7"}
    assert sum(v == "mail" for v in ids.values()) == 2          # advising + flash sale; digest has no cue
    assert corpus.sample(snap, seed=2, n_cue=10, n_control=10, n_mail=10) != a


def test_sample_caps_and_per_conversation_limit(snap):
    s = corpus.sample(snap, seed=0, n_cue=2, n_control=1, n_mail=0)
    assert [e["stratum"] for e in s].count("cue") == 2
    assert [e["stratum"] for e in s].count("control") == 1
    capped = corpus.sample(snap, seed=0, n_cue=10, n_control=10, n_mail=0, per_conversation=1)
    cue = [snap.by_id()[e["id"]].conversation for e in capped if e["stratum"] == "cue"]
    assert len(cue) == len(set(cue)) == 4                  # Mom, Alex, SHOP, Prof
    # The control stratum is an unbiased draw and ignores the cap.
    assert sum(e["stratum"] == "control" for e in capped) == 3


# ------------------------------------------------------------------- labels

def test_label_store_last_write_wins_and_is_private(tmp_path):
    store = labels.LabelStore(tmp_path / "attn" / "labels.jsonl")
    store.append("msg:g1", "none")
    store.append("msg:g1", "missing", "Quad 6pm")
    store.append("msg:g5", "promo")
    cur = store.current()
    assert cur["msg:g1"]["label"] == "missing" and cur["msg:g1"]["note"] == "Quad 6pm"
    assert len(store.path.read_text().strip().split("\n")) == 3     # history kept
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    with pytest.raises(ValueError):
        store.append("msg:g1", "maybe")


def test_label_store_ignores_a_torn_trailing_line(tmp_path):
    store = labels.LabelStore(tmp_path / "labels.jsonl")
    store.append("a", "none")
    with open(store.path, "a") as f:
        f.write('{"id": "b", "lab')
    assert set(store.current()) == {"a"}


def test_every_key_maps_to_a_known_label():
    assert set(labels.KEYS.values()) == set(labels.LABELS)


# --------------------------------------------------------------- evaluation

def _labelled(snap):
    sample = [{"id": f"msg:g{n}", "stratum": s} for n, s in
              ((1, "cue"), (4, "cue"), (5, "cue"), (8, "cue"), (3, "control"), (6, "control"), (7, "control"))]
    marks = {"msg:g1": "missing", "msg:g4": "missing", "msg:g5": "promo", "msg:g8": "known",
             "msg:g3": "none", "msg:g6": "scam", "msg:g7": "none"}
    return sample, {k: {"label": v} for k, v in marks.items()}


def test_cue_baseline_scores_and_attributes_false_alerts(snap):
    sample, marks = _labelled(snap)
    r = evaluate.evaluate(snap, sample, marks, evaluate.cue_baseline)
    assert (r["tp"], r["fp"], r["fn"], r["tn"]) == (2, 2, 0, 3)
    assert r["precision"] == 0.5 and r["recall"] == 1.0
    assert r["false_alerts_by_label"] == {"known": 1, "promo": 1}
    assert sorted(r["false_positive_ids"]) == ["msg:g5", "msg:g8"]
    assert r["prefilter"]["rejected_population"] == 3
    assert r["prefilter"]["estimated_missed"] == 0.0


def test_never_predictor_has_zero_recall_and_prefilter_miss_estimate(snap):
    sample, marks = _labelled(snap)
    marks["msg:g3"] = {"label": "missing"}                 # a real commitment the cue missed
    r = evaluate.evaluate(snap, sample, marks, evaluate.never)
    assert (r["tp"], r["fp"], r["fn"]) == (0, 0, 3)
    assert r["recall"] == 0.0 and r["precision"] is None
    assert r["prefilter"]["control_missing_rate"] == pytest.approx(0.333, abs=1e-3)
    assert r["prefilter"]["estimated_missed"] == pytest.approx(1.0)


def test_skipped_and_unlabelled_items_do_not_count(snap):
    sample, marks = _labelled(snap)
    marks["msg:g4"] = {"label": "skip"}
    del marks["msg:g8"]
    r = evaluate.evaluate(snap, sample, marks, evaluate.cue_baseline)
    assert r["skipped"] == 1 and r["unlabelled"] == 1
    assert r["labelled"] == 5
