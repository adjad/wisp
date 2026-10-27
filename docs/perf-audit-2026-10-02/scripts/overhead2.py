import asyncio, time, sys, traceback, statistics as st
sys.path.insert(0,'/Applications/Wisp.app/Contents/Resources/backend')
import httpx
from service.inference import attributed_transport as at
from service.inference.omlx_client import OMLXClient
from service.config import omlx_api_key
KEY=omlx_api_key()
# surface the hidden cause + count checks
cnt={'binding':0,'peer':0}
ob=at.DesktopOmlx.binding; op=at.DesktopOmlx.connected_peer
def b(self,*a,**k):
    cnt['binding']+=1
    try: return ob(self,*a,**k)
    except Exception as e: print('  [binding refused]',type(e).__name__,e.args); raise
def p(self,*a,**k):
    cnt['peer']+=1
    t=time.perf_counter()
    try: return op(self,*a,**k)
    except Exception as e: print('  [connected_peer refused]',type(e).__name__,e.args); raise
    finally: cnt.setdefault('peer_ms',[]).append((time.perf_counter()-t)*1000)
at.DesktopOmlx.binding=b; at.DesktopOmlx.connected_peer=p
BODY={"model":"Ling-3.0-tiny-oQ6e","messages":[{"role":"user","content":"Reply with the single word: ok"}],
      "max_tokens":4,"stream":False,"temperature":0,"chat_template_kwargs":{"enable_thinking":False}}
async def main():
    raw=httpx.AsyncClient(base_url="http://127.0.0.1:8000",headers={"Authorization":"Bearer "+KEY},timeout=60)
    cli=OMLXClient()
    for i in range(6):
        t=time.perf_counter(); await raw.post("/v1/chat/completions",json=BODY); r=time.perf_counter()-t
        cnt['binding']=cnt['peer']=0; cnt['peer_ms']=[]
        t=time.perf_counter()
        try:
            await cli._client.post("/v1/chat/completions",json=BODY); c=time.perf_counter()-t
        except Exception as e:
            c=time.perf_counter()-t; print('  client error',repr(e)); 
        print(f"raw {r*1000:6.0f} ms | via Wisp transport {c*1000:6.0f} ms | binding calls={cnt['binding']} peer checks={cnt['peer']} ({sum(cnt['peer_ms']):.0f} ms inside)")
asyncio.run(main())
