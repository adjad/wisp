"""Strict-host fetch mode: redirects stay on the authorised host, no archive fallback.

Also the thin-page floor: research still drops a page with almost no text,
while web_fetch returns a short API answer as it is.

Fake transport only; no DNS or network. The mode is off by default.
"""
import asyncio

import httpx
import pytest

from service.research import web

REAL_CLIENT = httpx.AsyncClient
PAGE = "<html><head><title>T</title></head><body>" + ("Readable text. " * 30) + "</body></html>"


@pytest.fixture
def net(monkeypatch):
    seen = []

    async def validate(raw):
        return web.canonicalize_url(raw)

    def install(handler):
        def handle(request):
            seen.append(str(request.url))
            return handler(request)
        monkeypatch.setattr(web.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(
            transport=httpx.MockTransport(handle), **kw))

    monkeypatch.setattr(web, "validate_public_url", validate)
    return seen, install


def redirect_handler(request):
    if request.url.host == "example.com":
        return httpx.Response(302, headers={"location": "https://other.example.org/landing"})
    return httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE)


def test_default_still_follows_cross_host_redirects(net):
    seen, install = net
    install(redirect_handler)
    page = asyncio.run(web.fetch_page("https://example.com/a"))
    assert "other.example.org" in page.url and any("other.example.org" in u for u in seen)


def test_strict_refuses_cross_host_redirect_and_skips_wayback(net):
    seen, install = net
    install(redirect_handler)
    with web.strict_host_fetch():
        with pytest.raises(web.WebError, match="different host"):
            asyncio.run(web.fetch_page("https://example.com/a"))
    assert not any("other.example.org" in u for u in seen)
    assert not any("archive.org" in u for u in seen)


def test_strict_allows_same_host_redirect(net):
    seen, install = net

    def handler(request):
        if request.url.path == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE)
    install(handler)
    with web.strict_host_fetch():
        page = asyncio.run(web.fetch_page("https://example.com/a"))
    assert page.url.endswith("/b")


def test_default_falls_back_to_wayback_but_strict_does_not(net):
    seen, install = net

    def handler(request):
        if request.url.host == "archive.org":
            return httpx.Response(200, json={"archived_snapshots": {"closest": {
                "available": True, "url": "https://web.archive.org/web/1/https://example.com/a"}}})
        if request.url.host == "web.archive.org":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE)
        return httpx.Response(500)
    install(handler)
    assert asyncio.run(web.fetch_page("https://example.com/a")).via_archive
    seen.clear()
    with web.strict_host_fetch():
        with pytest.raises(web.WebError):
            asyncio.run(web.fetch_page("https://example.com/a"))
    assert not any("archive.org" in u for u in seen)


def test_strict_mode_resets_after_context(net):
    with web.strict_host_fetch():
        assert web._STRICT_HOST.get() is True
    assert web._STRICT_HOST.get() is False


# --- thin-page floor: research keeps it, web_fetch takes short answers ---------
PRICE_URL = "https://api.example.com/v3/simple/price?ids=examplecoin&vs_currencies=usd"
PRICE_JSON = '{"examplecoin":{"usd":64123}}'


def short_handler(body, ctype="application/json"):
    def handler(request):
        if request.url.host == "archive.org":
            return httpx.Response(200, json={"archived_snapshots": {}})
        return httpx.Response(200, headers={"content-type": ctype}, text=body)
    return handler


def run_web_fetch(url):
    from service.tools import web_tools
    return asyncio.run(web_tools.web_fetch(url))


def test_web_fetch_returns_a_short_json_answer(net):
    from service.tools.untrusted import WEB_PAGE, begin_marker, end_marker
    seen, install = net
    install(short_handler(PRICE_JSON))
    assert len(PRICE_JSON) < 120
    out = run_web_fetch(PRICE_URL)
    assert "fetch failed" not in out
    assert out.startswith("Title: api.example.com\nURL: https://api.example.com/v3/simple/price")
    assert f"{begin_marker(WEB_PAGE)}\n{PRICE_JSON}\n{end_marker(WEB_PAGE)}" in out
    assert not any("archive.org" in u for u in seen)


def test_web_fetch_returns_a_one_line_text_answer(net):
    seen, install = net
    install(short_handler("Paris: +14 C\n", ctype="text/plain"))
    out = run_web_fetch("https://weather.example.com/Paris?format=3")
    assert "fetch failed" not in out and "Paris: +14 C" in out


def test_web_fetch_empty_body_is_an_error_and_is_not_sent_to_the_archive(net):
    seen, install = net
    install(short_handler("   \n"))
    out = run_web_fetch(PRICE_URL)
    assert out.startswith("(fetch failed: WebError: page contained too little readable text)")
    assert "<<<" not in out
    assert not any("archive.org" in u for u in seen)


def test_research_fetch_still_drops_thin_pages(net):
    seen, install = net
    install(short_handler(PRICE_JSON))
    with pytest.raises(web.WebError, match="too little readable text"):
        asyncio.run(web.fetch_page(PRICE_URL))
    # Research still asks the archive for a fuller copy of a thin page.
    assert any("archive.org" in u for u in seen)


def test_short_text_floor_resets_after_context(net):
    with web.short_text_fetch():
        assert web._MIN_TEXT_CHARS.get() == 1
    assert web._MIN_TEXT_CHARS.get() == 120
    with web.short_text_fetch(0):
        assert web._MIN_TEXT_CHARS.get() == 1  # an empty body is never accepted


def test_web_fetch_failure_says_what_to_try_instead(net):
    seen, install = net

    def handler(request):
        if request.url.host == "archive.org":
            return httpx.Response(200, json={"archived_snapshots": {}})
        return httpx.Response(403 if request.url.host == "blocked.example.com" else 404)
    install(handler)
    blocked = run_web_fetch("https://blocked.example.com/a")
    assert blocked.startswith("(fetch failed: WebError: HTTP 403) This site blocks non-browser fetches.")
    missing = run_web_fetch("https://en.wikipedia.org/api/rest_v1/page/summary/No_Such_Title_Xyz")
    assert missing.startswith("(fetch failed: WebError: HTTP 404) That title doesn't exist. Use web_search")
    other = run_web_fetch("https://example.com/gone")
    assert other.startswith("(fetch failed: WebError: HTTP 404) Try ONE documented alternate endpoint")
    quote = run_web_fetch("https://www.marketwatch.com/investing/stock/example")
    assert "use get_stock_price" in quote


def test_web_fetch_refusals_carry_no_retry_hint(net, monkeypatch):
    seen, install = net
    install(redirect_handler)

    async def refuse(raw):
        raise web.WebError("local and private-network URLs are blocked")
    with web.strict_host_fetch():
        out = run_web_fetch("https://example.com/a")
    assert out.startswith("(fetch failed: WebError: redirect to a different host") and out.endswith(")")
    monkeypatch.setattr(web, "validate_public_url", refuse)
    out = run_web_fetch("http://127.0.0.1/admin")
    assert out == "(fetch failed: WebError: local and private-network URLs are blocked)"
    assert not any("archive.org" in u for u in seen)
