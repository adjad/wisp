"""A new install only offers routine actions until the user engages or 3 days pass.

Automatic routine actions write only to Wisp's own schedule (never Apple
Calendar or Reminders). Even so, a brand-new install has learned from very
little, so its first guesses arrive as offers ("tap to add") instead of items
that appear on their own.
"""
from __future__ import annotations

import pytest

from service.assistant import behavior_runtime as br
from service.assistant.store import FIRST_LAUNCH_KEY
from tests.test_behavior_runtime import NOW, FakeEngine, FakeHub, gym_conflict_nudge, make_runtime, tick


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    from service import config
    for key in ("WISP_BEHAVIOR", "WISP_QA_SEED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "USER_CONFIG", tmp_path / "cfg" / "config.yaml")
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()
    br.set_runtime(None)
    yield
    br.set_runtime(None)
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()


@pytest.fixture
def store(tmp_path):
    from service.assistant.store import AssistantStore
    return AssistantStore(tmp_path / "assistant.db")


def _first_launch(store, value: float | str) -> None:
    store.record_completion(FIRST_LAUNCH_KEY, value if isinstance(value, str) else repr(value))


def _commitments(store) -> int:
    return store._db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]


def _nudge_event(hub) -> dict:
    [sent] = [e for e in hub.events if e["event"]["type"] == "nudge"]
    return sent["event"]


def test_new_install_offers_actions_instead_of_adding_them(tmp_path, store):
    _first_launch(store, NOW - 3600)
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    assert rt.auto_actions_held(store, NOW)
    hub = FakeHub()
    tick(rt, store, hub)
    ev = _nudge_event(hub)
    # The nudge still shows; nothing was written, and every action is an offer.
    assert ev["auto_performed"] == [] and ev["can_undo"] is False
    assert [a["auto"] for a in ev["actions"]] == [False, False, False]
    assert _commitments(store) == 0
    assert not any(e["event"] == {"type": "changed"} for e in hub.events)
    # Tapping an offer still adds it on Wisp's own schedule.
    added = rt.perform_action(store, "n1", 0, gym_conflict_nudge().actions[0], NOW)
    assert added is not None and added["title"] == "Pack gym bag" and _commitments(store) == 1


@pytest.mark.parametrize("verdict", ["useful", "undo"])
def test_useful_or_undo_ends_the_trial(tmp_path, store, verdict):
    _first_launch(store, NOW - 3600)
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    tick(rt, store, FakeHub())
    assert rt.auto_actions_held(store, NOW)
    rt.feedback(store, "n1", verdict)
    assert not rt.auto_actions_held(store, NOW)
    # A later answer on the same nudge does not take the opt-in back.
    rt.feedback(store, "n1", "not_now")
    assert not rt.auto_actions_held(store, NOW)


def test_trial_ends_three_days_after_first_launch(tmp_path, store):
    _first_launch(store, NOW - br.AUTO_ACTION_TRIAL_S - 1)
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    assert not rt.auto_actions_held(store, NOW)
    assert rt.auto_actions_held(store, NOW - 2)  # 1 s before the trial ended
    hub = FakeHub()
    tick(rt, store, hub)
    ev = _nudge_event(hub)
    assert [a["type"] for a in ev["auto_performed"]] == ["add_reminder", "add_hold"]
    assert _commitments(store) == 2


def test_upgraded_install_is_never_held(tmp_path, store):
    _first_launch(store, "0")  # what the store records for a database that already had data
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    assert store.first_launch_at() is None
    assert not rt.auto_actions_held(store, NOW)


def test_fresh_store_records_first_launch_once(tmp_path):
    from service.assistant.store import AssistantStore
    first = AssistantStore(tmp_path / "fresh.db")
    stamp = first.first_launch_at()
    assert stamp is not None
    first._db.close()
    again = AssistantStore(tmp_path / "fresh.db")
    assert again.first_launch_at() == stamp
    again._db.close()


def test_routine_items_carry_no_internal_id_in_their_context(tmp_path, store, monkeypatch):
    monkeypatch.setattr(br.BehaviorRuntime, "auto_actions_held", lambda self, store, now: False)
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    tick(rt, store, FakeHub())
    contexts = [r[0] for r in store._db.execute("SELECT context FROM commitments")]
    assert contexts and all(c.startswith(br.BEHAVIOR_CONTEXT) for c in contexts)
    assert not any("behavior_" in c or "[" in c for c in contexts)
    receipts = [r[0] for r in store._db.execute("SELECT context FROM assistant_behavior_action_receipts")]
    assert sorted(receipts) == sorted(contexts)


def test_receipt_written_with_the_old_id_suffix_is_the_same_action(tmp_path, store, monkeypatch):
    """Receipts from before this change end in " [behavior_<hex>]". A retry that
    builds the context without it must still match, not fail as a changed action."""
    import json
    from service.behavior.types import Nudge
    from service.publication_authority import PublicationUnavailable
    monkeypatch.setattr(br.BehaviorRuntime, "auto_actions_held", lambda self, store, now: False)
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[gym_conflict_nudge()]))
    tick(rt, store, FakeHub())
    receipts = [dict(r) for r in store._db.execute(
        "SELECT * FROM assistant_behavior_action_receipts ORDER BY action_index")]
    assert len(receipts) == 2
    with store._db:
        for r in receipts:
            legacy = r["context"] + " [" + r["commitment_id"] + "]"
            store._db.execute("UPDATE assistant_behavior_action_receipts SET context=? WHERE commitment_id=?",
                              (legacy, r["commitment_id"]))
            store._db.execute("UPDATE commitments SET context=? WHERE id=?", (legacy, r["commitment_id"]))
    payload = json.dumps(gym_conflict_nudge().to_dict(), sort_keys=True, allow_nan=False)
    canonical = Nudge.from_dict(json.loads(payload))
    actions = [dict(idx=r["action_index"], type=r["type"], id=r["commitment_id"], title=r["title"],
                    when_ts=r["when_ts"], kind=r["kind"], context=r["context"], end_ts=r["end_ts"])
               for r in receipts]
    prior = [dict(r) for r in rt._db.execute("SELECT * FROM actions WHERE nudge_id='n1' ORDER BY idx")]
    plan = store.prepare_nudge_write(rt, payload, canonical.to_event(), actions, prior, NOW, canonical.expires_at)
    assert [row["idx"] for row in json.loads(plan.data)["performed"]] == [0, 1]
    # Any other difference is still a changed action.
    changed = [{**actions[0], "context": actions[0]["context"] + " extra"}, actions[1]]
    with pytest.raises(PublicationUnavailable):
        store.prepare_nudge_write(rt, payload, canonical.to_event(), changed, prior, NOW, canonical.expires_at)


def _soon_nudge(due_ts: float):
    from service.behavior.types import Action, Nudge
    return Nudge(id="n-soon", kind="habit_conflict", title="Gym vs. 9:05 call",
                 body="Your call lands on gym time.", because="14 of the last 16 weekday mornings",
                 created_at=NOW, expires_at=NOW + 3600, priority=0.8, pattern_ids=("p_gym",),
                 actions=(Action("add_reminder", {"title": "Review calendar overlap", "when_ts": due_ts},
                                 auto=True, label="Remind me"),))


def test_tapped_reminder_due_eight_minutes_later_alerts(tmp_path, store):
    """Regression (audit B1): during the trial every routine action is a tap. A
    reminder the user taps to add must alert even when it is due minutes later."""
    from service.assistant.reminders import due_reminders
    _first_launch(store, NOW - 3600)
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[_soon_nudge(NOW + 480)]))
    tick(rt, store, FakeHub())
    assert _commitments(store) == 0                      # offered, not added
    added = rt.perform_action(store, "n-soon", 0, _soon_nudge(NOW + 480).actions[0], NOW)
    assert added is not None
    [note] = due_reminders(store, NOW + 481)
    assert note["title"] == "Review calendar overlap" and note["stage"] == "due"


def test_automatic_reminder_due_at_its_nudge_does_not_repeat_it(tmp_path, store, monkeypatch):
    from service.assistant.reminders import due_reminders
    monkeypatch.setattr(br.BehaviorRuntime, "auto_actions_held", lambda self, store, now: False)
    rt, _ = make_runtime(tmp_path, FakeEngine(nudges=[_soon_nudge(NOW + 300)]))
    hub = FakeHub()
    tick(rt, store, hub)
    assert [a["type"] for a in _nudge_event(hub)["auto_performed"]] == ["add_reminder"]
    assert due_reminders(store, NOW + 301) == []
