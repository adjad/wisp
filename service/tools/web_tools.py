"""Live external data via a real HTTP fetch — one dedicated tool instead of
the agent model improvising run_shell curl chains for weather/prices/news.

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
import re
import time
from urllib.parse import quote, urlparse

import httpx

from service.research.web import (fetch_page as research_fetch_page,
                                  render_search_results, search_web)
from service.tools.registry import register

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


_RSS_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_RSS_TITLE_RE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
_RSS_PUBDATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)
_RSS_SOURCE_RE = re.compile(r"<source[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</source>", re.S)


def _clean_rss(body: str) -> str:
    """Google News RSS (the tool's own documented "news" recipe) as readable
    headline lines instead of raw XML soup.

    MEASURED FAILURE (2026-08-18, immediately after fixing the web_fetch/
    _yahoo_get registry collision above): with real RSS finally reaching the
    model, it answered "what is on the news today?" by prefixing "**From the
    web:**" onto the ENTIRE raw XML document — tags, CDATA, generator/link
    boilerplate and all — rather than extracting headlines. It also called
    web_fetch six times with trivially varied query params (num=, start=)
    apparently hunting for a response shape it could parse. A model this size
    isn't going to reliably parse RSS out of raw markup; do that here, the
    same principle as _clean_html already applies to HTML.

    Deliberately narrow — Google News RSS's actual shape (title/pubDate/
    source per <item>) — not a general XML-to-text reducer.
    """
    items = _RSS_ITEM_RE.findall(body)
    if not items:
        return body  # not the shape expected; caller falls back to raw text
    lines = []
    for item in items[:20]:
        m = _RSS_TITLE_RE.search(item)
        if not m:
            continue
        title = _WS_RE.sub(" ", m.group(1)).strip()
        if not title:
            continue
        src = _RSS_SOURCE_RE.search(item)
        when = _RSS_PUBDATE_RE.search(item)
        bits = [b.strip() for b in (src.group(1) if src else "",
                                    when.group(1) if when else "") if b.strip()]
        suffix = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"- {title}{suffix}")
    return "\n".join(lines) if lines else body


def _clip(s: str) -> str:
    return s if len(s) <= MAX_CHARS else s[:MAX_CHARS] + f"\n…[truncated {len(s) - MAX_CHARS} chars]"


# Finance APIs the model reaches for that all require a key this tool doesn't
# have, plus the general-web pages that return navigation HTML instead of data.
_BAD_FINANCE_HOSTS = ("financialmodelingprep", "twelvedata", "alphavantage",
                      "marketwatch", "nasdaq.com", "bloomberg", "cnbc",
                      "finance.google", "stockanalysis")
_SEARCH_HOSTS = ("google.com/search", "bing.com", "search.yahoo")


def _recovery_hint(url: str, *, status: int | None = None) -> str:
    """What to do INSTEAD, chosen from the URL that just failed.

    These used to live in the tool's description, where every request paid for
    them whether or not anything had gone wrong. Attached to the failure they
    are both cheaper and far better targeted — the model gets the one correction
    that applies rather than a paragraph covering every case at once.
    """
    u = url.lower()
    host = (urlparse(url).hostname or "").lower()

    if any(b in u for b in _BAD_FINANCE_HOSTS):
        return (" That API needs a key this tool doesn't have. Use "
                "https://query1.finance.yahoo.com/v8/finance/chart/<TICKER>"
                "?range=1d&interval=1d instead, resolving a company name to a "
                "ticker first via .../v1/finance/search?q=<name>.")
    if any(s in u for s in _SEARCH_HOSTS):
        return (" General search engines return navigation HTML, not data. Use "
                "https://html.duckduckgo.com/html/?q=<query> for a web search, "
                "or the documented endpoint for weather/crypto/stocks/news.")
    if "finance.yahoo.com" in host and "/v8/finance/chart/" not in u:
        return (" Only the v8 chart endpoint works without a key: "
                "https://query1.finance.yahoo.com/v8/finance/chart/<TICKER>"
                "?range=1d&interval=1d")
    if "news.google.com" in host and status == 404:
        return (" news.google.com needs a non-empty q — try "
                "q=top stories for a generic news ask.")
    if "wikipedia.org" in host:
        return (" That title doesn't exist. Try "
                "https://html.duckduckgo.com/html/?q=<query> instead of "
                "guessing another article title.")
    if status in (401, 403):
        return (" This site blocks non-browser fetches. Don't retry it — use a "
                "documented endpoint above, or tell the user you couldn't get it.")
    return (" Try ONE documented alternate endpoint; if that also fails, stop "
            "and tell the user you couldn't retrieve it rather than trying "
            "more URLs.")


# The RECIPES stay in the description; the ANTI-PATTERNS moved to the failure
# result (see _recovery_hint).
#
# This description was 3,658 chars — ~915 tokens, and measured as 10% of the
# entire unscoped tool budget, paid on every single request that offered
# web_fetch. Since routes are scoped it's worse than that in relative terms: on
# the 2-tool live-web route it was most of the prompt.
#
# Splitting it by WHEN the model needs each part is what makes the cut safe.
# The endpoint recipes have to be here — the model picks a URL before its first
# call, and without them it guesses badly (this tool exists because improvised
# curl chains were unreliable). But "don't use the v7 quote endpoint", "don't
# guess a ticker", "don't fall back to MarketWatch", "one retry then stop" are
# all REMEDIAL: they only matter once a call has failed or is about to be
# repeated. Those now arrive attached to the actual error, which is both
# cheaper and better targeted than restating them on every unrelated request.
# "Don't answer from memory" is dropped entirely — the agent system prompt
# already has a dedicated block on exactly that.
async def _yahoo_get(url: str) -> httpx.Response | str:
    """GET with Yahoo's required browser UA. Returns the response, or an error
    string on failure/timeout — caller checks `isinstance(r, str)`.

    Internal helper only — deliberately UNDECORATED. It used to carry
    `@register("web_fetch", ...)` by accident: inserting this function (and
    _resolve_symbol/_one_quote/get_stock_price) between the real web_fetch's
    decorator and its `async def` left the decorator directly above THIS
    function instead. REGISTRY is a plain dict keyed by name, so that silently
    overwrote the "web_fetch" entry — every model call to web_fetch actually
    ran this function and got back a raw httpx.Response (or a Yahoo-specific
    error string) instead of fetched text. Measured live 2026-08-18: asked
    "what is on the news today?", the tool returned the literal string
    `<Response [200 OK]>`, which the model then couldn't use, and it went on
    to fabricate fake headlines on retry. get_stock_price was unaffected
    because it calls this as a plain function reference, not through the
    registry — only the MODEL-facing "web_fetch" name was shadowed.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=8) as c:
            return await c.get(url, headers={"User-Agent": _BROWSER_UA})
    except httpx.TimeoutException:
        return f"(timed out reaching Yahoo Finance for {url})"
    except Exception as e:  # noqa: BLE001
        return f"(error reaching Yahoo Finance: {type(e).__name__}: {e})"


# A bare ticker: 1-5 letters, optionally one dot-suffix (BRK.B), all caps once
# stripped/upper()'d. Anything else — "nvidia", "the governor's stock", spaces —
# needs the search endpoint to resolve to a real symbol first.
_TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")


async def _resolve_symbol(query: str, *,
                          force_search: bool = False) -> tuple[str, str] | str:
    """(symbol, display_name) for `query`, or an error string.

    `query` may already be a ticker (skip straight to the chart call) or a
    company name / anything else (resolve via Yahoo's search endpoint first).
    Always going through search for non-tickers, rather than guessing the
    ticker from the name, is the whole point — see get_stock_price's docstring
    for the failure this replaces.

    `force_search` skips the ticker shortcut. The shortcut is a guess: it only
    checks that the text LOOKS like a ticker, so a real company name that
    happens to be five letters is taken at face value and never verified.

    MEASURED FAILURE (2026-08-18): "summarize the movements of vicor corp over
    the last month" -> the model passed "VICOR", which matches _TICKER_RE, so
    the chart endpoint was called with it directly and returned 404. Vicor
    Corporation's actual ticker is VICR. The model then tried four different
    periods, got four 404s, and told the user the data "isn't available
    through this source". Search resolves "VICOR" to VICR on the first hit —
    the information was one call away the whole time. Callers now retry with
    force_search=True on a 404 (see _chart_404_retry).
    """
    q = (query or "").strip()
    if not q:
        return "(empty symbol)"
    candidate = q.upper()
    if _TICKER_RE.match(candidate) and not force_search:
        return candidate, candidate
    r = await _yahoo_get(
        f"https://query1.finance.yahoo.com/v1/finance/search?q={quote(q)}")
    if isinstance(r, str):
        return r
    if r.status_code >= 400:
        return f"(symbol search for {q!r} failed: HTTP {r.status_code})"
    try:
        quotes = r.json().get("quotes") or []
    except Exception:  # noqa: BLE001
        return f"(symbol search for {q!r} returned unreadable data)"
    equities = [x for x in quotes if x.get("quoteType") in ("EQUITY", "ETF", None)]
    hit = (equities or quotes or [None])[0]
    if not hit or not hit.get("symbol"):
        return f"(no ticker found for {q!r} — check the spelling or give the symbol directly)"
    return hit["symbol"], hit.get("shortname") or hit.get("longname") or q


async def _chart_404_retry(query: str, resolved: tuple[str, str]
                           ) -> tuple[str, str] | None:
    """Re-resolve `query` through search after its chart call 404'd.

    Returns the corrected (symbol, name), or None if there's nothing to retry:
    the symbol didn't come from the ticker shortcut, search fails too, or
    search just hands back the same symbol that already 404'd.
    """
    symbol, name = resolved
    if name != symbol:          # already came from search — nothing new to try
        return None
    again = await _resolve_symbol(query, force_search=True)
    if isinstance(again, str) or again[0] == symbol:
        return None
    return again


async def _one_quote(query: str) -> str:
    resolved = await _resolve_symbol(query)
    if isinstance(resolved, str):
        return f"{query}: {resolved}"
    symbol, name = resolved
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/{}"
           "?range=1d&interval=1d")
    r = await _yahoo_get(url.format(quote(symbol)))
    if isinstance(r, str):
        return f"{symbol}: {r}"
    if r.status_code == 404 and (fixed := await _chart_404_retry(query, resolved)):
        symbol, name = fixed
        r = await _yahoo_get(url.format(quote(symbol)))
        if isinstance(r, str):
            return f"{symbol}: {r}"
    if r.status_code >= 400:
        return f"{symbol}: (HTTP {r.status_code} fetching quote)"
    try:
        meta = r.json()["chart"]["result"][0]["meta"]
        price, ccy = meta["regularMarketPrice"], meta.get("currency", "USD")
    except Exception:  # noqa: BLE001
        return f"{symbol}: (quote data missing or in an unexpected shape)"
    label = f"{symbol} ({name})" if name and name != symbol else symbol
    return f"{label}: {price} {ccy}"


# --- Historical prices -------------------------------------------------------
#
# The same v8 chart endpoint _one_quote already uses, asked for a real range
# instead of range=1d. Adding it here rather than as a separate tool is
# deliberate: the model reliably picks get_stock_price for anything
# price-shaped, and a second near-identical tool ("get_stock_history") would
# compete with it for exactly the same requests — the failure documented in
# the tool-descriptions note. One tool, one optional time argument.
#
# MEASURED FAILURE (2026-08-18) this fixes: "can you check the price of micron
# over the last month" returned the LIVE price plus "the tool gives the live
# price but doesn't provide a historical chart or a date range, so I can't show
# the last month's price directly". The model was being honest about a real
# capability gap — nothing was broken, the data simply wasn't reachable.

# Yahoo rejects an arbitrary range string, so the accepted set is fixed. The
# interval is chosen here rather than exposed as a second argument: a small
# model given both would have to get two coupled values right (range=5y with
# interval=1d is ~1250 points), and there is exactly one sensible interval per
# range anyway.
_RANGE_INTERVAL = {
    "5d": "1d", "1mo": "1d", "3mo": "1d",
    "6mo": "1wk", "1y": "1wk", "2y": "1wk",
    "5y": "1mo", "10y": "1mo", "max": "1mo",
}

# What the user actually says -> Yahoo's range token. Checked longest-first so
# "6 months" doesn't match the "month" -> 1mo entry.
_PERIOD_WORDS = [
    (r"\b(?:ytd|year\s*to\s*date)\b", "1y"),
    (r"\b(?:10|ten)\s*years?\b", "10y"),
    (r"\b(?:5|five)\s*years?\b", "5y"),
    (r"\b(?:2|two)\s*years?\b", "2y"),
    (r"\b(?:1\s*)?years?\b|\b12\s*months?\b", "1y"),
    (r"\b(?:6|six)\s*months?\b", "6mo"),
    (r"\b(?:3|three)\s*months?\b|\bquarter\b", "3mo"),
    (r"\b(?:1\s*)?months?\b|\b(?:30|4)\s*(?:days?|weeks?)\b", "1mo"),
    (r"\bweeks?\b|\b(?:5|7)\s*days?\b", "5d"),
]

# Yahoo's range= tokens are month/year-scale buckets (see _RANGE_INTERVAL) —
# there is no "3wk" token, so a request for an exact week-scale span has no
# bucket that actually matches it. Before this, `_norm_period` fell back to
# the nearest bucket silently (a numbered week count didn't even reach the
# right nearest bucket — the "weeks?" pattern above matches ANY mention of
# "week", so "three weeks" and "one week" both landed on the SAME "5d"
# token), and nothing about the result said "this is 5 days, not 3 weeks".
#
# MEASURED FAILURE (2026-08-23, user's debug export): asked to compare GOOGL/
# MU to "exactly three weeks ago", the model called get_stock_price with
# period="3mo" — its own reasoning trace shows it knew that was wrong
# ("the user wants exactly 3 weeks ago... let me try 3mo and see what I
# get") — then relabeled the resulting 3-MONTH-old price as "Three weeks ago
# (May 22, 2026)" in a message it sent to the user's mom. Two failures
# stacked: the model didn't reach for an exact span, and the tool had no way
# to give it one even if it had tried "3 weeks" — the v8 chart endpoint also
# accepts period1/period2 (unix seconds) instead of range=, which gives an
# EXACT day-precise lookback. _exact_days below detects a numbered day/week
# count and _one_history uses it to build that query instead of snapping to
# whichever bucket happens to be nearest.
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
              "twelve": 12}
_EXACT_DAYS_RE = re.compile(
    r"\b(\d+|" + "|".join(_NUM_WORDS) + r")\s*(day|week)s?\b")
# Above ~6 weeks the month/year buckets are already a close enough match
# (and cheaper — Yahoo's 1mo/3mo/etc. ranges are pre-aggregated), so exact
# mode is reserved for spans genuinely too fine-grained for any bucket.
_EXACT_DAYS_MAX = 45


def _exact_days(period: str) -> int | None:
    """A precise day count for `period`, or None if it doesn't name one.

    Only day/week phrasings trigger this — "3 months" still resolves through
    _norm_period to the "3mo" bucket unchanged, since that bucket already IS
    an exact match for "3 months ago". It's specifically weeks/days that have
    no matching bucket at all.
    """
    p = (period or "").strip().lower()
    compact = re.fullmatch(r"(\d+)(d|w|wk)", p)
    if compact and p != "1d":
        count = int(compact.group(1)) * (1 if compact.group(2) == "d" else 7)
        return count if 1 <= count <= _EXACT_DAYS_MAX else None
    if p in {"last week", "past week", "one week ago"}:
        return 7
    m = _EXACT_DAYS_RE.search(p)
    if not m:
        return None
    raw, unit = m.group(1), m.group(2)
    n = int(raw) if raw.isdigit() else _NUM_WORDS[raw]
    days = n * 7 if unit == "week" else n
    return days if 1 <= days <= _EXACT_DAYS_MAX else None


def _norm_period(period: str) -> str | None:
    """Yahoo range token for `period`, or None if it isn't a period at all.

    Accepts the tokens verbatim ("1mo") and the phrasings a model paraphrases
    them into ("last month", "past 6 months", "1 year"). Returning None rather
    than defaulting to a range matters: an unrecognised value must fall back to
    the live quote, not silently answer about some other span of time.

    Callers should try `_exact_days` FIRST — this still matches bare "weeks"/
    "days" (no number) as a rough bucket for phrasing that doesn't name a
    precise span at all, e.g. "a few weeks back".
    """
    p = (period or "").strip().lower().replace("_", " ")
    if not p:
        return None
    compact = p.replace(" ", "")
    if compact in _RANGE_INTERVAL:
        return compact
    if compact in ("1d", "today", "now", "current"):
        return None
    for pattern, token in _PERIOD_WORDS:
        if re.search(pattern, p):
            return token
    return None


def _series(payload: dict) -> tuple[list[int], list[float]] | str:
    """(timestamps, closes) with gaps dropped, or an error string.

    Yahoo pads `close` with nulls for sessions it has no data for; zipping
    without dropping them puts `None` into the min()/max() below.
    """
    try:
        result = payload["chart"]["result"][0]
        stamps = result.get("timestamp") or []
        closes = result["indicators"]["quote"][0].get("close") or []
    except Exception:  # noqa: BLE001
        return "(historical data missing or in an unexpected shape)"
    pairs = [(t, c) for t, c in zip(stamps, closes) if c is not None]
    if not pairs:
        return "(no trading data in that period)"
    return [t for t, _ in pairs], [c for _, c in pairs]


def _fmt_day(epoch: int, tz: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        return datetime.fromtimestamp(epoch, ZoneInfo(tz)).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%d")


def _sample(items: list, limit: int) -> list:
    """Evenly-spaced subset of `items`, always keeping the first and last.

    A month of daily closes is 22 lines and a 5-year monthly series is 60; both
    are more than the answer needs and both eat the tool-result budget. Even
    spacing (rather than head/tail truncation) keeps the shape of the move
    visible, which is the thing a "how did it do over X" question is asking.
    """
    if len(items) <= limit:
        return items
    step = (len(items) - 1) / (limit - 1)
    picked = {round(i * step) for i in range(limit)}
    picked.add(len(items) - 1)
    return [items[i] for i in sorted(picked)]


def _history_comparisons(stamps: list[int], closes: list[float], tz: str) -> str:
    """Compute comparisons before sampling can discard the baseline date."""
    from datetime import date, timedelta
    latest = date.fromisoformat(_fmt_day(stamps[-1], tz))
    lines = []
    for days in (1, 7, 30):
        target = latest - timedelta(days=days)
        candidates = [i for i, stamp in enumerate(stamps)
                      if date.fromisoformat(_fmt_day(stamp, tz)) <= target]
        if not candidates:
            continue
        index = candidates[-1]
        baseline = closes[index]
        change = closes[-1] - baseline
        percent = f"{change / baseline * 100:+.2f}%" if baseline else "undefined percent (zero baseline)"
        lines.append(
            f"  {days}-calendar-day comparison, latest available close {latest}: "
            f"{closes[-1]:.2f} vs {baseline:.2f} on {_fmt_day(stamps[index], tz)} "
            f"(last available close on/before {target}); {change:+.2f}, {percent}")
    return "\n".join(lines)


async def _one_history(query: str, period: str, *, exact_days: int | None = None) -> str:
    """`period` is a Yahoo range token ("3mo") for the header/label; when
    `exact_days` is set, the actual request uses period1/period2 timestamps
    for a day-precise lookback instead of that bucket — see _exact_days.
    `period` is still passed so callers can label an exact request as e.g.
    "21 days" without this function re-deriving it.
    """
    resolved = await _resolve_symbol(query)
    if isinstance(resolved, str):
        return f"{query}: {resolved}"
    symbol, name = resolved
    if exact_days is not None:
        end = int(time.time())
        start = end - exact_days * 86400
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/{}"
               f"?period1={start}&period2={end}&interval=1d")
    else:
        interval = _RANGE_INTERVAL[period]
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/{}"
               f"?range={period}&interval={interval}")
    r = await _yahoo_get(url.format(quote(symbol)))
    if isinstance(r, str):
        return f"{symbol}: {r}"
    if r.status_code == 404 and (fixed := await _chart_404_retry(query, resolved)):
        symbol, name = fixed
        r = await _yahoo_get(url.format(quote(symbol)))
        if isinstance(r, str):
            return f"{symbol}: {r}"
    if r.status_code >= 400:
        return f"{symbol}: (HTTP {r.status_code} fetching history)"
    try:
        payload = r.json()
        meta = payload["chart"]["result"][0]["meta"]
    except Exception:  # noqa: BLE001
        return f"{symbol}: (history data missing or in an unexpected shape)"
    got = _series(payload)
    if isinstance(got, str):
        return f"{symbol}: {got}"
    stamps, closes = got
    ccy = meta.get("currency", "USD")
    tz = meta.get("exchangeTimezoneName", "America/New_York")
    first, last = closes[0], closes[-1]
    delta = last - first
    pct = (delta / first * 100) if first else 0.0
    hi_i = max(range(len(closes)), key=lambda i: closes[i])
    lo_i = min(range(len(closes)), key=lambda i: closes[i])
    label = f"{symbol} ({name})" if name and name != symbol else symbol
    head = (f"{label} over {period} ({_fmt_day(stamps[0], tz)} to "
            f"{_fmt_day(stamps[-1], tz)}, {ccy}):\n"
            f"  start {first:.2f} -> end {last:.2f}  "
            f"({delta:+.2f}, {pct:+.2f}%)\n"
            f"  high {closes[hi_i]:.2f} on {_fmt_day(stamps[hi_i], tz)}, "
            f"low {closes[lo_i]:.2f} on {_fmt_day(stamps[lo_i], tz)}")
    comparisons = _history_comparisons(stamps, closes, tz)
    points = _sample(list(zip(stamps, closes)), 32)
    body = "\n".join(f"  {_fmt_day(t, tz)}  {c:.2f}" for t, c in points)
    return f"{head}\n{comparisons}\n  ---\n{body}"


@register(
    "get_stock_price",
    "Get the price of one or more stocks/ETFs — the current price, or the "
    "history over a past period. Pass the company name OR ticker symbol "
    "exactly as the user said it — do NOT add tickers they did not mention. "
    "Handles company-name-to-ticker resolution and formats a clean answer; "
    "prefer this over web_fetch for any stock/equity price question. "
    "WHENEVER the user asks about a PAST span of time — 'over the last month', "
    "'this year', 'how has it done since June', 'past week' — pass `period`. "
    "It returns the start/end prices, the change and percent change, the high "
    "and low with their dates, and a sampled series of closes. Do NOT say you "
    "can only get the live price: with `period` you can get the history too. "
    "For an EXACT day/week count ('exactly three weeks ago', '10 days back') "
    "pass it as a number, e.g. '3 weeks' or '10 days' — this resolves to a "
    "precise lookback, not the nearest month-scale bucket. The result always "
    "states the ACTUAL start date and span it used (in the header line and the "
    "date on the 'start' price) — if the user named a specific span, check "
    "that header against what you're about to say before you describe a price "
    "as being from a particular time ago; never relabel the span the result "
    "actually covers as the one the user asked for. "
    "For CRYPTO prices, use web_fetch's documented CoinGecko form instead — "
    "this tool is equities/ETFs only.",
    {"type": "object",
     "properties": {
         "symbols": {"type": "array", "items": {"type": "string"},
                     "description": "company names or ticker symbols, e.g. "
                                    "[\"NVIDIA\", \"AMD\"] or [\"VICR\", \"MU\"]"},
         "period": {"type": "string",
                    "description": "how far back to look. Omit for the current "
                                   "price. One of: 5d, 1mo, 3mo, 6mo, 1y, 2y, "
                                   "5y, 10y, max (plain phrasings like 'last "
                                   "month' or 'past 6 months' also work). For "
                                   "an exact span under ~6 weeks, name the "
                                   "number: '3 weeks', '10 days', 'exactly 21 "
                                   "days' — these get a day-precise lookback "
                                   "instead of snapping to 1mo/3mo/etc."},
     },
     "required": ["symbols"]},
    category="web_read",
)
async def get_stock_price(symbols: list[str], period: str = "") -> str:
    syms = list(dict.fromkeys(s.strip() for s in (symbols or []) if (s or "").strip()))
    if len(syms) > 10:
        return "(error: at most 10 stock symbols per request; no symbols were silently omitted.)"
    if not syms:
        return "(no symbols given)"
    # An unrecognised period falls back to the live quote rather than erroring:
    # the user still gets a real price, which is a better failure than a tool
    # error the model then has to explain.
    #
    # Exact day/week counts are tried FIRST: they're a strict subset of what
    # _norm_period would otherwise catch (only day/week phrasings match at
    # all — see _exact_days), and a numbered span deserves the precise
    # period1/period2 lookback over the nearest bucket whenever one is named.
    exact = _exact_days(period)
    if exact is not None:
        label = f"{exact // 7} week{'s' if exact != 7 else ''}" if exact % 7 == 0 else f"{exact} days"
        requested = syms
        blocks = list(await asyncio.gather(
            *(_one_history(s, label, exact_days=exact) for s in requested)))
    elif span := _norm_period(period):
        # Fewer symbols than the live path: each history block is ~15 lines, so
        # ten of them would blow the tool-result budget on its own.
        requested = syms
        blocks = list(await asyncio.gather(
            *(_one_history(s, span) for s in requested)))
    else:
        if period.strip().lower() not in {"", "1d", "today", "now", "current"}:
            return f"(error: unsupported stock period {period!r}; no comparison was performed.)"
        requested = syms
        blocks = list(await asyncio.gather(*(_one_quote(s) for s in requested)))

    failed = [symbol for symbol, block in zip(requested, blocks)
              if block.lstrip().startswith("(") or ": (" in block]
    rendered = ("\n\n" if exact is not None or bool(_norm_period(period)) else "\n").join(blocks)
    if failed:
        return ("(error: incomplete stock lookup; no complete report is available. "
                f"Failed symbols: {', '.join(failed)}.)\n{rendered}")
    return rendered


# --- Weather -----------------------------------------------------------------
#
# A dedicated tool for the same reason get_stock_price exists: the model was
# building the URL itself and got it wrong in three separate ways in one
# session.
#
# MEASURED FAILURES (2026-08-18 22:36 log), all from hand-built wttr.in URLs:
#
#   1. NO LOCATION -> INVENTED ONE. "can you check the weather for tomorrow"
#      became `wttr.in/Los_Angeles`. The user is in Dublin, California. Nothing
#      in the prompt said Los Angeles; the model simply supplied a plausible
#      city and reported the result as fact.
#   2. AMBIGUOUS CITY -> WRONG CONTINENT. Told "in dublin ca", it fetched
#      `wttr.in/Dublin`, which wttr.in resolves to Dublin, IRELAND (+55°F,
#      light rain) — and reported that as the user's local weather. The "ca"
#      was dropped entirely.
#   3. CURRENT CONDITIONS REPORTED AS A FORECAST. `?format=3` returns only
#      right now, with no forecast in it at all, yet the answer was phrased
#      "Tomorrow in Los Angeles: +82°F". There was never any tomorrow data.
#
# All three are the model doing a job that is deterministic. This tool takes a
# place and returns current conditions plus a real 3-day forecast, and it
# ECHOES THE AREA wttr.in actually resolved to — so a Dublin/Dublin mixup is
# visible in the result rather than silently answered.
_WEATHER_DAYS = ("today", "tomorrow", "the day after tomorrow")


@register(
    "get_weather",
    "Current weather and a 3-day forecast for a place. Use this for ANY "
    "weather question — today's, tomorrow's, this weekend's — NOT web_fetch. "
    "`location` must name the place the user actually means, and US cities "
    "need their state ('Dublin,CA' — plain 'Dublin' is Dublin, Ireland). "
    "If you do not know where the user is, ASK them; never guess a city, and "
    "never answer a weather question from memory. The result names the area it "
    "resolved to — if that is not the place the user meant, say so rather than "
    "reporting its numbers.",
    {"type": "object",
     "properties": {
         "location": {"type": "string",
                      "description": "city, 'City,ST' for the US, postcode, or "
                                     "airport code — e.g. 'Dublin,CA', "
                                     "'London', '94568'"},
         "period": {"type": "string", "description": "Exact requested forecast date/range; unavailable coverage is reported, never expanded silently."},
     },
     "required": ["location"]},
    category="web_read",
)
async def get_weather(location: str, period: str = "") -> str:
    from service.workflows.compiler import is_temporal_location
    place = (location or "").strip()
    if not place or is_temporal_location(place):
        return ("(no location given — ask the user which city they want the "
                "weather for; do not guess one)")
    r = await _yahoo_get(f"https://wttr.in/{quote(place)}?format=j1")
    if isinstance(r, str):
        return r.replace("Yahoo Finance", "the weather service")
    if r.status_code >= 400:
        return (f"(weather lookup for {place!r} failed: HTTP {r.status_code}. "
                "Check the spelling, or add the state/country — "
                "'Dublin,CA' rather than 'Dublin')")
    try:
        d = r.json()
        cur = d["current_condition"][0]
        area = d["nearest_area"][0]
    except Exception:  # noqa: BLE001
        return f"(weather data for {place!r} came back in an unexpected shape)"

    def _first(node, key: str) -> str:
        try:
            return node[key][0]["value"]
        except Exception:  # noqa: BLE001
            return ""

    resolved = ", ".join(x for x in (_first(area, "areaName"),
                                     _first(area, "region"),
                                     _first(area, "country")) if x)
    out = [f"Weather for {resolved or place} (asked for: {place})",
           f"  now: {cur.get('temp_F')}°F ({cur.get('temp_C')}°C), "
           f"{_first(cur, 'weatherDesc').strip()}, "
           f"feels like {cur.get('FeelsLikeF')}°F, "
           f"humidity {cur.get('humidity')}%, "
           f"wind {cur.get('windspeedMiles')} mph"]
    forecast = d.get("weather") or []
    if period:
        from datetime import datetime, timedelta
        from service.tools.timeranges import resolve_span, BadPeriod
        try:
            start, end, _ = resolve_span(period)
        except BadPeriod as exc:
            return f"(error: unsupported weather range: {exc})"
        first, last = datetime.fromtimestamp(start), datetime.fromtimestamp(end)
        required = {(first + timedelta(days=i)).strftime("%Y-%m-%d")
                    for i in range((last.date() - first.date()).days)}
        available = {day.get("date") for day in forecast}
        if not required or not required <= available:
            return f"(error: weather forecast does not cover {period!r}; available dates: {', '.join(sorted(str(x) for x in available))}.)"
        forecast = [day for day in forecast if day.get("date") in required]
        out = [out[0] + f" — {period}; forecast only"]
    for i, day in enumerate(forecast):
        label = "forecast" if period else (_WEATHER_DAYS[i] if i < len(_WEATHER_DAYS) else day.get("date", ""))
        # hourly[4] is the midday slot in wttr.in's 3-hourly series — the one
        # that actually describes "what that day is like", rather than the
        # midnight reading hourly[0] would give.
        hours = day.get("hourly") or []
        mid = hours[4] if len(hours) > 4 else (hours[0] if hours else {})
        out.append(f"  {label} ({day.get('date')}): "
                   f"high {day.get('maxtempF')}°F / low {day.get('mintempF')}°F, "
                   f"{_first(mid, 'weatherDesc').strip()}, "
                   f"{mid.get('chanceofrain', '0')}% chance of rain")
    return "\n".join(out)


@register(
    "web_search",
    "Search the public web and return structured results with real titles, URLs, "
    "domains, and snippets. Use this when the user asks to search, research, "
    "compare sources, find recent information, or asks a question whose answer "
    "is not at a known URL. Call web_fetch afterward on the most relevant result "
    "when the snippet alone is insufficient. Never invent a URL or result.",
    {"type": "object",
     "properties": {
         "query": {"type": "string", "description": "specific web search query"},
         "limit": {"type": "integer", "description": "number of results, 1-10"},
     },
     "required": ["query"]},
    category="web_read",
    aliases=["search the web for this", "look this up online", "find recent sources",
             "research this topic", "what are reliable sources about this"],
)
async def web_search(query: str, limit: int = 6) -> str:
    try:
        if _current_news_intent(query):
            return await current_news(query, limit=limit)
        hits = await search_web(query, limit=max(1, min(int(limit), 10)))
        return render_search_results(hits)
    except Exception as exc:  # noqa: BLE001
        return f"(web search failed: {type(exc).__name__}: {exc})"


_NEWS_RANGE_MARKER = r"(?:last|past|previous|prior|preceding)"
_NEWS_CALENDAR_PERIOD = r"(?:weeks?|weekends?|fortnights?|months?|quarters?|years?)"
_NEWS_RANGE_UNIT = (rf"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|wks?|"
                    rf"{_NEWS_CALENDAR_PERIOD}|s|m|h|d|w)")
_NEWS_NUMBER_WORD = (r"(?:an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                     r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
                     r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|"
                     r"dozen|half|quarter|couple|few|several)")
_NEWS_FRACTION_GLYPH = r"[½¼¾⅓⅔⅛⅜⅝⅞]"
_NEWS_NUMERIC_FRACTION = rf"(?:[1-9]\d*\s*[/⁄]\s*[1-9]\d*|{_NEWS_FRACTION_GLYPH})"
_NEWS_MIXED_NUMBER = rf"\d+(?:[\s-]+{_NEWS_NUMERIC_FRACTION}|{_NEWS_FRACTION_GLYPH})"
# Recognize the longest numeric form first so a mixed amount cannot disappear
# or leave an initial one-day interval behind. Non-decimal amounts remain
# unsupported by the day-only feed; this grammar does not evaluate fractions.
_NEWS_QUANTITY = (rf"(?:{_NEWS_MIXED_NUMBER}|{_NEWS_NUMERIC_FRACTION}|\d+(?:\.\d+)?|\.\d+|"
                  rf"{_NEWS_NUMBER_WORD}(?:[\s-]+(?:{_NEWS_NUMBER_WORD}|and|of)){{0,5}})")
_NEWS_TIME_FRAME = r"(?:on|in|from|for|dated|as\s+of|before|after|during|over|within|since|between)"
_NEWS_DATE_FRAME = rf"\b{_NEWS_TIME_FRAME}\s+(?:the\s+)?"
_NEWS_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
               r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?")
_NEWS_DAY = r"(?:0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?"
_NEWS_CALENDAR_DATE = (rf"(?:(?:{_NEWS_MONTH}\s+(?:the\s+)?{_NEWS_DAY}|"
                       rf"{_NEWS_DAY}(?:\s+of)?\s+{_NEWS_MONTH})(?:(?:,\s*|\s+)\d{{4}})?|"
                       rf"{_NEWS_MONTH}\s+\d{{4}}|"
                       r"\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?|\d{4}-\d{1,2}-\d{1,2})")
_NEWS_WEEKDAY = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_NEWS_SHORT_WEEKDAY = r"(?:Mon|Tue(?:s)?|Wed|Thu(?:rs)?|Fri|Sat|Sun)\.?"
_NEWS_TIME_OF_DAY = r"(?:\s+(?:morning|afternoon|evening|night))?"
# A temporal phrase can end before a separate topic/region/time clause. A noun
# continuation is part of a source/title instead: Monday Night Football cannot
# match Monday or Monday Night, but Monday night about markets can match.
_NEWS_CLAUSE_END = (r"(?![\w’'-]|\.\w)(?=\s*(?:$|[?!,;:+()]|\.(?!\w))|\s+(?:about|regarding|concerning|"
                    r"covering|focused|with|in|on|for|from|dated|as|at|before|after|during|"
                    r"since|between|through|until|to|and|or|plus|today|tonight|latest|current|"
                    r"breaking|right|please|thanks|worldwide|globally|internationally)\b)")
# Consume continuations as part of the interval itself. The same whole-period
# grammar serves quoted values and duration classification; a unitless fraction
# cannot be dropped from either path or mistaken for a repeated one-day request.
_NEWS_DURATION = rf"{_NEWS_QUANTITY}[\s-]*{_NEWS_RANGE_UNIT}"
_NEWS_FRACTION = (rf"(?:{_NEWS_MIXED_NUMBER}|{_NEWS_NUMERIC_FRACTION}|"
                  r"(?:(?:a|one|another)[\s-]+)?half|"
                  r"(?:(?:a|one|two|three)[\s-]+)?(?:quarters?|thirds?)|"
                  r"0?\.\d*[1-9]\d*)")
_NEWS_DURATION_JOIN = r"[\s-]*(?:,\s*(?:and\s+)?|(?:and|plus)[\s-]+|\+\s*)"
_NEWS_INTERVAL = re.compile(
    rf"\b(?:the\s+)?(?P<marker>{_NEWS_RANGE_MARKER}|this|next)\s+"
    rf"(?:(?P<quantity>{_NEWS_QUANTITY})[\s-]*)?(?P<unit>{_NEWS_RANGE_UNIT})"
    rf"(?:[\s-]+periods?)?"
    rf"(?P<continuation>(?:{_NEWS_DURATION_JOIN}(?:{_NEWS_DURATION}|"
    rf"{_NEWS_FRACTION}(?:[\s-]*{_NEWS_RANGE_UNIT})?))*)"
    rf"{_NEWS_CLAUSE_END}", re.I)
_NEWS_RELATIVE_POINT = re.compile(
    rf"\b(?:{_NEWS_QUANTITY}[\s-]*)?{_NEWS_RANGE_UNIT}"
    rf"[\s-]+(?:ago|earlier|back|prior|previously){_NEWS_CLAUSE_END}", re.I)
_NEWS_MODIFIED_WEEKDAY = re.compile(
    rf"\b(?:this|next|{_NEWS_RANGE_MARKER})\s+(?:{_NEWS_WEEKDAY}|{_NEWS_SHORT_WEEKDAY})"
    rf"{_NEWS_TIME_OF_DAY}{_NEWS_CLAUSE_END}", re.I)
_NEWS_RELATIVE_DAY = re.compile(rf"\b(?:yesterday|tomorrow){_NEWS_CLAUSE_END}", re.I)
_NEWS_FRAMED_DATE = re.compile(
    rf"(?:{_NEWS_DATE_FRAME}|\b(?:news|headlines?)\s+(?:the\s+)?)"
    rf"{_NEWS_CALENDAR_DATE}{_NEWS_CLAUSE_END}", re.I)
_NEWS_FRAMED_YEAR = re.compile(rf"{_NEWS_DATE_FRAME}\d{{4}}{_NEWS_CLAUSE_END}", re.I)
_NEWS_FRAMED_WEEKDAY = re.compile(
    rf"(?:{_NEWS_DATE_FRAME}|\b(?:news|headlines?)\s+){_NEWS_WEEKDAY}"
    rf"{_NEWS_TIME_OF_DAY}{_NEWS_CLAUSE_END}", re.I)
# No optional 'the' on short names: from The Sun remains a publisher request.
_NEWS_FRAMED_SHORT_WEEKDAY = re.compile(
    rf"\b(?:{_NEWS_TIME_FRAME}|news|headlines?)\s+"
    rf"{_NEWS_SHORT_WEEKDAY}{_NEWS_TIME_OF_DAY}{_NEWS_CLAUSE_END}", re.I)
_NEWS_OPEN_RANGE = re.compile(
    rf"\b(?:since|between)\s+(?:the\s+)?(?:{_NEWS_CALENDAR_DATE}|\d{{4}}|"
    rf"{_NEWS_MONTH}|{_NEWS_WEEKDAY}|yesterday|today){_NEWS_CLAUSE_END}", re.I)
_NEWS_QUOTES = r'''"[^"]*"|“[^”]*”|(?<!\w)'[^']*'(?!\w)|‘[^’]*’'''
# Search operators may occur at the start of a parenthesized Boolean group.
_NEWS_TOKEN_START = r"(?<![^\s(])"
_NEWS_FILTER_KEY = (r"site|filetype|ext|intitle|allintitle|inurl|allinurl|"
                    r"intext|allintext|source|related|cache|lang|language|before|after|when")
_NEWS_FILTER_ATOM = re.compile(
    rf"(?P<key>{_NEWS_FILTER_KEY}):(?:{_NEWS_QUOTES}|[^\s()]+)", re.I)
_NEWS_FILTER_UNARY = re.compile(r"(?:NOT\b\s*|-\s*)", re.I)
_NEWS_FILTER_JOIN = re.compile(r"(?:AND|OR)\b\s*", re.I)
_NEWS_QUERY_TOKEN = re.compile(
    rf"(?P<filter>{_NEWS_TOKEN_START}(?:NOT\b|[-(]|(?:{_NEWS_FILTER_KEY}):))|"
    rf"(?P<url>https?://\S+)|(?P<quoted>{_NEWS_QUOTES})", re.I)
_NEWS_EXCLUDED = re.compile(rf"-(?:{_NEWS_QUOTES}|[^\s)]+)")
_NEWS_EMPTY_QUERY_GROUP = re.compile(r"\(\s*(?:(?:AND|OR|NOT)\b\s*)*\)", re.I)


def _news_filter_operand(query: str, start: int, *, negated: bool = False,
                         depth: int = 0) -> tuple[int, bool] | None:
    """Read only known filter operands; commit polarity after a complete parse."""
    if depth >= 32:
        return None
    position = start
    while unary := _NEWS_FILTER_UNARY.match(query, position):
        negated = not negated
        position = unary.end()
    if atom := _NEWS_FILTER_ATOM.match(query, position):
        return atom.end(), not negated and atom.group("key").lower() in {"before", "after", "when"}
    if position >= len(query) or query[position] != "(":
        return None
    position += 1
    positive_date = False
    while True:
        while position < len(query) and query[position].isspace():
            position += 1
        operand = _news_filter_operand(query, position, negated=negated, depth=depth + 1)
        if operand is None:
            return None  # Mixed prose, malformed syntax, and quoted titles stay prose.
        end, has_date = operand
        positive_date |= has_date
        position = end
        while position < len(query) and query[position].isspace():
            position += 1
        if position < len(query) and query[position] == ")":
            return position + 1, positive_date
        if join := _NEWS_FILTER_JOIN.match(query, position):
            position = join.end()
        elif position == end:
            return None


def _news_query_text(query: str) -> tuple[str, bool]:
    """Separate search syntax and quoted names from prose, without rewriting it."""
    explicit_operator = False

    def token(match: re.Match) -> str:
        if not match.group("quoted"):
            return " "
        value = match.group()[1:-1].strip()
        framed = re.search(_NEWS_DATE_FRAME + r"$", query[:match.start()], re.I)
        if not framed:
            return " "
        from_frame = bool(re.match(r"from\b", framed.group(), re.I))
        interval = _NEWS_INTERVAL.fullmatch(value)
        weekday = re.fullmatch(
            rf"(?:the\s+)?(?:{_NEWS_WEEKDAY}|{_NEWS_SHORT_WEEKDAY}){_NEWS_TIME_OF_DAY}", value, re.I)
        # A bare quoted weekday after 'from' remains an ambiguous source name.
        if from_frame and re.fullmatch(
                rf"{_NEWS_WEEKDAY}|{_NEWS_SHORT_WEEKDAY}", value, re.I):
            weekday = None
        # 'from' can introduce a source or time. Only a quoted, word-only,
        # title-cased interval gets the source interpretation. Stronger frames,
        # numeric intervals, calendar dates and relative points stay temporal.
        words = [word for word in re.findall(r"[A-Za-z]+", value)
                 if word.lower() not in {"the", "of", "and", "a", "an"}]
        if (interval and not interval.group("continuation") and from_frame
                and not re.search(r"\d", value) and words and all(word.istitle() for word in words)):
            return " "
        temporal = (interval or weekday or _NEWS_RELATIVE_POINT.fullmatch(value)
                    or _NEWS_MODIFIED_WEEKDAY.fullmatch(value) or _NEWS_RELATIVE_DAY.fullmatch(value)
                    or _NEWS_OPEN_RANGE.fullmatch(value)
                    or re.fullmatch(rf"(?:the\s+)?{_NEWS_CALENDAR_DATE}|\d{{4}}|today|tonight", value, re.I))
        return value if temporal else " "

    parts = []
    position = 0
    while match := _NEWS_QUERY_TOKEN.search(query, position):
        parts.append(query[position:match.start()])
        position = match.end()
        if match.group("filter"):
            operand = _news_filter_operand(query, match.start())
            if operand is not None:
                position, has_date = operand
                explicit_operator |= has_date
                parts.append(" ")
            elif excluded := _NEWS_EXCLUDED.match(query, match.start()):
                position = excluded.end()
                parts.append(" ")
            else:
                parts.append(match.group())
        else:
            # Quote frames use original offsets, including after removed filters.
            parts.append(token(match))
    parts.append(query[position:])
    text = "".join(parts)
    # Removed search payloads can leave ( OR ) or nested empty groups. Prune
    # only groups with no prose left; actual topic/date groups remain intact.
    while True:
        text, removed = _NEWS_EMPTY_QUERY_GROUP.subn(" ", text)
        if not removed:
            break
    return " ".join(text.split()), explicit_operator


def _news_interval_is_day(match: re.Match) -> bool:
    """Normalize fixed units; unsupported/compound intervals retain general search."""
    if match.group("marker").lower() in {"this", "next"} or match.group("continuation"):
        return False
    quantity = re.sub(r"[\s-]+", " ", match.group("quantity") or "1").lower()
    try:
        number = float(quantity)
    except ValueError:
        number = {"a": 1, "an": 1, "one": 1, "twenty four": 24}.get(quantity)
    unit = match.group("unit").lower()
    seconds = next((scale for aliases, scale in (
        ({"s", "sec", "secs", "second", "seconds"}, 1),
        ({"m", "min", "mins", "minute", "minutes"}, 60),
        ({"h", "hr", "hrs", "hour", "hours"}, 3600),
        ({"d", "day", "days"}, 86400),
        ({"w", "wk", "wks", "week", "weeks"}, 604800),
    ) if unit in aliases), None)
    return number is not None and seconds is not None and number * seconds == 86400


def _news_periods(text: str) -> list[bool]:
    """Extract supported time clauses: True is a rolling day, False any other time."""
    periods = [_news_interval_is_day(match) for match in _NEWS_INTERVAL.finditer(text)]
    for pattern in (_NEWS_RELATIVE_POINT, _NEWS_MODIFIED_WEEKDAY, _NEWS_RELATIVE_DAY,
                    _NEWS_FRAMED_DATE, _NEWS_FRAMED_YEAR, _NEWS_FRAMED_WEEKDAY,
                    _NEWS_FRAMED_SHORT_WEEKDAY, _NEWS_OPEN_RANGE):
        if pattern.search(text):
            periods.append(False)
    return periods


def _current_news_intent(query: str) -> bool:
    """Only an explicit current-news request gets a strict last-24-hours feed."""
    text, explicit_operator = _news_query_text(query)
    news_format = re.search(r"\b(?:news|headlines?|top stories)\b", text, re.I)
    if explicit_operator or not news_format:
        return False
    periods = _news_periods(text)
    if False in periods:
        return False
    # News can be the subject or a product name, rather than the requested format.
    # Topic/source clauses do not change the requested format, but their explicit
    # temporal clauses above still count (about the World Cup from Monday).
    topic = re.search(r"\b(?:about|on|regarding|concerning|covering|focused on|with coverage of|from)\b",
                      text, re.I)
    # A topic introducer can narrow an established news request, not turn a
    # guide or writing request into news by erasing the words after 'on'.
    subject = text[:topic.start()] if topic and news_format.start() < topic.start() else text
    if re.search(r"\b(?:api|docs?|documentation|guides?|tutorials?|history|historical|"
                 r"archives?|clone|how to|writing|write)\b", subject, re.I):
        return False
    return bool(periods or re.search(r"\b(?:today|tonight|latest|current|breaking|right now)\b", text, re.I)
                or re.fullmatch(r"\s*(?:(?:world|global|international)\s+)?"
                                r"(?:news|headlines?|top stories)[?!. ]*", text, re.I))


def dated_news_digest(xml: str, *, now: float, limit: int = 6) -> str:
    """Publication time is required. Search snippets are not verified events."""
    from email.utils import parsedate_to_datetime
    from xml.etree import ElementTree
    root = ElementTree.fromstring(xml)
    rows = []
    for item in root.findall("./channel/item"):
        title, link = item.findtext("title", "").strip(), item.findtext("link", "").strip()
        published = item.findtext("pubDate", "")
        try:
            dt = parsedate_to_datetime(published)
            if dt.tzinfo is None or not 0 <= now - dt.timestamp() <= 86400:
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        if not title or urlparse(link).scheme not in {"http", "https"}:
            continue
        source = item.findtext("source", "Publisher not provided")
        rows.append((dt.timestamp(), f"- {title} — {source}; published {dt.isoformat()}\n  {link}"))
    rows.sort(reverse=True)
    if not rows:
        return "(error: no dated news results from the last 24 hours; no current report is available.)"
    return ("News headlines published in the last 24 hours (publisher claims; "
            "article contents have not been independently verified):\n"
            + "\n".join(row for _, row in rows[:max(1, min(limit, 10))]))


async def current_news(query: str, limit: int = 6) -> str:
    # Preserve the user's topic, geography, exclusions and quoted entities.
    # Replacing a stock-market query with generic headlines discarded them.
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        response = await client.get("https://news.google.com/rss/search", params={
            "q": query + " when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        response.raise_for_status()
    if len(response.content) > 2_000_000:
        return "(error: news feed too large; no current report is available.)"
    return dated_news_digest(response.text, now=time.time(), limit=limit)


@register(
    "web_fetch",
    "Fetch a URL over HTTP(S) and return its text (GET only, no auth/cookies) — "
    "for live external data with no dedicated tool: crypto prices, sports "
    "scores, news, any public page or API. TWO THINGS HAVE THEIR OWN TOOL AND "
    "DO NOT BELONG HERE: stock/equity prices (`get_stock_price`) and weather "
    "(`get_weather`). Both exist because hand-building the URL for them was "
    "measured to get the wrong ticker and the wrong city. Known no-key "
    "endpoints, use these exact forms:\n"
    "  crypto   https://api.coingecko.com/api/v3/simple/price?ids=<coin>&vs_currencies=usd\n"
    "  news     https://news.google.com/rss/search?q=<topic>&hl=en-US&gl=US&ceid=US:en "
    "(q is required; use q=top stories for a generic ask)\n"
    "  facts    https://en.wikipedia.org/api/rest_v1/page/summary/<Title_With_Underscores> "
    "(read .extract); if the subject has no obvious title, "
    "https://html.duckduckgo.com/html/?q=<query>\n"
    "HTML is stripped to plain text; JSON/XML/text come back as-is, truncated if "
    "long. If a fetch fails, the error tells you what to try instead — follow it "
    "rather than guessing another URL.",
    {"type": "object",
     "properties": {
         "url": {"type": "string", "description": "the http(s) URL to fetch"},
     },
     "required": ["url"]},
    category="web_read",
)
async def web_fetch(url: str) -> str:
    try:
        page = await research_fetch_page(url)
    except Exception as exc:  # noqa: BLE001
        return f"(fetch failed: {type(exc).__name__}: {exc})"
    header = f"Title: {page.title}\nURL: {page.canonical_url}"
    if page.published_at:
        header += f"\nPublished: {page.published_at}"
    body = page.text
    if len(body) > 10500:
        body = body[:10500] + f"\n…[truncated {len(page.text) - 10500} characters]"
    return f"{header}\n\n{body}"
