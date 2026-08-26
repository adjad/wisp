"""The assistant layer: connectors → commitments store → reminders → SSE.

Turns Wisp from a chat wrapper into a personal assistant that watches the
calendar (and later mail), tracks upcoming commitments, and surfaces a live
countdown + reminders in the notch. See ASSISTANT_ARCHITECTURE.md.
"""
from service.assistant.store import assistant_store
from service.assistant.hub import hub
from service.assistant import scheduler

__all__ = ["assistant_store", "hub", "scheduler"]
