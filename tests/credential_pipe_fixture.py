"""Disposable test-only native framing, no Keychain or runtime state."""
import json
import os
import struct
from contextlib import contextmanager


@contextmanager
def fixture_pipe(values, role='primary', generation='absent', mutate=None):
    from service import credential_pipe
    # Each use models a separate launched service in tests sharing a process.
    credential_pipe._attempted = False
    saved = os.dup(0)
    reader, writer = os.pipe()
    doc = {'version':1,'pid':os.getpid(),'uid':os.getuid(),'role':role,
           'generation':generation,'credentials':values}
    body = json.dumps(doc).encode()
    frame = b'WISPCP1\n' + struct.pack('!I',len(body)) + body
    if mutate:
        frame = mutate(frame)
    os.write(writer, frame)
    os.close(writer)
    info = os.fstat(reader)
    os.dup2(reader, 0)
    os.close(reader)
    os.environ['WISP_CREDENTIAL_PIPE'] = f'v1:{info.st_dev}:{info.st_ino}'
    try:
        yield
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.environ.pop('WISP_CREDENTIAL_PIPE', None)
        credential_pipe._attempted = False
