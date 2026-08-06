"""Live external data via a real HTTP fetch — one dedicated tool instead of
gpt-oss improvising run_shell curl chains for weather/prices/news.

That improvisation was measured to be unreliable: multi-attempt tool loops
(4-8 calls), shell/grep incompatibilities (BSD grep lacks -P), occasional
30s+ stalls, and — worst — the model falling back to a plausible-looking
NUMBER FROM MEMORY when the fetch failed (a confidently wrong bitcoin price
with a fabricated "source"). A single reliable fetch call, plus the agent
system prompt's instruction to report failure honestly instead of guessing
(see service/agent/loop.py), targets both problems: fewer round-trips, and
nothing to fabricate around when the real data is one clean call away.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx

from service.tools.registry import register

# web_fetch takes a URL chosen by the model, and the model's idea of a URL can
# be steered by whatever it just read — a web page, an email, a message. So this
# tool is reachable by anything that can get text in front of the model, which
# makes it the natural way to turn Wisp into a probe of its own machine and LAN:
# oMLX's API on 127.0.0.1:8000, Wisp's own backend on :8765, a router admin page
# on 192.168.x.x, cloud metadata on 169.254.169.254.
#
# So: resolve the host and refuse anything that isn't a public address. The check
# runs on every redirect hop too, since a perfectly innocent public URL is
# allowed to 302 straight to localhost.
_MAX_REDIRECTS = 4


def _blocked_reason(host: str) -> str | None:
    """None if `host` resolves only to public addresses, else why it's refused."""
    if not host:
        return "no host in URL"
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as e:
        return f"could not resolve {host!r} ({e.__class__.__name__})"

    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return f"could not parse address {raw!r} for {host!r}"
        # is_global is False for loopback, private, link-local, multicast,
        # reserved and unspecified ranges — everything we want to exclude,
        # in one check that stays correct for IPv6 too.
        if not ip.is_global:
            return (
                f"{host!r} resolves to {ip}, which is a private, loopback or "
                "otherwise non-public address"
            )
    return None


async def _check_host(host: str) -> str | None:
    return await asyncio.to_thread(_blocked_reason, host)

MAX_CHARS = 3000
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_BLANK_RE = re.compile(r"\n{3,}")

_BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_CURL_UA = "curl/8.4.0"
# Per-host UA override. Most sites (Yahoo Finance's chart API included — see
# below) work fine, or ONLY work, with a browser-like UA; verified Yahoo's v8
# chart endpoint 429s on a curl-like UA but returns 200 on a browser UA. wttr.in
# is the opposite: it explicitly sniffs for a curl-like UA to decide whether to
# serve clean plain text or its full HTML page (verified: identical URL, only
# the UA differed). One static UA can't satisfy both, so pick per-host instead
# of guessing at a single "safe" default.
_CURL_UA_HOSTS = ("wttr.in",)


def _clean_html(body: str) -> str:
    body = _TAG_RE.sub(" ", body)
    body = _ANY_TAG_RE.sub(" ", body)
    body = _WS_RE.sub(" ", body)
    body = _BLANK_RE.sub("\n\n", body)
    return body.strip()


def _clip(s: str) -> str:
    return s if len(s) <= MAX_CHARS else s[:MAX_CHARS] + f"\n…[truncated {len(s) - MAX_CHARS} chars]"


@register(
    "web_fetch",
    "Fetch a URL over HTTP(S) and return its text content — for live external "
    "data with no dedicated tool: weather, stock/crypto prices, sports scores, "
    "news, or looking up any public web page/API the user asks about. GET only, "
    "no auth, no cookies. Known reliable no-key endpoints:\n"
    "  - Weather: https://wttr.in/<city>?format=3 (one-line) or no format= for "
    "a fuller forecast.\n"
    "  - Crypto price: https://api.coingecko.com/api/v3/simple/price?ids=<coin>"
    "&vs_currencies=usd (e.g. ids=bitcoin, ids=ethereum) — returns JSON.\n"
    "  - Stock/ETF price: https://query1.finance.yahoo.com/v8/finance/chart/"
    "<TICKER>?range=1d&interval=1d (e.g. NVDA, SPY, AAPL) — returns JSON; read "
    "chart.result[0].meta.regularMarketPrice (and .currency). ALWAYS include "
    "?range=1d&interval=1d — without it the response is mostly a full day of "
    "intraday price-history arrays you don't need, which bloats past this "
    "tool's truncation limit and pushes the actual price out of what you see. "
    "Do NOT use the v7 'quote' endpoint or any other finance API "
    "(financialmodelingprep, twelvedata, alpha vantage, etc.) — they require an "
    "API key/auth this tool doesn't have and will fail; the v8 chart endpoint "
    "above (WITH the range/interval params) is the one that actually works "
    "with no key.\n"
    "  - DON'T KNOW THE TICKER (user named a company, not a symbol, e.g. "
    "'Vicor Corp')? Resolve it FIRST with https://query1.finance.yahoo.com/v1/"
    "finance/search?q=<company name> — returns JSON, read quotes[0].symbol — "
    "THEN call the v8 chart endpoint above with that symbol. This is the "
    "correct and ONLY way to resolve a company name: do NOT guess a ticker "
    "from the company name, and do NOT fall back to fetching Google/Bing/"
    "MarketWatch/Nasdaq/etc — general web pages return mostly navigation/ad "
    "HTML, not the data you need, and finance sites routinely block "
    "non-browser fetches (401/403). Two calls (search, then chart) resolves "
    "any company name; if the search returns nothing plausible, say so — "
    "don't keep guessing.\n"
    "  - News: https://news.google.com/rss/search?q=<topic>&hl=en-US&gl=US&"
    "ceid=US:en — returns an RSS/XML feed of recent headlines. q is REQUIRED "
    "and must not be empty — an empty q returns HTTP 404. For a generic "
    "\"what's in the news\" ask with no specific topic, use q=top stories.\n"
    "HTML responses are stripped of tags/scripts down to plain text; JSON/XML/"
    "plain text are returned as-is (truncated if long). If a fetch errors or "
    "times out, ONE retry against a documented alternate endpoint above is "
    "fine — but if that also fails, STOP and tell the user you couldn't "
    "retrieve it. Do NOT keep trying more and more unrelated URLs (general "
    "search engines, other news/finance sites, etc.) hoping one works, and do "
    "NOT answer the live-data question from memory instead.",
    {"type": "object",
     "properties": {
         "url": {"type": "string", "description": "the http(s) URL to fetch"},
     },
     "required": ["url"]},
    category="web_read",
)
async def web_fetch(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return f"(refusing to fetch {url!r} — only http:// and https:// URLs are allowed)"
    host = urlparse(url).hostname or ""
    ua = _CURL_UA if any(host == h or host.endswith("." + h) for h in _CURL_UA_HOSTS) else _BROWSER_UA

    # Redirects are followed by hand rather than by httpx, so every hop gets the
    # same address check as the original URL.
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=10) as c:
            for _ in range(_MAX_REDIRECTS):
                blocked = await _check_host(urlparse(url).hostname or "")
                if blocked:
                    return f"(refusing to fetch {url} — {blocked})"
                r = await c.get(url, headers={"User-Agent": ua})
                if r.status_code in (301, 302, 303, 307, 308):
                    nxt = r.headers.get("location")
                    if not nxt:
                        break
                    url = str(httpx.URL(url).join(nxt))
                    if not re.match(r"^https?://", url, re.I):
                        return f"(refusing to follow redirect to {url!r} — not http(s))"
                    continue
                break
            else:
                return f"(too many redirects fetching {url})"
    except httpx.TimeoutException:
        return f"(timed out fetching {url} — try again or tell the user it's unavailable)"
    except Exception as e:  # noqa: BLE001
        return f"(error fetching {url}: {type(e).__name__}: {e})"
    if r.status_code >= 400:
        return f"(fetch failed: {url} returned HTTP {r.status_code})"
    ctype = r.headers.get("content-type", "")
    body = r.text
    if "html" in ctype:
        body = _clean_html(body)
    return _clip(body.strip() or "(empty response)")
