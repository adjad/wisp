"""Synthetic personal histories and opt-in/currentness controls. No live reads."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from service.behavior.engine import BehaviorEngine
from service.behavior.availability import discover, competing_activity, apply_interpretations
from service.behavior.types import Observation
from tests.test_message_content import live_message_publication, record, v3_carrier

NOW = datetime(2026, 10, 19, 8, tzinfo=ZoneInfo("UTC")).timestamp()


@pytest.fixture
def routine_runtime(tmp_path, monkeypatch):
    """Real private runtime/interpreter; only source publications/model are fake."""
    import json
    from copy import deepcopy
    from dataclasses import replace
    from service.assistant.behavior_runtime import BehaviorRuntime
    from service.assistant.behavior_interpreter import Interpreter
    from service.assistant.behavior_sources import Batch
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    runtimes = []
    def make(kind="away", *, sites=False):
        rows = history()
        if kind == "away":
            rows = [replace(o, attrs={**o.attrs, "apps": {}, "idle": 900, "inactive": 900, "screen_sleep": 900}) for o in rows]
        pattern = discover(rows, [], "2026-10-19", "UTC", NOW)[0]
        controls = dict(live=True, generation=1, role="fake", visits=([dict(host="arbitrary.test", ts=rows[0].ts)] if sites else []),
                        calls=[], reads=[], changes={}, response="normal")
        def sources(_):
            controls["reads"].append("cached-publication")
            token = controls["generation"]
            return Batch([], deepcopy(controls["visits"]), [lambda: controls["live"] and token == controls["generation"]], {})
        def current_patterns(_day, _zone, visits=(), now=None):
            value = deepcopy(pattern)
            value["website_visit_context"] = sorted({v["host"] for v in visits})
            value.update(deepcopy(controls["changes"]))
            return [value]
        def complete(_system, prompt, **_):
            controls["calls"].append(json.loads(prompt))
            if controls["response"] == "failed":
                raise RuntimeError("Synthetic unavailable analysis")
            if controls["response"] == "empty":
                return json.dumps(dict(demands=[], patterns=[]))
            if controls["response"] == "malformed":
                return "{not-json"
            patterns = json.loads(prompt)["recurring_patterns"]
            # Runtime projects seven dates. The fixture returns the same id;
            # the real interpreter validates references to the supplied data.
            return json.dumps(dict(demands=[], patterns=[dict(pattern_id=p["id"],
                availability="likely_occupied" if controls["response"] == "occupied_absence" else "unknown" if kind == "away" else "likely_occupied",
                evidence=["unadmitted-reference"] if controls["response"] == "wrong_reference" else p["evidence"],
                reason="Availability remains uncertain.") for p in patterns[:1]]))
        runtime = BehaviorRuntime(tmp_path/str(len(runtimes)),
            local_factory=lambda p: BehaviorEngine(p, tz="UTC", clock=lambda: NOW),
            behavior_sources_fn=sources, behavior_interpreter=Interpreter(complete),
            role_key_fn=lambda: controls["role"], clock=lambda: NOW)
        runtime.call("configure_signals", {"activity": True, "interpretation": True, "websites": sites}, NOW)
        monkeypatch.setattr(runtime.local_engine(), "availability_patterns", current_patterns)
        runtimes.append(runtime)
        return runtime, controls, pattern
    yield make
    for runtime in runtimes:
        runtime.close()


def test_recurring_absence_has_distinct_tentative_planning_kind(routine_runtime):
    runtime, controls, pattern = routine_runtime()
    runtime._refresh_availability(NOW)
    result = runtime.planning_context("2026-10-19", "UTC", NOW)
    assert len(result["windows"]) == 1
    hint = result["windows"][0]
    assert hint["kind"] == "possible_unavailability" and hint["title"] == "Possible time away"
    assert hint["prediction_target"] == "computer_activity" and hint["availability"] == "unknown"
    assert hint["action_authority"] == "none" and hint["basis"] == "inferred"
    assert hint["evidence"] == pattern["evidence"] and hint["confidence"] == pattern["confidence"]
    assert "does not establish" in hint["uncertainty"]
    assert runtime.planning_context("2026-10-19", "UTC", NOW+20)["generated_at"] == NOW
    assert len(controls["calls"]) == 1


@pytest.mark.parametrize("field,value", [
    ("descriptors", {"app:other": 1}), ("evidence", ["new-support"]), ("coverage", "unknown"),
    ("state_id", "changed-anchor"), ("support", 4), ("opportunities", 6),
    ("observed_at", NOW-20), ("confidence", .65), ("start", NOW+123), ("end", NOW+999),
])
def test_same_pattern_id_cannot_reuse_analysis_for_changed_evidence(routine_runtime, field, value):
    runtime, controls, _ = routine_runtime("active")
    runtime._refresh_availability(NOW)
    assert len(runtime.planning_context("2026-10-19", "UTC", NOW)["windows"]) == 1
    controls["changes"][field] = value
    assert runtime.planning_context("2026-10-19", "UTC", NOW)["windows"] == []
    assert len(controls["calls"]) == 1


def test_forecast_cannot_escape_analyzed_seven_day_horizon(routine_runtime):
    runtime, controls, _ = routine_runtime("active")
    runtime._refresh_availability(NOW)
    assert runtime.planning_context("2026-10-18", "UTC", NOW)["windows"] == []
    assert runtime.planning_context("2026-10-26", "UTC", NOW)["windows"] == []
    assert len(controls["calls"]) == 1


@pytest.mark.parametrize("kind", ["active", "away"])
def test_both_window_kinds_keep_unchanged_cached_website_context(routine_runtime, kind):
    runtime, controls, _ = routine_runtime(kind, sites=True)
    runtime._refresh_availability(NOW)
    result = runtime.planning_context("2026-10-19", "UTC", NOW)
    assert len(result["windows"]) == 1
    assert result["windows"][0]["website_visit_context"] == ["arbitrary.test"]
    assert len(controls["reads"]) == 2 and len(controls["calls"]) == 1
    controls["visits"] = []
    assert runtime.planning_context("2026-10-19", "UTC", NOW)["windows"] == []
    assert len(controls["reads"]) == 3 and len(controls["calls"]) == 1


@pytest.mark.parametrize("kind", ["active", "away"])
@pytest.mark.parametrize("fault", ["stale", "future", "role", "source", "superseded", "consent", "cancel", "missing_proof", "timezone"])
def test_cached_routine_admission_fails_before_any_new_capture(routine_runtime, kind, fault):
    runtime, controls, _ = routine_runtime(kind)
    runtime._refresh_availability(NOW)
    stamp, zone = NOW, "UTC"
    if fault == "stale": stamp += 301
    elif fault == "future": stamp -= 1
    elif fault == "role": controls["role"] = "changed"
    elif fault == "source": controls["live"] = False
    elif fault == "superseded": controls["generation"] += 1
    elif fault == "consent": runtime.call("configure_signals", {"activity": False}, NOW)
    elif fault == "cancel": runtime._availability_cache["cancellation"].set()
    elif fault == "timezone": zone = "America/Los_Angeles"
    else: runtime._availability_cache.pop("pattern_proofs")
    result = runtime.planning_context("2026-10-19", zone, stamp)
    assert result["windows"] == [] and len(controls["calls"]) == 1
    assert len(controls["reads"]) == 1


@pytest.mark.parametrize("fault", ["role", "consent", "cancel", "source", "invalid_batch", "exception"])
def test_mid_capture_changes_withhold_windows_and_drop_transient_bodies(routine_runtime, fault):
    from service.assistant.behavior_sources import Batch
    runtime, controls, _ = routine_runtime()
    runtime._refresh_availability(NOW)
    captured = []
    def changed_sources(_):
        controls["reads"].append("cached-publication")
        batch = Batch([{"text": "Synthetic private body never retained"}], [{"host": "arbitrary.test"}],
                      [lambda: controls["live"]], {})
        captured.append(batch)
        if fault == "role": controls["role"] = "changed"
        elif fault == "consent": runtime.call("configure_signals", {"activity": False}, NOW)
        elif fault == "cancel": runtime._availability_cache["cancellation"].set()
        elif fault == "source": controls["live"] = False
        elif fault == "invalid_batch": return {}
        elif fault == "exception": raise RuntimeError("Synthetic failed cached read")
        return batch
    runtime._sources_fn = changed_sources
    assert runtime.planning_context("2026-10-19", "UTC", NOW)["windows"] == []
    assert len(controls["calls"]) == 1 and len(controls["reads"]) == 2
    if fault not in {"invalid_batch", "exception"}:
        assert captured[0].records == captured[0].visits == []


@pytest.mark.parametrize("response", ["empty", "failed", "malformed", "occupied_absence", "wrong_reference"])
def test_unadmitted_analysis_cannot_create_an_away_hint(routine_runtime, response):
    runtime, controls, _ = routine_runtime()
    controls["response"] = response
    runtime._refresh_availability(NOW)
    assert runtime.planning_context("2026-10-19", "UTC", NOW)["windows"] == []
    assert len(controls["calls"]) == 1


def test_pattern_proof_is_bound_before_analysis_and_immutable(routine_runtime, monkeypatch):
    runtime, controls, _ = routine_runtime()
    original = runtime._interpreter.interpret
    def changing_analysis(records, patterns, **kwargs):
        result = original(records, patterns, **kwargs)
        for pattern in patterns:
            pattern["support"] = 4
        controls["changes"]["support"] = 4
        return result
    monkeypatch.setattr(runtime._interpreter, "interpret", changing_analysis)
    runtime._refresh_availability(NOW)
    cache = runtime._availability_cache
    assert cache is not None
    with pytest.raises(TypeError):
        cache["pattern_proofs"]["invented"] = "changed"
    assert runtime.planning_context("2026-10-19", "UTC", NOW)["windows"] == []


def test_invalid_fresh_receipt_cannot_replace_old_valid_receipt(routine_runtime):
    from service.assistant.behavior_sources import Batch
    runtime, controls, _ = routine_runtime()
    runtime._refresh_availability(NOW)
    runtime._sources_fn = lambda _: Batch([], [], [lambda: False], {})
    assert runtime._availability_cache["batch"].current()
    assert runtime.planning_context("2026-10-19", "UTC", NOW)["windows"] == []
    assert len(controls["calls"]) == 1


def test_vacuous_fresh_batch_cannot_replace_required_source_proof(routine_runtime):
    from service.assistant.behavior_sources import Batch
    runtime, controls, _ = routine_runtime()
    runtime._refresh_availability(NOW)
    runtime._sources_fn = lambda _: Batch([], [], [], {})
    assert runtime.planning_context("2026-10-19", "UTC", NOW)["windows"] == []


@pytest.mark.parametrize("change", [
    {"descriptors": {}}, {"descriptors": {"device:idle": float("nan")}},
    {"descriptors": {"device:idle": True}}, {"descriptors": {"device:idle": 10**1000}},
    {"descriptors": {"device:idle": .333, "app:arbitrary": .05}},
    {"descriptors": {"device:unknown": .333}}, {"coverage": "unknown"}, {"coverage": []},
    {"support": True}, {"support": 2}, {"opportunities": 100}, {"distinct_weeks": 2},
    {"evidence": []}, {"confidence": float("inf")}, {"end": NOW},
])
def test_unknown_or_malformed_recurrence_is_not_an_away_hint(change):
    from dataclasses import replace
    rows = [replace(o, attrs={**o.attrs, "apps": {}, "idle": 900}) for o in history()]
    pattern = discover(rows, [], "2026-10-19", "UTC", NOW)[0]
    pattern.update(change)
    item = dict(pattern_id=pattern["id"], availability="unknown", evidence=pattern["evidence"], reason="Unknown availability")
    assert apply_interpretations([pattern], [item]) == []


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


@pytest.mark.parametrize("reverse", [False, True])
def test_window_descriptors_never_borrow_apps_from_other_hours(reverse):
    from dataclasses import replace
    rows = []
    for original in history(weeks=3):
        if not original.id.endswith(":600"):
            continue
        for hour in (8, 9, 10, 12, 13, 14, 15, 16, 17, 18, 19, 20):
            start = original.ts + (hour-10)*3600
            apps = {"org.arbitrary.Alpha": 900} if hour == 10 else {"org.arbitrary.Alpha": 720, "net.unseen.Omega": 180}
            rows.append(replace(original, id=original.id+":"+str(hour), ts=start, end_ts=start+900,
                                attrs={**original.attrs, "apps": apps}))
    result = discover(list(reversed(rows)) if reverse else rows, [], "2026-10-19", "UTC", NOW)
    pattern = next(p for p in result if datetime.fromtimestamp(p["start"], ZoneInfo("UTC")).hour == 10)
    assert pattern["descriptors"] == {"app:org.arbitrary.Alpha": 1.0}
    assert pattern["support"] == pattern["opportunities"] == pattern["distinct_weeks"] == 3
    assert pattern["end"]-pattern["start"] == 900


def test_merged_descriptors_use_only_shared_supporting_dates():
    from dataclasses import replace
    rows = changing_history({(2, 600), (3, 615)})
    rows = [replace(o, attrs={**o.attrs, "apps": {"org.arbitrary.Alpha": 720, "net.unseen.Omega": 180}})
            if (int(o.id.split(":")[1]), int(o.id.split(":")[2])) in {(3, 600), (2, 615)} else o
            for o in rows]
    result = discover(rows, [], "2026-10-19", "UTC", NOW)
    assert len(result) == 1
    pattern = result[0]
    assert pattern["support"] == 3 and pattern["opportunities"] == 5
    assert pattern["end"]-pattern["start"] == 1800
    assert pattern["descriptors"] == {"app:org.arbitrary.Alpha": 1.0}


def test_recurring_away_remains_unknown_context_without_occupied_promotion():
    from dataclasses import replace
    rows = [replace(o, attrs={**o.attrs, "apps": {}, "idle": 900, "inactive": 900, "screen_sleep": 900})
            for o in history()]
    patterns = discover(rows, [], "2026-10-19", "UTC", NOW)
    assert len(patterns) == 1 and patterns[0]["support"] == 5
    assert "availability unknown" in patterns[0]["interpretation"]
    model_proposal = dict(pattern_id=patterns[0]["id"], availability="likely_occupied",
                          evidence=patterns[0]["evidence"], reason="Untrusted occupation guess")
    assert apply_interpretations(patterns, [model_proposal]) == []
    assert patterns[0]["descriptors"] == {"device:idle": .333, "device:inactive": .333, "device:screen_sleep": .333}


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


def test_actual_message_subset_runtime_rehydrates_old_analysis_only_with_current_proof(tmp_path, monkeypatch, live_message_publication):
    import json
    from service.assistant.behavior_runtime import BehaviorRuntime
    from service.assistant.behavior_sources import capture
    from service.assistant.behavior_interpreter import Interpreter
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    messages, clock = live_message_publication
    initial = clock[0]
    quote = "The arbitrary preparation is due tomorrow at 4pm UTC."
    stamp = datetime.fromtimestamp(initial-60, ZoneInfo("UTC")).isoformat()
    carrier = v3_carrier([record(guid="complete", text=quote, timestamp=stamp),
        record(guid="partial", text="An incomplete passage must never be interpreted.", timestamp=stamp)], partial=(1,))
    messages.cache_messages(carrier, True)
    calls, role = [], ["fake-local-role"]
    def complete(_system, prompt, **_):
        rows = json.loads(prompt)["evidence"]
        calls.append(rows)
        return json.dumps(dict(patterns=[], links=[], demands=[dict(source_id=r["id"], quote=r["text"],
            kind="preparation", title="Prepare", temporal_id=r["temporal"][0]["id"],
            ownership="user", importance="unknown") for r in rows]))
    runtime = BehaviorRuntime(tmp_path, local_factory=lambda p: BehaviorEngine(p, tz="UTC", clock=lambda: clock[0]),
        behavior_sources_fn=lambda s: capture(s, now=clock[0]), behavior_interpreter=Interpreter(complete),
        role_key_fn=lambda: role[0], clock=lambda: clock[0])
    day = datetime.fromtimestamp(initial, ZoneInfo("UTC")).date().isoformat()
    try:
        runtime.call("configure_signals", {"messages": True, "interpretation": True}, initial)
        runtime._refresh_availability(initial)
        first = runtime.planning_context(day, "UTC", initial)
        assert first["ready"] and len(first["demands"]) == 1 and len(calls) == 1
        assert [r["text"] for r in calls[0]] == [quote]
        demand = first["demands"][0]
        assert demand["source_receipt_at"] == initial and demand["observed_at"] == initial-60
        assert demand["quote"] == quote and demand["action_authority"] == "none"
        assert demand["confidence"] is None and demand["importance"] == "unknown"
        assert "aggregate partial coverage" in first["source_coverage"]["messages"]
        assert runtime.seed_progress()["sources"]["messages"]["publication_complete"] is False

        clock[0] += 1800
        assert runtime.planning_context(day, "UTC", clock[0])["demands"] == []
        messages.cache_messages(carrier, True)  # new receipt, unchanged source passage
        refreshed = runtime.planning_context(day, "UTC", clock[0])
        assert len(refreshed["demands"]) == 1 and len(calls) == 1
        assert refreshed["generated_at"] == first["generated_at"] == initial
        assert refreshed["evidence_checked_at"] == clock[0]
        assert refreshed["demands"][0]["analyzed_at"] == initial
        assert refreshed["demands"][0]["source_receipt_at"] == clock[0]
        assert refreshed["demands"][0]["observed_at"] == initial-60
        role[0] = "different-role"
        assert runtime.planning_context(day, "UTC", clock[0])["demands"] == []
        role[0] = "fake-local-role"
        assert len(runtime.planning_context(day, "UTC", clock[0])["demands"]) == 1
        messages.cache_messages("", False)
        assert runtime.planning_context(day, "UTC", clock[0])["demands"] == []
        messages.cache_messages(carrier, True)
        runtime.call("configure_signals", {"messages": False}, clock[0])
        assert runtime.planning_context(day, "UTC", clock[0])["demands"] == []
        assert len(calls) == 1  # planning reads and revocations never invoke inference
    finally:
        runtime.close()


@pytest.mark.parametrize("mutation", ["unavailable", "superseded", "same_generation_changed", "stale", "role", "consent", "stop"])
def test_actual_message_subset_runtime_withholds_mid_analysis_changes(tmp_path, monkeypatch, live_message_publication, mutation):
    import json
    from service.assistant.behavior_runtime import BehaviorRuntime, RuntimeStopped
    from service.assistant.behavior_sources import capture
    from service.assistant.behavior_interpreter import Interpreter
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    messages, clock = live_message_publication
    quote = "The arbitrary preparation is due tomorrow at 4pm UTC."
    stamp = datetime.fromtimestamp(clock[0]-60, ZoneInfo("UTC")).isoformat()
    carrier = v3_carrier([record(text=quote, timestamp=stamp)], coverage=dict(attempted=2, truncated=1))
    messages.cache_messages(carrier, True)
    role, calls = ["fake"], []
    def complete(_system, prompt, **_):
        rows = json.loads(prompt)["evidence"]
        calls.append(rows)
        if mutation == "unavailable":
            messages.cache_messages("", False)
        elif mutation == "superseded":
            messages.cache_messages(carrier, True)
        elif mutation == "same_generation_changed":
            monkeypatch.setattr(messages, "_lines", v3_carrier([record(text="Changed source passage", timestamp=stamp)]))
        elif mutation == "stale":
            clock[0] += 601
        elif mutation == "role":
            role[0] = "changed"
        elif mutation == "consent":
            runtime.call("configure_signals", {"messages": False}, clock[0])
        else:
            runtime.stop()
        return json.dumps(dict(patterns=[], links=[], demands=[dict(source_id=r["id"], quote=r["text"],
            kind="preparation", title="Prepare", temporal_id=r["temporal"][0]["id"],
            ownership="user", importance="unknown") for r in rows]))
    runtime = BehaviorRuntime(tmp_path, local_factory=lambda p: BehaviorEngine(p, tz="UTC", clock=lambda: clock[0]),
        behavior_sources_fn=lambda s: capture(s, now=clock[0]), behavior_interpreter=Interpreter(complete),
        role_key_fn=lambda: role[0], clock=lambda: clock[0])
    try:
        runtime.call("configure_signals", {"messages": True, "interpretation": True}, clock[0])
        engine = runtime.local_engine()
        runtime._refresh_availability(clock[0])
        assert len(calls) == 1 and runtime._availability_cache is None
        day = datetime.fromtimestamp(clock[0], ZoneInfo("UTC")).date().isoformat()
        if mutation == "stop":
            with pytest.raises(RuntimeStopped):
                runtime.planning_context(day, "UTC", clock[0])
        else:
            assert runtime.planning_context(day, "UTC", clock[0])["demands"] == []
        assert len(calls) == 1
        assert engine.seed_index().work() == []
    finally:
        runtime.close()


def test_actual_seed_runtime_retains_prior_work_without_model_on_planning_read(tmp_path, monkeypatch):
    import json
    from service.assistant.behavior_runtime import BehaviorRuntime
    from service.assistant.behavior_sources import Batch
    from service.assistant.behavior_seed import Snapshot
    from service.assistant.behavior_interpreter import Interpreter
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    live, calls, role = [True], [], ["fake"]
    text = "The payload assessment is due tomorrow at 4pm UTC."
    publication = Snapshot("email", tuple(dict(id=str(i), text=text+f" Item {i}.", source_time=NOW,
        time_basis="source_timestamp", direction="incoming") for i in range(6)),
        enabled=True, available=True, current=lambda: live[0], publication_complete=True)
    def sources(_):
        return Batch([], [], [], {"email": "bounded test publication"}, (publication,))
    def complete(_system, prompt, **_):
        calls.append(1)
        rows = json.loads(prompt)["evidence"]
        return json.dumps(dict(patterns=[], links=[], demands=[dict(source_id=r["id"], quote=r["text"],
            kind="preparation", title="Prepare payload", temporal_id=r["temporal"][0]["id"],
            ownership="user", importance="unknown") for r in rows]))
    runtime = BehaviorRuntime(tmp_path, local_factory=lambda p: BehaviorEngine(p, tz="UTC", clock=lambda: NOW),
        behavior_sources_fn=sources, behavior_interpreter=Interpreter(complete), role_key_fn=lambda: role[0], clock=lambda: NOW)
    try:
        runtime.call("configure_signals", {"email": True, "interpretation": True}, NOW)
        runtime._refresh_availability(NOW)
        runtime._refresh_availability(NOW+301)
        assert len(calls) == 2
        runtime._availability_cache = None  # simulate expiry/loss of transient pattern interpretation
        result = runtime.planning_context("2026-10-19", "UTC", NOW+1800)
        assert result["ready"] and len(result["demands"]) == 6 and result["windows"] == []
        assert result["generated_at"] <= NOW+301
        assert runtime.planning_context("2026-10-19", "UTC", NOW+2000)["generated_at"] == result["generated_at"]
        assert len(calls) == 2
        assert all(d["confidence"] is None and d["importance"] == "unknown" for d in result["demands"])
        role[0] = "different-role"
        assert runtime.planning_context("2026-10-19", "UTC", NOW+1800)["demands"] == []
        role[0] = "fake"
        live[0] = False
        assert runtime.planning_context("2026-10-19", "UTC", NOW+1800)["demands"] == []
        live[0] = True
        runtime.call("configure_signals", {"email": False}, NOW+1801)
        assert runtime.planning_context("2026-10-19", "UTC", NOW+1801)["demands"] == []
    finally:
        runtime.close()


def test_cached_seed_adapters_enumerate_beyond_latest_eight_without_source_fetch():
    from types import SimpleNamespace
    from service.assistant.behavior_sources import capture
    live = [True]
    publication = SimpleNamespace(generation=7, synced_at=NOW, rows=tuple(dict(account_id="native-account", message_id=str(i),
        subject="An arbitrary subject", body="A sufficiently long body.", ts=NOW-i) for i in range(13)))
    def getter(*, expected_generation=None):
        return publication if live[0] and expected_generation in (None, 7) else None
    batch = capture({"email": True}, {"email": getter}, now=NOW)
    assert len(batch.records) == 8 and len(batch.snapshots[0].records) == 13
    assert batch.snapshots[0].published_at == NOW and batch.current()
    live[0] = False
    assert not batch.current()


def test_notes_held_body_needs_matching_successful_live_receipt(monkeypatch):
    from types import SimpleNamespace
    from service.assistant import behavior_sources as sources
    monkeypatch.setattr(sources.time, "time", lambda: NOW)
    publication = SimpleNamespace(generation=1, raw_digest="bounded-content-digest", origin="live", malformed=0, omitted=0,
        body_started_at=NOW-10, body_received_at=NOW-5,
        latest_receipt=dict(available=True, started_at=NOW-10, received_at=NOW-5),
        records=(dict(reference="notes-local:content:0", title="An arbitrary note", body="Prepare tomorrow.", modified_at=NOW-20),))
    batch = sources.capture({"notes": True}, {"notes": lambda: publication}, now=NOW)
    note = next(s for s in batch.snapshots if s.source == "notes")
    assert note.available and note.records[0]["source_time"] is None and batch.current()
    publication.latest_receipt = dict(available=False, started_at=NOW-2, received_at=NOW)
    assert not batch.current()
    batch = sources.capture({"notes": True}, {"notes": lambda: publication}, now=NOW)
    assert not next(s for s in batch.snapshots if s.source == "notes").available


def test_retained_calendar_context_requires_matching_current_native_snapshot(engine):
    engine.configure_signals({"calendar_context": True}, NOW)
    event = Observation("native-event", "event", NOW+3600, subject="An arbitrary planned activity", source="calendar", source_id="n1")
    engine.observe_snapshot("calendar", [event], window_start=NOW-86400, window_end=NOW+86400, taken_at=NOW)
    assert engine.semantic_history("calendar", NOW, expected_sync=NOW)[0]["evidence_basis"] == "scheduled"
    assert engine.semantic_history("calendar", NOW, expected_sync=NOW+1) is None
    engine.observe_snapshot("calendar", [], window_start=NOW-86400, window_end=NOW+86400, taken_at=NOW+1)
    assert engine.semantic_history("calendar", NOW+1, expected_sync=NOW+1) == []


def test_seed_metadata_retention_runs_while_learning_is_off(tmp_path, monkeypatch):
    from service.behavior.store import Store
    from service.behavior.source_index import SourceIndex
    from service.assistant.behavior_seed import Seed, Snapshot
    from service.assistant.behavior_runtime import age_out
    store = Store(tmp_path / "behavior.db")
    publication = Snapshot("email", (dict(id="fake", text="A source passage worth inspecting.", source_time=NOW,
        time_basis="source_timestamp"),), enabled=True, available=True, current=lambda: True)
    Seed(SourceIndex(store)).run([publication], epoch="one", now=NOW, analysis_enabled=True,
        analyze=lambda *a, **k: {"links": []}, admit=lambda: True)
    store.close()
    monkeypatch.setenv("WISP_BEHAVIOR", "0")
    result = age_out(tmp_path, NOW+181*86400)
    assert result["seed_metadata"] >= 1
    store = Store(tmp_path / "behavior.db")
    try:
        assert store._conn.execute("SELECT count(*) FROM behavior_seed_records").fetchone()[0] == 0
    finally:
        store.close()


def test_existing_forget_deletes_seed_in_same_behavior_database(tmp_path, monkeypatch):
    from service.behavior.store import Store
    from service.behavior.source_index import SourceIndex
    from service.assistant.behavior_seed import Seed, Snapshot
    from service.assistant import behavior_runtime as module
    store = Store(tmp_path / "behavior.db")
    publication = Snapshot("email", (dict(id="fake", text="A source passage worth inspecting."),),
        enabled=True, available=True, current=lambda: True)
    Seed(SourceIndex(store)).run([publication], epoch="one", now=NOW, analysis_enabled=True,
        analyze=lambda *a, **k: {"links": []}, admit=lambda: True)
    store.close()
    monkeypatch.setattr(module, "_runtime", None)
    result = module.forget_all(None, home=tmp_path)
    assert result["files_failed"] == [] and not (tmp_path / "behavior.db").exists()


def test_busy_label_requires_unchanged_support_and_seven_day_horizon(tmp_path, monkeypatch):
    import json
    from service.assistant.behavior_runtime import BehaviorRuntime
    from service.assistant.behavior_interpreter import Interpreter
    from service.assistant.behavior_sources import Batch
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    pattern = dict(id="personal-pattern", evidence=["covered-observation"], descriptors={"app:org.arbitrary.Alpha": 1},
        start=NOW+3600, end=NOW+7200, support=4, opportunities=5, timezone="UTC", observed_at=NOW-86400)
    interpreter = Interpreter(lambda *a, **k: json.dumps(dict(demands=[], patterns=[dict(pattern_id=pattern["id"],
        availability="likely_occupied", reason="A provisional personal pattern", evidence=pattern["evidence"])])))
    runtime = BehaviorRuntime(tmp_path, local_factory=lambda p: BehaviorEngine(p, tz="UTC", clock=lambda: NOW),
        behavior_sources_fn=lambda _: Batch([], [], [], {}), behavior_interpreter=interpreter,
        role_key_fn=lambda: "fake", clock=lambda: NOW)
    try:
        runtime.call("configure_signals", {"activity": True, "interpretation": True}, NOW)
        monkeypatch.setattr(runtime.local_engine(), "availability_patterns", lambda *a, **k: [dict(pattern)])
        runtime._refresh_availability(NOW)
        assert len(runtime.planning_context("2026-10-19", "UTC", NOW)["windows"]) == 1
        pattern["descriptors"] = {"different-feature": 1}
        assert runtime.planning_context("2026-10-19", "UTC", NOW)["windows"] == []
        assert runtime.planning_context("2027-10-19", "UTC", NOW)["windows"] == []
    finally:
        runtime.close()


def test_runtime_retained_calendar_adapter_checks_original_receipt_and_canonical_generation(tmp_path, monkeypatch):
    from service.assistant.behavior_runtime import BehaviorRuntime
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    receipt = dict(available=True, last_sync=NOW, diagnostics=dict(authorized=True, snapshot_started_at=NOW-10,
        event_ids=["PRIVATE_DIAGNOSTIC_TITLE"], coverage_start=NOW-86400, coverage_end=NOW+86400))
    runtime = BehaviorRuntime(tmp_path, calendar_sync_fn=lambda: receipt, clock=lambda: NOW,
        local_factory=lambda p: BehaviorEngine(p, tz="UTC", clock=lambda: NOW))
    try:
        engine = runtime.local_engine()
        engine.configure_signals({"calendar_context": True}, NOW)
        event = Observation("native-event", "event", NOW+3600, subject="A scheduled activity", source="calendar", source_id="id1")
        engine.observe_snapshot("calendar", [event], window_start=NOW-86400, window_end=NOW+86400, taken_at=NOW)
        batch = runtime._source_batch(engine.signal_settings()["settings"], NOW)
        snapshot = next(s for s in batch.snapshots if s.source == "calendar")
        assert snapshot.available and snapshot.published_at == NOW-10 and batch.current()
        assert "PRIVATE_DIAGNOSTIC_TITLE" not in repr(snapshot.current.__defaults__)
        receipt["last_sync"] = NOW-1  # a different native generation not yet ingested into behavior
        assert not batch.current()
        batch = runtime._source_batch(engine.signal_settings()["settings"], NOW)
        assert not next(s for s in batch.snapshots if s.source == "calendar").available
        receipt["last_sync"] = NOW
        receipt["diagnostics"]["snapshot_started_at"] = NOW-1300
        batch = runtime._source_batch(engine.signal_settings()["settings"], NOW)
        assert not next(s for s in batch.snapshots if s.source == "calendar").available
    finally:
        runtime.close()


def test_completed_reminder_context_is_partial_and_needs_current_original_native_receipt(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from service.assistant.behavior_runtime import BehaviorRuntime
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    receipt = dict(available=True, last_sync=NOW, snapshot_started_at=NOW-10, diagnostics={})
    runtime = BehaviorRuntime(tmp_path, clock=lambda: NOW, calendar_sync_fn=lambda: {},
        local_factory=lambda p: BehaviorEngine(p, tz="UTC", clock=lambda: NOW))
    try:
        runtime._planning_store = SimpleNamespace(today_snapshot=lambda *a: {"sources": {"reminders": receipt}})
        engine = runtime.local_engine()
        engine.configure_signals({"reminders_context": True}, NOW)
        engine.observe([Observation("completed-one", "reminder_done", NOW-86400, subject="Completed work", source="reminders",
            attrs=dict(completion_time_known=True, evidence_basis="recorded_completion", completion_ts=NOW-86400))])
        batch = runtime._source_batch(engine.signal_settings()["settings"], NOW)
        snapshot = next(s for s in batch.snapshots if s.source == "reminders")
        assert snapshot.available and not snapshot.publication_complete and len(snapshot.records) == 1
        assert snapshot.records[0]["evidence_basis"] == "recorded_completion"
        receipt["snapshot_started_at"] = NOW-1300
        assert not batch.current()
    finally:
        runtime.close()
