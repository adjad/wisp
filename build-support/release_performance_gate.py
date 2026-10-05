"""Offline benchmark contracts in a separate, reserved-loopback sandbox.

The generic Simulation sandbox stays network-denied. This runs every reviewed
benchmark test, never the live measurement command, and imports complete evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import pipeline
from native_peer_gate import fixture_process

MODULE = "tests/test_release_performance.py"
EXPECTED_COUNT = 289
EXPECTED_CASES_SHA256 = "8aac3f73276f3e8be12692d9ce33436358901bac383fdb93729d2dbb79ed17b1"
EXPECTED = frozenset(json.loads((pipeline.ROOT / "test_fixtures/performance/offline_cases_v1.json").read_text()))
PLUGIN = '''import json,os
from pathlib import Path
rows=[]
def pytest_runtest_logreport(report):
 rows.append({'nodeid':report.nodeid,'phase':report.when,'outcome':report.outcome,'xfail':hasattr(report,'wasxfail')})
def pytest_sessionfinish(session,exitstatus):
 Path(os.environ['BENCHMARK_CASE_REPORT']).write_text(json.dumps({'collected':session.testscollected,'exitstatus':exitstatus,'rows':rows}))
'''


def case_digest(nodes):
    return hashlib.sha256(json.dumps(sorted(nodes), separators=(",", ":")).encode()).hexdigest()


def profile(scratch, python, port, canary):
    if type(port) is not int or not 1024 < port < 65536 or port == 8000:
        raise ValueError("benchmark_fixture_port_invalid")
    policy = pipeline.simulation_profile(scratch, python)
    policy += f'\n(allow network-bind network-inbound (local ip "localhost:{port}"))'
    policy += f'\n(allow network-outbound (remote ip "localhost:{port}"))'
    policy += '\n(deny file-read-data (literal ' + json.dumps(str(canary)) + '))'
    return policy


def validate(report, sha, *, allow_dirty=False):
    if (report.get("schema_version") != 1 or report.get("scope") != "offline-benchmark-reserved-loopback"
            or report.get("candidate_sha") != sha or report.get("ending_sha") != sha
            or report.get("status") != "PASS" or report.get("returncode") != 0
            or not allow_dirty and (report.get("clean_start") is not True or report.get("clean_end") is not True
                                    or report.get("dirty_allowed") is not False)):
        raise ValueError("benchmark_fixture_gate_unproven")
    cases = report.get("cases", {})
    rows = cases.get("rows", [])
    nodes = {r.get("nodeid") for r in rows}
    if (cases.get("collected") != EXPECTED_COUNT or cases.get("exitstatus") != 0
            or len(nodes) != EXPECTED_COUNT or not all(isinstance(n, str) for n in nodes)
            or nodes != EXPECTED or case_digest(nodes) != EXPECTED_CASES_SHA256 or len(rows) != 3 * EXPECTED_COUNT
            or {(r.get("nodeid"), r.get("phase")) for r in rows}
               != {(node, phase) for node in nodes for phase in ("setup", "call", "teardown")}
            or any(r.get("outcome") != "passed" or r.get("xfail") is not False for r in rows)):
        raise ValueError("benchmark_fixture_gate_incomplete")
    probes = report.get("probes", {})
    if set(probes) != {"denied_loopback", "denied_process", "denied_private_canary"} or any(v is not True for v in probes.values()):
        raise ValueError("benchmark_fixture_boundary_unproven")
    port = report.get("port")
    if type(port) is not int or not 1024 < port < 65536 or port == 8000:
        raise ValueError("benchmark_fixture_port_invalid")
    return report


def load_pinned(path, digest, sha, *, allow_dirty=False):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("benchmark_fixture_evidence_changed")
    return validate(json.loads(raw), sha, allow_dirty=allow_dirty)


def run_gate(expected_sha, *, allow_dirty=False):
    sha = pipeline.git("rev-parse", "HEAD")
    clean = not pipeline.git("status", "--porcelain")
    if sha != expected_sha or not clean and not allow_dirty:
        raise ValueError("benchmark_fixture_source_not_exact")
    started = time.monotonic()
    sys.path.insert(0, str(pipeline.ROOT))
    from scripts.run_simulation_qa import _child_environment
    # Held throughout all child tests; socket duplicates preserve the parent's reservation.
    with socket.socket() as listener, socket.socket() as denied:
        listener.bind(("127.0.0.1", 0))
        denied.bind(("127.0.0.1", 0))
        denied.listen()
        port = listener.getsockname()[1]
        with tempfile.TemporaryDirectory(prefix="wisp-benchmark-contracts-") as temporary:
            scratch = Path(temporary).resolve()
            env = _child_environment(scratch)
            canary = scratch / "private-canary"
            canary.write_bytes(b"synthetic private state")
            policy = profile(scratch, Path(sys.executable), port, canary)
            probes = {}
            commands = {
                "denied_loopback": f"import socket; socket.create_connection(('127.0.0.1',{denied.getsockname()[1]}),timeout=1)",
                "denied_process": "import subprocess; subprocess.run(['/usr/bin/true'],check=True)",
                "denied_private_canary": f"from pathlib import Path; Path({str(canary)!r}).read_bytes()",
            }
            for name, command in commands.items():
                # A PermissionError is required: an unavailable interpreter or failed launcher cannot pass.
                probe = "try:\n " + command.replace("; ", "\n ") + "\nexcept PermissionError:\n print('BOUNDARY_DENIED')\nelse:\n raise SystemExit(1)"
                result = subprocess.run(["/usr/bin/sandbox-exec", "-p", policy, sys.executable, "-B", "-c", probe],
                                        cwd=pipeline.ROOT, env=env, capture_output=True, text=True, timeout=10)
                probes[name] = result.returncode == 0 and result.stdout.strip() == "BOUNDARY_DENIED"
            case_report = scratch / "cases.json"
            (scratch / "benchmark_gate_plugin.py").write_text(PLUGIN)
            env.update(PYTHONPATH=str(scratch) + os.pathsep + str(pipeline.ROOT),
                       BENCHMARK_FIXTURE_FD=str(listener.fileno()), BENCHMARK_CASE_REPORT=str(case_report))
            result = fixture_process(["/usr/bin/sandbox-exec", "-p", policy, sys.executable, "-B", "-m", "pytest",
                                      "-p", "benchmark_gate_plugin", "-q", "-rs", MODULE],
                                     cwd=pipeline.ROOT, env=env, pass_fds=(listener.fileno(),),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=240)
            report = dict(schema_version=1, scope="offline-benchmark-reserved-loopback", candidate_sha=sha,
                          ending_sha=pipeline.git("rev-parse", "HEAD"), clean_start=clean,
                          clean_end=not pipeline.git("status", "--porcelain"), dirty_allowed=allow_dirty,
                          returncode=result.returncode, cases=json.loads(case_report.read_text()) if case_report.exists() else {},
                          port=port, probes=probes, duration_s=time.monotonic() - started,
                          stdout=result.stdout, stderr=result.stderr,
                          status="PASS" if result.returncode == 0 and all(probes.values()) else "FAIL")
            try:
                validate(report, sha, allow_dirty=allow_dirty)
            except ValueError as exc:
                report.update(status="FAIL", validation_error=str(exc))
            return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    report = run_gate(args.expected_sha, allow_dirty=args.allow_dirty)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "scope": report["scope"], "cases": EXPECTED_COUNT}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
