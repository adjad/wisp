import sys, time, statistics as st, subprocess, socket
sys.path.insert(0,'/Applications/Wisp.app/Contents/Resources/backend')
from service.inference import attributed_transport as at
from service.inference.local_peer import process_identity
# count subprocess spawns
calls=[]
orig=at.inspect_command
def counted(argv):
    t=time.perf_counter(); out=orig(argv); calls.append((argv[0].split('/')[-1]+' '+' '.join(argv[1:4]), time.perf_counter()-t)); return out
at.inspect_command=counted
try:
    t=time.perf_counter(); a=at.RuntimeAuthority().load(); print('load() = %s in %.0f ms'%(type(a).__name__,(time.perf_counter()-t)*1000))
except Exception as e:
    print('load failed:', type(e).__name__, getattr(e,'args',e)); sys.exit()
print('spawns during first load (incl. one-time tree qualification):',len(calls), 'sum %.0f ms'%(sum(c[1] for c in calls)*1000))
for n in range(3):
    calls.clear(); t=time.perf_counter()
    try: pid=a.binding()
    except Exception as e: print('binding failed', type(e).__name__, e.args); break
    dt=time.perf_counter()-t
    print('binding() #%d: %.0f ms, %d spawns'%(n+1,dt*1000,len(calls)))
# full per-connection check = connect + binding + process_identity + connected_peer ; then each write -> connected_peer again
s=socket.create_connection(('127.0.0.1',8000)); 
ident=process_identity(pid,a.uid)
calls.clear(); t=time.perf_counter()
try:
    owner=a.connected_peer(s,pid,ident)
    print('connected_peer(): %.0f ms, %d spawns'%((time.perf_counter()-t)*1000,len(calls)))
    for c in calls: print('   %6.1f ms  %s'%(c[1]*1000,c[0]))
except Exception as e: print('connected_peer failed', type(e).__name__, e.args)
