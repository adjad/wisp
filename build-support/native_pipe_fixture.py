"""Compile production framing and inspect only a disposable child's procargs."""
import ctypes
import json
import os
from pathlib import Path
import secrets
import select
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]


def procargs(pid):
    library=ctypes.CDLL(None,use_errno=True)
    mib=(ctypes.c_int*3)(1,49,pid)
    size=ctypes.c_size_t()
    if library.sysctl(mib,3,None,ctypes.byref(size),None,0)!=0 or not 0<size.value<=1024*1024:
        raise RuntimeError('procargs inspection unavailable')
    data=ctypes.create_string_buffer(size.value)
    if library.sysctl(mib,3,data,ctypes.byref(size),None,0)!=0:raise RuntimeError('procargs inspection unavailable')
    return data.raw[:size.value]


def qualify(root):
    root=Path(root);root.mkdir(mode=0o700,parents=True,exist_ok=True)
    binary=root/'native-pipe-fixture'
    subprocess.run(['/usr/bin/swiftc','-parse-as-library','-module-cache-path',str(root/'cache'),
        str(ROOT/'app/Sources/WispApp/BackendCredentials.swift'),str(ROOT/'tests/NativeCredentialPipeFixture.swift'),'-o',str(binary)],
        check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    values={name:secrets.token_hex(32) for name in ('WISP_LOCAL_OMLX_KEY','WISP_MINI_INFERENCE_KEY','WISP_MINI_NODE_KEY')}
    process=subprocess.Popen([str(binary),sys.executable,str(ROOT/'tests/credential_pipe_child.py'),str(root)],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={'PATH':'/usr/bin:/bin','HOME':str(root)})
    try:
        process.stdin.write(json.dumps(values).encode());process.stdin.close()
        for phase in ('BEFORE','AFTER'):
            if not select.select([process.stdout],[],[],20)[0]:raise RuntimeError('native pipe phase unavailable')
            line=process.stdout.readline().decode().strip().split()
            if len(line)!=2 or line[0]!=phase or not line[1].isdigit():raise RuntimeError('native pipe phase unavailable')
            pid=int(line[1])
            observed=[procargs(pid),subprocess.check_output(['/bin/ps','-Eww','-p',str(pid),'-o','command='])]
            if any(value.encode() in data or name.encode()+b'=' in data for data in observed for name,value in values.items()):
                raise RuntimeError('native credential exposure')
            (root/phase.lower()).touch(mode=0o600)
        if not select.select([process.stdout],[],[],20)[0] or process.stdout.readline()!=b'PIPE_PASS\n':
            raise RuntimeError('native pipe unavailable')
        process.wait(timeout=5)
        if process.returncode or process.stderr.read():raise RuntimeError('native pipe unavailable')
        return {'status':'PASS','exec_environment':'absent','procargs_before':'absent','procargs_after':'absent',
                'native_frame':'verified','descriptor':'closed','descendant_inheritance':'absent'}
    finally:
        if process.poll() is None:process.terminate()
        process.wait(timeout=5)
        values.clear()


if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='wisp-native-pipe-') as root:
        print(json.dumps(qualify(root),sort_keys=True))
