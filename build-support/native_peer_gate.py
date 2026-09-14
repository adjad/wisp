"""Mandatory native security gate with only pre-reserved disposable ports.

The ordinary Simulation sandbox remains network-denied. This separate sandbox
runs exactly the reviewed peer/reclamation cases and negative boundary probes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import tempfile
import time

import pipeline

MODULE='tests/test_runtime_peer.py'
EXPECTED={MODULE+'::'+name for name in (
    'test_rogue_runtime_connection_gets_zero_bytes[health]',
    'test_rogue_runtime_connection_gets_zero_bytes[prompt]',
    'test_rogue_runtime_connection_gets_zero_bytes[ensure]',
    'test_attributed_disposable_process_receives_health',
    'test_gateway_rogue_upstream_gets_no_credential_or_prompt[/health]',
    'test_gateway_rogue_upstream_gets_no_credential_or_prompt[/v1/chat/completions]',
    'test_epoch_change_during_prewrite_inspection_sends_zero_bytes',
    'test_native_reclamation_excludes_only_its_exact_pinned_directory_fd',
    'test_native_gate_denies_external_network_and_unrelated_execution')}
PLUGIN='''import json,os
from pathlib import Path
rows=[]
def pytest_runtest_logreport(report):
 rows.append({'nodeid':report.nodeid,'phase':report.when,'outcome':report.outcome,'xfail':hasattr(report,'wasxfail')})
def pytest_sessionfinish(session,exitstatus):
 Path(os.environ['PEER_TEST_CASE_REPORT']).write_text(json.dumps({'collected':session.testscollected,'exitstatus':exitstatus,'rows':rows}))
'''


def fixture_process(command, *, timeout, **kwargs):
    """Own a fresh process group and leave no fixture descendants behind."""
    process=subprocess.Popen(command,start_new_session=True,**kwargs)
    try:
        try:
            stdout,stderr=process.communicate(timeout=timeout)
            return subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(command,124,'','fixture process timeout')
    finally:
        def stop(kind):
            try:os.killpg(process.pid,kind)
            except ProcessLookupError:pass
        stop(signal.SIGTERM)
        try:process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            stop(signal.SIGKILL);process.communicate(timeout=5)
        finally:stop(signal.SIGKILL)
        process.wait(timeout=5)


def validate(report,sha,*,allow_dirty=False):
    if (report.get('schema_version')!=1 or report.get('scope')!='reserved-disposable-loopback-and-native-inspection'
            or report.get('candidate_sha')!=sha or report.get('ending_sha')!=sha
            or report.get('status')!='PASS' or report.get('returncode')!=0
            or not allow_dirty and (not report.get('clean_start') or not report.get('clean_end') or report.get('dirty_allowed'))):
        raise ValueError('native_peer_gate_unproven')
    cases=report.get('cases',{})
    if cases.get('collected')!=len(EXPECTED) or cases.get('exitstatus')!=0:raise ValueError('native_peer_gate_incomplete')
    rows=cases.get('rows',[])
    required={(node,phase) for node in EXPECTED for phase in ('setup','call','teardown')}
    if (len(rows)!=len(required) or {(r.get('nodeid'),r.get('phase')) for r in rows}!=required
            or any(r.get('outcome')!='passed' or r.get('xfail') is not False for r in rows)):
        raise ValueError('native_peer_gate_incomplete')
    ports=report.get('ports',[])
    if len(ports)!=7 or len(set(ports))!=7 or any(type(p) is not int or not 1024<p<65536 or p==8000 for p in ports):
        raise ValueError('native_peer_port_scope')
    return report


def run_gate(*,expected_sha=None,allow_dirty=False):
    sha=pipeline.git('rev-parse','HEAD')
    if expected_sha is not None and sha!=expected_sha:raise ValueError('native_peer_head_changed')
    clean=not pipeline.git('status','--porcelain')
    if not clean and not allow_dirty:raise ValueError('native_peer_dirty_source')
    sys.path.insert(0,str(pipeline.ROOT))
    from scripts.run_simulation_qa import _child_environment
    listeners=[];ipv6_reservations=[];denied_listener=None
    started=time.monotonic()
    try:
        # Reserve before granting access. Keep each socket open throughout the
        # child run, so an unrelated listener cannot take an allowed port.
        for _ in range(7):
            listener=socket.socket();listener.bind(('127.0.0.1',0));listeners.append(listener)
            ipv6=socket.socket(socket.AF_INET6);ipv6.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
            ipv6_reservations.append(ipv6);ipv6.bind(('::1',listener.getsockname()[1]))
        ports=[s.getsockname()[1] for s in listeners]
        if 8000 in ports:raise ValueError('native_peer_port_scope')
        denied_listener=socket.socket();denied_listener.bind(('127.0.0.1',0));denied_listener.listen()
        with tempfile.TemporaryDirectory(prefix='wisp-native-peer-') as temporary:
            scratch=Path(temporary).resolve()
            env=_child_environment(scratch)
            canary=scratch/'private-home-canary';canary.write_bytes(b'synthetic private data')
            case_report=scratch/'cases.json'
            (scratch/'native_gate_plugin.py').write_text(PLUGIN)
            env.update(PYTHONPATH=str(scratch)+os.pathsep+str(pipeline.ROOT),
                       PEER_TEST_PEER_FDS=json.dumps([s.fileno() for s in listeners]),
                       PEER_TEST_CASE_REPORT=str(case_report),PEER_TEST_PRIVATE_CANARY=str(canary),
                       PEER_TEST_DENIED_PORT=str(denied_listener.getsockname()[1]))
            profile=pipeline.simulation_profile(scratch,Path(sys.executable))
            profile+='\n(allow process-exec (literal "/usr/sbin/lsof") (literal "/usr/sbin/netstat"))'
            for port in ports:
                profile+=f'\n(allow network-bind (local ip "localhost:{port}"))'
                profile+=f'\n(allow network-inbound (local ip "localhost:{port}"))'
                profile+=f'\n(allow network-outbound (remote ip "localhost:{port}"))'
            profile+='\n(deny file-read-data (literal '+json.dumps(str(canary))+'))'
            result=fixture_process(['/usr/bin/sandbox-exec','-p',profile,sys.executable,'-B','-m','pytest',
                '-p','pytest_asyncio.plugin','-p','native_gate_plugin','-q','-rs',MODULE],
                cwd=pipeline.ROOT,env=env,pass_fds=tuple(s.fileno() for s in listeners),
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=180)
            cases=json.loads(case_report.read_text()) if case_report.exists() else {}
            report={'schema_version':1,'scope':'reserved-disposable-loopback-and-native-inspection',
                'candidate_sha':sha,'ending_sha':pipeline.git('rev-parse','HEAD'),
                'clean_start':clean,'clean_end':not pipeline.git('status','--porcelain'),'dirty_allowed':allow_dirty,
                'returncode':result.returncode,'cases':cases,'ports':ports,'duration_s':time.monotonic()-started,
                'stdout':result.stdout,'stderr':result.stderr,'status':'PASS' if result.returncode==0 else 'FAIL'}
            if result.returncode==0:validate(report,sha,allow_dirty=allow_dirty)
            return report
    finally:
        for listener in [*listeners,*ipv6_reservations]:listener.close()
        if denied_listener is not None:denied_listener.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-sha',required=True);parser.add_argument('--report',required=True)
    parser.add_argument('--allow-dirty',action='store_true');args=parser.parse_args()
    report=run_gate(expected_sha=args.expected_sha,allow_dirty=args.allow_dirty)
    Path(args.report).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'scope':report['scope'],'cases':len(EXPECTED)}))
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
