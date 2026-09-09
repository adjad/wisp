#!/usr/bin/env python3
"""Capture real Wisp requests offline, then compare synthetic local inference.

Capture uses isolated WISP_HOME, fake tools, and a fake model; live comparison
submits only the saved synthetic requests to oMLX and never executes tool calls.
It does not unload models or change model settings. See --help for commands.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch


MODEL = "Ling-3.0-tiny-oQ4e"
CLOCK = "\nThe current date and time is Tuesday, September 8, 2026 at 6:30 PM."
BODY = (
    "The workshop is on September 15 at 2:00 PM in Room 204. "
    "Please arrive ten minutes early and bring your laptop and charging cable. "
    "We will review the three draft proposals, choose one to prototype, and assign owners. "
    "The materials are available in the shared folder. "
    "Please send your questions by September 12 so we can include them in the agenda. "
    "The session ends at 3:30 PM; no additional preparation is required."
)


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


async def capture(root: Path, output: Path) -> None:
    sys.path.insert(0, str(root))
    os.environ["WISP_HOME"] = tempfile.mkdtemp(prefix="wisp-latency-capture-")
    from contextlib import ExitStack
    from dataclasses import replace
    from service import main
    from service.agent import loop
    from service.memory.store import SessionStore
    from service.tools.registry import REGISTRY

    class Client:
        def __init__(self):
            self.requests = []
            self.loads = 0

        async def ensure_only(self, *args, **kwargs):
            self.loads += 1

        async def stream_events(self, model, messages, **kwargs):
            self.requests.append(copy.deepcopy({"model": model, "messages": messages, **kwargs}))
            yield {"kind": "content", "text": "You're welcome."}
            yield {"kind": "final", "message": {
                "role": "assistant", "content": "You're welcome.",
                "reasoning_content": "", "tool_calls": None}}

    class CaptureStop(Exception):
        pass

    class FirstRequestClient(Client):
        async def stream_events(self, model, messages, **kwargs):
            self.requests.append(copy.deepcopy({"model": model, "messages": messages, **kwargs}))
            raise CaptureStop
            yield  # make this an async iterator

    class Approver:
        async def confirm(self, action):
            raise AssertionError("Capture must not execute effects")

    def forbidden(*args, **kwargs):
        raise AssertionError("Real tools/processes/network are forbidden during capture")

    async def emit(event):
        pass

    async def no_task(*args, **kwargs):
        return None

    def now_line(*, resolve_hint=False):
        return CLOCK + (" Resolve relative dates/times against this." if resolve_hint else "")

    rows = []
    # No lifespan startup: call the endpoint directly and consume its stream.
    with ExitStack() as stack:
        stack.enter_context(patch("socket.socket.connect", forbidden))
        stack.enter_context(patch("socket.socket.connect_ex", forbidden))
        stack.enter_context(patch("subprocess.Popen", forbidden))
        stack.enter_context(patch("service.memory.identity.identity_prompt_block", return_value=""))
        stack.enter_context(patch("service.memory.prompt_blocks.memory_block", return_value=""))
        stack.enter_context(patch.object(main, "memory_block", return_value=""))
        stack.enter_context(patch("service.memory.prompt_blocks.now_line", side_effect=now_line))
        stack.enter_context(patch.object(main, "now_line", side_effect=now_line))
        stack.enter_context(patch("service.skills.skills_context_block", return_value=""))
        stack.enter_context(patch("service.skills.always_skills_block", return_value=""))
        stack.enter_context(patch("service.skills.selected_skill_block", return_value=""))
        stack.enter_context(patch("service.tasks.reply_engine.prepare_task_turn_async", side_effect=no_task))
        stack.enter_context(patch.dict(REGISTRY, {n: replace(t, func=forbidden) for n, t in REGISTRY.items()}))

        for pin, prompt in [("agent", "thanks"), ("coding", "thanks"),
                            ("reasoning", "hello"), ("agent", "okay shorter")]:
            client = Client()
            starts = []
            async def start_engine():
                starts.append(True)
            session_store = SessionStore(Path(tempfile.mkdtemp()) / "sessions.db")
            sid = session_store.create_session()
            session_store.set_pinned(sid, pin, MODEL)
            session_store.add_turn(sid, "user", "Explain the workshop plan.")
            session_store.add_turn(sid, "assistant", BODY)
            with patch.object(main, "client", client, create=True), patch.object(main, "store", session_store), \
                    patch("service.memory.context.store", session_store), \
                    patch.object(main, "ensure_omlx", start_engine):
                response = await main.agent({"prompt": prompt, "session_id": sid, "debug": False})
                events = [json.loads(chunk.removeprefix("data: ").strip())
                          async for chunk in response.body_iterator]
            errors = [e for e in events if e.get("type") == "error"]
            if errors or not client.requests:
                raise RuntimeError(f"Social capture failed: {pin}/{prompt}: {errors}")
            rows.append({"case": f"social_{pin}_{prompt.replace(' ', '_')}",
                         "kind": "social", "prompt": prompt,
                         "request": client.requests[0],
                         "engine_starts": len(starts), "model_load_checks": client.loads})

        # This route has a deterministic direct result. Only its function is fake.
        result_text = "Synthetic conversation digest: workshop confirmed for September 15."
        REGISTRY["summarize_messages"] = replace(REGISTRY["summarize_messages"], func=lambda **kw: result_text)
        client = Client()
        starts = []
        async def start_engine():
            starts.append(True)
        session_store = SessionStore(Path(tempfile.mkdtemp()) / "sessions.db")
        with patch.object(main, "client", client, create=True), patch.object(main, "store", session_store), \
                patch("service.memory.context.store", session_store), \
                patch.object(main, "ensure_omlx", start_engine):
            response = await main.agent({"prompt": "summarize my messages", "debug": False})
            events = [json.loads(chunk.removeprefix("data: ").strip())
                      async for chunk in response.body_iterator]
        answer = "".join(e.get("text", "") for e in events if e.get("type") in {"text", "delta"})
        if any(e.get("type") == "error" for e in events):
            raise RuntimeError(f"Direct capture failed: {events}")
        rows.append({"case": "deterministic_messages", "kind": "deterministic",
                     "answer": answer, "expected_answer": result_text,
                     "engine_starts": len(starts), "model_load_checks": client.loads,
                     "model_requests": len(client.requests)})

        send_cases = [
            ("send_message_phone", "send_message", {"to": "+15555550123", "text": BODY},
             f"Send this exact text to +15555550123 using Messages: {BODY}"),
            ("send_message_address", "send_message", {"to": "alex@example.com", "text": BODY},
             f"Send this exact text to the iMessage address alex@example.com using Messages: {BODY}"),
            ("send_email", "send_email", {"to": "alex@example.com", "subject": "Workshop plan", "body": BODY},
             f"Email alex@example.com with subject Workshop plan and this exact body: {BODY}"),
            ("reply_bracketed_id", "reply_to_email", {"message_id": "<workshop-123@example.com>", "body": BODY},
             f"Reply in the email thread with Message-ID <workshop-123@example.com> using this exact body: {BODY}"),
            ("reply_canonical_id", "reply_to_email", {"message_id": "workshop-123@example.com", "body": BODY},
             f"Reply in the email thread with Message-ID workshop-123@example.com using this exact body: {BODY}"),
        ]
        for case_name, name, expected, prompt in send_cases:
            client = FirstRequestClient()
            try:
                await loop.run_agent(client, MODEL, [{"role": "user", "content": prompt}],
                    emit, Approver(), tools=[name], force_first_tool=name,
                    include_memory_context=False, debug=False)
            except CaptureStop:
                pass
            if len(client.requests) != 1:
                raise RuntimeError(f"Missing first request for {name}")
            rows.append({"case": case_name, "kind": "send", "expected_tool": name,
                         "expected_args": expected,
                         "request": client.requests[0]})

    files = ["service/main.py", "service/agent/loop.py", "service/tools/action_tools.py"]
    save(output, {"source_root": str(root), "model": MODEL,
                  "source_sha256": {n: hashlib.sha256((root/n).read_bytes()).hexdigest() for n in files},
                  "cases": rows})
    print(json.dumps({"captured": str(output), "cases": len(rows),
                      "requests": sum("request" in r for r in rows)}), flush=True)


async def compare(before: Path, after: Path, output: Path, reps: int) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from service.inference.omlx_client import OMLXClient
    from tokenizers import Tokenizer
    tok = Tokenizer.from_file(str(Path.home()/"Desktop/OMLX_Model_Files"/MODEL/"tokenizer.json"))
    nt = lambda s: len(tok.encode(s or "", add_special_tokens=False).ids)
    captures = {"before": json.loads(before.read_text()), "after": json.loads(after.read_text())}
    rows = []
    client = OMLXClient(timeout=120)
    try:
        # Avoid loading/unloading anything as a benchmark side effect.
        if MODEL not in await client.loaded_models():
            raise RuntimeError("Ling must already be resident; benchmark does not load or unload models")
        cases = {arm: {r["case"]: r for r in cap["cases"]} for arm, cap in captures.items()}
        selected = [n for n in cases["before"] if n != "social_agent_okay_shorter" and "request" in cases["before"][n]]
        for rep in range(reps):
            for name in selected:
                for arm in (["before", "after"] if rep % 2 == 0 else ["after", "before"]):
                    spec = cases[arm][name]
                    payload = copy.deepcopy(spec["request"])
                    payload = {k: v for k, v in payload.items() if v is not None}
                    if not payload.get("tools"):
                        payload.pop("tools", None)
                        payload.pop("tool_choice", None)
                    payload["stream"] = True
                    payload["stream_options"] = {"include_usage": True}
                    first = last = None
                    usage = {}
                    server_timing = {}
                    content = reasoning = ""
                    calls = {}
                    finish = None
                    begin = time.perf_counter()
                    async with client._client.stream("POST", "/v1/chat/completions", json=payload) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line.startswith("data: ") or line[6:].strip() == "[DONE]":
                                continue
                            chunk = json.loads(line[6:])
                            if chunk.get("usage"):
                                usage = chunk["usage"]
                            for key in ("timings", "timing"):
                                if chunk.get(key):
                                    server_timing[key] = chunk[key]
                            choices = chunk.get("choices") or []
                            if not choices:
                                continue
                            choice = choices[0]
                            finish = choice.get("finish_reason") or finish
                            delta = choice.get("delta") or {}
                            if any(delta.get(k) for k in ("content", "reasoning_content", "tool_calls")):
                                last = time.perf_counter()
                                first = first or last
                            content += delta.get("content") or ""
                            reasoning += delta.get("reasoning_content") or ""
                            for tc in delta.get("tool_calls") or []:
                                target = calls.setdefault(tc.get("index", 0), {"name": "", "arguments": ""})
                                fn = tc.get("function") or {}
                                target["name"] += fn.get("name") or ""
                                target["arguments"] += fn.get("arguments") or ""
                    elapsed = time.perf_counter() - begin
                    parsed = []
                    for call in calls.values():
                        try:
                            args = json.loads(call["arguments"])
                        except (TypeError, json.JSONDecodeError):
                            args = None
                        parsed.append({"name": call["name"], "args": args})
                    if spec["kind"] == "send":
                        # Native reply envelopes must be resolved by Wisp, never
                        # invented by the model. Permit only harmless optional
                        # defaults beyond the exact supplied payload fields.
                        optional_defaults = {"reply_all": False, "account": ""}
                        valid = (finish != "length" and len(parsed) == 1
                                 and parsed[0]["name"] == spec["expected_tool"]
                                 and isinstance(parsed[0]["args"], dict)
                                 and all(parsed[0]["args"].get(k) == v for k, v in spec["expected_args"].items())
                                 and all(k in optional_defaults and v == optional_defaults[k]
                                         for k, v in parsed[0]["args"].items()
                                         if k not in spec["expected_args"]))
                    else:
                        valid = bool(content.strip()) and not parsed and finish != "length"
                    row = {"case": name, "arm": arm, "rep": rep+1, "seconds": elapsed,
                           "first_model_delta_s": first-begin if first else None,
                           "generation_delta_span_s": last-first if last and first else None,
                           "usage": usage, "server_timing": server_timing,
                           "finish_reason": finish, "quality_pass": valid,
                           "content_tokens": nt(content), "reasoning_tokens": nt(reasoning.strip()),
                           "content": content, "reasoning": reasoning, "tool_calls": parsed}
                    rows.append(row)
                    save(output, {"before": str(before), "after": str(after), "reps": reps, "rows": rows})
                    print(json.dumps({k: row[k] for k in ["case", "arm", "rep", "seconds", "usage", "quality_pass", "content_tokens", "reasoning_tokens"]}), flush=True)
    finally:
        await client.aclose()
    summary = {}
    for name in selected:
        summary[name] = {}
        for arm in captures:
            sample = [r for r in rows if r["case"] == name and r["arm"] == arm]
            completion_counts = [r["usage"]["completion_tokens"] for r in sample
                                 if r["usage"].get("completion_tokens") is not None]
            decode_times = [r["usage"]["generation_duration"] for r in sample
                            if r["usage"].get("generation_duration") is not None]
            summary[name][arm] = {"median_seconds": statistics.median(r["seconds"] for r in sample),
                                 "quality_passes": sum(r["quality_pass"] for r in sample),
                                 "runs": len(sample),
                                 "median_completion_tokens": statistics.median(completion_counts) if completion_counts else None,
                                 "median_server_decode_seconds": statistics.median(decode_times) if decode_times else None,
                                 "median_content_tokens": statistics.median(r["content_tokens"] for r in sample)}
    save(output, {"before": str(before), "after": str(after), "reps": reps, "rows": rows, "summary": summary})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="mode", required=True)
    cap = subs.add_parser("capture")
    cap.add_argument("--source-root", type=Path, required=True)
    cap.add_argument("--output", type=Path, required=True)
    cmp = subs.add_parser("compare")
    cmp.add_argument("--before", type=Path, required=True)
    cmp.add_argument("--after", type=Path, required=True)
    cmp.add_argument("--output", type=Path, required=True)
    cmp.add_argument("--reps", type=int, default=3)
    args = parser.parse_args()
    if args.mode == "capture":
        asyncio.run(capture(args.source_root.resolve(), args.output.resolve()))
    else:
        asyncio.run(compare(args.before, args.after, args.output, args.reps))
