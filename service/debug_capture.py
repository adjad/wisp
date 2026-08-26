"""Per-tool-call debug capture: lets a tool's own internal model calls (email/
message summarizers, code delegation, etc.) attach their raw source data and
raw model I/O to that specific tool call's entry in the debug export, instead
of that detail being invisible behind the tool's final return string.

Concrete motivating gap: `summarize_emails` feeds a list of raw header lines
into its own internal the summarizer call and returns only the synthesized summary —
the debug export (see loop.py's raw_model_io) captures the AGENT's request/
response, but never what the tool itself sent to/got from ITS OWN model call,
or the raw source lines that went in. Same shape for summarize_messages
(imessage_tools.py) and create_tool's prepare_draft (tool_authoring.py).

Uses a ContextVar so nested async calls made from inside `run_tool` write into
whichever capture scope is active for the tool currently executing, with zero
plumbing through every tool function's signature — a tool just calls
`debug_capture.record(...)` and it lands in the right place automatically.
Inert (a None check, no-op) when no capture is active — e.g. the scheduler's
`run_daily_email_summary()` or the profile builder call the same `_summarize`
helpers outside any agent turn, and have no debug consumer to write into.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Iterator

_records: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "_debug_capture_records", default=None)


@contextmanager
def capture() -> Iterator[list[dict[str, Any]]]:
    """Open a capture scope around one tool's execution (see loop.py). Yields
    the list `record()` appends into for the duration of the `with` block."""
    token = _records.set([])
    try:
        yield _records.get()  # type: ignore[return-value]
    finally:
        _records.reset(token)


def record(kind: str, **fields: Any) -> None:
    """Attach a debug record to whichever capture scope is currently active.
    `kind` is "source" (raw input data, e.g. {"label", "text"}) or
    "model_call" (a nested completion, e.g. {"model", "request", "response"}).
    No-op outside a capture scope."""
    lst = _records.get()
    if lst is not None:
        lst.append({"kind": kind, **fields})
