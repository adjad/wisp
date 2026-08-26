"""Failure/latency/disconnect injection — thin accessors over
world.state["injection"], consulted by sandbox/outbound.py before each
action result and by sandbox/sync.py before each push.

Knobs:
  fail: {action_type: error_message}   — post ok:false without mutating world
  stall_ms: {action_type: ms}          — sleep before posting a result
  drop_result: [action_type]           — never post at all (exercises the
                                          45s outbox.DEFAULT_TIMEOUT_S)
  unavailable: [source]                — diagnostics.available=false
  sync_paused: bool                    — sync loops skip every push
  latency_ms: int                      — fixed delay on every sandbox->backend POST
"""
from __future__ import annotations

from typing import Any


def get_fail(state: dict, action_type: str) -> str | None:
    return (state.get("injection", {}).get("fail") or {}).get(action_type)


def get_stall_ms(state: dict, action_type: str) -> int:
    return int((state.get("injection", {}).get("stall_ms") or {}).get(action_type, 0))


def is_dropped(state: dict, action_type: str) -> bool:
    return action_type in (state.get("injection", {}).get("drop_result") or [])


def is_unavailable(state: dict, source: str) -> bool:
    return source in (state.get("injection", {}).get("unavailable") or [])


DEFAULT_INJECTION: dict[str, Any] = {
    "fail": {}, "stall_ms": {}, "drop_result": [], "unavailable": [],
    "sync_paused": False, "latency_ms": 0,
}


def apply_patch(state: dict, patch: dict) -> None:
    """Shallow-merge `patch` into state["injection"], replacing (not
    merging) list/dict values per key so clearing a knob is a single-key
    write (`{"fail": {}}`) rather than requiring a full-object PUT."""
    injection = state.setdefault("injection", dict(DEFAULT_INJECTION))
    for key, value in patch.items():
        if key not in DEFAULT_INJECTION:
            continue
        injection[key] = value
