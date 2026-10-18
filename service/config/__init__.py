"""Configuration loading for the MOE service."""
from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from .credentials import resolve as _credential

import time
import yaml

from service.paths import MOE_DIR

from service.engine.mode import bundled_base_path, is_bundled
# External oMLX keeps its state in ~/.omlx; the bundled engine keeps its own copy under
# ~/.moe/engine/base so Wisp's reads and writes never reach a user's oMLX.app.
_OMLX_STATE = bundled_base_path() or Path.home() / ".omlx"

OMLX_SETTINGS = _OMLX_STATE / "settings.json"
# oMLX's own per-model settings (temperature, sampling, etc.) — each entry
# also carries the "is_favorite" flag the user sets by starring a model in
# oMLX's UI. Reading this file directly (rather than oMLX's live /v1/models
# HTTP endpoint) means the favorites list is available even when the oMLX
# server subprocess isn't currently running — only the menu-bar app has to be
# up, which it always is once anything's been configured.
OMLX_MODEL_SETTINGS = _OMLX_STATE / "model_settings.json"
MODELS_YAML = Path(__file__).parent / "models.yaml"
# User overlay — persisted settings live OUTSIDE the app bundle so a repackage
# (which overwrites the bundled models.yaml) can't wipe them. models.yaml holds
# code defaults; this holds the user's changes, deep-merged on top at read time.
USER_CONFIG = MOE_DIR / "config.yaml"

@lru_cache
def omlx_settings() -> dict:
    """oMLX's own settings.json — single source of truth for host/port/api_key.

    A fresh install has no such file: oMLX.app was never opened, or there is no
    engine at all yet. That reads as ``{}`` so the service still starts and setup
    can say what is missing, instead of the backend dying at launch.
    """
    try:
        with OMLX_SETTINGS.open() as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _settings_section(name: str) -> dict:
    section = omlx_settings().get(name)
    return section if isinstance(section, dict) else {}


@lru_cache
def _packaged_config() -> dict:
    """Code defaults shipped in the bundle. Never written to at runtime."""
    with MODELS_YAML.open() as f:
        return yaml.safe_load(f) or {}


_overlay_valid = True


@lru_cache
def _user_overlay() -> dict:
    """Persisted user changes from ~/.moe/config.yaml (may be absent/empty).

    A file that cannot be read, parsed, or is not a mapping yields ``{}`` so
    startup survives, and is remembered as invalid (``user_overlay_valid``) so
    privacy-sensitive settings can fail closed instead of reading it as defaults."""
    global _overlay_valid
    _overlay_valid = True
    if not USER_CONFIG.exists():
        return {}
    try:
        with USER_CONFIG.open() as f:
            loaded = yaml.safe_load(f)
    except Exception:  # noqa: BLE001 — a corrupt overlay must not break startup
        _overlay_valid = False
        return {}
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        _overlay_valid = False
        return {}
    return loaded


def user_overlay_valid() -> bool:
    """False when ``config.yaml`` exists but is unreadable or not a mapping."""
    _user_overlay()
    if not _overlay_valid and _authority_changes.current().ready:
        _authority_changes.invalidate()
    return _overlay_valid


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


from . import authority_changes as _authority_changes
from service.publication_authority import mutating as _publication_mutation, revocation as _publication_revocation


@lru_cache
def _effective_config() -> dict:
    return _deep_merge(_packaged_config(), _user_overlay())


_raw_overlay_cache_clear = _user_overlay.cache_clear
_effective_cache_clear = _effective_config.cache_clear


def _authority_projection(value):
    return {key: value.get(key) for key in ("behavior", "roles", "inference")}


def models_config() -> dict:
    """Resolve config normally; stabilize history authority only after a safe read.

    An unsettled read clears private caches first. A concurrent writer changes
    the revision, so that read cannot grant readiness even if its cache result
    arrives late. Direct file edits remain unobserved until an explicit reload.
    """
    before = _authority_changes.current()
    if not before.ready and before.pending == 0:
        _raw_overlay_cache_clear()
        _effective_cache_clear()
    value = _effective_config()
    _authority_changes.observe(_authority_projection(value), valid=_overlay_valid, expected=before)
    return value


@_publication_mutation
def _clear_config_caches():
    _authority_changes.invalidate()
    _raw_overlay_cache_clear()
    _effective_cache_clear()


# Preserve callers' public cache API; clears withdraw authority immediately.
_user_overlay.cache_clear = _clear_config_caches
models_config.cache_clear = _clear_config_caches
models_config.cache_info = _effective_config.cache_info
models_config.cache_parameters = _effective_config.cache_parameters


def _write_overlay(update: dict) -> dict:
    """Serialize read/merge/atomic replacement; never truncate the live overlay."""
    import fcntl
    import os
    import tempfile

    USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with (USER_CONFIG.parent / ".config.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = yaml.safe_load(USER_CONFIG.read_text()) if USER_CONFIG.exists() else {}
        if current is not None and not isinstance(current, dict):
            raise ValueError("Invalid configuration overlay; refusing to overwrite it")
        merged = _deep_merge(current or {}, update)
        fd, path = tempfile.mkstemp(prefix=".config-", dir=USER_CONFIG.parent)
        try:
            with os.fdopen(fd, "w") as output:
                yaml.safe_dump(merged, output, sort_keys=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(path, USER_CONFIG)
        finally:
            if os.path.exists(path):
                os.unlink(path)
    return merged


def _save_overlay(update: dict) -> None:
    # Refuse reentrant calls before any file/cache/authority change. Keep the
    # unsettled revision while I/O proceeds outside the short application lease;
    # concurrent writers may register, but none can publish readiness yet.
    with _publication_revocation():
        relevant = isinstance(update, dict) and bool(set(update) & {"behavior", "roles", "inference"})
        before = _authority_changes.current()
        token = _authority_changes.begin() if relevant else None
    try:
        merged = _write_overlay(update)
    finally:
        # The atomic writer has released its file lock before callbacks or
        # cache invalidation. Failure never preserves a previous grant.
        _raw_overlay_cache_clear()
        _effective_cache_clear()
        if token is not None:
            _authority_changes.finish(token)
    if not relevant:
        _authority_changes.observe(_authority_projection(_deep_merge(_packaged_config(), merged)),
                                   valid=True, expected=before)


def get_daily_summary_hour() -> int:
    """The legacy AM/PM choice: 8 = 8am (AM), 20 = 8pm (PM).

    It no longer picks when the brief fires; both runs in
    get_daily_summary_times() do. The scheduler still reads it on the day of an
    upgrade, to tell which run an older version's once-a-day receipt covered."""
    return int(models_config().get("daily_summary_hour", 8))


def set_daily_summary_hour(hour: int) -> int:
    hour = 20 if int(hour) >= 12 else 8   # only two choices: 8am or 8pm
    _save_overlay({"daily_summary_hour": hour})
    return hour


# The two Daily Summary runs. The defaults are the two times Wisp already
# offered (8 AM and 8 PM), so the one a user picked before still arrives on time.
DAILY_SUMMARY_DEFAULT_TIMES = {"morning": 8, "evening": 20}
# Each run keeps to its half of the day, so the "morning" and "evening" labels
# stay true and the morning run always comes first.
_DAILY_SUMMARY_HOURS = {"morning": range(0, 12), "evening": range(12, 24)}


def _daily_summary_hour(part: str, value: object) -> int:
    if type(value) is not int or value not in _DAILY_SUMMARY_HOURS[part]:
        first, last = _DAILY_SUMMARY_HOURS[part][0], _DAILY_SUMMARY_HOURS[part][-1]
        raise ValueError(f"{part} hour must be a whole hour from {first} through {last}")
    return value


def get_daily_summary_times() -> dict[str, int]:
    """Hour (0-23) of the morning and evening Daily Summary runs.

    Persisted in the user overlay so it survives repackaging. A missing or
    invalid entry falls back to its default rather than turning the run off."""
    stored = models_config().get("daily_summary_times")
    stored = stored if isinstance(stored, dict) else {}
    times = {}
    for part, default in DAILY_SUMMARY_DEFAULT_TIMES.items():
        try:
            times[part] = _daily_summary_hour(part, stored.get(part, default))
        except ValueError:
            times[part] = default
    return times


def set_daily_summary_times(*, morning: object = None, evening: object = None) -> dict[str, int]:
    """Change either run's hour. Raises ValueError, and saves nothing, when
    either value is out of range for its half of the day."""
    update = {part: _daily_summary_hour(part, value)
              for part, value in (("morning", morning), ("evening", evening))
              if value is not None}
    if update:
        _save_overlay({"daily_summary_times": {**get_daily_summary_times(), **update}})
    return get_daily_summary_times()


def get_behavior_enabled() -> bool:
    """Whether behavior learning ("Learn my routines") is on. Default True; only
    an explicit false in the user overlay (``behavior: {enabled: false}``)
    turns it off, as does an overlay that exists but cannot be read (it may
    hold that false). ``WISP_BEHAVIOR`` overrides this (service/behavior_switch.py)."""
    if not user_overlay_valid():
        return False    # an unreadable settings file might hold the user's "off": fail closed
    section = models_config().get("behavior")
    return not (isinstance(section, dict) and section.get("enabled") is False)


def terse_tool_schemas_enabled() -> bool:
    """Opt-in terse tool schemas: one-line descriptions, no parameter prose.
    Meant for a model fine-tuned on the tools; OFF unless WISP_TERSE_TOOL_SCHEMAS
    is 1/true/yes/on. Read per call (not at import) so tests and a relaunched
    backend see the current value; unset or unrecognized keeps full schemas."""
    import os
    return os.environ.get("WISP_TERSE_TOOL_SCHEMAS", "").strip().lower() in {"1", "true", "yes", "on"}


def set_behavior_enabled(enabled: bool) -> bool:
    if type(enabled) is not bool:
        raise ValueError("behavior enabled must be a boolean")
    _save_overlay({"behavior": {"enabled": enabled}})
    return enabled


def omlx_base_url() -> str:
    if is_bundled():
        from service.engine import BUNDLED_PORT
        return f"http://127.0.0.1:{BUNDLED_PORT}"
    cfg = models_config()["omlx"].get("base_url")
    if cfg:
        return cfg.rstrip("/")
    s = _settings_section("server")
    return f"http://{s.get('host') or '127.0.0.1'}:{s.get('port') or 8000}"


def omlx_api_key() -> str:
    if is_bundled():
        # The engine's own key lives in its settings.json; a key the native launcher
        # holds for a user's oMLX.app must never be sent to the bundled engine.
        return str(_settings_section("auth").get("api_key") or "")
    return _credential("WISP_LOCAL_OMLX_KEY") or str(_settings_section("auth").get("api_key") or "")


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
    return thinking_kwargs(model, on=False)


def thinking_kwargs(model: str, *, on: bool) -> dict:
    """`chat_template_kwargs` stating the think block EXPLICITLY, on or off.

    For a model in `explicit_thinking` (models.yaml) the kwarg is sent on
    every request, both ways. Measured 2026-10-13 on
    Ling-3.0-tiny-Wisp-V3-merged-oQ4e: it was fine-tuned thinking-off and
    emits zero reasoning under its default template, but DOES think when
    `enable_thinking=True` is sent (3-4k chars, ~10x slower). Until then Wisp
    only ever sent `False` or nothing, so every path that meant "think" was
    inheriting the oMLX admin profile's toggle — which had been flipped off
    that afternoon without Wisp knowing. Explicit both ways pins the behavior
    Wisp was measured with, whatever the profile says.

    For every other model this is exactly `no_thinking_kwargs`: the `False`
    form for models verified to honor it, nothing otherwise. Their "on" paths
    keep omitting the kwarg, because their measured behavior depends on the
    template default and has not been re-measured with an explicit `True`.
    """
    if model in explicit_thinking_models():
        return {"chat_template_kwargs": {"enable_thinking": bool(on)}}
    if not on and model in no_thinking_capable():
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {}


def thinks_explicitly(model: str) -> bool:
    return model in explicit_thinking_models()


# Output budget for a user-facing summary when its think block is on: the
# block comes out of max_tokens, and 512 cannot hold ~1k tokens of V3
# reasoning plus the summary (finish_reason=length, then the "did not
# complete" fallback). 1,800 held every probe on 2026-10-13 with room.
_THINKING_SUMMARY_MAX_TOKENS = 1800


def user_facing_summary_budget(model: str, base: int) -> int:
    """`base` unless this summary request will carry an explicit think block."""
    kwargs = user_facing_summary_kwargs(model).get("chat_template_kwargs") or {}
    return max(base, _THINKING_SUMMARY_MAX_TOKENS) if kwargs.get("enable_thinking") is True else base


def user_facing_summary_kwargs(model: str) -> dict:
    """Template kwargs for summaries that are shown directly to the user.

    Ling's reasoning improves the synthesis of a user-facing digest or daily
    brief, so those requests deliberately keep its think block enabled.  This
    exception is intentionally here, rather than removing Ling from
    ``no_thinking_capable``: background extraction, search synthesis, and the
    fast narration paths still rely on that lower-latency setting.

    Other model families retain the established summary optimization.

    An `explicit_thinking` model says so outright (`enable_thinking: True`)
    rather than relying on the template default — see thinking_kwargs.
    """
    if thinks_explicitly(model):
        return thinking_kwargs(model, on=True)
    if model.casefold().startswith("ling-"):
        return {}
    return no_thinking_kwargs(model)


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
    from .quarantine import lease

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    def _open(req, timeout):
        if is_bundled():
            # The bundled engine's key never rides an environment or system proxy, and a redirect
            # cannot carry it elsewhere.
            return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect).open(
                req, timeout=timeout)
        return urllib.request.urlopen(req, timeout=timeout)

    def _get(url: str, timeout: float = 5.0):
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {omlx_api_key()}"})
        with lease(), _open(req, timeout) as r:
            return _json.loads(r.read())

    if is_bundled():
        # These plain urllib calls cannot prove who answers. The bundled engine's key goes
        # only to the child this backend launched and the checked client has verified: otherwise
        # the port may belong to anything, so skip (the file write still records the intent for
        # the next start).
        from service.engine.runtime import manager
        engine = manager()
        if engine.incarnation() is None or not engine.verified:
            return False

    try:
        from service.config.endpoints import endpoint
        base = endpoint().base_url
        if is_bundled():
            from service.engine import BUNDLED_PORT
            if base.rstrip("/") != f"http://127.0.0.1:{BUNDLED_PORT}":
                return False        # an overridden endpoint is not the engine that was verified
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
        with lease(), _open(req, 15) as r:
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




def _local_role_model(role: str) -> str:
    """Resolve only the local roster, ignoring any active remote binding."""
    roles = models_config()["roles"]
    model = roles.get(role)
    if not model:
        model = roles[models_config()["default_role"]]
    return model


def role_to_model(role: str) -> str:
    """Resolve a logical role to its currently bound concrete model id."""
    from service.inference.remote_policy import remote_only_profile, require_generation_role
    profile = remote_only_profile()
    if profile is not None:
        require_generation_role(role)
        return profile.model_id
    model = _local_role_model(role)
    binding = models_config().get("inference", {}).get("bindings", {}).get(role, {})
    return binding.get("model_id") or model


def tool_capable_models() -> list[str]:
    return models_config().get("tool_capable", [])


def no_thinking_capable() -> list[str]:
    return models_config().get("no_thinking_capable", [])


def explicit_thinking_models() -> list[str]:
    """Models whose think block Wisp states explicitly on every request."""
    return models_config().get("explicit_thinking", [])


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


def set_roles(assignments: dict[str, str]) -> None:
    """Point several roles at installed models in ONE overlay write, so a failure
    cannot leave some roles moved and others not."""
    from service.inference.remote_policy import require_local_models_disabled
    require_local_models_disabled("Inference settings changes")
    roles: dict[str, str] = {}
    for role, model in assignments.items():
        roles[role] = model
        if role == "fast":            # router shares the fast model
            roles["router"] = model
        if role == "general":         # general drives the agent loop
            roles["agent"] = model
    # Settings model selection is a local rollback, including coupled roles.
    _save_overlay({"roles": roles, "inference": {"bindings": {
        name: {"endpoint": "local", "model_id": value, "revision": "", "profile": "",
               "context_window": None, "qualified_capabilities": [], "dimensions": 0}
        for name, value in roles.items()}}})


def set_role(role: str, model: str) -> None:
    """Point a role at a different installed model, persisted to the user overlay."""
    set_roles({role: model})


CLOUD_ASSIGNABLE_ROLES = ("reasoning", "coding", "research")


LOCAL_PROVIDER_ROLES = ("reasoning", "agent", "coding")
# Roles that run Wisp's tool loop and therefore require a passing qualification.
LOCAL_PROVIDER_TOOL_ROLES = frozenset({"agent", "coding"})
# Schema 1 did not bind the API prefix and cannot establish this identity.
LOCAL_PROVIDER_QUALIFICATION_SCHEMA = 2
# Every role a provider connection can bind or reset, plus `general`, which
# set_roles also points `agent` at. Connecting rewrites the whole provider role
# set (selected roles bind, any other provider-bound role resets), so a change to
# any of these while a connection test is pending must supersede that test.
PROVIDER_CONNECTION_ROLES = (frozenset(LOCAL_PROVIDER_ROLES) | frozenset(CLOUD_ASSIGNABLE_ROLES)
                             | {"general"})


def local_provider_qualification(endpoint_cfg: dict, model_id: str, base_url: str,
                                 api_prefix: str) -> dict | None:
    """Accept only evidence explicitly bound to this exact app, prefix and model."""
    record = endpoint_cfg.get("qualification") if isinstance(endpoint_cfg, dict) else None
    if (isinstance(record, dict) and type(record.get("schema")) is int
            and record["schema"] == LOCAL_PROVIDER_QUALIFICATION_SCHEMA
            and record.get("qualified") is True
            and record.get("model_id") == model_id and record.get("base_url") == base_url
            and isinstance(record.get("api_prefix"), str)
            and record["api_prefix"] == api_prefix
            and isinstance(record.get("effective_context"), int)
            and not isinstance(record.get("effective_context"), bool)):
        return record
    return None


def local_provider_settings() -> dict:
    """Return display-safe configuration for one external loopback inference app."""
    cfg = models_config().get("inference", {})
    endpoint_cfg = cfg.get("endpoints", {}).get("local_provider", {})
    if not isinstance(endpoint_cfg, dict):
        endpoint_cfg = {}
    bindings = cfg.get("bindings", {})
    roles = [role for role in LOCAL_PROVIDER_ROLES
             if isinstance(bindings.get(role), dict)
             and bindings[role].get("endpoint") == "local_provider"]
    enabled = bool(endpoint_cfg) and endpoint_cfg.get("enabled", True) is True
    base_url = str(endpoint_cfg.get("base_url", "http://127.0.0.1:8767"))
    model_id = str(endpoint_cfg.get("model_id", ""))
    api_prefix = endpoint_cfg.get("api_prefix", "/v1")
    record = local_provider_qualification(endpoint_cfg, model_id, base_url, api_prefix)
    return {
        "enabled": enabled,
        "active": enabled and bool(roles) and not cloud_super_model_enabled(),
        "base_url": base_url,
        "api_prefix": str(endpoint_cfg.get("api_prefix", "/v1")),
        "model_id": model_id,
        "context_window": int(endpoint_cfg.get("context_window", 8192)),
        "roles": roles,
        "authenticated": False,
        "tools_qualified": record is not None,
        "qualified_context": record["effective_context"] if record else 0,
        "qualified_at": record.get("tested_at", "") if record else "",
    }


def _local_role_reset(role: str) -> dict:
    return {"endpoint": "local", "model_id": _local_role_model(role),
            "revision": "", "profile": "", "context_window": None,
            "qualified_capabilities": [], "dimensions": 0}


def set_local_provider(endpoint_cfg: dict, model_id: str, context_window: int,
                       roles: list[str], *, qualification: dict | None = None) -> None:
    """Bind an external loopback app to the chosen workloads.

    Tool-using workloads (`agent`, `coding`) need a server-recorded passing
    qualification for this exact app and model; a caller cannot assert one. The
    saved window is the one the app actually honors, which can be smaller than
    the window the user typed.
    """
    from service.inference.remote_policy import require_local_models_disabled
    require_local_models_disabled("Inference settings changes")
    if (not roles or len(set(roles)) != len(roles)
            or any(role not in LOCAL_PROVIDER_ROLES for role in roles)):
        raise ValueError("Choose Reasoning, Agent, or Coding for the local provider")
    qualified = bool(qualification and qualification.get("qualified") is True)
    if LOCAL_PROVIDER_TOOL_ROLES & set(roles) and not qualified:
        raise ValueError("Agent and Coding need the tool-calling test to pass first")
    window = context_window
    if qualified:
        window = min(context_window, int(qualification["effective_context"]))
    from service.config.endpoints import endpoint_from_config
    validated_endpoint = endpoint_from_config("local_provider", endpoint_cfg)
    api_prefix = validated_endpoint.api_prefix
    saved_endpoint = {**endpoint_cfg, "base_url": validated_endpoint.base_url,
                      "api_prefix": api_prefix, "model_id": model_id, "context_window": window}
    saved_endpoint["qualification"] = ({
        "schema": LOCAL_PROVIDER_QUALIFICATION_SCHEMA, "qualified": True, "model_id": model_id,
        "base_url": validated_endpoint.base_url, "api_prefix": api_prefix,
        "effective_context": int(qualification["effective_context"]),
        "tested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    } if qualified else None)  # None, not {}: the overlay deep-merges dicts and would keep old keys
    bindings: dict[str, dict] = {}
    current = models_config().get("inference", {}).get("bindings", {})
    for role in LOCAL_PROVIDER_ROLES:
        if role in roles:
            bindings[role] = {
                "endpoint": "local_provider", "model_id": model_id,
                "revision": "", "profile": "", "context_window": window,
                "qualified_capabilities": (["tools"] if qualified and role in LOCAL_PROVIDER_TOOL_ROLES
                                           else []),
                "dimensions": 0}
        elif (isinstance(current.get(role), dict)
              and current[role].get("endpoint") == "local_provider"):
            bindings[role] = _local_role_reset(role)
    _save_overlay({"inference": {"endpoints": {"local_provider": saved_endpoint},
                                 "bindings": bindings}})


def disable_local_provider() -> None:
    current = models_config().get("inference", {}).get("bindings", {})
    bindings = {role: _local_role_reset(role) for role in LOCAL_PROVIDER_ROLES
                if isinstance(current.get(role), dict)
                and current[role].get("endpoint") == "local_provider"}
    _save_overlay({"inference": {
        "endpoints": {"local_provider": {"enabled": False, "qualification": None}},
        "bindings": bindings,
    }})


def cloud_provider_settings() -> dict:
    """Return display-safe cloud configuration. Credentials are never resolved."""
    cfg = models_config().get("inference", {})
    endpoint_cfg = cfg.get("endpoints", {}).get("cloud", {})
    if not isinstance(endpoint_cfg, dict):
        endpoint_cfg = {}
    bindings = cfg.get("bindings", {})
    super_cfg = cfg.get("super_model", {})
    if not isinstance(super_cfg, dict):
        super_cfg = {}
    roles = [role for role in CLOUD_ASSIGNABLE_ROLES
             if isinstance(bindings.get(role), dict)
             and bindings[role].get("endpoint") == "cloud"]
    first = bindings.get(roles[0], {}) if roles else {}
    provider_name = str(endpoint_cfg.get("provider", "openrouter"))
    credential_ref = str(endpoint_cfg.get("credential_ref", "keychain:cloud"))
    credential_name = (credential_ref.removeprefix("keychain:")
                       if credential_ref.startswith("keychain:") else "cloud")
    return {
        "enabled": bool(endpoint_cfg) and endpoint_cfg.get("enabled", True) is True,
        "provider": provider_name,
        "provider_label": "OpenRouter" if provider_name == "openrouter" else "OpenAI-compatible",
        "base_url": str(endpoint_cfg.get("base_url", "https://openrouter.ai")),
        "api_prefix": str(endpoint_cfg.get("api_prefix", "/api/v1")),
        "model_id": str(super_cfg.get("model_id") or first.get("model_id", "")),
        "context_window": int(super_cfg.get("context_window")
                              or first.get("context_window", 16384)),
        "credential_name": credential_name,
        "roles": roles,
        "super_model_enabled": bool(super_cfg.get("enabled", False)),
    }


def set_cloud_provider(endpoint_cfg: dict, model_id: str, context_window: int,
                       roles: list[str], super_model_enabled: bool = False) -> None:
    """Persist one tested cloud endpoint and explicit generation-role bindings."""
    from service.inference.remote_policy import require_local_models_disabled
    require_local_models_disabled("Inference settings changes")
    selected = set(roles)
    if not selected.issubset(CLOUD_ASSIGNABLE_ROLES):
        raise ValueError("Unsupported cloud role")
    current = models_config().get("inference", {}).get("bindings", {})
    bindings = {}
    for role in CLOUD_ASSIGNABLE_ROLES:
        if role in selected:
            bindings[role] = {
                "endpoint": "cloud", "model_id": model_id, "revision": "",
                "profile": "", "context_window": context_window,
                "qualified_capabilities": [], "dimensions": 0,
            }
        elif not (isinstance(current.get(role), dict)
                  and current[role].get("endpoint") == "local_provider"):
            bindings[role] = {
                "endpoint": "local", "model_id": _local_role_model(role), "revision": "",
                "profile": "", "context_window": None,
                "qualified_capabilities": [], "dimensions": 0,
            }
    _save_overlay({"inference": {
        "endpoints": {"cloud": endpoint_cfg},
        "bindings": bindings,
        "super_model": {
            "enabled": bool(super_model_enabled),
            "model_id": model_id,
            "context_window": context_window,
        },
    }})


def cloud_super_model_enabled() -> bool:
    from service.inference.remote_policy import remote_only_profile
    if remote_only_profile() is not None:
        return False
    cfg = models_config().get("inference", {})
    endpoint_cfg = cfg.get("endpoints", {}).get("cloud", {})
    super_cfg = cfg.get("super_model", {})
    return (isinstance(endpoint_cfg, dict)
            and endpoint_cfg.get("enabled", True) is True
            and isinstance(super_cfg, dict)
            and super_cfg.get("enabled", False) is True)


def disable_cloud_provider() -> None:
    current = models_config().get("inference", {}).get("bindings", {})
    bindings = {}
    for role in CLOUD_ASSIGNABLE_ROLES:
        if isinstance(current.get(role), dict) and current[role].get("endpoint") == "cloud":
            bindings[role] = {
                "endpoint": "local", "model_id": _local_role_model(role), "revision": "",
                "profile": "", "context_window": None,
                "qualified_capabilities": [], "dimensions": 0,
            }
    _save_overlay({"inference": {"endpoints": {"cloud": {"enabled": False}},
                                  "bindings": bindings,
                                  "super_model": {"enabled": False}}})
