#!/usr/bin/env python3
"""Measure Ling's first tool-selection step without executing any tool.

The benchmark freezes the comparison contract needed for a later Laya run:
the user prompt, conversational context, ordered candidate tool names, and
tool schemas.  Ling receives Wisp's real first-step prompt; Laya must later use
the manifest's byte-identical ``canonical_state`` and ordered candidates.

No tool body is called.  ``run_agent`` is stopped at the boundary where it
would send its first model request, and that captured request is sent directly
to the local oMLX chat-completions endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Importing Wisp initializes its SQLite stores. Redirect those stores before
# importing service modules so a benchmark never reads or writes real user
# state while still leaving HOME intact for the local oMLX configuration.
os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-ling-benchmark-"))

from service.agent.loop import run_agent  # noqa: E402
from service.config import omlx_api_key, omlx_base_url  # noqa: E402
from service.router import route  # noqa: E402


CORPUS_PATH = Path(__file__).with_name("corpus.json")
DEFAULT_MANIFEST = Path(__file__).with_name("ling_comparison_manifest.json")
DEFAULT_OUTPUT = Path(__file__).with_name("results") / "ling-tool-selection.json"
DEFAULT_MODEL = "Ling-3.0-tiny-oQ6e"
FIXED_CLOCK = (
    "Current date/time: Sunday, September 20, 2026 at 10:00 AM PDT "
    "(America/Los_Angeles). Resolve relative dates/times against this."
)

# A bounded cross-section of the shared Laya corpus: narrow and broad menus,
# reads and effects, one compound workflow, a contextual follow-up, and three
# multilingual prompts.  The canonical text comes from corpus.json and is not
# duplicated here, so later edits cannot silently create two prompt versions.
DEFAULT_CASE_IDS = [
    "calendar_create",
    "calendar_update",
    "reminder_create",
    "reminder_complete",
    "email_summary",
    "email_send",
    "email_reply",
    "message_send",
    "contact_lookup",
    "file_find",
    "file_read",
    "note_create",
    "memory_store",
    "system_volume",
    "app_open",
    "music_play",
    "travel_directions",
    "compound_agenda_send",
    "followup_email_channel",
    "es_message",
    "fr_email",
    "de_file",
]


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256(value: Any) -> str:
    raw = value if isinstance(value, str) else stable_json(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return ordered[index]


def aggregate(values: list[float]) -> dict[str, float | None]:
    return {
        "count": len(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "mean": statistics.fmean(values) if values else None,
    }


def git_revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


def conversation(case: dict[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if case.get("last_user"):
        messages.append({"role": "user", "content": case["last_user"]})
    if case.get("last_assistant"):
        messages.append({"role": "assistant", "content": case["last_assistant"]})
    messages.append({"role": "user", "content": case["prompt"]})
    return messages


class CaptureStop(RuntimeError):
    pass


class CaptureClient:
    """Captures the exact first request built by run_agent."""

    target = None

    def __init__(self) -> None:
        self.request: dict[str, Any] | None = None

    async def ensure_only(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def stream_events(
        self, model: str, messages: list[dict[str, Any]], **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        self.request = {
            "model": model,
            "messages": messages,
            **kwargs,
        }
        raise CaptureStop
        yield {}  # pragma: no cover - makes this an async generator


class RejectApprover:
    async def confirm(self, action: Any) -> bool:
        raise AssertionError("The capture boundary must precede approvals")


async def _emit(_: dict[str, Any]) -> None:
    return None


def style_hint(decision: Any) -> str | None:
    # Importing these constants keeps the capture aligned with the live /agent
    # endpoint without copying their text into the benchmark.
    from service.main import (
        _CLARIFY_CHANNEL_HINT,
        _CLARIFY_TARGET_HINT,
        _LIGHT_READ_STYLE,
    )

    parts = []
    if decision.light_read:
        parts.append(_LIGHT_READ_STYLE)
    if decision.clarify_channel:
        parts.append(_CLARIFY_CHANNEL_HINT)
    if decision.clarify_target:
        parts.append(_CLARIFY_TARGET_HINT)
    return "\n".join(parts) or None


async def capture_case(
    case: dict[str, Any], target_model: str,
) -> tuple[Any, dict[str, Any] | None]:
    decision = await route(
        case["prompt"],
        last_user=case.get("last_user"),
        recent_users=case.get("recent_users"),
        last_assistant=case.get("last_assistant"),
        last_tools=case.get("last_tools"),
    )
    if not decision.needs_tools or decision.direct_calls:
        return decision, None

    client = CaptureClient()
    with ExitStack() as stack:
        stack.enter_context(patch(
            "service.agent.loop.prompt_blocks.now_line", return_value=FIXED_CLOCK,
        ))
        stack.enter_context(patch(
            "service.agent.loop.prompt_blocks.memory_block", return_value="",
        ))
        stack.enter_context(patch(
            "service.memory.identity.identity_prompt_block", return_value="",
        ))
        stack.enter_context(patch(
            "service.skills.skills_context_block", return_value="",
        ))
        try:
            await run_agent(
                client,
                target_model,
                conversation(case),
                _emit,
                RejectApprover(),
                tools=decision.tool_subset,
                force_first_tool=decision.force_first_tool,
                expect_tool_first=decision.expect_tool_first,
                style_hint=style_hint(decision),
                include_memory_context=False,
                multi_round=decision.multi_round,
                narration_after=decision.narration_after,
                direct_calls=[],
                required_tool_groups=decision.required_tool_groups,
                forbidden_tools=decision.forbidden_tools,
                conditional_tools=decision.conditional_tools,
                tool_argument_bindings=decision.tool_argument_bindings,
                reminder_action=decision.reminder_action,
                test_mode=False,
                debug=False,
            )
        except CaptureStop:
            pass
    if client.request is None:
        raise RuntimeError(f"{case['id']}: failed to capture first Ling request")
    return decision, client.request


async def build_manifest(
    case_ids: list[str], target_model: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    corpus = {row["id"]: row for row in json.loads(CORPUS_PATH.read_text())}
    missing = [case_id for case_id in case_ids if case_id not in corpus]
    if missing:
        raise ValueError(f"Unknown case ids: {missing}")

    tool_schemas: dict[str, dict[str, Any]] = {}
    requests: dict[str, dict[str, Any]] = {}
    manifest_cases = []
    for case_id in case_ids:
        case = corpus[case_id]
        decision, request = await capture_case(case, target_model)
        direct = [{"tool": name, "args": args} for name, args in decision.direct_calls]
        if request is None:
            candidate_names: list[str] = []
            request_hash = None
            tool_choice = None
            max_tokens = None
        else:
            candidates = request.get("tools") or []
            candidate_names = [item["function"]["name"] for item in candidates]
            for schema in candidates:
                tool_schemas[schema["function"]["name"]] = schema
            request_hash = sha256(request)
            tool_choice = request.get("tool_choice")
            max_tokens = request.get("max_tokens")
            requests[case_id] = request

        canonical_state = case.get("laya_state", case["prompt"])
        available = set(candidate_names)
        expected_group_availability = [
            bool(available & set(group)) for group in case["expected_tool_groups"]
        ]
        manifest_cases.append({
            "id": case_id,
            "language": case["language"],
            "prompt": case["prompt"],
            "prompt_sha256": sha256(case["prompt"]),
            "canonical_state": canonical_state,
            "canonical_state_sha256": sha256(canonical_state),
            "conversation": conversation(case),
            "expected_tool_groups": case["expected_tool_groups"],
            "expected_tool_group_availability": expected_group_availability,
            "expected_first_tool_available": (
                expected_group_availability[0] if expected_group_availability else True
            ),
            "candidate_tools": candidate_names,
            "candidate_tools_sha256": sha256(candidate_names),
            "tool_choice": tool_choice,
            "max_tokens": max_tokens,
            "ling_request_sha256": request_hash,
            "route": decision.as_dict(),
            "direct_calls": direct,
        })

    manifest = {
        "schema_version": 1,
        "source_revision": git_revision(),
        "model": target_model,
        "fixed_clock": FIXED_CLOCK,
        "comparison_contract": {
            "canonical_state": "byte-identical across Ling and Laya",
            "candidate_tool_names_and_order": "byte-identical across Ling and Laya",
            "tool_schema_source": "frozen below by tool name",
            "model_specific_wrappers": "allowed and reported separately",
        },
        "tool_schemas": {name: tool_schemas[name] for name in sorted(tool_schemas)},
        "cases": manifest_cases,
    }
    manifest["manifest_sha256"] = sha256(manifest)
    return manifest, requests


def _tool_call_rows(calls: dict[int, dict[str, str]]) -> list[dict[str, Any]]:
    rows = []
    for _, call in sorted(calls.items()):
        try:
            args = json.loads(call["arguments"])
        except (TypeError, json.JSONDecodeError):
            args = None
        rows.append({
            "id": call["id"],
            "name": call["name"],
            "arguments": args,
            "raw_arguments": call["arguments"],
        })
    return rows


async def run_request(
    client: httpx.AsyncClient,
    case: dict[str, Any],
    request: dict[str, Any],
    *,
    rep: int,
    phase: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request["model"],
        "messages": request["messages"],
        "max_tokens": request.get("max_tokens", 3000),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.get("temperature") is not None:
        payload["temperature"] = request["temperature"]
    if request.get("tools"):
        payload["tools"] = request["tools"]
        payload["tool_choice"] = request.get("tool_choice") or "auto"
    calls: dict[int, dict[str, str]] = {}
    reasoning_chars = 0
    content_chars = 0
    first_model_delta = None
    first_tool_delta = None
    last_tool_delta = None
    finish_reason = None
    usage: dict[str, Any] = {}
    timings: dict[str, Any] = {}
    started = time.perf_counter()
    async with client.stream("POST", "/v1/chat/completions", json=payload) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            body = line[len("data:"):].strip()
            if body == "[DONE]":
                break
            chunk = json.loads(body)
            if chunk.get("usage"):
                usage = chunk["usage"]
            for key in ("timing", "timings"):
                if chunk.get(key):
                    timings[key] = chunk[key]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or ""
            content = delta.get("content") or ""
            tool_deltas = delta.get("tool_calls") or []
            if reasoning or content or tool_deltas:
                now = time.perf_counter()
                first_model_delta = first_model_delta or now
            reasoning_chars += len(reasoning)
            content_chars += len(content)
            for tool_delta in tool_deltas:
                now = time.perf_counter()
                first_tool_delta = first_tool_delta or now
                last_tool_delta = now
                slot = calls.setdefault(
                    tool_delta.get("index", 0),
                    {"id": "", "name": "", "arguments": ""},
                )
                if tool_delta.get("id"):
                    slot["id"] = tool_delta["id"]
                function = tool_delta.get("function") or {}
                if function.get("name"):
                    slot["name"] += function["name"]
                if function.get("arguments"):
                    slot["arguments"] += function["arguments"]
    finished = time.perf_counter()
    parsed_calls = _tool_call_rows(calls)
    candidate_names = case["candidate_tools"]
    emitted_names = [call["name"] for call in parsed_calls]
    expected_first = set(case["expected_tool_groups"][0]) if case["expected_tool_groups"] else set()
    first_name = emitted_names[0] if emitted_names else None
    valid_calls = bool(parsed_calls) and all(
        call["name"] in candidate_names and isinstance(call["arguments"], dict)
        for call in parsed_calls
    )
    expected_hit = first_name in expected_first if expected_first else not parsed_calls

    def elapsed_ms(mark: float | None) -> float | None:
        return round((mark - started) * 1000, 3) if mark is not None else None

    return {
        "case_id": case["id"],
        "language": case["language"],
        "phase": phase,
        "rep": rep,
        "prompt_sha256": case["prompt_sha256"],
        "canonical_state_sha256": case["canonical_state_sha256"],
        "candidate_tools_sha256": case["candidate_tools_sha256"],
        "ling_request_sha256": case["ling_request_sha256"],
        "candidate_count": len(candidate_names),
        "tool_choice": case["tool_choice"],
        "first_model_delta_ms": elapsed_ms(first_model_delta),
        "first_tool_delta_ms": elapsed_ms(first_tool_delta),
        "complete_tool_call_ms": elapsed_ms(last_tool_delta),
        "stream_complete_ms": round((finished - started) * 1000, 3),
        "reasoning_chars": reasoning_chars,
        "content_chars": content_chars,
        "finish_reason": finish_reason,
        "tool_calls": parsed_calls,
        "valid_tool_calls": valid_calls,
        "expected_first_tool_hit": expected_hit,
        "expected_first_tool_available": case["expected_first_tool_available"],
        "usage": usage,
        "server_timings": timings,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    warm = [row for row in rows if row["phase"] == "warm"]
    eligible = [row for row in warm if row["expected_first_tool_available"]]
    complete = [row["complete_tool_call_ms"] for row in warm
                if row["complete_tool_call_ms"] is not None]
    return {
        "warm_runs": len(warm),
        "warm_cases": len({row["case_id"] for row in warm}),
        "first_model_delta_ms": aggregate([
            row["first_model_delta_ms"] for row in warm
            if row["first_model_delta_ms"] is not None
        ]),
        "first_tool_delta_ms": aggregate([
            row["first_tool_delta_ms"] for row in warm
            if row["first_tool_delta_ms"] is not None
        ]),
        "complete_tool_call_ms": aggregate(complete),
        "stream_complete_ms": aggregate([row["stream_complete_ms"] for row in warm]),
        "valid_tool_call_rate": (
            sum(bool(row["valid_tool_calls"]) for row in warm) / len(warm) if warm else None
        ),
        "expected_first_tool_accuracy_when_available": (
            sum(bool(row["expected_first_tool_hit"]) for row in eligible) / len(eligible)
            if eligible else None
        ),
        "eligible_accuracy_runs": len(eligible),
        "router_missing_expected_first_tool_runs": len(warm) - len(eligible),
        "no_tool_call_runs": sum(row["complete_tool_call_ms"] is None for row in warm),
    }


async def main(args: argparse.Namespace) -> None:
    case_ids = args.case_ids.split(",") if args.case_ids else DEFAULT_CASE_IDS
    manifest, requests = await build_manifest(case_ids, args.model)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "manifest": str(args.manifest),
        "manifest_sha256": manifest["manifest_sha256"],
        "captured_cases": len(manifest["cases"]),
        "inference_cases": len(requests),
    }), flush=True)
    if args.manifest_only:
        return

    api_key = omlx_api_key()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    client = httpx.AsyncClient(
        base_url=omlx_base_url(), headers=headers,
        timeout=httpx.Timeout(args.timeout, connect=5.0),
        trust_env=False, follow_redirects=False,
    )
    rows = []
    try:
        status_response = await client.get("/v1/models/status")
        status_response.raise_for_status()
        loaded_before = [row["id"] for row in status_response.json()["models"]
                         if row.get("loaded")]
        load_started = time.perf_counter()
        if manifest["model"] not in loaded_before:
            load_response = await client.post(f"/v1/models/{manifest['model']}/load")
            load_response.raise_for_status()
            deadline = time.monotonic() + args.load_timeout
            while time.monotonic() < deadline:
                status_response = await client.get("/v1/models/status")
                status_response.raise_for_status()
                loaded = [row["id"] for row in status_response.json()["models"]
                          if row.get("loaded")]
                if manifest["model"] in loaded:
                    break
                await asyncio.sleep(1)
            else:
                raise TimeoutError(f"{manifest['model']} did not load")
        load_seconds = time.perf_counter() - load_started
        status_response = await client.get("/v1/models/status")
        status_response.raise_for_status()
        loaded_after = [row["id"] for row in status_response.json()["models"]
                        if row.get("loaded")]

        cases = {row["id"]: row for row in manifest["cases"] if row["id"] in requests}
        ordered = [case_id for case_id in case_ids if case_id in requests]
        if not ordered:
            raise RuntimeError("No captured case requires Ling tool selection")

        first_id = ordered[0]
        first_probe = await run_request(
            client, cases[first_id], requests[first_id], rep=0, phase="first_probe",
        )
        rows.append(first_probe)
        print(json.dumps({
            "phase": "first_probe", "case": first_id,
            "complete_tool_call_ms": first_probe["complete_tool_call_ms"],
            "valid": first_probe["valid_tool_calls"],
            "expected": first_probe["expected_first_tool_hit"],
        }), flush=True)

        for rep in range(1, args.reps + 1):
            rep_order = ordered if rep % 2 else list(reversed(ordered))
            for index, case_id in enumerate(rep_order, 1):
                row = await run_request(
                    client, cases[case_id], requests[case_id], rep=rep, phase="warm",
                )
                rows.append(row)
                print(json.dumps({
                    "phase": "warm", "rep": rep, "index": index,
                    "total": len(rep_order), "case": case_id,
                    "complete_tool_call_ms": row["complete_tool_call_ms"],
                    "valid": row["valid_tool_calls"],
                    "expected": row["expected_first_tool_hit"],
                }), flush=True)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps({
                    "kind": "ling_tool_selection_latency",
                    "model": manifest["model"],
                    "manifest": display_path(args.manifest),
                    "manifest_sha256": manifest["manifest_sha256"],
                    "source_revision": manifest["source_revision"],
                    "machine": {
                        "platform": platform.platform(),
                        "machine": platform.machine(),
                        "python": platform.python_version(),
                    },
                    "loaded_before": loaded_before,
                    "loaded_after": loaded_after,
                    "model_ready_seconds": load_seconds,
                    "summary": summarize(rows),
                    "rows": rows,
                }, ensure_ascii=False, indent=2) + "\n")
    finally:
        await client.aclose()

    print(json.dumps({
        "output": str(args.output),
        "model_ready_seconds": load_seconds,
        "summary": summarize(rows),
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--case-ids", help="Comma-separated corpus case ids")
    parser.add_argument("--reps", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--load-timeout", type=float, default=120.0)
    parser.add_argument("--manifest-only", action="store_true")
    asyncio.run(main(parser.parse_args()))
