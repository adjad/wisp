"""Wisp tools for catching up on Codex desktop tasks."""
from __future__ import annotations

from service.codex_monitor import CodexMonitorUnavailable, codex_monitor
from service.tools.registry import register


@register(
    "get_codex_updates",
    "Catch the user up on their local Codex tasks/chats. Shows which Codex "
    "tasks are running, finished, failed, or possibly stalled, with the latest "
    "agent update. Use for 'what are my Codex chats doing?', 'catch me up on "
    "Codex', 'which coding tasks finished?', or 'does any Codex task need me?'. "
    "This is read-only and never resumes, messages, or changes a Codex task.",
    {"type": "object", "properties": {
        "view": {"type": "string", "enum": ["recent", "active", "attention", "all"],
                 "description": "recent (default), active, attention, or all"},
        "hours": {"type": "integer", "description": "recent window; default 24 hours"},
        "limit": {"type": "integer", "description": "maximum tasks; default 20"},
        "include_subagents": {"type": "boolean",
                              "description": "include internal subagent tasks; default false"},
    }},
    category="assistant_read",
    aliases=[
        "what are my codex chats doing", "catch me up on codex",
        "which codex tasks finished", "does any codex task need me",
        "show my running coding agents", "codex status overview",
    ],
)
def get_codex_updates(view: str = "recent", hours: int = 24, limit: int = 20,
                      include_subagents: bool = False) -> str:
    try:
        return codex_monitor.render(view=view, hours=hours, limit=limit,
                                    include_subagents=include_subagents)
    except CodexMonitorUnavailable as exc:
        return (f"Codex task monitoring is unavailable: {exc}. Make sure Codex "
                "has been opened at least once for this macOS account.")
