"""Let Wisp act for me (service/proactive/acting.py): the one gate, the record of what Wisp
did, and the "Wisp did this" card with Undo. Also the worked example for a new kind of action."""
from __future__ import annotations

import asyncio

import pytest

from service import config
from service.proactive import acting, autoadd, executor
from service.proactive.store import ProactiveStore

NOW = 1_800_000_000.0

# Texts in the shared helpers come from one saved contact's one-to-one chat (the autouse fixture
# that says so lives with the helpers and applies here by being imported).
from tests.test_proactive_auto_add import saved_contacts  # noqa: E402,F401


@pytest.fixture
def setting(tmp_path, monkeypatch):
    """The gate's own file, in this test's folder. Returns its path; `read(path)` is what is on disk."""
    path = tmp_path / "home" / acting.FILE
    monkeypatch.setattr(acting, "_path", lambda: path)
    # These tests use a made-up clock (NOW); the file's real creation time has its own test.
    monkeypatch.setattr(acting, "_born", lambda: 0.0)
    monkeypatch.setattr(acting, "_revoked", False)                       # restored after each test
    from service import idle
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    # Proving "the caller is the Wisp app" needs the real socket table; its own test replaces this.
    from service.proactive import api
    monkeypatch.setattr(api, "_require_app", lambda request: None)
    return path


def read(path):
    import json
    return json.loads(path.read_text()) if path.exists() else {}


def notes(setting):
    """Where "Not now" is kept, beside the gate's file."""
    return setting.with_name(acting.ASKED_FILE)


def _write(setting, **fields):
    setting.parent.mkdir(parents=True, exist_ok=True)
    setting.write_text(__import__("json").dumps(fields))


@pytest.fixture
def store(tmp_path):
    return ProactiveStore(tmp_path / "proactive")


def test_off_until_a_yes_and_the_yes_is_dated(setting):
    assert acting.state() == {"enabled": False, "enabled_at": None, "asked": False, "per_day": acting.MAX_PER_DAY,
                              "does": acting.DOES, "never": acting.NEVER, "scope": acting.SCOPE,
                              "agreed": [], "new": []}
    assert "after you say yes" in acting.SCOPE and "already there" in acting.SCOPE
    assert acting.DOES and "never sends" in acting.NEVER
    assert not acting.allows(NOW, "add") and acting.still_ok("add") == "Acting for you was turned off"
    acting.set_enabled(True, now=NOW)
    assert read(setting) == {"enabled": True, "enabled_at": NOW, "asked": True,
                             "covers": {key: NOW for key in acting.ACTIONS}}       # the yes names what it covers
    assert setting.stat().st_mode & 0o077 == 0                          # private to the person
    assert acting.state()["agreed"] == acting.DOES and acting.state()["new"] == []
    assert acting.enabled() and acting.enabled_at() == NOW and acting.asked() and acting.still_ok("add") is None


def test_only_what_arrives_after_the_yes(setting):
    acting.set_enabled(True, now=NOW)
    assert acting.allows(NOW, "add") and acting.allows(NOW + 60, "add")
    assert not acting.allows(NOW - 1, "add") and not acting.allows(None, "add") and not acting.allows("later", "add")
    # Off and on again moves the line forward: mail from the gap is not reached back for.
    acting.set_enabled(False, now=NOW + 100)
    assert not acting.allows(NOW + 200, "add") and acting.enabled_at() is None and acting.asked()
    acting.set_enabled(True, now=NOW + 300)
    assert not acting.allows(NOW + 200, "add") and acting.allows(NOW + 301, "add")


def test_off_stops_a_write_that_was_about_to_happen_and_so_does_a_turn(setting, monkeypatch):
    acting.set_enabled(True, now=NOW)
    assert acting.still_ok("add") is None and autoadd._still_ok() is None
    from service import idle
    monkeypatch.setattr(idle, "foreground_busy", lambda: True)
    assert "turn" in acting.still_ok("add")
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    acting.set_enabled(False)
    assert acting.still_ok("add") == "Acting for you was turned off" == autoadd._still_ok()


def test_a_file_that_is_missing_damaged_or_the_wrong_shape_means_off(setting):
    acting.set_enabled(True, now=NOW)
    assert acting.enabled()
    for bad in ("not json", "[]", '"yes"', '{"enabled": true}', '{"enabled": 1, "enabled_at": 5}',
                '{"enabled": true, "enabled_at": "x"}', '{"enabled": "true", "enabled_at": 5}'):
        setting.write_text(bad)                                          # read from disk each time: no cache
        assert not acting.enabled() and not acting.allows(NOW + 1, "add") and acting.still_ok("add") is not None, bad
    setting.unlink()
    assert not acting.enabled() and acting.state()["enabled_at"] is None
    with pytest.raises(ValueError):
        acting.set_enabled("yes")


def test_a_forged_file_cannot_backdate_the_yes(setting, monkeypatch):
    import time
    monkeypatch.undo()                                                   # the real creation-time check
    monkeypatch.setattr(acting, "_path", lambda: setting)
    setting.parent.mkdir(parents=True, exist_ok=True)
    before = time.time()
    setting.write_text('{"enabled": true, "enabled_at": 0, "asked": true, "covers": {"add": 0}}')   # "yes since 1970"
    assert acting.enabled() and acting.enabled_at() >= before - 2           # counts from when it was written
    assert not acting.allows(before - 3600, "add") and not acting.allows(0.0, "add")       # old mail stays out of reach
    assert acting.allows(time.time() + 5, "add")
    # The switch's own write is unaffected: its time and the file's agree.
    acting.set_enabled(True)
    assert abs(acting.enabled_at() - time.time()) < 5


def test_backdating_the_files_own_times_cannot_backdate_the_yes(setting, monkeypatch):
    """touch -t / os.utime / cp -p / a restore can move mtime (and, on APFS, birthtime) into the past;
    the inode change time (st_ctime) is set by the kernel and cannot be, so the yes never reaches back."""
    import os
    import time
    monkeypatch.undo()                                                   # the real _born
    monkeypatch.setattr(acting, "_path", lambda: setting)
    monkeypatch.setattr(acting, "_revoked", False)
    setting.parent.mkdir(parents=True, exist_ok=True)
    before = time.time()
    setting.write_text('{"enabled": true, "enabled_at": 1000, "asked": true}')
    past = 1_000_000.0                                                   # 1970
    os.utime(setting, (past, past))
    info = setting.stat()
    assert info.st_mtime == past                                         # the forger's own time took
    if getattr(info, "st_birthtime", None) is not None and info.st_birthtime < before - 2:
        # This platform let user code move the creation time back too (APFS does): the old check,
        # creation time alone, would have been fooled here.
        assert info.st_birthtime <= past + 1
    assert info.st_ctime >= before - 2                                   # ...but not the change time
    assert acting.enabled() and acting.enabled_at() >= before - 2
    assert not acting.allows(before - 3600, "add") and not acting.allows(past + 1, "add")


# --- off always lands; a yes that can't be saved leaves it off -----------------------------------

def _refuse(_setting):
    raise OSError("disk full")


def test_an_off_that_cannot_be_written_removes_the_file(setting, monkeypatch):
    acting.set_enabled(True, now=NOW)
    monkeypatch.setattr(acting, "_write", _refuse)
    reply = _client().put("/assistant/proactive/act", json={"enabled": False})
    assert reply.status_code == 200 and reply.json()["enabled"] is False and reply.json()["enabled_at"] is None
    assert not setting.exists() and not acting.enabled() and not acting._revoked     # missing file = off
    assert not acting.allows(NOW + 1, "add") and acting.still_ok("add") is not None


def test_an_off_that_can_be_neither_written_nor_removed_is_still_off_in_this_process(setting, monkeypatch):
    import os
    acting.set_enabled(True, now=NOW)
    folder = setting.parent
    os.chmod(folder, 0o500)                    # a real refusal: no new temp file, no unlink
    try:
        reply = _client().put("/assistant/proactive/act", json={"enabled": False})
        assert reply.status_code == 200 and reply.json()["enabled"] is False
        assert read(setting)["enabled"] is True                         # the disk still says yes...
        assert acting._revoked and not acting.enabled()                 # ...and it counts for nothing
        assert acting.enabled_at() is None and not acting.allows(NOW + 1, "add")
        assert acting.still_ok("add") == "Acting for you was turned off"
        assert _client().get("/assistant/proactive/act").json()["enabled"] is False
        acting.mark_asked()                                              # never writes the stale yes back
        assert not acting.enabled()
        # A yes while the disk still refuses: 409, and still off.
        reply = _client().put("/assistant/proactive/act", json={"enabled": True})
        assert reply.status_code == 409 and "couldn't save your yes" in reply.json()["detail"]
        assert "does nothing on its own" in reply.json()["detail"] and not acting.enabled()
    finally:
        os.chmod(folder, 0o700)
    # Once a yes is actually saved, it works again.
    on = _client().put("/assistant/proactive/act", json={"enabled": True}).json()
    assert on["enabled"] is True and not acting._revoked and acting.allows(on["enabled_at"] + 1, "add")


def test_a_yes_that_cannot_be_saved_never_leaves_an_earlier_yes_in_force(setting, monkeypatch):
    _write(setting, enabled=True, enabled_at=NOW, asked=True, covers={"add": NOW})   # an earlier, narrower yes
    monkeypatch.setattr(acting, "_write", _refuse)
    reply = _client().put("/assistant/proactive/act", json={"enabled": True})
    assert reply.status_code == 409 and reply.json()["detail"].startswith("Wisp couldn't save your yes")
    assert not setting.exists() and not acting.enabled() and not acting.allows(NOW + 1, "add")
    with pytest.raises(OSError):
        acting.set_enabled(True, now=NOW + 5)                            # the module raises; the gate is off
    assert not acting.enabled()


def test_a_yes_that_cannot_be_saved_or_removed_is_off_by_the_flag(setting, monkeypatch):
    _write(setting, enabled=True, enabled_at=NOW, asked=True, covers={"add": NOW})   # an earlier, narrower yes
    write, unlink = acting._write, acting._unlink
    monkeypatch.setattr(acting, "_write", _refuse)
    monkeypatch.setattr(acting, "_unlink", lambda: False)
    assert _client().put("/assistant/proactive/act", json={"enabled": True}).status_code == 409
    assert read(setting)["enabled"] is True and acting._revoked and not acting.enabled()
    assert acting.still_ok("add") is not None and not acting.allows(NOW + 1, "add")
    monkeypatch.setattr(acting, "_write", write)                         # the disk works again
    monkeypatch.setattr(acting, "_unlink", unlink)
    acting.set_enabled(True, now=NOW + 50)
    assert acting.enabled() and acting.enabled_at() == NOW + 50 and not acting._revoked


def test_every_reply_of_the_switch_is_what_is_on_disk(setting, monkeypatch):
    """set_act and act_asked answer with _act_state(), the gate read again, never the request."""
    from service.proactive import api
    monkeypatch.setattr(acting, "set_enabled", lambda on, **kw: {"enabled": on})   # "saved" nothing
    reply = _client().put("/assistant/proactive/act", json={"enabled": True})
    assert reply.status_code == 200 and reply.json()["enabled"] is False

    def broken(*args, **kw):
        raise RuntimeError("anything")
    monkeypatch.setattr(acting, "set_enabled", broken)
    assert _client().put("/assistant/proactive/act", json={"enabled": False}).json()["enabled"] is False
    monkeypatch.setattr(acting, "mark_asked", broken)
    assert _client().post("/assistant/proactive/act/asked", json={}).json() == api._act_state()


def test_the_settings_file_cannot_turn_it_on_any_more(setting, monkeypatch):
    # config.yaml is writable by the assistant's file tools under Full access, so nothing in it counts.
    monkeypatch.setattr(config, "user_overlay_valid", lambda: True)
    for value in ({"act_for_me": {"enabled": True, "enabled_at": 0}}, {"proactive_auto_add": True}):
        monkeypatch.setattr(config, "models_config", lambda value=value: value)
        assert not acting.enabled() and not acting.allows(NOW, "add") and not config.proactive_auto_add_enabled()


def test_put_aside_is_not_asked_again_and_never_turns_it_on(setting):
    acting.mark_asked()
    assert acting.asked() and not acting.enabled()
    assert not setting.exists()                                          # the gate's own file is never written
    assert read(notes(setting)) == {"asked": True, "aside": []}
    assert notes(setting).stat().st_mode & 0o077 == 0


def test_putting_the_question_aside_never_overwrites_a_yes(setting):
    acting.set_enabled(True, now=NOW)                                    # a yes from another window
    covers = dict(read(setting)["covers"])
    setting.write_text(__import__("json").dumps({"enabled": True, "enabled_at": NOW, "asked": False, "covers": covers}))
    before = setting.read_text()
    acting.mark_asked()                                                  # "Not now" lands a moment later
    assert setting.read_text() == before and acting.asked()              # the yes is untouched, byte for byte
    assert acting.allows(NOW + 1, "add")
    acting.set_enabled(False)
    before, noted = setting.read_text(), notes(setting).read_text()
    acting.mark_asked()
    assert setting.read_text() == before and notes(setting).read_text() == noted   # already asked: nothing is rewritten


# --- a new kind of action, start to finish (the interface a second module uses) --------------

def _follow_up(store, now=NOW + 10, fp="fp-follow-1"):
    return acting.record(store, fp, kind="follow_up", status="added", now=now,
                         title="Answer Maya", quote="are you coming on Friday?", who="Maya Chen",
                         source="messages", item_id="text:maya", received_ts=NOW + 5, timezone="UTC",
                         card={"meta": "REMINDER", "title": "Set a reminder to answer Maya",
                               "did": "I'll remind you tomorrow at 9 AM."},
                         show_until=now + 86400, undo={"reminder_id": "R-77"})


def test_an_action_is_recorded_shown_on_one_card_and_undone_by_its_own_undoer(setting, store, monkeypatch):
    acting.set_enabled(True, now=NOW)
    assert acting.room(store, NOW + 10) == acting.MAX_PER_DAY
    _follow_up(store)
    assert acting.room(store, NOW + 20) == acting.MAX_PER_DAY - 1
    [card] = autoadd.done_cards(store, NOW + 20)
    assert card["action"]["method"] == "undo_add" and card["action"]["record"] == "fp-follow-1"
    assert card["title"] == "Set a reminder to answer Maya" and card["meta"] == "REMINDER"
    assert card["body"] == "“are you coming on Friday?” I'll remind you tomorrow at 9 AM."
    assert "R-77" not in str(card)                        # what Undo needs never leaves the store
    undone = []

    async def undo(record):
        undone.append(record["undo"]["reminder_id"])
        return {"ok": True, "status": "succeeded", "message": "Reminder removed"}
    monkeypatch.setattr(acting, "_undoers", {})
    assert asyncio.run(autoadd.undo(card, store, now=NOW + 30))["status"] == "blocked"   # no undoer yet
    acting.register_undoer("follow_up", undo)
    result = asyncio.run(autoadd.undo(card, store, now=NOW + 30))
    assert result == {"ok": True, "status": "succeeded", "message": "Reminder removed"} and undone == ["R-77"]
    assert store.auto_added()["fp-follow-1"]["status"] == "undone"
    assert autoadd.done_cards(store, NOW + 40) == []
    assert asyncio.run(autoadd.undo(card, store, now=NOW + 50))["status"] == "blocked"   # never twice
    assert acting.room(store, NOW + 60) == acting.MAX_PER_DAY - 1                         # an undone action still counts


def test_a_card_comes_only_from_the_record_and_leaves_when_its_time_passes(setting, store):
    _follow_up(store)
    assert autoadd.done_cards(store, NOW + 10 + 86400 + 1) == []           # past show_until
    forged = {"action": {"method": "undo_add", "record": "no-such-record"}}
    assert asyncio.run(autoadd.undo(forged, store))["status"] == "blocked"
    with pytest.raises(ValueError):
        acting.record(store, "fp-x", kind="send_email", status="added", now=NOW, title="x")
    with pytest.raises(ValueError):
        acting.register_undoer("send_email", None)


def test_the_daily_cap_is_shared_by_every_kind(setting, store):
    for i in range(acting.MAX_PER_DAY):
        _follow_up(store, now=NOW + i, fp=f"fp-{i}")
    assert acting.room(store, NOW + 100) == 0
    assert store.auto_added_since(NOW - 1) == acting.MAX_PER_DAY          # auto-add's own cap sees them too
    assert acting.room(store, NOW + acting.DAY_S + 100) == acting.MAX_PER_DAY


def test_a_module_can_read_its_own_records_and_count(setting, store):
    _follow_up(store, now=NOW + 1, fp="f-1")
    acting.record(store, "f-2", kind="follow_up", status="undone", now=NOW + 2, title="t", person="maya@x")
    acting.record(store, "f-3", kind="follow_up", status="failed", now=NOW + 3, title="t")
    acting.record(store, "h-1", kind="hold", status="added", now=NOW + 4, title="t")
    assert [r["fingerprint"] for r in acting.records(store, kind="follow_up")] == ["f-3", "f-2", "f-1"]
    undone = acting.records(store, kind="follow_up", statuses=("undone", "undo_unknown"))
    assert [(r["fingerprint"], r["person"]) for r in undone] == [("f-2", "maya@x")]
    assert acting.done_since(store, NOW, kind="follow_up") == 2 and acting.done_since(store, NOW) == 3
    assert acting.done_since(store, NOW + 2.5, kind="follow_up") == 0
    acting.records(store)[0]["status"] = "changed"                       # copies: the store is untouched
    assert store.auto_added()["h-1"]["status"] == "added"


def test_recent_lists_words_only(setting, store):
    _follow_up(store)
    [row] = acting.recent(store, NOW + 20)
    assert row == {"kind": "follow_up", "title": "Set a reminder to answer Maya", "who": "Maya Chen",
                   "at": NOW + 10, "undone": False, "check": ""}


# --- the switch's routes ------------------------------------------------------------------------

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service.proactive.api import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_the_switch_is_the_apps_own_and_takes_only_a_real_boolean(setting):
    client, url = _client(), "/assistant/proactive/act"
    assert client.get(url).json()["enabled"] is False
    assert client.put(url, json={"enabled": True}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.put(url, json={"enabled": True}, headers={"Origin": "null"}).status_code == 403
    assert client.put(url, content='{"enabled": true}', headers={"Content-Type": "text/plain"}).status_code == 415
    for body in ({"enabled": "true"}, {"enabled": 1}, {}, {"enabled": True, "enabled_at": 0}, {"enabled": None}):
        assert client.put(url, json=body).status_code == 422
    assert not setting.exists() and not acting.enabled()
    on = client.put(url, json={"enabled": True}).json()
    assert on["enabled"] is True and on["enabled_at"] > 1_700_000_000 and on["per_day"] == 10 and on["per_cycle"] == 3
    off = client.put(url, json={"enabled": False}).json()
    assert off["enabled"] is False and off["enabled_at"] is None and off["asked"] is True
    assert autoadd._still_ok() == "Acting for you was turned off"


def test_the_question_can_be_put_aside_from_the_app_only(setting):
    client, url = _client(), "/assistant/proactive/act/asked"
    assert client.post(url, json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    reply = client.post(url, json={}).json()
    assert reply["asked"] is True and reply["enabled"] is False


def test_a_yes_that_cannot_be_saved_keeps_everything_off_and_says_so(setting, monkeypatch):
    def refuse(_setting):
        raise OSError("disk full")
    monkeypatch.setattr(acting, "_write", refuse)
    reply = _client().put("/assistant/proactive/act", json={"enabled": True})
    assert reply.status_code == 409 and "does nothing on its own" in reply.json()["detail"]
    assert not acting.enabled()


def test_only_the_wisp_app_itself_can_say_yes_and_anyone_local_can_say_no(setting, monkeypatch):
    from service import wisp_reset
    from service.proactive import api
    monkeypatch.undo()                                                   # the real _require_app
    monkeypatch.setattr(acting, "_path", lambda: setting)
    proven = {"is_app": False}
    monkeypatch.setattr(wisp_reset, "caller_is_parent_app", lambda client, server: proven["is_app"])
    client, url = _client(), "/assistant/proactive/act"
    # Another program on this Mac: no Origin, JSON, a clean boolean, and still refused.
    refused = client.put(url, json={"enabled": True})
    assert refused.status_code == 403 and "Only the Wisp app" in refused.json()["detail"] and not acting.enabled()

    def broken(client, server):
        raise RuntimeError("socket table unreadable")
    monkeypatch.setattr(wisp_reset, "caller_is_parent_app", broken)
    assert client.put(url, json={"enabled": True}).status_code == 403 and not acting.enabled()   # unproven is refused
    assert api._require_app.__module__ == "service.proactive.api"
    acting.set_enabled(True, now=NOW)
    # Off never depends on that proof.
    assert client.put(url, json={"enabled": False}).json()["enabled"] is False and not acting.enabled()


def test_the_gates_file_is_protected_from_the_assistants_own_tools(setting, monkeypatch):
    """A model must never be able to give or backdate the consent by writing a file or running a shell
    command, whatever the access mode: the write asks (or is refused), and is never simply allowed."""
    from service.paths import MOE_DIR
    from service.safety import policy
    target = MOE_DIR / acting.FILE
    assert target in policy._credential_roots()
    assert any(__import__("re").search(pattern, f"echo x > {target}") for pattern in policy._SHELL_SECRET)
    assert any(__import__("re").search(pattern, "sed -i '' s/false/true/ ~/.moe/act_for_me.json")
               for pattern in policy._SHELL_SECRET)
    # The decision itself, in every access mode: a write to it is never simply allowed.
    for full, view, expected in ((False, False, "CONFIRM"), (True, False, "CONFIRM"), (False, True, "DENY")):
        monkeypatch.setattr(policy, "_FULL_ACCESS", full)
        monkeypatch.setattr(policy, "_READ_ONLY", view)
        assert policy.decide("fs_write", {"path": str(target), "content": "{}"}, tool="write_file").tier.name == expected
        for cmd in (f"echo x > {target}", "echo x > ~/.moe/act_for_me.json",
                    "sed -i '' s/false/true/ ~/.moe/act_for_me.json", f"mv /tmp/x {target}"):
            assert policy.decide("shell", {"cmd": cmd}, tool="run_shell").tier.name == expected, (full, view, cmd)
    # config.yaml, where the gate used to live, IS writable without a card under Full access: why it moved.
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    assert policy.decide("fs_write", {"path": str(MOE_DIR / "config.yaml"), "content": "x"},
                         tool="write_file").tier.name == "ALLOW"
    monkeypatch.undo()
    assert acting._path() == __import__("service.paths", fromlist=["moe_dir"]).moe_dir() / acting.FILE


_MODES = [pytest.param(False, False, "CONFIRM", id="default"), pytest.param(True, False, "CONFIRM", id="full"),
          pytest.param(False, True, "DENY", id="view_only")]


def _gate_file():
    from service.paths import MOE_DIR
    return MOE_DIR / acting.FILE                    # only its name: decide() never opens it


# Every file-writing tool that takes a path (category fs_write). There is no edit_file/apply_patch tool.
_WRITE_TOOLS = [
    ("write_file", lambda t: {"path": str(t), "content": "{\"enabled\": true, \"enabled_at\": 0}"}),
    ("write_document", lambda t: {"path": str(t), "title": "x"}),
    ("spreadsheet_ops", lambda t: {"path": str(t), "headers": ["enabled"]}),
    ("move_path", lambda t: {"source": "/tmp/forged.json", "destination": str(t)}),
    ("organize_files", lambda t: {"pattern": acting.FILE, "folder": "/tmp", "destination": str(t.parent),
                                  "confirm": True, "preview_token": "t"}),
]

_SHELL_WRITES = [
    "cp /tmp/forged.json {t}", "cp -p /tmp/forged.json {t}", "cp /tmp/forged.json ~/.moe/act_for_me.json",
    "ln -s /tmp/forged.json {t}", "ln -sf /tmp/forged.json {t}", "ln -sf /tmp/forged.json ~/.moe/act_for_me.json",
    "python3 -c \"open('{t}','w').write('x')\"", "echo x | tee {t}", "echo x | tee ~/.moe/act_for_me.json",
    "mv /tmp/forged.json {t}", "rm {t} && echo x > {t}", "rm -f {t}; cp /tmp/forged.json {t}",
    "truncate -s 0 {t}",
]


@pytest.mark.parametrize("full,view,expected", _MODES)
@pytest.mark.parametrize("tool,args", _WRITE_TOOLS, ids=[name for name, _ in _WRITE_TOOLS])
def test_no_file_tool_may_write_the_gate_without_asking(monkeypatch, full, view, expected, tool, args):
    from service.safety import policy
    monkeypatch.setattr(policy, "_FULL_ACCESS", full)
    monkeypatch.setattr(policy, "_READ_ONLY", view)
    assert policy.decide("fs_write", args(_gate_file()), tool=tool).tier.name == expected


def test_organize_files_preview_moves_nothing_and_so_may_run(monkeypatch):
    # Its preview (confirm false) lists files only; the move itself is the case above.
    from service.safety import policy
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    args = {"pattern": acting.FILE, "folder": "/tmp", "destination": str(_gate_file().parent)}
    assert policy.decide("fs_write", args, tool="organize_files").tier.name == "ALLOW"


@pytest.mark.parametrize("full,view,expected", _MODES)
@pytest.mark.parametrize("cmd", _SHELL_WRITES)
def test_no_shell_command_may_write_the_gate_without_asking(monkeypatch, full, view, expected, cmd):
    from service.safety import policy
    monkeypatch.setattr(policy, "_FULL_ACCESS", full)
    monkeypatch.setattr(policy, "_READ_ONLY", view)
    command = cmd.format(t=_gate_file())
    tier = policy.decide("shell", {"cmd": command}, tool="run_shell").tier.name
    # Never ALLOW. Some spellings (rm, truncate) are refused outright, which is stricter than asking.
    assert tier != "ALLOW" and (tier == "DENY" if view else tier in ("CONFIRM", "DENY")), (command, tier)


# --- _require_app: what it hands to the socket-table proof -----------------------------------------

class _Request:
    def __init__(self, client, server, headers=None):
        self.scope, self.headers = {"client": client, "server": server}, dict(headers or {})


def _real_require_app(monkeypatch, answer):
    """The real _require_app with only caller_is_parent_app replaced; returns the calls it got."""
    from service import wisp_reset
    from service.proactive import api
    calls = []

    def check(client, server, **kw):
        calls.append((client, server, kw))
        if isinstance(answer, BaseException):
            raise answer
        return answer
    monkeypatch.setattr(wisp_reset, "caller_is_parent_app", check)
    return api._require_app, calls


def test_require_app_passes_the_connections_own_ends_through_unchanged(monkeypatch):
    require, calls = _real_require_app(monkeypatch, True)
    assert require(_Request(("127.0.0.1", 52011), ("127.0.0.1", 8765))) is None
    assert calls == [(("127.0.0.1", 52011), ("127.0.0.1", 8765), {})]


@pytest.mark.parametrize("client,server", [
    (None, ("127.0.0.1", 8765)), (("127.0.0.1", 52011), None), (None, None), ((), ("127.0.0.1", 8765)),
    (("10.0.0.5", 52011), ("127.0.0.1", 8765)), (("127.0.0.1", 52011), ("192.168.1.2", 8765)),
    (("::1", 52011), ("127.0.0.1", 8765)), (("localhost", 52011), ("127.0.0.1", 8765)),
    (("127.0.0.1", "52011"), ("127.0.0.1", 8765)), (("127.0.0.1", 52011), ("127.0.0.1", 8765.0)),
    (("127.0.0.1", True), ("127.0.0.1", 8765)), (("127.0.0.1", None), ("127.0.0.1", 8765)),
    (("127.0.0.1",), ("127.0.0.1", 8765)),
], ids=["no-client", "no-server", "neither", "empty-client", "remote-client", "remote-server",
        "ipv6-client", "name-not-address", "str-port", "float-port", "bool-port", "none-port", "short-client"])
def test_require_app_refuses_without_asking_the_check(monkeypatch, client, server):
    from fastapi import HTTPException
    require, calls = _real_require_app(monkeypatch, True)
    with pytest.raises(HTTPException) as refused:
        require(_Request(client, server))
    assert refused.value.status_code == 403 and calls == []


@pytest.mark.parametrize("answer", [False, None, 1, "yes", RuntimeError("socket table"), OSError("EPERM")],
                         ids=["false", "none", "truthy-int", "truthy-str", "raises", "oserror"])
def test_require_app_refuses_unless_the_check_says_exactly_true(monkeypatch, answer):
    from fastapi import HTTPException
    require, calls = _real_require_app(monkeypatch, answer)
    with pytest.raises(HTTPException) as refused:
        require(_Request(("127.0.0.1", 52011), ("127.0.0.1", 8765)))
    assert refused.value.status_code == 403 and len(calls) == 1


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_acting.py"' in manifest


# --- one path per message: Caught for you steps aside while Wisp acts for you --------------------

def test_caught_for_you_stops_creating_while_wisp_acts_for_you(setting, monkeypatch):
    from service.assistant import attention_runner as runner
    from service.attention.live import Settings
    live = Settings(mode="live", capture_events=True)
    assert runner.one_path(live) == live                               # acting off: unchanged
    for other in (Settings(mode="shadow"), Settings(mode="off")):
        assert runner.one_path(other) == other
    acting.set_enabled(True, now=NOW)
    stepped = runner.one_path(live)
    assert stepped.mode == "shadow" and stepped.capture_events is True and stepped.daily_cap == live.daily_cap
    assert runner.one_path(Settings(mode="off")).mode == "off"          # off is never turned into anything else
    acting.set_enabled(False)
    assert runner.one_path(live) == live

    def broken():
        raise RuntimeError("unreadable")
    monkeypatch.setattr(acting, "enabled", broken)
    assert runner.one_path(live).mode == "shadow"                       # unknown never leaves two writers


def test_auto_add_never_adds_what_is_already_on_the_list(store):
    import tests.test_proactive_auto_add as T
    item = T.text("Maya Chen", T.RECITAL)
    plan, _ = autoadd.plan_for(item, dict(T.OK), now=T.NOW, tz=T.TZ)
    # Caught for you (or the person) already made a reminder for it.
    existing = [{"source": "reminders", "title": "Maya recital", "when_ts": plan["start_ts"] - 1800}]
    writers = T.Writers()
    out = T.step(store, T.survivors(item), writers, existing=lambda n: existing)
    assert writers.calls == [] and out["added"] == [] and out["tap"] == []


def test_a_date_already_on_calendar_does_not_come_back_as_a_card_after_a_reply_reminder(store):
    """A follow-up on the message keeps the date's card offered, but not when the person already
    put the date on Calendar themselves: tapping that card would make a second event."""
    import tests.test_proactive_auto_add as T
    item = T.text("Maya Chen", T.RECITAL)
    plan, _ = autoadd.plan_for(item, dict(T.OK), now=T.NOW, tz=T.TZ)
    acting.record(store, "follow:1", kind="follow_up", status="added", now=T.NOW - 60, title="Answer Maya",
                  item_id=item["id"])
    existing = [{"source": "calendar", "title": "Maya recital", "when_ts": plan["start_ts"]}]
    writers, notes = T.Writers(), []
    out = asyncio.run(autoadd.step(T.survivors(item), {"calendar": [], "reminders": []}, now=T.NOW, tz=T.TZ,
                                   store=store, abort=lambda: False, writers=writers.table(),
                                   existing=lambda n: existing, is_enabled=lambda: True, debug=notes))
    assert writers.calls == [] and out["added"] == [] and out["tap"] == []
    assert notes[-1]["auto_add"] == "already on Calendar"
    # The same when they already handled or waved away that message's card.
    store.mark_handled([plan["item_fingerprint"]])
    notes.clear()
    out = asyncio.run(autoadd.step(T.survivors(item), {"calendar": [], "reminders": []}, now=T.NOW, tz=T.TZ,
                                   store=store, abort=lambda: False, writers=writers.table(),
                                   existing=lambda n: [], is_enabled=lambda: True, debug=notes))
    assert out["tap"] == [] and notes[-1]["auto_add"] == "dismissed or handled"


def test_a_write_cut_off_mid_way_is_settled_for_every_kind_and_listed_as_uncertain(setting, store):
    for fp, kind in (("a-1", "calendar"), ("f-1", "follow_up"), ("h-1", "hold")):
        acting.record(store, fp, kind=kind, status="dispatching", now=NOW - 600, title=f"Thing {fp}", item_id=fp)
    acting.record(store, "ok-1", kind="reminder", status="added", now=NOW - 700, title="Fine", item_id="ok")
    assert acting.room(store, NOW) == acting.MAX_PER_DAY - 1                # in-flight ones are not counted yet
    assert acting.settle(store, NOW) == 3 and acting.settle(store, NOW) == 0
    assert {r["status"] for r in acting.records(store) if r["fingerprint"] != "ok-1"} == {"unknown"}
    assert acting.room(store, NOW) == acting.MAX_PER_DAY - 4                # now every kind's cap sees them
    acting.record(store, "d-1", kind="draft", status="unknown", now=NOW - 650, title="Thing d-1", item_id="d")
    assert [r["check"] for r in acting.recent(store, NOW) if r["title"] == "Thing d-1"] == ["Calendar and Reminders"]
    store.auto_add_drop("d-1")
    store.auto_add_drop("never-there")                                      # dropping nothing is fine
    rows = {r["title"]: r["check"] for r in acting.recent(store, NOW)}
    assert rows == {"Thing a-1": "Calendar", "Thing f-1": "Reminders", "Thing h-1": "your Day plan", "Fine": ""}

    class Broken:
        def auto_added(self):
            raise RuntimeError("unreadable")
    assert acting.settle(Broken(), NOW) == 0                                # never fails a cycle


def test_one_message_gets_at_most_one_action_of_wisps_own(store):
    import tests.test_proactive_auto_add as T
    item = T.text("Maya Chen", T.RECITAL)
    # A follow-up reminder already landed on this same message in an earlier cycle.
    acting.record(store, "follow:1", kind="follow_up", status="added", now=T.NOW - 60, title="Answer Maya",
                  item_id=item["id"])
    writers, notes = T.Writers(), []
    out = asyncio.run(autoadd.step(T.survivors(item), {"calendar": [], "reminders": []}, now=T.NOW, tz=T.TZ,
                                   store=store, abort=lambda: False, writers=writers.table(),
                                   existing=lambda n: [], is_enabled=lambda: True, debug=notes))
    assert writers.calls == [] and out["added"] == []
    # Nothing more is DONE on that message, but its date is still offered as a card to tap, and the
    # reply reminder does not make the Brief treat the date as already added.
    assert [c["action"]["method"] for c in out["tap"]] == ["calendar_event"]
    assert notes[-1]["auto_add"] == "Wisp already acted on this message: tap to add"
    assert item["id"] not in out["items"]
    # A hold is about a calendar row, never a message, so it blocks nothing here.
    other = T.text("Sam Lee", "can you come to my recital Sun Nov 1 at 2pm?")
    acting.record(store, "hold:1", kind="hold", status="added", now=T.NOW - 60, title="x", item_id="cal:9")
    T.step(store, T.survivors(other), writers)
    assert [plan["who"] for _, plan in writers.calls] == ["Sam Lee"]


def test_resetting_wisp_erases_the_yes():
    from service import wisp_reset
    assert acting.FILE not in wisp_reset.KEEP_ENTRIES          # everything outside KEEP_ENTRIES is deleted


@pytest.mark.parametrize("cmd, protected", [
    ("echo x > ~/.moe/act_for*.json", True), ("echo x > ~/.moe/act_for_me.jso?", True),
    ("cp /tmp/x ~/.moe/a[c]t_for_me.json", True), ("echo x > ~/.moe/*.json", True), ("rm ~/.moe/*", True),
    ("sed -i '' s/false/true/ ~/.moe/acc*", True), ("echo x > ~/.moe/gr?nts.json", True),
    ("echo x > ~/.m?e/act*", True), ("cp /tmp/x ~/.*/gra*", True), ("echo x > ~/.[m]oe/act_for_me.jso?", True),
    ("echo x > ~/.moe/{grants,act_for_me}.json", True), ("echo x > ~/.moe/act_for_me.{json,bak}", True),
    ("cd ~/.moe && cat act*", True), ("cd ~/.moe; echo x > acc*.json", True),
    ("echo x > ~/.moe/notes*.txt", False), ("ls *.json", False), ("cat ~/Documents/act_for*.json", False),
    ("cd ~/Documents && cat act*", False), ("echo x > ~/.moe/meetings/{a,b}.json", False),
    ("echo x > ~/.moe/meetings/*.json", False),
])
def test_a_wildcard_that_can_expand_to_a_protected_file_is_never_simply_allowed(monkeypatch, cmd, protected):
    from service.safety import policy
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    tier = policy.decide("shell", {"cmd": cmd}, tool="run_shell").tier.name
    assert (tier != "ALLOW") is protected or (not protected and tier in ("ALLOW", "CONFIRM", "DENY")), (cmd, tier)
    if protected:
        assert tier in ("CONFIRM", "DENY"), (cmd, tier)
    assert (policy._moe_glob_hit(cmd) is not None) is protected, cmd


# --- a yes covers the actions that were named when it was given, and nothing added since -----------

def test_a_yes_from_before_actions_were_recorded_covers_nothing_and_is_asked_again(setting):
    _write(setting, enabled=True, enabled_at=NOW, asked=True)             # an earlier build's yes
    assert acting.enabled() is True                                         # the switch is on...
    for action in acting.ACTIONS:                                           # ...but it named nothing
        assert not acting.enabled(action) and not acting.allows(NOW + 60, action)
        assert acting.enabled_at(action) is None and acting.still_ok(action) == "You haven't said yes to this yet"
    assert autoadd.enabled() is False and autoadd._still_ok() is not None
    state = acting.state()
    assert state["asked"] is False and state["agreed"] == [] and state["new"] == acting.DOES
    assert acting.pending() == list(acting.ACTIONS)


def test_an_action_added_after_the_yes_does_nothing_until_it_is_agreed_to(setting):
    _write(setting, enabled=True, enabled_at=NOW, asked=True, covers={"add": NOW, "hold": NOW})
    assert acting.enabled("add") and acting.enabled("hold") and not acting.enabled("follow_up")
    assert acting.allows(NOW + 60, "add") and not acting.allows(NOW + 60, "follow_up")
    assert acting.pending() == ["follow_up"] and acting.asked() is False
    assert acting.state()["new"] == [acting.ACTIONS["follow_up"]]
    assert acting.state()["agreed"] == [acting.ACTIONS["add"], acting.ACTIONS["hold"]]
    # Yes to the new one: it counts from THIS yes; what was agreed keeps its own time.
    acting.set_enabled(True, now=NOW + 500)
    assert read(setting)["covers"] == {"add": NOW, "hold": NOW, "follow_up": NOW + 500}
    assert read(setting)["enabled_at"] == NOW
    assert acting.allows(NOW + 100, "add") and not acting.allows(NOW + 100, "follow_up")
    assert acting.allows(NOW + 501, "follow_up") and acting.pending() == [] and acting.asked() is True


def test_putting_a_new_action_aside_keeps_it_off_and_is_not_asked_again(setting):
    _write(setting, enabled=True, enabled_at=NOW, asked=True, covers={"add": NOW, "hold": NOW})
    before = setting.read_text()
    acting.mark_asked()                                                    # "Not now" on the re-ask
    assert read(notes(setting)) == {"asked": True, "aside": ["follow_up"]} and setting.read_text() == before
    assert acting.asked() is True and not acting.enabled("follow_up") and acting.enabled("add")
    assert acting.state()["new"] == [acting.ACTIONS["follow_up"]]         # still shown as not agreed
    acting.set_enabled(True, now=NOW + 900)                                # the switch, flipped on again later
    assert acting.enabled("follow_up") and acting.allows(NOW + 901, "follow_up")


def _real_clock(setting, monkeypatch):
    """The real `_born` (the fixture replaces it with a made-up clock)."""
    monkeypatch.undo()
    monkeypatch.setattr(acting, "_path", lambda: setting)
    monkeypatch.setattr(acting, "_revoked", False)


def test_not_now_never_touches_the_gates_file_so_no_agreed_time_moves(setting, monkeypatch):
    import time
    _real_clock(setting, monkeypatch)
    old = time.time() - 5000
    _write(setting, enabled=True, enabled_at=old, asked=True, covers={"add": old, "hold": old})
    info, text = setting.stat(), setting.read_text()
    since = {key: acting.enabled_at(key) for key in ("add", "hold")}
    acting.mark_asked()                                                    # "Not now" to the new action
    after = setting.stat()
    assert (after.st_ino, after.st_ctime_ns, after.st_mtime_ns) == (info.st_ino, info.st_ctime_ns, info.st_mtime_ns)
    assert setting.read_text() == text
    assert {key: acting.enabled_at(key) for key in ("add", "hold")} == since
    assert acting.asked() and acting.pending() == ["follow_up"] and not acting.enabled("follow_up")
    acting.mark_asked()                                                    # and again: still nothing
    assert setting.stat().st_ctime_ns == info.st_ctime_ns


def test_a_yes_that_names_nothing_new_writes_nothing(setting, monkeypatch):
    _real_clock(setting, monkeypatch)
    acting.set_enabled(True)
    info, since = setting.stat(), acting.enabled_at("add")
    assert acting.set_enabled(True, now=NOW * 2)["enabled"] is True        # the switch, confirmed once more
    assert (setting.stat().st_ino, setting.stat().st_ctime_ns) == (info.st_ino, info.st_ctime_ns)
    assert acting.enabled_at("add") == since


def test_a_yes_to_a_new_action_moves_the_agreed_ones_forward_to_that_write(setting, monkeypatch):
    """One file has one write time, and nothing acts on what arrived before it (`_born`). So saying
    yes to a new action also moves the actions agreed earlier up to that moment: never back."""
    import time
    _real_clock(setting, monkeypatch)
    old = time.time() - 5000
    _write(setting, enabled=True, enabled_at=old, asked=True, covers={"add": old, "hold": old})
    before = time.time()
    acting.set_enabled(True, now=before + 9000)                            # "Yes, these too"
    assert read(setting)["covers"] == {"add": old, "hold": old, "follow_up": before + 9000}   # what was said
    assert before - 2 <= acting.enabled_at("add") <= time.time() + 2       # what counts: this write
    assert not acting.allows(old + 60, "add") and acting.allows(time.time() + 5, "add")
    assert acting.enabled_at("follow_up") == before + 9000
    assert not acting.allows(time.time() + 5, "follow_up")


def test_a_number_too_large_to_be_a_time_covers_nothing_and_never_breaks_the_reply(setting):
    huge = 10 ** 400                                                       # float() of this raises
    _write(setting, enabled=True, enabled_at=NOW, asked=True, covers={"add": huge, "hold": NOW})
    assert not acting.enabled("add") and acting.enabled("hold") and not acting.allows(NOW + 1, "add")
    assert acting.enabled_at("add") is None and acting.still_ok("add") == "You haven't said yes to this yet"
    reply = _client().get("/assistant/proactive/act")
    assert reply.status_code == 200 and acting.ACTIONS["add"] in reply.json()["new"]
    _write(setting, enabled=True, enabled_at=huge, asked=True, covers={"add": NOW})
    assert not acting.enabled() and acting.enabled_at() is None and not acting.allows(NOW + 1, "add")
    assert _client().get("/assistant/proactive/act").json()["enabled"] is False
    assert not acting.allows(huge, "add") and not acting.allows(float("nan"), "add")
    setting.write_text('{"enabled": true, "enabled_at": 5, "covers": {"add": NaN, "hold": Infinity}}')
    assert not acting.enabled("add") and not acting.enabled("hold")


def test_yes_these_too_never_turns_everything_back_on_after_an_off(setting):
    _write(setting, enabled=True, enabled_at=NOW, asked=True, covers={"add": NOW})
    acting.set_enabled(False)                                              # another window: off
    reply = _client().put("/assistant/proactive/act", json={"enabled": True, "only_new": True})
    assert reply.status_code == 409 and "turned off" in reply.json()["detail"]
    assert not acting.enabled() and read(setting)["enabled"] is False
    with pytest.raises(acting.NotOn):
        acting.set_enabled(True, only_new=True)
    assert not acting._revoked and read(setting)["enabled"] is False       # the off on disk is untouched
    # While it is on, the same tap adds the new actions.
    _write(setting, enabled=True, enabled_at=NOW, asked=True, covers={"add": NOW})
    reply = _client().put("/assistant/proactive/act", json={"enabled": True, "only_new": True})
    assert reply.status_code == 200 and reply.json()["new"] == [] and acting.enabled("follow_up")
    assert _client().put("/assistant/proactive/act", json={"enabled": False, "only_new": True}).json()["enabled"] is False


def test_a_reminder_write_checks_its_callers_own_action_right_before_dispatch(setting, monkeypatch):
    """A follow-up's reminder goes through autoadd.create_reminder; its last check must ask about
    follow-ups, not about adding."""
    from service.assistant import outbox
    from service.proactive import executor
    sent = []

    async def request(kind, payload, before_dispatch=None):
        sent.append((kind, before_dispatch() if before_dispatch else None))
        return {"ok": True, "source_id": "R-1", "title": payload["title"]}
    monkeypatch.setattr(outbox, "request", request)
    monkeypatch.setattr(autoadd, "_app_connected_now", lambda: True)
    monkeypatch.setattr(executor, "_reminder_problem", lambda: None)
    plan = {"title": "Reply to Maya", "due_ts": NOW + 3600}
    _write(setting, enabled=True, enabled_at=NOW, asked=True, covers={"follow_up": NOW})
    assert asyncio.run(autoadd.create_reminder(plan, action="follow_up"))["ok"] is True
    assert sent == [("create_reminder", None)]                             # allowed at dispatch too
    blocked = asyncio.run(autoadd.create_reminder(plan))                   # adding was not agreed to
    assert blocked["status"] == "blocked" and len(sent) == 1
    _write(setting, enabled=True, enabled_at=NOW, asked=True, covers={"add": NOW})
    assert asyncio.run(autoadd.create_reminder(plan, action="follow_up"))["status"] == "blocked" and len(sent) == 1
    assert asyncio.run(autoadd.create_reminder(plan))["ok"] is True and len(sent) == 2


def test_every_gate_check_needs_its_action():
    for check in (acting.allows, acting.still_ok):
        with pytest.raises(TypeError):
            check(NOW) if check is acting.allows else check()


def test_off_then_on_names_everything_from_the_new_yes(setting):
    acting.set_enabled(True, now=NOW)
    acting.set_enabled(False)
    acting.set_enabled(True, now=NOW + 300)
    assert read(setting)["covers"] == {key: NOW + 300 for key in acting.ACTIONS}
    assert not acting.allows(NOW + 200, "add") and acting.allows(NOW + 301, "add")


def test_an_unknown_action_or_a_malformed_list_covers_nothing(setting):
    for covers in ("add", ["add"], {"add": "yesterday"}, {"send_email": NOW}, None):
        _write(setting, enabled=True, enabled_at=NOW, asked=True, covers=covers)
        assert not acting.enabled("add") and not acting.enabled("send_email") and not acting.allows(NOW + 1, "add")
    assert acting.ACTION_OF["calendar"] == acting.ACTION_OF["reminder"] == "add"
    assert set(acting.ACTION_OF.values()) - {"draft"} == set(acting.ACTIONS)


def test_each_writer_names_its_own_action(setting, store, monkeypatch, tmp_path):
    from service.proactive import autohold, followups
    _write(setting, enabled=True, enabled_at=NOW, asked=True, covers={"hold": NOW})
    assert autoadd.enabled() is False and followups.ACTION == "follow_up"
    card = {"id": "c1", "title": "Worksheet", "sources": [{"kind": "calendar", "ref": "cal:1"}],
            "action": {"method": "block_time", "button": "Block 2–3 PM?", "slot": {"start": NOW + 3600, "end": NOW + 7200},
                       "envelope": {"title": "Worksheet", "start": NOW + 3600}}}
    monkeypatch.setattr(autohold, "resolve_native", lambda ref: None)
    held = autohold.step([card], now=NOW, tz="UTC", store=store, abort=lambda: False, hours=lambda slot, tz: True,
                         hold=lambda c: {"ok": True, "status": "succeeded", "task_id": "t1"})
    assert held == {"c1"}                                                   # holding was agreed to
    _write(setting, enabled=True, enabled_at=NOW, asked=True, covers={"add": NOW})
    store2 = ProactiveStore(tmp_path / "another")                          # nothing recorded: only the gate stops it
    assert autohold.step([{**card, "id": "c2", "sources": [{"kind": "calendar", "ref": "cal:2"}]}], now=NOW, tz="UTC",
                         store=store2, abort=lambda: False, hours=lambda slot, tz: True,
                         hold=lambda c: {"ok": True, "status": "succeeded", "task_id": "t2"}) == set()
    _write(setting, enabled=True, enabled_at=NOW, asked=True, covers={"hold": NOW})
    assert autohold.step([{**card, "id": "c2", "sources": [{"kind": "calendar", "ref": "cal:2"}]}], now=NOW, tz="UTC",
                         store=store2, abort=lambda: False, hours=lambda slot, tz: True,
                         hold=lambda c: {"ok": True, "status": "succeeded", "task_id": "t2"}) == {"c2"}
