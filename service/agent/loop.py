"""The ReAct agent loop.

Drives the resident tool-capable model through:
  model -> tool_calls -> safety check -> (confirm) -> execute -> feed back -> repeat
until the model returns a final answer. Every action passes through the policy
engine and the audit log. Emits structured events for the frontend.
"""
from __future__ import annotations

import json
import hashlib
import re
import time
from collections import Counter
from datetime import datetime
from typing import Awaitable, Callable

from service import debug_capture
from service.config import narration_mode, no_thinking_kwargs, role_to_model
from service.inference.omlx_client import OMLXClient
from service.memory import prompt_blocks
from service.safety import Tier, audit, decide
from service.tools import (classify_tool_outcome, get_tool, is_tool_error,
                           tool_schemas)
from service.tools.registry import run_tool

Emit = Callable[[dict], Awaitable[None]]

_EFFECT_TOOLS = frozenset({
    "send_message", "send_email", "reply_to_email", "forward_email", "schedule_send",
    "draft_message", "draft_email", "add_reminder", "update_reminder",
    "add_calendar_event",
    "complete_reminder", "cancel_event", "clear_past_reminders", "clear_reminders",
    "cancel_scheduled_send", "toggle_setting",
    "write_file", "move_path", "delete_path", "trash_file",
})
_TERMINAL_OUTBOUND_DENIALS = frozenset({
    "send_message", "send_email", "reply_to_email", "forward_email", "schedule_send",
})
_DENIED_OUTBOUND_TEXT = "Okay — I didn’t send it."


async def _finish_denied_outbound(emit: Emit) -> str:
    await emit({"type": "text", "text": _DENIED_OUTBOUND_TEXT})
    await emit({"type": "done"})
    return _DENIED_OUTBOUND_TEXT


def _action_fingerprint(tool: str, args: dict) -> str:
    payload = json.dumps({"tool": tool, "args": args}, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

SYSTEM = (
    "You are Wisp, a private assistant running locally on the user's Mac. "
    "You can use tools to inspect and operate the machine. Keep answers concise.\n"
    "IMPORTANT rules about actions:\n"
    "- The current date/time is given to you below. If asked what time or date "
    "it is, just STATE IT — one short sentence, no tool call, no hedging like "
    "'I don't have a tool for that' (you don't need one, it's already right "
    "here), no asking the user to confirm first.\n"
    "- To do something, CALL THE TOOL directly. Do NOT ask the user for "
    "permission in text and do NOT just describe what you would do — the system "
    "automatically asks the user to confirm any risky action before it runs.\n"
    "- Read-only actions run automatically. If an action is blocked or the user "
    "denies it, acknowledge briefly and continue.\n"
    "- After a tool runs, report the actual result; never claim you still need "
    "confirmation for something that already happened.\n"
    # Verified failure 2026-08-19: asked to flag an email, no matching tool was
    # OFFERED (a router gap, since fixed) — the model called view_emails to
    # find the message, then told the user "I've flagged that email for
    # follow-up" with NO flag tool ever called. The user has no way to tell
    # this from a real success; they will act as though it happened.
    "- THE INVERSE IS EQUALLY SERIOUS: never say you did something — flagged, "
    "sent, deleted, scheduled, saved, moved — unless you can see the matching "
    "tool call actually ran THIS turn and succeeded. Calling a DIFFERENT tool "
    "(reading the item, looking it up) is not evidence the action happened. If "
    "no tool exists for what was asked, say plainly that you can't do it — a "
    "false 'done' is worse than admitting the gap, because the user has no way "
    "to tell the difference until it's too late.\n"
    # Verified failure 2026-08-19 (user's debug export), TWO rounds of it:
    # asked "from how far back can you check emails", the model first called
    # summarize_emails(count=20) — the RECENT-inbox digest, which cannot answer
    # a range question no matter how it's read — and pasted the raw subject
    # list back with no range ever stated. Rewriting this rule to also cover
    # reasoning-over-evidence didn't fix it on its own: replayed live after
    # that fix, the model called the SAME wrong tool again and answered with
    # an inbox summary instead of a range. The missing piece was that this is
    # a CAPABILITY question, answerable directly, not a data question that
    # needs a tool call to go gather evidence for.
    "- 'How far back can you check my email/messages/calendar/notes', 'how "
    "many X can you search', 'can you read attachments' — these ask about "
    "WISP'S OWN REACH, not about the user's data. You already know the answer "
    "from the tool descriptions themselves (they state their own coverage, "
    "e.g. summarize_emails/view_emails say how far back or how many they "
    "reach) — ANSWER DIRECTLY FROM THAT, in one or two sentences, the same way "
    "you state the date without a tool call. Do NOT call a data tool to "
    "'demonstrate' the range by dumping results — that answers a different "
    "question (what's actually there) with a wall of content instead of the "
    "range that was actually asked about, and the model has gotten this wrong "
    "twice by reaching for the tool out of habit instead of just reading its "
    "own capability text.\n"
    "- Separately: not every tool result IS the answer once you do call one — "
    "some are EVIDENCE you have to reason over. 'How many X do I have', "
    "'which of these is the biggest/most recent/oldest', 'is X more than Y' — "
    "these ask about something the returned data implies, not the data "
    "itself. Look at what actually came back (dates, counts, values) and "
    "STATE the derived fact in a sentence or two — do not paste the tool's "
    "raw output back and call that an answer; the user asked a question, not "
    "for a transcript. If the result doesn't fully answer it (e.g. it's "
    "empty and you can't tell whether that's because nothing matched or "
    "because you're past the tool's own reach), say what you can determine "
    "and name the uncertainty rather than presenting a guess as fact.\n"
    "  This is different from a request to SEE content — 'what did that "
    "email say', 'read me the note', 'show me my messages from Dan' — where "
    "the raw/verbatim result genuinely IS the answer and should be relayed "
    "as-is (see the mail/messages/notes rules below for which tools return "
    "verbatim content by design).\n"
    "- For live external facts — weather, stock/crypto prices, sports scores, "
    "breaking news, or any public webpage/API the user asks about — call "
    "`web_search` to discover real sources and `web_fetch` to read a selected "
    "source, NOT run_shell/curl. They are the reliable path (their descriptions "
    "lists known no-key endpoints for weather/crypto/news). If the fetch fails, "
    "is denied, or returns an error/no data, SAY you couldn't retrieve it — do "
    "NOT fall back to a number from memory. A confidently wrong live price/score "
    "is worse than 'I couldn't reach it right now.' Only state a live figure you "
    "actually got back from web_search/web_fetch this turn.\n"
    "- To write, fix, or refactor code, write it yourself and save it with "
    "`write_file` — do not just describe the code in your reply and stop there.\n"
    "- For anything about the user's calendar, schedule, deadlines, assignments, "
    "or what's coming up, call `get_upcoming`. For anything in the PAST — "
    "'what did I have last week', 'when did I last meet with X' — call "
    "`get_past_events` instead (covers roughly the last year). NEVER script "
    "Calendar.app via run_shell/osascript — it is slow and often lacks "
    "permission; Wisp already syncs the calendar locally.\n"
    "- If more than one Mail or Calendar account is linked, pass `account` to "
    "`summarize_emails`/`view_emails`/`get_upcoming`/`get_past_events` when "
    "the user asks about a SPECIFIC one ('my work email', 'my personal "
    "calendar') — omit it otherwise, including whenever only one account is "
    "linked.\n"
    "- To add something with a time: use `add_calendar_event` when the user wants "
    "a real CALENDAR EVENT or meeting (it writes to macOS Calendar and syncs to "
    "their devices); use `add_reminder` for a personal reminder in Wisp, "
    "mirrored to Apple Reminders. An existing calendar appointment is NOT a "
    "reminder, and `remember` only saves a fact, never an alert. When unsure which, prefer `add_calendar_event` for things "
    "with a specific time/place and `add_reminder` for 'remind me to …'.\n"
    "- To correct or reschedule a reminder that already exists, call "
    "`update_reminder`. A correction such as 'I mean today' is an action, not "
    "a durable preference: do not call `remember`, do not merely read it with "
    "`get_upcoming`, and never say it was updated unless `update_reminder` "
    "succeeded this turn.\n"
    "- To cancel/delete/remove something from the schedule, call `cancel_event` "
    "with whatever title the user named — do NOT ask for the date/time first; the "
    "tool matches by title and will tell you if it's ambiguous.\n"
    "- Reminder deletion has separate bulk scopes: `clear_reminders(scope='today')` "
    "means reminders due today, while `scope='all'` means every active Wisp or "
    "Reminders.app reminder. `clear_past_reminders` is only for explicitly old, "
    "overdue, or past-due items. Never pass 'today' as a title query and never "
    "describe a past-due-only result as clearing all reminders. Calendar events "
    "and meetings are not reminders and must stay untouched.\n"
    "- For an OVERVIEW of the user's email or inbox, call `summarize_emails`. "
    "For a SPECIFIC detail FROM an email — an order number, a confirmation "
    "code, an address, a pickup/delivery time, party details, exact wording "
    "to quote back — call `view_emails` instead, which returns the raw "
    "verbatim body/sender/recipient/subject, not a summary. There is no other "
    "way to read email; you CAN read it through these tools — never claim you "
    "cannot, and never fall back to run_shell/open_app to get at Mail.app.\n"
    # Verified failure 2026-08-10 02:32 (user's debug export): "is there
    # anything needed to be responded to on my emails?" names no time, and the
    # model scoped it to period="this week". At 2:32am on a Monday that range is
    # 2.5 hours old, so it matched nothing and the model told the user there was
    # nothing to respond to — while a full inbox sat there. It then repeated the
    # identical call when the user pushed back.
    "  ONLY scope mail by date when the user NAMED a time — `day` for one day "
    "they named, `period` for a range they named. A question with no time in it "
    "('anything I need to reply to?', 'anything important?', 'what's new?') "
    "means the RECENT inbox: pass neither. A calendar range can be nearly empty "
    "even when the inbox is full — early on a Monday, 'this week' is a few "
    "hours long — so never conclude the user has no mail from an empty range.\n"
    "  For 'what's unread', 'anything I haven't read', or 'what still needs a "
    "reply', pass `unread=true` rather than guessing from subject lines.\n"
    # "messages" was leaking into the email domain — the router used to widen
    # "any messages from mom" to the mail tools as well (see _INBOUND_RE), and
    # nothing in this prompt ever said what the word means. Both ends are fixed;
    # this is the half the model reads.
    "- MESSAGES MEANS THE MESSAGES APP — `summarize_messages` for an overview, "
    "`view_messages` for a specific detail. 'Messages', 'texts', 'iMessage', "
    "'SMS', 'who texted me' are ALL this one app, and NEVER email: if the user "
    "says messages, do not answer from their inbox, and do not call the mail "
    "tools to cover it. 'Mail', 'email', 'inbox' are the separate domain (see "
    "the email rule above). Only when the user names NO channel at all ('did I "
    "hear back from Dan?') should you check both.\n"
    "  Use `view_messages` for a specific detail (an address, a time, a code, exact "
    "wording) — pass `query` to search by keyword. You CAN read and quote "
    "their messages through these tools — never claim you cannot and never "
    "give a generic tutorial on how to use the Messages app. If the user "
    "names a specific day, pass it as `day`.\n"
    "- For anything the user wrote down or saved in Notes — an idea, a list, a "
    "code, project notes — call `search_notes` with a keyword `query`. This is "
    "always RAW verbatim content (there is no separate summary tool for notes).\n"
    # The user keeps very little in Notes, so it is the weakest source here and
    # a hit there is usually incidental. Ordering the search matters when the
    # model has to guess where something lives.
    "  NOTES IS THE LAST PLACE TO LOOK. This user keeps very little in Notes, so "
    "when it is unclear where a piece of information lives, search the other "
    "sources FIRST — messages, then mail, then calendar/reminders — and treat "
    "notes as the final fallback. Do still call it when the user explicitly says "
    "'my notes', or as part of a full sweep; just never lead with it, and never "
    "answer 'I couldn't find it' on the strength of an empty notes search alone.\n"
    # FILES WAS THE ONE DOMAIN WITH NO BLOCK HERE. Every other area of the
    # user's data had a rule; files had none, while run_shell's own description
    # listed the domains it should defer to and omitted files entirely — so on
    # a files request the model was handed run_shell's 554-character pitch,
    # `list_dir`'s 32-character stub, and no guidance at all. Measured over 5
    # reps of "List the files in my Downloads folder.": list_dir alone 2/5,
    # run_shell involved 3/5, worst case 4 run_shell calls / 5 model steps /
    # 9,097 characters of reasoning / 62s for a listing list_dir returns in
    # milliseconds. See builtin.list_dir for the matching description fix.
    "- For FILES AND FOLDERS on this Mac, call `list_dir` to see what's in a "
    "folder and `read_file` to read one — NOT run_shell with ls/cat/find. "
    "Write paths with a leading `~` (`~/Downloads`, `~/Desktop/notes.txt`): you "
    "do not know the user's account name, so `/Users/<name>/…` is a guess and "
    "is usually wrong. If a path turns out not to exist, call `list_dir` on the "
    "PARENT folder to see the real names — do NOT switch to run_shell, which is "
    "slower, may need its own confirmation, and gets you nothing extra here.\n"
    # REORGANIZING had no rule and no tool, and that combination destroyed
    # user data on 2026-08-16: the model correctly observed it had no move
    # tool, fell back to run_shell, and `rm -rf`'d the source folders without
    # ever running a move. move_path now exists (see builtin.move_path); this
    # tells the model to use it and states the invariant that makes the
    # dangerous shape unnecessary.
    "- To REORGANIZE, TIDY, SORT or FILE AWAY files, call `move_path` — once "
    "per file, several calls in the SAME step. It creates the destination "
    "folder for you, so you never need mkdir.\n"
    "  NEVER use run_shell with `rm` to reorganize anything. Moving files is "
    "the entire job: a folder you have emptied by moving its contents out is "
    "already reorganized, and an empty folder left behind is harmless. "
    "Deleting the SOURCE is never a step in a reorganize — if you are about to "
    "delete something the user did not explicitly ask you to delete, you have "
    "made a mistake. Move first, verify with `list_dir`, and delete nothing.\n"
    "- AGGREGATE QUESTIONS — for 'what's on my to-do list', 'what do I need to "
    "do', 'what am I forgetting', 'help me plan my day': call `get_upcoming` "
    "AND `search_notes` AND the summary tools TOGETHER. These "
    "have NO single source. The user's commitments are scattered across five "
    "places at once, and an answer from one of them is not a partial answer, it "
    "is a WRONG one — it quietly implies the other four were empty. So call "
    "ALL of these IN ONE STEP, in parallel, before you answer:\n"
    "    * `get_upcoming` — calendar events AND Reminders.app items\n"
    "    * `summarize_messages` — commitments people made to them, or they made\n"
    "    * `summarize_emails` — deadlines, asks, and things awaiting a reply\n"
    "    * `search_notes` — anything they wrote down themselves (listed LAST on "
    "purpose: this user keeps very little in Notes, so weight it lightest when "
    "merging, and never let an empty notes result read as 'nothing found')\n"
    "  Emit them as several tool calls in the SAME turn, not one at a time over "
    "several turns. Then merge what comes back into ONE list, grouped by what "
    "matters (due today / this week / waiting on a reply), and say where each "
    "item came from. If a source returns nothing, just leave it out — don't "
    "announce empty sources, and don't pad the list.\n"
    "  More generally: when a question spans several of the user's areas, call "
    "every relevant tool together rather than picking the single closest one. "
    "Reading one source and stopping is the most common way to answer these "
    "wrongly.\n"
    "- You CAN send things: `send_email` sends real mail, `send_message` sends "
    "a real iMessage/SMS, `reply_to_email` answers a specific email IN ITS "
    "THREAD (use it, not send_email, whenever the user says reply/respond — it "
    "needs the Message-ID that view_emails prints). Write the full draft out "
    "in your reply — recipient, subject, exact body — so the user can read it "
    "before the confirmation card, which shows only a one-line summary.\n"
    "  WRITING THE DRAFT IS NOT SENDING IT, AND IT IS NOT THE END OF YOUR "
    "TURN. When the user asked you to send something, the draft is a step you "
    "take on the way to calling the tool IN THAT SAME TURN — never stop after "
    "showing it, never end with 'here's the summary ready for Mom' or 'let me "
    "know if you'd like me to send it'. They already told you to send it; "
    "asking again is not caution, it is failing to do what was asked. The "
    "confirmation card is what protects them, and it only appears once you "
    "actually call the tool.\n"
    "  If they want to review it FIRST — 'draft it', 'write it but don't "
    "send', 'let me check it' — call `draft_email`/`draft_message` instead. "
    "Those open it in Mail/Messages already filled in and send nothing.\n"
    "  If they name a TIME for it to go out — 'text mom at 6', 'email them "
    "Monday morning', 'in 10 minutes' — call `schedule_send`, which delivers "
    "it later on its own. Pass its `when` argument the phrase itself ('in 10 "
    "minutes', 'monday morning') — Wisp resolves that to an exact time in "
    "Python; do NOT convert it to an ISO datetime yourself first, that is "
    "exactly the date/time arithmetic you are reliably wrong at. If the "
    "result says the time was in the past or unparseable, NOTHING was "
    "scheduled no matter how plausible the phrase looked — say so plainly "
    "and ask what time they meant, never report it as queued. "
    "`list_scheduled_sends` and `cancel_scheduled_send` manage the queue.\n"
    "  Never guess a recipient address or phone number: look it up with "
    "`lookup_contact`, or ask. If the user names someone by relationship or "
    "nickname ('mom', 'Dan', 'my landlord') rather than giving you a literal "
    "address, `lookup_contact` for that name is the FIRST tool call you make "
    "— before you draft anything, and always before send_message/send_email "
    "— never send yet, even as a first attempt, with a guessed handle. Your "
    "own email address(es), given below for ATTRIBUTION, are never a "
    "recipient for anyone else; if you don't have a real handle for the "
    "person in hand, you don't have enough to send yet. Sends always require "
    "the user's confirmation, every time, and that's expected — call the "
    "tool rather than asking for "
    "permission in text.\n"
    # VERIFIED FAILURE 2026-08-24 (user's debug export): "send mom a message
    # reminder her about my move in date" — the model called lookup_contact
    # for Mom, then immediately answered "I don't have the specific move-in
    # date handy" and asked the user for it, in the SAME turn `get_upcoming`
    # was sitting right there in its tool set. Only when the user manually
    # said "check my calendar" two turns later did it call get_upcoming and
    # find it. The recipient-lookup rule above already covers "who" — nothing
    # covered "what", so the model treated a missing fact as a dead end
    # instead of a search.
    "  The same applies to the CONTENT of what you're sending, not just the "
    "recipient: if the message needs a fact you don't already have — a date, "
    "a time, an address, a confirmation number, anything the user referred "
    "to with 'my X' or 'the X' — SEARCH for it FIRST with whichever tools "
    "you have this turn (`get_upcoming` for anything calendar-shaped, "
    "`search_notes`, `view_emails`/`view_messages` for a detail buried in a "
    "thread) BEFORE asking the user or saying you don't have it. Giving up "
    "without having looked is not caution, it's skipping a step you had the "
    "tools to do yourself. Only ask once your own search has come up empty "
    "or the result is genuinely ambiguous between more than one real "
    "candidate — and when you do ask, name the candidates you already found "
    "rather than asking as if you found nothing.\n"
    "- Mailbox upkeep: `archive_email` moves a message out of the inbox (it is "
    "NOT a delete — it stays in Archive), `mark_email_read` marks one read or "
    "unread. Both need the Message-ID from `view_emails`.\n"
    "- MEMORY — call `remember` whenever the user reveals something durable "
    "about themselves, EVEN IN PASSING and even when they never ask you to "
    "remember it. You are building a long-term picture of this person, not "
    "taking dictation. Save it the moment it comes up:\n"
    "    * what they own, drive, or use — 'I drive a BMW', 'I'm on a Mac'\n"
    "    * money and holdings — 'I invest in Nvidia and AMD', 'I bank with Chase'\n"
    "    * people in their life, and who those people are to them\n"
    "    * where they live, work, or study; their job, school, or field\n"
    "    * preferences, tastes, allergies, and how they want you to behave\n"
    "    * recurring routines, commitments, and ongoing projects\n"
    # VERIFIED FAILURE 2026-08-19 (user's debug export): a corrupted memory —
    # "Adi's family includes his mom (AdiJain888@gmail.com)" — was live for
    # weeks and used 624 times. AdiJain888@gmail.com is the USER'S OWN email
    # (it's in the identity block below as one of THEIR addresses), not his
    # mom's. Nothing in that conversation ever gave a mom's email; the fact was
    # invented and then treated as settled, and every later "email my mom"
    # silently sent to the user's own inbox instead of asking who to actually
    # use.
    "  NEVER SAVE A CONTACT DETAIL — an email, phone number, or address — FOR "
    "ANOTHER PERSON unless the user just told you that exact detail in this "
    "conversation, or a tool (lookup_contact, view_emails' sender/recipient "
    "fields) just returned it clearly attached to that person. Never infer one "
    "from nearby context, never reuse an address you see for a DIFFERENT "
    "reason (especially never the user's OWN address from the identity block "
    "below — that is explicitly THEIR account, not anyone else's), and never "
    "guess because a name and an address happen to appear near each other. If "
    "you don't actually have a person's contact detail, say so and ask — a "
    "wrong contact fact saved to memory does not just mislead one answer, it "
    "silently misdirects every future send to that person until someone "
    "notices.\n"
    "  A statement like 'I invest in Nvidia and AMD, and I drive a BMW' is "
    "THREE things worth keeping, and none of them came with the word "
    "'remember'. Waiting to be asked is the single most common way to get this "
    "wrong.\n"
    "  Never say 'I'll keep that in mind', 'noted', 'already noted', or "
    "'that's saved' unless you actually called `remember` on it THIS turn, or "
    "you can see it verbatim in the 'Things you were explicitly asked to "
    "remember' block already in your context. If you are not sure whether "
    "something was saved, call it again rather than assuming — `remember` "
    "updates an existing fact in place instead of duplicating it, so a repeat "
    "call is always safe. Claiming something is remembered when it isn't is "
    "worse than not mentioning memory at all: the user has no way to tell the "
    "difference until it's already gone.\n"
    "  Write each fact as ONE short standalone sentence that still makes sense "
    "months later with nothing around it: 'Adi drives a BMW', not 'he drives "
    "one'. Skip anything that is only about the request in front of you ('open "
    "Safari', 'summarize this email') — that is a task, not a fact about them.\n"
    # Verified failure: given "I invest in Nvidia and AMD, and I drive a BMW",
    # saving only the investments and then claiming the BMW was saved too left
    # the car permanently unrecorded while telling the user the opposite.
    "  ONE `remember` CALL PER FACT. When a message contains several unrelated "
    "facts, emit several separate tool calls in that same turn — one for the "
    "car, one for the investments — so each can later be updated or forgotten "
    "on its own. Do not merge unrelated facts into a single call, and never "
    "describe a fact as saved unless you actually called `remember` on it.\n"
    "  Pass the right `category`: 'fact' for things they own, hold, or are "
    "true of them (car, investments, where they live); 'preference' only for "
    "tastes and how they want you to behave; 'person' for people in their "
    "life; 'routine' for recurring commitments; 'project' for ongoing work.\n"
    "  `forget` removes one. Things you were already told are injected into "
    "your context automatically each turn — call `recall` only when you need "
    "something that isn't there.\n"
    "- To change something on a remote service — a webhook, an API the user "
    "gave you a URL for — call `http_request` (POST/PUT/PATCH/DELETE). Use "
    "`web_fetch` for plain reads; it's a GET and doesn't interrupt the user.\n"
    "- THINGS YOU CANNOT DO IN YOUR HEAD — you must RUN CODE (`run_shell`, or "
    "`create_tool` if they'll want it again) for these, and "
    "writing the answer yourself is always wrong, no matter how easy it looks:\n"
    "    * anything random or security-sensitive: passwords, tokens, API keys, "
    "UUIDs, dice rolls, shuffles, random picks. You are a language model — "
    "text you produce is NOT random and NOT cryptographically secure, even "
    "when it looks scrambled.\n"
    "    * hashes, checksums, encoding/decoding (MD5, SHA, base64, hex, URL "
    "encoding, JWT).\n"
    "    * precise arithmetic on big or many numbers, statistics over a list, "
    "unit/currency conversion with a live rate.\n"
    # Verified: asked for the ISO week number of a date, answering from your
    # head gave 46 when the answer was 47 — reliable-looking but wrong.
    "    * ANY calendar or date arithmetic — week numbers, day of the week, "
    "days/weeks between dates, leap years, 'what date is N days from X', "
    "timezone or DST conversion. You are reliably wrong at these and it "
    "always looks right.\n"
    "    * anything the user asks you to verify, count exactly, or reproduce "
    "byte-for-byte.\n"
    "  The test is not 'is this hard?' — it's 'is there one exact answer the "
    "user could check?'. If yes, run code. Do not reason your way to a number "
    "and present it; your arithmetic is not checkable and is often wrong.\n"
    "  For a ONE-OFF, run it with `run_shell` (e.g. "
    "`python3 -c \"import secrets; print(secrets.token_urlsafe(24))\"`) and "
    "report what it actually printed. If it's something they'll want AGAIN, "
    "call `create_tool` to build a real tool for it instead.\n"
    "- `create_tool` writes and installs a new tool using the on-device model "
    "— fully local, nothing leaves the machine. Use it when a request needs a "
    "capability no existing tool covers and the user would plausibly want it "
    "again. It is ONE call: the user reviews the actual generated code on a "
    "confirmation card and approves or rejects it there, so do not ask "
    "permission in text or paste the code first — say in one line what you're "
    "building, then call it.\n"
    "- The moment `create_tool` succeeds, that tool is callable — in the SAME "
    "turn, not the next one. So finish the job: call it and give the user "
    "their actual answer. Never end a turn with 'it's ready, ask me again'. "
    "If the request needs a folder on disk, pass `read_scopes`/`write_scopes` "
    "(e.g. ['~/Downloads']) — request the narrowest folder that does the job, "
    "since the tool is sandboxed to exactly what you declare and will be "
    "refused by the OS anywhere else.\n"
    "- If `create_tool` reports that Wisp already has a tool for this (contacts, "
    "messages, mail, calendar, notes), do NOT argue or retry — call that tool "
    "instead. A generated script genuinely cannot reach the user's personal "
    "data, so building one would only invent it.\n"
    "- For volume, call `get_volume`/`set_volume` — do NOT use run_shell/"
    "osascript for this, and do not try to guess the volume from context, "
    "just read it. For clipboard contents, call `clipboard_read`/"
    "`clipboard_write` rather than shelling out to pbpaste/pbcopy. For "
    "Wi-Fi on/off, call `set_wifi`. To lock the screen, call `lock_screen`.\n"
    # pmset -g log's BatteryHealth "Warning level"/"cap:" lines are low-charge
    # UI-warning events (cap = charge % at that moment), not a wear/health
    # metric — free-interpreting them produces confidently wrong degradation
    # claims. get_battery_status reports the real design-capacity health %.
    "- For battery charge, time remaining, cycle count, or health/degradation "
    "questions, call `get_battery_status` — do NOT use run_shell/pmset for "
    "this.\n"
    "- For keyboard backlight/lighting requests, call `set_keyboard_backlight` "
    "— do NOT use run_shell for this. It won't actually change anything (this "
    "Mac's keyboard backlight is fully automatic, confirmed not scriptable), "
    "but it returns the correct explanation to give the user, instead of you "
    "guessing a shell command or falsely claiming success. More generally: if "
    "you don't immediately know a command for some system control, that's a "
    "sign a dedicated tool exists (check the tool list) before you try to "
    "recall or guess a shell incantation from memory.\n"
    "- For music: `spotify` controls the Spotify app; `music` controls Apple "
    "Music. Use whichever the user names; if they just say 'play music' with "
    "no app named, prefer whichever is already open (check via a quick "
    "run_shell `pgrep` if genuinely unsure), otherwise default to `music` "
    "(it ships with macOS, so it's always available).\n"
    "- `get_upcoming` already includes reminders the user created directly in "
    "the Reminders app (not just ones Wisp itself created) — you don't need a "
    "separate tool or run_shell/osascript to check Reminders.app.\n"
    "- For 'what do you know about me/my X' or 'tell me about myself', call "
    "`recall` (same as 'what do you remember about X')."
)


def _parse_args(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _clean_tool_name(name: str) -> str:
    """Strip stray header tokens a parser can leave glued onto the parsed
    function name (e.g. 'send_message<|channel|>commentary').

    Written for gpt-oss, whose harmony chat template produced this exact
    shape ('web_fetch<|channel|>json', a 'functions.' namespace prefix) via
    oMLX's harmony adapter. gpt-oss is no longer rostered — the agent role is
    Agents-A1 (qwen3 lineage; upstream serves it with `--reasoning-parser
    qwen3 --tool-call-parser qwen3_coder`, not harmony), which does not emit
    this shape, so this is currently a no-op safety net rather than an active
    fix. Kept because it's cheap and the failure mode it guards against is
    expensive if a harmony-lineage model is ever rostered again: left
    uncleaned, the mangled name misses allowed_names, gets rejected as
    unoffered, and the rejection text tells the model to say the action isn't
    available — a working send_message surfaced to the user as "I can't send
    the message right now", which reads as Wisp refusing rather than a parse
    bug.
    """
    name = (name or "").split("<|", 1)[0].strip()
    return name.rsplit(".", 1)[-1] if name.startswith("functions.") else name


_STUCK_MESSAGE = (
    "I don't have a reliable way to do that right now — there's no tool for "
    "it and I couldn't find a working command, rather than keep guessing and "
    "risk getting it wrong."
)

# Shown when a step spent its entire token budget thinking and never reached an
# answer (see _demote_unclosed_think). Deliberately says the request is fine
# and worth repeating — this is a budget failure on our side, not a refusal and
# not something the user asked badly.
_TRUNCATED_MESSAGE = (
    "I ran out of room working that one out and didn't get to an answer. "
    "Ask me again — narrowing it a little (naming the calendar, inbox, or "
    "messages specifically) usually gets there."
)

# Last-resort text for a turn that produced literally nothing: no tool ran, no
# content survived, and the retry ladder above is spent. Something has to reach
# the user — an empty assistant bubble is the one outcome that reads as Wisp
# being broken rather than as Wisp having a bad turn.
#
# VERIFIED FAILURE 2026-08-18: "can you send a message to my[mom] with the
# movements of my stocks from today" — the model replied in text, got the
# call-a-tool nudge, then returned "\n\n\n" on both remaining attempts. Every
# existing guard missed it: last_tool_result was empty (no tool ever ran) and
# _think_leak was false (it wasn't a truncated monologue), so `text` fell
# through as whitespace and the turn returned it. The user saw a blank reply
# and typed "?".
_EMPTY_MESSAGE = (
    "I couldn't produce a reliable answer for this request."
)

# Appended to the system prompt only when run_agent(test_mode=True). Tells
# the model directly, rather than relying on it inferring anything from the
# stubbed-out results below — otherwise a model that gets back a placeholder
# and infers "success" will happily narrate a real-sounding answer built on
# nothing, which is exactly the fabrication test mode exists to prevent.
#
# Deliberately spells out BOTH branches (tool needed vs. not), as two
# unconditional rules rather than one general "describe the plan, don't
# really answer" statement. VERIFIED LIVE 2026-08-10 on "write a haiku about
# autumn" (routed with tools offered, needs_tools=True, tool_choice="auto",
# so the model is free to just answer in plain text): the earlier one-rule
# version told it to "describe the plan, not a real answer" without ever
# saying what to do when the plan IS "no tool, answer directly" — the model
# spent ~900 words of chain-of-thought arguing with itself over whether
# writing the actual haiku counted as the forbidden "real answer", concluding
# nothing, and got cut off mid-loop. A small model handles two short
# unconditional rules; it does not reliably resolve the self-referential case
# a single general rule leaves implicit.
_TEST_MODE_SUFFIX = (
    "\nTEST MODE — DRY RUN. Do not think out loud about these two rules or "
    "which one applies — just apply the right one and answer:\n"
    "1. If this request needs a tool: any tool you call will NOT actually "
    "run (nothing is read, written, or sent — you'll get back a placeholder, "
    "not a real result, no matter what you call), so call whichever tools "
    "you genuinely would, in the order you'd call them, then reply with "
    "ONLY a short plan of which tools you used and why — never state or "
    "imply anything was actually checked, found, sent, or completed.\n"
    "2. If this request needs NO tool at all (e.g. writing/explaining/"
    "answering from what you already know): call no tool, and reply with "
    "ONLY one short sentence saying no tool is needed and this would be "
    "answered directly — do NOT write the actual haiku/summary/code/answer "
    "itself, that is the exact 'real answer' this dry run exists to skip."
)

# Fed back as every tool's "result" in test mode instead of actually running
# it. Deliberately says nothing about the tool's real behavior — the model
# already has the tool's name/description/args from its own schema and
# earlier reasoning; this only needs to say "this did not run" clearly enough
# that the model can't mistake it for real data.
_TEST_MODE_STUB = "[TEST MODE] Not executed — this is a dry run, no tool actually ran."


def _is_looping(reasoning_text: str, min_count: int = 4, min_len: int = 20) -> bool:
    """Detects a degenerate reasoning loop: observed live on "turn on my
    keyboard lighting" (no dedicated tool, no plain shell equivalent) — the agent model
    repeated the same rejected guess ("maybe it's `sudo ... pmset ...`? Not.")
    verbatim ~30 times across paragraph breaks for 2 minutes/76 heartbeats
    before giving up, instead of converging or admitting it didn't know.
    Splits on paragraph breaks (how the model's own reasoning already chunks
    these repeats) and flags it once a non-trivial paragraph recurs enough to
    be clearly stuck rather than legitimately exploring a few options."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", reasoning_text) if len(p.strip()) >= min_len]
    if not paragraphs:
        return False
    return Counter(paragraphs).most_common(1)[0][1] >= min_count


def _template_kwargs(model: str, *, no_thinking: bool) -> dict:
    """chat_template_kwargs for one step.

    Just `no_thinking_kwargs` (enable_thinking, qwen3 lineage — what the
    current roster uses) when narrating; {} otherwise. This used to also merge
    in `effort_kwargs` (reasoning_effort, gpt-oss lineage) — removed with
    gpt-oss's retirement, since it always returned {} for every model on this
    roster. If a mixed roster returns, a second lineage's kwargs would need
    merging again rather than clobbering (each returns a whole
    `{"chat_template_kwargs": {...}}` dict).
    """
    return no_thinking_kwargs(model) if no_thinking else {}


async def _run_step(client, model, msgs, schemas, choice, max_tokens, emit,
                    *, stream_content: bool,
                    temperature: float | None = None,
                    no_thinking: bool = False,
                    debug: bool = True) -> tuple[dict, str, bool]:
    """One model turn, streamed. Emits live answer tokens (as `delta` events) and
    throttled `heartbeat`s during silent reasoning so the agent path is never a
    no-feedback black hole. Returns (assembled_message, reasoning_text,
    content_was_streamed) — the caller uses content_was_streamed to detect and
    correct a specific failure mode (see run_agent's clear_answer emission).

    `no_thinking`: suppress the model's think block for this step. ONLY set for
    a narration step — see run_agent's `narrating`. Never for tool selection,
    where it measures 0/3 tool calls."""
    reasoning_parts: list[str] = []
    final: dict = {}
    content_streamed = False
    last_hb = time.monotonic()
    # Degenerate WHITESPACE flood on the content channel — the content-side
    # twin of _is_looping's reasoning check, which cannot see this because it
    # only inspects reasoning text and only counts paragraphs >= 20 chars.
    # Verified live on a 12B-class model: asked to "Open the Calculator app", it
    # called open_app correctly, got "opened Calculator" back, and then emitted
    # NOTHING BUT newlines until the token budget ran out — 41s to produce 151
    # characters of "\n". That reads to the user as Wisp hanging after the
    # action already succeeded. Tripping here abandons the runaway generation
    # with empty content, which run_agent then falls back to last_tool_result
    # for — i.e. the user gets "opened Calculator", the true answer.
    content_chars = 0
    saw_real_content = False
    events = client.stream_events(
            model, msgs, tools=schemas, tool_choice=choice, max_tokens=max_tokens,
            temperature=temperature,
            **_template_kwargs(model, no_thinking=no_thinking))
    async for ev in events:
        kind = ev["kind"]
        if kind == "reasoning":
            reasoning_parts.append(ev["text"])
            now = time.monotonic()
            if now - last_hb > 1.5:          # keep the UI alive while it thinks
                await emit({"type": "heartbeat"})
                last_hb = now
            # Cheap periodic check (not on every token) — bail out of a
            # degenerate repetition loop instead of burning the rest of the
            # token budget on it. aclose() (not just `break`) so the
            # underlying stream/idle-tracking cleans up immediately rather
            # than waiting on GC.
            if len(reasoning_parts) % 20 == 0 and _is_looping("".join(reasoning_parts)):
                if stream_content:
                    await emit({"type": "delta", "text": _STUCK_MESSAGE})
                    content_streamed = True
                final = {"role": "assistant", "content": _STUCK_MESSAGE, "tool_calls": None}
                await events.aclose()
                break
        elif kind == "content":
            # Don't stream a reply we're about to reject on a forced-tool retry.
            if stream_content and ev["text"]:
                await emit({"type": "delta", "text": ev["text"]})
                content_streamed = True
            elif not stream_content and time.monotonic() - last_hb > 1.5:
                await emit({"type": "heartbeat"})
                last_hb = time.monotonic()
            content_chars += len(ev["text"])
            saw_real_content = saw_real_content or bool(ev["text"].strip())
            # Threshold, not first-token: a model legitimately opening with a
            # newline or two before real prose must not be cut off. 60 chars of
            # unbroken whitespace is never a real answer in progress, and the
            # flood generates slowly (measured ~3.7 chars/s), so the threshold
            # is also how long the user waits before the fallback kicks in.
            #
            # `_degenerate` tells run_agent this empty content is a DIAGNOSED
            # runaway, not the unparseable-response case its retry exists for.
            # Without it the retry fires, re-samples at +0.7 temperature, floods
            # again, and doubles the stall — measured 54s on a task the tool had
            # already completed in 0.1s.
            if not saw_real_content and content_chars > 60:
                final = {"role": "assistant", "content": "", "tool_calls": None,
                         "_degenerate": True}
                await events.aclose()
                break
        elif kind == "final":
            final = ev["message"]
    # Raw request/response for debug export (see OverlayModel.exportDebugLog
    # on the Swift side). Gated on `debug` — this serializes the full message
    # list AND every offered tool schema, on every attempt of every step, and
    # measured at up to ~1.6MB for a single real session. Free when Debug Mode
    # is off (the common case): the client sends whether it's on in the
    # `/agent` request body, and it's threaded down to here.
    if debug:
        request: dict = {
            "messages": msgs, "tools": schemas, "tool_choice": choice,
            "max_tokens": max_tokens,
        }
        # Mirrors what actually went over the wire — absent means "oMLX's own
        # per-model setting decided", so the export never reports a temperature
        # Wisp didn't send.
        if temperature is not None:
            request["temperature"] = temperature
        await emit({
            "type": "raw_model_io",
            "model": model,
            "request": request,
            "response": final,
        })
    return final, "".join(reasoning_parts).strip(), content_streamed


# Largest tool result, in characters, that goes into the model's context.
# ~12k chars is ~3k tokens; every normal result is far under it (the email and
# message summaries run 800-1,100 chars, a full view_emails page ~11,500), so
# in practice this only bites pathological ones.
#
# It exists because an uncapped result is the single biggest thing that can
# blow up memory here, and it compounds: the loop re-sends the whole
# conversation every step, so one oversized result is paid again on every
# subsequent step of the turn. Measured cause of a real crash: a 89,000-char
# show_profile result (~22k tokens) went into the context whole. On this model
# — head_dim=256, and oMLX TurboQuant converts only 7 of 32 cache layers — a
# token costs ~128KB of KV, so that one result was ~2.8GB, and the turn drove
# the process past its 19GB hard watermark until oMLX aborted the request and
# unloaded the model.
_MAX_TOOL_RESULT_CHARS = 12000

# Tool names as they're written in the prompt: `send_email`, `get_upcoming`.
_PROMPT_TOOL_RE = re.compile(r"`([a-z_][a-z0-9_]*)`")


def _system_blocks() -> list[tuple[frozenset, str]]:
    """SYSTEM split into (tools it talks about, text) pairs.

    The association is DERIVED from the prompt text rather than maintained by
    hand: a block's tools are the backticked names in its FIRST LINE that are
    really registered. A hand-written map would be one more thing to update
    when a tool is renamed, and it would fail silently — the block would just
    quietly stop being sent.

    Only the first line counts, because that's the block's subject. Scanning
    the whole block matches CROSS-REFERENCES too, which keeps the wrong things:
    the 400-token "You CAN send things" block says "look it up with
    `view_emails`" near its end, so a plain inbox read — which offers
    view_emails and nothing else — was still paying for the entire send
    section. The first line is what a block is ABOUT; later lines are what it
    points at.

    The rule is only as good as the prompt's first lines, so blocks whose
    opening sentence didn't name their own tools have been rewritten to do so.
    """
    from service.tools.registry import REGISTRY

    blocks: list[tuple[frozenset, str]] = []
    for part in re.split(r"\n(?=- )", SYSTEM):
        first_line = part.split("\n", 1)[0]
        named = frozenset(_PROMPT_TOOL_RE.findall(first_line)) & frozenset(REGISTRY)
        blocks.append((named, part))
    return blocks


_BLOCKS_CACHE: list[tuple[frozenset, str]] | None = None


def build_system(offered: set[str]) -> str:
    """The system prompt cut down to the tools actually offered this turn.

    Measured before this existed: SYSTEM was ~3,694 tokens sent on EVERY
    request, and 62% of it sat in eight blocks describing tools that most
    requests never get. "What's in my inbox" is routed to two tools and was
    still paying 646 tokens of memory rules, 400 of send rules, and 138 about
    the keyboard backlight.

    That matters more than it sounds: agent steps are prefill-dominated, so
    prompt tokens are most of the latency, and on a 16k window the prompt was
    also what pushed requests into oMLX's `400 Bad Request` (see _fit_window).

    A block with no registered tool in it is a CROSS-CUTTING rule (how to
    behave, what not to answer from memory) and is always kept. Everything
    else rides along only when one of its tools is on the table.

    Built once per TURN, not per step, deliberately: a system prompt that
    changed between steps would invalidate the prompt-cache prefix every time
    and re-prefill the whole thing.
    """
    global _BLOCKS_CACHE
    if _BLOCKS_CACHE is None:
        _BLOCKS_CACHE = _system_blocks()
    return "\n".join(text for named, text in _BLOCKS_CACHE
                     if not named or (named & offered))

# Smallest answer worth generating. Below this the model can't finish a
# sentence, so a window too tight for it needs tools/history dropped instead.
#
# Raised 600 -> 2000 on 2026-08-08. 600 was sized for a model that answers
# directly, but every text role now runs a model that THINKS first, and the
# budget has to cover the monologue AND the answer. Measured on the failing
# turn: "anything from earlier today?" took the unscoped route, _fit_window's
# step 1 shrank the reservation to 804, and the model produced 3,597 characters
# of pure chain-of-thought before hitting the ceiling — it never got to an
# answer or a tool call, and there is no answer short enough that it would
# have. A floor the thinking alone can exhaust isn't a floor. Raising it makes
# _fit_window fall through to steps 2 and 3 (drop old history, then tool
# schemas) instead, which costs breadth on one step rather than guaranteeing
# that step returns nothing usable.
_MIN_OUTPUT_TOKENS = 2000
# Never drop below this many tools — a step with nothing to call can only fail.
_MIN_TOOLS = 4


def _est_tokens(obj) -> int:
    """Cheap tokenizer-free estimate, same ~4-chars-per-token rule the context
    budgeter uses. Deliberately approximate: it only has to be right enough to
    keep us off the window boundary, and it errs high on JSON (punctuation-
    dense), which is the safe direction."""
    return len(obj if isinstance(obj, str) else json.dumps(obj)) // 4


def _fit_window(msgs: list[dict], schemas: list[dict], max_tokens: int,
                model: str, force_first_tool: str | None):
    """Trim a request until prompt + output fits the model's context window.

    Order of sacrifice, LEAST VALUABLE FIRST:
      1. the OLDEST history turns — never the system prompt (rules the model
         needs), never the last user message (the actual request), and never
         the most recent tool result (what the current step is reasoning about)
      2. TOOL SCHEMAS, from the end of the list
      3. the OUTPUT reservation, down to _MIN_OUTPUT_TOKENS

    Output used to go FIRST, as "the cheapest thing to give up". That was true
    when callers reserved 8000 tokens they never used, and it stopped being
    true when max_tokens was sized to what steps actually generate (3000,
    covering p90). Two measurements say output is now the LAST thing to cut:
    an output token costs ~137x a prompt token in wall time, and ~70% of the
    output budget goes on chain-of-thought before the answer starts — so a cut
    reservation lands almost entirely on the answer.

    Cutting it first was also a one-way door, which is the actual bug this
    ordering fixes. Step 1 lowered max_tokens to the floor, steps 2 and 3 then
    freed enough room for the full request, and nothing ever raised it back —
    so a turn that only needed one stale history turn dropped still returned a
    third less answer than it asked for. Reclaiming space first, and pricing
    the answer last, means the reservation is only reduced when the request
    genuinely cannot fit any other way.

    Tools come before output but after history because dropping one can make a
    request impossible to fulfil; they still have to be droppable, since on the
    unscoped route they are ~40% of a 24k window and a budget that treats them
    as fixed simply cannot fit. A forced first tool is always kept.
    """
    from service.config import model_context_window

    window = model_context_window(model)
    schemas = list(schemas)
    msgs = list(msgs)

    def _msg_tokens(m: dict) -> int:
        """A message's real cost, including any tool_calls payload.

        Counting only `content` undercounts an assistant tool-call message to
        ZERO: those carry content "" and put everything in `tool_calls`, whose
        arguments can be enormous — a write_file call embeds the entire file
        body as a JSON string. The budgeter therefore believed a request was
        thousands of tokens smaller than it was and let it overflow the window,
        which is the bare `400 Bad Request` this whole function exists to
        prevent.
        """
        n = _est_tokens(m.get("content") or "")
        if m.get("tool_calls"):
            n += _est_tokens(m["tool_calls"])
        return n

    overhead = _est_tokens(schemas) + sum(_msg_tokens(m) for m in msgs)

    # THE USER'S ACTUAL QUESTION IS NEVER DROPPABLE, wherever it now sits.
    #
    # The guard below used to be purely positional — "the last two messages" —
    # which is only the user's turn while the conversation still ends with it.
    # As soon as the loop appends an assistant tool-call and a tool result, the
    # last two are the tool traffic and the user's question slides to index 1,
    # the FIRST thing this loop reaches for. On a long multi-step turn the model
    # could be left reasoning over tool output with no record of what it was
    # asked — the single worst thing in the list to sacrifice, and the whole
    # reason the ordering comment calls history "least valuable first".
    last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"),
                    default=-1)

    # 1. Drop the oldest droppable history.
    def droppable(i: int) -> bool:
        if i == 0:                       # system prompt
            return False
        if i == last_user:               # the request being answered
            return False
        if i >= len(msgs) - 2:           # newest tool result / current exchange
            return False
        return True

    i = 1
    while overhead + max_tokens > window and i < len(msgs):
        if droppable(i):
            overhead -= _msg_tokens(msgs[i])
            msgs.pop(i)
            if i < last_user:            # indices shifted under us
                last_user -= 1
            continue
        i += 1

    # 2. Drop tools from the end, keeping any forced one.
    while (overhead + max_tokens > window and len(schemas) > _MIN_TOOLS):
        victim = next((s for s in reversed(schemas)
                       if s["function"]["name"] != force_first_tool), None)
        if victim is None:
            break
        overhead -= _est_tokens(victim)
        schemas.remove(victim)

    # 3. Last resort: price down the answer itself. Only reached when history
    # and tools together couldn't make room, so this is a request that cannot
    # fit any other way — not the ordinary case it used to be.
    if overhead + max_tokens > window:
        max_tokens = window - overhead

    return msgs, schemas, max(_MIN_OUTPUT_TOKENS, max_tokens)


# TOOL CALLS IN ONE STEP RUN SEQUENTIALLY, ON PURPOSE.
#
# Running a step's independent read-only calls concurrently looks like free
# latency — the aggregate route calls get_upcoming + search_notes +
# summarize_emails + summarize_messages together, and the last two each make
# their own model call, so in principle the step should cost their max rather
# than their sum. Built and measured it (2026-08-08); it was worse on every
# axis, so it was removed rather than shipped:
#
#   * SLOWER end to end. Two model-calling tools serialized: 27.3s median per
#     turn. The same two overlapped: 37.3s median, with a 74.5s outlier. There
#     is one resident model, so concurrent generations do not overlap — they
#     contend for the same weights and KV budget, and the total decode work is
#     unchanged.
#   * It CRASHES the tool. Two concurrent summarize_* calls reproduced oMLX's
#     known concurrent-load failure, where it returns a 200 with no `choices`
#     key at all: `KeyError: 'choices'` out of imessage_tools._summarize.
#   * The reads that AREN'T model calls have nothing to overlap anyway —
#     get_upcoming + search_notes + view_messages measured 16ms serial vs 15ms
#     parallel, because they all read in-memory caches.
#   * And concurrency is what threatens the memory ceiling: a single request
#     peaks ~4.6GB even at 20k prompt tokens, while overlapping requests drove
#     the process to 10.9GB against an 8.0GB guard.
#
# If this is ever revisited, the thing to fix first is the engine (one model
# instance cannot serve two generations in parallel), not this loop.


def _fit_tool_result(result: str, name: str) -> str:
    """Bound what a single tool result contributes to the context.

    Truncation is ANNOUNCED rather than silent, and names the tool's own
    narrowing arguments. A model handed a quietly-cut result will answer from
    the fragment as though it were the whole thing — which is worse than the
    memory problem this solves, because it's wrong instead of slow.
    """
    if len(result) <= _MAX_TOOL_RESULT_CHARS:
        return result
    kept = result[:_MAX_TOOL_RESULT_CHARS].rsplit("\n", 1)[0]
    return (f"{kept}\n\n[... {len(result) - len(kept):,} more characters were cut "
            f"to fit. This is NOT all of it — do not describe what you have as "
            f"complete. If you need what's missing, call {name} again with "
            f"narrower arguments (e.g. a `query`, a `day`, or a smaller "
            f"`count`) rather than assuming the rest is empty.]")


# How each source is introduced when several are merged into a fallback answer.
# Named per tool rather than printed as a bare function name: the user is
# reading this, and "From your calendar:" is an answer while "get_upcoming:" is
# a stack trace. Anything not listed falls back to the tool's own name, which is
# still better than silently dropping it.
_SOURCE_LABELS = {
    "get_upcoming": "From your calendar and reminders",
    "get_past_events": "From your past events",
    "search_notes": "From your notes",
    "summarize_emails": "From your email",
    "view_emails": "From your email",
    "summarize_messages": "From your messages",
    "view_messages": "From your messages",
    "lookup_contact": "From your contacts",
    "list_contacts": "From your contacts",
    "recall": "From what you've told me",
    "list_dir": "Files found",
    "read_file": "File contents",
    "web_fetch": "From the web",
    "web_search": "Web search results",
}


def _merge_results(results: list[tuple[str, str]]) -> str:
    """Present several tool results as ONE labelled answer.

    Used only on the fallback paths — the model produced no answer of its own,
    or the step budget ran out — where the alternative is showing the LAST
    result alone and implying the others were empty. Less polished than narrated
    prose, and strictly better than a confidently partial answer.

    A single result is returned untouched, so the common case is unchanged.
    """
    if len(results) == 1:
        return results[0][1]
    seen: set[str] = set()
    blocks: list[str] = []
    for name, result in results:
        # One block per TOOL, not per call: a tool called twice (two search_notes
        # queries) would otherwise get two identically-labelled sections.
        if name in seen:
            continue
        seen.add(name)
        label = _SOURCE_LABELS.get(name, name)
        blocks.append(f"**{label}:**\n{result.strip()}")
    return "\n\n".join(blocks)


async def run_agent(
    client: OMLXClient,
    model: str,
    messages: list[dict],
    emit: Emit,
    approver,
    *,
    tools: list[str] | None = None,
    active_skill: str = "",
    max_steps: int = 8,
    # Sized to what agent steps ACTUALLY generate, not to a round number.
    # max_tokens is not free: oMLX's prefill guard admits a request against
    # prompt + max_tokens, so every step reserved 8000 tokens of KV headroom
    # whether or not it used them. Measured over 190 real completions on this
    # model: median 268 tokens, p90 1595, max 5000 — so 8000 was reserving
    # ~30x the typical need on EVERY step of EVERY turn.
    #
    # That reservation is expensive here specifically because this model has
    # head_dim=256 with 8 KV-cache layers (~8MB per 64 tokens, i.e. ~128KB per
    # token) and oMLX's TurboQuant only converts 7 of its 32 cache layers to
    # 4-bit — most of the cache stays full precision. 8000 unused tokens is
    # therefore ~1GB of reserved KV per step. Combined with a prompt that
    # grows every step (the loop re-sends the whole conversation), a real
    # multi-tool turn walked 6,856 -> 18,718 prompt tokens and tipped the
    # process over its 19GB hard watermark, aborting the request and unloading
    # the model.
    #
    # 3000 covers p90 with room to spare and still leaves headroom for a long
    # narration.
    max_tokens: int = 3000,
    force_first_tool: str | None = None,
    expect_tool_first: bool = False,
    short_circuit_tools: set[str] | None = None,
    style_hint: str | None = None,
    include_memory_context: bool = True,
    temperature: float | None = None,
    # True for routes whose tools are SEQUENTIAL/COMPLEMENTARY rather than
    # alternatives (the aggregate to-do route, the document read-then-open
    # route, …) — see router.RouteDecision.multi_round for the reasoning and
    # router._domain_subset/_core_tools for which routes set it. Forces the
    # narration-thinking gate off even in config narration_mode()=="broad",
    # because that mode's premise — "every tool called so far is clean, so the
    # model is done" — doesn't hold when a route may genuinely need to call a
    # SECOND, different tool that it hasn't reached yet.
    multi_round: bool = False,
    # The tools whose results make a `multi_round` route provably done, after
    # which it may narrate without thinking after all. Ignored unless
    # multi_round is True; empty keeps multi_round's original meaning (never
    # narrate). See router.RouteDecision.narration_after.
    narration_after: frozenset[str] = frozenset(),
    # Tool calls the ROUTER already resolved in full — name AND arguments —
    # executed before the first model call. See router.RouteDecision.direct_calls
    # for when a rule is allowed to set this, and the execution block below for
    # what it shares with the model-driven path.
    direct_calls: list[tuple[str, dict]] | None = None,
    required_tool_groups: tuple[frozenset[str], ...] = (),
    forbidden_tools: frozenset[str] = frozenset(),
    conditional_tools: tuple[tuple[str, str, str, object], ...] = (),
    tool_argument_bindings: dict[str, dict] | None = None,
    reminder_action: str = "",
    # DRY RUN: every tool call the model makes is intercepted BEFORE the
    # safety decision/confirmation/execution — nothing runs, nothing is
    # written, nothing is sent. The model is told this in the system prompt
    # (see _TEST_MODE_SUFFIX) and gets back a fixed stub instead of a real
    # result, so it can still chain further tool calls to build out a
    # multi-step plan, but has no real data to answer the user's actual
    # question with — its eventual final-text response is necessarily a
    # description of what it WOULD do, not an attempt at the real answer.
    # Each intercepted call (name, args, safety tier, reason) is still
    # emitted as a normal `tool_call` event — see the interception point
    # below — so callers get the plan for free from the existing event
    # stream, no separate return path needed.
    test_mode: bool = False,
    # Whether to emit raw_model_io events (see _run_step) — the full request
    # (messages + every offered tool schema) and response, on every attempt of
    # every step. Real cost: measured up to ~1.6MB for a single session. The
    # client only needs this when Debug Mode is on and it's asking for an
    # export, so main.py threads the client's own debugMode flag through here
    # rather than sending it unconditionally.
    debug: bool = True,
) -> str:
    """Run the loop. Returns the final assistant text. Streams events via `emit`.

    `force_first_tool`: make this the ONLY tool available on the first step,
    instead of leaving delegation to the model's judgment.

    `short_circuit_tools`: tool names that already return complete, user-ready
    text — ONLY summarize_emails/summarize_messages qualify (see
    email_tools.py's/imessage_tools.py's _summarize(), each of which makes its
    own the summarizer call to synthesize real prose from raw headers/lines), so the
    tool result already IS the answer. Without this, the calling model (the summarizer,
    in the light-read route these tools live on) goes on to write a "final
    answer" that just restates the tool result nearly verbatim — a wasted
    extra generation round-trip that also LOOKED buggy in the UI (the
    collapsed tool-activity line and the full reply showing the same text
    twice). When the model's ONLY tool call in a step is one of these, skip
    that redundant round trip and use the tool's own result as the final text
    directly.

    Deliberately does NOT include get_upcoming/search_notes/view_emails/
    view_messages, even though they're also "light-read" tools — those return
    raw structured or verbatim data with no internal synthesis at all (view_
    emails/view_messages are explicitly documented as "RAW, verbatim... not a
    summary"), so the calling model's narration pass over their result is the
    ONLY synthesis step for that data, not a redundant second one. An earlier,
    over-broad version of this set included them too, which silenced that
    narration entirely and made e.g. calendar answers read as a bare
    mechanical dump of the raw template instead of natural prose.

    Also deliberately NOT applied when multiple tools are called in one step
    (e.g. a compound "messages and calendar" read calls both
    summarize_messages and get_upcoming) — that case needs the model to
    actually MERGE two separate results into one coherent reply, which is
    real synthesis, not an echo.

    Two empirically-verified findings shape this: oMLX ignores a *named*
    forced tool_choice for the agent model's tool calling (the model called
    write_file directly anyway) — restricting the tools list itself is
    the real guarantee, since a function absent from the schema can't be
    called. (Originally verified under gpt-oss's harmony-format tool calling;
    not yet reverified under Agents-A1's qwen3_coder tool-call parsing, so
    treat "oMLX's required is a soft nudge" as the safer default assumption
    rather than something to retest away.) That's necessary but not sufficient: with a longer system prompt
    in play, the model can still talk itself into a plain-text reply even with
    tool_choice="required" and only one tool available — oMLX's "required"
    turned out to be a soft nudge, not a decode-time constraint, once tested
    against the real system prompt rather than a short probe. So the first
    step retries with an explicit correction if no tool call comes back,
    rather than trusting either oMLX flag to guarantee it alone.
    """
    # Give the model "now" so it can resolve relative dates ("tomorrow", "this
    # Friday", "in 2 hours") when scheduling — otherwise it guesses the date.
    # resolve_hint=True: this loop's tools can SCHEDULE things, unlike
    # main.py's plain chat branches, which use the same shared helper without
    # it (see service/memory/prompt_blocks.py).
    #
    # Placed LAST among the system blocks (see sys_content below). It is the
    # only part of the system prompt that changes within a session, so
    # wherever it sits, everything after it is a cache miss on the next turn —
    # oMLX's prefix cache matches a literal token prefix. At position two it
    # invalidated the identity, memory and skills blocks, i.e. most of the
    # prompt; last, those all stay shared.
    #
    # Rounding the clock (to 5 minutes, say) would extend the shared prefix
    # further, and is deliberately NOT done: the system prompt tells the model
    # to answer "what time is it" directly from this line, so rounding buys a
    # little prefill and pays for it with an answer that is wrong by minutes.
    now_line = prompt_blocks.now_line(resolve_hint=True)
    # Who the user actually IS (service/memory/identity.py) — ground truth
    # available from the first launch. Always included: the name/address facts
    # are cheap and used well beyond messages (e.g. never suggesting the user's
    # own address as someone else's).
    #
    # The multi-person message-attribution rules are a SEPARATE, much larger
    # part of this block (~1,600 of its ~2,150 chars) and are gated on whether
    # this turn can even see a messages result — `view_messages` hands the
    # agent "Group of 3 (Mom, dad, Trishe) | Mom: @Trishe - Your post has 439
    # likes", so it needs the same rules the summarizers do for reading them,
    # but ONLY on a turn where that's possible. identity.py's own docstring
    # states the principle this follows: "an unfirable rule still costs
    # attention" — a scoped route with no messages tool offered can never
    # trigger these rules, so paying for them on every step of e.g. "open
    # Safari" or "set the volume" was pure waste. `tools is None` is the
    # unscoped route (the whole registry, messages included).
    offers_messages = tools is None or bool(set(tools) & {"view_messages", "summarize_messages"})
    identity_hint = ""
    try:
        from service.memory.identity import identity_prompt_block
        # "verbatim": the agent relays view_messages' raw record, which keeps
        # the `conversation | Sender: text` shape (see render_for_summary on
        # why the verbatim path is deliberately left un-annotated).
        identity_hint = identity_prompt_block(with_date=False, shape="verbatim",
                                              messages=offers_messages)
    except Exception:  # noqa: BLE001
        identity_hint = ""

    # Facts the user explicitly asked Wisp to remember (service/memory/facts.py).
    # NOTE: this is not the last block overall — skills_hint/style_hint follow
    # it below — so any precedence claim rests on the blocks' own wording, not
    # position.
    # Grounded delivery workflows must compose from this turn's source
    # receipts. Old remembered prose can be stale and carries no object-level
    # provenance; including it caused a vaccine reminder to be attributed to
    # Mom despite no tool result saying that. Ordinary agent turns keep the
    # user's requested memory context.
    memory_hint = prompt_blocks.memory_block() if include_memory_context else ""

    # Instructions from installed skills whose triggers match this turn (see
    # service/skills). Empty until the user installs one.
    skills_hint = ""
    try:
        from service.skills import skills_context_block
        last_user = next((m["content"] for m in reversed(messages)
                          if m.get("role") == "user"), "")
        skills_hint = skills_context_block(str(last_user), active_skill)
    except Exception:  # noqa: BLE001
        skills_hint = ""

    # style_hint (set for light-read narration) is appended LAST so it can
    # override the base prompt's "Keep answers concise" when the task is
    # narrating the user's own calendar/notes/verbatim data expressively.
    schemas = [s for s in tool_schemas(tools)
               if s["function"]["name"] not in forbidden_tools]
    # Every tool this TURN may use, as opposed to what a given step offers. A
    # forced step narrows the offer to one tool; a call to something else in
    # this set is premature, not impossible — see the rejection below.
    turn_tool_names = {s["function"]["name"] for s in schemas}
    # Scope the prompt to this turn's toolset (see build_system). `tools` is
    # None on the unscoped route, which yields the full prompt — same as before.
    sys_text = build_system({s["function"]["name"] for s in schemas})
    # Order matters: style_hint last so it can override "keep answers concise".
    # Prefix-cache: now_line goes after every block that is stable for the whole
    # session, because it is the one that changes (see its comment above).
    sys_content = (sys_text + identity_hint + memory_hint + skills_hint
                   + now_line
                   + (("\n" + style_hint) if style_hint else "")
                   + (_TEST_MODE_SUFFIX if test_mode else ""))
    if reminder_action:
        sys_content += (
            "\nREMINDER TASK: The user wants a reminder notification, not a new "
                "calendar event or a saved memory. get_upcoming labels the source "
                "of each item; a Calendar event is NOT proof of a reminder. "
                "In 'remind me to ask Trishy', Trishy is part of the reminder "
                "text, not a message recipient; 'my event/date' belongs to the user. "
                "Only a successful add_reminder result proves creation. These are "
            "Wisp/Reminders notifications, not a Clock-app ringing alarm. "
            + ("The user has not chosen an alert time. You may look up the "
               "appointment, then ask what time or how long before it to remind "
               "them. Do not invent a time or claim a reminder was set."
               if reminder_action == "clarify_time" else
               "Use the user-chosen alert time, looking up the appointment "
               "first for a relative lead time. Then call add_reminder; merely "
               "looking it up does not complete this request."))
    msgs: list[dict] = [{"role": "system", "content": sys_content}] + messages
    first_step_schemas = tool_schemas([force_first_tool]) if force_first_tool else schemas

    # the agent model turns run exclusive (no the summarizer co-residency attempt — verified it
    # doesn't hold once the agent model is actively generating anyway, see
    # OMLXClient.ensure_only). Must be re-applied on EVERY step here, not just
    # the caller's first ensure_only before run_agent started: this loop calls
    # ensure_only again after every tool call, and without exclusive here that
    # per-step call falls back to the default keep-warm behavior — silently
    # reloading the summarizer back in mid-turn, which is exactly the "the summarizer and the agent model
    # both loaded at once" bug this fixes. A light-read turn (model == the summarizer,
    # the small narrow-toolset route) doesn't need this — the summarizer IS the
    # keep-warm model there, so the default behavior already does the right
    # thing.
    exclusive = model == role_to_model("agent")

    # The most recent tool result this turn. Used as a fallback answer when the
    # model calls a tool that already returns user-ready text (a messages/
    # calendar/notes summary) but then produces an EMPTY final answer on the
    # next loop round — a verified small-model (the summarizer) failure mode: it calls
    # summarize_messages, gets a perfect summary back, then emits nothing, so
    # the user saw only the tool-activity line and no reply. Surfacing the tool
    # result beats returning empty. (the agent model doesn't hit this — it re-presents
    # the result itself — so this only ever kicks in when the model whiffed.)
    last_tool_result = ""
    last_tier: Tier | None = None
    # EVERY clean result this turn, in call order, as (tool_name, result).
    #
    # `last_tool_result` alone is not a safe fallback on a route that reads
    # several sources. The aggregate to-do route calls get_upcoming AND
    # search_notes AND summarize_emails AND summarize_messages in one step; if
    # the model then returns empty content, falling back to "the last one" hands
    # the user the messages digest AS their entire to-do list — committing
    # exactly the error SYSTEM spends twenty lines forbidding ("an answer from
    # one of them is not a partial answer, it is a WRONG one — it quietly
    # implies the other four were empty"). The step-limit fallback has the same
    # shape and is reached on precisely the multi-hop turns where several
    # sources have already been read.
    #
    # The data is already in hand either way; this only stops it being thrown
    # away. See _merge_results.
    clean_results: list[tuple[str, str]] = []
    # Tools CALLED this turn that have already come back with a real result.
    # Once every tool the model has called is clean, the model likely has
    # nothing left to figure out and the next step is a pure narration step
    # whose think block is dead weight (see config.narration_mode for the
    # "strict" vs "broad" measurement). A failure disables that shortcut,
    # because an error is exactly the case where the model should still be free
    # to think about a retry with corrected arguments.
    #
    # TWO KINDS OF FAILURE, and only one of them is permanent — this used to be
    # a single sticky `tools_failed` bool that was never reset, so one bad-args
    # TypeError (the common shape when a model hallucinates an argument name,
    # documented in registry.run_tool) disabled the narration win for every
    # remaining step of the turn, including a final step that is unambiguously
    # pure synthesis over clean results. The justification — "an error is when
    # the model most needs to reason about a corrected retry" — argues for the
    # NEXT step, not for a permanent latch.
    #
    #   failed_tools — RECOVERABLE. A tool that errored. If that same tool later
    #     returns a real result it is no longer outstanding, so the gate is
    #     computed as `not (failed_tools - tools_answered)`: the retry already
    #     happened and worked, and there is nothing left to reason about. An
    #     unknown tool never enters tools_answered, so it stays outstanding
    #     forever by construction rather than by special-casing.
    #   hard_failed — POLICY outcomes (blocked by safety, denied by the user at
    #     a confirmation card, called without being offered, a create_tool draft
    #     that could not be written). These are not retries that might succeed;
    #     the model has to find another way, which is thinking. Never cleared.
    tools_answered: set[str] = set()
    failed_tools: set[str] = set()
    hard_failed: set[str] = set()
    waived_tools: set[str] = set()
    attempted_tools: set[str] = set()
    tool_outcomes: list[tuple[str, object]] = []
    completed_effects: dict[str, str] = {}
    contract_force_tool: str | None = None

    def _record_outcome(name: str, result: str, *, planned: bool = False,
                        denied: bool = False, args: dict | None = None):
        outcome = classify_tool_outcome(name, result, planned=planned, denied=denied)
        attempted_tools.add(name)
        tool_outcomes.append((name, outcome))
        if outcome.status == "succeeded":
            if outcome.effect != "read" and args is not None:
                completed_effects[_action_fingerprint(name, args)] = result
            low = outcome.text.lower()
            for source, action, predicate, value in conditional_tools:
                if source != name:
                    continue
                if predicate == "percent_below":
                    m = re.search(r"(?:charge:\s*)?(\d{1,3})\s*%", low)
                    if m and int(m.group(1)) >= int(value):
                        waived_tools.add(action)
                elif predicate == "contains":
                    terms = tuple(str(x).lower() for x in value)
                    if not any(term in low for term in terms):
                        waived_tools.add(action)
                elif predicate == "rain_chance_above":
                    chances = [int(x) for x in re.findall(r"(\d{1,3})%\s+chance\s+of\s+rain", low)]
                    descriptions = re.sub(r"\d{1,3}%\s+chance\s+of\s+rain", "", low)
                    wet_words = re.search(r"\b(?:rain|showers?|storm|drizzle)\b", descriptions)
                    if chances and max(chances) <= int(value) and not wet_words:
                        waived_tools.add(action)
        return outcome

    def _unmet_group() -> frozenset[str] | None:
        covered = tools_answered | waived_tools
        return next((group for group in required_tool_groups
                     if not (group & covered)), None)

    def _unmet_candidates(group) -> set[str]:
        latest = dict(tool_outcomes)
        # A preview explicitly asks for a second call to commit the operation.
        # It must not exhaust the obligation's only eligible tool.
        previews = {name for name, outcome in latest.items() if outcome.status == "preview"}
        return set(group or ()) - (attempted_tools - previews)

    def _verified_final(text: str) -> str:
        from service.agent.verification import verify_delivery_claims
        text = verify_delivery_claims(text, tool_outcomes)
        actions = [(name, outcome) for name, outcome in tool_outcomes
                   if outcome.effect != "read"]
        if test_mode and actions:
            planned = ", ".join(dict.fromkeys(name for name, _ in actions))
            return (f"Dry run only — planned {planned}. No message, email, reminder, "
                    "file, calendar item, or setting was changed.")
        bad = [(name, outcome) for name, outcome in actions
               if outcome.status in {"denied", "failed", "needs_input"}]
        if bad and not any(outcome.status == "succeeded" for _, outcome in actions):
            name, outcome = bad[-1]
            return f"The requested action was not completed ({name}): {outcome.text}"
        if actions and all(name in {"move_path", "organize_files"} for name, _ in actions):
            receipts = [outcome.text for _, outcome in actions
                        if outcome.status in {"succeeded", "failed", "denied"}]
            if receipts:
                # Counts, skipped collisions and paths belong to the tool.
                # A model narration must not turn one move into "both moved".
                verified_moves = "\n".join(dict.fromkeys(receipts))
                if _unmet_group() is not None:
                    return verified_moves + "\nI couldn't complete every requested step."
                return verified_moves
        if reminder_action:
            created = [outcome for name, outcome in tool_outcomes
                       if name == "add_reminder" and outcome.status == "succeeded"]
            if not created:
                if reminder_action == "clarify_time":
                    request_text = " ".join(
                        str(m.get("content") or "") for m in messages
                        if m.get("role") == "user")
                    relative_to = (
                        "your move-in date" if re.search(r"\bmove[ -]?in\b", request_text, re.I)
                        else "the event" if re.search(
                            r"\b(?:appointment|meeting|event|reservation|date)\b",
                            request_text, re.I) else "")
                    suffix = (f", or how long before {relative_to}?"
                              if relative_to else "?")
                    return ("I haven’t created a reminder yet. What time should I "
                            "remind you" + suffix)
                return "I couldn't create the reminder. No reminder was added."
            if _unmet_group() is None and all(name == "add_reminder" for name, _ in actions):
                return created[-1].text
        return text

    def _prior_requirements_met(tool_name: str) -> bool:
        """Effect tools may not run before earlier source obligations."""
        covered = tools_answered | waived_tools
        for group in required_tool_groups:
            if tool_name in group:
                return True
            if not (group & covered):
                return False
        return True

    # ROUTER-DIRECT DISPATCH — calls the router resolved in full, run before the
    # first model call so the loop opens on what is already a narration step.
    #
    # Deliberately NOT reusing the step loop's inner execution block below. That
    # block also carries create_tool's draft-and-preview handling and the
    # allowed_names enforcement — all of which only mean something for a call
    # the MODEL made and chose the arguments for. Threading
    # a "this one came from the router" flag through all of it would be more
    # fragile than the block here. What the two DO share is the part that
    # matters: the same decide() -> approver -> run_tool() -> audit() path, so
    # safety tiers, confirmation cards and the audit log behave identically, and
    # the same tools_answered/failed_tools/hard_failed bookkeeping that feeds the
    # narration gate.
    for _name, _args in (direct_calls or []):
        _tool = get_tool(_name)
        if _tool is None:  # a roster/registry mismatch must not kill the turn
            continue
        _cid = f"direct_{_name}"
        _dec = decide(_tool.category, _args, tool=_name)
        await emit({"type": "tool_call", "id": _cid, "name": _name, "args": _args,
                    "decision": _dec.tier.value, "reason": _dec.reason,
                    **({"test_mode": True} if test_mode else {})})
        if test_mode:
            # Same contract as the loop's own dry run: report what WOULD happen,
            # execute nothing (see _TEST_MODE_STUB / the endpoint docstring).
            _result = _TEST_MODE_STUB
        elif _dec.tier is Tier.DENY:
            _result = f"BLOCKED by safety policy: {_dec.reason}"
            audit("deny", tool=_name, args=_args, reason=_dec.reason)
        elif _dec.tier is Tier.CONFIRM:
            # lock_screen is system_write and run_speed_test is network_active —
            # both CONFIRM tier, so a direct call is NOT a way around the card.
            _action = {"id": _cid, "tool": _name, "args": _args,
                       "reason": _dec.reason,
                       "fingerprint": _action_fingerprint(_name, _args)}
            # Router-direct bulk reminder calls bypass the model-driven block
            # that normally builds confirmation previews. Preserve the same
            # safety contract here: the card must show the exact reminder set
            # selected by the same helper the tool will execute.
            if _name == "clear_reminders":
                from service.tools.assistant_tools import reminders_matching
                _scope = str(_args.get("scope", "all"))
                _items = reminders_matching(_scope, _args.get("query", ""))
                _rows = [
                    f"{datetime.fromtimestamp(c['when_ts']):%a %b %-d, %Y}"
                    f"  {c['title']}" for c in _items]
                _action["reason"] = (
                    f"permanently deletes {len(_rows)} reminder(s) in "
                    f"scope '{_scope}' — always confirmed")
                _action["preview"] = (
                    "\n".join(_rows[:60])
                    + (f"\n…and {len(_rows) - 60} more"
                       if len(_rows) > 60 else ""))
            if await approver.confirm(_action):
                _result = await run_tool(_tool, _args)
                audit("confirm_allow", tool=_name, args=_args)
            else:
                _result = "The user denied this action."
                audit("confirm_deny", tool=_name, args=_args)
        else:
            _result = await run_tool(_tool, _args)
            audit("allow", tool=_name, args=_args)
        await emit({"type": "tool_result", "id": _cid, "result": _result[:20000]})
        _direct_outcome = _record_outcome(
            _name, _result, planned=test_mode, args=_args,
            denied=(_dec.tier is Tier.DENY or
                    (_dec.tier is Tier.CONFIRM and _result == "The user denied this action.")))
        # A tool message must reference a tool_call_id from a PRECEDING assistant
        # message or the chat template sees a malformed transcript, so synthesize
        # the assistant turn the model would have produced. This also makes the
        # call visible to _fit_window's budgeting, which counts tool_calls
        # payloads (see tests/test_fit_window.py).
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": _cid, "type": "function",
             "function": {"name": _name, "arguments": json.dumps(_args)}}]})
        msgs.append({"role": "tool", "tool_call_id": _cid,
                     "content": _fit_tool_result(_result, _name)})
        if _result.strip():
            last_tool_result = _result
            last_tier = _dec.tier
            if _dec.tier is not Tier.DENY and not is_tool_error(_result):
                clean_results.append((_name, _result))
        # Feed the narration gate exactly as the model-driven path does. A DENY —
        # including a confirmation card the user dismissed or that timed out — is
        # a policy outcome that must keep thinking ON, so the next step never
        # narrates as though a call that never ran had succeeded.
        if _direct_outcome.status == "denied":
            hard_failed.add(_name)
            if _direct_outcome.effect != "read":
                if _name in _TERMINAL_OUTBOUND_DENIALS:
                    return await _finish_denied_outbound(emit)
                response = "The requested action was not completed because approval was denied."
                await emit({"type": "text", "text": response})
                return response
        elif _direct_outcome.status in {"failed", "needs_input", "no_match"}:
            failed_tools.add(_name)
            if _direct_outcome.effect != "read":
                response = "The action did not return a verified success. I stopped without retrying.\n" + _result
                await emit({"type": "text", "text": response})
                return response
        elif _direct_outcome.status in {"succeeded", "planned"}:
            tools_answered.add(_name)
        if _direct_outcome.status == "succeeded" and _name == "draft_message":
            await emit({"type": "message_draft",
                        "to": str(_args.get("to") or ""),
                        "text": str(_args.get("text") or "")})
            return ""
        if (_direct_outcome.status == "denied"
                and _name in _TERMINAL_OUTBOUND_DENIALS):
            return await _finish_denied_outbound(emit)
        # Typed workflow source groups are singletons. Once one of those
        # required reads fails, later contact lookups cannot make the payload
        # complete and only add latency. Stop dispatching downstream reads;
        # the terminal response below reports the source failure.
        if (_direct_outcome.effect == "read"
                and _direct_outcome.status in {"failed", "no_match", "needs_input"}
                and frozenset({_name}) in required_tool_groups):
            break

    # A deterministic source workflow must never continue to its outbound
    # effect with a missing or partial source. This used to let one successful
    # quote out of four become a message containing three invented "price not
    # retrieved" placeholders. Direct source failures are terminal for this
    # revision: the user can retry the request, but no partial draft/send is
    # constructed and no extra model rounds are spent retrying blindly.
    required_direct_failure = next((
        (name, outcome) for name, outcome in tool_outcomes
        if outcome.effect == "read"
        and outcome.status in {"failed", "no_match", "needs_input"}
        and any(name in group and not (group & tools_answered)
                for group in required_tool_groups)
    ), None)
    if required_direct_failure is not None:
        name, outcome = required_direct_failure
        if outcome.status == "no_match":
            response = outcome.text
        elif name == "get_stock_price":
            response = ("I couldn’t retrieve every requested stock price, so I "
                        "didn’t prepare or send a partial update. Please try again.")
        else:
            response = (f"I couldn’t retrieve the required {name} source, so I "
                        "didn’t prepare or send a partial result.")
        await emit({"type": "text", "text": response})
        await emit({"type": "done"})
        return response

    # A strict lookup's empty result is already the complete grounded answer.
    # A narration pass previously turned an empty PlayStation lookup into an
    # unrelated inbox summary, so this result intentionally bypasses the model.
    if (direct_calls and len(direct_calls) == 1
            and direct_calls[0][0] == "view_emails"
            and direct_calls[0][1].get("strict_match")
            and last_tool_result.startswith("No emails matching")):
        await emit({"type": "text", "text": last_tool_result})
        await emit({"type": "done"})
        return last_tool_result

    # The direct call already IS the finished answer — same premise as the
    # step loop's short_circuit_tools below (summarize_emails/summarize_messages
    # synthesize real prose internally), so the turn ends here with ONE model
    # call total: the summarizer's own. Every guard mirrors that block: a single
    # call, ALLOW tier, a real non-error result, and not a multi_round route
    # where other sources are still required.
    if (direct_calls and not test_mode and short_circuit_tools
            and len(direct_calls) == 1
            and direct_calls[0][0] in short_circuit_tools
            and not multi_round
            and _unmet_group() is None
            and last_tier is Tier.ALLOW and last_tool_result.strip()
            and not is_tool_error(last_tool_result)):
        await emit({"type": "text", "text": last_tool_result})
        await emit({"type": "done"})
        return last_tool_result

    for _step in range(max_steps):
        await client.ensure_only(model, exclusive=exclusive, emit=emit)
        contract_candidate = None
        if (unmet_now := _unmet_group()) is not None:
            candidates_now = _unmet_candidates(unmet_now)
            contract_candidate = next(
                (s["function"]["name"] for s in schemas
                 if s["function"]["name"] in candidates_now),
                sorted(candidates_now)[0] if candidates_now else None)
        forced_tool = (contract_force_tool or contract_candidate
                       or (force_first_tool if force_first_tool and _step == 0
                           and force_first_tool not in tools_answered else None))
        forcing_first_step = bool(forced_tool)
        # expecting_tool: the router said this request NEEDS a tool, but we don't
        # restrict WHICH one (unlike force_first_tool). the agent model sometimes answers
        # from memory / says "I can't" instead of calling the obvious tool — this
        # nudge-and-retry corrects that on the first step. This is the fix for
        # "doesn't use its tools when it should".
        expecting_tool = expect_tool_first and _step == 0 and not forcing_first_step
        if forcing_first_step:
            # An obligation group contains alternatives, not an arbitrary
            # mandatory first member (e.g. list_dir vs find_files).
            alternatives = (set(unmet_now) if unmet_now and forced_tool in unmet_now
                            and not contract_force_tool else {forced_tool})
            step_schemas = [s for s in schemas if s['function']['name'] in alternatives]
        else:
            step_schemas = schemas
        choice = "required" if (forcing_first_step or expecting_tool) else "auto"
        # NARRATION STEP: the model's only remaining job is to write prose
        # about tool results already in its context, not to select a tool.
        # Skip the think block for it — measured 19.9s -> 3.3s for the same
        # answer (see config._narration_mode for the full measurement and the
        # "strict" vs "broad" distinction below).
        #
        # Conditions are deliberately narrow, because the SAME suppression on a
        # tool-SELECTION step measures 0/3 tool calls (see no_thinking_kwargs):
        #   - a scoped route only (`tools is not None`). On the unscoped route
        #     "every offered tool has answered" is meaningless — nobody calls
        #     all 55 — so the condition could only be satisfied by accident.
        #   - not while forcing/expecting a tool: those steps are selection by
        #     definition, and greedy + no-thinking is the exact combination
        #     that produced the 0/3.
        #   - no tool errored this turn: an error is when the model most needs
        #     to reason about a corrected retry.
        #   - not on a `multi_round` route (see the param's docstring above):
        #     those need the model to keep track of which of SEVERAL required
        #     tools it hasn't called yet, which is exactly the reasoning
        #     "broad" mode removes. UNLESS every tool in `narration_after` has
        #     answered — at that point the route's requirement is met, there is
        #     nothing left to keep track of, and the remaining work is pure
        #     synthesis. Measured on the aggregate to-do route, which calls all
        #     four sources in step 0: its step-1 narration was still spending
        #     2,415 characters of reasoning / 14.85s deciding nothing.
        #
        # "strict" vs "broad" is what OFFERED means:
        #   strict — every tool OFFERED this step must have answered. Fires on
        #     only 1-tool routes (~2 of 15 router subsets) — see the mode's
        #     docstring in config.py for why that's a real coverage gap.
        #   broad  — every tool the model has CALLED so far must have answered
        #     cleanly; uncalled offered alternatives don't block it.
        #     `tools_answered`/`failed_tools`/`hard_failed` already track
        #     exactly "called and clean" vs "called and failed", so the
        #     conjunction below IS "called ⊆ answered" with no separate
        #     tracking needed. A RECOVERABLE failure that the model has since
        #     retried successfully no longer counts against it — see the sets'
        #     comment above for why that distinction matters.
        #
        # tool_choice stays "auto" in every mode, so even here the model can
        # still call something if it disagrees — this only removes the
        # monologue, never an option.
        mode = narration_mode()
        narrating = (
            mode != "off"
            and tools is not None
            and not (forcing_first_step or expecting_tool)
            and (not multi_round or _unmet_group() is None
                 or bool(narration_after and narration_after <= tools_answered))
            and not hard_failed
            and not (failed_tools - tools_answered)
            and bool(tools_answered)
            and (mode == "broad"
                 or {s["function"]["name"] for s in step_schemas} <= tools_answered)
        )
        # Sampling is the model's own (as configured in oMLX) on every step
        # EXCEPT tool-SELECTION, which is pinned greedy. Higher temperature
        # increases variance away from the single most-likely — structured,
        # correct — continuation, exactly the wrong property when the model
        # MUST emit a tool call rather than wander into free text.
        #
        # Re-measured 2026-08-08 after this pin was briefly removed so oMLX's
        # per-model profile would apply everywhere: on Agents-A1-4B at its
        # configured temperature 0.85 / top_p 0.95, a `required` first step
        # failed to produce a tool call on ALL THREE attempts, burning 38s and
        # answering with a clarifying question instead. The third attempt even
        # opened "I understand you want me to use tools, but…" — it saw the
        # constraint and declined anyway. The retry budget alone does not cover
        # this: re-sampling a model that has decided to ask a question just
        # produces the same question again.
        #
        # Deliberately narrow: only the selection step. Narration steps
        # (choice="auto", after a tool has run) and every non-agent caller keep
        # the user's configured sampling untouched, so prose stays warm.
        step_temperature = 0.0 if choice == "required" else temperature
        # The names actually OFFERED to the model this step. Restricting the
        # schema alone ("a function absent from the schema can't be called")
        # turned out not to be a real guarantee — verified live: the summarizer, given
        # only get_upcoming, still emitted a tool_call for add_reminder (with
        # hallucinated argument names, since it was never given that
        # function's real signature either). get_tool() below does a GLOBAL
        # registry lookup with no awareness of what was offered this turn, so
        # that call would have gone on to execute — bypassing the entire point
        # of restricting the summarizer's light-read routes to a narrow, verified-safe
        # toolset. allowed_names is checked before execution below to actually
        # enforce the restriction the schema was supposed to guarantee.
        # The schemas as OFFERED, before any window trimming. _fit_window runs
        # per ATTEMPT below and must always start from this list, never from a
        # previous attempt's already-trimmed one — otherwise a step that had to
        # drop a tool once would keep dropping, and the toolset would shrink
        # with every retry.
        offered_schemas = step_schemas
        allowed_names = {s["function"]["name"] for s in step_schemas}
        step_msgs: list[dict] = []
        step_max_tokens = max_tokens

        msg: dict = {}
        reasoning = ""
        # Set when the previous attempt came back empty (an unparseable
        # response — see the note below), so the next one re-samples at a
        # higher temperature.
        retry_for_empty = False
        # Up to 2 retries (3 attempts) for both the forced-specific-tool case
        # and the softer "needs some tool" case. expecting_tool used to get
        # only 1 retry (2 attempts) — verified live that wasn't always enough:
        # on a light-read follow-up for a domain already answered earlier in
        # the conversation (e.g. "and my calendar" after "check my messages"),
        # the summarizer sometimes still hadn't called the tool by its LAST attempt,
        # producing confused self-referential text ("I apologize... it seems I
        # retrieved...") as the accepted final answer instead of ever running
        # the tool. The one extra attempt gives it another real chance.
        # Floor of 2 even on an ordinary step, purely so the empty-response
        # retry below has a budget to spend. It costs nothing when the model
        # answers normally: a non-empty response breaks out on the first pass.
        attempts = 3 if (forcing_first_step or expecting_tool) else 2
        for _attempt in range(attempts):
            # BUILD THE REQUEST INSIDE THE ATTEMPT LOOP, NOT ABOVE IT.
            #
            # This is what makes the nudge-and-retry ladder below actually a
            # retry rather than a repeat. The correcting nudge ("you must call
            # the tool now") is appended to `msgs`; _fit_window returns a COPY
            # (`msgs = list(msgs)`), so while this fitting happened once above
            # the loop, every attempt re-sent the same `step_msgs` and the nudge
            # never reached the model. A forced step is also pinned to
            # temperature 0.0, so an identical request decodes to an identical
            # response by construction — attempts 2 and 3 could not have
            # differed from attempt 1 even in principle. That is the mechanism
            # behind the failure recorded in config.py: "a `required` first step
            # failed to produce a tool call on ALL THREE attempts, burning 38s",
            # with the third attempt opening "I understand you want me to use
            # tools, but…". See tests/test_retry_nudge.py.
            #
            # Re-fitting (rather than just appending the nudge to step_msgs) is
            # deliberate: the request GROWS by two messages per attempt, and the
            # window check has to be redone against what is actually being sent.
            #
            # Make the request FIT the model's context window before sending it.
            # Nothing did this before: the loop assembled a request and hoped,
            # and oMLX answered a too-long one with a bare `400 Bad Request`
            # that surfaced to the user as an unexplained error mid-task.
            #
            # Reported live on "Summarize a PDF": the unscoped route offered 57
            # tools = 9,943 tokens, 62% of a 16,000 window, before a single
            # message. Four list_dir results later the prompt crossed 16,000 and
            # the turn died. The tools were the bulk of it, so trimming history
            # alone could never have saved it.
            step_msgs, step_schemas, step_max_tokens = _fit_window(
                msgs, offered_schemas, max_tokens, model, forced_tool)
            # Keep enforcement in sync with what was actually offered — a tool
            # dropped to fit must be rejected if the model calls it anyway,
            # exactly like one that was never offered.
            allowed_names = {s["function"]["name"] for s in step_schemas}
            # Verify before publication. Otherwise even a read-only route can
            # stream a false "delivered" before the receipt checker retracts it.
            stream = False
            # Retrying an unparseable (empty) response at a GREEDY temperature
            # is pointless — decoding is deterministic, so the retry reproduces
            # the identical unparseable token sequence (verified in oMLX's log,
            # two byte-identical failures a second apart). Nudging up
            # re-samples, which is what gets past whatever caused the parse
            # failure. Only reachable when a caller explicitly forced a temperature; on
            # the normal path (None) oMLX's own configured sampling is already
            # stochastic, so the plain retry re-samples on its own and Wisp
            # must not inject a number over the user's tuning to achieve that.
            attempt_temperature = step_temperature
            if retry_for_empty and step_temperature is not None:
                attempt_temperature = step_temperature + 0.7
            msg, reasoning, content_streamed = await _run_step(
                client, model, step_msgs, step_schemas, choice, step_max_tokens, emit,
                stream_content=stream, temperature=attempt_temperature,
                no_thinking=narrating, debug=debug)

            # A completely empty response — no content, no reasoning, no tool
            # calls — is not the model deciding to say nothing. It means oMLX's
            # parser failed to PARSE what the model emitted and dropped it.
            # Originally diagnosed under gpt-oss: oMLX's log showed the model
            # producing a perfectly correct harmony-format call, `commentary
            # to=functions.create_tool<|constrain|>json<|message|>{...}`,
            # rejected with "unexpected tokens remaining in message header" —
            # the working calls had a space before <|constrain|> and this one
            # didn't, a tokenization quirk that varied per tool name and per
            # sample. gpt-oss is no longer rostered; Agents-A1 (qwen3 lineage,
            # qwen3_coder tool-call format — no <|channel|>/harmony tokens at
            # all) has not reproduced this specific cause, but an empty
            # response is still treated as a parse failure rather than an
            # intentional silence, on the same logic: retrying re-samples and
            # almost always parses, whereas accepting the empty message
            # silently ends the turn mid-flow — which looked exactly like "the
            # model ignored its tools".
            # NOTE: reasoning is deliberately NOT part of this test. It used to
            # require reasoning to be empty too, which missed the commonest
            # shape of this failure: the analysis block parses fine and only
            # the tool call is dropped. Verified in a user's debug export —
            # asked to "create a tool to do so", the agent model reasoned exactly
            # "Need create_tool." and returned no call and no content. Because
            # reasoning was non-empty the retry never fired, the step fell
            # through with nothing, and the next step produced a flat "I can't
            # do that" — the model had decided to build the tool and the user
            # was told the opposite. Reasoning is not user-visible output: a
            # step with no content AND no tool call has produced nothing,
            # whatever it thought on the way there.
            if (not msg.get("tool_calls") and not (msg.get("content") or "").strip()
                    and not msg.get("_degenerate")
                    and _attempt < attempts - 1):
                # THE ANSWER IS EMPTY BUT SOMETHING MAY ALREADY BE ON SCREEN.
                # This is the shape of the worst user-visible failure in the
                # 2026-08-09 report: the model opened a <think> block, hit the
                # token ceiling before closing it, and oMLX therefore streamed
                # 11,859 characters of private monologue as CONTENT deltas.
                # `_demote_unclosed_think` correctly reclassifies it in the
                # final message — which is why we're here with empty content —
                # but the deltas had already gone out, and the user read
                # "We need to give an update to the user's mom... We need to
                # review the conversation history" as Wisp's reply, with the
                # real answer appended after it by the retry.
                #
                # Nothing can un-send a delta, so tell the client to DISCARD
                # what it rendered for this step before the retry replaces it.
                if content_streamed:
                    await emit({"type": "clear_answer"})
                retry_for_empty = True
                continue
            retry_for_empty = False

            if msg.get("tool_calls") or not (forcing_first_step or expecting_tool):
                break
            # Model replied in text instead of calling a tool — nudge and retry.
            #
            # Only discard what was shown if there is actually another attempt
            # coming. On the LAST attempt this text IS the accepted answer:
            # `stream` is True there precisely so it reaches the user, and the
            # `not tool_calls` branch below deliberately does NOT re-emit it
            # because it already streamed. Clearing it unconditionally deleted
            # the reply and the user got an empty response — caught by a smoke
            # test right after this guard was added, on "summarize my messages
            # from this month": 23 delta events, one clear_answer, empty reply.
            if content_streamed and _attempt < attempts - 1:
                await emit({"type": "clear_answer"})
            msgs.append({"role": "assistant", "content": msg.get("content") or ""})
            if forcing_first_step:
                nudge = (f"You must call the {forced_tool} tool now — "
                         "do not reply without calling it.")
            else:
                nudge = ("That request requires an action you can only do by "
                         "calling a tool (e.g. get_upcoming for the calendar or "
                         "schedule, summarize_emails for email, list_dir/read_file "
                         "for files, search_notes for notes, run_shell to run "
                         "something). Call the appropriate tool now — do NOT answer "
                         "from memory and do NOT say you can't.")
            msgs.append({"role": "user", "content": nudge})
        tool_calls = msg.get("tool_calls")

        # Correct a specific failure mode: oMLX can return content AND
        # tool_calls in the SAME response (verified live — the summarizer/the agent model both
        # do this, typically an "I'll check that / let me look into it"-style
        # preamble before the actual call). Since content streams live as it
        # arrives, before the response — and whether it includes tool_calls —
        # is fully known, that preamble was already shown to the user by the
        # time we get here. It is NOT the real answer (a tool is about to run
        # and either short-circuit with the tool's own result, or a later step
        # narrates it) — left alone, the UI ends up showing the stray preamble
        # immediately followed by the real answer concatenated after it,
        # reading like a single confused, "I apologize, let me actually do
        # this correctly..." reply. clear_answer tells the client to discard
        # whatever it already rendered from this step before the tool result
        # (or next step's narration) lands.
        if tool_calls and content_streamed:
            await emit({"type": "clear_answer"})

        if not tool_calls:
            text = msg.get("content") or ""
            unmet = _unmet_group()
            candidates = _unmet_candidates(unmet)
            if unmet is not None and candidates and _step < max_steps - 1:
                if content_streamed:
                    await emit({"type": "clear_answer"})
                # Restrict the next step to one still-unmet tool. Prefer the
                # route's advertised order so dependencies remain natural.
                contract_force_tool = next((s["function"]["name"] for s in schemas
                                            if s["function"]["name"] in candidates),
                                           sorted(candidates)[0])
                msgs.append({"role": "assistant", "content": text})
                msgs.append({"role": "user", "content": (
                    f"The request is not complete yet. Call {contract_force_tool} now. "
                    "Do not claim completion until every requested clause has a successful result.")})
                continue
            if reminder_action:
                # This route is buffered: no unverified "I've set it" can
                # flash on screen before the tool-result check retracts it.
                if unmet is not None:
                    text = "I couldn't complete every requested step. Still missing: " + ", ".join(sorted(unmet)) + "."
                text = _verified_final(text)
                await emit({"type": "text", "text": text})
                await emit({"type": "done"})
                return text
            if unmet is not None:
                if content_streamed:
                    await emit({"type": "clear_answer"})
                text = ("I couldn't complete every requested step. Still missing one of: "
                        + ", ".join(sorted(unmet)) + ".")
            # The step's "answer" was actually its own truncated chain-of-
            # thought (see _demote_unclosed_think). content has already been
            # blanked, but on this path it STREAMED as it was generated, so the
            # user is currently looking at the monologue — retract it before
            # falling through to the real fallbacks below. Verified live
            # 2026-08-08: "anything from earlier today?" ran out of budget mid-
            # thought and shipped 3,597 characters of "We need to answer the
            # user's question... maybe get_upcoming for today? But they asked"
            # as Wisp's reply.
            if msg.get("_think_leak") and content_streamed:
                await emit({"type": "clear_answer"})
            # Empty final answer but a tool already produced user-ready text this
            # turn (see last_tool_result) — surface that instead of returning
            # nothing. It wasn't streamed (the model emitted no content), so emit
            # it as a `text` event now. NOT when last_tool_result is itself an
            # error string (bad args, or the tool raised) — that's formatted for
            # the MODEL to read and retry from, not a real answer for the user;
            # showing it verbatim reads as Wisp reporting a Python traceback as
            # its response. Fall through to the plain-text branch below instead.
            if not text.strip() and last_tool_result:
                # Every clean source, not just the last one — see clean_results.
                text = (_merge_results(clean_results) if clean_results
                        else _STUCK_MESSAGE)
            elif not text.strip() and msg.get("_think_leak"):
                # Nothing streamed that's worth keeping and no tool ran, so
                # after the retraction above there is literally nothing on
                # screen. Say what happened in the user's terms rather than
                # ending the turn blank — the request is answerable, the model
                # just spent its whole token budget thinking about it.
                text = _TRUNCATED_MESSAGE
            elif not text.strip():
                # Empty for none of the reasons above — see _EMPTY_MESSAGE.
                # This is the floor: past here nothing else can fire, so the
                # turn either says this or says nothing at all.
                if content_streamed:
                    await emit({"type": "clear_answer"})
                text = _EMPTY_MESSAGE
            # Publish once, after checking receipts and obligations.
            verified = _verified_final(text)
            if verified != text:
                if content_streamed:
                    await emit({"type": "clear_answer"})
                text = verified
            await emit({"type": "text", "text": text})
            if reasoning:
                await emit({"type": "reasoning", "text": reasoning})
            await emit({"type": "done"})
            return text

        contract_force_tool = None
        msgs.append({"role": "assistant", "content": msg.get("content") or "",
                     "tool_calls": tool_calls})

        # BATCHED CONFIRMATION for calendar_write (cancel_event /
        # add_calendar_event). When a step proposes SEVERAL of these at once
        # — the shape a bulk "clear my calendar except X and Y" instruction
        # takes — the per-call loop below would otherwise raise one
        # confirmation card per call, which is not what "a confirmation
        # button" means to a user watching 14 of them appear in a row. This
        # collects every calendar_write call in the step into ONE card
        # listing every proposed change before any of them run; the per-call
        # loop still does its own decide()/execute for each (unchanged), it
        # just skips prompting again for a cid this pre-pass already
        # answered. A step with only ONE such call is left alone — it falls
        # through to the ordinary per-item CONFIRM path with its own preview
        # (see confirm_preview below), which is already the right UI for a
        # single change. See policy._ALWAYS_CONFIRM_CALENDAR for why these
        # need confirming at all — verified 2026-08-18, an unconfirmed bulk
        # cancel wrongly removed a kept event and fabricated a wrongly-dated
        # duplicate, with no human checkpoint of any kind.
        batch_verdict: dict[str, bool] = {}
        _batch_calls = [
            (tc.get("id", ""), _clean_tool_name(tc["function"]["name"]),
             _parse_args(tc["function"].get("arguments", "")))
            for tc in tool_calls
            if _clean_tool_name(tc["function"]["name"]) in ("cancel_event", "add_calendar_event")
            and _clean_tool_name(tc["function"]["name"]) in allowed_names
        ]
        if len(_batch_calls) > 1:
            _lines = []
            for _, _bname, _bargs in _batch_calls:
                if _bname == "cancel_event":
                    _lines.append(f"Cancel: {_bargs.get('title', '?')}")
                else:
                    _lines.append(f"Add: {_bargs.get('title', '?')} — "
                                  f"{_bargs.get('when_iso', '?')}")
            _batch_action = {
                "id": "batch_calendar", "tool": "calendar_changes", "args": {},
                "reason": f"changes your calendar in {len(_batch_calls)} ways — "
                          "always confirmed",
                "preview": "\n".join(_lines),
            }
            _batch_approved = await approver.confirm(_batch_action)
            for _bcid, _, _ in _batch_calls:
                batch_verdict[_bcid] = _batch_approved

        for tc in tool_calls:
            cid = tc.get("id", "")
            name = _clean_tool_name(tc["function"]["name"])
            args = _parse_args(tc["function"].get("arguments", ""))
            # A typed workflow owns identity/channel/time. The model owns only
            # the grounded prose it synthesizes from source results. Overlay
            # fixed values before grounding, confirmation previews and tool
            # execution so a locally generated call cannot redirect an action.
            if fixed := (tool_argument_bindings or {}).get(name):
                args = {**args, **fixed}

            if name in forbidden_tools:
                result = (f"({name} is explicitly forbidden by the user's constraints for "
                          "this request. Do not call it and do not claim it ran.)")
                audit("reject_forbidden", tool=name, args=args)
                hard_failed.add(name)
                await emit({"type": "tool_result", "id": cid, "result": result})
                msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
                continue

            if name in _EFFECT_TOOLS and name in tools_answered:
                result = (f"({name} already succeeded earlier this turn; it was NOT run "
                          "again. Narrate the existing result instead of duplicating the action.)")
                audit("reject_duplicate_effect", tool=name, args=args)
                await emit({"type": "tool_result", "id": cid, "result": result})
                msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
                continue

            if name in _EFFECT_TOOLS and not _prior_requirements_met(name):
                result = (f"({name} is premature: required source steps have not succeeded "
                          "yet. Complete those reads first, then retry this exact action. "
                          "Do not claim the action ran.)")
                audit("reject_ungrounded_order", tool=name, args=args)
                failed_tools.add(name)
                await emit({"type": "tool_result", "id": cid, "result": result})
                msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
                continue

            # Enforce the restriction the schema was supposed to guarantee
            # (see allowed_names' comment above) BEFORE any registry lookup or
            # execution — a real, registered tool that just wasn't offered
            # this turn must be rejected exactly like an unknown one, not
            # executed. The message explicitly tells the model to say so
            # rather than claim success: the observed failure mode wasn't
            # just an unauthorized call, it was the model going on to report
            # "done!" in its final answer despite the tool never running.
            if name not in allowed_names:
                # WITHHELD BY A FORCED STEP, NOT UNAVAILABLE. When this step
                # narrowed the offer to a single tool (force_first_tool),
                # everything else in the turn's toolset is still coming — it is
                # offered again the moment the forced step is done. Telling the
                # model it "isn't available" there is a lie that ends the turn:
                # verified live on a run_shell call the route HAD granted but the
                # step had narrowed away — the model read this message and
                # answered "I don't have the ability to search your file system"
                # — while the same request split into single steps worked fine.
                # Say what's actually true and keep it recoverable, so the retry
                # on the next step counts.
                if forcing_first_step and name in turn_tool_names:
                    result = (f"({name} is not callable on this step — it IS available, "
                              f"just not yet. Call the tool you were offered now; "
                              f"{name} can be called on the next step. Do NOT tell the "
                              "user this action is unavailable and do NOT say you "
                              "performed it.)")
                    audit("reject_premature", tool=name, args=args)
                    failed_tools.add(name)
                else:
                    result = (f"({name} is not available for this request — do NOT say you "
                              "performed this action. Tell the user this specific action "
                              "isn't available right now.)")
                    audit("reject_unoffered", tool=name, args=args)
                    # Keep thinking for the rest of the turn: the model reached
                    # for something it wasn't given and now has to find another
                    # way.
                    hard_failed.add(name)
                await emit({"type": "tool_result", "id": cid, "result": result})
                msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
                continue

            tool = get_tool(name)

            if tool is None:
                result = f"(unknown tool: {name})"
                # Recoverable set, but it can never clear — an unknown tool
                # cannot enter tools_answered — so this stays outstanding.
                failed_tools.add(name)
                await emit({"type": "tool_result", "id": cid, "result": result})
                msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
                continue

            # DRY RUN: report the call and stop — do not decide/confirm/run it.
            # Checked before the debug_capture block below on purpose: that
            # block's first job (create_tool's prepare_draft) is itself a real
            # model call that writes a code draft, which is exactly the kind
            # of actual output test mode promises not to produce. `dec` is
            # still computed so the plan can say what WOULD have happened
            # (auto-run vs. needing confirmation vs. blocked outright) without
            # ever reaching approver.confirm() or run_tool().
            #
            # Also checked before _validate_args and the completed_effects
            # dedupe below. Both exist to steer a REAL execution — argument
            # validation hands the model an error to retry from, and the dedupe
            # protects against running the same effect twice — and neither has
            # anything to guard when nothing executes. Ordered after them, an
            # imperfect argument list silently swallowed the plan: the step
            # emitted a tool_result with NO matching tool_call (a malformed
            # transcript on its own), the tool never entered tools_answered, and
            # an execution contract then ended the dry run with "I couldn't
            # complete every requested step" — a real-execution failure message
            # for a turn in which, by construction, nothing was ever attempted.
            if test_mode:
                dec = decide(tool.category, args, tool=name)
                result = _TEST_MODE_STUB
                await emit({"type": "tool_call", "id": cid, "name": name, "args": args,
                            "decision": dec.tier.value, "reason": dec.reason,
                            "test_mode": True})
                await emit({"type": "tool_result", "id": cid, "result": result})
                msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
                _record_outcome(name, result, planned=True)
                tools_answered.add(name)
                continue

            fingerprint = _action_fingerprint(name, args)
            from service.tools.registry import _validate_args
            if problem := _validate_args(tool, args):
                await emit({"type": "tool_result", "id": cid, "result": problem})
                msgs.append({"role": "tool", "tool_call_id": cid, "content": problem})
                failed_tools.add(name)
                continue
            if fingerprint in completed_effects:
                result = ('Already completed in this turn; not executed again.\n'
                          + completed_effects[fingerprint])
                await emit({"type": "tool_result", "id": cid, "result": result})
                msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
                continue

            # Captures anything the tool itself records about its own
            # internals via debug_capture.record() while it runs — e.g.
            # summarize_emails/summarize_messages record the raw source lines
            # fed to their own internal model call plus that call's raw
            # request/response, and create_tool's prepare_draft (right below)
            # records ITS OWN code-generation call. Opened HERE, before
            # create_tool's draft generation, not just around the dec.tier
            # execution further down — prepare_draft runs its own model call
            # before the tool's allow/confirm/deny decision even exists, so a
            # capture scope starting any later would silently miss it. Empty
            # for tools that don't record
            # anything (most of them), in which case it's dropped from the
            # emitted event below rather than sent as a pointless empty list.
            with debug_capture.capture() as dbg:
                # create_tool's whole safety story is "the user reviews the
                # actual code before it's installed", so the code must exist
                # BEFORE the confirmation card is raised — the card is shown
                # and answered before any tool runs. Generating here also
                # means a tool that couldn't be written never becomes a
                # prompt to approve it. (Previously prepare_draft was never
                # called at all: draft_code returned "" and the card asked
                # the user to approve installing code it showed them nothing
                # of.)
                if name == "create_tool":
                    from service.tools.tool_authoring import prepare_draft
                    draft_err = await prepare_draft(args)
                    if draft_err:
                        result = f"(couldn't create this tool: {draft_err})"
                        audit("draft_failed", tool=name, args=args, reason=draft_err)
                        hard_failed.add(name)
                        await emit({"type": "tool_result", "id": cid, "result": result,
                                    **({"debug": dbg} if dbg else {})})
                        msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
                        continue

                dec = decide(tool.category, args, tool=name)
                await emit({"type": "tool_call", "id": cid, "name": name,
                            "args": args, "decision": dec.tier.value, "reason": dec.reason})

                if dec.tier is Tier.DENY:
                    result = f"BLOCKED by safety policy: {dec.reason}"
                    audit("deny", tool=name, args=args, reason=dec.reason)
                elif dec.tier is Tier.CONFIRM:
                    action = {"id": cid, "tool": name, "args": args,
                              "reason": dec.reason,
                              "fingerprint": _action_fingerprint(name, args)}
                    # Installing a self-authored tool is the one confirm where the
                    # args don't describe what's actually at stake — `{"name": "x"}`
                    # says nothing about the code about to be written. Attach the
                    # real code so the card can show it; pulled from the pending
                    # draft rather than from args, so what's reviewed is what's
                    # installed.
                    if name == "create_tool":
                        from service.tools.tool_authoring import (
                            draft_code, draft_scope_line, draft_warning)
                        tname = str(args.get("name", ""))
                        action["preview"] = draft_code(tname)
                        # Lead with how far the tool can reach on disk, then any
                        # privileged capability it asks for. Both belong in the
                        # headline — a user approving "install a tool" should not
                        # have to infer either from the code body.
                        bits = [b for b in (draft_scope_line(tname),) if b]
                        if (warn := draft_warning(tname)):
                            bits.insert(0, f"⚠ This tool {warn}.")
                        if bits:
                            action["reason"] = " ".join(bits) + f" {dec.reason}"
                    # A single calendar_write call (the batch pre-pass above
                    # only fires for 2+ in the same step) — still gets a real
                    # preview rather than just the generic reason text.
                    elif name == "cancel_event":
                        action["preview"] = f"Cancel: {args.get('title', '?')}"
                    elif name == "add_calendar_event":
                        action["preview"] = (f"Add: {args.get('title', '?')} — "
                                             f"{args.get('when_iso', '?')}")
                    elif name == "clear_past_reminders":
                        # A bulk delete has to show its whole list, not a
                        # count: the user asked for "old reminders" and only
                        # they can tell which of these they are actually done
                        # with. Reads through the same helper the tool itself
                        # uses, so the card and the deletion cannot disagree
                        # about what is in scope.
                        from service.tools.assistant_tools import past_due_matching
                        _items = past_due_matching(args.get("query", ""),
                                                   args.get("days", 365))
                        _rows = [
                            f"{datetime.fromtimestamp(c['when_ts']):%a %b %-d, %Y}"
                            f"  {c['title']}" for c in _items]
                        action["reason"] = (
                            f"permanently deletes {len(_rows)} past-due "
                            "item(s) — always confirmed")
                        action["preview"] = (
                            "\n".join(_rows[:60])
                            + (f"\n…and {len(_rows) - 60} more"
                               if len(_rows) > 60 else ""))
                    elif name == "clear_reminders":
                        from service.tools.assistant_tools import reminders_matching
                        _scope = str(args.get("scope", "all"))
                        _items = reminders_matching(_scope, args.get("query", ""))
                        _rows = [
                            f"{datetime.fromtimestamp(c['when_ts']):%a %b %-d, %Y}"
                            f"  {c['title']}" for c in _items]
                        action["reason"] = (
                            f"permanently deletes {len(_rows)} reminder(s) in "
                            f"scope '{_scope}' — always confirmed")
                        action["preview"] = (
                            "\n".join(_rows[:60])
                            + (f"\n…and {len(_rows) - 60} more"
                               if len(_rows) > 60 else ""))
                    # Outbound messages: the reason line is a fixed generic
                    # string ("sends something on your behalf — always
                    # confirmed") with no recipient or content in it — the
                    # user has never actually seen what they're approving.
                    # Reuses the same `preview` field/UI block as create_tool
                    # above, just populated from the message content instead
                    # of a code draft. See action_tools.confirm_preview.
                    else:
                        from service.tools.action_tools import confirm_preview
                        if (preview := confirm_preview(name, args)):
                            action["preview"] = preview
                        elif name == "organize_files":
                            from service.tools.files_tools import move_preview_text
                            action["preview"] = move_preview_text(str(args.get("preview_token", "")))
                    # Already answered by the batched-calendar pre-pass above
                    # — don't prompt again for the same change.
                    if cid in batch_verdict:
                        approved = batch_verdict[cid]
                    else:
                        approved = await approver.confirm(action)
                    if approved:
                        result = await run_tool(tool, args)
                        audit("confirm_allow", tool=name, args=args)
                    else:
                        result = "The user denied this action."
                        audit("confirm_deny", tool=name, args=args)
                        # A denial is not an answered tool — the model has to
                        # work out what to do instead, which is thinking. Policy
                        # outcome, so it never clears (see hard_failed).
                        hard_failed.add(name)
                else:  # ALLOW
                    result = await run_tool(tool, args)
                    audit("allow", tool=name, args=args)

            # DISPLAY ONLY — the model always gets the full result on the next
            # line. The 2000-char cap here was actively misleading when
            # debugging: a 89,000-char show_profile result showed up in the
            # exported debug log as 2,000 chars, which reads as "the tool
            # truncated the profile" when in fact the opposite happened (the
            # whole thing went into the context and swamped it). Raised so the
            # export reflects what the model actually saw.
            await emit({"type": "tool_result", "id": cid, "result": result[:20000],
                        **({"debug": dbg} if dbg else {})})
            outcome = _record_outcome(
                name, result, args=args,
                denied=(dec.tier is Tier.DENY or
                        (dec.tier is Tier.CONFIRM and result == "The user denied this action.")))
            msgs.append({"role": "tool", "tool_call_id": cid,
                         "content": _fit_tool_result(result, name)})
            if result.strip():
                last_tool_result = result
                last_tier = dec.tier
                if dec.tier is not Tier.DENY and not is_tool_error(result):
                    clean_results.append((name, result))
            # Feed the narration gate (see `narrating` above). A tool counts as
            # ANSWERED only when it actually ran and returned something real.
            # A DENY is a policy outcome and never clears; an ERROR is a
            # recoverable failure that clears if the same tool later succeeds
            # (see failed_tools/hard_failed).
            if outcome.status == "denied":
                hard_failed.add(name)
                if outcome.effect != "read":
                    if name in _TERMINAL_OUTBOUND_DENIALS:
                        return await _finish_denied_outbound(emit)
                    response = "The requested action was not completed because approval was denied."
                    await emit({"type": "text", "text": response})
                    return response
            elif outcome.status in {"failed", "needs_input"}:
                failed_tools.add(name)
                if outcome.effect != "read":
                    # Retrying a write after an uncertain response can create
                    # duplicates; let the user correct the arguments first.
                    response = "The action did not return a verified success. I stopped without retrying.\n" + result
                    await emit({"type": "text", "text": response})
                    return response
            elif outcome.status == "succeeded":
                tools_answered.add(name)
            if outcome.status == "succeeded" and name == "draft_message":
                # A message draft is structured UI state, not assistant prose.
                # End the turn after emitting the exact editable recipient/body
                # so the model cannot replace the card with a Markdown draft or
                # ask another "should I send it?" question.
                await emit({"type": "message_draft",
                            "to": str(args.get("to") or ""),
                            "text": str(args.get("text") or "")})
                return ""
            if outcome.status == "denied" and name in _TERMINAL_OUTBOUND_DENIALS:
                return await _finish_denied_outbound(emit)
            if name == "create_tool" and get_tool(str(args.get("name", "")).strip().lower()):
                # A tool created THIS turn is usable for the rest of it. The
                # schema list is otherwise built once before the loop, so a
                # brand-new tool stayed invisible until the next message —
                # which made "build me something that does X" always take two
                # requests, with the user repeating themselves verbatim.
                # Rebuilt from the registry (which reload_skills just
                # refreshed) rather than patched, so it picks up the real
                # generated signature.
                new_name = str(args.get("name", "")).strip().lower()
                if tools is not None and new_name not in tools:
                    tools = [*tools, new_name]
                schemas = tool_schemas(tools)

        # See short_circuit_tools' docstring above. Only for a SINGLE
        # successful (ALLOW-tier) call to one of these tools — a compound
        # step with multiple tool calls still needs the model to merge them.
        # Also excludes an is_tool_error result: ALLOW tier only says the
        # policy engine allowed the call, not that it actually succeeded — a
        # bad-args rejection from a short_circuit tool must still go back to
        # the model for a real retry, not get short-circuited out as if the
        # error string were the finished summary.
        # NOT when this turn has gathered from anywhere ELSE. The short-circuit's
        # premise is "this tool's result IS the complete answer", which stops
        # being true the moment a second source has also answered — returning
        # here would silently discard it. Two ways that happens, both real:
        #
        #   * a `multi_round` route (the aggregate to-do list) where the model
        #     emits summarize_emails ALONE in a step. The turn would end with
        #     the mail digest as the entire answer to "what do I need to do",
        #     and get_upcoming/search_notes/summarize_messages would never run —
        #     precisely what SYSTEM calls "not a partial answer, it is a WRONG
        #     one". SYSTEM itself warns the model against calling these "one at
        #     a time over several turns", i.e. this is known model behaviour.
        #     Worse, email_tools._no_inbox_message() is not an is_tool_error, so
        #     a still-syncing mailbox could make "(No inbox data yet…)" the
        #     whole answer.
        #   * ANY route where a previous step already ran a different tool —
        #     e.g. get_upcoming in step 0, summarize_messages in step 1. That
        #     one is not multi_round, so `tools_answered <= {name}` is what
        #     catches it; the multi_round clause is kept as well because it
        #     states the route-level intent rather than inferring it.
        _sc_name = _clean_tool_name(tool_calls[0]["function"]["name"]) if tool_calls else ""
        if (short_circuit_tools and len(tool_calls) == 1
                and _sc_name in short_circuit_tools
                and not multi_round
                and _unmet_group() is None
                and tools_answered <= {_sc_name}
                and last_tier is Tier.ALLOW and last_tool_result.strip()
                and not is_tool_error(last_tool_result)):
            if reasoning:
                await emit({"type": "reasoning", "text": reasoning})
            await emit({"type": "text", "text": last_tool_result})
            await emit({"type": "done"})
            return last_tool_result

    # Step limit reached without the model ever producing a final answer.
    # Surface whatever the last tool actually returned instead of a bare,
    # unhelpful "stopped" message — verified live: a multi-hop lookup (wrong
    # ticker -> resolve the right one -> fetch its price) burned all 8 steps
    # mostly on redundant/wasted intermediate calls, and the LAST tool call
    # had already fetched the exact answer (a stock price) that then got
    # silently discarded here in favor of this message. The raw tool result
    # isn't as polished as a narrated answer, but it's real data instead of
    # nothing.
    # EXCEPT when that last result is itself an error (is_tool_error) — verified
    # live on a small model (LFM2.5-2.6B) asked a vague profile question: every
    # step called some tool with hallucinated/misnamed arguments, run_tool's
    # "(error calling ...)" message went back to the model to retry each time,
    # and it never did before the step budget ran out — so this fallback used
    # to hand the user that last formatted-for-the-model TypeError string
    # verbatim as if it were Wisp's real answer. An error is not "real data
    # instead of nothing"; it's a failed turn, and should read as one.
    # …and every source that answered, not just the last (see clean_results):
    # this exit is reached on exactly the multi-hop turns where several already
    # have, so "the last one" is a lottery over which tool happened to run last.
    if (unmet := _unmet_group()) is not None:
        fallback = ("I couldn't complete every requested step. Still missing one of: "
                    + ", ".join(sorted(unmet)) + ".")
    elif clean_results:
        fallback = _merge_results(clean_results)
    elif is_tool_error(last_tool_result):
        fallback = _STUCK_MESSAGE
    else:
        fallback = "(stopped after reaching the step limit)"
    fallback = _verified_final(fallback)
    await emit({"type": "text", "text": fallback})
    await emit({"type": "done"})
    return fallback
