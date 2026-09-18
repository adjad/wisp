"""Disabled RC-first update controller; no inference or compatibility probes.

Official release source: https://github.com/jundot/omlx/releases . Origin and
artifact authenticity are separate from successful process startup. The live
supervisor adapter is deliberately unavailable in this preparation package.
"""
import hashlib
import json
import re
import time
from pathlib import Path

UPDATE_ORIGIN = 'https://github.com/jundot/omlx'
UPDATE_RISK = 'RC incompatibility and performance risk accepted; no preactivation application testing'
UPDATE_HEX = re.compile('[0-9a-f]{64}')


def update_digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def select_omlx_release(catalog):
    """A complete official catalog; dev/beta/alpha releases are not RCs."""
    if (not isinstance(catalog,dict) or set(catalog)!={'schema_version','origin','complete','releases'}
            or type(catalog['schema_version']) is not int or catalog['schema_version']!=1
            or catalog['origin']!=UPDATE_ORIGIN or catalog['complete'] is not True
            or not isinstance(catalog['releases'],list) or not 1<=len(catalog['releases'])<=10000):
        raise ValueError('official_complete_release_catalog_required')
    rc=[];stable=[];ids=set();versions=set()
    for row in catalog['releases']:
        if (not isinstance(row,dict) or set(row)!={'id','version','published_at','prerelease','url','artifact_sha256'}
                or type(row['id']) is not int or row['id']<=0 or row['id'] in ids
                or type(row['published_at']) is not int or row['published_at']<0
                or type(row['prerelease']) is not bool or not isinstance(row['version'],str)
                or not re.fullmatch(r'v?[0-9]+\.[0-9]+\.[0-9]+(?:[.\-]?(?:rc|dev|beta|alpha)[0-9]+)?',row['version'])
                or row['version'] in versions or not isinstance(row['artifact_sha256'],str)
                or not UPDATE_HEX.fullmatch(row['artifact_sha256'])
                or not isinstance(row['url'],str) or not re.fullmatch(
                    re.escape(UPDATE_ORIGIN+'/releases/download/'+row['version']+'/')+r'[A-Za-z0-9_.-]+',row['url'])):
            raise ValueError('invalid_official_release_catalog')
        ids.add(row['id']);versions.add(row['version'])
        if re.fullmatch(r'v?[0-9]+\.[0-9]+\.[0-9]+[.\-]?rc[0-9]+',row['version']):
            if not row['prerelease']:raise ValueError('release_channel_conflict')
            rc.append(row)
        elif re.fullmatch(r'v?[0-9]+\.[0-9]+\.[0-9]+',row['version']):
            if row['prerelease']:raise ValueError('release_channel_conflict')
            stable.append(row)
    candidates=rc or stable
    if not candidates:raise ValueError('no_rc_or_stable_release')
    selected=max(candidates,key=lambda r:(r['published_at'],r['id']))
    return {'schema_version':1,'release':dict(selected),'channel':'rc' if rc else 'stable',
            'catalog_sha256':update_digest(catalog),'risk_acceptance':UPDATE_RISK,
            'application_behavior':'unverified','updates_enabled':False}


def omlx_update_status(root, read_document):
    path=Path(root)/'omlx-update-state.json'
    try:state=read_document(path)
    except FileNotFoundError:state=None
    operation=None
    if state is not None:
        validate_update_state(state)
        if state['operation'] is not None:
            operation=read_document(Path(root)/('.omlx-operation-'+state['operation']+'.json'))
    return {'schema_version':1,'status':'complete','updates_enabled':False,
            'live_adapter':'unavailable','state':state,'operation':operation,'risk_acceptance':UPDATE_RISK,
            'application_behavior':'unverified'}


def validate_update_state(state):
    if (not isinstance(state,dict) or set(state)!={'schema_version','generation','active','previous','operation','phase'}
            or type(state['schema_version']) is not int or state['schema_version']!=1 or type(state['generation']) is not int or state['generation']<0
            or state['phase'] not in ('idle','switching','running','rolled_back','recovery_required')
            or state['operation'] is not None and (not isinstance(state['operation'],str) or not UPDATE_HEX.fullmatch(state['operation']))):
        raise ValueError('update_state_unproven')
    for role in ('active','previous'):
        value=state[role]
        if value is not None and (not isinstance(value,dict)
                or set(value)!={'version','artifact_sha256','runtime_manifest_sha256','release_id'}
                or any(not isinstance(value[k],str) or not UPDATE_HEX.fullmatch(value[k]) for k in ('artifact_sha256','runtime_manifest_sha256','release_id'))
                or not isinstance(value['version'],str)):
            raise ValueError('update_receipt_unproven')


class PreparedUpdateController:
    """Durable operational protocol exercised only with an isolated adapter.

    Adapter staging verifies official artifact authenticity plus the downloaded
    digest, materializes an immutable complete tree, and records its hash. Its
    switch must atomically bind the supervisor and runtime authorization to one
    generation. No adapter is auto-selected; no live implementation is shipped.
    """
    def __init__(self,root,*,lock,read_document,write_document):
        self.root=Path(root);self.lock=lock;self.read=read_document;self.write=write_document

    def state(self):
        state=omlx_update_status(self.root,self.read)['state']
        if state is None:raise ValueError('initial_supervisor_binding_required')
        return state

    def stage(self,catalog,artifact,*,adapter=None,simulate=False):
        selected=select_omlx_release(catalog)
        if simulate is not True or adapter is None or getattr(adapter,'simulation_only',False) is not True:
            raise ValueError('live_update_adapter_unavailable')
        if not isinstance(artifact,bytes) or hashlib.sha256(artifact).hexdigest()!=selected['release']['artifact_sha256']:
            raise ValueError('update_download_integrity_failed')
        with self.lock(self.root):
            state=self.state()
            if state['phase'] not in ('idle','running','rolled_back'):raise ValueError('update_recovery_required')
            # No stable fallback if the selected RC fails any safeguard.
            if adapter.authentic(selected['release'],artifact) is not True:
                raise ValueError('update_artifact_authenticity_failed')
            if adapter.disk_admission(len(artifact),[state['active'],state['previous']]) is not True:
                raise ValueError('update_disk_admission_failed')
            receipt=adapter.stage_immutable(selected['release'],artifact)
            candidate={**state,'active':receipt}
            validate_update_state(candidate)
            if receipt['version']!=selected['release']['version'] or receipt['artifact_sha256']!=selected['release']['artifact_sha256']:
                raise ValueError('update_receipt_mismatch')
            if adapter.tree_matches(receipt) is not True:raise ValueError('update_tree_changed')
            staged={'schema_version':1,'receipt':receipt,'selection':selected,
                    'authenticity':'verified_by_isolated_adapter','execution':'simulation_only'}
            self.write(self.root,'.omlx-staged-'+update_digest(receipt)+'.json',staged)
            return {'schema_version':1,'receipt':receipt,'risk_acceptance':UPDATE_RISK,'application_behavior':'unverified'}

    def switch(self,receipt,*,expected_generation,operation_id,adapter=None,simulate=False,rollback=False):
        if simulate is not True or adapter is None or getattr(adapter,'simulation_only',False) is not True:
            raise ValueError('live_update_adapter_unavailable')
        if not isinstance(operation_id,str) or not UPDATE_HEX.fullmatch(operation_id):raise ValueError('update_operation_required')
        if type(expected_generation) is not int or expected_generation<0 or type(rollback) is not bool:
            raise ValueError('update_generation_conflict')
        with self.lock(self.root):
            state=self.state()
            name='.omlx-operation-'+operation_id+'.json'
            request={'receipt':receipt,'expected_generation':expected_generation,'rollback':rollback}
            try:consumed=self.read(self.root/name)
            except FileNotFoundError:consumed=None
            if consumed is not None:
                if consumed.get('request')!=request:raise ValueError('update_operation_conflict')
                if consumed.get('status')=='complete' and consumed.get('result')==state:return state
                raise ValueError('update_recovery_required')
            if (state['generation']!=expected_generation or state['phase'] not in ('idle','running','rolled_back')
                    or rollback and receipt!=state['previous']):raise ValueError('update_generation_conflict')
            validate_update_state({**state,'active':receipt})
            if not rollback:
                try:staged=self.read(self.root/('.omlx-staged-'+update_digest(receipt)+'.json'))
                except FileNotFoundError:raise ValueError('authenticated_stage_required') from None
                if (staged.get('schema_version')!=1 or staged.get('receipt')!=receipt
                        or staged.get('authenticity')!='verified_by_isolated_adapter' or staged.get('execution')!='simulation_only'
                        or staged.get('selection',{}).get('release',{}).get('version')!=receipt['version']
                        or staged.get('selection',{}).get('release',{}).get('artifact_sha256')!=receipt['artifact_sha256']):
                    raise ValueError('authenticated_stage_required')
            if adapter.tree_matches(receipt) is not True:raise ValueError('update_tree_changed')
            prior=state['active'];generation=state['generation']+1
            record={'schema_version':1,'status':'started','request':request,'prior':state,
                    'target':receipt,'expected_generation':expected_generation,'target_generation':generation}
            # Complete, atomically published intent precedes both state and
            # supervisor mutation. Every ID remains consumed across later runs.
            self.write(self.root,name,record)
            pending={**state,'operation':operation_id,'phase':'switching'}
            self.write(self.root,'omlx-update-state.json',pending)
            deadline=time.monotonic()+30
            try:
                # These methods inspect process lifecycle only. They must not
                # send prompts, load models, or call application endpoints.
                result=adapter.atomic_switch(expected_generation,receipt,generation,deadline)
                if result!='switched' or time.monotonic()>deadline:raise ValueError('update_switch_inconclusive')
                observed=adapter.observe_process(generation,deadline)
                if time.monotonic()>deadline:raise ValueError('update_process_observation_inconclusive')
                if observed=='running_bound':
                    final={'schema_version':1,'generation':generation,'active':receipt,'previous':prior,
                           'operation':operation_id,'phase':'rolled_back' if rollback else 'running'}
                elif observed in ('failed_start','exited','failed_binding'):
                    if prior is None or adapter.atomic_switch(generation,prior,generation+1,deadline)!='switched':
                        raise ValueError('update_rollback_inconclusive')
                    if adapter.observe_process(generation+1,deadline)!='running_bound':raise ValueError('update_rollback_inconclusive')
                    if time.monotonic()>deadline:raise ValueError('update_rollback_inconclusive')
                    final={'schema_version':1,'generation':generation+1,'active':prior,'previous':receipt,
                           'operation':operation_id,'phase':'rolled_back'}
                else:
                    # Unknown observations do not authorize automatic rollback.
                    raise ValueError('update_process_observation_inconclusive')
                self.write(self.root,'omlx-update-state.json',final)
                record.update(status='complete',result=final);self.write(self.root,name,record)
                return final
            except BaseException:
                pending['phase']='recovery_required'
                self.write(self.root,'omlx-update-state.json',pending)
                record['status']='recovery_required';self.write(self.root,name,record)
                raise


def manage_omlx_update(root,operation,arguments,read_document):
    """Independent SSH status/selection remain usable with all services down."""
    if operation=='omlx-status':
        if arguments:raise ValueError('invalid_update_arguments')
        return omlx_update_status(root,read_document)
    if operation=='omlx-select':
        if set(arguments)!={'catalog'}:raise ValueError('invalid_update_arguments')
        return {'status':'complete',**select_omlx_release(arguments['catalog'])}
    if operation in ('omlx-stage','omlx-update','omlx-rollback'):
        return {'schema_version':1,'status':'blocked','error':'live_update_adapter_unavailable',
                'updates_enabled':False,'application_behavior':'unverified'}
    raise ValueError('unsupported_update_operation')
