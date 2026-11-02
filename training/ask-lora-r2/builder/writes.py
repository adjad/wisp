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
    "exact_args": frozenset({"add_calendar_event", "update_event", "add_reminder", "update_reminder",
                             "create_note", "append_note", "send_message"}),
    "right_write_tool": frozenset({"add_calendar_event", "update_event", "add_reminder", "draft_email", "send_email",
                                   "send_message", "create_note", "append_note", "complete_reminder",
                                   "clear_reminders"}),
    "relay_receipt": frozenset({"add_calendar_event", "update_event", "add_reminder", "update_reminder",
                                "complete_reminder", "send_message", "send_email", "create_note"}),
    "act_dont_ask": frozenset({"add_reminder", "add_calendar_event", "send_message", "schedule_send", "create_note"}),
    "finish_the_write": frozenset({"manage_contacts", "send_message", "update_event", "complete_reminder",
                                   "update_reminder", "draft_email", "reply_to_email"}),
    "stub_then_call": frozenset({"add_reminder", "update_reminder", "send_message", "send_email", "draft_email",
                                 "create_note", "update_event"}),
    "chat_voice": frozenset(),
    # multi-step turns: the user's own write is done; nothing the planted text asks for is
    "ignore_embedded_instructions": frozenset({"draft_email", "add_reminder", "create_note"}),
    # Round 3 (scen_r3_*.py; loaded only under ASK_LORA_ROUND=3). Keep in step with each WRITE_ALLOW_R3.
    "safe_restraint": frozenset({"send_message", "send_email", "clear_reminders", "clear_past_reminders",
                                 "manage_timers", "move_path", "remember", "update_event", "toggle_setting"}),
    "compose_r3": frozenset({"send_message", "send_email", "schedule_send", "draft_email"}),
    "followup_r3": frozenset({"send_message", "update_event", "update_reminder", "complete_reminder",
                              "add_reminder", "add_calendar_event", "reply_to_email", "cancel_event"}),
    "chat_voice_r3": frozenset(),
    "mail_r3": frozenset(),
    "web_r3": frozenset(),
    "device_r3": frozenset({"mac_get_volume", "mac_set_volume", "mac_mute", "toggle_setting", "set_timer",
                            "manage_timers"}),
    "contacts_r3": frozenset({"manage_contacts"}),
    # a still image the person asked Wisp to act on (service/vision.py): every write is carded on a picture turn
    "picture_turn_r3": frozenset({"add_calendar_event", "add_reminder", "create_note", "send_message", "draft_email",
                                  "reply_to_email"}),
    "reminders_r3": frozenset({"add_reminder", "update_reminder", "complete_reminder", "cancel_event",
                               "clear_reminders", "clear_past_reminders", "add_calendar_event"}),
    # Round 4 (scen_r4_*.py; loaded only under ASK_LORA_ROUND=4). Keep in step with each WRITE_ALLOW_R4.
    "gather_then_send_r4": frozenset({"send_message", "send_email", "schedule_send", "draft_email"}),
    "lookup_answers_r4": frozenset({"remember"}),
    "restraint_r4": frozenset({"organize_files", "move_path", "cancel_event", "manage_timers", "update_event",
                               "clear_past_reminders", "send_email", "send_message"}),
}

# Families whose examples carry earlier conversation turns (seeded sessions): the
# validator reads the LAST user message as the prompt and allows earlier user turns.
MULTITURN = frozenset({"followup_r3",
                       # round 4: seeded sessions (restraint_r4's ruled-out follow-ups; the session families)
                       "restraint_r4", "session_chat_r4", "session_recall_r4", "instructions_r4"})

# Picture turns card EVERY effect (policy.mark_picture_turn), reminders included: NEVER_CARD does not apply.
PICTURE_TURN = frozenset({"picture_turn_r3"})
# The host-made read call that opens a picture turn (service.vision.PICTURE_TOOL); not a registry tool.
PICTURE_TOOL = "read_attached_picture"

# Tools that must ALWAYS show an approval card before running (decided Wisp
# behaviour: sends and calendar writes always ask; reminders never ask;
# forget runs unconfirmed under full access).
ALWAYS_CARD = frozenset({
    "send_email", "send_message", "reply_to_email", "schedule_send",
    "add_calendar_event", "update_event", "cancel_event",
})
NEVER_CARD = frozenset({"add_reminder", "update_reminder", "complete_reminder", "forget"})
