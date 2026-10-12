"""The Catch demo path and the live attention runner never both act on one message.

Both key a message by its id ("msg:<identity>"). Catch records an `attention:<hash>` event;
the runner records a ledger claim. Each path checks the other's record before it acts.
Everything is faked or temp: no Reminders, no real ~/.moe.
"""
import asyncio

import pytest

from service.assistant import attention_demo as catch, attention_runner as wiring
from service.assistant.store import AssistantStore
from service.attention import live
from service.attention.corpus import Item
from service.attention.live import Settings
from tests import test_attention_wiring as w
from tests.test_attention_wiring import feed, isolated, effects, go_live  # noqa: F401  (fixtures)

NOW = w.NOW
MSG = "msg:m"                                    # the id the runner derives for FEED's "m"


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = AssistantStore(tmp_path / "assistant.db")
    monkeypatch.setattr(wiring, "assistant_store", s)
    return s


def catch_candidate(item_id, when_ts):
    return catch.Candidate(reason=catch.REASON, item_id=item_id, sender="Mom", conversation="Mom", quote="Meet me in the Quad at 6PM",
                           when_ts=when_ts, text="Meet me in the Quad at 6PM", rule="t")


def persist(store, item_id, when_ts):
    return catch._persist(store, catch_candidate(item_id, when_ts), NOW)


async def test_runner_leaves_a_message_catch_already_persisted(isolated, feed, effects, store):
    go_live(isolated)
    store.enqueue_event({"type": "reminder", "title": "x", "when_ts": NOW + 3600},
                        dedupe_key=catch._key(MSG), expires_at=NOW + 7200)
    out = await wiring.run_tick(NOW)
    assert [o.state for o in out] == ["handled_elsewhere"]
    assert effects["created"] == [] and effects["published"] == []
    assert wiring.get_ledger().get(MSG)["state"] == "handled_elsewhere"


async def test_runner_still_acts_when_catch_has_not(isolated, feed, effects, store):
    go_live(isolated)
    assert [o.state for o in await wiring.run_tick(NOW)] == ["created"]


async def test_shadow_runner_records_without_blocking_catch(isolated, feed, effects, store):
    await wiring.run_tick(NOW)                                   # default mode: shadow
    assert wiring.get_ledger().get(MSG)["state"] == "shadow"
    assert wiring.get_ledger().holds_effect(MSG) is False
    assert catch._stack_holds(MSG) is False


async def test_catch_defers_to_a_message_the_runner_created(isolated, feed, effects, store):
    go_live(isolated)
    await wiring.run_tick(NOW)
    assert wiring.get_ledger().get(MSG)["state"] == "created"
    assert catch._stack_holds(MSG) is True
    assert persist(store, MSG, NOW + 3 * 3600) is None
    assert store.event_by_key(catch._key(MSG)) is None


def test_catch_defers_to_an_in_flight_or_unknown_claim_but_not_a_failure(isolated, store):
    led = wiring.get_ledger()
    for sid, state in (("msg:a", "claimed"), ("msg:b", "unknown"), ("msg:c", "undone")):
        led.claim(sid, now=NOW)
        if state != "claimed":
            led.finish(sid, state, {}, NOW)
        assert catch._stack_holds(sid) is True
    led.claim("msg:d", now=NOW)
    led.finish("msg:d", "failed", {}, NOW)
    assert catch._stack_holds("msg:d") is False


def test_catch_fails_closed_when_the_ledger_is_unreadable(isolated, store, monkeypatch):
    monkeypatch.setattr(wiring, "get_ledger", lambda: (_ for _ in ()).throw(OSError("locked")))
    assert catch._stack_holds(MSG) is True
    assert persist(store, MSG, NOW + 3 * 3600) is None


def test_synthetic_overlay_messages_are_unaffected(isolated, store):
    assert catch._stack_holds("demo:1") is False
    assert persist(store, "demo:1", NOW + 3 * 3600) is not None


def test_runner_fails_closed_when_the_store_is_unreadable(monkeypatch):
    class Broken:
        def event_by_key(self, key):
            raise OSError("locked")
    monkeypatch.setattr(wiring, "assistant_store", Broken())
    assert wiring._handled_by_catch(MSG) is True
