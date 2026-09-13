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
for name in names[1:]:
    assert Endpoint("mini", "https://mini.example", "env:" + name).api_key() == values[name]
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
