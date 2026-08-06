"""Who the user is — one definition, shared by everything that reads their data.

Wisp had ground-truth identity in exactly ONE place: the profile builder's
private `_identity_block` (see profile.py). Every other consumer of the user's
own mail and messages — the daily brief, `summarize_messages`, `summarize_emails`
— ran with no statement of who the user is at all. That is a correctness bug,
not a polish issue, because the raw material is a conversation between several
people and second-person pronouns in it belong to whoever was addressed:

    Group of 3 (Mom, dad, Trishe) | Mom: @Trishe - Your post has 439 likes

With no identity in the prompt the summarizer read "Your post" as the reader's
and reported the user's sister's milestone as the user's own. The message data
was correct at every layer — chat.db, handle resolution, contact names — the
prompt simply never said who "you" is.

So this module provides two blocks:

- `identity_block()`  — the facts: name, own email addresses, today's date.
- `attribution_rules()` — how to READ a message line given those facts: which
  sender label means the user, what a group label does and does not list, and
  the pronoun rule above.

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

    Deliberately name + addresses only. The saved-contact roster is a separate,
    much larger block (profile.contact_roster) that the profile builder wants
    and a summary prompt does not — 120 names would crowd out the messages the
    summarizer is supposed to be reading.
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
        lines.append(f"- Their own email address(es): {', '.join(emails)}. Mail "
                     "from these was SENT by the user; mail to them was RECEIVED "
                     "by the user. Any other address is someone else.")
    if with_date:
        lines.append(f"- Today's date: {time.strftime('%Y-%m-%d (%A)')}")
    return "\n".join(lines)


def attribution_rules() -> str:
    """How to attribute a message line to a person.

    Every rule here is a defect that actually shipped, not a hypothetical:
    a sister's viral post reported as the user's, and a four-person family
    group reported as a one-to-one chat with Mom.
    """
    name = user_name()
    who = name or "the user"
    return (
        "WHO SAID WHAT — message lines are `conversation | Sender: text`, and "
        "the conversation has several people in it. Attribute carefully:\n"
        f"- A line whose sender is exactly `Me` was written BY {who}. Every "
        "other sender is a DIFFERENT person writing TO them. Never describe "
        "something another person said as something the user said, or the "
        "reverse.\n"
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


def identity_prompt_block(*, messages: bool = True, with_date: bool = True) -> str:
    """The full identity preamble for a summarizer's system prompt.

    `messages=False` drops the message-line attribution rules for consumers
    (mail) whose material has no `Sender:` lines to misread.
    """
    parts = [p for p in (identity_block(with_date=with_date),
                         attribution_rules() if messages else "") if p]
    return ("\n\n" + "\n\n".join(parts)) if parts else ""
