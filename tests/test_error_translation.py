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
from service.inference.omlx_client import IncompleteStreamError, ModelLoadError  # noqa: E402

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


def test_incomplete_cloud_stream_gets_specific_recovery_message() -> None:
    print("\na provider stream failure gets a specific, actionable sentence")
    msg, detail = translate(
        IncompleteStreamError("Inference stream ended before completion"),
        endpoint_name="cloud",
    )
    check("message names the cloud engine", "cloud" in msg.lower())
    check("message is not the generic Wisp failure", "Wisp's end" not in msg)
    check("message confirms no actions ran", "no actions" in msg.lower())
    check("detail keeps the exception class", "IncompleteStreamError" in detail)


def _streamed(handler, *, managed: bool = True):
    """Run one stream_events call against a fake engine; returns the exception it raised."""
    from service.inference.omlx_client import OMLXClient

    async def run():
        client = OMLXClient(base_url="http://127.0.0.1:8000" if managed else "https://provider.invalid/v1",
                            api_key="test")
        client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(handler))
        try:
            async for _ in client.stream_events("some-model", [{"role": "user", "content": "hello"}],
                                                max_tokens=64):
                pass
        except Exception as exc:  # noqa: BLE001 - the exception is the result
            return exc
        finally:
            await client._client.aclose()
        return None

    return asyncio.run(run())


def _sse(*chunks: dict) -> httpx.Response:
    import json
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


# What oMLX 0.7.3 sends (server.py: _prefill_memory_openai_error_body, validate_context_window).
_GUARD_BODY = {"type": "error", "error": {
    "message": "Request rejected by the prefill memory guard: private-prompt-words",
    "type": "invalid_request_error", "code": "prefill_memory_exceeded",
    "omlx_code": "prefill_memory_exceeded", "estimated_bytes": 9, "limit_bytes": 5}}
_MEMORY = "That request needed more memory than Wisp is allowed to use."


def test_a_streamed_refusal_keeps_the_engines_reason() -> None:
    print("\nevery Ask turn is streamed: the engine's reason must survive an unread error body")
    exc = _streamed(lambda request: httpx.Response(400, json=_GUARD_BODY))
    check("a streamed 400 is still a status error", isinstance(exc, httpx.HTTPStatusError), repr(exc))
    msg, detail = translate(exc)
    check("a streamed memory-guard 400 says the request needed more memory", msg.startswith(_MEMORY), msg)
    check("and suggests a shorter question", "shorter question" in msg)
    check("the engine's own message is not shown or put in the detail",
          "private-prompt-words" not in msg + detail)

    overflow = {"error": {"message": "Prompt is too long: 20000 tokens > 16384 maximum context length",
                          "type": "invalid_request_error", "code": "context_length_exceeded"}}
    msg, _ = translate(_streamed(lambda request: httpx.Response(400, json=overflow)))
    check("a streamed context overflow says to start a new chat", "new chat" in msg, msg)
    msg, _ = translate(_status_error("Prompt is too long: 20000 tokens > 16384 maximum context length"))
    check("the 0.7.3 overflow wording is recognised on its own", "new chat" in msg, msg)
    msg, _ = translate(_status_error('{"error": {"code": "context_length_exceeded"}}'))
    check("and so is its code", "new chat" in msg, msg)
    msg, _ = translate(_status_error("Prompt too long: exceeds max context window of 16000"))
    check("the earlier wording still is", "new chat" in msg, msg)

    msg, _ = translate(_streamed(lambda request: httpx.Response(500, json={"error": {"message": "boom"}})))
    check("any other streamed failure keeps the plain status sentence", "(500)" in msg and "boom" not in msg, msg)
    big = httpx.Response(400, content=b"x" * 200_000 + b"prefill_memory_exceeded")
    msg, _ = translate(_streamed(lambda request: big))
    check("only the start of an oversized error body is kept", "(400)" in msg, msg)


def test_a_memory_guard_error_inside_the_stream_is_named() -> None:
    print("\nthe engine can also report its memory guard inside a 200 stream")
    from service.inference.omlx_client import ModelMemoryLimitError
    delta = {"choices": [{"index": 0, "delta": {"content": "Sure"}, "finish_reason": None}]}
    for code in ("prefill_memory_aborted", "prefill_memory_exceeded"):
        body = {"type": "error", "error": {"message": "guard: private-prompt-words", "omlx_code": code}}
        exc = _streamed(lambda request, body=body: _sse(delta, body))
        check(f"{code} raises the memory-limit error", isinstance(exc, ModelMemoryLimitError), repr(exc))
        msg, detail = translate(exc)
        check(f"{code} says the request needed more memory", msg.startswith(_MEMORY), msg)
        check(f"{code} keeps the engine's message out", "private-prompt-words" not in msg + detail)
    exc = _streamed(lambda request: _sse(delta, {"error": {"code": "prefill_memory_exceeded"}}))
    check("the code is read from error.code too", isinstance(exc, ModelMemoryLimitError), repr(exc))
    exc = _streamed(lambda request: _sse(delta, {"error": {"message": "x"}, "omlx_code": "prefill_memory_aborted"}))
    check("and from the top level", isinstance(exc, ModelMemoryLimitError), repr(exc))

    exc = _streamed(lambda request: _sse(delta, {"error": {"message": "boom", "type": "server_error"}}))
    check("any other in-stream error is still an incomplete stream",
          isinstance(exc, IncompleteStreamError), repr(exc))
    check("with the no-actions sentence", "no actions" in translate(exc)[0].lower())
    for odd in ({"error": "prefill_memory_exceeded"}, {"error": {"code": 7}}, {"error": None}):
        exc = _streamed(lambda request, odd=odd: _sse(odd))
        check(f"a malformed error chunk {odd} is an incomplete stream",
              isinstance(exc, IncompleteStreamError), repr(exc))


def test_a_remote_providers_error_body_is_never_read() -> None:
    print("\na remote provider's error text stays out of Wisp")
    from service.inference import remote_policy
    from service.inference.omlx_client import SanitizedHTTPStatusError
    original = remote_policy.remote_only_profile
    remote_policy.remote_only_profile = lambda: None
    try:
        exc = _streamed(lambda request: httpx.Response(400, json=_GUARD_BODY), managed=False)
        check("a remote 400 is the sanitized status error", isinstance(exc, SanitizedHTTPStatusError), repr(exc))
        check("with no body at all", exc.response.content == b"")
        msg, _ = translate(exc, endpoint_name="cloud")
        check("and the plain status sentence", "(400)" in msg and "memory" not in msg, msg)
        guard = {"error": {"code": "prefill_memory_exceeded"}}
        exc = _streamed(lambda request: _sse(guard), managed=False)
        check("a remote in-stream error is never read for a code",
              isinstance(exc, IncompleteStreamError), repr(exc))
    finally:
        remote_policy.remote_only_profile = original


if __name__ == "__main__":
    test_model_load_error_passes_through()
    test_connect_error_fires_retry_hook_without_waiting()
    test_memory_guard_and_context_window_get_different_sentences()
    test_unrecognized_status_still_gets_a_plain_sentence()
    test_unknown_exception_gets_generic_fallback()
    test_incomplete_cloud_stream_gets_specific_recovery_message()
    test_a_streamed_refusal_keeps_the_engines_reason()
    test_a_memory_guard_error_inside_the_stream_is_named()
    test_a_remote_providers_error_body_is_never_read()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
