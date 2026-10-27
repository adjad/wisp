import httpx, json, sys, time
BASE="http://127.0.0.1:8775"
def turn(prompt, sid=None):
    ev=[]; t0=time.perf_counter(); body={"prompt":prompt,"debug":False}
    if sid: body["session_id"]=sid
    first_delta=None; text=""
    with httpx.Client() as c:
        with c.stream("POST",BASE+"/agent",json=body,timeout=httpx.Timeout(300.0)) as r:
            for line in r.iter_lines():
                if not line.startswith("data: "): continue
                e=json.loads(line[6:]); t=time.perf_counter()-t0; ty=e.get("type")
                if ty=="session": sid=e.get("id") or sid
                if ty=="delta" and first_delta is None: first_delta=t
                if ty=="text": text=e.get("text","")
                if ty=="confirm":
                    c.post(BASE+"/agent/approve",json={"session_id":sid,"action_id":e.get("id"),"approved":False,"scope":"once"},timeout=30)
                ev.append((round(t,2),ty,(e.get("name") or e.get("reason") or e.get("text") or "")[:60] if ty!="delta" else ""))
                if ty=="done": break
    return sid,ev,time.perf_counter()-t0,first_delta,text
if __name__=="__main__":
    label=sys.argv[1]; prompts=sys.argv[2:]
    out=[]
    sid=None
    for p in prompts:
        sid,ev,total,fd,text=turn(p, None)   # fresh session each, same cold-ish state
        print(f"\n[{label}] {p!r}  total={total:.2f}s first_delta={fd}")
        last=0
        for t,ty,info in ev:
            if ty in("delta",) : continue
            print(f"   +{t:6.2f}s  (+{t-last:5.2f})  {ty:12s} {info}"); last=t
        print("   reply:",text[:140].replace("\n"," "))
        out.append(dict(prompt=p,total=total,first_delta=fd,events=ev))
    json.dump(out,open(f"turns_{label}.json","w"))
