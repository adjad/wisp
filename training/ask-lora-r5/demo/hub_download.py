"""One real Gemma-Wisp download through Wisp's setup path, on an ISOLATED backend (8775).

Driven by hub_download.sh, which starts the backend with a throwaway WISP_HOME and the
built-in engine. Steps: setup offers -> start engine -> download -> wait -> re-hash every
file against the manifest -> setup status -> apply -> one chat turn (cards auto-denied).
Prints one PASS/FAIL line per step and writes a JSON report. Never touches 8765 or :8000.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8775"
MODEL = "Gemma-Wisp"
KEPT = ("gemma-4-E4B-it-oQ4e-mtp", "gemma-4-E4B-it-r5-step-01800-oQ4e-mtp")
ALLOWED_EXTRA = {"README.md"}


def manifest(path: Path) -> dict[str, tuple[str, int]]:
    out = {}
    for line in path.read_text().splitlines():
        if line.strip():
            sha, size, name = line.split()
            out[name] = (sha, int(size))
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True, type=Path)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--wait-min", type=float, default=45)
    a = ap.parse_args()
    report: dict = {"steps": []}
    ok_all = True

    def step(name: str, ok: bool, **info):
        nonlocal ok_all
        ok_all &= ok
        report["steps"].append({"step": name, "pass": ok, **info})
        print(f"{'PASS' if ok else 'FAIL'} {name} {json.dumps(info)[:400]}", flush=True)
        a.out.write_text(json.dumps(report, indent=2))
        return ok

    c = httpx.Client(base_url=BASE, timeout=60)
    st = c.get("/setup/status").json()
    offers = json.dumps(st)
    step("setup offers Gemma-Wisp", MODEL in offers,
         base_offered=any(f'"{k}"' in offers for k in KEPT[:1]))

    r = c.post("/engine/start", json={})
    if not step("built-in engine starts", r.status_code == 200, status=r.status_code, body=r.text[:200]):
        return 1

    r = c.post("/engine/downloads", json={"model": MODEL}, timeout=200)
    if not step("download accepted", r.status_code == 200, status=r.status_code, body=r.text[:300]):
        return 1

    t0, task, last = time.time(), None, ""
    while time.time() - t0 < a.wait_min * 60:
        tasks = c.get("/engine/downloads").json().get("tasks", [])
        task = next((t for t in tasks if t.get("model") == MODEL), None)
        line = json.dumps({k: (task or {}).get(k) for k in ("state", "progress", "reason")})
        if line != last:
            print(f"  {round(time.time() - t0)}s {line}", flush=True)
            last = line
        if task and task.get("state") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(10)
    minutes = round((time.time() - t0) / 60, 1)
    if not step("download completes and passes Wisp's check", bool(task) and task.get("state") == "completed",
                minutes=minutes, task={k: (task or {}).get(k) for k in ("state", "reason", "message")}):
        return 1

    folder = a.home / "engine" / "models" / "Neo-Arid" / "Gemma-Wisp"
    want = manifest(a.manifest)
    present = sorted(p.name for p in folder.iterdir() if not p.name.startswith("."))
    bad = []
    for name, (sha, size) in want.items():
        p = folder / name
        if not p.is_file():
            bad.append(f"{name}: missing")
        elif p.stat().st_size != size or sha256(p) != sha:
            bad.append(f"{name}: differs")
    extra = [n for n in present if n not in want and n not in ALLOWED_EXTRA]
    step("downloaded files match the manifest (sha256)", not bad and not extra,
         files=len(present), bad=bad, extra=extra)

    st = c.get("/setup/status").json()
    step("setup lists it as installed", MODEL in json.dumps(st.get("installed", st)))

    t1 = time.time()
    r = c.post("/setup/apply", json={"model": MODEL}, timeout=120)
    step("setup apply", r.status_code == 200, status=r.status_code, seconds=round(time.time() - t1, 1),
         body="" if r.status_code == 200 else r.text[:300])

    reply, tools, error, t2 = [], [], "", time.time()
    with c.stream("POST", "/agent", json={"prompt": "In one sentence, what can you help me with?",
                                          "debug": False}, timeout=300) as s:
        sid = None
        for raw in s.iter_lines():
            if not raw.startswith("data: "):
                continue
            ev = json.loads(raw[6:])
            kind = ev.get("type")
            if kind == "session":
                sid = ev.get("id") or sid
            elif kind == "delta":
                reply.append(ev.get("text", ""))
            elif kind == "clear_answer":
                reply.clear()
            elif kind == "text":
                reply = [ev.get("text", "")]
            elif kind == "tool_call":
                tools.append(ev.get("name"))
            elif kind == "confirm":   # never approve
                c.post("/agent/approve", json={"session_id": sid, "action_id": ev.get("id"),
                                               "approved": False, "scope": "once"})
            elif kind == "error":
                error = str(ev.get("message") or ev)[:300]
            elif kind == "done":
                break
    text = "".join(reply).strip()
    step("one chat turn on the downloaded model", bool(text) and not error,
         seconds=round(time.time() - t2, 1), reply=text[:300], tools=tools, error=error)
    report["pass"] = ok_all
    a.out.write_text(json.dumps(report, indent=2))
    print("OVERALL", "PASS" if ok_all else "FAIL", flush=True)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
