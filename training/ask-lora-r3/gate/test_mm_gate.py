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
    print("ok test_mm_gate")


main()
