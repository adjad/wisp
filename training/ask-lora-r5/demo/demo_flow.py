#!/usr/bin/env python3
"""Demo-flow check for ONE model against an ISOLATED Wisp backend (port 8775 only).

Called by demo_flow.sh once per model. Never talks to 8765 / 8000. Every `confirm`
event is auto-DENIED (logic copied from scripts/replay_prompts.py), so nothing is
sent, created or deleted.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time

import httpx

REPLY_CAP = 1500
RESULT_CAP = 600
SLOW_S = 30.0
# A tool that was actually executed (not denied at the confirm gate) matching this is a failure.
RISKY = re.compile(r"(^|_)(send|delete|remove|trash|erase|forget|cancel|purge|empty|reply|forward)(_|$)", re.I)

ASK_PROMPTS = [
    ("greeting", "good morning wisp"),
    ("calendar", "What's on my calendar tomorrow?"),
    ("messages", "What did Mom text me?"),
    ("compose", "Write a short thank-you note to my professor"),
    ("chat", "Why is the sky blue?"),
    ("help", "How do I connect my Google account to you?"),
    ("plan", "What should I do today?"),
    ("mail", "What are my most recent emails about?"),
    ("messages2", "Did anyone text me today?"),
    ("reminders_read", "What reminders do I have today?"),
    ("reminders_write", "remind me to call the dentist tomorrow at 10"),
    ("files", "find my resume"),
]


def refuse_bad_url(base: str) -> None:
    m = re.fullmatch(r"http://127\.0\.0\.1:(\d+)", base)
    if not m or m.group(1) != "8775":
        sys.exit(f"refusing: backend must be http://127.0.0.1:8775, got {base!r}")


def stream_agent(client: httpx.Client, base: str, body: dict) -> dict:
    out = {"http_status": None, "reply": "", "tools": [], "denied": [], "route": "", "error": ""}
    deltas: list[str] = []
    sid = None
    t0 = time.time()
    try:
        with client.stream("POST", f"{base}/agent", json=body, timeout=httpx.Timeout(300.0)) as r:
            out["http_status"] = r.status_code
            if r.status_code != 200:
                out["error"] = r.read().decode("utf-8", "replace")[:300]
            else:
                for line in r.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    ev = json.loads(line[6:])
                    et = ev.get("type")
                    if et == "session":
                        sid = ev.get("id") or sid
                    elif et == "routed":
                        out["route"] = ev.get("reason", "")
                    elif et == "tool_call":
                        out["tools"].append({"name": ev.get("name"), "denied": False})
                    elif et == "tool_result" and out["tools"]:
                        out["tools"][-1]["result"] = str(ev.get("result", ""))[:RESULT_CAP]
                    elif et == "delta":
                        deltas.append(ev.get("text", ""))
                    elif et == "clear_answer":
                        deltas.clear()
                    elif et == "text":
                        out["reply"] = ev.get("text", "")
                    elif et == "confirm":
                        # NEVER approve.
                        out["denied"].append(ev.get("tool"))
                        for t in reversed(out["tools"]):
                            if t["name"] == ev.get("tool") and not t["denied"]:
                                t["denied"] = True
                                break
                        client.post(f"{base}/agent/approve", json={
                            "session_id": sid, "action_id": ev.get("id"),
                            "approved": False, "scope": "once"}, timeout=30.0)
                    elif et == "error":
                        out["error"] = str(ev.get("message") or ev)[:300]
                    elif et == "done":
                        break
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    if not out["reply"]:
        out["reply"] = "".join(deltas)
    out["seconds"] = round(time.time() - t0, 1)
    return out


def agent_step(client, base, name, prompt, extra=None) -> dict:
    body = {"prompt": prompt, "debug": False, **(extra or {})}
    r = stream_agent(client, base, body)
    reply = r["reply"].strip()
    risky = [t["name"] for t in r["tools"] if RISKY.search(t["name"] or "") and not t["denied"]]
    return {
        "step": name, "route": "/agent", "prompt": prompt,
        "http_status": r["http_status"], "seconds": r["seconds"],
        "reply": reply[:REPLY_CAP], "reply_chars": len(reply),
        "tools_called": [{"name": t["name"], "denied": t["denied"]} for t in r["tools"]],
        "denied_confirmations": r["denied"], "router_reason": r["route"], "error": r["error"],
        "pass": {"non_empty_reply": bool(reply),
                 "no_send_delete_executed": not risky,
                 "under_30s": r["seconds"] < SLOW_S},
        "risky_executed": risky,
    }


def get_step(client, base, name, method, path, params=None, json_body=None) -> dict:
    t0 = time.time()
    status, data, err = None, None, ""
    try:
        r = client.request(method, f"{base}{path}", params=params, json=json_body, timeout=180.0)
        status = r.status_code
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = r.text[:300]
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    secs = round(time.time() - t0, 1)
    text = json.dumps(data, ensure_ascii=False) if data is not None else ""
    return {
        "step": name, "route": f"{method} {path}", "params": params, "http_status": status,
        "seconds": secs, "reply": text[:REPLY_CAP], "reply_chars": len(text),
        "keys": sorted(data.keys()) if isinstance(data, dict) else None,
        "tools_called": [], "error": err,
        "pass": {"non_empty_reply": status == 200 and bool(data) and data != {} and data != [],
                 "no_send_delete_executed": True, "under_30s": secs < SLOW_S},
        "_data": data,
    }


def local_tz() -> str:
    p = os.path.realpath("/etc/localtime")
    return p.split("zoneinfo/", 1)[1] if "zoneinfo/" in p else "UTC"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", default="http://127.0.0.1:8775")
    ap.add_argument("--out", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--image-prompt", default="What does this screen say, and what should I do about it?")
    ap.add_argument("--wav", required=True)
    ap.add_argument("--analyze", action="store_true", help="also tap 'Analyze now' and poll the Brief")
    a = ap.parse_args()
    base = a.base_url.rstrip("/")
    refuse_bad_url(base)
    steps: list[dict] = []
    with httpx.Client() as c:
        try:
            c.get(f"{base}/health", timeout=10.0).raise_for_status()
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"isolated backend not reachable at {base}: {exc}")
        # 1. Ask with tools. Each prompt is its own session.
        for name, prompt in ASK_PROMPTS:
            s = agent_step(c, base, f"ask:{name}", prompt)
            steps.append(s)
            print(f"[{a.model}] ask:{name} {s['seconds']}s tools={[t['name'] for t in s['tools_called']]}", flush=True)
        # 2. Brief and Day.
        if a.analyze:
            before = c.get(f"{base}/assistant/proactive", timeout=60).text
            st = get_step(c, base, "brief:analyze_now", "POST", "/assistant/proactive/analyze", json_body={})
            steps.append(st)
            t0 = time.time()
            while time.time() - t0 < 120:
                time.sleep(5)
                if c.get(f"{base}/assistant/proactive", timeout=60).text != before:
                    break
            st["note"] = f"snapshot changed after {round(time.time()-t0)}s" if time.time() - t0 < 120 else "snapshot did not change in 120s"
        steps.append(get_step(c, base, "brief:proactive_snapshot", "GET", "/assistant/proactive"))
        steps.append(get_step(c, base, "brief:daily_summary_build", "POST", "/assistant/daily_summary", json_body={}))
        steps.append(get_step(c, base, "brief:daily_summary_latest", "GET", "/assistant/daily_summary/latest"))
        steps.append(get_step(c, base, "day:today_agenda", "GET", "/assistant/today",
                              params={"day": dt.date.today().isoformat(), "timezone": local_tz()}))
        # 3. Look at screen (vision turn through /agent with `image`).
        with open(a.image, "rb") as f:
            img = "data:image/png;base64," + base64.b64encode(f.read()).decode()
        s = agent_step(c, base, "vision:screen", a.image_prompt,
                       {"image": img, "image_source": "screen", "image_label": "Mail - Message too large"})
        s["image"] = os.path.basename(a.image)
        steps.append(s)
        # 4. Voice.
        with open(a.wav, "rb") as f:
            wav = base64.b64encode(f.read()).decode()
        v = get_step(c, base, "voice:transcribe", "POST", "/voice/transcribe", json_body={"audio": wav})
        v["wav"] = os.path.basename(a.wav)
        if isinstance(v.get("_data"), dict):
            v["reply"] = str(v["_data"].get("text") or v["_data"].get("detail") or "")[:REPLY_CAP]
            v["pass"]["non_empty_reply"] = v["http_status"] == 200 and bool(v["reply"].strip())
        steps.append(v)
    for s in steps:
        s.pop("_data", None)
    total = sum(1 for s in steps for v in s["pass"].values())
    ok = sum(1 for s in steps for v in s["pass"].values() if v)
    doc = {"model": a.model, "backend": base, "started": dt.datetime.now().isoformat(timespec="seconds"),
           "checks_passed": ok, "checks_total": total, "steps": steps}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    print(f"wrote {a.out}: {ok}/{total} checks", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
