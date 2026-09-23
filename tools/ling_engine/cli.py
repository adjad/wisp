"""CLI and loopback OpenAI-style chat interface for the Ling prototype."""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import select
import socket
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from engine import (
    MAX_GENERATION_TOKENS,
    MAX_PROMPT_TOKENS,
    Engine,
    GenerationCancelled,
    inspect_checkpoint,
)

DEFAULT_MODEL = Path(
    "/Users/adijain/Desktop/OMLX_Model_Files/TheWirelessPhoenix/Ling-3.0-tiny-oQ4e"
)
MODEL_ID = "Ling-3.0-tiny-oQ4e"
MAX_REQUEST_BYTES = 1_048_576


class APIError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_request"):
        super().__init__(message)
        self.status = status
        self.code = code


def _hardware() -> dict[str, Any]:
    def sysctl(key: str) -> str | None:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", key], capture_output=True, text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    return {
        "cpu": sysctl("machdep.cpu.brand_string"),
        "memory_bytes": (
            int(value) if (value := sysctl("hw.memsize")) is not None else None
        ),
        "accelerator": "MLX default device; actual GPU use requires measurement",
    }


def _prepare_chat(engine: Engine, request: dict[str, Any]) -> dict[str, Any]:
    supported = {
        "model", "messages", "max_tokens", "temperature", "seed", "thinking",
        "use_cache", "stream", "stream_options", "tools", "tool_choice",
        "top_p", "n",
    }
    unsupported = set(request) - supported
    if unsupported:
        raise APIError(f"Unsupported request fields: {', '.join(sorted(unsupported))}")
    if request.get("model") != MODEL_ID:
        raise APIError(f"Only model {MODEL_ID} is loaded", 404, "model_not_found")
    if request.get("tools") not in (None, []) or request.get("tool_choice") not in (None, "none"):
        raise APIError("Tool calling is not supported by this endpoint", code="unsupported_tools")
    if (type(request.get("top_p", 1)) not in (int, float)
            or request.get("top_p", 1) != 1
            or type(request.get("n", 1)) is not int
            or request.get("n", 1) != 1):
        raise APIError("Only top_p=1 and n=1 are supported")
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 64:
        raise APIError("messages must be a nonempty list of at most 64 messages")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}:
            raise APIError("Only system, user, and assistant text messages are supported")
        if not isinstance(message.get("content"), str):
            raise APIError("Every message content must be a string")
        if any(key in message for key in ("tool_calls", "tool_call_id", "function_call")):
            raise APIError("Tool-call history is not supported", code="unsupported_tools")
        if set(message) - {"role", "content"}:
            raise APIError("Unsupported message fields")
    stream = request.get("stream", False)
    thinking = request.get("thinking", False)
    use_cache = request.get("use_cache", False)
    if any(type(value) is not bool for value in (stream, thinking, use_cache)):
        raise APIError("stream, thinking, and use_cache must be booleans")
    if use_cache:
        raise APIError("Cross-request prefix caching is not implemented")
    options = request.get("stream_options")
    if options is not None and (not stream or not isinstance(options, dict) or set(options) - {"include_usage"}):
        raise APIError("stream_options supports only include_usage with stream=true")
    include_usage = options.get("include_usage", False) if options is not None else False
    if type(include_usage) is not bool:
        raise APIError("stream_options.include_usage must be a boolean")
    max_tokens = request.get("max_tokens", 128)
    temperature = request.get("temperature", 0.0)
    seed = request.get("seed", 0)
    if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_GENERATION_TOKENS:
        raise APIError(f"max_tokens must be 1..{MAX_GENERATION_TOKENS}")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not math.isfinite(temperature) or temperature < 0:
        raise APIError("temperature must be a finite nonnegative number")
    if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
        raise APIError("seed must be a 32-bit nonnegative integer")
    ids = engine.tokenize(messages, thinking=thinking)
    if not ids or len(ids) > MAX_PROMPT_TOKENS:
        raise APIError(f"prompt must contain 1..{MAX_PROMPT_TOKENS} tokens")
    if hasattr(engine, "config") and len(ids) + max_tokens > engine.config["max_position_embeddings"]:
        raise APIError("prompt plus generation exceeds model context length")
    return {
        "ids": ids, "stream": stream, "include_usage": include_usage,
        "max_tokens": max_tokens, "temperature": temperature, "seed": seed,
    }


def _usage(result: dict[str, Any]) -> dict[str, int]:
    return {
        "prompt_tokens": result["prompt_tokens"],
        "completion_tokens": result["generation_tokens"],
        "total_tokens": result["prompt_tokens"] + result["generation_tokens"],
    }


def _run_chat(engine: Engine, request: dict[str, Any], prepared: dict[str, Any] | None = None,
              cancelled: Any = None) -> dict[str, Any]:
    prepared = prepared or _prepare_chat(engine, request)
    if prepared["stream"]:
        raise APIError("Streaming requests must use the SSE endpoint")
    result = engine.generate_tokens(
        prepared["ids"], max_tokens=prepared["max_tokens"],
        temperature=prepared["temperature"], seed=prepared["seed"],
        use_cache=False, cancelled=cancelled,
    )
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": result["text"]},
                     "finish_reason": result["finish_reason"]}],
        "usage": _usage(result),
        "ling_metrics": {
            key: result[key] for key in (
                "cached_tokens", "prefill_seconds", "decode_seconds",
                "total_seconds", "peak_memory_bytes", "implementation",
            )
        },
    }


def create_server(engine: Engine, host: str = "127.0.0.1", port: int = 8767,
                  api_token: str | None = None) -> ThreadingHTTPServer:
    if not ip_address(host).is_loopback:
        raise ValueError("Server may bind only to a numeric loopback address")
    if not 0 <= port <= 65535:
        raise ValueError("port must be 0..65535")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *args: Any) -> None:
            # Do not log prompts, request paths, headers, or bearer tokens.
            pass

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def _error(self, error: APIError) -> None:
            self._json(error.status, {"error": {
                "message": str(error),
                "type": "server_error" if error.status >= 500 else "invalid_request_error",
                "code": error.code,
            }})

        def _authorized(self) -> bool:
            if api_token is None:
                return True
            expected = f"Bearer {api_token}"
            if hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                return True
            self._json(401, {"error": {
                "message": "Invalid or missing bearer token",
                "type": "authentication_error", "code": "invalid_api_key",
            }})
            return False

        def _request_boundary(self, post: bool = False) -> bool:
            """Keep browser and DNS-rebinding requests outside the model boundary."""
            try:
                def singleton(name: str, required: bool = False) -> str | None:
                    values = self.headers.get_all(name, [])
                    if len(values) > 1 or (required and len(values) != 1):
                        raise APIError(f"Invalid {name} header", code="invalid_header")
                    return values[0] if values else None

                bound_host, bound_port = self.server.server_address[:2]
                authority = (f"[{bound_host}]" if ":" in bound_host else bound_host)
                expected_host = f"{authority}:{bound_port}"
                if singleton("Host", required=True) != expected_host:
                    raise APIError("Host must match the loopback listener", 403, "invalid_host")
                origin = singleton("Origin")
                if origin is not None and origin != f"http://{expected_host}":
                    raise APIError("Origin must match the loopback listener", 403, "invalid_origin")
                singleton("Authorization")
                if post:
                    content_type = singleton("Content-Type", required=True)
                    if not re.fullmatch(
                        r'application/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?',
                        content_type.strip(), flags=re.IGNORECASE,
                    ):
                        raise APIError("Content-Type must be application/json", 415,
                                       "unsupported_media_type")
                    singleton("Content-Length", required=True)
                    if self.headers.get_all("Transfer-Encoding", []):
                        raise APIError("Transfer-Encoding is not supported", code="invalid_header")
                return True
            except APIError as error:
                # The rejected POST body has not been consumed. Do not let it
                # become a second request on a persistent HTTP/1.1 connection.
                self.close_connection = True
                self._error(error)
                return False

        def _event(self, payload: dict[str, Any] | str) -> None:
            value = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            self.wfile.write(f"data: {value}\n\n".encode("utf-8"))
            self.wfile.flush()

        def _comment(self) -> None:
            self.wfile.write(b": keep-alive\n\n")
            self.wfile.flush()

        def _client_closed(self) -> bool:
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                if not readable:
                    return False
                return self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
            except (BlockingIOError, InterruptedError):
                return False
            except OSError:
                return True

        def _stream_chat(self, prepared: dict[str, Any]) -> None:
            response_id = "chatcmpl-" + uuid.uuid4().hex
            created = int(time.time())

            def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
                return {
                    "id": response_id, "object": "chat.completion.chunk",
                    "created": created, "model": MODEL_ID,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                }

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.connection.settimeout(10)
            try:
                self._event(chunk({"role": "assistant", "content": ""}))
                result = engine.stream_text(
                    prepared["ids"],
                    on_text=lambda segment: self._event(chunk({"content": segment})),
                    on_idle=self._comment,
                    cancelled=self._client_closed,
                    max_tokens=prepared["max_tokens"],
                    temperature=prepared["temperature"],
                    seed=prepared["seed"], use_cache=False,
                )
                self._event(chunk({}, result["finish_reason"]))
                if prepared["include_usage"]:
                    self._event({
                        "id": response_id, "object": "chat.completion.chunk",
                        "created": created, "model": MODEL_ID,
                        "choices": [], "usage": _usage(result),
                    })
                self._event("[DONE]")
            except (GenerationCancelled, BrokenPipeError, ConnectionResetError, TimeoutError):
                return
            except OSError:
                return
            except Exception:
                try:
                    self._event({"error": {
                        "message": "Generation failed", "type": "server_error",
                        "code": "generation_failed",
                    }})
                    self._event("[DONE]")
                except OSError:
                    pass

        def do_GET(self) -> None:
            if not self._request_boundary():
                return
            if not self._authorized():
                return
            if self.path == "/health":
                self._json(200, {"status": "ready", "model": MODEL_ID,
                                 "capabilities": {"chat_completions": True,
                                                  "streaming": True, "tools": False}})
            elif self.path == "/v1/models":
                self._json(200, {"object": "list", "data": [{
                    "id": MODEL_ID, "object": "model", "owned_by": "local",
                }]})
            else:
                self._error(APIError("Unknown endpoint", 404, "not_found"))

        def do_POST(self) -> None:
            if not self._request_boundary(post=True):
                return
            if not self._authorized():
                return
            if self.path != "/v1/chat/completions":
                self._error(APIError("Unknown endpoint", 404, "not_found"))
                return
            try:
                self.connection.settimeout(10)
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_REQUEST_BYTES:
                    raise APIError(f"JSON request size must be 1..{MAX_REQUEST_BYTES} bytes", 413, "request_too_large")
                request = json.loads(self.rfile.read(size))
                if not isinstance(request, dict):
                    raise APIError("JSON request must be an object")
                prepared = _prepare_chat(engine, request)
                if prepared["stream"]:
                    self._stream_chat(prepared)
                else:
                    self._json(200, _run_chat(engine, request, prepared,
                                              cancelled=self._client_closed))
            except (GenerationCancelled, BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                return
            except APIError as error:
                self._error(error)
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._error(APIError(str(error)))
            except Exception:
                self._error(APIError("Generation failed", 500, "generation_failed"))

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    return Server((host, port), Handler)


def serve(engine: Engine, host: str, port: int) -> None:
    token = os.environ.get("LING_ENGINE_API_TOKEN") or None
    server = create_server(engine, host, port, api_token=token)
    print(f"Ling engine listening on http://{host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inspect", help="Read checkpoint and hardware metadata; do not load weights")
    gen = sub.add_parser("generate", help="Run one bounded chat generation")
    gen.add_argument("--prompt", required=True)
    gen.add_argument("--system")
    gen.add_argument("--max-tokens", type=int, default=128)
    gen.add_argument("--temperature", type=float, default=0.0)
    gen.add_argument("--seed", type=int, default=0)
    gen.add_argument("--thinking", action="store_true")
    server = sub.add_parser("serve", help="Serve local chat JSON and SSE on loopback")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    if args.command == "inspect":
        path, config = inspect_checkpoint(args.model)
        quant = config["quantization"]
        overrides = [v for v in quant.values() if isinstance(v, dict)]
        print(json.dumps({
            "model_path": str(path), "model_type": config["model_type"],
            "layers": config["num_hidden_layers"],
            "quantization": {key: quant[key] for key in ("bits", "group_size", "mode")},
            "quantized_overrides": len(overrides), "hardware": _hardware(),
        }, indent=2))
        return 0
    engine = Engine(args.model)
    if args.command == "generate":
        messages = []
        if args.system:
            messages.append({"role": "system", "content": args.system})
        messages.append({"role": "user", "content": args.prompt})
        result = engine.generate_tokens(
            engine.tokenize(messages, thinking=args.thinking),
            max_tokens=args.max_tokens, temperature=args.temperature, seed=args.seed,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    serve(engine, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
