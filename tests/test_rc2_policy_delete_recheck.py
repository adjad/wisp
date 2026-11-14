"""run_shell re-checks a plain delete just before it runs it (rc2 fix).

The only real deletes here are of folders inside pytest's tmp_path. Every
refusal case stubs subprocess.run so that nothing at all can run.
"""
import subprocess

import pytest

from service.safety import grants, policy
from service.safety.policy import Tier
from service.tools import builtin


@pytest.fixture
def home(monkeypatch, tmp_path):
    folder = tmp_path / "home"
    (folder / "Documents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.setattr(policy, "_FULL_ACCESS", False)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(policy, "_KEEP_FLOOR", True)
    monkeypatch.setattr(grants, "check", lambda *a, **k: None)
    return folder


def _no_run(*a, **k):
    raise AssertionError("nothing may run")


def _text(result) -> str:
    return str(getattr(result, "text", None) or getattr(result, "content", None) or result)


def test_a_quoted_delete_runs_the_decoded_words_without_a_shell(monkeypatch, home, tmp_path):
    target = tmp_path / "work dir" / "my cache"
    (target / "inner").mkdir(parents=True)
    (target / "inner" / "f.txt").write_text("x")
    seen = {}
    real_run = subprocess.run

    def spy(argv, **kwargs):
        seen["argv"], seen["shell"] = argv, kwargs.get("shell")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(builtin.subprocess, "run", spy)
    cmd = f"rm -rf '{target}'"
    assert policy.decide("shell", {"cmd": cmd}, tool="run_shell").tier is Tier.CONFIRM
    builtin.run_shell(cmd)
    # Where a sandbox can be applied, the same words follow run_shell's write guard
    # (service/safety/shell_sandbox.py).
    assert seen["argv"][-4:] == ["/bin/rm", "-rf", "--", str(target)] and seen["shell"] is False
    assert len(seen["argv"]) in (4, 7)
    assert not target.exists() and target.parent.exists()


def test_a_link_swapped_after_the_check_is_not_deleted(monkeypatch, home, tmp_path):
    first, second = tmp_path / "a" / "one", tmp_path / "a" / "two"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    link = tmp_path / "a" / "link"
    link.symlink_to(first)
    cmd = f"rm -rf {link}"
    judged = policy.decide("shell", {"cmd": cmd}, tool="run_shell").delete_targets
    assert judged == ((str(link), str(first.resolve())),)
    # Swapped to another ordinary folder between the check and the run.
    link.unlink()
    link.symlink_to(second)
    monkeypatch.setattr(builtin.subprocess, "run", _no_run)
    out = _text(builtin._run_plain_delete(cmd, judged))
    assert out.startswith("(error: not run:"), out
    assert link.is_symlink() and first.exists() and second.exists()


def test_a_link_swapped_to_a_protected_folder_is_not_deleted(monkeypatch, home, tmp_path):
    folder = tmp_path / "b" / "build"
    folder.mkdir(parents=True)
    link = tmp_path / "b" / "link"
    link.symlink_to(folder)
    cmd = f"rm -rf {link}"
    judged = policy.decide("shell", {"cmd": cmd}, tool="run_shell").delete_targets
    link.unlink()
    link.symlink_to(home / "Documents")
    monkeypatch.setattr(builtin.subprocess, "run", _no_run)
    assert _text(builtin._run_plain_delete(cmd, judged)).startswith("(error: not run:")
    # And run_shell's own re-check refuses it outright.
    assert "blocked by safety policy" in _text(builtin.run_shell(cmd))
    assert (home / "Documents").exists()


def test_a_folder_replaced_by_a_link_is_not_deleted(monkeypatch, home, tmp_path):
    folder = tmp_path / "c" / "build"
    folder.mkdir(parents=True)
    cmd = f"rm -rf {folder}"
    judged = policy.decide("shell", {"cmd": cmd}, tool="run_shell").delete_targets
    folder.rmdir()
    folder.symlink_to(tmp_path / "c")
    monkeypatch.setattr(builtin.subprocess, "run", _no_run)
    assert _text(builtin._run_plain_delete(cmd, judged)).startswith("(error: not run:")
