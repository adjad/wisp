"""Built-in tools: shell + filesystem. Registered on import."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from service.tools.registry import register

MAX_OUTPUT = 12_000  # chars returned to the model


def _clip(s: str) -> str:
    return s if len(s) <= MAX_OUTPUT else s[:MAX_OUTPUT] + f"\n…[truncated {len(s)-MAX_OUTPUT} chars]"


@register(
    "run_shell",
    "Run a shell command on the user's Mac and return its stdout/stderr. "
    "Use for inspecting and operating the system. Prefer read-only commands; "
    "anything that changes state will require the user's confirmation. "
    "Do NOT use this for things that have a dedicated tool instead — volume "
    "(get_volume/set_volume), clipboard (clipboard_read/clipboard_write), "
    "Wi-Fi (set_wifi), screen lock (lock_screen), battery charge/health "
    "(get_battery_status — pmset's log output is noisy PM event history, not "
    "a health metric, and misreading it produces wrong health claims), "
    "keyboard backlight (set_keyboard_backlight — there is no shell command "
    "for this; guessing one is a known dead end), calendar/reminders "
    "(get_upcoming/add_reminder/add_calendar_event/cancel_event), email "
    "(summarize_emails/view_emails), messages (summarize_messages/"
    "view_messages), notes (search_notes), media (spotify/music), or live web "
    "data — weather/prices/news/any URL (web_fetch) — those are faster, more "
    "reliable, and don't need a shell round-trip.",
    {"type": "object",
     "properties": {"cmd": {"type": "string", "description": "the shell command"}},
     "required": ["cmd"]},
    category="shell",
)
def run_shell(cmd: str) -> str:
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=120, cwd=str(Path.home()))
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
        return _clip(out.strip() or f"(exit {p.returncode}, no output)")
    except subprocess.TimeoutExpired:
        return "(command timed out after 120s)"
    except Exception as e:  # noqa: BLE001
        return f"(error running command: {e})"


@register(
    "read_file",
    "Read and return the contents of a text file.",
    {"type": "object",
     "properties": {"path": {"type": "string"}},
     "required": ["path"]},
    category="fs_read",
)
def read_file(path: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"(no such file: {p})"
    try:
        return _clip(p.read_text(errors="replace"))
    except Exception as e:  # noqa: BLE001
        return f"(error reading {p}: {e})"


@register(
    "list_dir",
    "List the entries in a directory.",
    {"type": "object",
     "properties": {"path": {"type": "string"}},
     "required": ["path"]},
    category="fs_read",
)
def list_dir(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_dir():
        return f"(not a directory: {p})"
    entries = sorted(os.listdir(p))
    return _clip("\n".join(entries) or "(empty)")


@register(
    "write_file",
    "Write text to a file, creating or overwriting it. Requires confirmation.",
    {"type": "object",
     "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
     "required": ["path", "content"]},
    category="fs_write",
)
def write_file(path: str, content: str) -> str:
    p = Path(path).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content)} chars to {p}"
    except Exception as e:  # noqa: BLE001
        return f"(error writing {p}: {e})"


@register(
    "delete_path",
    "Delete a file. Requires confirmation.",
    {"type": "object",
     "properties": {"path": {"type": "string"}},
     "required": ["path"]},
    category="fs_delete",
)
def delete_path(path: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"(no such path: {p})"
    if p.is_dir():
        return f"(refusing to delete a directory via this tool: {p})"
    try:
        p.unlink()
        return f"deleted {p}"
    except Exception as e:  # noqa: BLE001
        return f"(error deleting {p}: {e})"
