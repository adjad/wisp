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
import socket_posture
import artifact_signature
import primary_runtime


HELPER_SHA = "a" * 40


@pytest.fixture
def helper_source(monkeypatch):
    inputs = {name: (ROOT / name).read_bytes() for name in prep.HELPER_INPUTS}
    def clean(source):
        if source != HELPER_SHA:
            raise prep.Refused("synthetic_source_mismatch")
    monkeypatch.setattr(prep, "clean_source", clean)
    monkeypatch.setattr(prep, "helper_inputs", lambda source: inputs if source == HELPER_SHA else {})
    return inputs


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
    from bundle_contract import FILES, PREPARATION_FILES
    contents = {name: data for name in FILES}
    contents.pop("mini/__init__.py")
    contents[member_name] = data
    if runtime:
        manifest.update(artifact_type="offline-runtime", provenance={"strict_toolchain": True, "source_commit": manifest["source_commit"]})
        contents.update({"mini/payload/" + name: b"synthetic inert fixture" for name in
                         ("keychain-helper", "mini-launcher", "venv/bin/python3", "runtime-health.py", "provisioning/receiver.py")})
        contents.update({name: b"synthetic inert fixture" for name in PREPARATION_FILES})
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


def test_integrated_bundle_requires_every_runtime_and_preparation_member(tmp_path):
    from bundle_contract import FILES, PREPARATION_FILES, validate_contract
    path, _ = bundle(tmp_path, runtime=True)
    with tarfile.open(path) as archive:
        manifest = json.load(archive.extractfile("mini/bundle.json"))
    validate_contract(manifest)
    for missing in FILES | PREPARATION_FILES:
        changed = {**manifest, "files": [row for row in manifest["files"] if row["path"] != missing]}
        with pytest.raises(ValueError):
            validate_contract(changed)


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
    assert any("gui/" in argv[-1] for argv in calls)
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
    monkeypatch.setattr(prep.Path, "home", lambda: tmp_path)
    (tmp_path / '.moe').mkdir(mode=0o700)
    path, digest = bundle(tmp_path, runtime=True)
    raw, manifest = prep.validate_bundle(path, digest)
    values = {k: secrets.token_hex(32) for k in ("mini-inference", "mini-node")}
    events = []
    authentication = {"envelope": b"synthetic-envelope", "trust": b"synthetic-trust",
                      "trust_sha256": "c" * 64, "key_id": "synthetic", "release_sequence": 1}
    # This is the transport/order test; real cryptographic/replay adversaries
    # are separately exercised by the artifact signature tests.
    monkeypatch.setattr(artifact_signature, "verify_artifact", lambda *a, **k: events.append("publisher") or {})
    monkeypatch.setattr(artifact_signature, "consume_release", lambda *a, **k: events.append("consume"))
    monkeypatch.setattr(prep, "private_moe", lambda *a, **k: None)
    monkeypatch.setattr(prep, "clean_source", lambda source: None)
    monkeypatch.setattr(prep, "current_peer", lambda p: events.append("identity"))
    monkeypatch.setattr(prep, "keychain", lambda cmd: events.append("keychain") or json.dumps(values))
    def ssh(p, source, *args, data=None):
        assert source == "synthetic inert fixture"
        assert events == ["publisher", "identity", "consume", "keychain"]
        assert all(value not in source and value not in str(args) for value in values.values())
        payload = json.loads(data)
        assert payload["credentials"] == values
        assert "local-omlx" not in payload["credentials"]
        assert base64.b64decode(payload["publisher"]["envelope"]) == authentication["envelope"]
        return json.dumps({"schema_version": 1, "status": "complete", "jobs_enabled": False, "gateway_qualification_required": True})
    monkeypatch.setattr(prep, "ssh", ssh)
    monkeypatch.setattr(prep, "record_binding", lambda *a: None)
    monkeypatch.setattr(prep, "receiver_source", lambda: pytest.fail("mutable receiver read"))
    prep.activate(plan, raw, manifest, digest, authentication=authentication)


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
    monkeypatch.setattr(receiver, "backend_ports_silent", lambda: True)
    # Isolate legacy materialization/rollback retry behavior from the publisher
    # anti-replay layer. Real staging consumes a sequence even on later failure,
    # and requires a newly signed higher sequence for a retry.
    monkeypatch.setattr(receiver, "verify_artifact", lambda *a, **k: SimpleNamespace(release_sequence=1, statement_sha256='c' * 64))
    monkeypatch.setattr(receiver, "consume_release", lambda *a, **k: None)
    secrets = {k: "a" * 64 if k == "mini-node" else "b" * 64 for k in ("mini-node", "mini-inference")}
    manifest = json.loads(receiver.unpack(path.read_bytes())["mini/bundle.json"])
    payload = {"schema_version": 1, "operation": "stage", "node_id": "nTEST", "bundle_sha256": digest,
               "bundle": base64.b64encode(path.read_bytes()).decode(), "source_commit": manifest["source_commit"], "credentials": secrets,
               "publisher": {"envelope": base64.b64encode(b"synthetic").decode(),
                             "trust": base64.b64encode(b"synthetic").decode(),
                             "trust_sha256": "c" * 64, "key_id": "synthetic", "release_sequence": 1}}
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
    monkeypatch.setattr(receiver, "backend_ports_silent", lambda: True)
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
def test_primary_helper_reuse_requires_verified_identity(tmp_path, monkeypatch, mutation, helper_source):
    directory = tmp_path / "helper"
    directory.mkdir(mode=0o700)
    binary = directory / "wisp-keychain-helper"
    binary.write_bytes(b"synthetic")
    binary.chmod(0o700)
    receipt = directory / "helper.json"
    receipt.write_text(json.dumps({"schema_version": 3, "source_commit": HELPER_SHA, "sources": prep.helper_sources(helper_source), "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}))
    receipt.chmod(0o600)
    monkeypatch.setattr(prep, "run", lambda argv: b"wisp-mini-helper-v2\n" if argv[-1] == "protocol-version" else b"")
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


@pytest.mark.parametrize('key', ['tests', 'sshTests'])
@pytest.mark.parametrize('mutation', ['missing', 'empty', 'wildcard', 'extra', 'altered'])
def test_policy_negative_assertions_must_be_exact(plan, key, mutation):
    full = policy.fragment(plan['tailnet_user'], plan['user'])
    if mutation == 'missing': del full[key]
    elif mutation == 'empty': full[key] = []
    elif mutation == 'wildcard': full[key] = [{'src': '*', 'accept': ['*:*']}]
    elif mutation == 'extra': full[key].append({'src': 'unknown', 'deny': ['*:*']})
    else: full[key][0]['deny'] = []
    assert 'incomplete_policy_test_' + key in policy.review(full, plan['tailnet_user'], plan['user'])


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


@pytest.mark.parametrize("refresh_passes", [False, True])
def test_rollback_requires_fresh_runtime_confirmation(monkeypatch, capsys, refresh_passes):
    monkeypatch.setattr(prep, "current_peer", lambda p: None)
    monkeypatch.setattr(prep, "rollback", lambda p, **kw: None)
    def refresh(module, *, expected_source):
        assert module is prep and expected_source == HELPER_SHA
        if not refresh_passes: raise prep.Refused("synthetic_restart_refusal")
    monkeypatch.setattr(primary_runtime, "refresh", refresh)
    result = prep.main(["rollback", "--plan", str(INFRA / "plan.example.json"),
                        "--policy", str(INFRA / "policy.example.json"), "--live", "--apply", "--source-sha", HELPER_SHA])
    report = json.loads(capsys.readouterr().out)
    if refresh_passes:
        assert result == 0 and report["status"] == "complete"
        assert report["remote_jobs_enabled"] is False
        assert report["primary_runtime"] == "fresh_generation_verified"
    else:
        assert result == 1 and report["status"] == "blocked"
        assert "primary_runtime" not in report


@pytest.mark.parametrize("mutation", ["bytes", "manifest", "receiver_hash", "receiver_utf8"])
def test_receiver_authentication_precedes_credentials(plan, tmp_path, monkeypatch, mutation):
    path, digest = bundle(tmp_path, runtime=True)
    raw, manifest = prep.validate_bundle(path, digest)
    if mutation == "bytes":
        raw += b"changed"
    elif mutation == "manifest":
        manifest["source_commit"] = "f" * 40
    else:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            files = {member.name: archive.extractfile(member).read() for member in archive}
        name = "mini/payload/provisioning/receiver.py"
        files[name] = b"\xff" if mutation == "receiver_utf8" else b"changed"
        if mutation == "receiver_utf8":
            for row in manifest["files"]:
                if row["path"] == name:
                    row["sha256"] = hashlib.sha256(files[name]).hexdigest()
            files["mini/bundle.json"] = json.dumps(manifest).encode()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        raw = buffer.getvalue()
        digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(prep, "keychain", lambda *a: pytest.fail("credentials read before authentication"))
    monkeypatch.setattr(prep, "current_peer", lambda *a: pytest.fail("network before authentication"))
    with pytest.raises(prep.Refused):
        prep.activate(plan, raw, manifest, digest)


def test_bundle_validation_does_not_reopen_path(tmp_path, monkeypatch):
    path, digest = bundle(tmp_path, runtime=True)
    original = Path.read_bytes
    def read_bytes(self):
        result = original(self)
        if self == path:
            self.write_bytes(b"replacement after pinned read")
        return result
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    raw, manifest = prep.validate_bundle(path, digest)
    assert hashlib.sha256(raw).hexdigest() == digest
    assert manifest["artifact_type"] == "offline-runtime"


@pytest.mark.parametrize("mode", [0o700, 0o755])
def test_existing_moe_permission_migration_is_explicit(tmp_path, mode):
    directory = tmp_path / ".moe"
    directory.mkdir(mode=mode)
    directory.chmod(mode)
    sentinel = directory / "existing-user-file"
    sentinel.write_bytes(b"preserved")
    if mode != 0o700:
        with pytest.raises(prep.Refused):
            prep.private_moe(directory)
        assert directory.stat().st_mode & 0o777 == mode
    prep.private_moe(directory, migrate=True)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert sentinel.read_bytes() == b"preserved"


@pytest.mark.parametrize("kind", ["symlink", "file", "writable", "owner"])
def test_unsafe_moe_migration_refused(tmp_path, monkeypatch, kind):
    directory = tmp_path / ".moe"
    if kind == "symlink":
        directory.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "file":
        directory.write_text("inert")
    else:
        directory.mkdir(mode=0o700)
        if kind == "writable":
            directory.chmod(0o777)
        else:
            monkeypatch.setattr(prep.os, "getuid", lambda: directory.stat().st_uid + 1)
    with pytest.raises((prep.Refused, OSError)):
        prep.private_moe(directory, migrate=True)


@pytest.mark.parametrize("prior", ["none", "legacy", "stale"])
@pytest.mark.parametrize("failure", [None, "compile", "verify", "publish"])
def test_helper_upgrade_atomic_pair_and_real_bytes(tmp_path, monkeypatch, prior, failure, helper_source):
    import os
    directory = tmp_path / "provisioning"
    if prior != "none":
        directory.mkdir(mode=0o755)
        (directory / "wisp-keychain-helper").write_bytes(b"legacy-never-execute")
        (directory / "wisp-keychain-helper").chmod(0o755)
        (directory / "endpoints.json").write_bytes(b"preserve binding")
        if prior == "stale":
            (directory / "helper.json").write_bytes(b'{"schema_version":1}')
    old = {p.name: p.read_bytes() for p in directory.iterdir()} if directory.exists() else None
    commands = []
    def run(argv, **kwargs):
        commands.append(argv)
        if argv == ["/usr/bin/swiftc", "--version"]:
            return ("Apple Swift version " + prep.read_json(ROOT / "build-support/toolchain.json")["ci_swift"] + "\n").encode()
        if "-parse-as-library" in argv:
            if failure == "compile":
                raise prep.Refused("synthetic_compile_failure")
            Path(argv[-1]).write_bytes(b"new-authenticated-helper")
        if argv[-1] == "protocol-version":
            assert Path(argv[0]).read_bytes() == b"new-authenticated-helper"
            return b"wrong\n" if failure == "verify" else b"wisp-mini-helper-v2\n"
        return b""
    monkeypatch.setattr(prep, "run", run)
    if failure == "publish":
        def fail(*args):
            raise prep.Refused("synthetic_publish_failure")
        monkeypatch.setattr(prep, "swap_helper_directory", fail)
        if prior == "none":
            monkeypatch.setattr(prep.os, "rename", fail)
    if failure:
        with pytest.raises(prep.Refused):
            prep.prepare_helper(directory, expected_source=HELPER_SHA)
        assert ({p.name: p.read_bytes() for p in directory.iterdir()} if directory.exists() else None) == old
    else:
        binary = prep.prepare_helper(directory, expected_source=HELPER_SHA)
        assert prep.verify_helper(directory) == binary
        assert isinstance(prep.read_json(directory / "helper.json")["compiler"], str)
        if old:
            assert (directory / "endpoints.json").read_bytes() == old["endpoints.json"]
            backups = list(tmp_path.glob(".helper-previous-*"))
            assert len(backups) == 1
            assert {p.name: p.read_bytes() for p in backups[0].iterdir()} == old
    assert not any(argv[0] == str(directory / "wisp-keychain-helper") and argv[-1] != "protocol-version" for argv in commands)


@pytest.mark.parametrize("rows,stderr,code,expected", [
    (b"", b"", 0, True),
    (b"tcp4 0 0 *.8765 *.* LISTEN\n", b"", 0, False),
    (b"tcp6 0 0 ::1.8766 *.* LISTEN\n", b"", 0, False),
    (b"tcp4 0 0 127.0.0.1.8000 *.* LISTEN\n", b"", 0, True),
    (b"", b"permission denied", 0, False), (b"", b"", 1, False),
    (b"truncated", b"", 0, False)])
def test_disabled_backend_ports_fail_closed(monkeypatch, rows, stderr, code, expected):
    header = b"Active Internet connections (including servers)\nProto Recv-Q Send-Q Local Address Foreign Address (state)\n"
    def run(argv, **kwargs):
        assert argv == ["/usr/sbin/netstat", "-an", "-p", "tcp"]
        return SimpleNamespace(stdout=header + rows, stderr=stderr, returncode=code)
    monkeypatch.setattr(socket_posture.subprocess, "run", run)
    assert socket_posture.backend_ports_silent() is expected


def test_approved_serve_routes_cannot_hide_unowned_backend(plan, snapshot):
    snapshot["remote"]["backend_ports_silent"] = False
    assert not all(prep.preflight(snapshot, plan, policy.fragment(plan["tailnet_user"], plan["user"])).values())


def test_socket_inventory_failure_or_unknown_output_refused(monkeypatch):
    for value in (b"", b"unrecognized output", b"Active Internet connections (including servers)\n"):
        monkeypatch.setattr(socket_posture.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=value, stderr=b""))
        assert not socket_posture.backend_ports_silent()
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("synthetic", 10)
    monkeypatch.setattr(socket_posture.subprocess, "run", fail)
    assert not socket_posture.backend_ports_silent()


def test_receiver_refuses_live_backend_before_staging(tmp_path, monkeypatch):
    path, digest = bundle(tmp_path, runtime=True)
    raw, manifest = prep.validate_bundle(path, digest)
    monkeypatch.setattr(receiver, "active_jobs", lambda: set())
    monkeypatch.setattr(receiver, "backend_ports_silent", lambda: False)
    monkeypatch.setattr(receiver, "root_path", lambda: pytest.fail("must refuse before local mutation"))
    monkeypatch.setattr(receiver, "verify_artifact", lambda *a, **k: {})
    with pytest.raises(ValueError, match="backend_ports_must_be_silent"):
        receiver.install({"schema_version": 1, "operation": "stage", "bundle_sha256": digest,
            "bundle": base64.b64encode(raw).decode(), "source_commit": manifest["source_commit"],
            "node_id": "nTEST", "credentials": {"mini-inference": "a" * 64, "mini-node": "b" * 64},
            "publisher": {"envelope": "", "trust": "", "trust_sha256": "c" * 64,
                          "key_id": "synthetic", "release_sequence": 1}})


def test_invalid_local_auth_does_not_replace_helper(tmp_path, monkeypatch, helper_source):
    monkeypatch.setattr(prep.Path, "home", lambda: tmp_path)
    original = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda p: True if str(p) == "/Applications/Wisp.app" else original(p))
    monkeypatch.setattr(prep, "run", lambda *a, **k: b"")
    monkeypatch.setattr(prep, "read_json", lambda *a: {"auth": {"api_key": "invalid"}})
    monkeypatch.setattr(prep, "prepare_helper", lambda *a: pytest.fail("must not replace helper before validation"))
    with pytest.raises(prep.Refused, match="local_auth_migration_required"):
        prep.keychain("init", expected_source=HELPER_SHA)


def test_record_binding_holds_upgrade_lock(tmp_path, plan, monkeypatch):
    from contextlib import contextmanager
    directory = tmp_path / ".moe/provisioning"
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    monkeypatch.setattr(prep.Path, "home", lambda: tmp_path)
    locked = []
    @contextmanager
    def lock(parent):
        assert parent == directory.parent
        locked.append(True)
        yield
        assert prep.read_json(directory / "endpoints.json")["host"] == plan["host"]
        locked.pop()
    monkeypatch.setattr(prep, "provisioning_lock", lock)
    monkeypatch.setattr(prep, "verify_helper", lambda p: locked or pytest.fail("unlocked verify"))
    prep.record_binding(plan, {"source_commit": "a" * 40}, "b" * 64)
    assert not locked


def test_packaged_bootstrap_runs_without_repository_imports(tmp_path):
    source = prep.receiver_source()
    # Non-main execution loads definitions only, never probes the system.
    namespace = {"__name__": "synthetic_fixture"}
    exec(compile(source, "authenticated-receiver", "exec"), namespace)
    assert callable(namespace["install"]) and callable(namespace["backend_ports_silent"])


@pytest.mark.parametrize("prior", [False, True])
@pytest.mark.parametrize("failure", ["init", "response", "acl", "status"])
def test_helper_acceptance_failure_restores_exact_prior(tmp_path, monkeypatch, prior, failure, helper_source):
    directory = tmp_path / "provisioning"
    if prior:
        directory.mkdir(mode=0o700)
        for name, data, mode in [("wisp-keychain-helper", b"old signed helper", 0o700),
                                 ("helper.json", b'{"legacy":true}', 0o600),
                                 ("endpoints.json", b"unchanged receipt", 0o600)]:
            (directory / name).write_bytes(data)
            (directory / name).chmod(mode)
    previous = prep.helper_snapshot(directory) if prior else None
    protocol_calls = []
    synthetic_partial_store = {}
    def run(argv, **kwargs):
        if argv == ["/usr/bin/swiftc", "--version"]:
            return ("Apple Swift version " + prep.read_json(ROOT / "build-support/toolchain.json")["ci_swift"]).encode()
        if "-parse-as-library" in argv:
            Path(argv[-1]).write_bytes(b"new signed helper")
        if argv[-1] == "protocol-version":
            protocol_calls.append(argv[0])
            return b"wrong" if failure == "acl" and Path(argv[0]).parent == directory else b"wisp-mini-helper-v2\n"
        if "init" in argv:
            assert (directory / "wisp-keychain-helper").read_bytes() == b"new signed helper"
            if failure == "init":
                synthetic_partial_store["new-entry"] = "synthetic-only"
                raise prep.Refused("synthetic_storage_failure")
            return b"invalid" if failure == "response" else b'{"credentials":"ready"}'
        if argv[-1] == "status":
            return b'{"credentials":"missing"}'
        return b""
    monkeypatch.setattr(prep, "run", run)
    with pytest.raises(prep.Refused):
        prep.prepare_helper(directory, expected_source=HELPER_SHA, accept=lambda binary: prep.accept_primary_helper(binary, "a" * 64))
    assert (prep.helper_snapshot(directory) if directory.exists() else None) == previous
    rejected = list(tmp_path.glob(".helper-previous-*"))
    assert len(rejected) == 1
    assert (rejected[0] / "wisp-keychain-helper").read_bytes() == b"new signed helper"
    assert prep.read_json(tmp_path / ".helper-transaction.json")["phase"] == "helper_restored_keychain_unverified"
    if failure == "init":
        assert synthetic_partial_store == {"new-entry": "synthetic-only"}
    assert all(Path(p).name == "wisp-keychain-helper" for p in protocol_calls)


def test_failed_restoration_retains_journal_and_blocks_use(tmp_path, monkeypatch, helper_source):
    directory = tmp_path / ".moe/provisioning"
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    (directory / "wisp-keychain-helper").write_bytes(b"old")
    (directory / "wisp-keychain-helper").chmod(0o700)
    previous = prep.helper_snapshot(directory)
    def run(argv, **kwargs):
        if argv[-1] == "--version":
            return ("Swift version " + prep.read_json(ROOT / "build-support/toolchain.json")["ci_swift"]).encode()
        if "-parse-as-library" in argv:
            Path(argv[-1]).write_bytes(b"new")
        return b"wisp-mini-helper-v2" if argv[-1] == "protocol-version" else b""
    monkeypatch.setattr(prep, "run", run)
    original_swap = prep.swap_helper_directory
    count = []
    def swap(*args):
        count.append(1)
        if len(count) == 2:
            raise prep.Refused("synthetic_restore_failure")
        original_swap(*args)
    monkeypatch.setattr(prep, "swap_helper_directory", swap)
    def reject(binary):
        raise prep.Refused("synthetic_accept_failure")
    with pytest.raises(prep.Refused, match="helper_recovery_required"):
        prep.prepare_helper(directory, expected_source=HELPER_SHA, accept=reject)
    journal = prep.read_json(directory.parent / ".helper-transaction.json")
    assert prep.helper_snapshot(directory.parent / journal["slot"]) == previous
    monkeypatch.setattr(prep.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(prep, "run", lambda *a, **k: pytest.fail("recovery marker must block execution"))
    for command in ("status", "export-mini", "init"):
        with pytest.raises(prep.Refused, match="helper_recovery_required"):
            prep.keychain(command)


@pytest.mark.parametrize("failure", [None, "pin", "head", "tracked", "untracked", "changed_head"])
def test_rollback_uses_clean_exact_git_source(plan, monkeypatch, failure):
    sha = "a" * 40
    reads = []
    def run(argv, **kwargs):
        assert argv[:3] == ["/usr/bin/git", "-C", str(ROOT)]
        if "rev-parse" in argv:
            reads.append("head")
            return ("b" * 40 if failure == "head" or failure == "changed_head" and len(reads) > 1 else sha).encode()
        if "status" in argv:
            return {"tracked": b" M source", "untracked": b"?? rogue"}.get(failure, b"")
        assert argv[-2] == "show" and argv[-1].startswith(sha + ":infra/mac-mini/")
        return ("# authenticated " + argv[-1]).encode()
    monkeypatch.setattr(prep, "run", run)
    monkeypatch.setattr(prep, "receiver_source", lambda: pytest.fail("mutable source"))
    monkeypatch.setattr(prep, "current_peer", lambda *a: None)
    def ssh(plan, source, *args):
        assert failure is None
        assert source.count("# authenticated") == 4 and args == ("rollback",)
        return json.dumps({"schema_version": 1, "status": "complete", "jobs_enabled": False, "gateway_qualification_required": True})
    monkeypatch.setattr(prep, "ssh", ssh)
    if failure:
        with pytest.raises(prep.Refused):
            prep.rollback(plan, expected_source=None if failure == "pin" else sha)
    else:
        prep.rollback(plan, expected_source=sha)


@pytest.mark.parametrize("listing,complete,absent", [
    ("ALF: total number of apps = 0\n", True, True),
    ("ALF: total number of apps = 1\n1 : /Applications/Wisp.app\n ( Block incoming connections )\n", True, True),
    ("ALF: total number of apps = 1\n1 : /opt/homebrew/bin/python3\n ( Allow incoming connections )\n", True, False),
    ("ALF: total number of apps = 1\n1 : /Applications/oMLX.app\n ( Allow incoming connections )\n", True, False),
    ("ALF: total number of apps = 1\n1 : /Users/wisp/.wisp-mini/mini-launcher\n ( Allow incoming connections )\n", True, False),
    ("", False, False),
    ("unknown localized output", False, False),
    ("ALF: total number of apps = 1\n", False, False),
    ("ALF: total number of apps = 0\nunparsed service exception", False, False),
    ("ALF: total number of apps = 1\n2 : /Applications/Wisp.app\n ( Block incoming connections )\n", False, False),
    ("ALF: total number of apps = 1\n1 : /Applications/Wisp.app\n ( Unknown incoming connections )\n", False, False),
])
def test_firewall_inventory_is_complete_and_rejects_all_inbound_exceptions(listing, complete, absent):
    result = remote_probe.firewall_inventory(listing,
        "Automatically allow built-in signed software DISABLED",
        "Automatically allow downloaded signed software DISABLED")
    assert result == {"schema_version": 1, "complete": complete, "exceptions_absent": absent}
    assert "/Applications" not in json.dumps(result)


@pytest.mark.parametrize("signed,signed_app", [
    ("Automatically allow built-in signed software ENABLED", "Automatically allow downloaded signed software DISABLED"),
    ("Automatically allow built-in signed software DISABLED", "Automatically allow downloaded signed software ENABLED"),
    ("", "Automatically allow downloaded signed software DISABLED"),
    ("Automatically allow built-in signed software DISABLED", "unknown"),
])
def test_firewall_automatic_allow_or_unknown_is_not_safe(signed, signed_app):
    assert remote_probe.firewall_inventory("ALF: total number of apps = 0\n", signed, signed_app) == {
        "schema_version": 1, "complete": True, "exceptions_absent": False}


@pytest.mark.parametrize("name,uid,groups,member,accepted", [
    ("wisp", "501", "20 80", "user is a member of the group", True),
    ("root", "0", "0 80", "user is a member of the group", False),
    ("wisp", "0", "20 80", "user is a member of the group", False),
    ("wisp", "-1", "20 80", "user is a member of the group", False),
    ("wisp", "501", "20", "user is a member of the group", False),
    ("wisp", "501", "20 80", "user is not a member of the group", False),
    ("wisp", "501", "20 80", "", False),
    ("wisp;id", "501", "20 80", "user is a member of the group", False),
    ("wisp", "501", "20 80 unknown", "user is a member of the group", False),
])
def test_remote_account_requires_nonroot_uid_and_two_admin_checks(monkeypatch, name, uid, groups, member, accepted):
    def command(argv):
        if argv[:1] == ["/usr/bin/id"]:
            return {"-un": name, "-u": uid, "-G": groups}[argv[1]]
        assert argv == ["/usr/bin/dsmemberutil", "checkmembership", "-U", name, "-G", "admin"]
        return member
    monkeypatch.setattr(remote_probe, "command", command)
    result = remote_probe.account_posture()
    assert (result["name"] == "wisp" and result["uid"] > 0 and result["administrator"] is True) is accepted


@pytest.mark.parametrize("field,value", [
    ("name", "other"), ("name", "root"), ("uid", 0), ("uid", -1), ("uid", True),
    ("uid", "501"), ("administrator", False), ("administrator", 1),
    ("schema_version", True), ("schema_version", 1.0), ("unexpected", True),
])
def test_preflight_remote_account_strict_schema(plan, snapshot, field, value):
    snapshot["remote"]["account"][field] = value
    checks = prep.preflight(snapshot, plan, policy.fragment(plan["tailnet_user"], plan["user"]))
    assert checks["remote_account_admin_nonroot"] is False


@pytest.mark.parametrize("value", [None, [], "unknown", {}])
def test_preflight_missing_or_malformed_account_is_not_qualified(plan, snapshot, value):
    snapshot["remote"]["account"] = value
    checks = prep.preflight(snapshot, plan, policy.fragment(plan["tailnet_user"], plan["user"]))
    assert checks["remote_account_admin_nonroot"] is False


@pytest.mark.parametrize("value", [2.0, "2", True, None])
def test_remote_preflight_version_requires_exact_integer(plan, snapshot, value):
    snapshot["remote"]["schema_version"] = value
    checks = prep.preflight(snapshot, plan, policy.fragment(plan["tailnet_user"], plan["user"]))
    assert checks["remote_account_admin_nonroot"] is False


@pytest.mark.parametrize("location", ["primary", "remote"])
@pytest.mark.parametrize("value", [None, {}, {"schema_version": 1, "complete": 1, "exceptions_absent": True},
    {"schema_version": True, "complete": True, "exceptions_absent": True},
    {"schema_version": 1, "complete": True, "exceptions_absent": False}])
def test_preflight_firewall_unknown_or_numeric_booleans_refused(plan, snapshot, location, value):
    target = snapshot if location == "primary" else snapshot["remote"]
    target["firewall_inventory"] = value
    checks = prep.preflight(snapshot, plan, policy.fragment(plan["tailnet_user"], plan["user"]))
    assert checks[location + "_inbound_exceptions_absent"] is False
