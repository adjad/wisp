"""Demo mode's switch in the service (service/stage_mode.py and the /stage_mode routes): memory
only, off at start, the Wisp app's alone to turn on, and over by itself after four hours."""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from service import main, stage_mode

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def off_before_and_after(monkeypatch):
    monkeypatch.setattr(stage_mode, "_since", None)
    monkeypatch.setattr(stage_mode, "_listeners", [])
    yield
    stage_mode._since = None


@pytest.fixture
def wisp():
    return TestClient(main.app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)


def test_it_starts_off_and_lives_only_in_memory():
    assert stage_mode.active() is False and stage_mode.holds_background() is False
    assert stage_mode.state() == {"enabled": False, "remaining_s": 0, "max_s": 4 * 60 * 60}
    source = (ROOT / "service" / "stage_mode.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.split('"""')[2].splitlines() if not line.lstrip().startswith("#"))
    for word in ("open(", "write_", "MOE_DIR", "moe_dir", "json.dump", "os.environ", "config"):
        assert word not in code, word                 # no file, no saved setting, no environment switch


def test_on_then_off_and_it_ends_by_itself_after_four_hours():
    assert stage_mode.set_active(True, now=1000.0) == {"enabled": True, "remaining_s": 14400, "max_s": 14400}
    assert stage_mode.active(now=1000.0 + 14399)
    assert stage_mode.state(now=1000.0 + 3600)["remaining_s"] == 10800
    assert stage_mode.active(now=1000.0 + 14400) is False              # the backstop: four hours
    assert stage_mode.state(now=1000.0 + 14400) == {"enabled": False, "remaining_s": 0, "max_s": 14400}
    stage_mode.set_active(True)
    assert stage_mode.active() and stage_mode.holds_background()         # the real clock: just turned on
    assert stage_mode.set_active(False)["enabled"] is False and stage_mode.active() is False
    assert stage_mode.holds_background() is False
    with pytest.raises(ValueError):
        stage_mode.set_active("yes")


def test_pushing_on_again_never_extends_the_four_hours():
    stage_mode.set_active(True, now=1000.0)
    stage_mode.set_active(True, now=1000.0 + 7200)                      # the app pushes it again
    assert stage_mode.state(now=1000.0 + 7200)["remaining_s"] == 7200
    # After it has run out, turning it on is a new start.
    assert stage_mode.set_active(True, now=1000.0 + 20000)["remaining_s"] == 14400


def test_listeners_hear_each_change_once_and_can_never_break_the_switch():
    heard = []
    stage_mode.on_change(heard.append)
    stage_mode.on_change(heard.append)                                   # registered once

    def broken(_on):
        raise RuntimeError("warm-up failed")
    stage_mode.on_change(broken)
    assert stage_mode.set_active(True)["enabled"] is True
    stage_mode.set_active(True)                                          # no change: not told again
    stage_mode.set_active(False)
    assert heard == [True, False]


def test_only_the_wisp_app_can_turn_it_on_and_anyone_can_turn_it_off(wisp, monkeypatch):
    assert wisp.get("/stage_mode").json()["enabled"] is False
    refused = wisp.put("/stage_mode", json={"enabled": True})            # a local program that is not the app
    assert refused.status_code == 403 and "couldn't confirm this came from the Wisp app" in refused.json()["detail"]
    assert stage_mode.active() is False
    for bad in ({"enabled": "true"}, {"enabled": 1}, {}):
        assert wisp.put("/stage_mode", json=bad).status_code == 422
    real = main._require_wisp_app
    monkeypatch.setattr(main, "_require_wisp_app", lambda request, reason: None)   # the app's own request
    from service import stage_warm
    monkeypatch.setattr(stage_warm, "install", lambda client: None)      # no model is loaded in a test
    assert wisp.put("/stage_mode", json={"enabled": True}).json()["enabled"] is True
    assert wisp.get("/stage_mode").json()["enabled"] is True
    monkeypatch.setattr(main, "_require_wisp_app", real)                 # any other local program again
    off = wisp.put("/stage_mode", json={"enabled": False})               # off needs no proof
    assert off.status_code == 200 and off.json()["enabled"] is False and stage_mode.active() is False


def test_a_warm_up_that_cannot_start_never_blocks_the_switch(wisp, monkeypatch):
    monkeypatch.setattr(main, "_require_wisp_app", lambda request, reason: None)
    from service import stage_warm

    def broken(client):
        raise RuntimeError("no engine")
    monkeypatch.setattr(stage_warm, "install", broken)
    assert wisp.put("/stage_mode", json={"enabled": True}).json()["enabled"] is True


def test_this_file_is_in_the_simulation_manifest():
    manifest = (ROOT / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_stage_mode.py"' in manifest
