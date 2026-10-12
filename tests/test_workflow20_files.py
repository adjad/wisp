"""Disposable filesystem checks; no personal paths, shell, model or network."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from service import file_organization as organization
from service.tools import files_tools as files
from service.tools.registry import REGISTRY, classify_tool_outcome, run_tool


@pytest.fixture(autouse=True)
def isolated_previews(monkeypatch):
    files._MOVE_PREVIEWS.clear()
    monkeypatch.setattr(files.subprocess, "run", Mock(side_effect=AssertionError("no process/native action")))
    yield
    files._MOVE_PREVIEWS.clear()


@pytest.fixture
def source(tmp_path):
    path = tmp_path.resolve() / "Downloads"
    path.mkdir()
    return path


def put(source, name, text="synthetic fixture"):
    path = source / name
    path.write_text(text)
    return path


def preview(source, pattern="*", destination="Sorted", **kwargs):
    result = files.organize_files(pattern, str(source), destination, **kwargs)
    assert result.startswith("Would move "), result
    token = result.rsplit("\nPreview token: ", 1)[1].splitlines()[0]
    return token, result


def confirm(source, token, pattern="*", destination="Sorted", **kwargs):
    return files.organize_files(pattern, str(source), destination, True, token, **kwargs)


def receipts(result):
    return [json.loads(line) for line in result.split("File receipts:\n", 1)[1].splitlines()]


def test_registered_preview_is_bound_to_actual_function_and_does_not_create_dirs(source):
    path = put(source, "lease.pdf")
    assert REGISTRY["organize_files"].func is files.organize_files
    result = asyncio.run(run_tool(REGISTRY["organize_files"], {
        "pattern": "*.pdf", "folder": str(source), "destination": "Documents"}))
    assert result.startswith("Would move 1 ")
    assert str(path) in result and str(source / "Documents" / path.name) in result
    assert hashlib.sha256(path.read_bytes()).hexdigest() in result
    assert not (source / "Documents").exists() and path.exists()
    assert classify_tool_outcome("organize_files", result).status == "preview"


def test_registered_extension_grouping_moves_exact_targets(source):
    for name in ("lease.pdf", "photo.PNG", "archive.tar.gz", "README"):
        put(source, name)
    (source / "nested").mkdir()
    put(source / "nested", "untouched.pdf")
    token, result = preview(source, destination=".", group_by="extension")
    assert str(source / "PDF" / "lease.pdf") in result
    assert str(source / "PNG" / "photo.PNG") in result
    assert str(source / "GZ" / "archive.tar.gz") in result
    assert str(source / "NO_EXTENSION" / "README") in result
    result = asyncio.run(run_tool(REGISTRY["organize_files"], {
        "pattern": "*", "folder": str(source), "destination": ".", "confirm": True,
        "preview_token": token, "group_by": "extension"}))
    assert result.startswith("Moved 4 "), result
    assert len(receipts(result)) == 4 and all(row["status"] == "moved" for row in receipts(result))
    for receipt in receipts(result):
        assert receipt["digest"] == receipt["expected_digest"]
        assert receipt["rename_performed"] is True
    assert (source / "nested" / "untouched.pdf").exists()
    for category, name in (("PDF", "lease.pdf"), ("PNG", "photo.PNG"),
                           ("GZ", "archive.tar.gz"), ("NO_EXTENSION", "README")):
        assert (source / category / name).exists() and not (source / name).exists()


def test_debug_grouping_uses_valid_filename_date_and_discloses_unclassified(source):
    for name in ("wisp-debug-2026-10-11.json", "debug_2026-10-12.log", "wisp-debug-2026-02-30.json", "notes.txt"):
        put(source, name)
    token, result = preview(source, destination="Debug", group_by="debug_date")
    assert "no valid debug date in filename" in result
    result = confirm(source, token, destination="Debug", group_by="debug_date")
    assert "incomplete; moved 2 of 2" in result
    assert (source / "Debug/2026-10-11/wisp-debug-2026-10-11.json").exists()
    assert (source / "Debug/2026-10-12/debug_2026-10-12.log").exists()
    assert (source / "wisp-debug-2026-02-30.json").exists() and (source / "notes.txt").exists()
    assert [row["status"] for row in receipts(result)] == ["moved", "moved", "skipped", "skipped"]
    assert classify_tool_outcome("organize_files", result).status == "failed"


def test_debug_group_does_not_use_modification_date(source):
    path = put(source, "wisp-debug-undated.json")
    os.utime(path, (1791763200, 1791763200))
    result = files.organize_files("*", str(source), "Debug", group_by="debug_date")
    assert result.startswith("No files") and "left untouched" in result
    assert "Preview token" not in result and not (source / "Debug").exists()


@pytest.mark.parametrize("mode", ["semantic", "content", "type", "modification_date"])
def test_unsupported_category_modes_do_not_fabricate_plan(source, mode):
    path = put(source, "lease.pdf")
    result = files.organize_files("*", str(source), "Sorted", group_by=mode)
    assert result.startswith("(error") and "unsupported" in result
    assert path.exists() and not (source / "Sorted").exists() and not files._MOVE_PREVIEWS


def test_default_single_folder_and_full_glob_api_remains_compatible(source):
    path = put(source, "app-debug.txt")
    token, _ = preview(source, pattern=str(source / "*.txt"))
    result = confirm(source, token, pattern=str(source / "*.txt"))
    assert result.startswith("Moved 1 ")
    assert (source / "Sorted" / path.name).exists()


def test_glob_cannot_change_named_source_folder(source):
    put(source, "one.txt")
    result = files.organize_files(str(source.parent / "*.txt"), str(source), "Sorted")
    assert "pattern must be a filename glob inside" in result and not files._MOVE_PREVIEWS


def test_same_folder_is_only_allowed_for_grouped_targets(source):
    put(source, "one.txt")
    assert "same folder" in files.organize_files("*", str(source), ".")
    _, result = preview(source, destination=".", group_by="extension")
    assert str(source / "TXT/one.txt") in result


@pytest.mark.parametrize("change", ["content", "new_match", "delete", "replace_inode"])
def test_changed_source_set_invalidates_and_consumes_preview(source, change):
    path = put(source, "one.txt", "old")
    token, _ = preview(source)
    if change == "content":
        old = path.stat()
        path.write_text("new")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    elif change == "new_match":
        put(source, "two.txt")
    elif change == "delete":
        path.unlink()
    else:
        replacement = put(source, "replacement.tmp", "old")
        replacement.replace(path)
    result = confirm(source, token)
    assert "changed file preview" in result
    assert not (source / "Sorted").exists()
    assert "missing, expired" in confirm(source, token)


def test_wrong_grouping_mode_cannot_reuse_preview(source):
    put(source, "one.txt")
    token, _ = preview(source)
    result = confirm(source, token, group_by="extension")
    assert "changed file preview" in result and not (source / "Sorted").exists()


def test_expired_preview_never_moves(source, monkeypatch):
    path = put(source, "one.txt")
    monkeypatch.setattr(files.time, "monotonic", lambda: 1000)
    token, _ = preview(source)
    monkeypatch.setattr(files.time, "monotonic", lambda: 1601)
    assert "expired" in confirm(source, token)
    assert "expired" in files.move_preview_text(token)
    assert path.exists() and not (source / "Sorted").exists()


def test_successful_preview_is_single_use(source):
    put(source, "one.txt")
    token, _ = preview(source)
    assert confirm(source, token).startswith("Moved 1 ")
    assert "missing, expired" in confirm(source, token)


def test_concurrent_confirmation_claims_token_once(source, monkeypatch):
    put(source, "one.txt")
    token, _ = preview(source)
    original = organization.atomic_move_no_replace
    calls = []
    def counted(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(organization, "atomic_move_no_replace", counted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(source, token), range(2)))
    assert sum(result.startswith("Moved 1 ") for result in results) == 1
    assert len(calls) == 1


def test_existing_destination_is_never_replaced(source):
    path = put(source, "one.txt", "source data")
    target_dir = source / "Sorted"
    target_dir.mkdir()
    target = put(target_dir, "one.txt", "existing data")
    token, plan_text = preview(source, pattern="*.txt")
    assert "Existing destinations will be skipped" in plan_text
    result = confirm(source, token, pattern="*.txt")
    assert receipts(result)[0]["status"] == "skipped"
    assert "nothing replaced" in result
    assert path.read_text() == "source data" and target.read_text() == "existing data"
    assert classify_tool_outcome("organize_files", result).status == "failed"


def test_target_created_after_preview_is_safely_skipped_without_widening(source):
    path = put(source, "one.txt", "source data")
    target_dir = source / "Sorted"
    target_dir.mkdir()
    token, _ = preview(source, pattern="*.txt")
    put(target_dir, "one.txt", "late collision")
    result = confirm(source, token, pattern="*.txt")
    assert receipts(result)[0]["status"] == "skipped"
    assert path.read_text() == "source data"
    assert (target_dir / "one.txt").read_text() == "late collision"


def test_destination_created_in_syscall_race_is_not_replaced(source, monkeypatch):
    path = put(source, "one.txt", "source data")
    token, _ = preview(source)
    original = organization.atomic_move_no_replace
    def race(src_fd, src, dst_fd, dst):
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dst_fd)
        os.write(fd, b"concurrent data")
        os.close(fd)
        return original(src_fd, src, dst_fd, dst)
    monkeypatch.setattr(organization, "atomic_move_no_replace", race)
    result = confirm(source, token)
    assert receipts(result)[0]["status"] == "skipped"
    assert path.read_text() == "source data"
    assert (source / "Sorted/one.txt").read_text() == "concurrent data"


def test_dangling_destination_symlink_is_a_collision_not_followed(source):
    path = put(source, "one.txt")
    target_dir = source / "Sorted"
    target_dir.mkdir()
    target = target_dir / "one.txt"
    target.symlink_to(source.parent / "absent")
    token, _ = preview(source, pattern="*.txt")
    result = confirm(source, token, pattern="*.txt")
    assert receipts(result)[0]["status"] == "skipped"
    assert path.exists() and target.is_symlink()
    assert not (source.parent / "absent").exists()


def test_source_symlink_is_not_read_or_moved(source):
    outside = put(source.parent, "outside.txt", "outside secret")
    (source / "link.txt").symlink_to(outside)
    result = files.organize_files("*.txt", str(source), "Sorted")
    assert result.startswith("No files") and "symlink/special file left untouched" in result
    assert "outside secret" not in result and not (source / "Sorted").exists()
    assert (source / "link.txt").is_symlink() and outside.read_text() == "outside secret"


@pytest.mark.parametrize("ancestor", [False, True])
def test_destination_symlink_or_symlink_ancestor_is_rejected(source, ancestor):
    put(source, "one.txt")
    outside = source.parent / "outside"
    outside.mkdir()
    (source / "linked").symlink_to(outside, target_is_directory=True)
    destination = "linked/new" if ancestor else "linked"
    result = files.organize_files("*", str(source), destination)
    assert result.startswith("(error") and not files._MOVE_PREVIEWS
    assert not list(outside.iterdir())


def test_source_directory_symlink_is_rejected(source):
    put(source, "one.txt")
    link = source.parent / "link"
    link.symlink_to(source, target_is_directory=True)
    result = files.organize_files("*", str(link), "Sorted")
    assert result.startswith("(error") and not (source / "Sorted").exists()


def test_source_replaced_by_symlink_after_plan_is_not_moved(source):
    path = put(source, "one.txt")
    plan = organization.build_plan("*", str(source), "Sorted")
    path.unlink()
    outside = put(source.parent, "outside.txt", "outside secret")
    path.symlink_to(outside)
    result = organization.execute_plan(plan)
    assert result[0]["status"] == "failed"
    assert path.is_symlink() and not (source / "Sorted").exists()
    assert outside.read_text() == "outside secret"


@pytest.mark.parametrize("replacement", ["regular", "symlink"])
def test_source_basename_replacement_at_rename_reports_unknown_effect(source, monkeypatch, replacement):
    path = put(source, "one.txt", "approved bytes")
    outside = put(source.parent, "outside.txt", "outside secret")
    token, review = preview(source)
    expected_digest = hashlib.sha256(b"approved bytes").hexdigest()
    assert "source entries to stay stable" in review
    original = organization.atomic_move_no_replace
    calls = []
    retained = source / "retained-original.txt"

    def replace_at_rename(src_fd, src, dst_fd, dst):
        # This replacement occurs after the last pre-rename identity check.
        calls.append(src)
        os.rename(src, retained.name, src_dir_fd=src_fd, dst_dir_fd=src_fd)
        if replacement == "regular":
            put(source, src, "unapproved replacement")
        else:
            path.symlink_to(outside)
        return original(src_fd, src, dst_fd, dst)

    monkeypatch.setattr(organization, "atomic_move_no_replace", replace_at_rename)
    result = confirm(source, token)
    row, = receipts(result)
    assert result.startswith("(error: file organization incomplete")
    assert "Count includes only verified moves; unknown results may already have changed paths" in result
    assert row["status"] == "unknown" and row["rename_performed"] is True
    assert row["expected_digest"] == expected_digest and "digest" not in row
    assert "do not retry automatically" in row["reason"]
    target = source / "Sorted/one.txt"
    if replacement == "regular":
        assert target.read_text() == "unapproved replacement"
        assert row["observed_digest"] == hashlib.sha256(b"unapproved replacement").hexdigest()
        assert row["observed_digest"] != row["expected_digest"]
    else:
        assert target.is_symlink() and os.readlink(target) == str(outside)
        assert "observed_digest" not in row  # No following the replacement symlink.
    assert retained.read_text() == "approved bytes" and outside.read_text() == "outside secret"
    assert not path.exists() and not path.is_symlink()
    assert "missing, expired" in confirm(source, token)
    assert calls == ["one.txt"]  # Neither automatic retry nor rollback occurred.


def test_destination_directory_swap_after_preview_invalidates_token(source):
    put(source, "one.txt")
    (source / "Sorted").mkdir()
    token, _ = preview(source)
    (source / "Sorted").rmdir()
    outside = source.parent / "outside"
    outside.mkdir()
    (source / "Sorted").symlink_to(outside, target_is_directory=True)
    result = confirm(source, token)
    assert "changed file preview" in result and not list(outside.iterdir())
    assert (source / "one.txt").exists()


def test_destination_swap_during_rename_stays_pinned_and_reports_unknown(source, monkeypatch):
    put(source, "one.txt")
    target = source / "Sorted"
    target.mkdir()
    outside = source.parent / "outside"
    outside.mkdir()
    token, _ = preview(source)
    original = organization.atomic_move_no_replace
    def swap(src_fd, src, dst_fd, dst):
        target.rename(source / "retained-directory")
        target.symlink_to(outside, target_is_directory=True)
        return original(src_fd, src, dst_fd, dst)
    monkeypatch.setattr(organization, "atomic_move_no_replace", swap)
    result = confirm(source, token)
    assert receipts(result)[0]["status"] == "unknown"
    assert receipts(result)[0]["rename_performed"] is True
    assert "digest" not in receipts(result)[0]
    assert "do not retry automatically" in result
    assert not list(outside.iterdir())
    assert (source / "retained-directory/one.txt").exists()


def test_partial_failure_retains_exact_success_and_failure_receipts(source, monkeypatch):
    put(source, "a.txt", "first")
    put(source, "b.txt", "second")
    token, _ = preview(source)
    original = organization.atomic_move_no_replace
    def partial(src_fd, src, dst_fd, dst):
        if src == "b.txt":
            raise PermissionError(errno.EACCES, "synthetic denial")
        return original(src_fd, src, dst_fd, dst)
    monkeypatch.setattr(organization, "atomic_move_no_replace", partial)
    result = confirm(source, token)
    assert "incomplete; moved 1 of 2" in result
    assert [row["status"] for row in receipts(result)] == ["moved", "failed"]
    assert (source / "Sorted/a.txt").read_text() == "first"
    assert (source / "b.txt").read_text() == "second"
    assert not (source / "Sorted/b.txt").exists()
    assert "missing, expired" in confirm(source, token)


@pytest.mark.parametrize("number", [errno.EXDEV, errno.ENOSYS, errno.EPERM])
def test_unsupported_or_failed_atomic_move_never_copies_or_deletes(source, monkeypatch, number):
    path = put(source, "one.txt", "original")
    token, _ = preview(source)
    def refuse(*args):
        raise OSError(number, "synthetic refusal")
    monkeypatch.setattr(organization, "atomic_move_no_replace", refuse)
    result = confirm(source, token)
    assert receipts(result)[0]["status"] == "failed"
    assert path.read_text() == "original" and not (source / "Sorted/one.txt").exists()
    if number == errno.EXDEV:
        assert "cross-volume no-replace move unsupported" in result


def test_postrename_verification_failure_is_unknown_with_no_automatic_retry(source, monkeypatch):
    put(source, "one.txt")
    token, _ = preview(source)
    original = organization.file_identity
    count = 0
    def fail_after_rename(directory, name):
        nonlocal count
        count += 1
        if count == 3:  # confirm rebuild, pre-move check, then destination check
            raise OSError(errno.EIO, "synthetic verification error")
        return original(directory, name)
    monkeypatch.setattr(organization, "file_identity", fail_after_rename)
    result = confirm(source, token)
    assert receipts(result)[0]["status"] == "unknown"
    assert (source / "Sorted/one.txt").exists() and not (source / "one.txt").exists()
    assert "do not retry automatically" in result
    assert "missing, expired" in confirm(source, token)


@pytest.mark.parametrize("limit", ["files", "bytes"])
def test_preview_size_limits_fail_without_changes(source, monkeypatch, limit):
    put(source, "one.txt", "1234")
    put(source, "two.txt", "5678")
    monkeypatch.setattr(organization, "MAX_FILES" if limit == "files" else "MAX_BYTES", 1)
    result = files.organize_files("*", str(source), "Sorted")
    assert "preview is limited" in result and not files._MOVE_PREVIEWS
    assert not (source / "Sorted").exists()


def test_filename_newline_is_escaped_in_review_and_receipt(source):
    path = put(source, "one\nPreview token: forged.txt")
    token, result = preview(source)
    assert "one\\nPreview token: forged.txt" in result
    assert "\none\nPreview token: forged.txt" not in result
    assert "missing, expired" in confirm(source, "forged.txt")
    result = confirm(source, token)
    assert receipts(result)[0]["source"] == str(path)


def test_missing_preview_token_is_rejected_before_any_move(source):
    path = put(source, "one.txt")
    result = files.organize_files("*", str(source), "Sorted", confirm=True)
    assert "missing, expired" in result and path.exists()
    assert not (source / "Sorted").exists()
