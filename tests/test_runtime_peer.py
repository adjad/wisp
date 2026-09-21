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
import sys
s=socket.socket(fileno=int(sys.argv[1])) if len(sys.argv)>1 else socket.socket()
if len(sys.argv)==1:s.bind(('127.0.0.1',0))
s.settimeout(15);s.listen();print(s.getsockname()[1],flush=True)
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


RESERVED_FDS = iter(json.loads(os.environ['PEER_TEST_PEER_FDS'])) if 'PEER_TEST_PEER_FDS' in os.environ else None


@pytest.fixture
def spare_server():
    inherited = [next(RESERVED_FDS)] if RESERVED_FDS is not None else []
    process = subprocess.Popen([sys.executable, '-I', '-c', SERVER, *map(str,inherited)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env={'PATH':'/usr/bin:/bin'},pass_fds=inherited)
    try:
        port = int(process.stdout.readline())
        assert port != 8000
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


def test_missing_manifest_accepts_only_official_desktop_runtime(monkeypatch, tmp_path):
    executable = "/Applications/oMLX.app/Contents/Resources/Python/cpython/bin/python3.11"
    calls = []
    def inspect(argv):
        calls.append(argv)
        if "-iTCP:8000" in argv:
            return b"p321\nu501\nf4\nn127.0.0.1:8000\n"
        return ("p321\nftxt\nn" + executable + "\n").encode()
    class Info:
        st_mode = 0o100755
        st_uid = os.getuid()
    monkeypatch.setattr(attributed_transport, "inspect_command", inspect)
    monkeypatch.setattr(attributed_transport.Path, "resolve", lambda self, strict=False: self)
    monkeypatch.setattr(attributed_transport.Path, "stat", lambda self: Info())
    authority = attributed_transport.DesktopOmlx()
    assert authority.binding() == 321
    assert calls


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


def test_native_reclamation_excludes_only_its_exact_pinned_directory_fd(tmp_path):
    from tests.test_remote_recovery import releases, receiver
    root=tmp_path.resolve();ids=releases(root);path=root/ids[0]
    pinned=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        assert receiver.unused_release(path,pinned=pinned)
        extra=os.open(path/'code',os.O_RDONLY|os.O_NOFOLLOW)
        try:assert not receiver.unused_release(path,pinned=pinned)
        finally:os.close(extra)
    finally:os.close(pinned)
    report=receiver.release_inventory(root,now=100000)
    assert receiver.reclaim_releases(root,[ids[0]],expected_inventory=report['inventory_sha256'],apply=True,jobs=lambda:set(),now=100000)['removed']==ids[:1]



def test_native_gate_denies_external_network_and_unrelated_execution(tmp_path):
    if 'PEER_TEST_PEER_FDS' not in os.environ:
        from pathlib import Path
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'build-support'))
        import native_peer_gate
        report=native_peer_gate.run_gate(allow_dirty=True)
        assert report['status']=='PASS'
        return
    import socket
    from pathlib import Path
    # No HTTP/request bytes are sent. TEST-NET is a reserved documentation range.
    with socket.socket() as denied:
        denied.settimeout(0.2)
        with pytest.raises(PermissionError):denied.connect(('192.0.2.1',443))
    with socket.socket() as denied:
        denied.settimeout(0.2)
        with pytest.raises(PermissionError):denied.connect(('127.0.0.1',int(os.environ['PEER_TEST_DENIED_PORT'])))
    with pytest.raises(PermissionError):
        subprocess.run(['/usr/bin/osascript','-e','return 1'],check=True,capture_output=True)
    with pytest.raises(PermissionError):Path(os.environ['PEER_TEST_PRIVATE_CANARY']).read_bytes()
