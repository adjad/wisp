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
        try:        # an optional cross-check: the CI sandbox refuses to exec /usr/bin/xattr
            out = subprocess.run(["/usr/bin/xattr", "-p", "com.apple.quarantine", got["path"]],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return
        if out.returncode == 0:
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


# =========================================================================================
# The review's fixes: invisible characters, the card's destination, identity, fail closed, cancel
# =========================================================================================

import asyncio
import threading
import unicodedata

INVISIBLES = ["\u200b", "\u2060", "\u00ad", "\U000E0041", "\ufe0f", "\u180e", "\u3164", "\u200d"]


@pytest.mark.parametrize("invisible", INVISIBLES)
@pytest.mark.parametrize("suffix", ["command", "app", "sh", "scpt", "workflow", "terminal", "jar", "exe"])
async def test_an_invisible_character_cannot_hide_a_risky_suffix_from_the_save(world, home, suffix, invisible):
    name = f"Invoice.{suffix[:1]}{invisible}{suffix[1:]}"
    world.bytes = b"hello, a plain body"
    world.add(name)
    got = await sv.prepare()
    assert got["ok"] is True
    if unicodedata.category(invisible) in sv._DROPPED:       # some (variation selectors, fillers) stay in the name
        assert got["frozen"]["save_name"] == f"Invoice.{suffix}"
    done = await sv.save(got["frozen"])
    assert done["ok"] is False and done["wrote"] is False and done["reason"].startswith("Wisp didn't save it")
    assert list_dir(home / "Downloads") == [] and world.finder == []


async def test_a_harmless_name_with_an_invisible_character_is_saved_with_a_caution(world, home):
    world.add("Rep\u200bort.pdf")
    got = await sv.save((await sv.prepare())["frozen"])
    assert got["ok"] is True and "Take care: " in got["text"] and "hidden characters" in got["text"]
    assert list_dir(home / "Downloads") == ["Report.pdf"]


async def test_save_inspects_the_name_it_writes_and_the_name_it_was_given(world, home, monkeypatch):
    seen = []

    def verdict_for(high_name):
        async def inspect(job, name, data, *, timeout, part=None):
            seen.append(name)
            return {"risk": {"level": "high" if name == high_name else "", "reasons": ["It runs."]}}
        return inspect

    world.add("Invoice.comm\u200band")
    frozen = (await sv.prepare())["frozen"]
    for high_name in ("Invoice.command", "Invoice.comm\u200band"):     # the written name / the raw name
        seen.clear()
        monkeypatch.setattr(documents, "run_attachment_child", verdict_for(high_name))
        got = await sv.save(frozen)
        assert got["ok"] is False and got["reason"].startswith("Wisp didn't save it: It runs.")
        assert set(seen) == {"Invoice.command", "Invoice.comm\u200band"}
    assert list_dir(home / "Downloads") == []


async def test_a_caution_from_either_name_is_shown(world, home, monkeypatch):
    async def inspect(job, name, data, *, timeout, part=None):
        return {"risk": {"level": "caution" if "\u200b" in name else "", "reasons": ["Its name has odd characters"]}}

    monkeypatch.setattr(documents, "run_attachment_child", inspect)
    world.add("a\u200bb.pdf")
    got = await sv.save((await sv.prepare())["frozen"])
    assert got["ok"] is True and got["text"].endswith("Take care: Its name has odd characters.")


# --- the card -------------------------------------------------------------------------------

async def test_the_card_keeps_the_whole_destination_and_the_extension_whatever_is_long(world, home):
    deep = home / "Documents" / ("d" * 40) / ("e" * 40) / ("f" * 40) / "Taxes 2026"
    deep.mkdir(parents=True)
    relative = "Documents/" + "/".join(["d" * 40, "e" * 40, "f" * 40, "Taxes 2026"])
    world.add("N" * 300 + ".pdf", sender="S" * 300 + " <s@x.test>", subject="U" * 300)
    got = await sv.prepare(folder=relative)
    card = got["card"]
    assert len(card) <= 200
    assert card.endswith("to " + sv._fit_folder(relative)) and card.endswith("Taxes 2026")
    assert ".pdf” (PDF" in card and "…" in card
    assert card.count("“") == 2 and card.count("”") == 2


async def test_a_short_enough_deep_folder_is_whole(world, home):
    deep = home / "Documents" / ("a" * 30) / ("b" * 30)
    deep.mkdir(parents=True)
    relative = "Documents/" + "a" * 30 + "/" + "b" * 30
    world.add("N" * 300 + ".pdf", sender="S" * 300, subject="U" * 300)
    card = (await sv.prepare(folder=relative))["card"]
    assert len(card) <= 200 and card.endswith("to " + relative)


async def test_a_very_long_single_folder_name_still_ends_the_card(world, home):
    (home / "Documents" / ("z" * 200)).mkdir()
    world.add("x.pdf", sender="S" * 300, subject="U" * 300)
    card = (await sv.prepare(folder="Documents/" + "z" * 200))["card"]
    assert len(card) <= 200 and card.endswith("z" * 40) and "…" in card.split(" to ")[-1]


@pytest.mark.parametrize("length", [1, 5, 40, 90, 150, 300])
async def test_card_budget_holds_at_every_length(world, home, length):
    (home / "Documents" / "Taxes").mkdir(exist_ok=True)
    world.add("n" * length + ".pdf", sender="s" * length, subject="u" * length)
    card = (await sv.prepare(folder="Documents/Taxes"))["card"]
    assert len(card) <= 200 and card.endswith("to Documents/Taxes") and ".pdf”" in card


async def test_a_texted_file_card_keeps_the_destination_too(world, home):
    email = ma._Email("m", NOW - 60, sender="S" * 300, subject="x", source="messages", chat="C" * 300)
    email.files.append(ma._File("p" * 300 + ".heic", 1_500_000, message_id="55", source="messages", on_disk=True))
    world.emails.append(email)
    card = (await sv.prepare(source="messages"))["card"]
    assert len(card) <= 200 and card.endswith("to Downloads") and ".heic”" in card


def test_fit_name_cuts_the_stem_and_keeps_the_extension():
    assert sv._fit_name("abcdefghij.pdf", 8).endswith(".pdf") and len(sv._fit_name("abcdefghij.pdf", 8)) <= 8
    assert sv._fit_name("short.pdf", 20) == "short.pdf"


# --- identity between the card and the save -------------------------------------------------

async def test_a_renumbered_apple_mail_row_with_the_same_message_id_is_the_same_file(world, home):
    frozen = await frozen_of(world)
    world.emails[0].files[0].rowid = 9999
    world.emails[0].files[0].attachment_id = "renumbered"
    assert (await sv.save(frozen))["ok"] is True


async def test_a_changed_gmail_or_messages_id_is_not_the_same_file(world, home):
    email = world.add("g.pdf")
    email.files[0].gmail_account, email.files[0].gmail_message = "me@x.test", "m-1"
    email.files[0].gmail_attachment = "att-1"
    frozen = (await sv.prepare())["frozen"]
    assert frozen["gmail_message"] == "m-1"
    email.files[0].gmail_message = "m-2"
    assert (await sv.save(frozen))["reason"] == sv._GONE
    email.files[0].gmail_message = "m-1"
    email.files[0].gmail_account = "other@x.test"
    assert (await sv.save(frozen))["reason"] == sv._GONE
    assert list_dir(home / "Downloads") == [] and world.fetches == 0


async def test_without_a_message_id_a_changed_row_number_is_not_the_same_file(world, home):
    email = world.add("g.pdf", rfc="")
    email.files[0].gmail_account, email.files[0].gmail_message = "me@x.test", "m-1"
    email.files[0].gmail_attachment = "att-1"
    frozen = (await sv.prepare())["frozen"]
    email.files[0].rowid = 5          # Gmail identity is stable, but there is no Message-ID to excuse a new row
    assert (await sv.save(frozen))["reason"] == sv._GONE


async def test_messages_id_must_match(world, home):
    email = ma._Email("m", NOW - 60, sender="Sam", subject="Family", source="messages", chat="Family")
    email.files.append(ma._File("photo.heic", 1_500, message_id="55", source="messages", on_disk=True))
    world.emails.append(email)
    frozen = (await sv.prepare(source="messages"))["frozen"]
    email.files[0].message_id = "56"
    assert (await sv.save(frozen))["reason"] == sv._GONE


async def test_no_identity_and_a_look_alike_arrives_so_nothing_is_saved(world, home):
    world.add("x.pdf", rfc="", ts=1_700_000_000.2)
    frozen = (await sv.prepare())["frozen"]
    assert (frozen["rfc"], frozen["gmail_message"], frozen["message_id"]) == ("", "", "")
    twin = world.add("x.pdf", rfc="", ts=1_700_000_000.9)       # same second, sender, subject, name, size
    twin.files[0].rowid = 100
    got = await sv.save(frozen)
    assert got == {"ok": False, "wrote": False,
                   "reason": "More than one file looks like the one you confirmed, so nothing was saved."}
    assert world.fetches == 0 and list_dir(home / "Downloads") == []


async def test_no_identity_a_look_alike_in_another_second_is_not_a_twin(world, home):
    world.add("x.pdf", rfc="", ts=1_700_000_000.2)
    frozen = (await sv.prepare())["frozen"]
    world.add("x.pdf", rfc="", ts=1_700_000_001.1)
    assert (await sv.save(frozen))["ok"] is True


async def test_no_identity_another_sender_or_subject_is_not_a_twin(world, home):
    world.add("x.pdf", rfc="", ts=1_700_000_000.2)
    frozen = (await sv.prepare())["frozen"]
    world.add("x.pdf", rfc="", ts=1_700_000_000.4, sender="Someone Else <e@x.test>")
    world.add("x.pdf", rfc="", ts=1_700_000_000.5, subject="Different")
    assert (await sv.save(frozen))["ok"] is True


async def test_with_a_message_id_a_look_alike_without_one_does_not_confuse_the_save(world, home):
    frozen = await frozen_of(world)
    world.add("invoice.pdf", rfc="<other@x.test>")
    assert (await sv.save(frozen))["ok"] is True


async def test_a_forged_twin_with_the_same_message_id_and_another_sender_is_not_the_file(world, home):
    frozen = await frozen_of(world)
    real = world.emails.pop()
    twin = world.add("invoice.pdf", sender="Evil <evil@x.test>", ts=real.ts)       # same id, time, name, size
    twin.files[0].rowid = 7
    assert (await sv.save(frozen))["reason"] == sv._GONE
    assert world.fetches == 0 and list_dir(home / "Downloads") == []


async def test_a_forged_twin_with_another_subject_is_not_the_file(world, home):
    frozen = await frozen_of(world)
    real = world.emails.pop()
    world.add("invoice.pdf", subject="Payroll update", ts=real.ts)
    assert (await sv.save(frozen))["reason"] == sv._GONE


async def test_the_real_file_is_found_even_when_a_forged_twin_is_listed_first(world, home):
    frozen = await frozen_of(world)
    real = world.emails[0]
    twin = world.add("invoice.pdf", sender="Evil <evil@x.test>", ts=real.ts)
    world.emails.remove(twin)
    world.emails.insert(0, twin)
    assert (await sv.save(frozen))["ok"] is True


async def test_a_twin_identical_in_every_compared_field_is_refused_as_more_than_one(world, home):
    frozen = await frozen_of(world)
    twin = world.add("invoice.pdf", ts=world.emails[0].ts)
    twin.files[0].rowid = 7
    got = await sv.save(frozen)
    assert got == {"ok": False, "wrote": False, "reason": sv._AMBIGUOUS}
    assert world.fetches == 0 and list_dir(home / "Downloads") == []


async def test_a_renumbered_row_next_to_a_look_alike_is_refused_as_more_than_one(world, home):
    frozen = await frozen_of(world)
    world.emails[0].files[0].rowid = 9999
    twin = world.add("invoice.pdf", ts=world.emails[0].ts)
    twin.files[0].rowid = 4242
    assert (await sv.save(frozen))["reason"] == sv._AMBIGUOUS


async def test_a_name_over_300_characters_still_saves(world, home):
    name = "n" * 350 + ".pdf"
    frozen = await frozen_of(world, name)
    assert len(frozen["name"]) == 300 and frozen["name"].endswith(".pdf")
    got = await sv.save(frozen)
    assert got["ok"] is True, got


async def test_a_message_id_over_100_characters_still_saves(world, home):
    world.add("invoice.pdf", rfc="<" + "i" * 150 + "@x.test>")
    f = world.emails[0].files[0]
    f.message_id, f.gmail_message, f.gmail_account = "m" * 150, "g" * 400, "a" * 400
    got = await sv.prepare()
    assert got["ok"], got
    frozen = got["frozen"]
    assert len(frozen["message_id"]) == 100 and len(frozen["gmail_message"]) == 300
    saved = await sv.save(frozen)
    assert saved["ok"] is True, saved


# --- fail closed ----------------------------------------------------------------------------

@pytest.mark.parametrize("answer", [{}, {"warnings": []}, {"risk": None}, {"risk": "high"}, {"risk": {}},
                                    {"risk": {"level": "weird", "reasons": []}}, {"risk": {"level": 3}}, None, "x"])
async def test_an_answer_without_a_clear_risk_is_refused(world, home, monkeypatch, answer):
    frozen = await frozen_of(world)

    async def inspect(job, name, data, *, timeout, part=None):
        return answer

    monkeypatch.setattr(documents, "run_attachment_child", inspect)
    assert await sv.save(frozen) == {"ok": False, "wrote": False,
                                     "reason": "Wisp couldn't check the file, so it didn't save it."}
    assert list_dir(home / "Downloads") == []


async def test_a_folder_whose_real_path_cannot_be_read_back_is_refused(world, home, monkeypatch):
    frozen = await frozen_of(world)
    monkeypatch.setattr(sv, "_fd_path", lambda fd: "")
    got = await sv.save(frozen)
    assert got["ok"] is False and got["wrote"] is False and list_dir(home / "Downloads") == []


async def test_a_cancel_during_the_write_leaves_no_file(world, home, monkeypatch):
    frozen = await frozen_of(world)
    started, release = threading.Event(), threading.Event()
    real = sv._quarantine

    def slow(fd):
        started.set()
        release.wait(10)
        return real(fd)

    monkeypatch.setattr(sv, "_quarantine", slow)
    task = asyncio.ensure_future(sv.save(frozen))
    for _ in range(500):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set() and list_dir(home / "Downloads") != []        # the file exists mid-write
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(500):
        if list_dir(home / "Downloads") == []:
            break
        await asyncio.sleep(0.01)
    assert list_dir(home / "Downloads") == []


async def test_a_cancel_after_the_write_but_before_the_report_removes_the_file(world, home, monkeypatch):
    frozen = await frozen_of(world)
    started, release = threading.Event(), threading.Event()

    def reveal(path):
        started.set()
        release.wait(10)
        return True

    monkeypatch.setattr(sv, "_open_in_finder", reveal)
    task = asyncio.ensure_future(sv.save(frozen))
    for _ in range(500):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set() and list_dir(home / "Downloads") == ["invoice.pdf"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert list_dir(home / "Downloads") == []


async def test_a_folder_typed_in_another_letter_case_is_the_same_folder(world, home):
    if not (home / "downloads").exists():
        pytest.skip("this volume is case-sensitive")
    real, why = sv.check_folder(str(home / "downloads"))
    assert (real, why) == (str(home / "Downloads"), "")
    world.add("x.pdf")
    got = await sv.prepare(folder=str(home / "downloads"))
    assert got["ok"] is True and got["frozen"]["folder"] == str(home / "Downloads")
    done = await sv.save(got["frozen"])
    assert done["ok"] is True and list_dir(home / "Downloads") == ["x.pdf"]


def test_a_different_folder_is_still_refused(home):
    (home / "Documents" / "Downloads2").mkdir()
    assert sv.check_folder(str(home / "downloads2"))[0] == ""
    assert sv.check_folder(str(home / "Library" / "downloads"))[0] == ""
