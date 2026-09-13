"""Disabled scheduler and pure adapters under crash, race and capacity stress."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from tests.test_mini_resources import synthetic_capacity, synthetic_process

from mini.adapters import AdapterRefusal, SnapshotAdapter, REVISION, KINDS, portable_tool
from mini.runtime import Runtime
from mini.store import Store, CapacityError, Collision, StoreUnavailable, LEGACY_SCHEMA

SNAPSHOT={"items":[{"title":"Synthetic", "text":"A fixture snapshot; no provider was contacted."}]}


def runtime(tmp_path, **limits):
    store=Store(tmp_path.resolve()/'private','fixture-mini',**limits)
    store.register_job('study','study.generate',interval_s=10,first_due=100)
    adapter=SnapshotAdapter('study.generate',enabled=True,qualification={
        'kind':'study.generate','revision':REVISION,'scope':'portable-snapshot-only'})
    return Runtime(store,enabled_jobs=('study',),adapters={'study.generate':adapter})


def test_disabled_by_default_restart_and_explicit_enablement(tmp_path):
    active=runtime(tmp_path)
    disabled=Runtime(active.store)
    assert disabled.tick(1000000000,{'study':SNAPSHOT})==[]
    assert active.store.status()['pending_occurrences']==0
    restarted=Runtime(Store(active.store.path.parent,'fixture-mini'))
    assert restarted.tick(1000000000,{'study':SNAPSHOT})==[]
    assert active.tick(100,{'study':SNAPSHOT})
    assert active.store.status()['effects_enabled'] is False


def test_catchup_first_snapshot_wins_restart_and_clock_rollback(tmp_path):
    runner=runtime(tmp_path)
    ids=runner.stage(130,{'study':SNAPSHOT})
    assert len(ids)==4
    restarted=runtime(tmp_path)
    assert restarted.stage(130,{'study':{'items':[{'title':'Changed','text':'Later input'}]}})==[]
    assert len(restarted.complete_pending())==4
    assert restarted.tick(110,{'study':SNAPSHOT})==[]
    assert all('fixture snapshot' in r['text'] for r in restarted.store.page()['results'])
    assert len(restarted.tick(140,{'study':SNAPSHOT}))==1


def test_whole_batch_capacity_refusal_and_retry(tmp_path):
    runner=runtime(tmp_path,max_occurrences=3)
    with pytest.raises(CapacityError):runner.tick(130,{'study':SNAPSHOT})
    assert runner.store.status()['pending_occurrences']==0
    assert len(runner.tick(120,{'study':SNAPSHOT}))==3
    assert runner.tick(120,{'study':SNAPSHOT})==[]
    with pytest.raises(CapacityError):runner.tick(140,{'study':SNAPSHOT})
    assert len(runner.store.page()['results'])==3


def test_concurrent_tick_and_publication_exactly_once(tmp_path):
    runner=runtime(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _:runtime(tmp_path).tick(190,{'study':SNAPSHOT}),range(4)))
    results=runner.store.page()['results']
    assert len(results)==len({r['result_id'] for r in results})==10
    assert runner.store.status()['pending_occurrences']==0


def test_fault_mid_staging_rolls_back_every_occurrence(tmp_path):
    runner=runtime(tmp_path)
    with runner.store.connect(write=True) as db:
        db.execute("CREATE TRIGGER fault BEFORE INSERT ON runtime_inputs BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(StoreUnavailable):runner.stage(130,{'study':SNAPSHOT})
    assert runner.store.status()['pending_occurrences']==0
    with runner.store.connect(write=True) as db:db.execute('DROP TRIGGER fault')
    assert len(runner.tick(130,{'study':SNAPSHOT}))==4


def test_completion_failure_preserves_staged_snapshot(tmp_path):
    runner=runtime(tmp_path,max_results=1)
    runner.stage(110,{'study':SNAPSHOT})
    with pytest.raises(CapacityError):runner.complete_pending()
    assert runner.store.status()['pending_occurrences']==1
    assert len(runner.store.page()['results'])==1


@pytest.mark.parametrize('now',[True,-1,1.2,2**53+1,None])
def test_bad_time(now,tmp_path):
    with pytest.raises(ValueError):runtime(tmp_path).tick(now,{'study':SNAPSHOT})


def test_max_time_large_outage_and_interval_overflow(tmp_path):
    runner=runtime(tmp_path,max_occurrences=1)
    with pytest.raises(CapacityError):runner.tick(2**53,{'study':SNAPSHOT})
    for interval in (True,2**53+1,0,-1):
        with pytest.raises(ValueError):runner.store.register_job('bad','study.generate',interval_s=interval)


@pytest.mark.parametrize('kind',sorted(KINDS))
def test_adapters_individually_disabled_and_snapshot_only(kind):
    with pytest.raises(AdapterRefusal):SnapshotAdapter(kind).render(SNAPSHOT)
    with pytest.raises(AdapterRefusal):SnapshotAdapter(kind,enabled=True).render(SNAPSHOT)
    adapter=SnapshotAdapter(kind,enabled=True,qualification={'kind':kind,'revision':REVISION,'scope':'portable-snapshot-only'})
    assert adapter.render(SNAPSHOT)[1]
    for kwargs in ({},{'token':'fixture'},{'url':'https://example.invalid/private'}):
        with pytest.raises(AdapterRefusal,match='provider_not_qualified'):adapter.fetch_provider(**kwargs)
    for field in ('credentials','url','path','shell','mail','calendar','approved'):
        with pytest.raises(AdapterRefusal):adapter.render({**SNAPSHOT,field:'fixture'})


@pytest.mark.parametrize('name',['mail.read','messages.send','calendar.write','shell','filesystem.read','tcc','native.exec',
                                 'https://example.invalid','text.outline.__globals__','__import__',None])
def test_strict_portable_allowlist(name):
    with pytest.raises(AdapterRefusal):portable_tool(name,{'text':'fixture'})


def test_adapter_module_has_no_io_imports_and_never_interprets_content(monkeypatch):
    import ast
    import mini.adapters as adapters
    imports=[n.module for n in ast.walk(ast.parse(Path(adapters.__file__).read_text())) if isinstance(n,ast.ImportFrom)]
    assert imports==['types']
    assert not any(isinstance(n,ast.Import) for n in ast.walk(ast.parse(Path(adapters.__file__).read_text())))
    payload='file:///private/Mail $(touch /tmp/never) https://example.invalid/private'
    def forbidden(*a,**k):raise AssertionError('I/O attempted')
    monkeypatch.setattr('builtins.open',forbidden)
    monkeypatch.setattr(os,'system',forbidden)
    assert portable_tool('text.outline',{'text':payload})==payload


def test_crash_after_staging_recovers_persisted_input(tmp_path):
    root=tmp_path.resolve()/'private'
    code='''
import os,sys
from pathlib import Path
from tests.test_node_runtime_completion import runtime,SNAPSHOT
r=runtime(Path(sys.argv[1]))
r.stage(130,{'study':SNAPSHOT})
os._exit(0)
'''
    result=subprocess.run([sys.executable,'-B','-c',synthetic_process(code, tmp_path),str(tmp_path)],capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert len(runtime(tmp_path).complete_pending())==4


def test_exact_v1_migration_preserves_identity_and_rejects_drift(tmp_path):
    root=tmp_path.resolve()/'legacy';root.mkdir(mode=0o700)
    file=root/'node.sqlite3'
    with sqlite3.connect(file) as db:
        for sql in LEGACY_SCHEMA:db.execute(sql)
        db.executemany('INSERT INTO metadata VALUES (?,?)',[('node_id','fixture-mini'),('instance','a'*32),('cursor_key','b'*64)])
        db.execute('PRAGMA user_version=1')
    file.chmod(0o600)
    store=Store(root,'fixture-mini')
    assert store.instance=='a'*32 and store.key.hex()=='b'*64
    with store.connect() as db:assert db.execute('PRAGMA user_version').fetchone()[0]==2


def test_storage_reserve_unavailable_and_final_transaction_check(tmp_path,monkeypatch):
    from mini.resources import GB
    runner=runtime(tmp_path)
    monkeypatch.setattr(os,'statvfs',lambda _:type('FS',(),{'f_bavail':50*GB-1,'f_frsize':1})())
    with pytest.raises(CapacityError):runner.stage(100,{'study':SNAPSHOT})
    assert runner.store.status()['pending_occurrences']==0


def test_pending_completes_even_when_new_catchup_refuses(tmp_path):
    runner=runtime(tmp_path,max_occurrences=2)
    runner.stage(100,{'study':SNAPSHOT})
    with pytest.raises(CapacityError):runner.tick(10000,{'study':SNAPSHOT})
    assert runner.store.status()['pending_occurrences']==0
    assert len(runner.store.page()['results'])==1


def test_node_initial_free_space_preflight_refuses_before_database_creation(tmp_path,monkeypatch):
    from mini.resources import GB
    monkeypatch.setattr(os,'statvfs',lambda _:type('FS',(),{'f_bavail':150*GB-1,'f_frsize':1})())
    with pytest.raises(CapacityError):Store(tmp_path.resolve()/'private','fixture-mini')
    assert not (tmp_path/'private'/'node.sqlite3').exists()


def test_pending_completion_batches_and_sql_size_refusal(tmp_path,monkeypatch):
    runner=runtime(tmp_path)
    runner.stage(800,{'study':SNAPSHOT})
    assert len(runner.complete_pending())==71
    runner.stage(810,{'study':SNAPSHOT})
    with runner.store.connect(write=True) as db:
        db.execute('DROP TRIGGER immutable_inputs_update')
        db.execute("UPDATE runtime_inputs SET payload=? WHERE occurrence_id IN (SELECT occurrence_id FROM occurrences WHERE state='pending')",('x'*100001,))
    with pytest.raises(StoreUnavailable):runner.complete_pending()
