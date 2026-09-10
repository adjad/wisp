"""Synthetic privacy snapshots only; no source readers, sends, or user stores."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

_scratch = tempfile.TemporaryDirectory(prefix="wisp-privacy-import-")
os.environ["WISP_HOME"] = _scratch.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service.tools import cache_store, browser_history_tools as H, imessage_tools as M
from service.tools.action_tools import _resolve_recipient
from service.main import assistant_sync_browser_history, assistant_sync_messages


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_store, "CACHE_DIR", tmp_path / "cache")
    for name, value in dict(_revision=0, _applied_revision=0, _privacy_current=False, _enabled=None, _completed=set(),
                            _raw={b: "" for b in H._BROWSERS},
                            _synced_at={b: 0.0 for b in H._BROWSERS},
                            _available={b: False for b in H._BROWSERS},
                            _reason={b: "" for b in H._BROWSERS}).items():
        monkeypatch.setattr(H, name, value)
    for name, value in dict(_contacts={}, _name_handles={}, _birthdays={},
                            _contacts_revision=0, _contacts_applied_revision=0, _contacts_privacy_current=False, _lines="synthetic messages").items():
        monkeypatch.setattr(M, name, value)


def history(revision=1):
    return {"revision": revision, "enabled": True, "browsers": {
        b: {"lines": "1900000000 | example.test | /fixture | Synthetic page",
            "diagnostics": {"available": True}} for b in H._BROWSERS}}


def contacts(revision=1):
    return {"revision": revision, "contacts_enabled": True, "contacts_available": True,
            "contacts": {"+12025550123": "Fixture Person", "fixture@example.test": "Fixture Person"},
            "birthdays": {"01-02": ["Fixture Person"]}}


def post_history(body):
    return asyncio.run(assistant_sync_browser_history(body))


def post_contacts(body):
    return asyncio.run(assistant_sync_messages(body))


def assert_no_contacts():
    assert M.find_contacts("Fixture Person") == []
    assert M.resolve_contact("+12025550123") == "+12025550123"
    assert M.contact_names() == []
    assert M.birthdays_raw() == {}
    assert _resolve_recipient("Fixture Person")[0] == ""
    assert _resolve_recipient("Fixture Person", want_email=True)[0] == ""
    for name in ("contacts", "contact_handles", "birthdays"):
        assert cache_store.load(name) in ("", "{}")


def test_browser_disable_clears_disk_queries_and_late_payloads():
    post_history(history())
    assert len(H._all_rows()) == 2
    off = {"revision": 2, "enabled": False}
    assert post_history(off)["applied"]
    assert H.browser_history_sync_state() == "disabled"
    assert not H._all_rows()
    assert not any(H._raw.values())
    assert not any(H._synced_at.values())
    for b in H._BROWSERS:
        assert cache_store.load(f"browser_history_{b}") == ""
    assert not post_history(off)["applied"]
    assert not post_history(history())["applied"]
    assert not post_history({"enabled": True, "browser": "safari", "lines": "old"})["applied"]
    assert H.browser_history_sync_state() == "disabled"
    post_history(history(3))
    assert len(H._all_rows()) == 2


@pytest.mark.parametrize("available", [True, False])
def test_browser_authoritative_empty_or_unavailable_removes_previous_rows(available):
    post_history(history())
    body = history(2)
    body["browsers"]["safari"] = {"lines": "", "diagnostics": {"available": available}}
    post_history(body)
    assert [r["browser"] for r in H._all_rows()] == ["chrome"]
    assert cache_store.load("browser_history_safari") == ""


@pytest.mark.parametrize("clear", [
    {"contacts_enabled": False, "contacts_available": True},
    {"contacts_enabled": True, "contacts_available": False},
    {"contacts_enabled": False, "contacts_available": False},
    {"contacts_enabled": True, "contacts_available": True, "contacts": {}, "birthdays": {}},
])
def test_contacts_disabled_revoked_failed_or_empty_clears_all_consumers(clear):
    post_contacts(contacts())
    assert _resolve_recipient("Fixture Person")[0] == "+12025550123"
    body = {"revision": 2, **clear}
    assert post_contacts(body)["applied"]
    assert_no_contacts()
    assert M._lines == "synthetic messages"
    assert not post_contacts(body)["applied"]
    assert not post_contacts(contacts())["applied"]
    assert not post_contacts({"contacts": {"+12025550123": "Fixture Person"}})["applied"]
    assert_no_contacts()
    post_contacts(contacts(3))
    assert M.find_contacts("Fixture Person")


def test_birthdays_without_contact_handles_are_authoritative():
    body = contacts()
    body["contacts"] = {}
    post_contacts(body)
    assert M.contact_names() == []
    assert M.birthdays_raw() == {"01-02": ["Fixture Person"]}


def test_legacy_empty_contacts_clears_and_messages_push_does_not_touch_contacts():
    post_contacts({"contacts": contacts()["contacts"], "birthdays": contacts()["birthdays"]})
    post_contacts({"lines": "new synthetic messages", "diagnostics": {"available": True}})
    assert M.find_contacts("Fixture Person")
    post_contacts({"contacts": {}})
    assert_no_contacts()
    assert M._lines == "new synthetic messages"


@pytest.mark.parametrize("source", ["history", "contacts"])
def test_disk_clear_failure_still_denies_queries_and_allows_retry(monkeypatch, source):
    post = post_history if source == "history" else post_contacts
    post(history() if source == "history" else contacts())
    body = {"revision": 2, "enabled": False} if source == "history" else {
        "revision": 2, "contacts_enabled": False, "contacts_available": False}
    with monkeypatch.context() as failed:
        failed.setattr(Path, "unlink", lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic denied unlink")))
        with pytest.raises(OSError):
            post(body)
        assert not H._all_rows() if source == "history" else not M.find_contacts("Fixture Person")
    assert post(body)["applied"]
    if source == "contacts":
        assert_no_contacts()


@pytest.mark.parametrize("source", ["history", "contacts"])
def test_failed_ordering_fence_persistence_is_fail_closed(monkeypatch, source):
    with monkeypatch.context() as failed:
        failed.setattr(cache_store, "save", lambda *args: None)
        with pytest.raises(OSError):
            (post_history(history()) if source == "history" else post_contacts(contacts()))
    assert not H._all_rows() if source == "history" else not M.find_contacts("Fixture Person")


@pytest.mark.parametrize("with_fence", [False, True])
def test_relaunch_never_restores_legacy_or_pre_revocation_personal_data(tmp_path, with_fence):
    home = tmp_path / "relaunch"
    cache = home / "cache"
    cache.mkdir(parents=True)
    for name, data in {
        "contacts": json.dumps({"2025550123": "Fixture Person"}),
        "contact_handles": json.dumps({"fixture person": ["+12025550123"]}),
        "birthdays": json.dumps({"01-02": ["Fixture Person"]}),
        "browser_history_safari": "1900000000 | example.test | /fixture | Synthetic page",
    }.items():
        (cache / f"{name}.txt").write_text(data)
    if with_fence:
        (cache / "browser_history_privacy.txt").write_text(json.dumps({"revision": 2, "enabled": False}))
        (cache / "contacts_privacy_revision.txt").write_text("2")
    code = '''
from service.tools import browser_history_tools as H, imessage_tools as M
from service.tools.action_tools import _resolve_recipient
assert H._all_rows() == [] and not any(H._raw.values())
assert M.contact_names() == [] and M.birthdays_raw() == {}
assert _resolve_recipient("Fixture Person")[0] == ""
'''
    if with_fence:
        code += '''
assert H.browser_history_sync_state() == "disabled"
assert not H.apply_browser_history_sync({"revision": 1, "enabled": True})
assert not M.apply_contacts_sync({"revision": 1, "contacts": {"+12025550123": "Fixture Person"}})
'''
    subprocess.run([sys.executable, "-c", code], env={**os.environ, "WISP_HOME": str(home)}, check=True)


@pytest.mark.parametrize("post,body", [
    (post_history, {"revision": 1, "enabled": "false"}),
    (post_history, {"revision": True, "enabled": False}),
    (post_history, {"revision": 1, "enabled": True, "browsers": {}}),
    (post_contacts, {"revision": 1, "contacts_enabled": "false", "contacts_available": True}),
    (post_contacts, {"revision": 1, "contacts": {}}),
    (post_contacts, {"revision": 1, "contacts_enabled": True, "contacts_available": True}),
])
def test_malformed_privacy_contract_rejected(post, body):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        post(body)
    assert error.value.status_code == 422


@pytest.mark.parametrize("source", ["history", "contacts"])
def test_failed_newer_clear_fences_intervening_older_enabled_snapshot(monkeypatch, source):
    post, payload = (post_history, history) if source == "history" else (post_contacts, contacts)
    post(payload(10))
    clear = {"revision": 12, "enabled": False} if source == "history" else {
        "revision": 12, "contacts_enabled": False, "contacts_available": False}
    with monkeypatch.context() as failed:
        failed.setattr(Path, "unlink", lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic failure")))
        with pytest.raises(OSError):
            post(clear)
        assert not post(payload(11))["applied"]
        assert not H._all_rows() if source == "history" else not M.find_contacts("Fixture Person")
    assert post(clear)["applied"]
    assert not post(clear)["applied"]


@pytest.mark.parametrize("available", [False, True])
def test_full_browser_clear_disk_failure_cannot_leave_other_browser_queryable(monkeypatch, available):
    post_history(history(10))
    body = history(12)
    for entry in body["browsers"].values():
        entry["lines"] = ""
        entry["diagnostics"]["available"] = available
    with monkeypatch.context() as failed:
        failed.setattr(Path, "unlink", lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic failure")))
        with pytest.raises(OSError):
            post_history(body)
        assert not H._all_rows()
        assert not any(H._raw.values())
        assert not post_history(history(11))["applied"]
    assert post_history(body)["applied"]
    assert not H._all_rows()


@pytest.mark.parametrize("source", ["history", "contacts"])
def test_backend_restart_same_revision_is_not_fresh_and_new_snapshot_recovers(tmp_path, source):
    post, payload = (post_history, history) if source == "history" else (post_contacts, contacts)
    assert post(payload(10))["current"]
    home = cache_store.CACHE_DIR.parent
    code = '''
import asyncio, json, sys
from service.main import assistant_sync_browser_history, assistant_sync_messages
from service.main import assistant_browser_history_privacy_status, assistant_contacts_privacy_status
source, body = sys.argv[1], json.loads(sys.argv[2])
post = assistant_sync_browser_history if source == "history" else assistant_sync_messages
status = assistant_browser_history_privacy_status if source == "history" else assistant_contacts_privacy_status
assert asyncio.run(status()) == {"ok": True, "revision": 10, "current": False}
assert asyncio.run(post(body)) == {"ok": True, "applied": False, "revision": 10, "current": False}
body["revision"] = 11
assert asyncio.run(post(body)) == {"ok": True, "applied": True, "revision": 11, "current": True}
assert asyncio.run(post(body)) == {"ok": True, "applied": False, "revision": 11, "current": True}
'''
    subprocess.run([sys.executable, "-c", code, source, json.dumps(payload(10))],
                   env={**os.environ, "WISP_HOME": str(home)}, check=True)
