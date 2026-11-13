"""Demo mode's warm-up: load the chat model now and warm the Ask prompt prefix.

When Demo mode (service/stage_mode.py) turns on, one background task:
1. loads the chat model if it is not resident (the 5.5 GB cold reload is the
   slowest part of a first question);
2. sends ONE tiny generation whose request is the one a real Ask turn builds,
   so the engine's prefix cache holds the system prompt and the tool list;
3. while Demo mode stays on, repeats step 2 every KEEP_ALIVE_S: the engine's
   GPU keep-warm tick stops after 300 s with no request, which Wisp cannot set,
   and the same beat keeps the Ask prefix fresh in the cache.

How the request matches a real Ask turn: it is built BY the real builder.
`service.agent.loop.run_agent` runs with the arguments main.py's /agent passes
on a model-led turn of a fresh chat (`ask_turn_kwargs`), against a stand-in
client. Its first model request (system messages, tool schemas, template
kwargs) is captured and sent to the engine once with WARM_MAX_TOKENS, and the
loop is stopped right there by an exception the stand-in raises. No tool runs,
no approval is asked, nothing is stored. What a real question changes is after
the shared part anyway: the preloaded schemas, the tool-gated system blocks and
remembered facts follow the question, so the engine reuses the token prefix
the two share and prefills the rest.

IMPORTANT rules:
- Never blocks or fails the switch: everything runs in a task, every failure is
  caught and recorded in `state()`, and nothing is logged with prompt text.
- Not a user turn: never touches idle.begin_foreground / last_turn,
  memory_saver.note_intent, a chat session, the store or chat history.
- On the user's own oMLX app it never loads a model when that could evict theirs
  (memory_saver._look_at_omlx's block/others, the guard prewarm uses): it skips
  and records why.
- No power tier is set: the call is like a setup probe (power_tier never caps an
  untiered call, and Demo mode caps nothing anyway). The tier is a payload field,
  not part of the prompt, so it changes nothing about what the cache holds.
- Never while a turn runs or one of Wisp's own calls is on the model.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from service import stage_mode

log = logging.getLogger(__name__)

WARM_PROMPT = "hi"          # neutral: a greeting reads no source and asks for no tool
WARM_MAX_TOKENS = 1         # the prefill is the point; one token ends it
KEEP_ALIVE_S = 240.0        # under the engine's 300 s GPU keep-warm window
REWARM_MIN_S = 60.0         # rewarm() runs at most this often

# Skips after which trying again cannot help while Demo mode stays on.
_FINAL = frozenset({"remote", "external", "no_model", "legacy_routing"})

_client = None
_installed = False
_task: asyncio.Task | None = None
_wake: asyncio.Event | None = None
_warming = False
_last_attempt: float | None = None
_state: dict[str, Any] = {"loaded": None, "warmed_at": None, "skipped": None,
                          "prefix_tokens": None, "cached_tokens": None}

# Injected in tests: no real sleeping, no real clock.
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
_clock: Callable[[], float] = time.monotonic


class _Sent(BaseException):
    """Raised by the stand-in client once the warm request went out: ends run_agent
    before it reads a reply, runs a tool or makes a second call. BaseException, so no
    `except Exception` on the way out can turn it into an answer."""


def state() -> dict[str, Any]:
    """What the last warm-up did, for the UI and debugging."""
    return {**_state, "running": _task is not None and not _task.done()}


def install(client) -> None:
    """Register the Demo-mode listener once. A later call only updates the client."""
    global _client, _installed
    _client = client
    if not _installed:
        stage_mode.on_change(_on_change)
        _installed = True
    if stage_mode.active():
        _start()


def _on_change(on: bool) -> None:
    if on:
        _start()
    else:
        _cancel()


def _start() -> bool:
    """Start the one warm-up task on the running loop. False when one is already
    running or there is no running loop (then nothing happens)."""
    global _task, _wake
    if _task is not None and not _task.done():
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _wake = asyncio.Event()
    _task = loop.create_task(_run())
    return True


def _cancel() -> None:
    task = _task
    if task is not None and not task.done():
        task.cancel()


def _model() -> str | None:
    try:
        from service.config.endpoints import role_target
        return role_target("agent").model
    except Exception:  # noqa: BLE001 — no agent binding: nothing to warm
        return None


def _busy(model: str | None) -> str | None:
    """Why a warm request must wait now, or None."""
    from service import idle
    if idle.foreground_busy():
        return "turn"
    if model and idle.in_flight(model) > 0:
        return "in_flight"
    try:
        from service import memory_saver
        if memory_saver.prewarm_pending():
            return "loading"
    except Exception:  # noqa: BLE001
        pass
    return None


async def rewarm(reason: str) -> str:
    """Warm the Ask prefix again soon. Cheap and safe to call from anywhere on the
    service loop: does nothing unless Demo mode is on, no turn or call of Wisp's
    own is running, no warm-up is mid-request, and REWARM_MIN_S has passed since
    the last attempt. Returns what it did."""
    if not stage_mode.active():
        return "off"
    if why := _busy(_model()):
        return why
    if _warming:
        return "warming"
    if _last_attempt is not None and _clock() - _last_attempt < REWARM_MIN_S:
        return "debounced"
    log.info("stage warm: rewarm (%s)", reason)
    if _task is not None and not _task.done():
        if _wake is not None:
            _wake.set()
        return "woken"
    return "started" if _start() else "no_loop"


async def _nap(seconds: float) -> None:
    """Sleep until the next keep-alive beat or a rewarm(), whichever comes first."""
    wake = _wake
    if wake is None:
        await _sleep(seconds)
        return
    sleeper = asyncio.ensure_future(_sleep(seconds))
    waiter = asyncio.ensure_future(wake.wait())
    try:
        await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (sleeper, waiter):
            t.cancel()
        wake.clear()


async def _run() -> None:
    """Load and warm, then keep it warm until Demo mode ends. Never raises."""
    try:
        final = await _once()
        while not final and stage_mode.active():
            await _nap(KEEP_ALIVE_S)
            if not stage_mode.active():
                break
            if why := _busy(_model()):
                _state["skipped"] = f"busy:{why}"
                continue                       # skip a beat; the turn itself is warm traffic
            final = await _once()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — never out of the task
        _state["skipped"] = f"error:{type(exc).__name__}"
        log.info("stage warm: stopped: %s", type(exc).__name__)


async def _once() -> bool:
    """One load-if-needed and warm. True when Demo mode cannot be helped further."""
    global _warming, _last_attempt
    _warming = True
    _last_attempt = _clock()
    try:
        skipped = await _load()
        if skipped is None:
            skipped = await _warm()
        _state["skipped"] = skipped
        if skipped:
            log.info("stage warm: skipped: %s", skipped)
        return skipped in _FINAL
    finally:
        _warming = False


async def _load() -> str | None:
    """Make the chat model resident if that is safe. None, or why not."""
    client = _client
    from service.inference.remote_policy import remote_only_profile
    if client is None or remote_only_profile() is not None or not getattr(client, "managed", False):
        return "remote"
    model = _model()
    if not model:
        return "no_model"
    if why := _busy(model):
        return f"busy:{why}"                  # the turn loads it
    from service import memory_saver
    bundled = memory_saver.bundled_engine(client)
    on_app = not bundled and memory_saver.omlx_app(client)
    if not bundled and not on_app:
        return "external"                     # some other local server: not Wisp's to load
    try:
        if model in await client.loaded_models():
            _state["loaded"] = True
            return None
        if on_app:
            # The person's own oMLX: a load could evict THEIR models. Same guard as
            # memory_saver.prewarm; skip with the reason instead of evicting.
            view = await memory_saver._look_at_omlx(client, model)
            if view.block is not None:
                _state["loaded"] = False
                return f"omlx:{view.block}"
            if view.others or view.row.get("is_loading"):
                _state["loaded"] = False
                return "omlx:other_models"
        shared = getattr(memory_saver, "_load_shared", None)
        # Through prewarm's slot, so a turn starting now waits on this load
        # (settle_prewarm) instead of POSTing a second one beside it.
        await (shared(client, model) if shared is not None else client.load(model))
        _state["loaded"] = True
        log.info("stage warm: loaded the chat model")
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — the turn loads it anyway
        _state["loaded"] = False
        return f"load_failed:{type(exc).__name__}"


def _refuse_claim(tool, args) -> dict:
    raise _Sent()                             # unreachable: the loop stops at its first request


def _refuse_finish(claim, verified, **refusal) -> bool:
    return False


class _NoApproval:
    async def confirm(self, action) -> bool:
        return False


async def _discard(event: dict) -> None:
    return None


class _CaptureClient:
    """Stands in for the turn's client inside run_agent. Every attribute the loop
    reads (target, managed, base_url, ...) is the real turn client's; residency is a
    no-op (the model was loaded above); the first model request goes to the engine
    once with WARM_MAX_TOKENS and then ends the loop."""

    def __init__(self, inner):
        self._inner = inner
        self.request: dict[str, Any] | None = None
        self.response: dict[str, Any] | None = None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def ensure_only(self, *args, **kwargs) -> None:
        return None

    async def ensure_engine(self) -> None:
        return None

    async def stream_events(self, model, messages, *, tools=None, tool_choice=None,
                            temperature=None, max_tokens=2048, **extra):
        self.request = {"model": model, "messages": messages, "tools": tools,
                        "tool_choice": tool_choice, "temperature": temperature,
                        "max_tokens": WARM_MAX_TOKENS, **extra}
        self.response = await self._inner.chat(
            model, messages, tools=tools, tool_choice=tool_choice, temperature=temperature,
            max_tokens=WARM_MAX_TOKENS, **extra)
        raise _Sent()
        yield  # pragma: no cover — makes this an async generator like the real one

    async def stream(self, *args, **kwargs):
        raise _Sent()
        yield  # pragma: no cover


def ask_turn_kwargs(prompt: str = WARM_PROMPT) -> dict[str, Any] | None:
    """run_agent's keyword arguments for `prompt` as the first message of a fresh
    chat, as main.py's /agent builds them on a model-led turn (main.py: the forbidden
    set near `routing_guard_contract(prompt, model_led=True)`, the RouteDecision for
    `model_led_discovery`, the Ask hints in the `ask_quality` block and the
    `run_agent(...)` call). None when Ask does not run model-led (legacy router).

    Not covered, because a fresh chat has none of them: session pins, an active
    skill, offered creations / a pending send, earlier effects, the saved answer
    style (it rides in the second system message, after the tools, so it is not in
    the shared prefix), and a cloud Super model."""
    import os
    from service import main
    from service.inference.remote_policy import remote_only_profile
    from service.model_only import model_only_enabled
    from service.router.model_led import (continuation_create_exemption,
                                          continuation_requires_baseline,
                                          continuation_send_exemption,
                                          fresh_personal_obligation, memory_excluded,
                                          model_led_enabled, needs_effect_owner,
                                          personal_communication_request,
                                          PRIVATE_EGRESS_TOOLS, registry_specs)
    from service.router.router import routing_guard_contract
    from service.tools.registry import REGISTRY

    model_only = model_only_enabled()
    if remote_only_profile() is not None:
        return None
    if not (model_only or model_led_enabled(os.environ.get("WISP_MODEL_LED_ROUTING"))):
        return None
    last_assistant = ""
    forbidden, bindings = routing_guard_contract(prompt, model_led=True)
    if memory_excluded(prompt):
        forbidden |= frozenset({"recall", "search_memory", "search_conversations",
                                "remember", "forget", "clear_memory"})
        forbidden |= frozenset(t.name for t in registry_specs(REGISTRY)
                               if t.category in {"skill_tool", "mcp_read", "mcp_action"}
                               or t.name in {"run_shell", "run_applescript", "create_tool", "use_skill"})
    if personal_communication_request(prompt):
        forbidden |= PRIVATE_EGRESS_TOOLS
    if continuation_requires_baseline(prompt, last_assistant):
        exempt = continuation_send_exemption(prompt, last_assistant)
        offered = continuation_create_exemption(prompt, last_assistant)
        forbidden |= frozenset(t.name for t in registry_specs(REGISTRY)
                               if needs_effect_owner(t.category)
                               and t.category not in exempt and t.name not in offered)

    guidance: list[str] = []
    ask_forbidden: frozenset[str] = frozenset()
    ask_direct = False
    preload: list[str] = []
    stub_rank: list[str] | None = None
    if model_only:
        from service import model_only as mo
        preload, stub_rank = main._ask_tool_rank(prompt, "")
        guidance.insert(0, mo.ASK_STYLE_HINT)
        if mo.wisp_self_question(prompt):
            guidance.insert(1, mo.wisp_self_hint())
        if mo.day_plan_question(prompt, ""):
            guidance.insert(1, mo.ASK_DAY_PLAN_HINT)
        ask_forbidden = mo.ask_forbidden_tools(prompt, REGISTRY)
        if mo.pure_writing(prompt):
            guidance.insert(1, mo.ASK_COMPOSE_HINT)
        knowledge = mo.knowledge_question(prompt)
        if knowledge:
            guidance.insert(1, mo.ASK_KNOWLEDGE_HINT)
        ask_direct = bool((mo.pure_writing(prompt) and mo.compose_only(prompt)) or knowledge)
    else:
        preload = main._model_led_preload(prompt)
    guidance.append(main.MODEL_LED_UNTRUSTED_WEB_HINT)
    style_hint = "".join("\n" + g for g in guidance)
    return dict(
        tools=[], active_skill="",
        allow_skill_context=True, include_identity_context=True,
        force_first_tool=None, expect_tool_first=False,
        short_circuit_tools=frozenset() if model_only else main._PRESYNTHESIZED_TOOLS,
        style_hint=style_hint or None, public_web_synthesis=False,
        model_led_discovery=True, picture_reading=None, picture_label=None,
        model_led_preload=preload, ask_quality=model_only, ask_direct=ask_direct,
        model_led_stub_rank=stub_rank if model_only else None,
        earlier_effects=False, private_shell_history=False,
        fresh_personal_scope=fresh_personal_obligation(prompt, ""),
        claim_effect=_refuse_claim, finish_effect=_refuse_finish,
        include_memory_context=not ({"recall", "search_memory"} & set(forbidden)),
        multi_round=True, narration_after=frozenset(), direct_calls=[],
        required_tool_groups=(), forbidden_tools=frozenset(forbidden) | ask_forbidden,
        forbidden_reason="", conditional_tools=(), tool_argument_bindings=bindings,
        strict_read_limits=None, reminder_action="",
        test_mode=False, debug=False, planner=False, act_first=frozenset())


def _turn_client():
    """The client an Ask turn uses: main.py makes OMLXClient(target=...) for the
    model-led target. Replaced in tests."""
    from service.config.endpoints import role_target
    from service.inference.omlx_client import OMLXClient
    return OMLXClient(target=role_target("agent"))


async def _warm() -> str | None:
    """Send the real Ask turn's first request once, tiny. None, or why not."""
    model = _model()
    if not model:
        return "no_model"
    if why := _busy(model):
        return f"busy:{why}"
    try:
        kwargs = ask_turn_kwargs(WARM_PROMPT)
    except Exception as exc:  # noqa: BLE001
        return f"build_failed:{type(exc).__name__}"
    if kwargs is None:
        return "legacy_routing"
    from service.agent.loop import run_agent
    inner = _turn_client()
    capture = _CaptureClient(inner)
    try:
        try:
            await run_agent(capture, model, [{"role": "user", "content": WARM_PROMPT}],
                            _discard, _NoApproval(), **kwargs)
        except _Sent:
            pass
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a failed warm-up costs one slow first turn
        return f"warm_failed:{type(exc).__name__}"
    finally:
        close = getattr(inner, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass
    if capture.response is None:
        return "no_request"                   # the loop ended before asking the model
    usage = capture.response.get("usage") if isinstance(capture.response, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details")
    _state["prefix_tokens"] = usage.get("prompt_tokens")
    _state["cached_tokens"] = details.get("cached_tokens") if isinstance(details, dict) else None
    _state["warmed_at"] = time.time()
    log.info("stage warm: warmed (%s prompt tokens, %s cached)",
             _state["prefix_tokens"], _state["cached_tokens"])
    return None
