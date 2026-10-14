"""Podcasts, audiobooks, ambient sound, and internet radio.

WHY THESE ARE MOSTLY "OPEN AND SEARCH", NOT "SEARCH AND PLAY"
----------------------------------------------------------------
Podcasts.app and Books.app have essentially no AppleScript dictionary on
modern macOS — probed live on this machine: `tell application "Podcasts" to
name` fails outright, and driving them via the Accessibility API needs a grant
Wisp doesn't request for this. `music` (Apple Music/Spotify) in apps.py works because those
two specific apps kept a real scripting dictionary; Apple's newer apps mostly
didn't inherit it. Where a URL scheme exists (`podcasts://search?term=`,
verified live to be handled) that's what's used; where it doesn't, the tool is
honest about only launching the app rather than claiming a "play" it cannot
actually perform.

AMBIENT SOUND IS SYNTHESIZED, NOT BUNDLED
------------------------------------------
No audio assets ship with Wisp, and "rain"/"ocean" presets would need real
recordings this project doesn't have and can't fabricate. What white noise and
brown noise actually ARE, though, is generatable exactly — random samples for
white, a running sum (integrated white noise) for brown — using only the
stdlib `wave` module and `afplay`, both already on every Mac. So those two are
real and offered; anything that would need a recording is not.
"""
from __future__ import annotations

import math
import random
import struct
import subprocess
import wave
from pathlib import Path
from urllib.parse import quote

import httpx

from service.paths import MOE_DIR
from service.tools.registry import register

_TIMEOUT = 15
_UA = "Wisp/1.0 (local macOS assistant; personal use)"

# The literal path segment "wisp_ambient" is load-bearing, not cosmetic:
# play_audio(kind="stop") finds the background afplay loop with
# `pkill -f wisp_ambient`, matching against the full command line — which only
# contains this string because the WAV file's own path does. A generic
# "ambient" or "cache" directory name would make that pkill call match nothing.
_SOUND_DIR = MOE_DIR / "cache" / "wisp_ambient"
_SAMPLE_RATE = 22050


def _osa(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script], capture_output=True,
                          text=True, timeout=_TIMEOUT)


# --------------------------------------------------------------------------
# podcasts / audiobooks
# --------------------------------------------------------------------------
def _play_podcast(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "(error: a podcast search needs a `query`.)"
    p = subprocess.run(["open", f"podcasts://search?term={quote(q)}"],
                       capture_output=True, text=True, timeout=_TIMEOUT)
    if p.returncode != 0:
        return f"(could not open Podcasts: {(p.stderr or '').strip()})"
    return f"Opened Podcasts to search results for {q!r} — press play on what you want."


def _play_audiobook() -> str:
    p = _osa('tell application "Books" to activate')
    if p.returncode != 0:
        return f"(could not open Books: {(p.stderr or '').strip()})"
    return "Opened Books — pick up your audiobook from the Audiobooks tab."


# --------------------------------------------------------------------------
# ambient sound — synthesized, played in the background
# --------------------------------------------------------------------------
def _synth_noise(kind: str, seconds: int) -> Path:
    _SOUND_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _SOUND_DIR / f"{kind}_{seconds}s.wav"
    if path.exists():
        return path
    n = _SAMPLE_RATE * seconds
    samples = [random.randint(-8000, 8000) for _ in range(n)]
    if kind == "brown":
        # Brown noise is the running sum of white noise (a random walk),
        # normalized back into range — the standard, simplest brown-noise
        # construction, and cheap enough to do in pure Python for a few
        # seconds of audio.
        total = 0
        walked = []
        for s in samples:
            total += s
            walked.append(total)
        peak = max(1, max(abs(v) for v in walked))
        samples = [int(v / peak * 12000) for v in walked]
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SAMPLE_RATE)
        w.writeframes(b"".join(struct.pack("<h", max(-32000, min(32000, s))) for s in samples))
    return path


def _play_ambient(kind: str, minutes: int = 30) -> str:
    key = (kind or "").strip().lower()
    if key == "stop":
        subprocess.run(["pkill", "-f", "afplay.*wisp_ambient"], capture_output=True)
        return "Stopped ambient sound."
    if key not in ("white_noise", "brown_noise"):
        return f"(error: unknown kind {key!r}. Use white_noise, brown_noise, or stop.)"
    try:
        mins = max(1, min(180, int(minutes)))
    except (TypeError, ValueError):
        mins = 30

    # Synthesize a short loop and repeat it via a shell one-liner rather than
    # generating `mins` minutes of audio up front — a 3-hour WAV at 22kHz mono
    # is ~16MB for no benefit, since a 20s loop is inaudible as a loop under
    # either noise type.
    clip = _synth_noise("brown" if key == "brown_noise" else "white", 20)
    repeats = max(1, (mins * 60) // 20)
    subprocess.Popen(
        ["bash", "-c",
         f'for i in $(seq 1 {repeats}); do afplay "{clip}" || break; done'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    label = "brown noise" if key == "brown_noise" else "white noise"
    return f"Playing {label} for {mins} minutes. Say 'stop the ambient sound' to end it early."


# --------------------------------------------------------------------------
# internet radio
# --------------------------------------------------------------------------
async def _play_radio(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "(error: a radio search needs a `query`.)"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get("https://de1.api.radio-browser.info/json/stations/search",
                            params={"name": q, "limit": 5, "hidebroken": "true",
                                    "order": "votes", "reverse": "true"},
                            headers={"User-Agent": _UA})
    except httpx.HTTPError as e:
        return f"(could not reach the radio directory: {e})"
    if r.status_code >= 400:
        return f"(radio directory returned HTTP {r.status_code})"
    stations = r.json()
    if not stations:
        return f"No radio stations found matching {q!r}."
    station = stations[0]
    url = station.get("url_resolved") or station.get("url")
    name = station.get("name", q).strip()
    if not url:
        return f"Found {name!r} but it has no playable stream URL."
    subprocess.Popen(["afplay", url], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    return f"Playing {name} ({station.get('country', '')}). Say 'stop the radio' to end it."


def _stop_radio() -> str:
    p = subprocess.run(["pkill", "afplay"], capture_output=True)
    return "Stopped." if p.returncode == 0 else "Nothing was playing."


# --------------------------------------------------------------------------
# honestly not available
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# the one registered entry point
# --------------------------------------------------------------------------
@register(
    "play_audio",
    "Play non-music audio. kind='podcast' opens Apple Podcasts search for `query` "
    "(the user presses play; Podcasts isn't scriptable). kind='audiobook' opens "
    "Apple Books' Audiobooks. kind='radio' streams an internet radio station "
    "matching `query` (name or genre). kind='ambient' plays locally synthesized "
    "white or brown noise (`query` 'white_noise' or 'brown_noise'; no rain/ocean "
    "recordings) for `minutes`. kind='stop' stops radio and ambient sound. For "
    "songs and playlists use `music`.",
    {
        "type": "object",
        "properties": {
            "kind": {"type": "string",
                     "enum": ["podcast", "audiobook", "radio", "ambient", "stop"],
                     "description": "What to play, or 'stop'."},
            "query": {"type": "string",
                      "description": "Podcast/radio search, or white_noise/brown_noise for ambient."},
            "minutes": {"type": "integer", "description": "Ambient only: how long, default 30."},
        },
        "required": ["kind"],
    },
    category="app_control",
    aliases=["play the daily podcast", "find me a true crime podcast",
             "resume my audiobook", "continue listening to my book",
             "play some white noise", "I need background noise to focus",
             "play some jazz radio", "put on a news station", "play bbc radio 1",
             "stop the radio", "turn off the white noise", "stop the ambient sound"],
)
async def play_audio(kind: str, query: str = "", minutes: int = 30) -> str:
    key = (kind or "").strip().lower()
    if key == "podcast":
        return _play_podcast(query)
    if key == "audiobook":
        return _play_audiobook()
    if key == "radio":
        return await _play_radio(query)
    if key == "ambient":
        return _play_ambient(query or "white_noise", minutes)
    if key == "stop":
        ambient = subprocess.run(["pkill", "-f", "afplay.*wisp_ambient"], capture_output=True)
        radio = _stop_radio()
        return "Stopped." if ambient.returncode == 0 or radio == "Stopped." else "Nothing was playing."
    return f"(error: unknown kind {key!r}. Use podcast, audiobook, radio, ambient, or stop.)"
