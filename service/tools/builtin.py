"""Built-in tools: shell + filesystem. Registered on import."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from service.tools.registry import register

MAX_OUTPUT = 12_000  # chars returned to the model

# Cap on pages actually extracted from a PDF. Bounds worst-case latency on a
# huge scanned report rather than the tool silently hanging — the model
# already gets everything up to MAX_OUTPUT chars, so a report over this many
# pages is telling the model to keep reading rather than losing content it
# would otherwise have seen (the char cap bites long before the page cap
# would on ordinary text-heavy PDFs; this exists for the pathological case).
_PDF_MAX_PAGES = 200


def _clip(s: str) -> str:
    return s if len(s) <= MAX_OUTPUT else s[:MAX_OUTPUT] + f"\n…[truncated {len(s)-MAX_OUTPUT} chars]"


# macOS blocks Documents / Desktop / Downloads / iCloud Drive per-app under
# TCC, and the block surfaces as a bare PermissionError from os.listdir/open.
# Unhandled, that propagates as a tool exception and the model reports it as
# though the folder were empty or missing — which is how "only a small portion
# of files can be accessed by Wisp" looks from the outside. Name the real cause
# and the exact fix instead: this is a grant the user has to give in System
# Settings; no retry, different path, or shell fallback can work around it.
def _tcc_message(p: Path, action: str) -> str:
    return (f"(macOS blocked {action} {p} — Wisp does not have permission for "
            "that folder. Grant it in System Settings > Privacy & Security > "
            "Full Disk Access by adding Wisp, then quit and reopen Wisp. "
            "Tell the user this; do NOT retry, and do NOT try to reach it with "
            "run_shell — the same block applies there.)")


def _missing_dir_message(p: Path) -> str:
    """A dead-end path, answered with the nearest folder that DOES exist.

    MEASURED FAILURE (2026-08-18). Asked to "reorganize my wisp debug logs",
    the model guessed `~/Wisp/logs`, got "(not a directory: …)", guessed
    `~/Wisp`, got the same, ran two `run_shell` finds, tried `~/DebugLogs`,
    found it empty, and concluded "no files to reorganize — it looks like there
    are no log files to sort". The logs were in `~/Downloads` the whole time.
    Five tool calls, a wrong answer, and it never once listed a real folder.

    SYSTEM already tells it what to do here — "If a path turns out not to
    exist, call `list_dir` on the PARENT folder to see the real names — do NOT
    switch to run_shell" — and it did exactly the forbidden thing. So walk up to
    the nearest existing ancestor and hand back that listing directly: the
    model does not have to decide to do it, and the names it needs are already
    on screen. Same principle as _wrong_account_path and timeranges.BadPeriod —
    a failure should teach the caller, not just report.

    NOT flagged as "(error" (unlike _wrong_account_path): this return carries
    real, useful directory content, so it should stay eligible for the
    narration fallback rather than being filtered out of it.
    """
    anc = p.parent
    while anc != anc.parent and not anc.is_dir():
        anc = anc.parent
    head = f"(no such directory: {p})"
    if not anc.is_dir():
        return head
    try:
        entries = sorted(os.listdir(anc))
    except OSError:
        return head
    shown = [e for e in entries if not e.startswith(".")][:60]
    listing = "\n".join(shown) or "(empty)"
    return _clip(f"{head}\n\nThe nearest folder that DOES exist is `{anc}` — "
                 f"here is what is actually in it. Pick the right name from "
                 f"this list and call list_dir again; do NOT guess another "
                 f"path and do NOT switch to run_shell:\n{listing}")


def _wrong_account_path(path: str) -> str | None:
    """A corrective message when `path` names a `/Users/<account>` that isn't
    the real one — otherwise None.

    MEASURED FAILURE (2026-08-18). Asked to "reorganize my files" — a file
    request that names no folder — Ling-3.0-tiny built `/Users/AdiJain888` out
    of the user's EMAIL ADDRESS, which the identity block puts in every agent
    system prompt (see agent/loop.py's identity_hint). It then tried
    `/Users/AdiJain` and `/Users/Adi`. 10 runs out of 10.

    SYSTEM already forbids exactly this — "you do not know the user's account
    name, so `/Users/<name>/…` is a guess and is usually wrong" — and the model
    ignores it; adding a second, stronger rule right next to the email only took
    it from 10/10 to 2/10, at 2.4x the tool calls. A prompt cannot close this.
    So the fix is deterministic and lives here, where the answer is KNOWN:
    Path.home() is ground truth, and no model has to infer it.

    Corrective rather than silent: the old return, "(not a directory: …)",
    states the failure without naming the fix, so the model just guesses
    another spelling of the username and burns its remaining steps. This names
    the exact replacement path, so a retry lands on the first attempt. Same
    principle tools/timeranges.BadPeriod already states: "a bad argument should
    teach the caller the vocabulary rather than just failing".

    Deliberately NOT auto-rewriting the path. Silently redirecting one location
    to a different one is the kind of guess that, on move_path or delete_path,
    would act on a file the user never named.
    """
    p = (path or "").strip()
    if not p.startswith("/Users/"):
        return None
    parts = p.split("/")                      # ['', 'Users', '<account>', ...]
    if len(parts) < 3 or not parts[2]:
        return None
    account = parts[2]
    # /Users/Shared is a real, account-independent macOS location.
    if account == Path.home().name or account == "Shared":
        return None
    rest = "/".join(parts[3:]).rstrip("/")
    suggested = f"~/{rest}" if rest else "~"
    # Leading "(error" on purpose: registry.is_tool_error keys off exactly that
    # marker, and the agent loop uses it to keep failures OUT of clean_results —
    # the set _merge_results renders when the model produces no answer of its
    # own. Without the marker this text was reaching the USER verbatim (measured
    # 2026-08-18: 6 of 10 fallback answers opened with it). The model still
    # sees it either way; tool results are fed back regardless of this flag.
    return (f"(error: no such path: {p} — `{account}` is NOT the user's account "
            f"name. Their email address is not their username, and you cannot "
            f"guess the account name. Use `{suggested}` instead: `~` always means "
            f"the user's home folder. Retry this call with `{suggested}`.)")


@register(
    "run_shell",
    # The "don't use this, use X instead" list used to name ~15 tools
    # individually. That was written when every request saw the whole registry;
    # with routes scoped it became actively WRONG on the narrow ones — on the
    # ambiguous core route it told the model to prefer get_volume, set_wifi,
    # spotify and a dozen others that are not on the table there, which is the
    # same trap as pointing at a deleted tool. Naming the CATEGORIES and
    # deferring to "if it's in your tool list" is correct on every route and
    # about half the size. The two facts that are not guessable — pmset's log
    # is not a health metric, and the keyboard backlight isn't scriptable at
    # all — are kept, because those are exactly where the model invents a
    # confident wrong answer.
    "Run a shell command on the user's Mac and return its stdout/stderr. "
    "Use for inspecting and operating the system. Prefer read-only commands; "
    "anything that changes state will require the user's confirmation. "
    "If a dedicated tool for the job is in your tool list — files and folders, "
    "device settings, clipboard, calendar/reminders, mail, messages, notes, "
    "media, or live web "
    "data — call that instead: it is faster, more reliable, and some have no "
    "working shell equivalent (pmset's output is PM event history, NOT battery "
    "health; the keyboard backlight is not scriptable at all).",
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


def _read_pdf(p: Path) -> str:
    """Extract real text from a PDF via pypdf.

    Reported live: read_file used to just call p.read_text() on every path, so
    a PDF handed back its raw bytes — compressed FlateDecode streams and object
    headers, not the document's actual words (e.g. "x\\x9c}\\xdb\\x8e\\x1c..."
    where the real content should be). The model wasn't failing to summarize;
    it was asked to summarize noise and had no way to say so, which is why
    "Summarize a PDF" stalled rather than erroring.
    """
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(str(p))
    except PdfReadError as e:
        return f"(couldn't parse {p} as a PDF: {e})"

    if reader.is_encrypted:
        # pypdf can sometimes still open an encrypted PDF with an empty owner
        # password (common for "restrict editing, not reading" exports) — try
        # that before giving up, rather than reporting unreadable when it isn't.
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            pass
    if reader.is_encrypted:
        return f"({p} is password-protected — Wisp can't read it without the password.)"

    pages = reader.pages[:_PDF_MAX_PAGES]
    texts = [(page.extract_text() or "").strip() for page in pages]
    text = "\n\n".join(t for t in texts if t)
    if not text:
        # extract_text() returns "" for a page with no text LAYER — a scanned
        # photo/print PDF with no OCR, not a Wisp failure. This used to point at
        # describe_image; that tool is gone with the vision path, so say plainly
        # that it can't be read. Naming a tool that no longer exists is worse
        # than admitting the limit: the model would call it, get "not
        # available", and report that as a Wisp malfunction.
        return (f"({p} has no extractable text — it is a scanned/image-only PDF "
                "with no text layer. Wisp cannot read scanned documents. Tell "
                "the user this plainly; do not retry with another tool.)")
    note = (f"[showing first {_PDF_MAX_PAGES} of {len(reader.pages)} pages]\n\n"
           if len(reader.pages) > _PDF_MAX_PAGES else "")
    return _clip(note + text)


def _read_docx(p: Path) -> str:
    """Extract paragraph + table text from a Word document.

    A .docx is a ZIP of XML parts, so read_file's old p.read_text() returned
    ZIP central-directory bytes — same failure as the PDF case, different
    binary format. python-docx unpacks it properly instead.
    """
    from docx import Document
    from docx.opc.exceptions import PackageNotFoundError

    try:
        doc = Document(str(p))
    except PackageNotFoundError:
        return f"(couldn't parse {p} as a .docx file — it may be corrupt or not a real Word document)"

    parts = [para.text for para in doc.paragraphs if para.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n".join(parts)
    if not text:
        return f"({p} has no readable text — it may be empty or contain only images.)"
    return _clip(text)


def _read_xlsx(p: Path) -> str:
    """Extract cell values from every sheet, as plain rows.

    Formulas are read as their CACHED VALUE (data_only=True) — the model wants
    what the spreadsheet currently shows, not the formula text, and Excel
    stores both in the file so this needs no evaluation of our own.
    """
    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        wb = load_workbook(str(p), data_only=True, read_only=True)
    except InvalidFileException:
        return f"(couldn't parse {p} as an .xlsx file — it may be corrupt or not a real Excel workbook)"

    sheets = []
    for name in wb.sheetnames:
        ws = wb[name]
        rows = []
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            sheets.append(f"[Sheet: {name}]\n" + "\n".join(rows))
    text = "\n\n".join(sheets)
    if not text:
        return f"({p} has no data in any sheet.)"
    return _clip(text)


def _read_pptx(p: Path) -> str:
    """Extract text from every shape/frame, slide by slide, plus speaker notes."""
    from pptx import Presentation
    from pptx.exc import PackageNotFoundError as PptxPackageNotFoundError

    try:
        deck = Presentation(str(p))
    except PptxPackageNotFoundError:
        return f"(couldn't parse {p} as a .pptx file — it may be corrupt or not a real PowerPoint file)"

    slides = []
    for i, slide in enumerate(deck.slides, 1):
        lines = [shape.text_frame.text for shape in slide.shapes
                if shape.has_text_frame and shape.text_frame.text.strip()]
        if slide.has_notes_slide:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()
            if notes:
                lines.append(f"[speaker notes] {notes}")
        if lines:
            slides.append(f"[Slide {i}]\n" + "\n".join(lines))
    text = "\n\n".join(slides)
    if not text:
        return f"({p} has no readable text — it may contain only images/diagrams.)"
    return _clip(text)


_STRUCTURED_READERS = {
    ".docx": _read_docx,
    ".xlsx": _read_xlsx,
    ".pptx": _read_pptx,
}


@register(
    "read_file",
    "Read and return the contents of a file — plain text, or a PDF/.docx/"
    ".xlsx/.pptx (real text is extracted, not raw bytes). Scanned/image-only "
    "PDFs and image files CANNOT be read (Wisp has no vision model); say so "
    "plainly rather than trying another tool. Older .doc/.xls/.ppt (no 'x') "
    "are NOT supported — tell the user to re-save as the newer format.",
    {"type": "object",
     "properties": {"path": {"type": "string"}},
     "required": ["path"]},
    category="fs_read",
)
def read_file(path: str) -> str:
    if (msg := _wrong_account_path(path)):
        return msg
    p = Path(path).expanduser()
    # stat() BEFORE exists(). Path.exists() swallows OSError and returns False,
    # so a file macOS is merely blocking is indistinguishable from one that
    # isn't there — and "(no such file: ~/Documents/notes.txt)" sends the model
    # off inventing other paths for a file that exists and is simply
    # unreadable. See _tcc_message.
    try:
        p.stat()
    except PermissionError:
        return _tcc_message(p, "reading")
    except FileNotFoundError:
        # macOS reports a path inside a BLOCKED folder as ENOENT, not EPERM —
        # it hides whether the file exists at all. So a bare "no such file"
        # here can equally mean "you aren't allowed to know". Only trust it
        # when the parent folder is actually readable.
        try:
            os.listdir(p.parent)
        except PermissionError:
            return _tcc_message(p, "reading")
        except OSError:
            pass
        return f"(no such file: {p})"
    except OSError:
        pass
    if not p.exists():
        return f"(no such file: {p})"
    if p.is_dir():
        return f"({p} is a directory, not a file — use list_dir instead)"
    try:
        raw = p.read_bytes()
    except PermissionError:
        return _tcc_message(p, "reading")
    except Exception as e:  # noqa: BLE001
        return f"(error reading {p}: {e})"

    if p.suffix.lower() == ".pdf" or raw.startswith(b"%PDF-"):
        return _read_pdf(p)
    if p.suffix.lower() in _STRUCTURED_READERS:
        try:
            return _STRUCTURED_READERS[p.suffix.lower()](p)
        except Exception as e:  # noqa: BLE001 — malformed OOXML must report, not crash the turn
            return f"(error reading {p}: {e})"
    if p.suffix.lower() in (".doc", ".xls", ".ppt"):
        # The legacy binary Office formats (pre-2007, no trailing 'x') are a
        # completely different container (OLE2 compound file, not ZIP+XML) —
        # none of the three readers above can open one, and without this check
        # it would fall through to the NUL-byte guard below and get the same
        # unhelpful "is a binary file" message as a JPEG. Naming the real
        # reason (old format) tells the user exactly what to do about it.
        return (f"({p} is an older Office format (.{p.suffix.lstrip('.')}) that "
                "Wisp can't read — ask the user to re-save it as the newer "
                f".{p.suffix.lstrip('.')}x format in Word/Excel/PowerPoint.)")

    # Same NUL-byte heuristic git/diff use to flag a binary file — genuine
    # text essentially never contains one. Catches every OTHER format Wisp has
    # no reader for (images, .docx/.pptx/.xlsx, executables, archives) so those
    # report plainly instead of repeating the PDF bug in a new shape: raw bytes
    # decoded with errors="replace" LOOKS like a string, and nothing before
    # this would have stopped it from being handed to the model as if it were
    # the document's actual content.
    if b"\x00" in raw[:8192]:
        kind = p.suffix.lstrip(".") or "binary"
        return (f"({p} is a {kind} file, not text — Wisp can't read its contents. "
                "Image files in particular cannot be read at all: there is no "
                "vision model. Tell the user this plainly; do not retry with "
                "another tool.)")

    return _clip(raw.decode("utf-8", errors="replace"))


# Description length is load-bearing here, not padding. At its original 32
# characters ("List the entries in a directory.") this tool lost the files
# route to run_shell, whose own description runs 554 characters and reads as a
# pitch. Measured 2026-08-09 over 5 reps of "List the files in my Downloads
# folder.": list_dir alone 2/5, run_shell involved 3/5, worst case 4 run_shell
# calls / 5 model steps / 62s. Same failure class STABILITY_PLAN.md already
# records for an over-long run_shell description (4/4 wrong before, 0/4 after)
# — this is its mirror image, a tool too terse to be chosen.
#
# The `~` instruction is the second half of the fix, and it is what actually
# broke the worst run: the model does not know the account name and guessed
# `/Users/AdiJain/Downloads` (wrong case — survived only because macOS is
# case-insensitive) and `/Downloads` (did not exist, which is what sent it to
# run_shell). expanduser() below has always handled `~`; nothing said so.
@register(
    "list_dir",
    "List the files and folders inside a directory on the user's Mac — use "
    "this for 'what's in my Downloads/Desktop/Documents', 'list the files "
    "in X', or to check whether a file exists before reading it. Write the "
    "path with a leading `~` (e.g. `~/Downloads`, `~/Desktop/Projects`): you "
    "do NOT know the user's account name, so an absolute `/Users/<name>/...` "
    "path is a guess and will often be wrong. Pair with read_file to open "
    "something you found here. Pass `recursive=true` to see the WHOLE tree "
    "(subfolders and their contents) in one call instead of listing each "
    "subfolder one at a time — use this whenever the job needs to know what's "
    "inside nested folders, e.g. reorganizing/sorting files that might be "
    "scattered across several subfolders. Folders end with `/` in the output.",
    {"type": "object",
     "properties": {
         "path": {"type": "string", "description": "directory path, e.g. ~/Downloads"},
         "recursive": {"type": "boolean",
                       "description": "list subfolders' contents too, not just this "
                                      "directory's immediate entries (default false)"},
         "max_depth": {"type": "integer",
                       "description": "how many folder levels deep to descend when "
                                      "recursive is true (default 3)"},
     },
     "required": ["path"]},
    category="fs_read",
)
def list_dir(path: str, recursive: bool = False, max_depth: int = 3) -> str:
    if (msg := _wrong_account_path(path)):
        return msg
    p = Path(path).expanduser()
    if not p.is_dir():
        return _missing_dir_message(p)
    if not recursive:
        try:
            entries = sorted(os.listdir(p))
        except PermissionError:
            return _tcc_message(p, "listing")
        return _clip("\n".join(entries) or "(empty)")

    max_depth = max(1, min(int(max_depth or 3), 8))
    lines: list[str] = []
    root_depth = len(p.parts)
    # onerror: os.walk swallows errors silently by default, so a blocked
    # subfolder would just be absent from the listing with nothing said.
    _blocked: list[str] = []
    for dirpath, dirnames, filenames in os.walk(
            p, onerror=lambda e: _blocked.append(str(getattr(e, "filename", "")))):
        depth = len(Path(dirpath).parts) - root_depth
        if depth >= max_depth:
            dirnames.clear()  # os.walk stops descending past a cleared list
            continue
        dirnames.sort()
        rel = Path(dirpath).relative_to(p)
        prefix = "" if rel == Path(".") else f"{rel}/"
        for name in dirnames:
            lines.append(f"{prefix}{name}/")
        for name in sorted(filenames):
            lines.append(f"{prefix}{name}")
    if _blocked and not lines:
        return _tcc_message(p, "listing")
    if _blocked:
        lines.append(f"…[{len(_blocked)} subfolder(s) skipped — macOS denied "
                     "access; see Full Disk Access in System Settings]")
    return _clip("\n".join(lines) or "(empty)")


@register(
    "write_file",
    "Write text to a file, creating or overwriting it. Requires confirmation.",
    {"type": "object",
     "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
     "required": ["path", "content"]},
    category="fs_write",
)
def write_file(path: str, content: str) -> str:
    if (msg := _wrong_account_path(path)):
        return msg
    p = Path(path).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content)} chars to {p}"
    except Exception as e:  # noqa: BLE001
        return f"(error writing {p}: {e})"


@register(
    "move_path",
    "MOVE or RENAME a file or folder — this is how you reorganize, tidy up, or "
    "file things away. Use this for ANY request to move, rename, sort, group, "
    "organize or reorganize files, one call per item. Creates the destination "
    "folder automatically, so you do NOT need a separate mkdir. Do NOT use "
    "run_shell with mv/rm for this: moving is the whole job, and deleting a "
    "folder is never part of it — a folder becomes empty because its contents "
    "were moved out, and an empty folder is harmless to leave behind.",
    {"type": "object",
     "properties": {
         "source": {"type": "string", "description": "the file or folder to move, e.g. ~/Downloads/log.json"},
         "destination": {"type": "string",
                         "description": "where it should end up. A path ending in '/' or an "
                                        "existing folder moves the item INTO that folder keeping "
                                        "its name; otherwise it is the item's new full path."},
     },
     "required": ["source", "destination"]},
    category="fs_write",
)
def move_path(source: str, destination: str) -> str:
    """Reorganizing had no primitive, so the model reached for `run_shell`.

    WHY THIS TOOL EXISTS (2026-08-16). The file/document route offers
    read_file / list_dir / write_file / run_shell — nothing that can move
    anything. Asked to "reorganize the wisp debug logs in my downloads folder",
    the agent said so itself in its first step ("I do not have a specific 'file
    move' or 'reorganize' tool"), fell back to shell, created destinations
    inside the source folder, then ran `rm -rf` on the sources without ever
    running a single `mv`. Three weeks of logs were destroyed.

    A confirmation card now stands in front of that specific shell shape (see
    policy._DESTRUCTIVE_SHELL), but a card is a last line of defence. This is
    the first one: give the job a primitive that CANNOT delete, so the model
    never has a reason to reach past it. Every failure mode of a move is
    recoverable — worst case something lands in the wrong folder.

    Refuses to overwrite an existing destination: silently clobbering a file
    during a "tidy up" is the same class of invisible loss this exists to
    prevent, and the model can always pick another name.
    """
    # Both ends, not just the source: a move whose DESTINATION is an invented
    # /Users/<guess> would silently create that tree and strand the files
    # somewhere the user will never look.
    for candidate in (source, destination):
        if (msg := _wrong_account_path(candidate)):
            return msg
    src = Path(source).expanduser()
    if not src.exists():
        return f"(no such path: {src})"
    dst = Path(destination).expanduser()
    # "into this folder" vs "to this exact path". A trailing separator or an
    # existing directory means the former — the shape `mv` itself uses, and the
    # one a model reaching for "move X into Y/" naturally writes.
    if destination.endswith(("/", os.sep)) or dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        return (f"(refusing to overwrite {dst} — it already exists. "
                f"Move it somewhere else or pick a different name.)")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"moved {src} -> {dst}"
    except Exception as e:  # noqa: BLE001
        return f"(error moving {src} to {dst}: {e})"


@register(
    "delete_path",
    "Delete a file. Requires confirmation.",
    {"type": "object",
     "properties": {"path": {"type": "string"}},
     "required": ["path"]},
    category="fs_delete",
)
def delete_path(path: str) -> str:
    if (msg := _wrong_account_path(path)):
        return msg
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
