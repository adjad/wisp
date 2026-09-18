"""RC selection and isolated process-lifecycle controls; no inference probes."""
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'infra/mac-mini'))
import omlx_update as updates
import receiver
import artifact_signature


def release(version,number,*,prerelease):
    return {'id':number,'version':version,'published_at':number,'prerelease':prerelease,
            'url':updates.UPDATE_ORIGIN+'/releases/download/'+version+'/runtime.tar.gz',
            'artifact_sha256':hashlib.sha256(b'artifact').hexdigest()}


def catalog(*rows):
    return {'schema_version':1,'origin':updates.UPDATE_ORIGIN,'complete':True,'releases':list(rows)}


def test_newest_rc_wins_even_when_stable_or_development_is_newer():
    rows=catalog(release('v1.0.0rc1',1,prerelease=True),release('v1.0.0rc2',2,prerelease=True),
                 release('v1.0.0',3,prerelease=False),release('v2.0.0.dev1',4,prerelease=True))
    assert updates.select_omlx_release(rows)['release']['version']=='v1.0.0rc2'
    rows['releases']=rows['releases'][2:]
    assert updates.select_omlx_release(rows)['release']['version']=='v1.0.0'
    rows['releases']=rows['releases'][1:]
    with pytest.raises(ValueError):updates.select_omlx_release(rows)


@pytest.mark.parametrize('kind',['origin','incomplete','artifact-origin','channel'])
def test_bad_release_inventory_refuses(kind):
    rows=catalog(release('v1.0.0rc1',1,prerelease=True))
    if kind=='origin':rows['origin']='https://example.invalid'
    if kind=='incomplete':rows['complete']=False
    if kind=='artifact-origin':rows['releases'][0]['url']='https://example.invalid/runtime.tar.gz'
    if kind=='channel':rows['releases'][0]['prerelease']=False
    with pytest.raises(ValueError):updates.select_omlx_release(rows)


class IsolatedProcessAdapter:
    simulation_only=True
    def __init__(self):self.observations=['running_bound'];self.calls=[];self.generation=0
    def authentic(self,release,artifact):return True
    def disk_admission(self,size,retained):self.retained=retained;return True
    def stage_immutable(self,release,artifact):
        return {'version':release['version'],'artifact_sha256':release['artifact_sha256'],
                'runtime_manifest_sha256':'b'*64,'release_id':'c'*64}
    def tree_matches(self,receipt):return True
    def atomic_switch(self,expected,receipt,generation,deadline):
        assert self.generation==expected
        self.generation=generation;self.calls.append(('switch',generation));return 'switched'
    def observe_process(self,generation,deadline):
        self.calls.append(('observe',generation));return self.observations.pop(0)


def setup(tmp_path):
    root=tmp_path.resolve();root.chmod(0o700)
    prior={'version':'v0.9.0','artifact_sha256':'d'*64,'runtime_manifest_sha256':'e'*64,'release_id':'f'*64}
    receiver.management_receipt(root,'omlx-update-state.json',{'schema_version':1,'generation':0,'active':prior,
                                 'previous':None,'operation':None,'phase':'idle'})
    controller=updates.PreparedUpdateController(root,lock=artifact_signature.publication_lock,
        read_document=receiver.private_document,write_document=receiver.management_receipt)
    adapter=IsolatedProcessAdapter()
    rows=catalog(release('v1.0.0rc1',1,prerelease=True),release('v1.0.0',2,prerelease=False))
    return root,controller,adapter,rows,prior


def test_selected_rc_is_staged_and_switches_process_without_application_probe(tmp_path):
    root,controller,adapter,rows,prior=setup(tmp_path)
    receipt=controller.stage(rows,b'artifact',adapter=adapter,simulate=True)['receipt']
    result=controller.switch(receipt,expected_generation=0,operation_id='a'*64,adapter=adapter,simulate=True)
    assert result['active']==receipt and result['previous']==prior and result['phase']=='running'
    assert adapter.calls==[('switch',1),('observe',1)]
    assert controller.switch(receipt,expected_generation=0,operation_id='a'*64,adapter=adapter,simulate=True)==result
    assert updates.omlx_update_status(root,receiver.private_document)['application_behavior']=='unverified'


@pytest.mark.parametrize('failure',['failed_start','exited','failed_binding','unknown'])
def test_only_process_start_liveness_binding_failure_rolls_back(tmp_path,failure):
    root,controller,adapter,rows,prior=setup(tmp_path)
    receipt=controller.stage(rows,b'artifact',adapter=adapter,simulate=True)['receipt']
    adapter.observations=[failure,'running_bound']
    if failure=='unknown':
        with pytest.raises(ValueError):controller.switch(receipt,expected_generation=0,operation_id='a'*64,adapter=adapter,simulate=True)
        assert adapter.calls==[('switch',1),('observe',1)]
        assert controller.state()['phase']=='recovery_required'
    else:
        result=controller.switch(receipt,expected_generation=0,operation_id='a'*64,adapter=adapter,simulate=True)
        assert result['active']==prior and result['previous']==receipt and result['phase']=='rolled_back'


@pytest.mark.parametrize('failure',['integrity','authenticity','disk','tree','generation','live'])
def test_operational_safeguard_failure_does_not_fallback_to_stable(tmp_path,failure):
    root,controller,adapter,rows,prior=setup(tmp_path)
    artifact=b'artifact'
    if failure=='integrity':artifact=b'changed'
    if failure=='authenticity':adapter.authentic=lambda *a:False
    if failure=='disk':adapter.disk_admission=lambda *a:False
    if failure=='tree':adapter.tree_matches=lambda *a:False
    with pytest.raises(ValueError):
        result=controller.stage(rows,artifact,adapter=adapter,simulate=failure!='live')
        controller.switch(result['receipt'],expected_generation=99,operation_id='a'*64,adapter=adapter,simulate=True)
    assert controller.state()['active']==prior and adapter.calls==[]


def test_remote_update_status_is_independent_and_live_actions_disabled(tmp_path,monkeypatch):
    home=tmp_path.resolve();root=home/'.wisp-mini';root.mkdir(mode=0o700)
    monkeypatch.setattr(receiver.Path,'home',lambda:home)
    monkeypatch.setattr(receiver,'management_read',lambda a:pytest.fail('service access'))
    request={'schema_version':1,'operation':'omlx-status','node_id':'nFIXTURE','ip':'100.64.0.10','arguments':{}}
    assert receiver.manage(request,verify_identity=lambda p:None)['updates_enabled'] is False
    request.update(operation='omlx-rollback',apply=True)
    assert receiver.manage(request,verify_identity=lambda p:None)['error']=='live_update_adapter_unavailable'


def test_unstaged_receipt_cannot_switch_even_if_tree_matches(tmp_path):
    root,controller,adapter,rows,prior=setup(tmp_path)
    receipt=adapter.stage_immutable(rows['releases'][0],b'artifact')
    with pytest.raises(ValueError,match='authenticated_stage_required'):
        controller.switch(receipt,expected_generation=0,operation_id='a'*64,adapter=adapter,simulate=True)
    assert adapter.calls==[] and controller.state()['active']==prior


def test_interrupted_switch_preserves_target_and_blocks_restart_replay(tmp_path):
    root,controller,adapter,rows,prior=setup(tmp_path)
    receipt=controller.stage(rows,b'artifact',adapter=adapter,simulate=True)['receipt']
    original=adapter.atomic_switch
    def interrupted(*args):
        original(*args);raise InterruptedError('synthetic crash after switch')
    adapter.atomic_switch=interrupted
    with pytest.raises(InterruptedError):controller.switch(receipt,expected_generation=0,operation_id='a'*64,adapter=adapter,simulate=True)
    restarted=updates.PreparedUpdateController(root,lock=artifact_signature.publication_lock,
        read_document=receiver.private_document,write_document=receiver.management_receipt)
    status=updates.omlx_update_status(root,receiver.private_document)
    assert status['state']['phase']=='recovery_required'
    assert status['operation']['target']==receipt and status['operation']['prior']['active']==prior
    assert status['operation']['expected_generation']==0 and status['operation']['target_generation']==1
    with pytest.raises(ValueError):restarted.switch(receipt,expected_generation=0,operation_id='b'*64,adapter=adapter,simulate=True)
    assert adapter.calls==[('switch',1)]


def test_older_operation_remains_consumed_after_later_update(tmp_path):
    root,controller,adapter,rows,prior=setup(tmp_path)
    first=controller.stage(rows,b'artifact',adapter=adapter,simulate=True)['receipt']
    controller.switch(first,expected_generation=0,operation_id='a'*64,adapter=adapter,simulate=True)
    second=controller.stage(catalog(release('v2.0.0rc1',3,prerelease=True)),b'artifact',adapter=adapter,simulate=True)['receipt']
    adapter.observations=['running_bound']
    controller.switch(second,expected_generation=1,operation_id='b'*64,adapter=adapter,simulate=True)
    count=len(adapter.calls)
    with pytest.raises(ValueError):controller.switch(first,expected_generation=0,operation_id='a'*64,adapter=adapter,simulate=True)
    with pytest.raises(ValueError):controller.switch(first,expected_generation=2,operation_id='a'*64,adapter=adapter,simulate=True)
    assert len(adapter.calls)==count and controller.state()['active']==second


def test_overdue_adapter_result_is_inconclusive_without_automatic_rollback(tmp_path,monkeypatch):
    root,controller,adapter,rows,prior=setup(tmp_path)
    receipt=controller.stage(rows,b'artifact',adapter=adapter,simulate=True)['receipt']
    times=iter([0,31]);monkeypatch.setattr(updates.time,'monotonic',lambda:next(times))
    with pytest.raises(ValueError,match='inconclusive'):
        controller.switch(receipt,expected_generation=0,operation_id='a'*64,adapter=adapter,simulate=True)
    assert adapter.calls==[('switch',1)]
    assert controller.state()['phase']=='recovery_required'
