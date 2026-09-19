"""Compile or qualify synthetic ACL replacement; never use a shared user's Keychain.

Default compilation calls no Keychain APIs. Execution additionally requires an
explicit disposable-macOS assertion; it is excluded from ordinary simulation.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HELPER_INPUTS = ("app/Sources/WispApp/BackendCredentials.swift",
                 "infra/mac-mini/keychain-helper.swift", "build-support/toolchain.json")


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


def historical_helper_source(source_sha):
    """Select the first direct parent that can prove a complete helper receipt."""
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise RuntimeError("historical_source_unavailable")
    try:
        lineage = call(["/usr/bin/git", "-C", str(ROOT), "rev-list", "--parents", "-n", "1", source_sha])
        commits = lineage.stdout.decode("ascii").strip().split()
    except UnicodeError:
        raise RuntimeError("historical_source_unavailable") from None
    if (len(commits) < 2 or commits[0] != source_sha
            or any(not re.fullmatch(r"[0-9a-f]{40}", commit) for commit in commits)):
        raise RuntimeError("historical_source_unavailable")
    expected = set(HELPER_INPUTS)
    for parent in commits[1:]:
        try:
            tree = call(["/usr/bin/git", "-C", str(ROOT), "ls-tree", "--name-only", parent,
                         "--", *HELPER_INPUTS]).stdout.decode("utf-8").splitlines()
        except UnicodeError:
            raise RuntimeError("historical_source_unavailable") from None
        if len(tree) == len(expected) and set(tree) == expected:
            return parent
    raise RuntimeError("historical_source_unavailable")


REQUIRED_CASES = {"original_reader", "unrelated_reader_denied", "replacement_denied",
                  "exact_helper_receipt_restored", "original_reader_after_restoration",
                  "recovery_readiness_blocked", "locked_temporary_store_denied",
                  "unauthorized_historical_recovery_denied", "reviewed_historical_recovery",
                  "fresh_generation_usable", "replacement_still_denied"}

def assert_qualified(report):
    if report.get('pipe_qualification') != {'status':'PASS','exec_environment':'absent','procargs_before':'absent',
            'procargs_after':'absent','native_frame':'verified','descriptor':'closed','descendant_inheritance':'absent'}:
        raise RuntimeError('incomplete_native_pipe_qualification')
    if (report.get("status") != "PASS" or report.get("keychain_executed") is not True
            or report.get("cases") != {case: "PASS" for case in REQUIRED_CASES}
            or report.get("ambient_unchanged") is not True or report.get("temporary_keychain_deleted") is not True
            or report.get("temporary_files_removed") is not True or report.get("source_clean") is not True
            or report.get("ending_clean") is not True or report.get("source_sha") != report.get("ending_sha")):
        raise RuntimeError("incomplete_qualification")
    snapshots = report.get("helper_snapshots", {})
    if (report.get("schema_version") != 2 or report.get("automatic_acl_migration") != "unsupported"
            or set(snapshots) != {"before", "restored"} or snapshots["before"] != snapshots["restored"]
            or report.get("recovery_phase") != "helper_restored_keychain_unverified"
            or report.get("recovery_commands") != {command: "BLOCKED" for command in ("status", "export-mini", "init")}):
        raise RuntimeError("incomplete_recovery_qualification")
    if (not isinstance(report.get('historical_source'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', report['historical_source'])
            or report.get('recovery_result') != {'schema_version': 1, 'status': 'complete', 'credentials': 'ready',
                'generation': 'renewed', 'backend_refresh_required': True, 'helper_source': report['historical_source'],
                'acl_recovery': 'reviewed_prior_restored', 'credential_values': 'retained', 'replacement_accepted': False}
            or report.get("fresh_generation") is not True or report.get("quarantine_cleared") is not True
            or report.get("historical_source") == report.get("source_sha")):
        raise RuntimeError("incomplete_historical_recovery_qualification")
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


def fixture_transaction(root, source_sha, historical_sha=None):
    """Use the shipped transaction with a signed synthetic-binary build adapter.

    Only the compiled binary is substituted after checking immutable inputs. Real source receipt validation,
    directory swaps, durable journal/recovery and readiness gates still execute.
    The adapter never invokes a production credential helper.
    """
    folder = ROOT / "infra/mac-mini"
    sys.path.insert(0, str(folder))
    try:
        spec = importlib.util.spec_from_file_location("wisp_acl_transaction", folder / "node_prep.py")
        prep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prep)
    finally:
        sys.path.pop(0)
    if tuple(prep.HELPER_INPUTS) != HELPER_INPUTS:
        raise RuntimeError("fixture_helper_inputs_changed")
    class FixturePath(type(Path())):
        @classmethod
        def home(cls):
            return cls(root)
    prep.Path = FixturePath
    def run(argv, **kwargs):
        if kwargs:
            raise RuntimeError("unexpected_fixture_helper_input")
        if argv[:3] == ["/usr/bin/git", "-C", str(ROOT)]:
            operation = argv[3:]
            if (operation not in (["rev-parse", "HEAD"], ["status", "--porcelain", "--untracked-files=all"])
                    and operation not in [["show", sha + ":" + name] for sha in (source_sha, historical_sha or source_sha)
                                          for name in prep.HELPER_INPUTS]):
                raise RuntimeError("unexpected_fixture_git_operation")
            return call(argv).stdout
        if argv == ["/usr/bin/swiftc", "--version"]:
            return call(argv).stdout
        if argv[0] == "/usr/bin/swiftc" and "-parse-as-library" in argv:
            destination = Path(argv[-1])
            if argv[-2] != "-o" or destination.name != "wisp-keychain-helper" or destination.parent.parent != root / ".moe":
                raise RuntimeError("unexpected_fixture_build_target")
            paths = argv[argv.index("-module-cache-path") + 2:-2]
            inputs = prep.helper_inputs(source_sha)
            if len(paths) != 2 or any(Path(path).read_bytes() != inputs[name]
                                      for path, name in zip(paths, prep.HELPER_INPUTS[:2])):
                raise RuntimeError("fixture_compile_input_mismatch")
            shutil.copyfile(root / "reader-replacement", destination)
            return b""
        if argv[0] == "/usr/bin/codesign" or (len(argv) == 2 and argv[-1] == "protocol-version"):
            target = Path(argv[-1] if argv[0] == "/usr/bin/codesign" else argv[0])
            if target.name != "wisp-keychain-helper" or target.parent.parent != root / ".moe":
                raise RuntimeError("unexpected_fixture_helper_target")
            return call(argv).stdout
        raise RuntimeError("production_credential_execution_forbidden")
    prep.run = run
    return prep


def run_qualification(root, report):
    report["keychain_executed"] = True
    historical = historical_helper_source(report["source_sha"])
    report['historical_source'] = historical
    prep = fixture_transaction(root, report["source_sha"], historical)
    fixture_run = prep.run
    directory = root / ".moe/provisioning"
    directory.parent.mkdir(mode=0o700)
    directory.mkdir(mode=0o700)
    active = directory / "wisp-keychain-helper"
    shutil.copyfile(root / "reader-original", active)
    active.chmod(0o700)
    receipt = directory / "helper.json"
    receipt.write_text(json.dumps({"schema_version": 3, "source_commit": historical,
        "sources": prep.helper_sources(prep.helper_inputs(historical)),
        "sha256": hashlib.sha256(active.read_bytes()).hexdigest(), "fixture": "original"}, sort_keys=True))
    receipt.chmod(0o600)
    prior = prep.helper_snapshot(directory)
    report["helper_snapshots"] = {"before": prior}
    report["transaction_scope"] = "production immutable Git inputs, prepare_helper and keychain gate; signed synthetic binary substitution and private home"
    password = secrets.token_hex(32)
    local_token = secrets.token_hex(32)
    states = report["ambient_states"] = {}
    states["before"] = ambient_state(root)
    def operation(binary, command, *, denial=None, data=None):
        report["last_operation"] = binary.name + ":" + command
        trace = {"binary": binary.name, "command": command, "outcome": "INCOMPLETE"}
        report.setdefault("operations", []).append(trace)
        try:
            result = call([binary, "--isolated-temporary-keychain", root, command], data=data, expected=False)
        except subprocess.TimeoutExpired:
            trace["outcome"] = report["last_outcome"] = "TIMEOUT"
            raise
        trace["outcome"] = "SUCCESS" if result.returncode == 0 else "UNKNOWN_FAILURE"
        if result.returncode:
            known = {b"EXPECTED_POLICY_DENIAL\n", b"EXPECTED_OS_DENIAL\n", b"ISOLATION_FAILURE\n", b"UNAVAILABLE\n",
                     b"STORE_PATH_UNAVAILABLE\n", b"STORE_PATH_MISMATCH\n", b"STORE_FILE_MISMATCH\n"}
            report["last_outcome"] = result.stderr.decode().strip() if result.stderr in known or re.fullmatch(rb"UNAVAILABLE_OS_STATUS_-?[0-9]{1,10}\n", result.stderr) else "UNKNOWN_FAILURE"
            trace["outcome"] = report["last_outcome"]
        validate_outcome(result, denial)
    try:
        operation(root / "controller", "create", data=json.dumps({"password": password, "value": local_token}).encode())
        states["after_create"] = ambient_state(root)
        if states["after_create"] != states["before"]:
            raise RuntimeError("ambient_state_changed_during_create")
        operation(active, "read")
        report["cases"]["original_reader"] = "PASS"
        operation(root / "reader-unrelated", "read", denial={b"EXPECTED_OS_DENIAL\n"})
        report["cases"]["unrelated_reader_denied"] = "PASS"
        def reject_replacement(binary):
            operation(binary, "read", denial={b"EXPECTED_POLICY_DENIAL\n", b"EXPECTED_OS_DENIAL\n"})
            report["cases"]["replacement_denied"] = "PASS"
            raise prep.Refused("synthetic_replacement_denied")
        try:
            prep.prepare_helper(directory, expected_source=report["source_sha"], accept=reject_replacement)
        except prep.Refused as failure:
            if str(failure) != "helper_recovery_required" or report["cases"].get("replacement_denied") != "PASS":
                raise RuntimeError("unexpected_replacement_transaction") from None
        else:
            raise RuntimeError("replacement_was_accepted")
        restored = prep.helper_snapshot(directory)
        report["helper_snapshots"]["restored"] = restored
        if restored != prior:
            raise RuntimeError("old_helper_receipt_not_restored")
        report["cases"]["exact_helper_receipt_restored"] = "PASS"
        operation(active, "read")
        report["cases"]["original_reader_after_restoration"] = "PASS"
        marker = prep.read_json(directory.parent / ".helper-transaction.json")
        if marker.get("phase") != "helper_restored_keychain_unverified":
            raise RuntimeError("recovery_marker_missing")
        report["recovery_phase"] = marker["phase"]
        blocked = {}
        def no_execution(*args, **kwargs):
            raise RuntimeError("recovery_gate_executed_helper")
        prep.run = no_execution
        for command in ("status", "export-mini", "init"):
            try:
                prep.keychain(command)
            except prep.Refused as failure:
                if str(failure) != "helper_recovery_required":
                    raise RuntimeError("unexpected_recovery_result") from None
                blocked[command] = "BLOCKED"
            else:
                raise RuntimeError("recovery_gate_opened")
        report["recovery_commands"] = blocked
        report["cases"]["recovery_readiness_blocked"] = "PASS"
        # Exercise production recovery from the real replacement-denied state.
        # Native reads remain scoped to the synthetic temporary Keychain. The
        # adapter maps status/agreement to real successful reads, never ambient
        # production credential commands or a fabricated ACL acceptance.
        sys.path.insert(0, str(ROOT / 'infra/mac-mini'))
        try:
            import credential_recovery
        finally:
            sys.path.pop(0)
        prep.run = fixture_run
        try:
            credential_recovery.recover(prep, directory, report['source_sha'], 'restore-reviewed-prior', True)
        except prep.Refused as failure:
            if str(failure) != 'independent_recovery_authorization_required': raise
        else:
            raise RuntimeError('historical_recovery_unapproved')
        report['cases']['unauthorized_historical_recovery_denied'] = 'PASS'
        settings = root / '.omlx/settings.json'
        settings.parent.mkdir(mode=0o700)
        settings.write_text(json.dumps({'auth': {'api_key': local_token}}))
        settings.chmod(0o600)
        journal = directory.parent / '.helper-transaction.json'
        approval = dict(schema_version=1, action='restore-reviewed-prior', recovery_source=report['source_sha'],
            helper_source=historical, journal_sha256=hashlib.sha256(journal.read_bytes()).hexdigest(),
            prior=marker['prior'], candidate=marker['candidate'], uid=os.getuid())
        authorization = root / 'reviewed-recovery.json'
        authorization.write_text(json.dumps(approval, sort_keys=True))
        authorization.chmod(0o600)
        def recovery_run(argv, **kwargs):
            if argv == [str(active), 'status'] and not kwargs:
                operation(active, 'read')
                return b'{"credentials":"ready"}'
            if argv == [str(active), 'local-check'] and set(kwargs) == {'data'}:
                if json.loads(kwargs['data']) != {'expected': local_token}:
                    raise RuntimeError('synthetic_settings_disagreement')
                operation(active, 'read-check', data=kwargs['data'])
                return b'{"local":"verified"}'
            return fixture_run(argv, **kwargs)
        prep.run = recovery_run
        prep.rotate_credential_generation(directory.parent)
        before_generation = (directory.parent / '.credential-generation').read_bytes()
        report['recovery_result'] = credential_recovery.recover(prep, directory, report['source_sha'],
            'restore-reviewed-prior', True, authorization=authorization,
            authorization_sha256=hashlib.sha256(authorization.read_bytes()).hexdigest())
        report['cases']['reviewed_historical_recovery'] = 'PASS'
        report['fresh_generation'] = (directory.parent / '.credential-generation').read_bytes() != before_generation
        report['quarantine_cleared'] = not journal.exists()
        if not report['fresh_generation'] or not report['quarantine_cleared']:
            raise RuntimeError('recovery_not_usable')
        if json.loads(prep.keychain('status')) != {'credentials': 'ready'}:
            raise RuntimeError('recovered_helper_not_usable')
        report['cases']['fresh_generation_usable'] = 'PASS'
        operation(root / 'reader-replacement', 'read', denial={b'EXPECTED_POLICY_DENIAL\n', b'EXPECTED_OS_DENIAL\n'})
        report['cases']['replacement_still_denied'] = 'PASS'
        operation(root / "controller", "lock")
        operation(active, "read-locked", denial={b"EXPECTED_OS_DENIAL\n"})
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
              "qualification_scope": "ad-hoc signatures and private synthetic Keychain only",
              "schema_version": 2, "automatic_acl_migration": "unsupported", "cases": {}}
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
            from native_pipe_fixture import qualify
            report['pipe_qualification'] = qualify(root/'pipe-qualification')
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
