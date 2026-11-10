"""Listening to an audio attachment: windows in order, joined, honest about how much was heard.

The child job and the model are faked; nothing here loads or calls a model.
"""
from __future__ import annotations

import asyncio
import base64
import io
import time
import wave

import pytest

from service import attachment_audio as A
from service import documents, voice


def make_wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes(b"\x00\x00" * int(seconds * 16_000))
    return buf.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def run(coro):
    return asyncio.run(coro)


def later(seconds=60.0):
    return time.monotonic() + seconds


@pytest.fixture
def child(monkeypatch):
    """Fake the child job; `child.reply` is what it returns, `child.calls` what it was asked."""
    state = type("S", (), {})()
    state.calls = []
    state.reply = {"seconds": 6.0, "heard": 6.0, "stopped_early": False,
                   "windows": [b64(make_wav(2.0)), b64(make_wav(2.0)), b64(make_wav(2.0))]}

    async def fake(job, name, data, *, timeout, part=None):
        state.calls.append((job, name, data, timeout))
        if isinstance(state.reply, Exception):
            raise state.reply
        return state.reply
    monkeypatch.setattr(documents, "run_attachment_child", fake)
    return state


@pytest.fixture
def model(monkeypatch):
    """Fake transcribe_wav: replies in order from `model.texts`; an Exception entry is raised."""
    state = type("M", (), {})()
    state.texts = ["first", "second", "third"]
    state.seen = []

    async def fake(data, *, client, model, deadline):
        state.seen.append(voice.wav_seconds(data))
        item = state.texts[len(state.seen) - 1]
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(voice, "transcribe_wav", fake)
    return state


def listen(deadline=None):
    return run(A.listen("memo.m4a", b"audio", client=object(), model="m", deadline=deadline or later()))


def test_windows_are_transcribed_in_order_and_joined_with_one_space(child, model):
    model.texts = ["Hello there. ", " I will be late", "tomorrow  at nine."]
    heard = listen()
    assert heard.text == "Hello there. I will be late tomorrow at nine."
    assert model.seen == [2.0, 2.0, 2.0]
    assert child.calls[0][0] == "audio" and child.calls[0][2] == b"audio"
    assert heard == A.Heard(heard.text, 6.0, 6.0, True)


def test_a_silent_window_adds_nothing(child, model):
    model.texts = ["before", "", "after"]
    heard = listen()
    assert heard.text == "before after" and heard.complete


def test_a_recording_with_no_speech_is_empty_but_still_heard(child, model):
    model.texts = ["", "", ""]
    assert listen() == A.Heard("", 6.0, 6.0, True)


def test_the_child_is_bounded_by_the_deadline(child, model):
    listen(later(20.0))
    assert 1.0 <= child.calls[0][3] <= 20.0


def test_stopping_near_the_deadline_leaves_complete_false(child, model, monkeypatch):
    clock = {"left": 100.0}
    real = time.monotonic

    def fake_now():
        return real() + (100.0 - clock["left"])
    monkeypatch.setattr(A.time, "monotonic", fake_now)

    async def stepping(data, *, client, model, deadline):
        clock["left"] -= 98.0         # leaves too little for the next window
        return "only"
    monkeypatch.setattr(voice, "transcribe_wav", stepping)
    deadline = real() + 100.0
    heard = run(A.listen("memo.m4a", b"a", client=object(), model="m", deadline=deadline))
    assert heard.text == "only" and heard.heard_s == 2.0 and heard.total_s == 6.0
    assert heard.complete is False


def test_a_window_that_times_out_midway_stops_there(child, model):
    model.texts = ["one", TimeoutError("model busy"), "never"]
    heard = listen()
    assert heard.text == "one" and heard.heard_s == 2.0 and not heard.complete
    assert len(model.seen) == 2


def test_every_window_busy_is_a_timeout(child, model):
    model.texts = [TimeoutError("model busy")]
    with pytest.raises(TimeoutError):
        listen()


def test_a_deadline_already_passed_is_a_timeout_without_the_child(child, model):
    with pytest.raises(TimeoutError):
        listen(time.monotonic() - 1.0)
    assert child.calls == []


def test_a_child_that_stopped_early_is_not_complete(child, model):
    child.reply = {**child.reply, "seconds": 840.0, "heard": 6.0, "stopped_early": True}
    heard = listen()
    assert heard.complete is False and heard.total_s == 840.0 and heard.heard_s == 6.0


def test_a_bad_window_is_a_plain_value_error_before_any_model_work(child, model):
    for bad in ("not base64!!", "", None, 5):
        child.reply = {**child.reply, "windows": [b64(make_wav(2.0)), bad]}
        with pytest.raises(ValueError) as caught:
            listen()
        assert "recording" in str(caught.value)
    assert model.seen == []


def test_no_windows_is_a_plain_value_error(child, model):
    child.reply = {"seconds": 0.0, "heard": 0.0, "stopped_early": False, "windows": []}
    with pytest.raises(ValueError):
        listen()


def test_the_childs_value_error_passes_through(child, model):
    child.reply = ValueError("That recording isn't one Wisp can read.")
    with pytest.raises(ValueError, match="isn't one Wisp can read"):
        listen()


def test_a_model_without_ears_passes_through(child, model):
    model.texts = [voice.VoiceUnavailable("nope, can't hear audio")]
    with pytest.raises(voice.VoiceUnavailable):
        listen()


def test_the_real_transcribe_wav_sees_the_real_window_bytes(child, monkeypatch):
    """Both sides real: windows are decoded and reach voice.transcribe_wav's own checks."""
    class Client:
        calls = []

        def activity_key(self, model):
            return "k"

        async def status(self):
            return {"models": []}      # no row: not proof of ears

        async def chat(self, *a, **k):
            raise AssertionError("must not be called")
    with pytest.raises(voice.VoiceUnavailable):
        run(A.listen("memo.m4a", b"a", client=Client(), model="m", deadline=later()))
    child.reply = {**child.reply, "windows": [b64(make_wav(31.0))]}
    with pytest.raises(ValueError):
        run(A.listen("memo.m4a", b"a", client=Client(), model="m", deadline=later()))


@pytest.mark.parametrize("heard,expected", [
    (A.Heard("x", 42, 42, True), "Heard all 42 seconds."),
    (A.Heard("x", 1, 1, True), "Heard all 1 second."),
    (A.Heard("x", 125, 125, True), "Heard all of it (about 2 minutes)."),
    (A.Heard("x", 70, 70, True), "Heard all of it (about 1 minute)."),
    (A.Heard("x", 180, 180, True), "Heard all of it (about 3 minutes)."),
    (A.Heard("x", 59, 59, True), "Heard all 59 seconds."),
    (A.Heard("x", 60, 60, True), "Heard all of it (about 1 minute)."),
    (A.Heard("x", 120, 840, False), "Heard the first 2 minutes of 14."),
    (A.Heard("x", 20, 42, False), "Heard the first 20 seconds of 42."),
    (A.Heard("x", 30, 840, False), "Heard the first 30 seconds of 14 minutes."),
    (A.Heard("x", 599, 840, False),
     "Heard the first 10 minutes; the recording is longer than Wisp transcribes."),
    (A.Heard("x", 60, 60, False), "Heard the first 1 minute of 1."),
])
def test_how_much_wording(heard, expected):
    assert A.how_much(heard) == expected


def test_a_dropped_tail_under_the_minimum_does_not_make_it_incomplete(child, model):
    child.reply = {**child.reply, "dropped_tail": 0.19}
    assert listen().complete is True


def test_a_dropped_tail_of_a_word_or_more_is_not_complete(child, model):
    child.reply = {**child.reply, "dropped_tail": 0.5, "stopped_early": False}
    assert listen().complete is False


def test_the_docstring_says_what_debug_mode_records():
    doc = A.__doc__
    assert "Debug Mode" in doc and "transcript" in doc and "never recorded" in doc
    assert "nothing from the audio or the transcript" not in doc.lower()


def test_all_is_never_claimed_unless_complete():
    for total in (5, 90, 700):
        assert "all" not in A.how_much(A.Heard("", total - 3, total, False)).lower().split()
