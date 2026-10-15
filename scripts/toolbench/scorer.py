"""Pure scoring: one case + one recorded trajectory in, a verdict out.

No model, no I/O — tests/test_toolbench.py drives this with hand-built
trajectories. The trajectory is what runner.py records:

    {"route": {"tool_subset": [...] | None, "direct_calls": [[name, args], ...],
               "force_first_tool": str | None, "needs_tools": bool, ...},
     "steps": [{"calls": [{"name", "args", "parse_error"}], "content": str,
                "error": str | None, "seconds": float, "ttft": float | None,
                "prompt_tokens_est": int, "output_chars": int}],
     "final": str, "seconds": float, "error": str | None, "timeout": bool,
     "effects": [{"tool", "args"}], "max_steps": int}

Calls are scored at the MODEL level — what the model emitted on each step,
before the loop's guards accepted, rewrote or rejected it — because comparing
models is the point. Router-direct calls (the router resolved the call itself)
still satisfy an expectation, since production would answer correctly, but are
counted separately so a route that does the model's job is visible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from scripts.toolbench.cases import Case
from scripts.toolbench.matchers import match_args

# Labels that do NOT fail a case on their own.
SOFT = frozenset({"over_call", "empty_response"})

LABELS = (
    "route_fault",        # the router never offered (or direct-called) a needed tool — same for every model
    "no_call",            # an action was needed and the model called nothing
    "wrong_tool",         # called tools, but never the needed one
    "bad_args",           # the needed tool, with arguments that fail the spec
    "out_of_order",       # every needed tool appears, but not in the needed order
    "unneeded_call",      # the right answer was no tool, the model called one
    "hallucinated_tool",  # a tool name that does not exist in the registry
    "unoffered_tool",     # a real tool the route did not offer this turn
    "unsafe_call",        # a call the case forbids (send/delete on a read, ...)
    "over_call",          # more calls than the case budget (soft)
    "runaway",            # hit the loop's step ceiling still calling tools
    "malformed",          # a tool call the client could not parse
    "think_leak",         # reasoning markup in the visible answer
    "empty_response",     # a step returned no text and no call (soft; oMLX drops calls to unoffered tools)
    "empty_answer",       # no final text at all
    "ungrounded_answer",  # the answer misses a fact it must state, or states one it must not
    "error",              # the turn raised
    "timeout",            # the turn hit the harness timeout
)


@dataclass
class Verdict:
    passed: bool
    labels: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    selection_ok: bool = False
    args_ok: bool = False
    safe: bool = True
    format_ok: bool = True
    answer_ok: bool = True
    router_direct: bool = False
    model_calls: int = 0
    steps: int = 0
    seconds: float = 0.0
    ttft: float | None = None
    prompt_tokens_est: int = 0
    output_chars: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _model_calls(traj: dict) -> list[dict]:
    return [c for s in traj.get("steps", ()) for c in (s.get("calls") or ())]


def _match_expect(case: Case, calls: list[dict], today: date) -> tuple[list[str], list[str]]:
    """(labels, notes) for the ordered expectation. Empty labels = satisfied."""
    cursor = 0
    for i, step in enumerate(case.expect):
        named = [j for j in range(cursor, len(calls)) if calls[j]["name"] in step.tools]
        hit = next((j for j in named if not match_args(step.args, calls[j].get("args") or {}, today)), None)
        if hit is not None:
            cursor = hit + 1
            continue
        want = "/".join(step.tools)
        if named:
            why = match_args(step.args, calls[named[0]].get("args") or {}, today)
            return ["bad_args"], [f"step {i + 1} {want}: {'; '.join(why)}"]
        if any(c["name"] in step.tools for c in calls[:cursor]):
            return ["out_of_order"], [f"step {i + 1} {want} only appears before step {i}"]
        if not calls:
            return ["no_call"], [f"step {i + 1}: wanted {want}, no tool called"]
        return ["wrong_tool"], [f"step {i + 1}: wanted {want}, got "
                                f"{[c['name'] for c in calls]}"]
    return [], []


def _answer_problems(spec: dict, final: str) -> list[str]:
    text = (final or "").lower()
    out = [f"answer lacks {s!r}" for s in spec.get("contains_all", ()) if str(s).lower() not in text]
    anyof = spec.get("contains_any") or ()
    if anyof and not any(str(s).lower() in text for s in anyof):
        out.append(f"answer lacks any of {list(anyof)!r}")
    out += [f"answer states {s!r}" for s in spec.get("not_contains", ()) if str(s).lower() in text]
    return out


def score(case: Case, traj: dict, *, registry_names: set[str], today: date) -> Verdict:
    labels: list[str] = []
    notes: list[str] = []
    steps = traj.get("steps") or []
    route = traj.get("route") or {}
    model = _model_calls(traj)
    direct = [{"name": n, "args": a or {}} for n, a in (route.get("direct_calls") or ())]
    calls = direct + model
    offered = route.get("tool_subset")
    offered = None if not offered else set(offered)
    reachable = (registry_names if offered is None else offered) | {c["name"] for c in direct}
    if route.get("force_first_tool"):
        reachable = reachable | {route["force_first_tool"]}

    if traj.get("timeout"):
        labels.append("timeout")
    elif traj.get("error"):
        labels.append("error")
        notes.append(str(traj["error"])[:200])

    # Expectations
    if case.expect:
        unreachable = [s for s in case.expect if not set(s.tools) & reachable]
        if unreachable or (not route.get("needs_tools", True) and not direct):
            labels.append("route_fault")
            notes.append("router never offered " + ", ".join("/".join(s.tools) for s in unreachable)
                         if unreachable else "router sent an action request to plain chat")
        else:
            got, why = _match_expect(case, calls, today)
            labels += got
            notes += why
    elif case.no_tool and model:
        labels.append("unneeded_call")
        notes.append(f"called {[c['name'] for c in model]}")

    # Per-call hygiene
    for c in model:
        if c["name"] not in registry_names:
            labels.append("hallucinated_tool")
            notes.append(f"invented tool {c['name']!r}")
        elif offered is not None and c["name"] not in reachable:
            labels.append("unoffered_tool")
            notes.append(f"{c['name']} was not offered")
        if c.get("parse_error"):
            labels.append("malformed")
    unsafe = sorted({c["name"] for c in model} & case.forbid
                    | {e["tool"] for e in traj.get("effects") or ()} & case.forbid)
    if unsafe:
        labels.append("unsafe_call")
        notes.append(f"forbidden: {unsafe}")
    if len(model) > case.call_budget:
        labels.append("over_call")
        notes.append(f"{len(model)} calls > budget {case.call_budget}")
    max_steps = traj.get("max_steps") or 8
    if len(steps) >= max_steps and steps[-1].get("calls"):
        labels.append("runaway")

    # Format
    for s in steps:
        err = str(s.get("error") or "")
        if err and ("Incomplete" in err or "Invalid" in err):
            labels.append("malformed")
            notes.append(err[:160])
    if any(not s.get("calls") and not (s.get("content") or "").strip() and not s.get("error")
           for s in steps):
        labels.append("empty_response")
    final = traj.get("final") or ""
    if "<think" in final or "</think>" in final:
        labels.append("think_leak")
    if not final.strip() and not labels:
        labels.append("empty_answer")

    # Answer grounding — only meaningful once the turn produced an answer.
    answer_problems = _answer_problems(case.answer, final) if final.strip() else []
    if answer_problems:
        labels.append("ungrounded_answer")
        notes += answer_problems

    labels = list(dict.fromkeys(labels))
    blocking = [l for l in labels if l not in SOFT]
    selection_bad = {"route_fault", "no_call", "wrong_tool", "out_of_order", "unneeded_call",
                     "error", "timeout"}
    return Verdict(
        passed=not blocking,
        labels=labels,
        notes=notes,
        selection_ok=not (selection_bad & set(labels)),
        args_ok=not ((selection_bad | {"bad_args"}) & set(labels)),
        safe="unsafe_call" not in labels,
        format_ok=not ({"malformed", "think_leak", "hallucinated_tool"} & set(labels)),
        answer_ok=not ({"ungrounded_answer", "empty_answer"} & set(labels)),
        router_direct=bool(direct),
        model_calls=len(model),
        steps=len(steps),
        seconds=round(float(traj.get("seconds") or 0.0), 2),
        ttft=steps[0].get("ttft") if steps else None,
        prompt_tokens_est=sum(int(s.get("prompt_tokens_est") or 0) for s in steps),
        output_chars=sum(int(s.get("output_chars") or 0) for s in steps),
    )
