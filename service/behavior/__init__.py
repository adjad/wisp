"""Behavior engine: learn the user's routines from what they do, predict the
next day, and propose (or perform reversible) actions before being asked.

Portable by design: it runs in-process on the Mac. It is pure Python + SQLite
and must never import
``service.assistant``, ``service.tools``, ``service.main`` or ``service.config``
(importing those builds live stores against ~/.moe or needs macOS). A test pins
this. The Mac-side runtime that reads native sources and performs actions lives
in ``service.assistant.behavior_runtime``.

Code mines, the model only talks: patterns, predictions and nudge decisions are
deterministic statistics with evidence ids; an optional ``Phraser`` (an LLM)
only rewords text and may label ambiguous event titles from a fixed list.
"""
