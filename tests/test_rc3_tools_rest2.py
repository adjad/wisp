"""rc3 tools-rest chunk 2: authoring check, read_file/list_dir bounds and TCC
messages, find_place category matching, text_to_speech argv. No real `say`, no
network, no user data."""
import subprocess

import pytest

from service.tools import tool_authoring as ta


@pytest.mark.parametrize("task", [
    "count words in the given text",
    "convert text to title case",
    "slugify some text",
])
def test_plain_text_utilities_are_not_redirected(task):
    redirect, _ = ta._capability_check("text_tool", "", task)
    assert redirect == ""


@pytest.mark.parametrize("task,expect", [
    ("read my text messages", "view_messages"),
    ("show recent iMessages", "view_messages"),
    ("find texts from Sam", "view_messages"),
    ("list my contacts", "list_contacts"),
    ("summarize my inbox", "view_emails"),
])
def test_personal_data_requests_are_still_redirected(task, expect):
    redirect, _ = ta._capability_check("helper", "", task)
    assert expect in redirect


# --- b08-tools-rest#8: read_file and recursive list_dir are bounded ---------

def test_read_file_reads_only_a_prefix_of_a_huge_file(tmp_path, monkeypatch):
    from pathlib import Path

    from service.tools import builtin

    big = tmp_path / "huge.log"
    with big.open("wb") as fh:
        fh.write(b"first line\n")
        fh.truncate(2 * 1024 ** 3)  # sparse: no disk used
    # A NUL run would mean binary, so make the file look like text.
    with big.open("r+b") as fh:
        fh.seek(0)
        fh.write(b"a" * 100_000)

    def boom(self):
        raise AssertionError("read_bytes must not be used")

    monkeypatch.setattr(Path, "read_bytes", boom)
    out = builtin.read_file(str(big))
    assert out.startswith("a" * 100)
    assert len(out) < builtin.MAX_OUTPUT + 200
    assert "truncated" in out and "bytes" in out


def test_read_file_small_text_is_unchanged(tmp_path):
    from service.tools import builtin

    f = tmp_path / "note.txt"
    f.write_text("hello there\n")
    assert builtin.read_file(str(f)) == "hello there\n"


def test_read_file_binary_and_oversized_document(tmp_path, monkeypatch):
    from service.tools import builtin

    img = tmp_path / "pic.dat"
    img.write_bytes(b"\x89PNG\x00\x00" + b"x" * 50)
    assert "not text" in builtin.read_file(str(img))

    monkeypatch.setattr(builtin, "_MAX_DOCUMENT_BYTES", 10)
    pdf = tmp_path / "big.pdf"
    pdf.write_bytes(b"%PDF-1.4 " + b"x" * 100)
    assert "too large" in builtin.read_file(str(pdf))


def test_recursive_list_dir_stops_near_the_output_cap(tmp_path, monkeypatch):
    import time as _t

    from service.tools import builtin

    for d in range(20):
        sub = tmp_path / f"folder{d:02d}"
        sub.mkdir()
        for i in range(120):
            (sub / f"a_rather_long_file_name_number_{i:04d}.txt").touch()
    walked = []
    real_walk = builtin.os.walk

    def counting_walk(*a, **k):
        for item in real_walk(*a, **k):
            walked.append(item[0])
            yield item

    monkeypatch.setattr(builtin.os, "walk", counting_walk)
    t0 = _t.monotonic()
    out = builtin.list_dir(str(tmp_path), recursive=True, max_depth=3)
    assert _t.monotonic() - t0 < 5
    assert "listing cut" in out
    assert len(out) < builtin.MAX_OUTPUT + 300
    assert len(walked) < 21  # stopped before visiting every folder


def test_recursive_list_dir_small_tree_has_no_cut_marker(tmp_path):
    from service.tools import builtin

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.txt").touch()
    out = builtin.list_dir(str(tmp_path), recursive=True)
    assert out.splitlines() == ["a/", "a/b.txt"]


# --- b08-tools-rest#9: blocked folders get the permission message -----------

def test_list_dir_blocked_parent_gets_the_permission_message(tmp_path, monkeypatch):
    from service.tools import builtin

    blocked = tmp_path / "Documents"
    blocked.mkdir()
    real = builtin.os.listdir

    def fake(path="."):
        if str(path) == str(blocked):
            raise PermissionError(1, "Operation not permitted")
        return real(path)

    monkeypatch.setattr(builtin.os, "listdir", fake)
    out = builtin.list_dir(str(blocked / "Taxes"))
    assert "macOS blocked listing" in out
    assert "no such directory" not in out


def test_list_dir_really_missing_folder_still_lists_the_ancestor(tmp_path):
    from service.tools import builtin

    (tmp_path / "real").mkdir()
    out = builtin.list_dir(str(tmp_path / "nope"))
    assert out.startswith("(no such directory:")
    assert "real" in out


def test_find_files_blocked_parent_gets_the_permission_message(tmp_path, monkeypatch):
    from service.tools import files_tools

    blocked = tmp_path / "Documents"
    blocked.mkdir()
    real = files_tools.os.listdir

    def fake(path="."):
        if str(path) == str(blocked):
            raise PermissionError(1, "Operation not permitted")
        return real(path)

    monkeypatch.setattr(files_tools.os, "listdir", fake)
    out = files_tools.find_files("taxes", folder=str(blocked / "Taxes"))
    assert "macOS blocked" in out
    out = files_tools.find_files("taxes", folder=str(tmp_path / "missing"))
    assert "is not a folder on this Mac" in out
