"""Writing to Notes.app — the other half of `search_notes`.

Wisp could read the user's notes and not write one. "Take a note", "add that to
my packing list" and "jot this down" are T1 daily-driver requests in the
Capability Atlas, and every one of them had to go through `write_file` (a text
file on disk, which is NOT where the user keeps notes and does not sync) or be
declined outright.

Unlike `search_notes`, which reads a cache the Swift app syncs once a day, these
drive Notes.app live over AppleScript. That difference matters in one visible
way: a note created here will not appear in `search_notes` results until the
next sync. The tools say so in their output rather than letting the user
discover it by asking a follow-up question and getting nothing.

TCC: Notes automation access is already granted (NotesReader.swift uses it), so
these need no new permission prompt.
"""
from __future__ import annotations

import subprocess

from service.tools.registry import register

_TIMEOUT = 25


def _osa(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=_TIMEOUT)


def _esc(s: str) -> str:
    """Escape for embedding in an AppleScript string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _html(text: str) -> str:
    """Notes bodies are HTML. Newlines must become <br> or the whole note
    collapses onto one line — which looks like data loss to the user even
    though every character is technically still there."""
    out = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return out.replace("\n", "<br>")


def _err(p: subprocess.CompletedProcess) -> str | None:
    if p.returncode == 0:
        return None
    msg = (p.stderr or "").strip()
    if "-1743" in msg or "not allowed" in msg.lower():
        return ("(Notes automation isn't permitted yet. Grant it in System "
                "Settings > Privacy & Security > Automation > Wisp > Notes.)")
    return f"(error talking to Notes: {msg or 'unknown error'})"


@register(
    "create_note",
    "Create a NEW note in the user's Notes.app. Use this for 'take a note', "
    "'jot this down', or starting a list — not write_file, which puts a text "
    "file on disk where the user does not keep notes. Pass `checklist=true` to "
    "format the body as a checklist, one item per line.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The note's title — its first line."},
            "body": {"type": "string", "description": "The note's contents. Newlines are preserved."},
            "folder": {"type": "string", "description": "Optional Notes folder name. Defaults to the default folder."},
            "checklist": {"type": "boolean", "description": "Render body lines as a checklist."},
        },
        "required": ["title"],
    },
    category="notes_write",
    aliases=["take a note about this", "jot this down for me",
             "start a note for the packing list", "make a note that the wifi password is hunter2",
             "write this down somewhere I'll find it",
             "create a shopping list note"],
)
def create_note(title: str, body: str = "", folder: str = "",
                checklist: bool = False) -> str:
    title = (title or "").strip()
    if not title:
        return "(error: create_note needs a `title`.)"

    if checklist and body.strip():
        items = "".join(f"<li>{_html(ln.strip())}</li>"
                        for ln in body.splitlines() if ln.strip())
        body_html = f"<ul>{items}</ul>"
    else:
        body_html = _html(body)
    full = f"<div><b>{_html(title)}</b></div>{body_html}"

    if folder.strip():
        script = (f'tell application "Notes"\n'
                  f'  if not (exists folder "{_esc(folder.strip())}") then\n'
                  f'    return "NOFOLDER"\n'
                  f'  end if\n'
                  f'  make new note at folder "{_esc(folder.strip())}" '
                  f'with properties {{body:"{_esc(full)}"}}\n'
                  f'  return "OK"\n'
                  f'end tell')
    else:
        script = (f'tell application "Notes"\n'
                  f'  make new note with properties {{body:"{_esc(full)}"}}\n'
                  f'  return "OK"\n'
                  f'end tell')

    p = _osa(script)
    if (e := _err(p)) is not None:
        return e
    if p.stdout.strip() == "NOFOLDER":
        return (f"(There's no Notes folder called {folder!r}. "
                f"Leave `folder` out to use the default folder.)")
    kind = "checklist" if checklist else "note"
    return (f"Created the {kind} \"{title}\" in Notes."
            f" (It won't show up in search_notes until the next daily sync.)")


@register(
    "append_note",
    "Add lines to the END of an EXISTING note, found by part of its title. Use "
    "this for 'add milk to my shopping list'. If several notes match the title, "
    "it asks which one rather than guessing.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Part of the existing note's title, e.g. 'shopping'."},
            "text": {"type": "string", "description": "The line(s) to add at the end."},
        },
        "required": ["title", "text"],
    },
    category="notes_write",
    aliases=["add milk to my shopping list", "put that on my packing list",
             "add a line to the meeting note", "tack this onto my ideas note"],
)
def append_note(title: str, text: str) -> str:
    title = (title or "").strip()
    text = (text or "").strip()
    if not title:
        return "(error: append_note needs the `title` of the note to add to.)"
    if not text:
        return "(error: append_note needs `text` to add.)"

    # Matching happens INSIDE AppleScript rather than by listing every note and
    # filtering here: the note bodies are the user's personal content and there
    # is no reason to pull all of them into this process to find one title.
    script = (
        f'tell application "Notes"\n'
        f'  set hits to (every note whose name contains "{_esc(title)}")\n'
        f'  if (count of hits) is 0 then return "NONE"\n'
        f'  if (count of hits) > 1 then\n'
        f'    set out to ""\n'
        f'    repeat with n in hits\n'
        f'      set out to out & (name of n) & "\\n"\n'
        f'    end repeat\n'
        f'    return "MANY:" & out\n'
        f'  end if\n'
        f'  set target to item 1 of hits\n'
        f'  set body of target to (body of target) & "<div>{_esc(_html(text))}</div>"\n'
        f'  return "OK:" & (name of target)\n'
        f'end tell')

    p = _osa(script)
    if (e := _err(p)) is not None:
        return e
    out = p.stdout.strip()
    if out == "NONE":
        return f"No note has {title!r} in its title. Use create_note to start one."
    if out.startswith("MANY:"):
        names = [n for n in out[5:].splitlines() if n.strip()]
        listed = "\n".join(f"  - {n}" for n in names[:8])
        return (f"Several notes match {title!r} — which one?\n{listed}")
    name = out[3:] if out.startswith("OK:") else title
    return f"Added it to \"{name}\"."


@register(
    "scan_to_note",
    "Open Notes' document scanner (Continuity Camera) to scan a physical "
    "document into a new note. REQUIRES a paired iPhone or iPad nearby with "
    "Continuity Camera enabled — without one this just opens an empty "
    "scanner with nothing to capture.",
    {"type": "object", "properties": {}},
    category="notes_write",
    aliases=["scan this document into notes", "use my phone to scan this",
             "scan a document with continuity camera"],
)
def scan_to_note() -> str:
    script = (
        'tell application "Notes" to activate\n'
        'delay 0.3\n'
        'tell application "System Events" to tell process "Notes"\n'
        '  click menu item "Scan Documents…" of menu "File" of menu bar 1\n'
        'end tell')
    p = _osa(script)
    if p.returncode != 0:
        msg = (p.stderr or "").strip()
        if "-1719" in msg or "assistive" in msg.lower():
            return ("(Wisp needs Accessibility access to open the scanner. "
                    "Grant it in System Settings > Privacy & Security > "
                    "Accessibility, then try again.)")
        return f"(could not open the scanner: {msg})"
    return ("Opened the document scanner in Notes. This needs an iPhone or "
            "iPad nearby with Continuity Camera enabled — hold the document "
            "up to it to scan.")
