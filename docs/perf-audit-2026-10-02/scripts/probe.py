import httpx, json, os, sys, time
KEY=json.load(open(os.path.expanduser('~/.omlx/settings.json')))['auth']['api_key']; H={"Authorization":"Bearer "+KEY}
M=sys.argv[1]; TOOLS=json.load(open('menu_schemas.json'))
def post(body,t=90):
    t0=time.perf_counter()
    try:
        r=httpx.post("http://127.0.0.1:8000/v1/chat/completions",headers=H,json=body,timeout=t); d=r.json()
        return r.status_code,d,time.perf_counter()-t0
    except Exception as e: return 0,{"error":repr(e)[:100]},time.perf_counter()-t0
base=lambda msgs,**k: {"model":M,"messages":msgs,"max_tokens":600,"stream":False,**k}
# (a) two system messages (Wisp puts the clock in a 2nd system message)
code,d,dt=post(base([{"role":"system","content":"You are Wisp, a concise assistant."},{"role":"system","content":"Current time: Friday 3:07 PM."},{"role":"user","content":"What day is it and what time? One short sentence."}]))
msg=((d.get("choices") or [{}])[0].get("message") or {}) if code==200 else {}
print(f"(a) 2 system msgs: HTTP {code} {dt:.1f}s ->", (msg.get("content") or d.get("error") or d.get("detail") or "")[:90].replace("\n"," "))
# (b) enable_thinking toggle honored?
Q=[{"role":"user","content":"If a train leaves at 3:40pm and the trip takes 2h50m, when does it arrive? Answer in one sentence."}]
for flag in (True,False):
    code,d,dt=post(base(Q,chat_template_kwargs={"enable_thinking":flag}))
    m=((d.get("choices") or [{}])[0].get("message") or {}) if code==200 else {}
    u=d.get("usage",{}) if code==200 else {}
    print(f"(b) enable_thinking={flag!s:5}: HTTP {code} {dt:.1f}s completion_tokens={u.get('completion_tokens')} reasoning_chars={len(m.get('reasoning_content') or '')} answer={ (m.get('content') or '')[:60]!r}")
# (c) tool_choice=required honored?
code,d,dt=post(base([{"role":"system","content":"You are Wisp."},{"role":"user","content":"set a timer for 12 minutes"}],tools=TOOLS,tool_choice="required",temperature=0))
m=((d.get("choices") or [{}])[0].get("message") or {}) if code==200 else {}
tc=m.get("tool_calls") or []
print(f"(c) tool_choice=required @greedy: HTTP {code} {dt:.1f}s tool_calls={[t['function']['name'] for t in tc]} args={[t['function']['arguments'][:50] for t in tc]}")
