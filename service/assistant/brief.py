"""Daily brief composer — one combined summary of calendar + email + iMessage.

Powers both the on-demand "Daily Summary" button and the scheduled 8am/8pm
digest.

NO MODEL. The brief is rendered in Python from the same caches the chat tools
read — see `_render_brief` and the comment above it, which records what the
generative version cost and why each layer of it came off:

  * a model-written brief misattributed the user's own outgoing messages
    (_messages_rundown's measurements), so the sections became grounded
    excerpts;
  * the excerpt version then pasted the SOURCE TOOLS' return values together,
    and those are written for a model — the user's brief opened three of its
    four sections with prompt scaffolding (reported 2026-09-08);
  * so the sections are composed here, in the shape a person reads, and the
    model-facing renderings stay where they belong: in the tool output a model
    consumes.

The prompt-and-two-passes machinery below (`_BRIEF_SYS`, `_MSG_SYS`,
`_messages_rundown`, `_split_brief`, `_assemble_full`, the `_*_block` builders)
is no longer on any live path; it is kept for the regression tests that pin what
each of its failures looked like.

Calendar is deterministic (from the commitments store); email and messages come
from the caches the Swift MailReader/MessagesReader push. Everything degrades
gracefully: a source with no data (e.g. Messages before Full Disk Access is
granted) just says so in its own section instead of breaking the brief, and a
source that has not completed its current-launch read holds the whole brief back
rather than passing restored rows off as today's (see `_sections`).
"""
from __future__ import annotations

import re
import time
import traceback
from datetime import datetime, timedelta

from service.config import no_thinking_kwargs, role_to_model
from service.inference.omlx_client import OMLXClient
from service.assistant.store import assistant_store

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
    work out which of them was today. the summarizer doesn't reliably do that date
    arithmetic — it promotes a *future* event into "Today's Focus", which reads
    as a hallucinated appointment. get_upcoming already fixed this class of bug
    for chat (see assistant_tools._day_tag); the brief never got the same
    treatment. Now the split is computed in Python, every row carries its own
    TODAY/TOMORROW/weekday tag, and an empty today is stated explicitly rather
    than left as an absence the model can fill in.
    """
    from service.assistant.sync_status import source_status
    states = [source_status(source) for source in ("calendar", "reminders")]
    if any(source["state"] == "syncing" for source in states):
        return "CALENDAR: Wisp is still syncing calendar/reminder data. The schedule cannot yet be confirmed."
    unavailable = [source["label"] for source in states if source["state"] == "unavailable"]
    unavailable_ids = {source["id"] for source in states if source["state"] == "unavailable"}
    unavailable_note = ("\nSOURCE UNAVAILABLE: " + " and ".join(unavailable)
                        + ". Say this source could not be checked; never call "
                          "its missing items an empty schedule.") if unavailable else ""
    # _fmt renders the same TODAY-anchored row the chat tools use — one
    # rendering of a commitment across every surface.
    from service.tools.assistant_tools import _fmt, _without_holiday_calendars
    day_str = datetime.fromtimestamp(now).strftime("%A, %B %-d, %Y")
    events = [event for event in assistant_store.upcoming(now=now, days=7)
              if event.get("source") not in unavailable_ids]
    events = _without_holiday_calendars(events, include_holidays=False)
    if not events:
        if unavailable:
            return (f"CALENDAR — TODAY IS {day_str}.{unavailable_note}\n"
                    "No scheduled items in sources that could be checked. The full schedule is unknown.")
        return (f"CALENDAR — TODAY IS {day_str}.{unavailable_note}\n"
                "Nothing scheduled today or in the next 7 days. Say the day is "
                "clear; do NOT invent an event.")
    today = datetime.fromtimestamp(now).date()
    show_account = len({c.get("account") for c in events if c.get("account")}) > 1
    today_rows, later_rows = [], []
    for c in events:
        bucket = today_rows if datetime.fromtimestamp(c["when_ts"]).date() == today \
            else later_rows
        bucket.append(_fmt(c, now, show_account=show_account))

    blocks = [f"CALENDAR — TODAY IS {day_str}.{unavailable_note}"]
    if today_rows:
        blocks.append(f"ON THE CALENDAR TODAY ({len(today_rows)} item(s)) — these "
                      "and ONLY these are today's:\n" + "\n".join(today_rows))
    else:
        blocks.append("ON THE CALENDAR TODAY: no items in the sources that could be checked. "
                      + ("The full schedule is unknown." if unavailable else "The day is clear.")
                      + " Do NOT describe anything below as happening today.")
    if later_rows:
        blocks.append("LATER THIS WEEK (NOT today — never present these as "
                      "today's):\n" + "\n".join(later_rows))
    return "\n\n".join(blocks)


# Mail is capped separately from messages: an inbox is mostly machine mail, and
# the two lists below are budgeted so a busy newsletter day can't crowd out the
# handful of human emails that are the actual point of the section.
_MAX_HUMAN_EMAILS = 20
_MAX_MACHINE_SENDERS = 12


def _mail_rows(now: float) -> list[dict]:
    """Recent inbox headers, newest first — last 24h, or the newest few if the
    day was quiet, so an early-morning brief isn't empty just because the window
    happens to start after the last delivery.

    Goes through email_tools.header_rows (dicts) rather than _parse_lines
    (tuples) DELIBERATELY. This module unpacked that tuple positionally twice
    and broke both times a field was added to it — most recently `unread`, which
    made every Daily Summary press raise ValueError and reach the user as
    "Couldn't build a summary right now." (the Swift side maps any failure to
    that one string, which is why it never looked like a crash). Read fields by
    name here and a sixth field is a no-op for the brief.
    """
    from service.tools.email_tools import email_sync_state, header_rows
    # Never let restored pre-launch rows masquerade as a current Daily Summary.
    # _sections requests a live sync first; if it has not landed, the user gets
    # the explicit sync notice there rather than stale mail here.
    if email_sync_state() != "ready":
        return []
    # A lower bound alone is not a time window. The live cache in the
    # 2026-08-28 report contained three future-dated rows (2027/2030); all are
    # >= "24 hours ago", so they entered the brief and crowded out current
    # mail. Cap at now before choosing either the 24-hour set or fallback.
    recent = [r for r in header_rows(since_ts=now - 24 * 3600)
              if r["ts"] <= now]
    if recent:
        return recent
    return [r for r in header_rows() if r["ts"] <= now][:20]


def _email_block(now: float) -> str:
    """Inbox context, filtered to substantive mail before the model sees it.

    Same principle as _calendar_block: decide in Python, hand the model labeled
    blocks. _BRIEF_SYS used to carry a bare "Skip promotional/newsletter mail"
    instruction and leave the judgment to the model, which is a classification
    job it does inconsistently at this size — it dropped a real recruiter reply
    as promotional, and elsewhere gave a LinkedIn digest its own bullet. The
    machine/human split is already a solved, deterministic question here
    (email_tools.is_machine_sender), so it is answered before the prompt is
    built and the model is left with the part it is actually good at: writing.

    OTPs, marketing, routine newsletters, and duplicate headers are removed
    before synthesis. Important automated notices survive that filter.
    """
    from service.tools.email_tools import (
        email_sync_state, email_freshness_warning, filter_summary_rows,
        is_machine_sender)
    state = email_sync_state()
    if state == "syncing":
        return ("EMAIL: Wisp is still syncing mail after launch. Tell the user "
                "the email portion is not ready yet; do NOT claim there were "
                "no emails today and do NOT reuse older cached messages.")
    if state == "unavailable":
        return ("EMAIL: Wisp could not complete the live Mail sync. Say the "
                "email portion is unavailable right now; do NOT reuse older "
                "cached messages or claim there were no emails today.")
    rows = filter_summary_rows([
        (r["ts"], r["account"], r["sender"], r["subject"], r["unread"])
        for r in _mail_rows(now)
    ])
    rows = [{"ts": ts, "account": account, "sender": sender,
             "subject": subject, "unread": unread}
            for ts, account, sender, subject, unread in rows]
    warning = email_freshness_warning()
    if not rows:
        return ("EMAIL: the completed Mail read returned no matching messages. "
                + warning)

    show_account = len({r["account"] for r in rows if r["account"]}) > 1
    human, machine = [], []
    for r in rows:
        (machine if is_machine_sender(r["sender"]) else human).append(r)

    def _row(r: dict) -> str:
        # A leading • is the unread marker. `unread` is None on cache lines
        # written before Mail sync collected read status, and None means
        # UNKNOWN, not read (see email_tools._parse_pipe_lines) — so an unmarked
        # row is stated as "not marked" below rather than asserted as read.
        tag = f"[{r['account']}] " if show_account and r["account"] else ""
        return f"- {'• ' if r['unread'] else ''}{tag}{r['sender']} — {r['subject']}"

    blocks = ["EMAIL — recent inbox."]
    if warning:
        blocks.append(warning + " This is only the available local snapshot, not all mail today.")
    if human:
        unread = sum(1 for r in human if r["unread"])
        blocks.append(
            f"FROM PEOPLE ({len(human)} email(s), {unread} unread). These "
            "are the ones worth writing about. A leading • means unread; no • "
            "means it is read or its status is unknown, so never describe an "
            "unmarked email as unread:\n"
            + "\n".join(_row(r) for r in human[:_MAX_HUMAN_EMAILS]))
    else:
        blocks.append("FROM PEOPLE: none in the available snapshot. Do not claim "
                      "that no other messages could have arrived.")
    if machine:
        senders, seen = [], set()
        for r in machine:
            key = r["sender"].strip().lower()
            if key not in seen:
                seen.add(key)
                senders.append(r["sender"].strip())
        blocks.append(
            f"IMPORTANT AUTOMATED NOTICES ({len(machine)} email(s) from: "
            f"{', '.join(senders[:_MAX_MACHINE_SENDERS])}). These have already "
            "passed the noise filter; mention one only when it affects the user's "
            "day, and never give it its own section.")
    return "\n\n".join(blocks)


# How many of the most-recently-active conversations get a slot in the brief,
# and how many of that conversation's own lines it gets to bring. Both exist
# for the same reason: _messages_block used to hand the model 30 raw lines
# taken newest-first across ALL conversations combined, unsorted by thread. A
# single busy group chat (e.g. a family group) fills most or all of that
# window on an active day, so a genuinely separate 1:1 conversation could be
# down to its actual 2 real lines while sitting in a prompt that looks, in
# aggregate, like there's plenty of material to work with. Asked for "one line
# per conversation" against that backdrop, the model reached past what was
# there and invented a second topic for the thin thread — seen live
# 2026-08-08: a 2-line exchange that was only "measurements: 21x15x12" /
# "got it thanks" came back as also having "asked about a code snippet you
# shared", which appears nowhere in the actual messages. Grouping by
# conversation FIRST (deterministic, in Python) means the model is handed
# "Trishe: 2 lines" as a visibly small, complete block instead of 2 lines
# lost inside a 30-line multi-thread stream — it has no interleaved material
# left to misread as belonging to Trishe, and nothing to pad with.
_MAX_CONVERSATIONS = 6
_MAX_LINES_PER_CONVERSATION = 8


def _messages_block() -> str:
    # render_for_summary, not a plain join: it tags each group message with the
    # member it is addressed to, so "@Trishe - How is the AI conference going?"
    # can't be read as the user's conference. See imessage_tools._addressee.
    from service.tools.imessage_tools import (
        _parse_lines, filter_summary_message_rows, messages_sync_state,
        render_for_summary)
    if messages_sync_state() == "syncing":
        return "MESSAGES: Wisp is still syncing messages after launch."
    if messages_sync_state() == "unavailable":
        return "MESSAGES: unavailable in this launch."
    rows = filter_summary_message_rows(_parse_lines())
    if not rows:
        return "MESSAGES: no recent messages."
    cutoff = time.time() - 24 * 3600
    recent = [r for r in rows if r[0] >= cutoff] or rows[:40]
    # _parse_lines returns newest-first, so grouping in this order and taking
    # the first _MAX_CONVERSATIONS distinct contexts naturally picks the most
    # recently active conversations — not just the most recent raw lines,
    # which would silently favor whichever single thread is busiest.
    groups: dict[str, list[tuple[float, str, str]]] = {}
    order: list[str] = []
    for r in recent:
        ctx = r[1]
        if ctx not in groups:
            if len(order) >= _MAX_CONVERSATIONS:
                continue
            groups[ctx] = []
            order.append(ctx)
        if ctx in groups and len(groups[ctx]) < _MAX_LINES_PER_CONVERSATION:
            groups[ctx].append(r)
    blocks = []
    for ctx in order:
        group_rows = sorted(groups[ctx], key=lambda r: r[0])  # chronological within the thread
        rendered = render_for_summary(group_rows)
        blocks.append(f"CONVERSATION {len(blocks) + 1}: {ctx} "
                      f"({len(rendered)} message(s)):\n"
                      + "\n".join(f"- {line}" for line in rendered))
    # The arrow rule is restated HERE, touching the data, even though
    # identity.attribution_rules already states it in the system prompt. In the
    # brief that rule is ~6k characters up, behind _BRIEF_SYS and the profile
    # block, and it loses: measured 2026-08-07, 6/6 briefs still reported "Mom
    # sent a warm hello" and "Chetan shared his to-do list" for messages the
    # USER sent to them — even with the renderer already emitting arrows, which
    # on its own fixed the standalone message summarizer 12/12. Proximity is
    # doing real work; the shorter message-summary prompt doesn't need it.
    from service.memory.identity import user_name
    me = f"{user_name()} (you)" if user_name() else "you (the user)"
    return ("MESSAGES (recent iMessage/SMS), GROUPED BY CONVERSATION — each "
            "block below is one conversation, already separated out for you. "
            "Each line is `[Sender -> Recipient] text` — the name BEFORE the "
            f"arrow WROTE it, the name after it RECEIVED it. A line starting "
            f"`[{me} ->` is the user's OWN outgoing message: never credit it "
            "to the person it was sent to.\n\n" + "\n\n".join(blocks))


# Rebuilt 2026-08-14. The previous version described the output in prose and
# left the model to infer the shape from it; this one SHOWS the shape as a
# skeleton and states the rules as a flat checklist. Two reasons:
#
#  1. The role moved. Every measurement in this file's older comments was taken
#     on LFM2.5-2.6B, and the `fast` role is now Agents-A1-4B (qwen3 lineage) —
#     a model that follows a literal template far more reliably than it follows
#     a paragraph about one, and that does not need instructions repeated three
#     ways to comply.
#  2. Prose rules hide their own violations. "Skip promotional mail" and "use
#     bullets when there are several items" were both conditionals the model had
#     to evaluate mid-write. The promotional one is now decided in Python before
#     the prompt exists (see _email_block), and what's left is stated as rules
#     that are true or false about the finished text, not judgment calls.
#
# What did NOT change: the grounding rules. Those earn their length — each one
# is a specific hallucination that was observed live, not general caution.
_BRIEF_SYS = (
    "You are Wisp, the user's personal assistant, writing their daily brief "
    "from their calendar and inbox. Write like a thoughtful friend handing them "
    "the day over coffee: warm, specific, and short.\n"
    "\n"
    "Output EXACTLY these two marked sections, in this order. Each marker sits "
    "alone on its own line, written exactly as shown. Write nothing before the "
    "first marker and nothing after the last section.\n"
    "\n"
    "===TODAY===\n"
    "<2-4 short lines, PLAIN TEXT, for a phone notification>\n"
    "\n"
    "===FULL===\n"
    "<a warm 1-2 line greeting with one emoji>\n"
    "\n"
    "**📅 Today**\n"
    "<what is on today, or that the day is clear>\n"
    "\n"
    "**📧 Worth a look**\n"
    "<the email that actually matters>\n"
    "\n"
    "**🔭 Coming up**\n"
    "<OPTIONAL, and only for items the material lists under LATER THIS WEEK. "
    "Skip this section entirely when today already has plenty on it.>\n"
    "\n"
    # Angle-bracket slots, never a worked example. A version of the sibling
    # prompt below shipped with realistic sample content and the model copied it
    # into the answer as if it were the user's data — see the note in _MSG_SYS.
    # Nothing in this prompt may be mistakable for material.
    "Each **bold header** goes on its own line, with its text on the line "
    "BELOW it — never run a header and its text together on one line.\n"
    "\n"
    "IMPORTANT rules for ===TODAY===:\n"
    "- Plain text ONLY: no markdown, no asterisks, no bullets, no section "
    "headers, no emoji.\n"
    "- Line one is the shape of the day: how many things are on, and what the "
    "next one is.\n"
    "- Then anything in the inbox that is time-sensitive or waiting on a reply.\n"
    "- Nothing going on? Say that in one line. Do not pad it.\n"
    "\n"
    "IMPORTANT rules for ===FULL===:\n"
    "- The skeleton's headers are the ONLY headers you may write, spelled "
    "exactly as shown. Never invent a section of your own — not for automated "
    "mail, not for anything else. Two or three sections, that is the whole "
    "brief.\n"
    "- Two or more items in a section: one '- ' bullet each, one line each, the "
    "key thing in **bold**. A single item: one or two sentences, no bullet.\n"
    "- Say what an item actually IS, not that it exists: what the sender wants "
    "and what it means for the user, not '<sender> — email received'.\n"
    "- The material marks unread mail with a leading •. That bullet is a label "
    "for you, not text: never copy a • into the brief. Say 'unread' in words if "
    "it matters.\n"
    "- Keep it scannable. A section is at most a few lines; the whole brief is "
    "well under a screen.\n"
    "\n"
    "IMPORTANT — never do these:\n"
    "- Never write a messages, texts, or iMessage section, and never mention "
    "their texts at all. A separate, already-written messages summary is "
    "attached directly after your text; anything you add about messages "
    "duplicates it with worse attribution.\n"
    "- Never write a closing line, sign-off, or 'let me know if...' offer. Your "
    "final line is the end of the email section. The closing is added after the "
    "messages summary.\n"
    "- Never repeat the material back verbatim, and never copy a label, header, "
    "or instruction out of the material into the brief.\n"
    "- Never write about anything that is not in the material below. No invented "
    "events, people, subject lines, deadlines, or weather.\n"
    "\n"
    "GROUNDING RULE FOR PEOPLE: the identity block above says who the user is "
    "and how to tell their own mail from everyone else's. A brief is written in "
    "the second person, so every 'you' you write is a claim about the user "
    "specifically — only make it when the material actually supports it.\n"
    "\n"
    "GROUNDING RULE FOR DATES: the calendar section states today's date and "
    "splits the items into ON THE CALENDAR TODAY and LATER THIS WEEK, and tags "
    "every row TODAY / TOMORROW / a weekday. Use those tags verbatim — never "
    "work out a date yourself. Only rows under ON THE CALENDAR TODAY may be "
    "described as today's. If that section says nothing is on today, say the "
    "day is clear; an empty day is a good thing to report, not a gap to fill "
    "with something from later in the week.\n"
    "\n"
    "GROUNDING RULE FOR EMAIL: the inbox section is already filtered to "
    "meaningful mail from people and important automated notices. OTPs, "
    "marketing, newsletters, and repeated notifications are absent on purpose; "
    "never mention their absence or invent them."
)

# Markers the model is asked to emit; splits its single response into the
# three pieces above. If the model drops a marker (small local models
# occasionally do), that section falls back to empty and callers degrade
# gracefully rather than showing garbled output.
_MARKERS = ("TODAY", "MESSAGES", "FULL")


# Stage one of two is the only writer of the brief's Messages prose; stage two
# never sees the raw text (see _generate_brief).  It deliberately asks for a
# digest across the threads rather than one formatted entry per thread: the
# latter looked like a transcript in the daily brief even when every entry was
# individually accurate.
_MSG_SYS = (
    "You are Wisp, the user's personal assistant, catching them up on their "
    "recent texts for their daily brief — the way a close friend who scrolled "
    "through their messages would fill them in, not a machine report.\n"
    "\n"
    "Write ONE short integrated digest, two to four natural sentences in one "
    "paragraph. Combine related threads into the few themes, decisions, plans, "
    "or open questions that matter. Lead with anything needing the user's reply "
    "or attention. It is fine to omit a trivial acknowledgement or automated "
    "alert.\n"
    "\n"
    "This is a summary, NOT a conversation-by-conversation recap: do not list "
    "the conversations, messages, senders, or timestamps; do not write a "
    "header, bullets, numbering, quotes, or a sign-off. Do not restate messages "
    "one at a time or copy their wording.\n"
    "\n"
    "IMPORTANT rules:\n"
    "- Synthesize only what the material supports. Do not invent a plan, "
    "deadline, feeling, person, or detail.\n"
    "- Keep each conversation's facts in its own block; do not transfer a topic "
    "or detail between blocks.\n"
    "- Clearly say who did what. A line is `[Sender -> Recipient] text`: the "
    "name before the arrow wrote it. If the user sent it, write 'you sent' or "
    "'you asked', never as if the recipient said it.\n"
    "- Write directly to the user: call them 'you', never their own name.\n"
    "- Start with the digest and stop after it."
    "\n"
    "GETTING THE DIRECTION RIGHT IS THE WHOLE JOB. Every line says who WROTE it "
    "and who RECEIVED it: `[Sender -> Recipient] text`. A message the user sent "
    "is something THEY said — write 'you asked...', 'you sent...', never as "
    "though the recipient said it.\n"
    "\n"
    "WRITE TO THE USER, NOT ABOUT THEM. You are talking to them directly, so "
    "they are always 'you' and never their own name. The material spells their "
    "name out on every line they wrote so you can tell it is them — that is a "
    "label to read, not a name to use. 'You asked if you could bring a friend', "
    "never '<their name> asked if they could bring a friend'.\n"
    "\n"
    "STAY INSIDE THE BLOCK. Summarize each conversation using ONLY the lines in "
    "its own block. Never pull a topic, question, or detail across from another "
    "block, and never add one that appears in no block at all. A block with one "
    "or two short lines gets a one-clause summary of exactly those lines — a "
    "short exchange is a complete thing to report, not a gap to fill in to "
    "sound more substantial. If a block is genuinely just 'ok' or 'thanks', say "
    "that plainly rather than inventing what it replied to.\n"
    "\n"
    "The `[Name -> Name]` markers are routing labels for you to READ, not text "
    "to repeat. Never copy one into your answer — write plain prose about who "
    "did what.\n"
    "\n"
    # Ported from imessage_tools._SYS 2026-08-18. That rule was added for the
    # standalone summarize_messages tool and measurably helped there — but the
    # brief runs its OWN prompt over the SAME rendered lines, and never got it.
    # Measured here before porting, 10 runs of _messages_rundown against the
    # live Mom block: 4/10 said "Mom sent a stock update", 0/10 got it right.
    "A GREETING NAMES WHO IT'S FOR, NOT WHO'S SPEAKING. A line like "
    "'[Adi Jain (you) -> Mom] Hi Mom! Here are today's stock updates...' is "
    "written BY the name before the arrow, TO the name after it — the "
    "greeting 'Hi Mom' does not change that, no matter how naturally it reads "
    "as something Mom would say. Measured failure: this exact line was "
    "summarized as 'Mom sent a stock update' — flipping an outgoing message "
    "from the user INTO Mom, because its own opening words happened to name "
    "her. The arrow, not the wording of the message, is what tells you who "
    "sent it."
)

# Emoji for an assembled header. Deliberately only two cases: a group and
# everything else. The old prompt offered 👩/👨/🧑 for one-to-one chats, which
# asked the model to guess a stranger's gender off their name — 👤 is the
# honest version and it costs nothing.
_GROUP_EMOJI = "👥"
_PERSON_EMOJI = "👤"

# "1. text" / "1) text" / "[1] text" — the model's entry number. Tolerant on
# purpose: which of those it picks varies run to run, and the number is the
# only part that has to be read correctly.
_ENTRY_RE = re.compile(r"^\s*[\[(]?(\d{1,2})[\])]?[.):]?\s+(.*)$")


def _assemble_sections(raw: str, names: list[str]) -> str:
    """The model's numbered bodies + Python-built headers -> the rundown.

    Pairs each entry with names[n-1]. An entry whose number doesn't match a
    real block is DROPPED rather than guessed at: a body with no conversation
    behind it is exactly the "summarized a conversation that isn't there"
    failure the numbering exists to prevent. Two entries claiming the same
    number keep the first, which is what _clean_message_sections did for
    duplicate headers.

    Falls back to `raw` when nothing parses, so a run that ignores the format
    still shows the user something rather than a blank Messages section.
    """
    if not names:
        return raw.strip()
    bodies: dict[int, list[str]] = {}
    current: int | None = None
    for line in raw.split("\n"):
        m = _ENTRY_RE.match(line)
        if m:
            current = int(m.group(1))
            if current in bodies:      # duplicate number — keep the first
                current = None
                continue
            bodies[current] = [m.group(2).strip()]
        elif current is not None and line.strip():
            bodies[current].append(line.strip())
    if not bodies:
        return raw.strip()
    out = []
    for n in sorted(bodies):
        if not 1 <= n <= len(names):
            continue
        body = " ".join(x for x in bodies[n] if x).strip()
        if not body:
            continue
        name = names[n - 1]
        # A bare arrow fragment at the head of the body — "-> Mom You sent a
        # plain morning check-in…". _ROUTING_ECHO only matches a full
        # `[A -> B]` marker, so a half-copied one survives it. Seen live
        # 2026-08-18, roughly one run in ten. Only stripped when what follows
        # the arrow is this block's OWN name, which is the only shape it takes.
        body = re.sub(rf"^(?:->|→)\s*{re.escape(name)}\s*", "", body).strip()
        if not body:
            continue
        emoji = _GROUP_EMOJI if name.strip().lower().startswith("group") else _PERSON_EMOJI
        out.append(f"**{emoji} {name}**\n{body}")
    # Anything the model flagged as waiting on a reply goes first — the one
    # ordering decision worth keeping from the old prompt, now applied to the
    # assembled sections instead of asked for.
    urgent = [x for x in out if "⏰" in x or "needs a reply" in x.lower()]
    rest = [x for x in out if x not in urgent]
    return "\n\n".join(urgent + rest).strip() or raw.strip()


# A `[Someone -> Someone]` marker at the head of an output line. The prompt
# tells the model not to echo these; this is the guarantee it didn't. Seen live
# 2026-08-07: one run in six emitted the rendered lines back nearly verbatim,
# markers and all, which reaches the user as visible plumbing.
_ROUTING_ECHO = re.compile(r"^(\s*[-*]?\s*)\[[^\]\n]{1,120}->[^\]\n]{1,120}\]\s*",
                           re.MULTILINE)


# The unread dot _email_block puts in front of a row. Same category of problem
# as _ROUTING_ECHO: a marker written for the model to READ that it sometimes
# copies into the answer, where it reaches the user as "• Arnav M wants to
# connect" — a stray glyph in the middle of prose, or a bullet character the
# renderer doesn't treat as a list. Seen 2026-08-14, one run in three, with the
# rule against it already in _BRIEF_SYS.
#
# Rewritten to "- " rather than deleted: at the head of a line the model may
# well have meant it as a bullet, and normalizing gives a real markdown list
# either way. Mid-sentence dots are left alone — a • there is the model's own
# punctuation, not an echo of ours.
_UNREAD_DOT = re.compile(r"^([ \t]*)(?:[-*][ \t]*)?•[ \t]+", re.MULTILINE)

# Two closely-related failures in the skeleton's bracketed placeholder slots
# (e.g. "<a warm 1-2 line greeting with one emoji>"), both caught by looking
# at what's INSIDE the brackets rather than assuming "brackets around short
# text at the top of a section = the model filled the slot in and forgot to
# unwrap it":
#
# 1. The model fills the slot in but keeps the angle brackets, e.g.
#    "<Good afternoon ☀️>" instead of "Good afternoon ☀️" — real content, just
#    needs unwrapping.
# 2. The model never fills the slot in and instead echoes the INSTRUCTION that
#    describes it. Seen live 2026-08-15: one run's ===FULL=== opened with
#    "<2-4 short lines, PLAIN TEXT, for a phone notification>" — the
#    ===TODAY=== placeholder's text, copied verbatim into the wrong slot —
#    and a second run put TWO such lines back to back: that same copied
#    ===TODAY=== placeholder, then "<3 line greeting with one emoji>", a
#    paraphrase of the real ===FULL=== placeholder rather than a fill-in.
#    Reproduced against the live engine (scripted repeats of the stage-two
#    call, busy synthetic calendar/email/messages to lengthen the prompt):
#    2/20 runs. Debracketing case 2 the same way as case 1 is exactly the old
#    bug — it made the brackets invisible and left the raw instruction text
#    sitting at the top of the user's brief.
#
# The two are told apart by content: a real greeting never describes its own
# line count or spells out the word "emoji" (it just uses one), so those
# markers are treated as proof of a leaked instruction rather than content.
# Words that only ever appear inside a PROMPT slot, never inside real content.
# "conversation name" / "next conversation" joined the list 2026-08-18, when
# _MSG_SYS still asked the model to write its own section headers and it copied
# the header skeleton through verbatim. That prompt no longer exists — headers
# are built in Python now (see _assemble_sections) — so these two are belt and
# braces rather than the live fix they started as. Kept deliberately narrow:
# each entry has to be something the user's own calendar, mail or texts would
# never say, because anything looser starts deleting real content.
_LEAKED_PLACEHOLDER_MARKERS = re.compile(
    r"\bemoji\b|\bplain text\b|\bphone notification\b|"
    r"\bconversation name\b|\bnext conversation\b|"
    r"\d+(?:-\d+)?\s*(?:short\s+)?lines?\b",
    re.IGNORECASE)

# One bracketed line at the very start of the text — anchored to `\A`, one
# line, no nested brackets. Deliberately does NOT eat the newline(s) after the
# `>`: a kept (real-content) bracket needs that blank line preserved between
# the greeting and the section that follows it, and only a dropped
# (leaked-instruction) bracket should also take its trailing blank line with it
# — handled explicitly below rather than baked into the regex.
_LEADING_BRACKET = re.compile(r"\A[ \t]*<([^\n<>]{1,100})>[ \t]*")


def _strip_leading_placeholder_leaks(text: str) -> str:
    """Drop consecutive leaked-instruction brackets at the start of a section,
    then unwrap one real bracketed greeting if what's left starts with one.
    Loops rather than matching once because the live failure produced two
    leaked lines back to back — stopping after the first left the second
    sitting in the output."""
    while True:
        m = _LEADING_BRACKET.match(text)
        if not m:
            return text
        if _LEAKED_PLACEHOLDER_MARKERS.search(m.group(1)):
            text = text[m.end():].lstrip("\n")
            continue
        return m.group(1) + text[m.end():]


# Same leak as _LEADING_BRACKET/case 2 above, but NOT anchored to `\A` — for
# when the model writes real content FIRST, drops the leaked instruction into
# the middle, then continues with more real content.
#
# MEASURED FAILURE (2026-08-18): ===FULL=== opened with real prose ("Good
# afternoon. Today is Tuesday, August 18, 2026.") THEN
# "<2-4 short lines, PLAIN TEXT, for a phone notification>" verbatim, THEN
# more real content, then the normal **📅 Today** section. The leaked bracket
# sat well past position 0, so _LEADING_BRACKET's `\A` anchor never saw it —
# the 2026-08-15 fix only covers a leak that's the very first thing written.
#
# No case-1 (unwrap real content) branch here on purpose: a bracket that
# isn't at the start of the text was never a greeting slot, so a match here is
# ALWAYS the leaked-instruction case — remove it outright, never keep its
# contents.
_ANY_BRACKET = re.compile(r"[ \t]*<([^\n<>]{1,150})>[ \t]*\n?")


def _strip_leaked_brackets_anywhere(text: str) -> str:
    def repl(m: re.Match) -> str:
        return "" if _LEAKED_PLACEHOLDER_MARKERS.search(m.group(1)) else m.group(0)
    text = _ANY_BRACKET.sub(repl, text)
    # A removed bracket can leave a run of 3+ blank lines where it used to
    # separate two real paragraphs — collapse back to one blank line, same
    # normalization _split_brief's caller already applies elsewhere.
    return re.sub(r"\n{3,}", "\n\n", text)


def _strip_prompt_glyphs(text: str) -> str:
    text = _strip_leading_placeholder_leaks(text)
    text = _strip_leaked_brackets_anywhere(text)
    return _UNREAD_DOT.sub(r"\1- ", _ROUTING_ECHO.sub(r"\1", text))


# Kept as the old name for the message path and its tests, which only ever had
# routing markers to worry about.
def _strip_routing_markers(text: str) -> str:
    return _ROUTING_ECHO.sub(r"\1", text)


# A section header line in _messages_rundown's output — alone on its own line,
# nothing else on it. Matches BOTH the bold form the prompt asks for and a
# markdown `#` heading: measured 2026-08-18, the model picks the heading form
# in roughly half of runs, and while the prompt now names that explicitly, a
# backstop that only knew about `**bold**` silently did nothing on exactly the
# runs whose headers were malformed — the ⏰ strip and the duplicate-section
# drop below both no-oped there.
_SECTION_HEADER = re.compile(r"^(?:\*\*(.+?)\*\*|#{1,6}\s+(.+?))\s*$")

# A leading run of non-word characters — emoji, punctuation, whitespace — at the
# front of a header's text, stripped off before comparing two headers by name.
# ⏰ is included by construction: it's a non-word character, so a header that
# starts "⏰ Group \"Grad GC\"" and one that starts "👥 Group \"Grad GC\"" both
# normalize to the same bare name.
_HEADER_LEADING_SYMBOLS = re.compile(r"^[^\w\"']+")


def _clean_message_sections(text: str) -> str:
    """Backstop for two failure modes _MSG_SYS asks the model not to produce,
    kept here because asking is not the same as guaranteeing:

    1. ⏰ landing IN a header, e.g. "**⏰ Group "Grad GC"**" — seen live
       2026-08-14, one run in five, despite the header shape in the prompt
       showing only a person/group emoji. Deleted wherever it appears on a
       header line; the rule already sends the model to say 'needs a reply' in
       the body instead, so nothing is lost.
    2. The SAME conversation getting two sections — seen the same day, both as
       an exact repeat of a header string and as a group split into its own
       section plus a stray section for one member's phone number. Caught by
       comparing header text with emoji/punctuation stripped off: the second
       and later sections whose stripped name matches an earlier one are
       dropped whole, keeping the first (fullest) telling of that conversation.

    Operates on _messages_rundown's raw output, section by section, so a
    conversation's own prose is never touched — only the header lines and which
    whole sections survive.
    """
    lines = text.split("\n")
    idxs = [i for i, l in enumerate(lines) if _SECTION_HEADER.match(l.strip())]
    if not idxs:
        return text
    for i in idxs:
        lines[i] = re.sub(r"⏰\s*", "", lines[i])
    bounds = [(i, idxs[n + 1] if n + 1 < len(idxs) else len(lines))
              for n, i in enumerate(idxs)]
    seen: set[str] = set()
    out: list[str] = []
    for start, end in bounds:
        header = lines[start].strip().strip("*").lstrip("#").strip()
        name = _HEADER_LEADING_SYMBOLS.sub("", header).strip().lower()
        if name and name in seen:
            continue
        if name:
            seen.add(name)
        out.extend(lines[start:end])
    return "\n".join(out).strip()


async def _messages_rundown(now: float) -> str:
    """Stage one: a single synthesized digest, from a messages-only prompt.

    Split out because attribution accuracy is a function of prompt length on
    this model, and the brief's prompt is long. Measured 2026-08-07 against the
    live engine, on the same rendered lines, scoring whether the user's own
    outgoing messages were credited to their recipients:

        full brief prompt (system + calendar + email + messages)   ~1/6 clean
        same, minus the profile block                               2/4 clean
        messages-only context                                       3/4 clean

    Same model, same data, same renderer — only the surrounding prompt changed.
    So the messages are summarized alone here, and stage two composes the brief
    around this text instead of re-deriving attribution from raw lines.

    Returns "" when there's nothing to summarize or the model comes back empty;
    the caller then gives an explicit unavailable-digest state rather than
    exposing raw messages as if they were a summary.
    """
    block = _messages_block()
    # Any source-status sentence is deterministic plumbing, not message
    # material for the model. The plain fallback below renders the matching
    # user-facing empty/unavailable state without spending a model call or
    # risking a restored cache being narrated as current.
    if block.startswith("MESSAGES:"):
        return ""
    from service.memory.identity import identity_prompt_block
    c = _c()
    model = role_to_model("fast")
    await c.ensure_only(model)
    resp = await c.chat(
        model,
        [{"role": "system", "content": (identity_prompt_block().strip()
                                        + "\n\n" + _MSG_SYS).strip()},
         {"role": "user", "content": block}],
        # no_thinking_kwargs, same as summarize_emails/summarize_messages
        # (email_tools.py, imessage_tools.py) — this call was missing it, so
        # the model spent its whole budget on an unbounded "Thinking
        # Process:" monologue instead of the answer: measured 2026-08-08,
        # ~141s for a brief that should take a few seconds, and under any
        # concurrent request oMLX came back with a response missing
        # `choices` entirely (KeyError, 500 to the Daily Summary button).
        max_tokens=4000, **no_thinking_kwargs(model))
    text = (resp["choices"][0]["message"].get("content") or "").strip()
    # _strip_prompt_glyphs runs HERE, not only in _generate_brief. That caller
    # applies it to stage two's `sections` and then immediately overwrites
    # sections["MESSAGES"] with this rundown and concatenates it into FULL —
    # so anything this text leaked went straight to the screen unstripped.
    # That bypass is how the 2026-08-18 "**<emoji> … <conversation name>**"
    # headers survived a stripper that was already in the codebase and already
    # matched "emoji". Stripping at the source closes it for both consumers.
    digest = _message_digest(text)
    return _message_digest(_strip_prompt_glyphs(_strip_routing_markers(digest).strip()))


def _message_digest(text: str) -> str:
    """Make an imperfect model response read as one digest, never a transcript.

    The prompt asks for a paragraph, but local models occasionally decorate a
    perfectly good answer with bullets or a heading.  Flattening those cosmetic
    choices here keeps the daily brief's Messages section a summary instead of
    a list.  A direct source-line echo is rejected before its routing markers
    are stripped, so it cannot be mistaken for prose.
    """
    # A direct echo of the model-facing source rows is not an imperfect digest;
    # it is a transcript.  Omit it so the caller can show the honest degraded
    # state instead.  Test before _strip_routing_markers removes the evidence.
    if _ROUTING_ECHO.search(text):
        return ""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Drop a standalone heading such as "Messages"; the enclosing brief
        # already supplies that heading.
        plain = line.strip("*# ").strip().lower().rstrip(":")
        if plain in {"messages", "message summary", "recent messages"}:
            continue
        line = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line)
        lines.append(line)
    return " ".join(lines).strip()


def _split_brief(raw: str, fallback: str) -> dict[str, str]:
    pattern = "|".join(_MARKERS)
    parts = re.split(rf"===({pattern})===\s*", raw)
    sections = {m: t.strip() for m, t in zip(parts[1::2], parts[2::2])}
    if not sections.get("FULL"):
        # No ===FULL=== marker, so the whole response becomes the brief. Strip
        # any OTHER markers it did emit rather than showing them: seen live
        # 2026-08-07, a run that opened "===TODAY===" and never reached
        # ===FULL=== put that marker at the top of the user's brief.
        sections["FULL"] = re.sub(rf"===({pattern})===\s*", "", raw).strip() or fallback
    return sections


def _greeting(now: float) -> str:
    """"Good morning"/"afternoon"/"evening" for the actual clock time `now`
    falls in — not the twice-daily `part_of_day` schedule flag ("morning" for
    the 8am run, "evening" for the 8pm one), which is a fine label for which
    of the two scheduled pushes this is but was wrongly reused as the greeting
    itself: pressing the on-demand button at 2pm inherited the scheduler's
    binary morning/evening split and said "Good evening" hours early."""
    hour = datetime.fromtimestamp(now).hour
    return ("Good morning" if hour < 12 else
            "Good afternoon" if hour < 18 else "Good evening")


# ---------------------------------------------------------------------------
# The rendered brief — what the user actually reads.
#
# Every section below is composed HERE, in Python, from the same caches the chat
# tools read. It deliberately does NOT concatenate those tools' return values,
# which is what `_generate_brief` used to do and what the brief looked like on
# 2026-09-08:
#
#   "Upcoming (today, 3 item(s)) — each row is tagged relative to today:"
#   "Calendar events: 0; Wisp/Apple reminders: 3. A calendar event alone is
#    not a reminder."
#   "Source excerpts (not inferred outcomes); names, accounts and relative
#    dates below are quoted from the original messages."
#
# Those strings are written FOR A MODEL — they are get_upcoming's and
# grounded_digest's prompt scaffolding, doing a job (anchoring dates, blocking
# invented attribution) that matters when a model reads them and is noise when a
# person does. Three of the brief's four section openings were instructions
# addressed to something else, every row carried a "[Apple Reminder]" tag, and a
# reminder titled "send my vaccine report to UCSC.**" opened a stray bold run.
#
# Rendering here also removes three tool-level source ensures per press (each of
# which asks the Swift app for another Mail/Messages read) on top of the single
# readiness wait `_sections` already does.
# ---------------------------------------------------------------------------

# `*` and backtick open Markdown runs in the panel's renderer, and titles,
# subjects and message bodies are written by other people — a stray one in a
# reminder title bolded the rest of the section.
_MD_RUN_CHARS = re.compile(r"[*`]+")


def _clean(text: object, limit: int = 0) -> str:
    """A title/subject/body as one Markdown-safe line, optionally truncated."""
    out = _MD_RUN_CHARS.sub("", " ".join(str(text or "").split())).strip()
    if limit and len(out) > limit:
        out = out[:limit - 1].rstrip(" ,.;:-—") + "…"
    return out


def _clock(ts: float, now: float) -> str:
    """A wall-clock time, marked "Yesterday" when it isn't today's.

    The mail and message windows are the last 24 HOURS, so a bare "10:53 PM"
    sat in the same list as this morning's rows with nothing to separate them.
    """
    when = datetime.fromtimestamp(ts)
    label = when.strftime("%-I:%M %p")
    return label if when.date() == datetime.fromtimestamp(now).date() else f"Yesterday {label}"


def _account_label(account: object) -> str:
    """A linked account as a short tag: "adnjain@ucsc.edu" -> "ucsc".

    Shown on every row when more than one account is in play, so the full
    address would be most of the line.
    """
    name = _clean(account, 40)
    if "@" in name:
        domain = name.rsplit("@", 1)[1]
        return domain.rsplit(".", 2)[0] if domain.count(".") > 1 else domain.split(".")[0]
    return name


def _kinds(item: dict) -> set[str]:
    return {item.get("source") or ""} | set(item.get("duplicate_sources") or [])


def _is_reminder(item: dict) -> bool:
    """A reminder (Apple Reminders or one Wisp added), not a calendar event.

    Checked by source first and `kind` second: the source is authoritative for
    real rows, and `kind` covers a manually added commitment that never carried
    one.
    """
    return bool(_kinds(item) & {"reminders", "manual"}) or item.get("kind") == "reminder"


def _agenda(now: float) -> dict:
    """Today's calendar events and reminders, plus each source's readiness.

    One accessor for both the section the user reads and the notification card,
    so the two can never disagree about what is on today.
    """
    from service.assistant.sync_status import source_status
    from service.tools.assistant_tools import _without_holiday_calendars
    states = {source: source_status(source) for source in ("calendar", "reminders")}
    skip = {source for source, state in states.items() if state["state"] == "unavailable"}
    today = datetime.fromtimestamp(now).date()
    items = [item for item in assistant_store.upcoming(now=now, days=7)
             if item.get("source") not in skip
             and datetime.fromtimestamp(item["when_ts"]).date() == today]
    items = _without_holiday_calendars(items, include_holidays=False)
    items.sort(key=lambda item: item["when_ts"])
    return {
        "events": [item for item in items if not _is_reminder(item)],
        "reminders": [item for item in items if _is_reminder(item)],
        "states": states,
        "show_account": len({item.get("account") for item in items
                             if item.get("account")}) > 1,
    }


def _agenda_row(item: dict, now: float, *, show_account: bool) -> str:
    when = datetime.fromtimestamp(item["when_ts"])
    delta = item["when_ts"] - now
    if item.get("all_day"):
        clock, rel = "All day", ""
    else:
        clock = when.strftime("%-I:%M %p")
        if delta < -300:
            # "(now)" for something that was due two hours ago is what the old
            # rows said, and it read as "happening right now".
            rel = "overdue" if _is_reminder(item) else "earlier today"
        elif delta < 300:
            rel = "now"
        elif delta < 3600:
            rel = f"in {int(delta // 60)} min"
        else:
            hours = delta / 3600
            rel = f"in {hours:.0f} h" if hours >= 2 else f"in {hours:.1f} h"
    extras = []
    if item.get("kind") in ("exam", "assignment"):
        extras.append("exam" if item["kind"] == "exam" else "due")
    if item.get("organizer"):
        extras.append(f"with {_clean(item['organizer'], 40)}")
    if item.get("location"):
        extras.append(f"at {_clean(item['location'], 40)}")
    if show_account and item.get("account"):
        extras.append(_clean(item["account"], 24))
    tail = f" ({', '.join(extras)})" if extras else ""
    return (f"- **{clock}** · {_clean(item['title'], 90)}{tail}"
            + (f" — {rel}" if rel else ""))


def _schedule_section(now: float) -> str:
    """Calendar events and reminders as two labelled groups.

    Separated structurally rather than by tagging every row "[Apple Reminder]"
    and appending "A calendar event alone is not a reminder." — the distinction
    the tool output was spending a sentence on is a heading here.
    """
    agenda = _agenda(now)
    states, show_account = agenda["states"], agenda["show_account"]
    lines = ["**📅 Today**"]
    if agenda["events"]:
        lines += [_agenda_row(item, now, show_account=show_account)
                  for item in agenda["events"]]
    elif states["calendar"]["state"] == "syncing":
        lines.append("- Calendar is still syncing.")
    elif states["calendar"]["state"] == "unavailable":
        lines.append("- Calendar couldn't be read — check Wisp's access in Settings.")
    else:
        lines.append("- Nothing on your calendar. ✅")
    blocks = ["\n".join(lines)]
    if agenda["reminders"]:
        blocks.append("**✅ Reminders due today**\n"
                      + "\n".join(_agenda_row(item, now, show_account=show_account)
                                  for item in agenda["reminders"]))
    elif states["reminders"]["state"] == "unavailable":
        blocks.append("**✅ Reminders**\n- Reminders couldn't be read — check "
                      "Wisp's access in Settings.")
    return "\n\n".join(blocks)


def _mail_split(now: float) -> dict:
    """Today's inbox, filtered and split into mail from people vs. automated."""
    from service.tools.email_tools import (
        email_freshness_warning, filter_summary_rows, is_machine_sender)
    from service.assistant.sync_status import source_status
    state = source_status("email")["state"]
    rows = [{"ts": ts, "account": account, "sender": sender,
             "subject": subject, "unread": unread}
            for ts, account, sender, subject, unread in filter_summary_rows([
                (r["ts"], r["account"], r["sender"], r["subject"], r["unread"])
                for r in _mail_rows(now)])]
    return {
        "state": state,
        "people": [r for r in rows if not is_machine_sender(r["sender"])],
        "automated": [r for r in rows if is_machine_sender(r["sender"])],
        "show_account": len({r["account"] for r in rows if r["account"]}) > 1,
        "warning": email_freshness_warning(),
    }


def _mail_row(row: dict, *, show_account: bool) -> str:
    # A leading • is unread. `unread` is None on cache lines written before Mail
    # sync collected read status, and None means UNKNOWN — so an unmarked row is
    # never asserted to have been read.
    account = f" · {_account_label(row['account'])}" if show_account and row["account"] else ""
    return (f"- {'• ' if row['unread'] else ''}**{_clean(row['sender'], 40)}** — "
            f"{_clean(row['subject'], 100)}{account}")


_MAX_PEOPLE_EMAILS = 10
_MAX_NOTICE_EMAILS = 6


def _email_section(now: float) -> str:
    mail = _mail_split(now)
    if mail["state"] == "syncing":
        return "**📧 Inbox**\n- Mail is still syncing; ask again in a moment."
    if mail["state"] == "unavailable":
        return "**📧 Inbox**\n- Email couldn't be read in this launch."
    people, automated = mail["people"], mail["automated"]
    unread = sum(1 for r in people if r["unread"])
    header = "**📧 Inbox**"
    if people:
        header += f" — {len(people)} from people" + (f", {unread} unread" if unread else "")
    blocks = []
    if people:
        blocks.append(header + "\n" + "\n".join(
            _mail_row(r, show_account=mail["show_account"])
            for r in people[:_MAX_PEOPLE_EMAILS]))
    else:
        blocks.append(header + "\n- Nothing from a person in the last day.")
    if automated:
        extra = (f"\n- …and {len(automated) - _MAX_NOTICE_EMAILS} more."
                 if len(automated) > _MAX_NOTICE_EMAILS else "")
        blocks.append("**📬 Notices**\n" + "\n".join(
            _mail_row(r, show_account=mail["show_account"])
            for r in automated[:_MAX_NOTICE_EMAILS]) + extra)
    if mail["warning"]:
        blocks.append(mail["warning"])
    return "\n\n".join(blocks)


# How many conversations the section names, and how many messages each one is
# counted from. One line PER CONVERSATION, not per message: rendering every
# recent message put twelve lines in the brief, nine of them one group chat's
# back-and-forth, and buried the 1:1 that actually wanted an answer. The same
# imbalance _MAX_CONVERSATIONS documents for the model prompt, in the output.
_MAX_BRIEF_CONVERSATIONS = 6
_MAX_MESSAGE_ROWS = 200


def _message_rows(now: float, limit: int = 0) -> list[tuple[float, str, str, str]]:
    """Recent texts as (ts, conversation, speaker, body), newest first.

    Rendered from the cache directly rather than through
    `imessage_tools.render_for_summary`, whose `[Sender -> Recipient] body` shape
    and "'you' in this message means X, NOT the user" annotations exist to hold a
    2.6B model's attribution steady and are not text to show a person.
    """
    from service.tools.imessage_tools import _parse_lines, filter_summary_message_rows
    cutoff = now - 24 * 3600
    rows = [row for row in _parse_lines() if row[0] <= now]
    recent = filter_summary_message_rows([row for row in rows if row[0] >= cutoff])
    out = []
    for ts, context, text in (recent[:limit] if limit else recent):
        sender, sep, body = text.partition(":")
        sender, body = sender.strip(), (body if sep else text).strip()
        context = _clean(context, 40)
        group = context.startswith("Group ")
        label = context[len("Group "):].strip('"') if group else context
        if sender == "Me":
            speaker = "you"
        elif group or sender.lower() != label.lower():
            speaker = _clean(sender, 30)
        else:
            speaker = ""      # 1:1 incoming — the conversation label IS the sender
        out.append((ts, label, speaker, _clean(body, 110)))
    return out


def _conversations(now: float) -> list[dict]:
    """Recent texts grouped by conversation, most recently active first."""
    grouped: dict[str, dict] = {}
    for ts, label, speaker, body in _message_rows(now, limit=_MAX_MESSAGE_ROWS):
        convo = grouped.setdefault(label, {"label": label, "count": 0,
                                           "ts": ts, "speaker": speaker, "body": body})
        convo["count"] += 1
    return list(grouped.values())


def _messages_section(now: float) -> str:
    """One line per conversation: who, how many, and the latest message verbatim.

    Quoted rather than re-narrated, for the reason `_messages_rundown` documents
    at length: at this model size a written digest reported the user's own
    outgoing messages as something the other person said. A quote with its
    speaker named cannot be misattributed.
    """
    from service.tools.imessage_tools import messages_sync_state
    state = messages_sync_state()
    if state == "syncing":
        return "- Messages are still syncing; ask again in a moment."
    if state == "unavailable":
        return "- Messages couldn't be read in this launch."
    convos = _conversations(now)
    if not convos:
        return "- Nothing new in your texts. ✅"
    lines = []
    for convo in convos[:_MAX_BRIEF_CONVERSATIONS]:
        count = convo["count"]
        tally = f"{count} message{'s' if count != 1 else ''}"
        who = f"{convo['speaker']}: " if convo["speaker"] else ""
        lines.append(f"- **{convo['label']}** · {tally}, latest "
                     f"{_clock(convo['ts'], now)} — {who}“{convo['body']}”")
    if len(convos) > _MAX_BRIEF_CONVERSATIONS:
        lines.append(f"- …and {len(convos) - _MAX_BRIEF_CONVERSATIONS} other conversation(s).")
    return "\n".join(lines)


def _render_brief(now: float, messages_section: str) -> str:
    """Greeting + the three sections + the sign-off. The brief's only shape."""
    when = datetime.fromtimestamp(now)
    parts = [f"{_greeting(now)} — {when.strftime('%A, %B %-d')}. ☀️",
             _schedule_section(now),
             _email_section(now),
             "**💬 Messages**\n" + (messages_section.strip() or "- Nothing new. ✅"),
             _CLOSING_LINE]
    return "\n\n".join(part for part in parts if part.strip())


def _today_card(now: float) -> str:
    """The "📅 Today" notification body: plain text, a few short lines.

    Built from `_agenda`/`_mail_split` rather than from the brief's Markdown,
    because a notification renders none of it — the old card carried
    get_upcoming's raw output, asterisks and "A calendar event alone is not a
    reminder." included.
    """
    agenda = _agenda(now)
    events, reminders = agenda["events"], agenda["reminders"]
    counts = []
    counts.append(f"{len(events)} event{'s' if len(events) != 1 else ''} on your calendar"
                  if events else "nothing on your calendar")
    if reminders:
        counts.append(f"{len(reminders)} reminder{'s' if len(reminders) != 1 else ''} due")
    lines = [f"Today: {', '.join(counts)}."]
    upcoming = [item for item in events + reminders if item["when_ts"] >= now]
    upcoming.sort(key=lambda item: item["when_ts"])
    if upcoming:
        nxt = upcoming[0]
        clock = ("all day" if nxt.get("all_day")
                 else datetime.fromtimestamp(nxt["when_ts"]).strftime("%-I:%M %p"))
        lines.append(f"Next: {_clean(nxt['title'], 60)} at {clock}.")
    overdue = [item for item in reminders if item["when_ts"] < now - 300]
    if overdue:
        lines.append(f"{len(overdue)} reminder{'s' if len(overdue) != 1 else ''} already past due.")
    mail = _mail_split(now)
    if mail["state"] == "ready":
        people, automated = len(mail["people"]), len(mail["automated"])
        if people or automated:
            lines.append(f"Mail: {people} from people, {automated} automated.")
    return "\n".join(lines)


def _messages_card(now: float) -> str:
    """The "💬 Messages" notification body: plain text, a few short lines."""
    from service.tools.imessage_tools import messages_sync_state
    if messages_sync_state() != "ready":
        return ""
    convos = _conversations(now)
    if not convos:
        return ""
    total = sum(convo["count"] for convo in convos)
    named = ", ".join(convo["label"] for convo in convos[:4]) + (
        f", +{len(convos) - 4} more" if len(convos) > 4 else "")
    lines = [f"{total} recent message{'s' if total != 1 else ''} in {named}."]
    latest = convos[0]
    who = f"{latest['speaker']}: " if latest["speaker"] else ""
    lines.append(f"Latest in {latest['label']} — {who}{latest['body']}")
    return "\n".join(lines)


def _plain_brief(now: float) -> str:
    """Last-resort brief: the same rendered sections, with the Messages digest
    stated as unavailable instead of shown.

    Reached when `_generate_brief` itself raises. It shares `_render_brief` with
    the primary path deliberately — the two used to be separate renderers, and
    the degraded one was the better-looking of the pair (the primary path was
    concatenating model-facing tool output, see `_render_brief`'s comment). One
    renderer means the brief cannot change shape depending on which path
    produced it.
    """
    from service.assistant.sync_status import daily_syncing_message, summary_snapshot
    snapshot = summary_snapshot()
    if snapshot["syncing"]:
        return daily_syncing_message(snapshot)
    return _render_brief(now, _plain_messages_section(now))


_CLOSING_LINE = ("Let me know if you'd like me to dig into any of these or "
                 "help draft a reply. 🙂")

# A `**💬 Messages**`-style header (or any texts/iMessage wording) that stage
# two emitted despite being told not to. _BRIEF_SYS forbids it, but a 2.6B
# model follows a negative instruction imperfectly, and one that slips through
# would sit right above the real messages section as a duplicate — with the
# vaguer, unchecked attribution of the two. Cutting from the offending header
# to the end is safe because the messages section is the LAST thing stage two
# would write: _BRIEF_SYS orders the sections and forbids a sign-off after.
_STRAY_MESSAGES_HEADER = re.compile(
    r"\n[ \t]*(?:\*\*|##+\s*)?[^\n]{0,4}\s*(?:messages?|texts?|imessages?)\b[^\n]{0,40}"
    r"(?:\*\*|:)?[ \t]*\n.*\Z",
    re.IGNORECASE | re.DOTALL)


def _plain_messages_section(now: float) -> str:
    """Model-free Messages state for a degraded brief.

    A semantic digest needs the summarizer.  The old fallback exposed every
    message verbatim, turning a Daily *Summary* into a transcript precisely
    when the model was unavailable.  Be clear about that limitation instead of
    pretending a list is a summary.
    """
    from service.tools.imessage_tools import _parse_lines, messages_sync_state
    state = messages_sync_state()
    if state == "syncing":
        return "- Wisp is still syncing Messages; this section is not ready yet."
    if state == "unavailable":
        return "- Messages could not be checked in this launch."
    rows = _parse_lines()
    cutoff = now - 24 * 3600
    recent = [r for r in rows if r[0] >= cutoff] or rows[:20]
    if not recent:
        return "- Nothing new in your texts. ✅"
    return "- Your recent messages are available, but their digest could not be generated right now."


def _assemble_full(body: str, messages_section: str) -> str:
    """Stage two's calendar+email prose + the messages summary + the sign-off.

    Joined here rather than generated in one pass so the messages section that
    reaches the user is byte-for-byte the one stage one produced and stage
    one's short, attribution-focused prompt vouched for. Every version of this
    that let stage two write or "reuse" that section lost detail or
    re-attributed it (see _MSG_SYS and _generate_brief).
    """
    body = _STRAY_MESSAGES_HEADER.sub("", body.strip()).strip()
    messages_section = messages_section.strip()
    # The header is supplied here, not by the model, which keeps the three
    # brief sections visually parallel and leaves stage one free to write only
    # the integrated Messages digest.
    if messages_section:
        messages_section = f"**💬 Messages**\n\n{messages_section}"
    parts = [p for p in (body, messages_section) if p]
    if not parts:
        return ""
    return "\n\n".join(["\n\n".join(parts), _CLOSING_LINE])


async def _generate_brief(part_of_day: str) -> dict[str, str]:
    """The rendered brief, plus the two short notification bodies.

    No model call and no tool call. Both are avoidable: `_sections` has already
    confirmed source readiness, and every section is composed from the same
    caches the tools read (see `_render_brief`). The version this replaces
    awaited get_upcoming, summarize_emails and summarize_messages and pasted
    their strings together, which cost three more app-side source reads per press
    and handed the user their prompt scaffolding as the brief.
    """
    now = time.time()
    return {"TODAY": _today_card(now),
            "MESSAGES": _messages_card(now),
            "FULL": _render_brief(now, _messages_section(now))}


# One wait for the app's Calendar/Reminders/Mail/Messages reads, in one place.
# It used to be four: the endpoint ensured, `_sections` ensured again, and then
# summarize_emails and summarize_messages each ensured their own source from
# inside `_generate_brief`. Every one of those publishes a sync request the Swift
# readers answer with a fresh (AppleScript, serialized) read of the same source,
# so a single press queued several overlapping Mail scans and waited out
# 8+8+2.5+2.5s of separate deadlines. One longer wait is both faster in practice
# and far more likely to return a real brief on the first press instead of
# "try again in a moment".
_SOURCE_WAIT_S = 20.0


async def _sections(part_of_day: str, snapshot: dict | None = None) -> dict[str, str]:
    """The entry point both callers below go through. NEVER raises, and FULL is
    never empty.

    Pass `snapshot` when the caller has already awaited `ensure_daily_sources`
    (the endpoint has, so it can report per-source progress) — that skips the
    second, duplicate wait.

    The returned dict carries ``READY`` = "1" only when the brief reflects
    confirmed source data. A hold-back message is a brief the user should see but
    NOT one the scheduler may count as the day's delivered brief, and telling
    those apart is what stops the false "your daily summary is ready" pings (see
    `run_scheduled_brief`).

    It is written this way because of how the failure actually presented. One
    ValueError deep in _email_block (the mail cache grew an `unread` field and
    this module still unpacked four) became a 500 from /assistant/daily_summary,
    which the Swift client turns into a nil result, which the overlay renders as
    "Couldn't build a summary right now." Neither path surfaced a traceback
    anywhere the user would look, so a feature that had been dead for days read
    as a model having an off morning.

    A brief that reaches the user is worth more than a correct exception: the
    calendar, mail, and messages are all on disk and `_plain_brief` needs no
    model to render them. So anything thrown below degrades to that instead of to
    nothing, and the traceback goes to stderr for whoever is debugging.
    """
    # Restored caches are intentionally not enough for any Daily Summary
    # source. Ask the Swift app for current Calendar, Reminders, Mail, and
    # Messages reads, then name anything still in flight deterministically.
    try:
        from service.assistant.sync_status import (
            daily_syncing_message, ensure_daily_sources)
        if snapshot is None:
            snapshot = await ensure_daily_sources(timeout_seconds=_SOURCE_WAIT_S)
        if snapshot["syncing"]:
            return {"FULL": daily_syncing_message(snapshot)}
    except Exception:  # noqa: BLE001 — other brief sources should still work
        traceback.print_exc()
    try:
        from service.tools.email_tools import (
            email_freshness_warning, with_email_freshness_note)
        # Captured BEFORE composing: a Mail sync that lands mid-brief must not
        # remove the caveat that applies to the snapshot the brief was built from.
        warning = email_freshness_warning()
        sections = await _generate_brief(part_of_day)
        if sections.get("FULL", "").strip():
            # with_email_freshness_note is a no-op when the text already says it,
            # so the rendered mail section keeps its own inline copy and only the
            # notification card (which has no section to put it in) gains one.
            for key in ("FULL", "TODAY"):
                if sections.get(key, "").strip():
                    sections[key] = with_email_freshness_note(sections[key], warning)
            sections["READY"] = "1"
            return sections
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        return {"FULL": _plain_brief(time.time()), "READY": "1"}
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return {"FULL": ("I couldn't pull your calendar, mail, and messages "
                         "together just now — try again in a moment.")}

async def build_daily_brief(part_of_day: str = "morning") -> str:
    """The Daily Summary button's entry point (see _sections). Never raises."""
    return (await _sections(part_of_day))["FULL"]


def brief_without_model() -> str:
    """The rendered brief, for callers that already know no model is coming —
    currently the endpoint's engine-down path. Synchronous and never raises."""
    try:
        return _plain_brief(time.time())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return ("Wisp's local model isn't running right now, so I couldn't put "
                "your brief together. Try again once it's back up.")


async def run_scheduled_brief(part_of_day: str) -> bool:
    """Scheduler entry: compose the brief and push it to the app as a
    'daily_brief' event carrying the full text plus two notification bodies —
    one for the schedule, one for messages.

    Returns True only when a REAL brief was queued for acknowledgement, and publishes nothing
    otherwise. Both halves of that matter. A brief that held back because the
    launch sync was still running is not the day's brief: publishing it made the
    app post "Your daily summary is ready in Wisp." over a body that actually
    said Wisp was still syncing, and the scheduler then marked the day done, so
    the real brief never followed. Because the backend is a child of Wisp.app and
    the fired-date used to live only in memory, every relaunch inside the
    schedule's window repeated that — which is where the run of false "ready"
    pings came from (see scheduler._maybe_daily_brief for the persisted date).
    """
    day = datetime.now().date().isoformat()
    if assistant_store.event_by_key("daily_brief:" + day):
        return True
    sections = await _sections(part_of_day)
    if not sections.get("READY") or not sections.get("FULL", "").strip():
        return False
    from service.assistant.hub import hub
    await hub.publish({
        "type": "daily_brief",
        "part_of_day": part_of_day,
        "text": sections["FULL"],
        "today_summary": sections.get("TODAY", ""),
        "messages_summary": sections.get("MESSAGES", ""),
    }, dedupe_key="daily_brief:" + day, target={"type": "brief", "date": day})
    return True
