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
    assert json.loads(path.read_text()) == {"read_only": False, "full_access": True}
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
    assert json.loads(mode_file.read_text()) == {"read_only": policy.read_only(), "full_access": False}
    assert not leftover.exists()


def test_every_choice_saves_both_modes_as_they_are_in_force(mode_file, monkeypatch):
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    policy.choose_mode({"full_access": False})
    assert json.loads(mode_file.read_text()) == {"read_only": False, "full_access": False}
    policy.choose_mode({"read_only": True})
    assert json.loads(mode_file.read_text()) == {"full_access": False, "read_only": True}
    assert (policy.read_only(), policy.full_access()) == (True, False)


def test_choosing_one_mode_after_a_damaged_file_never_brings_full_access_back(tmp_path):
    """A damaged file starts the service with Full access forced off. Choosing view-only then
    used to save that one key, and the next start took Full access from policy.yaml's default."""
    path = tmp_path / "moe" / "access_mode.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    assert _boot(tmp_path)["full_access"] is False                    # forced off by the damage
    path.write_text("not json")
    after = _boot(tmp_path, {"read_only": True})
    assert after["full_access"] is False
    assert json.loads(path.read_text()) == {"read_only": True, "full_access": False}
    assert _boot(tmp_path)["full_access"] is False                    # and it stays off at the next start


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
    assert json.loads(mode_file.read_text())["full_access"] is False
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


# --- only the Wisp app can give Wisp more access --------------------------------------------

@pytest.mark.parametrize("body", [{"full_access": True}, {"read_only": False},
                                  {"full_access": True, "read_only": True}])
def test_a_caller_that_is_not_the_wisp_app_cannot_widen_access(wisp, mode_file, body):
    """Another program on this Mac (or a command the assistant runs) can reach this port. It must
    not be able to turn Full access on, or view-only off, through the service."""
    before = wisp.get("/mode").json()
    response = wisp.post("/mode", json=body)
    assert response.status_code == 403
    assert response.json()["detail"] == ("Wisp couldn't confirm this came from the Wisp app, so nothing changed. "
                                         "Quit and reopen Wisp, then try again.")
    assert wisp.get("/mode").json() == before and not mode_file.exists()


@pytest.mark.parametrize("body, key", [({"full_access": False}, "full_access"), ({"read_only": True}, "read_only")])
def test_narrowing_access_never_needs_the_proof(wisp, mode_file, monkeypatch, body, key):
    from service import main, wisp_reset

    def never(*_a, **_k):
        raise AssertionError("taking access back must not depend on proving the caller")
    monkeypatch.setattr(wisp_reset, "caller_is_parent_app", never)
    response = wisp.post("/mode", json=body)
    assert response.status_code == 200 and response.json()[key] is body[key]
    assert main is not None and wisp.get("/mode").status_code == 200      # and reading stays open


def test_the_wisp_app_itself_can_turn_full_access_on(wisp, mode_file, monkeypatch):
    from service import main
    monkeypatch.setattr(main, "_require_wisp_app", lambda request, reason: None)   # the proof passed
    wisp.post("/mode", json={"full_access": False})
    response = wisp.post("/mode", json={"full_access": True})
    assert response.status_code == 200 and response.json()["full_access"] is True
    assert json.loads(mode_file.read_text())["full_access"] is True


def test_the_proof_is_the_kernels_word_that_the_parent_app_holds_this_connection(monkeypatch):
    from fastapi import HTTPException
    from service import main, wisp_reset

    class Call:
        def __init__(self, client, server, headers=None):
            self.scope, self.headers = {"client": client, "server": server}, dict(headers or {})
    asked = []

    def proof(client, server):
        asked.append((client, server))
        return answer[0]
    monkeypatch.setattr(wisp_reset, "caller_is_parent_app", proof)
    answer = [True]
    assert main._require_wisp_app(Call(("127.0.0.1", 50111), ("127.0.0.1", 8765)), "no") is None
    assert asked == [(("127.0.0.1", 50111), ("127.0.0.1", 8765))]       # exactly this connection's two ends
    refusals = [(("127.0.0.1", 50111), ("127.0.0.1", 8765), False),     # a local program that is not the app
                (("127.0.0.1", 50111), ("127.0.0.1", 8765), 1),         # only True counts
                (("192.168.1.9", 50111), ("127.0.0.1", 8765), True),    # not loopback
                (("127.0.0.1", "50111"), ("127.0.0.1", 8765), True),    # not a real port
                (None, ("127.0.0.1", 8765), True), (("testclient", 50000), ("testserver", 80), True)]
    for client, server, said in refusals:
        answer[0] = said
        with pytest.raises(HTTPException) as refused:
            main._require_wisp_app(Call(client, server), "plain reason")
        assert refused.value.status_code == 403 and refused.value.detail == "plain reason"

    # A header that can stand in for the caller's address refuses before the proof is even asked.
    answer[0], asked[:] = True, []
    for name in wisp_reset.FORWARDED_HEADERS:
        with pytest.raises(HTTPException) as refused:
            main._require_wisp_app(Call(("127.0.0.1", 50111), ("127.0.0.1", 8765), {name: "127.0.0.1:50111"}), "no")
        assert refused.value.status_code == 403
    assert asked == []

    def broken(*_a):
        raise OSError("socket table unreadable")
    monkeypatch.setattr(wisp_reset, "caller_is_parent_app", broken)
    with pytest.raises(HTTPException):
        main._require_wisp_app(Call(("127.0.0.1", 50111), ("127.0.0.1", 8765)), "no")   # unproven is refused


# --- a forged X-Forwarded-For never stands in for the Wisp app --------------------------------

APP_PORT, OTHER_PORT = 40444, 50001


@pytest.fixture
def behind_proxy_headers(mode_file, monkeypatch, tmp_path):
    """The service as uvicorn serves it by DEFAULT (proxy headers trusted from 127.0.0.1), called
    by a local program on OTHER_PORT while the Wisp app holds a connection on APP_PORT. The proof
    is stubbed to what the kernel would say: only APP_PORT is the app's."""
    from starlette.testclient import TestClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    from service import idle, main, wisp_reset
    from service.proactive import acting
    from service.safety import grants
    asked = []

    def proof(client, server, **_kw):
        asked.append(client)
        return client == ("127.0.0.1", APP_PORT)
    monkeypatch.setattr(wisp_reset, "caller_is_parent_app", proof)
    monkeypatch.setattr(grants, "GRANTS_PATH", tmp_path / "grants.json")
    monkeypatch.setattr(grants, "_CACHE", {})
    monkeypatch.setattr(acting, "_path", lambda: tmp_path / "gate" / acting.FILE)
    monkeypatch.setattr(acting, "_revoked", False)
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    assert grants.grant("open_app", {}, decision="deny", scoped=False)["ok"] is True
    monkeypatch.setattr(policy, "_FULL_ACCESS", False)
    client = TestClient(ProxyHeadersMiddleware(main.app, trusted_hosts="127.0.0.1"),
                        base_url="http://127.0.0.1:8765", client=("127.0.0.1", OTHER_PORT),
                        raise_server_exceptions=False)
    return client, asked


def _widening_calls(client, headers):
    from service import wisp_reset
    return {
        "mode": client.post("/mode", json={"full_access": True}, headers=headers),
        "allow": client.post("/permissions", json={"tool": "run_shell", "scoped": False}, headers=headers),
        "unblock": client.delete("/permissions/open_app?scope=", headers=headers),
        "act": client.put("/assistant/proactive/act", json={"enabled": True}, headers=headers),
        # No reset phrase: even if the caller check were passed, this body resets nothing (422).
        "reset": client.post(wisp_reset.ROUTE, json={}, headers=headers),
    }


def _nothing_changed(mode_file, tmp_path):
    from service.proactive import acting
    from service.safety import grants
    assert policy.full_access() is False and not mode_file.exists()
    assert grants.check("run_shell", {"cmd": "ls"}) is None and grants.check("open_app", {"name": "Mail"}) == "deny"
    assert not acting.enabled() and not (tmp_path / "gate" / acting.FILE).exists()


def test_uvicorns_default_would_report_the_forged_port_as_the_caller():
    """The premise: with proxy headers trusted, the header's host:port becomes scope["client"]."""
    from starlette.testclient import TestClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    async def echo(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": json.dumps(list(scope["client"])).encode()})
    client = TestClient(ProxyHeadersMiddleware(echo, trusted_hosts="127.0.0.1"),
                        base_url="http://127.0.0.1:8765", client=("127.0.0.1", OTHER_PORT))
    assert client.get("/").json() == ["127.0.0.1", OTHER_PORT]
    assert client.get("/", headers={"X-Forwarded-For": f"127.0.0.1:{APP_PORT}"}).json() == ["127.0.0.1", APP_PORT]


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP", "X-Forwarded-Proto",
                                    "X-Forwarded-Host", "X-Forwarded-Port"])
def test_a_forged_forwarded_header_is_refused_on_every_app_only_route(behind_proxy_headers, mode_file, tmp_path, header):
    client, asked = behind_proxy_headers
    # Without the header the other program is simply not the app.
    for name, reply in _widening_calls(client, {}).items():
        assert reply.status_code == 403, (name, reply.status_code, reply.text)
    assert asked and all(port == OTHER_PORT for _host, port in asked)
    _nothing_changed(mode_file, tmp_path)
    # With it, naming the port of the connection the app really holds: refused before the proof
    # is asked at all, so the forged port never reaches it.
    asked.clear()
    value = f"for=127.0.0.1:{APP_PORT}" if header == "Forwarded" else f"127.0.0.1:{APP_PORT}"
    for name, reply in _widening_calls(client, {header: value}).items():
        assert reply.status_code == 403, (name, reply.status_code, reply.text)
    assert asked == []
    _nothing_changed(mode_file, tmp_path)


def test_the_apps_own_request_still_passes_and_narrowing_ignores_the_header(mode_file, monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    from service import main, wisp_reset
    monkeypatch.setattr(wisp_reset, "caller_is_parent_app",
                        lambda client, server, **_k: client == ("127.0.0.1", APP_PORT))
    monkeypatch.setattr(policy, "_FULL_ACCESS", False)
    app = TestClient(ProxyHeadersMiddleware(main.app, trusted_hosts="127.0.0.1"),
                     base_url="http://127.0.0.1:8765", client=("127.0.0.1", APP_PORT), raise_server_exceptions=False)
    # The app sends no such header: accepted, on the real route, by the real check.
    assert app.post("/mode", json={"full_access": True}).json()["full_access"] is True
    # The app's request with the header added is refused too: the header alone decides.
    assert app.post("/mode", json={"full_access": False}).status_code == 200
    assert app.post("/mode", json={"full_access": True}, headers={"X-Forwarded-For": "127.0.0.1:1"}).status_code == 403
    assert policy.full_access() is False
    # Narrowing never needed the proof, so a header changes nothing there.
    assert app.post("/mode", json={"read_only": True}, headers={"X-Forwarded-For": "10.0.0.1"}).status_code == 200


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


@pytest.mark.parametrize("cmd", [
    "mv ~/.moe/. ~/old", "mv ~/.moe// ~/old", "mv ~/.moe/./ /x", "mv ~/.moe/.//./ /x",
    "mv ~/.m* ~/old", "mv ~/.mo? ~/old", "mv ~/.[m]oe ~/old",
    "cp -R ~/staged/ ~/.moe/", "rsync -a ~/staged/ ~/.moe",
])
def test_changing_the_folder_by_glob_or_dot_asks(monkeypatch, cmd):
    """#413 audit: `~/.moe/.` and a dot-name glob that expands to it name the folder too."""
    from service.safety import grants
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(grants, "check", lambda *args: "allow")
    assert policy.shell_secret_hit({"cmd": cmd})
    assert policy.decide("shell", {"cmd": cmd}, tool="run_shell").tier is policy.Tier.CONFIRM


@pytest.mark.parametrize("cmd", [
    "cd ~/.moe && ls", "ls ~/.moe | wc -l", "ls -la ~/.moe", "du -sh ~/.moe", "ls ~/.m*",
])
def test_listing_the_folder_doesnt_ask(monkeypatch, cmd):
    """#413 audit: listing or counting what's in ~/.moe reads names, not a file's contents."""
    assert policy.shell_secret_hit({"cmd": cmd}) is None


@pytest.mark.parametrize("cmd", [
    "cd ~/.moe && cat *", "cd ~/.moe && ls & mv ~/.moe ~/x", "ls ~/.moe; mv ~/.moe ~/x",
    "ls $(mv ~/.moe ~/x)", "cd ~/.moe && cat grants.json", "ls ~/.moe > ~/out",
    "ls ~/.moe | sort -o ~/.moe", "ls ~/.moe | uniq - ~/.moe",
])
def test_a_listing_with_anything_else_still_asks(cmd):
    assert policy.shell_secret_hit({"cmd": cmd})


def test_a_file_inside_the_folder_or_its_parent_isn_t_the_folder():
    for cmd in ("touch ~/.moe/notes.txt", "mv ~/.moe/../x ~/y"):
        assert policy._moe_folder_hit(cmd) is None, cmd


def test_the_folder_pattern_is_linear_on_a_long_run_of_slashes():
    import time
    started = time.monotonic()
    policy._moe_folder_hit("mv ~/.moe" + "/" * 50_000 + "x")
    assert time.monotonic() - started < 1
