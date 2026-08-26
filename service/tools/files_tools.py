"""File search and bulk organization — Spotlight, not `find`/generated scripts.

THE MEASURED GAP THIS CLOSES
----------------------------
Across the 2026-08-18 debug exports `run_shell` was called 15 times and **10 of
those were a bare `find`** — hunting for Wisp's own logs, for model files, for
`.log` files by name. There was no file-search tool, so every "where is X"
question fell through to the shell. That was the single largest concrete
capability gap in the logs, and it is the one with numbers behind it.

Routing it through `mdfind` rather than `find` is the whole point:

  * Spotlight is indexed. `find / -name "*.log"` walks the filesystem and takes
    tens of seconds; mdfind answers from an index in milliseconds. The old
    shell path routinely hit run_shell's 120s timeout budget on home-directory
    scans.
  * Spotlight searches CONTENT, not just names. "the pdf about the lease" is a
    question `find` structurally cannot answer.
  * No shell quoting. A filename with a space, a quote or a `$` in it is a
    correctness problem for a generated `find` command and a non-event here.

`run_shell` stays available as the escape hatch — it is pinned into every
retrieved tool set — but it should no longer be the FIRST answer to "where is".
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from service.tools.registry import register

# Directories that are never what someone means by "my files", and that produce
# thousands of hits which crowd out the real answer. Caches and app bundles are
# the worst offenders: a search for "wisp" matches every build artifact in
# DerivedData before it reaches the user's actual document.
_NOISE = (
    "/Library/Caches/", "/Library/Containers/", "/Library/Application Support/",
    "/.Trash/", "/node_modules/", "/.git/", "/DerivedData/",
    "/Library/Developer/", "/.venv/", "/site-packages/", "/__pycache__/",
    "/Library/CloudStorage/.", "/Library/Group Containers/",
)

_MAX_HITS = 40


def _looks_noisy(path: str) -> bool:
    return any(seg in path for seg in _NOISE)


def _fmt(paths: list[str], *, truncated: bool) -> str:
    home = str(Path.home())
    lines = []
    for p in paths:
        try:
            st = os.stat(p)
            size = st.st_size
            unit = ("B", "KB", "MB", "GB")
            i = 0
            while size >= 1024 and i < 3:
                size /= 1024.0
                i += 1
            meta = f"{size:.0f}{unit[i]}"
        except OSError:
            meta = "?"
        lines.append(f"{p.replace(home, '~')}  ({meta})")
    out = "\n".join(lines)
    if truncated:
        out += (f"\n\n(showing the first {_MAX_HITS}; narrow with `folder` or a "
                f"more specific query if none of these are right)")
    return out


@register(
    "find_files",
    "Find files on the user's Mac by NAME or by CONTENT, using Spotlight. Use "
    "this for ANY 'where is / find / do I have a file' question — do NOT use "
    "run_shell with `find` for this, it is far slower and cannot search inside "
    "documents. `query` is what to look for ('lease', 'tax return', "
    "'screenshot'). Set `content=true` to search INSIDE documents rather than "
    "just filenames. Optionally restrict to `folder` (e.g. '~/Downloads') or a "
    "file `kind` ('pdf', 'image', 'folder').",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "What to look for — a name fragment or, with content=true, words inside the file."},
            "folder": {"type": "string",
                       "description": "Optional directory to search under, e.g. '~/Downloads'. Omit to search everywhere."},
            "kind": {"type": "string",
                     "description": "Optional file type filter: pdf, image, movie, audio, document, folder, app, text."},
            "content": {"type": "boolean",
                        "description": "Search inside file contents instead of filenames. Default false."},
        },
        "required": ["query"],
    },
    category="fs_read",
    aliases=[
        "where did I put my tax return",
        "find that pdf about the lease",
        "do I have a file called invoice somewhere",
        "where are my screenshots",
        "which folder is the presentation in",
        "find the document that mentions the security deposit",
    ],
)
def find_files(query: str, folder: str = "", kind: str = "",
               content: bool = False) -> str:
    query = (query or "").strip()
    if not query:
        return "(error: find_files needs a `query` — what should I look for?)"

    # mdfind's query language is its own thing, not shell — but the value still
    # has to survive being embedded in a quoted expression, so a literal double
    # quote in the user's query would otherwise terminate the predicate early.
    safe = query.replace('"', '\\"')
    if content:
        expr = f'kMDItemTextContent == "*{safe}*"cd'
    else:
        expr = f'kMDItemDisplayName == "*{safe}*"cd'

    kinds = {
        "pdf": "com.adobe.pdf", "image": "public.image", "movie": "public.movie",
        "audio": "public.audio", "text": "public.text", "folder": "public.folder",
        "app": "com.apple.application-bundle",
        "document": "public.content",
    }
    if kind:
        uti = kinds.get(kind.strip().lower())
        if uti is None:
            return (f"(error: unknown kind {kind!r}. Use one of: "
                    f"{', '.join(sorted(kinds))}.)")
        expr = f'({expr}) && (kMDItemContentTypeTree == "{uti}")'

    argv = ["mdfind", expr]
    if folder:
        root = Path(folder).expanduser()
        if not root.is_dir():
            return f"(error: {folder} is not a folder on this Mac)"
        argv = ["mdfind", "-onlyin", str(root), expr]

    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return ("(Spotlight took too long to answer. Try narrowing the search "
                "with `folder`.)")
    if p.returncode != 0:
        return f"(error running Spotlight: {p.stderr.strip() or 'unknown error'})"

    hits = [h for h in p.stdout.splitlines() if h.strip()]
    clean = [h for h in hits if not _looks_noisy(h)]
    # If filtering removed everything, the noise WAS the answer (someone
    # genuinely searching inside ~/Library). Better to show it than to claim
    # nothing exists.
    shown = clean or hits
    if not shown:
        where = f" under {folder}" if folder else ""
        scope = "contents" if content else "names"
        return (f"No files{where} matched {query!r} by {scope}. "
                f"{'Try content=true to search inside documents.' if not content else ''}").strip()

    truncated = len(shown) > _MAX_HITS
    return _fmt(shown[:_MAX_HITS], truncated=truncated)


# ==========================================================================
# organize_files — replaces the 4-call chain the diagnosis measured
# ==========================================================================
# 2026-08-18 debug logs: "reorganize my downloads folder" and "separate my
# wisp debug logs by date" both went list_dir -> write_code (generate a bash
# script) -> write_file (save it to ~/reorganize_wisp_debug.sh) -> run_shell
# (execute it) — four tool calls, ~9s of generation, and a leftover script
# file on disk to do a glob-and-move. This does the same job in one call, with
# a dry-run preview by default so a bad glob is visible before anything moves.
import fnmatch
import shutil


@register(
    "organize_files",
    "Move every file matching a glob pattern into a destination folder — 'put "
    "all the screenshots in one place', 'sort these by extension'. Creates the "
    "destination if needed. DEFAULTS TO A DRY RUN (preview only, nothing "
    "moves) — call again with `confirm=true` once the preview looks right. "
    "For moving ONE specific file, use move_path instead.",
    {
        "type": "object",
        "properties": {
            "pattern": {"type": "string",
                       "description": "Glob pattern, e.g. '*.png', 'Screenshot*', 'wisp-debug-*.json'."},
            "folder": {"type": "string", "description": "Folder to search in, e.g. '~/Downloads'."},
            "destination": {"type": "string", "description": "Folder to move matches into."},
            "confirm": {"type": "boolean",
                        "description": "Set true to actually move the files. False (default) previews only."},
        },
        "required": ["pattern", "folder", "destination"],
    },
    category="fs_write",
    aliases=["put all the screenshots in one folder", "sort these files by type",
             "gather all the pdfs into one place", "group my downloads by extension",
             "move all the wisp logs into a logs folder"],
)
def organize_files(pattern: str, folder: str, destination: str,
                   confirm: bool = False) -> str:
    src_dir = Path(folder).expanduser()
    if not src_dir.is_dir():
        return f"(error: {folder} is not a folder on this Mac.)"
    dst_dir = Path(destination).expanduser()

    matches = sorted(p for p in src_dir.iterdir()
                     if p.is_file() and fnmatch.fnmatch(p.name, pattern))
    if not matches:
        return f"No files in {folder} match {pattern!r}."

    if not confirm:
        listed = "\n".join(f"  {p.name}" for p in matches[:20])
        more = f"\n  (+{len(matches) - 20} more)" if len(matches) > 20 else ""
        return (f"Would move {len(matches)} file(s) from {folder} to "
                f"{destination}:\n{listed}{more}\n\n"
                f"Call again with confirm=true to actually move them.")

    dst_dir.mkdir(parents=True, exist_ok=True)
    moved, skipped = [], []
    for p in matches:
        target = dst_dir / p.name
        if target.exists():
            skipped.append(p.name)
            continue
        try:
            shutil.move(str(p), str(target))
            moved.append(p.name)
        except OSError as e:
            skipped.append(f"{p.name} ({e})")
    out = f"Moved {len(moved)} file(s) to {destination}."
    if skipped:
        out += f"\nSkipped {len(skipped)} (already exist at destination or errored): " + ", ".join(skipped[:10])
    return out


@register(
    "trash_file",
    "Move a file or folder to the Trash — recoverable, unlike delete_path "
    "(files) which is permanent. Use this for ordinary 'delete/remove/get rid "
    "of' requests; it's the safer default.",
    {"type": "object",
     "properties": {"path": {"type": "string", "description": "The file or folder to trash."}},
     "required": ["path"]},
    category="fs_write",
    aliases=["throw this away", "get rid of this file", "move this to the trash",
             "delete this folder", "trash the old screenshots"],
)
def trash_file(path: str) -> str:
    from service.tools.builtin import _wrong_account_path

    if (msg := _wrong_account_path(path)):
        return msg
    p = Path(path).expanduser()
    if not p.exists():
        return f"(no such path: {p})"
    script = (f'tell application "Finder" to delete POSIX file "{p}"')
    result = subprocess.run(["osascript", "-e", script], capture_output=True,
                            text=True, timeout=20)
    if result.returncode != 0:
        return f"(could not move {p.name} to Trash: {(result.stderr or '').strip()})"
    return f"Moved {p.name} to Trash."


@register(
    "create_folder",
    "Create a new empty folder. move_path already creates intermediate "
    "folders on its own, so use this only when the user wants an EMPTY folder "
    "with nothing to put in it yet.",
    {"type": "object",
     "properties": {"path": {"type": "string", "description": "The folder to create, e.g. '~/Documents/Taxes 2026'."}},
     "required": ["path"]},
    category="fs_write",
    aliases=["make a new folder called Taxes", "create a folder for this project",
             "set up a folder on my desktop"],
)
def create_folder(path: str) -> str:
    from service.tools.builtin import _wrong_account_path

    if (msg := _wrong_account_path(path)):
        return msg
    p = Path(path).expanduser()
    if p.exists():
        return f"({p} already exists.)" if p.is_dir() else f"(error: {p} already exists and is a file.)"
    try:
        p.mkdir(parents=True)
        return f"Created {p}."
    except OSError as e:
        return f"(error creating {p}: {e})"


@register(
    "archive_files",
    "Compress files or a folder into a .zip archive. Use for 'zip these up' "
    "or before sharing/backing up a set of files.",
    {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Files/folders to include."},
            "archive_path": {"type": "string",
                             "description": "Where to save the .zip. Defaults to a name next to the first item."},
        },
        "required": ["paths"],
    },
    category="fs_write",
    aliases=["zip up these files", "compress this folder", "make a zip archive of these",
             "package these files together"],
)
def archive_files(paths: list[str], archive_path: str = "") -> str:
    from service.tools.builtin import _wrong_account_path

    if not paths:
        return "(error: archive_files needs at least one path.)"
    resolved = []
    for raw in paths:
        if (msg := _wrong_account_path(raw)):
            return msg
        p = Path(raw).expanduser()
        if not p.exists():
            return f"(no such path: {p})"
        resolved.append(p)

    if archive_path.strip():
        out = Path(archive_path).expanduser()
        if out.suffix != ".zip":
            out = out.with_suffix(".zip")
    else:
        out = resolved[0].parent / f"{resolved[0].stem or resolved[0].name}.zip"
    if out.exists():
        return f"(refusing to overwrite {out} — it already exists. Pick a different name.)"

    # `ditto` (not zipfile/shutil.make_archive) preserves macOS resource forks
    # and extended attributes the way Finder's own "Compress" does — a plain
    # zipfile write silently drops them.
    argv = ["ditto", "-c", "-k", "--sequesterRsrc", *[str(p) for p in resolved], str(out)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return f"(error creating archive: {(result.stderr or '').strip()})"
    return f"Created {out} ({out.stat().st_size // 1024}KB)."


@register(
    "airdrop_file",
    "Share a file via AirDrop — opens the macOS AirDrop sharing sheet for the "
    "user to pick a nearby device. Wisp cannot pick the recipient or complete "
    "the transfer itself; the user finishes it in the sheet that opens.",
    {"type": "object",
     "properties": {"path": {"type": "string", "description": "The file to share."}},
     "required": ["path"]},
    category="fs_read",
    aliases=["airdrop this to my phone", "share this file with my ipad via airdrop",
             "share this with my ipad"],
)
def airdrop_file(path: str) -> str:
    from service.tools.builtin import _wrong_account_path

    if (msg := _wrong_account_path(path)):
        return msg
    p = Path(path).expanduser()
    if not p.exists():
        return f"(no such path: {p})"
    result = subprocess.run(["open", "-a", "Finder", "-R", str(p)],
                            capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        return f"(could not reveal {p.name}: {(result.stderr or '').strip()})"
    # No AppleScript verb opens the AirDrop sheet directly for an arbitrary
    # file — the reliable path is revealing it selected in Finder, where the
    # user invokes Share > AirDrop themselves (⌘-clicking the file then using
    # the Share menu, or the Finder toolbar Share button).
    return (f"Selected {p.name} in Finder — use the Share button (or "
            f"right-click > Share > AirDrop) to send it.")


_FINDER_LABELS = {"none": 0, "red": 2, "orange": 1, "yellow": 3, "green": 6,
                  "blue": 4, "purple": 5, "gray": 7, "grey": 7}


@register(
    "tag_file",
    "Set a Finder color label on a file or folder — the same colored tags "
    "Finder itself shows. Use 'none' to remove a label.",
    {"type": "object",
     "properties": {
         "path": {"type": "string", "description": "The file or folder to tag."},
         "color": {"type": "string",
                   "enum": ["none", "red", "orange", "yellow", "green", "blue", "purple", "gray"]},
     },
     "required": ["path", "color"]},
    category="fs_write",
    aliases=["tag this file red", "put a green label on this folder",
             "remove the color tag from this file", "mark this blue in finder"],
)
def tag_file(path: str, color: str) -> str:
    from service.tools.builtin import _wrong_account_path

    if (msg := _wrong_account_path(path)):
        return msg
    p = Path(path).expanduser()
    if not p.exists():
        return f"(no such path: {p})"
    idx = _FINDER_LABELS.get((color or "").strip().lower())
    if idx is None:
        return f"(error: unknown color {color!r}. Use: {', '.join(_FINDER_LABELS)}.)"
    script = (f'tell application "Finder" to set label index of '
             f'(POSIX file "{str(p)}" as alias) to {idx}')
    result = subprocess.run(["osascript", "-e", script], capture_output=True,
                            text=True, timeout=15)
    if result.returncode != 0:
        return f"(could not tag {p.name}: {(result.stderr or '').strip()})"
    return f"Tagged {p.name} {color}." if idx else f"Removed the label from {p.name}."


@register(
    "backup_folder",
    "Copy a folder to a backup destination, preserving everything (resource "
    "forks, extended attributes, permissions) the way Finder's own copy "
    "does. Not incremental — each call is a fresh full copy.",
    {"type": "object",
     "properties": {
         "source": {"type": "string", "description": "Folder to back up."},
         "destination": {"type": "string", "description": "Where the backup should go."},
     },
     "required": ["source", "destination"]},
    category="fs_write",
    aliases=["back up my documents folder", "make a backup copy of this folder",
             "copy this project folder somewhere safe"],
)
def backup_folder(source: str, destination: str) -> str:
    from service.tools.builtin import _wrong_account_path

    for raw in (source, destination):
        if (msg := _wrong_account_path(raw)):
            return msg
    src = Path(source).expanduser()
    if not src.is_dir():
        return f"(error: {source} is not a folder.)"
    dst = Path(destination).expanduser()
    if dst.exists():
        return f"(refusing to overwrite {dst} — it already exists. Pick a different destination.)"
    result = subprocess.run(["ditto", str(src), str(dst)], capture_output=True,
                            text=True, timeout=300)
    if result.returncode != 0:
        return f"(backup failed: {(result.stderr or '').strip()})"
    return f"Backed up {src.name} to {dst}."


# Built-in `textutil` covers rich-text-family conversions; `sips` covers
# images. Between them this is the real, always-available subset — no pandoc,
# no third-party dependency, matching every other tool's "what's actually on
# this Mac" constraint.
_TEXTUTIL_FORMATS = {"txt", "html", "rtf", "rtfd", "doc", "docx", "wordml", "odt"}
_SIPS_FORMATS = {"jpeg", "jpg", "png", "tiff", "gif", "bmp", "heic"}


@register(
    "convert_file",
    "Convert a document or image to a different format — txt/rtf/doc/docx/"
    "html/odt for documents (via textutil), jpeg/png/tiff/gif/bmp/heic for "
    "images (via sips). No pandoc, so PDF and markdown conversion aren't "
    "covered — those need the pdf/docx skills instead.",
    {"type": "object",
     "properties": {
         "path": {"type": "string", "description": "The file to convert."},
         "to_format": {"type": "string", "description": "Target format, e.g. 'docx', 'png'."},
     },
     "required": ["path", "to_format"]},
    category="fs_write",
    aliases=["convert this to a docx", "turn this image into a png",
             "save this as plain text", "convert this heic photo to jpeg"],
)
def convert_file(path: str, to_format: str) -> str:
    from service.tools.builtin import _wrong_account_path

    if (msg := _wrong_account_path(path)):
        return msg
    p = Path(path).expanduser()
    if not p.exists():
        return f"(no such file: {p})"
    fmt = (to_format or "").strip().lower().lstrip(".")
    out = p.with_suffix(f".{fmt}")
    if out.exists():
        return f"(refusing to overwrite {out} — it already exists.)"

    if fmt in _SIPS_FORMATS:
        sips_fmt = "jpeg" if fmt == "jpg" else fmt
        result = subprocess.run(["sips", "-s", "format", sips_fmt, str(p),
                                 "--out", str(out)],
                                capture_output=True, text=True, timeout=60)
    elif fmt in _TEXTUTIL_FORMATS:
        result = subprocess.run(["textutil", "-convert", fmt, str(p), "-output", str(out)],
                                capture_output=True, text=True, timeout=60)
    else:
        return (f"(error: can't convert to {to_format!r} — no pandoc on this "
                f"Mac. Documents: {', '.join(sorted(_TEXTUTIL_FORMATS))}. "
                f"Images: {', '.join(sorted(_SIPS_FORMATS))}.)")
    if result.returncode != 0:
        return f"(conversion failed: {(result.stderr or '').strip()})"
    if not out.exists():
        return f"(conversion reported success but {out.name} wasn't created — check the source format is actually {p.suffix.lstrip('.')}.)"
    return f"Converted to {out.name}."
