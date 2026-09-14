"""Backup/restore only touch private temporary synthetic SQLite databases."""
import json
import os
from pathlib import Path
import sqlite3

import pytest
from tests.test_mini_resources import synthetic_capacity, synthetic_process

from mini.backup import backup,restore,capacity
from mini.resources import GB, ResourceRefusal
from mini.store import Store,StoreUnavailable,CapacityError,CursorError
from tests.test_node_runtime_completion import runtime,SNAPSHOT


def fixture(tmp_path):
    runner=runtime(tmp_path)
    runner.tick(100,{'study':SNAPSHOT})
    parent=tmp_path.resolve()/'snapshots';parent.mkdir(mode=0o700)
    return runner, parent


@pytest.mark.asyncio
async def test_rotated_restore_replays_through_authenticated_node_without_duplicates(tmp_path,monkeypatch):
    import httpx
    from unittest.mock import AsyncMock
    from mini.node import Node
    from service import nodes
    from service.config.endpoints import Endpoint
    runner,parent=fixture(tmp_path)
    inbox=nodes.NodeInbox(tmp_path/'consumer.db')
    inbox.ingest('fixture-mini',runner.store.page(),expected_cursor='')
    old=inbox.cursor('fixture-mini')
    hub=type('Hub',(),{'publish':AsyncMock()})()
    await inbox.publish('fixture-mini',hub)
    previous_count=hub.publish.await_count
    snapshot=backup(runner.store,parent/'rotation.sqlite3')
    restored=Store(restore(snapshot,parent/'rotated','fixture-mini',identity_mode='rotate'),'fixture-mini')
    monkeypatch.setenv('NODE_ROTATION_FIXTURE_KEY','a'*64)
    monkeypatch.setattr(nodes,'endpoint',lambda name:Endpoint(name,'https://node.test','env:NODE_ROTATION_FIXTURE_KEY'))
    await nodes.poll_once(inbox,hub,{'node_id':'fixture-mini','endpoint':'fixture'},transport=httpx.ASGITransport(app=Node('a'*64,restored)))
    assert inbox.cursor('fixture-mini')!=old
    assert hub.publish.await_count==previous_count


@pytest.mark.parametrize('mode',['rotate','preserve'])
def test_online_backup_restore_contents_and_cursor_identity(tmp_path,mode):
    runner,parent=fixture(tmp_path)
    cursor=runner.store.page()['next_cursor']
    # Hold a WAL reader open: a plain copy of the main DB would omit commits.
    with runner.store.connect() as reader:
        reader.execute('SELECT * FROM metadata').fetchall()
        runner.tick(110,{'study':SNAPSHOT})
        snapshot=backup(runner.store,parent/'backup.sqlite3')
    destination=restore(snapshot,parent/'restored','fixture-mini',identity_mode=mode)
    restored=Store(destination,'fixture-mini')
    assert restored.page()['results']==runner.store.page()['results']
    assert restored.path.stat().st_mode&0o777==0o600
    if mode=='preserve':assert len(restored.page(cursor)['results'])==1
    else:
        with pytest.raises(CursorError):restored.page(cursor)


def test_restore_refuses_existing_target_and_snapshot_collision(tmp_path):
    runner,parent=fixture(tmp_path)
    snapshot=backup(runner.store,parent/'backup.sqlite3')
    before=runner.store.page()
    with pytest.raises(StoreUnavailable):restore(snapshot,runner.store.path.parent,'fixture-mini')
    with pytest.raises(StoreUnavailable):backup(runner.store,snapshot)
    assert runner.store.page()==before
    with pytest.raises(StoreUnavailable):restore(snapshot,parent/'bad','wrong-mini')
    assert not (parent/'bad').exists()


@pytest.mark.parametrize('sql',[
    'PRAGMA user_version=99','DROP TRIGGER immutable_results_update',
    "UPDATE metadata SET value='bad' WHERE key='instance'",
    "UPDATE occurrences SET state='pending'",
])
def test_restore_rejects_schema_integrity_and_content(tmp_path,sql):
    runner,parent=fixture(tmp_path)
    snapshot=backup(runner.store,parent/'backup.sqlite3')
    with sqlite3.connect(snapshot) as db:db.execute(sql)
    with pytest.raises(StoreUnavailable):restore(snapshot,parent/'restored','fixture-mini')
    assert not (parent/'restored').exists()


def test_corruption_capacity_and_unsafe_links(tmp_path,monkeypatch):
    runner,parent=fixture(tmp_path)
    snapshot=backup(runner.store,parent/'backup.sqlite3')
    link=parent/'link';link.symlink_to(snapshot)
    with pytest.raises((StoreUnavailable,OSError)):restore(link,parent/'restored','fixture-mini')
    snapshot.chmod(0o644)
    with pytest.raises(StoreUnavailable):restore(snapshot,parent/'restored','fixture-mini')
    snapshot.chmod(0o600)
    original_statvfs = os.statvfs
    monkeypatch.setattr(os,'statvfs',lambda _:type('FS',(),{'f_bavail':149*GB,'f_frsize':1})())
    with pytest.raises(CapacityError):restore(snapshot,parent/'restored','fixture-mini')
    assert not (parent/'restored').exists()
    snapshot.write_bytes(b'corrupt')
    monkeypatch.setattr(os,'statvfs',original_statvfs)
    with pytest.raises(StoreUnavailable):restore(snapshot,parent/'restored','fixture-mini')


def test_restore_injected_publication_failure_is_retryable(tmp_path,monkeypatch):
    runner,parent=fixture(tmp_path)
    snapshot=backup(runner.store,parent/'backup.sqlite3')
    import mini.backup as module
    original=module.publish
    def fail(*args):raise OSError('synthetic publication failure')
    monkeypatch.setattr(module,'publish',fail)
    with pytest.raises(ResourceRefusal):restore(snapshot,parent/'restored','fixture-mini')
    assert not (parent/'restored').exists()
    assert not list(parent.glob('.restore-*'))
    monkeypatch.setattr(module,'publish',original)
    assert restore(snapshot,parent/'restored','fixture-mini')


def test_backup_mid_operation_capacity_refusal_does_not_publish(tmp_path,monkeypatch):
    runner,parent=fixture(tmp_path)
    import mini.backup as module
    original=module.capacity
    calls=[]
    def fail(parent,growth,**kwargs):
        calls.append(1)
        if len(calls)>1:raise CapacityError('synthetic disk filled')
        return original(parent,growth,**kwargs)
    monkeypatch.setattr(module,'capacity',fail)
    with pytest.raises(CapacityError):backup(runner.store,parent/'backup.sqlite3')
    assert not (parent/'backup.sqlite3').exists()
    assert runner.store.page()['results']
    assert not list(parent.glob('.backup-*'))


def test_reserve_exact_boundaries(tmp_path,monkeypatch):
    for preflight,boundary in [(True,150*GB),(False,50*GB)]:
        monkeypatch.setattr(os,'statvfs',lambda _:type('FS',(),{'f_bavail':boundary,'f_frsize':1})())
        capacity(tmp_path,0,preflight=preflight)
        monkeypatch.setattr(os,'statvfs',lambda _:type('FS',(),{'f_bavail':boundary-1,'f_frsize':1})())
        with pytest.raises(CapacityError):capacity(tmp_path,0,preflight=preflight)


@pytest.mark.parametrize('operation',['backup','restore'])
def test_abrupt_exit_after_atomic_publication_has_complete_single_link_state(tmp_path,operation):
    import subprocess,sys
    runner,parent=fixture(tmp_path)
    source=backup(runner.store,parent/'source.sqlite3')
    code='''
import os,sys
from pathlib import Path
import mini.backup as b
from mini.store import Store
original=b.publish
def die_after(source,target):
    original(source,target)
    os._exit(0)
b.publish=die_after
if sys.argv[1]=='backup':
    b.backup(Store(sys.argv[2],'fixture-mini'),Path(sys.argv[3])/'published.sqlite3')
else:
    b.restore(sys.argv[4],Path(sys.argv[3])/'restored','fixture-mini')
'''
    result=subprocess.run([sys.executable,'-B','-c',synthetic_process(code, tmp_path),operation,str(runner.store.path.parent),str(parent),str(source)],capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    if operation=='backup':
        assert (parent/'published.sqlite3').stat().st_nlink==1
        restore(parent/'published.sqlite3',parent/'restored','fixture-mini')
    assert Store(parent/'restored','fixture-mini').page()['results']==runner.store.page()['results']


def test_atomic_publish_refuses_racing_existing_destination(tmp_path):
    from mini.backup import publish
    source=tmp_path/'source';target=tmp_path/'target'
    source.write_text('new');target.write_text('old')
    with pytest.raises(StoreUnavailable):publish(source,target)
    assert target.read_text()=='old' and source.read_text()=='new'


def test_oversized_sql_payload_refuses_before_json_parse(tmp_path,monkeypatch):
    from mini.backup import validate_database
    runner,parent=fixture(tmp_path)
    snapshot=backup(runner.store,parent/'backup.sqlite3')
    with sqlite3.connect(snapshot) as db:
        db.execute('DROP TRIGGER immutable_results_update')
        db.execute('UPDATE results SET payload=?,bytes=?',('x'*200001,200001))
        from mini.store import SCHEMA
        db.execute(next(sql for sql in SCHEMA if sql.startswith('CREATE TRIGGER immutable_results_update')))
        monkeypatch.setattr(json,'loads',lambda *_:pytest.fail('oversized payload parsed'))
        with pytest.raises(CapacityError):validate_database(db,'fixture-mini')


def test_preserved_restore_rejects_consumer_cursor_ahead_of_snapshot(tmp_path):
    runner,parent=fixture(tmp_path)
    snapshot=backup(runner.store,parent/'old.sqlite3')
    runner.tick(110,{'study':SNAPSHOT})
    ahead=runner.store.page()['next_cursor']
    restored=Store(restore(snapshot,parent/'restored','fixture-mini',identity_mode='preserve'),'fixture-mini')
    with pytest.raises(CursorError):restored.page(ahead)


def test_hardlinked_snapshot_is_refused(tmp_path):
    runner,parent=fixture(tmp_path)
    snapshot=backup(runner.store,parent/'backup.sqlite3')
    os.link(snapshot,parent/'alias')
    with pytest.raises(StoreUnavailable):restore(snapshot,parent/'restored','fixture-mini')
    assert not (parent/'restored').exists()


def test_crash_before_restore_publication_leaves_no_public_state(tmp_path):
    import subprocess,sys
    runner,parent=fixture(tmp_path)
    snapshot=backup(runner.store,parent/'backup.sqlite3')
    code='''
import os,sys
import mini.backup as b
b.publish=lambda *args:os._exit(0)
b.restore(sys.argv[1],sys.argv[2],'fixture-mini')
'''
    result=subprocess.run([sys.executable,'-B','-c',synthetic_process(code, tmp_path),str(snapshot),str(parent/'restored')],capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert not (parent/'restored').exists()
    assert list(parent.glob('.restore-*'))
    restore(snapshot,parent/'restored','fixture-mini')
    assert Store(parent/'restored','fixture-mini').page()['results']==runner.store.page()['results']
