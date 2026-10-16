"""Synthetic HTTP refusals; no engine, model or provider is contacted."""
import asyncio
from types import SimpleNamespace
import unittest

import httpx

from service.inference.inference_errors import EngineGuardUnavailableError, ModelMemoryLimitError
from service.inference.omlx_client import OMLXClient


class GuardClientTests(unittest.TestCase):
    def response(self, status, marker=True):
        return httpx.Response(status, headers={"X-Wisp-Guard-Refusal": "1"} if marker else {},
                              content=b"PRIVATE-DIAGNOSTIC-CANARY", request=httpx.Request("POST", "http://test.invalid/load"))

    def test_managed_guard_error_is_fixed_without_body(self):
        with self.assertRaises(EngineGuardUnavailableError) as caught:
            OMLXClient._check_response(SimpleNamespace(managed=True), self.response(503))
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(caught.exception.reason, "engine_guard_unavailable")

    def test_changed_capacity_uses_existing_memory_error(self):
        with self.assertRaises(ModelMemoryLimitError):
            OMLXClient._check_response(SimpleNamespace(managed=True), self.response(507))

    def test_unmarked_503_does_not_claim_guard_diagnosis(self):
        with self.assertRaises(httpx.HTTPStatusError):
            OMLXClient._check_response(SimpleNamespace(managed=True), self.response(503, marker=False))

    def test_remote_cannot_claim_local_guard_refusal(self):
        with self.assertRaises(httpx.HTTPStatusError) as caught:
            OMLXClient._check_response(SimpleNamespace(managed=False), self.response(503))
        self.assertNotIsInstance(caught.exception, EngineGuardUnavailableError)
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_explicit_load_refusal_is_not_replayed(self):
        for status, expected in ((503, EngineGuardUnavailableError), (507, ModelMemoryLimitError)):
            with self.subTest(status=status):
                calls = []
                async def post(path):
                    calls.append(path)
                    return self.response(status)
                client = OMLXClient.__new__(OMLXClient)
                client.managed = True
                client._client = SimpleNamespace(post=post)
                with self.assertRaises(expected):
                    asyncio.run(client.load("synthetic-model"))
                self.assertEqual(calls, ["/v1/models/synthetic-model/load"])

    def test_chat_and_stream_refuse_before_output_without_replay(self):
        async def run(streaming):
            calls, output = [], []
            def reply(request):
                calls.append(request.url.path)
                return self.response(503)
            client = OMLXClient.__new__(OMLXClient)
            client.managed = True
            client.api_prefix = "/v1"
            client._fit_request = lambda m, messages, tools, tokens, **kw: (m, messages, tools, tokens)
            client._payload = lambda *a, **kw: {"model": "synthetic-model"}
            client._client = httpx.AsyncClient(base_url="http://test.invalid", transport=httpx.MockTransport(reply))
            try:
                with self.assertRaises(EngineGuardUnavailableError) as caught:
                    if streaming:
                        async for event in client.stream_events("synthetic-model", []):
                            output.append(event)
                    else:
                        await client.chat("synthetic-model", [])
                self.assertEqual(output, [])
                self.assertEqual(calls, ["/v1/chat/completions"])
                self.assertNotIn("PRIVATE", str(caught.exception))
            finally:
                await client._client.aclose()
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                asyncio.run(run(streaming))

    def test_optional_warmup_refusal_preserves_the_requested_resident_model(self):
        calls = []
        client = OMLXClient.__new__(OMLXClient)
        client.managed = True
        client._keep_warm = {"optional"}
        async def loaded():
            return ["primary"]
        async def post(path):
            calls.append(path)
            return self.response(503)
        client.loaded_models = loaded
        client._client = SimpleNamespace(post=post)
        asyncio.run(client.ensure_only("primary", settle_timeout=0.1))
        self.assertEqual(calls, ["/v1/models/optional/load"])


if __name__ == "__main__":
    unittest.main()
