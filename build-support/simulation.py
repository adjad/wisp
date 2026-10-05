"""Add explicit build checks to the authoritative Simulation QA gates.

The product runner's reviewed manifest remains unchanged and directly usable.
The pipeline schedules build contracts under the original no-network sandbox.
Actual native peer fixtures run first in a separate reserved-port sandbox; their
hash-pinned complete results remain mandatory in the same combined QA report.
"""
from pathlib import Path
import os
import sys
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import run_simulation_qa as qa

BUILD_TESTS = {
    "tests/build_pipeline/pipeline_checks.py",
    "tests/build_pipeline/managed_live_qa_checks.py",
}
canonical_selection = qa._selected_tests


def selected_tests(profiles):
    selected = canonical_selection(profiles)
    if "full" in profiles:
        for path in BUILD_TESTS:
            if not (ROOT / path).is_file():
                raise RuntimeError("Missing build contract entry point: " + path)
        selected = sorted([*selected, *BUILD_TESTS])
    return selected


def main():
    qa._selected_tests = selected_tests
    if '--list' not in sys.argv:
        # Import actual separately executed results, never synthesize a pass or
        # skip a fixture. The parent pins complete private evidence by hash.
        import native_peer_gate
        path=Path(os.environ['PEER_TEST_GATE_REPORT'])
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=os.environ['PEER_TEST_GATE_SHA256']:
            raise RuntimeError('Native gate evidence changed')
        native_report=native_peer_gate.validate(json.loads(raw),qa._git('rev-parse','HEAD'),
                                               allow_dirty='--allow-dirty' in sys.argv)
        import browser_bridge_gate
        browser_report = browser_bridge_gate.load_pinned(os.environ['BROWSER_BRIDGE_GATE_REPORT'],
            os.environ['BROWSER_BRIDGE_GATE_SHA256'], qa._git('rev-parse', 'HEAD'),
            allow_dirty='--allow-dirty' in sys.argv)
        import release_performance_gate
        benchmark_report = release_performance_gate.load_pinned(os.environ["BENCHMARK_GATE_REPORT"],
            os.environ["BENCHMARK_GATE_SHA256"], qa._git("rev-parse", "HEAD"), allow_dirty="--allow-dirty" in sys.argv)
        original_run=qa._run
        def run(name,command,*args,**kwargs):
            if name == release_performance_gate.MODULE:
                print("[PASS] " + name + " (" + str(release_performance_gate.EXPECTED_COUNT) + " separately sandboxed offline benchmark cases)", flush=True)
                return qa.GateResult(name, [str(ROOT / "build-support/release_performance_gate.py")], 0,
                    benchmark_report["duration_s"], release_performance_gate.EXPECTED_COUNT, 0, 0,
                    json.dumps({"benchmark_gate": benchmark_report}, sort_keys=True), "")
            if name == browser_bridge_gate.MODULE:
                print('[PASS] ' + name + ' (' + str(len(browser_bridge_gate.EXPECTED)) + ' separately sandboxed signed Unix cases)', flush=True)
                return qa.GateResult(name, [str(ROOT / 'build-support/browser_bridge_gate.py')], 0,
                    browser_report['duration_s'], len(browser_bridge_gate.EXPECTED), 0, 0,
                    json.dumps({'browser_bridge_gate': browser_report}, sort_keys=True), '')
            if name!=native_peer_gate.MODULE:return original_run(name,command,*args,**kwargs)
            print('[PASS] '+name+' (9 separately sandboxed native cases)',flush=True)
            return qa.GateResult(name,[str(ROOT/'build-support/native_peer_gate.py')],0,
                native_report['duration_s'],len(native_peer_gate.EXPECTED),0,0,
                json.dumps({'native_gate':native_report},sort_keys=True),'')
        qa._run=run
    # These entry points use unittest.main(), so require reported nonzero counts
    # and direct script execution, just like the canonical standalone checks.
    qa.LEGACY_SCRIPT_TESTS = qa.LEGACY_SCRIPT_TESTS | BUILD_TESTS
    native = qa._native_gates

    def gates(build):
        result = native(build)
        for name, sources, contract in (
            ("assistant-delivery", ["WispClient.swift", "AssistantDelivery.swift"], "AssistantDeliveryChecks.swift"),
            ("node-presentation", ["NodePresentation.swift"], "NodePresentationChecks.swift"),
            ("backend-credentials", ["BackendCredentials.swift"], "BackendCredentialChecks.swift"),
            ("backend-recovery", ["BackendCredentials.swift", "BackendManager.swift", "PortGuard.swift", "BackendOwnership.swift"], "BackendRecoveryChecks.swift"),
            ("prompt-queue", ["PromptQueue.swift"], "PromptQueueChecks.swift"),
        ):
            binary = str(build / (os.environ["WISP_BUILD_FIXTURE_PREFIX"] + "-" + name))
            result.extend([
                (f"native/{name}-compile", [qa.TRUSTED_SWIFTC, "-parse-as-library",
                    "-swift-version", "5", "-module-cache-path", str(build / "module-cache"),
                    *["app/Sources/WispApp/" + s for s in sources], "tests/" + contract, "-o", binary]),
                (f"native/{name}-contract", [binary]),
            ])
            qa._NATIVE_GATE_DEPENDENCIES[f"native/{name}-contract"] = f"native/{name}-compile"
        for _, command in result:
            if command[0] == qa.TRUSTED_SWIFTC:
                command.append(str(ROOT / "build-support/FixtureTemporaryDirectory.swift"))
        return result

    qa._native_gates = gates
    return qa.main()


if __name__ == "__main__":
    raise SystemExit(main())
