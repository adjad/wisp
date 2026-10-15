"""Synthetic personal histories and opt-in/currentness controls. No live reads."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from service.behavior.engine import BehaviorEngine
from service.behavior.availability import discover, competing_activity
from service.behavior.types import Observation

NOW = datetime(2026, 10, 19, 8, tzinfo=ZoneInfo("UTC")).timestamp()


def history(app="org.arbitrary.Alpha", weeks=5, timezone="UTC", minute=600):
    day = datetime(2026, 10, 12, tzinfo=ZoneInfo(timezone))
    return [
        Observation(
            f"b:{i}:{m}",
            "activity_bin",
            (day - timedelta(weeks=i) + timedelta(minutes=m)).timestamp(),
            end_ts=(day - timedelta(weeks=i) + timedelta(minutes=m + 15)).timestamp(),
            source="activity",
            attrs=dict(timezone=timezone, covered=900, idle=0, apps={app: 900}),
        )
        for i in range(weeks)
        for m in (minute, minute + 15)
    ]


def test_label_free_recurrence_and_renaming():
    a = discover(history(), [], "2026-10-19", "UTC", NOW)
    b = discover(
        list(reversed(history("net.unseen.Omega"))), [], "2026-10-19", "UTC", NOW
    )
    assert len(a) == len(b) == 1
    assert [(p["support"], p["opportunities"], p["start"], p["end"]) for p in a] == [
        (p["support"], p["opportunities"], p["start"], p["end"]) for p in b
    ]
    assert "availability unknown" in a[0]["interpretation"]
    assert a[0]["end"] - a[0]["start"] == 1800


def test_missing_coverage_is_unknown_and_minimum_distinct_weeks():
    assert not discover(history(weeks=2), [], "2026-10-19", "UTC", NOW)
    rows = history()
    rows = [
        Observation(
            o.id, o.kind, o.ts, end_ts=o.end_ts, attrs={**o.attrs, "covered": 100}
        )
        for o in rows
    ]
    assert not discover(rows, [], "2026-10-19", "UTC", NOW)


def test_adjacent_windows_require_shared_support():
    rows = history(weeks=8)
    # Each bin occurs on three dates; no date supports both.
    rows = [
        o
        for o in rows
        if (int(o.id.split(":")[1]) < 3 and o.id.endswith(":600"))
        or (3 <= int(o.id.split(":")[1]) < 6 and o.id.endswith(":615"))
    ]
    result = discover(rows, [], "2026-10-19", "UTC", NOW)
    assert all(p["end"] - p["start"] == 900 for p in result)


def changing_history(exceptions):
    """All dates are covered; an alternate state is actual counterevidence."""
    from dataclasses import replace

    return [
        (
            replace(o, attrs={**o.attrs, "apps": {"org.other.State": 900}})
            if (int(o.id.split(":")[1]), int(o.id.split(":")[2])) in exceptions
            else o
        )
        for o in history()
    ]


def test_group_cannot_borrow_recency_from_disjoint_latest_dates():
    # Left recurs on A/B/C/D, right on A/B/C/E. Both independently have two
    # recent hits, but the full window has only C among its latest three dates.
    rows = changing_history({(0, 600), (1, 615)})
    patterns = discover(rows, [], "2026-10-19", "UTC", NOW)
    assert len(patterns) == 2
    assert all(p["end"] - p["start"] == 900 for p in patterns)
    assert all(p["support"] == 4 and p["opportunities"] == 5 for p in patterns)


def test_shared_recent_group_uses_only_shared_dates_for_freshness_and_evidence():
    # Both cover all five dates; the merged pattern occurs together on A/D/E.
    rows = changing_history({(2, 600), (3, 615)})
    patterns = discover(rows, [], "2026-10-19", "UTC", NOW)
    assert len(patterns) == 1
    pattern = patterns[0]
    assert pattern["end"] - pattern["start"] == 1800
    assert pattern["support"] == 3 and pattern["opportunities"] == 5
    assert pattern["distinct_weeks"] == 3
    shared = {o.id: o for o in rows if int(o.id.split(":")[1]) in {0, 1, 4}}
    assert set(pattern["evidence"]) <= shared.keys()
    assert pattern["observed_at"] == max(o.end_ts for o in shared.values())


def test_individual_newest_hit_cannot_freshen_a_merged_cohort():
    # Left has an extra newest hit, while the latest shared hit is a week older.
    rows = changing_history({(0, 615)})
    pattern = discover(rows, [], "2026-10-19", "UTC", NOW)[0]
    assert pattern["end"] - pattern["start"] == 1800
    shared = [o for o in rows if int(o.id.split(":")[1]) >= 1]
    assert pattern["observed_at"] == max(o.end_ts for o in shared)
    assert not any(e.startswith("b:0:") for e in pattern["evidence"])


def test_partial_event_bins_do_not_invent_uniform_coverage():
    bins = history(weeks=3)
    bins = [
        Observation(
            b.id, b.kind, b.ts, end_ts=b.end_ts, attrs={**b.attrs, "covered": 720}
        )
        for b in bins
    ]
    events = [
        Observation(
            "e" + b.id, "event", b.ts, end_ts=b.ts + 300, subject="Arbitrary event"
        )
        for b in bins
    ]
    assert competing_activity(events, bins, "UTC") == []


@pytest.fixture
def engine(tmp_path):
    e = BehaviorEngine(tmp_path / "behavior.db", tz="UTC", clock=lambda: NOW)
    yield e
    e.close()


def row(start, end, seq=1, **kw):
    return dict(
        start=start,
        end=end,
        sequence=seq,
        bundle="org.unseen.App",
        idle_seconds=0,
        screen_awake=True,
        session_active=True,
        timezone="UTC",
        **kw,
    )


def test_capture_default_off_consent_lease_renewal_and_idempotency(engine):
    assert engine.activity_lease("collector-session-1234", NOW)["enabled"] is False
    engine.configure_signals({"activity": True}, NOW - 60)
    lease = engine.activity_lease("collector-session-1234", NOW - 60)
    payload = dict(lease=lease, intervals=[row(NOW - 45, NOW - 30)])
    engine.activity_lease("collector-session-1234", NOW - 20)
    assert engine.observe_activity(payload, NOW)["accepted"] == 1
    assert engine.observe_activity(payload, NOW)["duplicate"]
    engine.configure_signals({"email": True}, NOW)
    lease = engine.activity_lease("collector-session-1234", NOW)
    with pytest.raises(ValueError):
        engine.observe_activity(
            dict(lease=lease, intervals=[row(NOW - 40, NOW - 25, 2)]), NOW
        )


def test_off_and_restart_reject_old_batches(engine, tmp_path):
    engine.configure_signals({"activity": True}, NOW - 60)
    lease = engine.activity_lease("collector-session-1234", NOW - 60)
    payload = dict(lease=lease, intervals=[row(NOW - 45, NOW - 30)])
    engine.configure_signals({"activity": False}, NOW)
    with pytest.raises(ValueError):
        engine.observe_activity(payload, NOW)
    assert engine._store.observations(kinds=["device_activity", "activity_bin"]) == []


@pytest.mark.parametrize("value", [0, 1, "true", [], {}])
def test_device_booleans_are_strict(engine, value):
    engine.configure_signals({"activity": True}, NOW - 60)
    lease = engine.activity_lease("collector-session-1234", NOW - 60)
    r = row(NOW - 45, NOW - 30)
    r["screen_awake"] = value
    with pytest.raises(ValueError):
        engine.observe_activity(dict(lease=lease, intervals=[r]), NOW)


def test_unknown_device_state_never_counts_as_foreground_engagement(engine):
    engine.configure_signals({"activity": True}, NOW - 60)
    lease = engine.activity_lease("collector-session-1234", NOW - 60)
    r = row(NOW - 45, NOW - 30)
    r["screen_awake"] = None
    engine.observe_activity(dict(lease=lease, intervals=[r]), NOW)
    assert engine._store.observations(kinds=["activity_bin"])[0].attrs["apps"] == {}


def test_capture_cannot_precede_consent_or_collector_start(engine):
    engine.configure_signals({"activity": True}, NOW - 60)
    lease = engine.activity_lease("collector-session-1234", NOW - 15)
    with pytest.raises(ValueError):
        engine.observe_activity(
            dict(lease=lease, intervals=[row(NOW - 30, NOW - 20)]), NOW
        )


def test_source_wrappers_fail_closed_and_are_immutable(tmp_path, monkeypatch):
    from service.tools import (
        imessage_tools as messages,
        browser_history_tools as browser,
    )

    for name in (
        "_lines",
        "_available",
        "_unavailable_reason",
        "_sync_completed",
        "_snapshot_generation",
        "_snapshot_captured_at",
    ):
        monkeypatch.setattr(messages, name, getattr(messages, name))
    monkeypatch.setattr(messages.cache_store, "save", lambda *a: None)
    monkeypatch.setattr(messages.time, "time", lambda: NOW)
    monkeypatch.setattr(
        messages,
        "structured_messages_snapshot",
        lambda: dict(
            state="ready",
            coverage={"status": "complete"},
            records=[{"text": "synthetic"}],
        ),
    )
    messages.cache_messages("fixture", True)
    snapshot = messages.structured_messages_snapshot_with_receipt()
    assert snapshot is not None
    with pytest.raises(TypeError):
        snapshot["records"][0]["text"] = "changed"
    old = snapshot["generation"]
    messages.cache_messages("", False)
    assert (
        messages.structured_messages_snapshot_with_receipt(expected_generation=old)
        is None
    )
    monkeypatch.setattr(messages.time, "time", lambda: NOW + 601)
    assert messages.structured_messages_snapshot_with_receipt() is None
    monkeypatch.setattr(browser.time, "time", lambda: NOW)
    monkeypatch.setattr(browser, "_enabled", True)
    monkeypatch.setattr(browser, "_privacy_current", True)
    monkeypatch.setattr(browser, "_revision", 4)
    monkeypatch.setattr(browser, "_applied_revision", 4)
    monkeypatch.setattr(
        browser,
        "_raw",
        {"safari": f"{NOW-60} | arbitrary.example | /?secret | title", "chrome": ""},
    )
    monkeypatch.setattr(browser, "_available", {"safari": True, "chrome": False})
    monkeypatch.setattr(browser, "_synced_at", {"safari": NOW, "chrome": 0})
    b = browser.current_browser_snapshot()
    assert dict(b["rows"][0]) == {"ts": NOW - 60, "host": "arbitrary.example"}
    monkeypatch.setattr(browser, "_enabled", False)
    assert browser.current_browser_snapshot(expected_receipt=b["receipt"]) is None


def test_source_adapter_uses_public_receipts_and_no_source_when_off():
    from service.assistant.behavior_sources import capture

    calls = []

    def prohibited():
        calls.append(1)
        raise AssertionError("disabled source read")

    batch = capture({}, {s: prohibited for s in ("email", "messages", "websites")})
    assert not calls and not batch.records and not batch.visits


def test_device_retention_applies_without_new_capture(engine):
    engine.observe(
        [
            Observation(
                "old-raw", "device_activity", NOW - 8 * 86400, source="activity"
            ),
            Observation("old-bin", "activity_bin", NOW - 57 * 86400, source="activity"),
        ]
    )
    engine._store.prune(NOW)
    assert not engine._store.observations()


def test_activity_api_is_opt_in_and_reads_do_not_trigger_interpretation(
    tmp_path, monkeypatch
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service import behavior_api
    from service.assistant.behavior_runtime import BehaviorRuntime

    monkeypatch.setenv("WISP_BEHAVIOR", "1")

    def forbidden(*a, **kw):
        raise AssertionError("read endpoint triggered a source/model")

    runtime = BehaviorRuntime(
        tmp_path,
        local_factory=lambda p: BehaviorEngine(p, tz="UTC", clock=lambda: NOW),
        behavior_sources_fn=forbidden,
        role_key_fn=forbidden,
        clock=lambda: NOW,
    )
    monkeypatch.setattr(behavior_api.behavior_runtime, "get_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(behavior_api.router)
    try:
        with TestClient(app) as client:
            settings = client.get("/behavior/signals").json()
            assert all(
                settings["settings"][key] is False
                for key in (
                    "activity",
                    "email",
                    "messages",
                    "websites",
                    "interpretation",
                    "remote_analysis",
                )
            )
            assert client.get(
                "/behavior/activity/lease", params={"client": "collector-session-1234"}
            ).json() == {"enabled": False}
            assert (
                client.put("/behavior/signals", json={"activity": "yes"}).status_code
                == 422
            )
            assert (
                client.get(
                    "/behavior/availability",
                    params={"day": "2026-10-19", "timezone": "UTC"},
                ).json()["ready"]
                is False
            )
            assert (
                client.get(
                    "/behavior/availability",
                    params={"day": "invalid", "timezone": "UTC"},
                ).status_code
                == 422
            )
    finally:
        runtime.close()
