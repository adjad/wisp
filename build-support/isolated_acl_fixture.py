"""Compile or qualify synthetic ACL replacement; never use a shared user's Keychain.

Default compilation calls no Keychain APIs. Execution additionally requires an
explicit disposable-macOS assertion; it is excluded from ordinary simulation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def call(argv, *, data=None, expected=True):
    result = subprocess.run([str(x) for x in argv], input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=120,
                            env={"PATH": "/usr/bin:/bin", "TMPDIR": tempfile.gettempdir(),
                                 **({"DEVELOPER_DIR": os.environ["DEVELOPER_DIR"]} if "DEVELOPER_DIR" in os.environ else {})})
    if expected and result.returncode:
        raise RuntimeError("isolated_fixture_operation_failed")
    return result


def validate_outcome(result, denial=None):
    if denial is not None:
        if result.returncode != 1 or result.stderr not in denial or result.stdout:
            raise RuntimeError("unexpected_acl_result")
    elif result.returncode or result.stderr or result.stdout not in (
            b"fixture-original-pass\n", b"fixture-replacement-pass\n", b"fixture-unrelated-pass\n"):
        raise RuntimeError("unexpected_fixture_diagnostic")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ephemeral-macos", action="store_true",
                        help="assert this is a disposable macOS VM/runner without user data or imported signing identities")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report = {"status": "UNAVAILABLE_NOT_EXECUTED", "keychain_executed": False,
              "qualification_scope": "ad-hoc signatures and private synthetic Keychain only", "cases": {}}
    try:
        if platform.system() != "Darwin":
            raise RuntimeError("macos_required")
        report.update(source_sha=call(["/usr/bin/git", "-C", ROOT, "rev-parse", "HEAD"]).stdout.decode().strip(),
                      os_build=call(["/usr/bin/sw_vers", "-buildVersion"]).stdout.decode().strip(),
                      os_version=platform.mac_ver()[0], architecture=platform.machine(),
                      compiler=call(["/usr/bin/swiftc", "--version"]).stdout.decode().strip())
        report["source_clean"] = not call(["/usr/bin/git", "-C", ROOT, "status", "--porcelain", "--untracked-files=all"]).stdout.strip()
        config = json.loads((ROOT / "build-support/toolchain.json").read_text())
        version = tuple(int(x) for x in platform.mac_ver()[0].split("."))
        minimum = tuple(int(x) for x in config["minimum_macos"].split("."))
        if args.ephemeral_macos and (version < minimum or platform.machine() != config["architecture"]):
            raise RuntimeError("unsupported_qualification_target")
        if args.ephemeral_macos and not report["source_clean"]:
            raise RuntimeError("clean_candidate_required")
        with tempfile.TemporaryDirectory(prefix="wisp-acl-fixture-") as temp:
            root = Path(temp).resolve()
            root.chmod(0o700)
            signatures = {}
            for name, flag in (("controller", None), ("reader-original", None),
                               ("reader-replacement", "FIXTURE_REPLACEMENT"), ("reader-unrelated", "FIXTURE_UNRELATED")):
                binary = root / name
                argv = ["/usr/bin/swiftc", "-parse-as-library", "-module-cache-path", root / "cache"]
                if flag:
                    argv += ["-D", flag]
                call([*argv, ROOT / "app/Sources/WispApp/BackendCredentials.swift",
                      ROOT / "infra/mac-mini/IsolatedACLFixture.swift", "-o", binary])
                call(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", "com.wisp.synthetic." + name, binary])
                call(["/usr/bin/codesign", "--verify", "--strict", binary])
                details = call(["/usr/bin/codesign", "-d", "--verbose=4", binary]).stderr.decode()
                identity = {key: re.search(r"^" + key + r"=(.+)$", details, re.M).group(1)
                            for key in ("Identifier", "CDHash")}
                identity["sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
                signatures[name] = identity
            report["signed_binaries"] = signatures
            report["compile"] = "PASS"
            if args.ephemeral_macos:
                report["keychain_executed"] = True
                active = root / "active-reader"
                shutil.copyfile(root / "reader-original", active)
                active.chmod(0o700)
                def operation(binary, command, *, denial=None, data=None):
                    result = call([binary, "--isolated-temporary-keychain", root, command], data=data, expected=False)
                    validate_outcome(result, denial)
                operation(root / "controller", "create", data=json.dumps({"password": secrets.token_hex(32), "value": secrets.token_hex(32)}).encode())
                operation(active, "read")
                report["cases"]["original_reader"] = "PASS"
                operation(root / "reader-unrelated", "read", denial={b"EXPECTED_OS_DENIAL\n"})
                report["cases"]["unrelated_reader_denied"] = "PASS"
                def replace_reader(name):
                    replacement = root / "replacement-stage"
                    shutil.copyfile(root / name, replacement)
                    replacement.chmod(0o700)
                    os.replace(replacement, active)
                replace_reader("reader-replacement")
                operation(active, "read", denial={b"EXPECTED_POLICY_DENIAL\n", b"EXPECTED_OS_DENIAL\n"})
                report["cases"]["replacement_denied"] = "PASS"
                replace_reader("reader-original")
                operation(active, "read")
                report["cases"]["original_restored"] = "PASS"
                replace_reader("reader-replacement")
                operation(root / "controller", "rebind")
                operation(active, "read")
                report["cases"]["explicit_synthetic_rebind"] = "PASS"
                replace_reader("reader-original")
                operation(active, "read", denial={b"EXPECTED_POLICY_DENIAL\n", b"EXPECTED_OS_DENIAL\n"})
                report["cases"]["old_identity_denied_after_rebind"] = "PASS"
                replace_reader("reader-replacement")
                operation(root / "controller", "lock")
                operation(active, "read", denial={b"EXPECTED_OS_DENIAL\n"})
                report["cases"]["locked_temporary_store_denied"] = "PASS"
                report["status"] = "PASS"
        report["ending_sha"] = call(["/usr/bin/git", "-C", ROOT, "rev-parse", "HEAD"]).stdout.decode().strip()
        report["ending_clean"] = not call(["/usr/bin/git", "-C", ROOT, "status", "--porcelain", "--untracked-files=all"]).stdout.strip()
        if args.ephemeral_macos and (report["ending_sha"] != report["source_sha"] or not report["ending_clean"]):
            raise RuntimeError("candidate_changed_during_qualification")
    except Exception:
        report["status"] = "BLOCK"
        report["error"] = "isolated_fixture_unavailable_or_failed"
    (output / "qualification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "keychain_executed": report["keychain_executed"]}))
    return int(report["status"] == "BLOCK")


if __name__ == "__main__":
    raise SystemExit(main())
