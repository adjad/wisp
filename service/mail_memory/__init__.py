"""Mail memory: long-term reminders and profile facts from months-old mail.

Design and measurements: docs/plans/2026-10-26-email-digest-memory-research.md.
It runs in the background and shows a suggestion on the Brief only when it is
important (importance.py); the rest is stored with the reason it was held back.
Profile facts (profile_facts.py, docs/plans/2026-10-28-mail-profile-facts.md) are
offered in Memory -> Review as proposed facts and used only once accepted.
"""
