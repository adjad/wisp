"""Reply reminders (followups.py) through the real background cycle, and the in-flight record.

The cycle tests drive cycle.run_cycle with the PRODUCTION auto-add step (cycle._default_autoadd),
the real gate file (acting.set_enabled, in this test's folder), a Google-fetched mail item built by
the real reader (sources.mail_items) and fakes at every native seam: Google's Sent rows and sync
state, Apple Mail's Sent read, Contacts, quiet hours, Reminders' write and delete. The unit tests
pin the "dispatching" record written before the native write. No subprocess, network, model or
sleep; files only under tmp_path.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="wisp-followups-cycle-tests-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ.setdefault("MOE_DIR", _scratch.name)

import pytest  # noqa: E402

import tests.test_proactive_followups as F  # noqa: E402
from service import config  # noqa: E402
from service.proactive import acting, autoadd, cycle, executor, followups, sources  # noqa: E402
from service.proactive.store import ProactiveStore  # noqa: E402

NOW, TZ, ME, D, H = F.NOW, F.TZ, F.ME, F.D, F.H
BODY = "Thanks! Can you review my section of the report?"


def mail():
    """One Google-fetched mail from Priya, a saved contact the user mailed on two days: a direct
    question, 30 hours old, unanswered. Built by the real reader."""
    [item] = sources.mail_items([F._google_row("Priya Shah", "priya@school.edu", body=BODY)], mine={ME}, now=NOW,
                                tz=TZ, linked={ME}, own=sources.OWN)
    assert item["google_fetched"] is True and item["account"] == ME
    return item


class Native:
    """Reminders' write and delete, and the Sent readers, as fakes that remember their calls."""

    def __init__(self):
        self.writes, self.deletes, self.gmail_sent, self.apple_sent, self.subprocess = [], [], [], [], []
        self.actions = []

    async def create_reminder(self, plan, *, action="add"):
        self.writes.append(plan)
        self.actions.append(action)
        return {"ok": True, "status": "succeeded", "source_id": f"R-{len(self.writes)}", "title": plan["title"]}

    async def delete_reminder(self, stored):
        self.deletes.append(dict(stored))
        return {"ok": True, "status": "succeeded"}

    def sent_rows(self):
        self.gmail_sent.append(1)
        return list(F.MAILED)

    async def read_sent(self, days, timeout=8.0):
        self.apple_sent.append(days)
        raise RuntimeError("the app is away")          # Apple Mail's Sent isn't current: Google's is


@pytest.fixture
def native(monkeypatch, tmp_path):
    fakes = Native()
    # The gate's own file in this test's folder; the made-up clock stands in for its creation time.
    monkeypatch.setattr(acting, "_path", lambda: tmp_path / "home" / acting.FILE)
    monkeypatch.setattr(acting, "_born", lambda: 0.0)
    acting._undoers.pop("follow_up", None)
    saved: dict = {}
    monkeypatch.setattr(config, "user_overlay_valid", lambda: True)
    monkeypatch.setattr(config, "models_config", lambda: dict(saved))
    monkeypatch.setattr(config, "_save_overlay", lambda patch: saved.update(patch))
    from service import idle, gmail_source, user_hours
    from service.assistant import store as assistant
    from service.mail_memory import bridge
    from service.memory import identity
    from service.tools import imessage_tools
    import subprocess
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    monkeypatch.setattr(identity, "user_emails", lambda: [ME])
    monkeypatch.setattr(identity, "user_name", lambda: "Adi Jain")
    # Google: ME linked, its Sent synced a minute ago, these Sent rows.
    monkeypatch.setattr(gmail_source, "connected_accounts", lambda *a: {ME: {}})
    monkeypatch.setattr(gmail_source, "_last", {ME: {"sent": NOW - 60}})
    monkeypatch.setattr(gmail_source, "sent_rows", fakes.sent_rows)
    monkeypatch.setattr(bridge, "read_sent", fakes.read_sent)
    followups._sent_cache.update(at=None, rows=[], fresh=False)

    async def no_sent(now=None):
        return []
    monkeypatch.setattr(sources, "all_sent_rows", no_sent)        # the cycle's own Sent check
    monkeypatch.setattr(imessage_tools, "contact_name_for_handle", F.lookup)
    monkeypatch.setattr(user_hours, "in_quiet_hours", lambda moment=None: False)
    monkeypatch.setattr(autoadd, "default_existing", lambda now: [])
    monkeypatch.setattr(assistant.assistant_store, "undated_reminders", lambda query="": [])
    monkeypatch.setattr(autoadd, "create_reminder", fakes.create_reminder)
    monkeypatch.setattr(autoadd, "delete_reminder", fakes.delete_reminder)

    def no_subprocess(*_a, **_k):
        fakes.subprocess.append(_a)
        raise OSError("no subprocesses in tests")
    monkeypatch.setattr(subprocess, "run", no_subprocess)
    yield fakes
    acting._undoers.pop("follow_up", None)


@pytest.fixture
def store(tmp_path):
    return ProactiveStore(tmp_path / "proactive")


async def _quiet_model(system, material, abort, tokens):
    return ""


def run(store, *, now=NOW, abort=lambda: False):
    item = mail()
    return asyncio.run(cycle.run_cycle(
        now=now, tz=TZ, force=True, reply_fn=_quiet_model, abort=abort, collect=lambda n, z: [item],
        schedule_fn=lambda n, z: {"calendar": [], "reminders": []}, store=store, resolve=lambda i: None,
        linked={ME}, own=sources.OWN, allowed=set(), freeze=lambda ref: None, deadline_fn=None,
        autoadd_fn=cycle._default_autoadd))


def follow_ups(store):
    return acting.records(store, kind="follow_up")


def did_cards(store):
    return [c for c in store.state()["cards"] if c["action"]["method"] == "undo_add"]


def test_a_qualifying_mail_gets_its_reminder_record_and_wisp_did_this_card_in_the_saved_brief(native, store):
    acting.set_enabled(True, now=NOW - 2 * D)              # the yes, before the mail arrived (NOW - 30 h)
    assert run(store)["status"] == "done"
    [plan] = native.writes
    assert native.actions == ["follow_up"]                 # the write's last check asks about follow-ups, not adding
    assert plan["title"] == "Reply to Priya Shah" and plan["quote"] == "Can you review my section of the report?"
    [record] = follow_ups(store)
    assert record["status"] == "added" and record["undo"]["source_id"] == "R-1" and record["account"] == ME
    [card] = did_cards(store)
    assert card["action"]["record"] == record["fingerprint"] and card["title"] == "Reminder to reply to Priya Shah"
    assert card["account"] == ME and native.subprocess == []


def test_with_the_gate_off_nothing_is_written_recorded_or_even_read_from_sent(native, store):
    assert run(store)["status"] == "done"
    assert native.writes == [] and follow_ups(store) == [] and did_cards(store) == []
    assert native.gmail_sent == [] and native.apple_sent == []


def test_a_yes_that_named_only_add_and_hold_writes_no_follow_up(native, store, tmp_path):
    path = acting._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabled": True, "enabled_at": NOW - 2 * D, "asked": True,
                                "covers": {"add": NOW - 2 * D, "hold": NOW - 2 * D}}), encoding="utf-8")
    assert acting.enabled("add") and not acting.enabled("follow_up")
    assert run(store)["status"] == "done"
    assert native.writes == [] and follow_ups(store) == [] and native.gmail_sent == []


def test_a_yes_that_named_only_follow_ups_still_writes_them(native, store):
    path = acting._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabled": True, "enabled_at": NOW - 2 * D, "asked": True,
                                "covers": {"follow_up": NOW - 2 * D}}), encoding="utf-8")
    assert not acting.enabled("add") and acting.enabled("follow_up")
    assert run(store)["status"] == "done"
    assert len(native.writes) == 1 and native.actions == ["follow_up"] and len(follow_ups(store)) == 1


def test_a_turn_starting_between_auto_add_and_follow_ups_stops_the_follow_up(native, store, monkeypatch):
    acting.set_enabled(True, now=NOW - 2 * D)
    turn = {"on": False}
    real = autoadd.step

    async def then_a_turn(*a, **k):
        got = await real(*a, **k)
        turn["on"] = True
        return got
    monkeypatch.setattr(autoadd, "step", then_a_turn)
    run(store, abort=lambda: turn["on"])
    assert turn["on"] is True                              # auto-add really ran first
    assert native.writes == [] and follow_ups(store) == [] and native.gmail_sent == []


def test_undo_through_the_executor_removes_the_reminder_and_a_second_cycle_writes_nothing(native, store):
    acting.set_enabled(True, now=NOW - 2 * D)
    run(store)
    [card] = did_cards(store)
    acting._undoers.pop("follow_up", None)                 # as after a restart: found through acting._HOME
    result = asyncio.run(executor.fire(card["id"], store=store, now=NOW + 120))
    assert result["ok"] is True and result["removed"] is True
    assert [d["source_id"] for d in native.deletes] == ["R-1"]
    [record] = follow_ups(store)
    assert record["status"] == "undone"
    run(store, now=NOW + 600)
    assert len(native.writes) == 1 and follow_ups(store)[0]["status"] == "undone" and did_cards(store) == []


# --- the in-flight record: "dispatching" before the write ----------------------------------------


async def _google_sent():
    return list(F.MAILED), None, True


def step(store, *, write, now=NOW):
    return followups.step([{"item": mail(), "result": {}}], {"reminders": []}, now=now, tz=TZ, store=store,
                          abort=lambda: False, write=write, read_sent=_google_sent, records=[],
                          quiet=F.never_quiet, reminders=[], gmail=lambda _n: ({ME}, set()),
                          apple=lambda *_a, **_k: False, lookup=F.lookup)


def test_the_record_says_dispatching_during_the_write_with_no_card(native, store):
    acting.set_enabled(True, now=NOW - 2 * D)
    seen, cards = [], []

    async def write(plan):                       # asserts after: step turns a raise here into "unknown"
        seen.extend(follow_ups(store))
        cards.append(autoadd.done_cards(store, NOW + 60))
        return {"ok": True, "source_id": "R-9", "title": plan["title"]}
    [fp] = asyncio.run(step(store, write=write))["added"]
    [during] = seen
    assert cards == [[]]                         # in flight: no "Wisp did this" card
    assert during["status"] == "dispatching" and during["fingerprint"] == fp and during["attempts"] == 1
    assert "undo" not in during and during["person"] and during["card"]["title"] == "Reminder to reply to Priya Shah"
    [after] = follow_ups(store)
    assert after["status"] == "added" and after["attempts"] == 1 and after["undo"]["source_id"] == "R-9"


def test_a_cancelled_write_leaves_dispatching_propagates_and_is_never_written_again(native, store):
    acting.set_enabled(True, now=NOW - 2 * D)
    started = []

    async def hangs(plan):
        started.append(plan)
        await asyncio.Event().wait()

    async def cancel_mid_write():
        task = asyncio.ensure_future(step(store, write=hangs))
        while not started:
            await asyncio.sleep(0)
        task.cancel()
        await task
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancel_mid_write())
    [record] = follow_ups(store)
    assert record["status"] == "dispatching"
    assert autoadd.done_cards(store, NOW + 60) == []
    # The next step: it may have landed, so it becomes "unknown", counts, and is never written again.
    again = F.Writer()
    assert asyncio.run(step(store, write=again, now=NOW + 600))["added"] == [] and again.calls == []
    [record] = follow_ups(store)
    assert record["status"] == "unknown" and "undo" not in record and record["unconfirmed_at"] == NOW + 600
    assert acting.done_since(store, NOW - D, kind="follow_up") == 1
    assert autoadd.done_cards(store, NOW + 660) == []


def test_a_write_that_raises_is_unknown_and_never_retried(native, store):
    acting.set_enabled(True, now=NOW - 2 * D)

    async def boom(_plan):
        raise TimeoutError
    assert asyncio.run(step(store, write=boom))["added"] == []
    assert follow_ups(store)[0]["status"] == "unknown"
    again = F.Writer()
    assert asyncio.run(step(store, write=again, now=NOW + 600))["added"] == [] and again.calls == []


def test_a_blocked_write_clears_dispatching_uncounted_and_may_try_again(native, store):
    acting.set_enabled(True, now=NOW - 2 * D)
    blocked = F.Writer({"ok": False, "status": "blocked"})
    assert asyncio.run(step(store, write=blocked))["added"] == []
    [record] = follow_ups(store)
    assert record["status"] == "blocked" and record["attempts"] == 0
    assert acting.done_since(store, NOW - D, kind="follow_up") == 0
    assert asyncio.run(step(store, write=blocked, now=NOW + 300))["added"] == [] and len(blocked.calls) == 2
    assert follow_ups(store)[0]["attempts"] == 0
    ok = F.Writer()
    assert asyncio.run(step(store, write=ok, now=NOW + 600))["added"] and len(ok.calls) == 1
    assert follow_ups(store)[0]["attempts"] == 1


def _dispatching(store, fp, person, at=NOW - 600):
    acting.record(store, fp, kind="follow_up", status="dispatching", now=at, title="Reply to Sam Lee",
                  person=person, attempts=1, card={"meta": "REMINDER", "title": "x", "did": "y"})


def test_stale_dispatching_records_are_settled_as_unknown_and_stop_nobody_else(native, store):
    acting.set_enabled(True, now=NOW - 2 * D)
    _dispatching(store, "followup:a", "followup-person:a")
    _dispatching(store, "followup:b", "followup-person:b")
    write = F.Writer()
    out = asyncio.run(step(store, write=write))
    stale = {r["fingerprint"]: r["status"] for r in follow_ups(store) if r["fingerprint"] in ("followup:a", "followup:b")}
    assert stale == {"followup:a": "unknown", "followup:b": "unknown"}      # may have landed: never written again
    # There is no daily cap any more (2026-11-13): two unsettled ones do not stop someone else's.
    assert len(out["added"]) == len(write.calls) == 1


def test_a_conversion_that_cant_be_saved_never_fails_the_step_and_still_blocks(native, store, monkeypatch):
    acting.set_enabled(True, now=NOW - 2 * D)
    item = mail()
    fp = followups.fingerprint(item, item["received_ts"])
    _dispatching(store, fp, followups.person_key(item))
    put = store.auto_add_put

    def refuses_conversion(key, entry):
        if entry.get("unconfirmed_at") is not None:
            raise OSError("disk full")
        put(key, entry)
    monkeypatch.setattr(store, "auto_add_put", refuses_conversion)
    write = F.Writer()
    assert asyncio.run(step(store, write=write))["added"] == [] and write.calls == []
    assert store.auto_added()[fp]["status"] == "dispatching"
    # And two unconverted ones still fill the follow-up kind's own two a day for anyone else.
    _dispatching(store, "followup:b", "followup-person:b")
    assert followups._blocked(store, NOW)[1] == 2


def test_a_dispatch_that_cant_be_recorded_is_never_written(native, store, monkeypatch):
    acting.set_enabled(True, now=NOW - 2 * D)
    real = acting.record

    def refuses(store_, fp, **kw):
        if kw.get("status") == "dispatching":
            raise OSError("disk full")
        return real(store_, fp, **kw)
    monkeypatch.setattr(acting, "record", refuses)
    write = F.Writer()
    assert asyncio.run(step(store, write=write))["added"] == [] and write.calls == []
    assert follow_ups(store) == []


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_followups_cycle.py"' in manifest
