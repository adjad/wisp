"""Malformed private frames fail closed with fixed diagnostics."""
import json
import os
import struct

import pytest

from service import credential_pipe as pipe
from tests.credential_pipe_fixture import fixture_pipe

VALUES={pipe.NAMES[0]:'a'*64,pipe.NAMES[1]:'b'*64,pipe.NAMES[2]:'c'*64}


def rewrite(frame, mutate):
    doc=json.loads(frame[12:]);mutate(doc)
    body=json.dumps(doc).encode()
    return pipe.MAGIC+struct.pack('!I',len(body))+body


@pytest.mark.parametrize('mutation',[
    lambda f:f[:-1],lambda f:f+b'extra',lambda f:b'WRONG123'+f[8:],lambda f:f[:8]+b'\xff'*4+f[12:],
    lambda f:rewrite(f,lambda d:d.update(pid=d['pid']+1)),
    lambda f:rewrite(f,lambda d:d.update(uid=d['uid']+1)),
    lambda f:rewrite(f,lambda d:d.update(role='node')),
    lambda f:rewrite(f,lambda d:d.update(version=True)),
    lambda f:rewrite(f,lambda d:d.update(generation='bad')),
    lambda f:rewrite(f,lambda d:d.update(extra=True)),
    lambda f:rewrite(f,lambda d:d['credentials'].update(unknown='d'*64)),
    lambda f:rewrite(f,lambda d:d['credentials'].update({pipe.NAMES[0]:'short'})),
    lambda f:rewrite(f,lambda d:d['credentials'].update({pipe.NAMES[0]:'b'*64})),
])
def test_invalid_frame_fixed_error_and_closed_fd(mutation):
    with fixture_pipe(VALUES,mutate=mutation):
        with pytest.raises(ValueError,match='^Native credential pipe unavailable$'):
            pipe.consume('primary')
        with pytest.raises(OSError):os.fstat(0)
        assert 'WISP_CREDENTIAL_PIPE' not in os.environ


def test_one_shot_and_no_inherited_environment():
    with fixture_pipe(VALUES):
        assert pipe.consume('primary')==(VALUES,'absent')
        with pytest.raises(ValueError,match='Native credential pipe unavailable'):pipe.consume('primary',optional=True)
        assert not set(pipe.NAMES).intersection(os.environ)


def test_wrong_descriptor_identity_refuses():
    with fixture_pipe(VALUES):
        os.environ['WISP_CREDENTIAL_PIPE']='v1:0:999999'
        with pytest.raises(ValueError):pipe.consume('primary')


def test_leaked_writer_deadline_refuses(monkeypatch):
    with fixture_pipe(VALUES):
        monkeypatch.setattr(pipe.select,'select',lambda *a:([],[],[]))
        with pytest.raises(ValueError):pipe.consume('primary')


def test_old_exec_environment_refuses_and_redacts(monkeypatch):
    with fixture_pipe(VALUES):
        monkeypatch.setenv(pipe.NAMES[0],VALUES[pipe.NAMES[0]])
        with pytest.raises(ValueError,match='^Native credential pipe unavailable$'):pipe.consume('primary')
        assert pipe.NAMES[0] not in os.environ
