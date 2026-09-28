"""Exact-case A04 qualification in a disposable AF_UNIX-only sandbox.

The generic Simulation profile retains its signing/network denials. This gate
owns all fixture descendants and imports no user's credentials or browser state.
"""
import argparse
import hashlib
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pipeline
from native_peer_gate import PLUGIN, fixture_process

MODULE = 'tests/test_browser_transport_native.py'
EXPECTED = {MODULE + '::test_signed_connected_peer_private_transport[' + mode + '-' + outcome + ']'
            for mode, outcome in [('normal', 'accepted'), ('oversize', 'denied'), ('empty', 'denied'),
                                  ('truncated', 'denied'), ('header_eof', 'denied'), ('timeout', 'denied'),
                                  ('backpressure', 'denied'), ('wrong_requirement', 'denied'),
                                  ('different_executable', 'denied')]}
EXPECTED |= {MODULE + '::test_mutual_signed_transport_rejects_wrong_server[' + flag + ']'
             for flag in ('False', 'True')}
PROBES = {'generic_codesign_denied', 'generic_unix_denied', 'dedicated_ip_denied',
          'dedicated_private_home_denied', 'dedicated_keychain_tool_denied',
          'dedicated_ip_outbound_denied', 'dedicated_unrelated_unix_denied',
          'dedicated_outside_connect_denied', 'dedicated_bound_outside_connect_denied'}


def profile(scratch, python, canary):
    policy = pipeline.simulation_profile(scratch, python, local_signing=True)
    # Narrow the inherited build-write permission to this disposable tree only.
    policy = '\n'.join(line for line in policy.splitlines() if not line.startswith('(allow file-write*'))
    policy += '\n(allow file-write* (subpath ' + json.dumps(str(scratch)) + ') (literal "/dev/null"))'
    # AF_UNIX only: no IP address and no endpoint outside the private socket tree.
    sockets = json.dumps(str(scratch / 'sockets'))
    policy += '\n(allow network-bind network-inbound (local unix-socket (subpath ' + sockets + ')))'
    policy += '\n(allow network-outbound (remote unix-socket (subpath ' + sockets + ')))'
    policy += '\n(deny file-read-data (literal ' + json.dumps(str(canary)) + '))'
    return policy


def validate(report, sha, *, allow_dirty=False):
    if (report.get('schema_version') != 1 or report.get('scope') != 'disposable-signed-af-unix'
            or report.get('candidate_sha') != sha or report.get('ending_sha') != sha
            or report.get('status') != 'PASS' or report.get('returncode') != 0
            or not allow_dirty and (report.get('clean_start') is not True or report.get('clean_end') is not True
                                    or report.get('dirty_allowed') is not False)):
        raise ValueError('browser_bridge_gate_unproven')
    cases = report.get('cases', {})
    rows = cases.get('rows', [])
    required = {(node, phase) for node in EXPECTED for phase in ('setup', 'call', 'teardown')}
    if (cases.get('collected') != 11 or cases.get('exitstatus') != 0 or len(rows) != 33
            or {(r.get('nodeid'), r.get('phase')) for r in rows} != required
            or any(r.get('outcome') != 'passed' or r.get('xfail') is not False for r in rows)):
        raise ValueError('browser_bridge_gate_incomplete')
    probes = report.get('probes', {})
    if set(probes) != PROBES or any(value is not True for value in probes.values()):
        raise ValueError('browser_bridge_gate_boundary_unproven')
    return report


def load_pinned(path, digest, sha, *, allow_dirty=False):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('browser_bridge_gate_evidence_changed')
    return validate(json.loads(raw), sha, allow_dirty=allow_dirty)


def boundary_probes(scratch, python, canary, dedicated):
    generic = pipeline.simulation_profile(scratch, python)
    denied = '''import sys,subprocess,socket
try:
 if sys.argv[1]=='codesign': subprocess.run(['/usr/bin/codesign','--version'],check=True)
 elif sys.argv[1]=='keychain': subprocess.run(['/usr/bin/security','help'],check=True)
 elif sys.argv[1]=='private': open(sys.argv[2]).read()
 elif sys.argv[1]=='ip':
  s=socket.socket();s.bind(('127.0.0.1',0))
 elif sys.argv[1]=='outbound':
  s=socket.socket();s.settimeout(0.2);s.connect(('192.0.2.1',443))
 else:
  s=socket.socket(socket.AF_UNIX);s.bind(sys.argv[2])
except PermissionError: sys.exit(0)
sys.exit(1)
'''
    results = {}
    for name, policy, operation in (
            ('generic_codesign_denied', generic, 'codesign'),
            ('generic_unix_denied', generic, 'unix'),
            ('dedicated_ip_denied', dedicated, 'ip'),
            ('dedicated_ip_outbound_denied', dedicated, 'outbound'),
            ('dedicated_unrelated_unix_denied', dedicated, 'unix'),
            ('dedicated_private_home_denied', dedicated, 'private'),
            ('dedicated_keychain_tool_denied', dedicated, 'keychain')):
        target = canary if operation == 'private' else scratch / 'denied'
        proc = fixture_process(['/usr/bin/sandbox-exec', '-p', policy, str(python), '-I', '-c', denied,
                                operation, str(target)], timeout=15, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        results[name] = proc.returncode == 0
    # The supervisor owns an unrelated endpoint with a reachable listener. A
    # denied connect must be due to policy, not a missing file/refused listener.
    with socket.socket(socket.AF_UNIX) as outside:
        endpoint = scratch / 'outside-peer'
        outside.bind(str(endpoint))
        outside.listen(2)
        probe = """import socket,sys
s=socket.socket(socket.AF_UNIX)
if sys.argv[1]=='bound': s.bind(sys.argv[3])
try: s.connect(sys.argv[2])
except PermissionError: sys.exit(0)
sys.exit(1)
"""
        for mode in ('unbound', 'bound'):
            proc = fixture_process(['/usr/bin/sandbox-exec', '-p', dedicated, str(python), '-I', '-c', probe,
                mode, str(endpoint), str(scratch / 'sockets' / 'bound-client')], timeout=15,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            name = 'dedicated_bound_outside_connect_denied' if mode == 'bound' else 'dedicated_outside_connect_denied'
            results[name] = proc.returncode == 0
    return results


def run_gate(*, expected_sha=None, allow_dirty=False):
    sha = pipeline.git('rev-parse', 'HEAD')
    if expected_sha is not None and sha != expected_sha:
        raise ValueError('browser_bridge_head_changed')
    clean = not pipeline.git('status', '--porcelain')
    if not clean and not allow_dirty:
        raise ValueError('browser_bridge_dirty_source')
    sys.path.insert(0, str(pipeline.ROOT))
    from scripts.run_simulation_qa import _child_environment
    started = time.monotonic()
    # Socket paths have a 104-byte limit. All descendants write inside this
    # short private tree; no generic /private/tmp write grant is made.
    with tempfile.TemporaryDirectory(prefix='wbb-', dir='/private/tmp') as temporary:
        scratch = Path(temporary).resolve()
        os.chmod(scratch, 0o700)
        (scratch / 'sockets').mkdir(mode=0o700)
        canary = scratch / 'private-home-canary'
        canary.write_text('synthetic private content')
        python = Path(sys.executable)
        dedicated = profile(scratch, python, canary)
        probes = boundary_probes(scratch, python, canary, dedicated)
        if not all(probes.values()):
            raise ValueError('browser_bridge_boundary_probe_failed: ' + str(probes))
        env = _child_environment(scratch)
        case_report = scratch / 'cases.json'
        (scratch / 'native_gate_plugin.py').write_text(PLUGIN)
        env.update(PYTHONPATH=str(scratch) + os.pathsep + str(pipeline.ROOT),
                   PEER_TEST_CASE_REPORT=str(case_report), BROWSER_BRIDGE_SOCKET_ROOT=str(scratch / 'sockets'))
        result = fixture_process(['/usr/bin/sandbox-exec', '-p', dedicated, str(python), '-B', '-m', 'pytest',
            '-p', 'pytest_asyncio.plugin', '-p', 'native_gate_plugin', '-q', '-rs', MODULE],
            cwd=pipeline.ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=180)
        report = dict(schema_version=1, scope='disposable-signed-af-unix', candidate_sha=sha,
            ending_sha=pipeline.git('rev-parse', 'HEAD'), clean_start=clean,
            clean_end=not pipeline.git('status', '--porcelain'), dirty_allowed=allow_dirty,
            returncode=result.returncode, cases=json.loads(case_report.read_text()) if case_report.exists() else {},
            probes=probes, duration_s=time.monotonic() - started, stdout=result.stdout, stderr=result.stderr,
            status='PASS' if result.returncode == 0 else 'FAIL')
        if result.returncode == 0:
            validate(report, sha, allow_dirty=allow_dirty)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--allow-dirty', action='store_true')
    args = parser.parse_args()
    report = run_gate(expected_sha=args.expected_sha, allow_dirty=args.allow_dirty)
    Path(args.report).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(dict(status=report['status'], scope=report['scope'], cases=len(EXPECTED))))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
