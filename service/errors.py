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

from service.inference.omlx_client import (
    REQUEST_MEMORY_MESSAGE,
    IncompleteStreamError,
    ModelLoadError,
    SanitizedHTTPStatusError,
)

_RetryHook = Callable[[], Awaitable[None]] | None

# Where the cloud key is entered again (SettingsView's Models page, Cloud inference).
RECONNECT_KEY_MESSAGE = ("Reconnect your cloud AI key: Wisp can't find it in Keychain. Open Settings › Models, "
                         "paste your key under Cloud inference and choose Test & Save.")
UNREADABLE_KEY_MESSAGE = ("Wisp couldn't read your cloud AI key from Keychain. If macOS asks, enter "
                          "your login password and choose Always Allow. If it keeps happening, "
                          "reconnect your key: Settings › Models, paste it under Cloud inference "
                          "and choose Test & Save.")
BUSY_KEY_MESSAGE = "Wisp is still reading your cloud AI key from Keychain. Try again in a moment."
WAITED_KEY_MESSAGE = ("Wisp waited for you to allow Keychain access, but no answer came in time. "
                      "Ask again, then enter your login password and choose Always Allow when macOS asks.")
COPY_PENDING_KEY_MESSAGE = ("Wisp still needs to bring your cloud AI key across in Keychain. Ask again, "
                            "and allow Keychain access when macOS asks.")
# The status line Ask shows while macOS's question is on screen.
KEYCHAIN_WAIT_STATUS = "Waiting for you to allow Keychain access…"


def _credential_failure(exc: BaseException):
    """The provider-key failure behind exc (raised directly, or wrapped by a caller)."""
    from service.config.endpoints import ProviderCredentialError
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, ProviderCredentialError):
            return exc
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        seen += 1
    return None


def credential_message(exc: BaseException) -> str | None:
    """The reconnect sentence for a provider-key failure, else None."""
    from service.config.endpoints import (ProviderCredentialBusy, ProviderCredentialCopyPending,
                                          ProviderCredentialMissing, ProviderCredentialTimedOut)
    found = _credential_failure(exc)
    if found is None:
        return None
    for kind, message in ((ProviderCredentialMissing, RECONNECT_KEY_MESSAGE),
                          (ProviderCredentialBusy, BUSY_KEY_MESSAGE),
                          (ProviderCredentialTimedOut, WAITED_KEY_MESSAGE),
                          (ProviderCredentialCopyPending, COPY_PENDING_KEY_MESSAGE)):
        if isinstance(found, kind):
            return message
    return UNREADABLE_KEY_MESSAGE


def startup_credential_hint(exc: BaseException) -> str | None:
    """The startup log's line for a key problem: only missing or unreadable keys. A key
    waiting to be copied (or a busy read) is fine and says nothing."""
    from service.config.endpoints import (ProviderCredentialBusy, ProviderCredentialCopyPending,
                                          ProviderCredentialTimedOut)
    found = _credential_failure(exc)
    if found is None or isinstance(found, (ProviderCredentialBusy, ProviderCredentialCopyPending,
                                           ProviderCredentialTimedOut)):
        return None
    return credential_message(found)


def translate(exc: Exception, *, retry_omlx: _RetryHook = None, endpoint_name: str = "local") -> tuple[str, str]:
    """Returns (message shown to the user, raw detail for the debug export)."""
    detail = f"{type(exc).__name__}: {exc}"
    # Endpoint names are internal identifiers; the user sees what the app is.
    endpoint_name = {"local_provider": "local app"}.get(endpoint_name, endpoint_name)

    if (message := credential_message(exc)) is not None:
        return message, detail

    if isinstance(exc, ModelLoadError):
        # Already a plain, specific message. A machine reason code (for example
        # why peer attribution refused) rides along in the debug detail only.
        if reason := getattr(exc, "reason", ""):
            detail = f"{detail} [reason={reason}]"
        return str(exc), detail

    if isinstance(exc, SanitizedHTTPStatusError):
        # The synthetic exception deliberately contains no remote headers,
        # redirect location, body, URL, or original prompt. Never attempt to
        # inspect it using the local oMLX diagnostics below.
        return (f"The {endpoint_name} AI engine returned an error "
                f"({exc.response.status_code}). Try again in a moment."), detail

    if isinstance(exc, IncompleteStreamError):
        if "allowed size" in str(exc):
            return (f"The {endpoint_name} AI response was larger than Wisp can "
                    "safely hold. Any answer text already received was kept."), detail
        return (f"The {endpoint_name} AI engine ended its response unexpectedly. "
                "Try again; no actions were run from the incomplete response."), detail

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        if endpoint_name != "local":
            return (f"The {endpoint_name} AI endpoint is unavailable. No automatic action replay was attempted."), detail
        if retry_omlx is not None:
            # Fire-and-forget: ensure_omlx() can take up to ~90s (start, then
            # a restart fallback) — the user gets the sentence immediately and
            # a real shot at the *next* try succeeding, rather than waiting
            # out the recovery attempt inside this one failed turn.
            asyncio.create_task(retry_omlx())
        if retry_omlx is None:
            return "The local AI engine is unavailable. Try again in a moment.", detail
        return ("The local AI engine isn't running. Wisp is trying to restart "
                "it — try again in a few seconds."), detail

    if isinstance(exc, httpx.HTTPStatusError):
        body = ""
        try:
            body = exc.response.text
        except Exception:  # noqa: BLE001 — body read is best-effort
            pass
        if "prefill_memory_exceeded" in body or "prefill_memory_aborted" in body:
            return REQUEST_MEMORY_MESSAGE, detail
        # oMLX 0.7.3 says "Prompt is too long: N tokens > M maximum context length" with the
        # code context_length_exceeded; earlier engines said "exceeds max context window".
        if any(sign in body for sign in ("exceeds max context window", "maximum context length",
                                         "context_length_exceeded")):
            # _fit_window (service/memory/context.py) exists specifically to
            # keep every request under the model's window — reaching this
            # branch means it missed a case, not that the user did anything
            # wrong. Loud because it should be unreachable.
            print(f"[errors] context-window 400 reached translate() despite "
                  f"_fit_window — budgeter has a hole: {body[:300]!r}",
                  file=sys.stderr)
            return ("That conversation got too long for the model's memory. "
                    "Starting a new chat will fix it."), detail
        return (f"The {endpoint_name} AI engine returned an error "
                f"({exc.response.status_code}). Try again in a moment."), detail

    if isinstance(exc, httpx.TimeoutException):
        return ("That request took too long and timed out. Try again, or "
                "with a shorter question."), detail

    return ("Something went wrong. Try again. If it keeps happening, right-click "
            "the Wisp icon in the menu bar and choose Report a Problem…"), detail
