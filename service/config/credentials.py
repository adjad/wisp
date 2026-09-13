"""Consume the native launch bridge before tools can inherit its environment.

Only these fixed references use the private store. Unrelated legacy env:
references retain their existing behavior. Never include values in exceptions.
"""
from __future__ import annotations

import os
import re

_NAMES = ("WISP_LOCAL_OMLX_KEY", "WISP_MINI_INFERENCE_KEY", "WISP_MINI_NODE_KEY")
_VALUES = {name: os.environ.pop(name) for name in _NAMES if name in os.environ}


def resolve(name: str) -> str:
    if name not in _NAMES:
        return os.environ.get(name, "").strip()
    value = _VALUES.get(name, "")
    if value and not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Invalid native credential bridge value")
    return value
