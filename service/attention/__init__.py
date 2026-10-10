"""Proactive attention: notice what the user has not put on their calendar.

The rule this package serves (user decision, 2026-10-08): Wisp interrupts only
when a text or email states something coming up soon that is in neither
Calendar nor Reminders. Wisp then adds the reminder and alerts. Promotional and
scam messages never qualify, and importance comes from recent two-way contact.

The measurement tools use frozen snapshots. ``detectors`` is pure code;
``extract`` optionally calls only a resident, managed loopback model for the
synthetic Catch demo. This package never reads live sources or writes stores.
The runtime lives in service.assistant.attention_demo.
"""
