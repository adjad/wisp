"""The article fetcher (service/study/fetch.py): the SSRF guard (https on 443, public
addresses only, every IP-literal spelling refused, every redirect hop checked again), no
cookies or Referer, the size and type caps, and readable text.

The web is an httpx.MockTransport and DNS a fake table: no network. Pages are made up."""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from service.study import fetch

ARTICLE = ("<html><head><title>Rivers and cities</title><style>.x{}</style>"
           "<script>alert('no')</script></head><body><nav>Home | World</nav>"
           "<article><h1>Rivers shaped cities</h1><p>Most old cities grew on rivers.</p>"
           "<p>Trade &amp; water mattered.</p><aside>Subscribe now</aside></article>"
           "<footer>Copyright</footer></body></html>")


class Web:
    def __init__(self, routes, dns=None):
        self.routes = routes
        self.dns = dns or {"news.example.org": ["93.184.216.34"], "cdn.example.net": ["93.184.216.35"]}
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        key = (request.headers["host"], request.url.path)
        item = self.routes.get(key)
        if item is None:
            return httpx.Response(404)
        return item() if callable(item) else item


@pytest.fixture
def web(monkeypatch):
    holder = {}

    def install(routes, dns=None):
        server = Web(routes, dns)
        monkeypatch.setattr(fetch, "_transport", httpx.MockTransport(server))

        async def resolver(host):
            if host not in server.dns:
                raise OSError("no such host")
            return list(server.dns[host])
        monkeypatch.setattr(fetch, "resolver", resolver)
        holder["server"] = server
        return server
    return install


def html(body, **headers):
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"} | headers, text=body)


def get(url, **kw):
    return asyncio.run(fetch.fetch_article(url, **kw))


def code(url):
    with pytest.raises(fetch.FetchError) as err:
        get(url)
    return err.value.code


# --- readable text ------------------------------------------------------------------

def test_an_article_reads_as_title_headings_and_paragraphs(web):
    server = web({("news.example.org", "/rivers"): html(ARTICLE)})
    got = get("https://news.example.org/rivers#top")
    assert got["title"] == "Rivers and cities"
    assert got["text"] == "# Rivers shaped cities\n\nMost old cities grew on rivers.\n\nTrade & water mattered."
    for dropped in ("alert", "Home | World", "Subscribe", "Copyright", ".x{}"):
        assert dropped not in got["text"]
    (request,) = server.requests
    # The connection goes to the checked address, with the real name as Host and TLS SNI.
    assert request.url.host == "93.184.216.34" and request.url.scheme == "https"
    assert request.headers["host"] == "news.example.org"
    assert request.extensions.get("sni_hostname") == "news.example.org"
    assert "cookie" not in request.headers and "referer" not in request.headers
    assert "authorization" not in request.headers and "Wisp" not in request.headers["user-agent"]


def test_plain_text_is_kept_and_a_page_without_blocks_falls_back():
    assert fetch.readable("<html><body><div>Just words here</div></body></html>")[1] == "Just words here"


def test_plain_text_pages(web):
    web({("news.example.org", "/a.txt"): httpx.Response(200, headers={"content-type": "text/plain"},
                                                        text="  Line one.\nLine two.  ")})
    assert get("https://news.example.org/a.txt") == {"url": "https://news.example.org/a.txt", "title": "",
                                                      "text": "Line one.\nLine two."}


def test_an_empty_page_not_text_and_errors(web):
    web({("news.example.org", "/empty"): html("<html><script>x()</script></html>"),
         ("news.example.org", "/pic"): httpx.Response(200, headers={"content-type": "image/png"}, content=b"x"),
         ("news.example.org", "/gone"): httpx.Response(410)})
    assert code("https://news.example.org/empty") == "empty"
    assert code("https://news.example.org/pic") == "not_text"
    assert code("https://news.example.org/gone") == "http_error"
    assert code("https://unknown.example.org/") == "unreachable"


def test_the_size_cap(web, monkeypatch):
    monkeypatch.setattr(fetch, "MAX_BYTES", 100)
    web({("news.example.org", "/big"): html("<p>" + "x" * 500 + "</p>"),
         ("news.example.org", "/declared"): html("<p>hi</p>", **{"content-length": "999999"})})
    assert code("https://news.example.org/big") == "too_large"
    assert code("https://news.example.org/declared") == "too_large"


def test_a_slow_page_times_out(web, monkeypatch):
    web({})

    async def handler(request):
        await asyncio.sleep(5)
        return html("<p>late</p>")
    monkeypatch.setattr(fetch, "_transport", httpx.MockTransport(handler))
    with pytest.raises(fetch.FetchError) as err:
        get("https://news.example.org/slow", timeout_s=1)
    assert err.value.code == "timeout"


# --- the guard ----------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://news.example.org/a", "ftp://news.example.org/a", "https://user:pw@news.example.org/a",
    "https://news.example.org:8443/a", "https://news.example.org:80/a", "javascript:alert(1)",
    "https://news.example.org/a b", "not a url"])
def test_only_https_on_443(url):
    with pytest.raises(fetch.FetchError) as err:
        fetch.check_url(url)
    assert err.value.code in ("bad_url", "blocked")


def test_port_443_spelled_out_is_fine():
    assert fetch.check_url("https://news.example.org:443/a")[0] == "news.example.org"


@pytest.mark.parametrize("host", [
    "127.0.0.1", "10.0.0.5", "93.184.216.34", "2130706433", "0177.0.0.1", "0x7f.0.0.1", "0x7f000001",
    "127.1", "[::1]", "[::ffff:127.0.0.1]", "[::ffff:7f00:1]", "[2606:4700::1111]", "[fe80::1%25en0]",
    "localhost", "printer.local", "router", "intranet.lan", "foo.internal", "a.localhost", "1.2.3.4.5"])
def test_every_ip_literal_and_local_name_is_refused(host):
    with pytest.raises(fetch.FetchError) as err:
        fetch.check_url(f"https://{host}/x")
    assert err.value.code in ("blocked", "bad_url")


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "100.127.255.254",
    "0.0.0.0", "224.0.0.1", "255.255.255.255", "192.0.2.1", "198.18.0.1", "::1", "fe80::1", "fc00::1",
    "::ffff:10.0.0.1", "64:ff9b::a00:1", "2002:a00:1::", "ff02::1", "::"])
def test_private_addresses_are_not_public(address):
    assert not fetch.public_address(address)


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111", "64:ff9b::808:808"])
def test_public_addresses_are(address):
    assert fetch.public_address(address)


def test_a_name_with_any_private_address_is_refused(web):
    server = web({("news.example.org", "/a"): html("<p>hi</p>")},
                 dns={"news.example.org": ["93.184.216.34", "10.0.0.7"],
                      "meta.example.org": ["169.254.169.254"], "cgnat.example.org": ["100.64.1.1"],
                      "v6.example.org": ["::ffff:127.0.0.1"]})
    for host in ("news", "meta", "cgnat", "v6"):
        assert code(f"https://{host}.example.org/a") == "blocked"
    assert server.requests == []     # refused before any connection


def test_every_redirect_hop_is_checked_again(web):
    server = web({("news.example.org", "/r1"): httpx.Response(302, headers={"location": "https://cdn.example.net/r2"}),
                  ("cdn.example.net", "/r2"): httpx.Response(301, headers={"location": "/arrived"}),
                  ("cdn.example.net", "/arrived"): html("<p>Arrived.</p>"),
                  ("news.example.org", "/to-http"): httpx.Response(302, headers={"location": "http://cdn.example.net/x"}),
                  ("news.example.org", "/to-local"): httpx.Response(302, headers={"location": "https://internal.example.org/"}),
                  ("news.example.org", "/to-ip"): httpx.Response(302, headers={"location": "https://169.254.169.254/"}),
                  ("news.example.org", "/loop"): httpx.Response(302, headers={"location": "/loop"})},
                 dns={"news.example.org": ["93.184.216.34"], "cdn.example.net": ["93.184.216.35"],
                      "internal.example.org": ["192.168.0.10"]})
    got = get("https://news.example.org/r1")
    assert got["url"] == "https://cdn.example.net/arrived" and got["text"] == "Arrived."
    assert [r.headers["host"] for r in server.requests] == ["news.example.org", "cdn.example.net", "cdn.example.net"]
    assert all("cookie" not in r.headers and "referer" not in r.headers for r in server.requests)
    assert code("https://news.example.org/to-http") == "bad_url"
    assert code("https://news.example.org/to-local") == "blocked"
    assert code("https://news.example.org/to-ip") == "blocked"
    server.requests.clear()
    assert code("https://news.example.org/loop") == "too_many_redirects"
    assert len(server.requests) == fetch.MAX_REDIRECTS + 1


def test_cookies_set_by_one_hop_never_reach_the_next(web):
    server = web({("news.example.org", "/r"): httpx.Response(
                      302, headers={"location": "/b", "set-cookie": "session=abc; Path=/"}),
                  ("news.example.org", "/b"): html("<p>ok</p>")})
    get("https://news.example.org/r")
    assert "cookie" not in server.requests[1].headers


def test_the_client_ignores_proxy_settings_from_the_environment(web, monkeypatch):
    seen = {}
    real = httpx.AsyncClient

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)
    monkeypatch.setattr(fetch.httpx, "AsyncClient", spy)
    web({("news.example.org", "/a"): html("<p>ok</p>")})
    get("https://news.example.org/a")
    assert seen["trust_env"] is False and seen["follow_redirects"] is False


def test_only_status_and_host_are_logged(web, caplog):
    web({("news.example.org", "/private-path"): html("<title>Secret title</title><p>Secret words</p>")})
    with caplog.at_level(logging.DEBUG, logger="service.study.fetch"):
        get("https://news.example.org/private-path?q=secret")
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "news.example.org" in logged and "200" in logged
    assert "private-path" not in logged and "Secret" not in logged and "secret" not in logged


def test_fetch_many_runs_at_most_six_at_once(web, monkeypatch):
    live = {"now": 0, "max": 0}

    async def handler(request):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.02)
        live["now"] -= 1
        return html("<p>ok</p>")
    web({})
    monkeypatch.setattr(fetch, "_transport", httpx.MockTransport(handler))
    urls = [f"https://news.example.org/{i}" for i in range(14)] + ["http://news.example.org/x"]
    got = asyncio.run(fetch.fetch_many(urls))
    assert all(g["text"] == "ok" for g in got[:14]) and isinstance(got[14], fetch.FetchError)
    assert live["max"] <= fetch.CONCURRENCY


@pytest.mark.parametrize("location", ["https://forms.gle/abc", "https://news.example.org/quiz/3",
                                      "https://news.example.org/x?token=1", "https://zoom.us/j/1",
                                      "https://news.example.org/attempt"])
def test_a_redirect_to_a_never_open_address_is_refused(web, location):
    server = web({("news.example.org", "/r"): httpx.Response(302, headers={"location": location})})
    assert code("https://news.example.org/r") == "not_text"
    assert len(server.requests) == 1          # the next hop is never requested


def test_a_compressed_reply_is_refused(web):
    web({("news.example.org", "/z"): httpx.Response(200, headers={"content-type": "text/html",
                                                                 "content-encoding": "gzip"}, content=b"x")})
    assert code("https://news.example.org/z") == "not_text"
