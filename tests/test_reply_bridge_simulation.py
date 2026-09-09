"""End-to-end reply contract through the sandbox app consumer, never real Mail."""
import asyncio
from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from sandbox.outbound import OutboundConsumer
from sandbox.world import World
from service.assistant.store import AssistantStore
from service.memory.store import SessionStore
from service.tasks.executor import execute_task
from service.tasks.reply_engine import prepare_task_turn_async
from service.tasks.source_readers import MailReader
from service.tools import action_tools


class Client:
    def __init__(self):
        self.posts = []

    async def post(self, path, json=None):
        self.posts.append(json)


def test_typed_reply_through_simulated_app_verifies_real_reply_to_and_cc(tmp_path, monkeypatch):
    world = World(path=tmp_path / "world.json", seed=7)
    client = Client()
    app = OutboundConsumer(world, client)
    original = next(e for e in world.state["emails"].values() if e["mailbox"] == "inbox")
    original["from_addr"] = "dan@fixture.example"
    original["reply_to"] = "support@fixture.example"
    original["cc"] = ["colleague@fixture.example"]
    seen = []

    async def bridge(kind, payload, **_):
        seen.append(kind)
        action_id = str(len(seen))
        await app._dispatch({"type": kind, "action_id": action_id, **payload})
        return next(p for p in client.posts if p["action_id"] == action_id)

    monkeypatch.setattr(action_tools, "app_request", bridge)
    now = datetime.now()
    reader = MailReader([{"message_id": original["message_id"], "account": original["account"],
                          "sender": "Dan", "subject": original["subject"], "body": original["body"],
                          "ts": now.timestamp()}], accounts=[original["account"]], synced_at=now.timestamp())
    sessions = SessionStore(tmp_path / "sessions.db")
    assistant = AssistantStore(tmp_path / "assistant.db")
    async def run():
        turn = await prepare_task_turn_async(sessions, "s", "reply all to Dan's email saying Thanks",
                                            assistant_store=assistant, mail_reader=reader, now=now)
        assert turn.executable
        assert seen == ["prepare_email_reply"]
        args = turn.plan.steps[0].args
        assert args["expected_reply"]["to"] == ["support@fixture.example"]
        assert "colleague@fixture.example" in args["expected_reply"]["cc"]
        approver = type("Approver", (), {"confirm": AsyncMock(return_value=True)})()
        result = await execute_task(turn.plan, AsyncMock(), approver)
        assert result.status == "completed"
        assert "support@fixture.example" in approver.confirm.call_args.args[0]["preview"]
        assert seen == ["prepare_email_reply", "reply_to_email"]
    asyncio.run(run())
    sent = [e for e in world.state["emails"].values() if e.get("in_reply_to") == original["message_id"]]
    assert len(sent) == 1 and sent[0]["to"] == ["support@fixture.example"]
    assert "colleague@fixture.example" in sent[0]["cc"]


def test_unavailable_raw_sync_cannot_authorize_source_resolution(tmp_path, monkeypatch):
    import httpx
    from sandbox.sync import SyncScheduler
    from service.tools import email_tools as mail
    from service.tasks.references import SourceRef, resolve_reference
    world = World(path=tmp_path / "world.json", seed=7)
    world.state["injection"]["unavailable"] = ["email"]
    monkeypatch.setattr(mail, "_raw_reference_scan", {})
    monkeypatch.setattr(mail, "_raw_emails", "old display data")
    monkeypatch.setattr(mail.cache_store, "save", lambda *a: None)
    class SyncClient:
        async def post(self, path, json):
            mail.cache_raw_emails(json["raw"], json["raw_coverage"])
            return httpx.Response(200, request=httpx.Request("POST", "http://fixture" + path))
    asyncio.run(SyncScheduler(world, SyncClient())._push("email_raw"))
    assert not mail.raw_reference_metadata()["complete"]
    r = MailReader([], **mail.raw_reference_metadata())
    assert resolve_reference(SourceRef("email"), r, now=datetime.now()).status == "unavailable"


@pytest.mark.parametrize("field", ["account", "account_id", "from_name", "from_addr", "subject", "message_id", "body", "to"])
@pytest.mark.parametrize("separator", ["\x01", "\x02"])
def test_raw_separator_collision_marks_affected_account_incomplete(tmp_path, field, separator):
    import httpx
    from sandbox.sync import SyncScheduler
    world = World(path=tmp_path / "world.json", seed=7)
    original = next(e for e in world.state["emails"].values() if e["mailbox"] == "inbox")
    original[field] = (["attacker" + separator + "@fixture.example"] if field == "to"
                       else "value" + separator + "injected")
    posts = []
    class SyncClient:
        async def post(self, path, json):
            posts.append(json)
            return httpx.Response(200, request=httpx.Request("POST", "http://fixture" + path))
    asyncio.run(SyncScheduler(world, SyncClient())._push("email_raw"))
    assert original["account"] in posts[0]["raw_coverage"]["failed_accounts"]
    assert not posts[0]["raw_coverage"]["complete"]


def test_sandbox_preparation_pins_native_id_and_uses_supplied_body(tmp_path):
    world = World(path=tmp_path / "world.json", seed=7)
    app = OutboundConsumer(world, Client())
    original = next(e for e in world.state["emails"].values() if e["mailbox"] == "inbox")
    original["account_id"] = "native-1"
    args = {"message_id": original["message_id"], "account": original["account"],
            "account_id": "native-1", "body": "Exact\nbody \\ path"}
    async def scenario():
        ok, _, result = await app._reply_action(args, prepare=True)
        assert ok and result["reply"]["content"] == args["body"]
        original["account_id"] = "replacement-account"
        ok, _, _ = await app._reply_action(args, prepare=True)
        assert not ok
    asyncio.run(scenario())


def test_app_rejects_recipient_change_after_preview_and_account_ambiguity(tmp_path):
    world = World(path=tmp_path / "world.json", seed=7)
    client = Client()
    app = OutboundConsumer(world, client)
    original = next(e for e in world.state["emails"].values() if e["mailbox"] == "inbox")
    copied = deepcopy(original)
    copied.update(id="copy", account="Other account")
    world.state["emails"]["copy"] = copied
    args = {"message_id": original["message_id"], "body": "Thanks"}
    async def run():
        await app._dispatch({"type": "prepare_email_reply", "action_id": "ambiguous", **args})
        assert client.posts[-1]["ok"] is False
        await app._dispatch({"type": "prepare_email_reply", "action_id": "preview",
                             **args, "account": original["account"]})
        expected = client.posts[-1]["reply"]
        original["reply_to"] = "changed@fixture.example"
        before = len(world.state["emails"])
        await app._dispatch({"type": "reply_to_email", "action_id": "send", **args,
                             "account": original["account"], "expected_reply": expected})
        assert client.posts[-1]["ok"] is False
        assert len(world.state["emails"]) == before
    asyncio.run(run())
