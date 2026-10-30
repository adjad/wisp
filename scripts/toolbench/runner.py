"""Run cases through production's model-led Ask turn against the fake world.

ISOLATION — `isolate()` must run before anything under `service` is imported:

  * WISP_HOME points at a throwaway directory, so memory facts, caches,
    sessions and grants are empty and nothing under the real ~/.moe is written.
    HOME is left alone: the oMLX settings and runtime authorization live there
    and the client needs them to reach the engine at all.
  * Every WISP_* switch that changes turn behaviour is cleared (_BEHAVIOUR_ENV),
    so the packaged models.yaml / policy.yaml defaults apply as on a fresh
    install: model_only on, narration thinking per models.yaml, full access.
  * The local endpoint is pinned to 127.0.0.1:<omlx_port> in the isolated
    overlay, so the client and the network guard agree on one port.
  * Outbound network is refused except loopback to the oMLX port.
  * Subprocesses are refused except a few read-only probes (`id`, `sysctl`...),
    so a code path that would reach Mail, Messages or the shell fails loudly
    instead of touching the real Mac. Refusals are recorded per case.
  * Every registered tool's function is swapped for a world fake, and
    `install_fakes` refuses to continue if any real function survives. It first
    registers the tools production adds at startup (skill tools, use_skill, a
    fake MCP server), then installs the world's store hooks and seeds memory;
    `uninstall_fakes` undoes all of it.

MODEL BINDING — `bind_model` points the text roles at the model under test with
the same overlay writer Settings uses (config.set_roles), so role_target("agent")
resolves to it exactly as after the user picked it. `profile_as` also gives it
the per-model treatment production gives a rostered model (see bind_model).

MODEL RESIDENCY — `BenchClient.ensure_only` loads the target if it is not
resident and NEVER evicts anything. The loop's own ensure_only would otherwise
treat the isolated config's roster as the keep-warm set and unload whatever the
user's running Wisp depends on. Freeing memory for a big model is an explicit,
separate step (`--exclusive` in the CLI).

FIDELITY — `prepare_turn` mirrors service/main.py's model-led /agent branch
(model_only on, managed local engine, no Super Model) and calls main's own
helpers wherever they exist: _model_only_turn, _managed_local_agent,
_tool_turn_messages, _history_budget, _ask_tool_rank, _answer_prefixed. Every
turn, including one that needs no tool, is one run_agent call, as in
production (a model-led decision always has needs_tools; a no-tool turn is the
loop's tool-free ask_direct step). The two system messages main.py writes
inline are copied here; a test pins them to main.py's text, and another fails
when main.py's run_agent call gains or loses a keyword.

Not mirrored: vision/document turns, the TurnInferenceClient readiness wrapper
(engine start and residency only; BenchClient does residency),
idle.begin_foreground, the rolling-summary fold and assistant-turn persistence
after the turn, and Debug Mode's raw_model_io events (debug=False). No
temperature is sent, so oMLX's per-model profile applies as in production.
"""
from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

# Read-only probes. ps and lsof are how the oMLX transport verifies the engine
# process it is talking to; without them every model request is refused.
_ALLOWED_EXEC = frozenset({"id", "sysctl", "sw_vers", "uname", "memory_pressure", "git", "vm_stat",
                           "ps", "lsof", "pgrep"})
DEFAULT_OMLX_PORT = 8000
_OMLX_PORT = DEFAULT_OMLX_PORT

# Switches a developer shell may carry that would turn a run into something no
# fresh install does. Engine location and API keys are left alone.
_BEHAVIOUR_ENV = ("WISP_MODEL_ONLY", "WISP_MODEL_ONLY_NARRATION_THINKING", "WISP_MODEL_LED_ROUTING",
                  "WISP_PLANNER", "WISP_TERSE_TOOL_SCHEMAS", "WISP_REMOTE_ONLY", "WISP_REMOTE_PARITY",
                  "WISP_REMOTE_FULL", "WISP_REMOTE_BACKGROUND", "WISP_EXPERIMENTS", "WISP_BEHAVIOR",
                  "WISP_BEHAVIOR_PHRASER")
# What isolate() found and cleared, for meta.json.
CLEARED_ENV: dict[str, str] = {}

# Per-case log of anything the guards refused; reset by run_case.
ESCAPES: list[str] = []


def isolate(home: Path, *, omlx_port: int = DEFAULT_OMLX_PORT) -> None:
    global _OMLX_PORT
    _OMLX_PORT = omlx_port
    if "service" in {m.split(".")[0] for m in list(__import__("sys").modules)}:
        raise RuntimeError("toolbench.isolate() must run before importing service.*")
    (home / ".moe").mkdir(parents=True, exist_ok=True)
    os.environ["WISP_HOME"] = str(home / ".moe")
    for var in _BEHAVIOUR_ENV:
        if var in os.environ:
            CLEARED_ENV[var] = os.environ.pop(var)
    _guard_network()
    _guard_subprocess()
    pin_endpoint(omlx_port)


def pin_endpoint(port: int) -> str:
    """Point the managed local endpoint at 127.0.0.1:<port> in the isolated overlay.

    Without it the endpoint comes from oMLX's settings.json, which can name a
    port the network guard refuses. Same overlay key a user would set.
    """
    from service import config
    base = f"http://127.0.0.1:{int(port)}"
    config._save_overlay({"inference": {"endpoints": {"local": {
        "base_url": base, "credential_ref": "local_omlx"}}}})
    return base


def _guard_network() -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def allowed(sock, address) -> bool:
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return True
        host, port = address[0], address[1]
        return host in ("127.0.0.1", "::1", "localhost") and port == _OMLX_PORT

    def connect(sock, address):
        if not allowed(sock, address):
            ESCAPES.append(f"network {address!r}")
            raise PermissionError(f"toolbench: network to {address!r} is blocked")
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if not allowed(sock, address):
            ESCAPES.append(f"network {address!r}")
            raise PermissionError(f"toolbench: network to {address!r} is blocked")
        return real_connect_ex(sock, address)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]


def _exe(args) -> str:
    first = args if isinstance(args, str) else (list(args)[0] if args else "")
    return os.path.basename(str(first).split()[0]) if first else ""


def _guard_subprocess() -> None:
    real_popen_init = subprocess.Popen.__init__

    def popen_init(self, args, *a, **kw):
        if kw.get("shell") or _exe(args) not in _ALLOWED_EXEC:
            ESCAPES.append(f"exec {_exe(args)!r}")
            raise PermissionError(f"toolbench: subprocess {_exe(args)!r} is blocked")
        return real_popen_init(self, args, *a, **kw)

    subprocess.Popen.__init__ = popen_init  # type: ignore[method-assign]

    real_exec = asyncio.create_subprocess_exec

    async def create_exec(program, *args, **kw):
        if _exe([program]) not in _ALLOWED_EXEC:
            ESCAPES.append(f"exec {_exe([program])!r}")
            raise PermissionError(f"toolbench: subprocess {program!r} is blocked")
        return await real_exec(program, *args, **kw)

    async def create_shell(cmd, *a, **kw):
        ESCAPES.append(f"shell {str(cmd)[:40]!r}")
        raise PermissionError("toolbench: shell subprocesses are blocked")

    asyncio.create_subprocess_exec = create_exec  # type: ignore[assignment]
    asyncio.create_subprocess_shell = create_shell  # type: ignore[assignment]


# ---------------------------------------------------------------- service side
# Everything below imports service.* lazily, after isolate().

# Module attributes install_fakes replaces, restored by uninstall_fakes.
_PATCHED_ATTRS = (
    ("service.memory.identity", "user_name"), ("service.memory.identity", "user_emails"),
    ("service.assistant.outbox", "request"), ("service.assistant.outbox", "app_connected"),
    ("service.tools.action_tools", "app_request"),
    ("service.tools.assistant_tools", "prepare_event_move"),
    ("service.tools.assistant_tools", "reminders_matching"),
    ("service.tools.assistant_tools", "past_due_matching"),
    ("service.tools.contacts_tools", "manage_contacts"),   # AUTHORITATIVE_FAKES
)
# What install_fakes found; None while nothing is installed.
_INSTALLED: dict | None = None
# The extra tools register_extra_tools added on the last install.
EXTRA_TOOLS: list[str] = []


def install_fakes(world, *, seed: bool = True) -> set[str]:
    """Swap every registered tool's function for a world fake; return the names.

    Order matters: the tools production adds at startup (skill tools,
    use_skill, the fake "drive" MCP server) are registered first so they get
    fakes too; the world's hooks replace the three assistant-store lookups the
    loop makes before a move or bulk delete; `seed` writes the world's saved
    facts and profile into the isolated stores (idempotent). Everything is
    undone by uninstall_fakes().
    """
    global _INSTALLED, EXTRA_TOOLS
    import importlib

    import service.tools  # noqa: F401  registers the roster
    from service import skills
    from service.mcp import TOOL_PARTS
    from service.skills import tools as skill_tools
    from service.tools.registry import REGISTRY
    from scripts.toolbench import world as world_mod

    if _INSTALLED is None:
        _INSTALLED = {
            "registry": dict(REGISTRY), "tool_parts": dict(TOOL_PARTS),
            "skills": dict(skills._skills), "skill_registered": set(skill_tools._registered),
            "attrs": {(m, a): getattr(importlib.import_module(m), a) for m, a in _PATCHED_ATTRS}}
    EXTRA_TOOLS = world_mod.register_extra_tools()
    import service.memory.identity as identity
    identity.user_name = lambda: world_mod.USER_NAME
    identity.user_emails = lambda: list(world_mod.USER_EMAILS)
    fakes = world_mod.make_fakes(world)
    marker = "_toolbench_fake"
    for name, tool in list(REGISTRY.items()):
        fn = fakes.get(name) or world_mod.generic_fake(world, name, tool.category)
        setattr(fn, marker, True)
        REGISTRY[name] = replace(tool, func=fn)
    real = [n for n, t in REGISTRY.items() if not getattr(t.func, marker, False)]
    if real:
        raise RuntimeError(f"toolbench: real tool functions survived: {real}")
    world_mod.install_hooks(world)
    _install_authoritative_fakes()
    _install_fake_outbox(world)
    if seed:
        world_mod.seed_memory(world)
    return set(REGISTRY)


def _install_authoritative_fakes() -> None:
    """Make each world fake of a production authoritative builtin pass
    router.model_led.authoritative_builtin (its function must be the module's own
    attribute and claim that module). Without this the loop refuses the call with
    "no supported completion contract" for every model. uninstall_fakes restores
    the module attributes (_PATCHED_ATTRS)."""
    import importlib

    from scripts.toolbench import world as world_mod
    from service.router.model_led import BUILTIN_COMPLETION_MODULES
    from service.tools.registry import REGISTRY
    for name in world_mod.AUTHORITATIVE_FAKES:
        module = importlib.import_module(BUILTIN_COMPLETION_MODULES[name])
        fn = REGISTRY[name].func
        fn.__module__ = module.__name__
        setattr(module, name, fn)


def host_refused_tools() -> frozenset[str]:
    """Registered tools the model-led loop refuses for EVERY model: production treats
    them as effects (router.model_led.effectful_call) but they have neither a result
    classification nor an authoritative-builtin receipt, so loop.py answers "This
    action has no supported completion contract. It was NOT run." (skill tools such
    as mac_*, MCP writes, correct_pattern). The scorer reports an expectation that
    needs only these as a route_fault, the host's, instead of a no_effect."""
    from service.router.model_led import (BUILTIN_COMPLETION_MODULES, effectful_call,
                                          has_effect_contract)
    from service.tools.registry import REGISTRY, classify_tool_outcome
    # BUILTIN_COMPLETION_MODULES tools (run_shell, run_applescript, manage_contacts) carry
    # a receipt in production; their fakes here are not authoritative, so judging them
    # by the faked registry would blame production for the harness.
    return frozenset(n for n, t in REGISTRY.items()
                     if n not in BUILTIN_COMPLETION_MODULES and effectful_call(t, {})
                     and not has_effect_contract(t, classify_tool_outcome(n, "")))


def write_tool_names() -> frozenset[str]:
    """Registered tools production treats as effects on a model-led turn
    (router/model_led.effectful_call): the ones scorer.score requires a world
    effect for. Judged with empty arguments, so manage_timers counts as a read."""
    from service.router.model_led import effectful_call
    from service.tools.registry import REGISTRY
    return frozenset(n for n, t in REGISTRY.items() if effectful_call(t, {}))


def uninstall_fakes() -> None:
    """Put back everything install_fakes changed: the registry (extra tools
    removed, real functions back), MCP tool parts, the skill registry and every
    patched module attribute. Seeded memory stays: it lives in the throwaway home."""
    global _INSTALLED, EXTRA_TOOLS
    if _INSTALLED is None:
        return
    import importlib

    from service import skills
    from service.mcp import TOOL_PARTS
    from service.skills import tools as skill_tools
    from service.tools.registry import REGISTRY

    REGISTRY.clear()
    REGISTRY.update(_INSTALLED["registry"])
    TOOL_PARTS.clear()
    TOOL_PARTS.update(_INSTALLED["tool_parts"])
    skills._skills = _INSTALLED["skills"]
    skill_tools._registered.clear()
    skill_tools._registered.update(_INSTALLED["skill_registered"])
    for (mod, attr), value in _INSTALLED["attrs"].items():
        setattr(importlib.import_module(mod), attr, value)
    _INSTALLED, EXTRA_TOOLS = None, []


def _install_fake_outbox(world) -> None:
    """Replace the native-app request channel the loop uses outside the registry.

    Some steps ask the Wisp app directly instead of going through a tool —
    notably reply_to_email's pre-approval check, which has the app prepare
    (and discard) the reply in Mail. No app is connected to this process, so
    each such request would wait out its timeout (~45s) and fail. The fake
    answers the reply preparation from the world's mail and refuses every
    other native request, recording it as an escape.
    """
    import service.assistant.outbox as outbox
    import service.tools.action_tools as action_tools
    from scripts.toolbench import world as world_mod

    async def request(event_type: str, payload: dict, timeout: float = 0.0) -> dict:
        if event_type == "prepare_email_reply":
            m = next((m for m in world.emails if m["id"] == payload.get("message_id")), None)
            if m is None:
                return {"ok": False, "error": "message not found"}
            sender = m["from"].split("<")[-1].rstrip(">").strip()
            return {"ok": True, "reply": {
                "message_id": m["id"], "account": payload.get("account") or m["account"],
                "account_id": payload.get("account_id") or f"acct-{m['account']}",
                "from": world_mod.USER_EMAILS[0], "to": [sender], "cc": [], "bcc": [],
                "subject": f"Re: {m['subject']}", "content": str(payload.get("body") or "")}}
        ESCAPES.append(f"native {event_type}")
        return {"ok": False, "status": "not_attempted", "dispatch_started": False,
                "error": "toolbench: no Wisp app is connected; nothing was done"}

    async def app_connected(*_a, **_k) -> bool:
        return False

    outbox.request = request
    outbox.app_connected = app_connected
    action_tools.app_request = request


TOOL_ERROR_PREFIX = "(error calling "


def bench_client_class():
    from service.inference.omlx_client import OMLXClient
    from service.agent.loop import _est_tokens

    class BenchClient(OMLXClient):
        """Records every model step; loads without ever evicting."""

        steps: list[dict]
        # "off" forces enable_thinking=false on every request (thinking variant C).
        thinking: str = "default"

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.steps = []

        # oMLX samples deterministically without a seed, so repeated trials of
        # one prompt come back identical; the CLI sets one per trial, on the
        # same schedule for every model.
        seed: int | None = None
        # Harness override of the engine profile's temperature on every step.
        # Under model_only production sends none (oMLX's per-model profile
        # decides), so a fair run leaves this None.
        temperature: float | None = None

        def _thinking_kw(self, kw: dict) -> dict:
            if self.seed is not None and "seed" not in kw:
                kw = {**kw, "seed": self.seed}
            if self.temperature is not None:
                kw = {**kw, "temperature": self.temperature}
            if self.thinking != "off":
                return kw
            ctk = dict(kw.get("chat_template_kwargs") or {})
            ctk["enable_thinking"] = False
            return {**kw, "chat_template_kwargs": ctk}

        async def ensure_only(self, model, *, settle_timeout=60.0, exclusive=False, emit=None):
            await self.make_resident(model, timeout=max(settle_timeout, 300.0))

        async def make_resident(self, model: str, *, timeout: float = 300.0) -> None:
            if model in await self.loaded_models():
                return
            await self.load(model)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if model in await self.loaded_models():
                    return
                await asyncio.sleep(1.0)
            raise TimeoutError(f"{model} did not become resident in {timeout:.0f}s")

        # True keeps each step's full request (messages, offered schemas,
        # thinking flag) for building training rows; off for scoring runs.
        capture: bool = False

        def _new_step(self, messages, tools, kw=None) -> dict:
            # Argument errors the loop fed back since the previous model step.
            seen = sum(len(st.get("tool_errors") or ()) for st in self.steps)
            errors = [str(m.get("content") or "") for m in messages
                      if m.get("role") == "tool"
                      and str(m.get("content") or "").lstrip().startswith(TOOL_ERROR_PREFIX)]
            # A name-only stub (model_led.stub_schema) carries no description;
            # calling one loads its schema and runs nothing.
            step = {"calls": [], "content": "", "reasoning_chars": 0, "error": None,
                    "tool_errors": errors[seen:],
                    "ttft": None, "seconds": 0.0, "offered": [t["function"]["name"] for t in tools or ()],
                    "stubs": [t["function"]["name"] for t in tools or ()
                              if "description" not in t["function"]],
                    "prompt_tokens_est": _est_tokens(messages) + _est_tokens(tools or []),
                    "output_chars": 0}
            if self.capture:
                import copy
                ctk = (kw or {}).get("chat_template_kwargs") or {}
                step["request"] = {"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools or []),
                                   "enable_thinking": ctk.get("enable_thinking", True) is not False}
            self.steps.append(step)
            return step

        @staticmethod
        def _finish(step: dict, message: dict) -> None:
            import json
            from service.agent.loop import _clean_tool_name
            step["content"] = message.get("content") or ""
            step["reasoning_chars"] = len(message.get("reasoning_content") or "")
            for tc in message.get("tool_calls") or ():
                fn = tc.get("function") or {}
                raw = fn.get("arguments") or "{}"
                try:
                    args, err = (json.loads(raw) if isinstance(raw, str) else raw), None
                except ValueError as e:
                    args, err = {}, str(e)
                # Scored under the name the loop dispatches ("functions.x<|channel|>..." -> "x").
                name = str(fn.get("name", ""))
                call = {"name": _clean_tool_name(name), "args": args, "parse_error": err}
                if call["name"] != name:
                    call["raw_name"] = name
                step["calls"].append(call)
            step["output_chars"] = (len(step["content"]) + step["reasoning_chars"]
                                    + sum(len(str((tc.get("function") or {}).get("arguments") or ""))
                                          for tc in message.get("tool_calls") or ()))

        async def stream_events(self, model, messages, *, tools=None, **kw):
            step = self._new_step(messages, tools, self._thinking_kw(kw))
            t0 = time.monotonic()
            try:
                async for ev in super().stream_events(model, messages, tools=tools, **self._thinking_kw(kw)):
                    if step["ttft"] is None and ev.get("kind") in ("reasoning", "content", "final"):
                        step["ttft"] = round(time.monotonic() - t0, 3)
                    if ev.get("kind") == "final":
                        self._finish(step, ev["message"])
                    yield ev
            except Exception as e:
                step["error"] = f"{type(e).__name__}: {e}"
                from service.model_only import guard_rejection
                # oMLX's prefill guard answering 400; the Ask loop retries once trimmed.
                step["guard_400"] = guard_rejection(e)
                raise
            finally:
                step["seconds"] = round(time.monotonic() - t0, 3)

        async def chat(self, model, messages, *, tools=None, **kw):
            step = self._new_step(messages, tools, self._thinking_kw(kw))
            t0 = time.monotonic()
            try:
                resp = await super().chat(model, messages, tools=tools, **self._thinking_kw(kw))
                self._finish(step, ((resp.get("choices") or [{}])[0]).get("message") or {})
                return resp
            except Exception as e:
                step["error"] = f"{type(e).__name__}: {e}"
                from service.model_only import guard_rejection
                step["guard_400"] = guard_rejection(e)
                raise
            finally:
                step["seconds"] = step["ttft"] = round(time.monotonic() - t0, 3)

    return BenchClient


def scripted_client_class():
    """A BenchClient that replays a gold plan instead of calling oMLX.

    Each model step pops the next plan entry and streams it back exactly as
    oMLX would (reasoning, content, final message with tool_calls), so the real
    agent loop executes the gold calls against the world fakes and the captured
    requests are byte-for-byte what Wisp would send at that step. A plan entry
    is {"think": str, "calls": [(name, args), ...]} or {"think": str, "text": str}.
    An entry marked "optional": True is skipped when none of its tools is
    offered at that step (e.g. a lookup the route does not allow).

    A gold call to a tool the step offers by name only is played as a call to
    that stub first (what a model must do to load its schema), and the entry
    itself on the next step.
    """
    import json
    Base = bench_client_class()

    class ScriptedClient(Base):
        plan: list[dict]
        # Set when the plan could not be followed (a gold call the step did not
        # offer, or the loop asked for more steps than the plan has).
        failure: str = ""
        # The entry whose stubs were just called; replayed on the next step.
        _held: dict | None = None
        _stubbed: frozenset = frozenset()

        async def make_resident(self, model, *, timeout=300.0):
            return None

        async def ensure_only(self, *a, **k):
            return None

        @staticmethod
        def _called_this_turn(messages) -> set[str]:
            last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
            return {(tc.get("function") or {}).get("name", "") for m in messages[last_user + 1:]
                    if m.get("role") == "assistant" for tc in (m.get("tool_calls") or ())}

        def _next(self, offered: set[str], already: set[str] = frozenset()) -> dict | None:
            while self.plan:
                entry = self.plan.pop(0)
                names = {n for n, _ in entry.get("calls", ())}
                # The router may have made this call itself (direct_calls) before
                # the model's first step; the gold plan then skips it.
                if names and names <= already and not entry.get("repeat"):
                    continue
                if names and not names <= offered:
                    if entry.get("optional"):
                        continue
                    self.failure = f"gold call {sorted(names - offered)} not offered"
                    return None
                return entry
            self.failure = self.failure or "loop asked for a step the plan does not have"
            return None

        def _pick(self, offered: set[str], stubs: set[str], already: set[str]) -> dict | None:
            if self._held is not None:
                entry, self._held = self._held, None
                return entry
            entry = self._next(offered, already - self._stubbed)
            stubbed = {n for n, _ in (entry or {}).get("calls", ())} & stubs
            if stubbed:
                self._held, self._stubbed = entry, self._stubbed | stubbed
                return {"think": entry.get("think", ""), "calls": [(n, {}) for n in sorted(stubbed)]}
            return entry

        def _message(self, entry: dict | None, enable_thinking: bool) -> dict:
            if entry is None:
                return {"role": "assistant", "content": "", "reasoning_content": "", "tool_calls": None}
            calls = [{"id": f"call_{i}_{abs(hash(json.dumps(a, sort_keys=True))) % 10**8}",
                      "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                     for i, (n, a) in enumerate(entry.get("calls", ()))]
            return {"role": "assistant", "content": entry.get("text", ""),
                    "reasoning_content": entry.get("think", "") if enable_thinking else "",
                    "tool_calls": calls or None}

        async def stream_events(self, model, messages, *, tools=None, **kw):
            kw = self._thinking_kw(kw)
            step = self._new_step(messages, tools, kw)
            on = ((kw.get("chat_template_kwargs") or {}).get("enable_thinking", True) is not False)
            msg = self._message(self._pick(set(step["offered"]), set(step["stubs"]),
                                           self._called_this_turn(messages)), on)
            if msg["reasoning_content"]:
                yield {"kind": "reasoning", "text": msg["reasoning_content"]}
            if msg["content"]:
                yield {"kind": "content", "text": msg["content"]}
            self._finish(step, msg)
            step["ttft"] = 0.0
            yield {"kind": "final", "message": msg}

        async def chat(self, model, messages, *, tools=None, **kw):
            kw = self._thinking_kw(kw)
            step = self._new_step(messages, tools, kw)
            on = ((kw.get("chat_template_kwargs") or {}).get("enable_thinking", True) is not False)
            msg = self._message(self._next(set(step["offered"]) | {"__none__"}, set()), on)
            self._finish(step, msg)
            return {"choices": [{"message": msg, "finish_reason": "stop"}]}

    return ScriptedClient


_REAL_CLOCK: dict = {}


def set_clock(when) -> None:
    """Pin "now" for Wisp's prompts and tools to `when` (a naive local datetime), or restore it (None).

    Training worlds each get their own date so the model learns to resolve
    "tomorrow" / "the coming Friday" against the date line in its prompt, not
    against one memorised day. Replaces datetime.datetime / datetime.date and
    time.strftime / time.localtime at the source (so lazy `from datetime import
    datetime` inside a function sees it too) and rebinds service.* modules that
    already captured the originals. time.time() and time.monotonic() stay real:
    every timeout and latency measurement is unaffected.
    """
    import datetime as _dt
    import sys
    import time as _time

    if not _REAL_CLOCK:
        _REAL_CLOCK.update(datetime=_dt.datetime, date=_dt.date, strftime=_time.strftime,
                           localtime=_time.localtime)
    real_dt, real_date = _REAL_CLOCK["datetime"], _REAL_CLOCK["date"]
    if when is None:
        new_dt, new_date = real_dt, real_date
        _time.strftime, _time.localtime = _REAL_CLOCK["strftime"], _REAL_CLOCK["localtime"]
    else:
        pinned = real_dt(when.year, when.month, when.day, when.hour, when.minute, when.second)

        class FakeDatetime(real_dt):
            @classmethod
            def now(cls, tz=None):
                return pinned if tz is None else pinned.astimezone().astimezone(tz)

            @classmethod
            def today(cls):
                return pinned

        class FakeDate(real_date):
            @classmethod
            def today(cls):
                return pinned.date()

        def localtime(secs=None):
            return _REAL_CLOCK["localtime"](pinned.timestamp() if secs is None else secs)

        def strftime(fmt, t=None):
            return _REAL_CLOCK["strftime"](fmt, localtime() if t is None else t)

        new_dt, new_date = FakeDatetime, FakeDate
        _time.localtime, _time.strftime = localtime, strftime
    old = {id(real_dt), id(real_date), id(_dt.datetime), id(_dt.date)}
    _dt.datetime, _dt.date = new_dt, new_date
    for name, mod in list(sys.modules.items()):
        if not (name.startswith("service") or name.startswith("scripts.toolbench")) or mod is None:
            continue
        for attr in ("datetime", "date"):
            val = getattr(mod, attr, None)
            if isinstance(val, type) and id(val) in old | {id(c) for c in _clock_classes}:
                setattr(mod, attr, new_dt if issubclass(val, real_dt) else new_date)
    _clock_classes.update({new_dt, new_date})


_clock_classes: set = set()


# ------------------------------------------------------------ thinking variants

THINKING_MODES = ("default", "narration-on", "narration-off", "off")
# model_only.narration_thinking_enabled reads this first, then models.yaml.
NARRATION_ENV = "WISP_MODEL_ONLY_NARRATION_THINKING"


def set_thinking(client, model: str, mode: str) -> dict:
    """Apply a thinking variant for `model`; return the effective flags.

    Under model_only the loop states enable_thinking ON for every tool-selection
    step of every model (agent/loop._template_kwargs) and reads exactly two
    per-turn switches for the rest: narration_thinking_enabled() and
    thinking_after_tool_required(model). no_thinking_capable / explicit_thinking
    are never read on that path, so they are not variants here.

    default        nothing set: models.yaml `model_only_narration_thinking`
    narration-on   WISP_MODEL_ONLY_NARRATION_THINKING=1
    narration-off  WISP_MODEL_ONLY_NARRATION_THINKING=0. A model listed in
                   thinking_required_after_tool still thinks after a tool
                   result (production keeps it on); only its tool-free
                   direct-answer and clean-narration steps drop it.
    off            narration off AND a harness override sending
                   enable_thinking=false on every request. No production
                   switch turns selection-step thinking off; meta.json says so.
    """
    if mode not in THINKING_MODES:
        raise ValueError(f"thinking must be one of {THINKING_MODES}")
    if mode == "default":
        os.environ.pop(NARRATION_ENV, None)
    else:
        os.environ[NARRATION_ENV] = "1" if mode == "narration-on" else "0"
    client.thinking = "off" if mode == "off" else "default"
    return effective_flags(model, client)


def effective_flags(model: str, client=None) -> dict:
    """What the model-only loop will actually do about thinking for `model`."""
    from service import config
    from service.model_only import model_only_enabled, narration_thinking_enabled
    narration = narration_thinking_enabled()
    after_tool = config.thinking_after_tool_required(model)
    override = getattr(client, "thinking", "default") == "off"
    return {
        "model_only": model_only_enabled(),
        "narration_thinking": narration,
        "thinking_required_after_tool": after_tool,
        # What _template_kwargs sends, per step kind (agent/loop.py ask_narrating).
        "selection_steps_think": not override,
        "narration_after_tool_thinks": (narration or after_tool) and not override,
        "direct_answer_thinks": narration and not override,
        # Read only by summary/background paths, never by the model-only loop.
        "no_thinking_capable": model in config.no_thinking_capable(),
        "explicit_thinking": config.thinks_explicitly(model),
        "tool_capable": config.is_tool_capable(model),
        "env": {NARRATION_ENV: os.environ.get(NARRATION_ENV)},
        "harness_override": "enable_thinking=false on every request" if override else None,
    }


# ------------------------------------------------------------- model binding

# models.yaml lists keyed by exact model id.
PROFILE_LISTS = ("tool_capable", "no_thinking_capable", "thinking_required_after_tool",
                 "explicit_thinking")
# service.config functions that key on the model id outside those lists:
# the context window (oMLX model_settings.json max_context_window, which
# role_target and every history/step budget derive from) and the "ling-" name
# prefix in user_facing_summary_kwargs.
ALIASED_LOOKUPS = ("model_context_window", "user_facing_summary_kwargs")
_ALIAS_ORIGINALS: dict = {}


def _rebind(old, new) -> None:
    """Swap `old` for `new` in service.config and every service module that
    imported it by name (`from service.config import x` at module level)."""
    import sys
    for name, mod in list(sys.modules.items()):
        if mod is None or not name.startswith("service"):
            continue
        for attr in ALIASED_LOOKUPS:
            if getattr(mod, attr, None) is old:
                setattr(mod, attr, new)


def _restore_aliases() -> None:
    from service import config
    for attr, original in _ALIAS_ORIGINALS.items():
        current = getattr(config, attr)
        if current is not original:
            _rebind(current, original)


def _install_aliases(model: str, profile_as: str) -> None:
    from service import config
    for attr in ALIASED_LOOKUPS:
        original = _ALIAS_ORIGINALS.setdefault(attr, getattr(config, attr))

        def aliased(m, *a, _orig=original, **k):
            return _orig(profile_as if m == model else m, *a, **k)
        aliased._toolbench_alias = (model, profile_as)
        _rebind(getattr(config, attr), aliased)


def bind_model(model: str, *, profile_as: str | None = None) -> dict:
    """Make `model` the one every text role resolves to, as Settings would.

    config.set_roles writes the isolated overlay, so role_target("agent") (the
    model-led target) and role_to_model resolve to `model` through production's
    own lookup. With `profile_as`, `model` also gets exactly what production
    gives `profile_as`: membership in each PROFILE_LISTS list (written to the
    same overlay a user would edit) and `profile_as`'s answer from each
    ALIASED_LOOKUPS function. Without it the packaged lists are restored and
    nothing is aliased, so behaviour is unchanged. Returns what took effect.
    """
    from service import config
    from service.config.endpoints import role_target
    from service.setup.guide import TEXT_ROLES

    _restore_aliases()
    profile_as = profile_as if profile_as and profile_as != model else None
    own_window = config.model_context_window(model)
    config.set_roles({role: model for role in TEXT_ROLES})
    packaged = config._packaged_config()
    lists = {}
    for key in PROFILE_LISTS:
        names = list(packaged.get(key) or [])
        if profile_as:
            names = [m for m in names if m != model] + ([model] if profile_as in names else [])
        lists[key] = names
    config._save_overlay(lists)
    if profile_as:
        _install_aliases(model, profile_as)
    target = role_target("agent")
    if target.model != model:
        raise RuntimeError(f"agent role resolved to {target.model!r}, not {model!r}")
    return {"model": model, "profile_as": profile_as, "endpoint": target.endpoint.base_url,
            "context_window": target.context_window, "own_context_window": own_window,
            "lists": {key: model in lists[key] for key in PROFILE_LISTS}}


_SAMPLING_KEY = re.compile(r"temp|top_|min_p|penalt|seed|think|reasoning|context|max_tokens|mtp|"
                           r"draft|template|sampl|power", re.I)


async def engine_settings(client, model: str) -> dict:
    """The per-model settings oMLX will apply to `model` (sampling, thinking,
    window). Real runs only: this talks to the engine. Tries the admin API,
    then the status listing, then the model_settings.json the engine loaded."""
    import json
    out: dict = {"source": None, "settings": {}, "errors": []}
    data = None
    try:
        r = await client._client.get(f"/admin/api/models/{model}/settings")
        if r.status_code == 200 and isinstance(r.json(), dict):
            data, out["source"] = r.json(), "admin_api"
        else:
            out["errors"].append(f"admin_api HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001 — best-effort provenance, never fails a run
        out["errors"].append(f"admin_api {type(e).__name__}")
    if data is None:
        try:
            row = next((m for m in (await client.status()).get("models", ()) if m.get("id") == model), None)
            if row and set(row) - {"id", "loaded"}:
                data, out["source"] = row, "v1/models/status"
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"status {type(e).__name__}")
    if data is None:
        from service.config import OMLX_MODEL_SETTINGS
        try:
            entry = (json.loads(OMLX_MODEL_SETTINGS.read_text()).get("models") or {}).get(model)
            if isinstance(entry, dict):
                data, out["source"] = entry, str(OMLX_MODEL_SETTINGS)
        except (OSError, ValueError) as e:
            out["errors"].append(f"model_settings.json {type(e).__name__}")

    def flat(d, prefix=""):
        for k, v in (d or {}).items():
            if isinstance(v, dict):
                yield from flat(v, f"{prefix}{k}.")
            else:
                yield f"{prefix}{k}", v
    out["settings"] = {k: v for k, v in flat(data) if _SAMPLING_KEY.search(k)}
    return out


# Engine-side settings that decide how a model samples and thinks; a profiled
# comparison is only fair when the candidate's match the profile model's.
PARITY_KEYS = ("temperature", "top_p", "top_k", "max_context_window", "enable_thinking", "mtp_enabled",
               "mtp_adaptive_max_depth")


def engine_mismatches(candidate: dict, profile: dict) -> dict[str, tuple]:
    """{key: (candidate value, profile value)} for every PARITY_KEYS key that
    differs between two engine_settings() results. Keys are matched on their
    last dotted component, so a nested admin-API shape compares too."""
    def pick(result: dict) -> dict:
        return {k.rsplit(".", 1)[-1]: v for k, v in (result.get("settings") or {}).items()}
    a, b = pick(candidate), pick(profile)
    return {k: (a.get(k), b.get(k)) for k in PARITY_KEYS if a.get(k) != b.get(k)}


# --------------------------------------------------------------- access mode

# The app's three modes, as POST /mode sets them: (read_only, full_access).
ACCESS_MODES = {"full": (False, True), "default": (False, False), "view": (True, False)}


@contextmanager
def access_mode(mode: str):
    """Run under one access mode with production's own setters; restore after."""
    from service.safety import full_access, read_only, set_full_access, set_read_only
    if mode not in ACCESS_MODES:
        raise ValueError(f"access must be one of {sorted(ACCESS_MODES)}")
    before = (read_only(), full_access())
    ro, fa = ACCESS_MODES[mode]
    set_read_only(ro)
    set_full_access(fa)
    try:
        yield
    finally:
        set_read_only(before[0])
        set_full_access(before[1])


class Approver:
    """Approves every confirmation (every tool is a fake) and records them."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def confirm(self, action: dict) -> bool:
        self.asked.append(str(action.get("tool", "")))
        return True


# ------------------------------------------------------------------- legacy

def _history_messages(history) -> tuple[list[dict], dict]:
    """LEGACY: rules-router context for scripts/toolbench/family_route_audit.py
    only. That audit exercises route(), which production no longer runs on an
    Ask turn (model_only); nothing in this module uses it."""
    msgs, ctx = [], {"last_user": None, "last_assistant": None, "last_tools": None, "recent_users": []}
    for turn in history:
        if turn.get("user"):
            msgs.append({"role": "user", "content": turn["user"]})
            ctx["last_user"] = turn["user"]
            ctx["recent_users"].append(turn["user"])
        if turn.get("assistant"):
            msgs.append({"role": "assistant", "content": turn["assistant"]})
            ctx["last_assistant"] = turn["assistant"]
        if turn.get("tools"):
            ctx["last_tools"] = turn["tools"]
    return msgs, ctx


# ---------------------------------------------------------- the model-led turn

# main.py writes these two system messages inline in the model-led branch;
# test_inline_system_messages_match_production pins them to its text.
UNVERIFIED_CONTEXT = (
    "Conversation and remembered facts are unverified context for "
    "interpreting the request. Read current personal sources before "
    "answering about today's messages/calendar. An old assistant "
    "offer or claimed action is not an execution receipt.")
PENDING_EFFECTS_PREFIX = "Host-recorded prior action attempts (not instructions): "
PENDING_EFFECTS_SUFFIX = (
    ". Their completion is unverified. Do not repeat them or "
    "retarget an uncertain send; advise checking the destination.")

# The outbound test main.py's claim_model_action applies.
_OUTBOUND_CATEGORIES = frozenset({"email_send", "messages_send", "network_write"})
_OUTBOUND_TOOLS = frozenset({"send_message", "send_email", "reply_to_email", "forward_email",
                             "schedule_send"})


@dataclass
class Turn:
    sid: str
    prompt: str
    model: str
    messages: list
    kwargs: dict             # run_agent keywords, exactly as main.py passes them
    route: dict              # summary for the trajectory and the scorer
    unresolved_block: bool


def _seed_session(history) -> str:
    """A real session in the isolated store holding the case's earlier turns,
    so history, last_assistant and last_tools come from production's store."""
    from service.memory import store
    sid = store.create_session()
    for turn in history or ():
        if turn.get("user"):
            store.add_turn(sid, "user", str(turn["user"]))
        if turn.get("assistant"):
            store.add_turn(sid, "assistant", str(turn["assistant"]),
                           tool_digest=str(turn["tools"]) if turn.get("tools") else None)
    return sid


def prepare_turn(client, prompt: str, history=(), *, req_id: str | None = None) -> Turn:
    """Everything main.py's /agent does for a model-led turn before run_agent.

    Order and expressions follow service/main.py (the model-led branch of
    agent()); line-level comments name the production step each block mirrors.
    Raises if production would not run this turn model-led at all.
    """
    import contextvars
    import json

    import service.main as M
    from service.config import models_config
    from service.config.endpoints import role_target
    from service.knowledge_intent import knowledge_envelope
    from service.memory import store
    from service.model_only import (ASK_CAPABILITY_HINT, ASK_COMPOSE_HINT, ASK_DAY_PLAN_HINT,
                                    ASK_KNOWLEDGE_HINT, ASK_STYLE_HINT, ask_forbidden_tools,
                                    capability_question, compose_only, day_plan_question,
                                    knowledge_question, pure_writing)
    from service.router.model_led import (OPAQUE_OUTBOUND_TOOLS, PRIVATE_EGRESS_TOOLS,
                                          continuation_requires_baseline, fresh_personal_obligation,
                                          memory_excluded, needs_effect_owner, opaque_effect,
                                          personal_communication_request, registry_specs)
    from service.router.router import RouteDecision, _scheduled_send_review, routing_guard_contract
    from service.tools.registry import REGISTRY

    req_id = req_id or uuid.uuid4().hex
    # Entry boundary: credentials never reach the store or the model.
    prompt, _ = M.redact_credentials(prompt)
    # _model_only_turn may switch model_only off for "this turn" through a
    # ContextVar; a copied context keeps that from leaking into later cases.
    if not contextvars.copy_context().run(M._model_only_turn, lambda: client):
        raise RuntimeError("production would not run this turn model-led (model_only off, agent role "
                           "not on the managed local engine, or Super Model on)")
    target = role_target("agent")
    if not M._managed_local_agent(client, target):
        raise RuntimeError(f"client {client.base_url} is not the managed agent target "
                           f"{target.endpoint.base_url}")
    literal_envelope = (knowledge_envelope(prompt) is not None
                        or _scheduled_send_review(prompt) is not None)
    sid = _seed_session(history)
    sess = store.get_session(sid)
    # Production's skill choice for the turn (the bundled mac-controls skill
    # is loaded by install_fakes); a fresh session has no active skill yet.
    from service import skills
    active_skill = "" if literal_envelope else skills.select_for_turn(
        prompt, str((sess or {}).get("active_skill") or ""), store.last_assistant_turn(sid) or "")
    if active_skill:
        store.set_active_skill(sid, active_skill)
    private_shell_history = bool(sess and (store.turn_count(sid) or sess.get("summary")
                                           or sess.get("summarized_idx")))
    last_assistant = store.last_assistant_turn(sid)
    last_tools = store.last_assistant_tools(sid)

    # The source/authority envelope, before any recovery reader.
    forbidden, bindings = routing_guard_contract(prompt)
    if memory_excluded(prompt):
        forbidden |= frozenset({"recall", "search_memory", "search_conversations", "remember", "forget",
                                "clear_memory"})
        forbidden |= frozenset(t.name for t in registry_specs(REGISTRY)
                               if t.category in {"skill_tool", "mcp_read", "mcp_action"}
                               or t.name in {"run_shell", "run_applescript", "create_tool", "use_skill"})
    if personal_communication_request(prompt):
        forbidden |= PRIVATE_EGRESS_TOOLS
    if forbidden and store.active_task(sid):
        forbidden |= frozenset({"run_shell"})
    pending_effects = store.pending_model_effects(sid)
    # model_only never interprets an old owner, so task_owner_verdict stays "none".
    unresolved_block = bool(store.unresolved_action(sid))
    if continuation_requires_baseline(prompt, last_assistant) or unresolved_block:
        forbidden |= frozenset(t.name for t in registry_specs(REGISTRY) if needs_effect_owner(t.category))
    decision = RouteDecision(
        role="agent", model=target.model, needs_tools=True,
        source="model_led", route_source="model_led_discovery",
        reason="Local model interprets request and selects capabilities/tools",
        tool_subset=[], multi_round=True, forbidden_tools=forbidden,
        tool_argument_bindings=bindings)
    if decision.role in models_config().get("inference", {}).get("bindings", {}):
        decision.model = target.model

    # Context: unverified-history preamble, then budgeted store history.
    user_msg = {"role": "user", "content": prompt}
    messages = M._tool_turn_messages(sid, user_msg, max_tokens=M._history_budget(target),
                                     test_mode=False,
                                     verified_results_only=decision.verified_results_only)
    messages.insert(0, {"role": "system", "content": UNVERIFIED_CONTEXT})
    if pending_effects:
        messages.insert(1, {"role": "system", "content": (
            PENDING_EFFECTS_PREFIX + json.dumps(pending_effects) + PENDING_EFFECTS_SUFFIX)})

    persisted: list[int] = []

    def persist_user_turn() -> int:
        if not persisted:
            persisted.append(store.add_turn(sid, "user", prompt))
        from service.memory.facts import store as fact_store
        fact_store.bind_source(req_id, sid, persisted[0])
        return persisted[0]

    def claim_model_action(tool, args):
        persist_user_turn()
        return store.claim_model_effect(
            sid, req_id, tool.name, args,
            outbound=opaque_effect(tool.category) or tool.name in OPAQUE_OUTBOUND_TOOLS
            or tool.category in _OUTBOUND_CATEGORIES or tool.name in _OUTBOUND_TOOLS)

    def finish_model_action(claim, verified):
        return store.finish_model_effect(sid, claim, verified=verified)

    # Answer guidance. A model-led decision has an empty subset and no direct
    # calls, so the routed web/stock synthesis hints can never apply; the Ask
    # hints replace them, in main.py's insertion order.
    guidance: list[str] = []
    hints: list[str] = ["style"]
    ask_preload, ask_stub_rank = M._ask_tool_rank(prompt)
    guidance.insert(0, ASK_STYLE_HINT)
    if capability_question(prompt):
        guidance.insert(1, ASK_CAPABILITY_HINT)
        hints.append("capability")
    if day_plan_question(prompt):
        guidance.insert(1, ASK_DAY_PLAN_HINT)
        hints.append("day_plan")
    ask_forbidden = ask_forbidden_tools(prompt, REGISTRY)
    if pure_writing(prompt):
        guidance.insert(1, ASK_COMPOSE_HINT)
        hints.append("compose")
    knowledge = knowledge_question(prompt)
    if knowledge:
        guidance.insert(1, ASK_KNOWLEDGE_HINT)
        hints.append("knowledge")
    ask_direct = bool((pure_writing(prompt) and compose_only(prompt)) or knowledge
                      or capability_question(prompt))
    guidance.append(M.MODEL_LED_UNTRUSTED_WEB_HINT)
    hints.append("untrusted_web")
    style_hint = "".join("\n" + g for g in guidance)

    fresh_scope = fresh_personal_obligation(prompt, last_tools or "")
    kwargs = dict(
        tools=decision.tool_subset,
        active_skill=active_skill,
        allow_skill_context=not literal_envelope,
        include_identity_context=True,
        force_first_tool=decision.force_first_tool,
        expect_tool_first=decision.expect_tool_first,
        short_circuit_tools=frozenset(),
        style_hint=style_hint or None,
        public_web_synthesis=False,
        model_led_discovery=True,
        model_led_preload=ask_preload,
        ask_quality=True,
        ask_direct=ask_direct,
        model_led_stub_rank=ask_stub_rank,
        private_shell_history=private_shell_history,
        fresh_personal_scope=fresh_scope,
        claim_effect=claim_model_action,
        finish_effect=finish_model_action,
        include_memory_context=not ({"recall", "search_memory"} & set(decision.forbidden_tools)),
        multi_round=decision.multi_round,
        narration_after=decision.narration_after,
        direct_calls=decision.direct_calls,
        required_tool_groups=decision.required_tool_groups,
        forbidden_tools=decision.forbidden_tools | ask_forbidden,
        conditional_tools=decision.conditional_tools,
        tool_argument_bindings=decision.tool_argument_bindings,
        strict_read_limits=decision.strict_read_limits,
        reminder_action=decision.reminder_action,
        test_mode=False,
        debug=False,                     # Debug Mode only adds raw_model_io events
        planner=False,
        act_first=frozenset(),
    )
    route = {"role": decision.role, "needs_tools": True, "source": "model_led",
             "reason": decision.reason, "tool_subset": [], "force_first_tool": None, "direct_calls": [],
             "forbidden_tools": sorted(decision.forbidden_tools | ask_forbidden),
             "preload": list(ask_preload), "stub_rank": list(ask_stub_rank), "ask_direct": ask_direct,
             "hints": hints, "fresh_personal_scope": fresh_scope, "unresolved_block": unresolved_block}
    return Turn(sid=sid, prompt=prompt, model=decision.model, messages=messages, kwargs=kwargs,
                route=route, unresolved_block=unresolved_block)


async def run_case(client, model: str, case, world, *, timeout: float, max_steps: int = 8,
                   access: str | None = None) -> dict:
    """One trial of one case. Returns the trajectory scorer.score() consumes.

    `access` overrides the case's access mode (default: the case's, else full).
    """
    import service.main as M
    from service.agent.loop import run_agent
    from service.config.endpoints import role_target
    from service.memory.capture import current_source

    ESCAPES.clear()
    client.steps = []
    if hasattr(client, "_held"):
        client._held, client._stubbed = None, frozenset()
    world.effects.clear()          # also restores the world (World.reset)
    world.access_needed = set(getattr(case, "access_needed", ()) or ())
    approver = Approver()
    access = access or getattr(case, "access", "") or "full"
    traj: dict = {"model": model, "case": case.id, "max_steps": max_steps, "steps": client.steps,
                  "error": None, "timeout": False, "final": "", "path": "model_led",
                  "access": access, "asked": approver.asked}
    if role_target("agent").model != model:
        # Rebinding here would silently drop a --profile-as treatment.
        raise RuntimeError(f"agent role is {role_target('agent').model!r}, not {model!r}: "
                           "call runner.bind_model(model, profile_as=...) before run_case")
    t0 = time.monotonic()
    try:
        with access_mode(access):
            req_id = uuid.uuid4().hex
            turn = prepare_turn(client, case.prompt, case.history, req_id=req_id)
            traj["route"] = turn.route
            shown: list[str] = []

            async def emit(ev):
                if ev.get("type") == "text":
                    shown.append(str(ev.get("text") or ""))

            agent_emit = (M._answer_prefixed(emit, M.UNRESOLVED_ACTION_NOTICE + "\n\n")
                          if turn.unresolved_block else emit)
            token = current_source.set({"source_type": "user_request", "source_id": req_id,
                                        "session_id": turn.sid, "quote": turn.prompt,
                                        "observed_at": time.time(), "label": "User request"})
            try:
                final = await asyncio.wait_for(run_agent(
                    client, turn.model, turn.messages, agent_emit, approver,
                    max_steps=max_steps, **turn.kwargs), timeout=timeout)
            finally:
                current_source.reset(token)
            traj["final"] = str(final or (shown[-1] if shown else ""))
    except asyncio.TimeoutError:
        traj["timeout"] = True
    except Exception as e:  # noqa: BLE001 — a crashed turn is a scored outcome, not a harness crash
        traj["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    traj.setdefault("route", {})
    traj["seconds"] = round(time.monotonic() - t0, 3)
    traj["model_seconds"] = round(sum(float(s.get("seconds") or 0.0) for s in client.steps), 3)
    traj["guard_retries"] = sum(bool(s.get("guard_400")) for s in client.steps)
    traj["effects"] = list(world.effects)
    traj["escapes"] = list(ESCAPES)
    return traj
