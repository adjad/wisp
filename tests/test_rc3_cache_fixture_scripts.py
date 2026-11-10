"""install_cache_fixtures.sh and restore_cache_backup.sh keep the real cache safe.

Both scripts run against a throwaway HOME and a copied script folder with stub
fixtures, so nothing here reads or writes the real ~/.moe.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = "install_cache_fixtures.sh"
RESTORE = "restore_cache_backup.sh"
MARKER = ".wisp_fixtures_installed"
REAL_MESSAGES = "Sam Rivera | are we still on for Thursday?\n"
REAL_NOTES = "Gate code for the shed: ask Dana\n"
FIXTURE_MESSAGES = "Synthetic Person | this is a fixture message\n"
FIXTURE_NOTES = "Synthetic note\n"
# Every program the two scripts start. Simulation QA's sandbox lets test
# children run only a short list, and a refused program would prove nothing.
TOOLS = ("date", "dirname", "ls", "mkdir", "cp", "chmod", "cmp", "head", "rm")


class Sandbox:
    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "repo"
        self.home = tmp_path / "home"
        self.moe = self.home / ".moe"
        self.cache = self.moe / "cache"
        (self.repo / "scripts").mkdir(parents=True)
        for name in (INSTALL, RESTORE):
            shutil.copy(ROOT / "scripts" / name, self.repo / "scripts" / name)
        fixtures = self.repo / "test_fixtures" / "cache"
        fixtures.mkdir(parents=True)
        (fixtures / "messages.txt").write_text(FIXTURE_MESSAGES, encoding="utf-8")
        (fixtures / "notes.txt").write_text(FIXTURE_NOTES, encoding="utf-8")
        self.cache.mkdir(parents=True)

    def run(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / script), *args],
            env={"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            capture_output=True, text=True, timeout=30,
        )

    def backups(self) -> list[Path]:
        return sorted(self.moe.glob("cache_backup_*"))

    def read(self, name: str) -> str:
        return (self.cache / name).read_text(encoding="utf-8")

    def seed_real_cache(self) -> None:
        (self.cache / "messages.txt").write_text(REAL_MESSAGES, encoding="utf-8")
        (self.cache / "notes.txt").write_text(REAL_NOTES, encoding="utf-8")


@pytest.fixture
def box(tmp_path) -> Sandbox:
    for tool in TOOLS:
        probe = subprocess.run(["bash", "-c", f"{tool} </dev/null >/dev/null"],
                               capture_output=True, timeout=10)
        if b"Operation not permitted" in probe.stderr:
            # Only that refusal skips: any other failure must fail the test.
            pytest.skip(f"this sandbox does not allow executing {tool}")
    return Sandbox(tmp_path)


def test_installing_twice_still_restores_the_real_cache(box) -> None:
    box.seed_real_cache()

    first = box.run(INSTALL)
    assert first.returncode == 0, first.stderr
    assert box.read("messages.txt") == FIXTURE_MESSAGES
    assert len(box.backups()) == 1
    backup = box.backups()[0]
    assert (box.cache / MARKER).read_text(encoding="utf-8").strip() == str(backup)

    second = box.run(INSTALL)
    assert second.returncode == 0, second.stderr
    assert "already installed" in second.stdout and str(backup) in second.stdout
    assert box.backups() == [backup]
    assert (backup / "messages.txt").read_text(encoding="utf-8") == REAL_MESSAGES
    assert (backup / "notes.txt").read_text(encoding="utf-8") == REAL_NOTES
    assert box.read("messages.txt") == FIXTURE_MESSAGES

    restored = box.run(RESTORE)
    assert restored.returncode == 0, restored.stderr
    assert box.read("messages.txt") == REAL_MESSAGES
    assert box.read("notes.txt") == REAL_NOTES
    assert not (box.cache / MARKER).exists()
    assert box.backups() == [backup]  # never deleted automatically


def test_fixture_copies_are_never_backed_up_as_real_content(box) -> None:
    # No marker (an install made before the marker existed): the cache already
    # holds the fixtures, and one file has since been re-synced with real text.
    (box.cache / "messages.txt").write_text(FIXTURE_MESSAGES, encoding="utf-8")
    (box.cache / "notes.txt").write_text(REAL_NOTES, encoding="utf-8")

    done = box.run(INSTALL)

    assert done.returncode == 0, done.stderr
    (backup,) = box.backups()
    assert sorted(path.name for path in backup.iterdir()) == ["notes.txt"]
    assert (backup / "notes.txt").read_text(encoding="utf-8") == REAL_NOTES


def test_restore_prefers_the_recorded_backup_over_a_newer_folder(box) -> None:
    box.seed_real_cache()
    assert box.run(INSTALL).returncode == 0
    (backup,) = box.backups()
    newer = box.moe / "cache_backup_29991231_235959"
    newer.mkdir()
    (newer / "messages.txt").write_text(FIXTURE_MESSAGES, encoding="utf-8")
    os.utime(backup, (1_700_000_000, 1_700_000_000))

    restored = box.run(RESTORE)

    assert restored.returncode == 0, restored.stderr
    assert str(backup) in restored.stdout
    assert box.read("messages.txt") == REAL_MESSAGES


def test_explicit_backup_path_is_still_honoured(box) -> None:
    box.seed_real_cache()
    assert box.run(INSTALL).returncode == 0
    chosen = box.moe / "cache_backup_20260101_000000"
    chosen.mkdir()
    (chosen / "messages.txt").write_text("Dana Whitfield | chosen backup\n", encoding="utf-8")

    restored = box.run(RESTORE, str(chosen))

    assert restored.returncode == 0, restored.stderr
    assert box.read("messages.txt") == "Dana Whitfield | chosen backup\n"
    assert not (box.cache / MARKER).exists()


def test_cache_with_only_json_makes_no_empty_backup(box) -> None:
    timers = box.cache / "timers.json"
    timers.write_text("[]", encoding="utf-8")

    installed = box.run(INSTALL)
    assert installed.returncode == 0, installed.stderr
    assert "no real cache content to back up" in installed.stdout
    assert box.backups() == []
    assert box.read("messages.txt") == FIXTURE_MESSAGES
    assert timers.read_text(encoding="utf-8") == "[]"

    again = box.run(INSTALL)
    assert again.returncode == 0, again.stderr
    assert box.backups() == []  # the fixtures are not real content

    restored = box.run(RESTORE)
    assert restored.returncode == 1
    assert "no cache backup found" in restored.stderr
    assert "No such file" not in restored.stderr
    assert timers.read_text(encoding="utf-8") == "[]"


def test_restore_refuses_a_backup_without_cache_files(box) -> None:
    box.seed_real_cache()
    assert box.run(INSTALL).returncode == 0
    empty = box.moe / "cache_backup_20260101_000000"
    empty.mkdir()
    (empty / "timers.json").write_text("[]", encoding="utf-8")

    refused = box.run(RESTORE, str(empty))

    assert refused.returncode == 1
    assert "holds no .txt cache files" in refused.stderr
    assert "No such file" not in refused.stderr
    assert box.read("messages.txt") == FIXTURE_MESSAGES
    assert (box.cache / MARKER).exists()  # the real backup is still on record
    assert box.run(RESTORE).returncode == 0
    assert box.read("messages.txt") == REAL_MESSAGES


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 file")
def test_failed_backup_stops_before_the_fixtures_overwrite_anything(box) -> None:
    box.seed_real_cache()
    unreadable = box.cache / "messages.txt"
    unreadable.chmod(0o000)
    try:
        failed = box.run(INSTALL)
    finally:
        unreadable.chmod(0o600)

    assert failed.returncode != 0
    assert "installed synthetic" not in failed.stdout
    assert box.read("messages.txt") == REAL_MESSAGES
    assert box.read("notes.txt") == REAL_NOTES
    assert not (box.cache / MARKER).exists()
