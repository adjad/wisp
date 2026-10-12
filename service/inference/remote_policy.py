"""Process-pinned, opt-in remote-inference policy; never resolves credentials.

Remote inference sends every generation role to ONE user-configured
OpenAI-compatible server while data, tools and approvals stay on the Mac. It is
off unless the process is launched with ``WISP_REMOTE_ONLY=1`` AND the user's
config (``~/.moe/config.yaml``, or ``$WISP_HOME/config.yaml``) carries a
complete ``inference.remote_only`` profile. Wisp ships no endpoint of its own:
a profile alone never moves inference off the Mac, and the launch flag without
a valid profile refuses to start. Settings cannot change the snapshot after
startup.

Profile (see docs/REMOTE_INFERENCE.md)::

    inference:
      remote_only:
        base_url: https://inference.example      # https origin, no path or slash
        model_id: example-model                  # sent as-is for every role
        credential_ref: keychain:example         # Keychain item, never a token
        context_window: 32000
        max_tokens: 8192
        request_bytes: 524288
        messages: 200
        aggregate_bytes: 131072
        tools: 32
        response_bytes: 4194304
        # optional: api_prefix (/v1), readiness_timeout (5, max 30), enabled (true)

Two modes share the pinned origin, model, Keychain-only credential and payload
validation:

* read-only (``WISP_REMOTE_ONLY=1`` alone): only the seven native reads and
  the behavior tools; every other tool, approval and write endpoint is
  refused, and no memory or history context is sent.
* full (``WISP_REMOTE_ONLY=1`` + ``WISP_REMOTE_FULL=1``): every built-in tool
  that does not need a local model or a Wisp-local extension runtime, each
  decided by the normal safety policy (service/safety/policy.py and
  grants.py) exactly as on a Mac with local models; sends always confirm.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import math
import os
import re
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

# Every limit the user's profile must state, with the range Wisp accepts.
LIMITS = {
    "context_window": (2048, 1_048_576),
    "max_tokens": (1, 131_072),
    "request_bytes": (4096, 16 * 1024 * 1024),
    "messages": (1, 1000),
    "aggregate_bytes": (1024, 16 * 1024 * 1024),
    "tools": (1, 128),
    "response_bytes": (1024, 64 * 1024 * 1024),
}
REQUIRED_KEYS = frozenset({"base_url", "model_id", "credential_ref", *LIMITS})
OPTIONAL_KEYS = frozenset({"enabled", "api_prefix", "readiness_timeout"})
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")
_CREDENTIAL_REF = re.compile(r"keychain:[A-Za-z][A-Za-z0-9_-]{0,63}")


def _refuse(message: str):
    from service.config.endpoints import EndpointConfigurationError
    raise EndpointConfigurationError(message)


@dataclass(frozen=True)
class RemoteOnlyProfile:
    """One validated remote-inference profile. No field has a built-in
    endpoint, model or credential: they come only from the user's config."""
    base_url: str
    model_id: str
    credential_ref: str
    context_window: int
    max_tokens: int
    request_bytes: int
    messages: int
    aggregate_bytes: int
    tools: int
    response_bytes: int
    api_prefix: str = "/v1"
    readiness_timeout: float = 5.0
    endpoint_name: str = "remote_only"
    provider: str = "openai-compatible"
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
            _refuse("Remote-only output budget exceeds the profile limit")
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


def profile_from_config(raw) -> RemoteOnlyProfile:
    """Validate a user's ``inference.remote_only`` mapping; refuse anything
    incomplete, unknown, out of range or carrying a secret."""
    if not isinstance(raw, dict):
        _refuse("Remote inference is enabled (WISP_REMOTE_ONLY=1) but no inference.remote_only "
                "profile is configured; see docs/REMOTE_INFERENCE.md")
    keys = set(raw)
    if keys - REQUIRED_KEYS - OPTIONAL_KEYS:
        _refuse("Unknown remote-inference profile fields: " + ", ".join(sorted(map(str, keys - REQUIRED_KEYS - OPTIONAL_KEYS))))
    if REQUIRED_KEYS - keys:
        _refuse("Incomplete remote-inference profile; missing: " + ", ".join(sorted(REQUIRED_KEYS - keys)))
    if raw.get("enabled", True) is not True:
        _refuse("Remote inference is launched but its profile is disabled")
    base_url = raw["base_url"]
    if not isinstance(base_url, str) or not base_url or base_url != base_url.rstrip("/"):
        _refuse("Remote-inference base_url must be an https origin without a trailing slash")
    model_id = raw["model_id"]
    if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
        _refuse("Invalid remote-inference model_id")
    credential_ref = raw["credential_ref"]
    if not isinstance(credential_ref, str) or not _CREDENTIAL_REF.fullmatch(credential_ref):
        _refuse("Remote-inference credential_ref must be keychain:<name>")
    limits = {}
    for key, (low, high) in LIMITS.items():
        value = raw[key]
        if type(value) is not int or not low <= value <= high:
            _refuse(f"Remote-inference {key} must be an integer from {low} to {high}")
        limits[key] = value
    if limits["max_tokens"] > limits["context_window"]:
        _refuse("Remote-inference max_tokens cannot exceed context_window")
    if limits["aggregate_bytes"] > limits["request_bytes"]:
        _refuse("Remote-inference aggregate_bytes cannot exceed request_bytes")
    api_prefix = raw.get("api_prefix", "/v1")
    timeout = raw.get("readiness_timeout", 5.0)
    if type(timeout) not in {int, float} or not (math.isfinite(timeout) and 0 < timeout <= 30):
        _refuse("Remote-inference readiness_timeout must be between 0 and 30 seconds")
    profile = RemoteOnlyProfile(base_url=base_url, model_id=model_id, credential_ref=credential_ref,
                                api_prefix=api_prefix, readiness_timeout=float(timeout), **limits)
    # The endpoint rules every remote target obeys: authenticated HTTPS, an
    # origin with no path, credentials or query, and a clean API prefix.
    from service.config.endpoints import endpoint_from_config
    ep = endpoint_from_config(profile.endpoint_name, {
        "base_url": profile.base_url, "credential_ref": profile.credential_ref,
        "provider": profile.provider, "api_prefix": profile.api_prefix,
        "readiness_timeout": profile.readiness_timeout,
    })
    if ep.base_url != profile.base_url:
        _refuse("Remote-inference base_url must be an https origin without a path")
    return profile


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
    _full_requested()
    # Only the launcher's WISP_REMOTE_ONLY=1 opts in. A profile left in
    # ~/.moe/config.yaml must never move a normal Mac's inference (and its
    # native reads) to a remote server on its own.
    if _FORCED_MODE is None:
        return None
    from service.config import models_config
    inference = models_config().get("inference", {})
    if not isinstance(inference, dict):
        _refuse("Invalid inference configuration")
    return profile_from_config(inference.get("remote_only"))


def remote_full_mode() -> bool:
    """True only in the validated full remote build (WISP_REMOTE_FULL=1)."""
    return remote_only_profile() is not None and _full_requested()


def remote_read_only_mode() -> bool:
    """True in the validated read-only remote build (WISP_REMOTE_ONLY=1 alone)."""
    return remote_only_profile() is not None and not _full_requested()


def require_generation_role(role: str) -> None:
    profile = remote_only_profile()
    if profile is not None and (not isinstance(role, str) or role not in profile.generation_roles):
        _refuse("This role is unavailable in remote-only inference")


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
    label = "remote-only-full" if remote_full_mode() else "remote-only-read"
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
        _refuse(f"{operation} is unavailable in remote-only inference")


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
