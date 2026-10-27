"""Mail memory: long-term reminders (and later profile facts) from months-old mail.

Design and measurements: docs/plans/2026-10-26-email-digest-memory-research.md.
It runs in the background and shows a suggestion on the Brief only when it is
important (importance.py); the rest is stored with the reason it was held back.
"""
