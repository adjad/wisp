"""Fetch one public web article as readable text for Find materials (docs/STUDY_TAB.md,
"Find materials").

IMPORTANT rules (an outbound kind of its own):
- https only, port 443 (another port is refused), no user:password in the url, and no IP
  literal in any spelling (dotted, decimal, octal, hex, IPv6, bracketed): a link names a host.
- The host must resolve ONLY to public addresses: loopback, private, link-local, CGNAT
  (100.64/10), multicast, unspecified, reserved and documentation ranges are refused, IPv4
  inside IPv6 (mapped, 6to4, NAT64) is unwrapped first, ".local"/"localhost"/single-label
  names and IP literals of those ranges are refused before any lookup.
- The connection goes to the address that was checked (the url is rewritten to that IP,
  with the real Host header and TLS SNI/certificate check on the real name), so a second
  DNS answer cannot swap in a private address.
- Redirects are followed by hand, at most MAX_REDIRECTS, each one checked again the same way.
- No cookies (a fresh client per hop, its jar never reused), no auth, no Referer, no proxy
  from the environment, a generic User-Agent.
- MAX_BYTES per page (streamed; the rest is never downloaded), ITEM_S per item, at most
  CONCURRENCY at a time; text/html or text/plain only.
- The text is untrusted data. Nothing about a page is logged except the status and host.
"""
from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

log = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024
ITEM_S = 20.0
CONCURRENCY = 6
MAX_REDIRECTS = 3
TEXT_CHARS = 400_000
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)"
ACCEPT = "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8,application/pdf;q=0.7"
TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/pdf")
PDF = "application/pdf"
PDF_MAX_BYTES = 15 * 1024 * 1024    # a public PDF (notes, a paper, a chapter)
PDF_TEXT_S = 30.0                     # its text is read in a sandboxed child process

MESSAGES = {
    "bad_url": "Wisp only reads public https links.",
    "blocked": "Wisp only reads pages on the public internet.",
    "unreachable": "Wisp couldn't reach that page. Check your connection and try again.",
    "timeout": "That page took too long to answer.",
    "http_error": "That page didn't open (the site refused or it's gone).",
    "too_many_redirects": "That link redirects too many times.",
    "not_text": "That link isn't a web page Wisp can read.",
    "too_large": "That page is too large to read.",
    "empty": "Wisp found no article text on that page. It may need a sign-in.",
}

# Tests swap in an httpx.MockTransport and a fake resolver; production leaves them as they are.
_transport: httpx.AsyncBaseTransport | None = None


class FetchError(Exception):
    def __init__(self, code: str):
        self.code = code if code in MESSAGES else "unreachable"
        self.message = MESSAGES[self.code]
        super().__init__(self.message)


# --- addresses ------------------------------------------------------------------------

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


def public_address(value) -> bool:
    """Whether one address is a public unicast address."""
    try:
        ip = ipaddress.ip_address(value) if not isinstance(value, (ipaddress.IPv4Address,
                                                                   ipaddress.IPv6Address)) else value
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            return False
        inner = ip.ipv4_mapped or (ip.sixtofour if ip.sixtofour is not None else None)
        if inner is None and ip.teredo:
            return False
        if inner is None and ip in _NAT64:
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if ip in _NAT64_LOCAL:
            return False
        if inner is not None:
            return public_address(inner)
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return False
    return bool(ip.is_global and not (ip.is_multicast or ip.is_private or ip.is_loopback or ip.is_link_local
                                      or ip.is_reserved or ip.is_unspecified))


def _literal(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


_NUMERIC_LABEL = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)")


def ip_literal(host: str) -> bool:
    """Any spelling an address parser could take as an IP: dotted, IPv6 (bracketed, mapped,
    scoped), and the inet_aton forms (one decimal number, octal, hex, short dotted). A real
    domain's last label is never all digits or 0x…, so any such label counts."""
    host = host.strip("[]").lower()
    if ":" in host or _literal(host) is not None:
        return True
    labels = [label for label in host.split(".") if label]
    return bool(labels) and (_NUMERIC_LABEL.fullmatch(labels[-1]) is not None
                             or all(_NUMERIC_LABEL.fullmatch(label) for label in labels))


def check_url(url) -> tuple[str, str]:
    """(host, url without fragment) for a url this fetcher may open, else FetchError.
    No network: the host's addresses are checked by resolve()."""
    if not isinstance(url, str) or len(url) > 4000 or any(c in url for c in "\r\n\t\x00 "):
        raise FetchError("bad_url")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise FetchError("bad_url") from None
    if parts.scheme.lower() != "https" or parts.username or parts.password or "@" in parts.netloc:
        raise FetchError("bad_url")
    if port not in (None, 443):
        raise FetchError("bad_url")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise FetchError("bad_url")
    if ip_literal(host) or "[" in parts.netloc:
        raise FetchError("blocked")      # every IP literal, public or not: a link names a host
    if ("." not in host or host == "localhost" or host.endswith((".localhost", ".local", ".internal",
                                                                 ".home.arpa", ".lan", ".intranet"))
            or not re.fullmatch(r"[a-z0-9.-]{1,253}", host)):
        raise FetchError("blocked")
    return host, urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, ""))


async def _getaddrinfo(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


# Tests replace this with a fake table: no DNS in tests.
resolver = _getaddrinfo


async def resolve(host: str) -> str:
    """One public address for host. Every address the name has must be public."""
    if ip_literal(host):
        raise FetchError("blocked")
    try:
        addresses = await resolver(host)
    except (OSError, UnicodeError):
        raise FetchError("unreachable") from None
    addresses = [a.split("%", 1)[0] if isinstance(a, str) else a for a in addresses or []]
    if not addresses:
        raise FetchError("unreachable")
    if not all(public_address(a) for a in addresses):
        raise FetchError("blocked")
    return str(addresses[0])


# --- the request ----------------------------------------------------------------------

def _pinned(url: str, address: str) -> str:
    parts = urlsplit(url)
    ip = ipaddress.ip_address(address)
    netloc = f"[{ip}]" if isinstance(ip, ipaddress.IPv6Address) else str(ip)
    return urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))


_QUIET: contextvars.ContextVar[bool] = contextvars.ContextVar("study_fetch_quiet", default=False)


class _QuietHttpx(logging.Filter):
    """httpx logs every request's full url at INFO; inside this fetcher that would carry the
    page's path and query, so those records are dropped (only our status-and-host line stays)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _QUIET.get()


for _name in ("httpx", "httpcore"):
    if not any(isinstance(f, _QuietHttpx) for f in logging.getLogger(_name).filters):
        logging.getLogger(_name).addFilter(_QuietHttpx())


async def _one_hop(url: str) -> tuple[httpx.Response | None, bytes, str]:
    """(response, body, location). location is set (and body empty) for a redirect."""
    _QUIET.set(True)      # this task's context only (fetch_article runs in its own task)
    host, url = check_url(url)
    address = await resolve(host)
    headers = {"Host": host, "User-Agent": USER_AGENT, "Accept": ACCEPT, "Accept-Language": "en"}
    async with httpx.AsyncClient(transport=_transport, trust_env=False, follow_redirects=False,
                                 timeout=httpx.Timeout(ITEM_S, connect=10.0)) as client:
        request = client.build_request("GET", _pinned(url, address), headers=headers,
                                       extensions={"sni_hostname": host})
        response = await client.send(request, stream=True)
        try:
            log.info("Study fetch: status=%s host=%s", response.status_code, host)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location") or ""
                if not location:
                    raise FetchError("http_error")
                return response, b"", urljoin(url, location)
            if response.status_code != 200:
                raise FetchError("http_error")
            media = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            if media not in TYPES:
                raise FetchError("not_text")
            cap = PDF_MAX_BYTES if media == PDF else MAX_BYTES
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > cap:
                raise FetchError("too_large")
            parts: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > cap:
                    raise FetchError("too_large")
                parts.append(chunk)
            return response, b"".join(parts), ""
        finally:
            await response.aclose()


async def _fetch(url: str) -> dict:
    for _ in range(MAX_REDIRECTS + 1):
        try:
            response, body, location = await _one_hop(url)
        except FetchError:
            raise
        except httpx.TimeoutException:
            raise FetchError("timeout") from None
        except httpx.HTTPError:
            raise FetchError("unreachable") from None
        if location:
            url = location
            continue
        media = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if media == PDF:
            if not body.startswith(b"%PDF-"):
                raise FetchError("not_text")
            return {"url": url, "title": "", "text": await pdf_text(body), "pdf": True}
        text = _decode(body, response)
        if media == "text/plain":
            title, text = "", text.strip()
        else:
            title, text = readable(text)
        text = text[:TEXT_CHARS]
        if not text.strip():
            raise FetchError("empty")
        return {"url": url, "title": title, "text": text}
    raise FetchError("too_many_redirects")


async def pdf_text(data: bytes) -> str:
    """A downloaded PDF's text, extracted in service/documents' child process (killed at its
    deadline or memory limit), never in the service. The text is untrusted data."""
    from service import documents
    try:
        text, _ = await documents.extract_async("download.pdf", data, timeout=PDF_TEXT_S, large=True)
    except (ValueError, TimeoutError, asyncio.TimeoutError):
        raise FetchError("empty") from None
    text = text[:TEXT_CHARS]
    if not text.strip():
        raise FetchError("empty")
    return text


def _decode(body: bytes, response: httpx.Response) -> str:
    charset = response.charset_encoding
    if not charset:
        head = body[:2048].decode("ascii", errors="ignore")
        match = re.search(r"""<meta[^>]+charset=["']?([A-Za-z0-9_-]{1,40})""", head, re.I)
        charset = match.group(1) if match else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


async def fetch_article(url: str, *, timeout_s: float = ITEM_S) -> dict:
    """{"url" (after redirects), "title", "text"} for one public page; FetchError otherwise."""
    try:
        # Its own task, so the quiet-httpx flag stays in that task's context.
        return await asyncio.wait_for(asyncio.ensure_future(_fetch(url)), max(1.0, min(float(timeout_s), ITEM_S)))
    except (TimeoutError, asyncio.TimeoutError):
        raise FetchError("timeout") from None


async def fetch_many(urls: list[str], *, timeout_s: float = ITEM_S) -> list[dict | FetchError]:
    """fetch_article for each url, at most CONCURRENCY at a time, in order; a failure is its
    FetchError in that place."""
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(url):
        async with gate:
            try:
                return await fetch_article(url, timeout_s=timeout_s)
            except FetchError as exc:
                return exc
    return list(await asyncio.gather(*(one(u) for u in urls)))


# --- readable text --------------------------------------------------------------------

_SKIP = frozenset(("script", "style", "nav", "footer", "aside", "noscript", "template", "svg", "form",
                   "iframe", "button", "select", "canvas", "object"))
_BLOCKS = {"p": "", "li": "- ", "blockquote": "", "pre": "", "figcaption": "", "dt": "", "dd": "",
           "h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}
_VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                   "source", "track", "wbr"))
_WS = re.compile(r"\s+")


class _Reader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.title_parts: list[str] = []
        self.in_title = False
        self.blocks: list[str] = []
        self.loose: list[str] = []
        self.current: list[str] | None = None
        self.prefix = ""
        self.block_tag = ""

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        if self.skip:
            if tag in _SKIP:
                self.skip += 1
            return
        if tag in _SKIP:
            self.skip = 1
            return
        if tag == "title":
            self.in_title = True
        elif tag in _BLOCKS and self.current is None:
            self.current, self.prefix, self.block_tag = [], _BLOCKS[tag], tag

    def handle_endtag(self, tag):
        if self.skip:
            if tag in _SKIP:
                self.skip -= 1
            return
        if tag == "title":
            self.in_title = False
        elif self.current is not None and tag == self.block_tag:
            text = _WS.sub(" ", "".join(self.current)).strip()
            if text:
                self.blocks.append(self.prefix + text)
            self.current = None

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title_parts.append(data)
        elif self.current is not None:
            self.current.append(data)
        elif data.strip():
            self.loose.append(data)


def readable(html: str) -> tuple[str, str]:
    """(title, text): the page's title, headings and paragraphs (and list items, quotes and
    preformatted text); script, style, nav, footer and aside are dropped. A page with no such
    blocks falls back to its other visible text."""
    reader = _Reader()
    try:
        reader.feed(html[: MAX_BYTES * 2])
        reader.close()
    except Exception:  # noqa: BLE001 — html.parser is lenient; a broken page reads as what it got
        pass
    if reader.current:
        text = _WS.sub(" ", "".join(reader.current)).strip()
        if text:
            reader.blocks.append(reader.prefix + text)
    title = _WS.sub(" ", "".join(reader.title_parts)).strip()[:300]
    blocks = reader.blocks
    if not blocks:
        blocks = [t for t in (_WS.sub(" ", x).strip() for x in reader.loose) if t]
    return title, "\n\n".join(blocks)
