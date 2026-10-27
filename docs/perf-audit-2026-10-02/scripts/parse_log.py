import re, sys, statistics as st, collections, json, os
LOG=os.path.expanduser('~/Library/Application Support/oMLX/logs/server.log')
pat=re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ - omlx\.server - INFO - Chat completion: model=(\S+), (\d+) tokens in ([\d.]+)s \(([\d.]+) tok/s\), prompt: (\d+), finish_reason=(\w+), max_tokens=(\d+)(?:, request_max_tokens=(\d+))?(?:, stream_model_ttft=([\d.]+)s)?(?:, stream_visible_ttft=([\d.]+)s)?')
rows=[]
for line in open(LOG,errors='ignore'):
    if 'Chat completion:' not in line: continue
    m=pat.match(line)
    if not m: continue
    d=dict(ts=m[1],model=m[2],toks=int(m[3]),secs=float(m[4]),tps=float(m[5]),prompt=int(m[6]),fin=m[7],maxt=int(m[8]),ttft=float(m[10]) if m[10] else None,vttft=float(m[11]) if m[11] else None)
    rows.append(d)
json.dump(rows,open('rows.json','w'))
print(len(rows))
by=collections.defaultdict(list)
for r in rows: by[r['model']].append(r)
for k,v in sorted(by.items(), key=lambda kv:-len(kv[1])):
    ts=[r['ts'] for r in v]
    print(f"{k:45s} n={len(v):6d}  {min(ts)} -> {max(ts)}")
