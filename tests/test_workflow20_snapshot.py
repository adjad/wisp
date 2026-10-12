"""Conservative availability snapshots from isolated fictional records."""
import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from service.assistant import store as module
from service.assistant.store import AssistantStore
from service.assistant.today import day_bounds
from service.tools import schedule_extras as S


@pytest.mark.parametrize('zone,day,hours', [
    ('UTC','2026-10-11',24), ('Pacific/Kiritimati','2026-10-11',24),
    ('America/Los_Angeles','2027-03-14',23), ('America/Los_Angeles','2026-11-01',25),
])
def test_snapshot_retains_unknown_all_day_ends_and_known_overlap(tmp_path, monkeypatch, zone, day, hours):
    lo, hi = day_bounds(day, zone)
    assert hi - lo == hours * 3600
    clock = dt.datetime.fromisoformat(day).replace(hour=6,tzinfo=ZoneInfo(zone))
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: clock.timestamp()))
    store = AssistantStore(tmp_path / 'synthetic.db')
    def row(sid,start,end=None,**extra):
        return dict(source_id=sid,title='Same fictional title',kind='event',when_ts=start,end_ts=end,**extra)
    rows = [row('unknown-all-day',lo-86400,all_day=True), row('unknown-timed',lo-3600),
            row('at-hi',hi,hi+3600), row('ends-at-lo',lo-3600,lo),
            row('overlap',lo-3600,lo+3600), row('point-at-lo',lo,lo),
            row('point-before-lo',lo-1,lo-1), row('inactive',lo+3600,lo+7200),
            row('distinct-a',lo+4000,lo+5000), row('distinct-b',lo+4000,lo+5000)]
    store.sync_source('calendar', rows, diagnostics={'snapshot_started_at':clock.timestamp(),
        'coverage_start':lo-86400,'coverage_end':hi})
    store._db.execute("UPDATE commitments SET status='done' WHERE source_id='inactive'")
    store._db.commit()
    store.sync_source('reminders',[dict(source_id='old-reminder',title='Old',kind='reminder',when_ts=lo-1)])
    store.add_manual('Old manual deadline',lo-1)
    snapshot = store.today_snapshot(day,zone)
    found = {r['source_id']:r for r in snapshot['commitments']}
    assert set(found) == {'unknown-all-day','unknown-timed','overlap','point-at-lo','distinct-a','distinct-b'}
    assert found['unknown-all-day']['end_ts'] is None
    assert found['overlap']['end_ts'] == lo+3600
    assert found['distinct-a']['id'] != found['distinct-b']['id']
    # The actual planner must see the unknown row and refuse to verify a gap.
    class Clock(dt.datetime):
        @classmethod
        def now(cls,tz=None):
            return clock.astimezone(tz) if tz else clock.replace(tzinfo=None)
    monkeypatch.setattr(S,'dt',SimpleNamespace(datetime=Clock,time=dt.time,timedelta=dt.timedelta))
    monkeypatch.setattr(S,'_availability_timezone',lambda:zone)
    monkeypatch.setattr(S,'_store',store)
    monkeypatch.setattr('service.assistant.sync_status.ensure_sources',AsyncMock(return_value={
        'sources':[{'id':'calendar','state':'ready'},{'id':'reminders','state':'ready'}], 'reminders_fresh':True}))
    result = asyncio.run(S.find_free_time(period=day))
    assert result.startswith('(error:') and 'missing or invalid ends' in result
    assert '9:00 AM' not in result and 'candidate' not in result
    store._db.close()
