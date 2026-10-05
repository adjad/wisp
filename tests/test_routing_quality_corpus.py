"""CI gate: the routing-quality corpus may only get better.

Runs the real router over test_fixtures/routing/quality_corpus.json (361
synthetic prompts; see docs/ROUTING_DIAGNOSIS.md) with the same scorer as
scripts/score_routing_quality.py. Every case id recorded in
quality_ratchet.json must still pass. A change that fixes more cases should
refresh the ratchet with `--write-ratchet`; a change must never remove an id
to pass. No model, embedding server, network, tool body or user data.
"""
import asyncio
import json
from pathlib import Path

from scripts.score_routing_quality import DEFAULT_CORPUS, RATCHET, run

FLOOR = 296  # passing cases at the commit that introduced this gate


def test_routing_quality_corpus_never_regresses():
    cases = json.loads(Path(DEFAULT_CORPUS).read_text())["cases"]
    assert len(cases) >= 300
    rows = {r["id"]: r for r in asyncio.run(run(cases))}
    required = json.loads(Path(RATCHET).read_text())["passing"]
    broken = [f"{i}: {rows[i]['prompt']!r} -> {rows[i]['classes']} ({rows[i]['reason'][:80]})"
              for i in required if not rows[i]["passed"]]
    assert not broken, "routing-quality regressions:\n" + "\n".join(broken)
    assert sum(r["passed"] for r in rows.values()) >= FLOOR


def test_corpus_ids_are_unique_and_expectations_well_formed():
    cases = json.loads(Path(DEFAULT_CORPUS).read_text())["cases"]
    assert len({c["id"] for c in cases}) == len(cases)
    for case in cases:
        assert case["expect"] in {"action", "chat", "either", "clarify"}
        assert all(isinstance(group, list) and group for group in case["need"])
        assert case["force"] is None or isinstance(case["force"], list)
