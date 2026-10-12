"""Strict-host fetch mode: redirects stay on the authorised host, no archive fallback.

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
