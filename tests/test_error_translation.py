"""service/errors.py — regression tests for docs/STABILITY_PLAN.md S1.

The bug: run_agent's one except clause handed `str(e)` straight to the
client, so a dead oMLX connection, a memory-guard 400, and a KeyError all
looked identical — a raw Python/httpx string with no indication of what
happened or what to do about it.

What must keep holding:
  * a dead connection gets a plain sentence AND fires the retry hook, exactly
    once, without the caller having to await the (up to ~90s) recovery;
  * a memory-guard 400 and a context-window 400 get DIFFERENT sentences, even
    though both arrive as the same httpx.HTTPStatusError shape — the only
    signal is the response body;
  * ModelLoadError's own message passes through unchanged (it's already
    specific — see omlx_client.py);
  * the raw exception text always survives as `detail`, for every branch,
    even though the user only ever sees the friendly sentence.

    .venv/bin/python tests/test_error_translation.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from service.errors import translate  # noqa: E402
from service.inference.omlx_client import ModelLoadError  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def _status_error(body: str, status: int = 400) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions")
    resp = httpx.Response(status, text=body, request=req)
    return httpx.HTTPStatusError(f"{status} error", request=req, response=resp)


def test_model_load_error_passes_through() -> None:
    print("\nModelLoadError's own message is already specific — pass it through")
    exc = ModelLoadError("could not load 'foo': it is not installed in oMLX.")
    msg, detail = translate(exc)
    check("message is the exception's own text", msg == str(exc))
    check("detail names the exception class", "ModelLoadError" in detail)


def test_connect_error_fires_retry_hook_without_waiting() -> None:
    print("\na dead connection gets a plain sentence and a fire-and-forget retry")
    fired = []

    async def retry_hook() -> None:
        await asyncio.sleep(0)  # never actually awaited by translate()
        fired.append(True)

    async def run() -> tuple[str, str]:
        req = httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions")
        result = translate(httpx.ConnectError("Connection refused", request=req),
                            retry_omlx=retry_hook)
        # translate() must return immediately — it only *schedules* the hook.
        check("retry hook not yet run synchronously", fired == [])
        await asyncio.sleep(0)  # let the scheduled task actually execute
        return result

    msg, detail = asyncio.run(run())
    check("message names the engine, not the exception", "engine" in msg.lower())
    check("retry hook eventually ran", fired == [True])
    check("detail keeps the raw exception", "ConnectError" in detail)


def test_memory_guard_and_context_window_get_different_sentences() -> None:
    print("\nsame exception shape, different body -> different sentence")
    mem_msg, _ = translate(_status_error("prefill_memory_exceeded: too much"))
    ctx_msg, _ = translate(_status_error("Prompt too long: exceeds max context window of 16000"))
    check("memory-guard message mentions memory", "memory" in mem_msg.lower())
    check("context-window message mentions the conversation", "conversation" in ctx_msg.lower())
    check("the two sentences are actually different", mem_msg != ctx_msg)


def test_unrecognized_status_still_gets_a_plain_sentence() -> None:
    print("\nan unmapped 4xx/5xx body still degrades to a sentence, not a traceback")
    msg, detail = translate(_status_error("some unrelated server error", status=503))
    check("message has no raw exception text in it", "Traceback" not in msg and "httpx" not in msg)
    check("status code is surfaced", "503" in msg)
    check("detail keeps the raw exception", "HTTPStatusError" in detail)


def test_unknown_exception_gets_generic_fallback() -> None:
    print("\nan exception type nobody mapped still returns a sentence + detail")
    msg, detail = translate(KeyError("choices"))
    check("message is generic, not the raw KeyError text", "'choices'" not in msg)
    check("detail keeps the raw exception", "KeyError" in detail)


if __name__ == "__main__":
    test_model_load_error_passes_through()
    test_connect_error_fires_retry_hook_without_waiting()
    test_memory_guard_and_context_window_get_different_sentences()
    test_unrecognized_status_still_gets_a_plain_sentence()
    test_unknown_exception_gets_generic_fallback()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
