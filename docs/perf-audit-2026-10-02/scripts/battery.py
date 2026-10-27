import httpx, json, os, sys, time, statistics as st
KEY=json.load(open(os.path.expanduser('~/.omlx/settings.json')))['auth']['api_key']
H={"Authorization":"Bearer "+KEY}
MODEL=sys.argv[1]; REPS=int(sys.argv[2]); BUDGET=float(sys.argv[3]) if len(sys.argv)>3 else 1e9
TOOLS=json.load(open('menu_schemas.json'))
SYSTEM=("You are Wisp, a personal assistant running on the user's Mac. Use the provided tools to act on the user's request. "
        "If a tool fits, call it; do not ask clarifying questions when the request is clear. For small talk, answer briefly without tools.")
# (prompt, accepted tools, required arg substring or None)
CASES=[("what's on my calendar tomorrow",{"get_upcoming"},None),
 ("show me my last 5 emails",{"view_emails"},None),
 ("text mom that I'll be 10 minutes late",{"send_message"},"mom"),
 ("do I have any unread emails from Amazon",{"view_emails","summarize_emails"},None),
 ("set a timer for 12 minutes",{"set_timer"},"12"),
 ("what's the weather in Seattle",{"get_weather"},"seattle"),
 ("remind me to call the dentist at 4pm",{"add_reminder"},"dentist"),
 ("find my note about the kitchen remodel",{"search_notes"},"kitchen"),
 ("what's in my Downloads folder",{"list_dir"},"download"),
 ("how much battery do I have left",{"get_battery_status"},None),
 ("who won the Lakers game last night",{"web_search"},"laker"),
 ("schedule lunch with Priya on Friday at noon",{"add_calendar_event"},"priya"),
 ("email Dave a summary of today's meeting: we agreed to ship on the 14th",{"send_email"},"dave"),
 ("summarize my messages from today",{"summarize_messages","view_messages"},None),
 ("thanks, that's all",set(),None)]
def call(prompt):
    body={"model":MODEL,"messages":[{"role":"system","content":SYSTEM},{"role":"user","content":prompt}],"tools":TOOLS,"tool_choice":"auto","max_tokens":2000,"stream":False}
    t=time.perf_counter()
    try:
        r=httpx.post("http://127.0.0.1:8000/v1/chat/completions",headers=H,json=body,timeout=75)
        d=r.json(); dt=time.perf_counter()-t
        ch=(d.get("choices") or [{}])[0]; m=ch.get("message",{})
        tcs=m.get("tool_calls") or []
        usage=d.get("usage",{})
        return dict(dt=dt,tool=[tc["function"]["name"] for tc in tcs],args=[tc["function"].get("arguments","") for tc in tcs],
                    toks=usage.get("completion_tokens"),ptoks=usage.get("prompt_tokens"),reason=len(m.get("reasoning_content") or ""),content=(m.get("content") or "")[:80],fin=ch.get("finish_reason"))
    except Exception as e:
        return dict(dt=time.perf_counter()-t,tool=[],args=[],toks=None,ptoks=None,reason=0,content="ERR "+repr(e)[:60],fin="error")
res=[]; start=time.perf_counter(); done=0
for rep in range(REPS):
    for p,ok,arg in CASES:
        if time.perf_counter()-start>BUDGET: break
        r=call(p); r['p']=p; r['ok']=ok; r['arg']=arg
        names=set(r['tool'])
        if not ok: r['correct']= (len(r['tool'])==0)
        else:
            r['correct']= bool(names) and names<=ok and (arg is None or any(arg in a.lower() for a in r['args']))
            try: [json.loads(a) for a in r['args']]; r['argjson']=True
            except Exception: r['argjson']=False
        res.append(r)
        json.dump(res,open(f"battery_{MODEL.replace('/','_')}.json",'w'),default=lambda o: sorted(o))
n=len(res); good=sum(r['correct'] for r in res)
notool=sum(1 for r in res if r['ok'] and not r['tool']); wrong=sum(1 for r in res if r['ok'] and r['tool'] and not r['correct'])
badjson=sum(1 for r in res if r['ok'] and r['tool'] and r.get('argjson') is False)
dts=[r['dt'] for r in res]; 
print(f"MODEL {MODEL}: {good}/{n} correct ({100*good/max(n,1):.0f}%)  no-tool-when-needed={notool}  wrong/bad-arg={wrong}  invalid-json-args={badjson}")
print(f"   latency/call  p50={st.median(dts):.2f}s  p90={sorted(dts)[int(.9*(n-1))]:.2f}s  max={max(dts):.1f}s | out tokens p50={st.median([r['toks'] or 0 for r in res]):.0f}  max={max([r['toks'] or 0 for r in res])} | reasoning chars p50={st.median([r['reason'] for r in res]):.0f} | prompt toks={res[0]['ptoks']}  | finish={ {f:sum(1 for r in res if r['fin']==f) for f in set(r['fin'] for r in res)} }")
fails=[(r['p'],r['tool'],r['content'][:50]) for r in res if not r['correct']]
for f in fails[:6]: print("   miss:",f)
json.dump(res,open(f"battery_{MODEL.replace('/','_')}.json","w"),default=lambda o: sorted(o))
