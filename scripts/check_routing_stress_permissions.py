#!/usr/bin/env python3
"""Exercise real policy/confirmation/agent code with scripted tool proposals."""
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from scripts.run_routing_stress_suite import ACTIVE, RUNTIME, Approver, bootstrap, dump, new_state

PROPOSALS={
    "send_message":{"to":"+1-202-555-0104","text":"Route test."},
    "send_email":{"to":"johnstandark@gmail.com","subject":"Route test","body":"Route test."},
    "schedule_send":{"channel":"email","to":"johnstandark@gmail.com","when":"2026-09-01T18:00:00","body":"Route test.","subject":"Route test"},
    "forward_email":{"message_id":"<route-forwarding@example.test>","to":"johnstandark@gmail.com","note":"Route test."},
    "cancel_event":{"title":"Route obsolete lunch"},
    "add_calendar_event":{"title":"Route test","when_iso":"2026-09-01T15:00:00","duration_min":45},
    "update_event":{"title":"Route dentist","when_iso":"2026-09-01T16:00:00","duration_min":30,"location":"Clinic 2"},
    "clear_reminders":{"scope":"today","query":"Route cleanup"},
    "clear_past_reminders":{"query":"Route expired","days":365},
}


class ScriptedClient:
    def __init__(self,calls): self.calls=calls; self.i=0
    async def ensure_only(self,*a,**kw): pass
    async def stream_events(self,*a,**kw):
        self.i+=1
        calls=[{"id":f"p{i}","type":"function","function":{"name":n,"arguments":json.dumps(args)}} for i,(n,args) in enumerate(self.calls)] if self.i==1 else []
        yield {"kind":"final","message":{"role":"assistant","content":"" if calls else "The action results are shown above.","tool_calls":calls}}


async def main():
    output=ROOT/"test_results/routing_stress_permissions"
    bootstrap(output,"Huihui-Ornith-1.5-9B-abliterated-oQ6e")
    from service.safety import grants
    policy=RUNTIME['policy']
    results=[]
    for name,args in PROPOSALS.items():
        for verdict in ["approve","deny","timeout"]:
            state=new_state({"id":"PERM-TEST","group_number":0,"title":"permission isolation","condition":None})
            token=ACTIVE.set(state)
            async def emit(event): state['events'].append(event)
            error=""
            try:
                await RUNTIME['loop'].run_agent(ScriptedClient([(name,args)]),RUNTIME['model'],[{"role":"user","content":"Perform exactly this already-scoped test action: "+name+" "+json.dumps(args)}],emit,Approver(verdict),tools=[name],max_steps=2,debug=False)
            except Exception as exc: error=f"{type(exc).__name__}: {exc}"
            expected_dispatch=verdict=="approve"
            ok=bool(state['dispatches'])==expected_dispatch and bool(state['approvals']) and not error
            if verdict=="timeout": ok=ok and any(e.get('type')=='confirm_timeout' for e in state['events'])
            results.append({"test":f"{name}/{verdict}","passed":ok,"error":error,"dispatches":state['dispatches'],"approvals":state['approvals'],"events":state['events']})
            ACTIVE.reset(token)
    # Always-confirm tools must not offer a standing allowance that lifts the gate.
    for name,args in PROPOSALS.items():
        token=ACTIVE.set(new_state({"id":"PERM-TEST","title":"policy","group_number":0}))
        before=policy.decide(RUNTIME['registry'][name].category,args,tool=name).tier.value
        grants.grant(name,args,decision="allow")
        after=policy.decide(RUNTIME['registry'][name].category,args,tool=name).tier.value
        results.append({"test":f"{name}/standing-grant-must-not-bypass","passed":after=="confirm","before":before,"after":after,"grantable":grants.is_grantable(name,args)})
        grants._CACHE.clear()
        ACTIVE.reset(token)
    for ro,full in [(True,False),(False,False),(False,True)]:
        policy.set_read_only(ro); policy.set_full_access(full)
        for name,args in PROPOSALS.items():
            token=ACTIVE.set(new_state({"id":"PERM-TEST","title":"policy","group_number":0}))
            actual=policy.decide(RUNTIME['registry'][name].category,args,tool=name).tier.value
            expected="deny" if ro else "confirm"
            results.append({"test":f"{name}/read_only={ro}/full_access={full}","passed":actual==expected,"actual":actual,"expected":expected})
            ACTIVE.reset(token)
    for label,fn in [("subprocess",lambda:subprocess.run(["/usr/bin/true"])),("external socket",lambda:socket.create_connection(("198.51.100.1",443),timeout=0.01)),("write outside results",lambda:Path('/tmp/wisp-stress-must-not-exist.txt').write_text('forbidden'))]:
        blocked=False
        try: fn()
        except RuntimeError as exc: blocked="STRESS SAFETY" in str(exc)
        results.append({"test":"isolation/"+label,"passed":blocked})
    summary={"tests":len(results),"passed":sum(r['passed'] for r in results),"failed":[r['test'] for r in results if not r['passed']],"results":results}
    dump(output/'permission_results.json',summary)
    print(json.dumps({k:v for k,v in summary.items() if k!='results'},indent=2))


if __name__=='__main__': asyncio.run(main())
