from __future__ import annotations

import importlib.util
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("laya_wisp_benchmark", HERE / "benchmark.py")
BENCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(BENCH)


def test_fixture_protocol_is_valid_and_broad() -> None:
    result = BENCH.validate_fixtures()
    assert result["cases"] == 70
    assert result["tag_counts"]["multilingual"] >= 10
    assert result["tag_counts"]["safety"] >= 10
    assert result["tag_counts"]["over_capacity"] == 3


def test_recorded_download_upper_bound_is_below_ceiling() -> None:
    limits = BENCH.PREFLIGHT["limits"]
    assert limits["planned_download_bytes"] == sum(
        model["expected_repository_bytes"] for model in BENCH.PREFLIGHT["models"]
    )
    assert limits["planned_download_bytes"] < limits["download_ceiling_bytes"]


def test_context_is_explicit_and_current_request_is_last() -> None:
    case = next(case for case in BENCH.FIXTURES["cases"] if case["id"] == "CX-001")
    state = BENCH.state_for_model(case)
    assert "Previous assistant:" in state
    assert "Previous tools: add_reminder" in state
    assert state.endswith("Current user: Today instead.")


def test_compound_label_does_not_claim_candidate_retrieval() -> None:
    case = next(case for case in BENCH.FIXTURES["cases"] if case["id"] == "CP-001")
    result = BENCH.candidate_assessment(
        {"domain": "multi", "operation": "compound"}, case,
        {"summarize_emails": "email_read", "summarize_messages": "messages_read"},
    )
    assert result["pass"] is False
    assert result["candidate_count"] == 0


def test_binary_auroc_handles_order_and_ties() -> None:
    assert BENCH.binary_auroc([0.8, 0.9], [0.1, 0.2]) == 1.0
    assert BENCH.binary_auroc([0.5], [0.5]) == 0.5
