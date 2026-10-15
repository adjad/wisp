"""Public Gmail history evidence with fake HTTP and transient synthetic bodies."""
from dataclasses import replace
from types import SimpleNamespace
import asyncio
from datetime import datetime, timezone

import pytest

from service import gmail_behavior_history as H
from service.assistant.behavior_sources import capture
from tests.test_gmail_behavior_history import FakeHTTP, message, token

NOW = datetime(2026, 10, 15, 8, tzinfo=timezone.utc).timestamp()
TEXT = "The arbitrary work is due 2026-10-20 at 4pm UTC."


@pytest.fixture
def history():
    clock = [NOW]
    scope = H.HistoryScope(int(NOW-86400), int(NOW), "mailbox", 100)
    account = H.AccountIdentity("gmail", "configured-one", "connection-one", "set-one")
    permission = [H.Admission(account, 1, 1, True, True, True, scope.digest)]
    provider = H.HistoryProvider(admission=lambda: permission[0],
        limits=H.Limits(80000, 8000000, 32000000, 1200),
        clock=lambda: clock[0], monotonic=lambda: 0)
    http = FakeHTTP(messages={"51": message("51", TEXT, int(NOW-60))})
    provider.control("start", scope=scope)
    budget = H.StepBudget(100, 5, 6, 400000, 30)
    def step():
        return asyncio.run(provider.step(http=http, token=token, budget=budget))
    step()
    assert provider.snapshot() and len(provider.snapshot().records) == 1
    assert provider.snapshot().records[0].body.eligible
    yield SimpleNamespace(provider=provider, clock=clock, permission=permission,
                          http=http, step=step, scope=scope, account=account)
    provider.invalidate("fixture complete")


def publications(history, native=None):
    return {"email": lambda **kw: native,
            "gmail_history": lambda: (history.provider.snapshot,)}


def test_public_history_with_native_mail_closed_reaches_evidence(history):
    calls = len(history.http.calls)
    batch = capture({"email": True}, getters=publications(history), now=NOW)
    snap = next(s for s in batch.snapshots if s.source == "email")
    assert snap.available and len(snap.records) == 1
    row = snap.records[0]
    assert row["text"] == TEXT and row["reference"]["resource_id"] == "51"
    assert row["reference"]["account_id"] == "configured-one"
    assert row["source_time"] is None and row["time_basis"] == "unknown"
    assert snap.publication_complete is False and batch.current()
    assert len(history.http.calls) == calls


def test_history_requires_existing_email_source_permission(history):
    def forbidden():
        raise AssertionError("history getter used while source is disabled")
    batch = capture({"email": False}, getters={"gmail_history": forbidden}, now=NOW)
    assert not batch.records and not any(s.available for s in batch.snapshots)


def test_expired_gmail_does_not_shadow_valid_native_mail(history):
    history.clock[0] += 1201
    native = SimpleNamespace(generation="native-one", synced_at=NOW,
        rows=({"account_id": "native-account", "message_id": "native-id",
               "subject": "Native", "body": "Independent native evidence.", "ts": NOW-10},))
    batch = capture({"email": True}, getters=publications(history, native), now=history.clock[0])
    snap = next(s for s in batch.snapshots if s.source == "email")
    assert snap.available and len(snap.records) == 1
    assert "Independent native evidence." in snap.records[0]["text"] and batch.current()


def test_history_receipt_withdrawal_cancels_captured_evidence(history):
    batch = capture({"email": True}, getters=publications(history), now=NOW)
    assert batch.current() and batch.records
    history.permission[0] = None
    assert not batch.current()
    current = capture({"email": True}, getters=publications(history), now=NOW)
    assert not current.records


def test_history_source_expiry_is_not_renewed_by_readback(history):
    batch = capture({"email": True}, getters=publications(history), now=NOW)
    assert batch.records and batch.current()
    original = history.provider.snapshot().receipt.expires_at
    for seconds in (10, 100, 1199):
        history.clock[0] = NOW+seconds
        assert batch.current() and history.provider.snapshot().receipt.expires_at == original
    history.clock[0] = original
    assert not batch.current()
    assert not capture({"email": True}, getters=publications(history), now=original).records


def test_actual_public_gmail_evidence_to_runtime_and_proposed_day_plan(history, tmp_path, monkeypatch):
    import json
    import sqlite3
    import threading
    from service.assistant import behavior_runtime as runtime_module
    from service.assistant.behavior_runtime import BehaviorRuntime
    from service.assistant.behavior_interpreter import Interpreter
    from service.assistant import daily_workspace as daily
    from service.assistant.store import AssistantStore
    from service.behavior.engine import BehaviorEngine

    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    prompts, role = [], ["isolated-local-role"]
    def complete(system, prompt, **kwargs):
        rows = json.loads(prompt)["evidence"]
        prompts.append(rows)
        return json.dumps(dict(patterns=[], links=[], demands=[dict(source_id=r["id"], quote=r["text"],
            kind="preparation", title="Tentative preparation", ownership="user", importance="unknown",
            temporal_id=r["temporal"][0]["id"]) for r in rows]))
    store = AssistantStore(tmp_path/"assistant.db")
    turns = sqlite3.connect(":memory:")
    turns.execute("CREATE TABLE turns (session_id TEXT, idx INTEGER, content TEXT, created_at REAL, role TEXT)")
    sessions = SimpleNamespace(_db=turns, _lock=threading.Lock())
    runtime = BehaviorRuntime(tmp_path/"behavior", clock=lambda: history.clock[0],
        local_factory=lambda p: BehaviorEngine(p, tz="UTC", clock=lambda: history.clock[0]),
        messages_fn=lambda: {}, contact_name_fn=lambda s:s, calendar_sync_fn=lambda: {},
        session_db_fn=lambda:sessions, behavior_sources_fn=lambda settings: capture(settings,
            getters=publications(history), now=history.clock[0]),
        behavior_interpreter=Interpreter(complete), role_key_fn=lambda:role[0])
    monkeypatch.setattr(runtime_module, "get_runtime", lambda:runtime)
    monkeypatch.setattr(daily, "_availability_provider", None)
    try:
        runtime.call("configure_signals", {"email": True, "interpretation": True}, NOW)
        runtime.work(store, NOW, force=True)
        assert runtime.errors == {}
        public = daily.availability_snapshot("2026-10-15", "UTC", NOW)
        assert public["ready"] and len(public["demands"]) == 1
        work = public["demands"][0]
        assert work["quote"] == TEXT and work["observed_at"] is None
        assert work["action_authority"] == "none" and work["importance"] == "unknown"
        assert work["reference"]["resource_id"] == "51"
        calls = len(history.http.calls)
        state = dict(tasks=[], commitments=[], preferences=dict(start_minute=540,end_minute=1080,not_before=None),
            sources={s:dict(available=True,last_sync=NOW,snapshot_started_at=NOW,
                diagnostics=dict(coverage_start=NOW-28800,coverage_end=NOW+57600)) for s in ("calendar","reminders")})
        workspace = daily.assess("2026-10-15", "UTC", state, {}, now=NOW, behavior=public)
        suggestions = [b for b in workspace["proposed_plan"]["blocks"] if b["kind"] == "suggestion"]
        assert len(suggestions) == 1 and suggestions[0]["task_id"] is None
        assert workspace["attention"][0]["importance"] == "unknown"
        for _ in range(3):
            assert daily.availability_snapshot("2026-10-15", "UTC", NOW)["ready"]
        assert len(prompts) == 1 and len(history.http.calls) == calls
        assert TEXT not in "\n".join(runtime.local_engine()._store._conn.iterdump())
        role[0] = "different-role"
        assert not daily.availability_snapshot("2026-10-15", "UTC", NOW)["ready"]
        role[0] = "isolated-local-role"
        history.permission[0] = None
        assert not daily.availability_snapshot("2026-10-15", "UTC", NOW)["ready"]
        assert len(prompts) == 1 and len(history.http.calls) == calls
    finally:
        runtime.stop();runtime.close();turns.close();store._db.close()


class CoreBridgeBackend:
    """Injected protocol fake over the real core; not a configured-token adapter."""
    def __init__(self, clock):
        self.clock = clock
        self.account = H.AccountIdentity("gmail", "configured-one", "connection-one", "set-one")
        self.opened = []

    def accounts(self):
        return (self.account,)

    def open(self, account, scope, probe):
        def admission():
            p = probe()
            if p is None or account not in self.accounts():
                return None
            return H.Admission(account, p.consent_epoch, p.session_epoch, p.history_start,
                               p.global_learning, p.email_consent, scope.digest)
        provider = H.HistoryProvider(admission=admission, limits=H.Limits(80000,8000000,32000000,1200),
            clock=lambda:self.clock[0], monotonic=lambda:0)
        provider.control("start", scope=scope)
        class Bridge:
            status = provider.status
            snapshot = provider.snapshot
            invalidate = provider.invalidate
            async def step(self, *, http, budget):
                return await provider.step(http=http, token=token, budget=budget)
        self.opened.append(provider)
        return Bridge()


@pytest.fixture
def host():
    from service.assistant.gmail_history_runtime import HistoryHost
    from service import config
    config.models_config()  # Trusted observation of this worker's private config.
    clock = [NOW]
    backend = CoreBridgeBackend(clock)
    host = HistoryHost(backend=backend)
    host.bind(enabled=True, settings={"email":True}, epoch="consent-one", role="local-fast-one")
    yield host, backend, clock
    host.invalidate("fixture complete", close=True)


def command(host, kind="start", **changes):
    status = host.status()
    account = status["accounts"][0]
    body = dict(command=kind, account_id=account["account_id"],
        connection_revision=account["connection_revision"], account_set_revision=account["account_set_revision"],
        revision=account["control_revision"], session=status["session"],
        permission_revision=status["permission_revision"])
    if kind == "start":
        body["scope"] = dict(start=int(NOW-86400),end=int(NOW),selection="mailbox",message_cap=100)
    return body | changes


def test_history_host_has_no_default_source_transport_or_restored_permission():
    from service.assistant.gmail_history_runtime import HistoryHost
    h = HistoryHost()
    assert h.status()["available"] is False and h.cached_getters() == ()
    h.bind(enabled=True, settings={"email":True}, epoch="consent", role="local")
    assert h.status()["accounts"] == [] and h.cached_getters() == ()


def test_host_start_and_getters_never_fetch_then_explicit_step_publishes(host):
    h, backend, clock = host
    assert backend.opened == [] and h.cached_getters() == ()
    h.control(command(h))
    http = FakeHTTP(messages={"51":message("51",TEXT,int(NOW-60))})
    assert h.status()["accounts"][0]["counts"]["fetched"] == 0 and http.calls == []
    asyncio.run(h.step("configured-one",http=http,budget=H.StepBudget(100,5,6,400000,30)))
    batch = capture({"email":True},getters={"email":lambda **kw:None},now=NOW,
                    gmail_history_getters=h.cached_getters)
    assert len(batch.records) == 1 and batch.current()
    calls = len(http.calls)
    for _ in range(3):
        assert h.status()["accounts"][0]["counts"]["fetched"] == 1 and batch.current()
    assert len(http.calls) == calls
    h.control(command(h,"pause"))
    assert h.cached_getters() == () and not batch.current()


def test_stale_or_payload_minted_permission_cannot_control_history(host):
    from service.assistant.gmail_history_runtime import HistoryConflict
    h,backend,clock = host
    original = command(h)
    with pytest.raises(ValueError):
        h.control(original | {"global_learning":True,"email_consent":True})
    assert backend.opened == []
    h.control(original)
    with pytest.raises(HistoryConflict):
        h.control(original)
    assert len(backend.opened) == 1
    stale_connection = command(h) | {"connection_revision":"copied-or-previous"}
    with pytest.raises(HistoryConflict):
        h.control(stale_connection)


@pytest.mark.parametrize("change", ["email", "learning", "role", "epoch", "forget"])
def test_host_withdraws_permission_on_each_application_boundary(host, change):
    h,backend,clock = host
    h.control(command(h))
    probe_before = backend.opened[0].status()["state"]
    assert probe_before == "running"
    if change == "forget":
        h.invalidate("Forget",close=True)
    else:
        h.bind(enabled=change!="learning",settings={"email":change!="email"},
               epoch="different" if change=="epoch" else "consent-one",
               role="different" if change=="role" else "local-fast-one")
    assert h.cached_getters() == () and backend.opened[0].snapshot() is None
    if change != "forget":
        h.bind(enabled=True,settings={"email":True},epoch="consent-one",role="local-fast-one")
        assert h.cached_getters() == ()  # no automatic resume


def test_pause_during_real_core_http_await_cannot_publish_late_body(host):
    from service.assistant.gmail_history_runtime import HistoryConflict
    h,backend,clock = host
    h.control(command(h))
    async def pause(method,url):
        h.control(command(h,"pause"))
    http = FakeHTTP(messages={"51":message("51",TEXT,int(NOW-60))},hook=pause)
    with pytest.raises(HistoryConflict):
        asyncio.run(h.step("configured-one",http=http,budget=H.StepBudget(100,5,6,400000,30)))
    assert len(http.calls) == 1 and h.cached_getters() == () and backend.opened[0].snapshot() is None


def test_reset_cannot_implicitly_restart_an_account_job(host):
    h,backend,clock = host
    h.control(command(h))
    h.control(command(h,"reset"))
    assert h.cached_getters() == () and len(backend.opened) == 1
    assert h.status()["accounts"][0]["state"] == "off"
