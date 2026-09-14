"""Synthetic RSA publisher signatures only; no live identity or credential access."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/mac-mini"))
import artifact_signature as signatures


@pytest.fixture(scope="module")
def synthetic_key(tmp_path_factory):
    root = tmp_path_factory.mktemp("synthetic-publisher")
    private = root / "synthetic.pem"
    subprocess.run(["/usr/bin/openssl", "genrsa", "-out", str(private), "2048"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    private.chmod(0o600)
    public = subprocess.check_output(["/usr/bin/openssl", "rsa", "-in", str(private), "-pubout"], stderr=subprocess.DEVNULL)
    yield private, public
    private.unlink()


@pytest.fixture
def candidate(synthetic_key):
    private, public = synthetic_key
    raw = b"synthetic immutable offline archive"
    identity = signatures.digest(public)
    trust = signatures.canonical({"schema_version": 1, "keys": {identity: public.decode()}})
    value = signatures.statement(key_id=identity, source_commit="a" * 40,
        bundle_sha256=signatures.digest(raw), release_sequence=12, issued_at=1000, expires_at=2000)
    with private.open("rb") as key:
        envelope = signatures.sign_statement(value, private_key_fd=key.fileno())
    kwargs = dict(trust_sha256=signatures.digest(trust), key_id=identity,
                  source_commit="a" * 40, bundle_sha256=signatures.digest(raw), release_sequence=12, now=1100)
    return raw, envelope, trust, kwargs


def verify(candidate):
    raw, envelope, trust, kwargs = candidate
    return signatures.verify_artifact(raw, envelope, trust, **kwargs)


def test_valid_signature(candidate):
    result = verify(candidate)
    assert result.source_commit == "a" * 40
    assert result.release_sequence == 12


@pytest.mark.parametrize("field,value", [("schema_version", 0), ("schema_version", True),
    ("algorithm", "none"), ("purpose", "other"), ("key_id", "b" * 64),
    ("source_commit", "b" * 40), ("bundle_sha256", "b" * 64),
    ("release_sequence", 11), ("issued_at", 1101), ("expires_at", 1100),
    ("extra", "unknown")])
def test_metadata_tampering_refused(candidate, field, value):
    raw, envelope, trust, kwargs = candidate
    changed = json.loads(envelope)
    changed["statement"][field] = value
    with pytest.raises(signatures.SignatureRefused):
        signatures.verify_artifact(raw, signatures.canonical(changed), trust, **kwargs)


@pytest.mark.parametrize("field,value", [("trust_sha256", "b" * 64), ("key_id", "b" * 64),
    ("source_commit", "b" * 40), ("bundle_sha256", "b" * 64), ("release_sequence", 11),
    ("now", 2000), ("now", 999)])
def test_external_authorization_must_match(candidate, field, value):
    raw, envelope, trust, kwargs = candidate
    kwargs[field] = value
    with pytest.raises(signatures.SignatureRefused):
        signatures.verify_artifact(raw, envelope, trust, **kwargs)


def test_archive_substitution(candidate):
    raw, envelope, trust, kwargs = candidate
    with pytest.raises(signatures.SignatureRefused):
        signatures.verify_artifact(raw + b"x", envelope, trust, **kwargs)


def test_signature_and_unknown_publisher(candidate):
    raw, envelope, trust, kwargs = candidate
    data = json.loads(envelope)
    data["signature"] = base64.b64encode(bytes(256)).decode()
    with pytest.raises(signatures.SignatureRefused):
        signatures.verify_artifact(raw, signatures.canonical(data), trust, **kwargs)
    trust = signatures.canonical({"schema_version": 1, "keys": {}})
    kwargs["trust_sha256"] = signatures.digest(trust)
    with pytest.raises(signatures.SignatureRefused):
        signatures.verify_artifact(raw, envelope, trust, **kwargs)


def test_duplicate_fields_fail_closed(candidate):
    raw, envelope, trust, kwargs = candidate
    envelope = envelope[:-1] + b',"signature":"invalid"}'
    with pytest.raises(signatures.SignatureRefused):
        signatures.verify_artifact(raw, envelope, trust, **kwargs)


def test_replay_and_downgrade_consume(candidate, tmp_path):
    tmp_path.chmod(0o700)
    receipt = verify(candidate)
    ledger = tmp_path / "releases.json"
    signatures.consume_release(receipt, ledger, now=1100)
    assert json.loads(ledger.read_text())["release_sequence"] == 12
    with pytest.raises(signatures.SignatureRefused):
        signatures.consume_release(receipt, ledger, now=1100)
    from dataclasses import replace
    with pytest.raises(signatures.SignatureRefused):
        signatures.consume_release(replace(receipt, release_sequence=11), ledger, now=1100)
    signatures.consume_release(replace(receipt, release_sequence=13), ledger, now=1100)
    ledger.unlink()
    with pytest.raises(signatures.SignatureRefused):
        signatures.consume_release(replace(receipt, release_sequence=14), ledger, now=1100)


@pytest.mark.parametrize("problem", ["symlink", "corrupt", "permissions", "expired"])
def test_ledger_safety(candidate, tmp_path, problem):
    tmp_path.chmod(0o700)
    ledger = tmp_path / "releases.json"
    if problem == "symlink":
        ledger.symlink_to(tmp_path / "target")
    elif problem == "corrupt":
        ledger.write_text("{}"); ledger.chmod(0o600)
    elif problem == "permissions":
        tmp_path.chmod(0o755)
    with pytest.raises(signatures.SignatureRefused):
        signatures.consume_release(verify(candidate), ledger, now=2000 if problem == "expired" else 1100)


def test_signing_subprocess_receives_no_private_material(monkeypatch, synthetic_key):
    original = signatures.subprocess.run
    calls = []
    def observed(argv, **kwargs):
        calls.append((argv, kwargs))
        return original(argv, **kwargs)
    monkeypatch.setattr(signatures.subprocess, "run", observed)
    private, public = synthetic_key
    value = signatures.statement(key_id=signatures.digest(public), source_commit="a" * 40,
        bundle_sha256="b" * 64, release_sequence=1, issued_at=1, expires_at=2)
    with private.open("rb") as key:
        signatures.sign_statement(value, private_key_fd=key.fileno())
    argv, options = calls[-1]
    assert argv[-1].startswith("/dev/fd/")
    assert str(private) not in argv
    assert b"PRIVATE KEY" not in options["input"]
    assert options["env"] == {"PATH": "/usr/bin:/bin"}
    assert options["stderr"] == subprocess.DEVNULL


def test_signer_without_approval_is_redacted(tmp_path):
    tool = ROOT / "build-support/sign_mini_artifact.py"
    result = subprocess.run([sys.executable, str(tool), "--bundle", "SECRET-SENTINEL", "--source-sha", "a" * 40,
        "--bundle-sha256", "c" * 64, "--key-id", "b" * 64, "--public-key", "public.pem", "--key-fd", "7", "--sequence", "1", "--issued-at", "1",
        "--expires-at", "2", "--output", str(tmp_path / "output")], capture_output=True)
    assert result.returncode == 1
    assert b"SECRET-SENTINEL" not in result.stdout + result.stderr
    assert b"publisher_signing_refused" in result.stdout
    assert not (tmp_path / "output").exists()


def test_parallel_consumption_accepts_once(candidate, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    tmp_path.chmod(0o700)
    verified = verify(candidate)
    def consume(_):
        try:
            signatures.consume_release(verified, tmp_path / "ledger.json", now=1100)
            return True
        except signatures.SignatureRefused:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(consume, range(8)))
    assert results.count(True) == 1


def test_initial_crash_marker_refuses(candidate, tmp_path):
    tmp_path.chmod(0o700)
    marker = tmp_path / "ledger.json.lock"
    marker.write_bytes(b"1")
    marker.chmod(0o600)
    with pytest.raises(signatures.SignatureRefused):
        signatures.consume_release(verify(candidate), tmp_path / "ledger.json", now=1100)


def test_unpinned_replacement_public_key_refused(candidate, synthetic_key):
    raw, envelope, trust, kwargs = candidate
    replacement = json.loads(trust)
    replacement["keys"][kwargs["key_id"]] += "\n"
    changed = signatures.canonical(replacement)
    with pytest.raises(signatures.SignatureRefused):
        signatures.verify_artifact(raw, envelope, changed, **kwargs)
    kwargs["trust_sha256"] = signatures.digest(changed)
    with pytest.raises(signatures.SignatureRefused):
        signatures.verify_artifact(raw, envelope, changed, **kwargs)


@pytest.fixture
def signed_runtime(tmp_path, synthetic_key):
    import time
    from tests.test_node_prep import bundle
    import node_prep
    path, archive_digest = bundle(tmp_path, runtime=True)
    raw, manifest = node_prep.validate_bundle(path, archive_digest)
    private, public = synthetic_key
    identity = signatures.digest(public)
    trust = signatures.canonical({'schema_version': 1, 'keys': {identity: public.decode()}})
    now = int(time.time())
    value = signatures.statement(key_id=identity, source_commit=manifest['source_commit'],
        bundle_sha256=archive_digest, release_sequence=42, issued_at=now - 10, expires_at=now + 3600)
    with private.open('rb') as key:
        envelope = signatures.sign_statement(value, private_key_fd=key.fileno())
    authentication = {'envelope': envelope, 'trust': trust, 'trust_sha256': signatures.digest(trust),
                      'key_id': identity, 'release_sequence': 42}
    return raw, manifest, archive_digest, authentication


def altered_authentication(authentication, fault, synthetic_key):
    """Source and sequence substitutions remain cryptographically valid signatures."""
    result = authentication.copy()
    if fault == 'trust':
        result['trust'] += b' '
    elif fault == 'publisher':
        result['key_id'] = 'f' * 64
    else:
        envelope = json.loads(result['envelope'])
        if fault == 'signature':
            envelope['signature'] = base64.b64encode(bytes(256)).decode()
        elif fault == 'version':
            envelope['statement']['schema_version'] = 0
        else:
            field, value = ('source_commit', 'e' * 40) if fault == 'source' else ('release_sequence', 41)
            envelope['statement'][field] = value
            private, _ = synthetic_key
            with private.open('rb') as key:
                result['envelope'] = signatures.sign_statement(envelope['statement'], private_key_fd=key.fileno())
            return result
        result['envelope'] = signatures.canonical(envelope)
    return result


@pytest.mark.parametrize('fault', ['signature', 'trust', 'source', 'sequence', 'publisher', 'version'])
def test_primary_activate_rejects_real_crypto_before_export_or_transfer(signed_runtime, synthetic_key, monkeypatch, fault):
    import node_prep
    raw, manifest, archive_digest, authentication = signed_runtime
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('external')
        raise AssertionError('credential or network boundary crossed')
    monkeypatch.setattr(node_prep, 'keychain', forbidden)
    monkeypatch.setattr(node_prep, 'ssh', forbidden)
    monkeypatch.setattr(node_prep, 'record_binding', forbidden)
    monkeypatch.setattr(node_prep, 'current_peer', forbidden)
    with pytest.raises(signatures.SignatureRefused):
        node_prep.activate({'node_id': 'nSynthetic'}, raw, manifest, archive_digest,
            authentication=altered_authentication(authentication, fault, synthetic_key))
    assert calls == []


def receiver_payload(signed_runtime, authentication):
    raw, manifest, archive_digest, _ = signed_runtime
    return {'schema_version': 1, 'operation': 'stage', 'node_id': 'nSynthetic',
            'bundle': base64.b64encode(raw).decode(), 'bundle_sha256': archive_digest,
            'source_commit': manifest['source_commit'],
            'credentials': {'mini-inference': 'a' * 64, 'mini-node': 'b' * 64},
            'publisher': {'envelope': base64.b64encode(authentication['envelope']).decode(),
                'trust': base64.b64encode(authentication['trust']).decode(),
                'trust_sha256': authentication['trust_sha256'], 'key_id': authentication['key_id'],
                'release_sequence': authentication['release_sequence']}}


@pytest.mark.parametrize('fault', ['signature', 'trust', 'source', 'sequence', 'publisher', 'version'])
def test_receiver_rejects_real_crypto_before_staging_or_import(signed_runtime, synthetic_key, monkeypatch, fault):
    import receiver
    authentication = altered_authentication(signed_runtime[3], fault, synthetic_key)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('mutation')
        raise AssertionError('staging or credential import boundary crossed')
    monkeypatch.setattr(receiver, 'materialize', forbidden)
    monkeypatch.setattr(receiver, 'execute', forbidden)
    monkeypatch.setattr(receiver, 'root_path', forbidden)
    monkeypatch.setattr(receiver, 'active_jobs', forbidden)
    monkeypatch.setattr(receiver, 'backend_ports_silent', forbidden)
    with pytest.raises(signatures.SignatureRefused):
        receiver.install(receiver_payload(signed_runtime, authentication))
    assert calls == []


def test_primary_real_signature_consumed_before_export_and_replay(signed_runtime, monkeypatch, tmp_path):
    import node_prep
    raw, manifest, archive_digest, authentication = signed_runtime
    home = tmp_path / 'synthetic-home'
    home.mkdir(mode=0o700)
    (home / '.moe').mkdir(mode=0o700)
    monkeypatch.setattr(node_prep.Path, 'home', lambda: home)
    events = []
    def source_check(source):
        assert source == manifest['source_commit']
        events.append('source')
    monkeypatch.setattr(node_prep, 'clean_source', source_check)
    monkeypatch.setattr(node_prep, 'current_peer', lambda plan: events.append('peer'))
    def export(operation):
        assert operation == 'export-mini'
        ledger = json.loads((home / '.moe/artifact-releases.json').read_text())
        assert ledger['release_sequence'] == 42
        events.append('export')
        return json.dumps({'mini-inference': 'a' * 64, 'mini-node': 'b' * 64}).encode()
    monkeypatch.setattr(node_prep, 'keychain', export)
    def transfer(plan, source, *, data):
        assert source.encode() == b'synthetic inert fixture'
        payload = json.loads(data)
        assert base64.b64decode(payload['bundle']) == raw
        assert base64.b64decode(payload['publisher']['envelope']) == authentication['envelope']
        events.append('transfer')
        return b'{"schema_version":1,"status":"complete","jobs_enabled":false,"gateway_qualification_required":true}'
    monkeypatch.setattr(node_prep, 'ssh', transfer)
    monkeypatch.setattr(node_prep, 'record_binding', lambda *args: events.append('binding'))
    node_prep.activate({'node_id': 'nSynthetic'}, raw, manifest, archive_digest, authentication=authentication)
    assert events == ['source', 'peer', 'export', 'transfer', 'binding']
    with pytest.raises(signatures.SignatureRefused):
        node_prep.activate({'node_id': 'nSynthetic'}, raw, manifest, archive_digest, authentication=authentication)
    assert events.count('export') == events.count('transfer') == events.count('binding') == 1


def test_receiver_real_signature_consumed_before_materialization_and_replay(signed_runtime, monkeypatch, tmp_path):
    import receiver
    root = tmp_path / 'synthetic-receiver'
    root.mkdir(mode=0o700)
    monkeypatch.setattr(receiver, 'root_path', lambda: root)
    monkeypatch.setattr(receiver, 'active_jobs', lambda: False)
    monkeypatch.setattr(receiver, 'backend_ports_silent', lambda: True)
    calls = []
    class StopBeforeMaterialization(Exception):
        pass
    def materialize(*args):
        assert json.loads((root / 'artifact-releases.json').read_text())['release_sequence'] == 42
        calls.append('materialize')
        raise StopBeforeMaterialization()
    def forbidden(*args, **kwargs):
        raise AssertionError('no live credential import permitted')
    monkeypatch.setattr(receiver, 'materialize', materialize)
    monkeypatch.setattr(receiver, 'execute', forbidden)
    payload = receiver_payload(signed_runtime, signed_runtime[3])
    with pytest.raises(StopBeforeMaterialization):
        receiver.install(payload)
    with pytest.raises(signatures.SignatureRefused):
        receiver.install(payload)
    assert calls == ['materialize']


@pytest.mark.parametrize('side', ['receiver', 'primary'])
@pytest.mark.parametrize('first_sequence', [42, 43])
def test_sequence_publication_is_one_operation(signed_runtime, synthetic_key, monkeypatch, tmp_path, side, first_sequence):
    """Pause after ledger consumption: a competing publisher cannot overtake."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import receiver, node_prep
    raw, manifest, digest, original = signed_runtime
    auths = {42: original}
    doc = json.loads(original['envelope'])['statement']
    doc['release_sequence'] = 43
    with synthetic_key[0].open('rb') as key:
        auths[43] = dict(original, envelope=signatures.sign_statement(doc, private_key_fd=key.fileno()), release_sequence=43)
    root = tmp_path / 'operations'
    root.mkdir(mode=0o700)
    entered, resume = Event(), Event()
    published = []
    def pause():
        entered.set()
        assert resume.wait(10), 'test failed to release suspended installer'
    if side == 'receiver':
        monkeypatch.setattr(receiver, 'root_path', lambda: root)
        monkeypatch.setattr(receiver, 'active_jobs', lambda: False)
        monkeypatch.setattr(receiver, 'backend_ports_silent', lambda: True)
        monkeypatch.setattr(receiver, 'runtime_health', lambda *args: None)
        monkeypatch.setattr(receiver, 'execute', lambda *args: None)
        materialize = receiver.materialize
        def paused_materialize(*args):
            pause()
            return materialize(*args)
        monkeypatch.setattr(receiver, 'materialize', paused_materialize)
        def install(sequence):
            receiver.install(receiver_payload(signed_runtime, auths[sequence]))
        def receipt(): return json.loads((root / 'receipt.json').read_text())
    else:
        monkeypatch.setattr(node_prep.Path, 'home', lambda: root)
        (root / '.moe').mkdir(mode=0o700)
        monkeypatch.setattr(node_prep, 'clean_source', lambda *args: None)
        monkeypatch.setattr(node_prep, 'current_peer', lambda *args: None)
        def export(*args):
            pause()
            return json.dumps({'mini-inference': 'a'*64, 'mini-node': 'b'*64}).encode()
        monkeypatch.setattr(node_prep, 'keychain', export)
        monkeypatch.setattr(node_prep, 'ssh', lambda *args, **kwargs:
            b'{"schema_version":1,"status":"complete","jobs_enabled":false,"gateway_qualification_required":true}')
        monkeypatch.setattr(node_prep, 'record_binding', lambda plan, manifest, digest, verified:
            published.append({'release_sequence': verified.release_sequence, 'statement_sha256': verified.statement_sha256}))
        def install(sequence):
            node_prep.activate({'node_id': 'nSynthetic'}, raw, manifest, digest, authentication=auths[sequence])
        def receipt(): return published[-1]
    other = 43 if first_sequence == 42 else 42
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(install, first_sequence)
        try:
            assert entered.wait(10)
            with pytest.raises(BlockingIOError): install(other)
            assert not published
        finally:
            resume.set()
        first.result()
    assert receipt()['release_sequence'] == first_sequence
    if other > first_sequence:
        install(other)
    else:
        with pytest.raises(signatures.SignatureRefused): install(other)
    assert receipt()['release_sequence'] == 43
    expected_statement = json.loads(auths[43]['envelope'])['statement']
    assert receipt()['statement_sha256'] == signatures.digest(signatures.canonical(expected_statement))
    ledger = root / ('artifact-releases.json' if side == 'receiver' else '.moe/artifact-releases.json')
    assert json.loads(ledger.read_text())['release_sequence'] == 43
    with pytest.raises(signatures.SignatureRefused): install(43)
    if side == 'receiver':
        owner = json.loads((root / receipt()['provisioning_id'] / '.owner.json').read_text())
        assert owner['release_sequence'] == receipt()['release_sequence']
        assert owner['statement_sha256'] == receipt()['statement_sha256']


@pytest.mark.parametrize('resource_contract', [True, False])
@pytest.mark.parametrize('failure', [False, True])
def test_artifact_health_uses_only_synthetic_capacity_and_leases(tmp_path, monkeypatch, resource_contract, failure):
    """Execute the actual health source against inert versioned module contracts."""
    import ast
    import platform
    from types import ModuleType
    source = ast.parse((ROOT / 'build-support/mini_artifact.py').read_text())
    health = next(ast.literal_eval(item.value) for item in source.body
                  if isinstance(item, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'HEALTH'
                                                         for target in item.targets))
    (tmp_path / 'dependencies.json').write_text('{}')
    package = ModuleType('mini')
    package.__path__ = []
    gateway, node, store, resources = (ModuleType('mini.' + name) for name in ('gateway', 'node', 'store', 'resources'))
    sentinel = tmp_path / 'never-create-production-leases'
    resources.LOCK_ROOT = sentinel
    original_statvfs = os.statvfs
    observed = []

    class StoreFixture:
        def __init__(self, path, identity):
            assert identity == 'synthetic-build'
            fs = os.statvfs(path)
            assert fs.f_bavail * fs.f_frsize >= 150 * 1024**3
            assert os.statvfs is not original_statvfs
            if resource_contract:
                assert resources.LOCK_ROOT == path.parent / 'volume-leases'
                assert resources.LOCK_ROOT != sentinel
                resources.LOCK_ROOT.mkdir(mode=0o700)
            observed.append(path.parent)
            if failure:
                raise RuntimeError('synthetic_store_refusal')

        def status(self):
            return {'jobs_enabled': False}

    class NodeFixture:
        def __init__(self, key, fixture):
            self.store = fixture

    store.Store = StoreFixture
    node.Node = NodeFixture
    gateway.Gateway = lambda *args: None
    gateway.route_response = lambda path, value: value
    for name, module in [('mini', package), ('mini.gateway', gateway), ('mini.node', node), ('mini.store', store)]:
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(sys.modules, 'mini.resources', resources if resource_contract else None)
    monkeypatch.setattr(platform, 'python_version', lambda: '3.13.14')
    monkeypatch.setattr(sys, 'argv', ['runtime-health.py', str(tmp_path)])
    monkeypatch.setattr(sys, 'path', list(sys.path))
    if failure:
        with pytest.raises(RuntimeError, match='synthetic_store_refusal'):
            exec(compile(health, 'runtime-health.py', 'exec'), {})
    else:
        exec(compile(health, 'runtime-health.py', 'exec'), {})
    assert os.statvfs is original_statvfs
    assert resources.LOCK_ROOT == sentinel
    assert observed and all(not path.exists() for path in observed)
    assert not sentinel.exists()
