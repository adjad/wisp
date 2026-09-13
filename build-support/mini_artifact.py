"""Build and verify a relocatable, offline mini runtime for one exact candidate.

No service is installed or activated. Network is used only to acquire public,
hash-locked dependencies during build; installation and health checks are offline.
"""
import argparse
import csv
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pipeline
from bootstrap_uv import ensure_python_archive, unpack_runtime

ROOT = pipeline.ROOT
sys.path.insert(0, str(ROOT))
from mini.build_bundle import build

HEALTH = '''import importlib.metadata, json, os, pathlib, sys, tempfile
root=pathlib.Path(sys.argv[1])
import platform
assert platform.python_version() == '3.13.14'
sys.path.insert(0,str(root/'runtime'))
expected=json.loads((root/'dependencies.json').read_text())
for name,version in expected.items():
    assert importlib.metadata.version(name)==version
import httpx, uvicorn
from mini.gateway import Gateway, route_response
from mini.node import Node
from mini.store import Store
assert route_response('/health', {'status':'ok'}) == {'status':'ok'}
with tempfile.TemporaryDirectory() as d:
    scratch=pathlib.Path(d).resolve()
    try:
        import mini.resources as resources
    except ModuleNotFoundError as error:
        if error.name != 'mini.resources':
            raise
        resources=None  # Baseline source contract has no resource module.
    original_statvfs=os.statvfs
    original_lock_root=resources.LOCK_ROOT if resources is not None else None
    # Fixture-only capacity and device leases. Never test against real disk
    # capacity or write the production shared uid/device lock root.
    synthetic_fs=os.statvfs_result((4096,4096,134217728,134217728,134217728,1000000,1000000,1000000,0,255))
    try:
        os.statvfs=lambda path: synthetic_fs
        if resources is not None:
            resources.LOCK_ROOT=scratch/'volume-leases'
        store=Store(scratch/'state', 'synthetic-build')
        node=Node('c'*64, store)
        gateway=Gateway('a'*64,'b'*64)
        assert not node.store.status()['jobs_enabled']
    finally:
        os.statvfs=original_statvfs
        if resources is not None:
            resources.LOCK_ROOT=original_lock_root
'''


def run(argv, **kwargs):
    subprocess.run([str(v) for v in argv], check=True, env=pipeline.clean_env(), **kwargs)


def prepare(destination, *, strict=True):
    config = pipeline.CONFIG
    state = destination / 'build-cache'
    state.mkdir()
    runner = pipeline.Runner()
    pipeline.check_locks()
    qualification = pipeline.doctor(runner, strict=strict)
    ensure_python_archive(config, state, offline=False)
    payload = destination / 'payload'
    payload.mkdir()
    (payload / 'provisioning').mkdir()
    (payload / 'provisioning/receiver.py').write_bytes(
        (ROOT / 'infra/mac-mini/artifact_signature.py').read_bytes() + b'\n' +
        (ROOT / 'infra/mac-mini/socket_posture.py').read_bytes() + b'\n' +
        (ROOT / 'infra/mac-mini/bundle_contract.py').read_bytes() + b'\n' +
        (ROOT / 'infra/mac-mini/receiver.py').read_bytes())
    unpack_runtime(config, state, payload / 'venv')
    python = payload / 'venv/bin/python3'
    lock = ROOT / 'mini/requirements.txt'
    wheels = payload / 'wheels'
    wheels.mkdir()
    # Registry hashes and the complete closure are committed. Never resolve an
    # sdist or install from the network. This download step cannot execute wheels.
    run([python, '-I', '-m', 'pip', '--isolated', 'download', '--require-hashes',
         '--only-binary=:all:', '--dest', wheels, '-r', lock])
    run([python, '-I', '-m', 'pip', '--isolated', 'install', '--no-index',
         '--find-links', wheels, '--require-hashes', '--only-binary=:all:',
         '--no-compile', '-r', lock])
    run([python, '-I', '-m', 'pip', 'check'])
    versions = {}
    for line in lock.read_text().splitlines():
        if '==' in line and not line.startswith((' ', '#')):
            name, version = line.split()[0].split('==')
            versions[name] = version
    (payload / 'dependencies.json').write_text(json.dumps(versions, sort_keys=True))
    (payload / 'runtime-health.py').write_text(HEALTH)
    for source, name in [('keychain-helper.swift','keychain-helper'), ('mini-launcher.swift','mini-launcher')]:
        run(['/usr/bin/swiftc', '-parse-as-library', '-module-cache-path', destination/'module-cache',
             ROOT/'app/Sources/WispApp/BackendCredentials.swift', ROOT/'infra/mac-mini'/source, '-o', payload/name])
        run(['/usr/bin/codesign', '--force', '--sign', '-', payload/name])
        run(['/usr/bin/codesign', '--verify', '--strict', payload/name])
        run([payload/name, 'protocol-version'])
    (payload/'runtime/mini').mkdir(parents=True)
    from mini.build_bundle import FILES
    for name in FILES:
        shutil.copyfile(ROOT/'mini'/name, payload/'runtime/mini'/name)
    run([python, '-I', '-B', payload/'runtime-health.py', payload])
    # Only python -m entrypoints are supported. pip's console scripts contain
    # temporary absolute shebangs; omit them and their RECORD rows entirely.
    for path in (payload / 'venv/bin').iterdir():
        if path.name not in {'python3', 'python3.13'}:
            path.unlink()
    for record in payload.glob('venv/lib/python*/site-packages/*.dist-info/RECORD'):
        rows = [row for row in csv.reader(io.StringIO(record.read_text())) if '/bin/' not in row[0]]
        out = io.StringIO()
        csv.writer(out, lineterminator='\n').writerows(rows)
        record.write_text(out.getvalue())
    # Remove generated caches and build-only paths; preserve wheel RECORD files.
    for path in list(payload.rglob('__pycache__')):
        shutil.rmtree(path)
    return payload, {'source_commit': pipeline.git('rev-parse','HEAD'),
                     'strict_toolchain': strict, 'toolchain': qualification,
                     'python_archive_sha256': config['python_archive_sha256'],
                     'python_version': config['python'], 'python_build': config['python_build'],
                     'lock_sha256': hashlib.sha256(lock.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--development', action='store_true')
    args = parser.parse_args()
    if pipeline.git('rev-parse','HEAD') != args.source_sha or pipeline.git('status','--porcelain','--untracked-files=normal'):
        raise ValueError('Build requires the clean exact candidate')
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='mini-build-') as temporary:
        work = Path(temporary)
        first_work, second_work = work/'first', work/'second'
        first_work.mkdir()
        second_work.mkdir()
        payload, provenance = prepare(first_work, strict=not args.development)
        other_payload, other_provenance = prepare(second_work, strict=not args.development)
        first, second = output/'mini.tar.gz', work/'second.tar.gz'
        if pipeline.git('status','--porcelain','--untracked-files=normal'):
            raise ValueError('Candidate changed during artifact creation')
        manifest = build(first, payload=payload, provenance=provenance, expected_sha=args.source_sha)
        build(second, payload=other_payload, provenance=other_provenance, expected_sha=args.source_sha)
        if first.read_bytes() != second.read_bytes():
            raise ValueError('Non-deterministic mini archive')
        sys.path.insert(0,str(ROOT/'infra/mac-mini'))
        import receiver
        files=receiver.unpack(first.read_bytes())
        relocated=work/'relocated'
        relocated.mkdir(mode=0o700)
        receiver.materialize(files, manifest, relocated)
        receiver.runtime_health(relocated)
        receipt={'source_commit':args.source_sha,'bundle_sha256':hashlib.sha256(first.read_bytes()).hexdigest(),
                 'deterministic':True,'relocated_health':True,'provenance':provenance}
        (output/'verification.json').write_text(json.dumps(receipt,indent=2,sort_keys=True)+'\n')


if __name__ == '__main__':
    main()
