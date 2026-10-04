"""Inference apps Wisp knows how to talk to, and detection of the ones running.

oMLX runs the whole assistant out of the box. Other OpenAI-compatible apps on a
loopback port serve chat and reasoning, and can also run the tool loop once they
pass Wisp's tool-calling test (see service/inference/qualify.py): Wisp will not
route tool calls to a model it has not qualified. Detection is read-only: it
asks a fixed set of 127.0.0.1 ports for their model list and nothing else.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import httpx

from service.config.endpoints import is_local_provider_origin

_MAX_BODY = 256 * 1024
_MAX_MODELS = 200
# httpx timeouts are per read, so a server that drips a byte at a time never trips them.
# Every probe therefore gets a hard wall-clock deadline for the whole exchange.
_PROBE_DEADLINE = 1.5


@dataclass(frozen=True)
class EngineProfile:
    id: str
    label: str
    port: int | None
    runs_tools: bool
    summary: str
    how_to_start: str
    url: str | None


OMLX = EngineProfile(
    "omlx", "oMLX", 8000, True,
    "Runs everything in Wisp, including tools and Smart Search.",
    "Open the oMLX menu-bar app; it starts its server on port 8000.",
    "https://github.com/jundot/omlx")

EXTERNAL_ENGINES = (
    EngineProfile("ollama", "Ollama", 11434, False,
                  "Chat and reasoning; tools too once it passes Wisp's test (needs a 16k+ context).",
                  "Start it with `ollama serve`, then pull a model.", "https://ollama.com"),
    EngineProfile("lmstudio", "LM Studio", 1234, False,
                  "Chat and reasoning; tools too once it passes Wisp's test (needs a 16k+ context).",
                  "Load a model, then start the local server from LM Studio's Developer tab.",
                  "https://lmstudio.ai"),
    EngineProfile("llamacpp", "llama.cpp", 8080, False,
                  "Chat and reasoning; tools too once it passes Wisp's test (needs a 16k+ context).",
                  "Run `llama-server -m model.gguf --port 8080`.",
                  "https://github.com/ggml-org/llama.cpp"),
    EngineProfile("mtplx", "MTPLX", None, False,
                  "Chat and reasoning; tools too once it passes Wisp's test. MTPLX defaults to port 8000, which oMLX and Wisp use, "
                  "so start it on a different port and connect it as an OpenAI-compatible app.",
                  "Start it on another port (see `mtplx start --help`), then connect it under “Other OpenAI-compatible app” below.",
                  "https://github.com/youssofal/MTPLX"),
    EngineProfile("custom", "Other OpenAI-compatible app", None, False,
                  "Any app on 127.0.0.1 that serves /v1/models and /v1/chat/completions.",
                  "Start its local server on a port other than 8000 or 8765, then enter it.", None),
)

# A local app Wisp has been used with before; probed alongside the well-known ports.
_EXTRA_PROBE_PORTS = (8767,)


@dataclass
class EngineState:
    profile: EngineProfile
    port: int | None
    running: bool = False
    models: list[str] = field(default_factory=list)


def _model_ids(payload: object) -> list[str]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    ids = [row["id"] for row in rows[:_MAX_MODELS]
           if isinstance(row, dict) and isinstance(row.get("id"), str) and 0 < len(row["id"]) <= 256]
    return sorted(set(ids))


async def _read_models(client: httpx.AsyncClient, port: int) -> tuple[bool, list[str]]:
    url = f"http://127.0.0.1:{port}/v1/models"
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                return False, []
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > _MAX_BODY:
                    return False, []
    except (httpx.HTTPError, OSError):
        return False, []
    try:
        return True, _model_ids(json.loads(body))
    except ValueError:
        return False, []


async def _probe(client: httpx.AsyncClient, port: int, deadline: float) -> tuple[bool, list[str]]:
    """One bounded probe: a slow, stalled, or endlessly streaming app is 'not detected'."""
    try:
        async with asyncio.timeout(deadline):
            return await _read_models(client, port)
    except TimeoutError:
        return False, []


async def detect_external_engines(timeout: float = 0.8, *, deadline: float = _PROBE_DEADLINE,
                                  transport: httpx.AsyncBaseTransport | None = None) -> list[EngineState]:
    """Report which known external apps are answering on their default port."""
    targets: list[tuple[EngineProfile, int]] = [
        (p, p.port) for p in EXTERNAL_ENGINES if p.port is not None
        and is_local_provider_origin(f"http://127.0.0.1:{p.port}")]
    known = {port for _, port in targets}
    custom = next(p for p in EXTERNAL_ENGINES if p.id == "custom")
    targets += [(custom, port) for port in _EXTRA_PROBE_PORTS
                if port not in known and is_local_provider_origin(f"http://127.0.0.1:{port}")]
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                 trust_env=False, transport=transport) as client:
        results = await asyncio.gather(*(_probe(client, port, deadline) for _, port in targets))
    return [EngineState(profile, port, running, models)
            for (profile, port), (running, models) in zip(targets, results) if running]
