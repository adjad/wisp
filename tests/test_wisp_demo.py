"""scripts/wisp_demo.py: a fresh demo home, then the real one restored exactly.

Runs against a scratch home and a scratch UserDefaults domain; never touches ~/.moe.
"""
from __future__ import annotations

import plistlib
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import wisp_demo  # noqa: E402


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _defaults(domain: str) -> dict:
    out = subprocess.run(["defaults", "export", domain, "-"], capture_output=True).stdout
    return plistlib.loads(out) if out else {}


@pytest.fixture
def world(tmp_path):
    moe = tmp_path / ".moe"
    (moe / "cache").mkdir(parents=True)
    (moe / "engine" / "models").mkdir(parents=True)
    (moe / "google_workspace").mkdir()
    (moe / "assistant.db").write_bytes(b"real commitments")
    (moe / "facts.db").write_bytes(b"real facts")
    (moe / "profile.json").write_text('{"know_you": "done"}')
    (moe / "cache" / "mail.json").write_text("real cache")
    (moe / "config.yaml").write_text("roles: {general: demo-model}\n")
    (moe / "gmail.json").write_text("{}")
    (moe / "google_workspace" / "state.json").write_text("{}")
    (moe / "engine" / "models" / "weights.safetensors").write_bytes(b"w" * 4096)
    (moe / "engine" / "engine.pid").write_text("12345")
    if sys.platform != "darwin":
        pytest.skip("macOS defaults(1) only")
    domain = f"wisp.demo-test.{uuid.uuid4()}"
    for key in ("wisp.setupGuide.shown", "wisp.notchTip.shown"):
        if subprocess.run(["defaults", "write", domain, key, "-bool", "true"],
                          capture_output=True).returncode:
            pytest.skip("cannot write UserDefaults here (sandboxed shell)")
    demo = wisp_demo.Demo(tmp_path, domain, "WispDemoTestNoSuchApp", ())
    yield demo
    subprocess.run(["defaults", "delete", domain], capture_output=True)
    (Path.home() / "Library" / "Preferences" / f"{domain}.plist").unlink(missing_ok=True)


def test_start_gives_a_fresh_home_and_stop_restores_exactly(world):
    original_tree = _tree(world.live)
    original_defaults = _defaults(world.domain)

    world.start()
    assert world.active()
    assert _tree(world.saved).keys() >= original_tree.keys()
    fresh = _tree(world.live)
    assert "assistant.db" not in fresh and "facts.db" not in fresh
    assert "profile.json" not in fresh and "cache/mail.json" not in fresh
    assert fresh["config.yaml"] == original_tree["config.yaml"]
    assert fresh["google_workspace/state.json"] == b"{}"
    assert fresh["engine/models/weights.safetensors"] == b"w" * 4096
    assert "engine/engine.pid" not in fresh
    assert _defaults(world.domain) == {}          # onboarding runs again

    # The demo writes its own state and onboarding flags.
    (world.live / "assistant.db").write_bytes(b"demo commitments")
    (world.live / "engine" / "models" / "weights.safetensors").write_bytes(b"changed")
    subprocess.run(["defaults", "write", world.domain, "wisp.debugMode", "-bool", "true"], check=True)

    world.stop()
    assert not world.active()
    assert _tree(world.live) == original_tree
    assert _defaults(world.domain) == original_defaults
    assert (world.last / "assistant.db").read_bytes() == b"demo commitments"


def test_discard_deletes_the_demo_and_a_second_demo_replaces_the_last(world):
    world.start()
    world.stop()
    assert world.last.is_dir()
    world.start()
    (world.live / "marker").write_text("second")
    world.stop(discard=True)
    assert not world.last.exists() or not (world.last / "marker").exists()
    assert (world.live / "assistant.db").read_bytes() == b"real commitments"


def test_refusals(world):
    with pytest.raises(wisp_demo.DemoError, match="No demo"):
        world.stop()
    world.start()
    with pytest.raises(wisp_demo.DemoError, match="already running"):
        world.start()
    (world.live / wisp_demo.DEMO_MARK).unlink()          # not provably ours any more
    with pytest.raises(wisp_demo.DemoError, match="not the demo home"):
        world.stop()
    assert world.active() and (world.saved / "assistant.db").is_file()


def test_refuses_while_a_backend_port_is_listening(world):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        world.ports = (server.getsockname()[1],)
        with pytest.raises(wisp_demo.DemoError, match="Quit Wisp first"):
            world.start()
    assert not world.active() and (world.live / "assistant.db").is_file()
    assert not (world.live / wisp_demo.STATE).exists()


def test_refuses_when_the_settings_backup_is_not_written(world, monkeypatch):
    real = world._defaults
    monkeypatch.setattr(world, "_defaults", lambda *args, check=True:
                        subprocess.CompletedProcess(args, 0) if args[0] == "export" else real(*args, check=check))
    with pytest.raises(wisp_demo.DemoError, match="could not back up"):
        world.start()
    assert not world.active() and (world.live / "assistant.db").is_file()
    assert not (world.live / wisp_demo.DEFAULTS_BACKUP).exists()
    assert _defaults(world.domain)                  # nothing was cleared
