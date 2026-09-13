"""Tailscale-SSH receiver. Input and child output are never echoed or logged.

Stages a versioned runtime and secret-free launchd templates, with jobs unloaded.
Only the two remote credentials are accepted; primary local auth never travels.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tarfile
import tempfile
import shutil
import stat

if "validate_contract" not in globals():
    from bundle_contract import validate_contract


LABELS = ("com.wisp.mini.gateway", "com.wisp.mini.node")
ASSETS = {"BackendCredentials.swift", "keychain-helper.swift", "mini-launcher.swift"}


def execute(argv, data=None):
    env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}
    result = subprocess.run(argv, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=env, timeout=300)
    if result.returncode:
        raise ValueError("operation_failed")


def unpack(raw):
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        files = {}
        size = 0
        for member in archive.getmembers():
            name = member.name
            if (not member.isfile() or not name.startswith("mini/") or ".." in Path(name).parts or str(Path(name)) != name
                    or name in files or len(files) >= 30000):
                raise ValueError("unsafe_bundle")
            size += member.size
            if size > 1024 * 1024 * 1024:
                raise ValueError("large_bundle")
            files[name] = archive.extractfile(member).read()
    manifest = json.loads(files["mini/bundle.json"])
    if manifest["schema_version"] != 1 or manifest["kind"] != "wisp-mini-runtime":
        raise ValueError("bundle_contract")
    validate_contract(manifest)
    rows = manifest["files"]
    hashes = {r["path"]: r["sha256"] for r in rows}
    if len(rows) != len(hashes) or set(hashes) != set(files) - {"mini/bundle.json"}:
        raise ValueError("bundle_files")
    if any(r.get("mode", 0o600) not in (0o600, 0o700) for r in rows):
        raise ValueError("bundle_mode")
    if any(hashlib.sha256(files[name]).hexdigest() != digest for name, digest in hashes.items()):
        raise ValueError("bundle_digest")
    return files


def root_path():
    root = Path.home() / ".wisp-mini"
    if root.is_symlink():
        raise ValueError("unsafe_root")
    if root.exists() and (not root.is_dir() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
        raise ValueError("unsafe_root")
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def active_jobs():
    active = set()
    for domain in ("gui/", "user/"):
        target = domain + str(os.getuid())
        result = subprocess.run(["/bin/launchctl", "print", target], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=10)
        if result.returncode:
            if domain == "gui/" and result.returncode == 125:
                continue
            raise ValueError("launchd_unavailable")
        for label in LABELS:
            service_target = target + "/" + label
            service = subprocess.run(["/bin/launchctl", "print", service_target], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=10)
            if service.returncode == 0:
                active.add(service_target)
            elif service.returncode != 113:
                raise ValueError("launchd_service_unknown")
    return active


def rollback():
    active = active_jobs()
    root = Path.home() / ".wisp-mini"
    if not root.exists():
        if active or root.is_symlink():
            raise ValueError("ownership_unproven")
        return
    root = root_path()
    receipt = root / "receipt.json"
    if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_uid != os.getuid() or receipt.stat().st_mode & 0o077:
        raise ValueError("ownership_unproven")
    record = json.loads(receipt.read_text())
    if record.get("schema_version") != 1 or record.get("labels") != list(LABELS):
        raise ValueError("ownership_unproven")
    release_id = record.get("provisioning_id", "")
    if not re.fullmatch(r"[0-9a-f]{64}", release_id):
        raise ValueError("ownership_unproven")
    release = root / release_id
    marker = release / ".owner.json"
    if release.is_symlink() or marker.is_symlink() or not marker.is_file():
        raise ValueError("ownership_unproven")
    owner = json.loads(marker.read_text())
    if (owner.get("schema_version") != 2 or owner.get("provisioning_id") != release_id
            or owner.get("bundle_sha256") != record.get("bundle_sha256")
            or owner.get("source_commit") != record.get("source_commit")
            or owner.get("inventory") != inventory(release)):
        raise ValueError("ownership_unproven")
    for target in sorted(active):
        execute(["/bin/launchctl", "bootout", target])
    if active_jobs():
        raise ValueError("jobs_still_loaded")
    # Keep installed versions, user state, Keychain and firewall untouched.


def materialize(files, manifest, destination):
    modes = {row["path"]: row.get("mode", 0o600) for row in manifest["files"]}
    for name, content in files.items():
        if not name.startswith("mini/payload/"):
            continue
        relative = name.removeprefix("mini/payload/")
        path = destination / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(modes[name])
    for directory in destination.rglob("*"):
        if directory.is_dir():
            directory.chmod(0o700)


def inventory(root):
    rows = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == ".owner.json":
            continue
        info = path.lstat()
        if relative == "state":
            if not path.is_symlink() or path.resolve() != root.parent / "state":
                raise ValueError("unsafe_state_link")
            rows[relative] = {"type": "state"}
            continue
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe_runtime_owner_mode")
        if stat.S_ISDIR(info.st_mode):
            rows[relative] = {"type": "directory", "mode": stat.S_IMODE(info.st_mode)}
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            rows[relative] = {"type": "file", "mode": stat.S_IMODE(info.st_mode),
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        else:
            raise ValueError("unsafe_runtime_type")
    return rows


def runtime_health(release):
    for name in ("keychain-helper", "mini-launcher"):
        execute(["/usr/bin/codesign", "--verify", "--strict", str(release / name)])
        result = subprocess.run([str(release / name), "protocol-version"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
            env={"PATH": "/usr/bin:/bin"})
        if result.returncode or result.stdout != b"wisp-mini-helper-v2\n":
            raise ValueError("unsupported_helper_version")
    execute([str(release / "venv/bin/python3"), "-I", "-B", str(release / "runtime-health.py"), str(release)])


def install(payload):
    if payload.get("schema_version") != 1 or payload.get("operation") != "stage":
        raise ValueError("invalid_protocol")
    digest = payload["bundle_sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid_pin")
    raw = base64.b64decode(payload["bundle"], validate=True)
    if len(raw) > 256 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("invalid_bundle")
    files = unpack(raw)
    manifest = json.loads(files["mini/bundle.json"])
    if (manifest.get("artifact_type") != "offline-runtime"
            or manifest["source_commit"] != payload.get("source_commit")
            or manifest["provenance"].get("strict_toolchain") is not True):
        raise ValueError("qualified_offline_candidate_required")
    secrets = payload["credentials"]
    if (set(secrets) != {"mini-inference", "mini-node"} or len(set(secrets.values())) != 2
            or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in secrets.values())):
        raise ValueError("invalid_credentials")
    node_id = payload["node_id"]
    if not re.fullmatch(r"n[A-Za-z0-9]+", node_id) or os.getuid() == 0:
        raise ValueError("invalid_user_or_node")
    if active_jobs():
        raise ValueError("jobs_must_be_disabled")
    root = root_path()
    import fcntl
    fd = os.open(root / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise ValueError("unsafe_install_lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        provisioning_id = hashlib.sha256(json.dumps({"bundle": digest, "node_id": node_id}, sort_keys=True).encode()).hexdigest()
        release = root / provisioning_id
        if release.is_symlink() or release.exists() and not release.is_dir():
            raise ValueError("unsafe_release")
        owner = {"schema_version": 2, "provisioning_id": provisioning_id, "node_id": node_id,
                 "source_commit": manifest["source_commit"], "bundle_sha256": digest,
                 "provenance": manifest["provenance"]}
        # Build an independent expected inventory from authenticated artifact bytes
        # even for repeat staging; a forged owner marker cannot bless changed code.
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=root) as temporary:
            stage = Path(temporary)
            materialize(files, manifest, stage)
            (stage / "launchd").mkdir(mode=0o700)
            for kind, label in zip(("gateway", "node"), LABELS):
                plist = {"Label": label, "ProgramArguments": [str(release / "mini-launcher"), kind, str(release), node_id],
                         "RunAtLoad": False, "KeepAlive": False, "Disabled": True,
                         "ProcessType": "Background", "Umask": 0o077}
                dest = stage / "launchd" / (label + ".plist")
                dest.write_bytes(plistlib.dumps(plist))
                dest.chmod(0o600)
            state = root / "state"
            if state.is_symlink() or state.exists() and (not state.is_dir() or state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077):
                raise ValueError("unsafe_state")
            # State is deliberately outside the immutable executable inventory.
            (stage / "state").symlink_to(state, target_is_directory=True)
            expected = inventory(stage)
            owner["inventory"] = expected
            if release.exists():
                marker = release / ".owner.json"
                if marker.is_symlink() or not marker.is_file() or marker.stat().st_mode & 0o077 or json.loads(marker.read_text()) != owner or inventory(release) != expected:
                    raise ValueError("existing_release_unproven")
                runtime_health(release)
            else:
                runtime_health(stage)
                marker = stage / ".owner.json"
                marker.write_text(json.dumps(owner, sort_keys=True))
                marker.chmod(0o600)
                # The only publication is an atomic directory rename, after every
                # file, signature, import and synthetic health check passed.
                os.rename(stage, release)
                stage.mkdir(mode=0o700)  # TemporaryDirectory cleanup owns only this.
                try:
                    runtime_health(release)
                except BaseException:
                    shutil.rmtree(release)
                    raise
            state.mkdir(mode=0o700, exist_ok=True)
            execute([str(release / "keychain-helper"), "import-mini"], json.dumps(secrets).encode())
            if active_jobs():
                raise ValueError("jobs_must_be_disabled")
            receipt = {"schema_version": 1, "bundle_sha256": digest, "provisioning_id": provisioning_id,
                       "source_commit": manifest["source_commit"], "provenance": manifest["provenance"],
                       "labels": list(LABELS), "jobs_enabled": False, "gateway_qualification_required": True}
            fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=root)
            try:
                with os.fdopen(fd, "w") as output:
                    json.dump(receipt, output, sort_keys=True)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, root / "receipt.json")
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


def main():
    try:
        os.umask(0o077)
        if sys.argv[1:] == ["rollback"]:
            rollback()
        elif not sys.argv[1:]:
            raw = sys.stdin.buffer.read(400 * 1024 * 1024 + 1)
            if len(raw) > 400 * 1024 * 1024:
                raise ValueError("large_frame")
            install(json.loads(raw))
        else:
            raise ValueError("invalid_operation")
        print('{"schema_version":1,"status":"complete","jobs_enabled":false,"gateway_qualification_required":true}')
    except Exception:
        print('{"schema_version":1,"status":"blocked"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
