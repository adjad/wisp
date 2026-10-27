"""Voice input: a spoken clip becomes the person's question, on this Mac only.

MEASURED 2026-10-26 on gemma-4-E4B-it-oQ4e-mtp (service/voice.py): warm, a 5 s
clip transcribed verbatim in 0.9 s. These checks use a stub client and a
synthetic checkpoint; nothing here loads or calls a model.
"""
from __future__ import annotations

import asyncio
import base64
import json
import struct
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from service import idle
import service.voice as V

MODEL = "gemma-stub"


def wav(seconds: float = 1.0, rate: int = 16_000, bits: int = 16, fmt: int = 1) -> bytes:
    frames = int(seconds * rate)
    align = bits // 8
    data = b"\x00\x00" * frames if bits == 16 else b"\x00" * frames * align
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(data), b"WAVE", b"fmt ", 16,
                         fmt, 1, rate, rate * align, align, bits, b"data", len(data))
    return header + data


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@pytest.fixture
def checkpoint(tmp_path):
    def make(audio=True, weights=True):
        root = tmp_path / ("audio" if audio else "plain") / ("w" if weights else "nw")
        root.mkdir(parents=True, exist_ok=True)
        (root / "config.json").write_text(json.dumps({"audio_config": {"x": 1} if audio else None}))
        names = {"model.language_model.layers.0.w": "a"}
        if weights:
            names["model.audio_tower.layers.0.w"] = "a"
        (root / "model.safetensors.index.json").write_text(json.dumps({"weight_map": names}))
        return str(root)
    V._audio_capable.clear()
    yield make
    V._audio_capable.clear()


class StubClient:
    def __init__(self, path, reply="Hey Wisp, what's on my calendar today?", model_type="vlm"):
        self.path, self.reply, self.model_type = path, reply, model_type
        self.calls: list[dict] = []

    def activity_key(self, model):
        return f"stub:{model}"

    async def status(self):
        return {"models": [{"id": MODEL, "model_type": self.model_type,
                            "engine_type": self.model_type, "model_path": self.path}]}

    async def chat(self, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        return {"choices": [{"message": {"content": self.reply}, "finish_reason": "stop"}]}


def run(coro):
    return asyncio.run(coro)


def test_wav_is_measured_and_only_16_bit_pcm_is_taken():
    assert V.wav_seconds(wav(2.0)) == pytest.approx(2.0)
    assert V.wav_seconds(wav(1.0, rate=48_000)) == pytest.approx(1.0)
    for bad in (b"", b"not a wav at all, definitely not" * 3, wav(1.0, fmt=3), wav(1.0, bits=8)):
        with pytest.raises(ValueError):
            V.wav_seconds(bad)


def test_clip_bounds():
    assert V.decode_clip(b64(wav(1.0)))
    for bad in ("", None, "not base64!!", b64(wav(31.0)), b64(wav(0.1))):
        with pytest.raises(ValueError):
            V.decode_clip(bad)
    with pytest.raises(ValueError):
        V.decode_clip("A" * (V._MAX_BYTES * 2))     # refused before decoding


def test_the_clip_reaches_the_model_as_audio_and_comes_back_as_words(checkpoint):
    client = StubClient(checkpoint())
    text = run(V.transcribe(b64(wav(1.0)), client=client, model=MODEL))
    assert text == "Hey Wisp, what's on my calendar today?"
    call = client.calls[0]
    parts = call["messages"][0]["content"]
    assert parts[0]["type"] == "input_audio" and parts[0]["input_audio"]["format"] == "wav"
    assert base64.b64decode(parts[0]["input_audio"]["data"]) == wav(1.0)
    assert "Do not answer" in parts[1]["text"]
    # A transcript, not an answer: no thinking, no sampling, no tools.
    assert call["chat_template_kwargs"] == {"enable_thinking": False}
    assert call["temperature"] == 0 and "tools" not in call


@pytest.mark.parametrize("audio,weights,model_type", [
    (False, False, "vlm"),   # a vision model: config has no audio encoder
    (True, False, "vlm"),    # config says audio, the quantized weights dropped it
    (True, True, "llm"),     # the engine serves it text-only
])
def test_a_model_without_ears_never_gets_the_clip(checkpoint, audio, weights, model_type):
    client = StubClient(checkpoint(audio, weights), model_type=model_type)
    with pytest.raises(V.VoiceUnavailable, match="can't hear audio"):
        run(V.transcribe(b64(wav(1.0)), client=client, model=MODEL))
    assert client.calls == []


def test_an_unlisted_model_or_silent_engine_is_not_proof_of_ears(checkpoint):
    client = StubClient(checkpoint())

    async def broken():
        raise ConnectionError("engine down")
    client.status = broken
    with pytest.raises(V.VoiceUnavailable):
        run(V.transcribe(b64(wav(1.0)), client=client, model=MODEL))
    with pytest.raises(V.VoiceUnavailable):
        run(V.transcribe(b64(wav(1.0)), client=StubClient(checkpoint()), model="other-model"))


def test_only_the_local_engine_hears_the_clip():
    cloud = SimpleNamespace(endpoint=SimpleNamespace(managed=False, name="cloud"), model="m")
    local_provider = SimpleNamespace(endpoint=SimpleNamespace(managed=True, name="local_provider"), model="m")
    for target in (cloud, local_provider):
        with patch("service.config.endpoints.role_target", return_value=target), \
                patch("service.inference.remote_policy.remote_only_profile", return_value=None):
            with pytest.raises(V.VoiceUnavailable, match="on this Mac"):
                V._resolve()
    local = SimpleNamespace(endpoint=SimpleNamespace(managed=True, name="local"), model="m")
    with patch("service.config.endpoints.role_target", return_value=local), \
            patch("service.inference.remote_policy.remote_only_profile", return_value=object()):
        with pytest.raises(V.VoiceUnavailable):
            V._resolve()


def test_waits_for_the_model_to_be_free(checkpoint):
    client = StubClient(checkpoint())
    key = client.activity_key(MODEL)

    async def scenario():
        idle.begin(key)
        task = asyncio.create_task(V.transcribe(b64(wav(1.0)), client=client, model=MODEL))
        await asyncio.sleep(0.3)
        assert client.calls == []            # still waiting on the other call
        idle.end(key)
        return await task
    assert run(scenario())
    assert len(client.calls) == 1


@pytest.mark.parametrize("raw,clean", [
    ("  Hey Wisp,\n what's up? ", "Hey Wisp, what's up?"),
    ('"Remind me at nine."', "Remind me at nine."),
    ("“What's next?”", "What's next?"),
    ("[silence]", ""),
    ("(no speech).", ""),
    ("", ""),
])
def test_transcript_cleanup(raw, clean):
    assert V.clean_transcript(raw) == clean


def test_endpoint_maps_failures_to_plain_messages(checkpoint):
    from fastapi import HTTPException
    from service import main

    async def unavailable(_):
        raise V.VoiceUnavailable("Voice works only with a model on this Mac.")

    async def busy(_):
        raise TimeoutError("model busy")

    async def bad(_):
        raise ValueError("clip is too long")

    async def ok(_):
        return "What's on my day?"

    async def crash(_):
        raise RuntimeError("engine 500")

    for fake, code in ((unavailable, 409), (busy, 503), (bad, 400), (crash, 502)):
        with patch.object(V, "transcribe", fake):
            with pytest.raises(HTTPException) as caught:
                run(main.voice_transcribe({"audio": "x"}))
            assert caught.value.status_code == code
            assert "Traceback" not in caught.value.detail and "500" not in caught.value.detail
    with patch.object(V, "transcribe", ok):
        assert run(main.voice_transcribe({"audio": "x"})) == {"text": "What's on my day?"}
