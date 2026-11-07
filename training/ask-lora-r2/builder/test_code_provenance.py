"""code_provenance.py: a build records the code it ran on, says when that code wasn't committed, and only claims a service
commit that a pinned copy matches file for file."""
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import code_provenance as P  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo(tmp_path):
    r = tmp_path / "repo"
    (r / "training" / "b").mkdir(parents=True)
    (r / "service").mkdir()
    (r / "scripts").mkdir()
    (r / "training" / "b" / "x.py").write_text("x = 1\n")
    (r / "service" / "a.py").write_text("a = 1\n")
    (r / "scripts" / "s.py").write_text("s = 1\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "c")
    return r, _git(r, "rev-parse", "HEAD")


def _pin(r, head):
    pin = r / "training" / "data" / f"pin-{head[:8]}"
    pin.mkdir(parents=True)
    shutil.copytree(r / "service", pin / "service")
    shutil.copytree(r / "scripts", pin / "scripts")
    (r / ".gitignore").write_text("training/data/\n")
    return pin


def test_clean_tree_records_its_head(tmp_path):
    r, head = _repo(tmp_path)
    d = P.describe(r / "training" / "b", r)
    assert d["code_head"] == head and d["code_dirty"] is False and "code_dirty_sha256" not in d
    assert d["service_head"] == head and d["service_dirty"] is False


def test_uncommitted_code_is_marked_and_hashed(tmp_path):
    r, head = _repo(tmp_path)
    (r / "training" / "notes.md").write_text("not code\n")
    assert P.code(r / "training" / "b")["code_dirty"] is False             # an untracked note isn't code
    (r / "training" / "b" / "x.py").write_text("x = 2\n")
    one = P.code(r / "training" / "b")
    assert one["code_head"] == head and one["code_dirty"] is True and one["code_dirty_files"] == ["training/b/x.py"]
    (r / "training" / "b" / "y.py").write_text("y = 1\n")
    two = P.code(r / "training" / "b")
    assert two["code_dirty_sha256"] != one["code_dirty_sha256"] and "training/b/y.py" in two["code_dirty_files"]


def test_a_matching_pin_names_its_commit(tmp_path):
    r, head = _repo(tmp_path)
    pin = _pin(r, head)
    s = P.service(pin, r / "training" / "b")
    assert s["service_head"] == head and s["service_matches_pin_commit"] is True and s["pin_differs_from_commit"] == []


def test_a_changed_pin_claims_no_commit(tmp_path):
    r, head = _repo(tmp_path)
    pin = _pin(r, head)
    (pin / "service" / "a.py").write_text("a = 2\n")
    (pin / "scripts" / "extra.py").write_text("e\n")
    s = P.service(pin, r / "training" / "b")
    assert s["service_head"] is None and s["service_matches_pin_commit"] is False
    assert {d["path"] for d in s["pin_differs_from_commit"]} == {"service/a.py", "scripts/extra.py"}


def test_no_git_uses_the_kit_file_or_says_unknown(tmp_path):
    d = tmp_path / "kit"
    d.mkdir()
    assert P.code(d)["code_head"] == "unknown"
    (d / P.KIT_FILE).write_text('{"code_head": "abc", "code_dirty": false}')
    assert P.code(d)["code_head"] == "abc" and P.code(d)["code_source"].startswith("kit")
