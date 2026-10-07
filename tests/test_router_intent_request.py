"""Offline request alignment and future final-answer supervision contracts."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from service.router.intent.request import (
    SYSTEM, MAX_OUTPUT_TOKENS, build_messages, completion_options,
    natural_context, repair_message, training_messages,
)
from service.router.intent.schema import SCHEMA

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def test_frozen_core_and_schema_remain_unchanged():
    assert hashlib.sha256(SYSTEM.encode()).hexdigest() == "800b235e8ab3ed7d76e74461cd20dbd0d58cb111e3d8f31b5adba27117b1b830"
    assert SCHEMA == json.loads((ROOT / "service/router/intent/intent.schema.v1.json").read_text())


def test_builder_keeps_literal_latest_request_and_bounded_natural_history():
    history = [{"role": "user", "content": "old"}] * 7 + [
        {"role": "tool", "content": "not context"},
        {"role": "assistant", "content": "Earlier calendar result. [Tools: get_upcoming]"},
        {"role": "user", "content": "X" * 2100},
    ]
    before = deepcopy(history)
    messages = build_messages("and tommrow?", context=history,
                              prior_tools=("get_upcoming",), now=NOW)
    assert history == before
    assert messages[-1] == {"role": "user", "content": "and tommrow?"}
    assert messages[1:-1] == natural_context(history)
    assert len(messages[1:-1]) == 5
    assert len(messages[-2]["content"]) == 2000
    assert "[Tools:" not in messages[-3]["content"]
    assert "Local clock: 2026-10-07T12:00:00+00:00" in messages[0]["content"]
    assert 'Prior completed tools (source metadata only): ["get_upcoming"]' in messages[0]["content"]
    assert "Intent schema:" not in messages[0]["content"]


def test_decoder_options_do_not_mutate_shared_schema():
    options = completion_options(SCHEMA)
    assert options["max_tokens"] == MAX_OUTPUT_TOKENS == 900
    assert options["temperature"] == 0
    assert options["chat_template_kwargs"] == {"enable_thinking": False}
    options["response_format"]["json_schema"]["schema"]["required"].clear()
    assert len(SCHEMA["required"]) == 5


def test_future_training_prefix_equals_serving_messages_and_masks_history():
    messages = build_messages("Only tomorrow", now=NOW, context=[
        {"role": "user", "content": "My calendar this week"},
        {"role": "assistant", "content": "Calendar overview."},
    ])
    before = deepcopy(messages)
    answer = {"version": 1, "kind": "read", "sources": [
        {"domain": "calendar", "operation": "overview", "time": {"named": "tomorrow"}}],
        "excluded_sources": [], "unsupported_constraints": []}
    trained = training_messages(messages, answer)
    assert messages == before
    assert [{k: v for k, v in m.items() if k != "training"} for m in trained[:-1]] == messages
    assert trained[2]["training"] is False
    assert trained[-1]["training"] is True
    assert json.loads(trained[-1]["content"]) == answer
    with pytest.raises(ValueError):
        training_messages([], answer)


def test_repair_is_bounded_and_never_replays_model_output():
    message = repair_message("bad filter " + "x" * 1000)
    assert message["role"] == "user"
    assert len(message["content"]) < 450
    assert "original latest request" in message["content"]
    assert "x" * 201 not in message["content"]


def test_development_adapter_uses_exact_serving_request_and_safe_variants(tmp_path):
    spec = importlib.util.spec_from_file_location("alignment_adapter", ROOT / "eval/prompt-alignment-dev-20261007/build_requests.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    cases = adapter.load_cases()
    assert len(cases) == 16 and all(c["synthetic"] for c in cases)
    first = cases[0]
    rows = adapter.build_variants(first)
    expected = build_messages(first["prompt"], context=first["history"],
                             prior_tools=first["prior_tools"], now=datetime.fromisoformat(first["now"]))
    assert rows["serving"]["messages"] == expected
    assert rows["core"]["messages"][0]["content"] == SYSTEM
    assert "Intent schema:" in rows["schema_text"]["messages"][0]["content"]
    assert {row["max_tokens"] for row in rows.values()} == {900}
    assert all(row["response_format"] == completion_options(SCHEMA)["response_format"] for row in rows.values())
    manifest = adapter.write_bundle(tmp_path / "requests")
    assert manifest["inference_performed"] is False
    assert manifest["cases"] == 16
    with pytest.raises(FileExistsError):
        adapter.write_bundle(tmp_path / "requests")


_DEVELOPMENT_CASES = [json.loads(line) for line in
    (ROOT / "eval/prompt-alignment-dev-20261007/cases.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("case", _DEVELOPMENT_CASES, ids=lambda case: case["id"])
def test_development_expected_intent_validates_and_read_compiles(case):
    from service.router.intent.validation import validate_intent
    from service.router.intent.compiler import compile_intent
    now = datetime.fromisoformat(case["now"])
    intent = validate_intent(case["expected"], case["prompt"],
        context=natural_context(case["history"]), prior_tools=case["prior_tools"], now=now)
    if intent.kind == "read":
        calls, _ = compile_intent(intent, now=now)
        assert calls


def test_cpu_capture_rejects_nonisolated_execution_before_creating_output(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    spec = importlib.util.spec_from_file_location("alignment_cpu_guard",
        ROOT / "eval/prompt-alignment-dev-20261007/cpu_latency.py")
    timing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(timing)
    with pytest.raises(RuntimeError, match="isolated pytest bootstrap"):
        timing.capture(tmp_path / "forbidden")
    assert not (tmp_path / "forbidden").exists()


def test_registered_cpu_latency_capture(monkeypatch):
    import os
    output = os.environ.get("PROMPT_ALIGNMENT_CPU_OUTPUT")
    if not output:
        pytest.skip("CPU timing runs only in its explicitly registered measurement window")
    from service.tools import registry
    from service.workflows import reads

    async def forbidden(*args, **kwargs):
        raise AssertionError("CPU timing must never execute tools")
    monkeypatch.setattr(registry, "run_tool", forbidden)
    monkeypatch.setattr(reads, "run_tool", forbidden)
    spec = importlib.util.spec_from_file_location("alignment_cpu_latency",
        ROOT / "eval/prompt-alignment-dev-20261007/cpu_latency.py")
    timing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(timing)
    summary = timing.capture(output)
    assert summary["cases"] == 16
    assert summary["synthetic_clocks"] == {row["id"]: row["now"] for row in _DEVELOPMENT_CASES}
    assert all(result["n"] == 800 for result in summary["paths"].values())
    assert summary["model_inference"] is False and summary["tool_execution"] is False


@pytest.mark.parametrize("case_id,expected_calls", [
    ("PA003", [("get_upcoming", {"period": "tomorrow", "calendar_only": True})]),
    ("PA004", [("get_upcoming", {"period": "this week", "calendar_only": False})]),
    ("PA008", [("view_emails", {"query": "Mira", "unread": True,
                                "strict_match": True, "period": "yesterday"})]),
    ("PA009", [("summarize_messages", {"conversation": "Rowan", "count": 5, "period": "yesterday"})]),
    ("PA010", [("get_upcoming", {"period": "this week", "calendar_only": True}),
               ("summarize_emails", {"period": "this week"})]),
])
def test_repaired_cases_compile_exact_source_filters(case_id, expected_calls):
    from service.router.intent.validation import applicable_read, validate_intent
    from service.router.intent.compiler import compile_intent
    case = next(c for c in _DEVELOPMENT_CASES if c["id"] == case_id)
    now = datetime.fromisoformat(case["now"])
    assert applicable_read(case["prompt"], case["history"], case["prior_tools"])
    intent = validate_intent(case["expected"], case["prompt"], context=case["history"],
                            prior_tools=case["prior_tools"], now=now)
    calls, _ = compile_intent(intent, now=now)
    assert calls == expected_calls


@pytest.mark.parametrize("case_id,field,value", [
    ("PA003", "time", {"named": "next week"}),
    ("PA004", "time", {"named": "next week"}),
    ("PA008", "query", "Mira Smith"),
    ("PA008", "time", None),
    ("PA009", "count", 6),
    ("PA010", "excluded_sources", []),
])
def test_repaired_reads_still_reject_changed_or_dropped_constraints(case_id, field, value):
    from service.router.intent.validation import InvalidIntent, validate_intent
    case = next(c for c in _DEVELOPMENT_CASES if c["id"] == case_id)
    answer = deepcopy(case["expected"])
    if field == "excluded_sources":
        answer[field] = value
    elif value is None:
        answer["sources"][0].pop(field)
    else:
        answer["sources"][0][field] = value
    with pytest.raises(InvalidIntent):
        validate_intent(answer, case["prompt"], context=case["history"],
            prior_tools=case["prior_tools"], now=datetime.fromisoformat(case["now"]))


@pytest.mark.parametrize("prompt", [
    "What public events are in Berlin this weej?",
    "whats up for this weef?",
    "Recap my calendar and email Rowan saying I can attend",
    "Recap my calendar this week; leave messages out; send email to Mira",
])
def test_scope_typo_repair_does_not_admit_public_unknown_or_effect_requests(prompt):
    from service.router.intent.validation import applicable_read
    assert not applicable_read(prompt)


@pytest.mark.parametrize("domain,query,prompt", [
    ("notes", "leave messages out this weej", 'Find notes named "leave messages out this weej"'),
    ("email", "Mira yesterday", 'Find email from "Mira yesterday"'),
])
def test_quoted_exclusion_typo_and_sender_date_remain_literal(domain, query, prompt):
    from service.router.intent.validation import InvalidIntent, validate_intent, source_requirements
    answer = {"version": 1, "kind": "read", "sources": [
        {"domain": domain, "operation": "records", "query": query}],
        "excluded_sources": [], "unsupported_constraints": []}
    assert source_requirements(prompt) == ({domain}, set())
    intent = validate_intent(answer, prompt, now=NOW)
    assert intent.sources[0].query == query and intent.sources[0].time is None
    answer["sources"][0]["time"] = {"named": "yesterday" if domain == "email" else "this week"}
    with pytest.raises(InvalidIntent):
        validate_intent(answer, prompt, now=NOW)


def test_terminal_count_fix_does_not_drop_an_independent_message_read():
    from service.router.intent.validation import InvalidIntent, validate_intent
    case = next(c for c in _DEVELOPMENT_CASES if c["id"] == "PA009")
    with pytest.raises(InvalidIntent):
        validate_intent(case["expected"], "Recap texts with Rowan yesterday; find messages from Mira today", now=NOW)
