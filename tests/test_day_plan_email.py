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


@pytest.mark.parametrize("begin_age,end_age,observation,failed,overlap,continuous", [
    (540, 590, "none", False, False, True),
    (600, 600, "none", False, False, True),
    (660, 660, "none", False, False, False),
    (660, 660, "before", False, False, False),
    (540, 660, "none", False, False, False),
    (540, 660, "pending", False, False, False),
    (660, 720, "pending", False, False, False),
    (540, 660, "none", False, True, False),
    (540, 660, "none", True, False, False),
    (540, 1141, "none", False, False, False),
    (540, 539, "none", False, False, False),
])
def test_capture_refresh_preserves_consent_only_without_identity_expiry(
        store, monkeypatch, begin_age, end_age, observation, failed, overlap, continuous):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service.assistant import daily_workspace_api as api, day_plan_email as delivery
    base = at(hour=7, minute=50)
    clock = [base]
    for name, value in {"_email_store": store, "_identity": SelfIdentity(), "_identity_started": 0,
        "_identity_generation": 0, "_identity_capture_at": 0, "_identity_epoch": "synthetic-epoch",
        "_identity_previous": (), "_identity_settled": False}.items():
        monkeypatch.setattr(api, name, value)
    monkeypatch.setattr(api.time, "time", lambda: clock[0])
    monkeypatch.setattr(delivery, "_policy_allows", lambda _: True)
    monkeypatch.setattr(api, "email_contents", lambda *args: dict(subject="Plan", body="Synthetic plan", ready=True))
    app = FastAPI(); app.include_router(api.router)
    success = dict(addresses=["me@example.test"], available=True, complete=True)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        def begin():
            result = client.post("/workspace/email/identity/begin")
            assert result.status_code == 200
            return result.json()
        def complete(capture, values=success):
            return client.post("/workspace/email/identity", json=capture | values)
        assert complete(begin()).status_code == 200
        original_epoch = api.self_identity().epoch
        settings = dict(day="2026-10-14", recipient="me@example.test", timezone=ZONE, minute=480)
        preview = client.post("/workspace/email/preview", json=settings).json()
        assert client.post("/workspace/email/enable", json=dict(token=preview["token"], confirmed=True)).status_code == 200
        clock[0] = base + begin_age
        if observation == "before":
            assert not api.get_email_settings()["consent_current"]
        capture = begin()
        if overlap:
            older, capture = capture, begin()
            assert complete(older).json() == dict(ok=False, superseded=True)
            assert not api.self_identity().complete
        clock[0] = base + end_age
        if observation == "pending":
            assert not api.get_email_settings()["consent_current"]
        response = complete(capture, dict(addresses=[], available=False, complete=False) if failed else success)
        invalid_capture = end_age - begin_age > 600 or end_age < begin_age
        assert response.status_code == (422 if invalid_capture else 200)
        if failed or invalid_capture:
            assert not api.self_identity().complete
            clock[0] = base + max(begin_age, end_age) + 1
            assert complete(begin()).status_code == 200
        assert api.get_email_settings()["consent_current"] is continuous
        assert (api.self_identity().epoch == original_epoch) is continuous
        clock[0] = max(clock[0], base + 600)
        calls = []
        async def sender(*args):
            calls.append(args)
            return dict(ok=True, status="succeeded")
        result = asyncio.run(deliver_due(store, identity=api.self_identity, compose=api.email_contents,
            sender=sender, policy_allows=lambda _: True, clock=lambda: clock[0]))
        assert len(calls) == int(continuous)
        assert result == ("submitted" if continuous else "connected_accounts_changed_review_settings")
        if not continuous:
            # A fresh identity restores eligibility only after another review.
            preview = client.post("/workspace/email/preview", json=settings).json()
            assert client.post("/workspace/email/enable", json=dict(token=preview["token"], confirmed=True)).status_code == 200
            assert api.get_email_settings()["consent_current"]


def test_direct_identity_replacement_checks_expiry_without_status_read(monkeypatch):
    from service.assistant import daily_workspace_api as api
    base = at()
    for name, value in {"_identity": SelfIdentity(), "_identity_started": 0,
        "_identity_epoch": "synthetic-epoch", "_identity_previous": (), "_identity_settled": False}.items():
        monkeypatch.setattr(api, name, value)
    api.publish_identity(["me@example.test"], available=True, complete=True, started_at=base, now=base)
    epoch = api._identity.epoch
    api.publish_identity(["me@example.test"], available=True, complete=True, started_at=base + 601, now=base + 601)
    assert api._identity.epoch != epoch


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


@pytest.fixture(scope="module")
def native_identity_flows(tmp_path_factory):
    """Execute exact production Swift bodies with recording-only collaborators."""
    import json
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    scratch = tmp_path_factory.mktemp("native-identity-flow")
    source = (root / "app/Sources/WispApp/MailReader.swift").read_text()
    flow = source[source.index("    func sync() {"):source.index("    func syncRaw() {")]
    posts = source[source.index("    private func postEmptyAccounts("):source.rindex("\n}")]
    transport = (root / "app/Sources/WispApp/MailHistoryMerge.swift").read_text()
    transport = transport[transport.index("enum MailHistoryTransport {"):transport.index("/// Serializes local")]
    fixture = (root / "tests/NativeMailIdentityFlowChecks.swift").read_text()
    capture_order = source[source.index("private final class MailIdentityCaptureOrder {"):source.index("// Reads recent Mail.app inbox headers.")]
    fixture = fixture.replace("// WISP_NATIVE_CAPTURE_ORDER", capture_order).replace("// WISP_NATIVE_TRANSPORT", transport).replace(
        "// WISP_NATIVE_READER_METHODS", flow + posts)
    generated = scratch / "IdentityFlow.swift"
    generated.write_text(fixture)
    executable = scratch / "identity-flow"
    compiled = subprocess.run(["swiftc", "-parse-as-library", "-swift-version", "5",
        "-module-cache-path", str(scratch / "module-cache"), str(generated), "-o", str(executable)],
        capture_output=True, text=True, timeout=90)
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return {row["case"]: row for row in json.loads(result.stdout)}


@pytest.mark.parametrize("case", ["success", "empty", "duplicate", "unavailable", "failed", "partial",
    "closed_index", "closed_failed_index", "closed_empty", "closed_during_header", "closed_between_accounts", "closed_before_identity",
    "identity_failed", "identity_malformed", "identity_empty"])
def test_actual_native_capture_flow_through_both_api_channels(store, monkeypatch, native_identity_flows, case):
    import ast
    import importlib
    from pathlib import Path
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service.assistant import daily_workspace_api as api, day_plan_email as delivery, outbox
    from service.tools import email_tools as email
    hub = importlib.import_module("service.assistant.hub")
    clock = [at(hour=7, minute=55)]
    for name, value in {"_email_store": store, "_identity": SelfIdentity(), "_identity_started": 0,
        "_identity_epoch": "native-flow-epoch", "_identity_previous": (), "_identity_settled": False,
        "_identity_generation": 0, "_identity_capture_at": 0,
        "_identity_session": "native-flow-test-session"}.items():
        monkeypatch.setattr(api, name, value)
    monkeypatch.setattr(api.time, "time", lambda: clock[0])
    monkeypatch.setattr(api, "email_contents", lambda *args: dict(ready=True, subject="Plan", body="Body"))
    monkeypatch.setattr(delivery, "_policy_allows", lambda _: True)
    monkeypatch.setattr(hub, "hub", SimpleNamespace(has_subscribers=True))
    calls = []
    async def bridge(*args):
        calls.append(args)
        return dict(ok=True, status="succeeded")
    monkeypatch.setattr(outbox, "request", bridge)
    # Compile only the actual handler; importing service.main would start unrelated app machinery.
    path = Path(__file__).resolve().parents[1] / "service/main.py"
    node = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "assistant_sync_emails")
    node.decorator_list = []
    namespace = {"Any": object}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    # Keep production legacy handler semantics while all cache persistence is private/inert.
    for name in ("cache_emails", "cache_history_headers", "cache_raw_emails", "cache_identity", "set_email_availability"):
        monkeypatch.setattr(email, name, lambda *args, **kwargs: None)
    app = FastAPI(); app.include_router(api.router)
    app.post("/assistant/sync/emails")(namespace["assistant_sync_emails"])
    api.publish_identity(["me@example.test"], available=True, complete=True, started_at=clock[0], now=clock[0])
    preview = store.preview(dict(recipient="me@example.test", timezone=ZONE, minute=480),
                            "Plan", "Body", api.self_identity(), now=clock[0])
    store.enable(preview["token"], api.self_identity(), confirmed=True, now=clock[0], policy_allows=lambda _: True)
    clock[0] = at(hour=7, minute=59)
    def due():
        return asyncio.run(deliver_due(store, identity=api.self_identity, compose=api.email_contents,
            sender=delivery._approved_native_submission, policy_allows=delivery._policy_allows,
            clock=lambda: at(hour=8)))
    # Replay real recorded native requests, never an OS/network invocation.
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        for request in native_identity_flows[case]["requests"]:
            body = request["body"]
            path = request["path"].replace("/assistant/today/workspace", "/workspace")
            response = client.post(path, **({"json": body} if body is not None else {}))
            assert response.status_code == 200, response.text
            if path.endswith("/identity/begin"):
                assert response.json() == dict(session="native-flow-test-session", generation=1)
                assert due() == "waiting_for_self_identity" and not calls
    clock[0] = at(hour=8)
    result = due()
    assert len(calls) == int(case == "success")
    assert result == ("submitted" if case == "success" else "waiting_for_self_identity")
    assert any(r["path"] == "/assistant/sync/emails" for r in native_identity_flows[case]["requests"])


def test_native_capture_transport_failure_aborts_before_observing_accounts(native_identity_flows):
    for case in ("bad_http", "bad_json"):
        flow = native_identity_flows[case]
        assert not flow["events"] and len(flow["requests"]) == 1


@pytest.fixture(scope="module")
def native_observer_flows(tmp_path_factory):
    """Bind the actual three callers, inventory parser and pure history state.

    No AppKit, AppleScript, real database, process launch or network is linked.
    Only this inspected Foundation fixture executable is run.
    """
    import json
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    scratch = tmp_path_factory.mktemp("native-observer-flow")
    source = (root / "app/Sources/WispApp/MailReader.swift").read_text()
    history = (root / "app/Sources/WispApp/MailHistoryMerge.swift").read_text()
    labels = (root / "app/Sources/WispApp/MailDBReader.swift").read_text()
    pieces = {
        "// WISP_OBSERVER_PURE_HISTORY": history,
        "// WISP_OBSERVER_CAPTURE_ORDER": source[source.index("private final class MailIdentityCaptureOrder {"):source.index("// Reads recent Mail.app inbox headers.")],
        "// WISP_OBSERVER_SCAN_BOOKKEEPING": source[source.index("    private func claimScan("):source.index("    // MARK: - Helpers")],
        "// WISP_OBSERVER_ACCOUNT_NAMES": source[source.index("    private typealias AccountEnumeration"):source.index("    /// Leading epoch-seconds field")],
        "// WISP_OBSERVER_SCAN_METHODS": source[source.index("    func sync() {"):source.index("    /// One retry chain at a time")],
        "// WISP_OBSERVER_HISTORY_HELPERS": source[source.index("    private func syncHistoryFromIndex("):source.index("    // MARK: - Posting")],
        "// WISP_OBSERVER_POST_METHODS": source[source.index("    private func postEmptyAccounts("):source.rindex("\n}")],
        "// WISP_OBSERVER_ACCOUNT_PARSER": labels[labels.index("    static func valid("):labels.index("    static func stored()")],
    }
    fixture = (root / "tests/NativeMailObserverFlowChecks.swift").read_text()
    for marker, body in pieces.items():
        assert fixture.count(marker) == 1
        fixture = fixture.replace(marker, body)
    generated = scratch / "ObserverFlow.swift"
    generated.write_text(fixture)
    executable = scratch / "observer-flow"
    compiled = subprocess.run(["swiftc", "-parse-as-library", "-swift-version", "5",
        "-module-cache-path", str(scratch / "module-cache"), str(generated), "-o", str(executable)],
        capture_output=True, text=True, timeout=90)
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return {row["case"]: row for row in json.loads(result.stdout)}


@pytest.fixture
def observer_replay(store, monkeypatch, tmp_path):
    """Exact native requests -> real private legacy caches + feature admission.

    The ASGI handler is extracted without importing/starting service.main.
    Policy uses the real Decision/Tier implementation; only its private grant
    lookup, remote-profile lookup, subscriber and final native bridge are inert.
    """
    import ast
    import importlib
    from pathlib import Path
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service import safety
    from service.safety import policy
    from service.inference import remote_policy
    from service.assistant import daily_workspace_api as api, day_plan_email as delivery, outbox
    from service.tools import email_tools as email, cache_store
    hub = importlib.import_module("service.assistant.hub")
    clock = [at(hour=7, minute=55)]
    for name, value in {"_email_store": store, "_identity": SelfIdentity(), "_identity_started": 0,
        "_identity_epoch": "observer-initial-epoch", "_identity_previous": (), "_identity_settled": False,
        "_identity_generation": 0, "_identity_capture_at": 0,
        "_identity_session": "native-observer-session-one"}.items():
        monkeypatch.setattr(api, name, value)
    monkeypatch.setattr(api.time, "time", lambda: clock[0])
    monkeypatch.setattr(api, "email_contents", lambda *args: dict(ready=True, subject="Plan", body="Body"))
    monkeypatch.setattr(hub, "hub", SimpleNamespace(has_subscribers=True))
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(safety.grants, "check", lambda *args: None)
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)
    cache_dir = tmp_path / "private-legacy-cache"
    monkeypatch.setattr(cache_store, "CACHE_DIR", cache_dir)
    for name in ("_headers", "_history", "_raw_emails", "_swift_headers", "_swift_history", "_swift_raw",
                 "_email_reason", "_email_read_source", "_native_source"):
        monkeypatch.setattr(email, name, "")
    for name in ("_headers_at", "_history_at", "_raw_emails_at", "_headers_sync_generation", "_native_headers_at",
                 "_native_history_at", "_native_raw_at", "_raw_snapshot_generation"):
        monkeypatch.setattr(email, name, 0)
    for name, value in {"_external_accounts": {}, "_swift_identity": [], "_identity_emails": [],
        "_email_available": None, "_email_sync_pending": False, "_raw_reference_scan": {}, "_raw_mail_snapshot": None}.items():
        monkeypatch.setattr(email, name, value)
    email.cache_emails("original headers")
    email.cache_history_headers("original history")
    email.cache_raw_emails("original body", coverage={"accounts": ["A"], "failed_accounts": [], "complete": True})
    email.cache_identity(["me@example.test"])
    email.set_email_availability(True, read_source="mail_app")
    assert cache_store.load("email_headers") == "original headers"
    assert cache_store.load("email_history") == "original history"
    assert cache_store.load("email_raw") == "original body"
    calls = []
    async def bridge(operation, payload):
        calls.append((operation, payload))
        return dict(ok=True, status="succeeded")
    monkeypatch.setattr(outbox, "request", bridge)
    path = Path(__file__).resolve().parents[1] / "service/main.py"
    node = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "assistant_sync_emails")
    node.decorator_list = []
    namespace = {"Any": object}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    app = FastAPI(); app.include_router(api.router)
    app.post("/assistant/sync/emails")(namespace["assistant_sync_emails"])
    api.publish_identity(["me@example.test"], available=True, complete=True, started_at=clock[0], now=clock[0])
    epoch = api.self_identity().epoch
    preview = store.preview(dict(recipient="me@example.test", timezone=ZONE, minute=480),
                            "Plan", "Body", api.self_identity(), now=clock[0])
    assert delivery._policy_allows("me@example.test")
    store.enable(preview["token"], api.self_identity(), confirmed=True, now=clock[0], policy_allows=delivery._policy_allows)
    clock[0] = at(hour=7, minute=59)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        def replay(request, *, expected=200):
            if "restart" in request:
                api._identity_session = request["restart"]
                api._identity_generation = 0
                api._identity_capture_at = api._identity_started = 0
                api._identity = SelfIdentity()
                api._identity_previous = ()
                api._identity_epoch = "observer-restarted-epoch"
                api._identity_settled = False
                return
            route = request["path"].replace("/assistant/today/workspace", "/workspace")
            response = client.post(route, **({"json": request["body"]} if request["body"] is not None else {}))
            assert response.status_code == expected, response.text
            if route.endswith("/identity/begin"):
                assert response.json() == dict(session=request["reply_session"], generation=request["reply_generation"])
            return response.json()
        def due(now=None):
            clock[0] = at(hour=8) if now is None else now
            return asyncio.run(deliver_due(store, identity=api.self_identity, compose=api.email_contents,
                sender=delivery._approved_native_submission, policy_allows=delivery._policy_allows, clock=lambda: clock[0]))
        yield SimpleNamespace(replay=replay, due=due, clock=clock, calls=calls, store=store, api=api,
                              email=email, cache=cache_store, epoch=epoch)


_OBSERVER_CASES = [lane + "_" + case for lane in ("raw", "history") for case in (
    "success", "empty", "duplicate", "unavailable", "closed", "closed_mid", "closed_reopened", "closed_before_identity", "closed_between",
    "body_failed", "partial", "two", "identity_failed", "identity_malformed", "identity_empty", "changed", "expanded")]
_OBSERVER_CASES += ["history_" + case for case in ("closed_batch", "index", "index_superseded", "enumeration_superseded", "walk_malformed", "capped", "commit_superseded", "deferred")]


@pytest.mark.parametrize("case", _OBSERVER_CASES)
def test_all_native_observers_revoke_or_preserve_through_real_cache_and_admission(native_observer_flows, observer_replay, case):
    h = observer_replay
    flow = native_observer_flows[case]
    native_case = case.split("_", 1)[1]
    blocked = native_case in {"empty", "unavailable", "closed", "closed_mid", "closed_reopened",
        "closed_before_identity", "closed_between", "closed_batch", "identity_failed", "identity_malformed", "identity_empty", "changed", "expanded"}
    for request in flow["requests"]:
        h.replay(request)
        if request["path"].endswith("/identity/begin"):
            assert h.due() == "waiting_for_self_identity" and not h.calls
            assert h.store.occurrence("2026-10-14") is None
    assert h.email._headers == h.cache.load("email_headers") == "original headers"
    if case == "raw_empty":
        assert h.email._history == h.cache.load("email_history") == ""
        assert h.email._raw_emails == h.cache.load("email_raw") == ""
        assert h.email._raw_reference_scan["complete"] is True
    if case in {"raw_closed", "raw_unavailable", "raw_body_failed"}:
        assert h.email._raw_emails == h.cache.load("email_raw") == "original body"
        assert h.email._raw_reference_scan["complete"] is False
    if case == "raw_partial":
        assert flow["content_calls"] == 2 and h.email._raw_reference_scan["accounts"] == ["A"]
        assert h.email._raw_reference_scan["failed_accounts"] == ["B"] and h.email._raw_reference_scan["complete"] is False
        assert h.email._raw_emails == h.cache.load("email_raw")
        assert h.email._parse_raw()[0]['account_id'] == 'one'
    expected_history = {
        "history_body_failed": "C3\x01A\x01one\x01failed\n",
        "history_partial": "C3\x01B\x01two\x01failed\n",
        "history_walk_malformed": "C3\x01A\x01one\x01failed\n",
        "history_capped": "C2\x01A\x01one\x010\x010\x011\n",
        "history_deferred": "original history",
        "history_index": "trusted synthetic history",
    }
    if case in expected_history:
        assert h.email._history == h.cache.load("email_history") == expected_history[case]
    if case in {"history_enumeration_superseded", "history_index_superseded", "history_commit_superseded"}:
        assert h.email._history == h.cache.load("email_history") == "original history"
    if case.startswith("history_"):
        assert h.email._raw_emails == h.cache.load("email_raw") == "original body"
    result = h.due()
    assert len(h.calls) == int(not blocked)
    assert h.api.self_identity().epoch == h.epoch if not blocked else h.api.self_identity().epoch != h.epoch
    if not blocked:
        assert result == "submitted" and h.store.occurrence("2026-10-14")["status"] == "submitted"
        h.due(at(hour=8, minute=1))
        assert len(h.calls) == 1
    else:
        assert result == ("connected_accounts_changed_review_settings" if native_case == "expanded" else "waiting_for_self_identity")
        assert h.store.occurrence("2026-10-14") is None


@pytest.mark.parametrize("lane", ["raw", "history"])
@pytest.mark.parametrize("failure", ["bad_http", "bad_json", "transport"])
def test_each_observer_releases_failed_begin_and_can_retry(native_observer_flows, lane, failure):
    flow = native_observer_flows[lane + "_" + failure]
    assert sum(r["path"].endswith("/identity/begin") for r in flow["requests"]) == 2
    assert sum(r["path"].endswith("/email/identity") for r in flow["requests"]) == 1
    assert flow["events"].count("accounts") == 1


@pytest.mark.parametrize("newer", ["headers", "history"])
def test_reordered_native_begin_does_not_observe_or_override_newer_accounts(native_observer_flows, observer_replay, newer):
    h = observer_replay
    flow = native_observer_flows["reordered_begin_" + newer]
    assert "raw" not in flow["events"]
    for request in flow["requests"]:
        h.replay(request)
    assert h.api.self_identity().epoch == h.epoch
    assert h.due() == "submitted" and len(h.calls) == 1


@pytest.mark.parametrize("older,newer", [("headers", "raw"), ("raw", "history"), ("history", "headers")])
@pytest.mark.parametrize("first,second", [("success", "empty"), ("empty", "success"), ("closed", "success"), ("success", "closed")])
def test_delayed_cross_observer_terminal_cannot_change_current_receipt(native_observer_flows, observer_replay, older, newer, first, second):
    h = observer_replay
    flow = native_observer_flows[f"delayed_{older}_{newer}_{first}_{second}"]
    last = None
    for request in flow["requests"]:
        last = h.replay(request)
    assert last == dict(ok=False, superseded=True)
    current = h.api.self_identity()
    assert current.complete is (second != "closed")
    assert current.addresses == (("me@example.test",) if second == "success" else ())
    # A pending older empty/failure was never accepted; it must not rotate the
    # epoch of the newer genuinely complete same-account positive.
    assert (current.epoch == h.epoch) is (second == "success")
    assert h.due() == ("submitted" if second == "success" else "waiting_for_self_identity")
    assert len(h.calls) == int(second == "success")


def test_restart_and_late_old_session_ack_require_new_consent(native_observer_flows, observer_replay):
    h = observer_replay
    for request in native_observer_flows["restart_reordered"]["requests"]:
        h.replay(request)
    assert h.api.self_identity().complete and h.api.self_identity().addresses == ("me@example.test",)
    assert h.api.self_identity().epoch != h.epoch
    assert h.due() == "connected_accounts_changed_review_settings" and not h.calls


@pytest.mark.parametrize("case", ["raw_success", "history_index"])
@pytest.mark.parametrize("age", [601, -1])
def test_actual_native_completion_keeps_original_capture_age(native_observer_flows, observer_replay, case, age):
    h = observer_replay
    for request in native_observer_flows[case]["requests"]:
        terminal = request["path"].endswith("/email/identity")
        if terminal:
            h.clock[0] = at(hour=7, minute=59) + age
        h.replay(request, expected=422 if terminal else 200)
    assert h.due(at(hour=8, minute=10)) == "waiting_for_self_identity" and not h.calls
    assert h.store.occurrence("2026-10-14") is None


def test_history_preflight_does_not_claim_to_observe_accounts(native_observer_flows):
    flow = native_observer_flows["history_preflight"]
    assert not flow["requests"] and not flow["events"] and flow["retries"] == 1


@pytest.mark.parametrize("case", ["raw_success", "history_index"])
def test_duplicate_actual_terminal_does_not_renew_its_read_start(native_observer_flows, observer_replay, case):
    h = observer_replay
    terminal = None
    for request in native_observer_flows[case]["requests"]:
        h.replay(request)
        if request["path"].endswith("/email/identity"):
            terminal = request
    original = h.api.self_identity()
    h.clock[0] = at(hour=8, minute=5)
    h.replay(terminal)
    assert h.api.self_identity() == original and original.received_at == at(hour=7, minute=59)
    assert h.due(at(hour=8, minute=10)) == "waiting_for_self_identity" and not h.calls


@pytest.mark.parametrize("case", ["raw_success", "history_index"])
def test_fresh_native_proof_after_continuity_expiry_needs_new_review(native_observer_flows, observer_replay, case):
    h = observer_replay
    h.clock[0] = at(hour=8, minute=6)
    for request in native_observer_flows[case]["requests"]:
        h.replay(request)
    assert h.api.self_identity().complete and h.api.self_identity().epoch != h.epoch
    assert h.due(at(hour=8, minute=6)) == "connected_accounts_changed_review_settings" and not h.calls


def test_continuous_actual_native_refreshes_preserve_reviewed_account_epoch(native_observer_flows, observer_replay):
    h = observer_replay
    times = iter([at(hour=7, minute=56), at(hour=8, minute=5), at(hour=8, minute=14)])
    for request in native_observer_flows["continuous_fresh"]["requests"]:
        if request["path"].endswith("/identity/begin"):
            h.clock[0] = next(times)
        h.replay(request)
    assert h.api.self_identity().received_at == at(hour=8, minute=14)
    assert h.api.self_identity().epoch == h.epoch
    assert h.due(at(hour=8, minute=15)) == "submitted" and len(h.calls) == 1


def test_actual_history_walk_yields_without_self_deadlock(native_observer_flows, observer_replay):
    h = observer_replay
    flow = native_observer_flows["history_yields_to_raw"]
    assert "history" in flow["events"] and "raw" not in flow["events"]
    for request in flow["requests"]:
        h.replay(request)
    assert h.due() == "submitted" and len(h.calls) == 1


@pytest.mark.parametrize("retry", ["success", "bad_http", "bad_json", "transport", "cancel", "cancel_data"])
def test_cold_start_header_reacquires_superseded_capture_once(native_observer_flows, retry):
    flow = native_observer_flows["cold_header_retry_" + retry]
    assert sum(request["path"].endswith("/identity/begin") for request in flow["requests"]) == 3
    assert flow["events"].count("accounts") == (2 if retry == "success" else 1)
    assert ("headers" in flow["events"]) is (retry == "success")


def test_reacquired_cold_header_capture_reaches_real_final_admission(native_observer_flows, observer_replay):
    h = observer_replay
    for request in native_observer_flows["cold_header_retry_success"]["requests"]:
        h.replay(request)
    assert h.email._headers == h.cache.load("email_headers") == "synthetic header"
    assert h.api.self_identity().epoch == h.epoch
    assert h.due() == "submitted" and len(h.calls) == 1


@pytest.mark.parametrize("retry", ["bad_http", "bad_json", "transport", "cancel", "cancel_data", "foreign", "superseded", "expired", "late_ack"])
def test_header_retry_is_bounded_and_future_invocation_can_recover(native_observer_flows, retry):
    failed = native_observer_flows["cold_header_retry_" + retry]
    recovered = native_observer_flows["cold_header_recover_" + retry]
    assert "headers" not in failed["events"]
    assert recovered["events"].count("headers") == 1
    expected = 2 if retry == "expired" else (4 if retry in {"foreign", "superseded"} else 3)
    assert sum(request.get("path", "").endswith("/identity/begin") for request in failed["requests"]) == expected
    assert sum(request.get("path", "").endswith("/identity/begin") for request in recovered["requests"]) == expected + 1


@pytest.mark.parametrize("retry", ["bad_http", "bad_json", "transport", "cancel", "cancel_data", "late_ack"])
def test_failed_header_reacquisition_does_not_leave_raw_proof_eligible(native_observer_flows, observer_replay, retry):
    h = observer_replay
    for request in native_observer_flows["cold_header_retry_" + retry]["requests"]:
        h.replay(request)
    assert h.email._headers == h.cache.load("email_headers") == "original headers"
    assert h.due() == "waiting_for_self_identity" and not h.calls
    assert h.store.occurrence("2026-10-14") is None


_ACK_INVALIDS = ["session_empty", "session_short", "session_19", "session_101", "session_number", "session_null", "session_missing", "session_unicode_19", "session_unicode_101", "session_unicode_200",
    "generation_true", "generation_false", "generation_zero", "generation_negative", "generation_string", "generation_null",
    "generation_missing", "generation_fraction", "generation_overflow"]


@pytest.mark.parametrize("lane", ["headers", "raw", "history"])
@pytest.mark.parametrize("invalid", _ACK_INVALIDS)
@pytest.mark.parametrize("stage", ["invalid", "recover"])
def test_invalid_native_ack_never_poisoned_later_same_session_admission(native_observer_flows, observer_replay, lane, invalid, stage):
    h = observer_replay
    flow = native_observer_flows[f"{stage}_{lane}_{invalid}"]
    expected_begins, expected_terminals = (2, 1) if stage == "invalid" else (4, 3)
    assert sum(r["path"].endswith("/identity/begin") for r in flow["requests"]) == expected_begins
    assert sum(r["path"].endswith("/email/identity") for r in flow["requests"]) == expected_terminals
    for request in flow["requests"]:
        h.replay(request)
        if request["path"].endswith("/identity/begin"):
            assert h.due() == "waiting_for_self_identity" and not h.calls
    assert h.api.self_identity().epoch == h.epoch
    assert h.api.self_identity().addresses == ("me@example.test",)
    assert h.due() == "submitted" and len(h.calls) == 1
    assert h.store.occurrence("2026-10-14")["status"] == "submitted"
    assert h.due(at(hour=8, minute=1)) == "submitted" and len(h.calls) == 1


@pytest.mark.parametrize("lane", ["headers", "raw", "history"])
@pytest.mark.parametrize("length", [20, 100])
def test_declared_session_boundaries_remain_native_and_backend_positives(native_observer_flows, observer_replay, lane, length):
    h = observer_replay
    flow = native_observer_flows[f"valid_session_{lane}_{length}"]
    h.api._identity_session = flow["requests"][0]["reply_session"]
    assert len(h.api._identity_session) == length
    for request in flow["requests"]:
        h.replay(request)
    assert h.due() == "submitted" and len(h.calls) == 1


@pytest.mark.parametrize("lane", ["headers", "raw", "history"])
def test_representable_positive_generation_boundary_remains_eligible(native_observer_flows, observer_replay, lane):
    h = observer_replay
    flow = native_observer_flows[f"valid_max_generation_{lane}"]
    h.api._identity_generation = 2**63 - 2
    for request in flow["requests"]:
        h.replay(request)
    assert h.api._identity_generation == 2**63 - 1
    assert h.due() == "submitted" and len(h.calls) == 1


@pytest.mark.parametrize("field,value", [("session", ""), ("session", "x" * 19), ("session", "x" * 101),
    ("session", 42), ("session", None), ("session", "e" + "\u0301" * 18),
    ("session", "e" + "\u0301" * 100), ("session", "e\u0301" * 100), ("generation", True), ("generation", False), ("generation", 0),
    ("generation", -1), ("generation", "1"), ("generation", None), ("generation", 1.5)])
def test_backend_still_rejects_malformed_receipt_while_real_native_proof_can_recover(native_observer_flows, observer_replay, field, value):
    h = observer_replay
    flow = native_observer_flows["raw_success"]
    h.replay(flow["requests"][0])
    terminal = next(r for r in flow["requests"] if r["path"].endswith("/email/identity"))
    corrupt = dict(path=terminal["path"], body={**terminal["body"], field: value})
    h.replay(corrupt, expected=422)
    assert h.due() == "waiting_for_self_identity" and not h.calls
    for request in flow["requests"][1:]:
        h.replay(request)
    assert h.due() == "submitted" and len(h.calls) == 1


@pytest.mark.parametrize("lane", ["headers", "raw", "history"])
@pytest.mark.parametrize("length", [20, 100])
def test_unicode_scalar_boundary_matches_backend_with_one_native_grapheme(native_observer_flows, observer_replay, lane, length):
    h = observer_replay
    flow = native_observer_flows[f"valid_unicode_session_{lane}_{length}"]
    session = flow["requests"][0]["reply_session"]
    assert len(session) == length and session == "e" + "\u0301" * (length - 1)
    h.api._identity_session = session
    for request in flow["requests"]:
        h.replay(request)
    assert h.due() == "submitted" and len(h.calls) == 1


def test_actual_native_scan_builders_compile_by_stable_identity(tmp_path):
    """Compile production Swift generators and AppleScript, without executing Mail."""
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    source = (root / 'app/Sources/WispApp/MailReader.swift').read_text()
    builders = source[source.index('    private func esc('):source.index('    // Enabled accounts, one native-ID/name pair')]
    target = source[source.index('    private struct AccountTarget {'):source.index('    /// Current enabled IDs')]
    # The native account target and every generator below come from production.
    generated = tmp_path / 'ScanCompile.swift'
    generated.write_text('import Foundation\n@main struct ScanCompile {\n' + target + builders + r'''
        static func main() {
            let generator = ScanCompile()
            for target in [AccountTarget(id: "native-A", label: "Google"),
                           AccountTarget(id: "native-B", label: "Google"),
                           AccountTarget(id: "native-quote\"\\", label: "gOOgle \"\\")] {
                let selector = generator.inboxSource(target)
                precondition(selector.contains("every account whose id is"))
                precondition(selector.contains("of scanAccount"))
                precondition(selector.contains("enabled of scanAccount"))
                precondition(!selector.contains("of account \""))
                let sources = [generator.headerScript(account: target, limit: 2),
                               generator.rawScript(account: target, limit: 2),
                               generator.historyBatchScript(account: target, start: 1, count: 2),
                               generator.historyBatchScript(account: target, start: 2, count: 2, cutoff: 1700000000)]
                for source in sources {
                    precondition(source.contains("Message account identity changed."))
                    var error: NSDictionary?
                    precondition(NSAppleScript(source: source)!.compileAndReturnError(&error), "Compilation: \(error ?? [:])")
                }
            }
            print("Production stable-ID scan generators compiled; no Mail scripts executed.")
        }
    }
    ''')
    binary = tmp_path / 'scan-compile'
    result = subprocess.run(['swiftc', '-parse-as-library', '-swift-version', '5',
        '-module-cache-path', str(tmp_path / 'module-cache'), str(generated),
        str(root / 'app/Sources/WispApp/MailHistoryMerge.swift'), '-o', str(binary)],
        capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'no Mail scripts executed' in result.stdout


def test_native_v2_bridge_admits_same_label_ids_including_empty_second_account(native_observer_flows, observer_replay):
    h = observer_replay
    for request in native_observer_flows['raw_duplicate_empty']['requests']:
        h.replay(request)
    snapshot = h.email.raw_email_snapshot()
    assert snapshot is not None and snapshot.complete
    assert snapshot.account_inventory == (('one', 'A'), ('two', 'A'))
    assert snapshot.successful_account_ids == ('one', 'two')
    assert {row['account_id'] for row in snapshot.rows} == {'one'}
    assert 'Which one?' in h.email._raw_account_scope(list(snapshot.rows), 'A')[1]
    assert not h.email._raw_account_scope(list(snapshot.rows), 'two')[0]


@pytest.mark.parametrize('case', ['duplicate_id', 'malformed_inventory'])
def test_native_inventory_failure_cannot_publish_body_evidence(native_observer_flows, observer_replay, case):
    h = observer_replay
    for request in native_observer_flows['raw_' + case]['requests']:
        h.replay(request)
    assert h.email.raw_email_snapshot() is None
    assert native_observer_flows['raw_' + case]['content_calls'] == 0
    assert h.email.raw_email_scan_status()['failures'] == [{'stage': 'account_inventory', 'code': 0}]
