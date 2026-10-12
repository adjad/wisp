"""Behavior-learning on/off switch.

Behavior learning is ON by default. Precedence, highest first:

1. ``WISP_BEHAVIOR`` in the environment, an explicit launch override and
   kill switch: ``1`` forces it on; ``0`` (or any other non-empty value)
   forces it off. Unset or empty defers to the next rule.
2. The persisted user setting ``behavior.enabled`` in ``~/.moe/config.yaml``
   (Settings -> "Learn my routines").
3. Default: on.

If ``config.yaml`` exists but cannot be read or is not a mapping, the saved
setting is unknown and may be an "off", so it counts as OFF (fail closed) until
the file is fixed; only an explicit ``WISP_BEHAVIOR=1`` still turns learning on.

Every caller asks per call and nothing caches the answer, so flipping the
setting starts or stops ingestion and nudging without restarting Wisp. Callers
on hot or tightly stubbed paths (the router, prompt assembly, the remote
policy) check this BEFORE importing ``service.assistant.behavior_runtime``,
whose import chain opens stores. ``behavior_runtime.enabled()`` delegates here.
"""
from __future__ import annotations

import os


def env_override() -> bool | None:
    """The explicit ``WISP_BEHAVIOR`` override: True, False, or None when unset.

    Only ``1`` turns it on; any other non-empty value (``0``, ``yes``) turns it
    off, so a malformed override can never switch learning on."""
    raw = os.environ.get("WISP_BEHAVIOR")
    if raw is None or raw == "":
        return None
    return raw == "1"


def setting_enabled() -> bool:
    """The persisted ``behavior.enabled`` setting (default True). False when the
    settings file is unreadable: an unknown setting never switches learning on."""
    try:
        from service.config import get_behavior_enabled
        return get_behavior_enabled()
    except Exception:  # noqa: BLE001 — fail closed: an unreadable setting is not consent
        return False


def setting_readable() -> bool:
    """False when ``~/.moe/config.yaml`` exists but cannot be read as a mapping."""
    try:
        from service.config import user_overlay_valid
        return user_overlay_valid()
    except Exception:  # noqa: BLE001
        return False


def remote_active() -> bool:
    """Whether a remote-inference profile is pinned for this launch. An
    invalid profile counts as active (fail closed: assume data would leave)."""
    try:
        from service.inference.remote_policy import remote_only_profile
        return remote_only_profile() is not None
    except Exception:  # noqa: BLE001
        return True


def phraser_enabled() -> bool:
    """Whether the engine may send wording / event-categorization requests to a
    model. On a Mac with local models it is on unless ``WISP_BEHAVIOR_PHRASER=0``.
    Under remote inference those requests carry calendar titles and nudge facts to
    the configured server, so it is OFF unless ``WISP_BEHAVIOR_PHRASER=1``."""
    raw = os.environ.get("WISP_BEHAVIOR_PHRASER")
    if remote_active():
        return raw == "1"
    return raw != "0"


def enabled() -> bool:
    override = env_override()
    return override if override is not None else setting_enabled()
