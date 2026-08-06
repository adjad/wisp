"""Approvers resolve `confirm`-tier actions to allow/deny.

The agent loop only calls `confirm()` for actions the policy flagged as needing
the user's say-so. Read-only actions never reach here.

An answer carries a SCOPE as well as a verdict: "once" (this call only),
"always" (record a standing grant so the same tool+target stops asking), or
"never" (record a standing block). The scope is applied here rather than in the
HTTP layer so every approver — including the headless one used by tests —
agrees on what an answer means. See service/safety/grants.py for what a
standing grant can and can't cover.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from service.safety import grants


def _apply_scope(action: dict, approved: bool, scope: str) -> None:
    """Persist a standing answer, if the user gave one. Never raises — failing
    to REMEMBER an answer must not fail the action the user just approved."""
    if scope not in ("always", "never"):
        return
    try:
        grants.grant(action.get("tool", ""), action.get("args") or {},
                     decision="allow" if approved else "deny")
    except Exception:  # noqa: BLE001
        pass


class AutoApprover:
    """Headless/testing approver with a fixed answer for confirm-tier actions."""

    def __init__(self, allow_confirm: bool = False) -> None:
        self.allow_confirm = allow_confirm

    async def confirm(self, action: dict) -> bool:
        return self.allow_confirm


class InteractiveApprover:
    """Emits a `confirm` event to the client and waits for an external resolve().

    The HTTP layer wires `resolve(action_id, approved, scope)` to an /approve
    endpoint so the Liquid-Glass confirmation card can answer.
    """

    def __init__(self, emit: Callable[[dict], Awaitable[None]]) -> None:
        self._emit = emit
        self._pending: dict[str, asyncio.Future] = {}
        self._actions: dict[str, dict] = {}

    async def confirm(self, action: dict) -> bool:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[action["id"]] = fut
        self._actions[action["id"]] = action
        # `grantable` tells the card whether to offer an "Always allow" button
        # at all — sending mail/messages never gets one (grants._NEVER_GRANTABLE),
        # and showing a button that silently does nothing would be worse than
        # not showing it.
        await self._emit({
            "type": "confirm",
            "grantable": action.get("tool") not in grants.never_grantable(),
            "scope_hint": grants.scope_for(action.get("tool", ""), action.get("args") or {}),
            **action,
        })
        try:
            return await fut
        finally:
            self._pending.pop(action["id"], None)
            self._actions.pop(action["id"], None)

    def resolve(self, action_id: str, approved: bool, scope: str = "once") -> bool:
        fut = self._pending.get(action_id)
        if fut and not fut.done():
            _apply_scope(self._actions.get(action_id, {}), approved, scope)
            fut.set_result(approved)
            return True
        return False
