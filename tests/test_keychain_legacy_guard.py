"""No test can reach the real login Keychain through the pre-rename lookup.

Where the app was renamed, the current Keychain name differs from the old one, and a
test that fakes only the current read used to send keychain_legacy to the real
`security` (and copy a real key). Here the names are made to differ on purpose, the
suites' own way of faking is used, and the only `security` calls allowed are fakes.
Synthetic only."""
import subprocess

import pytest

from service.config import keychain_legacy as legacy
from service.config import provider_credentials as secrets
from service.config.endpoints import ProviderCredentialMissing
from service.study import connector as C


def test_conftest_replaces_the_launcher_for_every_test():
    assert legacy.LAUNCH is not legacy._real_launch


def test_the_real_launcher_refuses_under_pytest(monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("a real security process was started")
    monkeypatch.setattr(subprocess, "run", must_not_run)
    with pytest.raises(legacy.RealKeychainRefused):
        legacy._real_launch(["/usr/bin/security", "find-generic-password", "-s", "x", "-a", "y"])


def test_children_a_test_starts_inherit_the_off_switch():
    import os
    assert os.environ.get("WISP_KEYCHAIN_LEGACY") == "off"


def test_explicit_env_children_and_smokes_carry_the_off_switch():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert (root / "tests/test_wisp_reset.py").read_text().count('"WISP_KEYCHAIN_LEGACY": "off"') == 2
    assert 'WISP_KEYCHAIN_LEGACY="off"' in (root / "build-support/pipeline.py").read_text()


def test_clearing_the_pytest_marker_still_refuses(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    with pytest.raises(legacy.RealKeychainRefused):
        legacy._real_launch(["/usr/bin/security", "help"])


def test_the_off_switch_refuses_outside_pytest(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("WISP_KEYCHAIN_LEGACY", "off")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    with pytest.raises(legacy.RealKeychainRefused):
        legacy._real_launch(["/usr/bin/security", "help"])


@pytest.fixture
def renamed(monkeypatch, tmp_path):
    """The renamed app's shape: current names differ from the pre-rename ones."""
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(C, "SERVICE", "com.renamed.study")
    monkeypatch.setattr(secrets, "SERVICE", "com.renamed.inference")
    monkeypatch.setattr(secrets, "_RECENT_FAILURE", {})
    monkeypatch.setattr(secrets, "_RECENT_MISSING", set())
    monkeypatch.setattr(secrets, "_RECENT_SUCCESS", {})
    monkeypatch.setattr(secrets, "_reuse_enabled", lambda: False)
    monkeypatch.setattr(secrets.quarantine, "check", lambda: None)
    C.forget_key()
    monkeypatch.setattr(C, "_cloud_target", lambda: None)
    calls = []
    real_default = legacy.LAUNCH            # conftest's fake: no old item

    def recorded(argv, **kwargs):
        calls.append(list(argv))
        return real_default(argv, **kwargs)
    monkeypatch.setattr(legacy, "LAUNCH", recorded)
    yield calls
    C.forget_key()


def test_a_study_suite_faking_only_its_own_read_never_reaches_keychain(renamed, monkeypatch):
    # tests/test_study_connector.py's way: only connector._security is faked.
    monkeypatch.setattr(C, "_security", lambda args: subprocess.CompletedProcess(args, 44, b"", b"not found"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("real run")))
    assert C._key_exists("openrouter") is False
    assert C._keychain_key("openrouter") is None
    assert renamed and all(a[:2] == ["/usr/bin/security", "find-generic-password"] for a in renamed)
    assert not any("-w" in a for a in renamed)          # only "is there one?", answered by the fake


def test_a_cloud_suite_faking_only_its_own_read_never_reaches_keychain(renamed, monkeypatch):
    def current_read_only(argv, **kwargs):
        assert "com.renamed.inference" in argv, "only the current-name read is faked here"
        return subprocess.CompletedProcess(argv, 44, b"", b"not found")
    monkeypatch.setattr(subprocess, "run", current_read_only)
    with pytest.raises(ProviderCredentialMissing):
        secrets.resolve_keychain("cloud", "https://example-provider.test")
    assert renamed and not any("-w" in a or a[1:2] == ["-i"] for a in renamed)


# --- every child a test or build smoke starts with its own environment ----------------

def _root():
    from pathlib import Path
    return Path(__file__).resolve().parents[1]


def test_simulation_qa_children_get_the_off_switch(tmp_path):
    from scripts import run_simulation_qa as simqa
    assert simqa._child_environment(tmp_path)["WISP_KEYCHAIN_LEGACY"] == "off"


def test_build_pipeline_children_get_the_off_switch():
    import sys
    sys.path.insert(0, str(_root() / "build-support"))
    try:
        import pipeline
    finally:
        sys.path.pop(0)
    assert pipeline.clean_env()["WISP_KEYCHAIN_LEGACY"] == "off"


def test_every_explicit_backend_environment_carries_the_off_switch():
    """A file that starts the service (service.main or uvicorn) and builds the child's
    environment from scratch must set the switch; inheriting os.environ gets conftest's."""
    import re
    root = _root()
    files = [*root.glob("tests/*.py"), *root.glob("tests/build_pipeline/*.py"),
             *root.glob("build-support/**/*.py"), *root.glob("build-support/**/*.swift"),
             *root.glob("tests/*.swift")]
    starts_service = re.compile(r"service\.main|uvicorn")
    from_scratch = re.compile(r'env\s*=\s*\{\s*"PATH"|environment\s*=\s*\[|env=\{"PATH"|"HOME": str\(fake_home\)')
    # Files that match the pattern but start no real service with that environment.
    not_a_backend = {
        "tests/test_release_performance.py",   # stubs service.main; its own envs start only `id`
    }
    missing = []
    for path in files:
        if path.name == "test_keychain_legacy_guard.py" or str(path.relative_to(root)) in not_a_backend:
            continue
        text = path.read_text(errors="ignore")
        if starts_service.search(text) and from_scratch.search(text) and "WISP_KEYCHAIN_LEGACY" not in text:
            missing.append(str(path.relative_to(root)))
    assert missing == [], f"service launched with an explicit environment and no off switch: {missing}"


def test_the_named_launchers_set_it():
    root = _root()
    assert '"WISP_KEYCHAIN_LEGACY": "off"' in (root / "tests/test_backend_identity_and_sandbox_isolation.py").read_text()
    assert '"WISP_KEYCHAIN_LEGACY": "off"' in (root / "build-support/managed_live_qa/native_main.swift").read_text()
