"""The ReAct agent loop.

Drives a tool-capable model (gpt-oss-20b / gemma-4-e4b) through:
  model -> tool_calls -> safety check -> (confirm) -> execute -> feed back -> repeat
until the model returns a final answer. Every action passes through the policy
engine and the audit log. Emits structured events for the frontend.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime

from service.config import is_super_model_active, resolve_reasoning_effort, role_to_model
from service.inference.omlx_client import OMLXClient
from service.safety import Tier, audit, decide
from service.tools import get_tool, tool_schemas
from service.tools.registry import run_tool

Emit = Callable[[dict], Awaitable[None]]

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
    "- For live external facts — weather, stock/crypto prices, sports scores, "
    "breaking news, or any public webpage/API the user asks about — call "
    "`web_fetch`, NOT run_shell/curl. It's the reliable path (its description "
    "lists known no-key endpoints for weather/crypto/news). If the fetch fails, "
    "is denied, or returns an error/no data, SAY you couldn't retrieve it — do "
    "NOT fall back to a number from memory. A confidently wrong live price/score "
    "is worse than 'I couldn't reach it right now.' Only state a live figure you "
    "actually got back from web_fetch this turn.\n"
    "- For any non-trivial code to write, fix, or refactor, call `write_code` to "
    "delegate it to the coding specialist rather than writing the code yourself; "
    "then use other tools (e.g. write_file) to apply its output.\n"
    "- For anything about the user's calendar, schedule, deadlines, assignments, "
    "or what's coming up, call `get_upcoming`. For anything in the PAST — "
    "'what did I have last week', 'when did I last meet with X' — call "
    "`get_past_events` instead (covers roughly the last year). NEVER script "
    "Calendar.app via run_shell/osascript — it is slow and often lacks "
    "permission; Wisp already syncs the calendar locally.\n"
    "- If more than one Mail or Calendar account is linked and the user asks "
    "about a SPECIFIC one ('my work email', 'my personal calendar'), pass "
    "`account` to the relevant tool (summarize_emails/view_emails/"
    "get_upcoming/get_past_events) — omit it otherwise, including whenever "
    "only one account is linked.\n"
    "- To add something with a time: use `add_calendar_event` when the user wants "
    "a real CALENDAR EVENT or meeting (it writes to macOS Calendar and syncs to "
    "their devices); use `add_reminder` for a lightweight personal nudge that "
    "only Wisp tracks. When unsure which, prefer `add_calendar_event` for things "
    "with a specific time/place and `add_reminder` for 'remind me to …'.\n"
    "- To cancel/delete/remove something from the schedule, call `cancel_event` "
    "with whatever title the user named — do NOT ask for the date/time first; the "
    "tool matches by title and will tell you if it's ambiguous.\n"
    "- For an OVERVIEW of the user's email or inbox, call `summarize_emails`. "
    "For a SPECIFIC detail FROM an email — an order number, a confirmation "
    "code, an address, a pickup/delivery time, party details, exact wording "
    "to quote back — call `view_emails` instead, which returns the raw "
    "verbatim body/sender/recipient/subject, not a summary. There is no other "
    "way to read email; you CAN read it through these tools — never claim you "
    "cannot, and never fall back to run_shell/open_app to get at Mail.app. If "
    "the user names a specific day ('today', 'yesterday', a date), pass it as "
    "`day`.\n"
    "- Same split for messages: `summarize_messages` for an overview, "
    "`view_messages` for a specific detail (an address, a time, a code, exact "
    "wording) — pass `query` to search by keyword. You CAN read and quote "
    "their messages through these tools — never claim you cannot and never "
    "give a generic tutorial on how to use the Messages app. If the user "
    "names a specific day, pass it as `day`.\n"
    "- For anything the user wrote down or saved in Notes — an idea, a list, a "
    "code, project notes — call `search_notes` with a keyword `query`. This is "
    "always RAW verbatim content (there is no separate summary tool for notes).\n"
    "- You CAN send things: `send_email` sends real mail from their account, "
    "`send_message` sends a real iMessage/SMS. Before calling either, WRITE THE "
    "FULL DRAFT OUT in your reply — recipient, subject, and the exact body — "
    "so they can read it before the confirmation card appears; the card shows "
    "only a one-line summary. Never guess a recipient address or phone number: "
    "look it up with `view_emails`/`view_messages` on the person's name, or ask. "
    "These always require the user's confirmation, every time, and that's "
    "expected — call the tool rather than asking for permission in text.\n"
    "- To remember something durable about the user across future "
    "conversations, call `remember`. Use it whenever they say 'remember…', "
    "'from now on…', or correct a standing assumption you were working from. "
    "`forget` removes one. Things you were already told are injected into your "
    "context automatically each turn — call `recall` only when you need "
    "something that isn't there.\n"
    "- To change something on a remote service — a webhook, an API the user "
    "gave you a URL for — call `http_request` (POST/PUT/PATCH/DELETE). Use "
    "`web_fetch` for plain reads; it's a GET and doesn't interrupt the user.\n"
    "- THINGS YOU CANNOT DO IN YOUR HEAD — you must RUN CODE for these, and "
    "writing the answer yourself is always wrong, no matter how easy it looks:\n"
    "    * anything random or security-sensitive: passwords, tokens, API keys, "
    "UUIDs, dice rolls, shuffles, random picks. You are a language model — "
    "text you produce is NOT random and NOT cryptographically secure, even "
    "when it looks scrambled.\n"
    "    * hashes, checksums, encoding/decoding (MD5, SHA, base64, hex, URL "
    "encoding, JWT).\n"
    "    * precise arithmetic on big or many numbers, statistics over a list, "
    "unit/currency conversion with a live rate.\n"
    "    * ANY calendar or date arithmetic — week numbers, day of the week, "
    "days/weeks between dates, leap years, 'what date is N days from X', "
    "timezone or DST conversion. You are reliably wrong at these and it "
    "always looks right. Verified: asked for the ISO week number of a date, "
    "answering from your head gave 46 when the answer was 47.\n"
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
    "- If create_tool reports that Wisp already has a tool for this (contacts, "
    "messages, mail, calendar, notes), do NOT argue or retry — call that tool "
    "instead. A generated script genuinely cannot reach the user's personal "
    "data, so building one would only invent it.\n"
    "- For volume, call `get_volume`/`set_volume` — do NOT use run_shell/"
    "osascript for this, and do not try to guess the volume from context, "
    "just read it. For clipboard contents, call `clipboard_read`/"
    "`clipboard_write` rather than shelling out to pbpaste/pbcopy. For "
    "Wi-Fi on/off, call `set_wifi`. To lock the screen, call `lock_screen`.\n"
    "- For battery charge, time remaining, cycle count, or health/degradation "
    "questions, call `get_battery_status` — do NOT use run_shell/pmset for "
    "this. `pmset -g log`'s BatteryHealth 'Warning level'/'cap:' lines are "
    "low-charge UI-warning events (cap is the charge % at that moment), NOT a "
    "wear/health metric — free-interpreting them produces confidently wrong "
    "degradation claims. `get_battery_status` reports the real design-capacity "
    "health percentage instead.\n"
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
    "- If the user asks you to learn about them, build/update a profile, or "
    "'get to know them' from their mail/messages/notes/calendar, call "
    "`build_profile`. If they ask what you know about them, or to see their "
    "profile, call `show_profile`."
)


def _parse_args(raw: str) -> dict:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def _clean_tool_name(name: str) -> str:
    """Strip harmony header tokens that oMLX's adapter sometimes leaves glued
    onto the parsed function name (e.g. 'send_message<|channel|>commentary',
    'web_fetch<|channel|>json').

    Same adapter quirk as the empty-response case handled below, caught one
    stage later: there the header failed to parse at all and the whole call was
    dropped; here it parses far enough to yield a call, but the name still
    carries the trailing `<|channel|>…` run. Whether it happens varies by quant
    — never seen across 1200+ audited calls on gpt-oss MXFP4-Q8, then four
    times in the first hour on the oQ4 build.

    Left uncleaned the damage is worse than a dropped call: the mangled name
    misses allowed_names, gets rejected as unoffered, and the rejection text
    tells the model to say the action isn't available — so a working
    send_message surfaced to the user as "I can't send the message right now",
    which reads as Wisp refusing rather than a parse bug. Also drops the
    `functions.` namespace prefix harmony uses, for the same reason.
    """
    name = (name or "").split("<|", 1)[0].strip()
    return name.rsplit(".", 1)[-1] if name.startswith("functions.") else name


_STUCK_MESSAGE = (
    "I don't have a reliable way to do that right now — there's no tool for "
    "it and I couldn't find a working command, rather than keep guessing and "
    "risk getting it wrong."
)


def _is_looping(reasoning_text: str, min_count: int = 4, min_len: int = 20) -> bool:
    """Detects a degenerate reasoning loop: observed live on "turn on my
    keyboard lighting" (no dedicated tool, no plain shell equivalent) — gpt-oss
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


async def _run_step(client, model, msgs, schemas, choice, max_tokens, emit,
                    *, stream_content: bool, temperature: float = 0.0) -> tuple[dict, str, bool]:
    """One model turn, streamed. Emits live answer tokens (as `delta` events) and
    throttled `heartbeat`s during silent reasoning so the agent path is never a
    no-feedback black hole. Returns (assembled_message, reasoning_text,
    content_was_streamed) — the caller uses content_was_streamed to detect and
    correct a specific failure mode (see run_agent's clear_answer emission)."""
    reasoning_parts: list[str] = []
    final: dict = {}
    content_streamed = False
    last_hb = time.monotonic()
    events = client.stream_events(
            model, msgs, tools=schemas, tool_choice=choice, max_tokens=max_tokens,
            temperature=temperature,
            chat_template_kwargs={"reasoning_effort": resolve_reasoning_effort("agent")})
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
        elif kind == "final":
            final = ev["message"]
    # Raw request/response for debug export (see OverlayModel.exportDebugLog
    # on the Swift side) — tracked unconditionally, same as the rest of the
    # per-turn debug metadata, not gated on Debug Mode being on.
    await emit({
        "type": "raw_model_io",
        "model": model,
        "request": {
            "messages": msgs, "tools": schemas, "tool_choice": choice,
            "temperature": temperature, "max_tokens": max_tokens,
        },
        "response": final,
    })
    return final, "".join(reasoning_parts).strip(), content_streamed


async def run_agent(
    client: OMLXClient,
    model: str,
    messages: list[dict],
    emit: Emit,
    approver,
    *,
    tools: list[str] | None = None,
    max_steps: int = 8,
    max_tokens: int = 8000,
    force_first_tool: str | None = None,
    expect_tool_first: bool = False,
    short_circuit_tools: set[str] | None = None,
    style_hint: str | None = None,
    system_suffix: str | None = None,
    temperature: float = 0.0,
) -> str:
    """Run the loop. Returns the final assistant text. Streams events via `emit`.

    `force_first_tool`: make this the ONLY tool available on the first step,
    instead of leaving delegation to the model's judgment. Used to guarantee
    the coding specialist (write_code) runs before write_file for agentic
    requests that are also creating a code artifact.

    `short_circuit_tools`: tool names that already return complete, user-ready
    text — ONLY summarize_emails/summarize_messages qualify (see
    email_tools.py's/imessage_tools.py's _summarize(), each of which makes its
    own gemma call to synthesize real prose from raw headers/lines), so the
    tool result already IS the answer. Without this, the calling model (gemma,
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
    forced tool_choice for gpt-oss's harmony-format tool calling (the model
    called write_file directly anyway) — restricting the tools list itself is
    the real guarantee, since a function absent from the schema can't be
    called. That's necessary but not sufficient: with a longer system prompt
    in play, the model can still talk itself into a plain-text reply even with
    tool_choice="required" and only one tool available — oMLX's "required"
    turned out to be a soft nudge, not a decode-time constraint, once tested
    against the real system prompt rather than a short probe. So the first
    step retries with an explicit correction if no tool call comes back,
    rather than trusting either oMLX flag to guarantee it alone.
    """
    # Give the model "now" so it can resolve relative dates ("tomorrow", "this
    # Friday", "in 2 hours") when scheduling — otherwise it guesses the date.
    now_line = ("\nThe current date and time is "
                + datetime.now().strftime("%A, %B %-d, %Y at %-I:%M %p")
                + ". Resolve relative dates/times against this.")
    # What Wisp already knows about the user (see service/memory/profile.py) —
    # injected on every turn so answers stay personal without the model having
    # to call show_profile first. Best-effort: a missing/corrupt profile file
    # must never break the turn, and there's simply nothing to add until
    # build_profile has run at least once.
    profile_hint = ""
    try:
        from service.memory.profile import profile_context_block
        profile_hint = profile_context_block()
    except Exception:  # noqa: BLE001
        profile_hint = ""

    # Who the user actually IS, and how to tell their messages from everyone
    # else's (service/memory/identity.py). Separate from the profile hint above
    # and not a substitute for it: the profile is model-derived and may be
    # stale or empty, while this is ground truth available from the first
    # launch. The agent reads the same multi-person message lines the
    # summarizers do — `view_messages` hands it "Group of 3 (Mom, dad, Trishe)
    # | Mom: @Trishe - Your post has 439 likes" — so it needs the same rules
    # for reading them.
    identity_hint = ""
    try:
        from service.memory.identity import identity_prompt_block
        identity_hint = identity_prompt_block(with_date=False)
    except Exception:  # noqa: BLE001
        identity_hint = ""

    # Facts the user explicitly asked Wisp to remember (service/memory/facts.py).
    # Deliberately injected AFTER the profile: the block's own wording tells the
    # model these win when they contradict the profile's inferences, and putting
    # it last also keeps that precedence obvious positionally.
    memory_hint = ""
    try:
        from service.memory.facts import memory_context_block
        memory_hint = memory_context_block()
    except Exception:  # noqa: BLE001
        memory_hint = ""

    # Instructions from installed skills whose triggers match this turn (see
    # service/skills). Empty until the user installs one.
    skills_hint = ""
    try:
        from service.skills import skills_context_block
        last_user = next((m["content"] for m in reversed(messages)
                          if m.get("role") == "user"), "")
        skills_hint = skills_context_block(str(last_user))
    except Exception:  # noqa: BLE001
        skills_hint = ""

    # style_hint (set for light-read narration) is appended LAST so it can
    # override the base prompt's "Keep answers concise" when the task is
    # narrating the user's own calendar/notes/verbatim data expressively.
    # system_suffix (Super Model's quality/self-testing directive) is appended
    # too — both are optional and never set at the same time in practice.
    sys_content = (SYSTEM + now_line + identity_hint + profile_hint + memory_hint + skills_hint
                   + (("\n" + system_suffix) if system_suffix else "")
                   + (("\n" + style_hint) if style_hint else ""))
    msgs: list[dict] = [{"role": "system", "content": sys_content}] + messages
    schemas = tool_schemas(tools)
    first_step_schemas = tool_schemas([force_first_tool]) if force_first_tool else schemas

    # gpt-oss turns run exclusive (no gemma co-residency attempt — verified it
    # doesn't hold once gpt-oss is actively generating anyway, see
    # OMLXClient.ensure_only). Must be re-applied on EVERY step here, not just
    # the caller's first ensure_only before run_agent started: this loop calls
    # ensure_only again after every tool call, and without exclusive here that
    # per-step call falls back to the default keep-warm behavior — silently
    # reloading gemma back in mid-turn, which is exactly the "gemma and gpt-oss
    # both loaded at once" bug this fixes. A light-read turn (model == gemma,
    # the small narrow-toolset route) doesn't need this — gemma IS the
    # keep-warm model there, so the default behavior already does the right
    # thing.
    # Super Model runs a model that ISN'T gpt-oss (the usual `agent` model), so
    # the plain `== agent` check would leave exclusive=False and let gemma stay
    # co-resident every step — the "gemma still loaded during Super Model" bug.
    # Super Model always wants the whole budget, so force exclusive there too.
    exclusive = model == role_to_model("agent") or is_super_model_active()

    # The most recent tool result this turn. Used as a fallback answer when the
    # model calls a tool that already returns user-ready text (a messages/
    # calendar/notes summary) but then produces an EMPTY final answer on the
    # next loop round — a verified small-model (gemma) failure mode: it calls
    # summarize_messages, gets a perfect summary back, then emits nothing, so
    # the user saw only the tool-activity line and no reply. Surfacing the tool
    # result beats returning empty. (gpt-oss doesn't hit this — it re-presents
    # the result itself — so this only ever kicks in when the model whiffed.)
    last_tool_result = ""
    last_tier: Tier | None = None
    # One-shot forcing flag: set after a step where write_code ran but
    # write_file didn't, consumed by the very next step. Guards against a
    # verified failure mode: the model (esp. under Super Model, on a
    # non-gpt-oss model) gets write_code's result, says in plain text "let me
    # write this to disk now", and then just... doesn't call write_file —
    # tool_choice="auto" lets it end the turn on that prose instead. Without
    # this, the file silently never gets written and the user has no way to
    # tell besides checking the filesystem themselves.
    force_save_next = False
    write_file_schemas = tool_schemas(["write_file"])

    for _step in range(max_steps):
        # Restore the agent model as the resident one: a tool (e.g. write_code)
        # may have swapped in a specialist that can't co-reside under the cap.
        # Cheap no-op when nothing was swapped.
        await client.ensure_only(model, exclusive=exclusive)
        forcing_first_step = bool(force_first_tool) and _step == 0
        # expecting_tool: the router said this request NEEDS a tool, but we don't
        # restrict WHICH one (unlike force_first_tool). gpt-oss sometimes answers
        # from memory / says "I can't" instead of calling the obvious tool — this
        # nudge-and-retry corrects that on the first step. This is the fix for
        # "doesn't use its tools when it should".
        expecting_tool = expect_tool_first and _step == 0 and not forcing_first_step
        forcing_save = (force_save_next and not forcing_first_step
                        and bool(write_file_schemas))
        force_save_next = False  # consumed — only ever forces the one step right after write_code
        if forcing_first_step:
            step_schemas = first_step_schemas
        elif forcing_save:
            step_schemas = write_file_schemas
        else:
            step_schemas = schemas
        choice = "required" if (forcing_first_step or expecting_tool or forcing_save) else "auto"
        # Tool-SELECTION attempts (choice="required") always use temperature
        # 0.0 regardless of what the caller passed for `temperature` — that
        # caller-provided warmth (e.g. 0.6 for expressive light-read
        # narration, see main.py's _LIGHT_READ_STYLE) is for prose generation
        # and actively hurts here. Higher sampling temperature increases
        # variance away from the single most-likely (correct, structured)
        # continuation — exactly the wrong property when the model MUST
        # reliably emit a tool call rather than wander into free-text
        # instead. This is suspected to be part of why gemma's tool-skipping
        # got MORE noticeable after narration was made warmer: a step tagged
        # "required" was still running at the narration temperature, right
        # when reliability mattered most. Steps that actually narrate
        # (choice="auto", after a tool already ran) keep the caller's
        # temperature — this only tightens the tool-selection step itself.
        step_temperature = 0.0 if choice == "required" else temperature
        # The names actually OFFERED to the model this step. Restricting the
        # schema alone ("a function absent from the schema can't be called")
        # turned out not to be a real guarantee — verified live: gemma, given
        # only get_upcoming, still emitted a tool_call for add_reminder (with
        # hallucinated argument names, since it was never given that
        # function's real signature either). get_tool() below does a GLOBAL
        # registry lookup with no awareness of what was offered this turn, so
        # that call would have gone on to execute — bypassing the entire point
        # of restricting gemma's light-read routes to a narrow, verified-safe
        # toolset. allowed_names is checked before execution below to actually
        # enforce the restriction the schema was supposed to guarantee.
        allowed_names = {s["function"]["name"] for s in step_schemas}

        msg: dict = {}
        reasoning = ""
        # Set when the previous attempt came back empty (an oMLX harmony parse
        # failure), so the next one re-samples at a higher temperature.
        retry_for_empty = False
        # Up to 2 retries (3 attempts) for both the forced-specific-tool case
        # and the softer "needs some tool" case. expecting_tool used to get
        # only 1 retry (2 attempts) — verified live that wasn't always enough:
        # on a light-read follow-up for a domain already answered earlier in
        # the conversation (e.g. "and my calendar" after "check my messages"),
        # gemma sometimes still hadn't called the tool by its LAST attempt,
        # producing confused self-referential text ("I apologize... it seems I
        # retrieved...") as the accepted final answer instead of ever running
        # the tool. The one extra attempt gives it another real chance.
        # Floor of 2 even on an ordinary step, purely so the empty-response
        # retry below has a budget to spend. It costs nothing when the model
        # answers normally: a non-empty response breaks out on the first pass.
        attempts = 3 if (forcing_first_step or expecting_tool or forcing_save) else 2
        for _attempt in range(attempts):
            # Only stream on the LAST attempt: earlier attempts might be a text
            # bypass we're about to reject and retry, and streaming that would
            # flash a wrong answer. If the final attempt is still text, it WAS
            # streamed, so the `not tool_calls` branch below won't re-emit it.
            last_attempt = _attempt == attempts - 1
            stream = last_attempt or not (forcing_first_step or expecting_tool or forcing_save)
            # Retrying an unparseable (empty) response at the SAME temperature
            # is pointless: step_temperature is 0.0 on agent turns, so decoding
            # is greedy and the retry reproduces the identical unparseable
            # token sequence — verified in oMLX's log, two byte-identical
            # failures a second apart. Nudging the temperature up re-samples,
            # which is what actually gets past the adapter's header quirk.
            attempt_temperature = step_temperature
            if retry_for_empty:
                attempt_temperature = max(step_temperature, 0.0) + 0.7
            msg, reasoning, content_streamed = await _run_step(
                client, model, msgs, step_schemas, choice, max_tokens, emit,
                stream_content=stream, temperature=attempt_temperature)

            # A completely empty response — no content, no reasoning, no tool
            # calls — is not the model deciding to say nothing. It means oMLX's
            # harmony adapter failed to PARSE what gpt-oss emitted and dropped
            # it. Verified in oMLX's own log: the model produced a perfectly
            # correct call, `commentary to=functions.create_tool<|constrain|>
            # json<|message|>{...}`, and the parser rejected it with
            # "unexpected tokens remaining in message header" — the working
            # calls have a space before <|constrain|> and this one didn't, a
            # tokenization quirk that varies per tool name and per sample.
            # Retrying re-samples and almost always parses, whereas accepting
            # the empty message silently ends the turn mid-flow — which looked
            # exactly like "the model ignored its tools".
            # NOTE: reasoning is deliberately NOT part of this test. It used to
            # require reasoning to be empty too, which missed the commonest
            # shape of this failure: the analysis block parses fine and only
            # the tool call is dropped. Verified in a user's debug export —
            # asked to "create a tool to do so", gpt-oss reasoned exactly
            # "Need create_tool." and returned no call and no content. Because
            # reasoning was non-empty the retry never fired, the step fell
            # through with nothing, and the next step produced a flat "I can't
            # do that" — the model had decided to build the tool and the user
            # was told the opposite. Reasoning is not user-visible output: a
            # step with no content AND no tool call has produced nothing,
            # whatever it thought on the way there.
            if (not msg.get("tool_calls") and not (msg.get("content") or "").strip()
                    and _attempt < attempts - 1):
                retry_for_empty = True
                continue
            retry_for_empty = False

            if msg.get("tool_calls") or not (forcing_first_step or expecting_tool or forcing_save):
                break
            # Model replied in text instead of calling a tool — nudge and retry.
            msgs.append({"role": "assistant", "content": msg.get("content") or ""})
            if forcing_first_step:
                nudge = (f"You must call the {force_first_tool} tool now — "
                         "do not reply without calling it.")
            elif forcing_save:
                nudge = ("You just generated code with write_code but have not "
                         "actually saved it anywhere yet. Call the write_file "
                         "tool now with the complete code as its content — do "
                         "not just say you will save it.")
            else:
                nudge = ("That request requires an action you can only do by "
                         "calling a tool (e.g. get_upcoming for the calendar or "
                         "schedule, summarize_emails for email, list_dir/read_file "
                         "for files, see_screen for the screen, run_shell to run "
                         "something). Call the appropriate tool now — do NOT answer "
                         "from memory and do NOT say you can't.")
            msgs.append({"role": "user", "content": nudge})
        tool_calls = msg.get("tool_calls")

        # Correct a specific failure mode: oMLX can return content AND
        # tool_calls in the SAME response (verified live — gemma/gpt-oss both
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
            # Empty final answer but a tool already produced user-ready text this
            # turn (see last_tool_result) — surface that instead of returning
            # nothing. It wasn't streamed (the model emitted no content), so emit
            # it as a `text` event now.
            if not text.strip() and last_tool_result:
                text = last_tool_result
                await emit({"type": "text", "text": text})
            # Otherwise the answer already streamed as `delta` events; surface
            # the thinking as a collapsible, then close. (No `text` event — that
            # would duplicate the streamed answer in the UI.)
            if reasoning:
                await emit({"type": "reasoning", "text": reasoning})
            await emit({"type": "done"})
            return text

        msgs.append({"role": "assistant", "content": msg.get("content") or "",
                     "tool_calls": tool_calls})

        # Set once more below if this step generates code without also saving
        # it — drives forcing_save on the NEXT step (see its comment above).
        ran_write_code = False
        ran_write_file = False

        for tc in tool_calls:
            cid = tc.get("id", "")
            name = _clean_tool_name(tc["function"]["name"])
            args = _parse_args(tc["function"].get("arguments", ""))

            # Enforce the restriction the schema was supposed to guarantee
            # (see allowed_names' comment above) BEFORE any registry lookup or
            # execution — a real, registered tool that just wasn't offered
            # this turn must be rejected exactly like an unknown one, not
            # executed. The message explicitly tells the model to say so
            # rather than claim success: the observed failure mode wasn't
            # just an unauthorized call, it was the model going on to report
            # "done!" in its final answer despite the tool never running.
            if name not in allowed_names:
                result = (f"({name} is not available for this request — do NOT say you "
                          "performed this action. Tell the user this specific action "
                          "isn't available right now.)")
                audit("reject_unoffered", tool=name, args=args)
                await emit({"type": "tool_result", "id": cid, "result": result})
                msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
                continue

            tool = get_tool(name)

            if tool is None:
                result = f"(unknown tool: {name})"
                await emit({"type": "tool_result", "id": cid, "result": result})
                msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
                continue

            # create_tool's whole safety story is "the user reviews the actual
            # code before it's installed", so the code must exist BEFORE the
            # confirmation card is raised — the card is shown and answered
            # before any tool runs. Generating here also means a tool that
            # couldn't be written never becomes a prompt to approve it.
            # (Previously prepare_draft was never called at all: draft_code
            # returned "" and the card asked the user to approve installing
            # code it showed them nothing of.)
            if name == "create_tool":
                from service.tools.tool_authoring import prepare_draft
                draft_err = await prepare_draft(args)
                if draft_err:
                    result = f"(couldn't create this tool: {draft_err})"
                    audit("draft_failed", tool=name, args=args, reason=draft_err)
                    await emit({"type": "tool_result", "id": cid, "result": result})
                    msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
                    continue

            dec = decide(tool.category, args, tool=name)
            await emit({"type": "tool_call", "id": cid, "name": name,
                        "args": args, "decision": dec.tier.value, "reason": dec.reason})

            if dec.tier is Tier.DENY:
                result = f"BLOCKED by safety policy: {dec.reason}"
                audit("deny", tool=name, args=args, reason=dec.reason)
            elif dec.tier is Tier.CONFIRM:
                action = {"id": cid, "tool": name, "args": args, "reason": dec.reason}
                # Installing a self-authored tool is the one confirm where the
                # args don't describe what's actually at stake — `{"name": "x"}`
                # says nothing about the code about to be written. Attach the
                # real code so the card can show it; pulled from the pending
                # draft rather than from args, so what's reviewed is what's
                # installed.
                if name == "create_tool":
                    from service.tools.tool_authoring import (
                        draft_code,
                        draft_scope_line,
                        draft_warning,
                    )
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
                approved = await approver.confirm(action)
                if approved:
                    result = await run_tool(tool, args)
                    audit("confirm_allow", tool=name, args=args)
                else:
                    result = "The user denied this action."
                    audit("confirm_deny", tool=name, args=args)
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
            await emit({"type": "tool_result", "id": cid, "result": result[:20000]})
            msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
            if result.strip():
                last_tool_result = result
                last_tier = dec.tier
            if name == "write_code" and dec.tier is not Tier.DENY:
                ran_write_code = True
            elif name == "write_file":
                ran_write_file = True
            elif name == "create_tool" and get_tool(str(args.get("name", "")).strip().lower()):
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

        force_save_next = ran_write_code and not ran_write_file

        # See short_circuit_tools' docstring above. Only for a SINGLE
        # successful (ALLOW-tier) call to one of these tools — a compound
        # step with multiple tool calls still needs the model to merge them.
        if (short_circuit_tools and len(tool_calls) == 1
                and _clean_tool_name(tool_calls[0]["function"]["name"]) in short_circuit_tools
                and last_tier is Tier.ALLOW and last_tool_result.strip()):
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
    fallback = last_tool_result if last_tool_result.strip() else "(stopped after reaching the step limit)"
    await emit({"type": "text", "text": fallback})
    await emit({"type": "done"})
    return fallback
