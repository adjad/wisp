"""Score a predictor against the user's labels.

A predictor decides, per item, whether Wisp would interrupt. The target is the
`missing` label. Every false alert is attributed to why it was wrong (already
known, not soon, promo, scam, nothing) because the cost of each is different:
the user named promo/scam as the failure to avoid, and "known" means Wisp
duplicated their own calendar.

The report holds counts and ids only, never message text, so it is safe to paste.
"""
from __future__ import annotations

from typing import Callable

from service.attention.corpus import Item, Snapshot, has_cue
from service.attention.labels import NEGATIVE, POSITIVE
from service.attention.prediction import Prediction

Predictor = Callable[[Item, Snapshot], Prediction]


def never(item: Item, snapshot: Snapshot) -> Prediction:
    return Prediction(False)


def cue_baseline(item: Item, snapshot: Snapshot) -> Prediction:
    """Alert on any incoming item with a time/place cue. The floor to beat."""
    return Prediction(item.direction == "incoming" and has_cue(item.text), "cue")


def _registry() -> dict[str, Predictor]:
    from service.attention.detectors import uncaptured_commitment
    return {"never": never, "cue_baseline": cue_baseline,
            "uncaptured_commitment": uncaptured_commitment}


PREDICTORS: dict[str, Predictor] = _registry()


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def evaluate(snapshot: Snapshot, sample: list[dict], labels: dict[str, dict],
             predictor: Predictor) -> dict:
    items = snapshot.by_id()
    tp = fp = fn = tn = 0
    fp_by_label: dict[str, int] = {}
    fp_ids: list[str] = []
    fn_ids: list[str] = []
    fn_blocked: dict[str, int] = {}          # which gate caused each missed commitment
    by_reason: dict[str, dict[str, int]] = {}
    strata: dict[str, dict[str, int]] = {}
    unlabelled = skipped = 0

    for entry in sample:
        item = items.get(entry["id"])
        rec = labels.get(entry["id"])
        if item is None:
            continue
        if rec is None:
            unlabelled += 1
            continue
        label = rec["label"]
        if label == "skip":
            skipped += 1
            continue
        pred = predictor(item, snapshot)
        positive = label == POSITIVE
        st = strata.setdefault(entry["stratum"], {"labelled": 0, "missing": 0})
        st["labelled"] += 1
        st["missing"] += positive
        if pred.alert:
            bucket = by_reason.setdefault(pred.reason or "unspecified", {"alerts": 0, "correct": 0})
            bucket["alerts"] += 1
            bucket["correct"] += positive
        if pred.alert and positive:
            tp += 1
        elif pred.alert:
            fp += 1
            fp_by_label[label] = fp_by_label.get(label, 0) + 1
            fp_ids.append(item.id)
        elif positive:
            fn += 1
            fn_ids.append(item.id)
            gate = pred.blocked_by or "unspecified"
            fn_blocked[gate] = fn_blocked.get(gate, 0) + 1
        else:
            tn += 1

    # How many real commitments does the sampling prefilter miss? The control
    # stratum is a random draw from everything the prefilter rejected, so its
    # `missing` rate scales to the whole rejected population.
    control = strata.get("control", {"labelled": 0, "missing": 0})
    rejected = sum(1 for i in items.values()
                   if i.source == "messages" and i.direction == "incoming" and not has_cue(i.text))
    miss_rate = _ratio(control["missing"], control["labelled"])
    return {
        "labelled": tp + fp + fn + tn, "skipped": skipped, "unlabelled": unlabelled,
        "positives": tp + fn,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": _ratio(tp, tp + fp), "recall": _ratio(tp, tp + fn),
        "false_alerts_by_label": {k: fp_by_label.get(k, 0) for k in NEGATIVE if fp_by_label.get(k)},
        "missed_by_gate": fn_blocked,
        "by_reason": by_reason, "strata": strata,
        "prefilter": {"control_missing_rate": miss_rate,
                      "estimated_missed": round(miss_rate * rejected, 1) if miss_rate is not None else None,
                      "rejected_population": rejected},
        "false_positive_ids": fp_ids, "false_negative_ids": fn_ids,
    }
