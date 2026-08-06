"""MOE agent service.

Endpoints:
  GET  /health           -> oMLX health
  GET  /models           -> installed models + role map
  POST /chat             -> simple completion (model or role), optional streaming
  POST /agent            -> route + run (agent loop or specialist completion); SSE events
  POST /agent/approve    -> resolve a confirm-tier action for a running session

The /agent stream emits JSON events: session, routed, delta, text, tool_call,
confirm, tool_result, done, error.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from service import auth, idle, idle_unloader, skills
from service.agent import InteractiveApprover, run_agent
from service.assistant import assistant_store
from service.assistant import scheduler as assistant_scheduler
from service.assistant.hub import hub as assistant_hub
from service.config import (
    air_compute_config,
    air_compute_status,
    favorite_models,
    get_super_model_name,
    get_thinking_level,
    is_super_model_active,
    models_config,
    resolve_reasoning_effort,
    role_to_model,
    roster_problems,
    set_air_compute,
    set_air_compute_status,
    set_role,
    set_super_model_active,
    set_super_model_name,
    set_thinking_level,
)
from service.inference.omlx_client import OMLXClient
from service.mcp import manager as mcp_manager
from service.memory import build_messages, maybe_summarize, store
from service.memory.context import MAX_CONTEXT_TOKENS
from service.router import classify_local, route
from service.search import embedder as search_embedder
from service.search import engine as search_engine

# Roles that are "worth keeping" — once a conversation reaches one of these it
# stays pinned there, so a follow-up like "make it faster" isn't downgraded to
# a small model. fast/general never override a pinned heavier expert.
# Roles a conversation "sticks" to so a follow-up isn't downgraded to the fast
# model mid-thread. Deliberately EXCLUDES vision: pinning would force every later
# text-only turn onto the weaker VLM just because one image was sent earlier.
_STICKY_ROLES = {"coding", "reasoning", "agent"}

# Live-history token budget while Super Model is engaged — double the normal
# 16K (see service/memory/context.py's MAX_CONTEXT_TOKENS). This is an
# app-side estimate of how much conversation history to include in the
# request; it's independent of (and smaller than) whatever context window the
# chosen model is actually configured for in oMLX — see set_super_model_name's
# oMLX-side max_context_window bump in service/config/__init__.py.
_SUPER_MODEL_CONTEXT_TOKENS = 32000

# Extra system guidance appended to the agent loop's prompt while Super Model
# is active. The user picks this mode deliberately for the hardest work and
# wants high-quality, VERIFIED output — so the model is told to actually run
# and test the code it writes (it has run_shell/write_file), not just hand back
# untested code. ("Use the Claude chat template for code" → prompt it to work
# the way Claude does: write, run, verify, fix, then report what it checked.)
_SUPER_MODEL_SYSTEM = (
    "You are in Super Model mode: the user deliberately selected you for the "
    "hardest work and freed up the whole machine for you. Favor CORRECTNESS and "
    "completeness over brevity.\n"
    "- Whenever you write or change code, TEST it before finishing: write it to "
    "a file, then actually run it with run_shell — build it, execute it, or run "
    "its tests — and read the output. If it errors or misbehaves, fix it and "
    "re-run until it genuinely works. Never present untested code as done.\n"
    "- Work the way Claude does on code: understand the task, write clean "
    "idiomatic code, verify it runs, then report completion with a one-line note "
    "of what you actually verified."
)

# Tools that already synthesize a COMPLETE final reply internally — see
# email_tools.py's/imessage_tools.py's _summarize(), which each make their own
# gemma call to turn raw headers/lines into real prose. Only THESE are passed
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
# gemma's calendar answers read as bare mechanical dumps of the raw template
# while gpt-oss (never short-circuited, since it doesn't use tool_subset)
# still narrated the same data in prose.
_PRESYNTHESIZED_TOOLS = {"summarize_emails", "summarize_messages"}

# Appended to the agent-loop system prompt ONLY for light-read routes (gemma
# reading the user's own calendar/notes/verbatim data). It deliberately
# overrides the base prompt's "Keep answers concise" for these read-outs: the
# base terse instruction is right for gpt-oss machine operations ("opened
# Safari.") but made gemma's narration of get_upcoming/view_emails/view_messages
# read as a bare mechanical restatement of the raw tool output. Paired with a
# higher sampling temperature (see the run_agent call) so the narration is
# genuinely warmer/more expressive, not just longer.
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

client: OMLXClient
SESSIONS: dict[str, dict] = {}

def _now_line() -> str:
    """Current date/time, appended to every system prompt so trivial questions
    like "what's the time" or "what day is it" can be answered directly from
    context — no tool call needed, and no more wrongly claiming no clock
    access. Mirrors the same line already used in the agent loop (loop.py)."""
    from datetime import datetime
    return ("\nThe current date and time is "
            + datetime.now().strftime("%A, %B %-d, %Y at %-I:%M %p") + ".")


def _profile_block() -> str:
    """Profile context for the non-tool chat branches. Best-effort — a missing
    or unreadable profile must never break a turn.

    Also carries the explicit-memory block (service/memory/facts.py). The two
    travel together everywhere the agent loop injects them, so a fact the user
    asked Wisp to remember is just as visible on a plain chat turn as on a
    tool-using one — otherwise "what's my sister's name" would answer correctly
    only when the router happened to pick a tool route."""
    out = ""
    try:
        from service.memory.profile import profile_context_block
        out += profile_context_block()
    except Exception:  # noqa: BLE001
        pass
    try:
        from service.memory.facts import memory_context_block
        out += memory_context_block()
    except Exception:  # noqa: BLE001
        pass
    return out


ROLE_SYSTEM = {
    "coding": "You are an expert software engineer. Write correct, idiomatic, "
              "production-quality code. Be concise; explain only what matters. "
              "Always format your answer in markdown and put code in fenced code "
              "blocks with a language tag (e.g. ```python).",
    "reasoning": "You are a careful reasoner. Think step by step and give a clear, "
                 "well-justified answer.",
    "vision": "You are a vision assistant. Describe and reason about the image "
              "accurately and concisely.",
    "fast": "You are a fast, friendly assistant. Answer briefly.",
    "general": "You are Wisp, a helpful local assistant on the user's Mac.",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = OMLXClient()
    # Keep the fast summarizer (gemma) always resident: message/calendar/notes
    # summaries route to it (see router), so it must be warm for an instant
    # reply, and it must survive every gpt-oss load (they co-fit — 15GB < the
    # 20.4GB cap). Warm it up in the background so startup isn't blocked on the
    # load; ensure_only reloads it anyway if this races the first request.
    #
    # The Smart Search embedder is deliberately NOT keep-warm. It used to be,
    # on the reasoning that 320MB is negligible — but "negligible" is the wrong
    # frame when the budget is already exhausted. Measured on this machine:
    # gpt-oss 11.25GB + gemma 6.33GB + embedder 0.32GB = 17.9GB against a 19GB
    # hard watermark, leaving nothing for KV cache, and a large prefill then
    # pushed gpt-oss to 11.89GB and failed a model load with a Metal
    # command-buffer error. Every megabyte that isn't required to be resident
    # should not be. The embedder is a 320MB cold load on ⌘⇧F, and
    # embedder.warm()/`/search/prewarm` already load it on notch hover — which
    # is early enough that search never actually pays for it interactively.
    #
    # Dropping it from keep-warm also means a non-exclusive ensure_only()
    # evicts it, so it can no longer creep back in alongside gpt-oss mid-turn.
    summarizer = role_to_model("fast")
    client.set_keep_warm({summarizer})

    async def _warm_summarizer():
        try:
            await ensure_omlx()
            await client.ensure_only(summarizer)
            # Preload the embedder out of band — the first search of a session
            # shouldn't be the thing that discovers it isn't loaded yet.
            await search_embedder.warm()
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

    async def _check_roster():
        """Report a roster that can't actually work, rather than failing mutely.

        Runs out of band because it needs oMLX's installed-model list, which
        means waiting on a server that may still be starting.
        """
        try:
            installed = await client.models()
        except Exception:  # noqa: BLE001 — oMLX may not be up yet; check config alone
            installed = None
        for problem in roster_problems(installed):
            print(f"[wisp] config warning: {problem}", flush=True)

    roster_task = asyncio.create_task(_check_roster())
    warm_task = asyncio.create_task(_warm_summarizer())
    unloader_task = asyncio.create_task(idle_unloader.run(client))
    assistant_task = asyncio.create_task(assistant_scheduler.run())
    yield
    roster_task.cancel()
    warm_task.cancel()
    unloader_task.cancel()
    assistant_task.cancel()
    mcp_task.cancel()
    await mcp_manager.stop()
    await client.aclose()


app = FastAPI(title="Wisp", lifespan=lifespan)


@app.middleware("http")
async def _require_api_key(request, call_next):
    """Gate every endpoint behind the local shared key. See service/auth.py for
    why loopback alone isn't sufficient here.

    Middleware rather than a per-route dependency so a newly added endpoint is
    protected by default instead of by remembering to opt in.
    """
    if request.url.path not in auth.PUBLIC_PATHS:
        supplied = request.headers.get(auth.HEADER_NAME, "")
        if not hmac.compare_digest(supplied, auth.api_key()):
            return JSONResponse(
                status_code=401,
                content={"detail": f"missing or invalid {auth.HEADER_NAME} header"},
            )
    return await call_next(request)


@app.get("/ping")
async def ping() -> dict[str, Any]:
    """Unauthenticated liveness probe — returns a constant, exposes nothing.
    Lets the app tell "backend is up" from "port is taken by something else"
    before it has read the key."""
    return {"ok": True, "service": "wisp"}

def _find_omlx_cli() -> str:
    """Locate oMLX's command-line tool.

    Used to start/stop the engine (see /wake_omlx and /shutdown_omlx). oMLX is
    a separate app Wisp does not ship, so where it lives is the user's choice:
    WISP_OMLX_CLI wins, then the two normal install locations. Returns the
    /Applications path as a last resort so the callers' FileNotFoundError
    handling stays on its usual path when oMLX isn't installed at all.
    """
    import os
    from pathlib import Path

    override = os.environ.get("WISP_OMLX_CLI", "").strip()
    if override:
        return override
    candidates = [
        Path("/Applications/oMLX.app/Contents/MacOS/omlx-cli"),
        Path.home() / "Applications/oMLX.app/Contents/MacOS/omlx-cli",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return str(candidates[0])


OMLX_CLI = _find_omlx_cli()


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
    return {"installed": await client.models(), "roles": models_config()["roles"]}


@app.post("/config")
async def config(body: dict[str, Any]) -> dict[str, Any]:
    role, model = body.get("role"), body.get("model")
    if role and model:
        set_role(role, model)
    return {"ok": True, "roles": models_config()["roles"]}


# Super Model — user-forced override for the hardest requests, never chosen by
# the router (see runner()'s override applied after normal routing). The
# Swift side quits other apps to free RAM BEFORE flipping `active` on; this
# endpoint itself only tracks state, no app-quitting happens server-side.
@app.get("/super_model")
async def get_super_model() -> dict[str, Any]:
    # active/model are always answerable regardless of oMLX's state; only the
    # installed-models list needs it alive, so a transiently-down engine (e.g.
    # right after Wisp launches, before oMLX has started) degrades to an empty
    # list there instead of failing the whole response.
    try:
        installed = await client.models()
    except Exception:  # noqa: BLE001
        installed = []
    return {
        "active": is_super_model_active(),
        "model": get_super_model_name(),
        "installed": installed,
        # Read straight from oMLX's own settings file, not the live server —
        # available even when oMLX's server subprocess isn't running (which
        # `installed` above needs and frequently isn't, since Wisp only
        # starts it on demand).
        "favorites": favorite_models(),
    }


@app.post("/super_model/model")
async def set_super_model(body: dict[str, Any]) -> dict[str, Any]:
    model = str(body.get("model") or "")
    if model:
        set_super_model_name(model)
    return {"model": get_super_model_name()}


# Idle-unload timeout to restore when Super Model turns off (None = wasn't
# active, nothing to restore). Super Model bumps it to 30 min so a big model
# doing long, high-quality agentic work isn't reclaimed between turns.
_pre_super_idle_minutes: float | None = None
_SUPER_MODEL_IDLE_MINUTES = 30.0


@app.post("/super_model/toggle")
async def toggle_super_model(body: dict[str, Any]) -> dict[str, Any]:
    global _pre_super_idle_minutes
    active = bool(body.get("active"))
    was_active = is_super_model_active()
    set_super_model_active(active)
    if active:
        # Hold the model resident far longer than the usual idle timeout —
        # Super Model turns can be long, and paying a full cold reload of a
        # large model between turns would defeat the point. Save the prior
        # value so turning Super Model off restores the user's own setting.
        if not was_active:
            _pre_super_idle_minutes = idle.get_idle_minutes()
        idle.set_idle_minutes(_SUPER_MODEL_IDLE_MINUTES)
        # Evict gemma (or whatever else is resident) and load the Super Model
        # target RIGHT NOW, not lazily on the next chat request — matching how
        # quitting other apps and raising the VRAM limit already happen at
        # toggle time (see AppQuitter/VRAMLimit on the Swift side), not on
        # first use. exclusive=True skips oMLX's keep-warm exemption entirely
        # (see OMLXClient.ensure_only's docstring), so this is a REAL full
        # eviction, not the partial one that preserves the always-warm
        # summarizer for normal turns. Best-effort: a hiccup here (oMLX cold-
        # starting, a slow load) doesn't block the toggle itself — the first
        # actual /agent request still calls ensure_only again as a fallback.
        try:
            await ensure_omlx()
            await client.ensure_only(get_super_model_name(), exclusive=True)
        except Exception:  # noqa: BLE001
            pass
    else:
        # Restore the pre-Super-Model idle timeout so normal memory reclaim
        # resumes at whatever cadence the user had configured.
        if _pre_super_idle_minutes is not None:
            idle.set_idle_minutes(_pre_super_idle_minutes)
            _pre_super_idle_minutes = None
    return {"active": is_super_model_active()}


@app.get("/air_compute")
async def get_air_compute() -> dict[str, Any]:
    return {"config": air_compute_config(), "status": air_compute_status()}


@app.post("/air_compute")
async def post_air_compute(body: dict[str, Any]) -> dict[str, Any]:
    cfg = set_air_compute(body)
    return {"config": cfg, "status": air_compute_status()}


@app.post("/air_compute/check")
async def check_air_compute() -> dict[str, Any]:
    cfg = air_compute_config()
    if not cfg["base_url"].strip():
        set_air_compute_status(
            "not_configured",
            message="No Air node address set. Enter the other Mac's URL first.",
        )
        return {"config": air_compute_config(), "status": air_compute_status()}
    base_url = cfg["base_url"].rstrip("/")
    timeout = max(0.1, cfg["timeout_ms"] / 1000)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as remote:
            resp = await remote.post(
                f"{base_url}/router/classify",
                json={"prompt": "hello", "has_image": False},
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get("role") not in {"coding", "reasoning", "agent", "vision", "fast", "general"}:
            raise ValueError("invalid router response")
        latency_ms = int((time.perf_counter() - started) * 1000)
        set_air_compute_status("available", latency_ms=latency_ms)
    except Exception as exc:  # noqa: BLE001 — status probing must not break settings
        set_air_compute_status("offline", message=str(exc), latency_ms=None)
    return {"config": air_compute_config(), "status": air_compute_status()}


@app.post("/router/classify")
async def router_classify(body: dict[str, Any]) -> dict[str, Any]:
    """Classify a prompt for another Wisp instance without doing final work."""
    await ensure_omlx()
    decision = await classify_local(
        client,
        str(body.get("prompt", "")),
        has_image=bool(body.get("has_image")),
    )
    return decision.as_dict()


@app.get("/mode")
async def get_mode() -> dict[str, Any]:
    from service.safety import full_access, read_only
    return {"read_only": read_only(), "full_access": full_access()}


@app.post("/mode")
async def set_mode(body: dict[str, Any]) -> dict[str, Any]:
    from service.safety import full_access, read_only, set_full_access, set_read_only
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
    """Free just the heavy agent model (gpt-oss, ~12.7GB) — used when the notch
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


@app.get("/thinking_level")
async def get_thinking_level_route() -> dict[str, Any]:
    return {"level": get_thinking_level()}


@app.post("/thinking_level")
async def set_thinking_level_route(body: dict[str, Any]) -> dict[str, Any]:
    return {"level": set_thinking_level(str(body.get("level", "")))}


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
    """
    prompt: str = body["prompt"]
    has_image = bool(body.get("image"))

    # Resolve / create the persistent session.
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
        await queue.put(ev)

    async def runner():
        try:
            await ensure_omlx()
            last_assistant = store.last_assistant_turn(sid) if sess else None
            last_tools = store.last_assistant_tools(sid) if sess else None

            # No separate LLM-classify step anymore (see route()'s docstring —
            # it was silently mis-classifying ~15% of genuinely tool-needing
            # requests). rule_route handles the clear cases instantly; anything
            # ambiguous now goes straight to gpt-oss via the agent loop.
            # last_tools lets a bare scope fragment ("from yesterday") continue
            # the previous turn's light-read domain instead of defaulting to gpt-oss.
            decision = await route(prompt, has_image=has_image, last_assistant=last_assistant,
                                   last_tools=last_tools)

            # If gpt-oss is ALREADY resident, pin GENERATION to it too — don't
            # swap models for a trivial follow-up once it's loaded. Vision is
            # the one thing that must still swap.
            #
            # EXCLUDES light-read routes (tool_subset set — messages/email/
            # calendar/notes/planning). Originally this covered them too, on the
            # theory that reusing a resident gpt-oss beats paying a swap back to
            # gemma. Measured that theory out and it's a net loss in the common
            # case: gpt-oss's OWN generation is 3-4x slower than gemma's per
            # response even with zero swap cost (~9s vs ~1-3s, see the planning
            # A/B), and answering there resets gpt-oss's idle timer, extending
            # how long light-reads keep getting hijacked instead of settling
            # back onto the fast path. It also fired across UNRELATED sessions —
            # any earlier gpt-oss use anywhere would silently drag a fresh
            # conversation's plain "what's on my calendar today" onto gpt-oss,
            # which is the reported bug this fixes. Light-reads now always
            # target gemma regardless of what else is resident.
            if decision.role != "vision" and not has_image and not decision.tool_subset:
                agent_model = role_to_model("agent")
                try:
                    resident = set(await client.loaded_models())
                except Exception:  # noqa: BLE001
                    resident = set()
                if agent_model in resident:
                    decision.model = agent_model
                    if decision.route_source == "rules":
                        decision.reason += " · kept on resident gpt-oss (no swap)"

            # Sticky routing: don't downgrade a conversation that already reached
            # a heavier expert. A pinned sticky role wins over fast/general.
            # EXCEPTION: a light-read decision (tool_subset set — messages/email/
            # calendar/notes/planning on the always-warm gemma) is a confident
            # RULE match carrying its own verified-safe toolset, not the kind of
            # ambiguous downgrade this guard exists to prevent. Without this
            # exemption, ANY earlier agent/coding/reasoning turn in a session
            # permanently drags every later "what's on my calendar" onto gpt-oss
            # for the rest of the conversation — a real reported bug (gpt-oss
            # answering a plain calendar-today read) that defeats the entire
            # point of keeping gpt-oss asleep for these.
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

            # Super Model wins over EVERYTHING above — router, resident-model
            # pin, sticky-role pin — since it's the last thing that touches
            # `decision` before it's used. Only ever True from an explicit
            # user toggle (see /super_model/toggle); the router itself never
            # sets it.
            if is_super_model_active():
                decision.role = "agent"
                decision.model = get_super_model_name()
                decision.needs_tools = True
                # NOT expect_tool_first: that forces tool_choice="required" on
                # step 0, which made even "hello" get the "you must call a
                # tool" nudge-and-retry (the model's reply literally complained
                # it was being told to use a tool). Tools stay AVAILABLE; the
                # model decides per-prompt whether to use one.
                decision.expect_tool_first = False
                decision.tool_subset = None
                decision.route_source = "super_model"
                decision.reason = "Super Model — user-forced override"

            await emit({"type": "routed", **decision.as_dict()})
            # gpt-oss turns get the FULL memory budget (exclusive) rather than
            # trying to co-reside with gemma — verified that gemma gets evicted
            # anyway once gpt-oss is actually generating (its KV cache grows
            # past the point where both fit), so attempting co-residency here
            # just adds overhead for a guarantee that doesn't hold. Unlike
            # before, gpt-oss is NOT swapped back out for gemma once the turn
            # ends — it stays resident (including for later chats/sessions,
            # per the resident-pin check above) until idle_unloader's normal
            # timeout reclaims it. Gemma only comes back once gpt-oss actually
            # idles out and a messages/calendar/notes request needs it.
            # Super Model always gets the exclusive budget too — the whole
            # point of quitting other apps for it is giving it the full 20GB,
            # not trying to co-reside with gemma.
            is_gpt_oss_turn = decision.model == role_to_model("agent") or decision.route_source == "super_model"
            await client.ensure_only(decision.model, exclusive=is_gpt_oss_turn)
            if decision.role in _STICKY_ROLES:
                store.set_pinned(sid, decision.role, decision.model)

            user_msg: dict[str, Any] = {"role": "user", "content": prompt}
            if has_image:
                user_msg["content"] = [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": body["image"]}},
                ]
            # Super Model gets a bigger live-history budget (32K vs. the usual
            # 16K estimated tokens) — the whole feature is for the hardest,
            # often longest-running requests, where trimming recent turns away
            # into the rolling summary sooner is exactly the wrong tradeoff.
            history_budget = _SUPER_MODEL_CONTEXT_TOKENS if is_super_model_active() else MAX_CONTEXT_TOKENS
            messages = build_messages(sid, max_tokens=history_budget) + [user_msg]

            if decision.needs_tools:
                # A route may name the one tool that must run first (memory
                # saves do — see _mk_light's `force`). Code delegation keeps
                # priority: an agentic request that's also writing code has to
                # reach write_code before anything else.
                force_tool = ("write_code" if decision.needs_code_delegation
                              else decision.force_first_tool)
                # Light-read routes (tool_subset set) may include a
                # pre-synthesized tool (summarize_emails/summarize_messages —
                # see _PRESYNTHESIZED_TOOLS) whose result is already the final
                # answer; short-circuit past the redundant "model restates the
                # tool result" round trip ONLY for those. See run_agent's
                # short_circuit_tools docstring for the compound-read
                # exception (multiple tools in one step still get merged).
                # Light-read = tool_subset set = gemma narrating the user's own
                # data. Give it the expressive style hint + a warmer temperature.
                # gpt-oss machine-operation runs (no tool_subset) keep the terse
                # default so "opened Safari."-style replies stay tight.
                is_light_read = bool(decision.tool_subset)
                final = await run_agent(client, decision.model, messages, emit, approver,
                                        tools=decision.tool_subset,
                                        force_first_tool=force_tool,
                                        expect_tool_first=decision.expect_tool_first,
                                        short_circuit_tools=_PRESYNTHESIZED_TOOLS,
                                        style_hint=_LIGHT_READ_STYLE if is_light_read else None,
                                        system_suffix=_SUPER_MODEL_SYSTEM if is_super_model_active() else None,
                                        temperature=0.6 if is_light_read else 0.0)
                captured["text"] = final or captured["text"]
            elif decision.role == "reasoning":
                # Runs on gpt-oss (already resident for agent/general — no swap)
                # at reasoning_effort=medium: empirically as rigorous as the
                # retired Phi-4 specialist, ~5-8x faster. oMLX splits its
                # chain-of-thought into reasoning_content (collapsible in the
                # UI) from the final answer in content.
                sysp = ROLE_SYSTEM["reasoning"] + _now_line()
                msgs = [{"role": "system", "content": sysp}] + messages
                chat_task = asyncio.create_task(client.chat(
                    decision.model, msgs, max_tokens=8000, temperature=0.3,
                    chat_template_kwargs={"reasoning_effort": resolve_reasoning_effort(decision.role)}))
                # Even at medium effort a hard prompt can run silent for a while
                # with no SSE traffic, which looks identical to a hung request
                # and can trip client-side idle timeouts. Keep the connection
                # alive and visibly working until the completion lands.
                while not chat_task.done():
                    await emit({"type": "heartbeat"})
                    try:
                        await asyncio.wait_for(asyncio.shield(chat_task), timeout=12)
                    except asyncio.TimeoutError:
                        continue
                resp = await chat_task
                msg = resp["choices"][0]["message"]
                reasoning = (msg.get("reasoning_content") or "").strip()
                answer = (msg.get("content") or "").strip()
                if not answer:  # model kept everything in content
                    answer = _strip_think(reasoning) or "(no answer)"
                    reasoning = ""
                # Raw request + the full raw oMLX response (non-streaming, so
                # this is the actual wire response, not a reassembly) — for
                # debug export, tracked unconditionally like other per-turn
                # debug metadata.
                await emit({
                    "type": "raw_model_io",
                    "model": decision.model,
                    "request": {
                        "messages": msgs, "max_tokens": 8000, "temperature": 0.3,
                        "chat_template_kwargs": {"reasoning_effort": resolve_reasoning_effort(decision.role)},
                    },
                    "response": resp,
                })
                if reasoning:
                    await emit({"type": "reasoning", "text": reasoning})
                await emit({"type": "text", "text": answer})
                await emit({"type": "done"})
            else:
                # This is the MOST-USED path (every general/fast/coding reply) and
                # previously had zero stall protection — unlike the reasoning
                # branch and agent loop, which both have heartbeats. If token
                # generation ever paused mid-stream (oMLX hiccup, memory
                # pressure), partial text just sat there with no signal of
                # whether it was still working or dead — the reported "stops
                # responding, just a void" symptom. Same watchdog pattern as the
                # reasoning branch: race each next-chunk against a short timeout
                # and emit a heartbeat on silence instead of going quiet.
                #
                # gpt-oss (general/fast/coding all resolve to it) splits output
                # into reasoning_content vs content same as the reasoning branch —
                # but this path used client.stream(), which only ever forwards
                # delta.content and silently drops reasoning_content. On prompts
                # whose chain-of-thought runs long ("explain how X works" was the
                # reported trigger), reasoning can consume most or all of the
                # token budget before content ever starts, so content stays empty
                # for the whole request: no delta events, just heartbeats then
                # done. Switched to stream_events() (reasoning + content + final)
                # so we can apply the same "model kept everything in the wrong
                # field" fallback the reasoning branch already has.
                # Plain (non-tool) chat gets the same profile context the agent
                # loop has had all along — otherwise "what's my schedule like"
                # answered from the profile in a tool-using turn but not in a
                # conversational one, which reads as Wisp randomly forgetting.
                sysp = (ROLE_SYSTEM.get(decision.role, ROLE_SYSTEM["general"])
                        + _now_line() + _profile_block())
                msgs = [{"role": "system", "content": sysp}] + messages
                events = client.stream_events(
                    decision.model, msgs, max_tokens=8000,
                    chat_template_kwargs={"reasoning_effort": resolve_reasoning_effort(decision.role)}).__aiter__()
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
                # export, tracked unconditionally like other per-turn debug
                # metadata.
                await emit({
                    "type": "raw_model_io",
                    "model": decision.model,
                    "request": {
                        "messages": msgs, "max_tokens": 8000,
                        "chat_template_kwargs": {"reasoning_effort": resolve_reasoning_effort(decision.role)},
                    },
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
            reply = captured["text"] or "".join(captured["deltas"])
            digest = ", ".join(dict.fromkeys(captured["tools"])) or None
            store.add_turn(sid, "user", prompt)
            store.add_turn(sid, "assistant", reply.strip(), tool_digest=digest)
            await maybe_summarize(client, sid, decision.model)
        except Exception as e:  # noqa: BLE001
            await emit({"type": "error", "message": str(e)})
            await emit({"type": "done"})
        finally:
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
    """On-demand combined brief (calendar + email) for the Daily Summary button."""
    from datetime import datetime as _dt

    from service.assistant.brief import build_daily_brief
    part = "morning" if _dt.now().hour < 12 else "evening"
    await ensure_omlx()
    text = await build_daily_brief(part)
    return {"ok": True, "text": text, "part_of_day": part}


@app.get("/assistant/profile")
async def get_assistant_profile() -> dict[str, Any]:
    """The profile Wisp has built about the user so far (see
    service/memory/profile.py) plus per-source last-scanned bookkeeping, for a
    dedicated Settings/Profile view. Empty text until build_profile has run."""
    from service.memory.profile import get_profile_meta, get_profile_text
    return {"text": get_profile_text(), "meta": get_profile_meta()}


@app.get("/memory/facts")
async def list_facts(limit: int = 500) -> dict[str, Any]:
    """Everything the user has asked Wisp to remember, for a Settings view.

    Separate from /assistant/profile on purpose: the profile is inferred and
    rebuilt nightly, these are stated by the user and never expire on their own,
    so they need their own list where each one can be individually deleted."""
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


@app.post("/assistant/profile/build")
async def build_assistant_profile() -> dict[str, Any]:
    """On-demand (re)build, for a Settings button — same local-only pass over
    already-synced Mail/Messages/Notes/Calendar as the `build_profile` agent
    tool, just reachable without going through chat. Can take a little while
    (several local-model calls); the client should show it as in-progress."""
    from service.memory.profile import build_profile as _build_profile
    await ensure_omlx()
    return await _build_profile()


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
    (gemma digest, synced every 5min), raw feeds view_emails (verbatim lookup,
    synced once/day — see MailReader.swift). Each field is independent and
    only touched when present: MailReader posts them on separate cadences, so
    a headers-only sync must not wipe out the still-fresh raw cache (and
    vice versa)."""
    from service.tools.email_tools import (
        cache_emails,
        cache_history_headers,
        cache_raw_emails,
        set_email_availability,
    )
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
    # The user's own account addresses — identity ground truth for the profile
    # builder (see profile._identity_block).
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


@app.post("/assistant/sync/messages")
async def assistant_sync_messages(body: dict[str, Any]) -> dict[str, Any]:
    """Receive recent iMessage/SMS lines from the Swift app (which holds the
    Full Disk Access needed to read chat.db directly) so the messages tool
    can summarize them with gemma."""
    from service.tools.imessage_tools import cache_contacts, cache_messages
    diag = body.get("diagnostics") or {}
    # Contacts arrive on their own (much slower) schedule from ContactsReader,
    # so this endpoint accepts either payload independently — a contacts push
    # carries no "lines" and must not wipe the message cache.
    if "contacts" in body:
        cache_contacts(body.get("contacts") or {})
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
            # gpt-oss just because a panel opened. The real /search call will
            # index on demand if this was skipped.
            if await _big_model_resident():
                return
            await search_embedder.index_document(key, chunks)
        except Exception:  # noqa: BLE001 — best-effort warm; failures surface on the real search
            pass

    asyncio.create_task(_index())
    return {"ok": True, "chunks": len(chunks)}


async def _big_model_resident() -> bool:
    """True if gpt-oss is currently loaded.

    Gate for the embedder's speculative warm paths. Both are fired on notch
    hover / panel open — i.e. exactly the moments a chat turn may already be
    running — so without this an idle hover could add the embedder alongside
    gpt-oss mid-generation, which is the co-residency this machine has no
    headroom for (see the keep_warm comment in lifespan). Skipping the warm
    costs a 320MB cold load on the next search; not skipping it risks failing
    the turn that's already in flight."""
    try:
        return role_to_model("agent") in set(await client.loaded_models())
    except Exception:  # noqa: BLE001 — if oMLX can't be reached there's nothing resident anyway
        return False


@app.post("/search/warm")
async def search_warm() -> dict[str, Any]:
    """Preload the embedder. The app calls this on launch and on notch hover so
    the first real search never pays the cold load."""
    await ensure_omlx()
    if await _big_model_resident():
        return {"ok": False, "skipped": "gpt-oss resident — not adding the embedder alongside it"}
    ok = await search_embedder.warm()
    return {"ok": ok, "model": search_embedder.embedding_model()}
