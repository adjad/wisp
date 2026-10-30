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
import re
import shlex
import subprocess
from datetime import datetime
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

# Hits shown by default, and at most when the caller asks for more.
_DEFAULT_HITS = 15
_MAX_HITS = 40
# Hits whose dates are read to break ties; the rest keep their name order.
_DATED_HITS = 400
# Where people keep the files they mean: these rank above the rest of home.
_USER_PLACES = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents/com~apple~CloudDocs")


def _looks_noisy(path: str) -> bool:
    return any(seg in path for seg in _NOISE)


def _name_rank(path: str, query: str) -> int:
    """0 the name is the query, 1 it starts with it, 2 has it as a word,
    3 has it inside a word, 4 has every word of it in any order, 5 none of
    that (a content hit)."""
    name = Path(path).name.casefold()
    stem = Path(path).stem.casefold()
    q = query.casefold()
    if q in (name, stem):
        return 0
    if name.startswith(q):
        return 1
    if re.search(rf"(?<![^\W_]){re.escape(q)}(?![^\W_])", name):
        return 2
    if q in name:
        return 3
    words = _words(q)
    return 4 if len(words) > 1 and all(w in name for w in words) else 5


def _place_rank(path: str, home: str) -> int:
    if any(path.startswith(f"{home}/{place}/") for place in _USER_PLACES):
        return 0
    return 1 if path.startswith(home + "/") else 2


def rank_hits(hits: list[str], query: str) -> list[str]:
    """Spotlight's hits, best first (2026-10-29 Ask eval: they came in mdfind's
    own order, so a stray match buried the file meant). By how well the name
    matches, then the user's own folders, then the most recently modified,
    then the shallowest path."""
    home = str(Path.home())
    keyed = sorted(hits, key=lambda h: (_name_rank(h, query), _place_rank(h, home), h.count("/")))

    def modified(path: str) -> float:
        try:
            return os.stat(path).st_mtime
        except OSError:
            return 0.0

    head = keyed[:_DATED_HITS]
    head.sort(key=lambda h: (_name_rank(h, query), _place_rank(h, home), -modified(h), h.count("/")))
    return head + keyed[_DATED_HITS:]


def _words(query: str) -> list[str]:
    return [w for w in re.split(r"[\s_\-.]+", query) if w]


def _mdfind_literal(text: str) -> str:
    """`text` safe inside a quoted mdfind value: a backslash is dropped (a
    trailing one would escape the closing quote) and a double quote escaped."""
    return text.replace("\\", "").replace('"', '\\"')


def _fmt(paths: list[str], *, total: int) -> str:
    home = str(Path.home())
    lines = []
    for p in paths:
        try:
            st = os.stat(p)
            modified = f"modified {datetime.fromtimestamp(st.st_mtime).strftime('%b %-d, %Y')}"
            if os.path.isdir(p) and not p.endswith((".app", ".bundle")):
                # A folder's own st_size is its directory entry, not what's in it;
                # list_dir(details=true) totals a folder.
                meta = f"folder, {modified}"
            else:
                size = st.st_size
                unit = ("B", "KB", "MB", "GB")
                i = 0
                while size >= 1024 and i < 3:
                    size /= 1024.0
                    i += 1
                meta = f"{size:.0f}{unit[i]}, {modified}"
        except OSError:
            meta = "?"
        lines.append(f"{p.replace(home, '~')}  ({meta})")
    out = "\n".join(lines)
    if total > len(paths):
        # The real count, so "how many …" isn't answered with the cap.
        out += (f"\n\n(+{total - len(paths)} more: Spotlight found {total} matches and these are "
                f"the best {len(paths)}, closest name match first, then your own folders, then newest. "
                f"Narrow with `folder`, `kind` or a more specific query if none of these are right.)")
    return out


@register(
    "find_files",
    "Find files on the user's Mac by NAME or by CONTENT, using Spotlight. Use "
    "this for ANY 'where is / find / do I have a file' question — do NOT use "
    "run_shell with `find` for this, it is far slower and cannot search inside "
    "documents. `query` is what to look for ('lease', 'tax return', "
    "'screenshot'). Set `content=true` to search INSIDE documents rather than "
    "just filenames. Optionally restrict to `folder` (e.g. '~/Downloads') or a "
    "file `kind` ('pdf', 'image', 'folder'). Hits come best first (closest name "
    "match, then the user's own folders, then most recently modified) with the "
    "total Spotlight found.",
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
            "limit": {"type": "integer",
                      "description": f"How many of the best hits to show (default {_DEFAULT_HITS}, at most {_MAX_HITS})."},
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
               content: bool = False, limit: int = _DEFAULT_HITS) -> str:
    query = (query or "").strip()
    if not query:
        return "(error: find_files needs a `query` — what should I look for?)"

    # mdfind's query language is its own thing, not shell — but the value still
    # has to survive being embedded in a quoted expression, so a literal double
    # quote in the user's query would otherwise terminate the predicate early.
    # Each word on its own, so "tax return 2025" finds "2025 Tax Return.pdf";
    # rank_hits puts the names with the exact phrase first.
    attribute = "kMDItemTextContent" if content else "kMDItemDisplayName"
    words = _words(query) or [query]
    expr = " && ".join(f'{attribute} == "*{_mdfind_literal(w)}*"cd' for w in words)

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

    try:
        top = max(1, min(int(limit or _DEFAULT_HITS), _MAX_HITS))
    except (TypeError, ValueError):
        top = _DEFAULT_HITS
    ranked = rank_hits(shown, query)
    return _fmt(ranked[:top], total=len(shown))


# ==========================================================================
# organize_files — replaces the 4-call chain the diagnosis measured
# ==========================================================================
# 2026-08-18 debug logs: "reorganize my downloads folder" and "separate my
# wisp debug logs by date" both went list_dir -> write_code (generate a bash
# script) -> write_file (save it to ~/reorganize_wisp_debug.sh) -> run_shell
# (execute it) — four tool calls, ~9s of generation, and a leftover script
# file on disk to do a glob-and-move. This does the same job in one call, with
# a dry-run preview by default so a bad glob is visible before anything moves.
import json
import secrets
import threading
import time

from service.file_organization import OrganizationPlan, build_plan, execute_plan

# Preview capabilities are process-local, short-lived, and single-use. A changed
# source snapshot requires a new preview; a guessed confirm=true is insufficient.
_MOVE_PREVIEWS: dict[str, tuple[float, OrganizationPlan]] = {}
_MOVE_PREVIEW_LOCK = threading.Lock()


def move_preview_text(token: str) -> str:
    with _MOVE_PREVIEW_LOCK:
        prior = _MOVE_PREVIEWS.get(token)
    if not prior or time.monotonic() - prior[0] > 600:
        return "File preview expired or missing; no files will move."
    plan = prior[1]
    return (f"Move {len(plan.moves)} file(s)\nFrom: {plan.folder}\nTo: {plan.destination}\n"
            + "\n".join(f"{json.dumps(move.source)} -> {json.dumps(move.destination)} "
                        f"(SHA-256: {move.identity.digest})" for move in plan.moves)
            + ("\nLeft untouched:\n" + "\n".join(f"{json.dumps(path)}: {reason}" for path, reason in plan.skipped)
               if plan.skipped else "")
            + ("\nExisting destinations will be skipped; nothing will replace them:\n"
               + "\n".join(json.dumps(path) for path in plan.existing_targets)
               if plan.existing_targets else "")
            + "\nExact-byte moves require source entries to stay stable through execution. "
              "A late source replacement may be moved and reported unknown; never retry unknown results automatically.")


@register(
    "organize_files",
    "Prepare source-to-target moves for immediate regular files matching "
    "a glob. Optional group_by=extension uses uppercase extension folders "
    "(NO_EXTENSION otherwise); debug_date uses valid YYYY-MM-DD in debug "
    "filenames, leaving unclassified files untouched. No semantic/content "
    "categorization. DEFAULT preview makes no filesystem changes. Confirm "
    "requires the same inputs and a fresh single-use preview_token. No existing "
    "target is replaced; cross-volume moves are unsupported. Partial/unknown "
    "results are disclosed. Exact bytes require stable source entries through "
    "rename; late replacement may move an unapproved entry and report unknown. "
    "Never retry unknown results automatically. Limited to 1000 files/512 MiB per preview.",
    {
        "type": "object",
        "properties": {
            "pattern": {"type": "string",
                       "description": "Glob pattern, e.g. '*.png', 'Screenshot*', 'wisp-debug-*.json'."},
            "folder": {"type": "string", "description": "Folder to search in, e.g. '~/Downloads'."},
            "destination": {"type": "string", "description": "Target folder; grouping parent when group_by is set. Use '.' to group inside source."},
            "group_by": {"type": "string", "enum": ["", "extension", "debug_date"],
                         "description": "Empty (default), extension, or debug_date. Dates come from filenames, never modification time."},
            "confirm": {"type": "boolean",
                        "description": "Set true to actually move the files. False (default) previews only."},
            "preview_token": {"type": "string", "description": "Token returned by the matching preview; required when confirm=true."},
        },
        "required": ["pattern", "folder", "destination"],
    },
    category="fs_write",
    aliases=["put all the screenshots in one folder", "sort these files by type",
             "gather all the pdfs into one place", "group my downloads by extension",
             "move all the wisp logs into a logs folder"],
)
def organize_files(pattern: str, folder: str, destination: str,
                   confirm: bool = False, preview_token: str = "", group_by: str = "") -> str:
    now = time.monotonic()
    with _MOVE_PREVIEW_LOCK:
        for token, (created, _) in list(_MOVE_PREVIEWS.items()):
            if now - created > 600:
                _MOVE_PREVIEWS.pop(token, None)
        if confirm:
            prior = _MOVE_PREVIEWS.pop(preview_token, None)
            if not prior:
                return "(error: missing, expired or changed file preview. Preview again before moving any files.)"
    try:
        plan = build_plan(pattern, folder, destination, group_by)
    except (OSError, ValueError) as exc:
        prefix = "missing, expired or changed file preview; " if confirm else ""
        return f"(error: {prefix}{exc}. No files moved.)"
    if confirm and prior[1] != plan:
        return "(error: missing, expired or changed file preview. Preview again before moving any files.)"
    if not plan.moves:
        note = ("\nLeft untouched:\n" + "\n".join(f"{json.dumps(path)}: {reason}" for path, reason in plan.skipped)
                if plan.skipped else "")
        return f"No files in {folder} match the requested move classification.{note}"
    if not confirm:
        preview_token = secrets.token_urlsafe(24)
        with _MOVE_PREVIEW_LOCK:
            if len(_MOVE_PREVIEWS) >= 1024:
                oldest = min(_MOVE_PREVIEWS, key=lambda token: _MOVE_PREVIEWS[token][0])
                _MOVE_PREVIEWS.pop(oldest)
            _MOVE_PREVIEWS[preview_token] = (now, plan)
        return (f"Would move {len(plan.moves)} file(s); preview only, no filesystem changes.\n"
                f"{move_preview_text(preview_token)}\n\nPreview token: {preview_token}\n"
                "Call again with the same inputs, confirm=true and preview_token to attempt these approved moves.")
    receipts = execute_plan(plan)
    moved = sum(row["status"] == "moved" for row in receipts)
    incomplete = any(row["status"] != "moved" for row in receipts)
    if incomplete:
        heading = (f"(error: file organization incomplete; moved {moved} of {len(plan.moves)} planned files. "
                   "Count includes only verified moves; unknown results may already have changed paths. "
                   "Read each receipt before retrying; unknown results must not be retried automatically.)")
    else:
        heading = f"Moved {moved} file(s) to {plan.destination}."
    return heading + "\nFile receipts:\n" + "\n".join(json.dumps(row, sort_keys=True) for row in receipts)


_FINDER_LABELS = {"none": 0, "red": 2, "orange": 1, "yellow": 3, "green": 6,
                  "blue": 4, "purple": 5, "gray": 7, "grey": 7}


# Built-in `textutil` covers rich-text-family conversions; `sips` covers
# images. Between them this is the real, always-available subset — no pandoc,
# no third-party dependency, matching every other tool's "what's actually on
# this Mac" constraint.
_TEXTUTIL_FORMATS = {"txt", "html", "rtf", "rtfd", "doc", "docx", "wordml", "odt"}
_SIPS_FORMATS = {"jpeg", "jpg", "png", "tiff", "gif", "bmp", "heic"}


