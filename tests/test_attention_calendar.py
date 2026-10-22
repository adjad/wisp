"""Synthetic capture consent, all-day wire and durable Calendar identities."""
import hashlib
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from service.assistant import attention_calendar as capture, outbox
from service.assistant.calendar_dates import all_day_bounds
from service.assistant.hub import Hub
from service.assistant.store import AssistantStore
from service.attention import live
from service.safety import Tier, policy, grants


async def create_calendar(item, **kwargs):
    return await capture.create_calendar(item, source_current=kwargs.pop('source_current', lambda: True), **kwargs)


def plan(**changes):
    return dict(capture_key="capture:fixture", title="STAT 5 Exam 1", day="2036-10-29",
                timezone="America/Los_Angeles", start_ts=None, duration_min=None,
                all_day=True, automatic=False, source_id="email:fixture", **changes)


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(policy, "_FULL_ACCESS", False)
    monkeypatch.setattr(grants, "check", lambda *a: None)
    from service.inference import remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)
    settings = SimpleNamespace(mode="shadow", capture_events=False)
    monkeypatch.setattr(live, "load_settings", lambda *a: settings)
    request = AsyncMock(return_value={"ok": True, "status": "succeeded", "source_id": "fixture-native"})
    monkeypatch.setattr(outbox, "request", request)
    return settings, request


@pytest.mark.parametrize("day,minutes", [("2036-03-09", 1380), ("2036-11-02", 1500), ("2036-10-29", 1440)])
def test_civil_day_uses_real_dst_bounds(day, minutes):
    start, elapsed = all_day_bounds(day, "America/Los_Angeles")
    assert elapsed == minutes
    zone = ZoneInfo("America/Los_Angeles")
    assert datetime.fromtimestamp(start, zone).date().isoformat() == day
    assert datetime.fromtimestamp(start + elapsed * 60, zone).hour == 0


@pytest.mark.asyncio
async def test_exact_host_approval_required_and_bound_to_whole_plan(isolated):
    _, request = isolated
    item = plan()
    assert (await create_calendar(item))["status"] == "blocked"
    assert (await create_calendar(item, approval={"approved": True}))["status"] == "blocked"
    approval = capture.approve_capture(item)
    for field, value in [("title", "Other exam"), ("day", "2036-10-30"),
                         ("timezone", "America/New_York"), ("source_id", "email:other"),
                         ("capture_key", "capture:other")]:
        assert (await create_calendar({**item, field: value}, approval=approval))["status"] == "blocked"
    request.assert_not_awaited()
    result = await create_calendar(item, approval=approval)
    assert result == {"ok": True, "status": "succeeded", "source_id": "fixture-native"}
    payload = request.await_args.args[1]
    assert payload["all_day"] is True and payload["due_date"] == item["day"]
    assert payload["timezone"] == "America/Los_Angeles"
    assert request.await_args.kwargs["idempotency_key"] == item["capture_key"]


@pytest.mark.asyncio
async def test_auto_consent_is_fresh_persisted_separate_from_legacy_live(isolated, monkeypatch):
    settings, request = isolated
    item = {**plan(), "automatic": True, "capture_events": True, "approved": True}
    for mode, enabled in [("shadow", True), ("off", True), ("live", False)]:
        settings.mode, settings.capture_events = mode, enabled
        assert (await create_calendar(item))["status"] == "blocked"
    request.assert_not_awaited()
    settings.mode, settings.capture_events = "live", True
    assert (await create_calendar(item))["ok"]
    # Generic tools keep the original always-confirm rule even with opt-in.
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    assert policy.decide("calendar_write", {}, tool="add_calendar_event").tier is Tier.CONFIRM
    settings.capture_events = False
    assert (await create_calendar(item))["status"] == "blocked"


@pytest.mark.asyncio
async def test_view_only_deny_grants_and_unknown_are_preserved(isolated, monkeypatch):
    _, request = isolated
    item = plan()
    token = capture.approve_capture(item)
    monkeypatch.setattr(policy, "_READ_ONLY", True)
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    assert (await create_calendar(item, approval=token))["status"] == "blocked"
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(grants, "check", lambda *a: "deny")
    assert (await create_calendar(item, approval=token))["status"] == "blocked"
    request.assert_not_awaited()
    monkeypatch.setattr(grants, "check", lambda *a: None)
    request.return_value = {"ok": False, "status": "unknown", "error": "receipt lost"}
    assert (await create_calendar(item, approval=token))["status"] == "unknown"


@pytest.mark.asyncio
async def test_missing_time_is_not_a_guessed_timed_meeting(isolated):
    _, request = isolated
    item = {**plan(), "all_day": False, "start_ts": 2100000000.0}
    assert (await create_calendar(item, approval=capture.approve_capture(item)))["status"] == "blocked"
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_source_is_revalidated_at_dispatch(isolated):
    _, request = isolated
    item, current = plan(), True
    async def reconnected(*args, **kwargs):
        nonlocal current
        current = False  # withdrawal/edit while waiting for native reconnect
        reason = kwargs['before_dispatch']()
        assert reason and 'source' in reason
        return {'ok': False, 'status': 'blocked', 'error': reason}
    request.side_effect = reconnected
    result = await create_calendar(item, approval=capture.approve_capture(item), source_current=lambda: current)
    assert result['status'] == 'blocked'


def event_payload(day="2036-11-02"):
    start, duration = all_day_bounds(day, "America/Los_Angeles")
    return dict(title="Exam", when_ts=start, duration_min=duration, location="", all_day=True,
                due_date=day, timezone="America/Los_Angeles")


def test_strict_all_day_payload_and_success_receipt_store_civil_day(tmp_path):
    store = AssistantStore(tmp_path / "assistant.db")
    try:
        body = {"type": "create_calendar_event", "action_id": "fixture", **event_payload()}
        assert store.calendar_payload(body["type"], body) == body
        for changes in [{"all_day": 1}, {"due_date": "2036-02-30"}, {"duration_min": 1440},
                        {"when_ts": body["when_ts"] + 3600}, {"timezone": "invalid"}, {"invitees": ["other"]}]:
            with pytest.raises((ValueError, KeyError)):
                store.calendar_payload(body["type"], {**body, **changes})
        event = store.enqueue_event(body, dedupe_key="action:fixture", target={"type": "calendar"}, expires_at=time.time() + 60)
        claim = store.claim_calendar_action(event["id"], body["type"], "fixture", body)
        store.complete_calendar_action(event["id"], body["type"], claim["claim_token"],
            {"ok": True, "status": "succeeded", "error": "", "source_id": "native:fixture"})
        row = store.upcoming(body["when_ts"] - 60, days=2)[0]
        assert row["all_day"] and row["when_ts"] == body["when_ts"]
        # due_date in commitments is the Reminders civil alert index; Calendar
        # keeps its native midnight + end, with the source civil date in the action.
        assert row["due_date"] is None
        assert store.event(event["id"])["payload"]["due_date"] == body["due_date"]
        calendar_row = store.calendar_events_overlapping(body["when_ts"], body["when_ts"] + 26 * 3600)[0]
        assert calendar_row["end_ts"] - row["when_ts"] == 25 * 3600
    finally:
        store._db.close()


@pytest.mark.asyncio
async def test_capture_action_identity_survives_restart_and_unknown(tmp_path, monkeypatch):
    store = AssistantStore(tmp_path / "assistant.db")
    key = "capture:fixture"
    action = "calendar-capture:" + hashlib.sha256(key.encode()).hexdigest()
    payload = event_payload()
    body = {"type": "create_calendar_event", "action_id": action, **payload}
    event = store.enqueue_event(body, dedupe_key="action:" + action, target={"type": "calendar"}, expires_at=time.time() + 60)
    claim = store.claim_calendar_action(event["id"], body["type"], action, body)
    store._db.close()
    restarted = AssistantStore(tmp_path / "assistant.db")
    channel = Hub(restarted)
    monkeypatch.setattr(outbox, "hub", channel)
    try:
        # No app is connected: a claimed unknown is still returned without a new action.
        assert (await outbox.request("create_calendar_event", payload, idempotency_key=key))["status"] == "unknown"
        assert (await outbox.request("create_calendar_event", {**payload, "title": "Other"}, idempotency_key=key))["status"] == "failed"
        result = {"ok": True, "status": "succeeded", "error": "", "source_id": "native:fixture"}
        restarted.complete_calendar_action(event["id"], body["type"], claim["claim_token"], result)
        assert await outbox.request("create_calendar_event", payload, idempotency_key=key) == result
        assert len(restarted.reminder_actions_by_prefix(action)) == 1
    finally:
        restarted._db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_before_timeout", [False, True])
async def test_capture_timeout_reuses_unclaimed_or_late_receipt_identity(tmp_path, monkeypatch, claim_before_timeout):
    from service.assistant import store as store_module

    # Keep durable wall-clock expiry separate from the real asyncio waiter.
    # A slow SQLite commit must not expire the claimed-case fixture first.
    wall_clock = SimpleNamespace(now=time.time())
    clock = SimpleNamespace(time=lambda: wall_clock.now, monotonic=time.monotonic)
    monkeypatch.setattr(outbox, 'time', clock)
    monkeypatch.setattr(store_module, 'time', clock)
    store = AssistantStore(tmp_path / "assistant.db")
    class FakeNativeChannel:
        has_subscribers = True
        def __init__(self):
            self.store, self.publications, self.claim = store, [], None

        async def publish(self, payload, **kwargs):
            row = store.enqueue_event(payload, **kwargs)
            self.publications.append(row['id'])
            if claim_before_timeout:
                self.claim = store.claim_calendar_action(row['id'], payload['type'], payload['action_id'], payload)
    channel = FakeNativeChannel()
    monkeypatch.setattr(outbox, 'hub', channel)
    payload, key = event_payload(), 'capture:timeout-fixture'
    action = 'calendar-capture:' + hashlib.sha256(key.encode()).hexdigest()
    try:
        first = await outbox.request('create_calendar_event', payload, timeout=.001, idempotency_key=key)
        assert first['status'] == 'unknown'
        row = store.event_by_key('action:' + action)
        assert row['expires_at'] == wall_clock.now + .001
        wall_clock.now += 1  # both completion/retry occur after durable expiry
        if claim_before_timeout:
            # Native completion can arrive after the waiting caller timed out.
            assert channel.claim['execute'] is True
            receipt = {'ok': True, 'status': 'succeeded', 'error': '', 'source_id': 'native:late'}
            store.complete_calendar_action(row['id'], 'create_calendar_event', channel.claim['claim_token'], receipt)
            assert await outbox.request('create_calendar_event', payload, idempotency_key=key) == receipt
            assert len(channel.publications) == 1
        else:
            second = await outbox.request('create_calendar_event', payload, timeout=.001, idempotency_key=key)
            assert second['status'] == 'unknown'
            assert channel.publications == [row['id'], row['id']]
            claim = store.claim_calendar_action(row['id'], 'create_calendar_event', action, row['payload'])
            assert claim['execute'] is False and claim['result']['status'] == 'failed'
        assert len(store.reminder_actions_by_prefix(action)) == 1
    finally:
        store._db.close()


@pytest.mark.asyncio
async def test_revocation_during_app_reconnect_prevents_publication(tmp_path, monkeypatch):
    store = AssistantStore(tmp_path / 'assistant.db')
    channel = Hub(store)
    publish = AsyncMock()
    monkeypatch.setattr(channel, 'publish', publish)
    monkeypatch.setattr(outbox, 'hub', channel)
    granted = True
    async def reconnect():
        nonlocal granted
        granted = False
        return True
    monkeypatch.setattr(outbox, 'app_connected', reconnect)
    try:
        result = await outbox.request('create_calendar_event', event_payload(),
            idempotency_key='capture:revoked', before_dispatch=lambda: None if granted else 'Capture disabled')
        assert result['status'] == 'blocked'
        publish.assert_not_awaited()
        assert outbox._pending == {}
    finally:
        store._db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('authority', ['grant', 'full_access'])
async def test_view_only_blocks_paired_reminder_despite_tool_authority(isolated, monkeypatch, automatic, authority):
    from service.assistant import attention_runner as runner
    settings, request = isolated
    settings.mode, settings.capture_events = 'live', True
    candidate = SimpleNamespace(source_id='fixture', review_fingerprint='exact', capture_key='capture:fixture',
                                timezone='America/Los_Angeles', title='Your exam', due_ts=time.time() + 3600)
    monkeypatch.setattr(runner, 'load_settings', lambda *a: settings)
    monkeypatch.setattr(runner, 'capture_candidates', lambda *a: [candidate])
    monkeypatch.setattr(runner, '_audit', lambda *a, **k: None)
    monkeypatch.setattr(policy, '_READ_ONLY', True)
    if authority == 'grant':
        monkeypatch.setattr(grants, 'check', lambda *a: 'allow')
    else:
        monkeypatch.setattr(policy, '_FULL_ACCESS', True)
    # The generic tool policy can permit a specific write; autonomous source
    # interpretation still has a hard view-only boundary for both pair effects.
    assert runner._policy_block() is None
    assert runner._capture_current(candidate, automatic=automatic) is False
    assert (await runner._capture_reminder(candidate, automatic=automatic))['status'] == 'blocked'
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_paired_reminder_rechecks_view_only_after_native_reconnect(isolated, monkeypatch):
    from service.assistant import attention_runner as runner
    _, request = isolated
    candidate = SimpleNamespace(title='Your exam', due_ts=time.time() + 3600, capture_key='capture:fixture')
    monkeypatch.setattr(runner, '_capture_current', lambda *a, **k: True)
    monkeypatch.setattr(runner, '_audit', lambda *a, **k: None)
    monkeypatch.setattr(grants, 'check', lambda *a: 'allow')
    async def reconnect(*args, **kwargs):
        monkeypatch.setattr(policy, '_READ_ONLY', True)
        reason = kwargs['before_dispatch']()
        assert reason and 'View-only' in reason
        return {'ok': False, 'status': 'blocked', 'error': reason}
    request.side_effect = reconnect
    assert (await runner._capture_reminder(candidate, automatic=True))['status'] == 'blocked'
