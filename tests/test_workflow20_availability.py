"""Coherent availability from fictional Calendar snapshots; no native/model effects."""
import asyncio
import copy
import datetime as dt
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from service.tools import schedule_extras as S
from service.tools.registry import REGISTRY, classify_tool_outcome, run_tool

ZONE = 'America/Los_Angeles'

def stamp(value):
    return dt.datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(ZONE)).timestamp()


def event(start='2026-10-11T10:30', end='2026-10-11T12:00', **extra):
    return dict(id='cal.brunch', source_id='native.brunch', source='calendar',
                title='Brunch', status='active', when_ts=stamp(start),
                end_ts=stamp(end) if end else None, all_day=False, **extra)


@pytest.fixture
def source(monkeypatch):
    state = SimpleNamespace(now=dt.datetime(2026, 10, 10, 9, tzinfo=ZoneInfo(ZONE)),
                            rows=[], tasks=[], reads=[], transform=None)
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return state.now.astimezone(tz) if tz else state.now.replace(tzinfo=None)
    monkeypatch.setattr(S, 'dt', SimpleNamespace(datetime=Clock, time=dt.time, timedelta=dt.timedelta))
    monkeypatch.setattr(S, '_availability_timezone', lambda: ZONE)
    state.sync = {'available': True, 'syncing': False, 'last_sync': state.now.timestamp(),
                  'snapshot_started_at': state.now.timestamp(), 'diagnostics': {
                      'coverage_start': stamp('2026-10-01T00:00'),
                      'coverage_end': stamp('2026-12-01T00:00')}}
    def snapshot(day, zone):
        assert zone == ZONE
        state.reads.append(day)
        lo = stamp(day + 'T00:00')
        hi = (dt.datetime.fromisoformat(day) + dt.timedelta(days=1)).replace(tzinfo=ZoneInfo(zone)).timestamp()
        rows = [r for r in state.rows if not isinstance(r.get('when_ts'), (int,float)) or not math.isfinite(r['when_ts']) or
                (r['when_ts'] < hi and (r.get('end_ts') is None or
                not isinstance(r['end_ts'], (int,float)) or r['end_ts'] > lo or r['when_ts'] >= lo))]
        result = copy.deepcopy({'sources': {'calendar': state.sync}, 'commitments': rows, 'tasks': state.tasks})
        return state.transform(day, result) if state.transform else result
    monkeypatch.setattr(S, '_store', SimpleNamespace(today_snapshot=snapshot))
    state.ensure = AsyncMock(return_value={'sources': [{'id':'calendar','state':'ready'},
                {'id':'reminders','state':'ready'}], 'reminders_fresh':True})
    monkeypatch.setattr('service.assistant.sync_status.ensure_sources', state.ensure)
    async def forbidden(*args, **kwargs):
        raise AssertionError('Availability must never perform a native effect')
    monkeypatch.setattr('service.assistant.outbox.request', forbidden)
    return state


def invoke(legacy=False, **args):
    if legacy:
        return S.find_free_time(days=2, **args)
    return asyncio.run(S.find_free_time(period='tomorrow', **args))


@pytest.mark.parametrize('legacy', [False, True])
def test_real_long_event_end_replaces_one_hour_assumption(source, legacy):
    source.rows = [event(end='2026-10-11T16:00')]
    result = invoke(legacy, minutes=60)
    assert '4:00 PM–' in result and '11:30 AM–' not in result
    assert 'assumed' not in result
    assert 'Reminders without durations are deadlines' in result
    assert 'other people' in result and 'not been created' in result
    assert source.reads == (['2026-10-10','2026-10-11'] if legacy else ['2026-10-11'])
    if legacy:
        source.ensure.assert_not_awaited()
    else:
        source.ensure.assert_awaited_once_with(('calendar','reminders'))


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('change', [
    {'end_ts':None}, {'end_ts':float('nan')}, {'end_ts':float('inf')},
    {'end_ts':stamp('2026-10-11T10:00')}, {'when_ts':None},
    {'when_ts':float('nan')}, {'all_day':True,'end_ts':None},
])
def test_unknown_invalid_intervals_never_claim_free(source, legacy, change):
    row = event(); row.update(change); source.rows=[row]
    result = invoke(legacy)
    assert result.startswith('(error:')
    assert 'candidate slots' not in result and 'Free for' not in result
    assert classify_tool_outcome('find_free_time', result).status != 'succeeded'


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('change', [
    {'available':False}, {'syncing':True}, {'last_sync':0},
    {'last_sync':stamp('2026-10-10T09:01')}, {'complete':False}, {'partial':True},
    {'diagnostics':{}}, {'diagnostics':{'coverage_start':0,'coverage_end':1}},
])
def test_bad_receipt_withholds_every_day(source, legacy, change):
    source.sync.update(change)
    assert invoke(legacy).startswith('(error:')


@pytest.mark.parametrize('legacy', [False, True])
def test_no_calendar_receipt_is_not_empty_calendar(source, legacy):
    source.transform=lambda day, result: {**result,'sources':{}}
    assert invoke(legacy).startswith('(error:')


@pytest.mark.parametrize('field', ['snapshot_started_at','last_sync','generation'])
def test_multiday_sync_generation_mismatch_even_with_unique_events(source, field):
    def transform(day, result):
        if day == '2026-10-11':
            result['sources']['calendar'][field] = 7
            result['commitments']=[{**event(),'id':'unique.new.event'}]
        return result
    source.transform=transform
    result=S.find_free_time(days=2)
    assert result.startswith('(error:')
    assert 'Free for' not in result


def test_same_receipt_cannot_hide_mutated_duplicate_occurrence(source):
    source.rows=[event(start='2026-10-10T22:00',end='2026-10-11T12:00')]
    def transform(day, result):
        if day == '2026-10-11':
            result['commitments'][0]['end_ts'] += 3600
        return result
    source.transform=transform
    assert 'changed between snapshots' in S.find_free_time(days=2)


def test_overnight_event_clips_to_real_end(source):
    source.rows=[event(start='2026-10-10T22:00',end='2026-10-11T12:00')]
    result=invoke(minutes=60)
    assert '12:00 PM–1:00 PM' in result
    assert '9:00 AM–' not in result


def test_all_day_multiday_event_blocks_every_covered_day(source):
    row=event(start='2026-10-10T00:00',end='2026-10-12T00:00'); row['all_day']=True
    source.rows=[row]
    assert 'No gaps' in S.find_free_time(days=2)


@pytest.mark.parametrize('legacy', [False, True])
def test_a_holiday_calendar_all_day_entry_is_not_busy(source, legacy):
    """2026-10-29 Ask run: Halloween and Election Day from "US Holidays" left whole
    days with no gaps. Holidays calendars are skipped; the user's own all-day
    events still block (above)."""
    row=event(start='2026-10-10T00:00',end='2026-10-12T00:00', context='US Holidays'); row['all_day']=True
    row['calendar_writable']=False
    source.rows=[row]
    assert 'No gaps' not in invoke(legacy)
    mine=event(start='2026-10-10T00:00',end='2026-10-12T00:00', context='Work'); mine['all_day']=True
    source.rows=[mine]
    assert 'No gaps' in invoke(legacy)


@pytest.mark.parametrize('status,blocked', [('cancelled',False),('declined',False),('done',False),('tentative',True),('active',True)])
def test_known_local_status_and_unknown_response_semantics(source,status,blocked):
    row=event(start='2026-10-11T09:00',end='2026-10-11T18:00'); row['status']=status
    source.rows=[row]
    result=invoke()
    assert ('No gaps' in result) is blocked
    assert 'response-unknown' in result


def test_duplicate_title_distinct_ids_are_not_deduped(source):
    source.rows=[event(end='2026-10-11T13:00'), {**event(start='2026-10-11T13:00',end='2026-10-11T16:00'),'id':'cal.other'}]
    assert '4:00 PM–' in invoke()


def test_missing_identity_fails_closed(source):
    row=event();row.pop('id');source.rows=[row]
    assert 'identity is missing' in invoke()


def test_due_only_reminders_do_not_invent_busy_hour(source):
    source.rows=[{**event(start='2026-10-11T09:00',end=None),'source':'reminders','kind':'reminder'}]
    assert '9:00 AM–9:30 AM' in invoke()


def test_manual_event_requires_real_end(source):
    source.rows=[{**event(end=None),'source':'manual','kind':'event'}]
    assert 'missing or invalid ends' in invoke()


def test_pinned_tasks_block_and_unpinned_do_not(source):
    source.tasks=[{'status':'active','pinned_start':stamp('2026-10-11T09:00'),'duration_minutes':120},
                  {'status':'active','pinned_start':None,'duration_minutes':60}]
    assert '11:00 AM–11:30 AM' in invoke()


@pytest.mark.parametrize('duration',[None,True,0,-1,1441,float('nan')])
def test_invalid_pinned_duration_is_not_free_time(source,duration):
    source.tasks=[{'pinned_start':stamp('2026-10-11T09:00'),'duration_minutes':duration}]
    assert invoke().startswith('(error:')


def test_async_refresh_clock_and_midnight_target_are_recomputed(source):
    source.now=dt.datetime(2026,10,10,23,59,tzinfo=ZoneInfo(ZONE))
    async def refresh(*args):
        source.now=dt.datetime(2026,10,11,0,1,tzinfo=ZoneInfo(ZONE))
        source.sync['last_sync']=source.now.timestamp()
        return {'sources':[{'id':'calendar','state':'ready'}]}
    source.ensure.side_effect=refresh
    result=invoke()
    assert source.reads==['2026-10-12']
    assert 'Monday, 2026-10-12' in result and not result.startswith('(error:')


def test_exact_minimum_and_half_open_touching_bounds(source):
    source.rows=[event(start='2026-10-11T09:00',end='2026-10-11T17:15')]
    assert '5:15 PM–6:00 PM' in invoke(minutes=45)
    source.rows[0]['end_ts'] += 1
    assert 'No gaps' in invoke(minutes=45)


@pytest.mark.parametrize('month,day,start_offset,end_offset,hours',[(10,31,'-07:00','-08:00',25),(3,7,'-08:00','-07:00',23)])
def test_dst_elapsed_day_and_offset_labels(source,month,day,start_offset,end_offset,hours):
    source.now=dt.datetime(2026,month,day,9,tzinfo=ZoneInfo(ZONE))
    source.sync['last_sync']=source.now.timestamp()
    source.sync['diagnostics']={'coverage_start':stamp('2026-01-01T00:00'),'coverage_end':stamp('2027-01-01T00:00')}
    result=invoke(minutes=hours*60 if hours<=24 else 1440,day_start=0,day_end=24)
    if hours==23:
        assert 'No gaps' not in result
    else:
        assert f'00:00:00{start_offset}' in result and f'00:00:00{end_offset}' in result
    assert 'America/Los_Angeles' in result


def test_dst_ambiguous_working_boundary_rejected(source):
    source.now=dt.datetime(2026,10,31,9,tzinfo=ZoneInfo(ZONE));source.sync['last_sync']=source.now.timestamp()
    assert 'repeated hour' in invoke(day_start=1,day_end=3)


def test_invalid_period_no_refresh_and_past_day_refused(source):
    for period in ['yesterday','2026-10-09','2026-99-01','tomorrow afternoon']:
        assert asyncio.run(S.find_free_time(period=period)).startswith('(error:')
    source.ensure.assert_not_awaited()


def test_registered_default_and_period_share_receipt_guard(source):
    for args in [{'days':2},{'period':'tomorrow'}]:
        result=asyncio.run(run_tool(REGISTRY['find_free_time'],args))
        assert classify_tool_outcome('find_free_time',result).status=='succeeded'
    source.sync['last_sync']=0
    for args in [{'days':2},{'period':'tomorrow'}]:
        result=asyncio.run(run_tool(REGISTRY['find_free_time'],args))
        assert classify_tool_outcome('find_free_time',result).status!='succeeded'


@pytest.mark.parametrize('field,value',[('available','yes'),('last_sync',True),('syncing','false')])
def test_malformed_readiness_is_not_freshness(source,field,value):
    source.sync[field]=value
    assert invoke().startswith('(error:')


def test_unknown_event_status_is_not_free(source):
    row=event();row['status']='unrecognized';source.rows=[row]
    assert 'unknown status' in invoke()


@pytest.mark.parametrize('month,day,hours',[(10,31,25),(3,7,23)])
def test_real_all_day_dst_span_blocks_entire_day(source,month,day,hours):
    source.now=dt.datetime(2026,month,day,9,tzinfo=ZoneInfo(ZONE))
    source.sync['last_sync']=source.now.timestamp()
    source.sync['diagnostics']={'coverage_start':stamp('2026-01-01T00:00'),'coverage_end':stamp('2027-01-01T00:00')}
    first=source.now.date()+dt.timedelta(days=1)
    last=first+dt.timedelta(days=1)
    row=event(start=first.isoformat()+'T00:00',end=last.isoformat()+'T00:00');row['all_day']=True
    assert row['end_ts']-row['when_ts']==hours*3600
    source.rows=[row]
    assert 'No gaps' in invoke(day_start=0,day_end=24)


@pytest.mark.parametrize('zone,month,day',[('UTC',10,10),('Pacific/Kiritimati',10,10),
                                         (ZONE,3,7),(ZONE,10,31)])
def test_real_store_fake_sync_receipt_and_zone_boundaries(source,tmp_path,monkeypatch,zone,month,day):
    from service.assistant import store as store_module
    from service.assistant.today import day_bounds
    store=store_module.AssistantStore(tmp_path/'fictional.sqlite')
    source.now=dt.datetime(2026,month,day,9,tzinfo=ZoneInfo(zone))
    monkeypatch.setattr(store_module,'time',SimpleNamespace(time=lambda:source.now.timestamp()))
    monkeypatch.setattr(S,'_store',store)
    monkeypatch.setattr(S,'_availability_timezone',lambda:zone)
    target=source.now.date()+dt.timedelta(days=1)
    lo,hi=day_bounds(target.isoformat(),zone)
    begin=dt.datetime.combine(target,dt.time(9),ZoneInfo(zone)).timestamp()
    end=dt.datetime.combine(target,dt.time(12),ZoneInfo(zone)).timestamp()
    store.sync_source('calendar',[{'source_id':'fictional-busy','title':'Fictional busy','kind':'event',
                                    'when_ts':begin,'end_ts':end}],diagnostics={
        'snapshot_started_at':source.now.timestamp(),'coverage_start':lo,'coverage_end':hi})
    try:
        result=invoke(minutes=90)
        assert '12:00 PM–1:30 PM' in result and target.isoformat() in result
        assert zone in result
    finally:
        store._db.close()


@pytest.mark.parametrize('period,first,last,count',[
    ('today','2026-10-10','2026-10-10',1),('tomorrow','2026-10-11','2026-10-11',1),
    ('this week','2026-10-10','2026-10-11',2),('next week','2026-10-12','2026-10-18',7),
    ('this month','2026-10-10','2026-10-31',22),('next month','2026-11-01','2026-11-30',30),
    ('2026-10-22','2026-10-22','2026-10-22',1)])
def test_full_calendar_date_ranges_share_one_coverage_receipt(source,period,first,last,count):
    result=asyncio.run(S.find_free_time(period=period))
    assert not result.startswith('(error:')
    assert source.reads[0]==first and source.reads[-1]==last and len(source.reads)==count


def test_second_day_uncovered_does_not_leak_first_day_slots(source):
    source.sync['diagnostics']['coverage_end']=stamp('2026-10-11T00:00')
    result=S.find_free_time(days=2)
    assert result.startswith('(error:') and 'Free for' not in result


@pytest.mark.parametrize('age,ready',[(180,True),(180.001,False),(-0.001,False)])
def test_receipt_freshness_boundary_is_exact(source,age,ready):
    source.sync['last_sync']=source.now.timestamp()-age
    assert invoke().startswith('(error:') is (not ready)


def test_unknown_manual_kind_cannot_hide_an_event(source):
    row={**event(),'source':'manual'};row.pop('kind',None);source.rows=[row]
    assert 'unknown kind' in invoke()


@pytest.mark.parametrize('writable', [True, None])
def test_the_users_own_holidays_calendar_still_blocks(source, writable):
    """dev8 audit: a user-owned "Family Holidays" calendar holding an all-day trip
    was treated as free. Only a read-only (subscribed) holidays calendar is skipped."""
    trip=event(start='2026-10-10T00:00',end='2026-10-12T00:00', context='Family Holidays'); trip['all_day']=True
    if writable is not None:
        trip['calendar_writable']=writable
    source.rows=[trip]
    assert 'No gaps' in invoke(False)
