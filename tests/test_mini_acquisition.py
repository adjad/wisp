"""Acquisition contracts use synthetic responses only; no live provider I/O."""
import asyncio
import copy
import json

import pytest
from tests.test_mini_resources import synthetic_capacity
from tests.test_node_runtime_completion import runtime, SNAPSHOT
from mini import acquisition as a
from mini.adapters import SnapshotAdapter, REVISION
from mini.store import Store, StoreUnavailable, Collision
from mini.backup import backup, restore


def configuration(kind='study.generate'):
    return dict(schema_version=1, connector_id='fixture', version='a'*40, kind=kind,
        origin='https://fixture.invalid', credential_role=a.ROLES[kind],
        capabilities=['snapshot.acquire','text.outline'], classification='synthetic',
        privacy={'outbound_private_data':False,'persist':'synthetic-only'}, enabled=False,
        limits=dict(requests_per_minute=60,deadline_ms=100,max_request_bytes=1024,max_result_bytes=10000))


def connector(kind='study.generate', snapshot=SNAPSHOT, delay=0, config=None):
    c = configuration(kind) if config is None else config
    receipt = dict(schema_version=1,contract_sha256=a.digest(c),scope='synthetic-acquisition-only',status='PASS')
    return a.Connector(c,adapter=a.FixtureAdapter(snapshot,delay=delay),receipt=receipt,
                       trusted_receipt_sha256=a.digest(receipt),simulate=True)


def asked(c):
    return a.request(c.configuration,node_id='fixture-mini',job_id='study',now=100,snapshot_id='fixture')


@pytest.mark.parametrize('kind',sorted(a.KINDS))
async def test_all_four_processors_acquire_stage_publish_restart_and_backup(kind,tmp_path):
    from mini.runtime import Runtime
    store=Store(tmp_path.resolve()/'state','fixture-mini')
    store.register_job('synthetic',kind,interval_s=10,first_due=100)
    processor=SnapshotAdapter(kind,enabled=True,qualification={'kind':kind,'revision':REVISION,'scope':'portable-snapshot-only'})
    runner=Runtime(store,enabled_jobs=('synthetic',),adapters={kind:processor})
    connections={'synthetic':connector(kind)}
    assert len(await a.acquire_tick(runner,130,connections,{'synthetic':'fixture'}))==4
    ids=[r['result_id'] for r in store.page()['results']]
    assert await a.acquire_tick(runner,130,connections,{'synthetic':'fixture'})==[]
    assert [r['result_id'] for r in store.page()['results']]==ids
    restored=tmp_path.resolve()/'restored'
    snapshot=tmp_path.resolve()/'snapshot.sqlite3'
    backup(store,snapshot);restore(snapshot,restored,'fixture-mini')
    assert [r['result_id'] for r in Store(restored,'fixture-mini').page()['results']]==ids
    assert store.status()['jobs_enabled'] is False


async def test_disabled_and_unqualified_capabilities_never_invoke_adapter():
    c=a.Connector(configuration())
    with pytest.raises(a.AcquisitionRefusal,match='disabled'):await c.acquire(asked(c))
    with pytest.raises(a.AcquisitionRefusal):a.Connector(configuration(),adapter=a.FixtureAdapter(SNAPSHOT))
    with pytest.raises(a.AcquisitionRefusal):a.Connector(configuration(),adapter=a.FixtureAdapter(SNAPSHOT),simulate=True)
    with pytest.raises(a.AcquisitionRefusal):a.Connector(configuration(),adapter=object(),simulate=True)


@pytest.mark.parametrize('changes',[
    {'enabled':True},{'origin':'https://real.example'},{'origin':'https://fixture.invalid/path'},
    {'origin':'file:///private'},{'origin':'https://user:secret@fixture.invalid'},
    {'credential_role':'admin.write'},{'classification':'private'}, {'version':'latest'},
    {'privacy':{'outbound_private_data':True,'persist':'synthetic-only'}},
    *[{'capabilities':[v]} for v in ('shell','filesystem.read','native','tcc','mail.send','http.redirect','__import__')]])
def test_undeclared_or_unsafe_contract_refuses(changes):
    c=configuration();c.update(changes)
    with pytest.raises(a.AcquisitionRefusal):connector(config=c)


@pytest.mark.parametrize('field,value',[('url','https://evil.invalid'),('credentials','secret'),('origin','https://other.invalid'),
    ('credential_role','canvas.read'),('operation','shell'),('schema_version',True),('parameters',{'snapshot_id':'https://evil.invalid'})])
async def test_request_schema_and_binding_confusion(field,value):
    c=connector();q=asked(c);q[field]=value
    with pytest.raises(a.AcquisitionRefusal):await c.acquire(q)


@pytest.mark.parametrize('text',['```python\nprint(1)','#!/bin/sh','$(touch x)','https://evil.invalid','file:///private',
    '<script>alert(1)</script>','Bearer secret','password=secret','a'*64,'sudo rm file'])
async def test_executable_urls_raw_credentials_refuse_without_leak(text,caplog):
    c=connector(snapshot={'items':[{'title':'Synthetic','text':text}]})
    with pytest.raises(a.AcquisitionRefusal) as error:await c.acquire(asked(c))
    assert str(error.value)=='forbidden_content'
    assert text not in caplog.text


async def test_response_redirect_error_and_binding_confusion(monkeypatch):
    original=a.FixtureAdapter.acquire
    for field,value in [('origin','https://other.invalid'),('credential_role','canvas.read'),('classification','private'),
                        ('location','https://evil.invalid'),('request_id','0'*64),('status','redirect')]:
        async def changed(self,q):
            result=await original(self,q);result[field]=value;return result
        monkeypatch.setattr(a.FixtureAdapter,'acquire',changed)
        c=connector()
        with pytest.raises(a.AcquisitionRefusal):await c.acquire(asked(c))


async def test_oversize_rate_deadline_cancel_and_no_staged_partial_batch(tmp_path):
    c=connector(snapshot={'items':[{'title':'x','text':'x'*11000}]})
    with pytest.raises(a.AcquisitionRefusal,match='oversize'):await c.acquire(asked(c))
    config=configuration();config['limits']['requests_per_minute']=1
    c=connector(config=config);await c.acquire(asked(c))
    with pytest.raises(a.AcquisitionRefusal,match='rate'):await c.acquire(asked(c))
    c=connector(delay=.2)
    runner=runtime(tmp_path)
    with pytest.raises(a.AcquisitionRefusal,match='timeout'):
        await a.acquire_tick(runner,100,{'study':c},{'study':'fixture'})
    assert runner.store.status()['pending_occurrences']==0
    c=connector(delay=.2)
    task=asyncio.create_task(c.acquire(asked(c)))
    await asyncio.sleep(.01);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    await asyncio.sleep(0)
    assert c._active.done()


async def test_collision_restart_pending_and_contract_mutation(tmp_path):
    runner=runtime(tmp_path);c=connector()
    q=asked(c);snapshot=await c.acquire(q)
    receipt=a.snapshot_receipt(c.configuration,q,snapshot,100)
    runner.stage(100,{'study':snapshot},acquisition={'study':receipt})
    restarted=runtime(tmp_path)
    assert len(restarted.complete_pending())==1
    changed=connector(snapshot={'items':[{'title':'Changed','text':'Different input'}]})
    with pytest.raises((Collision,StoreUnavailable)):
        await a.acquire_tick(restarted,100,{'study':changed},{'study':'fixture'})
    assert len(restarted.store.page()['results'])==1
    c.configuration['version']='b'*40
    with pytest.raises(a.AcquisitionRefusal,match='unqualified'):await c.acquire(q)


async def test_cancellation_resistant_fixture_retains_busy_until_done(monkeypatch):
    release=asyncio.Event()
    async def stubborn(self,q):
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {'schema_version':1,**{k:q[k] for k in ('request_id','origin','credential_role','classification')},
                'status':'ok','snapshot':SNAPSHOT}
    monkeypatch.setattr(a.FixtureAdapter,'acquire',stubborn)
    config=configuration();config['limits']['deadline_ms']=10
    c=connector(config=config)
    with pytest.raises(a.AcquisitionRefusal,match='timeout'):await c.acquire(asked(c))
    with pytest.raises(a.AcquisitionRefusal,match='busy'):await c.acquire(asked(c))
    release.set();await c._active


async def test_failed_second_acquisition_stages_no_first_snapshot(tmp_path):
    from mini.runtime import Runtime
    store=Store(tmp_path.resolve()/'batch','fixture-mini')
    adapters={}
    connections={}
    for jid,kind in (('a','canvas.sync'),('b','study.generate')):
        store.register_job(jid,kind,interval_s=10,first_due=100)
        adapters[kind]=SnapshotAdapter(kind,enabled=True,qualification={
            'kind':kind,'revision':REVISION,'scope':'portable-snapshot-only'})
        connections[jid]=connector(kind,delay=.2 if jid=='b' else 0)
    runner=Runtime(store,enabled_jobs=('a','b'),adapters=adapters)
    with pytest.raises(a.AcquisitionRefusal):await a.acquire_tick(runner,130,connections,{'a':'fixture','b':'fixture'})
    assert store.status()['pending_occurrences']==0
    assert store.page()['results']==[]


@pytest.mark.parametrize('change',['hash','request','role','origin','kind','version','job','node'])
def test_persisted_receipt_cannot_cross_binding(change,tmp_path):
    runner=runtime(tmp_path);c=connector();q=asked(c)
    receipt=a.snapshot_receipt(c.configuration,q,SNAPSHOT,100)
    if change=='hash':receipt['contract_sha256']='f'*64
    elif change=='request':receipt['request_id']='0'*64
    elif change in ('role','origin','kind','version'):
        key={'role':'credential_role','origin':'origin','kind':'kind','version':'version'}[change]
        receipt['configuration'][key]={'role':'canvas.read','origin':'https://other.invalid','kind':'canvas.sync','version':'b'*40}[change]
    else:
        q=a.request(c.configuration,node_id='other' if change=='node' else 'fixture-mini',
                    job_id='other' if change=='job' else 'study',now=100,snapshot_id='fixture')
        receipt=a.snapshot_receipt(c.configuration,q,SNAPSHOT,100)
    with pytest.raises(StoreUnavailable):runner.stage(100,{'study':SNAPSHOT},acquisition={'study':receipt})
    assert runner.store.status()['pending_occurrences']==0


async def test_corrupt_persisted_provenance_refuses_restart_and_backup(tmp_path):
    runner=runtime(tmp_path);c=connector();q=asked(c)
    runner.stage(100,{'study':SNAPSHOT},acquisition={'study':a.snapshot_receipt(c.configuration,q,SNAPSHOT,100)})
    with runner.store.connect(write=True) as db:
        sql=db.execute("SELECT sql FROM sqlite_master WHERE name='immutable_inputs_update'").fetchone()[0]
        row=db.execute('SELECT occurrence_id,payload FROM runtime_inputs').fetchone()
        changed=json.loads(row['payload']);changed['acquisition']['request_id']='0'*64
        db.execute('DROP TRIGGER immutable_inputs_update')
        db.execute('UPDATE runtime_inputs SET payload=? WHERE occurrence_id=?',
                   (json.dumps(changed,sort_keys=True,separators=(',',':')),row['occurrence_id']))
        db.execute(sql)
    with pytest.raises(StoreUnavailable):runner.complete_pending()
    with pytest.raises(StoreUnavailable):backup(runner.store,tmp_path.resolve()/'invalid.sqlite3')
