"""Shared machinery for the human-graded evaluation.

Both front ends — the terminal runner (`eval_ling.py`) and the web UI
(`eval_server.py`) — drive Wisp through this module, so a prompt behaves
identically whichever one you use and there is exactly one place where the SSE
contract, the safety gate and the auto-signals live.

The unit of work is `stream_turn`, an async generator of NORMALIZED events. The
terminal collects them into a `Turn` and prints once; the web server forwards
them to the browser as they arrive. Neither knows Wisp's raw event shapes.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

import httpx

from scripts.eval_ling_tasks import Task

BASE = "http://127.0.0.1:8765"
LOG_DIR = Path.home() / ".moe" / "evals"
DEFAULT_LOG = LOG_DIR / "ling_eval.jsonl"

# Per-prompt ceiling. A news fetch plus synthesis can legitimately run long;
# past this it is the stall we want recorded as a failure rather than a hang.
TURN_TIMEOUT = 180.0

GRADE_NAMES = {1: "broken", 2: "poor", 3: "good", 4: "excellent"}
FLAG_NAMES = [
    "hallucination", "wrong-tool", "attribution", "format-ignored",
    "verbose", "thinking-leak", "slow", "error", "unhelpful", "refused-wrongly",
]

# ---------------------------------------------------------------- signals
# Chain-of-thought that escaped into the answer. A thinking block that hits the
# token ceiling before closing comes back as ordinary content with NO <think>
# tag left to strip, so tag-matching alone misses it — these are the openers
# such a monologue actually starts with.
_LEAK_RE = re.compile(
    r"^\s*(?:<think>|okay,?\s+(?:so\s+)?the user|the user (?:wants|is asking|said)|"
    r"let me (?:analyze|think|start|see)|first,? i (?:need|should)|"
    r"i need to (?:figure|determine|analyze))", re.I)

# A tool reporting its own failure, anchored to the leading "(" those use.
_TOOL_ERR_RE = re.compile(r"\((?:could not|couldn't|unknown tool|unable to|failed|"
                          r".{0,24}\berror\b)", re.I)

# A home directory invented from the account name rather than read from the OS.
_BAD_PATH_RE = re.compile(r"/Users/(?!adijain\b)[a-z0-9._-]{2,}", re.I)

_HEDGE_RE = re.compile(r"\b(?:i (?:can'?t|cannot|don'?t have|am unable)|"
                       r"i'?m not able|no access|i don'?t know)\b", re.I)


@dataclass
class Turn:
    """Everything one prompt produced, before a human looks at it."""
    prompt: str = ""
    answer: str = ""
    reasoning: str = ""
    route: dict = field(default_factory=dict)
    tools: list[dict] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    session_id: str | None = None
    first_token_s: float | None = None
    elapsed_s: float = 0.0
    error: str = ""

    def apply(self, ev: dict) -> None:
        """Fold one normalized event in. Mirrors what the browser does in JS."""
        k = ev.get("kind")
        if k == "session":
            self.session_id = ev.get("id") or self.session_id
        elif k == "route":
            # Preserve the machine-checkable execution contract.  The older
            # four-field projection was sufficient for prose-quality evals,
            # but made compound dry runs look incomplete whenever synthetic
            # source data correctly prevented a downstream action call.
            self.route = {x: ev[x] for x in (
                "role", "model", "needs_tools", "reason", "source", "route_source",
                "direct_calls", "required_tool_groups", "forbidden_tools",
                "conditional_tools",
            ) if x in ev}
        elif k == "delta":
            self.answer += ev.get("text", "")
        elif k == "clear":
            self.answer = ""
        elif k == "answer":
            self.answer = ev.get("text", "") or self.answer
        elif k == "reasoning":
            self.reasoning = ev.get("text", "")
        elif k == "tool_call":
            self.tools.append({"id": ev.get("id", ""), "name": ev.get("name", "?"),
                               "args": ev.get("args") or {}, "result": ""})
        elif k == "tool_result":
            for t in self.tools:
                if t["id"] == ev.get("id"):
                    t["result"] = ev.get("result", "")
                    break
        elif k == "denied":
            self.denied.append(ev.get("name", "?"))
        elif k == "error":
            self.error = ev.get("message", "error")
        elif k == "done":
            self.elapsed_s = ev.get("elapsed_s", self.elapsed_s)
            self.first_token_s = ev.get("first_token_s", self.first_token_s)


def signals(turn: Turn, expect: tuple[str, ...] | list[str]) -> list[dict]:
    """Cheap mechanical checks, so a grader spends attention on judgment calls
    and not on things a regex can see. These NEVER set a score."""
    out: list[dict] = []

    def add(level: str, text: str) -> None:
        out.append({"level": level, "text": text})

    if turn.error:
        add("bad", f"error: {turn.error}")
    if not turn.answer.strip():
        add("bad", "empty answer")
    if _LEAK_RE.search(turn.answer):
        add("bad", "chain-of-thought leaked into answer")
    if _BAD_PATH_RE.search(turn.answer + json.dumps(turn.tools)):
        add("bad", "invented home directory")
    for t in turn.tools:
        if _TOOL_ERR_RE.search(str(t.get("result", ""))):
            add("warn", f"tool {t['name']} returned an error")

    names = [t["name"] for t in turn.tools]
    expect = tuple(expect or ())
    if expect and not names:
        add("warn", f"no tool called (expected {'/'.join(expect)})")
    elif expect and not (set(names) & set(expect)):
        add("warn", f"tool mismatch: called {', '.join(names)}, "
                    f"expected {'/'.join(expect)}")
    elif not expect and names:
        add("warn", f"called {', '.join(names)} for a no-tool prompt")

    if turn.denied:
        add("info", f"auto-denied: {', '.join(turn.denied)}")
    if _HEDGE_RE.search(turn.answer):
        add("info", "states a limitation — check it is the RIGHT one")
    if turn.elapsed_s > 60:
        add("warn", f"slow: {turn.elapsed_s:.0f}s")
    return out


# ---------------------------------------------------------------- running
async def stream_turn(client: httpx.AsyncClient, task: Task, prompt: str,
                      session_id: str | None = None,
                      live_writes: bool = False) -> AsyncIterator[dict]:
    """Drive one prompt through Wisp's real /agent endpoint.

    Yields normalized events. Safety: a task marked `plan` goes through
    test_mode (tool calls are intercepted before executing), and any
    confirmation for a tool outside the task's own expect list is answered NO
    — so even live_writes cannot wander outside what the task declared.
    """
    plan_mode = task.mode == "plan" and not live_writes
    body: dict = {"prompt": prompt}
    if plan_mode:
        body["test_mode"] = True          # stateless by design; carries no session
    elif session_id:
        body["session_id"] = session_id

    started = time.monotonic()
    first_token: float | None = None
    yield {"kind": "start", "prompt": prompt, "plan_mode": plan_mode}

    try:
        async with client.stream("POST", f"{BASE}/agent", json=body,
                                 timeout=TURN_TIMEOUT) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                ev = json.loads(line[6:])
                kind = ev.get("type", "?")

                if kind == "session":
                    session_id = ev.get("id") or session_id
                    yield {"kind": "session", "id": session_id}
                elif kind == "routed":
                    yield {"kind": "route",
                           **{x: ev[x] for x in
                              ("role", "model", "needs_tools", "reason", "source",
                               "route_source", "direct_calls", "required_tool_groups",
                               "forbidden_tools", "conditional_tools") if x in ev}}
                elif kind == "delta":
                    if first_token is None:
                        first_token = time.monotonic() - started
                    yield {"kind": "delta", "text": ev.get("text", "")}
                elif kind == "clear_answer":
                    # The loop discarded what it streamed for that step. Drop it
                    # here too, or the graded answer is text the user never saw.
                    yield {"kind": "clear"}
                elif kind == "text":
                    if first_token is None:
                        first_token = time.monotonic() - started
                    yield {"kind": "answer", "text": ev.get("text", "")}
                elif kind == "reasoning":
                    yield {"kind": "reasoning", "text": ev.get("text", "")}
                elif kind == "tool_call":
                    # Keyed by call id, not name: tool_result carries only `id`,
                    # and one turn can call the same tool twice (two fetches for
                    # a comparison).
                    yield {"kind": "tool_call", "id": ev.get("id", ""),
                           "name": ev.get("name", "?"), "args": ev.get("args") or {}}
                elif kind == "tool_result":
                    yield {"kind": "tool_result", "id": ev.get("id", ""),
                           "result": str(ev.get("result", ""))[:1500]}
                elif kind == "heartbeat":
                    yield {"kind": "heartbeat",
                           "elapsed_s": round(time.monotonic() - started, 1)}
                elif kind == "confirm":
                    # The approver spreads the action dict at the top level, so
                    # `tool` is the tool name.
                    name = ev.get("tool") or ev.get("name", "?")
                    ok = live_writes and name in task.expect
                    if not ok:
                        yield {"kind": "denied", "name": name}
                    await client.post(f"{BASE}/agent/approve", json={
                        "session_id": session_id or "", "action_id": ev.get("id"),
                        "approved": bool(ok), "scope": "once"}, timeout=20)
                elif kind == "error":
                    yield {"kind": "error",
                           "message": str(ev.get("message") or ev.get("error") or "error")}
                elif kind == "done":
                    break
    except httpx.ReadTimeout:
        yield {"kind": "error", "message": f"timed out after {TURN_TIMEOUT:.0f}s"}
    except Exception as exc:                                    # noqa: BLE001
        yield {"kind": "error", "message": f"{type(exc).__name__}: {exc}"}

    yield {"kind": "done",
           "elapsed_s": round(time.monotonic() - started, 1),
           "first_token_s": round(first_token, 1) if first_token is not None else None,
           "session_id": session_id}


async def run_turn(client: httpx.AsyncClient, task: Task, prompt: str,
                   session_id: str | None = None,
                   live_writes: bool = False) -> Turn:
    """Collect a whole turn — the terminal runner's view of stream_turn."""
    turn = Turn(prompt=prompt, session_id=session_id)
    async for ev in stream_turn(client, task, prompt, session_id, live_writes):
        turn.apply(ev)
    return turn


# ---------------------------------------------------------------- log
def load_log(path: Path = DEFAULT_LOG) -> dict[str, dict]:
    """Grades keyed by task id. A later record wins, so a redo overwrites."""
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[rec["id"]] = rec
    return out


def append_log(rec: dict, path: Path = DEFAULT_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def summarize(records: dict[str, dict]) -> dict:
    """Aggregate stats — the same numbers the CLI report and the web header show."""
    graded = [r for r in records.values() if r.get("grade")]
    if not graded:
        return {"n": 0, "mean": None, "dist": {}, "by_category": {},
                "flags": {}, "latency": {}, "needs_work": []}

    scores = [r["grade"] for r in graded]
    cats: dict[str, list[int]] = {}
    flags: dict[str, int] = {}
    for r in graded:
        cats.setdefault(r.get("category", "?"), []).append(r["grade"])
        for f in r.get("flags", []):
            flags[f] = flags.get(f, 0) + 1

    lat = sorted(r["elapsed_s"] for r in graded if r.get("elapsed_s"))
    return {
        "n": len(graded),
        "mean": round(sum(scores) / len(scores), 2),
        "dist": {g: scores.count(g) for g in (4, 3, 2, 1)},
        "by_category": {c: round(sum(v) / len(v), 2) for c, v in sorted(
            cats.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))},
        "category_n": {c: len(v) for c, v in cats.items()},
        "flags": dict(sorted(flags.items(), key=lambda kv: -kv[1])),
        "latency": {"median": lat[len(lat) // 2], "max": lat[-1]} if lat else {},
        "needs_work": sorted(
            [{"id": r["id"], "grade": r["grade"], "flags": r.get("flags", []),
              "note": r.get("note", "")} for r in graded if r["grade"] <= 2],
            key=lambda r: r["grade"]),
    }
