"""CPU-only boundary checks for the standalone Ling prototype."""

from __future__ import annotations

import json
import http.client
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ENGINE_DIR = Path(__file__).resolve().parents[1] / "tools" / "ling_engine"
sys.path.insert(0, str(ENGINE_DIR))
from cli import MODEL_ID, _prepare_chat, _run_chat, create_server  # noqa: E402
from engine import Engine, GenerationCancelled, inspect_checkpoint  # noqa: E402


class CheckpointTests(unittest.TestCase):
    def test_rejects_missing_and_incompatible_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with self.assertRaises(FileNotFoundError):
                inspect_checkpoint(path / "missing")
            with self.assertRaisesRegex(ValueError, "config.json"):
                inspect_checkpoint(path)
            (path / "config.json").write_text(json.dumps({
                "model_type": "wrong", "num_hidden_layers": 24,
                "quantization": {"bits": 4, "group_size": 64, "mode": "affine"},
            }))
            (path / "tokenizer.json").write_text("{}")
            (path / "model.safetensors").write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "model_type"):
                inspect_checkpoint(path)

    def test_accepts_expected_metadata_without_opening_weights(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = {
                "model_type": "bailing_hybrid", "num_hidden_layers": 24,
                "quantization": {"bits": 4, "group_size": 64, "mode": "affine"},
            }
            (path / "config.json").write_text(json.dumps(config))
            (path / "tokenizer.json").write_text("{}")
            (path / "model.safetensors").write_bytes(b"")
            loaded_path, loaded_config = inspect_checkpoint(path)
            self.assertEqual(loaded_path, path.resolve())
            self.assertEqual(loaded_config, config)


class FakeTokenizer:
    def __init__(self) -> None:
        self.kwargs = None

    def apply_chat_template(self, messages, **kwargs):
        self.kwargs = kwargs
        return "<role>HUMAN</role>hello<|role_end|><role>ASSISTANT</role>"

    def encode(self, text, **kwargs):
        self.kwargs = kwargs
        return [1, 2, 3]


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        # Bypass __init__: these tests must never load MLX or model weights.
        self.engine = object.__new__(Engine)
        self.engine.config = {"vocab_size": 10, "max_position_embeddings": 20}

    def test_generation_rejects_bad_bounds_before_mlx_import(self) -> None:
        cases = [
            ({"max_tokens": 0}, "max_tokens"),
            ({"max_tokens": 2049}, "max_tokens"),
            ({"max_tokens": True}, "max_tokens"),
            ({"temperature": -0.1}, "temperature"),
            ({"temperature": float("nan")}, "temperature"),
            ({"temperature": True}, "temperature"),
            ({"seed": -1}, "seed"),
            ({"seed": 2**32}, "seed"),
            ({"prompt_ids": []}, "prompt_ids"),
            ({"prompt_ids": [-1]}, "prompt_ids"),
            ({"prompt_ids": [10]}, "prompt_ids"),
            ({"prompt_ids": [True]}, "prompt_ids"),
            ({"prompt_ids": [1] * 8193}, "prompt"),
            ({"prompt_ids": [1] * 19, "max_tokens": 2}, "context"),
            ({"use_cache": True}, "prefix caching"),
            ({"use_cache": "false"}, "use_cache"),
        ]
        for overrides, message in cases:
            args = {"prompt_ids": [1], "max_tokens": 2, **overrides}
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, message):
                self.engine.generate_tokens(**args)

    def test_native_prompt_template_receives_tools_and_thinking(self) -> None:
        tokenizer = FakeTokenizer()
        self.engine.tokenizer = tokenizer
        messages = [{"role": "user", "content": "hello"}]
        tools = [{"type": "function", "function": {"name": "lookup"}}]
        rendered = self.engine.render_prompt(messages, tools=tools, thinking=True)
        self.assertIn("<role>HUMAN</role>", rendered)
        self.assertEqual(tokenizer.kwargs["tools"], tools)
        self.assertTrue(tokenizer.kwargs["enable_thinking"])
        self.assertEqual(self.engine.tokenize(messages), [1, 2, 3])
        self.assertEqual(tokenizer.kwargs, {"add_special_tokens": False})

    def test_native_prompt_rejects_malformed_messages(self) -> None:
        self.engine.tokenizer = FakeTokenizer()
        for messages in ([], [{"role": "bad", "content": "x"}],
                         [{"role": "user", "content": ["x"]}]):
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                self.engine.render_prompt(messages)


class StreamingCoreTests(unittest.TestCase):
    def _fake_runtime(self, closed):
        mlx = types.ModuleType("mlx")
        mlx.__path__ = []
        core = types.ModuleType("mlx.core")
        core.random = types.SimpleNamespace(seed=lambda _seed: None)
        core.array = lambda values: values
        core.synchronize = lambda: None
        core.get_peak_memory = lambda: 100
        mlx.core = core
        mlx_lm = types.ModuleType("mlx_lm")
        mlx_lm.__path__ = []
        generate = types.ModuleType("mlx_lm.generate")

        def generate_step(*_args, **_kwargs):
            try:
                for token in (1, 2, 3, 4, 5):
                    yield token, None
            finally:
                closed.append(True)

        generate.generate_step = generate_step
        sample = types.ModuleType("mlx_lm.sample_utils")
        sample.make_sampler = lambda **_kwargs: object()
        return {"mlx": mlx, "mlx.core": core, "mlx_lm": mlx_lm,
                "mlx_lm.generate": generate, "mlx_lm.sample_utils": sample}

    def _fake_engine(self):
        class Detokenizer:
            def __init__(self):
                self.raw = b""
                self.text = ""
                self.offset = 0

            def add_token(self, token):
                self.raw += {1: b"A", 2: b" ", 3: b"\xe2", 4: b"\x82\xac"}[token]
                try:
                    self.text = self.raw.decode("utf-8")
                except UnicodeDecodeError:
                    pass

            @property
            def last_segment(self):
                segment = self.text[self.offset:]
                self.offset = len(self.text)
                return segment

            def finalize(self):
                self.text = self.raw.decode("utf-8")

        class Tokenizer:
            eos_token_ids = {5}

            @property
            def detokenizer(self):
                return Detokenizer()

            def decode(self, tokens, **_kwargs):
                return b"".join({1: b"A", 2: b" ", 3: b"\xe2", 4: b"\x82\xac"}[t]
                                for t in tokens).decode("utf-8")

        engine = object.__new__(Engine)
        engine.config = {"vocab_size": 10, "max_position_embeddings": 20}
        engine.model = object()
        engine.tokenizer = Tokenizer()
        engine._lock = threading.Lock()
        engine.optimized = False
        engine.implementation = "stock"
        return engine

    def test_unicode_segments_and_final_token_contract(self):
        closed, segments = [], []
        engine = self._fake_engine()
        with patch.dict(sys.modules, self._fake_runtime(closed)):
            result = engine.stream_text([1], segments.append, max_tokens=8)
        self.assertEqual("".join(segments), "A €")
        self.assertEqual(result["tokens"], [1, 2, 3, 4])
        self.assertEqual(result["stop_token_id"], 5)
        self.assertEqual(result["generation_tokens"], 4)
        self.assertTrue(closed)
        self.assertTrue(engine._lock.acquire(blocking=False))
        engine._lock.release()

    def test_repeated_streams_have_independent_detokenizer_state(self):
        closed = []
        engine = self._fake_engine()
        with patch.dict(sys.modules, self._fake_runtime(closed)):
            first_segments, second_segments = [], []
            first = engine.stream_text([1], first_segments.append, max_tokens=8)
            second = engine.stream_text([1], second_segments.append, max_tokens=8)
        self.assertEqual("".join(first_segments), "A €")
        self.assertEqual("".join(second_segments), "A €")
        self.assertEqual(first["tokens"], second["tokens"])
        self.assertEqual(len(closed), 2)

    def test_disconnect_closes_generator_and_releases_lock(self):
        closed = []
        engine = self._fake_engine()

        def disconnect(_segment):
            raise BrokenPipeError("client left")

        with patch.dict(sys.modules, self._fake_runtime(closed)):
            with self.assertRaises(BrokenPipeError):
                engine.stream_text([1], disconnect, max_tokens=8)
        self.assertTrue(closed)
        self.assertTrue(engine._lock.acquire(blocking=False))
        engine._lock.release()

    def test_cancel_signal_stops_before_emitting_content(self):
        closed, segments = [], []
        engine = self._fake_engine()
        with patch.dict(sys.modules, self._fake_runtime(closed)):
            with self.assertRaisesRegex(GenerationCancelled, "Client disconnected"):
                engine.stream_text([1], segments.append, max_tokens=8,
                                   cancelled=lambda: True)
        self.assertEqual(segments, [])
        self.assertTrue(closed)
        self.assertTrue(engine._lock.acquire(blocking=False))
        engine._lock.release()


class FakeHTTPBuilder:
    model_path = Path("/tmp/Ling-3.0-tiny-oQ4e")

    def __init__(self):
        self.tokenize_calls = 0
        self.generation_calls = 0

    def tokenize(self, messages, thinking=False):
        self.tokenize_calls += 1
        return [1, 2]

    def generate_tokens(self, ids, **kwargs):
        self.generation_calls += 1
        return {
            "text": "hello", "prompt_tokens": 2, "generation_tokens": 1,
            "cached_tokens": 0, "prefill_seconds": 0.1,
            "decode_seconds": 0.2, "total_seconds": 0.3,
            "peak_memory_bytes": 100, "finish_reason": "stop",
            "implementation": "stock",
        }

    def stream_text(self, ids, on_text, **kwargs):
        self.generation_calls += 1
        on_text("A ")
        kwargs["on_idle"]()
        on_text("€")
        return {
            "text": "A €", "prompt_tokens": 2, "generation_tokens": 3,
            "cached_tokens": 0, "prefill_seconds": 0.1,
            "decode_seconds": 0.2, "total_seconds": 0.3,
            "peak_memory_bytes": 100, "finish_reason": "stop",
            "implementation": "stock",
        }


class HTTPBoundaryTests(unittest.TestCase):
    def test_rejects_unsupported_fields_and_modes(self) -> None:
        engine = FakeHTTPBuilder()
        base = {"model": MODEL_ID, "messages": [{"role": "user", "content": "hello"}]}
        cases = [
            ({"tools": [{"type": "function"}]}, "Tool calling"),
            ({"model": "other"}, "Only model"),
            ({"top_p": 0.9}, "Only top_p"),
            ({"logprobs": True}, "Unsupported request fields"),
            ({"thinking": "false"}, "thinking"),
            ({"use_cache": "false"}, "use_cache"),
            ({"messages": "hello"}, "messages"),
            ({"max_tokens": 0}, "max_tokens"),
            ({"messages": [{"role": "tool", "content": "x"}]}, "Only system"),
            ({"messages": [{"role": "assistant", "content": "", "tool_calls": []}]}, "Tool-call history"),
            ({"stream_options": {"include_usage": True}}, "stream_options"),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, message):
                _run_chat(engine, {**base, **overrides})

    def test_success_reports_actual_usage_and_metrics(self) -> None:
        result = _run_chat(FakeHTTPBuilder(), {
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hello"}]
        })
        self.assertEqual(result["choices"][0]["message"]["content"], "hello")
        self.assertEqual(result["usage"], {
            "prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3,
        })
        self.assertEqual(result["ling_metrics"]["implementation"], "stock")

    def test_stream_request_is_prepared_before_headers(self) -> None:
        result = _prepare_chat(FakeHTTPBuilder(), {
            "model": MODEL_ID, "messages": [{"role": "user", "content": "hello"}],
            "stream": True, "stream_options": {"include_usage": True},
        })
        self.assertTrue(result["stream"])
        self.assertTrue(result["include_usage"])


class HTTPServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = FakeHTTPBuilder()
        cls.server = create_server(cls.engine, port=0, api_token="test-token")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method: str, path: str, payload=None, authenticated=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer test-token"
        body = json.dumps(payload).encode() if payload is not None else None
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read().decode("utf-8")
        status, content_type = response.status, response.getheader("Content-Type")
        connection.close()
        return status, content_type, data

    def raw_chat(self, headers: list[tuple[str, str]]):
        body = json.dumps({
            "model": MODEL_ID, "messages": [{"role": "user", "content": "hello"}],
        }).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.putrequest("POST", "/v1/chat/completions", skip_host=True)
        for key, value in headers:
            connection.putheader(key, value)
        connection.endheaders(body)
        response = connection.getresponse()
        status, payload = response.status, json.loads(response.read())
        connection.close()
        return status, payload

    def test_browser_boundary_rejects_foreign_and_malformed_headers_before_inference(self) -> None:
        expected = f"127.0.0.1:{self.port}"
        body = json.dumps({
            "model": MODEL_ID, "messages": [{"role": "user", "content": "hello"}],
        }).encode()
        base = [("Host", expected), ("Authorization", "Bearer test-token"),
                ("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
        cases = [
            ([("Host", "attacker.example"), *base[1:]], 403),
            ([*base, ("Origin", "https://attacker.example")], 403),
            ([*base, ("Origin", f"http://{expected}/path")], 403),
            ([("Host", expected), ("Host", "attacker.example"), *base[1:]], 400),
            ([*base, ("Origin", f"http://{expected}"),
              ("Origin", "https://attacker.example")], 400),
            ([*base[1:]], 400),
            ([*base[:2], ("Content-Type", "text/plain"), base[-1]], 415),
            ([*base, ("Content-Type", "text/plain")], 400),
        ]
        for headers, expected_status in cases:
            before = (self.engine.tokenize_calls, self.engine.generation_calls)
            with self.subTest(headers=headers):
                status, payload = self.raw_chat(headers)
                self.assertEqual(status, expected_status)
                self.assertIn("error", payload)
                self.assertEqual(
                    (self.engine.tokenize_calls, self.engine.generation_calls), before,
                )

    def test_native_loopback_json_with_or_without_same_origin_runs(self) -> None:
        expected = f"127.0.0.1:{self.port}"
        body = json.dumps({
            "model": MODEL_ID, "messages": [{"role": "user", "content": "hello"}],
        }).encode()
        headers = [("Host", expected), ("Authorization", "Bearer test-token"),
                   ("Content-Type", "application/json; charset=utf-8"),
                   ("Content-Length", str(len(body)))]
        before = self.engine.generation_calls
        for extra in ([], [("Origin", f"http://{expected}")]):
            status, payload = self.raw_chat(headers + extra)
            self.assertEqual(status, 200)
            self.assertEqual(payload["choices"][0]["message"]["content"], "hello")
        self.assertEqual(self.engine.generation_calls, before + 2)

    def test_health_models_and_auth(self) -> None:
        status, _, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertEqual(health["model"], MODEL_ID)
        self.assertFalse(health["capabilities"]["tools"])
        status, _, body = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"][0]["id"], MODEL_ID)
        status, _, body = self.request("GET", "/health", authenticated=False)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_api_key")

    def test_sse_content_finish_usage_and_done(self) -> None:
        status, content_type, body = self.request("POST", "/v1/chat/completions", {
            "model": MODEL_ID, "messages": [{"role": "user", "content": "hello"}],
            "stream": True, "stream_options": {"include_usage": True},
        })
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/event-stream"))
        self.assertIn(": keep-alive\n\n", body)
        events = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[-1], "[DONE]")
        chunks = [json.loads(event) for event in events[:-1]]
        self.assertEqual(chunks[0]["choices"][0]["delta"]["role"], "assistant")
        content = "".join(chunk["choices"][0]["delta"].get("content", "")
                          for chunk in chunks if chunk["choices"])
        self.assertEqual(content, "A €")
        self.assertEqual(chunks[-2]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(chunks[-1]["usage"]["completion_tokens"], 3)

    def test_http_validation_and_nonstream(self) -> None:
        base = {"model": MODEL_ID, "messages": [{"role": "user", "content": "hello"}]}
        status, _, body = self.request("POST", "/v1/chat/completions", base)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "hello")
        status, _, body = self.request("POST", "/v1/chat/completions", {**base, "model": "wrong"})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "model_not_found")
        status, _, body = self.request("POST", "/v1/chat/completions", {**base, "tools": [{"type": "function"}]})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "unsupported_tools")

    def test_malformed_json_has_structured_error(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request("POST", "/v1/chat/completions", body=b"{",
                           headers={"Authorization": "Bearer test-token",
                                    "Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")

    def test_nonloopback_bind_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            create_server(FakeHTTPBuilder(), "0.0.0.0", 0)


if __name__ == "__main__":
    unittest.main()
