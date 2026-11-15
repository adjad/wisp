"""The Study connector: a cloud model reached with a key used for Study only.

Two providers, two exact origins (docs/STUDY_TAB.md, Providers). The user's
choice of provider and model lives in MOE_DIR/study/settings.json (0600); the
key lives only in the macOS Keychain (generic password, service
``com.wisp.study``, account = provider id). The key is never logged, never
returned, never written to a file and never put in an exception message.

On a Mac with no local engine (the Intel build, already all-cloud) Study falls
back to the existing cloud connection when no Study key is set, but only when
that connection points at one of the two Study origins.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

import httpx

from service.config import keychain_legacy

log = logging.getLogger(__name__)

SERVICE = "com.wisp.study"

PROVIDERS: dict[str, dict] = {
    "openrouter": {
        "id": "openrouter", "label": "OpenRouter",
        "origin": "https://openrouter.ai", "prefix": "/api/v1",
        "default_model": "google/gemini-3.8-flash",
        "models": [{"id": "google/gemini-3.8-flash", "label": "Gemini 3.8 Flash"}],
        "key_url": "https://openrouter.ai/keys", "key_hint": "sk-or-…",
    },
    "gemini": {
        "id": "gemini", "label": "Gemini API",
        "origin": "https://generativelanguage.googleapis.com", "prefix": "/v1beta/openai",
        "default_model": "gemini-3.8-flash",
        "models": [{"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash"}],
        "key_url": "https://aistudio.google.com/apikey", "key_hint": "AIza…",
    },
}
DEFAULT_PROVIDER = "openrouter"
# The only URLs this module ever posts to.
ALLOWED_URLS = frozenset(p["origin"] + p["prefix"] + "/chat/completions" for p in PROVIDERS.values())
_ORIGIN_PREFIX = {p["origin"]: p["prefix"] for p in PROVIDERS.values()}

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")
TIMEOUT_S = 90.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
KEY_CACHE_S = 60.0
EXISTS_CACHE_S = 30.0
# After a Keychain read that timed out or was refused (macOS asked and nobody answered,
# or Deny), no new read for this long: a pending prompt is never re-triggered per request.
KEY_FAILURE_HOLD_S = 60.0
# errSecItemNotFound: no key saved (a plain "missing", never held).
_NOT_FOUND = 44

MESSAGES = {
    "missing": "Add a Study key to make study material.",
    "rejected": "The Study key was not accepted. Check it in Study settings.",
    "busy": "The Study model is busy right now. Try again in a minute.",
    "unreachable": "Couldn't reach the Study model. Check your connection and try again.",
    "bad_reply": "The Study model sent something Wisp couldn't use. Try again.",
}


KEYCHAIN_WAIT_MESSAGE = ("Wisp waited for you to allow Keychain access to your Study key, but no "
                         "answer came in time. Try again, then enter your login password and choose "
                         "Always Allow when macOS asks.")
KEYCHAIN_MESSAGE = ("Wisp could not read the Study key from your Keychain. If macOS asked, "
                    "choose Always Allow. Otherwise reconnect your key: choose Study key in "
                    "Study, paste the key and save it.")


class StudyConnectorError(Exception):
    """A gentle, key-free failure. ``code`` is one of MESSAGES' keys."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code if code in MESSAGES else "bad_reply"
        self.message = message or MESSAGES[self.code]
        super().__init__(self.message)


# --- settings -----------------------------------------------------------------------

def _settings_path() -> Path:
    # Resolved at call time so a test's (or the sandbox's) WISP_HOME is honoured. Reading
    # it never creates anything; only set_choice writes it.
    from service.study.store import study_dir
    return study_dir() / "settings.json"


def _valid_model(model_id) -> bool:
    return isinstance(model_id, str) and bool(_MODEL_ID.fullmatch(model_id))


def choice() -> tuple[str, str]:
    """(provider, model_id): the saved choice, or OpenRouter and its default."""
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing or unreadable means the default
        data = {}
    if not isinstance(data, dict):
        data = {}
    provider = data.get("provider")
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    model_id = data.get("model_id")
    if not _valid_model(model_id):
        model_id = PROVIDERS[provider]["default_model"]
    return provider, model_id


def set_choice(provider: str, model_id: str) -> dict:
    if provider not in PROVIDERS:
        raise ValueError("Unknown Study provider")
    if model_id is None or model_id == "":
        model_id = PROVIDERS[provider]["default_model"]
    if not _valid_model(model_id):
        raise ValueError("Invalid Study model id")
    from service.paths import private_write_bytes
    data = json.dumps({"provider": provider, "model_id": model_id}).encode("utf-8")
    private_write_bytes(_settings_path(), data)
    forget_key()
    return status()


# --- the key ------------------------------------------------------------------------

_key_cache: dict[str, tuple[str, float]] = {}
_exists_cache: dict[str, tuple[bool, float]] = {}
_key_failed: dict[str, float] = {}     # provider -> when a read timed out or was refused


def forget_key(provider: str | None = None) -> None:
    """Drop cached keys, existence checks and held failures (all, or one provider's).
    Saving a key goes through set_choice, which calls this, so a new key is read at once."""
    if provider is None:
        _key_cache.clear()
        _exists_cache.clear()
        _key_failed.clear()
        _key_waited.clear()
    else:
        _key_cache.pop(provider, None)
        _exists_cache.pop(provider, None)
        _key_failed.pop(provider, None)
        _key_waited.pop(provider, None)


def _read_held(provider: str) -> bool:
    failed = _key_failed.get(provider)
    return failed is not None and time.monotonic() - failed < KEY_FAILURE_HOLD_S


def _security(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/usr/bin/security", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, check=False,
    )


def _clean_key(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if (not value or len(value) > 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        return None
    return value


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


# Providers whose last read waited on macOS's question with no answer (not a refusal).
_key_waited: dict[str, float] = {}


def _keychain_key(provider: str) -> str | None:
    """The saved Study key, or None. BLOCKING (it may wait on a Keychain prompt for up to
    10 s, or a minute while macOS asks to copy a key saved before the app's rename):
    async callers run it through asyncio.to_thread (see _resolve_async). A read that
    timed out or was refused is held for KEY_FAILURE_HOLD_S."""
    if provider not in PROVIDERS:
        return None
    hit = _key_cache.get(provider)
    if hit and time.monotonic() - hit[1] < KEY_CACHE_S:
        return hit[0]
    if _read_held(provider):
        return None
    try:
        result = _security(["find-generic-password", "-s", SERVICE, "-a", provider, "-w"])
        if result.returncode == _NOT_FOUND:
            # Saved before the app's rename? Copy it across once (macOS asks first, so
            # never on the event loop; the wait is long enough to type a password).
            value = None
            if keychain_legacy.copying() and not _on_loop():
                value = _clean_key(keychain_legacy.copy_forward("study", SERVICE, provider))
        elif result.returncode or result.stderr:
            _key_failed[provider] = time.monotonic()
            return None
        else:
            value = _clean_key(result.stdout.decode("ascii").rstrip("\n"))
    except keychain_legacy.LegacyTimedOut:
        _key_waited[provider] = time.monotonic()   # unanswered, not refused: no hold
        return None
    except (subprocess.TimeoutExpired, keychain_legacy.LegacyUnreadable):
        _key_failed[provider] = time.monotonic()
        return None
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError):
        return None
    _key_waited.pop(provider, None)
    if value is None:
        return None
    _key_cache[provider] = (value, time.monotonic())
    _exists_cache[provider] = (True, time.monotonic())
    return value


def _key_exists(provider: str) -> bool:
    """Whether a Study key is saved, without reading the secret (no -w, so no
    Keychain prompt). Cached briefly so status() stays cheap."""
    if provider not in PROVIDERS:
        return False
    hit = _exists_cache.get(provider)
    if hit and time.monotonic() - hit[1] < EXISTS_CACHE_S:
        return hit[0]
    if provider in _key_cache:
        found = True
    else:
        try:
            found = _security(["find-generic-password", "-s", SERVICE, "-a", provider]).returncode == 0
        except (OSError, subprocess.SubprocessError):
            found = False
        # A key saved before the app's rename counts: its first use copies it across.
        found = found or keychain_legacy.legacy_exists("study", SERVICE, provider)
    _exists_cache[provider] = (found, time.monotonic())
    return found


# --- the Intel cloud fallback ---------------------------------------------------------

def _cloud_target() -> dict | None:
    """The existing cloud connection, only on a remote-only Mac and only when it
    points at a Study origin: {"origin", "prefix", "model", "key": callable}.
    Any problem (these modules differ between branches) means no fallback."""
    try:
        from service.inference.remote_policy import remote_only_profile
        profile = remote_only_profile()
        if profile is None:
            return None
    except Exception:  # noqa: BLE001
        return None
    try:
        from service.inference.remote_policy import remote_default_target
        target = remote_default_target(_generation_role(profile))
        ep = target.endpoint
        origin, prefix, model = str(ep.base_url).rstrip("/"), str(ep.api_prefix), str(target.model)
        # Endpoint.api_key resolves "keychain:<name>" through provider_credentials.resolve_keychain
        # (its own hold and lock on the Intel branch). It may block: callers run it in a thread.
        key = ep.api_key
    except Exception:  # noqa: BLE001
        try:
            from service.config import cloud_provider_settings
            from service.config.endpoints import endpoint
            cfg = cloud_provider_settings()
            ep = endpoint("cloud")
            origin = str(cfg.get("base_url") or ep.base_url).rstrip("/")
            prefix = str(cfg.get("api_prefix") or ep.api_prefix)
            model = str(cfg.get("model_id") or "")
            key = ep.api_key
        except Exception:  # noqa: BLE001
            return None
    if _ORIGIN_PREFIX.get(origin) != prefix or not _valid_model(model):
        return None
    return {"origin": origin, "prefix": prefix, "model": model, "key": key}


def _generation_role(profile) -> str:
    """The remote profile's role for one-shot text generation: "general" (present in
    RemoteOnlyProfile.generation_roles on main and on the Intel branch), else its first role."""
    roles = tuple(getattr(profile, "generation_roles", ()) or ())
    if not roles or "general" in roles:
        return "general"
    return str(roles[0])


_cloud_cache: tuple[str | None, float] | None = None


def _cloud_model() -> str | None:
    """The fallback's model id, or None when there is no usable fallback (cached briefly)."""
    global _cloud_cache
    if _cloud_cache and time.monotonic() - _cloud_cache[1] < EXISTS_CACHE_S:
        return _cloud_cache[0]
    try:
        target = _cloud_target()
        model = target["model"] if target is not None else None
    except Exception:  # noqa: BLE001
        model = None
    _cloud_cache = (model, time.monotonic())
    return model


def _cloud_available() -> bool:
    return _cloud_model() is not None


# --- status -------------------------------------------------------------------------

def providers() -> list[dict]:
    keys = ("id", "label", "default_model", "models", "key_url", "key_hint")
    return [{k: (list(map(dict, p[k])) if k == "models" else p[k]) for k in keys}
            for p in PROVIDERS.values()]


def status() -> dict:
    """The contract's "connector" object. Never contains or reads the key."""
    provider, model_id = choice()
    active = None
    if _key_exists(provider):
        source, active = "study", model_id
    elif _cloud_available():
        source, active = "cloud", _cloud_model()
    else:
        source = "none"
    # active_model_id: the model a call would use right now (the cloud profile's on the
    # Intel fallback), or None when Study is not ready. model_id stays the Study choice.
    # long_context: that model is a Gemini model, so imports read more pages and generation
    # may use a whole course (docs/STUDY_TAB.md, Long context).
    return {"provider": provider, "model_id": model_id, "ready": source != "none",
            "source": source, "active_model_id": active, "long_context": long_context(active),
            "providers": providers()}


def long_context(model_id: str | None) -> bool:
    """Whether a model id is a Gemini model: bare ``gemini-…`` (the Gemini API) or
    ``google/gemini-…`` (OpenRouter). Anything else keeps the ordinary limits."""
    return isinstance(model_id, str) and (model_id.startswith("gemini-")
                                          or model_id.startswith("google/gemini-"))


async def status_async() -> dict:
    """status() for async routes: its Keychain existence check and the cloud profile read
    may block, so they run on a worker thread, never on the event loop."""
    return await asyncio.to_thread(status)


# --- cost bounds ----------------------------------------------------------------------

class PostBudget:
    """How many more POSTs the current request may send (shared across retries)."""

    def __init__(self, posts: int):
        self.posts = int(posts)


_BUDGET: contextvars.ContextVar[PostBudget | None] = contextvars.ContextVar("study_post_budget", default=None)


@contextlib.contextmanager
def post_budget(posts: int):
    """Cap the POSTs of everything inside (complete_json's retries included). A budget
    already in force is kept (the outer, request-wide one wins)."""
    if _BUDGET.get() is not None:
        yield _BUDGET.get()
        return
    budget = PostBudget(posts)
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


def posts_left() -> float:
    budget = _BUDGET.get()
    return float("inf") if budget is None else budget.posts


def _take_post() -> bool:
    budget = _BUDGET.get()
    if budget is None:
        return True
    if budget.posts <= 0:
        return False
    budget.posts -= 1
    return True


# --- completion ---------------------------------------------------------------------

def _resolve(key_reader: Callable[[str], str] | None) -> tuple[str, str, str, str]:
    """(url, model, key, provider) for one call; raises StudyConnectorError("missing")."""
    provider, model_id = choice()
    spec = PROVIDERS[provider]
    if key_reader is not None:
        try:
            key = _clean_key(key_reader(provider))
        except Exception:  # noqa: BLE001 — a reader's own error may quote the key
            key = None
        if key is None:
            raise StudyConnectorError("missing")
        return spec["origin"] + spec["prefix"] + "/chat/completions", model_id, key, provider
    key = _keychain_key(provider)
    if key is not None:
        return spec["origin"] + spec["prefix"] + "/chat/completions", model_id, key, provider
    unreadable = _read_held(provider)   # saved, but macOS did not hand it over
    waited = _key_waited.pop(provider, None) is not None
    try:
        cloud = _cloud_target()
    except Exception:  # noqa: BLE001
        cloud = None
    cloud_problem = None
    if cloud is not None:
        try:
            key = _clean_key(cloud["key"]())
        except Exception as exc:  # noqa: BLE001
            key = None
            from service.errors import credential_message
            cloud_problem = credential_message(exc)   # never the key's text
        if key is not None:
            cloud_provider = next(p for p, s in PROVIDERS.items() if s["origin"] == cloud["origin"])
            return cloud["origin"] + cloud["prefix"] + "/chat/completions", cloud["model"], key, cloud_provider
    raise StudyConnectorError("missing", KEYCHAIN_WAIT_MESSAGE if waited
                              else KEYCHAIN_MESSAGE if unreadable else cloud_problem)


def _content(data) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content
                          if isinstance(part, dict) and isinstance(part.get("text"), str))
    return content if isinstance(content, str) else ""


_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def parse_json_object(text: str) -> dict | None:
    """The JSON object in a model reply: bare, in a ```json fence, or wrapped in
    prose (the outermost {...})."""
    if not isinstance(text, str):
        return None
    candidates = [text.strip()]
    candidates += [m.group(1).strip() for m in _FENCE.finditer(text)]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _field_rejected(body: str, payload: dict) -> list[str]:
    """Which optional fields a 400 complains about (both, when it names neither)."""
    optional = [f for f in ("reasoning_effort", "response_format") if f in payload]
    lowered = body.lower()
    named = [f for f in optional
             if f in lowered or (f == "reasoning_effort" and "reasoning" in lowered)
             or (f == "response_format" and ("json_object" in lowered or "response format" in lowered
                                             or "json_schema" in lowered or "schema" in lowered))]
    return named or optional


def response_format(model: str, schema: dict | None, name: str = "study") -> dict:
    """Gemini's structured output (a JSON schema it must follow) when the model is a Gemini
    model and a schema is given; plain JSON mode otherwise."""
    if schema and long_context(model):
        return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}}
    return {"type": "json_object"}


async def complete_json(system: str, user: str, *, max_tokens: int = 6000,
                        key_reader: Callable[[str], str] | None = None,
                        transport: httpx.AsyncBaseTransport | None = None,
                        schema: dict | None = None, schema_name: str = "study") -> dict:
    """One non-streaming JSON completion from the Study model; returns the object. With a
    schema and a Gemini model the reply must follow that schema (structured output); a 400
    over it falls back to JSON mode, then to no response_format, and the caller's
    validation still checks every reply."""
    # Every Keychain read (the Study key, or the cloud profile's) may wait on a macOS prompt:
    # never on the event loop.
    url, model, key, provider = await asyncio.to_thread(_resolve, key_reader)
    if url not in ALLOWED_URLS:
        raise StudyConnectorError("unreachable", "Study only talks to its two providers.")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "Accept": "application/json"}
    if provider == "openrouter":
        headers["X-Title"] = "Wisp Study"
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": int(max_tokens),
        "temperature": 0.4,
        "reasoning_effort": "low",
        "response_format": response_format(model, schema, schema_name),
        "stream": False,
    }
    retries = 1          # bad JSON, 5xx or a timeout
    # a 400 over reasoning_effort / response_format; one more when a schema can step down to JSON mode
    field_retry = 2 if payload["response_format"]["type"] == "json_schema" else 1
    pending = "unreachable"   # what to report when the request's POST budget runs out
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                 timeout=TIMEOUT_S, transport=transport) as client:
        while True:
            failure: str | None = None
            if not _take_post():
                raise StudyConnectorError(pending)
            try:
                response = await client.post(url, headers=headers, json=payload)
            except httpx.TimeoutException:
                failure = "unreachable"
            except httpx.HTTPError:
                failure = "unreachable"
            if failure is None:
                status_code = response.status_code
                if status_code in (401, 403):
                    forget_key(provider)
                    raise StudyConnectorError("rejected")
                if status_code == 429:
                    raise StudyConnectorError("busy")
                if status_code == 400 and field_retry:
                    dropped = _field_rejected(response.text[:4000], payload)
                    if dropped:
                        field_retry -= 1
                        for field in dropped:
                            if field == "response_format" and payload[field]["type"] == "json_schema":
                                payload[field] = {"type": "json_object"}   # schema refused: JSON mode
                            else:
                                payload.pop(field, None)
                        log.info("Study provider refused %s; retrying without it", ", ".join(dropped))
                        pending = "bad_reply"
                        continue
                if status_code >= 500:
                    failure = "unreachable"
                elif status_code != 200:
                    log.warning("Study model call failed with HTTP %s", status_code)
                    raise StudyConnectorError("bad_reply")
                elif len(response.content) > MAX_RESPONSE_BYTES:
                    raise StudyConnectorError("bad_reply")
                else:
                    try:
                        data = response.json()
                    except ValueError:
                        data = None
                    parsed = parse_json_object(_content(data))
                    if parsed is not None:
                        return parsed
                    failure = "bad_reply"
            if retries and posts_left() > 0:
                retries -= 1
                pending = failure
                log.info("Study model call: %s, retrying once", failure)
                continue
            raise StudyConnectorError(failure)
