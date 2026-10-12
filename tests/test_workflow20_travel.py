"""Arrival planner contract tests: synthetic providers, no location/native/HTTP."""
import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest

from service.tools import maps_travel as travel


NOW = datetime(2026, 10, 12, 20, tzinfo=timezone.utc)
DEADLINE = "2026-10-12T23:00:00-07:00"
ZONE = "America/Los_Angeles"
HOME = "200 Oak Street, San Jose, CA"


@pytest.fixture(autouse=True)
def isolated_provider_clock_and_no_external_calls(monkeypatch):
    monkeypatch.setattr(travel, "_arrival_now", lambda: NOW)
    async def deny(*args, **kwargs):
        pytest.fail("arrival tests may not use HTTP or native providers")
    monkeypatch.setattr(travel, "_get", deny)
    monkeypatch.setattr(travel, "_geocode", deny)


def provider_result(request):
    target = datetime.fromisoformat(request["arrival_at"])
    current = request["origin"] == "current_location"
    return {
        "status": "ok", "request_id": request["request_id"],
        "provider": "apple_mapkit_eta", "traffic_basis": "expected_traffic",
        "observed_at": NOW.isoformat(), "valid_until": (NOW + timedelta(minutes=5)).isoformat(),
        "duration_seconds": 45 * 60,
        "expected_arrival": target.isoformat(),
        "expected_departure": (target - timedelta(minutes=45)).isoformat(),
        "origin": {"requested": request["origin"], "resolved": "Measured current location" if current else request["origin"],
                   "latitude": 37.33, "longitude": -121.89,
                   "source": "core_location" if current else "mapkit_search",
                   "observed_at": NOW.isoformat(), "accuracy_m": 10},
        "destination": {"requested": request["destination"],
                        "resolved": "San Francisco International Airport" if request["destination"] == "SFO" else request["destination"],
                        "latitude": 37.6213, "longitude": -122.3790, "source": "mapkit_search"},
    }


def fake_provider(monkeypatch, mutate=lambda result: None):
    calls = []
    async def provider(request):
        calls.append(request)
        result = provider_result(request)
        mutate(result)
        return result
    monkeypatch.setattr(travel, "_arrival_provider", provider)
    return calls


def run(**kwargs):
    return asyncio.run(travel.route_to_arrive("SFO", DEADLINE, ZONE, **kwargs))


def test_current_location_and_expected_traffic_produce_a_plan_without_opening_maps(monkeypatch):
    calls = fake_provider(monkeypatch)
    result = run(buffer_minutes=30)
    assert len(calls) == 1
    request = calls[0]
    assert request["origin"] == "current_location" and request["destination"] == "SFO"
    assert request["arrival_at"] == "2026-10-13T05:30:00+00:00"
    assert "leave by 2026-10-12 21:45 PDT" in result
    assert "planned arrival 2026-10-12 22:30 PDT" in result
    assert "2026-10-12 23:00 PDT" in result and "30 min buffer" in result
    assert "Apple Maps expected-traffic estimate: 45 min; lookup age 0s" in result
    assert "not a guaranteed arrival" in result and "Opened" not in result
    link = result.split("](", 1)[1].removesuffix(")")
    query = parse_qs(urlparse(link).query)
    assert query == {"saddr": ["37.33,-121.89"], "daddr": ["37.6213,-122.379"], "dirflg": ["d"]}


def test_explicit_origin_is_bound_and_not_replaced_by_current_location(monkeypatch):
    calls = fake_provider(monkeypatch)
    result = run(origin=HOME)
    assert calls[0]["origin"] == HOME
    assert HOME in result and "leave by" in result


@pytest.mark.parametrize("origin", ["", HOME])
def test_default_native_provider_is_explicitly_unavailable(origin):
    result = run(origin=origin)
    assert "unavailable" in result and "no departure estimate" in result
    assert "leave by" not in result and "maps.apple.com" not in result
    assert ("starting address" in result) == (not origin)


@pytest.mark.parametrize("destination", ["", "airport", "the airport", "home", "here", "there"])
def test_ambiguous_destination_does_not_reach_provider(destination, monkeypatch):
    async def fail(request):
        pytest.fail("ambiguous destination reached provider")
    monkeypatch.setattr(travel, "_arrival_provider", fail)
    assert "ask the user" in asyncio.run(travel.route_to_arrive(destination, DEADLINE, ZONE))


@pytest.mark.parametrize("origin", ["home", "my home", "here", "office", "current_location"])
def test_ambiguous_origin_does_not_become_a_guessed_address(origin, monkeypatch):
    async def fail(request):
        pytest.fail("ambiguous origin reached provider")
    monkeypatch.setattr(travel, "_arrival_provider", fail)
    assert "ask the user" in run(origin=origin)


@pytest.mark.parametrize("deadline,zone", [
    ("11", ZONE), ("11 PM tonight", ZONE), ("2026-10-12", ZONE),
    (DEADLINE, ""), (DEADLINE, "not/a/timezone"),
    ("2026-10-12T11:00:00-07:00", ZONE), ("2026-10-21T23:00:00-07:00", ZONE),
    ("2026-10-12T23:00:00-04:00", ZONE), ("2026-02-30T23:00:00Z", ZONE),
])
def test_invalid_ambiguous_past_or_excessive_deadline_abstains(deadline, zone, monkeypatch):
    async def fail(request):
        pytest.fail("invalid deadline reached provider")
    monkeypatch.setattr(travel, "_arrival_provider", fail)
    assert "clarify" in asyncio.run(travel.route_to_arrive("SFO", deadline, zone))


@pytest.mark.parametrize("buffer", [-1, 181, True, 1.5, "30"])
def test_invalid_buffers_abstain(buffer, monkeypatch):
    async def fail(request):
        pytest.fail("invalid buffer reached provider")
    monkeypatch.setattr(travel, "_arrival_provider", fail)
    assert "clarify" in run(buffer_minutes=buffer)


def test_buffer_cannot_push_arrival_target_into_the_past(monkeypatch):
    calls = fake_provider(monkeypatch)
    result = asyncio.run(travel.route_to_arrive("SFO", "2026-10-12T13:15:00-07:00", ZONE, buffer_minutes=30))
    assert "past" in result and not calls


def test_unambiguous_local_iso_uses_explicit_iana_timezone(monkeypatch):
    calls = fake_provider(monkeypatch)
    result = asyncio.run(travel.route_to_arrive("SFO", "2026-10-12T23:00:00", ZONE))
    assert calls[0]["arrival_at"] == "2026-10-13T06:00:00+00:00" and "23:00 PDT" in result


@pytest.mark.parametrize("value", ["2026-03-08T02:30:00", "2026-11-01T01:30:00",
                                     "2026-03-08T02:30:00-08:00"])
def test_dst_gap_fold_or_nonexistent_local_offset_is_rejected(value):
    with pytest.raises(ValueError):
        travel._arrival_time(value, ZoneInfo(ZONE))


@pytest.mark.parametrize("value,utc_hour", [("2026-11-01T01:30:00-07:00", 8),
                                             ("2026-11-01T01:30:00-08:00", 9)])
def test_explicit_offset_disambiguates_dst_fold(value, utc_hour):
    assert travel._arrival_time(value, ZoneInfo(ZONE)).hour == utc_hour


@pytest.mark.parametrize("field,value", [
    ("request_id", "wrong-request"), ("provider", "osrm"),
    ("traffic_basis", "free_flow"), ("traffic_basis", "unknown"),
    ("traffic_basis", True), ("duration_seconds", True),
    ("duration_seconds", float("nan")), ("duration_seconds", float("inf")),
    ("duration_seconds", 0), ("duration_seconds", -1), ("duration_seconds", 1000000),
    ("duration_seconds", 1), ("expected_arrival", "2026-10-13T06:15:00Z"),
    ("expected_departure", "2026-10-13T04:00:00Z"),
    ("observed_at", (NOW - timedelta(minutes=6)).isoformat()),
    ("observed_at", (NOW + timedelta(seconds=1)).isoformat()),
    ("valid_until", NOW.isoformat()), ("valid_until", "invalid"),
])
def test_wrong_unsupported_stale_or_inconsistent_provider_evidence_is_rejected(field, value, monkeypatch):
    fake_provider(monkeypatch, lambda result: result.update({field: value}))
    result = run()
    assert "could not be verified" in result and "leave by" not in result and "maps.apple.com" not in result


@pytest.mark.parametrize("endpoint,field,value", [
    ("origin", "requested", "Guessed City"), ("destination", "requested", "OAK"),
    ("destination", "resolved", "Oakland International Airport"),
    ("destination", "resolved", "San Francisco"), ("destination", "source", "model"),
    ("origin", "source", "saved_home"), ("origin", "source", "ip_address"),
    ("origin", "observed_at", (NOW - timedelta(minutes=3)).isoformat()),
    ("origin", "accuracy_m", 251), ("origin", "accuracy_m", -1),
    ("origin", "accuracy_m", True), ("origin", "latitude", 91),
    ("origin", "longitude", -181), ("origin", "latitude", float("nan")),
    ("destination", "longitude", float("inf")), ("destination", "latitude", True),
])
def test_unverified_wrong_location_or_bad_coordinate_never_yields_maps_link(endpoint, field, value, monkeypatch):
    fake_provider(monkeypatch, lambda result: result[endpoint].update({field: value}))
    result = run()
    assert "could not be verified" in result and "maps.apple.com" not in result


def test_explicit_origin_cannot_resolve_to_a_different_city(monkeypatch):
    fake_provider(monkeypatch, lambda result: result["origin"].update({"resolved": "200 Oak Street, San Francisco, CA"}))
    assert "could not be verified" in run(origin=HOME)


@pytest.mark.parametrize("status", ["denied", "unavailable", "partial", "stale", "failed", "unknown"])
def test_non_success_source_status_is_not_a_completed_plan(status, monkeypatch):
    fake_provider(monkeypatch, lambda result: result.update({"status": status}))
    result = run()
    assert "unavailable" in result and "starting address" in result and "leave by" not in result


@pytest.mark.parametrize("payload", [None, [], "success", {"status": "ok"}])
def test_malformed_provider_response_cannot_be_a_plan(payload, monkeypatch):
    async def provider(request):
        return payload
    monkeypatch.setattr(travel, "_arrival_provider", provider)
    result = run()
    assert "leave by" not in result and "maps.apple.com" not in result


def test_provider_exception_is_an_honest_failure(monkeypatch):
    async def provider(request):
        raise RuntimeError("private provider details")
    monkeypatch.setattr(travel, "_arrival_provider", provider)
    result = run()
    assert "unavailable" in result and "private provider details" not in result


def test_provider_cannot_rebind_the_original_request_identifier(monkeypatch):
    async def provider(request):
        request["request_id"] = "other-request"
        return provider_result(request)
    monkeypatch.setattr(travel, "_arrival_provider", provider)
    assert "could not be verified" in run()


def test_provider_timeout_cancels_work_without_recommending_departure(monkeypatch):
    cancelled = []
    async def provider(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    monkeypatch.setattr(travel, "_arrival_provider", provider)
    monkeypatch.setattr(travel, "_TIMEOUT", 0.001)
    result = run()
    assert cancelled == [True] and "unavailable" in result and "leave by" not in result


def test_past_required_departure_is_not_recommended(monkeypatch):
    fake_provider(monkeypatch)
    result = asyncio.run(travel.route_to_arrive("SFO", "2026-10-12T13:30:00-07:00", ZONE))
    assert "required departure is already in the past" in result and "leave by" not in result


def test_osrm_estimate_remains_explicitly_without_live_traffic(monkeypatch):
    async def geocode(place):
        return (37.33, -121.89, place)
    async def get(url, **kwargs):
        assert "project-osrm.org" in url
        return {"routes": [{"distance": 16093.44, "duration": 1800}]}
    monkeypatch.setattr(travel, "_geocode", geocode)
    monkeypatch.setattr(travel, "_get", get)
    result = asyncio.run(travel.travel_time(HOME, "SFO"))
    assert "free-flow estimate" in result and "live traffic not included" in result
