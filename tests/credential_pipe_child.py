"""Disposable native qualification child; barriers contain no credential data."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from service.credential_pipe import consume,NAMES

root=Path(sys.argv[1])
def wait(name):
    deadline=time.monotonic()+15
    while not (root/name).exists():
        if time.monotonic()>deadline:raise RuntimeError('fixture barrier unavailable')
        time.sleep(.01)
print('READY',flush=True)
wait('before')
values,generation=consume('primary')
assert generation=='absent' and not set(NAMES).intersection(os.environ) and 'WISP_CREDENTIAL_PIPE' not in os.environ
try:os.fstat(0)
except OSError:pass
else:raise AssertionError('credential fd remained open')
child=subprocess.run([sys.executable,'-I','-c',"import os; assert not any(k.startswith('WISP_') for k in os.environ)"],capture_output=True)
assert child.returncode==0
digest=hashlib.sha256('\n'.join(k+':'+v for k,v in sorted(values.items())).encode()).hexdigest()
print('CONSUMED '+digest,flush=True)
wait('after')
