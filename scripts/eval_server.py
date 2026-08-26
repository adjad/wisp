#!/usr/bin/env python3
"""Web front end for the human-graded evaluation.

    python scripts/eval_server.py            # then open http://127.0.0.1:8770

Serves the prompt board and proxies each run to the real Wisp on :8765. It has
to be a local server rather than a static page: the browser cannot reach Wisp
directly, and the run needs to answer confirmation prompts mid-stream to keep
the safety gate closed.

Grades land in the SAME log the terminal runner uses (~/.moe/evals/ling_eval.jsonl),
so the two front ends share state — grade some prompts in the browser, run the
rest from the shell, one report covers both.

Safety is inherited from eval_core.stream_turn: `plan` tasks execute nothing,
and any confirmation for a tool the task did not declare is answered NO. Pass
--write to promote plan tasks to live runs; the deny gate still holds.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # noqa: E402

from scripts import eval_core as core  # noqa: E402
from scripts.eval_ling_tasks import TASKS, Task  # noqa: E402

UI_FILE = Path(__file__).resolve().parent / "eval_ui.html"
LOG_PATH = core.DEFAULT_LOG
LIVE_WRITES = False

app = FastAPI(title="Wisp eval board")
_by_id: dict[str, Task] = {t.id: t for t in TASKS}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    # Read per request so editing the HTML just needs a browser refresh.
    return UI_FILE.read_text()


@app.get("/api/state")
async def state() -> JSONResponse:
    """Everything the board needs to render: prompts, grades so far, stats."""
    model, wisp_up = "unknown", False
    try:
        async with httpx.AsyncClient() as c:
            h = (await c.get(f"{core.BASE}/health", timeout=6)).json()
        model, wisp_up = h.get("default_model", "?"), True
    except Exception:                                           # noqa: BLE001
        pass

    records = core.load_log(LOG_PATH)
    return JSONResponse({
        "model": model,
        "wisp_up": wisp_up,
        "live_writes": LIVE_WRITES,
        "log_path": str(LOG_PATH),
        "flags": core.FLAG_NAMES,
        "grade_names": core.GRADE_NAMES,
        "tasks": [asdict(t) for t in TASKS],
        "grades": records,
        "summary": core.summarize(records),
    })


@app.get("/api/run")
async def run(id: str, followup: int = 0, session_id: str = "") -> StreamingResponse:
    """Run one prompt, streaming normalized events to the browser as SSE."""
    task = _by_id.get(id)
    if task is None:
        return JSONResponse({"error": f"unknown task {id}"}, status_code=404)
    prompt = (task.followup or "") if followup else task.prompt
    if not prompt:
        return JSONResponse({"error": "task has no follow-up"}, status_code=400)

    async def gen():
        turn = core.Turn(prompt=prompt)
        try:
            async with httpx.AsyncClient() as client:
                async for ev in core.stream_turn(client, task, prompt,
                                                 session_id or None, LIVE_WRITES):
                    turn.apply(ev)
                    if ev.get("kind") == "done":
                        # Attach the mechanical checks to the terminal event so
                        # the browser never has to reimplement them.
                        ev = {**ev, "signals": core.signals(turn, task.expect),
                              "answer": turn.answer, "tools": turn.tools}
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as exc:                                # noqa: BLE001
            yield f"data: {json.dumps({'kind': 'error', 'message': str(exc)})}\n\n"
            yield f"data: {json.dumps({'kind': 'done', 'elapsed_s': 0})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/grade")
async def grade(body: dict) -> JSONResponse:
    """Persist one judgment. Appends, so the history of a redo is kept."""
    tid = body.get("id", "")
    base = _by_id.get(tid.split("::")[0])
    rec = {
        "id": tid,
        "category": base.category if base else "?",
        "mode": base.mode if base else "live",
        "model": body.get("model", "?"),
        "prompt": body.get("prompt", ""),
        "answer": body.get("answer", ""),
        "tools": body.get("tools", []),
        "route": body.get("route", {}),
        "elapsed_s": body.get("elapsed_s"),
        "error": body.get("error", ""),
        "grade": body.get("grade"),
        "flags": body.get("flags", []),
        "note": body.get("note", ""),
        "skipped": bool(body.get("skipped")),
    }
    core.append_log(rec, LOG_PATH)
    records = core.load_log(LOG_PATH)
    return JSONResponse({"ok": True, "summary": core.summarize(records)})


@app.get("/api/report")
async def report() -> JSONResponse:
    return JSONResponse(core.summarize(core.load_log(LOG_PATH)))


def main() -> int:
    global LIVE_WRITES, LOG_PATH
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--log", default=str(core.DEFAULT_LOG))
    ap.add_argument("--write", action="store_true",
                    help="run plan-mode tasks FOR REAL (sends and writes happen)")
    args = ap.parse_args()

    LIVE_WRITES = args.write
    LOG_PATH = Path(args.log)

    print(f"Eval board  →  http://127.0.0.1:{args.port}")
    print(f"Wisp        →  {core.BASE}")
    print(f"Log         →  {LOG_PATH}")
    if LIVE_WRITES:
        print("--write: plan tasks will EXECUTE. Sends and writes are real.")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
