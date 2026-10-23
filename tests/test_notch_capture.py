"""Live-run plumbing: read-only file capture, snapshot install, launch and quit.

A temporary directory stands in for the user's home and a socket thread for
the app. Nothing here reads the real Documents or Desktop, starts the app,
runs `open`, signals a process or contacts an engine.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from scripts.notch_evaluation import capture, lifecycle, snapshots
from scripts.notch_evaluation.host import ENDPOINTS, LIVE_SOURCE_ENDPOINTS, HostConfig


def _home(tmp: Path) -> Path:
    home = tmp / "home"
    files = {
        "Documents/plan.md": "SYNTHETIC plan",
        "Documents/deep/notes.txt": "SYNTHETIC notes",
        "Documents/.hidden.txt": "hidden",
        "Documents/.git/config": "vcs",
        "Documents/node_modules/x.txt": "deps",
        "Documents/Tool.app/Contents/Info.plist": "bundle",
        "Documents/run.sh": "#!/bin/sh",
        "Documents/secret.pem": "key",
        "Desktop/todo.txt": "SYNTHETIC todo",
        "Desktop/MOE_Project/service/main.py": "code",
        "Desktop/MOE_Project/README.md": "readme",
        "Desktop/MOE_Project/docs/guide.md": "SYNTHETIC guide",
        "Desktop/MOE_Project/docs/corpus.md": "SYNTHETIC corpus",
        "Desktop/MOE_Project-local-backup/notch-1000/run/cases.json": "private",
        "Library/Mail/x.emlx": "mail",
    }
    for rel, text in files.items():
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    os.symlink(home / "Desktop/todo.txt", home / "Documents/link.txt")
    return home


def test_selection_keeps_documents_and_project_docs_only(tmp_path):
    home = _home(tmp_path)
    corpus = str(home / "Desktop/MOE_Project/docs/corpus.md")
    picked = capture.select_files(home, excluded=(corpus,))
    assert sorted(picked["files"]) == ["Desktop/MOE_Project/docs/guide.md", "Desktop/todo.txt",
                                       "Documents/deep/notes.txt", "Documents/plan.md"]
    assert picked["complete"] and picked["skipped"]["excluded_paths"] == 1
    assert picked["skipped"]["links"] == 1 and picked["skipped"]["refused_type"] == 2
    tight = capture.select_files(home, excluded=(corpus,), budget=len("SYNTHETIC guide"))
    assert not tight["complete"] and tight["skipped"]["over_budget"] == 3


def test_capture_is_verified_and_installs_into_a_fresh_root_only(tmp_path):
    home = _home(tmp_path)
    dest = tmp_path / "snap"
    result = capture.capture_files(dest, home=home, excluded=(str(home / "Desktop/MOE_Project/docs/corpus.md"),))
    assert result["files"] == 4 and not (tmp_path / "snap.no-state").exists()
    verified = snapshots.verify(dest)
    assert verified.ok and verified.manifest_sha256 == result["manifest_sha256"]
    assert (dest / "home/Documents/plan.md").read_text() == "SYNTHETIC plan"
    assert not (dest / "wisp").exists()                       # no Wisp state is captured
    root = tmp_path / "root"                                  # inside the system temporary area
    lifecycle.create_root(str(root))
    assert lifecycle.install_snapshot(str(dest), str(root)) == result["manifest_sha256"]
    assert snapshots.verify_installed(root, expected_manifest_sha256=result["manifest_sha256"]).ok
    with pytest.raises(lifecycle.RootError):                  # a populated root is not fresh
        lifecycle.install_snapshot(str(dest), str(root))

def test_an_altered_snapshot_is_refused(tmp_path):
    home = _home(tmp_path)
    dest = tmp_path / "snap"
    capture.capture_files(dest, home=home)
    (dest / "home/Documents/plan.md").write_text("changed")
    root = tmp_path / "root"
    with pytest.raises(lifecycle.RootError):
        lifecycle.install_snapshot(str(dest), str(root))


def test_reader_endpoints_exist_only_in_live_mode(tmp_path):
    fields = dict(run_id="r", owned_root=str(tmp_path), listen_port=18891, token="t" * 40, nonce="n" * 32)
    assert HostConfig(**fields).endpoints == ENDPOINTS
    live = HostConfig(**fields, source_mode="live").endpoints
    assert live == ENDPOINTS + LIVE_SOURCE_ENDPOINTS
    methods = {m for m, _ in LIVE_SOURCE_ENDPOINTS}
    assert methods <= {"GET", "POST"}
    assert not any("send" in p or "behavior" in p or "ack" in p or "claim" in p for _, p in LIVE_SOURCE_ENDPOINTS)
    assert [p for _, p in LIVE_SOURCE_ENDPOINTS if "event" in p] == [r"/assistant/events"]



class _Launcher:
    """The supervised `open -W` child: it ends once the app has quit."""

    def __init__(self):
        self.quit = threading.Event()

    def alive(self):
        return not self.quit.is_set()


def test_live_app_is_launched_by_launch_services_and_quit_by_token(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    monkeypatch.chdir(root)                    # as the supervisor runs; the socket name stays short
    argv = lifecycle.launch_services_argv("/x/Eval.app", str(root), {"B": "2", "A": "1"})
    assert argv[:3] == ["/usr/bin/open", "-n", "-W"] and argv[-1] == "/x/Eval.app"
    assert argv[3:7] == ["--env", "A=1", "--env", "B=2"]
    env = lifecycle.app_environment(str(root), str(root / "tmp-app"), launch_services=True)
    assert env[lifecycle.LAUNCH_FLAG] == lifecycle.LAUNCH_SERVICES      # the app must then self-confine
    assert lifecycle.LAUNCH_FLAG not in lifecycle.app_environment(str(root), "t", launch_services=False)
    launcher, seen = _Launcher(), []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(lifecycle.APP_INPUT)
    server.listen(1)

    def app():
        conn, _ = server.accept()
        with conn:
            request = json.loads(conn.recv(4096))
            seen.append(request)
            ok = request == {"op": "quit", "token": "T" * 40}
            conn.sendall(json.dumps({"status": "quitting" if ok else "refused"}).encode() + b"\n")
            if ok:
                launcher.quit.set()

    thread = threading.Thread(target=app, daemon=True)
    thread.start()
    outcome = lifecycle.request_app_quit(str(root), "T" * 40, launcher, wait=5)
    thread.join(5)
    server.close()
    assert outcome == {"requested": True, "reply": "quitting", "app_exited": True}
    # Nothing answering: reported, and the launcher is still waited on, never signalled.
    gone = lifecycle.request_app_quit(str(root / "missing"), "T" * 40, _Launcher(), wait=0.2)
    assert gone["requested"] is False and gone["app_exited"] is False
    assert gone["warning"].startswith("APP_STILL_RUNNING")


def test_stop_asks_the_app_to_quit_before_retiring_children(tmp_path):
    order = []

    class Proc:
        pid, returncode = 4321, None

        def poll(self):
            return self.returncode

        def send_signal(self, sig):
            order.append(("signal", sig))
            self.returncode = -sig

        def wait(self, timeout=None):
            return self.returncode

    sup = lifecycle.Supervisor(str(tmp_path), spawn=lambda argv, **kw: Proc())
    sup.start("app", ["/usr/bin/open"], env={}, cwd=str(tmp_path), log="app-launch.log")
    sup.before_stop = lambda: order.append("quit") or {"requested": True}
    state = sup.stop_all("stopped", "stop requested")
    assert order[0] == "quit" and state["app_quit"] == {"requested": True}
    assert sup.before_stop is None and "warning" not in state
    wedged = lifecycle.Supervisor(str(tmp_path), spawn=lambda argv, **kw: Proc())
    wedged.before_stop = lambda: {"requested": True, "app_exited": False, "warning": "APP_STILL_RUNNING: synthetic"}
    assert wedged.stop_all("stopped")["warning"] == "APP_STILL_RUNNING: synthetic"   # surfaced, not buried
