"""The periodic run: summarize a window, extract to-dos, queue reminders.

One model call per source per run (so at most two), each carrying that whole
window and asking for both outputs at once — every extra call is another full
prefill on an SSD-bandwidth-bound runtime.

The response format is **delimited, not JSON**. Small models are unreliable
JSON emitters and a single parse failure would lose the entire run's work; a
line-oriented format degrades gracefully instead, since a mangled TODO line
costs one to-do rather than everything.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any

from . import config, model, store

log = logging.getLogger("wispair.jobs")

SOURCES = ("email", "messages")

SOURCE_LABEL = {
    "email": "emails received",
    "messages": "text messages exchanged",
}

# The model is told the date explicitly. Without it, relative dates ("tomorrow",
# "Friday") get guessed — a real bug that shipped on the Pro before the same fix.
SYSTEM_TEMPLATE = """You are Wisp, a private assistant summarizing the user's own {label}.
The current date and time is {now} ({tz}). Today is {weekday}.

Write a brief, factual summary and extract any concrete action items the user needs to do.

Respond in EXACTLY this format and nothing else:

SUMMARY:
<2-4 sentences covering what actually happened. Group related items. Name senders or people where it helps. If nothing needs attention, say so plainly.>

TODO: <short action title> | <ISO 8601 datetime, or the word none>
TODO: <short action title> | <ISO 8601 datetime, or the word none>

Rules for TODO lines:
- Only real, concrete actions the USER must take. No "read this newsletter", no marketing, no automated notices.
- Resolve relative dates against the current date given above and write an absolute ISO 8601 datetime (e.g. 2026-08-07T09:00:00).
- Write "none" as the datetime if no deadline is stated. Do not invent one.
- If there are no genuine action items, write no TODO lines at all.
"""

USER_TEMPLATE = """Here are the {label} from {window}:

{content}
"""


def _fmt_window(start: float, end: float) -> str:
    s = datetime.fromtimestamp(start)
    e = datetime.fromtimestamp(end)
    if s.date() == e.date():
        return f"{s:%A %B %d}, {s:%H:%M} to {e:%H:%M}"
    return f"{s:%A %B %d %H:%M} to {e:%A %B %d %H:%M}"


def build_prompt(source: str, payloads: list[str],
                 window_start: float, window_end: float) -> tuple[str, str]:
    label = SOURCE_LABEL[source]
    now = datetime.now()
    system = SYSTEM_TEMPLATE.format(
        label=label,
        now=now.strftime("%Y-%m-%d %H:%M"),
        tz=time.strftime("%Z"),
        weekday=now.strftime("%A"),
    )
    user = USER_TEMPLATE.format(
        label=label,
        window=_fmt_window(window_start, window_end),
        content="\n".join(payloads),
    )
    return system, user


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------

_TODO_RE = re.compile(r"^\s*TODO\s*:\s*(.+)$", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"^\s*SUMMARY\s*:\s*(.*)$", re.IGNORECASE)


def parse_response(text: str) -> tuple[str, list[tuple[str, float | None]]]:
    """Split a model response into (summary, [(title, due_ts|None), ...]).

    Tolerant by design. The model may omit the SUMMARY: header, wrap things in
    markdown, or emit a TODO line before the summary; none of that should cost
    the run. Anything that isn't a recognizable TODO line is treated as summary
    prose.
    """
    summary_lines: list[str] = []
    todos: list[tuple[str, float | None]] = []
    seen_summary_header = False

    for raw in text.splitlines():
        line = raw.rstrip()
        m = _TODO_RE.match(line)
        if m:
            parsed = _parse_todo(m.group(1))
            if parsed:
                todos.append(parsed)
            continue
        m = _SUMMARY_RE.match(line)
        if m:
            seen_summary_header = True
            if m.group(1).strip():
                summary_lines.append(m.group(1).strip())
            continue
        # Strip markdown fences the model sometimes wraps the block in.
        if line.strip().startswith("```"):
            continue
        summary_lines.append(line)

    summary = "\n".join(summary_lines).strip()
    # Collapse the blank lines left behind by removed TODO/fence lines.
    summary = re.sub(r"\n{3,}", "\n\n", summary)
    if not summary and not seen_summary_header:
        summary = text.strip()
    return summary, todos


def _parse_todo(body: str) -> tuple[str, float | None] | None:
    """`<title> | <ISO datetime or none>` → (title, due_ts)."""
    if "|" in body:
        title, _, when = body.rpartition("|")
    else:
        # The model dropped the separator. Keep the to-do rather than the
        # deadline — an undated reminder still reaches the user.
        title, when = body, "none"
    title = title.strip().strip("*_`").strip()
    if not title:
        return None
    return title, _parse_when(when.strip())


def _parse_when(when: str) -> float | None:
    w = when.strip().strip("*_`").strip()
    if not w or w.lower() in {"none", "n/a", "null", "-", "no deadline", "unknown"}:
        return None
    # Normalize the shapes models actually emit around a strict ISO string.
    w = w.replace("Z", "+00:00")
    for candidate in (w, w.replace(" ", "T"), w.split()[0]):
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        # A date with no time means "that day" — 9am is a more useful default
        # for a reminder alarm than midnight, which fires while asleep.
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and "T" not in candidate:
            dt = dt.replace(hour=9)
        ts = dt.timestamp()
        # Reject the past and the absurd. A model that misreads the year emits
        # something like 2024 or 2124; neither should become a real alarm.
        now = time.time()
        if ts < now - 86400 or ts > now + 365 * 86400:
            return None
        return ts
    return None


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

async def run_source(cfg: dict[str, Any], source: str, force: bool = False) -> dict[str, Any]:
    """Summarize one source's pending window. Returns a per-source result dict."""
    items = store.pending_items(source, cfg["max_items_per_call"])
    if not items:
        return {"source": source, "status": "empty", "items": 0}

    # Skip-empty-windows: don't burn a full prefill on two routine emails.
    # The items stay pending and roll into the next run — unless they've been
    # waiting too long, in which case summarize whatever there is, because
    # otherwise a quiet stretch would silently produce nothing at all.
    oldest = min(float(i["ts"]) for i in items)
    waited_h = (time.time() - oldest) / 3600
    if (not force
            and len(items) < cfg["min_items_to_summarize"]
            and waited_h < cfg["max_rollover_hours"]):
        return {
            "source": source,
            "status": "rolled_over",
            "items": len(items),
            "waited_hours": round(waited_h, 1),
        }

    window_start = oldest
    window_end = max(float(i["ts"]) for i in items)
    system, user = build_prompt(source, [str(i["payload"]) for i in items],
                                window_start, window_end)

    try:
        raw = await model.complete(cfg, system, user)
    except model.ModelError as e:
        # Items stay pending — the next run retries them. This is exactly the
        # case the durable item store exists for.
        log.warning("run_source(%s): model call failed: %s", source, e)
        return {"source": source, "status": "model_error", "items": len(items),
                "error": str(e)[:300]}

    summary, todos = parse_response(raw)
    if not summary:
        log.warning("run_source(%s): empty summary from model", source)
        return {"source": source, "status": "empty_summary", "items": len(items)}

    # Commit the summary BEFORE marking items summarized, so a crash in between
    # costs a duplicate summary (harmless, visible) rather than a silent hole.
    store.add_summary(source, summary, window_start, window_end, len(items))
    store.mark_summarized([int(i["id"]) for i in items])

    queued = 0
    for title, due_ts in todos:
        if store.enqueue_reminder(title, due_ts, source=source):
            queued += 1

    store.set_diagnostic(f"{source}_last_summary", {
        "ts": time.time(), "items": len(items), "todos": len(todos), "queued": queued,
    })
    return {
        "source": source,
        "status": "summarized",
        "items": len(items),
        "todos_found": len(todos),
        "reminders_queued": queued,
    }


async def run_all(cfg: dict[str, Any], force: bool = False) -> dict[str, Any]:
    """One full run: every source, strictly sequentially.

    Sequential is not incidental — concurrent requests evict the single-entry
    prompt cache and force full re-prefills. Do not turn this into a gather().
    """
    run_id = store.start_run()
    results = []
    ok = True
    for source in SOURCES:
        try:
            res = await run_source(cfg, source, force=force)
        except Exception as e:                       # noqa: BLE001
            # One source failing must not abort the other. Mail Automation can
            # be revoked independently of Full Disk Access, and losing both
            # summaries because of one broken grant would be a bad trade.
            log.exception("run_source(%s) crashed", source)
            res = {"source": source, "status": "error", "error": str(e)[:300]}
        if res.get("status") in {"model_error", "error", "empty_summary"}:
            ok = False
        results.append(res)

    detail = "; ".join(f"{r['source']}={r['status']}" for r in results)
    store.end_run(run_id, ok, detail)

    # Cheap housekeeping while we're already awake and nothing else is running.
    store.prune_items()
    store.prune_summaries()

    return {"run_id": run_id, "ok": ok, "results": results, "ts": time.time()}


def next_run_ts(cfg: dict[str, Any], now: float | None = None) -> float:
    """Next scheduled wall-clock run time, as a unix timestamp."""
    now = now if now is not None else time.time()
    dt = datetime.fromtimestamp(now)
    hours = sorted(int(h) for h in cfg["run_hours"])
    for h in hours:
        cand = dt.replace(hour=h, minute=0, second=0, microsecond=0)
        if cand.timestamp() > now:
            return cand.timestamp()
    # Past the last run of the day — first run of tomorrow.
    tomorrow = (dt + timedelta(days=1)).replace(
        hour=hours[0], minute=0, second=0, microsecond=0)
    return tomorrow.timestamp()
