#!/usr/bin/env python
"""Model-free measure of how much of a long chat an Ask turn sends the model.

Plants the synthetic 20-exchange session from tests/ask_history_session.py in a
throwaway session store, then drives the REAL Ask turn assembly (history via
service.main._tool_turn_messages -> build_messages, the model-led system note,
_ask_tool_rank, run_agent with the real tool registry and _fit_window) against a
scripted fake client that never touches a model server. For each scenario it
captures the request the model would have been sent and reports:

  * estimated prompt tokens (tool schemas + messages, loop._est_tokens),
  * messages by role and tokens split into schemas / system / history,
  * recall: which exchanges-back still have their planted fact in the request.

Scenarios (same session, same final prompt, window 16384, Gemma 4 E4B):
  direct      one text reply; request 0
  after_tool  step 0 calls an offered read tool that returns ~6000 characters of
              synthetic text, step 1 answers; request 1

It only calls entry points the real turn calls, so it should run unchanged
before and after history-digest changes. Nothing here reads or writes the real
store: WISP_HOME points at a fresh temp dir before any service import.

    python scripts/measure_ask_history.py [--out PATH] [--window 16384]
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="wisp-measure-ask-history-")
# Both names: paths.py reads WISP_HOME; MOE_DIR is the older spelling.
os.environ["WISP_HOME"] = _TMP
os.environ["MOE_DIR"] = _TMP
os.environ["WISP_MODEL_ONLY"] = "1"

# Measure the tree you run it from (cwd), not the one the script lives in, so a
# clean export can be measured with the script kept elsewhere.
ROOT = Path(os.environ.get("WISP_MEASURE_ROOT") or os.getcwd()).resolve()
sys.path.insert(0, str(ROOT))

from service.agent import loop  # noqa: E402
from service.memory import context as memory_context  # noqa: E402
from service.memory.store import SessionStore  # noqa: E402
from service.tools import registry  # noqa: E402
from tests.ask_history_session import EXCHANGES, FINAL_PROMPT, plant, recall_depths  # noqa: E402

TOOL_RESULT_CHARS = 6000


class Client:
    """Scripted stand-in for the inference client: records requests, never calls out."""
    managed = True
    endpoint_name = "omlx"

    def __init__(self, script, window):
        self.script, self.requests, self.context_window = script, [], window
        self.target = self  # the fitter reads client.target.context_window

    async def ensure_only(self, *a, **k):
        pass

    async def stream_events(self, model, messages, **kwargs):
        step = len(self.requests)
        self.requests.append(copy.deepcopy({"messages": messages, **kwargs}))
        response = self.script[step] if step < len(self.script) else "Synthetic final answer."
        message = {"role": "assistant", "content": "", "tool_calls": None}
        if isinstance(response, list):
            message["tool_calls"] = response
        else:
            message["content"] = response
        yield {"kind": "final", "message": message}


class Approval:
    async def confirm(self, action):
        return False


def tool_call(name: str, **args) -> dict:
    return {"id": f"synthetic-{name}", "function": {"name": name, "arguments": json.dumps(args)}}


def tokens(messages: list[dict]) -> int:
    return sum(loop._est_tokens(m.get("content") or "")
               + (loop._est_tokens(m["tool_calls"]) if m.get("tool_calls") else 0)
               for m in messages)


def summarize(request: dict, label: str) -> dict:
    msgs = request["messages"]
    tools = request.get("tools") or []
    history = [m for m in msgs[:-1] if m.get("role") != "system"]
    system = [m for m in msgs if m.get("role") == "system"]
    roles: dict[str, int] = {}
    for m in msgs:
        roles[m.get("role", "?")] = roles.get(m.get("role", "?"), 0) + 1
    schema_tokens = loop._est_tokens(tools) if tools else 0
    depths = recall_depths(msgs)
    return {
        "label": label,
        "prompt_tokens": schema_tokens + tokens(msgs),
        "schema_tokens": schema_tokens,
        "system_tokens": tokens(system),
        "history_tokens": tokens(history),
        "last_message_tokens": tokens(msgs[-1:]),
        "messages": len(msgs),
        "roles": roles,
        "tools_offered": len(tools),
        "max_tokens": request.get("max_tokens"),
        "recalled": sorted(d for d, ok in depths.items() if ok),
        "recalled_count": sum(depths.values()),
        "deepest_recalled": max((d for d, ok in depths.items() if ok), default=0),
    }


def build_turn_messages(sid: str, window: int) -> list[dict]:
    """The message list /agent hands run_agent for a model-led Ask turn."""
    import service.main as main
    target = SimpleNamespace(context_window=window)
    user_msg = {"role": "user", "content": FINAL_PROMPT}
    messages = main._tool_turn_messages(sid, user_msg, max_tokens=main._history_budget(target),
                                        test_mode=False, verified_results_only=False)
    messages.insert(0, {"role": "system", "content": (
        "Conversation and remembered facts are unverified context for "
        "interpreting the request. Read current personal sources before "
        "answering about today's messages/calendar. An old assistant "
        "offer or claimed action is not an execution receipt.")})
    try:
        from service.memory.subject import system_message
        subject_msg, _ = system_message(memory_context.store, sid)
        if subject_msg:
            messages.insert(1, subject_msg)
    except Exception:  # noqa: BLE001 — informational only
        pass
    return messages


def run_turn(messages: list[dict], script, window: int) -> Client:
    import service.main as main
    client = Client(script, window)
    preload, stubs = main._ask_tool_rank(FINAL_PROMPT)

    async def emit(event):
        pass

    async def go():
        return await loop.run_agent(
            client, "synthetic-agent", copy.deepcopy(messages), emit, Approval(), tools=[],
            model_led_discovery=True, multi_round=True, ask_quality=True,
            model_led_preload=preload, model_led_stub_rank=stubs,
            include_memory_context=False, include_identity_context=False, debug=False)
    asyncio.run(go())
    return client


def pick_read_tools(request: dict) -> list:
    """Offered read tools we can call with no arguments (get_upcoming first)."""
    from service.router.model_led import READ_CATEGORIES
    offered = [s["function"] for s in request.get("tools") or [] if "description" in s["function"]]
    out = []
    for fn in sorted(offered, key=lambda f: f["name"] != "get_upcoming"):
        tool = registry.REGISTRY.get(fn["name"])
        if tool and tool.category in READ_CATEGORIES and not (fn.get("parameters") or {}).get("required"):
            out.append(tool)
    return out


def pick_read_tool(request: dict):
    """An offered read tool we can call with no arguments (prefer get_upcoming)."""
    tools = pick_read_tools(request)
    return tools[0] if tools else None


def table(rows: list[dict]) -> str:
    cols = [("scenario", "label", 26), ("prompt_tok", "prompt_tokens", 10), ("schema", "schema_tokens", 7),
            ("system", "system_tokens", 7), ("history", "history_tokens", 8), ("msgs", "messages", 5),
            ("tools", "tools_offered", 6), ("recalled/20", "recalled_count", 11), ("deepest", "deepest_recalled", 8)]
    out = ["  ".join(h.ljust(w) for h, _, w in cols)]
    for r in rows:
        out.append("  ".join(str(r[k]).ljust(w) for _, k, w in cols))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="write JSON results here")
    ap.add_argument("--window", type=int, default=16384, help="model context window (Gemma 4 E4B: 16384)")
    args = ap.parse_args()

    # The same session store build_messages reads (context.store), on the temp dir.
    memory_context.store = SessionStore(Path(_TMP) / "sessions.db")
    loop.audit = lambda *a, **k: None
    loop._BLOCKS_CACHE = None
    sid = memory_context.store.create_session()
    plant(memory_context.store, sid)

    import service
    print(f"measuring tree: {Path(service.__file__).resolve().parent.parent}")
    results = {"tree": str(Path(service.__file__).resolve().parent.parent), "window": args.window, "exchanges": len(EXCHANGES), "scenarios": {}}
    rows = []

    raw = memory_context.build_messages(sid, max_tokens=max(1500, args.window - 11500))
    raw_depths = recall_depths(raw)
    results["build_messages_raw"] = {
        "history_budget": max(1500, args.window - 11500), "messages": len(raw), "tokens": tokens(raw),
        "recalled": sorted(d for d, ok in raw_depths.items() if ok),
        "recalled_count": sum(raw_depths.values()),
        "deepest_recalled": max((d for d, ok in raw_depths.items() if ok), default=0),
    }

    turn_messages = build_turn_messages(sid, args.window)
    turn_depths = recall_depths(turn_messages)
    results["turn_messages_before_loop"] = {
        "messages": len(turn_messages), "tokens": tokens(turn_messages),
        "recalled_count": sum(turn_depths.values()),
        "deepest_recalled": max((d for d, ok in turn_depths.items() if ok), default=0),
    }

    # 1. direct
    client = run_turn(turn_messages, ["Synthetic final answer."], args.window)
    direct = summarize(client.requests[0], "direct (request 0)")
    results["scenarios"]["direct"] = direct
    rows.append(direct)

    # 2. after_tool
    tool = pick_read_tool(client.requests[0])
    if tool is None:
        results["scenarios"]["after_tool"] = {"error": "no offered read tool callable without arguments"}
    else:
        original = (tool.func, tool.unavailable_reason)
        text = ("Synthetic result line. " * 400)[:TOOL_RESULT_CHARS]

        async def fake(**kwargs):
            return text
        tool.func, tool.unavailable_reason = fake, ""
        try:
            client2 = run_turn(turn_messages, [[tool_call(tool.name)], "Synthetic final answer."], args.window)
        finally:
            tool.func, tool.unavailable_reason = original
        if len(client2.requests) < 2:
            results["scenarios"]["after_tool"] = {"error": f"only {len(client2.requests)} request(s) captured"}
        else:
            after = summarize(client2.requests[1], f"after_tool:{tool.name} (request 1)")
            after["tool_result_chars"] = TOOL_RESULT_CHARS
            # Which history messages survived, oldest first, to show what was dropped first.
            kept = [m["content"][:40] for m in client2.requests[1]["messages"] if m.get("role") in ("user", "assistant")]
            after["kept_first_user_assistant"] = kept[:3]
            results["scenarios"]["after_tool"] = after
            rows.append(after)

    # 3. after_two_tools: two distinct capped reads in one turn, then the
    # answer step (request 2). This is the step that used to drop every
    # earlier turn, the digest and the host notes.
    reads = pick_read_tools(client.requests[0])[:2]
    if len(reads) < 2:
        results["scenarios"]["after_two_tools"] = {"error": "fewer than two offered read tools callable without arguments"}
    else:
        originals = [(t.func, t.unavailable_reason) for t in reads]
        for n, t in enumerate(reads):
            body = (f"Synthetic result {n} line. " * 400)[:TOOL_RESULT_CHARS]

            async def fake(_body=body, **kwargs):
                return _body
            t.func, t.unavailable_reason = fake, ""
        try:
            client3 = run_turn(turn_messages, [[tool_call(reads[0].name)], [tool_call(reads[1].name)],
                                               "Synthetic final answer."], args.window)
        finally:
            for t, (func, reason) in zip(reads, originals):
                t.func, t.unavailable_reason = func, reason
        if len(client3.requests) < 3:
            results["scenarios"]["after_two_tools"] = {"error": f"only {len(client3.requests)} request(s) captured"}
        else:
            two = summarize(client3.requests[2], f"after_two:{reads[0].name}+{reads[1].name} (req 2)")
            two["tool_result_chars"] = TOOL_RESULT_CHARS
            two["tool_results_chars_sent"] = [len(m.get("content") or "") for m in client3.requests[2]["messages"]
                                              if m.get("role") == "tool"]
            two["request_kept"] = any(m.get("content") == FINAL_PROMPT for m in client3.requests[2]["messages"])
            results["scenarios"]["after_two_tools"] = two
            rows.append(two)

    # Where the loop cut: first surviving exchange in each request.
    for name, scenario in results["scenarios"].items():
        if "recalled" in scenario:
            scenario["dropped_oldest"] = 20 - scenario["deepest_recalled"]

    print(table(rows))
    br = results["build_messages_raw"]
    print(f"\nbuild_messages raw (budget {br['history_budget']} tok): {br['messages']} msgs, "
          f"{br['tokens']} tok, recalled {br['recalled_count']}/20, deepest {br['deepest_recalled']}")
    tm = results["turn_messages_before_loop"]
    print(f"turn messages before run_agent: {tm['messages']} msgs, {tm['tokens']} tok, "
          f"recalled {tm['recalled_count']}/20, deepest {tm['deepest_recalled']}")
    for name, s in results["scenarios"].items():
        if "recalled" in s:
            print(f"{name}: roles {s['roles']} max_tokens {s['max_tokens']} recalled {s['recalled']}")
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
