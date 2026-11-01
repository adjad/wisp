"""Remove on a Day suggestion: the block leaves the plan and is never suggested
again, by Replan or any later plan. Only Wisp's own record changes; the email or
reminder it came from stays in Needs you.

From the user's Day on 2026-10-30: "I do not need some of these calender events
anymore". Synthetic stores and evidence only; no native sources, no model.
"""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import os
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="wisp-day-remove-tests-")
os.environ["WISP_HOME"] = _scratch.name
os.environ["HOME"] = _scratch.name

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from service.assistant import daily_workspace as daily  # noqa: E402
from service.assistant.daily_workspace import assess, key  # noqa: E402
from service.assistant.store import AssistantStore  # noqa: E402

DAY, ZONE = "2026-10-30", "America/Los_Angeles"


def at(hour, minute=0):
    return datetime(2026, 10, 30, hour, minute, tzinfo=ZoneInfo(ZONE)).timestamp()


NOW = at(13, 55)


def state():
    return dict(tasks=[], commitments=[], revision=0,
                preferences=dict(start_minute=540, end_minute=1200, not_before=None),
                sources={s: dict(available=True, last_sync=NOW, snapshot_started_at=NOW,
                                 diagnostics=dict(coverage_start=at(0), coverage_end=at(23, 59) + 60))
                         for s in ("calendar", "reminders")})


def email(identity, title, sender):
    return dict(id=identity, title=title, quote=title, sender=sender, person=True,
                review_reason="Review this source for requests or changes.")


def reminder(identity, title, due):
    return dict(id=identity, title=title, due_ts=due)


EVIDENCE = dict(
    reminders=dict(ready=True, detail="Synthetic", records=[reminder("r1", "Review all of this before your midterm", at(23)),
                                                            reminder("r2", "Review reminder for STAT 5 Exam 1", at(23))]),
    email=dict(ready=True, detail="Synthetic", records=[email("a", "Review the topics for the midterm", "Course Staff"),
                                                        email("b", "Lab partner sign-up", "Maya Chen")]))
MIDTERM, STAT = key("reminders", "r1"), key("reminders", "r2")
TOPICS, LAB = key("email", "a"), key("email", "b")


def suggestions(result):
    return [b for b in result["proposed_plan"]["blocks"] if b["kind"] == "suggestion"]


def suggested_ids(result):
    ids = set()
    for block in suggestions(result):
        ids |= {m["evidence_id"] for m in block.get("members") or []} or {block["evidence_id"]}
    return ids


def run(removed=None):
    return assess(DAY, ZONE, state(), EVIDENCE, now=NOW, removed=removed)


def test_everything_is_suggested_until_removed():
    assert suggested_ids(run()) == {MIDTERM, STAT, TOPICS, LAB}


def test_a_removed_suggestion_leaves_the_plan_but_stays_in_needs_you():
    result = run({MIDTERM})
    assert MIDTERM not in suggested_ids(result)
    assert suggested_ids(result) == {STAT, TOPICS, LAB}
    assert MIDTERM in {i["id"] for i in result["attention"]}, "only the suggestion goes; the reminder stays"


def test_removing_a_grouped_review_removes_each_email_in_it():
    before = [b for b in suggestions(run()) if b.get("members")]
    assert len(before) == 1 and {m["evidence_id"] for m in before[0]["members"]} == {TOPICS, LAB}
    after = run({TOPICS, LAB})
    assert not any(b.get("members") for b in suggestions(after))
    assert suggested_ids(after) == {MIDTERM, STAT}


def test_the_store_keeps_removals_across_restarts(tmp_path):
    path = tmp_path / "assistant.db"
    AssistantStore(path).remove_suggestions([MIDTERM, MIDTERM, TOPICS])
    assert AssistantStore(path).removed_suggestions() == {MIDTERM, TOPICS}


def test_the_store_keeps_only_the_newest_removals(tmp_path, monkeypatch):
    store = AssistantStore(tmp_path / "assistant.db")
    monkeypatch.setattr(AssistantStore, "REMOVED_SUGGESTIONS_KEPT", 2)
    clock = iter(range(1000, 1010))
    monkeypatch.setattr("service.assistant.store.time.time", lambda: next(clock))
    for ident in ("a", "b", "c"):
        store.remove_suggestions([key("email", ident)])
    assert store.removed_suggestions() == {key("email", "b"), key("email", "c")}


@pytest.fixture
def day(tmp_path, monkeypatch):
    """The real Today router over a temporary store, synthetic evidence and a fixed clock."""
    import importlib
    from service.assistant import daily_workspace_api as api
    from service.assistant import today_api
    store = AssistantStore(tmp_path / "assistant.db")
    monkeypatch.setattr(store, "today_snapshot", lambda *args: state(), raising=False)
    saved = []
    monkeypatch.setattr(store, "today_save_preferences", lambda *args: saved.append(args), raising=False)
    monkeypatch.setattr(store, "hide_stale_tasks", lambda tasks, now: tasks, raising=False)
    monkeypatch.setattr(importlib.import_module("service.assistant.store"), "assistant_store", store)
    monkeypatch.setattr(today_api, "assistant_store", store)
    monkeypatch.setattr(daily, "collect_evidence", lambda *args, **kwargs: EVIDENCE)
    monkeypatch.setattr(api, "email_store", lambda: SimpleNamespace(ignored=lambda *args: []))
    monkeypatch.setattr(api, "availability", lambda *args: None)
    monkeypatch.setattr(api, "user_profile", lambda: None)
    monkeypatch.setattr(today_api, "live_access", lambda sources, now: sources)
    monkeypatch.setattr(today_api.time, "time", lambda: NOW)
    app = FastAPI()
    app.include_router(today_api.router)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        yield SimpleNamespace(client=client, store=store, saved=saved)


def plan_ids(payload):
    return suggested_ids(payload["workspace"])


def test_a_removed_suggestion_stays_removed_after_replan(day):
    shown = day.client.get("/assistant/today", params=dict(day=DAY, timezone=ZONE))
    assert shown.status_code == 200 and MIDTERM in plan_ids(shown.json())

    removed = day.client.post("/assistant/today/workspace/suggestions/remove", json=dict(evidence_ids=[MIDTERM]))
    assert removed.status_code == 200 and removed.json() == {"ok": True, "removed": 1}
    assert day.store.removed_suggestions() == {MIDTERM}

    refreshed = day.client.get("/assistant/today", params=dict(day=DAY, timezone=ZONE)).json()
    assert MIDTERM not in plan_ids(refreshed) and STAT in plan_ids(refreshed)

    replanned = day.client.post("/assistant/today/replan", json=dict(
        day=DAY, timezone=ZONE, revision=0, start_minute=540, end_minute=1200, not_before=None))
    assert replanned.status_code == 200, replanned.text
    assert day.saved, "Replan really ran"
    assert MIDTERM not in plan_ids(replanned.json()), "Replan never brings a removed suggestion back"
    assert MIDTERM in {i["id"] for i in replanned.json()["workspace"]["attention"]}


@pytest.mark.parametrize("body", [
    dict(evidence_ids=[]),
    dict(evidence_ids=["not an id"]),
    dict(evidence_ids=[MIDTERM], extra=True),
    dict(evidence_ids=[key("email", str(n)) for n in range(21)]),
])
def test_malformed_removals_are_refused_and_change_nothing(day, body):
    response = day.client.post("/assistant/today/workspace/suggestions/remove", json=body)
    assert response.status_code == 422
    assert day.store.removed_suggestions() == set()


def test_a_page_cannot_remove_suggestions(day):
    response = day.client.post("/assistant/today/workspace/suggestions/remove", json=dict(evidence_ids=[MIDTERM]),
                               headers={"Origin": "https://example.com"})
    assert response.status_code == 403
    assert day.store.removed_suggestions() == set()


# Undo and "Bring back removed suggestions" (Release Auditor note on rc1 batch 1:
# Remove had no way back, and a grouped Review block hid every email at once).

def test_the_plan_lists_todays_removed_suggestions():
    assert run()["removed_suggestions"] == []
    assert sorted(run({TOPICS, LAB})["removed_suggestions"]) == sorted([TOPICS, LAB])
    assert run({key("email", "long-gone")})["removed_suggestions"] == [], "only what today would suggest is listed"


def test_removed_suggestions_are_listed_past_the_suggestion_cap(monkeypatch):
    # The first emails fill a two-suggestion cap; the last four were removed.
    many = dict(EVIDENCE, email=dict(ready=True, detail="Synthetic", records=[
        email(str(n), f"Question {n} about the project", f"Sender {n}") for n in range(12)]))
    order = [i["id"] for i in assess(DAY, ZONE, state(), many, now=NOW)["attention"]]
    late = set(order[-4:])
    monkeypatch.setattr(daily, "MAX_SUGGESTIONS", 2)
    result = assess(DAY, ZONE, state(), many, now=NOW, removed=late)
    assert len(suggestions(result)) >= 1
    assert set(result["removed_suggestions"]) == late, "Bring back sees every removal, not just those before the cap"


def test_the_store_restores_listed_removals_only(tmp_path):
    store = AssistantStore(tmp_path / "assistant.db")
    store.remove_suggestions([MIDTERM, TOPICS, LAB])
    assert store.restore_suggestions([TOPICS, LAB, TOPICS]) == 2
    assert store.removed_suggestions() == {MIDTERM}
    assert store.restore_suggestions([STAT]) == 0 and store.removed_suggestions() == {MIDTERM}


def test_undo_brings_a_grouped_review_back_in_one_call(day):
    path = "/assistant/today/workspace/suggestions/"
    assert day.client.post(path + "remove", json=dict(evidence_ids=[TOPICS, LAB])).status_code == 200
    hidden = day.client.get("/assistant/today", params=dict(day=DAY, timezone=ZONE)).json()
    assert not {TOPICS, LAB} & plan_ids(hidden)
    assert sorted(hidden["workspace"]["removed_suggestions"]) == sorted([TOPICS, LAB])

    undone = day.client.post(path + "restore", json=dict(evidence_ids=[TOPICS, LAB]))
    assert undone.status_code == 200 and undone.json() == {"ok": True, "restored": 2}
    back = day.client.get("/assistant/today", params=dict(day=DAY, timezone=ZONE)).json()
    grouped = [b for b in suggestions(back["workspace"]) if b.get("members")]
    assert len(grouped) == 1 and {m["evidence_id"] for m in grouped[0]["members"]} == {TOPICS, LAB}
    assert back["workspace"]["removed_suggestions"] == []


def test_bring_back_restores_only_todays_removals(day):
    path = "/assistant/today/workspace/suggestions/"
    other_day = key("email", "from-last-week")
    day.client.post(path + "remove", json=dict(evidence_ids=[MIDTERM]))
    day.client.post(path + "remove", json=dict(evidence_ids=[TOPICS, LAB, other_day]))
    listed = day.client.get("/assistant/today", params=dict(day=DAY, timezone=ZONE)).json()["workspace"]["removed_suggestions"]
    assert set(listed) == {MIDTERM, TOPICS, LAB}

    restored = day.client.post(path + "restore", json=dict(evidence_ids=listed))
    assert restored.status_code == 200 and restored.json() == {"ok": True, "restored": 3}
    assert day.store.removed_suggestions() == {other_day}, "another day's removal stays removed"
    replanned = day.client.post("/assistant/today/replan", json=dict(
        day=DAY, timezone=ZONE, revision=0, start_minute=540, end_minute=1200, not_before=None))
    assert plan_ids(replanned.json()) == {MIDTERM, STAT, TOPICS, LAB}


@pytest.mark.parametrize("body", [
    dict(),
    dict(all=True),
    dict(evidence_ids=[]),
    dict(evidence_ids=["not an id"]),
    dict(evidence_ids=[MIDTERM], extra=True),
    dict(evidence_ids=[key("email", str(n)) for n in range(daily.MAX_ITEMS + 1)]),
])
def test_malformed_restores_are_refused_and_change_nothing(day, body):
    day.store.remove_suggestions([MIDTERM])
    response = day.client.post("/assistant/today/workspace/suggestions/restore", json=body)
    assert response.status_code == 422
    assert day.store.removed_suggestions() == {MIDTERM}


def test_a_page_cannot_restore_suggestions(day):
    day.store.remove_suggestions([MIDTERM])
    response = day.client.post("/assistant/today/workspace/suggestions/restore", json=dict(evidence_ids=[MIDTERM]),
                               headers={"Origin": "https://example.com"})
    assert response.status_code == 403
    assert day.store.removed_suggestions() == {MIDTERM}
