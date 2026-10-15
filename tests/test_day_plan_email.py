"""Isolated daily email lifecycle with verified synthetic self identity and fake sender."""
import asyncio
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

_scratch = tempfile.TemporaryDirectory(prefix="wisp-daily-email-tests-")
os.environ["WISP_HOME"] = _scratch.name

from service.assistant.day_plan_email import EmailStore, SelfIdentity, deliver_due
from service.assistant.today import RevisionConflict

ZONE = "America/Los_Angeles"


def at(day=14, hour=7, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo(ZONE)).timestamp()


def identity(now):
    return SelfIdentity(("me@example.test",), now, True)


@pytest.fixture
def store(tmp_path):
    value = EmailStore(tmp_path / "private" / "email.sqlite3", process="test-process")
    yield value
    value.close()


def enable(store, now=None, minute=480):
    now = at() if now is None else now
    preview = store.preview(dict(recipient="me@example.test", timezone=ZONE, minute=minute),
                            "My day plan", "A complete preview", identity(now), now=now)
    return store.enable(preview["token"], identity(now), confirmed=True, now=now)


def sweep(store, now, *, result=None, compose=None, own=None, allowed=True, calls=None):
    calls = [] if calls is None else calls

    async def sender(operation, payload):
        calls.append((operation, payload))
        return result if result is not None else dict(ok=True, status="succeeded")
    value = asyncio.run(deliver_due(store, identity=own or (lambda: identity(now)),
        compose=compose or (lambda *args: dict(ready=True, subject="My day plan", body="Fresh source-backed plan")),
        sender=sender, policy_allows=lambda recipient: allowed, now=now))
    return value, calls


def test_default_off_preview_is_inert_and_requires_reviewed_enable(store):
    assert not store.settings()["enabled"]
    assert sweep(store, at(hour=8))[0] == "off"
    p = store.preview(dict(recipient="me@example.test", timezone=ZONE, minute=480), "Plan", "Body", identity(at()), now=at())
    assert not store.settings()["enabled"]
    with pytest.raises(ValueError):
        store.enable(p["token"], identity(at()), confirmed=False, now=at())


@pytest.mark.parametrize("recipient", ["someone@example.test", "me@example.test\nBcc: other@example.test", "me@example.test,other@example.test", "Me <me@example.test>"])
def test_nonself_or_multiple_recipient_cannot_enable(store, recipient):
    with pytest.raises(ValueError):
        store.preview(dict(recipient=recipient, timezone=ZONE, minute=480), "Plan", "Body", identity(at()), now=at())


def test_stale_incomplete_future_identity_and_expired_preview_failclosed(store):
    for own in [SelfIdentity(("me@example.test",), at() - 601, True),
                SelfIdentity(("me@example.test",), at(), False), SelfIdentity(("me@example.test",), at() + 1, True)]:
        with pytest.raises(ValueError):
            store.preview(dict(recipient="me@example.test", timezone=ZONE, minute=480), "Plan", "Body", own, now=at())
    p = store.preview(dict(recipient="me@example.test", timezone=ZONE, minute=480), "Plan", "Body", identity(at()), now=at())
    with pytest.raises(ValueError):
        store.enable(p["token"], identity(at() + 601), confirmed=True, now=at() + 601)


def test_daily_once_and_restart_persistence(store):
    enable(store)
    assert sweep(store, at(hour=8))[0] == "submitted"
    assert sweep(store, at(hour=8, minute=1))[1] == []
    second = EmailStore(store.path, process="restart")
    try:
        assert sweep(second, at(hour=8, minute=2))[1] == []
        assert sweep(second, at(day=15, hour=8))[0] == "submitted"
    finally:
        second.close()


def test_pause_invalidates_preview_and_preclaim_snapshot(store):
    original = enable(store)
    paused = store.pause(original["revision"])
    assert not paused["enabled"] and sweep(store, at(hour=8))[1] == []
    assert not store.claim(original, "2026-10-14", at(hour=8), "Plan", "Body", now=at(hour=8))
    with pytest.raises(RevisionConflict):
        store.pause(original["revision"])


def test_source_holdback_and_identity_loss_do_not_claim_or_send(store):
    enable(store)
    assert sweep(store, at(hour=8), compose=lambda *a: dict(ready=False))[0] == "waiting_for_sources"
    assert sweep(store, at(hour=8), own=lambda: SelfIdentity())[0] == "waiting_for_self_identity"
    assert sweep(store, at(hour=8), allowed=False)[0] == "blocked_by_policy"
    assert store.status()["last_result"] is None
    assert sweep(store, at(hour=8, minute=5))[0] == "submitted"


@pytest.mark.parametrize("result,expected", [
    (dict(ok=False, status="unknown"), "unknown"), (dict(ok=False), "unknown"),
    (dict(ok=False, status="not_attempted", dispatch_started=False), "not_attempted"),
    (dict(ok=False, status="not_attempted", dispatch_started=True), "unknown")])
def test_no_automatic_retry_of_any_claimed_attempt(store, result, expected):
    enable(store)
    assert sweep(store, at(hour=8), result=result)[0] == expected
    assert sweep(store, at(hour=8, minute=1))[1] == []
    assert store.status()["last_result"]["status"] == expected


def test_crash_after_claim_is_unknown_on_restart_and_never_resent(store):
    config = enable(store)
    assert store.claim(config, "2026-10-14", at(hour=8), "Plan", "Body", now=at(hour=8))
    same_process = EmailStore(store.path, process="test-process")
    assert same_process.status()["last_result"]["status"] == "submitting"
    same_process.close()
    second = EmailStore(store.path, process="restart")
    try:
        assert second.status()["last_result"]["status"] == "unknown"
        assert sweep(second, at(hour=8, minute=1))[1] == []
    finally:
        second.close()


def test_two_connections_have_one_claim(store):
    config = enable(store)
    second = EmailStore(store.path, process="test-process")
    try:
        assert store.claim(config, "2026-10-14", at(hour=8), "Plan", "Body", now=at(hour=8))
        assert not second.claim(config, "2026-10-14", at(hour=8), "Plan", "Body", now=at(hour=8))
    finally:
        second.close()


def test_late_start_skips_and_enabling_after_time_starts_tomorrow(store):
    enable(store)
    assert sweep(store, at(hour=9))[0] == "missed"
    assert store.status()["last_result"]["status"] == "missed"
    store.pause(store.settings()["revision"])
    assert enable(store, now=at(hour=9))["start_day"] == "2026-10-15"


def test_dst_gap_and_fold_are_skipped_without_send(store):
    # Set a controlled consent before each transition, without native state.
    reviewed = enable(store)
    for day, minute in [("2026-03-08", 150), ("2026-11-01", 90)]:
        config = reviewed | dict(minute=minute, start_day=day)
        import json
        store._db.execute("UPDATE settings SET payload=?", (json.dumps(config),))
        store._db.commit()
        now = datetime.fromisoformat(day + "T08:00:00").replace(tzinfo=ZoneInfo(ZONE)).timestamp()
        assert sweep(store, now)[0] == "missed"
        assert store.status()["last_result"]["status"] == "missed"


def test_settings_change_during_composition_prevents_dispatch(store):
    config = enable(store)
    def compose(*args):
        store.pause(config["revision"])
        return dict(ready=True, subject="Plan", body="Body")
    assert sweep(store, at(hour=8), compose=compose)[1] == []


def test_private_database_and_no_retained_email_body(store):
    enable(store)
    sweep(store, at(hour=8))
    assert store.path.stat().st_mode & 0o077 == 0
    row = dict(store._db.execute("SELECT * FROM occurrences").fetchone())
    assert "body" not in row and len(row["body_hash"]) == 64


def test_exception_after_native_boundary_is_unknown(store):
    enable(store)
    async def sender(*args):
        raise RuntimeError("synthetic interruption")
    with pytest.raises(RuntimeError):
        asyncio.run(deliver_due(store, identity=lambda: identity(at(hour=8)), compose=lambda *args: dict(
            ready=True, subject="Plan", body="Body"), sender=sender, policy_allows=lambda _: True, now=at(hour=8)))
    assert store.status()["last_result"]["status"] == "unknown"


def test_availability_correction_is_local_to_day_and_zone(store):
    store.ignore("2026-10-14", ZONE, "routine")
    assert store.ignored("2026-10-14", ZONE) == ["routine"]
    assert store.ignored("2026-10-15", ZONE) == []
    assert store.ignored("2026-10-14", "UTC") == []
    store.ignore("2026-10-14", ZONE, "routine", False)
    assert store.ignored("2026-10-14", ZONE) == []


def test_api_defaults_failclosed_and_receipts_invalidate_in_order(store, monkeypatch):
    from service.assistant import daily_workspace_api as api
    monkeypatch.setattr(api, "_identity", SelfIdentity())
    monkeypatch.setattr(api, "_identity_started", 0)
    monkeypatch.setattr(api, "_email_store", store)
    monkeypatch.setattr(api.time, "time", lambda: at())
    assert api.get_email_settings()["own_addresses"] == []
    api.publish_identity(["me@example.test"], available=True, complete=True, started_at=at(), now=at())
    assert api.get_email_settings()["own_addresses"] == ["me@example.test"]
    api.publish_identity([], available=False, complete=False, started_at=at() + 1, now=at() + 1)
    api.publish_identity(["me@example.test"], available=True, complete=True, started_at=at(), now=at() + 2)
    assert api.get_email_settings()["own_addresses"] == []


def test_http_preview_enable_pause_and_strict_validation(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service.assistant import daily_workspace_api as api
    from service.assistant import day_plan_email as delivery
    monkeypatch.setattr(delivery, "_policy_allows", lambda _: True)
    monkeypatch.setattr(api, "_email_store", store)
    monkeypatch.setattr(api, "_identity", identity(at()))
    monkeypatch.setattr(api.time, "time", lambda: at())
    monkeypatch.setattr(api, "email_contents", lambda *args: dict(subject="Plan", body="Complete proposed plan", ready=True))
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        body = dict(day="2026-10-14", recipient="me@example.test", timezone=ZONE, minute=480)
        p = client.post("/workspace/email/preview", json=body)
        assert p.status_code == 200 and p.json()["body"] == "Complete proposed plan"
        assert not store.settings()["enabled"]
        assert client.post("/workspace/email/enable", json=dict(token=p.json()["token"], confirmed="true")).status_code == 422
        enabled = client.post("/workspace/email/enable", json=dict(token=p.json()["token"], confirmed=True))
        assert enabled.status_code == 200 and enabled.json()["enabled"]
        assert client.post("/workspace/email/enable", json=dict(token=p.json()["token"], confirmed=True)).status_code == 422
        assert client.post("/workspace/email/pause", json=dict(revision=0)).status_code == 409
        assert client.post("/workspace/email/pause", json=dict(revision=enabled.json()["revision"])).status_code == 200
        assert client.post("/workspace/email/preview", json=body | dict(minute=True)).status_code == 422
        assert client.post("/workspace/email/preview", json=body | dict(cc="other@example.test")).status_code == 422


@pytest.mark.parametrize("receipt", [None, [], "ok", 123, {"ok": True, "status": "failed"}])
def test_malformed_receipt_is_immediately_durable_unknown(store, receipt):
    enable(store)
    async def sender(*args):
        return receipt
    result = asyncio.run(deliver_due(store, identity=lambda: identity(at(hour=8)), compose=lambda *args: dict(
        ready=True, subject="Plan", body="Body"), sender=sender, policy_allows=lambda _: True, now=at(hour=8)))
    assert result == "unknown" and store.status()["last_result"]["status"] == "unknown"


def test_late_sweep_preserves_successful_result(store):
    enable(store)
    assert sweep(store, at(hour=8))[0] == "submitted"
    assert sweep(store, at(hour=9))[0] == "submitted"
    assert store.status()["health"]["state"] == "submitted"


def test_preview_body_is_never_persisted(store):
    store.preview(dict(recipient="me@example.test", timezone=ZONE, minute=480), "Plan", "PRIVATE SOURCE BODY", identity(at()), now=at())
    assert "PRIVATE SOURCE BODY" not in "\n".join(store._db.iterdump())


@pytest.mark.parametrize("change", ["identity", "policy", "clock"])
def test_dispatch_rechecks_changes_during_composition(store, change):
    enable(store)
    values = dict(own=identity(at(hour=8)), allowed=True, now=at(hour=8))
    calls = []
    def compose(*args):
        if change == "identity": values["own"] = SelfIdentity()
        if change == "policy": values["allowed"] = False
        if change == "clock": values["now"] = at(hour=9)
        return dict(ready=True, subject="Plan", body="Body")
    async def sender(*args):
        calls.append(args)
        return dict(ok=True)
    result = asyncio.run(deliver_due(store, identity=lambda: values["own"], compose=compose,
        sender=sender, policy_allows=lambda _: values["allowed"], clock=lambda: values["now"]))
    assert result in {"waiting_for_self_identity", "blocked_by_policy", "delivery_window_changed"}
    assert not calls and store.status()["last_result"] is None


def test_changed_account_epoch_or_content_scope_requires_new_review(store):
    enable(store)
    assert sweep(store, at(hour=8), own=lambda: SelfIdentity(("me@example.test",), at(hour=8), True, "new-session"))[0] == "connected_accounts_changed_review_settings"
    import json
    config = store.settings()
    config["consent"]["source_scope"].append("arbitrary-source")
    store._db.execute("UPDATE settings SET payload=?", (json.dumps(config),))
    store._db.commit()
    assert sweep(store, at(hour=8))[0] == "day_plan_scope_changed_review_settings"


def test_behavior_scope_requires_new_preview_and_explicit_consent(store):
    import json
    preview = store.preview(dict(recipient="me@example.test", timezone=ZONE, minute=480),
                            "Plan", "Body", identity(at()), now=at())
    assert "behavior" in preview["consent"]["source_scope"] and not store.settings()["enabled"]
    old_preview = json.loads(store._db.execute("SELECT payload FROM previews WHERE token=?", (preview["token"],)).fetchone()[0])
    old_preview["consent"]["source_scope"].remove("behavior")
    store._db.execute("UPDATE previews SET payload=? WHERE token=?", (json.dumps(old_preview), preview["token"]))
    store._db.commit()
    with pytest.raises(ValueError, match="format changed"):
        store.enable(preview["token"], identity(at()), confirmed=True, now=at())
    assert not store.settings()["enabled"]
    current = enable(store)
    current["consent"]["source_scope"].remove("behavior")
    store._db.execute("UPDATE settings SET payload=?", (json.dumps(current),))
    store._db.commit()
    outcome, calls = sweep(store, at(hour=8))
    assert outcome == "day_plan_scope_changed_review_settings" and not calls


def test_server_capture_order_and_empty_receipts_revoke_without_client_clock(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service.assistant import daily_workspace_api as api
    monkeypatch.setattr(api, "_email_store", store)
    monkeypatch.setattr(api, "_identity", SelfIdentity())
    monkeypatch.setattr(api, "_identity_started", 0)
    monkeypatch.setattr(api, "_identity_generation", 0)
    monkeypatch.setattr(api, "_identity_settled", False)
    monkeypatch.setattr(api, "_identity_previous", ())
    monkeypatch.setattr(api.time, "time", lambda: at())
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        old = client.post("/workspace/email/identity/begin").json()
        latest = client.post("/workspace/email/identity/begin").json()
        success = dict(addresses=["me@example.test"], available=True, complete=True)
        assert client.post("/workspace/email/identity", json=old | success).json()["superseded"]
        assert client.post("/workspace/email/identity", json=latest | success).status_code == 200
        assert api.get_email_settings()["own_addresses"] == ["me@example.test"]
        assert client.post("/workspace/email/identity", json=latest | dict(addresses=["malformed"], available=False, complete=False)).status_code == 200
        assert client.post("/workspace/email/identity", json=latest | success).status_code == 200
        assert api.get_email_settings()["own_addresses"] == []
        assert client.post("/workspace/email/identity", json=latest | success | dict(started_at=at())).status_code == 422


@pytest.mark.parametrize("blocked", ["policy", "paused", "identity", "reconnect", "body", "recipient"])
def test_final_native_admission_rechecks_all_host_boundaries(store, monkeypatch, blocked):
    from types import SimpleNamespace
    from service.assistant import day_plan_email as delivery, daily_workspace_api as api, outbox
    import importlib
    hub = importlib.import_module("service.assistant.hub")
    config = enable(store)
    store.claim(config, "2026-10-14", at(hour=8), "Plan", "Body", now=at(hour=8))
    monkeypatch.setattr(api, "email_store", lambda: store)
    monkeypatch.setattr(api, "self_identity", lambda: SelfIdentity() if blocked == "identity" else identity(at(hour=8)))
    monkeypatch.setattr(delivery.time, "time", lambda: at(hour=8))
    monkeypatch.setattr(delivery, "_policy_allows", lambda _: blocked != "policy")
    monkeypatch.setattr(hub, "hub", SimpleNamespace(has_subscribers=blocked != "reconnect"))
    calls = []
    async def native(*args):
        calls.append(args)
        return dict(ok=True, status="succeeded")
    monkeypatch.setattr(outbox, "request", native)
    if blocked == "paused": store.pause(config["revision"])
    payload = dict(to=["other@example.test" if blocked == "recipient" else "me@example.test"], cc=[], subject="Plan", body="Changed" if blocked == "body" else "Body")
    result = asyncio.run(delivery._approved_native_submission("send_email", payload))
    assert not calls and result == dict(ok=False, status="not_attempted", dispatch_started=False)


def test_exact_reviewed_daily_scope_can_use_existing_fake_bridge_once(store, monkeypatch):
    from types import SimpleNamespace
    from service.assistant import day_plan_email as delivery, daily_workspace_api as api, outbox
    import importlib
    hub = importlib.import_module("service.assistant.hub")
    from service import safety
    from service.safety import policy
    monkeypatch.setattr(api, "email_store", lambda: store)
    monkeypatch.setattr(api, "self_identity", lambda: identity(at(hour=8)))
    monkeypatch.setattr(delivery.time, "time", lambda: at(hour=8))
    monkeypatch.setattr(hub, "hub", SimpleNamespace(has_subscribers=True))
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(safety.grants, "check", lambda *args: None)
    from service.inference import remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)
    calls = []
    async def native(operation, payload):
        calls.append((operation, payload))
        return dict(ok=True, status="succeeded")
    monkeypatch.setattr(outbox, "request", native)
    enable(store)
    for _ in range(2):
        asyncio.run(deliver_due(store, identity=api.self_identity, compose=lambda *args: dict(ready=True, subject="Plan", body="Body"),
            sender=delivery._approved_native_submission, policy_allows=delivery._policy_allows, now=at(hour=8)))
    assert len(calls) == 1 and calls[0][1] == dict(to=["me@example.test"], cc=[], subject="Plan", body="Body")
    monkeypatch.setattr(policy, "_READ_ONLY", True)
    assert not delivery._policy_allows("me@example.test")
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(safety.grants, "check", lambda *args: "deny")
    assert not delivery._policy_allows("me@example.test")
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: "synthetic-remote")
    monkeypatch.setattr(remote_policy, "remote_tool_allowed", lambda _: False)
    assert not delivery._policy_allows("me@example.test")


@pytest.mark.parametrize("case", ["namespace", "string", "mapping", "missing", "unknown", "malformed_decision", "deny", "allow", "confirm"])
def test_policy_admission_requires_real_decision_and_enum(monkeypatch, case):
    from types import SimpleNamespace
    from service import safety
    from service.assistant import day_plan_email as delivery
    values = dict(namespace=SimpleNamespace(tier="confirm"), string="confirm", mapping={"tier": "confirm"},
        missing=SimpleNamespace(), unknown=SimpleNamespace(tier="unexpected"),
        malformed_decision=safety.Decision("confirm", "not a Tier"), deny=safety.Decision(safety.Tier.DENY, "denied"),
        allow=safety.Decision(safety.Tier.ALLOW, "allowed"), confirm=safety.Decision(safety.Tier.CONFIRM, "review required"))
    monkeypatch.setattr(safety, "decide", lambda *args, **kwargs: values[case])
    assert delivery._policy_allows("me@example.test") is (case in {"allow", "confirm"})


@pytest.mark.parametrize("path", ["/email/identity/begin", "/email/identity", "/email/preview", "/email/enable", "/email/pause", "/availability"])
@pytest.mark.parametrize("headers", [
    {"Origin": "https://untrusted.example"}, {"Origin": "null"},
    {"Host": "untrusted.example", "Origin": "http://untrusted.example"},
    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
    {"Host": "localhost.untrusted.example:8765", "Origin": "http://localhost.untrusted.example:8765"},
])
def test_workspace_browser_rejection_has_no_identity_settings_or_delivery_effect(store, monkeypatch, path, headers):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service.assistant import daily_workspace_api as api, day_plan_email as delivery
    config = enable(store)
    own = identity(at())
    preview = store.preview(dict(recipient="me@example.test", timezone=ZONE, minute=480), "Plan", "Body", own, now=at())
    for name, value in {"_email_store": store, "_identity": own, "_identity_generation": 1,
        "_identity_session": "synthetic-capture-session", "_identity_capture_at": at(), "_identity_started": 0,
        "_identity_settled": False, "_identity_previous": ("me@example.test",)}.items():
        monkeypatch.setattr(api, name, value)
    monkeypatch.setattr(api.time, "time", lambda: at())
    effects = []
    def compose(*args):
        effects.append("compose")
        return dict(subject="Plan", body="Body", ready=True)
    async def sender(*args):
        effects.append("send")
        return dict(ok=True)
    monkeypatch.setattr(api, "email_contents", compose)
    monkeypatch.setattr(delivery, "_approved_native_submission", sender)
    monkeypatch.setattr(delivery, "_policy_allows", lambda _: True)
    bodies = {"/email/identity": dict(session="synthetic-capture-session", generation=1, addresses=[], available=False, complete=False),
        "/email/preview": dict(day="2026-10-14", recipient="me@example.test", timezone=ZONE, minute=480),
        "/email/enable": dict(token=preview["token"], confirmed=True), "/email/pause": dict(revision=config["revision"]),
        "/availability": dict(day="2026-10-14", timezone=ZONE, window="synthetic-window")}
    app = FastAPI(); app.include_router(api.router)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        kwargs = dict(json=bodies[path]) if path in bodies else dict(content=b"", headers=headers | {"Content-Type": "text/plain"})
        if path in bodies:
            kwargs["headers"] = headers
        result = client.post("/workspace" + path, **kwargs)
    assert result.status_code == 403
    assert api._identity == own and api._identity_generation == 1
    assert store.settings() == config and not store.ignored("2026-10-14", ZONE)
    assert store._db.execute("SELECT COUNT(*) FROM previews").fetchone()[0] == 1
    assert not effects and store.status()["last_result"] is None


@pytest.mark.parametrize("headers,peer", [
    ({"Host": "127.0.0.1:9999", "Origin": "http://127.0.0.1:9999"}, "127.0.0.1"),
    ({"Origin": "http://127.0.0.1:8765 http://untrusted.example"}, "127.0.0.1"),
    ({"Origin": ""}, "127.0.0.1"), ({}, "192.0.2.1"),
    ({"Sec-Fetch-Site": "unexpected"}, "127.0.0.1"),
])
def test_workspace_rejects_untrusted_transport_metadata_before_capture(monkeypatch, headers, peer):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service.assistant import daily_workspace_api as api
    own = identity(at())
    monkeypatch.setattr(api, "_identity", own)
    monkeypatch.setattr(api, "_identity_generation", 1)
    app = FastAPI(); app.include_router(api.router)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=(peer, 50000)) as client:
        result = client.post("/workspace/email/identity/begin", headers=headers)
    assert result.status_code == 403 and api._identity == own and api._identity_generation == 1


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765"])
@pytest.mark.parametrize("browser", [False, True])
def test_workspace_accepts_native_and_same_origin_loopback_capture(monkeypatch, host, browser):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service.assistant import daily_workspace_api as api
    monkeypatch.setattr(api, "_identity", identity(at()))
    monkeypatch.setattr(api, "_identity_generation", 1)
    monkeypatch.setattr(api, "_identity_capture_at", 0)
    monkeypatch.setattr(api, "_identity_settled", True)
    app = FastAPI(); app.include_router(api.router)
    headers = {"Host": host}
    if browser:
        headers.update(Origin="http://" + host, **{"Sec-Fetch-Site": "same-origin"})
    # This TestClient release cannot parse IPv6 base URLs. Exercise the actual
    # IPv6 Host and peer in ASGI metadata without using its URL parser.
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("::1" if host.startswith("[") else "127.0.0.1", 50000)) as client:
        result = client.post("/workspace/email/identity/begin", headers=headers)
    assert result.status_code == 200 and api._identity_generation == 2 and not api._identity.complete


def test_workspace_rejects_duplicate_browser_security_headers(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service.assistant import daily_workspace_api as api
    own = identity(at())
    monkeypatch.setattr(api, "_identity", own)
    monkeypatch.setattr(api, "_identity_generation", 1)
    app = FastAPI(); app.include_router(api.router)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        for key, value in [("Host", "127.0.0.1:8765"), ("Origin", "http://127.0.0.1:8765"), ("Sec-Fetch-Site", "same-origin")]:
            assert client.post("/workspace/email/identity/begin", headers=[(key, value), (key, value)]).status_code == 403
    assert api._identity == own and api._identity_generation == 1


def test_malformed_policy_has_zero_fake_bridge_effects(store, monkeypatch):
    import importlib
    from types import SimpleNamespace
    from service import safety
    from service.assistant import day_plan_email as delivery, daily_workspace_api as api, outbox
    hub = importlib.import_module("service.assistant.hub")
    config = enable(store)
    store.claim(config, "2026-10-14", at(hour=8), "Plan", "Body", now=at(hour=8))
    monkeypatch.setattr(api, "email_store", lambda: store)
    monkeypatch.setattr(api, "self_identity", lambda: identity(at(hour=8)))
    monkeypatch.setattr(delivery.time, "time", lambda: at(hour=8))
    monkeypatch.setattr(hub, "hub", SimpleNamespace(has_subscribers=True))
    monkeypatch.setattr(safety, "decide", lambda *args, **kwargs: SimpleNamespace(tier="confirm"))
    calls = []
    async def native(*args):
        calls.append(args)
        return dict(ok=True)
    monkeypatch.setattr(outbox, "request", native)
    result = asyncio.run(delivery._approved_native_submission("send_email", dict(to=["me@example.test"], cc=[], subject="Plan", body="Body")))
    assert not calls and result["status"] == "not_attempted"
