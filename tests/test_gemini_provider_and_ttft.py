"""The Gemini API as a second cloud provider, and a quicker first token on the cloud model.

Synthetic only: run with a fresh WISP_HOME; sockets are forbidden and no Keychain item
is read. Every transport is a fake. Covers:
- the Gemini profile (Google's origin and prefix only) and its labels;
- its payload shape (`reasoning_effort`, never OpenRouter's `reasoning` or `provider`);
- thought signatures (`extra_content` on each tool call) captured, streamed and whole,
  and sent back unchanged on the next request of the turn, and nowhere else;
- the shared keep-alive pool, the warm-up endpoint, the readiness proof, no-tools
  streaming, OpenRouter's provider routing and prompt-cache mark, all only under
  cloud-everything; everything else unchanged.
"""
import asyncio
import contextlib
import copy
import io
import json
import logging
import os
from unittest.mock import patch

import httpcore
import httpx
import pytest

from tests.test_remote_inference_policy import RemoteFixture, policy, EndpointConfigurationError
from tests.test_remote_reasoning_replay import (ReplayBase, TOOL, TOOL_NAME, _Records, sse,
                                                streamed_answer)
from service.config.endpoints import endpoint_from_config
from service.inference import attributed_transport as at
from service.inference import omlx_client
from service.inference.omlx_client import (OMLXClient, SanitizedHTTPStatusError, replay_tool_calls,
                                           without_replay_reasoning)
from service.inference.providers import (GEMINI_API_PREFIX, GEMINI_ORIGIN, OPENROUTER_ORIGIN,
                                         wire_provider)

SIG = "SIGNATURE-PRIVATE-MARKER-41ab" + "Q" * 200
SIG2 = "SIGNATURE-PRIVATE-MARKER-41ab-second"
EXTRA_KEY = policy.REPLAY_TOOL_EXTRA_KEY
GEMINI = {"base_url": GEMINI_ORIGIN, "api_prefix": GEMINI_API_PREFIX, "model_id": "gemini-3.8-flash",
          "context_window": 32000, "max_tokens": 8192, "request_bytes": 524288,
          "messages": 200, "aggregate_bytes": 131072}
OPENROUTER = {**GEMINI, "base_url": OPENROUTER_ORIGIN, "api_prefix": "/api/v1",
              "model_id": "google/gemini-3.8-flash"}


def extra(sig=SIG):
    return {"google": {"thought_signature": sig}}


def gemini_calls(parallel=False, ids=True):
    names = ["a", "b"] if parallel else ["a"]
    out = []
    for i, n in enumerate(names):
        call = {"type": "function", "function": {"name": TOOL_NAME, "arguments": json.dumps({"q": n})}}
        if ids:
            call["id"] = f"call_{n}"
        if i == 0:                              # Gemini signs the first call of a step
            call["extra_content"] = extra()
        out.append(call)
    return out


def gemini_tool_reply(parallel=False, ids=True):
    return {"choices": [{"message": {"role": "assistant", "content": None,
                                     "tool_calls": gemini_calls(parallel, ids)},
                         "finish_reason": "stop"}]}


def gemini_streamed_tool_reply(parallel=False, ids=True):
    # Each call whole on one chunk, no index, ended with "stop" (Gemini's shape).
    chunks = [{"choices": [{"delta": {"role": "assistant", "tool_calls": [call]},
                            "finish_reason": None}]} for call in gemini_calls(parallel, ids)]
    chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    return chunks


def answer(text="Done."):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


class CloudBase(ReplayBase):
    """A real OMLXClient on a remote-only profile; `everything(True)` adds parity."""
    profile_overrides = GEMINI

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("WISP_REMOTE_PARITY", None)
        omlx_client._REMOTE_READY.clear()
        self.addCleanup(omlx_client._REMOTE_READY.clear)

    def everything(self, on=True):
        self.cfg["remote_parity"] = on

    def use(self, overrides, **more):
        self.profile.update(overrides, **more)
        self.cfg["inference"]["remote_only"].update(overrides, **more)
        policy.remote_only_profile.cache_clear()

    def run_client(self, replies, go):
        client = self.client(replies)

        async def wrapped():
            try:
                return await go(client)
            finally:
                await client.aclose()
        return asyncio.run(wrapped())


# --- the profile ----------------------------------------------------------------

class GeminiProfile(RemoteFixture):
    def endpoint_cfg(self, **changes):
        return {"enabled": True, "provider": "gemini", "base_url": GEMINI_ORIGIN,
                "api_prefix": GEMINI_API_PREFIX, "credential_ref": "keychain:gemini", **changes}

    def test_google_origin_and_prefix_only(self):
        ep = endpoint_from_config("cloud", self.endpoint_cfg())
        self.assertEqual((ep.provider, ep.base_url, ep.api_prefix), ("gemini", GEMINI_ORIGIN, GEMINI_API_PREFIX))
        self.assertEqual(endpoint_from_config("cloud", self.endpoint_cfg(base_url=GEMINI_ORIGIN + "/")).base_url,
                         GEMINI_ORIGIN)
        for changes in ({"base_url": "http://generativelanguage.googleapis.com"},
                        {"base_url": "https://generativelanguage.googleapis.com.evil.example"},
                        {"base_url": "https://evil.example"},
                        {"base_url": "https://GENERATIVELANGUAGE.googleapis.com:444"},
                        {"base_url": OPENROUTER_ORIGIN},
                        {"api_prefix": "/v1beta"}, {"api_prefix": "/v1"}, {"api_prefix": "/api/v1"}):
            with self.subTest(changes=changes), self.assertRaises(EndpointConfigurationError):
                endpoint_from_config("cloud", self.endpoint_cfg(**changes))

    def test_wire_is_told_from_the_exact_origin(self):
        self.assertEqual(wire_provider("openai-compatible", GEMINI_ORIGIN), "gemini")
        self.assertEqual(wire_provider("openai-compatible", OPENROUTER_ORIGIN), "openrouter")
        self.assertEqual(wire_provider("openai-compatible", "https://generativelanguage.googleapis.com.x"),
                         "openai-compatible")
        self.assertEqual(wire_provider("omlx", GEMINI_ORIGIN), "omlx")
        self.assertEqual(policy.profile_from_config({**self.profile, **GEMINI}).wire, "gemini")
        self.assertEqual(policy.remote_only_profile().wire, "openai-compatible")

    def test_tables_hold_the_bare_ids(self):
        for model in policy.GEMINI_API_MODELS:
            self.assertEqual(policy.remote_context_window(model), 1_000_000)
            self.assertEqual(policy.remote_input_modalities(model),
                             frozenset({"text", "image", "video", "file", "audio"}))
            self.assertEqual(policy.REASONING_EFFORT_MODELS[model], "low")
            self.assertEqual(policy.reasoning_fields("gemini", model), {"reasoning_effort": "low"})
            self.assertEqual(policy.reasoning_fields("openrouter", model), {})
            self.assertEqual(policy.reasoning_fields("openai-compatible", model), {})
        self.assertNotIn("gemini-3.8-flash", policy.REPLAY_REASONING_MODELS)
        self.assertEqual(policy.reasoning_fields("gemini", "google/gemini-3.8-flash"), {})
        for gate, expected in ((False, {}), (True, {"reasoning": {"effort": "low"}})):
            with patch.object(policy, "cloud_everything_active", lambda gate=gate: gate):
                # OpenRouter's Gemini ids reason under cloud-everything only (NOTE 1).
                self.assertEqual(policy.reasoning_fields("openrouter", "google/gemini-3.8-flash"), expected)
                self.assertEqual(policy.reasoning_fields("gemini", "gemini-3.8-flash"),
                                 {"reasoning_effort": "low"})

    def test_connect_accepts_gemini_and_its_listing_proves_the_key(self):
        from service import main
        from tests.test_cloud_probe_openrouter import Probe
        probe = Probe("gemini")
        self.assertTrue(asyncio.run(main._cloud_model_answers(probe, "gemini-3.8-flash",
                                                              ["models/gemini-3.8-flash"])))
        self.assertTrue(asyncio.run(main._cloud_model_answers(probe, "gemini-3.8-flash", ["gemini-3.8-flash"])))
        self.assertEqual(probe.chats, 0)
        bad = Probe("gemini", answer=False)
        self.assertFalse(asyncio.run(main._cloud_model_answers(bad, "gemini-3.8-flash", [])))
        from fastapi import HTTPException
        body = {"provider": "gemini", "base_url": "https://evil.example", "api_prefix": GEMINI_API_PREFIX,
                "model_id": "gemini-3.8-flash", "context_window": 1_000_000, "credential_name": "gemini",
                "roles": ["everything"]}
        with patch("service.config.provider_credentials.resolve_keychain",
                   side_effect=AssertionError("never read for a refused host")):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(main.connect_cloud_inference(body))
        self.assertEqual(raised.exception.status_code, 400)

    def test_settings_label(self):
        from service import config
        self.cfg = {"inference": {"remote_only": copy.deepcopy(self.profile),
                                  "endpoints": {"cloud": self.endpoint_cfg()}}}
        policy.remote_only_profile.cache_clear()
        self.assertEqual(config.cloud_provider_settings()["provider_label"], "Gemini API")


# --- payload shape and validation -------------------------------------------------

class GeminiPayload(CloudBase):
    def body(self, **extra_fields):
        return {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "x"}],
                "stream": True, "max_tokens": 3000, **extra_fields}

    def test_reasoning_effort_not_reasoning_nor_provider(self):
        def go(client):
            async def steps():
                [e async for e in client.stream_events(client.target.model,
                                                       [{"role": "user", "content": "x"}],
                                                       tools=[TOOL], max_tokens=3000)]
            return steps()
        for parity in (False, True):
            with self.subTest(parity=parity):
                self.everything(parity)
                policy.remote_only_profile.cache_clear()
                self.run_client([streamed_answer("ok")], go)
                body = self.bodies[0]
                self.assertTrue(body["stream"])               # the Gemini API streams
                self.assertEqual(body["reasoning_effort"], "low")
                self.assertNotIn("reasoning", body)
                self.assertNotIn("provider", body)

    def test_validate_payload_per_provider(self):
        p = policy.remote_only_profile()
        p.validate_payload(self.body(reasoning_effort="low"))
        for bad in ({"reasoning": {"effort": "low"}}, {"provider": {"sort": "latency"}},
                    {"reasoning_effort": "high"}, {"reasoning_effort": "minimal"},
                    {"reasoning_effort": None}, {"reasoning_effort": {"effort": "low"}}):
            with self.subTest(bad=bad), self.assertRaises(EndpointConfigurationError):
                p.validate_payload(self.body(**bad))
        with self.assertRaises(EndpointConfigurationError):
            p.validate_payload(self.body(reasoning_effort="low", max_tokens=2048))
        router = policy.profile_from_config({**self.profile, **OPENROUTER})
        rbody = {**self.body(), "model": OPENROUTER["model_id"]}
        # OpenRouter's Gemini reasoning is cloud-everything only (review 2 NOTE 1).
        with self.assertRaises(EndpointConfigurationError):
            router.validate_payload({**rbody, "reasoning": {"effort": "low"}})
        self.everything(True)
        router.validate_payload({**rbody, "provider": {"sort": "latency"}, "reasoning": {"effort": "low"}})
        for bad in ({"reasoning_effort": "low"}, {"provider": {"sort": "price"}},
                    {"provider": {"sort": "latency", "only": ["x"]}}):
            with self.subTest(bad=bad), self.assertRaises(EndpointConfigurationError):
                router.validate_payload({**rbody, **bad})
        other = policy.profile_from_config({**self.profile, "base_url": "https://inference.example",
                                            "api_prefix": "/v1"})
        for bad in ({"reasoning_effort": "low"}, {"provider": {"sort": "latency"}}):
            with self.subTest(other=bad), self.assertRaises(EndpointConfigurationError):
                other.validate_payload({**self.body(), **bad})

    def test_extra_content_only_on_gemini_assistant_calls(self):
        call = {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"},
                "extra_content": extra()}
        rows = [{"role": "user", "content": "x"},
                {"role": "assistant", "content": "", "tool_calls": [call]},
                {"role": "tool", "tool_call_id": "c1", "content": "r"}]
        p = policy.remote_only_profile()
        p.validate_payload(self.body(messages=rows))
        for value in ({"google": {"thought_signature": ""}}, {"google": {"thought_signature": 5}},
                      {"google": "x"}, {"other": {}}, "sig", None, [extra()]):
            bad = copy.deepcopy(rows)
            bad[1]["tool_calls"][0]["extra_content"] = value
            with self.subTest(value=value), self.assertRaises(EndpointConfigurationError):
                p.validate_payload(self.body(messages=bad))
        held = copy.deepcopy(rows)
        held[1]["tool_calls"][0][EXTRA_KEY] = "x"
        with self.assertRaises(EndpointConfigurationError):
            p.validate_payload(self.body(messages=held))
        other = policy.profile_from_config({**self.profile, **OPENROUTER})
        with self.assertRaises(EndpointConfigurationError):
            other.validate_payload({**self.body(messages=rows), "model": OPENROUTER["model_id"]})
        # Signatures count against the replay cap (response_bytes), not the transcript.
        tight = policy.profile_from_config({**self.profile, "aggregate_bytes": 1024, "request_bytes": 4096,
                                            "response_bytes": 2048})
        big = copy.deepcopy(rows)
        big[1]["tool_calls"][0]["extra_content"] = extra("s" * 1500)
        tight.validate_payload(self.body(messages=big))
        big[1]["tool_calls"][0]["extra_content"] = extra("s" * 3000)
        with self.assertRaisesRegex(EndpointConfigurationError, "too large to continue"):
            tight.validate_payload(self.body(messages=big))


# --- thought signatures -------------------------------------------------------------

class GeminiSignatures(CloudBase):
    def two_step(self, via_stream, parallel, ids=True):
        first_reply = (gemini_streamed_tool_reply(parallel, ids) if via_stream
                       else gemini_tool_reply(parallel, ids))
        second_reply = streamed_answer("Done.") if via_stream else answer("Done.")
        ask = [{"role": "user", "content": "Look it up"}]

        async def go(client):
            async def call(messages):
                if via_stream:
                    events = [e async for e in client.stream_events(client.target.model, messages,
                                                                    tools=[TOOL], max_tokens=3000)]
                    self.assertNotIn(SIG, repr(events))
                    return events[-1]["message"]
                return (await client.chat(client.target.model, messages, tools=[TOOL],
                                          max_tokens=3000))["choices"][0]["message"]
            first = await call(ask)
            self.assertTrue(all("extra_content" not in c for c in first["tool_calls"]))
            self.assertNotIn(SIG, repr(first) + str(first))
            self.assertNotIn(SIG, json.dumps(without_replay_reasoning(first), default=repr))
            row_calls = replay_tool_calls(client, first, first["tool_calls"])
            row = {"role": "assistant", "content": "", "tool_calls": row_calls}
            results = [{"role": "tool", "tool_call_id": c["id"], "content": "result"} for c in row_calls]
            await call(ask + [row, *results])
            return first
        first = self.run_client([first_reply, second_reply], go)
        self.assertEqual([b["stream"] for b in self.bodies], [via_stream] * 2)
        self.assertNotIn(b"extra_content", self.raw[0])
        sent = [m for m in self.bodies[1]["messages"] if m.get("tool_calls")]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["tool_calls"][0]["extra_content"], extra())
        self.assertEqual(len(sent[0]["tool_calls"]), 2 if parallel else 1)
        if parallel:
            self.assertNotIn("extra_content", sent[0]["tool_calls"][1])   # only the signed call
        self.assertIn(json.dumps(extra(), separators=(",", ":")).encode(), self.raw[1].replace(b" ", b""))
        return first

    def test_captured_and_replayed_streamed_and_whole(self):
        for via_stream in (True, False):
            for parallel in (False, True):
                with self.subTest(stream=via_stream, parallel=parallel):
                    self.two_step(via_stream, parallel)

    def test_calls_without_ids_still_carry_their_signature(self):
        first = self.two_step(True, False, ids=False)
        self.assertEqual(first["tool_calls"][0]["id"], "call_0")

    def test_a_step_without_a_signature_gets_googles_skip_value(self):
        rows = [{"role": "user", "content": "x"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "picture_0", "type": "function", "function": {"name": TOOL_NAME, "arguments": "{}"}},
                    {"id": "picture_1", "type": "function", "function": {"name": TOOL_NAME, "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "picture_0", "content": "r"},
                {"role": "tool", "tool_call_id": "picture_1", "content": "r"}]
        original = copy.deepcopy(rows)

        async def go(client):
            [e async for e in client.stream_events(client.target.model, rows, tools=[TOOL], max_tokens=3000)]
        self.run_client([streamed_answer("ok")], go)
        self.assertEqual(rows, original)                         # the caller's list is untouched
        sent = self.bodies[0]["messages"][1]["tool_calls"]
        self.assertEqual(sent[0]["extra_content"], extra(policy.GEMINI_SKIP_SIGNATURE))
        self.assertNotIn("extra_content", sent[1])

    def test_other_providers_never_receive_a_signature(self):
        self.use(OPENROUTER)
        rows = [{"role": "user", "content": "x"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c", "type": "function", "function": {"name": TOOL_NAME, "arguments": "{}"},
                     "extra_content": extra()}]},
                {"role": "tool", "tool_call_id": "c", "content": "r"}]

        async def go(client):
            await client.chat(client.target.model, rows, tools=[TOOL], max_tokens=3000)
        self.run_client([answer()], go)
        self.assertNotIn(b"extra_content", self.raw[0])
        self.assertNotIn(SIG.encode(), self.raw[0])

    def test_a_reply_from_another_provider_holds_nothing(self):
        self.use(OPENROUTER)

        async def go(client):
            return (await client.chat(client.target.model, [{"role": "user", "content": "x"}],
                                      tools=[TOOL], max_tokens=3000))["choices"][0]["message"]
        message = self.run_client([gemini_tool_reply()], go)
        self.assertNotIn(EXTRA_KEY, message)

    def test_oversized_signature_is_refused(self):
        self.use({}, response_bytes=1024)

        async def go(client):
            return [e async for e in client.stream_events(client.target.model,
                                                          [{"role": "user", "content": "x"}],
                                                          tools=[TOOL], max_tokens=3000)]
        reply = gemini_streamed_tool_reply()
        reply[0]["choices"][0]["delta"]["tool_calls"][0]["extra_content"] = extra("z" * 70_000)
        with self.assertRaisesRegex(Exception, "allowed size"):
            self.run_client([reply], go)


class GeminiAgentTurn(CloudBase):
    """A whole tool turn through the agent loop, streamed."""

    def setUp(self):
        super().setUp()
        from service import main  # noqa: F401 - registers the real tools
        from service.agent import loop
        from service.safety.policy import Decision, Tier
        from service.tools.registry import REGISTRY, Tool
        self.loop = loop
        self.tool_runs = []

        async def lookup(**kwargs):
            self.tool_runs.append(kwargs)
            return f"synthetic result {kwargs.get('q')}"
        tool = Tool(TOOL_NAME, "Synthetic lookup.", TOOL["function"]["parameters"], "read", lookup)
        for p in (patch.dict(REGISTRY, {TOOL_NAME: tool}),
                  patch.object(loop, "decide", lambda *_a, **_k: Decision(Tier.ALLOW, "synthetic")),
                  patch.object(loop, "audit", lambda *_a, **_k: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_turn_replays_the_signature_and_shows_it_nowhere(self):
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                client = self.client([gemini_streamed_tool_reply(parallel), streamed_answer("Final.")])
                events, out, err, records = [], io.StringIO(), io.StringIO(), _Records()

                async def emit(event):
                    events.append(event)

                class Approver:
                    async def confirm(self, *_a, **_k):
                        return True

                async def go():
                    try:
                        return await self.loop.run_agent(
                            client, client.target.model, [{"role": "user", "content": "Look it up"}],
                            emit, Approver(), tools=[TOOL_NAME], include_memory_context=False,
                            include_identity_context=False, multi_round=True, max_steps=3, debug=True)
                    finally:
                        await client.aclose()
                logging.getLogger().addHandler(records)
                try:
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        final = asyncio.run(go())
                finally:
                    logging.getLogger().removeHandler(records)
                self.assertEqual(final, "Final.")
                self.assertEqual(len(self.bodies), 2)
                self.assertTrue(all(b["stream"] for b in self.bodies))
                rows = [m for m in self.bodies[1]["messages"] if m.get("tool_calls")]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["tool_calls"][0]["extra_content"], extra())
                for surface in (json.dumps(events, default=repr), out.getvalue(), err.getvalue(),
                                "\n".join(records.lines)):
                    self.assertNotIn(SIG, surface)
                    self.assertNotIn("extra_content", surface)
                    self.assertNotIn(EXTRA_KEY, surface)


# --- errors -----------------------------------------------------------------------

class GeminiErrors(CloudBase):
    def test_statuses_read_the_same_as_openrouter(self):
        from service.errors import translate
        messages = {}
        with patch.object(omlx_client._RateLimitRetry, "DELAYS", (0.0, 0.0, 0.0)):
            for overrides in (GEMINI, OPENROUTER):
                self.use(overrides)
                for status in (400, 401, 403, 429):
                    gemini_body = [{"error": {"code": status, "message": "API key not valid: AIza" + "x" * 35,
                                              "status": "INVALID_ARGUMENT"}}]

                    def handle(request, status=status, gemini_body=gemini_body):
                        return httpx.Response(status, json=gemini_body)
                    with patch("service.inference.attributed_transport.CredentialTransport",
                               return_value=httpx.MockTransport(handle)):
                        client = OMLXClient(target=policy.remote_default_target("agent"))

                    async def go(client=client):
                        try:
                            await client.chat(client.target.model, [{"role": "user", "content": "x"}])
                        finally:
                            await client.aclose()
                    with self.assertRaises(SanitizedHTTPStatusError) as raised:
                        asyncio.run(go())
                    text, detail = translate(raised.exception, endpoint_name="cloud")
                    self.assertNotIn("AIza", text + detail)
                    messages.setdefault(status, set()).add(text)
        self.assertTrue(all(len(texts) == 1 for texts in messages.values()), messages)


# --- shared keep-alive pool --------------------------------------------------------

class FakePool:
    def __init__(self, created, failures=()):
        self.closed = False
        self.requests = 0
        self.failures = failures
        created.append(self)

    async def handle_async_request(self, request):
        self.requests += 1
        if self.failures:
            raise self.failures.pop(0)
        return httpcore.Response(200, headers=[], content=b"{}")

    async def aclose(self):
        self.closed = True


@pytest.fixture
def fake_pools(monkeypatch):
    created, failures = [], []
    monkeypatch.setattr(at, "_shared_remote_pool", lambda: FakePool(created, failures))
    monkeypatch.setattr(at, "_SHARED_POOLS", {})
    return created, failures


def _request(path="/api/v1/x"):
    return httpx.Request("POST", OPENROUTER_ORIGIN + path, content=b"{}")


def test_shared_pool_reused_across_clients_and_kept_open(fake_pools):
    created, _ = fake_pools

    async def go():
        one = at.CredentialTransport(OPENROUTER_ORIGIN, "k", managed=False, shared=True)
        two = at.CredentialTransport(OPENROUTER_ORIGIN, "k", managed=False, shared=True)
        await one.handle_async_request(_request())
        await one.aclose()                                   # a turn's routine close
        await two.handle_async_request(_request())
        await two.aclose()
        three = at.CredentialTransport(OPENROUTER_ORIGIN, "k", managed=False, shared=True)
        await three.handle_async_request(_request())
        await three.aclose()
    asyncio.run(go())
    assert len(created) == 1 and created[0].requests == 3 and not created[0].closed


def test_network_error_retires_the_pool_and_retries_a_stale_connection_once(fake_pools):
    created, failures = fake_pools

    async def go():
        t = at.CredentialTransport(OPENROUTER_ORIGIN, "k", managed=False, shared=True)
        await (await t.handle_async_request(_request())).aclose()
        failures.append(httpcore.RemoteProtocolError("Server disconnected"))
        response = await t.handle_async_request(_request())  # retried on a fresh pool
        assert response.status_code == 200
        await response.aclose()
        assert len(created) == 2 and created[0].closed and not created[1].closed
        failures.extend([httpcore.ConnectTimeout("x")])
        with pytest.raises(httpx.ConnectError):              # never retried
            await t.handle_async_request(_request())
        failures.extend([httpcore.ReadError("a"), httpcore.ReadError("b")])
        with pytest.raises(httpx.ConnectError):              # retried once, not twice
            await t.handle_async_request(_request())
        await t.aclose()
    asyncio.run(go())
    assert all(p.closed for p in created[:-1])


def test_one_failure_never_closes_a_pool_another_request_is_still_reading(fake_pools):
    created, failures = fake_pools

    async def go():
        # One long-lived client (main.py's) carries both requests: E's only user.
        a = at.CredentialTransport(OPENROUTER_ORIGIN, "k", managed=False, shared=True)
        x = await a.handle_async_request(_request())         # X streams on pool E
        failures.append(httpcore.ReadError("stale"))
        y = await a.handle_async_request(_request())         # Y fails on E, retried on F
        assert y.status_code == 200 and len(created) == 2
        old, fresh = created
        assert old.requests == 2 and fresh.requests == 1
        assert not old.closed and not fresh.closed           # X is still reading E
        await x.aclose()                                     # X completes
        assert old.closed and not fresh.closed
        await y.aclose()
        await a.aclose()
        assert not fresh.closed                              # a routine close keeps it
    asyncio.run(go())


def test_a_second_failure_never_closes_the_fresh_pool(fake_pools):
    created, _ = fake_pools
    gate = None

    class Stale(FakePool):
        async def handle_async_request(self, request):
            self.requests += 1
            if self is created[0]:                           # both requests meet stale E
                await gate.wait()
                raise httpcore.ReadError("stale")
            return httpcore.Response(200, headers=[], content=b"{}")

    async def go():
        nonlocal gate
        gate = asyncio.Event()
        with patch.object(at, "_shared_remote_pool", lambda: Stale(created)):
            a = at.CredentialTransport(OPENROUTER_ORIGIN, "k", managed=False, shared=True)
            b = at.CredentialTransport(OPENROUTER_ORIGIN, "k", managed=False, shared=True)
            both = asyncio.gather(a.handle_async_request(_request()), b.handle_async_request(_request()))
            await asyncio.sleep(0)
            gate.set()
            one, two = await both
            assert one.status_code == two.status_code == 200
            assert len(created) == 2                         # both retried on the same fresh pool
            old, fresh = created
            assert old.closed and not fresh.closed and fresh.requests == 2
            await one.aclose()
            await two.aclose()
            assert not fresh.closed
            await a.aclose()
            await b.aclose()
    asyncio.run(go())


def test_only_remote_https_shares_and_others_keep_no_keepalive(fake_pools):
    created, _ = fake_pools
    for kwargs in ({"managed": True, "shared": True}, {"managed": False, "shared": False}):
        t = at.CredentialTransport("http://127.0.0.1:9123", "k", **kwargs)
        assert not t.shared and t.pool._max_keepalive_connections == 0
    local = at.CredentialTransport("http://127.0.0.1:9123", "k", managed=False, shared=True)
    assert not local.shared and local.pool._max_keepalive_connections == 0
    remote = at.CredentialTransport(OPENROUTER_ORIGIN, "k", managed=False)
    assert not remote.shared and remote.pool._max_keepalive_connections == 0
    assert created == []


def test_real_shared_pool_keeps_connections_alive():
    pool = at._shared_remote_pool()
    assert pool._max_keepalive_connections == at.SHARED_KEEPALIVE_CONNECTIONS == 4
    assert pool._keepalive_expiry == at.SHARED_KEEPALIVE_EXPIRY_S == 90.0


def test_origin_and_authorization_unchanged_on_the_shared_pool(fake_pools):
    seen = []

    class Recording(FakePool):
        async def handle_async_request(self, request):
            seen.append(list(request.headers))
            return await super().handle_async_request(request)
    created, _ = fake_pools
    with patch.object(at, "_shared_remote_pool", lambda: Recording(created)):
        async def go():
            t = at.CredentialTransport(OPENROUTER_ORIGIN, "secret-key", managed=False, shared=True)
            req = _request()
            req.headers["Authorization"] = "Bearer injected"
            await t.handle_async_request(req)
            with pytest.raises(Exception):
                await t.handle_async_request(httpx.Request("POST", GEMINI_ORIGIN + "/x", content=b"{}"))
            await t.aclose()
        asyncio.run(go())
    assert len(seen) == 1                                    # the other origin never reached a pool
    assert [v for k, v in seen[0] if k.lower() == b"authorization"] == [b"Bearer secret-key"]


class SharedGate(CloudBase):
    def test_client_shares_only_under_cloud_everything(self):
        for parity in (False, True):
            with self.subTest(parity=parity):
                self.everything(parity)
                policy.remote_only_profile.cache_clear()
                with patch("service.inference.attributed_transport.CredentialTransport",
                           return_value=httpx.MockTransport(lambda r: httpx.Response(200))) as made:
                    OMLXClient(target=policy.remote_default_target("agent"))
                self.assertIs(made.call_args.kwargs["shared"], parity)


# --- warm-up, readiness, timeouts ---------------------------------------------------

class Warm(CloudBase):
    def setUp(self):
        super().setUp()
        # Importing main lays out WISP_HOME's state folder, which the credential gate
        # needs before any remote request; without it each test depends on order.
        from service import main  # noqa: F401

    def warm_paths(self):
        paths = []

        def handle(request):
            paths.append((request.method, request.url.path))
            return httpx.Response(200, json={"data": [{"id": "models/gemini-3.8-flash"}]})
        with patch("service.inference.attributed_transport.CredentialTransport",
                   return_value=httpx.MockTransport(handle)):
            client = OMLXClient(target=policy.remote_default_target("agent"))

        async def go():
            try:
                await client.warm()
            finally:
                await client.aclose()
        asyncio.run(go())
        return paths

    def test_warm_request_per_provider(self):
        self.everything(True)
        self.assertEqual(self.warm_paths(), [("GET", "/v1beta/openai/models")])
        self.use(OPENROUTER)
        self.assertEqual(self.warm_paths(), [("HEAD", "/api/v1")])     # never the large list

    def test_endpoint_gate_and_rate_limit(self):
        from service import main
        runs = []

        async def fake_warm():
            runs.append(1)
        with patch.object(main, "_warm_cloud_connection", fake_warm), \
                patch.object(main, "_warm_last", None), patch.object(main, "_warm_task", None):
            async def go():
                answers = [await main.warm_cloud_inference()]
                self.everything(True)
                answers.append(await main.warm_cloud_inference())
                await main._warm_task
                answers.append(await main.warm_cloud_inference())       # within 20 s
                main._warm_last -= main._WARM_INTERVAL_S + 1
                answers.append(await main.warm_cloud_inference())
                await main._warm_task
                return answers
            answers = asyncio.run(go())
        self.assertEqual([a["warming"] for a in answers], [False, True, False, True])
        self.assertEqual(answers[0]["reason"], "not_cloud")
        self.assertEqual(answers[2]["reason"], "recent")
        self.assertEqual(len(runs), 2)

    def test_warm_task_is_bounded_and_swallows_failures(self):
        from service import main
        self.everything(True)

        class Slow:
            def __init__(self, **_k):
                self.closed = False

            async def warm(self):
                await asyncio.sleep(60)

            async def aclose(self):
                self.closed = True
        made = []
        with patch.object(main, "OMLXClient", lambda **k: made.append(Slow()) or made[-1]), \
                patch.object(main, "_WARM_TIMEOUT_S", 0.05):
            asyncio.run(main._warm_cloud_connection())
        self.assertTrue(made[0].closed)

    def test_readiness_read_once_under_cloud_everything(self):
        for parity, expected in ((False, 4), (True, 1)):
            with self.subTest(parity=parity):
                omlx_client._REMOTE_READY.clear()
                self.everything(parity)
                policy.remote_only_profile.cache_clear()
                reads = []

                def handle(request):
                    reads.append(request.url.path)
                    return httpx.Response(200, json={"data": [{"id": "gemini-3.8-flash"}]})
                for _turn in range(2):
                    with patch("service.inference.attributed_transport.CredentialTransport",
                               return_value=httpx.MockTransport(handle)):
                        client = OMLXClient(target=policy.remote_default_target("agent"))

                    async def go(client=client):
                        try:
                            await client.health()
                            await client.ensure_only(client.target.model)
                        finally:
                            await client.aclose()
                    asyncio.run(go())
                self.assertEqual(len(reads), expected)

    def test_warm_always_sends_while_readiness_is_fresh(self):
        # Review 2 S3: the kept-alive connection expires at 90 s, the proof at 300 s.
        omlx_client._REMOTE_READY.clear()
        self.everything(True)
        self.assertEqual(self.warm_paths(), [("GET", "/v1beta/openai/models")])
        self.assertEqual(len(omlx_client._REMOTE_READY), 1)                # the proof is fresh
        self.assertEqual(self.warm_paths(), [("GET", "/v1beta/openai/models")])

    def test_a_refused_key_forgets_the_readiness_proof(self):
        for status in (401, 403, 500):
            with self.subTest(status=status):
                omlx_client._REMOTE_READY.clear()
                self.everything(True)
                policy.remote_only_profile.cache_clear()

                def handle(request, status=status):
                    if request.url.path.endswith("/models"):
                        return httpx.Response(200, json={"data": [{"id": "gemini-3.8-flash"}]})
                    return httpx.Response(status, json={"error": "no"})
                with patch("service.inference.attributed_transport.CredentialTransport",
                           return_value=httpx.MockTransport(handle)):
                    client = OMLXClient(target=policy.remote_default_target("agent"))

                async def go(client=client):
                    try:
                        await client.health()
                        self.assertEqual(len(omlx_client._REMOTE_READY), 1)
                        with self.assertRaises(SanitizedHTTPStatusError):
                            await client.chat(client.target.model, [{"role": "user", "content": "x"}])
                    finally:
                        await client.aclose()
                asyncio.run(go())
                self.assertEqual(len(omlx_client._REMOTE_READY), 0 if status in (401, 403) else 1)


def test_timeout_scales_with_input_only_under_the_gate(monkeypatch):
    monkeypatch.setattr(policy, "cloud_everything_active", lambda: False)
    assert policy.cloud_call_timeout(12.0, 4096, input_chars=500_000) == 12.0
    monkeypatch.setattr(policy, "cloud_everything_active", lambda: True)
    base = 4096 / 150 + 15
    assert policy.cloud_call_timeout(12.0, 4096) == pytest.approx(base)
    # 500k characters, about 125k tokens of prefill: five more seconds.
    assert policy.cloud_call_timeout(12.0, 4096, input_chars=500_000) == pytest.approx(base + 5)
    assert policy.cloud_call_timeout(12.0, 4096, input_chars=10**10) == pytest.approx(
        base + policy.CLOUD_PREFILL_MAX_S)
    for junk in (-5, None, "500000", 2.5):
        assert policy.cloud_call_timeout(12.0, 4096, input_chars=junk) == pytest.approx(base)


def test_startup_upgrade_is_intel_only(monkeypatch):
    import service.config as config
    from tests.test_cloud_scale_up_core import legacy_cfg
    monkeypatch.setattr(config, "_intel_mac", lambda: False)
    assert config.upgraded_cloud_profile(legacy_cfg()) is None
    import platform
    monkeypatch.setattr(config, "_intel_mac", lambda: platform.machine() == "x86_64")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    assert config.upgraded_cloud_profile(legacy_cfg()) is None
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert config.upgraded_cloud_profile(legacy_cfg())["context_window"] == 1_000_000
    gemini = legacy_cfg(base_url=GEMINI_ORIGIN, api_prefix=GEMINI_API_PREFIX, model_id="gemini-3.8-flash")
    assert config.upgraded_cloud_profile(gemini)["context_window"] == 1_000_000


# --- streaming without tools, provider routing, prompt cache -------------------------

class OpenRouterTTFT(CloudBase):
    profile_overrides = OPENROUTER

    def events_for(self, replies, tools):
        async def go(client):
            return [e async for e in client.stream_events(client.target.model,
                                                          [{"role": "system", "content": "Rules."},
                                                           {"role": "user", "content": "x"}],
                                                          tools=tools, max_tokens=3000)]
        return self.run_client(replies, go)

    def test_no_tools_streams_for_replay_models_only_under_the_gate(self):
        streamed = [{"choices": [{"delta": {"reasoning": "PRIVATE-THOUGHT",
                                            "reasoning_details": [{"type": "reasoning.text",
                                                                   "text": "PRIVATE-THOUGHT", "index": 0}]},
                                  "finish_reason": None}]},
                    {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
                    {"choices": [{"delta": {"content": " there"}, "finish_reason": "stop"}]}]
        self.everything(True)
        events = self.events_for([streamed], None)
        self.assertTrue(self.bodies[0]["stream"])
        self.assertEqual([e["kind"] for e in events], ["content", "content", "final"])
        self.assertEqual(events[-1]["message"]["content"], "Hello there")
        self.assertNotIn("PRIVATE-THOUGHT", repr(events))
        self.assertNotIn(policy.REPLAY_REASONING_KEY, events[-1]["message"])
        # Offering tools: still fetched whole, for an exact replay.
        self.events_for([answer("ok")], [TOOL])
        self.assertFalse(self.bodies[0]["stream"])
        # Without cloud-everything: unchanged, fetched whole even with no tools.
        self.everything(False)
        policy.remote_only_profile.cache_clear()
        self.events_for([answer("ok")], None)
        self.assertFalse(self.bodies[0]["stream"])

    def test_provider_routing_only_under_the_gate(self):
        self.everything(True)
        self.events_for([answer("ok")], [TOOL])
        self.assertEqual(self.bodies[0]["provider"], {"sort": "latency"})
        self.assertEqual(self.bodies[0]["reasoning"], {"effort": "low"})
        self.everything(False)
        policy.remote_only_profile.cache_clear()
        self.events_for([answer("ok")], [TOOL])
        self.assertNotIn("provider", self.bodies[0])

    def test_anthropic_system_prompt_cache_mark_only_under_the_gate(self):
        self.use({"model_id": "anthropic/claude-haiku-5.5"})
        self.everything(True)
        self.events_for([answer("ok")], [TOOL])
        self.assertEqual(self.bodies[0]["messages"][0]["content"],
                         [{"type": "text", "text": "Rules.", "cache_control": {"type": "ephemeral"}}])
        self.assertEqual(self.bodies[0]["messages"][1], {"role": "user", "content": "x"})
        p = policy.remote_only_profile()
        bad = {"model": p.model_id, "stream": False, "max_tokens": 3000,
               "messages": [{"role": "system", "content": [{"type": "text", "text": "R",
                                                            "cache_control": {"type": "persistent"}}]},
                            {"role": "user", "content": "x"}]}
        with self.assertRaises(EndpointConfigurationError):
            p.validate_payload(bad)
        self.everything(False)
        policy.remote_only_profile.cache_clear()
        self.events_for([answer("ok")], [TOOL])
        self.assertEqual(self.bodies[0]["messages"][0]["content"], "Rules.")
        # Gemini models on OpenRouter get no mark.
        self.use({"model_id": "google/gemini-3.8-flash"})
        self.everything(True)
        self.events_for([answer("ok")], [TOOL])
        self.assertEqual(self.bodies[0]["messages"][0]["content"], "Rules.")


def test_managed_clients_send_messages_unchanged():
    class Managed:
        managed = True
    rows = [{"role": "assistant", "content": "", "tool_calls": [{"id": "x", "extra_content": extra()}]}]
    assert OMLXClient._wire_messages(Managed(), "m", rows) is rows
    assert replay_tool_calls(Managed(), {EXTRA_KEY: object()}, rows[0]["tool_calls"]) is rows[0]["tool_calls"]


# --- Connect while already all-cloud: the new key and model are tested directly ---------

def _switch_probe(monkeypatch, status, body=None):
    import httpx
    from types import SimpleNamespace
    from service import main as main_module
    from service.inference import attributed_transport as at
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {})

    monkeypatch.setattr(at, "CredentialTransport",
                        lambda base_url, key, managed: httpx.MockTransport(handler))
    checked = SimpleNamespace(base_url="https://generativelanguage.googleapis.com",
                              api_prefix="/v1beta/openai", provider="gemini")
    return main_module, checked, seen


def test_switch_probe_accepts_an_answer_and_sends_gemini_shape(monkeypatch):
    import asyncio, json
    main_module, checked, seen = _switch_probe(monkeypatch, 200, {"choices": [{"message": {"content": "OK"}}]})
    asyncio.run(main_module._remote_only_switch_probe(checked, "gemini-3.8-flash", "k"))
    sent = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1beta/openai/chat/completions"
    assert sent["model"] == "gemini-3.8-flash" and sent["reasoning_effort"] == "low"
    assert "reasoning" not in sent and "provider" not in sent


def test_switch_probe_adds_no_reasoning_for_a_model_without_it(monkeypatch):
    # Review 2 NOTE 2: a Gemini API model with no reasoning (Gemma) must not get it.
    import asyncio, json
    main_module, checked, seen = _switch_probe(monkeypatch, 200, {"choices": [{"message": {"content": "OK"}}]})
    asyncio.run(main_module._remote_only_switch_probe(checked, "gemma-4-31b-it", "k"))
    sent = json.loads(seen[0].content)
    assert sent["model"] == "gemma-4-31b-it"
    assert not {"reasoning_effort", "reasoning", "provider"} & set(sent)


def test_switch_probe_refuses_a_bad_key_or_model(monkeypatch):
    import asyncio
    from fastapi import HTTPException
    for status, words in ((401, "rejected that key"), (403, "rejected that key"),
                          (404, "exact model ID"), (500, "could not be reached")):
        main_module, checked, _ = _switch_probe(monkeypatch, status, {"error": {"message": "secret echo"}})
        with pytest.raises(HTTPException) as caught:
            asyncio.run(main_module._remote_only_switch_probe(checked, "gemini-3.8-flash", "k"))
        assert words in caught.value.detail and "secret echo" not in caught.value.detail
