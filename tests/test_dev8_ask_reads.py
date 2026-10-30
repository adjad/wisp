"""The rc1 Ask eval's remaining reads, on top of test_rc1_eval_reads.py: Apple
Mail's Sent mailbox through the app (view_emails mailbox='sent'), sizes and
dates through a whole tree (list_dir recursive details), word-wise ranked file
search with a top-N (find_files limit), work/personal addresses (wisp_status
accounts) and what's new from the bundled release notes (wisp_status
whats_new). All are reads on existing tools."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from service import gmail_source
from service.mail_memory import bridge
from service.tools import builtin, email_tools, files_tools, misc_t1
from service.tools.registry import REGISTRY


def test_the_reads_stay_on_existing_tools():
    assert "limit" in REGISTRY["find_files"].parameters["properties"]
    assert REGISTRY["find_files"].category == "fs_read"
    assert REGISTRY["view_emails"].category == "email_read"
    assert "Apple Mail" in REGISTRY["view_emails"].description


# -- Apple Mail sent mail ----------------------------------------------------

def test_bridge_sent_rows_keep_only_well_formed_header_fields():
    rows = bridge._sent_rows({"rows": [
        {"ts": 100.0, "account": "U1", "subject": "Hi", "rfc_message_id": "<a@b>",
         "to": ["Ana <ana@x.com>", 7, "", "b@x.com"], "recipients": 4, "raw_b64": "Ym9keQ=="},
        {"ts": "bad"}, {"ts": 0}, "junk",
        {"ts": 5, "to": "not a list", "recipients": True},
    ]})
    assert rows == [
        {"ts": 100.0, "account": "U1", "subject": "Hi", "rfc_message_id": "<a@b>",
         "to": ["Ana <ana@x.com>", "b@x.com"], "recipients": 4},
        {"ts": 5.0, "account": "", "subject": "", "rfc_message_id": "", "to": [], "recipients": 0},
    ]


def test_bridge_asks_for_sent_with_a_capped_window(monkeypatch):
    asked = {}

    async def fake_ask(kind, timeout, **fields):
        asked.update(kind=kind, **fields)
        return {"ok": True, "rows": [], "capped": True}
    monkeypatch.setattr(bridge, "_ask", fake_ask)
    assert asyncio.run(bridge.read_sent(400)) == ([], True)
    assert asked == {"kind": "sent", "days": bridge.SENT_DAYS_MAX}

    async def failing(kind, timeout, **fields):
        return {"ok": False, "error": "unreadable"}
    monkeypatch.setattr(bridge, "_ask", failing)
    with pytest.raises(bridge.BridgeError, match="unreadable"):
        asyncio.run(bridge.read_sent(3))


def test_a_sent_reply_is_accepted_only_for_a_sent_request():
    async def run():
        bridge._pending["r" * 24] = ("sent", asyncio.get_running_loop().create_future())
        try:
            assert not bridge.deliver({"request_id": "r" * 24, "kind": "index", "ok": True})
            assert bridge.deliver({"request_id": "r" * 24, "kind": "sent", "ok": True})
        finally:
            bridge._pending.pop("r" * 24, None)
    asyncio.run(run())


def _setup(monkeypatch, *, gmail=(), native=(), problem=None, capped=False, accounts=("me@gmail.com",)):
    monkeypatch.setattr(gmail_source, "connected_accounts", lambda cfg=None: {a: {} for a in accounts})
    monkeypatch.setattr(gmail_source, "sent_rows", lambda: [dict(r) for r in gmail])
    monkeypatch.setattr(email_tools, "native_email_accounts",
                        lambda: [{"id": "ICLOUD-UUID", "name": "iCloud"}, {"id": "WORK-UUID", "name": "Work"}])
    asked: list[int] = []

    async def read_sent(days, timeout=8.0):
        asked.append(days)
        if problem:
            raise bridge.BridgeError(problem)
        return [dict(r) for r in native], capped
    monkeypatch.setattr(bridge, "read_sent", read_sent)
    return asked


def _native(ts, subject, to=("ana@example.com",), account="ICLOUD-UUID", mid="", total=None):
    return {"ts": ts, "account": account, "subject": subject, "rfc_message_id": mid,
            "to": list(to), "recipients": len(to) if total is None else total}


def test_sent_mail_reads_apple_mail_without_gmail(monkeypatch):
    now = time.time()
    _setup(monkeypatch, accounts=(), native=[_native(now - 60, "Dinner Friday", ("Ana <ana@example.com>",)),
                                            _native(now - 7200, "Team update", ("a@x.com",) * 5, total=7)])
    out = asyncio.run(email_tools.view_emails(mailbox="sent"))
    assert "2 found" in out
    assert out.index("Dinner Friday") < out.index("Team update")
    assert "to Ana <ana@example.com>: Dinner Friday" in out
    assert "(+2 more)" in out                    # 7 recipients, 5 named
    assert "[from" not in out                    # one account: no labels


def test_sent_mail_merges_gmail_and_mail_and_shows_each_email_once(monkeypatch):
    now = time.time()
    _setup(monkeypatch,
           gmail=[{"ts": now - 300, "to": "bo@example.com", "subject": "Lease", "message_id": "<Lease-1@mail.gmail.com>",
                   "account": "me@gmail.com"}],
           native=[_native(now - 300, "Lease", ("bo@example.com",), account="GMAIL-IN-MAIL", mid="<lease-1@mail.gmail.com>"),
                   _native(now - 60, "Report", account="WORK-UUID")])
    out = asyncio.run(email_tools.sent_emails_view())
    assert "2 found" in out and out.count("Lease") == 1
    assert "[from Work]" in out and "[from me@gmail.com]" in out
    work = asyncio.run(email_tools.sent_emails_view(account="work"))
    assert "Report" in work and "Lease" not in work


def test_sent_mail_says_when_mail_couldn_t_be_read_but_gmail_could(monkeypatch):
    now = time.time()
    _setup(monkeypatch, problem="no_mail_index",
           gmail=[{"ts": now - 60, "to": "c@example.com", "subject": "Hi", "account": "me@gmail.com"}])
    out = asyncio.run(email_tools.sent_emails_view())
    assert "c@example.com" in out and "Apple Mail's Sent mailbox wasn't read" in out and "Full Disk Access" in out


def test_sent_mail_reaches_further_back_in_mail_than_gmail(monkeypatch):
    now = time.time()
    asked = _setup(monkeypatch, native=[_native(now - 20 * 86400, "Old plan")])
    out = asyncio.run(email_tools.sent_emails_view(period="last month"))
    assert asked and 20 < asked[0] <= bridge.SENT_DAYS_MAX
    assert "Gmail's sent mail goes back only" in out


def test_sent_mail_names_a_capped_mail_list(monkeypatch):
    _setup(monkeypatch, native=[_native(time.time() - 60, "One")], capped=True)
    assert "stops at its newest 1" in asyncio.run(email_tools.sent_emails_view())


def test_sent_mail_stays_outside_the_corpus_scope(monkeypatch):
    _setup(monkeypatch)
    token = email_tools._CORPUS_MAIL_IDENTIFIER.set("x")
    try:
        assert "outside the corpus scope" in asyncio.run(email_tools.view_emails(mailbox="sent"))
    finally:
        email_tools._CORPUS_MAIL_IDENTIFIER.reset(token)


# -- list_dir: sizes and dates through a tree ---------------------------------

def _tree(tmp_path: Path) -> Path:
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "a" / "one.bin").write_bytes(b"x" * 1000)
    (tmp_path / "a" / "b" / "two.bin").write_bytes(b"x" * 2000)
    (tmp_path / "a" / "b" / "c" / "deep.bin").write_bytes(b"x" * 4000)
    (tmp_path / "top.txt").write_text("hi")
    return tmp_path


def test_recursive_details_total_every_folder_and_list_nested_files(tmp_path):
    out = builtin.list_dir(str(_tree(tmp_path)), recursive=True, details=True, max_depth=2)
    lines = {line.split("  ")[0]: line for line in out.splitlines()}
    assert "7.0 KB" in lines["a/"]               # everything under a, at any depth
    assert "6.0 KB" in lines["a/b/"]
    assert "1.0 KB" in lines["a/one.bin"] and "modified" in lines["a/one.bin"]
    assert "a/b/two.bin" not in lines            # depth 3: past max_depth
    assert "at least" not in out


def test_recursive_details_sort_by_size_across_the_tree(tmp_path):
    out = builtin.list_dir(str(_tree(tmp_path)), recursive=True, details=True, sort="size")
    names = [line.split("  ")[0] for line in out.splitlines()]
    assert names[:4] == ["a/", "a/b/", "a/b/c/", "a/b/two.bin"]   # deep.bin is past depth 3
    assert names[-1] == "top.txt"


def test_non_recursive_details_still_list_one_level(tmp_path):
    out = builtin.list_dir(str(_tree(tmp_path)), details=True)
    assert [line.split("  ")[0] for line in out.splitlines()] == ["a/", "top.txt"]


def test_a_walk_out_of_budget_marks_only_unfinished_folders(tmp_path, monkeypatch):
    (tmp_path / "done").mkdir()
    (tmp_path / "done" / "f").write_text("x")
    (tmp_path / "zbig").mkdir()
    for i in range(20):
        (tmp_path / "zbig" / str(i)).write_text("x")
    monkeypatch.setattr(builtin, "_DETAIL_MAX_FILES", 8)
    out = builtin.list_dir(str(tmp_path), details=True)
    lines = {line.split("  ")[0]: line for line in out.splitlines() if not line.startswith("(")}
    assert "at least" in lines["zbig/"]
    assert out.splitlines()[-1].startswith('("at least"')


# -- find_files: words, limit, folders ---------------------------------------

def _mdfind(monkeypatch, hits):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "\n".join(hits), "")
    monkeypatch.setattr(subprocess, "run", run)
    return seen


def test_find_files_matches_each_word_in_any_order(monkeypatch):
    seen = _mdfind(monkeypatch, ["/tmp/2025 Tax Return.pdf"])
    files_tools.find_files('tax "return')
    expr = seen["argv"][-1]
    assert expr == 'kMDItemDisplayName == "*tax*"cd && kMDItemDisplayName == "*\\"return*"cd'


def test_name_rank_puts_the_phrase_above_words_in_any_order():
    assert files_tools._name_rank("/x/tax return.pdf", "tax return") == 0
    assert files_tools._name_rank("/x/Return of tax.pdf", "tax return") == 4
    assert files_tools._name_rank("/x/notes.txt", "tax return") == 5


def test_find_files_shows_a_ranked_top_n_and_how_many_more(monkeypatch):
    _mdfind(monkeypatch, [f"/tmp/report{i}.txt" for i in range(30)])
    out = files_tools.find_files("report", limit=5)
    assert len([line for line in out.splitlines() if line.startswith("/tmp/")]) == 5
    assert "+25 more" in out and "found 30 matches" in out
    capped = files_tools.find_files("report", limit=500)
    assert len([line for line in capped.splitlines() if line.startswith("/tmp/")]) == 30


def test_find_files_labels_a_folder_hit_as_a_folder(monkeypatch, tmp_path):
    (tmp_path / "Taxes").mkdir()
    (tmp_path / "taxes.pdf").write_bytes(b"x" * 3000)
    _mdfind(monkeypatch, [str(tmp_path / "Taxes"), str(tmp_path / "taxes.pdf")])
    out = files_tools.find_files("taxes")
    folder = next(line for line in out.splitlines() if line.endswith(")") and "/Taxes  " in line)
    assert "(folder, modified" in folder
    assert "3KB, modified" in out


# -- wisp_status accounts: work or personal ------------------------------------

def test_accounts_name_work_and_personal_addresses(monkeypatch):
    monkeypatch.setattr(gmail_source, "connected_accounts", lambda cfg=None: {"me@gmail.com": {}})
    monkeypatch.setattr(gmail_source, "has_calendar", lambda email, item=None: True)
    monkeypatch.setattr(email_tools, "native_email_accounts", lambda: [{"id": "1", "name": "Exchange"}])
    monkeypatch.setattr(email_tools, "get_identity_emails", lambda: ["pat@acme.com", "me@gmail.com", "pat@icloud.com"])
    out = misc_t1._accounts_report()
    assert "me@gmail.com: personal Google account; Gmail, Calendar" in out
    assert "Your addresses in Apple Mail: pat@acme.com (work or school), pat@icloud.com (personal)." in out
    assert "Work or school addresses (not a personal mail provider): pat@acme.com." in out


def test_accounts_say_when_no_address_is_a_work_one(monkeypatch):
    monkeypatch.setattr(gmail_source, "connected_accounts", lambda cfg=None: {"me@gmail.com": {}})
    monkeypatch.setattr(gmail_source, "has_calendar", lambda email, item=None: False)
    monkeypatch.setattr(email_tools, "native_email_accounts", lambda: [])
    monkeypatch.setattr(email_tools, "get_identity_emails", lambda: [])
    assert "None of these addresses is on a work or school domain" in misc_t1._accounts_report()


# -- what's new: bundled release notes ---------------------------------------

_NOTES = """**Wisp 0.5.0-dev8** is a development build.

## What's new

### A smoother first run

- **Move to Applications.** Opened from a download, Wisp asks once.
- **Open at login** moved to the Permissions step. More text.
  - A nested detail.
- **Three.** x
- **Four.** x
- **Five.** x
- **Six.** x

### The notch

- **The notch steps aside for full screen.** Detail.

## Fixes

- **Human-sounding replies turns on.** Detail.

## Known issues

- The notch hasn't been tried on a real notched Mac yet. More.
"""


def _resources(tmp_path: Path, version: str, names: list[str]) -> Path:
    res = tmp_path / "Resources"
    (res / "Documentation" / "releases").mkdir(parents=True)
    (res / "build-info.json").write_text(json.dumps({"version": version}))
    for name in names:
        (res / "Documentation" / "releases" / name).write_text(f"notes for {name}")
    return res


def test_release_notes_path_picks_this_version_s_newest_build(tmp_path):
    res = _resources(tmp_path, "0.5.0", ["0.4.5.md", "0.5.0-dev2.md", "0.5.0-dev10.md", "0.5.0-dev9.md", "1.2.0.md",
                                         "1.2.0-performance-waiver.json"])
    assert misc_t1.release_notes_path(res)[1] == "0.5.0-dev10"
    (res / "Documentation" / "releases" / "0.5.0-rc1.md").write_text("rc")
    assert misc_t1.release_notes_path(res)[1] == "0.5.0-rc1"
    (res / "Documentation" / "releases" / "0.5.0.md").write_text("final")
    assert misc_t1.release_notes_path(res)[1] == "0.5.0"


def test_release_notes_path_is_none_without_notes_for_this_version(tmp_path):
    assert misc_t1.release_notes_path(_resources(tmp_path, "0.6.0", ["0.5.0-dev8.md"])) is None


def test_release_notes_are_found_in_the_real_app_bundle_layout(tmp_path, monkeypatch):
    # The app ships this module at Resources/backend/service/tools/misc_t1.py, while
    # pipeline.py writes Resources/Documentation/releases and Resources/build-info.json.
    res = _resources(tmp_path, "0.5.0", ["0.5.0-dev7.md", "0.5.0-dev8.md"])
    module = res / "backend" / "service" / "tools" / "misc_t1.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    monkeypatch.setattr(misc_t1, "__file__", str(module))
    assert misc_t1._resources() == res.resolve()
    found = misc_t1.release_notes_path()
    assert found is not None and found[1] == "0.5.0-dev8"
    assert found[0] == (res / "Documentation" / "releases" / "0.5.0-dev8.md").resolve()


def test_release_notes_from_source_are_this_checkout_s_newest():
    found = misc_t1.release_notes_path()
    assert found is not None and found[0].parent.name == "releases"
    version = json.loads((Path(misc_t1.__file__).resolve().parents[2] / "build-support" / "toolchain.json")
                         .read_text())["version"]
    assert found[1].startswith(version)


def test_release_notes_headlines_group_and_cap(monkeypatch):
    monkeypatch.setattr(misc_t1, "_WHATS_NEW_PER_GROUP", 3)
    out = misc_t1.release_notes_headlines(_NOTES, "0.5.0-dev8")
    assert out.startswith("What's new in Wisp 0.5.0-dev8 (from its release notes")
    assert ("A smoother first run:\n- Move to Applications.\n- Open at login moved to the Permissions step.\n"
            "- Three.\nThe notch:\n- The notch steps aside for full screen.\nFixes:\n"
            "- Human-sounding replies turns on.\nKnown issues:\n"
            "- The notch hasn't been tried on a real notched Mac yet.\n(+3 more in the release notes.)") in out
    assert "nested" not in out and "Detail" not in out


def test_whats_new_prefers_release_notes_and_falls_back_to_the_changelog(monkeypatch, tmp_path):
    notes = tmp_path / "0.5.0-dev8.md"
    notes.write_text(_NOTES)
    monkeypatch.setattr(misc_t1, "release_notes_path", lambda resources=None: (notes, "0.5.0-dev8"))
    assert "Move to Applications" in misc_t1.whats_new()
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [0.5.0]\n\n### Added\n- **Changelog item.** x\n")
    monkeypatch.setattr(misc_t1, "release_notes_path", lambda resources=None: None)
    monkeypatch.setattr(misc_t1, "_changelog_path", lambda: changelog)
    assert "Changelog item" in misc_t1.whats_new()
    assert asyncio.run(REGISTRY["wisp_status"].func(topic="whats_new")).startswith("What's new in Wisp, [0.5.0]")


def test_the_app_bundles_this_version_s_release_notes():
    source = (Path(misc_t1.__file__).resolve().parents[2] / "build-support" / "pipeline.py").read_text()
    assert 'notes = docs / "releases"' in source and '(ROOT / "docs/releases").glob(meta["version"]' in source
