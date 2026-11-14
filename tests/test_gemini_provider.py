"""The Gemini API as a second cloud provider (Apple silicon port of the Intel provider).

Synthetic only: run with a fresh WISP_HOME; sockets are forbidden and no Keychain item
is read. Every transport is a fake. Covers:
- the Gemini profile (Google's origin and prefix only), its wire, its label, and
  Connect refusing any other host before a key is read;
- its payload shape (`reasoning_effort`, never OpenRouter's `reasoning`);
- thought signatures (`extra_content` on each tool call) captured, streamed and whole,
  sent back unchanged on the next request of the turn, and nowhere else;
- managed (local engine) clients send their messages unchanged.
"""
import asyncio
import contextlib
import copy
import io
import json
import logging
import os
from unittest.mock import patch

import httpx

from tests.test_remote_inference_policy import RemoteFixture, policy, EndpointConfigurationError
from tests.test_remote_reasoning_replay import (ReplayBase, TOOL, TOOL_NAME, _Records,
                                                streamed_answer)
from service.config.endpoints import endpoint_from_config
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
    """A real OMLXClient on a remote-only profile pointed at the Gemini API."""
    profile_overrides = GEMINI

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("WISP_REMOTE_PARITY", None)

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
        self.assertEqual(wire_provider("gemini", GEMINI_ORIGIN), "gemini")
        self.assertEqual(wire_provider("openai-compatible", GEMINI_ORIGIN), "gemini")
        self.assertEqual(wire_provider("openai-compatible", OPENROUTER_ORIGIN), "openrouter")
        self.assertEqual(wire_provider("openai-compatible", "https://generativelanguage.googleapis.com.x"),
                         "openai-compatible")
        self.assertEqual(wire_provider("omlx", GEMINI_ORIGIN), "omlx")
        self.assertEqual(policy.profile_from_config({**self.profile, **GEMINI}).wire, "gemini")
        self.assertEqual(policy.remote_only_profile().wire, "openai-compatible")

    def test_effort_table_holds_the_bare_ids_only(self):
        for model in policy.GEMINI_API_MODELS:
            self.assertEqual(policy.REASONING_EFFORT_MODELS[model], "low")
            self.assertEqual(policy.reasoning_fields("gemini", model), {"reasoning_effort": "low"})
            self.assertEqual(policy.reasoning_fields("openrouter", model), {})
            self.assertEqual(policy.reasoning_fields("openai-compatible", model), {})
            # OpenRouter's own ids for the same models are unchanged by this provider.
            self.assertNotIn("google/" + model, policy.REASONING_EFFORT_MODELS)
        self.assertNotIn("gemini-3.8-flash", policy.REPLAY_REASONING_MODELS)

    def test_connect_refuses_another_host_before_reading_any_key(self):
        from fastapi import HTTPException
        from service import main
        for roles in (["reasoning"], ["everything"]):
            body = {"provider": "gemini", "base_url": "https://evil.example", "api_prefix": GEMINI_API_PREFIX,
                    "model_id": "gemini-3.8-flash", "context_window": 32000, "credential_name": "gemini",
                    "roles": roles}
            with self.subTest(roles=roles), patch(
                    "service.config.provider_credentials.resolve_keychain",
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
        router.validate_payload(rbody)
        # No provider routing in this port, and OpenRouter's Gemini ids get no reasoning option.
        for bad in ({"reasoning_effort": "low"}, {"provider": {"sort": "latency"}},
                    {"reasoning": {"effort": "low"}}):
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


def test_managed_clients_send_messages_unchanged():
    class Managed:
        managed = True
    rows = [{"role": "assistant", "content": "", "tool_calls": [{"id": "x", "extra_content": extra()}]}]
    assert OMLXClient._wire_messages(Managed(), rows) is rows
    assert replay_tool_calls(Managed(), {EXTRA_KEY: object()}, rows[0]["tool_calls"]) is rows[0]["tool_calls"]
