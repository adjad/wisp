"""Bounded, local-only MIME display extraction; no Mail/cache integration.

``parse_message(raw, identity={"source_id": "caller-owned-id"})`` accepts bytes
and copies ordinary string metadata without trusting message headers as identity.
Display/anchor text is untrusted plain text: consumers must escape it or use a
text-only UI, never insert it as HTML. Links are actual HTML anchor
hrefs, not inferred from labels or plaintext. Only absolute HTTP(S) destinations
without ambiguous authority/control syntax are retained. This is a syntactic
filter, not a reputation check or authorization to visit a destination.

HTML is preferred in multipart/alternative (last usable HTML, else last usable
plain body). Mixed bodies retain order; attachments and embedded messages are
excluded. Related containers expose only their designated root. No CSS/browser
layout is attempted. Issues record malformed MIME/encoding, filtered links, and
HTML recovery; ``partial`` means some display content survived, ``malformed``
means none survived damage, and ``empty`` means no supported display body was
present (for example an attachment-only message). All resource-limit failures
reject the whole message, with no
truncated text or links. ``complete`` does not certify sender authenticity.
"""

from __future__ import annotations

import base64
import binascii
import codecs
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser
import ipaddress
import quopri
import re
from types import MappingProxyType
from typing import Literal, Mapping
import unicodedata
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ParseLimits:
    """Inclusive budgets; parts include containers and attached messages.

    Input bytes are checked before MIME parsing, part count at allocation time.
    Depth counts the root as one. Decoded budgets cover all eligible text parts,
    including unselected alternatives. Output budgets cover extracted content
    before alternative selection. No caller budget may exceed these hard caps.
    """

    max_message_bytes: int = 2 * 1024 * 1024
    max_parts: int = 100
    max_depth: int = 20
    max_part_bytes: int = 512 * 1024
    max_decoded_bytes: int = 2 * 1024 * 1024
    max_text_chars: int = 512 * 1024
    max_links: int = 1000

    def __post_init__(self):
        for name, field in self.__dataclass_fields__.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= field.default:
                raise ValueError(f"{name} must be an integer in 1..{field.default}")


@dataclass(frozen=True)
class MailLink:
    href: str
    anchor_text: str
    part: str


@dataclass(frozen=True)
class ParseIssue:
    code: str
    part: str


@dataclass(frozen=True)
class ParsedMail:
    identity: Mapping[str, str]
    status: Literal["complete", "partial", "malformed", "empty", "rejected"]
    display_text: str
    links: tuple[MailLink, ...]
    issues: tuple[ParseIssue, ...]


class _LimitExceeded(Exception):
    def __init__(self, code: str, part: str = "1"):
        self.issue = ParseIssue(code, part)


def _clean_text(value: str) -> str:
    # Drop display controls, including bidi overrides, while preserving lines.
    value = "".join(c for c in value if c in "\r\n\t" or
                    unicodedata.category(c) not in {"Cc", "Cf", "Cs"})
    lines = [" ".join(line.split()) for line in value.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _safe_href(value: str) -> str | None:
    value = value.strip(" ")
    if not value or any(c.isspace() or unicodedata.category(c).startswith("C")
                        or c in '\\<>"\'' for c in value):
        return None
    if re.search(r"%(?![0-9a-fA-F]{2})|%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)", value, re.I):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname
        if not host or not host.isascii() or "%" in host:
            return None
        if ":" in host:
            ipaddress.IPv6Address(host)
            if not re.fullmatch(r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?", parsed.netloc):
                return None
        elif not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label, re.I)
                     for label in host.removesuffix(".").split(".")) or len(host) > 253:
            return None
        # urlsplit validates port ranges lazily. Reject an empty explicit port.
        if parsed.netloc.endswith(":") or parsed.port == 0:
            return None
    except ValueError:
        return None
    return value


class _Budget:
    def __init__(self, limits: ParseLimits):
        self.limits = limits
        self.decoded = self.text = self.links = 0

    def add_text(self, value: str, part: str):
        self.text += len(value)
        if self.text > self.limits.max_text_chars:
            raise _LimitExceeded("text_limit", part)


class _DisplayHTML(HTMLParser):
    _hidden = {"head", "script", "style", "template", "iframe", "object",
               "svg", "math", "noscript"}
    _blocks = {"p", "div", "br", "hr", "li", "ul", "ol", "table", "tr",
               "td", "th", "blockquote", "pre", "section", "article",
               "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self, part: str, budget: _Budget, issue):
        super().__init__(convert_charrefs=True)
        self.part, self.budget, self.issue = part, budget, issue
        self.text: list[str] = []
        self.links: list[MailLink] = []
        self.hidden: list[str] = []
        self.anchor: tuple[str | None, list[str]] | None = None

    def _append(self, value: str):
        self.budget.add_text(value, self.part)
        self.text.append(value)
        if self.anchor is not None:
            self.anchor[1].append(value)

    def _finish_anchor(self):
        if self.anchor is not None:
            href, chunks = self.anchor
            if href is not None:
                self.budget.links += 1
                if self.budget.links > self.budget.limits.max_links:
                    raise _LimitExceeded("link_limit", self.part)
                self.links.append(MailLink(href, _clean_text("".join(chunks)), self.part))
            self.anchor = None

    def handle_starttag(self, tag, attrs):
        if tag in self._hidden:
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag in self._blocks:
            self._append("\n")
        if tag == "a":
            if self.anchor is not None:
                self.issue("malformed_html", self.part)
                self._finish_anchor()
            hrefs = [value for key, value in attrs if key == "href"]
            href = _safe_href(hrefs[0]) if len(hrefs) == 1 and hrefs[0] is not None else None
            if hrefs and href is None:
                self.issue("filtered_href", self.part)
            self.anchor = (href, [])
        if tag == "img":
            # Alt text is local text; src/srcset are never read or loaded.
            self._append(next((value or "" for key, value in attrs if key == "alt"), ""))

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag == "a":
            self._finish_anchor()
        if tag in self._blocks:
            self._append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.hidden:
            self._append(data)

    def finish(self):
        self.close()
        if self.anchor is not None or self.hidden or self.rawdata:
            self.issue("malformed_html", self.part)
        self._finish_anchor()
        return _clean_text("".join(self.text)), tuple(self.links)


def parse_message(raw: bytes, *, identity: Mapping[str, str],
                  limits: ParseLimits = ParseLimits()) -> ParsedMail:
    """Parse one message without I/O. Wrong API argument types raise errors.

    MIME issues are stable code/part pairs (1, 1.1, etc.), deduplicated in
    encounter order. No message content is echoed in diagnostics. Ordinary
    attachments/resources are intentionally skipped, not treated as damage.
    Identity is an immutable defensive copy of caller metadata; it is never
    filled from From, Message-ID, or other untrusted message headers.
    """
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    if not isinstance(limits, ParseLimits):
        raise TypeError("limits must be ParseLimits")
    if not isinstance(identity, Mapping) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in identity.items()):
        raise TypeError("identity must map strings to strings")
    metadata = MappingProxyType(dict(sorted(identity.items())))
    issues: list[ParseIssue] = []
    seen: set[ParseIssue] = set()
    budget = _Budget(limits)
    ambiguous_parts: set[str] = set()

    def issue(code, part):
        item = ParseIssue(code, part)
        if item not in seen:
            seen.add(item)
            issues.append(item)

    def result(status, text="", links=()):
        return ParsedMail(metadata, status, text, links, tuple(issues))

    parts = 0

    def factory(*, policy):
        nonlocal parts
        parts += 1
        if parts > limits.max_parts:
            raise _LimitExceeded("part_limit")
        return EmailMessage(policy=policy)

    def decode(part, path):
        encoding = str(part.get("Content-Transfer-Encoding", "7bit")).strip().lower()
        if encoding not in {"base64", "quoted-printable", "7bit", "8bit", "binary"}:
            issue("unsupported_transfer_encoding", path)
            return None
        if encoding in {"base64", "quoted-printable"}:
            payload = part.get_payload(decode=False)
            try:
                data = payload.encode("ascii")
            except UnicodeEncodeError:
                issue("invalid_transfer_bytes", path)
                return None
        else:
            # decode=True preserves original 8bit bytes rather than replacing
            # bad charset sequences as get_payload(decode=False) can do.
            data = part.get_payload(decode=True)
        if encoding == "base64":
            try:
                data = base64.b64decode(re.sub(rb"[\t\r\n ]", b"", data), validate=True)
            except (binascii.Error, ValueError):
                issue("invalid_base64", path)
                return None
        elif encoding == "quoted-printable":
            if re.search(rb"=(?![0-9A-Fa-f]{2}|\r?\n)", data):
                issue("invalid_quoted_printable", path)
            data = quopri.decodestring(data)
        elif encoding == "7bit" and any(b > 127 for b in data):
            issue("invalid_7bit", path)
        if len(data) > limits.max_part_bytes:
            raise _LimitExceeded("part_byte_limit", path)
        budget.decoded += len(data)
        if budget.decoded > limits.max_decoded_bytes:
            raise _LimitExceeded("decoded_byte_limit", path)
        charset = part.get_content_charset() or "ascii"
        try:
            codec = codecs.lookup(charset)
            # Non-text codecs (e.g. base64_codec) must never transform the body.
            if not getattr(codec, "_is_text_encoding", False):
                raise LookupError
            return data.decode(charset, errors="strict")
        except (LookupError, UnicodeError, ValueError):
            issue("invalid_charset_or_text", path)
            return data.decode("utf-8", errors="replace")

    def extract(part, path):
        if path in ambiguous_parts:
            return "", (), ""
        if part.get_content_disposition() == "attachment" or part.get_filename() is not None:
            return "", (), ""
        kind = part.get_content_type()
        if kind.startswith("message/"):
            return "", (), ""
        if part.is_multipart():
            children = list(part.iter_parts())
            if kind == "multipart/related":
                start = part.get_param("start")
                selected = [i for i, child in enumerate(children)
                            if str(child.get("Content-ID", "")) == start] if start else [0]
                if len(selected) != 1 or not children:
                    issue("invalid_related_root", path)
                    return "", (), ""
                index = selected[0]
                return extract(children[index], f"{path}.{index + 1}")
            if kind not in {"multipart/mixed", "multipart/alternative"}:
                issue("unsupported_multipart", path)
                return "", (), ""
            bodies = [extract(child, f"{path}.{index + 1}")
                      for index, child in enumerate(children)]
            if kind == "multipart/alternative":
                for preferred in ("text/html", "text/plain"):
                    for body in reversed(bodies):
                        if body[2] == preferred and (body[0] or body[1]):
                            return body
                return "", (), ""
            text = "\n\n".join(body[0] for body in bodies if body[0])
            return text, tuple(link for body in bodies for link in body[1]), kind
        if kind not in {"text/plain", "text/html"}:
            return "", (), ""
        text = decode(part, path)
        if text is None:
            return "", (), ""
        if kind == "text/html":
            parser = _DisplayHTML(path, budget, issue)
            try:
                parser.feed(text)
                text, links = parser.finish()
            except (AssertionError, ValueError):
                issue("malformed_html", path)
                return "", (), ""
        else:
            budget.add_text(text, path)
            text, links = _clean_text(text), ()
        return text, links, kind

    try:
        if len(raw) > limits.max_message_bytes:
            raise _LimitExceeded("message_byte_limit")
        if not raw.strip():
            issue("empty_message", "1")
            return result("malformed")
        # A policy factory avoids BytesParser's compatibility-probe allocation,
        # so the part budget counts exactly the message objects in the tree.
        message = BytesParser(policy=policy.default.clone(message_factory=factory)).parsebytes(raw)
        # Include defects/depth of skipped attachments in structural validation.
        pending = [(message, "1", 1)]
        while pending:
            part, path, depth = pending.pop()
            if depth > limits.max_depth:
                raise _LimitExceeded("depth_limit", path)
            for defect in part.defects:
                issue("mime_" + type(defect).__name__, path)
            headers_seen = set()
            for name, raw_value in part.raw_items():
                name = name.lower()
                if name not in {"content-type", "content-transfer-encoding", "content-disposition"}:
                    continue
                if name in headers_seen:
                    issue("duplicate_mime_header", path)
                    ambiguous_parts.add(path)
                headers_seen.add(name)
                value = part.policy.header_fetch_parse(name, raw_value)
                for defect in getattr(value, "defects", ()):
                    issue("header_" + type(defect).__name__, path)
            if part.is_multipart():
                pending.extend(reversed([(child, f"{path}.{i + 1}", depth + 1)
                                         for i, child in enumerate(part.iter_parts())]))
        text, links, _ = extract(message, "1")
        if len(text) > limits.max_text_chars:
            raise _LimitExceeded("text_limit")
        if not text and not links:
            status = "malformed" if issues else "empty"
            issue("no_display_body", "1")
            return result(status)
        return result("partial" if issues else "complete", text, links)
    except _LimitExceeded as exc:
        issue(exc.issue.code, exc.issue.part)
        return result("rejected")
    except (ValueError, UnicodeError, RecursionError, IndexError):
        # Malformed email header parameters and HTML declarations can raise
        # instead of recording stdlib defects. Never return unchecked content.
        issue("parser_error", "1")
        return result("malformed")
