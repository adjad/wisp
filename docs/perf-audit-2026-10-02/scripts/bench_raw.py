import httpx, json, os, sys, time, random, statistics as st, uuid
KEY=json.load(open(os.path.expanduser('~/.omlx/settings.json')))['auth']['api_key']
MODEL=os.environ.get('BENCH_MODEL','Ling-3.0-tiny-oQ6e')
H={"Authorization":"Bearer "+KEY}
FILL=("The quarterly planning meeting covered hiring, the roadmap for the mobile client, vendor contracts, and the migration of the "
      "billing service to the new region. Action items were assigned to the platform, design and finance teams, with follow-ups "
      "scheduled for the next two sprints. ")
def prompt_tokens(text_msgs, think=False):
    r=httpx.post("http://127.0.0.1:8000/v1/chat/completions",headers=H,timeout=120,json={"model":MODEL,"messages":text_msgs,"max_tokens":1,
        "chat_template_kwargs":{"enable_thinking":think}})
    return r.json()["usage"]["prompt_tokens"]
def make_msgs(ntok, nonce=True, tail="", task="Count from 1 to 200, separated by commas. Output only the numbers.", sysextra=""):
    # calibrate reps by linear fit
    def build(k):
        body=(FILL*k)
        pre=f"[session {uuid.uuid4().hex[:12]}] " if nonce else "[session fixed] "
        return [{"role":"system","content":pre+"You are a helpful assistant. "+sysextra},
                {"role":"user","content":"Background notes:\n"+body+"\n\n"+task+tail}]
    return build
cal={}
def calibrate(n):
    b=make_msgs(n,nonce=False)
    t1=prompt_tokens(b(1)); t11=prompt_tokens(b(11)); per=(t11-t1)/10
    k=max(1,int(round((n-t1)/per))+1); return k
def stream(msgs, think=False, max_tokens=150, extra=None):
    body={"model":MODEL,"messages":msgs,"max_tokens":max_tokens,"stream":True,"stream_options":{"include_usage":True},
          "chat_template_kwargs":{"enable_thinking":think}}
    if extra: body.update(extra)
    t0=time.perf_counter(); first=None; stamps=[]; usage=None; reasoning=0; content=0
    with httpx.stream("POST","http://127.0.0.1:8000/v1/chat/completions",headers=H,json=body,timeout=300) as r:
        for line in r.iter_lines():
            if not line.startswith("data: "): continue
            d=line[6:]
            if d=="[DONE]": break
            j=json.loads(d)
            if j.get("usage"): usage=j["usage"]
            ch=j.get("choices") or []
            if not ch: continue
            delta=ch[0].get("delta",{})
            txt=delta.get("content") or ""; rs=delta.get("reasoning_content") or delta.get("reasoning") or ""
            if txt or rs or delta.get("tool_calls"):
                now=time.perf_counter()-t0
                if first is None: first=now
                stamps.append(now); content+=len(txt); reasoning+=len(rs)
    end=time.perf_counter()-t0
    n=(usage or {}).get("completion_tokens") or len(stamps)
    dec=(stamps[-1]-stamps[0]) if len(stamps)>1 else 0
    gaps=[b-a for a,b in zip(stamps,stamps[1:])]
    return dict(ttft=first,total=end,chunks=len(stamps),out_tokens=n,usage=usage,decode_tps=(len(stamps)-1)/dec if dec>0 else None,
                tps_by_usage=(n-1)/dec if dec>0 else None,gaps=gaps,reasoning_chars=reasoning,content_chars=content)
def q(v,p): v=sorted(v); return v[min(len(v)-1,int(len(v)*p))]
if __name__=="__main__":
    which=sys.argv[1]
    if which=="scaling":
        sizes=[int(x) for x in os.environ.get("SIZES","500,2000,4000,8000,12000").split(",")]
        print(f"model={MODEL}")
        print(f"{'ctx':>6} {'prompt':>7} | {'TTFT cold':>9} {'prefill tok/s':>13} | {'decode tok/s (chunks)':>21} {'by usage':>9} | {'gap p50/p99 ms':>15} {'out':>5}")
        for n in sizes:
            k=calibrate(n); rows=[]
            for rep in range(3):
                m=make_msgs(n)(k); r=stream(m)
                pt=(r['usage'] or {}).get('prompt_tokens',n)
                rows.append((pt,r))
            pt=rows[0][0]
            ttft=st.median(r['ttft'] for _,r in rows)
            dtps=st.median(r['decode_tps'] for _,r in rows if r['decode_tps'])
            utps=st.median(r['tps_by_usage'] for _,r in rows if r['tps_by_usage'])
            gaps=[g for _,r in rows for g in r['gaps']]
            print(f"{n:6d} {pt:7d} | {ttft:8.2f}s {pt/ttft:13.0f} | {dtps:21.1f} {utps:9.1f} | {q(gaps,.5)*1000:6.1f}/{q(gaps,.99)*1000:6.1f} {rows[0][1]['out_tokens']:5d}")
