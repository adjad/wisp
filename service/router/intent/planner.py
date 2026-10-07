"""Bounded, opt-in resident Ling interpretation; no startup or residency effects."""
from __future__ import annotations
import asyncio
import json
import os
from datetime import datetime
from typing import Awaitable, Callable
from urllib.parse import urlsplit
from service.config.endpoints import Endpoint, Target, is_loopback, role_target
from .schema import DOMAINS, SCHEMA, PlanningResult
from .request import SYSTEM, natural_context, build_messages, completion_options, repair_message
from .validation import InvalidIntent, applicable_read, source_requirements, validate_intent
from .compiler import UnsupportedRead, compile_intent


def enabled(config: dict | None) -> bool:
    return bool(config and config.get("enabled") is True and
                os.environ.get("WISP_INTENT_ROUTER_KILL", "").strip().lower() not in {"1", "true", "yes", "on"})


def _client_matches_target(client, target: Target) -> bool:
    """Compare frozen routing/transport metadata without reading credentials.

    The legacy shared client has no Target. It can serve only the canonical
    unqualified local binding; custom bindings need explicit target provenance.
    Synthetic clients supply the same metadata, with an inert transport stub.
    """
    if not isinstance(target, Target) or not isinstance(target.endpoint, Endpoint):
        return False
    ep = target.endpoint
    if (target.role != "router" or not target.model or "ling" not in target.model.casefold()
            or ep.name != "local" or ep.managed is not True or not is_loopback(ep.base_url)
            or ep.provider != "omlx" or ep.api_prefix != "/v1"):
        return False
    parsed = urlsplit(ep.base_url)
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        return False
    if (getattr(client, "managed", False) is not True
            or not isinstance(getattr(client, "base_url", None), str)
            or client.base_url.rstrip("/") != ep.base_url.rstrip("/")
            or getattr(client, "endpoint_name", None) != ep.name
            or getattr(getattr(client, "provider", None), "name", None) != ep.provider
            or getattr(client, "api_prefix", None) != ep.api_prefix):
        return False
    supplied = getattr(client, "target", None)
    if supplied is None:
        if ep.credential_ref != "local_omlx" or target.revision or target.profile or target.dimensions:
            return False
    elif (not isinstance(supplied, Target) or supplied.role != target.role
          or supplied != target
          or supplied.endpoint.credential_ref != ep.credential_ref
          or supplied.endpoint.managed is not True):
        return False
    transport = getattr(client, "_credential_transport", None)
    # CredentialTransport already enforces origin and process attribution. Do
    # not replace it, resolve its keys, or reinterpret managed=True as identity.
    origin = getattr(transport, "origin", None)
    return (origin is not None and str(origin).rstrip("/") == ep.base_url.rstrip("/")
            and getattr(transport, "backend", None) is not None)


async def resident_eligible(client, target: Target) -> bool:
    """Read-only resource seam; never start an engine or change residency."""
    if not _client_matches_target(client, target):
        return False
    status = await client.status()
    return any(row.get("id") == target.model and row.get("loaded") is True
               for row in status.get("models", []) if isinstance(row, dict))


async def plan_read(prompt: str, *, client, config: dict | None, model: str | None = None,
                    context=(), prior_tools=(), now: datetime | None = None,
                    eligibility: Callable[..., Awaitable[bool]] | None = None,
                    on_generation: Callable[[Target], None] | None = None) -> PlanningResult | None:
    if not enabled(config) or not applicable_read(prompt, context, prior_tools):
        return None
    domains = config.get("domains", [])
    allowed = set(domains) & DOMAINS if isinstance(domains, (list, tuple)) else set()
    required, _ = source_requirements(prompt)
    if not allowed or required - allowed:
        return None
    now = now or datetime.now().astimezone()
    messages = build_messages(prompt, context=context, prior_tools=prior_tools, now=now)
    # Validation uses the builder-selected history; wire preservation awaits strict fitting.
    history = messages[1:-1]
    # Config cannot redirect to a model, override the configured provider, or
    # grow generation unbounded. One overall deadline includes residency checks,
    # transport, parse/validation, and at most one repair.
    try:
        seconds = max(0.05, min(float(config.get("deadline_seconds", 2.5)), 5.0))
    except (TypeError, ValueError):
        seconds = 2.5
    attempts = 0
    try:
        async with asyncio.timeout(seconds):
            target = role_target("router")
            if (client is None or not _client_matches_target(client, target)
                    or (model is not None and model != target.model)):
                return PlanningResult("clarify", response="I cannot safely resolve this read with the configured local routing endpoint. Please name the source, date range, and filters explicitly.", reason="routing target unavailable or mismatched", attempts=0)
            model = target.model
            for attempt in range(2 if config.get("repair", True) is True else 1):
                if client is None or not await (eligibility or resident_eligible)(client, target):
                    return PlanningResult("clarify", response="I cannot safely resolve this read while the local routing model is unavailable. Please name the source, date range, and filters explicitly.", reason="resident model unavailable", attempts=attempts)
                attempts += 1
                if on_generation is not None:
                    on_generation(target)
                response = await client.chat(model, messages, **completion_options(SCHEMA))
                try:
                    choice = response["choices"][0]
                    if choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls"):
                        raise InvalidIntent("Incomplete or tool-bearing intent output")
                    content = choice["message"].get("content")
                    if not isinstance(content, str) or len(content) > 16000:
                        raise InvalidIntent("Missing or oversized intent object")
                    def unique_object(pairs):
                        value = {}
                        for key, item in pairs:
                            if key in value:
                                raise InvalidIntent("Duplicate JSON fields")
                            value[key] = item
                        return value
                    value = json.loads(content, object_pairs_hook=unique_object)
                    intent = validate_intent(value, prompt, context=history, prior_tools=prior_tools, now=now)
                    if any(source.domain not in allowed for source in intent.sources):
                        return PlanningResult("clarify", response="That read source is not enabled for structured routing. Please ask for an enabled source separately.", intent=intent, reason="domain disabled", attempts=attempts)
                    if intent.kind in {"none", "inline"}:
                        return PlanningResult("declined", intent=intent, reason="no source read", attempts=attempts)
                    if intent.kind == "unsupported":
                        return PlanningResult("clarify", response="I cannot safely apply this request with the available structured reads. Please specify the source and supported filters.", intent=intent, reason="unsupported intent", attempts=attempts)
                    calls, text = compile_intent(intent, now=now)
                    return PlanningResult("compiled", tuple(calls), text, intent, "validated read intent", attempts)
                except UnsupportedRead as exc:
                    return PlanningResult("clarify", response=str(exc), reason="unsupported filter", attempts=attempts)
                except (InvalidIntent, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                    # Never repeat the full model output (it may contain injected
                    # prose); only our bounded validation diagnosis guides repair.
                    messages.append(repair_message(str(exc)))
    except asyncio.CancelledError:
        raise
    except Exception:
        # No unsafe broad rule/menu fallback after an applicable read has failed.
        return PlanningResult("clarify", response="I could not safely resolve that read within the routing budget. Please name the source, date range, and filters explicitly.", reason="planner unavailable or deadline", attempts=attempts)
    return PlanningResult("clarify", response="I could not validate all sources and filters in that read. Please clarify the source, date range, and exact search text.", reason="invalid intent after bounded repair", attempts=attempts)
