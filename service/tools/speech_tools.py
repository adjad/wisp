"""Text-to-speech via `say` (real, built into every Mac) — and two honest
non-implementations.

`transcribe_audio` needs Apple's Speech framework (SFSpeechRecognizer), which
has no CLI and no Python binding — it would need a new Swift bridge (record ->
recognize -> return text) that does not exist yet. `live_captions` is a
system-wide Accessibility overlay with no API surface at all, scriptable or
otherwise. Both say so rather than pretending.
"""
from __future__ import annotations

import subprocess

from service.tools.registry import register

_TIMEOUT = 60


@register(
    "text_to_speech",
    "Speak text out loud through the Mac's speakers, or save it as an audio "
    "file. Use for 'read this to me', 'say this out loud'.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to say."},
            "voice": {"type": "string", "description": "Optional voice name, e.g. 'Samantha'. Defaults to system voice."},
            "save_to": {"type": "string", "description": "Optional file path to save as audio (.aiff) instead of speaking aloud."},
        },
        "required": ["text"]},
    category="app_control",
    aliases=["read this out loud to me", "say this out loud",
             "speak this text", "read me this paragraph"],
)
def text_to_speech(text: str, voice: str = "", save_to: str = "") -> str:
    txt = (text or "").strip()
    if not txt:
        return "(error: text_to_speech needs `text`.)"
    argv = ["say"]
    if voice.strip():
        argv += ["-v", voice.strip()]
    out_path = None
    if save_to.strip():
        from pathlib import Path
        out_path = Path(save_to).expanduser()
        if out_path.suffix.lower() != ".aiff":
            out_path = out_path.with_suffix(".aiff")
        argv += ["-o", str(out_path)]
    argv.append(txt)
    p = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT)
    if p.returncode != 0:
        return f"(could not speak the text: {(p.stderr or '').strip()})"
    return f"Saved speech to {out_path.name}." if out_path else "Spoken."


@register(
    "transcribe_audio",
    "Transcribe speech from an audio file to text. NOT YET AVAILABLE — this "
    "needs a native Speech-framework bridge (SFSpeechRecognizer) in the Swift "
    "app that doesn't exist yet. Says so rather than guessing at content.",
    {"type": "object",
     "properties": {"path": {"type": "string", "description": "Audio file to transcribe."}}},
    category="app_control",
    aliases=["transcribe this voice memo", "what does this recording say",
             "convert this recording into a written transcript"],
)
def transcribe_audio(path: str = "") -> str:
    return ("Audio transcription isn't built yet — it needs a native Speech "
            "framework bridge in the Swift app that doesn't exist today. I "
            "can't guess at what an audio file says, so I'm telling you "
            "rather than making something up.")


@register(
    "live_captions",
    "Turn on system-wide Live Captions. NOT AVAILABLE — Live Captions is a "
    "system accessibility overlay with no scriptable API at all on macOS. "
    "Says so rather than pretending to toggle it.",
    {"type": "object", "properties": {}},
    category="app_control",
    aliases=["turn on live captions", "enable captions for what's playing"],
)
def live_captions() -> str:
    return ("Live Captions has no API — turn it on in System Settings > "
            "Accessibility > Live Captions.")
