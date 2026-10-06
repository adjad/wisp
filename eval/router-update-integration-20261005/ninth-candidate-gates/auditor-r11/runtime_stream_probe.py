import sys,json,asyncio,tempfile
from pathlib import Path
from types import SimpleNamespace
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-67e4cef-gates-20261005/auditor')
sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
HEAD=e.git_revision();assert HEAD=='67e4cefa53687f265287b9d068fc549c1f7955b6'
base=Path(tempfile.mkdtemp(prefix='stream-fixture-',dir=OUT));home=base/'original-home';state=base/'state';home.mkdir(mode=0o700);state.mkdir(mode=0o700);(home/'.moe').mkdir(mode=0o700)
lock=home/'.moe/.provisioning.lock';lock.write_bytes(b'auditor synthetic lock');lock.chmod(0o600)
Path.home=classmethod(lambda cls:home)
grant=e.InferenceGrant('Ling-AuditorFixture','synthetic-revision','a'*64,'synthetic-not-runtime-admission',('127.0.0.1',8000),True)
cap=e.RuntimeCapability(grant,end_utc='2099-01-01T00:00:00+00:00',execution_receipt='synthetic-only',enabled=True)
e.install_guard(state,OUT,inference_grant=grant,runtime_capability=cap)
class Fake:
 _client=SimpleNamespace(event_hooks={})
 async def stream_events(self,*a,**k):
  yield dict(kind='content',text='Synthetic frame one')
  yield dict(kind='content',text='Synthetic frame two')
 async def aclose(self):pass
f=Fake();wrapped=e._ScopedResidentClient(f,cap)
def policy_event():
 try:sys.audit('socket.connect',object(),('127.0.0.1',8000))
 except PermissionError:return 'denied'
 return 'admitted'
async def run():
 before=dict(phase=cap._current_phase(),socket_policy_only=policy_event(),home=str(Path.home()))
 stream=wrapped.stream_events(grant.model,[])
 first=await anext(stream)
 between=dict(phase=cap._current_phase(),socket_policy_only=policy_event(),home=str(Path.home()))
 await stream.aclose()
 after=dict(phase=cap._current_phase(),socket_policy_only=policy_event(),home=str(Path.home()),busy=cap._busy)
 return dict(head=HEAD,mode='inert fake client; sys.audit policy events only; no socket/HTTP/native process/auth read',before=before,first=first,between=between,after=after,raw=cap.raw,pass_=between['phase'] is None and between['socket_policy_only']=='denied')
r=asyncio.run(run());(OUT/'runtime-stream-probe.json').write_text(json.dumps(r,indent=2));print(json.dumps(r))
