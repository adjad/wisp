"""The picker's proposal bars: recall >= base, precision >= base, spurious (made minus right) <= base.

F1 alone passes a checkpoint that drops from 6 right to 5 right while making far fewer proposals; these tests
pin that case (and its mirror, more proposals right but more spurious) so the hard r4 bars stay enforced.
Run: python -m pytest training/ask-lora-r3/mmeval -q
"""
import copy
import json
import subprocess
import sys
from pathlib import Path

MM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MM))

import pick_checkpoint as P   # noqa: E402
import scoring as S           # noqa: E402

PY = sys.executable


def vision(tp, pred, gold=11):
    """Vision metrics with everything else at a passing value; F1 = 2tp/(pred+gold) as scoring computes it."""
    prec, rec = tp / pred, tp / gold
    return {"n": 40, "fact_recall": 0.8, "proposal_precision": round(prec, 4), "proposal_recall": round(rec, 4),
            "proposal_f1": round(2 * prec * rec / (prec + rec), 4), "proposal_validity": 1.0, "abstain_ok": 1.0,
            "empty_replies": 0, "proposal_pred": pred, "proposal_tp": tp, "proposal_gold": gold,
            "leak_ids": [], "injection_leaks": 0}


def judge(base_v, ckpt_v, tol=None):
    base, ckpt = {"vision": base_v}, {"vision": ckpt_v}
    return P.with_proposal_bars(S.compare(base, ckpt, tol), base, ckpt, tol)


def test_r3_base_numbers_are_the_bar():
    base = vision(tp=6, pred=13)                 # 6 of 11 right, 7 spurious: the untuned base in the r3 gate
    assert base["proposal_pred"] - base["proposal_tp"] == 7
    v = judge(base, copy.deepcopy(base))
    assert v["ok"] and not v["failed"]
    assert {c["check"]: c["status"] for c in v["checks"]}["vision_spurious_proposals"] == "ok"


def test_precision_heavy_checkpoint_that_loses_recall_fails():
    base = vision(tp=6, pred=13)                 # F1 0.50
    ckpt = vision(tp=5, pred=5)                  # F1 0.625: passes the F1 check, but 5 right < 6 right
    plain = S.compare({"vision": base}, {"vision": ckpt})
    assert plain["ok"], plain["failed"]          # the old picker accepted it
    v = judge(base, ckpt)
    assert not v["ok"] and v["failed"] == ["vision_proposal_recall"], v["failed"]


def test_more_right_but_more_spurious_fails():
    base = vision(tp=6, pred=13)                 # 7 spurious
    ckpt = vision(tp=8, pred=17)                 # F1 0.571, recall up, 9 spurious
    assert S.compare({"vision": base}, {"vision": ckpt})["ok"]
    v = judge(base, ckpt)
    assert not v["ok"] and "vision_spurious_proposals" in v["failed"] and "vision_proposal_recall" not in v["failed"]


def test_better_on_every_bar_passes_and_precision_has_its_own_check():
    base = vision(tp=6, pred=13)
    ckpt = vision(tp=7, pred=9)                  # 7 right, 2 spurious
    v = judge(base, ckpt)
    assert v["ok"], v["failed"]
    low_prec = vision(tp=6, pred=13)
    low_prec["proposal_precision"] = 0.40        # recall and spurious equal, precision below base 0.4615
    assert judge(base, low_prec)["failed"] == ["vision_proposal_precision"]


def test_missing_metrics_skip_and_tolerance_override():
    base = {"vision": {"n": 3, "fact_recall": 0.8}}          # no proposal rows
    v = P.with_proposal_bars({"checks": [], "failed": [], "skipped": [], "no_data": [], "ok": True}, base, base)
    assert v["ok"] and set(v["skipped"]) == set(P.PROPOSAL_TOL) and set(v["no_data"]) == set(P.PROPOSAL_TOL)
    b, c = vision(6, 13), vision(5, 5)
    assert judge(b, c, {"vision_proposal_recall": 0.1})["ok"]


def _results(ckpt_vision, base_vision):
    def entry(vis, loss):
        m = {"vision": vis}
        return {"metrics": m, "verdict": S.compare({"vision": base_vision}, m), "dev_loss_text": loss, "step": 1}
    return {"base": {"vision": base_vision}, "checkpoints": {"regressed": entry(ckpt_vision, 0.5),
                                                              "good": entry(vision(7, 9), 0.9)}}


def test_pick_skips_the_low_loss_checkpoint_that_loses_proposals_right(tmp_path):
    base = vision(6, 13)
    res = _results(vision(5, 5), base)
    chosen, table = P.pick(res)
    by = {t["name"]: t for t in table}
    assert chosen == "good" and not by["regressed"]["ok"] and "vision_proposal_recall" in by["regressed"]["failed"]
    path = tmp_path / "results.json"
    path.write_text(json.dumps(res))
    r = subprocess.run([PY, "-I", str(MM / "pick_checkpoint.py"), "--results", str(path), "--json",
                        "--tol", "vision_proposal_recall=0.1"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["chosen"] == "regressed"      # the override re-judges with the looser recall tolerance
