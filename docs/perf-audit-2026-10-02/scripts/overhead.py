import asyncio, time, json, os, sys, statistics as st
sys.path.insert(0, '/Applications/Wisp.app/Contents/Resources/backend')
import httpx
from service.inference.omlx_client import OMLXClient
from service.config import omlx_api_key
KEY = omlx_api_key()
MODEL = "Ling-3.0-tiny-oQ6e"
BODY = {"model": MODEL, "messages":[{"role":"user","content":"Reply with the single word: ok"}],
        "max_tokens": 4, "stream": False, "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False}}
async def timeit(fn, n):
    out=[]
    for _ in range(n):
        t=time.perf_counter(); await fn(); out.append(time.perf_counter()-t)
    return out
def summ(name, v):
    v=sorted(v); print(f"{name:42s} n={len(v)} min={v[0]*1000:7.1f}ms p50={st.median(v)*1000:7.1f}ms max={v[-1]*1000:7.1f}ms")
async def main():
    raw = httpx.AsyncClient(base_url="http://127.0.0.1:8000", headers={"Authorization":"Bearer "+KEY}, timeout=60)
    cli = OMLXClient()
    print("client managed:", cli.managed, "authority:", type(cli._credential_transport.backend.authority).__name__)
    # warm both paths
    await raw.get("/health"); 
    try:
        await cli._client.get("/health")
    except Exception as e:
        print("client warm error:", repr(e)); 
    summ("raw httpx GET /health", await timeit(lambda: raw.get("/health"), 15))
    try:
        summ("OMLXClient GET /health (checked transport)", await timeit(lambda: cli._client.get("/health"), 15))
    except Exception as e: print("client err", repr(e))
    summ("raw httpx POST 4-token completion", await timeit(lambda: raw.post("/v1/chat/completions", json=BODY), 8))
    try:
        summ("OMLXClient POST 4-token completion", await timeit(lambda: cli._client.post("/v1/chat/completions", json=BODY, headers={}), 8))
    except Exception as e: print("client err", repr(e))
asyncio.run(main())
