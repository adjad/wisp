"""Configuration loading for the MOE service."""
from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import yaml

from service.paths import STATE_DIR

OMLX_SETTINGS = Path.home() / ".omlx" / "settings.json"
# oMLX's own per-model settings (temperature, sampling, etc.) — each entry
# also carries the "is_favorite" flag the user sets by starring a model in
# oMLX's UI. Reading this file directly (rather than oMLX's live /v1/models
# HTTP endpoint) means the favorites list is available even when the oMLX
# server subprocess isn't currently running — only the menu-bar app has to be
# up, which it always is once anything's been configured.
OMLX_MODEL_SETTINGS = Path.home() / ".omlx" / "model_settings.json"
MODELS_YAML = Path(__file__).parent / "models.yaml"
# User overlay — persisted settings live OUTSIDE the app bundle so a repackage
# (which overwrites the bundled models.yaml) can't wipe them. models.yaml holds
# code defaults; this holds the user's changes, deep-merged on top at read time.
USER_CONFIG = STATE_DIR / "config.yaml"

DEFAULT_AIR_COMPUTE = {
    "enabled": False,
    # No default hostname: the Air node is whatever the user's other Mac is
    # called, so there is nothing sensible to guess. Empty until they set it in
    # Settings (or ~/.moe/config.yaml), and the feature stays off until then.
    "base_url": "",
    "timeout_ms": 2500,
    "capabilities": {
        "routing": True,
        "summaries": False,
        "draft": False,
    },
}

_AIR_STATUS = {
    "state": "not_checked",
    "message": "",
    "latency_ms": None,
    "checked_at": None,
}

# gpt-oss's reasoning_effort, applied to every completion (general chat,
# reasoning, and the agent loop — see main.py/agent/loop.py). Runtime-adjustable
# via GET/POST /thinking_level, in-memory only (matches read_only/full_access/
# idle_minutes — a restart reverts to "auto"). Also the knob for the
# reasoning-starves-content bug: "low" bounds how much of the token budget
# chain-of-thought can eat before the actual answer starts.
#
# "auto" (the default) doesn't send one fixed level for every request — gpt-oss
# can't dynamically choose its own effort mid-generation (reasoning_effort is a
# decode-time parameter set BEFORE the call starts), so "auto" means the ROUTER
# decides per-request from what already classified the prompt: see
# resolve_reasoning_effort() below. Picking "low"/"medium"/"high" explicitly
# overrides that and pins every request to one level, same as before.
_THINKING_LEVELS = ("auto", "low", "medium", "high")
_thinking_level = "auto"


@lru_cache
def omlx_settings() -> dict:
    """oMLX's own settings.json — single source of truth for host/port/api_key."""
    with OMLX_SETTINGS.open() as f:
        return json.load(f)


@lru_cache
def _packaged_config() -> dict:
    """Code defaults shipped in the bundle. Never written to at runtime."""
    with MODELS_YAML.open() as f:
        return yaml.safe_load(f) or {}


@lru_cache
def _user_overlay() -> dict:
    """Persisted user changes from ~/.moe/config.yaml (may be absent/empty)."""
    if not USER_CONFIG.exists():
        return {}
    try:
        with USER_CONFIG.open() as f:
            return yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001 — a corrupt overlay must not break startup
        return {}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@lru_cache
def models_config() -> dict:
    """Effective config = packaged defaults with the user overlay merged on top."""
    return _deep_merge(_packaged_config(), _user_overlay())


def _save_overlay(update: dict) -> None:
    """Deep-merge `update` into ~/.moe/config.yaml and invalidate caches."""
    USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    current: dict = {}
    if USER_CONFIG.exists():
        try:
            with USER_CONFIG.open() as f:
                current = yaml.safe_load(f) or {}
        except Exception:  # noqa: BLE001
            current = {}
    merged = _deep_merge(current, update)
    with USER_CONFIG.open("w") as f:
        yaml.safe_dump(merged, f, sort_keys=False)
    _user_overlay.cache_clear()
    models_config.cache_clear()


def get_daily_summary_hour() -> int:
    """Hour (0-23) the scheduled daily brief fires. 8 = 8am (AM), 20 = 8pm (PM).
    Persisted in the user overlay so it survives repackaging."""
    return int(models_config().get("daily_summary_hour", 8))


def set_daily_summary_hour(hour: int) -> int:
    hour = 20 if int(hour) >= 12 else 8   # only two choices: 8am or 8pm
    _save_overlay({"daily_summary_hour": hour})
    return hour


def _merged_air_compute(cfg: dict) -> dict:
    raw = cfg.get("air_compute") or {}
    caps = {
        **DEFAULT_AIR_COMPUTE["capabilities"],
        **(raw.get("capabilities") or {}),
    }
    return {
        "enabled": bool(raw.get("enabled", DEFAULT_AIR_COMPUTE["enabled"])),
        "base_url": str(raw.get("base_url", DEFAULT_AIR_COMPUTE["base_url"])).rstrip("/"),
        "timeout_ms": int(raw.get("timeout_ms", DEFAULT_AIR_COMPUTE["timeout_ms"])),
        "capabilities": {
            "routing": bool(caps.get("routing", True)),
            "summaries": bool(caps.get("summaries", False)),
            "draft": bool(caps.get("draft", False)),
        },
    }


def air_compute_config() -> dict:
    return _merged_air_compute(models_config())


def air_compute_status() -> dict:
    return dict(_AIR_STATUS)


def set_air_compute_status(state: str, *, message: str = "",
                           latency_ms: int | None = None) -> None:
    import time

    _AIR_STATUS.update({
        "state": state,
        "message": message,
        "latency_ms": latency_ms,
        "checked_at": time.time(),
    })


def omlx_base_url() -> str:
    cfg = models_config()["omlx"].get("base_url")
    if cfg:
        return cfg.rstrip("/")
    s = omlx_settings()["server"]
    return f"http://{s['host']}:{s['port']}"


def omlx_api_key() -> str:
    return omlx_settings()["auth"]["api_key"]


def get_thinking_level() -> str:
    return _thinking_level


def set_thinking_level(level: str) -> str:
    global _thinking_level
    if level in _THINKING_LEVELS:
        _thinking_level = level
    return _thinking_level


# Per-role effort when "auto" is active. Mirrors tuning already established
# elsewhere rather than inventing new cutoffs:
#   - "agent" (the tool-calling loop) explicitly wants LOW: this is also the
#     documented fix for the "reasoning eats the token budget before the tool
#     call happens" failure mode (see the module comment above and loop.py),
#     and every extra step of a multi-step tool loop repeats this cost.
#   - "fast" (trivial chit-chat — greetings, thanks, acks) needs no reasoning
#     at all.
#   - "reasoning" is deliberately NOT bumped to "high" here even though it
#     only gets reached via router.py's HARD_REASON_RE gate (genuinely hard
#     math/proof/complexity prompts): this app already measured "medium" as
#     equally rigorous as a retired high-effort specialist on exactly these
#     hard prompts, just 5-8x faster (see main.py's reasoning branch) — auto
#     escalating to "high" would silently throw that away for no proven
#     accuracy gain. It falls through to the "medium" default below instead.
#   - everything else (general chat, coding, and reasoning per above) gets
#     "medium" — the same level this whole app defaulted to globally before
#     "auto" existed.
_AUTO_EFFORT_BY_ROLE = {
    "agent": "low",
    "fast": "low",
}


def resolve_reasoning_effort(role: str) -> str:
    """The reasoning_effort to actually send for this request. Returns the
    fixed level as-is unless the user has "auto" selected, in which case the
    ROUTER's classification of this request (`role`) picks it per-request —
    see _AUTO_EFFORT_BY_ROLE."""
    if _thinking_level != "auto":
        return _thinking_level
    return _AUTO_EFFORT_BY_ROLE.get(role, "medium")


# "Super Model" — a single model the user explicitly forces every request onto
# for the hardest work (agentic coding, tough reasoning). ONLY engaged by
# direct user action (a Wisp button that also quits other apps to free RAM for
# it) — the router never picks this on its own; see main.py's `runner()`,
# which applies this override as the LAST step, after all normal routing/
# sticky-pin logic, so nothing else can silently unpick it mid-session.
#
# The MODEL CHOICE persists across restarts (user overlay) since it's actively
# being tuned by hand and re-picking it every launch would be annoying.
# Whether it's currently ACTIVE is in-memory only, like thinking_level — a
# restart should never silently leave the app pinned in "quit everything and
# force one model" mode without a fresh, explicit toggle.
_super_model_active = False


DEFAULT_SUPER_MODEL = "Qwen3.6-27B-MTP-4bit-MLX"


def get_super_model_name() -> str:
    """The model Super Model uses when active — defaults to
    DEFAULT_SUPER_MODEL until the user picks something else in Settings."""
    return str(models_config().get("super_model") or DEFAULT_SUPER_MODEL)


def set_super_model_name(model: str) -> str:
    _save_overlay({"super_model": model})
    set_model_context_window(model, 32000)
    return get_super_model_name()


def set_model_context_window(model: str, tokens: int) -> bool:
    """Patch ONE model's max_context_window in oMLX's own model_settings.json
    — the per-model override oMLX's admin UI calls "Ctx Window", which wins
    over both the model's native declared length and the global fallback (see
    oMLX's get_max_context_window() resolution order). There's no per-REQUEST
    equivalent in oMLX's chat-completions API; context length is resolved at
    model-load time, so this only takes effect the next time `model` is
    (re)loaded — an already-resident instance keeps whatever it loaded with.

    Deliberately conservative: only ever touches the single
    models.<model>.max_context_window key, leaves every other model and every
    other setting (temperature, sampling, etc.) byte-for-byte untouched, backs
    up the original first (oMLX's own convention — see its .bak-<timestamp>
    files), and no-ops (rather than fabricating a new entry) if oMLX has never
    configured this model — writing a well-formed entry from scratch risks
    getting the schema wrong. Best-effort: never raises, since this must never
    break setting which model Super Model uses.
    """
    if not OMLX_MODEL_SETTINGS.exists():
        return False
    try:
        with OMLX_MODEL_SETTINGS.open() as f:
            raw = f.read()
        data = json.loads(raw)
        models = data.get("models")
        if not isinstance(models, dict) or model not in models:
            return False  # oMLX has never touched this model — don't fabricate an entry
        if not isinstance(models[model], dict):
            return False
        if models[model].get("max_context_window") == tokens:
            return True  # already set, nothing to do

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = OMLX_MODEL_SETTINGS.with_name(f"{OMLX_MODEL_SETTINGS.name}.bak-{timestamp}")
        backup.write_text(raw)

        models[model]["max_context_window"] = tokens
        tmp = OMLX_MODEL_SETTINGS.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(OMLX_MODEL_SETTINGS)
        return True
    except Exception:  # noqa: BLE001
        return False


def is_super_model_active() -> bool:
    return _super_model_active


def set_super_model_active(active: bool) -> bool:
    global _super_model_active
    _super_model_active = bool(active)
    return _super_model_active


def role_to_model(role: str) -> str:
    """Resolve a logical role (e.g. 'coding') to a concrete oMLX model id."""
    roles = models_config()["roles"]
    model = roles.get(role)
    if not model:
        model = roles[models_config()["default_role"]]
    return model


def tool_capable_models() -> list[str]:
    return models_config().get("tool_capable", [])


def is_tool_capable(model: str) -> bool:
    return model in tool_capable_models()


def roster_problems(installed: list[str] | None = None) -> list[str]:
    """Config problems worth telling the user about at startup.

    The roster in models.yaml names specific oMLX model ids, and `tool_capable`
    is an explicit allowlist. On the machine this was developed against those
    line up; on a fresh install with a different set of models pulled, they may
    not — and the failure was silent and baffling. If the `agent` role resolves
    to a model that isn't in `tool_capable`, the entire agent loop (every tool,
    every action) quietly stops working while chat carries on fine.

    Returns human-readable strings, empty when everything checks out. Purely
    diagnostic: nothing here changes behaviour or blocks startup.
    """
    problems: list[str] = []
    cfg = models_config()
    roles: dict[str, str] = cfg.get("roles", {}) or {}
    capable = tool_capable_models()

    agent = roles.get("agent")
    if agent and capable and agent not in capable:
        problems.append(
            f"the 'agent' role resolves to {agent!r}, which is not in "
            f"tool_capable ({', '.join(capable)}). Tool calling — and so every "
            "action Wisp can take — will not work. Fix the roster or add the "
            "model to tool_capable in service/config/models.yaml."
        )

    if installed is not None:
        have = set(installed)
        missing = sorted({m for m in roles.values() if m and m not in have})
        if missing:
            problems.append(
                "these roster models are not installed in oMLX: "
                + ", ".join(missing)
                + ". Pull them, or repoint the roles in Settings."
            )

    return problems


def agent_model() -> str:
    """The tool-driving model used for the agent loop."""
    return role_to_model("agent")


def favorite_models() -> list[str]:
    """Models starred as favorites in oMLX — read straight from its own
    settings file, not the live server, so this works even when the oMLX
    server subprocess is down. Never raises: a missing/corrupt file just
    means no favorites, not a broken request."""
    if not OMLX_MODEL_SETTINGS.exists():
        return []
    try:
        with OMLX_MODEL_SETTINGS.open() as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return []
    models = data.get("models", {})
    if not isinstance(models, dict):
        return []
    return sorted(name for name, cfg in models.items()
                  if isinstance(cfg, dict) and cfg.get("is_favorite"))


def set_role(role: str, model: str) -> None:
    """Point a role at a different installed model, persisted to the user overlay."""
    roles = {role: model}
    if role == "fast":            # router shares the fast model
        roles["router"] = model
    if role == "general":         # general drives the agent loop
        roles["agent"] = model
    _save_overlay({"roles": roles})


def set_air_compute(update: dict) -> dict:
    """Persist MacBook Air side-compute settings to the user overlay."""
    current = air_compute_config()  # effective (packaged + overlay) starting point

    if "enabled" in update:
        current["enabled"] = bool(update["enabled"])
    if "base_url" in update:
        base_url = str(update["base_url"]).strip().rstrip("/")
        if base_url:
            current["base_url"] = base_url
    if "timeout_ms" in update:
        current["timeout_ms"] = max(100, int(update["timeout_ms"]))

    incoming_caps = update.get("capabilities") or {}
    caps = dict(current["capabilities"])
    for name in ("routing", "summaries", "draft"):
        if name in incoming_caps:
            caps[name] = bool(incoming_caps[name])
    current["capabilities"] = caps

    _save_overlay({"air_compute": current})
    return current
