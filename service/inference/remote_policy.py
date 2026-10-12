"""Process-pinned, opt-in Intel/GX10 remote-inference policy; never resolves credentials.

The launcher supplies WISP_REMOTE_ONLY=1 plus the exact nonsecret profile in
inference.remote_only. Settings cannot change this snapshot after startup.

Two modes share the pinned origin, model, Keychain-only credential and payload
validation:

* read-only (``WISP_REMOTE_ONLY=1`` alone): only the seven native reads and
  the behavior tools; every other tool, approval and write endpoint is
  refused. Budgets are the original 8k gateway contract.
* full (``WISP_REMOTE_ONLY=1`` + ``WISP_REMOTE_FULL=1``, Intel v5): every
  built-in tool that does not need a local model or a Wisp-local extension
  runtime, each decided by the normal safety policy (service/safety/policy.py
  and grants.py) exactly as on a Mac with local models. Budgets follow the
  100k-context GX10 contract (REMOTE_FULL_PROFILE).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from functools import lru_cache
import json
import math
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from service.config.endpoints import Target

_FORCED_MODE = os.environ.get("WISP_REMOTE_ONLY")
# Read once at import, like _FORCED_MODE: the mode is a launch decision.
_FULL_MODE = os.environ.get("WISP_REMOTE_FULL")
_READ_TOOLS = frozenset({"get_upcoming", "get_past_events", "search_reminders",
                         "view_emails", "summarize_emails", "view_messages",
                         "summarize_messages"})
_BEHAVIOR_TOOLS = frozenset({"describe_me", "predict_my_day", "my_patterns", "correct_pattern"})
# Built-ins that full remote mode never offers or runs. Each needs something
# this build does not have: create_tool generates code with the local coding
# model and installs it; use_skill/wisp_skills/wisp_mcp serve the Wisp-local
# skill and MCP runtimes, which never start in a remote-only build (and their
# content must not leave the Mac). Everything else is left to the normal
# safety policy, never to this list.
_REMOTE_FULL_EXCLUDED = frozenset({"create_tool", "use_skill", "wisp_skills", "wisp_mcp"})


def _refuse(message: str):
    from service.config.endpoints import EndpointConfigurationError
    raise EndpointConfigurationError(message)


@dataclass(frozen=True)
class RemoteOnlyProfile:
    enabled: bool = True
    endpoint_name: str = "gx10_demo"
    base_url: str = "https://6wjp.tail4cc204.ts.net"
    provider: str = "openai-compatible"
    api_prefix: str = "/v1"
    credential_ref: str = "keychain:gx10demo"
    model_id: str = "qwen3.6:35b"
    context_window: int = 8000
    max_tokens: int = 4096
    readiness_timeout: float = 5.0
    request_bytes: int = 131072
    messages: int = 32
    aggregate_bytes: int = 24000
    tools: int = 32
    response_bytes: int = 4194304
    generation_roles: tuple[str, ...] = ("fast", "router", "agent", "general", "reasoning", "coding")
    tool_capabilities: tuple[str, ...] = ("tools",)

    def fit_output(self, value: int) -> int:
        if type(value) is not int or value <= 0:
            _refuse("Remote output budget must be a positive integer")
        return min(value, self.max_tokens)

    def validate_payload(self, payload: dict) -> None:
        allowed = {"model", "messages", "max_tokens", "stream", "temperature", "tools", "tool_choice"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            _refuse("Unsupported remote-only inference options")
        if payload.get("model") != self.model_id or type(payload.get("stream")) is not bool:
            _refuse("Remote-only inference requires its exact model and stream flag")
        value = payload.get("max_tokens")
        if type(value) is not int or not 1 <= value <= self.max_tokens:
            _refuse("Remote-only output budget exceeds the gateway contract")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= self.messages:
            _refuse("Remote-only message budget exceeded")
        for row in messages:
            if not isinstance(row, dict) or not isinstance(row.get("role"), str):
                _refuse("Invalid remote-only message")
            if row["role"] not in {"system", "user", "assistant", "tool"}:
                _refuse("Unsupported remote-only message role")
            content = row.get("content")
            if not isinstance(content, str) and not (row["role"] == "assistant" and row.get("tool_calls") and content is None):
                _refuse("Remote-only inference accepts text and tool transcripts only")
        tools = payload.get("tools")
        if tools is not None and (not isinstance(tools, list) or not 1 <= len(tools) <= self.tools):
            _refuse("Remote-only tool schema budget exceeded")
        if "temperature" in payload:
            t = payload["temperature"]
            if type(t) not in {int, float}:
                _refuse("Invalid remote-only temperature")
            try:
                valid = math.isfinite(t) and 0 <= t <= 2
            except OverflowError:
                valid = False
            if not valid:
                _refuse("Invalid remote-only temperature")
        try:
            wire = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            aggregate = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False,
                                   separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError, UnicodeError):
            _refuse("Invalid bounded remote-only payload")
        if len(wire) > self.request_bytes or len(aggregate) > self.aggregate_bytes:
            _refuse("Remote-only transcript/schema byte budget exceeded")


# The v5 full-mode gateway contract: the GX10 serves qwen3.6:35b with a
# 32,000-token context. Every other field (origin, model id, credential,
# roles, tool count, readiness timeout, response bound) is identical to the
# read-only contract, so the packaged profile differs ONLY in these values.
REMOTE_FULL_LIMITS = {"context_window": 32000, "max_tokens": 8192,
                      "request_bytes": 524288, "messages": 200,
                      "aggregate_bytes": 131072}
REMOTE_FULL_PROFILE = RemoteOnlyProfile(**REMOTE_FULL_LIMITS)


def _full_requested() -> bool:
    if _FULL_MODE not in {None, "1"}:
        _refuse("Invalid remote full launch mode")
    if _FULL_MODE != "1":
        return False
    if _FORCED_MODE != "1":
        _refuse("Remote full mode requires the forced remote-only launch")
    return True


@lru_cache(maxsize=1)
def remote_only_profile() -> RemoteOnlyProfile | None:
    """Validate one startup snapshot; deliberately not cleared by Settings."""
    if _FORCED_MODE not in {None, "1"}:
        _refuse("Invalid remote-only launch mode")
    full = _full_requested()
    from service.config import models_config
    config = models_config()
    inference = config.get("inference", {})
    if not isinstance(inference, dict):
        _refuse("Invalid inference configuration")
    raw = inference.get("remote_only")
    # Only the launcher's WISP_REMOTE_ONLY=1 opts in. A profile left in packaged
    # defaults or ~/.moe/config.yaml must never move a normal Mac's inference
    # (and its native reads) to a remote server on its own.
    if _FORCED_MODE is None:
        return None
    # Full mode accepts only the full (32k) contract. Read-only mode (the
    # build) accepts the original 8k contract or, because the Intel
    # v5 package ships ONE profile for both of its runtime modes, the full
    # contract: the larger budgets add no tools, writes or endpoints there.
    candidates = (REMOTE_FULL_PROFILE,) if full else (RemoteOnlyProfile(), REMOTE_FULL_PROFILE)
    fields = asdict(candidates[0])
    if not isinstance(raw, dict) or set(raw) != set(fields):
        _refuse("Remote-only launch requires the complete fixed profile")
    normalized = dict(raw)
    for key in ("generation_roles", "tool_capabilities"):
        if not isinstance(raw[key], (list, tuple)):
            _refuse("Invalid remote-only role/capability metadata")
        normalized[key] = tuple(raw[key])
    for expected in candidates:
        if _matches_contract(normalized, expected):
            return expected
    _refuse("Remote-only profile differs from the approved fixed contract")


def _matches_contract(normalized: dict, expected: RemoteOnlyProfile) -> bool:
    for key, value in asdict(expected).items():
        observed = normalized[key]
        if (key == "readiness_timeout" and type(observed) in {int, float}):
            good = observed == value
        else:
            good = type(observed) is type(value) and observed == value
        if not good:
            return False
    return True


def remote_full_mode() -> bool:
    """True only in the validated full remote build (WISP_REMOTE_FULL=1)."""
    return remote_only_profile() is not None and _full_requested()


def remote_read_only_mode() -> bool:
    """True in the validated read-only remote build (WISP_REMOTE_ONLY=1 alone)."""
    return remote_only_profile() is not None and not _full_requested()


def require_generation_role(role: str) -> None:
    profile = remote_only_profile()
    if profile is not None and (not isinstance(role, str) or role not in profile.generation_roles):
        _refuse("This role is unavailable in the remote-only demo")


def remote_default_target(role: str = "fast") -> Target:
    profile = remote_only_profile()
    if profile is None:
        _refuse("Remote-only profile is not enabled")
    require_generation_role(role)
    from service.config.endpoints import Target, endpoint_from_config
    ep = endpoint_from_config(profile.endpoint_name, {
        "base_url": profile.base_url, "credential_ref": profile.credential_ref,
        "managed": False, "provider": profile.provider, "api_prefix": profile.api_prefix,
        "readiness_timeout": profile.readiness_timeout,
    })
    label = "intel-gx10-full-v5" if remote_full_mode() else "intel-gx10-hours-demo-v1"
    return Target(role, ep, profile.model_id, profile=label,
                  context_window=profile.context_window, capabilities=profile.tool_capabilities)


def validate_remote_target(target: Target) -> None:
    if remote_only_profile() is None:
        return
    from service.config.endpoints import Target
    if not isinstance(target, Target):
        _refuse("Remote-only inference requires an explicit bound target")
    require_generation_role(target.role)
    if target != remote_default_target(target.role):
        _refuse("Remote-only inference refuses alternate targets or credentials")


def require_local_models_disabled(operation: str) -> None:
    if remote_only_profile() is not None:
        _refuse(f"{operation} is unavailable in the remote-only demo")


def remote_tool_allowed(tool_name: str | None) -> bool:
    """Whether a remote-only build may offer/run this tool at all.

    Full mode: every built-in except _REMOTE_FULL_EXCLUDED; the normal safety
    policy then decides allow/confirm/deny for each call. Read-only mode: the
    fixed read allowlist (remote_read_tool_allowed)."""
    if not isinstance(tool_name, str) or not tool_name:
        return False
    if remote_full_mode():
        if tool_name in _REMOTE_FULL_EXCLUDED:
            return False
        # Only tools registered by Wisp itself; skill and MCP runtimes never
        # start in a remote-only build, but refuse their names regardless.
        from service.tools.registry import REGISTRY
        tool = REGISTRY.get(tool_name)
        return (tool is not None and tool.category != "skill_tool"
                and not tool_name.startswith("mcp_"))
    return remote_read_tool_allowed(tool_name)


def remote_read_tool_allowed(tool_name: str | None) -> bool:
    """The read-only allowlist. Unchanged by full mode."""
    if not isinstance(tool_name, str):
        return False
    if tool_name in _READ_TOOLS:
        return True
    # Behavior-learning tools read (or, for correct_pattern, edit) only Wisp's
    # own learned profile — never a native source. Allowed only while
    # behavior learning is on, so the pinned read set (_READ_TOOLS) is
    # unchanged. Their schemas are small; the routes offer them alone (see
    # router._behavior_request).
    if tool_name in _BEHAVIOR_TOOLS:
        # The same import-free switch behavior_runtime.enabled() uses; the
        # assistant package (which opens stores) is never imported from here.
        from service.behavior.flags import env_enabled
        return env_enabled()
    return False
