"""Provisioning orchestration with explicit effect boundaries and sanitized reports."""
from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit

from bundle_contract import validate_contract
from policy import PRIMARY, TAG, fragment, render, review, username, identity, funnel_disabled

ROOT = Path(__file__).resolve().parents[2]
ENV_NAMES = ("WISP_LOCAL_OMLX_KEY", "WISP_MINI_INFERENCE_KEY", "WISP_MINI_NODE_KEY")


class Refused(Exception):
    """Only fixed diagnostic codes, never external command output."""


def run(argv, *, data=None):
    env = {k: v for k, v in os.environ.items() if k not in ENV_NAMES}
    if argv[0] == "/usr/bin/git":
        env = {k: v for k, v in env.items() if not k.startswith("GIT_")}
        env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
                   GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.fsmonitor",
                   GIT_CONFIG_VALUE_0="false")
    try:
        result = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, timeout=120, check=False)
        if result.returncode:
            raise Refused("command_failed")
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        raise Refused("command_unavailable") from None


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise Refused("invalid_json_document") from None


def validate_plan(plan):
    if plan.get("schema_version") != 1:
        raise Refused("unsupported_plan_version")
    try:
        username(plan["user"])
        identity(plan["tailnet_user"])
        host = plan["host"]
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9-]+\.ts\.net", host):
            raise ValueError()
        ip = ipaddress.ip_address(plan["ip"])
        if ip not in ipaddress.ip_network("100.64.0.0/10") or str(ip) == PRIMARY:
            raise ValueError()
        if not re.fullmatch(r"n[A-Za-z0-9]+", plan["node_id"]):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise Refused("invalid_target_identity") from None


def verify_peer(status, plan):
    validate_plan(plan)
    own = status.get("Self", {})
    if status.get("BackendState") != "Running" or PRIMARY not in own.get("TailscaleIPs", []) or own.get("Tags"):
        raise Refused("primary_identity_mismatch")
    peers = [p for p in status.get("Peer", {}).values()
             if p.get("DNSName", "").rstrip(".") == plan["host"]]
    if len(peers) != 1:
        raise Refused("target_identity_ambiguous")
    peer = peers[0]
    if (peer.get("ID") != plan["node_id"] or plan["ip"] not in peer.get("TailscaleIPs", [])
            or TAG not in peer.get("Tags", []) or peer.get("Online") is not True
            or peer.get("Expired", False) is not False or not peer.get("sshHostKeys")):
        raise Refused("target_identity_mismatch")
    return peer


def local_only(config):
    try:
        url = urlsplit(config["omlx"]["base_url"])
        loopback = url.hostname == "localhost" or ipaddress.ip_address(url.hostname).is_loopback
        bindings = config.get("inference", {}).get("bindings", {})
        return (loopback and url.scheme == "http" and not url.username and not url.password
                and not url.query and not url.fragment and url.path in {"", "/"}
                and all(isinstance(b, dict) and b.get("endpoint", "local") == "local" for b in bindings.values())
                and config.get("proactive_node", {}).get("enabled", False) is False)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def preflight(snapshot, plan, policy):
    verify_peer(snapshot.get("tailscale", {}), plan)
    checks = {
        "policy_restricted": not review(policy, plan["tailnet_user"], plan["user"]),
        "firewall_preserved_enabled": snapshot.get("firewall_enabled") is True,
        "all_roles_local_jobs_disabled": local_only(snapshot.get("config", {})),
        "omlx_loopback_only": snapshot.get("omlx_loopback_only") is True,
        "credentials_ready": snapshot.get("credentials") == "ready",
        "remote_firewall_enabled": snapshot.get("remote", {}).get("firewall_enabled") is True,
        "remote_ssh_cli_variant": snapshot.get("remote", {}).get("ssh_cli_variant") is True,
        "remote_omlx_loopback": snapshot.get("remote", {}).get("omlx_loopback_only") is True,
        "remote_funnel_disabled": snapshot.get("remote", {}).get("funnel_disabled") is True,
        "remote_serve_restricted": snapshot.get("remote", {}).get("serve_restricted") is True,
        "remote_backend_ports_silent": snapshot.get("remote", {}).get("backend_ports_silent") is True,
        "remote_jobs_disabled": snapshot.get("remote", {}).get("jobs_disabled") is True,
    }
    return checks


HELPER_INPUTS = ("app/Sources/WispApp/BackendCredentials.swift",
                "infra/mac-mini/keychain-helper.swift", "build-support/toolchain.json")


def helper_inputs(source):
    if not isinstance(source, str) or not re.fullmatch(r"[0-9a-f]{40}", source):
        raise Refused("reviewed_helper_source_required")
    return {name: run(["/usr/bin/git", "-C", str(ROOT), "show", source + ":" + name])
            for name in HELPER_INPUTS}


def helper_sources(inputs):
    return {name: hashlib.sha256(data).hexdigest() for name, data in inputs.items()}


def verify_helper(directory, *, expected_source=None):
    import stat
    binary = directory / "wisp-keychain-helper"
    receipt = directory / "helper.json"
    for path, mode, kind in ((directory, 0o700, stat.S_ISDIR), (binary, 0o700, stat.S_ISREG), (receipt, 0o600, stat.S_ISREG)):
        info = path.lstat()
        if not kind(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode or info.st_nlink != 1 and path != directory:
            raise Refused("unsafe_helper_permissions")
    record = read_json(receipt)
    if (record.get("schema_version") != 3
            or expected_source is not None and record.get("source_commit") != expected_source):
        raise Refused("helper_provenance_mismatch")
    if (record.get("sources") != helper_sources(helper_inputs(record.get("source_commit")))
            or record.get("sha256") != hashlib.sha256(binary.read_bytes()).hexdigest()):
        raise Refused("helper_provenance_mismatch")
    run(["/usr/bin/codesign", "--verify", "--strict", str(binary)])
    if run([str(binary), "protocol-version"]).strip() != b"wisp-mini-helper-v2":
        raise Refused("helper_version_mismatch")
    return binary


def private_moe(directory, *, migrate=False):
    """Only explicit initialization may tighten a normal umask-022 directory."""
    import stat
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise Refused("unsafe_helper_path")
    if migrate:
        directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        if info.st_uid != os.getuid() or mode not in ({0o700, 0o755} if migrate else {0o700}):
            raise Refused("unsafe_helper_permissions")
        if mode != 0o700:
            os.fchmod(fd, 0o700)
        after = os.fstat(fd)
        if stat.S_IMODE(after.st_mode) != 0o700 or directory.lstat().st_ino != after.st_ino:
            raise Refused("unsafe_helper_permissions")
    finally:
        os.close(fd)


def swap_helper_directory(stage, directory):
    """macOS atomically exchanges the entire binary/receipt pair, retaining old state."""
    import ctypes
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(os.fsencode(stage), os.fsencode(directory), 0x00000002) != 0:  # RENAME_SWAP
        raise Refused("atomic_helper_upgrade_failed")


def helper_snapshot(directory):
    """Compare restored state without executing or trusting a legacy helper."""
    import stat
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) not in (0o700, 0o755)):
        raise Refused("unsafe_helper_permissions")
    entries = {}
    for entry in directory.iterdir():
        item = entry.lstat()
        if (not stat.S_ISREG(item.st_mode) or item.st_uid != os.getuid()
                or item.st_nlink != 1 or stat.S_IMODE(item.st_mode) & 0o7022):
            raise Refused("unsafe_helper_permissions")
        entries[entry.name] = {"mode": stat.S_IMODE(item.st_mode), "uid": item.st_uid,
            "sha256": hashlib.sha256(entry.read_bytes()).hexdigest()}
    return {"inode": info.st_ino, "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "files": entries}


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def recovery_marker(journal, record):
    fd, temp = tempfile.mkstemp(prefix=".helper-journal-", dir=journal.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(record, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, journal)
        sync_directory(journal.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def prepare_helper(directory, *, expected_source=None, accept=None):
    if os.path.lexists(directory.parent / ".helper-transaction.json"):
        raise Refused("helper_recovery_required")
    clean_source(expected_source)
    with provisioning_lock(directory.parent):
        return _prepare_helper(directory, expected_source=expected_source, accept=accept)


def rotate_credential_generation(parent):
    import secrets
    fd, temporary = tempfile.mkstemp(prefix=".credential-generation-", dir=parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(secrets.token_hex(32))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, parent / ".credential-generation")
        sync_directory(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _prepare_helper(directory, *, expected_source=None, accept=None):
    """Publish a source-pinned receipt/helper pair; never execute the old helper."""
    import shutil
    import stat
    published = False
    journal = directory.parent / ".helper-transaction.json"
    if journal.exists() or journal.is_symlink():
        raise Refused("helper_recovery_required")
    clean_source(expected_source)
    inputs = helper_inputs(expected_source)
    input_hashes = helper_sources(inputs)
    stage = Path(tempfile.mkdtemp(prefix=".helper-previous-", dir=directory.parent))
    prior = None
    try:
        if directory.exists() or directory.is_symlink():
            prior = helper_snapshot(directory)
            # Preserve unrelated receipts; unknown nested or linked state refuses.
            for entry in directory.iterdir():
                if entry.name not in ("wisp-keychain-helper", "helper.json"):
                    shutil.copy2(entry, stage / entry.name, follow_symlinks=False)
        binary = stage / "wisp-keychain-helper"
        try:
            version = run(["/usr/bin/swiftc", "--version"]).decode("utf-8")
        except UnicodeError:
            raise Refused("unqualified_helper_compiler") from None
        config = json.loads(inputs["build-support/toolchain.json"])
        if not re.search(r"\bSwift version " + re.escape(config["ci_swift"]) + r"(?=\s|$)", version):
            raise Refused("unqualified_helper_compiler")
        with tempfile.TemporaryDirectory(prefix=".helper-inputs-", dir=directory.parent) as temporary:
            compile_paths = []
            for name in HELPER_INPUTS[:2]:
                path = Path(temporary) / Path(name).name
                path.write_bytes(inputs[name])
                path.chmod(0o600)
                compile_paths.append(path)
            run(["/usr/bin/swiftc", "-parse-as-library", "-module-cache-path", str(stage / "cache"),
                 *map(str, compile_paths), "-o", str(binary)])
            for name, path in zip(HELPER_INPUTS, compile_paths):
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                        or path.read_bytes() != inputs[name]):
                    raise Refused("helper_compile_input_changed")
        clean_source(expected_source)
        binary.chmod(0o700)
        run(["/usr/bin/codesign", "--force", "--sign", "-", str(binary)])
        receipt = stage / "helper.json"
        receipt.write_text(json.dumps({"schema_version": 3, "source_commit": expected_source, "sources": input_hashes,
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "compiler": version}, sort_keys=True))
        receipt.chmod(0o600)
        sealed_receipt = receipt.read_bytes()
        sealed_binary = hashlib.sha256(binary.read_bytes()).hexdigest()
        verify_helper(stage, expected_source=expected_source)
        def unchanged_candidate(candidate):
            for name, mode in (("helper.json", 0o600), ("wisp-keychain-helper", 0o700)):
                path = candidate / name
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode):
                    raise Refused("helper_candidate_changed")
            if ((candidate / "helper.json").read_bytes() != sealed_receipt
                    or hashlib.sha256((candidate / "wisp-keychain-helper").read_bytes()).hexdigest() != sealed_binary):
                raise Refused("helper_candidate_changed")
        shutil.rmtree(stage / "cache", ignore_errors=True)
        for entry in stage.iterdir():
            with entry.open("rb") as source:
                os.fsync(source.fileno())
        sync_directory(stage)
        clean_source(expected_source)
        unchanged_candidate(stage)
        # A persistent, secret-free journal blocks all cooperating readers after
        # interruption or failed restoration. Retain both states for recovery.
        record = {"schema_version": 1, "slot": stage.name, "prior": prior, "phase": "publishing"}
        # Invalidate credentials captured before this transaction, even if the
        # helper is restored and a later recovery removes the journal.
        rotate_credential_generation(directory.parent)
        with open(journal, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
            json.dump(record, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        sync_directory(directory.parent)
        acceptance_started = False
        try:
            if prior is not None:
                swap_helper_directory(stage, directory)
            else:
                os.rename(stage, directory)
            published = True
            sync_directory(directory.parent)
            binary = directory / "wisp-keychain-helper"
            clean_source(expected_source)
            unchanged_candidate(directory)
            acceptance_started = accept is not None
            result = accept(binary) if accept is not None else binary
            sync_directory(directory.parent)
            journal.unlink()
            sync_directory(directory.parent)
        except BaseException:
            try:
                if published:
                    if prior is not None:
                        swap_helper_directory(stage, directory)
                    else:
                        os.rename(directory, stage)
                restored = helper_snapshot(directory) if directory.exists() else None
                if restored != prior:
                    raise Refused("helper_restore_unverified")
                sync_directory(directory.parent)
                if acceptance_started:
                    # initialize() can have created some missing entries before
                    # failure. Restoring files proves nothing about those ACLs.
                    record["phase"] = "helper_restored_keychain_unverified"
                    recovery_marker(journal, record)
                else:
                    journal.unlink()
                    sync_directory(directory.parent)
            except BaseException:
                # Removal might have preceded a failed durability barrier.
                # Recreate a blocking marker where storage still permits it.
                try:
                    record["phase"] = "restoration_unverified"
                    recovery_marker(journal, record)
                except BaseException:
                    pass
                raise Refused("helper_recovery_required") from None
            if acceptance_started:
                raise Refused("helper_recovery_required") from None
            raise
        return result
    finally:
        if not published and not journal.exists():
            shutil.rmtree(stage, ignore_errors=True)


def accept_primary_helper(binary, local):
    result = run([str(binary), "init", "/Applications/Wisp.app"], data=local.encode())
    try:
        if json.loads(result) != {"credentials": "ready"}:
            raise ValueError
        verify_helper(binary.parent)
        if json.loads(run([str(binary), "status"])) != {"credentials": "ready"}:
            raise ValueError
    except (ValueError, TypeError):
        raise Refused("helper_acceptance_failed") from None
    return result


from contextlib import contextmanager


@contextmanager
def provisioning_lock(parent):
    import fcntl
    import stat
    lock = os.open(parent / ".provisioning.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise Refused("unsafe_helper_permissions")
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock)


def keychain(command, *, expected_source=None):
    directory = Path.home() / ".moe" / "provisioning"
    try:
        if os.path.lexists(directory.parent / ".helper-transaction.json"):
            raise Refused("helper_recovery_required")
        if command == "init":
            clean_source(expected_source)
        private_moe(directory.parent, migrate=command == "init")
        with provisioning_lock(directory.parent):
            if os.path.lexists(directory.parent / ".helper-transaction.json"):
                raise Refused("helper_recovery_required")
            if command == "init":
                if not Path("/Applications/Wisp.app").is_dir():
                    raise Refused("trusted_wisp_app_required")
                run(["/usr/bin/codesign", "--verify", "--strict", "/Applications/Wisp.app"])
                settings = read_json(Path.home() / ".omlx" / "settings.json")
                local = settings.get("auth", {}).get("api_key", "")
                if not isinstance(local, str) or not re.fullmatch(r"[0-9a-f]{64}", local):
                    raise Refused("local_auth_migration_required")
                return _prepare_helper(directory, expected_source=expected_source,
                                      accept=lambda binary: accept_primary_helper(binary, local))
            binary = verify_helper(directory)
            return run([str(binary), command])
    except OSError:
        raise Refused("primary_initialization_required") from None


def validate_bundle(path, expected_sha, expected_source=None):
    raw = Path(path).read_bytes()
    return raw, validate_bundle_bytes(raw, expected_sha, expected_source)[0]


def validate_bundle_bytes(raw, expected_sha, expected_source=None):
    """Validate the same immutable bytes that will be sent, without extraction."""
    if len(raw) > 256 * 1024 * 1024 or not re.fullmatch(r"[0-9a-f]{64}", expected_sha or ""):
        raise Refused("invalid_bundle_pin")
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise Refused("bundle_digest_mismatch")
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            members = archive.getmembers()
            names = [m.name for m in members]
            if len(names) != len(set(names)) or len(names) > 30000:
                raise Refused("unsafe_bundle_entries")
            total = 0
            files = {}
            for member in members:
                parts = Path(member.name).parts
                if not parts or Path(member.name).is_absolute() or ".." in parts or str(Path(member.name)) != member.name or not member.isfile():
                    raise Refused("unsafe_bundle_entry")
                total += member.size
                if total > 1024 * 1024 * 1024:
                    raise Refused("bundle_too_large")
                files[member.name] = archive.extractfile(member).read()
            manifest = json.loads(files["mini/bundle.json"])
            if manifest.get("schema_version") != 1:
                raise Refused("unsupported_bundle_version")
            if manifest.get("kind") != "wisp-mini-runtime":
                raise Refused("unsupported_bundle_contract")
            if expected_source is not None and manifest.get("source_commit") != expected_source:
                raise Refused("candidate_source_mismatch")
            rows = manifest["files"]
            validate_contract(manifest)
            expected = {row["path"]: row["sha256"] for row in rows}
            if len(expected) != len(rows) or any(not name.startswith("mini/") for name in expected):
                raise Refused("invalid_bundle_file_list")
            if set(expected) != set(files) - {"mini/bundle.json"}:
                raise Refused("bundle_file_set_mismatch")
            if any(hashlib.sha256(files[name]).hexdigest() != digest for name, digest in expected.items()):
                raise Refused("bundle_file_digest_mismatch")
    except (tarfile.TarError, KeyError, ValueError, TypeError):
        raise Refused("invalid_bundle_manifest") from None
    return manifest, files


def emit(command, mode, checks=None, **extra):
    report = {"schema_version": 1, "command": command, "mode": mode, "checks": checks or {}, **extra}
    print(json.dumps(report, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init-primary", "policy-render", "preflight", "activate", "doctor", "rollback"])
    parser.add_argument("--plan")
    parser.add_argument("--policy", help="complete exported policy as strict JSON, not only a fragment")
    parser.add_argument("--fixture", help="synthetic snapshot; never contacts any system")
    parser.add_argument("--live", action="store_true", help="explicitly allow system reads; combine --apply for writes")
    parser.add_argument("--apply", action="store_true", help="explicitly authorize this command's live mutation")
    parser.add_argument("--bundle")
    parser.add_argument("--bundle-sha256")
    parser.add_argument("--source-sha")
    args = parser.parse_args(argv)
    mode = "live" if args.live else "dry-run"
    try:
        if args.fixture and (args.live or args.apply) or args.apply and not args.live:
            raise Refused("fixture_live_conflict")
        if args.command == "init-primary":
            if args.live:
                status = json.loads(keychain("init", expected_source=args.source_sha) if args.apply else keychain("status"))
                emit(args.command, mode, credentials_ready=status.get("credentials") == "ready")
                return 0 if status.get("credentials") == "ready" else 1
            emit(args.command, mode, planned_accounts=["local-omlx", "mini-inference", "mini-node"], mutations=0)
            return 0
        if not args.plan or not args.policy:
            raise Refused("plan_and_full_policy_required")
        plan, policy = read_json(args.plan), read_json(args.policy)
        validate_plan(plan)
        if args.command == "policy-render":
            combined = render(policy, plan["tailnet_user"], plan["user"])
            print(json.dumps(combined, indent=2, sort_keys=True))
            warnings = review(combined, plan["tailnet_user"], plan["user"])
            print("Additive policy: existing grants remain effective; full policy review is required.", file=sys.stderr)
            if warnings:
                print("Activation blocked: broader or unresolved additive access.", file=sys.stderr)
            return 1 if warnings else 0
        if args.command == "rollback":
            # Recovery must remain possible when credentials/firewall/policy or
            # running-job posture fail activation gates. Identity still gates it.
            if args.live:
                current_peer(plan)
                if args.apply:
                    rollback(plan, expected_source=args.source_sha)
                    restore_primary_local()
                    emit(args.command, mode, status="restart_required", remote_jobs_enabled=False,
                         primary_config="local", primary_runtime="restart_required")
                    return 2
                else:
                    sys.path.insert(0, str(ROOT))
                    from service.config import models_config
                    if not local_only(models_config()):
                        raise Refused("primary_rollback_pending")
            elif args.fixture:
                verify_peer(read_json(args.fixture).get("tailscale", {}), plan)
            else:
                raise Refused("fixture_required_for_dry_run")
            emit(args.command, mode, status="ready", planned_remote_jobs_enabled=False)
            return 0
        if args.live:
            snapshot = live_snapshot(plan)
        elif args.fixture:
            snapshot = read_json(args.fixture)
        else:
            raise Refused("fixture_required_for_dry_run")
        checks = preflight(snapshot, plan, policy)
        if not all(checks.values()):
            emit(args.command, mode, checks, status="blocked")
            return 1
        if args.command == "activate":
            if not args.bundle:
                raise Refused("versioned_bundle_required")
            raw, manifest = validate_bundle(args.bundle, args.bundle_sha256, args.source_sha)
            checks["bundle_verified"] = True
            if args.live and args.apply:
                if not args.source_sha:
                    raise Refused("reviewed_source_sha_required")
                activate(plan, raw, manifest, args.bundle_sha256)
        elif args.command == "rollback" and args.live and args.apply:
            rollback(plan, expected_source=args.source_sha)
        emit(args.command, mode, checks, status="ready" if not args.apply else "complete", roles="local", proactive_node=False, gateway_qualification_required=True)
        return 0
    except Exception:
        # Never echo external output, exception text, plan content, or credentials.
        emit(args.command, mode, status="blocked", error="prerequisite_or_operation_failed")
        return 1

TAILSCALE = "/opt/homebrew/bin/tailscale"


def receiver_source():
    return (ROOT / "infra/mac-mini/socket_posture.py").read_text() + "\n" + (ROOT / "infra/mac-mini/bundle_contract.py").read_text() + "\n" + (ROOT / "infra/mac-mini/receiver.py").read_text()


def ssh(plan, source, *arguments, data=None):
    import shlex
    validate_plan(plan)
    # Each argument is quoted because SSH transports a remote shell command.
    command = " ".join(shlex.quote(s) for s in ["/usr/bin/python3", "-I", "-B", "-c", source, *arguments])
    return run([TAILSCALE, "ssh", plan["user"] + "@" + plan["ip"], command], data=data)


def current_peer(plan):
    status = json.loads(run([TAILSCALE, "status", "--json"]))
    verify_peer(status, plan)
    return status


def live_snapshot(plan):
    from remote_probe import probe
    status = current_peer(plan)
    # No imports of the application server, tools, MCP or source readers.
    sys.path.insert(0, str(ROOT))
    from service.config import models_config
    remote = json.loads(ssh(plan, (ROOT / "infra/mac-mini/socket_posture.py").read_text() + "\n" + (ROOT / "infra/mac-mini/remote_probe.py").read_text(), plan["host"]))
    local = probe()
    credentials = json.loads(keychain("status"))
    return {"tailscale": status, "firewall_enabled": local["firewall_enabled"],
            "omlx_loopback_only": local["omlx_loopback_only"], "config": models_config(),
            "credentials": credentials.get("credentials"), "remote": remote}


def activate(plan, raw, manifest, digest):
    import base64
    if manifest.get("artifact_type") != "offline-runtime" or manifest.get("provenance", {}).get("strict_toolchain") is not True:
        raise Refused("qualified_offline_candidate_required")
    authenticated, files = validate_bundle_bytes(raw, digest, manifest.get("source_commit"))
    if authenticated != manifest:
        raise Refused("candidate_manifest_mismatch")
    try:
        source = files["mini/payload/provisioning/receiver.py"].decode("utf-8")
    except (KeyError, UnicodeError):
        raise Refused("authenticated_receiver_required") from None
    current_peer(plan)  # Verify identity again immediately before credential access.
    credentials = json.loads(keychain("export-mini"))
    if (set(credentials) != {"mini-inference", "mini-node"} or len(set(credentials.values())) != 2
            or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in credentials.values())):
        raise Refused("invalid_mini_credentials")
    payload = {"schema_version": 1, "operation": "stage", "node_id": plan["node_id"],
               "bundle_sha256": digest, "bundle": base64.b64encode(raw).decode(),
               "source_commit": manifest["source_commit"], "credentials": credentials}
    result = json.loads(ssh(plan, source,
                            data=json.dumps(payload).encode()))
    if result != {"schema_version": 1, "status": "complete", "jobs_enabled": False, "gateway_qualification_required": True}:
        raise Refused("remote_staging_incomplete")
    record_binding(plan, manifest, digest)


def record_binding(plan, manifest, digest):
    # Bind native credentials only after the reviewed peer and exact candidate
    # have been staged. Model YAML cannot authorize a new credential origin.
    directory = Path.home() / ".moe/provisioning"
    private_moe(directory.parent)
    with provisioning_lock(directory.parent):
        if os.path.lexists(directory.parent / ".helper-transaction.json"):
            raise Refused("helper_recovery_required")
        verify_helper(directory)
        fd, temporary = tempfile.mkstemp(prefix=".endpoints-", dir=directory)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump({"schema_version": 1, "node_id": plan["node_id"], "host": plan["host"],
                           "source_commit": manifest["source_commit"], "bundle_sha256": digest}, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, directory / "endpoints.json")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def clean_source(expected_source):
    if not isinstance(expected_source, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_source):
        raise Refused("reviewed_rollback_source_required")
    head = run(["/usr/bin/git", "-C", str(ROOT), "rev-parse", "HEAD"]).strip()
    dirty = run(["/usr/bin/git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all"])
    if head != expected_source.encode() or dirty.strip():
        raise Refused("clean_exact_rollback_source_required")


def rollback(plan, *, expected_source=None):
    # The separately reviewed source pin authorizes recovery code, even when
    # the staged release is older or primary helper acceptance is unavailable.
    clean_source(expected_source)
    try:
        source = "\n".join(run(["/usr/bin/git", "-C", str(ROOT), "show",
            expected_source + ":infra/mac-mini/" + name]).decode("utf-8")
            for name in ("socket_posture.py", "bundle_contract.py", "receiver.py"))
    except UnicodeError:
        raise Refused("invalid_rollback_source") from None
    current_peer(plan)
    clean_source(expected_source)
    result = json.loads(ssh(plan, source, "rollback"))
    if result != {"schema_version": 1, "status": "complete", "jobs_enabled": False, "gateway_qualification_required": True}:
        raise Refused("remote_rollback_incomplete")


def restore_primary_local():
    sys.path.insert(0, str(ROOT))
    from service.config import models_config, _save_overlay
    config = models_config()
    bindings = {}
    for role in config.get("inference", {}).get("bindings", {}):
        model = config.get("roles", {}).get(role)
        if not isinstance(model, str) or not model:
            raise Refused("known_local_role_required")
        bindings[role] = {"endpoint": "local", "model_id": model, "revision": "", "profile": "",
                          "dimensions": 0, "context_window": 0, "qualified_capabilities": []}
    _save_overlay({"inference": {"bindings": bindings}, "proactive_node": {"enabled": False}})
    if not local_only(models_config()):
        raise Refused("primary_local_restore_unverified")
