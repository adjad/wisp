"""Synthetic-only provisioning contracts. No network, Keychain or system changes."""
import base64
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra/mac-mini"
sys.path.insert(0, str(INFRA))
import node_prep as prep
import policy
import receiver
import remote_probe


@pytest.fixture
def plan():
    return prep.read_json(INFRA / "plan.example.json")


@pytest.fixture
def snapshot():
    return prep.read_json(INFRA / "preflight.fixture.json")


def bundle(tmp_path, *, member_name="mini/__init__.py", version=1, runtime=False):
    data = b'"""Synthetic inert module."""\n'
    manifest = json.loads((INFRA / "bundle-manifest.fixture.json").read_text())
    manifest["schema_version"] = version
    from bundle_contract import FILES
    contents = {name: data for name in FILES}
    contents.pop("mini/__init__.py")
    contents[member_name] = data
    if runtime:
        manifest.update(artifact_type="offline-runtime", provenance={"strict_toolchain": True, "source_commit": manifest["source_commit"]})
        contents.update({"mini/payload/" + name: b"synthetic inert fixture" for name in
                         ("keychain-helper", "mini-launcher", "venv/bin/python3", "runtime-health.py")})
    manifest["files"] = [{"path": name, "sha256": hashlib.sha256(value).hexdigest()} for name, value in contents.items()]
    path = tmp_path / "fixture.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, content in [*contents.items(), ("mini/bundle.json", json.dumps(manifest).encode())]:
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_full_fixture_pass_and_peer_real_json_shape(plan, snapshot):
    assert all(prep.preflight(snapshot, plan, policy.fragment(plan["tailnet_user"], plan["user"])).values())
    assert "Expired" not in prep.verify_peer(snapshot["tailscale"], plan)


@pytest.mark.parametrize("key,value", [("ID", "nOTHER"), ("Tags", []), ("Online", False), ("Expired", True),
                                       ("sshHostKeys", []), ("DNSName", "wrong.example.ts.net")])
def test_peer_mismatch_blocks(plan, snapshot, key, value):
    next(iter(snapshot["tailscale"]["Peer"].values()))[key] = value
    with pytest.raises(prep.Refused):
        prep.verify_peer(snapshot["tailscale"], plan)


@pytest.mark.parametrize("user", ["root", "wisp;id", "-oProxyCommand", "wisp\nroot", "autogroup:nonroot"])
def test_user_shell_injection_and_root_refused(plan, user):
    plan["user"] = user
    with pytest.raises(prep.Refused):
        prep.validate_plan(plan)


@pytest.mark.parametrize("key", ["firewall_enabled", "omlx_loopback_only", "credentials"])
def test_unknown_primary_posture_blocks(plan, snapshot, key):
    snapshot.pop(key)
    assert not all(prep.preflight(snapshot, plan, policy.fragment(plan["tailnet_user"], plan["user"])).values())


@pytest.mark.parametrize("config", [
    {"omlx": {"base_url": "http://0.0.0.0:8000"}},
    {"omlx": {"base_url": "http://127.0.0.1:8000"}, "proactive_node": {"enabled": True}},
    {"omlx": {"base_url": "http://127.0.0.1:8000"}, "inference": {"bindings": {"coding": {"endpoint": "mini"}}}},
])
def test_remote_roles_or_nonloopback_rejected(config):
    assert not prep.local_only(config)


def test_additive_render_never_deletes_and_blocks_broad_access(plan):
    existing = {"acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]}]}
    merged = policy.render(existing, plan["tailnet_user"], plan["user"])
    assert merged["acls"] == existing["acls"]
    assert policy.review(merged, plan["tailnet_user"], plan["user"])
    assert "grants" not in existing


@pytest.mark.parametrize("key,addition", [("grants", {"src": ["*"], "dst": ["tag:wisp-inference"], "ip": ["*"]}),
                                         ("ssh", {"action": "accept", "src": ["*"], "dst": ["*"], "users": ["root"]}),
                                         ("nodeAttrs", {"target": ["*"], "attr": ["funnel"]})])
def test_broader_grants_ssh_funnel_block(plan, key, addition):
    full = policy.fragment(plan["tailnet_user"], plan["user"])
    full.setdefault(key, []).append(addition)
    assert policy.review(full, plan["tailnet_user"], plan["user"])


def test_policy_exact_ip_protocol_ports_and_ssh_user(plan):
    full = policy.fragment(plan["tailnet_user"], plan["user"])
    grant = full["grants"][0]
    for source in [policy.PRIMARY, "100.64.0.20"]:
        for port in [22, 443, 8443, 8000, 8765, 8766, 80]:
            for protocol in ["tcp", "udp"]:
                allowed = source in grant["src"] and f"{protocol}:{port}" in grant["ip"]
                assert allowed == (source == policy.PRIMARY and protocol == "tcp" and port in [22, 443, 8443])
    assert full["ssh"][0]["action"] == "check"
    assert full["ssh"][0]["users"] == ["wisp"]
    assert full["sshTests"][0]["dst"] == [policy.TAG]


@pytest.mark.parametrize("config", [{"AllowFunnel": {"host:443": True}}, {"Foreground": {"x": {"AllowFunnel": {"host:443": True}}}},
                                     {"unknown": {}}, None, {"Foreground": []}])
def test_funnel_recursive_unknown_fail_closed(config):
    assert not policy.funnel_disabled(config)
    assert not remote_probe.funnel_disabled(config)


def test_probe_sanitizes_serve_urls_and_gui_jobs(monkeypatch):
    synthetic = secrets.token_hex(32)
    def command(argv):
        if "serve" in argv:
            return json.dumps({"Web": {"host:443": {"Handlers": {"/": {"Proxy": "http://user:" + synthetic + "@localhost"}}}}})
        return ""
    monkeypatch.setattr(remote_probe, "command", command)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=b'com.wisp.mini.gateway', stderr=b'')
    monkeypatch.setattr(remote_probe.subprocess, "run", run)
    result = remote_probe.probe()
    assert result["jobs_disabled"] is False
    assert "gui/" in calls[0][-1]
    assert synthetic not in json.dumps(result) and "serve" not in result


@pytest.mark.parametrize("name,version", [("../escape", 1), ("/absolute", 1), ("mini/../escape", 1), ("mini/main.py", 99)])
def test_unsafe_or_wrong_version_bundle(tmp_path, name, version):
    path, digest = bundle(tmp_path, member_name=name, version=version)
    with pytest.raises(prep.Refused):
        prep.validate_bundle(path, digest)


def test_bundle_pin_and_perfile_digest(tmp_path):
    path, digest = bundle(tmp_path)
    raw, manifest = prep.validate_bundle(path, digest)
    assert receiver.unpack(raw)
    with pytest.raises(prep.Refused):
        prep.validate_bundle(path, "0" * 64)


def test_every_fixture_command_has_zero_live_calls(plan, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(prep, "run", lambda *a, **k: pytest.fail("live command"))
    monkeypatch.setattr(prep, "keychain", lambda *a: pytest.fail("Keychain call"))
    common = ["--plan", str(INFRA / "plan.example.json"), "--policy", str(INFRA / "policy.example.json"),
              "--fixture", str(INFRA / "preflight.fixture.json")]
    path, digest = bundle(tmp_path)
    for command in ("init-primary", "policy-render", "preflight", "doctor", "rollback", "activate"):
        extra = ["--bundle", str(path), "--bundle-sha256", digest] if command == "activate" else []
        assert prep.main([command, *common, *extra]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert prep.main(["activate", *common, "--live", "--apply"]) == 1


def test_activation_identity_before_keys_and_stdin_only(plan, tmp_path, monkeypatch):
    path, digest = bundle(tmp_path, runtime=True)
    raw, manifest = prep.validate_bundle(path, digest)
    values = {k: secrets.token_hex(32) for k in ("mini-inference", "mini-node")}
    events = []
    monkeypatch.setattr(prep, "current_peer", lambda p: events.append("identity"))
    monkeypatch.setattr(prep, "keychain", lambda cmd: events.append("keychain") or json.dumps(values))
    def ssh(p, source, *args, data=None):
        assert events == ["identity", "keychain"]
        assert all(value not in source and value not in str(args) for value in values.values())
        payload = json.loads(data)
        assert payload["credentials"] == values
        assert "local-omlx" not in payload["credentials"]
        return json.dumps({"schema_version": 1, "status": "complete", "jobs_enabled": False, "gateway_qualification_required": True})
    monkeypatch.setattr(prep, "ssh", ssh)
    monkeypatch.setattr(prep, "record_binding", lambda *a: None)
    prep.activate(plan, raw, manifest, digest)


def test_external_failure_output_not_reported(monkeypatch, capsys):
    synthetic = secrets.token_hex(32)
    monkeypatch.setattr(prep, "keychain", lambda c: (_ for _ in ()).throw(prep.Refused(synthetic)))
    assert prep.main(["init-primary", "--live"]) == 1
    assert synthetic not in capsys.readouterr().out


@pytest.mark.parametrize("failure_stage", ["materialize", "health", "import-mini"])
@pytest.mark.parametrize("tamper", ["content", "symlink", "directory", "marker", "mode"])
def test_receiver_transaction_retry_and_secret_non_disclosure(tmp_path, monkeypatch, failure_stage, tamper):
    path, digest = bundle(tmp_path, runtime=True)
    monkeypatch.setattr(receiver.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(receiver, "active_jobs", lambda: set())
    secrets = {k: "a" * 64 if k == "mini-node" else "b" * 64 for k in ("mini-node", "mini-inference")}
    manifest = json.loads(receiver.unpack(path.read_bytes())["mini/bundle.json"])
    payload = {"schema_version": 1, "operation": "stage", "node_id": "nTEST", "bundle_sha256": digest,
               "bundle": base64.b64encode(path.read_bytes()).decode(), "source_commit": manifest["source_commit"], "credentials": secrets}
    failures = [failure_stage]
    def step(kind):
        if failures and failures[0] == kind:
            failures.pop()
            raise ValueError("synthetic interruption")
    original = receiver.materialize
    def materialize(*args):
        original(*args)
        step("materialize")
    monkeypatch.setattr(receiver, "materialize", materialize)
    monkeypatch.setattr(receiver, "runtime_health", lambda p: step("health"))
    monkeypatch.setattr(receiver, "execute", lambda argv, data=None: step("import-mini"))
    with pytest.raises(ValueError):
        receiver.install(payload)
    root = tmp_path / ".wisp-mini"
    assert not (root / "receipt.json").exists()
    assert not list(root.glob(".stage-*"))
    if failure_stage in ("materialize", "health"):
        assert not [p for p in root.iterdir() if p.is_dir()]
    receiver.install(payload)
    receiver.install(payload)
    receipt = json.loads((root / "receipt.json").read_text())
    assert receipt["jobs_enabled"] is False
    release = root / receipt["provisioning_id"]
    for file in root.rglob("*"):
        if file.is_file():
            assert all(value.encode() not in file.read_bytes() for value in secrets.values())
    active = {"gui/501/com.wisp.mini.node"}
    monkeypatch.setattr(receiver, "active_jobs", lambda: set(active))
    monkeypatch.setattr(receiver, "execute", lambda argv, data=None: active.remove(argv[-1]))
    receiver.rollback()
    assert not active
    binary = release / "keychain-helper"
    if tamper == "content": binary.write_text("tampered")
    if tamper == "symlink":
        binary.unlink()
        binary.symlink_to(release / "mini-launcher")
    if tamper == "directory":
        binary.unlink()
        binary.mkdir(mode=0o700)
    if tamper == "marker": (release / ".dependencies-ready").touch()
    if tamper == "mode": binary.chmod(0o777)
    with pytest.raises(ValueError):
        receiver.install(payload)


def test_rollback_checks_launchd_without_state(tmp_path, monkeypatch):
    monkeypatch.setattr(receiver.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(receiver, "active_jobs", lambda: {"gui/501/com.wisp.mini.node"})
    monkeypatch.setattr(receiver, "execute", lambda *a: pytest.fail("unowned bootout"))
    with pytest.raises(ValueError, match="ownership_unproven"):
        receiver.rollback()
    monkeypatch.setattr(receiver, "active_jobs", lambda: set())
    receiver.rollback()


def test_exact_serve_contract_rejects_other_hosts_paths_ports_backends():
    host = "fixture.tailnet.ts.net"
    valid = {"TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}}, "Web": {
        host+":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}},
        host+":8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8766"}}}}}
    assert remote_probe.serve_restricted(valid, host)
    assert remote_probe.serve_restricted({}, host)
    numeric_boolean = copy.deepcopy(valid)
    numeric_boolean["TCP"]["443"]["HTTPS"] = 1
    assert not remote_probe.serve_restricted(numeric_boolean, host)
    for changed in [str(valid).replace(host, "attacker.ts.net"), str(valid).replace("8765", "8000"),
                    str(valid).replace("'/'", "'/private'"), str(valid).replace("'443'", "'80'"),
                    str(valid).replace("127.0.0.1", "0.0.0.0")]:
        import ast
        assert not remote_probe.serve_restricted(ast.literal_eval(changed), host)


@pytest.mark.parametrize("mutation", ["digest", "mode", "link", "version", "receipt"])
def test_primary_helper_reuse_requires_verified_identity(tmp_path, monkeypatch, mutation):
    directory = tmp_path / "helper"
    directory.mkdir(mode=0o700)
    binary = directory / "wisp-keychain-helper"
    binary.write_bytes(b"synthetic")
    binary.chmod(0o700)
    receipt = directory / "helper.json"
    receipt.write_text(json.dumps({"schema_version": 2, "sources": prep.helper_sources(), "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}))
    receipt.chmod(0o600)
    monkeypatch.setattr(prep, "run", lambda argv: "wisp-mini-helper-v2\n" if argv[-1] == "protocol-version" else "")
    assert prep.verify_helper(directory) == binary
    if mutation == "digest": binary.write_bytes(b"changed")
    if mutation == "mode": binary.chmod(0o777)
    if mutation == "link":
        binary.unlink()
        binary.symlink_to(receipt)
    if mutation == "version": monkeypatch.setattr(prep, "run", lambda argv: "old")
    if mutation == "receipt": receipt.write_text('{}')
    with pytest.raises(prep.Refused):
        prep.verify_helper(directory)


@pytest.mark.parametrize("key,value", [("tests", 42), ("sshTests", {}), ("groups", []), ("acls", False), ("nodeAttrs", 0), ("tagOwners", [])])
def test_malformed_policy_schema_is_blocked(plan, key, value):
    full = policy.fragment(plan["tailnet_user"], plan["user"])
    full[key] = value
    assert policy.review(full, plan["tailnet_user"], plan["user"])
    with pytest.raises(ValueError):
        policy.render(full, plan["tailnet_user"], plan["user"])


@pytest.mark.parametrize("config", [{"AllowFunnel": False}, {"TCP": 42}, {"Web": "invalid"}])
def test_malformed_serve_schema_is_not_safe(config):
    assert not policy.funnel_disabled(config)
    assert not remote_probe.funnel_disabled(config)


def test_disabled_override_is_not_a_loaded_service(monkeypatch):
    def run(argv, **kwargs):
        if argv[-1].count("/") == 1:
            return SimpleNamespace(returncode=0, stdout=b'disabled services = { com.wisp.mini.node => true }')
        return SimpleNamespace(returncode=113, stdout=b'')
    monkeypatch.setattr(remote_probe.subprocess, "run", run)
    assert remote_probe.jobs_disabled()
    assert receiver.active_jobs() == set()


def test_rollback_does_not_claim_running_backend_cache_refreshed(monkeypatch, capsys):
    monkeypatch.setattr(prep, "current_peer", lambda p: None)
    monkeypatch.setattr(prep, "rollback", lambda p: None)
    monkeypatch.setattr(prep, "restore_primary_local", lambda: None)
    result = prep.main(["rollback", "--plan", str(INFRA / "plan.example.json"),
                        "--policy", str(INFRA / "policy.example.json"), "--live", "--apply"])
    report = json.loads(capsys.readouterr().out)
    assert result == 2 and report["status"] == "restart_required"
    assert report["remote_jobs_enabled"] is False
    assert report["primary_runtime"] == "restart_required"
