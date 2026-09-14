"""Pro receives untrusted results; no mini, credentials or effects are used."""
import asyncio
import copy
from unittest.mock import AsyncMock

import httpx
import pytest

from service import nodes
from service.config.endpoints import Endpoint


def result(node="mini", rid="r1"):
    return {"schema_version": 1, "node_id": node, "result_id": rid, "job_id": "j1",
            "occurrence_id": "o1", "kind": "study.generate", "title": "Study guide", "text": "Synthetic guide", "proposal": None}


def page(*results, cursor="next"):
    return {"schema_version": 1, "results": list(results), "next_cursor": cursor}


def test_duplicate_delivery_and_cursor_commit(tmp_path):
    inbox = nodes.NodeInbox(tmp_path / "inbox.db")
    inbox.ingest("mini", page(result()), expected_cursor="")
    inbox.ingest("mini", page(result(), cursor="later"), expected_cursor="next")
    assert inbox.cursor("mini") == "later"
    assert len(inbox.pending("mini")) == 1


def test_conflicting_page_rolls_back_results_and_cursor(tmp_path):
    inbox = nodes.NodeInbox(tmp_path / "inbox.db")
    inbox.ingest("mini", page(result()), expected_cursor="")
    changed = result(); changed["text"] = "conflicting replacement"
    with pytest.raises(nodes.NodeProtocolError):
        inbox.ingest("mini", page(result(rid="r2"), changed, cursor="later"), expected_cursor="next")
    assert inbox.cursor("mini") == "next"
    assert len(inbox.pending("mini")) == 1


@pytest.mark.parametrize("mutation", [lambda r: r.update(type="send_email"), lambda r: r.update(approved=True),
    lambda r: r.update(schema_version=True), lambda r: r.update(node_id="other"), lambda r: r.update(kind="send_email")])
def test_forged_effect_authority_rejected(tmp_path, mutation):
    inbox = nodes.NodeInbox(tmp_path / "inbox.db")
    row = result(); mutation(row)
    with pytest.raises(nodes.NodeProtocolError):
        inbox.ingest("mini", page(row), expected_cursor="")
    assert inbox.cursor("mini") == ""


def test_effect_proposal_is_presentation_only(tmp_path):
    inbox = nodes.NodeInbox(tmp_path / "inbox.db")
    row = result(); row.update(kind="effect.proposal", proposal={"effect_id": "e1", "kind": "email.send", "arguments": {"to": "fixture@example.invalid"}})
    inbox.ingest("mini", page(row), expected_cursor="")
    hub = type("Hub", (), {"publish": AsyncMock()})()
    asyncio.run(inbox.publish("mini", hub))
    event = hub.publish.call_args.args[0]
    assert event["type"] == "node_result" and "not executed" in event["text"]
    assert not {"approved", "action_id", "arguments"} & event.keys()


def test_publish_failure_recovers_without_duplicate_hub_event(tmp_path):
    async def run():
        inbox = nodes.NodeInbox(tmp_path / "inbox.db")
        inbox.ingest("mini", page(result()), expected_cursor="")
        seen, attempts = {}, []
        class Hub:
            async def publish(self, event, *, dedupe_key):
                seen.setdefault(dedupe_key, event)
                attempts.append(dedupe_key)
                if len(attempts) == 1:
                    raise RuntimeError("crash after durable hub commit")
        with pytest.raises(RuntimeError):
            await inbox.publish("mini", Hub())
        recovered = nodes.NodeInbox(tmp_path / "inbox.db")
        await recovered.publish("mini", Hub())
        assert len(seen) == 1 and len(attempts) == 2
        assert not recovered.pending("mini")
    asyncio.run(run())


def test_dedupe_keys_encode_node_result_pair(tmp_path):
    async def run():
        inbox = nodes.NodeInbox(tmp_path / "inbox.db")
        hub = type("Hub", (), {"publish": AsyncMock()})()
        for node, rid in [("a:b", "c"), ("a", "b:c")]:
            inbox.ingest(node, page(result(node, rid)), expected_cursor="")
            await inbox.publish(node, hub)
        keys = [c.kwargs["dedupe_key"] for c in hub.publish.call_args_list]
        assert len(set(keys)) == 2
    asyncio.run(run())


def test_poll_uses_node_credential_and_durable_cursor(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("NODE_FIXTURE_KEY", "node-secret")
        monkeypatch.setattr(nodes, "endpoint", lambda name: Endpoint(name, "https://node.test", "env:NODE_FIXTURE_KEY"))
        inbox = nodes.NodeInbox(tmp_path / "inbox.db")
        def handler(req):
            assert req.headers["Authorization"] == "Bearer node-secret"
            assert req.url.path == "/v1/results"
            return httpx.Response(200, json=page(result()))
        hub = type("Hub", (), {"publish": AsyncMock()})()
        await nodes.poll_once(inbox, hub, {"node_id": "mini", "endpoint": "mini-node"}, transport=httpx.MockTransport(handler))
        assert inbox.cursor("mini") == "next" and not inbox.pending("mini")
    asyncio.run(run())


@pytest.mark.asyncio
async def test_cursor_recovery_replays_without_duplicate_publication(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_FIXTURE_KEY", "node-secret")
    monkeypatch.setattr(nodes, "endpoint", lambda name: Endpoint(name, "https://node.test", "env:NODE_FIXTURE_KEY"))
    inbox = nodes.NodeInbox(tmp_path / "inbox.db")
    inbox.ingest("mini", page(result()), expected_cursor="")
    inbox.ingest("other", page(result("other")), expected_cursor="")
    hub = type("Hub", (), {"publish": AsyncMock()})()
    await inbox.publish("mini", hub)
    calls = []
    def handler(req):
        calls.append(req.url.params['cursor'])
        assert req.headers['Authorization'] == 'Bearer node-secret'
        if calls[-1]:
            return httpx.Response(400, json={'error': {'code': 'invalid_cursor_or_query'}})
        return httpx.Response(200, json=page(result(), result(rid='r2'), cursor='rotated'))
    await nodes.poll_once(inbox, hub, {'node_id': 'mini', 'endpoint': 'mini-node'}, transport=httpx.MockTransport(handler))
    assert calls == ['next', '']
    assert inbox.cursor('mini') == 'rotated' and inbox.cursor('other') == 'next'
    assert hub.publish.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body', [
    (401, b'{"error":{"code":"invalid_cursor_or_query"}}'),
    (403, b'{"error":{"code":"invalid_cursor_or_query"}}'),
    (409, b'{"error":{"code":"invalid_cursor_or_query"}}'),
    (400, b'{"error":{"code":"other"}}'),
    (400, b'{"error":{"code":"invalid_cursor_or_query"},"extra":true}'),
    (400, b'{"error":{"code":"invalid_cursor_or_query","code":"invalid_cursor_or_query"}}'),
    (400, b'not JSON'), (400, b' '*1025), (302, b'{}')])
async def test_cursor_recovery_rejects_other_failures(tmp_path, monkeypatch, status, body):
    monkeypatch.setenv('NODE_FIXTURE_KEY', 'fixture')
    monkeypatch.setattr(nodes, 'endpoint', lambda name: Endpoint(name, 'https://node.test', 'env:NODE_FIXTURE_KEY'))
    inbox = nodes.NodeInbox(tmp_path / 'inbox.db')
    inbox.ingest('mini', page(result()), expected_cursor='')
    hub = type('Hub', (), {'publish': AsyncMock()})()
    with pytest.raises((ValueError, httpx.HTTPError)):
        await nodes.poll_once(inbox, hub, {'node_id': 'mini', 'endpoint': 'mini-node'},
            transport=httpx.MockTransport(lambda req: httpx.Response(status, content=body)))
    assert inbox.cursor('mini') == 'next'
    hub.publish.assert_not_awaited()


def test_cursor_reset_is_compare_and_swap(tmp_path):
    inbox = nodes.NodeInbox(tmp_path / 'inbox.db')
    inbox.ingest('mini', page(result()), expected_cursor='')
    with pytest.raises(nodes.NodeProtocolError):
        inbox.reset_cursor('mini', 'stale')
    assert inbox.cursor('mini') == 'next'
    inbox.reset_cursor('mini', 'next')
    with pytest.raises(nodes.NodeProtocolError):
        inbox.reset_cursor('mini', 'next')
    assert len(inbox.pending('mini')) == 1
