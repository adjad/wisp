import sys, json, time, statistics as st
from drive import turn
mode=sys.argv[1]; reps=int(sys.argv[2])
PROMPTS=["hello","what is on my calendar today","summarize my unread emails","explain what a bloom filter is in two sentences"]
turn("hi there")   # warm-up, discarded
time.sleep(3)
res={}
for p in PROMPTS:
    res[p]=[]
    for _ in range(reps):
        sid,ev,total,fd,text=turn(p)
        res[p].append(dict(total=total,first_delta=fd,events=ev,reply=text[:100]))
        time.sleep(3)
json.dump(res,open(f"anat_{mode}.json","w"))
print(f"MODE={mode}")
for p,rs in res.items():
    print(f"  {p[:46]:46s} totals={[round(r['total'],1) for r in rs]}  err={[ [e[1] for e in r['events'] if e[1]=='error'] for r in rs]}")
