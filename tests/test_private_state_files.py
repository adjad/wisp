"""Wisp's personal-data state files are owner-only (0600) from the first byte.

#459 made the SQLite stores 0600; these are the JSON, text and log files beside
them that were still written with the process umask (0644 in a 0755 ~/.moe),
or written 0644 and chmod-ed afterwards. Each test runs under a 022 umask with
every path redirected into tmp_path: no model, network or user data.
"""
import importlib.util
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from service import paths

ROOT = Path(__file__).resolve().parents[1]


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture(autouse=True)
def loose_umask():
    old = os.umask(0o022)          # what a normal login shell hands the app
    try:
        yield
    finally:
        os.umask(old)


def leftovers(directory: Path, keep: set[str]) -> list[str]:
    return sorted(name for name in os.listdir(directory) if name not in keep)


# ── service.paths helpers ─────────────────────────────────────────────────────

def test_private_write_bytes_is_owner_only_atomic_and_replaces_a_loose_file(tmp_path):
    target = tmp_path / "new-dir" / "state.json"
    paths.private_write_bytes(target, b"one")
    assert mode(target) == 0o600 and mode(target.parent) == 0o700
    os.chmod(target, 0o644)                                   # an older build's file
    paths.private_write_bytes(target, b"two")
    assert mode(target) == 0o600 and target.read_bytes() == b"two"
    assert leftovers(target.parent, {"state.json"}) == []     # no temp file left


def test_private_write_bytes_replaces_a_symlink_without_touching_its_target(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    os.chmod(outside, 0o644)
    link = tmp_path / "state.json"
    link.symlink_to(outside)
    paths.private_write_bytes(link, b"mine")
    assert not link.is_symlink() and mode(link) == 0o600 and link.read_bytes() == b"mine"
    assert outside.read_text() == "keep" and mode(outside) == 0o644


def test_private_append_text_creates_private_and_tightens_an_old_log(tmp_path):
    log = tmp_path / "audit.jsonl"
    paths.private_append_text(log, "a\n")
    assert mode(log) == 0o600
    os.chmod(log, 0o644)
    paths.private_append_text(log, "b\n")
    assert mode(log) == 0o600 and log.read_text() == "a\nb\n"
    outside = tmp_path / "outside.log"
    outside.write_text("")
    link = tmp_path / "linked.jsonl"
    link.symlink_to(outside)
    with pytest.raises(OSError):
        paths.private_append_text(link, "x\n")
    assert outside.read_text() == ""


def test_tighten_private_file_refuses_links_and_non_files(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("x")
    os.chmod(outside, 0o644)
    link = tmp_path / "link"
    link.symlink_to(outside)
    paths.tighten_private_file(link)
    assert mode(outside) == 0o644                              # not followed
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o644)
    paths.tighten_private_file(fifo)                          # returns, does not hang
    assert mode(fifo) == 0o644
    paths.tighten_private_file(tmp_path / "missing")          # fine


# ── item 4: private_sqlite_file no longer follows symlinked sidecars ─────────

def test_private_sqlite_file_does_not_follow_a_symlinked_sidecar(tmp_path):
    db = tmp_path / "store.db"
    sqlite3.connect(db).close()
    os.chmod(db, 0o644)
    outside = tmp_path / "someone-elses-file"
    outside.write_text("x")
    os.chmod(outside, 0o644)
    (tmp_path / "store.db-wal").symlink_to(outside)
    paths.private_sqlite_file(db)
    assert mode(db) == 0o600                                  # the real file is tightened
    assert mode(outside) == 0o644                             # the link target is not


def test_private_sqlite_file_does_not_follow_a_symlinked_database(tmp_path):
    outside = tmp_path / "elsewhere.db"
    sqlite3.connect(outside).close()
    os.chmod(outside, 0o644)
    db = tmp_path / "store.db"
    db.symlink_to(outside)
    paths.private_sqlite_file(db)                             # logged, not raised
    assert mode(outside) == 0o644
    dangling = tmp_path / "dangling.db"
    dangling.symlink_to(tmp_path / "nowhere.db")
    paths.private_sqlite_file(dangling)
    assert not (tmp_path / "nowhere.db").exists()             # not created through the link


def test_behavior_store_local_helper_does_not_follow_a_symlinked_sidecar(tmp_path):
    from service.behavior import store as behavior_store
    db = tmp_path / "behavior.db"
    sqlite3.connect(db).close()
    os.chmod(db, 0o644)
    outside = tmp_path / "outside"
    outside.write_text("x")
    os.chmod(outside, 0o644)
    (tmp_path / "behavior.db-shm").symlink_to(outside)
    behavior_store._private_sqlite_file(str(db))
    assert mode(db) == 0o600 and mode(outside) == 0o644


# ── item 1: ~/.moe/checkins.json ─────────────────────────────────────────────

def test_checkins_state_is_private_atomic_and_an_old_file_is_tightened(tmp_path, monkeypatch):
    from service.assistant import checkin
    state = tmp_path / "moe" / "checkins.json"
    monkeypatch.setattr(checkin, "STATE_PATH", state)
    state.parent.mkdir()
    legacy = state.with_suffix(".json.tmp")                   # an older build's torn write
    legacy.write_text("{}")
    os.chmod(legacy, 0o644)
    checkin._update_day("2026-11-04", lambda record: record["decisions"].append({"id": "t1"}))
    assert mode(state) == 0o600
    assert json.loads(state.read_text())["days"]["2026-11-04"]["decisions"] == [{"id": "t1"}]
    assert leftovers(state.parent, {"checkins.json"}) == []
    os.chmod(state, 0o644)                                    # what a build before this left
    assert checkin.day_record("2026-11-04")["decisions"] == [{"id": "t1"}]
    assert mode(state) == 0o600                               # tightened on the next read


# ── items in the same family found by the scan ───────────────────────────────

def test_audit_log_is_owner_only_and_an_old_log_is_tightened(tmp_path, monkeypatch):
    import importlib
    audit = importlib.import_module("service.safety.audit")   # the package re-exports the function
    log = tmp_path / "moe" / "audit.jsonl"
    monkeypatch.setattr(audit, "AUDIT_LOG", log)
    audit.audit("confirm_deny", tool="send_message", args={"to": "Mom"})
    assert mode(log) == 0o600 and json.loads(log.read_text())["tool"] == "send_message"
    os.chmod(log, 0o644)
    audit.audit("allow", tool="web_fetch")
    assert mode(log) == 0o600 and len(log.read_text().splitlines()) == 2
    outside = tmp_path / "outside.log"
    outside.write_text("")
    linked = tmp_path / "linked.jsonl"
    linked.symlink_to(outside)
    monkeypatch.setattr(audit, "AUDIT_LOG", linked)
    monkeypatch.setattr(audit, "_warned", False)
    audit.audit("allow", tool="web_fetch")                    # does not raise
    assert outside.read_text() == "" and audit._warned


def test_grants_file_is_owner_only_and_tightened_on_load(tmp_path, monkeypatch):
    from service.publication_authority import Authority
    from service.safety import grants
    path = tmp_path / "grants.json"
    monkeypatch.setattr(grants, "GRANTS_PATH", path)
    monkeypatch.setattr(grants, "_CACHE", {})
    authority = Authority("test-private-grants")              # never the process's own
    authority.publish()
    monkeypatch.setattr(grants, "_publication", authority)
    monkeypatch.setattr(grants, "_proof_known", True)
    assert grants.grant("web_fetch", {"url": "https://example.com/a"}).get("ok") is True
    assert mode(path) == 0o600 and "web_fetch" in json.loads(path.read_text())
    assert grants.publication_witness() is not None           # proof still follows a good save
    os.chmod(path, 0o644)
    grants._load()
    assert mode(path) == 0o600


def test_brief_state_is_owner_only(tmp_path, monkeypatch):
    from datetime import date
    from service.assistant import scheduler
    monkeypatch.setattr(paths, "MOE_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "_last_brief", None)
    monkeypatch.setattr(scheduler, "_last_brief_loaded", False)
    scheduler._record_brief_date(date(2026, 11, 4))
    state = tmp_path / "brief_state.json"
    assert mode(state) == 0o600 and json.loads(state.read_text()) == {"last_brief": "2026-11-04"}
    os.chmod(state, 0o644)                                    # an older build's file
    monkeypatch.setattr(scheduler, "_last_brief_loaded", False)
    assert scheduler._brief_date() == date(2026, 11, 4)
    assert mode(state) == 0o600                               # tightened when read


@pytest.fixture
def creation_modes(monkeypatch):
    """The mode of every file created through os.open, at the moment it is created.

    Empty when a writer creates its file some other way (open()/write_text with
    the umask), which is exactly the old 0644-then-chmod pattern."""
    seen = []
    real_open = os.open

    def watching_open(path, flags, mode_arg=0o777, *args, **kwargs):
        fd = real_open(path, flags, mode_arg, *args, **kwargs)
        if flags & os.O_CREAT:
            seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd

    monkeypatch.setattr(os, "open", watching_open)
    return seen


def test_contact_and_message_caches_are_never_briefly_loose(tmp_path, monkeypatch, creation_modes):
    from service.tools import cache_store
    creation_modes.clear()                # only what the write below creates
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    legacy = tmp_path / "cache" / "contacts.tmp"              # an older build's torn write
    legacy.write_text("Mom: 555-0100")
    os.chmod(legacy, 0o644)
    cache_store.save("contacts", "Mom: 555-0100")
    assert creation_modes and set(creation_modes) == {0o600}
    assert mode(tmp_path / "cache" / "contacts.txt") == 0o600
    assert cache_store.load("contacts") == "Mom: 555-0100"
    assert leftovers(tmp_path / "cache", {"contacts.txt"}) == []


def test_timer_store_is_never_briefly_loose(tmp_path, monkeypatch, creation_modes):
    from service.tools import timers_alarms
    creation_modes.clear()                # only what the write below creates
    store = tmp_path / "cache" / "timers.json"
    monkeypatch.setattr(timers_alarms, "_STORE", store)
    monkeypatch.setattr(timers_alarms, "_TIMERS", {"t1": {"label": "pasta"}})
    timers_alarms._save()
    assert creation_modes and set(creation_modes) == {0o600}
    assert mode(store) == 0o600 and json.loads(store.read_text()) == {"t1": {"label": "pasta"}}


def test_research_cache_entries_are_never_briefly_loose(tmp_path, monkeypatch, creation_modes):
    from service.research import cache
    creation_modes.clear()                # only what the write below creates
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "research_cache")
    cache.put("https://example.com/a", url="https://example.com/a", title="A", text="body",
              content_type="text/html", published_at="")
    assert creation_modes and set(creation_modes) == {0o600}
    (entry,) = (tmp_path / "research_cache").iterdir()
    assert mode(entry) == 0o600 and cache.get("https://example.com/a")["title"] == "A"


# ── item 3: developer scripts that write real-usage output ───────────────────

def _load_script(name, monkeypatch):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_wisp_script_{name}", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)    # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


def test_eval_log_is_owner_only_in_a_new_private_dir(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    eval_core = _load_script("eval_core", monkeypatch)
    log = tmp_path / "evals" / "ling_eval.jsonl"
    eval_core.append_log({"id": "t1", "prompt": "text Mom"}, log)
    assert mode(log.parent) == 0o700 and mode(log) == 0o600
    os.chmod(log, 0o644)
    eval_core.append_log({"id": "t2"}, log)
    assert mode(log) == 0o600 and set(eval_core.load_log(log)) == {"t1", "t2"}


@pytest.mark.parametrize("script", ["replay_prompts", "harvest_training_data"])
def test_script_outputs_are_owner_only(script, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    module = _load_script(script, monkeypatch)
    out = tmp_path / "out.json"
    with module._open_private(str(out) if script == "replay_prompts" else out) as handle:
        handle.write("[]")
    assert mode(out) == 0o600
    os.chmod(out, 0o644)
    with module._open_private(out, "a") as handle:
        handle.write("\n")
    assert mode(out) == 0o600 and out.read_text() == "[]\n"
    chosen = tmp_path / "chosen.json"                         # a link the developer set up
    chosen.write_text("")
    os.chmod(chosen, 0o644)
    link = tmp_path / "linked.json"
    link.symlink_to(chosen)
    with module._open_private(link) as handle:
        handle.write("{}")
    assert chosen.read_text() == "{}" and mode(chosen) == 0o600
    with module._open_private(Path(os.devnull)) as handle:    # not a regular file: never re-moded
        handle.write("x")


def test_harvest_creates_a_new_output_dir_private():
    # main() needs a live model; the call it uses is checked here, and
    # private_mkdirs itself below.
    source = (ROOT / "scripts" / "harvest_training_data.py").read_text()
    assert "private_mkdirs(out_dir)" in source
    assert '.open(mode)' not in source


def test_every_new_parent_directory_is_private(tmp_path):
    # batch 10 Auditor note (b): mkdir(mode=0o700, parents=True) gives 0700 to
    # the last directory only; the parents it creates got 0755.
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o755)
    os.chmod(existing, 0o755)
    deep = existing / "a" / "b" / "c"
    paths.private_mkdirs(deep)
    for directory in (existing / "a", existing / "a" / "b", deep):
        assert mode(directory) == 0o700, directory
    assert mode(existing) == 0o755                            # an existing one is left alone
    paths.private_write_bytes(tmp_path / "w1" / "w2" / "state.json", b"x")
    paths.private_append_text(tmp_path / "l1" / "l2" / "audit.jsonl", "x\n")
    for directory in ("w1", "w1/w2", "l1", "l1/l2"):
        assert mode(tmp_path / directory) == 0o700, directory


def test_eval_log_parents_are_all_private(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    eval_core = _load_script("eval_core", monkeypatch)
    log = tmp_path / "moe" / "evals" / "ling_eval.jsonl"
    eval_core.append_log({"id": "t1"}, log)
    assert mode(tmp_path / "moe") == 0o700 and mode(log.parent) == 0o700


# ── temp files a write left by dying before its rename ──────────────────────

def test_private_write_bytes_cleans_up_its_temp_when_the_rename_fails(tmp_path, monkeypatch):
    target = tmp_path / "state.json"

    def failing_replace(src, dst):
        raise OSError("injected")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError, match="injected"):
        paths.private_write_bytes(target, b"x")
    assert os.listdir(tmp_path) == []


def test_stale_temps_are_swept_by_the_next_write_but_live_ones_are_not(tmp_path):
    target = tmp_path / "contacts.txt"
    stale = tmp_path / ".contacts.txt.111.deadbeef.tmp"
    live = tmp_path / ".contacts.txt.222.cafebabe.tmp"
    for leftover in (stale, live):
        leftover.write_bytes(b"old copy")
    old = os.stat(stale).st_mtime - paths.STALE_TEMP_S - 60
    os.utime(stale, (old, old))
    paths.private_write_bytes(target, b"new")
    assert not stale.exists() and live.exists()               # a concurrent write is left alone
    assert paths.private_temp_files(target) == [live]


def test_privacy_clears_remove_leftover_temp_copies(tmp_path, monkeypatch):
    from service.tools import browser_history_tools, cache_store, imessage_tools
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path)
    for name in ("contacts.txt", ".contacts.txt.1.aa.tmp", "browser_history_safari.txt",
                 ".browser_history_safari.txt.1.bb.tmp"):
        (tmp_path / name).write_text("private")
    monkeypatch.setattr(imessage_tools, "_contacts", {})
    monkeypatch.setattr(imessage_tools, "_name_handles", {})
    monkeypatch.setattr(imessage_tools, "_birthdays", {})
    imessage_tools.clear_contacts()
    browser_history_tools._delete_history_files(["safari"])
    assert os.listdir(tmp_path) == []


def test_research_purge_removes_stale_temp_copies(tmp_path, monkeypatch):
    from service.research import cache
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    stale = tmp_path / ".abc.json.1.aa.tmp"
    stale.write_text("{}")
    old = os.stat(stale).st_mtime - paths.STALE_TEMP_S - 60
    os.utime(stale, (old, old))
    cache.purge_older_than()
    assert not stale.exists()
