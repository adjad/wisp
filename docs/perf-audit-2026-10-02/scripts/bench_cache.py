import sys, uuid, time, statistics as st
sys.argv=[sys.argv[0],'x']
from bench_raw import *
N=8000
k=calibrate(N)
def msgs(sys_nonce, user_tail="", clock=None, clock_where="none"):
    body=(FILL*k)
    sysmsg=f"[{sys_nonce}] You are Wisp, a helpful assistant."
    user="Background notes:\n"+body+"\n\nCount from 1 to 20, separated by commas. Output only the numbers."+user_tail
    m=[{"role":"system","content":sysmsg},{"role":"user","content":user}]
    if clock and clock_where=="front": m[0]["content"]=f"Current time: {clock}\n"+m[0]["content"]
    if clock and clock_where=="after_system": m.insert(1,{"role":"system","content":f"Current time: {clock}"})
    return m
def run(label,m,reps=1):
    out=[]
    for _ in range(reps):
        r=stream(m,max_tokens=40); out.append(r)
    pt=(out[0]['usage'] or {}).get('prompt_tokens'); 
    print(f"{label:58s} prompt={pt} TTFT={st.median(o['ttft'] for o in out):5.2f}s")
nonce=uuid.uuid4().hex[:10]
base=msgs(nonce)
run("A  cold (new prefix)",base)
run("B  identical repeat (full hit expected)",base)
run("C  same prefix, last line changed (tail-only change)",msgs(nonce,user_tail=" Please also be brief."))
run("D  identical repeat again",base)
nonce2=uuid.uuid4().hex[:10]
run("E  first system line changed (head change => miss)",msgs(nonce2))
# clock placement: simulate time changing between calls (minute tick)
n3=uuid.uuid4().hex[:10]
run("F1 clock at FRONT of system, t=17:00",msgs(n3,clock="17:00",clock_where="front"))
run("F2 clock at FRONT of system, t=17:01 (1 min later)",msgs(n3,clock="17:01",clock_where="front"))
n4=uuid.uuid4().hex[:10]
run("G1 clock in LATER system msg (after body), t=17:00",msgs(n4,clock="17:00",clock_where="after_system"))
run("G2 clock in LATER system msg, t=17:01 (1 min later)",msgs(n4,clock="17:01",clock_where="after_system"))
