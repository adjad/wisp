"""RemoteEngine <-> brain_server round trip (FastAPI TestClient as transport),
error mapping, and the offline profile-card cache."""
from __future__ import annotations

import inspect
import os
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

from brain_server.app import create_app
from service.behavior.client import BrainAuthError, BrainUnavailable, RemoteEngine
from service.behavior.engine import BehaviorEngine
from service.behavior.types import Feedback, Nudge, Observation, Pattern, Prediction

from tests.test_brain_server import TOKEN, FakeEngine

PUBLIC = ("observe", "observe_snapshot", "mine", "patterns", "set_pattern_status", "predict",
          "generate_nudges", "nudges_since", "feedback", "profile_card", "describe", "status")


@pytest.fixture
def fake():
    return FakeEngine()


@pytest.fixture
def remote(fake, tmp_path):
    app = create_app(tmp_path / "brain.db", TOKEN, engine_factory=lambda: fake, background=False)
    with TestClient(app) as c:
        yield RemoteEngine("http://127.0.0.1:8790", TOKEN, client=c, cache_path=tmp_path / "card.txt")


def test_same_public_methods_and_signatures_as_engine():
    for name in PUBLIC:
        assert callable(getattr(RemoteEngine, name)), name
        eng = inspect.signature(getattr(BehaviorEngine, name))
        rem = inspect.signature(getattr(RemoteEngine, name))
        assert list(eng.parameters) == list(rem.parameters), name
        for pname, p in eng.parameters.items():
            assert rem.parameters[pname].kind == p.kind, (name, pname)
            assert rem.parameters[pname].default == p.default, (name, pname)


def test_round_trip_all_methods(remote, fake):
    obs = [Observation(id=f"cal:g:{i}", kind="event", ts=1000.0 + i, subject="Gym", source="demo",
                       end_ts=2000.0 + i, source_id="g", attrs={"i": i}) for i in range(3)]
    assert remote.observe(obs) == 3
    assert fake.obs["cal:g:2"] == obs[2] and fake.obs["cal:g:2"].attrs == {"i": 2}

    snap = remote.observe_snapshot("calendar", obs[:1], window_start=1.0, window_end=2.0, taken_at=1.5)
    assert snap == {"upserted": 1, "cancelled": 1, "moved": 0}
    assert ("observe_snapshot", "calendar", 1.0, 2.0, 1.5) in fake.calls

    mined = remote.mine(now=123.0)
    assert mined == [fake._pattern()] and isinstance(mined[0], Pattern)
    assert mined[0].window.weekdays == (0, 1, 2, 3, 4) and mined[0].evidence == ("cal:a:1", "cal:b:2")
    assert ("mine", 123.0) in fake.calls

    assert remote.patterns(status="active", kind="habit") == [fake._pattern()]
    assert ("patterns", "active", "habit") in fake.calls

    p = remote.set_pattern_status("p1", "confirmed")
    assert p.status == "confirmed"

    preds = remote.predict(now=5.0, horizon_s=3600.0)
    assert isinstance(preds[0], Prediction) and preds[0].refs == ("cal:a:1",)
    assert ("predict", 5.0, 3600.0) in fake.calls
    remote.predict(now=6.0)               # no horizon -> omitted -> engine default
    assert ("predict", 6.0, None) in fake.calls

    created = remote.generate_nudges(now=10.0)
    assert isinstance(created[0], Nudge) and created[0] == fake.nudge_log[0]
    assert created[0].actions[0].params == {"title": "Gym", "when_ts": 5.0}
    remote.generate_nudges(now=11.0)
    items, cursor = remote.nudges_since(1)
    assert [n.id for n in items] == ["n2"] and cursor == 2
    items, cursor = remote.nudges_since(cursor)
    assert items == [] and cursor == 2

    fb = Feedback(nudge_id="n1", verdict="useful", ts=42.0)
    assert remote.feedback(fb) is None
    assert fake.feedbacks == [fb]

    assert remote.profile_card(max_bytes=20) == "Gym weekday mornings"
    assert remote.describe("gym")["summary"] == "about gym"
    st = remote.status()
    assert st["patterns"] == 1 and "brain_server" in st


def test_errors_map_to_python_exceptions(remote):
    with pytest.raises(KeyError):
        remote.set_pattern_status("nope", "active")
    with pytest.raises(ValueError):
        remote.set_pattern_status("p1", "maybe")
    with pytest.raises(ValueError):
        remote.feedback(Feedback(nudge_id="n1", verdict="meh", ts=1.0))


def test_wrong_token_raises_auth_error(fake, tmp_path):
    app = create_app(tmp_path / "b.db", TOKEN, engine_factory=lambda: fake, background=False)
    with TestClient(app) as c:
        bad = RemoteEngine("http://127.0.0.1:8790", "x" * 40, client=c)
        with pytest.raises(BrainAuthError):
            bad.status()
        assert isinstance(BrainAuthError("x"), BrainUnavailable)
        assert bad.healthy() is True  # healthz needs no token


def test_transport_errors_and_5xx_raise_unavailable():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    r = RemoteEngine("https://gx10.example.ts.net:8443", TOKEN, transport=httpx.MockTransport(refuse))
    for call in (lambda: r.status(), lambda: r.observe([]), lambda: r.nudges_since(0)):
        with pytest.raises(BrainUnavailable):
            call()
    assert r.healthy() is False

    r5 = RemoteEngine("https://gx10.example.ts.net:8443", TOKEN,
                      transport=httpx.MockTransport(lambda req: httpx.Response(502, text="bad gateway")))
    with pytest.raises(BrainUnavailable):
        r5.predict()


def test_profile_card_cached_for_offline_use(tmp_path):
    state = {"up": True}

    def handler(request):
        if not state["up"]:
            raise httpx.ConnectTimeout("down", request=request)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, json={"text": "Gym weekday mornings\nSam afternoons\nLunch 12-1"})

    cache = tmp_path / "sub" / "card.txt"
    r = RemoteEngine("https://gx10.example.ts.net:8443", lambda: TOKEN, cache_path=cache,
                     transport=httpx.MockTransport(handler))
    assert r.profile_card() == "Gym weekday mornings\nSam afternoons\nLunch 12-1"
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600
    state["up"] = False
    assert r.profile_card() == "Gym weekday mornings\nSam afternoons\nLunch 12-1"
    assert r.profile_card(max_bytes=35) == "Gym weekday mornings\nSam afternoons"
    # a fresh client (e.g. after an app restart) reads the persisted card
    r2 = RemoteEngine("https://gx10.example.ts.net:8443", TOKEN, cache_path=cache,
                      transport=httpx.MockTransport(handler))
    assert r2.profile_card() == "Gym weekday mornings\nSam afternoons\nLunch 12-1"
    # nothing cached and unreachable -> empty card (never blocks the prompt)
    r3 = RemoteEngine("https://gx10.example.ts.net:8443", TOKEN, transport=httpx.MockTransport(handler))
    assert r3.profile_card() == ""


def test_url_and_token_validation():
    with pytest.raises(ValueError):
        RemoteEngine("http://gx10.example.ts.net:8443", TOKEN)  # plain http off-loopback
    RemoteEngine("http://127.0.0.1:8790", TOKEN)
    with pytest.raises(ValueError):
        RemoteEngine("ftp://x", TOKEN)
    r = RemoteEngine("http://127.0.0.1:8790", "",
                     transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})))
    with pytest.raises(BrainAuthError):
        r.status()


def test_no_redirect_following_keeps_token_on_origin():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/v1/status"})

    r = RemoteEngine("https://gx10.example.ts.net:8443", TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(BrainUnavailable):
        r.status()
    assert seen == ["https://gx10.example.ts.net:8443/v1/status"]
