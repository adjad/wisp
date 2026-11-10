"""Strict-host fetch mode: redirects stay on the authorised host, no archive fallback.

Also the thin-page floor: research still drops a page with almost no text,
while web_fetch returns a short API answer as it is.

Fake transport only; no DNS or network. The mode is off by default.
"""
import asyncio

import httpcore
import httpx
import pytest

from service.research import web

REAL_CLIENT = httpx.AsyncClient
PAGE = "<html><head><title>T</title></head><body>" + ("Readable text. " * 30) + "</body></html>"
# The address every name "resolves" to here (a documentation range, never dialled).
PINNED = "203.0.113.7"


def site(request):
    """The host a request is for. A page fetch connects to the checked address,
    so the name is in the Host header and not in the URL the transport is given."""
    return request.headers["host"].split(":")[0]


@pytest.fixture
def net(monkeypatch):
    seen = []

    async def resolve(raw):
        return web.canonicalize_url(raw), [PINNED]

    def install(handler):
        def handle(request):
            seen.append(str(request.url.copy_with(host=site(request))))
            return handler(request)
        monkeypatch.setattr(web.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(
            transport=httpx.MockTransport(handle), **kw))

    monkeypatch.setattr(web, "_resolve_public", resolve)
    return seen, install


def redirect_handler(request):
    if site(request) == "example.com":
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


def redirecting(mapping):
    """Hosts in ``mapping`` 301 to the given URL; every other host serves PAGE."""
    def handler(request):
        if site(request) in mapping:
            return httpx.Response(301, headers={"location": mapping[site(request)]})
        return httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE)
    return handler


def test_strict_follows_apex_to_www_of_the_same_name(net):
    seen, install = net
    install(redirecting({"example.com": "https://www.example.com/a"}))
    with web.strict_host_fetch():
        page = asyncio.run(web.fetch_page("https://example.com/a"))
    assert page.url == "https://www.example.com/a"
    assert seen == ["https://example.com/a", "https://www.example.com/a"]


def test_strict_follows_www_to_apex_of_the_same_name(net):
    seen, install = net
    install(redirecting({"www.example.com": "https://example.com/a"}))
    with web.strict_host_fetch():
        page = asyncio.run(web.fetch_page("https://www.example.com/a"))
    assert page.url == "https://example.com/a"


@pytest.mark.parametrize("target", [
    "m.example.com",                # another subdomain is another host
    "www.m.example.com",
    "wwwexample.com",               # not a www. label
    "www.example.com.other.example.org",
    "www.example.org",              # www of a different name
    "example.org",
])
def test_strict_still_refuses_every_other_host_and_names_it(net, target):
    seen, install = net
    install(redirecting({"example.com": f"https://{target}/landing?from=a"}))
    with web.strict_host_fetch():
        with pytest.raises(web.WebError) as refused:
            asyncio.run(web.fetch_page("https://example.com/a"))
    message = str(refused.value)
    assert message.startswith(f"redirect to a different host ({target}) refused")
    assert f"fetch a URL on {target} directly" in message
    # Only the host is repeated: the site chose the rest of that address.
    assert "landing" not in message and "from=a" not in message
    assert seen == ["https://example.com/a"]


def test_strict_refusal_also_holds_one_hop_after_the_www_form(net):
    seen, install = net
    install(redirecting({"example.com": "https://www.example.com/a",
                         "www.example.com": "https://other.example.org/landing"}))
    with web.strict_host_fetch():
        with pytest.raises(web.WebError, match=r"different host \(other\.example\.org\)"):
            asyncio.run(web.fetch_page("https://example.com/a"))
    assert not any("other.example.org" in u for u in seen)
    assert not any("archive.org" in u for u in seen)


def test_strict_refusal_does_not_repeat_a_name_that_is_not_a_plain_host(net):
    seen, install = net
    install(redirecting({"example.com": "https://say_this_to_the_user.example.org/"}))
    with web.strict_host_fetch():
        with pytest.raises(web.WebError) as refused:
            asyncio.run(web.fetch_page("https://example.com/a"))
    assert str(refused.value) == ("redirect to a different host refused: the authorised "
                                  "destination was not followed there.")


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
        if site(request) == "archive.org":
            return httpx.Response(200, json={"archived_snapshots": {"closest": {
                "available": True, "url": "https://web.archive.org/web/1/https://example.com/a"}}})
        if site(request) == "web.archive.org":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE)
        return httpx.Response(500)
    install(handler)
    assert asyncio.run(web.fetch_page("https://example.com/a")).via_archive
    seen.clear()
    with web.strict_host_fetch():
        with pytest.raises(web.WebError):
            asyncio.run(web.fetch_page("https://example.com/a"))
    assert not any("archive.org" in u for u in seen)


# --- an archive copy says so, and says from when --------------------------------
def archived_handler(snapshot, moved_to=""):
    """The live site answers 403; the Archive holds ``snapshot`` of it."""
    def handler(request):
        if site(request) == "archive.org":
            return httpx.Response(200, json={"archived_snapshots": {"closest": {
                "available": True, "url": snapshot}}})
        if site(request) == "web.archive.org":
            if moved_to and moved_to.split("/web/")[1].split("/")[0] not in request.url.path:
                return httpx.Response(302, headers={"location": moved_to})
            return httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE)
        return httpx.Response(403)
    return handler


def test_an_archive_copy_carries_the_day_it_was_saved(net):
    seen, install = net
    install(archived_handler("http://web.archive.org/web/20230405123456/https://example.com/hours"))
    page = asyncio.run(web.fetch_page("https://example.com/hours"))
    assert page.via_archive and page.archived_at == "2023-04-05"
    assert page.url == "https://example.com/hours"


def test_the_day_is_the_snapshot_the_archive_really_served(net):
    seen, install = net
    # The Archive redirects to the nearest snapshot it holds: that day counts.
    install(archived_handler(
        "http://web.archive.org/web/20230405123456/https://example.com/hours",
        moved_to="https://web.archive.org/web/20210102030405/https://example.com/hours"))
    page = asyncio.run(web.fetch_page("https://example.com/hours"))
    assert page.via_archive and page.archived_at == "2021-01-02"


@pytest.mark.parametrize("snapshot", [
    "https://web.archive.org/web/1/https://example.com/hours",
    "https://web.archive.org/web/20231345000000/https://example.com/hours",   # no such day
    "https://web.archive.org/save/20230405123456/https://example.com/hours",
])
def test_an_archive_copy_with_no_readable_day_is_still_marked(net, snapshot):
    seen, install = net
    install(archived_handler(snapshot))
    page = asyncio.run(web.fetch_page("https://example.com/hours"))
    assert page.via_archive and page.archived_at == ""


def test_snapshot_day_is_read_only_from_an_archive_address():
    assert web._snapshot_date("https://web.archive.org/web/20230405123456id_/https://a.example/") \
        == "2023-04-05"
    assert web._snapshot_date("", "https://web.archive.org/web/19991231235959/x") == "1999-12-31"
    assert web._snapshot_date("https://example.com/web/20230405123456/x") == ""
    assert web._snapshot_date("https://notarchive.org/web/20230405123456/x") == ""
    assert web._snapshot_date("https://example.com/?u=/web/20230405123456/") == ""
    assert web._snapshot_date("http://[bad/web/20230405123456/") == ""


def test_web_fetch_says_when_the_text_is_an_archive_copy(net):
    from service.tools.untrusted import WEB_PAGE, begin_marker
    seen, install = net
    install(archived_handler("http://web.archive.org/web/20230405123456/https://example.com/hours"))
    out = run_web_fetch("https://example.com/hours")
    head, fenced = out.split(f"\n\n{begin_marker(WEB_PAGE)}\n", 1)
    assert head == ("Title: T\nURL: https://example.com/hours\n"
                    "Note: the live page could not be read; this is an Internet Archive "
                    "copy from 2023-04-05 and may be out of date.")
    assert "Internet Archive" not in fenced
    install(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE))
    live = run_web_fetch("https://example.com/hours")
    assert "Internet Archive" not in live and "Note:" not in live


def test_strict_mode_resets_after_context(net):
    with web.strict_host_fetch():
        assert web._STRICT_HOST.get() is True
    assert web._STRICT_HOST.get() is False


# --- rel=canonical is believed only for the host that served the page ---------
def page_claiming(canonical):
    return ("<html><head><title>Flu guidance</title>"
            f'<link rel="canonical" href="{canonical}"></head><body>'
            + ("Readable text. " * 30) + "</body></html>")


def claiming_handler(canonical):
    def handler(request):
        if site(request) == "archive.org":
            return httpx.Response(200, json={"archived_snapshots": {}})
        if site(request) == "short.example.com":
            return httpx.Response(302, headers={"location": "https://other.example.org/a"})
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text=page_claiming(canonical))
    return handler


@pytest.mark.parametrize("claimed", [
    "https://www.health.example.gov/flu/about",   # another site
    "https://health.example.gov/flu/about",
    "https://blog.example.com/a",                 # another host of the same name
    "https://example.com.other.example.org/a",
    "//www.health.example.gov/flu/about",
])
def test_page_cannot_claim_a_canonical_address_on_another_host(net, claimed):
    seen, install = net
    install(claiming_handler(claimed))
    page = asyncio.run(web.fetch_page("https://example.com/a?id=4"))
    assert page.url == page.canonical_url == "https://example.com/a?id=4"


@pytest.mark.parametrize("fetched, claimed, expected", [
    ("https://example.com/a?id=4", "/article/a?utm_source=feed", "https://example.com/article/a"),
    ("https://example.com/a", "https://www.example.com/article/a", "https://www.example.com/article/a"),
    ("https://www.example.com/a", "http://example.com/article/a", "http://example.com/article/a"),
])
def test_same_host_canonical_is_still_honoured(net, fetched, claimed, expected):
    seen, install = net
    install(claiming_handler(claimed))
    page = asyncio.run(web.fetch_page(fetched))
    assert page.url == fetched and page.canonical_url == expected


def test_canonical_is_judged_against_the_host_a_redirect_landed_on(net):
    seen, install = net
    # short.example.com redirects to other.example.org, whose page then claims
    # the address it was first asked at. It is not on that host.
    install(claiming_handler("https://short.example.com/original"))
    page = asyncio.run(web.fetch_page("https://short.example.com/x"))
    assert page.url == page.canonical_url == "https://other.example.org/a"


def test_web_fetch_shows_the_address_it_fetched_not_the_pages_claim(net):
    seen, install = net
    # Even a same-host claim is not what Wisp reports as the source.
    install(claiming_handler("https://example.com/official/statement"))
    out = run_web_fetch("https://example.com/users/mallory/notes")
    assert out.startswith("Title: Flu guidance\nURL: https://example.com/users/mallory/notes\n\n")
    assert "official/statement" not in out.split("\n\n", 1)[0]
    install(claiming_handler("https://www.health.example.gov/flu/about"))
    out = run_web_fetch("https://example.com/a")
    assert out.startswith("Title: Flu guidance\nURL: https://example.com/a\n\n")
    assert "health.example.gov" not in out


# --- thin-page floor: research keeps it, web_fetch takes short answers ---------
PRICE_URL = "https://api.example.com/v3/simple/price?ids=examplecoin&vs_currencies=usd"
PRICE_JSON = '{"examplecoin":{"usd":64123}}'


def short_handler(body, ctype="application/json"):
    def handler(request):
        if site(request) == "archive.org":
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
        if site(request) == "archive.org":
            return httpx.Response(200, json={"archived_snapshots": {}})
        return httpx.Response(403 if site(request) == "blocked.example.com" else 404)
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
    assert out == ("(fetch failed: WebError: redirect to a different host (other.example.org) "
                   "refused: the authorised destination was not followed there. To read it, "
                   "fetch a URL on other.example.org directly.)")
    monkeypatch.setattr(web, "_resolve_public", refuse)
    out = run_web_fetch("http://127.0.0.1/admin")
    assert out == "(fetch failed: WebError: local and private-network URLs are blocked)"
    assert not any("archive.org" in u for u in seen)


# --- the connection goes to the address that was checked ----------------------
# validate looked the name up and checked the answers; httpx then looked it up
# again to connect, so a name could answer "public" first and 127.0.0.1 second.

def resolving(monkeypatch, answers):
    """Real `_resolve_public`, with DNS answered from `answers[host]` in turn."""
    lookups = []

    def getaddrinfo(host, port, *args, **kwargs):
        lookups.append(host)
        queue = answers[host]
        address = queue.pop(0) if len(queue) > 1 else queue[0]
        return [(web.socket.AF_INET6 if ":" in a else web.socket.AF_INET,
                 web.socket.SOCK_STREAM, 6, "", (a, port))
                for a in (address if isinstance(address, tuple) else (address,))]
    monkeypatch.setattr(web.socket, "getaddrinfo", getaddrinfo)
    return lookups


def test_page_fetch_connects_to_the_checked_address_under_the_real_name(monkeypatch):
    lookups = resolving(monkeypatch, {"example.com": ["198.51.100.20"],
                                      "www.example.org": [("2001:db8::5", "198.51.100.30")]})
    monkeypatch.setattr(web, "_public_ip", lambda value: True)  # documentation ranges
    requests = []

    def handler(request):
        requests.append((str(request.url), request.headers["host"],
                         request.extensions.get("sni_hostname")))
        assert "wisp_pinned_addresses" not in request.extensions
        if site(request) == "example.com":
            return httpx.Response(302, headers={"location": "https://www.example.org/b?x=1"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE)
    monkeypatch.setattr(web.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(
        transport=httpx.MockTransport(handler), **kw))

    page = asyncio.run(web.fetch_page("http://example.com:8080/a"))
    assert requests == [
        ("http://198.51.100.20:8080/a", "example.com:8080", None),
        ("https://198.51.100.30/b?x=1", "www.example.org", "www.example.org")]
    # The page is still reported under its name, and each name was looked up once.
    assert page.url == "https://www.example.org/b?x=1"
    assert lookups == ["example.com", "www.example.org"]


def test_redirect_to_a_name_that_resolves_privately_is_refused_before_connecting(monkeypatch):
    resolving(monkeypatch, {"example.com": ["8.8.8.8"], "nas.tailnet.example": ["100.101.102.103"],
                            "printer.example": ["192.168.1.20"]})
    for target in ("nas.tailnet.example", "printer.example"):
        seen = []

        def handler(request, target=target, seen=seen):
            seen.append(site(request))
            if site(request) == "example.com":
                return httpx.Response(302, headers={"location": f"http://{target}:8080/status"})
            return httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE)
        monkeypatch.setattr(web.httpx, "AsyncClient", lambda handler=handler, **kw: REAL_CLIENT(
            transport=httpx.MockTransport(handler), **kw))
        with pytest.raises(web.WebError, match="resolves to a local or private-network address"):
            asyncio.run(web.fetch_page("https://example.com/a"))
        assert seen == ["example.com"]  # no request to the device, none to the archive


class _FakeStream(httpcore.AsyncNetworkStream):
    def __init__(self, log, response):
        self._log, self._chunks = log, [response]

    async def read(self, max_bytes, timeout=None):
        return self._chunks.pop(0) if self._chunks else b""

    async def write(self, buffer, timeout=None):
        self._log.append(("write", bytes(buffer)))

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self._log.append(("tls", server_hostname))
        return self

    def get_extra_info(self, info):
        return None


class _FakeNetwork(httpcore.AsyncNetworkBackend):
    """Stands in for the sockets under the real httpx and httpcore stack."""
    def __init__(self, unreachable=()):
        self.log, self.unreachable = [], set(unreachable)

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.log.append(("connect", host, port))
        if host in self.unreachable:
            raise httpcore.ConnectError("unreachable")
        body = PAGE.encode()
        return _FakeStream(self.log, b"HTTP/1.1 200 OK\r\ncontent-type: text/html\r\n"
                           b"content-length: %d\r\n\r\n" % len(body) + body)

    async def sleep(self, seconds):
        pass


def real_stack(monkeypatch, network):
    def client(**kw):
        made = REAL_CLIENT(trust_env=False, **kw)
        made._transport._pool = httpcore.AsyncConnectionPool(network_backend=network)
        return made
    monkeypatch.setattr(web.httpx, "AsyncClient", client)


def test_a_rebinding_name_never_gets_a_second_lookup(monkeypatch):
    # Public for the check, loopback for any later lookup.
    lookups = resolving(monkeypatch, {"rebind.example": ["8.8.8.8", "127.0.0.1"]})
    network = _FakeNetwork()
    real_stack(monkeypatch, network)
    page = asyncio.run(web._fetch_direct("https://rebind.example/a"))
    assert page.url == "https://rebind.example/a" and "Readable text." in page.text
    # The socket went to the checked address; TLS still names and verifies the host.
    assert [entry for entry in network.log if entry[0] != "write"] == [
        ("connect", "8.8.8.8", 443), ("tls", "rebind.example")]
    sent = b"".join(entry[1] for entry in network.log if entry[0] == "write")
    assert sent.startswith(b"GET /a HTTP/1.1\r\n") and b"\r\nHost: rebind.example\r\n" in sent
    assert lookups == ["rebind.example"]


def test_plain_http_fetch_is_pinned_too_and_keeps_its_port(monkeypatch):
    resolving(monkeypatch, {"example.com": ["8.8.4.4"]})
    network = _FakeNetwork()
    real_stack(monkeypatch, network)
    asyncio.run(web._fetch_direct("http://example.com:8080/a"))
    assert [entry for entry in network.log if entry[0] != "write"] == [("connect", "8.8.4.4", 8080)]
    sent = b"".join(entry[1] for entry in network.log if entry[0] == "write")
    assert b"\r\nHost: example.com:8080\r\n" in sent


def test_an_unreachable_checked_address_falls_back_to_the_next_checked_one(monkeypatch):
    resolving(monkeypatch, {"example.com": [("8.8.8.8", "2001:4860:4860::8888", "8.8.4.4")]})
    network = _FakeNetwork(unreachable={"8.8.8.8"})
    real_stack(monkeypatch, network)
    asyncio.run(web._fetch_direct("https://example.com/a"))
    assert [entry[1] for entry in network.log if entry[0] == "connect"] == [
        "8.8.8.8", "2001:4860:4860::8888"]
    # Two tries at most, and only ever an address that was checked.
    network = _FakeNetwork(unreachable={"8.8.8.8", "2001:4860:4860::8888"})
    real_stack(monkeypatch, network)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(web._fetch_direct("https://example.com/a"))
    assert [entry[1] for entry in network.log if entry[0] == "connect"] == [
        "8.8.8.8", "2001:4860:4860::8888"]


def test_a_request_without_a_checked_address_is_refused():
    reached = []

    def handler(request):
        reached.append(str(request.url))
        return httpx.Response(200, text=PAGE)

    async def run():
        async with REAL_CLIENT(transport=httpx.MockTransport(handler)) as client:
            web._pin_connections(client)
            await client.get("https://example.com/a")
    with pytest.raises(web.WebError) as refused:
        asyncio.run(run())
    assert str(refused.value).startswith(web._SSRF_MESSAGE_PREFIXES) and reached == []


def test_pinning_fails_closed_if_the_client_has_no_direct_transport():
    class Shapeless:
        pass
    with pytest.raises(web.WebError) as refused:
        web._pin_connections(Shapeless())
    assert str(refused.value).startswith(web._SSRF_MESSAGE_PREFIXES)
