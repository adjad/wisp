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

import re
import subprocess

from service.tools.registry import not_done, register

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


_BULLET = re.compile(r"^\s*[-*•]\s+(\S.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(\S.*)$")


def _item_text(line: str) -> str:
    """One list line without its "- " / "* " / "• " / "1. " marker."""
    m = _BULLET.match(line) or _NUMBERED.match(line)
    return (m.group(1) if m else line).strip()


def _body_html(text: str) -> str:
    """A note body: "- " / "* " / "• " lines become <ul> items and "1. " lines
    <ol> items, with the marker dropped. Left as text, Notes showed it beside
    its own bullet ("• - Demo point 1"). Other lines keep the <br> rule above."""
    parts: list[str] = []
    plain: list[str] = []
    items: list[str] = []
    kind = ""

    def close_list():
        nonlocal kind
        if kind:
            parts.append(f"<{kind}>" + "".join(f"<li>{i}</li>" for i in items) + f"</{kind}>")
            items.clear()
            kind = ""

    for line in text.split("\n"):
        m_b, m_n = _BULLET.match(line), _NUMBERED.match(line)
        this = "ul" if m_b else "ol" if m_n else ""
        if not this:
            close_list()
            plain.append(line)
            continue
        if plain:
            parts.append(_html("\n".join(plain)))
            plain.clear()
        if kind != this:
            close_list()
            kind = this
        items.append(_html((m_b or m_n).group(1).strip()))
    close_list()
    if plain:
        parts.append(_html("\n".join(plain)))
    return "".join(parts)


_BULLET_ASK = re.compile(r"\b(?:bullets?|bullet\s+points?|list\s+items?|checklist\s+items?)\b", re.I)


def _asked_for_bullet() -> bool:
    """The person's own message this turn asked for a bullet or list item."""
    from service.tools.self_send import turn_text
    return bool(_BULLET_ASK.search(turn_text()))


_LIST_ASK = re.compile(r"\b(?:bullets?|bullet\s+points?|(?:check)?list|list\s+items?)\b", re.I)
_LIST_JOIN = re.compile(r"\s*[,;]\s*(?:and\s+)?|\s+and\s+", re.I)


def _split_one_list_line(body: str) -> str:
    """"Alpha, Beta, Gamma" as one "- " line each, when the person's own
    message this turn asked for bullets or a list and the body is that single
    run ("…with three bullet points: Alpha, Beta, Gamma" was written as ONE
    bullet, 2026-11-13 real-model run). Anything else is returned unchanged:
    several lines, a sentence, or a turn that asked for no list."""
    from service.tools.self_send import turn_text
    lines = [line for line in (body or "").split("\n") if line.strip()]
    if len(lines) != 1 or not _LIST_ASK.search(turn_text()):
        return body
    items = [i.strip() for i in _LIST_JOIN.split(_item_text(lines[0]).rstrip(".")) if i.strip()]
    if len(items) < 2 or any(len(i.split()) > 5 for i in items):
        return body
    return "\n".join(f"- {i}" for i in items)


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
    "Create a NEW note in the user's Notes.app: 'take a note', 'jot this down', 'save this to "
    "my notes', a list. Still a note when it mentions a day or time (not add_reminder), and "
    "not remember or write_file. `checklist=true` formats one item per line.",
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
        return not_done("(error: create_note needs a `title`.)",
                        question="What should the note be called?")

    body = _split_one_list_line(body)
    if checklist and body.strip():
        items = "".join(f"<li>{_html(_item_text(ln))}</li>"
                        for ln in body.splitlines() if ln.strip())
        body_html = f"<ul>{items}</ul>"
    else:
        body_html = _body_html(body)
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
        # The script returned before its `make new note`.
        return not_done(f"(There's no Notes folder called {folder!r}. "
                        f"Leave `folder` out to use the default folder.)",
                        question=f"There's no Notes folder called “{folder}”, so I didn't create the note. "
                                 "Should I use the default folder?")
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
            "bullet": {"type": "boolean", "description": "true when the user asked for a bullet or list item."},
        },
        "required": ["title", "text"],
    },
    category="notes_write",
    aliases=["add milk to my shopping list", "put that on my packing list",
             "add a line to the meeting note", "tack this onto my ideas note"],
)
def append_note(title: str, text: str, bullet: bool = False) -> str:
    title = (title or "").strip()
    text = (text or "").strip()
    # A bullet is a real list item. "Add one more bullet to that note" sent
    # "<div>Demo point 4</div>", a plain line, and Wisp called it a bullet
    # (2026-11-13 real-model run). When the note ends in a list, the new lines
    # join that list; when a bullet was asked for, they start one; otherwise
    # they stay the plain lines they were.
    lines = [line for line in text.split("\n") if line.strip()]
    items = "".join(f"<li>{_html(_item_text(line))}</li>" for line in lines)
    if (bullet is True or _asked_for_bullet()) and not any(
            _BULLET.match(line) or _NUMBERED.match(line) for line in lines):
        text = "\n".join(f"- {line.strip()}" for line in lines)
    if not title:
        return not_done("(error: append_note needs the `title` of the note to add to.)",
                        question="Which note should I add that to?")
    if not text:
        return not_done("(error: append_note needs `text` to add.)",
                        question="What should I add to the note?")

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
        f'  set b to body of target\n'
        f'  set e to b\n'
        f'  repeat while (length of e) > 1 and (e ends with linefeed or e ends with return or e ends with " ")\n'
        f'    set e to text 1 thru -2 of e\n'
        f'  end repeat\n'
        f'  set closer to ""\n'
        f'  repeat with c in {{"</ul></div>", "</ol></div>", "</ul>", "</ol>"}}\n'
        f'    if closer is "" and e ends with (c as text) then set closer to (c as text)\n'
        f'  end repeat\n'
        f'  if closer is not "" and (length of e) > (length of closer) then\n'
        f'    set body of target to (text 1 thru -((length of closer) + 1) of e) & "{_esc(items)}" & closer\n'
        f'  else\n'
        f'    set body of target to b & "<div>{_esc(_body_html(text))}</div>"\n'
        f'  end if\n'
        f'  return "OK:" & (name of target)\n'
        f'end tell')

    p = _osa(script)
    if (e := _err(p)) is not None:
        return e
    out = p.stdout.strip()
    # NONE and MANY: the script returned before its `set body`.
    if out == "NONE":
        return not_done(f"No note has {title!r} in its title. Use create_note to start one.",
                        question=f"I couldn't find a note called “{title}”, so nothing was added. "
                                 "Should I start a new one?")
    if out.startswith("MANY:"):
        names = [n for n in out[5:].splitlines() if n.strip()]
        listed = "\n".join(f"  - {n}" for n in names[:8])
        return not_done(f"Several notes match {title!r} — which one?\n{listed}",
                        question=f"Several notes match “{title}”, so nothing was added. Which one?\n{listed}")
    name = out[3:] if out.startswith("OK:") else title
    return f"Added it to \"{name}\"."




def delete_note_reason(title: str, folder: str = "") -> str:
    """The confirmation card's line for delete_note (policy.decide and the
    card's folder lookup both use it, so they cannot word it differently)."""
    title = " ".join(str(title or "").split())[:120]
    folder = " ".join(str(folder or "").split())[:80]
    where = f"from the “{folder}” folder" if folder else "from Notes"
    return (f"deletes the note “{title}” {where}. Notes keeps it in Recently "
            "Deleted for 30 days — always confirmed")


def _find_notes(title: str, folder: str):
    """Notes whose title is exactly `title` (case-insensitive), outside
    Recently Deleted: a list of (note id, folder id, folder name, title), or
    "NOFOLDER", or an error string."""
    row = ('    set out to out & (id of n) & tab & (id of f) & tab & (name of f) '
           '& tab & (name of n) & linefeed\n')
    if folder:
        script = (f'tell application "Notes"\n'
                  f'  if not (exists folder "{_esc(folder)}") then return "NOFOLDER"\n'
                  f'  set f to folder "{_esc(folder)}"\n'
                  f'  set out to ""\n'
                  f'  repeat with n in (every note of f whose name is "{_esc(title)}")\n'
                  f'{row}'
                  f'  end repeat\n'
                  f'  return "HITS:" & out\n'
                  f'end tell')
    else:
        script = (f'tell application "Notes"\n'
                  f'  set out to ""\n'
                  f'  repeat with n in (every note whose name is "{_esc(title)}")\n'
                  f'    set f to container of n\n'
                  f'    if (name of f) is not "Recently Deleted" then\n'
                  f'  {row}'
                  f'    end if\n'
                  f'  end repeat\n'
                  f'  return "HITS:" & out\n'
                  f'end tell')
    p = _osa(script)
    if (e := _err(p)) is not None:
        return e
    out = p.stdout.strip("\n")
    if out.strip() == "NOFOLDER":
        return "NOFOLDER"
    if not out.startswith("HITS:"):
        return f"(error talking to Notes: unexpected reply {out[:80]!r})"
    hits = []
    for line in out[5:].splitlines():
        parts = line.split("\t")
        # Exact title only: never a note that merely contains the words.
        if len(parts) == 4 and parts[3].strip().casefold() == title.casefold():
            hits.append(tuple(parts))
    return hits


def delete_note_card(args: dict) -> tuple[dict, str] | None:
    """For the confirmation card: the call bound to the one note's folder and
    a reason naming it, or None when there is not exactly one match (the tool
    then refuses after the card without deleting anything). Read-only."""
    title = str(args.get("title") or "").strip()
    folder = str(args.get("folder") or "").strip()
    if not title:
        return None
    hits = _find_notes(title, folder)
    if not isinstance(hits, list) or len(hits) != 1:
        return None
    return {**args, "folder": hits[0][2]}, delete_note_reason(hits[0][3], hits[0][2])


@register(
    "delete_note",
    "Delete one note, by exact title.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "The note's exact title."},
            "folder": {"type": "string", "description": "Optional folder to look in."},
        },
        "required": ["title"],
    },
    category="notes_write",
    aliases=["delete my packing list note", "remove the note called groceries",
             "get rid of the old meeting note", "trash my ideas note"],
)
def delete_note(title: str, folder: str = "") -> str:
    title = " ".join((title or "").split())
    folder = (folder or "").strip()
    if not title:
        return not_done("(error: delete_note needs the exact `title` of the note.)",
                        question="Which note should I delete?")

    hits = _find_notes(title, folder)
    if hits == "NOFOLDER":
        return not_done(f"(There's no Notes folder called {folder!r}. Nothing was deleted.)",
                        question=f"There's no Notes folder called “{folder}”, so I didn't delete anything. "
                                 "Which folder is the note in?")
    if isinstance(hits, str):
        # Only the lookup ran: nothing was deleted.
        return not_done(hits)
    where = f" in the “{folder}” folder" if folder else ""
    if not hits:
        return not_done(f"No note is titled exactly {title!r}{where}. Nothing was deleted.",
                        question=f"I couldn't find a note titled “{title}”{where}, so nothing was deleted. "
                                 "What is its exact title?")
    if len(hits) > 1:
        listed = "\n".join(f"  - {h[3]} (folder: {h[2]})" for h in hits[:8])
        return not_done(f"Several notes are titled {title!r}; nothing was deleted. Which one?\n{listed}",
                        question=f"Several notes are titled “{title}”, so nothing was deleted. "
                                 f"Which folder is the one to delete in?\n{listed}")

    note_id, folder_id, folder_name, name = hits[0]
    p = _osa(f'tell application "Notes"\n'
             f'  delete note id "{_esc(note_id)}"\n'
             f'  return "OK"\n'
             f'end tell')
    if (e := _err(p)) is not None:
        # The script may have stopped after the delete: say so, never not_done.
        return (f"{e} I can't tell whether “{name}” was deleted. "
                "Check Notes (and Recently Deleted) before trying again.")

    # Success only once a read-back shows the note has left that folder.
    v = _osa(f'tell application "Notes"\n'
             f'  if exists note id "{_esc(note_id)}" then\n'
             f'    if (id of container of note id "{_esc(note_id)}") is "{_esc(folder_id)}" then return "STILL"\n'
             f'  end if\n'
             f'  set out to ""\n'
             f'  repeat with n in (every note of folder id "{_esc(folder_id)}" whose name is "{_esc(name)}")\n'
             f'    set out to out & (name of n) & linefeed\n'
             f'  end repeat\n'
             f'  return "LEFT:" & out\n'
             f'end tell')
    left = v.stdout.strip("\n") if v.returncode == 0 else ""
    remaining = [n for n in left[5:].splitlines()
                 if n.strip().casefold() == name.strip().casefold()] if left.startswith("LEFT:") else None
    if remaining is None or remaining:
        return (f"(couldn't confirm the delete: Notes still lists “{name}” in {folder_name} "
                "or did not answer. Check Notes before trying again.)")
    return (f"Deleted the note “{name}” from {folder_name}. "
            "Notes keeps it in Recently Deleted for 30 days.")
