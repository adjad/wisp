"""Multi-source scoring for the held-out `heldout_sources` set (cases/sources.yaml).

The user's aim for round 5 was "using all of my sources". A question that spans several
of the user's apps should read each of them; a question about one app should read that app
and stop. A case says so with an optional `sources:` block:

    sources:
      shape: topic            # topic | person | prep | owe | one_hit | nothing | conflict | single
      read: [mail, messages]  # the sources a good turn reads (a single contrast case lists one)
      use:                    # per source, any-of tokens the final answer states when it used that
        mail: ["terminal 2", "{flight_time}"]
        messages: ["got my email"]    # a token starting with "re:" is a regex (case-insensitive)
      contrast: false         # true: a single-source question; reading another source is over-reading

Three measures per trial, all computed from the stored trajectory (no model):

    reads_all   every source in `read` was read by a model call that ran
    uses_all    the answer states at least one token for every source in `use`
                (a case with no `use`, such as `nothing`, passes when the case's own answer checks do)
    over_read   a contrast case read a source outside `read` with that source's own tool

What counts as a read. COVERAGE is generous: a tool that returns a source's content reads it, so
`get_upcoming` reads the calendar and, unless `calendar_only` is set, the reminders it lists, and
`get_my_day_plan` reads the calendar and the reminders. OVER-READING is strict: only a source's own
tool counts (view_emails for mail, search_reminders for reminders), so the natural `get_upcoming`
for a calendar question is never charged with reading reminders. A call that failed its argument
check ("(error calling …)") or only loaded a schema read nothing. `get_recent_activity` ("what's
new", no query) reads nothing for coverage, since it cannot search a topic, and counts as reading
mail, messages, calendar, reminders and notes for over-reading.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict

SOURCES = ("mail", "messages", "notes", "calendar", "reminders", "memory", "weather", "web", "day_plan")
SHAPES = ("topic", "person", "prep", "owe", "one_hit", "nothing", "conflict", "single")

# A source's own tools: these both cover the source and charge an over-read.
OWN_TOOLS = {
    "mail": ("view_emails", "summarize_emails"),
    "messages": ("view_messages", "summarize_messages"),
    "notes": ("search_notes",),
    "calendar": ("get_upcoming", "get_past_events"),
    "reminders": ("search_reminders",),
    "memory": ("recall",),
    "weather": ("get_weather",),
    "web": ("web_search",),
}
# Combined tools: coverage only (they read several sources, so they never over-read).
COMBINED_COVERS = {
    "get_my_day_plan": ("calendar", "reminders", "day_plan"),
    "daily_brief": ("calendar", "reminders"),
}
# Broad read with no query: charged as an over-read, never counted as coverage.
BROAD_OVERREAD = {"get_recent_activity": ("mail", "messages", "calendar", "reminders", "notes")}

_OWN_BY_TOOL = {t: s for s, tools in OWN_TOOLS.items() for t in tools}
_ERROR_CALL = re.compile(r"\(error calling ([A-Za-z_][\w.]*)\(")


def _fold(text: str) -> str:
    return (text or "").replace("’", "'").replace("‘", "'").lower()


def check_spec(spec, where: str) -> dict:
    """Validated copy of a case's `sources:` block (raises ValueError)."""
    if not isinstance(spec, dict):
        raise ValueError(f"{where}: sources must be a mapping")
    unknown = set(spec) - {"shape", "read", "use", "contrast"}
    if unknown:
        raise ValueError(f"{where}: unknown sources keys {sorted(unknown)}")
    if spec.get("shape") not in SHAPES:
        raise ValueError(f"{where}: sources.shape must be one of {SHAPES}")
    read = spec.get("read")
    if not isinstance(read, list) or not read or set(map(str, read)) - set(SOURCES):
        raise ValueError(f"{where}: sources.read is a non-empty list of {SOURCES}")
    use = spec.get("use") or {}
    if not isinstance(use, dict) or set(use) - set(read):
        raise ValueError(f"{where}: sources.use keys must be among sources.read")
    for src, toks in use.items():
        if not isinstance(toks, list) or not toks or not all(isinstance(t, str) and t.strip() for t in toks):
            raise ValueError(f"{where}: sources.use.{src} is a non-empty list of strings")
        for t in toks:
            if t.startswith("re:"):
                try:
                    re.compile(t[3:])
                except re.error as e:
                    raise ValueError(f"{where}: sources.use.{src} pattern {t!r}: {e}") from None
    contrast = spec.get("contrast", False)
    if not isinstance(contrast, bool):
        raise ValueError(f"{where}: sources.contrast must be true or false")
    if contrast != (spec["shape"] == "single"):
        raise ValueError(f"{where}: contrast is true exactly for shape single")
    if contrast and len(read) > 3:
        raise ValueError(f"{where}: a contrast case reads at most three sources")
    return {"shape": spec["shape"], "read": [str(r) for r in read],
            "use": {str(k): list(v) for k, v in use.items()}, "contrast": contrast}


def _ran(traj: dict) -> list[dict]:
    """The model calls that actually ran: not schema loads, not calls the registry rejected."""
    from scripts.toolbench.scorer import _split_calls
    model, _discovery = _split_calls(traj)
    # A call the registry rejected for its arguments left a "(error calling name(" line in its step.
    failed: dict[str, int] = defaultdict(int)
    for s in traj.get("steps") or ():
        for e in s.get("tool_errors") or ():
            m = _ERROR_CALL.search(e)
            if m:
                failed[m.group(1).rsplit(".", 1)[-1]] += 1
    out = []
    for c in model:
        name = str(c.get("name") or "").rsplit(".", 1)[-1]
        if failed[name] > 0:
            failed[name] -= 1
            continue
        out.append({**c, "name": name})
    return out


def sources_read(calls: list[dict]) -> set[str]:
    """Sources covered (generous: see the module docstring)."""
    got: set[str] = set()
    for c in calls:
        name, args = c["name"], c.get("args") or {}
        if name in _OWN_BY_TOOL:
            got.add(_OWN_BY_TOOL[name])
            if name == "get_upcoming" and not args.get("calendar_only"):
                got.add("reminders")
        got.update(COMBINED_COVERS.get(name, ()))
    return got


def sources_overread(calls: list[dict]) -> set[str]:
    """Sources a call charged as read for over-reading (strict: a source's own tool only)."""
    got: set[str] = set()
    for c in calls:
        name = c["name"]
        if name in _OWN_BY_TOOL:
            got.add(_OWN_BY_TOOL[name])
        got.update(BROAD_OVERREAD.get(name, ()))
    return got


def _states(token: str, final: str) -> bool:
    if token.startswith("re:"):
        return re.search(token[3:], final, re.I) is not None
    return _fold(token) in _fold(final)


def score_trial(spec: dict, traj: dict, *, answer_ok: bool = True) -> dict:
    """One trial against a (bound) `sources:` spec. `answer_ok` is the scorer's own answer verdict."""
    calls = _ran(traj)
    covered, charged = sources_read(calls), sources_overread(calls)
    want = set(spec["read"])
    final = traj.get("final") or ""
    used = {s: any(_states(t, final) for t in toks) for s, toks in (spec.get("use") or {}).items()}
    uses_all = all(used.values()) if used else bool(final.strip()) and answer_ok
    return {"reads_all": want <= covered, "missing": sorted(want - covered), "covered": sorted(covered),
            "uses_all": bool(uses_all) and answer_ok, "unused": sorted(s for s, ok in used.items() if not ok),
            "over_read": bool(spec.get("contrast")) and bool(charged - want),
            "extra": sorted(charged - want), "n_calls": len(calls)}


def summarize(trials: list[dict]) -> dict:
    """Per-model shares over `trials`: [{model, case, shape, contrast, **score_trial}]."""
    by_model: dict[str, list[dict]] = defaultdict(list)
    for t in trials:
        by_model[t["model"]].append(t)
    out = {}
    for model, ts in by_model.items():
        multi = [t for t in ts if not t["contrast"]]
        contrast = [t for t in ts if t["contrast"]]

        def share(rows, key):
            return sum(bool(r[key]) for r in rows) / len(rows) if rows else None
        both = [t for t in multi if t["reads_all"] and t["uses_all"]]
        shapes = {}
        for shape in sorted({t["shape"] for t in ts}):
            rs = [t for t in ts if t["shape"] == shape]
            shapes[shape] = {"trials": len(rs), "reads_all": share(rs, "reads_all"), "uses_all": share(rs, "uses_all"),
                             "over_read": share(rs, "over_read") if shape == "single" else None}
        out[model] = {
            "trials": len(ts), "multi_trials": len(multi), "contrast_trials": len(contrast),
            "reads_all": share(multi, "reads_all"), "uses_all": share(multi, "uses_all"),
            "reads_and_uses": len(both) / len(multi) if multi else None,
            "over_read": share(contrast, "over_read"),
            "contrast_reads_own": share(contrast, "reads_all"),
            "calls_mean": statistics.mean(t["n_calls"] for t in ts) if ts else 0.0,
            "by_shape": shapes}
    return out


def trials_from_rows(rows: list[dict], cases: dict, meta: dict | None = None) -> list[dict]:
    """Score every stored row whose case carries a `sources:` spec. `cases` maps id -> bound Case."""
    from scripts.toolbench import report as rep
    out = []
    for r in rows:
        c = cases.get(r["case"])
        spec = getattr(c, "sources", None)
        if not spec or r["v"].get("excluded"):
            continue
        s = score_trial(spec, rep.traj_from_row(r, meta), answer_ok=bool(r["v"].get("answer_ok", True)))
        out.append({"model": r["model"], "case": r["case"], "trial": r.get("trial"), "shape": spec["shape"],
                    "contrast": spec["contrast"], **s})
    return out


def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def render(summary: dict, short=lambda m: m) -> str:
    models = list(summary)
    if not models:
        return "(no multi-source trials)"
    w = max(18, *(len(short(m)) for m in models)) + 2
    lines = ["MULTI-SOURCE (held-out sources set)"]

    def row(label, cells):
        lines.append(f"{label:<34}" + "".join(f"{c:>{w}}" for c in cells))
    row("", [short(m) for m in models])
    row("trials (multi / contrast)", [f"{summary[m]['multi_trials']} / {summary[m]['contrast_trials']}" for m in models])
    row("reads every expected source", [_pct(summary[m]["reads_all"]) for m in models])
    row("answer uses those sources", [_pct(summary[m]["uses_all"]) for m in models])
    row("reads and uses", [_pct(summary[m]["reads_and_uses"]) for m in models])
    row("contrast: over-reads", [_pct(summary[m]["over_read"]) for m in models])
    row("model calls per trial", [f"{summary[m]['calls_mean']:.1f}" for m in models])
    shapes = sorted({s for m in models for s in summary[m]["by_shape"]})
    for shape in shapes:
        row(f"  {shape} reads / uses", [
            (f"{_pct(summary[m]['by_shape'][shape]['reads_all'])} / {_pct(summary[m]['by_shape'][shape]['uses_all'])}"
             if shape in summary[m]["by_shape"] else "-") for m in models])
    return "\n".join(lines)
