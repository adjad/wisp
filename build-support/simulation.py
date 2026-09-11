"""Add explicit build checks to the authoritative Simulation QA gates.

The product runner's reviewed manifest remains unchanged and directly usable.
The pipeline separately schedules its unittest contract entry point and two
additional native fixture programs under the same QA report and sandbox.
"""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import run_simulation_qa as qa

BUILD_TESTS = {"tests/build_pipeline/pipeline_checks.py"}
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
    # These entry points use unittest.main(), so require reported nonzero counts
    # and direct script execution, just like the canonical standalone checks.
    qa.LEGACY_SCRIPT_TESTS = qa.LEGACY_SCRIPT_TESTS | BUILD_TESTS
    native = qa._native_gates

    def gates(build):
        result = native(build)
        for name, sources, contract in (
            ("assistant-delivery", ["WispClient.swift", "AssistantDelivery.swift"], "AssistantDeliveryChecks.swift"),
            ("backend-recovery", ["BackendManager.swift"], "BackendRecoveryChecks.swift"),
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
