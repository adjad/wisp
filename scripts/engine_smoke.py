#!/usr/bin/env python3
"""Start a private bundled engine and exercise its chat and embeddings APIs."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.engine import EngineLayout, EngineManager  # noqa: E402

RESERVED_PORTS = frozenset({8000, 8765, 8791})


class SmokeError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-root", type=Path, required=True,
                        help="bundled engine directory containing py/, sp/, omlx/ and omlx_web/")
    parser.add_argument("--model-dir", type=Path, required=True,
                        help="directory containing at least one downloaded model")
    parser.add_argument("--model", help="optional model id to require in the model listing")
    parser.add_argument("--embedding-model", help="model id for the /v1/embeddings check (skipped when absent)")
    parser.add_argument("--rerank-model", help="model id for the /v1/rerank check (skipped when absent)")
    return parser


def validate_port(port: int) -> int:
    if port in RESERVED_PORTS:
        raise SmokeError(f"Refusing reserved port {port}")
    if not 1024 <= port <= 65535:
        raise SmokeError(f"Port {port} is outside the usable range")
    return port


def random_free_port() -> int:
    """Ask the OS for an unused ephemeral port, excluding every Wisp/oMLX port."""
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port not in RESERVED_PORTS:
            return validate_port(port)
    raise SmokeError("Could not allocate a non-reserved free port")


def _model_id(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    value = item.get("id") or item.get("model")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _directory_size(path: Path) -> int:
    total = 0
    for current, directories, files in os.walk(path, followlinks=False):
        here = Path(current)
        directories[:] = [name for name in directories if not (here / name).is_symlink()]
        for name in files:
            file_path = here / name
            try:
                if not file_path.is_symlink():
                    total += file_path.stat().st_size
            except OSError:
                continue
    return total


def _model_directory(model_id: str, item: dict, model_dir: Path) -> Path | None:
    root = model_dir.resolve(strict=True)
    names = [model_id, model_id.replace("/", "--"), model_id.rsplit("/", 1)[-1]]
    for field in ("path", "model_path", "local_path"):
        value = item.get(field)
        if isinstance(value, str) and value:
            names.insert(0, value)
    for name in names:
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() and resolved.is_relative_to(root):
            return resolved
    return None


def choose_smallest_model(models: list[Any], model_dir: Path, required_model: str | None = None) -> tuple[str, int]:
    choices = []
    for item in models:
        model_id = _model_id(item)
        if model_id is None or (required_model is not None and model_id != required_model):
            continue
        directory = _model_directory(model_id, item, model_dir)
        if directory is not None:
            choices.append((_directory_size(directory), model_id))
    if not choices:
        qualifier = f" named {required_model!r}" if required_model else ""
        raise SmokeError(f"Could not find a listed model{qualifier} under --model-dir")
    size, model_id = min(choices, key=lambda choice: (choice[0], choice[1].casefold()))
    return model_id, size


def _embedding_dimensions(payload: dict) -> int:
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise SmokeError("Embeddings response has no data vector")
    vector = data[0].get("embedding")
    if not isinstance(vector, list) or not vector:
        raise SmokeError("Embeddings response has an empty or invalid vector")
    return len(vector)


def visible_chat_answer(payload: object) -> str:
    """A 200 or a choices envelope is not evidence that a model can answer."""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
        raise SmokeError("Chat completion did not finish successfully")
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise SmokeError("Chat completion has no assistant message")
    content = message.get("content")
    if not isinstance(content, str):
        raise SmokeError("Chat completion has no visible answer")
    content = re.sub(r"(?s)<think>.*?</think>", "", content)
    # An unclosed reasoning block is not a visible answer either.
    content = content.split("<think>", 1)[0].strip()
    if not content:
        raise SmokeError("Chat completion has no visible answer")
    return content


def run_smoke(engine_root: Path, model_dir: Path, model: str | None = None,
              embedding_model: str | None = None, rerank_model: str | None = None) -> dict:
    engine_root = Path(engine_root).resolve(strict=True)
    model_dir = Path(model_dir).resolve(strict=True)
    if not engine_root.is_dir() or not model_dir.is_dir():
        raise SmokeError("--engine-root and --model-dir must be directories")
    port = validate_port(random_free_port())
    manager = None
    with tempfile.TemporaryDirectory(prefix="wisp-engine-smoke-") as temp_data:
        layout = EngineLayout(engine_root, Path(temp_data) / "data")
        manager = EngineManager(layout, port=port, extra_model_dirs=(model_dir,))
        try:
            started = time.monotonic()
            manager.start()
            if not asyncio.run(manager.wait_ready(timeout=300)):
                raise SmokeError("Bundled engine did not become ready")
            startup_seconds = time.monotonic() - started

            headers = {"Authorization": f"Bearer {manager.api_key}"}
            base = f"http://127.0.0.1:{port}"
            timeout = httpx.Timeout(600.0, connect=10.0)
            with httpx.Client(headers=headers, timeout=timeout) as client:
                model_response = client.get(f"{base}/v1/models")
                model_response.raise_for_status()
                model_payload = model_response.json()
                models = model_payload.get("data") if isinstance(model_payload, dict) else None
                if not isinstance(models, list) or not models:
                    raise SmokeError("Engine listed no models")
                listed = {_model_id(item) for item in models}
                for wanted in (embedding_model, rerank_model):
                    if wanted is not None and wanted not in listed:
                        raise SmokeError(f"Engine did not list {wanted!r}")
                chat_candidates = [item for item in models
                                   if _model_id(item) not in (embedding_model, rerank_model)]
                selected_model, model_bytes = choose_smallest_model(chat_candidates, model_dir, model)

                started = time.monotonic()
                chat_response = client.post(f"{base}/v1/chat/completions", json={
                    "model": selected_model,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "max_tokens": 512,
                    "temperature": 0,
                })
                chat_response.raise_for_status()
                chat_payload = chat_response.json()
                chat_text = visible_chat_answer(chat_payload)
                chat_seconds = time.monotonic() - started

                embedding_dimensions = None
                if embedding_model is not None:
                    embedding_response = client.post(f"{base}/v1/embeddings", json={
                        "model": embedding_model,
                        "input": "Wisp bundled engine smoke test.",
                    })
                    embedding_response.raise_for_status()
                    embedding_dimensions = _embedding_dimensions(embedding_response.json())
                rerank_results = None
                if rerank_model is not None:
                    rerank_response = client.post(f"{base}/v1/rerank", json={
                        "model": rerank_model,
                        "query": "capital of France",
                        "documents": ["Paris is the capital of France.", "Bananas are yellow."],
                    })
                    rerank_response.raise_for_status()
                    rerank_results = rerank_response.json().get("results")
                    if not isinstance(rerank_results, list) or len(rerank_results) != 2:
                        raise SmokeError("Rerank response does not score both documents")
                return {
                    "ok": True,
                    "port": port,
                    "models": sorted(filter(None, (_model_id(item) for item in models)), key=str.casefold),
                    "model": selected_model,
                    "model_bytes": model_bytes,
                    "chat_completion": "passed",
                    "startup_seconds": round(startup_seconds, 3),
                    "chat_seconds": round(chat_seconds, 3),
                    "embedding_dimensions": embedding_dimensions,
                    "rerank_results": rerank_results,
                    "chat_text": chat_text,
                }
        finally:
            manager.stop()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_smoke(args.engine_root, args.model_dir, args.model,
                           args.embedding_model, args.rerank_model)
    except Exception as exc:  # keep a single machine-readable result on every failure path
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
