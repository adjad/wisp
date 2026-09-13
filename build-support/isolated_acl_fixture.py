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


REQUIRED_CASES = {"original_reader", "unrelated_reader_denied", "replacement_denied", "original_restored",
                  "explicit_synthetic_rebind", "old_identity_denied_after_rebind", "locked_temporary_store_denied"}


def assert_qualified(report):
    if (report.get("status") != "PASS" or report.get("keychain_executed") is not True
            or report.get("cases") != {case: "PASS" for case in REQUIRED_CASES}
            or report.get("ambient_unchanged") is not True or report.get("temporary_keychain_deleted") is not True
            or report.get("temporary_files_removed") is not True or report.get("source_clean") is not True
            or report.get("ending_clean") is not True or report.get("source_sha") != report.get("ending_sha")):
        raise RuntimeError("incomplete_qualification")
    states = report.get("ambient_states", {})
    if set(states) != {"before", "after_create", "after_cases", "after_cleanup"} or not all(value == states["before"] for value in states.values()):
        raise RuntimeError("ambient_state_changed")


def ambient_state(root):
    result = call([root / "controller", "--isolated-temporary-keychain", root, "state"])
    if result.stderr or len(result.stdout) > 1024 * 1024:
        raise RuntimeError("metadata_unavailable")
    state = json.loads(result.stdout)
    if not isinstance(state, dict) or set(state) != {"schema_version", "default", "search_list"} or state["schema_version"] != 1:
        raise RuntimeError("metadata_unavailable")
    if not isinstance(state["search_list"], list):
        raise RuntimeError("metadata_unavailable")
    for entry in state["search_list"] + ([state["default"]] if state["default"] is not None else []):
        if (not isinstance(entry, dict) or set(entry) != {"path", "status_bits"}
                or not isinstance(entry["path"], str) or not Path(entry["path"]).is_absolute()
                or type(entry["status_bits"]) is not int):
            raise RuntimeError("metadata_unavailable")
    # lstat only: never read login-Keychain contents or credential item values.
    folder = Path.home() / "Library/Keychains"
    names = {"login.keychain", "login.keychain-db"}
    if folder.exists():
        names.update(path.name for path in folder.iterdir() if path.name.startswith("login.keychain"))
    files = {}
    for name in sorted(names):
        try:
            info = (folder / name).lstat()
            files[name] = {key: getattr(info, key) for key in
                ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")}
        except FileNotFoundError:
            files[name] = None
    state["login_file_metadata"] = files
    return state


def run_qualification(root, report):
    report["keychain_executed"] = True
    active = root / "active-reader"
    shutil.copyfile(root / "reader-original", active)
    active.chmod(0o700)
    password = secrets.token_hex(32)
    states = report["ambient_states"] = {}
    states["before"] = ambient_state(root)
    def operation(binary, command, *, denial=None, data=None):
        report["last_operation"] = binary.name + ":" + command
        trace = {"binary": binary.name, "command": command, "outcome": "INCOMPLETE"}
        report.setdefault("operations", []).append(trace)
        result = call([binary, "--isolated-temporary-keychain", root, command], data=data, expected=False)
        trace["outcome"] = "SUCCESS" if result.returncode == 0 else "UNKNOWN_FAILURE"
        if result.returncode:
            report["last_outcome"] = result.stderr.decode("ascii", errors="replace").strip() if result.stderr in (
                b"EXPECTED_POLICY_DENIAL\n", b"EXPECTED_OS_DENIAL\n", b"ISOLATION_FAILURE\n", b"UNAVAILABLE\n", b"STORE_PATH_UNAVAILABLE\n",
                b"STORE_PATH_MISMATCH\n", b"STORE_FILE_MISMATCH\n") else "UNKNOWN_FAILURE"
            trace["outcome"] = report["last_outcome"]
        validate_outcome(result, denial)
    def replace_reader(name):
        replacement = root / "replacement-stage"
        shutil.copyfile(root / name, replacement)
        replacement.chmod(0o700)
        os.replace(replacement, active)
    try:
        operation(root / "controller", "create", data=json.dumps({"password": password, "value": secrets.token_hex(32)}).encode())
        # Detect automatic list/default changes immediately, not only after delete.
        states["after_create"] = ambient_state(root)
        if states["after_create"] != states["before"]:
            raise RuntimeError("ambient_state_changed_during_create")
        operation(active, "read")
        report["cases"]["original_reader"] = "PASS"
        operation(root / "reader-unrelated", "read", denial={b"EXPECTED_OS_DENIAL\n"})
        report["cases"]["unrelated_reader_denied"] = "PASS"
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
    finally:
        try:
            states["after_cases"] = ambient_state(root)
        finally:
            try:
                if (root / "synthetic.keychain-db").exists():
                    operation(root / "controller", "cleanup", data=json.dumps({"password": password}).encode())
                report["temporary_keychain_deleted"] = not list(root.glob("synthetic.keychain*"))
                if not report["temporary_keychain_deleted"]:
                    raise RuntimeError("temporary_keychain_cleanup_failed")
            finally:
                states["after_cleanup"] = ambient_state(root)
                report["ambient_unchanged"] = len(states) == 4 and all(value == states["before"] for value in states.values())
    if not report["ambient_unchanged"]:
        raise RuntimeError("ambient_state_changed")
    report["status"] = "PASS"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ephemeral-macos", action="store_true",
                        help="assert this is a disposable macOS VM/runner without user data or imported signing identities")
    parser.add_argument("--expected-sha", help="required reviewed exact head for hosted execution")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report = {"status": "UNAVAILABLE_NOT_EXECUTED", "keychain_executed": False,
              "qualification_scope": "ad-hoc signatures and private synthetic Keychain only", "cases": {}}
    root = None
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
        if args.ephemeral_macos and (os.environ.get("GITHUB_ACTIONS") != "true"
                or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
                or os.environ.get("RUNNER_OS") != "macOS"):
            raise RuntimeError("disposable_github_runner_required")
        if args.ephemeral_macos and (args.expected_sha != report["source_sha"]
                or not re.fullmatch(r"[0-9a-f]{40}", args.expected_sha or "")):
            raise RuntimeError("exact_head_required")
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
                run_qualification(root, report)
        report["temporary_files_removed"] = not root.exists()
        report["ending_sha"] = call(["/usr/bin/git", "-C", ROOT, "rev-parse", "HEAD"]).stdout.decode().strip()
        report["ending_clean"] = not call(["/usr/bin/git", "-C", ROOT, "status", "--porcelain", "--untracked-files=all"]).stdout.strip()
        if args.ephemeral_macos and (report["ending_sha"] != report["source_sha"] or not report["ending_clean"]):
            raise RuntimeError("candidate_changed_during_qualification")
        if args.ephemeral_macos:
            assert_qualified(report)
    except Exception:
        report["status"] = "BLOCK"
        report["error"] = "isolated_fixture_unavailable_or_failed"
    if root is not None:
        report["temporary_files_removed"] = not root.exists()
    (output / "qualification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "keychain_executed": report["keychain_executed"]}))
    return int(report["status"] == "BLOCK")


if __name__ == "__main__":
    raise SystemExit(main())
