"""Disposable spare-port processes only; never probe installed oMLX or port 8000."""
import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from service.inference import local_peer, attributed_transport
from service.inference.omlx_client import OMLXClient, ModelLoadError
from tests.test_mini_resources import synthetic_capacity, synthetic_process

SERVER = '''import socket,json
s=socket.socket();s.bind(('127.0.0.1',0));s.listen();print(s.getsockname()[1],flush=True)
c,_=s.accept();c.settimeout(15); data=b''
try:
 while b'\\r\\n\\r\\n' not in data:
  block=c.recv(8192)
  if not block: break
  data+=block
 if data:
  body=b'{"status":"ok"}'
  c.sendall(b'HTTP/1.1 200 OK\\r\\nContent-Length: '+str(len(body)).encode()+b'\\r\\n\\r\\n'+body)
except (OSError,TimeoutError): pass
print(json.dumps({'bytes':len(data),'authorization':b'Authorization:' in data,'prompt':b'PRIVATE_PROMPT' in data}),flush=True)
c.close();s.close()
'''


@pytest.fixture
def spare_server():
    process = subprocess.Popen([sys.executable, '-I', '-c', SERVER], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env={'PATH':'/usr/bin:/bin'})
    port = int(process.stdout.readline())
    assert port != 8000
    try:
        yield process, port
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['health', 'prompt', 'ensure'])
async def test_rogue_runtime_connection_gets_zero_bytes(spare_server, monkeypatch, tmp_path, action):
    process, port = spare_server
    monkeypatch.setattr(local_peer.Path, 'home', lambda: tmp_path)
    client = OMLXClient(base_url=f'http://127.0.0.1:{port}', api_key='a'*64)
    try:
        with pytest.raises(ModelLoadError, match='Local inference peer attribution unavailable'):
            if action == 'health':
                await client.health()
            elif action == 'prompt':
                await client._client.post('/v1/chat/completions', json={'messages':['PRIVATE_PROMPT']})
            else:
                from service import main
                monkeypatch.setattr(main, 'client', client, raising=False)
                await main.ensure_omlx()
        report = json.loads(await asyncio.to_thread(process.stdout.readline))
        assert report == {'bytes':0,'authorization':False,'prompt':False}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_attributed_disposable_process_receives_health(spare_server):
    process, port = spare_server
    class Authority:
        uid = os.getuid()
        connected_peer = local_peer.ManagedOmlx.connected_peer
        def binding(self, expected_pid=None):
            assert process.poll() is None
            assert expected_pid in (None, process.pid)
            return process.pid
    authority = Authority()
    authority.port = port
    authority.prep = SimpleNamespace(run=attributed_transport.inspect_command)
    client = OMLXClient(base_url=f'http://127.0.0.1:{port}', api_key='a'*64)
    client._credential_transport.backend.authority = SimpleNamespace(load=lambda: authority)
    try:
        assert await client.health() == {'status':'ok'}
        report = json.loads(await asyncio.to_thread(process.stdout.readline))
        assert report['bytes'] > 0 and report['authorization'] and not report['prompt']
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('path',['/health','/v1/chat/completions'])
async def test_gateway_rogue_upstream_gets_no_credential_or_prompt(spare_server,monkeypatch,tmp_path,path):
    from mini import gateway
    from tests.test_mini_http import invoke, Gateway
    process,port=spare_server
    monkeypatch.setattr(gateway,'UPSTREAM',f'http://127.0.0.1:{port}')
    monkeypatch.setattr(local_peer.Path,'home',lambda:tmp_path)
    status,body,_=await invoke(Gateway('a'*64,'b'*64),path=path,method='GET' if path=='/health' else 'POST')
    assert status>=500 and b'Bearer' not in body and b'PRIVATE_PROMPT' not in body
    report=json.loads(await asyncio.to_thread(process.stdout.readline))
    assert report=={'bytes':0,'authorization':False,'prompt':False}


@pytest.mark.asyncio
async def test_epoch_change_during_prewrite_inspection_sends_zero_bytes(spare_server):
    process,port=spare_server
    client=OMLXClient(base_url=f'http://127.0.0.1:{port}',api_key='a'*64)
    class Authority:
        uid=os.getuid()
        prep=SimpleNamespace(run=attributed_transport.inspect_command)
        def binding(self):return process.pid
        def connected_peer(self,*args):
            result=local_peer.ManagedOmlx.connected_peer(self,*args)
            self.calls+=1
            if self.calls==2:client.invalidate_connections()
            return result
    authority=Authority();authority.port=port;authority.calls=0
    client._credential_transport.backend.authority=SimpleNamespace(load=lambda:authority)
    try:
        with pytest.raises(ModelLoadError):await client.health()
        assert json.loads(await asyncio.to_thread(process.stdout.readline))['bytes']==0
    finally:await client.aclose()
