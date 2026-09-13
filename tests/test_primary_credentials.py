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
                                 (1, b"", b"EXPECTED_POLICY_DENIAL\n"), (-9, b"", b""),
                                 (1, b"unexpected", b"EXPECTED_OS_DENIAL\n")]:
        with pytest.raises(RuntimeError):
            module.validate_outcome(SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr), allowed)


def test_isolated_fixture_cannot_query_ambient_credentials():
    import re
    source = (ROOT / "infra/mac-mini/IsolatedACLFixture.swift").read_text()
    calls = set(re.findall(r"BackendCredentials\.(\w+)\(", source))
    assert calls == {"trustedIdentity", "valid", "verifyItemAccess"}
    for forbidden in ("SecKeychainSetDefault", "SecKeychainSetSearchList", "SecKeychainCopyDefault",
                      "SecKeychainCopySearchList", "SecKeychainLockAll", "SecItemDelete", "SecItemUpdate"):
        assert forbidden not in source
    assert "kSecMatchSearchList as String: [keychain]" in source
    assert "kSecUseKeychain as String: store" in source
