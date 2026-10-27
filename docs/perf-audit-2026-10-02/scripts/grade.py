import json,sys,statistics as st
WRITE={'send_email','send_message','add_calendar_event','add_reminder','create_note','set_timer'}
def grade(path):
    res=json.load(open(path)); n=len(res)
    strict=lenient=0; need=[r for r in res if r['ok']]; nt=[r for r in res if not r['ok']]
    s_ok=l_ok=0; wrong_write=0; notool=0; badjson=0
    for r in res:
        names=set(r['tool']); ok=set(r['ok']); arg=r['arg']
        argok= arg is None or any(arg in a.lower() for a in r['args'])
        if not ok:
            c=(len(names)==0); s_ok+=c; l_ok+=c; continue
        if not names: notool+=1
        s=bool(names) and names<=ok and argok
        l=bool(names&ok) and argok and not ((names-ok)&WRITE)
        if (names-ok)&WRITE: wrong_write+=1
        if r.get('argjson') is False: badjson+=1
        s_ok+=s; l_ok+=l
    dts=sorted(r['dt'] for r in res)
    toks=[r['toks'] or 0 for r in res]
    print(f"{path[8:-5]:42s} n={n:3d} strict={100*s_ok/n:3.0f}% lenient={100*l_ok/n:3.0f}% no-tool={notool:2d} wrong-WRITE={wrong_write:2d} badjson={badjson} | lat p50={st.median(dts):5.2f}s p90={dts[int(.9*(n-1))]:5.2f}s max={dts[-1]:5.1f}s | out tok p50={st.median(toks):4.0f} p90={sorted(toks)[int(.9*(n-1))]:4d} | reasoning chars p50={st.median([r['reason'] for r in res]):.0f}")
for p in sys.argv[1:]: grade(p)
