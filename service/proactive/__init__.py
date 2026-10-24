"""Proactive engine: Wisp does the catching up before you ask.

triage (one small call per item) → judge (one bounded call, ≤8 items) →
prepare (≤5 ready-to-fire cards, no tools) → executor (only on a tap, through
verified paths). Output is `workspace.proactive` on GET /assistant/today
(cycle.attach) and the routes in api.py.
"""
