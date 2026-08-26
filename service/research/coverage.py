"""Deterministic coverage scoring and contradiction detection.

Both run over evidence records already validated by the extraction step
(exact quote, known subquestion, known source) — this module never talks to
the model. The workflow diagram's "coverage/contradiction matrix" box is
entirely host-side arithmetic; the model only ever supplied the underlying
evidence and, later, prose that cites it.
"""
from __future__ import annotations

from collections import defaultdict

from service.research.textutil import numbers, terms

_STRONG_CLASSES = {"primary", "official", "academic"}
_MAX_PAIRS_PER_QUESTION = 60


def coverage(evidence_rows: list[dict], plan: dict) -> dict[str, dict]:
    """Per-subquestion independent-source coverage.

    A subquestion is "met" once it has evidence from at least two distinct
    domains, or one source classified primary/official/academic — mirroring
    the plan's stopping rule, where independence is inapplicable to a single
    authoritative source.
    """
    by_q: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"domains": set(), "strong": set()})
    for row in evidence_rows:
        if row.get("stance") == "contradicts":
            continue
        bucket = by_q[row["subquestion_id"]]
        bucket["domains"].add(row.get("domain") or row["source_id"])
        if row.get("quality_class") in _STRONG_CLASSES:
            bucket["strong"].add(row["source_id"])
    out: dict[str, dict] = {}
    for question in plan.get("subquestions", []):
        qid = question["id"]
        bucket = by_q.get(qid, {"domains": set(), "strong": set()})
        met = len(bucket["domains"]) >= 2 or bool(bucket["strong"])
        out[qid] = {"independent_sources": len(bucket["domains"]), "met": met}
    return out


def uncovered(plan: dict, coverage_map: dict[str, dict]) -> list[dict]:
    return [q for q in plan.get("subquestions", [])
            if not coverage_map.get(q["id"], {}).get("met")]


def detect_contradictions(evidence_rows: list[dict]) -> list[dict]:
    """Flag likely conflicts: opposing stance, or disjoint numbers over
    otherwise-overlapping claims, between two different sources on the same
    subquestion. Heuristic and conservative — it under-flags rather than
    invents disagreement that isn't there.
    """
    by_q: dict[str, list[dict]] = defaultdict(list)
    for row in evidence_rows:
        by_q[row["subquestion_id"]].append(row)
    found: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for qid, rows in by_q.items():
        pairs = 0
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                if a["source_id"] == b["source_id"]:
                    continue
                pairs += 1
                if pairs > _MAX_PAIRS_PER_QUESTION:
                    break
                key = tuple(sorted((a["evidence_id"], b["evidence_id"])))
                if key in seen_pairs:
                    continue
                conflict = ""
                if {a.get("stance"), b.get("stance")} == {"supports", "contradicts"}:
                    conflict = "opposing stance on the same question"
                else:
                    nums_a, nums_b = numbers(a["quote"]), numbers(b["quote"])
                    overlap_terms = terms(a["quote"]) & terms(b["quote"])
                    if nums_a and nums_b and not (nums_a & nums_b) and len(overlap_terms) >= 4:
                        conflict = f"different figures ({', '.join(sorted(nums_a))} vs {', '.join(sorted(nums_b))}) for what appears to be the same claim"
                if conflict:
                    seen_pairs.add(key)
                    found.append({"subquestion_id": qid, "evidence_id_a": a["evidence_id"],
                                 "evidence_id_b": b["evidence_id"], "description": conflict})
    return found


def stopping_reason(*, coverage_map: dict[str, dict], stale_rounds: int,
                    queries_issued: int, max_queries: int,
                    sources_used: int, max_sources: int,
                    model_calls: int, max_model_calls: int) -> str:
    if all(v["met"] for v in coverage_map.values()):
        return "coverage_met"
    if stale_rounds >= 2:
        return "no_new_evidence"
    if queries_issued >= max_queries or sources_used >= max_sources or model_calls >= max_model_calls:
        return "budget_exhausted"
    return ""
