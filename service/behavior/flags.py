"""Behavior-learning on/off switch, readable without importing anything else.

Callers on hot or tightly stubbed paths (main.lifespan in the remote-inference
build, the router, prompt assembly, the remote policy) check this BEFORE
importing ``service.assistant.behavior_runtime``, whose import chain opens
stores and pulls in modules those paths must not load while behavior learning
is off. Pure stdlib; ``behavior_runtime.enabled()`` delegates here.
"""
from __future__ import annotations

import os


def env_enabled() -> bool:
    """On only with ``WISP_BEHAVIOR=1``; anything else (unset, ``0``) is off."""
    return os.environ.get("WISP_BEHAVIOR") == "1"
