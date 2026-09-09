"""Deterministic web discovery and safe content extraction.

The chat model never constructs search-result objects and never receives raw
HTML.  That matters for ordinary small-model reliability, and it is a hard
security boundary for an abliterated checkpoint: text found on a page is data,
not an instruction and never a route to Wisp's action tools.
"""
from __future__ import annotations

import asyncio
import base64
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
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_multicast or ip.is_reserved or ip.is_unspecified)


async def validate_public_url(raw: str) -> str:
    """Validate every destination, including DNS answers, before connecting."""
    url = canonicalize_url(raw)
    if not url:
        raise WebError("only public http(s) URLs are allowed")
    p = urlparse(url)
    host = p.hostname or ""
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise WebError("local and private-network URLs are blocked")
    try:
        if ipaddress.ip_address(host):
            if not _public_ip(host):
                raise WebError("local and private-network URLs are blocked")
            return url
    except ValueError:
        pass

    def _resolve() -> list[str]:
        return list({row[4][0] for row in socket.getaddrinfo(
            host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)})

    try:
        addresses = await asyncio.wait_for(asyncio.to_thread(_resolve), timeout=4.0)
    except Exception as exc:  # noqa: BLE001
        raise WebError(f"could not resolve {host}: {type(exc).__name__}") from exc
    if not addresses or any(not _public_ip(addr) for addr in addresses):
        raise WebError("URL resolves to a local or private-network address")
    return url


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
            return unquote(target)
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


def _extract_feed(data: bytes) -> tuple[str, str]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise WebError("invalid XML/feed") from exc
    title = ""
    lines: list[str] = []
    for elem in root.iter():
        local = elem.tag.rsplit("}", 1)[-1].lower()
        value = _clean_inline("".join(elem.itertext()))
        if local == "title" and value and not title:
            title = value
        if local in {"item", "entry"}:
            item_title = ""
            summary = ""
            for child in elem:
                key = child.tag.rsplit("}", 1)[-1].lower()
                val = _clean_inline("".join(child.itertext()))
                if key == "title": item_title = val
                elif key in {"summary", "description", "content"}: summary = val
            if item_title:
                lines.append(f"{item_title}\n{summary}".strip())
        if len(lines) >= 30:
            break
    return title, "\n\n".join(lines)[:_MAX_TEXT_CHARS]


async def _fetch_direct(raw_url: str) -> Page:
    """Fetch one public page with redirect and size limits plus SSRF checks."""
    current = await validate_public_url(raw_url)
    data = b""
    ctype = ""
    encoding = "utf-8"
    async with httpx.AsyncClient(timeout=httpx.Timeout(18, connect=7),
                                 follow_redirects=False,
                                 headers={"User-Agent": _UA, "Accept": "*/*"}) as client:
        completed = False
        for _ in range(_MAX_REDIRECTS + 1):
            async with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = response.headers.get("location")
                    if not target:
                        raise WebError("redirect had no destination")
                    current = await validate_public_url(urljoin(current, target))
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
        title, text = _extract_feed(data)
    elif "html" in ctype or b"<html" in data[:2000].lower():
        parser = _ReadableHTML()
        parser.feed(data.decode(encoding, errors="replace"))
        title, text, published = parser.title, parser.text(), parser.published
        if parser.canonical:
            canonical = canonicalize_url(urljoin(current, parser.canonical)) or current
    else:
        text = data.decode(encoding, errors="replace")[:_MAX_TEXT_CHARS]
    text = _BLANKS.sub("\n\n", text).strip()
    if len(text) < 120:
        raise WebError("page contained too little readable text")
    if not published:
        match = _DATE_RE.search(text[:5000])
        published = match.group(0) if match else ""
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


async def fetch_page(raw_url: str) -> Page:
    """Fetch a page directly; on failure (blocked, gone, thin, timed out —
    anything except our own SSRF refusal), try the Wayback Machine's own
    snapshot of the same URL before giving up."""
    openalex = await _fetch_openalex_work(raw_url)
    if openalex:
        return openalex
    wiki = _wikipedia_title_from_url(raw_url)
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
        snapshot_url = await _wayback_snapshot_url(raw_url)
        if not snapshot_url:
            raise
        try:
            archived = await _fetch_direct(snapshot_url)
        except Exception:  # noqa: BLE001
            raise exc from None
        return Page(url=raw_url, canonical_url=archived.canonical_url, title=archived.title,
                   text=archived.text, content_type=archived.content_type,
                   published_at=archived.published_at, via_archive=True)


def render_search_results(hits: list[SearchHit]) -> str:
    """Compact, model-friendly output for the ordinary chat web_search tool."""
    if not hits:
        return "(no web results found)"
    rendered = "\n\n".join(
        f"[{i}] {hit.title}\nURL: {hit.url}\n{hit.snippet}".strip()
        for i, hit in enumerate(hits, 1))
    note = getattr(hits, "coverage_note", "")
    return f"{note}\n\n{rendered}" if note else rendered
