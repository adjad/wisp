"""Isolated Wisp backend on :8775 with transport tracing + optional counterfactual patches.
MODE=none   : stock behaviour
MODE=cache  : RuntimeAuthority.load() returns ONE cached DesktopOmlx (skips per-connection tree walk)
MODE=off    : no attribution at all (floor)
"""
import os, sys, time, json, threading
MODE=os.environ.get('MODE','none')
TRACE=os.environ.get('TRACE','trace.jsonl')
os.environ['WISP_HOME']=os.path.abspath('iso_home')
os.environ['WISP_BACKEND_URL']='http://127.0.0.1:8775'
sys.path.insert(0,'/Users/adijain/Desktop/MOE_Project')
os.chdir('/Users/adijain/Desktop/MOE_Project')
from service.inference import attributed_transport as at
lock=threading.Lock()
def log(**kw):
    kw['t']=time.time()
    with lock, open(os.path.join('/private/tmp/claude-501/-Users-adijain-Desktop-MOE-Project/5e4ba776-e4b5-406d-82b3-56ab928e76bf/scratchpad',TRACE),'a') as f: f.write(json.dumps(kw)+'\n')
# --- counterfactual patches
if MODE=='cache':
    _orig=at.RuntimeAuthority.load; _cached={}
    def load(self):
        if 'a' not in _cached: _cached['a']=_orig(self)
        return _cached['a']
    at.RuntimeAuthority.load=load
if MODE=='off':
    from httpcore._backends.auto import AutoBackend
    at.CheckedBackend.connect_tcp=AutoBackend.connect_tcp
# --- tracing (does not change behaviour)
_l=at.RuntimeAuthority.load
def traced_load(self):
    t=time.perf_counter(); r=_l(self); log(ev='authority_load',ms=(time.perf_counter()-t)*1000); return r
at.RuntimeAuthority.load=traced_load
_h=at.CredentialTransport.handle_async_request
async def traced(self,request):
    t=time.perf_counter()
    path=request.url.path
    try:
        r=await _h(self,request); log(ev='oMLX_http',method=request.method,path=path,ms_to_headers=(time.perf_counter()-t)*1000,status=r.status_code); return r
    except Exception as e:
        log(ev='oMLX_http_err',path=path,ms=(time.perf_counter()-t)*1000,err=type(e).__name__); raise
at.CredentialTransport.handle_async_request=traced
import uvicorn
uvicorn.run('service.main:app',host='127.0.0.1',port=8775,log_level='warning')
