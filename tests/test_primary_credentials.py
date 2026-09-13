"""Fresh-process tests use generated synthetic values, never the real Keychain."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_bridge_consumes_keys_before_child_and_preserves_legacy_reference():
    code = '''
import os, secrets, subprocess, sys
names = ("WISP_LOCAL_OMLX_KEY", "WISP_MINI_INFERENCE_KEY", "WISP_MINI_NODE_KEY")
values = {name: secrets.token_hex(32) for name in names}
os.environ.update(values)
os.environ["LEGACY_SYNTHETIC_KEY"] = "legacy"
from service.config import credentials, omlx_api_key
from service.config.endpoints import Endpoint
assert not set(names).intersection(os.environ)
assert omlx_api_key() == values[names[0]]
credentials.reviewed_bindings = lambda: {"schema_version": 1, "host": "mini.fixture.ts.net", "node_id": "nFIXTURE"}
assert Endpoint("mini", "https://mini.fixture.ts.net", "env:" + names[1]).api_key() == values[names[1]]
assert Endpoint("mini", "https://mini.fixture.ts.net:8443", "env:" + names[2]).api_key(purpose="node", node_id="nFIXTURE") == values[names[2]]
try:
    Endpoint("mini", "https://mini.example", "env:" + names[0]).api_key()
except ValueError:
    pass
else:
    raise AssertionError("remote local-key bypass")
assert Endpoint("mini", "https://mini.example", "env:LEGACY_SYNTHETIC_KEY").api_key() == "legacy"
child = subprocess.check_output([sys.executable, "-c", "import os; print(sorted(os.environ))"])
assert all(name.encode() not in child for name in names)
assert all(value.encode() not in child for value in values.values())
print("safe")
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "safe\n"


def test_malformed_bridge_consumed_and_error_sanitized():
    code = '''
import os
os.environ["WISP_LOCAL_OMLX_KEY"] = "invalid-generated-fixture"
from service.config import omlx_api_key
assert "WISP_LOCAL_OMLX_KEY" not in os.environ
try:
    omlx_api_key()
except ValueError as error:
    assert "fixture" not in str(error)
else:
    raise AssertionError("accepted malformed bridge")
'''
    assert subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True).returncode == 0


def test_legacy_local_fallback_without_bridge(monkeypatch):
    from service import config
    from service.config import credentials
    monkeypatch.setattr(credentials, "_VALUES", {})
    monkeypatch.setattr(config, "omlx_settings", lambda: {"auth": {"api_key": "synthetic-legacy"}})
    assert config.omlx_api_key() == "synthetic-legacy"


def test_fixed_credential_role_origin_and_node_binding(monkeypatch):
    import pytest
    from service.config import credentials
    from service.config.endpoints import Endpoint
    monkeypatch.setattr(credentials, 'reviewed_bindings', lambda: {'host':'mini.fixture.ts.net','node_id':'nFIXTURE'})
    for name, purpose, origin, node in [
        ('WISP_MINI_NODE_KEY','inference','https://mini.fixture.ts.net:8443','nFIXTURE'),
        ('WISP_MINI_INFERENCE_KEY','node','https://mini.fixture.ts.net','nFIXTURE'),
        ('WISP_MINI_INFERENCE_KEY','inference','https://evil.example',None),
        ('WISP_MINI_INFERENCE_KEY','inference','https://mini.fixture.ts.net:8443',None),
        ('WISP_MINI_NODE_KEY','node','https://mini.fixture.ts.net:8443','nOTHER'),
        ('WISP_MINI_INFERENCE_KEY','inference','https://MINI.fixture.ts.net',None),
        ('WISP_MINI_INFERENCE_KEY','inference','https://mini.fixture.ts.net.',None),
        ('WISP_MINI_INFERENCE_KEY','inference','https://mini.fixture.ts.net/path',None),
    ]:
        with pytest.raises(ValueError):
            Endpoint('direct', origin, 'env:'+name).api_key(purpose=purpose, node_id=node)


def test_binding_receipt_malformed_schema_is_sanitized(tmp_path, monkeypatch):
    import json
    import pytest
    from service.config import credentials
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    directory = tmp_path/'.moe/provisioning'
    directory.mkdir(mode=0o700, parents=True)
    directory.parent.chmod(0o700)
    receipt = directory/'endpoints.json'
    for value in ([], None, {'schema_version':True, 'node_id':'nTEST'}, {'schema_version':1, 'node_id':[]}):
        receipt.write_text(json.dumps(value))
        receipt.chmod(0o600)
        with pytest.raises(ValueError, match='Reviewed credential binding unavailable'):
            credentials.reviewed_bindings()


def test_isolated_qualification_rejects_inconclusive_denials():
    import importlib.util
    from types import SimpleNamespace
    import pytest
    spec = importlib.util.spec_from_file_location("isolated_acl_fixture", ROOT / "build-support/isolated_acl_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    allowed = {b"EXPECTED_OS_DENIAL\n"}
    module.validate_outcome(SimpleNamespace(returncode=1, stdout=b"", stderr=b"EXPECTED_OS_DENIAL\n"), allowed)
    for code, stdout, stderr in [(1, b"", b"UNAVAILABLE\n"), (1, b"", b"ISOLATION_FAILURE\n"),
                                 (1, b"", b"STORE_PATH_MISMATCH\n"), (1, b"", b"STORE_FILE_MISMATCH\n"),
                                 (1, b"", b"STORE_PATH_UNAVAILABLE\n"), (1, b"", b"EXPECTED_POLICY_DENIAL\n"), (-9, b"", b""),
                                 (1, b"unexpected", b"EXPECTED_OS_DENIAL\n")]:
        with pytest.raises(RuntimeError):
            module.validate_outcome(SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr), allowed)


def test_isolated_fixture_cannot_query_ambient_credentials():
    import re
    source = (ROOT / "infra/mac-mini/IsolatedACLFixture.swift").read_text()
    calls = set(re.findall(r"BackendCredentials\.(\w+)\(", source))
    assert calls == {"trustedIdentity", "valid", "verifyItemAccess"}
    for forbidden in ("SecKeychainSetDefault", "SecKeychainSetSearchList", "SecKeychainLockAll", "SecItemDelete", "SecItemUpdate", "SecKeychainItemSetAccess", "dlsym"):
        assert forbidden not in source
    assert "kSecMatchSearchList as String: [keychain]" in source
    assert "kSecUseKeychain as String: store" in source


def test_acl_runtime_gate_requires_complete_invariant_evidence():
    import copy
    import importlib.util
    import pytest
    spec = importlib.util.spec_from_file_location("acl_runtime_gate", ROOT / "build-support/isolated_acl_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = {"default": None, "search_list": [], "login_file_metadata": {}}
    valid = {"status": "PASS", "keychain_executed": True,
             "cases": {name: "PASS" for name in module.REQUIRED_CASES},
             "ambient_unchanged": True, "temporary_keychain_deleted": True, "temporary_files_removed": True,
             "source_clean": True, "ending_clean": True, "source_sha": "a" * 40, "ending_sha": "a" * 40,
             "schema_version": 2, "automatic_acl_migration": "unsupported",
             "helper_snapshots": {"before": {"files": {"helper.json": "same"}}, "restored": {"files": {"helper.json": "same"}}},
             "recovery_phase": "helper_restored_keychain_unverified",
             "recovery_commands": {command: "BLOCKED" for command in ("status", "export-mini", "init")},
             "ambient_states": {name: copy.deepcopy(state) for name in ("before", "after_create", "after_cases", "after_cleanup")}}
    module.assert_qualified(valid)
    for key in ("keychain_executed", "ambient_unchanged", "temporary_keychain_deleted", "temporary_files_removed", "ending_clean"):
        value = copy.deepcopy(valid)
        value[key] = False
        with pytest.raises(RuntimeError):
            module.assert_qualified(value)
    for kind in ("case", "snapshot", "mutation", "head", "receipt", "recovery", "schema"):
        value = copy.deepcopy(valid)
        if kind == "case":
            value["cases"].pop(next(iter(module.REQUIRED_CASES)))
        elif kind == "snapshot":
            value["ambient_states"].pop("after_cases")
        elif kind == "mutation":
            value["ambient_states"]["after_cases"]["search_list"] = ["synthetic-store-must-not-be-listed"]
        elif kind == "receipt":
            value["helper_snapshots"]["restored"]["files"]["helper.json"] = "changed"
        elif kind == "recovery":
            value["recovery_commands"]["status"] = "READY"
        elif kind == "schema":
            value["schema_version"] = 1
        else:
            value["ending_sha"] = "b" * 40
        with pytest.raises(RuntimeError):
            module.assert_qualified(value)


def test_acl_runtime_cleans_scoped_store_after_creation_failure(tmp_path, monkeypatch):
    import importlib.util
    import json
    from types import SimpleNamespace
    import pytest
    spec = importlib.util.spec_from_file_location("acl_runtime_cleanup", ROOT / "build-support/isolated_acl_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("controller", "reader-original", "reader-replacement", "reader-unrelated"):
        (tmp_path / name).write_bytes(b"inert synthetic file")
    store = tmp_path / "synthetic.keychain-db"
    commands = []
    def fake_call(argv, **kwargs):
        command = argv[-1]
        commands.append(command)
        if command == "create":
            store.write_bytes(b"synthetic state")
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"UNAVAILABLE\n")
        assert command == "cleanup"
        assert set(json.loads(kwargs["data"])) == {"password"}
        store.unlink()
        return SimpleNamespace(returncode=0, stdout=b"fixture-original-pass\n", stderr=b"")
    monkeypatch.setattr(module, "call", fake_call)
    monkeypatch.setattr(module, "ambient_state", lambda root: {"default": None, "search_list": []})
    report = {"cases": {}}
    with pytest.raises(RuntimeError):
        module.run_qualification(tmp_path, report)
    assert commands == ["create", "cleanup"]
    assert not store.exists() and report["temporary_keychain_deleted"]
    assert not report["ambient_unchanged"] and len(report["ambient_states"]) == 3
    assert report["cases"] == {} and report.get("status") != "PASS"


def test_acl_failed_replacement_restores_pair_and_blocks_readiness(tmp_path, monkeypatch):
    import importlib.util
    import json
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location("acl_failed_replacement", ROOT / "build-support/isolated_acl_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tmp_path = tmp_path.resolve()
    for name in ("controller", "reader-original", "reader-replacement", "reader-unrelated"):
        (tmp_path / name).write_bytes(name.encode())
    monkeypatch.setattr(module, "ambient_state", lambda root: {"default": None, "search_list": []})
    observed = {}
    calls = []
    store = tmp_path / "synthetic.keychain-db"
    def fake_call(argv, **kwargs):
        if argv == ["/usr/bin/swiftc", "--version"]:
            version = json.loads((ROOT / "build-support/toolchain.json").read_text())["ci_swift"]
            return SimpleNamespace(stdout=("Apple Swift version " + version).encode())
        if argv[0] == "/usr/bin/codesign":
            return SimpleNamespace(stdout=b"")
        if argv[-1] == "protocol-version":
            return SimpleNamespace(stdout=b"wisp-mini-helper-v2")
        command = argv[-1]
        calls.append(command)
        assert command != "rebind"
        assert observed.get("password", "never-in-argv") not in " ".join(map(str, argv))
        if command == "create":
            observed.update(json.loads(kwargs["data"]))
            store.write_bytes(b"synthetic")
        elif command == "cleanup":
            assert json.loads(kwargs["data"]) == {"password": observed["password"]}
            store.unlink()
        if command == "read":
            expected = b"reader-replacement" if len(calls) == 4 else b"reader-original"
            if len(calls) != 3:
                assert Path(argv[0]).read_bytes() == expected
        denied = len(calls) in (3, 4, 7)
        return SimpleNamespace(returncode=int(denied), stdout=b"" if denied else b"fixture-original-pass\n",
                               stderr=b"EXPECTED_OS_DENIAL\n" if denied else b"")
    monkeypatch.setattr(module, "call", fake_call)
    report = {"cases": {}}
    module.run_qualification(tmp_path, report)
    assert report["cases"] == {case: "PASS" for case in module.REQUIRED_CASES}
    assert report["helper_snapshots"]["before"] == report["helper_snapshots"]["restored"]
    assert report["recovery_phase"] == "helper_restored_keychain_unverified"
    assert report["recovery_commands"] == {command: "BLOCKED" for command in ("status", "export-mini", "init")}
    assert calls[-1] == "cleanup" and not store.exists()
    assert report["temporary_keychain_deleted"] and report["ambient_unchanged"]
    assert observed["password"] not in json.dumps(report) and observed["value"] not in json.dumps(report)
