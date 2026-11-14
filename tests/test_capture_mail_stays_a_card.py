"""Caught for you never writes to Calendar or Reminders from MAIL by itself (MAIL_CAPTURES_ITSELF).

Synthetic rows and records only: no mailbox, Messages, Calendar or user state is read,
and every native effect is a fake that records its call.
"""
import sys
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from service.attention import event_capture
from service.attention.event_capture import candidates, capture, captures_itself, mail_items
from service.attention.ledger import Ledger
from service.attention.live import Settings, load_settings

TZ = 'America/Los_Angeles'
MON = datetime(2026, 10, 19, 10, tzinfo=ZoneInfo(TZ)).timestamp()
HOUR = 3600
POSED = ('"Dean <dean@school.edu>" <x@evil.com>', '')
SPLIT = ('Dean <dean@school.edu>', 'x@evil.com')


def mail(**changes):
    return {'ts': MON, 'account_id': 'synthetic-account', 'message_id': '<exam@example.test>',
            'sender': 'Yunan Wang via Ed <noreply@edstem.org>',
            'subject': '26 Fall-STAT 5 02: Reminder: STAT 5 Exam 1 Next Wednesday', 'body': '', **changes}


def message(text, *, ts=MON, direction='incoming', guid='plan'):
    return {'guid': guid, 'conversation': 'synthetic-friend', 'sender': 'Friend', 'timestamp': ts,
            'direction': direction, 'text': text}


def plans(rows=(), records=(), *, now=MON + 10):
    return candidates(list(records), list(rows), now=now, tz=TZ, settings=Settings())


@pytest.fixture
def wired(tmp_path, monkeypatch):
    from service.assistant import attention_api
    from service.assistant import attention_runner as wiring
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    wiring.reset()
    monkeypatch.setattr(wiring, 'MOE_DIR', tmp_path)
    monkeypatch.setattr(attention_api, 'MOE_DIR', tmp_path)
    monkeypatch.setattr(wiring, 'current_zone', lambda: TZ)
    clock = SimpleNamespace(value=MON + 10)
    monkeypatch.setattr(wiring, 'time', SimpleNamespace(time=lambda: clock.value))
    monkeypatch.setattr(attention_api, 'time', SimpleNamespace(time=lambda: clock.value))
    sources = {'records': [], 'rows': []}
    monkeypatch.setattr(wiring, 'capture_sources', lambda: (sources['records'], sources['rows'], {}, None))
    monkeypatch.setattr(wiring, '_capture_commitments', lambda *args: [])
    writes, checks = [], []

    async def calendar(plan, *, approval=None, source_current=None):
        # The real Calendar writer re-reads admission here; record what it would see.
        checks.append(source_current() if source_current else None)
        writes.append(('calendar', plan, approval))
        return {'ok': True, 'status': 'succeeded', 'source_id': 'fake-calendar'}

    async def reminder(c, *, automatic):
        writes.append(('reminder', c, automatic))
        return {'ok': True, 'status': 'succeeded'}
    monkeypatch.setattr(wiring, '_capture_reminder', reminder)
    monkeypatch.setitem(sys.modules, 'service.assistant.attention_calendar',
                        SimpleNamespace(create_calendar=calendar, approve_capture=lambda plan: object()))
    app = FastAPI()
    app.include_router(attention_api.router)
    yield SimpleNamespace(wiring=wiring, client=TestClient(app), clock=clock, sources=sources,
                          writes=writes, checks=checks, settings=tmp_path / 'attention/settings.json')
    wiring.reset()


def go_live(w):
    assert w.client.put('/assistant/attention/settings', json={'mode': 'live', 'capture_events': True}).status_code == 200
    w.clock.value += 300


async def test_live_mail_candidate_is_never_created_by_itself_but_stays_tap_addable(wired):
    w = wired
    go_live(w)
    w.sources['rows'] = [mail(message_id='<new@example.test>', ts=w.clock.value - 1)]
    found = w.wiring.capture_candidates(w.clock.value, TZ, load_settings(w.settings))
    assert len(found) == 1 and found[0].automatic_eligible and found[0].source_kind == 'mail'
    assert captures_itself(found[0]) is False
    assert 'from email: waits for your tap' in found[0].when
    assert await w.wiring.run_capture_tick(w.clock.value, TZ, load_settings(w.settings)) == []
    assert w.writes == []
    # Re-checked at the effect too: even Live + capture_events does not admit it automatically.
    assert w.wiring._capture_current(found[0], automatic=True) is False
    assert w.wiring._capture_current(found[0], automatic=False) is True
    record = w.client.get('/assistant/attention').json()['recent'][0]
    assert record['state'] == 'shadow' and record['captureable'] is True
    added = w.client.post('/assistant/attention/capture', json={
        'source_id': record['source_id'], 'fingerprint': record['fingerprint']})
    assert added.status_code == 200 and added.json()['state'] == 'created'
    assert [k for k, *_ in w.writes] == ['calendar', 'reminder']
    assert w.writes[0][1]['automatic'] is False and w.writes[1][2] is False
    assert w.checks == [True]


async def test_live_text_candidate_is_still_created_by_itself(wired):
    w = wired
    go_live(w)
    w.sources['records'] = [message('Hi', ts=MON - HOUR, direction='outgoing', guid='history'),
                            message('Our coffee chat next Thursday', ts=w.clock.value - 1)]
    rows = await w.wiring.run_capture_tick(w.clock.value, TZ, load_settings(w.settings))
    assert len(rows) == 1 and rows[0]['state'] == 'created'
    assert [k for k, *_ in w.writes] == ['calendar', 'reminder']
    assert w.writes[0][1]['automatic'] is True and w.writes[1][2] is True
    assert w.checks == [True]


async def test_flipping_the_constant_on_restores_automatic_mail_capture(wired, monkeypatch):
    monkeypatch.setattr(event_capture, 'MAIL_CAPTURES_ITSELF', True)
    w = wired
    go_live(w)
    w.sources['rows'] = [mail(message_id='<new@example.test>', ts=w.clock.value - 1)]
    rows = await w.wiring.run_capture_tick(w.clock.value, TZ, load_settings(w.settings))
    assert len(rows) == 1 and rows[0]['state'] == 'created'
    assert [k for k, *_ in w.writes] == ['calendar', 'reminder']
    assert w.writes[0][1]['automatic'] is True and w.checks == [True]


async def test_capture_itself_refuses_automatic_mail_before_any_claim_or_effect(tmp_path):
    c = plans([mail()])[0]
    ledger = Ledger(tmp_path / 'attention.db')
    calls = []

    async def effect(candidate, **kwargs):
        calls.append(kwargs)
        return {'ok': True, 'status': 'succeeded'}
    with pytest.raises(ValueError):
        await capture(c, ledger=ledger, now=MON + 10, settings=Settings(), fresh_commitments=lambda: [],
                      create_calendar=effect, create_reminder=effect, automatic=True)
    assert calls == [] and ledger.get(c.capture_key) is None
    # A candidate merely relabelled as a text is still not a text.
    from dataclasses import replace
    assert captures_itself(replace(c, source_kind='messages')) is False
    row = await capture(c, ledger=ledger, now=MON + 10, settings=Settings(), fresh_commitments=lambda: [],
                        create_calendar=effect, create_reminder=effect, automatic=False)
    assert row['state'] == 'created' and calls == [{'automatic': False}, {'automatic': False}]


@pytest.mark.parametrize('sender,address', [POSED, SPLIT])
def test_a_posed_sender_is_never_school_mail(sender, address, monkeypatch):
    monkeypatch.setattr(event_capture, 'MAIL_CAPTURES_ITSELF', True)
    row = mail(sender=sender, sender_address=address)
    items, school = mail_items([row])
    assert school == set()
    assert [(i.sender, i.conversation) for i in items] == [('x@evil.com', 'x@evil.com')]
    assert plans([row]) == []


def test_a_sender_with_no_real_address_is_not_offered():
    for sender in ('Dean <dean@school.edu> <"x>"@evil.com>', 'Dean of School', ''):
        assert mail_items([mail(sender=sender)]) == ([], set())
    # A parsed-address field that is not one clean address is never mined for one
    # (the old parseaddr reading took dean@school.edu out of it).
    assert mail_items([mail(sender='x@evil.com', sender_address='Dean <dean@school.edu>')]) == ([], set())


def test_school_is_judged_on_the_real_address_domain_only():
    assert mail_items([mail(sender='Prof <prof@school.edu>')])[1]
    assert mail_items([mail(sender='Prof', sender_address='prof@school.edu')])[1]
    assert not mail_items([mail(sender='prof@school.edu via Ed <x@evil.com>')])[1]
    assert not mail_items([mail(sender='Prof <prof@school.edu.evil.com>')])[1]


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_capture_mail_stays_a_card.py"' in manifest
