import sys,os,json,asyncio,tempfile,copy,traceback,fcntl,time
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-67e4cef-gates-20261005/auditor')
sys.path.insert(0,str(ROOT));from scripts import eval_router_update as e
SHA=e.git_revision();assert SHA=='67e4cefa53687f265287b9d068fc549c1f7955b6'
mode=sys.argv[1] if len(sys.argv)>1 else 'stream'
base=Path(tempfile.mkdtemp(prefix='wisp-67e4cef-runtime-'+mode+'-',dir=OUT)).resolve();home=base/'fixture-home';state=base/'isolated';home.mkdir(mode=0o700);state.mkdir(mode=0o700)
(home/'.moe').mkdir(mode=0o700);(home/'.omlx').mkdir(mode=0o700)
lock=home/'.moe/.provisioning.lock';lock.write_bytes(b'Independent synthetic lease pin');lock.chmod(0o600)
model='Ling-Independent-Auditor';roles=['router','fast','agent','general','coding','reasoning']
import yaml,httpx
(home/'.moe/config.yaml').write_text(yaml.safe_dump(dict(roles=dict.fromkeys(roles,model),inference=dict(bindings={r:dict(endpoint='local',model_id=model,revision='auditor-revision',context_window=4096) for r in roles}))))
(home/'.omlx/settings.json').write_text(json.dumps(dict(auth=dict(api_key='auditor-inert-fixture-value'),server=dict(host='127.0.0.1',port=8000))))
(home/'.omlx/model_settings.json').write_text(json.dumps(dict(models={model:dict(max_context_window=4096)})))
if mode=='marker':(home/'.moe/.helper-transaction.json').write_text('{}')
if mode=='managed':(home/'.moe/omlx-runtime-authorization.json').write_text('{}')
if mode=='symlink':
 lock.unlink();lock.symlink_to(base/'unrelated')
if mode=='missing':lock.unlink()
Path.home=classmethod(lambda cls:home)
grant=e.InferenceGrant(model,'auditor-revision','c'*64,'OFFLINE-FIXTURE-NOT-RUNTIME-ADMISSION',('127.0.0.1',8000),True)
cap=e.RuntimeCapability(grant,end_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),execution_receipt='INERT-ONLY',enabled=True)
e.install_guard(state,OUT,inference_grant=grant,runtime_capability=cap)
from service import config as cfg
from service.config import quarantine
from service.config.endpoints import role_target
from service.inference import omlx_client
saved_constants=(cfg.USER_CONFIG,cfg.OMLX_SETTINGS,cfg.OMLX_MODEL_SETTINGS)
seen=[];rows=[];raw={}
class FakeSupported:
 def __init__(self,*,target):
  assert target.endpoint.api_key()=='auditor-inert-fixture-value'
  assert quarantine._gate.directory==home/'.moe'
  self.target=target;self.base_url=target.endpoint.base_url;self.endpoint_name=target.endpoint.name;self.api_prefix=target.endpoint.api_prefix;self.provider=SimpleNamespace(name='omlx');self.managed=True
  self._credential_transport=SimpleNamespace(origin=httpx.URL(self.base_url),backend=object())
  def reply(req):
   seen.append(dict(method=req.method,path=req.url.path,auth_present=bool(req.headers.get('Authorization'))))
   return httpx.Response(200,json=dict(models=[dict(id=model,loaded=True)]))
  self._client=quarantine.guard_client(httpx.AsyncClient(base_url=self.base_url,headers={'Authorization':'Bearer auditor-inert-fixture-value'},transport=httpx.MockTransport(reply)))
 async def status(self):return (await self._client.get('/v1/models/status')).json()
 async def chat(self,name,messages,**kw):
  await self._client.post('/v1/chat/completions',json=dict(model=name,messages=messages,**kw))
  if kw.get('wait'):await asyncio.Event().wait()
  return dict(synthetic=True)
 async def stream_events(self,name,messages,**kw):
  await self._client.post('/v1/chat/completions',json=dict(model=name,messages=messages))
  try:
   yield dict(kind='content',text='Independent frame')
   if kw.get('wait'):await asyncio.Event().wait()
   yield dict(kind='content',text='Second frame')
  finally:seen.append(dict(stream_finalized=True))
 async def aclose(self):
  seen.append(dict(closed=True));await self._client.aclose()
omlx_client.OMLXClient=FakeSupported
if mode=='identity':
 class Bad(FakeSupported):
  def __init__(self,*,target):super().__init__(target=target);self.base_url='http://127.0.0.1:2'
 omlx_client.OMLXClient=Bad

def record(label,actual,expected=True):
 rows.append(dict(id=label,actual=actual,expected=expected,pass_=actual==expected))
def policy():
 try:sys.audit('socket.connect',object(),('127.0.0.1',8000))
 except PermissionError:return 'denied'
 return 'admitted'
def denycall(f):
 try:f()
 except PermissionError:return True
 return False
async def exercise():
 if mode in ['missing','symlink','marker','managed','identity']:
  try:
   async with e.supported_resident_client(cap):record('factory-failure-rejected',False)
  except (PermissionError,FileNotFoundError,quarantine.CredentialQuarantined) as ex:
   record('factory-failure-rejected',True);raw['exception_class']=type(ex).__name__
  record('no-dispatch-before-factory-failure',not any('method' in x for x in seen))
  record('owned-client-closed-if-constructed',seen==[dict(closed=True)] if mode=='identity' else seen==[])
 else:
  async with e.supported_resident_client(cap) as supplied:
   raw['captured_target']=dict(role=supplied.target.role,model=supplied.target.model,revision=supplied.target.revision,context=supplied.target.context_window)
   record('configuration-constants-restored-in-body',(cfg.USER_CONFIG,cfg.OMLX_SETTINGS,cfg.OMLX_MODEL_SETTINGS)==saved_constants)
   record('isolated-config-no-auth-material','auditor-inert-fixture-value' not in (state/'.moe/config.yaml').read_text())
   record('no-load-or-start-method',not hasattr(supplied,'ensure_only') and not hasattr(supplied,'start'))
   record('default-caller-socket-denied',policy(),'denied')
   adapter=e.ResidentInferenceAdapter(supplied,grant)
   record('supported-status-auth-delegation',(await adapter.status())['models'][0]['loaded'])
   record('metadata-transport-is-borrowed',adapter._credential_transport is supplied._credential_transport)
   if mode=='stream':
    raw['before']=dict(phase=cap._current_phase(),policy=policy(),home=str(Path.home()))
    stream=adapter.stream_events(model,[dict(role='user',content='Independent synthetic request')])
    raw['first']=await anext(stream)
    raw['between']=dict(phase=cap._current_phase(),policy=policy(),home=str(Path.home()))
    record('caller-after-yield-private-phase-denied',cap._current_phase() is None)
    record('caller-after-yield-socket-policy-denied',policy(),'denied')
    hold=asyncio.Event()
    async def inherited():
     await hold.wait()
     return dict(phase=cap._current_phase(),policy=policy(),home=str(Path.home()))
    child=asyncio.create_task(inherited())
    await stream.aclose();hold.set();raw['child-after-close']=await child
    record('child-after-close-phase-denied',raw['child-after-close']['phase'] is None)
    record('child-after-close-socket-policy-denied',raw['child-after-close']['policy'],'denied')
    record('parent-after-close-restored',cap._current_phase() is None and policy()=='denied' and not cap._busy)
    # Confirm caller cancellation finalizes the owned generator and lease.
    entered=asyncio.Event()
    async def caller():
     s=supplied.stream_events(model,[],wait=True)
     try:
      await anext(s);entered.set();await asyncio.Event().wait()
     finally:await s.aclose()
    task=asyncio.create_task(caller());await entered.wait();task.cancel()
    try:await task
    except asyncio.CancelledError:record('caller-cancellation-propagates',True)
    else:record('caller-cancellation-propagates',False)
    record('caller-cancellation-clears-busy-and-parent-phase',not cap._busy and cap._current_phase() is None)
    raw['stream_raw']=copy.deepcopy(cap.raw)
   elif mode=='cancel':
    for operation in ['cancel','deadline','revoke','busy']:
     start=asyncio.Event();original=supplied._client.chat
     async def waiting(*a,**kw):start.set();await asyncio.Event().wait()
     supplied._client.chat=waiting
     old_stop=cap.stop_monotonic
     task=asyncio.create_task(supplied.chat(model,[]));await start.wait()
     if operation=='busy':
      try:await supplied.status()
      except PermissionError:record('concurrent-request-denied',True)
      else:record('concurrent-request-denied',False)
      task.cancel()
     elif operation=='revoke':cap.revoked=True;task.cancel()
     elif operation=='deadline':
      # Active timeout was installed with old deadline; force cancellation here,
      # then verify the next operation respects an expired retained clock.
      task.cancel()
     else:task.cancel()
     try:await task
     except asyncio.CancelledError:record(operation+'-cancellation-propagates',True)
     else:record(operation+'-cancellation-propagates',False)
     record(operation+'-cleanup',not cap._busy and cap._current_phase() is None)
     cap.revoked=False;cap.stop_monotonic=old_stop;supplied._client.chat=original
    cap.stop_monotonic=e._REAL_MONOTONIC()+.03
    task=asyncio.create_task(supplied.chat(model,[],wait=True))
    try:await task
    except TimeoutError:record('real-clock-request-timeout',True)
    else:record('real-clock-request-timeout',False)
    record('timeout-record-is-honest',cap.raw[-1]['outcome']=='TimeoutError')
   elif mode=='policy':
    # No HTTP dispatch: exact policy checks on in-memory Request objects only.
    bads=[('POST','/v1/models/load',{}),('GET','/v1/chat/completions',None),('POST','/v1/chat/completions',dict(model='Ling-other')),('GET','/v1/models/status?probe=1',None)]
    with cap._phase('peer'):
     for method,path,body in bads:record('request-limit-'+method+path,denycall(lambda method=method,path=path,body=body:cap._request_allowed(httpx.Request(method,'http://127.0.0.1:8000'+path,json=body))))
     for url in ['http://localhost:8000/v1/models/status','http://127.0.0.1:8001/v1/models/status','http://user@127.0.0.1:8000/v1/models/status']:
      record('origin-'+url,denycall(lambda url=url:cap._request_allowed(httpx.Request('GET',url))))
     for p in [home/'Documents/private.txt',home/'.omlx/settings.json']:record('peer-read-denied-'+p.name,not cap._read_allowed(p))
     fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
     try:
      try:os.write(fd,b'mutation')
      except OSError:record('lock-lease-fd-is-readonly',True)
      else:record('lock-lease-fd-is-readonly',False)
     finally:os.close(fd)
     record('arbitrary-Popen-no-callsite',not cap._process_allowed(('/bin/ps',['/bin/ps','-ww','-p','1','-o','ppid=,uid=,comm='],None,dict(PATH='/usr/bin:/bin:/usr/sbin',LC_ALL='C'))))
     record('thread-context-propagates-while-owned',await asyncio.to_thread(cap._current_phase),'peer')
    record('thread-context-restores-after-phase',await asyncio.to_thread(cap._current_phase),None)
    for flags in [os.O_RDWR,os.O_RDONLY,os.O_CREAT|os.O_RDWR]:record('public-lock-open-'+str(flags),denycall(lambda flags=flags:os.open(lock,flags)))
    old_wall,old_mono=cap.stop_wall,cap.stop_monotonic
    with patch.object(e.time,'time',lambda:0):
     cap.stop_wall=e._REAL_WALL()-1;record('scenario-clock-cannot-extend-wall-admission',denycall(cap.check))
     cap.stop_wall=old_wall;cap.stop_monotonic=e._REAL_MONOTONIC()-1;record('monotonic-expiry-denied',denycall(cap.check))
    cap.stop_monotonic=old_mono
   elif mode=='race':
    staging=base/'replacement-lock';staging.write_bytes(b'Independent synthetic lease pin');staging.chmod(0o600)
    original_open=cap._original_open;opens=0
    def racing(path,flags,*a,**kw):
     nonlocal opens
     if Path(path)==lock:
      opens+=1
      if opens==2:lock.rename(lock.with_suffix('.old'));staging.rename(lock)
     return original_open(path,flags,*a,**kw)
    cap._original_open=racing
    count=len(seen)
    with cap._phase('peer'):
     record('replacement-race-denied-before-fd-handoff',denycall(lambda:os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)))
    record('race-policy-made-no-new-HTTP-dispatch',len(seen)==count)
    record('race-second-open-exercised',opens==2)
    cap._original_open=original_open
    lock.unlink();lock.with_suffix('.old').rename(lock)
   elif mode in ['generation','contention']:
    if mode=='generation':
     p=base/'generation';p.write_text('d'*64);p.chmod(0o600);p.rename(home/'.moe/.credential-generation')
    else:
     with cap._phase('peer'):fd=cap._original_open(lock,os.O_RDONLY|os.O_NOFOLLOW);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    count=len(seen)
    try:
     try:await supplied.status()
     except quarantine.CredentialQuarantined:record('genuine-gate-denied',True)
     else:record('genuine-gate-denied',False)
     record('genuine-gate-before-new-HTTP-bytes',len(seen)==count)
     if mode=='generation':record('genuine-gate-cleared-cached-auth',supplied._client._client.headers.get('Authorization') is None)
    finally:
     if mode=='contention':fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
  record('capability-revoked-after-factory',cap.revoked)
 record('all-constants-restored',(cfg.USER_CONFIG,cfg.OMLX_SETTINGS,cfg.OMLX_MODEL_SETTINGS)==saved_constants)
 record('projection-removed',not (state/'.moe/config.yaml').exists() and not (state/'.omlx/model_settings.json').exists())
 record('caller-policy-denied-after-factory',policy(),'denied')
 raw['calls']=seen;raw['capability_receipts']=cap.raw
try:asyncio.run(exercise())
except Exception as ex:
 if mode=='race' and isinstance(ex,PermissionError) and str(ex)=='Existing credential lock changed':
  record('changed-lock-cleanup-is-honest',True);record('race-capability-revoked',cap.revoked);record('race-state-restored',not (state/'.moe/config.yaml').exists() and (cfg.USER_CONFIG,cfg.OMLX_SETTINGS,cfg.OMLX_MODEL_SETTINGS)==saved_constants)
 else:rows.append(dict(id='unexpected-fixture-exception',pass_=False,error=repr(ex),trace=traceback.format_exc()))
result=dict(candidate_sha=SHA,mode=mode,fixture_root=str(base),synthetic_only=True,no_actual_sockets_native_or_production_auth=True,rows=rows,raw=raw,passed=sum(r['pass_'] for r in rows),total=len(rows))
(OUT/('runtime-'+mode+'-results.json')).write_text(json.dumps(result,indent=2,default=str));print(json.dumps(dict(mode=mode,passed=result['passed'],total=result['total'],failures=[r['id'] for r in rows if not r['pass_']])))
