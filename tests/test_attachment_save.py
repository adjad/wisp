"""Saving an attachment to disk (service/attachment_save.py).

A fake home folder in a tmp dir (never the real one). `mail_attachments.find` and `fetch` are faked; the
child's `inspect` is the REAL one (documents.run_attachment_child) so the risk verdicts are the real ones.
`open -R` is never launched."""
from __future__ import annotations

import copy
import ctypes
import logging
import os
import sys
import time

import pytest

from service import attachment_save as sv, documents, mail_attachments as ma
from tests.test_documents import pdf_with_text
from tests.test_mail_attachments import MZ

NOW = time.time()
PDF = pdf_with_text("Invoice for March, total 240.00")


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("WISP_HOME", str(tmp_path / "wisp"))
    root = tmp_path / "home"
    for sub in ("Downloads", "Documents/Taxes", "Library", "Desktop", ".hidden", "Documents/Thing.app"):
        (root / sub).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root))
    return root.resolve()


class World:
    def __init__(self, monkeypatch):
        self.emails: list = []
        self.found_calls: list[dict] = []
        self.bytes: bytes | Exception = PDF
        self.fetches = 0
        self.finder: list[str] = []
        self.finder_ok = True

        async def find(**kwargs):
            self.found_calls.append(kwargs)
            return list(self.emails), []

        async def fetch(email, f, *, deadline):
            self.fetches += 1
            if isinstance(self.bytes, Exception):
                raise self.bytes
            return self.bytes, "Apple Mail"

        def reveal(path):
            self.finder.append(path)
            return self.finder_ok

        monkeypatch.setattr(ma, "find", find)
        monkeypatch.setattr(ma, "fetch", fetch)
        monkeypatch.setattr(sv, "_open_in_finder", reveal)

    def add(self, *names, sender="Acme Billing <billing@acme.test>", subject="March invoice", size=240_000,
            ts=None, rfc="<m1@acme.test>"):
        email = ma._Email("k", ts or NOW - 3600, sender=sender, subject=subject, rfc=rfc, apple=True)
        for i, name in enumerate(names):
            email.files.append(ma._File(name, size, rowid=7 + i, attachment_id=f"a{i}", on_disk=True))
            email.files[-1].nth = 1
        self.emails.append(email)
        return email


@pytest.fixture
def world(monkeypatch, home):
    return World(monkeypatch)


def list_dir(path) -> list[str]:
    return sorted(os.listdir(path))


async def frozen_of(world, name="invoice.pdf", **kw):
    world.add(name)
    got = await sv.prepare(**kw)
    assert got["ok"], got
    return got["frozen"]


# --- prepare ----------------------------------------------------------------------------

async def test_one_candidate_card_and_frozen(world, home):
    world.add("invoice-march.pdf")
    got = await sv.prepare(query="acme invoice")
    assert got["ok"] is True
    card = got["card"]
    assert card == ("Save “invoice-march.pdf” (PDF, 234 KB) from Acme Billing's email "
                    "“March invoice” to Downloads")
    assert len(card) <= 200
    frozen = got["frozen"]
    assert frozen["folder"] == str(home / "Downloads") and frozen["save_name"] == "invoice-march.pdf"
    assert set(frozen) == set(sv._FIELDS)
    import json
    json.dumps(frozen)
    assert world.fetches == 0 and list_dir(home / "Downloads") == []
    call = world.found_calls[0]
    assert call["count"] == 5 and call["most"] == 5 and call["source"] == "mail"


async def test_several_files_ask_which(world):
    world.add("a.pdf", "b.pdf")
    world.add("c.pdf", sender="Bob", subject="Other")
    got = await sv.prepare(query="invoice")
    assert got["ok"] is False and got["reason"] == "More than one file matches."
    assert got["question"].startswith("Which file? ") and "a.pdf" in got["question"] and "Bob" in got["question"]
    assert got["question"].count("“") <= 5


async def test_question_lists_at_most_five(world):
    world.add(*[f"f{i}.pdf" for i in range(8)])
    got = await sv.prepare()
    assert got["question"].count("“") == 5


async def test_name_picks_one_case_insensitively(world):
    world.add("Invoice.PDF", "invoice-old.pdf")
    got = await sv.prepare(name="invoice.pdf")
    assert got["ok"] is True and got["frozen"]["name"] == "Invoice.PDF"


async def test_name_matching_two_still_asks(world):
    world.add("invoice.pdf")
    world.add("INVOICE.pdf", subject="Again")
    got = await sv.prepare(name="invoice.pdf")
    assert got["ok"] is False and got["question"]


async def test_nothing_found(world):
    got = await sv.prepare(query="nothing")
    assert got == {"ok": False, "reason": "I didn't find an attachment like that.", "question": ""}


async def test_messages_card(world):
    email = ma._Email("m", NOW - 60, sender="Sam", subject="Family", source="messages", chat="Family")
    email.files.append(ma._File("photo.heic", 1_500_000, message_id="55", source="messages", on_disk=True))
    world.emails.append(email)
    got = await sv.prepare(source="messages")
    assert got["card"] == "Save “photo.heic” (HEIC, 1.4 MB) from Sam's message in Family to Downloads"
    assert world.found_calls[0]["source"] == "messages"
    assert got["frozen"]["message_id"] == "55" and got["frozen"]["source"] == "messages"


async def test_card_is_cleaned_and_short(world):
    world.add("x.pdf", sender="Eve ‮<e@x.test>", subject="a“\x00b" + "z" * 500)
    got = await sv.prepare()
    assert len(got["card"]) <= 200 and "‮" not in got["card"] and "\x00" not in got["card"]
    assert got["card"].count("“") == 2


async def test_card_shows_folder_inside_documents(world):
    world.add("x.pdf")
    got = await sv.prepare(folder="Documents/Taxes")
    assert got["card"].endswith("to Documents/Taxes")


async def test_unfetchable_file_is_not_a_candidate(world):
    email = world.add("x.pdf")
    email.files[0].rowid = 0
    assert (await sv.prepare())["ok"] is False


# --- folder rules -----------------------------------------------------------------------

@pytest.mark.parametrize("spelling", ["Downloads", "downloads", "my Downloads folder", "Desktop", "Documents",
                                      "Documents/Taxes", "~/Documents/Taxes", "~/Downloads", None, ""])
def test_good_folders(home, spelling):
    real, why = sv.check_folder(spelling)
    assert real and not why and real.startswith(str(home))


def test_absolute_path_inside_home(home):
    assert sv.check_folder(str(home / "Documents" / "Taxes")) == (str(home / "Documents" / "Taxes"), "")


def test_bad_folders(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "Documents" / "escape").symlink_to(outside)
    (home / "Documents" / "intolib").symlink_to(home / "Library")
    (home / "Documents" / "file.txt").write_text("x")
    cases = [".hidden", "~/.hidden", "Documents/.hidden", "Library", "~/Library", str(home / "Library"),
             str(outside), "/tmp", "/", "/Applications", "/usr/local", "/System", "~/.ssh", "~/.Trash",
             "Documents/escape", "Documents/intolib", "Documents/file.txt", "Documents/nope", "Taxes",
             "Documents/../.hidden", "Documents/Thing.app", str(home), "~", "Documents/\x00x",
             "~/Library/Keychains"]
    for case in cases:
        real, why = sv.check_folder(case)
        assert real == "" and why, case
    assert not (home / "Documents" / "nope").exists()
    assert sv.check_folder("Library")[1] == "Wisp only saves into a folder in your home folder, like Downloads or Documents."


async def test_prepare_refuses_bad_folder_without_searching(world, home):
    world.add("x.pdf")
    got = await sv.prepare(folder="Library")
    assert got["ok"] is False and "home folder" in got["reason"] and got["question"] == ""
    assert not world.found_calls


def test_policy_helpers_are_used(home, monkeypatch):
    from service.safety import policy
    monkeypatch.setattr(policy, "credential_path_hit", lambda *a, **k: "x")
    assert sv.check_folder("Downloads")[0] == ""
    monkeypatch.undo()


# --- names ------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("report.pdf", "report.pdf"),
    ("../../etc/passwd", "passwd"),
    ("a/b/c.txt", "c.txt"),
    ("a\\b\\c.txt", "c.txt"),
    ("dir/", "dir"),
    ("..", "attachment"),
    ("", "attachment"),
    (None, "attachment"),
    ("   ", "attachment"),
    (".bashrc", "bashrc"),
    ("...hidden.pdf", "hidden.pdf"),
    ("  spaced .pdf", "spaced.pdf"),
    ("name.pdf. . ", "name.pdf"),
    ("a:b.pdf", "a-b.pdf"),
    ("tab\tin\x00name\x1b.txt", "tabinname.txt"),
    ("invoice‮gpj.exe", "invoicegpj.exe"),
    ("zero​width﻿.pdf", "zerowidth.pdf"),
    ("é.txt", "é.txt"),
])
def test_clean_name(raw, expected):
    assert sv.clean_name(raw) == expected


def test_rlo_cannot_disguise_the_extension():
    cleaned = sv.clean_name("annual‮fdp.exe")
    assert cleaned == "annualfdp.exe" and sv._extension(cleaned) == "exe"


def test_long_name_keeps_extension():
    cleaned = sv.clean_name("x" * 300 + ".pdf")
    assert len(cleaned) == 120 and cleaned.endswith(".pdf")
    wide = sv.clean_name("\U0001f600" * 200 + ".pdf")
    assert wide.endswith(".pdf") and len(wide.encode()) <= 200


# --- integrity --------------------------------------------------------------------------

@pytest.mark.parametrize("key,value", [("folder", "/tmp"), ("save_name", "other.pdf"), ("name", "other.pdf"),
                                       ("size", 1), ("rowid", 99), ("ts", 5.0), ("source", "messages"),
                                       ("check", "0" * 32)])
async def test_altered_frozen_is_refused(world, home, key, value):
    frozen = await frozen_of(world)
    bad = dict(frozen)
    bad[key] = value
    assert await sv.save(bad) == {"ok": False, "wrote": False, "reason": "That isn't the file you confirmed."}
    assert list_dir(home / "Downloads") == [] and world.fetches == 0


async def test_malformed_frozen_is_refused(world, home):
    frozen = await frozen_of(world)
    for bad in (None, {}, "x", [], {**frozen, "extra": 1}, {k: v for k, v in frozen.items() if k != "nth"},
                {**frozen, "from_me": 1}):
        got = await sv.save(bad)
        assert got["ok"] is False and got["wrote"] is False


async def test_frozen_from_another_process_key_is_refused(world, home, monkeypatch):
    frozen = await frozen_of(world)
    monkeypatch.setattr(sv, "_KEY", b"another key")
    assert (await sv.save(frozen))["reason"] == "That isn't the file you confirmed."


# --- save: the happy path and the file moving -------------------------------------------

async def test_save_writes_file(world, home):
    frozen = await frozen_of(world, "invoice-march.pdf")
    got = await sv.save(frozen)
    path = home / "Downloads" / "invoice-march.pdf"
    assert got == {"ok": True, "text": "Saved “invoice-march.pdf” to Downloads. Shown in Finder.",
                   "path": str(path)}
    assert path.read_bytes() == PDF
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    assert world.finder == [str(path)]
    call = world.found_calls[-1]
    assert call["name"] == "invoice-march.pdf" and call["source"] == "mail"
    assert abs((call["end"] - call["start"]) - 6 * 3600) < 1


async def test_save_to_documents_taxes(world, home):
    world.add("w2.pdf")
    got = await sv.prepare(folder="Documents/Taxes")
    done = await sv.save(got["frozen"])
    assert done["ok"] and (home / "Documents/Taxes/w2.pdf").exists()
    assert "to Documents/Taxes." in done["text"]


async def test_file_gone(world, home):
    frozen = await frozen_of(world)
    world.emails.clear()
    assert await sv.save(frozen) == {"ok": False, "wrote": False,
                                     "reason": "That file isn't there any more, so nothing was saved."}
    assert world.fetches == 0 and list_dir(home / "Downloads") == []


async def test_size_changed(world, home):
    frozen = await frozen_of(world)
    world.emails[0].files[0].size += 1
    assert (await sv.save(frozen))["reason"] == "That file isn't there any more, so nothing was saved."


async def test_other_email_same_name_is_not_the_same_file(world, home):
    frozen = await frozen_of(world)
    world.emails[0].subject = "Something else"
    world.emails[0].rfc = "<other@x.test>"
    assert (await sv.save(frozen))["ok"] is False
    assert list_dir(home / "Downloads") == []


async def test_matches_by_sender_and_subject_without_message_id(world, home):
    world.add("x.pdf", rfc="")
    frozen = (await sv.prepare())["frozen"]
    assert (await sv.save(frozen))["ok"] is True


async def test_unreadable_gives_its_sentence(world, home):
    frozen = await frozen_of(world)
    world.bytes = ma.Unreadable("Mail hasn't downloaded it to this Mac.")
    got = await sv.save(frozen)
    assert got == {"ok": False, "wrote": False, "reason": "Mail hasn't downloaded it to this Mac."}


async def test_empty_file_not_saved(world, home):
    frozen = await frozen_of(world)
    world.bytes = b""
    got = await sv.save(frozen)
    assert got["ok"] is False and list_dir(home / "Downloads") == []


async def test_folder_changed_since_the_card(world, home):
    frozen = await frozen_of(world)
    (home / "Downloads").rename(home / "Gone")
    (home / "Downloads").symlink_to(home / "Library")
    got = await sv.save(frozen)
    assert got["ok"] is False and got["wrote"] is False
    assert list_dir(home / "Library") == []


# --- risk (the real child) --------------------------------------------------------------

async def test_program_named_pdf_is_refused(world, home):
    frozen = await frozen_of(world, "invoice.pdf")
    world.bytes = MZ
    got = await sv.save(frozen)
    assert got["ok"] is False and got["wrote"] is False
    assert got["reason"].startswith("Wisp didn't save it: ")
    assert got["reason"].endswith(" Open the email in Mail if you still want it.")
    assert list_dir(home / "Downloads") == [] and world.finder == []


async def test_caution_is_saved_with_take_care(world, home, monkeypatch):
    frozen = await frozen_of(world)

    async def inspect(job, name, data, *, timeout, part=None):
        return {"risk": {"level": "caution", "reasons": ["It has macros that can run when opened"]}}

    monkeypatch.setattr(documents, "run_attachment_child", inspect)
    got = await sv.save(frozen)
    assert got["ok"] is True
    assert got["text"].endswith(" Take care: It has macros that can run when opened.")
    assert (home / "Downloads" / "invoice.pdf").exists()


async def test_inspect_failure_refuses(world, home, monkeypatch):
    frozen = await frozen_of(world)

    async def broken(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(documents, "run_attachment_child", broken)
    got = await sv.save(frozen)
    assert got["ok"] is False and list_dir(home / "Downloads") == []


async def test_real_pdf_passes_the_real_inspect(world, home):
    got = await sv.save(await frozen_of(world))
    assert got["ok"] is True


# --- writing ----------------------------------------------------------------------------

async def test_collisions_are_numbered_never_overwritten(world, home):
    original = home / "Downloads" / "invoice.pdf"
    original.write_bytes(b"MINE")
    frozen = await frozen_of(world)
    first = await sv.save(frozen)
    second = await sv.save(frozen)
    assert original.read_bytes() == b"MINE"
    assert first["path"].endswith("invoice (2).pdf") and second["path"].endswith("invoice (3).pdf")
    assert "invoice (2).pdf" in first["text"]
    assert list_dir(home / "Downloads") == ["invoice (2).pdf", "invoice (3).pdf", "invoice.pdf"]


async def test_symlink_at_destination_is_not_followed(world, home):
    target = home / "Documents" / "precious.txt"
    target.write_text("KEEP")
    (home / "Downloads" / "invoice.pdf").symlink_to(target)
    got = await sv.save(await frozen_of(world))
    assert got["ok"] is True and got["path"].endswith("invoice (2).pdf")
    assert target.read_text() == "KEEP"
    assert (home / "Downloads" / "invoice.pdf").is_symlink()


async def test_dangling_symlink_at_destination_is_not_followed(world, home):
    (home / "Downloads" / "invoice.pdf").symlink_to(home / "Documents" / "not-there.txt")
    got = await sv.save(await frozen_of(world))
    assert got["ok"] is True and got["path"].endswith("invoice (2).pdf")
    assert not (home / "Documents" / "not-there.txt").exists()


async def test_ninety_nine_copies_then_stops(world, home):
    for n in range(1, 100):
        (home / "Downloads" / sv._numbered("invoice.pdf", n)).write_bytes(b"x")
    got = await sv.save(await frozen_of(world))
    assert got["ok"] is False and got["wrote"] is False and len(list_dir(home / "Downloads")) == 99


async def test_partial_write_failure_leaves_nothing(world, home, monkeypatch):
    frozen = await frozen_of(world)
    real_write = os.write
    calls = []

    def flaky(fd, data):
        calls.append(1)
        if len(calls) == 1:
            return real_write(fd, bytes(data[:10]))
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "write", flaky)
    got = await sv.save(frozen)
    assert got["ok"] is False and got["wrote"] is False
    assert list_dir(home / "Downloads") == [] and world.finder == []


async def test_fsync_failure_leaves_nothing(world, home, monkeypatch):
    frozen = await frozen_of(world)

    def broken(fd):
        raise OSError(5, "io")

    monkeypatch.setattr(os, "fsync", broken)
    assert (await sv.save(frozen))["ok"] is False
    assert list_dir(home / "Downloads") == []


# --- quarantine -------------------------------------------------------------------------

def read_xattr(path) -> bytes | None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32,
                              ctypes.c_int]
    libc.getxattr.restype = ctypes.c_ssize_t
    buffer = ctypes.create_string_buffer(256)
    got = libc.getxattr(os.fsencode(path), b"com.apple.quarantine", buffer, 256, 0, 1)   # 1 = XATTR_NOFOLLOW
    return buffer.raw[:got] if got >= 0 else None


async def test_quarantine_attribute_is_on_the_saved_file(world, home, tmp_path):
    probe = tmp_path / "probe"
    probe.write_bytes(b"x")
    fd = os.open(probe, os.O_RDWR)
    try:
        supported = sv._set_quarantine(fd, b"0083;0;Wisp;")
    finally:
        os.close(fd)
    got = await sv.save(await frozen_of(world))
    if not supported:
        assert got["ok"] is False and got["reason"] == sv._NO_MARK     # refused, as designed
        assert list_dir(home / "Downloads") == []
        pytest.skip("this filesystem or platform refuses the quarantine extended attribute")
    assert got["ok"] is True
    value = read_xattr(got["path"])
    assert value is not None
    flags, stamp, agent, rest = value.decode().split(";")
    assert flags == "0083" and agent == "Wisp" and rest == ""
    assert abs(int(stamp, 16) - time.time()) < 120
    if sys.platform == "darwin":
        import subprocess
        out = subprocess.run(["/usr/bin/xattr", "-p", "com.apple.quarantine", got["path"]], capture_output=True,
                             text=True, timeout=10)
        assert out.stdout.strip() == value.decode()


async def test_no_quarantine_means_no_file(world, home, monkeypatch):
    frozen = await frozen_of(world)
    monkeypatch.setattr(sv, "_set_quarantine", lambda fd, value: False)
    got = await sv.save(frozen)
    assert got == {"ok": False, "wrote": False,
                   "reason": "Wisp couldn't mark the file as downloaded, so it didn't save it."}
    assert list_dir(home / "Downloads") == [] and world.finder == []


async def test_quarantine_is_set_before_the_final_mode(world, home, monkeypatch):
    frozen = await frozen_of(world)
    seen = {}

    def spy(fd, value):
        seen["mode"] = os.fstat(fd).st_mode & 0o777
        seen["value"] = value
        return True

    monkeypatch.setattr(sv, "_set_quarantine", spy)
    got = await sv.save(frozen)
    assert got["ok"] is True and seen["mode"] == 0o600
    assert seen["value"].startswith(b"0083;") and seen["value"].endswith(b";Wisp;")
    assert (os.stat(got["path"]).st_mode & 0o777) == 0o644


# --- Finder, output ---------------------------------------------------------------------

async def test_finder_failure_keeps_the_save(world, home):
    frozen = await frozen_of(world)
    world.finder_ok = False
    got = await sv.save(frozen)
    assert got["ok"] is True and "Shown in Finder" not in got["text"]
    assert got["text"] == "Saved “invoice.pdf” to Downloads."
    assert (home / "Downloads" / "invoice.pdf").exists()


def test_finder_call_is_fixed_argv_no_shell(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        raise OSError("blocked")

    monkeypatch.setattr(sv.subprocess, "run", run)
    assert sv._open_in_finder("/Users/x/Downloads/a.pdf") is False
    assert seen["argv"] == ["/usr/bin/open", "-R", "/Users/x/Downloads/a.pdf"]
    assert seen["shell"] is False and seen["timeout"] == 10 and seen["env"] == {"PATH": "/usr/bin:/bin"}


async def test_nothing_printed_or_logged(world, home, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    world.add("secret-lease.pdf", sender="Pat Private <pat@private.test>", subject="Confidential")
    got = await sv.prepare()
    await sv.save(got["frozen"])
    world.bytes = MZ
    await sv.save(got["frozen"])
    out = capsys.readouterr()
    for blob in (out.out, out.err, caplog.text):
        assert "secret-lease" not in blob and "Pat" not in blob and "private.test" not in blob
        assert str(home) not in blob


async def test_save_never_raises(world, home, monkeypatch):
    frozen = await frozen_of(world)

    async def boom(**kw):
        raise RuntimeError("x")

    monkeypatch.setattr(ma, "find", boom)
    got = await sv.save(frozen)
    assert got["ok"] is False and got["wrote"] is False
    assert (await sv.prepare())["ok"] is False


async def test_save_does_not_mutate_frozen(world, home):
    frozen = await frozen_of(world)
    before = copy.deepcopy(frozen)
    await sv.save(frozen)
    assert frozen == before
