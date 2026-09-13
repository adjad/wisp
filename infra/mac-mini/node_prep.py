"""Provisioning orchestration with explicit effect boundaries and sanitized reports."""
from __future__ import annotations

import argparse
import hashlib
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
        "remote_jobs_disabled": snapshot.get("remote", {}).get("jobs_disabled") is True,
    }
    return checks


def keychain(command):
    # Persistent helper identity is required by file-based Keychain ACLs.
    # Only explicit init --live --apply installs it; read commands never build.
    directory = Path.home() / ".moe" / "provisioning"
    binary = directory / "wisp-keychain-helper"
    if command == "init":
        settings = read_json(Path.home() / ".omlx" / "settings.json")
        local = settings.get("auth", {}).get("api_key", "")
        if not isinstance(local, str) or not re.fullmatch(r"[0-9a-f]{64}", local):
            raise Refused("local_auth_migration_required")
        if not (Path("/Applications/Wisp.app")).is_dir():
            raise Refused("trusted_wisp_app_required")
        if directory.is_symlink() or binary.is_symlink():
            raise Refused("unsafe_helper_path")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
            raise Refused("unsafe_helper_permissions")
        if not binary.exists():
            with tempfile.TemporaryDirectory(prefix="helper-build-", dir=directory) as temp:
                output = str(Path(temp) / "helper")
                run(["/usr/bin/swiftc", "-parse-as-library", "-module-cache-path", str(Path(temp) / "cache"),
                     str(ROOT / "app/Sources/WispApp/BackendCredentials.swift"),
                     str(ROOT / "infra/mac-mini/keychain-helper.swift"), "-o", output])
                os.rename(output, binary)
        return run([str(binary), "init", "/Applications/Wisp.app"], data=local.encode())
    if directory.is_symlink() or binary.is_symlink() or not binary.is_file():
        raise Refused("primary_initialization_required")
    return run([str(binary), command])


def validate_bundle(path, expected_sha):
    """Validate an externally pinned, bounded archive without extracting it."""
    raw = Path(path).read_bytes()
    if len(raw) > 64 * 1024 * 1024 or not re.fullmatch(r"[0-9a-f]{64}", expected_sha or ""):
        raise Refused("invalid_bundle_pin")
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise Refused("bundle_digest_mismatch")
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            names = [m.name for m in members]
            if len(names) != len(set(names)) or len(names) > 2000:
                raise Refused("unsafe_bundle_entries")
            total = 0
            files = {}
            for member in members:
                parts = Path(member.name).parts
                if not parts or Path(member.name).is_absolute() or ".." in parts or str(Path(member.name)) != member.name or not member.isfile():
                    raise Refused("unsafe_bundle_entry")
                total += member.size
                if total > 128 * 1024 * 1024:
                    raise Refused("bundle_too_large")
                files[member.name] = archive.extractfile(member).read()
            manifest = json.loads(files["mini/bundle.json"])
            if manifest.get("schema_version") != 1:
                raise Refused("unsupported_bundle_version")
            if manifest.get("kind") != "wisp-mini-runtime" or manifest.get("base_commit") != "c23e9c9e8860222a8aa4b070364a7e17236b3ec8":
                raise Refused("unsupported_bundle_contract")
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
    return raw, manifest


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
    args = parser.parse_args(argv)
    mode = "live" if args.live else "dry-run"
    try:
        if args.fixture and (args.live or args.apply) or args.apply and not args.live:
            raise Refused("fixture_live_conflict")
        if args.command == "init-primary":
            if args.live:
                status = json.loads(keychain("init" if args.apply else "status"))
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
                    rollback(plan)
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
            raw, manifest = validate_bundle(args.bundle, args.bundle_sha256)
            checks["bundle_verified"] = True
            if args.live and args.apply:
                activate(plan, raw, manifest, args.bundle_sha256)
        elif args.command == "rollback" and args.live and args.apply:
            rollback(plan)
        emit(args.command, mode, checks, status="ready" if not args.apply else "complete", roles="local", proactive_node=False, gateway_qualification_required=True)
        return 0
    except Exception:
        # Never echo external output, exception text, plan content, or credentials.
        emit(args.command, mode, status="blocked", error="prerequisite_or_operation_failed")
        return 1

TAILSCALE = "/opt/homebrew/bin/tailscale"


def receiver_source():
    return (ROOT / "infra/mac-mini/bundle_contract.py").read_text() + "\n" + (ROOT / "infra/mac-mini/receiver.py").read_text()


def ssh(plan, source, *arguments, data=None):
    import shlex
    validate_plan(plan)
    # Each argument is quoted because SSH transports a remote shell command.
    command = " ".join(shlex.quote(s) for s in ["/usr/bin/python3", "-c", source, *arguments])
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
    remote = json.loads(ssh(plan, (ROOT / "infra/mac-mini/remote_probe.py").read_text()))
    local = probe()
    credentials = json.loads(keychain("status"))
    return {"tailscale": status, "firewall_enabled": local["firewall_enabled"],
            "omlx_loopback_only": local["omlx_loopback_only"], "config": models_config(),
            "credentials": credentials.get("credentials"), "remote": remote}


def activate(plan, raw, manifest, digest):
    import base64
    current_peer(plan)  # Verify identity again immediately before credential access.
    credentials = json.loads(keychain("export-mini"))
    if (set(credentials) != {"mini-inference", "mini-node"} or len(set(credentials.values())) != 2
            or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in credentials.values())):
        raise Refused("invalid_mini_credentials")
    assets = {"BackendCredentials.swift": (ROOT / "app/Sources/WispApp/BackendCredentials.swift").read_text(),
              **{name: (ROOT / "infra/mac-mini" / name).read_text()
                 for name in ("keychain-helper.swift", "mini-launcher.swift")}}
    payload = {"schema_version": 1, "operation": "stage", "node_id": plan["node_id"],
               "bundle_sha256": digest, "bundle": base64.b64encode(raw).decode(),
               "assets": assets, "credentials": credentials}
    result = json.loads(ssh(plan, receiver_source(),
                            data=json.dumps(payload).encode()))
    if result != {"schema_version": 1, "status": "complete", "jobs_enabled": False, "gateway_qualification_required": True}:
        raise Refused("remote_staging_incomplete")


def rollback(plan):
    current_peer(plan)
    result = json.loads(ssh(plan, receiver_source(), "rollback"))
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
