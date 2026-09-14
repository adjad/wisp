"""Synthetic recovery and exact-host management without live services."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from mini import recovery
from mini.store import StoreUnavailable
from tests.test_mini_resources import synthetic_capacity

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'infra/mac-mini'))
import artifact_signature as signatures
import receiver
import node_prep


def test_zero_database_requires_explicit_new_state(tmp_path):
    root=tmp_path.resolve();source=root/'state';source.mkdir(mode=0o700)
    main=source/'node.sqlite3';main.touch(mode=0o600)
    report=recovery.doctor(source)
    assert report['state']=='zero_main_without_wal'
    with pytest.raises(StoreUnavailable):recovery.recover_state(source,root/'fresh','fixture',expected_inventory=report['inventory_sha256'],fresh=True)
    result=recovery.recover_state(source,root/'fresh','fixture',expected_inventory=report['inventory_sha256'],fresh=True,apply=True,activity=lambda p:True)
    assert result['originals']=='preserved' and main.read_bytes()==b''
    assert (root/'fresh/node.sqlite3').stat().st_size>0


def test_zero_database_with_committed_wal_recovers_copy(tmp_path):
    from mini.store import SCHEMA
    root=tmp_path.resolve();source=root/'state';source.mkdir(mode=0o700)
    main=source/'node.sqlite3'
    db=sqlite3.connect(main);main.chmod(0o600)
    db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
    for sql in SCHEMA:db.execute(sql)
    db.executemany('INSERT INTO metadata VALUES(?,?)',[('node_id','fixture'),('instance','a'*32),('cursor_key','b'*64)])
    db.execute('PRAGMA user_version=2');db.commit()
    # Preserve a crashed process's main/WAL pair, then close only the fixture's source.
    captured={p.name:p.read_bytes() for p in source.iterdir() if p.name!='node.sqlite3-shm'}
    db.close()
    for p in source.iterdir():p.unlink()
    for name,raw in captured.items():
        (source/name).write_bytes(b'' if name=='node.sqlite3' else raw);(source/name).chmod(0o600)
    before=recovery.state_inventory(source)
    assert recovery.doctor(source)['state']=='zero_main_with_wal'
    recovery.recover_state(source,root/'recovered','fixture',expected_inventory=recovery.inventory_digest(before),apply=True,activity=lambda p:True)
    assert recovery.state_inventory(source)==before
    with sqlite3.connect(root/'recovered/node.sqlite3') as restored:
        assert dict(restored.execute('SELECT key,value FROM metadata'))['instance']!='a'*32


@pytest.mark.parametrize('failure',['active','changed','wal','symlink','capacity'])
def test_recovery_refuses_ambiguous_state(tmp_path,monkeypatch,failure):
    root=tmp_path.resolve();source=root/'state';source.mkdir(mode=0o700)
    main=source/'node.sqlite3';main.touch(mode=0o600)
    pin=recovery.doctor(source)['inventory_sha256']
    if failure=='changed':main.write_bytes(b'changed')
    if failure=='wal':(source/'node.sqlite3-wal').write_bytes(b'invalid');(source/'node.sqlite3-wal').chmod(0o600)
    if failure=='symlink':main.unlink();main.symlink_to(root/'outside')
    if failure=='capacity':monkeypatch.setattr(recovery.backup,'capacity',lambda *a,**k:(_ for _ in ()).throw(StoreUnavailable('capacity')))
    with pytest.raises((StoreUnavailable,OSError)):
        recovery.recover_state(source,root/'new','fixture',expected_inventory=pin,apply=True,fresh=True,activity=lambda p:failure!='active')
    assert not (root/'new').exists()


def test_missing_ledger_requires_independent_high_water_and_is_idempotent(tmp_path):
    root=tmp_path.resolve();root.chmod(0o700)
    sentinel=root/'artifact-releases.json.lock';sentinel.write_bytes(b'1');sentinel.chmod(0o600)
    report=signatures.ledger_doctor(root)
    assert report['status']=='recovery_required'
    auth={'schema_version':1,'operation':'recover-ledger','root':str(root),'uid':os.getuid(),
          'sentinel_sha256':report['sentinel_sha256'],'sequence_floor':42,'statement_sha256':'a'*64,
          'independent_high_water_review':True,'nonce':'c'*64,
          'recovery_history_sha256':report['recovery_history_sha256']}
    pin=signatures.digest(signatures.canonical(auth))
    with pytest.raises(ValueError):signatures.recover_ledger(root,auth,pin)
    assert not signatures.recover_ledger(root,auth,pin,apply=True)['idempotent']
    assert signatures.recover_ledger(root,auth,pin,apply=True)['idempotent']
    assert signatures.ledger_doctor(root)['release_sequence']==42 and sentinel.read_bytes()==b'1'
    for changed in ({**auth,'sequence_floor':1},{**auth,'independent_high_water_review':False}):
        with pytest.raises(ValueError):signatures.recover_ledger(root,changed,signatures.digest(signatures.canonical(changed)),apply=True)


def releases(root,count=4):
    root.chmod(0o700);(root/'state').mkdir(mode=0o700)
    result=[]
    for seq in range(1,count+1):
        owner={'schema_version':2,'node_id':'nFIXTURE','source_commit':'b'*40,'bundle_sha256':str(seq)*64,
               'release_sequence':seq,'statement_sha256':str(seq)*64,'provenance':{}}
        rid=hashlib.sha256(json.dumps({'bundle':owner['bundle_sha256'],'node_id':owner['node_id'],
            'release_sequence':seq,'statement_sha256':owner['statement_sha256']},sort_keys=True).encode()).hexdigest()
        path=root/rid;path.mkdir(mode=0o700)
        (path/'code').write_bytes(b'synthetic');(path/'code').chmod(0o600)
        (path/'state').symlink_to(root/'state')
        owner.update(provisioning_id=rid,inventory=receiver.inventory(path))
        (path/'.owner.json').write_text(json.dumps(owner));(path/'.owner.json').chmod(0o600)
        os.utime(path,(1,1));result.append(rid)
    receipt={**owner,'schema_version':1,'labels':list(receiver.LABELS)}
    (root/'receipt.json').write_text(json.dumps(receipt));(root/'receipt.json').chmod(0o600)
    (root/'artifact-releases.json').write_text(json.dumps({'schema_version':1,'release_sequence':count,'statement_sha256':str(count)*64}));(root/'artifact-releases.json').chmod(0o600)
    (root/'artifact-releases.json.lock').write_bytes(b'1');(root/'artifact-releases.json.lock').chmod(0o600)
    return result


def test_reclaim_preserves_current_previous_state_and_ledger(tmp_path):
    root=tmp_path.resolve();ids=releases(root)
    report=receiver.release_inventory(root,now=100000)
    assert [r['id'] for r in report['releases'] if r['eligible']]
    result=receiver.reclaim_releases(root,ids[:2],expected_inventory=report['inventory_sha256'],apply=True,
        activity=lambda p:True,jobs=lambda:set(),now=100000)
    assert result['removed']==ids[:2]
    assert all((root/i).is_dir() for i in ids[2:]) and (root/'state').is_dir()
    assert signatures.ledger_doctor(root)['release_sequence']==4


@pytest.mark.parametrize('failure',['current','previous','active','jobs','pin','owner','symlink','ledger'])
def test_reclaim_ambiguity_refuses(tmp_path,failure):
    root=tmp_path.resolve();ids=releases(root)
    report=receiver.release_inventory(root,now=100000)
    chosen=ids[-1] if failure=='current' else ids[-2] if failure=='previous' else ids[0]
    if failure=='owner':(root/chosen/'code').write_bytes(b'changed')
    if failure=='symlink':(root/chosen/'code').unlink();(root/chosen/'code').symlink_to(root/'state')
    if failure=='ledger':(root/'artifact-releases.json').write_text('{}')
    with pytest.raises(ValueError):receiver.reclaim_releases(root,[chosen],expected_inventory='bad' if failure=='pin' else report['inventory_sha256'],apply=True,
        activity=lambda p:failure!='active',jobs=lambda:{'active'} if failure=='jobs' else set(),now=100000)
    assert all((root/i).is_dir() for i in ids)


def test_remote_diagnostics_independent_of_services_and_wrong_host_refuses(tmp_path,monkeypatch):
    home=tmp_path.resolve();root=home/'.wisp-mini';root.mkdir(mode=0o700);releases(root)
    monkeypatch.setattr(receiver.Path,'home',lambda:home)
    monkeypatch.setattr(receiver,'execute',lambda *a:pytest.fail('Wisp/oMLX execution'))
    request={'schema_version':1,'node_id':'nFIXTURE','ip':'100.64.0.10','operation':'diagnose','arguments':{}}
    assert receiver.manage(request,verify_identity=lambda p:None)['management']=='ssh_independent_of_services'
    with pytest.raises(ValueError):receiver.manage(request,verify_identity=lambda p:(_ for _ in ()).throw(ValueError('wrong host')))


def test_source_swap_cannot_change_external_bytes_or_permissions(tmp_path,monkeypatch):
    from mini.resources import ResourceRefusal
    root=tmp_path.resolve();source=root/'state';source.mkdir(mode=0o700)
    main=source/'node.sqlite3';main.write_bytes(b'evidence');main.chmod(0o600)
    outside=root/'outside';outside.write_bytes(b'untouched');outside.chmod(0o640)
    before=recovery.state_inventory(source)
    original=recovery.copy_approved
    def swap(src,dst,expected):
        src.unlink();src.symlink_to(outside)
        return original(src,dst,expected)
    monkeypatch.setattr(recovery,'copy_approved',swap)
    with pytest.raises((OSError,StoreUnavailable,ResourceRefusal)):
        recovery.recover_state(source,root/'new','fixture',expected_inventory=recovery.inventory_digest(before),apply=True,activity=lambda p:True)
    assert outside.read_bytes()==b'untouched' and outside.stat().st_mode&0o777==0o640
    assert not (root/'new').exists()


def test_old_recovery_approval_cannot_reset_a_later_ledger_incident(tmp_path):
    root=tmp_path.resolve();root.chmod(0o700)
    sentinel=root/'artifact-releases.json.lock';sentinel.write_bytes(b'1');sentinel.chmod(0o600)
    report=signatures.ledger_doctor(root)
    auth={'schema_version':1,'operation':'recover-ledger','root':str(root),'uid':os.getuid(),
          'sentinel_sha256':report['sentinel_sha256'],'sequence_floor':42,'statement_sha256':'a'*64,
          'independent_high_water_review':True,'nonce':'d'*64,'recovery_history_sha256':report['recovery_history_sha256']}
    pin=signatures.digest(signatures.canonical(auth))
    signatures.recover_ledger(root,auth,pin,apply=True)
    ledger=root/'artifact-releases.json'
    signatures.consume_release(signatures.VerifiedArtifact('f'*64,'f'*40,'f'*64,100,'e'*64,1000),ledger,now=100)
    ledger.unlink()
    with pytest.raises(ValueError):signatures.recover_ledger(root,auth,pin,apply=True)
    changed={**auth,'nonce':'f'*64}
    with pytest.raises(ValueError):signatures.recover_ledger(root,changed,signatures.digest(signatures.canonical(changed)),apply=True)
    assert not ledger.exists()


@pytest.mark.parametrize('swap',['directory','file'])
def test_reclaim_post_rename_substitution_preserves_unrelated_state(tmp_path,swap):
    root=tmp_path.resolve();ids=releases(root);rid=ids[0]
    report=receiver.release_inventory(root,now=100000)
    def activity(path):
        if path.name=='.reclaim-'+rid:
            if swap=='directory':
                path.rename(root/'saved-original');path.mkdir(mode=0o700)
                target=path/'unrelated'
            else:
                (path/'code').unlink();target=path/'code'
            target.write_bytes(b'unrelated');target.chmod(0o600)
        return True
    with pytest.raises(ValueError):receiver.reclaim_releases(root,[rid],expected_inventory=report['inventory_sha256'],apply=True,activity=activity,jobs=lambda:set(),now=100000)
    target=root/('.reclaim-'+rid)/('unrelated' if swap=='directory' else 'code')
    assert target.read_bytes()==b'unrelated'


def test_interrupted_reclamation_resumes_only_approved_remaining_tree(tmp_path):
    root=tmp_path.resolve();ids=releases(root);rid=ids[0]
    report=receiver.release_inventory(root,now=100000)
    def interrupt(path):
        if path.name=='.reclaim-'+rid:raise InterruptedError('synthetic interruption')
        return True
    kwargs=dict(expected_inventory=report['inventory_sha256'],apply=True,jobs=lambda:set(),now=100000)
    with pytest.raises(InterruptedError):receiver.reclaim_releases(root,[rid],activity=interrupt,**kwargs)
    assert (root/('.reclaim-'+rid)).is_dir()
    assert receiver.reclaim_releases(root,[rid],activity=lambda p:True,**kwargs)['removed']==[rid]
    assert receiver.reclaim_releases(root,[rid],activity=lambda p:True,**kwargs)['removed']==[rid]
    assert all((root/i).is_dir() for i in ids[1:])


def test_management_recovery_holds_same_lock_as_restart(tmp_path,monkeypatch):
    home=tmp_path.resolve();root=home/'.wisp-mini';root.mkdir(mode=0o700);releases(root)
    monkeypatch.setattr(receiver.Path,'home',lambda:home)
    monkeypatch.setattr(receiver,'active_jobs',lambda:set())
    def child(argv):
        with pytest.raises(BlockingIOError):
            with signatures.publication_lock(root):pass
        assert 'recover-state' in argv
        return b'{"schema_version":1,"status":"complete","originals":"preserved"}'
    monkeypatch.setattr(receiver,'management_read',child)
    request={'schema_version':1,'node_id':'nFIXTURE','ip':'100.64.0.10','operation':'recover-state','apply':True,
             'arguments':{'operation_id':'a'*64,'destination':'recovered-fixture','inventory_sha256':'b'*64,'fresh_empty':False}}
    assert receiver.manage(request,verify_identity=lambda p:None)['status']=='complete'
    monkeypatch.setattr(receiver,'management_read',lambda a:pytest.fail('repeated operation'))
    assert receiver.manage(request,verify_identity=lambda p:None)['status']=='complete'




@pytest.mark.parametrize('boundary',[1,2])
def test_interrupted_intent_publication_permits_fresh_reviewed_recovery(tmp_path,monkeypatch,boundary):
    root=tmp_path.resolve();root.chmod(0o700)
    path=root/'artifact-releases.json.lock';path.write_bytes(b'1');path.chmod(0o600)
    report=signatures.ledger_doctor(root)
    auth={'schema_version':1,'operation':'recover-ledger','root':str(root),'uid':os.getuid(),
          'sentinel_sha256':report['sentinel_sha256'],'sequence_floor':42,'statement_sha256':'a'*64,
          'independent_high_water_review':True,'nonce':'b'*64,'recovery_history_sha256':report['recovery_history_sha256']}
    original=os.fsync;calls=[]
    def fail(fd):
        calls.append(fd)
        if len(calls)==boundary:raise InterruptedError('synthetic intent interruption')
        return original(fd)
    with monkeypatch.context() as patch:
        patch.setattr(signatures.os,'fsync',fail)
        with pytest.raises(InterruptedError):signatures.recover_ledger(root,auth,signatures.digest(signatures.canonical(auth)),apply=True)
    fresh=signatures.ledger_doctor(root)
    assert fresh['status']=='recovery_required'
    new={**auth,'nonce':'c'*64,'sequence_floor':100,'recovery_history_sha256':fresh['recovery_history_sha256']}
    assert signatures.recover_ledger(root,new,signatures.digest(signatures.canonical(new)),apply=True)['status']=='complete'


@pytest.mark.parametrize('mutation',['header','frame','truncated','trailing','commit'])
def test_damaged_wal_is_never_published(tmp_path,mutation):
    source=tmp_path.resolve()/'state';source.mkdir(mode=0o700)
    db=sqlite3.connect(source/'node.sqlite3')
    db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
    db.execute('CREATE TABLE synthetic(value TEXT)');db.execute("INSERT INTO synthetic VALUES('evidence')");db.commit()
    raw=bytearray((source/'node.sqlite3-wal').read_bytes());db.close()
    if mutation=='header':raw[24]^=1
    if mutation=='frame':raw[56]^=1
    if mutation=='truncated':raw=raw[:-1]
    if mutation=='trailing':raw+=b'x'
    if mutation=='commit':
        size=int.from_bytes(raw[8:12],'big');offset=len(raw)-(size+24)+4
        assert int.from_bytes(raw[offset:offset+4],'big')>0
        raw[offset:offset+4]=b'\x00'*4
    with pytest.raises(StoreUnavailable):recovery.reconstruct_wal(bytes(raw))
