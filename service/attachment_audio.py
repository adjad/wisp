"""Listen to an audio attachment (a voice memo) with the local model and return its words.

The child process (documents.run_attachment_child, job "audio") turns the file into
16 kHz mono WAV windows of at most 29.5 s, covering at most the first 600 s. Each window
is then transcribed in order by voice.transcribe_wav on the model the caller proved local.

IMPORTANT: the transcript is attacker-controlled text. A voice memo can say "ignore your
instructions". This module does not fence it; the caller must put it inside
`untrusted.fence(...)` before it reaches any model or the person.

This module logs and prints nothing. The audio itself is never recorded for debugging
(voice._ask_model replaces it with an "omitted" marker). With Debug Mode on, the model's reply, which
is the transcript text, is recorded like any other model reply; outside a Debug Mode capture nothing is.
"""
from __future__ import annotations

import base64
import binascii
import time
from typing import NamedTuple

from service import documents, voice

# Stop starting windows when less than this is left before the deadline.
_MIN_LEFT_S = 4.0
# Time kept back from the deadline for the child's own work being bounded by it.
_CHILD_TIMEOUT_S = 60.0


class Heard(NamedTuple):
    text: str
    heard_s: float
    total_s: float
    complete: bool


def _decode(window) -> bytes:
    if not isinstance(window, str) or not window:
        raise ValueError("Wisp couldn't read that recording.")
    try:
        return base64.b64decode(window, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Wisp couldn't read that recording.") from None


def _seconds(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and number > 0 else 0.0


async def listen(name: str, data: bytes, *, client, model: str, deadline: float) -> Heard:
    """What was said in the recording, and how much of it was heard.

    Raises ValueError(plain sentence) for an unreadable recording, voice.VoiceUnavailable when
    the model cannot hear, and TimeoutError when not one window could be transcribed because
    the engine stayed busy. A recording with no speech returns Heard("", ...).
    """
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("model busy")
    info = await documents.run_attachment_child("audio", name, data,
                                                timeout=max(1.0, min(left, _CHILD_TIMEOUT_S)))
    if not isinstance(info, dict):
        raise ValueError("Wisp couldn't read that recording.")
    windows = info.get("windows")
    if not isinstance(windows, list) or not windows:
        raise ValueError("Wisp couldn't read that recording.")
    decoded = [_decode(w) for w in windows]          # strict, all before any model work
    total = _seconds(info.get("seconds"))

    pieces: list[str] = []
    heard = 0.0
    done = 0
    for clip in decoded:
        if deadline - time.monotonic() < _MIN_LEFT_S:
            break
        try:
            text = await voice.transcribe_wav(clip, client=client, model=model, deadline=deadline)
        except TimeoutError:
            break
        done += 1
        heard += voice.wav_seconds(clip)
        if text:
            pieces.append(text)
    if done == 0:
        raise TimeoutError("model busy")
    text = " ".join(" ".join(pieces).split())
    if total:
        heard = min(heard, total)
    else:
        total = heard
    # A tail under 0.2 s that fit no window (dropped_tail) cannot hold a word and does not make it incomplete.
    complete = (done == len(decoded) and not bool(info.get("stopped_early"))
                and _seconds(info.get("dropped_tail")) < 0.2)
    return Heard(text, heard, total, complete)


def _span(seconds: float) -> tuple[int, str]:
    """(count, unit): whole seconds under a minute, whole minutes from a minute up."""
    whole = int(round(seconds))
    if whole < 60:
        return whole, "second" if whole == 1 else "seconds"
    minutes = whole // 60
    return minutes, "minute" if minutes == 1 else "minutes"


def how_much(heard: Heard) -> str:
    """One plain sentence on how much of the recording was heard. Never says "all" unless complete."""
    count, unit = _span(heard.total_s)
    if heard.complete:
        if unit.startswith("second"):
            return f"Heard all {count} {unit}."
        minutes = max(1, (int(round(heard.total_s)) + 30) // 60)
        return f"Heard all of it (about {minutes} minute{'' if minutes == 1 else 's'})."
    first, first_unit = _span(heard.heard_s)
    if heard.total_s > 600.5 and heard.heard_s >= 595:
        return "Heard the first 10 minutes; the recording is longer than Wisp transcribes."
    if first_unit == unit or (first_unit.rstrip("s") == unit.rstrip("s")):
        return f"Heard the first {first} {first_unit} of {count}."
    return f"Heard the first {first} {first_unit} of {count} {unit}."
