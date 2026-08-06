"""Conversation memory: server-side persisted sessions + budgeted context.

`store` persists every session/turn to SQLite under ~/.moe. `context` turns a
session into the token-capped message list the model actually sees, keeping a
fixed working set so long chats don't blow the memory budget.
"""
from service.memory.context import build_messages, maybe_summarize
from service.memory.store import SessionStore, store

__all__ = ["SessionStore", "store", "build_messages", "maybe_summarize"]
