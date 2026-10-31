"""Drive Wisp's real /agent Ask route with a scripted (ideal) model.

Import order matters: boot (fresh WISP_HOME) -> network guard -> clock patch ->
service imports. Every registered tool's function is replaced by a stub that
answers from the current scenario's synthetic world; no real tool runs, no
model loads, no socket opens.
"""
from __future__ import annotations

import boot  # noqa: F401  (WISP_HOME first)

import copy
import datetime as _dt
import json
import socket
import time as _time

# --- network guard: no TCP/UDP connection may leave this process -------------
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guard(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        raise ConnectionRefusedError(f"lora-dataset harness: network disabled ({address!r})")
    return _real_connect(self, address)


def _guard_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        raise ConnectionRefusedError(f"lora-dataset harness: network disabled ({address!r})")
    return _real_connect_ex(self, address)


socket.socket.connect = _guard
socket.socket.connect_ex = _guard_ex

# --- frozen, per-scenario clock ----------------------------------------------
_real_time = _time.time
_real_localtime = _time.localtime
_real_strftime = _time.strftime
_real_mono = _time.monotonic
_CLOCK = {"base": _real_time(), "mono": _real_mono()}


def set_clock(when: _dt.datetime) -> None:
    """Freeze 'now' at `when` (naive local time); it then ticks with real elapsed time."""
    _CLOCK["base"] = _time.mktime(when.timetuple())
    _CLOCK["mono"] = _real_mono()


def fake_time() -> float:
    return _CLOCK["base"] + (_real_mono() - _CLOCK["mono"])


def _fake_localtime(secs=None):
    return _real_localtime(fake_time() if secs is None else secs)


def _fake_strftime(fmt, t=None):
    return _real_strftime(fmt, _fake_localtime() if t is None else t)


_time.time = fake_time
_time.time_ns = lambda: int(fake_time() * 1e9)
_time.localtime = _fake_localtime
_time.strftime = _fake_strftime
_time.ctime = lambda secs=None: _time.asctime(_fake_localtime(secs))

_RealDT, _RealDate = _dt.datetime, _dt.date


class FrozenDateTime(_RealDT):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(fake_time(), tz)

    @classmethod
    def today(cls):
        return cls.fromtimestamp(fake_time())

    @classmethod
    def utcnow(cls):
        return cls.fromtimestamp(fake_time(), _dt.timezone.utc).replace(tzinfo=None)


class FrozenDate(_RealDate):
    @classmethod
    def today(cls):
        d = _RealDT.fromtimestamp(fake_time())
        return cls(d.year, d.month, d.day)


_dt.datetime = FrozenDateTime
_dt.date = FrozenDate

# --- Wisp imports (after the patches so `from datetime import datetime` binds the frozen class)
from fastapi.testclient import TestClient  # noqa: E402

import service.main as main  # noqa: E402
import service.tools  # noqa: E402,F401
from service.tools.registry import REGISTRY  # noqa: E402
import service.memory.identity as identity  # noqa: E402

USER_NAME = "Sam Rivera"
USER_EMAILS = ["sam.rivera@linden.edu", "samriv.dev@example.com"]
identity._full_name = USER_NAME
from service.tools.email_tools import cache_identity  # noqa: E402

cache_identity(USER_EMAILS)

PROFILE = {
    "name": "Sam",
    "about": ("CS sophomore at Linden University in Portland, Oregon. I work part-time "
              "at the campus library help desk and live off campus with my roommate Maya."),
    "goals": ["Get an A in Operating Systems", "Land a summer software internship",
              "Run a half marathon in the spring"],
    "struggles": ["Starting big assignments early"],
    "people": [{"name": "Maya Chen", "relation": "roommate"},
               {"name": "Carmen Rivera", "relation": "sister"},
               {"name": "Theo Park", "relation": "best friend"}],
}


def set_profile(profile: dict | None) -> None:
    from service.memory import profile as prof
    prof.clear()
    if profile:
        prof.update(dict(profile))


# --- stubs -------------------------------------------------------------------
class ToolError(Exception):
    """Raised by a world handler to make the stub fail like the real tool would."""


CURRENT = {"world": None, "calls": [], "approve": [], "cards": []}

# Production registers the bundled skills' tools (use_skill, mac_*) at startup
# (main.lifespan -> skills.load()); TestClient skips the lifespan, so load them
# here, before the stubs, or the offered tool lists differ from production's.
from service import skills as _skills  # noqa: E402

try:
    _skills.load()
except Exception:  # noqa: BLE001  (production tolerates a failed load the same way)
    pass


def _make_stub(name: str, original):
    async def stub(**args):
        world = CURRENT["world"]
        CURRENT["calls"].append((name, copy.deepcopy(args)))
        if world is None:
            raise RuntimeError(f"no synthetic world for {name}")
        return world.handle(name, args)
    stub.__module__ = getattr(original, "__module__", "service.tools")
    stub.__name__ = getattr(original, "__name__", name)
    stub.__qualname__ = stub.__name__
    return stub


for _name, _tool in REGISTRY.items():
    _tool.func = _make_stub(_name, _tool.func)

# manage_contacts is an authoritative builtin (router.model_led.BUILTIN_COMPLETION_MODULES):
# the loop accepts its BuiltinCompletion receipt only when the registered function IS the
# module's own attribute. Point the module at the stub (as toolbench does).
import service.tools.contacts_tools as _contacts_tools  # noqa: E402

_contacts_tools.manage_contacts = REGISTRY["manage_contacts"].func


def is_write(name: str, args: dict | None = None) -> bool:
    """A tool production treats as an effect on a model-led turn (model_led.effectful_call)."""
    from service.router.model_led import effectful_call
    tool = REGISTRY.get(name)
    return bool(tool and effectful_call(tool, args or {}))


# --- the loop's own store lookups, pointed at the current world ------------------
# Before an update_event card the loop calls assistant_tools.prepare_event_move, and
# before a bulk reminder delete reminders_matching / past_due_matching, against the
# real (empty) assistant store. Without these every move is refused and every
# delete card is empty (toolbench world.install_hooks does the same).
import service.tools.assistant_tools as _assistant_tools  # noqa: E402


def _strip_refs(rows):
    return [{k: v for k, v in r.items() if k != "_ref"} for r in rows]


def _hook_prepare_event_move(args: dict) -> dict:
    return CURRENT["world"].prepare_event_move(args)


def _hook_reminders_matching(scope="all", query="", *, now=None, include_stale=False):
    return _strip_refs(CURRENT["world"].reminder_rows(scope, query, include_stale=include_stale))


def _hook_past_due_matching(query="", days=365):
    return _strip_refs(CURRENT["world"].past_due_rows(query, days))


_assistant_tools.prepare_event_move = _hook_prepare_event_move
_assistant_tools.reminders_matching = _hook_reminders_matching
_assistant_tools.past_due_matching = _hook_past_due_matching

# --- the native-app channel the loop uses outside the registry -------------------
# reply_to_email's pre-approval step (action_tools.prepare_reply_args) asks the Wisp
# app to prepare (and discard) the reply. No app is connected here; answer it from
# the world and refuse every other native request (nothing real may run).
import service.tools.action_tools as _action_tools  # noqa: E402


async def _fake_app_request(event_type: str, payload: dict, timeout: float = 0.0) -> dict:
    world = CURRENT["world"]
    if event_type == "prepare_email_reply" and world is not None:
        return world.prepare_email_reply(payload)
    return {"ok": False, "status": "not_attempted", "dispatch_started": False,
            "error": "lora-dataset harness: no Wisp app is connected; nothing was done"}


_action_tools.app_request = _fake_app_request


# --- scripted approval cards -------------------------------------------------------
class ScriptedApprover:
    """Stand-in for service.agent.InteractiveApprover: raises the same `confirm`
    event (so the stream shows the card) and answers it at once from the
    scenario's `approve` list (one entry per card, in order; missing = Allow)
    instead of waiting up to 300 s for POST /agent/approve."""

    def __init__(self, emit, *a, **k):
        self._emit = emit
        self.request_id = k.get("request_id")

    async def confirm(self, action: dict) -> bool:
        from service.safety import grants
        CURRENT["cards"].append(str(action.get("tool", "")))
        await self._emit({
            "type": "confirm",
            "grantable": grants.is_grantable(action.get("tool", ""), action.get("args") or {}),
            "scope_hint": grants.scope_for(action.get("tool", ""), action.get("args") or {}),
            **({"request_id": self.request_id} if self.request_id else {}),
            **action,
        })
        queue = CURRENT["approve"]
        return bool(queue.pop(0)) if queue else True

    def resolve(self, *a, **k) -> bool:
        return False

    def has_pending(self, *_a) -> bool:
        return False


main.InteractiveApprover = ScriptedApprover


# --- scripted ideal model ------------------------------------------------------
class Call:
    def __init__(self, _tool: str, /, **args):
        self.name, self.args = _tool, args

    def __repr__(self):
        return f"Call({self.name}, {self.args})"


class Final:
    def __init__(self, text: str):
        self.text = text


class Bad:
    """A deliberately wrong step (kept in history, never a training target)."""
    def __init__(self, *calls: Call):
        self.calls = list(calls)


class ScriptError(Exception):
    pass


def _families(request) -> dict[str, str]:
    """tool name -> capability family, parsed from the model-led system block."""
    out: dict[str, str] = {}
    for message in request["messages"]:
        if message.get("role") != "system":
            continue
        for line in str(message.get("content") or "").splitlines():
            if ": " in line and " Tools: " in line:
                family = line.split(":", 1)[0].strip()
                for tool in line.split(" Tools: ", 1)[1].split(","):
                    out[tool.strip()] = family
    return out


class ScriptedPolicy:
    managed = True
    endpoint_name = "omlx"

    def __init__(self, steps):
        self.steps = list(steps)
        self.idx = 0
        self.requests: list[dict] = []
        self.responses: list[dict] = []   # {"message":..., "kind": call|load|final|bad}
        self.base_url = main.role_target("agent").endpoint.base_url
        self.context_window = 32000
        self.target = self
        self._n = 0

    async def ensure_only(self, *a, **k):
        pass

    async def ensure_engine(self, *a, **k):
        pass

    async def loaded_models(self):
        return []

    def _tc(self, call: Call) -> dict:
        self._n += 1
        return {"id": f"call_{self._n}", "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.args, ensure_ascii=False)}}

    def decide(self, request) -> tuple[dict, str]:
        if self.idx >= len(self.steps):
            raise ScriptError(f"loop asked for step {len(self.requests)} past the script")
        step = self.steps[self.idx]
        if isinstance(step, Final):
            self.idx += 1
            return {"role": "assistant", "content": step.text, "tool_calls": None}, "final"
        bad = isinstance(step, Bad)
        calls = step.calls if bad else step
        offered = {}
        for schema in request.get("tools") or []:
            fn = schema["function"]
            offered[fn["name"]] = "description" in fn
        full = [c for c in calls if offered.get(c.name) is True]
        stubs = [c for c in calls if offered.get(c.name) is False]
        missing = [c for c in calls if c.name not in offered]
        if missing:
            families = _families(request)
            fams = sorted({families.get(c.name, "") for c in missing} - {""})
            if not fams or "get_tool_schemas" not in offered:
                raise ScriptError(f"tools not reachable this turn: {[c.name for c in missing]}")
            load = Call("get_tool_schemas", families=fams[:3],
                        tools=sorted({c.name for c in missing}))
            return {"role": "assistant", "content": "", "tool_calls": [self._tc(load)]}, "load"
        if stubs:
            return {"role": "assistant", "content": "",
                    "tool_calls": [self._tc(c) for c in stubs]}, "load"
        assert len(full) == len(calls)
        self.idx += 1
        return ({"role": "assistant", "content": "", "tool_calls": [self._tc(c) for c in calls]},
                "bad" if bad else "call")

    async def stream_events(self, model, messages, **kwargs):
        request = copy.deepcopy({"model": model, "messages": messages, **kwargs})
        self.requests.append(request)
        message, kind = self.decide(request)
        self.responses.append({"message": copy.deepcopy(message), "kind": kind})
        yield {"kind": "final", "message": message}


async def _noop(*a, **k):
    return None


main.ensure_omlx = _noop
_client_holder: dict = {}
main.OMLXClient = lambda *a, **k: _client_holder["c"]
main.TurnInferenceClient = lambda c, *a, **k: c

_tc = TestClient(main.app)


def run_scenario(prompt: str, steps, world, when: _dt.datetime, profile=PROFILE, approve=None):
    """Run one Ask turn through /agent. Returns (policy, events, final_text).

    `approve` answers the turn's approval cards in order (a list of bools, or one
    bool for every card; missing = Allow). Afterwards CURRENT["cards"] holds the
    tool name of every card the host raised and CURRENT["calls"] every tool run."""
    set_clock(when)
    set_profile(profile)
    policy = ScriptedPolicy(steps)
    _client_holder["c"] = policy
    main.client = policy
    CURRENT["world"], CURRENT["calls"], CURRENT["cards"] = world, [], []
    if isinstance(approve, bool):
        approve = [approve] * 64
    CURRENT["approve"] = list(approve or [])
    response = _tc.post("/agent", json={"prompt": prompt, "debug": False})
    events = []
    for line in response.text.splitlines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:]))
            except ValueError:
                pass
    sid = next((e.get("id") for e in events if e.get("type") == "session"), None)
    if sid:
        _tc.delete(f"/sessions/{sid}")
    CURRENT["world"] = None   # CURRENT["calls"] is kept for the caller's check
    final = next((e.get("text") for e in reversed(events) if e.get("type") == "text"), None)
    return policy, events, final
