"""Text out of an attached document's bytes, bounded. Runs in a CHILD process.

service/documents.py starts `python -I` on this module's `main()` for every
attached document, feeds the bytes on stdin and reads one JSON line back. The
parsers here (pypdf, python-docx, openpyxl, python-pptx, textutil) can take
seconds and hundreds of MB on a hostile file; in a child they can't stall the
service's event loop, and the parent kills the child at its deadline (a thread
can't be killed). Pure: no service imports, no network, nothing written to disk.

Rules:
- IMPORTANT: a ZIP-based document (docx/xlsx/pptx/odt) is checked BEFORE any
  parser opens it: the central directory is walked by hand
  (linked_documents._zip_preflight), then every entry's declared size, the
  total, the compression ratio, encryption and method are bounded. A 179 KB
  docx holding 60 MB of XML took 46 s and 1.45 GB in python-docx before this
  (Release Auditor, 2026-10-28).
- Extraction stops once MAX_TEXT_CHARS is reached (pages, paragraphs, slides
  and rows alike) and walks at most MAX_PDF_PAGES PDF pages.
- Every failure is a ValueError with a plain sentence for the person.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import PurePath
from types import SimpleNamespace
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 24_000
MAX_PDF_PAGES = 60
# ZIP bounds. Entry count and total are looser than linked_documents' because a
# real deck or report carries many parts and images; the ratio and per-XML-part
# limits are what stop a decompression bomb, and they are as strict.
ZIP_ENTRIES = 2_000
ZIP_TOTAL_BYTES = 64 * 1024 * 1024
ZIP_XML_PART_BYTES = 8 * 1024 * 1024
ZIP_PART_BYTES = 24 * 1024 * 1024
ZIP_RATIO = 100
_TEXTUTIL_TIMEOUT_S = 20.0
# Child-process ceilings (best effort: macOS enforces CPU time, not address space).
CHILD_CPU_SECONDS = 40
CHILD_MEMORY_BYTES = 2 * 1024 * 1024 * 1024

_TEXTUTIL = {"doc": "doc", "rtf": "rtf", "odt": "odt", "wordml": "wordml"}
_HTML = {"html", "htm"}
_PLAIN = {"txt", "text", "md", "markdown", "csv", "tsv", "json", "log", "xml", "yaml", "yml"}
_ZIPPED = {"docx", "xlsx", "pptx", "odt"}
_LEGACY = {"xls": ".xlsx", "ppt": ".pptx"}

TOO_COMPLEX = "That document is too large or complex for Wisp to read."
DAMAGED = "That document couldn't be read. It may be damaged."


class _Enough(Exception):
    """Raised inside a walk once the text cap is reached."""


class _Text:
    """Collects text up to the cap, then stops the walk."""

    def __init__(self, limit: int = MAX_TEXT_CHARS):
        self.parts: list[str] = []
        self.size = 0
        self.limit = limit

    def add(self, text: str) -> None:
        if not text:
            return
        self.parts.append(text)
        self.size += len(text) + 1
        if self.size > self.limit:
            raise _Enough

    def joined(self, sep: str = "\n") -> str:
        return sep.join(self.parts)


def _kind(name: str) -> str:
    return PurePath(name or "").suffix.lower().lstrip(".")


# --- ZIP preflight ----------------------------------------------------------------

def zip_guard(data: bytes) -> None:
    """Refuse a ZIP whose expansion isn't bounded, before any parser sees it."""
    from service import linked_documents
    try:
        linked_documents._zip_preflight(data, SimpleNamespace(zip_entries=ZIP_ENTRIES))
    except linked_documents._Stop as stop:
        raise ValueError(TOO_COMPLEX if stop.status == "limit_exceeded" else DAMAGED) from None
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            names, total = set(), 0
            for info in archive.infolist():
                if info.filename in names:
                    raise ValueError(DAMAGED)
                names.add(info.filename)
                if info.flag_bits & 1:
                    raise ValueError("That document is password-protected.")
                if info.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
                    raise ValueError(DAMAGED)
                xml = info.filename.lower().endswith((".xml", ".rels"))
                if info.file_size > (ZIP_XML_PART_BYTES if xml else ZIP_PART_BYTES):
                    raise ValueError(TOO_COMPLEX)
                total += info.file_size
                if total > ZIP_TOTAL_BYTES:
                    raise ValueError(TOO_COMPLEX)
                if info.file_size > max(1, info.compress_size) * ZIP_RATIO:
                    raise ValueError(TOO_COMPLEX)
    except BadZipFile:
        raise ValueError(DAMAGED) from None


# --- the formats ------------------------------------------------------------------

def _pdf(data: bytes) -> tuple[str, bool]:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                pass
        if reader.is_encrypted:
            raise ValueError("That PDF is password-protected. Wisp can't read it without the password.")
        out = _Text()
        total = len(reader.pages)
        try:
            for index, page in enumerate(reader.pages):
                if index >= MAX_PDF_PAGES:
                    return out.joined("\n\n"), True
                out.add((page.extract_text() or "").strip())
        except _Enough:
            return out.joined("\n\n"), True
        return out.joined("\n\n"), total > MAX_PDF_PAGES
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 — a damaged PDF is unreadable, never a crash
        raise ValueError("That PDF couldn't be read. It may be damaged.") from None


def _docx(data: bytes) -> tuple[str, bool]:
    from docx import Document
    doc = Document(io.BytesIO(data))
    out = _Text()
    try:
        for paragraph in doc.paragraphs:
            if paragraph.text.strip():
                out.add(paragraph.text)
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    out.add(" | ".join(cells))
    except _Enough:
        return out.joined(), True
    return out.joined(), False


def _xlsx(data: bytes) -> tuple[str, bool]:
    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = _Text()
    try:
        for sheet in book.worksheets:
            out.add(f"[Sheet: {sheet.title}]")
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                if any(c.strip() for c in cells):
                    out.add(" | ".join(cells).rstrip(" |"))
    except _Enough:
        return out.joined(), True
    finally:
        book.close()
    return out.joined(), False


def _pptx(data: bytes) -> tuple[str, bool]:
    from pptx import Presentation
    deck = Presentation(io.BytesIO(data))
    out = _Text()
    try:
        for i, slide in enumerate(deck.slides, 1):
            lines = [shape.text_frame.text for shape in slide.shapes
                     if shape.has_text_frame and shape.text_frame.text.strip()]
            if slide.has_notes_slide:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
                if notes:
                    lines.append(f"[speaker notes] {notes}")
            if lines:
                out.add(f"[Slide {i}]\n" + "\n".join(lines))
    except _Enough:
        return out.joined("\n\n"), True
    return out.joined("\n\n"), False


def _textutil(data: bytes, fmt: str) -> str:
    """macOS's own converter, fed on stdin so nothing is written to disk."""
    try:
        done = subprocess.run(["/usr/bin/textutil", "-stdin", "-stdout", "-format", fmt, "-convert", "txt",
                               "-encoding", "UTF-8"], input=data, capture_output=True,
                              timeout=_TEXTUTIL_TIMEOUT_S, env={"PATH": "/usr/bin:/bin", "LANG": "C"})
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("That document couldn't be read.") from None
    if done.returncode != 0:
        raise ValueError(DAMAGED)
    return done.stdout.decode("utf-8", "replace")


class _HTMLText(HTMLParser):
    """Visible text of an HTML page: scripts and styles dropped, blocks on their own lines."""
    _SKIP = {"script", "style", "noscript", "template"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "td"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data):
        if not self._skipping:
            self.parts.append(data)


def _html(data: bytes) -> str:
    parser = _HTMLText()
    parser.feed(_plain_text(data))
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def _plain_text(data: bytes) -> str:
    if b"\x00" in data[:8192] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("That file isn't a document Wisp can read.")
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def extract_text(name: str, data: bytes) -> tuple[str, bool]:
    """(text, cut short) for a document's bytes, or ValueError with a plain sentence."""
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("That document is too large for Wisp to read (20 MB at most).")
    kind = _kind(name)
    if kind in _LEGACY:
        raise ValueError(f"Wisp can't read old .{kind} files. Save it as {_LEGACY[kind]} and try again.")
    if kind in _ZIPPED:
        zip_guard(data)
    cut = False
    try:
        if kind == "pdf":
            text, cut = _pdf(data)
            if not text.strip():
                raise ValueError("That PDF has no text Wisp can read (it looks scanned).")
        elif kind == "docx":
            text, cut = _docx(data)
        elif kind == "xlsx":
            text, cut = _xlsx(data)
        elif kind == "pptx":
            text, cut = _pptx(data)
        elif kind in _HTML:
            text = _html(data)
        elif kind in _TEXTUTIL:
            text = _textutil(data, _TEXTUTIL[kind])
        elif kind in _PLAIN or not kind:
            text = _plain_text(data)
        else:
            raise ValueError(f"Wisp can't read .{kind} files yet.")
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 — a damaged file is unreadable, never a crash
        raise ValueError(DAMAGED) from None
    if not text.strip():
        raise ValueError("That document has no text Wisp can read.")
    if len(text) > MAX_TEXT_CHARS:
        text, cut = text[:MAX_TEXT_CHARS], True
    return text, cut


def _limit_self() -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (CHILD_CPU_SECONDS, CHILD_CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY_BYTES, CHILD_MEMORY_BYTES))
    except (ImportError, ValueError, OSError):
        pass


def main() -> None:
    """Child entry: argv[1] is the file name, stdin the bytes; one JSON line out."""
    _limit_self()
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    data = sys.stdin.buffer.read(MAX_DOCUMENT_BYTES + 1)
    try:
        text, cut = extract_text(name, data)
        result = {"text": text, "cut": cut}
    except ValueError as error:
        result = {"error": str(error) or DAMAGED}
    except MemoryError:
        result = {"error": TOO_COMPLEX}
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
