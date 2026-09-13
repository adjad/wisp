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

if "validate_contract" not in globals():
    from bundle_contract import validate_contract

BASE = "c23e9c9e8860222a8aa4b070364a7e17236b3ec8"
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
                    or name in files or len(files) >= 2000):
                raise ValueError("unsafe_bundle")
            size += member.size
            if size > 128 * 1024 * 1024:
                raise ValueError("large_bundle")
            files[name] = archive.extractfile(member).read()
    manifest = json.loads(files["mini/bundle.json"])
    if manifest["schema_version"] != 1 or manifest["kind"] != "wisp-mini-runtime" or manifest["base_commit"] != BASE:
        raise ValueError("bundle_contract")
    validate_contract(manifest)
    rows = manifest["files"]
    hashes = {r["path"]: r["sha256"] for r in rows}
    if len(rows) != len(hashes) or set(hashes) != set(files) - {"mini/bundle.json"}:
        raise ValueError("bundle_files")
    if any(hashlib.sha256(files[name]).hexdigest() != digest for name, digest in hashes.items()):
        raise ValueError("bundle_digest")
    return files


def root_path():
    root = Path.home() / ".wisp-mini"
    if root.is_symlink():
        raise ValueError("unsafe_root")
    if root.exists() and (root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
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
    root = Path.home() / ".wisp-mini"
    if not root.exists():
        return
    root = root_path()
    receipt = root / "receipt.json"
    if receipt.is_symlink() or not receipt.exists():
        raise ValueError("ownership_unproven")
    record = json.loads(receipt.read_text())
    if record.get("schema_version") != 1 or record.get("labels") != list(LABELS):
        raise ValueError("ownership_unproven")
    active = active_jobs()
    for target in sorted(active):
        execute(["/bin/launchctl", "bootout", target])
    if active_jobs():
        raise ValueError("jobs_still_loaded")
    # Keep installed versions, user state, Keychain and firewall untouched.


def install(payload):
    if payload.get("schema_version") != 1 or payload.get("operation") != "stage":
        raise ValueError("invalid_protocol")
    digest = payload["bundle_sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid_pin")
    raw = base64.b64decode(payload["bundle"], validate=True)
    if len(raw) > 64 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("invalid_bundle")
    files = unpack(raw)
    assets = payload["assets"]
    if set(assets) != ASSETS or any(not isinstance(v, str) or len(v) > 50000 for v in assets.values()):
        raise ValueError("invalid_assets")
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
    # Serialize staging so concurrent activation cannot overwrite a receipt.
    import fcntl
    lockpath = root / ".install.lock"
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        provisioning_id = hashlib.sha256(json.dumps({"bundle": digest, "assets": assets, "node_id": node_id}, sort_keys=True).encode()).hexdigest()
        release = root / provisioning_id
        owner = {"schema_version": 1, "provisioning_id": provisioning_id, "node_id": node_id}
        if release.is_symlink():
            raise ValueError("unsafe_release")
        if release.exists():
            marker = release / ".owner.json"
            if marker.is_symlink() or not marker.is_file() or json.loads(marker.read_text()) != owner:
                raise ValueError("existing_release_unproven")
        else:
            release.mkdir(mode=0o700)
            (release / ".owner.json").write_text(json.dumps(owner, sort_keys=True))
        # Resumable staging: only exact owned content may be reused. Nothing
        # runs until a separate service-enablement operation after qualification.
        for name, content in {**{"runtime/" + k: v for k, v in files.items()},
                              **{k: v.encode() for k, v in assets.items()}}.items():
            dest = release / name
            if any(parent.is_symlink() for parent in dest.parents if parent != root.parent):
                raise ValueError("unsafe_release_path")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_symlink() or dest.exists() and dest.read_bytes() != content:
                raise ValueError("changed_release_content")
            if not dest.exists():
                dest.write_bytes(content)
        for source, binary in (("keychain-helper.swift", "keychain-helper"), ("mini-launcher.swift", "mini-launcher")):
            if not (release / binary).exists():
                execute(["/usr/bin/swiftc", "-parse-as-library", "-module-cache-path", str(release / "cache"),
                         str(release / "BackendCredentials.swift"), str(release / source), "-o", str(release / binary)])
        if not (release / ".venv-ready").exists():
            execute(["/opt/homebrew/bin/python3.13", "-m", "venv", str(release / "venv")])
            (release / ".venv-ready").touch()
        if not (release / ".dependencies-ready").exists():
            execute([str(release / "venv/bin/python"), "-m", "pip", "install", "--disable-pip-version-check",
                     "-r", str(release / "runtime/mini/requirements.txt")])
            (release / ".dependencies-ready").touch()
        # Always validate both keys, including repeated activation. Import is
        # idempotent for matching values and refuses rotation or local-key reuse.
        execute([str(release / "keychain-helper"), "import-mini"], json.dumps(secrets).encode())
        (release / "launchd").mkdir(exist_ok=True)
        for kind, label in zip(("gateway", "node"), LABELS):
            plist = {"Label": label, "ProgramArguments": [str(release / "mini-launcher"), kind, str(release), node_id],
                     "RunAtLoad": False, "KeepAlive": False, "Disabled": True,
                     "ProcessType": "Background", "Umask": 0o077}
            dest = release / "launchd" / (label + ".plist")
            data = plistlib.dumps(plist)
            if dest.is_symlink() or dest.exists() and dest.read_bytes() != data:
                raise ValueError("changed_launch_template")
            if not dest.exists():
                dest.write_bytes(data)
        state = root / "state"
        if state.is_symlink():
            raise ValueError("unsafe_state")
        state.mkdir(mode=0o700, exist_ok=True)
        link = release / "state"
        if not link.is_symlink() and not link.exists():
            link.symlink_to(state, target_is_directory=True)
        elif not link.is_symlink() or link.resolve() != state:
            raise ValueError("unsafe_state_link")
        receipt = {"schema_version": 1, "bundle_sha256": digest, "provisioning_id": provisioning_id,
                   "labels": list(LABELS), "jobs_enabled": False, "gateway_qualification_required": True}
        fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=root)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(receipt, output, sort_keys=True)
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
            raw = sys.stdin.buffer.read(100 * 1024 * 1024 + 1)
            if len(raw) > 100 * 1024 * 1024:
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
