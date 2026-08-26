"""MOE agent service.

Endpoints:
  GET  /health           -> oMLX health
  GET  /models           -> installed models + role map
  POST /chat             -> simple completion (model or role), optional streaming
  POST /agent            -> route + run (agent loop or specialist completion); SSE events
  POST /agent/approve    -> resolve a confirm-tier action for a running session

The /agent stream emits JSON events: session, routed, status, heartbeat, delta,
text, tool_call, confirm, tool_result, done, error. `status` carries a
human-readable `text` describing in-progress work (e.g. a cold model
load/swap) that has no other visible signal — see OMLXClient.ensure_only.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse

from service import idle, idle_unloader
from service.config import (
    favorite_models,
    models_config,
    no_thinking_kwargs,
    role_to_model,
    save_installed_models,
    set_role,
)
from service.agent import InteractiveApprover, run_agent
from service.errors import translate as translate_error
from service.inference.omlx_client import OMLXClient
from service.memory import store, build_messages, maybe_summarize
from service.memory.prompt_blocks import memory_block, now_line
from service.memory.context import default_history_budget
from service.router import route
from service.assistant import assistant_store, scheduler as assistant_scheduler
from service.assistant.hub import hub as assistant_hub
from service import skills
from service.mcp import manager as mcp_manager
from service.search import engine as search_engine, embedder as search_embedder
from service.research import ResearchManager
from service.research import cache as research_cache

# Roles that are "worth keeping" — once a conversation reaches one of these it
# stays pinned there, so a follow-up like "make it faster" isn't downgraded to
# a small model. fast/general never override a pinned heavier expert.
# Roles a conversation "sticks" to so a follow-up isn't downgraded to the fast
# model mid-thread.
_STICKY_ROLES = {"coding", "reasoning", "agent"}

# Tools that already synthesize a COMPLETE final reply internally — see
# email_tools.py's/imessage_tools.py's _summarize(), which each make their own
# the summarizer call to turn raw headers/lines into real prose. Only THESE are passed
# to run_agent's short_circuit_tools: a second "model restates the tool
# result" pass over their output is pure redundant echo (the reported bug —
# duplicated text, and separately a confused meta-commentary reply when the
# same tool had already run earlier in the conversation).
#
# Deliberately does NOT include get_upcoming / search_notes / view_emails /
# view_messages — those return raw structured or verbatim data with NO
# internal synthesis (view_emails/view_messages are explicitly documented as
# "RAW, verbatim... not a summary"). For those, the model's ONE narration
# pass over the tool result is the ONLY synthesis step for that data, not a
# redundant second one — short-circuiting them too (an earlier, over-broad
# version of this set did) silenced that narration entirely, which is why
# the summarizer's calendar answers read as bare mechanical dumps of the raw template
# while the agent model (never short-circuited, since it doesn't use tool_subset)
# still narrated the same data in prose.
_PRESYNTHESIZED_TOOLS = {"summarize_emails", "summarize_messages", "search_coverage",
                         "wisp_capabilities"}

# Appended to the agent-loop system prompt ONLY for light-read routes (the summarizer
# reading the user's own calendar/notes/verbatim data). It deliberately
# overrides the base prompt's "Keep answers concise" for these read-outs: the
# base terse instruction is right for the agent model machine operations ("opened
# Safari.") but made the summarizer's narration of get_upcoming/view_emails/view_messages
# read as a bare mechanical restatement of the raw tool output. Warmth is
# carried entirely by this prompt — sampling stays whatever the model is
# configured with in oMLX.
_LIGHT_READ_STYLE = (
    "For THIS request you are giving the user a warm, caring, genuinely helpful "
    "read-out of their own calendar / notes / messages — so ignore the 'keep "
    "answers concise' instruction above.\n"
    "Write like a thoughtful friend, not a machine: open with a short warm line, "
    "and when there are MULTIPLE items (several events, notes, etc.) lay them out "
    "as a SHORT BULLET LIST ('- ') with the key thing **bolded** (time, name, "
    "place) rather than a dense paragraph — a single item can just be a friendly "
    "sentence or two. A few tasteful emojis are welcome where they add warmth "
    "(e.g. 📅 for schedule, ✅ when nothing's pending) — not on every line. Vary "
    "your sentence rhythm, and close with a light caring offer to help. Stay "
    "completely grounded in exactly what the tool returned — never invent events, "
    "people, times, or details that aren't there."
)

# Appended when RouteDecision.clarify_channel is set — a send/compose intent
# named a recipient ("tell mom about my schedule", "send this to mom tonight")
# but no app, so both view_messages/summarize_messages and view_emails/
# summarize_emails (plus send_message/send_email) were offered together with
# no way to tell which one the user meant. Left alone the model silently picks
# one, which is wrong exactly as often as it's right — asking costs one turn,
# a wrong-channel send costs the user having to notice and redo it.
_CLARIFY_CHANNEL_HINT = (
    "For THIS request: the user didn't say whether to reach this person by "
    "TEXT (Messages) or EMAIL, and you have tools for both. ASK which one "
    "before sending, drafting, or scheduling anything — do not guess or "
    "default to one. Keep the question to one short line, not a preamble."
)

# Appended when RouteDecision.clarify_target is set — a reorganize that names
# no folder, no path and no class of file. Measured 2026-08-18: on "reorganize
# my files" the agent model listed the entire home directory, then `/Users/`,
# and returned a raw directory dump 10 times out of 10 without ever writing a
# sentence. The scope such a request implies is everything the user owns, and
# the job is MOVING files, so guessing is the expensive option and one short
# question is the cheap one.
_CLARIFY_TARGET_HINT = (
    "For THIS request: the user asked you to reorganize/tidy something but did "
    "NOT say WHICH folder, path, or kind of file they mean. Do NOT start "
    "listing directories and do NOT guess — the implied scope is their whole "
    "home folder. ASK which folder they want organised, and how they want it "
    "grouped, in one or two short lines. Do not call any tool on this turn."
)

client: OMLXClient
SESSIONS: dict[str, dict] = {}
research_manager = ResearchManager()

ROLE_SYSTEM = {
    "coding": "You are an expert software engineer. Write correct, idiomatic, "
              "production-quality code. Be concise; explain only what matters. "
              "Always format your answer in markdown and put code in fenced code "
              "blocks with a language tag (e.g. ```python).",
    "reasoning": "You are a careful reasoner. Think step by step and give a clear, "
                 "well-justified answer.",
    "fast": "You are a fast, friendly assistant. Answer briefly.",
    "general": "You are Wisp, a helpful local assistant on the user's Mac.",
}


def _sync_keep_warm() -> None:
    """(Re)point the client's keep-warm set at whatever `fast` and `agent`
    currently resolve to.

    Called once at startup and again on every `/config` role change that
    could touch either — a role reassignment must re-pin the NEW model and
    release the old one, or the retired model sits resident forever (memory
    wasted, never idle-unloaded) while the model actually in use pays a cold
    reload every time it's gone idle for 5+ minutes.
    """
    client.set_keep_warm({role_to_model("fast"), role_to_model("agent")})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = OMLXClient()
    # Keep the resident chat model warm. Every text role resolves to it now
    # (see config/models.yaml), so this is the model essentially every request
    # needs and there is no second chat model for it to contend with. Warm it
    # in the background so startup isn't blocked on the load; ensure_only
    # reloads it anyway if this races the first request.
    #
    # The Smart Search embedder is deliberately NOT keep-warm. It used to be,
    # on the reasoning that 320MB is negligible — but "negligible" is the wrong
    # frame when the budget is already exhausted. That was measured back when
    # two large chat models had to co-fit: 11.25GB + 6.33GB + 0.32GB = 17.9GB
    # against a 19GB hard watermark, leaving nothing for KV cache, and a large
    # prefill then failed a model load with a Metal command-buffer error. The
    # headroom is far more comfortable with a single ~3GB resident model, but
    # the reasoning still holds. The embedder now stays unloaded at Wisp
    # startup. Opening Smart Search starts `/search/prewarm` for the captured
    # document, and an ambiguous tool route loads it only when semantic
    # retrieval is genuinely needed.
    #
    # Dropping it from keep-warm also means a non-exclusive ensure_only()
    # evicts it, so it can no longer creep back in mid-turn.
    #
    # This tracks role_to_model(...) rather than naming a model — see
    # _sync_keep_warm below — so a role reassignment in Settings re-pins
    # whichever model is current instead of leaving a retired one wrongly
    # exempt from idle-unload forever.
    summarizer = role_to_model("fast")
    _sync_keep_warm()

    async def _warm_summarizer():
        try:
            await ensure_omlx()
            # Loads `summarizer` as the target AND, per ensure_only's step 3,
            # every other keep-warm model (currently the agent model too) —
            # one call warms both from a cold backend start.
            await client.ensure_only(summarizer)
        except Exception:  # noqa: BLE001 — warmup is best-effort, never fatal
            pass

    # Installed skills (~/.moe/skills) and MCP servers (~/.moe/mcp.json) both
    # contribute tools to the registry, so they load before the first request
    # can arrive. Neither is required — a fresh install has zero of each — and
    # a failure in either must not stop the backend from serving.
    try:
        skills.load()
    except Exception:  # noqa: BLE001
        pass
    mcp_task = asyncio.create_task(mcp_manager.start())

    # Retention: unpinned research jobs keep their evidence/quotes/report
    # forever, but the full fetched page bodies (only needed for re-extraction,
    # not for what's already cited) are reclaimed after 30 days. Best-effort —
    # a fresh install has nothing to purge, and a failure here must not block
    # startup.
    try:
        research_manager.store.purge_stale_bodies()
        research_cache.purge_older_than()
    except Exception:  # noqa: BLE001
        pass

    warm_task = asyncio.create_task(_warm_summarizer())
    unloader_task = asyncio.create_task(idle_unloader.run(client))
    assistant_task = asyncio.create_task(assistant_scheduler.run())
    yield
    warm_task.cancel()
    unloader_task.cancel()
    assistant_task.cancel()
    mcp_task.cancel()
    await mcp_manager.stop()
    await client.aclose()


app = FastAPI(title="Wisp", lifespan=lifespan)

OMLX_CLI = "/Applications/oMLX.app/Contents/MacOS/omlx-cli"


async def _omlx_cli(*args: str) -> bool:
    """Run an omlx-cli subcommand. False if the CLI isn't installed."""
    try:
        subprocess.Popen([OMLX_CLI, *args],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except FileNotFoundError:
        return False


async def _await_omlx(seconds: float) -> bool:
    for _ in range(int(seconds * 2)):
        await asyncio.sleep(0.5)
        try:
            await client.health()
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def ensure_omlx() -> None:
    """Make sure the oMLX engine is running; start it on demand if it was stopped.

    Falls back to `restart` when `start` fails to bring the server up. This is
    not belt-and-braces: quitting Wisp calls /shutdown_omlx (see the quit
    handler), and oMLX can be left in a state where its menu-bar app is alive
    but the server isn't listening — in which case `omlx-cli start` reports
    "oMLX server unresponsive on port 8000" and gives up, while `restart`
    recovers cleanly. Without this, every AI request after a Wisp
    quit/relaunch fails and the app looks completely broken until oMLX is
    restarted by hand.
    """
    try:
        await client.health()
        return
    except Exception:  # noqa: BLE001
        pass
    if not await _omlx_cli("start", "--no-wait"):
        return
    if await _await_omlx(30):
        return
    # `start` didn't take — the stale-server case above.
    if not await _omlx_cli("restart"):
        return
    await _await_omlx(60)


@app.get("/health")
async def health() -> dict[str, Any]:
    return await client.health()


@app.post("/shutdown_omlx")
async def shutdown_omlx() -> dict[str, Any]:
    """Stop the oMLX engine entirely (frees its ~2GB baseline). Called on app quit."""
    try:
        subprocess.Popen([OMLX_CLI, "stop"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"stopped": True}
    except FileNotFoundError:
        return {"stopped": False}


@app.get("/models")
async def models() -> dict[str, Any]:
    installed = await client.models()
    if installed:  # don't clobber the saved roster with a transient empty read
        save_installed_models(installed)
    return {"installed": installed, "roles": models_config()["roles"]}


@app.post("/config")
async def config(body: dict[str, Any]) -> dict[str, Any]:
    role, model = body.get("role"), body.get("model")
    if role and model:
        set_role(role, model)
        # `general` also repoints `agent` (see set_role) and either one can
        # change what should be keep-warm — re-pin live rather than waiting
        # for a restart, so a role swap doesn't leave the OLD model wrongly
        # exempt from idle-unload while the new one gets no pin at all.
        if role in ("fast", "general", "agent"):
            _sync_keep_warm()
    return {"ok": True, "roles": models_config()["roles"]}


@app.get("/mode")
async def get_mode() -> dict[str, Any]:
    from service.safety import read_only, full_access
    return {"read_only": read_only(), "full_access": full_access()}


@app.post("/mode")
async def set_mode(body: dict[str, Any]) -> dict[str, Any]:
    from service.safety import read_only, set_read_only, full_access, set_full_access
    if "read_only" in body:
        set_read_only(bool(body["read_only"]))
    if "full_access" in body:
        set_full_access(bool(body["full_access"]))
    return {"read_only": read_only(), "full_access": full_access()}


@app.post("/unload_all")
async def unload_all() -> dict[str, Any]:
    """Free memory/power: unload every resident model from oMLX."""
    loaded = await client.loaded_models()
    for m in loaded:
        await client.unload(m)
    return {"unloaded": loaded}


@app.post("/unload_agent")
async def unload_agent() -> dict[str, Any]:
    """Free just the heavy agent model (the agent model, ~12.7GB) — used when the notch
    is dismissed to a quiet menu-bar-only state, so the always-on router
    (small, cheap to keep resident) stays warm for a fast reopen while the
    big model's memory is given back."""
    model = role_to_model("agent")
    loaded = await client.loaded_models()
    if model in loaded:
        await client.unload(model)
        return {"unloaded": [model]}
    return {"unloaded": []}


@app.get("/idle_timeout")
async def get_idle_timeout() -> dict[str, Any]:
    return {"idle_minutes": idle.get_idle_minutes()}


@app.post("/idle_timeout")
async def set_idle_timeout(body: dict[str, Any]) -> dict[str, Any]:
    """Set the auto-unload idle timeout in minutes (0 disables it)."""
    if "idle_minutes" in body:
        idle.set_idle_minutes(float(body["idle_minutes"]))
    return {"idle_minutes": idle.get_idle_minutes()}


@app.post("/chat")
async def chat(body: dict[str, Any]):
    await ensure_omlx()
    messages = body.get("messages") or [{"role": "user", "content": body["prompt"]}]
    model = body.get("model") or role_to_model(body.get("role", "default"))
    await client.ensure_only(model)
    if body.get("stream"):
        async def gen():
            async for chunk in client.stream(model, messages,
                                             max_tokens=body.get("max_tokens", 8000)):
                yield chunk
        return StreamingResponse(gen(), media_type="text/plain")
    resp = await client.chat(model, messages, max_tokens=body.get("max_tokens", 8000))
    msg = resp["choices"][0]["message"]
    return {"model": model, "content": msg.get("content"), "usage": resp.get("usage")}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _strip_think(text: str) -> str:
    """Hide reasoning models' chain-of-thought, showing only the final answer."""
    text = re.sub(r"(?is)<think>.*?</think>", "", text)
    text = re.sub(r"(?is)<think>.*$", "", text)      # unclosed (truncated) think
    return text.strip()


@app.post("/agent")
async def agent(body: dict[str, Any]):
    """Route the request and run it, streaming events over SSE.

    Conversation continuity: pass `session_id` to continue a chat (history is
    loaded from the store, budgeted, and prepended). Omit it to start a new one;
    the id comes back in the opening `session` event for the client to reuse.

    `test_mode: true` — TOOL TEST MODE. Runs the exact same router decision a
    real turn would get, then either reports "no tools needed" or hands off to
    run_agent(test_mode=True) (see its docstring), which lets the model choose
    tools normally but intercepts every call before it does anything — no
    tool actually runs, nothing is read/written/sent. The response is the
    tool-call plan (as `tool_call` events, same shape as a real turn) plus the
    model's own prose description of that plan. Deliberately stateless: no
    session is read or created, and nothing is persisted — a test run must
    leave no trace and must not see conversation history it wasn't given,
    since "what would Wisp do with THIS prompt" is the whole point.

    `debug: bool` — whether to emit `raw_model_io` events (the full request,
    including every tool schema offered, and response for each model call this
    turn). Defaults to true for backward compatibility with a client that
    doesn't send it; the Swift client sends its own Debug Mode toggle here so
    the cost (measured up to ~1.6MB of events for one real session) is only
    paid when the user actually wants an export.
    """
    prompt: str = body["prompt"]
    # An `image` in the body is ACCEPTED AND IGNORED. Wisp has no vision model
    # and no vision route any more, so there is nothing that could look at it —
    # answering the text half of the request is strictly better than 500ing on a
    # field an older client might still send.
    test_mode = bool(body.get("test_mode"))
    # Whether the client's Debug Mode is on — gates raw_model_io events (the
    # full request + response for every model call this turn). Defaults True
    # so an older client that never sends this field keeps the old
    # unconditional-capture behavior rather than silently losing it.
    debug = bool(body.get("debug", True))

    # Resolve / create the persistent session. Skipped entirely in test mode
    # (see docstring) — sid is just a label for the `session` SSE event.
    if test_mode:
        sid = uuid.uuid4().hex
        sess = None
    else:
        sid = body.get("session_id")
        if not sid or store.get_session(sid) is None:
            sid = store.create_session()
        sess = store.get_session(sid)

    queue: asyncio.Queue = asyncio.Queue()
    approver = InteractiveApprover(lambda ev: queue.put(ev))
    # Key the in-flight registry by a unique REQUEST id, not the session id.
    # Two overlapping requests on the same session used to clobber each other:
    # the second overwrote SESSIONS[sid], then the first's `finally` popped it,
    # breaking the survivor's /agent/approve routing. Approvals now match on the
    # globally-unique action_id (see the approve endpoint), so the request key
    # only needs to be unique.
    req_id = uuid.uuid4().hex
    SESSIONS[req_id] = {"sid": sid, "queue": queue, "approver": approver}

    # Collected for persistence after the turn finishes.
    captured: dict[str, Any] = {"text": "", "deltas": [], "tools": []}

    async def emit(ev: dict):
        t = ev.get("type")
        if t == "delta":
            captured["deltas"].append(ev.get("text", ""))
        elif t == "text":
            captured["text"] = ev.get("text", "")
        elif t == "tool_call":
            captured["tools"].append(ev.get("name", "?"))
        elif t == "clear_answer":
            # The agent loop discarded whatever it streamed for that step (a
            # tool-call preamble, or — the case that made this urgent — an
            # unclosed <think> monologue that oMLX handed back as content).
            # The CLIENT is told to drop it; this buffer has to drop it too,
            # or the discarded text is still what gets PERSISTED as the turn.
            #
            # Verified 2026-08-09: without this, a turn was stored as 12,704
            # characters — 11,859 of raw chain-of-thought followed by the 844
            # of real answer — because `reply` falls back to "".join(deltas)
            # and nothing ever cleared them.
            captured["deltas"].clear()
        await queue.put(ev)

    async def runner():
        # Tell background model work (the daily brief) to stand down while
        # the user is waiting — there is one resident model, so
        # anything else generating doesn't interleave with this turn, it
        # contends with it. See service/idle.foreground_busy for the
        # measurement (12.8s vs 319.7s for the same call).
        idle.begin_foreground()
        try:
            await ensure_omlx()
            last_assistant = store.last_assistant_turn(sid) if sess else None
            last_tools = store.last_assistant_tools(sid) if sess else None

            # No separate LLM-classify step anymore (see route()'s docstring —
            # it was silently mis-classifying ~15% of genuinely tool-needing
            # requests). rule_route handles the clear cases instantly; anything
            # ambiguous now goes straight to the agent model via the agent loop.
            # last_tools lets a bare scope fragment ("from yesterday") continue
            # the previous turn's light-read domain instead of defaulting to the agent model.
            decision = await route(prompt, last_assistant=last_assistant,
                                   last_tools=last_tools)

            # A "pin generation to whichever big model is already resident"
            # step used to sit here, to avoid paying a swap for a trivial
            # follow-up. It is gone because it can no longer do anything: every
            # text role resolves to the same resident model (see models.yaml),
            # so decision.model already IS that model on every non-vision route.
            # Keeping it would only have kept appending a "kept on resident
            # <model> (no swap)" note describing a swap that cannot happen.

            # Sticky routing: don't downgrade a conversation that already reached
            # a heavier expert. A pinned sticky role wins over fast/general.
            # EXCEPTION: a light-read decision (tool_subset set — messages/email/
            # calendar/notes/planning on the always-warm the summarizer) is a confident
            # RULE match carrying its own verified-safe toolset, not the kind of
            # ambiguous downgrade this guard exists to prevent. Without this
            # exemption, ANY earlier agent/coding/reasoning turn in a session
            # permanently drags every later "what's on my calendar" onto the agent model
            # for the rest of the conversation — a real reported bug (the agent model
            # answering a plain calendar-today read) that defeats the entire
            # point of keeping the agent model asleep for these.
            if (sess and sess["pinned_role"] in _STICKY_ROLES
                    and decision.role not in _STICKY_ROLES
                    and not decision.tool_subset):
                # Preserve whatever needs_tools THIS turn's own classification
                # already decided — sticky-pin exists to stop the MODEL from
                # downgrading mid-conversation, not to strip tool access a fresh
                # classification (or the confirms_offered_action check above)
                # correctly granted. Forcing it to `role == "agent"` here used to
                # silently take tools away from a follow-up like "go ahead" once
                # a session had pinned to coding/reasoning.
                needs_tools = decision.needs_tools
                decision.role = sess["pinned_role"]
                decision.model = sess["pinned_model"]
                decision.needs_tools = needs_tools or decision.role == "agent"
                decision.reason = f"pinned to {decision.role} for this conversation"

            await emit({"type": "routed", **decision.as_dict()})
            # Chat turns take the FULL memory budget (exclusive) rather than
            # trying to co-reside with a second model. This was measured back
            # when a large agent model and a small summarizer were both in
            # play: the small one got evicted anyway once the big one was
            # actually generating (its KV cache grows past the point where both
            # fit), so attempting co-residency just added overhead for a
            # guarantee that didn't hold. It still applies — the embedding
            # model (Smart Search) is a genuinely different model that would
            # otherwise linger.
            exclusive_turn = decision.model == role_to_model("agent")
            await client.ensure_only(decision.model, exclusive=exclusive_turn, emit=emit)
            if decision.role in _STICKY_ROLES and not test_mode:
                store.set_pinned(sid, decision.role, decision.model)

            user_msg: dict[str, Any] = {"role": "user", "content": prompt}
            # Test mode is stateless (see the endpoint docstring) — the prompt
            # stands alone, with no session history loaded or built on.
            if test_mode:
                messages = [user_msg]
            else:
                messages = build_messages(sid, max_tokens=default_history_budget()) + [user_msg]

            if test_mode and not decision.needs_tools:
                # No tool would be offered at all — reasoning/general/fast/
                # coding all generate a REAL answer, which test mode must
                # never do (see the endpoint docstring). Report the plan
                # without running the model a second time.
                await emit({"type": "text", "text": (
                    f"No tools needed — this would be answered directly "
                    f"({decision.reason}).")})
                await emit({"type": "done"})
            elif decision.needs_tools:
                # A route may name the one tool that must run first (memory
                # saves do — see _mk_light's `force`).
                force_tool = decision.force_first_tool
                # Light-read routes (tool_subset set) may include a
                # pre-synthesized tool (summarize_emails/summarize_messages —
                # see _PRESYNTHESIZED_TOOLS) whose result is already the final
                # answer; short-circuit past the redundant "model restates the
                # tool result" round trip ONLY for those. See run_agent's
                # short_circuit_tools docstring for the compound-read
                # exception (multiple tools in one step still get merged).
                # Light-read = the model narrating the user's OWN data
                # (calendar / notes / messages / mail). Give it the expressive
                # style hint; warmth is the prompt's job, not a temperature Wisp
                # substitutes for the one configured against this model in oMLX.
                #
                # Reads decision.light_read, NOT bool(decision.tool_subset).
                # Those were the same thing only while every scoped route was a
                # personal-data read; device-control, web and ambiguous routes
                # are scoped too now, and none of them should be answered in a
                # bulleted calendar-readout voice.
                is_light_read = decision.light_read
                # Composed, not either/or: a route can be both a warm read-out
                # AND channel-ambiguous in principle (though not on any route
                # today — light_read and clarify_channel haven't co-occurred in
                # practice, since compose routes aren't light reads). Composing
                # rather than picking one keeps that true by construction
                # instead of by coincidence.
                style_hint = ((_LIGHT_READ_STYLE if is_light_read else "")
                             + ("\n" + _CLARIFY_CHANNEL_HINT if decision.clarify_channel else "")
                             + ("\n" + _CLARIFY_TARGET_HINT if decision.clarify_target else ""))
                final = await run_agent(client, decision.model, messages, emit, approver,
                                        tools=decision.tool_subset,
                                        force_first_tool=force_tool,
                                        expect_tool_first=decision.expect_tool_first,
                                        short_circuit_tools=_PRESYNTHESIZED_TOOLS,
                                        style_hint=style_hint or None,
                                        multi_round=decision.multi_round,
                                        narration_after=decision.narration_after,
                                        direct_calls=decision.direct_calls,
                                        required_tool_groups=decision.required_tool_groups,
                                        forbidden_tools=decision.forbidden_tools,
                                        conditional_tools=decision.conditional_tools,
                                        test_mode=test_mode, debug=debug)
                captured["text"] = final or captured["text"]
            else:
                # This is the MOST-USED path (every general/fast/coding/
                # reasoning reply — a "reasoning" role runs here too, at the
                # same reasoning_effort=medium: empirically as rigorous as the
                # retired Phi-4 specialist, ~5-8x faster) and previously had
                # zero stall protection on the general path. If token
                # generation ever paused mid-stream (oMLX hiccup, memory
                # pressure), partial text just sat there with no signal of
                # whether it was still working or dead — the reported "stops
                # responding, just a void" symptom. Races each next-chunk
                # against a short timeout and emits a heartbeat on silence
                # instead of going quiet.
                #
                # the agent model (every text role resolves to it) splits output
                # into reasoning_content vs content — but this path used to use
                # client.stream(), which only ever forwards delta.content and
                # silently drops reasoning_content. On prompts whose
                # chain-of-thought runs long ("explain how X works" was the
                # reported trigger), reasoning can consume most or all of the
                # token budget before content ever starts, so content stays empty
                # for the whole request: no delta events, just heartbeats then
                # done. Uses stream_events() (reasoning + content + final) so we
                # can apply a "model kept everything in the wrong field"
                # fallback.
                # Plain (non-tool) chat gets the same remembered-facts context
                # the agent loop has had all along — otherwise a fact the user
                # asked Wisp to remember would answer correctly in a
                # tool-using turn but not in a conversational one, which reads
                # as Wisp randomly forgetting.
                #
                # always_skills_block() (e.g. the humanizer style skill) is
                # deliberately added ONLY here, never in run_agent's prompt —
                # the tool-calling agent loop is already fragile about
                # instruction competition (see run_agent's force_first_tool/
                # expect_tool_first machinery), and this path is guaranteed
                # tool-free. Excluded for "coding" too: several of the
                # style rules (no em/en dashes, no hyphenated compounds) are
                # prose-specific and could otherwise bleed into code syntax.
                from service.skills import always_skills_block
                sysp = (ROLE_SYSTEM.get(decision.role, ROLE_SYSTEM["general"])
                        + now_line() + memory_block()
                        + (always_skills_block() if decision.role != "coding" else ""))
                msgs = [{"role": "system", "content": sysp}] + messages
                # "fast" is TRIVIAL_RE's positive match only (greetings, thanks,
                # acks — see router.py) — the one role where the chain-of-thought
                # a thinking model opens with is pure latency on a ~20-token
                # answer, the same reasoning no_thinking_kwargs already applies
                # to the summary paths. NOT "general": a real question routed
                # there benefits from the model actually thinking, and NEVER the
                # agent loop, which measures 0/3 tool calls with thinking
                # suppressed on a selection step (see no_thinking_kwargs's
                # docstring) — this path never drives tools, so that risk
                # doesn't apply here.
                think_kwargs = (no_thinking_kwargs(decision.model)
                                if decision.role == "fast" else {})
                events = client.stream_events(
                    decision.model, msgs, max_tokens=8000, **think_kwargs).__aiter__()
                content_seen = False
                reasoning_parts: list[str] = []
                final_msg: dict = {}
                while True:
                    try:
                        ev = await asyncio.wait_for(events.__anext__(), timeout=8)
                    except asyncio.TimeoutError:
                        await emit({"type": "heartbeat"})
                        continue
                    except StopAsyncIteration:
                        break
                    if ev["kind"] == "reasoning":
                        reasoning_parts.append(ev["text"])
                    elif ev["kind"] == "content":
                        content_seen = True
                        await emit({"type": "delta", "text": ev["text"]})
                    elif ev["kind"] == "final":
                        final_msg = ev["message"]
                # Raw request + the reassembled response (streamed, so there's
                # no single wire response — this is the concatenated deltas
                # exactly as stream_events() assembled them) — for debug
                # export. Gated on the client's Debug Mode (see `debug` above);
                # off by default cost, on when the user actually wants an
                # export.
                if debug:
                    await emit({
                        "type": "raw_model_io",
                        "model": decision.model,
                        "request": {"messages": msgs, "max_tokens": 8000},
                        "response": final_msg,
                    })
                if not content_seen:
                    fallback = _strip_think("".join(reasoning_parts)
                                            or final_msg.get("content") or "")
                    if fallback:
                        await emit({"type": "delta", "text": fallback})
                elif reasoning_parts:
                    # Content streamed normally, so the reasoning is genuinely
                    # separate chain-of-thought (not reused as the fallback
                    # answer above) — surface it as a collapsible "reasoning"
                    # event like the dedicated reasoning branch does, so the
                    # UI's "Show reasoning" disclosure and debug log export
                    # aren't empty for the general/fast/coding path even
                    # though it's the one most turns take.
                    await emit({"type": "reasoning", "text": "".join(reasoning_parts).strip()})
                await emit({"type": "done"})

            # Persist the exchange, then fold any overflow into the summary.
            # Skipped in test mode — a dry run must leave no trace (see the
            # endpoint docstring): nothing was actually asked or answered.
            if not test_mode:
                reply = captured["text"] or "".join(captured["deltas"])
                digest = ", ".join(dict.fromkeys(captured["tools"])) or None
                store.add_turn(sid, "user", prompt)
                store.add_turn(sid, "assistant", reply.strip(), tool_digest=digest)
                await maybe_summarize(client, sid, decision.model)
        except Exception as e:  # noqa: BLE001
            message, detail = translate_error(e, retry_omlx=ensure_omlx)
            await emit({"type": "error", "message": message, "detail": detail})
            await emit({"type": "done"})
            # Persist the user's prompt even though the turn failed — it was
            # NEVER saved, because the only add_turn call sat past everything
            # that could raise (line ~973, reached only on full success).
            #
            # VERIFIED FAILURE 2026-08-19 (user's debug export): a compound
            # request ("send an email with the weather, my stock movements, and
            # my schedule") errored somewhere in the agent loop. The error
            # reached the client, but store.add_turn was never called, so the
            # session's history had a silent HOLE where that turn should be —
            # every later reference to it ("the first thing i asked", "the
            # first convo we had") got no match, and the model — correctly,
            # given what it could actually see — said it had no record of any
            # such request. The user re-asked three times across ten turns
            # before the conversation recovered. Not a model-quality problem:
            # Wisp's own memory of the request was gone before the model ever
            # got a second chance to help.
            #
            # A short honest assistant turn goes alongside it (not the real
            # answer, since there isn't one) so a later "did you send that"
            # gets "that attempt failed" rather than the model reasoning over a
            # dangling unanswered user message with no signal either way.
            if not test_mode:
                try:
                    store.add_turn(sid, "user", prompt)
                    store.add_turn(sid, "assistant",
                                   f"(This request could not be completed — {message} "
                                   f"Nothing was sent or changed. Ask again if you'd "
                                   f"like me to retry.)")
                except Exception:  # noqa: BLE001 — persistence must not mask the real error
                    pass
        finally:
            idle.end_foreground()
            await queue.put(None)  # sentinel

    asyncio.create_task(runner())

    async def stream():
        yield _sse({"type": "session", "id": sid})
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield _sse(ev)
        SESSIONS.pop(req_id, None)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/sessions")
async def list_sessions() -> dict[str, Any]:
    return {"sessions": store.list_sessions()}


@app.get("/sessions/{sid}")
async def get_session(sid: str) -> dict[str, Any]:
    sess = store.get_session(sid)
    if not sess:
        return {"ok": False, "error": "unknown session"}
    return {"ok": True, "session": sess, "turns": store.turns_from(sid, 0)}


@app.delete("/sessions/{sid}")
async def delete_session(sid: str) -> dict[str, Any]:
    store.delete_session(sid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Assistant layer — commitments (calendar/manual), reminders, the notch chip.
# ---------------------------------------------------------------------------
@app.get("/assistant/next")
async def assistant_next() -> dict[str, Any]:
    """The single soonest active commitment — data for the notch countdown chip."""
    return {"commitment": assistant_store.next_active()}


@app.get("/assistant/upcoming")
async def assistant_upcoming(days: int = 7) -> dict[str, Any]:
    return {"commitments": assistant_store.upcoming(days=max(1, min(days, 60)))}


@app.get("/assistant/status")
async def assistant_status() -> dict[str, Any]:
    return {"connectors": assistant_scheduler.connectors_status()}


@app.post("/assistant/sync/calendar")
async def assistant_sync_calendar(body: dict[str, Any]) -> dict[str, Any]:
    """Receive calendar events (or, with source='reminders', native
    Reminders.app items — see RemindersWriter.swift) from the Swift app
    (which holds the TCC grant) and sync into the commitments store. Each
    `source` is a separate replace-set (see AssistantStore.sync_source), so
    Calendar and Reminders syncing independently can't wipe each other out."""
    source = str(body.get("source") or "calendar")
    events = body.get("events") or []
    items = []
    for e in events:
        if e.get("when_ts") is None or not e.get("title"):
            continue
        items.append({
            "source_id": str(e.get("source_id") or f"{e['title']}-{e['when_ts']}"),
            "kind": str(e.get("kind") or "event"),
            "title": str(e["title"]),
            "context": e.get("context"),
            "organizer": e.get("organizer"),
            "account": e.get("account"),
            "when_ts": float(e["when_ts"]),
            "all_day": bool(e.get("all_day")),
            "location": e.get("location"),
            "confidence": 1.0,
        })
    n = assistant_store.sync_source(source, items)
    assistant_scheduler.record_sync(source, n, diagnostics=body.get("diagnostics") or {})
    await assistant_hub.publish({"type": "changed"})
    return {"ok": True, "synced": n}


@app.post("/assistant/commitments")
async def assistant_add(body: dict[str, Any]) -> dict[str, Any]:
    """Manually add a reminder/event. Accepts when_ts (epoch) or when_iso."""
    title = str(body.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "title required"}
    when_ts = body.get("when_ts")
    if when_ts is None and body.get("when_iso"):
        try:
            from datetime import datetime
            when_ts = datetime.fromisoformat(str(body["when_iso"])).timestamp()
        except ValueError:
            return {"ok": False, "error": "bad when_iso"}
    if when_ts is None:
        return {"ok": False, "error": "when_ts or when_iso required"}
    c = assistant_store.add_manual(title, float(when_ts),
                                   kind=str(body.get("kind") or "reminder"),
                                   context=body.get("context"))
    await assistant_hub.publish({"type": "changed"})
    return {"ok": True, "commitment": c}


@app.post("/assistant/commitments/{cid}")
async def assistant_update(cid: str, body: dict[str, Any]) -> dict[str, Any]:
    """Mark a commitment done/dismissed (status)."""
    status = str(body.get("status") or "").strip()
    if status not in {"active", "done", "dismissed"}:
        return {"ok": False, "error": "status must be active|done|dismissed"}
    ok = assistant_store.set_status(cid, status)
    if ok:
        await assistant_hub.publish({"type": "changed"})
    return {"ok": ok}


@app.post("/assistant/daily_summary")
async def assistant_daily_summary() -> dict[str, Any]:
    """On-demand combined brief (calendar + email + messages) for the Daily
    Summary button.

    Always 200 with a `text` the app can show. build_daily_brief already
    guarantees a non-empty brief on its own (see brief._sections); the try here
    covers the step before it — ensure_omlx, which raises when the engine is
    down. That case is worth its own sentence rather than a generic failure: a
    dead engine is something the user can act on, and it is also the one
    failure where the rendered fallback is still perfectly good, since nothing
    in it needs a model.
    """
    from service.assistant.brief import build_daily_brief
    from datetime import datetime as _dt
    part = "morning" if _dt.now().hour < 12 else "evening"
    try:
        await ensure_omlx()
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        from service.assistant.brief import brief_without_model
        return {"ok": False, "error": "engine unavailable",
                "text": brief_without_model(), "part_of_day": part}
    return {"ok": True, "text": await build_daily_brief(part),
            "part_of_day": part}


@app.get("/memory/facts")
async def list_facts(limit: int = 500) -> dict[str, Any]:
    """Everything the user has asked Wisp to remember, for a Settings view.
    Stated by the user and never expire on their own, so they need their own
    list where each one can be individually deleted."""
    from service.memory.facts import store as fact_store
    return {"facts": fact_store.all(limit=limit), "count": fact_store.count()}


@app.post("/memory/facts")
async def add_fact(body: dict[str, Any]) -> dict[str, Any]:
    from service.memory.facts import store as fact_store
    text = str(body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty fact"}
    return {"ok": True, **fact_store.add(
        text, category=str(body.get("category") or "fact"),
        pinned=bool(body.get("pinned")))}


@app.delete("/memory/facts/{fact_id}")
async def delete_fact(fact_id: int) -> dict[str, Any]:
    from service.memory.facts import store as fact_store
    return {"ok": fact_store.delete(fact_id)}


@app.post("/memory/facts/{fact_id}/pin")
async def pin_fact(fact_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """Pin a fact so it's always in the per-turn context block, ahead of
    whatever recency would otherwise select."""
    from service.memory.facts import store as fact_store
    return {"ok": fact_store.set_pinned(fact_id, bool(body.get("pinned", True)))}


@app.get("/assistant/summary_schedule")
async def get_summary_schedule() -> dict[str, Any]:
    from service.config import get_daily_summary_hour
    hour = get_daily_summary_hour()
    return {"hour": hour, "period": "AM" if hour < 12 else "PM"}


@app.post("/assistant/summary_schedule")
async def set_summary_schedule(body: dict[str, Any]) -> dict[str, Any]:
    """Set the scheduled daily-brief time. Accepts {"period":"AM"|"PM"} or
    {"hour": 8|20}."""
    from service.config import set_daily_summary_hour
    if "period" in body:
        hour = 8 if str(body["period"]).upper() == "AM" else 20
    else:
        hour = int(body.get("hour", 8))
    hour = set_daily_summary_hour(hour)
    return {"hour": hour, "period": "AM" if hour < 12 else "PM"}


@app.post("/assistant/sync/emails")
async def assistant_sync_emails(body: dict[str, Any]) -> dict[str, Any]:
    """Receive recent inbox headers and/or a raw-content batch from the Swift
    app (which holds Mail Automation access): headers feed summarize_emails
    (the summarizer digest, synced every 5min), raw feeds view_emails (verbatim lookup,
    synced once/day — see MailReader.swift). Each field is independent and
    only touched when present: MailReader posts them on separate cadences, so
    a headers-only sync must not wipe out the still-fresh raw cache (and
    vice versa)."""
    from service.tools.email_tools import (
        cache_emails, cache_history_headers, cache_raw_emails, set_email_availability)
    if "headers" in body:
        cache_emails(str(body.get("headers") or ""))
    if "raw" in body:
        cache_raw_emails(str(body.get("raw") or ""))
    # A separate, slower-cadence scan reaching back up to a year (headers only
    # — no body) — see MailReader.swift's historyScript. Independent field so
    # it can sync on its own timer without racing/clobbering "headers" (recent,
    # 5-min) or "raw" (recent, 15-min).
    if "history" in body:
        cache_history_headers(str(body.get("history") or ""))
    # The user's own account addresses — identity ground truth used by
    # identity.py (brief.py, the summarizer tools) to tell the user's own
    # outgoing mail apart from mail sent to them.
    if "identity_emails" in body:
        from service.tools.email_tools import cache_identity
        cache_identity(list(body.get("identity_emails") or []))
    # diagnostics: whether the AppleScript could read Mail at all (permission),
    # reported on EVERY sync — including empty/failed ones that carry no headers
    # — so the "no data" message can tell "still syncing / empty inbox" apart
    # from "grant permission".
    diag = body.get("diagnostics")
    if diag is not None:
        set_email_availability(bool(diag.get("available")), str(diag.get("reason") or ""))
    return {"ok": True}


@app.post("/assistant/sync/notes")
async def assistant_sync_notes(body: dict[str, Any]) -> dict[str, Any]:
    """Receive raw Notes.app content from the Swift app (which holds Notes
    Automation access) so search_notes can look through it verbatim."""
    from service.tools.notes_tools import cache_notes
    cache_notes(str(body.get("raw") or ""))
    return {"ok": True}


@app.post("/assistant/sync/browser_history")
async def assistant_sync_browser_history(body: dict[str, Any]) -> dict[str, Any]:
    """Receive recent Safari/Chrome history from the Swift app (which holds
    the Full Disk Access needed to read the history databases directly).
    Each browser posts independently — see BrowserHistoryReader.swift — so
    one browser being absent never clobbers the other's cache."""
    from service.tools.browser_history_tools import cache_browser_history
    browser = str(body.get("browser") or "")
    diag = body.get("diagnostics") or {}
    cache_browser_history(browser, str(body.get("lines") or ""),
                          available=bool(diag.get("available")),
                          reason=str(diag.get("reason") or ""))
    return {"ok": True}


@app.post("/assistant/sync/messages")
async def assistant_sync_messages(body: dict[str, Any]) -> dict[str, Any]:
    """Receive recent iMessage/SMS lines from the Swift app (which holds the
    Full Disk Access needed to read chat.db directly) so the messages tool
    can summarize them with the summarizer."""
    from service.tools.imessage_tools import cache_birthdays, cache_contacts, cache_messages
    diag = body.get("diagnostics") or {}
    # Contacts arrive on their own (much slower) schedule from ContactsReader,
    # so this endpoint accepts either payload independently — a contacts push
    # carries no "lines" and must not wipe the message cache.
    if "contacts" in body:
        cache_contacts(body.get("contacts") or {})
        cache_birthdays(body.get("birthdays") or {})
        return {"ok": True}
    cache_messages(str(body.get("lines") or ""),
                   available=bool(diag.get("available")),
                   reason=str(diag.get("reason") or ""))
    return {"ok": True}


@app.delete("/assistant/commitments/{cid}")
async def assistant_delete(cid: str) -> dict[str, Any]:
    """Cancel/delete a commitment. For a real calendar event, ask the Swift app
    to remove it from macOS Calendar (it holds the write grant); the re-sync then
    drops it from the store. Manual items are deleted outright."""
    c = assistant_store.get(cid)
    if not c:
        return {"ok": False, "error": "not found"}
    if c["source"] == "calendar" and c.get("source_id"):
        # when_ts disambiguates which OCCURRENCE to delete: EventKit gives every
        # instance of a recurring event the same identifier, so an identifier-only
        # lookup could hit the wrong occurrence.
        await assistant_hub.publish({"type": "delete_calendar_event",
                                     "source_id": c["source_id"],
                                     "when_ts": c.get("when_ts")})
        assistant_store.set_status(cid, "dismissed")  # leave the chip immediately
    else:
        assistant_store.delete(cid)
    await assistant_hub.publish({"type": "changed"})
    return {"ok": True}


@app.post("/assistant/action_result")
async def assistant_action_result(body: dict[str, Any]) -> dict[str, Any]:
    """The Swift app reporting the outcome of an action the backend asked it to
    perform (send_email, send_message — see service/assistant/outbox.py).

    Unlike the fire-and-forget calendar events, these are awaited: the agent
    turn is blocked on this result so it can tell the user "sent" or "that
    failed" truthfully instead of assuming."""
    from service.assistant.outbox import complete
    action_id = str(body.get("action_id") or "")
    if not action_id:
        return {"ok": False, "error": "action_id is required"}
    delivered = complete(action_id, {
        "ok": bool(body.get("ok")),
        "error": str(body.get("error") or ""),
    })
    return {"ok": True, "delivered": delivered}


@app.get("/assistant/events")
async def assistant_events() -> StreamingResponse:
    """SSE stream of reminders + `changed` pings for the Swift app."""
    async def stream():
        q = assistant_hub.subscribe()
        try:
            yield _sse({"type": "hello"})
            while True:
                ev = await q.get()
                yield _sse(ev)
        finally:
            assistant_hub.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/agent/approve")
async def approve(body: dict[str, Any]) -> dict[str, Any]:
    sid = body["session_id"]
    action_id = body["action_id"]
    approved = bool(body["approved"])
    # "once" (default), "always", or "never" — see service/agent/approver.py.
    # Anything unrecognized falls back to "once", so an older client that
    # doesn't send the field keeps its existing ask-every-time behavior.
    scope = str(body.get("scope") or "once")
    if scope not in ("once", "always", "never"):
        scope = "once"
    # Route the approval to whichever in-flight request holds this pending
    # action_id (tool-call ids are globally unique). Filtering by session_id
    # first keeps overlapping requests on different sessions isolated. This
    # replaces the old SESSIONS[session_id] lookup that broke when two requests
    # shared a session.
    for entry in list(SESSIONS.values()):
        if entry["sid"] != sid:
            continue
        if entry["approver"].resolve(action_id, approved, scope):
            return {"ok": True, "scope": scope}
    return {"ok": False, "error": "no pending action for that id"}


@app.get("/skills")
async def list_skills() -> dict[str, Any]:
    """Installed skills and where they live, for a Skills pane."""
    return {"dir": str(skills.SKILLS_DIR),
            "skills": [s.as_dict() for s in skills.all_skills().values()]}


@app.post("/skills/reload")
async def reload_skills() -> dict[str, Any]:
    """Re-scan ~/.moe/skills. Lets the user edit a SKILL.md and pick up the
    change without restarting the backend — editing is most of the workflow
    while writing one."""
    loaded = skills.load()
    return {"ok": True, "count": len(loaded),
            "skills": [s.as_dict() for s in loaded.values()]}


@app.post("/skills/install")
async def install_skill(body: dict[str, Any]) -> dict[str, Any]:
    """Install from a local folder or .md file (see skills.install for why
    there's no install-from-URL)."""
    source = str(body.get("path") or "")
    if not source:
        return {"ok": False, "error": "path is required"}
    return skills.install(source)


@app.post("/skills/{name}/enable")
async def enable_skill(name: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"ok": skills.set_enabled(name, bool(body.get("enabled", True)))}


@app.delete("/skills/{name}")
async def delete_skill(name: str) -> dict[str, Any]:
    return skills.uninstall(name)


@app.get("/mcp/servers")
async def list_mcp_servers() -> dict[str, Any]:
    """Configured MCP servers, whether each is running, and the tools it
    contributed. `config_path` is where the user edits the list."""
    return mcp_manager.status()


@app.post("/mcp/reload")
async def reload_mcp() -> dict[str, Any]:
    """Restart every MCP server from the current mcp.json. Needed after editing
    the config, and the way to recover a server that died."""
    return await mcp_manager.reload()


@app.get("/permissions")
async def get_permissions() -> dict[str, Any]:
    """Standing "always allow / always block" grants, plus the current global
    access mode. Backs a Permissions pane where every rule the user has ever
    clicked through is visible and revocable in one place — a grant you can't
    find again is not really a permission model."""
    from service.safety import grants
    from service.safety.policy import full_access, read_only
    return {
        "grants": grants.all_grants(),
        "never_grantable": grants.never_grantable(),
        "read_only": read_only(),
        "full_access": full_access(),
    }


@app.post("/permissions")
async def set_permission(body: dict[str, Any]) -> dict[str, Any]:
    """Add a standing grant without waiting to be asked mid-conversation."""
    from service.safety import grants
    tool = str(body.get("tool") or "")
    if not tool:
        return {"ok": False, "error": "tool is required"}
    decision = str(body.get("decision") or "allow")
    if decision not in ("allow", "deny"):
        return {"ok": False, "error": "decision must be allow or deny"}
    return grants.grant(tool, body.get("args") or {}, decision=decision,
                        scoped=bool(body.get("scoped", True)))


@app.delete("/permissions/{tool}")
async def revoke_permission(tool: str, scope: str | None = None) -> dict[str, Any]:
    from service.safety import grants
    return {"ok": grants.revoke(tool, scope)}


@app.get("/audit")
async def get_audit(limit: int = 200) -> dict[str, Any]:
    """The tail of the append-only action log (~/.moe/audit.jsonl).

    Every tool call Wisp has run, allowed, blocked, or had denied, newest
    first. The log has always been written; without a way to read it back, "you
    can see what it did" was only true if you knew to go find the file."""
    import json as _json
    from service.safety.audit import AUDIT_LOG
    if not AUDIT_LOG.exists():
        return {"entries": []}
    try:
        lines = AUDIT_LOG.read_text(errors="replace").splitlines()
    except Exception as e:  # noqa: BLE001
        return {"entries": [], "error": str(e)}
    entries = []
    for line in reversed(lines[-max(limit, 1) * 2:]):
        try:
            entries.append(_json.loads(line))
        except Exception:  # noqa: BLE001 — skip a torn/partial line
            continue
        if len(entries) >= limit:
            break
    return {"entries": entries}


# ---------------------------------------------------------------------------
# Smart Search — the ⌘⇧F replacement for ⌘F. See SMART_SEARCH_DESIGN.md.
# ---------------------------------------------------------------------------

@app.post("/search")
async def search(body: dict[str, Any]):
    """Search `text` for `query`, streaming each tier as it lands.

    The Swift app extracts the focused window's text (AX / PDFKit / OCR) and
    posts it here with the query. Events: query, literal, lexical, semantic,
    answering, answer, done — rendered progressively so the first results show
    in about a millisecond and no tier can block the ones before it.

    Deliberately NOT calling ensure_omlx()/ensure_only(): T0 and T1 need no
    model at all, and starting the engine on a keystroke would stall a search
    that was about to succeed without it. The embedder and synthesizer each
    degrade on their own if oMLX isn't up.
    """
    text: str = body.get("text") or ""
    query: str = body.get("query") or ""
    want_answer = bool(body.get("want_answer", True))
    force_answer = bool(body.get("force_answer", False))
    force_global = bool(body.get("force_global", False))

    async def stream():
        try:
            async for event in search_engine.search_stream(
                    client, text, query, want_answer=want_answer,
                    force_answer=force_answer, force_global=force_global):
                yield _sse(event)
        except Exception as e:  # noqa: BLE001 — never leave the UI hanging
            yield _sse({"event": "error", "message": str(e)[:300]})

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/search/prewarm")
async def search_prewarm(body: dict[str, Any]) -> dict[str, Any]:
    """Kick off chunking + embedding for `text` without waiting on it.

    Fired the moment the search panel opens (before the user has typed
    anything) — for a novel-length document the embedding pass over hundreds
    of chunks is the slow part, and running it now spends the seconds the user
    takes to read the field and type their question. By the time they submit a
    real query, /search's own T2 step often finds the document already warm
    instead of paying that cost inline.

    Chunking runs inline (pure CPU, milliseconds even for a whole book) so the
    response carries a real chunk count; the embed pass itself is backgrounded
    so this returns immediately rather than blocking on it.
    """
    text: str = body.get("text") or ""
    if not text:
        return {"ok": False}
    key, chunks, _ = search_engine.extract_and_index(text)

    async def _index() -> None:
        try:
            await ensure_omlx()
            # Same gate as /search/warm — don't pull the embedder in beside
            # the agent model just because a panel opened. The real /search call will
            # index on demand if this was skipped.
            if await _big_model_resident():
                return
            await search_embedder.index_document(key, chunks)
        except Exception:  # noqa: BLE001 — best-effort warm; failures surface on the real search
            pass

    asyncio.create_task(_index())
    return {"ok": True, "chunks": len(chunks)}


async def _big_model_resident() -> bool:
    """True if the agent model is currently loaded.

    Gate for the embedder's speculative warm paths. Both are fired on notch
    hover / panel open — i.e. exactly the moments a chat turn may already be
    running — so without this an idle hover could add the embedder alongside
    the agent model mid-generation, which is the co-residency this machine has no
    headroom for (see the keep_warm comment in lifespan). Skipping the warm
    costs a 320MB cold load on the next search; not skipping it risks failing
    the turn that's already in flight."""
    try:
        return role_to_model("agent") in set(await client.loaded_models())
    except Exception:  # noqa: BLE001 — if oMLX can't be reached there's nothing resident anyway
        return False


@app.post("/search/warm")
async def search_warm() -> dict[str, Any]:
    """Explicitly preload the embedder.

    Wisp no longer calls this at app launch. It remains available for a manual
    warm request; normal Smart Search use loads/indexes through
    `/search/prewarm` when the search panel captures a document.
    """
    await ensure_omlx()
    if await _big_model_resident():
        return {"ok": False, "skipped": "the agent model resident — not adding the embedder alongside it"}
    ok = await search_embedder.warm()
    return {"ok": ok, "model": search_embedder.embedding_model()}


# --------------------------------------------------------------------------
# Research mode — persistent jobs, independent of chat sessions.

@app.post("/research/jobs")
async def research_create(body: dict[str, Any]) -> dict[str, Any]:
    await ensure_omlx()
    try:
        return await research_manager.create(
            client, str(body.get("prompt") or ""),
            depth=str(body.get("depth") or "standard"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/research/jobs")
async def research_list() -> dict[str, Any]:
    return {"jobs": research_manager.store.list_jobs()}


def _research_job_or_404(job_id: str) -> dict[str, Any]:
    try:
        return research_manager.detail(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="research job not found") from exc


@app.get("/research/jobs/{job_id}")
async def research_get(job_id: str) -> dict[str, Any]:
    return _research_job_or_404(job_id)


@app.patch("/research/jobs/{job_id}/plan")
async def research_plan(job_id: str, body: dict[str, Any]) -> dict[str, Any]:
    _research_job_or_404(job_id)
    return research_manager.update_plan(job_id, body.get("plan") or body)


@app.patch("/research/jobs/{job_id}/domains")
async def research_domains(job_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Edit source-domain allow/block lists mid-run; applies from the next round."""
    _research_job_or_404(job_id)
    return research_manager.update_domains(job_id, allowed=body.get("allowed_domains"),
                                           blocked=body.get("blocked_domains"))


@app.post("/research/jobs/{job_id}/pin")
async def research_pin(job_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    _research_job_or_404(job_id)
    return research_manager.pin(job_id, bool((body or {}).get("pinned", True)))


@app.delete("/research/jobs/{job_id}")
async def research_delete(job_id: str) -> dict[str, Any]:
    _research_job_or_404(job_id)
    research_manager.delete(job_id)
    return {"deleted": job_id}


@app.post("/research/jobs/{job_id}/start")
async def research_start(job_id: str) -> dict[str, Any]:
    await ensure_omlx()
    _research_job_or_404(job_id)
    return research_manager.start(job_id, client)


@app.post("/research/jobs/{job_id}/pause")
async def research_pause(job_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    _research_job_or_404(job_id)
    return research_manager.pause(job_id, bool((body or {}).get("paused", True)))


@app.post("/research/jobs/{job_id}/cancel")
async def research_cancel(job_id: str) -> dict[str, Any]:
    _research_job_or_404(job_id)
    return research_manager.cancel(job_id)


@app.post("/research/jobs/{job_id}/steer")
async def research_steer(job_id: str, body: dict[str, Any]) -> dict[str, Any]:
    _research_job_or_404(job_id)
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="steering text is required")
    return research_manager.steer(job_id, text)


@app.get("/research/jobs/{job_id}/events")
async def research_events(job_id: str, after: int = 0):
    _research_job_or_404(job_id)

    async def stream():
        cursor = max(0, int(after))
        quiet = 0
        while True:
            rows = research_manager.store.events_after(job_id, cursor)
            if rows:
                quiet = 0
                for event in rows:
                    cursor = int(event["seq"])
                    yield _sse(event)
            else:
                quiet += 1
                if quiet % 12 == 0:
                    yield ": heartbeat\n\n"
            job = research_manager.store.get_job(job_id) or {}
            if job.get("state") in {"complete", "partial", "cancelled", "failed"} and not rows:
                break
            await asyncio.sleep(0.35)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/research/jobs/{job_id}/report")
async def research_report(job_id: str) -> dict[str, Any]:
    job = _research_job_or_404(job_id)
    return {"id": job_id, "state": job["state"], "markdown": job["report_md"],
            "report": job["report"], "sources": job["sources"]}


@app.get("/research/jobs/{job_id}/export.md")
async def research_export(job_id: str):
    job = _research_job_or_404(job_id)
    filename = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(job["plan"].get("title") or "research"))[:60]
    return Response(job["report_md"], media_type="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{filename}.md"'})
