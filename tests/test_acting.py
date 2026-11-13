"""Let Wisp act for me (service/proactive/acting.py): the one gate, the record of what Wisp
did, and the "Wisp did this" card with Undo. Also the worked example for a new kind of action."""
from __future__ import annotations

import asyncio

import pytest

from service import config
from service.proactive import acting, autoadd, executor
from service.proactive.store import ProactiveStore

NOW = 1_800_000_000.0


@pytest.fixture
def setting(tmp_path, monkeypatch):
    """The gate's own file, in this test's folder. Returns its path; `read(path)` is what is on disk."""
    path = tmp_path / "home" / acting.FILE
    monkeypatch.setattr(acting, "_path", lambda: path)
    # These tests use a made-up clock (NOW); the file's real creation time has its own test.
    monkeypatch.setattr(acting, "_born", lambda: 0.0)
    from service import idle
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    # Proving "the caller is the Wisp app" needs the real socket table; its own test replaces this.
    from service.proactive import api
    monkeypatch.setattr(api, "_require_app", lambda request: None)
    return path


def read(path):
    import json
    return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def store(tmp_path):
    return ProactiveStore(tmp_path / "proactive")


def test_off_until_a_yes_and_the_yes_is_dated(setting):
    assert acting.state() == {"enabled": False, "enabled_at": None, "asked": False, "per_day": acting.MAX_PER_DAY,
                              "does": acting.DOES, "never": acting.NEVER, "scope": acting.SCOPE}
    assert "after you say yes" in acting.SCOPE and "already there" in acting.SCOPE
    assert acting.DOES and "never sends" in acting.NEVER
    assert not acting.allows(NOW) and acting.still_ok() == "Acting for you was turned off"
    acting.set_enabled(True, now=NOW)
    assert read(setting) == {"enabled": True, "enabled_at": NOW, "asked": True}
    assert setting.stat().st_mode & 0o077 == 0                          # private to the person
    assert acting.enabled() and acting.enabled_at() == NOW and acting.asked() and acting.still_ok() is None


def test_only_what_arrives_after_the_yes(setting):
    acting.set_enabled(True, now=NOW)
    assert acting.allows(NOW) and acting.allows(NOW + 60)
    assert not acting.allows(NOW - 1) and not acting.allows(None) and not acting.allows("later")
    # Off and on again moves the line forward: mail from the gap is not reached back for.
    acting.set_enabled(False, now=NOW + 100)
    assert not acting.allows(NOW + 200) and acting.enabled_at() is None and acting.asked()
    acting.set_enabled(True, now=NOW + 300)
    assert not acting.allows(NOW + 200) and acting.allows(NOW + 301)


def test_off_stops_a_write_that_was_about_to_happen_and_so_does_a_turn(setting, monkeypatch):
    acting.set_enabled(True, now=NOW)
    assert acting.still_ok() is None and autoadd._still_ok() is None
    from service import idle
    monkeypatch.setattr(idle, "foreground_busy", lambda: True)
    assert "turn" in acting.still_ok()
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    acting.set_enabled(False)
    assert acting.still_ok() == "Acting for you was turned off" == autoadd._still_ok()


def test_a_file_that_is_missing_damaged_or_the_wrong_shape_means_off(setting):
    acting.set_enabled(True, now=NOW)
    assert acting.enabled()
    for bad in ("not json", "[]", '"yes"', '{"enabled": true}', '{"enabled": 1, "enabled_at": 5}',
                '{"enabled": true, "enabled_at": "x"}', '{"enabled": "true", "enabled_at": 5}'):
        setting.write_text(bad)                                          # read from disk each time: no cache
        assert not acting.enabled() and not acting.allows(NOW + 1) and acting.still_ok() is not None, bad
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
    setting.write_text('{"enabled": true, "enabled_at": 0, "asked": true}')   # "yes since 1970"
    assert acting.enabled() and acting.enabled_at() >= before - 2           # counts from when it was written
    assert not acting.allows(before - 3600) and not acting.allows(0.0)       # old mail stays out of reach
    assert acting.allows(time.time() + 5)
    # The switch's own write is unaffected: its time and the file's agree.
    acting.set_enabled(True)
    assert abs(acting.enabled_at() - time.time()) < 5


def test_the_settings_file_cannot_turn_it_on_any_more(setting, monkeypatch):
    # config.yaml is writable by the assistant's file tools under Full access, so nothing in it counts.
    monkeypatch.setattr(config, "user_overlay_valid", lambda: True)
    for value in ({"act_for_me": {"enabled": True, "enabled_at": 0}}, {"proactive_auto_add": True}):
        monkeypatch.setattr(config, "models_config", lambda value=value: value)
        assert not acting.enabled() and not acting.allows(NOW) and not config.proactive_auto_add_enabled()


def test_put_aside_is_not_asked_again_and_never_turns_it_on(setting):
    acting.mark_asked()
    assert acting.asked() and not acting.enabled() and read(setting)["enabled"] is False


def test_putting_the_question_aside_never_overwrites_a_yes(setting):
    acting.set_enabled(True, now=NOW)                                    # a yes from another window
    setting.write_text('{"enabled": true, "enabled_at": %r, "asked": false}' % NOW)
    acting.mark_asked()                                                  # "Not now" lands a moment later
    assert read(setting) == {"enabled": True, "enabled_at": NOW, "asked": True} and acting.allows(NOW + 1)
    acting.set_enabled(False)
    before = setting.read_text()
    acting.mark_asked()
    assert setting.read_text() == before                                 # already asked: nothing is rewritten


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
                   "at": NOW + 10, "undone": False}


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
    assert writers.calls == [] and out["added"] == [] and out["tap"] == []
    assert notes[-1]["auto_add"] == "Wisp already acted on this message"
    # A hold is about a calendar row, never a message, so it blocks nothing here.
    other = T.text("Sam Lee", "can you come to my recital Sun Nov 1 at 2pm?")
    acting.record(store, "hold:1", kind="hold", status="added", now=T.NOW - 60, title="x", item_id="cal:9")
    T.step(store, T.survivors(other), writers)
    assert [plan["who"] for _, plan in writers.calls] == ["Sam Lee"]


def test_resetting_wisp_erases_the_yes():
    from service import wisp_reset
    assert acting.FILE not in wisp_reset.KEEP_ENTRIES          # everything outside KEEP_ENTRIES is deleted
