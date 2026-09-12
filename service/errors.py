"""Translates a raw exception from an agent turn into a user-facing sentence.

`run_agent`'s single top-level except clause used to hand `str(e)` straight to
the client — a dead oMLX connection, a memory-guard 400, and a KeyError all
looked the same: a raw Python/httpx string with no indication of what
happened or what to do about it. See docs/STABILITY_PLAN.md S1.

The raw exception is never discarded — every caller emits it alongside the
friendly sentence as `detail`, which the Swift client folds into the debug
export (see OverlayModel.swift's `errorDetail`) but never shows as the
headline.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Awaitable, Callable

import httpx

from service.inference.omlx_client import ModelLoadError

_RetryHook = Callable[[], Awaitable[None]] | None


def translate(exc: Exception, *, retry_omlx: _RetryHook = None) -> tuple[str, str]:
    """Returns (message shown to the user, raw detail for the debug export)."""
    detail = f"{type(exc).__name__}: {exc}"

    if isinstance(exc, ModelLoadError):
        return str(exc), detail  # already a plain, specific message

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        if retry_omlx is not None:
            # Fire-and-forget: ensure_omlx() can take up to ~90s (start, then
            # a restart fallback) — the user gets the sentence immediately and
            # a real shot at the *next* try succeeding, rather than waiting
            # out the recovery attempt inside this one failed turn.
            asyncio.create_task(retry_omlx())
        return ("The local AI engine isn't running. Wisp is trying to restart "
                "it — try again in a few seconds."), detail

    if isinstance(exc, httpx.HTTPStatusError):
        body = ""
        try:
            body = exc.response.text
        except Exception:  # noqa: BLE001 — body read is best-effort
            pass
        if "prefill_memory_exceeded" in body:
            return ("That request needed more memory than Wisp is allowed to "
                    "use. Try a shorter question or fewer attachments."), detail
        if "exceeds max context window" in body:
            # _fit_window (service/memory/context.py) exists specifically to
            # keep every request under the model's window — reaching this
            # branch means it missed a case, not that the user did anything
            # wrong. Loud because it should be unreachable.
            print(f"[errors] context-window 400 reached translate() despite "
                  f"_fit_window — budgeter has a hole: {body[:300]!r}",
                  file=sys.stderr)
            return ("That conversation got too long for the model's memory. "
                    "Starting a new chat will fix it."), detail
        return (f"The local AI engine returned an error "
                f"({exc.response.status_code}). Try again in a moment."), detail

    if isinstance(exc, httpx.TimeoutException):
        return ("That request took too long and timed out. Try again, or "
                "with a shorter question."), detail

    return ("Something went wrong on Wisp's end. Try again — if it keeps "
            "happening, check the debug export."), detail
