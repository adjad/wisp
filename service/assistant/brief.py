"""Daily brief composer — one combined summary of calendar + email + iMessage.

Powers both the on-demand "Daily Summary" button and the scheduled 8am/8pm
digest.

Runs on the `fast` role (the always-warm summarizer the rest of the assistant
layer uses), NOT the agent model: the brief is a background task, and cold-loading the
12.7GB `general` model every morning is the swap this design exists to avoid.

TWO model calls, not one. Messages are summarized alone first
(_messages_rundown), then the brief is composed around that finished prose.
Attribution accuracy on this model degrades as the prompt grows, and a brief
that reports the user's own outgoing message as something the recipient said is
worse than a slower brief — see _messages_rundown for the measurements. It is
an improvement, not a cure: at 2.6B the section is still wrong some of the time.

Calendar is deterministic (from the commitments store); email and messages come
from the caches the Swift MailReader/MessagesReader push. Everything degrades
gracefully: a source with no data (e.g. Messages before Full Disk Access is
granted) just says so in its own block instead of breaking the brief.
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
    from service.tools.email_tools import header_rows
    return header_rows(since_ts=now - 24 * 3600) or header_rows(limit=20)


def _email_block(now: float) -> str:
    """Inbox context, PARTITIONED into mail from people vs. automated mail.

    Same principle as _calendar_block: decide in Python, hand the model labeled
    blocks. _BRIEF_SYS used to carry a bare "Skip promotional/newsletter mail"
    instruction and leave the judgment to the model, which is a classification
    job it does inconsistently at this size — it dropped a real recruiter reply
    as promotional, and elsewhere gave a LinkedIn digest its own bullet. The
    machine/human split is already a solved, deterministic question here
    (email_tools.is_machine_sender), so it is answered before the prompt is
    built and the model is left with the part it is actually good at: writing.

    Automated mail is summarized as a sender roll-up rather than dropped
    outright — "6 newsletters, nothing needing you" is a true and useful line,
    and silently hiding mail from a brief that claims to cover the inbox is not.
    """
    from service.tools.email_tools import is_machine_sender
    rows = _mail_rows(now)
    if not rows:
        return ("EMAIL: no inbox data available (Mail automation may not be "
                "granted). Say you couldn't check their mail — do NOT say the "
                "inbox is empty, and do NOT invent messages.")

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
    if human:
        unread = sum(1 for r in human if r["unread"])
        blocks.append(
            f"FROM REAL PEOPLE ({len(human)} email(s), {unread} unread). These "
            "are the ones worth writing about. A leading • means unread; no • "
            "means it is read or its status is unknown, so never describe an "
            "unmarked email as unread:\n"
            + "\n".join(_row(r) for r in human[:_MAX_HUMAN_EMAILS]))
    else:
        blocks.append("FROM REAL PEOPLE: none. Nobody wrote to them personally "
                      "— say the inbox was quiet on that front rather than "
                      "promoting an automated email into something personal.")
    if machine:
        senders, seen = [], set()
        for r in machine:
            key = r["sender"].strip().lower()
            if key not in seen:
                seen.add(key)
                senders.append(r["sender"].strip())
        blocks.append(
            f"AUTOMATED / NEWSLETTERS / NOTIFICATIONS ({len(machine)} email(s) "
            f"from: {', '.join(senders[:_MAX_MACHINE_SENDERS])}). Already "
            "filtered out for you: mention these ONLY as a single passing count "
            "if at all, and never give one its own bullet.")
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


# Conversation names of the blocks _messages_block most recently rendered, in
# block order. Populated there and read by _messages_rundown, which pairs them
# with the model's numbered bodies to build the section headers itself. A
# module-level list rather than a second return value only because
# _messages_block has other callers' expectations to keep; it is written and
# read within one synchronous call in _messages_rundown.
_names: list[str] = []


def _messages_block() -> str:
    # render_for_summary, not a plain join: it tags each group message with the
    # member it is addressed to, so "@Trishe - How is the AI conference going?"
    # can't be read as the user's conference. See imessage_tools._addressee.
    from service.tools.imessage_tools import _parse_lines, render_for_summary
    rows = _parse_lines()
    if not rows:
        return "MESSAGES: no data (grant Wisp Full Disk Access to include this)."
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
        # Numbered, and the number is load-bearing: it is the key the model's
        # answer is matched back on so the HEADERS can be built in Python
        # rather than generated. See _MSG_SYS and _assemble_sections.
        blocks.append(f"[{len(blocks) + 1}] {ctx} ({len(rendered)} message(s)):\n"
                      + "\n".join(f"- {line}" for line in rendered))
    _names.clear()
    _names.extend(order)
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
    "GROUNDING RULE FOR EMAIL: the inbox section is already split into mail "
    "FROM REAL PEOPLE and AUTOMATED mail. Write about the first group. The "
    "second group has been filtered for you — at most give it one passing "
    "count, never its own bullets."
)

# Markers the model is asked to emit; splits its single response into the
# three pieces above. If the model drops a marker (small local models
# occasionally do), that section falls back to empty and callers degrade
# gracefully rather than showing garbled output.
_MARKERS = ("TODAY", "MESSAGES", "FULL")


# Stage one of two, and now the ONLY thing that writes the brief's messages
# section — stage two no longer touches it (see _generate_brief).
#
# The body rules below are deliberately the same ones imessage_tools._SYS uses
# for the standalone `summarize_messages` tool, because that tool's output is
# the target: asked for a message summary directly, Wisp produced per-
# conversation sections with real substance ("You sent her the microwave
# measurements (21x15x12 inches), and she confirmed she got it"), while the
# brief's version of the same conversations collapsed to a single vague line
# ("A message exchange occurred where Trishe confirmed details about a
# character") that also merged two different conversations together. The
# difference was never the model or the data — both run on `fast` over the same
# rendered lines — it was that this prompt asked for "exactly one short line
# per block" and stage two then paraphrased those lines a second time.
#
# What is NOT copied from _SYS: its warm one-line lead-in and its closing
# offer. Those would be redundant here — the brief supplies its own greeting
# and its own sign-off around this section (see _CLOSING_LINE).
_MSG_SYS = (
    "You are Wisp, the user's personal assistant, catching them up on their "
    "recent texts for their daily brief — the way a close friend who scrolled "
    "through their messages would fill them in, not a machine report.\n"
    "\n"
    "The material below is already split into one numbered block per "
    "conversation. For EACH block, write one entry:\n"
    "\n"
    "the block's number, a period, a space, then a couple of natural sentences "
    "about what is going on in that conversation.\n"
    "\n"
    "One entry per block, in the same order, each on its own line.\n"
    "\n"
    # BODIES ONLY — the headers are built in Python from the block names (see
    # _assemble_sections). Three separate live failures came from asking this
    # model to WRITE the header, and each prompt fix produced a new shape:
    #
    #   1. Shown as a skeleton "**<emoji> <conversation name>**", the model
    #      copied the placeholders through verbatim — 2026-08-18, all five of
    #      the user's sections came out headed "**<emoji> 👩 👩 👧 👦
    #      <conversation name>**", not one conversation actually named.
    #   2. Described in prose instead, it used the arrow labels from INSIDE the
    #      block: "**Mom -> Adi Jain (you)**", "**Group "Grad GC" -> Group
    #      "Grad GC"**".
    #   3. Told to wrap the header in asterisks, it split the emoji onto its
    #      own italic line above the name.
    #
    # Every one of those is a formatting decision with exactly one right
    # answer that Python already knows — the block's own name. Generating it
    # was never buying anything. This also retires the ⏰-in-header and
    # duplicate-section backstops' reason for existing (both kept anyway; see
    # _clean_message_sections), since a header the model never writes cannot
    # carry a stray ⏰ or repeat a conversation.
    "Do NOT write a title, a name, or a header of any kind above an entry — "
    "the number is how the entry is matched to its conversation, and the name "
    "is added afterwards. Just the number and the sentences.\n"
    "\n"
    "IMPORTANT rules:\n"
    "- The number of blocks below is the number of entries you write. Not "
    "fewer, not more. Never merge two blocks into one entry, and never split "
    "one block into two.\n"
    "- A GROUP conversation has several different senders inside its ONE "
    "block — that is normal, expected, and still exactly one entry. Never "
    "pull one sender out of a group block and give that sender their own "
    "entry: their phone number or name is not a second conversation, it is "
    "one voice inside the group's single block.\n"
    "- Never skip a block because its name is a bare phone number or email — "
    "that only means the contact isn't saved, so summarize it and call them by "
    "that handle.\n"
    "- Write a couple of natural sentences on what is actually going on and "
    "the vibe. Synthesize — do not restate messages one at a time and do not "
    "quote them verbatim.\n"
    "- Mark anything genuinely waiting on the user's reply, or time-sensitive, "
    "with ⏰ or a bolded 'needs a reply'. A marketing blast, a short-code "
    "sender, or an automated alert never needs a reply — say what it was in "
    "one clause and move on.\n"
    "- Start at the first entry and stop after the last. No intro line, no "
    "sign-off, no overall summary — the greeting and the closing come from "
    "somewhere else.\n"
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
    """Stage one: the message summary, from a prompt that holds ONLY messages.

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
    the caller then falls back to handing stage two the raw lines, which is the
    old single-call behavior rather than a blank section.
    """
    block = _messages_block()
    if block.startswith("MESSAGES: no data"):
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
    return _clean_message_sections(
        _strip_prompt_glyphs(
            _assemble_sections(_strip_routing_markers(text).strip(), list(_names))))


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


def _plain_brief(now: float) -> str:
    """Deterministic brief for when the model returns nothing at all.

    This exists because the old fallback was the model's own `context` string,
    and that text is written AT the model, not at the user: it carries the
    scaffolding ("CALENDAR — TODAY IS ...", "these and ONLY these are today's",
    "EMAIL (recent inbox, sender | subject):") plus every raw row. Shown live on
    2026-08-07, it read as an unformatted dump next to the polished email and
    message summaries in the same session — the user reported it as the brief
    being "raw output, not refined".

    An empty `content` is not rare enough to leave unstyled: LFM2.5 always
    thinks, and `_demote_unclosed_think` deliberately blanks `content` whenever
    the think block was still open at the token ceiling. So this path renders
    the same three sources in the brief's own shape — warm header, bold section
    titles, short bullets — and simply doesn't editorialize, since there's no
    model output to editorialize with.
    """
    from service.tools.assistant_tools import _fmt
    from service.tools.imessage_tools import (_parse_lines as _msg_lines,
                                              render_for_summary)
    when = datetime.fromtimestamp(now)
    out = [f"{_greeting(now)}, here's your day. ☀️\n"]

    today = when.date()
    events = assistant_store.upcoming(now=now, days=7)
    show_account = len({c.get("account") for c in events if c.get("account")}) > 1
    rows = [_fmt(c, now, show_account=show_account) for c in events
            if datetime.fromtimestamp(c["when_ts"]).date() == today]
    out.append("**📅 Today**")
    out.append("\n".join(rows) if rows else "- Nothing on the calendar today. ✅")

    cutoff = now - 24 * 3600
    # Same partition the model gets (see _email_block), so the degraded brief
    # leads with mail from people instead of ten newsletters.
    from service.tools.email_tools import is_machine_sender
    mail = _mail_rows(now)
    human = [r for r in mail if not is_machine_sender(r["sender"])]
    for label, rows in (("**📧 Inbox**", human[:10]),
                        ("**📬 Also arrived**", [] if len(human) >= 10 else
                         [r for r in mail if is_machine_sender(r["sender"])][:5])):
        if rows:
            out.append(f"\n{label}")
            out.append("\n".join(f"- {'• ' if r['unread'] else ''}**{r['sender']}**"
                                 f" — {r['subject']}" for r in rows))

    msgs = _msg_lines()
    recent_msgs = [r for r in msgs if r[0] >= cutoff] or msgs[:10]
    if recent_msgs:
        out.append("\n**💬 Messages**")
        out.append("\n".join(f"- {line}"
                             for line in render_for_summary(recent_msgs[:10])))

    out.append("\nAsk me about any of these and I'll dig in.")
    return "\n".join(out)


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
    """Deterministic messages section, for when stage one returns nothing.

    Same grouping the model would have been given (see _messages_block), just
    rendered directly instead of summarized — one bold header per conversation
    with its own lines under it. Degraded, but never wrong: it states only what
    was actually said, and keeps the per-conversation shape so the section
    still reads as a section rather than a flat dump of unrelated lines.
    """
    from service.tools.imessage_tools import _parse_lines
    rows = _parse_lines()
    cutoff = now - 24 * 3600
    recent = [r for r in rows if r[0] >= cutoff] or rows[:20]
    if not recent:
        return "- Nothing new in your texts. ✅"
    groups: dict[str, list] = {}
    for r in recent:
        if r[1] not in groups and len(groups) >= _MAX_CONVERSATIONS:
            continue
        groups.setdefault(r[1], []).append(r)
    out = []
    for ctx, group_rows in groups.items():
        out.append(f"**💬 {ctx}**")
        for _ts, _ctx, text in sorted(group_rows,
                                      key=lambda r: r[0])[-_MAX_LINES_PER_CONVERSATION:]:
            # `_parse_lines` gives "Sender: body"; sender "Me" is the user.
            # Named explicitly rather than via render_for_summary's
            # `[A -> B]` arrows — those are prompt plumbing meant for a model
            # to read, and _strip_routing_markers exists precisely to keep
            # them off the user's screen.
            sender, sep, msg = text.partition(":")
            who = "You" if sender.strip() == "Me" else sender.strip()
            out.append(f"- **{who}:** {(msg if sep else text).strip()}")
        out.append("")
    return "\n".join(out).strip()


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
    # Header supplied here, not by the model: stage one is told to start at its
    # first conversation header so it can't drift into writing an intro line,
    # and this keeps the brief's three sections visually parallel
    # (**📅 Today** / **📧 Worth a look** / **💬 Messages**) with the per-
    # conversation headers reading as items underneath it.
    if messages_section:
        messages_section = f"**💬 Messages**\n\n{messages_section}"
    parts = [p for p in (body, messages_section) if p]
    if not parts:
        return ""
    return "\n\n".join(["\n\n".join(parts), _CLOSING_LINE])


async def _generate_brief(part_of_day: str) -> dict[str, str]:
    now = time.time()
    greeting = _greeting(now)
    # Stage one: messages alone, so their attribution is settled before they
    # enter a prompt long enough to scramble it (see _messages_rundown). Stage
    # two then gets finished prose in place of raw lines. If it comes back
    # empty we hand over the raw lines exactly as before — degraded, not blank.
    #
    # Caught, not propagated: the brief is a THREE-SOURCE report, and one dead
    # source is not a reason to have no brief. Before this, anything raised in
    # here — an engine hiccup, a cache row that changed shape — took out the
    # calendar and email sections too and reached the user as one flat
    # "Couldn't build a summary right now."
    try:
        rundown = await _messages_rundown(now)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        rundown = ""
    # Stage two is NOT given the messages at all any more — not the raw lines,
    # and not even the finished rundown to "reuse". Handing it the rundown and
    # asking it to keep the facts intact still cost detail every time: it
    # re-paraphrased a written paragraph down to one clause and, on 2026-08-08,
    # merged the 1:1 Trishe thread into the family group's Mahabharata chat
    # because both name Trishe. A summary that is already correct cannot be
    # improved by paraphrasing it again, so it now bypasses stage two entirely
    # and is spliced into the finished brief verbatim (see below).
    context = (f"{greeting}. Today is {datetime.now().strftime('%A, %B %-d')}.\n\n"
               + _calendar_block(now) + "\n\n" + _email_block(now))
    c = _c()
    # The `fast` role — the always-warm summarizer the rest of the assistant
    # layer uses (calendar/email/messages summaries all run on it), NOT the agent model.
    # Running the brief on the agent model cold-loaded the 12.7GB model for a background
    # task every morning, against the whole "keep the agent model asleep" design. The
    # brief is the same kind of work — synthesize already-fetched context into
    # prose — just over more sources at once.
    model = role_to_model("fast")
    await c.ensure_only(model)
    # Identity FIRST, before the formatting instructions: the brief is written
    # in the second person, so "who is 'you'" has to be settled before the model
    # starts deciding whose news goes in it. Without this block the brief
    # reported a family member's viral post — "@Trishe - Your post has 439
    # likes", sent by Mom to a four-person family group — as the user's own.
    from service.memory.identity import identity_prompt_block
    # messages=False: those are the `Sender -> Recipient` line-attribution rules,
    # and stage two hasn't been given a single message line since the split (see
    # above). They were left on out of inertia — dead prompt that still competes
    # for attention with the rules that DO apply here, and that describes a data
    # shape absent from this context, which is its own small invitation to
    # invent one. Stage one, which does get those lines, still asks for them.
    system = identity_prompt_block(messages=False).strip() + "\n\n" + _BRIEF_SYS
    resp = await c.chat(
        model,
        [{"role": "system", "content": system.strip()},
         {"role": "user", "content": context}],
        # no_thinking_kwargs (see _messages_rundown above) — without it this
        # call, same as that one, burned its budget on an unbounded "Thinking
        # Process:" monologue instead of the brief. 5000 is kept at/above
        # summarize_emails/summarize_messages (now 2500): the brief's prompt
        # is the biggest of the three (~9.4k chars of system + context), so it
        # gets the most headroom, not the least.
        #
        # CORRECTION (2026-08-09): this comment used to claim "max_tokens is a
        # ceiling, not a reservation, so the extra costs nothing". That is
        # wrong, and it contradicts both agent/loop.run_agent's own reasoning
        # for pinning steps at 3000 and the measured behaviour of oMLX's
        # prefill guard, which admits a request against prompt + max_tokens.
        # Unused headroom IS reserved KV. It costs no decode time (nothing
        # generates near the ceiling), but it does cost memory against the
        # 8.5GB budget, and it is held concurrently with the agent
        # conversation's own cache. Kept at 5000 anyway: the brief runs alone
        # on a schedule rather than alongside a live turn, and a truncated
        # brief is a quality bug. Keep both summarizers and this in step if
        # either moves.
        max_tokens=5000, **no_thinking_kwargs(model))
    raw = (resp["choices"][0]["message"].get("content") or "").strip()
    # Fall back to a rendered brief, never to `context` — that's the prompt we
    # wrote for the model, instruction lines and all.
    sections = _split_brief(raw, fallback=_plain_brief(now))
    # Stage two sees the inbox's unread dots, and — whenever stage one came back
    # empty and it fell back to the raw rendered lines — the routing markers
    # too. Both are prompt plumbing; neither belongs on screen.
    sections = {k: _strip_prompt_glyphs(v) for k, v in sections.items()}
    # The push-notification MESSAGES card is stage one's text, full stop —
    # stage two isn't asked to reproduce it at all. Having it copy the rundown
    # back out only spent tokens (and latency) on a section we then overwrote,
    # and "copy this verbatim" is not something a small model reliably does.
    messages_section = rundown or _plain_messages_section(now)
    if rundown:
        sections["MESSAGES"] = rundown
    # Assemble the in-app brief deterministically: stage two's calendar+email
    # prose, then the messages summary EXACTLY as stage one wrote it, then the
    # sign-off. Concatenation rather than generation is the point — it is the
    # only way the messages section reaching the user is guaranteed to be the
    # one whose attribution was actually checked.
    sections["FULL"] = _assemble_full(sections.get("FULL", ""), messages_section)
    return sections


async def _sections(part_of_day: str) -> dict[str, str]:
    """_generate_brief with a floor under it. NEVER raises, and FULL is never
    empty — the entry point both callers below go through.

    It is written this way because of how the failure actually presented. One
    ValueError deep in _email_block (the mail cache grew an `unread` field and
    this module still unpacked four) became a 500 from /assistant/daily_summary,
    which the Swift client turns into a nil result, which the overlay renders as
    "Couldn't build a summary right now." The scheduled 8am brief died of the
    same exception and simply never fired — _maybe_daily_brief marks the day
    done BEFORE awaiting, so a raise there is silence, not a retry. Neither path
    surfaced a traceback anywhere the user would look, so a feature that had
    been dead for days read as a model having an off morning.

    A brief that reaches the user is worth more than a correct exception: the
    calendar, mail, and messages are all on disk and _plain_brief needs no model
    to render them. So anything thrown below degrades to that instead of to
    nothing, and the traceback goes to stderr for whoever is debugging.
    """
    try:
        sections = await _generate_brief(part_of_day)
        if sections.get("FULL", "").strip():
            return sections
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        return {"FULL": _plain_brief(time.time())}
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


async def run_scheduled_brief(part_of_day: str) -> None:
    """Scheduler entry: compose the brief and push it to the app as both a
    first-class 'brief' event (so the panel can show the full text) and the
    payload for two notification cards — one for calendar+email, one for
    messages — instead of one generic 'summary is ready' ping.

    The TODAY/MESSAGES cards are best-effort: on the degraded path there is no
    model output to split, so they come back empty and the app falls back to a
    single generic notification (see OverlayModel's "daily_brief" case) with the
    real, rendered brief still waiting in the panel."""
    sections = await _sections(part_of_day)
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
