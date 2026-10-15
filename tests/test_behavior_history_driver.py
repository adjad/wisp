"""Budgeted explicit-history work; all source, clock and HTTP state is fake."""
import asyncio

import pytest

from service import gmail_behavior_history as H
from service.assistant.gmail_history_runtime import HistoryHost
from tests.test_behavior_gmail_history import CoreBridgeBackend, NOW, TEXT, command
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
