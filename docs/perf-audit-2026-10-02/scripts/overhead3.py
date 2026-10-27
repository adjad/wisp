import asyncio, time, sys, os, statistics as st
sys.path.insert(0,'/Applications/Wisp.app/Contents/Resources/backend')
from pathlib import Path
from service.inference import attributed_transport as at
from service.inference.omlx_client import OMLXClient
for i in range(3):
    t=time.perf_counter(); a=at.RuntimeAuthority().load(); print('RuntimeAuthority().load() #%d: %.0f ms'%(i+1,(time.perf_counter()-t)*1000))
root=at.DesktopOmlx.python_root
t=time.perf_counter(); n=sum(1 for _ in root.rglob('*')); print('python_root files:',n,'rglob alone %.0f ms'%((time.perf_counter()-t)*1000))
srv=at.DesktopOmlx.server_entry.parent
t=time.perf_counter(); n=sum(1 for _ in srv.rglob('*')); print('server_entry dir files:',n,'rglob %.0f ms'%((time.perf_counter()-t)*1000))
# readiness-type calls through client
async def main():
    cli=OMLXClient()
    for name,fn in [('status()',cli.status),('loaded_models()',cli.loaded_models),('health()',cli.health),('models()',cli.models)]:
        for k in range(2):
            t=time.perf_counter()
            try: await fn(); ok='ok'
            except Exception as e: ok=type(e).__name__
            print(f'{name:18s} #{k+1}: {(time.perf_counter()-t)*1000:7.0f} ms {ok}')
asyncio.run(main())
