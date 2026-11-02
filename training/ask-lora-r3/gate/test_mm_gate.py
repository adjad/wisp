"""Mac test for mm_gate.judge on synthetic score JSON:  python test_mm_gate.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mm_gate as G  # noqa: E402

V = {"summary": {
    "A": {"fact_recall": 0.9, "document_fact_recall": 0.8, "proposal_ok": "4/6", "obeyed_injection": "0/8", "empty_replies": [], "spurious_proposals": [], "hallucinated_cases": 1},
    "same": {"fact_recall": 0.9, "document_fact_recall": 0.8, "proposal_ok": "4/6", "obeyed_injection": "0/8", "empty_replies": [], "spurious_proposals": [], "hallucinated_cases": 1},
    "r2like": {"fact_recall": 0.72, "document_fact_recall": 0.5, "proposal_ok": "0/6", "obeyed_injection": "0/8", "empty_replies": ["x", "y"], "spurious_proposals": [], "hallucinated_cases": 1}}}
A = {"A": {"wer_norm_mean": 0.12, "wer_raw_mean": 0.2, "cer_mean": 0.05, "capitalised_and_punctuated": "20/22", "no_speech_empty": "2/2", "injections_obeyed": "0/3"},
     "same": {"wer_norm_mean": 0.12, "wer_raw_mean": 0.2, "cer_mean": 0.05, "capitalised_and_punctuated": "20/22", "no_speech_empty": "2/2", "injections_obeyed": "0/3"},
     "r2like": {"wer_norm_mean": 0.12, "wer_raw_mean": 0.2, "cer_mean": 0.05, "capitalised_and_punctuated": "15/22", "no_speech_empty": "2/2", "injections_obeyed": "0/3"}}


RD = {"summary": {
    "A": {"format_ok": 0.98, "text_recall": 0.9, "empty_replies": [], "obeyed_injection": "0/8", "answered_rows": 0, "hallucinated_cases": 1},
    "same": {"format_ok": 0.98, "text_recall": 0.9, "empty_replies": [], "obeyed_injection": "0/8", "answered_rows": 0, "hallucinated_cases": 1},
    "bad": {"format_ok": 0.9, "text_recall": 0.85, "empty_replies": ["x"], "obeyed_injection": "1/8", "answered_rows": 2, "hallucinated_cases": 1}}}
PT = {"summary": {
    "A": {"call_ok": 0.8, "obeyed_injection": "0/20", "reread": 0, "empty_replies": []},
    "same": {"call_ok": 0.8, "obeyed_injection": "0/20", "reread": 0, "empty_replies": []},
    "bad": {"call_ok": 0.7, "obeyed_injection": "1/20", "reread": 2, "empty_replies": ["q"]}}}


def test_reading_group():
    rows, ok = G.judge(V, A, "A", "same", reading=RD)
    assert ok and len(rows) == 13 + 6 and {r["group"] for r in rows} == {"vision", "audio", "reading"}
    rows, ok = G.judge(V, A, "A", "bad", reading=RD)
    assert {r["metric"] for r in rows if r["group"] == "reading" and not r["ok"]} == {
        "format_ok", "text_recall", "empty_replies", "obeyed_injection", "answered_rows"}
    # a reading summary without the model, or with a renamed key, fails; not passing --reading judges as before
    assert not G.judge(V, A, "A", "same", reading={"summary": {"A": RD["summary"]["A"]}})[1]
    renamed = {"summary": {m: {("fmt" if k == "format_ok" else k): v for k, v in d.items()} for m, d in RD["summary"].items()}}
    assert not G.judge(V, A, "A", "same", reading=renamed)[1]
    assert G.judge(V, A, "A", "same", optional=["format_ok"], reading=renamed)[1]


def test_picture_turn_group():
    rows, ok = G.judge(V, A, "A", "same", picture_turns=PT)
    assert ok and len(rows) == 13 + 4
    rows, ok = G.judge(V, A, "A", "bad", picture_turns=PT)
    assert {r["metric"] for r in rows if r["group"] == "pturn" and not r["ok"]} == {"call_ok", "obeyed_injection", "reread", "empty_replies"}


def test_reading_scoring_flow():
    """run_reading.py's own pipeline offline: every gate case read through service.vision.read_picture by a canned
    client, scored like the live run; then a model that offers to do what the picture says is caught."""
    import json
    import os
    import subprocess
    import tempfile
    repo = Path(__file__).resolve().parents[3]
    rr = Path(__file__).resolve().parent / "vision" / "run_reading.py"
    cases = Path(__file__).resolve().parent / "vision" / "cases.json"
    if not cases.is_file():
        print("skip reading flow: gate/vision/cases.json not built")
        return
    env = dict(os.environ, MOE_DIR=tempfile.mkdtemp(prefix="gate-reading-"))
    r = subprocess.run([sys.executable, str(rr), "run", "--repo", str(repo), "--dry-run"], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "request shape OK" in r.stdout, (r.stdout, r.stderr[-800:])
    sys.path.insert(0, str(rr.parent))
    import run_reading as RR
    cs = RR.reading_cases()
    assert len(cs) == 60 and all(not c.get("pages") for c in cs)
    recs = {c["id"]: {"id": c["id"], "reading": RR.perfect_reply(c), "raw": RR.perfect_reply(c), "secs": 1.0} for c in cs}
    byid = {c["id"]: c for c in cs}
    good = RR.summarize(RR.score_rows(byid, recs), recs)
    assert good["format_ok"] == 1.0 and good["text_recall"] == 1.0 and good["empty_replies"] == [] and good["answered_rows"] == 0
    assert good["obeyed_injection"].startswith("0/") and good["hallucinated_cases"] == 0, good
    inj = next(c for c in cs if c.get("injection") and c["injection"].get("obeyed_if"))
    bad = dict(recs)
    bad[inj["id"]] = {**recs[inj["id"]], "reading": "TEXT:\n" + inj["injection"]["obeyed_if"][0] + "\nDESCRIPTION:\nSure, I'll do that for you.",
                      "raw": "x"}
    victim = cs[0]["id"] if cs[0]["id"] != inj["id"] else cs[1]["id"]
    bad[victim] = {**recs[victim], "reading": "TEXT:\n(none)\nDESCRIPTION:\nNothing readable could be made out.", "raw": ""}   # fallback hides an empty answer
    bs = RR.summarize(RR.score_rows(byid, bad), bad)
    assert bs["obeyed_injection"].split("/")[0] == "1" and bs["empty_replies"] == [victim] and bs["answered_rows"] == 1, bs
    assert bs["text_recall"] < good["text_recall"]
    st = subprocess.run([sys.executable, str(rr.parent / "picture_turns.py"), "selftest"], capture_output=True, text=True)
    assert st.returncode == 0 and "ok picture_turns" in st.stdout, st.stderr[-500:]


def main():
    rows, ok = G.judge(V, A, "A", "same")
    assert ok and len(rows) == 13, rows
    rows, ok = G.judge(V, A, "A", "r2like")
    bad = {(r["group"], r["metric"]) for r in rows if not r["ok"]}
    assert not ok and bad == {("vision", "fact_recall"), ("vision", "document_fact_recall"), ("vision", "proposal_ok"), ("vision", "empty_replies"),
                              ("audio", "capitalised_and_punctuated")}, bad
    rows, ok = G.judge(V, A, "A", "missing")
    assert not ok
    # a metric key absent from B (e.g. renamed by run_audio.py) fails; --optional waives it
    import copy
    A2 = copy.deepcopy(A)
    del A2["same"]["wer_norm_mean"]
    rows, ok = G.judge(V, A2, "A", "same")
    assert not ok and [(r["metric"], r["why"]) for r in rows if not r["ok"]] == [("wer_norm_mean", "metric missing from the results")]
    rows, ok = G.judge(V, A2, "A", "same", optional=["wer_norm_mean"])
    assert ok
    V2 = copy.deepcopy(V)
    V2["summary"]["same"]["fact_recall"] = None
    assert not G.judge(V2, A, "A", "same")[1]
    test_reading_group()
    test_picture_turn_group()
    test_reading_scoring_flow()
    print("ok test_mm_gate")


main()
