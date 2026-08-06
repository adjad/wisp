"""Wisp Air — periodic assistant node.

FastAPI on 0.0.0.0:8767. Three clients talk to it:

  * **WispAirReader.app** (localhost) pushes Mail/Messages content and polls for
    reminders to create. It exists as a separate signed .app because macOS TCC
    grants attach to a process bundle — a Python process cannot hold Mail
    Automation or Full Disk Access in any usable way.
  * **The scheduler** (in-process) fires the seven daily runs.
  * **The Pro** pulls summaries whenever it wakes up.

This is a SEPARATE service from ~/WispAir/air_service.py (port 8766, a disabled
routing experiment with known-stale rules). Do not merge them.

Run:  python3 air_periodic.py
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from wispair import config, jobs, model, readers, scheduler, store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("wispair")


def _key_fingerprint(key: str) -> str:
    """A short, non-reversible tag for the API key.

    Enough to confirm the Pro and the Air agree on which key they're using,
    without the key itself ending up in a log file.
    """
    import hashlib

    return hashlib.sha256(key.encode()).hexdigest()[:8]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup/shutdown. Uses the lifespan API rather than @app.on_event —
    the latter is deprecated and the Air pip-installs whatever FastAPI is
    current, so relying on it would be a time bomb."""
    store.init()
    key = config.api_key()
    cfg = config.load()
    task = asyncio.create_task(scheduler.loop())
    log.info("Wisp Air periodic node up on %s:%s", cfg["bind_host"], cfg["bind_port"])
    # Deliberately NOT the key itself. This service binds 0.0.0.0 and runs under
    # launchd, so its log is a long-lived file on disk and the shared secret has
    # no business being copied into it on every restart. Print the path instead;
    # `cat` it when the key is actually needed.
    log.info("API key is in %s (fingerprint %s)", config.API_KEY_PATH, _key_fingerprint(key))
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Wisp Air — Periodic Node", lifespan=lifespan)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

async def require_key(x_wisp_key: str = Header(default="")) -> None:
    """Shared-secret gate on everything except /health.

    /health stays open deliberately: it is what the user (and the Pro's
    settings pane) reach for when something is wrong, and needing the key to
    ask "are you alive?" makes debugging worse without protecting anything —
    it reports no personal content.
    """
    expected = config.api_key()
    # Constant-time-ish; the key is high-entropy so this is belt-and-braces.
    if not x_wisp_key or not _const_eq(x_wisp_key, expected):
        raise HTTPException(status_code=401, detail="bad or missing X-Wisp-Key")


def _const_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= ord(x) ^ ord(y)
    return diff == 0


# --------------------------------------------------------------------------
# Reader ingest
# --------------------------------------------------------------------------

@app.post("/sync/emails", dependencies=[Depends(require_key)])
async def sync_emails(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Reader pushes `{headers: str, diagnostics: {...}}`."""
    return _ingest("email", readers.parse_email_headers, body.get("headers", ""), body)


@app.post("/sync/messages", dependencies=[Depends(require_key)])
async def sync_messages(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Reader pushes `{lines: str, diagnostics: {...}}`."""
    return _ingest("messages", readers.parse_message_lines, body.get("lines", ""), body)


def _ingest(source: str, parse, text: str, body: dict[str, Any]) -> dict[str, Any]:
    if isinstance(body.get("diagnostics"), dict):
        store.set_diagnostic(source, body["diagnostics"])

    items = parse(text or "")
    added = store.add_items(source, items)

    # The cursor may advance as soon as items are on disk — see store.py's
    # module docstring for why that's safe here and why it is NOT the same as
    # advancing before the summary exists.
    newest = readers.newest_ts(items)
    if newest:
        store.set_cursor(source, newest)

    log.info("ingest %s: %d parsed, %d new", source, len(items), added)
    return {"ok": True, "parsed": len(items), "new": added,
            "cursor": store.get_cursor(source)}


@app.get("/cursors", dependencies=[Depends(require_key)])
async def get_cursors() -> dict[str, Any]:
    """How far back each reader needs to scan.

    `scan_from` already has the overlap subtracted, so the reader does not need
    to know the rule — it just scans from the number it's given. The overlap
    exists because some IMAP servers deliver mail with a backdated
    `date received`, which pure early-termination would skip forever.
    """
    cfg = config.load()
    overlap = cfg["reader_overlap_hours"] * 3600
    out = {}
    for source in jobs.SOURCES:
        cur = store.get_cursor(source)
        out[source] = {
            "cursor": cur,
            "scan_from": max(0.0, cur - overlap) if cur else 0.0,
        }
    return out


# --------------------------------------------------------------------------
# Reminders — the reader app polls these
# --------------------------------------------------------------------------

@app.get("/reminders/pending", dependencies=[Depends(require_key)])
async def reminders_pending() -> dict[str, Any]:
    """To-dos waiting to be written into Reminders.app.

    Polled rather than pushed. The alternative (an SSE hub, as the Pro uses) has
    to handle reconnects and missed events; for a batch node where a to-do can
    perfectly well wait 30 seconds, a poll against a durable queue is simply
    harder to get wrong — if the app is down, nothing is lost.
    """
    return {"reminders": store.pending_reminders()}


@app.post("/reminders/ack", dependencies=[Depends(require_key)])
async def reminders_ack(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """The app confirms it actually created these EKReminders."""
    ids = [int(i) for i in body.get("ids", []) if str(i).isdigit()]
    return {"ok": True, "acked": store.ack_reminders(ids)}


# --------------------------------------------------------------------------
# Summaries — the Pro pulls these
# --------------------------------------------------------------------------

@app.get("/summaries", dependencies=[Depends(require_key)])
async def get_summaries(since: float = Query(0.0),
                        include_acked: bool = Query(False)) -> dict[str, Any]:
    rows = store.summaries_since(since, include_acked=include_acked)
    return {"summaries": rows, "count": len(rows), "now": time.time()}


@app.post("/summaries/ack", dependencies=[Depends(require_key)])
async def ack_summaries(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Pro confirms receipt: `{through: <epoch>}`.

    Summaries are marked, never deleted on read — the Pro may be asleep for
    hours, and a delete-on-read would lose a summary to a single dropped
    response.
    """
    through = float(body.get("through", 0) or 0)
    return {"ok": True, "acked": store.ack_summaries(through)}


# --------------------------------------------------------------------------
# Ops
# --------------------------------------------------------------------------

@app.get("/health")
async def health(deep: bool = Query(False)) -> dict[str, Any]:
    """Open (no key). `?deep=1` also round-trips the model server.

    The deep check is opt-in because it costs a real prefill, and something
    polling /health every few seconds would evict the prompt cache — the exact
    failure mode §4.5 of the design doc warns about.
    """
    cfg = config.load()
    out: dict[str, Any] = {
        "ok": True,
        "service": "wispair-periodic",
        "now": time.time(),
        "cursors": store.all_cursors(),
        "counts": store.counts(),
        "last_run": store.last_run(),
        "next_run_ts": jobs.next_run_ts(cfg),
        "run_hours": cfg["run_hours"],
        "diagnostics": store.all_diagnostics(),
        "model": {"base_url": cfg["model_base_url"], "name": cfg["model_name"]},
    }
    if deep:
        out["model_check"] = await model.health(cfg)
    return out


@app.post("/run", dependencies=[Depends(require_key)])
async def manual_run(force: bool = Query(False)) -> dict[str, Any]:
    """Trigger a run now. `?force=1` ignores the min-items threshold."""
    return await jobs.run_all(config.load(), force=force)


@app.get("/config", dependencies=[Depends(require_key)])
async def get_config() -> dict[str, Any]:
    return config.load()


@app.post("/config", dependencies=[Depends(require_key)])
async def post_config(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    allowed = set(config.DEFAULTS)
    patch = {k: v for k, v in body.items() if k in allowed}
    if not patch:
        raise HTTPException(status_code=400, detail=f"no known keys; allowed: {sorted(allowed)}")
    return config.save(patch)


@app.exception_handler(Exception)
async def _unhandled(request, exc):        # noqa: ANN001
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)[:300]})


if __name__ == "__main__":
    cfg = config.load()
    store.init()
    print(f"API key is in {config.API_KEY_PATH}")
    uvicorn.run(app, host=cfg["bind_host"], port=cfg["bind_port"], log_level="info")
