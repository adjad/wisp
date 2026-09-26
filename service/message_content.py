"""Internal A12 content normalization; no source reader or public A01 protocol.

Input is a caller-owned dict with guid, conversation, sender, direction,
timestamp, text and links. IDs are opaque, case-sensitive strings. Timestamps
are Unix seconds or RFC 3339 strings with a known offset (microsecond precision).
Links are an explicit list of {url, title?} dicts, never extracted from text.
Unknown keys are ignored. No I/O, clock, native bridge or global mutable state
is used; callers remain responsible for source provenance and rendering.

Coverage describes ONLY this supplied record, never Messages sync completeness.
Missing/null text or links means unknown coverage; explicit empty values mean
examined and empty. Malformed or bounded-away content makes the result partial.
No usable text/link makes it invalid, even when metadata is valid. A source GUID
gives a stable identity; without one, the normalized-content fingerprint is only
a best-effort identity and cannot distinguish identical repeated messages.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import math
import re
import unicodedata
from urllib.parse import quote, urlsplit

MAX_TEXT_CHARS = 100_000
MAX_LINKS = 256
MAX_URL_CHARS = 8192
MAX_ID_CHARS = 1024
MAX_TITLE_CHARS = 512
_FIELDS = ("guid", "conversation", "sender", "direction", "timestamp", "text", "links")
_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z", re.ASCII,
)
_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})|%(?:0[0-9a-fA-F]|1[0-9a-fA-F]|7[fF])")
_PERCENT = re.compile(r"%[0-9a-fA-F]{2}")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)


@dataclass(frozen=True)
class ContentIssue:
    field: str
    code: str
    index: int | None = None


@dataclass(frozen=True)
class ContentCoverage:
    supplied_fields: tuple[str, ...]
    valid_metadata: tuple[str, ...]
    text: str  # missing, invalid, empty, complete, partial
    links: str  # missing, invalid, complete, partial
    links_supplied: int | None
    links_examined: int
    links_accepted: int  # occurrences, including duplicates
    links_rejected: int
    links_duplicate: int
    links_skipped: int


@dataclass(frozen=True)
class ContentLink:
    url: str
    titles: tuple[str, ...]  # distinct caller labels, not fetched page titles


@dataclass(frozen=True)
class MessageContent:
    identity: str
    identity_kind: str  # guid or content_fingerprint
    guid: str | None
    conversation: str | None
    sender: str | None
    direction: str | None
    timestamp: str | None  # UTC RFC 3339, always six fractional digits
    text: str | None
    links: tuple[ContentLink, ...]


@dataclass(frozen=True)
class ContentResult:
    status: str  # complete, partial, invalid
    message: MessageContent | None
    coverage: ContentCoverage
    issues: tuple[ContentIssue, ...]  # codes only; never raw rejected content


def canonicalize_url(value: object) -> str | None:
    """Return a conservative HTTP(S) URL or None, without resolving/fetching it.

    Reject credentials, whitespace, controls, backslashes, ambiguous numeric
    hosts, scoped IPv6 and malformed escapes/ports. Unicode DNS uses IDNA only
    when its round trip preserves the label except case. Paths, query order,
    tracking parameters and fragments retain meaning; escapes are uppercased,
    never decoded. This is syntax normalization, NOT an SSRF or trust decision:
    even private/loopback hosts may be represented, but nothing is opened here.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_URL_CHARS:
        return None
    if any(c.isspace() or unicodedata.category(c) in {"Cc", "Cf", "Cs"}
           or c in '\\<>"`{}|^' for c in value) or _BAD_PERCENT.search(value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            return None
        if "@" in parts.netloc or "%" in parts.netloc:
            return None
        host = parts.hostname
        if not host:
            return None
        if parts.netloc.startswith("["):
            end = parts.netloc.index("]")
            suffix = parts.netloc[end + 1:]
            if suffix and not re.fullmatch(r":[0-9]+", suffix):
                return None
            host = "[" + ipaddress.IPv6Address(host).compressed + "]"
        else:
            if ":" in parts.netloc and not re.fullmatch(r"[^:]+:[0-9]+", parts.netloc):
                return None
            labels = host.split(".")
            encoded = []
            for label in labels:
                ascii_label = label.encode("idna").decode("ascii").lower()
                if not _DNS_LABEL.fullmatch(ascii_label):
                    return None
                # Validate caller-supplied punycode as well as Unicode input.
                decoded = ascii_label.encode("ascii").decode("idna")
                if not label.isascii():
                    original_label = unicodedata.normalize("NFC", label.lower())
                    if unicodedata.normalize("NFC", decoded.lower()) != original_label:
                        return None
                encoded.append(ascii_label)
            host = ".".join(encoded)
            if len(host) > 253:
                return None
            # Browsers may interpret decimal/octal/hex host spellings as IPv4.
            if re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", encoded[-1]):
                host = str(ipaddress.IPv4Address(host))
        port = parts.port
        if port == 0:
            return None
        scheme = parts.scheme.lower()
        authority = host if port is None or (scheme, port) in {("http", 80), ("https", 443)} else f"{host}:{port}"

        def component(text: str, safe: str) -> str:
            return _PERCENT.sub(lambda m: m.group().upper(), quote(text, safe=safe + "%"))

        path = component(parts.path or "/", "/:@!$&'()*+,;=-._~")
        query = component(parts.query, "/?:@!$&'()*+,;=-._~")
        fragment = component(parts.fragment, "/?:@!$&'()*+,;=-._~")
        # Preserve explicit empty query/fragment delimiters too.
        url = f"{scheme}://{authority}{path}"
        if "?" in value.split("#", 1)[0]:
            url += "?" + query
        if "#" in value:
            url += "#" + fragment
        return url if len(url) <= MAX_URL_CHARS else None
    except (ValueError, UnicodeError):
        return None


def _timestamp(value: object) -> str | None:
    try:
        if isinstance(value, str):
            if not _RFC3339.fullmatch(value) or value.endswith("-00:00"):
                return None
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif type(value) in (int, float):
            if not math.isfinite(value):
                return None
            parsed = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=value)
        else:
            return None
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def _clean_text(value: str, limit: int) -> tuple[str, tuple[str, ...]]:
    codes = []
    if len(value) > limit:
        value = value[:limit]
        codes.append("truncated")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    repaired = "".join(
        "\ufffd" if unicodedata.category(c) in {"Cc", "Cs"} and c not in "\n\t" else c
        for c in value
    )
    if repaired != value:
        codes.append("replaced_control_or_surrogate")
    normalized = unicodedata.normalize("NFC", repaired)
    # A few Unicode characters expand even in NFC (e.g. U+0344).
    if len(normalized) > limit:
        normalized = normalized[:limit]
        if "truncated" not in codes:
            codes.append("truncated")
    return normalized, tuple(codes)


def _digest(kind: str, value: object) -> str:
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"message:{kind}:" + hashlib.sha256(payload.encode("ascii")).hexdigest()


def normalize_message_content(value: object) -> ContentResult:
    """Normalize a supplied dict, retaining usable content with explicit loss.

    No sender/direction is inferred from labels or body text. GUID/conversation/
    sender are opaque and preserved exactly; empty, surrounding-whitespace or
    control-bearing values are rejected, rather than aliased to a different ID.
    Direction accepts incoming/outgoing, case-insensitively. Missing metadata
    and missing content channels are partial, not proof of source absence.
    """
    issues: list[ContentIssue] = []
    is_record = isinstance(value, dict)
    record = value if is_record else {}
    if not is_record:
        issues.append(ContentIssue("record", "invalid_type"))
    supplied = tuple(field for field in _FIELDS if field in record)
    metadata: dict[str, str | None] = {}
    for field in _FIELDS[:5]:
        raw = record.get(field)
        normalized = None
        if field == "timestamp":
            normalized = _timestamp(raw)
        elif field == "direction":
            if isinstance(raw, str) and len(raw) <= MAX_ID_CHARS and raw.strip().lower() in {"incoming", "outgoing"}:
                normalized = raw.strip().lower()
        elif isinstance(raw, str) and 0 < len(raw) <= MAX_ID_CHARS:
            if raw == raw.strip() and not any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in raw):
                normalized = raw
        if normalized is None:
            issues.append(ContentIssue(field, "missing" if raw is None else "invalid"))
        metadata[field] = normalized

    raw_text = record.get("text")
    text = None
    text_state = "missing"
    if isinstance(raw_text, str):
        text, codes = _clean_text(raw_text, MAX_TEXT_CHARS)
        issues.extend(ContentIssue("text", code) for code in codes)
        text_state = "partial" if codes else "complete" if text.strip() else "empty"
    else:
        text_state = "missing" if raw_text is None else "invalid"
        issues.append(ContentIssue("text", text_state))

    raw_links = record.get("links")
    links_state = "missing" if raw_links is None else "invalid"
    link_count = None
    examined = accepted = rejected = duplicate = skipped = 0
    by_url: dict[str, set[str]] = {}
    if isinstance(raw_links, (list, tuple)):
        link_count = len(raw_links)
        skipped = max(0, link_count - MAX_LINKS)
        links_state = "partial" if skipped else "complete"
        if skipped:
            issues.append(ContentIssue("links", "truncated"))
        for index, raw_link in enumerate(raw_links[:MAX_LINKS]):
            examined += 1
            url = canonicalize_url(raw_link.get("url")) if isinstance(raw_link, dict) else None
            if url is None:
                rejected += 1
                links_state = "partial"
                issues.append(ContentIssue("links", "invalid_url", index))
                continue
            accepted += 1
            if url in by_url:
                duplicate += 1
            titles = by_url.setdefault(url, set())
            title = raw_link.get("title")
            if title is not None:
                if not isinstance(title, str):
                    codes = ("invalid_title",)
                else:
                    title, codes = _clean_text(title, MAX_TITLE_CHARS)
                    if title.strip():
                        titles.add(title)
                if codes:
                    links_state = "partial"
                    issues.extend(ContentIssue("links", code, index) for code in codes)
    else:
        issues.append(ContentIssue("links", links_state))
    links = tuple(ContentLink(url, tuple(sorted(titles))) for url, titles in sorted(by_url.items()))
    coverage = ContentCoverage(
        supplied, tuple(field for field, item in metadata.items() if item is not None),
        text_state, links_state, link_count, examined, accepted, rejected, duplicate, skipped,
    )
    if not (text and text.strip()) and not links:
        issues.append(ContentIssue("record", "no_usable_content"))
        return ContentResult("invalid", None, coverage, tuple(issues))
    kind = "guid" if metadata["guid"] is not None else "content_fingerprint"
    identity_value = metadata["guid"] if kind == "guid" else {
        **metadata, "text": text, "links": [(link.url, link.titles) for link in links],
    }
    message = MessageContent(_digest(kind, identity_value), kind, **metadata, text=text, links=links)
    return ContentResult("partial" if issues else "complete", message, coverage, tuple(issues))
