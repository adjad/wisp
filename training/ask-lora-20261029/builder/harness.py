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


CURRENT = {"world": None, "calls": []}


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


def run_scenario(prompt: str, steps, world, when: _dt.datetime, profile=PROFILE):
    """Run one Ask turn through /agent. Returns (policy, events, final_text)."""
    set_clock(when)
    set_profile(profile)
    policy = ScriptedPolicy(steps)
    _client_holder["c"] = policy
    main.client = policy
    CURRENT["world"], CURRENT["calls"] = world, []
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
