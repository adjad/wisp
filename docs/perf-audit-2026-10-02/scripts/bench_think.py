import sys, statistics as st, threading, time
sys.argv=[sys.argv[0],'x']
from bench_raw import *
Q=[{"role":"system","content":"You are Wisp, a concise personal assistant on a Mac. Answer directly."},
   {"role":"user","content":"I have a dentist appointment Thursday at 3pm and a team standup every weekday at 9:30. If I also want a 45 minute gym session on Thursday, what's the latest I could start it so I'm not late for the dentist if the gym is 20 minutes from the dentist? Answer in 2 sentences."}]
print("== thinking ON vs OFF on a short reasoning-ish question (sampling = oMLX profile) ==")
for think in (False,True):
    rs=[stream(Q,think=think,max_tokens=1500) for _ in range(3)]
    for r in rs: pass
    print(f"think={think!s:5}  out_tokens={[r['out_tokens'] for r in rs]}  total_s={[round(r['total'],2) for r in rs]}  reasoning_chars={[r['reasoning_chars'] for r in rs]} answer_chars={[r['content_chars'] for r in rs]}")
print("\n== concurrency: 2 simultaneous 8k-prompt requests vs 1 ==")
k=calibrate(8000)
def one(res,i):
    m=make_msgs(8000)(k); res[i]=stream(m,max_tokens=150)
solo=[None]; one(solo,0); s=solo[0]
print(f"solo      : TTFT {s['ttft']:.2f}s  decode {s['tps_by_usage']:.0f} tok/s  total {s['total']:.2f}s")
res=[None,None]; ts=[threading.Thread(target=one,args=(res,i)) for i in range(2)]
t0=time.perf_counter(); [t.start() for t in ts]; [t.join() for t in ts]; wall=time.perf_counter()-t0
for i,r in enumerate(res): print(f"concurrent{i}: TTFT {r['ttft']:.2f}s  decode {r['tps_by_usage']:.0f} tok/s  total {r['total']:.2f}s")
print(f"wall for both: {wall:.2f}s  vs 2x solo = {2*s['total']:.2f}s")
