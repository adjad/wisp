"""Round-2 write support: the one shared contract between the builder
(harness/world/build/scen) and the validator (validate/selftest).

A Scenario may carry:
- ``approve``: one entry per approval card the turn raises, in order;
  True = the user taps Allow, False = Deny. Missing entries mean Allow.
- ``expect_cards``: tool names that must raise an approval card.

(``approve`` may also be a single bool applied to every card.) The builder
reads both with getattr, so a Scenario without them behaves as before.

Each built example's metadata line then carries:
- ``writes``: names of the non-read tools called anywhere in the example
  (history or target), in order ("non-read" = model_led.effectful_call, see
  harness.is_write; Bad steps count, they stay in the history);
- ``approve`` and ``expect_cards``, copied from the scenario (null if unset);
- ``cards_seen``: tool names whose approval card the host actually raised,
  for the whole TURN (the same list on every step of it);
- ``shown_final``: the text the host displayed, when the host rewrote the
  model's final (``_verified_final`` on write turns) or ended the turn itself,
  else null. Only a turn that called a write may have one; a read turn whose
  final the host changed is a build failure. The training target is always
  the scripted step, never ``shown_final``;
- ``effects``: [tool, args] of every write that took effect in the world
  (World.effects), for the whole turn.

Host behaviour scenario authors must script for (measured on this branch):
- A DENIED write ends the turn with no further model request: sends
  (loop._TERMINAL_OUTBOUND_DENIALS) show "Okay — I didn’t send it.", other
  writes "The requested action was not completed because approval was
  denied." The tool result is "The user denied this action." So a deny
  scenario's steps END at the write call (no Final); the host text lands in
  ``shown_final``.
- add_reminder-only turns show the tool receipt, not the model's final.
- A prompt containing "my calendar" / "my inbox" / "my messages" / "my
  email" sets a fresh-personal obligation that a write does not satisfy:
  the shown final becomes "I haven't checked a current personal source for
  that yet. ..." Avoid those phrasings in write prompts (or read first).
- update_event's when_iso is pinned to the Mac's UTC offset before the
  card, clear_reminders gets expected_ids, reply_to_email gets the prepared
  envelope; build.HOST_BOUND accepts exactly those rewrites.

WRITE_ALLOW maps a family to the non-read tools its examples may call.
A family absent from it stays reads-only (the round-1 rule). The validator
fails any example that calls a non-read tool its family does not allow, and
any example whose ``expect_cards`` differ from ``cards_seen``.
"""
from __future__ import annotations

# Filled in as the round-2 write families land (family -> allowed write tools).
WRITE_ALLOW: dict[str, frozenset[str]] = {
    "safe_effects": frozenset({"send_message", "send_email", "schedule_send", "remember", "forget"}),
}

# Tools that must ALWAYS show an approval card before running (decided Wisp
# behaviour: sends and calendar writes always ask; reminders never ask;
# forget runs unconfirmed under full access).
ALWAYS_CARD = frozenset({
    "send_email", "send_message", "reply_to_email", "schedule_send",
    "add_calendar_event", "update_event", "cancel_event",
})
NEVER_CARD = frozenset({"add_reminder", "update_reminder", "complete_reminder", "forget"})
