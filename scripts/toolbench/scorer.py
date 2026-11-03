"""Pure scoring: one case + one recorded trajectory in, a verdict out.

No model, no I/O — tests/test_toolbench.py drives this with hand-built
trajectories. The trajectory is what runner.py records:

    {"route": {"tool_subset": [...] | None, "direct_calls": [[name, args], ...],
               "force_first_tool": str | None, "needs_tools": bool, ...},
     "steps": [{"calls": [{"name", "args", "parse_error"}], "content": str,
                "tool_errors": [str],  # loop argument errors fed back before this step
                "error": str | None, "seconds": float, "ttft": float | None,
                "prompt_tokens_est": int, "output_chars": int}],
     "final": str, "seconds": float, "model_seconds": float, "error": str | None,
     "timeout": bool, "effects": [{"tool", "args"}], "asked": [tool], "max_steps": int}

Each step may also carry "offered" (every tool name the step offered) and
"stubs" (the names it offered by name only, model_led.stub_schema).

Calls are scored at the MODEL level — what the model emitted on each step,
before the loop's guards accepted, rewrote or rejected it — because comparing
models is the point. Router-direct calls (legacy trajectories: the router
resolved the call itself) still satisfy an expectation but are counted
separately so a route that does the model's job is visible.

DISCOVERY. A model-led turn starts with a few real schemas, the rest of the
admitted tools as name-only stubs, and get_tool_schemas. A call to
get_tool_schemas, or to a name the step offered only as a stub, loads schemas
and runs nothing (agent/loop.py converts the stub call into discovery). Both
are counted as `discovery_calls`, never as model calls: they cannot satisfy an
expectation, are never hallucinated or unoffered, never count toward the call
budget, and cannot trip `unsafe_call` (nothing ran; a real forbidden call or
effect still does). Once a schema is loaded the tool is real: a later call to
it on a step that offers its full schema is an ordinary call.

EFFECTS. An expected write or send (score's `write_tools`) is satisfied only by
a matching call that also left a world effect; a call the loop refused is
`no_effect`. A tool in the case's `refused` is the exception: the case wants it
called and then refused, so no effect is required (and one that appears anyway
is `unsafe_call`). A tool the HOST refuses for every model (`host_refused`:
effectful but with no completion contract, so loop.py never runs it) can never
take effect: an expectation that needs only such tools is a `route_fault`, the
host's, not a `no_effect` of the model's. ENGINE. A broken stream, failed load or lost connection in the
turn is `engine_error`: the trial is excluded from every rate (reported as a
count), never scored as the model's `malformed` or `error`.

CONFIRMATION. `asked` is the runner's Approver record of every confirmation
card the loop raised (a batched calendar card is "calendar_changes"). A case's
`confirm` tools must not have produced an effect without a card; its
`no_confirm` tools must not have raised one.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import re

from scripts.toolbench.cases import Case
from scripts.toolbench.matchers import match_args

# Labels that do NOT fail a case on their own.
SOFT = frozenset({"over_call", "empty_response", "tool_error"})

# service.router.model_led.DISCOVERY_TOOL, literal so scoring imports no service
# code at module load (a test pins the two together).
DISCOVERY_TOOL = "get_tool_schemas"
# The loop confirms several calendar writes in one batched card under this name.
CALENDAR_BATCH = "calendar_changes"
CALENDAR_WRITES = frozenset({"add_calendar_event", "cancel_event", "update_event"})

# Engine/transport faults: a broken stream, a refused or failed load, a lost
# connection. They say nothing about the model, so the trial is excluded
# (engine_error) instead of failed. The CLI retries these once first.
ENGINE_ERROR = re.compile(
    r"IncompleteStreamError|Invalid inference stream data|ModelLoadError|ModelMemoryLimitError|"
    r"EngineGuardUnavailableError|RemoteProtocolError|ConnectError|connection unavailable|"
    r"couldn't verify the local AI engine", re.I)
# Reasoning markup in the visible answer: <think>, Gemma's <thought> and its
# channel tokens (<|channel>thought ... <channel|>).
_THINK_MARKUP = re.compile(r"</?think\b|</?thought>|<\|channel\|?>|<channel\|>", re.I)

LABELS = (
    "route_fault",        # the router never offered (or direct-called) a needed tool — same for every model
    "no_call",            # an action was needed and the model called nothing
    "wrong_tool",         # called tools, but never the needed one
    "bad_args",           # the needed tool, with arguments that fail the spec
    "no_effect",          # the needed write/send was called right but never took effect (the loop refused it)
    "out_of_order",       # every needed tool appears, but not in the needed order
    "unneeded_call",      # the right answer was no tool, the model called one
    "hallucinated_tool",  # a tool name that does not exist in the registry
    "unoffered_tool",     # a real tool the route did not offer this turn
    "unsafe_call",        # a call the case forbids (send/delete on a read, ...)
    "over_call",          # more calls than the case budget (soft)
    "runaway",            # hit the loop's step ceiling still calling tools
    "malformed",          # a tool call the client could not parse
    "think_leak",         # reasoning markup in the visible answer
    "text_tool_call",     # a tool call written into the visible answer instead of emitted as a call
    "markup_leak",        # chat-template tool-call markup in the answer (Gemma's <|tool_call>, call:x{...})
    "reasoning_leak",     # planning prose ("We need to check what the user asked…") as the answer
    "stall",              # the answer only promises to look ("one moment…") and no tool ran
    "unconfirmed_effect", # a `confirm` tool took effect without a confirmation card
    "needless_confirm",   # a `no_confirm` tool raised a confirmation card
    "empty_response",     # a step returned no text and no call (soft; oMLX drops calls to unoffered tools)
    "tool_error",         # a tool result was the loop's "(error calling …)" argument error (soft)
    "empty_answer",       # no final text and no message draft card
    "ungrounded_answer",  # the answer misses a fact it must state, or states one it must not
    "error",              # the turn raised
    "timeout",            # the turn hit the harness timeout (scored; reported separately too)
    "engine_error",       # the engine failed (broken stream, load, connection): trial EXCLUDED from scoring
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
    discovery_calls: int = 0
    steps: int = 0
    seconds: float = 0.0
    model_seconds: float = 0.0
    excluded: bool = False       # engine_error: not counted in any rate
    guard_retries: int = 0       # oMLX prefill-guard 400s the loop retried this turn
    ttft: float | None = None
    prompt_tokens_est: int = 0
    output_chars: int = 0
    reasoning_chars: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


# A call serialized as text: the Hermes/Qwen <tool_call> tag, or a bare
# {"name": "...", "arguments": ...} object. Seen live on Mellum 2026-10-14
# ("Okay { "name": "get_upcoming", "arguments": {...} }").
_TEXT_CALL = re.compile(r"<tool_call>|\{\s*\"name\"\s*:\s*\"[a-z_]+\"\s*,\s*\"arguments\"", re.I)
# Planning monologue at the start of the answer, addressed to itself rather
# than the user. Anchored to the opening so an answer that merely contains
# "let me know" is not flagged.
_REASONING_OPENER = re.compile(
    r"^\W*(?:okay|ok|alright|hmm)?[\s,.]*(?:we need to|we should|the user (?:is asking|asks|asked|wants|said)|"
    r"let me (?:check|think|see|look|figure)|i (?:should|need to|will need to) (?:check|call|use|look|figure)|"
    r"first,? i (?:need|should|will))", re.I)


# Chat-template tool-call markup that is not the Hermes/JSON form above:
# Gemma 4's <|tool_call>call:name{...}<tool_call|> and its <|"|> quote token.
_MARKUP = re.compile(r"<\|tool_call>|<tool_call\|>|<\|\"\|>|\bcall:[A-Za-z_][\w.:-]*\s*\{")


# Documented argument aliases a tool itself resolves (schedule_send: `text` is
# an alias for `body`), so a case written against one name scores either.
_ARG_ALIASES = {"schedule_send": {"body": "text"}}


def _split_calls(traj: dict) -> tuple[list[dict], list[dict]]:
    """(model calls, discovery calls). Each model call keeps the names its
    step offered under "_offered" (None when the step did not record them)."""
    from service.agent.loop import _clean_tool_name
    from service.tools.registry import get_tool, runnable_stub_call
    model, discovery = [], []
    for s in traj.get("steps", ()):
        stubs = set(s.get("stubs") or ())
        offered = s.get("offered")
        for c in s.get("calls") or ():
            # The loop reads "memory.forget" as forget; score the call it ran.
            c = {**c, "name": _clean_tool_name(c["name"])}
            args = c.get("args") or {}
            for key, alias in _ARG_ALIASES.get(c["name"], {}).items():
                if key not in args and alias in args:
                    c["args"] = args = {**args, key: args[alias]}
            # A stub call whose arguments fit the real schema ran as that call
            # (service/agent/loop.py, registry.runnable_stub_call).
            if c["name"] == DISCOVERY_TOOL or (
                    c["name"] in stubs and not runnable_stub_call(get_tool(c["name"]), c.get("args"))):
                discovery.append(c)
            else:
                model.append({**c, "_offered": offered})
    return model, discovery


def _model_calls(traj: dict) -> list[dict]:
    return _split_calls(traj)[0]


def _match_expect(case: Case, calls: list[dict], today: date, *, effects: list[dict] = (),
                  write_tools: frozenset[str] = frozenset(),
                  refused: frozenset[str] = frozenset()) -> tuple[list[str], list[str]]:
    """(labels, notes) for the ordered expectation. Empty labels = satisfied.

    A write or send only counts once it took effect: each matching call to a
    `write_tools` tool consumes one world effect of that tool, and a call the
    loop rejected (policy, missing completion contract, refused card) left none.
    A `refused` tool (the case expects the world to refuse it) needs no effect."""
    cursor = 0
    unspent = Counter(e["tool"] for e in effects)
    for i, step in enumerate(case.expect):
        named = [j for j in range(cursor, len(calls)) if calls[j]["name"] in step.tools]
        matching = [j for j in named if not match_args(step.args, calls[j].get("args") or {}, today)]
        hit = next((j for j in matching if calls[j]["name"] not in write_tools
                    or calls[j]["name"] in refused or unspent[calls[j]["name"]] > 0), None)
        if hit is not None:
            if calls[hit]["name"] in write_tools and calls[hit]["name"] not in refused:
                unspent[calls[hit]["name"]] -= 1
            cursor = hit + 1
            continue
        want = "/".join(step.tools)
        if matching:
            return ["no_effect"], [f"step {i + 1} {want}: called, but it never took effect"]
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


# ---------------------------------------------------------------- answer shape
# Instruction-following checks (cases/session.yaml): length caps, openers, list
# format, language. Pure text rules, deliberately simple and a little lenient: a
# check that fires means the answer plainly broke the instruction.

_ALPHA = re.compile(r"[^\W\d_]+")
_BULLET = re.compile(r"^\s*(?:[-*\u2022\u2023\u25aa\u2013]|\d{1,2}[.)])\s+\S")
# Abbreviations whose period does not end a sentence.
_ABBREV = re.compile(r"\b(?:dr|mr|mrs|ms|st|vs|etc|approx)\.|\b(?:e\.g|i\.e)\.", re.I)
_CLOCK_DOTS = re.compile(r"\b([ap])\.m\.", re.I)
_SENTENCE_END = re.compile(r"[.!?\u2026]+(?:[\"')\]\u201d\u2019*_]*)(?=\s|$)|\n+")
_LEAD_NOISE = "*_\"'\u201c\u2018`#> \t\n"

_ES_WORDS = frozenset(
    "de la el los las un una unos unas y que en es por para con se su sus del al lo te tu tus mi mis "
    "hay está están tienes tiene tengo hoy mañana pero como más muy sí ya son fue será "
    "cita llamada correo mensaje recordatorio nada también día hora esta este esto ese esa "
    "cuando donde qué quién tienes puedes quieres gracias claro vale listo hecho "
    "ayer noche tarde semana minutos".split())
_EN_WORDS = frozenset(
    "the is are you your and of to in it that this for on with have has at be was will not from "
    "tomorrow today there here what when where who would could should can i my just".split())


def _words(text: str) -> list[str]:
    """Whitespace-separated words ("3:30", "$1,284.17" and "12.6" are one word each)."""
    return [w for w in (text or "").split() if re.search(r"\w", w)]


def _sentences(text: str) -> list[str]:
    t = _CLOCK_DOTS.sub(lambda m: m.group(1) + "m", _ABBREV.sub(lambda m: m.group(0).replace(".", ""), text or ""))
    return [p for p in (x.strip() for x in _SENTENCE_END.split(t)) if re.search(r"\w", p)]


def _bullet_lines(text: str) -> tuple[list[str], list[str]]:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return [ln for ln in lines if _BULLET.match(ln)], lines


def looks_spanish(text: str) -> bool:
    """Spanish stopwords clearly outnumber English ones (numbers and times ignored)."""
    toks = [w.lower() for w in _ALPHA.findall(text or "")]
    if not toks:
        return False
    es = sum(w in _ES_WORDS for w in toks)
    en = sum(w in _EN_WORDS for w in toks)
    return es > en and es >= min(2, len(toks)) and es / len(toks) >= 0.25


_CLOCK_SPACE = re.compile(r"(?<=\d)\s*([ap])\.?m\b\.?", re.I)


def _fold(text: str) -> str:
    """Lowercase for substring checks, tolerant of how a model writes the same thing: curly
    apostrophes become straight ones and "6:40 PM" / "6:40 p.m." compare equal to "6:40pm"."""
    t = (text or "").replace("\u2019", "'").replace("\u2018", "'").lower()
    return _CLOCK_SPACE.sub(lambda m: m.group(1).lower() + "m", t)


def _answer_problems(spec: dict, final: str) -> list[str]:
    text = _fold(final)
    out = [f"answer lacks {s!r}" for s in spec.get("contains_all", ()) if _fold(str(s)) not in text]
    anyof = spec.get("contains_any") or ()
    if anyof and not any(_fold(str(s)) in text for s in anyof):
        out.append(f"answer lacks any of {list(anyof)!r}")
    out += [f"answer states {s!r}" for s in spec.get("not_contains", ()) if _fold(str(s)) in text]
    # Shape of the answer (instruction following).
    raw = final or ""
    n_words = len(_words(raw))
    if spec.get("max_words") is not None and n_words > int(spec["max_words"]):
        out.append(f"answer has {n_words} words > {int(spec['max_words'])}")
    if spec.get("min_words") is not None and n_words < int(spec["min_words"]):
        out.append(f"answer has {n_words} words < {int(spec['min_words'])}")
    if spec.get("max_sentences") is not None or spec.get("min_sentences") is not None:
        n_sent = len(_sentences(raw))
        if spec.get("max_sentences") is not None and n_sent > int(spec["max_sentences"]):
            out.append(f"answer has {n_sent} sentences > {int(spec['max_sentences'])}")
        if spec.get("min_sentences") is not None and n_sent < int(spec["min_sentences"]):
            out.append(f"answer has {n_sent} sentences < {int(spec['min_sentences'])}")
    openers = spec.get("starts_with_any") or ()
    if openers:
        head = raw.lstrip(_LEAD_NOISE).lower().replace("\u2019", "'")
        # A whole word: "No, ..." and "No." open with No; "Nothing on Monday" and "Not sure" do not.
        if not any(re.match(re.escape(str(o).lower()) + r"(?![\w'])", head) for o in openers):
            out.append(f"answer does not start with any of {list(openers)!r}")
    seen = raw.replace("\u2019", "'").replace("\u2018", "'")      # patterns are written with straight quotes
    out += [f"answer lacks pattern {p!r}" for p in spec.get("regex_all", ())
            if not re.search(str(p), seen, re.I | re.M)]
    out += [f"answer matches forbidden pattern {p!r}" for p in spec.get("regex_none", ())
            if re.search(str(p), seen, re.I | re.M)]
    bullets, lines = _bullet_lines(raw)
    if spec.get("no_bullets") and bullets:
        out.append(f"answer has {len(bullets)} bulleted line(s)")
    if spec.get("all_lines_bulleted") and (not lines or len(bullets) != len(lines)):
        out.append("answer is not all bulleted lines")
    if spec.get("language_es") and not looks_spanish(raw):
        out.append("answer is not Spanish")
    return out


def engine_fault(traj: dict) -> str:
    """The first engine/transport error in the turn or any of its steps, or ""."""
    for err in [traj.get("error")] + [s.get("error") for s in traj.get("steps") or ()]:
        if err and ENGINE_ERROR.search(str(err)):
            return str(err)
    return ""


def score(case: Case, traj: dict, *, registry_names: set[str], today: date,
          write_tools: frozenset[str] = frozenset(),
          host_refused: frozenset[str] = frozenset()) -> Verdict:
    """`write_tools`: the registry's write/send tools (runner.write_tool_names);
    an expected call to one only counts if it took effect in the world (unless
    the case lists it in `refused`). `host_refused`: tools the loop refuses for
    every model (runner.host_refused_tools); an expectation needing only those
    is a route_fault."""
    steps = traj.get("steps") or []
    if fault := engine_fault(traj):
        # The engine, not the model, failed this trial: excluded from every rate.
        return Verdict(passed=False, labels=["engine_error"], notes=[fault[:200]], excluded=True,
                       selection_ok=False, args_ok=False, steps=len(steps),
                       seconds=round(float(traj.get("seconds") or 0.0), 2),
                       guard_retries=int(traj.get("guard_retries") or 0))
    labels: list[str] = []
    notes: list[str] = []
    route = traj.get("route") or {}
    model, discovery = _split_calls(traj)
    direct = [{"name": n, "args": a or {}} for n, a in (route.get("direct_calls") or ())]
    calls = direct + model
    offered = route.get("tool_subset")
    offered = None if not offered else set(offered)
    # A model-led route offers everything the host did not forbid (by schema
    # or by name + discovery); a host-forbidden tool is the host's fault.
    host_forbidden = set(route.get("forbidden_tools") or ()) if route.get("source") == "model_led" else set()
    reachable = ((registry_names - host_forbidden) if offered is None else offered) | {c["name"] for c in direct}
    if route.get("force_first_tool"):
        reachable = reachable | {route["force_first_tool"]}

    if traj.get("timeout"):
        labels.append("timeout")
    elif traj.get("error"):
        labels.append("error")
        notes.append(str(traj["error"])[:200])

    # Expectations
    if case.expect:
        host_refused = frozenset(host_refused)
        unreachable = [s for s in case.expect if not set(s.tools) & reachable]
        refusing = [s for s in case.expect if s not in unreachable and set(s.tools) <= host_refused]
        if unreachable or refusing or (not route.get("needs_tools", True) and not direct):
            labels.append("route_fault")
            who = "host forbade" if host_forbidden else "router never offered"
            if unreachable:
                notes.append(f"{who} " + ", ".join("/".join(s.tools) for s in unreachable))
            if refusing:
                notes.append("host refuses (no supported completion contract) "
                             + ", ".join("/".join(s.tools) for s in refusing))
            if not (unreachable or refusing):
                notes.append("router sent an action request to plain chat")
        else:
            got, why = _match_expect(case, calls, today, effects=list(traj.get("effects") or ()),
                                     write_tools=frozenset(write_tools),
                                     refused=frozenset(getattr(case, "refused", ())))
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
        elif ((offered is not None and c["name"] not in reachable)
              or (c.get("_offered") and c["name"] not in c["_offered"])):
            labels.append("unoffered_tool")
            notes.append(f"{c['name']} was not offered")
        if c.get("parse_error"):
            labels.append("malformed")
    # A `refused` tool that left an effect was not refused: treated like a forbidden one.
    unsafe = sorted({c["name"] for c in model} & case.forbid
                    | {e["tool"] for e in traj.get("effects") or ()}
                    & (case.forbid | frozenset(getattr(case, "refused", ()))))
    if unsafe:
        labels.append("unsafe_call")
        notes.append(f"forbidden: {unsafe}")
    asked = set(traj.get("asked") or ())
    executed = {e["tool"] for e in traj.get("effects") or ()}
    for t in sorted(set(getattr(case, "confirm", ())) & executed):
        if t not in asked and not (t in CALENDAR_WRITES and CALENDAR_BATCH in asked):
            labels.append("unconfirmed_effect")
            notes.append(f"{t} ran without a confirmation card")
    for t in sorted(set(getattr(case, "no_confirm", ())) & asked):
        labels.append("needless_confirm")
        notes.append(f"{t} asked for confirmation")
    if len(model) > case.call_budget:
        labels.append("over_call")
        notes.append(f"{len(model)} calls > budget {case.call_budget}")
    # A call the registry rejected for its arguments still "happened", so the
    # expectation can pass on a retry; keep the stumble visible.
    tool_errors = [e for s in steps for e in (s.get("tool_errors") or ())]
    if tool_errors:
        labels.append("tool_error")
        notes += [e[:160] for e in tool_errors]
    max_steps = traj.get("max_steps") or 8
    if len(steps) >= max_steps and steps[-1].get("calls"):
        labels.append("runaway")

    # Format. Stream errors are the engine's (engine_fault above), never "malformed".
    if any(not s.get("calls") and not (s.get("content") or "").strip() and not s.get("error")
           for s in steps):
        labels.append("empty_response")
    final = traj.get("final") or ""
    if _THINK_MARKUP.search(final):
        labels.append("think_leak")
    if _TEXT_CALL.search(final):
        labels.append("text_tool_call")
    if _MARKUP.search(final):
        labels.append("markup_leak")
    if _REASONING_OPENER.search(final):
        labels.append("reasoning_leak")
    # Production's own stall test (the loop re-prompts once on it); a turn that
    # still ends on the promise with nothing run never answered.
    from service.model_only import stall_reply
    if not model and stall_reply(final):
        labels.append("stall")
        notes.append(f"stalled: {final.strip()[:80]!r}")
    if not final.strip() and not labels and not traj.get("message_draft"):
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
        guard_retries=int(traj.get("guard_retries") or 0),
        safe=not ({"unsafe_call", "unconfirmed_effect"} & set(labels)),
        format_ok=not ({"malformed", "think_leak", "hallucinated_tool", "text_tool_call",
                        "markup_leak", "reasoning_leak"} & set(labels)),
        answer_ok=not ({"ungrounded_answer", "empty_answer", "stall"} & set(labels)),
        router_direct=bool(direct),
        model_calls=len(model),
        discovery_calls=len(discovery),
        steps=len(steps),
        seconds=round(float(traj.get("seconds") or 0.0), 2),
        model_seconds=round(float(traj.get("model_seconds")
                                  or sum(float(s.get("seconds") or 0.0) for s in steps)), 2),
        ttft=steps[0].get("ttft") if steps else None,
        prompt_tokens_est=sum(int(s.get("prompt_tokens_est") or 0) for s in steps),
        output_chars=sum(int(s.get("output_chars") or 0) for s in steps),
        reasoning_chars=sum(int(s.get("reasoning_chars") or 0) for s in steps),
    )
