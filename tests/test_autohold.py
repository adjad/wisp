"""Hold time for a deadline (service/proactive/autohold.py): under "Let Wisp act for me" the free
slot is pinned on Day without a tap, Wisp-local, one per deadline, with Undo. Fake holds and a
fake Day store; nothing reads a calendar or writes anywhere real."""
from __future__ import annotations

import asyncio

import pytest

from service import config
from service.proactive import acting, autoadd, autohold
from service.proactive.store import ProactiveStore

NOW = 1_800_000_000.0


@pytest.fixture
def setting(monkeypatch, tmp_path):
    saved: dict = {}
    # The gate's own file (acting._path), in this test's folder: never ~/.moe.
    if hasattr(acting, "_path"):
        monkeypatch.setattr(acting, "_path", lambda: tmp_path / "home" / "act_for_me.json")
    monkeypatch.setattr(config, "user_overlay_valid", lambda: True)
    monkeypatch.setattr(config, "models_config", lambda: dict(saved))
    monkeypatch.setattr(config, "_save_overlay", lambda patch: saved.update(patch))
    from service import idle
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    return saved


@pytest.fixture(autouse=True)
def native(monkeypatch):
    """The native id behind each card ref (never the real assistant store). Empty: unreadable."""
    ids: dict[str, str] = {}
    monkeypatch.setattr(autohold, "resolve_native", lambda ref: ids.get(ref))
    return ids


@pytest.fixture
def store(tmp_path):
    return ProactiveStore(tmp_path / "proactive")


def card(n=1, method="block_time"):
    start = NOW + 3600 * n
    return {"id": f"card:deadline{n}", "title": f"Worksheet {n} for STAT 5", "why": f"Worksheet {n} for STAT 5 is due tomorrow.",
            "account": "", "sources": [{"kind": "calendar", "ref": f"cal:{n}", "who": "", "quote": f"Worksheet {n}"}],
            "action": {"method": method, "button": "Block 2–3 PM?", "slot": {"start": start, "end": start + 3600},
                       "envelope": {"kind": "block_time", "title": f"Worksheet {n} for STAT 5", "start": start,
                                    "duration_min": 60, "day": "2027-01-15", "timezone": "UTC"}}}


class Holds:
    def __init__(self, outcome=None):
        self.calls, self.outcome = [], outcome

    def __call__(self, c):
        self.calls.append(c["id"])
        return dict(self.outcome) if self.outcome else {"ok": True, "status": "succeeded", "task_id": "task-" + c["id"]}


def run(store, cards, holds, abort=lambda: False, now=NOW):
    return autohold.step(cards, now=now, tz="UTC", store=store, abort=abort, hold=holds, hours=lambda slot, tz: True)


def test_nothing_is_held_until_the_person_says_yes(setting, store):
    holds = Holds()
    assert run(store, [card(1)], holds) == set() and holds.calls == [] and store.auto_added() == {}
    acting.set_enabled(True, now=NOW - 60)
    assert run(store, [card(1)], holds) == {"card:deadline1"} and holds.calls == ["card:deadline1"]


def test_a_hold_becomes_one_wisp_did_this_card_that_names_the_day_plan(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    run(store, [card(1)], Holds())
    [done] = autoadd.done_cards(store, NOW + 1)
    assert done["meta"] == "DAY PLAN" and done["title"] == "Held 2–3 PM for Worksheet 1 for STAT 5"
    assert "Day plan in Wisp, not on your calendar" in done["body"] and "due tomorrow" in done["body"]
    assert done["action"]["method"] == "undo_add" and "task-" not in str(done)
    assert autoadd.done_cards(store, NOW + 2 * 3600 + 1) == []            # gone once the held hour has passed


def test_one_hold_per_deadline_two_a_cycle_and_no_daily_cap_so_holds_continue_past_ten_other_actions(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    holds = Holds()
    cards = [card(1), card(2), card(3), card(4, method="reminder")]
    assert run(store, cards, holds) == {"card:deadline1", "card:deadline2"}          # MAX_PER_CYCLE
    assert run(store, cards, holds) == {"card:deadline3"}                            # never the first two again
    assert holds.calls == ["card:deadline1", "card:deadline2", "card:deadline3"]     # and never a non-hold card
    for i in range(20):                                                              # twice the old shared cap
        acting.record(store, f"other-{i}", kind="reminder", status="added", now=NOW, title="x")
    assert acting.done_since(store, NOW - acting.DAY_S) >= 20
    assert run(store, [card(9)], holds) == {"card:deadline9"} and "card:deadline9" in holds.calls   # still acts


def test_off_or_a_turn_stops_it_before_the_hold(setting, store, monkeypatch):
    acting.set_enabled(True, now=NOW - 60)
    holds = Holds()
    assert run(store, [card(1)], holds, abort=lambda: True) == set() and holds.calls == []
    from service import idle
    monkeypatch.setattr(idle, "foreground_busy", lambda: True)
    assert run(store, [card(1)], holds) == set() and holds.calls == []
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    acting.set_enabled(False)
    assert run(store, [card(1)], holds) == set() and holds.calls == []


def test_a_slot_that_went_or_is_already_planned_records_nothing_and_a_failure_is_tried_once_more(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    for outcome in ({"ok": False, "status": "blocked", "error": "not free"},
                    {"ok": True, "status": "succeeded", "task_id": "t", "already": True}):
        assert run(store, [card(1)], Holds(outcome)) == set() and store.auto_added() == {}
    failing = Holds({"ok": False, "status": "failed", "error": "not saved"})
    for _ in range(4):
        assert run(store, [card(2)], failing) == set()
    assert len(failing.calls) == autohold.MAX_ATTEMPTS and autoadd.done_cards(store, NOW + 1) == []
    assert acting.done_since(store, NOW - acting.DAY_S) == 0                  # a failed hold counts as nothing done


class Day:
    def __init__(self, task):
        self.task, self.saves = task, []

    def today_task(self, task_id):
        return dict(self.task) if self.task and self.task["id"] == task_id else None

    def today_save_task(self, values, *, task_id, revision):
        assert revision == self.task["revision"]
        self.saves.append((task_id, values))
        self.task = {**self.task, **values, "revision": revision + 1}
        return self.task


def test_undo_removes_only_wisps_own_block_and_the_deadline_is_never_held_or_offered_again(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    c = card(1)
    run(store, [c], Holds())
    record = store.auto_added()[autohold.fingerprint(c)]
    start = c["action"]["slot"]["start"]
    mine = {"id": "task-card:deadline1", "status": "active", "pinned_start": start,
            "title": "Worksheet 1 for STAT 5", "revision": 3}
    day = Day(dict(mine))
    assert autohold._undo(record, day) == {"ok": True, "status": "succeeded", "message": "Removed the hold from your Day plan"}
    assert day.saves == [("task-card:deadline1", {"status": "dismissed"})]
    # A block the person moved or renamed is theirs.
    for changed in ({**mine, "pinned_start": start + 1800}, {**mine, "title": "Worksheet 1 (with Sam)"}):
        moved = Day(changed)
        assert autohold._undo(record, moved)["status"] == "blocked" and moved.saves == []
    assert autohold._undo(record, Day(None))["ok"] is True                      # already gone
    assert autohold._undo({"undo": {}}, day)["status"] == "blocked"
    # Through the card: the registered undoer, by the stored record.
    [done] = autoadd.done_cards(store, NOW + 1)

    async def fake(rec):
        return autohold._undo(rec, Day(dict(mine)))
    acting.register_undoer("hold", fake)
    try:
        assert asyncio.run(autoadd.undo(done, store, now=NOW + 2))["ok"] is True
    finally:
        acting.register_undoer("hold", autohold.undo)
    assert store.auto_added()[autohold.fingerprint(c)]["status"] == "undone"
    holds = Holds()
    assert run(store, [c], holds) == set() and holds.calls == []
    assert autohold.without_undone([c, card(2)], store) == [card(2)]


def test_the_undoer_is_found_after_a_restart_and_the_words_say_what_is_true():
    acting._undoers.pop("hold", None)
    from service.proactive import followups
    acting._undoers.pop("follow_up", None)
    assert acting.undoer("hold") is autohold.undo and acting.undoer("follow_up") is followups.undo
    assert any("Day plan" in line and "never on your calendar" in line for line in acting.DOES)


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_autohold.py"' in manifest


def _hours(monkeypatch, working, quiet):
    """The person's hours as user_hours' own Hours objects (never the real file)."""
    from service import user_hours
    monkeypatch.setattr(user_hours, "working_hours", lambda: working)
    monkeypatch.setattr(user_hours, "quiet_hours", lambda: quiet)


def test_a_slot_in_quiet_hours_or_outside_working_hours_is_never_held(setting, store, monkeypatch):
    # Changed with finding 8: hours are read as Hours objects (user_hours never raises; it
    # falls back to its defaults), so the old "reading the hours raised" case is gone.
    from service.user_hours import ALL_DAYS, DEFAULT_QUIET, DEFAULT_WORKING, Hours
    acting.set_enabled(True, now=NOW - 60)
    holds = Holds()
    go = lambda: autohold.step([card(1)], now=NOW, tz="UTC", store=store, abort=lambda: False, hold=holds)   # noqa: E731
    # card(1) is 09:00-10:00 UTC on Friday 2027-01-15.
    _hours(monkeypatch, DEFAULT_WORKING, Hours(8 * 60, 9 * 60 + 30, ALL_DAYS))      # quiet till 9:30
    assert go() == set() and holds.calls == [] and store.auto_added() == {}
    _hours(monkeypatch, Hours(10 * 60, 17 * 60, ALL_DAYS), DEFAULT_QUIET)            # work starts at 10
    assert go() == set() and holds.calls == []
    # The END of the slot counts too: an hour that runs past the working day is not held.
    _hours(monkeypatch, Hours(6 * 60, 9 * 60 + 59, ALL_DAYS), DEFAULT_QUIET)
    assert go() == set() and holds.calls == []
    # The defaults (9-5 weekdays, quiet 10 PM-8 AM) are what apply until the person sets hours.
    _hours(monkeypatch, DEFAULT_WORKING, DEFAULT_QUIET)
    assert go() == {"card:deadline1"}
    assert autohold.inside_hours({"start": 5.0, "end": 5.0}, "UTC") is False and autohold.inside_hours({}, "UTC") is False


def test_quiet_hours_inside_a_long_slot_or_a_slot_past_the_working_day_are_seen(monkeypatch):
    from service.user_hours import ALL_DAYS, DEFAULT_QUIET, Hours
    day = NOW - 8 * 3600                                                    # Friday 00:00 UTC
    slot = {"start": day + 9 * 3600, "end": day + 13 * 3600}                # 9 AM-1 PM
    # Quiet 10:15-10:40, strictly inside the slot: neither end touches it.
    _hours(monkeypatch, Hours(8 * 60, 18 * 60, ALL_DAYS), Hours(10 * 60 + 15, 10 * 60 + 40, ALL_DAYS))
    assert autohold.inside_hours(slot, "UTC") is False
    _hours(monkeypatch, Hours(8 * 60, 18 * 60, ALL_DAYS), Hours(0, 0, ALL_DAYS))       # no quiet hours
    assert autohold.inside_hours(slot, "UTC") is True
    # Working hours end at 12:30, inside the slot.
    _hours(monkeypatch, Hours(8 * 60, 12 * 60 + 30, ALL_DAYS), DEFAULT_QUIET)
    assert autohold.inside_hours(slot, "UTC") is False
    # Ends exactly when the working day ends: held (the last minute is 12:59).
    _hours(monkeypatch, Hours(8 * 60, 13 * 60, ALL_DAYS), DEFAULT_QUIET)
    assert autohold.inside_hours(slot, "UTC") is True
    # Saturday under the default weekday hours: not held.
    from service.user_hours import DEFAULT_WORKING
    _hours(monkeypatch, DEFAULT_WORKING, DEFAULT_QUIET)
    sat = {"start": slot["start"] + 86400, "end": slot["start"] + 86400 + 3600}
    assert autohold.inside_hours(sat, "UTC") is False


# --- review repairs -------------------------------------------------------------------------

def moved(c, ref, n=7):
    """The same deadline after a reschedule: a new commitment row id, a new time, a new card id."""
    start = NOW + 3600 * n
    return {**c, "id": c["id"] + "-moved", "sources": [{**c["sources"][0], "ref": ref}],
            "action": {**c["action"], "slot": {"start": start, "end": start + 3600},
                       "envelope": {**c["action"]["envelope"], "start": start}}}


def undo_through_the_store(store, c):
    fp = autohold.fingerprint(c)
    store.auto_add_put(fp, {**store.auto_added()[fp], "status": "undone"})


def test_a_rescheduled_deadline_is_never_held_again_after_undo(setting, store, native):
    acting.set_enabled(True, now=NOW - 60)
    native.update({"cal:1": "evt-1", "cal:1-next": "evt-1", "cal:2": "evt-2", "cal:2-next": "evt-2"})
    c = card(1)
    assert run(store, [c], Holds()) == {"card:deadline1"}
    undo_through_the_store(store, c)
    later = moved(c, "cal:1-next")
    assert autohold.fingerprint(later) == autohold.fingerprint(c)          # the native id, not the row id
    holds = Holds()
    assert run(store, [later, card(2)], holds) == {"card:deadline2"} and holds.calls == ["card:deadline2"]
    assert autohold.without_undone([later, card(2)], store) == [card(2)]   # nor offered as a tap card
    # Held (not undone) and then rescheduled: still one hold per deadline.
    holds = Holds()
    assert run(store, [moved(card(2), "cal:2-next")], holds) == set() and holds.calls == []


def test_without_a_native_id_the_source_and_title_still_block_it(setting, store, native):
    acting.set_enabled(True, now=NOW - 60)
    c = card(5)                                                             # no native id can be read
    run(store, [c], Holds())
    undo_through_the_store(store, c)
    later = moved(c, "cal:5-next")
    assert autohold.fingerprint(later) != autohold.fingerprint(c)
    holds = Holds()
    assert run(store, [later], holds) == set() and holds.calls == []
    assert autohold.without_undone([later], store) == []
    # Readable now, unreadable then: the title still matches. Two deadlines with their own
    # native ids never block each other by title.
    native["cal:5-third"] = "evt-5"
    assert autohold.without_undone([moved(c, "cal:5-third")], store) == []
    native.update({"cal:6": "evt-6", "cal:6b": "evt-6b"})
    twin = {**card(6), "sources": [{**card(6)["sources"][0], "quote": "Weekly reading"}]}
    run(store, [twin], Holds())
    undo_through_the_store(store, twin)
    other = {**moved(twin, "cal:6b"), "id": "card:other"}
    assert autohold.without_undone([other], store) == [other] and run(store, [other], Holds()) == {"card:other"}


class Seq:
    def __init__(self, *outcomes):
        self.calls, self.outcomes = [], list(outcomes)

    def __call__(self, c):
        self.calls.append(c["id"])
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return dict(out)


def test_a_save_whose_read_back_failed_is_claimed_on_the_retry(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    c = card(1)
    # Changed with finding 7: the failed save names its task (saved_task_id), and only that
    # exact task is claimed on the retry.
    holds = Seq({"ok": False, "status": "failed", "error": "The Day task wasn't saved.", "saved_task_id": "t-9"},
                {"ok": True, "status": "succeeded", "task_id": "t-9", "already": True})
    assert run(store, [c], holds) == set()
    assert store.auto_added()[autohold.fingerprint(c)]["saved_task_id"] == "t-9"
    assert run(store, [c], holds) == {"card:deadline1"}
    record = store.auto_added()[autohold.fingerprint(c)]
    assert record["status"] == "added" and record["undo"]["task_id"] == "t-9" and record["attempts"] == 2
    assert len(autoadd.done_cards(store, NOW + 1)) == 1 and acting.done_since(store, NOW - acting.DAY_S) == 1


def test_an_already_after_a_failure_that_saved_nothing_or_another_task_stays_the_persons(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    # Nothing was saved (no saved_task_id): a block the person then pinned is theirs.
    c = card(1)
    holds = Seq({"ok": False, "status": "failed", "error": "Couldn't read today's plan."},
                {"ok": True, "status": "succeeded", "task_id": "theirs", "already": True})
    assert run(store, [c], holds) == set() and run(store, [c], holds) == set()
    assert store.auto_added()[autohold.fingerprint(c)]["status"] == "failed"
    assert autoadd.done_cards(store, NOW + 1) == []
    # Saved t-1, but the "already" is another task: still theirs.
    c2 = card(2)
    holds = Seq({"ok": False, "status": "failed", "error": "x", "saved_task_id": "t-1"},
                {"ok": True, "status": "succeeded", "task_id": "t-2", "already": True})
    assert run(store, [c2], holds) == set() and run(store, [c2], holds) == set()
    assert store.auto_added()[autohold.fingerprint(c2)]["status"] == "failed"


def test_the_real_hold_never_adopts_the_persons_block_after_an_unreadable_plan(setting, store):
    from service.proactive import deadlines
    acting.set_enabled(True, now=NOW - 60)
    day, c = FakeDay(), card(1)
    snapshot = day.today_snapshot

    def unreadable(d, tz):
        raise OSError("locked")
    day.today_snapshot = unreadable
    real = lambda x: deadlines.hold(x, now=NOW, store=day)                   # noqa: E731
    first = deadlines.hold(c, now=NOW, store=day)
    assert first["status"] == "failed" and "saved_task_id" not in first
    assert run(store, [c], real) == set() and not day.tasks
    # The person taps the card themselves: the same title and start, their own task.
    day.today_snapshot = snapshot
    day.today_save_task({"title": "Worksheet 1 for STAT 5", "pinned_start": c["action"]["slot"]["start"],
                         "duration_minutes": 60, "day": "2027-01-15", "timezone": "UTC"})
    assert run(store, [c], real) == set()
    assert store.auto_added()[autohold.fingerprint(c)]["status"] == "failed"
    assert day.tasks["t1"]["status"] == "active" and autoadd.done_cards(store, NOW + 1) == []
    # Saved but not read back: the failure names the saved task.
    other = FakeDay()
    other.read_back = False
    out = deadlines.hold(card(2), now=NOW, store=other)
    assert out["status"] == "failed" and out["saved_task_id"] == "t1"


def test_an_already_planned_block_with_no_attempt_of_wisps_is_left_alone(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    holds = Seq({"ok": True, "status": "succeeded", "task_id": "theirs", "already": True})
    assert run(store, [card(1)], holds) == set() and store.auto_added() == {}


def test_the_record_says_dispatching_during_the_hold_and_a_cut_off_one_is_never_held_again(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    seen = []

    def watching(c):
        seen.append({k: dict(v) for k, v in store.auto_added().items()})
        return {"ok": True, "status": "succeeded", "task_id": "t1"}
    assert run(store, [card(1)], watching) == {"card:deadline1"}
    [[during]] = [list(s.values()) for s in seen]
    assert during["status"] == "dispatching" and during["kind"] == "hold" and "card" not in during
    assert [r["status"] for r in store.auto_added().values()] == ["added"]

    # A hold cut off before its outcome was recorded (the process died inside the write).
    class Died(BaseException):
        pass

    def dies(c):
        raise Died()
    with pytest.raises(Died):
        run(store, [card(2)], dies)
    left = [r for r in store.auto_added().values() if r["status"] == "dispatching"]
    assert len(left) == 1 and acting.shown(left[0], NOW) is False
    again = Holds()
    assert run(store, [card(2)], again, now=NOW + 600) == set() and again.calls == []     # even before it is settled
    assert acting.settle(store, NOW + 700) == 1
    assert run(store, [card(2)], again, now=NOW + 800) == set() and again.calls == []
    assert {r["check"] for r in acting.recent(store, NOW + 900) if r["check"]} == {"your Day plan"}


def test_a_hold_that_cant_be_marked_first_is_not_made(setting, store, monkeypatch):
    acting.set_enabled(True, now=NOW - 60)

    def refuse(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(store, "auto_add_put", refuse)
    holds = Holds()
    assert run(store, [card(1)], holds) == set() and holds.calls == []


def test_a_hold_that_raised_is_unknown_counted_and_never_retried(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    c = card(1)
    holds = Seq(RuntimeError("database is locked"))
    assert run(store, [c], holds) == set()
    assert store.auto_added()[autohold.fingerprint(c)]["status"] == "unknown"
    assert acting.done_since(store, NOW - acting.DAY_S) == 1
    assert run(store, [c], holds) == set() and holds.calls == ["card:deadline1"]
    assert autoadd.done_cards(store, NOW + 1) == []


def test_the_write_is_checked_against_the_clock_at_that_moment(setting, store, monkeypatch):
    from service.proactive import deadlines
    acting.set_enabled(True, now=NOW - 60)
    seen = []

    def fake(c, *, now, store=None):
        seen.append(now)
        return {"ok": True, "status": "succeeded", "task_id": "t-1"}
    monkeypatch.setattr(deadlines, "hold", fake)
    held = autohold.step([card(1)], now=NOW, tz="UTC", store=store, abort=lambda: False,
                         hours=lambda slot, tz: True, clock=lambda: NOW + 900)
    assert held == {"card:deadline1"} and seen == [NOW + 900]
    assert store.auto_added()[autohold.fingerprint(card(1))]["at"] == NOW   # the cap still counts by the cycle


def test_undo_of_a_block_the_person_finished_says_so_and_leaves_it(setting, store):
    acting.set_enabled(True, now=NOW - 60)
    c = card(1)
    run(store, [c], Holds())
    record = store.auto_added()[autohold.fingerprint(c)]
    finished = Day({"id": "task-card:deadline1", "status": "done", "pinned_start": c["action"]["slot"]["start"],
                    "title": "Worksheet 1 for STAT 5", "revision": 2})
    result = autohold._undo(record, finished)
    assert result == {"ok": True, "status": "succeeded", "message": "You already finished that one, so Wisp left it"}
    assert finished.saves == [] and finished.task["status"] == "done"
    gone = Day({"id": "task-card:deadline1", "status": "dismissed", "revision": 2})
    assert autohold._undo(record, gone)["message"] == "It was already off your Day plan" and gone.saves == []


class FakeDay:
    """Just enough of the assistant store for the real deadlines.hold: a Day plan in memory."""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.read_back = True

    def today_snapshot(self, day, tz):
        from service.assistant.today import default_preferences
        return {"tasks": [dict(t) for t in self.tasks.values()], "commitments": [],
                "preferences": default_preferences(day, tz), "sources": {}}

    def today_save_task(self, values, *, task_id=None, revision=None):
        if task_id is None:
            task_id = f"t{len(self.tasks) + 1}"
            self.tasks[task_id] = {"id": task_id, "status": "active", "revision": 1, **values}
        else:
            assert revision == self.tasks[task_id]["revision"]
            self.tasks[task_id] = {**self.tasks[task_id], **values, "revision": revision + 1}
        return dict(self.tasks[task_id])

    def today_task(self, task_id):
        return dict(self.tasks[task_id]) if self.read_back and task_id in self.tasks else None


def test_the_real_hold_retry_and_already_paths_end_in_one_tracked_block_with_undo(setting, store):
    from service.proactive import deadlines
    acting.set_enabled(True, now=NOW - 60)
    day, c = FakeDay(), card(1)
    real = lambda x: deadlines.hold(x, now=NOW, store=day)                   # noqa: E731
    day.read_back = False                                                   # saved, but the read-back fails
    assert run(store, [c], real) == set() and store.auto_added()[autohold.fingerprint(c)]["status"] == "failed"
    day.read_back = True
    assert run(store, [c], real) == {"card:deadline1"} and len(day.tasks) == 1   # claimed, not saved twice
    record = store.auto_added()[autohold.fingerprint(c)]
    assert record["status"] == "added" and record["undo"]["task_id"] == "t1"
    assert autohold._undo(record, day)["message"] == "Removed the hold from your Day plan"
    assert day.tasks["t1"]["status"] == "dismissed"
    # A block the person pinned themselves at that time is "already" with no attempt of Wisp's: left alone.
    theirs = FakeDay()
    theirs.today_save_task({"title": "Worksheet 1 for STAT 5", "pinned_start": c["action"]["slot"]["start"],
                            "duration_minutes": 60, "day": "2027-01-15", "timezone": "UTC"})
    fresh = card(1)
    fresh["sources"] = [{**fresh["sources"][0], "ref": "cal:fresh", "quote": "Something else"}]
    assert run(store, [fresh], lambda x: deadlines.hold(x, now=NOW, store=theirs)) == set()
    assert autohold.fingerprint(fresh) not in store.auto_added()


# --- a repeating deadline, and a reminder Wisp itself added -------------------------------------

def _with_native(monkeypatch, mapping):
    monkeypatch.setattr(autohold, "resolve_native", lambda ref: mapping.get(ref))


def test_next_weeks_occurrence_of_a_repeating_deadline_is_held_but_a_snooze_is_not(setting, store, monkeypatch):
    acting.set_enabled(True, now=NOW - 60)
    _with_native(monkeypatch, {"cal:1": "EK-weekly", "cal:1b": "EK-weekly", "cal:1c": "EK-weekly"})
    holds = Holds()
    first = {**card(1), "due_ts": NOW + 86400}
    assert run(store, [first], holds) == {"card:deadline1"}
    # Snoozed by a day: a new row id, the same native id, a due date close by. Still the same deadline.
    snoozed = {**card(1), "id": "card:deadline1b", "due_ts": NOW + 2 * 86400,
               "sources": [{"kind": "calendar", "ref": "cal:1b", "who": "", "quote": "Worksheet 1"}]}
    assert run(store, [snoozed], holds) == set()
    # A week later: the next occurrence, held in its own right, once.
    later = {**card(1), "id": "card:deadline1c", "due_ts": NOW + 8 * 86400,
             "sources": [{"kind": "calendar", "ref": "cal:1c", "who": "", "quote": "Worksheet 1"}]}
    assert run(store, [later], holds, now=NOW + 7 * 86400) == {"card:deadline1c"}
    assert run(store, [later], holds, now=NOW + 7 * 86400) == set()
    assert holds.calls == ["card:deadline1", "card:deadline1c"]


def test_a_reminder_wisp_itself_auto_added_is_not_also_given_a_held_hour(setting, store, monkeypatch):
    acting.set_enabled(True, now=NOW - 60)
    acting.record(store, "autoadd:1", kind="reminder", status="added", now=NOW - 30, title="Submit the report",
                  source_id="R-9", reminders=[{"source_id": "R-9"}, {"source_id": "R-10"}])
    _with_native(monkeypatch, {"cal:1": "R-9", "cal:2": "R-10", "cal:3": "R-other"})
    holds = Holds()
    assert run(store, [card(1), card(2), card(3)], holds) == {"card:deadline3"}
    assert holds.calls == ["card:deadline3"]


# --- repeating deadlines across Undo (finding 6), unconfirmed auto-adds (finding 11) ---------------

DAY = 86400


def occurrence(ref, due, n=1):
    """One occurrence (or a snoozed copy) of the weekly "Worksheet 1": its own row and card id."""
    return {**card(n), "id": f"card:{ref}", "due_ts": due,
            "sources": [{"kind": "calendar", "ref": ref, "who": "", "quote": "Worksheet 1"}]}


def undo_record(store, fp):
    store.auto_add_put(fp, {**store.auto_added()[fp], "status": "undone"})


def _key_of(store, card_id):
    return next(k for k, r in store.auto_added().items() if r.get("item_id") == card_id.removeprefix("card:"))


def test_a_later_occurrence_undone_and_then_snoozed_is_not_held_again(setting, store, native):
    acting.set_enabled(True, now=NOW - 60)
    native.update({r: "EK-weekly" for r in ("w1", "w2", "w2-snoozed")})
    holds = Holds()
    assert run(store, [occurrence("w1", NOW + DAY)], holds) == {"card:w1"}
    week2 = occurrence("w2", NOW + 8 * DAY)
    assert run(store, [week2], holds, now=NOW + 7 * DAY) == {"card:w2"}
    rec = store.auto_added()[_key_of(store, "card:w2")]
    assert rec["native_key"] and rec["deadline_ts"] == NOW + 8 * DAY and "EK-weekly" not in str(rec)
    undo_record(store, _key_of(store, "card:w2"))
    snoozed = occurrence("w2-snoozed", NOW + 9 * DAY)
    assert run(store, [snoozed], holds, now=NOW + 7 * DAY) == set()
    assert autohold.without_undone([snoozed], store) == []
    assert holds.calls == ["card:w1", "card:w2"]


def test_a_first_occurrence_undone_and_snoozed_four_days_is_not_held_again(setting, store, native):
    acting.set_enabled(True, now=NOW - 60)
    native.update({"a": "EK-1", "a-later": "EK-1"})
    holds = Holds()
    assert run(store, [occurrence("a", NOW + DAY)], holds) == {"card:a"}
    undo_record(store, _key_of(store, "card:a"))
    later = occurrence("a-later", NOW + 5 * DAY)                    # 4 days > SAME_DEADLINE_S
    assert run(store, [later], holds) == set() and autohold.without_undone([later], store) == []
    assert holds.calls == ["card:a"]


def test_next_weeks_occurrence_is_still_held_once_when_nothing_was_undone(setting, store, native):
    acting.set_enabled(True, now=NOW - 60)
    native.update({"b1": "EK-2", "b2": "EK-2", "b2s": "EK-2"})
    holds = Holds()
    assert run(store, [occurrence("b1", NOW + DAY)], holds) == {"card:b1"}
    nxt = occurrence("b2", NOW + 8 * DAY)
    assert autohold.without_undone([nxt], store) == [nxt]
    assert run(store, [nxt], holds, now=NOW + 7 * DAY) == {"card:b2"}
    assert run(store, [nxt, occurrence("b2s", NOW + 9 * DAY)], holds, now=NOW + 7 * DAY) == set()
    assert holds.calls == ["card:b1", "card:b2"]


def test_a_run_of_short_snoozes_stays_one_deadline(setting, store, native):
    acting.set_enabled(True, now=NOW - 60)
    native.update({f"s{i}": "EK-3" for i in range(5)})
    holds = Holds()
    assert run(store, [occurrence("s0", NOW + DAY)], holds) == {"card:s0"}
    for i in range(1, 5):                                            # snoozed a day at a time: 4 days in all
        assert run(store, [occurrence(f"s{i}", NOW + (1 + i) * DAY)], holds) == set()
    assert holds.calls == ["card:s0"]


def test_an_occurrence_three_weeks_after_an_undone_one_is_held(setting, store, native):
    acting.set_enabled(True, now=NOW - 60)
    native.update({"c1": "EK-4", "c2": "EK-4", "c4": "EK-4"})
    holds = Holds()
    assert run(store, [occurrence("c1", NOW + DAY)], holds) == {"card:c1"}
    undo_record(store, _key_of(store, "card:c1"))
    week2 = occurrence("c2", NOW + 8 * DAY)                          # inside the 14-day window
    assert autohold.without_undone([week2], store) == [] and run(store, [week2], holds) == set()
    week4 = occurrence("c4", NOW + 22 * DAY)
    assert autohold.without_undone([week4], store) == [week4]
    assert run(store, [week4], holds, now=NOW + 21 * DAY) == {"card:c4"}
    assert holds.calls == ["card:c1", "card:c4"]


def test_an_unconfirmed_auto_add_with_no_id_blocks_a_hold_for_its_title_near_its_time(setting, store, native):
    acting.set_enabled(True, now=NOW - 60)
    acting.record(store, "autoadd:u", kind="reminder", status="unknown", now=NOW - 30, title="Worksheet 1",
                  due_ts=NOW + DAY)
    acting.record(store, "autoadd:f", kind="calendar", status="failed", now=NOW - 30, title="Worksheet 2",
                  start_ts=NOW + DAY)                               # failed: never landed, no block
    holds = Holds()
    same = occurrence("u1", NOW + DAY + 3600)
    far = {**occurrence("u2", NOW + 5 * DAY), "id": "card:u2"}
    two = {**occurrence("u3", NOW + DAY, n=2), "sources": [{"kind": "calendar", "ref": "u3", "who": "",
                                                           "quote": "Worksheet 2"}]}
    assert run(store, [same], holds) == set() and holds.calls == []
    assert run(store, [two], holds) == {"card:u3"}
    assert run(store, [far], holds) == {"card:u2"}                  # the same title, days away: its own deadline
    assert holds.calls == ["card:u3", "card:u2"]
