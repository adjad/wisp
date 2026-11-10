"""Text-to-speech via `say` (real, built into every Mac)."""
from __future__ import annotations

import subprocess

from service.tools.registry import register

_TIMEOUT = 60
_MAX_TIMEOUT = 600


def _timeout_for(txt: str, saving: bool) -> float:
    """Spoken text takes about 10-15 characters a second, so the wait scales
    with its length instead of cutting a long read-aloud off at a minute."""
    if saving:
        return _TIMEOUT
    return min(_MAX_TIMEOUT, max(_TIMEOUT, len(txt) / 8))


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
    # "--" ends the options, so text that starts with "-" (a bullet list,
    # "-5 degrees") is read aloud instead of failing as an unknown option.
    argv += ["--", txt]
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=_timeout_for(txt, bool(out_path)))
    except subprocess.TimeoutExpired:
        return ("(speech did not finish in time and was stopped. Tell the "
                "user; do not speak it again.)")
    if p.returncode != 0:
        return f"(could not speak the text: {(p.stderr or '').strip()})"
    return f"Saved speech to {out_path.name}." if out_path else "Spoken."


