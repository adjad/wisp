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


@pytest.fixture
def captured_availability(routine_runtime,monkeypatch):
    """Actual runtime, interpreter, index and publication guard; synthetic source."""
    from copy import deepcopy
    from service.publication_authority import Authority,Witness
    runtime,controls,pattern = routine_runtime("active",sites=True)
    source = Authority("availability-fallback-synthetic-source")
    source.publish()
    original_sources = runtime._sources_fn
    captured = []
    def sources(settings):
        batch = original_sources(settings)
        batch.detail = {"websites":"captured-private-coverage"}
        batch.publication_witnesses = (Witness.combine(source.token()),)
        captured.append(batch)
        return batch
    runtime._sources_fn = sources
    # A real durable index exercises the enriched readback path even when this
    # particular synthetic publication contains only recurring activity.
    runtime.local_engine().seed_index()
    runtime._refresh_availability(NOW)
    positive = runtime.planning_context("2026-10-19","UTC",NOW)
    assert positive["ready"] and len(positive["windows"])==len(positive["patterns"])==1
    assert positive["patterns"][0]["website_visit_context"]==["arbitrary.test"]
    assert positive["source_coverage"]=={"websites":"captured-private-coverage"}
    assert len(controls["calls"])==1 and source.token().current()
    monkeypatch.setattr(runtime,"clock",lambda:NOW+9)
    return runtime,controls,source,captured,deepcopy(positive)


def _assert_no_captured_availability(value,positive):
    import json
    assert value["ready"] is False
    assert all(value[key]==[] for key in ("patterns","windows","demands","associations"))
    assert value["source_coverage"]=={} and not value.get("observed_context")
    assert value["generated_at"]==value["evidence_checked_at"]==NOW+9
    assert value["timezone"]=="UTC" and value["version"]==1
    serialized = json.dumps(value,sort_keys=True)
    for marker in ("arbitrary.test","captured-private-coverage",positive["patterns"][0]["id"],
                   *positive["patterns"][0]["evidence"]):
        assert marker not in serialized


@pytest.mark.parametrize("transport",["runtime","http"])
@pytest.mark.parametrize("fault",["source","role","settings","exception"])
def test_withdrawn_midcapture_response_contains_no_captured_context(captured_availability,monkeypatch,transport,fault):
    runtime,controls,source,captured,positive = captured_availability
    original_sources = runtime._sources_fn
    def changed(settings):
        batch = original_sources(settings)
        assert batch.visits and batch.detail and batch.current()
        if fault=="source":
            source.withdraw()
            controls["live"] = False
        elif fault=="role":
            controls["role"] = "changed-after-capture"
        elif fault=="settings":
            runtime.call("configure_signals",{"websites":False},NOW+9)
        else:
            raise RuntimeError("captured-private-coverage read failed")
        return batch
    runtime._sources_fn = changed
    if transport=="runtime":
        value = runtime.planning_context("2026-10-19","UTC",NOW+9)
    else:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from service import behavior_api
        monkeypatch.setattr(behavior_api.behavior_runtime,"get_runtime",lambda:runtime)
        app = FastAPI()
        app.include_router(behavior_api.router)
        with TestClient(app) as client:
            response = client.get("/behavior/availability",params={"day":"2026-10-19","timezone":"UTC"})
        assert response.status_code==200
        value = response.json()
    _assert_no_captured_availability(value,positive)
    assert len(controls["calls"])==1 and len(captured)==3


@pytest.mark.parametrize("transport",["runtime","http"])
@pytest.mark.parametrize("fault",["source","cache","cancel","exception"])
def test_final_availability_refusal_scrubs_enriched_response(captured_availability,monkeypatch,transport,fault):
    from service.publication_authority import Witness
    runtime,controls,source,captured,positive = captured_availability
    seen = []
    if fault=="exception":
        original_context = runtime.local_engine().availability_context
        def failed_context(*args,**kwargs):
            original_context(*args,**kwargs)
            assert captured[-1].detail==positive["source_coverage"]
            seen.append("after-enrichment")
            raise RuntimeError("captured-private-coverage context failed")
        monkeypatch.setattr(runtime.local_engine(),"availability_context",failed_context)
    else:
        original_prepare = runtime._prepare_publication_witness
        def withdrawn_after_preparation(*args,**kwargs):
            witness = original_prepare(*args,**kwargs)
            assert type(witness) is Witness and witness.current()
            assert captured[-1].detail==positive["source_coverage"]
            seen.append(witness)
            if fault=="source": source.withdraw()
            elif fault=="cache": runtime._availability_cache = None
            else: runtime._availability_cache["cancellation"].set()
            return witness
        monkeypatch.setattr(runtime,"_prepare_publication_witness",withdrawn_after_preparation)
    if transport=="runtime":
        value = runtime.planning_context("2026-10-19","UTC",NOW+9)
    else:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from service import behavior_api
        monkeypatch.setattr(behavior_api.behavior_runtime,"get_runtime",lambda:runtime)
        app = FastAPI()
        app.include_router(behavior_api.router)
        with TestClient(app) as client:
            response = client.get("/behavior/availability",params={"day":"2026-10-19","timezone":"UTC"})
        assert response.status_code==200
        value = response.json()
    assert len(seen)==1 and len(captured)==3 and len(controls["calls"])==1
    if fault in {"source","cancel"}: assert not seen[0].current()
    elif fault=="cache": assert seen[0].current()  # Actual cache identity guard refused.
    _assert_no_captured_availability(value,positive)


def test_failed_availability_views_never_alias_captured_or_later_views(captured_availability,monkeypatch):
    import json
    runtime,controls,source,captured,positive = captured_availability
    original = json.dumps(positive,sort_keys=True)
    def unavailable(*args,**kwargs):
        assert captured[-1].detail==positive["source_coverage"]
        raise RuntimeError("context unavailable after enrichment")
    monkeypatch.setattr(runtime.local_engine(),"availability_context",unavailable)
    first = runtime.planning_context("2026-10-19","UTC",NOW+9)
    _assert_no_captured_availability(first,positive)
    for field in ("patterns","windows","demands","associations"):
        first[field].append({"caller":"injected-private-value"})
    first["source_coverage"]["caller"] = {"nested":["injected-private-value"]}
    second = runtime.planning_context("2026-10-19","UTC",NOW+9)
    _assert_no_captured_availability(second,positive)
    assert all(first[field] is not second[field] for field in ("patterns","windows","demands","associations","source_coverage"))
    assert json.dumps(positive,sort_keys=True)==original
    assert all(batch.detail=={"websites":"captured-private-coverage"} for batch in captured)
    assert source.token().current() and len(controls["calls"])==1


def test_current_uninterpreted_raw_patterns_remain_admitted_without_new_source_read(routine_runtime):
    runtime,controls,pattern = routine_runtime("active",sites=True)
    runtime._refresh_availability(NOW)
    runtime._availability_cache = None
    before = len(controls["reads"])
    value = runtime.planning_context("2026-10-19","UTC",NOW+9)
    assert value["ready"] is False and value["windows"]==value["demands"]==value["associations"]==[]
    assert len(value["patterns"])==1 and value["patterns"][0]["id"]==pattern["id"]
    assert value["patterns"][0]["website_visit_context"]==[]
    assert len(controls["reads"])==before and len(controls["calls"])==1


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
    from service.publication_authority import Clock
    monkeypatch.setattr(messages,"LIVE_CLOCK",Clock())
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
    from service.publication_authority import Clock
    monkeypatch.setattr(messages,"LIVE_CLOCK",Clock())
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
    from service.publication_authority import Authority,Witness
    source = Authority("synthetic-bounded-email-source")
    source.publish()
    text = "The payload assessment is due tomorrow at 4pm UTC."
    publication = Snapshot("email", tuple(dict(id=str(i), text=text+f" Item {i}.", source_time=NOW,
        time_basis="source_timestamp", direction="incoming") for i in range(6)),
        enabled=True, available=True, current=lambda: live[0], publication_complete=True,
        publication_witness=Witness.combine(source.token()))
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
    from service.assistant.store import AssistantStore
    from service.assistant.behavior_seed import Seed, Snapshot
    from service.assistant import behavior_runtime as module
    store = Store(tmp_path / "behavior.db")
    publication = Snapshot("email", (dict(id="fake", text="A source passage worth inspecting."),),
        enabled=True, available=True, current=lambda: True)
    Seed(SourceIndex(store)).run([publication], epoch="one", now=NOW, analysis_enabled=True,
        analyze=lambda *a, **k: {"links": []}, admit=lambda: True)
    assert store._conn.execute("SELECT COUNT(*) FROM behavior_seed_records").fetchone()[0]==1
    store.close()
    monkeypatch.setattr(module, "_runtime", None)
    planning = AssistantStore(tmp_path / "assistant.db")
    try:
        unrelated = planning.add_manual("Unrelated authored task",NOW+600)
        result = module.forget_all(planning, home=tmp_path)
        assert result["files_failed"] == [] and not (tmp_path / "behavior.db").exists()
        assert planning.get(unrelated["id"])==unrelated
        assert result["removed_items"]==[] and result["kept_items"]==0
    finally:
        planning._db.close()


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
    from service.assistant import store as store_module
    from service.assistant.store import AssistantStore
    from service.assistant.behavior_runtime import BehaviorRuntime
    from service.publication_authority import Clock
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    clock = [NOW]
    monkeypatch.setattr(store_module.time,"time",lambda:clock[0])
    monkeypatch.setattr(store_module,"LIVE_CLOCK",Clock())
    planning = AssistantStore(tmp_path/"planning.db")
    planning.sync_source("reminders",[],diagnostics=dict(authorized=True,snapshot_started_at=NOW-10))
    runtime = BehaviorRuntime(tmp_path/"runtime",clock=lambda:clock[0],calendar_sync_fn=lambda:{},
        local_factory=lambda p:BehaviorEngine(p,tz="UTC",clock=lambda:clock[0]))
    try:
        runtime._planning_store = planning
        engine = runtime.local_engine()
        engine.configure_signals({"reminders_context":True},NOW)
        engine.observe([Observation("completed-one","reminder_done",NOW-86400,subject="Completed work",source="reminders",
            attrs=dict(completion_time_known=True,evidence_basis="recorded_completion",completion_ts=NOW-86400))])
        batch = runtime._source_batch(engine.signal_settings()["settings"],NOW)
        snapshot = next(s for s in batch.snapshots if s.source == "reminders")
        assert snapshot.available and not snapshot.publication_complete and len(snapshot.records) == 1
        assert snapshot.records[0]["evidence_basis"] == "recorded_completion"
        assert snapshot.publication_witness.current()
        clock[0] += 1300
        assert not batch.current() and not snapshot.publication_witness.current()
    finally:
        runtime.close()
        planning._db.close()


# These real core modules are copied from the checkout under test into a private
# subprocess. Only the tools registration initializer and explicit external
# boundaries are inert; the public producer, cache APIs, miner, interpreter,
# Seed/SourceIndex, Store and day planner retain their actual implementations.
# Missing or newly imported production modules fail rather than skip coverage.
_PUBLIC_PLANNING_SOURCES = (
    'service/__init__.py',
    'service/assistant/__init__.py',
    'service/assistant/behavior_interpreter.py',
    'service/assistant/behavior_history_driver.py',
    'service/assistant/gmail_history_runtime.py',
    'service/assistant/behavior_runtime.py',
    'service/assistant/behavior_seed.py',
    'service/assistant/behavior_sources.py',
    'service/assistant/daily_workspace.py',
    'service/assistant/recovery.py',
    'service/assistant/reminders.py',
    'service/assistant/scheduler.py',
    'service/assistant/store.py',
    'service/assistant/today.py',
    'service/behavior/__init__.py',
    'service/behavior/activity.py',
    'service/behavior/associations.py',
    'service/behavior/availability.py',
    'service/behavior/categorize.py',
    'service/behavior/commitments.py',
    'service/behavior/engine.py',
    'service/behavior/evidence.py',
    'service/behavior/history.py',
    'service/behavior/miners.py',
    'service/behavior/nudges.py',
    'service/behavior/phraser.py',
    'service/behavior/predict.py',
    'service/behavior/priorities.py',
    'service/behavior/profile.py',
    'service/behavior/source_index.py',
    'service/behavior/store.py',
    'service/behavior/timeutil.py',
    'service/behavior/types.py',
    'service/behavior/weekly.py',
    'service/browser/__init__.py',
    'service/browser/contracts.py',
    'service/discovery/__init__.py',
    'service/discovery/contracts.py',
    'service/discovery/store.py',
    'service/inference/__init__.py',
    'service/message_content.py',
    'service/paths.py',
    'service/publication_authority.py',
    'service/gmail_behavior_history.py',
    'service/gmail_source.py',
    'service/config/authority_changes.py',
    'service/tasks/__init__.py',
    'service/tasks/temporal.py',
    'service/temporal_facts.py',
    'service/tools/__init__.py',
    'service/tools/browser_history_tools.py',
    'service/tools/cache_store.py',
    'service/tools/email_tools.py',
    'service/tools/everyday.py',
    'service/tools/imessage_tools.py',
    'service/tools/message_digest.py',
    'service/tools/notes_tools.py',
    'service/tools/registry.py',
    'service/tools/timeranges.py',
)


def test_actual_public_behavior_producer_to_today_from_coherent_checkout(tmp_path):
    from pathlib import Path
    import hashlib
    import json
    import subprocess
    import sys

    checkout = Path(__file__).resolve().parents[1]
    private = tmp_path / "public-planning-composition"
    private.mkdir(mode=0o700)
    source = private / "source"
    source.mkdir()
    for name in ("home", "state", "tmp", "evidence", "facades"):
        (private / name).mkdir(mode=0o700)
    selected = {}
    for relative in _PUBLIC_PLANNING_SOURCES:
        original = checkout / relative
        assert original.is_file(), "Required real planning source is missing: " + relative
        data = original.read_bytes()
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(0o444)
        selected[relative] = dict(origin="current coherent checkout",
            sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
    manifest = dict(root=str(private), source=str(source), files=selected,
        qualification="Real public composition only; normal app startup and live effects excluded.")
    (private / "SOURCE_INPUT_MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    proof = private / "facades" / "actual-public-proof.py"
    proof.write_text(_PUBLIC_PLANNING_PROOF)
    environment = dict(PATH="/usr/bin:/bin:/usr/sbin:/sbin", HOME=str(private / "home"),
        WISP_HOME=str(private / "state"), TMPDIR=str(private / "tmp"), PYTHONPATH=str(source),
        PYTHONDONTWRITEBYTECODE="1", WISP_BEHAVIOR="0", LANG="en_US.UTF-8")
    completed = subprocess.run([sys.executable, str(proof), str(private)], cwd=source,
        env=environment, text=True, capture_output=True, timeout=60)
    (private / "evidence" / "stdout.log").write_text(completed.stdout)
    (private / "evidence" / "stderr.log").write_text(completed.stderr)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads((private / "evidence" / "actual-public-result.json").read_text())
    assert result["result"] == "PASS" and not result["unexpected_effects"]
    assert len(result["cases"]) == 54
    assert all(case["result"] == "PASS" for case in result["cases"])
    for relative, metadata in selected.items():
        assert hashlib.sha256((source / relative).read_bytes()).hexdigest() == metadata["sha256"]
        assert (checkout / relative).read_bytes() == (source / relative).read_bytes()


# The first 42 controls cover durable source-backed work, receipts, ownership,
# source/query zones and DST, plus actual busy recurrence. The final 12 cover
# actual inactivity recurrence, typed advisory rendering, pins/full authored
# days, corrections, revocation, cancellation and private Forget/restart.
# Guards are installed before importing the actual production modules. The
# source file is embedded so CI needs no external fixture, Git fetch or /tmp
# artifact. Inert facades are confined to configuration/hub/client boundaries;
# none replaces the producer, its public wrapper or the planner.
_PUBLIC_PLANNING_PROOF = r'''"""Bounded C137 -> repaired CCBF consumer proof. Run only from approved private snapshot."""
from pathlib import Path
import ast
import copy
import datetime
import hashlib
import importlib.machinery
import json
import os
import sqlite3
import sys
import threading
import time
import types

ROOT = Path(sys.argv[1]).resolve()
SOURCE = ROOT / 'source'
INPUT = json.loads((ROOT / 'SOURCE_INPUT_MANIFEST.json').read_text())
assert Path(os.environ['HOME']).resolve() == ROOT / 'home'
assert Path(os.environ['WISP_HOME']).resolve() == ROOT / 'state'
assert Path(os.environ['TMPDIR']).resolve() == ROOT / 'tmp'
assert 'WISP_TODAY_WINDOW_CHECK' not in os.environ
for rel, meta in INPUT['files'].items():
    assert hashlib.sha256((SOURCE / rel).read_bytes()).hexdigest() == meta['sha256'], rel

RESULT = {'cases': [], 'guard_controls': [], 'unexpected_effects': [], 'source_manifest': INPUT,
          'facades': {}, 'limitations': ['Aggregate tools initializer omitted; normal app initialization NOT_TESTED.',
          'Config/role/idle/hub/model boundaries synthetic; no provider/model/native/app/beta-runtime qualification.']}
PHASE = 'controls'
class ForbiddenEffect(BaseException):
    pass

def deny(label):
    RESULT['guard_controls' if PHASE == 'controls' else 'unexpected_effects'].append(label)
    raise ForbiddenEffect(label)

def inside(path, root):
    try:
        Path(path).resolve().relative_to(root)
        return True
    except (ValueError, TypeError, OSError):
        return False

RUNTIME_READ = [Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()]

def audit(event, args):
    if event == 'sqlite3.connect':
        if not inside(args[0], ROOT):
            deny('sqlite outside private fixture: ' + str(args[0]))
    elif event in ('subprocess.Popen', 'os.system', 'socket.connect', 'socket.__new__') or event.startswith(('os.exec', 'os.spawn')):
        deny(event)
    elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.utime', 'os.rename', 'os.link', 'os.symlink'):
        indexes = (0, 1) if event in ('os.rename', 'os.link', 'os.symlink') else (0,)
        for index in indexes:
            if len(args) <= index or not inside(os.fsdecode(args[index]), ROOT):
                deny('path mutation outside private fixture: ' + event)
    elif event == 'open' and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.fsdecode(args[0]); mode = args[1] or ''; flags = args[2] if len(args) > 2 else 0
        writing = (isinstance(mode, str) and any(x in mode for x in 'wax+')) or (isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
        if writing and not inside(path, ROOT):
            deny('write outside private fixture: ' + path)
        if not writing and path.startswith('/Users/') and not inside(path, ROOT) and not any(inside(path, p) for p in RUNTIME_READ):
            deny('user read outside private fixture: ' + path)

ALLOWED = set()
for rel in INPUT['files']:
    name = rel.removesuffix('.py').replace('/', '.')
    if name.endswith('.__init__'):
        name = name[:-9]
    ALLOWED.add(name)
FACADES = {'service.tools', 'service.config', 'service.idle', 'service.assistant.hub', 'service.inference.omlx_client'}
class ImportFence:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('service.') and fullname not in ALLOWED | FACADES:
            deny('unapproved service import: ' + fullname)
        return None
fence = ImportFence()
for event, args in [('subprocess.Popen', ()), ('socket.connect', ()), ('sqlite3.connect', ('/outside/fixture.db',)), ('open', ('/outside/fixture.txt', 'w', os.O_WRONLY)), ('os.remove', ('/outside/fixture.txt', -1)), ('os.rename', (str(ROOT/'tmp'/'source'), '/outside/destination', -1, -1))]:
    try:
        audit(event, args)
    except ForbiddenEffect:
        pass
    else:
        raise AssertionError('guard failed: ' + event)
try:
    fence.find_spec('service.main')
except ForbiddenEffect:
    pass
else:
    raise AssertionError('import guard failed')
PHASE = 'execution'
sys.addaudithook(audit)
sys.meta_path.insert(0, fence)
CLOCK = [datetime.datetime(2026, 10, 19, 8, tzinfo=datetime.timezone.utc).timestamp()]
time.time = lambda: CLOCK[0]

def facade(name, exports, package=False):
    m = types.ModuleType(name)
    m.__file__ = str(Path(__file__).resolve())
    m.__spec__ = importlib.machinery.ModuleSpec(name, loader=None, is_package=package)
    if package:
        m.__path__ = []
    def missing(attr):
        if attr.startswith('__'):
            raise AttributeError(attr)
        return deny(name + '.' + attr)
    m.__getattr__ = missing
    for key, value in exports.items():
        setattr(m, key, value)
    sys.modules[name] = m
    RESULT['facades'][name] = {'file': m.__file__, 'exports': sorted(exports), 'source_sha256': hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest(), 'behavior': 'unknown attribute or external call raises ForbiddenEffect'}
    return m

def forbidden(*a, **k):
    return deny('external facade call')
def prepared_config():
    from service.config import authority_changes
    authority_changes.observe({'fixture': 'fixed-local-role'},valid=True,expected=authority_changes.current())
    return {}
config_boundary = facade('service.config', {'role_to_model': forbidden, 'user_facing_summary_kwargs': forbidden,
    'models_config': prepared_config}, package=True)
config_boundary.__path__ = [str(SOURCE / 'service/config')]
config_boundary.authority_changes = importlib.import_module('service.config.authority_changes')
RESULT['facades']['service.config']['exports'].append('authority_changes (actual manifest module)')
facade('service.idle', {})
facade('service.assistant.hub', {'hub': types.SimpleNamespace(publish=forbidden, emit=forbidden)})
class NoClient:
    def __init__(self, *a, **k):
        deny('OMLXClient constructor')
facade('service.inference.omlx_client', {'OMLXClient': NoClient})
import service
pkg = types.ModuleType('service.tools')
pkg.__path__ = [str(SOURCE / 'service/tools')]
pkg.__file__ = str(SOURCE / 'service/tools/__init__.py')
pkg.__spec__ = importlib.machinery.ModuleSpec('service.tools', loader=None, is_package=True)
pkg.__spec__.submodule_search_locations = pkg.__path__
sys.modules['service.tools'] = pkg
service.tools = pkg
RESULT['facades']['service.tools'] = {'file': pkg.__file__, 'behavior': 'Only aggregate register-all initializer omitted; real canonical submodules unchanged.'}

from zoneinfo import ZoneInfo
from service.assistant import behavior_runtime as runtime_module
from service.assistant.behavior_runtime import BehaviorRuntime
from service.assistant.behavior_interpreter import Interpreter
from service.behavior.engine import BehaviorEngine
from service.behavior.types import Observation
from service.assistant import daily_workspace as daily
from service.assistant.store import AssistantStore
from service.assistant.today import day_bounds
from service.paths import MOE_DIR
from service.tools import email_tools as emails, imessage_tools as messages, notes_tools as notes
from service.publication_authority import Clock
assert MOE_DIR.resolve() == ROOT / 'state'
assert daily._availability_provider is None
LIVE = [True]
runtime_module.enabled = lambda: LIVE[0]
ZONE = 'America/Los_Angeles'
N = [0]

def public(day=None, zone=ZONE):
    day = day or datetime.datetime.fromtimestamp(CLOCK[0], ZoneInfo(zone)).date().isoformat()
    return daily.availability_snapshot(day, zone, CLOCK[0])

def carrier(text, stamp):
    raw = dict(guid='fixture-guid', conversation='fixture-chat', sender='fixture-sender', direction='incoming',
               timestamp=datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).isoformat(), text=text, links=[])
    coverage = dict(version=1, kind='coverage', attempted=1, emitted=1, skipped=0, truncated=0,
        limit=2000, byte_limit=4_000_000, window_days=365, row_limit=20000, reached_row_limit=False)
    row = dict(version=1, kind='record', record=raw,
        source=dict(kind='messages', message_guid=raw['guid'], chat_guid=raw['conversation'], navigation_url=None),
        coverage=dict(text='complete', links='complete'))
    return '\n'.join('V3 | ' + json.dumps(v) for v in [coverage, row]) + '\nV2 | fixture legacy'

class Case:
    def __init__(self, source, *, ownership='user', mutation=None, stamp=None, note_age=0, engine_zone='UTC', include_links=False, pattern_availability='likely_occupied'):
        N[0] += 1
        self.home = ROOT / 'state' / ('case-' + str(N[0])); self.home.mkdir()
        self.source = source; self.calls = []; self.role = ['fixture-fast']; self.ownership = ownership; self.mutation = mutation
        self.engine_zone = engine_zone; self.include_links = include_links; self.pattern_availability = pattern_availability
        self.initial = CLOCK[0]; self.source_time = stamp if stamp is not None else self.initial - 60
        # Cases deliberately jump across dates/DST. Each is an independent
        # synthetic source clock epoch; old live tokens are never rearmed.
        messages.LIVE_CLOCK = Clock()
        notes.LIVE_CLOCK = Clock()
        self.text = 'The arbitrary preparation is due 2026-11-02 at 16:00 UTC.'
        if source == 'notes':
            self.text = 'Review the arbitrary roadmap with the team.'
        self.store = AssistantStore(self.home / 'assistant.db')
        self.runtime = BehaviorRuntime(self.home, local_factory=lambda p: BehaviorEngine(p, phraser=None, clock=lambda: CLOCK[0], tz=self.engine_zone),
            messages_fn=forbidden, contact_name_fn=forbidden, calendar_sync_fn=lambda: {}, session_db_fn=forbidden,
            behavior_interpreter=Interpreter(self.complete), role_key_fn=lambda: self.role[0], clock=lambda: CLOCK[0])
        self.runtime._planning_store = self.store
        runtime_module._runtime = self.runtime
        self.runtime.call('configure_signals', {source: True, 'interpretation': True}, CLOCK[0])
        self.publish(note_age=note_age)
        self.calendar()
    def publish(self, *, available=True, changed=False, note_age=0):
        text = self.text + (' Changed revision.' if changed else '')
        if self.source == 'messages':
            messages.cache_messages(carrier(text, self.source_time), available)
        elif self.source == 'notes':
            raw = '\x01'.join([str(self.source_time), 'Synthetic note', 'Fixture', text]) + '\x02'
            notes.cache_notes(raw, available, snapshot_started_at=CLOCK[0] - note_age)
        else:
            emails.set_email_availability(available, read_source='mail_app')
            raw = '\x01'.join([str(self.source_time), 'U', 'Fixture account', 'fixture-account-id',
                'sender@example.invalid', 'self@example.invalid', 'Synthetic preparation', '<fixture-message>', text]) + '\x02'
            emails.cache_raw_emails(raw, dict(complete=True, accounts=['Fixture account'], failed_accounts=[]))
    def complete(self, _system, prompt, **kwargs):
        payload = json.loads(prompt); rows = payload['evidence']; self.calls.append(copy.deepcopy(rows))
        if self.mutation:
            self.mutation(self)
        response = dict(patterns=[dict(pattern_id=p['id'], availability=self.pattern_availability,
                reason='Synthetic completion interprets an actual covered recurring pattern; activity remains uncertain.',
                evidence=p['evidence']) for p in payload['recurring_patterns']],
            demands=[dict(source_id=r['id'], quote=r['text'], kind='preparation', title='Prepare synthetic work',
                temporal_id=(r['temporal'][0]['id'] if r['temporal'] else None),
                ownership=self.ownership, importance='unknown') for r in rows])
        if rows:
            response['links'] = []
            if self.include_links and len(rows) >= 2 and rows[0]['source'] != rows[1]['source']:
                response['links'] = [dict(relation='possibly_related', left_id=rows[0]['id'], right_id=rows[1]['id'],
                    left_quote=rows[0]['text'], right_quote=rows[1]['text'])]
        return json.dumps(response)
    def calendar(self, *, day=None, zone=ZONE):
        day = day or datetime.datetime.fromtimestamp(CLOCK[0], ZoneInfo(zone)).date().isoformat()
        lo, hi = day_bounds(day, zone)
        existing = self.store.today_snapshot(day, zone)['sources'].get('calendar', {})
        if existing.get('snapshot_started_at') != CLOCK[0]:
            self.store.sync_source('calendar', [], diagnostics=dict(authorized=True, complete=True, partial=False,
                snapshot_started_at=CLOCK[0], coverage_start=lo, coverage_end=hi))
        return day
    def seed(self):
        self.runtime._refresh_availability(CLOCK[0])
        assert len(self.calls) == 1, (self.source, self.runtime.errors, self.runtime._seed_status, len(self.calls))
    def read(self, *, day=None, zone=ZONE, refresh_calendar=True):
        day = self.calendar(day=day, zone=zone) if refresh_calendar else (day or datetime.datetime.fromtimestamp(CLOCK[0], ZoneInfo(zone)).date().isoformat())
        snapshot = public(day, zone)
        workspace = daily.workspace(self.store, day, zone, now=CLOCK[0], behavior=snapshot)
        attention = [i for i in workspace['attention'] if i['source'] == 'behavior']
        return snapshot, workspace, attention
    def close(self):
        self.runtime.close(); self.store._db.close(); runtime_module._runtime = None

def passed(name, details=None):
    RESULT['cases'].append({'name': name, 'result': 'PASS', 'details': details})

def main():
    for source, ttl in [('email', 1200), ('messages', 600), ('notes', 86400)]:
        c = Case(source)
        try:
            c.seed()
            first, ws, attention = c.read()
            assert first['ready'] and len(first['demands']) == 1 and len(attention) == 1, (source, first, c.runtime.errors, c.runtime._seed_status)
            original = copy.deepcopy(first['demands'][0]); assert original['analyzed_at'] == c.initial
            assert attention[0]['analysis_timezone'] == 'UTC'
            if source != 'notes':
                assert original['deadline_precision'] == 'instant' and original['due_range'] is not None
            assert attention[0]['due_range'] == original['due_range']
            assert original['action_authority'] == 'none'
            passed(source + ': real public wrapper/default consumer/private Store/Today positive')
            for age in [1801, 2 * 86400]:
                CLOCK[0] = c.initial + age; c.publish()
                snap, ws, items = c.read()
                assert snap['ready'] and len(items) == 1
                assert snap['generated_at'] == c.initial and items[0]['analyzed_at'] == c.initial
                assert items[0]['source_receipt_at'] == CLOCK[0] and items[0]['observed_at'] == original['observed_at']
                assert items[0]['due_range'] == original['due_range'] and items[0]['analysis_timezone'] == 'UTC'
                assert len(c.calls) == 1
                passed(source + ': durable analysis ' + str(age) + ' seconds old')
            stamp = CLOCK[0]; baseline = copy.deepcopy(snap['demands'][0]); changes = c.runtime.local_engine()._store._conn.total_changes; assistant_changes = c.store._db.total_changes
            for delta in [1, 2, 3]:
                CLOCK[0] = stamp + delta
                reread, _, items = c.read(refresh_calendar=False)
                assert items[0]['analyzed_at'] == baseline['analyzed_at'] and items[0]['source_receipt_at'] == baseline['source_receipt_at']
                assert reread['evidence_checked_at'] == CLOCK[0] and len(c.calls) == 1
            assert c.runtime.local_engine()._store._conn.total_changes == changes
            assert c.store._db.total_changes == assistant_changes
            passed(source + ': repeated GET no model/no source receipt renewal/no behavior DB write')
            CLOCK[0] = stamp + ttl - .001
            assert public()['demands'], source
            CLOCK[0] = stamp + ttl
            assert not public()['demands'], source
            assert len(c.calls) == 1
            passed(source + ': exact source TTL boundary and expiry')
        finally:
            c.close()
        for label, mutate in [('revision', lambda x: x.publish(changed=True)), ('revoke', lambda x: x.publish(available=False)),
                              ('role', lambda x: x.role.__setitem__(0, 'different-role')),
                              ('consent', lambda x: x.runtime.call('configure_signals', {source: False}, CLOCK[0])),
                              ('stop', lambda x: x.runtime.stop())]:
            CLOCK[0] += 3601
            c = Case(source)
            try:
                c.seed(); assert public()['demands']; mutate(c)
                assert not public().get('demands') and len(c.calls) == 1
                passed(source + ': current ' + label + ' withholds without inference')
            finally:
                c.close()
    CLOCK[0] += 3601
    c = Case('notes', note_age=3600)
    try:
        c.seed(); snap, ws, items = c.read()
        assert len(items) == 1 and snap['demands'][0]['source_receipt_at'] == CLOCK[0] - 3600
        assert items[0]['observed_at'] is None and items[0]['deadline_precision'] == 'unknown' and items[0]['planning_duration'] is None
        assert items[0]['source_receipt_at'] == CLOCK[0] - 3600
        passed('Notes original read-start one hour old admitted by actual 24h producer, unknown prose/deadline remains review only')
    finally:
        c.close()
    for source in ['email', 'messages', 'notes']:
        CLOCK[0] += 3601
        event = threading.Event()
        c = Case(source, mutation=lambda x: event.set())
        c.runtime._availability_cancel.event = event
        try:
            c.seed(); assert not public().get('demands'); assert c.runtime.local_engine().seed_index().work() == []
            passed(source + ': cancellation during fake completion prevents durable publication')
        finally:
            c.close()
    CLOCK[0] = datetime.datetime(2026, 10, 19, 8, tzinfo=ZoneInfo(ZONE)).timestamp()
    c = Case('email', ownership='unknown')
    try:
        c.seed(); snap, ws, items = c.read()
        assert items and items[0]['ownership'] == 'unknown' and items[0]['planning_duration'] is None
        assert items[0]['needs_review'] and items[0]['importance'] == 'unknown'
        assert not any(b.get('evidence_id') == items[0]['id'] for b in ws['proposed_plan']['blocks'])
        passed('Actual producer unknown ownership remains review only')
    finally:
        c.close()
    for day, seconds in [('2026-03-08', 23 * 3600), ('2026-11-01', 25 * 3600)]:
        CLOCK[0] = datetime.datetime.fromisoformat(day+'T08:00:00').replace(tzinfo=ZoneInfo(ZONE)).timestamp()
        c = Case('email')
        try:
            lo, hi = day_bounds(day, ZONE); assert hi - lo == seconds
            authored = c.store.today_save_task(dict(title='Authored priority work', day=day, timezone=ZONE,
                duration_minutes=60, due_ts=None, pinned_start=None))
            before = c.store.today_snapshot(day, ZONE)['tasks']
            c.seed(); snap, ws, items = c.read(day=day)
            assert items and snap['demands'][0]['timezone'] == items[0]['analysis_timezone'] == 'UTC'
            assert items[0]['due_range'] == snap['demands'][0]['due_range']
            assert items[0]['deadline_precision'] == 'instant' and items[0]['due_ts'] == datetime.datetime(2026, 11, 2, 16, tzinfo=datetime.timezone.utc).timestamp()
            blocks = ws['proposed_plan']['blocks']
            assert any(b.get('task_id') == authored['id'] for b in blocks)
            assert all(lo <= b['start'] < b['end'] <= hi for b in blocks)
            assert c.store.today_snapshot(day, ZONE)['tasks'] == before
            assert len(c.calls) == 1
            passed('Actual cross-zone public producer/Store/Today plan uses '+str(seconds//3600)+' hour DST day and preserves authored work')
        finally:
            c.close()
    CLOCK[0] = datetime.datetime(2026, 11, 1, 8, tzinfo=ZoneInfo(ZONE)).timestamp()
    c = Case('email')
    try:
        c.text = 'The arbitrary preparation is due 2026-11-02.'; c.publish(); c.seed()
        snap, ws, items = c.read(day='2026-11-01')
        expected = [datetime.datetime(2026, 11, d, tzinfo=datetime.timezone.utc).timestamp() for d in [2, 3]]
        assert items and items[0]['deadline_precision'] == 'date' and items[0]['due_ts'] is None
        assert items[0]['due_range'] == snap['demands'][0]['due_range'] == expected
        assert datetime.datetime.fromtimestamp(expected[0], ZoneInfo(ZONE)).date().isoformat() == '2026-11-01'
        assert datetime.datetime.fromtimestamp(expected[1], ZoneInfo(ZONE)).date().isoformat() == '2026-11-02'
        assert items[0]['analysis_timezone'] == 'UTC'
        passed('Actual source UTC date range crosses query midnight without relocalizing its absolute half-open bounds')
    finally:
        c.close()
    CLOCK[0] = datetime.datetime(2026, 11, 1, 8, tzinfo=ZoneInfo(ZONE)).timestamp()
    c = Case('email')
    try:
        c.text = 'The arbitrary preparation is due November 2.'; c.publish(); c.seed()
        _, _, items = c.read(day='2026-11-01')
        assert items and items[0]['deadline_precision'] == 'unknown' and items[0]['due_range'] is None
        assert items[0]['planning_duration'] is None
        passed('Actual yearless date remains unknown review-only; consumer invents no year or deadline')
    finally:
        c.close()
    CLOCK[0] = datetime.datetime(2026, 10, 19, 8, tzinfo=ZoneInfo(ZONE)).timestamp()
    c = Case('email', engine_zone=ZONE, include_links=True)
    try:
        c.runtime.call('configure_signals', {'messages': True, 'activity': True}, CLOCK[0])
        messages.cache_messages(carrier(c.text, c.source_time), True)
        target = datetime.datetime(2026, 10, 19, tzinfo=ZoneInfo(ZONE))
        bins = []
        for week in range(1, 6):
            for minute in list(range(14*60, 16*60, 15)) + list(range(18*60, 19*60, 15)):
                start = target - datetime.timedelta(weeks=week) + datetime.timedelta(minutes=minute)
                bins.append(Observation('synthetic-covered:'+str(week)+':'+str(minute), 'activity_bin', start.timestamp(),
                    end_ts=(start+datetime.timedelta(minutes=15)).timestamp(), source='activity',
                    attrs=dict(timezone=ZONE, covered=900, idle=0, apps={'fixture.arbitrary.reader': 900})))
        assert c.runtime.local_engine().observe(bins) == len(bins)
        c.seed(); first = public('2026-10-19')
        assert len(first['windows']) == 2 and len(first['associations']) == 1, first
        old = c.initial
        assert first['associations'][0]['analyzed_at'] == old
        CLOCK[0] += 1801; c.publish(); messages.cache_messages(carrier(c.text, c.source_time), True)
        c.runtime._refresh_availability(CLOCK[0]); assert len(c.calls) == 2
        snap, ws, items = c.read(day='2026-10-19')
        assert snap['ready'] and snap['generated_at'] == old and snap['evidence_checked_at'] == CLOCK[0]
        assert len(snap['associations']) == 1 and snap['associations'][0]['analyzed_at'] == old
        assert len(snap['windows']) == 2 and all(d['analyzed_at'] == old for d in snap['demands'])
        assert ws['tentative_count'] == 2 and len(items) == 2
        spans = [(datetime.datetime.fromtimestamp(b['start'], ZoneInfo(ZONE)).hour,
                  datetime.datetime.fromtimestamp(b['end'], ZoneInfo(ZONE)).hour)
                 for b in ws['proposed_plan']['blocks'] if b['kind'] == 'behavior']
        assert spans == [(14, 16), (18, 19)], spans
        for i in range(3):
            c.read(day='2026-10-19', refresh_calendar=False)
        assert len(c.calls) == 2
        passed('Actual old saved cross-source association/demands coexist with freshly interpreted real recurring 14-16 and 18-19 windows; GET runs no model')
        CLOCK[0] += 1
        lo, hi = day_bounds('2026-10-19', ZONE)
        c.store.sync_source('calendar', [], diagnostics=dict(authorized=True, complete=False, partial=True,
            snapshot_started_at=CLOCK[0], coverage_start=lo, coverage_end=hi))
        _, held, _ = c.read(day='2026-10-19', refresh_calendar=False)
        assert not held['proposed_plan']['sources']['calendar']['ready']
        assert not any(b['kind'] in ('task', 'suggestion') for b in held['proposed_plan']['blocks'])
        passed('Actual latest persisted partial Calendar holds flexible behavior/source work back')
        CLOCK[0] += 1; c.calendar(day='2026-10-19')
        _, recovered, _ = c.read(day='2026-10-19', refresh_calendar=False)
        assert recovered['proposed_plan']['sources']['calendar']['ready'] and recovered['suggested_count'] > 0
        assert len(c.calls) == 2
        passed('Actual complete Calendar successor recovers planning without renewed analysis or inference')
    finally:
        c.close()
    def inactivity_case(*, interpretation='unknown', mutation=None):
        CLOCK[0] = datetime.datetime(2026, 10, 19, 8, tzinfo=ZoneInfo(ZONE)).timestamp()
        c = Case('email', engine_zone=ZONE, pattern_availability=interpretation, mutation=mutation)
        c.runtime.call('configure_signals', {'activity': True}, CLOCK[0])
        target = datetime.datetime(2026, 10, 19, tzinfo=ZoneInfo(ZONE))
        rows = []
        for week in range(1, 6):
            for minute in range(14*60, 16*60, 15):
                start = target - datetime.timedelta(weeks=week) + datetime.timedelta(minutes=minute)
                rows.append(Observation('private-idle:'+str(week)+':'+str(minute), 'activity_bin', start.timestamp(),
                    end_ts=(start+datetime.timedelta(minutes=15)).timestamp(), source='activity',
                    attrs=dict(timezone=ZONE, covered=900, idle=900, apps={})))
        assert c.runtime.local_engine().observe(rows) == len(rows)
        return c
    c = inactivity_case()
    try:
        c.seed(); snap, ws, _ = c.read(day='2026-10-19')
        assert len(snap['windows']) == 1 and snap['windows'][0]['kind'] == 'possible_unavailability'
        win = snap['windows'][0]
        assert win['prediction_target'] == 'computer_activity' and win['availability'] == 'unknown' and win['action_authority'] == 'none'
        holds = [b for b in ws['proposed_plan']['blocks'] if b['kind'] == 'behavior_away']
        assert len(holds) == 1 and holds[0]['title'] == 'Possible time away'
        assert holds[0]['availability'] == 'unknown' and holds[0]['action_authority'] == 'none'
        assert not any(b['kind'] == 'behavior' for b in ws['proposed_plan']['blocks'])
        assert 'availability unknown' in daily.render(ws) and 'Likely busy' not in daily.render(ws)
        passed('CEC real inactivity bins/miner/unknown interpretation -> typed possible_unavailability -> behavior_away, never busy/free/attendance')
        ignored = daily.workspace(c.store, '2026-10-19', ZONE, now=CLOCK[0], behavior=public('2026-10-19'), ignored_windows=[win['id']])
        assert ignored['tentative_count'] == 0 and len(c.calls) == 1
        passed('Actual away correction uses stable pattern ID without analysis or source refresh')
        pin = datetime.datetime(2026, 10, 19, 14, 30, tzinfo=ZoneInfo(ZONE)).timestamp()
        task = c.store.today_save_task(dict(title='Explicit authored pin', day='2026-10-19', timezone=ZONE,
            duration_minutes=30, due_ts=None, pinned_start=pin))
        before = c.store.today_snapshot('2026-10-19', ZONE)['tasks']
        _, pinned, _ = c.read(day='2026-10-19', refresh_calendar=False)
        assert [(b['start'], b['end']) for b in pinned['proposed_plan']['blocks'] if b.get('task_id') == task['id']] == [(pin, pin+1800)]
        intervals = [(b['start'], b['end']) for b in pinned['proposed_plan']['blocks'] if b['kind'] == 'behavior_away']
        assert intervals == [(pin-1800, pin), (pin+1800, pin+5400)], intervals
        assert c.store.today_snapshot('2026-10-19', ZONE)['tasks'] == before and len(c.calls) == 1
        passed('Actual authored pin splits optional away guidance and remains unchanged')
    finally:
        c.close()
    c = inactivity_case()
    try:
        task = c.store.today_save_task(dict(title='Fitting full-day authored work', day='2026-10-19', timezone=ZONE,
            duration_minutes=9*60, due_ts=None, pinned_start=None))
        c.seed(); snap, ws, _ = c.read(day='2026-10-19')
        assert len(snap['windows']) == 1 and not ws['tentative_count']
        assert not any(t['task_id'] == task['id'] for t in ws['proposed_plan']['unscheduled']), ws['proposed_plan']['unscheduled']
        authored_blocks = [b for b in ws['proposed_plan']['blocks'] if b.get('task_id') == task['id']]
        assert len(authored_blocks) == 1 and authored_blocks[0]['end'] - authored_blocks[0]['start'] == 9*3600
        assert any(t['task_id'] != task['id'] for t in ws['proposed_plan']['unscheduled'])
        passed('Actual fitting authored full-day work takes precedence over optional computer-inactivity guidance; source suggestion correctly waits')
    finally:
        c.close()
    c = inactivity_case(interpretation='likely_occupied')
    try:
        c.seed(); snap, ws, _ = c.read(day='2026-10-19')
        assert not snap['windows'] and not ws['tentative_count'] and len(c.calls) == 1
        passed('CEC rejects semantic likely_occupied on inactivity-only history; consumer receives no busy hold')
    finally:
        c.close()
    for label, mutation in [('expired cache', lambda c: CLOCK.__setitem__(0, CLOCK[0]+301)),
            ('revoked source', lambda c: c.publish(available=False)), ('role changed', lambda c: c.role.__setitem__(0, 'changed')),
            ('consent changed', lambda c: c.runtime.call('configure_signals', {'email': False}, CLOCK[0])),
            ('stopped runtime', lambda c: c.runtime.stop())]:
        c = inactivity_case()
        try:
            c.seed(); assert public('2026-10-19')['windows']; mutation(c)
            snap = public('2026-10-19'); assert not snap['windows'] and len(c.calls) == 1
            passed('Actual away publication withheld after '+label+'; no GET inference')
        finally:
            c.close()
    event = threading.Event(); c = inactivity_case(mutation=lambda _: event.set()); c.runtime._availability_cancel.event = event
    try:
        c.seed(); assert not public('2026-10-19')['windows'] and c.runtime.local_engine().seed_index().work() == []
        passed('Cancellation during actual away interpretation prevents both saved work and away publication')
    finally:
        c.close()
    c = inactivity_case()
    try:
        authored = c.store.today_save_task(dict(title='Preserve user-authored work on Forget', day='2026-10-19', timezone=ZONE,
            duration_minutes=30, due_ts=None, pinned_start=None))
        c.seed(); assert public('2026-10-19')['windows']
        old_runtime = c.runtime
        forgotten = runtime_module.forget_all(c.store, home=c.home)
        assert not forgotten['files_failed'] and not (c.home/'behavior.db').exists() and not (c.home/'behavior_runtime.db').exists()
        try:
            old_runtime.planning_context('2026-10-19', ZONE, CLOCK[0])
        except runtime_module.RuntimeStopped:
            pass
        else:
            raise AssertionError('Retired pre-Forget runtime exported context')
        c.runtime = BehaviorRuntime(c.home, local_factory=lambda p: BehaviorEngine(p, phraser=None, clock=lambda: CLOCK[0], tz=ZONE),
            messages_fn=forbidden, contact_name_fn=forbidden, calendar_sync_fn=lambda: {}, session_db_fn=forbidden,
            behavior_interpreter=Interpreter(c.complete), role_key_fn=lambda: c.role[0], clock=lambda: CLOCK[0])
        c.runtime._planning_store = c.store; runtime_module._runtime = c.runtime
        snap = public('2026-10-19'); assert not snap['windows'] and not snap['demands'] and len(c.calls) == 1
        assert any(t['id'] == authored['id'] for t in c.store.today_snapshot('2026-10-19', ZONE)['tasks'])
        passed('Actual Forget clears private learned databases, retires old references and exports no old away/work after explicit private restart; user task retained')
    finally:
        c.close()
    assert not RESULT['unexpected_effects'], RESULT['unexpected_effects']
    modules = {}
    for name, module in list(sys.modules.items()):
        if name.startswith('service'):
            file = getattr(module, '__file__', None)
            if name not in FACADES:
                assert file and inside(file, SOURCE), (name, file)
                rel = str(Path(file).relative_to(SOURCE)); assert rel in INPUT['files']; assert hashlib.sha256(Path(file).read_bytes()).hexdigest() == INPUT['files'][rel]['sha256']
            modules[name] = file
    RESULT['actual_imported_modules'] = modules
    RESULT['result'] = 'PASS'

try:
    main()
except BaseException as exc:
    RESULT['result'] = 'FAIL'; RESULT['failure'] = {'type': type(exc).__name__, 'detail': str(exc)}
    raise
finally:
    for rel, meta in INPUT['files'].items():
        assert hashlib.sha256((SOURCE / rel).read_bytes()).hexdigest() == meta['sha256'], rel
    (ROOT / 'evidence' / 'actual-public-result.json').write_text(json.dumps(RESULT, indent=2, default=str) + '\n')
    print(json.dumps({'result': RESULT.get('result'), 'passed_cases': len(RESULT['cases']), 'unexpected_effects': RESULT['unexpected_effects'], 'root': str(ROOT)}))
'''
