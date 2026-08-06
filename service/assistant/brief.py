"""Daily brief composer — one combined summary of calendar + email + iMessage.

Produced by gpt-oss (the `general` role), NOT the small always-on gemma
summarizer: the brief's headline job is the EMAIL summary, which the user wants
done by the big model once a day at 8am (see build_daily_brief). It's a
once-daily (or on-demand button) task, so paying gpt-oss's load is worth the
quality — unlike the constant messages/calendar/notes lookups, which run on the
always-warm gemma. Powers both the on-demand "Daily Summary" button and the
scheduled 8am/8pm digest.

Calendar is deterministic (from the commitments store); email and messages come
from the caches the Swift MailReader/MessagesReader push. Everything degrades
gracefully: a source with no data (e.g. Messages before Full Disk Access is
granted) just says so in its own block instead of breaking the brief.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from service.assistant.store import assistant_store
from service.config import role_to_model
from service.inference.omlx_client import OMLXClient

_client: OMLXClient | None = None


def _c() -> OMLXClient:
    global _client
    if _client is None:
        _client = OMLXClient()
    return _client


def _calendar_block(now: float) -> str:
    """Calendar context, PARTITIONED into today vs. the rest of the week.

    The brief's headline line is "what's on today", but this block used to hand
    the model a flat 7-day list of bare "Thu Aug 6"-style dates and leave it to
    work out which of them was today. gemma doesn't reliably do that date
    arithmetic — it promotes a *future* event into "Today's Focus", which reads
    as a hallucinated appointment. get_upcoming already fixed this class of bug
    for chat (see assistant_tools._day_tag); the brief never got the same
    treatment. Now the split is computed in Python, every row carries its own
    TODAY/TOMORROW/weekday tag, and an empty today is stated explicitly rather
    than left as an absence the model can fill in.
    """
    # _fmt renders the same TODAY-anchored row the chat tools use — one
    # rendering of a commitment across every surface.
    from service.tools.assistant_tools import _fmt
    day_str = datetime.fromtimestamp(now).strftime("%A, %B %-d, %Y")
    events = assistant_store.upcoming(now=now, days=7)
    if not events:
        return (f"CALENDAR — TODAY IS {day_str}.\n"
                "Nothing scheduled today or in the next 7 days. Say the day is "
                "clear; do NOT invent an event.")
    today = datetime.fromtimestamp(now).date()
    show_account = len({c.get("account") for c in events if c.get("account")}) > 1
    today_rows, later_rows = [], []
    for c in events:
        bucket = today_rows if datetime.fromtimestamp(c["when_ts"]).date() == today \
            else later_rows
        bucket.append(_fmt(c, now, show_account=show_account))

    blocks = [f"CALENDAR — TODAY IS {day_str}."]
    if today_rows:
        blocks.append(f"ON THE CALENDAR TODAY ({len(today_rows)} item(s)) — these "
                      "and ONLY these are today's:\n" + "\n".join(today_rows))
    else:
        blocks.append("ON THE CALENDAR TODAY: nothing at all. The day is clear — "
                      "say so plainly, and do NOT describe anything below as "
                      "happening today.")
    if later_rows:
        blocks.append("LATER THIS WEEK (NOT today — never present these as "
                      "today's):\n" + "\n".join(later_rows))
    return "\n\n".join(blocks)


def _email_block() -> str:
    from service.tools.email_tools import _parse_lines  # cached headers
    rows = _parse_lines()
    if not rows:
        return "EMAIL: no inbox data (Mail automation may not be granted)."
    # last 24h of mail. Rows are (ts, account, sender, subject) — the account
    # field was added when Mail sync grew multi-account support and this
    # unpack was left at three, so every call raised ValueError and the Daily
    # Summary button failed 100% of the time with "Couldn't build a summary
    # right now." (the Swift side maps any error to that one string, which is
    # why it never looked like a crash).
    cutoff = time.time() - 24 * 3600
    recent = [(s, subj) for ts, _acct, s, subj in rows if ts >= cutoff] or \
             [(s, subj) for _ts, _acct, s, subj in rows[:20]]
    lines = [f"- {s} | {subj}" for s, subj in recent[:30]]
    return "EMAIL (recent inbox, sender | subject):\n" + "\n".join(lines)


def _messages_block() -> str:
    # render_for_summary, not a plain join: it tags each group message with the
    # member it is addressed to, so "@Trishe - How is the AI conference going?"
    # can't be read as the user's conference. See imessage_tools._addressee.
    from service.tools.imessage_tools import _parse_lines, render_for_summary
    rows = _parse_lines()
    if not rows:
        return "MESSAGES: no data (grant Wisp Full Disk Access to include this)."
    cutoff = time.time() - 24 * 3600
    recent = [r for r in rows if r[0] >= cutoff] or rows[:20]
    lines = [f"- {line}" for line in render_for_summary(recent[:30])]
    return "MESSAGES (recent iMessage/SMS, conversation | sender: text):\n" + "\n".join(lines)


_BRIEF_SYS = (
    "You are Wisp, the user's warm, caring personal assistant preparing their "
    "daily brief from their upcoming calendar, recent email, and recent messages.\n"
    "\n"
    "Produce THREE pieces of output, in this exact order, each preceded by its "
    "own marker line exactly as shown and nothing else on that line:\n"
    "\n"
    "===TODAY===\n"
    "A compact summary of the calendar + email, written for a push notification: "
    "PLAIN TEXT, no markdown, no asterisks, no emoji section headers, 2-4 short "
    "lines. Lead with the day's shape (how many meetings, what's next) then "
    "anything in email that's time-sensitive or needs a reply. Skip "
    "promotional/newsletter mail. If there's genuinely nothing, say so in one "
    "short line.\n"
    "\n"
    "===MESSAGES===\n"
    "A compact summary of recent messages, written for a push notification: "
    "PLAIN TEXT, no markdown, 1-3 short lines — who reached out and the gist of "
    "what they need, if anything. If there's nothing meaningful, say so in one "
    "short line (e.g. 'Nothing new since last time.').\n"
    "\n"
    "===FULL===\n"
    "The full brief for in-app reading — the kind a thoughtful friend would hand "
    "you with your coffee. FORMAT it to be genuinely pleasant and easy to scan — "
    "NOT a wall of text:\n"
    "- Open with a warm one- or two-line greeting that sets the tone for the day "
    "(include a fitting emoji, e.g. ☀️ / 👋).\n"
    "- Then a few short sections, each with a bold header that STARTS with a "
    "relevant emoji — e.g. '**📅 Today**', '**📧 Worth a look**', '**💬 Messages**'.\n"
    "- Inside a section, when there are multiple distinct items (several emails, a "
    "few jobs, multiple events), use a SHORT BULLET LIST ('- '), one line each, "
    "with the key thing **bolded** — don't cram them into one long paragraph. When "
    "it's just one thing, a short sentence or two is perfect.\n"
    "- Close with a brief, caring sign-off and an offer to help (e.g. draft a "
    "reply, dig into one of these).\n"
    "\n"
    "VOICE (applies to all three pieces): warm, human, and caring — like a person "
    "who's genuinely looking out for them. Stay completely grounded in what's "
    "actually in their calendar, inbox, and messages — never invent events, "
    "people, or details.\n"
    "\n"
    "GROUNDING RULE FOR PEOPLE: the identity block above says who the user is "
    "and how to tell their messages from everyone else's. A brief is written in "
    "the second person, so every 'you' you write is a claim about the user "
    "specifically — only make it when the material actually supports it. "
    "Someone else's news in a group chat is THEIR news; name them.\n"
    "\n"
    "GROUNDING RULE FOR DATES: the calendar section states today's date and "
    "splits the items into what is ON THE CALENDAR TODAY and what is LATER THIS "
    "WEEK, and tags every row TODAY / TOMORROW / a weekday. Use those tags "
    "verbatim — never work out a date yourself. Only rows under ON THE CALENDAR "
    "TODAY may be described as today's. If that section says nothing is on "
    "today, say the day is clear; an empty day is a good thing to report, not a "
    "gap to fill with something from later in the week."
)

# Markers the model is asked to emit; splits its single response into the
# three pieces above. If the model drops a marker (small local models
# occasionally do), that section falls back to empty and callers degrade
# gracefully rather than showing garbled output.
_MARKERS = ("TODAY", "MESSAGES", "FULL")


def _split_brief(raw: str, fallback: str) -> dict[str, str]:
    pattern = "|".join(_MARKERS)
    parts = re.split(rf"===({pattern})===\s*", raw)
    sections = {m: t.strip() for m, t in zip(parts[1::2], parts[2::2])}
    if not sections.get("FULL"):
        sections["FULL"] = raw.strip() or fallback
    return sections


async def _generate_brief(part_of_day: str) -> dict[str, str]:
    now = time.time()
    greeting = "Good morning" if part_of_day == "morning" else "Good evening"
    context = (f"{greeting}. Today is {datetime.now().strftime('%A, %B %-d')}.\n\n"
               + _calendar_block(now) + "\n\n" + _email_block() + "\n\n" + _messages_block())
    c = _c()
    # gemma (the always-warm summarizer), NOT gpt-oss. This brief is generated
    # on a schedule (8am/8pm) and on demand from the Daily Summary button;
    # running it on gpt-oss cold-loaded the 12.7GB model — a swap every morning
    # for a background task — against the whole "keep gpt-oss asleep, summarize
    # on the resident gemma" design the rest of the assistant layer follows
    # (calendar/email/messages summaries all run on gemma). The brief is the
    # same kind of work — synthesize already-fetched calendar/email/message
    # context into prose — just over more sources at once; gemma handles it at
    # the (recently loosened) summary quality without any swap.
    model = role_to_model("fast")
    await c.ensure_only(model)
    # Identity FIRST, before the formatting instructions: the brief is written
    # in the second person, so "who is 'you'" has to be settled before the model
    # starts deciding whose news goes in it. Without this block the brief
    # reported a family member's viral post — "@Trishe - Your post has 439
    # likes", sent by Mom to a four-person family group — as the user's own.
    from service.memory.identity import identity_prompt_block
    from service.memory.profile import profile_context_block
    system = identity_prompt_block().strip() + "\n\n" + _BRIEF_SYS + profile_context_block(1200)
    resp = await c.chat(
        model,
        [{"role": "system", "content": system.strip()},
         {"role": "user", "content": context}],
        temperature=0.4, max_tokens=2000)
    raw = (resp["choices"][0]["message"].get("content") or "").strip()
    return _split_brief(raw, fallback=context)


async def build_daily_brief(part_of_day: str = "morning") -> str:
    sections = await _generate_brief(part_of_day)
    return sections["FULL"]


async def run_scheduled_brief(part_of_day: str) -> None:
    """Scheduler entry: compose the brief and push it to the app as both a
    first-class 'brief' event (so the panel can show the full text) and the
    payload for two notification cards — one for calendar+email, one for
    messages — instead of one generic 'summary is ready' ping."""
    sections = await _generate_brief(part_of_day)
    if not sections.get("FULL"):
        return
    from service.assistant.hub import hub
    await hub.publish({
        "type": "daily_brief",
        "part_of_day": part_of_day,
        "text": sections["FULL"],
        "today_summary": sections.get("TODAY", ""),
        "messages_summary": sections.get("MESSAGES", ""),
    })
