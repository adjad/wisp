"""Hand-authored synthetic fixtures, never recorded provider responses."""
from copy import deepcopy
import json
import socket
import unittest
from unittest.mock import patch

from adapter import (ContractError, ENDPOINT, MAX_BYTES, request, response,
                     stream_response, synthetic_followup)

MESSAGES = [{"role": "user", "content": "Look up the fictional item cobalt-widget."}]
TOOLS = [{"type": "function", "function": {
    "name": "lookup_demo_item", "description": "Read a synthetic inventory fixture.",
    "parameters": {"type": "object", "properties": {"item": {"type": "string"}},
                   "required": ["item"], "additionalProperties": False}}}]
NAMES = {"lookup_demo_item"}
CALL = {"id": "demo-1", "type": "function", "function": {
    "name": "lookup_demo_item", "arguments": '{"item":"cobalt-widget"}'}}
ASSISTANT = {"role": "assistant", "content": None, "tool_calls": [CALL]}
USAGE = {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100, "cost": 0.00004}


def envelope(message=None, finish="stop", usage=USAGE):
    return {"id": "gen-synthetic", "model": "fixture/model", "choices": [{
        "index": 0, "message": message or {"role": "assistant", "content": "Synthetic answer."},
        "finish_reason": finish}], "usage": usage}


def decode(data):
    return response(200, json.dumps(data).encode(), allowed_tools=NAMES)


def event(delta=None, finish=None, **extra):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **extra}


def sse(events, done=True):
    wire = ": OPENROUTER PROCESSING\r\n\r\n" + "".join(
        "data: " + json.dumps(e, ensure_ascii=False) + "\r\n\r\n" for e in events)
    return (wire + ("data: [DONE]\r\n\r\n" if done else "")).encode()


class ContractTests(unittest.TestCase):
    def setUp(self):
        # Any accidental networking in the exercised code fails the suite.
        self.guard = patch.object(socket, "socket", side_effect=AssertionError("Network prohibited"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def test_disabled_by_default(self):
        with self.assertRaises(ContractError):
            request("fixture/model", "fixture-provider", MESSAGES, TOOLS)

    def test_request_policy_and_isolation(self):
        r = request("fixture/model", "fixture-provider", MESSAGES, TOOLS, enabled=True)
        self.assertEqual(r["url"], ENDPOINT)
        self.assertEqual(r["headers"], {"Content-Type": "application/json"})
        self.assertEqual(r["body"]["provider"], {"only": ["fixture-provider"],
            "allow_fallbacks": False, "require_parameters": True, "data_collection": "deny", "zdr": True})
        self.assertFalse(r["body"]["parallel_tool_calls"])
        self.assertEqual(r["body"]["tools"], TOOLS)
        r["body"]["messages"][0]["content"] = "changed"
        self.assertIn("fictional", MESSAGES[0]["content"])

    def test_no_local_extra_parameters(self):
        with self.assertRaises(TypeError):
            request("fixture/model", "fixture-provider", MESSAGES, TOOLS, enabled=True, chat_template_kwargs={})

    def test_request_bounds(self):
        for limit in (0, -1, 257, True, "128"):
            with self.subTest(limit=limit), self.assertRaises(ContractError):
                request("fixture/model", "fixture-provider", MESSAGES, TOOLS, enabled=True, max_tokens=limit)
        with self.assertRaises(ContractError):
            request("fixture/model", "fixture-provider", [{"role": "user", "content": "x" * MAX_BYTES}], [], enabled=True)

    def test_provider_requires_nonempty_string(self):
        for provider in ([], ["fixture-provider"], {}, True, 1, "", " "):
            with self.subTest(provider=provider), self.assertRaises(ContractError):
                request("fixture/model", provider, MESSAGES, TOOLS, enabled=True)

    def test_text_and_usage(self):
        result = decode(envelope())
        self.assertEqual(result["message"]["content"], "Synthetic answer.")
        self.assertEqual(result["usage"], USAGE)

    def test_tool_round_trip_keeps_schema_ids_and_reasoning(self):
        assistant = deepcopy(ASSISTANT)
        assistant["reasoning_details"] = [{"type": "reasoning.opaque", "data": "synthetic-opaque"}]
        first = decode(envelope(assistant, "tool_calls"))
        followup = synthetic_followup(MESSAGES, first["message"], {"demo-1": '{"stock":7}'}, TOOLS)
        second_request = request("fixture/model", "fixture-provider", followup, TOOLS, enabled=True)
        self.assertEqual(second_request["body"]["tools"], TOOLS)
        self.assertEqual(followup[-2], assistant)
        self.assertEqual(followup[-1]["tool_call_id"], "demo-1")
        self.assertEqual(decode(envelope())["message"]["role"], "assistant")
        self.assertEqual(len(MESSAGES), 1)

    def test_followup_requires_exact_ids(self):
        for results in ({}, {"wrong": "x"}, {"demo-1": "x", "extra": "x"}):
            with self.subTest(results=results), self.assertRaises(ContractError):
                synthetic_followup(MESSAGES, ASSISTANT, results, TOOLS)

    def test_http_errors_and_redirects_do_not_echo_body(self):
        for status in (301, 307, 400, 401, 402, 403, 408, 429, 500, 502, 503):
            with self.subTest(status=status), self.assertRaises(ContractError) as caught:
                response(status, b'{"error":{"message":"sensitive-marker"}}')
            self.assertNotIn("sensitive-marker", str(caught.exception))

    def test_error_inside_http_success(self):
        with self.assertRaises(ContractError):
            decode({"error": {"code": 502, "message": "synthetic failure"}})

    def test_malformed_envelopes(self):
        for data in ([], {}, {"choices": []}, {"choices": [None]}, {"choices": [{"message": []}]}):
            with self.subTest(data=data), self.assertRaises(ContractError):
                decode(data)
        for raw in (b"{", b"NaN", b"x" * (MAX_BYTES + 1)):
            with self.assertRaises(ContractError):
                response(200, raw)

    def test_incomplete_filtered_empty_completions(self):
        for finish in (None, "length", "content_filter", "error", "tool_calls"):
            with self.subTest(finish=finish), self.assertRaises(ContractError):
                decode(envelope(finish=finish))
        with self.assertRaises(ContractError):
            decode(envelope({"role": "assistant", "content": ""}))
        with self.assertRaises(ContractError):
            decode(envelope(ASSISTANT, "length"))

    def test_invalid_tool_arguments_and_names(self):
        for arguments in ("{", "[]", '"text"', "null", "NaN", {}):
            altered = deepcopy(ASSISTANT)
            altered["tool_calls"][0]["function"]["arguments"] = arguments
            with self.subTest(arguments=arguments), self.assertRaises(ContractError):
                decode(envelope(altered, "tool_calls"))
        altered = deepcopy(ASSISTANT)
        altered["tool_calls"][0]["function"]["name"] = "send_real_message"
        with self.assertRaises(ContractError):
            decode(envelope(altered, "tool_calls"))

    def test_duplicate_and_missing_ids(self):
        altered = deepcopy(ASSISTANT)
        altered["tool_calls"].append(deepcopy(CALL))
        with self.assertRaises(ContractError):
            decode(envelope(altered, "tool_calls"))
        altered["tool_calls"] = [deepcopy(CALL)]
        del altered["tool_calls"][0]["id"]
        with self.assertRaises(ContractError):
            decode(envelope(altered, "tool_calls"))

    def test_falsey_malformed_tool_call_containers(self):
        for calls in ({}, "", 0, False):
            with self.subTest(calls=calls):
                with self.assertRaises(ContractError):
                    decode(envelope({"role": "assistant", "content": "x", "tool_calls": calls}))
                with self.assertRaises(ContractError):
                    stream_response([sse([event({"content": "x", "tool_calls": calls}, "stop")])])
    def test_unknown_usage_is_not_zero(self):
        data = envelope()
        del data["usage"]
        self.assertIsNone(decode(data)["usage"])
        self.assertEqual(decode(envelope(usage={"cost": 0}))["usage"]["cost"], 0)

    def test_invalid_usage(self):
        for usage in ({"cost": -1}, {"cost": "NaN"}, {"cost": "Infinity"}, {"cost": "bad"},
                      {"cost": True}, {"prompt_tokens": -1}, {"total_tokens": True}, []):
            with self.subTest(usage=usage), self.assertRaises(ContractError):
                decode(envelope(usage=usage))

    def test_stream_every_byte_boundary_utf8_and_repeated_terminal_usage(self):
        wire = sse([event({"content": "Café ☕"}), event(finish="stop"),
                    event({"content": "", "role": "assistant"}, "stop", usage=USAGE)])
        expected = {"message": {"role": "assistant", "content": "Café ☕"}, "usage": USAGE}
        for split in range(len(wire) + 1):
            self.assertEqual(stream_response([wire[:split], wire[split:]]), expected)
        self.assertEqual(stream_response([wire[i:i+1] for i in range(len(wire))]), expected)

    def test_stream_tool_fragments_and_usage_only_chunk(self):
        wire = sse([event({"tool_calls": [{"index": 0, "id": "demo-1", "type": "function", "function": {
            "name": "lookup_demo_item", "arguments": '{"item":'}}]}),
            event({"tool_calls": [{"index": 0, "function": {"arguments": '"cobalt-widget"}'}}]}),
            event(finish="tool_calls"), {"choices": [], "usage": USAGE}])
        self.assertEqual(stream_response([wire], allowed_tools=NAMES), {"message": ASSISTANT, "usage": USAGE})

    def test_stream_error_truncation_and_missing_finish(self):
        for wire in (sse([event({"content": "partial"})], done=False),
                     sse([event({"content": "partial"})]),
                     sse([event({"content": "partial"}), {"error": {"code": 502}}]),
                     sse([event({"content": "partial"}, "length")]), b"data: {bad}\n\n",
                     b"data: \xff\n\n", b"x" * (MAX_BYTES + 1)):
            with self.assertRaises(ContractError):
                stream_response([wire])

    def test_stream_conflicting_finish_and_late_content(self):
        for events in ([event({"content": "x"}, "stop"), event(finish="length")],
                       [event({"content": "x"}, "stop"), event({"content": "late"})]):
            with self.assertRaises(ContractError):
                stream_response([sse(events)])
        with self.assertRaises(ContractError):
            stream_response([sse([event({"content": "x"}, "stop")]) + b"data: {}\n\n"])

    def test_reasoning_stream_is_explicitly_unsupported(self):
        with self.assertRaises(ContractError):
            stream_response([sse([event({"reasoning": "synthetic"}), event({"content": "x"}, "stop")])])


if __name__ == "__main__":
    unittest.main(verbosity=2)
