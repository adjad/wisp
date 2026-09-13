"""Synthetic restart receipt checks; no signals, Keychain, or lsof execution."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'infra/mac-mini'))
import node_prep as prep
import primary_runtime as runtime

SHA = 'a' * 40


@pytest.fixture
def context(tmp_path, monkeypatch):
    parent = tmp_path / '.moe'
    parent.mkdir(mode=0o700)
    config = parent / 'config.yaml'
    config.write_text('synthetic local configuration')
    config.chmod(0o600)
    events, ticks = [], [0.0]
    def clean(sha):
        assert sha == SHA
        events.append('source')
    monkeypatch.setattr(prep, 'clean_source', clean)
    monkeypatch.setattr(prep, 'restore_primary_local', lambda: events.append('restore'))
    original_rotate = prep.rotate_credential_generation
    def rotate(path):
        original_rotate(path)
        events.append(runtime.current_generation(parent))
        receipt = {'schema_version': 1, 'pid': 12345,
                   'generation': runtime.current_generation(parent),
                   'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest()}
        output = parent / 'backend-runtime.json'
        output.write_text(json.dumps(receipt))
        output.chmod(0o600)
    monkeypatch.setattr(prep, 'rotate_credential_generation', rotate)
    def run(args):
        assert args == ['/usr/sbin/lsof', '-nP', '-a', '-iTCP:8765', '-sTCP:LISTEN', '-Fp']
        events.append('listener')
        return b'p12345\n'
    monkeypatch.setattr(prep, 'run', run)
    monkeypatch.setattr(os, 'kill', lambda *a: pytest.fail('no process signals'))
    def sleep(delta): ticks[0] += delta
    options = dict(expected_source=SHA, home=tmp_path, timeout=0.3,
                   sleep=sleep, now=lambda: ticks[0])
    return parent, config, events, options


def test_fresh_generation_exact_current_listener_and_overlay_required(context):
    parent, config, events, options = context
    runtime.refresh(prep, **options)
    assert 'listener' in events and events[-1] == 'source'
    assert not (parent / '.helper-transaction.json').exists()


@pytest.mark.parametrize('kind', ['stale', 'schema_bool', 'pid_bool', 'digest', 'extra', 'missing', 'listener', 'multiple'])
def test_invalid_receipt_times_out_and_quarantines_with_another_epoch(context, monkeypatch, kind):
    parent, config, events, options = context
    original = prep.rotate_credential_generation
    def rotate(path):
        original(path)
        output = parent / 'backend-runtime.json'
        receipt = json.loads(output.read_text())
        if kind == 'stale': receipt['generation'] = '0' * 64
        elif kind == 'schema_bool': receipt['schema_version'] = True
        elif kind == 'pid_bool': receipt['pid'] = True
        elif kind == 'digest': receipt['config_sha256'] = '0' * 64
        elif kind == 'extra': receipt['unreviewed'] = True
        elif kind == 'missing': output.unlink(); return
        output.write_text(json.dumps(receipt))
    monkeypatch.setattr(prep, 'rotate_credential_generation', rotate)
    if kind in ('listener', 'multiple'):
        monkeypatch.setattr(prep, 'run', lambda *a: b'p999\n' if kind == 'listener' else b'p12345\np999\n')
    with pytest.raises(prep.Refused, match='backend_refresh_unverified'):
        runtime.refresh(prep, **options)
    assert json.loads((parent / '.helper-transaction.json').read_text())['phase'] == 'runtime_unverified'
    epochs = [value for value in events if len(value) == 64]
    assert len(epochs) == 2 and epochs[0] != epochs[1]


def test_timeout_retry_uses_fresh_epoch_and_keeps_local_configuration(context, monkeypatch):
    parent, config, events, options = context
    real_run = prep.run
    monkeypatch.setattr(prep, 'run', lambda *a: b'')
    with pytest.raises(prep.Refused): runtime.refresh(prep, **options)
    rejected = runtime.current_generation(parent)
    monkeypatch.setattr(prep, 'run', real_run)
    runtime.refresh(prep, **options)
    assert runtime.current_generation(parent) != rejected
    assert not (parent / '.helper-transaction.json').exists()
    assert config.read_text() == 'synthetic local configuration'


def test_config_change_during_listener_check_cannot_pass(context, monkeypatch):
    parent, config, events, options = context
    def run(*args):
        config.write_text('changed configuration')
        return b'p12345\n'
    monkeypatch.setattr(prep, 'run', run)
    with pytest.raises(prep.Refused): runtime.refresh(prep, **options)
    assert (parent / '.helper-transaction.json').exists()


def test_unrelated_journal_is_not_overwritten(context):
    parent, config, events, options = context
    marker = parent / '.helper-transaction.json'
    original = {'schema_version': 2, 'operation': 'helper'}
    prep.recovery_marker(marker, original)
    with pytest.raises(prep.Refused, match='unrelated'): runtime.refresh(prep, **options)
    assert json.loads(marker.read_text()) == original
    assert 'restore' not in events


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'mode', 'oversized'])
def test_descriptor_reader_refuses_unsafe_files(tmp_path, kind):
    target = tmp_path / 'target'
    target.write_bytes(b'{}')
    target.chmod(0o600)
    path = tmp_path / 'receipt'
    if kind == 'symlink': path.symlink_to(target)
    elif kind == 'hardlink': os.link(target, path)
    elif kind == 'fifo': os.mkfifo(path, 0o600)
    else:
        path.write_bytes(b'x' * 4097 if kind == 'oversized' else b'{}')
        path.chmod(0o644 if kind == 'mode' else 0o600)
    with pytest.raises((OSError, ValueError)): runtime.private_json(path)


@pytest.mark.parametrize('value', ['0' * 63, 'G' * 64, '0' * 64 + '\n'])
def test_generation_is_strict_private_64_hex(tmp_path, value):
    path = tmp_path / '.credential-generation'
    path.write_text(value)
    path.chmod(0o600)
    with pytest.raises(ValueError): runtime.current_generation(tmp_path)


def test_duplicate_receipt_fields_refused(tmp_path):
    path = tmp_path / 'receipt'
    path.write_text('{"pid":123,"pid":456}')
    path.chmod(0o600)
    with pytest.raises(ValueError): runtime.private_json(path)
