"""Tool registry. Each tool declares a safety `category` used by the policy engine."""
from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Callable

REGISTRY: dict[str, "Tool"] = {}

# Compatibility registrations for capabilities that have no executable local
# implementation.  Keeping the names lets old saved workflows and direct
# callers receive a precise explanation, while ``unavailable_reason`` makes
# them non-routable and prevents approval/dispatch.  This is the authoritative
# inventory: do not advertise API-key settings or providers Wisp does not have.
UNAVAILABLE_TOOL_REASONS: dict[str, str] = {
    "country_info": (
        "Wisp does not currently have a country-data lookup provider. "
        "No country lookup was performed; ask me to search the web instead."
    ),
    "find_local_events": (
        "Wisp does not currently have a local-events provider. "
        "No event lookup was performed; ask me to search the web instead."
    ),
    "get_lyrics": (
        "Wisp does not have a licensed lyrics source. No lyrics lookup was performed; "
        "use a licensed service such as Apple Music or Genius."
    ),
    "identify_song": (
        "Wisp cannot identify nearby music because its app has no ShazamKit audio bridge. "
        "No recording or song identification ran."
    ),
    "live_captions": (
        "Wisp cannot control macOS Live Captions because macOS exposes no supported "
        "automation API for it. Nothing was changed; use System Settings > Accessibility > "
        "Live Captions."
    ),
    "lookup_media_title": (
        "Wisp does not currently have a movie or TV metadata provider. "
        "No title lookup was performed; ask me to search the web instead."
    ),
    "set_hotkey": (
        "Wisp cannot register global hotkeys because its app has no Accessibility event-tap "
        "bridge. No shortcut was created."
    ),
    "set_keyboard_backlight": (
        "Wisp cannot change this Mac's keyboard backlight: no supported manual software "
        "control is available. Nothing was changed."
    ),
    "track_flight": (
        "Wisp does not currently have a live flight-status provider. "
        "No flight lookup was performed; ask me to search the web instead."
    ),
    "track_package": (
        "Wisp does not currently have a carrier-tracking provider. "
        "No package lookup was performed; check the carrier link or ask me to search your "
        "email for the shipping confirmation."
    ),
    "transcribe_audio": (
        "Wisp cannot transcribe audio because its app has no Speech-framework bridge. "
        "No file was opened or transcribed."
    ),
    "transit_info": (
        "Wisp does not currently have a live transit-arrivals provider. "
        "No departure lookup was performed; transit directions in Apple Maps are still "
        "available."
    ),
}

EVENT_UPDATE_UNAVAILABLE = (
    "Calendar event updates are unavailable in Wisp because it cannot yet "
    "preserve the existing event's duration, calendar, attendees, and other details. "
    "This update made no changes to any calendar event or reminder. "
    "Please edit the event in Calendar.")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict          # JSON schema for the arguments
    category: str             # safety category: shell, fs_read, fs_write, ...
    func: Callable[..., Any]  # called with **args, returns a string (sync or async)
    # Example user utterances this tool answers ("where did I put my taxes",
    # "find that pdf"). Used ONLY by router/semantic.py, which embeds them
    # alongside the name and description so retrieval matches how a person
    # actually asks rather than how the tool is named. Never serialized into
    # `schema()`, so they cost zero prompt tokens no matter how many are added
    # — the whole point is to improve selection without spending context.
    aliases: list[str] = field(default_factory=list)
    # Optional stable search document when model-facing wording is optimized.
    # Description edits otherwise alter BM25 corpus statistics and embedding
    # rankings for every tool. Never serialized into a model tool schema.
    retrieval_description: str | None = None
    # An unavailable implementation is stopped before approval/dispatch, even
    # if a router or model selects its retained compatibility registration.
    unavailable_reason: str = ""

    @property
    def effective_retrieval_description(self) -> str:
        return self.description if self.retrieval_description is None else self.retrieval_description

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class ToolOutcome:
    """Typed interpretation of a tool result used by the agent verifier.

    Tools may continue returning strings; this compatibility layer gives the
    loop a reliable distinction between success, denial/failure, and dry-run
    planning while individual tools migrate to richer native outcomes.
    """
    status: str  # succeeded | no_match | needs_input | denied | failed | planned
    text: str
    effect: str = ""
    facts: dict[str, Any] = field(default_factory=dict)


_TOOL_EFFECTS = {
    "send_message": "sent", "send_email": "sent", "reply_to_email": "sent",
    "forward_email": "sent",
    "schedule_send": "scheduled", "draft_message": "drafted",
    "draft_email": "drafted", "add_reminder": "created",
    "update_reminder": "updated", "update_event": "updated",
    "add_calendar_event": "created", "complete_reminder": "completed",
    "cancel_event": "cancelled", "cancel_scheduled_send": "cancelled",
    "clear_past_reminders": "deleted", "clear_reminders": "deleted",
    "toggle_setting": "changed", "write_file": "written", "move_path": "moved",
    "delete_path": "deleted", "trash_file": "deleted", "organize_files": "moved",
}


def classify_tool_outcome(tool_name: str, result: str, *, planned: bool = False,
                          denied: bool = False) -> ToolOutcome:
    text = str(result or "")
    effect = _TOOL_EFFECTS.get(tool_name, "read")
    if planned:
        return ToolOutcome("planned", text, effect)
    low = text.strip().lower()
    if tool_name == "organize_files" and low.startswith("would move "):
        return ToolOutcome("preview", text, effect)
    if tool_name == "organize_files" and low.startswith(("no files", "moved 0 ")):
        return ToolOutcome("no_match", text, effect)
    if denied or "user denied this action" in low or low.startswith("blocked by safety"):
        return ToolOutcome("denied", text, effect)
    if is_tool_error(text) or any(mark in low for mark in (
            "(not sent", "(not scheduled", "(couldn't", "(could not",
            "(no recipient", "(nothing to send", "(channel must", "was not sent")):
        return ToolOutcome("failed", text, effect)
    if tool_name == "update_event":
        if (text == EVENT_UPDATE_UNAVAILABLE
                or low.startswith("nothing upcoming or past matches") or "which one?" in low):
            return ToolOutcome("needs_input", text, effect)
        # The old cancel + recreate receipt is no longer an executable or
        # preservation-safe contract, nor evidence of native completion.
        return ToolOutcome("failed", text, effect)
    if tool_name == "get_upcoming":
        if low.startswith("wisp is still syncing"):
            return ToolOutcome("needs_input", text, effect)
        if low.startswith("wisp could not check"):
            return ToolOutcome("failed", text, effect)
        if re.search(r"\bnothing scheduled in (?:the )?next \d+ day\(s\)", low):
            return ToolOutcome("no_match", text, effect)
    if any(mark in low for mark in ("nothing active matches", "nothing found",
                                     "no matches", "no inbox data")):
        return ToolOutcome("no_match", text, effect)
    if any(mark in low for mark in ("which one?", "ask the user", "needs part of")):
        return ToolOutcome("needs_input", text, effect)
    if tool_name == "add_reminder" and not low.startswith("reminder set:"):
        # Bad ISO dates, past times, and arbitrary nonempty strings are not
        # evidence of a write. The creation tool emits this receipt after save.
        return ToolOutcome("failed", text, effect)
    receipts = {
        "send_message": "message sent to ", "send_email": "email sent to ",
        "reply_to_email": "reply sent", "forward_email": "forwarded to ",
        "schedule_send": "scheduled:", "draft_email": "draft opened in mail",
        "draft_message": "message draft prepared in wisp",
    }
    if tool_name in receipts and not low.startswith(receipts[tool_name]):
        return ToolOutcome("failed", text, effect)
    return ToolOutcome("succeeded", text, effect)


def register(name: str, description: str, parameters: dict, category: str,
             aliases: list[str] | None = None, *,
             retrieval_description: str | None = None, unavailable_reason: str = ""):
    def deco(func):
        reason = unavailable_reason or UNAVAILABLE_TOOL_REASONS.get(name, "")
        REGISTRY[name] = Tool(name, description, parameters, category, func,
                              list(aliases or []), retrieval_description, reason)
        return func
    return deco


def get_tool(name: str) -> Tool | None:
    return REGISTRY.get(name)


def is_tool_routable(name: str) -> bool:
    """Whether ``name`` may be offered or dispatched as a working capability."""
    tool = REGISTRY.get(name)
    return bool(tool and not tool.unavailable_reason)


def routable_tool_names() -> set[str]:
    return {name for name, tool in REGISTRY.items() if not tool.unavailable_reason}


def tool_schemas(names: list[str] | None = None) -> list[dict]:
    tools = REGISTRY.values() if names is None else [REGISTRY[n] for n in names if n in REGISTRY]
    return [t.schema() for t in tools if not t.unavailable_reason]


def _arg_hint(tool: Tool) -> str:
    """A compact "name: type (required)" listing for the tool's real
    parameters — fed back on a bad call so the model can self-correct instead
    of guessing again blind."""
    props = tool.parameters.get("properties", {})
    required = set(tool.parameters.get("required", []))
    parts = [f"{name}: {schema.get('type', 'any')}" + (" (required)" if name in required else "")
             for name, schema in props.items()]
    return ", ".join(parts) or "(no arguments)"


def _validate_args(tool: Tool, args: dict) -> str | None:
    """Checks `args` against the tool's REGISTERED schema before dispatch,
    instead of only finding out from a TypeError once `tool.func(**args)` has
    already been called. Every tool today takes named kwargs with no **kwargs
    catch-all, so an unexpected key does still raise — but relying on that is
    fragile (a future tool with a catch-all would silently swallow a
    hallucinated arg instead of surfacing it) and it's the wrong layer to
    depend on for something the schema already tells us. Returns an
    error string in the same shape the old TypeError branch used (so
    `is_tool_error` below still recognizes it), or None if `args` is clean.
    """
    if not isinstance(args, dict):
        return f"(error calling {tool.name}: arguments must be a JSON object.)"
    props = tool.parameters.get("properties", {})
    unknown = [k for k in args if k not in props]
    missing = [r for r in tool.parameters.get("required", []) if r not in args]
    def invalid(value, schema, path):
        kind = schema.get('type')
        matches = {
            'string': isinstance(value, str), 'boolean': isinstance(value, bool),
            'integer': isinstance(value, int) and not isinstance(value, bool),
            'number': isinstance(value, (int, float)) and not isinstance(value, bool),
            'array': isinstance(value, list), 'object': isinstance(value, dict),
            'null': value is None,
        }
        types = kind if isinstance(kind, list) else [kind]
        if kind and not any(matches.get(t, True) for t in types):
            return f"{path} must be {kind}"
        if 'enum' in schema and value not in schema['enum']:
            return f"{path} must be one of {schema['enum']!r}"
        if isinstance(value, list) and 'items' in schema:
            for index, item in enumerate(value):
                if problem := invalid(item, schema['items'], f'{path}[{index}]'):
                    return problem
        return None
    problems = [problem for key, value in args.items() if key in props
                and (problem := invalid(value, props[key], key))]
    if not unknown and not missing and not problems:
        return None
    bits = []
    if unknown:
        bits.append(f"unexpected argument(s) {unknown!r}")
    if missing:
        bits.append(f"missing required argument(s) {missing!r}")
    bits.extend(problems)
    return (f"(error calling {tool.name}({args!r}): {'; '.join(bits)}. "
            f"Expected arguments — {_arg_hint(tool)}. Call it again with corrected arguments.)")


def is_tool_error(result: str) -> bool:
    """True for a tool_result string produced by run_tool's own failure paths
    (bad args caught by `_validate_args`, or an exception from the tool
    itself) — every one of those is built with the same leading "(error"
    marker. Used by the agent loop so a step-limit fallback never surfaces
    one of these formatted-for-the-model error strings to the user as if it
    were a real answer (see run_agent's final fallback)."""
    return result.strip().startswith("(error")


async def run_tool(tool: Tool, args: dict) -> str:
    """Runs the tool and returns its result as a tool_result string.

    Previously an unhandled exception here (missing/misnamed/wrong-typed
    argument -> TypeError, or any other runtime error) propagated all the way
    up through the agent loop and killed the ENTIRE turn with a generic
    top-level error — the model never saw what went wrong and had no chance
    to retry with corrected arguments, which is the actual fix for a bad tool
    call. Caught here instead, so a bad call becomes an ordinary tool_result
    the model can read and correct on its next step, the same way a wrong
    file path or an ambiguous title already does.
    """
    if tool.unavailable_reason:
        return tool.unavailable_reason
    # Drop junk empty-name keys before dispatch. Small models emit `{"": ""}`
    # for a no-argument tool instead of `{}` — observed live: a `show_profile`
    # call came back as {'': ''}, raised TypeError, and burned a whole agent
    # step recovering from it. An empty key can never be a real parameter name,
    # so silently ignoring it is strictly better than failing the call.
    if args:
        args = {k: v for k, v in args.items() if k}
    # Reject a bad call BEFORE it ever reaches tool.func — see _validate_args.
    if (err := _validate_args(tool, args)) is not None:
        return err
    try:
        # A SYNC tool runs on a worker thread, not on the event loop.
        #
        # 18 registered tools are plain `def`s, and their blocking budgets are
        # not small: run_shell's subprocess.run has timeout=120, run_speed_test
        # 45, every _osascript/_run helper 20, and read_file's PDF/docx/xlsx
        # extraction is pure CPU over as many as 200 pages. Called inline, each
        # of those freezes uvicorn's single event loop for its whole duration —
        # which stops the SSE heartbeats that keep the UI from looking hung,
        # stops the /assistant/sync pushes that keep the mail/messages caches
        # warm, and stalls any second request.
        #
        # This is THREAD concurrency, not model concurrency — it is explicitly
        # not the parallel-tool-execution work that was built, measured and
        # reverted (see the block comment in agent/loop.py). Still exactly one
        # tool at a time, and nothing here touches oMLX.
        if inspect.iscoroutinefunction(tool.func):
            return str(await tool.func(**args))
        res = await asyncio.to_thread(tool.func, **args)
        # A sync function can still RETURN an awaitable (a plain `def` that
        # hands back a coroutine); to_thread only resolves the call itself.
        if inspect.isawaitable(res):
            res = await res
        return str(res)
    except TypeError as e:
        return (f"(error calling {tool.name}({args!r}): {e}. "
                f"Expected arguments — {_arg_hint(tool)}. Call it again with corrected arguments.)")
    except Exception as e:  # noqa: BLE001 — must surface as a tool_result, never crash the turn
        return f"(error running {tool.name}: {e})"
