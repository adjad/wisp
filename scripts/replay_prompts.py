#!/usr/bin/env python3
"""Replay a list of prompts against the LIVE Wisp backend and capture what it
actually did — route decision, tools called (with args and results), and the
verbatim reply.

This exists because the debug exports the user sends are point-in-time: they
show that a turn went wrong, but not whether a fix changed anything. This
replays the same prompts against the running backend so a fix can be measured
instead of assumed.

SAFETY — READ BEFORE RUNNING
    Every `confirm` event is auto-DENIED, so no message/email is ever sent and
    no destructive tool runs. Verified against ~/.moe/audit.jsonl, where each
    blocked call lands as `confirm_deny`.

    Two side effects this CANNOT prevent, both observed live:
      * `view_emails`/`summarize_emails` drive Mail.app over AppleScript, which
        LAUNCHES Mail if it isn't already running. Expect Mail to open.
      * `draft_email`/`draft_message` deliberately bypass the confirmation gate
        (a draft opens a prefilled compose window and sends nothing — the
        user's own click is the gate, see service/tools/action_tools.py's
        module docstring). Expect compose windows to open.
    Warn the user before a run that touches mail or drafts.

    Because sends are denied, the model's reply on those turns is a reaction to
    the DENIAL ("I couldn't send that", then retry flailing). That is an
    artifact of this harness, not a Wisp bug. The meaningful artifact on a send
    turn is the composed body, captured under `denied[].args`.

Usage
    .venv/bin/python scripts/replay_prompts.py PROMPTS.json [out.json]

    PROMPTS.json is [{"label": "...", "prompts": ["...", "..."]}, ...].
    Prompts inside one label share a session, so multi-turn context is kept.
"""
from __future__ import annotations

import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8765"
RESULT_CAP = 4000  # chars of each tool result to keep


def run_prompt(client: httpx.Client, prompt: str, sid: str | None) -> dict:
    out: dict = {"prompt": prompt, "route": "", "tools": [], "text": "",
                 "denied": [], "session_id": sid, "error": ""}
    deltas: list[str] = []
    body: dict = {"prompt": prompt, "debug": False}
    if sid:
        body["session_id"] = sid
    t0 = time.time()
    try:
        with client.stream("POST", f"{BASE}/agent", json=body,
                           timeout=httpx.Timeout(300.0)) as r:
            for line in r.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                ev = json.loads(line[6:])
                et = ev.get("type")
                if et == "session":
                    out["session_id"] = ev.get("id") or out["session_id"]
                elif et == "routed":
                    out["route"] = ev.get("reason", "")
                elif et == "tool_call":
                    out["tools"].append({"name": ev.get("name"), "args": ev.get("args")})
                elif et == "tool_result" and out["tools"]:
                    out["tools"][-1]["result"] = str(ev.get("result", ""))[:RESULT_CAP]
                elif et == "delta":
                    deltas.append(ev.get("text", ""))
                elif et == "clear_answer":
                    deltas.clear()
                elif et == "text":
                    out["text"] = ev.get("text", "")
                elif et == "confirm":
                    # NEVER approve. See the safety note in the module docstring.
                    out["denied"].append({"tool": ev.get("tool"), "args": ev.get("args")})
                    client.post(f"{BASE}/agent/approve", json={
                        "session_id": out["session_id"], "action_id": ev.get("id"),
                        "approved": False, "scope": "once"}, timeout=30.0)
                elif et == "error":
                    out["error"] = str(ev.get("message") or ev)
                elif et == "done":
                    break
    except Exception as exc:  # noqa: BLE001 — a dead backend must not lose prior results
        out["error"] = f"{type(exc).__name__}: {exc}"
    if not out["text"]:
        out["text"] = "".join(deltas)
    out["secs"] = round(time.time() - t0, 1)
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    spec = json.load(open(sys.argv[1]))
    outfile = sys.argv[2] if len(sys.argv) > 2 else "replay_results.json"
    results: list[dict] = []
    with httpx.Client() as client:
        try:
            client.get(f"{BASE}/health", timeout=10.0)
        except Exception:
            print(f"backend not reachable at {BASE} — start Wisp first")
            return 1
        for group in spec:
            label, prompts = group["label"], group["prompts"]
            sid = None
            for p in prompts:
                res = run_prompt(client, p, sid)
                sid = res["session_id"]
                res["label"] = label
                results.append(res)
                print(f"[{label}] ({res['secs']}s) {p[:60]!r}", flush=True)
                print(f"    route: {res['route']}", flush=True)
                print(f"    tools: {[t['name'] for t in res['tools']]}", flush=True)
                if res["denied"]:
                    print(f"    DENIED (never sent): {[d['tool'] for d in res['denied']]}",
                          flush=True)
                if res["error"]:
                    print(f"    ERROR: {res['error']}", flush=True)
                # Persist after EVERY turn: the backend died mid-run once and
                # took 9 turns of results with it.
                json.dump(results, open(outfile, "w"), indent=2)
    print(f"\nwrote {len(results)} results -> {outfile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
