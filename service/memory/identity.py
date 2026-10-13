"""Who the user is — one definition, shared by everything that reads their data.

Every consumer of the user's own mail and messages — the daily brief,
`summarize_messages`, `summarize_emails` — used to run with no statement of
who the user is at all. That is a correctness bug, not a polish issue, because
the raw material is a conversation between several people and second-person
pronouns in it belong to whoever was addressed:

    Group of 3 (Mom, dad, Trishe) | Mom: @Trishe - Your post has 439 likes

With no identity in the prompt the summarizer read "Your post" as the reader's
and reported the user's sister's milestone as the user's own. The message data
was correct at every layer — chat.db, handle resolution, contact names — the
prompt simply never said who "you" is.

So this module provides two blocks:

- `identity_block()`  — the facts: name, own email addresses, today's date.
- `attribution_rules(shape)` — how to READ a message line given those facts:
  which part of the line names the speaker, what a group label does and does
  not list, and the pronoun rule above. `shape` picks the caller's line format
  ("directed" arrows for summarizers, "verbatim" for the raw record); only the
  matching rule is emitted, since an unfirable rule still costs attention.

Both are cheap string builders with no model calls, safe to call per request.
"""
from __future__ import annotations

import subprocess
import time

# The macOS full name (`id -F`) is the one identity signal available without any
# TCC grant, any sync, or any model call — it works on a cold install before
# Mail/Messages/Contacts have ever synced. Cached because it shells out and the
# answer cannot change while the process is alive.
_full_name: str | None = None


def user_name() -> str:
    """The user's real name, or "" if it can't be determined."""
    global _full_name
    if _full_name is None:
        try:
            _full_name = subprocess.run(["id", "-F"], capture_output=True,
                                        text=True, timeout=5).stdout.strip()
        except Exception:  # noqa: BLE001 — identity is best-effort, never fatal
            _full_name = ""
    return _full_name


def user_emails() -> list[str]:
    """The user's own account addresses, as reported by the Swift Mail sync."""
    try:
        from service.tools.email_tools import get_identity_emails
        return get_identity_emails()
    except Exception:  # noqa: BLE001
        return []


def identity_block(*, with_date: bool = True) -> str:
    """Ground-truth identity facts. Empty string if nothing is known.

    Deliberately name + addresses only — a full saved-contact roster (100+
    names) would crowd out the messages a summary prompt is supposed to be
    reading.
    """
    name = user_name()
    emails = user_emails()
    if not name and not emails:
        return ""
    lines = ["THE USER — this is who 'you'/'your'/'their' means in your OUTPUT "
             "(ground truth, trust it over anything in the material below):"]
    if name:
        lines.append(f"- Name: {name}. Use exactly this spelling; do not expand, "
                     "translate, or invent variants of it.")
    if emails:
        # Verified failure 2026-08-19: a memory fact wrongly attributed one of
        # these addresses to the user's MOM, and stayed wrong for weeks because
        # nothing here said this list must never be reused as someone else's —
        # it only said what mail direction meant. Every future "email my X"
        # silently went to the user's own inbox instead of asking who X even is.
        lines.append(f"- Their own email address(es): {', '.join(emails)}. Mail "
                     "from these was SENT by the user; mail to them was RECEIVED "
                     "by the user. Any other address is someone else — and "
                     "these addresses belong to NO ONE ELSE: never save one of "
                     "them to memory as another person's contact detail, and "
                     "never suggest one as who to send TO when the user means "
                     "somebody besides themselves.")
    if with_date:
        lines.append(f"- Today's date: {time.strftime('%Y-%m-%d (%A)')}")
    return "\n".join(lines)


def attribution_rules(shape: str) -> str:
    """How to attribute a message line to a person, for ONE line shape.

    Every rule here is a defect that actually shipped, not a hypothetical:
    a sister's viral post reported as the user's, a four-person family group
    reported as a one-to-one chat with Mom, and the user's own outgoing
    messages reported back to them as things the recipient had said.

    `shape` is required, not defaulted, because two shapes are genuinely in
    use and a consumer must say which one it feeds:
      * "directed" — imessage_tools.render_for_summary's sender-first
        `[Sender -> Recipient] text` (the summarizers and the daily brief);
      * "verbatim" — the raw cached `conversation | Sender: text` that
        view_messages returns, where annotating the record would read as
        something a person actually typed.

    Only the matching rule is emitted. Describing BOTH shapes measurably hurt:
    with the two-shape text in front of it the `fast` model answered "who wrote
    the to-do list?" as "36726" (an unrelated shortcode two lines below) 2/2,
    where the shape-specific text got it right. Every extra paragraph competes
    for a 2.6B model's attention, so a rule that cannot fire is not free.
    """
    name = user_name()
    who = name or "the user"
    if shape not in ("directed", "verbatim"):
        raise ValueError(f"unknown line shape {shape!r}")
    if shape == "directed":
        lead = (
            "- Every line reads `[Sender -> Recipient] text`. THE NAME BEFORE "
            "THE ARROW IS THE SPEAKER, and the one after it is who they wrote "
            f"to. `[{who} (you) -> Mom] Hello` is {who} writing TO Mom — it is "
            "NOT something Mom said. Never flip an arrow.\n")
    else:
        lead = (
            "- Lines read `conversation | Sender: text`, where the part before "
            "the `|` is the CONVERSATION, not the speaker. A line whose sender "
            f"is exactly `Me` was written BY {who}, and a one-to-one chat is "
            "labelled with the OTHER person's name — so `Mom | Me: Hello` is "
            f"{who} writing to Mom, not Mom writing. Every sender other than "
            f"`Me` is a different person writing TO {who}.\n")
    return (
        "WHO SAID WHAT — attribute every line carefully; these conversations "
        "have several people in them.\n"
        + lead +
        "- Never describe something another person said as something the user "
        "said, or the reverse.\n"
        "- A `Group ...` label names the OTHER members of that group. "
        f"{who} is in every one of those groups too and is NEVER listed in the "
        "label. So 'Group of 3 (A, B, C)' is a FOUR-person conversation, and "
        "it stays a group even when only one member happens to have spoken "
        "recently — do not report it as a one-to-one chat with that member.\n"
        "- 'you' / 'your' / 'u' INSIDE a message someone else sent refers to "
        f"whoever THEY were addressing, which is often another member, not "
        f"{who}. When the message names or @-mentions a person "
        "('@Trishe - your post', 'nice work Sam'), everything in that message "
        f"is about THAT person. Attribute it to them, not to {who}, and say so "
        "plainly (e.g. 'Mom congratulated Trishe on her post').\n"
        "- Only treat a message as being about the user when it is a "
        "one-to-one chat with them, when it addresses them by name, or when "
        "nobody else is named in it. If you genuinely cannot tell who is meant, "
        "describe what was said without assigning it to anyone."
    )


def identity_prompt_block(*, messages: bool = True, with_date: bool = True,
                          shape: str = "directed") -> str:
    """The full identity preamble for a summarizer's system prompt.

    `messages=False` drops the message-line attribution rules for consumers
    (mail) whose material has no `Sender:` lines to misread. `shape` says which
    line shape this consumer feeds — see attribution_rules; it defaults to
    "directed" because the summarizers that go through render_for_summary are
    the common case, and consumers of the verbatim record (the agent loop,
    which relays view_messages output) pass "verbatim" explicitly.
    """
    parts = [p for p in (identity_block(with_date=with_date),
                         attribution_rules(shape) if messages else "") if p]
    return ("\n\n" + "\n\n".join(parts)) if parts else ""
