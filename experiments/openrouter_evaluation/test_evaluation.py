"""Offline characterization, including assertions that document adoption blockers.

No service initialization: load the real endpoint/readiness source with synthetic
dependencies. These are wrapper tests, not HTTP transport or billing evidence.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules during execution.
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


pilot = load("pinned_openrouter_pilot", HERE / "vendor/adapter.py")
endpoints = load("isolated_endpoints", ROOT / "service/config/endpoints.py")
errors = load("isolated_errors", ROOT / "service/inference/inference_errors.py")


def completion(**extra):
    return json.dumps({"model": "fixture/model", "provider": "fixture-provider",
        "choices": [{"message": {"role": "assistant", "content": "synthetic"},
                     "finish_reason": "stop"}], **extra}).encode()


class PilotGapTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch.object(socket, "socket", side_effect=AssertionError("Network prohibited"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def request(self, messages=None, model="fixture/model", **kwargs):
        return pilot.request(model, "fixture-provider", messages or [
            {"role": "user", "content": "synthetic"}], [], enabled=True, **kwargs)

    def test_gap_model_and_provider_are_not_catalog_allowlisted(self):
        self.assertEqual(self.request(model="unknown/not-qualified")["body"]["model"],
                         "unknown/not-qualified")
        self.assertEqual(pilot.request("fixture/model", "unknown-provider", [], [],
            enabled=True)["body"]["provider"]["only"], ["unknown-provider"])

    def test_gap_served_identity_is_discarded_and_not_checked(self):
        result = pilot.response(200, completion(model="unexpected/model", provider="unexpected"))
        self.assertEqual(set(result), {"message", "usage"})

    def test_gap_no_redaction_or_consent_for_history_and_tool_results(self):
        # Invented markers, never personal data or secrets.
        messages = [{"role": "system", "content": "SYNTHETIC_PRIVATE_HISTORY"},
                    {"role": "tool", "tool_call_id": "fixture-1",
                     "content": "SYNTHETIC_PRIVATE_TOOL_RESULT"}]
        self.assertEqual(self.request(messages)["body"]["messages"], messages)

    def test_gap_token_ceiling_is_not_spend_budget(self):
        for _ in range(50):
            self.request(max_tokens=256)
        result = pilot.response(200, completion(usage={"cost": 999}))
        self.assertEqual(result["usage"]["cost"], 999)
        self.assertNotIn("max_price", self.request()["body"]["provider"])

    def test_gap_tool_arguments_not_schema_validated(self):
        raw = json.dumps({"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": "fixture-1",
            "type": "function", "function": {"name": "lookup_demo_item",
            "arguments": '{"unexpected":true}'}}]}}]}).encode()
        result = pilot.response(200, raw, allowed_tools={"lookup_demo_item"})
        self.assertIn("unexpected", result["message"]["tool_calls"][0]["function"]["arguments"])

    def test_gap_stream_is_buffered_and_iterator_not_closed_on_failure(self):
        class Source:
            closed = False
            def __iter__(self):
                yield b"x" * (pilot.MAX_BYTES + 1)
            def close(self):
                self.closed = True
        source = Source()
        with self.assertRaises(pilot.ContractError):
            pilot.stream_response(source)
        self.assertFalse(source.closed)

    def test_gap_raw_iterator_errors_are_not_sanitized(self):
        def broken():
            raise TimeoutError("SYNTHETIC_TRANSPORT_MARKER")
            yield b""
        with self.assertRaisesRegex(TimeoutError, "SYNTHETIC_TRANSPORT_MARKER"):
            pilot.stream_response(broken())

    def test_disabled_requires_literal_true(self):
        for enabled in (False, None, 1, "true"):
            with self.subTest(enabled=enabled), self.assertRaises(pilot.ContractError):
                pilot.request("fixture/model", "fixture-provider", [], [], enabled=enabled)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.config = ModuleType("service.config")
        self.cfg = {"inference": {"endpoints": {
            "cloud": {"base_url": "https://openrouter.ai", "credential_ref": "env:FIXTURE_ONLY"}},
            "bindings": {}}}
        self.config.models_config = lambda: self.cfg
        self.config.omlx_base_url = lambda: "http://127.0.0.1:8000"
        self.config.role_to_model = lambda role: "fixture-local"
        self.config.model_context_window = lambda model: 8000
        self.mods = patch.dict(sys.modules, {"service.config": self.config})
        self.mods.start()
        self.addCleanup(self.mods.stop)

    def test_default_target_stays_managed_local(self):
        target = endpoints.role_target("agent")
        self.assertTrue(target.endpoint.managed)
        self.assertEqual(target.endpoint.name, "local")

    def test_fast_and_router_cannot_be_cloud(self):
        for role in ("fast", "router"):
            self.cfg["inference"]["bindings"][role] = {"endpoint": "cloud"}
            with self.assertRaises(endpoints.EndpointConfigurationError):
                endpoints.role_target(role)

    def test_openrouter_api_base_path_is_rejected(self):
        self.cfg["inference"]["endpoints"]["cloud"]["base_url"] += "/api/v1"
        with self.assertRaises(endpoints.EndpointConfigurationError):
            endpoints.endpoint("cloud")

    def test_explicit_disabled_endpoint_rejected(self):
        self.cfg["inference"]["endpoints"]["cloud"]["enabled"] = False
        with self.assertRaises(endpoints.EndpointConfigurationError):
            endpoints.endpoint("cloud")

    def test_gap_named_endpoint_without_enabled_flag_is_enabled(self):
        # New cloud config must require opt-in separately; current default is true.
        self.assertEqual(endpoints.endpoint("cloud").name, "cloud")


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Event loop creation precedes this guard; no connect/bind may occur.
        self.guards = [patch.object(socket.socket, op, side_effect=AssertionError("Network prohibited"))
                       for op in ("connect", "connect_ex", "bind")]
        for guard in self.guards:
            guard.start()
            self.addCleanup(guard.stop)
        fake_httpx = ModuleType("httpx")
        fake_httpx.HTTPError = type("HTTPError", (Exception,), {})
        fake_config = ModuleType("service.config")
        fake_config.role_to_model = lambda role: "fixture-agent"
        fake_client = ModuleType("service.inference.omlx_client")
        fake_client.ModelLoadError = errors.ModelLoadError
        fake_client.OMLXClient = None
        with patch.dict(sys.modules, {"httpx": fake_httpx, "service.config": fake_config,
            "service.config.endpoints": endpoints, "service.inference.omlx_client": fake_client}):
            self.module = load("isolated_readiness", ROOT / "service/inference/readiness.py")
        ep = endpoints.Endpoint("cloud", "https://example.invalid", "fixture", readiness_timeout=.01)
        self.remote = SimpleNamespace(managed=False, target=endpoints.Target("agent", ep, "fixture/model"),
                                      ensure_only=AsyncMock(), chat=AsyncMock(return_value={"ok": True}))
        self.local = SimpleNamespace(ensure_only=AsyncMock(), chat=AsyncMock(return_value={"local": True}),
                                     aclose=AsyncMock())
        self.module.OMLXClient = lambda **kw: self.local
        self.module.role_target = lambda role: endpoints.Target(role,
            endpoints.Endpoint("local", "http://127.0.0.1:8000", "fixture", managed=True), "fixture-local")
        self.fallback = AsyncMock()
        self.turn = self.module.TurnInferenceClient(self.remote, AsyncMock(), fallback_start=self.fallback)

    async def test_readiness_timeout_falls_back_only_before_generation(self):
        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()
        self.remote.ensure_only.side_effect = stalled
        self.assertEqual(await self.turn.chat("fixture/model", []), {"local": True})
        self.remote.chat.assert_not_called()
        self.fallback.assert_awaited_once()
        await self.turn.close_fallback()
        self.local.aclose.assert_awaited_once()

    async def test_readiness_failure_with_tools_never_falls_back(self):
        self.remote.ensure_only.side_effect = errors.ModelLoadError("synthetic")
        with self.assertRaises(errors.ModelLoadError):
            await self.turn.chat("fixture/model", [], tools=[{"type": "function"}])
        self.fallback.assert_not_called()
        self.remote.chat.assert_not_called()

    async def test_generation_timeout_never_retries_or_falls_back(self):
        self.remote.chat.side_effect = TimeoutError("synthetic")
        with self.assertRaises(TimeoutError):
            await self.turn.chat("fixture/model", [])
        self.remote.chat.assert_awaited_once()
        self.fallback.assert_not_called()

    async def test_readiness_cancellation_never_falls_back(self):
        self.remote.ensure_only.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.turn.chat("fixture/model", [])
        self.fallback.assert_not_called()
        self.remote.chat.assert_not_called()

    async def test_silent_stream_cancellation_closes_inner_generator(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        async def events(*args, **kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield {"kind": "final"}
            finally:
                closed.set()
        self.remote.stream_events = events
        stream = self.turn.stream_events("fixture/model", [])
        task = asyncio.create_task(anext(stream))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        self.fallback.assert_not_called()

    async def test_early_consumer_close_closes_inner_generator(self):
        closed = []
        async def events(*args, **kwargs):
            try:
                yield {"kind": "content", "text": "synthetic partial"}
                yield {"kind": "final", "message": {"content": "synthetic"}}
            finally:
                closed.append(True)
        self.remote.stream_events = events
        stream = self.turn.stream_events("fixture/model", [])
        self.assertEqual((await anext(stream))["kind"], "content")
        await stream.aclose()
        self.assertEqual(closed, [True])

    async def test_midstream_error_has_no_final_or_fallback(self):
        async def events(*args, **kwargs):
            yield {"kind": "content", "text": "synthetic partial"}
            raise TimeoutError("synthetic")
        self.remote.stream_events = events
        seen = []
        with self.assertRaises(TimeoutError):
            async for event in self.turn.stream_events("fixture/model", []):
                seen.append(event)
        self.assertEqual([e["kind"] for e in seen], ["content"])
        self.fallback.assert_not_called()

    async def test_gap_generation_has_no_wrapper_total_deadline(self):
        entered = asyncio.Event()
        async def stalled(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.remote.chat.side_effect = stalled
        task = asyncio.create_task(self.turn.chat("fixture/model", []))
        await asyncio.wait_for(entered.wait(), 1)
        try:
            # Readiness deadline is .01; generation remains pending beyond it.
            await asyncio.wait_for(asyncio.shield(task), .03)
            self.fail("Synthetic generation unexpectedly completed")
        except TimeoutError:
            self.assertFalse(task.done())
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
