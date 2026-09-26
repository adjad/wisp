"""Pure, bounded extraction for caller-acquired linked-document evidence.

This deliberately does not replace tools.builtin._read_docx: that reader accepts
a filesystem path for the read_file tool. Here the caller supplies bytes/text,
and receives normalized text with evidence sections. No paths, URLs, acquisition,
relationships, macros, embedded objects, or external entities are opened.

Offsets are half-open Python character indexes into Extraction.text, NOT source
byte, rendered-page, or XML offsets. Locators identify logical source blocks.
HTML is a static text projection, not a browser visibility/accessibility model.
DOCX supports the main WordprocessingML body (including table paragraphs), not
layout, revision review, headers, notes, or OCR. Omitted document content and
limits are explicit reasons; a partial result is never complete evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from html.parser import HTMLParser
from io import BytesIO
import re
import struct
from typing import Literal
from xml.parsers import expat
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile
import zlib


Status = Literal["complete", "empty", "partial", "encrypted", "image_only",
                 "corrupt", "unsupported", "limit_exceeded"]
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_WORD_NAMESPACES = {"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                    "http://purl.oclc.org/ooxml/wordprocessingml/main"}
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@dataclass(frozen=True)
class ExtractionLimits:
    """Positive budgets; callers may lower, but cannot exceed these hard caps."""

    input_bytes: int = 4 * 1024 * 1024
    zip_entries: int = 256
    zip_total_bytes: int = 32 * 1024 * 1024
    zip_entry_bytes: int = 8 * 1024 * 1024
    zip_ratio: int = 100
    parser_events: int = 100_000
    depth: int = 128
    output_chars: int = 500_000
    sections: int = 10_000

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 0 < value <= field.default:
                raise ValueError(f"{field.name} must be an integer in 1..{field.default}")


@dataclass(frozen=True)
class EvidenceSection:
    start: int
    end: int
    kind: str
    locator: str


@dataclass(frozen=True)
class Extraction:
    status: Status
    text: str = ""
    sections: tuple[EvidenceSection, ...] = ()
    reasons: tuple[str, ...] = ()


class _Stop(Exception):
    def __init__(self, status: Status, reason: str):
        self.status, self.reason = status, reason


class _Output:
    def __init__(self, limits: ExtractionLimits):
        self.limits = limits
        self.parts: list[str] = []
        self.sections: list[EvidenceSection] = []
        self.length = 0
        self.reasons: list[str] = []
        self.image = False

    def note(self, reason: str):
        if reason not in self.reasons:
            self.reasons.append(reason)

    def add(self, text: str, kind: str, locator: str):
        text = text.strip()
        if not text:
            return
        if len(self.sections) >= self.limits.sections:
            raise _Stop("limit_exceeded", "section_limit")
        separator = 1 if self.parts else 0
        remaining = self.limits.output_chars - self.length - separator
        if remaining <= 0:
            raise _Stop("limit_exceeded", "output_limit")
        clipped = len(text) > remaining
        text = text[:remaining]
        start = self.length + separator
        self.parts.append(text)
        self.sections.append(EvidenceSection(start, start + len(text), kind, locator))
        self.length = start + len(text)
        if clipped:
            raise _Stop("limit_exceeded", "output_limit")

    def result(self, stop: _Stop | None = None) -> Extraction:
        if stop:
            self.note(stop.reason)
        if self.parts:
            status = "partial" if self.reasons else "complete"
        elif stop:
            status = stop.status
        elif self.image:
            status = "image_only"
        elif self.reasons:
            status = "partial"
        else:
            status = "empty"
        return Extraction(status, "\n".join(self.parts), tuple(self.sections),
                          tuple(self.reasons))


class _Budget:
    def __init__(self, limits: ExtractionLimits):
        self.limits, self.events = limits, 0

    def event(self):
        self.events += 1
        if self.events > self.limits.parser_events:
            raise _Stop("limit_exceeded", "parser_event_limit")

    def depth(self, value: int):
        if value > self.limits.depth:
            raise _Stop("limit_exceeded", "parser_depth_limit")


def _decode(content: bytes | str) -> str:
    if isinstance(content, str):
        text = content
    else:
        encoding = "utf-16" if content.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        try:
            text = content.decode(encoding)
        except UnicodeError:
            raise _Stop("corrupt", "invalid_text_encoding") from None
    if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
        raise _Stop("unsupported", "binary_text")
    return text.replace("\r\n", "\n").replace("\r", "\n")


class _HTML(HTMLParser):
    _VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
    _SKIP = frozenset("head script style template noscript iframe noembed noframes object svg canvas".split())
    _BLOCK = frozenset("address article aside blockquote div dl dt dd fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 header li main nav ol p pre section table tbody td th thead tr ul".split())

    def __init__(self, out: _Output):
        # Scripting selects noscript tokenization only; nothing is executed.
        # The pinned Python 3.13.14 parser supplies raw/RCDATA/plaintext modes.
        super().__init__(convert_charrefs=True, scripting=True)
        self.out, self.budget = out, _Budget(out.limits)
        self.stack: list[tuple[str, bool]] = []
        self.buffer: list[str] = []
        self.buffer_length = 0
        self.block = 0
        self.kind = "paragraph"

    def flush(self):
        if self.buffer:
            self.block += 1
            text = re.sub(r"\s+", " ", "".join(self.buffer)).strip()
            self.buffer.clear()
            self.buffer_length = 0
            self.out.add(text, self.kind, f"html:block[{self.block}]")

    def handle_starttag(self, tag, attrs):
        self.budget.event()
        inherited = bool(self.stack and self.stack[-1][1])
        hidden = any(key == "hidden" for key, _ in attrs)
        skip = inherited or tag in self._SKIP or hidden
        if not inherited and tag in {"img", "svg", "canvas"} and not hidden:
            self.out.image = True
            self.out.note("image_content_omitted")
        if not inherited and tag in {"iframe", "object", "embed"}:
            self.out.note("embedded_content_omitted")
        if not inherited and (tag in self._BLOCK or skip):
            self.flush()
        if not skip and tag in self._BLOCK:
            self.kind = "heading" if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} else "paragraph"
        if not skip and tag in {"br", "hr"}:
            self.handle_data(" ")
        if tag not in self._VOID:
            self.budget.depth(len(self.stack) + 1)
            self.stack.append((tag, skip))
        if (tag in self.CDATA_CONTENT_ELEMENTS or
                tag in self.RCDATA_CONTENT_ELEMENTS or tag in {"noscript", "plaintext"}):
            # HTMLParser normally enters this mode after handle_starttag, but
            # skips that step for its XHTML-style startend callback. Enter it
            # here for both paths; native RCDATA performs exactly one unescape.
            self.set_cdata_mode(tag, escapable=tag in self.RCDATA_CONTENT_ELEMENTS)

    def handle_startendtag(self, tag, attrs):
        # HTML ignores the slash on non-void elements. Closing them here could
        # expose hidden/script text or allow literal markup to close an ancestor.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        self.budget.event()
        if tag in self._VOID:
            return
        found = next((i for i in range(len(self.stack) - 1, -1, -1)
                      if self.stack[i][0] == tag), None)
        if found is None:
            self.out.note("malformed_html")
            return
        if found != len(self.stack) - 1:
            self.out.note("malformed_html")
        if not self.stack[-1][1] and tag in self._BLOCK:
            self.flush()
        del self.stack[found:]
        block = next((name for name, _ in reversed(self.stack) if name in self._BLOCK), "")
        self.kind = "heading" if block in {"h1", "h2", "h3", "h4", "h5", "h6"} else "paragraph"

    def handle_data(self, data):
        self.budget.event()
        if self.stack and self.stack[-1][1]:
            return
        # Normalize each data run only after joining, preserving inline spacing.
        remaining = self.out.limits.input_bytes - self.buffer_length
        if len(data) > remaining:
            raise _Stop("limit_exceeded", "parser_buffer_limit")
        self.buffer.append(data)
        self.buffer_length += len(data)

    def handle_comment(self, data):
        self.budget.event()

    def handle_decl(self, decl):
        self.budget.event()
        if decl.lower() != "doctype html":
            raise _Stop("unsupported", "html_declaration")

    def unknown_decl(self, data):
        raise _Stop("unsupported", "html_declaration")

    def handle_pi(self, data):
        raise _Stop("unsupported", "html_processing_instruction")


def _html(text: str, out: _Output):
    # Marked sections (including legacy conditional declarations) are outside
    # this projection. Reject conservatively even inside comments/scripts;
    # HTMLParser's treatment of bogus marked sections varies across Python.
    if "<![" in text:
        raise _Stop("unsupported", "html_declaration")
    parser = _HTML(out)
    # One bounded feed avoids repeated scanning of an unfinished long token.
    try:
        parser.feed(text)
        if parser.rawdata:
            out.note("unfinished_html_token")
        parser.close()
    except AssertionError:
        # HTMLParser raises AssertionError for malformed marked declarations.
        raise _Stop("corrupt", "invalid_html_declaration") from None
    if parser.stack and parser.stack[-1][0] == "plaintext":
        # EOF is plaintext's terminator; even </plaintext> is literal text.
        parser.stack.pop()
    if parser.stack:
        out.note("unclosed_html")
    parser.flush()


def _zip_preflight(content: bytes, limits: ExtractionLimits):
    """Bound central-directory work BEFORE ZipFile allocates its entry list.

    Deliberately reject ZIP64, split archives, and prefixed/self-extracting ZIPs.
    A count from EOCD alone is insufficient: ZipFile trusts directory size.
    """
    end = content.rfind(b"PK\x05\x06", max(0, len(content) - 65557))
    if end < 0 or end + 22 > len(content):
        raise _Stop("corrupt", "invalid_zip_directory")
    disk, cd_disk, count_disk, count, size, offset, comment = struct.unpack_from("<4H2IH", content, end + 4)
    if end + 22 + comment != len(content) or offset + size != end:
        raise _Stop("corrupt", "invalid_zip_directory")
    if disk or cd_disk or count_disk != count or count == 65535:
        raise _Stop("unsupported", "unsupported_zip_layout")
    if count > limits.zip_entries:
        raise _Stop("limit_exceeded", "zip_entry_limit")
    position, actual = offset, 0
    while position < end:
        if position + 46 > end or content[position:position + 4] != b"PK\x01\x02":
            raise _Stop("corrupt", "invalid_zip_directory")
        name, extra, entry_comment = struct.unpack_from("<3H", content, position + 28)
        position += 46 + name + extra + entry_comment
        actual += 1
        if actual > limits.zip_entries:
            raise _Stop("limit_exceeded", "zip_entry_limit")
    if position != end or actual != count:
        raise _Stop("corrupt", "invalid_zip_directory")


def _word_name(name: str) -> str:
    namespace, _, local = name.rpartition("}")
    return local if namespace in _WORD_NAMESPACES else ""


def _word_xml(data: bytes, out: _Output):
    budget = _Budget(out.limits)
    parser = expat.ParserCreate(namespace_separator="}")
    stack: list[str] = []
    paragraph: list[str] | None = None
    paragraph_index, kind, body_count = 0, "paragraph", 0
    omitted_depth: int | None = None

    def start(name, attrs):
        nonlocal paragraph, paragraph_index, kind, body_count, omitted_depth
        budget.event()
        budget.depth(len(stack) + 1)
        local = _word_name(name)
        if omitted_depth is not None:
            # Count all work/depth, but no descendant may change paragraph
            # state, contribute text, or trigger a different interpretation.
            stack.append(local)
            return
        if name == "http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent":
            # Choice and Fallback are mutually exclusive rendering branches.
            # Reading both would manufacture duplicate/conflicting evidence.
            raise _Stop("unsupported", "alternate_word_content")
        if not stack and local != "document":
            raise _Stop("corrupt", "invalid_word_document")
        if local == "body":
            body_count += 1
            if stack != ["document"] or body_count != 1:
                raise _Stop("corrupt", "invalid_word_body")
        stack.append(local)
        if "body" not in stack:
            return
        if local == "p":
            if paragraph is not None:
                raise _Stop("unsupported", "nested_word_paragraph")
            paragraph, kind = [], "paragraph"
            paragraph_index += 1
        elif local == "pStyle" and paragraph is not None:
            style = next((v for k, v in attrs.items() if _word_name(k) == "val"), "")
            if re.fullmatch(r"Heading[1-9]", style, re.I):
                kind = "heading"
        elif local in {"drawing", "pict"}:
            out.image = True
            out.note("image_content_omitted")
            omitted_depth = len(stack)
        elif local in {"altChunk", "object", "subDoc"}:
            out.note("embedded_content_omitted")
            omitted_depth = len(stack)
        elif local in {"del", "moveFrom"}:
            out.note("revision_content_omitted")
            omitted_depth = len(stack)
        elif local in {"tab", "br", "cr", "noBreakHyphen", "softHyphen"} and paragraph is not None:
            paragraph.append({"tab": "\t", "br": "\n", "cr": "\n",
                              "noBreakHyphen": "\u2011", "softHyphen": "\u00ad"}[local])

    def end(name):
        nonlocal paragraph, omitted_depth
        budget.event()
        if omitted_depth is not None:
            if len(stack) == omitted_depth:
                omitted_depth = None
            stack.pop()
            return
        if _word_name(name) == "p" and paragraph is not None:
            out.add("".join(paragraph), kind, f"word/document.xml:p[{paragraph_index}]")
            paragraph = None
        stack.pop()

    def characters(text):
        budget.event()
        if omitted_depth is None and paragraph is not None and stack[-1] == "t":
            paragraph.append(text)

    def reject(*args):
        raise _Stop("unsupported", "xml_declaration_forbidden")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    try:
        parser.Parse(data, True)
    except (expat.ExpatError, LookupError, ValueError):
        raise _Stop("corrupt", "invalid_word_xml") from None
    if body_count != 1:
        raise _Stop("corrupt", "missing_word_body")


def _docx(content: bytes, out: _Output):
    if content.startswith(_OLE):
        encrypted = all(name.encode("utf-16-le") in content
                        for name in ("EncryptedPackage", "EncryptionInfo"))
        raise _Stop("encrypted" if encrypted else "unsupported",
                    "encrypted_office_container" if encrypted else "legacy_office_container")
    _zip_preflight(content, out.limits)
    try:
        with ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            names: set[str] = set()
            total = 0
            for info in infos:
                if info.filename in names:
                    raise _Stop("corrupt", "duplicate_zip_part")
                names.add(info.filename)
                if info.flag_bits & 1:
                    raise _Stop("encrypted", "encrypted_zip_entry")
                if info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
                    raise _Stop("unsupported", "zip_compression")
                total += info.file_size
                if info.file_size > out.limits.zip_entry_bytes:
                    raise _Stop("limit_exceeded", "zip_part_size_limit")
                if total > out.limits.zip_total_bytes:
                    raise _Stop("limit_exceeded", "zip_expansion_limit")
                if info.file_size > max(1, info.compress_size) * out.limits.zip_ratio:
                    raise _Stop("limit_exceeded", "zip_ratio_limit")
            if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                raise _Stop("corrupt", "missing_docx_part")
            if any(re.fullmatch(r"word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml", name)
                   for name in names):
                out.note("ancillary_word_parts_omitted")
            with archive.open("word/document.xml") as source:
                data = source.read(out.limits.zip_entry_bytes + 1)
            if len(data) > out.limits.zip_entry_bytes:
                raise _Stop("limit_exceeded", "zip_part_size_limit")
            _word_xml(data, out)
    except (BadZipFile, EOFError, zlib.error, UnicodeError, ValueError):
        raise _Stop("corrupt", "invalid_docx_archive") from None
    except NotImplementedError:
        raise _Stop("unsupported", "zip_feature") from None


def extract_linked_document(content: bytes | str, media_type: str, *,
                            limits: ExtractionLimits | None = None) -> Extraction:
    """Extract DOCX, HTML or text/plain; the caller explicitly labels the format.

    Text bytes must be UTF-8 (optional BOM) or BOM-marked UTF-16. There is no
    encoding guessing or binary-to-text fallback. MIME parameters are ignored;
    callers must decode other encodings themselves. Programmer errors (invalid
    argument types/limits) raise; hostile document failures return fixed reasons.
    Encrypted status is a signature-based handoff, not cryptographic validation.
    """
    if not isinstance(content, (bytes, str)) or not isinstance(media_type, str):
        raise TypeError("content must be bytes or str and media_type must be str")
    if limits is not None and not isinstance(limits, ExtractionLimits):
        raise TypeError("limits must be ExtractionLimits")
    out = _Output(limits or ExtractionLimits())
    try:
        if len(content) > out.limits.input_bytes:
            raise _Stop("limit_exceeded", "input_size_limit")
        if isinstance(content, str):
            try:
                size = len(content.encode("utf-8"))
            except UnicodeError:
                raise _Stop("corrupt", "invalid_text_encoding") from None
            if size > out.limits.input_bytes:
                raise _Stop("limit_exceeded", "input_size_limit")
        # Bound metadata too; it is supplied by the caller, not trusted input.
        if len(media_type) > 256:
            raise _Stop("unsupported", "media_type")
        mime = media_type.split(";", 1)[0].strip().lower()
        if mime == DOCX:
            if not isinstance(content, bytes):
                raise _Stop("unsupported", "docx_requires_bytes")
            _docx(content, out)
        elif mime == "text/html":
            _html(_decode(content), out)
        elif mime == "text/plain":
            budget = _Budget(out.limits)
            for index, line in enumerate(_decode(content).split("\n"), 1):
                budget.event()
                out.add(line, "line", f"text:line[{index}]")
        else:
            raise _Stop("unsupported", "media_type")
    except _Stop as stop:
        return out.result(stop)
    return out.result()
