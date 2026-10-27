import json,os,sys,time,subprocess,urllib.request
M=sys.argv[1]; BUDGET=sys.argv[2] if len(sys.argv)>2 else "230"; REPS=sys.argv[3] if len(sys.argv)>3 else "2"
LING="Ling-3.0-tiny-oQ6e"
key=json.load(open(os.path.expanduser('~/.omlx/settings.json')))['auth']['api_key']
PY='/Users/adijain/Desktop/MOE_Project/.venv/bin/python'
def call(method,path,body=None,t=180):
    r=urllib.request.Request('http://127.0.0.1:8000'+path,method=method,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},data=json.dumps(body).encode() if body is not None else None)
    return json.loads(urllib.request.urlopen(r,timeout=t).read() or b'{}')
def resident(): return [m['id'] for m in call('GET','/v1/models/status')['models'] if m.get('loaded')]
def ensure(model):
    t=time.time()
    for other in resident():
        if other!=model: call('POST',f'/v1/models/{other}/unload')
    for _ in range(60):
        if not [r for r in resident() if r!=model]: break
        time.sleep(1)
    if model not in resident(): call('POST',f'/v1/models/{model}/load')
    for _ in range(120):
        if model in resident(): return time.time()-t
        time.sleep(1)
    raise RuntimeError('load timeout '+model)
try:
    lt=ensure(M); print(f"== {M}: resident after {lt:.1f}s (swap {subprocess.run(['sysctl','-n','vm.swapusage'],capture_output=True,text=True).stdout.strip()})",flush=True)
    sz=[m for m in call('GET','/v1/models/status')['models'] if m['id']==M][0].get('estimated_size')
    print(f"   size on disk/resident est: {sz/1e9:.2f} GB",flush=True)
    for name,cmd,env in [("probe",[PY,'probe.py',M],{}),
                         ("speed",[PY,'bench_raw.py','scaling'],{"BENCH_MODEL":M,"SIZES":"500,4000,8000"}),
                         ("battery",[PY,'battery.py',M,REPS,BUDGET],{})]:
        e=dict(os.environ); e.update(env)
        out=subprocess.run(cmd,capture_output=True,text=True,env=e,timeout=480)
        print(f"-- {name}"); print((out.stdout or '')[-1800:].rstrip()); 
        if out.returncode: print('   (stderr tail)',(out.stderr or '')[-300:])
        sys.stdout.flush()
finally:
    try: print(f"== restoring {LING}: {ensure(LING):.1f}s; resident={resident()}")
    except Exception as e: print('RESTORE FAILED',e)
