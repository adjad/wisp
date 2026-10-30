"""The user's access choice survives a restart (service/safety/policy.choose_mode).

POST /mode used to flip only the in-memory flags, so a user who turned Full
access off got it back from policy.yaml's `full_access: true` on the next
start. The choice is now saved in WISP_HOME/access_mode.json (0600) and laid
over the YAML default at import; a missing file means the YAML default, and a
file that is there but bad means Full access off (it may have been an "off").

A restart is a fresh interpreter: MOE_DIR is resolved once at import, so each
boot runs in its own process with WISP_HOME and HOME under tmp_path. Nothing
here ever touches the real ~/.moe.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from service.safety import policy

ROOT = Path(__file__).resolve().parent.parent

_BOOT = """
import json, sys
from service.safety import policy
if len(sys.argv) > 1:
    policy.choose_mode(json.loads(sys.argv[1]))
print(json.dumps({"read_only": policy.read_only(), "full_access": policy.full_access(),
                  "published": policy.publication_witness() is not None}))
"""


def _yaml_default() -> dict[str, bool]:
    cfg = policy._load_yaml()
    return {"read_only": bool(cfg.get("read_only", True)),
            "full_access": bool(cfg.get("full_access", False))}


def _boot(home: Path, choice: dict | None = None) -> dict:
    """Start the policy module in a new process, optionally choose, report the modes."""
    env = {**os.environ, "WISP_HOME": str(home / "moe"), "HOME": str(home / "user")}
    argv = [sys.executable, "-c", _BOOT] + ([json.dumps(choice)] if choice is not None else [])
    done = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    state = json.loads(done.stdout.strip().splitlines()[-1])
    assert state.pop("published") is True   # the startup proof covers the saved choice
    return state


@pytest.fixture
def mode_file(tmp_path, monkeypatch):
    """In-process: the saved choice lives under tmp_path, and the modes and the
    policy proof are restored afterwards."""
    from service.publication_authority import Authority
    path = tmp_path / "moe" / "access_mode.json"
    monkeypatch.setattr(policy, "MODE_PATH", path)
    monkeypatch.setattr(policy, "_READ_ONLY", policy._READ_ONLY)
    monkeypatch.setattr(policy, "_FULL_ACCESS", policy._FULL_ACCESS)
    authority = Authority("test-access-mode")
    authority.publish()
    monkeypatch.setattr(policy, "_publication", authority)
    monkeypatch.setattr(policy, "_proof_known", True)
    return path


# --- a restart keeps the choice ------------------------------------------------------

def test_full_access_off_survives_a_restart(tmp_path):
    default = _yaml_default()
    assert _boot(tmp_path) == default                       # fresh install: policy.yaml
    flipped = not default["full_access"]
    assert _boot(tmp_path, {"full_access": flipped})["full_access"] is flipped
    assert _boot(tmp_path) == {**default, "full_access": flipped}
    # And back again: turning it back on is a choice that sticks too.
    _boot(tmp_path, {"full_access": default["full_access"]})
    assert _boot(tmp_path) == default


def test_read_only_survives_a_restart_the_same_way(tmp_path):
    default = _yaml_default()
    flipped = not default["read_only"]
    _boot(tmp_path, {"read_only": flipped})
    assert _boot(tmp_path) == {**default, "read_only": flipped}


def test_a_missing_file_means_the_yaml_default(tmp_path):
    assert _boot(tmp_path) == _yaml_default()


@pytest.mark.parametrize("contents", [
    "",                                     # empty (a torn write)
    "not json",
    "[]",
    '{"full_access": "false"}',             # strings are not choices
    '{"full_access": 0}',
    '{"full_access": null}',
    '{"full_access": true, "disable_safety_floor": true}',   # unknown key: the whole file is refused
    "symlink",                              # a link to a valid file is refused, not followed
    "directory",
    "fifo",                                 # must not hang the start either
    "hardlink",
    "unreadable",
])
def test_a_bad_file_fails_closed(tmp_path, contents):
    """#409 audit: a saved "off" that was damaged or replaced must not fall back to
    policy.yaml's shipped `full_access: true`. read_only keeps the YAML default."""
    default = _yaml_default()
    path = tmp_path / "moe" / "access_mode.json"
    path.parent.mkdir(parents=True)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"full_access": True}))
    if contents == "symlink":
        path.symlink_to(target)
    elif contents == "directory":
        path.mkdir()
    elif contents == "fifo":
        os.mkfifo(path)
    elif contents == "hardlink":
        os.link(target, path)
    elif contents == "unreadable":
        path.write_text(json.dumps({"full_access": True}))
        path.chmod(0)
    else:
        path.write_text(contents)
    assert _boot(tmp_path) == {**default, "full_access": False}


def test_a_new_choice_replaces_a_bad_file(tmp_path):
    path = tmp_path / "moe" / "access_mode.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    _boot(tmp_path, {"full_access": True})
    assert json.loads(path.read_text()) == {"full_access": True}
    assert _boot(tmp_path)["full_access"] is True


# --- the saved file ---------------------------------------------------------------------

def test_the_saved_file_is_0600_whatever_the_umask(mode_file):
    mode_file.parent.mkdir(parents=True)
    leftover = mode_file.with_name(mode_file.name + ".tmp")
    leftover.write_text("{}")
    leftover.chmod(0o644)                   # a tmp left behind by a crash keeps its mode on O_TRUNC
    previous = os.umask(0)
    try:
        policy.choose_mode({"full_access": False})
    finally:
        os.umask(previous)
    assert stat.S_IMODE(mode_file.stat().st_mode) == 0o600
    assert json.loads(mode_file.read_text()) == {"full_access": False}
    assert not leftover.exists()


def test_choices_merge_and_only_chosen_keys_are_saved(mode_file):
    policy.choose_mode({"full_access": False})
    assert json.loads(mode_file.read_text()) == {"full_access": False}
    policy.choose_mode({"read_only": True})
    assert json.loads(mode_file.read_text()) == {"full_access": False, "read_only": True}
    assert (policy.read_only(), policy.full_access()) == (True, False)


def test_an_empty_choice_writes_nothing(mode_file):
    policy.choose_mode({})
    assert not mode_file.exists()


def test_an_unknown_key_is_refused_before_any_effect(mode_file):
    before = policy.publication_witness()
    modes = (policy.read_only(), policy.full_access())
    with pytest.raises(ValueError):
        policy.choose_mode({"full_access": not modes[1], "disable_safety_floor": True})
    assert (policy.read_only(), policy.full_access()) == modes
    assert before.current() and not mode_file.exists()


# --- the same revocation and publication path as the setters -------------------------

def test_a_choice_revokes_and_republishes_like_the_setter(mode_file):
    before = policy.publication_witness()
    flipped = not policy.full_access()
    policy.choose_mode({"full_access": flipped})
    after = policy.publication_witness()
    assert policy.full_access() is flipped
    assert before is not None and not before.current()
    assert after is not None and after.current()


def test_a_choice_during_publication_is_refused_before_any_effect(mode_file):
    from service.publication_authority import PublicationBusy, publication
    current = policy.publication_witness()
    modes = (policy.read_only(), policy.full_access())
    with publication(current):
        with pytest.raises(PublicationBusy):
            policy.choose_mode({"full_access": not modes[1]})
        assert current.current()
    assert (policy.read_only(), policy.full_access()) == modes
    assert not mode_file.exists()


def test_a_failed_save_keeps_the_change_and_says_so(mode_file):
    blocker = mode_file.parent              # a file where the folder should be
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("")
    before = policy.publication_witness()
    with pytest.raises(OSError):
        policy.choose_mode({"full_access": False})
    assert policy.full_access() is False    # turning it off takes effect regardless
    assert not before.current() and policy.publication_witness().current()


# --- only the user makes the choice ------------------------------------------------------
#
# The file decides the mode at the next start, so the model writing it would be
# Full access granting itself. It is guarded like grants.json: it asks in every
# mode and a standing grant cannot lift that. (The model's shell can't reach
# POST /mode at all: the local-service floor refuses 127.0.0.1:8765.)

@pytest.mark.parametrize("path", [str(policy.MODE_PATH), "~/.moe/access_mode.json"])
def test_the_model_cannot_write_the_choice_itself(path, monkeypatch):
    from service.safety import grants
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(grants, "check", lambda *args: "allow")
    write = policy.decide("fs_write", {"path": path, "content": '{"full_access": true}'},
                          tool="write_file")
    assert write.tier is policy.Tier.CONFIRM
    shell = policy.decide("shell", {"cmd": f"echo '{{\"full_access\": true}}' > {path}"},
                          tool="run_shell")
    assert shell.tier is policy.Tier.CONFIRM


def test_a_skill_cannot_write_the_choice_even_with_home_granted(tmp_path):
    from service.skills import sandbox
    profile = sandbox.build_profile(tmp_path, [], [Path.home()])
    assert f'(deny file-write* (subpath "{policy.MODE_PATH}"))' in profile


# --- POST /mode -------------------------------------------------------------------------

@pytest.fixture(scope="module")
def wisp():
    from starlette.testclient import TestClient
    from service import main
    return TestClient(main.app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)


def test_post_mode_saves_the_choice(wisp, mode_file):
    response = wisp.post("/mode", json={"full_access": False})
    assert response.status_code == 200 and response.json()["full_access"] is False
    assert json.loads(mode_file.read_text()) == {"full_access": False}
    assert wisp.get("/mode").json()["full_access"] is False


def test_post_mode_reports_a_choice_it_could_not_save(wisp, mode_file):
    mode_file.parent.parent.mkdir(parents=True, exist_ok=True)
    mode_file.parent.write_text("")         # the save fails
    response = wisp.post("/mode", json={"full_access": False})
    assert response.status_code == 500
    assert "restarts" in response.json()["detail"]
    # The app re-reads /mode after a failed reply and sees what is in force.
    assert wisp.get("/mode").json()["full_access"] is False


@pytest.mark.parametrize("value", ["false", 0, 1, None, [], {}])
def test_post_mode_takes_only_true_or_false(wisp, mode_file, value):
    """bool("false") is True, and the choice is saved: anything but a boolean is refused."""
    before = wisp.get("/mode").json()
    response = wisp.post("/mode", json={"full_access": value})
    assert response.status_code == 422
    assert wisp.get("/mode").json() == before and not mode_file.exists()


def test_moving_wisp_s_folder_itself_asks_in_every_mode(monkeypatch):
    """#409 audit: moving ~/.moe aside and another folder into its place swaps in a
    forged access_mode.json without naming it, so a change to the folder itself asks."""
    from service.paths import MOE_DIR
    from service.safety import grants
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(grants, "check", lambda *args: "allow")
    staged = str(MOE_DIR.parent / "staged")
    move = policy.decide("fs_write", {"source": str(MOE_DIR), "destination": str(MOE_DIR) + ".old"},
                         tool="move_file")
    assert move.tier is policy.Tier.CONFIRM
    into = policy.decide("fs_write", {"source": staged, "destination": str(MOE_DIR) + "/"}, tool="move_file")
    assert into.tier is policy.Tier.CONFIRM
    shell = policy.decide("shell", {"cmd": "mv ~/.moe ~/.moe.old && mv ~/staged ~/.moe"}, tool="run_shell")
    assert shell.tier is policy.Tier.CONFIRM
    # A file inside the folder that isn't a guarded one, and reading the folder, don't.
    note = policy.decide("fs_write", {"path": str(MOE_DIR / "notes.txt"), "content": "x"}, tool="write_file")
    assert note.tier is policy.Tier.ALLOW
    listing = policy.decide("fs_read", {"path": str(MOE_DIR)}, tool="list_directory")
    assert listing.tier is policy.Tier.ALLOW
