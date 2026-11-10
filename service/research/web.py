"""Deterministic web discovery and safe content extraction.

The chat model never constructs search-result objects and never receives raw
HTML.  That matters for ordinary small-model reliability, and it is a hard
security boundary for an abliterated checkpoint: text found on a page is data,
not an instruction and never a route to Wisp's action tools.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import datetime
import html
import io
import ipaddress
import json
import os
import re
import socket
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, replace
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, quote_plus, unquote, urljoin, urlparse, urlunparse

import httpx
from pypdf import PdfReader

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36 WispResearch/1.0")
# For APIs that WANT to know they're talking to a bot (Wikimedia's policy is
# explicit about this): an honest, non-browser-spoofed identifier. Reusing
# the browser-shaped _UA here reads as disguised traffic and gets 403'd.
_WIKI_UA = "WispResearch/1.1 (https://github.com/adjad/wisp; low-volume personal research)"
_MAX_FETCH_BYTES = 2_500_000
_MAX_TEXT_CHARS = 180_000
_MAX_TITLE_CHARS = 300
_MAX_REDIRECTS = 5
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")
_DATE_RE = re.compile(
    r"\b(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},\s+20\d{2})\b", re.I)
_OPENALEX_SEARCH_LOCK = asyncio.Lock()
_WIKIMEDIA_SEARCH_LOCK = asyncio.Lock()
# Includes HTTP, parsing, and time waiting for a provider's courtesy lock.
_SEARCH_DEADLINE_SECONDS = 12.0


class WebError(RuntimeError):
    pass


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    domain: str = ""
    rank: int = 0
    query: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class SearchResults(list[SearchHit]):
    """List-compatible results with an honest description of limited coverage."""
    def __init__(self, hits=(), *, coverage_note: str = "") -> None:
        super().__init__(hits)
        self.coverage_note = coverage_note


@dataclass
class Page:
    url: str
    canonical_url: str
    title: str
    text: str
    content_type: str
    published_at: str = ""
    via_archive: bool = False
    # The day an archive copy was saved (YYYY-MM-DD), or "" when it is not an
    # archive copy or the day is unknown.
    archived_at: str = ""


def canonicalize_url(raw: str) -> str:
    """Stable URL identity without tracking parameters or fragments."""
    raw = html.unescape((raw or "").strip())
    p = urlparse(raw)
    if p.scheme not in {"http", "https"} or not p.hostname:
        return ""
    host = p.hostname.lower().rstrip(".")
    port = p.port
    netloc = host
    if port and not ((p.scheme == "http" and port == 80) or
                     (p.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    kept = []
    for part in p.query.split("&") if p.query else []:
        key = part.split("=", 1)[0].lower()
        if key.startswith("utm_") or key in {
                "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}:
            continue
        kept.append(part)
    path = re.sub(r"/{2,}", "/", p.path or "/")
    return urlunparse((p.scheme.lower(), netloc, path, "", "&".join(kept), ""))


def _public_ip(value: str) -> bool:
    """True only for an address on the public internet.

    ``is_global`` is what keeps out shared address space (100.64.0.0/10:
    carrier NAT and tailnet devices), which none of the other flags cover. The
    other flags stay, because ``is_global`` alone accepts the NAT64 form of a
    private address. An IPv4-mapped IPv6 address must pass as itself and as the
    IPv4 address it carries.
    """
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    for address in (ip,) if mapped is None else (ip, mapped):
        if (not address.is_global or address.is_private or address.is_loopback
                or address.is_link_local or address.is_multicast
                or address.is_reserved or address.is_unspecified):
            return False
    return True


def _request_url(raw: str, canonical: str) -> str:
    """The address to request: the one given, on the checked host.

    ``canonicalize_url`` is an identity key for spotting the same page twice.
    It drops parameters such as ``ref`` and ``source`` that some sites need
    (a GitHub contents URL's ``?ref=dev`` picks the branch), so it is not the
    address to fetch. Scheme, host and port come from the canonical form,
    which is what was checked; userinfo and the fragment are dropped; path
    and query are kept as given.
    """
    original = urlparse(html.unescape((raw or "").strip()))
    checked = urlparse(canonical)
    return urlunparse((checked.scheme, checked.netloc, original.path or "/",
                       original.params, original.query, ""))


async def _resolve_public(raw: str) -> tuple[str, list[str]]:
    """Validate a destination and return it with the addresses that passed.

    Every DNS answer has to be public. The fetch then connects to one of the
    returned addresses and to nothing else (``_PinnedTransport``), so a name
    cannot give a public answer here and a private one to the connection.
    The returned URL is the request address (``_request_url``), on the host
    that was checked.
    """
    canonical = canonicalize_url(raw)
    if not canonical:
        raise WebError("only public http(s) URLs are allowed")
    url = _request_url(raw, canonical)
    p = urlparse(canonical)
    host = p.hostname or ""
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise WebError("local and private-network URLs are blocked")
    try:
        if ipaddress.ip_address(host):
            if not _public_ip(host):
                raise WebError("local and private-network URLs are blocked")
            return url, [host]
    except ValueError:
        pass

    def _resolve() -> list[str]:
        rows = socket.getaddrinfo(
            host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
        return list(dict.fromkeys(row[4][0] for row in rows))

    try:
        addresses = await asyncio.wait_for(asyncio.to_thread(_resolve), timeout=4.0)
    except Exception as exc:  # noqa: BLE001
        raise WebError(f"could not resolve {host}: {type(exc).__name__}") from exc
    if not addresses or any(not _public_ip(addr) for addr in addresses):
        raise WebError("URL resolves to a local or private-network address")
    # One address of each family first, IPv4 leading: the connection tries the
    # first and falls back to the second, so an IPv6-only network still works.
    v4 = [addr for addr in addresses if ":" not in addr]
    v6 = [addr for addr in addresses if ":" in addr]
    return url, v4[:1] + v6[:1] + v4[1:] + v6[1:]


async def validate_public_url(raw: str) -> str:
    """Validate every destination, including DNS answers, before connecting."""
    return (await _resolve_public(raw))[0]


_PINNED_ADDRESSES = "wisp_pinned_addresses"
_PINNED_TRIES = 2


class _PinnedTransport(httpx.AsyncBaseTransport):
    """Open the connection to an address that was checked, and to no other.

    ``_resolve_public`` looks a name up and checks every answer. Left alone,
    httpx looks the name up a second time to connect, and a hostile name can
    answer that second lookup with 127.0.0.1 or a LAN address (DNS rebinding).
    So the request goes to the checked address itself. The Host header, the
    TLS server name and the certificate check all stay on the real name, and
    the client still sees the request under its real URL. A request that
    carries no checked address is refused.

    Only the client's direct transport is wrapped. When the system sends web
    traffic through a proxy, the proxy opens the connection and does its own
    lookup; that path is unchanged.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        addresses = list(request.extensions.get(_PINNED_ADDRESSES) or ())[:_PINNED_TRIES]
        if not addresses:
            raise WebError("could not resolve a checked address for this request")
        extensions = {key: value for key, value in request.extensions.items()
                      if key != _PINNED_ADDRESSES}
        if request.url.scheme == "https":
            extensions["sni_hostname"] = request.url.raw_host.decode("ascii")
        for index, address in enumerate(addresses):
            pinned = httpx.Request(request.method, request.url.copy_with(host=address),
                                   headers=request.headers, stream=request.stream,
                                   extensions=extensions)
            try:
                return await self._inner.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if index + 1 == len(addresses):
                    raise
        raise WebError("could not resolve a checked address for this request")

    async def aclose(self) -> None:
        await self._inner.aclose()


def _pin_connections(client: httpx.AsyncClient) -> None:
    """Make ``client`` connect only where ``_PinnedTransport`` allows."""
    inner = getattr(client, "_transport", None)
    if not isinstance(inner, httpx.AsyncBaseTransport):
        # httpx no longer keeps its direct transport here. Refuse to fetch
        # rather than fetch with the second lookup back.
        raise WebError("could not resolve a checked address: connection pinning is unavailable")
    client._transport = _PinnedTransport(inner)


class _DDGParser(HTMLParser):
    """Narrow parser for DuckDuckGo's stable HTML results page."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._kind = ""
        self._href = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self._kind, self._href, self._parts = "title", a.get("href") or "", []
        elif tag in {"a", "div"} and "result__snippet" in classes:
            self._kind, self._parts = "snippet", []

    def handle_data(self, data: str) -> None:
        if self._kind:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._kind == "title" and tag == "a":
            title = _clean_inline(" ".join(self._parts))
            if title and self._href:
                self.results.append({"title": title, "href": self._href, "snippet": ""})
            self._kind, self._href, self._parts = "", "", []
        elif self._kind == "snippet" and tag in {"a", "div"}:
            snippet = _clean_inline(" ".join(self._parts))
            if self.results and snippet:
                self.results[-1]["snippet"] = snippet
            self._kind, self._parts = "", []


class _BingParser(HTMLParser):
    """Fallback parser for Bing's ordinary result list."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._in_result = False
        self._in_h2 = False
        self._kind = ""
        self._href = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        if tag == "li" and "b_algo" in classes:
            self._in_result = True
        elif self._in_result and tag == "h2":
            self._in_h2 = True
        elif self._in_result and self._in_h2 and tag == "a":
            self._kind, self._href, self._parts = "title", a.get("href") or "", []
        elif self._in_result and self.results and tag == "p":
            self._kind, self._parts = "snippet", []

    def handle_data(self, data: str) -> None:
        if self._kind:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._kind == "title" and tag == "a":
            title = _clean_inline(" ".join(self._parts))
            if title and self._href:
                self.results.append({"title": title, "href": self._href, "snippet": ""})
            self._kind, self._href, self._parts = "", "", []
        elif self._kind == "snippet" and tag == "p":
            snippet = _clean_inline(" ".join(self._parts))
            if self.results and snippet:
                self.results[-1]["snippet"] = snippet
            self._kind, self._parts = "", []
        if tag == "h2":
            self._in_h2 = False
        elif tag == "li" and self._in_result:
            self._in_result = False


def _clean_inline(value: str) -> str:
    return _WS.sub(" ", html.unescape(value or "")).strip()


def _ddg_target(href: str) -> str:
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    p = urlparse(href)
    if "duckduckgo.com" in (p.hostname or ""):
        target = parse_qs(p.query).get("uddg", [""])[0]
        if target:
            # parse_qs has decoded it once already; decoding again turned
            # %2B into + and %25 into %, a different address.
            return target
    return href


def _bing_target(href: str) -> str:
    """Decode Bing's `ck/a?...&u=a1<base64>` wrapper to the real result URL."""
    href = html.unescape(href)
    p = urlparse(href)
    if (p.hostname or "").lower().endswith("bing.com") and p.path.startswith("/ck/"):
        encoded = parse_qs(p.query).get("u", [""])[0]
        if encoded.startswith("a1"):
            raw = encoded[2:]
            try:
                raw += "=" * (-len(raw) % 4)
                decoded = base64.urlsafe_b64decode(raw).decode("utf-8")
                if canonicalize_url(decoded):
                    return decoded
            except (ValueError, UnicodeDecodeError):
                pass
    return href


def _openalex_abstract(index: object) -> str:
    """Reconstruct OpenAlex's compact inverted-index abstract."""
    if not isinstance(index, dict):
        return ""
    positioned: list[tuple[int, str]] = []
    for word, positions in index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int):
                positioned.append((pos, word))
    return " ".join(word for _, word in sorted(positioned))


async def _search_ddg(query: str, limit: int) -> list[SearchHit]:
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=7),
                                 follow_redirects=True,
                                 headers={"User-Agent": _UA, "Accept": "text/html"}) as client:
        response = await client.get(url)
        # DDG currently returns a 202 challenge page to this client. Treating
        # that as a successful empty search silently activated Bing's poisoned
        # anti-bot result page in the old implementation.
        if response.status_code != 200:
            raise WebError(f"DuckDuckGo returned HTTP {response.status_code}")
        parser = _DDGParser()
        parser.feed(response.text)
    out = []
    for row in parser.results:
        try:
            target = _ddg_target(row["href"])
        except ValueError:
            continue
        hit = _search_hit(row["title"], target, row.get("snippet", ""), query)
        if hit and all(previous.url != hit.url for previous in out):
            out.append(hit)
        if len(out) >= limit:
            break
    if not out:
        raise WebError("DuckDuckGo returned no parseable results")
    return out


def _search_hit(title: object, url: object, snippet: object, query: str) -> SearchHit | None:
    """A bad provider row must not discard its valid siblings."""
    if not isinstance(title, str) or not isinstance(url, str):
        return None
    try:
        target = canonicalize_url(url)
    except ValueError:
        return None
    title = _clean_inline(re.sub(r"<[^>]+>", " ", html.unescape(title)))[:300]
    if not target or not title:
        return None
    snippet = snippet if isinstance(snippet, str) else ""
    snippet = _clean_inline(re.sub(r"<[^>]+>", " ", html.unescape(snippet)))[:900]
    return SearchHit(title=title, url=target, snippet=snippet,
        domain=(urlparse(target).hostname or "").removeprefix("www."), query=query)


async def _search_brave(query: str, limit: int) -> list[SearchHit]:
    """Optional independent general-web API; no request without explicit setup."""
    api_key = os.environ.get("WISP_BRAVE_SEARCH_API_KEY", "").strip()
    if not api_key:
        return []
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5),
                                 follow_redirects=False,
                                 headers={"Accept": "application/json",
                                          "X-Subscription-Token": api_key}) as client:
        response = await client.get("https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(limit, 20), "result_filter": "web",
                    "text_decorations": "false"})
        if response.status_code != 200:
            # Never include the authenticated request or remote error payload.
            raise WebError(f"Brave returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise WebError("Brave returned invalid JSON") from exc
    if not isinstance(data, dict) or data.get("error"):
        raise WebError("Brave returned an invalid search response")
    web = data.get("web", {})
    if not isinstance(web, dict) or not isinstance(web.get("results", []), list):
        raise WebError("Brave returned an invalid result list")
    out = []
    for row in web.get("results", []):
        if not isinstance(row, dict):
            continue
        hit = _search_hit(row.get("title"), row.get("url"), row.get("description"), query)
        if hit and all(previous.url != hit.url for previous in out):
            out.append(hit)
        if len(out) >= limit:
            break
    return out


async def _search_openalex(query: str, limit: int) -> list[SearchHit]:
    """Structured, no-key scholarly discovery; never parses a result webpage."""
    params = {"search": query, "per-page": min(limit, 10),
              "select": "id,display_name,publication_year,abstract_inverted_index"}
    api_key = os.environ.get("WISP_OPENALEX_API_KEY", "").strip()
    if api_key:
        params["api_key"] = api_key
    async with _OPENALEX_SEARCH_LOCK:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=7),
                                     headers={"User-Agent": _WIKI_UA,
                                              "Accept": "application/json"}) as client:
            response = await client.get("https://api.openalex.org/works", params=params)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise WebError("OpenAlex returned an invalid result list")
            rows = data["results"]
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not isinstance(row.get("id"), str) or not isinstance(row.get("display_name"), str):
            continue
        raw_id = row["id"]
        work_id = raw_id.rstrip("/").rsplit("/", 1)[-1]
        title = _clean_inline(str(row.get("display_name") or ""))
        if not re.fullmatch(r"W\d+", work_id) or not title:
            continue
        abstract = _openalex_abstract(row.get("abstract_inverted_index"))
        year = str(row.get("publication_year") or "")
        snippet = _clean_inline(f"{year}. {abstract}")[:900]
        hit = _search_hit(title, f"https://openalex.org/{work_id}", snippet, query)
        if hit and all(previous.url != hit.url for previous in out):
            out.append(hit)
        if len(out) >= limit:
            break
    return out


async def _search_wikipedia(query: str, limit: int) -> list[SearchHit]:
    params = {"action": "query", "format": "json", "list": "search",
              "srsearch": query, "srlimit": min(limit, 10), "utf8": "1", "maxlag": "5"}
    # Wikimedia asks API clients to serialize calls and identify themselves.
    # Research fans out subquestions concurrently, so enforce that courtesy at
    # the provider boundary rather than relying on every caller to remember it.
    async with _WIKIMEDIA_SEARCH_LOCK:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=7),
                                     headers={"User-Agent": _WIKI_UA,
                                              "Api-User-Agent": _WIKI_UA,
                                              "Accept": "application/json"}) as client:
            response = await client.get("https://en.wikipedia.org/w/api.php", params=params)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise WebError("Wikipedia returned an invalid search response")
            if data.get("error"):
                raise WebError("Wikipedia API error")
            result = data.get("query", {})
            if not isinstance(result, dict) or not isinstance(result.get("search", []), list):
                raise WebError("Wikipedia returned an invalid result list")
            rows = result.get("search", [])
    out = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("title"), str):
            continue
        title = _clean_inline(row["title"])
        if not title:
            continue
        snippet = _clean_inline(re.sub(r"<[^>]+>", " ", str(row.get("snippet") or "")))
        target = f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
        hit = _search_hit(title, target, snippet, query)
        if hit and all(previous.url != hit.url for previous in out):
            out.append(hit)
        if len(out) >= limit:
            break
    return out


_ACADEMIC_QUERY = re.compile(
    r"\b(?:peer[- ]reviewed|scholarly|systematic reviews?|meta[- ]analys[ei]s|"
    r"clinical trials?|randomi[sz]ed (?:controlled )?trials?|research papers?|scientific (?:studies|evidence)|"
    r"academic (?:research|literature|sources)|papers? (?:on|about))\b", re.I)
_SEARCH_OPERATOR = re.compile(
    r'"|(?:^|\s)(?:-?(?:site|filetype|ext|intitle|allintitle|inurl|allinurl|'
    r'intext|allintext|before|after|lang|language):|-\w)', re.I)
_QUERY_FILLER = set("""a an the and or of for to in on at by with from about as
    is are was were be been how what why when where who which can could do does
    did i me my we our you your please find search web online tell show explain
    research paper papers study studies scientific evidence academic literature
    scholarly peer reviewed systematic review meta analysis clinical trial trials""".split())


def _query_terms(text: str) -> set[str]:
    # Retain short entities, accents and technical anchors such as SQL, Go, C++.
    text = unicodedata.normalize("NFKC", text).casefold()
    return set(re.findall(r"[^\W_]+(?:[+#]+)?", text)) - _QUERY_FILLER


def _background_matches(query: str, hit: SearchHit) -> bool:
    anchors = _query_terms(query)
    matches = anchors & _query_terms(hit.title + " " + hit.snippet)
    # A pair of incidental words in a long query is not enough; short queries
    # still retain their one or two meaningful anchors.
    minimum = max(min(2, len(anchors)), (len(anchors) + 1) // 2)
    return bool(anchors) and len(matches) >= minimum


def _merge_search_groups(groups: dict[str, list[SearchHit]], query: str,
                         wanted: int, academic: bool) -> SearchResults:
    """Preserve general-provider ordering; specialists must earn their slots."""
    hits: list[SearchHit] = []
    seen: set[str] = set()
    general_count = 0
    background_kinds: set[str] = set()
    # Academic requests balance papers and web pages; all others spend the
    # available budget on general-web sources before encyclopedia background.
    primary = (["openalex"] if academic else []) + ["brave", "ddg"]
    fallback = ["wikipedia"] if academic else ["wikipedia", "openalex"]
    for names in (primary, fallback):
        for offset in range(max((len(groups.get(name, [])) for name in names), default=0)):
            for name in names:
                rows = groups.get(name, [])
                if offset >= len(rows) or not isinstance(rows[offset], SearchHit):
                    continue
                row = rows[offset]
                hit = _search_hit(row.title, row.url, row.snippet, query)
                if not hit or hit.url in seen:
                    continue
                if name in {"openalex", "wikipedia"} and not _background_matches(query, hit):
                    continue
                seen.add(hit.url)
                hits.append(replace(hit, rank=len(hits) + 1))
                general_count += name in {"brave", "ddg"}
                if name in {"openalex", "wikipedia"}:
                    background_kinds.add("scholarly" if name == "openalex" else "encyclopedia")
                if len(hits) == wanted:
                    break
            if len(hits) == wanted:
                break
        if len(hits) == wanted:
            break
    note = ""
    if hits and not general_count and not academic:
        kinds = " and ".join(sorted(background_kinds))
        note = (f"General-web results were unavailable. These are {kinds} background "
                "results, not verification of current details, prices, or availability.")
    return SearchResults(hits, coverage_note=note)


async def search_web(query: str, *, limit: int = 8) -> list[SearchHit]:
    """Bounded discovery for chat and research, preserving completed providers.

    Bing HTML/RSS remains excluded following the off-topic live reproduction.
    Specialized APIs are not general-web replacements and do not implement web
    search operators, so constrained queries only go to general-web providers.
    """
    query = _clean_inline(query)[:500]
    if not query:
        return []
    wanted = max(1, min(int(limit), 20))
    constrained = bool(_SEARCH_OPERATOR.search(query))
    academic = bool(_ACADEMIC_QUERY.search(query)) and not constrained
    calls = {"ddg": _search_ddg(query, wanted)}
    if os.environ.get("WISP_BRAVE_SEARCH_API_KEY", "").strip():
        calls["brave"] = _search_brave(query, wanted)
    if academic:
        calls["openalex"] = _search_openalex(query, wanted)
    if not constrained:
        calls["wikipedia"] = _search_wikipedia(query, wanted)
    tasks = {asyncio.create_task(call): name for name, call in calls.items()}
    pending = set(tasks)
    groups: dict[str, list[SearchHit]] = {}
    errors = []
    scholar_started = academic or constrained
    deadline = asyncio.get_running_loop().time() + _SEARCH_DEADLINE_SECONDS
    try:
        while pending:
            done, pending = await asyncio.wait(pending, timeout=max(
                0, deadline - asyncio.get_running_loop().time()),
                return_when=asyncio.FIRST_COMPLETED)
            if not done:
                errors.extend(f"{tasks[task]}: timed out" for task in pending)
                break
            for task in done:
                name = tasks[task]
                try:
                    rows = task.result()
                    if not isinstance(rows, list):
                        raise WebError("invalid result list")
                    groups[name] = rows
                except (Exception, asyncio.CancelledError) as exc:
                    errors.append(f"{name}: {type(exc).__name__}")
            primary = {name: rows for name, rows in groups.items() if name != "wikipedia"}
            if (len(_merge_search_groups(primary, query, wanted, academic)) >= wanted
                    and not (academic and any(tasks[task] == "openalex" for task in pending))):
                break
            # Scientific research often uses subject keywords rather than the
            # word "papers". Preserve scholarly discovery as a relevant-only
            # fallback when general coverage is sparse, within the same budget.
            general = {name: rows for name, rows in groups.items() if name in {"brave", "ddg"}}
            if (not scholar_started
                    and not any(tasks[task] in {"brave", "ddg"} for task in pending)
                    and len(_merge_search_groups(general, query, wanted, False)) < wanted):
                task = asyncio.create_task(_search_openalex(query, wanted))
                tasks[task] = "openalex"
                pending.add(task)
                scholar_started = True
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    hits = _merge_search_groups(groups, query, wanted, academic)
    if not hits and errors:
        raise WebError("search providers failed (" + ", ".join(sorted(errors)) + ")")
    return hits


class _ReadableHTML(HTMLParser):
    """Conservative structural text extractor with source-order preservation."""
    _DROP = {"script", "style", "noscript", "svg", "canvas", "nav", "footer", "form"}
    _BLOCK = {"p", "div", "article", "section", "main", "li", "blockquote",
              "pre", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "br"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.canonical = ""
        self.published = ""
        self._drop_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in self._DROP:
            self._drop_depth += 1
        if self._drop_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag == "link" and a.get("rel", "").lower() == "canonical":
            self.canonical = a.get("href", "")
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in {"article:published_time", "date", "datepublished", "pubdate"}:
                self.published = a.get("content", "")[:80]
        if tag in self._BLOCK:
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._DROP:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if self._drop_depth:
            return
        if tag == "title":
            self._in_title = False
            self.title = _clean_inline(" ".join(self._title_parts))
        if tag in self._BLOCK:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        self._out.append(data)

    def text(self) -> str:
        value = "".join(self._out).replace("\xa0", " ")
        lines = [_clean_inline(line) for line in value.splitlines()]
        # Drop navigation crumbs and retain paragraph-like material. Short
        # headings stay because they carry report structure.
        out: list[str] = []
        for line in lines:
            if not line:
                continue
            if out and line == out[-1]:
                continue
            if len(line) < 2:
                continue
            out.append(line)
        return "\n\n".join(out)[:_MAX_TEXT_CHARS]


def _extract_pdf(data: bytes) -> tuple[str, str]:
    try:
        reader = PdfReader(io.BytesIO(data))
        title = str((reader.metadata or {}).get("/Title") or "")
        pages = []
        for page in reader.pages[:80]:
            text = page.extract_text() or ""
            if text.strip():
                pages.append(text)
        body = _BLANKS.sub("\n\n", "\n\n".join(pages)).strip()
        if not body:
            raise WebError("PDF contains no extractable text (it may be scanned)")
        return title, body[:_MAX_TEXT_CHARS]
    except WebError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WebError(f"could not read PDF: {type(exc).__name__}") from exc


# How many elements a feed's titles and summaries may be read from in total.
# A real feed needs a few hundred (one title, then 30 items); the 2.5 MB fetch
# cap allows about 600,000 elements in all.
_FEED_TEXT_NODES = 500_000


def _feed_text(elem: ET.Element, left: list[int]) -> str:
    """The text inside ``elem``, as ``"".join(elem.itertext())`` gives it,
    read within the budget ``left[0]`` that every call for one feed shares.

    Reading an element's text walks everything under it. Feed elements that
    sit inside one another are each walked again from the top, so a response
    made of nothing but nesting cost the square of its size. The shared budget
    keeps the whole feed to one bounded amount of work.
    """
    left[0] -= 1
    parts = [elem.text or ""]
    stack: list[tuple[ET.Element | None, object]] = [(None, iter(elem))]
    while stack and left[0] > 0:
        owner, children = stack[-1]
        child = next(children, None)
        if child is None:
            stack.pop()
            if owner is not None and owner.tail:
                parts.append(owner.tail)
            continue
        left[0] -= 1
        if child.text:
            parts.append(child.text)
        stack.append((child, iter(child)))
    return _clean_inline("".join(parts))


def _extract_feed(data: bytes) -> tuple[str, str]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise WebError("invalid XML/feed") from exc
    title = ""
    lines: list[str] = []
    left = [_FEED_TEXT_NODES]
    # Text is read only where it is used: the first title that has any, and
    # the title and summary of each item. It used to be read for every element
    # of the document, on the event loop.
    for elem in root.iter():
        local = elem.tag.rsplit("}", 1)[-1].lower()
        if local == "title" and not title:
            title = _feed_text(elem, left)
        if local in {"item", "entry"}:
            item_title = ""
            summary = ""
            for child in elem:
                key = child.tag.rsplit("}", 1)[-1].lower()
                if key == "title":
                    item_title = _feed_text(child, left)
                elif key in {"summary", "description", "content"}:
                    summary = _feed_text(child, left)
            if item_title:
                lines.append(f"{item_title}\n{summary}".strip())
        if len(lines) >= 30 or left[0] <= 0:
            break
    return title, "\n\n".join(lines)[:_MAX_TEXT_CHARS]


# Strict-host mode: set by a caller that authorised ONE specific host (the user
# typed it, or confirmed that URL). The fetch then refuses redirects to any other
# host and never forwards the URL to a third-party archive. Default is off, so
# every other caller keeps the old redirect and Wayback behaviour.
_STRICT_HOST: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "wisp_web_strict_host", default=False)


@contextlib.contextmanager
def strict_host_fetch(enabled: bool = True):
    token = _STRICT_HOST.set(bool(enabled))
    try:
        yield
    finally:
        _STRICT_HOST.reset(token)


def _without_www(host: str) -> str:
    """``www.example.com`` and ``example.com`` are one site for a strict fetch.

    Exactly one leading ``www.`` label, nothing else: any other subdomain or a
    different name is still a different host. Sites redirect between the two
    forms as a matter of course, and neither form was built by the model.
    """
    return host[4:] if host.startswith("www.") and len(host) > 4 else host


_PLAIN_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")


def _cross_host_refusal(landed: str) -> str:
    """Refusal that names where the redirect pointed, so the caller can ask for
    that host itself and have it confirmed. The name comes from the remote
    site's Location header: only a plain hostname is repeated, never a path or
    query, and nothing at all if it is not shaped like one."""
    where = f" ({landed})" if _PLAIN_HOST_RE.match(landed) else ""
    hint = f" To read it, fetch a URL on {landed} directly." if where else ""
    return (f"redirect to a different host{where} refused: the authorised "
            f"destination was not followed there.{hint}")


# Research drops a page with almost no readable text: it is a consent wall, a
# script-only shell or an error stub, and never evidence. A caller that wants
# the body whatever its length (web_fetch on a JSON price or a one-line status
# endpoint) lowers the floor for its own fetch; an empty body is still an error.
_THIN_PAGE_CHARS = 120
_THIN_PAGE_MESSAGE = "page contained too little readable text"
_MIN_TEXT_CHARS: contextvars.ContextVar[int] = contextvars.ContextVar(
    "wisp_web_min_text_chars", default=_THIN_PAGE_CHARS)


@contextlib.contextmanager
def short_text_fetch(min_chars: int = 1):
    """Accept any non-empty response inside this block, however short."""
    token = _MIN_TEXT_CHARS.set(max(1, int(min_chars)))
    try:
        yield
    finally:
        _MIN_TEXT_CHARS.reset(token)


async def _fetch_direct(raw_url: str) -> Page:
    """Fetch one public page with redirect and size limits plus SSRF checks."""
    current, addresses = await _resolve_public(raw_url)
    strict_host = (urlparse(current).hostname or "").lower() if _STRICT_HOST.get() else ""
    data = b""
    ctype = ""
    encoding = "utf-8"
    # No kept-alive connections: two names can share one checked address, and a
    # connection opened for the first must not carry a request for the second.
    async with httpx.AsyncClient(timeout=httpx.Timeout(18, connect=7),
                                 follow_redirects=False,
                                 limits=httpx.Limits(max_connections=10,
                                                     max_keepalive_connections=0),
                                 headers={"User-Agent": _UA, "Accept": "*/*"}) as client:
        _pin_connections(client)
        completed = False
        for _ in range(_MAX_REDIRECTS + 1):
            async with client.stream("GET", current,
                                     extensions={_PINNED_ADDRESSES: addresses}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = response.headers.get("location")
                    if not target:
                        raise WebError("redirect had no destination")
                    current, addresses = await _resolve_public(urljoin(current, target))
                    if strict_host:
                        landed = (urlparse(current).hostname or "").lower()
                        if _without_www(landed) != _without_www(strict_host):
                            raise WebError(_cross_host_refusal(landed))
                    continue
                if response.status_code >= 400:
                    raise WebError(f"HTTP {response.status_code}")
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > _MAX_FETCH_BYTES:
                    raise WebError(f"response exceeded {_MAX_FETCH_BYTES // 1_000_000} MB limit")
                ctype = response.headers.get("content-type", "").lower()
                encoding = response.encoding or "utf-8"
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > _MAX_FETCH_BYTES:
                        raise WebError(f"response exceeded {_MAX_FETCH_BYTES // 1_000_000} MB limit")
                data = bytes(chunks)
                completed = True
                break
        if not completed:
            raise WebError("too many redirects")

    title = ""
    published = ""
    canonical = current
    if "pdf" in ctype or current.lower().endswith(".pdf"):
        title, text = await asyncio.to_thread(_extract_pdf, data)
    elif "xml" in ctype or "rss" in ctype or data.lstrip().startswith(b"<?xml"):
        # Off the event loop, as the PDF branch is: a large feed is parsed in
        # one piece, and replies and heartbeats must keep moving meanwhile.
        title, text = await asyncio.to_thread(_extract_feed, data)
    elif "html" in ctype or b"<html" in data[:2000].lower():
        parser = _ReadableHTML()
        parser.feed(data.decode(encoding, errors="replace"))
        title, text, published = parser.title, parser.text(), parser.published
        if parser.canonical:
            # rel=canonical is the page's own claim about its address. It is
            # believed only for the host that served the page (with or without
            # www.): a page on one site cannot name another site as its own.
            claimed = canonicalize_url(urljoin(current, parser.canonical))
            if claimed and (_without_www((urlparse(claimed).hostname or "").lower())
                            == _without_www((urlparse(current).hostname or "").lower())):
                canonical = claimed
    else:
        text = data.decode(encoding, errors="replace")[:_MAX_TEXT_CHARS]
    text = _BLANKS.sub("\n\n", text).strip()
    if len(text) < _MIN_TEXT_CHARS.get():
        raise WebError(_THIN_PAGE_MESSAGE)
    if not published:
        match = _DATE_RE.search(text[:5000])
        published = match.group(0) if match else ""
    # The title is the page's own text (<title>, a PDF's /Title, a feed's
    # <title>) and callers print it on a line of their own: one line, capped.
    title = " ".join(title.split())[:_MAX_TITLE_CHARS]
    return Page(url=current, canonical_url=canonicalize_url(canonical) or current,
                title=title or (urlparse(current).hostname or current), text=text,
                content_type=ctype, published_at=published)


_WIKIPEDIA_HOST_RE = re.compile(r"^([a-z0-9-]+)\.(?:m\.)?wikipedia\.org$", re.I)
_OPENALEX_WORK_RE = re.compile(r"^/([Ww]\d+)/?$")


async def _fetch_openalex_work(raw_url: str) -> Page | None:
    p = urlparse(raw_url)
    if (p.hostname or "").lower().removeprefix("www.") != "openalex.org":
        return None
    match = _OPENALEX_WORK_RE.fullmatch(p.path)
    if not match:
        return None
    work_id = match.group(1).upper()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=7),
                                     headers={"User-Agent": _WIKI_UA,
                                              "Accept": "application/json"}) as client:
            response = await client.get(f"https://api.openalex.org/works/{work_id}")
            response.raise_for_status()
            row = response.json() or {}
    except Exception:  # noqa: BLE001
        return None
    title = _clean_inline(str(row.get("display_name") or ""))
    abstract = _openalex_abstract(row.get("abstract_inverted_index"))
    if not title or len(abstract) < 120:
        return None
    authors = []
    for authorship in row.get("authorships") or []:
        name = _clean_inline(str((authorship.get("author") or {}).get("display_name") or ""))
        if name:
            authors.append(name)
    year = str(row.get("publication_year") or "")
    doi = str(row.get("doi") or "")
    metadata = "; ".join(part for part in (
        f"Authors: {', '.join(authors[:12])}" if authors else "",
        f"Published: {year}" if year else "",
        f"DOI: {doi}" if doi else "") if part)
    text = f"{title}\n\n{metadata}\n\nAbstract\n\n{abstract}".strip()
    canonical = f"https://openalex.org/{work_id}"
    return Page(url=canonical, canonical_url=canonical, title=title, text=text,
                content_type="text/plain; charset=utf-8", published_at=year)


def _wikipedia_title_from_url(url: str) -> tuple[str, str] | None:
    """Return (language, title) if `url` is an ordinary Wikipedia article URL."""
    p = urlparse(url)
    match = _WIKIPEDIA_HOST_RE.match((p.hostname or "").lower())
    if not match or not p.path.startswith("/wiki/"):
        return None
    lang = match.group(1)
    if lang in {"www", "m"}:
        lang = "en"
    title = unquote(p.path[len("/wiki/"):]).replace("_", " ").strip()
    return (lang, title) if title else None


async def _fetch_wikipedia_article(lang: str, title: str) -> Page | None:
    """Pull a clean plain-text extract from Wikipedia's own Action API.

    Wikimedia's CDN requires a compliant, identifying User-Agent for
    automated traffic and rate-limits/blocks anything that looks like a
    generic scrape of the human-facing article page — which is exactly what
    a plain HTML fetch of a wikipedia.org URL looks like. The Action API is
    Wikimedia's own free, no-key, purpose-built endpoint for programmatic
    access, so routing here isn't a workaround, it's the intended path.
    """
    api_url = f"https://{lang}.wikipedia.org/w/api.php"
    params = {"action": "query", "format": "json", "prop": "extracts",
             "explaintext": "1", "exsectionformat": "plain", "redirects": "1", "titles": title}
    try:
        # A browser-spoofed UA hitting api.php reads as disguised bot traffic
        # and gets 403'd by Wikimedia's own policy layer — the opposite of
        # what a compliant, honestly-identifying UA is for. Use one here.
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=7),
                                     headers={"User-Agent": _WIKI_UA}) as client:
            response = await client.get(api_url, params=params)
            response.raise_for_status()
            data = response.json()
    except Exception:  # noqa: BLE001
        return None
    pages = ((data.get("query") or {}).get("pages")) or {}
    page = next(iter(pages.values()), None)
    if not isinstance(page, dict) or "missing" in page:
        return None
    text = _BLANKS.sub("\n\n", str(page.get("extract") or "")).strip()
    if len(text) < 120:
        return None
    canonical_title = str(page.get("title") or title)
    canonical = f"https://{lang}.wikipedia.org/wiki/{quote(canonical_title.replace(' ', '_'))}"
    return Page(url=canonical, canonical_url=canonical, title=canonical_title,
               text=text[:_MAX_TEXT_CHARS], content_type="text/plain; charset=utf-8", published_at="")


_WAYBACK_AVAILABLE_URL = "https://archive.org/wayback/available"
# Messages raised by validate_public_url itself, BEFORE any HTTP request is
# attempted. A URL rejected for these reasons must never be looked up on the
# Archive either — that would be using a third party to launder a request we
# already decided not to make, not a legitimate content fallback.
_SSRF_MESSAGE_PREFIXES = ("only public http(s) URLs are allowed",
                          "local and private-network URLs are blocked",
                          "could not resolve", "URL resolves to a local or private-network address")


async def _wayback_snapshot_url(url: str) -> str:
    """Ask the Internet Archive whether it holds its own snapshot of `url`.

    This is a separate, consenting archive service answering for itself about
    content it already chose to crawl and serve — not a way around the origin
    site's own access control. Returns "" if archive.org has no snapshot or is
    unreachable; callers fall back to the original error either way.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=5)) as client:
            response = await client.get(_WAYBACK_AVAILABLE_URL,
                                        params={"url": url}, headers={"User-Agent": _UA})
            response.raise_for_status()
            data = response.json()
    except Exception:  # noqa: BLE001
        return ""
    snapshot = (data.get("archived_snapshots") or {}).get("closest") or {}
    if snapshot.get("available") and snapshot.get("url"):
        return str(snapshot["url"])
    return ""


_WAYBACK_STAMP_RE = re.compile(r"/web/(\d{4})(\d{2})(\d{2})")


def _snapshot_date(*urls: str) -> str:
    """The day a Wayback snapshot was saved, as YYYY-MM-DD, or "" if unknown.

    A snapshot address starts its path with a 14-digit timestamp
    (``/web/20230405123456/https://…``). Only digits that make a real date on
    an archive.org address come back, so nothing else from the address can
    reach the text a caller writes around it.
    """
    for url in urls:
        try:
            parsed = urlparse(url or "")
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        match = _WAYBACK_STAMP_RE.match(parsed.path)
        if not match or not (host == "archive.org" or host.endswith(".archive.org")):
            continue
        try:
            return datetime.date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            continue
    return ""


async def fetch_page(raw_url: str) -> Page:
    """Fetch a page directly; on failure (blocked, gone, thin, timed out —
    anything except our own SSRF refusal), try the Wayback Machine's own
    snapshot of the same URL before giving up."""
    strict = _STRICT_HOST.get()
    # Strict mode talks to the authorised host only: no api.openalex.org detour.
    openalex = None if strict else await _fetch_openalex_work(raw_url)
    if openalex:
        return openalex
    wiki = _wikipedia_title_from_url(raw_url)
    if wiki and strict and (urlparse(raw_url).hostname or "").lower() != f"{wiki[0]}.wikipedia.org":
        wiki = None  # the API host would differ from the authorised host
    if wiki:
        article = await _fetch_wikipedia_article(*wiki)
        if article:
            return article
        # Not a real article (Special:/Talk:/a redlink) or the API was
        # unreachable — fall through to the ordinary path below rather than
        # failing outright.
    try:
        return await _fetch_direct(raw_url)
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, WebError) and str(exc).startswith(_SSRF_MESSAGE_PREFIXES):
            raise
        if strict:
            raise  # never hand the URL to archive.org
        if (isinstance(exc, WebError) and str(exc) == _THIN_PAGE_MESSAGE
                and _MIN_TEXT_CHARS.get() < _THIN_PAGE_CHARS):
            raise  # the caller took any length and the live answer was empty
        snapshot_url = await _wayback_snapshot_url(raw_url)
        if not snapshot_url:
            raise
        try:
            archived = await _fetch_direct(snapshot_url)
        except Exception:  # noqa: BLE001
            raise exc from None
        return Page(url=raw_url, canonical_url=archived.canonical_url, title=archived.title,
                   text=archived.text, content_type=archived.content_type,
                   published_at=archived.published_at, via_archive=True,
                   # Where the fetch landed first: the Archive redirects to the
                   # snapshot it really holds, which can be from another day.
                   archived_at=_snapshot_date(archived.url, snapshot_url))


def render_search_results(hits: list[SearchHit]) -> str:
    """Compact, model-friendly output for the ordinary chat web_search tool."""
    if not hits:
        return "(no web results found)"
    rendered = "\n\n".join(
        f"[{i}] {hit.title}\nURL: {hit.url}\n{hit.snippet}".strip()
        for i, hit in enumerate(hits, 1))
    note = getattr(hits, "coverage_note", "")
    return f"{note}\n\n{rendered}" if note else rendered
