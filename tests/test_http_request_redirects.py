"""http_request must never follow a redirect to a host the user didn't confirm."""
import asyncio

import httpx

from service.tools import action_tools


def test_http_request_does_not_follow_307(monkeypatch):
    hosts = []

    def handler(req: httpx.Request) -> httpx.Response:
        hosts.append(req.url.host)
        if req.url.host == "api.example.com":
            return httpx.Response(307, headers={"location": "https://attacker.example/steal"})
        return httpx.Response(200, text="stolen")

    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        assert kwargs.get("follow_redirects") is False
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(action_tools.httpx, "AsyncClient", client)
    fn = getattr(action_tools.http_request, "__wrapped__", action_tools.http_request)
    out = asyncio.run(fn("https://api.example.com/hook", body='{"secret": 1}',
                         headers='{"X-Api-Key": "k"}'))
    assert hosts == ["api.example.com"]
    assert "NOT followed" in str(out)
    assert "attacker.example" in str(out)
