"""Budgeted explicit-history work; all source, clock and HTTP state is fake."""
import asyncio

import pytest

from service.assistant.gmail_history_runtime import HistoryHost
from tests.test_behavior_gmail_history import CoreBridgeBackend, NOW, TEXT, command, public_host
from tests.test_gmail_behavior_history import FakeHTTP, message


@pytest.fixture
def driver_host():
    from service import config
    config.models_config()
    clock = [NOW]
    backend = CoreBridgeBackend(clock)
    host = HistoryHost(backend=backend)
    host.bind(enabled=True,settings={"email":True},epoch="consent",role="synthetic-local")
    yield host,backend,clock
    host.invalidate("fixture complete",close=True)


def test_no_explicit_start_never_constructs_a_client(driver_host):
    from service.assistant.behavior_history_driver import HistoryDriver
    host,backend,clock = driver_host
    def forbidden():
        raise AssertionError("Constructed HTTP without explicit Start")
    driver = HistoryDriver(host,client_factory=forbidden,monotonic=lambda:clock[0])
    for _ in range(3):
        assert asyncio.run(driver.advance())["state"] == "idle"
    assert backend.opened == []


def test_acquisition_spacing_cannot_be_bypassed_and_owned_clients_close_once(driver_host):
    from service.assistant.behavior_history_driver import HistoryDriver
    host,backend,clock = driver_host
    host.control(command(host))
    clients = []
    class Client(FakeHTTP):
        closed = 0
        async def aclose(self):
            self.closed += 1
    def factory():
        http = Client(pages=[{"messages":[{"id":str(i)} for i in range(12)]}],
            messages={str(i):message(str(i),TEXT,int(NOW-i)) for i in range(12)})
        clients.append(http)
        return http
    driver = HistoryDriver(host,client_factory=factory,monotonic=lambda:clock[0])
    first = asyncio.run(driver.advance())
    assert first["state"] == "advanced" and len(clients) == 1 and clients[0].closed == 1
    calls = sum(len(c.calls) for c in clients)
    assert calls <= 6 and backend.opened[0].status()["fetched"] <= 5
    for seconds in (0,1,29):
        clock[0] = NOW+seconds
        assert asyncio.run(driver.advance())["state"] == "waiting"
    assert len(clients) == 1
    clock[0] = NOW+30
    assert asyncio.run(driver.advance())["state"] == "advanced"
    assert len(clients) == 2 and all(c.closed == 1 for c in clients)


def test_borrowed_client_is_not_closed_and_backpressure_does_not_fetch(driver_host):
    from service.assistant.behavior_history_driver import HistoryDriver
    host,backend,clock = driver_host
    host.control(command(host))
    http = FakeHTTP(messages={"51":message("51",TEXT,int(NOW-60))})
    async def forbidden():
        raise AssertionError("Closed a caller-owned client")
    http.aclose = forbidden
    driver = HistoryDriver(host,client=http,monotonic=lambda:clock[0])
    assert asyncio.run(driver.advance(pending=lambda account:8))["state"] == "backpressure"
    assert http.calls == []
    assert asyncio.run(driver.advance(pending=lambda account:0))["state"] == "advanced"
    assert len(http.calls) == 2


def test_cancel_during_http_withdraws_job_and_closes_owned_client(driver_host):
    from service.assistant.behavior_history_driver import HistoryDriver
    host,backend,clock = driver_host
    host.control(command(host))
    clients = []
    async def run():
        entered = asyncio.Event()
        async def blocked(method,url):
            entered.set()
            await asyncio.Event().wait()
        class Client(FakeHTTP):
            closed = 0
            async def aclose(self):
                self.closed += 1
        def factory():
            http = Client(hook=blocked)
            clients.append(http)
            return http
        driver = HistoryDriver(host,client_factory=factory,monotonic=lambda:clock[0])
        task = asyncio.create_task(driver.advance())
        await entered.wait()
        assert (await driver.advance())["state"] == "running"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert clients[0].closed == 1 and host.cached_getters() == ()
    asyncio.run(run())


@pytest.mark.parametrize("account_count,fault",[(1,None),(2,None),(1,"pause_http"),(1,"cancel_http"),(1,"role_model"),
    (1,"continuation"),(1,"continuation_drain"),(1,"late"),(1,"segment_limit"),(1,"close_failure"),
    (1,"commit_withdrawal"),(1,"commit_boundary"),(1,"withdraw_prepared")])
def test_default_runtime_driver_advances_public_history_into_day_planning(public_host, tmp_path, monkeypatch, account_count, fault):
    import json
    import sqlite3
    import threading
    from types import SimpleNamespace
    from service import gmail_source as G
    from service.assistant import behavior_runtime as br, daily_workspace as daily
    from service.assistant.behavior_interpreter import Interpreter
    from service.assistant.store import AssistantStore
    from service.behavior.engine import BehaviorEngine
    from service.tools import email_tools
    from tests.test_behavior_runtime import FakeHub
    p = public_host
    p.messages.clear()
    p.messages.update({str(i):message(str(i),TEXT,int(NOW-i-1)) for i in range(200 if fault == "segment_limit" else 12)})
    if account_count == 2:
        p.source_config["accounts"]["second@example.invalid"] = {"refresh_token":"second-refresh"}
        G.refresh_history_configuration()
    monkeypatch.setenv("WISP_BEHAVIOR","1")
    monkeypatch.setattr(email_tools,"raw_email_snapshot",lambda **kwargs:None)
    def forbidden(*a,**kw):
        raise AssertionError("Production network factory was invoked")
    monkeypatch.setattr(G.HistoryHTTPClient,"network",forbidden)
    prompts,clients = [],[]
    def client_factory():
        http = G.HistoryHTTPClient.synthetic(p.server)
        if fault == "close_failure":
            close = http.aclose
            async def broken_close():
                await close()
                raise RuntimeError("Synthetic cleanup failure")
            http.aclose = broken_close
        clients.append(http)
        return http
    def complete(system,prompt,**kwargs):
        from service import config
        rows = json.loads(prompt)["evidence"]
        prompts.append(rows)
        if fault == "role_model":
            config.set_role("fast","changed-synthetic-role")
        return json.dumps(dict(patterns=[],links=[],demands=[dict(source_id=r["id"],quote=r["text"],
            kind="preparation",title="Tentative preparation",ownership="user",importance="unknown",
            temporal_id=r["temporal"][0]["id"]) for r in rows]))
    store = AssistantStore(tmp_path/"assistant.db")
    turns = sqlite3.connect(":memory:",check_same_thread=False)
    turns.execute("CREATE TABLE turns (session_id TEXT, idx INTEGER, content TEXT, created_at REAL, role TEXT)")
    runtime = br.BehaviorRuntime(tmp_path/"runtime",clock=lambda:p.clock[0],
        local_factory=lambda path:BehaviorEngine(path,tz="UTC",clock=lambda:p.clock[0]),
        messages_fn=lambda:{},contact_name_fn=lambda s:s,calendar_sync_fn=lambda:{},
        session_db_fn=lambda:SimpleNamespace(_db=turns,_lock=threading.Lock()),
        role_key_fn=lambda:"synthetic-local",behavior_interpreter=Interpreter(complete),
        gmail_history_client_factory=client_factory,history_monotonic=lambda:p.clock[0])
    monkeypatch.setattr(br,"get_runtime",lambda:runtime)
    monkeypatch.setattr(daily,"_availability_provider",None)
    publication_started,withdrawn_at_boundary = [False],[False]
    rejected_at_boundary = []
    try:
        runtime.call("configure_signals",{"email":True,"interpretation":True},NOW)
        from service.config import authority_changes
        from service.publication_authority import PublicationBusy
        authority_before = authority_changes.current()
        if fault in {"commit_withdrawal","commit_boundary"}:
            def withdraw_at_write(sql):
                if sql.startswith("INSERT INTO behavior_seed_work"):
                    publication_started[0] = True
                if (not withdrawn_at_boundary[0] and publication_started[0] and
                        (fault == "commit_withdrawal" and sql.startswith("INSERT INTO behavior_seed_work") or
                         fault == "commit_boundary" and sql == "COMMIT")):
                    from service import config
                    withdrawn_at_boundary[0] = True
                    try:
                        config.set_role("fast","changed-at-publication")
                    except PublicationBusy as error:
                        rejected_at_boundary.append(error)
                    else:
                        raise AssertionError("Reentrant withdrawal was falsely acknowledged")
            runtime.local_engine()._store._conn.set_trace_callback(withdraw_at_write)
        if fault == "withdraw_prepared":
            prepare = runtime._prepare_publication_witness
            def prepare_then_withdraw(*args,**kwargs):
                witness = prepare(*args,**kwargs)
                if prompts and not withdrawn_at_boundary[0]:
                    from service import config
                    config.set_role("fast","changed-before-publication")
                    withdrawn_at_boundary[0] = True
                return witness
            monkeypatch.setattr(runtime,"_prepare_publication_witness",prepare_then_withdraw)
        assert runtime.email_history_status()["accounts"] and clients == [] and prompts == []
        assert p.calls == []
        asyncio.run(runtime.tick(store,FakeHub(),now=p.clock[0]))
        assert clients == [] and p.calls == [] and prompts == []  # No restored or startup Start.
        for row in runtime.email_history_status()["accounts"]:
            body = command(runtime._email_history)
            body.update({key:row[key] for key in ("account_id","connection_revision","account_set_revision")})
            body["revision"] = row["control_revision"]
            if fault == "segment_limit":
                body["scope"]["message_cap"] = 2000
            runtime.email_history_control(body)
        assert clients == [] and p.calls == []
        async def run():
            if fault in {"pause_http","cancel_http"}:
                entered = asyncio.Event()
                async def interrupted(request):
                    if request.url.path.endswith("/0"):
                        if fault == "pause_http":
                            runtime.email_history_control(command(runtime._email_history,"pause"))
                        else:
                            entered.set()
                            await asyncio.Event().wait()
                p.hook[0] = interrupted
                task = asyncio.create_task(runtime.tick(store,FakeHub(),now=p.clock[0]))
                if fault == "cancel_http":
                    await asyncio.wait_for(entered.wait(),3)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    await task
                assert len(p.calls) == 4 and prompts == []
                assert runtime._email_history.cached_getters() == ()
                assert len(clients) == 1 and clients[0]._client.is_closed
                assert not daily.availability_snapshot("2026-10-15","UTC",p.clock[0])["demands"]
                return
            await runtime.tick(store,FakeHub(),now=p.clock[0])
            if fault == "close_failure":
                assert len(p.calls) == 6 and prompts == []
                assert runtime._email_history.cached_getters() == () and clients[0]._client.is_closed
                assert "email_history" in runtime.errors and runtime.last["tick"] == p.clock[0]
                p.clock[0] += 30
                await runtime.tick(store,FakeHub(),now=p.clock[0],force=True)
                assert len(p.calls) == 6 and len(clients) == 1 and prompts == []
                return
            assert len(p.calls) == 6 and len(prompts) == 1
            if fault in {"role_model","withdraw_prepared"}:
                if fault == "withdraw_prepared":
                    assert withdrawn_at_boundary[0] and authority_changes.current() is not authority_before
                assert runtime._email_history.cached_getters() == ()
                assert not daily.availability_snapshot("2026-10-15","UTC",p.clock[0])["demands"]
                conn = runtime.local_engine().seed_index().store._conn
                for table in ("work","links","capacity"):
                    assert conn.execute("SELECT COUNT(*) FROM behavior_seed_"+table).fetchone()[0] == 0
                assert conn.execute("SELECT coalesce(sum(covered),0),coalesce(sum(attempts),0) FROM behavior_seed_records").fetchone()[:] == (0,0)
                assert clients[0]._client.is_closed
                return
            if fault in {"commit_withdrawal","commit_boundary"}:
                # Supported public ordering contract: a same-thread mutation
                # cannot take effect or acknowledge success during publication.
                assert withdrawn_at_boundary[0] and len(rejected_at_boundary) == 1
                assert "not applied" in str(rejected_at_boundary[0])
                assert authority_changes.current() is authority_before
                assert runtime._email_history.cached_getters()
                assert daily.availability_snapshot("2026-10-15","UTC",p.clock[0])["demands"]
                conn = runtime.local_engine().seed_index().store._conn
                assert conn.execute("SELECT count(*) FROM behavior_seed_work").fetchone()[0] == 3
                assert not conn.in_transaction
                from service import config
                config.set_role("fast","changed-at-publication")  # Actual retry, outside the lease.
                assert authority_changes.current() is not authority_before
                assert runtime._email_history.cached_getters() == ()
                assert not daily.availability_snapshot("2026-10-15","UTC",p.clock[0])["demands"]
                assert clients[0]._client.is_closed
                return
            assert runtime.seed_progress()["sources"]["email"]["analyzed"] == 3
            assert daily.availability_snapshot("2026-10-15","UTC",p.clock[0])["demands"]
            if fault == "segment_limit":
                for segment in range(2,33):
                    p.clock[0] = NOW+(segment-1)*900
                    before = len(p.calls)
                    await runtime.tick(store,FakeHub(),now=p.clock[0],force=True)
                    assert len(p.calls)-before <= 6
                    assert runtime.email_history_status()["accounts"][0]["continuation"]["segment"] == segment
                calls,models = len(p.calls),len(prompts)
                p.clock[0] = NOW+32*900
                await runtime.tick(store,FakeHub(),now=p.clock[0],force=True)
                row = runtime.email_history_status()["accounts"][0]
                assert row["state"] == "partial" and row["continuation"]["blocked_reason"] == "segment_limit"
                assert row["counts"]["fetched"] < 200 and row["counts"]["pending"] > 0
                assert len(p.calls) == calls and len(prompts) == models and len(clients) == 32
                assert daily.availability_snapshot("2026-10-15","UTC",p.clock[0])["ready"]
                p.clock[0] += 30
                await runtime.tick(store,FakeHub(),now=p.clock[0],force=True)
                assert len(p.calls) == calls and all(client._client.is_closed for client in clients)
                return
            if fault in {"continuation","continuation_drain","late"}:
                from service.assistant.behavior_sources import capture
                if fault == "continuation_drain":
                    p.clock[0] = NOW+30
                    await runtime.tick(store,FakeHub(),now=p.clock[0])
                    assert len(p.calls) == 11 and len(prompts) == 2
                    assert runtime.seed_progress()["sources"]["email"]["pending"] == 1
                batch = capture({"email":True},getters={"email":lambda **kwargs:None},now=p.clock[0],
                                gmail_history_getters=runtime._email_history.cached_getters)
                old = {d["id"] for d in daily.availability_snapshot("2026-10-15","UTC",p.clock[0])["demands"]}
                assert batch.current() and old
                if fault == "late":
                    p.clock[0] = NOW+1200
                    assert runtime.seed_progress()["state"] == "unavailable"
                    await runtime.tick(store,FakeHub(),now=p.clock[0],force=True)
                    assert len(p.calls) == 6 and len(prompts) == 1 and len(clients) == 1
                    assert not batch.current()
                    assert not daily.availability_snapshot("2026-10-15","UTC",p.clock[0])["demands"]
                    return
                p.clock[0] = NOW+900
                await runtime.tick(store,FakeHub(),now=p.clock[0],force=True)
                if fault == "continuation_drain":
                    assert len(p.calls) == 11 and len(prompts) == 3  # Drain before dropping any old proof.
                    assert batch.current()
                    assert runtime.seed_progress()["sources"]["email"]["pending"] == 0
                    p.clock[0] = NOW+930
                    await runtime.tick(store,FakeHub(),now=p.clock[0],force=True)
                    expected_cache,expected_calls,expected_prompts = 4,17,4
                else:
                    assert len(p.calls) == 12 and len(prompts) == 2
                    for delta in (930,960):
                        p.clock[0] = NOW+delta
                        await runtime.tick(store,FakeHub(),now=p.clock[0],force=True)
                    expected_cache,expected_calls,expected_prompts = 9,17,4
                row = runtime.email_history_status()["accounts"][0]
                assert row["continuation"]["segment"] == 2
                assert row["counts"]["fetched"] == 12 and row["counts"]["cached_eligible"] == expected_cache
                assert len(p.calls) == expected_calls and len(prompts) == expected_prompts
                assert not batch.current()  # Old source times/proofs were withdrawn, not renewed.
                assert len({r["id"] for rows in prompts for r in rows}) == 12
                current = daily.availability_snapshot("2026-10-15","UTC",p.clock[0])
                assert current["ready"] and not old.intersection(d["id"] for d in current["demands"])
                assert all(client._client.is_closed for client in clients)
                assert len([r for r in p.calls if r.url.path.endswith("/profile")]) == 2
                assert len([r for r in p.calls if r.url.path.endswith("/token")]) == 2
                return
            for delta in (0,1,29):
                p.clock[0] = NOW+delta
                await runtime.tick(store,FakeHub(),now=p.clock[0],force=True)
                assert len(p.calls) == 6 and len(prompts) == 1
            for delta in range(30,91 if account_count == 1 else 181,30):
                p.clock[0] = NOW+delta
                await runtime.tick(store,FakeHub(),now=p.clock[0],force=True)
                if account_count == 2 and delta == 30:
                    assert len(p.calls) == 12  # One global step, one different account on its next turn.
                    assert [row["counts"]["fetched"] for row in runtime.email_history_status()["accounts"]] == [3,3]
            progress = runtime.seed_progress()["sources"]["email"]
            assert progress["analyzed"] == 12*account_count and progress["pending"] == 0
            expected_prompts = 4 if account_count == 1 else 7
            assert len(prompts) == expected_prompts and len(clients) == 3*account_count
            assert len(p.calls) == 15*account_count  # Token/profile/list +12 bodies per account.
            assert all(client._client.is_closed for client in clients)
            calls = len(p.calls)
            for _ in range(3):
                public = daily.availability_snapshot("2026-10-15","UTC",p.clock[0])
                assert public["ready"]
                assert all(d["action_authority"] == "none" and d["observed_at"] is None for d in public["demands"])
            assert len(p.calls) == calls and len(prompts) == expected_prompts
        asyncio.run(run())
        assert runtime.errors == {} if fault != "close_failure" else set(runtime.errors) == {"email_history"}
    finally:
        runtime.stop();runtime.close();turns.close();store._db.close()
