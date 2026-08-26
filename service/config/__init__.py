"""Configuration loading for the MOE service."""
from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import yaml

from service.paths import MOE_DIR

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
USER_CONFIG = MOE_DIR / "config.yaml"

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


def omlx_base_url() -> str:
    cfg = models_config()["omlx"].get("base_url")
    if cfg:
        return cfg.rstrip("/")
    s = omlx_settings()["server"]
    return f"http://{s['host']}:{s['port']}"


def omlx_api_key() -> str:
    return omlx_settings()["auth"]["api_key"]


# Whether the agent loop's NARRATION step still generates a think block, and
# how strictly it decides a step qualifies as one.
#
# A narration step is one where every tool the model has CALLED this turn has
# already returned a clean result, so the model's only remaining job is to
# write prose about data already in its context. That is structurally
# identical to the summary path, where no_thinking_kwargs measured 17.4s -> 2.0s
# with a CLEANER answer.
#
# Measured on an isolated narration request (Agents-A1-4B, 2026-08-08, 3 reps
# per arm — a get_upcoming result in context, warm-readout style hint):
#
#   thinking ON  (what "off" restores)  3,398 chars of reasoning, 485-char answer, 19.9s
#   thinking OFF (this)                     0 chars of reasoning, 471-char answer,  3.3s
#
# Same answer, ~6x faster on that STEP. End to end the win is smaller, because
# a turn also pays for tool selection and the tool itself — measured through the
# real router and agent loop, 4 one-tool prompts x 3 reps:
#
#   thinking ON   7.40s median per turn
#   thinking OFF  5.03s median per turn      (-32%)
#
# Quote the turn number, not the step number: the step figure is real but is not
# what a user experiences. Correctness was unchanged across the full
# scripts/test_model.py suite (21/22 both arms, same single pre-existing
# failure), and the narration step fired exactly once in 12/12 turns.
#
# The freed budget also goes back to the answer, which is the other half of why
# this exists (see agent/loop._fit_window on output being the last thing cut).
#
# THREE MODES, not a bool, because the FIRST version of this gate ("strict")
# shipped with a coverage problem worth naming explicitly rather than silently
# widening later:
#
#   "off"    — always think on a narration step. The pre-2026-08-08 behavior.
#   "strict" — skip thinking only when EVERY tool OFFERED this step has
#              answered. Measured to fire on only 2 of 15 router subsets — a
#              memory question offers remember/recall/forget and a recall only
#              ever calls `recall`; messages offers two read tools and a read
#              calls one. The model is plainly done either way, and still paid
#              for the monologue.
#   "broad"  — skip thinking whenever every tool the model has CALLED so far
#              has answered cleanly (offered-but-uncalled alternatives don't
#              block it). Extends the same win to most routes. NOT used for
#              routes whose tools are genuinely sequential/complementary rather
#              than alternatives (the aggregate to-do route, the document
#              read-then-open route, …) — see router.RouteDecision.multi_round,
#              which agent/loop.run_agent's `multi_round` param forces off
#              regardless of this setting. Needs its own A/B before shipping as
#              the default; see moe-narration-broad-mode memory once measured.
#
# tool_choice stays "auto" on a narration step in every mode except "off":
# measured, forcing "none" gave no speed or quality benefit (21.4s, and a
# SHORTER answer) while removing the model's ability to call a tool it still
# needs. Keeping "auto" means this optimization can never make a turn unable to
# finish its job.
#
# In-memory only — a restart returns to the default. The switch exists so
# this can be turned off instantly if a regression shows up in the field,
# without a repackage.
_NARRATION_MODES = ("off", "strict", "broad")
# Flipped to "broad" 2026-08-09 after its A/B: scripts/test_model.py 21/22 in
# both modes (same single pre-existing files_list flake), and on the 4
# newly-covered routes strict mode's gate literally never fires (0/42 steps
# across 20 reps) while broad fires on 19/41 and cuts total reasoning
# GENERATED by 26% (35,027 -> 25,930 chars over the same 20 turns — a
# load-independent signal, unlike wall time which is noisy on this machine
# from background contention). Median wall time 8.11s -> 6.04s (-25%),
# consistent with the reasoning-char reduction.
_narration_mode = "broad"


def narration_mode() -> str:
    """"off" | "strict" | "broad" — see _narration_mode above."""
    return _narration_mode


def set_narration_mode(mode: str) -> str:
    global _narration_mode
    if mode in _NARRATION_MODES:
        _narration_mode = mode
    return _narration_mode


def no_thinking_kwargs(model: str) -> dict:
    """`chat_template_kwargs` asking the model NOT to emit a think block.

    For the SUMMARY path only (email/message summarizers): those prompts hand
    the model the source text and ask for prose about it, so the chain of
    thought is pure latency — nothing is being figured out. Measured on
    Agents-A1-4B (2026-08-08), same inbox prompt: 17.4s / 1087 tokens / 4117
    chars of reasoning with thinking on, versus 2.0s / 110 tokens / zero
    reasoning with it off, and the summary itself came out cleaner.

    MUST NOT be used on the agent loop. Measured in the same session with
    tool_choice="required" and greedy decoding: thinking ON produced the right
    two tool calls 4/4 times, thinking OFF produced ZERO tool calls 3/3 times
    (byte-identical clarifying question each run). Suppressing the think block
    removes the very reasoning that gets the model from "user asked for X" to
    "therefore call tool Y", and greedy decoding then locks that refusal in.

    Gated per-model rather than sent unconditionally: an unknown template
    variable is only harmless while every rostered model happens to be lenient
    about it. `enable_thinking` is a Qwen-lineage template variable, which is
    why it IS the right lever for Agents-A1 (qwen3 lineage) — models whose
    template ignores it (gpt-oss took reasoning_effort instead, and is no
    longer rostered; LFM2.5's think block can't be disabled at all) get {}
    instead of dead weight on the wire. Verified the top-level (non-template)
    `enable_thinking` form is silently ignored by oMLX, so it has to travel
    inside chat_template_kwargs.
    """
    return ({"chat_template_kwargs": {"enable_thinking": False}}
            if model in no_thinking_capable() else {})


def _push_context_window_to_server(model: str, tokens: int) -> bool:
    """Tell the RUNNING oMLX server about a new context window.

    See set_model_context_window's docstring for why the file write alone is
    not enough. Synchronous urllib rather than httpx/async on purpose: every
    caller of set_model_context_window is a plain sync settings path, and this
    must not force them to become coroutines.

    Sends the model's full current settings with only max_context_window
    changed, filtered to the keys oMLX's ModelSettingsRequest actually accepts
    — an unknown key 422s the whole request, and an omitted known key gets
    nulled. The accepted-key list is read from oMLX's own /openapi.json rather
    than hardcoded, so it can't drift out of date against the running build.

    Best-effort by design: oMLX being down is the normal case at some call
    sites (Settings can be changed before the engine starts), and the file
    write still records the intent for the next startup.
    """
    import json as _json
    import urllib.error
    import urllib.request

    def _get(url: str, timeout: float = 5.0):
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {omlx_api_key()}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read())

    try:
        base = omlx_base_url()
        with OMLX_MODEL_SETTINGS.open() as f:
            current = (_json.load(f).get("models") or {}).get(model)
        if not isinstance(current, dict):
            return False

        try:
            schema = _get(f"{base}/openapi.json")
            allowed = set(schema["components"]["schemas"]
                          ["ModelSettingsRequest"]["properties"])
        except Exception:  # noqa: BLE001 — fall back to sending what we have
            allowed = set(current)

        body = {k: v for k, v in current.items() if k in allowed}
        body["max_context_window"] = tokens

        req = urllib.request.Request(
            f"{base}/admin/api/models/{model}/settings",
            data=_json.dumps(body).encode(), method="PUT",
            headers={"Authorization": f"Bearer {omlx_api_key()}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001 — oMLX may simply not be running
        return False


def set_model_context_window(model: str, tokens: int) -> bool:
    """Patch ONE model's max_context_window in oMLX's own model_settings.json
    — the per-model override oMLX's admin UI calls "Ctx Window", which wins
    over both the model's native declared length and the global fallback (see
    oMLX's get_max_context_window() resolution order).

    WRITING THE FILE IS NOT ENOUGH ON ITS OWN, and this used to be the whole
    function. Verified live 2026-08-08: raising Agents-A1-4B from 16,000 to
    24,000 in the file, then unloading AND reloading the model, still got
    `Prompt too long: 17394 tokens exceeds max context window of 16000` — the
    running oMLX server caches model_settings.json at STARTUP, so a model
    reload re-reads oMLX's in-memory copy, not the file. The old docstring
    claimed a reload was sufficient; it isn't, and the failure is silent (the
    setting looks applied everywhere you'd think to check).

    So this now does BOTH:
      1. `PUT /admin/api/models/<id>/settings`, which updates the live server.
         This is what actually takes effect. Still only at the next model LOAD
         — an already-resident instance keeps whatever it loaded with — but now
         the reload picks up the new value instead of the stale cached one.
      2. the file write, so the value survives an oMLX restart even if the
         server wasn't running when this was called.

    The PUT sends the model's FULL existing settings with only this one key
    changed, never a partial body: oMLX rewrites the whole entry from what it
    receives, so a partial PUT silently nulls everything omitted — that would
    wipe temperature/top_p and, worse, `turboquant_kv_enabled`/`_kv_bits`,
    which is what keeps KV at ~44.5KB/token instead of ~128KB. Verified: two
    keys oMLX's ModelSettingsRequest schema does NOT accept
    (`active_profile_name`, `turboquant_skip_last`) get dropped from the entry
    by the PUT regardless, so the file write in step 2 restores them.

    Best-effort throughout: never raises, since this must never break a
    role/model assignment.
    """
    if not OMLX_MODEL_SETTINGS.exists():
        return False
    try:
        # Snapshot BEFORE the PUT. oMLX rewrites the whole entry from the body
        # it receives, so any key its schema doesn't accept is gone from the
        # file afterwards — `active_profile_name` (the profile name shown in
        # oMLX's UI) and `turboquant_skip_last` (part of the KV-quantization
        # config) are both in that category. Restoring them below is only
        # possible from a copy taken first.
        with OMLX_MODEL_SETTINGS.open() as f:
            raw = f.read()
        before = json.loads(raw)
        entry_before = (before.get("models") or {}).get(model)
        if not isinstance(entry_before, dict):
            return False  # oMLX has never touched this model — don't fabricate an entry
        already = entry_before.get("max_context_window") == tokens
    except Exception:  # noqa: BLE001
        return False

    _push_context_window_to_server(model, tokens)

    try:
        # Re-read: the PUT above may have just rewritten this file.
        with OMLX_MODEL_SETTINGS.open() as f:
            data = json.load(f)
        models = data.get("models")
        if not isinstance(models, dict) or not isinstance(models.get(model), dict):
            return False
        entry = models[model]
        restored = {k: v for k, v in entry_before.items() if k not in entry}
        if already and entry.get("max_context_window") == tokens and not restored:
            return True  # already set and nothing was dropped — nothing to write

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = OMLX_MODEL_SETTINGS.with_name(f"{OMLX_MODEL_SETTINGS.name}.bak-{timestamp}")
        backup.write_text(raw)

        entry.update(restored)                      # put back what the PUT dropped
        entry["max_context_window"] = tokens        # and record the intent
        tmp = OMLX_MODEL_SETTINGS.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(OMLX_MODEL_SETTINGS)
        return True
    except Exception:  # noqa: BLE001
        return False




def role_to_model(role: str) -> str:
    """Resolve a logical role (e.g. 'coding') to a concrete oMLX model id."""
    roles = models_config()["roles"]
    model = roles.get(role)
    if not model:
        model = roles[models_config()["default_role"]]
    return model


def tool_capable_models() -> list[str]:
    return models_config().get("tool_capable", [])


def no_thinking_capable() -> list[str]:
    return models_config().get("no_thinking_capable", [])


# Fallback when oMLX has no max_context_window for a model. Deliberately small:
# guessing high silently overflows the window (the model truncates or the
# process OOMs), guessing low only costs a little conversational memory.
_DEFAULT_CONTEXT_WINDOW = 8000


def model_context_window(model: str) -> int:
    """The model's context window in tokens, as configured in oMLX.

    Read live from ~/.omlx/model_settings.json rather than cached in Wisp,
    because the user changes it in oMLX's admin UI and Wisp's budgets have to
    follow. This is the number every other context budget is derived from — see
    memory/context.history_budget.
    """
    try:
        with OMLX_MODEL_SETTINGS.open() as f:
            models = (json.load(f) or {}).get("models") or {}
        win = int((models.get(model) or {}).get("max_context_window") or 0)
        return win if win > 0 else _DEFAULT_CONTEXT_WINDOW
    except Exception:  # noqa: BLE001 — a missing/corrupt file must not break chat
        return _DEFAULT_CONTEXT_WINDOW


def is_tool_capable(model: str) -> bool:
    """Whether `model` can drive the tool loop.

    The model assigned to the `agent` role counts by definition — it IS the
    model the loop runs on. Without that clause the static roster below was a
    second place you had to remember to edit: reassigning the agent role in
    Settings left router._finalize "forcing" the request onto the very model it
    had just rejected, rewriting decision.role to "agent" and appending a
    "forced agent model (the agent model)" reason that named a model no longer in use.
    The route still worked, but every explanation of it was wrong, and the
    roster silently stopped meaning anything once it disagreed with the role.
    """
    return model == role_to_model("agent") or model in tool_capable_models()


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


def save_installed_models(models: list[str]) -> None:
    """Persist the most recently seen installed-models list to the user
    overlay, so it's available even when oMLX's server subprocess isn't
    currently up — mirrors favorite_models() reading OMLX_MODEL_SETTINGS
    directly for the same reason. Called whenever /models successfully
    refreshes the live list (including the Settings "Refresh models" button)."""
    _save_overlay({"installed_models": sorted(models)})


def set_role(role: str, model: str) -> None:
    """Point a role at a different installed model, persisted to the user overlay."""
    roles = {role: model}
    if role == "fast":            # router shares the fast model
        roles["router"] = model
    if role == "general":         # general drives the agent loop
        roles["agent"] = model
    _save_overlay({"roles": roles})


