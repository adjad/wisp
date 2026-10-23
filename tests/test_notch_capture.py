"""The live run's read-only file capture and snapshot install, on a SYNTHETIC home.

A temporary directory stands in for the user's home. Nothing here reads the
real Documents or Desktop, starts the app or contacts an engine.
"""
from __future__ import annotations

import os
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
    assert not any("event" in p or "send" in p or "behavior" in p for _, p in LIVE_SOURCE_ENDPOINTS)

