"""Grammar-constrained tool planning for the agent loop (WISP_PLANNER).

The free-form loop asks the model to write a tool call in its chat template's
format and hopes it does. Measured on 2026-10-13 against oMLX: `tool_choice`
is only consulted for "none" (oMLX server.py `_resolve_template_tools`), so
"required" and a named choice are no-ops; and oMLX's Ling output parser
silently drops a call to any tool absent from the request, so Wisp receives an
empty message. Ling also invents names (`memory`, `search_emails`, `think`).

A planning step instead sends oMLX a JSON schema (`response_format`), which it
enforces at the logit level with its bundled xgrammar backend: the `tool`
field is an enum of the names offered this step, so an unoffered or invented
name cannot be produced, and a step that must call a tool really must
(`minItems: 1`). The plan is converted into an ordinary assistant
`tool_calls` message, so every call still goes through run_agent's existing
policy, confirmation, receipt and repeat guards unchanged. Nothing here grants
authority or executes anything.

A plan with no calls ends tool use for the turn: the loop runs one
unconstrained narration step with a short instruction for that status (for
example "say plainly it was not found"), with no tools offered.

`thought` comes first on purpose. The grammar applies from the first output
token, so a model that must reason before choosing (LFM2.5 always thinks) has
nowhere to do it otherwise: without the field LFM2.5-24B returned empty plans
on the pilot prompts; with it the same prompts passed with fewer calls.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field

STATUSES = ("call_tools", "answer", "not_found", "cannot_do", "clarify")
MAX_CALLS_PER_PLAN = 4
# A plan is a short thought plus at most four calls. LFM2.5 pads JSON with
# whitespace runs between fields; 1,500 leaves room for that.
PLAN_MAX_TOKENS = 1500
THOUGHT_MAX_CHARS = 600

# Per-turn budget of MISSES per read tool: calls that came back empty, with a
# parenthesized notice, or failed. After two, that tool is not run again this
# turn and the model must answer or report not_found. Measured 2026-10-13
# (LFM2.5-24B, production loop, 59 cases): 93 same-tool repeat calls against
# 23 identical ones, e.g. search_notes with "gate code", "gate", "code",
# "home", "security", "apartment"; lookup_contact rephrases names the same
# way. Counting misses rather than calls leaves distinct requested targets
# alone ("notes for alpha, beta and gamma", "read these three files") as long
# as they find something (independent review of #211).
READ_MISS_BUDGET = 2


# Tool-call syntax in a tool-free narration step: LFM2.5's pythonic markers,
# the GLM/Ling and Hermes <tool_call> tag.
TOOL_CALL_MARKUP = re.compile(r"<\|tool_call_start\|>|<tool_call>")
_TOOL_CALL_SPAN = re.compile(
    r"<\|tool_call_start\|>.*?(?:<\|tool_call_end\|>|$)|<tool_call>.*?(?:</tool_call>|$)", re.S)


def strip_tool_call_markup(text: str) -> str:
    """The prose around any tool-call spans (an unterminated span runs to the end)."""
    return _TOOL_CALL_SPAN.sub("", text or "").strip()


def planner_enabled(value: str | None) -> bool:
    """WISP_PLANNER=1 turns planning on; unset or 0 keeps the free-form loop.
    Invalid values fail closed rather than silently picking either path."""
    if value is None:
        return False
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError("WISP_PLANNER must be true or false")


def enabled_from_env() -> bool:
    return planner_enabled(os.environ.get("WISP_PLANNER"))


PLAN_RULES = (
    "PLANNING STEP. Reply with ONLY the JSON plan; it is executed by Wisp, not shown to the user.\n"
    "IMPORTANT rules:\n"
    "- First write `thought`: one or two sentences — what the user wants, and which offered "
    "tool(s) answer it.\n"
    "- Choose the FEWEST calls that fully answer the request, usually one. Do only what was "
    "asked; do not check extra sources 'just in case'.\n"
    "- Calls in one plan run together. Only list a call whose arguments you already know; a "
    "call that needs an earlier result (a contact's number, a message id) goes in the NEXT plan. "
    "Never write a placeholder like <contact> as an argument.\n"
    "- Never repeat a call that already ran with the same arguments. Results already in the "
    "conversation are final.\n"
    "- `status`:\n"
    "    * call_tools — `calls` lists the tool calls to run now.\n"
    "    * answer — the request is answered by results already in the conversation, or needs "
    "no tool at all (chat, writing, explaining).\n"
    "    * not_found — you searched and the information is not there. Use it after a search "
    "came back without what was asked; do not keep rephrasing the same search.\n"
    "    * cannot_do — no offered tool can do what was asked. Read the tool descriptions first: "
    "the user's schedule, to-dos, reminders, mail, messages, notes, files and saved facts are "
    "reachable through them, so a question about those is never cannot_do.\n"
    "    * clarify — essential details are missing and no offered tool can find them.\n"
    "- Use each tool's exact parameter names from its schema."
)

_REQUIRED_NOTE = ("\n- This step MUST call at least one tool: status is call_tools and `calls` "
                  "is not empty.")

STATUS_NOTES = {
    "answer": "Write the answer to the user now from the tool results above. Do not call any tool.",
    "not_found": ("The information was not found in the sources you checked. Tell the user plainly "
                  "what you checked and that it was not there. Do not guess and do not call any tool."),
    "cannot_do": ("None of the available tools can do this. Say so plainly in one or two sentences, "
                  "and offer the closest thing you can do if there is one. Do not call any tool and "
                  "do not claim anything was done."),
    "clarify": ("Ask the user one short question for the detail you need. Do not call any tool and "
                "do not claim anything was done."),
}


def plan_schema(names: list[str], *, require_call: bool, searched: bool = True) -> dict:
    """The JSON schema oMLX enforces for one planning step.

    Arguments stay a free object here; run_agent validates them against the
    tool's registered schema before dispatch and hands the error back for the
    next plan. Per-tool argument schemas (anyOf keyed by tool) are a possible
    tightening once their grammar-compile cost is measured.
    """
    call = {"type": "object",
            "properties": {"tool": {"type": "string", "enum": sorted(set(names))},
                           "arguments": {"type": "object"}},
            "required": ["tool", "arguments"]}
    calls: dict = {"type": "array", "items": call, "maxItems": MAX_CALLS_PER_PLAN}
    statuses = list(STATUSES)
    if not searched:
        # not_found means "I looked and it is not there"; before any tool has
        # run this turn it can only be a claim about a search that never
        # happened. Measured 2026-10-13, Ling V3, real planner path: 5 of 59
        # cases planned not_found on the first step and told the user "I
        # checked your calendar and found nothing" with zero calls made.
        statuses.remove("not_found")
    if require_call:
        calls["minItems"] = 1
        statuses = ["call_tools"]
    if not names:
        calls["maxItems"] = 0
        statuses = [s for s in statuses if s != "call_tools"] or ["answer"]
    return {"type": "object",
            "properties": {"thought": {"type": "string", "maxLength": THOUGHT_MAX_CHARS},
                           "calls": calls,
                           "status": {"type": "string", "enum": statuses}},
            "required": ["thought", "calls", "status"]}


def plan_request_kwargs(names: list[str], *, require_call: bool, searched: bool = True) -> dict:
    return {"response_format": {"type": "json_schema", "json_schema": {
        "name": "wisp_plan", "strict": True,
        "schema": plan_schema(names, require_call=require_call, searched=searched)}}}


def plan_instruction(*, require_call: bool) -> dict:
    """The per-step planning instruction, appended after the conversation so
    the stable system prefix (and its prompt cache) is untouched."""
    return {"role": "system", "content": PLAN_RULES + (_REQUIRED_NOTE if require_call else "")}


@dataclass
class Plan:
    status: str
    thought: str = ""
    calls: list[tuple[str, dict]] = field(default_factory=list)
    error: str = ""

    @property
    def ends_tool_use(self) -> bool:
        return self.status != "call_tools" or not self.calls


def parse_plan(content: str | None, allowed: set[str]) -> Plan:
    """Decode a planning step's content. Defensive even though the grammar
    should make every field valid: a backend that degraded to prompt-injection
    (oMLX sends a Warning header) or a truncated generation must not execute
    anything. Unparseable output becomes an error plan with no calls."""
    raw = (content or "").strip()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return Plan(status="answer", error="unparseable plan")
    if not isinstance(data, dict):
        return Plan(status="answer", error="plan is not an object")
    status = data.get("status") if data.get("status") in STATUSES else "call_tools"
    calls: list[tuple[str, dict]] = []
    for item in data.get("calls") or []:
        if not isinstance(item, dict):
            continue
        name, args = item.get("tool"), item.get("arguments")
        if not isinstance(name, str) or name not in allowed:
            continue
        calls.append((name, args if isinstance(args, dict) else {}))
    calls = calls[:MAX_CALLS_PER_PLAN]
    # Listed calls win over the status. `calls` precedes `status` in the
    # schema, so the status is written after the model has already committed
    # to (or declined) concrete calls. Measured 2026-10-13, LFM2.5-24B with
    # status before calls: "whats on my todo list" and "i think i saved the
    # gate code somewhere in notes" each wrote a thought naming the right tool
    # (search_reminders, search_notes) and then status not_found, so nothing
    # was searched. Repeats stay bounded by the read budget and repeat guards.
    if calls:
        status = "call_tools"
    elif status == "call_tools":
        status = "answer"
    return Plan(status=status, thought=str(data.get("thought") or "")[:THOUGHT_MAX_CHARS],
                calls=calls)


def plan_message(plan: Plan) -> dict:
    """An assistant message in the shape oMLX returns for native tool calls,
    so the rest of run_agent cannot tell a planned call from a native one."""
    stamp = uuid.uuid4().hex[:8]
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": f"plan_{stamp}_{i}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}
                           for i, (name, args) in enumerate(plan.calls)]}


def call_key(name: str, arguments) -> tuple[str, str]:
    """Identity of a call for repeat detection: the name plus its arguments
    as canonical JSON, whether they arrive as a dict or a JSON string."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            return name, arguments
    return name, json.dumps(arguments or {}, sort_keys=True)


def budget_message(name: str, misses: int) -> str:
    return (f"({name} has already come back empty or failed {misses} times this turn; it was NOT "
            "run again. Answer from the results you have, or report that it was not found.)")
