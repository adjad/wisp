"""Synthetic event capture; no native tools, mailbox reads, or user state."""
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from service.attention.event_capture import candidates, capture, preview
from service.attention.ledger import Ledger
from service.attention.live import Settings, load_settings, save_settings

TZ = 'America/Los_Angeles'
ZONE = ZoneInfo(TZ)
MON = datetime(2026, 10, 19, 10, tzinfo=ZONE).timestamp()
HOUR = 3600


def mail(subject='26 Fall-STAT 5 02: Reminder: STAT 5 Exam 1 Next Wednesday', **changes):
    return {'ts': MON, 'account_id': 'synthetic-account', 'message_id': '<exam@example.test>',
            'sender': 'Yunan Wang via Ed <noreply@edstem.org>', 'subject': subject, 'body': '', **changes}


def message(text, *, ts=MON, direction='incoming', guid='plan'):
    return {'guid': guid, 'conversation': 'synthetic-friend', 'sender': 'Friend', 'timestamp': ts,
            'direction': direction, 'text': text}


def plans(rows=None, records=None, *, now=MON+10, settings=None):
    return candidates(records or [], rows if rows is not None else [mail()], now=now, tz=TZ,
                      settings=settings or Settings())


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / 'attention.db')


def test_announced_exam_eligible_without_text_contact_and_anchored_to_received_day():
    early = plans()[0]
    delayed = plans(now=MON+86400)[0]
    assert early.day == delayed.day == '2026-10-21'
    assert early.start_ts is None and early.duration_min is None
    assert 'all-day Calendar' in early.when and '09:00' in early.when
    assert early.calendar_plan(automatic=True)['all_day'] is True


def test_exam_never_rebased_after_original_wednesday():
    assert plans(now=MON+7*86400) == []


@pytest.mark.parametrize('text,day', [
    ('Coffee chat next Thursday', '2026-10-22'),
    ('Our hangout next Friday', '2026-10-23'),
    ("See you for rehearsal next Wenesday", '2026-10-21'),
    ("Our interview next Thirsday", '2026-10-22'),
    ('Our appointment next Tuesday', '2026-10-20'),
    ('Dinner next Friday at 6pm for 2 hours', '2026-10-23'),
])
def test_generic_confirmed_events_and_common_weekday_typos(text, day):
    got = plans([], [message('Hi', ts=MON-HOUR, direction='outgoing', guid='history'), message(text)])
    assert len(got) == 1 and got[0].day == day
    if '2 hours' in text:
        assert got[0].duration_min == 120 and got[0].start_ts is not None


@pytest.mark.parametrize('subject', [
    'STAT 5 Exam next Wednesday?', 'Maybe STAT 5 exam next Wednesday',
    'STAT 5 exam next Wednesday cancelled', 'No STAT 5 exam next Wednesday',
    'STAT 5 exam was next Wednesday', 'STAT 5 Exam next Wednesday — 50% off',
    'STAT 5 Exam next Wednesday newsletter', 'STAT 5 Exam next Wednesday if you want',
])
def test_unconfirmed_cancelled_past_negated_promotional_not_captured(subject):
    assert plans([mail(subject)]) == []


def test_unknown_email_sender_does_not_gain_trust_from_display_name():
    assert plans([mail(sender='Professor via Ed <offer@evil.example>')]) == []


def test_quoted_history_is_not_an_event_source():
    assert plans([mail('STAT 5 update', body='Thanks\nOn Monday, Professor wrote:\nExam next Wednesday')]) == []


def test_email_body_and_subject_can_supply_same_date_without_conflict():
    assert plans([mail(body='Our STAT 5 Exam 1 is next Wednesday.\nUnsubscribe from notifications')])[0].day == '2026-10-21'


def test_question_needs_real_affirmation_not_any_reply():
    incoming = message('Coffee chat next Thursday?')
    history = message('Hi', ts=MON-HOUR, direction='outgoing', guid='h')
    assert plans([], [history, incoming, message('Unrelated update', ts=MON+1, direction='outgoing', guid='reply')]) == []
    assert len(plans([], [history, incoming, message('Yes sounds good', ts=MON+1, direction='outgoing', guid='reply')])) == 1


def test_later_cancellation_stops_earlier_preview():
    assert plans([], [message('Hi', ts=MON-HOUR, direction='outgoing', guid='h'),
                      message('Coffee chat next Thursday'),
                      message('Never mind, cancelled', ts=MON+1, guid='cancel')]) == []


def test_existing_live_setting_does_not_opt_into_calendar(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('{"mode":"live"}')
    assert load_settings(path).capture_events is False
    save_settings(path, Settings(mode='live', capture_events=True))
    assert load_settings(path).capture_events is True
    path.write_text('{"mode":"live","capture_events":"true"}')
    assert load_settings(path).mode == 'off'


async def run_capture(c, ledger, *, result=None, fresh=None, current=lambda: True):
    calls = []
    async def effect(kind, candidate, **kwargs):
        calls.append((kind, candidate, kwargs))
        return (result or {}).get(kind, {'ok': True, 'status': 'succeeded', 'source_id': 'fake-'+kind})
    async def calendar(c, **kwargs):
        return await effect('calendar', c, **kwargs)
    async def reminder(c, **kwargs):
        return await effect('reminder', c, **kwargs)
    row = await capture(c, ledger=ledger, now=MON+10, settings=Settings(),
                        fresh_commitments=lambda: fresh or [], create_calendar=calendar,
                        create_reminder=reminder, automatic=True, still_current=current)
    return row, calls


async def test_pair_is_once_across_restart_resync_and_new_message(ledger):
    c = plans()[0]
    preview(c, ledger, MON+10)
    row, calls = await run_capture(c, ledger)
    assert row['state'] == 'created' and [k for k, *_ in calls] == ['calendar', 'reminder']
    reopened = Ledger(ledger._db.execute('PRAGMA database_list').fetchone()[2])
    again, calls = await run_capture(replace(c, source_id='mail:followup'), reopened)
    assert again['state'] == 'created' and calls == []
    assert ledger.capture_count_since(MON) == 1


@pytest.mark.parametrize('kind', ['calendar', 'reminder'])
async def test_partial_native_outcome_persists_and_never_blind_retries(ledger, kind):
    c = plans()[0]
    row, calls = await run_capture(c, ledger, result={kind: {'ok': False, 'status': 'unknown'}})
    assert row['state'] == 'unknown'
    assert row['detail']['effects'][kind]['status'] == 'unknown'
    again, calls = await run_capture(c, ledger)
    assert again['state'] == 'unknown' and calls == []


async def test_verified_failure_discloses_other_effect_success(ledger):
    row, _ = await run_capture(plans()[0], ledger, result={'reminder': {'ok': False, 'status': 'failed'}})
    assert row['state'] == 'partial'
    assert row['detail']['effects']['calendar']['status'] == 'succeeded'
    assert row['detail']['effects']['reminder']['status'] == 'failed'


async def test_start_only_event_gets_reminder_but_no_invented_calendar_duration(ledger):
    c = plans([mail('STAT 5 Exam next Wednesday at 3pm')])[0]
    row, calls = await run_capture(c, ledger)
    assert row['state'] == 'partial' and [k for k, *_ in calls] == ['reminder']
    assert row['detail']['effects']['calendar']['status'] == 'needs_duration'


@pytest.mark.parametrize('source,expected', [('calendar', 'reminder'), ('reminders', 'calendar')])
async def test_existing_destination_does_not_hide_missing_other_effect(ledger, source, expected):
    c = plans()[0]
    saved = {'id': 'existing', 'source': source, 'status': 'active', 'title': 'STAT 5 Exam 1',
             'when_ts': datetime(2026, 10, 21, 9, tzinfo=ZONE).timestamp()}
    row, calls = await run_capture(c, ledger, fresh=[saved])
    assert row['state'] == 'created' and [k for k, *_ in calls] == [expected]


async def test_claim_survives_crash_without_dispatching_again(ledger):
    c = plans()[0]
    ledger.claim(c.capture_key, now=MON-1000, detail={'effects': {'calendar': {'status': 'claimed'}}})
    ledger.recover(MON)
    row, calls = await run_capture(c, ledger)
    assert row['state'] == 'unknown' and calls == []


async def test_stale_source_refused_before_claim(ledger):
    with pytest.raises(ValueError, match='source changed'):
        await run_capture(plans()[0], ledger, current=lambda: False)
    assert ledger.get(plans()[0].capture_key) is None


async def test_source_invalidation_between_effects_prevents_second_write(ledger):
    checks = iter([True, True, False])
    row, calls = await run_capture(plans()[0], ledger, current=lambda: next(checks))
    assert row['state'] == 'partial' and [k for k, *_ in calls] == ['calendar']
    assert row['detail']['effects']['reminder']['status'] == 'blocked'


async def test_pair_daily_cap_counts_one_pair_not_its_preview_mirror(ledger):
    c = plans()[0]
    preview(c, ledger, MON+10)
    await run_capture(c, ledger)
    assert ledger.capture_count_since(MON) == 1
    assert ledger.count_since(MON) == 2  # ordinary recent rows include the display mirror


async def test_native_receipt_title_is_not_duplicated_in_capture_history(ledger):
    c = plans()[0]
    preview(c, ledger, MON+10)
    row, _ = await run_capture(c, ledger, result={'reminder': {
        'ok': True, 'status': 'succeeded', 'source_id': 'native:reminder',
        'title': 'NATIVE-RECEIPT-PRIVATE-TITLE', 'due_ts': c.due_ts}})
    assert 'title' not in row['detail']['effects']['reminder']
    ledger.purge_content(MON+8*86400, MON+10*86400)
    for sid in (c.capture_key, 'preview:' + c.source_id):
        saved = ledger.get(sid)
        assert saved['title'] is None and 'title' not in saved['detail'] and 'quote' not in saved['detail']


def test_refresh_preview_updates_displayed_fields_but_never_finished_effects(ledger):
    c = plans()[0]
    preview(c, ledger, MON+10)
    revised = replace(c, title='Revised exam', due_ts=c.due_ts+3600, when='Revised reminder time')
    row = preview(revised, ledger, MON+20)
    assert row['title'] == revised.title and row['due_ts'] == revised.due_ts
    assert row['detail']['review_fingerprint'] == revised.review_fingerprint
    ledger.finish(row['source_id'], 'created', row['detail'], MON+30)
    assert preview(c, ledger, MON+40)['title'] == revised.title


async def test_legacy_reminder_attempt_consumes_paired_daily_cap(ledger):
    ledger.claim('msg:legacy', now=MON+1)
    ledger.finish('msg:legacy', 'created', now=MON+1)
    c = plans()[0]
    calls = []
    async def effect(*args, **kwargs):
        calls.append(args)
        return {'ok': True}
    with pytest.raises(ValueError, match='Daily capture limit'):
        await capture(c, ledger=ledger, now=MON+10, settings=Settings(daily_cap=1),
            fresh_commitments=lambda: [], create_calendar=effect, create_reminder=effect, automatic=True)
    assert calls == []


@pytest.mark.parametrize('cap,expected_calls', [(1, 0), (2, 1)])
async def test_pair_and_display_mirror_consume_one_legacy_cap_slot(ledger, cap, expected_calls):
    from service.attention.runner import process
    for source_id in ['capture:prior', 'preview:prior']:
        ledger.claim(source_id, now=MON+1)
        ledger.finish(source_id, 'created', now=MON+1)
    ledger.mark_live(MON-HOUR)
    calls = []
    async def effect(plan):
        calls.append(plan)
        return {'ok': True, 'status': 'succeeded'}
    async def publish(event):
        pass
    rows = [message('Hi', ts=MON-HOUR, direction='outgoing', guid='contact'),
            message('Meeting today at 3pm', ts=MON+2, guid='new-plan')]
    outcomes = await process(records=rows, commitments=[], now=MON+10, tz=TZ,
        settings=Settings(mode='live', daily_cap=cap), ledger=ledger,
        create=effect, publish=publish, fresh_commitments=lambda: [])
    assert len(calls) == expected_calls
    assert outcomes and outcomes[0].state == ('created' if expected_calls else 'capped')


@pytest.fixture
def wired(tmp_path, monkeypatch):
    import sys
    from service.assistant import attention_runner as wiring
    from service.assistant import attention_api
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    wiring.reset()
    monkeypatch.setattr(wiring, 'MOE_DIR', tmp_path)
    monkeypatch.setattr(attention_api, 'MOE_DIR', tmp_path)
    monkeypatch.setattr(wiring, 'current_zone', lambda: TZ)
    clock = SimpleNamespace(value=MON+10)
    monkeypatch.setattr(wiring, 'time', SimpleNamespace(time=lambda: clock.value))
    monkeypatch.setattr(attention_api, 'time', SimpleNamespace(time=lambda: clock.value))
    sources = {'records': [], 'rows': [mail()]}
    monkeypatch.setattr(wiring, 'capture_sources', lambda: (sources['records'], sources['rows'], {}, None))
    monkeypatch.setattr(wiring, '_capture_commitments', lambda *args: [])
    writes, approvals = [], []
    native_reminder = wiring._capture_reminder
    async def calendar(plan, *, approval=None, source_current=None):
        writes.append(('calendar', plan, approval))
        return {'ok': True, 'status': 'succeeded', 'source_id': 'fake-calendar'}
    def approve(plan):
        token = object()
        approvals.append((plan, token))
        return token
    async def reminder(c, *, automatic):
        writes.append(('reminder', c, automatic))
        return {'ok': True, 'status': 'succeeded'}
    monkeypatch.setattr(wiring, '_capture_reminder', reminder)
    monkeypatch.setitem(sys.modules, 'service.assistant.attention_calendar',
                        SimpleNamespace(create_calendar=calendar, approve_capture=approve))
    app = FastAPI()
    app.include_router(attention_api.router)
    yield SimpleNamespace(wiring=wiring, client=TestClient(app), clock=clock, sources=sources,
                          writes=writes, approvals=approvals, native_reminder=native_reminder, settings=tmp_path / 'attention/settings.json')
    wiring.reset()


async def test_automatic_enable_does_not_replay_existing_exam_but_explicit_add_can(wired):
    w = wired
    await w.wiring.run_capture_tick(w.clock.value, TZ, Settings())
    records = w.client.get('/assistant/attention').json()['recent']
    assert len(records) == 1 and records[0]['captureable'] is True
    assert w.client.put('/assistant/attention/settings', json={'mode': 'live', 'capture_events': True}).status_code == 200
    w.clock.value += 300
    await w.wiring.run_capture_tick(w.clock.value, TZ, load_settings(w.settings))
    assert w.writes == []
    reviewed = w.client.post('/assistant/attention/capture', json={
        'source_id': records[0]['source_id'], 'fingerprint': records[0]['fingerprint']})
    assert reviewed.status_code == 200 and reviewed.json()['state'] == 'created'
    assert len(w.approvals) == 1 and w.writes[0][2] is w.approvals[0][1]
    assert w.writes[0][1]['automatic'] is False
    status = w.client.get('/assistant/attention').json()
    assert len(status['recent']) == 1 and status['created_today'] == 1
    assert status['recent'][0]['undoable'] is False  # no paired deletion by fuzzy identity


async def test_explicit_add_refuses_source_changed_after_review(wired):
    w = wired
    await w.wiring.run_capture_tick(w.clock.value, TZ, Settings())
    record = w.client.get('/assistant/attention').json()['recent'][0]
    w.sources['rows'] = [mail(body='Exam cancelled')]
    response = w.client.post('/assistant/attention/capture', json={
        'source_id': record['source_id'], 'fingerprint': record['fingerprint']})
    assert response.status_code == 409 and not w.writes and not w.approvals


async def test_expired_displayed_reminder_requires_updated_exact_preview(wired):
    w = wired
    await w.wiring.run_capture_tick(w.clock.value, TZ, Settings())
    old = w.client.get('/assistant/attention').json()['recent'][0]
    w.clock.value = datetime(2026, 10, 21, 9, 5, tzinfo=ZONE).timestamp()
    response = w.client.post('/assistant/attention/capture', json={
        'source_id': old['source_id'], 'fingerprint': old['fingerprint']})
    assert response.status_code == 409 and not w.writes and not w.approvals
    fresh = w.client.get('/assistant/attention').json()['recent'][0]
    assert fresh['fingerprint'] != old['fingerprint'] and fresh['due_ts'] > w.clock.value
    assert fresh['captureable']
    added = w.client.post('/assistant/attention/capture', json={
        'source_id': fresh['source_id'], 'fingerprint': fresh['fingerprint']})
    assert added.status_code == 200 and added.json()['state'] == 'created'


async def test_old_reminder_live_optin_never_writes_calendar(wired):
    w = wired
    save_settings(w.settings, Settings(mode='live'))
    await w.wiring.run_capture_tick(w.clock.value, TZ, load_settings(w.settings))
    w.clock.value += 300
    w.sources['rows'] = [mail(message_id='<new@example.test>', ts=w.clock.value-1)]
    await w.wiring.run_capture_tick(w.clock.value, TZ, load_settings(w.settings))
    assert w.writes == []


async def test_new_source_after_automatic_pair_optin_captures_without_explicit_token(wired):
    w = wired
    w.client.put('/assistant/attention/settings', json={'mode': 'live', 'capture_events': True})
    w.clock.value += 300
    w.sources['rows'] = [mail(message_id='<new@example.test>', ts=w.clock.value-1)]
    await w.wiring.run_capture_tick(w.clock.value, TZ, load_settings(w.settings))
    assert [item[0] for item in w.writes] == ['calendar', 'reminder']
    assert w.writes[0][1]['automatic'] is True and w.writes[0][2] is None


async def test_source_unavailable_at_capture_time_blocks_exact_preview(wired):
    w = wired
    await w.wiring.run_capture_tick(w.clock.value, TZ, Settings())
    record = w.client.get('/assistant/attention').json()['recent'][0]
    w.sources['rows'] = []
    response = w.client.post('/assistant/attention/capture', json={
        'source_id': record['source_id'], 'fingerprint': record['fingerprint']})
    assert response.status_code == 409 and w.writes == []


async def test_settings_change_invalidates_reviewed_reminder_default(wired):
    w = wired
    await w.wiring.run_capture_tick(w.clock.value, TZ, Settings())
    record = w.client.get('/assistant/attention').json()['recent'][0]
    w.client.put('/assistant/attention/settings', json={'default_hour': 8})
    response = w.client.post('/assistant/attention/capture', json={
        'source_id': record['source_id'], 'fingerprint': record['fingerprint']})
    assert response.status_code == 409 and not w.writes


async def test_preview_zone_and_relative_date_stay_pinned_after_travel(wired, monkeypatch):
    w = wired
    w.sources['rows'] = [mail(ts=datetime(2026, 10, 19, 23, 30, tzinfo=ZONE).timestamp(), subject='STAT 5 Exam tomorrow')]
    w.clock.value = w.sources['rows'][0]['ts'] + 1
    await w.wiring.run_capture_tick(w.clock.value, TZ, Settings())
    first = w.wiring.capture_candidates(w.clock.value, TZ, Settings())[0]
    monkeypatch.setattr(w.wiring, 'current_zone', lambda: 'Asia/Tokyo')
    after = w.wiring.capture_candidates(w.clock.value+30, 'Asia/Tokyo', Settings())[0]
    assert after.day == first.day == '2026-10-20' and after.timezone == TZ
    assert after.source_fingerprint == first.source_fingerprint


@pytest.mark.parametrize('text', [
    'I have an appointment next Wednesday', 'My brother has an exam next Wednesday',
    'Her exam next Wednesday', "I'm meeting Sam next Thursday", "John's interview next Thursday",
])
def test_sender_and_third_party_events_are_not_owned_commitments(text):
    assert plans([], [message('Hi', ts=MON-HOUR, direction='outgoing', guid='h'), message(text)]) == []


def test_explicit_joint_participation_qualifies():
    got = plans([], [message('Hi', ts=MON-HOUR, direction='outgoing', guid='h'),
                     message('I have our coffee chat next Thursday')])
    assert len(got) == 1 and got[0].automatic_eligible


@pytest.mark.parametrize('text', [
    'We are considering coffee next Thursday', 'We are thinking about coffee next Thursday',
    'We are hoping for lunch next Thursday', 'Coffee next Thursday if the weather is good',
    'We will not meet next Friday', 'We are not having dinner next Friday',
    "We don't meet next Friday", "We aren't having dinner next Friday",
    "Our coffee isn't happening next Thursday", "Dinner next Friday isn't confirmed",
    'We don’t meet next Friday', 'We aren’t having dinner next Friday',
    'Our coffee isn’t happening next Thursday', 'Dinner next Friday isn’t confirmed',
])
def test_event_nouns_never_override_uncertainty_or_negation(text):
    assert plans([], [message('Hi', ts=MON-HOUR, direction='outgoing', guid='h'), message(text)]) == []


@pytest.mark.parametrize('text', ['Do not forget our exam next Wednesday',
                                 "Don't forget our coffee chat next Thursday",
                                 'Don’t forget our coffee chat next Thursday',
                                 'Our dinner next Friday', 'See you next Friday'])
def test_positive_confirmed_plan_controls_survive_negation_guard(text):
    assert plans([], [message('Hi', ts=MON-HOUR, direction='outgoing', guid='h'), message(text)])


def test_generic_institutional_event_needs_review_of_participation():
    got = plans([mail('School seminar next Wednesday', sender='Office <office@school.edu>')])
    assert len(got) == 1 and got[0].automatic_eligible is False
    assert 'review participation' in got[0].when


async def test_revoking_auto_capture_between_effects_blocks_reminder(wired, monkeypatch):
    w = wired
    w.client.put('/assistant/attention/settings', json={'mode': 'live', 'capture_events': True})
    w.clock.value += 300
    w.sources['rows'] = [mail(message_id='<fresh@example.test>', ts=w.clock.value-1)]
    async def revoke(c, **kwargs):
        w.writes.append(('calendar', c, kwargs))
        save_settings(w.settings, Settings(mode='live', capture_events=False))
        return {'ok': True, 'status': 'succeeded'}
    monkeypatch.setattr(w.wiring, '_capture_calendar', revoke)
    await w.wiring.run_capture_tick(w.clock.value, TZ, Settings(mode='live', capture_events=True))
    assert [item[0] for item in w.writes] == ['calendar']
    row = w.wiring.get_ledger().recent(10)[0]
    assert row['detail']['effects']['reminder']['status'] == 'blocked'


def test_purge_removes_nested_pair_text_errors_but_keeps_dedup_and_identity(ledger):
    c = plans()[0]
    for sid in (c.capture_key, 'preview:' + c.source_id):
        ledger.claim(sid, title='private exam title', due_ts=MON-10, now=MON-8*86400)
        ledger.finish(sid, 'partial', {'title': 'private exam title', 'quote': 'private source words',
            'day': c.day, 'scope': c.scope, 'effects': {
                'calendar': {'status': 'succeeded', 'source_id': 'native-fake'},
                'reminder': {'status': 'failed', 'error': 'private source words'}}}, MON-8*86400)
    assert ledger.purge_content(MON-7*86400, MON) == 2
    for sid in (c.capture_key, 'preview:' + c.source_id):
        row = ledger.get(sid)
        assert row['title'] is None and 'title' not in row['detail'] and 'quote' not in row['detail']
        assert 'error' not in row['detail']['effects']['reminder']
        assert row['detail']['effects']['calendar']['source_id'] == 'native-fake'
        assert row['detail']['scope'] == c.scope
    assert ledger.purge_content(MON-7*86400, MON) == 0


def test_explicit_clock_range_supplies_exact_duration_without_defaults():
    got = plans([mail('STAT 5 Exam next Wednesday from 3pm to 4:30pm')])
    assert len(got) == 1 and got[0].duration_min == 90
    assert datetime.fromtimestamp(got[0].start_ts, ZONE).hour == 15


def test_conflicting_duration_and_clock_range_are_not_auto_captured():
    assert plans([mail('STAT 5 Exam next Wednesday 3pm to 4pm for 2 hours')]) == []


def test_source_adapter_never_uses_unadmitted_email_or_restored_messages(monkeypatch):
    from service.assistant import attention_runner as wiring
    from service.tools import email_tools, imessage_tools
    monkeypatch.setattr(email_tools, 'raw_email_snapshot', lambda: None)
    monkeypatch.setattr(imessage_tools, 'structured_messages_snapshot', lambda: {
        'state': 'loading', 'records': [message('Our coffee chat next Thursday')]})
    records, rows, _, _ = wiring.capture_sources()
    assert records == [] and rows == []


async def test_status_hides_add_for_no_longer_admitted_source(wired):
    await wired.wiring.run_capture_tick(wired.clock.value, TZ, Settings())
    assert wired.client.get('/assistant/attention').json()['recent'][0]['captureable']
    wired.sources['rows'] = []
    assert wired.client.get('/assistant/attention').json()['recent'][0]['captureable'] is False


@pytest.mark.parametrize('text', ["We're volunteering next Saturday", 'We will go hiking next Friday', "Let's play badminton next Thursday"])
def test_clear_joint_commitment_does_not_require_predefined_event_noun(text):
    got = plans([], [message('Hi', ts=MON-HOUR, direction='outgoing', guid='h'), message(text)])
    assert len(got) == 1 and got[0].automatic_eligible


@pytest.mark.parametrize('text', ["We are not volunteering next Saturday", 'We are considering hiking next Friday', 'Our package arrives tomorrow'])
def test_negated_or_speculative_generic_activity_is_not_confirmed(text):
    assert plans([], [message('Hi', ts=MON-HOUR, direction='outgoing', guid='h'), message(text)]) == []


@pytest.mark.parametrize('text', ['I have an appointment next Wednesday. Your notes look good',
                                 'FYI your brother has an exam next Wednesday',
                                 'We are catching up. I have an appointment next Wednesday'])
def test_unrelated_participation_and_third_party_role_never_authorize_event(text):
    assert plans([], [message('Hi', ts=MON-HOUR, direction='outgoing', guid='h'), message(text)]) == []


async def test_distinct_explicit_times_same_course_same_day_are_separate_events(ledger):
    a = plans([mail('STAT 5 exam next Wednesday at 10am for 1 hour')])[0]
    b = plans([mail('STAT 5 exam next Wednesday at 3pm for 1 hour', message_id='<other@example.test>')])[0]
    first, _ = await run_capture(a, ledger)
    second, calls = await run_capture(b, ledger)
    assert first['source_id'] != second['source_id'] and len(calls) == 2


async def test_calendar_wiring_supplies_fresh_source_callback_at_dispatch(wired, monkeypatch):
    import sys
    w = wired
    candidate = w.wiring.capture_candidates(w.clock.value, TZ, Settings())[0]
    async def reconnect_then_check(plan, *, approval, source_current):
        assert source_current() is True
        w.sources['rows'] = []  # source publication becomes unavailable while reconnect waits
        assert source_current() is False
        return {'ok': False, 'status': 'blocked'}
    monkeypatch.setitem(sys.modules, 'service.assistant.attention_calendar',
                        SimpleNamespace(create_calendar=reconnect_then_check))
    got = await w.wiring._capture_calendar(candidate, automatic=False)
    assert got['status'] == 'blocked'


async def test_reminder_rechecks_source_after_reconnect_and_has_no_local_fallback(wired, monkeypatch):
    from service.assistant import outbox
    w = wired
    candidate = w.wiring.capture_candidates(w.clock.value, TZ, Settings())[0]
    attempted = []
    async def reconnect_then_check(kind, payload, *, before_dispatch):
        assert kind == 'create_reminder' and payload['due_ts'] == candidate.due_ts
        assert before_dispatch() is None
        w.sources['rows'] = []
        error = before_dispatch()
        assert error is not None
        attempted.append(False)
        return {'ok': False, 'status': 'blocked', 'error': error}
    monkeypatch.setattr(outbox, 'request', reconnect_then_check)
    got = await w.native_reminder(candidate, automatic=False)
    assert got['status'] == 'blocked' and attempted == [False]


async def test_native_reminder_success_is_preserved_when_refresh_publish_fails(wired, monkeypatch):
    from service.assistant import outbox
    w = wired
    candidate = w.wiring.capture_candidates(w.clock.value, TZ, Settings())[0]
    async def native(*args, **kwargs):
        return {'ok': True, 'status': 'succeeded', 'source_id': 'fake-reminder'}
    async def broken(*args, **kwargs):
        raise RuntimeError('refresh only')
    monkeypatch.setattr(outbox, 'request', native)
    monkeypatch.setattr(w.wiring.hub, 'publish', broken)
    assert (await w.native_reminder(candidate, automatic=False))['status'] == 'succeeded'


@pytest.mark.parametrize('text', [
    'Our coffee next Thursday at 3pm. I practiced for 2 hours',
    'I practiced for 2 hours. Our coffee next Thursday at 3pm',
    'Our dinner next Friday at 6pm. The commute takes for 2 hours',
    'The commute takes for 2 hours. Our dinner next Friday at 6pm',
    'Our dinner next Friday at 6pm, and the commute takes for 2 hours',
    'Our dinner next Friday at 6pm and the commute takes for 2 hours',
    'Our dinner next Friday at 6pm, the commute takes for 2 hours',
    'Our dinner next Friday at 6pm the commute takes for 2 hours',
    'Our exam next Wednesday at 3pm, and practice lasts for 2 hours',
    'Practice lasts for 2 hours; our exam next Wednesday at 3pm',
])
def test_unrelated_duration_never_supplies_event_length(text):
    got = plans([], [message('Hi', ts=MON-HOUR, direction='outgoing', guid='h'), message(text)])
    assert len(got) == 1
    assert got[0].start_ts is not None and got[0].duration_min is None
    assert 'needs an end time' in got[0].when


@pytest.mark.parametrize('text', [
    'Our exam next Wednesday. Practice starts at 3pm for 2 hours',
    'Practice starts at 3pm for 2 hours. Our exam next Wednesday',
    'Our exam next Wednesday. Our meeting today is at 3pm for 1 hour',
    'Our meeting today is at 3pm for 1 hour. Our exam next Wednesday',
    'Our coffee next Thursday. Meeting today is 3pm to 4pm',
    'Meeting today is 3pm to 4pm. Our coffee next Thursday',
    'Our exam next Wednesday, but practice is at 3pm for 2 hours',
    'Our exam next Wednesday and practice starts at 3pm for 2 hours',
    'Our exam next Wednesday; practice starts at 3pm for 2 hours',
    'Our exam next Wednesday. Practice is 3pm to 4pm',
    'Practice is 3pm to 4pm. Our exam next Wednesday',
])
def test_distinct_event_clauses_never_mix_date_clock_or_range(text):
    assert plans([], [message('Hi', ts=MON-HOUR, direction='outgoing', guid='h'), message(text)]) == []


@pytest.mark.parametrize('body', [
    'My commute starts at 3pm for 2 hours',
    'My commute is 3pm to 4pm',
    'My commute took for 2 hours',
])
def test_school_date_only_subject_does_not_borrow_commute_time_or_duration(body):
    got = plans([mail('STAT 5 Exam next Wednesday', body=body)])
    assert len(got) == 1 and got[0].day == '2026-10-21'
    assert got[0].start_ts is None and got[0].duration_min is None
    assert got[0].calendar_plan(automatic=True)['all_day'] is True


@pytest.mark.parametrize('body', ['Practice is 3pm to 4pm', 'Practice starts at 3pm for 2 hours'])
def test_school_subject_and_separately_timed_practice_are_ambiguous(body):
    assert plans([mail('STAT 5 Exam next Wednesday', body=body)]) == []


@pytest.mark.parametrize('subject,body', [
    ('My STAT 5 exam next Wednesday', ''),
    ('STAT 5 exam next Wednesday', 'My brother has his exam next Wednesday.'),
    ('STAT 5 exam next Wednesday', 'Your brother has an exam next Wednesday.'),
    ('STAT 5 exam next Wednesday', 'I have my exam next Wednesday.'),
])
def test_institutional_domain_never_overrides_explicit_other_event_owner(subject, body):
    assert plans([mail(subject, sender='Classmate <friend@school.edu>', body=body)]) == []


def test_personalized_institutional_exam_remains_eligible():
    got = plans([mail('STAT 5 exam next Wednesday', body='Your STAT 5 exam is next Wednesday')])
    assert len(got) == 1 and got[0].automatic_eligible
    assert got[0].start_ts is None and got[0].duration_min is None


@pytest.mark.parametrize('text,minutes', [
    ('Our coffee next Thursday at 3pm for 2 hours', 120),
    ('Our coffee next Thursday for 2 hours at 3pm', 120),
    ('Our coffee for 2 hours next Thursday at 3pm', 120),
    ('Our coffee next Thursday at 3pm, for 2 hours', 120),
    ('Our coffee next Thursday from 3pm to 4:30pm', 90),
    ('Our coffee next Thursday from 3pm to 4:30pm for 90 minutes', 90),
])
def test_event_local_duration_and_range_controls_stay_exact(text, minutes):
    got = plans([], [message('Hi', ts=MON-HOUR, direction='outgoing', guid='h'), message(text)])
    assert len(got) == 1 and got[0].duration_min == minutes
    assert datetime.fromtimestamp(got[0].start_ts, ZONE).hour == 15


def test_date_only_event_never_invents_clock_from_a_duration():
    got = plans([mail('STAT 5 Exam next Wednesday for 2 hours')])
    assert len(got) == 1 and got[0].start_ts is None and got[0].duration_min is None


@pytest.mark.parametrize('text', [
    'Our coffee next Thursday at 3pm. I ran from 4pm to 6pm',
    'I ran from 4pm to 6pm. Our coffee next Thursday at 3pm',
    'Our coffee next Thursday at 3pm; the commute is 4pm to 6pm',
    'The commute is 4pm to 6pm; our coffee next Thursday at 3pm',
    'Our coffee next Thursday at 3pm and the commute is 4pm to 6pm',
])
def test_unrelated_clock_range_never_supplies_event_interval(text):
    got = plans([], [message('Hi', ts=MON-HOUR, direction='outgoing', guid='h'), message(text)])
    assert len(got) == 1 and got[0].duration_min is None
    assert datetime.fromtimestamp(got[0].start_ts, ZONE).hour == 15
    assert 'commute' not in got[0].quote and 'I ran' not in got[0].quote


def test_two_matching_exam_clauses_keep_single_date_only_schedule():
    got = plans([mail('STAT 5 Exam next Wednesday', body='Your STAT 5 exam is next Wednesday.')])
    assert len(got) == 1 and got[0].start_ts is None


def test_unsupported_compound_duration_never_shortens_event():
    assert plans([mail('STAT 5 exam next Wednesday at 3pm for 1 hour and 30 minutes')]) == []


@pytest.mark.parametrize('text', [
    'Our exam next Wednesday commute starts at 3pm for 2 hours',
    'Our exam next Wednesday commute is 3pm to 4pm',
    'Our exam next Wednesday practice is 3pm to 4pm',
    'Our exam next Wednesday meeting today starts at 3pm for 1 hour',
])
def test_missing_punctuation_does_not_link_another_activity_clock(text):
    assert plans([], [message('Hi', ts=MON-HOUR, direction='outgoing', guid='h'), message(text)]) == []
