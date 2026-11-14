"""Same-turn replay of a remote provider's reasoning_details, over a mocked transport.

Synthetic only: run with a fresh WISP_HOME; sockets are forbidden and no Keychain
item is read. A provider's reasoning that comes with tool calls is sent back
unchanged on the next request of the same turn, and appears nowhere else: not in
events, the debug export, logs, stored history, the summary input or a later turn.
Models that need it back are fetched whole, never streamed.
"""
import asyncio
import contextlib
import copy
import io
import json
import logging
import pickle
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx

from tests.test_remote_inference_policy import RemoteFixture, policy, EndpointConfigurationError
from service.errors import translate
from service.inference.omlx_client import (OMLXClient, ReasoningReplayTooLargeError, ReplayReasoning,
                                           replay_reasoning_details, without_replay_reasoning)

MARKER = "REPLAY-PRIVATE-MARKER-7c2e"
KEY = policy.REPLAY_REASONING_KEY
EFFORT_MODEL = next(iter(policy.REASONING_EFFORT_MODELS))
REPLAY_MODELS = sorted(policy.REPLAY_REASONING_MODELS)
TOOL_NAME = "synthetic_lookup"
TOOL = {"type": "function", "function": {"name": TOOL_NAME, "description": "Synthetic lookup.",
                                         "parameters": {"type": "object",
                                                        "properties": {"q": {"type": "string"}}}}}
FULL = {"context_window": 32000, "max_tokens": 8192, "request_bytes": 524288,
        "messages": 200, "aggregate_bytes": 131072}


def details_for(model):
    """Realistic reasoning_details for a model's provider format, every entry type."""
    fmt = "anthropic-claude-v1" if model.startswith("anthropic/") else "google-gemini-v1"
    return [{"type": "reasoning.text", "text": MARKER + " plan the lookup",
             "signature": "sig-" + MARKER + "-" + "A" * 64, "id": "rd_1", "format": fmt, "index": 0},
            {"type": "reasoning.summary", "summary": MARKER + " summary", "id": "rd_2", "format": fmt,
             "index": 1},
            {"type": "reasoning.encrypted", "data": "enc-" + MARKER + "-" + "B" * 256, "id": "rd_3",
             "format": fmt, "index": 2}]


def calls(parallel):
    names = ["a", "b"] if parallel else ["a"]
    return [{"id": f"call_{n}", "type": "function",
             "function": {"name": TOOL_NAME, "arguments": json.dumps({"q": n})}} for n in names]


def tool_reply(details, parallel=False):
    return {"choices": [{"message": {"role": "assistant", "content": None,
                                     "reasoning": MARKER + " reasoning text",
                                     "reasoning_details": copy.deepcopy(details),
                                     "tool_calls": calls(parallel)},
                         "finish_reason": "tool_calls"}]}


def answer_reply(text):
    return {"choices": [{"message": {"role": "assistant", "content": text,
                                     "reasoning": MARKER + " final thoughts",
                                     "reasoning_details": [{"type": "reasoning.text", "text": MARKER,
                                                            "format": "google-gemini-v1", "index": 0}]},
                         "finish_reason": "stop"}]}


def sse(chunks):
    return "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"


def streamed_tool_reply(details_lists):
    chunks = [{"choices": [{"delta": {"reasoning": MARKER + " text", "reasoning_details": d},
                            "finish_reason": None}]} for d in details_lists]
    call = dict(calls(False)[0], index=0)
    chunks.append({"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": None}]})
    chunks.append({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    return chunks


def streamed_answer(text):
    return [{"choices": [{"delta": {"content": text}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]}]


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class ReplayBase(RemoteFixture):
    """A real OMLXClient on the remote profile, its transport answered by `replies`:
    a dict is a JSON body, a list is a stream of SSE chunks."""
    profile_overrides = FULL

    def use_model(self, model, **limits):
        self.profile.update(model_id=model, **limits)
        self.cfg["inference"]["remote_only"].update(model_id=model, **limits)
        policy.remote_only_profile.cache_clear()

    def client(self, replies):
        self.bodies, self.raw = [], []

        def handle(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": self.profile["model_id"]}]})
            self.raw.append(request.content)
            self.bodies.append(json.loads(request.content))
            reply = replies[len(self.bodies) - 1]
            if isinstance(reply, dict):
                return httpx.Response(200, json=reply)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse(reply))
        with patch("service.inference.attributed_transport.CredentialTransport",
                   return_value=httpx.MockTransport(handle)):
            return OMLXClient(target=policy.remote_default_target("agent"))


class AgentTurns(ReplayBase):
    """Whole tool turns through the agent loop (stream_events)."""

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

    def run_turn(self, client, messages, events=None):
        events = [] if events is None else events

        async def emit(event):
            events.append(event)

        class Approver:
            async def confirm(self, *_a, **_k):
                return True

        async def go():
            try:
                return await self.loop.run_agent(
                    client, client.target.model, messages, emit, Approver(), tools=[TOOL_NAME],
                    include_memory_context=False, include_identity_context=False,
                    multi_round=True, max_steps=3, debug=True)
            finally:
                await client.aclose()
        return asyncio.run(go())

    def captured_turn(self, replies, prompt="Look it up"):
        client = self.client(replies)
        self.tool_runs.clear()
        events, out, err, records = [], io.StringIO(), io.StringIO(), _Records()
        logging.getLogger().addHandler(records)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                final = self.run_turn(client, [{"role": "user", "content": prompt}], events)
        finally:
            logging.getLogger().removeHandler(records)
        return final, events, [out.getvalue(), err.getvalue(), "\n".join(records.lines)]

    def assert_replayed(self, expected, parallel):
        self.assertEqual(len(self.bodies), 2)
        self.assertEqual(sorted(r["q"] for r in self.tool_runs), ["a", "b"] if parallel else ["a"])
        self.assertNotIn(b"reasoning_details", self.raw[0])
        msgs = self.bodies[1]["messages"]
        rows = [i for i, m in enumerate(msgs) if "reasoning_details" in m]
        self.assertEqual(len(rows), 1)                       # once, on the one call row
        row = msgs[rows[0]]
        self.assertEqual(row["role"], "assistant")
        self.assertEqual([c["id"] for c in row["tool_calls"]],
                         ["call_a", "call_b"] if parallel else ["call_a"])
        self.assertEqual(row["reasoning_details"], expected)
        self.assertEqual(json.dumps(row["reasoning_details"], sort_keys=False),
                         json.dumps(expected, sort_keys=False))
        self.assertIn(compact(expected).encode(), self.raw[1])
        results = msgs[rows[0] + 1:rows[0] + 1 + len(row["tool_calls"])]
        self.assertEqual([m["role"] for m in results], ["tool"] * len(row["tool_calls"]))
        self.assertEqual(sorted(m["tool_call_id"] for m in results),
                         sorted(c["id"] for c in row["tool_calls"]))
        self.assertFalse(any(KEY in m for m in msgs))

    def assert_no_leak(self, events, surfaces):
        exported = json.dumps(events, default=repr)
        for surface in [exported, *surfaces]:
            self.assertNotIn(MARKER, surface)
            self.assertNotIn(KEY, surface)
            self.assertNotIn("reasoning_details", surface)
        self.assertEqual(sum(e.get("type") == "raw_model_io" for e in events), 2)
        self.assertFalse(any(e.get("type") == "reasoning" for e in events))

    def test_replay_models_single_and_parallel_tool_turns(self):
        for model in REPLAY_MODELS:
            for parallel in (False, True):
                with self.subTest(model=model, parallel=parallel):
                    self.use_model(model)
                    expected = details_for(model)
                    final, events, surfaces = self.captured_turn(
                        [tool_reply(expected, parallel), answer_reply("Synthetic final answer.")])
                    self.assertEqual(final, "Synthetic final answer.")
                    self.assertTrue(all(b["stream"] is False for b in self.bodies))   # never streamed
                    self.assert_replayed(expected, parallel)
                    self.assert_no_leak(events, surfaces)
                    option = policy.reasoning_option(model)   # OpenRouter Gemini: gate only
                    if option is not None:
                        self.assertTrue(all(b.get("reasoning") == option
                                            and b["max_tokens"] >= policy.REASONING_MIN_MAX_TOKENS
                                            for b in self.bodies))
                    else:
                        self.assertTrue(all("reasoning" not in b for b in self.bodies))

    def test_history_summary_and_a_later_turn_hold_none_of_it(self):
        from service.memory import context
        from service.memory.store import SessionStore
        from service.memory.subject import record_turn
        for model in REPLAY_MODELS:
            with self.subTest(model=model):
                self.use_model(model)
                final, events, _ = self.captured_turn(
                    [tool_reply(details_for(model), True), answer_reply("First answer.")])
                self.assertEqual(final, "First answer.")
                # Persist the turn as the /agent endpoint does: the prompt, the reply
                # text, and the subject from the tool_call/tool_result events.
                folder = tempfile.TemporaryDirectory()
                self.addCleanup(folder.cleanup)
                db = Path(folder.name) / "sessions.db"
                store = SessionStore(db)
                sid = store.create_session()
                store.add_turn(sid, "user", "Look it up")
                names = ", ".join(dict.fromkeys(e["name"] for e in events if e["type"] == "tool_call"))
                store.add_turn(sid, "assistant", final, tool_digest=names or None)
                record_turn(store, sid, [e for e in events if e["type"] == "tool_call"],
                            [e for e in events if e["type"] == "tool_result"])
                store._db.commit()
                for path in db.parent.glob(db.name + "*"):
                    data = path.read_bytes()
                    for needle in (MARKER, KEY, "reasoning_details"):
                        self.assertNotIn(needle.encode(), data, path.name)
                # The rolling summary folds stored turns: its input has none of it.
                lines, _ = context._fold_input(store.turns_range(sid, 0, store.turn_count(sid)))
                self.assertNotIn(MARKER, "\n".join(lines))
                # The next turn: history from the store, and its first request.
                with patch.object(context, "store", store):
                    history = context.build_messages(sid)
                self.assertNotIn(MARKER, json.dumps(history))
                client = self.client([answer_reply("Second answer.")])
                later = self.run_turn(client, history + [{"role": "user", "content": "And now?"}])
                self.assertEqual(later, "Second answer.")
                self.assertEqual(len(self.bodies), 1)
                self.assertNotIn(b"reasoning_details", self.raw[0])
                self.assertNotIn(MARKER.encode(), self.raw[0])

    def test_oversize_reasoning_stops_the_turn_with_a_plain_message(self):
        for model in REPLAY_MODELS:
            with self.subTest(model=model):
                self.use_model(model, response_bytes=2048)
                big = [{"type": "reasoning.encrypted", "data": "x" * 4000,
                        "format": "google-gemini-v1", "index": 0}]
                client = self.client([tool_reply(big), answer_reply("never sent")])
                with self.assertRaises(ReasoningReplayTooLargeError) as raised:
                    self.run_turn(client, [{"role": "user", "content": "Look it up"}])
                self.assertEqual(len(self.bodies), 1)             # never sent without it
                message, _ = translate(raised.exception, endpoint_name="cloud")
                self.assertEqual(message, policy.REASONING_REPLAY_TOO_LARGE)

    def test_streamed_models_replay_concatenated_deltas_best_effort(self):
        pieces = [details_for("google/x")[:1], details_for("google/x")[1:2], details_for("google/x")[2:]]
        expected = [item for piece in pieces for item in piece]
        final, events, surfaces = self.captured_turn(
            [streamed_tool_reply(pieces), streamed_answer("Synthetic final answer.")])
        self.assertEqual(final, "Synthetic final answer.")
        self.assertTrue(all(b["stream"] is True for b in self.bodies))
        self.assert_replayed(expected, parallel=False)
        self.assert_no_leak(events, surfaces)

    def test_malformed_streamed_details_are_not_replayed_and_the_call_still_runs(self):
        bad = [[{"type": "reasoning.text", "text": "x", "index": 0}], {"not": "a list"}]
        final, _, _ = self.captured_turn([streamed_tool_reply(bad), streamed_answer("Done.")])
        self.assertEqual(final, "Done.")
        self.assertEqual(len(self.bodies), 2)
        self.assertNotIn(b"reasoning_details", self.raw[1])


class ClientCalls(ReplayBase):
    """Two-step tool turns made directly with chat() and stream_events()."""

    def two_step(self, model, parallel, via_stream):
        self.use_model(model)
        expected = details_for(model)
        client = self.client([tool_reply(expected, parallel), answer_reply("Done.")])
        ask = [{"role": "user", "content": "Look it up"}]

        async def call(messages):
            if via_stream:
                events = [e async for e in client.stream_events(client.target.model, messages,
                                                                tools=[TOOL], max_tokens=3000)]
                self.assertFalse(any(e["kind"] == "reasoning" for e in events))
                return events[-1]["message"]
            return (await client.chat(client.target.model, messages, tools=[TOOL],
                                      max_tokens=3000))["choices"][0]["message"]

        async def go():
            try:
                first = await call(ask)
                held = replay_reasoning_details(client, first)
                self.assertEqual(held, expected)
                self.assertNotIn("reasoning_details", first)
                self.assertNotIn("reasoning", first)
                self.assertEqual(first["reasoning_content"], "")
                self.assertNotIn(MARKER, repr(first) + str(first))
                self.assertNotIn(MARKER, json.dumps(without_replay_reasoning(first)))
                row = {"role": "assistant", "content": "", "tool_calls": first["tool_calls"],
                       "reasoning_details": held}
                results = [{"role": "tool", "tool_call_id": c["id"], "content": "result"}
                           for c in first["tool_calls"]]
                second = await call(ask + [row, *results])
                self.assertNotIn(KEY, second)
                return second
            finally:
                await client.aclose()
        second = asyncio.run(go())
        self.assertEqual(second["content"], "Done.")
        self.assertEqual([b["stream"] for b in self.bodies], [False, False])
        sent = [m for m in self.bodies[1]["messages"] if "reasoning_details" in m]
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(sent[0]["tool_calls"]), 2 if parallel else 1)
        self.assertEqual(json.dumps(sent[0]["reasoning_details"], sort_keys=False),
                         json.dumps(expected, sort_keys=False))
        self.assertNotIn(b"reasoning_details", self.raw[0])

    def test_chat_and_stream_events_two_step_for_every_replay_model(self):
        for model in REPLAY_MODELS:
            for parallel in (False, True):
                for via_stream in (False, True):
                    with self.subTest(model=model, parallel=parallel, stream=via_stream):
                        self.two_step(model, parallel, via_stream)

    def test_whole_reply_final_answer_discards_its_details(self):
        self.use_model(REPLAY_MODELS[0])
        client = self.client([answer_reply("Plain answer.")])

        async def go():
            try:
                return [e async for e in client.stream_events(client.target.model,
                                                              [{"role": "user", "content": "hi"}])]
            finally:
                await client.aclose()
        events = asyncio.run(go())
        self.assertEqual([e["kind"] for e in events], ["content", "final"])
        self.assertNotIn(KEY, events[-1]["message"])
        self.assertNotIn(MARKER, repr(events))

    def test_whole_reply_checks_its_calls_like_a_stream(self):
        self.use_model(REPLAY_MODELS[0])
        for arguments in ("{not json", "[]", None):
            reply = tool_reply(details_for(REPLAY_MODELS[0]))
            reply["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
            client = self.client([reply])

            async def go():
                try:
                    return [e async for e in client.stream_events(
                        client.target.model, [{"role": "user", "content": "x"}], tools=[TOOL],
                        max_tokens=3000)]
                finally:
                    await client.aclose()
            with self.subTest(arguments=arguments), self.assertRaises(Exception) as raised:
                asyncio.run(go())
            self.assertIn("tool", str(raised.exception).lower())

    def test_unusable_details_from_a_replay_model_stop_rather_than_drop(self):
        self.use_model(REPLAY_MODELS[0])
        reply = tool_reply([])
        reply["choices"][0]["message"]["reasoning_details"] = [{"type": "x"}, "not an object"]
        client = self.client([reply])

        async def go():
            try:
                await client.chat(client.target.model, [{"role": "user", "content": "x"}],
                                  tools=[TOOL], max_tokens=3000)
            finally:
                await client.aclose()
        with self.assertRaisesRegex(Exception, "unusable reasoning"):
            asyncio.run(go())

    def test_holder_never_serializes_or_prints(self):
        held = ReplayReasoning([{"data": MARKER}])
        self.assertNotIn(MARKER, repr(held) + str(held) + repr({"k": held}))
        for attempt in (lambda: json.dumps({"k": held}), lambda: pickle.dumps(held)):
            with self.assertRaises(TypeError):
                attempt()
        self.assertIs(copy.deepcopy(held), held)

    def test_managed_clients_never_replay(self):
        class Managed:
            managed = True
        self.assertIsNone(replay_reasoning_details(Managed(), {KEY: ReplayReasoning([{}])}))
        self.assertIsNone(replay_reasoning_details(object(), {KEY: ReplayReasoning([{}])}))


class PayloadChecks(RemoteFixture):
    def body(self, details=None, **extra):
        call = {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
        rows = [{"role": "user", "content": "synthetic"},
                {"role": "assistant", "content": "", "tool_calls": [call],
                 "reasoning_details": copy.deepcopy(details or details_for("google/x"))},
                {"role": "tool", "tool_call_id": "c1", "content": "result"}]
        return {"model": self.profile["model_id"], "messages": rows, "stream": True,
                "max_tokens": 3000, **extra}

    def test_reasoning_details_only_on_assistant_tool_call_rows(self):
        p = policy.remote_only_profile()
        p.validate_payload(self.body())
        details = details_for("google/x")
        for row in ({"role": "user", "content": "x", "reasoning_details": details},
                    {"role": "assistant", "content": "x", "reasoning_details": details},
                    {"role": "tool", "tool_call_id": "c1", "content": "x", "reasoning_details": details}):
            with self.subTest(row=row["role"]), self.assertRaises(EndpointConfigurationError):
                p.validate_payload({**self.body(), "messages": [row]})
        for value in ({"type": "x"}, "text", [], ["text"], [[{}]], None, [{"a": 1}, 2]):
            body = self.body()
            body["messages"][1]["reasoning_details"] = value
            with self.subTest(value=value), self.assertRaises(EndpointConfigurationError):
                p.validate_payload(body)
        for key, value in ((KEY, details), ("reasoning_details", ReplayReasoning(details))):
            body = self.body()
            body["messages"][1][key] = value
            with self.subTest(key=key), self.assertRaises(EndpointConfigurationError):
                p.validate_payload(body)

    def test_replayed_details_have_their_own_cap_outside_the_transcript_budget(self):
        tight = policy.profile_from_config({**self.profile, "request_bytes": 8192,
                                            "aggregate_bytes": 4096, "response_bytes": 65536})
        large = [{"type": "reasoning.encrypted", "data": "x" * 30000, "format": "google-gemini-v1",
                  "index": 0}]
        tight.validate_payload(self.body(large))      # past request and aggregate on its own
        too_large = [{"type": "reasoning.encrypted", "data": "x" * 70000, "index": 0}]
        with self.assertRaisesRegex(EndpointConfigurationError, "too large to continue"):
            tight.validate_payload(self.body(too_large))
        # The transcript itself is still bounded by its own budgets.
        body = self.body(large)
        body["messages"][0]["content"] = "y" * 5000
        with self.assertRaisesRegex(EndpointConfigurationError, "byte budget"):
            tight.validate_payload(body)

    def test_top_level_reasoning_only_for_its_model_and_effort(self):
        p = policy.remote_only_profile()
        with self.assertRaises(EndpointConfigurationError):
            p.validate_payload(self.body(reasoning={"effort": "medium"}))   # not a listed model
        effort = policy.profile_from_config({**self.profile, "model_id": EFFORT_MODEL})
        body = {**self.body(), "model": EFFORT_MODEL}
        effort.validate_payload({**body, "reasoning": {"effort": "medium"}})
        for value in ({"effort": "high"}, {"effort": "low"}, {"effort": "medium", "max_tokens": 9},
                      {"max_tokens": 1024}, {"enabled": True}, {}, "medium", None, ["medium"],
                      {"effort": None}, {"effort": "MEDIUM"}):
            with self.subTest(value=value), self.assertRaises(EndpointConfigurationError):
                effort.validate_payload({**body, "reasoning": value})
        with self.assertRaises(EndpointConfigurationError):
            effort.validate_payload({**body, "max_tokens": 2048, "reasoning": {"effort": "medium"}})
        effort.validate_payload({**body, "max_tokens": 2049, "reasoning": {"effort": "medium"}})
        for model in REPLAY_MODELS:
            if model != EFFORT_MODEL:
                other = policy.profile_from_config({**self.profile, "model_id": model})
                with self.subTest(model=model), self.assertRaises(EndpointConfigurationError):
                    other.validate_payload({**body, "model": model, "reasoning": {"effort": "medium"}})
        self.assertEqual(policy.REASONING_EFFORT_MODELS, {
            EFFORT_MODEL: "medium", "google/gemini-3.8-flash": "low",
            "google/gemini-4-carbon": "low", "google/gemini-4-argon": "low",
            # The Gemini API's bare ids (sent as reasoning_effort, on its origin only).
            "gemini-3.8-flash": "low", "gemini-4-carbon": "low", "gemini-4-argon": "low"})
        # Provider-correct shape: a bare Gemini API id gets `reasoning_effort` on the
        # Gemini wire only; an OpenRouter id gets `reasoning: {effort}` off it only.
        for bare in policy.GEMINI_API_MODELS:
            with self.subTest(model=bare):
                self.assertEqual(policy.reasoning_fields("gemini", bare), {"reasoning_effort": "low"})
                self.assertEqual(policy.reasoning_fields("openrouter", bare), {})
                # OpenRouter's Gemini ids reason only under cloud-everything (NOTE 1).
                self.assertEqual(policy.reasoning_fields("openrouter", "google/" + bare), {})
                with patch.object(policy, "cloud_everything_active", lambda: True):
                    self.assertEqual(policy.reasoning_fields("openrouter", "google/" + bare),
                                     {"reasoning": {"effort": "low"}})
                    self.assertEqual(policy.reasoning_fields("gemini", "google/" + bare), {})
        self.assertEqual(policy.reasoning_fields("openrouter", EFFORT_MODEL), {"reasoning": {"effort": "medium"}})
        self.assertEqual(policy.reasoning_fields("gemini", EFFORT_MODEL), {})


class ReasoningOption(ReplayBase):
    def bodies_for(self, requests):
        ok = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        client = self.client([ok] * len(requests))

        async def go():
            try:
                for stream, kwargs in requests:
                    if stream:
                        [e async for e in client.stream_events(client.target.model, **kwargs)]
                    else:
                        await client.chat(client.target.model, **kwargs)
            finally:
                await client.aclose()
        asyncio.run(go())
        return self.bodies

    def test_effort_model_gets_medium_effort_with_room_for_its_budget(self):
        self.use_model(EFFORT_MODEL)
        ask = [{"role": "user", "content": "synthetic"}]
        transcript = ask + [{"role": "assistant", "content": "", "tool_calls": calls(False)},
                            {"role": "tool", "tool_call_id": "call_a", "content": "result"}]
        bodies = self.bodies_for([
            (True, {"messages": ask, "tools": [TOOL], "max_tokens": 3000}),     # agent step
            (True, {"messages": ask, "tools": [TOOL], "max_tokens": 512}),      # small tool step
            (False, {"messages": transcript, "max_tokens": 700}),               # tool-turn answer
            (False, {"messages": ask, "max_tokens": 16}),                       # Connect probe
            (False, {"messages": ask, "max_tokens": 400}),                      # background line
            (True, {"messages": ask, "max_tokens": 4096}),                      # plain answer
        ])
        self.assertEqual([b.get("reasoning") for b in bodies],
                         [{"effort": "medium"}] * 3 + [None, None, {"effort": "medium"}])
        # A bound client already sends at least 2000 (the window fitter's output floor),
        # so the small tool-free calls go out at 2000, without reasoning.
        self.assertEqual([b["max_tokens"] for b in bodies], [3000, 2049, 2049, 2000, 2000, 4096])

    def test_a_profile_cap_below_the_floor_never_sends_reasoning(self):
        self.use_model(EFFORT_MODEL, max_tokens=2048)
        bodies = self.bodies_for([(True, {"messages": [{"role": "user", "content": "x"}],
                                          "tools": [TOOL], "max_tokens": 512})])
        self.assertNotIn("reasoning", bodies[0])
        self.assertEqual(bodies[0]["max_tokens"], 2000)

    def test_other_models_send_no_reasoning_key(self):
        for model in ["example-model", *[m for m in REPLAY_MODELS if m not in policy.REASONING_EFFORT_MODELS]]:
            with self.subTest(model=model):
                self.use_model(model)
                streamed = model not in policy.REPLAY_REASONING_MODELS
                if streamed:
                    client = self.client([streamed_answer("ok")])

                    async def go():
                        try:
                            [e async for e in client.stream_events(
                                client.target.model, [{"role": "user", "content": "x"}],
                                tools=[TOOL], max_tokens=3000)]
                        finally:
                            await client.aclose()
                    asyncio.run(go())
                    bodies = self.bodies
                else:
                    bodies = self.bodies_for([(True, {"messages": [{"role": "user", "content": "x"}],
                                                      "tools": [TOOL], "max_tokens": 512})])
                self.assertTrue(all("reasoning" not in b for b in bodies))
                self.assertEqual(bodies[0]["max_tokens"], 3000 if streamed else 2000)
