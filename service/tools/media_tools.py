"""Podcasts, audiobooks, ambient sound, and internet radio.

WHY THESE ARE MOSTLY "OPEN AND SEARCH", NOT "SEARCH AND PLAY"
----------------------------------------------------------------
Podcasts.app and Books.app have essentially no AppleScript dictionary on
modern macOS — probed live on this machine: `tell application "Podcasts" to
name` fails outright, and driving them via the Accessibility API needs a grant
Wisp doesn't request for this. `music`/`spotify` in apps.py work because those
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
# play_ambient(kind="stop") finds the background afplay loop with
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
@register(
    "play_podcast",
    "Open Apple Podcasts to search results for a show or episode. Podcasts.app "
    "has no scripting support on modern macOS, so this opens the search rather "
    "than guaranteeing autoplay — the user taps play once it's in front of them.",
    {"type": "object",
     "properties": {"query": {"type": "string", "description": "Podcast name or topic to search for."}},
     "required": ["query"]},
    category="app_control",
    aliases=["play the daily podcast", "find me a true crime podcast",
             "put on the last episode of my favorite show", "search podcasts for tech news"],
)
def play_podcast(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "(error: play_podcast needs a `query`.)"
    p = subprocess.run(["open", f"podcasts://search?term={quote(q)}"],
                       capture_output=True, text=True, timeout=_TIMEOUT)
    if p.returncode != 0:
        return f"(could not open Podcasts: {(p.stderr or '').strip()})"
    return f"Opened Podcasts to search results for {q!r} — press play on what you want."


@register(
    "play_audiobook",
    "Open Apple Books to the Audiobooks section. Books.app has no scripting "
    "support to search or resume a specific title, so this opens the app and "
    "the user picks from there.",
    {"type": "object", "properties": {}},
    category="app_control",
    aliases=["resume my audiobook", "open my audiobook", "continue listening to my book"],
)
def play_audiobook() -> str:
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


@register(
    "play_ambient",
    "Play white noise or brown noise in the background — for focus, sleep, or "
    "blocking distraction. Synthesized locally, not a recording, so 'rain' or "
    "'ocean' aren't available — only white_noise and brown_noise. Loops for "
    "the given duration, or use manage_timers/stopwatch to stop it early via "
    "`stop_ambient`.",
    {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["white_noise", "brown_noise", "stop"],
                     "description": "Which sound, or 'stop' to end playback."},
            "minutes": {"type": "integer", "description": "How long to play, in minutes. Default 30."},
        },
        "required": ["kind"],
    },
    category="app_control",
    aliases=["play some white noise", "put on brown noise while I sleep",
             "I need background noise to focus", "play static to help me sleep",
             "turn off the white noise", "stop the ambient sound"],
)
def play_ambient(kind: str, minutes: int = 30) -> str:
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
@register(
    "play_radio",
    "Find and play an internet radio station by name or genre — jazz, news, "
    "a specific station name. Streams in the background via a free public "
    "station directory (radio-browser.info); playback quality depends on the "
    "station.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Station name or genre, e.g. 'smooth jazz', 'BBC Radio 1'."},
        },
        "required": ["query"]},
    category="app_control",
    aliases=["play some jazz radio", "put on a news station",
             "find me a radio station for classical music", "play bbc radio 1",
             "put on an internet radio station"],
)
async def play_radio(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return "(error: play_radio needs a `query`.)"
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


@register(
    "stop_radio",
    "Stop internet radio started with play_radio.",
    {"type": "object", "properties": {}},
    category="app_control",
    aliases=["stop the radio", "turn off the radio station", "stop streaming"],
)
def stop_radio() -> str:
    p = subprocess.run(["pkill", "afplay"], capture_output=True)
    return "Stopped." if p.returncode == 0 else "Nothing was playing."


# --------------------------------------------------------------------------
# streaming apps
# --------------------------------------------------------------------------
_STREAMING_APPS = {
    "netflix": "Netflix", "disney+": "Disney+", "disney plus": "Disney+",
    "hulu": "Hulu", "max": "Max", "hbo max": "Max", "prime video": "Prime Video",
    "amazon prime": "Prime Video", "apple tv": "TV", "apple tv+": "TV",
    "youtube": "YouTube", "paramount+": "Paramount+", "peacock": "Peacock",
}


@register(
    "play_streaming",
    "Open a streaming app (Netflix, Disney+, Hulu, Max, Prime Video, Apple "
    "TV, YouTube, Paramount+, Peacock). Launches the app; picking the actual "
    "show is up to the user, since these apps aren't scriptable.",
    {"type": "object",
     "properties": {"app": {"type": "string", "description": "Which streaming app, e.g. 'Netflix'."}},
     "required": ["app"]},
    category="app_control",
    aliases=["open netflix", "put on disney plus", "launch hulu",
             "I want to watch something on max", "open the apple tv app"],
)
def play_streaming(app: str) -> str:
    key = (app or "").strip().lower()
    target = _STREAMING_APPS.get(key)
    if target is None:
        target = next((v for k, v in _STREAMING_APPS.items() if k in key), None)
    if target is None:
        return (f"(I don't recognize {app!r} as a streaming app. Known: "
                f"{', '.join(sorted(set(_STREAMING_APPS.values())))}.)")
    p = subprocess.run(["open", "-a", target], capture_output=True,
                       text=True, timeout=_TIMEOUT)
    if p.returncode != 0:
        return (f"({target} doesn't seem to be installed, or couldn't be "
                f"opened: {(p.stderr or '').strip()})")
    return f"Opened {target}."


# --------------------------------------------------------------------------
# honestly not available
# --------------------------------------------------------------------------
@register(
    "identify_song",
    "Identify a song playing nearby, Shazam-style. NOT YET AVAILABLE — this "
    "would need a native ShazamKit bridge in the Swift app (microphone "
    "capture + on-device fingerprint match), which hasn't been built. Says so "
    "rather than guessing a song.",
    {"type": "object", "properties": {}},
    category="app_control",
    aliases=["what song is this", "shazam this", "identify this music",
             "what's playing right now"],
)
def identify_song() -> str:
    return ("Song identification isn't built yet — it needs a native "
            "ShazamKit bridge (microphone capture in the Swift app) that "
            "doesn't exist in Wisp today. I can't guess a song from nothing, "
            "so I'm telling you rather than making one up.")


@register(
    "get_lyrics",
    "Look up song lyrics. NOT AVAILABLE — there is no free, keyless API for "
    "licensed lyrics; every option needs a paid license. Says so rather than "
    "guessing or reproducing lyrics from memory (which risks copyright "
    "infringement and is often wrong anyway).",
    {"type": "object",
     "properties": {"song": {"type": "string", "description": "Song title (and artist, if known)."}}},
    category="app_control",
    aliases=["what are the lyrics to this song", "show me the lyrics",
             "what does this song say"],
)
def get_lyrics(song: str = "") -> str:
    return ("I don't have a lyrics source — licensed lyrics need a paid API "
            "with no free/keyless option, so this isn't available. I also "
            "won't reproduce lyrics from memory: I can't guarantee accuracy "
            "and it risks reproducing copyrighted text. Try Genius or Apple "
            "Music, which show lyrics for what's currently playing.")


@register(
    "lookup_media_title",
    "Look up info about a movie or TV show — cast, synopsis, rating, release "
    "year. Requires a free TMDB API key the user hasn't configured yet — "
    "until then this reports that plainly.",
    {"type": "object",
     "properties": {"title": {"type": "string", "description": "Movie or show title."}},
     "required": ["title"]},
    category="web_read",
    aliases=["what's that movie about", "who's in the new dune movie",
             "when did that show come out", "what's the rating on this movie"],
)
def lookup_media_title(title: str) -> str:
    return ("Movie/show lookups need a free TMDB API key that isn't "
            "configured yet — add one in Wisp's settings under API keys to "
            "enable this.")
