"""run_shell's kernel write guard for Wisp's own folder (service/safety/shell_sandbox.py).

Every command here runs for real, against a Wisp folder and a home that live in a temp
directory made for this file; the real ~/.moe and the real Keychains are never named. The
real-process tests need to apply a sandbox, which macOS refuses inside another sandbox
(Simulation QA, a sandboxed reviewer): there they are skipped and only the profile-text and
wiring tests run.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_scratch = tempfile.TemporaryDirectory(prefix="wisp-shell-sandbox-")
_TOP = Path(os.path.realpath(_scratch.name))
# The Wisp folder sits two folders down, so a parent can be renamed in a test. The `world`
# fixture points service.paths.MOE_DIR at it (the suite has already fixed WISP_HOME by now).
_WISP = _TOP / "outer" / "inner" / "wisp-home"

from service.safety import grants, policy, shell_sandbox
from service.tools import builtin

GATE = _WISP / "act_for_me.json"
BEFORE = '{"on": false}'
real = pytest.mark.skipif(not shell_sandbox.usable(),
                          reason="a sandbox cannot be applied inside another sandbox")


@pytest.fixture
def world(monkeypatch):
    """Full access, a fake home, and a fresh gate file that says no."""
    home = _TOP / "home"
    (home / "Library" / "Keychains").mkdir(parents=True, exist_ok=True)
    (_TOP / "elsewhere").mkdir(exist_ok=True)
    _WISP.mkdir(parents=True, exist_ok=True)
    GATE.write_text(BEFORE)
    (_WISP / "notes.txt").write_text("readable")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr("service.paths.MOE_DIR", _WISP)
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(policy, "_KEEP_FLOOR", True)
    monkeypatch.setattr(grants, "check", lambda *a, **k: None)
    return home


def _py(code: str) -> str:
    return shlex.join([sys.executable, "-I", "-c", code])


# The spellings the policy's text check cannot read: the name is put together at run time.
RUN_TIME_SPELLINGS = {
    "a shell variable": f'd={shlex.quote(str(_WISP))}; n=act_for; echo forged > "$d/${{n}}_me.json"',
    "an interpreter": _py(f"import os; open(os.path.join({str(_WISP)!r}, 'act_' + 'for_me.json'), 'w').write('forged')"),
    "find's own matching": f"find {shlex.quote(str(_WISP))} -name 'act*' -exec sh -c 'echo forged > \"$1\"' _ {{}} \\;",
    "ANSI-C quoting": f"echo forged > {shlex.quote(str(_WISP))}/$'\\x61ct_for_me.json'",
}


@real
@pytest.mark.parametrize("how", sorted(RUN_TIME_SPELLINGS))
def test_no_spelling_can_write_the_consent_file_under_full_access(world, how):
    out = builtin.run_shell(RUN_TIME_SPELLINGS[how])
    assert GATE.read_text() == BEFORE, (how, out)
    assert sorted(p.name for p in _WISP.iterdir()) == ["act_for_me.json", "notes.txt"], (how, out)


@real
@pytest.mark.parametrize("how,cmd", [
    ("a new file", f"echo x > {_WISP}/grants.json"),
    ("a new folder", f"mkdir {_WISP}/skills"),
    ("delete", f"rm -f {GATE}"),
    ("move it out", f"mv {GATE} {_TOP}/elsewhere/"),
    ("move one in", f"echo forged > {_TOP}/elsewhere/f && mv {_TOP}/elsewhere/f {GATE}"),
    ("chmod", f"chmod 777 {GATE}"),
    ("from inside, by a relative name", f"cd {_WISP} && echo forged > act_for_me.json"),
    ("through ..", f"echo forged > {_WISP}/../wisp-home/act_for_me.json"),
    ("another letter case", f"echo forged > {_WISP.parent}/WISP-HOME/ACT_FOR_ME.JSON"),
    ("the Data volume spelling", f"echo forged > /System/Volumes/Data{GATE}"),
    ("through a symlink", f"ln -s {_WISP} {_TOP}/elsewhere/link && echo forged > {_TOP}/elsewhere/link/act_for_me.json"),
    ("through a hard link", f"ln {GATE} {_TOP}/elsewhere/hard && echo forged > {_TOP}/elsewhere/hard"),
    ("later, in the background", f"(sleep 0.3; echo forged > {GATE}) >/dev/null 2>&1 & wait"),
    ("a file descriptor on the folder", _py(
        f"import os; d = os.open({str(_WISP)!r}, os.O_RDONLY); "
        "os.open('act_for_me.json', os.O_WRONLY | os.O_TRUNC, dir_fd=d)")),
])
def test_the_folder_cannot_be_changed_any_other_way(world, how, cmd):
    mode = GATE.stat().st_mode
    out = builtin.run_shell(cmd)
    assert GATE.read_text() == BEFORE and GATE.stat().st_mode == mode, (how, out)
    assert sorted(p.name for p in _WISP.iterdir()) == ["act_for_me.json", "notes.txt"], (how, out)
    assert not (_TOP / "elsewhere" / "act_for_me.json").exists(), (how, out)


@real
@pytest.mark.parametrize("folder", [_WISP, _WISP.parent, _WISP.parent.parent])
def test_neither_the_folder_nor_a_parent_can_be_renamed_away(world, folder):
    """Renaming a parent would leave Wisp's path pointing at a folder anyone can fill."""
    aside = _TOP / "elsewhere" / "aside"
    out = builtin.run_shell(f"mv {folder} {aside} && mkdir -p {_WISP} && echo forged > {GATE}")
    assert folder.is_dir() and not aside.exists(), out
    assert GATE.read_text() == BEFORE, out
    # A parent is closed to renaming only: a file can still be made in it.
    if folder != _WISP:
        builtin.run_shell(f"echo fine > {folder}/beside.txt")
        assert (folder / "beside.txt").read_text() == "fine\n"
        (folder / "beside.txt").unlink()


@real
def test_the_keychains_folder_is_closed_too(world):
    # The name is put together at run time: the text policy refuses the plain spelling
    # itself, and this must be the guard's doing.
    cmd = f'k=chains; echo x > "{world}/Library/Key$k/login.$k-db"'
    assert policy.decide("shell", {"cmd": cmd}, tool="run_shell").tier is policy.Tier.ALLOW
    out = builtin.run_shell(cmd)
    assert list((world / "Library" / "Keychains").iterdir()) == [], out


@real
def test_a_disk_image_cannot_be_mounted_over_the_folder_or_a_parent(world):
    """`hdiutil attach -mountpoint` would swap every file in the folder at once."""
    source = _TOP / "elsewhere" / "image-source"
    source.mkdir(exist_ok=True)
    (source / "act_for_me.json").write_text("forged")
    image = _TOP / "elsewhere" / "swap.dmg"
    if not image.exists():
        subprocess.run(["/usr/bin/hdiutil", "create", "-quiet", "-srcfolder", str(source),
                        "-format", "UDRO", str(image)], check=True, capture_output=True, timeout=60)
    free = _TOP / "elsewhere" / "mount-here"
    free.mkdir(exist_ok=True)
    attach = "hdiutil attach -quiet -nobrowse -readonly -mountpoint"
    try:
        for target in (_WISP, _WISP.parent, _WISP.parent.parent):
            out = builtin.run_shell(f"{attach} {target} {image}")
            assert GATE.read_text() == BEFORE, (target, out)
            assert not os.path.ismount(target), (target, out)
        # Mounting somewhere else is not this guard's business.
        builtin.run_shell(f"{attach} {free} {image}")
        assert (free / "act_for_me.json").read_text() == "forged"
    finally:
        for target in (free, _WISP, _WISP.parent, _WISP.parent.parent):
            if os.path.ismount(target):
                subprocess.run(["/usr/bin/hdiutil", "detach", "-quiet", "-force", str(target)],
                               capture_output=True, timeout=60)


@real
def test_wisps_own_service_cannot_be_reached_and_other_local_ports_can(world, monkeypatch):
    """A script the shell runs is a local program like the app; the kernel tells them apart."""
    import socket
    import threading

    def listen():
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)

        def serve():
            while True:
                try:
                    peer, _ = server.accept()
                except OSError:
                    return
                peer.sendall(b"reached")
                peer.close()

        threading.Thread(target=serve, daemon=True).start()
        return server, server.getsockname()[1]

    wisp, wisp_port = listen()
    other, other_port = listen()
    monkeypatch.setattr(shell_sandbox, "wisp_ports", lambda: [wisp_port])
    # The port is worked out at run time, as a script file would.
    ask = ("import socket, sys; s = socket.create_connection((sys.argv[1], int(sys.argv[2]) + int(sys.argv[3])), 3); "
           "print(s.recv(16).decode())")
    try:
        for host in ("127.0.0.1", "localhost"):
            out = builtin.run_shell(shlex.join([sys.executable, "-I", "-c", ask, host, str(wisp_port - 1), "1"]))
            assert "reached" not in out and "Operation not permitted" in out, (host, out)
        out = builtin.run_shell(shlex.join([sys.executable, "-I", "-c", ask, "127.0.0.1", str(other_port - 1), "1"]))
        assert str(out) == "reached"
    finally:
        wisp.close()
        other.close()


@real
def test_everything_else_works_as_before(world):
    target = _TOP / "elsewhere" / "ordinary.txt"
    out = builtin.run_shell(f"echo hello > {target} && cat {target}")
    assert target.read_text() == "hello\n" and str(out) == "hello"
    assert out.completion_code == 0 and out.phase == "mutation"
    # Reading inside Wisp's folder is not what this guard is for.
    assert str(builtin.run_shell(f"cat {_WISP}/notes.txt | tr a-z A-Z")) == "READABLE"
    # Exit status and stderr come back unchanged, with no note for an ordinary failure.
    failed = builtin.run_shell("echo out; echo err >&2; exit 3")
    assert failed.completion_code == 3 and str(failed) == "out\n\n[stderr]\nerr"
    # The home folder itself is only closed to renaming.
    builtin.run_shell(f"echo ok > {world}/in-home.txt")
    assert (world / "in-home.txt").read_text() == "ok\n"


@real
def test_a_refusal_tells_the_model_why_and_a_nested_sandbox_fails_plainly(world):
    refused = builtin.run_shell(f"n=act_for; echo forged > {_WISP}/${{n}}_me.json")
    assert refused.completion_code != 0
    assert "Operation not permitted" in refused and "[note] Wisp's shell cannot change" in refused
    nested = builtin.run_shell("/usr/bin/sandbox-exec -p '(version 1)(allow default)' /usr/bin/true")
    assert nested.completion_code != 0 and "[note]" in nested


@real
def test_a_plain_delete_runs_under_the_same_guard(world, monkeypatch):
    target = _TOP / "elsewhere" / "build"
    (target / "inner").mkdir(parents=True)
    seen = {}
    real_run = subprocess.run

    def spy(argv, **kwargs):
        seen["argv"] = argv
        return real_run(argv, **kwargs)

    monkeypatch.setattr(builtin.subprocess, "run", spy)
    assert policy.decide("shell", {"cmd": f"rm -rf {target}"}, tool="run_shell").delete_targets
    builtin.run_shell(f"rm -rf {target}")
    assert seen["argv"][0] == shell_sandbox.SANDBOX_EXEC
    assert seen["argv"][-4:] == ["/bin/rm", "-rf", "--", str(target)]
    assert not target.exists()


# --- these run everywhere, sandboxed or not ------------------------------------------------

def test_the_profile_names_each_folder_as_written_and_as_it_resolves(world, monkeypatch, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(_WISP.parent)
    monkeypatch.setattr("service.paths.MOE_DIR", link / "wisp-home")
    profile = shell_sandbox.build_profile()
    lines = profile.splitlines()
    assert lines[:2] == ["(version 1)", "(allow default)"]
    for folder in (link / "wisp-home", _WISP, world / ".moe", world / "Library" / "Keychains"):
        assert f'(deny file-write* (subpath "{folder}"))' in lines
    # Every parent by its exact path, so it cannot be renamed or (a symlink) swapped ...
    for parent in (link, _WISP.parent, _WISP.parent.parent, world, world / "Library"):
        assert f'(deny file-write* (literal "{parent}"))' in lines
    # Mounting over a folder or a parent is closed the same way as writing to it.
    assert sorted(line.replace("file-mount", "file-write*") for line in lines if "file-mount" in line) \
        == sorted(line for line in lines if "file-write*" in line)
    # ... and nothing wider than that: no parent is closed as a whole, the root is not named.
    assert '(literal "/")' not in profile and '(subpath "/")' not in profile
    assert f'(subpath "{_WISP.parent}")' not in profile and f'(subpath "{world}")' not in profile
    # Wisp's own service, and no other local port.
    assert lines[-1] == '(deny network-outbound (remote ip "localhost:8765"))'
    assert all(line.startswith(("(deny file-write* ", "(deny file-mount ")) for line in lines[2:-1])


def test_the_service_port_follows_an_isolated_backend(monkeypatch):
    monkeypatch.setattr("service.identity.bind_port", lambda argv=None: 8775)
    assert shell_sandbox.wisp_ports() == [8765, 8775]
    assert shell_sandbox.build_profile().endswith(
        '(deny network-outbound (remote ip "localhost:8765"))\n'
        '(deny network-outbound (remote ip "localhost:8775"))\n')
    monkeypatch.setattr("service.identity.bind_port", lambda argv=None: None)
    assert shell_sandbox.wisp_ports() == [8765]


def test_a_quote_in_a_path_cannot_end_the_profile_string(world, monkeypatch, tmp_path):
    monkeypatch.setattr("service.paths.MOE_DIR", tmp_path / 'a"b\\c')
    profile = shell_sandbox.build_profile()
    assert f'(subpath "{tmp_path}/a\\"b\\\\c")' in profile


def test_without_a_sandbox_the_command_runs_as_it_did_before(world, monkeypatch):
    monkeypatch.setattr(shell_sandbox, "usable", lambda: False)
    seen = {}

    def spy(argv, **kwargs):
        seen["argv"], seen["shell"] = argv, kwargs.get("shell")
        return subprocess.CompletedProcess(argv, 1, "", "x: Operation not permitted")

    monkeypatch.setattr(builtin.subprocess, "run", spy)
    out = builtin.run_shell("n=x; echo $n > /nonexistent/x")
    assert seen == {"argv": ["/bin/sh", "-c", "n=x; echo $n > /nonexistent/x"], "shell": False}
    assert "[note]" not in out                       # no guard ran, so no guard is blamed


def test_with_a_sandbox_the_command_is_wrapped_and_a_read_is_not(world, monkeypatch):
    monkeypatch.setattr(shell_sandbox, "usable", lambda: True)
    calls = []

    def spy(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(builtin.subprocess, "run", spy)
    builtin.run_shell("n=x; echo $n")
    assert calls[0][:2] == [shell_sandbox.SANDBOX_EXEC, "-p"]
    assert calls[0][2] == shell_sandbox.build_profile() and calls[0][3:] == ["/bin/sh", "-c", "n=x; echo $n"]
    # Without full access a validated read is a fixed argv with no shell, and stays one.
    monkeypatch.setattr(policy, "_FULL_ACCESS", False)
    assert policy.decide("shell", {"cmd": "pwd"}, tool="run_shell").shell_argv is not None
    builtin.run_shell("pwd")
    assert calls[1][0] != shell_sandbox.SANDBOX_EXEC and "/bin/sh" not in calls[1]


def test_only_a_working_sandbox_is_remembered(monkeypatch):
    answers = iter([False, False, True])
    monkeypatch.setattr(shell_sandbox, "_usable", False)
    monkeypatch.setattr(shell_sandbox, "_ask", lambda: next(answers))
    # Asked again after each no, never after a yes (a fourth ask would raise StopIteration).
    assert [shell_sandbox.usable() for _ in range(5)] == [False, False, True, True, True]


def test_the_question_is_a_fixed_command_and_does_not_use_subprocess_run(monkeypatch):
    started = []

    class Probe:
        def __init__(self, argv, **kwargs):
            started.append((argv, kwargs.get("env")))

        def wait(self, timeout=None):
            return 0

    def not_this(*a, **k):
        raise AssertionError("a caller's subprocess.run must not answer the question")

    monkeypatch.setattr(shell_sandbox.subprocess, "Popen", Probe)
    monkeypatch.setattr(shell_sandbox.subprocess, "run", not_this)
    assert shell_sandbox._ask() is True
    assert started == [([shell_sandbox.SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/usr/bin/true"],
                        {"PATH": "/usr/bin:/bin"})]


def test_a_missing_or_stuck_sandbox_exec_is_a_no_not_a_crash(monkeypatch):
    def gone(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(shell_sandbox, "_usable", False)
    monkeypatch.setattr(shell_sandbox.subprocess, "Popen", gone)
    assert shell_sandbox.usable() is False
    assert shell_sandbox.contain(["/bin/sh", "-c", "true"]) == (["/bin/sh", "-c", "true"], False)

    class Stuck:
        killed = False

        def __init__(self, argv, **kwargs):
            pass

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("sandbox-exec", timeout)
            return -9

        def kill(self):
            Stuck.killed = True

    monkeypatch.setattr(shell_sandbox.subprocess, "Popen", Stuck)
    assert shell_sandbox.usable() is False and Stuck.killed


@pytest.mark.parametrize("contained,code,stderr,noted", [
    (True, 1, "sh: x: Operation not permitted", True),
    (True, 71, "sandbox-exec: sandbox_apply: Operation not permitted", True),
    (True, 0, "warning: Operation not permitted", False),        # it worked
    (True, 1, "No such file or directory", False),               # some other failure
    (False, 1, "sh: x: Operation not permitted", False),         # no guard was on
    (True, 1, None, False),
])
def test_the_note_is_added_only_when_the_guard_may_be_the_reason(contained, code, stderr, noted):
    assert bool(shell_sandbox.explain(contained, code, stderr)) is noted
