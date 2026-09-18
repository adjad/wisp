"""Full exported policy and immutable backup proof, no Tailnet publication."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'infra/mac-mini'))
import policy
import node_prep

OWNER='owner@example.invalid'
INVENTORY={'schema_version':1,'complete':True,'devices':[
    {'id':'mini','ips':['100.64.0.10'],'tags':[policy.TAG]},
    {'id':'air','ips':['100.64.0.20'],'tags':['tag:air']} ]}
AIR={'tagOwners':{'tag:air':[OWNER]},'groups':{'group:air-users':[OWNER]},'hosts':{'air':'100.64.0.20'},
     'grants':[{'src':[OWNER],'dst':['100.64.0.20'],'ip':['tcp:443']}],
     'acls':[{'action':'accept','src':[OWNER],'dst':['100.64.0.20:8765'],'proto':'tcp'}],
     'ssh':[{'action':'check','src':[OWNER],'dst':['100.64.0.20'],'users':['airuser'],'checkPeriod':'always'}],
     'tests':[{'src':'100.64.0.30','proto':'tcp','accept':['100.64.0.20:443'],'deny':['100.64.0.20:22']}],
     'sshTests':[{'src':OWNER,'dst':['100.64.0.20'],'check':['airuser'],'deny':['root']}]}


def test_air_policy_is_preserved_with_complete_inventory():
    original=copy.deepcopy(AIR)
    combined=policy.render(AIR,OWNER,'miniuser')
    assert AIR==original
    assert policy.review(combined,OWNER,'miniuser',INVENTORY)==[]
    assert policy.render(combined,OWNER,'miniuser')==combined
    for key in ('grants','acls','ssh','tests','sshTests'):
        assert combined[key][:len(AIR[key])]==AIR[key]
    assert policy.review(combined,OWNER,'miniuser')


@pytest.mark.parametrize('mutation',[
    lambda p:p['grants'][0].update(dst=[policy.TAG]),
    lambda p:p['grants'][0].update(dst=['100.64.0.10']),
    lambda p:p['grants'][0].update(dst=['tag:air']),
    lambda p:p['grants'][0].update(dst=['air']),
    lambda p:p['grants'][0].update(dst=['100.64.0.0/10']),
    lambda p:p['grants'][0].update(src=['group:air-users']),
    lambda p:p['grants'][0].update(ip=['*']),
    lambda p:p['hosts'].update(air='100.64.0.10'),
    lambda p:p.update(autoApprovers={'routes':{'0.0.0.0/0':[policy.TAG]}}),
    lambda p:p.update(ipsets={'ipset:air':['100.64.0.10']}),
    lambda p:p['tagOwners'].update({policy.TAG:['other@example.invalid']}),
    lambda p:p.update(nodeAttrs=[{'target':[policy.TAG],'attr':['funnel']}]),
])
def test_overlap_and_indirect_syntax_refuse(mutation):
    combined=policy.render(AIR,OWNER,'miniuser');mutation(combined)
    assert policy.review(combined,OWNER,'miniuser',INVENTORY)


def test_multitag_device_is_not_disjoint():
    inventory=copy.deepcopy(INVENTORY);inventory['devices'][1]['tags'].append(policy.TAG)
    assert policy.review(policy.render(AIR,OWNER,'miniuser'),OWNER,'miniuser',inventory)


def test_before_after_hashes_bind_plan_and_unchanged_rules():
    plan={'tailnet_user':OWNER,'user':'miniuser','policy_inventory':INVENTORY}
    after=policy.render(AIR,OWNER,'miniuser')
    evidence=policy.backup_evidence(json.dumps(AIR),json.dumps(after),plan)
    assert policy.verify_backups(evidence,after,plan)
    for key in ('before','after','before_sha256','after_sha256','plan_sha256'):
        bad=copy.deepcopy(evidence);bad[key]+=' '
        assert not policy.verify_backups(bad,after,plan)
    assert not policy.verify_backups(evidence,after,{**plan,'user':'different'})
    after['grants'].pop(0)
    assert not policy.verify_backups(evidence,after,plan)


def test_duplicate_json_keys_refuse(tmp_path):
    source=tmp_path/'policy.json';source.write_text('{"grants":[],"grants":[]}')
    with pytest.raises(node_prep.Refused):node_prep.read_json(source)


def bound_fixture():
    import hashlib
    digest=lambda v:hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    root=Path(__file__).resolve().parents[1]/'infra/mac-mini'
    snapshot=json.loads((root/'preflight.fixture.json').read_text())
    plan=json.loads((root/'plan.example.json').read_text())
    snapshot['tailscale']['Self'].update(ID='primary',Tags=[])
    snapshot['tailscale']['Peer']['air']={'ID':'air','TailscaleIPs':['100.64.0.20'],'Tags':['tag:air']}
    plan['policy_inventory']={'schema_version':1,'complete':True,'devices':[
        {'id':'primary','ips':[policy.PRIMARY],'tags':[]},
        {'id':plan['node_id'],'ips':[plan['ip']],'tags':[policy.TAG]},
        {'id':'air','ips':['100.64.0.20'],'tags':['tag:air']}]}
    combined=policy.render(AIR,plan['tailnet_user'],plan['user'])
    before=json.dumps(AIR);after=json.dumps(combined)
    approval={'schema_version':1,'source':'independent-admin-export','complete_export_reviewed':True,
              'target':{k:plan[k] for k in ('host','node_id','ip')},
              'inventory_sha256':digest(plan['policy_inventory']),'observed_sha256':digest(policy.security_projection(snapshot['tailscale'])),
              'exported_policy_sha256':hashlib.sha256(before.encode()).hexdigest(),'issued_at':100,'expires_at':200}
    snapshot['policy_inventory_approval']=approval
    plan['policy_inventory_approval_sha256']=digest(approval)
    snapshot['policy_backups']=policy.backup_evidence(before,after,plan)
    return snapshot,plan,combined


def test_complete_independently_pinned_inventory_binds_preflight():
    snapshot,plan,combined=bound_fixture()
    assert all(node_prep.preflight(snapshot,plan,combined,now=150).values())
    snapshot['tailscale']['Peer']['air'].update(TxBytes=500,RxBytes=100,LastWrite='new',LastHandshake='new')
    assert all(node_prep.preflight(snapshot,plan,combined,now=150).values())


@pytest.mark.parametrize('change',['target-confusion','new-tag','secondary-ip','new-peer','missing-peer','stale',
                                   'future','host','export','approval','incomplete'])
def test_inventory_target_freshness_and_observation_confusion_refuse(change):
    snapshot,plan,combined=bound_fixture();now=150
    if change=='target-confusion':
        plan['policy_inventory']['devices'][1]['tags']=['tag:air']
        plan['policy_inventory']['devices'][2]['tags']=[policy.TAG]
        combined['grants'].append({'src':['other@example.invalid'],'dst':[plan['ip']],'ip':['tcp:443']})
    if change=='new-tag':snapshot['tailscale']['Peer']['air']['Tags'].append(policy.TAG)
    if change=='secondary-ip':snapshot['tailscale']['Self']['TailscaleIPs'].append('fd7a:115c:a1e0::1')
    if change=='new-peer':snapshot['tailscale']['Peer']['new']={'ID':'new','TailscaleIPs':['100.64.0.22'],'Tags':[]}
    if change=='missing-peer':snapshot['tailscale']['Peer'].pop('air')
    if change=='stale':now=200
    if change=='future':now=99
    if change=='host':plan['host']='different.example-tailnet.ts.net'
    if change=='export':snapshot['policy_backups']['before_sha256']='f'*64
    if change=='approval':plan['policy_inventory_approval_sha256']='f'*64
    if change=='incomplete':snapshot['policy_inventory_approval']['complete_export_reviewed']=False
    assert not policy.inventory_binding(snapshot,plan,now=now)


def test_all_primary_addresses_protected_from_disjoint_exceptions():
    inventory=copy.deepcopy(INVENTORY)
    inventory['devices'].append({'id':'primary','ips':[policy.PRIMARY,'fd7a:115c:a1e0::1'],'tags':[]})
    assert 'fd7a:115c:a1e0::1' not in policy.disjoint_context(inventory)
