"""Behavior-learning on/off switch.

Behavior learning is ON by default. Precedence, highest first:

1. ``WISP_BEHAVIOR`` in the environment, an explicit launch override and
   kill switch: ``1`` forces it on; ``0`` (or any other non-empty value)
   forces it off. Unset or empty defers to the next rule.
2. The persisted user setting ``behavior.enabled`` in ``~/.moe/config.yaml``
   (Settings -> "Learn my routines").
3. Default: on.

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
    """The persisted ``behavior.enabled`` setting (default True)."""
    try:
        from service.config import get_behavior_enabled
        return get_behavior_enabled()
    except Exception:  # noqa: BLE001 — an unreadable setting is the default, not a crash
        return True


def enabled() -> bool:
    override = env_override()
    return override if override is not None else setting_enabled()
