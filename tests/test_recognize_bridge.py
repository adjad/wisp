"""Text recognition over the mail-memory bridge: what is sent and how replies are read."""
import base64
import math

import pytest

from service.mail_memory import bridge

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 20


def fake_ask(monkeypatch, replies):
    seen = []

    async def ask(kind, timeout, **fields):
        seen.append((kind, timeout, fields))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(bridge, "_ask", ask)
    return seen


async def test_sends_image_and_wait(monkeypatch):
    seen = fake_ask(monkeypatch, [{"ok": True, "text": "INVOICE 4471", "lines": 1, "confidence": 0.9}])
    assert await bridge.recognize_text(JPEG) == ("INVOICE 4471", 0.9)
    assert seen == [("recognize", 20.0, {"image_b64": base64.b64encode(JPEG).decode(), "wait": 20})]


@pytest.mark.parametrize("timeout, wait", [(0.2, 1), (7.2, 8), (20.0, 20), (59.5, 60), (600, 60), (math.inf, 60)])
async def test_wait_is_the_timeout_rounded_up_and_clamped(monkeypatch, timeout, wait):
    seen = fake_ask(monkeypatch, [{"ok": False, "error": "no_text"}])
    await bridge.recognize_text(JPEG, timeout=timeout)
    assert seen[0][2]["wait"] == wait


async def test_late_reply_is_a_timeout(monkeypatch):
    fake_ask(monkeypatch, [{"ok": False, "error": "late"}])
    with pytest.raises(bridge.BridgeError) as caught:
        await bridge.recognize_text(JPEG)
    assert str(caught.value) == "recognize_timeout"


async def test_ask_timeout_is_recognize_timeout(monkeypatch):
    fake_ask(monkeypatch, [bridge.BridgeError("recognize_timeout")])
    with pytest.raises(bridge.BridgeError, match="recognize_timeout"):
        await bridge.recognize_text(JPEG)


async def test_reply_handling(monkeypatch):
    seen = fake_ask(monkeypatch, [
        {"ok": False, "error": "no_text"},
        {"ok": False, "error": "failed"},
        {"ok": False, "error": "weird"},
        {"ok": True, "text": "a" * 250_000, "lines": 1, "confidence": 7},
        {"ok": True, "text": "a", "lines": 1, "confidence": -3},
        {"ok": True, "text": "a", "lines": 1, "confidence": "high"},
        {"ok": True, "text": "a", "lines": 1, "confidence": True},
        {"ok": True, "text": "a", "lines": 1, "confidence": float("nan")},
        {"ok": True, "text": 5, "lines": 1, "confidence": 0.5}])
    assert await bridge.recognize_text(JPEG) == ("", 0.0)
    with pytest.raises(bridge.BridgeError, match="^failed$"):
        await bridge.recognize_text(JPEG)
    with pytest.raises(bridge.BridgeError, match="^recognize_failed$"):
        await bridge.recognize_text(JPEG)
    text, confidence = await bridge.recognize_text(JPEG)
    assert len(text) == 200_000 and confidence == 1.0
    assert (await bridge.recognize_text(JPEG))[1] == 0.0
    for _ in range(4):
        with pytest.raises(bridge.BridgeError, match="recognize_failed"):
            await bridge.recognize_text(JPEG)
    assert len(seen) == 9


async def test_refuses_locally(monkeypatch):
    seen = fake_ask(monkeypatch, [])
    for bad in (b"", b"\x89PNG\r\n", b"\xff\xd8", b"GIF89a" + b"\xff\xd8\xff", "text", None):
        with pytest.raises(bridge.BridgeError, match="bad_image"):
            await bridge.recognize_text(bad)
    with pytest.raises(bridge.BridgeError, match="too_large"):
        await bridge.recognize_text(b"\xff\xd8\xff" + b"0" * (4 * 1024 * 1024))
    assert seen == []
